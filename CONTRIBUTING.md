# Contributing

Issues and pull requests are welcome.

## Setup

```bash
git clone https://github.com/SeraFxy/netease-mail-mcp
cd netease-mail-mcp
uv sync
```

## Checks

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy src
uv run pytest
```

The offline tests run against `tests/fakeimap.py`, a small real IMAP server on a socket pair that
reproduces NetEase quirks. New behaviour needs a test there.

## Live tests (opt-in)

`pytest -m live` talks to real NetEase accounts and is never run in CI. You need your own accounts
with credentials already set up via `setup`:

```bash
NETEASE_MAIL_LIVE_ACCOUNTS=you@163.com uv run pytest -m live
```

They only read, plus create and delete their own draft. `scripts/benchmark.py` is read-only too and
takes accounts as arguments or from `NETEASE_MAIL_BENCH_ACCOUNTS`.

Never commit real addresses, message subjects, UIDs or authorization codes.
