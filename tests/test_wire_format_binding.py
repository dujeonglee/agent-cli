"""Wire-format 모델별 바인딩 (Phase 1) — docs/multi-wire-format/DESIGN.md §8.

해석 체인(명시 > resume 메타 > 모델 바인딩 > DEFAULT), models.json
``wire_format`` 필드 조회, 서브에이전트 effective-model 바인딩,
AgentLoop ctx-우선 폴백(G2), 부트스트랩 배선(G1)을 고정한다.
"""

import json
from unittest.mock import MagicMock

import pytest

import agent_cli.config as _config
from agent_cli.providers.capabilities import ModelCapabilities
from agent_cli.wire_formats import (
    get as get_wf,
)
from agent_cli.wire_formats import (
    resolve_wire_format,
    wire_format_for_model,
)


@pytest.fixture
def models_file(tmp_path, monkeypatch):
    """바인딩이 있는/없는/깨진 모델 엔트리를 가진 임시 models.json."""
    target = tmp_path / "models.json"
    data = {
        "models": {
            "bound-md": {"context_window": 8192, "wire_format": "json_fc"},
            "bound-xml": {"context_window": 8192, "wire_format": "xml_fc"},
            "unbound": {"context_window": 8192},
            "bad-bound": {"context_window": 8192, "wire_format": "no_such_format"},
        }
    }
    target.write_text(json.dumps(data), encoding="utf-8")
    monkeypatch.setattr(_config, "_SEARCH_PATHS", [target])
    monkeypatch.setattr(_config, "_cached_registry", None)


@pytest.fixture
def caps():
    return ModelCapabilities(
        context_window=32768,
        max_output_tokens=4096,
        supports_thinking=False,
        thinking_budget=0,
    )


# ── wire_format_for_model — models.json 바인딩 조회 ──────────


class TestWireFormatForModel:
    def test_binding_returned(self, models_file):
        assert wire_format_for_model("bound-md") == "json_fc"

    def test_entry_without_field_returns_none(self, models_file):
        assert wire_format_for_model("unbound") is None

    def test_unknown_model_returns_none(self, models_file):
        assert wire_format_for_model("nonexistent-model") is None

    def test_empty_model_returns_none(self, models_file):
        assert wire_format_for_model("") is None

    def test_non_string_binding_ignored(self, tmp_path, monkeypatch):
        # 손상된 엔트리 (wire_format 이 문자열 아님) — 조용히 None
        target = tmp_path / "models.json"
        target.write_text(
            json.dumps({"models": {"weird": {"wire_format": 42}}}), encoding="utf-8"
        )
        monkeypatch.setattr(_config, "_SEARCH_PATHS", [target])
        monkeypatch.setattr(_config, "_cached_registry", None)
        assert wire_format_for_model("weird") is None


# ── resolve_wire_format — 해석 체인 ──────────────────────────


class TestResolveWireFormat:
    def test_explicit_beats_meta_and_binding(self, models_file):
        wf = resolve_wire_format(
            explicit="xml_fc", session_format="json_fc", model="bound-md"
        )
        assert wf.name == "xml_fc"

    def test_meta_beats_binding(self, models_file):
        wf = resolve_wire_format(
            explicit=None, session_format="xml_fc", model="bound-md"
        )
        assert wf.name == "xml_fc"

    def test_binding_used_when_no_explicit_no_meta(self, models_file):
        wf = resolve_wire_format(explicit=None, session_format=None, model="bound-md")
        assert wf.name == "json_fc"

    def test_all_absent_falls_to_default(self, models_file):
        wf = resolve_wire_format(explicit=None, session_format=None, model="unbound")
        assert wf is get_wf(None)  # DEFAULT_WIRE_FORMAT (suite pin 존중)

    def test_no_model_falls_to_default(self, models_file):
        wf = resolve_wire_format(explicit=None, session_format=None, model="")
        assert wf is get_wf(None)

    def test_unknown_explicit_raises(self, models_file):
        with pytest.raises(KeyError):
            resolve_wire_format(explicit="nope", session_format=None, model="")

    def test_unknown_meta_raises(self, models_file):
        with pytest.raises(KeyError):
            resolve_wire_format(explicit=None, session_format="nope", model="")

    def test_unknown_binding_raises(self, models_file):
        # D2: 조용한 폴백 금지 — 바인딩 오타는 fail-fast
        with pytest.raises(KeyError):
            resolve_wire_format(explicit=None, session_format=None, model="bad-bound")


# ── create_subagent_ctx — effective model 바인딩 (G3) ────────


class TestSubagentBinding:
    def _parent(self, tmp_path, name="xml_fc"):
        from agent_cli.context.manager import ContextManager

        return ContextManager(
            tmp_path / "parent", max_context_tokens=1000, wire_format=get_wf(name)
        )

    def test_bound_model_overrides_parent_format(self, tmp_path, models_file):
        from agent_cli.subagent.runner import create_subagent_ctx

        parent = self._parent(tmp_path, "xml_fc")
        ctx, error = create_subagent_ctx(
            "none", parent, tmp_path / "sub", model="bound-md"
        )
        assert error == ""
        assert ctx.wire_format.name == "json_fc"

    def test_unbound_model_inherits_parent(self, tmp_path, models_file):
        from agent_cli.subagent.runner import create_subagent_ctx

        parent = self._parent(tmp_path, "xml_fc")
        ctx, error = create_subagent_ctx(
            "none", parent, tmp_path / "sub", model="unbound"
        )
        assert error == ""
        assert ctx.wire_format is parent.wire_format

    def test_no_model_inherits_parent(self, tmp_path, models_file):
        # 기존 동작 회귀 가드 — model 미전달 = 종전 부모 상속
        from agent_cli.subagent.runner import create_subagent_ctx

        parent = self._parent(tmp_path, "xml_fc")
        ctx, error = create_subagent_ctx("none", parent, tmp_path / "sub")
        assert error == ""
        assert ctx.wire_format is parent.wire_format

    def test_bad_binding_rejects_spawn(self, tmp_path, models_file):
        # D2: unknown 바인딩 이름 → spawn 거부 (세션은 안 죽음)
        from agent_cli.subagent.runner import create_subagent_ctx

        parent = self._parent(tmp_path, "xml_fc")
        ctx, error = create_subagent_ctx(
            "none", parent, tmp_path / "sub", model="bad-bound"
        )
        assert ctx is None
        assert "no_such_format" in error

    def test_bound_model_without_parent(self, tmp_path, models_file):
        from agent_cli.subagent.runner import create_subagent_ctx

        ctx, error = create_subagent_ctx(
            "none", None, tmp_path / "sub", model="bound-md"
        )
        assert error == ""
        assert ctx.wire_format.name == "json_fc"

    def test_fork_mode_applies_binding(self, tmp_path, models_file):
        from agent_cli.subagent.runner import create_subagent_ctx

        parent = self._parent(tmp_path, "xml_fc")
        parent.add({"role": "user", "content": "hi"})
        ctx, error = create_subagent_ctx(
            "fork", parent, tmp_path / "sub", model="bound-md"
        )
        assert error == ""
        assert ctx.wire_format.name == "json_fc"


# ── AgentLoop ctx-우선 폴백 (G2 — split-brain 수리) ──────────


class TestLoopCtxFallback:
    def test_none_wire_format_falls_to_ctx(self, tmp_path, caps):
        # RED (G2): 현재는 ctx 가 아니라 전역 기본으로 폴백해 split-brain
        from agent_cli.context.manager import ContextManager
        from agent_cli.loop.core import AgentLoop

        ctx = ContextManager(
            tmp_path / "s", max_context_tokens=1000, wire_format=get_wf("json_fc")
        )
        loop = AgentLoop(
            query="q", provider=MagicMock(), capabilities=caps, model="m", ctx=ctx
        )
        assert loop.wire_format is ctx.wire_format

    def test_explicit_wire_format_still_wins_over_ctx(self, tmp_path, caps):
        from agent_cli.context.manager import ContextManager
        from agent_cli.loop.core import AgentLoop

        ctx = ContextManager(
            tmp_path / "s", max_context_tokens=1000, wire_format=get_wf("json_fc")
        )
        loop = AgentLoop(
            query="q",
            provider=MagicMock(),
            capabilities=caps,
            model="m",
            ctx=ctx,
            wire_format=get_wf("xml_fc"),
        )
        assert loop.wire_format.name == "xml_fc"

    def test_no_ctx_falls_to_default(self, caps):
        from agent_cli.loop.core import AgentLoop

        loop = AgentLoop(query="q", provider=MagicMock(), capabilities=caps, model="m")
        assert loop.wire_format is get_wf(None)


# ── _bootstrap_provider 배선 (G1 — resume 메타 존중) ─────────


class TestBootstrapWiring:
    def _fake_setup(self, resolved_model="unbound"):
        provider = MagicMock()
        caps = ModelCapabilities(
            context_window=32768,
            max_output_tokens=4096,
            supports_thinking=False,
            thinking_budget=0,
        )
        return (provider, caps, resolved_model, "http://x", "", "openai")

    def test_resume_meta_respected_without_flag(self, monkeypatch, models_file):
        # G1: react 세션을 플래그 없이 resume → react 유지
        import agent_cli.main as main_mod

        monkeypatch.setattr(
            main_mod, "_setup_provider", lambda *a, **k: self._fake_setup()
        )
        boot = main_mod._bootstrap_provider(
            "openai", None, None, None, None, 0, session_format="xml_fc"
        )
        assert boot.wire_format.name == "xml_fc"

    def test_explicit_flag_beats_resume_meta(self, monkeypatch, models_file):
        import agent_cli.main as main_mod

        monkeypatch.setattr(
            main_mod, "_setup_provider", lambda *a, **k: self._fake_setup()
        )
        boot = main_mod._bootstrap_provider(
            "openai", None, None, None, "json_fc", 0, session_format="xml_fc"
        )
        assert boot.wire_format.name == "json_fc"

    def test_model_binding_used_for_new_session(self, monkeypatch, models_file):
        import agent_cli.main as main_mod

        monkeypatch.setattr(
            main_mod,
            "_setup_provider",
            lambda *a, **k: self._fake_setup(resolved_model="bound-md"),
        )
        boot = main_mod._bootstrap_provider(
            "openai", None, None, None, None, 0, session_format=None
        )
        assert boot.wire_format.name == "json_fc"

    def test_no_sources_falls_to_default(self, monkeypatch, models_file):
        import agent_cli.main as main_mod

        monkeypatch.setattr(
            main_mod, "_setup_provider", lambda *a, **k: self._fake_setup()
        )
        boot = main_mod._bootstrap_provider(
            "openai", None, None, None, None, 0, session_format=None
        )
        assert boot.wire_format is get_wf(None)

    def test_cli_flag_default_is_none(self):
        # D3: --response-format default None — 명시성 감지의 전제
        import inspect

        import agent_cli.main as main_mod

        for cmd in (main_mod.run, main_mod.web):
            param = inspect.signature(cmd).parameters["response_format"]
            # typer.Option 객체의 default 속성이 None 이어야 한다
            assert param.default.default is None, cmd.__name__


class TestResumeWireFormatHelper:
    """★감사 #3 (v7.11.4): 대화형-resume 재해석이 typer 본문 인라인이라
    무검증이었음 — 헬퍼 추출 후 고정. G1(silent format switch 금지):
    기록 포맷이 미등록 이름이면 조용한 default 폴백이 아니라 Exit(2)."""

    def _session(self, fmt):
        from agent_cli.context.session import create_session

        s = create_session()
        s.response_format = fmt
        return s

    def _current(self):
        from agent_cli.wire_formats import get as get_wf

        return get_wf("xml_fc")

    def test_explicit_flag_wins_no_reinterpret(self):
        from agent_cli.main import resume_wire_format

        cur = self._current()
        out = resume_wire_format(self._session("json_fc"), cur, "xml_fc")
        assert out is cur  # 명시 플래그 = 체인 1순위

    def test_recorded_format_reinterpreted(self):
        from agent_cli.main import resume_wire_format

        out = resume_wire_format(self._session("json_fc"), self._current(), None)
        assert out.name == "json_fc"

    def test_same_format_passthrough(self):
        from agent_cli.main import resume_wire_format

        cur = self._current()
        assert resume_wire_format(self._session("xml_fc"), cur, None) is cur

    def test_unknown_recorded_format_exits_2(self):
        import click
        import pytest
        import typer

        from agent_cli.main import resume_wire_format

        with pytest.raises((typer.Exit, click.exceptions.Exit, SystemExit)) as ei:
            resume_wire_format(
                self._session("md_array"), self._current(), None
            )  # v6.0.0 에서 rename 된 이름 — 조용한 폴백 금지
        code = getattr(ei.value, "exit_code", getattr(ei.value, "code", None))
        assert code == 2

    def test_writeback_persists_to_meta(self, tmp_path, monkeypatch):
        """run/web 의 write-back(session.response_format=해석값; save_meta)
        이 실제 meta 파일에 남는지 — 연속 resume 포맷 drift 방지."""
        import json

        import agent_cli.context.session as sess_mod
        from agent_cli.context.session import create_session, save_meta

        monkeypatch.setattr(sess_mod, "_SESSIONS_BASE", tmp_path)
        s = create_session()
        s.response_format = "xml_fc"
        save_meta(s)
        line = (
            (tmp_path / "sessions" / s.session_id / "session.jsonl")
            .read_text()
            .splitlines()[0]
        )
        assert json.loads(line)["_meta"]["response_format"] == "xml_fc"
