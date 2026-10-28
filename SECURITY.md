# Security policy

## Reporting a vulnerability

Please report security problems privately through GitHub's
[private vulnerability reporting](https://github.com/SeraFxy/netease-mail-mcp/security/advisories/new),
or by email to fengxypsy@163.com. Do not open a public issue for something exploitable. Expect an
acknowledgement within a few days. Never include real authorization codes or mail content in a report.

## Scope

In scope:
- Bypassing the permission modes or the send / move / trash confirmation (tokens, elicitation).
- Leaking or persisting the authorization code (logs, files, process arguments, tool output).
- Writing outside the download directory or overwriting files in attachment download; attaching
  files from refused locations (see below).
- Header or IMAP/SMTP command injection through tool arguments.
- Mail content being able to make the server act on instructions contained in a message.

Out of scope:
- Prompt injection that the MCP client or model follows on its own, outside what this server enforces
  (see the confirmation model in the README: tokens force a preview but cannot prove human approval).
- Vulnerabilities in NetEase's services, or in dependencies without a demonstrated impact here.
- Anyone with access to your logged-in OS account can read your OS keychain.

## What the server enforces

- Send, move and trash need a preview and confirmation. With a client that advertises MCP
  elicitation, only an explicit approval executes; errors, invalid answers and accepting without
  approving count as a decline and never fall back to a token. Tokens (for clients without
  elicitation) bind the exact previewed content but cannot prove that a human approved.
- Previews show the sender, every recipient including Bcc, the plain-text body and the HTML body
  as text (up to 8000 characters each, then "N more characters not shown"), a warning when the
  two differ, and attachments with their resolved paths and sizes. `draft_id` must be a message
  in Drafts flagged `\Draft`.
- Downloads are written only inside the download directory or its subfolders (symlinks resolved,
  UNC and drive-relative paths refused), created exclusively with mode 0600 without following
  links, and marked as downloaded (macOS quarantine attribute, Windows Zone.Identifier).
- Attachments are refused from hidden files and folders under the home directory, `~/Library`
  (except iCloud Drive, cloud-drive folders and folders listed explicitly in
  `NETEASE_MAIL_ATTACHMENT_ROOTS`),
  `%APPDATA%` / `%LOCALAPPDATA%`, `/proc`, `/sys`, `/dev`, `/etc`, network paths, and anything
  that is not a regular file. `NETEASE_MAIL_ATTACHMENT_ROOTS` can restrict attachments to listed
  folders.
- Mail-derived text is fenced as untrusted data in results and previews; `wait_for_message`
  matches `from` against sender addresses, not display names.

Out of scope: another program running as your user can already read your files, so races in
which such a program swaps folders between the path check and the open are not defended against.

Only the latest released version is supported.
