"""세션 노브는 한 번 선언되고 표면들이 파생한다 (agent_cli/knobs.py, v10.43.0).

종전엔 같은 노브가 env 읽기·CLI 플래그·web API 에 손으로 따로 배선돼 셋(사고·
문법·자동 승인)과 감사는 `run` 에 플래그가 없었다. 여기서 고정하는 것은 **표에서
파생된 대칭**이다: `run`/`web` 둘 다 표의 플래그 전부를 노출하고, env 는 표가
적은 이름만 읽히며(소스 어디에도 표 밖의 노브 env 읽기가 없다), 각 노브의 set 이
자기 소유자를 실제로 바꾸고, 우선순위는 CLI > env > 기본값이다. README 의 env
표도 표를 다 싣는다.
"""

from __future__ import annotations

import re
from pathlib import Path
from types import SimpleNamespace

import pytest
import typer
from typer.testing import CliRunner

from agent_cli import knobs as K
from agent_cli.context.manager import ContextManager
from agent_cli.knobs import KNOB_BY_NAME, KNOBS, KnobSession, apply_all, resolve_all

_PKG = Path(__file__).resolve().parent.parent / "agent_cli"


def _registered_options(command: str) -> set[str]:
    """등록된 click 파라미터의 옵션 이름 집합 (help 렌더를 파싱하지 않는다 —
    Rich 색상이 켜진 CI 에서 리터럴 `--x` 가 사라진다, tests/test_stream_idle)."""
    from typer.main import get_command

    from agent_cli.main import app

    cmd = get_command(app).commands[command]
    return {
        o
        for p in cmd.params
        for o in getattr(p, "opts", []) + getattr(p, "secondary_opts", [])
    }


class TestTableIsTheSingleDefinition:
    def test_names_flags_envs_are_unique(self):
        assert len({k.name for k in KNOBS}) == len(KNOBS)
        assert len({k.flag for k in KNOBS}) == len(KNOBS)
        assert len({k.env for k in KNOBS}) == len(KNOBS)
        assert all(
            k.flag.startswith("--") and k.env.startswith("AGENT_CLI_") for k in KNOBS
        )

    def test_run_and_web_expose_every_flag(self):
        for cmd in ("run", "web"):
            opts = _registered_options(cmd)
            for k in KNOBS:
                assert k.flag in opts, (cmd, k.flag)
                if k.kind.is_flag:
                    assert f"--no-{k.flag[2:]}" in opts, (cmd, k.flag)

    def test_no_knob_env_is_read_outside_the_table(self):
        """표 밖에서 노브 env 를 읽는 코드가 생기면 그 표면만 다른 값을 본다 —
        종전의 비대칭이 정확히 그 모양이었다."""
        offenders = []
        for path in _PKG.rglob("*.py"):
            if path.name == "knobs.py":
                continue
            text = path.read_text(encoding="utf-8")
            for k in KNOBS:
                if re.search(rf"environ(?:\.get)?\s*[\[(]\s*[\"']{k.env}[\"']", text):
                    offenders.append((path.relative_to(_PKG).as_posix(), k.env))
        assert offenders == []

    def test_readme_env_table_lists_every_knob_env(self):
        readme = (_PKG.parent / "README.md").read_text(encoding="utf-8")
        for k in KNOBS:
            assert f"| `{k.env}` |" in readme, k.env
            assert f"`{k.flag}`" in readme, k.flag


class TestKinds:
    def test_choice(self):
        kind = K.Choice("off", "on", "auto")
        assert kind.parse(" ON ") == "on" and kind.choices() == ("off", "on", "auto")
        with pytest.raises(ValueError):
            kind.parse("maybe")

    def test_bool(self):
        kind = K.Bool()
        assert kind.parse(True) is True and kind.parse("off") is False
        assert kind.parse("1") is True and kind.parse("no") is False
        with pytest.raises(ValueError):
            kind.parse("sometimes")

    def test_number_and_duration(self):
        assert K.Number(int).parse("3") == 3 and K.Number(float).parse("0.6") == 0.6
        assert K.Number(int).parse(7) == 7
        with pytest.raises(ValueError):
            K.Number(int).parse("abc")
        assert K.Duration().parse("600") == 600 and K.Duration().parse("10m") == 600
        assert K.Duration().parse("0") == 0 and K.Duration().parse(120) == 120
        with pytest.raises(ValueError):
            K.Duration().parse("soon")


class TestResolution:
    """CLI 인자 > env > 기본값. 잘못된 env 는 기본값으로(종전 env 들의 동작)."""

    @pytest.mark.parametrize(
        "name,env,expected",
        [
            ("thinking", None, "off"),  # v9.24.8: 미설정은 끔
            ("thinking", "on", "on"),
            ("thinking", "AUTO", "auto"),
            ("thinking", "bogus", "off"),  # 모르는 값도 끔
            ("reasoning_effort", None, "auto"),
            ("reasoning_effort", "high", "high"),
            ("grammar", None, "auto"),
            ("grammar", "off", "off"),
            ("max_agents", None, 10),
            ("max_agents", "3", 3),
            ("max_agents", "abc", 10),
            ("compaction_ratio", "0.6", 0.6),
            ("compaction_ratio", "", 1.0),
            ("stall", "900", 900),
            ("stall", None, 600),
            ("stall_attempts", "2", 2),
            ("auto_approve", "1", True),
            ("auto_approve", None, False),
            ("auditor", "yes", True),
            ("auditor", "0", False),
        ],
    )
    def test_env_then_default(self, name, env, expected):
        knob = KNOB_BY_NAME[name]
        environ = {} if env is None else {knob.env: env}
        assert knob.resolve(None, environ) == expected

    def test_cli_beats_env(self):
        knob = KNOB_BY_NAME["max_agents"]
        assert knob.resolve(7, {knob.env: "3"}) == 7
        assert knob.resolve(0, {knob.env: "3"}) == 0  # 명시적 무제한 ≠ 미지정

    def test_resolve_all_covers_every_knob(self, monkeypatch):
        for k in KNOBS:
            monkeypatch.delenv(k.env, raising=False)
        values = resolve_all({"grammar": "off"})
        assert set(values) == {k.name for k in KNOBS}
        assert values["grammar"] == "off" and values["thinking"] == "off"


def _renderer():
    import io

    from rich.console import Console

    from agent_cli.render.minimal import MinimalRenderer

    return MinimalRenderer(Console(file=io.StringIO(), force_terminal=False))


class _Registry:
    def __init__(self):
        self.max_agents = 10

    def set_max_agents(self, v):
        self.max_agents = max(0, int(v))
        return self.max_agents


class TestApplyReachesTheOwners:
    def _session(self, tmp_path):
        from tests.binding_support import make_binding

        return KnobSession(
            ctx=ContextManager(tmp_path / "s", max_context_tokens=10_000),
            registry=_Registry(),
            renderer=_renderer(),
            auditor=SimpleNamespace(enabled=False),
            binding=make_binding(model="m"),
        )

    def test_every_knob_round_trips_through_its_owner(self, tmp_path):
        s = self._session(tmp_path)
        values = {
            "thinking": "on",
            "reasoning_effort": "high",
            "grammar": "off",
            "max_agents": 3,
            "compaction_ratio": 0.8,
            "stall": 300,
            "stall_attempts": 6,
            "auto_approve": True,
            "auditor": True,
        }
        apply_all(s, values)
        for k in KNOBS:
            if k.name in values:
                assert k.get(s) == values[k.name], k.name
        assert KNOB_BY_NAME["model"].get(s) == "m"  # 값 없음 → 손대지 않는다
        assert s.ctx.thinking_override == {
            "enable_thinking": True,
            "reasoning_effort": "high",
        }
        assert s.ctx.grammar_override is False
        assert s.ctx.stream_idle_timeout_s == 300 and s.ctx.stream_max_attempts == 6
        assert s.renderer.auto_approve is True and s.auditor.enabled is True

    def test_thinking_and_effort_do_not_clobber_each_other(self, tmp_path):
        s = self._session(tmp_path)
        KNOB_BY_NAME["reasoning_effort"].set(s, "low")
        KNOB_BY_NAME["thinking"].set(s, "auto")
        assert s.ctx.thinking_override == {"reasoning_effort": "low"}
        KNOB_BY_NAME["reasoning_effort"].set(s, "auto")
        assert s.ctx.thinking_override == {}
        KNOB_BY_NAME["thinking"].set(s, "off")
        assert s.ctx.thinking_override == {"enable_thinking": False}

    def test_defaults_match_the_owners_fresh_state(self, tmp_path, monkeypatch):
        """표의 기본값 = 소유자가 아무 노브 없이 시작한 값 — 둘이 어긋나면 "기본값"
        이 두 개가 된다."""
        for k in KNOBS:
            monkeypatch.delenv(k.env, raising=False)
        s = self._session(tmp_path)
        for k in KNOBS:
            if k.default is not None:  # 모델의 기본은 config/프로바이더가 정한다
                assert k.get(s) == k.default, k.name

    def test_missing_owners_are_skipped_not_crashed(self, tmp_path):
        """headless 부트(세션 없음)·테스트는 소유자 일부가 None — 표는 그 노브를
        건너뛰고 나머지는 적용한다."""
        s = self._session(tmp_path)
        s.auditor = None
        s.ctx = None
        assert not KNOB_BY_NAME["auditor"].available(s)
        assert not KNOB_BY_NAME["thinking"].available(s)
        apply_all(s, {"auditor": True, "thinking": "on", "max_agents": 2})
        assert s.registry.max_agents == 2


class TestCliSurface:
    def _app(self):
        app = typer.Typer()
        seen = {}

        @app.command()
        @K.knob_options
        def go(query: str = typer.Argument(...), knob_args: dict | None = None):
            seen.update(knob_args)
            print("ok")

        return app, seen

    def test_only_given_flags_are_passed_parsed(self):
        app, seen = self._app()
        r = CliRunner().invoke(
            app,
            [
                "q",
                "--thinking",
                "ON",
                "--max-agents",
                "3",
                "--stall",
                "5m",
                "--auditor",
            ],
        )
        assert r.exit_code == 0, r.output
        assert seen == {
            "thinking": "on",
            "max_agents": 3,
            "stall": 300,
            "auditor": True,
        }

    def test_no_flag_pair_for_bools(self):
        app, seen = self._app()
        r = CliRunner().invoke(app, ["q", "--no-auto-approve", "--no-auditor"])
        assert r.exit_code == 0 and seen == {"auto_approve": False, "auditor": False}

    def test_bad_value_fails_before_the_session_starts(self):
        app, seen = self._app()
        r = CliRunner().invoke(app, ["q", "--grammar", "maybe"])
        assert r.exit_code != 0 and "--grammar" in r.output and seen == {}


class TestModelKnob:
    """모델 노브 — 바인딩을 바꾸면 **다음 LLM 호출**부터 새 모델·능력이 실린다
    (복사본 없음). models.json 에 있고 이 세션의 방언에 묶인 모델만."""

    @pytest.fixture
    def models(self, tmp_path, monkeypatch):
        import json

        import agent_cli.config as _config
        import agent_cli.dialects as _dialects

        target = tmp_path / "models.json"
        target.write_text(
            json.dumps(
                {
                    "models": {
                        "a": {"context_window": 80000, "dialect": "json_fc"},
                        "b": {"context_window": 20000, "dialect": "json_fc"},
                        "x": {"context_window": 8000, "dialect": "xml_fc"},
                    }
                }
            ),
            encoding="utf-8",
        )
        monkeypatch.setattr(_config, "_SEARCH_PATHS", [target])
        monkeypatch.setattr(_config, "_cached_registry", None)
        monkeypatch.setattr(_dialects, "_override", None)

    def test_switch_applies_to_the_next_call_without_copies(self, tmp_path, models):
        from agent_cli.loop.state import LoopConfig
        from agent_cli.providers.capabilities import ModelCapabilities
        from tests.binding_support import make_binding

        b = make_binding(
            model="a",
            capabilities=ModelCapabilities(80000, 1000, False),
            dialect="json_fc",
        )
        cfg = LoopConfig(binding=b)
        ctx = ContextManager(tmp_path / "s", binding=b)
        s = KnobSession(binding=b)
        assert cfg.model == "a" and ctx.max_context_tokens == 80000 * 7 // 10
        assert KNOB_BY_NAME["model"].set(s, "b") == "b"
        assert cfg.model == "b" and cfg.capabilities.context_window == 20000
        assert ctx.max_context_tokens == 20000 * 7 // 10  # 예산도 따라간다
        assert KNOB_BY_NAME["model"].get(s) == "b"

    def test_unknown_or_foreign_dialect_models_are_refused(self, models):
        from tests.binding_support import make_binding

        b = make_binding(model="a", dialect="json_fc")
        with pytest.raises(ValueError, match="not in models.json"):
            KNOB_BY_NAME["model"].set(KnobSession(binding=b), "nope")
        with pytest.raises(ValueError, match="dialect 'xml_fc'"):
            KNOB_BY_NAME["model"].set(KnobSession(binding=b), "x")
        assert b.model == "a"  # 거부는 아무것도 바꾸지 않는다
        assert b.switch("a") == ""  # 같은 이름은 no-op

    def test_pinned_copy_is_not_followed_by_switch(self, models):
        from tests.binding_support import make_binding

        b = make_binding(model="a", dialect="json_fc")
        child = b.pinned("b")
        assert child.model == "b" and child is not b
        assert b.switch("a") == "" and child.model == "b"

    def test_cli_short_alias(self):
        app, seen = TestCliSurface()._app()
        r = CliRunner().invoke(app, ["q", "-m", "zeta"])
        assert r.exit_code == 0 and seen == {"model": "zeta"}


class TestSubagentsInheritTheSessionValues:
    def test_child_ctx_copies_thinking_and_grammar_from_parent(self, tmp_path):
        """종전엔 ctx 마다 env 를 다시 읽어 web 🧠·📐 노브가 팀원·delegate 에
        닿지 않았다 — 이제 부모의 현재 값을 물려받는다."""
        from agent_cli.subagent.runner import create_subagent_ctx

        parent = ContextManager(tmp_path / "s", max_context_tokens=10_000)
        parent.set_thinking_override(enable_thinking=True, reasoning_effort="low")
        parent.set_grammar_override(False)
        child, err = create_subagent_ctx("none", parent, tmp_path / "s" / "run_x")
        assert err == "" and child is not None
        assert child.thinking_override == {
            "enable_thinking": True,
            "reasoning_effort": "low",
        }
        assert child.grammar_override is False
        child.set_thinking_override(enable_thinking=False)
        assert parent.thinking_override["enable_thinking"] is True  # 복사본


class TestRunBootApplies:
    def test_apply_knobs_helper_orders_cli_over_env(self, tmp_path, monkeypatch):
        from agent_cli.main import _apply_knobs

        monkeypatch.setenv("AGENT_CLI_MAX_AGENTS", "3")
        monkeypatch.setenv("AGENT_CLI_THINKING", "on")
        ctx = ContextManager(tmp_path / "s", max_context_tokens=10_000)
        reg = _Registry()
        auditor = SimpleNamespace(enabled=False)
        _apply_knobs(
            {"max_agents": 7},
            ctx=ctx,
            registry=reg,
            renderer=_renderer(),
            auditor=auditor,
        )
        assert reg.max_agents == 7  # CLI
        assert ctx.thinking_override == {"enable_thinking": True}  # env
        assert auditor.enabled is False  # 기본값

    def test_minimal_renderer_auto_approve_answers_without_asking(self):
        from agent_cli.render.base import ConfirmOption

        r = _renderer()
        r.set_auto_approve(True)
        key, comment = r.confirm(
            "run it?",
            [ConfirmOption("y", "allow"), ConfirmOption("n", "deny")],
            default_key="n",
        )
        assert (key, comment) == ("y", "")
        assert "자동 승인" in r.con.file.getvalue()
