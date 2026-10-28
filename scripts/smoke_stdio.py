"""Smoke test for an installed build: MCP stdio initialize + tools/list round-trip.

Usage: python scripts/smoke_stdio.py [command ...]
Default command is ``netease-mail-mcp-server`` (must be on PATH). Needs no mail account.
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

EXPECTED = {
    "list_accounts",
    "list_folders",
    "search_messages",
    "read_message",
    "get_thread",
    "download_attachment",
    "wait_for_message",
    "create_draft",
    "send_email",
    "manage_messages",
}


async def main(command: list[str]) -> int:
    env = {**os.environ, "NETEASE_MAIL_MODE": "full"}
    with tempfile.TemporaryDirectory() as tmp:
        env["NETEASE_MAIL_CONFIG"] = os.path.join(tmp, "config.toml")
        env.pop("NETEASE_MAIL_ACCOUNTS", None)
        params = StdioServerParameters(command=command[0], args=command[1:], env=env)
        async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
            init = await asyncio.wait_for(session.initialize(), 60)
            tools = await asyncio.wait_for(session.list_tools(), 60)
    names = {t.name for t in tools.tools}
    print(f"server: {init.serverInfo.name} {init.serverInfo.version}; tools: {sorted(names)}")
    if names != EXPECTED:
        print(f"unexpected tools: missing={EXPECTED - names} extra={names - EXPECTED}")
        return 1
    return 0


if __name__ == "__main__":
    cmd = sys.argv[1:] or ["netease-mail-mcp-server"]
    sys.exit(asyncio.run(main(cmd)))
