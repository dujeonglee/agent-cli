"""Dropped-field recovery: ``thought_required`` / ``action_required`` flags.

Two symmetric wire-format flags govern what happens when an emission is
missing a structured field:

  - ``action_required=False`` → a dropped/empty action is recovered by the
    loop via ``infer_action`` on the *preserved* action_input (wire-key
    prefix → tool). ``True`` → straight to NO_ACTION recovery.
  - ``thought_required=False`` → a missing thought is tolerated. ``True`` →
    NO_THOUGHT recovery.

The parser-side invariant (``WireFormat.parse`` contract) is that
action_input is preserved even when the action slot is empty/invalid, so
both flag branches have something to work with. This file pins:

  1. Both shipped parsers (json_fc / xml_fc) preserve action_input across
     dropped-action shapes (v7.0.0 — react 제거로 쌍이 json_fc/xml_fc 로).
  2. Cross-wire parity: same semantic emission → same recovery outcome.
  3. The loop honors each flag: False → infer/tolerate, True → recover.
     The shipped plugins both set False, so the True branches are pinned
     against a synthetic strict plugin. NOTE: ``format_no_thought_retry``
     는 react 전용 메서드였으므로 (thought_required=True 포맷만 필요)
     synthetic 이 직접 정의한다.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from agent_cli.loop import run_loop
from agent_cli.providers.base import LLMResponse
from agent_cli.providers.capabilities import ModelCapabilities
from agent_cli.recovery.intervention import Intervention
from agent_cli.tools.registry import infer_action
from agent_cli.wire_formats import get
from agent_cli.wire_formats.json_fc import JsonFcFormat


# ── Fixtures / helpers ───────────────────────────────


@pytest.fixture
def caps():
    return ModelCapabilities(
        context_window=32768,
        max_output_tokens=4096,
        supports_thinking=False,
        thinking_budget=0,
    )


def _make_provider(*responses):
    provider = MagicMock()
    provider.call.side_effect = [LLMResponse(content=r) for r in responses]
    return provider


def _complete(result: str) -> str:
    return f'done\n\n[{{"action": "complete", "result": "{result}"}}]'


class _StrictJson(JsonFcFormat):
    """Synthetic plugin pinning the True branches of both flags. The
    shipped plugins are all False, so without this the recovery paths for
    a *required* field would be untested. parse 는 상속 — loop 의
    플래그-게이트 분기만 다르다."""

    thought_required = True
    action_required = True

    def format_no_thought_retry(self, *, prior_content: str) -> Intervention:
        # react 전용이던 메서드 — thought_required=True 포맷만 필요해
        # ABC 에 없다. strict 게이트 검증용 최소 구현.
        return Intervention(
            message="Add reasoning prose before the array, then re-emit.",
            primitives=["no_thought_retry"],
        )


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
            assert plugin.thought_required is False, name
            assert plugin.action_required is False, name


# ── 3. Loop honors the flags ─────────────────────────
# 복구 여부는 도구의 파일 부수효과로 측정 (메시지 텍스트 스캔 금지 —
# NO_ACTION/NO_THOUGHT 개입이 raw 를 echo 하므로 텍스트는 오탐).


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
            query="go",
            provider=provider,
            capabilities=caps,
            model="m",
            wire_format=_StrictJson(),
        )
        assert result.success
        assert provider.call.call_count == 2  # NO_ACTION retry happened
        assert not target.exists()


class TestThoughtRequiredGate:
    def test_false_tolerates_missing_thought(self, caps, tmp_path):
        # 산문 없이 배열만 — thought_required=False (json_fc) → 그대로 실행.
        target = tmp_path / "made.txt"
        provider = _make_provider(
            f'[{{"action": "write_file", "path": "{target}", "content": "data"}}]',
            _complete("done"),
        )
        result = run_loop(
            query="go",
            provider=provider,
            capabilities=caps,
            model="m",
            wire_format=JsonFcFormat(),
        )
        assert result.success
        assert target.exists()  # ran despite missing thought

    def test_true_fires_no_thought_recovery(self, caps, tmp_path):
        target = tmp_path / "made.txt"
        provider = _make_provider(
            f'[{{"action": "write_file", "path": "{target}", "content": "data"}}]',
            _complete("done"),
        )
        result = run_loop(
            query="go",
            provider=provider,
            capabilities=caps,
            model="m",
            wire_format=_StrictJson(),
        )
        assert result.success
        assert provider.call.call_count == 2  # NO_THOUGHT retry happened
        assert not target.exists()  # recovery before write


# ── 4. Prompt flag hook (output unchanged, gate wired) ──


class TestPromptFlagHook:
    """``_gated_rule`` lets the flags weaken/drop a Format-Rules clause
    later. Today no plugin supplies a ``soft`` variant, so the prompt is
    unchanged — the hook is wired but inert."""

    def test_gated_rule_selects_by_flag(self):
        from agent_cli.wire_formats.base import WireFormat

        assert WireFormat._gated_rule(True, "S", "soft") == "S"
        assert WireFormat._gated_rule(False, "S", "soft") == "soft"
        assert WireFormat._gated_rule(False, "S") == "S"
        assert WireFormat._gated_rule(True, "S") == "S"

    def test_prompts_keep_strong_wording(self):
        # 플래그는 False 지만 soft 미공급 — 강한 의무 문구가 유지된다.
        fr = get("json_fc").format_rules()
        assert 'must have an "action"' in fr

    def test_field_specific_composes_numbered_rules(self):
        for name in ("json_fc", "xml_fc"):
            fs = get(name).format_rules_field_specific()
            assert fs.startswith("1. "), name
            assert "\n2. " in fs, name

    def test_softening_takes_effect_via_synthetic_plugin(self):
        class _SoftThought(JsonFcFormat):
            def format_rules_field_specific(self) -> str:
                return f"1. {self._gated_rule(self.thought_required, 'STRONG', 'thought optional')}"

        out = _SoftThought().format_rules_field_specific()
        assert "thought optional" in out
        assert "STRONG" not in out
