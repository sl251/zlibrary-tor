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
```

The command prompts privately for both values. Session keys grant account access and must be protected like passwords.

## Headless execution

Without a TTY, login, import-session, and registration never fall back to visible prompts or implicit stdin reads. Missing credentials return JSON with `reason: credentials_required`.

- `login --email you@example.com --stdin-password` reads one password line from an explicitly supplied pipe. Use a trusted secret manager as the producer; never use a literal password in an `echo` command or shell history. The pipe producer must close or supply a line; explicit stdin mode can wait for that producer.
- `ZLIBRARY_PASSWORD` supplies a password for login or explicitly authorized registration. It is never saved to config. Avoid environment dumps and persistent shell profiles; clear it after use.
- `ZLIBRARY_REMIX_USERID` and `ZLIBRARY_REMIX_USERKEY` supply an ephemeral session. `import-session` explicitly persists these environment values to config; normal search/profile/download do not.
- Secret input precedence is explicit stdin, then the relevant environment value, then a private terminal prompt. No password or session-key command-line flags are provided.

Run `python scripts/zlibrary_tor.py logout` to remove both saved session values while preserving other configuration. The result flags any session environment values still present. Clear those in the calling shell separately. This is local logout, not server-side token revocation.

Config writes use a unique temporary file with POSIX 0600 permissions before replacement. On Windows, protect the config directory using account ACLs; chmod does not provide equivalent isolation.

## Configuration precedence

1. Environment variables `ZLIBRARY_BASE_URL`, `ZLIBRARY_TOR_PROXY`, and `ZLIBRARY_TOR_CONFIG`.
2. Saved config values.
3. Built-in onion URL and local Tor proxy defaults.

Environment variables `ZLIBRARY_REMIX_USERID` and `ZLIBRARY_REMIX_USERKEY` may supply a session for ephemeral containers; avoid putting them in checked-in Compose files.

## Restricted-server proxy chain

When an Agent runs on a server where Tor cannot connect directly, keep the CLI pointed at the Tor listener (for example `socks5://tor:9050`). Configure Tor itself with `HTTPSProxy proxy-host:port` or `Socks5Proxy proxy-host:port`, according to the user's actual upstream protocol. The conventional proxy is Tor's upstream transport; it is not a replacement for Tor onion routing.
