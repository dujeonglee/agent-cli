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
    def test_messages_pair_ids_and_tail_is_its_own_user_message(self, tmp_path, wf):
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
        # 꼬리(세션 상태·complete 안내)는 도구 결과가 아니라 하니스의 말 — `tool`
        # 뒤 user 메시지로 따로 (v10.22.0; 종전엔 마지막 tool 본문 끝에 붙였다).
        assert roles == ["system", "user", "assistant", "tool", "tool", "user"]
        ids = [tc["id"] for tc in msgs[2]["tool_calls"]]
        assert [m["tool_call_id"] for m in msgs[3:5]] == ids
        assert msgs[4]["content"] == "B"
        assert msgs[-1]["content"].startswith("(If nothing remains to do")
        assert "## Live Agents" in msgs[-1]["content"]
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
            assert calls[:2] == results[:2] == ["call_1_0", "call_1_1"], name
            # 종결 호출까지 포함해 모든 호출에 결과가 있다 (v10.20.0).
            assert calls == results, name


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
        # the list is the one the model was given — `complete` included
        assert (
            "Unknown tool 'shell'. Available: read_file, complete"
            in second[-1]["content"]
        )

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
        assert calls[1][-2]["role"] == "tool"
        assert calls[1][-1]["role"] == "user"  # 꼬리(complete 안내)


class TestEveryCallGetsAResult:
    """v10.20.0. OpenAI 규격: 모든 ``tool_calls`` 뒤에는 같은 id 의 `tool`
    메시지가 온다. 기록은 그렇지 않은 턴을 만든다 — 같은 파일 편집 N 개는
    결과가 하나, `complete` 는 결과가 없다. 렌더에서 채운다."""

    def _run(self, tmp_path, caps, wf, responses):
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
        )
        return result, ctx, [c.kwargs["messages"] for c in provider.call.call_args_list]

    @staticmethod
    def _resp(calls, content=""):
        return LLMResponse(
            content=content,
            tool_calls=[
                {"id": f"x{i}", "name": n, "input": a} for i, (n, a) in enumerate(calls)
            ],
            usage=TokenUsage(input_tokens=10, output_tokens=5),
        )

    def test_same_file_edits_each_get_their_own_result(self, tmp_path, caps, wf):
        f = tmp_path / "f.txt"
        f.write_text("a\nb\nc\n")
        from agent_cli.tools.read_file import compute_line_hash

        refs = [
            f"{i}#{compute_line_hash(i, line)}"
            for i, line in enumerate(["a", "b", "c"], 1)
        ]
        result, _ctx, calls = self._run(
            tmp_path,
            caps,
            wf,
            [
                self._resp(
                    [
                        (
                            "edit_file",
                            {
                                "path": str(f),
                                "op": "replace",
                                "pos": refs[0],
                                "lines": "A",
                            },
                        ),
                        (
                            "edit_file",
                            {
                                "path": str(f),
                                "op": "replace",
                                "pos": refs[2],
                                "lines": "C",
                            },
                        ),
                    ]
                ),
                self._resp([("complete", {"result": "done"})]),
            ],
        )
        assert result.output == "done"
        second = calls[1]
        assert _unpaired(second) == []
        tools = [m for m in second if m["role"] == "tool"]
        assert [m["tool_call_id"] for m in tools] == ["call_1_0", "call_1_1"]
        # 호출마다 자기 결과 (v10.23.0): 각 편집의 원본 줄 범위와 -/+, 마지막
        # 호출에 파일 요약 + 새 해시라인 에코.
        assert tools[0]["content"].startswith(
            f"replace {refs[0]}: replaced original lines 1–1"
        )
        assert "- a\n+ A" in tools[0]["content"]
        assert "Edit complete" not in tools[0]["content"]
        assert tools[1]["content"].startswith(
            f"replace {refs[2]}: replaced original lines 3–3"
        )
        assert "- c\n+ C" in tools[1]["content"]
        assert (
            "Edit complete" in tools[1]["content"] and "2 edits," in tools[1]["content"]
        )

    def test_complete_gets_a_result_on_resume(self, tmp_path, caps, wf):
        _result, _ctx, _calls = self._run(
            tmp_path, caps, wf, [self._resp([("complete", {"result": "done"})])]
        )
        resumed = ContextManager(
            session_dir=tmp_path, max_context_tokens=30_000, dialect=wf, resume=True
        )
        resumed.add({"role": "user", "content": "next"})
        msgs = resumed.get_messages()
        assert _unpaired(msgs) == []
        i = next(k for k, m in enumerate(msgs) if m.get("tool_calls"))
        assert msgs[i]["tool_calls"][0]["function"]["name"] == "complete"
        assert msgs[i + 1] == {
            "role": "tool",
            "tool_call_id": "call_1_0",
            "content": "completed task: Q",
        }
        assert msgs[i + 2]["role"] == "user"

    def test_text_dialect_is_untouched(self):
        d = get("json_fc")
        msgs = [{"role": "assistant", "content": "x"}, {"role": "user", "content": "y"}]
        assert d.pair_call_results(msgs) is msgs


class TestServerCallsAreNotReparsed:
    """v10.20.0. 호출은 서버가 구조로 준다 — 텍스트로 바꿔 다시 파싱하지 않는다."""

    def _run(self, tmp_path, caps, wf, responses):
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
        )
        return result, ctx, [c.kwargs["messages"] for c in provider.call.call_args_list]

    def test_array_in_the_prose_does_not_replace_the_real_call(
        self, tmp_path, caps, wf
    ):
        """종전: 산문 속 `[{"action": …}]` 이 "첫 배열" 로 이겨 실제 호출이 버려졌다."""
        (tmp_path / "real.txt").write_text("REAL")
        result, ctx, calls = self._run(
            tmp_path,
            caps,
            wf,
            [
                LLMResponse(
                    content='Not [{"action": "read_file", "path": "decoy.txt"}] — the real one:',
                    tool_calls=[
                        {
                            "id": "x",
                            "name": "read_file",
                            "input": {"path": str(tmp_path / "real.txt")},
                        }
                    ],
                    usage=TokenUsage(input_tokens=10, output_tokens=5),
                ),
                LLMResponse(
                    content="",
                    tool_calls=[
                        {"id": "y", "name": "complete", "input": {"result": "done"}}
                    ],
                    usage=TokenUsage(input_tokens=10, output_tokens=5),
                ),
            ],
        )
        assert result.output == "done"
        tool_msgs = [m for m in calls[1] if m["role"] == "tool"]
        assert "REAL" in tool_msgs[0]["content"]
        rec = next(r for r in ctx.get_raw_messages() if r.get("ops"))
        assert rec["ops"][0]["action_input"]["path"].endswith("real.txt")
        assert "decoy" in rec["thought"]  # 산문은 생각으로 남는다

    def test_broken_arguments_are_reported_not_executed(self, tmp_path, caps, wf):
        """provider 가 JSON 이 아닌 인자를 원문으로 넘기면: 실행 없이 어디서
        깨졌는지 알린다. 종전엔 빈 인자로 실행돼 "필수 인자 없음" 이 됐다."""
        target = tmp_path / "x.txt"
        result, _ctx, calls = self._run(
            tmp_path,
            caps,
            wf,
            [
                LLMResponse(
                    content="",
                    tool_calls=[
                        {
                            "id": "x",
                            "name": "write_file",
                            "input": None,
                            "arguments": f'{{"path": "{target}", "content": "oops',
                        }
                    ],
                    usage=TokenUsage(input_tokens=10, output_tokens=5),
                ),
                LLMResponse(
                    content="",
                    tool_calls=[
                        {"id": "y", "name": "complete", "input": {"result": "done"}}
                    ],
                    usage=TokenUsage(input_tokens=10, output_tokens=5),
                ),
            ],
        )
        assert result.output == "done"
        assert not target.exists()
        nudge = calls[1][-1]["content"]
        assert "were not valid JSON" in nudge
        assert "Missing required" not in nudge
        assert '"content": "oops' in nudge  # 원문 인용

    def test_broken_complete_does_not_end_the_run(self, tmp_path, caps, wf):
        """종전: `complete` 의 깨진 인자가 빈 dict 가 되어 런이 답 없이 끝났다."""
        result, _ctx, calls = self._run(
            tmp_path,
            caps,
            wf,
            [
                LLMResponse(
                    content="",
                    tool_calls=[
                        {
                            "id": "x",
                            "name": "complete",
                            "input": None,
                            "arguments": '{"result": "half',
                        }
                    ],
                    usage=TokenUsage(input_tokens=10, output_tokens=5),
                ),
                LLMResponse(
                    content="",
                    tool_calls=[
                        {"id": "y", "name": "complete", "input": {"result": "whole"}}
                    ],
                    usage=TokenUsage(input_tokens=10, output_tokens=5),
                ),
            ],
        )
        assert result.output == "whole"
        assert len(calls) == 2

    def test_batch_rejections_land_on_their_own_call_ids(self, tmp_path, caps, wf):
        """배치 안의 거부(깨진 인자·인자 누락·모르는 도구)는 자기 호출 id 의
        `tool` 메시지로 간다 — 결과가 다른 호출에 붙지 않는다."""
        (tmp_path / "a.txt").write_text("A")
        (tmp_path / "d.txt").write_text("D")
        result, _ctx, calls = self._run(
            tmp_path,
            caps,
            wf,
            [
                LLMResponse(
                    content="",
                    tool_calls=[
                        {
                            "id": "1",
                            "name": "read_file",
                            "input": {"path": str(tmp_path / "a.txt")},
                        },
                        {
                            "id": "2",
                            "name": "read_file",
                            "input": None,
                            "arguments": "{bad",
                        },
                        {"id": "3", "name": "read_file", "input": {}},
                        {"id": "4", "name": "nosuch_tool", "input": {"x": 1}},
                        {
                            "id": "5",
                            "name": "read_file",
                            "input": {"path": str(tmp_path / "d.txt")},
                        },
                    ],
                    usage=TokenUsage(input_tokens=10, output_tokens=5),
                ),
                LLMResponse(
                    content="",
                    tool_calls=[
                        {"id": "y", "name": "complete", "input": {"result": "done"}}
                    ],
                    usage=TokenUsage(input_tokens=10, output_tokens=5),
                ),
            ],
        )
        assert result.output == "done"
        tools = [m for m in calls[1] if m["role"] == "tool"]
        assert [m["tool_call_id"] for m in tools] == [f"call_1_{i}" for i in range(5)]
        assert "A" in tools[0]["content"]
        assert "not valid JSON" in tools[1]["content"]
        assert "Missing required" in tools[2]["content"]
        assert "Unknown tool" in tools[3]["content"]
        assert "D" in tools[4]["content"]

    def test_terminal_result_quotes_the_request_it_answered(self):
        """종결 결과는 그 턴이 답한 요청(첫 줄, 80자)을 인용한다 — 하니스
        안내문(형식 넛지 등)은 요청이 아니다."""
        d = get("native_fc")
        long_req = "x" * 100 + "\nsecond line"
        msgs = [
            {"role": "user", "content": long_req},
            {"role": "user", "content": "Your response contained no function call — …"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "call_2_0",
                        "type": "function",
                        "function": {"name": "complete", "arguments": "{}"},
                    }
                ],
            },
        ]
        out = d.pair_call_results(msgs)
        assert out[-1]["content"] == "completed task: " + "x" * 80
        # 요청이 없으면 문구만
        assert d.pair_call_results(msgs[2:])[-1]["content"] == "completed task"

    def test_terminal_result_states_the_harness_bookkeeping(self):
        """`answers` 가 있으면 `complete` 의 결과는 하니스가 실제로 한 회계다
        (v10.23.0): 주장된 요청은 큐에서 지워지고, 남은 것이 보인다."""
        from agent_cli.context.render import render_history_message

        d = get("native_fc")
        rec = d.serialize_terminal_for_history(
            "", "done", answers=["17", "18"], open_requests=["19"]
        )
        assert rec["open_requests"] == ["19"]
        msgs = render_history_message(rec, d, index=4, assistant_index=None)
        assert [m["role"] for m in msgs] == ["assistant", "tool"]
        assert msgs[1]["tool_call_id"] == "call_4_0"
        assert msgs[1]["content"] == (
            "completed task. Requests [17][18] removed from the user request "
            "queue. Still pending: [19]."
        )
        # 남은 것이 없을 때·하나일 때
        rec = d.serialize_terminal_for_history(
            "", "x", answers=["17"], open_requests=[]
        )
        assert render_history_message(rec, d, index=1, assistant_index=None)[1][
            "content"
        ] == (
            "completed task. Request [17] removed from the user request queue. "
            "No request pending."
        )
        # 옛 세션(answers 는 있고 남은 목록은 없음) — 회계 뒷문장 없이
        rec = d.serialize_terminal_for_history("", "x", answers=["17"])
        assert "open_requests" not in rec
        assert render_history_message(rec, d, index=1, assistant_index=None)[1][
            "content"
        ].endswith("queue.")
        # answers 없음 → 결과 메시지 없음 (pair_call_results 가 요청 인용으로 채움)
        rec = d.serialize_terminal_for_history("", "x")
        assert len(render_history_message(rec, d, index=1, assistant_index=None)) == 1
        # 텍스트 방언은 그대로 한 건
        rec_t = get("json_fc").serialize_terminal_for_history(
            "", "x", answers=["17"], open_requests=[]
        )
        assert (
            len(
                render_history_message(
                    rec_t, get("json_fc"), index=1, assistant_index=None
                )
            )
            == 1
        )


class TestTerminalBookkeepingReachesTheModel:
    """실제 루프: `answers` 를 보낸 complete 뒤 다음 요청 본문에 회계 `tool`
    메시지가 실린다 (v10.23.0) — 렌더 함수가 아니라 provider 가 받은 messages 로."""

    def test_next_request_carries_the_settled_queue(self, tmp_path, caps, wf):
        from tests.loop_ports import make_ports

        queue = [None, {"id": "2", "nickname": "Ann", "text": "REQ-B"}]

        def resp(calls, content=""):
            return LLMResponse(
                content=content,
                tool_calls=[
                    {"id": f"x{i}", "name": n, "input": a}
                    for i, (n, a) in enumerate(calls)
                ],
                usage=TokenUsage(input_tokens=10, output_tokens=5),
            )

        (tmp_path / "a.txt").write_text("A")
        provider = MagicMock()
        provider.call.side_effect = [
            resp([("read_file", {"path": str(tmp_path / "a.txt")})]),
            resp([("complete", {"result": "A done", "answers": ["1"]})]),
            resp([("complete", {"result": "B done", "answers": ["2"]})]),
        ]
        ctx = ContextManager(
            session_dir=tmp_path, max_context_tokens=30_000, dialect=wf
        )
        result = run_loop(
            query="REQ-A",
            query_author="Bob",
            query_request_id="1",
            provider=provider,
            capabilities=caps,
            model="m",
            ctx=ctx,
            max_turns=6,
            dialect=wf,
            ports=make_ports(
                owner="main",
                dequeue_user_message=lambda: queue.pop(0) if queue else None,
            ),
        )
        assert result.output.strip() == "B done"
        # 부분 응답(독촉이 이어짐): 그 complete 의 답은 독촉 관찰 하나다 —
        # 회계 문구를 따로 합성해 같은 id 를 둘로 만들지 않는다.
        third = provider.call.call_args_list[2].kwargs["messages"]
        assert _unpaired(third) == []
        i = next(
            k
            for k, m in enumerate(third)
            if any(
                t["function"]["name"] == "complete" for t in m.get("tool_calls") or []
            )
        )
        replies = [m for m in third[i + 1 :] if m["role"] == "tool"]
        assert len(replies) == 1
        assert "still unanswered" in replies[0]["content"]
        assert '[2] (Ann) "REQ-B"' in replies[0]["content"]
        # 최종 complete(남은 것 없음) 뒤에 이어지는 요청: 회계 문구가 답이다.
        resumed = ContextManager(
            session_dir=tmp_path, max_context_tokens=30_000, dialect=wf, resume=True
        )
        resumed.add({"role": "user", "content": "REQ-C"})
        msgs = resumed.get_messages()
        assert _unpaired(msgs) == []
        last_complete = max(
            k
            for k, m in enumerate(msgs)
            if any(
                t["function"]["name"] == "complete" for t in m.get("tool_calls") or []
            )
        )
        assert msgs[last_complete + 1] == {
            "role": "tool",
            "tool_call_id": msgs[last_complete]["tool_calls"][0]["id"],
            "content": (
                "completed task. Request [2] removed from the user request queue. "
                "No request pending."
            ),
        }
