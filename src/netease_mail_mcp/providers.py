"""NetEase personal-mail domains: hosts, ports and where to get a 授权码."""

from __future__ import annotations

from dataclasses import dataclass

from .errors import ConfigError

SETTINGS_PATH = "设置 → POP3/SMTP/IMAP"
SETTINGS_PATH_EN = "Settings → POP3/SMTP/IMAP"


@dataclass(frozen=True)
class Provider:
    domain: str
    label: str
    imap_host: str
    smtp_host: str
    webmail: str
    # "verified": exercised against a real account; "supported": same NetEase platform,
    # not yet exercised; "untested": host names follow the documented pattern only.
    status: str
    imap_port: int = 993
    smtp_ssl_port: int = 465
    smtp_starttls_port: int = 25  # 587 does NOT work on NetEase (times out)


PROVIDERS: dict[str, Provider] = {
    p.domain: p
    for p in (
        Provider(
            "163.com",
            "网易 163 邮箱",
            "imap.163.com",
            "smtp.163.com",
            "https://mail.163.com",
            "verified",
        ),
        Provider(
            "126.com",
            "网易 126 邮箱",
            "imap.126.com",
            "smtp.126.com",
            "https://mail.126.com",
            "supported",
        ),
        Provider(
            "yeah.net",
            "网易 yeah.net 邮箱",
            "imap.yeah.net",
            "smtp.yeah.net",
            "https://mail.yeah.net",
            "verified",
        ),
        Provider(
            "188.com",
            "网易 188 财富邮",
            "imap.188.com",
            "smtp.188.com",
            "https://mail.188.com",
            "supported",
        ),
        Provider(
            "vip.163.com",
            "网易 VIP 163 邮箱",
            "imap.vip.163.com",
            "smtp.vip.163.com",
            "https://vip.163.com",
            "untested",
        ),
        Provider(
            "vip.126.com",
            "网易 VIP 126 邮箱",
            "imap.vip.126.com",
            "smtp.vip.126.com",
            "https://vip.126.com",
            "untested",
        ),
    )
}


def domain_of(address: str) -> str:
    return address.rsplit("@", 1)[-1].strip().lower()


def provider_for(address: str) -> Provider:
    domain = domain_of(address)
    try:
        return PROVIDERS[domain]
    except KeyError:
        raise ConfigError(
            f"不支持的邮箱域名 {domain}（支持：{', '.join(PROVIDERS)}）。"
            f"企业邮箱（qiye.163.com）不在本工具范围内。",
            hint=f"Unsupported domain {domain}. Supported: {', '.join(PROVIDERS)}.",
        ) from None


def auth_code_instructions(address: str) -> str:
    """Where to enable IMAP/SMTP and create a 授权码, in Chinese and English."""
    p = provider_for(address)
    return (
        f"1. 打开 {p.webmail} 登录 {address}，进入「{SETTINGS_PATH}」。\n"
        f"2. 开启「IMAP/SMTP 服务」，按提示用手机验证后生成一个「授权码」（客户端授权密码）。\n"
        f"   授权码只显示一次；它不是登录密码。免费邮箱可用的授权码个数有限，"
        f"在网页版删除某个授权码会让用它的客户端立即掉线。\n"
        f"3. 同一页的「收取选项」决定 IMAP 能看到多久的邮件：设为「最近30天」时，"
        f"更早的邮件对本工具不可见；需要全部历史邮件请改为「全部」。\n"
        f"\n"
        f"1. Sign in to {p.webmail} as {address} and open {SETTINGS_PATH_EN}.\n"
        f"2. Enable the IMAP/SMTP service and create a client authorization code (授权码).\n"
        f"   It is shown once and is NOT your password; deleting it in webmail revokes it.\n"
        f"3. The receive window option (收取选项) on the same page limits what IMAP can see:\n"
        f"   with 'last 30 days', older mail is invisible to this tool; choose 'all' if needed."
    )


def login_failure_hint(address: str) -> str:
    p = provider_for(address)
    return (
        f"登录失败。请检查：1) 已在 {p.webmail} 的「{SETTINGS_PATH}」开启 IMAP/SMTP 服务；"
        f"2) 用的是「授权码」而不是登录密码；3) 该授权码没有在网页版被删除（删除后立即失效）。"
        f"重新生成授权码后运行：netease-mail-mcp-server setup --email {address}\n"
        f"Login failed. Check that IMAP/SMTP is enabled in webmail ({SETTINGS_PATH_EN}), that "
        f"you used the client authorization code (授权码) rather than the account password, "
        f"and that the code was not revoked in webmail. Create a new code and re-run setup."
    )


UNSAFE_LOGIN_HINT = (
    "网易服务器拒绝打开文件夹（Unsafe Login）：通常是客户端没有在登录后发送 IMAP ID。"
    "本工具会自动发送；若仍出现，请在网页版确认 IMAP 已开启，或稍后重试。\n"
    "NetEase refused to open the folder ('Unsafe Login'). This happens when the IMAP ID "
    "command is missing; this server sends it automatically. If it persists, re-check that "
    "IMAP is enabled in webmail and retry later."
)

RECEIVE_WINDOW_HINT = (
    "提示：网易网页版「设置 → POP3/SMTP/IMAP → 收取选项」若为「最近30天」，"
    "IMAP 只能看到近 30 天的邮件。Hint: if the webmail receive window (收取选项) is "
    "'last 30 days', older mail is not visible over IMAP."
)
