"""디코딩 문법 **수용 감사** — 문법이 형식의 캐노니컬 출력을 막지 않는다.

문법은 파서보다 좁아야 한다(수리 경로는 막는 게 목적) — 하지만 **형식 자체가
렌더하는 올바른 턴**은 전부 통과해야 한다. 그 방향(캐노니컬 ⊆ 문법)은 구조를
보는 단위 테스트로는 못 잡는다: 초판 xml_fc 문법은 블록 스타일
(`<parameter=k>\\nx\\n</parameter>`)만 허용해 프롬프트 예시가 쓰는 인라인
스타일(`<parameter=k>x</parameter>`)을 막았고, 이 감사가 잡았다.

엔진은 서버(omlx)가 쓰는 xgrammar 그대로 — 바이트 단위 가짜 토크나이저로
문자열 수용 여부와 종료 가능 여부를 묻는다. xgrammar 가 없으면 건너뛴다
(선택 dev 의존성).
"""

from __future__ import annotations

import json
from typing import ClassVar

import pytest

xgr = pytest.importorskip("xgrammar")

from agent_cli.tools.registry import (
    TOOL_SCHEMAS,
    allows_extra_keys,
    effective_tool_names,
    flat_param_schemas,
)
from agent_cli.tools.virtual import AskTool
from agent_cli.wire_formats import get

_STOP = 256


@pytest.fixture(scope="module")
def compiler():
    vocab = [bytes([i]).decode("latin-1") for i in range(256)] + ["</s>"]
    info = xgr.TokenizerInfo(
        vocab, vocab_type=xgr.VocabType.RAW, stop_token_ids=[_STOP]
    )
    return xgr.GrammarCompiler(info)


@pytest.fixture(scope="module")
def big_vocab():
    """합성 60K 어휘 — 앞 256 은 바이트(id=값), 뒤는 1~7자 ASCII 조각. 마스크
    비용은 어휘 크기 × 문법 상태 수라 바이트 어휘로는 함정이 안 보인다."""
    import random
    import string

    rnd = random.Random(7)
    alphabet = string.ascii_letters + string.digits + " \n<>/=()[]{}|&!?.,:;\"'_-*"
    syn: set[str] = set()
    while len(syn) < 60000:
        syn.add("".join(rnd.choice(alphabet) for _ in range(rnd.randint(1, 7))))
    vocab = [bytes([i]).decode("latin-1") for i in range(256)] + sorted(syn) + ["</s>"]
    info = xgr.TokenizerInfo(
        vocab, vocab_type=xgr.VocabType.RAW, stop_token_ids=[len(vocab) - 1]
    )
    return info, len(vocab)


def _accepts(compiled, text: str) -> bool:
    """문자열 전체를 받고 **끝낼 수 있는가** (EOS 수용)."""
    m = xgr.GrammarMatcher(compiled)
    ok = m.accept_string(text.encode("utf-8").decode("latin-1"))
    return bool(ok and m.accept_token(_STOP))


_PLAIN = {
    "string": "x",
    "integer": 1,
    "number": 1,
    "boolean": True,
    "array": [],
    "object": {},
}
_RICH = {
    "string": 'line one\nline "two" \\ back • 유니코드 <div>hi</div> ```py```',
    "integer": 42,
    "number": 3.5,
    "boolean": False,
    "array": ["a", 1, {"k": None}],
    "object": {"nested": [1, 2], "s": "v"},
}


def _sample(prop: dict, rich: bool):
    t = prop.get("type")
    t = t[0] if isinstance(t, list) else t
    if "enum" in prop:
        return prop["enum"][-1 if rich else 0]
    return (_RICH if rich else _PLAIN).get(t or "", "x")


def _tools(fmt: str, resident: bool):
    wf = get(fmt)
    out = []
    for n in effective_tool_names(None, wf):
        params = (
            AskTool.RESIDENT_PARAMETERS
            if (resident and n == "ask")
            else TOOL_SCHEMAS[n].parameters
        )
        out.append((n, flat_param_schemas(n, params), allows_extra_keys(params)))
    return out


@pytest.mark.parametrize("fmt", ["json_fc", "xml_fc"])
@pytest.mark.parametrize("resident", [False, True], ids=["main", "resident"])
@pytest.mark.parametrize("thinking_open", [False, True], ids=["think-off", "think-on"])
def test_every_tools_canonical_turn_is_accepted(compiler, fmt, resident, thinking_open):
    wf = get(fmt)
    tools = _tools(fmt, resident)
    compiled = compiler.compile_grammar(wf.grammar(tools, thinking_open=thinking_open))
    pre = "let me think about <x> and </y> [a]\n</think>\n\n" if thinking_open else ""
    blocked = []
    for name, flat, _extra in tools:
        for rich in (False, True):
            required = {k: _sample(p, rich) for k, (p, r) in flat.items() if r}
            everything = {k: _sample(p, rich) for k, (p, _r) in flat.items()}
            for inp in (required or dict(list(everything.items())[:1]), everything):
                turn = pre + wf.render_full_example(
                    thought="I will do it.",
                    action=name,
                    action_input=wf.render_action_input(inp),
                )
                if not _accepts(compiled, turn):
                    blocked.append((name, rich, turn[:160]))
                else:
                    # 받아들인 턴은 파서도 캐노니컬(stage 1)로 읽는다 — 왕복
                    parsed = wf.parse_turn(turn)
                    assert parsed.parse_stage == 1 and parsed.ops, (name, turn[:160])
    assert not blocked, "\n".join(map(str, blocked))


@pytest.mark.parametrize("fmt", ["json_fc", "xml_fc"])
def test_two_ops_in_one_turn(compiler, fmt):
    wf = get(fmt)
    compiled = compiler.compile_grammar(wf.grammar(_tools(fmt, False)))
    a = wf.render_action_input({"command": "ls"})
    b = wf.render_action_input({"path": "f.txt"})
    if fmt == "json_fc":
        turn = (
            "ok\n\n["
            + json.dumps({"action": "shell", **json.loads(a)})
            + ", "
            + json.dumps({"action": "read_file", **json.loads(b)})
            + "]"
        )
    else:
        turn = (
            f"ok\n\n<tool_call>\n<function=shell>\n{a}\n</function>\n</tool_call>\n"
            f"<tool_call>\n<function=read_file>\n{b}\n</function>\n</tool_call>"
        )
    assert _accepts(compiled, turn)
    assert len(wf.parse_turn(turn).ops) == 2


class TestWhatTheGrammarForbids:
    """막는 것이 곧 존재 이유 — 형식 실패 부류가 생성 단계에서 사라진다."""

    def test_unknown_tool_and_unknown_key(self, compiler):
        wf = get("json_fc")
        compiled = compiler.compile_grammar(wf.grammar(_tools("json_fc", False)))
        assert not _accepts(compiled, 'x\n\n[{"action": "no_such_tool", "arg": 1}]')
        assert not _accepts(compiled, 'x\n\n[{"action": "shell", "cmd": "ls"}]')

    @pytest.mark.parametrize("fmt", ["json_fc", "xml_fc"])
    def test_prose_only_turn_stays_expressible(self, compiler, fmt):
        """v3 A/B: 호출을 필수로 하면 산문을 끝내고 턴을 마치려는 순간 EOS 가
        마스킹되어 32K 상한까지 산문이 이어진다(18〜24K 토큰 진행 중 타임아웃).
        문법은 호출의 *모양*만 강제하고, 호출 없는 턴은 복구 계층이 맡는다."""
        wf = get(fmt)
        compiled = compiler.compile_grammar(wf.grammar(_tools(fmt, False)))
        assert _accepts(compiled, "I think I am done.")
        assert _accepts(compiled, "Done.\n\nReally done, with a [bracket] and <tag>.")

    def test_long_prose_is_fine(self, compiler):
        wf = get("json_fc")
        compiled = compiler.compile_grammar(wf.grammar(_tools("json_fc", False)))
        assert _accepts(
            compiled, "a" * 20000 + '\n\n[{"action": "complete", "result": "r"}]'
        )

    @pytest.mark.parametrize("fmt", ["json_fc", "xml_fc"])
    def test_per_token_mask_cost_is_cheap(self, big_vocab, fmt):
        """v2 A/B 함정: 그룹 반복 `( … ){0,4000}` 은 실제 토크나이저(248K)에서
        토큰당 2.2초(문자 클래스 상한 0.08ms, 무제한 그룹 0.00ms) — 15분에 LLM
        호출 0건. 바이트 토크나이저(257)로는 0.01ms 라 안 보이고, 합성 60K
        어휘로는 350ms 로 드러난다. 그 어휘로 토큰당 마스크 비용을 잰다."""
        import time

        import xgrammar as xgr

        info, vocab_size = big_vocab
        wf = get(fmt)
        compiled = xgr.GrammarCompiler(info).compile_grammar(
            wf.grammar(_tools(fmt, False))
        )
        # 산문 → 호출 구조 → 본문(이스케이프·유니코드 포함) → 닫기까지 한 바퀴.
        # 두 번째 함정(`j_hex` 하위 규칙을 star 안에서 참조 → 문자열 구간이
        # 실제 토크나이저에서 65ms/토큰, 합성 어휘에서 11ms)도 이 구간이 잡는다.
        body = json.dumps('caf\u00e9 "q" \\ tab\t nl\n ' * 6)[1:-1]
        text = "think (?<![A-Za-z]) and b3 << 24 | a[0] " * 3 + "\n\n"
        text += (
            '[{"action": "write_file", "path": "/a", "content": "' + body + '"}]'
            if fmt == "json_fc"
            else "<tool_call>\n<function=write_file>\n<parameter=path>/a</parameter>\n"
            "<parameter=content>\n" + body + "\n</parameter>\n</function>\n</tool_call>"
        )
        m = xgr.GrammarMatcher(compiled)
        bm = xgr.allocate_token_bitmask(1, vocab_size)
        toks = text.encode("utf-8")  # 바이트 토큰 id = 바이트 값 (어휘 앞 256)
        t = time.perf_counter()
        for b in toks:
            m.fill_next_token_bitmask(bm)
            assert m.accept_token(b)
        per_ms = (time.perf_counter() - t) / len(toks) * 1000
        assert per_ms < 5, f"{fmt}: {per_ms:.2f} ms/token"

    @pytest.mark.parametrize("fmt", ["json_fc", "xml_fc"])
    def test_prose_may_hold_the_opener_character(self, compiler, fmt):
        """A/B 2026-09-25: 산문에서 `<`/`[` 를 통째로 막았더니 `(?<!`·`<<`·`a[0]`
        가 마스킹돼 생각이 뜻을 바꾸거나 폭주했다. 금지는 여는 시퀀스만."""
        wf = get(fmt)
        compiled = compiler.compile_grammar(wf.grammar(_tools(fmt, False)))
        thought = "lookbehind (?<![A-Za-z]) and b3 << 24 | a[0] < b[1], list[str]"
        call = (
            '[{"action": "complete", "result": "r"}]'
            if fmt == "json_fc"
            else "<tool_call>\n<function=complete>\n<parameter=result>r</parameter>\n"
            "</function>\n</tool_call>"
        )
        assert _accepts(compiled, thought + "\n\n" + call)
        # 산문 한가운데의 여는 시퀀스는 산문일 수 없다 — 거기서 호출이 시작된다.
        # 빈 줄이 몇 개든(종전 접두 사슬은 `\n\n<tool_call>` 을 산문으로 흘렸다).
        opener = "[" if fmt == "json_fc" else "<tool_call>"
        for blank in ("\n\n", "\n\n\n", "\n\n\n\n"):
            assert not _accepts(compiled, "a" + blank + opener + "junk\n\n" + call), (
                blank
            )
        # 턴이 개행으로 시작해도 산문이다 (개행 토큰을 막지 않는다)
        assert _accepts(compiled, "\n\n" + thought + "\n\n" + call)
        assert _accepts(compiled, "\n")

    def test_raw_newline_inside_a_json_string_is_allowed(self, compiler):
        """하니스가 구제하는 것은 막지 않는다: 파서의 마지막 단계가 strict=False
        로 제어 문자를 읽으므로, 파일 본문의 raw 개행을 줄마다 마스킹해 `\\n`
        으로 유도할 이유가 없다. 이스케이프 문법은 여전히 엄격."""
        wf = get("json_fc")
        compiled = compiler.compile_grammar(wf.grammar(_tools("json_fc", False)))
        assert _accepts(compiled, 'x\n\n[{"action": "complete", "result": "a\nb\tc"}]')
        assert _accepts(compiled, 'x\n\n[{"action": "complete", "result": "a\\nb"}]')
        assert not _accepts(
            compiled, 'x\n\n[{"action": "complete", "result": "a\\qb"}]'
        )

    @pytest.mark.parametrize("fmt", ["json_fc", "xml_fc"])
    def test_direct_start_and_empty_array_and_loose_whitespace(self, compiler, fmt):
        """구제 가능한 모양은 허용: 산문 없이 바로 호출, 빈 배열 `[]`(NO_ACTION
        넛지가 구제), 태그 사이 빈 줄·공백(파서는 `\\s*`), 호출 뒤 공백."""
        wf = get(fmt)
        compiled = compiler.compile_grammar(wf.grammar(_tools(fmt, False)))
        if fmt == "json_fc":
            assert _accepts(compiled, '[{"action": "complete", "result": "r"}]')
            assert _accepts(compiled, "thought\n\n[]")
            assert _accepts(compiled, "[ ]")
        else:
            call = (
                "<tool_call>\n\n<function=shell>\n\n<parameter=command>ls</parameter>\n\n"
                "</function>\n\n</tool_call>"
            )
            assert _accepts(compiled, call)  # 바로 시작 + 태그 사이 빈 줄
            assert _accepts(compiled, "thought\n" + call + "\n\n" + call + "\n\n")
            assert _accepts(compiled, "thought\n\n\n" + call)

    def test_xml_line_start_opener_is_always_a_call(self, compiler):
        """누수 봉합: 개행 하나 뒤의 `<tool_call>` 도 호출이다 — 종전엔 `\\n\\n`
        만 오프너라 `\\n<tool_call>` 뒤가 산문으로 흡수돼 그 턴의 제약이 조용히
        꺼졌다. 이제 그 자리의 모르는 도구는 문법이 막는다."""
        wf = get("xml_fc")
        compiled = compiler.compile_grammar(wf.grammar(_tools("xml_fc", False)))
        bad = "t\n<tool_call>\n<function=no_such>\n</function>\n</tool_call>"
        assert not _accepts(compiled, bad)
        assert not _accepts(compiled, "t\n\n" + bad[2:])
        # 줄 중간의 `<tool_call>` 언급은 산문 (문법 이야기를 할 수 있어야 한다)
        assert _accepts(compiled, "the tag <tool_call> opens a call. done.")

    def test_xml_body_may_hold_anything_but_its_closer(self, compiler):
        wf = get("xml_fc")
        compiled = compiler.compile_grammar(wf.grammar(_tools("xml_fc", False)))
        ok = (
            "x\n\n<tool_call>\n<function=write_file>\n<parameter=path>a.html</parameter>\n"
            "<parameter=content>\n<div>hi</div>\n```py\nprint(1)\n```\n</parameter>\n"
            "</function>\n</tool_call>"
        )
        assert _accepts(compiled, ok)
        bad = ok.replace("print(1)", "print(1)</parameter>")
        assert not _accepts(compiled, bad)

    def test_thinking_open_requires_the_close_tag_first(self, compiler):
        wf = get("json_fc")
        compiled = compiler.compile_grammar(
            wf.grammar(_tools("json_fc", False), thinking_open=True)
        )
        assert not _accepts(compiled, 'x\n\n[{"action": "complete", "result": "r"}]')
        assert _accepts(
            compiled, 'hmm\n</think>\n\nx\n\n[{"action": "complete", "result": "r"}]'
        )


class TestFreeFormSchemas:
    """MCP 도구처럼 키를 열거하지 않는 스키마 — 문법이 모든 키를 막으면 그 도구는
    영영 못 부른다. `allows_extra_keys` 가 True 면 임의 키를 허용한다."""

    FREE: ClassVar[dict] = {
        "type": "object",
        "properties": {},
        "additionalProperties": True,
    }
    LOOSE: ClassVar[dict] = {
        "type": "object",
        "properties": {"q": {"type": "string"}},
    }  # additional 미선언
    STRICT: ClassVar[dict] = {
        "type": "object",
        "properties": {"q": {"type": "string"}},
        "additionalProperties": False,
    }

    def test_allows_extra_keys_rule(self):
        assert allows_extra_keys(self.FREE) is True
        assert allows_extra_keys({"type": "object"}) is True  # properties 없음
        assert allows_extra_keys(self.LOOSE) is False  # 열거된 키만 (프롬프트와 동일)
        assert allows_extra_keys(self.STRICT) is False

    @pytest.mark.parametrize("fmt", ["json_fc", "xml_fc"])
    def test_free_form_tool_accepts_any_key(self, compiler, fmt):
        wf = get(fmt)
        tools = [
            ("srv.search", flat_param_schemas("srv.search", self.FREE), True),
            (
                "complete",
                flat_param_schemas("complete", TOOL_SCHEMAS["complete"].parameters),
                False,
            ),
        ]
        compiled = compiler.compile_grammar(wf.grammar(tools))
        if fmt == "json_fc":
            turn = 'x\n\n[{"action": "srv.search", "query": "cats", "limit": 3, "opts": {"a": [1]}}]'
        else:
            turn = (
                "x\n\n<tool_call>\n<function=srv.search>\n<parameter=query>cats</parameter>\n"
                "<parameter=limit>3</parameter>\n</function>\n</tool_call>"
            )
        assert _accepts(compiled, turn)
