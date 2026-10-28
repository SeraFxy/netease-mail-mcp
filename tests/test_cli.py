from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from netease_mail_mcp import cli
from netease_mail_mcp.config import load_config

from .conftest import MemoryBackend


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> MemoryBackend:
    monkeypatch.setenv("NETEASE_MAIL_CONFIG", str(tmp_path / "config.toml"))
    monkeypatch.delenv("NETEASE_MAIL_ACCOUNTS", raising=False)
    backend = MemoryBackend()
    monkeypatch.setattr("netease_mail_mcp.credentials.default_backend", lambda: backend)
    return backend


@pytest.mark.parametrize("client", cli.CLIENTS)
def test_config_snippets(client: str, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["config", client, "--mode", "draft"]) == 0
    out = capsys.readouterr().out
    assert "netease-mail" in out and "NETEASE_MAIL_MODE" in out
    if client in ("cursor", "claude-desktop"):
        body = json.loads(out.split("\n", 1)[1])
        assert body["mcpServers"]["netease-mail"]["env"] == {"NETEASE_MAIL_MODE": "draft"}


def test_setup_non_interactive_then_remove(env: MemoryBackend, monkeypatch, capsys) -> None:
    monkeypatch.setattr("sys.stdin", io.StringIO("ABCDEFGHIJKLMNOP\n"))
    code = cli.main(["setup", "--email", "Me@163.com", "--auth-code-stdin", "--no-verify"])
    out = capsys.readouterr().out
    assert code == 0 and env.data == {"me@163.com": "ABCDEFGHIJKLMNOP"}
    assert "ABCDEFGHIJKLMNOP" not in out  # never echo the secret
    assert "claude mcp add" in out and "[mcp_servers.netease-mail]" in out
    assert load_config().accounts == ("me@163.com",)
    assert cli.main(["accounts", "list"]) == 0
    assert "me@163.com  credential: memory" in capsys.readouterr().out
    assert cli.main(["accounts", "remove", "me@163.com"]) == 0
    assert env.data == {} and load_config().accounts == ()


def test_setup_rejects_bad_code_and_domain(env: MemoryBackend, monkeypatch) -> None:
    monkeypatch.setattr("sys.stdin", io.StringIO("not a code!\n"))
    assert cli.main(["setup", "--email", "me@163.com", "--auth-code-stdin", "--no-verify"]) == 2
    assert cli.main(["setup", "--email", "me@gmail.com", "--auth-code-stdin"]) == 2
    assert env.data == {}


def test_doctor_reports_missing_credential(env: MemoryBackend, monkeypatch, capsys) -> None:
    monkeypatch.setenv("NETEASE_MAIL_ACCOUNTS", "me@163.com")
    assert cli.main(["doctor", "--json"]) == 1
    report = json.loads(capsys.readouterr().out)
    checks = {c["check"]: c["status"] for c in report["accounts"][0]["checks"]}
    assert checks == {"provider": "ok", "credential": "fail"}
