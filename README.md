# zlibrary-tor

Portable Agent Skill + JSON CLI for searching and downloading books from Z-Library's onion E-API through a local Tor SOCKS proxy. Works with Claude Code, Codex CLI, Hermes Agent, and any agent that loads [SKILL.md](SKILL.md)-style skills.

[English](README.md) | [简体中文](README.zh-CN.md)

## Why

- **JSON-first**: every command emits JSON, so an agent can parse results deterministically — no HTML scraping, no fragile selectors.
- **Tor-only by design**: the CLI talks to the v3 onion service through a SOCKS5 Tor listener and refuses clear-web or private-network targets by default.
- **Password hygiene**: passwords are read from hidden prompts via `getpass`, used once, and never written to disk or logs. Only `remix_userid` / `remix_userkey` session values are stored (chmod 600).
- **Bounded by construction**: v3 onion address validation, redirect limit, download size cap, no cookie jar, no fallback proxy.

## Install as an Agent Skill

Clone into your agent's skills directory:

| Agent | Path |
|---|---|
| Claude Code | `~/.claude/skills/zlibrary-tor` |
| Codex CLI | `~/.codex/skills/zlibrary-tor` |
| Hermes Agent | `~/.hermes/skills/zlibrary-tor` |
| Custom | Anywhere — invoke `scripts/zlibrary_tor.py` directly |

```bash
git clone https://github.com/sl251/zlibrary-tor.git ~/.claude/skills/zlibrary-tor
python -m pip install -r ~/.claude/skills/zlibrary-tor/scripts/requirements.txt
```

Start Tor with a SOCKS5 listener on `127.0.0.1:9050` (Tor Browser's bundled Tor works), then verify the pipeline:

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

Read [SKILL.md](SKILL.md) for the agent workflow and [references/accounts.md](references/accounts.md) for account-registration and session-handling rules.

## CLI reference

All commands output JSON. Authentication is optional for `doctor` and `search`, required for `profile` and `download`.

### `doctor` — verify Tor + E-API reachability

```bash
python scripts/zlibrary_tor.py doctor
```

### `login` — interactive login (hidden password prompt)

```bash
python scripts/zlibrary_tor.py login --email you@example.com
```

Stores the session in the config file (default `~/.config/zlibrary-tor/config.json`; override with `ZLIBRARY_TOR_CONFIG`). Already logged in via Tor Browser? `import-session` lets you paste existing `remix_userid` / `remix_userkey` values instead.

### `profile` — account info and download quota

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

### `search` — find books

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

### `download` — fetch a chosen book

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

Options: `--output-dir` (default `downloads`), `--max-mb` (default 80, hard cap 512). The agent should ask you to pick a search result before downloading — see [SKILL.md](SKILL.md).

### `register` — API account registration

Only when you specifically want API registration and have your own valid email. See [references/accounts.md](references/accounts.md) for the recommended Tor Browser path and the rules.

## Configuration

| Env var | Meaning | Default |
|---|---|---|
| `ZLIBRARY_TOR_CONFIG` | Config file path | `~/.config/zlibrary-tor/config.json` |
| `ZLIBRARY_TOR_PROXY` | Tor SOCKS5 proxy | `socks5://127.0.0.1:9050` |
| `ZLIBRARY_BASE_URL` | E-API base URL | built-in v3 onion address |

Onion access requires a SOCKS5 Tor proxy — a conventional HTTP proxy (e.g. mihomo/clash) cannot resolve `.onion` services. If the server's Tor needs an upstream proxy, configure Tor itself with `HTTPSProxy`/`Socks5Proxy` rather than repointing this CLI. Details in [references/accounts.md](references/accounts.md#restricted-server-proxy-chain).

## Security

- This is not a general onion crawler or open proxy; the CLI only talks to the configured E-API host.
- Passwords are accepted only through hidden interactive prompts and are never saved.
- Session keys are stored locally (chmod 600) and must be protected like passwords.
- Downloads require an explicit book `id`/`hash` pair, follow at most 3 redirects, and are size-capped.
- Users are responsible for complying with applicable copyright and access rules.

## License

MIT
