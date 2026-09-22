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


def _with_ctx(items):
    """history 기록을 보는 테스트용 — 실제 `ContextManager` 를 단 루프."""
    import tempfile
    from pathlib import Path

    from agent_cli.context.manager import ContextManager

    ctx = ContextManager(Path(tempfile.mkdtemp()) / "s", max_context_tokens=30000)
    return _drain(items, ctx=ctx)


def _op(**action_input):
    return types.SimpleNamespace(action="complete", action_input=action_input)


def _ids(loop):
    return [r["id"] for r in loop._state.run_requests]


def _notice(loop, **ai):
    """정산(주장분 제거) → 최후 통지. `_op_complete` 이 하는 두 걸음이다.

    통지는 이제 **독촉이 끝난 자리**의 마지막 수단이라, 남은 것을 계산하는
    `_settle_requests` 를 거치지 않고는 의미가 없다.
    """
    from agent_cli.loop.dispatch import _claimed_ids

    op = _op(**ai)
    claimed = _claimed_ids(op.action_input)
    still_open = loop._dispatch._settle_requests(claimed)
    return loop._dispatch._with_unanswered_notice(
        claimed, still_open, ai.get("result", "")
    )


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
    def test_single_request_run_stays_quiet_when_undeclared(self):
        """단건 런에서 `answers` 생략은 조용히 넘어간다.

        되돌리기도 안 하는 자리라 각주가 실측 90% 런마다 뜨는데, 요청이
        하나면 결과가 곧 그 답이라 사람이 확인할 것이 없다 — 순수 소음이다.
        """
        loop = _drain([{"id": "1", "nickname": "", "text": "a"}])
        assert _notice(loop, result="결과") == "결과"

    def test_a_declared_miss_surfaces_even_in_a_single_request_run(self):
        """**모델이 직접 밝힌 미답은 건수와 무관하다** (사용자 지적).

        종전엔 `len(pending) < 2` 로 먼저 빠져나가, 단건 런에서 모델이
        `answers: []` 로 "이건 안 답했다" 고 말해도 그 진술을 삼켰다.
        생략(모름)과 신고(앎)는 다른 축이다.
        """
        loop = _drain([{"id": "1", "nickname": "Bob", "text": "a"}])
        out = _notice(loop, result="결과", answers=[])
        assert "were not answered" in out and "[1]" in out

    def test_omitting_answers_is_reported_as_undeclared_not_unanswered(self):
        """**생략은 "미답" 이 아니라 "미신고" 다.**

        라이브(xrnway) 실측: 모델은 꼬리가 매 턴 요구하는데도 `answers` 를
        안 실었고, **실제로는 둘 다 답했다**. 그때 "answered: none" 이라고
        쓰면 하네스가 **거짓을 단언**한다 — 조용한 관용보다 나쁘다.
        그렇다고 전부 답한 것으로 쳐 주면 잡으려던 실패가 안 보인다.
        아는 것만 적는다.
        """
        loop = _drain(
            [
                {"id": "1", "nickname": "Bob", "text": "a"},
                {"id": "2", "nickname": "Ann", "text": "b"},
            ]
        )
        out = _notice(loop, result="결과")
        assert "[1]" in out and "[2]" in out, "무엇이 미신고인지 안 보인다"
        assert "undeclared" in out, "미신고를 미답으로 단언하면 안 된다"
        assert "were not answered" not in out, (
            "모델이 실제로 답했을 수 있다 — 단언하면 거짓이 된다"
        )

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
        assert "were not answered" in out, (
            "모델이 직접 밝힌 미답이다 — 여기서는 단언해도 된다"
        )
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

    def test_the_description_names_the_section_the_tail_actually_renders(self):
        """설명이 가리키는 이름과 꼬리의 제목이 **같아야** 한다.

        라이브 실측: 설명은 "the tail lists Outstanding Requests" 를 보라는데
        꼬리는 `## Open Requests` 를 그린다 — 모델에게 없는 이름을 찾으라고
        하고 있었다. 두 문자열이 따로 살아 있으면 조용히 어긋난다.
        """
        from agent_cli.constants import outstanding_requests_block
        from agent_cli.tools import TOOLS

        block = outstanding_requests_block([{"id": "1", "author": "", "text": "a"}])
        heading = next(
            ln.lstrip("# ").strip() for ln in block.splitlines() if ln.startswith("#")
        )
        complete = TOOLS["complete"]
        surfaces = [complete.description] + [
            v.get("description", "") for v in complete.parameters["properties"].values()
        ]
        for text in surfaces:
            if "tail lists" in text:
                assert heading in text, (
                    f"꼬리 제목은 {heading!r} 인데 설명은 다른 이름을 가리킨다: {text!r}"
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
            requests=outstanding_requests_block(requests) if requests else ""
        )

    def test_ids_ride_the_per_turn_tail(self):
        blob = self._tail(
            [
                {"id": "1", "author": "Bob", "text": "첫째"},
                {"id": "2", "author": "Ann", "text": "둘째"},
            ]
        )
        assert "[1]" in blob and "[2]" in blob, "모델이 id 를 못 본다"
        assert "Open Requests" in blob
        # 단어가 아니라 **행동 지시**를 고정한다 — 뒷문장에도 `answers` 가
        # 나와서, 지시문을 지워도 단어 검사만으로는 통과한다.
        assert "Set `answers`" in blob and "reported to" in blob

    def test_single_request_run_also_shows_its_id(self):
        """**1건에도 싣는다** (사용자 지적).

        합쳐진 런(실측 10%)에서만 보이면 모델이 id 어휘를 배울 기회가 없다 —
        그때 처음 본 필드를 곧바로 채우라는 요구가 된다. 거부·각주는 여전히
        합쳐진 런에서만이라 90% 런의 턴·소음은 안 는다.
        """
        blob = self._tail([{"id": "1", "author": "Bob", "text": "하나뿐"}])
        assert "[1]" in blob and "Open Requests" in blob
        assert 'answers: ["1"]' in blob, "무엇을 채우라는지 예시가 없다"

    def test_llm_caller_feeds_the_block_only_when_merged(self):
        """렌더러가 아니라 **호출부**가 게이트를 쥔다 — 소스 핀."""
        import inspect

        from agent_cli.loop.llm import LLMCaller

        src = inspect.getsource(LLMCaller._build_session_state)
        assert "outstanding_requests_block" in src, "꼬리에 안 실린다"
        assert "if pending:" in src, "단건 런이 꼬리에서 빠졌다"
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


# ── ⑤ 거부 — 안내만으로는 안 됐다 ─────────────────────


class TestAnswersAreRequired:
    """라이브(xrnway): 꼬리가 매 턴 요구하는데도 모델이 `answers` 를 생략했다.

    모델은 꼬리를 **읽었다** — 자기 생각에 "실제 미완 요청은 [2],[3]뿐" 이라고
    id 까지 적었다. 그런데도 필드를 안 채웠다. 안내로는 안 되므로 형식 교정으로
    한 번 되돌린다.
    """

    def _complete(self, loop, **ai):
        op = _op(**ai)
        return loop._dispatch._op_complete("raw", object(), op, {})

    def _merged(self):
        return _drain(
            [
                {"id": "1", "nickname": "Bob", "text": "a"},
                {"id": "2", "nickname": "Ann", "text": "b"},
            ]
        )

    def test_missing_answers_is_bounced_once(self):
        loop = self._merged()
        first = loop._dispatch._require_answers("raw", _op(result="r"), {})
        assert first is not None, "생략이 그대로 통과했다"
        assert loop._state.answers_prompted is True

        # 두 번째는 받아준다 — 무한 되묻기는 런을 태운다.
        second = loop._dispatch._require_answers("raw", _op(result="r"), {})
        assert second is None, "되묻기가 한 번으로 안 끝난다"

    def test_bounce_names_the_ids_and_the_field(self):
        loop = self._merged()
        msgs = []
        loop._dispatch._intervene = lambda _t, m, *a, **k: msgs.append(m)
        loop._dispatch._require_answers("raw", _op(result="r"), {})
        (msg,) = msgs
        assert '"1"' in msg and '"2"' in msg, "어떤 id 를 대라는지 안 보인다"
        assert "answers" in msg and "Re-emit `complete`" in msg
        assert not HANGUL.findall(msg), f"개입 문구에 한글: {msg!r}"

    def test_present_answers_passes_through(self):
        loop = self._merged()
        assert (
            loop._dispatch._require_answers("raw", _op(result="r", answers=["1"]), {})
            is None
        )
        assert loop._state.answers_prompted is False, "필요 없는데 표식을 세웠다"

    def test_single_request_run_is_never_bounced(self):
        """실측 90%인 단건 런에 턴을 하나 더 쓰면 안 된다."""
        loop = _drain([{"id": "1", "nickname": "Bob", "text": "a"}])
        assert loop._dispatch._require_answers("raw", _op(result="r"), {}) is None

    def test_empty_answers_list_is_bounced(self):
        """`answers: []` 는 '아무것도 안 답함' 이 아니라 **빈칸**이다 —
        합쳐진 런에서 아무것도 안 답하고 complete 할 이유가 없다."""
        loop = self._merged()
        assert (
            loop._dispatch._require_answers("raw", _op(result="r", answers=[]), {})
            is not None
        )


class TestThroughRunLoop:
    """**실제 `run_loop` 로 한 번은 지나가 봐야 한다.**

    위의 전부는 `AgentLoop` 를 직접 세우고 디스패처 메서드를 부른다. 그게
    편한 만큼, 배선이 끊겨도 초록으로 남는다 — 이 저장소가 한 번 당한 바로
    그 클래스다(docs/wiring/DESIGN.md).

    여기서는 라이브 웹과 같은 순서로 전 경로를 태운다: 스타터가 id 를 갖고
    들어오고, 턴1 경계엔 큐가 비었다가, 턴2 경계에서 두 번째 요청이 드레인
    포트로 들어오고, 그 턴의 `complete` 이 `answers` 없이 나간다. 되돌림이
    실제로 LLM 을 한 번 더 부르는지까지 본다.
    """

    @staticmethod
    def _provider(*contents):
        from agent_cli.providers.base import LLMResponse

        p = MagicMock()
        p.call.side_effect = [LLMResponse(content=c) for c in contents]
        return p

    @staticmethod
    def _env(ops):
        import json

        return "## Thought\nt\n\n## Action\n" + json.dumps(ops)

    def _run(self, provider, *, pending, request_id="1", **kw):
        import tempfile
        from pathlib import Path

        from agent_cli.context.manager import ContextManager
        from agent_cli.loop import run_loop

        queue = list(pending)
        ctx = ContextManager(Path(tempfile.mkdtemp()) / "s", max_context_tokens=30000)
        return run_loop(
            query="REQ-A",
            query_author="Bob",
            query_request_id=request_id,
            provider=provider,
            capabilities=ModelCapabilities(
                context_window=32768,
                max_output_tokens=4096,
                supports_thinking=False,
            ),
            model="m",
            ctx=ctx,
            max_turns=6,
            wire_format="json_fc",
            ports=make_ports(
                owner="main",
                dequeue_user_message=lambda: queue.pop(0) if queue else None,
            ),
            **kw,
        )

    def test_mid_run_drain_bounces_a_bare_complete(self):
        """라이브(51awzs)에서 안 튀던 자리 — 끝까지 태워서 못박는다."""
        provider = self._provider(
            self._env([{"action": "shell", "shell_command": "echo A"}]),
            self._env([{"action": "complete", "result": "A/B done"}]),
            self._env(
                [{"action": "complete", "result": "A/B done", "answers": ["1", "2"]}]
            ),
        )
        result = self._run(
            provider,
            # 턴1 경계엔 비었고, 턴2 경계에서 도착한다 (라이브와 같은 순서).
            pending=[None, {"id": "2", "nickname": "Ann", "text": "REQ-B"}],
        )
        assert provider.call.call_count == 3, (
            "되돌림이 LLM 을 다시 부르지 않았다 — 맨 complete 이 통과했다"
        )
        assert result.output.strip() == "A/B done", result.output

    def test_partial_claim_keeps_the_loop_running(self):
        """부분 주장은 런을 끝내지 않는다 — 전 경로로 확인한다."""
        provider = self._provider(
            self._env([{"action": "shell", "shell_command": "echo A"}]),
            self._env([{"action": "complete", "result": "A done", "answers": ["1"]}]),
            self._env(
                [{"action": "complete", "result": "B done too", "answers": ["2"]}]
            ),
        )
        result = self._run(
            provider,
            pending=[None, {"id": "2", "nickname": "Ann", "text": "REQ-B"}],
        )
        assert provider.call.call_count == 3, (
            "미답 [2] 를 남기고 런이 끝났다 — 독촉이 루프를 이어가지 않았다"
        )
        assert result.output.strip() == "B done too", result.output

    def test_a_stubborn_model_cannot_burn_the_run(self):
        """같은 요청을 두 번 독촉하지 않는다 — 각주로 닫는다."""
        provider = self._provider(
            self._env([{"action": "shell", "shell_command": "echo A"}]),
            self._env([{"action": "complete", "result": "A done", "answers": ["1"]}]),
            self._env([{"action": "complete", "result": "still A", "answers": ["1"]}]),
        )
        result = self._run(
            provider,
            pending=[None, {"id": "2", "nickname": "Ann", "text": "REQ-B"}],
        )
        assert provider.call.call_count == 3, "독촉이 한 번으로 안 끝났다"
        assert "were not answered" in result.output
        assert "REQ-B" in result.output

    def test_starter_alone_is_not_bounced_through_the_loop(self):
        """단건 런은 턴을 더 쓰지 않는다 (실측 90%)."""
        provider = self._provider(self._env([{"action": "complete", "result": "done"}]))
        result = self._run(provider, pending=[])
        assert provider.call.call_count == 1
        assert "undeclared" not in result.output


# ── ⑥ 미답이 남으면 런은 끝나지 않는다 ────────────────


class TestOpenRequestsHoldTheLoop:
    """**outstanding 이 있으면 루프가 끝나면 안 된다** (사용자 지적).

    종전엔 부분 주장(`answers: ["1"]` 인데 [2] 가 열려 있음)이 각주 한 줄로
    끝났다. 그 요청은 다음 런으로도 안 넘어가고(`run_requests` 는 런 수명),
    다음 런이 온다는 보장도 없다 — 세션이 유휴로 들어가면 사라진다.

    붙잡기와 다르다: 최종답은 **먼저 렌더되고 history 에 들어간다**. 기다리는
    사람이 없으므로 "complete 을 붙잡지 않는다" 는 원칙은 그대로다.
    """

    @staticmethod
    def _merged():
        return _drain(
            [
                {"id": "1", "nickname": "Bob", "text": "첫째"},
                {"id": "2", "nickname": "Ann", "text": "둘째"},
            ]
        )

    def _complete(self, loop, **ai):
        turn = types.SimpleNamespace(thought="t")
        return loop._dispatch._op_complete("raw", turn, _op(**ai), {})

    def test_partial_claim_continues_the_loop(self):
        from agent_cli.loop.dispatch import _CONTINUE

        loop = self._merged()
        out = self._complete(loop, result="첫째 완료", answers=["1"])
        assert out is _CONTINUE, "미답이 남았는데 런이 끝났다"
        assert _ids(loop) == ["2"], "주장된 요청이 회계에서 안 지워졌다"

    def test_claimed_request_is_closed(self):
        loop = self._merged()
        self._complete(loop, result="둘 다", answers=["1", "2"])
        assert _ids(loop) == [], "전부 주장했는데 열린 채로 남았다"

    def test_all_claimed_ends_the_run(self):
        from agent_cli.loop.dispatch import _CONTINUE

        loop = self._merged()
        out = self._complete(loop, result="둘 다", answers=["1", "2"])
        assert out is not _CONTINUE, "끝낼 수 있는데 런을 붙들었다"
        assert out.output == "둘 다"

    def test_the_final_is_recorded_once(self):
        """독촉 경로에서 final 이 history 에 **한 번만** 들어간다.

        `_intervene` 의 `_append_observation` 이 이미 이 턴의 assistant
        레코드를 저장한다 — 여기서 터미널 레코드를 또 넣으면 같은 final 이
        두 번 박히고 웹 타임라인에도 카드가 두 장 뜬다(라이브 실측).
        """
        loop = _with_ctx(
            [
                {"id": "1", "nickname": "Bob", "text": "a"},
                {"id": "2", "nickname": "Ann", "text": "b"},
            ]
        )
        self._complete(loop, result="첫째 완료", answers=["1"])
        finals = [
            m for m in loop.ctx.get_raw_messages() if m.get("role") == "assistant"
        ]
        assert len(finals) == 1, f"이 턴의 assistant 레코드가 {len(finals)}개다"

    def test_result_is_delivered_before_the_nag(self):
        """붙잡기가 아니다 — 답은 독촉 **전에** 렌더된다."""
        loop = _with_ctx(
            [
                {"id": "1", "nickname": "Bob", "text": "a"},
                {"id": "2", "nickname": "Ann", "text": "b"},
            ]
        )
        order = []
        import agent_cli.loop.dispatch as D

        real = D.render_step
        D.render_step = lambda kind, text, *a, **k: order.append((kind, text))
        loop._dispatch._intervene = lambda *a, **k: order.append(("nag", ""))
        try:
            self._complete(loop, result="첫째 완료", answers=["1"])
        finally:
            D.render_step = real
        assert order[0] == ("final", "첫째 완료"), f"답이 먼저 안 나갔다: {order}"
        assert ("nag", "") in order, "독촉이 없었다"

    def test_nag_names_the_open_ids_and_is_english(self):
        # 요청 본문은 사용자 원문이라 한글이 정당하게 섞인다 — 하네스 문구만
        # 보려고 영문 요청을 쓴다.
        loop = _drain(
            [
                {"id": "1", "nickname": "Bob", "text": "first"},
                {"id": "2", "nickname": "Ann", "text": "second"},
            ]
        )
        msgs = []
        loop._dispatch._intervene = lambda _t, m, *a, **k: msgs.append(m)
        self._complete(loop, result="r", answers=["1"])
        (msg,) = msgs
        assert '"2"' in msg and "Ann" in msg, "무엇이 남았는지 안 보인다"
        assert "still unanswered" in msg and "complete" in msg
        assert not HANGUL.findall(msg), f"독촉 문구에 한글: {msg!r}"

    def test_nag_is_once_per_request(self):
        """고집 센 모델과 물려 런을 태우면 안 된다 — 요청당 한 번."""
        from agent_cli.loop.dispatch import _CONTINUE

        loop = self._merged()
        assert self._complete(loop, result="r", answers=["1"]) is _CONTINUE
        second = self._complete(loop, result="r", answers=[])
        assert second is not _CONTINUE, "같은 요청을 두 번 독촉했다"
        assert "were not answered" in second.output, "포기했으면 각주는 남겨야 한다"

    def test_undeclared_is_never_nagged(self):
        """무엇이 남았는지 모르면 '남은 걸 해라'는 말에 근거가 없다."""
        from agent_cli.loop.dispatch import _CONTINUE

        loop = self._merged()
        loop._state.answers_prompted = True  # 되돌림은 이미 한 번 썼다
        out = self._complete(loop, result="r")
        assert out is not _CONTINUE
        assert "undeclared" in out.output

    def test_single_request_run_is_untouched(self):
        from agent_cli.loop.dispatch import _CONTINUE

        loop = _drain([{"id": "1", "nickname": "", "text": "a"}])
        out = self._complete(loop, result="답")
        assert out is not _CONTINUE and out.output == "답"


# ── ⑦ 주장은 history 에 남는다 ────────────────────────


class TestClaimsSurviveHistory:
    """`answers` 가 history 에 남아야 한다.

    종전엔 터미널 레코드를 `(thought, result)` 로 **재구성**해서 주장이 통째로
    사라졌다 — 인스펙터·resume·감사 어디서도 모델이 무엇을 답했다고 했는지 볼
    수 없었고, 라이브 조사에서 그 재구성본을 "모델이 안 보냈다" 는 증거로
    읽었다.
    """

    @staticmethod
    def _formats():
        from agent_cli import wire_formats

        return [wire_formats.get(n) for n in ("json_fc", "xml_fc")]

    @staticmethod
    def _inputs(rec):
        if rec.get("ops"):
            return [o["action_input"] for o in rec["ops"]]
        return [rec["action_input"]]

    def test_claims_are_stored(self):
        for wf in self._formats():
            rec = wf.serialize_terminal_for_history("t", "r", answers=["1", "2"])
            (ai,) = self._inputs(rec)
            assert ai["answers"] == ["1", "2"], f"{wf}: 주장이 버려졌다"

    def test_absent_claim_stores_no_key(self):
        """부재와 빈 주장은 다르다 — 없던 키를 만들면 resume 이 거짓을 읽는다."""
        for wf in self._formats():
            rec = wf.serialize_terminal_for_history("t", "r")
            (ai,) = self._inputs(rec)
            assert "answers" not in ai, f"{wf}: 없던 주장을 지어냈다"

    def test_empty_claim_is_preserved(self):
        for wf in self._formats():
            rec = wf.serialize_terminal_for_history("t", "r", answers=[])
            (ai,) = self._inputs(rec)
            assert ai.get("answers") == [], f"{wf}: '아무것도 안 답함' 이 지워졌다"

    def test_op_complete_writes_the_claim(self):
        loop = _with_ctx(
            [
                {"id": "1", "nickname": "", "text": "a"},
                {"id": "2", "nickname": "", "text": "b"},
            ]
        )
        loop._dispatch._op_complete(
            "raw",
            types.SimpleNamespace(thought="t"),
            _op(result="둘 다", answers=["1", "2"]),
            {},
        )
        rec = [
            m
            for m in loop.ctx.get_raw_messages()
            if m.get("role") == "assistant" and m.get("ops")
        ][-1]
        assert rec["ops"][0]["action_input"]["answers"] == ["1", "2"]


# ── ⑧ "이 답이 무엇에 대한 답인가" ────────────────────


class TestFinalCarriesTheRequests:
    """최종답 렌더에 **답한 요청**을 실어 준다.

    합쳐진 런에서는 카드 위치가 알려주지 않는다 — 요청 둘이 한 턴에 들어오고
    최종답은 하나다. `complete` 의 `answers` id 를 `{id, author, text}` 로
    풀어 렌더에 넘긴다.
    """

    @staticmethod
    def _merged():
        return _drain(
            [
                {"id": "1", "nickname": "Bob", "text": "첫째"},
                {"id": "2", "nickname": "Ann", "text": "둘째"},
            ]
        )

    def _final_calls(self, loop, **ai):
        import agent_cli.loop.dispatch as D

        seen = []
        real = D.render_step
        D.render_step = lambda kind, text, *a, **k: seen.append((kind, k))
        loop._dispatch._intervene = lambda *a, **k: None
        try:
            loop._dispatch._op_complete(
                "raw", types.SimpleNamespace(thought="t"), _op(**ai), {}
            )
        finally:
            D.render_step = real
        return [k.get("requests") for kind, k in seen if kind == "final"]

    def test_claimed_requests_reach_the_renderer(self):
        loop = self._merged()
        (reqs,) = self._final_calls(loop, result="둘 다", answers=["1", "2"])
        assert [r["id"] for r in reqs] == ["1", "2"]
        assert [r["author"] for r in reqs] == ["Bob", "Ann"]

    def test_only_the_claimed_ones(self):
        loop = self._merged()
        (reqs,) = self._final_calls(loop, result="첫째만", answers=["1"])
        assert [r["id"] for r in reqs] == ["1"], "주장 안 한 요청까지 실렸다"

    def test_captured_before_settle_removes_them(self):
        """`_settle_requests` 가 주장분을 지우므로 **그 전에** 뽑아야 한다."""
        loop = self._merged()
        (reqs,) = self._final_calls(loop, result="둘 다", answers=["1", "2"])
        assert _ids(loop) == [], "정산이 안 돌았다 — 테스트가 순서를 안 본다"
        assert len(reqs) == 2, "정산 뒤에 뽑아서 빈 목록이 됐다"

    def test_undeclared_carries_nothing(self):
        """생략은 모름이다 — 추측해서 칩을 그리면 거짓이 된다."""
        loop = self._merged()
        loop._state.answers_prompted = True
        (reqs,) = self._final_calls(loop, result="r")
        assert not reqs

    def test_unknown_id_resolves_to_nothing(self):
        loop = self._merged()
        (reqs,) = self._final_calls(loop, result="r", answers=["nope"])
        assert reqs == []


class TestRequestIdOnUserRecords:
    """user 레코드의 `request_id` — 회계의 빠져 있던 연결고리.

    id 는 `run_requests` 까지만 가고 레코드엔 안 찍혀서, 디스크의 어떤 것도
    "이 `answers` 주장이 어느 user 턴을 가리키는지" 를 말할 수 없었다.
    """

    def test_drained_request_is_stamped(self):
        loop = _with_ctx([{"id": "7", "nickname": "Bob", "text": "a"}])
        rec = [m for m in loop.ctx.get_raw_messages() if m.get("role") == "user"][-1]
        assert rec["request_id"] == "7"

    def test_routed_command_is_not_stamped(self):
        """라우팅 명령은 요청이 아니다 — 회계와 같은 문."""
        loop = _drain(
            [{"id": "7", "nickname": "Bob", "text": "/sh ls"}],
            route=lambda _t: True,
        )
        assert _ids(loop) == []

    def test_system_wake_is_not_stamped(self):
        import tempfile
        from pathlib import Path

        from agent_cli.context.manager import ContextManager

        ctx = ContextManager(Path(tempfile.mkdtemp()) / "s", max_context_tokens=30000)
        loop = _drain(
            [{"id": "7", "nickname": "", "text": "wake", "system": True}], ctx=ctx
        )
        rec = [m for m in loop.ctx.get_raw_messages() if m.get("role") == "user"][-1]
        assert "request_id" not in rec, "합성 wake 가 사용자 요청으로 찍혔다"

    def test_replay_resolves_ids_back_to_requests(self):
        """resume 에서도 같은 칩이 뜬다 — id → 요청 맵을 레코드에서 재구성."""
        from agent_cli.render.web import WebRenderer

        r = WebRenderer.__new__(WebRenderer)
        r._replay_requests = {}
        seen = []
        r.final = lambda text, turn=0, requests=None: seen.append((text, requests))
        r._replay_requests["2"] = {"id": "2", "author": "Ann", "text": "둘째"}
        r._replay_assistant_op(
            "complete", {"result": "done", "answers": ["2", "없는id"]}
        )
        (text, reqs) = seen[0]
        assert text == "done"
        assert [x["id"] for x in reqs] == ["2"], "모르는 id 를 지어냈다"

    def test_pre_v9_17_session_gets_no_chips(self):
        """`answers` 가 없던 세션은 칩도 없다 — 위치로 추측하지 않는다."""
        from agent_cli.render.web import WebRenderer

        r = WebRenderer.__new__(WebRenderer)
        r._replay_requests = {"1": {"id": "1", "author": "Bob", "text": "a"}}
        seen = []
        r.final = lambda text, turn=0, requests=None: seen.append(requests)
        r._replay_assistant_op("complete", {"result": "done"})
        assert seen == [[]]


# ── ⑨ 에이전트 루프는 건드리지 않는다 ────────────────


class TestAgentLoopsAreUntouched:
    """회계·되돌림·독촉·칩은 **웹 main 루프에만** 걸린다 (사용자 우려).

    `run_requests` 를 채우는 입구는 둘뿐이다 — 스타터의 `query_request_id`
    와 드레인 포트 `dequeue_user_message`. 둘 다 `main.py` 의 웹 워커만
    넘긴다. 상주·일회성·스킬 루프의 포트 빌더는 전부 `dequeue_user_message=
    None` 이고 `query_request_id` 는 기본값 "" 이다. 그래서 그 루프들에선
    목록이 항상 비고, 새 기계가 전부 조용하다.

    여기서 **프로덕션 빌더**(`runtime.ports_for_resident` / `ports_for_oneshot`)
    로 직접 돌린다 — 나중에 누가 에이전트 러너에 id 를 넘기면 독촉이
    상주 에이전트에 조용히 켜지는 부류라, 테스트 스텁이 아니라 실제
    조립 지점을 못 박는다.
    """

    @staticmethod
    def _env(ops):
        import json

        return "## Thought\nt\n\n## Action\n" + json.dumps(ops)

    def _run_agent(self, ports, *contents):
        import tempfile
        from pathlib import Path

        from agent_cli.context.manager import ContextManager
        from agent_cli.loop import run_loop
        from agent_cli.providers.base import LLMResponse

        p = MagicMock()
        p.call.side_effect = [LLMResponse(content=c) for c in contents]
        ctx = ContextManager(Path(tempfile.mkdtemp()) / "s", max_context_tokens=30000)
        # 상주 에이전트의 배치는 요청 여럿을 `query` 하나로 합친다
        # (`process_batch`). 그 모양 그대로.
        res = run_loop(
            query="(Several messages arrived together)\n\n[main]: 일감 A\n\n[main]: 일감 B",
            query_author="",
            provider=p,
            capabilities=ModelCapabilities(
                context_window=32768, max_output_tokens=4096, supports_thinking=False
            ),
            model="m",
            ctx=ctx,
            max_turns=4,
            wire_format="json_fc",
            ports=ports,
        )
        return p, ctx, res

    @staticmethod
    def _resident():
        from agent_cli.runtime import ports_for_resident

        return ports_for_resident(key="agt-x", message_handler=None, questions=None)

    @staticmethod
    def _oneshot():
        from agent_cli.runtime import ports_for_oneshot

        return ports_for_oneshot(owner="agent:agt-x")

    def test_resident_complete_without_answers_is_not_bounced_or_nagged(self):
        p, _ctx, res = self._run_agent(
            self._resident(),
            self._env([{"action": "complete", "result": "둘 다 끝"}]),
        )
        assert p.call.call_count == 1, "에이전트 complete 이 되돌려지거나 독촉됐다"
        assert res.output == "둘 다 끝", "에이전트 최종답에 각주가 붙었다"

    def test_oneshot_complete_without_answers_is_not_bounced_or_nagged(self):
        p, _ctx, res = self._run_agent(
            self._oneshot(),
            self._env([{"action": "complete", "result": "끝"}]),
        )
        assert p.call.call_count == 1
        assert res.output == "끝"

    def test_a_spurious_answers_field_from_an_agent_is_inert(self):
        """`complete` 설명에 `answers` 가 보이니 에이전트 모델이 흉내 낼 수
        있다 — 셀 요청이 없으니 아무 일도 일어나지 않아야 한다."""
        p, _ctx, res = self._run_agent(
            self._resident(),
            self._env([{"action": "complete", "result": "끝", "answers": ["1", "2"]}]),
        )
        assert p.call.call_count == 1
        assert res.output == "끝"

    def test_agent_prompt_carries_no_open_requests_tail(self):
        from agent_cli.loop import AgentLoop

        loop = AgentLoop(
            query="[main]: 일감 A\n\n[main]: 일감 B",
            provider=MagicMock(),
            capabilities=ModelCapabilities(
                context_window=32768, max_output_tokens=4096, supports_thinking=False
            ),
            model="m",
            ports=self._resident(),
        )
        loop._setup()
        assert loop._state.run_requests == [], "에이전트 루프에 회계가 생겼다"
        blob = loop._llm._build_session_state(30000)
        assert "Open Requests" not in blob, "에이전트 꼬리에 요청 목록이 떴다"

    def test_agent_user_records_carry_no_request_id(self):
        _p, ctx, _res = self._run_agent(
            self._resident(),
            self._env([{"action": "complete", "result": "끝"}]),
        )
        users = [m for m in ctx.get_raw_messages() if m.get("role") == "user"]
        assert users and all("request_id" not in m for m in users)

    def test_agent_final_renders_no_request_chips(self):
        import agent_cli.loop.dispatch as D

        seen = []
        real = D.render_step
        D.render_step = lambda kind, text, *a, **k: seen.append((kind, k))
        try:
            self._run_agent(
                self._resident(),
                self._env([{"action": "complete", "result": "끝", "answers": ["1"]}]),
            )
        finally:
            D.render_step = real
        finals = [k for kind, k in seen if kind == "final"]
        assert finals and not finals[0].get("requests"), "에이전트 final 에 칩이 붙었다"
