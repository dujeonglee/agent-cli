"""MCP 도구 호출의 상한과 런의 정지 (vNEXT).

종전엔 ``McpClientManager.call_tool`` 이 서버의 답을 끝없이 기다렸다 — 답하지 않는 서버 하나가 런을
멈춰 세웠고, ■ 정지도 그 기다림을 풀지 못했다. 이제 답·상한(서버 설정 ``call_timeout_s``, 기본
120초)·런의 정지 중 먼저 온 것으로 끝내고, 그만둔 이유를 사실 문장으로 돌려준다.

앞쪽은 기다림 함수 하나를 가짜 시계·정지 판정으로 본다(시간 단언 없음). 뒤쪽은 가짜 MCP 서버
(``tests/fixtures/slow_mcp_server.py``)를 실제 프로세스로 띄워 상한·정지·포기 뒤 재사용을 본다.
"""

from __future__ import annotations

import asyncio
import inspect
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from agent_cli.mcp.adapter import McpTool
from agent_cli.mcp.client import (
    McpCallStopped,
    McpCallTimedOut,
    McpClientManager,
    await_answer_or_give_up,
)
from agent_cli.mcp.config import (
    DEFAULT_CALL_TIMEOUT_S,
    McpServerConfig,
    _parse_server_config,
    call_timeout_problem,
)
from agent_cli.tools.base import RunContext

SERVER = Path(__file__).parent / "fixtures" / "slow_mcp_server.py"


def _ticks(*values: float):
    """부를 때마다 다음 값을 내는 가짜 시계 — 마지막 값에서 멈춘다."""
    it = iter(values)
    last = [values[0]]

    def clock() -> float:
        last[0] = next(it, last[0])
        return last[0]

    return clock


class _NeverAnswers:
    """답하지 않는 호출 — 취소되면 그 사실을 적는다."""

    def __init__(self) -> None:
        self.cancelled = False

    async def __call__(self):
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        result = loop.run_until_complete(coro)
        leftover = [t for t in asyncio.all_tasks(loop) if not t.done()]
        assert leftover == [], f"떠 있는 태스크: {leftover}"
        return result
    finally:
        loop.close()


# ── 기다림 함수 ──────────────────────────────────────────


def test_answer_comes_back():
    async def answers():
        return "the answer"

    got = _run(
        await_answer_or_give_up(answers(), limit_s=120, stopped=lambda: False, poll_s=0)
    )
    assert got == "the answer"


def test_limit_gives_up_and_cancels_the_call():
    call = _NeverAnswers()

    async def go():
        with pytest.raises(McpCallTimedOut) as e:
            await await_answer_or_give_up(
                call(),
                limit_s=120,
                stopped=lambda: False,
                clock=_ticks(0, 60, 119, 120),
                poll_s=0,
            )
        return e.value

    err = _run(go())
    assert err.limit_s == 120
    assert call.cancelled


def test_stop_gives_up_with_the_seconds_waited():
    call = _NeverAnswers()
    polls = iter([False, False, True])

    async def go():
        with pytest.raises(McpCallStopped) as e:
            await await_answer_or_give_up(
                call(),
                limit_s=120,
                stopped=lambda: next(polls),
                clock=_ticks(0, 1, 2, 3.7, 3.7),
                poll_s=0,
            )
        return e.value

    err = _run(go())
    assert err.elapsed_s == 3  # 셸의 정지 문장처럼 버림
    assert call.cancelled


def test_a_stop_set_before_the_call_gives_up_at_once():
    coro = _NeverAnswers()()

    async def go():
        with pytest.raises(McpCallStopped) as e:
            await await_answer_or_give_up(
                coro, limit_s=120, stopped=lambda: True, clock=_ticks(5), poll_s=0
            )
        return e.value

    assert _run(go()).elapsed_s == 0
    # 한 번도 돌지 않은 채 취소됐다 — 닫혔고, 떠 있지 않다
    assert inspect.getcoroutinestate(coro) == inspect.CORO_CLOSED


def test_an_error_from_the_call_passes_through():
    async def fails():
        raise ValueError("server said no")

    with pytest.raises(ValueError, match="server said no"):
        _run(
            await_answer_or_give_up(
                fails(), limit_s=120, stopped=lambda: False, poll_s=0
            )
        )


# ── 설정 키 ─────────────────────────────────────────────


def test_call_timeout_s_defaults_to_120_and_is_read_from_the_entry():
    assert DEFAULT_CALL_TIMEOUT_S == 120
    assert _parse_server_config("s", {"command": "x"}).call_timeout_s == 120
    cfg = _parse_server_config("s", {"command": "x", "call_timeout_s": 2.5})
    assert cfg.call_timeout_s == 2.5


@pytest.mark.parametrize("value", [0, -1, "30", True, None, float("inf"), [30]])
def test_unusable_call_timeout_s_is_named(value):
    assert call_timeout_problem(value) == (
        f"call_timeout_s must be a positive number of seconds, got {value!r}"
    )


@pytest.mark.parametrize("value", [1, 0.5, 600])
def test_usable_call_timeout_s(value):
    assert call_timeout_problem(value) is None


def test_unusable_call_timeout_s_fails_that_servers_connection():
    """다른 잘못된 서버 설정(command·url 없음)과 같은 길 — 그 서버의 연결 실패로 보인다."""
    m = McpClientManager()
    cfg = _parse_server_config("s", {"command": "x", "call_timeout_s": "30"})
    try:
        status = m.connect_all({"s": cfg}, warn=False)
    finally:
        m.disconnect_all()
    assert status == {
        "s": "error: Invalid config for 's': call_timeout_s must be a positive "
        "number of seconds, got '30'"
    }


# ── 모델이 읽는 문장 ──────────────────────────────────────


def _tool(manager) -> McpTool:
    return McpTool(manager, "slow", "hang", "Never answer.", {})


def test_limit_sentence():
    manager = MagicMock()
    manager.call_tool.side_effect = McpCallTimedOut(120)
    r = _tool(manager).run({})
    assert not r.success
    assert r.error == (
        "MCP mcp__slow__hang did not answer within 120s, so the call was given up. "
        "The server may still be working on it."
    )


def test_limit_sentence_keeps_a_fractional_limit():
    manager = MagicMock()
    manager.call_tool.side_effect = McpCallTimedOut(2.5)
    assert "did not answer within 2.5s," in _tool(manager).run({}).error


def test_stop_sentence_and_the_runs_stop_reaches_the_manager():
    manager = MagicMock()
    manager.call_tool.side_effect = McpCallStopped(7)
    stop = threading.Event()
    r = _tool(manager).run({}, ctx=RunContext(stopped=stop.is_set))
    assert r.error == "This run was stopped, so the MCP call was given up after 7s."
    assert manager.call_tool.call_args.kwargs["stopped"] == stop.is_set


# ── 실제 서버 프로세스 ────────────────────────────────────


def _alive(pid: int) -> bool:
    """살아 있는가 — 거둬지지 않은 좀비는 죽은 것으로 본다."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    if Path("/proc").is_dir():
        try:
            stat = Path(f"/proc/{pid}/stat").read_text()
            return stat.rsplit(")", 1)[1].split()[0] != "Z"
        except (OSError, IndexError):
            return False
    r = subprocess.run(
        ["ps", "-o", "stat=", "-p", str(pid)],
        capture_output=True,
        text=True,
        check=False,
    )
    state = r.stdout.strip()
    return bool(state) and not state.startswith("Z")


def wait_until(pred, timeout: float = 20.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.02)
    return bool(pred())


@pytest.fixture(scope="module")
def servers(tmp_path_factory):
    """같은 가짜 서버 둘 — ``quick`` 은 상한 0.5초, ``patient`` 는 60초(정지만 보려고)."""
    marks = {n: tmp_path_factory.mktemp(n) for n in ("quick", "patient")}
    configs = {
        "quick": McpServerConfig(
            name="quick",
            command=sys.executable,
            args=[str(SERVER), str(marks["quick"])],
            call_timeout_s=0.5,
        ),
        "patient": McpServerConfig(
            name="patient",
            command=sys.executable,
            args=[str(SERVER), str(marks["patient"])],
            call_timeout_s=60,
        ),
    }
    m = McpClientManager()
    status = m.connect_all(configs, warn=False)
    assert status == {"quick": "connected", "patient": "connected"}, status
    pids = [int((marks[n] / "server.pid").read_text()) for n in marks]
    baseline = len(asyncio.all_tasks(m._get_loop()))
    yield m, marks, baseline
    m.disconnect_all()
    # disconnect 가 서버 프로세스를 끝내지 못하는 경우가 있다(이 변경 전부터 — 보고에 적었다) —
    # 이 테스트가 띄운 프로세스는 이 테스트가 거둔다.
    for pid in pids:
        if _alive(pid):
            os.kill(pid, signal.SIGTERM)
    for pid in pids:
        assert wait_until(lambda p=pid: not _alive(p)), f"서버 {pid} 가 남았다"


def _fast(m, server: str, text: str) -> str:
    return McpTool(m, server, "fast", "", {}).run({"text": text}).output


def test_real_server_limit_then_the_connection_still_answers(servers):
    m, marks, baseline = servers
    r = McpTool(m, "quick", "hang", "", {}).run({"tag": "limit"})
    assert r.error == (
        "MCP mcp__quick__hang did not answer within 0.5s, so the call was given up. "
        "The server may still be working on it."
    )
    assert (marks["quick"] / "hanging-limit").exists()
    assert _fast(m, "quick", "after-limit") == "fast:after-limit"
    assert len(asyncio.all_tasks(m._get_loop())) == baseline


def test_real_server_a_late_answer_does_not_become_the_next_result(servers):
    m, marks, baseline = servers
    r = McpTool(m, "quick", "sleep", "", {}).run({"seconds": 1.0, "tag": "late"})
    assert "did not answer within 0.5s" in r.error
    # 서버는 하던 일을 끝내고 답을 보냈다 — 그 답이 다음 호출의 결과로 섞이지 않는다
    assert wait_until(lambda: (marks["quick"] / "slept-late").exists())
    assert _fast(m, "quick", "after-late") == "fast:after-late"
    assert _fast(m, "quick", "again") == "fast:again"
    assert len(asyncio.all_tasks(m._get_loop())) == baseline


def test_real_server_run_stop_gives_up_then_the_connection_still_answers(servers):
    m, marks, baseline = servers
    stop = threading.Event()

    def stop_when_hanging():
        if wait_until(lambda: (marks["patient"] / "hanging-stop").exists()):
            stop.set()

    t = threading.Thread(target=stop_when_hanging, daemon=True)
    t.start()
    r = McpTool(m, "patient", "hang", "", {}).run(
        {"tag": "stop"}, ctx=RunContext(stopped=stop.is_set)
    )
    t.join()
    assert r.error is not None
    assert r.error.startswith(
        "This run was stopped, so the MCP call was given up after "
    )
    assert r.error.endswith("s.")
    assert _fast(m, "patient", "after-stop") == "fast:after-stop"
    assert len(asyncio.all_tasks(m._get_loop())) == baseline
