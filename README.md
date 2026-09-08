# zlibrary-tor

Portable Agent Skill and JSON CLI for using the Z-Library onion E-API through a local Tor SOCKS proxy.

## Install as an Agent Skill

Clone this repository into the skills directory used by your agent. For Codex:

```bash
git clone https://github.com/sl251/zlibrary-tor.git ~/.codex/skills/zlibrary-tor
python -m pip install -r ~/.codex/skills/zlibrary-tor/scripts/requirements.txt
```

Start Tor with a SOCKS5 listener on `127.0.0.1:9050`, then verify connectivity:

```bash
python ~/.codex/skills/zlibrary-tor/scripts/zlibrary_tor.py doctor
```

Read [SKILL.md](SKILL.md) for the agent workflow and [references/accounts.md](references/accounts.md) for the account-registration and session-handling rules.

## Security

- This is not a general onion crawler or open proxy.
- Passwords are accepted only through hidden interactive prompts and are never saved.
- Session keys are stored locally and must be protected like passwords.
- Downloads require an explicit book id/hash and are size-limited.
- Users are responsible for complying with applicable copyright and access rules.

## License

MIT
