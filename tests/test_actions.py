from __future__ import annotations

import imaplib
import smtplib
import threading
import time

import pytest

from netease_mail_mcp import actions, imap
from netease_mail_mcp.actions import (
    append_draft,
    default_smtp_factory,
    delete_draft,
    ensure_sent_copy,
    extract_codes,
    move_messages,
    smtp_login,
    wait_for_message,
)
from netease_mail_mcp.errors import (
    AuthError,
    ConnectionFailed,
    InvalidArgument,
    MailTimeout,
    NotFoundError,
    ProtocolError,
)
from netease_mail_mcp.ids import MessageRef, decode_id
from netease_mail_mcp.imap import open_session
from netease_mail_mcp.mime import compose
from netease_mail_mcp.search import Filters

from .conftest import PASSWORD, SmtpRecorder
from .fakeimap import DRAFTS, INBOX, SENT, TRASH, FakeImapServer, wait_until
from .samples import ME, code_mail, simple


def test_extract_codes_numeric_alnum_and_noise() -> None:
    assert (
        extract_codes("您的验证码", "您的验证码是：482913，10 分钟内有效。订单号 20261028991。")[0]
        == "482913"
    )
    assert extract_codes("Your code", "Your verification code is K7Q2ZP.") == ["K7Q2ZP"]
    assert extract_codes("Sign in", "Use code 5521 to sign in. © 2026 Example") == ["5521"]
    assert extract_codes("Welcome", "Thanks for joining! Order 123456 ships soon.") == []
    assert extract_codes("123456 is your Slack confirmation code", "")[0] == "123456"
    assert "2026" not in extract_codes("验证码", "验证码 839201，2026 年有效")


def test_wait_for_message_polling_detects_new_mail(imap_server: FakeImapServer) -> None:
    imap_server.deliver(INBOX, simple(subject="old code mail"))
    session = open_session(ME, PASSWORD, factory=imap_server.connect)
    folder = session.resolve_folder("INBOX")

    def later() -> None:
        time.sleep(0.2)
        imap_server.deliver(INBOX, simple(subject="newsletter"))
        imap_server.deliver(INBOX, code_mail("739104"))

    threading.Thread(target=later, daemon=True).start()
    result = wait_for_message(
        session,
        folder,
        Filters.build(subject="验证码"),
        timeout_s=5,
        poll_s=0.05,
        sleep=time.sleep,
    )
    session.close()
    assert result["found"] is True and result["codes"][0] == "739104"
    assert result["message"]["subject"] == "您的验证码" and result["wait_mode"] == "poll"


def test_wait_for_message_ignores_existing_and_times_out(imap_server: FakeImapServer) -> None:
    imap_server.deliver(INBOX, code_mail("111111"))
    session = open_session(ME, PASSWORD, factory=imap_server.connect)
    clock = [0.0]

    def fake_sleep(seconds: float) -> None:
        clock[0] += seconds

    result = wait_for_message(
        session,
        session.resolve_folder("INBOX"),
        Filters.build(),
        timeout_s=3,
        poll_s=1,
        clock=lambda: clock[0],
        sleep=fake_sleep,
    )
    assert result["found"] is False and result["timed_out"] is True
    hit = wait_for_message(
        session,
        session.resolve_folder("INBOX"),
        Filters.build(),
        timeout_s=1,
        poll_s=1,
        lookback_s=600,
        clock=lambda: clock[0],
        sleep=fake_sleep,
    )
    session.close()
    assert hit["found"] is True and hit["codes"] == ["111111"]


@pytest.mark.skipif(not hasattr(__import__("imaplib").IMAP4, "idle"), reason="imaplib.idle: 3.14+")
def test_wait_for_message_uses_idle_when_available() -> None:
    server = FakeImapServer(user=ME, password=PASSWORD, post_login_extra=("IDLE",))
    session = open_session(ME, PASSWORD, factory=server.connect)
    threading.Timer(0.3, lambda: server.deliver(INBOX, code_mail("246810"))).start()
    result = wait_for_message(
        session, session.resolve_folder("INBOX"), Filters.build(), timeout_s=5, poll_s=5
    )
    session.close()
    assert result["found"] and result["wait_mode"] == "idle" and result["codes"] == ["246810"]


def test_move_without_move_capability_uses_uid_expunge_only(imap_server: FakeImapServer) -> None:
    keep = imap_server.deliver(INBOX, simple(subject="keep"), flags=("\\Deleted",))
    gone = imap_server.deliver(INBOX, simple(subject="move me"))
    session = open_session(ME, PASSWORD, factory=imap_server.connect)
    target = session.resolve_folder("trash")
    result = move_messages(session, [MessageRef(ME, INBOX, 1001, gone)], target)
    session.close()
    assert result["method"] == "COPY+STORE+UID EXPUNGE"
    assert decode_id(result["moved"][0]["new_id"]).folder == TRASH
    assert [m.uid for m in imap_server.mailbox(INBOX).messages] == [keep]  # other \Deleted kept
    assert len(imap_server.mailbox(TRASH).messages) == 1
    assert not imap_server.commands("EXPUNGE")  # never a plain EXPUNGE


def test_move_with_move_capability() -> None:
    server = FakeImapServer(user=ME, password=PASSWORD, post_login_extra=("MOVE",))
    uid = server.deliver(INBOX, simple())
    session = open_session(ME, PASSWORD, factory=server.connect)
    result = move_messages(
        session, [MessageRef(ME, INBOX, 1001, uid)], session.resolve_folder("已发送")
    )
    session.close()
    assert result["method"] == "MOVE" and len(server.mailbox(SENT).messages) == 1
    assert not server.mailbox(INBOX).messages


def test_draft_append_and_delete_only_that_uid(imap_server: FakeImapServer) -> None:
    other = imap_server.deliver(DRAFTS, simple(subject="user draft"), flags=("\\Deleted",))
    session = open_session(ME, PASSWORD, factory=imap_server.connect)
    result = append_draft(
        session, compose(from_addr=ME, to=["a@x.com"], subject="草稿", body="正文")
    )
    ref = decode_id(result["draft_id"])
    assert ref.folder == DRAFTS and result["folder"] == "草稿箱"
    stored = imap_server.mailbox(DRAFTS).by_uid(ref.uid)
    assert stored is not None and stored.flags == {"\\Draft", "\\Seen"}
    delete_draft(session, ref)
    assert [m.uid for m in imap_server.mailbox(DRAFTS).messages] == [other]
    with pytest.raises(InvalidArgument):
        delete_draft(session, MessageRef(ME, INBOX, 1001, 1))
    session.close()


def test_ensure_sent_copy_appends_only_when_missing(imap_server: FakeImapServer) -> None:
    session = open_session(ME, PASSWORD, factory=imap_server.connect)
    msg = compose(from_addr=ME, to=["a@x.com"], subject="s", body="b").message
    raw = msg.as_bytes()
    assert ensure_sent_copy(session, raw, msg["Message-ID"], sleep=lambda s: None) == "appended"
    assert ensure_sent_copy(session, raw, msg["Message-ID"], sleep=lambda s: None) == (
        "stored_by_server"
    )
    session.close()
    assert len(imap_server.mailbox(SENT).messages) == 1


def test_smtp_fallback_and_auth_error() -> None:
    recorder = SmtpRecorder(fail_ports=(465,))
    _client, port = smtp_login(ME, PASSWORD, recorder)
    assert port == 25 and recorder.connects[-1][2] is False
    with pytest.raises(AuthError) as err:
        smtp_login(ME, "WRONG", SmtpRecorder())
    assert "授权码" in str(err.value)
    with pytest.raises(ConnectionFailed):
        smtp_login(ME, PASSWORD, SmtpRecorder(fail_ports=(465, 25)))
    assert wait_until(lambda: True)


def test_wait_for_message_reconnects_and_keeps_the_baseline(imap_server: FakeImapServer) -> None:
    session = open_session(ME, PASSWORD, factory=imap_server.connect)
    imap_server.deliver(INBOX, code_mail("111111"))  # before the call: must not be returned
    reconnects: list[int] = []

    def reconnect():
        reconnects.append(1)
        return open_session(ME, PASSWORD, factory=imap_server.connect)

    naps = [0]

    def drop_then_deliver(seconds: float) -> None:
        naps[0] += 1
        if naps[0] == 1:
            imap_server.drop_connections()
            imap_server.deliver(INBOX, code_mail("135790"))  # arrives while disconnected

    result = wait_for_message(
        session,
        session.resolve_folder("INBOX"),
        Filters.build(subject="验证码"),
        timeout_s=10,
        poll_s=1,
        sleep=drop_then_deliver,
        reconnect=reconnect,
    )
    session.close()
    assert result["found"] is True and result["codes"] == ["135790"]
    assert result["reconnects"] == 1 and len(reconnects) == 1


def test_wait_for_message_returns_partial_result_when_reconnect_fails(
    imap_server: FakeImapServer,
) -> None:
    session = open_session(ME, PASSWORD, factory=imap_server.connect)
    baseline = imap_server.mailbox(INBOX).uidnext
    attempts: list[int] = []

    def reconnect():
        attempts.append(1)
        raise ConnectionFailed("down", hint="down")

    result = wait_for_message(
        session,
        session.resolve_folder("INBOX"),
        Filters.build(),
        timeout_s=10,
        poll_s=1,
        sleep=lambda s: imap_server.drop_connections(),
        reconnect=reconnect,
    )
    session.close()
    assert result["found"] is False and result["interrupted"] is True
    assert result["baseline_uidnext"] == baseline and "connection_failed" in result["reason"]
    assert len(attempts) == actions.RECONNECT_ATTEMPTS


def test_no_mutation_without_move_or_uidplus() -> None:
    server = FakeImapServer(
        user=ME, password=PASSWORD, capabilities=("IMAP4rev1", "ID", "SPECIAL-USE")
    )
    uid = server.deliver(INBOX, simple(subject="stay"))
    draft = server.deliver(DRAFTS, simple(subject="draft"), flags=("\\Draft",))
    session = open_session(ME, PASSWORD, factory=server.connect)
    with pytest.raises(ProtocolError, match="UIDPLUS"):
        move_messages(session, [MessageRef(ME, INBOX, 1001, uid)], session.resolve_folder("trash"))
    with pytest.raises(ProtocolError, match="UIDPLUS"):
        delete_draft(session, MessageRef(ME, DRAFTS, 1002, draft))
    session.close()
    assert not any(c.startswith(("UID COPY", "UID STORE")) for c in server.commands("UID"))
    assert server.mailbox(INBOX).messages[0].flags == set()
    assert server.mailbox(DRAFTS).messages[0].flags == {"\\Draft"}
    assert not server.mailbox(TRASH).messages


def test_open_session_wraps_network_errors_and_closes(imap_server, monkeypatch) -> None:
    closed: list[str] = []

    def factory(host: str, port: int, timeout: float):
        conn = imap_server.connect(host, port, timeout)
        original = conn.shutdown

        def shutdown() -> None:
            closed.append("x")
            original()

        conn.shutdown = shutdown  # type: ignore[method-assign]
        return conn

    def raising(exc: BaseException):
        def fn(*args, **kwargs):
            raise exc

        return fn

    cases = [
        (TimeoutError("slow"), MailTimeout),
        (ConnectionResetError("reset"), ConnectionFailed),
        (imaplib.IMAP4.abort("EOF"), ConnectionFailed),
    ]
    for exc, kind in cases:
        monkeypatch.setattr(imap, "send_id", raising(exc))
        with pytest.raises(kind):
            open_session(ME, PASSWORD, factory=factory)
    monkeypatch.undo()
    monkeypatch.setattr(imaplib.IMAP4, "login", raising(TimeoutError("slow login")))
    with pytest.raises(MailTimeout):
        open_session(ME, PASSWORD, factory=factory)
    assert len(closed) == 4


def test_starttls_failure_closes_the_smtp_socket(monkeypatch) -> None:
    closed: list[bool] = []

    class Client:
        def __init__(self, host: str, port: int, timeout: float) -> None:
            pass

        def ehlo(self) -> None:
            pass

        def starttls(self, context=None) -> None:
            raise smtplib.SMTPNotSupportedError("STARTTLS extension not supported")

        def close(self) -> None:
            closed.append(True)

    monkeypatch.setattr(actions.smtplib, "SMTP", Client)
    with pytest.raises(smtplib.SMTPNotSupportedError):
        default_smtp_factory("smtp.163.com", 25, False, 5)
    assert closed == [True]


def test_folder_created_after_listing_is_found(imap_server: FakeImapServer, pool) -> None:
    with pool.session(ME) as session:
        session.resolve_folder("INBOX")
    imap_server.add_mailbox("Projects")
    with pool.session(ME) as session:
        assert session.resolve_folder("Projects").raw == "Projects"
        with pytest.raises(NotFoundError):
            session.resolve_folder("Nope")
    assert len(imap_server.commands("LIST")) == 2  # one re-read, not one per lookup
