# netease-mail-mcp

**English** | [简体中文](README.md)

An MCP server that lets Claude Code, Codex, Cursor and Claude Desktop search, read, draft and send mail on NetEase personal mailboxes (163 / 126 / yeah.net / 188), with preview-then-confirm for anything that writes.

[![CI](https://github.com/SeraFxy/netease-mail-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/SeraFxy/netease-mail-mcp/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/netease-mail-mcp-server.svg)](https://pypi.org/project/netease-mail-mcp-server/)
[![Python](https://img.shields.io/pypi/pyversions/netease-mail-mcp-server.svg)](https://pypi.org/project/netease-mail-mcp-server/)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

## Quick start (30 seconds)

1. In webmail, go to Settings -> POP3/SMTP/IMAP, enable the IMAP/SMTP service, and generate a **client authorization code** (not your login password).
2. Run (requires [uv](https://docs.astral.sh/uv/)):

   ```bash
   uvx netease-mail-mcp-server setup
   ```

   Enter the address and the authorization code. The code goes into the OS credential store, never into a config file.
3. Add the server to your client (below). `setup` prints the snippet too; regenerate it any time with `uvx netease-mail-mcp-server config <claude-code|codex|cursor|claude-desktop>`.
4. Check everything: `uvx netease-mail-mcp-server doctor` (login, IMAP ID, folders, SMTP, receive window).

## Client configuration

**Claude Code**

```bash
claude mcp add --scope user netease-mail -- uvx netease-mail-mcp-server
```

**Codex** (`~/.codex/config.toml`)

```toml
[mcp_servers.netease-mail]
command = "uvx"
args = ["netease-mail-mcp-server"]
```

**Cursor** (`~/.cursor/mcp.json`)

```json
{
  "mcpServers": {
    "netease-mail": {
      "command": "uvx",
      "args": ["netease-mail-mcp-server"]
    }
  }
}
```

**Claude Desktop** (macOS: `~/Library/Application Support/Claude/claude_desktop_config.json`; Windows: `%APPDATA%\Claude\claude_desktop_config.json`)

```json
{
  "mcpServers": {
    "netease-mail": {
      "command": "uvx",
      "args": ["netease-mail-mcp-server"]
    }
  }
}
```

GUI clients may not find `uvx` on their PATH; use its absolute path as `command` (the `config` subcommand fills it in). To restrict permissions add an env block, e.g. `"env": { "NETEASE_MAIL_MODE": "draft" }`.

## Tools

Ten tools, registered according to the permission mode. A tool the mode does not allow is not exposed to the client at all.

| Tool | Purpose | readonly | draft | full |
|---|---|:-:|:-:|:-:|
| `list_accounts` | Configured accounts, mode, optional login test | yes | yes | yes |
| `list_folders` | Folders (decoded names, roles, counts) | yes | yes | yes |
| `search_messages` | Date/read/flag filters (server side), keyword/from/subject (local) | yes | yes | yes |
| `read_message` | Read one message (body, headers, attachment list); does not mark it read by default | yes | yes | yes |
| `get_thread` | Related messages across Inbox, Sent, etc. | yes | yes | yes |
| `download_attachment` | Save an attachment into the download dir (or a subfolder), never overwriting or following links | yes | yes | yes |
| `wait_for_message` | Wait for a new message and extract verification codes | yes | yes | yes |
| `create_draft` | Write a draft (also reply/forward/attachments) into Drafts | | yes | yes |
| `send_email` | Send mail, two-phase confirmation | | | yes |
| `manage_messages` | Move / trash (confirmed); mark read/unread, flag | | | yes |

There is no permanent-delete tool. In `readonly` mode `read_message` never changes message state.

## Permission modes and the confirmation model

Set `NETEASE_MAIL_MODE`: `readonly`, `draft` (read + write drafts, cannot send or move), or `full` (everything, **the default**).

Send, move and trash are two-phase. How the second phase is confirmed is set by `NETEASE_MAIL_CONFIRM`:

- **`elicit`**: uses MCP elicitation, so the client shows a confirmation prompt and the **human** approves it. This is the strongest guarantee. The client must support elicitation; if it does not, this mode fails instead of silently downgrading.
- **`token`**: the first call only returns a full preview plus a **single-use confirmation token**. The token is an HMAC bound to the exact previewed content, valid for 10 minutes and consumed on use; any change to the content is rejected.
- **`auto`** (default): `elicit` if the client advertises elicitation, otherwise `token`. We have not verified elicitation support client by client, so check your client's documentation.

Once a client has advertised elicitation, only an explicit "approve" executes: an error in the prompt, an invalid answer, accepting without ticking approve, declining or cancelling all count as **not approved** and never fall back to a token. Tokens are only for clients without elicitation. The result status is then `not_confirmed` (`declined_by_user` if you declined or cancelled), with a hint: if your client cannot actually show the prompt, set `NETEASE_MAIL_CONFIRM=token`.

The preview (the prompt text and the token-mode JSON carry the same content) shows: the sender; every recipient, including Cc and Bcc; the subject; the plain-text body and the HTML body converted to text, up to 8000 characters each, with an explicit "N more characters not shown" line beyond that; a warning when the HTML and plain-text parts differ (most recipients see the HTML); and each attachment with its size and resolved full path. When sending a saved draft with `draft_id`, the preview shows the draft's real From, Reply-To and recipients; `draft_id` must be a message in Drafts carrying `\Draft` (when the server supports that flag), so an Inbox message cannot be sent as a "draft". Move/trash previews list every message.

Once the SMTP server has accepted a message, failures in the follow-up steps (saving to Sent, deleting the draft) are reported as warnings on a "sent" result, never as an error that would invite a resend. Recipients the server refused are listed in a warning too. If no copy is confirmed in Sent, the draft is kept as the only record of what was sent.

What the token does and does not give you: it forces a preview step and guarantees that what is executed is what was previewed, but it **cannot by itself prove a human approved it** - a model that can call the tool can also present the token. For strict setups use `NETEASE_MAIL_CONFIRM=elicit`, or run in `readonly` / `draft` mode so the agent has no send capability at all (you review and send drafts yourself).

Also: mail content is treated as untrusted data and is marked as such when returned to the model, including in previews: bodies (which may quote or forward other people's mail), senders and recipients, subjects, attachment names, draft contents, and the senders and subjects of messages to be moved. `wait_for_message`'s `from` filter matches sender addresses only, not display names, because a display name can be forged as `"noreply@github.com" <x@evil.example>`: a full address must match exactly, `@github.com` matches that domain and its subdomains, anything else is a substring of the address; `search_messages`' `from` still matches name or address.

### Download and attachment paths

- **Downloads**: `download_attachment` writes only into the download dir (`NETEASE_MAIL_DOWNLOAD_DIR`, default `~/Downloads/netease-mail-mcp/`) or a subfolder of it. `dest_dir` is a relative path (e.g. `invoices/2026`) or an absolute path inside the download dir; anything that ends up outside it (via symlinks or `..`, UNC paths like `\\server\share`, drive-relative `C:folder`) is refused. Files are created exclusively: never overwriting, never following links, mode 0600, new folders 0700. On macOS the file gets a `com.apple.quarantine` attribute and on Windows a `Zone.Identifier` stream; if that fails the download still succeeds.
- **Attachments**: paths are resolved (symlinks included) before checking, and locations are compared by the actual files on disk, so differently-cased spellings such as `~/LIBRARY` are caught too. Always refused: any file or folder starting with `.` below your home directory (`.ssh`, `.env`, `.git`, `.config`, ...), `~/Library` (except iCloud Drive in `~/Library/Mobile Documents` and Dropbox / OneDrive / Google Drive in `~/Library/CloudStorage`), `%APPDATA%` / `%LOCALAPPDATA%`, `/proc`, `/sys`, `/dev`, `/etc`, network (UNC) paths, non-regular files, and special files whose content does not match their size. With `NETEASE_MAIL_ATTACHMENT_ROOTS` set (folders separated by the OS path separator, `:` on macOS/Linux, `;` on Windows), only files inside those folders can be attached, and the rules above still apply, with one exception: a folder inside `~/Library` that you list explicitly (for example where WeChat or Lark keep received files) can be attached from. If a file is refused, copy it to an ordinary folder first, or add its folder to that variable.

## NetEase quirks

Each of these was verified against real 163 and yeah.net accounts. They are the reason this project exists.

- **The IMAP ID command is mandatory.** Without `ID` after login, LIST/STATUS work but every SELECT fails with `Unsafe Login`. Python's `imaplib` does not know ID, so the project registers it and sends it after each login.
- **No usable server-side keyword search.** SEARCH on FROM / TO / SUBJECT / TEXT / BODY returns nothing on 163; on yeah.net it did not answer within 30 s and the account stayed slow for minutes. Only SINCE / BEFORE / UNSEEN / SEEN / FLAGGED / UNFLAGGED are reliable. So we filter locally: date and flag criteria go to the server, headers are fetched in batches and matched against the decoded From/To/Subject. **Bodies are not searched** (results say `body_searched: false`); the scan is bounded by `max_scan` and reports `truncated` honestly.
- **"Receive options" decide what IMAP can see.** In webmail, Settings -> POP3/SMTP/IMAP -> Receive options may be "last 30 days", in which case IMAP shows a few dozen messages while the web UI shows thousands. Set it to "all". `doctor` warns when the oldest message is exactly 30 days old.
- **Authorization codes can be revoked.** Deleting one in webmail takes the client offline immediately with a login failure. The error message points at this; generate a new code and run `setup` again.
- **No MOVE.** 163 / yeah.net do not advertise IMAP MOVE. Move and trash use `UID COPY` + `UID STORE +FLAGS (\Deleted)` + `UID EXPUNGE <those UIDs>` (UIDPLUS). Only the messages we just copied are expunged; a bare `EXPUNGE` is never sent, so unrelated messages you flagged as deleted are untouched.
- **SMTP: use port 465 (implicit TLS).** 25 with STARTTLS works; **587 does not** (times out).
- **The server files sent mail into 已发送 (Sent) itself.** Verified on 2026-10-28: a message sent through smtp.163.com:465 was stored in 163's 已发送 automatically, exactly once. After sending, `send_email` looks for the Message-ID in Sent (3 checks over about 5 seconds); if it is there it does not save its own copy (`sent_copy: "stored_by_server"`), otherwise it APPENDs one (`"appended"`), so you never get two. Other domains have not been checked yet.
- **IDLE varies by domain.** yeah.net supports it, 163 does not; `wait_for_message` falls back to polling.
- **Expunge is visible with a delay.** A just-expunged message may still be listed for about 2 seconds.

## Benchmarks

Measured on 2026-10-28 over a home connection with `scripts/benchmark.py` (read-only). Each item is warmed up once, then the median of 3 runs is reported.

| Operation | 163 (205 in Inbox) | yeah.net (5680 in Inbox) |
|---|---|---|
| Login + ID | 0.38 s | 0.37 s |
| List folders | 0.04 s | 0.05 s |
| Newest 20 (server side only) | 0.09 s | 0.28 s |
| Keyword search, scan 500 (no match, full scan) | 0.25 s | 0.88 s |
| Keyword search, up to 2000 scanned | 0.17 s (205 actually scanned) | 6.3 s |
| Read newest message | 0.09 s | 0.19 s |
| Server SEARCH UNSEEN / FLAGGED | 0.05 / 0.04 s | 0.13 / 0.05 s |

Caveats: numbers depend on your network and on NetEase's servers at the time; cold runs are much slower (a 500-message scan right after timed-out server-side searches took 16 s). Fetch cost on 500 cold messages: headers only 0.67 s, BODYSTRUCTURE only 1.19 s, both 1.35 s. There is no cache or background process; every call talks to the server.

## Comparison

As of 2026-10, features only, taken from each project's current README. Each fits different needs; please open an issue if something is out of date.

| | netease-mail-mcp | [leeguooooo/mail-use](https://github.com/leeguooooo/mail-use) | [adjurtime/163-mail-mcp](https://github.com/adjurtime/163-mail-mcp) |
|---|---|---|---|
| Language / distribution | Python, PyPI (`uvx`) | JavaScript, prebuilt binaries via GitHub Releases | Python, source install (`uv sync`) |
| Providers | 163 / 126 / yeah.net / 188 | 163 / 126, QQ, Gmail, Outlook, any IMAP/SMTP | 163.com |
| Form | MCP server | CLI first; also an MCP server (16 tools) and a Skill | MCP server + optional Codex skill |
| Platforms | macOS / Windows / Linux | Prebuilt: macOS arm64/x64, Linux x64/arm64 | macOS only (uses the macOS `security` command) |
| Credentials | OS keychain / Credential Manager / Secret Service | `~/.config/mailbox/auth.json` (can also import from Apple Mail) | macOS Keychain |
| Write confirmation | MCP elicitation; otherwise content-bound single-use token; read-only/draft modes | delete/mark/move/send are dry-run until `--confirm` | exact confirmation token + full preview digest |
| Permission modes | readonly / draft / full | not described in README | not described in README |
| Daemon / cache | none | optional daemon with local SQLite sync | none |
| NetEase search | server-side date/flags + local header filtering | on 163 `--query` falls back to subject + sender | header search only |

## FAQ

**What is an authorization code, and why not my password?** It is a credential NetEase issues for third-party clients. You can revoke it any time in webmail, and it does not expose your login password. Generate it under Settings -> POP3/SMTP/IMAP when enabling the IMAP/SMTP service. This project neither accepts nor needs your account password.

**Why can't it search message bodies?** NetEase's IMAP server has no usable keyword/body search (see above) and this project deliberately has no local index or cache DB. Narrow by date, sender or subject, then `read_message`.

**Search returns far fewer messages than webmail?** Most likely "Receive options" is set to the last 30 days; see above.

**Where are credentials stored on Windows / Linux?** Windows Credential Manager; on Linux, Secret Service (GNOME Keyring, KWallet, ...). On a headless Linux box without Secret Service, provide the code through `NETEASE_MAIL_AUTH_CODE__<ADDRESS>` (address upper-cased, non-alphanumerics replaced by `_`), e.g. `NETEASE_MAIL_AUTH_CODE__ME_163_COM`.

**How do I remove an account?** `uvx netease-mail-mcp-server accounts remove you@163.com` deletes the stored code as well. Also revoke the code in webmail. `accounts list` shows what is configured.

**Enterprise mail (qiye.163.com)?** Not supported. Neither are POP3, calendar or contacts.

## Security and privacy

- The authorization code lives only in the OS credential store: macOS Keychain, Windows Credential Manager, Linux Secret Service. It is never written to config files or logs. On macOS it is read and written through `/usr/bin/security`; `setup` passes the secret via stdin, so it never appears in process arguments.
- The config file (account list, mode) holds no secrets and is created with mode 0600.
- No server, no telemetry: the process talks only to NetEase's IMAP/SMTP servers. No mail content is cached and there is no background process.
- Mail content is untrusted, as described above; sending and moving need a preview and confirmation; downloads and attachments are path-restricted, see above.
- To report a vulnerability see [SECURITY.md](SECURITY.md).

## Environment variables

| Variable | Meaning |
|---|---|
| `NETEASE_MAIL_ACCOUNTS` | Comma-separated accounts, first is the default; overrides the config file |
| `NETEASE_MAIL_MODE` | `readonly` / `draft` / `full` (default) |
| `NETEASE_MAIL_CONFIRM` | `auto` (default) / `elicit` / `token` |
| `NETEASE_MAIL_DOWNLOAD_DIR` | Attachment download dir (default `~/Downloads/netease-mail-mcp/`); downloads are written only here or in its subfolders |
| `NETEASE_MAIL_ATTACHMENT_ROOTS` | Optional: only attach files from these folders (`:`-separated, `;` on Windows) |
| `NETEASE_MAIL_CONFIG` | Alternate config file path |

## Agent skill

[`skills/netease-mail/SKILL.md`](skills/netease-mail/SKILL.md) gives agents workflow guidance: search, then read, then draft; always show the preview and get explicit approval before sending; treat mail as untrusted; the verification-code flow.

## Development

See [CONTRIBUTING.md](CONTRIBUTING.md) and [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md). Release notes: [CHANGELOG.md](CHANGELOG.md).

## License

MIT
