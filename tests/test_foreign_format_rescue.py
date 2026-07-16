"""Phase 3 — foreign-format 구제 (multi-wire-format DESIGN §9).

바인딩 포맷이 0-op 로 읽는 emission 을 타 등록 포맷 파서가 action-보유
ops 로 읽어내면 그 turn 으로 진행하고 ``FAILURE_FOREIGN_FORMAT`` 라벨.
실측 근거: 35B bakeoff 0-op 캡처의 17%가 json_fc 회귀 (PHASE2.md §8).
prior 는 바인딩 포맷의 캐노니컬 shape 로 재렌더 (corrected_record 경로,
B→C) — 누출 raw 재공급으로 인한 mimicry 강화 없음.
"""

import json
from unittest.mock import MagicMock

import pytest

from agent_cli.providers.base import LLMResponse
from agent_cli.providers.capabilities import ModelCapabilities
from agent_cli.wire_formats import get as get_wf, try_foreign_parse

# 실측 캡처 (35B, 2026-07-17) — json_fc 회귀 예1
MD_ARRAY_LEAK = (
    "## Thought\n"
    "The shell output shows a directory listing. Let me check app.py.\n\n"
    "## Action\n"
    '[{"action": "shell", "command": "head -50 app.py"}]'
)

# 실측 캡처 예5 — json_fc 회귀 + 미닫힘 배열 (json_fc 수리 기계가 살림)
MD_ARRAY_LEAK_UNCLOSED = (
    "## Thought\n\n\n"
    "## Action\n"
    '[{"action": "edit_file", "op": "replace", "path": "src/auth.py", '
    '"pos": "2#KT", "lines": ["    return bcrypt.hashpw(x)"]}'
)

# 실측 캡처 예4 — 키메라 (JSON 이 중간에 태그로 변이; 어느 파서로도 불가)
CHIMERA = (
    "Your reasoning here.\n\n"
    "## Action\n"
    '[{"action": "shell", "command">cat app.py</parameter>'
)


@pytest.fixture
def production_default(monkeypatch):
    """conftest 는 유닛 스위트 전역에서 DEFAULT 를 react 로 핀 — 구제
    순서(DEFAULT 우선)를 검증하는 테스트는 프로덕션 기본(json_fc)으로."""
    monkeypatch.setattr("agent_cli.wire_formats.DEFAULT_WIRE_FORMAT", "json_fc")


class TestTryForeignParse:
    def test_json_fc_leak_rescued_in_xml_fc_stream(self, production_default):
        rescued = try_foreign_parse(get_wf("xml_fc"), MD_ARRAY_LEAK)
        assert rescued is not None
        turn, source = rescued
        assert source == "json_fc"
        assert turn.ops[0].action == "shell"
        assert turn.ops[0].action_input == {"command": "head -50 app.py"}
        assert "directory listing" in turn.thought

    def test_unclosed_array_leak_rescued_via_json_fc_repair(self, production_default):
        rescued = try_foreign_parse(get_wf("xml_fc"), MD_ARRAY_LEAK_UNCLOSED)
        assert rescued is not None
        turn, source = rescued
        assert source == "json_fc"
        assert turn.ops[0].action == "edit_file"
        assert turn.ops[0].action_input["pos"] == "2#KT"

    def test_xml_fc_leak_rescued_in_json_fc_stream(self):
        # 대칭: json_fc-바인딩 스트림에 xml_fc emission
        xml_emission = (
            "reading now.\n\n<tool_call>\n<function=read_file>\n"
            "<parameter=path>a.py</parameter>\n</function>\n</tool_call>"
        )
        rescued = try_foreign_parse(get_wf("json_fc"), xml_emission)
        assert rescued is not None
        turn, source = rescued
        assert source == "xml_fc"
        assert turn.ops[0].action == "read_file"

    def test_chimera_not_rescued(self):
        assert try_foreign_parse(get_wf("xml_fc"), CHIMERA) is None

    def test_plain_prose_not_rescued(self):
        assert try_foreign_parse(get_wf("xml_fc"), "그냥 생각만 서술한다.") is None

    def test_bound_format_never_selected(self, production_default):
        # 계약: 바인딩 포맷 자신은 절대 소스가 아니다. (타 포맷 — react 의
        # 3-stage 파서 — 가 대신 읽어낼 수는 있음; dispatch 에선 이 emission
        # 이 애초에 바인딩 파서로 성공하므로 구제 경로에 오지도 않는다.)
        rescued = try_foreign_parse(get_wf("json_fc"), MD_ARRAY_LEAK)
        assert rescued is None or rescued[1] != "json_fc"

    def test_default_format_tried_first(self, production_default):
        # 순서 결정성: DEFAULT(json_fc) 가 최우선 (누출 최빈 포맷)
        rescued = try_foreign_parse(get_wf("xml_fc"), MD_ARRAY_LEAK)
        assert rescued is not None and rescued[1] == "json_fc"


# ── dispatch e2e — 구제 실행·라벨·prior 캐노니컬 재렌더 ──────


@pytest.fixture
def caps():
    return ModelCapabilities(
        context_window=32768,
        max_output_tokens=4096,
        supports_thinking=False,
        thinking_budget=0,
    )


def _provider(*responses):
    p = MagicMock()
    p.call.side_effect = [LLMResponse(content=r) for r in responses]
    return p


def _xml_complete(result: str) -> str:
    return (
        "<tool_call>\n<function=complete>\n"
        f"<parameter=result>{result}</parameter>\n</function>\n</tool_call>"
    )


class TestDispatchRescue:
    def test_leaked_json_fc_op_executes_in_xml_fc_loop(
        self, caps, tmp_path, production_default
    ):
        from agent_cli.context.manager import ContextManager
        from agent_cli.loop import run_loop

        f = tmp_path / "note.txt"
        f.write_text("payload-xyz")
        leak = (
            "## Thought\nreading the file.\n\n## Action\n"
            f'[{{"action": "read_file", "path": "{f}"}}]'
        )
        ctx = ContextManager(
            session_dir=tmp_path / "s",
            max_context_tokens=100_000,
            wire_format=get_wf("xml_fc"),
        )
        provider = _provider(leak, _xml_complete("saw payload-xyz"))
        result = run_loop(
            query="read note.txt",
            provider=provider,
            capabilities=caps,
            model="m",
            ctx=ctx,
            wire_format="xml_fc",
            record_turns=True,
        )
        assert result.success
        assert "payload-xyz" in result.output or "saw" in result.output
        # 도구가 실제 실행돼 관찰이 둘째 콜 메시지에 도달
        args, kwargs = provider.call.call_args_list[1]
        msgs = args[0] if args else kwargs.get("messages")
        joined = str(msgs)
        assert "payload-xyz" in joined
        # prior 캐노니컬 재렌더: 누출 raw(## Action)가 아니라 xml_fc shape
        assert "<function=read_file>" in joined
        assert "## Action" not in joined

    def test_rescue_labeled_foreign_format_in_turns_jsonl(
        self, caps, tmp_path, production_default
    ):
        from agent_cli.context.manager import ContextManager
        from agent_cli.loop import run_loop

        f = tmp_path / "a.txt"
        f.write_text("x")
        leak = f'## Action\n[{{"action": "read_file", "path": "{f}"}}]'
        ctx = ContextManager(
            session_dir=tmp_path / "s",
            max_context_tokens=100_000,
            wire_format=get_wf("xml_fc"),
        )
        provider = _provider(leak, _xml_complete("done"))
        run_loop(
            query="q",
            provider=provider,
            capabilities=caps,
            model="m",
            ctx=ctx,
            wire_format="xml_fc",
            record_turns=True,
        )
        turns = (tmp_path / "s" / "turns.jsonl").read_text().splitlines()
        signals = [json.loads(t).get("failure_signal") for t in turns]
        assert "FOREIGN_FORMAT" in signals
        prims = [p for t in turns for p in json.loads(t).get("primitives_applied", [])]
        assert any(p.startswith("foreign_parse:json_fc") for p in prims)

    def test_history_record_is_ops_shaped_not_bare(
        self, caps, tmp_path, production_default
    ):
        from agent_cli.context.manager import ContextManager
        from agent_cli.loop import run_loop

        f = tmp_path / "a.txt"
        f.write_text("x")
        leak = f'## Action\n[{{"action": "read_file", "path": "{f}"}}]'
        ctx = ContextManager(
            session_dir=tmp_path / "s",
            max_context_tokens=100_000,
            wire_format=get_wf("xml_fc"),
        )
        provider = _provider(leak, _xml_complete("done"))
        run_loop(
            query="q",
            provider=provider,
            capabilities=caps,
            model="m",
            ctx=ctx,
            wire_format="xml_fc",
        )
        recs = [
            json.loads(line)
            for line in (tmp_path / "s" / "history.jsonl").read_text().splitlines()
        ]
        assistants = [r for r in recs if r.get("role") == "assistant"]
        # 구제 턴은 bare content 가 아니라 구조화 ops 레코드로 저장
        assert any(
            r.get("ops") and r["ops"][0].get("action") == "read_file"
            for r in assistants
        )

    def test_leaked_terminal_complete_finishes_run(
        self, caps, tmp_path, production_default
    ):
        from agent_cli.context.manager import ContextManager
        from agent_cli.loop import run_loop

        leak = (
            "## Thought\ndone.\n\n## Action\n"
            '[{"action": "complete", "result": "final answer via leak"}]'
        )
        ctx = ContextManager(
            session_dir=tmp_path / "s",
            max_context_tokens=100_000,
            wire_format=get_wf("xml_fc"),
        )
        provider = _provider(leak)
        result = run_loop(
            query="q",
            provider=provider,
            capabilities=caps,
            model="m",
            ctx=ctx,
            wire_format="xml_fc",
        )
        assert result.success
        assert result.output == "final answer via leak"

    def test_unrescuable_prose_still_no_action_nudge(self, caps, tmp_path):
        from agent_cli.context.manager import ContextManager
        from agent_cli.loop import run_loop

        ctx = ContextManager(
            session_dir=tmp_path / "s",
            max_context_tokens=100_000,
            wire_format=get_wf("xml_fc"),
        )
        provider = _provider("생각만 서술하고 아무 호출도 없다.", _xml_complete("ok"))
        result = run_loop(
            query="q",
            provider=provider,
            capabilities=caps,
            model="m",
            ctx=ctx,
            wire_format="xml_fc",
            record_turns=True,
        )
        assert result.success  # NO_ACTION 넛지 후 둘째 emission 으로 완료
        turns = (tmp_path / "s" / "turns.jsonl").read_text().splitlines()
        signals = [json.loads(t).get("failure_signal") for t in turns]
        assert "NO_ACTION" in signals
