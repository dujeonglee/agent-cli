"""사용자 요청 회계 — 무엇이 답해졌는지 (docs/agent-ask 의 한 층 위).

`ask` 는 질문마다 id 가 있고 미답이 보인다. **사용자 요청엔 그게 없었다.**
웹 큐가 이미 id 를 발급하는데(`enqueue` → `{id, …}`; `cancel_pending` 이
쓴다) 루프로 오면서 `item.get("text")` 한 줄에서 버려졌다.

그래서 drain-all 이 요청 셋을 한 턴에 합치고 `complete` 하나가 나가면
**무엇이 답해졌는지 아무도 몰랐다** — 두 사람이 서로 다른 걸 물었는데 모델이
하나만 답해도 조용하다. `run_authors` 는 *누가* 물었는지만 안다.

여기서 고정하는 것은 **회계**지 강제가 아니다. `complete` 은 붙잡지 않는다.
"""

from __future__ import annotations

import re
import types
from unittest.mock import MagicMock

from agent_cli.loop import AgentLoop
from agent_cli.providers.capabilities import ModelCapabilities
from tests.loop_ports import make_ports

HANGUL = re.compile(r"[가-힣]")


def _loop(dequeue=None, route=None, **kw):
    return AgentLoop(
        query="Q",
        provider=MagicMock(),
        capabilities=ModelCapabilities(
            context_window=32768, max_output_tokens=4096, supports_thinking=False
        ),
        model="m",
        ports=make_ports(dequeue_user_message=dequeue, route_message=route),
        **kw,
    )


def _drain(items, route=None, **kw):
    """큐 아이템들을 턴 경계 주입에 흘려보내고 루프를 돌려준다."""
    q = list(items)
    loop = _loop(dequeue=lambda: q.pop(0) if q else None, route=route, **kw)
    loop._inject_queued_messages()
    return loop


def _op(**action_input):
    return types.SimpleNamespace(action="complete", action_input=action_input)


def _ids(loop):
    return [r["id"] for r in loop._state.run_requests]


def _notice(loop, **ai):
    # 두 번째 인자는 `_op_complete` 이 이미 언랩한 최종 답이다.
    return loop._dispatch._with_unanswered_notice(_op(**ai), ai.get("result", ""))


def _user_texts(loop):
    return [
        m.get("content", "")
        for m in loop.ctx.get_messages()
        if isinstance(m, dict) and m.get("role") == "user"
    ]


# ── ① id 가 런까지 온다 ────────────────────────────────


class TestRequestsReachTheRun:
    def test_drained_requests_are_recorded(self):
        loop = _drain(
            [
                {"id": "r1", "nickname": "Bob", "text": "이거 해줘"},
                {"id": "r2", "nickname": "Ann", "text": "저거도"},
            ]
        )
        assert _ids(loop) == ["r1", "r2"]
        assert loop._state.run_requests[0]["author"] == "Bob"

    def test_run_starter_is_recorded(self):
        loop = _loop(query_request_id="r0", query_author="Bob")
        loop._setup()
        assert _ids(loop) == ["r0"]

    def test_routed_command_is_not_a_pending_request(self):
        """라우팅 명령은 **이미 처리된 것**이다 — 미답으로 남으면 안 된다."""
        loop = _drain(
            [{"id": "r1", "nickname": "Bob", "text": "/skill x"}],
            route=lambda text: True,
        )
        assert _ids(loop) == []

    def test_system_wake_item_is_not_a_request(self):
        """`enqueue_system` 의 합성 깨우기는 사람 발화가 아니다."""
        loop = _drain([{"id": "w1", "nickname": "", "text": "wake", "system": True}])
        assert _ids(loop) == []

    def test_cli_has_no_requests(self):
        """CLI 는 요청이 하나뿐이라 회계할 것이 없다 — 큐 자체가 없다."""
        loop = _loop()
        loop._inject_queued_messages()
        assert _ids(loop) == []


# ── ② complete 의 주장 ────────────────────────────────


class TestClaims:
    def test_single_request_run_is_not_accounted(self):
        """요청이 하나면 회계할 것이 없다 — 결과가 곧 그 요청의 답이다.
        실측상 런의 90%가 여기고, 아무 변화도 없어야 한다."""
        loop = _drain([{"id": "r1", "nickname": "", "text": "a"}])
        assert _notice(loop, result="결과") == "결과"
        assert _notice(loop, result="결과", answers=[]) == "결과"

    def test_omitting_answers_claims_nothing(self):
        """**생략은 "전부 답함" 이 아니다** (v9.16.0).

        종전엔 하위호환을 위해 생략을 "전부 주장" 으로 쳤는데, 그러면 정작
        잡으려던 실패(하나만 답하고 넘어감)가 영영 안 보인다 — 요청을 흘린
        모델이 `answers` 를 실을 리도 없기 때문이다. 이제 꼬리가 매 턴
        요구하므로, 그래도 생략한 것은 "밝히지 않음" 으로 읽는다.
        """
        loop = _drain(
            [
                {"id": "r1", "nickname": "", "text": "a"},
                {"id": "r2", "nickname": "", "text": "b"},
            ]
        )
        out = _notice(loop, result="결과")
        assert "r1" in out and "r2" in out, "생략이 조용히 통과했다"

    def test_claiming_all_leaves_no_notice(self):
        loop = _drain(
            [
                {"id": "r1", "nickname": "", "text": "a"},
                {"id": "r2", "nickname": "", "text": "b"},
            ]
        )
        assert _notice(loop, result="결과", answers=["r1", "r2"]) == "결과"

    def test_partial_claim_surfaces_the_rest(self):
        loop = _drain(
            [
                {"id": "r1", "nickname": "Bob", "text": "첫째"},
                {"id": "r2", "nickname": "Ann", "text": "둘째"},
            ]
        )
        out = _notice(loop, result="결과", answers=["r1"])
        assert out.startswith("결과")
        assert "r2" in out and "Ann" in out and "둘째" in out
        assert "r1" not in out, "답한 요청까지 미답으로 뜬다"

    def test_unknown_id_is_ignored(self):
        """모르는 id 를 주장해도 터지지 않는다 — 아무것도 주장 안 한 것."""
        loop = _drain(
            [
                {"id": "r1", "nickname": "", "text": "a"},
                {"id": "r2", "nickname": "", "text": "b"},
            ]
        )
        out = _notice(loop, result="결과", answers=["nope"])
        assert "r1" in out and "r2" in out

    def test_single_string_answer_is_tolerated(self):
        """리스트 대신 문자열 하나를 보내는 모델 습관을 관용한다.

        요청이 **둘** 이어야 관용 여부가 드러난다 — 하나뿐이면 관용을 지워도
        `claimed is None`(전부 주장)으로 떨어져 결과가 같다(틀린 이유로 통과).
        """
        loop = _drain(
            [
                {"id": "r1", "nickname": "", "text": "a"},
                {"id": "r2", "nickname": "", "text": "b"},
            ]
        )
        out = _notice(loop, result="결과", answers="r1")
        assert "r2" in out, "문자열 주장이 무시돼 전부 주장으로 떨어졌다"
        assert "r1" not in out

    def test_no_requests_means_no_notice(self):
        loop = _loop()
        assert _notice(loop, result="결과", answers=[]) == "결과"

    def test_notice_is_english(self):
        """하네스가 붙이는 문구다 — 사람도 모델도 읽는다."""
        loop = _drain(
            [
                {"id": "r1", "nickname": "", "text": "a"},
                {"id": "r2", "nickname": "", "text": "b"},
            ]
        )
        tail = _notice(loop, result="ok", answers=[])[len("ok") :]
        assert not HANGUL.findall(tail), f"통지에 한글: {tail!r}"
        assert "not" in tail and "answered" in tail

    def test_complete_is_never_held(self):
        """미답이 남아도 결과는 **그대로 나간다**. 붙잡으면 일을 시킨 쪽이
        자기와 무관한 요청이 풀릴 때까지 결과를 못 받는다."""
        loop = _drain([{"id": "r1", "nickname": "", "text": "a"}])
        out = _notice(loop, result="최종 답", answers=[])
        assert out.startswith("최종 답"), "결과가 통지에 가려졌다"


# ── ③ 도구 표면 ───────────────────────────────────────


class TestSchema:
    def test_complete_advertises_answers(self):
        from agent_cli.tools import TOOLS

        params = TOOLS["complete"].parameters
        assert "answers" in params["properties"], "모델이 주장할 수단을 못 본다"
        assert "result" in params["required"]
        assert "answers" not in params["required"], (
            "필수로 만들면 기존 complete 이 전부 깨진다"
        )

    def test_description_tells_the_model_what_omitting_means(self):
        """단어 `answers` 만 보면 안 된다 — 지시문을 지워도 통과한다
        (사보타주가 실제로 새어나갔다). **행동과 결과**를 고정한다."""
        from agent_cli.tools import TOOLS

        desc = TOOLS["complete"].description
        assert "list the ids you actually answered" in desc, "무엇을 할지 안 말한다"
        assert "unanswered" in desc, "빠뜨리면 어떻게 되는지 안 말한다"


# ── ④ 모델이 id 를 볼 수 있어야 한다 ──────────────────


class TestIdsReachTheModel:
    """라이브 세션 cgyx7z 가 찾은 구멍, 그리고 그 첫 수정도 부족했던 것.

    `complete(answers=[id])` 는 스키마·처리·통지가 다 있었는데 **도달
    불가**였다 — 모델이 받는 것은 `[nickname]: text` 뿐이고 id 는 큐 경계에서
    죽었다. 그래서 드레인 시점에 목록을 한 번 주입했는데, **그래도 모델이
    `answers` 를 생략했다**: 한 번 주입된 줄은 턴이 길어지면 뒤로 밀리고,
    정작 `complete` 을 쓰는 순간엔 멀다.

    이제 **매 턴 꼬리**(`prompts/session_state.py`)에 싣는다 — 재현성 주의가
    가장 센 자리이고, history 에 남지 않는다.
    """

    def _tail(self, requests):
        from agent_cli.constants import outstanding_requests_block
        from agent_cli.prompts.session_state import build_session_state

        return build_session_state(
            requests=outstanding_requests_block(requests) if len(requests) > 1 else ""
        )

    def test_ids_ride_the_per_turn_tail(self):
        blob = self._tail(
            [
                {"id": "1", "author": "Bob", "text": "첫째"},
                {"id": "2", "author": "Ann", "text": "둘째"},
            ]
        )
        assert "[1]" in blob and "[2]" in blob, "모델이 id 를 못 본다"
        assert "Outstanding Requests" in blob
        # 단어가 아니라 **행동 지시**를 고정한다 — 뒷문장에도 `answers` 가
        # 나와서, 지시문을 지워도 단어 검사만으로는 통과한다.
        assert "set `answers`" in blob and "reported to" in blob

    def test_single_request_run_has_no_block(self):
        """요청이 하나면 회계할 게 없다 — 꼬리를 더럽히지 않는다."""
        blob = self._tail([{"id": "1", "author": "Bob", "text": "하나뿐"}])
        assert "Outstanding Requests" not in blob

    def test_llm_caller_feeds_the_block_only_when_merged(self):
        """렌더러가 아니라 **호출부**가 게이트를 쥔다 — 소스 핀."""
        import inspect

        from agent_cli.loop.llm import LLMCaller

        src = inspect.getsource(LLMCaller._build_session_state)
        assert "outstanding_requests_block" in src, "꼬리에 안 실린다"
        assert "len(pending) > 1" in src, "단건 런에도 실린다"
        assert "requests=requests" in src

    def test_the_block_is_not_persisted(self):
        """꼬리는 feed 시점에만 붙고 history 에 안 남는다 — 한 번 주입하던
        종전 방식은 `ctx.add` 라 resume 프리뷰까지 따라다녔다."""
        import inspect

        from agent_cli.loop import core

        src = inspect.getsource(core.AgentLoop._inject_queued_messages)
        assert "outstanding_requests_block" not in src, (
            "드레인 시점 1회 주입이 되살아났다 — 그러면 history 에 박힌다"
        )

    def test_block_is_english(self):
        from agent_cli.constants import outstanding_requests_block

        out = outstanding_requests_block(
            [{"id": "1", "author": "Bob", "text": "hello"}, {"id": "2", "text": "hi"}]
        )
        harness = out.replace("hello", "").replace("hi", "")
        assert not HANGUL.findall(harness), f"꼬리에 한글: {harness!r}"
