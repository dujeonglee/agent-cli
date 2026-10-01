"""Dialect — 스펙 하나로 렌더·파서·문법·산문·history 왕복을 만드는 엔진 (Phase 5).

설계: docs/multi-wire-format/PHASE5.md §4.3. 옛 ``xml_fc``/``json_fc`` 모듈의 동작을
스펙에서 읽은 토큰으로 일반화한 것이라, 각 경로의 주석은 그 모듈에서 가져왔다.
등가성 합격선(§7)은 옛 모듈을 ``_legacy/`` 에 두고 코퍼스로 잰다.

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

from agent_cli.thinking_tags import ORPHAN_THINK_TAG_RE
from agent_cli.wire_formats.base import (
    Op,
    ParsedTurn,
    WireFormat,
    _terminal_input,
)
from agent_cli.wire_formats.recovery import quote_spans
from agent_cli.wire_formats.recovery.tagged import (
    _extract_params_lenient,
    _lenient_tool_open_re,
    _trim_block,
)
from agent_cli.wire_formats.spec import ArgStyle, DialectSpec

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


class _TaggedTokens:
    """TAGGED 스펙에서 결정적으로 파생되는 정규식 묶음 (한 번 만들어 재사용)."""

    def __init__(self, spec: DialectSpec):
        I = re.IGNORECASE
        co, cc = re.escape(spec.call_open), re.escape(spec.call_close)
        nw0, nw1 = re.escape(spec.name_wrap[0]), re.escape(spec.name_wrap[1])
        po, ps = re.escape(spec.param_open_prefix), re.escape(spec.param_open_suffix)
        self.call_open = re.compile(co, I)
        self.call_close = re.compile(cc, I)
        self.name_open = re.compile(rf"{nw0}([\w.\-]*){nw1}", I)
        self.name_close = re.compile(re.escape(spec.name_close), I)
        # 첫 구조 토큰: 호출 래퍼 또는 이름 여는 태그의 접두 (``<function=``)
        self.first_struct = re.compile(rf"{co}|{nw0}", I)
        self.param_open = re.compile(rf"{po}([\w.\-]+){ps}", I)
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
        self.struct_ahead = rf"{po}|{re.escape(spec.name_close)}|{cc}|{nw0}|{co}"
        self.param_closed = re.compile(
            rf"{po}([\w.\-]+){ps}(.*?)</{closer_alt}>\s*(?={self.struct_ahead}|\Z)",
            re.DOTALL | I,
        )
        self.struct_stop = re.compile(self.struct_ahead, I)
        # thought 산문에 흘린 구조 센티널 라인 (sanitize — prior 재주입 방지)
        c_tag = re.escape(spec.tag_name(spec.call_open))
        n_tag = re.escape(spec.tag_name(spec.name_wrap[0]))
        p_tag = re.escape(spec.tag_name(spec.param_open_prefix))
        self.sentinel_line = re.compile(
            rf"^\s*</?(?:{c_tag}|{n_tag}(?:=[\w.\-]*)?|{p_tag}(?:=[\w.\-]*)?)>\s*$",
            re.MULTILINE | I,
        )
        # format runaway: 빈 래퍼 골격 반복 (≥2 임계)
        self.degen_empty = re.compile(rf"{co}\s*{cc}", I)
        self.degen_open_run = re.compile(rf"{co}(?=\s*{co})", I)


class Dialect(WireFormat):
    """스펙 구동 와이어 포맷 — ``WireFormat`` ABC 의 전 표면을 스펙에서 유도."""

    def __init__(self, spec: DialectSpec):
        if spec.prose is None:
            raise ValueError(f"dialect '{spec.name}' has no prose fragments")
        self.spec = spec
        self.name = spec.name
        self.action_required = spec.action_required
        self.multi_op = spec.multi_op
        self.exposes_complete = spec.exposes_complete
        self.degeneration_trigger = spec.degeneration_trigger
        if spec.args in (ArgStyle.TAGGED, ArgStyle.TAGGED_PAIR):
            self._t = _TaggedTokens(spec)
            # 미닫힘 <think> 가 tool call 을 EOF-삼킴하지 않게 구조 마커에서 정지.
            self.thinking_stop = self._t.first_struct
        else:
            self._t = None
            self.thinking_stop = re.compile(r"(?m)^\s*\[")

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
        body = self.render_params(params)
        inner = f"\n{body}" if body else ""
        return f"{s.name_wrap[0]}{name}{s.name_wrap[1]}{inner}\n{s.name_close}"

    def render_full_example(self, *, thought, action: str, action_input: str) -> str:
        s = self.spec
        th = thought if thought is not None else "your reasoning"
        # action_input 이 이미 완성된 이름-태그 콜이면 그대로, 파라미터
        # 라인들만이면 action 으로 감싼다.
        if s.name_wrap[0] in action_input:
            call = action_input
        else:
            body = f"\n{action_input}" if action_input else ""
            call = f"{s.name_wrap[0]}{action}{s.name_wrap[1]}{body}\n{s.name_close}"
        return f"{th}\n\n{s.call_open}\n{call}\n{s.call_close}"

    # ─── Grammar ────────────────────────────────────────────────

    def grammar(self, tools, *, thinking_open: bool = False) -> str | None:
        if self.spec.args is ArgStyle.TAGGED:
            return self._grammar_tagged(tools, thinking_open=thinking_open)
        return None

    def _grammar_tagged(self, tools, *, thinking_open: bool) -> str:
        """prose → one or more ``<call><name=NAME>…</name></call>``, a terminal call
        (``complete``) only last. Parameter bodies are raw text that may not
        contain the param closer (the only thing that ends them)."""
        from agent_cli.wire_formats.grammar import (
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
            lines.append(
                rf'{tool_rule_name(name)} ::= "{s.name_wrap[0]}{name}{s.name_wrap[1]}" ws {expr} "{s.name_close}"'
            )
        return "\n".join(lines)

    # ─── Parsing (TAGGED) ────────────────────────────────────────

    def parse_turn(self, llm_text: str) -> ParsedTurn:
        text, thinking = self.strip_thinking(llm_text)
        if self._t is None:
            raise NotImplementedError(f"ArgStyle {self.spec.args} — S3/S4")
        return self._parse_tagged(text, thinking)

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
                or t.name_close.search(seg) is not None
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
        for i, fm in enumerate(name_opens):
            seg_end = (
                name_opens[i + 1].start() if i + 1 < len(name_opens) else len(body)
            )
            if fm.start() not in qualified_starts:
                continue
            segment = body[fm.end() : seg_end]
            params, trunc = self._extract_params(segment)
            hybrid = False
            if not params and self.spec.lenient.tag_name_variants:
                # 하이브리드 변종: 캐노니컬 이름 태그 안에 plain-tag 파라미터.
                inner = segment
                close_m = t.name_close.search(inner)
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

        n_name = len(name_opens)
        n_co = len(t.call_open.findall(body))
        n_cc = len(t.call_close.findall(body))
        n_nc = len(t.name_close.findall(body))
        drifted = n_co != n_name or n_co != n_cc or n_nc != n_name
        return ops, drifted, truncated_any

    # ─── Degeneration / sanitize ────────────────────────────────

    def is_degenerate(self, text: str) -> bool:
        if self._t is None:
            return False
        hits = len(self._t.degen_empty.findall(text)) + len(
            self._t.degen_open_run.findall(text)
        )
        return hits >= 2

    def sanitize_thought(self, thought: str | None) -> str | None:
        if not thought:
            return thought
        thought = ORPHAN_THINK_TAG_RE.sub("", thought)
        if self._t is not None:
            thought = self._t.sentinel_line.sub("", thought)
        return thought.strip()

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

    def render_assistant_from_history(self, record: dict) -> dict:
        ops = record.get("ops")
        if isinstance(ops, list) and ops:
            s = self.spec
            calls = "\n".join(
                f"{s.call_open}\n"
                + self.render_call(o.get("action") or "", o.get("action_input") or {})
                + f"\n{s.call_close}"
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
