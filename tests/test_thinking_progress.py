"""P5 (v8.56.0): 사고(reasoning_content) 스트림 가시화.

러너웨이 진단 근거: 35B 벤치에서 631s/46K 토큰 단일 생성이 화면·로그
완전 무음이라 서버 hang 으로 오진됐다 — 사고 델타는 on_chunk(본문)를
타지 않기 때문. 이 채널이 스피너 마르퀴(🧠 카운터)·웹 상단바로 흐른다.

v10.70.0: 셋째 채널 — 서버가 구조로 보내는 도구 호출 조각(native_fc 의
``delta.tool_calls``)도 ``on_tool_call`` 로 흘러 🔧 카운터가 된다. 앞 숫자는 세 채널의 합.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from agent_cli.providers import http as http_mod
from agent_cli.providers.openai import _map_openai_payload
from tests.loop_ports import TEST_PORTS


class TestStreamDelivery:
    def _resp(self, lines):
        class R:
            def iter_lines(self):
                yield from lines

            def close(self):
                pass

        return R()

    def test_on_thinking_receives_reasoning_deltas(self):
        lines = [
            b'data: {"choices":[{"delta":{"reasoning_content":"think1"}}]}',
            b'data: {"choices":[{"delta":{"content":"answer"}}]}',
            b'data: {"choices":[{"delta":{"reasoning_content":"think2"}}]}',
            b"data: [DONE]",
        ]
        got_think, got_text = [], []
        acc = http_mod.run_sse_stream(
            self._resp(lines),
            got_text.append,
            map_payload=_map_openai_payload,
            on_thinking=got_think.append,
        )
        assert got_think == ["think1", "think2"]
        assert got_text == ["answer"]  # 사고는 본문 채널을 오염시키지 않는다
        assert acc.content == "answer" and "think1" in acc.thinking

    def test_no_callback_is_fine(self):
        lines = [b'data: {"choices":[{"delta":{"reasoning_content":"t"}}]}']
        acc = http_mod.run_sse_stream(
            self._resp(lines), lambda t: None, map_payload=_map_openai_payload
        )
        assert "t" in acc.thinking


class TestLoopWiring:
    def test_provider_receives_on_thinking_callable(self, tmp_path):
        import json

        from agent_cli.context.manager import ContextManager
        from agent_cli.loop import run_loop
        from agent_cli.providers.base import LLMResponse
        from agent_cli.providers.capabilities import ModelCapabilities

        provider = MagicMock()
        provider.call.return_value = LLMResponse(
            content=json.dumps({"action": "complete", "result": "ok"})
        )
        run_loop(
            ports=TEST_PORTS,
            query="q",
            provider=provider,
            capabilities=ModelCapabilities(
                context_window=32768, max_output_tokens=1024, supports_thinking=True
            ),
            model="m",
            ctx=ContextManager(session_dir=tmp_path),
            max_turns=2,
        )
        assert callable(provider.call.call_args.kwargs.get("on_thinking"))


class TestWebRenderer:
    def test_thinking_tick_throttled_and_reset(self):
        from agent_cli.render.web import WebRenderer

        r = WebRenderer()
        events = []
        r._emit = lambda ev, d, persistent=False: events.append((ev, d))
        r.thinking_chunk("x" * 400)  # 첫 델타 → 즉시 1회
        r.thinking_chunk("x" * 400)  # 0.5s 내 → 스로틀
        assert len(events) == 1
        assert events[0][0] == "thinking_tick" and events[0][1]["tokens"] == 100
        # v9.9.0: 누적은 **스코프별**이다 — main 은 빈 키(`""`).
        r._tick_state[""]["last_thinking"] = 0.0
        r.thinking_chunk("x" * 400)
        assert len(events) == 2 and events[1][1]["tokens"] == 300
        r.stream_end()  # 리셋 (+stream_end 이벤트)
        assert "" not in r._tick_state

    def test_scopes_do_not_share_the_counter(self):
        """동시에 도는 에이전트들의 토큰이 **한 숫자에 더해지면 안 된다**.

        렌더러는 싱글턴이고 상주 에이전트 워커는 각자 자기 스레드에서 돈다.
        종전엔 누적이 인스턴스 속성이라 (1) 숫자가 서로 더해지고 (2) 한 쪽의
        `stream_end` 가 전원의 카운터를 0 으로 만들었다 — 에이전트 4대를 띄운
        실세션에서 드러났다."""
        import threading

        from agent_cli.render.web import WebConnection, WebRenderer

        # `_emit` 은 스텁하지 않는다 — 실제 팬아웃이 `task_id` 를 붙이는
        # 경로까지 함께 봐야 "스코프별"이 화면까지 도달하는지 확인된다.
        r = WebRenderer()
        conn = WebConnection(id="c1")
        r.register_connection(conn)

        def drain():
            out = []
            while not conn.queue.empty():
                ev, d = conn.queue.get_nowait()
                if ev.endswith("_tick"):
                    out.append((ev, d))
            return out

        def as_scope(task_id, fn):
            r._thread_to_task[threading.get_ident()] = task_id
            try:
                fn()
            finally:
                r._thread_to_task.pop(threading.get_ident(), None)

        for tid, n in (("agt-a#1", 400), ("agt-b#1", 800)):
            t = threading.Thread(
                target=as_scope, args=(tid, lambda n=n: r.thinking_chunk("x" * n))
            )
            t.start()
            t.join()

        by_scope = {d["task_id"]: d["tokens"] for _, d in drain()}
        assert by_scope == {"agt-a#1": 100, "agt-b#1": 200}, (
            f"스코프별로 갈리지 않았다: {by_scope}"
        )

        # 한 쪽의 종료가 다른 쪽 누적을 지우지 않는다.
        threading.Thread(target=as_scope, args=("agt-a#1", r.stream_end)).start()
        import time as _t

        _t.sleep(0.05)
        assert "agt-a#1" not in r._tick_state
        assert r._tick_state["agt-b#1"]["thinking_chars"] == 800


class TestToolCallChannel:
    """v10.70.0: native_fc 에서 긴 호출을 쓰는 동안 content 가 0 이라 마르퀴·생성 줄이
    무진전처럼 보였다(gcode 2차 런 `~0 tokens`). 호출 조각이 셋째 채널로 흐르고, 앞
    숫자는 세 채널의 합, 내역은 0 이 아닌 🧠·🔧 만."""

    def _resp(self, lines):
        class R:
            def iter_lines(self):
                yield from lines

            def close(self):
                pass

        return R()

    def test_on_tool_call_receives_name_and_argument_pieces(self):
        lines = [
            b'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"c1","function":{"name":"python","arguments":""}}]}}]}',
            b'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"function":{"arguments":"{\\"code\\": \\"pri"}}]}}]}',
            b'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"function":{"arguments":"nt(1)\\"}"}}]}}]}',
            b"data: [DONE]",
        ]
        got_tool, got_text = [], []
        acc = http_mod.run_sse_stream(
            self._resp(lines),
            got_text.append,
            map_payload=_map_openai_payload,
            on_tool_call=got_tool.append,
        )
        assert got_text == []  # content 는 비었다 — 종전엔 화면이 완전 무음
        assert "".join(got_tool) == 'python{"code": "print(1)"}'
        assert acc.tool_calls[0]["arguments"] == '{"code": "print(1)"}'  # 누산 불변

    def test_provider_receives_on_tool_call_callable(self, tmp_path):
        import json

        from agent_cli.context.manager import ContextManager
        from agent_cli.loop import run_loop
        from agent_cli.providers.base import LLMResponse
        from agent_cli.providers.capabilities import ModelCapabilities

        provider = MagicMock()
        provider.call.return_value = LLMResponse(
            content=json.dumps({"action": "complete", "result": "ok"})
        )
        run_loop(
            ports=TEST_PORTS,
            query="q",
            provider=provider,
            capabilities=ModelCapabilities(
                context_window=32768, max_output_tokens=1024, supports_thinking=False
            ),
            model="m",
            ctx=ContextManager(session_dir=tmp_path),
            max_turns=2,
        )
        assert callable(provider.call.call_args.kwargs.get("on_tool_call"))

    def test_marquee_sums_three_channels_and_lists_nonzero_parts(self):
        from io import StringIO

        from rich.console import Console

        from agent_cli.render.minimal import MinimalRenderer

        buf = StringIO()
        r = MinimalRenderer(Console(file=buf, force_terminal=True, width=120))
        r.tool_call_chunk("x" * 400)  # 100 토큰 — content 0 이어도 숫자가 움직인다
        out = buf.getvalue()
        assert "~100 tokens (🔧 100)" in out and "🧠" not in out
        r.thinking_chunk("y" * 80)  # +20
        r.stream_chunk("z" * 40)  # +10
        assert "~130 tokens (🧠 20 · 🔧 100)" in buf.getvalue()
        r.stream_end()
        assert r._tool_buf == ""
        buf.truncate(0)
        buf.seek(0)
        r.stream_chunk("w" * 40)
        out = buf.getvalue()  # 본문만이면 내역 괄호가 없다 (얼굴의 괄호는 제외)
        assert "~10 tokens" in out and "tokens (" not in out

    def test_web_tool_tick(self):
        from agent_cli.render.web import WebRenderer

        r = WebRenderer()
        events = []
        r._emit = lambda ev, d, persistent=False: events.append((ev, d))
        r.tool_call_chunk("x" * 400)
        assert events == [("tool_tick", {"tokens": 100})]
        r.stream_end()
        assert "" not in r._tick_state
