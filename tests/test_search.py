from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from netease_mail_mcp.errors import InvalidArgument
from netease_mail_mcp.imap import ConnectionPool
from netease_mail_mcp.search import Filters, resolve_search_folders, search

from .fakeimap import ADS, INBOX, JUNK, SENT, TRASH, VIRUS, FakeImapServer
from .samples import ME, gbk_plain, nested_with_attachments, simple

BASE = datetime(2026, 10, 1, tzinfo=timezone.utc)


def seed(server: FakeImapServer, count: int, *, every: int = 0, subject: str = "Invoice") -> None:
    for i in range(count):
        subj = f"{subject} {i}" if every and i % every == 0 else f"Note {i}"
        server.deliver(INBOX, simple(subject=subj, body=f"b{i}"), when=BASE + timedelta(hours=i))


def run(pool: ConnectionPool, folder="INBOX", limit=20, max_scan=500, **filters):
    with pool.session(ME) as session:
        folders = resolve_search_folders(session, folder)
        return search(session, folders, Filters.build(**filters), limit=limit, max_scan=max_scan)


def test_server_side_only_fetches_newest_limit(imap_server: FakeImapServer, pool) -> None:
    seed(imap_server, 120)
    result = run(pool, limit=5)
    assert result["returned"] == 5 and result["total_candidates"] == 120
    assert result["scanned"] == 5 and result["truncated"] is False
    assert result["more_available"] is True and result["body_searched"] is False
    assert [m["subject"] for m in result["messages"]] == [f"Note {i}" for i in range(119, 114, -1)]


def test_keyword_is_client_side_never_server_search(imap_server: FakeImapServer, pool) -> None:
    seed(imap_server, 30, every=10)
    result = run(pool, query="invoice")
    assert [m["subject"] for m in result["messages"]] == ["Invoice 20", "Invoice 10", "Invoice 0"]
    searches = [c for c in imap_server.commands("UID") if c.startswith("UID SEARCH")]
    assert all("SUBJECT" not in c and "FROM" not in c and "TEXT" not in c for c in searches)


def test_batching_truncation_and_newest_first(imap_server: FakeImapServer, pool) -> None:
    seed(imap_server, 450, every=100)
    result = run(pool, query="invoice", max_scan=300)
    assert result["scanned"] == 300 and result["truncated"] is True
    assert [m["subject"] for m in result["messages"]] == [
        "Invoice 400",
        "Invoice 300",
        "Invoice 200",
    ]
    fetches = [c for c in imap_server.commands("UID") if c.startswith("UID FETCH")]
    assert len(fetches) == 2  # 200 + 100 in UID-set batches, not one FETCH per message
    full = run(pool, query="invoice", max_scan=5000)
    assert full["truncated"] is False and full["returned"] == 5


def test_limit_stops_scanning_early(imap_server: FakeImapServer, pool) -> None:
    seed(imap_server, 600, every=1)
    result = run(pool, query="invoice", limit=3, max_scan=600)
    assert result["returned"] == 3 and result["scanned"] == 200 and result["truncated"] is False


def test_all_terms_and_chinese_headers(imap_server: FakeImapServer, pool) -> None:
    imap_server.deliver(INBOX, gbk_plain())
    imap_server.deliver(INBOX, simple(subject="月报 draft", sender="李四 <lisi@x.cn>"))
    assert run(pool, query="张三 报表")["returned"] == 1
    assert run(pool, query="张三 draft")["returned"] == 0
    assert run(pool, **{"sender": "lisi"})["messages"][0]["subject"] == "月报 draft"
    assert run(pool, recipient=ME)["returned"] == 2


def test_flags_dates_and_attachments(imap_server: FakeImapServer, pool) -> None:
    imap_server.deliver(INBOX, simple(subject="old"), flags=("\\Seen",), when=BASE)
    imap_server.deliver(INBOX, simple(subject="new"), when=BASE + timedelta(days=10))
    imap_server.deliver(
        INBOX,
        nested_with_attachments(),
        flags=("\\Flagged", "\\Seen"),
        when=BASE + timedelta(days=11),
    )
    assert [m["subject"] for m in run(pool, unread=True)["messages"]] == ["new"]
    assert [m["subject"] for m in run(pool, flagged=True)["messages"]] == ["Report with files"]
    assert [m["subject"] for m in run(pool, before="2026-10-05")["messages"]] == ["old"]
    assert run(pool, since="2026-10-05")["returned"] == 2
    attached = run(pool, has_attachment=True)
    assert [m["subject"] for m in attached["messages"]] == ["Report with files"]
    assert attached["messages"][0]["has_attachment"] is True
    with pytest.raises(InvalidArgument):
        Filters.build(since="2026/10/01")
    with pytest.raises(InvalidArgument):
        Filters.build(since="2026-10-05", before="2026-10-01")


def test_all_folders_excludes_junk_trash_virus_and_merges_by_date(imap_server, pool) -> None:
    imap_server.deliver(INBOX, simple(subject="inbox"), when=BASE)
    imap_server.deliver(SENT, simple(subject="sent"), when=BASE + timedelta(days=2))
    imap_server.deliver(ADS, simple(subject="ads"), when=BASE + timedelta(days=1))
    for box in (JUNK, TRASH, VIRUS):
        imap_server.deliver(box, simple(subject="hidden"), when=BASE + timedelta(days=3))
    result = run(pool, folder="all", query="")
    assert [m["subject"] for m in result["messages"]] == ["sent", "ads", "inbox"]
    assert "垃圾邮件" not in result["folders"] and "已删除" not in result["folders"]
    explicit = run(pool, folder=["垃圾邮件", "INBOX"])
    assert {m["subject"] for m in explicit["messages"]} == {"hidden", "inbox"}


def test_empty_result_mentions_receive_window(pool) -> None:
    result = run(pool, query="x")
    assert result["returned"] == 0 and any("收取选项" in n for n in result["notes"])


def test_from_filter_can_ignore_display_names(imap_server: FakeImapServer, pool) -> None:
    imap_server.deliver(INBOX, simple(sender='"noreply@github.com" <phish@evil.example>'))
    imap_server.deliver(INBOX, simple(sender="GitHub <noreply@github.com>", subject="real"))
    imap_server.deliver(INBOX, simple(sender="Bot <noreply@github.com.evil.example>"))
    imap_server.deliver(INBOX, simple(sender="Ops <ops@mail.github.com>", subject="sub"))
    strict = run(pool, sender="github.com", sender_address_only=True)
    assert sorted(m["subject"] for m in strict["messages"]) == ["Hello", "real", "sub"]
    exact = run(pool, sender="NoReply@GitHub.com", sender_address_only=True)
    assert [m["subject"] for m in exact["messages"]] == ["real"]  # full address: equal only
    domain = run(pool, sender="@github.com", sender_address_only=True)
    assert sorted(m["subject"] for m in domain["messages"]) == ["real", "sub"]
    assert run(pool, sender="github.com")["returned"] == 4  # search_messages: name or address


def test_exact_sender_forms_reject_lookalike_addresses() -> None:
    from netease_mail_mcp.search import _address_matches, _norm

    for wanted in ("noreply@github.com", "@github.com"):
        assert _address_matches("NoReply@GitHub.com", _norm(wanted))
        for fake in ("noreply@\uff47\uff49\uff54\uff48\uff55\uff42.com", "\uff2eoreply@github.com"):
            assert not _address_matches(fake, _norm(wanted))  # full-width look-alikes
    assert _address_matches("ops@mail.github.com", _norm("@github.com"))
    assert not _address_matches("noreply@github.com.evil.example", _norm("noreply@github.com"))
