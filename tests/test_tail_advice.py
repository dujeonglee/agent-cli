"""꼬리 = 중복 없는 상시 규칙 + 상태별 권고 (v10.73.0, docs/tail/DESIGN.md).

세 가지를 고정한다.

1. Task Guidelines 에서 다른 곳과 겹치던 줄이 빠졌다 — python 도구 설명, 작업
   블록·Open Items 의 wait 안내, "실패하면 원인부터"(→ 상태별 권고). 머리 Context
   Discipline 의 "추론 간결" 불릿도 꼬리 한 줄로 합쳐졌다.
2. 지난 턴에 실패한 호출이 있으면 **그 다음 턴의 꼬리에만** 권고 한 줄이 뜬다 —
   상태 블록 머리 바로 아래, 열린 항목보다 앞.
3. 디스패치가 턴마다 플래그를 새로 세고(실행 실패·거부 둘 다 올린다), 성공한 턴
   뒤에는 사라진다.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from agent_cli.loop import run_loop
from agent_cli.prompts.session_state import (
    FAILED_LAST_TURN_NOTICE,
    RULES_HEADER,
    SESSION_STATE_HEADER,
    build_session_state,
)
from agent_cli.prompts.system_prompt import (
    CONTEXT_DISCIPLINE,
    TASK_GUIDELINES,
    build_system_prompt,
)
from agent_cli.providers.base import LLMResponse
from agent_cli.providers.capabilities import ModelCapabilities
from tests.loop_ports import TEST_PORTS


def _complete(result: str) -> str:
    return json.dumps([{"action": "complete", "result": result}])


class TestGuidelinesCarryOnlyWhatIsNowhereElse:
    def test_python_line_is_gone_the_tool_description_has_it(self):
        from agent_cli.tools.python_tool import PythonTool

        assert "`python`" not in TASK_GUIDELINES
        assert "KEEPS STATE" in PythonTool.description  # 한 곳에만

    def test_wait_and_polling_guidance_left_the_standing_rules(self):
        """돌고 있을 때만 뜨는 `## Background jobs` 블록("do not poll" · `kill`)과
        `## Open Items` 의 wait 줄이 그 자리를 맡는다."""
        from agent_cli.loop.ledger import _TAIL_WAIT

        assert "call `wait`" not in TASK_GUIDELINES
        assert "poll" not in TASK_GUIDELINES and "kill" not in TASK_GUIDELINES
        assert "`wait`" in _TAIL_WAIT
        # 백그라운드로 **띄우는** 법은 조건을 만들 수 없는 원칙이라 남는다
        assert "`background: true`" in TASK_GUIDELINES
        assert "You are told when it ends" in TASK_GUIDELINES

    def test_diagnose_first_is_a_state_line_not_a_standing_rule(self):
        assert "diagnose the cause" not in TASK_GUIDELINES
        assert "fix the cause, then retry" in FAILED_LAST_TURN_NOTICE
        assert "do not switch tactics" in FAILED_LAST_TURN_NOTICE

    def test_terse_reasoning_lives_once_in_the_tail(self):
        """머리 Context Discipline 의 불릿은 지웠고(v8.59.0 실측 문장을 남기는 쪽),
        "관찰을 되풀이하지 말라" 는 그 꼬리 줄에 합쳐졌다."""
        assert "Keep your reasoning brief" not in CONTEXT_DISCIPLINE
        assert "Keep your reasoning brief" not in build_system_prompt(
            ModelCapabilities(32768, 4096, False), ["shell"]
        )
        assert "never restate what an observation already shows" in TASK_GUIDELINES
        assert "one point per sentence" in TASK_GUIDELINES

    def test_the_block_got_shorter(self):
        """매 턴 다시 prefill 되는 구간 — 종전 3,197자(16줄). 되살아나면 실패."""
        assert TASK_GUIDELINES.count("\n- ") == 13
        assert len(TASK_GUIDELINES) < 2500


class TestFailedLastTurnNotice:
    def test_absent_by_default(self):
        out = build_session_state(turn=3, max_turns=10)
        assert FAILED_LAST_TURN_NOTICE not in out

    def test_rides_right_under_the_turn_line_before_open_items(self):
        out = build_session_state(
            turn=3,
            max_turns=10,
            items="## Open Items\n- u-1 x",
            failed_last_turn=True,
            guidelines="## Task Guidelines\n- r",
        )
        lines = out.split("\n")
        i = lines.index(SESSION_STATE_HEADER)
        assert lines[i + 1].startswith("turn 3/10")
        assert lines[i + 2] == FAILED_LAST_TURN_NOTICE
        assert out.index(FAILED_LAST_TURN_NOTICE) < out.index("## Open Items")
        assert out.index(RULES_HEADER) < out.index(FAILED_LAST_TURN_NOTICE)

    def test_renders_even_when_it_is_the_only_state(self):
        out = build_session_state(failed_last_turn=True)
        assert SESSION_STATE_HEADER in out and FAILED_LAST_TURN_NOTICE in out
        assert build_session_state() == ""


class TestDispatchSetsTheFlagPerTurn:
    @pytest.fixture
    def caps(self):
        return ModelCapabilities(
            context_window=32768, max_output_tokens=4096, supports_thinking=False
        )

    @staticmethod
    def _recording_provider(*contents: str):
        """호출 **시점**의 마지막 user 메시지를 복사해 둔다 — MagicMock 의
        call_args_list 는 같은 리스트 객체를 가리켜 나중에 보면 뒤 턴의 내용이다."""
        provider = MagicMock()
        tails: list[str] = []
        replies = [LLMResponse(content=c) for c in contents]

        def call(messages=None, **kwargs):
            msgs = messages if messages is not None else kwargs["messages"]
            users = [m for m in msgs if m.get("role") == "user"]
            tails.append(str(users[-1].get("content", "")) if users else "")
            return replies.pop(0)

        provider.call.side_effect = call
        return provider, tails

    def test_tool_failure_then_success(self, caps, monkeypatch, tmp_path):
        """턴 1 의 shell 이 터진다 → 턴 2 꼬리에 권고 → 턴 2 가 성공 → 턴 3 꼬리엔
        없다 → complete. 꼬리는 ctx 가 마지막 user 메시지 본문에 붙인다."""
        from agent_cli.context.manager import ContextManager
        from agent_cli.tools.base import ToolResult
        from agent_cli.tools.registry import TOOLS

        def flaky(args, **_k):
            if args.get("command") == "a":
                raise RuntimeError("boom 4711")
            return ToolResult(success=True, output="ok")

        monkeypatch.setattr(TOOLS["shell"], "_run", flaky)
        provider, tails = self._recording_provider(
            json.dumps([{"action": "shell", "command": "a"}]),
            json.dumps([{"action": "shell", "command": "b"}]),
            _complete("done"),
        )
        run_loop(
            ports=TEST_PORTS,
            query="q",
            provider=provider,
            capabilities=caps,
            model="m",
            ctx=ContextManager(session_dir=tmp_path),
            max_turns=6,
        )
        assert len(tails) == 3
        assert FAILED_LAST_TURN_NOTICE not in tails[0]
        assert FAILED_LAST_TURN_NOTICE in tails[1]
        assert FAILED_LAST_TURN_NOTICE not in tails[2]

    def test_rejected_call_counts_as_a_failure(self, caps, tmp_path):
        """모르는 도구 — 실행되지 않은 거부도 "실패한 호출" 이다: 고칠 원인이 있다."""
        from agent_cli.context.manager import ContextManager

        provider, tails = self._recording_provider(
            json.dumps([{"action": "no_such_tool", "x": 1}]), _complete("done")
        )
        run_loop(
            ports=TEST_PORTS,
            query="q",
            provider=provider,
            capabilities=caps,
            model="m",
            ctx=ContextManager(session_dir=tmp_path),
            max_turns=6,
        )
        assert FAILED_LAST_TURN_NOTICE not in tails[0]
        assert FAILED_LAST_TURN_NOTICE in tails[-1]
