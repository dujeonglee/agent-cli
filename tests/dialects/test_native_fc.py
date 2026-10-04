"""native_fc — 서버 네이티브 함수 호출 방언 (docs/dialects/NATIVE.md, v10.2.0).

파싱 주체가 서버다: 요청에 `tools`, 응답은 `tool_calls`. 기록은 같은 `{thought, ops}`,
내보낼 때만 assistant 는 `tool_calls`, 관찰은 op 마다 `tool` 메시지(id 는 렌더 시 합성).
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from agent_cli.context.manager import ContextManager
from agent_cli.dialects import get
from agent_cli.loop import run_loop
from agent_cli.providers.base import LLMResponse, TokenUsage
from agent_cli.providers.capabilities import ModelCapabilities
from tests.loop_ports import TEST_PORTS


@pytest.fixture
def wf():
    return get("native_fc")


@pytest.fixture
def caps():
    return ModelCapabilities(
        context_window=32768, max_output_tokens=4096, supports_thinking=False
    )


class TestSurface:
    def test_registered_and_server_parsed(self, wf):
        assert wf.name == "native_fc" and wf.server_parsed is True
        assert get("json_fc").server_parsed is False
        assert (
            wf.grammar([("read_file", {"path": ({"type": "string"}, True)}, False)])
            is None
        )

    def test_prose_says_function_calling_not_text(self, wf):
        rules = wf.format_rules()
        assert "function calling" in rules and "<tool_call>" in rules  # 금지로 언급
        assert "no JSON arrays" in rules
        assert "function call" in wf.failure_framing_parse_fail()

    def test_parses_flat_op_array_like_json_fc(self, wf):
        t = wf.parse_turn('plan\n\n[{"action": "read_file", "path": "a.py"}]')
        assert t.thought == "plan" and t.parse_stage == 1
        assert [(o.action, o.action_input) for o in t.ops] == [
            ("read_file", {"path": "a.py"})
        ]


class TestRenderToRequest:
    def test_assistant_record_becomes_tool_calls(self, wf):
        rec = {
            "role": "assistant",
            "thought": "read both",
            "ops": [
                {"action": "read_file", "action_input": {"path": "a"}},
                {"action": "shell", "action_input": {"command": "ls"}},
            ],
        }
        msg = wf.render_assistant_from_history(rec, index=7)
        assert msg["role"] == "assistant" and msg["content"] == "read both"
        assert [tc["id"] for tc in msg["tool_calls"]] == ["call_7_0", "call_7_1"]
        assert msg["tool_calls"][0]["type"] == "function"
        assert msg["tool_calls"][0]["function"]["name"] == "read_file"
        assert json.loads(msg["tool_calls"][0]["function"]["arguments"]) == {
            "path": "a"
        }
        # 텍스트 방언은 그대로 텍스트
        assert "tool_calls" not in get("json_fc").render_assistant_from_history(
            rec, index=7
        )

    def test_observation_parts_become_tool_messages(self, wf):
        rec = {
            "role": "user",
            "tool": "read_file×2",
            "success": True,
            "content": "Observation: [1/2] read_file — OK\nA\n\n[2/2] read_file — OK\nB",
            "parts": [
                {"tool": "read_file", "success": True, "content": "A"},
                {"tool": "read_file", "success": True, "content": "B"},
            ],
        }
        out = wf.render_observation_from_history(rec, index=8, assistant_index=7)
        assert out == [
            {"role": "tool", "tool_call_id": "call_7_0", "content": "A"},
            {"role": "tool", "tool_call_id": "call_7_1", "content": "B"},
        ]

    def test_observation_without_parts_is_one_tool_message(self, wf):
        rec = {
            "role": "user",
            "tool": "shell",
            "success": True,
            "content": "Observation: total 0",
            "artifact": "/tmp/x",
        }
        (m,) = wf.render_observation_from_history(rec, index=2, assistant_index=1)
        assert m == {
            "role": "tool",
            "tool_call_id": "call_1_0",
            "content": "total 0\n→ /tmp/x",
        }

    def test_text_dialect_returns_none(self):
        rec = {"role": "user", "tool": "shell", "content": "Observation: x"}
        assert (
            get("json_fc").render_observation_from_history(
                rec, index=1, assistant_index=0
            )
            is None
        )


class TestContextManagerAssembly:
    def test_messages_pair_ids_and_tail_goes_on_last_tool_message(self, tmp_path, wf):
        ctx = ContextManager(
            session_dir=tmp_path, max_context_tokens=20_000, dialect=wf
        )
        ctx.add({"role": "system", "content": "sys"})
        ctx.add({"role": "user", "content": "do it"})
        ctx.add(
            {
                "role": "assistant",
                "thought": "t",
                "ops": [
                    {"action": "read_file", "action_input": {"path": "a"}},
                    {"action": "read_file", "action_input": {"path": "b"}},
                ],
            }
        )
        ctx.add(
            {
                "role": "user",
                "tool": "read_file×2",
                "success": True,
                "content": "Observation: …",
                "parts": [
                    {"tool": "read_file", "success": True, "content": "A"},
                    {"tool": "read_file", "success": True, "content": "B"},
                ],
            }
        )
        ctx.set_session_state("## Live Agents\n(none)")
        msgs = ctx.get_messages()
        roles = [m["role"] for m in msgs]
        assert roles == ["system", "user", "assistant", "tool", "tool"]
        ids = [tc["id"] for tc in msgs[2]["tool_calls"]]
        assert [m["tool_call_id"] for m in msgs[3:]] == ids
        assert (
            msgs[-1]["content"].startswith("B")
            and "## Live Agents" in msgs[-1]["content"]
        )
        # 같은 기록을 텍스트 방언으로 읽으면 종전 모양 (스키마 공유)
        ctx2 = ContextManager(
            session_dir=tmp_path,
            max_context_tokens=20_000,
            dialect=get("json_fc"),
            resume=True,
        )
        roles2 = [m["role"] for m in ctx2.get_messages()]
        assert roles2 == ["system", "user", "assistant", "user"]


class TestSystemPromptAndSchemas:
    def test_available_tools_section_is_omitted(self, caps, wf):
        from agent_cli.prompts.system_prompt import build_system_prompt_sections

        names = [
            n
            for n, _ in build_system_prompt_sections(
                caps, active_tools=["read_file", "shell"], dialect=wf
            )
        ]
        assert "Available Tools" not in names
        assert "Response Format" in names
        names_json = [
            n
            for n, _ in build_system_prompt_sections(
                caps, active_tools=["read_file", "shell"], dialect=get("json_fc")
            )
        ]
        assert "Available Tools" in names_json

    def test_function_schemas_carry_full_guides(self, wf):
        from agent_cli.prompts.system_prompt import function_schemas_for

        schemas = function_schemas_for(["read_file", "edit_file", "shell"], wf)
        by = {s["function"]["name"]: s for s in schemas}
        assert "complete" in by and "edit_file" in by  # 항상 포함되는 종결 함수
        assert by["edit_file"]["type"] == "function"
        assert (
            "hashline" in by["edit_file"]["function"]["description"]
        )  # 가이드 전문 (N1)
        assert by["read_file"]["function"]["parameters"]["type"] == "object"


class TestLoop:
    def _provider(self, responses):
        provider = MagicMock()
        provider.call.side_effect = responses
        return provider

    def test_tool_calls_drive_the_loop_and_tools_are_sent(self, tmp_path, caps, wf):
        ctx = ContextManager(
            session_dir=tmp_path, max_context_tokens=30_000, dialect=wf
        )
        provider = self._provider(
            [
                LLMResponse(
                    content="I will read it.",
                    tool_calls=[
                        {"id": "x1", "name": "read_file", "input": {"path": "nope.txt"}}
                    ],
                    usage=TokenUsage(input_tokens=10, output_tokens=5),
                ),
                LLMResponse(
                    content="",
                    tool_calls=[
                        {"id": "x2", "name": "complete", "input": {"result": "done"}}
                    ],
                    usage=TokenUsage(input_tokens=10, output_tokens=5),
                ),
            ]
        )
        result = run_loop(
            ports=TEST_PORTS,
            query="Q",
            provider=provider,
            capabilities=caps,
            model="m",
            ctx=ctx,
            max_turns=5,
        )
        assert result.success and result.output == "done"
        first = provider.call.call_args_list[0].kwargs
        settings = first["settings"]
        assert settings.tools and any(
            t["function"]["name"] == "read_file" for t in settings.tools
        )
        assert settings.grammar is None
        # 두 번째 호출의 메시지: assistant(tool_calls) → tool(짝 맞는 id)
        msgs = provider.call.call_args_list[1].kwargs["messages"]
        asst = next(m for m in msgs if m["role"] == "assistant" and m.get("tool_calls"))
        tool_msg = msgs[msgs.index(asst) + 1]
        assert tool_msg["role"] == "tool"
        assert tool_msg["tool_call_id"] == asst["tool_calls"][0]["id"]
        assert asst["content"] == "I will read it."
        # 기록은 같은 스키마 — thought + ops
        rows = [
            json.loads(l)
            for l in (tmp_path / "history.jsonl").read_text().splitlines()
            if l
        ]
        a = next(r for r in rows if r.get("role") == "assistant")
        assert (
            a["thought"] == "I will read it." and a["ops"][0]["action"] == "read_file"
        )

    def test_batch_observation_stores_parts_for_every_dialect(self, tmp_path, caps):
        """기록은 방언 중립(v10.2.2): json_fc 세션도 op 별 조각을 남겨 native 로
        이어 읽을 때 호출 N 개에 `tool` 메시지 N 개가 짝지어진다."""
        for name in ("native_fc", "json_fc"):
            d = tmp_path / name
            ctx = ContextManager(
                session_dir=d, max_context_tokens=30_000, dialect=get(name)
            )
            two = [
                {"id": "a", "name": "read_file", "input": {"path": "x1"}},
                {"id": "b", "name": "read_file", "input": {"path": "x2"}},
            ]
            provider = self._provider(
                [
                    LLMResponse(
                        content="",
                        tool_calls=two,
                        usage=TokenUsage(input_tokens=1, output_tokens=1),
                    ),
                    LLMResponse(
                        content="",
                        tool_calls=[
                            {"id": "c", "name": "complete", "input": {"result": "ok"}}
                        ],
                        usage=TokenUsage(input_tokens=1, output_tokens=1),
                    ),
                ]
            )
            res = run_loop(
                ports=TEST_PORTS,
                query="Q",
                provider=provider,
                capabilities=caps,
                model="m",
                ctx=ctx,
                max_turns=5,
            )
            assert res.success, name
            rows = [
                json.loads(l)
                for l in (d / "history.jsonl").read_text().splitlines()
                if l
            ]
            obs = next(r for r in rows if r.get("role") == "user" and r.get("tool"))
            assert [p["tool"] for p in obs["parts"]] == ["read_file", "read_file"], name
            # 어느 방언의 세션이든 native 로 다시 읽으면 호출과 결과가 짝을 이룬다.
            resumed = ContextManager(
                session_dir=d,
                max_context_tokens=30_000,
                dialect=get("native_fc"),
                resume=True,
            )
            msgs = resumed.get_messages()
            calls = [
                c["id"]
                for m in msgs
                if m["role"] == "assistant"
                for c in m.get("tool_calls", [])
            ]
            results = [m["tool_call_id"] for m in msgs if m["role"] == "tool"]
            assert calls[:2] == results == ["call_1_0", "call_1_1"], name


def _unpaired(messages: list[dict]) -> list[str]:
    """`tool` messages that do not answer a call of the assistant message
    right before them — what the OpenAI message rules reject."""
    open_ids: set[str] = set()
    bad = []
    for m in messages:
        if m["role"] == "assistant":
            open_ids = {t["id"] for t in m.get("tool_calls") or []}
        elif m["role"] == "tool":
            if m["tool_call_id"] in open_ids:
                open_ids.discard(m["tool_call_id"])
            else:
                bad.append(m["tool_call_id"])
        else:
            open_ids = set()
    return bad


class TestRejectedCallKeepsTheMessageOrderValid:
    """v10.14.0. A format-rejected call (unknown tool, bad arguments) is not
    stored, so its rejection has no assistant `tool_calls` to answer. It used
    to go out as a `tool` message anyway — with no call before it, or reusing
    the id of an earlier, already answered call."""

    def _run(self, tmp_path, caps, wf, responses, active_tools=None):
        ctx = ContextManager(
            session_dir=tmp_path, max_context_tokens=30_000, dialect=wf
        )
        provider = MagicMock()
        provider.call.side_effect = responses
        result = run_loop(
            ports=TEST_PORTS,
            query="Q",
            provider=provider,
            capabilities=caps,
            model="m",
            ctx=ctx,
            max_turns=6,
            active_tools=active_tools,
        )
        assert result.success
        return [c.kwargs["messages"] for c in provider.call.call_args_list]

    @staticmethod
    def _call(name, args, content=""):
        return LLMResponse(
            content=content,
            tool_calls=[{"id": "x", "name": name, "input": args}],
            usage=TokenUsage(input_tokens=10, output_tokens=5),
        )

    def test_unknown_tool_on_the_first_turn(self, tmp_path, caps, wf):
        calls = self._run(
            tmp_path,
            caps,
            wf,
            [
                self._call("shell", {"command": "ls"}),
                self._call("complete", {"result": "done"}),
            ],
            active_tools=["read_file"],
        )
        second = calls[1]
        assert _unpaired(second) == []
        assert second[-1]["role"] == "user"
        assert "Unknown tool 'shell'" in second[-1]["content"]

    def test_bad_arguments_on_the_first_turn(self, tmp_path, caps, wf):
        calls = self._run(
            tmp_path,
            caps,
            wf,
            [
                self._call("read_file", {"bogus": 1}),
                self._call("complete", {"result": "done"}),
            ],
        )
        assert _unpaired(calls[1]) == []
        assert calls[1][-1]["role"] == "user"

    def test_rejection_after_an_answered_call_does_not_reuse_its_id(
        self, tmp_path, caps, wf
    ):
        calls = self._run(
            tmp_path,
            caps,
            wf,
            [
                self._call("read_file", {"path": "nope.txt"}),
                self._call("shell", {"command": "ls"}),
                self._call("complete", {"result": "done"}),
            ],
            active_tools=["read_file"],
        )
        third = calls[2]
        assert _unpaired(third) == []
        assert [m["role"] for m in third[-3:]] == ["assistant", "tool", "user"]
        assert "Unknown tool 'shell'" in third[-1]["content"]

    def test_a_paired_observation_is_still_a_tool_message(self, tmp_path, caps, wf):
        calls = self._run(
            tmp_path,
            caps,
            wf,
            [
                self._call("read_file", {"path": "nope.txt"}),
                self._call("complete", {"result": "done"}),
            ],
        )
        assert _unpaired(calls[1]) == []
        assert calls[1][-1]["role"] == "tool"
