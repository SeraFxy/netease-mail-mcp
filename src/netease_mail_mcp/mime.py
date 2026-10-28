"""MIME: decode messages defensively, and compose drafts, replies and forwards.

Chinese mail in the wild mixes GBK/GB2312/GB18030 bodies, RFC 2047 headers, RFC 2231
filenames, raw 8-bit headers and mislabelled charsets. Decoding tries the declared charset
(GB-family aliases map to gb18030, a superset), then utf-8, then gb18030, and finally
falls back to ``errors="replace"``. It never raises.
"""

from __future__ import annotations

import codecs
import email
import email.header
import email.utils
import html.parser
import mimetypes
import os
import re
import stat
import unicodedata
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email import policy
from email.message import EmailMessage, Message
from pathlib import Path
from typing import Any

from . import __version__
from .config import ENV_ATTACHMENT_ROOTS
from .errors import InvalidArgument, LimitError
from .safety import MAX_ATTACHMENTS, MAX_OUTGOING_BYTES, PREVIEW_TEXT_CHARS, sha256_hex

# --------------------------------------------------------------------------- decoding

_CHARSET_ALIASES = {
    "gb2312": "gb18030",
    "gbk": "gb18030",
    "x-gbk": "gb18030",
    "cp936": "gb18030",
    "gb_2312-80": "gb18030",
    "csgb2312": "gb18030",
    "euc-cn": "gb18030",
    "x-euc-cn": "gb18030",
    "chinese": "gb18030",
    "big5": "big5hkscs",
    "utf8": "utf-8",
    "unknown-8bit": "",
    "x-unknown": "",
    "unknown": "",
    "default": "",
}
_LATIN_LIKE = {"us-ascii", "ascii", "iso-8859-1", "latin-1", "latin1", "windows-1252", "cp1252"}


def _codec(charset: str | None) -> str | None:
    if not charset:
        return None
    name = str(charset).strip().strip('"').lower()
    name = _CHARSET_ALIASES.get(name, name)
    if not name:
        return None
    try:
        codecs.lookup(name)
    except LookupError:
        return None
    return name


def _candidate_codecs(codec: str | None) -> list[str]:
    candidates: list[str] = []
    if codec and codec not in _LATIN_LIKE:
        candidates.append(codec)
    for fallback in ("utf-8", "gb18030"):
        if fallback not in candidates:
            candidates.append(fallback)
    if codec and codec not in candidates:
        candidates.append(codec)
    return candidates


def decode_bytes(data: bytes, charset: str | None = None) -> str:
    codec = _codec(charset)
    for name in _candidate_codecs(codec):
        try:
            return data.decode(name)
        except (UnicodeDecodeError, LookupError):
            continue
    return data.decode(codec or "utf-8", errors="replace")


def decode_excerpt(data: bytes, charset: str | None, max_chars: int) -> tuple[str, bool]:
    """The first ``max_chars`` characters of ``data`` and whether more text follows.

    Truncation is judged in decoded characters, not bytes (a GBK character is 2 bytes, its
    UTF-8 form 3). Every supported encoding uses at most 4 bytes per character, so more than
    ``4 * max_chars`` bytes always means more than ``max_chars`` characters; below that the
    whole text is decoded and counted.
    """
    limit = max_chars * 4
    if len(data) <= limit:
        text = decode_bytes(data, charset)
        return text[:max_chars], len(text) > max_chars
    head = data[:limit]
    codec = _codec(charset)
    for name in _candidate_codecs(codec):
        for cut in range(4):  # the prefix may end inside a multi-byte character
            try:
                return head[: len(head) - cut].decode(name)[:max_chars], True
            except (UnicodeDecodeError, LookupError):
                continue
    return head.decode(codec or "utf-8", errors="replace")[:max_chars], True


def _recover_8bit(text: str) -> str:
    """Re-decode raw 8-bit header bytes that the parser smuggled in as surrogates."""
    if any("\udc80" <= ch <= "\udcff" for ch in text):
        return decode_bytes(text.encode("utf-8", "surrogateescape"))
    return text


_UNFOLD = re.compile(r"\r?\n(?=[ \t])")


def _collapse(text: str) -> str:
    return " ".join(text.split())


def decode_header_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        # Unfold first: decode_header() drops the folding whitespace that follows an
        # encoded-word, gluing it to the next plain word ("实测" + "2026" -> "实测2026").
        value = _UNFOLD.sub("", value)
    try:
        parts = email.header.decode_header(value)
    except Exception:
        return _collapse(_recover_8bit(str(value)))
    pieces: list[str] = []
    for chunk, charset in parts:
        if isinstance(chunk, bytes):
            pieces.append(decode_bytes(chunk, charset))
        else:
            pieces.append(_recover_8bit(chunk))
    return _collapse("".join(pieces))


def parse_addresses(*values: Any) -> list[tuple[str, str]]:
    raw = [str(v) for v in values if v]
    if not raw:
        return []
    pairs = email.utils.getaddresses([_recover_8bit(v) for v in raw])
    out: list[tuple[str, str]] = []
    for name, addr in pairs:
        addr = addr.strip()
        if "@" in addr:
            out.append((decode_header_value(name), addr.lower()))
    return out


def format_address(name: str, addr: str) -> str:
    if not name or name.lower() == addr.lower():
        return addr
    if re.search(r'[,;<>@"()\[\]:\\，；]', name):
        name = '"' + name.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return f"{name} <{addr}>"


def format_addresses(pairs: Iterable[tuple[str, str]]) -> str:
    return ", ".join(format_address(n, a) for n, a in pairs)


def parse_date(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = email.utils.parsedate_to_datetime(str(value))
    except (TypeError, ValueError, IndexError):
        return None
    if parsed is None:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def iso_date(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone().isoformat(timespec="minutes")


_MSGID_RE = re.compile(r"<[^<>\s]+>")


def message_ids(value: Any) -> list[str]:
    return _MSGID_RE.findall(str(value or ""))


# --------------------------------------------------------------------------- HTML → text


class _HTMLText(html.parser.HTMLParser):
    BLOCK = frozenset(
        [
            "p",
            "div",
            "tr",
            "table",
            "section",
            "article",
            "header",
            "footer",
            "nav",
            "aside",
            "blockquote",
            "pre",
            "ul",
            "ol",
            "dl",
            "dt",
            "dd",
            "h1",
            "h2",
            "h3",
            "h4",
            "h5",
            "h6",
            "hr",
            "center",
            "form",
        ]
    )
    SKIP = frozenset(["script", "style", "head", "title", "noscript", "template", "svg"])

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self.skip = 0
        self.link: tuple[str | None, int] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self.SKIP:
            self.skip += 1
            return
        if self.skip:
            return
        if tag == "br":
            self.out.append("\n")
        elif tag == "li":
            self.out.append("\n- ")
        elif tag in ("td", "th"):
            self.out.append(" ")
        elif tag in self.BLOCK:
            self.out.append("\n")
        elif tag == "img":
            alt = dict(attrs).get("alt")
            if alt:
                self.out.append(f"[{alt}]")
        if tag == "a":
            # Links do not nest: like browsers, a new <a> closes the open one. This also keeps
            # the link-text join below linear (each piece of text belongs to one link).
            self._close_link()
            self.link = (dict(attrs).get("href"), len(self.out))

    def _close_link(self) -> None:
        if self.link is None:
            return
        href, start = self.link
        self.link = None
        text = "".join(self.out[start:]).strip()
        if href and href.startswith(("http://", "https://")) and href not in text:
            self.out.append(f" ({href})")

    def handle_endtag(self, tag: str) -> None:
        if tag in self.SKIP:
            self.skip = max(0, self.skip - 1)
            return
        if self.skip:
            return
        if tag == "a":
            self._close_link()
        elif tag in self.BLOCK:
            self.out.append("\n")

    def handle_data(self, data: str) -> None:
        if not self.skip:
            self.out.append(data)


MAX_HTML_CHARS = 2_000_000  # larger HTML is cut before conversion (it runs under the lock)


def html_to_text(markup: str, limit: int = MAX_HTML_CHARS) -> str:
    truncated = len(markup) > limit
    if truncated:
        markup = markup[:limit]
    parser = _HTMLText()
    try:
        parser.feed(markup)
        parser.close()
    except Exception:  # malformed markup: keep what was parsed
        pass
    lines = [" ".join(line.split()) for line in "".join(parser.out).splitlines()]
    out: list[str] = []
    for line in lines:
        if line or (out and out[-1]):
            out.append(line)
    text = "\n".join(out).strip()
    if truncated:
        text += (
            f"\n\n[HTML 太长，只转换了前 {limit} 个字符 / HTML too long: only the first "
            f"{limit} characters were converted]"
        )
    return text


# --------------------------------------------------------------------------- parsing


@dataclass
class Attachment:
    index: int
    filename: str
    content_type: str
    size: int
    inline: bool
    content_id: str | None
    part: Message = field(repr=False, compare=False)

    def payload(self) -> bytes:
        if self.content_type == "message/rfc822":
            inner = self.part.get_payload()
            if isinstance(inner, list) and inner and isinstance(inner[0], Message):
                return inner[0].as_bytes(policy=policy.compat32)
        data = self.part.get_payload(decode=True)
        return data if isinstance(data, bytes) else b""

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "index": self.index,
            "filename": self.filename,
            "content_type": self.content_type,
            "size": self.size,
        }
        if self.inline:
            data["inline"] = True
        return data


@dataclass
class ParsedMessage:
    message: Message
    subject: str
    from_: list[tuple[str, str]]
    to: list[tuple[str, str]]
    cc: list[tuple[str, str]]
    bcc: list[tuple[str, str]]
    reply_to: list[tuple[str, str]]
    date: datetime | None
    message_id: str
    in_reply_to: str
    references: list[str]
    text: str
    html: str
    text_source: str  # "plain" | "html" | "none"
    attachments: list[Attachment]

    def header_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "from": format_addresses(self.from_),
            "to": format_addresses(self.to),
            "subject": self.subject,
            "date": iso_date(self.date),
            "message_id": self.message_id or None,
        }
        if self.cc:
            data["cc"] = format_addresses(self.cc)
        if self.bcc:
            data["bcc"] = format_addresses(self.bcc)
        if self.reply_to:
            data["reply_to"] = format_addresses(self.reply_to)
        if self.in_reply_to:
            data["in_reply_to"] = self.in_reply_to
        if self.references:
            data["references"] = self.references
        return data


def _filename(part: Message) -> str | None:
    try:
        name = part.get_filename()
    except Exception:
        name = None
    if not name:
        return None
    name = _recover_8bit(str(name))
    if "=?" in name:
        name = decode_header_value(name)
    return name.strip() or None


def _leaves(part: Message) -> Iterator[Message]:
    """Leaf parts, without descending into attached messages (they are attachments)."""
    if part.get_content_type() == "message/rfc822":
        yield part
        return
    if part.is_multipart():
        payload = part.get_payload()
        for sub in payload if isinstance(payload, list) else []:
            if isinstance(sub, Message):
                yield from _leaves(sub)
        return
    yield part


def part_text(part: Message) -> str:
    payload = part.get_payload(decode=True)
    data = payload if isinstance(payload, bytes) else b""
    try:
        charset = part.get_content_charset()
    except Exception:
        charset = None
    return decode_bytes(data, charset)


def _default_name(ctype: str, index: int) -> str:
    if ctype == "message/rfc822":
        return f"message-{index}.eml"
    ext = mimetypes.guess_extension(ctype) or ".bin"
    return f"attachment-{index}{ext}"


def parse_message(raw: bytes) -> ParsedMessage:
    msg = email.message_from_bytes(raw, policy=policy.compat32)
    plain: list[str] = []
    markup: list[str] = []
    attachments: list[Attachment] = []
    for part in _leaves(msg):
        ctype = part.get_content_type()
        disposition = part.get_content_disposition()
        filename = _filename(part)
        is_body = (
            ctype in ("text/plain", "text/html") and disposition != "attachment" and not filename
        )
        if is_body:
            (plain if ctype == "text/plain" else markup).append(part_text(part))
            continue
        index = len(attachments)
        if ctype == "message/rfc822":
            inner = part.get_payload()
            first = inner[0] if isinstance(inner, list) and inner else None
            size = len(first.as_bytes(policy=policy.compat32)) if isinstance(first, Message) else 0
            if not filename and isinstance(first, Message):
                subject = decode_header_value(first.get("Subject"))
                filename = (subject[:80] + ".eml") if subject else None
        else:
            payload = part.get_payload(decode=True)
            size = len(payload) if isinstance(payload, bytes) else 0
        content_id = (part.get("Content-ID") or "").strip() or None
        attachments.append(
            Attachment(
                index=index,
                filename=filename or _default_name(ctype, index),
                content_type=ctype,
                size=size,
                inline=disposition == "inline" or (disposition is None and bool(content_id)),
                content_id=content_id,
                part=part,
            )
        )
    text = "\n\n".join(t.strip() for t in plain if t.strip())
    html_body = "\n".join(markup)
    source = "plain"
    if not text and html_body.strip():
        text = html_to_text(html_body)
        source = "html"
    if not text:
        source = "none"
    return ParsedMessage(
        message=msg,
        subject=decode_header_value(msg.get("Subject")),
        from_=parse_addresses(msg.get("From")),
        to=parse_addresses(*msg.get_all("To", [])),
        cc=parse_addresses(*msg.get_all("Cc", [])),
        bcc=parse_addresses(*msg.get_all("Bcc", [])),
        reply_to=parse_addresses(msg.get("Reply-To")),
        date=parse_date(msg.get("Date")),
        message_id=(message_ids(msg.get("Message-ID")) or [""])[0],
        in_reply_to=(message_ids(msg.get("In-Reply-To")) or [""])[0],
        references=message_ids(msg.get("References")),
        text=text,
        html=html_body,
        text_source=source,
        attachments=attachments,
    )


def parse_headers(raw: bytes) -> ParsedMessage:
    """Parse a header block (from ``BODY.PEEK[HEADER.FIELDS ...]``)."""
    return parse_message(raw.rstrip(b"\r\n") + b"\r\n\r\n")


def snippet(text: str, limit: int = 200) -> str:
    flat = _collapse(text)
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


# --------------------------------------------------------------------------- BODYSTRUCTURE


def bodystructure_attachments(structure: Any) -> int:
    """Count attachment-like leaves in a parsed BODYSTRUCTURE list."""
    if not isinstance(structure, list) or not structure:
        return 0
    if isinstance(structure[0], list):  # multipart: (part)(part)... "subtype" [extensions]
        total = 0
        for child in structure:
            if not isinstance(child, list):
                break  # the subtype; extension lists (params, disposition) follow it
            total += bodystructure_attachments(child)
        return total

    def text(v: Any) -> str:
        if isinstance(v, bytes):
            return v.decode("utf-8", "replace")
        return str(v) if v is not None else ""

    maintype = text(structure[0]).lower()
    subtype = text(structure[1]).lower() if len(structure) > 1 else ""
    params = structure[2] if len(structure) > 2 and isinstance(structure[2], list) else []
    has_name = any(text(params[i]).lower() == "name" for i in range(0, len(params) - 1, 2))
    # Extension data: disposition follows md5 at a type-dependent index.
    if maintype == "text":
        ext_start = 8
    elif maintype == "message" and subtype == "rfc822":
        return 1
    else:
        ext_start = 7
    disposition = None
    if len(structure) > ext_start + 1 and isinstance(structure[ext_start + 1], list):
        disposition = structure[ext_start + 1]
    disp_type = text(disposition[0]).lower() if disposition else ""
    disp_params = disposition[1] if disposition and len(disposition) > 1 else None
    has_filename = isinstance(disp_params, list) and any(
        text(disp_params[i]).lower().startswith("filename")
        for i in range(0, len(disp_params) - 1, 2)
    )
    if disp_type == "attachment" or has_filename:
        return 1
    if maintype == "text" and subtype in ("plain", "html"):
        return 1 if has_name else 0
    if disp_type == "inline":
        return 0
    content_id = text(structure[3]) if len(structure) > 3 else ""
    if content_id and maintype == "image":
        return 0  # inline image referenced from HTML
    return 1


# --------------------------------------------------------------------------- filenames

_WINDOWS_RESERVED = {
    "con",
    "prn",
    "aux",
    "nul",
    *(f"com{i}" for i in range(1, 10)),
    *(f"lpt{i}" for i in range(1, 10)),
}


def safe_filename(name: str, default: str = "attachment") -> str:
    """Make an email-supplied filename safe to create on any OS."""
    name = unicodedata.normalize("NFC", name or "")
    name = name.replace("\\", "/").split("/")[-1]
    name = re.sub(r'[\x00-\x1f\x7f<>:"|?*]', "_", name)
    name = name.strip().strip(".").strip()
    if not name:
        name = default
    stem, dot, ext = name.rpartition(".")
    if not dot:
        stem, ext = name, ""
    if stem.lower() in _WINDOWS_RESERVED:
        stem = "_" + stem
    ext = ext[:20]
    max_stem = 150 - (len(ext) + 1 if ext else 0)
    stem = stem[:max_stem].rstrip() or "attachment"
    return f"{stem}.{ext}" if ext else stem


def numbered_names(filename: str, limit: int = 1000) -> Iterator[str]:
    """``a.txt``, ``a (1).txt``, ``a (2).txt``, ... at most ``limit`` names in total."""
    path = Path(filename)
    stem, ext = path.stem, path.suffix
    yield filename
    for counter in range(1, limit):
        yield f"{stem} ({counter}){ext}"


# --------------------------------------------------------------------------- composing

_REPLY_PREFIX = re.compile(r"^\s*((re|aw|回复|答复)(\s*\[\d+\])?\s*[:：]\s*)+", re.IGNORECASE)
_FWD_PREFIX = re.compile(r"^\s*((fwd?|转发)(\s*\[\d+\])?\s*[:：]\s*)+", re.IGNORECASE)
_ANY_PREFIX = re.compile(
    r"^\s*((re|aw|fwd?|回复|答复|转发)(\s*\[\d+\])?\s*[:：]\s*)+", re.IGNORECASE
)


def reply_subject(subject: str) -> str:
    return "Re: " + _REPLY_PREFIX.sub("", subject or "").strip()


def forward_subject(subject: str) -> str:
    return "Fwd: " + _FWD_PREFIX.sub("", subject or "").strip()


def thread_subject_key(subject: str) -> str:
    previous = None
    text = subject or ""
    while previous != text:
        previous = text
        text = _ANY_PREFIX.sub("", text)
    return _collapse(text).casefold()


def reply_threading(original: ParsedMessage) -> tuple[str | None, str | None]:
    refs = list(original.references)
    if not refs and original.in_reply_to:
        refs = [original.in_reply_to]
    if original.message_id and original.message_id not in refs:
        refs.append(original.message_id)
    refs = refs[-20:]
    return (original.message_id or None), (" ".join(refs) or None)


def reply_recipients(
    original: ParsedMessage, self_addr: str, reply_all: bool
) -> tuple[list[str], list[str]]:
    me = self_addr.lower()
    from_self = bool(original.from_) and all(a == me for _, a in original.from_)
    primary = original.to if from_self else (original.reply_to or original.from_)
    to: list[str] = []
    seen = {me}
    for name, addr in primary:
        if addr not in seen:
            to.append(format_address(name, addr))
            seen.add(addr)
    cc: list[str] = []
    if reply_all:
        for name, addr in [*original.to, *original.cc]:
            if addr not in seen:
                cc.append(format_address(name, addr))
                seen.add(addr)
    if not to and cc:
        to, cc = cc[:1], cc[1:]
    return to, cc


_ADDR_RE = re.compile(r"^[^@\s<>,;]+@[^@\s<>,;]+\.[^@\s<>,;]+$")


_SEPARATORS = frozenset(",;，；")


def split_address_list(text: str) -> list[str]:
    """Split on , ; ， ； outside quoted names, comments and <...>; drop empty pieces."""
    pieces: list[str] = []
    current: list[str] = []
    quoted = escaped = False
    angle = comment = 0
    for ch in text:
        if escaped:
            escaped = False
        elif ch == "\\" and (quoted or comment):
            escaped = True
        elif ch == '"' and not comment:
            quoted = not quoted
        elif not quoted:
            if ch == "(":
                comment += 1
            elif ch == ")" and comment:
                comment -= 1
            elif ch == "<" and not comment:
                angle += 1
            elif ch == ">" and angle:
                angle -= 1
            elif ch in _SEPARATORS and not (angle or comment):
                pieces.append("".join(current))
                current = []
                continue
        current.append(ch)
    pieces.append("".join(current))
    return [p.strip() for p in pieces if p.strip()]


def normalize_recipients(value: str | Sequence[str] | None, field_name: str) -> list[str]:
    """Parse 'a@x.com, "Zhang, San" <b@y.com>' or a list into formatted, validated addresses."""
    if value is None:
        return []
    items = [value] if isinstance(value, str) else list(value)
    out: list[str] = []
    for item in items:
        if not isinstance(item, str):
            raise InvalidArgument(f"{field_name} 格式不对", hint=f"{field_name} must be strings")
        if re.search(r"[\r\n\x00]", item):
            raise InvalidArgument(
                f"{field_name} 里不能有换行符", hint=f"{field_name} must not contain newlines"
            )
        for piece in split_address_list(item):
            pairs = email.utils.getaddresses([piece])
            name, addr = pairs[0] if len(pairs) == 1 else ("", "")
            addr = addr.strip()
            if not _ADDR_RE.match(addr):
                raise InvalidArgument(
                    f"{field_name} 里的地址无效：{piece!r}（名字里有逗号时请加英文引号，"
                    '如 "Zhang, San" <z@x.com>）',
                    hint=f"Invalid address in {field_name}: {piece!r} (quote display names "
                    'that contain commas: "Zhang, San" <z@x.com>)',
                )
            out.append(format_address(name.strip(), addr.lower()))
    return out


def bare_address(formatted: str) -> str:
    return email.utils.parseaddr(formatted)[1].lower()


@dataclass
class OutgoingAttachment:
    filename: str
    content_type: str
    data: bytes
    path: str | None = None

    @property
    def size(self) -> int:
        return len(self.data)

    @property
    def sha256(self) -> str:
        return sha256_hex(self.data)


_SENSITIVE_DIRS = {".ssh", ".gnupg", ".aws", ".kube", ".docker", ".password-store", "keychains"}
_SYSTEM_DIRS = ("/proc", "/sys", "/dev", "/etc")
# iCloud Drive; Dropbox / OneDrive / Google Drive: ordinary documents synced into ~/Library.
_LIBRARY_DOCUMENT_DIRS = {"mobile documents", "cloudstorage"}


def parts_below(path: str | Path, root: str | Path) -> tuple[str, ...] | None:
    """The components of ``path`` below ``root`` (``()`` for root itself), or None if outside.

    Both should be symlink-resolved. Besides comparing the strings (case-insensitively on
    Windows), each ancestor is compared with ``root`` by device and inode, because macOS and
    Windows disks ignore letter case: /USERS/me/.zshrc is ~/.zshrc.
    """
    p, r = os.path.normcase(str(path)), os.path.normcase(str(root))
    try:
        if os.path.commonpath([p, r]) == r:
            return Path(os.path.relpath(p, r)).parts
    except ValueError:  # different drives, or relative and absolute
        pass
    try:
        root_stat = os.stat(root)
    except OSError:
        return None
    if not root_stat.st_ino:  # inode numbers are only meaningful when non-zero
        return None
    candidate = Path(path)
    for ancestor in (candidate, *candidate.parents):
        try:
            if os.path.samestat(os.stat(ancestor), root_stat):
                return candidate.parts[len(ancestor.parts) :]
        except OSError:
            continue
    return None


def path_inside(path: str | Path, root: str | Path) -> bool:
    """``path`` is ``root`` or below it (see :func:`parts_below`)."""
    return parts_below(path, root) is not None


def _is_unc(text: str) -> bool:
    return text.startswith(("\\\\", "//"))


def attachment_denied(
    resolved: Path,
    environ: Mapping[str, str] | None = None,
    roots: Sequence[str] = (),
) -> str | None:
    """Why a (symlink-resolved) path must never be attached, or None.

    Always refused, whatever NETEASE_MAIL_ATTACHMENT_ROOTS says: network (UNC) paths, any
    file or folder whose name starts with "." below the home directory (.ssh, .env, .git,
    .config ...), %APPDATA% / %LOCALAPPDATA%, /proc /sys /dev /etc, and folders named like
    credential stores anywhere. ~/Library is refused too, except iCloud Drive and cloud-drive
    folders (Mobile Documents, CloudStorage) and folders strictly inside it that ``roots``
    (resolved NETEASE_MAIL_ATTACHMENT_ROOTS entries) names explicitly; ~/Library itself does
    not count.
    """
    env = os.environ if environ is None else environ
    text = str(resolved)
    if _is_unc(text):
        return "网络路径 network (UNC) path"
    home = os.path.realpath(Path.home())
    below_home = parts_below(text, home)
    if below_home is not None:
        if any(part.startswith(".") for part in below_home):
            return "主目录下的隐藏文件或目录 hidden file or folder in your home directory"
        library = os.path.join(home, "Library")
        below_library = parts_below(text, library)
        if (
            below_library is not None
            and not (below_library and below_library[0].casefold() in _LIBRARY_DOCUMENT_DIRS)
            and not any(path_inside(text, r) and parts_below(r, library) for r in roots)
        ):
            return "~/Library"
    for var in ("APPDATA", "LOCALAPPDATA"):
        if env.get(var) and path_inside(text, os.path.realpath(env[var])):
            return f"%{var}%"
    if os.name != "nt":
        for system_dir in _SYSTEM_DIRS:
            if path_inside(text, os.path.realpath(system_dir)):
                return system_dir
    if any(part.lower() in _SENSITIVE_DIRS for part in resolved.parts):
        return "凭据目录 credential directory"
    return None


def _refuse(path_text: str, reason: str, roots: Sequence[Path]) -> InvalidArgument:
    if roots:
        allowed = os.pathsep.join(str(r) for r in roots)
        how = (
            f"只允许附加 {ENV_ATTACHMENT_ROOTS} 里的目录（{allowed}）中的普通文件；"
            "请把文件复制到其中一个目录，或把它所在的目录加进这个环境变量。"
        )
        how_en = (
            f"Only regular files inside {ENV_ATTACHMENT_ROOTS} ({allowed}) can be attached: copy "
            "the file into one of them, or add its folder to that variable."
        )
    else:
        how = "如确需发送，请先把文件复制到普通目录（例如 文稿/Documents）再附加。"
        how_en = "If you really mean to send it, copy it to an ordinary folder first."
    return InvalidArgument(
        f"出于安全考虑，不能附加这个文件：{path_text}（{reason}）。{how}",
        hint=f"Refusing to attach {path_text} ({reason}). {how_en}",
    )


def load_attachment(
    path_text: str,
    *,
    roots: Sequence[Path] = (),
    environ: Mapping[str, str] | None = None,
) -> OutgoingAttachment:
    """Read a local file to attach, enforcing the attachment path policy.

    ``roots`` (NETEASE_MAIL_ATTACHMENT_ROOTS) optionally limits attachments to files inside
    those folders; :func:`attachment_denied` applies in every case. Symlinks are resolved
    before checking, and the file is read from a descriptor that must be a regular file
    whose size matches what was read.
    """
    text = str(path_text)
    path = Path(text).expanduser()
    if _is_unc(text):
        raise _refuse(text, "网络路径 network (UNC) path", roots)
    if not path.is_absolute():
        raise InvalidArgument(
            f"附件路径必须是绝对路径：{path_text}",
            hint=f"Attachment path must be absolute: {path_text}",
        )
    try:
        resolved = path.resolve(strict=True)
    except (OSError, RuntimeError):
        raise InvalidArgument(
            f"附件不存在或无法访问：{path_text}", hint=f"Attachment not found: {path_text}"
        ) from None
    real_roots = [os.path.realpath(r) for r in roots]
    reason = attachment_denied(resolved, environ, real_roots)
    if reason:
        raise _refuse(text, reason, roots)
    if roots and not any(path_inside(resolved, r) for r in real_roots):
        raise _refuse(
            text, f"不在 {ENV_ATTACHMENT_ROOTS} 里 not inside {ENV_ATTACHMENT_ROOTS}", roots
        )
    flags = (
        os.O_RDONLY
        | getattr(os, "O_BINARY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    try:
        fd = os.open(resolved, flags)
    except OSError:
        raise InvalidArgument(
            f"附件无法读取：{path_text}", hint=f"Attachment is not readable: {path_text}"
        ) from None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise InvalidArgument(
                f"附件必须是普通文件：{path_text}",
                hint=f"Attachment must be a regular file: {path_text}",
            )
        if info.st_size > MAX_OUTGOING_BYTES:
            raise LimitError(
                f"附件 {path.name} 超过 20 MB 上限。", hint="Attachment exceeds the 20 MB limit."
            )
        with os.fdopen(fd, "rb") as fh:
            fd = -1  # owned by fh now
            data = fh.read(MAX_OUTGOING_BYTES + 1)
    except OSError:
        raise InvalidArgument(
            f"附件无法读取：{path_text}", hint=f"Attachment is not readable: {path_text}"
        ) from None
    finally:
        if fd >= 0:
            os.close(fd)
    if len(data) != info.st_size:
        raise InvalidArgument(
            f"附件 {path_text} 读到的内容和文件大小不一致（特殊文件或正在被修改），已拒绝。",
            hint="The file's content does not match its size (a special file, or it is "
            "being modified); refusing to attach it.",
        )
    ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    return OutgoingAttachment(filename=path.name, content_type=ctype, data=data, path=str(resolved))


@dataclass
class Composed:
    message: EmailMessage
    from_addr: str
    to: list[str]
    cc: list[str]
    bcc: list[str]
    subject: str
    body: str
    html_body: str | None
    attachments: list[OutgoingAttachment]
    in_reply_to: str | None
    references: str | None

    @property
    def envelope_recipients(self) -> list[str]:
        seen: list[str] = []
        for addr in (bare_address(a) for a in [*self.to, *self.cc, *self.bcc]):
            if addr and addr not in seen:
                seen.append(addr)
        return seen

    def digest_payload(self) -> dict[str, Any]:
        return {
            "from": self.from_addr,
            "to": self.to,
            "cc": self.cc,
            "bcc": self.bcc,
            "subject": self.subject,
            "body_sha256": sha256_hex(self.body),
            "html_sha256": sha256_hex(self.html_body) if self.html_body else None,
            "attachments": [[a.filename, a.size, a.sha256] for a in self.attachments],
            "in_reply_to": self.in_reply_to,
            "references": self.references,
        }

    def preview(self, cap: int = PREVIEW_TEXT_CHARS) -> dict[str, Any]:
        """Everything the recipients will get, for a human to check before sending."""
        data: dict[str, Any] = {
            "from": self.from_addr,
            "to": self.to,
            "cc": self.cc,
            "bcc": self.bcc,
            "subject": self.subject,
            **text_preview("body", self.body, cap),
        }
        if self.html_body:
            data.update(html_preview(self.html_body, self.body, cap))
        data["attachments"] = [
            {
                "filename": a.filename,
                "size": a.size,
                "content_type": a.content_type,
                "path": a.path,
                "source": "file" if a.path else "original_message",
            }
            for a in self.attachments
        ]
        if self.in_reply_to:
            data["in_reply_to"] = self.in_reply_to
        if self.references:
            data["references"] = self.references
        return data


def text_preview(key: str, text: str, cap: int) -> dict[str, Any]:
    """``{key: text[:cap], key_chars: n, key_omitted_chars: n - cap}``."""
    return {
        key: text[:cap],
        f"{key}_chars": len(text),
        f"{key}_omitted_chars": max(0, len(text) - cap),
    }


_WORDS = re.compile(r"https?://[^\s()<>\[\]\"']+|\w+")


def _words(text: str) -> set[str]:
    return set(_WORDS.findall(unicodedata.normalize("NFKC", text).casefold()))


def html_differs(plain: str, html_text: str) -> bool:
    """True when the HTML part says something the plain part does not, or the other way round.

    Whitespace, case and markup are ignored; words and URLs are compared.
    """
    return _words(plain) != _words(html_text)


def html_preview(markup: str, plain: str, cap: int) -> dict[str, Any]:
    text = html_to_text(markup)
    return {
        **text_preview("html_text", text, cap),
        "html_chars": len(markup),
        "html_differs_from_body": html_differs(plain, text),
    }


_COMPOSE_POLICY = policy.SMTP.clone(cte_type="7bit")


def compose(
    *,
    from_addr: str,
    to: Sequence[str],
    cc: Sequence[str] = (),
    bcc: Sequence[str] = (),
    subject: str,
    body: str,
    html_body: str | None = None,
    attachments: Sequence[OutgoingAttachment] = (),
    attached_messages: Sequence[tuple[str, bytes]] = (),
    in_reply_to: str | None = None,
    references: str | None = None,
) -> Composed:
    if re.search(r"[\r\n]", subject or ""):
        raise InvalidArgument("主题里不能有换行符", hint="Subject must not contain newlines")
    if not (to or cc or bcc):
        raise InvalidArgument("至少需要一个收件人（to）", hint="At least one recipient is required")
    if len(attachments) + len(attached_messages) > MAX_ATTACHMENTS:
        raise LimitError(
            f"附件最多 {MAX_ATTACHMENTS} 个。", hint=f"At most {MAX_ATTACHMENTS} attachments."
        )
    total = (
        len(body.encode("utf-8"))
        + len((html_body or "").encode("utf-8"))
        + sum(a.size for a in attachments)
        + sum(len(raw) for _, raw in attached_messages)
    )
    if total > MAX_OUTGOING_BYTES:
        raise LimitError(
            f"邮件总大小 {total / 1048576:.1f} MB 超过 20 MB 上限。",
            hint="The message exceeds the 20 MB limit.",
        )
    msg = EmailMessage(policy=_COMPOSE_POLICY)
    try:
        msg["From"] = from_addr
        if to:
            msg["To"] = ", ".join(to)
        if cc:
            msg["Cc"] = ", ".join(cc)
        if bcc:
            msg["Bcc"] = ", ".join(bcc)
        msg["Subject"] = subject
        msg["Date"] = email.utils.formatdate(localtime=True)
        msg["Message-ID"] = email.utils.make_msgid(domain=from_addr.rsplit("@", 1)[-1])
        if in_reply_to:
            msg["In-Reply-To"] = in_reply_to
        if references:
            msg["References"] = references
        msg["X-Mailer"] = f"netease-mail-mcp {__version__}"
    except (ValueError, TypeError) as exc:
        raise InvalidArgument(f"邮件头无效：{exc}", hint=f"Invalid header: {exc}") from None
    msg.set_content(body or "", charset="utf-8")
    if html_body:
        msg.add_alternative(html_body, subtype="html", charset="utf-8")
    for att in attachments:
        maintype, _, subtype = att.content_type.partition("/")
        msg.add_attachment(
            att.data,
            maintype=maintype or "application",
            subtype=subtype or "octet-stream",
            filename=att.filename,
        )
    for name, raw in attached_messages:
        msg.add_attachment(raw, maintype="application", subtype="octet-stream", filename=name)
    return Composed(
        message=msg,
        from_addr=from_addr,
        to=list(to),
        cc=list(cc),
        bcc=list(bcc),
        subject=subject,
        body=body or "",
        html_body=html_body,
        attachments=[*attachments]
        + [OutgoingAttachment(n, "application/octet-stream", raw) for n, raw in attached_messages],
        in_reply_to=in_reply_to,
        references=references,
    )


def quote_original(original: ParsedMessage, limit: int = 20_000) -> str:
    when = iso_date(original.date) or "?"
    who = format_addresses(original.from_) or "?"
    text = original.text if len(original.text) <= limit else original.text[:limit] + "\n…"
    quoted = "\n".join("> " + line if line else ">" for line in text.splitlines())
    return f"在 {when}，{who} 写道：\n{quoted}"


def forward_block(original: ParsedMessage, limit: int = 50_000) -> str:
    text = original.text if len(original.text) <= limit else original.text[:limit] + "\n…"
    lines = [
        "---------- 转发的邮件 / Forwarded message ----------",
        f"发件人 From: {format_addresses(original.from_)}",
        f"日期 Date: {iso_date(original.date) or ''}",
        f"主题 Subject: {original.subject}",
        f"收件人 To: {format_addresses(original.to)}",
    ]
    if original.cc:
        lines.append(f"抄送 Cc: {format_addresses(original.cc)}")
    return "\n".join(lines) + "\n\n" + text


def serialize(message: EmailMessage) -> bytes:
    return message.as_bytes(policy=_COMPOSE_POLICY)
