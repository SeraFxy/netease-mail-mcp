"""Search: server-side date/flag criteria, then batched client-side matching on headers.

NetEase IMAP returns nothing for SEARCH FROM / TO / SUBJECT / TEXT / BODY, so keyword
matching happens locally on decoded From/To/Cc/Subject headers, newest first, in UID
batches. Bodies are never searched (``body_searched: false`` says so honestly).
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any

from .errors import InvalidArgument, MailTimeout
from .ids import MessageRef
from .imap import (
    EXCLUDED_FROM_ALL,
    Folder,
    ImapSession,
    MailboxState,
    fetch_body,
    fetch_flags,
    parse_internaldate,
)
from .mime import (
    ParsedMessage,
    bodystructure_attachments,
    format_addresses,
    iso_date,
    parse_headers,
)
from .providers import RECEIVE_WINDOW_HINT
from .safety import FETCH_CHUNK, UNTRUSTED_NOTICE

HEADER_FIELDS = "FROM TO CC SUBJECT DATE MESSAGE-ID IN-REPLY-TO REFERENCES"
SUMMARY_ITEMS = (
    f"(UID FLAGS RFC822.SIZE INTERNALDATE BODYSTRUCTURE BODY.PEEK[HEADER.FIELDS ({HEADER_FIELDS})])"
)

# Flag criteria NetEase evaluates correctly on the server (see docs/dev/HANDOFF.md for the
# live check). Anything not listed here is filtered client-side from FLAGS.
SERVER_FLAG_KEYS = frozenset({"UNSEEN", "SEEN", "FLAGGED", "UNFLAGGED"})

_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def parse_day(value: str | None, field_name: str) -> date | None:
    if value is None or not str(value).strip():
        return None
    match = re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})", str(value).strip())
    if not match:
        raise InvalidArgument(
            f"{field_name} 需要 YYYY-MM-DD 格式，收到 {value!r}",
            hint=f"{field_name} must be YYYY-MM-DD, got {value!r}",
        )
    try:
        return date(*(int(x) for x in match.groups()))
    except ValueError:
        raise InvalidArgument(
            f"{field_name} 不是有效日期：{value!r}", hint=f"Invalid date: {value!r}"
        ) from None


def imap_date(day: date) -> str:
    return f"{day.day}-{_MONTHS[day.month - 1]}-{day.year}"


def _norm(text: str) -> str:
    return unicodedata.normalize("NFKC", text or "").casefold()


def _address_matches(address: str, wanted: str) -> bool:
    """Sender filter on an address: a full address must be equal, '@domain' matches that
    domain and its subdomains, anything else is a substring. ``wanted`` is normalized;
    ``address`` is only lower-cased, and the exact forms need it to be ASCII, so look-alikes
    such as full-width 'ｇｉｔｈｕｂ.ｃｏｍ' never pass for the real address."""
    address = address.lower()
    if "@" in wanted and not address.isascii():
        return False
    if wanted.startswith("@"):
        domain = address.rpartition("@")[2]
        return domain == wanted[1:] or domain.endswith("." + wanted[1:])
    if "@" in wanted:
        return address == wanted
    return wanted in address


# --------------------------------------------------------------------------- summaries


@dataclass
class Summary:
    ref: MessageRef
    folder: Folder
    flags: list[str]
    size: int
    internaldate: datetime | None
    headers: ParsedMessage
    attachments: int

    @property
    def unread(self) -> bool:
        return "\\seen" not in {f.lower() for f in self.flags}

    @property
    def flagged(self) -> bool:
        return "\\flagged" in {f.lower() for f in self.flags}

    @property
    def when(self) -> datetime:
        """Arrival time (INTERNALDATE), consistent with SINCE/BEFORE."""
        return self.internaldate or self.headers.date or _EPOCH

    @property
    def sort_key(self) -> tuple[datetime, int]:
        return self.when, self.ref.uid

    @property
    def written_key(self) -> tuple[datetime, datetime, int]:
        """Chronology for threads: the Date header first (Sent copies may be appended late)."""
        return self.headers.date or self.when, self.when, self.ref.uid

    def to_dict(self) -> dict[str, Any]:
        h = self.headers
        data: dict[str, Any] = {
            "id": self.ref.encode(),
            "folder": self.folder.name,
            "date": iso_date(h.date or self.internaldate),
            "from": format_addresses(h.from_),
            "to": format_addresses(h.to),
            "subject": h.subject,
            "unread": self.unread,
            "flagged": self.flagged,
            "has_attachment": self.attachments > 0,
            "size": self.size,
        }
        if h.cc:
            data["cc"] = format_addresses(h.cc)
        return data


def summarize(account: str, state: MailboxState, record: dict[str, Any]) -> Summary:
    size_value = record.get("RFC822.SIZE")
    try:
        size = int(str(size_value)) if size_value is not None else 0
    except ValueError:
        size = 0
    return Summary(
        ref=MessageRef(account, state.folder.raw, state.uidvalidity, int(record["UID"])),
        folder=state.folder,
        flags=fetch_flags(record),
        size=size,
        internaldate=parse_internaldate(record.get("INTERNALDATE")),
        headers=parse_headers(fetch_body(record) or b""),
        attachments=bodystructure_attachments(record.get("BODYSTRUCTURE")),
    )


def fetch_summaries(
    session: ImapSession, state: MailboxState, uids: Sequence[int], chunk: int = FETCH_CHUNK
) -> list[Summary]:
    if not uids:
        return []
    records = session.uid_fetch(uids, SUMMARY_ITEMS, chunk=chunk)
    return [summarize(session.account, state, r) for r in records]


# --------------------------------------------------------------------------- filters


@dataclass
class Filters:
    terms: list[str] = field(default_factory=list)
    sender: str | None = None
    recipient: str | None = None
    subject: str | None = None
    since: date | None = None
    before: date | None = None
    unread: bool | None = None
    flagged: bool | None = None
    has_attachment: bool | None = None
    sender_address_only: bool = False  # match `sender` against addresses, not display names

    @classmethod
    def build(
        cls,
        *,
        query: str | None = None,
        sender: str | None = None,
        recipient: str | None = None,
        subject: str | None = None,
        since: str | None = None,
        before: str | None = None,
        unread: bool | None = None,
        flagged: bool | None = None,
        has_attachment: bool | None = None,
        sender_address_only: bool = False,
    ) -> Filters:
        since_day = parse_day(since, "since")
        before_day = parse_day(before, "before")
        if since_day and before_day and since_day >= before_day:
            raise InvalidArgument(
                "since 必须早于 before（before 不含当天）",
                hint="since must be earlier than before (before is exclusive)",
            )
        return cls(
            terms=[_norm(t) for t in (query or "").split() if t.strip()],
            sender=_norm(sender) if sender and sender.strip() else None,
            recipient=_norm(recipient) if recipient and recipient.strip() else None,
            subject=_norm(subject) if subject and subject.strip() else None,
            since=since_day,
            before=before_day,
            unread=unread,
            flagged=flagged,
            has_attachment=has_attachment,
            sender_address_only=sender_address_only,
        )

    def server_criteria(self) -> list[str]:
        criteria: list[str] = []
        if self.since:
            criteria += ["SINCE", imap_date(self.since)]
        if self.before:
            criteria += ["BEFORE", imap_date(self.before)]
        if self.unread is not None:
            key = "UNSEEN" if self.unread else "SEEN"
            if key in SERVER_FLAG_KEYS:
                criteria.append(key)
        if self.flagged is not None:
            key = "FLAGGED" if self.flagged else "UNFLAGGED"
            if key in SERVER_FLAG_KEYS:
                criteria.append(key)
        return criteria or ["ALL"]

    @property
    def client_side(self) -> bool:
        """True when some condition can only be checked locally (needs scanning)."""
        if self.terms or self.sender or self.recipient or self.subject:
            return True
        if self.has_attachment is not None:
            return True
        if (
            self.unread is not None
            and ("UNSEEN" if self.unread else "SEEN") not in SERVER_FLAG_KEYS
        ):
            return True
        return self.flagged is not None and (
            ("FLAGGED" if self.flagged else "UNFLAGGED") not in SERVER_FLAG_KEYS
        )

    def matches(self, item: Summary) -> bool:
        h = item.headers
        sender = _norm(format_addresses(h.from_))
        recipients = _norm(format_addresses([*h.to, *h.cc]))
        subject = _norm(h.subject)
        if self.terms:
            haystack = " ".join((sender, recipients, subject))
            if not all(term in haystack for term in self.terms):
                return False
        if self.sender:
            if self.sender_address_only:
                # Display names are free text: '"noreply@github.com" <x@evil.example>'.
                if not any(_address_matches(addr, self.sender) for _, addr in h.from_):
                    return False
            elif self.sender not in sender:
                return False
        if self.recipient and self.recipient not in recipients:
            return False
        if self.subject and self.subject not in subject:
            return False
        if self.unread is not None and item.unread != self.unread:
            return False
        if self.flagged is not None and item.flagged != self.flagged:
            return False
        return self.has_attachment is None or (item.attachments > 0) == self.has_attachment

    def describe(self) -> dict[str, Any]:
        client = []
        if self.terms:
            client.append("query (From/To/Cc/Subject)")
        if self.sender:
            client.append("from (address)" if self.sender_address_only else "from (name/address)")
        if self.recipient:
            client.append("to/cc")
        if self.subject:
            client.append("subject")
        if self.has_attachment is not None:
            client.append("has_attachment (BODYSTRUCTURE)")
        return {"server": " ".join(self.server_criteria()), "client": client}


# --------------------------------------------------------------------------- search


def resolve_search_folders(
    session: ImapSession, folder: str | Sequence[str] | None
) -> list[Folder]:
    if folder is None or (isinstance(folder, str) and not folder.strip()):
        return [session.resolve_folder("INBOX")]
    if isinstance(folder, str):
        if folder.strip().lower() == "all":
            return [
                f for f in session.folders() if f.selectable and f.role not in EXCLUDED_FROM_ALL
            ]
        return [session.resolve_folder(folder)]
    resolved: list[Folder] = []
    for spec in folder:
        found = session.resolve_folder(spec)
        if all(f.raw != found.raw for f in resolved):
            resolved.append(found)
    if not resolved:
        raise InvalidArgument("folder 列表为空", hint="folder list is empty")
    return resolved


@dataclass
class _FolderScan:
    folder: Folder
    state: MailboxState
    pending: list[int]  # newest first
    matches: list[Summary] = field(default_factory=list)


def search(
    session: ImapSession,
    folders: Sequence[Folder],
    filters: Filters,
    *,
    limit: int,
    max_scan: int,
) -> dict[str, Any]:
    criteria = filters.server_criteria()
    scans: list[_FolderScan] = []
    timed_out = False
    scanned = 0
    try:
        for folder in folders:
            state = session.open_folder(folder)
            uids = session.uid_search(criteria)
            scans.append(_FolderScan(folder, state, list(reversed(uids))))
        total = sum(len(s.pending) for s in scans)
        if not filters.client_side:
            # The server already applied every condition: fetch just the newest `limit`.
            for scan in scans:
                take, scan.pending = scan.pending[:limit], scan.pending[limit:]
                session.open_folder(scan.folder)
                scan.matches = fetch_summaries(session, scan.state, take)
                scanned += len(take)
        else:
            chunk = FETCH_CHUNK if len(scans) == 1 else max(50, FETCH_CHUNK // 2)
            active = [s for s in scans if s.pending]
            while active and scanned < max_scan:
                for scan in list(active):
                    if scanned >= max_scan:
                        break
                    size = min(chunk, max_scan - scanned, len(scan.pending))
                    batch, scan.pending = scan.pending[:size], scan.pending[size:]
                    session.open_folder(scan.folder)
                    found = fetch_summaries(session, scan.state, batch, chunk=chunk)
                    scanned += len(batch)
                    for item in sorted(found, key=lambda s: s.ref.uid, reverse=True):
                        if filters.matches(item):
                            scan.matches.append(item)
                    if len(scan.matches) >= limit or not scan.pending:
                        active.remove(scan)
    except MailTimeout:
        timed_out = True
        total = sum(len(s.pending) + len(s.matches) for s in scans)
    merged = sorted((m for s in scans for m in s.matches), key=lambda s: s.sort_key, reverse=True)
    truncated = filters.client_side and any(s.pending and len(s.matches) < limit for s in scans)
    results = merged[:limit]
    more = (
        len(merged) > limit
        or truncated
        or (not filters.client_side and any(s.pending for s in scans))
    )
    out: dict[str, Any] = {
        "account": session.account,
        "folders": [f.name for f in folders],
        "messages": [m.to_dict() for m in results],
        "returned": len(results),
        "scanned": scanned,
        "total_candidates": total,
        "truncated": truncated or timed_out,
        "more_available": more or timed_out,
        "body_searched": False,
        "criteria": filters.describe(),
        "untrusted_content": True,
        "notice": UNTRUSTED_NOTICE,
    }
    notes = []
    if filters.client_side:
        notes.append(
            "query/from/to/subject 只匹配发件人、收件人、抄送和主题，不搜正文（网易 IMAP 不支持）。"
            " Keyword filters match From/To/Cc/Subject only; NetEase IMAP cannot search bodies."
        )
    if truncated:
        notes.append(
            f"只检查了最新的 {scanned} 封候选邮件；"
            "可加大 max_scan（≤5000）或用 since/before 缩小范围。"
            f" Only the newest {scanned} candidates were checked; raise max_scan (≤5000) or "
            "narrow with since/before."
        )
    if timed_out:
        out["timed_out"] = True
        notes.append("服务器响应超时，结果不完整。The server timed out; results are partial.")
    if not results and total == 0:
        notes.append(RECEIVE_WINDOW_HINT)
    if notes:
        out["notes"] = notes
    return out
