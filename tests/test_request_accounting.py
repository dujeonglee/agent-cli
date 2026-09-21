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

from agent_cli.context.manager import ContextManager
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
    def test_omitting_answers_claims_everything(self):
        """하위호환 — 종전 `complete` 은 `answers` 가 없고, 행동이 같아야 한다."""
        loop = _drain([{"id": "r1", "nickname": "", "text": "a"}])
        assert _notice(loop, result="결과") == "결과"

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
        """모르는 id 를 주장해도 터지지 않는다."""
        loop = _drain([{"id": "r1", "nickname": "", "text": "a"}])
        assert "r1" in _notice(loop, result="결과", answers=["nope"])

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
        loop = _drain([{"id": "r1", "nickname": "", "text": "a"}])
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
        from agent_cli.tools import TOOLS

        assert "answers" in TOOLS["complete"].description


# ── ④ 모델이 id 를 볼 수 있어야 한다 ──────────────────


class TestIdsReachTheModel:
    """라이브 세션 cgyx7z 가 찾은 구멍.

    `complete(answers=[id])` 는 스키마·처리·통지가 다 있었는데 **도달
    불가**였다 — 모델이 받은 것은 `[nickname]: text` 뿐이고 id 는 큐 경계에서
    죽었다. 기능은 다 있고 잇는 선 하나가 없던, 이 저장소의 그 클래스다.
    """

    def _with_ctx(self, items, tmp_path, **kw):
        q = list(items)
        loop = _loop(
            dequeue=lambda: q.pop(0) if q else None,
            ctx=ContextManager(tmp_path / "s", max_context_tokens=30_000),
            **kw,
        )
        loop._inject_queued_messages()
        return loop

    def test_ids_are_shown_when_requests_merge(self, tmp_path):
        loop = self._with_ctx(
            [
                {"id": "r1", "nickname": "Bob", "text": "첫째"},
                {"id": "r2", "nickname": "Ann", "text": "둘째"},
            ],
            tmp_path,
        )
        blob = "\n".join(_user_texts(loop))
        assert "r1" in blob and "r2" in blob, "모델이 id 를 못 본다"
        # 단어 `answers` 만 보면 안 된다 — 뒷문장에도 나와서, 지시문을 지워도
        # 통과한다(사보타주가 실제로 그렇게 새어나갔다). **행동 지시**를 고정한다.
        assert "list the ids" in blob and "complete(answers=" in blob, (
            "모델이 무엇을 해야 하는지 못 듣는다"
        )

    def test_single_request_stays_clean(self, tmp_path):
        """요청이 하나면 회계할 게 없다 — 목록을 실으면 소음이다."""
        loop = self._with_ctx(
            [{"id": "r1", "nickname": "Bob", "text": "하나뿐"}], tmp_path
        )
        assert "Outstanding requests" not in "\n".join(_user_texts(loop))

    def test_starter_is_listed_too(self, tmp_path):
        """런을 연 요청도 미답 대상이다 — 목록에서 빠지면 주장할 길이 없다.

        상태를 손으로 꾸미지 않는다: `_setup()` 이 스타터를 기록하고
        `_inject_queued_messages()` 가 드레인분을 더하는 **실제 경로**로 간다.
        """
        q = [{"id": "r2", "nickname": "Ann", "text": "둘째"}]
        loop = _loop(
            dequeue=lambda: q.pop(0) if q else None,
            ctx=ContextManager(tmp_path / "s", max_context_tokens=30_000),
            query_request_id="r1",
            query_author="Bob",
        )
        loop._setup()
        loop._inject_queued_messages()
        assert _ids(loop) == ["r1", "r2"]
        blob = "\n".join(_user_texts(loop))
        assert "r1" in blob and "r2" in blob, "스타터가 목록에서 빠졌다"

    def test_outstanding_notice_is_english(self):
        from agent_cli.constants import outstanding_requests_notice

        out = outstanding_requests_notice(
            [{"id": "r1", "author": "Bob", "text": "hello"}]
        )
        # 요청 본문(사용자 원문)은 어떤 언어든 올 수 있다 — 하네스 문구만 본다.
        harness = out.replace("hello", "")
        assert not HANGUL.findall(harness), f"통지에 한글: {harness!r}"
