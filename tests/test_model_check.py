"""부팅 시 모델 검증 — 삼치 구분이 계약이다 (v9.5.0, docs/model-resolution).

고장의 원인은 "기본값이 있다"가 아니라 **"그 이름을 아무도 확인하지 않는다"**
였다. 서버에서 없어진 모델이 `config.json` 에 남아 첫 LLM 호출까지 살아남고,
상주 에이전트에서는 대화 한복판의 거부 카드로 보였다(사용자 제보).

여기서 고정하는 것은 **세 갈래가 서로 섞이지 않는다**는 것:

    목록 받음 + 이름 있음   → 통과
    목록 받음 + 이름 없음   → ModelNotFound   (고칠 방법을 알려줄 수 있다)
    목록 못 받음            → **통과**        (확인 불가를 틀림으로 단정 금지)

마지막 줄이 이 파일의 존재 이유다. 구 `setup._list_models` 는 실패를 전부
`[]` 로 뭉갰고, 그 모양을 그대로 쓰면 오프라인·`/models` 미지원 서버에서
"모델이 하나도 없다"로 읽혀 부팅이 막힌다 — 원래 고장보다 나쁜 회귀다.
"""

from __future__ import annotations

import pytest

from agent_cli import model_check
from agent_cli.model_check import (
    ModelListing,
    ModelNotFound,
    NoModelSelected,
    list_models,
    verify_model,
)

MODELS = ("Qwen3.6-35B-A3B-8bit", "Qwen3.8-Flash-Next-oQ4e-mtp")
URL = "http://127.0.0.1:8000/v1"


@pytest.fixture(autouse=True)
def _clear_cache():
    """프로세스 캐시는 테스트 간에 새어서는 안 된다."""
    model_check._cache.clear()
    yield
    model_check._cache.clear()


class _Resp:
    def __init__(self, status=200, payload=None, boom=False):
        self.status_code = status
        self._payload = payload if payload is not None else {}
        self._boom = boom

    def json(self):
        if self._boom:
            raise ValueError("not json")
        return self._payload


def _ok_payload(ids=MODELS):
    return {"data": [{"id": i} for i in ids]}


def _patch(monkeypatch, resp=None, exc=None):
    calls = []

    def fake_get(url, headers=None, timeout=None):
        calls.append({"url": url, "headers": headers or {}})
        if exc is not None:
            raise exc
        return resp

    import requests

    monkeypatch.setattr(requests, "get", fake_get)
    return calls


class TestListing:
    def test_success_returns_ids_and_available(self, monkeypatch):
        _patch(monkeypatch, _Resp(200, _ok_payload()))
        r = list_models(URL)
        assert r.available is True
        assert r.models == MODELS

    def test_network_failure_is_unavailable_not_empty(self, monkeypatch):
        """연결 실패를 "모델 0개"로 읽으면 오프라인에서 부팅이 막힌다."""
        _patch(monkeypatch, exc=OSError("connection refused"))
        r = list_models(URL)
        assert r.available is False
        assert r.models == ()
        assert "connection refused" in r.reason

    def test_404_models_endpoint_is_unavailable(self, monkeypatch):
        """`/models` 미지원 서버 — 이름이 틀린 게 아니라 못 물어본 것이다."""
        _patch(monkeypatch, _Resp(404))
        r = list_models(URL)
        assert r.available is False
        assert "404" in r.reason

    def test_unparseable_body_is_unavailable(self, monkeypatch):
        _patch(monkeypatch, _Resp(200, boom=True))
        assert list_models(URL).available is False

    def test_empty_list_is_unavailable_not_a_verdict(self, monkeypatch):
        """200 인데 빈 목록: 형식이 다른 서버일 수 있어 단정하지 않는다 —
        여기서 "없다"고 단정하면 모든 모델이 거짓 거절된다."""
        _patch(monkeypatch, _Resp(200, {"data": []}))
        assert list_models(URL).available is False

    def test_result_is_cached_per_endpoint(self, monkeypatch):
        calls = _patch(monkeypatch, _Resp(200, _ok_payload()))
        list_models(URL)
        list_models(URL)
        assert len(calls) == 1, "같은 엔드포인트를 두 번 조회했다"

    def test_anthropic_uses_its_own_auth_headers(self, monkeypatch):
        calls = _patch(monkeypatch, _Resp(200, _ok_payload()))
        list_models("https://api.anthropic.com/v1", "k", "anthropic")
        h = calls[0]["headers"]
        assert h.get("x-api-key") == "k"
        assert "anthropic-version" in h
        assert "Authorization" not in h

    def test_openai_uses_bearer(self, monkeypatch):
        calls = _patch(monkeypatch, _Resp(200, _ok_payload()))
        list_models(URL, "k", "openai")
        assert calls[0]["headers"].get("Authorization") == "Bearer k"


class TestVerify:
    def test_known_model_passes(self, monkeypatch):
        _patch(monkeypatch, _Resp(200, _ok_payload()))
        assert verify_model(MODELS[0], URL).available is True

    def test_missing_model_raises_with_fix_information(self, monkeypatch):
        """메시지가 **고칠 수 있게** 만들어야 한다 — 이름·목록·출처."""
        _patch(monkeypatch, _Resp(200, _ok_payload()))
        with pytest.raises(ModelNotFound) as ei:
            verify_model("Qwen-3.8-Flash-Next-Uncensored-MLX-MXFP4", URL, origin="cfg")
        err = ei.value
        assert err.model.startswith("Qwen-3.8-Flash")
        assert err.listing.models == MODELS
        assert err.origin == "cfg"

    def test_empty_model_raises_no_model_selected(self, monkeypatch):
        """추측 폴백을 없앴으므로 "안 골랐다"가 그대로 드러나야 한다."""
        _patch(monkeypatch, _Resp(200, _ok_payload()))
        with pytest.raises(NoModelSelected):
            verify_model("", URL)

    def test_unlistable_server_does_not_reject_the_name(self, monkeypatch):
        """★ 핵심 계약: 확인할 수 없는 것을 틀렸다고 하지 않는다."""
        _patch(monkeypatch, exc=OSError("offline"))
        r = verify_model("whatever-model", URL)
        assert r.available is False  # 호출부가 이걸 보고 경고만 낸다

    def test_missing_name_still_raises_even_when_unlistable(self, monkeypatch):
        """이름이 아예 없는 것은 조회와 무관하게 확정적 오류다."""
        _patch(monkeypatch, exc=OSError("offline"))
        with pytest.raises(NoModelSelected):
            verify_model("", URL)


class TestSuggest:
    def test_typo_gets_a_near_name(self):
        r = ModelListing(MODELS, True)
        assert r.suggest("Qwen3.6-35B-A3B-8bti") == "Qwen3.6-35B-A3B-8bit"

    def test_unrelated_name_gets_nothing(self):
        assert ModelListing(MODELS, True).suggest("gpt-4o") == ""

    def test_empty_name_gets_nothing(self):
        assert ModelListing(MODELS, True).suggest("") == ""


class TestInteractiveGate:
    """보드가 띄운 인스턴스는 stdin 이 파이프다 — 물어보면 **멈춘다**
    (v9.1.0 에서 `agent-cli mcp` 가 파이프 뒤에서 걸린 것과 같은 함정)."""

    def test_env_flag_forces_non_interactive(self, monkeypatch):
        monkeypatch.setenv("AGENT_CLI_NO_INPUT", "1")
        assert model_check.is_interactive() is False

    def test_non_tty_stdin_is_not_interactive(self, monkeypatch):
        monkeypatch.delenv("AGENT_CLI_NO_INPUT", raising=False)

        class _Pipe:
            def isatty(self):
                return False

        monkeypatch.setattr("sys.stdin", _Pipe())
        assert model_check.is_interactive() is False
