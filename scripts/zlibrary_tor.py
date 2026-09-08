#!/usr/bin/env python3
"""Portable Z-Library E-API CLI over Tor. All command output is JSON."""

from __future__ import annotations

import argparse
import getpass
import ipaddress
import json
import os
import re
import stat
import sys
from pathlib import Path
from urllib.parse import urljoin, urlparse

try:
    import httpx
except ImportError as exc:  # pragma: no cover
    raise SystemExit(
        "Missing dependency: install scripts/requirements.txt in your Python environment"
    ) from exc

DEFAULT_BASE_URL = (
    "http://loginzlib2vrak5zzpcocc3ouizykn6k5qecgj2tzlnab5wcbqhembyd.onion"
)
DEFAULT_PROXY = "socks5://127.0.0.1:9050"
HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "User-Agent": "zlibrary-tor-skill/0.1",
}
REDIRECTS = {301, 302, 303, 307, 308}
MAX_REDIRECTS = 3


class CliError(RuntimeError):
    pass


def config_path() -> Path:
    override = os.environ.get("ZLIBRARY_TOR_CONFIG")
    if override:
        return Path(override).expanduser()
    if os.name == "nt" and os.environ.get("APPDATA"):
        return Path(os.environ["APPDATA"]) / "zlibrary-tor" / "config.json"
    return Path.home() / ".config" / "zlibrary-tor" / "config.json"


def load_config() -> dict:
    path = config_path()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CliError(f"cannot read config: {exc}") from exc
    return data if isinstance(data, dict) else {}


def save_config(data: dict) -> None:
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        temp.chmod(stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass
    temp.replace(path)


def settings() -> dict:
    cfg = load_config()
    cfg["base_url"] = os.environ.get(
        "ZLIBRARY_BASE_URL", cfg.get("base_url", DEFAULT_BASE_URL)
    ).rstrip("/")
    cfg["proxy"] = os.environ.get("ZLIBRARY_TOR_PROXY", cfg.get("proxy", DEFAULT_PROXY))
    cfg["remix_userid"] = os.environ.get(
        "ZLIBRARY_REMIX_USERID", cfg.get("remix_userid", "")
    )
    cfg["remix_userkey"] = os.environ.get(
        "ZLIBRARY_REMIX_USERKEY", cfg.get("remix_userkey", "")
    )
    validate_base_url(cfg["base_url"])
    if cfg["base_url"].lower().endswith(".onion") and not str(
        cfg["proxy"]
    ).lower().startswith(("socks5://", "socks5h://")):
        raise CliError("onion access requires a SOCKS5 Tor proxy")
    return cfg


def validate_base_url(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise CliError("base URL must use http:// or https://")
    if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
        raise CliError("base URL must not contain a path, query, or fragment")
    validate_target(url, allow_http_onion=True)


def validate_target(url: str, *, allow_http_onion: bool = False) -> None:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if parsed.scheme not in {"http", "https"} or not host:
        raise CliError("refusing a non-HTTP(S) target")
    if host.endswith(".onion"):
        label = host[:-6]
        valid = len(label) == 56 and all(
            c in "abcdefghijklmnopqrstuvwxyz234567" for c in label
        )
        if not valid:
            raise CliError("refusing an invalid/non-v3 onion address")
        if parsed.scheme != "https" and not allow_http_onion:
            raise CliError("HTTP is allowed only for the configured onion service")
        return
    if parsed.scheme != "https":
        raise CliError("refusing a clear-web download that is not HTTPS")
    if host in {"localhost", "localhost.localdomain"} or host.endswith(
        (".local", ".localhost", ".internal", ".home.arpa")
    ):
        raise CliError("refusing a local hostname")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return
    if not address.is_global:
        raise CliError("refusing a private/reserved IP target")


def auth(cfg: dict) -> tuple[dict, dict]:
    uid = str(cfg.get("remix_userid", ""))
    key = str(cfg.get("remix_userkey", ""))
    if not uid or not key:
        return {}, {}
    headers = {"remix-userid": uid, "remix-userkey": key}
    cookies = {
        "remix_userid": uid,
        "remix_userkey": key,
        "siteLanguageV2": "en",
    }
    return headers, cookies


def client(cfg: dict) -> httpx.Client:
    proxy = str(cfg.get("proxy", "")).replace("socks5h://", "socks5://", 1)
    return httpx.Client(
        proxy=proxy or None,
        timeout=httpx.Timeout(45.0, connect=30.0),
        headers=HEADERS,
        follow_redirects=False,
    )


def request_json(
    cfg: dict,
    method: str,
    path: str,
    *,
    data: dict | None = None,
    authenticated: bool = False,
) -> dict:
    headers, cookies = auth(cfg) if authenticated else ({}, {})
    try:
        with client(cfg) as session:
            response = session.request(
                method,
                cfg["base_url"] + path,
                data=data,
                headers=headers,
                cookies=cookies,
            )
    except httpx.HTTPError as exc:
        raise CliError(f"network error: {type(exc).__name__}: {exc}") from exc
    if response.status_code in REDIRECTS:
        raise CliError(f"unexpected API redirect (HTTP {response.status_code})")
    try:
        payload = response.json()
    except ValueError as exc:
        sample = response.text[:160].replace("\n", " ")
        raise CliError(
            f"API returned non-JSON HTTP {response.status_code}: {sample}"
        ) from exc
    if response.status_code >= 400:
        raise CliError(f"API HTTP {response.status_code}: {payload}")
    return payload


def extract_session(payload: dict) -> tuple[str, str]:
    user = payload.get("user") or payload.get("response") or {}
    uid = str(user.get("id") or user.get("user_id") or "")
    key = str(user.get("remix_userkey") or user.get("user_key") or "")
    if not payload.get("success") or not uid or not key:
        message = payload.get("error") or payload.get("message") or "login failed"
        raise CliError(str(message))
    return uid, key


def emit(value: object) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2))


def cmd_doctor(cfg: dict, _args) -> None:
    payload = request_json(cfg, "GET", "/eapi/info")
    emit(
        {
            "ok": True,
            "base_url": cfg["base_url"],
            "proxy": "configured" if cfg.get("proxy") else "none",
            "authenticated": bool(auth(cfg)[0]),
            "api_success": payload.get("success"),
        }
    )


def cmd_login(cfg: dict, args) -> None:
    password = getpass.getpass("Z-Library password: ")
    payload = request_json(
        cfg,
        "POST",
        "/eapi/user/login",
        data={"email": args.email, "password": password},
    )
    uid, key = extract_session(payload)
    stored = load_config()
    stored.update({"base_url": cfg["base_url"], "proxy": cfg["proxy"]})
    stored.update({"remix_userid": uid, "remix_userkey": key})
    save_config(stored)
    emit({"ok": True, "config": str(config_path()), "remix_userid": uid})


def cmd_import_session(cfg: dict, _args) -> None:
    uid = getpass.getpass("remix_userid: ").strip()
    key = getpass.getpass("remix_userkey: ").strip()
    if not uid or not key:
        raise CliError("both session values are required")
    stored = load_config()
    stored.update(
        {
            "base_url": cfg["base_url"],
            "proxy": cfg["proxy"],
            "remix_userid": uid,
            "remix_userkey": key,
        }
    )
    save_config(stored)
    emit({"ok": True, "config": str(config_path())})


def cmd_register(cfg: dict, args) -> None:
    if not args.yes:
        raise CliError("registration creates an external account; rerun with --yes")
    password = getpass.getpass("New password: ")
    repeated = getpass.getpass("Repeat password: ")
    if password != repeated or not password:
        raise CliError("passwords do not match or are empty")
    payload = request_json(
        cfg,
        "POST",
        "/eapi/user/registration",
        data={"email": args.email, "password": password, "name": args.name},
    )
    emit({"ok": bool(payload.get("success")), "response": payload})


def cmd_profile(cfg: dict, _args) -> None:
    if not auth(cfg)[0]:
        raise CliError("no session; run login or import-session")
    emit(request_json(cfg, "GET", "/eapi/user/profile", authenticated=True))


def cmd_search(cfg: dict, args) -> None:
    data: dict[str, object] = {"message": args.query, "limit": min(args.limit, 20)}
    if args.language:
        data["languages[]"] = args.language
    if args.extension:
        data["extensions[]"] = args.extension
    payload = request_json(
        cfg,
        "POST",
        "/eapi/book/search",
        data=data,
        authenticated=bool(auth(cfg)[0]),
    )
    books = [
        {
            key: book.get(key)
            for key in (
                "id",
                "hash",
                "title",
                "author",
                "year",
                "language",
                "extension",
                "filesizeString",
            )
        }
        for book in payload.get("books") or []
    ]
    emit({"ok": bool(payload.get("success")), "count": len(books), "books": books})


def safe_filename(value: str) -> str:
    cleaned = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", value).strip(" .")
    return cleaned[:180] or "book"


def download_stream(cfg: dict, url: str, output: Path, max_bytes: int) -> int:
    current = url
    headers, cookies = auth(cfg)
    for hop in range(MAX_REDIRECTS + 1):
        validate_target(current, allow_http_onion=current.startswith(cfg["base_url"]))
        with (
            client(cfg) as session,
            session.stream(
                "GET", current, headers=headers, cookies=cookies
            ) as response,
        ):
            if response.status_code in REDIRECTS:
                location = response.headers.get("location")
                if hop == MAX_REDIRECTS or not location:
                    raise CliError(
                        "download redirect limit exceeded or Location missing"
                    )
                current = urljoin(current, location)
                continue
            response.raise_for_status()
            written = 0
            with output.open("wb") as handle:
                for chunk in response.iter_bytes():
                    written += len(chunk)
                    if written > max_bytes:
                        handle.close()
                        output.unlink(missing_ok=True)
                        raise CliError(
                            f"download exceeds {max_bytes // 1024 // 1024} MiB"
                        )
                    handle.write(chunk)
            return written
    raise CliError("download redirect failure")


def cmd_download(cfg: dict, args) -> None:
    if not auth(cfg)[0]:
        raise CliError("download requires a session; run login or import-session")
    payload = request_json(
        cfg,
        "GET",
        f"/eapi/book/{args.id}/{args.hash}/file",
        authenticated=True,
    )
    file_info = payload.get("file") or {}
    url = str(file_info.get("downloadLink") or "")
    if not payload.get("success") or not url:
        raise CliError(str(payload.get("error") or "download link unavailable"))
    extension = safe_filename(str(file_info.get("extension") or "bin")).lower()
    description = str(file_info.get("description") or f"book_{args.id}")
    name = safe_filename(description.split(" (")[0])
    if not name.lower().endswith("." + extension):
        name += "." + extension
    output_dir = Path(args.output_dir).expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    output = output_dir / name
    max_bytes = max(1, min(args.max_mb, 512)) * 1024 * 1024
    size = download_stream(cfg, url, output, max_bytes)
    emit({"ok": True, "path": str(output), "bytes": size})


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description=__doc__)
    commands = root.add_subparsers(dest="command", required=True)
    commands.add_parser("doctor")
    login = commands.add_parser("login")
    login.add_argument("--email", required=True)
    commands.add_parser("import-session")
    register = commands.add_parser("register")
    register.add_argument("--email", required=True)
    register.add_argument("--name", required=True)
    register.add_argument("--yes", action="store_true")
    commands.add_parser("profile")
    search = commands.add_parser("search")
    search.add_argument("query")
    search.add_argument("--limit", type=int, default=5)
    search.add_argument("--language", default="")
    search.add_argument("--extension", default="")
    download = commands.add_parser("download")
    download.add_argument("--id", required=True)
    download.add_argument("--hash", required=True)
    download.add_argument("--output-dir", default="downloads")
    download.add_argument("--max-mb", type=int, default=80)
    return root


COMMANDS = {
    "doctor": cmd_doctor,
    "login": cmd_login,
    "import-session": cmd_import_session,
    "register": cmd_register,
    "profile": cmd_profile,
    "search": cmd_search,
    "download": cmd_download,
}


def main() -> int:
    args = parser().parse_args()
    try:
        COMMANDS[args.command](settings(), args)
    except (CliError, httpx.HTTPError, OSError) as exc:
        emit({"ok": False, "error": str(exc)})
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
