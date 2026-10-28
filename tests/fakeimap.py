"""A small in-memory IMAP server that speaks real IMAP over a socketpair.

Clients are genuine :class:`imaplib.IMAP4` objects, so response parsing (literals, quoting,
untagged responses) is exercised exactly as against a real server. Behaviour mimics NetEase:

* SELECT/EXAMINE fail with ``NO SELECT Unsafe Login`` unless the client sent ``ID``.
* SEARCH FROM / TO / SUBJECT / TEXT / BODY / HEADER silently return nothing.
* Folder names are modified UTF-7 with special-use flags; MOVE/IDLE are optional.
"""

from __future__ import annotations

import email
import imaplib
import re
import select
import socket
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email import policy
from email.message import Message
from typing import Any

MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]

# Raw (modified UTF-7) names as NetEase sends them.
INBOX = "INBOX"
DRAFTS = "&g0l6P3ux-"  # 草稿箱
SENT = "&XfJT0ZAB-"  # 已发送
TRASH = "&XfJSIJZk-"  # 已删除
JUNK = "&V4NXPpCuTvY-"  # 垃圾邮件
VIRUS = "&dcVr0mWHTvZZOQ-"  # 病毒文件夹
ADS = "&Xn9USpCuTvY-"  # 广告邮件


@dataclass
class FakeMessage:
    uid: int
    raw: bytes
    flags: set[str]
    internaldate: datetime


@dataclass
class FakeMailbox:
    name: str
    special: tuple[str, ...] = ()
    uidvalidity: int = 1000
    uidnext: int = 1
    messages: list[FakeMessage] = field(default_factory=list)
    noselect: bool = False

    def add(self, raw: bytes, flags: tuple[str, ...] = (), when: datetime | None = None) -> int:
        uid = self.uidnext
        self.uidnext += 1
        self.messages.append(FakeMessage(uid, raw, set(flags), when or datetime.now(timezone.utc)))
        return uid

    def by_uid(self, uid: int) -> FakeMessage | None:
        return next((m for m in self.messages if m.uid == uid), None)


def _quote(value: str) -> bytes:
    return b'"' + value.replace("\\", "\\\\").replace('"', '\\"').encode() + b'"'


def _string(value: str | bytes | None, force_literal: bool = False) -> bytes:
    if value is None:
        return b"NIL"
    data = value if isinstance(value, bytes) else value.encode("utf-8")
    if force_literal or not data.isascii() or b"\r" in data or b"\n" in data:
        return b"{%d}\r\n" % len(data) + data
    return _quote(data.decode("ascii"))


def _tokenize(data: bytes) -> list[Any]:
    """Client command tokenizer: atoms (bracket-aware), quoted, literals (bytes), lists."""
    pos = 0

    def parse_list(end: int) -> tuple[list[Any], int]:
        nonlocal pos
        items: list[Any] = []
        while pos < len(data):
            c = data[pos]
            if c == 0x20:
                pos += 1
            elif c == end:
                pos += 1
                return items, pos
            elif c == 0x28:
                pos += 1
                sub, _ = parse_list(0x29)
                items.append(sub)
            elif c == 0x22:
                pos += 1
                out = bytearray()
                while data[pos] != 0x22:
                    if data[pos] == 0x5C:
                        pos += 1
                    out.append(data[pos])
                    pos += 1
                pos += 1
                items.append(out.decode("utf-8"))
            elif c == 0x00:
                close = data.index(b"\x00", pos + 1)
                items.append(literals[int(data[pos + 1 : close])])
                pos = close + 1
            else:
                start = pos
                depth = 0
                while pos < len(data):
                    ch = data[pos]
                    if ch == 0x5B:
                        depth += 1
                    elif ch == 0x5D:
                        depth -= 1
                    elif depth == 0 and ch in b" ()":
                        break
                    pos += 1
                items.append(data[start:pos].decode("utf-8"))
        return items, pos

    literals: list[bytes] = _tokenize.literals  # type: ignore[attr-defined]
    items, _ = parse_list(-1)
    return items


class FakeImapServer:
    def __init__(
        self,
        user: str = "me@163.com",
        password: str = "AUTHCODEABCDEFGH",
        *,
        capabilities: tuple[str, ...] = ("IMAP4rev1", "ID", "UIDPLUS", "SPECIAL-USE", "LITERAL+"),
        post_login_extra: tuple[str, ...] = (),
        require_id: bool = True,
        literal_names: bool = False,
        flags_after_literal: bool = False,
    ) -> None:
        self.user = user
        self.password = password
        self.capabilities = capabilities
        self.post_login_extra = post_login_extra
        self.require_id = require_id
        self.literal_names = literal_names
        self.flags_after_literal = flags_after_literal
        self.lock = threading.RLock()
        self.changed = threading.Condition(self.lock)
        self.log: list[str] = []
        self.connections = 0
        self.handlers: list[_Connection] = []
        self.mailboxes: dict[str, FakeMailbox] = {}
        for name, special, validity in (
            (INBOX, (), 1001),
            (DRAFTS, ("\\Drafts",), 1002),
            (SENT, ("\\Sent",), 1003),
            (TRASH, ("\\Trash",), 1004),
            (JUNK, ("\\Junk",), 1005),
            (VIRUS, (), 1006),
            (ADS, (), 1007),
        ):
            self.mailboxes[name] = FakeMailbox(name, special, validity)

    # -- helpers for tests -------------------------------------------------------------

    def mailbox(self, name: str) -> FakeMailbox:
        return self.mailboxes[name]

    def deliver(
        self, name: str, raw: bytes, flags: tuple[str, ...] = (), when: datetime | None = None
    ) -> int:
        with self.changed:
            uid = self.mailboxes[name].add(raw, flags, when)
            self.changed.notify_all()
            return uid

    def commands(self, verb: str | None = None) -> list[str]:
        with self.lock:
            return [c for c in self.log if verb is None or c.split(" ", 1)[0] == verb]

    def connect(self, host: str, port: int, timeout: float) -> imaplib.IMAP4:
        client_sock, server_sock = socket.socketpair()
        handler = _Connection(self, server_sock)
        handler.start()
        with self.lock:
            self.connections += 1
            self.handlers.append(handler)
        return _FakeIMAP4(client_sock, timeout)

    def drop_connections(self) -> None:
        """Simulate the server hanging up on every open connection."""
        with self.lock:
            handlers, self.handlers = self.handlers, []
        for handler in handlers:
            try:
                handler.sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    def add_mailbox(self, name: str, uidvalidity: int = 2000) -> FakeMailbox:
        with self.lock:
            box = self.mailboxes[name] = FakeMailbox(name, (), uidvalidity)
            return box


class _FakeIMAP4(imaplib.IMAP4):
    def __init__(self, sock: socket.socket, timeout: float | None) -> None:
        self._fake_sock = sock
        super().__init__("fake.imap", 993, timeout)

    def _create_socket(self, timeout: float | None = None) -> socket.socket:
        if timeout is not None:
            self._fake_sock.settimeout(timeout)
        return self._fake_sock


class _Connection(threading.Thread):
    def __init__(self, server: FakeImapServer, sock: socket.socket) -> None:
        super().__init__(daemon=True)
        self.srv = server
        self.sock = sock
        self.rfile = sock.makefile("rb")
        self.authed = False
        self.id_sent = False
        self.selected: FakeMailbox | None = None
        self.readonly = True
        self.known: set[int] = set()

    # -- io ----------------------------------------------------------------------------

    def send(self, data: bytes) -> None:
        self.sock.sendall(data + b"\r\n")

    def read_command(self) -> tuple[str, str, list[Any], str] | None:
        line = self.rfile.readline()
        if not line:
            return None
        literals: list[bytes] = []
        buf = b""
        while True:
            line = line.rstrip(b"\r\n")
            match = re.search(rb"\{(\d+)(\+?)\}$", line)
            if not match:
                buf += line
                break
            if not match.group(2):
                self.send(b"+ go ahead")
            data = self.rfile.read(int(match.group(1)))
            buf += line[: match.start()] + b"\x00%d\x00" % len(literals)
            literals.append(data)
            line = self.rfile.readline()
        _tokenize.literals = literals  # type: ignore[attr-defined]
        tokens = _tokenize(buf)
        tag = str(tokens[0])
        command = str(tokens[1]).upper() if len(tokens) > 1 else ""
        text = buf.decode("utf-8", "replace")
        return tag, command, tokens[2:], text

    # -- main loop ---------------------------------------------------------------------

    def caps(self) -> str:
        extra = self.srv.post_login_extra if self.authed else ()
        return " ".join((*self.srv.capabilities, *extra))

    def run(self) -> None:
        try:
            self.send(f"* OK [CAPABILITY {self.caps()}] fake NetEase IMAP ready".encode())
            while True:
                parsed = self.read_command()
                if parsed is None:
                    return
                tag, command, args, text = parsed
                with self.srv.lock:
                    if command == "LOGIN":
                        self.srv.log.append("LOGIN ***")
                    else:
                        self.srv.log.append(" ".join(text.split(" ")[1:]))
                handler = getattr(self, "cmd_" + command.lower(), None)
                if command == "UID":
                    sub = str(args[0]).upper()
                    handler = getattr(self, "uid_" + sub.lower(), None)
                    args = args[1:]
                if handler is None:
                    self.send(f"{tag} BAD unknown command {command}".encode())
                    continue
                if handler(tag, args) == "BYE":
                    return
        except OSError:
            return
        except Exception:  # surface fake-server bugs instead of a bare broken pipe
            import traceback

            traceback.print_exc()
            raise
        finally:
            try:
                self.sock.close()
            except OSError:
                pass

    # -- commands ----------------------------------------------------------------------

    def cmd_capability(self, tag: str, args: list[Any]) -> None:
        self.send(f"* CAPABILITY {self.caps()}".encode())
        self.send(f"{tag} OK CAPABILITY completed".encode())

    def cmd_login(self, tag: str, args: list[Any]) -> None:
        user, password = str(args[0]), str(args[1])
        if user.lower() == self.srv.user and password == self.srv.password:
            self.authed = True
            self.send(f"{tag} OK LOGIN completed".encode())
        else:
            self.send(f"{tag} NO LOGIN Login error user name or password error".encode())

    def cmd_id(self, tag: str, args: list[Any]) -> None:
        self.id_sent = True
        self.send(b'* ID ("name" "Coremail Imap" "vendor" "Mailtech")')
        self.send(f"{tag} OK ID completed".encode())

    def cmd_logout(self, tag: str, args: list[Any]) -> str:
        self.send(b"* BYE bye")
        self.send(f"{tag} OK LOGOUT completed".encode())
        return "BYE"

    def cmd_noop(self, tag: str, args: list[Any]) -> None:
        self._exists_update()
        self.send(f"{tag} OK NOOP completed".encode())

    def _exists_update(self) -> None:
        if self.selected is not None:
            with self.srv.lock:
                uids = {m.uid for m in self.selected.messages}
            if uids - self.known:
                self.known = uids
                self.send(f"* {len(uids)} EXISTS".encode())

    def cmd_list(self, tag: str, args: list[Any]) -> None:
        for box in self.srv.mailboxes.values():
            flags = ["\\HasNoChildren", *box.special] + (["\\Noselect"] if box.noselect else [])
            head = f'* LIST ({" ".join(flags)}) "/" '.encode()
            if self.srv.literal_names:
                self.send(head + _string(box.name, force_literal=True))
            else:
                self.send(head + _quote(box.name))
        self.send(f"{tag} OK LIST completed".encode())

    def _box(self, tag: str, name: Any) -> FakeMailbox | None:
        box = self.srv.mailboxes.get(str(name)) or (
            self.srv.mailboxes.get("INBOX") if str(name).upper() == "INBOX" else None
        )
        if box is None:
            self.send(f"{tag} NO Mailbox does not exist".encode())
        return box

    def cmd_status(self, tag: str, args: list[Any]) -> None:
        box = self._box(tag, args[0])
        if box is None:
            return
        unseen = sum(1 for m in box.messages if "\\Seen" not in m.flags)
        items = (
            f"MESSAGES {len(box.messages)} UNSEEN {unseen} UIDNEXT {box.uidnext} "
            f"UIDVALIDITY {box.uidvalidity}"
        )
        self.send(b"* STATUS " + _quote(box.name) + f" ({items})".encode())
        self.send(f"{tag} OK STATUS completed".encode())

    def _open(self, tag: str, args: list[Any], readonly: bool) -> None:
        verb = "EXAMINE" if readonly else "SELECT"
        if self.srv.require_id and not self.id_sent:
            self.send(
                f"{tag} NO {verb} Unsafe Login. Please contact kefu@188.com for help".encode()
            )
            return
        box = self._box(tag, args[0])
        if box is None:
            return
        if box.noselect:
            self.send(f"{tag} NO cannot select".encode())
            return
        self.selected, self.readonly = box, readonly
        with self.srv.lock:
            self.known = {m.uid for m in box.messages}
            count = len(box.messages)
        self.send(f"* {count} EXISTS".encode())
        self.send(b"* 0 RECENT")
        self.send(f"* OK [UIDVALIDITY {box.uidvalidity}] UIDs valid".encode())
        self.send(f"* OK [UIDNEXT {box.uidnext}] Predicted next UID".encode())
        self.send(b"* FLAGS (\\Answered \\Seen \\Deleted \\Draft \\Flagged)")
        mode = "READ-ONLY" if readonly else "READ-WRITE"
        self.send(f"{tag} OK [{mode}] {verb} completed".encode())

    def cmd_select(self, tag: str, args: list[Any]) -> None:
        self._open(tag, args, readonly=False)

    def cmd_examine(self, tag: str, args: list[Any]) -> None:
        self._open(tag, args, readonly=True)

    def cmd_close(self, tag: str, args: list[Any]) -> None:
        self.selected = None
        self.send(f"{tag} OK CLOSE completed".encode())

    cmd_unselect = cmd_close

    def cmd_append(self, tag: str, args: list[Any]) -> None:
        box = self._box(tag, args[0])
        if box is None:
            return
        flags: tuple[str, ...] = ()
        raw = args[-1]
        for arg in args[1:-1]:
            if isinstance(arg, list):
                flags = tuple(str(f) for f in arg)
        with self.srv.changed:
            uid = box.add(bytes(raw), flags)
            self.srv.changed.notify_all()
        self.send(f"{tag} OK [APPENDUID {box.uidvalidity} {uid}] APPEND completed".encode())

    def cmd_idle(self, tag: str, args: list[Any]) -> None:
        if "IDLE" not in self.caps().split():
            self.send(f"{tag} BAD IDLE not supported".encode())
            return
        self.send(b"+ idling")
        # Poll with select(): a timeout on a makefile() reader would break it for good.
        while True:
            self._exists_update()
            ready, _, _ = select.select([self.sock], [], [], 0.05)
            if not ready:
                continue
            line = self.rfile.readline()
            if not line or line.strip().upper() == b"DONE":
                break
        self.send(f"{tag} OK IDLE terminated".encode())

    def cmd_expunge(self, tag: str, args: list[Any]) -> None:
        self._expunge(None)
        self.send(f"{tag} OK EXPUNGE completed".encode())

    # -- UID commands ------------------------------------------------------------------

    def _uids(self, spec: str) -> list[int]:
        assert self.selected is not None
        existing = sorted(m.uid for m in self.selected.messages)
        top = existing[-1] if existing else 0
        wanted: set[int] = set()
        for part in spec.split(","):
            if ":" in part:
                a, b = part.split(":", 1)
                lo = top if a == "*" else int(a)
                hi = top if b == "*" else int(b)
                lo, hi = min(lo, hi), max(lo, hi)
                wanted.update(u for u in existing if lo <= u <= hi)
            else:
                value = top if part == "*" else int(part)
                if value in existing:
                    wanted.add(value)
        return sorted(wanted)

    def _require_selected(self, tag: str) -> bool:
        if self.selected is None:
            self.send(f"{tag} BAD no mailbox selected".encode())
            return False
        return True

    def uid_search(self, tag: str, args: list[Any]) -> None:
        if not self._require_selected(tag):
            return
        assert self.selected is not None
        result = [m for m in self.selected.messages]
        i = 0
        items = [str(a) if not isinstance(a, bytes) else a.decode() for a in args]
        while i < len(items):
            key = items[i].upper()
            i += 1
            if key == "ALL":
                continue
            if key in ("SEEN", "UNSEEN", "FLAGGED", "UNFLAGGED", "DELETED", "DRAFT"):
                flag = {
                    "SEEN": "\\Seen",
                    "UNSEEN": "\\Seen",
                    "FLAGGED": "\\Flagged",
                    "UNFLAGGED": "\\Flagged",
                    "DELETED": "\\Deleted",
                    "DRAFT": "\\Draft",
                }[key]
                want = not key.startswith("UN")
                result = [m for m in result if (flag in m.flags) == want]
            elif key in ("SINCE", "BEFORE", "ON"):
                day, mon, year = items[i].split("-")
                i += 1
                when = datetime(int(year), MONTHS.index(mon) + 1, int(day), tzinfo=timezone.utc)
                if key == "SINCE":
                    result = [m for m in result if m.internaldate >= when]
                elif key == "BEFORE":
                    result = [m for m in result if m.internaldate < when]
                else:
                    result = [m for m in result if m.internaldate.date() == when.date()]
            elif key == "UID":
                uids = set(self._uids(items[i]))
                i += 1
                result = [m for m in result if m.uid in uids]
            elif key in ("FROM", "TO", "CC", "BCC", "SUBJECT", "TEXT", "BODY"):
                i += 1
                result = []  # NetEase: silently empty
            elif key == "HEADER":
                i += 2
                result = []
            else:
                self.send(f"{tag} BAD unsupported search key {key}".encode())
                return
        self.send(("* SEARCH " + " ".join(str(m.uid) for m in result)).rstrip().encode())
        self.send(f"{tag} OK SEARCH completed".encode())

    def uid_fetch(self, tag: str, args: list[Any]) -> None:
        if not self._require_selected(tag):
            return
        assert self.selected is not None
        uids = self._uids(str(args[0]))
        spec = args[1] if isinstance(args[1], list) else [args[1]]
        wanted = [str(s) for s in spec]
        seqs = {m.uid: i + 1 for i, m in enumerate(self.selected.messages)}
        for uid in uids:
            msg = self.selected.by_uid(uid)
            assert msg is not None
            parts: list[bytes] = [b"UID %d" % uid]
            tail: list[bytes] = []
            for item in wanted:
                upper = item.upper()
                if upper == "UID":
                    continue
                if upper == "FLAGS":
                    flags = b"FLAGS (" + " ".join(sorted(msg.flags)).encode() + b")"
                    (tail if self.srv.flags_after_literal else parts).append(flags)
                elif upper == "RFC822.SIZE":
                    parts.append(b"RFC822.SIZE %d" % len(msg.raw))
                elif upper == "INTERNALDATE":
                    d = msg.internaldate
                    stamp = f"{d.day:02d}-{MONTHS[d.month - 1]}-{d.year} {d:%H:%M:%S} +0000"
                    parts.append(b'INTERNALDATE "' + stamp.encode() + b'"')
                elif upper == "BODYSTRUCTURE":
                    parsed = email.message_from_bytes(msg.raw, policy=policy.compat32)
                    parts.append(b"BODYSTRUCTURE " + bodystructure(parsed))
                elif upper.startswith("BODY"):
                    peek = upper.startswith("BODY.PEEK")
                    section = item[item.index("[") :]
                    data, key = self._section(msg.raw, section)
                    parts.append(key.encode() + b" " + _string(data, force_literal=True))
                    if not peek and not self.readonly:
                        msg.flags.add("\\Seen")
                        if "FLAGS" not in [w.upper() for w in wanted]:
                            tail.append(b"FLAGS (" + " ".join(sorted(msg.flags)).encode() + b")")
            body = b" ".join(parts + tail)
            self.sock.sendall(b"* %d FETCH (" % seqs[uid] + body + b")\r\n")
        self.send(f"{tag} OK FETCH completed".encode())

    def _section(self, raw: bytes, section: str) -> tuple[bytes, str]:
        match = re.match(r"\[(.*?)\](?:<(\d+)\.(\d+)>)?$", section, re.S)
        assert match, section
        inner = match.group(1)
        if inner.upper().startswith("HEADER.FIELDS"):
            names = re.findall(r"[A-Za-z0-9-]+", inner[len("HEADER.FIELDS") :])
            head = raw.split(b"\r\n\r\n", 1)[0] if b"\r\n\r\n" in raw else raw.split(b"\n\n", 1)[0]
            lines = re.split(rb"\r?\n(?![ \t])", head)
            keep = [
                ln
                for ln in lines
                if ln.split(b":", 1)[0].decode("latin-1").upper() in {n.upper() for n in names}
            ]
            data = b"\r\n".join(keep) + b"\r\n\r\n"
            return data, f"BODY[{inner}]"
        if inner == "":
            if match.group(2) is not None:
                start, length = int(match.group(2)), int(match.group(3))
                return raw[start : start + length], f"BODY[]<{start}>"
            return raw, "BODY[]"
        raise AssertionError(f"unsupported section {section}")

    def uid_store(self, tag: str, args: list[Any]) -> None:
        if not self._require_selected(tag):
            return
        if self.readonly:
            self.send(f"{tag} NO mailbox is read-only".encode())
            return
        assert self.selected is not None
        uids = self._uids(str(args[0]))
        op = str(args[1]).upper()
        flags = {str(f) for f in (args[2] if isinstance(args[2], list) else [args[2]])}
        for uid in uids:
            msg = self.selected.by_uid(uid)
            assert msg is not None
            if op.startswith("+"):
                msg.flags |= flags
            elif op.startswith("-"):
                msg.flags -= flags
            else:
                msg.flags = set(flags)
        self.send(f"{tag} OK STORE completed".encode())

    def _copy(self, tag: str, args: list[Any]) -> tuple[list[int], list[int], FakeMailbox] | None:
        assert self.selected is not None
        uids = self._uids(str(args[0]))
        target = self._box(tag, args[1])
        if target is None:
            return None
        new = []
        with self.srv.changed:
            for uid in uids:
                msg = self.selected.by_uid(uid)
                assert msg is not None
                new.append(target.add(msg.raw, tuple(msg.flags - {"\\Deleted"}), msg.internaldate))
            self.srv.changed.notify_all()
        return uids, new, target

    def uid_copy(self, tag: str, args: list[Any]) -> None:
        if not self._require_selected(tag):
            return
        result = self._copy(tag, args)
        if result is None:
            return
        src, dst, target = result
        self.send(
            f"{tag} OK [COPYUID {target.uidvalidity} {','.join(map(str, src))} "
            f"{','.join(map(str, dst))}] COPY completed".encode()
        )

    def uid_move(self, tag: str, args: list[Any]) -> None:
        if "MOVE" not in self.caps().split():
            self.send(f"{tag} BAD MOVE not supported".encode())
            return
        if not self._require_selected(tag):
            return
        if self.readonly:
            self.send(f"{tag} NO read-only".encode())
            return
        result = self._copy(tag, args)
        if result is None:
            return
        src, dst, target = result
        self.send(
            f"* OK [COPYUID {target.uidvalidity} {','.join(map(str, src))} "
            f"{','.join(map(str, dst))}] moved".encode()
        )
        self._expunge(set(src), require_deleted=False)
        self.send(f"{tag} OK MOVE completed".encode())

    def uid_expunge(self, tag: str, args: list[Any]) -> None:
        if not self._require_selected(tag):
            return
        self._expunge(set(self._uids(str(args[0]))))
        self.send(f"{tag} OK UID EXPUNGE completed".encode())

    def _expunge(self, only: set[int] | None, require_deleted: bool = True) -> None:
        assert self.selected is not None
        with self.srv.lock:
            seq = 1
            keep = []
            for msg in self.selected.messages:
                gone = (only is None or msg.uid in only) and (
                    not require_deleted or "\\Deleted" in msg.flags
                )
                if gone:
                    self.send(f"* {seq} EXPUNGE".encode())
                else:
                    keep.append(msg)
                    seq += 1
            self.selected.messages[:] = keep
            self.known = {m.uid for m in keep}


def bodystructure(msg: Message) -> bytes:
    """BODYSTRUCTURE for a parsed message (non-ASCII values are sent as literals)."""
    maintype = msg.get_content_maintype()
    if maintype == "multipart":
        parts = b"".join(bodystructure(p) for p in msg.get_payload())  # type: ignore[union-attr]
        boundary = msg.get_boundary() or "b"
        return (
            b"("
            + parts
            + b" "
            + _quote(msg.get_content_subtype().upper())
            + b' ("BOUNDARY" '
            + _quote(boundary)
            + b") NIL NIL)"
        )
    params: list[bytes] = []
    for key, value in (msg.get_params() or [])[1:]:
        if isinstance(value, tuple):
            key, value = key + "*", f"{value[0] or ''}'{value[1] or ''}'{value[2]}"
        params += [_quote(key.upper()), _string(str(value))]
    param_list = b"(" + b" ".join(params) + b")" if params else b"NIL"
    cid = msg.get("Content-ID")
    encoding = (msg.get("Content-Transfer-Encoding") or "7BIT").upper()
    payload = msg.get_payload()
    size = len(payload.encode("utf-8", "surrogateescape")) if isinstance(payload, str) else 0
    fields = [
        _quote(maintype.upper()),
        _quote(msg.get_content_subtype().upper()),
        param_list,
        _string(cid) if cid else b"NIL",
        b"NIL",
        _quote(encoding),
        str(size).encode(),
    ]
    if maintype == "text":
        lines = payload.count("\n") if isinstance(payload, str) else 0
        fields.append(str(lines).encode())
    elif msg.get_content_type() == "message/rfc822":
        inner = payload[0] if isinstance(payload, list) else None
        fields += [
            b"(" + b" ".join([b"NIL"] * 10) + b")",
            bodystructure(inner) if inner is not None else b"NIL",
            b"0",
        ]
    disposition = msg.get_content_disposition()
    if disposition:
        dparams: list[bytes] = []
        for key, value in (msg.get_params(header="content-disposition") or [])[1:]:
            if isinstance(value, tuple):
                key, value = key + "*", f"{value[0] or ''}'{value[1] or ''}'{value[2]}"
            dparams += [_quote(key.upper()), _string(str(value))]
        dlist = b"(" + b" ".join(dparams) + b")" if dparams else b"NIL"
        disp = b"(" + _quote(disposition.upper()) + b" " + dlist + b")"
    else:
        disp = b"NIL"
    fields += [b"NIL", disp, b"NIL"]
    return b"(" + b" ".join(fields) + b")"


def wait_until(predicate: Any, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False
