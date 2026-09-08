# zlibrary-tor

An agent skill for Z-Library: your AI agent connects to Z-Library's onion site through a local Tor proxy and searches or downloads books for you. Every command outputs JSON, so the agent can parse results directly instead of scraping web pages.

Works with Claude Code, Codex CLI, Hermes Agent, and any agent that loads SKILL.md-style skills.

[English](README.md) | [简体中文](README.zh-CN.md)

## Setup

You need two things:

1. A local Tor with a SOCKS5 listener on `127.0.0.1:9050`. The easiest way: install Tor Browser and use the Tor it bundles.
2. This repo cloned into your agent's skills directory:

| Agent | Clone to |
|---|---|
| Claude Code | `~/.claude/skills/zlibrary-tor` |
| Codex CLI | `~/.codex/skills/zlibrary-tor` |
| Hermes Agent | `~/.hermes/skills/zlibrary-tor` |
| Other | Anywhere; just invoke `scripts/zlibrary_tor.py` |

```bash
git clone https://github.com/sl251/zlibrary-tor.git ~/.claude/skills/zlibrary-tor
python -m pip install -r ~/.claude/skills/zlibrary-tor/scripts/requirements.txt
```

Then run `doctor` once to confirm Tor and Z-Library are both reachable:

```bash
python ~/.claude/skills/zlibrary-tor/scripts/zlibrary_tor.py doctor
```

If you see this, you're good:

```json
{
  "ok": true,
  "base_url": "http://loginzlib2vrak5zzpcocc3ouizykn6k5qecgj2tzlnab5wcbqhembyd.onion",
  "proxy": "configured",
  "authenticated": false,
  "api_success": 1
}
```

After that, just tell your agent "search Z-Library for The Three-Body Problem" and it will follow the workflow in [SKILL.md](SKILL.md). For account registration and login, see [references/accounts.md](references/accounts.md).

## Commands

Seven commands, all JSON output. `doctor` and `search` work without login; `profile` and `download` need one.

### doctor: check that Tor and the API are reachable

```bash
python scripts/zlibrary_tor.py doctor
```

### login: interactive login (password not echoed)

```bash
python scripts/zlibrary_tor.py login --email you@example.com
```

The password prompt hides input, and the password is never written anywhere. Only the session values (`remix_userid` / `remix_userkey`) are saved, to `~/.config/zlibrary-tor/config.json` by default; override the location with `ZLIBRARY_TOR_CONFIG`.

Already logged in via Tor Browser? Run `import-session` and paste the two remix values from the browser instead of typing your password again.

### profile: account info and daily download quota

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

### search: find books

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

(Only the first result is shown here; you get 5 back.) Options: `--language`, `--extension` (epub, pdf, ...), `--limit` (max 20).

### download: fetch a book

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

`--output-dir` sets the save directory (default `downloads`), `--max-mb` caps the size (default 80 MB, max 512).

The normal flow: you pick a book from the search results, and the agent downloads it using that book's `id` and `hash`.

### register: create a new account

Prefer registering manually in Tor Browser (there are CAPTCHAs and confirmation emails, which are easier to handle in a browser). If you specifically want API registration, see [references/accounts.md](references/accounts.md).

## Environment variables

| Variable | What it does | Default |
|---|---|---|
| `ZLIBRARY_TOR_CONFIG` | Where the config file lives | `~/.config/zlibrary-tor/config.json` |
| `ZLIBRARY_TOR_PROXY` | Tor proxy address | `socks5://127.0.0.1:9050` |
| `ZLIBRARY_BASE_URL` | Z-Library address | built-in onion address |

A common trap: `.onion` addresses only resolve through Tor's SOCKS5. A regular proxy like mihomo/clash cannot reach them. If Tor itself needs an upstream proxy on your server, add `HTTPSProxy`/`Socks5Proxy` to Tor's config instead of pointing this CLI at mihomo.

## Security notes

- Talks only to the configured Z-Library host; it is not a general onion browser
- Passwords exist only inside hidden prompts; never saved, never logged
- Session values are stored with chmod 600; treat them like passwords and keep them out of git
- Downloads require an explicit book id + hash, are size-capped, and follow at most 3 redirects
- You are responsible for complying with copyright and access rules; don't use this to bypass DRM

## License

MIT
