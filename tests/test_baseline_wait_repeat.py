"""1단계 기준 보강 — 설계 §11 의 추론 B: 반복 감지기는 `wait` 를 반복 호출로 세는가 (v10.144.1, 테스트만).

main 이 run 에이전트의 회신을 제자리에서 기다리는 동안 작업 통지가 네 번 온다 — main 은 깰 때마다 **같은 인자**로
`wait` 를 다시 부른다. 지금은: 개입이 한 번도 없고(같은 호출 반복 감지 B1 은 도구 디스패치 길에만 있고 `wait` 는 종결
op 길로 간다, StaleGuard 는 `wait` 턴을 제자리로 세지 않는다) 실패 신호도 없다. 세는 것은 턴 상한뿐이다 — 깬 뒤의
호출마다 한 턴. `[단계 5 에서 바뀜 — 구간 단위]`
"""

from __future__ import annotations

import json
import threading

from agent_cli.render import get_renderer
from tests.test_engine_p2 import (
    Provider,
    call,
    complete_all,
    owed_id,
    records,
    session,  # noqa: F401 — 픽스처
)
from tests.test_wait_checkin import FakeTimer

_NOTICES = 4


def _job_notice(reg, n: int) -> str:
    return reg.deliver(
        "main",
        mail={"kind": "job", "success": True, "output": f"🧵 job #{n} finished rc=0"},
        text="",
        author="",
        expects_reply=False,
    )


def test_repeated_identical_waits_draw_no_intervention(session, monkeypatch):  # noqa: F811
    renderer = get_renderer()
    entered = threading.Semaphore(0)
    inner = renderer.wait_in_place

    def wait_in_place(on, awaited):
        if on:
            entered.release()  # main 이 제자리 대기에 들었다 — 통지는 그 뒤에 보낸다
        return inner(on, awaited)

    monkeypatch.setattr(renderer, "wait_in_place", wait_in_place)
    release = threading.Event()
    p = Provider(
        {
            "MAIN": [call("agent", mode="run", task="TASK-K slow")]
            + [call("wait", reason="the agent")] * (_NOTICES + 1)
            + [complete_all("done")],
            "TASK-K": [
                lambda m: (
                    release.wait(10),
                    call("reply", id=owed_id(m), text="K done"),
                )[1],
                call("complete", result="ok"),
            ],
        }
    )
    s = session(p)
    reg = s["reg"]
    reg.checkin_timer_factory = FakeTimer

    def script():
        for n in range(1, _NOTICES + 1):
            assert entered.acquire(timeout=10)
            _job_notice(reg, n)
        assert entered.acquire(timeout=10)
        release.set()

    t = threading.Thread(target=script, daemon=True)
    t.start()
    result = s["run"]()
    t.join(10)
    assert result.success and result.output == "done"

    users = [
        str(r.get("content") or "").split("\n", 1)[0][:60]
        for r in records(s["ctx"])
        if r.get("role") == "user"
    ]
    waits = [u for u in users if u.startswith("Observation: waiting — will wake on:")]
    assert len(waits) == _NOTICES + 1
    assert [u for u in users if u.startswith("🧵 job #")] == [
        f"🧵 job #{n} finished rc=0" for n in range(1, _NOTICES + 1)
    ]
    # 개입 없음 — 관찰은 받아들인 `wait`·통지·회신뿐
    assert not [
        u
        for u in users
        if u.startswith("Observation:")
        and not u.startswith("Observation: waiting —")
        and not u.startswith("Observation: started agent")
    ]
    turns = s["ctx"].session_dir / "turns.jsonl"
    rows = [json.loads(ln) for ln in turns.read_text(encoding="utf-8").splitlines()]
    assert [r.get("failure_signal") for r in rows] == [None] * len(rows)
    assert len(rows) == 1 + (_NOTICES + 1) + 1  # 위임 · `wait` 다섯 · `complete`
