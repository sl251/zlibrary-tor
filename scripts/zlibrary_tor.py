#!/usr/bin/env python3
"""Portable Z-Library E-API CLI over Tor. All command output is JSON."""

from __future__ import annotations

import argparse
import base64
import contextlib
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
MAX_PROBE_BYTES = 64 * 1024


class CliError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        reason: str = "command_failed",
        stage: str | None = None,
        status_code: int | None = None,
    ):
        super().__init__(message)
        self.reason = reason
        self.stage = stage
        self.status_code = status_code


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


def is_valid_v3_onion(host: str) -> bool:
    if not isinstance(host, str):
        return False
    host = host.lower()
    if not host.endswith(".onion") or host.endswith(".onion."):
        return False
    label = host[:-6]
    if "." in label:
        return False
    if not re.fullmatch(r"[a-z2-7]{56}", label):
        return False
    try:
        raw = base64.b32decode(label.upper())
    except Exception:
        return False
    if len(raw) != 35:
        return False
    pubkey = raw[:32]
    checksum = raw[32:34]
    version = raw[34]
    if version != 3:
        return False
    expected_checksum = hashlib.sha3_256(
        b".onion checksum" + pubkey + bytes([version])
    ).digest()[:2]
    return checksum == expected_checksum


def validate_onion_origin(url: str) -> str:
    if not isinstance(url, str):
        raise CliError("invalid onion origin", reason="invalid_origin")
    try:
        parsed = urlparse(url)
    except Exception as exc:
        raise CliError("invalid onion origin", reason="invalid_origin") from exc

    scheme = (parsed.scheme or "").lower()
    if scheme not in {"http", "https"}:
        raise CliError("invalid onion origin", reason="invalid_origin")

    if (
        parsed.username is not None
        or parsed.password is not None
        or "@" in parsed.netloc
    ):
        raise CliError("invalid onion origin", reason="invalid_origin")

    if parsed.path not in {"", "/"}:
        raise CliError("invalid onion origin", reason="invalid_origin")

    if parsed.query or parsed.fragment:
        raise CliError("invalid onion origin", reason="invalid_origin")

    port = parsed.port
    if port is not None:
        if scheme == "http" and port != 80:
            raise CliError("invalid onion origin", reason="invalid_origin")
        if scheme == "https" and port != 443:
            raise CliError("invalid onion origin", reason="invalid_origin")

    host = (parsed.hostname or "").lower()
    if not is_valid_v3_onion(host):
        raise CliError("invalid onion origin", reason="invalid_origin")

    return f"{scheme}://{host}"


def normalize_origin(url: str) -> str:
    parsed = urlparse(url)
    scheme = (parsed.scheme or "http").lower()
    host = (parsed.hostname or "").lower()
    port = parsed.port
    if (
        port is None
        or (scheme == "http" and port == 80)
        or (scheme == "https" and port == 443)
    ):
        return f"{scheme}://{host}"
    return f"{scheme}://{host}:{port}"


def _normalize_domains(cfg: dict) -> dict:
    raw_domains = cfg.get("domains")
    primary_origin = normalize_origin(DEFAULT_BASE_URL)
    if raw_domains is None:
        domains = {
            "primary": {
                "url": primary_origin,
                "trust": "builtin",
                "source": "repository-default",
                "verified_at": None,
            }
        }
        custom_base = cfg.get("base_url")
        if custom_base:
            norm_custom = normalize_origin(custom_base)
            if norm_custom != primary_origin:
                domains["custom"] = {
                    "url": norm_custom,
                    "trust": "legacy_unverified",
                    "source": "legacy-config",
                    "verified_at": None,
                }
        return domains

    if not isinstance(raw_domains, dict):
        raise CliError(
            "domains in config must be a dictionary", reason="invalid_config"
        )

    domains = {}
    for name, d in raw_domains.items():
        if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,32}", name):
            raise CliError("invalid domain record in config", reason="invalid_config")
        if not isinstance(d, dict) or not isinstance(d.get("url"), str):
            raise CliError("invalid domain record in config", reason="invalid_config")
        trust = d.get("trust", "unverified")
        url_val = normalize_origin(d["url"])
        if trust == "builtin" and url_val != primary_origin:
            trust = "legacy_unverified"
        domains[name] = {
            "url": url_val,
            "trust": trust,
            "source": d.get("source"),
            "verified_at": d.get("verified_at"),
        }

    if "primary" not in domains:
        domains["primary"] = {
            "url": primary_origin,
            "trust": "builtin",
            "source": "repository-default",
            "verified_at": None,
        }

    return domains


def is_trusted_origin(cfg: dict, origin: str) -> bool:
    norm_origin = normalize_origin(origin)
    domains = cfg.get("domains")
    if not isinstance(domains, dict):
        return norm_origin == normalize_origin(DEFAULT_BASE_URL)
    for d in domains.values():
        if isinstance(d, dict) and d.get("trust") in {"builtin", "user_verified"}:
            if normalize_origin(str(d.get("url", ""))) == norm_origin:
                return True
    return False


def require_trusted_origin(cfg: dict, origin: str | None = None) -> None:
    target = origin or cfg.get("base_url", "")
    if not is_trusted_origin(cfg, target):
        raise CliError(
            "target origin is not a trusted domain",
            reason="untrusted_origin",
        )


@contextlib.contextmanager
def exclusive_lock(lock_path: Path, *, reason: str, message: str):
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as exc:
        raise CliError(message, reason=reason) from exc
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(f"pid={os.getpid()}\ntime={time.time()}\n")
        yield
    finally:
        try:
            lock_path.unlink(missing_ok=True)
        except OSError:
            pass


def validate_account_name(name: str) -> None:
    if not isinstance(name, str):
        raise CliError("invalid account alias", reason="invalid_account_alias")
    if name in {"auto", "env"}:
        raise CliError(
            f"'{name}' is a reserved account alias", reason="invalid_account_alias"
        )
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,32}", name):
        raise CliError(
            "account alias must match [A-Za-z0-9_-]{1,32}",
            reason="invalid_account_alias",
        )


def normalize_config(raw: dict) -> dict:
    if not isinstance(raw, dict):
        raise CliError("invalid config file", reason="invalid_config")

    cfg = dict(raw)
    schema_version = cfg.get("schema_version")
    if schema_version is not None:
        if not isinstance(schema_version, int):
            raise CliError("invalid config schema version", reason="invalid_config")
        if schema_version > 2:
            raise CliError(
                "unsupported config schema version",
                reason="unsupported_config_version",
            )

    if schema_version == 2:
        accounts = cfg.get("accounts", {})
        if not isinstance(accounts, dict):
            raise CliError(
                "accounts in config must be a dictionary", reason="invalid_config"
            )
        for name, acc in accounts.items():
            validate_account_name(name)
            if not isinstance(acc, dict):
                raise CliError(
                    f"account '{name}' must be a dictionary", reason="invalid_config"
                )
        default_acc = cfg.get("default_account")
        if default_acc is not None and not isinstance(default_acc, str):
            raise CliError(
                "default_account must be a string or null", reason="invalid_config"
            )
        cfg.setdefault("accounts", {})
        cfg.setdefault("base_url", DEFAULT_BASE_URL)
        cfg.setdefault("proxy", DEFAULT_PROXY)
        cfg.setdefault("timeout", 45)
        cfg.setdefault("retries", 1)
        cfg.setdefault("default_account", None)
        cfg["domains"] = _normalize_domains(cfg)
        return cfg

    # Legacy config (no schema_version or < 2)
    if "base_url" in raw and not isinstance(raw["base_url"], str):
        raise CliError("base_url must be a string", reason="invalid_config")
    if "proxy" in raw and not isinstance(raw["proxy"], str):
        raise CliError("proxy must be a string", reason="invalid_config")
    if "timeout" in raw and not isinstance(raw["timeout"], (int, float)):
        raise CliError("timeout must be numeric", reason="invalid_config")
    if "retries" in raw and not isinstance(raw["retries"], int):
        raise CliError("retries must be an integer", reason="invalid_config")

    uid = cfg.pop("remix_userid", None)
    key = cfg.pop("remix_userkey", None)
    has_uid = bool(uid)
    has_key = bool(key)
    if has_uid != has_key:
        raise CliError(
            "incomplete session in legacy config", reason="incomplete_session"
        )

    cfg["schema_version"] = 2
    cfg.setdefault("base_url", DEFAULT_BASE_URL)
    cfg.setdefault("proxy", DEFAULT_PROXY)
    cfg.setdefault("timeout", 45)
    cfg.setdefault("retries", 1)

    accounts = (
        dict(cfg.get("accounts", {})) if isinstance(cfg.get("accounts"), dict) else {}
    )
    default_acc = cfg.get("default_account")

    if has_uid and has_key:
        origin_val = normalize_origin(raw.get("base_url") or DEFAULT_BASE_URL)
        accounts["default"] = {
            "remix_userid": str(uid),
            "remix_userkey": str(key),
            "session_origin": origin_val,
        }
        default_acc = "default"

    cfg["accounts"] = accounts
    cfg["default_account"] = default_acc
    cfg["domains"] = _normalize_domains(cfg)
    return cfg


def config_path() -> Path:
    override = os.environ.get("ZLIBRARY_TOR_CONFIG")
    if override:
        return Path(override).expanduser().resolve()
    if os.name == "nt" and os.environ.get("APPDATA"):
        return (Path(os.environ["APPDATA"]) / "zlibrary-tor" / "config.json").resolve()
    return (Path.home() / ".config" / "zlibrary-tor" / "config.json").resolve()


def load_config() -> dict:
    path = config_path()
    if not path.exists():
        return normalize_config({})
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CliError(f"cannot read config: {exc}", reason="invalid_config") from exc
    if not isinstance(data, dict):
        raise CliError("invalid config file", reason="invalid_config")

    # If legacy config needs migration and file exists, persist it under lock
    if "schema_version" not in data or data["schema_version"] < 2:
        lock_path = path.parent / (path.name + ".lock")
        with exclusive_lock(
            lock_path, reason="config_locked", message="cannot acquire config lock"
        ):
            # Re-read under lock
            try:
                reloaded = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise CliError(
                    f"cannot read config: {exc}", reason="invalid_config"
                ) from exc
            if not isinstance(reloaded, dict):
                raise CliError("invalid config file", reason="invalid_config")
            migrated = normalize_config(reloaded)
            try:
                save_config(migrated)
            except OSError as exc:
                raise CliError(
                    "cannot write migrated config", reason="config_write_failed"
                ) from exc
            return migrated
    return normalize_config(data)


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


def update_config(updater) -> dict:
    path = config_path()
    lock_path = path.parent / (path.name + ".lock")
    with exclusive_lock(
        lock_path, reason="config_locked", message="cannot acquire config lock"
    ):
        if not path.exists():
            cfg = normalize_config({})
        else:
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise CliError(
                    f"cannot read config: {exc}", reason="invalid_config"
                ) from exc
            if not isinstance(data, dict):
                raise CliError("invalid config file", reason="invalid_config")
            cfg = normalize_config(data)
        updater(cfg)
        save_config(cfg)
        return cfg


def settings() -> dict:
    cfg = load_config()
    cfg["base_url"] = os.environ.get(
        "ZLIBRARY_BASE_URL", cfg.get("base_url", DEFAULT_BASE_URL)
    ).rstrip("/")
    cfg["proxy"] = os.environ.get("ZLIBRARY_TOR_PROXY", cfg.get("proxy", DEFAULT_PROXY))
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
        if not is_valid_v3_onion(host):
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
    require_trusted_origin(cfg)
    origin_val = str(cfg.get("session_origin") or "")
    if origin_val and normalize_origin(origin_val) != normalize_origin(
        cfg.get("base_url", "")
    ):
        raise CliError(
            "session origin does not match base URL", reason="session_origin_mismatch"
        )
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
    if authenticated:
        require_trusted_origin(cfg)
    headers, cookies = auth(cfg) if authenticated else ({}, {})
    attempts = 1 + (cfg.get("retries", 1) if retryable else 0)
    response = None
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
    if response is None:
        raise CliError("no response received", reason="network_error")
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
    if response.status_code == 401:
        raise CliError("API HTTP 401", reason="session_invalid", status_code=401)
    if response.status_code == 429:
        raise CliError("API HTTP 429", reason="rate_limited", status_code=429)
    if response.status_code == 403:
        raise CliError("API HTTP 403", reason="profile_failed", status_code=403)
    if response.status_code >= 400:
        raise CliError(
            f"API HTTP {response.status_code}",
            reason="api_http_error",
            status_code=response.status_code,
        )
    return payload


def fetch_profile(cfg: dict) -> dict:
    payload = request_json(
        cfg, "GET", "/eapi/user/profile", authenticated=True, retryable=False
    )
    if not payload.get("success"):
        raise CliError("profile request failed", reason="profile_failed")
    return payload


def _parse_non_negative_int(val: object) -> int | None:
    if isinstance(val, bool) or val is None:
        return None
    if isinstance(val, int):
        return val if val >= 0 else None
    if isinstance(val, str) and val.isdecimal():
        return int(val)
    return None


def parse_quota(user: dict | None) -> dict:
    checked_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    if not isinstance(user, dict):
        return {
            "status": "unknown",
            "limit": None,
            "used": None,
            "remaining": None,
            "checked_at": checked_at,
        }
    used = _parse_non_negative_int(user.get("downloads_today"))
    limit = _parse_non_negative_int(user.get("downloads_limit"))
    if used is None or limit is None:
        return {
            "status": "unknown",
            "limit": None,
            "used": None,
            "remaining": None,
            "checked_at": checked_at,
        }
    remaining = max(limit - used, 0)
    status = "available" if remaining > 0 else "exhausted"
    return {
        "status": status,
        "limit": limit,
        "used": used,
        "remaining": remaining,
        "checked_at": checked_at,
    }


def resolve_account(
    cfg: dict, account_name: str | None, *, require: bool = False
) -> tuple[str | None, dict | None]:
    # 1. Explicit account name
    if account_name is not None:
        if account_name == "env":
            uid = os.environ.get("ZLIBRARY_REMIX_USERID", "")
            key = os.environ.get("ZLIBRARY_REMIX_USERKEY", "")
            has_uid = bool(uid)
            has_key = bool(key)
            if not (has_uid and has_key):
                if has_uid != has_key:
                    raise CliError(
                        "incomplete environment session",
                        reason="incomplete_environment_session",
                    )
                raise CliError(
                    "environment session required when --account env is specified",
                    reason="account_required",
                )
            origin_val = os.environ.get("ZLIBRARY_SESSION_ORIGIN", "")
            if not origin_val:
                raise CliError(
                    "environment session requires ZLIBRARY_SESSION_ORIGIN",
                    reason="session_origin_missing",
                )
            if normalize_origin(origin_val) != normalize_origin(cfg["base_url"]):
                raise CliError(
                    "session origin does not match base URL",
                    reason="session_origin_mismatch",
                )
            return "env", {
                "remix_userid": uid,
                "remix_userkey": key,
                "session_origin": origin_val,
            }

        validate_account_name(account_name)
        accounts = cfg.get("accounts", {})
        if account_name not in accounts:
            raise CliError(
                f"account '{account_name}' not found", reason="account_not_found"
            )
        acc = accounts[account_name]
        if not isinstance(acc, dict):
            raise CliError(
                f"account '{account_name}' not found", reason="account_not_found"
            )
        uid = str(acc.get("remix_userid") or "")
        key = str(acc.get("remix_userkey") or "")
        if not uid or not key:
            if require:
                raise CliError(
                    f"account '{account_name}' has no saved session",
                    reason="account_required",
                )
            return account_name, None
        origin_val = str(acc.get("session_origin") or "")
        if not origin_val:
            raise CliError(
                "account has no bound session origin",
                reason="session_origin_missing",
            )
        if normalize_origin(origin_val) != normalize_origin(cfg["base_url"]):
            raise CliError(
                "session origin does not match base URL",
                reason="session_origin_mismatch",
            )
        return account_name, acc

    # 2. Account name not specified: check environment session
    uid = os.environ.get("ZLIBRARY_REMIX_USERID", "")
    key = os.environ.get("ZLIBRARY_REMIX_USERKEY", "")
    has_uid = bool(uid)
    has_key = bool(key)
    if has_uid != has_key:
        raise CliError(
            "incomplete environment session",
            reason="incomplete_environment_session",
        )
    if has_uid and has_key:
        origin_val = os.environ.get("ZLIBRARY_SESSION_ORIGIN", "")
        if not origin_val:
            raise CliError(
                "environment session requires ZLIBRARY_SESSION_ORIGIN",
                reason="session_origin_missing",
            )
        if normalize_origin(origin_val) != normalize_origin(cfg["base_url"]):
            raise CliError(
                "session origin does not match base URL",
                reason="session_origin_mismatch",
            )
        return "env", {
            "remix_userid": uid,
            "remix_userkey": key,
            "session_origin": origin_val,
        }

    # 3. Default account in config
    default_name = cfg.get("default_account")
    if default_name and default_name in cfg.get("accounts", {}):
        acc = cfg["accounts"][default_name]
        if isinstance(acc, dict):
            uid = str(acc.get("remix_userid") or "")
            key = str(acc.get("remix_userkey") or "")
            if uid and key:
                origin_val = str(acc.get("session_origin") or "")
                if not origin_val:
                    raise CliError(
                        "account has no bound session origin",
                        reason="session_origin_missing",
                    )
                if normalize_origin(origin_val) != normalize_origin(cfg["base_url"]):
                    raise CliError(
                        "session origin does not match base URL",
                        reason="session_origin_mismatch",
                    )
                return default_name, acc
            elif require:
                raise CliError(
                    f"default account '{default_name}' has no saved session",
                    reason="account_required",
                )

    # 4. Fallback for test fixture setting remix_userid/key directly on cfg:
    cfg_uid = str(cfg.get("remix_userid") or "")
    cfg_key = str(cfg.get("remix_userkey") or "")
    if cfg_uid and cfg_key:
        origin_val = str(
            cfg.get("session_origin")
            or normalize_origin(cfg.get("base_url", DEFAULT_BASE_URL))
        )
        return cfg.get("_account_name") or "default", {
            "remix_userid": cfg_uid,
            "remix_userkey": cfg_key,
            "session_origin": origin_val,
        }

    if require:
        raise CliError(
            "authentication requires an account; run login or import-session",
            reason="account_required",
        )
    return None, None


def select_download_account(cfg: dict) -> tuple[str, dict]:
    accounts = cfg.get("accounts", {})
    if not isinstance(accounts, dict) or not accounts:
        raise CliError(
            "no account with saved session available", reason="account_required"
        )

    default_acc = cfg.get("default_account")
    ordered_names: list[str] = []
    if default_acc and default_acc in accounts:
        ordered_names.append(default_acc)
    for name in sorted(accounts.keys()):
        if name != default_acc:
            ordered_names.append(name)

    seen_identities: set[tuple[str, str]] = set()
    candidates: list[tuple[str, dict]] = []
    for name in ordered_names:
        acc = accounts[name]
        if not isinstance(acc, dict):
            continue
        uid = str(acc.get("remix_userid") or "")
        key = str(acc.get("remix_userkey") or "")
        origin_val = str(acc.get("session_origin") or "")
        if not uid or not key or not origin_val:
            continue
        if normalize_origin(origin_val) != normalize_origin(cfg["base_url"]):
            continue
        identity = (uid, normalize_origin(origin_val))
        if identity in seen_identities:
            continue
        seen_identities.add(identity)
        candidates.append((name, acc))

    if not candidates:
        raise CliError(
            "no account with saved session available", reason="account_required"
        )

    for name, acc in candidates:
        acc_cfg = dict(cfg)
        acc_cfg["remix_userid"] = acc["remix_userid"]
        acc_cfg["remix_userkey"] = acc["remix_userkey"]
        acc_cfg["session_origin"] = acc["session_origin"]
        acc_cfg["_account_name"] = name

        try:
            profile_payload = fetch_profile(acc_cfg)
        except CliError as exc:
            if exc.reason == "rate_limited":
                raise CliError(
                    "rate limit encountered during account selection",
                    reason="rate_limited",
                ) from exc
            if exc.reason == "session_invalid":
                continue
            raise CliError(
                f"account selection incomplete: {exc}",
                reason="account_selection_incomplete",
            ) from exc

        user = (
            profile_payload.get("user")
            if isinstance(profile_payload.get("user"), dict)
            else {}
        )
        quota = parse_quota(user)
        if quota["status"] == "unknown":
            raise CliError(
                "account quota unknown", reason="account_selection_incomplete"
            )
        if quota["status"] == "exhausted":
            continue
        if quota["status"] == "available":
            return name, acc

    raise CliError(
        "no permitted account has confirmed remaining quota",
        reason="no_available_account",
    )


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


def probe_domain(cfg: dict, target_url: str) -> dict:
    norm_url = validate_onion_origin(target_url)
    probe_cfg = {
        "proxy": cfg.get("proxy", DEFAULT_PROXY),
        "timeout": cfg.get("timeout", 45.0),
    }

    try:
        with client(probe_cfg) as session:
            with session.stream(
                "GET",
                norm_url + "/eapi/info",
                headers=HEADERS,
            ) as response:
                if response.status_code in REDIRECTS:
                    raise CliError(
                        f"API redirect HTTP {response.status_code}",
                        reason="api_redirect",
                    )
                if response.status_code != 200:
                    raise CliError(
                        f"API HTTP {response.status_code}",
                        reason="api_failed",
                        status_code=response.status_code,
                    )
                content = bytearray()
                for chunk in response.iter_bytes():
                    content.extend(chunk)
                    if len(content) > MAX_PROBE_BYTES:
                        raise CliError(
                            "response exceeded 64 KiB limit",
                            reason="response_oversized",
                        )
    except httpx.TransportError as exc:
        raise CliError(
            f"network error: {type(exc).__name__}", reason="network_error"
        ) from exc

    try:
        payload = json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CliError(
            "API returned non-JSON response", reason="invalid_response"
        ) from exc

    if not isinstance(payload, dict):
        raise CliError(
            "API returned non-object JSON response", reason="invalid_response"
        )

    success_val = payload.get("success")
    if success_val not in (1, True):
        raise CliError(
            "API returned unsuccessful response",
            reason="api_failed",
            stage="api_response",
        )

    domains = cfg.get("domains", {})
    trust_status = "unverified"
    if isinstance(domains, dict):
        for d in domains.values():
            if isinstance(d, dict) and normalize_origin(d.get("url", "")) == norm_url:
                trust_status = d.get("trust", "unverified")
                break

    return {
        "ok": True,
        "base_url": norm_url,
        "reachable": True,
        "api_compatible": True,
        "ownership_verified": False,
        "trust": trust_status,
        "credentials_sent": False,
        "warnings": ["API compatibility does not prove Z-Library ownership."],
    }


def cmd_doctor(cfg: dict, args) -> None:
    account_arg = getattr(args, "account", None)
    if account_arg == "auto":
        raise CliError(
            "--account auto is only allowed for download",
            reason="invalid_arguments",
        )
    if account_arg:
        acc_name, acc_data = resolve_account(cfg, account_arg, require=True)
        authenticated = bool(
            acc_data and acc_data.get("remix_userid") and acc_data.get("remix_userkey")
        )
    else:
        acc_name, acc_data = resolve_account(cfg, None, require=False)
        authenticated = bool(
            acc_data and acc_data.get("remix_userid") and acc_data.get("remix_userkey")
        )

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
        payload = request_json(
            cfg, "GET", "/eapi/info", retryable=True, authenticated=False
        )
    except CliError as exc:
        exc.stage = "api_response" if exc.reason != "network_error" else "onion_request"
        raise
    if not payload.get("success"):
        raise CliError(
            "API health check failed", reason="api_failed", stage="api_response"
        )
    norm_base = normalize_origin(cfg["base_url"])
    domains = cfg.get("domains", {})
    trust_status = "unverified"
    if isinstance(domains, dict):
        for d in domains.values():
            if isinstance(d, dict) and normalize_origin(d.get("url", "")) == norm_base:
                trust_status = d.get("trust", "unverified")
                break

    result = {
        "ok": True,
        "base_url": cfg["base_url"],
        "proxy": "configured" if cfg.get("proxy") else "none",
        "authenticated": authenticated,
        "api_success": payload.get("success"),
        "reachable": True,
        "api_compatible": bool(payload.get("success") in (1, True)),
        "ownership_verified": False,
        "trust": trust_status,
    }
    if acc_name:
        result["account"] = acc_name
    if acc_data and acc_data.get("session_origin"):
        sess_orig = acc_data["session_origin"]
        result["session_origin"] = sess_orig
        result["session_origin_match"] = bool(normalize_origin(sess_orig) == norm_base)
    else:
        result["session_origin"] = None
        result["session_origin_match"] = None
    emit(result)


def cmd_login(cfg: dict, args) -> None:
    require_trusted_origin(cfg)
    account_name = getattr(args, "account", None)
    if account_name:
        validate_account_name(account_name)
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

    def updater(stored: dict) -> None:
        stored.update({"base_url": cfg["base_url"], "proxy": cfg["proxy"]})
        accounts = stored.setdefault("accounts", {})
        target_name = account_name
        if not target_name:
            target_name = stored.get("default_account") or "default"
        accounts[target_name] = {
            "remix_userid": uid,
            "remix_userkey": key,
            "session_origin": normalize_origin(cfg["base_url"]),
        }
        if not stored.get("default_account"):
            stored["default_account"] = target_name

    update_config(updater)
    target = account_name or load_config().get("default_account") or "default"
    emit({"ok": True, "account": target, "config": str(config_path())})


def cmd_import_session(cfg: dict, args) -> None:
    require_trusted_origin(cfg)
    account_name = getattr(args, "account", None)
    if account_name:
        validate_account_name(account_name)
    uid = secret("remix_userid: ", "ZLIBRARY_REMIX_USERID").strip()
    key = secret("remix_userkey: ", "ZLIBRARY_REMIX_USERKEY").strip()
    if not uid or not key:
        raise CliError("both session values are required")

    def updater(stored: dict) -> None:
        stored.update({"base_url": cfg["base_url"], "proxy": cfg["proxy"]})
        accounts = stored.setdefault("accounts", {})
        target_name = account_name
        if not target_name:
            target_name = stored.get("default_account") or "default"
        accounts[target_name] = {
            "remix_userid": uid,
            "remix_userkey": key,
            "session_origin": normalize_origin(cfg["base_url"]),
        }
        if not stored.get("default_account"):
            stored["default_account"] = target_name

    update_config(updater)
    target = account_name or load_config().get("default_account") or "default"
    emit({"ok": True, "account": target, "config": str(config_path())})


def cmd_register(cfg: dict, args) -> None:
    require_trusted_origin(cfg)
    if getattr(args, "account", None) == "auto":
        raise CliError(
            "--account auto is not supported for register",
            reason="invalid_arguments",
        )
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


def cmd_logout(_cfg: dict, args) -> None:
    account_name = getattr(args, "account", None)
    all_accounts = getattr(args, "all", False)

    def updater(stored: dict) -> None:
        accounts = stored.get("accounts", {})
        if all_accounts:
            for acc in accounts.values():
                if isinstance(acc, dict):
                    acc.pop("remix_userid", None)
                    acc.pop("remix_userkey", None)
                    acc.pop("session_origin", None)
        elif account_name:
            validate_account_name(account_name)
            if account_name in accounts and isinstance(accounts[account_name], dict):
                accounts[account_name].pop("remix_userid", None)
                accounts[account_name].pop("remix_userkey", None)
                accounts[account_name].pop("session_origin", None)
        else:
            default_acc = stored.get("default_account")
            if (
                default_acc
                and default_acc in accounts
                and isinstance(accounts[default_acc], dict)
            ):
                accounts[default_acc].pop("remix_userid", None)
                accounts[default_acc].pop("remix_userkey", None)
                accounts[default_acc].pop("session_origin", None)

    if config_path().exists():
        update_config(updater)

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


def cmd_profile(cfg: dict, args) -> None:
    account_arg = getattr(args, "account", None)
    if account_arg == "auto":
        raise CliError(
            "--account auto is only allowed for download",
            reason="invalid_arguments",
        )
    acc_name, acc_data = resolve_account(cfg, account_arg, require=True)
    acc_cfg = dict(cfg)
    acc_cfg["remix_userid"] = acc_data["remix_userid"]
    acc_cfg["remix_userkey"] = acc_data["remix_userkey"]
    acc_cfg["session_origin"] = acc_data["session_origin"]
    acc_cfg["_account_name"] = acc_name

    payload = fetch_profile(acc_cfg)
    redacted = redact_credentials(payload)
    user = redacted.get("user") if isinstance(redacted.get("user"), dict) else {}
    redacted["account"] = acc_name
    redacted["quota"] = parse_quota(user)
    emit(redacted)


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
    account_arg = getattr(args, "account", None)
    if account_arg == "auto":
        raise CliError(
            "--account auto is only allowed for download",
            reason="invalid_arguments",
        )
    acc_name, acc_data = resolve_account(
        cfg, account_arg, require=(account_arg is not None)
    )
    search_cfg = dict(cfg)
    if acc_data:
        search_cfg["remix_userid"] = acc_data["remix_userid"]
        search_cfg["remix_userkey"] = acc_data["remix_userkey"]
        search_cfg["session_origin"] = acc_data["session_origin"]
        search_cfg["_account_name"] = acc_name
    else:
        search_cfg["remix_userid"] = ""
        search_cfg["remix_userkey"] = ""

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
        search_cfg,
        "POST",
        "/eapi/book/search",
        data=data,
        authenticated=bool(auth(search_cfg)[0]),
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


def cmd_accounts(cfg: dict, args) -> None:
    sub = getattr(args, "accounts_command", None)
    if sub == "list":
        refresh = getattr(args, "refresh", False)
        stored = load_config()
        default_acc = stored.get("default_account")
        accounts_dict = stored.get("accounts", {})
        result_accounts = []
        for name in sorted(accounts_dict.keys()):
            acc = accounts_dict[name]
            session_present = bool(
                isinstance(acc, dict)
                and acc.get("remix_userid")
                and acc.get("remix_userkey")
            )
            quota_info = None
            if refresh and session_present:
                acc_cfg = dict(cfg)
                acc_cfg["remix_userid"] = acc["remix_userid"]
                acc_cfg["remix_userkey"] = acc["remix_userkey"]
                acc_cfg["session_origin"] = acc.get("session_origin", "")
                acc_cfg["_account_name"] = name
                try:
                    profile_payload = fetch_profile(acc_cfg)
                    user = (
                        profile_payload.get("user")
                        if isinstance(profile_payload.get("user"), dict)
                        else {}
                    )
                    quota_info = parse_quota(user)
                except CliError as exc:
                    quota_info = {
                        "status": exc.reason,
                        "limit": None,
                        "used": None,
                        "remaining": None,
                        "checked_at": time.strftime(
                            "%Y-%m-%dT%H:%M:%SZ", time.gmtime()
                        ),
                    }
                    if exc.reason == "rate_limited":
                        result_accounts.append(
                            {
                                "name": name,
                                "is_default": (name == default_acc),
                                "session_present": session_present,
                                "quota": quota_info,
                            }
                        )
                        break
            result_accounts.append(
                {
                    "name": name,
                    "is_default": (name == default_acc),
                    "session_present": session_present,
                    "quota": quota_info,
                }
            )
        emit(
            {
                "ok": True,
                "default_account": default_acc,
                "accounts": result_accounts,
            }
        )
    elif sub == "use":
        target = args.name
        validate_account_name(target)

        def updater(stored: dict) -> None:
            if target not in stored.get("accounts", {}):
                raise CliError(
                    f"account '{target}' not found", reason="account_not_found"
                )
            stored["default_account"] = target

        updated = update_config(updater)
        emit(
            {
                "ok": True,
                "default_account": updated.get("default_account"),
            }
        )
    elif sub == "remove":
        target = args.name
        validate_account_name(target)

        def updater(stored: dict) -> None:
            accounts = stored.get("accounts", {})
            if target not in accounts:
                raise CliError(
                    f"account '{target}' not found", reason="account_not_found"
                )
            accounts.pop(target, None)
            if stored.get("default_account") == target:
                stored["default_account"] = None

        updated = update_config(updater)
        emit(
            {
                "ok": True,
                "removed": target,
                "default_account": updated.get("default_account"),
            }
        )


def cmd_domains(cfg: dict, args) -> None:
    sub = getattr(args, "domains_command", None)
    if sub == "list":
        stored = load_config()
        active = normalize_origin(
            cfg.get("base_url", stored.get("base_url", DEFAULT_BASE_URL))
        )
        domains_dict = stored.get("domains", {})
        result_domains = []
        for name in sorted(domains_dict.keys()):
            d = domains_dict[name]
            if isinstance(d, dict):
                norm_u = normalize_origin(d.get("url", ""))
                result_domains.append(
                    {
                        "name": name,
                        "url": norm_u,
                        "trust": d.get("trust", "unverified"),
                        "source": d.get("source"),
                        "verified_at": d.get("verified_at"),
                        "is_active": (norm_u == active),
                    }
                )
        emit(
            {
                "ok": True,
                "active_origin": active,
                "domains": result_domains,
            }
        )
    elif sub == "check":
        url = getattr(args, "url", None)
        if not url:
            raise CliError("missing --url", reason="invalid_arguments")
        result = probe_domain(cfg, url)
        emit(result)
    elif sub == "add":
        name = args.name
        validate_account_name(name)
        norm_url = validate_onion_origin(args.url)
        target_host = urlparse(norm_url).hostname or ""
        confirm_host = (args.confirm_host or "").strip().lower()
        if confirm_host != target_host:
            raise CliError(
                "confirm-host does not match url hostname",
                reason="confirmation_mismatch",
            )
        verified_from = (args.verified_from or "").strip()
        if not verified_from:
            raise CliError(
                "verified-from source URL required", reason="source_required"
            )
        parsed_src = urlparse(verified_from)
        if (
            parsed_src.username is not None
            or parsed_src.password is not None
            or "@" in parsed_src.netloc
        ):
            raise CliError(
                "source URL must not contain credentials", reason="invalid_source"
            )
        if parsed_src.scheme not in {"http", "https"}:
            raise CliError(
                "source URL must use http:// or https://", reason="invalid_source"
            )

        now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

        def updater(stored: dict) -> None:
            domains = stored.setdefault("domains", {})
            if name in domains:
                raise CliError(
                    f"domain alias '{name}' already exists", reason="domain_exists"
                )
            for existing_name, d in domains.items():
                if (
                    isinstance(d, dict)
                    and normalize_origin(d.get("url", "")) == norm_url
                ):
                    raise CliError(
                        f"origin already registered under alias '{existing_name}'",
                        reason="domain_exists",
                    )
            domains[name] = {
                "url": norm_url,
                "trust": "user_verified",
                "source": verified_from,
                "verified_at": now_iso,
            }

        update_config(updater)
        emit(
            {
                "ok": True,
                "name": name,
                "url": norm_url,
                "trust": "user_verified",
                "config": str(config_path()),
            }
        )
    elif sub == "use":
        name = args.name
        validate_account_name(name)

        def updater(stored: dict) -> None:
            domains = stored.get("domains", {})
            if name not in domains:
                raise CliError(f"domain '{name}' not found", reason="domain_not_found")
            stored["base_url"] = domains[name]["url"]

        updated = update_config(updater)
        emit(
            {
                "ok": True,
                "name": name,
                "base_url": updated["base_url"],
            }
        )
    elif sub == "remove":
        name = args.name
        validate_account_name(name)

        def updater(stored: dict) -> None:
            domains = stored.get("domains", {})
            if name not in domains:
                raise CliError(f"domain '{name}' not found", reason="domain_not_found")
            active_url = normalize_origin(stored.get("base_url", DEFAULT_BASE_URL))
            target_url = normalize_origin(domains[name].get("url", ""))
            if target_url == active_url:
                raise CliError(
                    "cannot remove active domain in use", reason="domain_in_use"
                )
            domains.pop(name, None)

        update_config(updater)
        emit(
            {
                "ok": True,
                "removed": name,
            }
        )


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
        if same_origin:
            require_trusted_origin(cfg)
            headers, cookies = auth(cfg)
        else:
            headers, cookies = ({}, {})
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


def cache_key(*args, **kwargs) -> str:
    if len(args) == 2 and isinstance(args[0], str) and isinstance(args[1], str):
        book_id, book_hash = args[0], args[1]
    elif len(args) >= 2 and hasattr(args[1], "id") and hasattr(args[1], "hash"):
        book_id, book_hash = str(args[1].id), str(args[1].hash)
    elif len(args) >= 1 and hasattr(args[0], "id") and hasattr(args[0], "hash"):
        book_id, book_hash = str(args[0].id), str(args[0].hash)
    else:
        book_id = str(kwargs.get("id") or "")
        book_hash = str(kwargs.get("hash") or "")
    return hashlib.sha256(f"zlibrary\n{book_id}\n{book_hash}".encode()).hexdigest()


def legacy_cache_keys(cfg: dict, book_id: str, book_hash: str) -> list[str]:
    trusted_origins: list[str] = []
    domains = cfg.get("domains")
    if isinstance(domains, dict):
        for d in domains.values():
            if isinstance(d, dict) and d.get("trust") in {"builtin", "user_verified"}:
                u = d.get("url")
                if u:
                    trusted_origins.append(u)
                    trusted_origins.append(normalize_origin(u))
    else:
        trusted_origins.append(DEFAULT_BASE_URL)
        trusted_origins.append(normalize_origin(DEFAULT_BASE_URL))

    seen = set()
    keys = []
    for orig in trusted_origins:
        k = hashlib.sha256(f"{orig}\n{book_id}\n{book_hash}".encode()).hexdigest()
        if k not in seen:
            seen.add(k)
            keys.append(k)
    return keys


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


def find_cached_download(
    cfg: dict, args, output_dir: Path, max_bytes: int
) -> Path | None:
    new_k = cache_key(args.id, args.hash)
    new_manifest = output_dir / f".zlib-{new_k}.json"
    cached = cached_file(new_manifest, output_dir, max_bytes)
    if cached is not None:
        return cached

    for old_k in legacy_cache_keys(cfg, args.id, args.hash):
        old_manifest = output_dir / f".zlib-{old_k}.json"
        if old_manifest.is_file():
            cached = cached_file(old_manifest, output_dir, max_bytes)
            if cached is not None:
                try:
                    record = json.loads(old_manifest.read_text(encoding="utf-8"))
                    fd, temp_name = tempfile.mkstemp(
                        dir=output_dir, prefix=".zlib-", suffix=".tmp"
                    )
                    with os.fdopen(fd, "w", encoding="utf-8") as handle:
                        json.dump(record, handle)
                    Path(temp_name).replace(new_manifest)
                    return cached
                except Exception as exc:
                    raise CliError(
                        "cache migration failed", reason="cache_migration_failed"
                    ) from exc
    return None


def cmd_download(cfg: dict, args) -> None:
    account_arg = getattr(args, "account", None)
    if account_arg == "auto" and args.force:
        raise CliError(
            "--account auto cannot be combined with --force",
            reason="invalid_arguments",
        )

    output_dir = Path(args.output_dir).expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    key = cache_key(args.id, args.hash)
    book_lock = output_dir / f".zlib-{key}.lock"

    with exclusive_lock(
        book_lock,
        reason="download_locked",
        message="download already running; remove stale lock only after checking no process is active",
    ):
        # 1. Local cache check
        max_bytes = args.max_mb * 1024 * 1024
        existing = find_cached_download(cfg, args, output_dir, max_bytes)
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

        # 2. Download coordination lock
        coord_lock = config_path().parent / (config_path().name + ".download.lock")
        with exclusive_lock(
            coord_lock,
            reason="download_locked",
            message="download coordination lock held; another download is active",
        ):
            # Re-read fresh config under lock
            fresh_cfg = load_config()
            active_cfg = dict(cfg)
            active_cfg["accounts"] = fresh_cfg.get("accounts", {})
            active_cfg["default_account"] = fresh_cfg.get("default_account")
            active_cfg["domains"] = fresh_cfg.get("domains", {})

            if account_arg == "auto":
                acc_name, acc_data = select_download_account(active_cfg)
                selection_mode = "quota"
            else:
                acc_name, acc_data = resolve_account(
                    active_cfg, account_arg, require=True
                )
                selection_mode = "explicit" if account_arg else "default"

            download_cfg = dict(active_cfg)
            download_cfg["remix_userid"] = acc_data["remix_userid"]
            download_cfg["remix_userkey"] = acc_data["remix_userkey"]
            download_cfg["session_origin"] = acc_data["session_origin"]
            download_cfg["_account_name"] = acc_name

            download_locked(
                download_cfg,
                args,
                output_dir,
                account_name=acc_name,
                selection=selection_mode,
            )


def download_locked(
    cfg: dict,
    args,
    output_dir: Path,
    *,
    account_name: str | None = None,
    selection: str | None = None,
) -> None:
    key = cache_key(args.id, args.hash)
    manifest = output_dir / f".zlib-{key}.json"
    max_bytes = args.max_mb * 1024 * 1024
    existing = find_cached_download(cfg, args, output_dir, max_bytes)
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
    require_trusted_origin(cfg)
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
    res = {
        "ok": True,
        "status": "downloaded",
        "path": str(output),
        "bytes": size,
    }
    if account_name:
        res["account"] = account_name
    if selection == "quota":
        res["selection"] = "quota"
    emit(res)


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

    doctor = commands.add_parser("doctor")
    doctor.add_argument("--account")

    login = commands.add_parser("login")
    login.add_argument("--email", required=True)
    login.add_argument("--stdin-password", action="store_true")
    login.add_argument("--account")

    imp = commands.add_parser("import-session")
    imp.add_argument("--account")

    logout = commands.add_parser("logout")
    logout.add_argument("--account")
    logout.add_argument("--all", action="store_true")

    register = commands.add_parser("register")
    register.add_argument("--email", required=True)
    register.add_argument("--name", required=True)
    register.add_argument("--yes", action="store_true")
    register.add_argument("--account")

    profile = commands.add_parser("profile")
    profile.add_argument("--account")

    search = commands.add_parser("search")
    search.add_argument("query")
    search.add_argument("--limit", type=positive, choices=range(1, 21), default=5)
    search.add_argument("--page", type=positive, default=1)
    search.add_argument("--language", default="")
    search.add_argument("--extension", default="")
    search.add_argument("--account")

    download = commands.add_parser("download")
    download.add_argument("--id", type=identifier, required=True)
    download.add_argument("--hash", type=identifier, required=True)
    download.add_argument("--output-dir", default="downloads")
    download.add_argument("--max-mb", type=positive, choices=range(1, 513), default=80)
    download.add_argument("--force", action="store_true")
    download.add_argument("--account")

    accounts = commands.add_parser("accounts")
    acc_sub = accounts.add_subparsers(dest="accounts_command", required=True)
    acc_list = acc_sub.add_parser("list")
    acc_list.add_argument("--refresh", action="store_true")
    acc_use = acc_sub.add_parser("use")
    acc_use.add_argument("name")
    acc_remove = acc_sub.add_parser("remove")
    acc_remove.add_argument("name")

    domains = commands.add_parser("domains")
    dom_sub = domains.add_subparsers(dest="domains_command", required=True)
    dom_sub.add_parser("list")
    dom_check = dom_sub.add_parser("check")
    dom_check.add_argument("--url", required=True)
    dom_add = dom_sub.add_parser("add")
    dom_add.add_argument("name")
    dom_add.add_argument("--url", required=True)
    dom_add.add_argument("--verified-from", required=True)
    dom_add.add_argument("--confirm-host", required=True)
    dom_use = dom_sub.add_parser("use")
    dom_use.add_argument("name")
    dom_remove = dom_sub.add_parser("remove")
    dom_remove.add_argument("name")

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
    "accounts": cmd_accounts,
    "domains": cmd_domains,
}


def main() -> int:
    try:
        args = parser().parse_args()
        if args.command == "logout":
            COMMANDS[args.command]({}, args)
        elif (
            args.command == "accounts"
            and getattr(args, "accounts_command", "") in {"use", "remove", "list"}
            and not getattr(args, "refresh", False)
        ):
            COMMANDS[args.command](load_config(), args)
        elif args.command == "domains" and getattr(args, "domains_command", "") in {
            "use",
            "remove",
            "list",
            "add",
        }:
            COMMANDS[args.command](load_config(), args)
        else:
            COMMANDS[args.command](settings(), args)
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
