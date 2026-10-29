<!-- mcp-name: io.github.SeraFxy/netease-mail-mcp -->
# netease-mail-mcp

[English](README.en.md) | **简体中文**

让 Claude Code、Codex、Cursor、Claude Desktop 安全地读写你的网易邮箱（163 / 126 / yeah.net / 188）：搜索、阅读、起草、发送，写操作先预览、再确认。

[![CI](https://github.com/SeraFxy/netease-mail-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/SeraFxy/netease-mail-mcp/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/netease-mail-mcp-server.svg)](https://pypi.org/project/netease-mail-mcp-server/)
[![Python](https://img.shields.io/pypi/pyversions/netease-mail-mcp-server.svg)](https://pypi.org/project/netease-mail-mcp-server/)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

## 30 秒上手

1. 在网页版邮箱 设置 → POP3/SMTP/IMAP 里开启「IMAP/SMTP 服务」，生成一个**客户端授权码**（不是登录密码）。
2. 在终端运行（需要 [uv](https://docs.astral.sh/uv/)）：

   ```bash
   uvx netease-mail-mcp-server setup
   ```

   按提示输入邮箱地址和授权码。授权码存进系统钥匙串，不会写进配置文件。
3. 按下面任一客户端的写法接入。`setup` 结束时也会打印对应的配置；之后可随时用 `uvx netease-mail-mcp-server config <claude-code|codex|cursor|claude-desktop>` 重新生成。
4. 自检：`uvx netease-mail-mcp-server doctor`（检查登录、IMAP ID、文件夹、SMTP、收取选项）。

## 接入各个客户端

**Claude Code**

```bash
claude mcp add --scope user netease-mail -- uvx netease-mail-mcp-server
```

**Codex**（`~/.codex/config.toml`）

```toml
[mcp_servers.netease-mail]
command = "uvx"
args = ["netease-mail-mcp-server"]
```

**Cursor**（`~/.cursor/mcp.json`）

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

**Claude Desktop**（macOS：`~/Library/Application Support/Claude/claude_desktop_config.json`；Windows：`%APPDATA%\Claude\claude_desktop_config.json`）

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

Claude Desktop 等图形客户端启动时可能找不到 `uvx`，此时把 `command` 换成 `uvx` 的绝对路径（`config` 子命令会自动填好）。要限制权限，加环境变量，例如 `"env": { "NETEASE_MAIL_MODE": "draft" }`。

## 工具

共 10 个工具，按权限模式注册：模式不允许的工具根本不会出现在客户端里。

| 工具 | 作用 | readonly | draft | full |
|---|---|:-:|:-:|:-:|
| `list_accounts` | 已配置账号、当前模式，可选测试登录 | ✓ | ✓ | ✓ |
| `list_folders` | 文件夹（已解码名称、角色、计数） | ✓ | ✓ | ✓ |
| `search_messages` | 按日期/已读/星标（服务端）与关键词、发件人、主题（本地）搜索 | ✓ | ✓ | ✓ |
| `read_message` | 读一封邮件（正文、头、附件清单），默认不改已读状态 | ✓ | ✓ | ✓ |
| `get_thread` | 取相关会话（收件箱、已发送等） | ✓ | ✓ | ✓ |
| `download_attachment` | 下载附件到下载目录（或其子目录），不覆盖、不跟随链接 | ✓ | ✓ | ✓ |
| `wait_for_message` | 等一封新邮件并提取验证码 | ✓ | ✓ | ✓ |
| `create_draft` | 写草稿（含回复、转发、附件），存入草稿箱 | | ✓ | ✓ |
| `send_email` | 发送邮件，两阶段确认 | | | ✓ |
| `manage_messages` | 移动 / 删除到垃圾箱（需确认）；标记已读、星标 | | | ✓ |

没有永久删除工具。`readonly` 下 `read_message` 也不会改变任何邮件状态。

## 权限模式与确认机制

用 `NETEASE_MAIL_MODE` 设置：`readonly`（只读）、`draft`（读 + 写草稿，不能发信和移动）、`full`（全部，**默认**）。

发送、移动、删除是两阶段操作，确认方式由 `NETEASE_MAIL_CONFIRM` 决定：

- **`elicit`**：通过 MCP 的 elicitation 机制，由客户端弹出确认界面，**由你本人**点击同意后才执行。这是最强的保证。客户端必须支持 elicitation，不支持时该模式会直接报错，不会悄悄降级。
- **`token`**：第一次调用只返回完整预览和一个**一次性确认令牌**。令牌是 HMAC，绑定刚才预览的**确切内容**，10 分钟内有效、用过即废；内容有任何改动都会被拒绝。
- **`auto`（默认）**：客户端声明支持 elicitation 就走 `elicit`，否则走 `token`。我们没有逐个核实各客户端对 elicitation 的支持情况，请以你所用客户端的文档为准。

只要客户端声明了支持 elicitation，就只有你明确勾选「同意」才会执行：弹窗出错、客户端返回无效答复、点了接受却没勾选同意、拒绝或取消，一律按**未同意**处理，不会退回到令牌。令牌只给根本不支持 elicitation 的客户端用。这时结果状态是 `not_confirmed`（你点了拒绝或取消则是 `declined_by_user`），并附带提示：如果你的客户端其实弹不出确认框，把 `NETEASE_MAIL_CONFIRM` 设成 `token`。

预览（弹窗里的文字和令牌模式下的 JSON 是同一份内容）包括：发件人；全部收件人，含抄送和密送；主题；纯文本正文和 HTML 正文转成的文本，各最多 8000 字，超出时明确写出「还有 N 个字符未显示」；HTML 和纯文本内容不一致时给出警告（收件人多半看到的是 HTML）；每个附件的文件名、大小和解析后的完整路径。用 `draft_id` 发送草稿时，预览显示草稿里真实的 From、Reply-To 和收件人；`draft_id` 必须是草稿箱里带 `\Draft` 标记的草稿（服务器支持这个标记时），收件箱里的邮件不能当草稿发出去。移动/删除的预览列出每一封邮件。

SMTP 服务器接受邮件之后，保存「已发送」或删除草稿这些后续步骤即使出错，结果也是「已发送」加警告，不会报错，免得被误以为没发出去而重发。部分收件人被服务器拒绝时也给出警告，并列出这些地址。确认「已发送」里没有副本时，草稿不删除，留作发出内容的唯一记录。

诚实地说明令牌的边界：令牌**强制模型先预览再执行**，并保证执行的就是预览的内容，但它**无法证明有人类批准过**——能调用工具的模型自己也能拿着令牌再调一次。因此对安全要求高的场景，请设置 `NETEASE_MAIL_CONFIRM=elicit`，或者直接使用 `readonly` / `draft` 模式，让 Agent 根本没有发信能力（草稿由你自己在邮箱里检查后发送）。

另外：邮件内容一律视为不可信数据，返回给模型时会加上标记，提示其中的指令不是命令；预览里的正文（可能引用或转发了别人的邮件）、发件人和收件人、主题、附件文件名、草稿内容，以及待移动邮件的发件人和主题也一样。`wait_for_message` 的 `from` 只匹配发件地址，不看显示名，因为显示名可以随便伪造成 `"noreply@github.com" <x@evil.example>`：写完整地址时要求完全一致，写 `@github.com` 匹配这个域名及其子域名，其他写法按地址里的子串匹配；`search_messages` 的 `from` 仍同时匹配显示名和地址。

### 下载和附件的路径限制

- **下载**：`download_attachment` 只会写进下载目录（`NETEASE_MAIL_DOWNLOAD_DIR`，默认 `~/Downloads/netease-mail-mcp/`）或它的子目录。`dest_dir` 写相对路径（如 `发票/2026`）或下载目录里的绝对路径；指向下载目录以外的（包括经过符号链接或 `..`、UNC 路径 `\\server\share`、`C:相对路径` 这类写法）一律拒绝。文件以独占方式新建：不覆盖已有文件、不跟随链接，权限 0600，新建的目录 0700。macOS 上给文件加 `com.apple.quarantine` 标记，Windows 上写 `Zone.Identifier`，加不上也不影响下载。
- **附件**：路径先解析符号链接再检查，并按磁盘上的实际文件判断位置，所以 `~/LIBRARY` 这类大小写不同的写法同样会被识别。以下位置一律拒绝：主目录下任何以 `.` 开头的文件或目录（`.ssh`、`.env`、`.git`、`.config` 等）、`~/Library`（iCloud 云盘所在的 `~/Library/Mobile Documents` 和 Dropbox / OneDrive / Google Drive 所在的 `~/Library/CloudStorage` 除外）、`%APPDATA%` / `%LOCALAPPDATA%`、`/proc`、`/sys`、`/dev`、`/etc`、网络路径（UNC）、非普通文件，以及读到的内容和文件大小对不上的特殊文件。设置 `NETEASE_MAIL_ATTACHMENT_ROOTS`（多个目录用系统路径分隔符隔开，macOS/Linux 是 `:`，Windows 是 `;`）后，只能附加这些目录里的文件，上面的拒绝规则照样生效；唯一的例外是明确写进这个变量、又位于 `~/Library` 里的目录（例如微信、飞书存放收到文件的目录），其中的文件可以附加。被拒时，把文件复制到普通目录再附加，或把它所在的目录加进这个变量。

## 网易邮箱的坑

这些是用真实 163 / yeah.net 账号逐项验证出来的，也是本项目存在的理由。

- **必须先发 IMAP ID。** 登录后若不发 `ID` 命令，LIST/STATUS 能用，但每次 SELECT 都会失败并提示 `Unsafe Login`。Python 的 `imaplib` 不认识 ID，本项目自行注册并在每次登录后发送。
- **服务端搜不了关键词。** 对 FROM / TO / SUBJECT / TEXT / BODY 的 SEARCH，在 163 上返回空结果；在 yeah.net 上我们实测 30 秒内不返回，之后账号还会慢上几分钟。可靠的只有 SINCE / BEFORE / UNSEEN / SEEN / FLAGGED / UNFLAGGED。所以本项目的做法是：日期和状态条件交给服务端，再批量取回邮件头，在本地对解码后的发件人、收件人、主题做匹配。**不搜正文**，结果里会明确标 `body_searched: false`；搜索范围有上限（`max_scan`），被截断时会如实报告 `truncated`。
- **「收取选项」决定 IMAP 能看到什么。** 网页版 设置 → POP3/SMTP/IMAP → 收取选项 默认可能是「最近 30 天」，此时收件箱在 IMAP 里可能只有几十封，而网页上有几千封。不是 bug，把它改成「全部」即可；`doctor` 会在发现最早的邮件正好是 30 天前时提醒你。
- **授权码可被撤销。** 在网页版删除授权码会立刻让对应客户端掉线，并报登录失败。登录失败的错误信息会指向这一点；重新生成授权码、再运行 `setup` 即可。
- **没有 MOVE。** 163/yeah.net 不支持 IMAP MOVE。移动和删除用 `UID COPY` + `UID STORE +FLAGS (\Deleted)` + `UID EXPUNGE <这些 UID>`（依赖 UIDPLUS）。只会清除我们刚复制的那几封，不会发无参数的 `EXPUNGE`，不会连带清掉你别处标了删除的邮件。
- **SMTP 用 465（隐式 TLS）。** 25 端口 + STARTTLS 可用，**587 端口不通**（超时）。
- **发出的信，服务器会自己存进「已发送」。** 2026-10-28 实测：经 smtp.163.com:465 发出的一封邮件，163 自动在「已发送」里存了一份，且只有一份。`send_email` 发送后按 Message-ID 在「已发送」里找（约 5 秒内查 3 次），找到就不再自己保存（结果里 `sent_copy: "stored_by_server"`），找不到才用 APPEND 补存一份（`"appended"`），所以不会出现两份。其他域名尚未实测。
- **IDLE 因域名而异。** yeah.net 支持，163 不支持；`wait_for_message` 在不支持时自动改为轮询。
- **删除有延迟。** 刚 EXPUNGE 的邮件可能在约 2 秒内仍被列出。

## 性能

测试环境：2026-10-28，家庭宽带，`scripts/benchmark.py`（只读，不发信不改信）。每项先预热一次，再取 3 次的中位数。

| 操作 | 163（收件箱 205 封） | yeah.net（收件箱 5680 封） |
|---|---|---|
| 登录 + ID | 0.38 s | 0.37 s |
| 列文件夹 | 0.04 s | 0.05 s |
| 最新 20 封（仅服务端） | 0.09 s | 0.28 s |
| 关键词搜索，扫描 500 封（无命中，全量扫描） | 0.25 s | 0.88 s |
| 关键词搜索，最多扫描 2000 封 | 0.17 s（实际 205 封） | 6.3 s |
| 读最新一封 | 0.09 s | 0.19 s |
| 服务端 SEARCH UNSEEN / FLAGGED | 0.05 / 0.04 s | 0.13 / 0.05 s |

说明：数字依赖网络和网易服务器当时的状态，冷启动明显更慢（一次紧接在超时的服务端搜索之后的 500 封扫描耗时 16 s）。批量取头的开销：500 封冷数据下，只取邮件头 0.67 s，只取 BODYSTRUCTURE 1.19 s，两者都取 1.35 s。这是一个本地的无缓存、无后台进程的实现，每次调用都实时访问服务器。

## 对比

截至 2026-10，仅比较功能，信息来自两个项目当前的 README；它们各有适合的场景，如有出入请提 issue 指正。

| | netease-mail-mcp | [leeguooooo/mail-use](https://github.com/leeguooooo/mail-use) | [adjurtime/163-mail-mcp](https://github.com/adjurtime/163-mail-mcp) |
|---|---|---|---|
| 语言 / 分发 | Python，PyPI（`uvx`） | JavaScript，GitHub Release 二进制 | Python，源码安装（`uv sync`） |
| 支持邮箱 | 163 / 126 / yeah.net / 188 | 163 / 126、QQ、Gmail、Outlook、任意 IMAP/SMTP | 163.com |
| 形态 | MCP 服务器 | CLI 为主，也提供 MCP 服务器（16 个工具）和 Skill | MCP 服务器 + 可选 Codex Skill |
| 平台 | macOS / Windows / Linux | 预编译二进制：macOS arm64/x64、Linux x64/arm64 | 仅 macOS（凭据用 macOS `security`） |
| 凭据存储 | 系统钥匙串 / 凭据管理器 / Secret Service | `~/.config/mailbox/auth.json`（亦可导入 Apple Mail） | macOS 钥匙串 |
| 写操作确认 | MCP elicitation；否则绑定内容的一次性令牌；另有只读/草稿模式 | 删除、标记、移动、发送默认 dry-run，加 `--confirm` 才执行 | 精确确认令牌 + 完整预览摘要 |
| 权限模式 | readonly / draft / full | 未在 README 中说明 | 未在 README 中说明 |
| 后台进程 / 缓存 | 无 | 可选常驻 daemon，本地 SQLite 同步 | 无 |
| 网易搜索的处理 | 服务端日期/状态 + 本地过滤头信息 | 163 的 `--query` 退回到只匹配主题和发件人 | 仅搜邮件头 |

## 常见问题

**授权码是什么？为什么不用我的密码？** 授权码是网易为第三方客户端单独生成的凭据，可随时在网页版撤销，不会暴露登录密码，也绕过不了你的登录保护。在 设置 → POP3/SMTP/IMAP 开启 IMAP/SMTP 服务时生成。本项目不接受、也不需要账号密码。

**为什么搜不到正文？** 网易的 IMAP 服务端不支持有效的关键词/正文搜索（见上文），而本项目不建本地索引或缓存库。需要正文时，先用日期、发件人、主题缩小范围，再用 `read_message` 读。

**搜索结果比网页上少很多？** 多半是「收取选项」设成了最近 30 天，见上文。

**Windows / Linux 的凭据存在哪？** Windows 存在凭据管理器，Linux 存在 Secret Service（GNOME Keyring、KWallet 等）。无图形桌面的 Linux 没有 Secret Service 时，可通过环境变量 `NETEASE_MAIL_AUTH_CODE__<邮箱大写，非字母数字换成下划线>` 提供授权码，例如 `NETEASE_MAIL_AUTH_CODE__ME_163_COM`。

**怎么删除一个账号？** `uvx netease-mail-mcp-server accounts remove you@163.com`，会同时删除钥匙串里的授权码。也别忘了在网页版撤销这个授权码。查看已配置账号：`accounts list`。

**支持企业邮箱（qiye.163.com）吗？** 不支持。也不支持 POP3、日历、通讯录。

## 安全与隐私

- 授权码只存在系统凭据库：macOS 钥匙串、Windows 凭据管理器、Linux Secret Service；不写入任何配置文件或日志。macOS 上通过 `/usr/bin/security` 读写，`setup` 经标准输入传递密钥，不会出现在进程参数里。
- 配置文件（账号列表、模式）权限为 0600，不含密钥。
- 本项目无服务端、无遥测，只直连网易的 IMAP/SMTP 服务器；不缓存邮件内容，没有后台进程。
- 邮件内容视为不可信，见上文；发信和移动需要预览与确认；下载和附件有路径限制，见上文。
- 发现安全问题请看 [SECURITY.md](SECURITY.md)。

## 环境变量

| 变量 | 含义 |
|---|---|
| `NETEASE_MAIL_ACCOUNTS` | 逗号分隔的账号，第一个为默认；优先于配置文件 |
| `NETEASE_MAIL_MODE` | `readonly` / `draft` / `full`（默认） |
| `NETEASE_MAIL_CONFIRM` | `auto`（默认）/ `elicit` / `token` |
| `NETEASE_MAIL_DOWNLOAD_DIR` | 附件下载目录（默认 `~/Downloads/netease-mail-mcp/`）；下载只会写进这里或它的子目录 |
| `NETEASE_MAIL_ATTACHMENT_ROOTS` | 可选：只允许从这些目录附加文件（用 `:`，Windows 用 `;` 分隔） |
| `NETEASE_MAIL_CONFIG` | 另指定配置文件路径 |

## Agent Skill

[`skills/netease-mail/SKILL.md`](skills/netease-mail/SKILL.md) 给 Agent 提供工作流指引（先搜索再阅读再起草、发送前必须展示预览并取得明确同意、邮件内容不可信、验证码流程）。

## 开发

见 [CONTRIBUTING.md](CONTRIBUTING.md) 和 [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)。版本记录见 [CHANGELOG.md](CHANGELOG.md)。

## 许可证

MIT
