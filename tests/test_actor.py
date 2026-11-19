"""`run_actor` — 런을 여는 루프 하나의 계약(가짜 액터로)."""

from __future__ import annotations

import pytest

from agent_cli.actor import run_actor
from agent_cli.main_input import CLOSED


class _Actor:
    """``takes`` 를 차례로 꺼내고 부른 것을 적는다."""

    def __init__(self, takes, *, poll_secs=None, stop_after=None, ends=()):
        self.takes = list(takes)
        self.poll_secs = poll_secs
        self.stop_after = stop_after
        self.ends = set(ends)
        self.calls: list[tuple] = []

    def should_stop(self):
        self.calls.append(("should_stop",))
        return self.stop_after is not None and len(self.calls) > self.stop_after

    def idle(self):
        self.calls.append(("idle",))

    def take(self, timeout):
        self.calls.append(("take", timeout))
        return self.takes.pop(0)

    def open(self, got):
        self.calls.append(("open", got))
        return None if got == "command" else f"start:{got}"

    def run_start(self, start):
        self.calls.append(("run", start))
        if start == "start:boom":
            raise RuntimeError("run died")
        return f"done:{start}"

    def ended(self, start, finished):
        self.calls.append(("ended", start, finished))
        return start in self.ends


def test_a_turn_is_idle_take_open_run_ended_in_that_order():
    a = _Actor(["a", CLOSED])
    run_actor(a)
    assert a.calls == [
        ("idle",),
        ("take", None),
        ("open", "a"),
        ("run", "start:a"),
        ("ended", "start:a", "done:start:a"),
        ("idle",),
        ("take", None),
    ]


def test_a_command_opens_no_run_and_the_loop_goes_on():
    a = _Actor(["command", "b", CLOSED])
    run_actor(a)
    assert [c[0] for c in a.calls].count("run") == 1


def test_a_timeout_asks_the_stop_question_again():
    a = _Actor([None, None, "x", CLOSED], poll_secs=0.5, stop_after=99)
    run_actor(a)
    assert [c[0] for c in a.calls].count("should_stop") == 4
    assert ("take", 0.5) in a.calls


def test_should_stop_ends_the_loop_and_is_not_asked_without_a_poll():
    a = _Actor(["x", "y"], poll_secs=0.5, stop_after=1)
    run_actor(a)
    assert a.calls[-1] == ("should_stop",)
    b = _Actor(["x", CLOSED], poll_secs=None)
    run_actor(b)
    assert ("should_stop",) not in b.calls


def test_ended_true_ends_the_actor():
    a = _Actor(["x", "y"], ends={"start:x"})
    run_actor(a)
    assert ("open", "y") not in a.calls


def test_the_loop_does_not_catch_a_runs_exception():
    """예외 정책은 구현의 것 — 루프는 잡지 않는다(그 런의 끝 판정도 없다)."""
    a = _Actor(["boom", CLOSED])
    with pytest.raises(RuntimeError, match="run died"):
        run_actor(a)
    assert ("ended", "start:boom", None) not in a.calls and a.calls[-1][0] == "run"


class TestRunEnd:
    """런 끝의 사실 한 모양 — 종류의 반환에서 짓는다."""

    def _r(self, **kw):
        from agent_cli.tools.result import ToolResult

        return ToolResult(kw.pop("success", True), output=kw.pop("output", "o"), **kw)

    def test_outcomes(self):
        from agent_cli.actor import RunEnd

        assert RunEnd.of(self._r()).outcome == "done"
        assert RunEnd.of(self._r(success=False)).outcome == "failed"
        assert RunEnd.of(self._r(), stopped=True).outcome == "stopped"
        assert (
            RunEnd.of(self._r(waiting=True), stopped=True).outcome == "wait"
        )  # wait 가 먼저
        assert RunEnd.of((self._r(), 0.1)).outcome == "done"  # 에이전트의 (결과, 초)
        boom = RuntimeError("x")
        end = RunEnd.crashed(boom)
        assert end.outcome == "crashed" and end.error is boom and not end.ok

    def test_output_and_result_are_kept(self):
        from agent_cli.actor import RunEnd

        r = self._r(output="answer")
        end = RunEnd.of((r, 0.2))
        assert end.output == "answer" and end.result == (r, 0.2) and end.ok
