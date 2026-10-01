"""새 방언(hermes_json·glm_argkey)의 디코딩 문법 수용 감사 (Phase 5 S4).

`tests/test_decoding_grammar_accept.py` 와 같은 철학 — 형식 자체가 렌더하는 올바른
턴은 전부 통과(캐노니컬 ⊆ 문법), 하니스가 구제 못 하는 모양(모르는 도구·종결 뒤
호출·빈 통신값)은 막는다. 실모델 미검증(PHASE5 D3) — 여기서 검증하는 것은 문법과
렌더·파서의 정합뿐이다. xgrammar 가 없으면 건너뛴다."""

from __future__ import annotations

import time

import pytest

xgr = pytest.importorskip("xgrammar")

from agent_cli.dialects import get
from agent_cli.tools.registry import (
    TOOL_SCHEMAS,
    allows_extra_keys,
    effective_tool_names,
    flat_param_schemas,
)

_STOP = 256
NEW = ["hermes_json", "glm_argkey"]


@pytest.fixture(scope="module")
def compiler():
    vocab = [bytes([i]).decode("latin-1") for i in range(256)] + ["</s>"]
    info = xgr.TokenizerInfo(
        vocab, vocab_type=xgr.VocabType.RAW, stop_token_ids=[_STOP]
    )
    return xgr.GrammarCompiler(info)


def _accepts(compiled, text: str) -> bool:
    m = xgr.GrammarMatcher(compiled)
    ok = m.accept_string(text.encode("utf-8").decode("latin-1"))
    return bool(ok and m.accept_token(_STOP))


def _tools(fmt):
    wf = get(fmt)
    return [
        (
            n,
            flat_param_schemas(n, TOOL_SCHEMAS[n].parameters),
            allows_extra_keys(TOOL_SCHEMAS[n].parameters),
        )
        for n in effective_tool_names(None, wf)
    ]


def _turn(fmt, thought, ops):
    return get(fmt).render_assistant_from_history(
        {"thought": thought, "ops": [{"action": a, "action_input": i} for a, i in ops]}
    )["content"]


@pytest.mark.parametrize("fmt", NEW)
class TestAccepts:
    def test_canonical_single_and_multi(self, compiler, fmt):
        compiled = compiler.compile_grammar(get(fmt).grammar(_tools(fmt)))
        assert _accepts(compiled, _turn(fmt, "t", [("shell", {"command": "ls"})]))
        assert _accepts(
            compiled,
            _turn(
                fmt, "t", [("read_file", {"path": "a"}), ("read_file", {"path": "b"})]
            ),
        )
        assert _accepts(compiled, _turn(fmt, None, [("complete", {"result": "done"})]))

    def test_thinking_open_then_call(self, compiler, fmt):
        compiled = compiler.compile_grammar(
            get(fmt).grammar(_tools(fmt), thinking_open=True)
        )
        turn = "let me think about <x> [a]\n</think>\n\n" + _turn(
            fmt, "t", [("shell", {"command": "ls"})]
        )
        assert _accepts(compiled, turn)
        # 사고 구간 안의 호출 여는 태그는 막힌다 (v9.24.8)
        assert not _accepts(
            compiled,
            "<tool_call>\n</think>\n\n"
            + _turn(fmt, "t", [("shell", {"command": "ls"})]),
        )

    def test_prose_only_and_opener_character_in_prose(self, compiler, fmt):
        compiled = compiler.compile_grammar(get(fmt).grammar(_tools(fmt)))
        assert _accepts(compiled, "just thinking, no call this turn.")
        thought = "lookbehind (?<![A-Za-z]) and b3 << 24 | a[0] < b[1], list[str]"
        assert _accepts(compiled, _turn(fmt, thought, [("complete", {"result": "r"})]))

    def test_raw_multiline_value(self, compiler, fmt):
        compiled = compiler.compile_grammar(get(fmt).grammar(_tools(fmt)))
        turn = _turn(
            fmt,
            "t",
            [("write_file", {"path": "a.py", "content": "def f():\n    return 1\n"})],
        )
        assert _accepts(compiled, turn)
        t = get(fmt).parse_turn(turn)
        assert t.ops[0].action_input["content"] == "def f():\n    return 1\n"


@pytest.mark.parametrize("fmt", NEW)
class TestForbids:
    def test_unknown_tool_and_key(self, compiler, fmt):
        compiled = compiler.compile_grammar(get(fmt).grammar(_tools(fmt)))
        assert not _accepts(compiled, _turn(fmt, "t", [("no_such_tool", {"x": 1})]))
        assert not _accepts(compiled, _turn(fmt, "t", [("shell", {"cmd": "ls"})]))

    def test_nothing_after_a_terminal_call(self, compiler, fmt):
        compiled = compiler.compile_grammar(get(fmt).grammar(_tools(fmt)))
        ok = _turn(
            fmt, "t", [("shell", {"command": "ls"}), ("complete", {"result": "r"})]
        )
        bad = _turn(
            fmt, "t", [("complete", {"result": "r"}), ("shell", {"command": "ls"})]
        )
        assert _accepts(compiled, ok) and not _accepts(compiled, bad)

    def test_other_tagged_dialects_are_rejected(self, compiler, fmt):
        compiled = compiler.compile_grammar(get(fmt).grammar(_tools(fmt)))
        for other in ("xml_fc", *NEW):
            if other == fmt:
                continue
            assert not _accepts(
                compiled, _turn(other, "t", [("shell", {"command": "ls"})])
            ), other

    def test_json_fc_shape_is_just_prose_here(self, compiler, fmt):
        """bare 배열은 이 문법에서 산문이다 — 산문-only 턴은 표현 가능해야 하므로
        막지 않는다(막으면 EOS 가 마스킹돼 폭주). 파서도 호출로 읽지 않는다."""
        compiled = compiler.compile_grammar(get(fmt).grammar(_tools(fmt)))
        leak = _turn("json_fc", "t", [("shell", {"command": "ls"})])
        assert _accepts(compiled, leak)
        assert get(fmt).parse_turn(leak).ops == []

    def test_blank_communication_values_are_blocked(self, compiler, fmt):
        compiled = compiler.compile_grammar(get(fmt).grammar(_tools(fmt)))
        assert not _accepts(
            compiled, _turn(fmt, "t", [("message", {"to": "main", "text": "  "})])
        )
        assert _accepts(
            compiled, _turn(fmt, "t", [("message", {"to": "main", "text": "hi"})])
        )


@pytest.mark.parametrize("fmt", NEW)
def test_per_token_mask_cost_is_cheap(fmt):
    import random
    import string

    rnd = random.Random(7)
    alphabet = string.ascii_letters + string.digits + " \n<>/=()[]{}|&!?.,:;\"'_-*"
    syn: set[str] = set()
    while len(syn) < 20000:
        syn.add("".join(rnd.choice(alphabet) for _ in range(rnd.randint(1, 7))))
    vocab = [bytes([i]).decode("latin-1") for i in range(256)] + sorted(syn) + ["</s>"]
    info = xgr.TokenizerInfo(
        vocab, vocab_type=xgr.VocabType.RAW, stop_token_ids=[len(vocab) - 1]
    )
    comp = xgr.GrammarCompiler(info)
    compiled = comp.compile_grammar(get(fmt).grammar(_tools(fmt)))
    text = _turn(
        fmt, "thinking " * 40, [("write_file", {"path": "a", "content": "x " * 300})]
    )
    m = xgr.GrammarMatcher(compiled)
    bm = xgr.allocate_token_bitmask(1, len(vocab))
    toks = text.encode("utf-8")
    t = time.perf_counter()
    for b in toks:
        m.fill_next_token_bitmask(bm)
        assert m.accept_token(b)
    per_ms = (time.perf_counter() - t) / len(toks) * 1000
    assert per_ms < 5, f"{fmt}: {per_ms:.2f} ms/token"
