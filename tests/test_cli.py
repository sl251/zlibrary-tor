import io
import json
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
    assert cli.load_config()["remix_userkey"] == "private-key"
    cli.cmd_logout({}, None)
    assert "remix_userkey" not in cli.load_config()
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
    return SimpleNamespace(
        id="123",
        hash="abcdef",
        output_dir=str(tmp_path),
        max_mb=1,
        force=False,
        **kwargs,
    )


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
