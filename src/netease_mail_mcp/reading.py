"""Reading: full message, thread reconstruction, attachment download, folder listing."""

from __future__ import annotations

import os
import sys
import time
from datetime import timedelta
from pathlib import Path, PureWindowsPath
from typing import Any

from .errors import InvalidArgument, LimitError, MailError, NotFoundError
from .ids import MessageRef
from .imap import Folder, ImapSession, fetch_body, fetch_flags
from .mime import (
    Attachment,
    ParsedMessage,
    decode_excerpt,
    numbered_names,
    parse_message,
    path_inside,
    safe_filename,
    snippet,
    thread_subject_key,
)
from .safety import (
    MAX_DOWNLOAD_BYTES,
    TEXT_EXCERPT_CHARS,
    UNTRUSTED_NOTICE,
    sha256_hex,
    wrap_untrusted,
)
from .search import SUMMARY_ITEMS, Summary, fetch_summaries, imap_date, summarize

MAX_MESSAGE_BYTES = 80 * 1024 * 1024  # base64 inflates a 50 MB attachment to ~68 MB
SNIPPET_ITEMS = "(UID BODY.PEEK[]<0.32768>)"


def fetch_raw(session: ImapSession, uid: int) -> tuple[dict[str, Any], bytes]:
    meta = session.fetch_one(uid, "(UID FLAGS RFC822.SIZE INTERNALDATE)")
    try:
        size = int(str(meta.get("RFC822.SIZE") or 0))
    except ValueError:
        size = 0
    if size > MAX_MESSAGE_BYTES:
        raise LimitError(
            f"邮件太大（{size / 1048576:.0f} MB），超过 80 MB 上限。",
            hint="Message is larger than the 80 MB processing limit.",
        )
    record = session.fetch_one(uid, "(UID BODY.PEEK[])")
    return meta, fetch_body(record) or b""


# --------------------------------------------------------------------------- read


def read_message(
    session: ImapSession,
    ref: MessageRef,
    *,
    max_chars: int,
    include_html: bool,
    mark_read: bool,
) -> dict[str, Any]:
    state = session.open_ref(ref, readonly=not mark_read)
    meta, raw = fetch_raw(session, ref.uid)
    parsed = parse_message(raw)
    flags = fetch_flags(meta)
    if mark_read and "\\seen" not in {f.lower() for f in flags}:
        session.uid_store([ref.uid], "+FLAGS.SILENT", "(\\Seen)")
        flags.append("\\Seen")
    body = parsed.text
    result: dict[str, Any] = {
        "id": ref.encode(),
        "account": ref.account,
        "folder": state.folder.name,
        **parsed.header_dict(),
        "flags": flags,
        "unread": "\\seen" not in {f.lower() for f in flags},
        "size": len(raw),
        "text_source": parsed.text_source,
        "body": wrap_untrusted(body[:max_chars]),
        "body_chars": len(body),
        "body_truncated": len(body) > max_chars,
        "attachments": [a.to_dict() for a in parsed.attachments],
        "untrusted_content": True,
        "notice": UNTRUSTED_NOTICE,
    }
    if include_html and parsed.html:
        result["html"] = wrap_untrusted(parsed.html[:max_chars])
        result["html_truncated"] = len(parsed.html) > max_chars
    return result


# --------------------------------------------------------------------------- thread


def get_thread(
    session: ImapSession,
    ref: MessageRef,
    *,
    max_messages: int,
    max_scan: int = 2000,
    window_days: int = 180,
) -> dict[str, Any]:
    state = session.open_ref(ref)
    origin = summarize(session.account, state, session.fetch_one(ref.uid, SUMMARY_ITEMS))
    known: set[str] = {x for x in (origin.headers.message_id, origin.headers.in_reply_to) if x}
    known.update(origin.headers.references)
    key = thread_subject_key(origin.headers.subject)

    folders: list[Folder] = [state.folder]
    for role in ("inbox", "sent"):
        found = session.folder_by_role(role)
        if found and all(f.raw != found.raw for f in folders):
            folders.append(found)

    since = (origin.when - timedelta(days=window_days)).date()
    budget = max(100, max_scan // len(folders))
    candidates: list[Summary] = []
    scanned = 0
    truncated = False
    for folder in folders:
        folder_state = session.open_folder(folder)
        uids = list(reversed(session.uid_search(["SINCE", imap_date(since)])))
        if len(uids) > budget:
            truncated = True
            uids = uids[:budget]
        scanned += len(uids)
        candidates.extend(fetch_summaries(session, folder_state, uids))

    members: dict[tuple[str, int], tuple[Summary, str]] = {
        (origin.ref.folder, origin.ref.uid): (origin, "origin")
    }
    changed = True
    while changed:
        changed = False
        for item in candidates:
            k = (item.ref.folder, item.ref.uid)
            if k in members:
                continue
            h = item.headers
            links = set(h.references) | ({h.in_reply_to} if h.in_reply_to else set())
            if (h.message_id and h.message_id in known) or (links & known):
                members[k] = (item, "headers")
                if h.message_id:
                    known.add(h.message_id)
                known.update(links)
                changed = True
    if len(key) >= 3:
        for item in candidates:
            k = (item.ref.folder, item.ref.uid)
            if k not in members and thread_subject_key(item.headers.subject) == key:
                members[k] = (item, "subject")

    # The same message can sit in two folders (e.g. mail to yourself): keep one copy.
    unique: dict[str, tuple[Summary, str]] = {}
    for item, how in members.values():
        mid = item.headers.message_id or f"{item.ref.folder}/{item.ref.uid}"
        if mid not in unique or how == "origin":
            unique[mid] = (item, how)
    ordered = sorted(unique.values(), key=lambda pair: pair[0].written_key)
    omitted = 0
    if len(ordered) > max_messages:
        omitted = len(ordered) - max_messages
        keep = ordered[-max_messages:]
        if all(how != "origin" for _, how in keep):
            keep = [p for p in ordered if p[1] == "origin"] + keep[1:]
        ordered = keep

    snippets: dict[tuple[str, int], str] = {}
    by_folder: dict[str, list[Summary]] = {}
    for item, _ in ordered:
        by_folder.setdefault(item.ref.folder, []).append(item)
    for raw_name, items in by_folder.items():
        try:
            session.open_folder(session.folder_by_raw(raw_name))
            for record in session.uid_fetch([i.ref.uid for i in items], SNIPPET_ITEMS):
                parsed = parse_message(fetch_body(record) or b"")
                snippets[(raw_name, int(record["UID"]))] = snippet(parsed.text, 300)
        except MailError:
            continue

    messages = []
    for item, how in ordered:
        data = item.to_dict()
        data["match"] = how
        text = snippets.get((item.ref.folder, item.ref.uid))
        if text:
            data["snippet"] = wrap_untrusted(text)
        messages.append(data)
    out: dict[str, Any] = {
        "account": session.account,
        "messages": messages,
        "count": len(messages),
        "omitted_older": omitted,
        "folders_searched": [f.name for f in folders],
        "since": since.isoformat(),
        "scanned": scanned,
        "truncated": truncated,
        "untrusted_content": True,
        "notice": UNTRUSTED_NOTICE,
    }
    if any(how == "subject" for _, how in ordered):
        out["notes"] = [
            "match=subject 表示只按主题归入，可能不是同一会话。"
            " match=subject means grouped by subject only; it may be unrelated."
        ]
    return out


# --------------------------------------------------------------------------- download

_TEXT_EXTENSIONS = {".txt", ".csv", ".json", ".md", ".markdown", ".log", ".tsv", ".xml", ".yaml"}


def _is_textual(att: Attachment) -> bool:
    ctype = att.content_type
    if ctype.startswith("text/") or ctype in ("application/json", "application/csv"):
        return True
    return Path(att.filename).suffix.lower() in _TEXT_EXTENSIONS


def pick_attachment(parsed: ParsedMessage, index: int | None, filename: str | None) -> Attachment:
    if index is None and not filename:
        raise InvalidArgument(
            "需要 index 或 filename（见 read_message 返回的 attachments）。",
            hint="Pass index or filename (see read_message's attachments).",
        )
    if index is not None:
        for att in parsed.attachments:
            if att.index == index:
                return att
    if filename:
        for att in parsed.attachments:
            if att.filename == filename:
                return att
        lowered = filename.casefold()
        for att in parsed.attachments:
            if att.filename.casefold() == lowered:
                return att
    names = ", ".join(f"{a.index}: {a.filename}" for a in parsed.attachments) or "（无附件 none）"
    raise NotFoundError(f"找不到这个附件。附件列表：{names}", hint=f"Attachment not found: {names}")


_CREATE_FLAGS = (
    os.O_WRONLY
    | os.O_CREAT
    | os.O_EXCL
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_BINARY", 0)
    | getattr(os, "O_CLOEXEC", 0)
)
MAX_NAME_ATTEMPTS = 1000


def _outside_error(dest_dir: str, root: str) -> InvalidArgument:
    return InvalidArgument(
        f'dest_dir 必须是下载目录 {root} 里的子目录（可以写相对路径，如 "发票"），'
        f"收到：{dest_dir}。要换下载目录，请设置 NETEASE_MAIL_DOWNLOAD_DIR。",
        hint=f"dest_dir must be a subdirectory of the download dir {root} (a relative path such "
        f"as 'invoices' works); got {dest_dir!r}. Set NETEASE_MAIL_DOWNLOAD_DIR to change the "
        "download dir.",
    )


def _make_private_dirs(path: Path) -> None:
    """mkdir -p where every directory created here is 0700 (mkdir(parents=True) ignores mode)."""
    missing: list[Path] = []
    current = path
    while not current.exists() and current != current.parent:
        missing.append(current)
        current = current.parent
    for directory in reversed(missing):
        try:
            os.mkdir(directory, 0o700)
        except FileExistsError:
            pass
    if not path.is_dir():
        raise NotADirectoryError(str(path))


def download_directory(download_dir: Path, dest_dir: str | None) -> Path:
    """Resolve where to save: the download dir or a subdirectory of it (created 0700).

    Symlinks are resolved before the containment check, so a link inside the download dir
    cannot point the download elsewhere. UNC and drive-relative forms are refused outright.
    """
    base = Path(download_dir).expanduser()
    try:
        _make_private_dirs(base)
    except OSError as exc:
        raise InvalidArgument(
            f"无法创建下载目录 {base}：{exc}", hint=f"Cannot create the download dir {base}: {exc}"
        ) from None
    root = os.path.realpath(base)
    if not os.path.isdir(root):
        raise InvalidArgument(f"下载目录不是文件夹：{root}", hint=f"Not a directory: {root}")
    if not dest_dir or not dest_dir.strip():
        return Path(root)
    text = dest_dir.strip()
    windows = PureWindowsPath(text)
    if (
        "\x00" in text
        or text.startswith(("\\\\", "//"))
        or windows.drive.startswith(("\\\\", "//"))
        or (windows.drive and not windows.root)
    ):
        raise _outside_error(dest_dir, root)
    candidate = Path(text).expanduser()
    if not candidate.is_absolute():
        candidate = Path(root) / candidate
    if not path_inside(os.path.realpath(candidate), root):
        raise _outside_error(dest_dir, root)
    try:
        _make_private_dirs(candidate)
    except OSError as exc:
        raise InvalidArgument(
            f"无法创建目录 {candidate}：{exc}", hint=f"Cannot create {candidate}: {exc}"
        ) from None
    final = os.path.realpath(candidate)  # re-check: a link may have appeared meanwhile
    if not path_inside(final, root) or not os.path.isdir(final):
        raise _outside_error(dest_dir, root)
    return Path(final)


def _create_exclusive(directory: Path, filename: str) -> tuple[Path, int]:
    """Create a new file (0600, never following or replacing an existing entry)."""
    for name in numbered_names(filename, MAX_NAME_ATTEMPTS):
        path = directory / name
        try:
            return path, os.open(path, _CREATE_FLAGS, 0o600)
        except FileExistsError:
            continue
        except OSError as exc:
            raise InvalidArgument(
                f"无法在 {directory} 创建文件：{exc}", hint=f"Cannot create a file: {exc}"
            ) from None
    raise LimitError(
        f"{directory} 里已有太多同名文件（{filename}）。",
        hint=f"Too many files named like {filename!r} in {directory}.",
    )


_QUARANTINE_VALUE = "0081;{:08x};netease-mail-mcp;"


def _quarantine_macos(path: Path) -> str | None:
    import ctypes
    import ctypes.util

    libc = ctypes.CDLL(ctypes.util.find_library("c") or "libc.dylib", use_errno=True)
    setxattr = libc.setxattr
    setxattr.argtypes = [
        ctypes.c_char_p,
        ctypes.c_char_p,
        ctypes.c_char_p,
        ctypes.c_size_t,
        ctypes.c_uint32,
        ctypes.c_int,
    ]
    setxattr.restype = ctypes.c_int
    value = _QUARANTINE_VALUE.format(int(time.time())).encode("ascii")
    xattr_nofollow = 0x0001
    name = b"com.apple.quarantine"
    if setxattr(os.fsencode(path), name, value, len(value), 0, xattr_nofollow) != 0:
        return None
    return "com.apple.quarantine"


def _zone_identifier(path: Path) -> str | None:
    with open(f"{path}:Zone.Identifier", "wb") as fh:
        fh.write(b"[ZoneTransfer]\r\nZoneId=3\r\n")
    return "Zone.Identifier"


def mark_downloaded(path: Path) -> str | None:
    """Best effort: tag the file as downloaded from the internet. Never raises."""
    try:
        if sys.platform == "darwin":
            return _quarantine_macos(path)
        if os.name == "nt":
            return _zone_identifier(path)
    except Exception:
        return None
    return None


def download_attachment(
    session: ImapSession,
    ref: MessageRef,
    *,
    index: int | None,
    filename: str | None,
    dest_dir: str | None,
    default_dir: Path,
) -> dict[str, Any]:
    directory = download_directory(default_dir, dest_dir)
    session.open_ref(ref)
    _, raw = fetch_raw(session, ref.uid)
    parsed = parse_message(raw)
    att = pick_attachment(parsed, index, filename)
    data = att.payload()
    if len(data) > MAX_DOWNLOAD_BYTES:
        raise LimitError(
            f"附件 {len(data) / 1048576:.1f} MB，超过 50 MB 上限。",
            hint="Attachment exceeds the 50 MB download limit.",
        )
    path, fd = _create_exclusive(directory, safe_filename(att.filename))
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
    except OSError as exc:
        try:
            os.unlink(path)
        except OSError:
            pass
        raise InvalidArgument(f"写入 {path} 失败：{exc}", hint=f"Writing failed: {exc}") from None
    result: dict[str, Any] = {
        "path": str(path),
        "filename": att.filename,
        "saved_as": path.name,
        "size": len(data),
        "sha256": sha256_hex(data),
        "content_type": att.content_type,
        "quarantine": mark_downloaded(path),
        "untrusted_content": True,
    }
    if _is_textual(att):
        try:
            charset = att.part.get_content_charset()
        except Exception:
            charset = None
        text, truncated = decode_excerpt(data, charset, TEXT_EXCERPT_CHARS)
        result["text_excerpt"] = wrap_untrusted(text)
        result["text_excerpt_truncated"] = truncated
        result["notice"] = UNTRUSTED_NOTICE
    return result


# --------------------------------------------------------------------------- folders


def list_folders(session: ImapSession, *, with_counts: bool) -> dict[str, Any]:
    folders = session.folders(refresh=True)
    items = []
    for folder in folders:
        data = folder.to_dict()
        if with_counts and folder.selectable:
            try:
                status = session.status(folder, "(MESSAGES UNSEEN)")
                data["messages"] = status.get("MESSAGES")
                data["unseen"] = status.get("UNSEEN")
            except MailError as exc:
                data["error"] = exc.message
        items.append(data)
    return {"account": session.account, "folders": items}
