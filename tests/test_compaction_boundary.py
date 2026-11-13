"""경계 모양 압축 요약 (v10.123.0, docs/context-compaction/DESIGN.md §8.11).

본 호출과 같은 요청(system·tools·messages)을 마지막 축출 메시지까지 잘라 보내고 뒤에
경계 줄 + 지침을 붙인다 — 서버의 접두(KV) 캐시가 요약 호출에도 듣게. 실제 공급자를
가짜 HTTP 위에 올려, 압축 직전 본 호출 본문과 경계 요약 본문을 나란히 본다.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from agent_cli import dialects
from agent_cli.context.manager import ContextManager
from agent_cli.prompts.compaction import (
    BOUNDARY_LINE,
    SUMMARY_HEADINGS,
    boundary_instruction,
    looks_like_summary,
    summary_heading_count,
)
from tests.loop_ports import TEST_PORTS

SUMMARY = "TASK — fix a.py\nSTATE — f0..f2 read\nFACTS — path f1.py"
_POST = "agent_cli.providers.openai.requests.post"


def _sse(lines):
    r = MagicMock()
    r.iter_lines.return_value = iter(lines)
    r.raise_for_status.return_value = None
    r.status_code = 200
    return r


def _text_stream(text: str):
    chunk = json.dumps({"choices": [{"delta": {"content": text}}]})
    done = json.dumps({"choices": [{"delta": {}, "finish_reason": "stop"}]})
    return _sse([f"data: {chunk}".encode(), f"data: {done}".encode(), b"data: [DONE]"])


def _tool_call_stream():
    call = {
        "index": 0,
        "id": "c9",
        "function": {"name": "read_file", "arguments": '{"path": "f9.py"}'},
    }
    chunk = json.dumps(
        {"choices": [{"delta": {"tool_calls": [call]}, "finish_reason": "tool_calls"}]}
    )
    return _sse([f"data: {chunk}".encode(), b"data: [DONE]"])


_ERR_STREAM = [
    b'data: {"choices":[{"delta":{"content":"TASK - half"}}]}',
    b'data: {"error":{"code":"timeout","message":"upstream timed out"}}',
]


def _loop(tmp_path: Path, dialect_name: str, *, context_window: int = 200_000):
    from agent_cli.providers.capabilities import ModelCapabilities
    from agent_cli.providers.openai import OpenAIProvider
    from tests.loopkit import make_loop

    dialect = dialects.get(dialect_name)
    ctx = ContextManager(
        tmp_path / "session", max_context_tokens=1_000_000, dialect=dialect
    )
    caps = ModelCapabilities(
        context_window=context_window,
        max_output_tokens=65_536,
        supports_thinking=True,
    )
    loop = make_loop(
        ports=TEST_PORTS,
        query="x",
        provider=OpenAIProvider("http://gw/v1", "k"),
        capabilities=caps,
        model="m",
        ctx=ctx,
        dialect=dialect,
    )
    loop._prompt.rebuild()
    return loop, ctx


def _fill(ctx, n: int = 6, *, pin_id: str | None = None):
    """요청 하나 + (호출, 관찰) n 쌍. ``pin_id`` 면 둘째 쌍 뒤에 미답 요청 하나."""
    ctx.add({"role": "user", "content": "please fix a.py"})
    for i in range(n):
        ctx.add(
            {
                "role": "assistant",
                "thought": f"reading f{i}",
                "ops": [{"action": "read_file", "action_input": {"path": f"f{i}.py"}}],
            }
        )
        ctx.add(
            {
                "role": "user",
                "tool": "read_file",
                "success": True,
                "content": f"Observation: body of f{i} " * 20,
            }
        )
        if pin_id and i == 1:
            ctx.add(
                {"role": "user", "content": "also check b.py", "request_id": pin_id}
            )


def _main_body(loop):
    """본 호출 한 번 — 압축 직전 요청 본문을 캡처한다(목표가 커서 압축은 안 돈다)."""
    with patch(_POST) as post:
        post.return_value = _text_stream("ok")
        loop._llm._call_llm()
    return post.call_args.kwargs["json"]


def _compact(ctx, *responses):
    """``compact_now`` 를 가짜 응답 순서대로 — (결과, 보낸 본문들)."""
    with patch(_POST) as post:
        post.side_effect = list(responses)
        result = ctx.compact_now()
    return result, [c.kwargs["json"] for c in post.call_args_list]


def _assert_true_prefix(main: dict, boundary: dict, *, prior: bool = False):
    """경계 본문 = 본 호출 본문의 앞 n 개 + 규칙대로 붙은 지침."""
    bm, mm = boundary["messages"], main["messages"]
    instr = boundary_instruction(prior_summary=prior, images=False)
    last = bm[-1]
    if last["content"] == instr:  # tool·assistant 뒤 새 user
        n = len(bm) - 1
        assert last["role"] == "user"
        assert bm[n - 1]["role"] in ("tool", "assistant")
        assert bm[:n] == mm[:n]
    else:  # user 끝에 덧붙임
        n = len(bm) - 1
        assert bm[:n] == mm[:n]
        assert mm[n]["role"] == "user"
        assert last == {**mm[n], "content": mm[n]["content"] + "\n\n" + instr}
    assert n < len(mm)  # 꼬리 자리까지 가지 않는다(남는 쪽이 있다)
    return n


class TestBoundaryRequestIsTheMainPrefix:
    @pytest.mark.parametrize("dialect_name", ["json_fc", "xml_fc", "native_fc"])
    def test_same_system_tools_and_leading_messages(self, tmp_path, dialect_name):
        loop, ctx = _loop(tmp_path, dialect_name)
        _fill(ctx)
        main = _main_body(loop)
        result, bodies = _compact(ctx, _text_stream(SUMMARY))
        assert result.compacted and len(bodies) == 1
        boundary = bodies[0]
        # 요청 머리: 같은 system(OpenAI 는 messages[0])·같은 tools
        assert boundary["messages"][0] == main["messages"][0]
        assert main["messages"][0]["content"] == loop._llm.prompt.system
        assert boundary.get("tools") == main.get("tools")
        if dialect_name == "native_fc":
            assert boundary["tools"]
        _assert_true_prefix(main, boundary)
        # 요약 호출만의 것: 사고 끔·출력 상한·스트림, 문법·json_mode 없음
        assert boundary["chat_template_kwargs"] == {"enable_thinking": False}
        assert boundary["max_tokens"] == 16_384
        assert boundary["stream"] is True
        assert "guided_grammar" not in boundary
        assert "response_format" not in boundary
        # 꼬리(세션 상태·complete 넛지)는 싣지 않는다
        state = ctx._session_state
        assert state and state not in json.dumps(boundary, ensure_ascii=False)
        assert ctx.summary == SUMMARY
        assert ctx._last_summary_shape == "boundary"

    def test_prefix_dicts_equal_main_messages(self, tmp_path):
        """공용 렌더 경로: 접두의 메시지는 ``get_messages`` 앞 N 개와 같은 dict."""
        _, ctx = _loop(tmp_path, "native_fc")
        _fill(ctx)
        _, evict, _ = ctx._split_for_compaction()
        prefix, _ = ctx._boundary_prefix(evict)
        assert prefix == ctx.get_messages()[: len(prefix)]


class TestBoundaryCut:
    def _ctx(self, tmp_path, dialect_name):
        _, ctx = _loop(tmp_path, dialect_name)
        _fill(ctx)
        return ctx

    def test_native_fc_cut_at_call_takes_its_results(self, tmp_path):
        """자른 끝이 tool_calls assistant 면 뒤따르는 tool 결과(남는 레코드의 것)까지."""
        ctx = self._ctx(tmp_path, "native_fc")
        evict = ctx.get_raw_messages()[:4]  # user, A0, O0, A1
        assert evict[-1]["role"] == "assistant"
        prefix, through = ctx._boundary_prefix(evict)
        assert through == 4  # O1 레코드의 결과가 들어왔다
        assert prefix[-2]["role"] == "assistant" and prefix[-2]["tool_calls"]
        assert prefix[-1]["role"] == "tool"
        assert prefix[-1]["tool_call_id"] == prefix[-2]["tool_calls"][0]["id"]
        assert prefix == ctx.get_messages()[: len(prefix)]
        messages, _ = ctx._boundary_messages(evict)
        assert messages[:-1] == prefix
        assert messages[-1]["role"] == "user"
        assert messages[-1]["content"].startswith(BOUNDARY_LINE)

    def test_text_dialect_cut_at_observation_appends_to_user(self, tmp_path):
        ctx = self._ctx(tmp_path, "json_fc")
        evict = ctx.get_raw_messages()[:3]  # user, A0, O0
        prefix, _ = ctx._boundary_prefix(evict)
        messages, _ = ctx._boundary_messages(evict)
        assert prefix[-1]["role"] == "user"
        assert len(messages) == len(prefix)
        assert messages[-1]["content"] == (
            prefix[-1]["content"]
            + "\n\n"
            + boundary_instruction(prior_summary=False, images=False)
        )
        # 복사 후 수정 — 렌더 미러·캐시 원본은 그대로
        assert BOUNDARY_LINE not in json.dumps(ctx.get_messages())

    def test_text_dialect_cut_at_assistant_adds_user(self, tmp_path):
        ctx = self._ctx(tmp_path, "json_fc")
        evict = ctx.get_raw_messages()[:2]  # user, A0
        messages, _ = ctx._boundary_messages(evict)
        assert messages[-2]["role"] == "assistant"
        assert messages[-1] == {
            "role": "user",
            "content": boundary_instruction(prior_summary=False, images=False),
        }

    def test_pinned_request_stays_in_place(self, tmp_path):
        _, ctx = _loop(tmp_path, "native_fc")
        ctx.set_open_requests(lambda: ["r-9"])
        _fill(ctx, n=8, pin_id="r-9")
        _, evict, retained = ctx._split_for_compaction()
        pinned = next(m for m in retained if m.get("request_id") == "r-9")
        assert pinned not in evict
        prefix, _ = ctx._boundary_prefix(evict)
        assert any(m.get("content") == "also check b.py" for m in prefix)
        assert prefix == ctx.get_messages()[: len(prefix)]

    def test_prior_summary_adds_fold_in_line(self, tmp_path):
        loop, ctx = _loop(tmp_path, "json_fc")
        _fill(ctx, n=10)
        first, bodies = _compact(ctx, _text_stream(SUMMARY))
        assert first.compacted
        assert "Fold it in" not in json.dumps(bodies[0])
        for i in range(10, 16):
            ctx.add({"role": "user", "content": f"more work {i} " * 30})
        main = _main_body(loop)
        second, bodies = _compact(ctx, _text_stream(SUMMARY + "\nDONE — more"))
        assert second.compacted
        boundary = bodies[0]
        assert "Fold it in" in boundary["messages"][-1]["content"]
        assert boundary["messages"][1]["content"].startswith(
            "## Summary of earlier conversation"
        )
        _assert_true_prefix(main, boundary, prior=True)


class TestBoundaryFallback:
    def _run(self, tmp_path, *responses, dialect_name="json_fc", **kw):
        loop, ctx = _loop(tmp_path, dialect_name, **kw)
        _fill(ctx)
        _main_body(loop)
        result, bodies = _compact(ctx, *responses)
        return ctx, result, bodies

    @staticmethod
    def _is_transcript(body) -> bool:
        return body["messages"][0]["content"].startswith(
            "You are compacting an agent's working transcript"
        ) and body["messages"][-1]["content"].startswith("Transcript to summarise:")

    def test_abnormal_stream_falls_back_to_transcript(self, tmp_path):
        ctx, result, bodies = self._run(
            tmp_path, _sse(list(_ERR_STREAM)), _text_stream(SUMMARY)
        )
        assert len(bodies) == 2 and self._is_transcript(bodies[1])
        assert not self._is_transcript(bodies[0])
        assert result.compacted and ctx.summary == SUMMARY
        assert ctx._last_summary_shape == "transcript"
        assert ctx._last_shape_fallback.startswith("error:")
        assert "upstream timed out" in ctx._last_shape_fallback

    def test_tool_call_only_is_empty(self, tmp_path):
        ctx, result, bodies = self._run(
            tmp_path,
            _tool_call_stream(),
            _text_stream(SUMMARY),
            dialect_name="native_fc",
        )
        assert len(bodies) == 2 and self._is_transcript(bodies[1])
        assert result.compacted and ctx._last_shape_fallback == "empty"
        # 전사 모양은 종전대로 tools 를 싣지 않는다
        assert "tools" not in bodies[1] and "tools" in bodies[0]

    def test_continuing_the_task_is_not_a_summary(self, tmp_path):
        ctx, _, bodies = self._run(
            tmp_path, _text_stream("Next I will read f6.py."), _text_stream(SUMMARY)
        )
        assert len(bodies) == 2 and self._is_transcript(bodies[1])
        assert ctx.summary == SUMMARY and ctx._last_shape_fallback == "not_summary"

    def test_too_large_goes_straight_to_transcript(self, tmp_path):
        # 창이 작아 경계 요청(접두 + 지침 + 출력 상한)이 들어가지 않는다
        ctx, _, bodies = self._run(
            tmp_path, _text_stream(SUMMARY), context_window=20_000
        )
        assert len(bodies) == 1 and self._is_transcript(bodies[0])
        assert bodies[0]["max_tokens"] == 16_384
        assert ctx._last_summary_shape == "transcript"
        assert ctx._last_shape_fallback.startswith("too_large:")

    def test_both_fail_is_compaction_error_and_cache_untouched(self, tmp_path):
        loop, ctx = _loop(tmp_path, "json_fc")
        _fill(ctx)
        _main_body(loop)
        before = ctx.get_messages()
        raw = ctx.get_raw_messages()
        result, bodies = _compact(ctx, _sse(list(_ERR_STREAM)), _sse(list(_ERR_STREAM)))
        assert len(bodies) == 2
        assert not result.compacted and "upstream timed out" in result.error
        assert "boundary shape: error:" in result.error
        assert ctx.summary == "" and ctx.compaction_count == 0
        assert ctx.get_messages() == before and ctx.get_raw_messages() == raw

    def test_trailing_call_block_is_stripped_in_text_dialect(self, tmp_path):
        d = dialects.get("json_fc")
        text = SUMMARY + "\n\n" + d.prompt_call_example("read_file", {"path": "f6.py"})
        ctx, _, bodies = self._run(tmp_path, _text_stream(text))
        assert len(bodies) == 1 and ctx._last_summary_shape == "boundary"
        assert '"action"' not in ctx.summary
        assert ctx.summary.startswith("TASK — fix a.py")

    def test_recorder_gets_shape_and_reason(self, tmp_path):
        loop, ctx = _loop(tmp_path, "json_fc")
        seen = []

        class Rec:
            def record_compaction(self, **kw):
                seen.append(kw)

        ctx.set_recorder(Rec())
        _fill(ctx)
        _main_body(loop)
        _compact(ctx, _text_stream("no headings here"), _text_stream(SUMMARY))
        assert seen[-1]["summary_shape"] == "transcript"
        assert seen[-1]["shape_fallback"] == "not_summary"
        assert seen[-1]["failure_signal"] is None

    def test_plain_callback_uses_transcript_only(self, tmp_path):
        """``boundary_room`` 없이 등록한 요약기(테스트의 가짜)는 종전 그대로."""
        ctx = ContextManager(tmp_path / "s", max_context_tokens=1_000_000)
        calls = []
        ctx.set_compactor(lambda msgs: calls.append(msgs) or "S")
        _fill(ctx)
        assert ctx.compact_now().compacted
        assert len(calls) == 1
        assert calls[0][0]["content"].startswith("Transcript to summarise:")
        assert ctx._last_summary_shape == "transcript"
        assert ctx._last_shape_fallback == ""


class TestSummaryCheck:
    def test_two_headings_at_line_start(self):
        assert looks_like_summary("TASK — a\nSTATE — b")
        assert looks_like_summary("## TASK\nx\n**DONE** — y")
        assert not looks_like_summary("TASK — a only")
        assert not looks_like_summary("I will check the TASK and STATE later.")
        assert summary_heading_count("TASK\nTASK\nFACTS") == 2

    def test_headings_are_shared_with_transcript_prompt(self, tmp_path):
        """제목 일곱 줄은 한 벌 — 전사 모양 프롬프트와 경계 지침이 같은 상수."""
        assert SUMMARY_HEADINGS in boundary_instruction(
            prior_summary=False, images=False
        )
        loop, _ = _loop(tmp_path, "json_fc")
        with patch(_POST) as post:
            post.return_value = _text_stream(SUMMARY)
            loop._llm._llm_compact_summarize([{"role": "user", "content": "t"}])
        assert (
            SUMMARY_HEADINGS in post.call_args.kwargs["json"]["messages"][0]["content"]
        )
