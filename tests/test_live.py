"""Opt-in live tests against real NetEase accounts: ``pytest -m live``.

Accounts come from ``NETEASE_MAIL_LIVE_ACCOUNTS`` (comma list); credentials from the system
keychain (or ``NETEASE_MAIL_AUTH_CODE__...``). These tests only read, plus create and then
delete their own draft (exact UID). They never send mail and never touch real messages.
"""

from __future__ import annotations

import os
import time

import pytest

from netease_mail_mcp.actions import append_draft, delete_draft
from netease_mail_mcp.credentials import CredentialStore
from netease_mail_mcp.ids import decode_id
from netease_mail_mcp.imap import ConnectionPool
from netease_mail_mcp.mime import compose
from netease_mail_mcp.reading import list_folders, read_message
from netease_mail_mcp.search import Filters, resolve_search_folders, search

pytestmark = pytest.mark.live

ACCOUNTS = [
    a.strip() for a in os.environ.get("NETEASE_MAIL_LIVE_ACCOUNTS", "").split(",") if a.strip()
]


@pytest.fixture(scope="module")
def pool():
    if not ACCOUNTS:
        pytest.skip("set NETEASE_MAIL_LIVE_ACCOUNTS to run live tests")
    p = ConnectionPool(CredentialStore())
    yield p
    p.close_all()


@pytest.mark.parametrize("account", ACCOUNTS or ["-"])
def test_login_id_and_special_folders(pool, account: str) -> None:
    with pool.session(account) as session:
        assert session.id_accepted
        roles = {f.role for f in session.folders()}
        assert {"inbox", "drafts", "sent", "trash", "junk"} <= roles
        state = session.open_folder(session.resolve_folder("INBOX"))
        assert state.uidvalidity > 0


@pytest.mark.parametrize("account", ACCOUNTS or ["-"])
def test_server_flag_criteria_match_client_flags(pool, account: str) -> None:
    with pool.session(account) as session:
        session.open_folder(session.resolve_folder("INBOX"))
        uids = session.uid_search(["ALL"])[-300:]
        if not uids:
            pytest.skip("empty INBOX")
        records = session.uid_fetch(uids, "(UID FLAGS)")
        seen = {r["UID"] for r in records if "\\seen" in {f.lower() for f in r.get("FLAGS", [])}}
        flagged = {
            r["UID"] for r in records if "\\flagged" in {f.lower() for f in r.get("FLAGS", [])}
        }
        window = ["UID", f"{uids[0]}:{uids[-1]}"]
        assert set(session.uid_search([*window, "SEEN"])) == seen
        assert set(session.uid_search([*window, "UNSEEN"])) == set(uids) - seen
        assert set(session.uid_search([*window, "FLAGGED"])) == flagged


@pytest.mark.parametrize("account", ACCOUNTS or ["-"])
def test_search_and_read_newest(pool, account: str) -> None:
    with pool.session(account) as session:
        folders = resolve_search_folders(session, "INBOX")
        result = search(session, folders, Filters.build(), limit=3, max_scan=500)
        for message in result["messages"]:
            ref = decode_id(message["id"])
            read = read_message(session, ref, max_chars=2000, include_html=False, mark_read=False)
            assert read["unread"] == message["unread"]  # BODY.PEEK leaves flags alone
        assert list_folders(session, with_counts=False)["folders"]


@pytest.mark.parametrize("account", ACCOUNTS or ["-"])
def test_draft_round_trip(pool, account: str) -> None:
    subject = f"[netease-mail-mcp live test {int(time.time())}] please ignore"
    with pool.session(account) as session:
        composed = compose(from_addr=account, to=[account], subject=subject, body="live test")
        draft = append_draft(session, composed)
        ref = decode_id(draft["draft_id"])
        try:
            back = read_message(session, ref, max_chars=500, include_html=False, mark_read=False)
            assert back["subject"] == subject
        finally:
            delete_draft(session, ref)
        for _ in range(5):  # NetEase may need a moment before the expunge is visible
            session.open_folder(session.folder_by_raw(ref.folder), force=True)
            if ref.uid not in session.uid_search(["ALL"]):
                break
            time.sleep(1)
        else:
            pytest.fail("draft still visible after delete")
