"""subagent 공용 러너 계약 (teammate P0, docs/teammate/DESIGN.md §4.1).

delegate 가 러너의 유일한 소비자인 동안(P0)의 게이트는 두 겹이다:
기존 delegate 테스트 무수정 전체 통과(바이트 동일 동작) + 여기의
러너 자체 계약. P1 에서 teammate 가 두 번째 소비자로 붙을 때 이
계약이 공유 의미의 기준선이 된다.
"""

from __future__ import annotations

import agent_cli.loop as loop_mod
import agent_cli.subagent.runner as runner_mod
from agent_cli.context.manager import ContextManager
from agent_cli.subagent.runner import (
    apply_role_overrides,
    create_subagent_ctx,
    run_subagent_message,
)

# ── apply_role_overrides ────────────────────────


class TestApplyRoleOverrides:
    def test_empty_config_is_passthrough(self):
        tools, model, hooks = apply_role_overrides(
            {}, allowed_tools=["shell"], model="m1", hooks_config={"a": 1}
        )
        assert tools == ["shell"]
        assert model == "m1"
        assert hooks == {"a": 1}

    def test_config_fills_allowed_tools_when_unset(self):
        tools, _, _ = apply_role_overrides(
            {"allowed-tools": ["read_file"]},
            allowed_tools=None,
            model="",
            hooks_config=None,
        )
        assert tools == ["read_file"]

    def test_explicit_allowed_tools_beat_config(self):
        tools, _, _ = apply_role_overrides(
            {"allowed-tools": ["read_file"]},
            allowed_tools=["shell"],
            model="",
            hooks_config=None,
        )
        assert tools == ["shell"]

    def test_config_model_overrides(self):
        _, model, _ = apply_role_overrides(
            {"model": "role-model"},
            allowed_tools=None,
            model="caller",
            hooks_config=None,
        )
        assert model == "role-model"

    def test_non_string_model_ignored(self):
        _, model, _ = apply_role_overrides(
            {"model": {"bad": 1}}, allowed_tools=None, model="caller", hooks_config=None
        )
        assert model == "caller"

    def test_hooks_merged_on_top_of_caller(self, monkeypatch):
        # merge 의미(교체 아님)는 hooks 모듈 소유 — 여기선 배선만 검증.
        calls = {}

        def fake_parse(raw):
            calls["parsed"] = raw
            return {"overlay": True}

        def fake_merge(base, overlay):
            calls["merged"] = (base, overlay)
            return {"merged": True}

        import agent_cli.hooks as hooks_mod

        monkeypatch.setattr(hooks_mod, "parse_hooks_config", fake_parse)
        monkeypatch.setattr(hooks_mod, "merge_hooks_configs", fake_merge)

        _, _, hooks = apply_role_overrides(
            {"hooks": {"OnToolStart": []}},
            allowed_tools=None,
            model="",
            hooks_config={"base": True},
        )
        assert hooks == {"merged": True}
        assert calls["parsed"] == {"OnToolStart": []}
        assert calls["merged"] == ({"base": True}, {"overlay": True})

    def test_non_dict_hooks_ignored(self):
        _, _, hooks = apply_role_overrides(
            {"hooks": "bogus"}, allowed_tools=None, model="", hooks_config={"base": 1}
        )
        assert hooks == {"base": 1}


# ── create_subagent_ctx ─────────────────────────


class TestCreateSubagentCtx:
    def test_none_mode_fresh_ctx_inherits_budget_and_wire_format(self, tmp_path):
        parent = ContextManager(tmp_path / "parent", max_context_tokens=12345)
        ctx, error = create_subagent_ctx("none", parent, tmp_path / "sub")
        assert error == ""
        assert ctx is not None
        assert ctx.max_context_tokens == 12345
        assert type(ctx.wire_format) is type(parent.wire_format)
        assert ctx.get_raw_messages() == []

    def test_none_mode_without_parent_defaults(self, tmp_path):
        ctx, error = create_subagent_ctx("none", None, tmp_path / "sub")
        assert error == ""
        assert ctx is not None
        # 부모 없으면 예산 0 전달 → ContextManager 자체 기본값으로 정규화.
        bare = ContextManager(tmp_path / "bare", max_context_tokens=0)
        assert ctx.max_context_tokens == bare.max_context_tokens

    def test_inherits_parent_compaction_ratio(self, tmp_path):
        # Sub-agent snapshots the parent's live compaction ratio at creation
        # (web-slider value), like max_context_tokens — all three modes.
        parent = ContextManager(tmp_path / "parent", max_context_tokens=1000)
        parent.set_compaction_ratio(0.6)
        parent.add({"role": "user", "content": "hi"})  # fork needs history
        for mode, sub in (("none", "s1"), ("fork", "s2"), ("resume", "s3")):
            ctx, error = create_subagent_ctx(mode, parent, tmp_path / sub)
            assert error == "" and ctx is not None
            assert ctx.compaction_ratio == 0.6, mode

    def test_no_parent_uses_default_ratio(self, tmp_path):
        ctx, error = create_subagent_ctx("none", None, tmp_path / "sub")
        assert error == "" and ctx is not None
        assert ctx.compaction_ratio == 0.8  # DEFAULT_COMPACTION_RATIO

    def test_fork_without_parent_is_error(self, tmp_path):
        ctx, error = create_subagent_ctx("fork", None, tmp_path / "sub")
        assert ctx is None
        # delegate 는 이 문자열을 "Delegation rejected: {error}" 로 감싼다 —
        # 기존 관찰 문구 바이트 불변의 전제.
        assert error == "fork requires parent context"

    def test_fork_copies_parent_history(self, tmp_path):
        parent = ContextManager(tmp_path / "parent", max_context_tokens=500)
        parent.add({"role": "user", "content": "hello from parent"})
        ctx, error = create_subagent_ctx("fork", parent, tmp_path / "sub")
        assert error == ""
        assert ctx is not None
        assert (tmp_path / "sub" / "history.jsonl").is_file()
        raw = ctx.get_raw_messages()
        assert any("hello from parent" in str(m) for m in raw)

    def test_live_ctx_registered_to_inspector_scope(self, tmp_path, monkeypatch):
        seen = []

        class _Recorder:
            def note_scope_ctx(self, ctx):
                seen.append(ctx)

        import agent_cli.render as render_mod

        monkeypatch.setattr(render_mod, "get_renderer", lambda: _Recorder())
        ctx, _ = create_subagent_ctx("none", None, tmp_path / "sub")
        assert seen == [ctx]


# ── run_subagent_message ────────────────────────


class TestRunSubagentMessage:
    def _capture_run_loop(self, monkeypatch, result="done"):
        captured = {}

        def fake_run_loop(**kwargs):
            captured.update(kwargs)

            class _R:
                success = True
                output = result

            return _R()

        monkeypatch.setattr(loop_mod, "run_loop", fake_run_loop)
        return captured

    def test_threads_args_and_increments_depth(self, tmp_path, monkeypatch):
        captured = self._capture_run_loop(monkeypatch)
        ctx = ContextManager(tmp_path / "sub", max_context_tokens=100)
        stop = object()

        loop_result, duration = run_subagent_message(
            "do the thing",
            ctx,
            provider="P",
            capabilities="C",
            model="m",
            timeout=77,
            depth=1,
            max_depth=3,
            active_tools=["shell"],
            agent_name="explorer",
            stop_event=stop,
            agent_role="ROLE",
            compaction_enabled=False,
        )

        assert loop_result.output == "done"
        assert duration >= 0.0
        assert captured["query"] == "do the thing"
        assert captured["ctx"] is ctx
        # 서브루프는 호출자 깊이 + 1 로 돈다 (기존 delegate 의미).
        assert captured["depth"] == 2
        assert captured["max_depth"] == 3
        assert captured["agent_timeout"] == 77
        assert captured["verbose"] is False
        assert captured["active_tools"] == ["shell"]
        assert captured["agent_name"] == "explorer"
        assert captured["stop_event"] is stop
        assert captured["agent_role"] == "ROLE"
        assert captured["compaction_enabled"] is False

    def test_lazy_imports_stay_function_local(self):
        # 순환 회피 계약: 모듈 로드 시점에 context.manager/loop/render 를
        # 끌어오지 않는다 (registry → delegate → … 순환의 재발 방지).
        import ast
        import inspect

        tree = ast.parse(inspect.getsource(runner_mod))
        top_level_imports = {
            node.module
            for node in tree.body
            if isinstance(node, ast.ImportFrom) and node.module
        }
        for banned in (
            "agent_cli.context.manager",
            "agent_cli.loop",
            "agent_cli.render",
        ):
            assert banned not in top_level_imports
