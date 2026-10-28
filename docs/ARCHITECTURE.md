# Architecture

A stateless stdio MCP server. Every tool call opens (or reuses, within the process) a short-lived
IMAP/SMTP connection to NetEase, does its work and returns JSON. There is no cache database, no
index and no background daemon.

## Module map (`src/netease_mail_mcp/`)

| Module | Role |
|---|---|
| `cli.py` | `serve` (default), `setup`, `doctor`, `config <client>`, `accounts` |
| `config.py` | Accounts, mode, limits; environment overrides a flat TOML config file |
| `providers.py` | Domain to IMAP/SMTP host table |
| `credentials.py` | OS credential store (macOS `/usr/bin/security`, `keyring` elsewhere), env fallback |
| `imap.py` | Connect, login, `ID`, EXAMINE/SELECT, modified UTF-7, LIST parsing, batched UID FETCH, pool |
| `mime.py` | Defensive parsing (GBK/GB18030, RFC 2047/2231, mislabelled charsets), HTML to text, composing |
| `ids.py` | Opaque message ids: account, folder, UIDVALIDITY, UID |
| `search.py` | Server-side date/flag criteria plus local, batched header filtering |
| `reading.py` | read / thread / download / list folders |
| `actions.py` | Drafts, SMTP send, move/trash/mark, wait-for-message, code extraction |
| `safety.py` | Modes, limits, HMAC confirmation tokens, elicitation, untrusted-content wrapping |
| `server.py` | FastMCP tool registration, gated by mode |
| `errors.py` | Bilingual, actionable error types |

## Design decisions

- **Stateless, no cache DB.** Mail is read live. Nothing about message content persists on disk,
  which keeps the privacy story simple and avoids stale-cache bugs. The cost is latency on large
  mailboxes, bounded by `max_scan` (see the README benchmarks).
- **ID before SELECT.** NetEase rejects SELECT with `Unsafe Login` unless the client sent IMAP `ID`
  after login. The session layer always sends it and re-reads CAPABILITY afterwards.
- **Local keyword filtering.** NetEase's server-side keyword SEARCH is unusable, so only date and
  flag criteria go to the server. Headers are then fetched in UID batches (about 200 per FETCH,
  headers plus BODYSTRUCTURE in one round trip) and matched locally, newest first, until the limit
  or scan cap is reached. Results report `scanned`, `truncated` and `body_searched: false`.
- **Opaque ids bound to UIDVALIDITY and account.** Stale ids are rejected instead of acting on the
  wrong message; sequence numbers are never used.
- **Two-phase writes.** Send, move and trash first produce a canonical preview. Execution either
  goes through MCP elicitation (a human answers in the client) or requires a single-use HMAC token
  over the exact preview, with a 10 minute TTL. Anything that differs from the preview is refused.
  A token forces preview-before-execute but cannot prove a human approved it. Tokens are used
  only when the client does not advertise elicitation; once it does, anything but an explicit
  approval is a decline. Previews carry the full recipient list, body and HTML-as-text (8000
  characters each, with the count not shown) and attachment paths. After SMTP accepts a
  message, follow-up failures (Sent copy, draft deletion) are warnings, never errors.
- **Mode gating at registration.** Tools above the configured mode are not registered, and each call
  re-checks the mode.
- **Safe expunge.** Without MOVE, move/trash is `UID COPY` + `UID STORE +FLAGS.SILENT (\Deleted)` +
  `UID EXPUNGE <only those UIDs>` (UIDPLUS, checked before the COPY). A bare `EXPUNGE` is never
  sent. There is no permanent delete tool.
- **Filesystem confinement.** Downloads are created with `O_EXCL | O_NOFOLLOW`, mode 0600, only
  inside the (symlink-resolved) download directory. Attachments are read only from regular files
  outside hidden home paths, `~/Library` (iCloud Drive and cloud drives excepted), `%APPDATA%`,
  system directories and network paths, compared by device and inode so letter case does not matter,
  optionally limited to `NETEASE_MAIL_ATTACHMENT_ROOTS`.
- **No injection into protocol commands.** Untrusted strings are quoted for IMAP and rejected if
  they contain CR/LF; outgoing headers are validated.
- **Blocking I/O off the event loop.** Tools are async; IMAP/SMTP work runs in threads, serialised
  per account. A timed-out connection is marked broken and never reused; `wait_for_message`
  replaces it (up to 3 reconnects) and keeps its UIDNEXT baseline.

## Tests

`tests/fakeimap.py` is a real IMAP server over a socket pair that reproduces NetEase behaviour (ID
required, empty keyword SEARCH, no MOVE, literals in responses), so tests use genuine `imaplib`
clients. `pytest -m live` runs opt-in checks against your own accounts.
