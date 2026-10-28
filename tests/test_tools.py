"""End-to-end through a real MCP client session (in-memory transport)."""

from __future__ import annotations

import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest
from mcp import types

from netease_mail_mcp.ids import decode_id, encode_id
from netease_mail_mcp.mime import parse_message

from .conftest import ToolCallError, connect, make_services
from .fakeimap import DRAFTS, INBOX, SENT, TRASH, FakeImapServer
from .samples import ME, nested_with_attachments, simple

pytestmark = pytest.mark.anyio

READ_TOOLS = {
    "list_accounts",
    "list_folders",
    "search_messages",
    "read_message",
    "get_thread",
    "download_attachment",
    "wait_for_message",
}


@pytest.mark.parametrize(
    ("mode", "expected"),
    [
        ("readonly", READ_TOOLS),
        ("draft", READ_TOOLS | {"create_draft"}),
        ("full", READ_TOOLS | {"create_draft", "send_email", "manage_messages"}),
    ],
)
async def test_mode_gating_at_registration(pool, store, smtp, tmp_path, mode, expected) -> None:
    services = make_services(pool, store, smtp, tmp_path, mode=mode)
    async with connect(services) as client:
        assert set(await client.names()) == expected
        if mode == "readonly":
            result = await client.session.call_tool("create_draft", {"to": "a@x.com"})
            assert result.isError


async def test_search_and_read_keeps_unread_and_wraps_body(imap_server, services) -> None:
    imap_server.deliver(INBOX, nested_with_attachments())
    async with connect(services) as client:
        found = await client.call("search_messages", **{"from": "carol", "query": "report"})
        assert found["returned"] == 1 and found["untrusted_content"] is True
        msg = found["messages"][0]
        assert msg["has_attachment"] is True and msg["unread"] is True
        read = await client.call("read_message", id=msg["id"])
    assert (
        read["body"].startswith("<<<EMAIL_CONTENT id=") and "See attached report." in read["body"]
    )
    assert [a["filename"] for a in read["attachments"]] == [
        "attachment-0.png",
        "报表.csv",
        "合同.pdf",
    ]
    assert read["unread"] is True and imap_server.mailbox(INBOX).messages[0].flags == set()
    assert all("BODY[]" not in c or "PEEK" in c for c in imap_server.commands("UID"))


async def test_read_rejects_stale_and_foreign_ids(imap_server, services) -> None:
    imap_server.deliver(INBOX, simple())
    async with connect(services) as client:
        with pytest.raises(ToolCallError, match="stale_id"):
            await client.call("read_message", id=encode_id(ME, INBOX, 1, 1))
        with pytest.raises(ToolCallError, match="config_error"):
            await client.call("read_message", id=encode_id("x@126.com", INBOX, 1001, 1))
        with pytest.raises(ToolCallError, match="invalid_id"):
            await client.call("read_message", id="nonsense")


async def test_download_attachment_no_overwrite_and_excerpt(
    imap_server, services, tmp_path
) -> None:
    imap_server.deliver(INBOX, nested_with_attachments())
    async with connect(services) as client:
        msg_id = (await client.call("search_messages"))["messages"][0]["id"]
        first = await client.call("download_attachment", id=msg_id, filename="报表.csv")
        second = await client.call("download_attachment", id=msg_id, index=1)
        with pytest.raises(ToolCallError, match="not_found"):
            await client.call("download_attachment", id=msg_id, filename="nope.txt")
    assert Path(first["path"]).name == "报表.csv" and Path(second["path"]).name == "报表 (1).csv"
    assert Path(first["path"]).parent == tmp_path / "dl"
    assert "苹果,3" in first["text_excerpt"] and first["sha256"] == second["sha256"]


async def test_download_is_confined_to_the_download_dir(imap_server, services, tmp_path) -> None:
    imap_server.deliver(INBOX, nested_with_attachments())
    dl = tmp_path / "dl"
    dl.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    escapes = [str(outside), "../outside", str(dl / ".." / "outside"), "\\\\host\\share", "C:x"]
    try:
        (dl / "link").symlink_to(outside, target_is_directory=True)
        escapes.append("link")
    except OSError:
        pass  # no symlink privilege (Windows)
    async with connect(services) as client:
        msg_id = (await client.call("search_messages"))["messages"][0]["id"]
        for bad in escapes:
            with pytest.raises(ToolCallError, match="invalid_argument"):
                await client.call("download_attachment", id=msg_id, index=1, dest_dir=bad)
        sub = await client.call("download_attachment", id=msg_id, index=1, dest_dir="发票/2026")
        inside = await client.call(
            "download_attachment", id=msg_id, index=1, dest_dir=str(dl / "a")
        )
    assert not any(outside.iterdir())
    assert Path(sub["path"]).parent == (dl / "发票" / "2026").resolve()
    assert Path(inside["path"]).parent == (dl / "a").resolve()
    if os.name != "nt":
        assert stat.S_IMODE(os.stat(sub["path"]).st_mode) == 0o600
        assert stat.S_IMODE(os.stat(dl / "发票").st_mode) == 0o700


async def test_download_skips_dangling_symlink_and_marks_file(
    imap_server, services, tmp_path, monkeypatch
) -> None:
    from netease_mail_mcp import reading

    imap_server.deliver(INBOX, nested_with_attachments())
    dl = tmp_path / "dl"
    dl.mkdir()
    try:
        (dl / "报表.csv").symlink_to(tmp_path / "target-that-does-not-exist")
    except OSError:
        pytest.skip("symlinks are not available")
    async with connect(services) as client:
        msg_id = (await client.call("search_messages"))["messages"][0]["id"]
        saved = await client.call("download_attachment", id=msg_id, filename="报表.csv")

        def broken(path: Path) -> str:
            raise OSError("xattr not supported")

        monkeypatch.setattr(reading, "_quarantine_macos", broken)
        monkeypatch.setattr(reading, "_zone_identifier", broken)
        unmarked = await client.call("download_attachment", id=msg_id, filename="报表.csv")
    assert Path(saved["path"]).name == "报表 (1).csv"
    assert not (tmp_path / "target-that-does-not-exist").exists()
    assert unmarked["quarantine"] is None and Path(unmarked["path"]).name == "报表 (2).csv"
    if sys.platform == "darwin":
        assert saved["quarantine"] == "com.apple.quarantine"
        check = subprocess.run(
            ["/usr/bin/xattr", "-p", "com.apple.quarantine", saved["path"]],
            capture_output=True,
            text=True,
        )
        assert check.stdout.startswith("0081;")


async def test_download_excerpt_truncation_counts_gbk_characters(imap_server, services) -> None:
    from email.message import EmailMessage

    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = "a@x.com", ME, "gbk"
    msg.set_content("see attachment")
    for name, count in (("long.csv", 5000), ("short.csv", 3000)):
        msg.add_attachment(
            ("报" * count).encode("gbk"), maintype="text", subtype="csv", filename=name
        )
    imap_server.deliver(INBOX, msg.as_bytes())
    async with connect(services) as client:
        msg_id = (await client.call("search_messages"))["messages"][0]["id"]
        long = await client.call("download_attachment", id=msg_id, filename="long.csv")
        short = await client.call("download_attachment", id=msg_id, filename="short.csv")
    assert long["text_excerpt_truncated"] is True and short["text_excerpt_truncated"] is False


async def test_get_thread_links_inbox_and_sent(imap_server, services) -> None:
    imap_server.deliver(
        INBOX,
        simple(
            subject="Plan",
            message_id="<a@x>",
            sender="bob@x.com",
            date="Mon, 26 Oct 2026 09:00:00 +0800",
        ),
    )
    imap_server.deliver(
        SENT,
        simple(
            subject="Re: Plan",
            message_id="<b@x>",
            sender=ME,
            to="bob@x.com",
            in_reply_to="<a@x>",
            references="<a@x>",
            date="Mon, 26 Oct 2026 10:00:00 +0800",
        ),
    )
    imap_server.deliver(
        INBOX,
        simple(
            subject="Re: Plan",
            message_id="<c@x>",
            sender="bob@x.com",
            in_reply_to="<b@x>",
            references="<a@x> <b@x>",
            date="Mon, 26 Oct 2026 11:00:00 +0800",
        ),
    )
    imap_server.deliver(INBOX, simple(subject="Unrelated", message_id="<z@x>"))
    async with connect(services) as client:
        found = await client.call("search_messages", subject="unrelated")
        assert found["returned"] == 1
        first = (await client.call("search_messages", query="plan"))["messages"][-1]
        thread = await client.call("get_thread", id=first["id"])
    assert [m["subject"] for m in thread["messages"]] == ["Plan", "Re: Plan", "Re: Plan"]
    assert [m["folder"] for m in thread["messages"]] == ["INBOX", "已发送", "INBOX"]
    assert thread["messages"][0]["snippet"].startswith("<<<EMAIL_CONTENT")


async def test_create_reply_all_draft(imap_server, services) -> None:
    imap_server.deliver(
        INBOX,
        simple(
            subject="Re: Budget",
            message_id="<m1@x>",
            references="<m0@x>",
            sender="Alice <alice@x.com>",
            to=f"{ME}, bob@x.com",
            cc="carol@x.com",
            body="原文内容",
        ),
    )
    async with connect(services) as client:
        msg_id = (await client.call("search_messages"))["messages"][0]["id"]
        draft = await client.call("create_draft", reply_to_id=msg_id, reply_all=True, body="好的")
    assert draft["folder"] == "草稿箱" and decode_id(draft["draft_id"]).folder == DRAFTS
    raw = imap_server.mailbox(DRAFTS).messages[0].raw
    parsed = parse_message(raw)
    assert parsed.subject == "Re: Budget"
    assert parsed.in_reply_to == "<m1@x>" and parsed.references == ["<m0@x>", "<m1@x>"]
    assert [a for _, a in parsed.to] == ["alice@x.com"]
    assert [a for _, a in parsed.cc] == ["bob@x.com", "carol@x.com"]
    assert parsed.text.startswith("好的") and "> 原文内容" in parsed.text


async def test_send_email_token_flow(imap_server, services, smtp, tmp_path) -> None:
    attachment = tmp_path / "a.txt"
    attachment.write_text("data")
    args = {
        "to": "bob@x.com",
        "subject": "Hi",
        "body": "Hello Bob",
        "bcc": "boss@x.com",
        "attachments": [str(attachment)],
    }
    async with connect(services) as client:
        preview = await client.call("send_email", **args)
        assert preview["status"] == "confirmation_required" and not smtp.sent
        filename = preview["preview"]["attachments"][0]["filename"]
        assert filename.startswith("<<<EMAIL_CONTENT") and "\na.txt\n" in filename
        token = preview["confirmation_token"]
        with pytest.raises(ToolCallError, match="confirmation_failed"):
            await client.call(
                "send_email", **{**args, "body": "Hello Bob!!"}, confirmation_token=token
            )
        attachment.write_text("changed")
        with pytest.raises(ToolCallError, match="confirmation_failed"):
            await client.call("send_email", **args, confirmation_token=token)
        attachment.write_text("data")
        sent = await client.call("send_email", **args, confirmation_token=token)
        with pytest.raises(ToolCallError, match="confirmation_failed"):
            await client.call("send_email", **args, confirmation_token=token)  # single use
    assert sent["sent"] is True and sent["sent_copy"] == "appended" and sent["smtp_port"] == 465
    assert len(smtp.sent) == 1 and smtp.sent[0]["to"] == ["bob@x.com", "boss@x.com"]
    copy = parse_message(imap_server.mailbox(SENT).messages[0].raw)
    assert copy.subject == "Hi" and copy.bcc == [("", "boss@x.com")]


async def test_send_saved_draft_and_delete_it(imap_server, services, smtp) -> None:
    async with connect(services) as client:
        draft = await client.call("create_draft", to="bob@x.com", subject="Draft", body="saved")
        preview = await client.call("send_email", draft_id=draft["draft_id"])
        assert "\nDraft\n" in preview["preview"]["subject"]
        with pytest.raises(ToolCallError, match="invalid_argument"):
            await client.call("send_email", draft_id=draft["draft_id"], body="x")
        sent = await client.call(
            "send_email",
            draft_id=draft["draft_id"],
            confirmation_token=preview["confirmation_token"],
        )
    assert sent["draft_deleted"] is True and not imap_server.mailbox(DRAFTS).messages
    assert smtp.sent[0]["to"] == ["bob@x.com"]


async def test_failures_after_smtp_accepted_are_warnings(
    imap_server, services, smtp, monkeypatch
) -> None:
    from netease_mail_mcp import server as server_module

    def unexpected(*args, **kwargs):
        raise ValueError("unexpected parser bug")

    def disk_error(*args, **kwargs):
        raise OSError("connection reset")

    async def send_draft(client) -> dict:
        draft = await client.call("create_draft", to="bob@x.com", subject="D", body="saved")
        preview = await client.call("send_email", draft_id=draft["draft_id"])
        return await client.call(
            "send_email",
            draft_id=draft["draft_id"],
            confirmation_token=preview["confirmation_token"],
        )

    async with connect(services) as client:
        with monkeypatch.context() as patch:
            patch.setattr(server_module, "ensure_sent_copy", unexpected)
            sent = await send_draft(client)
        assert sent["status"] == "sent" and sent["sent"] is True and len(smtp.sent) == 1
        # No copy in Sent: the draft is the only record of what went out, so it stays.
        assert sent["draft_deleted"] is False and len(imap_server.mailbox(DRAFTS).messages) == 1
        assert len(sent["warnings"]) == 3 and "unexpected parser bug" in sent["warnings"][0]
        assert "draft was kept" in sent["warnings"][1]
        assert "Do not send it again" in sent["warnings"][2]

        monkeypatch.setattr(server_module, "delete_draft", disk_error)
        sent = await send_draft(client)
    assert sent["status"] == "sent" and sent["sent_copy"] == "appended" and len(smtp.sent) == 2
    assert sent["draft_deleted"] is False and len(sent["warnings"]) == 2
    assert "connection reset" in sent["warnings"][0]


async def test_partially_refused_recipients_are_a_warning(imap_server, services, smtp) -> None:
    smtp.refuse = ("ghost@x.com",)
    args = {"to": "bob@x.com, ghost@x.com", "subject": "Hi", "body": "Hello"}
    async with connect(services) as client:
        preview = await client.call("send_email", **args)
        sent = await client.call(
            "send_email", **args, confirmation_token=preview["confirmation_token"]
        )
    assert sent["status"] == "sent" and list(sent["refused"]) == ["ghost@x.com"]
    assert "ghost@x.com" in sent["warnings"][0] and "these addresses only" in sent["warnings"][0]


async def test_elicitation_accept_and_decline(imap_server, pool, store, smtp, tmp_path) -> None:
    answers = []

    async def elicit(context, params: types.ElicitRequestParams):
        answers.append(params.message)
        approve = "Bob" in params.message
        return types.ElicitResult(action="accept", content={"approve": approve})

    services = make_services(pool, store, smtp, tmp_path, confirm="auto")
    async with connect(services, elicitation_callback=elicit) as client:
        sent = await client.call("send_email", to="bob@x.com", subject="Hi", body="Hello Bob")
        declined = await client.call("send_email", to="eve@x.com", subject="Hi", body="Hello Eve")
    assert sent["sent"] is True and declined["status"] == "declined_by_user"
    assert len(smtp.sent) == 1 and "bob@x.com" in answers[0]


async def test_send_preview_shows_full_content(pool, store, smtp, tmp_path) -> None:
    attachment = tmp_path / "报价.txt"
    attachment.write_text("x" * 2048)
    asked: list[str] = []

    async def decline(context, params: types.ElicitRequestParams):
        asked.append(params.message)
        return types.ElicitResult(action="decline")

    body = "开头" + "y" * 7998 + "结尾"  # 8002 chars: the last two are beyond the 8000 cap
    args = {
        "to": ["Zhang <z@x.com>", "b@x.com"],
        "cc": "c1@x.com, c2@x.com",
        "bcc": "hidden@x.com",
        "subject": "季度报价",
        "body": body,
        "html_body": '<p>Please pay at <a href="https://pay.evil.example/">our portal</a></p>',
        "attachments": [str(attachment)],
    }
    services = make_services(pool, store, smtp, tmp_path, confirm="auto")
    async with connect(services, elicitation_callback=decline) as client:
        declined = await client.call("send_email", **args)
    token_services = make_services(pool, store, smtp, tmp_path, confirm="token")
    async with connect(token_services) as client:
        token = await client.call("send_email", **args)
    text = asked[0]
    assert "Zhang <z@x.com>, b@x.com" in text and "c1@x.com, c2@x.com" in text
    assert "hidden@x.com" in text and "季度报价" in text
    assert "开头" in text and "结尾" not in text and "还有 2 个字符未显示" in text
    assert "https://pay.evil.example/" in text and "HTML 正文和纯文本正文内容不同" in text
    assert str(attachment.resolve()) in text and "2.0 KB" in text
    assert declined["status"] == "declined_by_user" and not smtp.sent
    preview = token["preview"]
    assert token["untrusted_content"] is True and "confirmation_token" in token
    assert preview["body"].startswith("<<<EMAIL_CONTENT") and preview["body_omitted_chars"] == 2
    assert "https://pay.evil.example/" in preview["html_text"]
    assert preview["html_differs_from_body"] is True
    cc = [c.split("\n")[1] for c in preview["cc"]]  # each fenced: names can be free text
    assert "\nhidden@x.com\n" in preview["bcc"][0] and cc == ["c1@x.com", "c2@x.com"]
    assert preview["attachments"][0]["path"] == str(attachment.resolve())
    assert preview["attachments"][0]["size"] == 2048


@pytest.mark.parametrize(
    "answer",
    [
        types.ErrorData(code=-32603, message="client UI crashed"),
        types.ElicitResult(action="accept", content={}),
        types.ElicitResult(action="accept", content={"approve": "yes"}),
        types.ElicitResult(action="cancel"),
    ],
)
async def test_failed_elicitation_is_a_decline_not_a_token(
    pool, store, smtp, tmp_path, answer
) -> None:
    async def elicit(context, params: types.ElicitRequestParams):
        return answer

    services = make_services(pool, store, smtp, tmp_path, confirm="auto")
    async with connect(services, elicitation_callback=elicit) as client:
        result = await client.call("send_email", to="bob@x.com", subject="Hi", body="x")
    assert result["status"] in ("declined_by_user", "not_confirmed") and result["sent"] is False
    assert "confirmation_token" not in result and not smtp.sent


async def test_send_draft_requires_a_real_draft(imap_server, services, smtp) -> None:
    from email.message import EmailMessage

    inbox_uid = imap_server.deliver(INBOX, simple(subject="from a stranger", to="victim@x.com"))
    unflagged = imap_server.deliver(DRAFTS, simple(subject="no flag", to="bob@x.com"))
    msg = EmailMessage()
    msg["From"] = "Someone Else <other@x.com>"
    msg["Reply-To"] = "collector@evil.example"
    msg["To"] = "bob@x.com"
    msg["Bcc"] = "hidden@x.com"
    msg["Subject"] = "webmail draft"
    msg.set_content("plain says hello")
    msg.add_alternative("<p>html says <b>pay now</b></p>", subtype="html")
    flagged = imap_server.deliver(DRAFTS, msg.as_bytes(), flags=("\\Draft",))
    async with connect(services) as client:
        for folder, uid, validity in ((INBOX, inbox_uid, 1001), (DRAFTS, unflagged, 1002)):
            with pytest.raises(ToolCallError, match="invalid_argument"):
                await client.call("send_email", draft_id=encode_id(ME, folder, validity, uid))
        result = await client.call("send_email", draft_id=encode_id(ME, DRAFTS, 1002, flagged))
    preview = result["preview"]
    assert preview["from"].startswith("<<<EMAIL_CONTENT")  # names in a draft are free text
    assert "\nSomeone Else <other@x.com>\n" in preview["from"]
    assert "\ncollector@evil.example\n" in preview["reply_to"]
    assert len(preview["bcc"]) == 1 and "\nhidden@x.com\n" in preview["bcc"][0]
    assert "plain says hello" in preview["body"] and "pay now" in preview["html_text"]
    assert preview["html_differs_from_body"] is True and len(preview["warnings"]) == 2
    assert result["untrusted_content"] is True and not smtp.sent


async def test_elicit_mode_requires_client_support(pool, store, smtp, tmp_path) -> None:
    services = make_services(pool, store, smtp, tmp_path, confirm="elicit")
    async with connect(services) as client:
        with pytest.raises(ToolCallError, match="elicitation"):
            await client.call("send_email", to="bob@x.com", subject="Hi", body="x")
    assert not smtp.sent


async def test_manage_messages_trash_two_phase_and_flags(imap_server, services) -> None:
    imap_server.deliver(INBOX, simple(subject="one"))
    imap_server.deliver(INBOX, simple(subject="two"))
    async with connect(services) as client:
        ids = [m["id"] for m in (await client.call("search_messages"))["messages"]]
        marked = await client.call("manage_messages", ids=ids, action="mark_read")
        assert marked["count"] == 2
        assert all("\\Seen" in m.flags for m in imap_server.mailbox(INBOX).messages)
        preview = await client.call("manage_messages", ids=ids[:1], action="trash")
        assert preview["status"] == "confirmation_required"
        subject = preview["preview"]["messages"][0]["subject"]
        assert subject.startswith("<<<EMAIL_CONTENT") and "\ntwo\n" in subject
        assert preview["untrusted_content"] is True
        assert len(imap_server.mailbox(INBOX).messages) == 2
        with pytest.raises(ToolCallError, match="confirmation_failed"):
            await client.call(
                "manage_messages",
                ids=ids,
                action="trash",
                confirmation_token=preview["confirmation_token"],
            )
        done = await client.call(
            "manage_messages",
            ids=ids[:1],
            action="trash",
            confirmation_token=preview["confirmation_token"],
        )
    assert done["done"] is True and done["target_folder"] == "已删除"
    assert [len(imap_server.mailbox(b).messages) for b in (INBOX, TRASH)] == [1, 1]


async def test_wait_for_message_tool_with_lookback(imap_server: FakeImapServer, services) -> None:
    from .samples import code_mail

    imap_server.deliver(INBOX, code_mail("552211"))
    async with connect(services) as client:
        result = await client.call(
            "wait_for_message", subject="验证码", timeout_s=1, lookback_s=300
        )
    assert result["found"] is True and result["codes"] == ["552211"]


async def test_wait_for_message_from_matches_addresses_only(imap_server, services) -> None:
    from .samples import code_mail

    forged = code_mail("666666").replace(
        b"Service <no-reply@service.example>", b'"no-reply@service.example" <x@evil.example>'
    )
    imap_server.deliver(INBOX, forged)
    imap_server.deliver(INBOX, code_mail("123456"))
    async with connect(services) as client:
        result = await client.call(
            "wait_for_message", **{"from": "service.example"}, timeout_s=1, lookback_s=300
        )
    assert result["found"] is True and result["codes"] == ["123456"]


async def test_list_accounts_and_folders(imap_server, services) -> None:
    imap_server.deliver(INBOX, simple(), flags=("\\Seen",))
    imap_server.deliver(INBOX, simple(subject="u"))
    async with connect(services) as client:
        accounts = await client.call("list_accounts", check=True)
        folders = await client.call("list_folders", with_counts=True)
    acct = accounts["accounts"][0]
    assert acct["credential_present"] and acct["check"]["inbox_messages"] == 2
    assert acct["check"]["inbox_unseen"] == 1 and accounts["mode"] == "full"
    roles = {f["name"]: f["role"] for f in folders["folders"]}
    assert roles["草稿箱"] == "drafts" and roles["病毒文件夹"] == "virus"
    assert next(f for f in folders["folders"] if f["name"] == "INBOX")["messages"] == 2
