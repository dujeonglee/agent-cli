"""멈춘 뒤엔 도구 0회 — 도구 op 실행 직전에 런의 stop Event 를 본다 (main·상주·run 공통).

비스트리밍 provider 는 응답을 다 받은 뒤에야 돌려주므로, 응답을 받는 사이 선 정지(웹 ■·
Ctrl+C·호출자의 정지·kill)는 스트리밍 검사에 걸리지 않는다. 종전엔 그 응답의 도구가 한 번 더
실행됐다(쓰기 도구면 사용자가 멈춘 뒤 파일이 바뀐다). 여기서는 provider 가 응답을 돌려주기
직전에 정지를 세우고, 도구가 한 번도 돌지 않았는지 디스크로 확인한다.
"""

from __future__ import annotations

import json
import threading
import time

from agent_cli.context.manager import ContextManager
from agent_cli.providers.base import LLMResponse
from agent_cli.providers.capabilities import ModelCapabilities
from tests.binding_support import make_binding
from tests.loop_ports import make_ports
from tests.loopkit import run_loop

CAPS = ModelCapabilities(
    context_window=32768, max_output_tokens=4096, supports_thinking=False
)


class _StopThenCall:
    """첫 호출: ``stop()`` 을 부른 뒤(응답 도중 정지) ``reply`` 를 돌려준다. 그 뒤엔 complete."""

    def __init__(self, stop, reply: str):
        self._stop = stop
        self._reply = reply
        self.calls = 0

    def call(self, messages, system, model, capabilities, **kwargs):
        self.calls += 1
        if self.calls == 1:
            self._stop()
            return LLMResponse(content=self._reply)
        return LLMResponse(content=json.dumps({"action": "complete", "result": "x"}))


def _write(path) -> str:
    return json.dumps({"action": "write_file", "path": str(path), "content": "x\n"})


def test_main_loop_runs_no_tool_after_stop(tmp_path):
    stop = threading.Event()
    target = tmp_path / "main.txt"
    p = _StopThenCall(stop.set, _write(target))
    ctx = ContextManager(tmp_path / "s", max_context_tokens=20_000)
    out = run_loop(
        query="go",
        binding=make_binding(provider=p, capabilities=CAPS),
        ports=make_ports(),
        ctx=ctx,
        stop_event=stop,
    )
    assert not target.exists()
    assert out.error == "Interrupted by user" and p.calls == 1
    tools = [m.get("tool") for m in ctx.get_raw_messages() if m.get("tool")]
    assert tools == ["interrupt"]  # 도구 관찰 없이 interrupt 기록 하나


def test_main_loop_multi_op_runs_none_after_stop(tmp_path):
    stop = threading.Event()
    a, b = tmp_path / "a.txt", tmp_path / "b.txt"
    reply = json.dumps(
        [
            {"action": "write_file", "path": str(a), "content": "1\n"},
            {"action": "read_file", "path": str(b)},
        ]
    )
    p = _StopThenCall(stop.set, reply)
    out = run_loop(
        query="go",
        binding=make_binding(provider=p, capabilities=CAPS),
        ports=make_ports(),
        ctx=ContextManager(tmp_path / "s", max_context_tokens=20_000),
        stop_event=stop,
    )
    assert not a.exists() and out.error == "Interrupted by user"


def test_complete_is_not_blocked(tmp_path):
    """``complete`` 는 아무것도 실행하지 않는다 — 이미 쓴 답은 버리지 않는다."""
    stop = threading.Event()
    p = _StopThenCall(stop.set, json.dumps({"action": "complete", "result": "done"}))
    out = run_loop(
        query="go",
        binding=make_binding(provider=p, capabilities=CAPS),
        ports=make_ports(),
        ctx=ContextManager(tmp_path / "s", max_context_tokens=20_000),
        stop_event=stop,
    )
    assert out.success and out.output == "done"


def _registry(tmp_path, p):
    from agent_cli.subagent.agents_live import AgentRegistry

    binding = make_binding(provider=p, capabilities=CAPS)
    reg = AgentRegistry(tmp_path / "s", runtime={"binding": binding, "max_turns": 5})
    parent = ContextManager(tmp_path / "s", max_context_tokens=20_000, binding=binding)
    return reg, parent


def _until(pred, limit=10.0) -> bool:
    end = time.monotonic() + limit
    while time.monotonic() < end and not pred():
        time.sleep(0.01)
    return pred()


def test_run_agent_runs_no_tool_after_its_stop(tmp_path):
    """run 의 ■ (v10.142.0 — 부른 런의 정지에는 묶이지 않는다): 응답 도중 ■ → 도구 0회, 요청자에게 stopped 회신."""
    target = tmp_path / "run.txt"
    holder = {}
    p = _StopThenCall(lambda: holder["reg"].stop_run(holder["key"]), _write(target))
    reg, parent = _registry(tmp_path, p)
    holder["reg"] = reg
    key, err = reg.spawn(parent_ctx=parent, ends_after_task=True, parent="main")
    assert not err
    holder["key"] = key
    assert reg.request(key, "go", item_id="r-1") == ""
    try:
        assert _until(lambda: reg.get(key).state == "dead")
        assert not target.exists()
        assert p.calls == 1
        assert [m.get("re") for m in reg._pending] == ["r-1"]
        assert reg._pending[0]["mark"] == "stopped by user"
    finally:
        reg.shutdown_all()


def test_resident_runs_no_tool_after_its_run_stop(tmp_path):
    """상주의 ■ — 런만 멈추고(도구 0회) 에이전트는 남는다."""
    target = tmp_path / "res.txt"
    holder = {}
    p = _StopThenCall(lambda: holder["reg"].stop_run(holder["key"]), _write(target))
    reg, parent = _registry(tmp_path, p)
    holder["reg"] = reg
    key, err = reg.spawn(parent_ctx=parent)
    assert not err
    holder["key"] = key
    assert reg.request(key, "go", item_id="r-2") == ""
    try:
        assert _until(lambda: [m.get("re") for m in reg._pending] == ["r-2"])
        assert not target.exists()
        assert p.calls == 1
        assert reg.get(key).state != "dead"
    finally:
        reg.shutdown_all()


def test_resident_runs_no_tool_after_kill(tmp_path):
    from agent_cli.subagent.agents_live import AgentRegistry

    target = tmp_path / "resident.txt"
    holder = {}

    def stop():
        holder["tm"].stop_event.set()  # kill 이 세우는 평생 stop — 응답 도중

    p = _StopThenCall(stop, _write(target))
    binding = make_binding(provider=p, capabilities=CAPS)
    reg = AgentRegistry(tmp_path / "s", runtime={"binding": binding, "max_turns": 5})
    from agent_cli.context.manager import ContextManager as CM

    parent = CM(tmp_path / "s", max_context_tokens=20_000, binding=binding)
    key, err = reg.spawn(parent_ctx=parent)
    assert not err
    holder["tm"] = reg.get(key)
    assert reg.request(key, "go") == ""
    deadline = time.time() + 10
    while time.time() < deadline and reg.get(key).worker.is_alive():
        time.sleep(0.02)
    if reg.get(
        key
    ).worker.is_alive():  # 루프가 끝난 뒤 받은편지함 대기 — 종료 신호로 깨운다
        reg.shutdown_all()
    assert not target.exists()
    assert p.calls == 1
