"""P5 (v8.56.0): 사고(reasoning_content) 스트림 가시화.

러너웨이 진단 근거: 35B 벤치에서 631s/46K 토큰 단일 생성이 화면·로그
완전 무음이라 서버 hang 으로 오진됐다 — 사고 델타는 on_chunk(본문)를
타지 않기 때문. 이 채널이 스피너 마르퀴(思 카운터)·웹 상단바로 흐른다.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from agent_cli.providers import http as http_mod
from agent_cli.providers.openai import _map_openai_payload


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
        r._last_think_emit = 0.0
        r.thinking_chunk("x" * 400)
        assert len(events) == 2 and events[1][1]["tokens"] == 300
        r.stream_end()  # 리셋 (+stream_end 이벤트)
        assert r._think_chars == 0
