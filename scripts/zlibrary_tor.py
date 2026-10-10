#!/usr/bin/env python3
"""Portable Z-Library E-API CLI over Tor. All command output is JSON."""

from __future__ import annotations

import argparse
import getpass
import hashlib
import ipaddress
import json
import math
import os
import re
import socket
import stat
import sys
import tempfile
import time
import warnings
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
    def __init__(
        self, message: str, *, reason: str = "command_failed", stage: str | None = None
    ):
        super().__init__(message)
        self.reason = reason
        self.stage = stage


def secret(prompt: str, env: str, *, stdin: bool = False) -> str:
    if stdin:
        value = sys.stdin.readline().rstrip("\r\n")
    elif os.environ.get(env):
        value = os.environ[env]
    else:
        if not sys.stdin.isatty():
            raise CliError(
                "credentials require a private terminal or explicit secret input",
                reason="credentials_required",
            )
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("error", getpass.GetPassWarning)
                value = getpass.getpass(prompt)
        except (getpass.GetPassWarning, EOFError) as exc:
            raise CliError(
                "hidden credential input unavailable", reason="credentials_required"
            ) from exc
    if not value:
        raise CliError("empty credential input", reason="credentials_required")
    return value


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
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=".config-", suffix=".tmp")
    temp = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            if os.name != "nt":
                os.fchmod(handle.fileno(), stat.S_IRUSR | stat.S_IWUSR)
            json.dump(data, handle, ensure_ascii=False, indent=2)
        temp.replace(path)
    finally:
        temp.unlink(missing_ok=True)


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
    if (urlparse(cfg["base_url"]).hostname or "").endswith(".onion") and not str(
        cfg["proxy"]
    ).lower().startswith(("socks5://", "socks5h://")):
        raise CliError("onion access requires a SOCKS5 Tor proxy")
    try:
        cfg["timeout"] = float(
            os.environ.get("ZLIBRARY_TOR_TIMEOUT", cfg.get("timeout", 45))
        )
        cfg["retries"] = int(
            os.environ.get("ZLIBRARY_TOR_RETRIES", cfg.get("retries", 1))
        )
    except (TypeError, ValueError) as exc:
        raise CliError("timeout and retries must be numeric") from exc
    if (
        not math.isfinite(cfg["timeout"])
        or not 1 <= cfg["timeout"] <= 600
        or not 0 <= cfg["retries"] <= 2
    ):
        raise CliError("timeout must be 1..600 seconds; retries must be 0..2")
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
    if parsed.username is not None or parsed.password is not None:
        raise CliError("target URL must not contain credentials")
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
        timeout=httpx.Timeout(cfg.get("timeout", 45.0)),
        headers=HEADERS,
        follow_redirects=False,
        trust_env=False,
    )


def request_json(
    cfg: dict,
    method: str,
    path: str,
    *,
    data: dict | None = None,
    authenticated: bool = False,
    retryable: bool = False,
) -> dict:
    headers, cookies = auth(cfg) if authenticated else ({}, {})
    attempts = 1 + (cfg.get("retries", 1) if retryable else 0)
    for attempt in range(attempts):
        try:
            with client(cfg) as session:
                session.cookies.update(cookies)
                response = session.request(
                    method,
                    cfg["base_url"] + path,
                    data=data,
                    headers=headers,
                )
            if response.status_code not in {502, 503, 504} or attempt + 1 == attempts:
                break
        except httpx.TransportError as exc:
            if attempt + 1 == attempts:
                raise CliError(
                    f"network error: {type(exc).__name__}", reason="network_error"
                ) from exc
        time.sleep(0.5 * 2**attempt)
    if response.status_code in REDIRECTS:
        raise CliError(f"unexpected API redirect (HTTP {response.status_code})")
    try:
        payload = response.json()
    except ValueError as exc:
        raise CliError(
            f"API returned non-JSON HTTP {response.status_code}",
            reason="invalid_response",
        ) from exc
    if not isinstance(payload, dict):
        raise CliError(
            "API returned a non-object JSON response", reason="invalid_response"
        )
    if response.status_code >= 400:
        raise CliError(f"API HTTP {response.status_code}", reason="api_http_error")
    return payload


def extract_session(payload: dict) -> tuple[str, str]:
    user = payload.get("user") or payload.get("response") or {}
    if not isinstance(user, dict):
        raise CliError("invalid login response", reason="invalid_response")
    uid = str(user.get("id") or user.get("user_id") or "")
    key = str(user.get("remix_userkey") or user.get("user_key") or "")
    if not payload.get("success") or not uid or not key:
        raise CliError(
            "login failed; check credentials or complete browser challenges",
            reason="login_failed",
        )
    return uid, key


def emit(value: object) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2))


def cmd_doctor(cfg: dict, _args) -> None:
    proxy = urlparse(cfg["proxy"])
    try:
        with socket.create_connection(
            (proxy.hostname or "127.0.0.1", proxy.port or 9050),
            timeout=min(cfg.get("timeout", 45), 5),
        ):
            pass
    except OSError as exc:
        raise CliError(
            "cannot connect to the configured proxy; check Tor listener",
            reason="proxy_unreachable",
            stage="local_proxy",
        ) from exc
    try:
        payload = request_json(cfg, "GET", "/eapi/info", retryable=True)
    except CliError as exc:
        exc.stage = "api_response" if exc.reason != "network_error" else "onion_request"
        raise
    if not payload.get("success"):
        raise CliError(
            "API health check failed", reason="api_failed", stage="api_response"
        )
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
    password = secret(
        "Z-Library password: ", "ZLIBRARY_PASSWORD", stdin=args.stdin_password
    )
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
    uid = secret("remix_userid: ", "ZLIBRARY_REMIX_USERID").strip()
    key = secret("remix_userkey: ", "ZLIBRARY_REMIX_USERKEY").strip()
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
    password = secret("New password: ", "ZLIBRARY_PASSWORD")
    repeated = secret("Repeat password: ", "ZLIBRARY_PASSWORD")
    if password != repeated or not password:
        raise CliError("passwords do not match or are empty")
    payload = request_json(
        cfg,
        "POST",
        "/eapi/user/registration",
        data={"email": args.email, "password": password, "name": args.name},
    )
    if not payload.get("success"):
        raise CliError(
            "registration failed; complete registration in Tor Browser",
            reason="registration_failed",
        )
    emit({"ok": True, "message": "complete any email confirmation in your browser"})


def cmd_logout(_cfg: dict, _args) -> None:
    stored = load_config()
    for key in ("remix_userid", "remix_userkey"):
        stored.pop(key, None)
    if config_path().exists():
        save_config(stored)
    emit(
        {
            "ok": True,
            "environment_session_present": bool(
                os.environ.get("ZLIBRARY_REMIX_USERID")
                or os.environ.get("ZLIBRARY_REMIX_USERKEY")
            ),
            "server_session_revoked": False,
        }
    )


def cmd_profile(cfg: dict, _args) -> None:
    if not auth(cfg)[0]:
        raise CliError("no session; run login or import-session")
    payload = request_json(cfg, "GET", "/eapi/user/profile", authenticated=True)
    if not payload.get("success"):
        raise CliError("profile request failed", reason="profile_failed")
    emit(redact_credentials(payload))


def redact_credentials(value):
    if isinstance(value, dict):
        return {
            key: redact_credentials(item)
            for key, item in value.items()
            if key.lower().replace("-", "_")
            not in {
                "password",
                "remix_userkey",
                "user_key",
                "token",
                "access_token",
                "refresh_token",
            }
        }
    if isinstance(value, list):
        return [redact_credentials(item) for item in value]
    return value


def cmd_search(cfg: dict, args) -> None:
    data: dict[str, object] = {
        "message": args.query,
        "limit": args.limit,
        "page": args.page,
    }
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
        retryable=True,
    )
    raw_books = payload.get("books") or []
    if not isinstance(raw_books, list) or any(
        not isinstance(book, dict) for book in raw_books
    ):
        raise CliError("invalid search response", reason="invalid_response")
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
        for book in raw_books
    ]
    if not payload.get("success"):
        raise CliError("search failed", reason="search_failed")
    emit({"ok": True, "page": args.page, "count": len(books), "books": books})


def safe_filename(value: str) -> str:
    cleaned = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", value).strip(" .")
    cleaned = cleaned[:140].rstrip(" .") or "book"
    if cleaned.split(".")[0].upper() in {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        *(f"COM{i}" for i in range(10)),
        *(f"LPT{i}" for i in range(10)),
    }:
        cleaned = "_" + cleaned
    return cleaned


def download_stream(cfg: dict, url: str, output: Path, max_bytes: int) -> int:
    current = url
    for hop in range(MAX_REDIRECTS + 1):
        same_origin = origin(current) == origin(cfg["base_url"])
        validate_target(current, allow_http_onion=same_origin)
        headers, cookies = auth(cfg) if same_origin else ({}, {})
        headers = {**headers, "Accept-Encoding": "identity"}
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
            if response.status_code != 200:
                raise CliError(
                    "expected a complete file response", reason="invalid_download"
                )
            if (
                response.headers.get("content-encoding", "identity").lower()
                != "identity"
            ):
                raise CliError(
                    "unexpected encoded file response", reason="invalid_download"
                )
            media_type = response.headers.get("content-type", "").split(";")[0].lower()
            if media_type in {"text/html", "application/json"}:
                raise CliError(
                    "server returned a page instead of a book",
                    reason="invalid_download",
                )
            length = response.headers.get("content-length")
            if length is not None and (
                not length.isdecimal() or int(length) > max_bytes
            ):
                raise CliError(
                    "invalid or oversized Content-Length", reason="invalid_download"
                )
            written = 0
            fd, name = tempfile.mkstemp(
                dir=output.parent, prefix=".zlib-", suffix=".part"
            )
            temporary = Path(name)
            try:
                with os.fdopen(fd, "wb") as handle:
                    for chunk in response.iter_bytes():
                        written += len(chunk)
                        if written > max_bytes:
                            raise CliError(
                                "download exceeds size limit", reason="size_limit"
                            )
                        handle.write(chunk)
                    if not written or (length is not None and written != int(length)):
                        raise CliError(
                            "empty or incomplete download", reason="incomplete_download"
                        )
                    handle.flush()
                    os.fsync(handle.fileno())
                temporary.replace(output)
            finally:
                temporary.unlink(missing_ok=True)
            return written
    raise CliError("download redirect failure")


def origin(url: str) -> tuple:
    parsed = urlparse(url)
    return (
        parsed.scheme,
        parsed.hostname,
        parsed.port or (443 if parsed.scheme == "https" else 80),
    )


def digest(path: Path) -> str:
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def cache_key(cfg: dict, args) -> str:
    return hashlib.sha256(
        f"{cfg['base_url']}\n{args.id}\n{args.hash}".encode()
    ).hexdigest()


def cached_file(manifest: Path, output_dir: Path, max_bytes: int) -> Path | None:
    try:
        record = json.loads(manifest.read_text(encoding="utf-8"))
        if not isinstance(record, dict) or not isinstance(record.get("name"), str):
            return None
        output = output_dir / record["name"]
        if (
            output.is_symlink()
            or output.resolve().parent != output_dir
            or not output.is_file()
        ):
            return None
        size = output.stat().st_size
        if (
            0 < size <= max_bytes
            and size == record.get("bytes")
            and digest(output) == record.get("sha256")
        ):
            return output
    except (OSError, ValueError):
        pass
    return None


def cmd_download(cfg: dict, args) -> None:
    output_dir = Path(args.output_dir).expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    lock = output_dir / f".zlib-{cache_key(cfg, args)}.lock"
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as exc:
        raise CliError(
            "download already running; remove stale lock only after checking no process is active",
            reason="download_locked",
        ) from exc
    try:
        os.close(fd)
        download_locked(cfg, args, output_dir)
    finally:
        lock.unlink(missing_ok=True)


def download_locked(cfg: dict, args, output_dir: Path) -> None:
    key = cache_key(cfg, args)
    manifest = output_dir / f".zlib-{key}.json"
    max_bytes = args.max_mb * 1024 * 1024
    existing = cached_file(manifest, output_dir, max_bytes)
    if existing and not args.force:
        emit(
            {
                "ok": True,
                "status": "cached",
                "path": str(existing),
                "bytes": existing.stat().st_size,
            }
        )
        return
    if not auth(cfg)[0]:
        raise CliError("download requires a session; run login or import-session")
    payload = request_json(
        cfg,
        "GET",
        f"/eapi/book/{args.id}/{args.hash}/file",
        authenticated=True,
    )
    file_info = payload.get("file") or {}
    if not isinstance(file_info, dict):
        raise CliError("invalid file response", reason="invalid_response")
    url = str(file_info.get("downloadLink") or "")
    if not payload.get("success") or not url:
        message = str(payload.get("error") or payload.get("message") or "").lower()
        # Conservative recognition only; unknown failures must not be labelled quota errors.
        quota = any(
            term in message
            for term in (
                "daily download limit",
                "daily limit reached",
                "download quota exceeded",
                "daily_quota_exhausted",
            )
        )
        raise CliError(
            "daily download quota exhausted" if quota else "download link unavailable",
            reason="daily_quota_exhausted" if quota else "download_link_unavailable",
        )
    extension = safe_filename(str(file_info.get("extension") or "bin")).lower()
    description = str(file_info.get("description") or f"book_{args.id}")
    name = safe_filename(description.split(" (")[0])
    if name.lower().endswith("." + extension):
        name = name[: -(len(extension) + 1)]
    extension = extension[:12]
    name = f"{name}-{key[:16]}.{extension}"
    output = output_dir / name
    if output.exists() and not args.force:
        raise CliError(
            "unverified existing file; use --force to replace", reason="file_exists"
        )
    size = download_stream(cfg, url, output, max_bytes)
    record = {"name": output.name, "bytes": size, "sha256": digest(output)}
    fd, temp_name = tempfile.mkstemp(dir=output_dir, prefix=".zlib-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(record, handle)
        Path(temp_name).replace(manifest)
    finally:
        Path(temp_name).unlink(missing_ok=True)
    emit({"ok": True, "status": "downloaded", "path": str(output), "bytes": size})


class JsonParser(argparse.ArgumentParser):
    def error(self, message):
        # argparse messages may echo secret or untrusted command-line values.
        raise CliError(
            "invalid arguments; run --help for usage", reason="invalid_arguments"
        )


def positive(value: str) -> int:
    number = int(value)
    if number < 1:
        raise ValueError("must be positive")
    return number


def identifier(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_-]+", value):
        raise ValueError("invalid book identifier")
    return value


def parser() -> argparse.ArgumentParser:
    root = JsonParser(description=__doc__)
    commands = root.add_subparsers(dest="command", required=True)
    commands.add_parser("doctor")
    login = commands.add_parser("login")
    login.add_argument("--email", required=True)
    login.add_argument("--stdin-password", action="store_true")
    commands.add_parser("import-session")
    commands.add_parser("logout")
    register = commands.add_parser("register")
    register.add_argument("--email", required=True)
    register.add_argument("--name", required=True)
    register.add_argument("--yes", action="store_true")
    commands.add_parser("profile")
    search = commands.add_parser("search")
    search.add_argument("query")
    search.add_argument("--limit", type=positive, choices=range(1, 21), default=5)
    search.add_argument("--page", type=positive, default=1)
    search.add_argument("--language", default="")
    search.add_argument("--extension", default="")
    download = commands.add_parser("download")
    download.add_argument("--id", type=identifier, required=True)
    download.add_argument("--hash", type=identifier, required=True)
    download.add_argument("--output-dir", default="downloads")
    download.add_argument("--max-mb", type=positive, choices=range(1, 513), default=80)
    download.add_argument("--force", action="store_true")
    return root


COMMANDS = {
    "doctor": cmd_doctor,
    "login": cmd_login,
    "import-session": cmd_import_session,
    "logout": cmd_logout,
    "register": cmd_register,
    "profile": cmd_profile,
    "search": cmd_search,
    "download": cmd_download,
}


def main() -> int:
    try:
        args = parser().parse_args()
        COMMANDS[args.command]({} if args.command == "logout" else settings(), args)
    except (CliError, httpx.HTTPError, OSError, ValueError) as exc:
        result = {
            "ok": False,
            "error": str(exc) if isinstance(exc, CliError) else type(exc).__name__,
            "reason": getattr(exc, "reason", "command_failed"),
        }
        if getattr(exc, "stage", None):
            result["stage"] = exc.stage
        emit(result)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
