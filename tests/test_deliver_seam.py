"""주소 배달 seam — `AgentRegistry.deliver` (docs/wiring/DESIGN.md §3.1, C1).

종전엔 `if addr == "main": _push_reply(...) else: request(...)` 가 **세 곳에
손으로 복제**돼 있었다(`_deliver_question`/`_deliver_answer`/`kick_owed`).
C1 은 그 분기를 하나로 접었다. v10.77.0 (B): 에이전트·main 에게 가는 질문과
독촉이 없어져 그 둘은 사라졌고, 사람의 답(`_deliver_answer`)은 asker 에게 가는
요청 하나다 — 그 인자를 통째로 고정한다.
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
    base = {"id": "q1", "asker": "a1", "target": "user", "text": "T?"}
    base.update(kw)
    return Question(**base)


# ── 사람의 답 배달 ──────────────────────────────────────


class TestDeliverAnswer:
    def test_human_answer_goes_to_the_asker_as_an_answer_envelope(self, reg, calls):
        """사람 발신이라 답 런은 사람 런이다(`expects_reply=True` — 닫는 법이 사람
        요청과 같다). 봉투가 무엇에 대한 답인지(``re``) 적는다."""
        reg._deliver_answer(_q(asker="a1", target="user"), "42")
        assert calls.mail == []
        assert calls.requests == [
            (
                "a1",
                "Your question: T?\nAnswer: 42",
                {
                    "author": "user",
                    "expects_reply": True,
                    "kind": "answer",
                    "re": "q1",
                    "todo": "Continue the work it was blocking.",
                },
            )
        ]


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
