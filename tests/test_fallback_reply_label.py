"""런 끝 폴백의 표지는 갚는 요청의 id 다 (v10.145.1).

종전엔 main 이 받는 폴백 머리가 ``re [<그 런을 연 배달 id>]`` 였다(`build_reply_record` 가 ``run`` 을 썼다). 그 런을
요청 아닌 것(체크인·하위 회신)이 열었거나, 런 도중 흡수한 둘째 요청의 폴백이면 main 은 본 적 없는 id 를 받았다.
워커는 문(``door``)에서 멈춘다 — 런 끝을 테스트가 직접 부른다(`end_run`).
"""

from __future__ import annotations

import threading

import pytest

from agent_cli.subagent.agent_mail import build_reply_record
from tests.test_agents_live import _FakeLoopResult, make_registry
from tests.test_engine_p2 import wait_until


@pytest.fixture
def helper(tmp_path):
    door = threading.Event()

    def runner(kind, start, ctx):
        door.wait()
        return _FakeLoopResult(output="ok"), 0.01

    reg = make_registry(tmp_path, runner=runner)
    key, _ = reg.spawn(name="helper")
    assert wait_until(lambda: reg.get(key).state == "idle")
    try:
        yield reg, key
    finally:
        door.set()
        reg.shutdown_all()


def _heads(reg) -> list[str]:
    return [
        build_reply_record(m, registry=reg)["content"].splitlines()[0]
        for m in reg.drain_replies()
    ]


def test_a_run_opened_by_a_checkin_labels_its_fallback_with_the_debt(helper):
    """앞선 런이 `wait` 로 남긴 main 의 요청 — 체크인(``n-77``)이 연 런이 회신 없이 끝나면 폴백은 그 요청에 대한 것."""
    reg, key = helper
    debt = reg.note_owes(f"agent:{key}", "main", "TASK from main")
    assert reg.end_run(f"agent:{key}", "run summary", run="n-77", success=True) == 1
    (head,) = _heads(reg)
    assert head.endswith(f"re [{debt.id}] ──") and "n-77" not in head


def test_a_second_request_absorbed_mid_run_gets_its_own_label(helper):
    """런을 연 요청과 런 도중 흡수한 둘째 요청 — 폴백 둘이 각자 자기 요청의 id 를 단다."""
    reg, key = helper
    first = reg.note_owes(f"agent:{key}", "main", "first")
    second = reg.note_owes(f"agent:{key}", "main", "second")
    assert reg.end_run(f"agent:{key}", "run summary", run=first.id, success=True) == 2
    heads = _heads(reg)
    assert [h.rsplit("re [", 1)[1] for h in heads] == [
        f"{first.id}] ──",
        f"{second.id}] ──",
    ]
