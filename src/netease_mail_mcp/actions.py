"""Actions: drafts, move/trash/mark/flag, SMTP send, waiting for mail, code extraction."""

from __future__ import annotations

import email
import imaplib
import re
import smtplib
import ssl
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from email.message import EmailMessage
from pathlib import Path
from typing import Any

from .errors import (
    AuthError,
    ConnectionFailed,
    InvalidArgument,
    MailError,
    MailTimeout,
    NotFoundError,
    ProtocolError,
)
from .ids import MessageRef, decode_id, encode_id
from .imap import Folder, ImapSession, fetch_body, fetch_flags
from .mime import (
    _COMPOSE_POLICY,
    _REPLY_PREFIX,
    Composed,
    OutgoingAttachment,
    compose,
    format_address,
    format_addresses,
    forward_block,
    forward_subject,
    html_preview,
    load_attachment,
    message_ids,
    normalize_recipients,
    parse_headers,
    parse_message,
    quote_original,
    reply_recipients,
    reply_subject,
    reply_threading,
    safe_filename,
    serialize,
    snippet,
    text_preview,
)
from .providers import login_failure_hint, provider_for
from .reading import fetch_raw
from .safety import (
    MAX_ATTACHMENTS,
    PREVIEW_TEXT_CHARS,
    UNTRUSTED_NOTICE,
    sha256_hex,
    wrap_preview,
    wrap_untrusted,
)
from .search import Filters, fetch_summaries, imap_date

# --------------------------------------------------------------------------- composing


def build_composed(
    session: ImapSession,
    account: str,
    *,
    to: str | Sequence[str] | None = None,
    cc: str | Sequence[str] | None = None,
    bcc: str | Sequence[str] | None = None,
    subject: str | None = None,
    body: str | None = None,
    html_body: str | None = None,
    reply_to_id: str | None = None,
    reply_all: bool = False,
    quote: bool = True,
    forward_id: str | None = None,
    forward_mode: str = "inline",
    attachments: Sequence[str] | None = None,
    attachment_roots: Sequence[Path] = (),
) -> Composed:
    if reply_to_id and forward_id:
        raise InvalidArgument(
            "reply_to_id 和 forward_id 只能二选一。",
            hint="Use reply_to_id or forward_id, not both.",
        )
    if forward_mode not in ("inline", "attachment"):
        raise InvalidArgument("forward_mode 只能是 inline 或 attachment", hint="bad forward_mode")
    paths = list(attachments or [])
    if len(paths) > MAX_ATTACHMENTS:
        raise InvalidArgument(
            f"附件最多 {MAX_ATTACHMENTS} 个。", hint=f"At most {MAX_ATTACHMENTS} attachments."
        )
    files: list[OutgoingAttachment] = [load_attachment(p, roots=attachment_roots) for p in paths]
    to_list = normalize_recipients(to, "to")
    cc_list = normalize_recipients(cc, "cc")
    bcc_list = normalize_recipients(bcc, "bcc")
    text = body or ""
    in_reply_to = references = None
    attached: list[tuple[str, bytes]] = []

    def load(original_id: str) -> tuple[bytes, Any]:
        ref = decode_id(original_id)
        if ref.account != account:
            raise InvalidArgument(
                f"这封邮件属于 {ref.account}，不能用 {account} 回复/转发；请把 account 设为 "
                f"{ref.account}。",
                hint=f"The message belongs to {ref.account}; set account accordingly.",
            )
        session.open_ref(ref)
        _, raw = fetch_raw(session, ref.uid)
        return raw, parse_message(raw)

    if reply_to_id:
        _, original = load(reply_to_id)
        in_reply_to, references = reply_threading(original)
        derived_to, derived_cc = reply_recipients(original, account, reply_all)
        if not to_list:
            to_list = derived_to
        if reply_all and not cc_list:
            taken = {a.lower() for a in to_list}
            cc_list = [c for c in derived_cc if c.lower() not in taken]
        if subject is None or not subject.strip():
            subject = reply_subject(original.subject)
        elif _REPLY_PREFIX.match(subject):
            subject = reply_subject(subject)
        if quote and original.text:
            text = text.rstrip() + "\n\n" + quote_original(original)
    if forward_id:
        raw, original = load(forward_id)
        if subject is None or not subject.strip():
            subject = forward_subject(original.subject)
        if forward_mode == "attachment":
            attached.append((safe_filename(original.subject or "message")[:100] + ".eml", raw))
        else:
            text = text.rstrip() + "\n\n" + forward_block(original)
            files.extend(
                OutgoingAttachment(a.filename, a.content_type, a.payload())
                for a in original.attachments
                if not a.inline
            )
    if subject is None:
        raise InvalidArgument("缺少 subject（主题）。", hint="subject is required")
    if not (to_list or cc_list or bcc_list):
        raise InvalidArgument("缺少收件人 to。", hint="At least one recipient (to) is required.")
    return compose(
        from_addr=account,
        to=to_list,
        cc=cc_list,
        bcc=bcc_list,
        subject=subject,
        body=text,
        html_body=html_body,
        attachments=files,
        attached_messages=attached,
        in_reply_to=in_reply_to,
        references=references,
    )


# --------------------------------------------------------------------------- drafts


def find_by_message_id(
    session: ImapSession, folder: Folder, message_id: str, *, days: int = 2, max_check: int = 50
) -> tuple[int, int] | None:
    """Find (uidvalidity, uid) of a message by Message-ID (NetEase cannot SEARCH HEADER)."""
    state = session.open_folder(folder, force=True)
    since = date.today() - timedelta(days=days)
    uids = list(reversed(session.uid_search(["SINCE", imap_date(since)])))[:max_check]
    if not uids:
        return None
    for record in session.uid_fetch(uids, "(UID BODY.PEEK[HEADER.FIELDS (MESSAGE-ID)])"):
        if message_id in message_ids(parse_headers(fetch_body(record) or b"").message_id):
            return state.uidvalidity, int(record["UID"])
    return None


def require_folder(session: ImapSession, role: str) -> Folder:
    folder = session.folder_by_role(role)
    if folder is None:
        names = {"drafts": "草稿箱 Drafts", "sent": "已发送 Sent", "trash": "已删除 Trash"}
        raise NotFoundError(
            f"找不到{names.get(role, role)}文件夹。", hint=f"No {role} folder found on the server."
        )
    return folder


def append_draft(session: ImapSession, composed: Composed) -> dict[str, Any]:
    drafts = require_folder(session, "drafts")
    raw = serialize(composed.message)
    uidvalidity, uid = session.append(drafts, "(\\Draft \\Seen)", raw)
    message_id = str(composed.message["Message-ID"])
    if uid is None:
        found = find_by_message_id(session, drafts, message_id)
        if found:
            uidvalidity, uid = found
    if uid is not None and uidvalidity is None:
        uidvalidity = session.open_folder(drafts, force=True).uidvalidity
    draft_id = (
        encode_id(session.account, drafts.raw, uidvalidity, uid)
        if uid is not None and uidvalidity is not None
        else None
    )
    return {
        "saved": True,
        "draft_id": draft_id,
        "folder": drafts.name,
        "message_id": message_id,
        "size": len(raw),
        "preview": wrap_preview(composed.preview()),
        "untrusted_content": True,
        "notice": UNTRUSTED_NOTICE,
    }


def require_uidplus(session: ImapSession) -> None:
    """Refuse before changing anything when exactly-these-UIDs EXPUNGE is impossible."""
    if not session.has("UIDPLUS"):
        raise ProtocolError(
            "服务器既不支持 MOVE 也不支持 UIDPLUS，无法只删除指定邮件；没有做任何修改。",
            hint="The server supports neither MOVE nor UIDPLUS, so it cannot expunge only the "
            "given messages; nothing was changed.",
        )


def ensure_can_move(session: ImapSession) -> None:
    if not session.has("MOVE"):
        require_uidplus(session)


def delete_draft(session: ImapSession, ref: MessageRef) -> None:
    """Remove exactly one draft (used after sending it, and by live tests)."""
    folder = session.folder_by_raw(ref.folder)
    if folder.role != "drafts":
        raise InvalidArgument(
            "只能删除草稿箱里的草稿。", hint="Only messages in the Drafts folder can be deleted."
        )
    require_uidplus(session)
    session.open_ref(ref, readonly=False)
    session.fetch_one(ref.uid, "(UID FLAGS)")
    session.uid_store([ref.uid], "+FLAGS.SILENT", "(\\Deleted)")
    session.uid_expunge([ref.uid])


# --------------------------------------------------------------------------- manage

FLAG_ACTIONS = {
    "mark_read": ("+FLAGS.SILENT", "(\\Seen)"),
    "mark_unread": ("-FLAGS.SILENT", "(\\Seen)"),
    "flag": ("+FLAGS.SILENT", "(\\Flagged)"),
    "unflag": ("-FLAGS.SILENT", "(\\Flagged)"),
}
MOVE_ACTIONS = ("move", "trash")


def group_refs(refs: Sequence[MessageRef]) -> dict[str, list[MessageRef]]:
    groups: dict[str, list[MessageRef]] = {}
    for ref in refs:
        groups.setdefault(ref.folder, []).append(ref)
    return groups


def set_flags(session: ImapSession, refs: Sequence[MessageRef], action: str) -> dict[str, Any]:
    operation, flags = FLAG_ACTIONS[action]
    done: list[str] = []
    for _raw, items in group_refs(refs).items():
        session.open_ref(items[0], readonly=False)
        for ref in items[1:]:
            session.open_ref(ref, readonly=False)
        present = {int(r["UID"]) for r in session.uid_fetch([r.uid for r in items], "(UID)")}
        missing = [r for r in items if r.uid not in present]
        if missing:
            raise NotFoundError(
                f"{len(missing)} 封邮件已不在原文件夹，未做任何修改。",
                hint="Some messages are gone from their folder; nothing was changed.",
            )
        session.uid_store([r.uid for r in items], operation, flags)
        done.extend(r.encode() for r in items)
    return {"action": action, "done": True, "count": len(done), "ids": done}


def describe_targets(session: ImapSession, refs: Sequence[MessageRef]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for _raw, group in group_refs(refs).items():
        state = session.open_ref(group[0])
        for ref in group[1:]:
            session.open_ref(ref)
        found = {s.ref.uid: s for s in fetch_summaries(session, state, [r.uid for r in group])}
        for ref in group:
            summary = found.get(ref.uid)
            if summary is None:
                raise NotFoundError(
                    f"邮件 {ref.encode()} 已不在 {state.folder.name}，请重新搜索。",
                    hint="A message is no longer in its folder; search again.",
                )
            data = summary.to_dict()
            items.append(
                {k: data[k] for k in ("id", "folder", "date", "from", "subject") if k in data}
            )
    return items


def resolve_move_target(session: ImapSession, action: str, target_folder: str | None) -> Folder:
    ensure_can_move(session)
    if action == "trash":
        return require_folder(session, "trash")
    if not target_folder:
        raise InvalidArgument("move 需要 target_folder。", hint="move needs target_folder")
    target = session.resolve_folder(target_folder)
    if not target.selectable:
        raise InvalidArgument(
            f"{target.name} 不能存放邮件。", hint=f"{target.name} cannot hold messages"
        )
    return target


def move_messages(
    session: ImapSession, refs: Sequence[MessageRef], target: Folder
) -> dict[str, Any]:
    ensure_can_move(session)
    moved: list[dict[str, Any]] = []
    for raw_name, items in group_refs(refs).items():
        if raw_name == target.raw:
            raise InvalidArgument(
                f"邮件已经在 {target.name} 里。", hint=f"Messages are already in {target.name}."
            )
        session.open_ref(items[0], readonly=False)
        for ref in items[1:]:
            session.open_ref(ref, readonly=False)
        uids = [r.uid for r in items]
        if session.has("MOVE"):
            mapping = session.uid_move(uids, target)
            method = "MOVE"
        else:
            mapping = session.uid_copy(uids, target)
            session.uid_store(uids, "+FLAGS.SILENT", "(\\Deleted)")
            session.uid_expunge(uids)  # UID EXPUNGE: only these UIDs, never plain EXPUNGE
            method = "COPY+STORE+UID EXPUNGE"
        for ref in items:
            entry: dict[str, Any] = {"old_id": ref.encode()}
            if mapping and ref.uid in mapping[1]:
                entry["new_id"] = encode_id(
                    ref.account, target.raw, mapping[0], mapping[1][ref.uid]
                )
            moved.append(entry)
    return {
        "done": True,
        "target_folder": target.name,
        "method": method if refs else None,
        "count": len(moved),
        "moved": moved,
    }


# --------------------------------------------------------------------------- SMTP

SmtpFactory = Callable[[str, int, bool, float], smtplib.SMTP]


def default_smtp_factory(host: str, port: int, implicit_tls: bool, timeout: float) -> smtplib.SMTP:
    context = ssl.create_default_context()
    if implicit_tls:
        return smtplib.SMTP_SSL(host, port, timeout=timeout, context=context)
    client = smtplib.SMTP(host, port, timeout=timeout)
    try:
        client.ehlo()
        client.starttls(context=context)
        client.ehlo()
    except BaseException:
        try:
            client.close()
        except Exception:
            pass
        raise
    return client


def _quit(client: smtplib.SMTP) -> None:
    try:
        client.quit()
    except Exception:
        try:
            client.close()
        except Exception:
            pass


def smtp_login(
    account: str, auth_code: str, factory: SmtpFactory, timeout: float = 30.0
) -> tuple[smtplib.SMTP, int]:
    """465 implicit TLS first, then 25 + STARTTLS (NetEase's 587 times out)."""
    provider = provider_for(account)
    attempts: list[str] = []
    for port, implicit in ((provider.smtp_ssl_port, True), (provider.smtp_starttls_port, False)):
        try:
            client = factory(provider.smtp_host, port, implicit, timeout)
        except (OSError, smtplib.SMTPException) as exc:
            attempts.append(f"{port}: {type(exc).__name__}: {exc}")
            continue
        try:
            client.login(account, auth_code)
        except smtplib.SMTPAuthenticationError as exc:
            _quit(client)
            raise AuthError(
                f"SMTP 登录失败：{exc.smtp_code} {exc.smtp_error!r}",
                hint=login_failure_hint(account),
            ) from None
        except (OSError, smtplib.SMTPException) as exc:
            _quit(client)
            attempts.append(f"{port}: {type(exc).__name__}: {exc}")
            continue
        return client, port
    raise ConnectionFailed(
        f"无法连接 SMTP 服务器 {provider.smtp_host}：{'; '.join(attempts)}",
        hint=f"Cannot reach {provider.smtp_host} on 465 or 25.",
    )


def smtp_send(
    account: str,
    auth_code: str,
    message: EmailMessage,
    recipients: Sequence[str],
    *,
    factory: SmtpFactory = default_smtp_factory,
    timeout: float = 30.0,
) -> dict[str, Any]:
    client, port = smtp_login(account, auth_code, factory, timeout)
    try:
        # send_message drops Bcc headers from the transmitted copy.
        refused = client.send_message(message, from_addr=account, to_addrs=list(recipients))
    except smtplib.SMTPRecipientsRefused as exc:
        raise ProtocolError(
            f"所有收件人都被服务器拒绝：{exc.recipients}",
            hint="The server refused every recipient; nothing was sent.",
        ) from None
    except Exception as exc:
        raise ProtocolError(
            f"发送过程中出错（{type(exc).__name__}: {exc}）。邮件可能没有发出，为避免重复发送不会"
            "自动重试；请先在「已发送」里确认。",
            hint="Sending failed midway; the message may or may not have been sent. Not "
            "retrying automatically to avoid duplicates; check the Sent folder first.",
        ) from None
    finally:
        _quit(client)
    return {
        "smtp_port": port,
        "refused": {
            addr: [code, msg.decode("utf-8", "replace") if isinstance(msg, bytes) else str(msg)]
            for addr, (code, msg) in (refused or {}).items()
        },
    }


def ensure_sent_copy(
    session: ImapSession,
    raw: bytes,
    message_id: str,
    *,
    sleep: Callable[[float], None] = time.sleep,
    delays: Sequence[float] = (0.0, 2.0, 3.0),
) -> str:
    """Make sure Sent has a copy: keep the server's own copy if one appears, else APPEND."""
    sent = session.folder_by_role("sent")
    if sent is None:
        return "no_sent_folder"
    for delay in delays:
        if delay:
            sleep(delay)
        if find_by_message_id(session, sent, message_id, days=1, max_check=30):
            return "stored_by_server"
    session.append(sent, "(\\Seen)", raw)
    return "appended"


@dataclass
class PreparedSend:
    account: str
    message: EmailMessage
    recipients: list[str]
    payload: dict[str, Any]
    preview: dict[str, Any]
    draft_ref: MessageRef | None = None
    delete_draft: bool = False


def prepare_draft_send(
    session: ImapSession, account: str, draft_id: str, delete_after: bool
) -> PreparedSend:
    ref = decode_id(draft_id)
    if ref.account != account:
        raise InvalidArgument(
            f"草稿属于 {ref.account}，请把 account 设为它。",
            hint=f"The draft belongs to {ref.account}.",
        )
    folder = session.folder_by_raw(ref.folder)
    if folder.role != "drafts":
        raise InvalidArgument(
            f"draft_id 必须指向草稿箱里的草稿，这封邮件在 {folder.name}。"
            "要转发或回复别的邮件，请用 forward_id / reply_to_id。",
            hint=f"draft_id must be a message in the Drafts folder (this one is in "
            f"{folder.name}); use forward_id / reply_to_id for other mail.",
        )
    state = session.open_ref(ref)
    meta, raw = fetch_raw(session, ref.uid)
    flags = {f.lower() for f in fetch_flags(meta)}
    if "\\draft" in state.flags and "\\draft" not in flags:
        raise InvalidArgument(
            "这封邮件没有 \\Draft 标记，不是草稿，不能按草稿发送。",
            hint="The message is not flagged \\Draft, so it is not sent as a draft.",
        )
    parsed = parse_message(raw)
    message = email.message_from_bytes(raw, _class=EmailMessage, policy=_COMPOSE_POLICY)
    if not message.get("Message-ID"):
        message["Message-ID"] = email.utils.make_msgid(domain=account.rsplit("@", 1)[-1])
    recipients: list[str] = []
    for _, addr in [*parsed.to, *parsed.cc, *parsed.bcc]:
        if addr not in recipients:
            recipients.append(addr)
    if not recipients:
        raise InvalidArgument("草稿没有收件人。", hint="The draft has no recipients.")
    plain = parsed.text if parsed.text_source == "plain" else ""
    sender = format_addresses(parsed.from_)
    reply_to = format_addresses(parsed.reply_to)
    preview: dict[str, Any] = {
        "draft_id": draft_id,
        "from": sender or None,
        "reply_to": reply_to or None,
        "to": [format_address(n, a) for n, a in parsed.to],
        "cc": [format_address(n, a) for n, a in parsed.cc],
        "bcc": [format_address(n, a) for n, a in parsed.bcc],
        "subject": parsed.subject,
        **text_preview("body", plain, PREVIEW_TEXT_CHARS),
    }
    if parsed.html.strip():
        preview.update(html_preview(parsed.html, plain, PREVIEW_TEXT_CHARS))
    preview["attachments"] = [{**a.to_dict(), "source": "draft"} for a in parsed.attachments]
    preview["delete_draft_after_send"] = delete_after
    if parsed.in_reply_to:
        preview["in_reply_to"] = parsed.in_reply_to
    warnings = []
    if [a for _, a in parsed.from_] != [account.lower()]:
        warnings.append(
            f"草稿的发件人 From 是 {sender or '（空 empty）'}，不是 {account}。"
            f" The draft's From is {sender or '(empty)'}, not {account}."
        )
    if parsed.reply_to and [a for _, a in parsed.reply_to] != [account.lower()]:
        warnings.append(
            f"收件人回复时会发往 Reply-To：{reply_to}。 Replies will go to Reply-To: {reply_to}."
        )
    if warnings:
        preview["warnings"] = warnings
    payload = {
        "action": "send_email",
        "account": account,
        "draft": draft_id,
        "raw_sha256": sha256_hex(raw),
        "recipients": recipients,
        "delete_draft": delete_after,
    }
    return PreparedSend(account, message, recipients, payload, preview, ref, delete_after)


def prepare_compose_send(session: ImapSession, account: str, **compose_args: Any) -> PreparedSend:
    composed = build_composed(session, account, **compose_args)
    payload = {"action": "send_email", "account": account, **composed.digest_payload()}
    return PreparedSend(
        account, composed.message, composed.envelope_recipients, payload, composed.preview()
    )


# --------------------------------------------------------------------------- codes

_KEYWORDS = re.compile(
    r"(验证码|校验码|动态码|确认码|激活码|安全码|认证码|动态密码|一次性密码|驗證碼|"
    r"\bverification\b|\bverify\b|\bpasscode\b|\bsecurity code\b|\bone[- ]time\b|"
    r"\blogin code\b|\bcode\b|\botp\b|\bpin\b|\b2fa\b)",
    re.IGNORECASE,
)
_URLS = re.compile(r"https?://\S+|www\.\S+|\S+@\S+\.\S+", re.IGNORECASE)
_CANDIDATE = re.compile(r"(?<![A-Za-z0-9])([A-Za-z0-9]{4,8})(?![A-Za-z0-9])")


def extract_codes(subject: str, body: str, limit: int = 5) -> list[str]:
    """Likely verification codes: 4–8 char tokens with digits, close to a code keyword."""
    scored: dict[str, float] = {}
    for bonus, text in ((5.0, subject or ""), (0.0, body or "")):
        clean = _URLS.sub(" ", text)
        keywords = [m.span() for m in _KEYWORDS.finditer(clean)]
        if not keywords:
            continue
        for match in _CANDIDATE.finditer(clean):
            token = match.group(1)
            digits = sum(c.isdigit() for c in token)
            if digits == 0:
                continue
            if not token.isdigit() and not (digits >= 2 or token.isupper()):
                continue
            distances = []
            for start, end in keywords:
                if match.start() >= end:
                    distances.append(match.start() - end)
                elif match.end() <= start:
                    distances.append((start - match.end()) * 2)
                else:
                    distances.append(0)
            distance = min(distances)
            if distance > 80:
                continue
            if token.isdigit() and len(token) == 4 and 1900 <= int(token) <= 2099 and distance > 3:
                continue  # probably a year ("© 2026"), unless it directly follows the keyword
            score = 100.0 - distance + bonus
            if token.isdigit():
                score += 15 if len(token) == 6 else 8
            scored[token] = max(scored.get(token, 0.0), score)
    return [t for t, _ in sorted(scored.items(), key=lambda kv: -kv[1])][:limit]


# --------------------------------------------------------------------------- waiting


RECONNECT_ATTEMPTS = 3


def _idle_once(session: ImapSession, duration: float) -> str:
    """IDLE until EXISTS or ``duration`` (Python 3.14+ imaplib).

    Returns "ok", "unsupported" (fall back to polling on the same connection) or "broken"
    (the connection died; the session is marked broken and must be replaced).
    """
    idle = getattr(session.conn, "idle", None)
    if idle is None:
        return "unsupported"
    try:
        with idle(duration=duration) as idler:
            for typ, _data in idler:
                if typ == "EXISTS":
                    break
        return "ok"
    except (imaplib.IMAP4.abort, OSError):
        session.broken = True
        return "broken"
    except imaplib.IMAP4.error:
        return "unsupported"


def wait_for_message(
    session: ImapSession,
    folder: Folder,
    filters: Filters,
    *,
    timeout_s: float,
    poll_s: float,
    lookback_s: float = 0,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    reconnect: Callable[[], ImapSession] | None = None,
) -> dict[str, Any]:
    """Wait for a new matching message.

    The UIDNEXT baseline taken at the start is kept for the whole wait. If the connection
    drops, up to RECONNECT_ATTEMPTS fresh sessions are opened with ``reconnect`` and waiting
    continues from the same baseline; if that fails, a timed-out result with ``interrupted``
    and the reason is returned instead of an error. Sessions opened here are closed here.
    """
    started = clock()
    deadline = started + timeout_s
    state = session.open_folder(folder, force=True)
    if state.uidnext:
        baseline = state.uidnext
    else:
        existing = session.uid_search(["ALL"])
        baseline = (existing[-1] + 1) if existing else 1
    checked: set[int] = set()
    pending: list[int] = []
    if lookback_s > 0:
        cutoff = datetime.now(timezone.utc) - timedelta(seconds=lookback_s)
        recent = session.uid_search(["SINCE", imap_date(cutoff.date())])
        for item in fetch_summaries(session, state, [u for u in recent if u < baseline]):
            if item.internaldate and item.internaldate >= cutoff:
                pending.append(item.ref.uid)
    use_idle = session.has("IDLE") and hasattr(session.conn, "idle")
    mode = "idle" if use_idle else "poll"
    reconnects = 0
    owned: list[ImapSession] = []

    def finish(data: dict[str, Any]) -> dict[str, Any]:
        data["waited_s"] = round(clock() - started, 1)
        data["wait_mode"] = mode
        if reconnects:
            data["reconnects"] = reconnects
        return data

    try:
        while True:
            try:
                fresh = [u for u in session.uid_search(["UID", f"{baseline}:*"]) if u >= baseline]
                candidates = sorted({*pending, *fresh} - checked)
                summaries = fetch_summaries(session, state, candidates)
                pending = []
                for item in sorted(summaries, key=lambda s: s.ref.uid):
                    checked.add(item.ref.uid)
                    if not filters.matches(item):
                        continue
                    text = ""
                    if item.size <= 5 * 1024 * 1024:
                        try:
                            _, raw = fetch_raw(session, item.ref.uid)
                            text = parse_message(raw).text
                        except MailError:
                            text = ""
                    return finish(
                        {
                            "found": True,
                            "message": item.to_dict(),
                            "codes": extract_codes(item.headers.subject, text),
                            "snippet": wrap_untrusted(snippet(text, 600)) if text else None,
                            "untrusted_content": True,
                            "notice": UNTRUSTED_NOTICE,
                        }
                    )
                checked.update(candidates)
                remaining = deadline - clock()
                if remaining <= 0:
                    return finish(
                        {
                            "found": False,
                            "timed_out": True,
                            "checked_new_messages": len(checked),
                            "hint": "没有等到符合条件的新邮件；可以检查垃圾邮件文件夹，或放宽 "
                            "from/subject。 No matching new message; check the Junk folder or "
                            "relax from/subject.",
                        }
                    )
                if use_idle:
                    outcome = _idle_once(session, min(remaining, 30.0))
                    if outcome == "broken":
                        raise ConnectionFailed(
                            "IDLE 时连接中断。", hint="The connection dropped during IDLE."
                        )
                    if outcome == "unsupported":
                        use_idle, mode = False, "poll"
                if not use_idle:
                    sleep(max(0.2, min(poll_s, remaining)))
                state = session.open_folder(folder, force=True)  # refresh the mailbox view
                if state.uidnext and state.uidnext < baseline:
                    baseline = state.uidnext  # UIDVALIDITY reset
            except (ConnectionFailed, MailTimeout) as exc:
                session.broken = True
                failure: MailError = exc
                replacement: ImapSession | None = None
                while reconnect is not None and reconnects < RECONNECT_ATTEMPTS:
                    if clock() >= deadline:
                        break
                    reconnects += 1
                    try:
                        candidate = reconnect()
                    except MailError as err:
                        failure = err
                        sleep(max(0.2, min(1.0 * reconnects, deadline - clock())))
                        continue
                    owned.append(candidate)
                    try:
                        state = candidate.open_folder(folder, force=True)
                    except MailError as err:
                        failure = err
                        candidate.broken = True
                        continue
                    replacement = candidate
                    break
                if replacement is None:
                    return finish(
                        {
                            "found": False,
                            "timed_out": True,
                            "interrupted": True,
                            "reason": failure.render(),
                            "baseline_uidnext": baseline,
                            "checked_new_messages": len(checked),
                            "hint": "等待中连接断开且无法恢复；可以稍后再调用，并用 lookback_s "
                            "覆盖中断的这段时间。 The connection dropped and could not be "
                            "restored; call again later with lookback_s covering the gap.",
                        }
                    )
                session = replacement
                if state.uidnext and state.uidnext < baseline:
                    baseline = state.uidnext
                use_idle = use_idle and session.has("IDLE") and hasattr(session.conn, "idle")
    finally:
        for extra in owned:
            extra.close()
