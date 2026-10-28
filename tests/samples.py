"""Sample messages as raw bytes, written the way real (often messy) Chinese mail looks."""

from __future__ import annotations

import base64
from email.message import EmailMessage
from email.utils import formatdate

ME = "me@163.com"


def b64(text: str, charset: str) -> str:
    return base64.b64encode(text.encode(charset)).decode()


def rfc2047(text: str, charset: str = "utf-8") -> str:
    return f"=?{charset}?B?{b64(text, charset)}?="


def simple(
    subject: str = "Hello",
    sender: str = "Alice <alice@example.com>",
    to: str = ME,
    body: str = "hi there",
    *,
    message_id: str | None = None,
    in_reply_to: str | None = None,
    references: str | None = None,
    cc: str | None = None,
    date: str | None = None,
) -> bytes:
    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = to
    if cc:
        msg["Cc"] = cc
    msg["Subject"] = subject
    msg["Date"] = date or formatdate(localtime=True)
    msg["Message-ID"] = message_id or f"<{abs(hash((subject, body, sender)))}@example.com>"
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
    if references:
        msg["References"] = references
    msg.set_content(body)
    return msg.as_bytes()


def gbk_plain() -> bytes:
    """GBK body labelled gb2312 (with a GBK-only character), RFC 2047 GBK headers."""
    body = "您好，附件是本月报表。镕基金"  # 镕 is outside GB2312
    return (
        f"From: {rfc2047('张三', 'gbk')} <zhangsan@126.com>\r\n"
        f"To: {ME}\r\n"
        f"Subject: {rfc2047('本月报表', 'gbk')}\r\n"
        "Date: Mon, 26 Oct 2026 10:00:00 +0800\r\n"
        "Message-ID: <gbk-1@126.com>\r\n"
        "MIME-Version: 1.0\r\n"
        'Content-Type: text/plain; charset="gb2312"\r\n'
        "Content-Transfer-Encoding: base64\r\n\r\n"
        f"{b64(body, 'gbk')}\r\n"
    ).encode("ascii")


def mislabelled() -> bytes:
    """Declared utf-8 but actually GBK, plus a raw 8-bit UTF-8 subject header."""
    head = (
        "From: shop@example.cn\r\n"
        f"To: {ME}\r\n"
        "Subject: 订单已发货\r\n"
        "Date: Tue, 27 Oct 2026 09:00:00 +0800\r\n"
        "Message-ID: <mis-1@example.cn>\r\n"
        "Content-Type: text/plain; charset=utf-8\r\n"
        "Content-Transfer-Encoding: 8bit\r\n\r\n"
    ).encode()
    return head + "您的订单已经发货。".encode("gbk") + b"\r\n"


def html_only() -> bytes:
    return (
        "From: news@example.com\r\n"
        f"To: {ME}\r\n"
        "Subject: =?utf-8?Q?=E9=80=9A=E7=9F=A5?=\r\n"
        "Date: Tue, 27 Oct 2026 11:00:00 +0800\r\n"
        "Message-ID: <html-1@example.com>\r\n"
        "Content-Type: text/html; charset=utf-8\r\n\r\n"
        "<html><head><style>p{color:red}</style><title>t</title></head><body>"
        '<p>请点击<a href="https://example.com/verify?t=1">这里</a>确认。</p>'
        "<script>alert(1)</script><div>第二段&nbsp;内容</div></body></html>\r\n"
    ).encode()


def alternative() -> bytes:
    msg = EmailMessage()
    msg["From"] = "bob@example.com"
    msg["To"] = ME
    msg["Subject"] = "Alt"
    msg["Message-ID"] = "<alt-1@example.com>"
    msg.set_content("plain version")
    msg.add_alternative("<p>html version</p>", subtype="html")
    return msg.as_bytes()


def nested_with_attachments() -> bytes:
    """mixed[ related[ alternative[plain, html], inline image ], csv (RFC 2231 GBK name), pdf ]."""
    boundary = ("mixed", "related", "alt")
    gbk_name = "".join(f"%{b:02X}" for b in "报表.csv".encode("gbk"))
    csv_data = "名称,数量\n苹果,3\n".encode("gbk")
    return (
        "From: Carol <carol@example.com>\r\n"
        f"To: {ME}\r\n"
        "Cc: dave@example.com\r\n"
        "Subject: Report with files\r\n"
        "Date: Wed, 28 Oct 2026 08:00:00 +0800\r\n"
        "Message-ID: <nested-1@example.com>\r\n"
        f'Content-Type: multipart/mixed; boundary="{boundary[0]}"\r\n\r\n'
        f"--{boundary[0]}\r\n"
        f'Content-Type: multipart/related; boundary="{boundary[1]}"\r\n\r\n'
        f"--{boundary[1]}\r\n"
        f'Content-Type: multipart/alternative; boundary="{boundary[2]}"\r\n\r\n'
        f"--{boundary[2]}\r\n"
        "Content-Type: text/plain; charset=utf-8\r\n\r\n"
        "See attached report.\r\n"
        f"--{boundary[2]}\r\n"
        "Content-Type: text/html; charset=utf-8\r\n\r\n"
        '<p>See attached <b>report</b>.</p><img src="cid:logo1">\r\n'
        f"--{boundary[2]}--\r\n"
        f"--{boundary[1]}\r\n"
        "Content-Type: image/png\r\n"
        "Content-ID: <logo1>\r\n"
        "Content-Transfer-Encoding: base64\r\n\r\n"
        f"{base64.b64encode(b'PNGDATA').decode()}\r\n"
        f"--{boundary[1]}--\r\n"
        f"--{boundary[0]}\r\n"
        "Content-Type: text/csv; charset=gbk\r\n"
        f"Content-Disposition: attachment; filename*=gbk''{gbk_name}\r\n"
        "Content-Transfer-Encoding: base64\r\n\r\n"
        f"{base64.b64encode(csv_data).decode()}\r\n"
        f"--{boundary[0]}\r\n"
        f'Content-Type: application/pdf; name="{rfc2047("合同.pdf")}"\r\n'
        f'Content-Disposition: attachment; filename="{rfc2047("合同.pdf")}"\r\n'
        "Content-Transfer-Encoding: base64\r\n\r\n"
        f"{base64.b64encode(b'%PDF-1.4 fake').decode()}\r\n"
        f"--{boundary[0]}--\r\n"
    ).encode("ascii")


def code_mail(code: str = "482913", html: bool = False, subject: str = "您的验证码") -> bytes:
    msg = EmailMessage()
    msg["From"] = "Service <no-reply@service.example>"
    msg["To"] = ME
    msg["Subject"] = subject
    msg["Message-ID"] = f"<code-{code}@service.example>"
    text = f"您好！\n您的验证码是：{code}，10 分钟内有效。订单号 20261028991。\n客服电话 4008123123"
    if html:
        msg.set_content(f"<p>Your verification code is <b>{code}</b></p>", subtype="html")
    else:
        msg.set_content(text)
    return msg.as_bytes()
