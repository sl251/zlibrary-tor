# Account registration and authentication

## Recommended path

1. Start Tor Browser and open the configured Z-Library onion address from a trusted bookmark.
2. Register one account with a valid email address and a unique password.
3. Complete any email-confirmation or interactive anti-bot step in the browser.
4. Run `python scripts/zlibrary_tor.py login --email you@example.com` in a private terminal. Enter the password only at the hidden prompt.
5. The CLI stores only `remix_userid` and `remix_userkey`; it does not store the password.

This is preferred because registration and confirmation behavior can change, and a human can inspect the domain and complete challenges safely.

## API registration

The documented E-API exposes `POST /eapi/user/registration` with `email`, `password`, and `name`. The CLI wraps it as:

```bash
python scripts/zlibrary_tor.py register --email you@example.com --name YourName --yes
```

The command prompts twice for the password and performs only one registration attempt. `--yes` acknowledges that an external account will be created. If the response requires email confirmation, finish that step manually, then run `login`.

Do not retry registration automatically, create account pools, use temporary-mail services, or attempt to defeat CAPTCHA/rate limits.

## Existing remix credentials

Existing credentials can be imported without exposing them on the command line:

```bash
python scripts/zlibrary_tor.py import-session
# Or import into a specific named account
python scripts/zlibrary_tor.py import-session --account personal
```

The command prompts privately for both values. Session keys grant account access and must be protected like passwords.

## Multi-account management and session binding

Configuration schema v2 stores multiple accounts under alias names (`[A-Za-z0-9_-]{1,32}`) with a `default_account` pointer. Legacy v1 configurations are migrated automatically to `accounts.default` with atomic locking.

Each stored account records:
- `remix_userid` and `remix_userkey` session credentials.
- `session_origin`: the origin URL where the session was established. Credentials are bound strictly to this origin; attempting to send credentials to a mismatched origin fails immediately with `session_origin_mismatch` before making any network requests.

Account management commands:
- `python scripts/zlibrary_tor.py accounts list`: lists configured aliases and indicates the default account (zero network).
- `python scripts/zlibrary_tor.py accounts list --refresh`: queries each account's profile once to report confirmed quota without modifying state.
- `python scripts/zlibrary_tor.py accounts use NAME`: sets the default account alias (zero network).
- `python scripts/zlibrary_tor.py accounts remove NAME`: deletes an account alias from configuration (zero network).

Account selection precedence:
1. Explicit `--account NAME` CLI argument.
2. Explicit `--account env` CLI argument.
3. Ambient environment session (`ZLIBRARY_REMIX_USERID` + `ZLIBRARY_REMIX_USERKEY` + `ZLIBRARY_SESSION_ORIGIN`).
4. Configured `default_account`.
5. If none of the above are valid or present, fails with `account_required`.

Automatic pre-request selection (`--account auto`):
- Available only on `download`.
- Acquires download coordination lock, then checks candidate accounts (default account first, remaining aliases sorted alphabetically, deduplicated by `remix_userid + session_origin`).
- Checks each candidate with a single `fetch_profile` call to find the first account with confirmed `remaining > 0`.
- Stops immediately on 429 (`rate_limited`) or unparseable/unknown quota (`account_selection_incomplete`).
- If all valid candidates are confirmed exhausted, returns `no_available_account` without requesting a download link.
- Never rotates or retries after a download link request has begun; at most one download link request is issued.
- `--account auto --force` is rejected with `invalid_arguments`.

## Headless execution

Without a TTY, login, import-session, and registration never fall back to visible prompts or implicit stdin reads. Missing credentials return JSON with `reason: credentials_required`.

- `login --email you@example.com --stdin-password` reads one password line from an explicitly supplied pipe. Use a trusted secret manager as the producer; never use a literal password in an `echo` command or shell history. The pipe producer must close or supply a line; explicit stdin mode can wait for that producer.
- `ZLIBRARY_PASSWORD` supplies a password for login or explicitly authorized registration. It is never saved to config. Avoid environment dumps and persistent shell profiles; clear it after use.
- `ZLIBRARY_REMIX_USERID` and `ZLIBRARY_REMIX_USERKEY` supply an ephemeral session. In addition, `ZLIBRARY_SESSION_ORIGIN` must be set to match the configured base URL; missing or mismatched origins fail before network requests. `import-session` explicitly persists environment values to config; normal search/profile/download do not.
- Secret input precedence is explicit stdin, then the relevant environment value, then a private terminal prompt. No password or session-key command-line flags are provided.

Run `python scripts/zlibrary_tor.py logout` to remove saved session values while preserving other configuration. Use `--account NAME` to log out a specific account, or `--all` to clear all account sessions while preserving aliases and network settings. The result flags any session environment values still present. Clear those in the calling shell separately. This is local logout, not server-side token revocation.

Config writes use a unique temporary file with POSIX 0600 permissions before atomic replacement under a configuration lock. On Windows, protect the config directory using account ACLs; chmod does not provide equivalent isolation.

## Configuration precedence

1. Environment variables `ZLIBRARY_BASE_URL`, `ZLIBRARY_TOR_PROXY`, and `ZLIBRARY_TOR_CONFIG`.
2. Explicit CLI `--account` selection.
3. Ambient environment session (`ZLIBRARY_REMIX_USERID`, `ZLIBRARY_REMIX_USERKEY`, `ZLIBRARY_SESSION_ORIGIN`).
4. Saved config values under `default_account`.
5. Built-in onion URL and local Tor proxy defaults.

## Restricted-server proxy chain

When an Agent runs on a server where Tor cannot connect directly, keep the CLI pointed at the Tor listener (for example `socks5://tor:9050`). Configure Tor itself with `HTTPSProxy proxy-host:port` or `Socks5Proxy proxy-host:port`, according to the user's actual upstream protocol. The conventional proxy is Tor's upstream transport; it is not a replacement for Tor onion routing.
