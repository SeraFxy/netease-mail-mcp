"""Read-only benchmark against real accounts (no writes, no sends).

Usage: uv run python scripts/benchmark.py you@163.com [other@126.com ...]
       NETEASE_MAIL_BENCH_ACCOUNTS=you@163.com uv run python scripts/benchmark.py

For each account it measures (wall clock, one warm-up pass excluded):
login+ID, LIST, newest-20 listing (server-side only), keyword search that matches nothing
(forces a full client-side scan) over max_scan=500 and 2000, reading the newest message,
and the server-side SEARCH criteria that NetEase supports. Output is JSON.
"""

from __future__ import annotations

import json
import os
import statistics
import sys
import time
from collections.abc import Callable
from datetime import date, timedelta
from typing import Any

from netease_mail_mcp.credentials import CredentialStore
from netease_mail_mcp.ids import decode_id
from netease_mail_mcp.imap import open_session
from netease_mail_mcp.reading import read_message
from netease_mail_mcp.search import Filters, imap_date, resolve_search_folders, search


def timed(fn: Callable[[], Any], runs: int = 3) -> tuple[Any, float]:
    fn()  # warm-up (server-side caches)
    samples = []
    result = None
    for _ in range(runs):
        start = time.perf_counter()
        result = fn()
        samples.append(time.perf_counter() - start)
    return result, round(statistics.median(samples), 3)


def bench(account: str, store: CredentialStore) -> dict[str, Any]:
    out: dict[str, Any] = {"account": account}
    code = store.get(account)
    start = time.perf_counter()
    session = open_session(account, code)
    out["login_id_s"] = round(time.perf_counter() - start, 3)
    try:
        _, out["list_folders_s"] = timed(lambda: session.folders(refresh=True))
        inbox = resolve_search_folders(session, "INBOX")
        out["inbox_messages"] = session.open_folder(inbox[0]).exists
        newest, out["newest20_s"] = timed(
            lambda: search(session, inbox, Filters.build(), limit=20, max_scan=500)
        )
        for scan in (500, 2000):
            result, seconds = timed(
                lambda scan=scan: search(
                    session,
                    inbox,
                    Filters.build(query="zz-no-such-term-zz"),
                    limit=20,
                    max_scan=scan,
                ),
                runs=2,
            )
            out[f"keyword_scan_{scan}"] = {"s": seconds, "scanned": result["scanned"]}
        if newest["messages"]:
            ref = decode_id(newest["messages"][0]["id"])
            _, out["read_newest_s"] = timed(
                lambda: read_message(
                    session, ref, max_chars=20000, include_html=False, mark_read=False
                )
            )
        session.open_folder(inbox[0])
        since = imap_date(date.today() - timedelta(days=14))
        criteria = {}
        for name, crit in (
            ("ALL", ["ALL"]),
            ("SINCE", ["SINCE", since]),
            ("UNSEEN", ["UNSEEN"]),
            ("SEEN", ["SEEN"]),
            ("FLAGGED", ["FLAGGED"]),
            ("UNFLAGGED", ["UNFLAGGED"]),
        ):
            uids, seconds = timed(lambda crit=crit: session.uid_search(crit), runs=1)
            criteria[name] = {"count": len(uids), "s": seconds}
        out["server_search"] = criteria
        out["capabilities"] = sorted(session.capabilities)
    finally:
        session.close()
    return out


def main() -> int:
    accounts = sys.argv[1:] or [
        a.strip() for a in os.environ.get("NETEASE_MAIL_BENCH_ACCOUNTS", "").split(",") if a.strip()
    ]
    if not accounts:
        print(__doc__)
        return 2
    store = CredentialStore()
    print(json.dumps([bench(a, store) for a in accounts], ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
