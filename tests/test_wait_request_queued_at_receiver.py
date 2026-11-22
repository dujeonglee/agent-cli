"""요청을 보내고 받는 쪽이 **집기 전에** `wait` 하면 — 받는 쪽이 main 이든 에이전트든 (5c-2 C).

에이전트는 워커 없이 세운다. 상태는 레지스트리의 진짜 메서드(`message_handler`·`request`·`absorb_pending`·
`absorb_main`·`mark_waiting`)로만 옮기고, 판정은 `wait` 가 부르는 그 함수(`Mailbox.accept_wait`)다. 시간에 기대는
것은 없다 — "집기 전" 은 받는 쪽이 아직 꺼내지 않았다는 장부의 사실이다.
"""

from __future__ import annotations

import pytest

from agent_cli.session_slots import set_agent_registry
from agent_cli.subagent.agents_live import AgentInstance
from agent_cli.wake import Mailbox, parked_debtors
from tests.test_agents_live import make_registry

AA, BB = "agent:aa", "agent:bb"


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


def _agent(reg, key):
    """워커 없는 에이전트 — 일하는 중(런 안)으로 세운다."""
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
    tm.state = "busy"
    reg._agents[key] = tm
    return tm


def _ask(reg, sender: str, to: str, text: str) -> str:
    """``sender`` 의 `message`(회신 기대) — 그 요청의 id."""
    receipt, err = reg.message_handler(sender)(to, text)
    assert err == "", err
    return receipt.split('"')[1]


def _row_state(reg, rid: str) -> str:
    return reg._post.row(rid).state


def _wait(reg, addr: str):
    """``addr`` 의 `wait` 판정 — 받아들여지면 "will wake on" 조각들, 거절이면 None. 표시는 남기지 않는다."""
    box = Mailbox(reg, addr)
    on = box.accept_wait()
    box.clear_waiting()
    return None if on is None else on.sources


class TestReceiverHasNotPickedItUp:
    def test_c1_main_is_busy(self, reg):
        """c1 — main 이 일하는 중이라(다음 턴 경계까지 집지 않는다) 요청이 main 앞에 줄 서 있다."""
        _agent(reg, "aa")
        rid = _ask(reg, AA, "main", "which directory?")
        assert _row_state(reg, rid) == "queued"
        # 종전엔 거절("nothing would wake you … no agent owes you a reply") — main 은 명단에 없어 줄 선 요청이 어느
        # 칸에도 들지 않았다
        assert _wait(reg, AA) == ["main has your request queued"]
        assert parked_debtors(reg.wake_facts(), AA) == []

    def test_c2_main_is_waiting_in_place(self, reg):
        """c2 — main 이 aa 의 회신을 제자리에서 기다리는 중이다. aa 가 main 에게 묻고 곧바로 `wait`."""
        tm = _agent(reg, "aa")
        assert reg.request("aa", "do the job", author="main") == ""
        reg.absorb_pending(tm)  # aa 가 main 의 요청을 집었다 — aa 는 main 에게 빚졌다
        reg.mark_waiting("main")  # main 의 `wait` 가 받아들여졌다
        rid = _ask(reg, AA, "main", "which directory?")
        assert _row_state(reg, rid) == "queued"
        # 그 봉투가 main 을 깨운다 — 고리 거절이 아니다(main 은 aa 의 잠든 채무자가 아니다). 종전엔 거절
        assert _wait(reg, AA) == ["main has your request queued"]
        assert parked_debtors(reg.wake_facts(), AA) == []

    def test_c3_an_agent_is_waiting_in_place(self, reg):
        """c3 — 받는 쪽 bb 가 제자리에서 기다리는 중이다. 줄 선 요청이 bb 를 깨운다 — 조각은 하나다(종전과 같다)."""
        _agent(reg, "aa")
        _agent(reg, "bb")
        reg.mark_waiting(BB)
        rid = _ask(reg, AA, "bb", "what is x?")
        assert _row_state(reg, rid) == "queued"
        assert _wait(reg, AA) == ["agent:bb has your request queued"]
        assert parked_debtors(reg.wake_facts(), AA) == []

    def test_after_main_picks_it_up_main_owes(self, reg):
        """집은 뒤에는 빚이다(종전과 같다)."""
        _agent(reg, "aa")
        rid = _ask(reg, AA, "main", "which directory?")
        reg.absorb_main()
        reg.note_owes("main", AA, "which directory?", id=rid)
        assert _row_state(reg, rid) == "picked"
        assert _wait(reg, AA) == ["main owes you a reply"]
