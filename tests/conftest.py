from __future__ import annotations

import json
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from netease_mail_mcp.config import Config
from netease_mail_mcp.credentials import CredentialStore
from netease_mail_mcp.imap import ConnectionPool
from netease_mail_mcp.safety import ConfirmationTokens
from netease_mail_mcp.server import Services, create_server

from .fakeimap import FakeImapServer
from .samples import ME

PASSWORD = "AUTHCODEABCDEFGH"


class MemoryBackend:
    name = "memory"

    def __init__(self, data: dict[str, str] | None = None) -> None:
        self.data = dict(data or {})

    def get(self, address: str) -> str | None:
        return self.data.get(address)

    def exists(self, address: str) -> bool:
        return address in self.data

    def set(self, address: str, code: str) -> None:
        self.data[address] = code

    def delete(self, address: str) -> bool:
        return self.data.pop(address, None) is not None


@dataclass
class FakeSMTP:
    host: str
    port: int
    implicit: bool
    password: str
    sent: list[dict[str, Any]]
    deliver: Any = None
    refuse: tuple[str, ...] = ()

    def login(self, user: str, password: str) -> None:
        import smtplib

        if password != self.password:
            raise smtplib.SMTPAuthenticationError(535, b"Error: authentication failed")

    def send_message(self, msg: Any, from_addr: str, to_addrs: list[str]) -> dict[str, Any]:
        self.sent.append(
            {"from": from_addr, "to": list(to_addrs), "raw": msg.as_bytes(), "bcc": msg.get("Bcc")}
        )
        if self.deliver:
            self.deliver(msg.as_bytes())
        return {addr: (550, b"Mailbox not found") for addr in to_addrs if addr in self.refuse}

    def quit(self) -> None:
        pass


@dataclass
class SmtpRecorder:
    password: str = PASSWORD
    sent: list[dict[str, Any]] = field(default_factory=list)
    connects: list[tuple[str, int, bool]] = field(default_factory=list)
    fail_ports: tuple[int, ...] = ()
    deliver: Any = None
    refuse: tuple[str, ...] = ()

    def __call__(self, host: str, port: int, implicit: bool, timeout: float) -> FakeSMTP:
        self.connects.append((host, port, implicit))
        if port in self.fail_ports:
            raise TimeoutError("timed out")
        return FakeSMTP(host, port, implicit, self.password, self.sent, self.deliver, self.refuse)


@pytest.fixture(autouse=True)
def _isolated_appdata(tmp_path_factory: pytest.TempPathFactory, monkeypatch) -> None:
    """%APPDATA% / %LOCALAPPDATA% are never attachable; on Windows CI the pytest temp dir
    lives under %LOCALAPPDATA%, so point both somewhere the tests do not write files."""
    fake = tmp_path_factory.getbasetemp() / "appdata-unused"
    monkeypatch.setenv("APPDATA", str(fake / "Roaming"))
    monkeypatch.setenv("LOCALAPPDATA", str(fake / "Local"))


@pytest.fixture
def imap_server() -> FakeImapServer:
    return FakeImapServer(user=ME, password=PASSWORD)


@pytest.fixture
def store() -> CredentialStore:
    return CredentialStore(MemoryBackend({ME: PASSWORD}), environ={})


@pytest.fixture
def pool(imap_server: FakeImapServer, store: CredentialStore) -> Iterator[ConnectionPool]:
    p = ConnectionPool(store, factory=imap_server.connect, timeout=5)
    yield p
    p.close_all()


@pytest.fixture
def smtp() -> SmtpRecorder:
    return SmtpRecorder()


def make_services(
    pool: ConnectionPool,
    store: CredentialStore,
    smtp: SmtpRecorder,
    tmp_path: Path,
    *,
    mode: str = "full",
    confirm: str = "token",
    accounts: tuple[str, ...] = (ME,),
    clock: Any = None,
) -> Services:
    config = Config(accounts=accounts, mode=mode, confirm=confirm, download_dir=tmp_path / "dl")
    tokens = ConfirmationTokens(clock=clock) if clock else ConfirmationTokens()
    return Services(
        config=config,
        credentials=store,
        pool=pool,
        tokens=tokens,
        smtp_factory=smtp,
        sleep=lambda s: None,
    )


@pytest.fixture
def services(
    pool: ConnectionPool, store: CredentialStore, smtp: SmtpRecorder, tmp_path: Path
) -> Services:
    return make_services(pool, store, smtp, tmp_path)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class ToolClient:
    def __init__(self, session: Any) -> None:
        self.session = session

    async def call(self, name: str, **arguments: Any) -> dict[str, Any]:
        result = await self.session.call_tool(name, arguments)
        text = result.content[0].text
        if result.isError:
            raise ToolCallError(text)
        return json.loads(text)

    async def names(self) -> list[str]:
        return [t.name for t in (await self.session.list_tools()).tools]


class ToolCallError(Exception):
    pass


@asynccontextmanager
async def connect(
    services: Services, elicitation_callback: Any = None
) -> AsyncIterator[ToolClient]:
    from mcp.shared.memory import create_connected_server_and_client_session

    server = create_server(services)
    async with create_connected_server_and_client_session(
        server, elicitation_callback=elicitation_callback
    ) as session:
        yield ToolClient(session)
