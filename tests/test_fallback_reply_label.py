"""런 끝 폴백의 표지는 갚는 요청의 id 다 (v10.145.1).

종전엔 main 이 받는 폴백 머리가 ``re [<그 런을 연 배달 id>]`` 였다(`build_reply_record` 가 ``run`` 을 썼다). 그 런을
요청 아닌 것(체크인·하위 회신)이 열었거나, 런 도중 흡수한 둘째 요청의 폴백이면 main 은 본 적 없는 id 를 받았다.
워커는 문(``door``)에서 멈춘다 — 런 끝을 테스트가 직접 부른다(`end_run`).
"""

from __future__ import annotations

import threading

import pytest

from agent_cli.subagent import conversation_log
from agent_cli.subagent.agent_mail import _NO_REPLY_LABEL as NO_REPLY_LABEL
from agent_cli.subagent.agent_mail import build_reply_record
from tests.mail_support import absorbed_mail
from tests.test_agents_live import _FakeLoopResult, make_registry
from tests.test_engine_p2 import wait_until


@pytest.fixture
def helper_door(tmp_path):
    door = threading.Event()

    def runner(kind, start, ctx):
        door.wait()
        return _FakeLoopResult(output="ok"), 0.01

    reg = make_registry(tmp_path, runner=runner)
    key, _ = reg.spawn(name="helper")
    assert wait_until(lambda: reg.get(key).state == "idle")
    try:
        yield reg, key, door
    finally:
        door.set()
        reg.shutdown_all()


@pytest.fixture
def helper(helper_door):
    reg, key, _door = helper_door
    return reg, key


def _heads(reg) -> list[str]:
    return [
        build_reply_record(m, registry=reg)["content"].splitlines()[0]
        for m in absorbed_mail(reg)
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


# ── 🤝 창의 폴백 줄(`conversation.jsonl` 의 out 줄) ──


def _out_lines(reg, key) -> list[tuple]:
    return [
        (ln["item"], ln["to"], bool(ln.get("fallback")), ln["text"].splitlines()[0])
        for ln in conversation_log.read(reg.get(key).home_dir)
        if ln["direction"] == "out"
    ]


def _run_with_two_requests(reg, key, door, *, first_author="main") -> tuple[str, str]:
    """작업 구간 하나가 요청 둘을 든다 — 첫 요청이 구간을 열고(워커는 문에서 멈춘다) 둘째는 그 구간에 흡수된다.
    문을 열어 회신 없이 끝낸다. 두 요청의 id 를 돌려준다."""
    tm = reg.get(key)
    assert reg.request(key, "first", author=first_author) == ""
    assert wait_until(lambda: tm.state == "busy" and tm.current_item)
    first = tm.current_item
    assert reg.request(key, "second") == ""
    reg.absorb_pending(tm)
    (second,) = [d.id for d in reg.reply_debts(f"agent:{key}") if d.id != first]
    door.set()
    assert wait_until(lambda: tm.state == "idle")
    return first, second


def test_the_window_shows_a_fallback_line_per_debt(helper_door):
    """폴백은 빚마다 가고(위 테스트) 창의 줄도 빚마다 하나다 — 그 빚의 id 와 상대로 (v10.165.0; 종전엔 첫 항목 하나)."""
    reg, key, door = helper_door
    first, second = _run_with_two_requests(reg, key, door)
    assert _out_lines(reg, key) == [
        (first, "main", True, NO_REPLY_LABEL),
        (second, "main", True, NO_REPLY_LABEL),
    ]


def test_a_human_run_with_a_debt_keeps_the_fallback_for_the_creditor(helper_door):
    """사람이 창에서 연 구간에 main 의 빚이 남았다 — 폴백 줄은 빚의 상대(main)에게, 사람에게는 라벨 없는 결과 줄
    (v10.165.0; 종전엔 사람 줄 하나에 폴백 라벨이 붙었다)."""
    reg, key, door = helper_door
    first, second = _run_with_two_requests(reg, key, door, first_author="user:dj")
    assert _out_lines(reg, key) == [
        (second, "main", True, NO_REPLY_LABEL),
        (first, "user:dj", False, "ok"),
    ]


def test_a_human_run_without_debts_keeps_its_one_line(helper_door):
    reg, key, door = helper_door
    tm = reg.get(key)
    assert reg.request(key, "hello", author="user:dj") == ""
    assert wait_until(lambda: tm.state == "busy" and tm.current_item)
    first = tm.current_item
    door.set()
    assert wait_until(lambda: tm.state == "idle")
    assert _out_lines(reg, key) == [(first, "user:dj", False, "ok")]
