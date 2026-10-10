# zlibrary-tor

一个给 AI agent 用的 Z-Library 技能：让 agent 通过本机 Tor 代理连 Z-Library 的 onion 站，帮你搜书、下书。命令行输出全是 JSON，agent 拿到就能直接解析，不用抓网页。

适用于 Claude Code、Codex CLI、Hermes Agent，以及其他支持 SKILL.md 格式技能的 agent。

[English](README.md) | [简体中文](README.zh-CN.md)

## 先装好这两样

1. 本机跑一个 Tor，开 SOCKS5 监听 `127.0.0.1:9050`。最简单的办法：装个 Tor Browser，它自带的 Tor 就行。
2. 把仓库克隆到你 agent 的技能目录：

| Agent | 克隆到 |
|---|---|
| Claude Code | `~/.claude/skills/zlibrary-tor` |
| Codex CLI | `~/.codex/skills/zlibrary-tor` |
| Hermes Agent | `~/.hermes/skills/zlibrary-tor` |
| 其他 | 随便放哪，直接调 `scripts/zlibrary_tor.py` |

```bash
git clone https://github.com/sl251/zlibrary-tor.git ~/.claude/skills/zlibrary-tor
python -m pip install -r ~/.claude/skills/zlibrary-tor/scripts/requirements.txt
```

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

一共 8 条，操作结果和错误输出 JSON（帮助信息仍为文本）。`doctor` 和 `search` 不用登录，`profile` 和 `download` 要先登录。

### doctor：检查链路通不通

```bash
python scripts/zlibrary_tor.py doctor
```

### login：登录（交互式，密码不回显）

```bash
python scripts/zlibrary_tor.py login --email you@example.com
```

输密码时屏幕上不显示，密码也不会存到任何文件。存下来的只有会话凭证（`remix_userid` / `remix_userkey`），默认放在 `~/.config/zlibrary-tor/config.json`，可以用环境变量 `ZLIBRARY_TOR_CONFIG` 换位置。

如果你已经在 Tor Browser 里登录过 Z-Library，不想再输一遍密码，用 `import-session` 把浏览器里的那两个 remix 值粘进来就行。

### profile：看账号信息和今日下载额度

```bash
python scripts/zlibrary_tor.py profile
```

```json
{
  "success": 1,
  "user": {
    "id": 12345678,
    "email": "you@example.com",
    "downloads_today": 0,
    "downloads_limit": 10
  }
}
```

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
```

```json
{
  "ok": true,
  "path": "downloads/三体-<book-key>.epub",
  "bytes": 2598912
}
```

`--output-dir` 指定保存目录（默认 `downloads`），`--max-mb` 限大小（默认 80 MB，最大 512）。

正常用法是你从搜索结果里挑一本，agent 拿它对应的 `id` 和 `hash` 去下载。

### register：注册新账号

一般不建议直接用这个，优先在 Tor Browser 里手动注册（有验证码、确认邮件这些步骤，浏览器里处理更稳妥）。确实想走 API 注册再看 [references/accounts.md](references/accounts.md)。

### logout：清除本地会话

```bash
python scripts/zlibrary_tor.py logout
```

保留代理等配置，只删除已保存的会话。不会撤销服务器令牌，也无法清除父进程的环境变量；结果会提示是否仍有环境会话。

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

一个常见的坑：onion 网址必须走 Tor 的 SOCKS5，mihomo/clash 这类普通代理是解析不了 `.onion` 的。如果你的服务器上 Tor 本身需要上游代理，去 Tor 的配置里加 `HTTPSProxy`/`Socks5Proxy`，别把这个 CLI 指到 mihomo 上。

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
