"""Configuration: accounts, mode, confirmation style, download dir.

Sources, highest priority first: environment variables, then the config file written by
``setup`` (``~/.config/netease-mail-mcp/config.toml``; ``%APPDATA%`` on Windows).
The file is flat TOML. It is read with :mod:`tomllib` on 3.11+ and a tiny parser on 3.10,
and written by hand, so there is no TOML dependency.
"""

from __future__ import annotations

import json
import os
import re
import sys
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .errors import ConfigError
from .providers import provider_for

MODES = ("readonly", "draft", "full")
CONFIRM_MODES = ("auto", "token", "elicit")
DEFAULT_MODE = "full"
DEFAULT_CONFIRM = "auto"

ENV_ACCOUNTS = "NETEASE_MAIL_ACCOUNTS"
ENV_MODE = "NETEASE_MAIL_MODE"
ENV_CONFIRM = "NETEASE_MAIL_CONFIRM"
ENV_DOWNLOAD_DIR = "NETEASE_MAIL_DOWNLOAD_DIR"
ENV_ATTACHMENT_ROOTS = "NETEASE_MAIL_ATTACHMENT_ROOTS"  # os.pathsep-separated allowlist
ENV_CONFIG = "NETEASE_MAIL_CONFIG"  # alternative config file path (tests, multiple profiles)

_ADDRESS_RE = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9\-]+(\.[A-Za-z0-9\-]+)+$")


def normalize_address(value: str) -> str:
    address = (value or "").strip().lower()
    if not _ADDRESS_RE.match(address):
        raise ConfigError(
            f"邮箱地址格式不对：{value!r}", hint=f"Not a valid email address: {value!r}"
        )
    return address


def default_config_path(environ: Mapping[str, str] | None = None) -> Path:
    env = os.environ if environ is None else environ
    if env.get(ENV_CONFIG):
        return Path(env[ENV_CONFIG]).expanduser()
    if os.name == "nt":
        base = Path(env.get("APPDATA") or Path.home() / "AppData" / "Roaming")
    else:
        base = Path(env.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return base / "netease-mail-mcp" / "config.toml"


def default_download_dir() -> Path:
    return Path.home() / "Downloads" / "netease-mail-mcp"


@dataclass(frozen=True)
class Config:
    accounts: tuple[str, ...] = ()
    mode: str = DEFAULT_MODE
    confirm: str = DEFAULT_CONFIRM
    download_dir: Path = field(default_factory=default_download_dir)
    attachment_roots: tuple[Path, ...] = ()
    path: Path | None = None
    sources: Mapping[str, str] = field(default_factory=dict)

    @property
    def default_account(self) -> str | None:
        return self.accounts[0] if self.accounts else None

    def resolve_account(self, account: str | None) -> str:
        if not self.accounts:
            raise ConfigError(
                "还没有配置邮箱账号：运行 `netease-mail-mcp-server setup`，"
                f"或在 MCP 配置里设置环境变量 {ENV_ACCOUNTS}。",
                hint=f"No account configured. Run `netease-mail-mcp-server setup` or set "
                f"{ENV_ACCOUNTS}.",
            )
        if not account:
            return self.accounts[0]
        address = normalize_address(account)
        if address not in self.accounts:
            raise ConfigError(
                f"账号 {address} 没有配置（已配置：{', '.join(self.accounts)}）。",
                hint=f"Account {address} is not configured.",
            )
        return address


# --------------------------------------------------------------------------- TOML (flat)


def _strip_comment(line: str) -> str:
    in_str = False
    escaped = False
    for i, ch in enumerate(line):
        if in_str:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_str = False
        elif ch == '"':
            in_str = True
        elif ch == "#":
            return line[:i]
    return line


def _parse_scalar(text: str) -> Any:
    text = text.strip()
    if text.startswith('"'):
        return json.loads(text)
    if text.startswith("'") and text.endswith("'"):
        return text[1:-1]
    if text in ("true", "false"):
        return text == "true"
    if re.fullmatch(r"[+-]?\d+", text):
        return int(text)
    raise ValueError(f"unsupported value: {text}")


def _parse_array(text: str) -> list[Any]:
    inner = text.strip()[1:-1]
    items: list[Any] = []
    buf = ""
    in_str = False
    escaped = False
    for ch in inner:
        if in_str:
            buf += ch
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_str = False
        elif ch == '"':
            in_str = True
            buf += ch
        elif ch == ",":
            if buf.strip():
                items.append(_parse_scalar(buf))
            buf = ""
        else:
            buf += ch
    if buf.strip():
        items.append(_parse_scalar(buf))
    return items


def parse_flat_toml(text: str) -> dict[str, Any]:
    """Parse the flat subset of TOML that ``setup`` writes (used on Python 3.10)."""
    data: dict[str, Any] = {}
    for lineno, raw in enumerate(text.splitlines(), 1):
        line = _strip_comment(raw).strip()
        if not line:
            continue
        if line.startswith("["):
            raise ValueError(f"line {lineno}: tables are not supported in this config file")
        key, sep, value = line.partition("=")
        if not sep:
            raise ValueError(f"line {lineno}: expected key = value")
        key = key.strip().strip('"')
        value = value.strip()
        try:
            data[key] = _parse_array(value) if value.startswith("[") else _parse_scalar(value)
        except (ValueError, json.JSONDecodeError) as exc:
            raise ValueError(f"line {lineno}: {exc}") from None
    return data


def loads_toml(text: str) -> dict[str, Any]:
    if sys.version_info >= (3, 11):
        import tomllib

        return tomllib.loads(text)
    return parse_flat_toml(text)


def dumps_toml(data: Mapping[str, Any]) -> str:
    lines = ["# netease-mail-mcp configuration (written by `netease-mail-mcp-server setup`)."]
    lines.append("# Secrets are NOT stored here: 授权码 live in the system keychain.")
    for key, value in data.items():
        if value is None:
            continue
        if isinstance(value, bool):
            rendered = "true" if value else "false"
        elif isinstance(value, int):
            rendered = str(value)
        elif isinstance(value, (list, tuple)):
            rendered = "[" + ", ".join(json.dumps(str(v), ensure_ascii=False) for v in value) + "]"
        else:
            rendered = json.dumps(str(value), ensure_ascii=False)
        lines.append(f"{key} = {rendered}")
    return "\n".join(lines) + "\n"


def read_config_file(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return loads_toml(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ConfigError(
            f"配置文件 {path} 读取失败：{exc}", hint=f"Cannot read config file {path}: {exc}"
        ) from None


def write_config_file(path: Path, data: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".config.", suffix=".toml", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(dumps_toml(data))
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


# --------------------------------------------------------------------------- loading


def _split_accounts(value: Any) -> list[str]:
    if isinstance(value, str):
        items = value.split(",")
    elif isinstance(value, (list, tuple)):
        items = [str(v) for v in value]
    else:
        raise ConfigError(f"accounts 配置格式不对：{value!r}", hint="accounts must be a list")
    seen: list[str] = []
    for item in items:
        if not item.strip():
            continue
        address = normalize_address(item)
        provider_for(address)  # fail early on unsupported domains
        if address not in seen:
            seen.append(address)
    return seen


def _split_roots(value: Any) -> tuple[Path, ...]:
    items = value.split(os.pathsep) if isinstance(value, str) else list(value or [])
    roots: list[Path] = []
    for item in items:
        text = str(item).strip()
        if not text:
            continue
        root = Path(text).expanduser()
        if not root.is_absolute():
            raise ConfigError(
                f"{ENV_ATTACHMENT_ROOTS} 里的目录必须是绝对路径：{text!r}",
                hint=f"{ENV_ATTACHMENT_ROOTS} entries must be absolute paths: {text!r}",
            )
        roots.append(root)
    return tuple(roots)


def load_config(environ: Mapping[str, str] | None = None, path: Path | None = None) -> Config:
    env = os.environ if environ is None else environ
    cfg_path = path or default_config_path(env)
    file_data = read_config_file(cfg_path)
    sources: dict[str, str] = {}

    def pick(env_key: str, file_key: str, default: Any) -> Any:
        if env.get(env_key, "").strip():
            sources[file_key] = "env"
            return env[env_key].strip()
        if file_key in file_data:
            sources[file_key] = "file"
            return file_data[file_key]
        sources[file_key] = "default"
        return default

    accounts = _split_accounts(pick(ENV_ACCOUNTS, "accounts", []))
    mode = str(pick(ENV_MODE, "mode", DEFAULT_MODE)).lower()
    if mode not in MODES:
        raise ConfigError(
            f"mode 只能是 {' / '.join(MODES)}，收到 {mode!r}",
            hint=f"{ENV_MODE} must be one of {', '.join(MODES)}",
        )
    confirm = str(pick(ENV_CONFIRM, "confirm", DEFAULT_CONFIRM)).lower()
    if confirm not in CONFIRM_MODES:
        raise ConfigError(
            f"confirm 只能是 {' / '.join(CONFIRM_MODES)}，收到 {confirm!r}",
            hint=f"{ENV_CONFIRM} must be one of {', '.join(CONFIRM_MODES)}",
        )
    download = pick(ENV_DOWNLOAD_DIR, "download_dir", None)
    download_dir = Path(str(download)).expanduser() if download else default_download_dir()
    roots = _split_roots(pick(ENV_ATTACHMENT_ROOTS, "attachment_roots", []))
    return Config(
        accounts=tuple(accounts),
        mode=mode,
        confirm=confirm,
        download_dir=download_dir,
        attachment_roots=roots,
        path=cfg_path,
        sources=sources,
    )


def add_account_to_file(path: Path, address: str, *, make_default: bool = False) -> list[str]:
    data = read_config_file(path)
    accounts = _split_accounts(data.get("accounts", []))
    if address in accounts:
        accounts.remove(address)
        accounts.insert(0 if make_default else len(accounts), address)
    elif make_default:
        accounts.insert(0, address)
    else:
        accounts.append(address)
    data["accounts"] = accounts
    write_config_file(path, data)
    return accounts


def remove_account_from_file(path: Path, address: str) -> bool:
    data = read_config_file(path)
    accounts = _split_accounts(data.get("accounts", []))
    if address not in accounts:
        return False
    accounts.remove(address)
    data["accounts"] = accounts
    write_config_file(path, data)
    return True
