from __future__ import annotations

import pytest

from netease_mail_mcp.errors import AuthError, InvalidArgument, ProtocolError, StaleIdError
from netease_mail_mcp.ids import MessageRef
from netease_mail_mcp.imap import (
    ConnectionPool,
    decode_mutf7,
    encode_mutf7,
    fetch_body,
    fetch_flags,
    open_session,
    parse_fetch,
    parse_list,
    quote,
    uid_set,
)

from .conftest import PASSWORD, MemoryBackend
from .fakeimap import DRAFTS, INBOX, SENT, FakeImapServer
from .samples import ME, nested_with_attachments, simple


@pytest.mark.parametrize(
    ("text", "raw"),
    [
        ("已发送", "&XfJT0ZAB-"),
        ("草稿箱", "&g0l6P3ux-"),
        ("病毒文件夹", "&dcVr0mWHTvZZOQ-"),
        ("账户账号yeah", "&jSZiN40mU,c-yeah"),
        ("A&B", "A&-B"),
        ("INBOX", "INBOX"),
    ],
)
def test_mutf7_round_trip(text: str, raw: str) -> None:
    assert encode_mutf7(text) == raw
    assert decode_mutf7(raw) == text


def test_mutf7_emoji_and_malformed() -> None:
    assert decode_mutf7(encode_mutf7("项目🚀/2026")) == "项目🚀/2026"
    assert decode_mutf7("&broken") == "&broken"


def test_parse_list_quoted_literal_nil_and_flags() -> None:
    data = [
        b'(\\HasNoChildren) "/" "INBOX"',
        b'(\\HasNoChildren \\Drafts) "/" "&g0l6P3ux-"',
        (b'(\\HasNoChildren \\Sent) "/" {10}', b"&XfJT0ZAB-"),
        b"",
        b'(\\Noselect \\HasChildren) NIL "Work"',
        b'(\\HasNoChildren) "/" "&XfJT0ZAB-/a \\"q\\""',
        b'(\\HasNoChildren) "/" &V4NXPpCuTvY-',
    ]
    folders = parse_list(data)
    assert [f.name for f in folders] == [
        "INBOX",
        "草稿箱",
        "已发送",
        "Work",
        '已发送/a "q"',
        "垃圾邮件",
    ]
    assert [f.role for f in folders] == ["inbox", "drafts", "sent", None, None, "junk"]
    assert folders[3].selectable is False and folders[3].delimiter is None


def test_parse_fetch_with_several_literals_and_trailing_items() -> None:
    data = [
        (b'1 (UID 7 BODYSTRUCTURE ("APPLICATION" "PDF" ("NAME" {8}', "合同.pdf".encode()[:8]),
        (
            b') NIL NIL "BASE64" 10 NIL NIL NIL) BODY[HEADER.FIELDS (FROM SUBJECT)] {20}',
            b"Subject: hi\r\n\r\n    ",
        ),
        b" FLAGS (\\Seen \\Flagged))",
        b"2 (UID 9 FLAGS () RFC822.SIZE 55)",
    ]
    records = parse_fetch(data)
    assert [r["UID"] for r in records] == [7, 9]
    assert fetch_flags(records[0]) == ["\\Seen", "\\Flagged"]
    assert fetch_body(records[0]) == b"Subject: hi\r\n\r\n    "
    assert records[1]["RFC822.SIZE"] == "55"


def test_uid_set_and_quote() -> None:
    assert uid_set([5, 1, 2, 3, 9, 10, 7]) == "1:3,5,7,9:10"
    assert quote('a"b\\c') == '"a\\"b\\\\c"'
    for bad in ("x\r\nA1 DELETE INBOX", "nul\x00", "中文"):
        with pytest.raises(InvalidArgument):
            quote(bad)


def test_id_is_sent_before_examine(imap_server: FakeImapServer) -> None:
    session = open_session(ME, PASSWORD, factory=imap_server.connect)
    session.open_folder(session.resolve_folder("INBOX"))
    session.close()
    verbs = [c.split(" ", 1)[0] for c in imap_server.commands()]
    assert verbs.index("ID") < verbs.index("EXAMINE")
    assert verbs.index("LOGIN") < verbs.index("ID")
    assert session.id_accepted


def test_unsafe_login_without_id_explains(imap_server: FakeImapServer, monkeypatch) -> None:
    import netease_mail_mcp.imap as imap_mod

    monkeypatch.setattr(imap_mod, "send_id", lambda conn: False)
    session = open_session(ME, PASSWORD, factory=imap_server.connect)
    with pytest.raises(ProtocolError) as err:
        session.open_folder(session.resolve_folder("INBOX"))
    assert "Unsafe Login" in str(err.value) and "IMAP ID" in str(err.value)
    session.close()


def test_login_failure_points_to_auth_code(imap_server: FakeImapServer) -> None:
    with pytest.raises(AuthError) as err:
        open_session(ME, "WRONGCODE", factory=imap_server.connect)
    assert "授权码" in str(err.value) and "revoked" in str(err.value)


def test_capabilities_refreshed_after_login() -> None:
    server = FakeImapServer(user=ME, password=PASSWORD, post_login_extra=("MOVE", "IDLE"))
    session = open_session(ME, PASSWORD, factory=server.connect)
    assert session.has("MOVE") and session.has("IDLE")
    session.close()


def test_literal_folder_names_resolve_by_name_and_role() -> None:
    server = FakeImapServer(user=ME, password=PASSWORD, literal_names=True)
    session = open_session(ME, PASSWORD, factory=server.connect)
    assert session.resolve_folder("已发送").raw == SENT
    assert session.resolve_folder("drafts").raw == DRAFTS
    assert session.resolve_folder("\\Sent").raw == SENT
    assert session.resolve_folder("inbox").raw == INBOX
    session.close()


def test_fetch_parsing_against_server_literals_in_bodystructure() -> None:
    server = FakeImapServer(user=ME, password=PASSWORD, flags_after_literal=True)
    server.deliver(INBOX, nested_with_attachments(), flags=("\\Seen",))
    session = open_session(ME, PASSWORD, factory=server.connect)
    state = session.open_folder(session.resolve_folder("INBOX"))
    records = session.uid_fetch([1], "(UID FLAGS BODYSTRUCTURE BODY.PEEK[HEADER.FIELDS (SUBJECT)])")
    assert records[0]["UID"] == 1 and fetch_flags(records[0]) == ["\\Seen"]
    assert b"Report with files" in (fetch_body(records[0]) or b"")
    assert isinstance(records[0]["BODYSTRUCTURE"], list)
    assert state.uidvalidity == 1001
    session.close()


def test_stale_uidvalidity_rejected(imap_server: FakeImapServer) -> None:
    imap_server.deliver(INBOX, simple())
    session = open_session(ME, PASSWORD, factory=imap_server.connect)
    with pytest.raises(StaleIdError):
        session.open_ref(MessageRef(ME, INBOX, 999, 1))
    session.close()


def test_pool_reuses_connection_and_expires(imap_server: FakeImapServer) -> None:
    from netease_mail_mcp.credentials import CredentialStore

    now = [0.0]
    store = CredentialStore(MemoryBackend({ME: PASSWORD}), environ={})
    pool = ConnectionPool(store, factory=imap_server.connect, idle_ttl=60, clock=lambda: now[0])
    with pool.session(ME) as s1:
        s1.folders()
    with pool.session(ME) as s2:
        assert s2 is s1
    assert imap_server.connections == 1
    assert imap_server.commands("NOOP")  # liveness check before reuse
    now[0] = 120.0
    with pool.session(ME) as s3:
        assert s3 is not s1
    assert imap_server.connections == 2
    pool.close_all()
    assert imap_server.commands("LOGOUT")
