"""방언 모델별 바인딩 — docs/dialects/DESIGN.md §8, v10.3.0 체인.

해석 체인(``--dialect`` 강제 > 모델 바인딩 > 에러 — 기본값·세션 메타·
부모 상속 없음), models.json ``dialect`` 필드 조회, 서브에이전트
effective-model 바인딩, AgentLoop ctx-우선 폴백(G2), 부트스트랩 배선을
고정한다. 스위트 전역 픽스처가 json_fc 를 강제하므로 체인 테스트는
``_override`` 를 None 으로 되돌린다.
"""

import json
from unittest.mock import MagicMock

import pytest

import agent_cli.config as _config
import agent_cli.dialects as _dialects
from agent_cli.dialects import (
    DialectUnbound,
    dialect_for_model,
    resolve_dialect,
)
from agent_cli.dialects import (
    get as get_wf,
)
from agent_cli.providers.capabilities import ModelCapabilities
from tests.loop_ports import TEST_PORTS


@pytest.fixture
def models_file(tmp_path, monkeypatch):
    """바인딩이 있는/없는/깨진 모델 엔트리를 가진 임시 models.json."""
    target = tmp_path / "models.json"
    data = {
        "models": {
            "bound-md": {"context_window": 8192, "dialect": "json_fc"},
            "bound-xml": {"context_window": 8192, "dialect": "xml_fc"},
            "unbound": {"context_window": 8192},
            "bad-bound": {"context_window": 8192, "dialect": "no_such_format"},
        }
    }
    target.write_text(json.dumps(data), encoding="utf-8")
    monkeypatch.setattr(_config, "_SEARCH_PATHS", [target])
    monkeypatch.setattr(_config, "_cached_registry", None)
    monkeypatch.setattr(_dialects, "_override", None)


@pytest.fixture
def caps():
    return ModelCapabilities(
        context_window=32768,
        max_output_tokens=4096,
        supports_thinking=False,
    )


# ── dialect_for_model — models.json 바인딩 조회 ──────────


class TestDialectForModel:
    def test_binding_returned(self, models_file):
        assert dialect_for_model("bound-md") == "json_fc"

    def test_entry_without_field_returns_none(self, models_file):
        assert dialect_for_model("unbound") is None

    def test_unknown_model_returns_none(self, models_file):
        assert dialect_for_model("nonexistent-model") is None

    def test_empty_model_returns_none(self, models_file):
        assert dialect_for_model("") is None

    def test_non_string_binding_ignored(self, tmp_path, monkeypatch):
        # 손상된 엔트리 (dialect 이 문자열 아님) — 조용히 None
        target = tmp_path / "models.json"
        target.write_text(
            json.dumps({"models": {"weird": {"dialect": 42}}}), encoding="utf-8"
        )
        monkeypatch.setattr(_config, "_SEARCH_PATHS", [target])
        monkeypatch.setattr(_config, "_cached_registry", None)
        assert dialect_for_model("weird") is None


# ── resolve_dialect — 해석 체인 ──────────────────────────


class TestResolveDialect:
    def test_override_beats_binding(self, models_file):
        _dialects.set_dialect_override("xml_fc")
        assert resolve_dialect("bound-md").name == "xml_fc"

    def test_binding_used_without_override(self, models_file):
        assert resolve_dialect("bound-md").name == "json_fc"
        assert resolve_dialect("bound-xml").name == "xml_fc"

    def test_unbound_model_raises(self, models_file):
        # v10.3.0: 기본값 없음 — 묶이지 않은 모델은 사용자가 묶어야 한다
        with pytest.raises(DialectUnbound) as ei:
            resolve_dialect("unbound")
        msg = ei.value.args[0]
        assert "unbound" in msg and "models.json" in msg and "--dialect" in msg

    def test_no_model_raises(self, models_file):
        with pytest.raises(DialectUnbound):
            resolve_dialect("")

    def test_override_rescues_unbound_model(self, models_file):
        _dialects.set_dialect_override("json_fc")
        assert resolve_dialect("unbound").name == "json_fc"

    def test_unknown_override_raises(self, models_file):
        _dialects.set_dialect_override("nope")
        with pytest.raises(KeyError):
            resolve_dialect("bound-md")

    def test_unknown_binding_raises(self, models_file):
        # D2: 조용한 폴백 금지 — 바인딩 오타는 fail-fast
        with pytest.raises(KeyError):
            resolve_dialect("bad-bound")


# ── create_subagent_ctx — main 과 같은 체인 ─────────────────


class TestSubagentBinding:
    def _parent(self, tmp_path, name="xml_fc"):
        from agent_cli.context.manager import ContextManager

        return ContextManager(
            tmp_path / "parent", max_context_tokens=1000, dialect=get_wf(name)
        )

    def test_bound_model_overrides_parent_format(self, tmp_path, models_file):
        from agent_cli.subagent.runner import create_subagent_ctx

        parent = self._parent(tmp_path, "xml_fc")
        ctx, error = create_subagent_ctx(
            "none", parent, tmp_path / "sub", model="bound-md"
        )
        assert error == ""
        assert ctx.dialect.name == "json_fc"

    def test_unbound_model_rejects_spawn(self, tmp_path, models_file):
        # v10.3.0: 부모 상속 없음 — 묶이지 않은 모델은 spawn 거부
        from agent_cli.subagent.runner import create_subagent_ctx

        parent = self._parent(tmp_path, "xml_fc")
        ctx, error = create_subagent_ctx(
            "none", parent, tmp_path / "sub", model="unbound"
        )
        assert ctx is None
        assert "No dialect for model 'unbound'" in error

    def test_no_model_rejects_spawn(self, tmp_path, models_file):
        from agent_cli.subagent.runner import create_subagent_ctx

        parent = self._parent(tmp_path, "xml_fc")
        ctx, error = create_subagent_ctx("none", parent, tmp_path / "sub")
        assert ctx is None
        assert "No dialect" in error

    def test_override_forces_subagent_too(self, tmp_path, models_file):
        # --dialect 는 세션 전체 강제 — 서브 모델의 바인딩도 덮는다
        from agent_cli.subagent.runner import create_subagent_ctx

        _dialects.set_dialect_override("xml_fc")
        parent = self._parent(tmp_path, "xml_fc")
        ctx, error = create_subagent_ctx(
            "none", parent, tmp_path / "sub", model="bound-md"
        )
        assert error == ""
        assert ctx.dialect is parent.dialect

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
        assert ctx.dialect.name == "json_fc"

    def test_fork_mode_applies_binding(self, tmp_path, models_file):
        from agent_cli.subagent.runner import create_subagent_ctx

        parent = self._parent(tmp_path, "xml_fc")
        parent.add({"role": "user", "content": "hi"})
        ctx, error = create_subagent_ctx(
            "fork", parent, tmp_path / "sub", model="bound-md"
        )
        assert error == ""
        assert ctx.dialect.name == "json_fc"


# ── AgentLoop ctx-우선 폴백 (G2 — split-brain 수리) ──────────


class TestLoopCtxFallback:
    def test_none_dialect_falls_to_ctx(self, tmp_path, caps):
        # RED (G2): 현재는 ctx 가 아니라 전역 기본으로 폴백해 split-brain
        from agent_cli.context.manager import ContextManager
        from agent_cli.loop.core import AgentLoop

        ctx = ContextManager(
            tmp_path / "s", max_context_tokens=1000, dialect=get_wf("json_fc")
        )
        loop = AgentLoop(
            ports=TEST_PORTS,
            query="q",
            provider=MagicMock(),
            capabilities=caps,
            model="m",
            ctx=ctx,
        )
        assert loop.dialect is ctx.dialect

    def test_explicit_dialect_still_wins_over_ctx(self, tmp_path, caps):
        from agent_cli.context.manager import ContextManager
        from agent_cli.loop.core import AgentLoop

        ctx = ContextManager(
            tmp_path / "s", max_context_tokens=1000, dialect=get_wf("json_fc")
        )
        loop = AgentLoop(
            ports=TEST_PORTS,
            query="q",
            provider=MagicMock(),
            capabilities=caps,
            model="m",
            ctx=ctx,
            dialect=get_wf("xml_fc"),
        )
        assert loop.dialect.name == "xml_fc"

    def test_no_ctx_falls_to_default(self, caps):
        from agent_cli.loop.core import AgentLoop

        loop = AgentLoop(
            ports=TEST_PORTS,
            query="q",
            provider=MagicMock(),
            capabilities=caps,
            model="m",
        )
        assert loop.dialect is get_wf(None)


# ── _bootstrap_provider 배선 ─────────────────────────────


class TestBootstrapWiring:
    def _fake_setup(self, resolved_model="unbound"):
        provider = MagicMock()
        caps = ModelCapabilities(
            context_window=32768,
            max_output_tokens=4096,
            supports_thinking=False,
        )
        return (provider, caps, resolved_model, "http://x", "", "openai")

    def _exit_code(self, ei):
        return getattr(ei.value, "exit_code", getattr(ei.value, "code", None))

    def test_model_binding_used(self, monkeypatch, models_file):
        import agent_cli.main as main_mod

        monkeypatch.setattr(
            main_mod,
            "_setup_provider",
            lambda *a, **k: self._fake_setup(resolved_model="bound-xml"),
        )
        boot = main_mod._bootstrap_provider("openai", None, None, None, None, 0)
        assert boot.dialect.name == "xml_fc"

    def test_explicit_flag_beats_binding_and_is_process_wide(
        self, monkeypatch, models_file
    ):
        import agent_cli.main as main_mod

        monkeypatch.setattr(
            main_mod,
            "_setup_provider",
            lambda *a, **k: self._fake_setup(resolved_model="bound-xml"),
        )
        boot = main_mod._bootstrap_provider("openai", None, None, None, "json_fc", 0)
        assert boot.dialect.name == "json_fc"
        # 부트가 강제를 프로세스에 심었다 — 서브에이전트도 같은 값을 본다
        assert resolve_dialect("bound-xml").name == "json_fc"

    def test_unbound_model_exits_2(self, monkeypatch, models_file):
        import click
        import typer

        import agent_cli.main as main_mod

        monkeypatch.setattr(
            main_mod, "_setup_provider", lambda *a, **k: self._fake_setup()
        )
        with pytest.raises((typer.Exit, click.exceptions.Exit, SystemExit)) as ei:
            main_mod._bootstrap_provider("openai", None, None, None, None, 0)
        assert self._exit_code(ei) == 2

    def test_cli_flag_default_is_none(self):
        # D3: --dialect default None — 명시성 감지의 전제
        import inspect

        import agent_cli.main as main_mod

        for cmd in (main_mod.run, main_mod.web):
            param = inspect.signature(cmd).parameters["dialect"]
            # typer.Option 객체의 default 속성이 None 이어야 한다
            assert param.default.default is None, cmd.__name__
