"""Command line: serve (default) | setup | doctor | config <client> | accounts."""

from __future__ import annotations

import argparse
import getpass
import json
import os
import shutil
import sys
import time
from collections.abc import Sequence
from typing import Any, TextIO

from . import __version__
from .actions import default_smtp_factory, smtp_login
from .config import (
    ENV_MODE,
    add_account_to_file,
    default_config_path,
    load_config,
    normalize_address,
    remove_account_from_file,
)
from .credentials import CredentialStore, env_var_name, validate_auth_code
from .errors import MailError
from .imap import open_session
from .providers import RECEIVE_WINDOW_HINT, auth_code_instructions, provider_for

SERVER_NAME = "netease-mail"
PACKAGE = "netease-mail-mcp-server"
CLIENTS = ("claude-code", "codex", "cursor", "claude-desktop")


def _out(text: str = "", stream: TextIO | None = None) -> None:
    print(text, file=stream or sys.stdout, flush=True)


# --------------------------------------------------------------------------- config snippets


def _launcher(absolute: bool) -> tuple[str, list[str]]:
    uvx = shutil.which("uvx")
    if uvx:
        return (uvx if absolute else "uvx"), [PACKAGE]
    exe = shutil.which(PACKAGE)
    if exe:
        return exe, []
    return "uvx", [PACKAGE]


def client_snippet(client: str, mode: str | None = None) -> str:
    env = {ENV_MODE: mode} if mode else {}
    if client == "claude-code":
        command, args = _launcher(absolute=False)
        env_flags = "".join(f" -e {k}={v}" for k, v in env.items())
        launch = " ".join([command, *args])
        return f"claude mcp add --scope user {SERVER_NAME}{env_flags} -- {launch}"
    if client == "codex":
        command, args = _launcher(absolute=False)
        lines = [
            "# ~/.codex/config.toml",
            f"[mcp_servers.{SERVER_NAME}]",
            f"command = {json.dumps(command)}",
            f"args = {json.dumps(args)}",
        ]
        if env:
            lines += ["", f"[mcp_servers.{SERVER_NAME}.env]"]
            lines += [f"{k} = {json.dumps(v)}" for k, v in env.items()]
        return "\n".join(lines)
    if client in ("cursor", "claude-desktop"):
        # GUI apps often lack the shell PATH: use an absolute command path.
        command, args = _launcher(absolute=True)
        entry: dict[str, Any] = {"command": command, "args": args}
        if env:
            entry["env"] = env
        where = (
            "~/.cursor/mcp.json"
            if client == "cursor"
            else (
                "~/Library/Application Support/Claude/claude_desktop_config.json (macOS) / "
                "%APPDATA%\\Claude\\claude_desktop_config.json (Windows)"
            )
        )
        body = json.dumps({"mcpServers": {SERVER_NAME: entry}}, ensure_ascii=False, indent=2)
        return f"// {where}\n{body}"
    raise ValueError(f"unknown client {client}")


def print_all_snippets(mode: str | None = None) -> None:
    titles = {
        "claude-code": "Claude Code",
        "codex": "Codex",
        "cursor": "Cursor",
        "claude-desktop": "Claude Desktop",
    }
    for client in CLIENTS:
        _out(f"\n--- {titles[client]} ---")
        _out(client_snippet(client, mode))


# --------------------------------------------------------------------------- doctor


def _check_account(address: str, store: CredentialStore) -> tuple[list[dict[str, Any]], bool]:
    checks: list[dict[str, Any]] = []
    ok = True

    def add(name: str, status: str, detail: str = "") -> None:
        nonlocal ok
        if status == "fail":
            ok = False
        checks.append({"check": name, "status": status, "detail": detail})

    try:
        provider = provider_for(address)
    except MailError as exc:
        add("provider", "fail", exc.message)
        return checks, False
    add(
        "provider",
        "warn" if provider.status == "untested" else "ok",
        f"{provider.label} ({provider.status}) {provider.imap_host} / {provider.smtp_host}",
    )
    try:
        code, source = store.lookup(address)
    except MailError as exc:
        add("credential", "fail", exc.message)
        return checks, False
    if not code:
        add("credential", "fail", f"no 授权码 — run setup or set {env_var_name(address)}")
        return checks, False
    add("credential", "ok", f"found in {source}")

    started = time.monotonic()
    try:
        session = open_session(address, code)
    except MailError as exc:
        add("imap_login", "fail", str(exc))
        return checks, False
    add("imap_login", "ok", f"{time.monotonic() - started:.1f}s")
    try:
        add(
            "imap_id",
            "ok" if session.id_accepted else "fail",
            "ID accepted" if session.id_accepted else "server rejected IMAP ID",
        )
        inbox = session.resolve_folder("INBOX")
        state = session.open_folder(inbox)
        status = session.status(inbox, "(MESSAGES UNSEEN)")
        add("examine_inbox", "ok", f"{state.exists} messages, {status.get('UNSEEN', '?')} unseen")
        uids = session.uid_search(["ALL"])
        if uids:
            record = session.fetch_one(uids[0], "(UID INTERNALDATE)")
            from .imap import parse_internaldate

            oldest = parse_internaldate(record.get("INTERNALDATE"))
            if oldest:
                age = (time.time() - oldest.timestamp()) / 86400
                detail = (
                    f"oldest visible INBOX message: {oldest.date().isoformat()} ({age:.0f} days)"
                )
                if age <= 35:
                    add("receive_window", "warn", detail + " — " + RECEIVE_WINDOW_HINT)
                else:
                    add("receive_window", "ok", detail)
        caps = {c: session.has(c) for c in ("MOVE", "IDLE", "UIDPLUS", "SPECIAL-USE", "ID")}
        add("capabilities", "ok", ", ".join(f"{c}={'yes' if v else 'no'}" for c, v in caps.items()))
        roles = {r: session.folder_by_role(r) for r in ("drafts", "sent", "trash", "junk")}
        missing = [r for r, f in roles.items() if f is None]
        add(
            "special_folders",
            "warn" if missing else "ok",
            ", ".join(f"{r}={f.name if f else 'missing'}" for r, f in roles.items()),
        )
    except MailError as exc:
        add("imap", "fail", str(exc))
    finally:
        session.close()

    started = time.monotonic()
    try:
        client, port = smtp_login(address, code, default_smtp_factory)
        try:
            client.quit()
        except Exception:
            pass
        add("smtp_login", "ok", f"port {port}, {time.monotonic() - started:.1f}s")
    except MailError as exc:
        add("smtp_login", "fail", str(exc))
    return checks, ok


def cmd_doctor(args: argparse.Namespace) -> int:
    try:
        config = load_config()
        accounts = [normalize_address(args.account)] if args.account else list(config.accounts)
    except MailError as exc:
        _out(exc.render(), sys.stderr)
        return 2
    if not accounts:
        _out(
            "没有配置账号，先运行 setup。"
            "No accounts configured: run `netease-mail-mcp-server setup`."
        )
        return 1
    store = CredentialStore()
    report: list[dict[str, Any]] = []
    all_ok = True
    for address in accounts:
        checks, ok = _check_account(address, store)
        all_ok = all_ok and ok
        report.append({"account": address, "ok": ok, "checks": checks})
    if args.json:
        _out(
            json.dumps(
                {"ok": all_ok, "accounts": report, "mode": config.mode},
                ensure_ascii=False,
                indent=2,
            )
        )
    else:
        _out(f"netease-mail-mcp {__version__} — mode={config.mode}, confirm={config.confirm}")
        marks = {"ok": "[ OK ]", "warn": "[WARN]", "fail": "[FAIL]"}
        for entry in report:
            _out(f"\n{entry['account']}")
            for check in entry["checks"]:
                _out(f"  {marks[check['status']]} {check['check']}: {check['detail']}")
        _out("\n" + ("全部通过 All checks passed." if all_ok else "有检查失败 Some checks failed."))
    return 0 if all_ok else 1


# --------------------------------------------------------------------------- setup


def cmd_setup(args: argparse.Namespace) -> int:
    try:
        raw = args.email or input("邮箱地址 Email address: ")
        address = normalize_address(raw)
        provider = provider_for(address)
    except (MailError, EOFError) as exc:
        _out(exc.render() if isinstance(exc, MailError) else "已取消 cancelled", sys.stderr)
        return 2
    _out(
        f"\n{provider.label}: IMAP {provider.imap_host}:{provider.imap_port}, "
        f"SMTP {provider.smtp_host}:{provider.smtp_ssl_port}"
    )
    if provider.status == "untested":
        _out("注意：这个域名还没有在真实账号上测试过。Note: this domain is untested.")
    _out("\n" + auth_code_instructions(address) + "\n")
    try:
        if args.auth_code_stdin:
            code = sys.stdin.readline()
        else:
            code = getpass.getpass("授权码（输入不显示）Authorization code (hidden): ")
        code = validate_auth_code(code)
    except (MailError, EOFError, KeyboardInterrupt) as exc:
        _out(exc.render() if isinstance(exc, MailError) else "已取消 cancelled", sys.stderr)
        return 2

    if not args.no_verify:
        _out("测试 IMAP 登录 Testing IMAP (login + ID + EXAMINE INBOX)…")
        try:
            session = open_session(address, code)
            try:
                state = session.open_folder(session.resolve_folder("INBOX"))
                _out(f"  OK: INBOX {state.exists} 封 messages")
            finally:
                session.close()
            _out("测试 SMTP 登录 Testing SMTP login…")
            client, port = smtp_login(address, code, default_smtp_factory)
            try:
                client.quit()
            except Exception:
                pass
            _out(f"  OK: port {port}")
        except MailError as exc:
            _out(exc.render(), sys.stderr)
            return 1

    store = CredentialStore()
    try:
        backend = store.set(address, code)
    except MailError as exc:
        _out(exc.render(), sys.stderr)
        return 1
    _out(f"授权码已保存到 Saved to: {backend} (service netease-mail-mcp, account {address})")
    path = default_config_path()
    try:
        accounts = add_account_to_file(path, address, make_default=args.default)
    except (OSError, MailError) as exc:
        _out(f"写配置失败 Cannot write config {path}: {exc}", sys.stderr)
        return 1
    _out(f"配置已写入 Config written: {path}  accounts = {', '.join(accounts)}")
    _out("\n把下面任一段加到你的 MCP 客户端 Add one of these to your MCP client:")
    print_all_snippets()
    _out("\n检查连接 Verify anytime: netease-mail-mcp-server doctor")
    return 0


# --------------------------------------------------------------------------- accounts


def cmd_accounts(args: argparse.Namespace) -> int:
    store = CredentialStore()
    if args.accounts_command == "remove":
        try:
            address = normalize_address(args.email)
        except MailError as exc:
            _out(exc.render(), sys.stderr)
            return 2
        removed_secret = store.delete(address)
        removed_config = remove_account_from_file(default_config_path(), address)
        _out(
            f"{address}: keychain entry {'removed' if removed_secret else 'not found'}, "
            f"config entry {'removed' if removed_config else 'not found'}."
        )
        if os.environ.get(env_var_name(address)):
            _out(f"注意：环境变量 {env_var_name(address)} 仍然设置着。The env var is still set.")
        return 0 if (removed_secret or removed_config) else 1
    try:
        config = load_config()
    except MailError as exc:
        _out(exc.render(), sys.stderr)
        return 2
    if not config.accounts:
        _out("没有配置账号 No accounts configured.")
        return 0
    for address in config.accounts:
        try:
            source = store.source(address)
        except MailError:
            source = None
        marker = "*" if address == config.default_account else " "
        _out(f"{marker} {address}  credential: {source or 'missing'}")
    return 0


# --------------------------------------------------------------------------- main


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="netease-mail-mcp-server",
        description="MCP server for NetEase personal mail (163 / 126 / yeah.net / 188).",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("serve", help="run the MCP server over stdio (default)")

    setup = sub.add_parser("setup", help="add an account: test login, store 授权码, write config")
    setup.add_argument("--email", help="email address (otherwise prompted)")
    setup.add_argument(
        "--auth-code-stdin",
        action="store_true",
        help="read the 授权码 from stdin instead of a hidden prompt",
    )
    setup.add_argument("--default", action="store_true", help="make this the default account")
    setup.add_argument(
        "--no-verify", action="store_true", help="skip the IMAP/SMTP login test (offline use)"
    )

    doctor = sub.add_parser("doctor", help="diagnose accounts (login, ID, folders, SMTP)")
    doctor.add_argument("--account", help="check only this address")
    doctor.add_argument("--json", action="store_true", help="machine-readable output")

    config = sub.add_parser("config", help="print an MCP client configuration snippet")
    config.add_argument("client", choices=CLIENTS)
    config.add_argument(
        "--mode",
        choices=("readonly", "draft", "full"),
        help="also set NETEASE_MAIL_MODE in the snippet",
    )

    accounts = sub.add_parser("accounts", help="list or remove accounts")
    acc_sub = accounts.add_subparsers(dest="accounts_command")
    acc_sub.add_parser("list", help="list configured accounts")
    remove = acc_sub.add_parser("remove", help="delete the keychain entry and config entry")
    remove.add_argument("email")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    command = args.command or "serve"
    if command == "serve":
        from .server import build_services, run_stdio

        try:
            services = build_services()
        except MailError as exc:
            _out(exc.render(), sys.stderr)
            return 2
        run_stdio(services)
        return 0
    if command == "setup":
        return cmd_setup(args)
    if command == "doctor":
        return cmd_doctor(args)
    if command == "config":
        _out(client_snippet(args.client, args.mode))
        return 0
    if command == "accounts":
        return cmd_accounts(args)
    return 2


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
