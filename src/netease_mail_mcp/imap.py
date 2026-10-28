"""IMAP layer for NetEase: login + ID, mUTF-7, LIST/FETCH parsing, sessions, pooling.

NetEase specifics handled here:

* After LOGIN the client must send ``ID``; without it SELECT/EXAMINE fail with
  ``NO SELECT Unsafe Login``. :mod:`imaplib` does not know ``ID``, so it is registered below.
* Folder names are modified UTF-7 (``&XfJT0ZAB-`` = 已发送); special-use flags are advertised.
* Never interpolate untrusted strings into commands: :func:`quote` rejects CR/LF/NUL and
  non-ASCII (Python 3.15's imaplib also rejects CR/LF in arguments).
"""

from __future__ import annotations

import base64
import imaplib
import re
import ssl
import threading
import time
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from . import __version__
from .credentials import CredentialStore
from .errors import (
    AuthError,
    ConnectionFailed,
    InvalidArgument,
    MailError,
    MailTimeout,
    NotFoundError,
    ProtocolError,
    StaleIdError,
)
from .ids import MessageRef
from .providers import UNSAFE_LOGIN_HINT, login_failure_hint, provider_for
from .safety import FETCH_CHUNK

imaplib.Commands.setdefault("ID", ("AUTH", "SELECTED"))
imaplib.Commands.setdefault("MOVE", ("SELECTED",))
imaplib.Commands.setdefault("UNSELECT", ("SELECTED",))

ID_PARAMS: tuple[tuple[str, str], ...] = (
    ("name", "netease-mail-mcp"),
    ("version", __version__),
    ("vendor", "netease-mail-mcp"),
    ("support-url", "https://github.com/SeraFxy/netease-mail-mcp"),
)

# --------------------------------------------------------------------------- quoting


def quote(value: str) -> str:
    """Quote an IMAP string argument; refuse anything that could break the command line."""
    if re.search(r"[\r\n\x00]", value):
        raise InvalidArgument(
            "参数里不能有换行符。", hint="IMAP arguments must not contain CR, LF or NUL."
        )
    if not value.isascii():
        raise InvalidArgument(
            "IMAP 参数必须是 ASCII（文件夹名会自动编码）。",
            hint="IMAP arguments must be ASCII (folder names are encoded automatically).",
        )
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def id_argument() -> str:
    return "(" + " ".join(f"{quote(k)} {quote(v)}" for k, v in ID_PARAMS) + ")"


def uid_set(uids: Sequence[int]) -> str:
    """Compress UIDs into an IMAP sequence set: [1,2,3,7,9,10] -> '1:3,7,9:10'."""
    ordered = sorted(set(int(u) for u in uids))
    if not ordered:
        raise InvalidArgument("UID 列表为空", hint="empty UID set")
    parts: list[str] = []
    start = prev = ordered[0]
    for uid in ordered[1:]:
        if uid == prev + 1:
            prev = uid
            continue
        parts.append(f"{start}:{prev}" if start != prev else str(start))
        start = prev = uid
    parts.append(f"{start}:{prev}" if start != prev else str(start))
    return ",".join(parts)


# --------------------------------------------------------------------------- modified UTF-7


def encode_mutf7(text: str) -> str:
    out: list[str] = []
    pending: list[str] = []

    def flush() -> None:
        if pending:
            raw = "".join(pending).encode("utf-16-be")
            out.append("&" + base64.b64encode(raw).decode("ascii").rstrip("=").replace("/", ","))
            out.append("-")
            pending.clear()

    for ch in text:
        if 0x20 <= ord(ch) <= 0x7E:
            flush()
            out.append("&-" if ch == "&" else ch)
        else:
            pending.append(ch)
    flush()
    return "".join(out)


def decode_mutf7(text: str) -> str:
    out: list[str] = []
    i = 0
    while i < len(text):
        ch = text[i]
        if ch != "&":
            out.append(ch)
            i += 1
            continue
        end = text.find("-", i + 1)
        if end == -1:
            out.append(text[i:])
            break
        if end == i + 1:
            out.append("&")
        else:
            chunk = text[i + 1 : end].replace(",", "/")
            try:
                out.append(base64.b64decode(chunk + "=" * (-len(chunk) % 4)).decode("utf-16-be"))
            except (ValueError, UnicodeDecodeError):
                out.append(text[i : end + 1])
        i = end + 1
    return "".join(out)


# --------------------------------------------------------------------------- response parsing

_LITERAL_TAIL = re.compile(rb"\{(\d+)\+?\}\s*$")


def group_responses(data: Sequence[Any]) -> list[tuple[bytes, list[bytes]]]:
    """Regroup imaplib's untagged data into one entry per server response.

    imaplib returns ``(line_up_to_literal, literal)`` tuples followed by the bytes that
    trail the last literal. A single FETCH response may contain several literals (e.g. a
    non-ASCII filename in BODYSTRUCTURE plus the header block), so tuples are accumulated
    until the trailing bytes element. Literals are replaced by ``\\x00<n>\\x00`` markers.
    """
    groups: list[tuple[bytes, list[bytes]]] = []
    buf = b""
    literals: list[bytes] = []
    for item in data:
        if item is None:
            continue
        if isinstance(item, tuple):
            head = bytes(item[0])
            match = _LITERAL_TAIL.search(head)
            if match:
                head = head[: match.start()]
            buf += head + b"\x00%d\x00" % len(literals)
            literals.append(bytes(item[1]))
        else:
            buf += bytes(item)
            groups.append((buf, literals))
            buf, literals = b"", []
    if buf or literals:
        groups.append((buf, literals))
    return groups


class _Parser:
    """Tiny parser for IMAP response data: atoms, "quoted", {literal}, (lists), NIL."""

    def __init__(self, buf: bytes, literals: list[bytes]) -> None:
        self.buf = buf
        self.literals = literals
        self.pos = 0

    def _skip(self) -> None:
        while self.pos < len(self.buf) and self.buf[self.pos] in b" \t\r\n":
            self.pos += 1

    def done(self) -> bool:
        self._skip()
        return self.pos >= len(self.buf)

    def value(self) -> Any:
        self._skip()
        if self.pos >= len(self.buf):
            raise ValueError("unexpected end of response")
        ch = self.buf[self.pos]
        if ch == 0x28:  # (
            self.pos += 1
            items: list[Any] = []
            while True:
                self._skip()
                if self.pos >= len(self.buf):
                    return items
                if self.buf[self.pos] == 0x29:  # )
                    self.pos += 1
                    return items
                items.append(self.value())
        if ch == 0x22:  # "
            self.pos += 1
            out = bytearray()
            while self.pos < len(self.buf):
                c = self.buf[self.pos]
                if c == 0x5C and self.pos + 1 < len(self.buf):
                    out.append(self.buf[self.pos + 1])
                    self.pos += 2
                    continue
                self.pos += 1
                if c == 0x22:
                    break
                out.append(c)
            return bytes(out).decode("utf-8", "surrogateescape")
        if ch == 0x00:
            end = self.buf.index(b"\x00", self.pos + 1)
            index = int(self.buf[self.pos + 1 : end])
            self.pos = end + 1
            return self.literals[index]
        start = self.pos
        depth = 0
        while self.pos < len(self.buf):
            c = self.buf[self.pos]
            if c == 0x5B:  # [
                depth += 1
            elif c == 0x5D:  # ]
                depth = max(0, depth - 1)
            elif depth == 0 and c in b" ()\x00":
                break
            self.pos += 1
        if self.pos == start:  # stray ')' at top level
            self.pos += 1
            return None
        atom = self.buf[start : self.pos].decode("utf-8", "surrogateescape")
        return None if atom.upper() == "NIL" else atom


def as_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", "surrogateescape")
    return str(value)


def as_bytes(value: Any) -> bytes:
    if value is None:
        return b""
    if isinstance(value, bytes):
        return value
    return str(value).encode("utf-8", "surrogateescape")


def parse_fetch(data: Sequence[Any]) -> list[dict[str, Any]]:
    """Parse ``UID FETCH`` data into dicts keyed by upper-case item name."""
    records: list[dict[str, Any]] = []
    for buf, literals in group_responses(data):
        parser = _Parser(buf, literals)
        try:
            parser.value()  # sequence number
            items = parser.value()
        except (ValueError, IndexError):
            continue
        if not isinstance(items, list):
            continue
        record: dict[str, Any] = {}
        for i in range(0, len(items) - 1, 2):
            key = items[i]
            if isinstance(key, str):
                record[key.upper()] = items[i + 1]
        if "UID" in record:
            try:
                record["UID"] = int(as_text(record["UID"]))
            except ValueError:
                continue
            records.append(record)
    return records


def fetch_body(record: dict[str, Any]) -> bytes | None:
    for key, value in record.items():
        if key.startswith("BODY[") or key in ("RFC822", "RFC822.HEADER"):
            return as_bytes(value) if value is not None else b""
    return None


def fetch_flags(record: dict[str, Any]) -> list[str]:
    flags = record.get("FLAGS") or []
    return [as_text(f) for f in flags] if isinstance(flags, list) else []


_MONTHS = {
    m: i
    for i, m in enumerate(
        ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), 1
    )
}
_INTERNALDATE_RE = re.compile(
    r"\s*(\d{1,2})-([A-Za-z]{3})-(\d{4}) (\d{2}):(\d{2}):(\d{2}) ([+-])(\d{2})(\d{2})"
)


def parse_internaldate(value: Any) -> datetime | None:
    match = _INTERNALDATE_RE.match(as_text(value))
    if not match:
        return None
    day, mon, year, hh, mm, ss, sign, tzh, tzm = match.groups()
    month = _MONTHS.get(mon.lower())
    if not month:
        return None
    offset = timedelta(hours=int(tzh), minutes=int(tzm)) * (1 if sign == "+" else -1)
    try:
        return datetime(
            int(year), month, int(day), int(hh), int(mm), int(ss), tzinfo=timezone(offset)
        )
    except ValueError:
        return None


# --------------------------------------------------------------------------- folders

_ROLE_FLAGS = {
    "\\drafts": "drafts",
    "\\sent": "sent",
    "\\trash": "trash",
    "\\junk": "junk",
    "\\archive": "archive",
    "\\all": "all",
    "\\flagged": "flagged",
}
_ROLE_NAMES = {
    "inbox": "inbox",
    "收件箱": "inbox",
    "草稿箱": "drafts",
    "drafts": "drafts",
    "draft": "drafts",
    "已发送": "sent",
    "sent": "sent",
    "sent messages": "sent",
    "sent items": "sent",
    "已删除": "trash",
    "trash": "trash",
    "deleted": "trash",
    "deleted messages": "trash",
    "垃圾邮件": "junk",
    "junk": "junk",
    "spam": "junk",
    "病毒文件夹": "virus",
    "virus": "virus",
}
# Roles users can name instead of a folder ("sent", "草稿箱", "\\Trash", ...).
ROLE_ALIASES = {**_ROLE_NAMES, **{flag: role for flag, role in _ROLE_FLAGS.items()}}
EXCLUDED_FROM_ALL = frozenset({"junk", "trash", "virus"})


@dataclass(frozen=True)
class Folder:
    raw: str
    name: str
    delimiter: str | None
    flags: tuple[str, ...]
    role: str | None

    @property
    def selectable(self) -> bool:
        lowered = {f.lower() for f in self.flags}
        return not ({"\\noselect", "\\nonexistent"} & lowered)

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {"name": self.name, "raw": self.raw, "role": self.role}
        if not self.selectable:
            data["selectable"] = False
        return data


def _role_for(raw: str, name: str, flags: Sequence[str]) -> str | None:
    if raw.upper() == "INBOX":
        return "inbox"
    for flag in flags:
        role = _ROLE_FLAGS.get(flag.lower())
        if role and role not in ("all", "flagged"):
            return role
    return _ROLE_NAMES.get(name.strip().lower())


def parse_list(data: Sequence[Any]) -> list[Folder]:
    folders: list[Folder] = []
    for buf, literals in group_responses(data):
        parser = _Parser(buf, literals)
        try:
            flags = parser.value()
            delimiter = parser.value()
            name = parser.value()
        except (ValueError, IndexError):
            continue
        if not isinstance(flags, list) or name is None:
            continue
        raw = as_text(name)
        if raw.upper() == "INBOX":
            raw = "INBOX"
        flag_list = tuple(as_text(f) for f in flags)
        decoded = "INBOX" if raw == "INBOX" else decode_mutf7(raw)
        folders.append(
            Folder(
                raw=raw,
                name=decoded,
                delimiter=as_text(delimiter) or None,
                flags=flag_list,
                role=_role_for(raw, decoded, flag_list),
            )
        )
    return folders


def parse_status(data: Sequence[Any]) -> dict[str, int]:
    for buf, literals in group_responses(data):
        parser = _Parser(buf, literals)
        try:
            parser.value()
            items = parser.value()
        except (ValueError, IndexError):
            continue
        if isinstance(items, list):
            result: dict[str, int] = {}
            for i in range(0, len(items) - 1, 2):
                try:
                    result[as_text(items[i]).upper()] = int(as_text(items[i + 1]))
                except ValueError:
                    pass
            return result
    return {}


def _response_text(data: Any) -> str:
    if not data:
        return ""
    parts = data if isinstance(data, list) else [data]
    return " ".join(as_text(p[0] if isinstance(p, tuple) else p) for p in parts if p).strip()


_APPENDUID_RE = re.compile(r"APPENDUID (\d+) (\d+)", re.IGNORECASE)
_COPYUID_RE = re.compile(r"COPYUID (\d+) ([\d:,]+) ([\d:,]+)", re.IGNORECASE)


def _expand_set(text: str) -> list[int]:
    out: list[int] = []
    for part in text.split(","):
        if ":" in part:
            lo, hi = (int(x) for x in part.split(":", 1))
            out.extend(range(min(lo, hi), max(lo, hi) + 1))
        elif part:
            out.append(int(part))
    return out


def parse_copyuid(text: str) -> tuple[int, dict[int, int]] | None:
    match = _COPYUID_RE.search(text)
    if not match:
        return None
    src = _expand_set(match.group(2))
    dst = _expand_set(match.group(3))
    if len(src) != len(dst):
        return None
    return int(match.group(1)), dict(zip(src, dst))


# --------------------------------------------------------------------------- session


@dataclass(frozen=True)
class MailboxState:
    folder: Folder
    uidvalidity: int
    uidnext: int | None
    exists: int
    readonly: bool
    flags: frozenset[str] = frozenset()  # the mailbox's FLAGS response, lower-cased


ConnFactory = Callable[[str, int, float], imaplib.IMAP4]


def ssl_connection(host: str, port: int, timeout: float) -> imaplib.IMAP4:
    return imaplib.IMAP4_SSL(host, port, ssl_context=ssl.create_default_context(), timeout=timeout)


class ImapSession:
    """One logged-in connection for one account. Not thread-safe; the pool serialises use."""

    def __init__(self, account: str, conn: imaplib.IMAP4, *, id_accepted: bool) -> None:
        self.account = account
        self.conn = conn
        self.id_accepted = id_accepted
        self.capabilities = {str(c).upper() for c in getattr(conn, "capabilities", ())}
        self.state: MailboxState | None = None
        self.broken = False
        self.last_used = time.monotonic()
        self._folders: list[Folder] | None = None
        self._listed_this_call = False  # LIST already re-read since the last reset()

    # -- plumbing ----------------------------------------------------------------------

    def has(self, capability: str) -> bool:
        return capability.upper() in self.capabilities

    def call(self, what: str, fn: Callable[..., tuple[str, Any]], *args: Any) -> tuple[str, Any]:
        try:
            typ, data = fn(*args)
        except imaplib.IMAP4.abort as exc:
            self.broken = True
            raise ConnectionFailed(
                f"{what}：连接被服务器断开（{exc}）", hint=f"{what}: connection aborted ({exc})"
            ) from None
        except TimeoutError:
            self.broken = True
            raise MailTimeout(
                f"{what}：服务器响应超时。", hint=f"{what}: the server did not answer in time."
            ) from None
        except OSError as exc:
            self.broken = True
            raise ConnectionFailed(f"{what}：网络错误（{exc}）", hint=f"{what}: {exc}") from None
        except imaplib.IMAP4.error as exc:
            raise ProtocolError(f"{what} 失败：{exc}", hint=f"{what} failed: {exc}") from None
        self.last_used = time.monotonic()
        return typ, data

    def ok(self, what: str, typ: str, data: Any) -> None:
        if typ == "OK":
            return
        text = _response_text(data)
        if "unsafe login" in text.lower():
            raise ProtocolError(f"{what} 失败：{text}", hint=UNSAFE_LOGIN_HINT)
        raise ProtocolError(f"{what} 失败：{text}", hint=f"{what} failed: {text}")

    def reset(self) -> None:
        self.state = None
        self._listed_this_call = False

    # -- folders -----------------------------------------------------------------------

    def folders(self, refresh: bool = False) -> list[Folder]:
        if self._folders is None or refresh:
            typ, data = self.call("LIST", self.conn.list)
            self.ok("LIST", typ, data)
            self._folders = parse_list(data)
            self._listed_this_call = True
        return self._folders

    def _relist_after_miss(self) -> bool:
        """A pooled session's folder list can be stale (folder created meanwhile): re-read
        LIST once per tool call after a lookup misses. True if it was re-read."""
        if self._listed_this_call:
            return False
        self.folders(refresh=True)
        return True

    def folder_by_role(self, role: str) -> Folder | None:
        for _attempt in range(2):
            for folder in self.folders():
                if folder.role == role and folder.selectable:
                    return folder
            if not self._relist_after_miss():
                break
        return None

    def folder_by_raw(self, raw: str) -> Folder:
        for _attempt in range(2):
            for folder in self.folders():
                if folder.raw == raw:
                    return folder
            if raw.upper() == "INBOX" or not self._relist_after_miss():
                break
        if raw.upper() == "INBOX":
            return Folder("INBOX", "INBOX", "/", (), "inbox")
        raise NotFoundError(
            f"文件夹 {decode_mutf7(raw)} 已不存在，请重新搜索。",
            hint="The folder no longer exists; search again.",
        )

    def _find_folder(self, text: str) -> Folder | None:
        folders = self.folders()
        for folder in folders:
            if text in (folder.name, folder.raw):
                return folder
        lowered = text.lower()
        for folder in folders:
            if lowered in (folder.name.lower(), folder.raw.lower()):
                return folder
        role = ROLE_ALIASES.get(lowered)
        if role:
            for folder in folders:
                if folder.role == role and folder.selectable:
                    return folder
        return None

    def resolve_folder(self, spec: str | None) -> Folder:
        text = (spec or "INBOX").strip()
        if text.upper() == "INBOX" or text == "收件箱":
            return next(
                (f for f in self.folders() if f.raw == "INBOX"),
                Folder("INBOX", "INBOX", "/", (), "inbox"),
            )
        found = self._find_folder(text)
        if found is None and self._relist_after_miss():
            found = self._find_folder(text)
        if found is not None:
            return found
        names = ", ".join(f.name for f in self.folders() if f.selectable)
        raise NotFoundError(
            f"找不到文件夹 {text!r}。可用文件夹：{names}",
            hint=f"Folder {text!r} not found. Available: {names}",
        )

    # -- mailbox state -----------------------------------------------------------------

    def open_folder(
        self, folder: Folder, *, readonly: bool = True, force: bool = False
    ) -> MailboxState:
        current = self.state
        if (
            not force
            and current is not None
            and current.folder.raw == folder.raw
            and (current.readonly == readonly or not current.readonly)
        ):
            return current
        verb = "EXAMINE" if readonly else "SELECT"
        self.state = None
        typ, data = self.call(
            f"{verb} {folder.name}", self.conn.select, quote(folder.raw), readonly
        )
        self.ok(f"打开文件夹 {folder.name} ({verb})", typ, data)
        responses = self.conn.untagged_responses
        uidvalidity = _last_int(responses.get("UIDVALIDITY"))
        if uidvalidity is None:
            raise ProtocolError(
                f"服务器没有返回 {folder.name} 的 UIDVALIDITY。",
                hint="Server did not report UIDVALIDITY.",
            )
        exists = _last_int(data if isinstance(data, list) else None) or 0
        self.state = MailboxState(
            folder=folder,
            uidvalidity=uidvalidity,
            uidnext=_last_int(responses.get("UIDNEXT")),
            exists=exists,
            readonly=readonly,
            flags=_flag_set(responses.get("FLAGS")),
        )
        return self.state

    def open_ref(self, ref: MessageRef, *, readonly: bool = True) -> MailboxState:
        folder = self.folder_by_raw(ref.folder)
        state = self.open_folder(folder, readonly=readonly)
        if state.uidvalidity != ref.uidvalidity:
            raise StaleIdError(
                f"文件夹 {folder.name} 已变化（UIDVALIDITY 不同），这个 id 失效了，请重新搜索。",
                hint="Mailbox changed (UIDVALIDITY mismatch); search again to get fresh ids.",
            )
        return state

    # -- commands ----------------------------------------------------------------------

    def uid_search(self, criteria: Sequence[str]) -> list[int]:
        typ, data = self.call("SEARCH", self.conn.uid, "SEARCH", *criteria)
        self.ok("SEARCH", typ, data)
        uids: set[int] = set()
        for chunk in data or []:
            if chunk:
                uids.update(int(x) for x in as_text(chunk).split() if x.isdigit())
        return sorted(uids)

    def uid_fetch(
        self, uids: Sequence[int], items: str, chunk: int = FETCH_CHUNK
    ) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        ordered = list(uids)
        for start in range(0, len(ordered), chunk):
            part = ordered[start : start + chunk]
            wanted = set(part)
            typ, data = self.call("FETCH", self.conn.uid, "FETCH", uid_set(part), items)
            self.ok("FETCH", typ, data)
            records.extend(r for r in parse_fetch(data) if r["UID"] in wanted)
        return records

    def fetch_one(self, uid: int, items: str) -> dict[str, Any]:
        records = self.uid_fetch([uid], items)
        if not records:
            name = self.state.folder.name if self.state else "?"
            raise NotFoundError(
                f"文件夹 {name} 里没有这封邮件（可能已被移动或删除），请重新搜索。",
                hint="Message not found in its folder (moved or deleted?); search again.",
            )
        return records[0]

    def status(
        self, folder: Folder, items: str = "(MESSAGES UNSEEN UIDNEXT UIDVALIDITY)"
    ) -> dict[str, int]:
        typ, data = self.call(f"STATUS {folder.name}", self.conn.status, quote(folder.raw), items)
        self.ok(f"STATUS {folder.name}", typ, data)
        return parse_status(data)

    def append(
        self, folder: Folder, flags: str, message: bytes, when: float | None = None
    ) -> tuple[int | None, int | None]:
        """APPEND a message; returns (uidvalidity, uid) from APPENDUID when available."""
        typ, data = self.call(
            f"APPEND {folder.name}",
            self.conn.append,
            quote(folder.raw),
            flags,
            when if when is not None else time.time(),
            message,
        )
        self.ok(f"保存到 {folder.name} (APPEND)", typ, data)
        match = _APPENDUID_RE.search(_response_text(data))
        if not match:
            return None, None
        return int(match.group(1)), int(match.group(2))

    def uid_store(self, uids: Sequence[int], operation: str, flags: str) -> None:
        typ, data = self.call("STORE", self.conn.uid, "STORE", uid_set(uids), operation, flags)
        self.ok("STORE", typ, data)

    def _pop_copyuid(self, data: Any) -> tuple[int, dict[int, int]] | None:
        # imaplib files bracketed codes ([COPYUID ...]) from tagged and untagged lines alike
        # under untagged_responses["COPYUID"].
        copied = self.conn.untagged_responses.pop("COPYUID", None)
        text = _response_text(data)
        if copied:
            text += " COPYUID " + as_text(copied[-1])
        return parse_copyuid(text)

    def uid_copy(self, uids: Sequence[int], target: Folder) -> tuple[int, dict[int, int]] | None:
        self.conn.untagged_responses.pop("COPYUID", None)
        typ, data = self.call("COPY", self.conn.uid, "COPY", uid_set(uids), quote(target.raw))
        self.ok(f"COPY → {target.name}", typ, data)
        return self._pop_copyuid(data)

    def uid_move(self, uids: Sequence[int], target: Folder) -> tuple[int, dict[int, int]] | None:
        self.conn.untagged_responses.pop("COPYUID", None)
        typ, data = self.call("MOVE", self.conn.uid, "MOVE", uid_set(uids), quote(target.raw))
        self.ok(f"MOVE → {target.name}", typ, data)
        return self._pop_copyuid(data)

    def uid_expunge(self, uids: Sequence[int]) -> None:
        """Expunge exactly these UIDs (requires UIDPLUS). Plain EXPUNGE is never used."""
        if not self.has("UIDPLUS"):
            raise ProtocolError(
                "服务器不支持 UIDPLUS，无法只删除指定邮件，已停止。",
                hint="Server lacks UIDPLUS; refusing to EXPUNGE more than the given UIDs.",
            )
        typ, data = self.call("UID EXPUNGE", self.conn.uid, "EXPUNGE", uid_set(uids))
        self.ok("UID EXPUNGE", typ, data)

    def noop(self) -> None:
        typ, data = self.call("NOOP", self.conn.noop)
        self.ok("NOOP", typ, data)

    def close(self) -> None:
        if self.broken:
            _discard(self.conn)  # a dead or timed-out connection: no LOGOUT round trip
        else:
            try:
                self.conn.logout()
            except Exception:
                _discard(self.conn)
        self.broken = True


def _flag_set(values: Any) -> frozenset[str]:
    if not values:
        return frozenset()
    return frozenset(f.lower() for f in re.findall(r"\\?[^\s()\\]+", as_text(values[-1])))


def _last_int(values: Any) -> int | None:
    if not values:
        return None
    for value in reversed(values):
        try:
            return int(as_text(value).split()[0])
        except (ValueError, IndexError):
            continue
    return None


def send_id(conn: imaplib.IMAP4) -> bool:
    try:
        typ, data = conn._simple_command("ID", id_argument())
        conn._untagged_response(typ, data, "ID")
        return bool(typ == "OK")
    except imaplib.IMAP4.abort:
        raise
    except imaplib.IMAP4.error:
        return False


def _discard(conn: imaplib.IMAP4) -> None:
    """Close the socket without a LOGOUT round trip (the connection may be dead or stuck)."""
    try:
        conn.shutdown()
    except Exception:
        pass


def _network_error(what: str, exc: BaseException) -> MailError:
    if isinstance(exc, TimeoutError):
        return MailTimeout(f"{what}：服务器响应超时。", hint=f"{what}: the server did not answer.")
    if isinstance(exc, imaplib.IMAP4.abort):
        return ConnectionFailed(f"{what}：连接被服务器断开（{exc}）", hint=f"{what}: {exc}")
    if isinstance(exc, OSError):
        return ConnectionFailed(f"{what}：网络错误（{exc}）", hint=f"{what}: {exc}")
    return ProtocolError(
        f"{what}：意外错误（{type(exc).__name__}: {exc}）", hint=f"{what}: {type(exc).__name__}"
    )


def open_session(
    account: str,
    auth_code: str,
    *,
    factory: ConnFactory = ssl_connection,
    timeout: float = 30.0,
) -> ImapSession:
    """Connect, LOGIN, ID and re-read CAPABILITY. Network failures become MailErrors and the
    connection is always closed when anything after connecting fails."""
    provider = provider_for(account)
    where = f"{provider.imap_host}:{provider.imap_port}"
    try:
        conn = factory(provider.imap_host, provider.imap_port, timeout)
    except TimeoutError:
        raise MailTimeout(
            f"连接 {where} 超时。", hint=f"Connecting to {where} timed out."
        ) from None
    except (OSError, imaplib.IMAP4.error) as exc:
        raise ConnectionFailed(
            f"无法连接 {where}：{exc}", hint=f"Cannot connect to {where}: {exc}"
        ) from None
    try:
        conn.login(account, auth_code)
    except imaplib.IMAP4.abort as exc:
        _discard(conn)
        raise _network_error("登录 LOGIN", exc) from None
    except imaplib.IMAP4.error as exc:
        _quiet_logout(conn)
        raise AuthError(f"{account} 登录失败：{exc}", hint=login_failure_hint(account)) from None
    except Exception as exc:
        _discard(conn)
        raise _network_error("登录 LOGIN", exc) from None
    try:
        accepted = send_id(conn)
        refresh_capabilities(conn)
    except Exception as exc:
        _discard(conn)
        raise _network_error("发送 IMAP ID / CAPABILITY", exc) from None
    return ImapSession(account, conn, id_accepted=accepted)


def refresh_capabilities(conn: imaplib.IMAP4) -> None:
    """Re-read CAPABILITY after login: servers may advertise more once authenticated."""
    try:
        typ, data = conn.capability()
    except imaplib.IMAP4.abort:
        raise
    except imaplib.IMAP4.error:
        return
    if typ == "OK" and data and data[-1]:
        conn.capabilities = tuple(as_text(data[-1]).upper().split())


def _quiet_logout(conn: imaplib.IMAP4) -> None:
    try:
        conn.logout()
    except Exception:
        pass


# --------------------------------------------------------------------------- pool


class ConnectionPool:
    """Short-lived per-account connection reuse.

    One idle session per account is kept for ``idle_ttl`` seconds and checked with NOOP
    before reuse. There is no background thread: expiry happens on the next use and on
    :meth:`close_all` (called at shutdown).
    """

    def __init__(
        self,
        credentials: CredentialStore,
        *,
        factory: ConnFactory = ssl_connection,
        timeout: float = 30.0,
        idle_ttl: float = 60.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.credentials = credentials
        self.factory = factory
        self.timeout = timeout
        self.idle_ttl = idle_ttl
        self.clock = clock
        self._idle: dict[str, tuple[ImapSession, float]] = {}
        self._locks: dict[str, threading.Lock] = {}
        self._guard = threading.Lock()

    def _lock_for(self, account: str) -> threading.Lock:
        with self._guard:
            return self._locks.setdefault(account, threading.Lock())

    def connect(self, account: str) -> ImapSession:
        return open_session(
            account, self.credentials.get(account), factory=self.factory, timeout=self.timeout
        )

    def _take_idle(self, account: str) -> ImapSession | None:
        with self._guard:
            entry = self._idle.pop(account, None)
        if entry is None:
            return None
        session, parked_at = entry
        if self.clock() - parked_at > self.idle_ttl:
            session.close()
            return None
        try:
            session.noop()
            return session
        except MailError:
            session.close()
            return None

    def _park(self, account: str, session: ImapSession) -> None:
        if session.broken:
            session.close()
            return
        with self._guard:
            old = self._idle.pop(account, None)
            self._idle[account] = (session, self.clock())
        if old is not None and old[0] is not session:
            old[0].close()

    def expire_idle(self) -> None:
        now = self.clock()
        with self._guard:
            stale = [a for a, (_, t) in self._idle.items() if now - t > self.idle_ttl]
            sessions = [self._idle.pop(a)[0] for a in stale]
        for session in sessions:
            session.close()

    @contextmanager
    def session(self, account: str) -> Iterator[ImapSession]:
        """Borrow the account's session for the duration of one tool call."""
        self.expire_idle()
        with self._lock_for(account):
            session = self._take_idle(account) or self.connect(account)
            session.reset()
            try:
                yield session
            finally:
                self._park(account, session)

    @contextmanager
    def dedicated(self, account: str) -> Iterator[ImapSession]:
        """A private connection for long waits, so other tools are not blocked."""
        session = self.connect(account)
        try:
            yield session
        finally:
            session.close()

    def close_all(self) -> None:
        with self._guard:
            sessions = [s for s, _ in self._idle.values()]
            self._idle.clear()
        for session in sessions:
            session.close()
