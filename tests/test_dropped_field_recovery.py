"""Dropped-field recovery: the ``action_required`` flag.

The wire-format flag governs what happens when an emission is missing its
action:

  - ``action_required=False`` → a dropped/empty action is recovered by the
    loop via ``infer_action`` on the *preserved* action_input (wire-key
    prefix → tool). ``True`` → straight to NO_ACTION recovery.

A missing *thought* is always tolerated (v9.23.3): the NO_THOUGHT recovery and
its ``thought_required`` flag were removed as dead code — both shipped
formats set it False and none implemented ``format_no_thought_retry``.

The parser-side invariant (``WireFormat.parse`` contract) is that
action_input is preserved even when the action slot is empty/invalid, so
both flag branches have something to work with. This file pins:

  1. Both shipped parsers (json_fc / xml_fc) preserve action_input across
     dropped-action shapes (v7.0.0 — react 제거로 쌍이 json_fc/xml_fc 로).
  2. Cross-wire parity: same semantic emission → same recovery outcome.
  3. The loop honors the flag: False → infer, True → recover. The shipped
     plugins both set False, so the True branch is pinned against a
     synthetic strict plugin.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from agent_cli.loop import run_loop
from agent_cli.providers.base import LLMResponse
from agent_cli.providers.capabilities import ModelCapabilities
from agent_cli.tools.registry import infer_action
from agent_cli.wire_formats import get
from agent_cli.wire_formats.json_fc import JsonFcFormat
from tests.loop_ports import TEST_PORTS

# ── Fixtures / helpers ───────────────────────────────


@pytest.fixture
def caps():
    return ModelCapabilities(
        context_window=32768,
        max_output_tokens=4096,
        supports_thinking=False,
    )


def _make_provider(*responses):
    provider = MagicMock()
    provider.call.side_effect = [LLMResponse(content=r) for r in responses]
    return provider


def _complete(result: str) -> str:
    return f'done\n\n[{{"action": "complete", "result": "{result}"}}]'


class _StrictJson(JsonFcFormat):
    """Synthetic plugin pinning the True branch of ``action_required``. The
    shipped plugins are False, so without this the recovery path for a
    *required* action would be untested. parse 는 상속 — loop 의
    플래그-게이트 분기만 다르다."""

    action_required = True


# ── 1. Parser preserves action_input across dropped-action shapes ──

_JSON_FC_CASES = [
    (
        "actionless_op_in_array",
        'x\n\n[{"shell_command": "make"}]',
        {"shell_command": "make"},
    ),
    (
        "bare_actionless_object",
        '{"shell_command": "make"}',
        {"shell_command": "make"},
    ),
    (
        "empty_action_string",
        '[{"action": "", "shell_command": "make"}]',
        {"shell_command": "make"},
    ),
]


class TestJsonFcPreservation:
    @pytest.mark.parametrize(
        "name,raw,exp_input",
        _JSON_FC_CASES,
        ids=[c[0] for c in _JSON_FC_CASES],
    )
    def test_parse_preserves_input(self, name, raw, exp_input):
        parsed = get("json_fc").parse(raw)
        assert parsed.action_input == exp_input
        assert not parsed.action  # dropped → loop will infer / NO_ACTION echo
        assert infer_action(parsed.action_input) == "shell"

    def test_thought_only_is_unrecoverable(self):
        parsed = get("json_fc").parse("just thinking, no ops")
        assert not parsed.action
        assert parsed.action_input is None


class TestXmlFcPreservation:
    def test_empty_function_name_preserves_params(self):
        parsed = get("xml_fc").parse(
            "<tool_call>\n<function=>\n"
            "<parameter=shell_command>make</parameter>\n"
            "</function>\n</tool_call>"
        )
        assert parsed.action_input == {"shell_command": "make"}
        assert not parsed.action
        assert infer_action(parsed.action_input) == "shell"


# ── 2. Cross-wire parity ─────────────────────────────


class TestCrossWireParity:
    def test_dropped_action_same_outcome(self):
        # 두 내장 포맷이 같은 의미의 emission(action 없는 op, prefixed
        # param)에서 같은 dropped-action 복구 지점에 도달한다.
        jt = get("json_fc").parse_turn('x\n\n[{"shell_command": "ls"}]')
        xt = get("xml_fc").parse_turn(
            "x\n\n<tool_call>\n<function=>\n"
            "<parameter=shell_command>ls</parameter>\n</function>\n</tool_call>"
        )
        assert len(jt.ops) == len(xt.ops) == 1
        assert jt.ops[0].action is None and xt.ops[0].action is None
        assert (
            jt.ops[0].action_input == xt.ops[0].action_input == {"shell_command": "ls"}
        )
        assert (
            infer_action(jt.ops[0].action_input)
            == infer_action(xt.ops[0].action_input)
            == "shell"
        )

    def test_shipped_plugins_optional_by_default(self):
        for name in ("json_fc", "xml_fc"):
            plugin = get(name)
            assert plugin.action_required is False, name
            assert not hasattr(plugin, "thought_required"), name  # v9.23.3 제거


# ── 3. Loop honors the flags ─────────────────────────
# 복구 여부는 도구의 파일 부수효과로 측정 (메시지 텍스트 스캔 금지 —
# NO_ACTION 개입이 raw 를 echo 하므로 텍스트는 오탐).


class TestActionRequiredGate:
    def test_false_flat_dropped_action_falls_to_no_action(self, caps, tmp_path):
        # flat input 의 dropped action 은 infer 불가(다수 도구가 `path` 공유)
        # → action_required=False 여도 NO_ACTION 복구로 (자동 디스패치 없음).
        target = tmp_path / "made.txt"
        provider = _make_provider(
            f'x\n\n[{{"path": "{target}", "content": "data"}}]',
            _complete("done"),
        )
        result = run_loop(
            ports=TEST_PORTS,
            query="go",
            provider=provider,
            capabilities=caps,
            model="m",
            wire_format=JsonFcFormat(),
        )
        assert result.success
        assert provider.call.call_count == 2  # NO_ACTION retry (infer can't help)
        assert not target.exists()  # not auto-dispatched

    def test_infer_machinery_preserved_for_prefixed_input(self):
        # dropped-action 복구 SEAM 의 권위 pin — 전 도구 flat-native 후에도
        # 의도적으로 보존된 latent 기계 (미래 prefixed 도구/포맷용).
        assert (
            infer_action({"write_file_path": "x", "write_file_content": "y"})
            == "write_file"
        )
        assert infer_action({"path": "x"}) is None  # flat = ambiguous

    def test_true_skips_infer_and_recovers(self, caps, tmp_path):
        target = tmp_path / "made.txt"
        provider = _make_provider(
            f'x\n\n[{{"path": "{target}", "content": "data"}}]',
            _complete("done"),
        )
        result = run_loop(
            ports=TEST_PORTS,
            query="go",
            provider=provider,
            capabilities=caps,
            model="m",
            wire_format=_StrictJson(),
        )
        assert result.success
        assert provider.call.call_count == 2  # NO_ACTION retry happened
        assert not target.exists()


class TestMissingThoughtIsTolerated:
    def test_missing_thought_runs(self, caps, tmp_path):
        # 산문 없이 배열만 — 생각은 선택 → 그대로 실행.
        target = tmp_path / "made.txt"
        provider = _make_provider(
            f'[{{"action": "write_file", "path": "{target}", "content": "data"}}]',
            _complete("done"),
        )
        result = run_loop(
            ports=TEST_PORTS,
            query="go",
            provider=provider,
            capabilities=caps,
            model="m",
            wire_format=JsonFcFormat(),
        )
        assert result.success
        assert target.exists()  # ran despite missing thought


# ── 4. Prompt wording ─────────────────────────────


class TestPromptWording:
    def test_prompts_keep_strong_action_wording(self):
        # action 은 플래그와 무관하게 강한 의무 문구다.
        fr = get("json_fc").format_rules()
        assert 'must have an "action"' in fr
