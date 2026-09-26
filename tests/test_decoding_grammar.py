"""디코딩 문법 제약 (v9.24.0) — 서버가 wire shape 를 강제한다.

텍스트 프로토콜은 형식을 부탁하고(시스템 프롬프트) 나온 뒤에 고친다(복구
계층). 문법을 받는 서버(omlx/xgrammar·vLLM·llama.cpp)에선 형식 밖 토큰의
확률이 0 이라 잘못된 발화가 아예 없다 — Harbor extract-elf 의 32K자 폭주
(열린 `<tool_call>` 안의 추론)가 구조적으로 불가능해진다. 라이브 검증
(2026-09-25, Qwen3.8-Flash-Next on omlx): json_fc·xml_fc × thinking on/off
네 조합 모두 stage-1 파싱, 본문 안 코드 펜스·`<div>` 통과.

여기서 고정하는 계약:
1. 문법은 **프롬프트가 가르친 도구 집합과 키**에서 나온다 — 같은 함수.
2. `<think>` 가 열린 채 시작하는 콜은 닫는 태그를 먼저 허용한다(없으면 출력
   전체가 사고 채널에 갇힌다 — 실측).
3. 서버가 강제할 때만(capabilities.supports_grammar) 싣고, 세션/env 로 끌 수
   있다. 제약 중엔 사고 스위치를 항상 명시한다(문법의 가정과 서버 기본값이
   어긋나지 않게).
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from agent_cli.loop import run_loop
from agent_cli.providers.base import CallSettings, LLMResponse
from agent_cli.providers.capabilities import ModelCapabilities
from agent_cli.tools.registry import (
    TOOL_SCHEMAS,
    effective_tool_names,
    flat_param_schemas,
)
from agent_cli.wire_formats import get
from agent_cli.wire_formats.grammar import (
    JSON_RULES,
    json_value_rule,
    not_containing,
    prose_rule,
    think_prefix,
)
from tests.loop_ports import TEST_PORTS


def _tools(names, fmt="json_fc"):
    ordered = effective_tool_names(names, get(fmt))
    return [
        (n, flat_param_schemas(n, TOOL_SCHEMAS[n].parameters), False) for n in ordered
    ]


# ── 1. 형식-무관 조각 ─────────────────────────────


class TestGrammarPieces:
    def test_not_containing_builds_the_prefix_chain(self):
        rule = not_containing("body", "</p>")
        # 리터럴의 첫 글자로 시작할 수 없는 글자, 또는 진짜 접두 + 깨는 글자
        assert rule.startswith("body ::= ([^<] | ")
        assert '"<" [^/]' in rule and '"</" [^p]' in rule and '"</p" [^>]' in rule
        assert rule.endswith(")*")

    def test_think_prefix_only_when_open(self):
        assert think_prefix(False) == ("", "")
        pre, rules = think_prefix(True)
        assert pre == 'think "</think>\\n\\n" '
        assert rules.startswith("think ::= ") and '"</think" [^>]' in rules

    def test_prose_is_line_based_and_unbounded(self):
        """줄 단위: xml 은 어떤 줄도 `<tool_call>` 로 시작 못 함, json 은 빈 줄 뒤
        (또는 첫 줄)만 `[` 로 시작 못 함. 생각 속 `[`/`<` 는 그 외 어디서나
        자유(문자 금지는 추론을 분포 밖으로 밀어냈다). 길이 상한은 없다 —
        그룹 반복 `{0,n}` 은 xgrammar 에서 토큰당 초 단위 비용."""
        x = prose_rule("prose", "<tool_call>")
        assert x.splitlines()[0] == 'prose ::= prose_l ( "\\n" prose_l )*'
        assert x.splitlines()[1].startswith(
            'prose_l ::= ( [^\\n<] [^\\n]* | "<" ( [^\\nt] [^\\n]* )? | '
        )
        assert x.splitlines()[1].endswith('"<tool_call" ( [^\\n>] [^\\n]* )? )?')
        j = prose_rule("prose", "[", after_blank_line=True)
        assert j.splitlines()[0].startswith(
            'prose ::= ( "\\n" )* ( prose_nb ( "\\n" prose_any )* '
        )
        assert "prose_any ::= [^\\n]+" in j and j.endswith(
            "prose_nb ::= [^\\n\\[] [^\\n]*"
        )
        assert "{0," not in x + j

    def test_json_value_rule_follows_declared_type(self):
        assert json_value_rule({"type": "string"}) == "j_string"
        assert json_value_rule({"type": "integer"}) == "j_number"
        assert json_value_rule({"type": "boolean"}) == '( "true" | "false" )'
        assert json_value_rule({"type": "array"}) == "j_array"
        assert json_value_rule({"type": ["string", "null"]}) == "j_value"
        assert json_value_rule({}) == "j_value"

    def test_json_rules_have_no_wildcard(self):
        # xgrammar EBNF 에 `.` 은 없다 — 실측에서 컴파일 오류였다.
        for line in JSON_RULES.splitlines():
            body = line.split("::=", 1)[1]
            assert " . " not in body and not body.rstrip().endswith(" .")


# ── 2. 두 형식의 문법 생성 ────────────────────────


class TestJsonFcGrammar:
    def test_root_prose_then_ops_and_every_tool_rule(self):
        tools = _tools(["shell", "write_file"])
        g = get("json_fc").grammar(tools)
        lines = g.splitlines()
        assert lines[0] == 'root ::= ( ops | prose "\\n\\n" ops | prose )'
        assert prose_rule("prose", "[", after_blank_line=True) in g
        assert 'ops ::= "[" j_ws ( op ( j_ws "," j_ws op )* )? j_ws "]"' in g
        assert "op ::= t_shell | t_write_file | t_complete" in g  # complete 는 항상
        # 도구 이름과 키가 열거된다 — 모르는 도구/키는 낼 수 없다
        assert 't_shell ::= "{" j_ws "\\"action\\"" j_ws ":" j_ws "\\"shell\\""' in g
        assert '"\\"command\\"" j_ws ":" j_ws j_string' in g
        assert '"\\"content\\"" j_ws ":" j_ws j_string' in g
        assert '"\\"answers\\"" j_ws ":" j_ws j_array' in g  # complete 의 answers
        assert g.endswith(JSON_RULES)

    def test_literal_escapes_are_two_characters(self):
        # 파이썬 개행이 아니라 EBNF 이스케이프 — 원시 개행이 "…" 안에 들어가면
        # 문법이 깨진다 (패치 중 실제로 났던 실수).
        import re

        g = get("json_fc").grammar(_tools(["shell"]))
        assert '"\\n\\n"' in g
        # 줄마다 규칙 하나 — 리터럴 안에 원시 개행이 들어갔다면 이름 없는
        # 이어짐 줄이 생긴다.
        for line in g.splitlines():
            assert re.match(r"^\w+ ::= ", line), repr(line)

    def test_thinking_open_adds_the_close_tag_first(self):
        g = get("json_fc").grammar(_tools(["shell"]), thinking_open=True)
        assert (
            g.splitlines()[0]
            == 'root ::= think "</think>\\n\\n" ( ops | prose "\\n\\n" ops | prose )'
        )
        assert any(line.startswith("think ::= ") for line in g.splitlines())

    def test_tool_set_matches_the_prompt(self):
        # 프롬프트와 같은 함수(effective_tool_names)에서 나온다 — complete 추가,
        # 조건부 도구는 뒤로.
        tools = _tools(["edit_file", "shell"])
        assert [n for n, *_ in tools] == ["shell", "complete", "edit_file"]


class TestXmlFcGrammar:
    def test_root_calls_and_bodies(self):
        g = get("xml_fc").grammar(_tools(["shell", "write_file"], "xml_fc"))
        lines = g.splitlines()
        # 바로 호출 | 산문 + 개행 + 호출 | 산문만; 호출 사이·끝은 공백 관용(ws)
        assert (
            lines[0] == 'root ::= ( call | prose "\\n" call | prose ) ( ws call )* ws'
        )
        assert prose_rule("prose", "<tool_call>") in g  # 줄 첫 오프너만 제외
        assert "ws ::= [ \\t\\r\\n]*" in g
        assert 'call ::= "<tool_call>" ws fn ws "</tool_call>"' in g
        assert "fn ::= t_shell | t_write_file | t_complete" in g
        # 본문은 </parameter> 를 담을 수 없을 뿐 — `<`, `</`, 펜스 전부 허용
        assert 'body ::= ([^<] | "<" [^/] | "</" [^p]' in g
        # 인라인·블록 스타일 모두 — body 가 앞뒤 개행을 품는다 (수용 감사가 잡은 초판 오류)
        assert (
            't_write_file ::= "<function=write_file>" ws ( "<parameter=path>" body '
            '"</parameter>" ws | "<parameter=content>" body "</parameter>" ws )* '
            '"</function>"'
        ) in g

    def test_thinking_open_variant(self):
        g = get("xml_fc").grammar(_tools(["shell"], "xml_fc"), thinking_open=True)
        assert g.startswith('root ::= think "</think>\\n\\n" ( call | prose')


class TestBaseDefault:
    def test_formats_without_a_grammar_return_none(self):
        from agent_cli.wire_formats.base import WireFormat

        assert WireFormat.grammar(get("json_fc"), []) is None  # 기본 구현


# ── 3. provider: body 에 실리고, 제약 중엔 사고 스위치를 명시 ──


class TestProviderBody:
    @pytest.fixture
    def caps_thinking(self):
        return ModelCapabilities(
            context_window=32768, max_output_tokens=4096, supports_thinking=True
        )

    def _call(self, mock_post, caps, settings):
        from agent_cli.providers.openai import OpenAIProvider

        r = MagicMock()
        r.raise_for_status.return_value = None
        r.json.return_value = {
            "choices": [{"message": {"content": "[]"}, "finish_reason": "stop"}]
        }
        mock_post.return_value = r
        OpenAIProvider("http://s/v1", "k").call(
            messages=[{"role": "user", "content": "hi"}],
            system="s",
            model="m",
            capabilities=caps,
            settings=settings,
        )
        return mock_post.call_args.kwargs["json"]

    @patch("agent_cli.providers.openai.requests.post")
    def test_grammar_goes_into_guided_grammar(self, mock_post, caps_thinking):
        body = self._call(
            mock_post, caps_thinking, CallSettings(grammar='root ::= "x"')
        )
        assert body["guided_grammar"] == 'root ::= "x"'

    @patch("agent_cli.providers.openai.requests.post")
    def test_no_grammar_no_field(self, mock_post, caps_thinking):
        body = self._call(mock_post, caps_thinking, CallSettings())
        assert "guided_grammar" not in body
        assert "chat_template_kwargs" not in body  # 오버라이드 없음 → 종전대로

    @patch("agent_cli.providers.openai.requests.post")
    def test_constrained_call_pins_the_thinking_switch(self, mock_post, caps_thinking):
        """문법은 `<think>` 가 열려 있는지를 가정한다 — 서버 기본값에 맡기면
        가정과 실제가 어긋나 출력이 사고 채널에 갇힌다(실측). 제약 중엔
        오버라이드가 없어도 스위치를 명시한다."""
        body = self._call(
            mock_post, caps_thinking, CallSettings(grammar='root ::= "x"')
        )
        assert body["chat_template_kwargs"] == {"enable_thinking": True}
        body = self._call(
            mock_post,
            caps_thinking,
            CallSettings(grammar='root ::= "x"', thinking={"enable_thinking": False}),
        )
        assert body["chat_template_kwargs"] == {"enable_thinking": False}


# ── 4. capabilities: 플래그 · 엔트리 왕복 · 프로브 ────


class TestCapabilityFlag:
    def test_entry_round_trip_and_unknown(self):
        from agent_cli.providers.capabilities import _build_from_entry, caps_to_entry

        base = {
            "context_window": 32768,
            "max_output_tokens": 4096,
            "supports_thinking": False,
        }
        assert _build_from_entry(base).supports_grammar is None  # 키 없음 = 모름
        assert (
            _build_from_entry({**base, "supports_grammar": True}).supports_grammar
            is True
        )
        assert (
            _build_from_entry({**base, "supports_grammar": False}).supports_grammar
            is False
        )
        caps = ModelCapabilities(32768, 4096, False)
        assert "supports_grammar" not in caps_to_entry(caps)  # 모름은 적지 않는다
        caps = ModelCapabilities(32768, 4096, False, supports_grammar=True)
        assert caps_to_entry(caps)["supports_grammar"] is True

    @patch("agent_cli.providers.capabilities.requests.post")
    def test_probe_is_judged_by_the_answer_not_by_acceptance(self, mock_post):
        """모르는 필드를 조용히 버리는 서버가 있다(omlx 의 guided_regex) — 200 이
        아니라 출력이 문법에 묶였는지로 판정한다."""
        from agent_cli.providers.capabilities import _OpenAITransport

        t = _OpenAITransport("http://s/v1", "m")
        r = MagicMock()
        r.raise_for_status.return_value = None
        r.json.return_value = {"choices": [{"message": {"content": "GRAMMAR-OK"}}]}
        mock_post.return_value = r
        assert t.grammar_probe() is True
        body = mock_post.call_args.kwargs["json"]
        assert body["guided_grammar"] == 'root ::= "GRAMMAR-OK"'
        assert body["chat_template_kwargs"] == {"enable_thinking": False}
        r.json.return_value = {"choices": [{"message": {"content": "Hello!"}}]}
        assert t.grammar_probe() is False

    @patch("agent_cli.providers.capabilities.requests.post")
    def test_registered_model_without_the_key_is_not_probed_at_boot(self, mock_post):
        """키 없는 등록 엔트리는 미확인(None) 그대로 — 부트에서 모델에 요청을
        내지 않는다(서버가 바쁘면 그 한 요청에 열기가 수십 초 밀렸다). 판정은
        모델 감지 경로(`_detect_capabilities`)가 한 번 하고 저장한다."""
        from agent_cli.providers import capabilities as C

        entry = {
            "context_window": 32768,
            "max_output_tokens": 4096,
            "supports_thinking": False,
        }
        with patch.object(C, "get_model_entry", return_value=entry):
            caps = C.get_capabilities("m", provider="openai", base_url="http://s/v1")
        assert caps.supports_grammar is None
        assert mock_post.call_count == 0

    def test_anthropic_transport_has_no_grammar(self):
        from agent_cli.providers.capabilities import _AnthropicTransport

        assert _AnthropicTransport("http://s", "m").grammar_probe() is False


# ── 5. 세션/env 오버라이드 ────────────────────────


class TestOverride:
    def test_env_default(self, monkeypatch):
        from agent_cli.context.manager import default_grammar_override

        monkeypatch.delenv("AGENT_CLI_GRAMMAR", raising=False)
        assert default_grammar_override() is None
        monkeypatch.setenv("AGENT_CLI_GRAMMAR", "off")
        assert default_grammar_override() is False
        monkeypatch.setenv("AGENT_CLI_GRAMMAR", "on")
        assert default_grammar_override() is True

    def test_runtime_setter(self, tmp_path):
        from agent_cli.context.manager import ContextManager

        ctx = ContextManager(session_dir=tmp_path)
        assert ctx.grammar_override is None
        assert ctx.set_grammar_override(False) is False
        assert ctx.set_grammar_override(None) is None


# ── 6. 루프 배선 — 실제 run_loop 로 ────────────────


class TestLoopWiring:
    @staticmethod
    def _caps(grammar):
        return ModelCapabilities(
            context_window=32768,
            max_output_tokens=4096,
            supports_thinking=True,
            supports_grammar=grammar,
        )

    def _run(self, tmp_path, caps, *, wire="json_fc", env_off=False, snapshot=None):
        from agent_cli.context.manager import ContextManager

        provider = MagicMock()
        provider.call.side_effect = [
            LLMResponse(content=json.dumps([{"action": "complete", "result": "ok"}]))
        ]
        ctx = ContextManager(session_dir=tmp_path)
        if env_off:
            ctx.set_grammar_override(False)
        with patch(
            "agent_cli.loop.llm.render_system_prompt_snapshot",
            side_effect=lambda sections, turn, grammar=None: (
                snapshot.append((sections, grammar)) if snapshot is not None else None
            ),
        ):
            run_loop(
                ports=TEST_PORTS,
                query="q",
                provider=provider,
                capabilities=caps,
                model="m",
                ctx=ctx,
                wire_format=wire,
            )
        return provider.call.call_args.kwargs["settings"]

    def test_grammar_passed_when_the_server_enforces(self, tmp_path):
        settings = self._run(tmp_path, self._caps(True))
        g = settings.grammar
        assert g and g.startswith("root ::= think ")  # supports_thinking → 열린 think
        assert "t_complete" in g and "t_shell" in g

    def test_not_passed_when_unknown_or_unsupported(self, tmp_path):
        assert self._run(tmp_path, self._caps(None)).grammar is None
        assert self._run(tmp_path, self._caps(False)).grammar is None

    def test_session_off_wins(self, tmp_path):
        assert self._run(tmp_path, self._caps(True), env_off=True).grammar is None

    def test_explicit_on_never_covers_unknown_or_unsupported(self, tmp_path):
        """켜기는 지원으로 기록된 모델에서만 — 미확인(None)도 미지원(False)도
        on 으로 덮이지 않는다. 지원 여부는 감지 때 판정하는 것이지 켜기 요청이
        정하는 게 아니다."""
        from agent_cli.context.manager import ContextManager, grammar_active

        assert grammar_active(True, None) is False
        assert grammar_active(True, False) is False
        assert grammar_active(None, True) is True
        assert grammar_active(True, True) is True
        assert grammar_active(False, True) is False
        for flag in (None, False):
            provider = MagicMock()
            provider.call.side_effect = [
                LLMResponse(
                    content=json.dumps([{"action": "complete", "result": "ok"}])
                )
            ]
            ctx = ContextManager(session_dir=tmp_path)
            ctx.set_grammar_override(True)
            run_loop(
                ports=TEST_PORTS,
                query="q",
                provider=provider,
                capabilities=self._caps(flag),
                model="m",
                ctx=ctx,
            )
            assert provider.call.call_args.kwargs["settings"].grammar is None, flag

    def test_thinking_off_uses_the_closed_variant(self, tmp_path):
        from agent_cli.context.manager import ContextManager

        provider = MagicMock()
        provider.call.side_effect = [
            LLMResponse(content=json.dumps([{"action": "complete", "result": "ok"}]))
        ]
        ctx = ContextManager(session_dir=tmp_path)
        ctx.set_thinking_override(enable_thinking=False)
        run_loop(
            ports=TEST_PORTS,
            query="q",
            provider=provider,
            capabilities=self._caps(True),
            model="m",
            ctx=ctx,
        )
        g = provider.call.call_args.kwargs["settings"].grammar
        assert g.startswith('root ::= ( ops | prose "\\n\\n" ops | prose )')

    def test_xml_fc_binding_gets_the_xml_grammar(self, tmp_path):
        provider = MagicMock()
        provider.call.side_effect = [
            LLMResponse(
                content="done\n\n<tool_call>\n<function=complete>\n<parameter=result>\nok\n"
                "</parameter>\n</function>\n</tool_call>"
            )
        ]
        from agent_cli.context.manager import ContextManager

        run_loop(
            ports=TEST_PORTS,
            query="q",
            provider=provider,
            capabilities=self._caps(True),
            model="m",
            ctx=ContextManager(session_dir=tmp_path),
            wire_format="xml_fc",
        )
        g = provider.call.call_args.kwargs["settings"].grammar
        assert '"<tool_call>" ws fn' in g

    def test_inspector_snapshot_carries_the_grammar_beside_the_prompt(self, tmp_path):
        """문법은 프롬프트 섹션이 아니다 — 섹션 목록은 손대지 않고 별도 인자
        `(thinking_open, ebnf)` 로 넘긴다(인스펙터 토큰 합계에 안 섞임)."""
        seen: list = []
        self._run(tmp_path, self._caps(True), snapshot=seen)
        sections, grammar = seen[-1]
        assert all(not n.startswith("Decoding grammar") for n, _ in sections)
        assert grammar[0] is True  # supports_thinking → <think> 열린 채 시작
        assert grammar[1].startswith("root ::= think")
        seen.clear()
        self._run(tmp_path, self._caps(False), snapshot=seen)
        assert seen[-1][1] is None
