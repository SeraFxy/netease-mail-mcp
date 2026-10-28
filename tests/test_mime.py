from __future__ import annotations

import os
from pathlib import Path

import pytest

from netease_mail_mcp.config import load_config
from netease_mail_mcp.errors import ConfigError, InvalidArgument, LimitError
from netease_mail_mcp.mime import (
    bodystructure_attachments,
    compose,
    decode_bytes,
    decode_excerpt,
    decode_header_value,
    forward_subject,
    html_to_text,
    load_attachment,
    normalize_recipients,
    numbered_names,
    parse_headers,
    parse_message,
    reply_recipients,
    reply_subject,
    reply_threading,
    safe_filename,
    thread_subject_key,
)

from .samples import (
    ME,
    alternative,
    gbk_plain,
    html_only,
    mislabelled,
    nested_with_attachments,
    rfc2047,
    simple,
)


def test_rfc2047_headers_gbk_and_utf8() -> None:
    assert decode_header_value(rfc2047("本月报表", "gbk")) == "本月报表"
    assert decode_header_value(rfc2047("你好", "utf-8") + " world") == "你好 world"
    # labelled gb2312 but uses a GBK-only character
    assert decode_header_value(rfc2047("镕", "gbk").replace("gbk", "gb2312")) == "镕"
    # labelled utf-8 but bytes are GBK
    assert decode_header_value(rfc2047("报表", "gbk").replace("gbk", "utf-8")) == "报表"


def test_folded_subject_keeps_space_after_encoded_word() -> None:
    # Regression: sent subject came back as "...发信实测2026-10-28 18:27:06".
    subject = "【测试】netease-mail-mcp 发信实测 2026-10-28 18:27:06"
    raw = (
        b"Subject: =?utf-8?b?44CQ5rWL6K+V44CRbmV0ZWFzZS1tYWlsLW1jcCDlj5Hkv6Hlrp7mtYs=?=\r\n"
        b" 2026-10-28 18:27:06\r\n\r\n"
    )
    assert parse_headers(raw).subject == subject
    assert decode_header_value("=?utf-8?b?5Y+R?=\n =?utf-8?b?5L+h?=\n x") == "发信 x"


def test_decode_bytes_fallbacks() -> None:
    assert decode_bytes("中文".encode("gbk"), "utf-8") == "中文"
    assert decode_bytes("中文".encode(), "x-unknown-charset") == "中文"
    assert decode_bytes("中文".encode("gbk"), None) == "中文"
    assert "�" in decode_bytes(b"\xff\xfe\xfa", "utf-8") or decode_bytes(b"\xff", None)


def test_gbk_plain_body_and_headers() -> None:
    msg = parse_message(gbk_plain())
    assert msg.subject == "本月报表"
    assert msg.from_ == [("张三", "zhangsan@126.com")]
    assert msg.text == "您好，附件是本月报表。镕基金" and msg.text_source == "plain"


def test_mislabelled_charset_and_raw_8bit_subject() -> None:
    msg = parse_message(mislabelled())
    assert msg.subject == "订单已发货"
    assert msg.text == "您的订单已经发货。"


def test_html_only_to_text_keeps_links_drops_scripts() -> None:
    msg = parse_message(html_only())
    assert msg.text_source == "html"
    assert "请点击这里 (https://example.com/verify?t=1)确认。" in msg.text
    assert "alert" not in msg.text and "color" not in msg.text
    assert "第二段 内容" in msg.text
    assert msg.subject == "通知"


def test_alternative_prefers_plain() -> None:
    msg = parse_message(alternative())
    assert msg.text == "plain version" and "html version" in msg.html


def test_nested_multipart_attachments_rfc2231_and_2047_names() -> None:
    msg = parse_message(nested_with_attachments())
    assert msg.text == "See attached report."
    names = [(a.filename, a.content_type, a.inline) for a in msg.attachments]
    assert names == [
        ("attachment-0.png", "image/png", True),
        ("报表.csv", "text/csv", False),
        ("合同.pdf", "application/pdf", False),
    ]
    assert msg.attachments[2].payload() == b"%PDF-1.4 fake"
    assert msg.cc == [("", "dave@example.com")]


def test_html_to_text_is_linear_on_nested_links_and_capped() -> None:
    import time

    depth = 20000
    markup = (
        "".join(f'<a href="https://x.example/{i}">' for i in range(depth)) + "t" + "</a>" * depth
    )
    started = time.perf_counter()
    text = html_to_text(markup)
    assert time.perf_counter() - started < 3.0
    assert text.endswith("t (https://x.example/19999)")
    nested = html_to_text('<a href="https://a.example">x<a href="https://b.example">y</a></a>')
    assert nested == "x (https://a.example)y (https://b.example)"
    capped = html_to_text("<p>" + "z" * 50 + "</p>", limit=20)
    assert capped.startswith("z" * 17) and "only the first 20 characters" in capped


def test_html_to_text_lists_and_entities() -> None:
    text = html_to_text("<ul><li>一</li><li>二</li></ul><p>a&amp;b<br>c</p>")
    assert text == "- 一\n- 二\n\na&b\nc"


def test_bodystructure_counting() -> None:
    attachment = [
        "APPLICATION",
        "PDF",
        ["NAME", "a.pdf"],
        None,
        None,
        "BASE64",
        "10",
        None,
        ["ATTACHMENT", ["FILENAME", "a.pdf"]],
        None,
    ]
    plain = ["TEXT", "PLAIN", ["CHARSET", "utf-8"], None, None, "7BIT", "5", "1", None, None, None]
    inline_img = ["IMAGE", "PNG", None, "<logo>", None, "BASE64", "7", None, None, None]
    mixed = [
        [plain, inline_img, "RELATED", ["BOUNDARY", "x"], None],
        attachment,
        "MIXED",
        ["BOUNDARY", "y"],
        None,
        None,
    ]
    assert bodystructure_attachments(mixed) == 1
    assert bodystructure_attachments(plain) == 0
    assert bodystructure_attachments([plain, "ALTERNATIVE", ["BOUNDARY", "z"]]) == 0


@pytest.mark.parametrize(
    ("raw", "safe"),
    [
        ("../../etc/passwd", "passwd"),
        ("..\\..\\boot.ini", "boot.ini"),
        (".bashrc", "bashrc"),
        ("con.txt", "_con.txt"),
        ("a<b>:c?.pdf", "a_b__c_.pdf"),
        ("", "attachment"),
        ("报表 2026.xlsx", "报表 2026.xlsx"),
    ],
)
def test_safe_filename(raw: str, safe: str) -> None:
    assert safe_filename(raw) == safe


def test_numbered_names_are_bounded() -> None:
    names = list(numbered_names("a.txt", 4))
    assert names == ["a.txt", "a (1).txt", "a (2).txt", "a (3).txt"]
    assert next(iter(numbered_names("archive.tar.gz"))) == "archive.tar.gz"


def test_decode_excerpt_counts_characters_not_bytes() -> None:
    long_gbk = ("报" * 5000).encode("gbk")  # 10 000 bytes, 5000 chars
    text, truncated = decode_excerpt(long_gbk, "gbk", 4000)
    assert text == "报" * 4000 and truncated is True
    short_gbk = ("报" * 3000).encode("gbk")
    assert decode_excerpt(short_gbk, "gbk", 4000) == ("报" * 3000, False)
    assert decode_excerpt(b"x" * 4000, None, 4000)[1] is False
    assert decode_excerpt(b"x" * 4001, None, 4000)[1] is True
    # A prefix cut inside a multi-byte character still decodes with the declared charset.
    data = b"a" + ("é" * 20).encode("utf-8")  # the 16-byte prefix ends mid-character
    assert decode_excerpt(data, "utf-8", 4) == ("aééé", True)


@pytest.mark.parametrize(
    ("subject", "expected"),
    [
        ("Hello", "Re: Hello"),
        ("Re: Hello", "Re: Hello"),
        ("RE: re: Hello", "Re: Hello"),
        ("回复：你好", "Re: 你好"),
        ("Re[2]: x", "Re: x"),
        ("Fwd: x", "Re: Fwd: x"),
    ],
)
def test_reply_subject(subject: str, expected: str) -> None:
    assert reply_subject(subject) == expected


def test_forward_subject_and_thread_key() -> None:
    assert forward_subject("FW: 转发: x") == "Fwd: x"
    assert thread_subject_key("Re: Fwd: 回复： Hello  World") == "hello world"


def test_reply_threading_headers() -> None:
    original = parse_message(
        simple(message_id="<c@x>", in_reply_to="<b@x>", references="<a@x> <b@x>")
    )
    assert reply_threading(original) == ("<c@x>", "<a@x> <b@x> <c@x>")
    first = parse_message(simple(message_id="<only@x>"))
    assert reply_threading(first) == ("<only@x>", "<only@x>")


def test_reply_all_excludes_self_and_dedupes() -> None:
    original = parse_message(
        simple(
            sender="Alice <alice@example.com>",
            to=f"Me <{ME.upper()}>, bob@example.com",
            cc="alice@example.com, carol@example.com",
        )
    )
    to, cc = reply_recipients(original, ME, reply_all=True)
    assert to == ["Alice <alice@example.com>"]
    assert cc == ["bob@example.com", "carol@example.com"]
    to_only, cc_none = reply_recipients(original, ME, reply_all=False)
    assert to_only == ["Alice <alice@example.com>"] and cc_none == []


def test_reply_to_own_sent_message_goes_to_original_recipients() -> None:
    original = parse_message(simple(sender=ME, to="bob@example.com"))
    assert reply_recipients(original, ME, reply_all=False) == (["bob@example.com"], [])


def test_normalize_recipients() -> None:
    assert normalize_recipients("a@x.com； B <B@Y.com>", "to") == ["a@x.com", "B <b@y.com>"]
    assert normalize_recipients(["张三 <z@x.cn>"], "to") == ["张三 <z@x.cn>"]
    assert normalize_recipients('"Zhang, San" <z@x.com>, b@y.com,', "to") == [
        '"Zhang, San" <z@x.com>',
        "b@y.com",
    ]
    assert normalize_recipients('"李；四" <l@x.cn>，c@x.cn', "cc") == [
        '"李；四" <l@x.cn>',
        "c@x.cn",
    ]
    for bad in ("bob", "a@x.com\r\nBcc: evil@x.com", "a@x.com b@y.com", "Zhang, San <z@x.com>"):
        with pytest.raises(InvalidArgument):
            normalize_recipients(bad, "to")


def test_compose_round_trip_with_attachment(tmp_path: Path) -> None:
    path = tmp_path / "说明.txt"
    path.write_text("附件内容", encoding="utf-8")
    composed = compose(
        from_addr=ME,
        to=["张三 <z@x.cn>"],
        bcc=["secret@x.cn"],
        subject="测试 subject",
        body="正文",
        html_body="<p>正文</p>",
        attachments=[load_attachment(str(path))],
        in_reply_to="<a@x>",
        references="<a@x>",
    )
    raw = composed.message.as_bytes()
    assert raw.isascii()  # 7-bit safe: RFC 2047 headers, base64/QP bodies
    parsed = parse_message(raw)
    assert parsed.subject == "测试 subject" and parsed.text == "正文"
    assert [a.filename for a in parsed.attachments] == ["说明.txt"]
    assert parsed.in_reply_to == "<a@x>" and parsed.bcc == [("", "secret@x.cn")]
    assert composed.envelope_recipients == ["z@x.cn", "secret@x.cn"]


def test_attachment_validation(tmp_path: Path) -> None:
    with pytest.raises(InvalidArgument):
        load_attachment("relative/file.txt")
    with pytest.raises(InvalidArgument):
        load_attachment(str(tmp_path))  # directory
    with pytest.raises(InvalidArgument):
        load_attachment(str(tmp_path / "missing.txt"))
    secret = tmp_path / ".ssh" / "id_rsa"
    secret.parent.mkdir()
    secret.write_text("key")
    with pytest.raises(InvalidArgument):
        load_attachment(str(secret))
    many = []
    for i in range(11):
        p = tmp_path / f"f{i}.txt"
        p.write_text("x")
        many.append(load_attachment(str(p)))
    with pytest.raises(LimitError):
        compose(from_addr=ME, to=["a@x.com"], subject="s", body="b", attachments=many)


def test_attachment_path_policy(tmp_path: Path, monkeypatch) -> None:
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    appdata = tmp_path / "appdata"
    monkeypatch.setenv("APPDATA", str(appdata))
    files = {
        "ok": home / "docs" / "report.txt",
        "dotdir": home / ".config" / "app" / "token.txt",
        "dotfile": home / "project" / ".env",
        "library": home / "Library" / "Cookies" / "c.txt",
        "appdata": appdata / "App" / "state.txt",
        "allowed": tmp_path / "allowed" / "a.txt",
    }
    for path in files.values():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("content")
    assert load_attachment(str(files["ok"])).data == b"content"
    for key in ("dotdir", "dotfile", "library", "appdata"):
        with pytest.raises(InvalidArgument, match=r"copy|复制"):
            load_attachment(str(files[key]))
    link = home / "docs" / "innocent.txt"
    try:
        link.symlink_to(files["dotfile"])
    except OSError:
        pass  # no symlink privilege (Windows)
    else:
        with pytest.raises(InvalidArgument):
            load_attachment(str(link))  # resolved before checking
    roots = [tmp_path / "allowed"]
    assert load_attachment(str(files["allowed"]), roots=roots).data == b"content"
    with pytest.raises(InvalidArgument, match="NETEASE_MAIL_ATTACHMENT_ROOTS"):
        load_attachment(str(files["ok"]), roots=roots)
    with pytest.raises(InvalidArgument):
        load_attachment("\\\\server\\share\\x.txt")
    if os.name != "nt":
        for system in ("/etc/hosts", "/dev/null"):
            with pytest.raises(InvalidArgument):
                load_attachment(system)
        fifo = tmp_path / "pipe"
        os.mkfifo(fifo)
        with pytest.raises(InvalidArgument, match="regular"):
            load_attachment(str(fifo))


def test_attachment_policy_cloud_drives_case_and_explicit_roots(
    tmp_path: Path, monkeypatch
) -> None:
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    library = home / "Library"
    files = {
        "icloud": library / "Mobile Documents" / "com~apple~CloudDocs" / "plan.pdf",
        "dropbox": library / "CloudStorage" / "Dropbox" / "q3.xlsx",
        "dropbox_hidden": library / "CloudStorage" / "Dropbox" / ".dropbox" / "info.json",
        "wechat": library / "Containers" / "chat" / "Data" / "File" / "contract.pdf",
        "cookies": library / "Cookies" / "c.txt",
        "dotfile": home / "project" / ".env",
    }
    for path in files.values():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("content")
    assert load_attachment(str(files["icloud"])).data == b"content"
    assert load_attachment(str(files["dropbox"])).data == b"content"
    for key in ("dropbox_hidden", "wechat", "cookies"):
        with pytest.raises(InvalidArgument):
            load_attachment(str(files[key]))
    # An explicit root inside ~/Library lifts the ~/Library rule for that folder only.
    roots = [library / "Containers" / "chat" / "Data" / "File", home / "project"]
    assert load_attachment(str(files["wechat"]), roots=roots).data == b"content"
    with pytest.raises(InvalidArgument):
        load_attachment(str(files["dotfile"]), roots=roots)  # hidden files never
    with pytest.raises(InvalidArgument):
        load_attachment(str(files["cookies"]), roots=[home])  # home is not inside ~/Library
    with pytest.raises(InvalidArgument):
        load_attachment(str(files["cookies"]), roots=[library])  # nor is ~/Library itself
    # macOS and Windows disks ignore letter case; the policy must too.
    upper_home = tmp_path / "HOME"
    if upper_home.exists():
        for variant in (
            upper_home / "project" / ".env",
            upper_home / "LIBRARY" / "Cookies" / "c.txt",
            home / "library" / "cookies" / "c.txt",
        ):
            with pytest.raises(InvalidArgument, match=r"copy|复制"):
                load_attachment(str(variant))
        cloud = home / "library" / "cloudstorage" / "Dropbox" / "q3.xlsx"
        assert load_attachment(str(cloud)).data == b"content"


def test_attachment_roots_from_env(tmp_path: Path) -> None:
    value = os.pathsep.join([str(tmp_path / "a"), str(tmp_path / "b"), ""])
    config = load_config({"NETEASE_MAIL_ATTACHMENT_ROOTS": value}, path=tmp_path / "none.toml")
    assert config.attachment_roots == (tmp_path / "a", tmp_path / "b")
    with pytest.raises(ConfigError):
        load_config({"NETEASE_MAIL_ATTACHMENT_ROOTS": "relative"}, path=tmp_path / "none.toml")


def test_compose_rejects_header_injection() -> None:
    with pytest.raises(InvalidArgument):
        compose(from_addr=ME, to=["a@x.com"], subject="hi\r\nBcc: evil@x.com", body="b")
