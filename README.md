# zlibrary-tor

[![CI](https://github.com/sl251/zlibrary-tor/actions/workflows/test.yml/badge.svg)](https://github.com/sl251/zlibrary-tor/actions/workflows/test.yml)

An AI agent skill and CLI for accessing Z-Library's onion site through a local Tor proxy, with every command outputting structured JSON for direct parsing instead of web scraping. Downloads are quota-aware: local cache hits return `status: cached` without consuming daily download limits, alongside tiered failure diagnosis. Built in standard SKILL.md format to mount directly into Claude Code, Codex CLI, Hermes Agent, and Gemini CLI.

[English](README.md) | [简体中文](README.zh-CN.md)

## Features

- **Direct onion E-API connection**: Connects directly to Z-Library's onion E-API through Tor SOCKS5 without depending on third-party web mirrors or scraping.
- **Agent-native JSON CLI**: Every command outputs structured JSON to stdout for machine consumption, keeping error codes and failure reasons machine-parseable.
- **Quota-aware downloads**: Verifies local file size and SHA-256 against `.zlib-*.json` sidecars; cache hits return `status: cached` without consuming daily download limits.
- **Headless credential injection**: Authenticates non-interactively via stdin (`--stdin-password`) or environment variables (`ZLIBRARY_PASSWORD`, remix keys), with hidden password prompts and zero disk persistence for plaintext secrets.
- **Paginated search**: Filters books by language and file extension, with explicit page and limit pagination controls.
- **Offline testing & cross-platform CI**: Comprehensive test suite uses HTTPX `MockTransport` requiring no live credentials, Tor connection, or quota, validated on Linux, macOS, and Windows.

## Quick start

1. **Clone the repository**:
```bash
git clone https://github.com/sl251/zlibrary-tor.git ~/.claude/skills/zlibrary-tor
```

2. **Install dependencies**:
```bash
python -m pip install -r ~/.claude/skills/zlibrary-tor/scripts/requirements.txt
```

3. **Verify Tor and API connectivity**:
```bash
python ~/.claude/skills/zlibrary-tor/scripts/zlibrary_tor.py doctor
```

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

CLI commands output operational results and errors as JSON (help remains text). `doctor` and `search` work without login; `profile` and `download` need an account.

### doctor: check that Tor and the API are reachable

```bash
python scripts/zlibrary_tor.py doctor
```

### login: interactive login (password not echoed)

```bash
python scripts/zlibrary_tor.py login --email you@example.com
```

The password prompt hides input, and the password is never written anywhere. Only the session values (`remix_userid` / `remix_userkey`) and origin binding are saved, to `~/.config/zlibrary-tor/config.json` by default; override the location with `ZLIBRARY_TOR_CONFIG`. Use `--account NAME` to save credentials under a specific alias. Without it, login replaces the current default account session; if no default is set, it saves to `default` and selects it.

Already logged in via Tor Browser? Run `import-session` (optionally `--account NAME`) and paste the two remix values from the browser instead of typing your password again. Environment import requires the complete `ZLIBRARY_REMIX_USERID`, `ZLIBRARY_REMIX_USERKEY`, and `ZLIBRARY_SESSION_ORIGIN` group, with an origin matching the current trusted target; partial groups are rejected without prompting.

### profile: account info and daily download quota

```bash
python scripts/zlibrary_tor.py profile
# Or check a specific account
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
    "used": 0,
    "limit": 10,
    "remaining": 10,
    "checked_at": "2026-10-11T00:00:00Z"
  }
}
```

*(Note: Quota values shown are illustrative examples from API responses; actual limits and usage are determined dynamically by upstream responses.)*

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
# Or specify an account
python scripts/zlibrary_tor.py download --id 26610712 --hash def1ec --account personal
# Or use pre-request quota check across configured accounts
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

`--output-dir` sets the save directory (default `downloads`), `--max-mb` caps the size (default 80 MB, max 512). `--account auto` evaluates confirmed remaining quotas before requesting a download link. An exhausted candidate is skipped so the next candidate can be checked; unknown quota or a rate limit stops selection. Once a link request starts, failures stop the download without retrying or switching accounts or origins. `--force` explicitly replaces a cached file but cannot be combined with `--account auto`.

The coordination lock covers downloads using this configuration on this machine. It does not coordinate browser downloads, other configuration files, or other machines, and profile quota checks do not reserve quota. Stop older download processes before upgrading so they cannot bypass the new lock.

The normal flow: you pick a book from the search results, and the agent downloads it using that book's `id` and `hash`.

### register: create a new account

Prefer registering manually in Tor Browser (there are CAPTCHAs and confirmation emails, which are easier to handle in a browser). If you specifically want API registration, see [references/accounts.md](references/accounts.md).

### logout: clear saved session

```bash
python scripts/zlibrary_tor.py logout
# Or log out a specific named account
python scripts/zlibrary_tor.py logout --account personal
# Or clear all account sessions while preserving aliases and network settings
python scripts/zlibrary_tor.py logout --all
```

This removes local credentials, preserves proxy settings, and reports whether environment credentials remain. It does not revoke the server session.

### accounts: manage saved accounts

```bash
# List configured accounts and active selection
python scripts/zlibrary_tor.py accounts list

# Check live quota across accounts (single profile query per account)
python scripts/zlibrary_tor.py accounts list --refresh

# Switch default account
python scripts/zlibrary_tor.py accounts use personal

# Remove a saved account alias (zero network requests)
python scripts/zlibrary_tor.py accounts remove old_account
```

### domains: manage onion origins and mirrors

```bash
# List configured origins, trust levels, and verification status
python scripts/zlibrary_tor.py domains list

# Check an unverified candidate onion via uncredentialed GET /eapi/info
python scripts/zlibrary_tor.py domains check --url "http://<candidate>.onion"

# Add an onion origin after human verification of official source and explicit host confirmation
python scripts/zlibrary_tor.py domains add backup \
  --url "http://<verified>.onion" \
  --verified-from "https://official-announcement-url" \
  --confirm-host "<verified>.onion"

# Switch active base origin without migrating any session tokens
python scripts/zlibrary_tor.py domains use backup

# Remove an unneeded mirror (active origin cannot be removed)
python scripts/zlibrary_tor.py domains remove backup
```

Candidate probing is uncredentialed and does not grant trust or verify ownership. Adding a domain requires explicit human verification and confirmation; candidate source URLs are never fetched automatically. Switching domains never migrates credentials or forwards sessions.

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
| `ZLIBRARY_SESSION_ORIGIN` | Bound origin URL for environment sessions (`ZLIBRARY_REMIX_*`) | none (must match configured base URL) |

## Common pitfalls

- **`.onion` domains require Tor SOCKS5**: `.onion` hostnames can only be resolved and reached through Tor's SOCKS5 proxy (`127.0.0.1:9050` by default). Standard HTTP/SOCKS proxies like mihomo or clash cannot resolve `.onion` addresses. If Tor itself requires an upstream proxy on your server, configure `HTTPSProxy` or `Socks5Proxy` inside Tor's configuration (`torrc`), rather than pointing this tool at mihomo or clash.
- **No automatic retries for quota-sensitive calls**: Requesting a download link can consume your daily quota, and login/registration endpoints create server-side state. Therefore, health checks and searches retry on transient network errors, but login, registration, and download-link requests are never automatically retried.
- **Pre-existing files without sidecars are not trusted**: Download cache checks strictly verify the local file size and SHA-256 against its `.zlib-*.json` sidecar. Pre-existing files in the download directory that lack a valid sidecar are not automatically trusted as cached copies and will not bypass download requests unless verified.

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
