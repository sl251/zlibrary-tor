# zlibrary-tor

[![CI](https://github.com/sl251/zlibrary-tor/actions/workflows/test.yml/badge.svg)](https://github.com/sl251/zlibrary-tor/actions/workflows/test.yml)

专为 AI Agent 设计的 Z-Library 技能与命令行工具：通过本地 Tor 代理访问 Z-Library onion 站，全部命令输出结构化 JSON 供 Agent 直接解析。下载具备配额感知能力，命中本地缓存时返回 `status: cached` 且不消耗每日下载额度，并提供分级失败诊断。采用标准 SKILL.md 格式，可直接挂载到 Claude Code、Codex CLI、Hermes Agent 与 Gemini CLI。

[English](README.md) | [简体中文](README.zh-CN.md)

## 特性

- **onion E-API 直连**：通过本地 Tor SOCKS5 代理直连 Z-Library onion 官方 E-API，不经第三方镜像或网页抓取。
- **Agent 原生 JSON CLI**：所有操作命令与错误响应均输出结构化 JSON，便于机器可靠解析与流程调度。
- **配额感知下载**：结合 `.zlib-*.json` sidecar 文件校验本地大小与 SHA-256；命中缓存返回 `status: cached`，不消耗每日下载配额。
- **Headless 凭据注入**：支持通过 stdin（`--stdin-password`）或环境变量（`ZLIBRARY_PASSWORD`、remix 令牌）进行无交互注入，密码不回显且明文不落盘。
- **分页搜索**：支持按语言、文件格式过滤，支持明确的页码（page）与条数（limit）分页检索。
- **离线测试与跨平台 CI**：测试套件基于 HTTPX `MockTransport` 构建，无需真实凭据、Tor 代理或下载配额，并在 Linux、macOS 与 Windows CI 矩阵中持续验证。

## 快速开始

1. **克隆仓库**：
```bash
git clone https://github.com/sl251/zlibrary-tor.git ~/.claude/skills/zlibrary-tor
```

2. **安装依赖**：
```bash
python -m pip install -r ~/.claude/skills/zlibrary-tor/scripts/requirements.txt
```

3. **运行 doctor 检查**：
```bash
python ~/.claude/skills/zlibrary-tor/scripts/zlibrary_tor.py doctor
```

## 先装好这两样

1. 本机跑一个 Tor，开 SOCKS5 监听 `127.0.0.1:9050`。最简单的办法：装个 Tor Browser，它自带的 Tor 就行。
2. 把仓库克隆到你 agent 的技能目录：

| Agent | 克隆到 |
|---|---|
| Claude Code | `~/.claude/skills/zlibrary-tor` |
| Codex CLI | `~/.codex/skills/zlibrary-tor` |
| Hermes Agent | `~/.hermes/skills/zlibrary-tor` |
| 其他 | 随便放哪，直接调 `scripts/zlibrary_tor.py` |

装完先跑一句 `doctor`，确认 Tor 和 Z-Library 都通：

```bash
python ~/.claude/skills/zlibrary-tor/scripts/zlibrary_tor.py doctor
```

输出这样就是通了：

```json
{
  "ok": true,
  "base_url": "http://loginzlib2vrak5zzpcocc3ouizykn6k5qecgj2tzlnab5wcbqhembyd.onion",
  "proxy": "configured",
  "authenticated": false,
  "api_success": 1
}
```

装好后直接对 agent 说「帮我搜一下三体」就行，它会照着 [SKILL.md](SKILL.md) 的流程走。账号注册、登录的事看 [references/accounts.md](references/accounts.md)。

## 命令

操作结果和错误输出 JSON（帮助信息仍为文本）。`doctor` 和 `search` 不用登录，`profile` 和 `download` 需要账号。

### doctor：检查链路通不通

```bash
python scripts/zlibrary_tor.py doctor
```

### login：登录（交互式，密码不回显）

```bash
python scripts/zlibrary_tor.py login --email you@example.com
```

输密码时屏幕上不显示，密码也不会存到任何文件。存下来的只有会话凭证（`remix_userid` / `remix_userkey`）及原点绑定，默认放在 `~/.config/zlibrary-tor/config.json`，可以用环境变量 `ZLIBRARY_TOR_CONFIG` 换位置。可用 `--account NAME` 指定保存的账号别名（默认 `default`）。

如果你已经在 Tor Browser 里登录过 Z-Library，不想再输一遍密码，用 `import-session`（可选 `--account NAME`）把浏览器里的那两个 remix 值粘进来就行。

### profile：看账号信息和今日下载额度

```bash
python scripts/zlibrary_tor.py profile
# 或查看指定账号
python scripts/zlibrary_tor.py profile --account NAME
```

```json
{
  "success": 1,
  "user": {
    "id": 12345678,
    "email": "you@example.com",
    "downloads_today": 0,
    "downloads_limit": 10
  },
  "account": "default",
  "quota": {
    "status": "available",
    "downloads_today": 0,
    "downloads_limit": 10,
    "remaining": 10
  }
}
```

*(注：配额数值为 API 响应示例，实际额度以服务端返回为准。)*

### search：搜书

```bash
python scripts/zlibrary_tor.py search "三体" --limit 5 --language chinese
```

```json
{
  "ok": true,
  "count": 5,
  "books": [
    {
      "id": 26610712,
      "hash": "def1ec",
      "title": "三体",
      "author": "刘慈欣",
      "year": 2008,
      "language": "Chinese",
      "extension": "epub",
      "filesizeString": "2.48 MB"
    }
  ]
}
```

（示例只贴了第一条，实际会返回 5 条；书名、id 都是真实数据。）支持 `--language`（按语言过滤）、`--extension`（按格式过滤，如 epub、pdf）、`--limit`（1–20）、`--page`（默认 1）。

### download：下书

```bash
python scripts/zlibrary_tor.py download --id 26610712 --hash def1ec
# 或指定账号
python scripts/zlibrary_tor.py download --id 26610712 --hash def1ec --account personal
# 或使用配额预检自动选择可用账号
python scripts/zlibrary_tor.py download --id 26610712 --hash def1ec --account auto
```

```json
{
  "ok": true,
  "status": "downloaded",
  "path": "downloads/三体-<book-key>.epub",
  "bytes": 2598912
}
```

`--output-dir` 指定保存目录（默认 `downloads`），`--max-mb` 限大小（默认 80 MB，最大 512）。`--account auto` 在请求下载链接前评估各账号确认的剩余配额，若配额耗尽或未知则立即停止，传输途中不换账号、不重试。`--force` 明确允许重新获取并替换缓存，但不可与 `--account auto` 混用。

正常用法是你从搜索结果里挑一本，agent 拿它对应的 `id` 和 `hash` 去下载。

### register：注册新账号

一般不建议直接用这个，优先在 Tor Browser 里手动注册（有验证码、确认邮件这些步骤，浏览器里处理更稳妥）。确实想走 API 注册再看 [references/accounts.md](references/accounts.md)。

### logout：清除本地会话

```bash
python scripts/zlibrary_tor.py logout
# 或清除指定别名的会话
python scripts/zlibrary_tor.py logout --account personal
# 或清除全部账号会话（保留别名与网络配置）
python scripts/zlibrary_tor.py logout --all
```

保留代理等配置，只删除已保存的会话。不会撤销服务器令牌，也无法清除父进程的环境变量；结果会提示是否仍有环境会话。

### accounts：管理已保存账号

```bash
# 列出已保存账号及当前选中
python scripts/zlibrary_tor.py accounts list

# 刷新并检查各账号实时配额（每个账号单次 profile 查询）
python scripts/zlibrary_tor.py accounts list --refresh

# 切换默认账号
python scripts/zlibrary_tor.py accounts use personal

# 移除已保存账号别名（纯本地操作，零网络）
python scripts/zlibrary_tor.py accounts remove old_account
```


### 自动化与可靠下载

无 TTY 且未提供凭据时立即返回 JSON 错误。可用 `login --stdin-password` 从可信的密码管理器管道读取一行，或由安全环境注入 `ZLIBRARY_PASSWORD`。不要用带明文密码的 echo 命令。已有的 `ZLIBRARY_REMIX_USERID` / `ZLIBRARY_REMIX_USERKEY` 也可使用，详见[账户说明](references/accounts.md)。

下载前用站点与书籍 id/hash 查找记录，验证本地文件大小和 SHA-256，匹配后返回 `status: cached`，不请求 API。请保留图书旁的 `.zlib-*.json`。文件名加入书籍标识以避免同名冲突；`--force` 明确允许重新获取并替换，可能再次消耗额度。没有记录的旧文件不会直接被视为有效缓存。

先写唯一的 `.part` 临时文件，传输完成且通过可用的长度检查后再替换正式文件。失败保留原文件，不自动断点续传。这不能证明文件来源真实性，也无法在服务端没有长度信息时发现所有截断。同目录的同一本书有并发锁；进程被强制终止后，仅在确认没有下载进程运行时清理遗留 `.lock`。

失败返回非零退出码和 `reason`。`doctor` 区分 `local_proxy`、`onion_request`、`api_response`，不会把一般超时误报成确定的 Tor 电路故障。已知配额提示返回 `daily_quota_exhausted`，其他失败仍保留为一般错误。只有健康检查和搜索会对传输异常及 HTTP 502/503/504 有限重试；登录、注册、申请下载链接均不自动重试。

### 可选 CLI 打包运行

需要 Python 3.11+，原来的克隆安装方式仍可用。只运行 CLI 可使用：

```bash
uvx --from git+https://github.com/sl251/zlibrary-tor.git zlibrary-tor doctor
```

这只安装 CLI，不会把 skill 放入 agent 的发现目录。

## 环境变量

| 变量 | 干嘛的 | 默认值 |
|---|---|---|
| `ZLIBRARY_TOR_CONFIG` | 配置文件放哪 | `~/.config/zlibrary-tor/config.json` |
| `ZLIBRARY_TOR_PROXY` | Tor 代理地址 | `socks5://127.0.0.1:9050` |
| `ZLIBRARY_TOR_TIMEOUT` | 每次请求超时秒数（1–600） | `45` |
| `ZLIBRARY_TOR_RETRIES` | 只读操作额外重试次数（0–2） | `1` |
| `ZLIBRARY_BASE_URL` | Z-Library 地址 | 内置 onion 地址 |
| `ZLIBRARY_SESSION_ORIGIN` | 环境变量会话（`ZLIBRARY_REMIX_*`）绑定的原点 URL | 无（必须与配置基准 URL 严格匹配） |

## 常见坑

- **`.onion` 域名只能通过 Tor SOCKS5 解析**：`.onion` 域名只能由 Tor 本地 SOCKS5 代理（默认 `127.0.0.1:9050`）解析与路由。mihomo/clash 等普通 HTTP/SOCKS 代理无法解析 `.onion`。如果服务器上的 Tor 本身需要上游代理，请在 Tor 配置文件（`torrc`）中配置 `HTTPSProxy`/`Socks5Proxy`，而不是将本 CLI 直接指向普通代理。
- **申请下载链接可能消耗配额，不自动重试**：申请下载链接可能会计入今日下载配额，登录和注册接口也会产生服务端状态。因此，仅健康检查和搜索会对偶发网络抖动进行重试，登录、注册和申请下载链接绝不自动重试。
- **不带 `.zlib-*.json` sidecar 的旧文件不自动信任**：下载缓存依赖对应 sidecar 记录的文件大小与 SHA-256 进行严格比对。下载目录下缺少有效 sidecar 的旧文件不会被自动信任为有效缓存，无法跳过下载请求。

## 安全设计

- API 只请求配置的站点；下载可跳转到 HTTPS 主机，但会话凭据只发送到配置的精确源
- 密码来自隐藏输入或明确配置的安全输入源，不落盘、不进日志
- POSIX 下会话文件权限为 600；Windows 下需使用账户 ACL 保护配置目录。别提交凭据到 git
- 下载必须是明确的某本书（id + hash），大小有上限，重定向最多跟 3 次
- 版权合规自己负责，别拿这个绕 DRM

## 开发验证

```bash
python -m pip install -e ".[dev]" build
pytest -q
ruff check scripts tests
ruff format --check scripts tests
python -m build
```

测试通过 HTTPX MockTransport 模拟请求，不需要 Tor、真实凭据或下载额度。CI 覆盖 Linux、macOS、Windows。分页测试只验证请求参数，并不代替当前上游接口实测。

MCP、自动镜像切换、断点续传、独立图书详情命令暂缓，先验证协议及信任边界，具体原因见[演进取舍](references/evolution.md)。

## 许可证

MIT
