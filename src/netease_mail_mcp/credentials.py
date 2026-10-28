"""授权码 storage.

Layout (shared with the earlier private prototype, so existing entries keep working):
service ``netease-mail-mcp``, username = lower-case email address.

Backends:

* macOS: the login keychain through Apple's ``/usr/bin/security`` tool. Keychain items carry
  an access-control list naming the apps allowed to read them without a prompt. Items made
  by ``security add-generic-password`` (the prototype's documented setup) trust only
  ``security``, so reading them through Python ``keyring`` pops a GUI dialog and blocks a
  background MCP server. Using ``security`` for both reading and writing avoids that dialog and
  survives Python upgrades (each new interpreter binary would otherwise have to be re-approved).
* Elsewhere: the ``keyring`` package (Windows Credential Locker, Secret Service, ...).

Fallback: environment variable ``NETEASE_MAIL_AUTH_CODE__<ADDRESS>`` (address upper-cased,
every non-alphanumeric character replaced by ``_``), for headless machines and CI.

Secrets are never logged, printed, or put on a command line.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from collections.abc import Mapping
from typing import Protocol

from .errors import CredentialError

SERVICE = "netease-mail-mcp"
ENV_PREFIX = "NETEASE_MAIL_AUTH_CODE__"
SECURITY_BIN = "/usr/bin/security"
_SECURITY_NOT_FOUND = 44
_CODE_RE = re.compile(r"^[A-Za-z0-9]{4,64}$")


def env_var_name(address: str) -> str:
    return ENV_PREFIX + re.sub(r"[^A-Z0-9]", "_", address.strip().upper())


def validate_auth_code(code: str) -> str:
    code = (code or "").strip()
    if not _CODE_RE.match(code):
        raise CredentialError(
            "授权码格式不对：网易授权码是 16 位字母（不是登录密码）。",
            hint="The authorization code should be letters/digits only (NetEase issues 16 "
            "letters). It is not your account password.",
        )
    return code


class Backend(Protocol):
    name: str

    def get(self, address: str) -> str | None: ...

    def exists(self, address: str) -> bool: ...

    def set(self, address: str, code: str) -> None: ...

    def delete(self, address: str) -> bool: ...


class MacKeychainBackend:
    """Login keychain via ``/usr/bin/security`` (no Python-binary ACL prompts)."""

    name = "macos-keychain"

    def __init__(self, binary: str = SECURITY_BIN, timeout: float = 30.0) -> None:
        self.binary = binary
        self.timeout = timeout

    def _run(self, args: list[str], stdin: str | None = None) -> subprocess.CompletedProcess[str]:
        try:
            return subprocess.run(
                [self.binary, *args],
                input=stdin,
                stdin=None if stdin is not None else subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=self.timeout,
                check=False,
            )
        except subprocess.TimeoutExpired:
            raise CredentialError(
                "访问钥匙串超时：可能有系统弹窗在等待你点「允许」。",
                hint="Keychain access timed out; a macOS permission dialog may be waiting.",
            ) from None
        except OSError as exc:
            raise CredentialError(
                f"无法运行 {self.binary}：{exc}", hint=f"Cannot run {self.binary}: {exc}"
            ) from None

    def get(self, address: str) -> str | None:
        proc = self._run(["find-generic-password", "-s", SERVICE, "-a", address, "-w"])
        if proc.returncode == _SECURITY_NOT_FOUND:
            return None
        if proc.returncode != 0:
            raise CredentialError(
                f"读取钥匙串失败（security 退出码 {proc.returncode}）。",
                hint=f"Keychain read failed (security exit code {proc.returncode}).",
            )
        value = proc.stdout.rstrip("\r\n")
        return value or None

    def exists(self, address: str) -> bool:
        proc = self._run(["find-generic-password", "-s", SERVICE, "-a", address])
        return proc.returncode == 0

    def set(self, address: str, code: str) -> None:
        code = validate_auth_code(code)
        if not re.fullmatch(r"[A-Za-z0-9._%+\-@]+", address):
            raise CredentialError(f"地址含非法字符：{address!r}", hint="Invalid address")
        # Interactive mode reads the command from stdin, so the secret never appears in argv
        # (where other local users could see it via `ps`).
        command = f"add-generic-password -U -s {SERVICE} -a {address} -w {code}\n"
        proc = self._run(["-i"], stdin=command)
        if proc.returncode != 0 or not self.exists(address):
            raise CredentialError(
                "写入钥匙串失败。", hint="Could not store the code in the macOS keychain."
            )

    def delete(self, address: str) -> bool:
        proc = self._run(["delete-generic-password", "-s", SERVICE, "-a", address])
        return proc.returncode == 0


class KeyringBackend:
    """Cross-platform storage through the ``keyring`` package."""

    name = "keyring"

    def get(self, address: str) -> str | None:
        import keyring
        from keyring.errors import KeyringError

        try:
            return keyring.get_password(SERVICE, address)
        except KeyringError:
            return None

    def exists(self, address: str) -> bool:
        return bool(self.get(address))

    def set(self, address: str, code: str) -> None:
        import keyring
        from keyring.errors import KeyringError

        code = validate_auth_code(code)
        try:
            keyring.set_password(SERVICE, address, code)
        except KeyringError as exc:
            raise CredentialError(
                f"没有可用的系统钥匙串（{exc}）。可改用环境变量 {env_var_name(address)}。",
                hint=f"No usable system keyring ({exc}); set {env_var_name(address)} instead.",
            ) from None

    def delete(self, address: str) -> bool:
        import keyring
        from keyring.errors import KeyringError

        try:
            keyring.delete_password(SERVICE, address)
            return True
        except KeyringError:
            return False


def default_backend() -> Backend:
    if sys.platform == "darwin" and os.path.exists(SECURITY_BIN):
        return MacKeychainBackend()
    return KeyringBackend()


class CredentialStore:
    """Looks up 授权码: system keychain first, then the environment variable fallback."""

    def __init__(
        self, backend: Backend | None = None, environ: Mapping[str, str] | None = None
    ) -> None:
        self.backend = backend or default_backend()
        self._environ = environ

    @property
    def environ(self) -> Mapping[str, str]:
        return os.environ if self._environ is None else self._environ

    def lookup(self, address: str) -> tuple[str | None, str | None]:
        code = self.backend.get(address)
        if code:
            return code, self.backend.name
        env_value = self.environ.get(env_var_name(address), "").strip()
        if env_value:
            return env_value, "env"
        return None, None

    def get(self, address: str) -> str:
        code, _ = self.lookup(address)
        if not code:
            raise CredentialError(
                f"没有找到 {address} 的授权码：运行 `netease-mail-mcp-server setup --email "
                f"{address}`，或设置环境变量 {env_var_name(address)}。",
                hint=f"No authorization code stored for {address}. Run setup or set "
                f"{env_var_name(address)}.",
            )
        return code

    def source(self, address: str) -> str | None:
        """Where the code would come from, without reading the secret when avoidable."""
        if self.backend.exists(address):
            return self.backend.name
        if self.environ.get(env_var_name(address), "").strip():
            return "env"
        return None

    def set(self, address: str, code: str) -> str:
        self.backend.set(address, code)
        return self.backend.name

    def delete(self, address: str) -> bool:
        return self.backend.delete(address)
