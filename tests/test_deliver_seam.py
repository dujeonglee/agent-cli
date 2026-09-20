"""주소 배달 seam — `AgentRegistry.deliver` (docs/wiring/DESIGN.md §3.1, C1).

종전엔 `if addr == "main": _push_reply(...) else: request(...)` 가 **세 곳에
손으로 복제**돼 있었다(`_deliver_question`/`_deliver_answer`/`remind_owed`).
C1 은 그 분기를 하나로 접는다 — **행동 불변**이어야 한다.

"같은 두 호출을 같은 인자로 한다" 는 **추론이지 증거가 아니다**: 세 호출부는
`author` 파생도, `expects_reply` 도, `question_id` 도, 주소 표기(`_deliver_answer`
는 맨 키를 썼다)도 전부 달랐다. 그래서 여기서는 **호출부 × 백엔드 6조합의 인자를
통째로 고정**한다. 접는 과정에서 인자가 하나라도 바뀌면 걸린다.
"""

from __future__ import annotations

import pytest

import agent_cli.render as render_mod
from agent_cli.subagent.agents_live import AgentRegistry, Question
from tests.test_agents_live import RecordingRenderer, make_registry


@pytest.fixture
def renderer(monkeypatch):
    r = RecordingRenderer()
    monkeypatch.setattr(render_mod, "get_renderer", lambda: r)
    return r


@pytest.fixture
def reg(tmp_path, renderer):
    """워커 없는 레지스트리 — 배달 **인자**만 재는 자리다.

    워커를 띄우면 배달된 항목이 곧바로 런이 되어 렌더러·독촉과 뒤엉킨다.
    `_push_reply`/`request` 를 가로채므로 실제 워커는 필요 없다.
    """
    r = make_registry(tmp_path)
    yield r
    r.shutdown_all()


class _Calls:
    """배달의 두 백엔드를 가로채 인자를 그대로 기록한다."""

    def __init__(self, registry: AgentRegistry):
        self.mail: list[dict] = []
        self.requests: list[tuple] = []
        self.fail_with = ""
        registry._push_reply = self.mail.append  # type: ignore[method-assign]
        registry.request = self._request  # type: ignore[method-assign]

    def _request(self, key, message, **kw):
        self.requests.append((key, message, kw))
        return self.fail_with


@pytest.fixture
def calls(reg):
    return _Calls(reg)


def _q(**kw) -> Question:
    base = {"id": "q1", "asker": "main", "target": "agent:k1", "text": "T?"}
    base.update(kw)
    return Question(**base)


# ── ① 질문 배달 ────────────────────────────────────────


class TestDeliverQuestion:
    def test_to_main_goes_to_mailbox_with_full_structure(self, reg, calls):
        """main 은 메일박스 — **구조**를 싣는다(`answer(id)` 와 UI 가 읽는다)."""
        reg._deliver_question(_q(asker="a1", target="main"), render=False)
        assert calls.requests == []
        assert calls.mail == [
            {
                "kind": "question",
                "id": "q1",
                "key": "a1",
                "profile": "",
                "name": "",
                "success": True,
                "output": "T?",
            }
        ]

    def test_to_agent_goes_to_inbox_with_sourced_one_liner(self, reg, calls):
        """에이전트는 inbox — 평문 한 줄에 출처를 머리로 단다."""
        reg._deliver_question(_q(asker="main", target="agent:k1"), render=False)
        assert calls.mail == []
        assert calls.requests == [
            (
                "k1",
                "[question q1 from main]: T?",
                {
                    "author": "main",
                    # 산출물이 asker 에게 되돌아가면 안 된다 — 답은 answer 로만
                    "expects_reply": False,
                    "question_id": "q1",
                    "hop": 0,
                },
            )
        ]

    def test_backend_error_reaches_the_caller(self, reg, calls):
        """배달 실패가 `_deliver_question` 의 반환으로 올라와야 한다 —
        `register_question` 이 그 값을 보고 질문을 취소한다. `return` 이
        빠지면 죽은 대상에 건 질문이 영영 열린 채 남는다.
        """
        calls.fail_with = "boom"
        assert reg._deliver_question(_q(target="agent:k1"), render=False) == "boom"

    def test_peer_asker_author_is_namespaced(self, reg, calls):
        """asker 가 에이전트면 author 는 `agent:<key>` — 맨 키가 아니다."""
        reg._deliver_question(_q(asker="a1", target="agent:k1"), render=False)
        assert calls.requests[0][2]["author"] == "agent:a1"

    def test_human_target_delivers_nothing(self, reg, calls):
        """사람 주소는 ❓ 트레이가 표면 — 배달할 inbox 가 없다."""
        assert reg._deliver_question(_q(target="user"), render=False) == ""
        assert (calls.mail, calls.requests) == ([], [])

    def test_unroutable_target_keeps_its_own_message(self, reg, calls):
        """접기 전 문구 그대로 — 일반 주소 오류로 갈아끼우지 않았다."""
        err = reg._deliver_question(_q(target="nowhere"), render=False)
        assert err == "unroutable question target 'nowhere'"
        assert (calls.mail, calls.requests) == ([], [])


# ── ② 답 배달 ──────────────────────────────────────────


class TestDeliverAnswer:
    def test_to_main_mailbox_labels_the_answering_agent(self, reg, calls):
        reg._deliver_answer(_q(asker="main", target="agent:k1"), "42")
        assert calls.requests == []
        assert calls.mail == [
            {
                "kind": "answer",
                "id": "q1",
                "key": "k1",  # 맨 키 — 라벨이므로 `agent:` 접두사 없이
                "success": True,
                "output": "[answer to your question: T?]\n42",
            }
        ]

    def test_to_agent_keeps_expects_reply_true(self, reg, calls):
        """답만 `expects_reply=True` — 답 런의 산출물이 제자리로 가야 한다."""
        reg._deliver_answer(_q(asker="a1", target="main"), "42")
        assert calls.mail == []
        assert calls.requests == [
            (
                "a1",
                "[answer to your question: T?]\n42",
                {
                    "author": "main",
                    "expects_reply": True,
                    "question_id": "",
                    "hop": 0,
                },
            )
        ]


# ── ③ 독촉 배달 ────────────────────────────────────────


class TestRemindOwed:
    def _owe(self, reg, asker: str, target: str) -> None:
        q = _q(asker=asker, target=target, delivered_seq=1)
        reg._questions[q.id] = q

    def test_to_main_mailbox_is_keyed_by_the_waiting_asker(self, reg, calls):
        self._owe(reg, asker="a1", target="main")
        assert reg.remind_owed("main") == 1
        assert calls.requests == []
        (item,) = calls.mail
        assert item["kind"] == "reminder"
        assert item["key"] == "a1"  # 기다리는 쪽
        assert item["success"] is True
        assert "[q1] (from a1)" in item["output"]

    def test_to_agent_author_is_the_waiting_asker_not_the_owner(self, reg, calls):
        """발신자는 빚진 쪽이 아니라 **기다리는 쪽** — 안 그러면 창에서
        '자기가 자기에게' 로 읽힌다."""
        self._owe(reg, asker="main", target="agent:k1")
        assert reg.remind_owed("agent:k1") == 1
        assert calls.mail == []
        key, body, kw = calls.requests[0]
        assert key == "k1"
        assert "[q1] (from main)" in body
        assert kw == {
            "author": "main",
            "expects_reply": False,  # 독촉의 산출물은 어디로도 가지 않는다
            "question_id": "",
            "hop": 0,
        }


# ── seam 자체 ──────────────────────────────────────────


class TestDeliverSurface:
    def test_unknown_address_is_rejected_not_silently_dropped(self, reg, calls):
        err = reg.deliver(
            "somewhere", mail={}, text="x", author="main", expects_reply=False
        )
        assert err == "unroutable address 'somewhere'"
        assert (calls.mail, calls.requests) == ([], [])

    def test_agent_backend_propagates_the_error_string(self, reg):
        """`request` 의 에러가 호출부로 그대로 올라간다 — 삼키지 않는다.

        `"nope" in err` 로는 부족하다: `agent:` 분기를 통째로 지워도
        `"unroutable address 'agent:nope'"` 가 그 부분 문자열을 포함해
        **지운 채로 통과**한다. 백엔드가 실제로 불렸음을 문구로 고정한다.
        """
        err = reg.deliver(
            "agent:nope", mail={}, text="x", author="main", expects_reply=False
        )
        assert err.startswith("unknown agent 'nope'")
