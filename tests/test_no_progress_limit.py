"""진전 없는 응답의 연속 상한 (PROP-no-progress-turns, v10.177.14).

진전 없음은 다섯이다 — 버려진 응답(형식 되묻기·홀로 거절된 호출·B1 넛지), 호출이 전부 실행 전에 거절된 응답
(`wait` 거절 포함), 결과가 모두 이미 본 것인 응답(StaleGuard 의 제자리 턴), 코어가 끊은 응답(출력 절단·폭주·스트림
서버 오류). 연달아 :data:`NO_PROGRESS_LIMIT` 번이면 런이 끝난다(v10.177.10 은 버려진 응답만 셌다 — 나머지 넷은
``max_turns`` 0 에서 끝나지 않았다). 가짜 provider 는 상한(:data:`CAP`)에서 멈춘다 — 그때까지 끝나지 않으면
:class:`ProviderCap` 이 난다."""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from agent_cli import confirm
from agent_cli.context.manager import ContextManager
from agent_cli.dialects import get
from agent_cli.loop import dispatch as dispatch_module
from agent_cli.loop.dispatch import NO_PROGRESS_LIMIT, TurnDispatcher
from agent_cli.loop.state import LoopState
from agent_cli.loop.tool_bridge import ToolBridge
from agent_cli.providers.base import LLMResponse
from agent_cli.providers.capabilities import ModelCapabilities
from tests.loop_ports import TEST_PORTS, make_ports, queue_inbox
from tests.loopkit import make_config, run_loop

CAPS = ModelCapabilities(
    context_window=32768, max_output_tokens=4096, supports_thinking=False
)
#: 가짜 provider 의 응답 수 상한 — 넘으면 멈춘다(런이 끝나지 않았다는 뜻)
CAP = 30
DIALECTS = ("json_fc", "native_fc")


class ProviderCap(BaseException):
    """가짜 provider 가 상한에 닿았다 — 런이 그때까지 끝나지 않았다. ``BaseException`` 인 까닭: 엔진은 공급자의
    ``Exception`` 을 "LLM call failed" 로 받아 런을 끝낸다 — 그 길로 새면 끝난 것처럼 보인다."""


def call(dialect: str, tool: str, args: dict) -> LLMResponse:
    if dialect == "native_fc":
        return LLMResponse(
            content="ok", tool_calls=[{"id": "c0", "name": tool, "input": args}]
        )
    return LLMResponse(content="x\n\n" + json.dumps([{"action": tool, **args}]))


def prose(dialect: str) -> LLMResponse:
    return LLMResponse(content="I am thinking about it.")


def bad_arguments(dialect: str) -> LLMResponse:
    """native_fc: 인자가 JSON 이 아닌 호출 / json_fc: 깨진 배열(NO_JSON)."""
    if dialect == "native_fc":
        return LLMResponse(
            content="ok",
            tool_calls=[
                {"id": "c0", "name": "read_file", "input": None, "arguments": "{bad"}
            ],
        )
    return LLMResponse(content='x\n\n[{"action": "read_file", "path": }]')


FAILED_EDIT = (
    "edit_file",
    {"path": "f.py", "op": "replace", "pos": "2#ZZ", "lines": ["x"]},
)
REFUSED_EDIT = (
    "edit_file",
    {"path": "f.py", "op": "bogus", "pos": "2#ZZ", "lines": ["x"]},
)


def run(tmp_path, monkeypatch, dialect, responses, *, ports=TEST_PORTS, **attrs):
    """``responses``: 응답 목록 — 마지막 것을 끝없이 되풀이한다. (결과, 호출 수); 상한이면 ProviderCap.
    ``attrs`` 는 ``run_loop`` 의 설정·시작 귀속(``query_request_id`` 등)."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "f.py").write_text("a\nb\nc\n")
    n = [0]

    def respond(**kw):
        n[0] += 1
        if n[0] > CAP:
            raise ProviderCap
        return responses[min(n[0], len(responses)) - 1]

    provider = MagicMock()
    provider.call.side_effect = respond
    ctx = ContextManager(tmp_path / "s", max_context_tokens=30000, dialect=get(dialect))
    result = run_loop(
        ports=ports,
        query="q",
        provider=provider,
        capabilities=CAPS,
        model="m",
        dialect=get(dialect),
        ctx=ctx,
        **attrs,
    )
    return result, n[0]


END = f"No progress: the last {NO_PROGRESS_LIMIT} responses produced nothing new."


def ends_at(tmp_path, monkeypatch, dialect, responses):
    result, calls = run(tmp_path, monkeypatch, dialect, responses)
    assert result.success is False and result.error == END
    return calls


@pytest.mark.parametrize("dialect", DIALECTS)
class TestNoProgressEndsTheRun:
    def test_same_prose_ends_at_the_limit(self, tmp_path, monkeypatch, dialect):
        assert ends_at(tmp_path, monkeypatch, dialect, [prose(dialect)]) == (
            NO_PROGRESS_LIMIT
        )

    def test_same_bad_arguments_end_at_the_limit(self, tmp_path, monkeypatch, dialect):
        assert ends_at(tmp_path, monkeypatch, dialect, [bad_arguments(dialect)]) == (
            NO_PROGRESS_LIMIT
        )

    def test_refused_call_after_a_failed_run_of_the_same_tool(
        self, tmp_path, monkeypatch, dialect
    ):
        """처음 보고한 결함 — 실행된 두 응답(읽기·실패한 편집) 뒤 거절이 상한만큼."""
        responses = [
            call(dialect, "read_file", {"path": "f.py"}),
            call(dialect, *FAILED_EDIT),
            call(dialect, *REFUSED_EDIT),
        ]
        assert ends_at(tmp_path, monkeypatch, dialect, responses) == (
            2 + NO_PROGRESS_LIMIT
        )

    def test_different_discarded_shapes_in_a_row_end_at_the_limit(
        self, tmp_path, monkeypatch, dialect
    ):
        """모양이 매번 달라 B1 이 못 잡는 연속 — 산문·스키마 거절(필수 빠짐)·인자 오류가 번갈아."""
        shapes = [
            prose(dialect),
            call(dialect, "read_file", {}),
            bad_arguments(dialect),
        ]
        responses = [shapes[i % 3] for i in range(CAP)]
        assert ends_at(tmp_path, monkeypatch, dialect, responses) == (NO_PROGRESS_LIMIT)

    def test_an_executed_call_in_between_starts_the_count_again(
        self, tmp_path, monkeypatch, dialect
    ):
        """버려진 응답 9 → 실행된 호출 1 → 버려진 응답 9 → `complete`: 끝나지 않고 그 결과로 끝난다."""
        nine = [prose(dialect)] * (NO_PROGRESS_LIMIT - 1)
        responses = [
            *nine,
            call(dialect, "read_file", {"path": "f.py"}),
            *nine,
            call(dialect, "complete", {"result": "done"}),
        ]
        result, calls = run(tmp_path, monkeypatch, dialect, responses)
        assert result.success and result.output == "done"
        assert calls == len(responses)

    def test_an_accepted_virtual_op_in_between_starts_the_count_again(
        self, tmp_path, monkeypatch, dialect
    ):
        """가상 op(받아들여진 `message`)도 기록에 남는 응답이다."""
        nine = [prose(dialect)] * (NO_PROGRESS_LIMIT - 1)
        responses = [
            *nine,
            call(dialect, "message", {"to": "agt-x", "text": "hi"}),
            *nine,
            call(dialect, "complete", {"result": "done"}),
        ]
        ports = make_ports(message_handler=lambda to, text, **kw: ("", ""))
        result, calls = run(tmp_path, monkeypatch, dialect, responses, ports=ports)
        assert result.success and result.output == "done"
        assert calls == len(responses)

    def test_a_complete_sent_back_in_between_starts_the_count_again(
        self, tmp_path, monkeypatch, dialect
    ):
        """`complete` 되돌림(열린 항목)은 결과가 기록되는 응답이라 세지 않고 0 으로 돌린다."""
        nine = [prose(dialect)] * (NO_PROGRESS_LIMIT - 1)
        responses = [
            *nine,
            call(dialect, "complete", {"result": "early"}),
            *nine,
            call(dialect, "complete", {"result": "done", "answers": ["u-1"]}),
        ]
        result, calls = run(
            tmp_path, monkeypatch, dialect, responses, query_request_id="u-1"
        )
        assert result.success and result.output == "done"
        assert calls == len(responses)


def _dispatcher() -> TurnDispatcher:
    cfg = make_config(tools_list=["shell", "complete"])
    state = LoopState(query="q")
    return TurnDispatcher(
        cfg,
        state,
        ctx=None,
        tools=ToolBridge(cfg, state, ctx=None, provider=None),
        recorder=None,
    )


@pytest.mark.parametrize(
    ("action", "counts"), [(confirm.APPROVE, 0), (confirm.DENY, NO_PROGRESS_LIMIT - 1)]
)
def test_continue_on_the_stale_question_starts_the_count_again(
    monkeypatch, action, counts
):
    """웹 main 의 StaleGuard 질문에 사람이 Continue 를 고르면 진전 없는 응답의 연속 수도 0(Q2). Stop 은 런을 세운다."""
    d = _dispatcher()
    d._no_progress_in_a_row = NO_PROGRESS_LIMIT - 1
    monkeypatch.setattr(
        dispatch_module.confirm,
        "ask_person",
        lambda *a, **k: ("c-1", confirm.Decision(action, by="user:dj")),
    )
    assert d._ask_stale(["shell ls → 17"]) is (action == confirm.APPROVE)
    assert d._no_progress_in_a_row == counts


def test_a_new_segment_starts_the_count_again():
    """`wait` 에서 깨어 구간이 새로 시작되면 B1·되돌림 예산과 함께 0."""
    d = _dispatcher()
    d._no_progress_in_a_row = NO_PROGRESS_LIMIT - 1
    d._begin_segment()
    assert d._no_progress_in_a_row == 0


# ── 턴을 세는 갈래 여섯 (PROP-no-progress-turns §1.1) ──────────


def batch_of_unknown_tools(dialect: str) -> LLMResponse:
    """묶음의 호출이 전부 실행 전에 거절된다 — 모르는 도구 둘."""
    ops = [("frobnicate", {"x": 1}), ("zork", {"y": 2})]
    if dialect == "native_fc":
        return LLMResponse(
            content="ok",
            tool_calls=[
                {"id": f"c{i}", "name": n, "input": a} for i, (n, a) in enumerate(ops)
            ],
        )
    return LLMResponse(
        content="x\n\n" + json.dumps([{"action": n, **a} for n, a in ops])
    )


def turn_counting_shapes(dialect: str) -> dict[str, LLMResponse]:
    return {
        "every call refused": batch_of_unknown_tools(dialect),
        "wait refused": call(dialect, "wait", {}),
        "same failing run": call(dialect, "read_file", {"path": "nope.py"}),
        # 쓰기의 같은 실패(해시 불일치) — 성공한 쓰기만 진전이다(Q4)
        "same failing edit": call(dialect, *FAILED_EDIT),
        "output cut off": LLMResponse(
            content='x\n\n[{"action": "read_file", "pa', stop_reason="length"
        ),
        "server error": LLMResponse(
            content="", stop_reason="server_error", stop_detail="upstream"
        ),
        "runaway": LLMResponse(
            content="aaaa", stop_reason="runaway", stop_detail="repeat"
        ),
    }


SHAPES = tuple(turn_counting_shapes("json_fc"))


#: 같은 실패의 실행은 처음 한 번은 새 결과다(아직 본 적 없음) — 둘째부터 제자리 턴
FIRST_IS_NEW = {"same failing run", "same failing edit"}


@pytest.mark.parametrize("dialect", DIALECTS)
@pytest.mark.parametrize("shape", SHAPES)
def test_turn_counting_shapes_end_at_the_limit(tmp_path, monkeypatch, dialect, shape):
    """턴을 세는 진전 없음도 연달아 상한이면 끝난다 — 런 끝 줄의 사유가 그 갈래를 말한다."""
    seen = []
    monkeypatch.setattr(
        dispatch_module, "render_run_ended", lambda text, *a, **k: seen.append(text)
    )
    calls = ends_at(
        tmp_path, monkeypatch, dialect, [turn_counting_shapes(dialect)[shape]]
    )
    assert calls == NO_PROGRESS_LIMIT + (1 if shape in FIRST_IS_NEW else 0)
    assert seen and seen[-1].endswith(f"(last: {REASON[shape]}). Stopping.")


REASON = {
    "every call refused": "every call was refused",
    "wait refused": "wait refused",
    "same failing run": "the same results again",
    "same failing edit": "the same results again",
    "output cut off": "output cut off",
    "server error": "server error",
    "runaway": "runaway output",
}


@pytest.mark.parametrize("dialect", DIALECTS)
def test_different_kinds_of_no_progress_in_a_row_end_at_the_limit(
    tmp_path, monkeypatch, dialect
):
    """종류가 매번 달라도 하나의 연속이다 — 산문·`wait` 거절·출력 절단·폭주가 번갈아."""
    shapes = turn_counting_shapes(dialect)
    cycle = [
        prose(dialect),
        shapes["wait refused"],
        shapes["output cut off"],
        shapes["runaway"],
    ]
    responses = [cycle[i % len(cycle)] for i in range(CAP)]
    assert ends_at(tmp_path, monkeypatch, dialect, responses) == NO_PROGRESS_LIMIT


@pytest.mark.parametrize("dialect", DIALECTS)
def test_a_batch_with_one_call_that_ran_is_progress(tmp_path, monkeypatch, dialect):
    """묶음의 호출 하나라도 실행되어 새 결과를 냈으면 진전이다 — 거절이 섞여도."""
    nine = [prose(dialect)] * (NO_PROGRESS_LIMIT - 1)
    if dialect == "native_fc":
        mixed = LLMResponse(
            content="ok",
            tool_calls=[
                {"id": "c0", "name": "read_file", "input": {"path": "f.py"}},
                {"id": "c1", "name": "frobnicate", "input": {"x": 1}},
            ],
        )
    else:
        mixed = LLMResponse(
            content="x\n\n"
            + json.dumps(
                [
                    {"action": "read_file", "path": "f.py"},
                    {"action": "frobnicate", "x": 1},
                ]
            )
        )
    responses = [*nine, mixed, *nine, call(dialect, "complete", {"result": "done"})]
    result, calls = run(tmp_path, monkeypatch, dialect, responses)
    assert result.success and result.output == "done" and calls == len(responses)


@pytest.mark.parametrize("dialect", DIALECTS)
def test_human_words_in_between_start_the_count_again(tmp_path, monkeypatch, dialect):
    """런 도중 흡수된 사람의 새 말은 새 일이다 — 연속 수가 0 으로 돌아간다(Q7)."""
    nine = [prose(dialect)] * (NO_PROGRESS_LIMIT - 1)
    responses = [
        *nine,
        *nine,
        call(dialect, "complete", {"result": "done", "answers": ["u-1", "u-9"]}),
    ]
    words = [{"id": "u-9", "text": "also check README", "nickname": "dj"}]
    served = [0]

    def pop():
        served[0] += 1
        # 아홉 번째 응답 뒤의 턴 경계(10번째 호출 직전)에 한 번
        return words.pop() if served[0] == NO_PROGRESS_LIMIT and words else None

    ports = make_ports(inbox=queue_inbox(pop))
    result, calls = run(
        tmp_path, monkeypatch, dialect, responses, ports=ports, query_request_id="u-1"
    )
    assert result.success and result.output == "done" and calls == len(responses)
