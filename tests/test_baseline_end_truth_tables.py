"""1단계 기준 보강 ⑤ — 두 종료 판정의 진리표 (v10.144.1, 테스트만).

(가) 런이 끝나는가 — `complete` 를 받아들이는가, 되돌리는가(되묻기 예산 `NAG_BUDGET`). 진짜 `MainLoop` 에 대본
provider. 되돌림 문장은 **원문 비교**다 — 4c·5 가 문구를 건드리면 여기서 걸린다. 예산은 런 하나가 쓴다
`[단계 5 에서 바뀜 — 구간 단위]`. 기다리는 회신이 남은 칸(되돌림·⏳ 통지)은 오라클 s22_dies_requested, main 이 빚진
회신을 갚지 않는 칸은 s03_agent_to_main_fallback 이 글 전체를 고정했다.

(나) 세션이 끝나는가 — run 은 `QuietPolicy.should_stop`, web 은 `web_instance_is_active`(idle 리퍼). 등록부는
생성자 인자로 받는 덕 타입이라 대역을 넣는다 — 대역이 가진 이름은 실물 클래스에도 있다고 먼저 단언한다(어긋나
조용히 초록이 되지 않게).
"""

from __future__ import annotations

import itertools
from types import SimpleNamespace

import pytest

from agent_cli.harness.auditor import SessionAuditor
from agent_cli.jobs import JobRegistry
from agent_cli.loop.ledger import NAG_BUDGET
from agent_cli.main import web_instance_is_active
from agent_cli.main_input import MainInput
from agent_cli.monitor import MonitorRegistry
from agent_cli.pump import QuietPolicy
from agent_cli.schedule.registry import ScheduleRegistry
from agent_cli.subagent.agents_live import AgentRegistry
from tests.test_engine_p2 import (
    Provider,
    call,
    records,
    session,  # noqa: F401 — 픽스처
)

# ── (가) 런이 끝나는가 ────────────────────────────────────────

_NAG_NO_ANSWERS = (
    "Observation: your result was delivered, but your `complete` did not say which "
    "items it closes (`answers` is missing), so nothing was closed:\n"
    "  2. by `complete` (last)\n"
    '     [u-0] "MAIN go"\n'
    '     → then `complete`, listing in `answers` the ids it covers ("u-0")\n'
    "     `result` is your whole answer: the answer first; when it rests on work you "
    "did, end with a `Checked:` line and a `Not checked:` line\n"
    "Close them in that order — by action first, then `complete` again with `answers` "
    "for the items it covers. ({left} more reminder{s} before the harness closes the "
    "rest: unanswered requests are reported to the user, unpaid replies get your run "
    "summary.)"
)
_NAG_EMPTY_ANSWERS = (
    "Observation: your result was delivered, but 1 item is still open:\n"
    "  2. by `complete` (last)\n"
    '     [u-0] "MAIN go"\n'
    '     → then `complete`, listing in `answers` the ids it covers ("u-0")\n'
    "     `result` is your whole answer: the answer first; when it rests on work you "
    "did, end with a `Checked:` line and a `Not checked:` line\n"
    "Close them in that order — by action first, then `complete` again with `answers` "
    "for the items it covers. ({left} more reminder{s} before the harness closes the "
    "rest: unanswered requests are reported to the user, unpaid replies get your run "
    "summary.)"
)
_LAST_NAG_EMPTY_ANSWERS = (
    "Observation: your result was delivered, but 1 item is still open:\n"
    "  2. by `complete` (last)\n"
    '     [u-0] "MAIN go"\n'
    '     → then `complete`, listing in `answers` the ids it covers ("u-0")\n'
    "     `result` is your whole answer: the answer first; when it rests on work you "
    "did, end with a `Checked:` line and a `Not checked:` line\n"
    "Close them in that order — by action first, then `complete` again with `answers` "
    "for the items it covers. (Last reminder — after this the harness closes the rest.)"
)


def _nags(ctx) -> list[str]:
    return [
        str(r.get("content") or "")
        for r in records(ctx)
        if str(r.get("content") or "").startswith(
            "Observation: your result was delivered"
        )
    ]


def _run(session_factory, steps):
    s = session_factory(Provider({"MAIN": steps}))
    result = s["run"]()
    return result, _nags(s["ctx"]), s


class TestRunEnds:
    def test_answers_cover_the_request_ends_the_run(self, session):  # noqa: F811
        result, nags, _ = _run(
            session, [call("complete", result="the answer", answers=["u-0"])]
        )
        assert (result.success, result.output, nags) == (True, "the answer", [])

    def test_missing_answers_is_sent_back_then_accepted(self, session):  # noqa: F811
        result, nags, _ = _run(
            session,
            [
                call("complete", result="first"),
                call("complete", result="second", answers=["u-0"]),
            ],
        )
        assert nags == [_NAG_NO_ANSWERS.format(left=NAG_BUDGET - 1, s="s")]
        assert (result.success, result.output) == (True, "second")

    def test_empty_answers_is_sent_back_then_accepted(self, session):  # noqa: F811
        result, nags, _ = _run(
            session,
            [
                call("complete", result="first", answers=[]),
                call("complete", result="second", answers=["u-0"]),
            ],
        )
        assert nags == [_NAG_EMPTY_ANSWERS.format(left=NAG_BUDGET - 1, s="s")]
        assert (result.success, result.output) == (True, "second")

    def test_the_budget_runs_out_and_the_harness_closes_the_request(self, session):  # noqa: F811
        """예산 8 을 다 쓰면 아홉째 `complete` 가 받아들여지고 답 말미에 미답 통지가 붙는다."""
        result, nags, _ = _run(
            session,
            [call("complete", result=f"try {i}", answers=[]) for i in range(1, 10)],
        )
        assert nags == [
            _NAG_EMPTY_ANSWERS.format(left=left, s="s" if left != 1 else "")
            for left in range(NAG_BUDGET - 1, 0, -1)
        ] + [_LAST_NAG_EMPTY_ANSWERS]
        assert result.success
        assert result.output == (
            'try 9\n\n⏳ 1 open item(s) in this run were not closed:\n   [u-0] "MAIN go"'
        )


# ── (나) 세션이 끝나는가 ──────────────────────────────────────

#: 대역 → 실물 클래스와 그 이름들
_SURFACES = {
    "registry": (AgentRegistry, ("has_active_work", "any_activity")),
    "queue": (MainInput, ("pending_count",)),
    "jobs": (JobRegistry, ("has_active_work",)),
    "monitors": (MonitorRegistry, ("has_active_work",)),
    "schedules": (ScheduleRegistry, ("has_active_work",)),
    "harness": (SessionAuditor, ("in_flight",)),
}


def test_the_stubs_name_only_what_the_real_classes_have():
    for cls, names in _SURFACES.values():
        for n in names:
            assert hasattr(cls, n), (cls.__name__, n)


def _stubs(on: set[str]) -> dict:
    return {
        "registry": SimpleNamespace(
            has_active_work=lambda: "agents" in on,
            any_activity=lambda: "agents_working" in on,
        ),
        "queue": SimpleNamespace(pending_count=lambda: 1 if "queue" in on else 0),
        "jobs": SimpleNamespace(has_active_work=lambda: "jobs" in on),
        "monitors": SimpleNamespace(has_active_work=lambda: "monitors" in on),
        "schedules": SimpleNamespace(has_active_work=lambda: "schedules" in on),
        "harness": SimpleNamespace(in_flight=1 if "harness" in on else 0),
    }


_RUN_TERMS = ("agents", "queue", "jobs", "monitors", "harness", "schedules")


def _policy(on: set[str], announced: list):
    st = _stubs(on)
    return QuietPolicy(
        main_input=st["queue"],
        agent_registry=st["registry"],
        monitors=st["monitors"],
        schedules=st["schedules"],
        harness=st["harness"],
        jobs=st["jobs"],
        on_wait=announced.append,
    )


@pytest.mark.parametrize(
    "on",
    [
        set(c)
        for n in range(len(_RUN_TERMS) + 1)
        for c in itertools.combinations(_RUN_TERMS, n)
    ],
    ids=lambda on: "+".join(sorted(on)) or "nothing",
)
def test_run_stops_only_when_every_term_is_quiet(on):
    """run: 어느 항이든 켜져 있으면 끝나지 않는다. 에이전트·큐가 조용할 때 남은 하니스 쪽 항은 종류 하나를 한
    번 알린다 — 작업 > 모니터 > 감사 > 예약 순."""
    announced: list = []
    policy = _policy(on, announced)
    assert policy.should_stop() is (not on)
    policy.should_stop()  # 같은 종류는 다시 알리지 않는다
    harness_side = [t for t in ("jobs", "monitors", "harness", "schedules") if t in on]
    if on and not ({"agents", "queue"} & on):
        assert announced == [harness_side[0]]
    else:
        assert announced == []


_WEB_TERMS = (
    "viewer",
    "main_busy",
    "agents_working",
    "agents",
    "queue",
    "jobs",
    "monitors",
    "harness",
    "schedules",
)


@pytest.mark.parametrize(
    "on",
    [
        set(c)
        for n in range(len(_WEB_TERMS) + 1)
        for c in itertools.combinations(_WEB_TERMS, n)
    ],
    ids=lambda on: "+".join(sorted(on)) or "nothing",
)
def test_web_instance_stays_while_any_web_term_is_on(on):
    """web: 뷰어·main 바쁨·에이전트 활동(`any_activity` — 미배달 회신 **제외**, 열린 사람 질문 포함)·큐·작업·
    모니터·감사·예약 — 하나라도 켜지면 걷히지 않는다. 미배달 회신만(``agents`` — `has_active_work`)은 세지
    않는다: run 과 다른 뜻이다."""
    st = _stubs(on)
    renderer = SimpleNamespace(
        hub=SimpleNamespace(has_live_connections=lambda: "viewer" in on),
        worker_is_busy=lambda: "main_busy" in on,
    )
    active = web_instance_is_active(
        renderer,
        st["queue"],
        st["registry"],
        monitors=st["monitors"],
        schedules=st["schedules"],
        harness=st["harness"],
        jobs=st["jobs"],
    )
    assert active is bool(on - {"agents"})


def test_the_agent_term_means_different_things_on_a_real_registry(tmp_path):
    """실물 레지스트리 한 칸 — main 앞 미배달 회신 하나: run 은 끝나지 않고(`has_active_work`), web 은 걷힐 수 있다
    (`any_activity` — resume 이 agents.json 에서 되살린다)."""
    reg = AgentRegistry(tmp_path, runtime={"model": "m"})
    try:
        reg.deliver(
            "main",
            mail={"kind": "job", "success": True, "output": "🧵 job #1 finished rc=0"},
            text="",
            author="",
            expects_reply=False,
        )
        assert (reg.has_active_work(), reg.any_activity()) == (True, False)
    finally:
        reg.shutdown_all()
