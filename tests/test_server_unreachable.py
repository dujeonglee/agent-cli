"""서버에 못 닿으면 그 자리에서 말한다 (v10.126.0).

종전엔 ``/v1/models`` 가 연결 거부여도 "Could not read /v1/models … — continuing" 로 넘어가고,
등록 안 된 모델이면 프로브 3종 → "Context window size [4096]:" 질문(비대화식이면 EOF) →
``No dialect for model`` (rc=2) 로 10~20초 뒤 엉뚱한 원인으로 끝났다. 마지막 LLM 실패 줄은
``HTTPConnectionPool…NewConnectionError(<urllib3… object at 0x…>)`` 원문이었다.

불변(먼저 고정): **등록된 모델 + 서버 꺼짐 → 종전처럼 뜬다** — 서버를 나중에 켜는 상주 방과
보드 되살리기(``agent-cli web``, stdin 은 ``DEVNULL``, ``quiet=True``)가 이 경로에 기댄다.
"""

from __future__ import annotations

import contextlib
import json
import socket
import ssl
import threading
from unittest.mock import patch

import pytest
import requests
import typer
from rich.console import Console

import agent_cli.config as _config
import agent_cli.dialects as _dialects
import agent_cli.main as m
from agent_cli import model_check
from agent_cli.providers import capabilities as _caps
from agent_cli.providers.credentials import Credentials
from agent_cli.providers.http import classify_connection_error

URL = "http://127.0.0.1:1/v1"

# 예외는 **실제 소켓**에서 만든다 — 사슬 모양(MaxRetryError·NewConnectionError·
# NameResolutionError …)이 urllib3 1.26·2.x 마다 달라 손으로 만든 예외는 분류를 못 고정한다.
# 세션은 ``trust_env=False`` — 프록시 환경변수가 테스트를 흔들지 않게.
_PROXY_VARS = (
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "NO_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy",
    "no_proxy",
)


def _real_exc(url, *, timeout=3.0, proxies=None):
    s = requests.Session()
    s.trust_env = False
    try:
        s.get(url, timeout=timeout, proxies=proxies or {})
    except requests.exceptions.RequestException as e:
        return e
    finally:
        s.close()
    raise AssertionError(f"no error from {url}")


def _closed_port() -> int:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


@contextlib.contextmanager
def _listener(mode):
    """``close`` = 받고 요청을 읽은 뒤 바로 닫는다 · ``silent`` = 받고 아무 말도 안 한다."""
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(8)
    held = []

    def _serve():
        while True:
            try:
                c, _ = srv.accept()
            except OSError:
                return
            if mode == "close":
                with contextlib.suppress(OSError):
                    c.recv(4096)
                c.close()
            else:
                held.append(c)

    threading.Thread(target=_serve, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{srv.getsockname()[1]}/v1"
    finally:
        srv.close()
        for c in held:
            c.close()


def _refused():
    return _real_exc(f"http://127.0.0.1:{_closed_port()}/v1/models")


def _tls_error():
    """fetch 테스트와 같은 방식 — requests 가 실제로 싸는 모양 그대로."""
    import urllib3.exceptions as u3

    inner = ssl.SSLCertVerificationError(
        1,
        "[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: "
        "unable to get local issuer certificate (_ssl.c:1006)",
    )
    inner.verify_message = "unable to get local issuer certificate"
    return requests.exceptions.SSLError(
        u3.MaxRetryError(None, "/v1/models", u3.SSLError(inner))
    )


@pytest.fixture
def no_proxy_env(monkeypatch):
    for v in _PROXY_VARS:
        monkeypatch.delenv(v, raising=False)


@pytest.fixture(autouse=True)
def registry(tmp_path, monkeypatch):
    target = tmp_path / "models.json"
    target.write_text(
        json.dumps(
            {"models": {"reg-model": {"context_window": 32768, "dialect": "json_fc"}}}
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(_config, "_SEARCH_PATHS", [target])
    monkeypatch.setattr(_config, "_cached_registry", None)
    monkeypatch.setattr(_dialects, "_override", None)
    monkeypatch.setattr(_config, "_CONFIG_PATHS", [tmp_path / "config.json"])
    (tmp_path / "config.json").write_text('{"provider": "openai"}', encoding="utf-8")
    _config.reload_config()
    model_check._cache.clear()
    yield
    model_check._cache.clear()
    _config.reload_config()


@pytest.fixture
def out(monkeypatch):
    con = Console(width=80, record=True, color_system=None)
    monkeypatch.setattr(m, "console", con)
    return con


@pytest.fixture
def probes(monkeypatch):
    """런타임 탐지(프로브 3종) 호출 수."""
    calls = []

    def _detect(*a, **k):
        calls.append(a)

    monkeypatch.setattr(_caps, "_detect_runtime_capabilities", _detect)
    return calls


@pytest.fixture
def asked(monkeypatch):
    calls = []
    monkeypatch.setattr(
        m, "_prompt_model_capabilities", lambda model: calls.append(model)
    )
    return calls


def _models_get(monkeypatch, *, exc=None, status=200, payload=None):
    class _Resp:
        status_code = status

        def json(self):
            return payload or {}

    def _get(*a, **k):
        if exc is not None:
            raise exc
        return _Resp()

    monkeypatch.setattr(requests, "get", _get)


# ── 불변: 등록된 모델 + 서버 꺼짐 → 뜬다 ───────────────────────


@pytest.mark.parametrize("quiet", [False, True])
def test_registered_model_and_server_down_still_boots(
    monkeypatch, out, probes, asked, quiet
):
    _models_get(monkeypatch, exc=_refused())
    monkeypatch.setenv("AGENT_CLI_NO_INPUT", "1")
    binding = m._bootstrap_provider(
        "openai", "reg-model", URL, "", None, 0, quiet=quiet, explicit_model=True
    )
    assert binding.model == "reg-model"
    assert binding.capabilities.context_window == 32768
    assert binding.dialect.name == "json_fc"
    assert probes == [] and asked == []
    text = out.export_text()
    if quiet:
        assert text == ""
    else:
        assert f"Cannot connect to {URL}" in text
        assert "registered settings" in text
        assert "Could not read /v1/models" not in text


def test_web_command_boots_with_registered_model_and_server_down(
    tmp_path, monkeypatch, probes
):
    """보드가 방을 띄우는 그 명령 — `web` 은 부트 뒤 서버를 세운다. 서버 기동 직전까지 닿는지."""
    from typer.testing import CliRunner

    _models_get(monkeypatch, exc=_refused())
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("AGENT_CLI_NO_INPUT", "1")
    reached = []

    class _Stop(Exception):
        pass

    def _stop(*a, **k):
        reached.append(1)
        raise _Stop

    with (
        patch("agent_cli.main._setup_mcp", side_effect=_stop),
    ):
        r = CliRunner().invoke(
            m.app,
            ["web", "-p", "openai", "--base-url", URL, "-m", "reg-model"],
            input="",
        )
    assert reached == [1], r.output
    assert isinstance(r.exception, _Stop), r.output
    assert probes == []


# ── 등록 안 된 모델 + 서버 꺼짐 → 그 자리에서 2 ───────────────


@pytest.mark.parametrize("quiet", [False, True])
def test_unregistered_model_and_server_down_exits_2_without_probes(
    monkeypatch, out, probes, asked, quiet
):
    _models_get(monkeypatch, exc=_refused())
    with pytest.raises(typer.Exit) as ei:
        m._setup_provider("openai", "new-model", URL, "", quiet=quiet, explicit=True)
    assert ei.value.exit_code == 2
    assert probes == [] and asked == []
    text = out.export_text()
    assert URL in text
    assert "new-model" in text and "not registered" in text
    assert "--base-url" in text


def test_long_url_is_one_chunk_at_80_columns(monkeypatch, out, probes):
    long_url = "http://" + "a" * 70 + ".example.internal:8000/v1"
    assert len(long_url) > 80
    _models_get(monkeypatch, exc=_refused())
    with pytest.raises(typer.Exit):
        m._setup_provider("openai", "new-model", long_url, "", explicit=True)
    assert long_url in out.export_text()


def test_dns_and_tls_failures_are_unreachable(monkeypatch):
    def _no_dns(*a, **k):
        raise socket.gaierror(socket.EAI_NONAME, "nodename nor servname provided")

    with monkeypatch.context() as mp:
        mp.setattr(socket, "getaddrinfo", _no_dns)
        dns = _real_exc("http://model-host.invalid:8000/v1/models")
    for exc, reason in (
        (dns, "host not found"),
        (_tls_error(), "TLS error: certificate verify failed: unable to get"),
    ):
        model_check._cache.clear()
        _models_get(monkeypatch, exc=exc)
        listing = model_check.list_models(URL, Credentials())
        assert listing.unreachable is True and listing.available is False
        assert listing.reason.startswith(reason)


# ── 그 밖의 "묻지 못함" 은 종전 그대로 ────────────────────────


@pytest.mark.parametrize(
    "setup",
    [
        {"exc": requests.exceptions.ReadTimeout("read timed out")},
        {"exc": requests.exceptions.ConnectTimeout("connect timed out")},
        {"status": 404},
        {"status": 500},
        {"payload": {"data": []}},
    ],
    ids=["read-timeout", "connect-timeout", "http-404", "http-500", "empty-list"],
)
def test_other_unavailable_listings_keep_the_old_path(monkeypatch, out, probes, setup):
    _models_get(monkeypatch, **setup)
    listing = model_check.list_models(URL, Credentials())
    assert listing.available is False and listing.unreachable is False
    model_check._cache.clear()
    monkeypatch.setenv("AGENT_CLI_NO_INPUT", "1")
    result = m._setup_provider("openai", "new-model", URL, "", explicit=True)
    assert result[2] == "new-model"
    assert len(probes) == 1, "미등록 모델은 종전처럼 탐지로 간다"
    assert "Could not read /v1/models" in out.export_text()


# ── 탐지 실패 뒤의 질문은 터미널에서만 ─────────────────────────


def test_capability_question_is_not_asked_noninteractively(monkeypatch, probes, asked):
    _models_get(monkeypatch, status=404)
    monkeypatch.setattr("agent_cli.model_check.is_interactive", lambda: False)
    m._setup_provider("openai", "new-model", URL, "", explicit=True)
    assert asked == []


def test_capability_question_is_asked_interactively(monkeypatch, probes, asked):
    _models_get(monkeypatch, status=404)
    monkeypatch.setattr("agent_cli.model_check.is_interactive", lambda: True)
    m._setup_provider("openai", "new-model", URL, "", explicit=True)
    assert asked == ["new-model"]


def test_capability_question_default_is_the_value_actually_used(monkeypatch):
    """빈 입력 = 묻지 않았을 때 쓰이는 `DEFAULT_CAPABILITIES` 의 창 (종전 질문은 [4096])."""
    prompts = []
    answers = iter(["", "n", "n", "json_fc"])

    class _R:
        def prompt_user(self, text, multiline=False):
            prompts.append(text)
            return next(answers)

    saved = {}
    monkeypatch.setattr(m, "get_renderer", lambda: _R())
    monkeypatch.setattr(
        "agent_cli.config.save_model_entry", lambda name, e: saved.update(e)
    )
    caps = m._prompt_model_capabilities("new-model")
    window = _caps.DEFAULT_CAPABILITIES.context_window
    assert f"[{window}]" in prompts[0]
    assert caps.context_window == window
    assert saved["context_window"] == window


# ── 분류 헬퍼 (부트·LLM 실패 줄 공용) ───────────────────────────


def test_classify_closed_port_is_refused():
    assert classify_connection_error(_refused()) == ("connection refused", True)


def test_classify_server_that_closes_after_accept_is_not_unreachable():
    """서버는 있다 — 받고 바로 닫았다(omlx 가 호출 도중 끊는 것과 같은 모양)."""
    with _listener("close") as url:
        exc = _real_exc(f"{url}/models")
    assert isinstance(exc, requests.exceptions.ConnectionError)
    assert classify_connection_error(exc) == ("connection closed by the server", False)


def test_classify_dns_failure_without_real_dns(monkeypatch):
    def _no_dns(*a, **k):
        raise socket.gaierror(socket.EAI_NONAME, "nodename nor servname provided")

    monkeypatch.setattr(socket, "getaddrinfo", _no_dns)
    exc = _real_exc("http://model-host.invalid:8000/v1/models")
    assert classify_connection_error(exc) == ("host not found", True)


def test_classify_read_timeout():
    with _listener("silent") as url:
        exc = _real_exc(f"{url}/models", timeout=0.3)
    assert isinstance(exc, requests.exceptions.ReadTimeout)
    assert classify_connection_error(exc) == ("no response (timeout)", False)


def test_classify_proxy_error():
    proxy = f"http://127.0.0.1:{_closed_port()}"
    exc = _real_exc("http://example.invalid/v1/models", proxies={"http": proxy})
    assert isinstance(exc, requests.exceptions.ProxyError)
    assert classify_connection_error(exc) == ("proxy error", False)


def test_classify_tls_keeps_the_reason():
    reason, unreachable = classify_connection_error(_tls_error())
    assert reason == (
        "TLS error: certificate verify failed: unable to get local issuer certificate"
    )
    assert unreachable is True


def test_classify_other_errors_are_none():
    assert classify_connection_error(ValueError("x")) is None
    assert (
        classify_connection_error(requests.exceptions.ChunkedEncodingError("x")) is None
    )


def test_server_that_closes_after_accept_takes_the_old_boot_path(
    monkeypatch, out, probes, no_proxy_env
):
    """못 닿음이 아니므로 미등록 모델도 2 로 끝나지 않는다 — 종전처럼 경고 뒤 탐지."""
    monkeypatch.setenv("AGENT_CLI_NO_INPUT", "1")
    with _listener("close") as url:
        result = m._setup_provider("openai", "new-model", url, "", explicit=True)
    assert result[2] == "new-model"
    assert len(probes) == 1
    text = " ".join(out.export_text().split())
    assert "Could not read /v1/models" in text
    assert "(connection closed by the server)" in text
    assert "Cannot connect" not in text


def test_closed_port_boot_line_names_the_fact(monkeypatch, out, probes, no_proxy_env):
    url = f"http://127.0.0.1:{_closed_port()}/v1"
    with pytest.raises(typer.Exit) as ei:
        m._setup_provider("openai", "new-model", url, "", explicit=True)
    assert ei.value.exit_code == 2
    assert f"✗ Cannot connect to the server at {url} (connection refused)." in (
        out.export_text()
    )
    assert probes == []


@pytest.mark.parametrize(
    ("make", "shown"),
    [
        (_refused, "ConnectionError: connection refused"),
        (
            _tls_error,
            (
                "SSLError: TLS error: certificate verify failed: unable to get local "
                "issuer certificate"
            ),
        ),
    ],
    ids=["refused", "tls"],
)
def test_llm_failure_line_hides_urllib3_repr(tmp_path, monkeypatch, make, shown):
    """실제 `run` 을 가짜 provider 로 — 최종 실패 줄에 객체 repr 가 없고 사유는 남는다."""
    from typer.testing import CliRunner

    from agent_cli.providers.capabilities import DEFAULT_CAPABILITIES
    from tests.binding_support import make_binding

    exc = make()

    class _Down:
        def call(self, *a, **k):
            raise exc

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    binding = make_binding(provider=_Down(), capabilities=DEFAULT_CAPABILITIES)
    with (
        patch("agent_cli.main._bootstrap_provider", return_value=binding),
        patch("agent_cli.main._setup_mcp", return_value=(None, {})),
    ):
        r = CliRunner().invoke(m.app, ["run", "hi", "--max-turns", "1"])
    assert r.exit_code == 1, r.output
    flat = " ".join(r.output.split())
    assert "LLM call failed" in flat
    assert shown in flat
    assert "object at 0x" not in r.output
    assert "HTTPConnectionPool" not in r.output
