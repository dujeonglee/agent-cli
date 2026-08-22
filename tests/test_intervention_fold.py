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

        # 턴 N: 깨진 emission → NO_ACTION 개입 기록. (순수 산문은 v7.14 부터
        # 산문-완료로 수용되므로, 개입을 트리거하려면 액션-잔해가 명확한
        # 깨진 op-array 를 쓴다.)
        d._handle_text_path('[{"완전 깨진 op — 파싱 불가')
        assert any(is_format_intervention(r) for r in ctx.get_raw_messages())
        # 턴 N+1: 파싱 성공 emission → 개입 쌍이 캐시 뷰에서 소멸
        good = '## Thought\nok\n## Action\n[{"action":"complete","result":"done"}]'
        d._handle_text_path(good)
        assert all(not is_format_intervention(r) for r in ctx.get_raw_messages())
        # history 에는 남음 (불변) — 개입 레코드가 파일에 보존
        import json as _json

        recs = [_json.loads(line) for line in ctx.history_path.read_text().splitlines()]
        assert any(is_format_intervention(r) for r in recs)


class TestFoldOffsetResumeConvergence:
    """P0-8a 검증: fold 는 캐시 **중간**을 제거하고 ``_dynamic_start_index``
    (prefix-skip 오프셋)를 올리지 않는다 — 이는 버그가 아니라 설계다:
    resume 이 forward slice 로드 직후 ``fold_resolved_interventions()`` 를
    **재적용**해(레코드-기반 재판정) live 와 동일 뷰로 결정적으로 수렴한다
    (manager docstring "live↔resume 동일 뷰, 사이드카 상태 불필요").
    이 스위트는 리뷰에서 제기된 시나리오(fold 후 evict 로 오프셋 전진 →
    resume)에서도 수렴이 깨지지 않음을 회귀로 고정한다."""

    def test_fold_then_evict_then_resume_converges(self, tmp_path):
        sd = tmp_path / "s"
        ctx = ContextManager(sd, max_context_tokens=100_000)
        # [u0, u1, 실패, 개입, 성공, u2] — 개입 쌍은 캐시 중간에 위치.
        ctx.add({"role": "user", "content": "q0" * 50})
        ctx.add({"role": "user", "content": "q1" * 50})
        ctx.add(_fail_prior())
        ctx.add(_intervention())
        ctx.fold_resolved_interventions(assume_tail_resolved=True)  # live fold
        ctx.add(_success_turn())
        ctx.add({"role": "user", "content": "q2" * 50})
        # evict 로 앞의 u0 하나를 버려 오프셋을 전진시킨다 (fold 뒤의 전진 —
        # 리뷰 시나리오의 핵심: 오프셋은 접힌 중간 레코드를 모른다).
        per = ctx.get_estimated_tokens() // len(ctx._cache) + 1
        ctx._evict_fifo(ctx.get_estimated_tokens() - per)
        assert ctx._dynamic_start_index >= 1
        live_view = [(m.get("role"), m.get("content", "")[:8]) for m in ctx._cache]
        # 접힌 개입은 live 뷰에 없다.
        assert all("invalid JSON" not in (m.get("content") or "") for m in ctx._cache)

        resumed = ContextManager(sd, max_context_tokens=100_000, resume=True)
        resumed_view = [
            (m.get("role"), m.get("content", "")[:8]) for m in resumed._cache
        ]
        # 수렴: forward slice 에 다시 들어온 접힌 레코드를 재-fold 가 제거해
        # live 와 동일 뷰 (재혼입이 최종 뷰에 남지 않는다).
        assert resumed_view == live_view
        assert all(
            "invalid JSON" not in (m.get("content") or "") for m in resumed._cache
        )

    def test_repeated_fold_evict_cycles_stay_convergent(self, tmp_path):
        """fold·evict 가 여러 번 교차해도(오프셋-접힘 불일치 누적) resume
        재-fold 수렴은 유지된다."""
        sd = tmp_path / "s"
        ctx = ContextManager(sd, max_context_tokens=100_000)
        for i in range(3):
            ctx.add({"role": "user", "content": f"q{i}" * 60})
            ctx.add(_fail_prior())
            ctx.add(_intervention())
            ctx.fold_resolved_interventions(assume_tail_resolved=True)
            ctx.add(_success_turn())
            per = ctx.get_estimated_tokens() // max(len(ctx._cache), 1) + 1
            ctx._evict_fifo(max(ctx.get_estimated_tokens() - per, 1))
        live_view = [(m.get("role"), m.get("content", "")[:8]) for m in ctx._cache]
        resumed = ContextManager(sd, max_context_tokens=100_000, resume=True)
        resumed_view = [
            (m.get("role"), m.get("content", "")[:8]) for m in resumed._cache
        ]
        assert resumed_view == live_view


class TestHidxAlignmentInvariant:
    """P0-8a 북키핑 불변식: ``_cache_hidx`` 는 캐시와 항상 같은 길이·순서이며
    각 값은 그 레코드의 실제 history 서수다 — 모든 변형(add/fold/evict/resume)
    후에도 유지된다."""

    def _aligned(self, ctx):
        assert len(ctx._cache_hidx) == len(ctx._cache)
        assert ctx._cache_hidx == sorted(ctx._cache_hidx)  # 순서 보존(단조)

    def test_invariant_across_mutations(self, tmp_path):
        sd = tmp_path / "s"
        ctx = ContextManager(sd, max_context_tokens=100_000)
        for i in range(3):
            ctx.add({"role": "user", "content": f"q{i}" * 40})
        self._aligned(ctx)
        assert ctx._cache_hidx == [0, 1, 2]
        ctx.add(_fail_prior())
        ctx.add(_intervention())
        ctx.fold_resolved_interventions(assume_tail_resolved=True)
        self._aligned(ctx)
        assert ctx._cache_hidx == [0, 1, 2]  # 중간(3,4) 접힘 반영
        ctx.add(_success_turn())
        self._aligned(ctx)
        assert ctx._cache_hidx == [0, 1, 2, 5]  # 서수는 history 기준 유지
        per = ctx.get_estimated_tokens() // len(ctx._cache) + 1
        ctx._evict_fifo(ctx.get_estimated_tokens() - per)
        self._aligned(ctx)
        assert (
            ctx._dynamic_start_index == ctx._cache_hidx[0] if ctx._cache_hidx else True
        )
        # resume 후에도 정렬 유지 + 서수 연속성
        resumed = ContextManager(sd, max_context_tokens=100_000, resume=True)
        self._aligned(resumed)
        assert resumed._history_ordinal == 6  # 총 add 수 (fold 는 history 불변)
