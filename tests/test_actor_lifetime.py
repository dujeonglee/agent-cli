"""런을 여는 루프의 수명 — 무엇이 루프를 끝내고 무엇이 계속하게 하나. 순서는 테스트가 쥔다(조건 대기).

- run 에이전트(``ends_after_task``): `wait` 로 끝난 런 뒤에는 내려오지 않고(회신이 깨운다), 그 밖의 끝(`complete`)
  뒤에 한 번 내려온다 — 부른 쪽의 ``on_task_end`` 를 마지막 런의 결과로 한 번 부르고, 되살릴 대상이 아니다.
- 상주 에이전트: 런이 끝나도 남는다.
- main(web): 런이 예외로 끝나면 표면이 삼키고 다음 차례를 받는다 — 그 런의 끝 정리는 부르지 않는다.
"""

from __future__ import annotations

import threading

from agent_cli.main_input import MainInput
from agent_cli.pump import ForeverPolicy
from agent_cli.tools.result import ToolResult
from tests.pump_support import FakeRegistry, FakeSurface, make_pump
from tests.test_agents_live import make_registry
from tests.test_engine_p2 import wait_until


def _scripted(results):
    """런마다 다음 결과를 낸다 — 런이 몇 번 돌았는지 센다."""
    queue = list(results)
    calls: list[str] = []

    def runner(kind, start, ctx):
        calls.append(start.query)
        return queue.pop(0), 0.01

    return calls, runner


class TestRunAgent:
    def test_a_wait_keeps_it_and_the_next_complete_brings_it_down_once(self, tmp_path):
        calls, runner = _scripted(
            [
                ToolResult(True, output="waiting", waiting=True),
                ToolResult(True, output="final"),
            ]
        )
        reg = make_registry(tmp_path, runner=runner)
        key, err = reg.spawn(name="r", ends_after_task=True)
        assert not err
        tm = reg.get(key)
        assert wait_until(lambda: tm.state == "idle")
        ended: list = []
        tm.on_task_end = ended.append
        assert reg.request(key, "the task") == ""
        assert wait_until(lambda: len(calls) == 1 and tm.state == "idle")
        assert tm.worker.is_alive() and ended == []  # `wait` — 내려오지 않는다
        assert reg.request(key, "the reply it waited for") == ""
        assert wait_until(lambda: not tm.worker.is_alive())
        assert len(calls) == 2 and [r.output for r in ended] == ["final"]
        assert tm.state == "dead" and tm.revivable is False and tm.handled >= 1

    def test_a_complete_brings_it_down_without_another_run(self, tmp_path):
        calls, runner = _scripted([ToolResult(True, output="done")])
        reg = make_registry(tmp_path, runner=runner)
        key, _ = reg.spawn(name="r", ends_after_task=True)
        tm = reg.get(key)
        assert wait_until(lambda: tm.state == "idle")
        assert reg.request(key, "one") == ""
        assert wait_until(lambda: not tm.worker.is_alive())
        assert len(calls) == 1 and tm.state == "dead"


class TestResidentAgent:
    def test_it_stays_after_a_complete(self, tmp_path):
        calls, runner = _scripted(
            [ToolResult(True, output="a"), ToolResult(True, output="b")]
        )
        reg = make_registry(tmp_path, runner=runner)
        key, _ = reg.spawn(name="s")
        tm = reg.get(key)
        assert wait_until(lambda: tm.state == "idle")
        assert reg.request(key, "first") == ""
        assert wait_until(lambda: len(calls) == 1 and tm.state == "idle")
        assert tm.worker.is_alive()
        assert reg.request(key, "second") == ""
        assert wait_until(lambda: len(calls) == 2 and tm.state == "idle")
        assert tm.worker.is_alive() and tm.revivable is True
        reg.shutdown_all()


class TestMainRunEnd:
    def test_a_web_main_run_that_raises_is_cleaned_up_like_a_failed_run(self):
        """런이 예외로 끝나면(web 은 삼킨다) 그 런도 끝 정리를 한다 — 실패한 런처럼(빚 정리 `run_ended`, 표면의 런 끝),
        `run_failed` 뒤에. 종전엔 정리를 건너뛰어 그 빚이 다음 런으로 넘어갔다."""
        q = MainInput()
        q.enqueue(None, "boom")
        q.enqueue(None, "fine")
        reg = FakeRegistry()
        surface = FakeSurface(swallow_errors=True)
        seen: list[str] = []

        def run_main(start):
            seen.append(start.query)
            if start.query == "boom":
                raise RuntimeError("run died")
            if len(seen) == 2:
                q.close()
            return ToolResult(True, output="ok")

        done = threading.Event()
        pump = make_pump(
            main_input=q,
            run_main=run_main,
            registry=reg,
            surface=surface,
            policy=ForeverPolicy(),
        )
        t = threading.Thread(target=lambda: (pump.run(), done.set()), daemon=True)
        t.start()
        assert done.wait(10)
        assert seen == ["boom", "fine"]
        assert reg.ended == [("main", "", False), ("main", "ok", False)]
        kinds = [c[0] for c in surface.calls]
        assert kinds.count("run_failed") == 1
        first_end = kinds.index("run_ended")
        assert (
            kinds.index("run_failed") < first_end < kinds.index("bind_stop", first_end)
        )
        crashed = next(c[1] for c in surface.calls if c[0] == "run_ended")
        assert (
            crashed.success is False
            and crashed.error == "internal error: RuntimeError: run died"
        )


class TestMainRunEndOrder:
    def test_a_cleanup_that_raises_goes_to_run_failed_then_the_stop_handle_is_released(
        self,
    ):
        """끝 정리 안의 예외도 런과 같은 보호 안 — `run_failed` 뒤에 중단 핸들을 푼다(정리를 두 번 하지 않는다)."""
        q = MainInput()
        q.enqueue(None, "hi")
        surface = FakeSurface(swallow_errors=True)
        surface.run_ended = lambda result, start: (_ for _ in ()).throw(
            RuntimeError("cleanup bug")
        )
        reg = FakeRegistry()
        pump = make_pump(
            main_input=q,
            run_main=lambda s: ToolResult(True, output="ok"),
            registry=reg,
            surface=surface,
        )
        pump.run()
        kinds = [c[0] for c in surface.calls]
        assert kinds[-2:] == ["run_failed", "bind_stop"] and surface.calls[-1] == (
            "bind_stop",
            False,
        )
        assert reg.ended == [("main", "ok", False)]  # 정리는 한 번
