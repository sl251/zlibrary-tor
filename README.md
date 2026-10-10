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

Eight commands; operational results and errors are JSON (help remains text). `doctor` and `search` work without login; `profile` and `download` need one.

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

(Only the first result is shown here; you get 5 back.) Options: `--language`, `--extension` (epub, pdf, ...), `--limit` (1–20), `--page` (default 1).

### download: fetch a book

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

`--output-dir` sets the save directory (default `downloads`), `--max-mb` caps the size (default 80 MB, max 512).

The normal flow: you pick a book from the search results, and the agent downloads it using that book's `id` and `hash`.

### register: create a new account

Prefer registering manually in Tor Browser (there are CAPTCHAs and confirmation emails, which are easier to handle in a browser). If you specifically want API registration, see [references/accounts.md](references/accounts.md).

### logout: clear saved session

```bash
python scripts/zlibrary_tor.py logout
```

This removes local credentials, preserves proxy settings, and reports whether environment credentials remain. It does not revoke the server session.

### Automation and reliable downloads

Headless login fails promptly unless credentials are supplied explicitly. Use `login --stdin-password` with a trusted secret-manager pipe, or inject `ZLIBRARY_PASSWORD`; never put a literal secret in a command. Existing `ZLIBRARY_REMIX_USERID` and `ZLIBRARY_REMIX_USERKEY` also work. See [account guidance](references/accounts.md).

Downloads first check a sidecar keyed by service and book ID/hash, then verify file size and SHA-256 before requesting a link. A match returns `status: cached` without using the API. Keep `.zlib-*.json` alongside your books. New filenames include a book key to avoid title collisions. `--force` explicitly replaces a file and may consume quota again. Older files without a sidecar are not automatically trusted.

Files stream into unique `.part` files and replace the destination only after successful transfer and available length checks. Failures preserve existing files. This does not prove publisher authenticity or detect all truncation when the server supplies no length. No automatic resume is performed. Same-book downloads in one directory use a lock; remove a stale `.lock` only after confirming no download process is active.

Errors include `reason` and a nonzero exit status. `doctor` reports `local_proxy`, `onion_request`, or `api_response`; it does not infer a precise Tor circuit failure from a generic timeout. Recognized quota messages yield `daily_quota_exhausted`; other errors remain generic. Only health checks and searches retry transport failures and HTTP 502/503/504. Login, registration and download-link acquisition are never automatically retried.

### Optional CLI packaging

Requires Python 3.11+. The cloned skill remains usable as before. To run just the CLI without manually cloning:

```bash
uvx --from git+https://github.com/sl251/zlibrary-tor.git zlibrary-tor doctor
```

This installs the CLI, not the agent's skill discovery files.

## Environment variables

| Variable | What it does | Default |
|---|---|---|
| `ZLIBRARY_TOR_CONFIG` | Where the config file lives | `~/.config/zlibrary-tor/config.json` |
| `ZLIBRARY_TOR_PROXY` | Tor proxy address | `socks5://127.0.0.1:9050` |
| `ZLIBRARY_TOR_TIMEOUT` | Per-request timeout in seconds (1–600) | `45` |
| `ZLIBRARY_TOR_RETRIES` | Extra read-only attempts (0–2) | `1` |
| `ZLIBRARY_BASE_URL` | Z-Library address | built-in onion address |

A common trap: `.onion` addresses only resolve through Tor's SOCKS5. A regular proxy like mihomo/clash cannot reach them. If Tor itself needs an upstream proxy on your server, add `HTTPSProxy`/`Socks5Proxy` to Tor's config instead of pointing this CLI at mihomo.

## Security notes

- API requests use the configured host. Download links may redirect to HTTPS hosts; session credentials stay on the exact configured origin
- Passwords come from hidden prompts or explicit secret input; never saved or logged
- Session files use POSIX mode 600; on Windows protect the config directory with account ACLs. Keep credentials out of git
- Downloads require an explicit book id + hash, are size-capped, and follow at most 3 redirects
- You are responsible for complying with copyright and access rules; don't use this to bypass DRM

## Development

```bash
python -m pip install -e ".[dev]" build
pytest -q
ruff check scripts tests
ruff format --check scripts tests
python -m build
```

Tests use HTTPX MockTransport and temporary directories, with no Tor, live credentials or quota required. CI covers Linux, macOS and Windows. Pagination is sent to the E-API; mocks verify the request contract, not current upstream behavior.

MCP, automatic mirror failover, resumable downloads and a standalone book-detail command remain deferred until their protocol and trust requirements are verified. See [the implementation decisions](references/evolution.md).

## License

MIT
