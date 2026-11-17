"""런을 여는 봉투 뒤의 첫 흡수 순서 — 도착 순서 그대로 (v10.145.0, 단일 구조 2단계).

`[v10.145.0 에서 바뀜 — 2단계 예외 D2: 옛 구현은 Z, X]` 옛 워커는 깨우지 않는 항목(X — "Stop everything" 의
정지 회신 같은 ``opens_run`` 거짓)을 받은편지함에서 꺼내 들고 있다가, 런을 열기 직전 받은편지함 **끝**으로
되돌렸다(`_release_held`). 그 사이 줄 선 Z 가 있으면 첫 흡수가 Z, X 순서였다. 우편은 깨우지 않는 봉투를 줄에 둔 채
깨우는 봉투(Y)로 런을 열고, 첫 흡수가 남은 것을 도착 순서(X, Z)로 읽는다 — 모델이 읽는 글도 그 순서다.
"""

from __future__ import annotations

import threading

import pytest

import agent_cli.render as render_mod
from agent_cli.session_slots import set_agent_registry
from agent_cli.subagent.agents_live import AgentRegistry
from tests.binding_support import make_binding
from tests.test_agents_live import RecordingRenderer
from tests.test_engine_p2 import CAPS, Provider, call, wait_until


@pytest.fixture
def held_start(monkeypatch):
    """런 시작(`begin_agent_work`)을 신호가 설 때까지 잡는 렌더러 — 그 사이에 Z 를 넣는다."""
    gate = threading.Event()
    r = RecordingRenderer()
    begin = r.begin_agent_work

    def begin_agent_work(**kw):
        begin(**kw)
        assert gate.wait(10)

    r.begin_agent_work = begin_agent_work
    monkeypatch.setattr(render_mod, "get_renderer", lambda: r)
    return gate


def test_first_absorption_reads_what_waited_in_arrival_order(tmp_path, held_start):
    seen: dict = {}

    def first_call(msgs):
        seen["texts"] = [str(m.get("content") or "") for m in msgs]
        return call("complete", result="ok", answers=[])

    p = Provider({"TASK-Y": [first_call]})
    binding = make_binding(provider=p, capabilities=CAPS, model="m")
    reg = AgentRegistry(
        tmp_path / "s",
        runtime={"binding": binding, "max_turns": 5, "depth": 0, "max_depth": 2},
    )
    set_agent_registry(reg)
    try:
        key, _ = reg.spawn(name="aa")
        assert wait_until(lambda: reg.get(key).state == "idle")
        assert (
            reg.request(
                key, "X-NOTE held", expects_reply=False, kind="notice", opens_run=False
            )
            == ""
        )
        assert reg.request(key, "TASK-Y opens the run") == ""
        assert wait_until(lambda: reg.get(key).state == "busy")
        assert (
            reg.request(key, "Z-NOTE later", expects_reply=False, kind="notice") == ""
        )
        held_start.set()
        assert wait_until(lambda: "texts" in seen)
        joined = "\n".join(seen["texts"])
        assert joined.index("TASK-Y opens the run") < joined.index("X-NOTE held")
        assert joined.index("X-NOTE held") < joined.index("Z-NOTE later")
    finally:
        held_start.set()
        reg.shutdown_all()
        set_agent_registry(None)
