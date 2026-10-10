"""B1(행동 루프) 넛지 fold (v10.32.0) — 형식 개입과 같은 일회성 재료.

종전엔 ``recovery`` 마킹이 없어 런 끝까지 남았다. 이제 ``recovery == "loop"``
로 찍히고, 다음 파싱 성공(또는 더 새로운 개입)에 캐시 뷰에서 접힌다. 형식
개입과 다른 점 하나: 직전 assistant 레코드는 실패 emission 이 아니라 **실행된
호출**이므로 넛지만 접고 그 앞은 남긴다. history.jsonl 은 불변.
"""

from __future__ import annotations

from agent_cli.context.manager import ContextManager
from agent_cli.context.records import (
    fold_resolved_intervention_indices,
    is_folding_intervention,
    is_format_intervention,
    is_loop_intervention,
)


def _call(cmd="ls"):
    return {
        "role": "assistant",
        "thought": "t",
        "ops": [{"action": "shell", "action_input": {"command": cmd}}],
    }


def _obs(text="a.py"):
    return {"role": "user", "tool": "shell", "success": True, "content": text}


def _loop_nudge(n=2):
    return {
        "role": "user",
        "tool": "shell",
        "success": False,
        "recovery": "loop",
        "content": f"You have called shell(...) {n} times in a row. …",
    }


def _format_nudge():
    return {
        "role": "user",
        "tool": "",
        "success": False,
        "recovery": "format",
        "content": "Observation: invalid JSON …",
    }


class TestPredicate:
    def test_loop_marking(self):
        assert is_loop_intervention(_loop_nudge())
        assert is_folding_intervention(_loop_nudge())
        # 형식 술어는 그대로 엄격 — 루프 넛지는 형식 개입이 아니다
        assert not is_format_intervention(_loop_nudge())

    def test_unmarked_tool_failure_still_stays(self):
        failed = {"role": "user", "tool": "shell", "success": False, "content": "ERR"}
        assert not is_loop_intervention(failed)
        assert not is_folding_intervention(failed)


class TestIndices:
    def test_loop_nudge_folds_alone_after_success(self):
        recs = [_call(), _obs(), _loop_nudge(), _call("pwd")]
        # 넛지(2)만 — 실행된 호출(0)·그 관찰(1)은 남는다
        assert fold_resolved_intervention_indices(recs) == [2]

    def test_level_two_resolves_level_one(self):
        recs = [_call(), _obs(), _loop_nudge(2), _loop_nudge(3)]
        assert fold_resolved_intervention_indices(recs) == [2]

    def test_unresolved_tail_stays(self):
        assert (
            fold_resolved_intervention_indices([_call(), _obs(), _loop_nudge()]) == []
        )

    def test_format_pairing_unchanged(self):
        # 형식 개입은 여전히 직전 실패 emission 과 짝으로 접힌다
        fail = {"role": "assistant", "content": "…broken…"}
        recs = [fail, _format_nudge(), _call()]
        assert fold_resolved_intervention_indices(recs) == [1, 0]


class TestLiveFold:
    def test_manager_folds_loop_nudge_only(self, tmp_path):
        ctx = ContextManager(session_dir=tmp_path)
        for r in (_call(), _obs("MARK"), _loop_nudge()):
            ctx.add(r)
        folded = ctx.fold_resolved_interventions(assume_tail_resolved=True)
        assert folded == 1
        left = ctx.get_raw_messages()
        assert not any(is_folding_intervention(r) for r in left)
        assert any(r.get("content") == "MARK" for r in left)  # 관찰 잔류
        assert any(r.get("role") == "assistant" for r in left)  # 호출 잔류

    def test_resume_view_matches_live(self, tmp_path):
        ctx = ContextManager(session_dir=tmp_path)
        for r in (_call(), _obs(), _loop_nudge(), _call("pwd")):
            ctx.add(r)
        ctx.fold_resolved_interventions()  # resume 경로: 레코드만으로 재판정
        assert not any(is_loop_intervention(r) for r in ctx.get_raw_messages())
        # history.jsonl 은 불변
        on_disk = (tmp_path / "history.jsonl").read_text()
        assert '"recovery": "loop"' in on_disk
