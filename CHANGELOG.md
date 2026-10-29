# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/).

## [0.1.0] - 2026-10-29

### Added
- MCP server for NetEase personal mail (163 / 126 / yeah.net / 188) with 10 tools: `list_accounts`,
  `list_folders`, `search_messages`, `read_message`, `get_thread`, `download_attachment`,
  `wait_for_message`, `create_draft`, `send_email`, `manage_messages`.
- Permission modes `readonly` / `draft` / `full`; tools outside the mode are not registered.
- Two-phase send / move / trash with MCP elicitation, or HMAC single-use confirmation tokens bound
  to the previewed content as the fallback.
- NetEase handling: IMAP ID after login, local keyword filtering (no server-side keyword search),
  receive-window detection, `UID COPY` + `UID EXPUNGE` of only the moved UIDs when MOVE is missing,
  GBK/GB18030 and mislabelled-charset decoding.
- CLI: `setup`, `doctor`, `config <client>`, `accounts list|remove`.
- Credentials in the OS keychain (macOS via `/usr/bin/security`, Windows Credential Manager,
  Linux Secret Service), with an environment-variable fallback.
- Agent skill (`skills/netease-mail`), MCP Registry `server.json`, CI and release workflows.
- `NETEASE_MAIL_ATTACHMENT_ROOTS`: optional allowlist of folders attachments may come from.

### Security
- `download_attachment` writes only inside the download directory or its subfolders (symlinks
  resolved; UNC and drive-relative paths refused), creates files exclusively with mode 0600 and
  folders with 0700, bounds the unique-name search, and marks files as downloaded (macOS
  quarantine attribute, Windows Zone.Identifier). On Windows, which has no `O_NOFOLLOW`,
  existing entries, dangling links included, are skipped by name before creating.
- Outgoing attachments are refused from hidden paths under the home directory, `~/Library`
  (except iCloud Drive, cloud-drive folders and folders listed explicitly in
  `NETEASE_MAIL_ATTACHMENT_ROOTS`), compared by the actual files on disk so letter case does not
  matter,
  `%APPDATA%` / `%LOCALAPPDATA%`, `/proc` `/sys` `/dev` `/etc`, network paths, non-regular files
  and files whose content does not match their size.
- Elicitation is strict: once a client advertises it, errors, invalid answers and accepting
  without approving are declines; tokens are only used for clients without elicitation.
- Send previews show every recipient (incl. Bcc), body and HTML-as-text up to 8000 characters
  with the number of characters not shown, a warning when HTML and text differ, and attachment
  paths and sizes; mail-derived preview text (bodies, names, subjects, attachment names) is
  fenced as untrusted.
- `send_email(draft_id)` only sends messages in Drafts carrying `\Draft`, and previews the
  draft's real From and Reply-To.
- `wait_for_message` matches `from` against sender addresses, not display names; a full address
  must match exactly.

### Fixed
- Timeouts and resets during login, ID and CAPABILITY are reported as errors and the connection
  is closed; the STARTTLS SMTP path closes its socket on failure.
- After SMTP accepted a message, failures while saving to Sent or deleting the draft are warnings
  on a "sent" result, never errors; partially refused recipients are a warning, and the draft is
  kept unless a copy is confirmed in Sent.
- Without MOVE, UIDPLUS is checked before any COPY or STORE.
- Folders created after the folder list was cached are found (LIST is re-read once on a miss).
- `wait_for_message` reconnects after a dropped connection and keeps its baseline; if it cannot,
  it returns an `interrupted` result instead of an error.
- Recipients like `"Zhang, San" <z@x.com>` are parsed correctly.
- `text_excerpt_truncated` counts characters (it was wrong for GBK attachments).
- HTML-to-text conversion is linear on nested links and limited to 2 MB of HTML.
- Confirmation tokens are checked and consumed in one locked step.

[0.1.0]: https://github.com/SeraFxy/netease-mail-mcp/releases/tag/v0.1.0
