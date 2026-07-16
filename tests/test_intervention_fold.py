"""형식-복구 개입 fold (v4.51.0) — "dynamic context 에는 성공 궤적만".

설계 결정(사용자): 형식 개입(NO_THOUGHT/NO_JSON/NO_ACTION/A4/A5)은 교정의
일회성 재료 — 다음 파싱 성공 시 [실패 prior, 개입] 쌍을 캐시 뷰에서 접는다.
B1(행동 루프)과 도구 실행 실패는 과제 정보라 잔류. history.jsonl 불변.
"""

from __future__ import annotations

from agent_cli.context.manager import ContextManager
from agent_cli.context.records import (
    fold_resolved_intervention_indices,
    is_format_intervention,
)


def _fail_prior():
    return {"role": "assistant", "content": "…broken emission…"}


def _intervention(**kw):
    rec = {
        "role": "user",
        "tool": "",
        "success": False,
        "content": "Observation: invalid JSON — emit ## Thought/## Action …",
    }
    rec.update(kw)
    return rec


def _success_turn():
    return {
        "role": "assistant",
        "thought": "t",
        "ops": [{"action": "shell", "action_input": {"command": "ls"}}],
    }


class TestPredicate:
    def test_marked_format_intervention(self):
        # A4/A5 류: tool=<도구명>이라도 recovery 마킹으로 식별
        assert is_format_intervention(
            {
                "role": "user",
                "tool": "read_file",
                "success": False,
                "recovery": "format",
            }
        )

    def test_legacy_toolless_backstop(self):
        assert is_format_intervention(_intervention())  # tool=="" 규약

    def test_b1_and_tool_errors_are_not(self):
        # B1 개입/도구 실행 실패: tool=<도구명> + 마킹 없음 → 비대상
        assert not is_format_intervention(
            {"role": "user", "tool": "shell", "success": False, "content": "loop?"}
        )
        assert not is_format_intervention(
            {"role": "user", "tool": "shell", "success": False, "content": "ERROR"}
        )
        assert not is_format_intervention({"role": "user", "content": "그냥 채팅"})

    def test_indices_pair_and_unresolved_tail(self):
        recs = [_fail_prior(), _intervention(), _success_turn()]
        assert fold_resolved_intervention_indices(recs) == [1, 0]
        # 꼬리 미해소(성공 없음) → 레코드-기반 판정으론 잔류
        assert (
            fold_resolved_intervention_indices([_fail_prior(), _intervention()]) == []
        )


class TestManagerFold:
    def _ctx(self, tmp_path):
        return ContextManager(tmp_path / "s", max_context_tokens=100_000)

    def test_live_fold_removes_pair(self, tmp_path):
        ctx = self._ctx(tmp_path)
        ctx.add({"role": "user", "content": "질문"})
        ctx.add(_fail_prior())
        ctx.add(_intervention())
        before = ctx.get_estimated_tokens()
        n = ctx.fold_resolved_interventions(assume_tail_resolved=True)
        assert n == 2
        cache = ctx.get_raw_messages()
        assert all(not is_format_intervention(r) for r in cache)
        assert all(r.get("content") != "…broken emission…" for r in cache)
        assert ctx.get_estimated_tokens() < before
        # get_messages(_nl_cache 무효화) 정합
        assert all(
            "broken emission" not in m.get("content", "") for m in ctx.get_messages()
        )

    def test_consecutive_failures_keep_only_latest(self, tmp_path):
        # live 흐름 재현: 실패1→개입1→(파싱성공X, 재실패)→fold 호출 없음…
        # 두 번째 성공 시 fold 가 해소분+꼬리 전부 접는다
        ctx = self._ctx(tmp_path)
        ctx.add(_fail_prior())
        ctx.add(_intervention(content="개입 1"))
        ctx.add(_fail_prior())
        ctx.add(_intervention(content="개입 2"))
        ctx.fold_resolved_interventions(assume_tail_resolved=True)
        cache = ctx.get_raw_messages()
        assert all(not is_format_intervention(r) for r in cache)

    def test_history_jsonl_is_immutable(self, tmp_path):
        ctx = self._ctx(tmp_path)
        ctx.add(_fail_prior())
        ctx.add(_intervention())
        ctx.fold_resolved_interventions(assume_tail_resolved=True)
        # 관측·디버그 보존: 파일에는 개입이 그대로 남는다
        text = ctx.history_path.read_text()
        assert "invalid JSON" in text

    def test_resume_reapplies_same_view(self, tmp_path):
        first = self._ctx(tmp_path)
        first.add({"role": "user", "content": "질문"})
        first.add(_fail_prior())
        first.add(_intervention())
        first.add(_success_turn())  # 해소 증거가 history 에 남는 순서
        resumed = ContextManager(
            tmp_path / "s", max_context_tokens=100_000, resume=True
        )
        cache = resumed.get_raw_messages()
        assert all(not is_format_intervention(r) for r in cache)
        assert any(r.get("role") == "assistant" and r.get("ops") for r in cache)


class TestDispatcherIntegration:
    def test_parse_success_folds_prior_intervention(self, tmp_path):
        from agent_cli.loop import LoopConfig, LoopState, ToolBridge, TurnDispatcher
        from agent_cli.wire_formats import get as get_wf

        wf = get_wf("json_fc")
        ctx = ContextManager(tmp_path / "s", max_context_tokens=100_000)
        cfg = LoopConfig(tools_list=["shell", "complete"], wire_format=wf)
        st = LoopState(query="q")
        from agent_cli.recovery.observability import TurnRecorder

        d = TurnDispatcher(
            cfg,
            st,
            ctx=ctx,
            tools=ToolBridge(cfg, st, ctx, None),
            recorder=TurnRecorder(session_dir=None, enabled=False),
        )

        # 턴 N: 깨진 emission → NO_JSON 개입 기록
        d._handle_text_path("완전 산문 — 파싱 불가")
        assert any(is_format_intervention(r) for r in ctx.get_raw_messages())
        # 턴 N+1: 파싱 성공 emission → 개입 쌍이 캐시 뷰에서 소멸
        good = '## Thought\nok\n## Action\n[{"action":"complete","result":"done"}]'
        d._handle_text_path(good)
        assert all(not is_format_intervention(r) for r in ctx.get_raw_messages())
        # history 에는 남음 (불변) — 개입 레코드가 파일에 보존
        import json as _json

        recs = [_json.loads(line) for line in ctx.history_path.read_text().splitlines()]
        assert any(is_format_intervention(r) for r in recs)
