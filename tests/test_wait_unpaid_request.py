"""요청자의 `wait` 를 깨우는 에이전트 — 아직 회신이 가지 않은 요청을 들고 있을 때만 (v10.143.0, 사용자 결정).

에이전트 A 가 요청자 X 에게 갚을 것이 하나도 없으면(회신했다·통지로 돈다·대기열엔 통지뿐) A 의 진행 중 런과
받은편지함은 X 의 `wait` 를 깨울 근거가 아니다 — 그 런이 끝나도 X 에게 갈 것이 없다. 종전엔 "A 가 X 의 일로 바쁘다"(`current_author`)를
그대로 근거로 쳐, 회신을 먼저 받은 main 의 `wait` 가 제자리 대신 런을 끝냈다(CI min-deps 실측). 받은편지함을
떠나는 순간이 곧 빚이다(v10.145.0 — 옛 구현은 빚을 적기 전 틈이 있었다).

에이전트는 워커 없이 세운다 — 상태는 레지스트리의 진짜 메서드(`request`·`absorb_pending`·`note_owes`·
`pay_debt`)로만 옮긴다. 시간에 기대는 것은 없다. 끝의 둘은 실제 엔진으로 모델이 받는 바이트를 본다.
"""

from __future__ import annotations

import threading

import pytest

from agent_cli.session_slots import set_agent_registry
from agent_cli.subagent.agents_live import AgentInstance
from agent_cli.wake import relation_items, wake_sources
from tests import test_engine_p2
from tests.mail_support import absorbed_mail
from tests.test_agents_live import make_registry
from tests.test_engine_p2 import (
    Provider,
    call,
    complete_all,
    contents,
    main_waits_in_place,
    owed_id,
    wait_until,
)

#: 실제 엔진의 main 세션(레지스트리·입력 큐·MainLoop) — P2 테스트의 픽스처 그대로
session = test_engine_p2.session

REFUSED = (
    "Observation: wait refused — nothing would wake you: no job or monitor of yours is "
    "running and no agent owes you a reply. Keep working, or call `complete` if the "
    "task is done. To hear from an agent, send it a request with `message` first."
)


@pytest.fixture
def reg(tmp_path, monkeypatch):
    import agent_cli.render as render_mod

    class _Quiet:
        def __getattr__(self, name):
            return lambda *a, **k: None

    monkeypatch.setattr(render_mod, "get_renderer", lambda: _Quiet())
    r = make_registry(tmp_path)
    set_agent_registry(r)
    yield r
    set_agent_registry(None)


def _idle_agent(reg, key):
    """워커 없는 에이전트 — 받은편지함에 들어간 것은 테스트가 집는다."""
    tm = AgentInstance(
        key,
        profile_name="explorer",
        role_prompt="",
        allowed_tools=None,
        model="m",
        hooks_config=None,
        context_mode="fresh",
        home_dir=reg.session_dir / key,
    )
    tm.state = "idle"
    reg._agents[key] = tm
    return tm


def _pick_up(reg, tm):
    """워커가 하는 것 — 받은편지함의 항목으로 런을 시작한다. 공개 길(`absorb_pending` — 런 시작과 같은 회계:
    처리 중으로 옮기고 요청이면 빚을 적는다)로 줄 선 것 전부를."""
    (first, *_rest) = reg.queued_for(f"agent:{tm.key}")
    tm.state = "busy"
    tm.current_author = first.src
    reg.absorb_pending(tm)
    return {"id": first.id, "author": first.src, "text": first.text}


def _relations(reg, addr) -> dict[str, str]:
    """``addr`` 를 깨울 에이전트와 그 관계(owes·working·queued) — 공개 판정(`wake.relation_items`)의 글에서."""
    kinds = (
        ("owes you a reply", "owes"),
        ("working on", "working"),
        ("queued", "queued"),
    )
    return {
        who: next(k for frag, k in kinds if frag in text)
        for who, text in relation_items(reg.wake_facts(), addr)
    }


class TestWhoWakesTheRequester:
    @pytest.mark.parametrize("requester", ["main", "agent:agt-req"])
    def test_replied_agent_still_running_does_not_wake_the_requester(
        self, reg, requester
    ):
        """① 회신한 뒤 런을 끝내지 않은 에이전트뿐이면 요청자에게 깨울 근거가 없다 (main·상주 요청자)."""
        if requester != "main":
            _idle_agent(reg, "agt-req")
        tm = _idle_agent(reg, "agt-w")
        assert reg.request("agt-w", "find callers", author=requester) == ""
        item = _pick_up(reg, tm)
        assert wake_sources(requester, agents=reg) != []  # 일하는 동안은 근거
        (d,) = reg.reply_debts("agent:agt-w")
        assert reg.pay_debt("agent:agt-w", d, "3 callers") == ""
        assert tm.state == "busy" and reg.taken_by("agent:agt-w")[0].id == item["id"]
        if requester == "main":
            absorbed_mail(reg)  # 회신은 이미 읽었다
        else:
            reg.absorb_pending(reg.get("agt-req"))  # 요청자가 회신을 읽었다
        assert wake_sources(requester, agents=reg) == []
        assert relation_items(reg.wake_facts(), requester) == []

    @pytest.mark.parametrize("requester", ["main", "agent:agt-req"])
    def test_unpaid_request_keeps_waking(self, reg, requester):
        """② 갚지 않은 요청이 남으면 계속 근거 — 둘 중 하나만 갚아도 다른 하나로."""
        if requester != "main":
            _idle_agent(reg, "agt-req")
        tm = _idle_agent(reg, "agt-w")
        reg.request("agt-w", "first", author=requester)
        reg.request("agt-w", "second", author=requester)
        # 첫 항목으로 런을 시작하고 둘째는 런 도중 흡수 — 같은 회계(`absorb_pending`)라 한 번에
        _pick_up(reg, tm)
        first = next(d for d in reg.reply_debts("agent:agt-w") if d.text == "first")
        reg.pay_debt("agent:agt-w", first, "done 1")
        relations = _relations(reg, requester)
        assert relations.get("agent:agt-w") in ("owes", "working")
        assert any("agt-w" in s for s in wake_sources(requester, agents=reg))

    def test_picking_a_request_is_owing_it(self, reg):
        """③ 받은편지함을 떠나는 순간이 곧 빚이다 — `[v10.145.0 에서 바뀜 — 옛 구현은 집은 뒤 빚을 적기까지 틈이
        있어 그 순간 근거가 "working on your request" 하나였다]`. 빚이기도 하지만 한 에이전트는 한 줄 — working(v10.146.6)."""
        tm = _idle_agent(reg, "agt-w")
        reg.request("agt-w", "find callers", author="main")
        item = _pick_up(reg, tm)
        assert [d.id for d in reg.reply_debts("agent:agt-w")] == [item["id"]]
        assert list(reg.queued_for("agent:agt-w")) == []
        assert wake_sources("main", agents=reg) == [
            "agent:agt-w working on your request"
        ]

    def test_notice_run_does_not_wake_the_sender(self, reg):
        """회신을 기대하지 않는 통지로 도는 런은 보낸 쪽의 근거가 아니다."""
        tm = _idle_agent(reg, "agt-w")
        reg.request(
            "agt-w",
            "fyi: scope changed",
            author="main",
            expects_reply=False,
            kind="notice",
        )
        _pick_up(reg, tm)
        assert tm.state == "busy" and tm.current_author == "main"
        assert wake_sources("main", agents=reg) == []

    def test_only_a_queued_notice_does_not_wake_the_sender(self, reg):
        """대기열의 통지는 근거가 아니다 — 받는 쪽이 읽어도 보낸 쪽에게 돌아올 것이 없다. 같은 자리의 요청은 근거."""
        _idle_agent(reg, "agt-w")
        reg.request(
            "agt-w",
            "fyi: scope changed",
            author="main",
            expects_reply=False,
            kind="notice",
        )
        assert len(reg.queued_for("agent:agt-w")) == 1
        assert wake_sources("main", agents=reg) == []
        reg.request("agt-w", "and count the callers", author="main")
        assert wake_sources("main", agents=reg) == [
            "agent:agt-w has your request queued"
        ]

    def test_others_requests_do_not_count(self, reg):
        """다른 요청자의 갚지 않은 요청은 그 요청자의 근거일 뿐이다. `[v10.145.0 에서 바뀜 — 옛 구현은 집은 뒤 빚을
        적기까지 틈이 있어 그 순간 근거가 "working on your request" 하나였다]`"""
        _idle_agent(reg, "agt-req")
        tm = _idle_agent(reg, "agt-w")
        reg.request("agt-w", "for agt-req", author="agent:agt-req")
        _pick_up(reg, tm)
        tm.current_author = "main"  # 화자 표시가 main 이어도
        assert wake_sources("main", agents=reg) == []
        assert wake_sources("agent:agt-req", agents=reg) == [
            "agent:agt-w working on your request"
        ]


# ── 실제 엔진 — 모델이 받는 바이트 ─────────────────────────────


class TestMainSeesIt:
    def test_main_reading_the_reply_first_gets_wait_refused_not_parked(self, session):
        """CI min-deps 에서 난 순서를 결정적으로 — 회신이 main 의 다음 경계 전에 오고, run 에이전트는 아직
        내려오지 않았다. 종전엔 그 `wait` 가 "working on your request" 로 런을 끝냈다(waiting).

        회신 순서는 테스트가 쥔다: 에이전트는 main 의 둘째 모델 호출이 시작된 뒤에야 `reply` 한다 — `agent`
        호출 뒤 경계보다 회신이 먼저 오면 그 경계가 회신을 배달해 버려 이 순서를 볼 수 없다."""
        holder: dict = {}
        main_second_call = threading.Event()
        release = threading.Event()

        def agent_reply(msgs):
            assert main_second_call.wait(10)  # main 이 `agent` 호출 뒤 경계를 지났다
            return call("reply", id=owed_id(msgs), text="3 callers: a.py, b.py, c.py")

        def agent_linger(msgs):
            assert release.wait(
                10
            )  # main 이 둘째 `wait` 의 답을 받을 때까지 내려오지 않는다
            return call("complete", result="replied")

        def main_wait_after_reply(msgs):
            main_second_call.set()
            # 회신이 main 메일함에 든 뒤 — 이 호출의 결과 뒤 경계가 그것을 먼저 배달한다
            assert wait_until(lambda: holder["reg"].arrived_for("main"))
            return call("shell", command="true")

        def main_complete(msgs):
            release.set()
            return complete_all("main finished")(msgs)

        p = Provider(
            {
                "MAIN": [
                    call("agent", mode="run", task="TASK-W find callers"),
                    main_wait_after_reply,
                    call("wait"),
                    main_complete,
                ],
                "TASK-W": [agent_reply, agent_linger],
            }
        )
        s = session(p)
        holder["reg"] = s["reg"]
        result = s["run"]()
        assert result.success and not getattr(result, "waiting", False)
        text = contents(s["ctx"])
        assert "3 callers: a.py, b.py, c.py" in text
        assert REFUSED in text
        assert "working on your request" not in text
        assert not main_waits_in_place(s["reg"])

    def test_unpaid_run_still_parks_main_in_place(self, session):
        """② 의 실제 엔진 쪽 — 회신 전의 `wait` 는 여전히 제자리에서 기다린다."""
        holder: dict = {}

        def reply_late(msgs):
            assert wait_until(lambda: main_waits_in_place(holder["reg"]))
            return call("reply", id=owed_id(msgs), text="late answer")

        p = Provider(
            {
                "MAIN": [
                    call("agent", mode="run", task="TASK-U slow"),
                    call("wait"),
                    complete_all("ok"),
                ],
                "TASK-U": [reply_late, call("complete", result="done")],
            }
        )
        s = session(p)
        holder["reg"] = s["reg"]
        result = s["run"]()
        assert result.success and not getattr(result, "waiting", False)
        text = contents(s["ctx"])
        assert "will wake on: agent:agt-" in text and "late answer" in text
        assert REFUSED not in text
