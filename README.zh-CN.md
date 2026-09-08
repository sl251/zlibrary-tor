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

一共 7 条，每条都输出 JSON。`doctor` 和 `search` 不用登录，`profile` 和 `download` 要先登录。

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

（示例只贴了第一条，实际会返回 5 条；书名、id 都是真实数据。）支持 `--language`（按语言过滤）、`--extension`（按格式过滤，如 epub、pdf）、`--limit`（最多 20）。

### download：下书

```bash
python scripts/zlibrary_tor.py download --id 26610712 --hash def1ec
```

```json
{
  "ok": true,
  "path": "downloads/三体.epub",
  "bytes": 2598912
}
```

`--output-dir` 指定保存目录（默认 `downloads`），`--max-mb` 限大小（默认 80 MB，最大 512）。

正常用法是你从搜索结果里挑一本，agent 拿它对应的 `id` 和 `hash` 去下载。

### register：注册新账号

一般不建议直接用这个，优先在 Tor Browser 里手动注册（有验证码、确认邮件这些步骤，浏览器里处理更稳妥）。确实想走 API 注册再看 [references/accounts.md](references/accounts.md)。

## 环境变量

| 变量 | 干嘛的 | 默认值 |
|---|---|---|
| `ZLIBRARY_TOR_CONFIG` | 配置文件放哪 | `~/.config/zlibrary-tor/config.json` |
| `ZLIBRARY_TOR_PROXY` | Tor 代理地址 | `socks5://127.0.0.1:9050` |
| `ZLIBRARY_BASE_URL` | Z-Library 地址 | 内置 onion 地址 |

一个常见的坑：onion 网址必须走 Tor 的 SOCKS5，mihomo/clash 这类普通代理是解析不了 `.onion` 的。如果你的服务器上 Tor 本身需要上游代理，去 Tor 的配置里加 `HTTPSProxy`/`Socks5Proxy`，别把这个 CLI 指到 mihomo 上。

## 安全设计

- 只访问 Z-Library 这一个站，不是什么 onion 网站都能用它访问
- 密码只出现在隐藏输入里，用完就没了，不落盘不进日志
- 会话凭证的存储文件权限是 600，等同密码，别提交到 git
- 下载必须是明确的某本书（id + hash），大小有上限，重定向最多跟 3 次
- 版权合规自己负责，别拿这个绕 DRM

## 许可证

MIT
