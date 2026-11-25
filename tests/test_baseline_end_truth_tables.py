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
from agent_cli.post.office import PostOffice
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

#: 대역 → 실물(클래스, 또는 속성이 생성자에서 서는 것은 인스턴스)과 그 이름들
_SURFACES = {
    "registry": (AgentRegistry, ("has_agent_work", "open_human_questions")),
    "post": (PostOffice, ("arrived",)),
    "queue": (MainInput(), ("post",)),
    "jobs": (JobRegistry, ("has_active_work",)),
    "monitors": (MonitorRegistry, ("has_active_work",)),
    "schedules": (ScheduleRegistry, ("has_active_work",)),
    "harness": (SessionAuditor, ("in_flight",)),
}


def test_the_stubs_name_only_what_the_real_classes_have():
    for real, names in _SURFACES.values():
        for n in names:
            assert hasattr(real, n), (real, n)


def _stubs(on: set[str]) -> dict:
    """``queue`` — main 상자의 깨우는 봉투(사람의 말·깨우는 메일), ``agents`` — 에이전트 몫의 일,
    ``question`` — 에이전트가 사람에게 물은 열린 질문."""
    post = SimpleNamespace(
        arrived=lambda addr, *, waking_only, words: (
            1 if "queue" in on and addr == "main" else 0
        )
    )
    return {
        "registry": SimpleNamespace(
            has_agent_work=lambda: "agents" in on,
            open_human_questions=lambda: ["q-1"] if "question" in on else [],
        ),
        "queue": SimpleNamespace(post=post),
        "jobs": SimpleNamespace(has_active_work=lambda: "jobs" in on),
        "monitors": SimpleNamespace(has_active_work=lambda: "monitors" in on),
        "schedules": SimpleNamespace(has_active_work=lambda: "schedules" in on),
        "harness": SimpleNamespace(in_flight=1 if "harness" in on else 0),
    }


_RUN_TERMS = (
    "agents",
    "queue",
    "question",
    "jobs",
    "monitors",
    "harness",
    "schedules",
)


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
    """run: 어느 항이든 켜져 있으면 끝나지 않는다 — 열린 사람 질문도 run 의 항이다(6c — 종전엔 답할 길이 웹뿐이라
    세지 않았다). 에이전트·main 상자가 조용할 때 남은 기다림은 종류 하나를 한 번 알린다 — 작업 > 모니터 > 감사 >
    질문 > 예약 순."""
    announced: list = []
    policy = _policy(on, announced)
    assert policy.should_stop() is (not on)
    policy.should_stop()  # 같은 종류는 다시 알리지 않는다
    waits = [
        t for t in ("jobs", "monitors", "harness", "question", "schedules") if t in on
    ]
    if on and not ({"agents", "queue"} & on):
        assert announced == [waits[0].replace("question", "questions")]
    else:
        assert announced == []


_WEB_TERMS = (
    "viewer",
    "main_busy",
    "question",
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
    """web: 뷰어·main 바쁨·열린 사람 질문(웹의 호스트 항)·에이전트 몫의 일·main 상자의 깨우는 봉투·작업·모니터·
    감사·예약 — 하나라도 켜지면 걷히지 않는다. run 과 같은 세션 판정 위에 웹 항 셋이 더 있다(5b-1)."""
    st = _stubs(on)
    renderer = SimpleNamespace(
        hub=SimpleNamespace(has_live_connections=lambda: "viewer" in on),
        worker_is_busy=lambda: "main_busy" in on,
    )
    active = web_instance_is_active(
        renderer,
        SimpleNamespace(main_input=st["queue"]),
        st["registry"],
        monitors=st["monitors"],
        schedules=st["schedules"],
        harness=st["harness"],
        jobs=st["jobs"],
    )
    assert active is bool(on)


def test_waking_mail_before_main_is_live_for_run_and_web(tmp_path):
    """실물 한 칸 — main 앞 깨우는 메일 하나: run 은 끝나지 않고 웹도 걷히지 않는다. [5b-1] 종전엔 웹이 이 메일을
    세지 않았다(resume 이 agents.json 에서 되살린다는 이유) — 판정이 하나가 되어 같은 사실에 같은 답이다."""
    mi = MainInput()
    reg = AgentRegistry(tmp_path, runtime={"model": "m"}, post=mi.post)
    try:
        reg.deliver(
            "main",
            mail={"kind": "job", "success": True, "output": "🧵 job #1 finished rc=0"},
            text="",
            author="",
        )
        renderer = SimpleNamespace(
            hub=SimpleNamespace(has_live_connections=lambda: False),
            worker_is_busy=lambda: False,
        )
        run_stops = QuietPolicy(main_input=mi, agent_registry=reg).should_stop()
        web_live = web_instance_is_active(renderer, SimpleNamespace(main_input=mi), reg)
        assert (run_stops, web_live) == (False, True)
    finally:
        reg.shutdown_all()
