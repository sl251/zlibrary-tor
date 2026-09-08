# zlibrary-tor

可移植的 Agent 技能 + JSON CLI：通过本地 Tor SOCKS 代理访问 Z-Library 的 onion E-API，完成搜索与下载。兼容 Claude Code、Codex CLI、Hermes Agent 以及任何加载 [SKILL.md](SKILL.md) 格式技能的 agent。

[English](README.md) | [简体中文](README.zh-CN.md)

## 为什么做这个

- **JSON 优先**：所有命令输出 JSON，agent 可以确定性解析结果——不抓 HTML、不依赖脆弱的选择器。
- **只走 Tor**：CLI 通过 SOCKS5 Tor 监听访问 v3 onion 服务，默认拒绝明网和内网目标。
- **密码卫生**：密码只经 `getpass` 隐藏提示输入，用完即弃，绝不落盘、不进日志。磁盘上只存 `remix_userid` / `remix_userkey` 会话值（chmod 600）。
- **边界内建**：v3 onion 地址校验、重定向上限、下载大小封顶、无 cookie jar、无代理回退。

## 作为 Agent 技能安装

克隆到你所用 agent 的技能目录：

| Agent | 路径 |
|---|---|
| Claude Code | `~/.claude/skills/zlibrary-tor` |
| Codex CLI | `~/.codex/skills/zlibrary-tor` |
| Hermes Agent | `~/.hermes/skills/zlibrary-tor` |
| 自定义 | 任意位置——直接调用 `scripts/zlibrary_tor.py` |

```bash
git clone https://github.com/sl251/zlibrary-tor.git ~/.claude/skills/zlibrary-tor
python -m pip install -r ~/.claude/skills/zlibrary-tor/scripts/requirements.txt
```

先让 Tor 在 `127.0.0.1:9050` 起一个 SOCKS5 监听（Tor Browser 自带的 Tor 就行），然后验证链路：

```bash
python ~/.claude/skills/zlibrary-tor/scripts/zlibrary_tor.py doctor
```

```json
{
  "ok": true,
  "base_url": "http://loginzlib2vrak5zzpcocc3ouizykn6k5qecgj2tzlnab5wcbqhembyd.onion",
  "proxy": "configured",
  "authenticated": false,
  "api_success": true
}
```

Agent 工作流见 [SKILL.md](SKILL.md)，账号注册与会话处理规则见 [references/accounts.md](references/accounts.md)。

## CLI 命令

所有命令输出 JSON。`doctor` 和 `search` 无需登录，`profile` 和 `download` 需要会话。

### `doctor` — 验证 Tor + E-API 连通性

```bash
python scripts/zlibrary_tor.py doctor
```

### `login` — 交互式登录（隐藏密码输入）

```bash
python scripts/zlibrary_tor.py login --email you@example.com
```

会话保存在配置文件里（默认 `~/.config/zlibrary-tor/config.json`，可用 `ZLIBRARY_TOR_CONFIG` 覆盖）。已经在 Tor Browser 里登录过？用 `import-session` 粘贴已有的 `remix_userid` / `remix_userkey` 即可。

### `profile` — 账号信息与下载配额

```bash
python scripts/zlibrary_tor.py profile
```

```json
{
  "success": true,
  "user": {
    "id": "41934710",
    "email": "you@example.com",
    "downloads_today": 0,
    "downloads_limit": 10
  }
}
```

### `search` — 搜索书籍

```bash
python scripts/zlibrary_tor.py search "三体" --limit 5 --language chinese
```

```json
{
  "ok": true,
  "count": 5,
  "books": [
    {
      "id": "123456789",
      "hash": "abcdef0123456789abcdef",
      "title": "三体",
      "author": "刘慈欣",
      "year": "2008",
      "language": "chinese",
      "extension": "epub",
      "filesizeString": "2.1 MB"
    }
  ]
}
```

### `download` — 下载选中的书

```bash
python scripts/zlibrary_tor.py download --id 123456789 --hash abcdef0123456789abcdef
```

```json
{
  "ok": true,
  "path": "/abs/path/downloads/三体.epub",
  "bytes": 2202011
}
```

参数：`--output-dir`（默认 `downloads`）、`--max-mb`（默认 80，硬上限 512）。agent 应该先让你从搜索结果里挑一本再下载——见 [SKILL.md](SKILL.md)。

### `register` — API 注册账号

只在你明确想走 API 注册、且有自己的有效邮箱时使用。推荐的 Tor Browser 注册路径和规则见 [references/accounts.md](references/accounts.md)。

## 配置

| 环境变量 | 含义 | 默认值 |
|---|---|---|
| `ZLIBRARY_TOR_CONFIG` | 配置文件路径 | `~/.config/zlibrary-tor/config.json` |
| `ZLIBRARY_TOR_PROXY` | Tor SOCKS5 代理 | `socks5://127.0.0.1:9050` |
| `ZLIBRARY_BASE_URL` | E-API 基地址 | 内置 v3 onion 地址 |

onion 访问必须走 SOCKS5 Tor 代理——常规 HTTP 代理（如 mihomo/clash）解析不了 `.onion`。如果服务器上的 Tor 需要上游代理，应给 Tor 本身配置 `HTTPSProxy`/`Socks5Proxy`，而不是改这个 CLI 的指向。详见 [references/accounts.md](references/accounts.md#restricted-server-proxy-chain)。

## 安全性

- 这不是通用 onion 爬虫或开放代理；CLI 只访问配置的 E-API 主机。
- 密码只经隐藏交互提示输入，绝不保存。
- 会话密钥本地存储（chmod 600），需像密码一样保护。
- 下载必须提供明确的书籍 `id`/`hash`，最多跟随 3 次重定向，且有大小封顶。
- 用户需自行遵守适用的版权与访问规则。

## 许可证

MIT
