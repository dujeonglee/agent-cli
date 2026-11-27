"""버려진 응답(하니스가 저장하지 않고 되묻기만 남긴 응답)의 연속 상한 (NOTE-unbounded-retries §4).

형식 되묻기·홀로 거절된 호출(A4·A5·인자 JSON 오류)·B1 넛지는 턴을 세지 않는다 — 같은 응답이 되풀이되면 다른
상한이 없었다(v10.177.9 까지 — 가짜 provider 의 응답 수 상한 :data:`CAP` 에서도 끝나지 않았다). v10.177.10 부터
연달아 :data:`DISCARDED_RESPONSE_LIMIT` 번이면 런이 끝난다. 가짜 provider 는 상한(:data:`CAP`)에서 멈춘다 — 그때까지
끝나지 않으면 :class:`ProviderCap` 이 난다."""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from agent_cli.context.manager import ContextManager
from agent_cli.dialects import get
from agent_cli.loop.dispatch import DISCARDED_RESPONSE_LIMIT, TurnDispatcher
from agent_cli.loop.state import LoopState
from agent_cli.loop.tool_bridge import ToolBridge
from agent_cli.providers.base import LLMResponse
from agent_cli.providers.capabilities import ModelCapabilities
from tests.loop_ports import TEST_PORTS, make_ports
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


END = (
    f"No progress: the last {DISCARDED_RESPONSE_LIMIT} responses were rejected "
    "before anything ran."
)


def ends_at(tmp_path, monkeypatch, dialect, responses):
    result, calls = run(tmp_path, monkeypatch, dialect, responses)
    assert result.success is False and result.error == END
    return calls


@pytest.mark.parametrize("dialect", DIALECTS)
class TestDiscardedResponsesEndTheRun:
    def test_same_prose_ends_at_the_limit(self, tmp_path, monkeypatch, dialect):
        assert ends_at(tmp_path, monkeypatch, dialect, [prose(dialect)]) == (
            DISCARDED_RESPONSE_LIMIT
        )

    def test_same_bad_arguments_end_at_the_limit(self, tmp_path, monkeypatch, dialect):
        assert ends_at(tmp_path, monkeypatch, dialect, [bad_arguments(dialect)]) == (
            DISCARDED_RESPONSE_LIMIT
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
            2 + DISCARDED_RESPONSE_LIMIT
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
        assert ends_at(tmp_path, monkeypatch, dialect, responses) == (
            DISCARDED_RESPONSE_LIMIT
        )

    def test_an_executed_call_in_between_starts_the_count_again(
        self, tmp_path, monkeypatch, dialect
    ):
        """버려진 응답 9 → 실행된 호출 1 → 버려진 응답 9 → `complete`: 끝나지 않고 그 결과로 끝난다."""
        nine = [prose(dialect)] * (DISCARDED_RESPONSE_LIMIT - 1)
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
        nine = [prose(dialect)] * (DISCARDED_RESPONSE_LIMIT - 1)
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
        nine = [prose(dialect)] * (DISCARDED_RESPONSE_LIMIT - 1)
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


def test_a_new_segment_starts_the_count_again():
    """`wait` 에서 깨어 구간이 새로 시작되면 B1·되돌림 예산과 함께 0."""
    cfg = make_config(tools_list=["shell", "complete"])
    state = LoopState(query="q")
    d = TurnDispatcher(
        cfg,
        state,
        ctx=None,
        tools=ToolBridge(cfg, state, ctx=None, provider=None),
        recorder=None,
    )
    d._discarded_in_a_row = DISCARDED_RESPONSE_LIMIT - 1
    d._begin_segment()
    assert d._discarded_in_a_row == 0


# ── 턴을 세는 갈래 여섯 — 지금은 끝나지 않는다 (PROP-no-progress-turns §1.1, 고정) ──────────


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


@pytest.mark.parametrize("dialect", DIALECTS)
@pytest.mark.parametrize("shape", SHAPES)
def test_turn_counting_shapes_do_not_end_the_run(tmp_path, monkeypatch, dialect, shape):
    """턴을 세는 응답이라 버려진 응답 카운터가 0 으로 돌아가거나(묶음·`wait`·실행) 건드리지 않는다(코어의 끊김) —
    ``max_turns`` 0 에서는 가짜 provider 의 상한까지 끝나지 않는다."""
    with pytest.raises(ProviderCap):
        run(tmp_path, monkeypatch, dialect, [turn_counting_shapes(dialect)[shape]])
