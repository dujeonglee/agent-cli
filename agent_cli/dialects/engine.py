"""Dialect — 스펙 하나로 렌더·파서·문법·산문·history 왕복을 만드는 엔진 (Phase 5).

설계: docs/dialects/PHASE5.md §4.3. 옛 ``xml_fc``/``json_fc`` 모듈의 동작을
스펙에서 읽은 토큰으로 일반화한 것이라, 각 경로의 주석은 그 모듈에서 가져왔다.
등가성 합격선(§7)은 옛 모듈의 고정 출력(``tests/equivalence/expected/``)과 코퍼스로 잰다.

파이프라인::

    parse_turn(text)
      0. strip_thinking (ABC 공용)
      1. 인용 영역 — 균형 ``` 쌍 + 인라인 `…` (recovery.quote_spans)
      2. 트리거/앵커 — 첫 **자격 있는** 호출 (산문 언급·펜스 예시는 후보 탈락)
      3. 호출 분할 — name-open 단위 세그먼트 (인용 후보도 세그먼트 경계는 끊는다)
      4. 이름 — NameSlot
      5. 인자 — ArgStyle: TAGGED(스펙 param 정규식) | JSON (recovery.json)
      6. 타입 복원 — value_mode=raw 면 스키마가 string 이 아닌 param 만 JSON parse
      7. stage — 1 캐노니컬 / 2 드리프트·구제·절단 / 0 ops 없음
"""

from __future__ import annotations

import json
import re

from agent_cli.dialects.base import (
    DialectBase,
    Op,
    ParsedTurn,
    _terminal_input,
)
from agent_cli.dialects.recovery import quote_spans
from agent_cli.dialects.recovery.json import (
    _extract_op_json,
    _op_anchor,
    close_unbalanced,
    describe_json_error,
)
from agent_cli.dialects.recovery.tagged import (
    _extract_params_lenient,
    _lenient_tool_open_re,
    _trim_block,
)
from agent_cli.dialects.spec import ArgStyle, DialectSpec, NameSlot
from agent_cli.thinking_tags import ORPHAN_THINK_TAG_RE

# JSON parse 를 시도할 스키마 타입 — string/미선언은 raw 유지.
_COERCE_TYPES = frozenset({"integer", "number", "boolean", "array", "object"})


def coerce_params(action: str, params: dict) -> dict:
    """스키마-주도 타입 강제 — 도구 스키마의 non-string param 만 JSON parse.

    실패는 raw 유지: 진단·복구는 기존 A5(SCHEMA_MISMATCH) 경로 소관이라
    파서가 검증을 중복하지 않는다 (발생 원인 위치 원칙). strict=False —
    raw 값이라 array/object 안의 literal 개행이 정당하다.
    """
    from agent_cli.tools.registry import TOOLS

    tool = TOOLS.get(action)
    if tool is None:
        return params
    props = tool.parameters.get("properties", {})
    out: dict = {}
    for k, v in params.items():
        t = props.get(k, {}).get("type", "")
        if t in _COERCE_TYPES and isinstance(v, str):
            try:
                out[k] = json.loads(v.strip(), strict=False)
                continue
            except (json.JSONDecodeError, ValueError):
                pass
        out[k] = v
    return out


def _fmt_value(value) -> str:
    """파라미터 값 렌더 — str 은 raw 그대로, 그 외는 JSON 인라인 (parse 의
    스키마-주도 강제와 대칭 = round-trip)."""
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False)


def _tag_names(*tokens: str) -> list[str]:
    """``<tool_call>``/``<function=``/``</arg_key><arg_value>`` → 태그 이름들 (중복 제거)."""
    out: list[str] = []
    for tok in tokens:
        for name in re.findall(r"</?([\w:.\-]+)", tok or ""):
            if name not in out:
                out.append(name)
    return out


class _WrapperTokens:
    """호출 래퍼(``call``)만 있는 스펙(JSON_IN_TAG)의 정규식 묶음."""

    def __init__(self, spec: DialectSpec):
        I = re.IGNORECASE
        co, cc = re.escape(spec.call_open), re.escape(spec.call_close)
        self.call_open = re.compile(co, I)
        self.call_close = re.compile(cc, I)
        self.first_struct = self.call_open
        tags = "|".join(map(re.escape, _tag_names(spec.call_open)))
        self.sentinel_line = re.compile(rf"^\s*</?(?:{tags})>\s*$", re.MULTILINE | I)
        self.degen_empty = re.compile(rf"{co}\s*{cc}", I)
        self.degen_open_run = re.compile(rf"{co}(?=\s*{co})", I)


class _TaggedTokens(_WrapperTokens):
    """TAGGED / TAGGED_PAIR 스펙에서 결정적으로 파생되는 정규식 묶음 (한 번 만들어 재사용)."""

    def __init__(self, spec: DialectSpec):
        super().__init__(spec)
        I = re.IGNORECASE
        co, cc = re.escape(spec.call_open), re.escape(spec.call_close)
        self.body_head = spec.name_slot is NameSlot.BODY_HEAD
        if self.body_head:
            # 이름 = 호출 여는 태그 직후 첫 토큰 (``<tool_call>NAME``) — 호출 단위와
            # 이름 단위가 같고 닫는 이름 태그는 없다.
            nw0 = co
            self.name_open = re.compile(rf"{co}\s*([\w.\-]*)", I)
            self.name_close = None
            name_close_ahead = ""
        else:
            nw0, nw1 = re.escape(spec.name_wrap[0]), re.escape(spec.name_wrap[1])
            self.name_open = re.compile(rf"{nw0}([\w.\-]*){nw1}", I)
            self.name_close = re.compile(re.escape(spec.name_close), I)
            name_close_ahead = re.escape(spec.name_close) + "|"
        # 첫 구조 토큰: 호출 래퍼 또는 이름 여는 태그의 접두 (``<function=``)
        self.first_struct = re.compile(rf"{co}|{nw0}", I)
        po = re.escape(spec.param_open_prefix)
        # 키 뒤 접미: TAGGED 는 ``>`` 한 글자, TAGGED_PAIR 는 ``</arg_key><arg_value>`` 처럼
        # 태그 둘 — 태그 경계마다 공백을 허용한다(GLM-4.5 여러 줄 / 4.7 한 줄 모두).
        suffix_tags = re.findall(r"<[^>]+>", spec.param_open_suffix)
        ps = (
            r"\s*" + r"\s*".join(map(re.escape, suffix_tags))
            if suffix_tags
            else re.escape(spec.param_open_suffix)
        )
        self.param_open = re.compile(rf"{po}\s*([\w.\-]+)\s*{ps}", I)
        closer = spec.closer_tag_name
        closer_alt = (
            rf"(?:{re.escape(closer)}|\1)"
            if spec.lenient.key_named_closer
            else re.escape(closer)
        )
        # 닫힌 파라미터 — lookahead 앵커 (PHASE2 §5.3): closer 뒤에 (공백 지나)
        # 다음 구조 토큰이 따라올 때만 경계. 값 안의 고아 closer 는 앵커
        # 불일치로 값에 포함된다. closer 는 ``</parameter>`` 또는 (옵션) 키-이름
        # ``</KEY>`` — 실전(2026-07-17) 35B 가 캐노니컬 블록 안에서
        # ``<parameter=path>…</path>`` 로 닫았다.
        self.struct_ahead = rf"{po}|{name_close_ahead}{cc}|{nw0}|{co}"
        self.param_closed = re.compile(
            rf"{po}\s*([\w.\-]+)\s*{ps}(.*?)</{closer_alt}>\s*(?={self.struct_ahead}|\Z)",
            re.DOTALL | I,
        )
        self.struct_stop = re.compile(self.struct_ahead, I)
        # thought 산문에 흘린 구조 센티널 라인 (sanitize — prior 재주입 방지)
        tags = "|".join(
            rf"{re.escape(n)}(?:=[\w.\-]*)?"
            for n in _tag_names(
                spec.call_open,
                spec.name_wrap[0],
                spec.param_open_prefix,
                spec.param_open_suffix,
                spec.param_close,
            )
        )
        self.sentinel_line = re.compile(rf"^\s*</?(?:{tags})>\s*$", re.MULTILINE | I)


#: 결과 없는 호출에 렌더 때 채우는 `tool` 메시지 본문 (``pair_call_results``).
#: 종결 문구는 실측으로 골랐다(v10.20.0, 6턴 대화 × 12~20, "호출 없이 산문만" 거부율):
#: 없음 10.0% · "Delivered to the user." 17.5% · 지난 최종답을 산문 assistant 로 71%
#: · "completed task" 11.1% · "completed task: <답한 요청>" 9.7%. 긴 안내 문구는
#: 모델이 산문 답을 따라 하게 만들었고, 답한 요청을 인용하면 어느 요청이 닫혔는지
#: 보인다(사용자 제안).
_TERMINAL_CALL_RESULT = "completed task"
_REQUEST_EXCERPT_CHARS = 80


def _request_excerpt(content) -> str:
    """사용자 요청의 첫 줄, 80자까지 — 종결 결과에 인용한다."""
    return str(content or "").strip().split("\n")[0][:_REQUEST_EXCERPT_CHARS]


def _terminal_call_result(request: str) -> str:
    return f"{_TERMINAL_CALL_RESULT}: {request}" if request else _TERMINAL_CALL_RESULT


_MERGED_CALL_RESULT = (
    "No separate result — this call was handled together with the other calls "
    "of this turn; see their results."
)


class Dialect(DialectBase):
    """스펙 구동 와이어 포맷 — ``DialectBase`` ABC 의 전 표면을 스펙에서 유도."""

    def __init__(self, spec: DialectSpec):
        if spec.prose is None:
            raise ValueError(f"dialect '{spec.name}' has no prose fragments")
        self.spec = spec
        # 서브클래스가 클래스 속성 ``name`` 으로 다른 이름을 주면(A/B 변형 — bakeoff
        # 의 json_fc_fenced) 그것이 이긴다. ABC 는 ``name: str`` 주석만 둔다.
        self.name = getattr(type(self), "name", None) or spec.name
        self.action_required = spec.action_required
        self.multi_op = spec.multi_op
        self.degeneration_trigger = spec.degeneration_trigger
        self._t = self._w = None
        if spec.args in (ArgStyle.TAGGED, ArgStyle.TAGGED_PAIR):
            self._t = self._w = _TaggedTokens(spec)
            # 미닫힘 <think> 가 tool call 을 EOF-삼킴하지 않게 구조 마커에서 정지.
            self.thinking_stop = self._t.first_struct
        elif spec.args is ArgStyle.JSON_IN_TAG:
            self._w = _WrapperTokens(spec)
            self.thinking_stop = self._w.first_struct
        else:
            # 미닫힘 <think> 뒤의 bare 배열(라인 선두 `[`)을 EOF-삼킴에서 보호.
            self.thinking_stop = re.compile(r"(?m)^\s*\[")
        # 옛 md_array 프라이어 누출 위생(결정 1 과 별개 — 파싱 관용이 아니라 거부):
        # thought 에 흘린 헤더 센티널 라인 제거, 빈 헤더 반복 러너웨이 감지.
        self._md_sentinel = re.compile(
            r"^\s*##\s*(?:Thought|Action|Input)\s*$", re.MULTILINE
        )
        self._md_degen = re.compile(
            r"##\s*(?:Thought|Action|Input)(?=\s*##\s*(?:Thought|Action|Input))"
        )

    def strip_thinking(self, text: str) -> tuple[str, str | None]:
        """ABC 의 classmethod 는 ``cls.thinking_stop`` 을 읽는다 — 스펙 구동 인스턴스는
        정지점이 인스턴스 속성이라 여기서 인스턴스 값으로 같은 일을 한다."""
        from agent_cli.thinking_tags import strip_think_blocks

        cleaned, thinking = strip_think_blocks(text, stop=self.thinking_stop)
        return cleaned, (thinking or None)

    # ─── Prompt ─────────────────────────────────────────────────

    def format_rules(self) -> str:
        return self.spec.prose.rules

    def render_action_input(self, action_input) -> str:
        # 가이드는 wire-key prefixed dict 를 넘긴다 — flat 콜로.
        if isinstance(action_input, dict) and action_input:
            from agent_cli.tools.registry import TOOLS

            for tool_name in sorted(TOOLS, key=len, reverse=True):
                pfx = tool_name + "_"
                if all(k.startswith(pfx) for k in action_input):
                    flat = {k[len(pfx) :]: v for k, v in action_input.items()}
                    return self.render_call(tool_name, flat)
        if self.spec.args in (ArgStyle.JSON_NATIVE, ArgStyle.JSON_IN_TAG):
            return json.dumps(action_input, ensure_ascii=False)
        if isinstance(action_input, dict):
            return self.render_params(action_input)
        return self.render_params({"result": action_input})

    def render_params(self, params: dict) -> str:
        s = self.spec
        lines = []
        for k, v in params.items():
            text = _fmt_value(v)
            open_ = s.param[0].replace("{k}", str(k))
            if "\n" in text:
                # 멀티라인 값은 블록 스타일 — 파서의 개행-1-트림과 대칭.
                lines.append(f"{open_}\n{text}\n{s.param_close}")
            else:
                lines.append(f"{open_}{text}{s.param_close}")
        return "\n".join(lines)

    def render_call(self, name: str, params: dict) -> str:
        """호출 하나의 캐노니컬 본문 (래퍼 ``call`` 제외)."""
        s = self.spec
        if s.args is ArgStyle.JSON_NATIVE:
            return json.dumps({"action": name, **params}, ensure_ascii=False)
        if s.args is ArgStyle.JSON_IN_TAG:
            return json.dumps({"name": name, "arguments": params}, ensure_ascii=False)
        body = self.render_params(params)
        inner = f"\n{body}" if body else ""
        if s.name_slot is NameSlot.BODY_HEAD:
            return f"{name}{inner}"
        return f"{s.name_wrap[0]}{name}{s.name_wrap[1]}{inner}\n{s.name_close}"

    def _wrap_call(self, call: str) -> str:
        """``call`` 래퍼로 감싼다 — BODY_HEAD 는 이름이 여는 태그에 바로 붙는다."""
        s = self.spec
        sep = "" if s.name_slot is NameSlot.BODY_HEAD else "\n"
        return f"{s.call_open}{sep}{call}\n{s.call_close}"

    def render_full_example(self, *, thought, action: str, action_input: str) -> str:
        s = self.spec
        th = thought if thought is not None else "your reasoning"
        if s.args is ArgStyle.JSON_NATIVE:
            # action_input 은 이미 flat op JSON(render_action_input) — 한 원소 배열로
            # 감싸고, 표준 키 입력이면 action 을 스플라이스.
            op = action_input
            if '"action"' not in op:
                try:
                    obj = json.loads(op)
                    if isinstance(obj, dict):
                        op = json.dumps({"action": action, **obj}, ensure_ascii=False)
                except json.JSONDecodeError:
                    pass
            return f"{th}\n\n[{op}]"
        if s.args is ArgStyle.JSON_IN_TAG:
            # 이미 완성된 호출(``{"name": <이 action>, "arguments": {…}}``)이면 그대로,
            # 아니면 인자 객체로 보고 감싼다. 문자열 검사(``"name" in …``)는 안 된다 —
            # 인자 이름이 ``name``/``arguments`` 인 도구(run_skill·code_index·agent)가
            # 있어, 그 인자 객체를 완성된 호출로 오인하면 문법·파서 모두 어긋난다.
            try:
                obj = json.loads(action_input)
            except json.JSONDecodeError:
                obj = {}
            if (
                isinstance(obj, dict)
                and obj.get("name") == action
                and isinstance(obj.get("arguments"), dict)
            ):
                call = action_input
            else:
                call = json.dumps(
                    {"name": action, "arguments": obj if isinstance(obj, dict) else {}},
                    ensure_ascii=False,
                )
            return f"{th}\n\n{self._wrap_call(call)}"
        if s.name_slot is NameSlot.BODY_HEAD:
            # 이미 ``NAME\n<param…`` 꼴이면 그대로, 파라미터 라인들만이면 이름을 앞에.
            if re.match(r"[\w.\-]+\s*(?:$|<)", action_input or ""):
                call = action_input
            else:
                body = f"\n{action_input}" if action_input else ""
                call = f"{action}{body}"
            return f"{th}\n\n{self._wrap_call(call)}"
        # action_input 이 이미 완성된 이름-태그 콜이면 그대로, 파라미터
        # 라인들만이면 action 으로 감싼다.
        if s.name_wrap[0] in action_input:
            call = action_input
        else:
            body = f"\n{action_input}" if action_input else ""
            call = f"{s.name_wrap[0]}{action}{s.name_wrap[1]}{body}\n{s.name_close}"
        return f"{th}\n\n{self._wrap_call(call)}"

    # ─── Grammar ────────────────────────────────────────────────

    def grammar(self, tools, *, thinking_open: bool = False) -> str | None:
        if self.spec.server_parsed:
            return None  # 서버가 파싱한다 — 디코딩 문법 없음
        if self.spec.args in (ArgStyle.TAGGED, ArgStyle.TAGGED_PAIR):
            return self._grammar_tagged(tools, thinking_open=thinking_open)
        if self.spec.args is ArgStyle.JSON_NATIVE:
            return self._grammar_json_native(tools, thinking_open=thinking_open)
        if self.spec.args is ArgStyle.JSON_IN_TAG:
            return self._grammar_json_in_tag(tools, thinking_open=thinking_open)
        return None

    def _grammar_json_in_tag(self, tools, *, thinking_open: bool) -> str:
        """prose → one or more ``<call>{"name": NAME, "arguments": {…}}</call>``, a
        terminal call only last. ``arguments`` 의 키·값 타입은 열거하되 **필수 enum
        강제(forced)는 걸지 않는다** — 첫 멤버 앞에 쉼표가 없는 객체라 공용
        ``tool_params_expr`` 의 ``( sep item )*`` 꼴이 맞지 않는다; 검증기가 본다."""
        from agent_cli.dialects.grammar import (
            JSON_NONBLANK_STRING,
            JSON_RULES,
            call_sequence_rules,
            enum_literals,
            grammar_params,
            json_value_rule,
            prose_rule,
            think_prefix,
            tool_rule_name,
        )

        s = self.spec
        co, cc = s.call_open, s.call_close
        pre, pre_rules = think_prefix(thinking_open, forbid=s.forbid_in_think)
        lines = [
            rf'root ::= {pre}( calls | prose "\n" calls | prose ) ws',
            prose_rule("prose", s.prose_opener),
            r"ws ::= [ \t\r\n]*",
            *call_sequence_rules(
                "calls",
                [n for n, *_ in tools],
                call=lambda fn: rf'"{co}" ws ( {fn} ) ws "{cc}"',
                sep="ws ",
            ),
        ]
        if pre_rules:
            lines.append(pre_rules)
        for name, flat, extra_ok in tools:
            items = []
            for p in grammar_params(flat):
                if p.kind == "enum":
                    value = enum_literals(p.enum, quote='"')
                elif p.kind == "text_nonempty":
                    value = JSON_NONBLANK_STRING
                else:
                    value = json_value_rule(p.prop)
                items.append(rf'"\"{p.name}\"" j_ws ":" j_ws {value}')
            if extra_ok:
                items.append("j_member")
            rule = tool_rule_name(name)
            if items:
                lines.append(f"a_{rule} ::= " + " | ".join(items))
                args = rf'"{{" j_ws ( a_{rule} ( j_ws "," j_ws a_{rule} )* )? j_ws "}}"'
            else:
                args = r'"{" j_ws "}"'
            lines.append(
                rf'{rule} ::= "{{" j_ws "\"name\"" j_ws ":" j_ws "\"{name}\"" j_ws "," '
                rf'j_ws "\"arguments\"" j_ws ":" j_ws {args} j_ws "}}"'
            )
        lines.append(JSON_RULES)
        return "\n".join(lines)

    def _grammar_json_native(self, tools, *, thinking_open: bool) -> str:
        """prose → one bare JSON array of ``{"action": <name>, <params>}`` ops, a
        terminal op (``complete``) only last. Prose may not contain a paragraph-
        initial ``[`` so the first one is the array — and is bounded."""
        from agent_cli.dialects.grammar import (
            JSON_NONBLANK_STRING,
            JSON_RULES,
            call_sequence_rules,
            enum_literals,
            grammar_params,
            json_value_rule,
            prose_rule,
            think_prefix,
            tool_params_expr,
            tool_rule_name,
        )

        s = self.spec
        pre, pre_rules = think_prefix(thinking_open, forbid=s.forbid_in_think)
        # root: 배열로 바로 시작 | 산문 + 빈 줄 + 배열 | 산문만. 호출은 선택,
        # 빈 배열도 허용(NO_ACTION 넛지가 구제).
        lines = [
            rf'root ::= {pre}( ops | prose "\n\n" ops | prose )',
            prose_rule(
                "prose", s.prose_opener, after_blank_line=s.prose_after_blank_line
            ),
            'ops ::= "[" j_ws ( calls )? j_ws "]"',
            *call_sequence_rules(
                "calls",
                [n for n, *_ in tools],
                call=lambda fn: f"( {fn} )",
                sep='j_ws "," j_ws ',
            ),
        ]
        if pre_rules:
            lines.append(pre_rules)
        for name, flat, extra_ok in tools:
            items: dict[str, str] = {}
            forced = []
            for p in grammar_params(flat):
                if p.kind == "enum":
                    value = enum_literals(p.enum, quote='"')
                elif p.kind == "text_nonempty":
                    value = JSON_NONBLANK_STRING
                else:
                    value = json_value_rule(p.prop)
                items[p.name] = rf'"\"{p.name}\"" j_ws ":" j_ws {value}'
                if p.forced:
                    forced.append(p.name)
            if extra_ok:  # 자유 스키마(MCP 등) — 아무 키나, 값은 JSON 값
                items["*"] = "j_member"
            expr, extra_rules = tool_params_expr(
                name, items, forced, sep='j_ws "," j_ws '
            )
            lines.extend(extra_rules)
            lines.append(
                rf'{tool_rule_name(name)} ::= "{{" j_ws "\"action\"" j_ws ":" j_ws '
                rf'"\"{name}\"" {expr} j_ws "}}"'
            )
        lines.append(JSON_RULES)
        return "\n".join(lines)

    def _grammar_tagged(self, tools, *, thinking_open: bool) -> str:
        """prose → one or more ``<call><name=NAME>…</name></call>``, a terminal call
        (``complete``) only last. Parameter bodies are raw text that may not
        contain the param closer (the only thing that ends them)."""
        from agent_cli.dialects.grammar import (
            call_sequence_rules,
            enum_literals,
            grammar_params,
            nonblank_not_containing,
            not_containing,
            prose_rule,
            think_prefix,
            tool_params_expr,
            tool_rule_name,
        )

        s = self.spec
        co, cc, pc = s.call_open, s.call_close, s.param_close
        po, ps = s.param_open_prefix, s.param_open_suffix
        # 사고 구간에는 호출 여는 태그를 못 쓴다(v9.24.8).
        pre, pre_rules = think_prefix(thinking_open, forbid=s.forbid_in_think)
        lines = [
            rf'root ::= {pre}( calls | prose "\n" calls | prose ) ws',
            prose_rule("prose", s.prose_opener),
            r"ws ::= [ \t\r\n]*",
            *call_sequence_rules(
                "calls",
                [n for n, *_ in tools],
                call=lambda fn: rf'"{co}" ws ( {fn} ) ws "{cc}"',
                sep="ws ",
            ),
            not_containing("body", pc),
            r"pname ::= [A-Za-z0-9_.\-]+",  # 자유 스키마 도구의 임의 키 — 파서의 [\w.\-]+
        ]
        if pre_rules:
            lines.append(pre_rules)
        # 비어있지 않은 본문은 규칙 하나로 두고 참조한다 (xgrammar 컴파일 비용).
        lines.append(f"body_nb ::= {nonblank_not_containing(pc)}")
        for name, flat, extra_ok in tools:
            items: dict[str, str] = {}
            forced = []
            for p in grammar_params(flat):
                if p.kind == "enum":
                    value = rf'"\n"? {enum_literals(p.enum)} "\n"?'
                elif p.kind == "text_nonempty":
                    value = "body_nb"
                else:
                    value = "body"
                items[p.name] = rf'"{po}{p.name}{ps}" {value} "{pc}" ws'
                if p.forced:
                    forced.append(p.name)
            if extra_ok:
                items["*"] = rf'"{po}" pname "{ps}" body "{pc}" ws'
            expr, extra_rules = tool_params_expr(name, items, forced)
            lines.extend(extra_rules)
            if s.name_slot is NameSlot.BODY_HEAD:
                lines.append(rf'{tool_rule_name(name)} ::= "{name}" ws {expr}')
            else:
                lines.append(
                    rf'{tool_rule_name(name)} ::= "{s.name_wrap[0]}{name}{s.name_wrap[1]}" ws {expr} "{s.name_close}"'
                )
        return "\n".join(lines)

    # ─── Parsing (TAGGED) ────────────────────────────────────────

    def parse_turn(self, llm_text: str) -> ParsedTurn:
        text, thinking = self.strip_thinking(llm_text)
        if self.spec.args is ArgStyle.JSON_NATIVE:
            turn = self._parse_json_native(text)
            turn.thinking = thinking
            return turn
        if self.spec.args is ArgStyle.JSON_IN_TAG:
            return self._parse_json_in_tag(text, thinking)
        return self._parse_tagged(text, thinking)

    # ─── Parsing (JSON_IN_TAG) ───────────────────────────────────

    @staticmethod
    def _hermes_op(obj: dict) -> tuple[str | None, dict, bool]:
        """``{"name", "arguments"}`` 객체 → ``(name, args, drifted)``.

        관용(드리프트로 계수): ``parameters`` 키(Llama 식), 문자열로 직렬화된
        ``arguments``, 이름·인자 키 없이 평평하게 적힌 인자."""
        name = obj.get("name")
        name = name if isinstance(name, str) and name else None
        drifted = False
        if "arguments" in obj:
            args = obj["arguments"]
        elif "parameters" in obj:
            args, drifted = obj["parameters"], True
        else:
            args = {k: v for k, v in obj.items() if k != "name"}
            drifted = bool(args)
        if isinstance(args, str):
            drifted = True
            try:
                args = json.loads(args)
            except json.JSONDecodeError:
                args = {}
        if not isinstance(args, dict):
            args, drifted = {}, True
        return name, args, drifted

    def _parse_json_in_tag(self, text: str, thinking) -> ParsedTurn:
        """``<call>{json}</call>`` 블록 순회 — 자격 = 세그먼트에 ``{`` 가 있다;
        인용(펜스·인라인) 안 후보는 자격 있는 비인용 후보가 있을 때만 제외."""
        w = self._w
        fences = quote_spans(text) if self.spec.lenient.inline_quotes_exclude else []
        opens = list(w.call_open.finditer(text))
        cands: list[tuple[re.Match, int, bool]] = []
        for m in opens:
            nxt = w.call_open.search(text, m.end())
            close = w.call_close.search(text, m.end())
            ends = [x.start() for x in (nxt, close) if x is not None]
            end = min(ends) if ends else len(text)
            if "{" in text[m.end() : end]:
                cands.append((m, end, any(a <= m.start() < b for a, b in fences)))
        unquoted = [c for c in cands if not c[2]]
        use = unquoted or cands
        if not use:
            return self._parse_json_in_tag_bare(text, thinking)
        start = use[0][0].start()
        thought = self.sanitize_thought(text[:start]) or None
        ops: list[Op] = []
        drifted = False
        for m, end, _q in use:
            seg = text[m.end() : end]
            parsed, repaired = _extract_op_json(seg)
            if parsed is None:
                continue
            obj = parsed[0] if isinstance(parsed, list) and parsed else parsed
            if not isinstance(obj, dict):
                continue
            name, args, d = self._hermes_op(obj)
            if name is None and not args:
                continue
            trunc = repaired and close_unbalanced(seg)[1]
            ops.append(Op(action=name, action_input=args, truncated=trunc))
            drifted |= repaired or d
        if not ops:
            if thought:
                return ParsedTurn(
                    thought=thought, ops=[], raw=text, parse_stage=1, thinking=thinking
                )
            return ParsedTurn(raw=text, parse_stage=0, thinking=thinking)
        drifted |= len(opens) != len(w.call_close.findall(text))
        return ParsedTurn(
            thought=thought,
            ops=ops,
            raw=text,
            parse_stage=2 if drifted else 1,
            thinking=thinking,
        )

    def _parse_json_in_tag_bare(self, text: str, thinking) -> ParsedTurn:
        """래퍼 없이 ``{"name": …, "arguments": …}`` 만 적은 드리프트 — stage 2."""
        start = _op_anchor(text)
        if start >= 0:
            parsed, _rep = _extract_op_json(text[start:])
            arr = parsed if isinstance(parsed, list) else [parsed]
            objs = [
                x
                for x in arr
                if isinstance(x, dict) and ("name" in x or "arguments" in x)
            ]
            if objs:
                ops = []
                for obj in objs:
                    name, args, _d = self._hermes_op(obj)
                    if name is None and not args:
                        continue
                    ops.append(Op(action=name, action_input=args, truncated=False))
                if ops:
                    thought = self.sanitize_thought(text[:start]) or None
                    return ParsedTurn(
                        thought=thought,
                        ops=ops,
                        raw=text,
                        parse_stage=2,
                        thinking=thinking,
                    )
        thought = self.sanitize_thought(text)
        if thought and thought.strip():
            return ParsedTurn(
                thought=thought, ops=[], raw=text, parse_stage=1, thinking=thinking
            )
        return ParsedTurn(raw=text, parse_stage=0, thinking=thinking)

    # ─── Parsing (JSON_NATIVE) ───────────────────────────────────

    def _parse_json_native(self, llm_text: str) -> ParsedTurn:
        """산문 + op JSON 배열/객체 (bare 객체 = 1-op 관용) — 옛 json_fc 의 캐노니컬
        경로. md_array 헤더 경로는 없다(결정 1)."""

        def _ops(items, truncated_last: bool = False) -> list:
            # P0-3: EOF 절단 증거(미닫힘 괄호를 close_unbalanced 로 복구)가 있으면
            # **마지막 op 에만** truncated — 앞선 op 들은 온전하다.
            last = len(items) - 1
            return [
                Op(
                    action=(
                        it.get("action") if isinstance(it.get("action"), str) else None
                    ),
                    action_input={k: v for k, v in it.items() if k != "action"},
                    truncated=truncated_last and idx == last,
                )
                for idx, it in enumerate(items)
            ]

        # 앵커는 _op_anchor 가 고른다 — 본문 첫 `[`/`{` 가 아니라 **자격 있는** op
        # 시작(산문 속 `board[i][j]`, `- [ ]`, `[문서](url)` 를 거른다).
        start = _op_anchor(llm_text)
        if start >= 0:
            thought = self.sanitize_thought(llm_text[:start]) or None
            body = llm_text[start:]
            parsed, repaired = _extract_op_json(body)
            if parsed is not None:
                arr = parsed if isinstance(parsed, list) else [parsed]
                items = [x for x in arr if isinstance(x, dict)]
                stage = 2 if repaired else 1
                trunc = repaired and close_unbalanced(body)[1]
                # `any("action")` 가드: 산문 속 `[1,2,3]` 같은 비-op 배열은 thought-only.
                if any("action" in it for it in items):
                    return ParsedTurn(
                        thought=thought,
                        ops=_ops(items, truncated_last=trunc),
                        raw=llm_text,
                        parse_stage=stage,
                    )
                # actionless-op 보존 불변식: action 은 없지만 input 을 실은 dict-op 는
                # 위치 신호(배열이 emission 의 끝)로 op 의도 — infer/NO_ACTION 재료.
                if (
                    items
                    and all(isinstance(x, dict) and x for x in arr)
                    and llm_text.rstrip().endswith(("]", "}"))
                ):
                    return ParsedTurn(
                        thought=thought,
                        ops=_ops(items),
                        raw=llm_text,
                        parse_stage=stage,
                    )
            elif '"action"' in body:
                # op 시도 흔적이 있는 깨진 JSON — 수리까지 실패한 진짜 파손 → stage 0
                # (NO_JSON 진단 + 캐럿).
                return ParsedTurn(thought=thought, raw=llm_text, parse_stage=0)

        # thought-only / blank — 완료가 아니라 NO_ACTION 넛지 대상.
        thought = self.sanitize_thought(llm_text)
        if thought and thought.strip():
            return ParsedTurn(thought=thought, ops=[], raw=llm_text, parse_stage=1)
        return ParsedTurn(raw=llm_text, parse_stage=0)

    def _call_opens(self, text: str) -> list[re.Match]:
        """이름-여는 태그 중 **자격 있는** 것 (후보 검증 ①②, v7.28.1/v9.24.8).

        ① 세그먼트 자격: open 뒤 다음 구조 토큰 전에 param 또는 closer 가 와야
        호출. EOF 까지 닿으면 자격 유지(절단 관용 — A5 진단 보존).
        ② 인용: 균형 펜스·인라인 코드 안의 후보는 예시 — 자격 있는 비인용 후보가
        있을 때만 제외. 세그먼트 경계는 **raw** 구조 토큰으로 — 언급의 세그먼트가
        실호출의 파라미터를 삼키면 언급이 그 파라미터로 자격을 얻는다."""
        t = self._t
        fences = quote_spans(text) if self.spec.lenient.inline_quotes_exclude else []

        def fenced(pos: int) -> bool:
            return any(a <= pos < b for a, b in fences)

        qualified: list[tuple[re.Match, bool]] = []
        for m in t.name_open.finditer(text):
            nxt = t.first_struct.search(text, m.end())
            seg = text[m.end() : nxt.start()] if nxt else text[m.end() :]
            ok = (
                nxt is None
                or t.param_open.search(seg) is not None
                or (t.name_close is not None and t.name_close.search(seg) is not None)
                or t.call_close.search(seg) is not None
            )
            if ok:
                qualified.append((m, fenced(m.start())))
        unquoted = [m for m, f in qualified if not f]
        return unquoted or [m for m, _f in qualified]

    def _call_anchor(self, text: str) -> int | None:
        """첫 실호출의 시작 — 공백만 사이에 둔 래퍼 ``<tool_call>`` 까지 거슬러
        본문에 남긴다(드리프트 계수가 래퍼/이름 수를 비교한다)."""
        opens = self._call_opens(text)
        if not opens:
            return None
        anchor = opens[0].start()
        for tc in self._t.call_open.finditer(text, 0, anchor):
            if not text[tc.end() : anchor].strip():
                return tc.start()
        return anchor

    def _extract_params(self, segment: str) -> tuple[dict, bool]:
        """한 이름 블록 본문에서 파라미터 추출 — ``(params, truncated)``.
        1) 닫힌 파라미터(lookahead 앵커) 전부, 2) 남은 미닫힘 open 은 다음 구조
        토큰/EOF 까지를 값으로 회수(절단·closer 생략 변종)."""
        t = self._t
        params: dict = {}
        truncated = False
        covered = 0
        for pm in t.param_closed.finditer(segment):
            params[pm.group(1)] = _trim_block(pm.group(2))
            covered = pm.end()
        tail = segment[covered:]
        while True:
            om = t.param_open.search(tail)
            if om is None:
                break
            rest = tail[om.end() :]
            stop = t.struct_stop.search(rest)
            raw = rest[: stop.start()] if stop else rest
            params[om.group(1)] = _trim_block(raw.rstrip())
            truncated = True
            tail = rest[stop.start() :] if stop else ""
        return params, truncated

    def _parse_tagged(self, text: str, thinking) -> ParsedTurn:
        t = self._t
        anchor = self._call_anchor(text)
        first = t.first_struct.search(text) if anchor is None else None
        if anchor is None and first is None:
            lenient = self._parse_lenient(text)
            if lenient is not None:
                lenient.thinking = thinking
                return lenient
            thought = self.sanitize_thought(text)
            if thought and thought.strip():
                return ParsedTurn(
                    thought=thought, ops=[], raw=text, parse_stage=1, thinking=thinking
                )
            return ParsedTurn(raw=text, parse_stage=0, thinking=thinking)

        start = anchor if anchor is not None else first.start()
        thought = self.sanitize_thought(text[:start]) or None
        body = text[start:]
        ops, drifted, truncated_any = self._extract_ops(body)

        if not ops:
            lenient = self._parse_lenient(text)
            if lenient is not None:
                lenient.thinking = thinking
                return lenient
            if thought:
                return ParsedTurn(
                    thought=thought, ops=[], raw=text, parse_stage=1, thinking=thinking
                )
            return ParsedTurn(raw=text, parse_stage=0, thinking=thinking)

        return ParsedTurn(
            thought=thought,
            ops=ops,
            terminal=False,
            raw=text,
            parse_stage=2 if (drifted or truncated_any) else 1,
            thinking=thinking,
        )

    def _parse_lenient(self, text: str) -> ParsedTurn | None:
        """tool-name 태그 변종 구제 (``Lenient.tag_name_variants``) — strict 0-op
        뒤의 최후 폴백. 라인-단독 ``<TOOL>`` (등록 도구명) 오픈을 op 경계로."""
        if not self.spec.lenient.tag_name_variants:
            return None
        opens = list(_lenient_tool_open_re().finditer(text))
        if not opens:
            return None
        thought = self.sanitize_thought(text[: opens[0].start()]) or None
        ops: list[Op] = []
        for i, m in enumerate(opens):
            seg_end = opens[i + 1].start() if i + 1 < len(opens) else len(text)
            segment = text[m.end() : seg_end]
            name = m.group(1)
            close_m = re.search(rf"</{re.escape(name)}>", segment, re.IGNORECASE)
            if close_m is not None:
                segment = segment[: close_m.start()]
            params = coerce_params(name, _extract_params_lenient(segment))
            ops.append(Op(action=name, action_input=params, truncated=False))
        return ParsedTurn(
            thought=thought, ops=ops, terminal=False, raw=text, parse_stage=2
        )

    def _extract_ops(self, body: str) -> tuple[list[Op], bool, bool]:
        """``(ops, drifted, truncated_any)`` — 이름 블록 순회 추출. 래퍼는 파싱에
        필수가 아니다(bare 이름 태그도 수용하되 drift 로 계수). 미닫힘 태그도 drift."""
        t = self._t
        name_opens = list(t.name_open.finditer(body))
        qualified_starts = {m.start() for m in self._call_opens(body)}
        ops: list[Op] = []
        truncated_any = False
        # v10.1.3: 닫힌 파라미터의 **값 안**에 있는 이름·파라미터 태그는 텍스트다.
        # 종전엔 세그먼트를 다음 이름 태그에서 무조건 잘라, 값에 포맷 자체가
        # 적힌 경우(문서·동료에게 보내는 형식 예시, 백틱·펜스 안이라도) 값이
        # 잘리고 유령 op(`shell {}`)가 생겼다. 닫힌 파라미터를 이름 태그 직후부터
        # 순차 소비한 뒤 그 뒤에서만 다음 이름 태그를 찾는다.
        consumed_until = -1
        n_name = 0
        consumed: list[
            tuple[int, int]
        ] = []  # 닫힌 파라미터 값 영역 — 드리프트 계수 제외
        for fm in name_opens:
            if fm.start() < consumed_until:
                continue  # 앞 호출의 닫힌 파라미터 값 안 — 텍스트
            n_name += 1
            if fm.start() not in qualified_starts:
                continue
            cursor = fm.end()
            while True:
                nxt = cursor
                while nxt < len(body) and body[nxt].isspace():
                    nxt += 1
                pm = t.param_closed.match(body, nxt)
                if pm is None:
                    break
                cursor = pm.end()
            consumed_until = cursor
            if cursor > fm.end():
                consumed.append((fm.end(), cursor))
            seg_end = next(
                (m.start() for m in name_opens if m.start() >= cursor), len(body)
            )
            segment = body[fm.end() : seg_end]
            params, trunc = self._extract_params(segment)
            hybrid = False
            if not params and self.spec.lenient.tag_name_variants:
                # 하이브리드 변종: 캐노니컬 이름 태그 안에 plain-tag 파라미터.
                inner = segment
                close_m = t.name_close.search(inner) if t.name_close else None
                if close_m is not None:
                    inner = inner[: close_m.start()]
                params = _extract_params_lenient(inner)
                hybrid = bool(params)
            name = fm.group(1).strip() or None
            if name is None and not params:
                continue  # 이름도 파라미터도 없는 빈 골격
            if name is not None and self.spec.value_mode == "raw":
                params = coerce_params(name, params)
            ops.append(Op(action=name, action_input=params, truncated=trunc))
            truncated_any |= trunc or hybrid

        def _outside(pattern) -> int:
            # 값 안의 래퍼·닫는 태그는 텍스트 — 드리프트 계수에서 뺀다 (v10.1.3)
            return sum(
                1
                for m in pattern.finditer(body)
                if not any(a <= m.start() < b for a, b in consumed)
            )

        n_co = _outside(t.call_open)
        n_cc = _outside(t.call_close)
        drifted = n_co != n_name or n_co != n_cc
        if t.name_close is not None:
            drifted |= _outside(t.name_close) != n_name
        return ops, drifted, truncated_any

    # ─── Degeneration / sanitize ────────────────────────────────

    def is_degenerate(self, text: str) -> bool:
        if self._w is None:
            # 캐노니컬 bare-배열 모양의 러너웨이 시그니처는 실측 전 — 옛 헤더
            # 반복(프라이어 누출)만 감지.
            return len(self._md_degen.findall(text)) >= 2
        hits = len(self._w.degen_empty.findall(text)) + len(
            self._w.degen_open_run.findall(text)
        )
        return hits >= 2

    def sanitize_thought(self, thought: str | None) -> str | None:
        if not thought:
            return thought
        thought = ORPHAN_THINK_TAG_RE.sub("", thought)
        sentinel = self._w.sentinel_line if self._w is not None else self._md_sentinel
        return sentinel.sub("", thought).strip()

    def diagnose_syntax_error(self, prior_content: str) -> str | None:
        if self.spec.args not in (ArgStyle.JSON_NATIVE, ArgStyle.JSON_IN_TAG):
            return None
        # 첫 브레이스부터가 JSON 후보.
        start = next((i for i, c in enumerate(prior_content) if c in "[{"), -1)
        candidate = prior_content[start:] if start >= 0 else prior_content
        return describe_json_error(candidate)

    # ─── History round-trip (multi-op record) ───────────────────

    def serialize_assistant_for_history(self, raw_text: str) -> dict:
        turn = self.parse_turn(raw_text)
        if turn.ops:
            return {
                "role": "assistant",
                "thought": turn.thought or "",
                "ops": [
                    {"action": op.action, "action_input": op.action_input or {}}
                    for op in turn.ops
                ],
            }
        return {"role": "assistant", "content": self.sanitize_thought(raw_text) or ""}

    def serialize_terminal_for_history(
        self, thought: str, result: str, answers: list[str] | None = None
    ) -> dict:
        return {
            "role": "assistant",
            "thought": thought or "",
            "ops": [
                {
                    "action": self.spec.terminal_op,
                    "action_input": _terminal_input(result, answers),
                }
            ],
        }

    @property
    def server_parsed(self) -> bool:
        return bool(self.spec.server_parsed)

    def ops_from_server_calls(self, tool_calls: list[dict]) -> list[Op]:
        """서버가 파싱해 준 호출 목록 → 이 턴의 op 들 (v10.20.0, 서버 파싱 방언).

        종전엔 호출을 flat op 배열 **텍스트**로 바꿔 ``parse_turn`` 에 다시
        넣었다 — 산문(content)에 `[{"action": …}]` 모양이 있으면 그쪽이
        "첫 배열" 로 이겨 실제 호출이 버려졌다(재현: 생각 속 배열 + 실제 호출
        → 생각 속 배열만 실행). 호출은 서버가 이미 구조로 줬으니 그대로 쓴다.
        인자가 JSON 이 아니었던 호출(provider 가 ``input=None`` + 원문
        ``arguments``)은 ``action_input`` 에 그 **문자열**을 싣는다 — dispatch
        가 실행 대신 "인자가 JSON 이 아님" 을 알린다."""
        ops: list[Op] = []
        for tc in tool_calls:
            if not isinstance(tc, dict) or not tc.get("name"):
                continue
            inp = tc.get("input")
            if isinstance(inp, dict):
                ops.append(Op(action=tc["name"], action_input=inp))
            else:
                ops.append(
                    Op(action=tc["name"], action_input=str(tc.get("arguments") or ""))
                )
        return ops

    @staticmethod
    def call_id(assistant_index: int, op_index: int) -> str:
        """렌더 시 합성하는 호출 id — assistant 와 뒤따르는 관찰이 같은 규칙."""
        return f"call_{assistant_index}_{op_index}"

    def render_observation_from_history(
        self, record: dict, *, index: int, assistant_index: int | None
    ) -> list[dict] | None:
        if not self.spec.server_parsed:
            return None
        content = record.get("content") or ""
        if isinstance(content, str) and content.startswith("Observation: "):
            content = content[len("Observation: ") :]
        artifact = record.get("artifact") or ""
        if artifact:
            content = f"{content}\n→ {artifact}"
        if assistant_index is None:
            # 짝이 되는 호출이 없다(형식 거절 — 거절된 호출은 저장되지 않는다).
            # `tool` 메시지는 바로 앞 assistant 의 tool_calls 에 대한 답이어야
            # 하므로, 호출 없는 턴의 넛지와 같은 user 메시지로 낸다 (v10.14.0).
            return [{"role": "user", "content": content}]
        # 배치 관찰은 op 별 조각(`parts`)으로, 단일 관찰은 본문 하나로 — 둘 다
        # 조각 목록 하나로 보고 op 순서대로 `tool` 메시지를 낸다.
        parts = record.get("parts") or [{"content": content}]
        return [
            {
                "role": "tool",
                "tool_call_id": self.call_id(assistant_index, i),
                "content": str(p.get("content") or ""),
            }
            for i, p in enumerate(parts)
        ]

    def pair_call_results(self, messages: list[dict]) -> list[dict]:
        """모든 ``tool_calls`` 가 같은 id 의 `tool` 메시지를 갖게 한다 (v10.20.0).

        OpenAI 규격은 호출마다 결과를 요구한다. 기록은 그렇지 않은 턴을 만든다:
        같은 파일 편집 N 개는 한 번에 적용돼 결과가 하나고(병렬 배치·중단된
        배치의 남은 호출도 같다), 종결 호출(`complete`)은 결과가 없다. 종전엔
        답 없는 호출이 그대로 나갔다 — 엄격한 서버는 400, 너그러운 서버에서도
        모델은 불렀는데 결과가 없는 호출을 본다. 결과는 호출 순서대로 붙으므로
        (``render_observation_from_history``) 빠진 id 는 뒤쪽이고, 거기에 짧은
        안내를 채운다: 종결 호출에는 "completed task: <그 턴이 답한 요청>"
        (앞선 마지막 사용자 요청의 첫 줄 — 하니스 안내문은 제외), 합쳐진
        호출에는 "다른 호출과 함께 처리됨". 기록은 바꾸지 않는다 — 옛 세션도
        읽을 때 맞는다."""
        if not self.spec.server_parsed:
            return messages
        from agent_cli.dialects import all_system_user_prefixes

        nudge_prefixes = all_system_user_prefixes()
        out: list[dict] = []
        i = 0
        n = len(messages)
        last_request = ""
        while i < n:
            msg = messages[i]
            calls = msg.get("tool_calls") if msg.get("role") == "assistant" else None
            if not calls:
                if msg.get("role") == "user" and not any(
                    str(msg.get("content") or "").startswith(p) for p in nudge_prefixes
                ):
                    last_request = _request_excerpt(msg.get("content"))
                out.append(msg)
                i += 1
                continue
            j = i + 1
            answered: dict[str, dict] = {}
            while j < n and messages[j].get("role") == "tool":
                answered[messages[j].get("tool_call_id") or ""] = messages[j]
                j += 1
            out.append(msg)
            for c in calls:
                reply = answered.get(c["id"])
                if reply is None:
                    name = (c.get("function") or {}).get("name")
                    reply = {
                        "role": "tool",
                        "tool_call_id": c["id"],
                        "content": _terminal_call_result(last_request)
                        if name == self.spec.terminal_op
                        else _MERGED_CALL_RESULT,
                    }
                out.append(reply)
            i = j
        return out

    def render_assistant_from_history(
        self, record: dict, *, index: int | None = None
    ) -> dict:
        ops = record.get("ops")
        if isinstance(ops, list) and ops and self.spec.server_parsed:
            # 서버 네이티브: 구조화 메시지 (NATIVE.md §3). content 는 thought 만.
            msg: dict = {
                "role": "assistant",
                "content": record.get("thought") or "",
                "tool_calls": [
                    {
                        "id": self.call_id(index or 0, i),
                        "type": "function",
                        "function": {
                            "name": o.get("action") or "",
                            "arguments": json.dumps(
                                o.get("action_input") or {}, ensure_ascii=False
                            ),
                        },
                    }
                    for i, o in enumerate(ops)
                    if isinstance(o, dict)
                ],
            }
            return msg
        if isinstance(ops, list) and ops:
            s = self.spec
            if s.args is ArgStyle.JSON_NATIVE:
                rendered = json.dumps(
                    [
                        {"action": o.get("action"), **(o.get("action_input") or {})}
                        for o in ops
                        if isinstance(o, dict)
                    ],
                    ensure_ascii=False,
                )
                thought = record.get("thought", "")
                content = f"{thought}\n\n{rendered}" if thought else rendered
                return {"role": "assistant", "content": content}
            calls = "\n".join(
                self._wrap_call(
                    self.render_call(o.get("action") or "", o.get("action_input") or {})
                )
                for o in ops
                if isinstance(o, dict)
            )
            thought = record.get("thought", "")
            content = f"{thought}\n\n{calls}" if thought else calls
            return {"role": "assistant", "content": content}
        return super().render_assistant_from_history(record)

    # ─── Recovery wording (조각) ────────────────────────────────

    def constraint_reminder_call(self) -> str:
        return self.spec.prose.reminder_call

    def constraint_reminder_action_required(self) -> str:
        return self.spec.prose.reminder_action_required

    def failure_framing_parse_fail(self) -> str:
        return self.spec.prose.framing_parse_fail

    def no_action_detail(self) -> str:
        return self.spec.prose.no_action_detail

    def static_retry_hint_no_json(self) -> str:
        return self.spec.prose.retry_no_json

    def static_retry_hint_no_action(self) -> str:
        return self.spec.prose.retry_no_action

    def system_user_prefixes(self) -> tuple[str, ...]:
        return self.spec.prose.user_prefixes
