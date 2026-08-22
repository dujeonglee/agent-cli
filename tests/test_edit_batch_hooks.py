"""P0-2: 같은 파일 edit_file 배치의 훅/이력 계약 — 단건 경로와 동형.

종전 dispatch 가 ``apply_edits_batch`` 를 직접 불러 배치에서만 Pre/PostToolUse
훅과 ``recent_tool_history``(B1 입력)가 통째로 빠졌다. 이 스위트는 신설
``ToolBridge.dispatch_edit_batch`` 의 계약을 고정한다:

  - PreToolUse 는 **edit 별** 발화, 하나라도 블록 → **배치 전체 미적용**
    (all-or-nothing 원자성 유지) + 단건과 동형의 블록 ToolResult, 블록 시
    post/이력 미기록(단건 계약 동형).
  - 훅의 modified_input 은 해당 edit 에 반영.
  - 적용은 ``apply_edits_batch`` 1회(단일 쓰기·관찰 1건) — 배치 의미 보존.
  - PostToolUse 1회 / recent_tool_history 는 edit 별.
  - 예외 안전망·문구는 단건 오케스트레이터와 동일.
"""

from __future__ import annotations

from agent_cli.loop.state import LoopConfig, LoopState
from agent_cli.loop.tool_bridge import ToolBridge
from agent_cli.tools.read_file import compute_line_hash


def _ref(n: int, line: str) -> str:
    return f"{n}#{compute_line_hash(n, line)}"


def _write(tmp_path, name, lines):
    p = tmp_path / name
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return p


class _Ctx:
    is_blocked = False
    block_reason = None
    modified_input = None


class _FakeHookRunner:
    """PreToolUse 별 블록/수정 시나리오를 주입하는 기록형 러너."""

    def __init__(self, block_when=None, modify_when=None):
        self.calls: list[tuple[str, str, dict]] = []
        self._block_when = block_when  # callable(input_dict) -> bool
        self._modify_when = modify_when  # callable(input_dict) -> dict|None

    def fire(self, event, **kw):
        tool_input = kw.get("tool_input") or {}
        self.calls.append(
            (
                event,
                kw.get("tool_name"),
                dict(tool_input) if isinstance(tool_input, dict) else {},
            )
        )
        ctx = _Ctx()
        if event == "PreToolUse":
            if self._block_when and self._block_when(tool_input):
                ctx.is_blocked = True
                ctx.block_reason = "policy says no"
            elif self._modify_when:
                mod = self._modify_when(tool_input)
                if mod is not None:
                    ctx.modified_input = mod
        return ctx

    def pre_calls(self):
        return [c for c in self.calls if c[0] == "PreToolUse"]

    def post_calls(self):
        return [c for c in self.calls if c[0] == "PostToolUse"]


def _bridge(runner=None) -> ToolBridge:
    cfg = LoopConfig(hook_runner=runner)
    return ToolBridge(cfg, LoopState(), ctx=None, provider=None)


def _two_edits(lines):
    return [
        {"op": "replace", "pos": _ref(2, lines[1]), "lines": ["B"]},
        {"op": "replace", "pos": _ref(4, lines[3]), "lines": ["D"]},
    ]


class TestDispatchEditBatchHooks:
    def test_pre_per_edit_post_once_history_per_edit(self, tmp_path):
        """계약의 골자: pre=edit 별 / post=1회 / 이력=edit 별 / 적용 성공."""
        lines = ["a", "b", "c", "d", "e"]
        p = _write(tmp_path, "f.txt", lines)
        runner = _FakeHookRunner()
        bridge = _bridge(runner)

        result = bridge.dispatch_edit_batch(str(p), _two_edits(lines))

        assert result.success
        assert p.read_text().splitlines() == ["a", "B", "c", "D", "e"]
        # PreToolUse 는 edit 별 — 각 호출이 자기 입력을 본다.
        pres = runner.pre_calls()
        assert len(pres) == 2
        assert pres[0][1] == "edit_file" and pres[0][2]["lines"] == ["B"]
        assert pres[1][2]["lines"] == ["D"]
        # PostToolUse 는 1회 (물리 실행 1회 — 포매터류 중복 실행 방지).
        assert len(runner.post_calls()) == 1
        # 이력은 edit 별 (B1 이 각 편집을 본다).
        rows = bridge.recent_tool_history
        assert len(rows) == 2
        assert all(r["tool"] == "edit_file" and r["success"] for r in rows)

    def test_block_aborts_whole_batch_atomically(self, tmp_path):
        """어느 edit 의 블록이든 배치 전체 미적용 (all-or-nothing) — 파일 불변,
        단건과 동형의 블록 결과, 블록 시 post/이력 미기록(단건 계약 동형)."""
        lines = ["a", "b", "c", "d", "e"]
        p = _write(tmp_path, "f.txt", lines)
        before = p.read_text()
        runner = _FakeHookRunner(block_when=lambda inp: inp.get("lines") == ["D"])
        bridge = _bridge(runner)

        result = bridge.dispatch_edit_batch(str(p), _two_edits(lines))

        assert not result.success
        assert "Blocked by PreToolUse hook" in result.error  # 단건 동형 문구
        assert "none applied" in result.error  # 배치 원자성 명시
        assert p.read_text() == before  # 첫 edit 도 미적용
        assert runner.post_calls() == []  # 단건 동형: 블록 시 post 미발화
        assert bridge.recent_tool_history == []  # 블록 시 이력 미기록

    def test_modified_input_applied_to_that_edit(self, tmp_path):
        """훅의 modified_input 이 해당 edit 에만 반영된다 (단건 동형)."""
        lines = ["a", "b", "c", "d", "e"]
        p = _write(tmp_path, "f.txt", lines)

        def modify(inp):
            if inp.get("lines") == ["B"]:
                out = dict(inp)
                out["lines"] = ["B-MODIFIED"]
                return out
            return None

        bridge = _bridge(_FakeHookRunner(modify_when=modify))
        result = bridge.dispatch_edit_batch(str(p), _two_edits(lines))

        assert result.success
        assert p.read_text().splitlines() == ["a", "B-MODIFIED", "c", "D", "e"]

    def test_exception_becomes_toolresult_same_wording(self, tmp_path, monkeypatch):
        """apply 가 raise 하면 단건 안전망과 동일 문구의 ToolResult(False) —
        런이 죽지 않고, post/이력도 단건 동형으로 실행된다."""
        import agent_cli.tools.edit_file as ef

        def boom(path, edits):
            raise TypeError("kaboom")

        monkeypatch.setattr(ef, "apply_edits_batch", boom)
        lines = ["a", "b", "c", "d", "e"]
        p = _write(tmp_path, "f.txt", lines)
        runner = _FakeHookRunner()
        bridge = _bridge(runner)

        result = bridge.dispatch_edit_batch(str(p), _two_edits(lines))

        assert not result.success
        assert result.error.startswith("Tool 'edit_file' raised TypeError: kaboom")
        assert "retry" in result.error  # 단건 안전망 문구 보존
        assert len(runner.post_calls()) == 1  # 실패도 post 발화 (단건 동형)
        assert len(bridge.recent_tool_history) == 2
        assert all(not r["success"] for r in bridge.recent_tool_history)

    def test_no_hooks_plain_batch_semantics_preserved(self, tmp_path):
        """훅 미구성 시 순수 배치 의미 그대로 — 겹침 거부 all-or-nothing 포함."""
        lines = ["a", "b", "c", "d", "e"]
        p = _write(tmp_path, "f.txt", lines)
        bridge = _bridge(runner=None)
        # 정상 배치
        assert bridge.dispatch_edit_batch(str(p), _two_edits(lines)).success
        assert p.read_text().splitlines() == ["a", "B", "c", "D", "e"]
        # 겹침 배치 → 실패 + 파일 불변 (기존 apply 계약 보존)
        cur = p.read_text().splitlines()
        overlap = [
            {"op": "replace", "pos": _ref(2, cur[1]), "lines": ["X"]},
            {"op": "replace", "pos": _ref(2, cur[1]), "lines": ["Y"]},
        ]
        before = p.read_text()
        r = bridge.dispatch_edit_batch(str(p), overlap)
        assert not r.success
        assert p.read_text() == before
        # 실패도 이력에 남는다 (B1 이 오류 반복을 본다).
        assert bridge.recent_tool_history[-1]["success"] is False
