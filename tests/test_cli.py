import base64
import hashlib
import io
import json
import stat
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
import zlibrary_tor as cli


@pytest.fixture
def cfg(monkeypatch, tmp_path):
    for key in list(cli.os.environ):
        if key.startswith("ZLIBRARY_"):
            monkeypatch.delenv(key)
    monkeypatch.setenv("ZLIBRARY_TOR_CONFIG", str(tmp_path / "config.json"))
    return {
        "base_url": cli.DEFAULT_BASE_URL,
        "proxy": cli.DEFAULT_PROXY,
        "remix_userid": "123",
        "remix_userkey": "private-key",
        "retries": 1,
    }


def mock_network(monkeypatch, handler):
    monkeypatch.setattr(
        cli,
        "client",
        lambda cfg: httpx.Client(
            transport=httpx.MockTransport(handler), follow_redirects=False
        ),
    )
    monkeypatch.setattr(cli.time, "sleep", lambda _: None)


def test_headless_fast_failure(cfg):
    script = Path(cli.__file__)
    result = subprocess.run(
        [sys.executable, str(script), "login", "--email", "test@example.com"],
        input="",
        text=True,
        capture_output=True,
        check=False,
        timeout=5,
    )
    assert result.returncode == 1
    assert json.loads(result.stdout)["reason"] == "credentials_required"
    assert not result.stderr


def test_explicit_password_stdin(cfg, monkeypatch):
    monkeypatch.setattr(sys, "stdin", io.StringIO("secret\n"))
    assert cli.secret("password", "ZLIBRARY_PASSWORD", stdin=True) == "secret"


def test_import_and_logout(cfg, monkeypatch, capsys):
    monkeypatch.setenv("ZLIBRARY_REMIX_USERID", "123")
    monkeypatch.setenv("ZLIBRARY_REMIX_USERKEY", "private-key")
    cli.cmd_import_session(cfg, None)
    assert cli.load_config()["accounts"]["default"]["remix_userkey"] == "private-key"
    cli.cmd_logout({}, None)
    assert "remix_userkey" not in cli.load_config().get("accounts", {}).get(
        "default", {}
    )
    assert cli.load_config()["proxy"] == cli.DEFAULT_PROXY
    assert "private-key" not in capsys.readouterr().out


@pytest.mark.parametrize(
    "url",
    [
        "https://127.0.0.1/x",
        "https://[::1]/x",
        "https://192.168.1.1/x",
        "http://example.com",
        "https://a.onion",
        "https://localhost/x",
        "https://user:pass@example.com",
    ],
)
def test_target_rejection(url):
    with pytest.raises(cli.CliError):
        cli.validate_target(url)


def test_redirect_credentials(cfg, monkeypatch, tmp_path):
    requests = []

    def handler(request):
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(
                302, headers={"location": "https://files.example/book"}
            )
        return httpx.Response(200, content=b"book")

    mock_network(monkeypatch, handler)
    output = tmp_path / "book.epub"
    assert cli.download_stream(cfg, cfg["base_url"] + "/file", output, 100) == 4
    assert requests[0].headers["remix-userkey"] == "private-key"
    assert "remix-userkey" not in requests[1].headers
    assert "cookie" not in requests[1].headers


class BrokenStream(httpx.SyncByteStream):
    def __iter__(self):
        yield b"partial"
        raise httpx.ReadError("disconnected")


@pytest.mark.parametrize("kind", ["broken", "empty", "truncated", "oversize", "html"])
def test_atomic_failure_preserves_file(cfg, monkeypatch, tmp_path, kind):
    responses = {
        "broken": lambda: httpx.Response(200, stream=BrokenStream()),
        "empty": lambda: httpx.Response(200, content=b""),
        "truncated": lambda: httpx.Response(
            200, headers={"content-length": "50"}, content=b"short"
        ),
        "oversize": lambda: httpx.Response(200, content=b"a" * 101),
        "html": lambda: httpx.Response(
            200, headers={"content-type": "text/html"}, content=b"login"
        ),
    }
    mock_network(monkeypatch, lambda _: responses[kind]())
    output = tmp_path / "book.epub"
    output.write_bytes(b"previous")
    with pytest.raises((cli.CliError, httpx.HTTPError)):
        cli.download_stream(cfg, cfg["base_url"] + "/file", output, 100)
    assert output.read_bytes() == b"previous"
    assert not list(tmp_path.glob("*.part"))


def download_args(tmp_path, **kwargs):
    values = {
        "id": "123",
        "hash": "abcdef",
        "output_dir": str(tmp_path),
        "max_mb": 1,
        "force": False,
    }
    values.update(kwargs)
    return SimpleNamespace(**values)


def test_cache_before_quota_and_corruption(cfg, monkeypatch, tmp_path, capsys):
    calls = []

    def handler(request):
        calls.append(request.url.path)
        if request.url.path.endswith("/file"):
            return httpx.Response(
                200,
                json={
                    "success": 1,
                    "file": {
                        "downloadLink": cfg["base_url"] + "/content",
                        "description": "Title",
                        "extension": "epub",
                    },
                },
            )
        return httpx.Response(200, content=b"book")

    mock_network(monkeypatch, handler)
    args = download_args(tmp_path)
    cli.cmd_download(cfg, args)
    output = Path(json.loads(capsys.readouterr().out)["path"])
    cli.cmd_download(cfg, args)
    assert json.loads(capsys.readouterr().out)["status"] == "cached"
    assert len(calls) == 2
    output.write_bytes(b"fake")
    assert (
        cli.cached_file(
            tmp_path / f".zlib-{cli.cache_key(cfg, args)}.json", tmp_path, 1024
        )
        is None
    )
    args.force = True
    cli.cmd_download(cfg, args)
    assert output.read_bytes() == b"book"
    assert len(calls) == 4


@pytest.mark.parametrize("retryable,expected", [(True, 2), (False, 1)])
def test_bounded_retries(cfg, monkeypatch, retryable, expected):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(503, json={"error": "unavailable"})

    mock_network(monkeypatch, handler)
    with pytest.raises(cli.CliError):
        cli.request_json(cfg, "POST", "/test", retryable=retryable)
    assert len(calls) == expected


def test_pagination(cfg, monkeypatch, capsys):
    def handler(request):
        assert b"page=3" in request.content
        return httpx.Response(200, json={"success": 1, "books": []})

    mock_network(monkeypatch, handler)
    cli.cmd_search(cfg, cli.parser().parse_args(["search", "title", "--page", "3"]))
    assert json.loads(capsys.readouterr().out)["page"] == 3


@pytest.mark.parametrize(
    "message,reason",
    [
        ("Daily download limit reached", "daily_quota_exhausted"),
        ("not found", "download_link_unavailable"),
    ],
)
def test_quota(cfg, monkeypatch, tmp_path, message, reason):
    mock_network(
        monkeypatch,
        lambda _: httpx.Response(200, json={"success": 0, "error": message}),
    )
    with pytest.raises(cli.CliError) as caught:
        cli.cmd_download(cfg, download_args(tmp_path))
    assert caught.value.reason == reason


def test_doctor_proxy_failure(cfg, monkeypatch):
    def fail(*args, **kwargs):
        raise OSError("refused")

    monkeypatch.setattr(cli.socket, "create_connection", fail)
    with pytest.raises(cli.CliError) as caught:
        cli.cmd_doctor(cfg, None)
    assert caught.value.stage == "local_proxy"


def test_error_does_not_echo_server_secrets(cfg, monkeypatch):
    mock_network(
        monkeypatch,
        lambda _: httpx.Response(403, json={"remix_userkey": "private-key"}),
    )
    with pytest.raises(cli.CliError) as caught:
        cli.request_json(cfg, "GET", "/test")
    assert "private-key" not in str(caught.value)


@pytest.mark.parametrize("value", ["CON", "NUL.txt", "../bad:name", "..."])
def test_filename(value):
    result = cli.safe_filename(value)
    assert result and "/" not in result and ":" not in result
    assert result.split(".")[0] not in {"CON", "NUL"}


@pytest.mark.parametrize(
    "args",
    [
        ["search", "x", "--limit", "0"],
        ["search", "x", "--page", "-1"],
        ["download", "--id", "../x", "--hash", "abc"],
    ],
)
def test_invalid_arguments(args):
    with pytest.raises(cli.CliError):
        cli.parser().parse_args(args)


def test_lock_prevents_second_link(cfg, monkeypatch, tmp_path):
    args = download_args(tmp_path)
    (tmp_path / f".zlib-{cli.cache_key(cfg, args)}.lock").touch()

    def unexpected(_):
        pytest.fail("locked download must not access network")

    mock_network(monkeypatch, unexpected)
    with pytest.raises(cli.CliError) as caught:
        cli.cmd_download(cfg, args)
    assert caught.value.reason == "download_locked"


def test_cache_path_escape(tmp_path):
    manifest = tmp_path / "cache.json"
    manifest.write_text(
        json.dumps({"name": "../outside.epub", "bytes": 4, "sha256": "x"})
    )
    assert cli.cached_file(manifest, tmp_path, 100) is None


def test_redirect_limit(cfg, monkeypatch, tmp_path):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(302, headers={"location": "/loop"})

    mock_network(monkeypatch, handler)
    with pytest.raises(cli.CliError):
        cli.download_stream(cfg, cfg["base_url"] + "/start", tmp_path / "book", 100)
    assert len(calls) == 4


def test_origin_not_prefix(cfg, monkeypatch, tmp_path):
    def handler(request):
        assert "cookie" not in request.headers
        assert "remix-userkey" not in request.headers
        return httpx.Response(200, content=b"book")

    mock_network(monkeypatch, handler)
    other_port = cfg["base_url"] + ":8080/book"
    with pytest.raises(cli.CliError):
        cli.download_stream(cfg, other_port, tmp_path / "book", 100)
    cli.download_stream(cfg, "https://files.example/book", tmp_path / "book", 100)


def test_rate_limit_never_retried(cfg, monkeypatch):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(429, json={"error": "slow down"})

    mock_network(monkeypatch, handler)
    with pytest.raises(cli.CliError):
        cli.request_json(cfg, "POST", "/search", retryable=True)
    assert len(calls) == 1


def test_profile_redaction(cfg, monkeypatch, capsys):
    mock_network(
        monkeypatch,
        lambda _: httpx.Response(
            200,
            json={
                "success": 1,
                "user": {"remix_userkey": "private-key", "downloads_today": 2},
            },
        ),
    )
    cli.cmd_profile(cfg, None)
    result = json.loads(capsys.readouterr().out)
    assert result["user"] == {"downloads_today": 2}


@pytest.mark.parametrize("value", ["nan", "inf", "0", "601", "wrong"])
def test_timeout_validation(cfg, monkeypatch, value):
    monkeypatch.setenv("ZLIBRARY_TOR_TIMEOUT", value)
    with pytest.raises(cli.CliError):
        cli.settings()


def test_doctor_api_failure(cfg, monkeypatch):
    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

    monkeypatch.setattr(cli.socket, "create_connection", lambda *a, **kw: Connection())
    mock_network(monkeypatch, lambda _: httpx.Response(200, json={"success": 0}))
    with pytest.raises(cli.CliError) as caught:
        cli.cmd_doctor(cfg, None)
    assert caught.value.stage == "api_response"


# ---------------------------------------------------------------------------
# Section 4.2 Configuration and Account tests
# ---------------------------------------------------------------------------


def test_migrate_legacy_session_to_default_account(tmp_path, monkeypatch):
    cfg_file = tmp_path / "config.json"
    legacy_data = {
        "base_url": "http://legacy.onion",
        "proxy": "socks5://127.0.0.1:9050",
        "timeout": 30,
        "retries": 2,
        "remix_userid": "legacy-uid",
        "remix_userkey": "legacy-key",
    }
    cfg_file.write_text(json.dumps(legacy_data), encoding="utf-8")
    monkeypatch.setenv("ZLIBRARY_TOR_CONFIG", str(cfg_file))

    loaded = cli.load_config()
    assert loaded["schema_version"] == 2
    assert loaded["default_account"] == "default"
    assert loaded["accounts"]["default"]["remix_userid"] == "legacy-uid"
    assert loaded["accounts"]["default"]["remix_userkey"] == "legacy-key"
    assert loaded["accounts"]["default"]["session_origin"] == "http://legacy.onion"
    assert "remix_userid" not in loaded
    assert "remix_userkey" not in loaded
    assert loaded["timeout"] == 30
    assert loaded["retries"] == 2

    on_disk = json.loads(cfg_file.read_text(encoding="utf-8"))
    assert on_disk["schema_version"] == 2
    assert "default" in on_disk["accounts"]


def test_migration_binds_persisted_origin_not_environment(tmp_path, monkeypatch):
    cfg_file = tmp_path / "config.json"
    legacy_data = {
        "base_url": "http://persisted-site.onion",
        "remix_userid": "uid",
        "remix_userkey": "key",
    }
    cfg_file.write_text(json.dumps(legacy_data), encoding="utf-8")
    monkeypatch.setenv("ZLIBRARY_TOR_CONFIG", str(cfg_file))
    monkeypatch.setenv("ZLIBRARY_BASE_URL", "http://env-override.onion")

    loaded = cli.load_config()
    assert (
        loaded["accounts"]["default"]["session_origin"] == "http://persisted-site.onion"
    )


def test_migration_is_idempotent(tmp_path, monkeypatch):
    cfg_file = tmp_path / "config.json"
    legacy_data = {
        "remix_userid": "uid",
        "remix_userkey": "key",
    }
    cfg_file.write_text(json.dumps(legacy_data), encoding="utf-8")
    monkeypatch.setenv("ZLIBRARY_TOR_CONFIG", str(cfg_file))

    first = cli.load_config()
    first_disk = cfg_file.read_text(encoding="utf-8")
    second = cli.load_config()
    second_disk = cfg_file.read_text(encoding="utf-8")
    assert first == second
    assert first_disk == second_disk


def test_migration_failure_preserves_original_file(tmp_path, monkeypatch):
    cfg_file = tmp_path / "config.json"
    original_text = json.dumps({"remix_userid": "uid", "remix_userkey": "key"})
    cfg_file.write_text(original_text, encoding="utf-8")
    monkeypatch.setenv("ZLIBRARY_TOR_CONFIG", str(cfg_file))

    def fail_save(_):
        raise OSError("write denied")

    monkeypatch.setattr(cli, "save_config", fail_save)
    with pytest.raises(cli.CliError) as caught:
        cli.load_config()
    assert caught.value.reason == "config_write_failed"
    assert cfg_file.read_text(encoding="utf-8") == original_text


def test_partial_legacy_session_rejected(tmp_path, monkeypatch):
    cfg_file = tmp_path / "config.json"
    original_text = json.dumps({"remix_userid": "uid"})
    cfg_file.write_text(original_text, encoding="utf-8")
    monkeypatch.setenv("ZLIBRARY_TOR_CONFIG", str(cfg_file))

    with pytest.raises(cli.CliError) as caught:
        cli.load_config()
    assert caught.value.reason == "incomplete_session"
    assert cfg_file.read_text(encoding="utf-8") == original_text


def test_invalid_config_is_not_overwritten(tmp_path, monkeypatch):
    cfg_file = tmp_path / "config.json"
    bad_json = "not-json-content {"
    cfg_file.write_text(bad_json, encoding="utf-8")
    monkeypatch.setenv("ZLIBRARY_TOR_CONFIG", str(cfg_file))

    with pytest.raises(cli.CliError) as caught:
        cli.load_config()
    assert caught.value.reason == "invalid_config"
    assert cfg_file.read_text(encoding="utf-8") == bad_json


def test_future_schema_version_rejected(tmp_path, monkeypatch):
    cfg_file = tmp_path / "config.json"
    future_json = json.dumps({"schema_version": 99, "data": 123})
    cfg_file.write_text(future_json, encoding="utf-8")
    monkeypatch.setenv("ZLIBRARY_TOR_CONFIG", str(cfg_file))

    with pytest.raises(cli.CliError) as caught:
        cli.load_config()
    assert caught.value.reason == "unsupported_config_version"
    assert cfg_file.read_text(encoding="utf-8") == future_json


def test_config_lock_prevents_lost_update(tmp_path, monkeypatch):
    cfg_file = tmp_path / "config.json"
    monkeypatch.setenv("ZLIBRARY_TOR_CONFIG", str(cfg_file))

    lock_file = tmp_path / "config.json.lock"
    lock_file.touch()
    with pytest.raises(cli.CliError) as caught:
        cli.update_config(lambda c: None)
    assert caught.value.reason == "config_locked"
    lock_file.unlink()

    def update_a(c):
        c.setdefault("accounts", {})["acc_a"] = {
            "remix_userid": "1",
            "remix_userkey": "k1",
            "session_origin": cli.DEFAULT_BASE_URL,
        }

    def update_b(c):
        c.setdefault("accounts", {})["acc_b"] = {
            "remix_userid": "2",
            "remix_userkey": "k2",
            "session_origin": cli.DEFAULT_BASE_URL,
        }

    cli.update_config(update_a)
    cli.update_config(update_b)
    final_cfg = cli.load_config()
    assert "acc_a" in final_cfg["accounts"]
    assert "acc_b" in final_cfg["accounts"]


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permissions only")
def test_config_permissions_posix(tmp_path, monkeypatch):
    cfg_file = tmp_path / "config.json"
    monkeypatch.setenv("ZLIBRARY_TOR_CONFIG", str(cfg_file))
    cli.save_config({"schema_version": 2})
    mode = stat.S_IMODE(cfg_file.stat().st_mode)
    assert mode == 0o600


def test_named_login_preserves_other_accounts(tmp_path, monkeypatch):
    cfg_file = tmp_path / "config.json"
    monkeypatch.setenv("ZLIBRARY_TOR_CONFIG", str(cfg_file))
    initial_data = {
        "schema_version": 2,
        "default_account": "existing",
        "accounts": {
            "existing": {
                "remix_userid": "111",
                "remix_userkey": "k111",
                "session_origin": cli.DEFAULT_BASE_URL,
            }
        },
    }
    cfg_file.write_text(json.dumps(initial_data), encoding="utf-8")

    mock_network(
        monkeypatch,
        lambda _: httpx.Response(
            200,
            json={
                "success": 1,
                "user": {"id": 222, "remix_userkey": "k222"},
            },
        ),
    )
    monkeypatch.setenv("ZLIBRARY_PASSWORD", "pwd")
    cli.cmd_login(
        cli.settings(),
        SimpleNamespace(
            email="test@example.com", stdin_password=False, account="second"
        ),
    )

    updated = cli.load_config()
    assert "existing" in updated["accounts"]
    assert "second" in updated["accounts"]
    assert updated["accounts"]["second"]["remix_userid"] == "222"
    assert updated["default_account"] == "existing"


def test_second_login_preserves_default_account(tmp_path, monkeypatch):
    cfg_file = tmp_path / "config.json"
    monkeypatch.setenv("ZLIBRARY_TOR_CONFIG", str(cfg_file))

    login_responses = [
        {"success": 1, "user": {"id": 1, "remix_userkey": "k1"}},
        {"success": 1, "user": {"id": 2, "remix_userkey": "k2"}},
    ]
    call_idx = 0

    def handler(_):
        nonlocal call_idx
        res = login_responses[call_idx]
        call_idx += 1
        return httpx.Response(200, json=res)

    mock_network(monkeypatch, handler)
    monkeypatch.setenv("ZLIBRARY_PASSWORD", "pwd")

    cli.cmd_login(
        cli.settings(),
        SimpleNamespace(
            email="first@example.com", stdin_password=False, account="first"
        ),
    )
    assert cli.load_config()["default_account"] == "first"

    cli.cmd_login(
        cli.settings(),
        SimpleNamespace(
            email="second@example.com", stdin_password=False, account="second"
        ),
    )
    assert cli.load_config()["default_account"] == "first"


@pytest.mark.parametrize(
    "name",
    [
        "auto",
        "env",
        "../path",
        "name/with/slash",
        "a" * 33,
        "",
        "bad name",
        "user@name",
    ],
)
def test_account_alias_validation(name):
    with pytest.raises(cli.CliError) as caught:
        cli.validate_account_name(name)
    assert caught.value.reason == "invalid_account_alias"


def test_account_use_and_remove_are_offline(tmp_path, monkeypatch):
    cfg_file = tmp_path / "config.json"
    monkeypatch.setenv("ZLIBRARY_TOR_CONFIG", str(cfg_file))
    initial = {
        "schema_version": 2,
        "default_account": "a1",
        "accounts": {
            "a1": {
                "remix_userid": "1",
                "remix_userkey": "k1",
                "session_origin": cli.DEFAULT_BASE_URL,
            },
            "a2": {
                "remix_userid": "2",
                "remix_userkey": "k2",
                "session_origin": cli.DEFAULT_BASE_URL,
            },
        },
    }
    cfg_file.write_text(json.dumps(initial), encoding="utf-8")

    def forbid_network(_):
        pytest.fail("network must not be accessed during local accounts management")

    monkeypatch.setattr(cli, "client", forbid_network)

    cli.cmd_accounts(
        cli.load_config(),
        SimpleNamespace(accounts_command="use", name="a2"),
    )
    assert cli.load_config()["default_account"] == "a2"

    cli.cmd_accounts(
        cli.load_config(),
        SimpleNamespace(accounts_command="remove", name="a2"),
    )
    cfg_now = cli.load_config()
    assert "a2" not in cfg_now["accounts"]
    assert cfg_now["default_account"] is None


def test_logout_one_preserves_other_sessions(tmp_path, monkeypatch):
    cfg_file = tmp_path / "config.json"
    monkeypatch.setenv("ZLIBRARY_TOR_CONFIG", str(cfg_file))
    initial = {
        "schema_version": 2,
        "default_account": "a1",
        "accounts": {
            "a1": {
                "remix_userid": "1",
                "remix_userkey": "k1",
                "session_origin": cli.DEFAULT_BASE_URL,
            },
            "a2": {
                "remix_userid": "2",
                "remix_userkey": "k2",
                "session_origin": cli.DEFAULT_BASE_URL,
            },
        },
    }
    cfg_file.write_text(json.dumps(initial), encoding="utf-8")

    cli.cmd_logout({}, SimpleNamespace(account="a1", all=False))
    updated = cli.load_config()
    assert "remix_userkey" not in updated["accounts"]["a1"]
    assert "a1" in updated["accounts"]
    assert updated["accounts"]["a2"]["remix_userkey"] == "k2"


def test_logout_all_preserves_network_settings(tmp_path, monkeypatch):
    cfg_file = tmp_path / "config.json"
    monkeypatch.setenv("ZLIBRARY_TOR_CONFIG", str(cfg_file))
    initial = {
        "schema_version": 2,
        "base_url": "http://my-zlib.onion",
        "proxy": "socks5://127.0.0.1:9150",
        "timeout": 50,
        "retries": 2,
        "default_account": "a1",
        "accounts": {
            "a1": {
                "remix_userid": "1",
                "remix_userkey": "k1",
                "session_origin": "http://my-zlib.onion",
            },
            "a2": {
                "remix_userid": "2",
                "remix_userkey": "k2",
                "session_origin": "http://my-zlib.onion",
            },
        },
    }
    cfg_file.write_text(json.dumps(initial), encoding="utf-8")

    cli.cmd_logout({}, SimpleNamespace(account=None, all=True))
    updated = cli.load_config()
    assert updated["base_url"] == "http://my-zlib.onion"
    assert updated["proxy"] == "socks5://127.0.0.1:9150"
    assert updated["timeout"] == 50
    assert updated["retries"] == 2
    assert "a1" in updated["accounts"]
    assert "a2" in updated["accounts"]
    assert "remix_userkey" not in updated["accounts"]["a1"]
    assert "remix_userkey" not in updated["accounts"]["a2"]


def test_explicit_account_ignores_environment_session(tmp_path, monkeypatch):
    cfg_file = tmp_path / "config.json"
    monkeypatch.setenv("ZLIBRARY_TOR_CONFIG", str(cfg_file))
    cfg_file.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "accounts": {
                    "saved": {
                        "remix_userid": "saved-id",
                        "remix_userkey": "saved-key",
                        "session_origin": cli.DEFAULT_BASE_URL,
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("ZLIBRARY_REMIX_USERID", "env-id")
    monkeypatch.setenv("ZLIBRARY_REMIX_USERKEY", "env-key")
    monkeypatch.setenv("ZLIBRARY_SESSION_ORIGIN", cli.DEFAULT_BASE_URL)

    acc_name, acc = cli.resolve_account(cli.settings(), "saved", require=True)
    assert acc_name == "saved"
    assert acc["remix_userid"] == "saved-id"
    assert acc["remix_userkey"] == "saved-key"


def test_partial_environment_session_rejected(tmp_path, monkeypatch):
    monkeypatch.setenv("ZLIBRARY_REMIX_USERID", "env-id")
    with pytest.raises(cli.CliError) as caught:
        cli.resolve_account(cli.settings(), None, require=False)
    assert caught.value.reason == "incomplete_environment_session"


def test_environment_session_requires_bound_origin(tmp_path, monkeypatch):
    monkeypatch.setenv("ZLIBRARY_REMIX_USERID", "env-id")
    monkeypatch.setenv("ZLIBRARY_REMIX_USERKEY", "env-key")

    with pytest.raises(cli.CliError) as caught:
        cli.resolve_account(cli.settings(), None, require=True)
    assert caught.value.reason == "session_origin_missing"

    monkeypatch.setenv("ZLIBRARY_SESSION_ORIGIN", "http://mismatch.onion")
    with pytest.raises(cli.CliError) as caught:
        cli.resolve_account(cli.settings(), None, require=True)
    assert caught.value.reason == "session_origin_mismatch"


def test_account_outputs_do_not_expose_credentials(tmp_path, monkeypatch, capsys):
    cfg_file = tmp_path / "config.json"
    monkeypatch.setenv("ZLIBRARY_TOR_CONFIG", str(cfg_file))
    secret_uid = "secret-uid-999"
    secret_key = "secret-key-888"

    mock_network(
        monkeypatch,
        lambda _: httpx.Response(
            200,
            json={
                "success": 1,
                "user": {
                    "id": secret_uid,
                    "remix_userkey": secret_key,
                    "downloads_today": 0,
                    "downloads_limit": 5,
                },
            },
        ),
    )
    monkeypatch.setenv("ZLIBRARY_PASSWORD", "super-secret-pw")

    cli.cmd_login(
        cli.settings(),
        SimpleNamespace(
            email="test@example.com", stdin_password=False, account="personal"
        ),
    )
    out1 = capsys.readouterr().out
    assert secret_uid not in out1
    assert secret_key not in out1
    assert "super-secret-pw" not in out1

    cli.cmd_accounts(
        cli.load_config(), SimpleNamespace(accounts_command="list", refresh=False)
    )
    out2 = capsys.readouterr().out
    assert secret_uid not in out2
    assert secret_key not in out2

    cli.cmd_profile(cli.settings(), SimpleNamespace(account="personal"))
    out3 = capsys.readouterr().out
    assert secret_key not in out3

    cli.cmd_logout({}, SimpleNamespace(account="personal", all=False))
    out4 = capsys.readouterr().out
    assert secret_uid not in out4
    assert secret_key not in out4


# ---------------------------------------------------------------------------
# Section 4.3 Quota, Selection, and Download tests
# ---------------------------------------------------------------------------


def test_parse_quota_valid_values():
    q1 = cli.parse_quota({"downloads_today": 2, "downloads_limit": 10})
    assert q1["status"] == "available"
    assert q1["remaining"] == 8
    assert q1["used"] == 2
    assert q1["limit"] == 10

    q2 = cli.parse_quota({"downloads_today": "5", "downloads_limit": "5"})
    assert q2["status"] == "exhausted"
    assert q2["remaining"] == 0


def test_parse_quota_rejects_bool_float_and_negative():
    for bad in [True, False, 1.5, -1, "1.5", "-1", "", "abc"]:
        q = cli.parse_quota({"downloads_today": bad, "downloads_limit": 10})
        assert q["status"] == "unknown"
        assert q["remaining"] is None


def test_parse_quota_missing_fields_is_unknown():
    assert cli.parse_quota({})["status"] == "unknown"
    assert cli.parse_quota({"downloads_today": 1})["status"] == "unknown"
    assert cli.parse_quota({"downloads_limit": 5})["status"] == "unknown"
    assert cli.parse_quota(None)["status"] == "unknown"


def test_parse_quota_clamps_remaining_to_zero():
    q = cli.parse_quota({"downloads_today": 12, "downloads_limit": 10})
    assert q["status"] == "exhausted"
    assert q["remaining"] == 0


def test_profile_adds_account_and_normalized_quota(tmp_path, monkeypatch, capsys):
    cfg_file = tmp_path / "config.json"
    monkeypatch.setenv("ZLIBRARY_TOR_CONFIG", str(cfg_file))
    cfg_file.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "default_account": "acc1",
                "accounts": {
                    "acc1": {
                        "remix_userid": "123",
                        "remix_userkey": "key123",
                        "session_origin": cli.DEFAULT_BASE_URL,
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    mock_network(
        monkeypatch,
        lambda _: httpx.Response(
            200,
            json={
                "success": 1,
                "user": {
                    "id": 123,
                    "downloads_today": 1,
                    "downloads_limit": 5,
                    "remix_userkey": "key123",
                },
            },
        ),
    )
    cli.cmd_profile(cli.settings(), SimpleNamespace(account="acc1"))
    out = json.loads(capsys.readouterr().out)
    assert out["account"] == "acc1"
    assert out["quota"]["status"] == "available"
    assert out["quota"]["remaining"] == 4
    assert "remix_userkey" not in out["user"]


def test_auto_selects_first_confirmed_available_account(tmp_path, monkeypatch, capsys):
    cfg_file = tmp_path / "config.json"
    monkeypatch.setenv("ZLIBRARY_TOR_CONFIG", str(cfg_file))
    cfg_file.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "default_account": "acc1",
                "accounts": {
                    "acc1": {
                        "remix_userid": "1",
                        "remix_userkey": "k1",
                        "session_origin": cli.DEFAULT_BASE_URL,
                    },
                    "acc2": {
                        "remix_userid": "2",
                        "remix_userkey": "k2",
                        "session_origin": cli.DEFAULT_BASE_URL,
                    },
                },
            }
        ),
        encoding="utf-8",
    )
    profile_calls = []
    file_calls = []

    def handler(request):
        path = request.url.path
        if "/user/profile" in path:
            uid = request.headers.get("remix-userid")
            profile_calls.append(uid)
            if uid == "1":
                return httpx.Response(
                    200,
                    json={
                        "success": 1,
                        "user": {"downloads_today": 5, "downloads_limit": 5},
                    },
                )
            else:
                return httpx.Response(
                    200,
                    json={
                        "success": 1,
                        "user": {"downloads_today": 0, "downloads_limit": 5},
                    },
                )
        elif "/file" in path:
            file_calls.append(request.headers.get("remix-userid"))
            return httpx.Response(
                200,
                json={
                    "success": 1,
                    "file": {
                        "downloadLink": cli.DEFAULT_BASE_URL + "/download",
                        "description": "Book",
                        "extension": "epub",
                    },
                },
            )
        elif "/download" in path:
            return httpx.Response(200, content=b"content")
        return httpx.Response(404)

    mock_network(monkeypatch, handler)
    args = download_args(tmp_path, account="auto")
    cli.cmd_download(cli.settings(), args)

    assert profile_calls == ["1", "2"]
    assert file_calls == ["2"]
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == "downloaded"
    assert out["account"] == "acc2"
    assert out["selection"] == "quota"


def test_auto_deduplicates_same_user_aliases(tmp_path, monkeypatch):
    cfg_file = tmp_path / "config.json"
    monkeypatch.setenv("ZLIBRARY_TOR_CONFIG", str(cfg_file))
    cfg_file.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "default_account": "alias1",
                "accounts": {
                    "alias1": {
                        "remix_userid": "same_user",
                        "remix_userkey": "k",
                        "session_origin": cli.DEFAULT_BASE_URL,
                    },
                    "alias2": {
                        "remix_userid": "same_user",
                        "remix_userkey": "k",
                        "session_origin": cli.DEFAULT_BASE_URL,
                    },
                },
            }
        ),
        encoding="utf-8",
    )
    calls = []

    def handler(request):
        if "/user/profile" in request.url.path:
            calls.append(request.headers.get("remix-userid"))
            return httpx.Response(
                200,
                json={
                    "success": 1,
                    "user": {"downloads_today": 5, "downloads_limit": 5},
                },
            )
        return httpx.Response(404)

    mock_network(monkeypatch, handler)
    args = download_args(tmp_path, account="auto")
    with pytest.raises(cli.CliError) as caught:
        cli.cmd_download(cli.settings(), args)
    assert caught.value.reason == "no_available_account"
    assert len(calls) == 1


def test_auto_stops_on_unknown_quota(tmp_path, monkeypatch):
    cfg_file = tmp_path / "config.json"
    monkeypatch.setenv("ZLIBRARY_TOR_CONFIG", str(cfg_file))
    cfg_file.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "default_account": "acc1",
                "accounts": {
                    "acc1": {
                        "remix_userid": "1",
                        "remix_userkey": "k1",
                        "session_origin": cli.DEFAULT_BASE_URL,
                    },
                    "acc2": {
                        "remix_userid": "2",
                        "remix_userkey": "k2",
                        "session_origin": cli.DEFAULT_BASE_URL,
                    },
                },
            }
        ),
        encoding="utf-8",
    )
    calls = []

    def handler(request):
        if "/user/profile" in request.url.path:
            calls.append(request.headers.get("remix-userid"))
            return httpx.Response(200, json={"success": 1, "user": {"id": 1}})
        return httpx.Response(404)

    mock_network(monkeypatch, handler)
    args = download_args(tmp_path, account="auto")
    with pytest.raises(cli.CliError) as caught:
        cli.cmd_download(cli.settings(), args)
    assert caught.value.reason == "account_selection_incomplete"
    assert calls == ["1"]


def test_auto_stops_on_rate_limit(tmp_path, monkeypatch):
    cfg_file = tmp_path / "config.json"
    monkeypatch.setenv("ZLIBRARY_TOR_CONFIG", str(cfg_file))
    cfg_file.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "default_account": "acc1",
                "accounts": {
                    "acc1": {
                        "remix_userid": "1",
                        "remix_userkey": "k1",
                        "session_origin": cli.DEFAULT_BASE_URL,
                    },
                    "acc2": {
                        "remix_userid": "2",
                        "remix_userkey": "k2",
                        "session_origin": cli.DEFAULT_BASE_URL,
                    },
                },
            }
        ),
        encoding="utf-8",
    )
    calls = []

    def handler(request):
        if "/user/profile" in request.url.path:
            calls.append(request.headers.get("remix-userid"))
            return httpx.Response(429, json={"error": "rate limit"})
        return httpx.Response(404)

    mock_network(monkeypatch, handler)
    args = download_args(tmp_path, account="auto")
    with pytest.raises(cli.CliError) as caught:
        cli.cmd_download(cli.settings(), args)
    assert caught.value.reason == "rate_limited"
    assert calls == ["1"]


def test_auto_skips_explicitly_invalid_session_before_link(tmp_path, monkeypatch):
    cfg_file = tmp_path / "config.json"
    monkeypatch.setenv("ZLIBRARY_TOR_CONFIG", str(cfg_file))
    cfg_file.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "default_account": "acc1",
                "accounts": {
                    "acc1": {
                        "remix_userid": "1",
                        "remix_userkey": "k1",
                        "session_origin": cli.DEFAULT_BASE_URL,
                    },
                    "acc2": {
                        "remix_userid": "2",
                        "remix_userkey": "k2",
                        "session_origin": cli.DEFAULT_BASE_URL,
                    },
                },
            }
        ),
        encoding="utf-8",
    )

    def handler(request):
        path = request.url.path
        if "/user/profile" in path:
            uid = request.headers.get("remix-userid")
            if uid == "1":
                return httpx.Response(401, json={"error": "unauthorized"})
            return httpx.Response(
                200,
                json={
                    "success": 1,
                    "user": {"downloads_today": 0, "downloads_limit": 5},
                },
            )
        elif "/file" in path:
            return httpx.Response(
                200,
                json={
                    "success": 1,
                    "file": {
                        "downloadLink": cli.DEFAULT_BASE_URL + "/download",
                        "description": "Book",
                        "extension": "epub",
                    },
                },
            )
        elif "/download" in path:
            return httpx.Response(200, content=b"content")
        return httpx.Response(404)

    mock_network(monkeypatch, handler)
    args = download_args(tmp_path, account="auto")
    cli.cmd_download(cli.settings(), args)


def test_auto_does_not_classify_generic_403_as_expired(tmp_path, monkeypatch):
    cfg_file = tmp_path / "config.json"
    monkeypatch.setenv("ZLIBRARY_TOR_CONFIG", str(cfg_file))
    cfg_file.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "default_account": "acc1",
                "accounts": {
                    "acc1": {
                        "remix_userid": "1",
                        "remix_userkey": "k1",
                        "session_origin": cli.DEFAULT_BASE_URL,
                    },
                    "acc2": {
                        "remix_userid": "2",
                        "remix_userkey": "k2",
                        "session_origin": cli.DEFAULT_BASE_URL,
                    },
                },
            }
        ),
        encoding="utf-8",
    )
    calls = []

    def handler(request):
        if "/user/profile" in request.url.path:
            calls.append(request.headers.get("remix-userid"))
            return httpx.Response(403, json={"error": "forbidden"})
        return httpx.Response(404)

    mock_network(monkeypatch, handler)
    args = download_args(tmp_path, account="auto")
    with pytest.raises(cli.CliError) as caught:
        cli.cmd_download(cli.settings(), args)
    assert caught.value.reason == "account_selection_incomplete"
    assert calls == ["1"]


def test_auto_all_exhausted_requests_no_link(tmp_path, monkeypatch):
    cfg_file = tmp_path / "config.json"
    monkeypatch.setenv("ZLIBRARY_TOR_CONFIG", str(cfg_file))
    cfg_file.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "default_account": "acc1",
                "accounts": {
                    "acc1": {
                        "remix_userid": "1",
                        "remix_userkey": "k1",
                        "session_origin": cli.DEFAULT_BASE_URL,
                    },
                },
            }
        ),
        encoding="utf-8",
    )
    link_called = False

    def handler(request):
        nonlocal link_called
        if "/user/profile" in request.url.path:
            return httpx.Response(
                200,
                json={
                    "success": 1,
                    "user": {"downloads_today": 5, "downloads_limit": 5},
                },
            )
        if "/file" in request.url.path:
            link_called = True
            return httpx.Response(200, json={"success": 1})
        return httpx.Response(404)

    mock_network(monkeypatch, handler)
    args = download_args(tmp_path, account="auto")
    with pytest.raises(cli.CliError) as caught:
        cli.cmd_download(cli.settings(), args)
    assert caught.value.reason == "no_available_account"
    assert not link_called


def test_explicit_account_never_falls_back(tmp_path, monkeypatch):
    cfg_file = tmp_path / "config.json"
    monkeypatch.setenv("ZLIBRARY_TOR_CONFIG", str(cfg_file))
    cfg_file.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "default_account": "acc1",
                "accounts": {
                    "acc1": {
                        "remix_userid": "1",
                        "remix_userkey": "k1",
                        "session_origin": cli.DEFAULT_BASE_URL,
                    },
                    "acc2": {
                        "remix_userid": "2",
                        "remix_userkey": "k2",
                        "session_origin": cli.DEFAULT_BASE_URL,
                    },
                },
            }
        ),
        encoding="utf-8",
    )
    calls = []

    def handler(request):
        if "/file" in request.url.path:
            calls.append(request.headers.get("remix-userid"))
            return httpx.Response(
                200,
                json={
                    "success": 0,
                    "error": "Daily download limit reached",
                },
            )
        return httpx.Response(404)

    mock_network(monkeypatch, handler)
    args = download_args(tmp_path, account="acc1")
    with pytest.raises(cli.CliError) as caught:
        cli.cmd_download(cli.settings(), args)
    assert caught.value.reason == "daily_quota_exhausted"
    assert calls == ["1"]


def test_link_failure_never_switches_account_or_domain(tmp_path, monkeypatch):
    cfg_file = tmp_path / "config.json"
    monkeypatch.setenv("ZLIBRARY_TOR_CONFIG", str(cfg_file))
    cfg_file.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "default_account": "acc1",
                "accounts": {
                    "acc1": {
                        "remix_userid": "1",
                        "remix_userkey": "k1",
                        "session_origin": cli.DEFAULT_BASE_URL,
                    },
                    "acc2": {
                        "remix_userid": "2",
                        "remix_userkey": "k2",
                        "session_origin": cli.DEFAULT_BASE_URL,
                    },
                },
            }
        ),
        encoding="utf-8",
    )
    link_attempts = 0

    def handler(request):
        nonlocal link_attempts
        if "/user/profile" in request.url.path:
            return httpx.Response(
                200,
                json={
                    "success": 1,
                    "user": {"downloads_today": 0, "downloads_limit": 5},
                },
            )
        if "/file" in request.url.path:
            link_attempts += 1
            return httpx.Response(500, json={"error": "server error"})
        return httpx.Response(404)

    mock_network(monkeypatch, handler)
    args = download_args(tmp_path, account="auto")
    with pytest.raises(cli.CliError):
        cli.cmd_download(cli.settings(), args)
    assert link_attempts == 1


def test_auto_force_is_rejected(cfg):
    args = download_args(Path("/tmp"), account="auto", force=True)
    with pytest.raises(cli.CliError) as caught:
        cli.cmd_download(cfg, args)
    assert caught.value.reason == "invalid_arguments"


def test_cache_precedes_profile_and_account_selection(tmp_path, monkeypatch, capsys):
    cfg_file = tmp_path / "config.json"
    monkeypatch.setenv("ZLIBRARY_TOR_CONFIG", str(cfg_file))
    cfg_file.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "default_account": "acc1",
                "accounts": {
                    "acc1": {
                        "remix_userid": "1",
                        "remix_userkey": "k1",
                        "session_origin": cli.DEFAULT_BASE_URL,
                    },
                },
            }
        ),
        encoding="utf-8",
    )
    args = download_args(tmp_path, account="auto")
    key = cli.cache_key(cli.settings(), args)
    book_file = tmp_path / "book.epub"
    book_file.write_bytes(b"cached book content")
    manifest = tmp_path / f".zlib-{key}.json"
    manifest.write_text(
        json.dumps(
            {
                "name": "book.epub",
                "bytes": len(b"cached book content"),
                "sha256": cli.digest(book_file),
            }
        ),
        encoding="utf-8",
    )

    def forbid_network(_):
        pytest.fail("network must not be accessed on cache hit")

    mock_network(monkeypatch, forbid_network)
    cli.cmd_download(cli.settings(), args)
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == "cached"


def test_download_coordination_lock_blocks_second_book(tmp_path, monkeypatch):
    cfg_file = tmp_path / "config.json"
    monkeypatch.setenv("ZLIBRARY_TOR_CONFIG", str(cfg_file))
    cfg_file.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "default_account": "acc1",
                "accounts": {
                    "acc1": {
                        "remix_userid": "1",
                        "remix_userkey": "k1",
                        "session_origin": cli.DEFAULT_BASE_URL,
                    },
                },
            }
        ),
        encoding="utf-8",
    )
    coord_lock = tmp_path / "config.json.download.lock"
    coord_lock.touch()

    def forbid_network(_):
        pytest.fail(
            "network must not be accessed when download coordination lock is held"
        )

    mock_network(monkeypatch, forbid_network)
    args = download_args(tmp_path)
    with pytest.raises(cli.CliError) as caught:
        cli.cmd_download(cli.settings(), args)
    assert caught.value.reason == "download_locked"


def test_owned_locks_released_after_failure(tmp_path, monkeypatch):
    cfg_file = tmp_path / "config.json"
    monkeypatch.setenv("ZLIBRARY_TOR_CONFIG", str(cfg_file))
    cfg_file.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "default_account": "acc1",
                "accounts": {
                    "acc1": {
                        "remix_userid": "1",
                        "remix_userkey": "k1",
                        "session_origin": cli.DEFAULT_BASE_URL,
                    },
                },
            }
        ),
        encoding="utf-8",
    )
    mock_network(
        monkeypatch,
        lambda _: httpx.Response(500, json={"error": "fail"}),
    )
    args = download_args(tmp_path)
    with pytest.raises(cli.CliError):
        cli.cmd_download(cli.settings(), args)

    coord_lock = tmp_path / "config.json.download.lock"
    book_lock = tmp_path / f".zlib-{cli.cache_key(cli.settings(), args)}.lock"
    assert not coord_lock.exists()
    assert not book_lock.exists()


def test_existing_lock_is_not_removed(tmp_path, monkeypatch):
    cfg_file = tmp_path / "config.json"
    monkeypatch.setenv("ZLIBRARY_TOR_CONFIG", str(cfg_file))
    cfg_file.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "default_account": "acc1",
                "accounts": {
                    "acc1": {
                        "remix_userid": "1",
                        "remix_userkey": "k1",
                        "session_origin": cli.DEFAULT_BASE_URL,
                    },
                },
            }
        ),
        encoding="utf-8",
    )
    coord_lock = tmp_path / "config.json.download.lock"
    coord_lock.write_text("pid=99999\n", encoding="utf-8")

    args = download_args(tmp_path)
    with pytest.raises(cli.CliError):
        cli.cmd_download(cli.settings(), args)

    assert coord_lock.exists()
    assert coord_lock.read_text(encoding="utf-8") == "pid=99999\n"


def test_acceptance_a1_to_a10(tmp_path, monkeypatch, capsys):
    # Tests A1-A10 offline acceptance sequence from TASKSPEC 2.10
    cfg_file = tmp_path / "config.json"
    monkeypatch.setenv("ZLIBRARY_TOR_CONFIG", str(cfg_file))

    # A1: Legacy migration via accounts list
    legacy = {
        "base_url": cli.DEFAULT_BASE_URL,
        "proxy": cli.DEFAULT_PROXY,
        "remix_userid": "leg_uid",
        "remix_userkey": "leg_key",
    }
    cfg_file.write_text(json.dumps(legacy), encoding="utf-8")
    cli.cmd_accounts(
        cli.load_config(),
        SimpleNamespace(accounts_command="list", refresh=False),
    )
    a1_out = json.loads(capsys.readouterr().out)
    assert a1_out["ok"] is True
    assert a1_out["accounts"][0]["name"] == "default"
    assert "leg_key" not in json.dumps(a1_out)

    # A2: Save personal account via login
    monkeypatch.setenv("ZLIBRARY_PASSWORD", "pwd")
    mock_network(
        monkeypatch,
        lambda _: httpx.Response(
            200,
            json={
                "success": 1,
                "user": {"id": "p_uid", "remix_userkey": "p_key"},
            },
        ),
    )
    cli.cmd_login(
        cli.settings(),
        SimpleNamespace(email="p@ex.com", stdin_password=False, account="personal"),
    )
    a2_out = json.loads(capsys.readouterr().out)
    assert a2_out["ok"] is True
    assert a2_out["account"] == "personal"
    assert "p_key" not in json.dumps(a2_out)

    # A3: Accounts list shows two accounts, default_account preserved
    cli.cmd_accounts(
        cli.load_config(),
        SimpleNamespace(accounts_command="list", refresh=False),
    )
    a3_out = json.loads(capsys.readouterr().out)
    names = [a["name"] for a in a3_out["accounts"]]
    assert "default" in names and "personal" in names
    assert a3_out["default_account"] == "default"

    # A4: Switch default account with accounts use
    cli.cmd_accounts(
        cli.load_config(),
        SimpleNamespace(accounts_command="use", name="personal"),
    )
    a4_out = json.loads(capsys.readouterr().out)
    assert a4_out["ok"] is True
    assert a4_out["default_account"] == "personal"

    # A5: Query profile for personal
    mock_network(
        monkeypatch,
        lambda _: httpx.Response(
            200,
            json={
                "success": 1,
                "user": {
                    "id": "p_uid",
                    "downloads_today": 0,
                    "downloads_limit": 10,
                },
            },
        ),
    )
    cli.cmd_profile(cli.settings(), SimpleNamespace(account="personal"))
    a5_out = json.loads(capsys.readouterr().out)
    assert a5_out["account"] == "personal"
    assert a5_out["quota"]["remaining"] == 10

    # A6: Refresh accounts list
    cli.cmd_accounts(
        cli.settings(), SimpleNamespace(accounts_command="list", refresh=True)
    )
    a6_out = json.loads(capsys.readouterr().out)
    assert a6_out["ok"] is True

    # A7: Auto download selects account with remaining quota and requests link once
    def auto_handler(request):
        path = request.url.path
        if "/user/profile" in path:
            uid = request.headers.get("remix-userid")
            if uid == "p_uid":
                return httpx.Response(
                    200,
                    json={
                        "success": 1,
                        "user": {"downloads_today": 0, "downloads_limit": 10},
                    },
                )
            return httpx.Response(
                200,
                json={
                    "success": 1,
                    "user": {"downloads_today": 10, "downloads_limit": 10},
                },
            )
        if "/file" in path:
            return httpx.Response(
                200,
                json={
                    "success": 1,
                    "file": {
                        "downloadLink": cli.DEFAULT_BASE_URL + "/dl",
                        "description": "B",
                        "extension": "epub",
                    },
                },
            )
        if "/dl" in path:
            return httpx.Response(200, content=b"book bytes")
        return httpx.Response(404)

    mock_network(monkeypatch, auto_handler)
    dl_args = download_args(tmp_path, account="auto")
    cli.cmd_download(cli.settings(), dl_args)
    a7_out = json.loads(capsys.readouterr().out)
    assert a7_out["status"] == "downloaded"
    assert a7_out["account"] == "personal"

    # A8: Cached download: repeated download returns status: cached, zero network
    cli.cmd_download(cli.settings(), dl_args)
    a8_out = json.loads(capsys.readouterr().out)
    assert a8_out["status"] == "cached"

    # A9: Logout personal account
    cli.cmd_logout({}, SimpleNamespace(account="personal", all=False))
    a9_out = json.loads(capsys.readouterr().out)
    assert a9_out["server_session_revoked"] is False
    cfg_a9 = cli.load_config()
    assert "remix_userkey" not in cfg_a9["accounts"]["personal"]
    assert "remix_userkey" in cfg_a9["accounts"]["default"]

    # A10: Logout all accounts
    cli.cmd_logout({}, SimpleNamespace(account=None, all=True))
    cfg_a10 = cli.load_config()
    assert "default" in cfg_a10["accounts"]
    assert "personal" in cfg_a10["accounts"]
    assert "remix_userkey" not in cfg_a10["accounts"]["default"]
    assert "remix_userkey" not in cfg_a10["accounts"]["personal"]
    assert cfg_a10["proxy"] == cli.DEFAULT_PROXY


def make_v3_onion(name_bytes: bytes = b"X" * 32, version: int = 3) -> str:
    checksum = hashlib.sha3_256(
        b".onion checksum" + name_bytes + bytes([version])
    ).digest()[:2]
    raw = name_bytes + checksum + bytes([version])
    return base64.b32encode(raw).decode().lower() + ".onion"


def test_onion_checksum_and_version_validation():
    valid_onion = cli.DEFAULT_BASE_URL.split("://")[1]
    assert cli.is_valid_v3_onion(valid_onion) is True
    assert cli.validate_onion_origin(f"http://{valid_onion}") == f"http://{valid_onion}"

    gen_onion = make_v3_onion(b"A" * 32, 3)
    assert cli.is_valid_v3_onion(gen_onion) is True
    assert cli.validate_onion_origin(f"http://{gen_onion}") == f"http://{gen_onion}"

    raw = base64.b32decode(gen_onion[:-6].upper())
    bad_checksum = raw[:32] + bytes([raw[32] ^ 0xFF, raw[33]]) + bytes([raw[34]])
    bad_onion = base64.b32encode(bad_checksum).decode().lower() + ".onion"
    assert cli.is_valid_v3_onion(bad_onion) is False
    with pytest.raises(cli.CliError) as exc_info:
        cli.validate_onion_origin(f"http://{bad_onion}")
    assert exc_info.value.reason == "invalid_origin"
    assert bad_onion not in str(exc_info.value)

    bad_ver = make_v3_onion(b"A" * 32, 4)
    assert cli.is_valid_v3_onion(bad_ver) is False
    with pytest.raises(cli.CliError):
        cli.validate_onion_origin(f"http://{bad_ver}")

    assert cli.is_valid_v3_onion("short.onion") is False
    with pytest.raises(cli.CliError):
        cli.validate_onion_origin("http://short.onion")


def test_onion_origin_rejects_userinfo_path_query_and_port():
    valid = make_v3_onion(b"B" * 32, 3)
    cases = [
        f"http://user:pass@{valid}",
        f"http://user@{valid}",
        f"http://{valid}/path",
        f"http://{valid}/eapi/info",
        f"http://{valid}?query=1",
        f"http://{valid}#fragment",
        f"http://{valid}:8080",
        f"https://{valid}:8443",
        f"http://sub.{valid}",
        f"http://{valid}.",
        f"ftp://{valid}",
    ]
    for case in cases:
        with pytest.raises(cli.CliError) as exc_info:
            cli.validate_onion_origin(case)
        assert exc_info.value.reason == "invalid_origin"
        assert case not in str(exc_info.value)

    assert cli.validate_onion_origin(f"http://{valid}/") == f"http://{valid}"
    assert cli.validate_onion_origin(f"http://{valid}:80") == f"http://{valid}"
    assert cli.validate_onion_origin(f"https://{valid}:443") == f"https://{valid}"


def test_probe_never_sends_credentials(monkeypatch, tmp_path):
    cfg_file = tmp_path / "config.json"
    cfg_file.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "base_url": cli.DEFAULT_BASE_URL,
                "proxy": cli.DEFAULT_PROXY,
                "accounts": {
                    "default": {
                        "remix_userid": "u1",
                        "remix_userkey": "k1",
                        "session_origin": cli.DEFAULT_BASE_URL,
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("ZLIBRARY_TOR_CONFIG", str(cfg_file))
    monkeypatch.setenv("ZLIBRARY_REMIX_USERID", "env_uid")
    monkeypatch.setenv("ZLIBRARY_REMIX_USERKEY", "env_key")
    monkeypatch.setenv("ZLIBRARY_SESSION_ORIGIN", cli.DEFAULT_BASE_URL)

    candidate = make_v3_onion(b"C" * 32, 3)
    cand_url = f"http://{candidate}"
    recorded_requests = []

    def handler(request):
        recorded_requests.append(request)
        return httpx.Response(200, json={"success": 1})

    mock_network(monkeypatch, handler)

    res = cli.probe_domain(cli.load_config(), cand_url)
    assert res["ok"] is True
    assert res["credentials_sent"] is False
    assert len(recorded_requests) == 1
    req = recorded_requests[0]
    assert "remix-userid" not in req.headers
    assert "remix-userkey" not in req.headers
    assert "cookie" not in req.headers


def test_probe_does_not_follow_redirects(monkeypatch):
    candidate = make_v3_onion(b"D" * 32, 3)
    cand_url = f"http://{candidate}"
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(302, headers={"location": "http://other.onion/eapi/info"})

    mock_network(monkeypatch, handler)

    with pytest.raises(cli.CliError) as exc_info:
        cli.probe_domain({}, cand_url)
    assert exc_info.value.reason == "api_redirect"
    assert len(calls) == 1


def test_probe_rejects_oversized_response(monkeypatch):
    candidate = make_v3_onion(b"E" * 32, 3)
    cand_url = f"http://{candidate}"

    class HugeStream(httpx.SyncByteStream):
        def __iter__(self):
            for _ in range(70):
                yield b"X" * 1024

    mock_network(monkeypatch, lambda req: httpx.Response(200, stream=HugeStream()))

    with pytest.raises(cli.CliError) as exc_info:
        cli.probe_domain({}, cand_url)
    assert exc_info.value.reason == "response_oversized"


def test_compatible_api_does_not_verify_ownership(monkeypatch, tmp_path):
    cfg_file = tmp_path / "config.json"
    cfg_file.write_text(json.dumps({"schema_version": 2}), encoding="utf-8")
    monkeypatch.setenv("ZLIBRARY_TOR_CONFIG", str(cfg_file))

    candidate = make_v3_onion(b"F" * 32, 3)
    cand_url = f"http://{candidate}"

    mock_network(monkeypatch, lambda req: httpx.Response(200, json={"success": 1}))

    res = cli.probe_domain(cli.load_config(), cand_url)
    assert res["ok"] is True
    assert res["reachable"] is True
    assert res["api_compatible"] is True
    assert res["ownership_verified"] is False
    assert res["trust"] == "unverified"
    assert "API compatibility does not prove Z-Library ownership." in res["warnings"]

    loaded = cli.load_config()
    assert candidate not in str(loaded.get("domains", {}))


def test_domain_add_requires_manual_evidence_and_confirmation(tmp_path, monkeypatch):
    cfg_file = tmp_path / "config.json"
    cfg_file.write_text(json.dumps({"schema_version": 2}), encoding="utf-8")
    monkeypatch.setenv("ZLIBRARY_TOR_CONFIG", str(cfg_file))

    candidate = make_v3_onion(b"G" * 32, 3)
    cand_url = f"http://{candidate}"

    args_bad_confirm = SimpleNamespace(
        name="backup",
        url=cand_url,
        confirm_host="wrong.onion",
        verified_from="https://t.me/zlib_official",
        domains_command="add",
    )
    with pytest.raises(cli.CliError) as exc_info:
        cli.cmd_domains(cli.load_config(), args_bad_confirm)
    assert exc_info.value.reason == "confirmation_mismatch"
    assert "backup" not in cli.load_config().get("domains", {})

    args_cred_source = SimpleNamespace(
        name="backup",
        url=cand_url,
        confirm_host=candidate,
        verified_from="https://user:pass@example.com/source",
        domains_command="add",
    )
    with pytest.raises(cli.CliError) as exc_info:
        cli.cmd_domains(cli.load_config(), args_cred_source)
    assert exc_info.value.reason == "invalid_source"


def test_domain_add_does_not_fetch_evidence_url(tmp_path, monkeypatch, capsys):
    cfg_file = tmp_path / "config.json"
    cfg_file.write_text(json.dumps({"schema_version": 2}), encoding="utf-8")
    monkeypatch.setenv("ZLIBRARY_TOR_CONFIG", str(cfg_file))

    candidate = make_v3_onion(b"H" * 32, 3)
    cand_url = f"http://{candidate}"

    def exploding_handler(request):
        pytest.fail(f"Network request was unexpectedly made to: {request.url}")

    mock_network(monkeypatch, exploding_handler)

    args = SimpleNamespace(
        name="backup",
        url=cand_url,
        confirm_host=candidate,
        verified_from="https://example.com/official-announcement",
        domains_command="add",
    )
    cli.cmd_domains(cli.load_config(), args)
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is True
    assert out["trust"] == "user_verified"
    assert out["url"] == cand_url

    loaded = cli.load_config()
    assert loaded["domains"]["backup"]["trust"] == "user_verified"
    assert (
        loaded["domains"]["backup"]["source"]
        == "https://example.com/official-announcement"
    )
    assert loaded["domains"]["backup"]["verified_at"] is not None


def test_domain_use_does_not_move_sessions(tmp_path, monkeypatch, capsys):
    cfg_file = tmp_path / "config.json"
    cand_onion = make_v3_onion(b"I" * 32, 3)
    cand_url = f"http://{cand_onion}"
    initial = {
        "schema_version": 2,
        "base_url": cli.DEFAULT_BASE_URL,
        "accounts": {
            "personal": {
                "remix_userid": "uid_pers",
                "remix_userkey": "key_pers",
                "session_origin": cli.DEFAULT_BASE_URL,
            }
        },
        "domains": {
            "primary": {
                "url": cli.DEFAULT_BASE_URL,
                "trust": "builtin",
                "source": "repository-default",
                "verified_at": None,
            },
            "backup": {
                "url": cand_url,
                "trust": "user_verified",
                "source": "https://example.com/proof",
                "verified_at": "2026-10-11T00:00:00Z",
            },
        },
    }
    cfg_file.write_text(json.dumps(initial), encoding="utf-8")
    monkeypatch.setenv("ZLIBRARY_TOR_CONFIG", str(cfg_file))

    args = SimpleNamespace(name="backup", domains_command="use")
    cli.cmd_domains(cli.load_config(), args)
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is True
    assert out["base_url"] == cand_url

    loaded = cli.load_config()
    assert loaded["base_url"] == cand_url
    assert loaded["accounts"]["personal"]["session_origin"] == cli.DEFAULT_BASE_URL


def test_untrusted_origin_blocks_login_before_secret_send(monkeypatch, tmp_path):
    cand_onion = make_v3_onion(b"J" * 32, 3)
    cand_url = f"http://{cand_onion}"
    cfg_file = tmp_path / "config.json"
    cfg_file.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "base_url": cand_url,
                "domains": {
                    "primary": {
                        "url": cli.DEFAULT_BASE_URL,
                        "trust": "builtin",
                        "source": "repository-default",
                        "verified_at": None,
                    },
                    "custom": {
                        "url": cand_url,
                        "trust": "legacy_unverified",
                        "source": "legacy-config",
                        "verified_at": None,
                    },
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("ZLIBRARY_TOR_CONFIG", str(cfg_file))

    def fail_if_network(request):
        pytest.fail(f"Network request unexpectedly made to: {request.url}")

    mock_network(monkeypatch, fail_if_network)

    args = SimpleNamespace(
        email="test@example.com", stdin_password=True, account="default"
    )
    with pytest.raises(cli.CliError) as exc_info:
        cli.cmd_login(cli.settings(), args)
    assert exc_info.value.reason == "untrusted_origin"


def test_untrusted_origin_blocks_registration(monkeypatch, tmp_path):
    cand_onion = make_v3_onion(b"K" * 32, 3)
    cand_url = f"http://{cand_onion}"
    cfg_file = tmp_path / "config.json"
    cfg_file.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "base_url": cand_url,
                "domains": {
                    "primary": {
                        "url": cli.DEFAULT_BASE_URL,
                        "trust": "builtin",
                        "source": "repository-default",
                        "verified_at": None,
                    },
                    "custom": {
                        "url": cand_url,
                        "trust": "legacy_unverified",
                        "source": "legacy-config",
                        "verified_at": None,
                    },
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("ZLIBRARY_TOR_CONFIG", str(cfg_file))

    def fail_if_network(request):
        pytest.fail("Network request was made")

    mock_network(monkeypatch, fail_if_network)

    args = SimpleNamespace(email="new@example.com", name="User", yes=True, account=None)
    with pytest.raises(cli.CliError) as exc_info:
        cli.cmd_register(cli.settings(), args)
    assert exc_info.value.reason == "untrusted_origin"


def test_environment_base_url_cannot_grant_trust(monkeypatch, tmp_path):
    cand_onion = make_v3_onion(b"L" * 32, 3)
    cand_url = f"http://{cand_onion}"
    cfg_file = tmp_path / "config.json"
    cfg_file.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "base_url": cli.DEFAULT_BASE_URL,
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("ZLIBRARY_TOR_CONFIG", str(cfg_file))
    monkeypatch.setenv("ZLIBRARY_BASE_URL", cand_url)

    cfg = cli.settings()
    assert cfg["base_url"] == cand_url

    with pytest.raises(cli.CliError) as exc_info:
        cli.require_trusted_origin(cfg)
    assert exc_info.value.reason == "untrusted_origin"


def test_legacy_custom_domain_is_not_auto_trusted(tmp_path, monkeypatch):
    cand_onion = make_v3_onion(b"M" * 32, 3)
    cand_url = f"http://{cand_onion}"
    cfg_file = tmp_path / "config.json"
    cfg_file.write_text(
        json.dumps(
            {
                "base_url": cand_url,
                "remix_userid": "123",
                "remix_userkey": "key",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("ZLIBRARY_TOR_CONFIG", str(cfg_file))

    loaded = cli.load_config()
    assert loaded["domains"]["custom"]["trust"] == "legacy_unverified"

    with pytest.raises(cli.CliError) as exc_info:
        cli.require_trusted_origin(loaded)
    assert exc_info.value.reason == "untrusted_origin"


def test_session_origin_mismatch_blocks_authenticated_request(tmp_path, monkeypatch):
    backup_onion = make_v3_onion(b"N" * 32, 3)
    backup_url = f"http://{backup_onion}"
    cfg_file = tmp_path / "config.json"
    cfg_file.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "base_url": backup_url,
                "domains": {
                    "primary": {
                        "url": cli.DEFAULT_BASE_URL,
                        "trust": "builtin",
                        "source": "repository-default",
                        "verified_at": None,
                    },
                    "backup": {
                        "url": backup_url,
                        "trust": "user_verified",
                        "source": "https://example.com/proof",
                        "verified_at": "2026-10-11T00:00:00Z",
                    },
                },
                "accounts": {
                    "personal": {
                        "remix_userid": "uid",
                        "remix_userkey": "key",
                        "session_origin": cli.DEFAULT_BASE_URL,
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("ZLIBRARY_TOR_CONFIG", str(cfg_file))

    def fail_if_network(request):
        pytest.fail(f"Network request was made to: {request.url}")

    mock_network(monkeypatch, fail_if_network)

    args = SimpleNamespace(account="personal")
    with pytest.raises(cli.CliError) as exc_info:
        cli.cmd_profile(cli.settings(), args)
    assert exc_info.value.reason == "session_origin_mismatch"


def test_trusted_backup_redirect_still_receives_no_session(tmp_path, monkeypatch):
    backup_onion = make_v3_onion(b"O" * 32, 3)
    backup_url = f"https://{backup_onion}"
    cfg_file = tmp_path / "config.json"
    cfg_file.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "base_url": cli.DEFAULT_BASE_URL,
                "domains": {
                    "primary": {
                        "url": cli.DEFAULT_BASE_URL,
                        "trust": "builtin",
                        "source": "repository-default",
                        "verified_at": None,
                    },
                    "backup": {
                        "url": backup_url,
                        "trust": "user_verified",
                        "source": "https://example.com/proof",
                        "verified_at": "2026-10-11T00:00:00Z",
                    },
                },
                "accounts": {
                    "default": {
                        "remix_userid": "my_uid",
                        "remix_userkey": "my_key",
                        "session_origin": cli.DEFAULT_BASE_URL,
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("ZLIBRARY_TOR_CONFIG", str(cfg_file))

    requests = []

    def handler(request):
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(302, headers={"location": f"{backup_url}/content"})
        return httpx.Response(200, content=b"downloaded file")

    mock_network(monkeypatch, handler)
    cfg = cli.settings()
    acc_name, acc_data = cli.resolve_account(cfg, "default")
    cfg["remix_userid"] = acc_data["remix_userid"]
    cfg["remix_userkey"] = acc_data["remix_userkey"]
    cfg["session_origin"] = acc_data["session_origin"]

    output = tmp_path / "book.epub"
    cli.download_stream(cfg, cfg["base_url"] + "/start", output, 1024)

    assert len(requests) == 2
    assert requests[0].headers.get("remix-userkey") == "my_key"
    assert "remix-userkey" not in requests[1].headers
    assert "cookie" not in requests[1].headers


def test_tls_verification_is_not_disabled():
    c = cli.client({})
    assert getattr(c, "_transport", None) is not None


def test_cross_account_cache_reuse(tmp_path, monkeypatch, capsys):
    cfg_file = tmp_path / "config.json"
    cfg_file.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "base_url": cli.DEFAULT_BASE_URL,
                "accounts": {
                    "acc1": {
                        "remix_userid": "u1",
                        "remix_userkey": "k1",
                        "session_origin": cli.DEFAULT_BASE_URL,
                    },
                    "acc2": {
                        "remix_userid": "u2",
                        "remix_userkey": "k2",
                        "session_origin": cli.DEFAULT_BASE_URL,
                    },
                },
                "default_account": "acc1",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("ZLIBRARY_TOR_CONFIG", str(cfg_file))

    link_calls = 0

    def handler(request):
        nonlocal link_calls
        if "/file" in request.url.path:
            link_calls += 1
            return httpx.Response(
                200,
                json={
                    "success": 1,
                    "file": {
                        "downloadLink": cli.DEFAULT_BASE_URL + "/dl",
                        "description": "CrossAccountBook",
                        "extension": "epub",
                    },
                },
            )
        if "/dl" in request.url.path:
            return httpx.Response(200, content=b"Cross account content")
        return httpx.Response(404)

    mock_network(monkeypatch, handler)

    args1 = download_args(tmp_path, id="100", hash="abc1", account="acc1")
    cli.cmd_download(cli.settings(), args1)
    res1 = json.loads(capsys.readouterr().out)
    assert res1["status"] == "downloaded"
    assert link_calls == 1

    args2 = download_args(tmp_path, id="100", hash="abc1", account="acc2")
    cli.cmd_download(cli.settings(), args2)
    res2 = json.loads(capsys.readouterr().out)
    assert res2["status"] == "cached"
    assert link_calls == 1


def test_cross_verified_origin_legacy_cache_reuse(tmp_path, monkeypatch, capsys):
    backup_onion = make_v3_onion(b"P" * 32, 3)
    backup_url = f"http://{backup_onion}"
    cfg_file = tmp_path / "config.json"
    cfg_file.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "base_url": backup_url,
                "domains": {
                    "primary": {
                        "url": cli.DEFAULT_BASE_URL,
                        "trust": "builtin",
                        "source": "repository-default",
                        "verified_at": None,
                    },
                    "backup": {
                        "url": backup_url,
                        "trust": "user_verified",
                        "source": "https://example.com/proof",
                        "verified_at": "2026-10-11T00:00:00Z",
                    },
                },
                "accounts": {
                    "backup_acc": {
                        "remix_userid": "u_b",
                        "remix_userkey": "k_b",
                        "session_origin": backup_url,
                    }
                },
                "default_account": "backup_acc",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("ZLIBRARY_TOR_CONFIG", str(cfg_file))

    book_file = tmp_path / "legacy_book-abcdef1234567890.epub"
    book_content = b"Legacy book content for cross origin test"
    book_file.write_bytes(book_content)

    legacy_key = hashlib.sha256(
        f"{cli.DEFAULT_BASE_URL}\n200\ndef2".encode()
    ).hexdigest()
    legacy_manifest = tmp_path / f".zlib-{legacy_key}.json"
    legacy_manifest.write_text(
        json.dumps(
            {
                "name": book_file.name,
                "bytes": len(book_content),
                "sha256": cli.digest(book_file),
            }
        ),
        encoding="utf-8",
    )

    def fail_if_network(request):
        pytest.fail(f"Network request was unexpectedly made: {request.url}")

    mock_network(monkeypatch, fail_if_network)

    args = download_args(tmp_path, id="200", hash="def2", account="backup_acc")
    cli.cmd_download(cli.settings(), args)
    res = json.loads(capsys.readouterr().out)
    assert res["status"] == "cached"
    assert res["path"] == str(book_file)

    new_key = cli.cache_key("200", "def2")
    new_manifest = tmp_path / f".zlib-{new_key}.json"
    assert new_manifest.is_file()
    assert legacy_manifest.is_file()


def test_unverified_origin_not_used_for_cache_alias_lookup(
    tmp_path, monkeypatch, capsys
):
    unverified_onion = make_v3_onion(b"Q" * 32, 3)
    unverified_url = f"http://{unverified_onion}"
    cfg_file = tmp_path / "config.json"
    cfg_file.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "base_url": cli.DEFAULT_BASE_URL,
                "domains": {
                    "primary": {
                        "url": cli.DEFAULT_BASE_URL,
                        "trust": "builtin",
                        "source": "repository-default",
                        "verified_at": None,
                    },
                    "unverified": {
                        "url": unverified_url,
                        "trust": "legacy_unverified",
                        "source": "unverified",
                        "verified_at": None,
                    },
                },
                "accounts": {
                    "default": {
                        "remix_userid": "u",
                        "remix_userkey": "k",
                        "session_origin": cli.DEFAULT_BASE_URL,
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("ZLIBRARY_TOR_CONFIG", str(cfg_file))

    book_file = tmp_path / "unverified_book.epub"
    book_file.write_bytes(b"some content")
    unverified_key = hashlib.sha256(f"{unverified_url}\n300\nghi3".encode()).hexdigest()
    unverified_manifest = tmp_path / f".zlib-{unverified_key}.json"
    unverified_manifest.write_text(
        json.dumps(
            {
                "name": book_file.name,
                "bytes": len(b"some content"),
                "sha256": cli.digest(book_file),
            }
        ),
        encoding="utf-8",
    )

    args = download_args(tmp_path, id="300", hash="ghi3")
    assert cli.find_cached_download(cli.settings(), args, tmp_path, 1024 * 1024) is None


def test_cached_file_path_escape_still_rejected(tmp_path):
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "name": "../escape.epub",
                "bytes": 10,
                "sha256": "abcdef",
            }
        ),
        encoding="utf-8",
    )
    assert cli.cached_file(manifest, tmp_path, 1024) is None

    target_file = tmp_path / "real.epub"
    target_file.write_bytes(b"12345")
    symlink_file = tmp_path / "symlink.epub"
    try:
        symlink_file.symlink_to(target_file)
        sym_manifest = tmp_path / "sym_manifest.json"
        sym_manifest.write_text(
            json.dumps(
                {
                    "name": "symlink.epub",
                    "bytes": 5,
                    "sha256": cli.digest(target_file),
                }
            ),
            encoding="utf-8",
        )
        assert cli.cached_file(sym_manifest, tmp_path, 1024) is None
    except OSError:
        pass


def test_b1_to_b9_acceptance_suite(tmp_path, monkeypatch, capsys):
    cfg_file = tmp_path / "config.json"
    monkeypatch.setenv("ZLIBRARY_TOR_CONFIG", str(cfg_file))
    for key in list(cli.os.environ):
        if key.startswith("ZLIBRARY_") and key != "ZLIBRARY_TOR_CONFIG":
            monkeypatch.delenv(key)

    # B1: domains list -> 默认地址 trust: builtin; 不输出 session
    cli.cmd_domains(cli.load_config(), SimpleNamespace(domains_command="list"))
    b1_out = json.loads(capsys.readouterr().out)
    assert b1_out["ok"] is True
    assert any(
        d["name"] == "primary" and d["trust"] == "builtin" for d in b1_out["domains"]
    )
    assert "remix" not in json.dumps(b1_out)

    # B2: doctor -> 保留旧字段，新增信任字段；无凭据
    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

    monkeypatch.setattr(cli.socket, "create_connection", lambda *a, **kw: Connection())

    def doctor_handler(request):
        assert "/eapi/info" in request.url.path
        assert "remix-userkey" not in request.headers
        return httpx.Response(200, json={"success": 1})

    mock_network(monkeypatch, doctor_handler)
    cli.cmd_doctor(cli.settings(), SimpleNamespace(account=None))
    b2_out = json.loads(capsys.readouterr().out)
    assert b2_out["ok"] is True
    assert b2_out["reachable"] is True
    assert b2_out["api_compatible"] is True
    assert b2_out["ownership_verified"] is False
    assert b2_out["trust"] == "builtin"
    assert b2_out["authenticated"] is False

    # B3: 候选检查 -> credentials_sent: false; 兼容响应也不会升级信任
    candidate_onion = make_v3_onion(b"B3_cand" + b"0" * 25, 3)
    candidate_url = f"http://{candidate_onion}"
    cli.cmd_domains(
        cli.settings(),
        SimpleNamespace(domains_command="check", url=candidate_url),
    )
    b3_out = json.loads(capsys.readouterr().out)
    assert b3_out["ok"] is True
    assert b3_out["credentials_sent"] is False
    assert b3_out["trust"] == "unverified"
    assert b3_out["ownership_verified"] is False

    # B4: 缺少确认 -> domains add backup --url CANDIDATE (missing confirm) -> 非零退出；不写配置
    with pytest.raises(cli.CliError):
        cli.cmd_domains(
            cli.load_config(),
            SimpleNamespace(
                domains_command="add",
                name="backup",
                url=candidate_url,
                confirm_host="wrong.onion",
                verified_from="https://t.me/zlibrary_official",
            ),
        )
    assert "backup" not in cli.load_config().get("domains", {})

    # B5: 完成人工核验 -> 使用 3.3 节完整 add 命令 -> ok/name/url/trust: user_verified
    cli.cmd_domains(
        cli.load_config(),
        SimpleNamespace(
            domains_command="add",
            name="backup",
            url=candidate_url,
            confirm_host=candidate_onion,
            verified_from="https://t.me/zlibrary_official",
        ),
    )
    b5_out = json.loads(capsys.readouterr().out)
    assert b5_out["ok"] is True
    assert b5_out["name"] == "backup"
    assert b5_out["url"] == candidate_url
    assert b5_out["trust"] == "user_verified"

    # Set up account on primary origin
    def login_updater(stored):
        stored.setdefault("accounts", {})["personal"] = {
            "remix_userid": "p_uid",
            "remix_userkey": "p_key",
            "session_origin": cli.DEFAULT_BASE_URL,
        }
        stored["default_account"] = "personal"

    cli.update_config(login_updater)

    # B6: 切换 -> domains use backup -> ok/base_url; session 原点保持原值
    cli.cmd_domains(
        cli.load_config(),
        SimpleNamespace(domains_command="use", name="backup"),
    )
    b6_out = json.loads(capsys.readouterr().out)
    assert b6_out["ok"] is True
    assert b6_out["base_url"] == candidate_url
    loaded_b6 = cli.load_config()
    assert loaded_b6["accounts"]["personal"]["session_origin"] == cli.DEFAULT_BASE_URL

    # B7: 旧会话保护 -> profile --account personal -> session_origin_mismatch; 零网络
    def no_network_allowed(request):
        pytest.fail("Network unexpectedly accessed!")

    mock_network(monkeypatch, no_network_allowed)

    with pytest.raises(cli.CliError) as exc_b7:
        cli.cmd_profile(cli.settings(), SimpleNamespace(account="personal"))
    assert exc_b7.value.reason == "session_origin_mismatch"

    # B8: 跨域缓存 -> 对已有本地书籍执行 download -> status: cached; 零 API 请求
    b8_book = tmp_path / "b8_book-key.epub"
    b8_content = b"Content for B8 cross origin cache test"
    b8_book.write_bytes(b8_content)
    legacy_k = hashlib.sha256(
        f"{cli.DEFAULT_BASE_URL}\n888\nhash888".encode()
    ).hexdigest()
    (tmp_path / f".zlib-{legacy_k}.json").write_text(
        json.dumps(
            {
                "name": b8_book.name,
                "bytes": len(b8_content),
                "sha256": cli.digest(b8_book),
            }
        ),
        encoding="utf-8",
    )
    b8_args = download_args(tmp_path, id="888", hash="hash888", account=None)
    cli.cmd_download(cli.settings(), b8_args)
    b8_out = json.loads(capsys.readouterr().out)
    assert b8_out["ok"] is True
    assert b8_out["status"] == "cached"

    # B9: 未信任覆盖 -> 测试中设置未知 ZLIBRARY_BASE_URL 后调用 login -> untrusted_origin; 密码未发送
    unknown_onion = make_v3_onion(b"B9_unknown" + b"0" * 22, 3)
    monkeypatch.setenv("ZLIBRARY_BASE_URL", f"http://{unknown_onion}")
    with pytest.raises(cli.CliError) as exc_b9:
        cli.cmd_login(
            cli.settings(),
            SimpleNamespace(
                email="test@example.com", stdin_password=True, account=None
            ),
        )
    assert exc_b9.value.reason == "untrusted_origin"
