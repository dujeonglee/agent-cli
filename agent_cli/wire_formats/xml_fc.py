"""xml_fc wire format — 태그-파라미터 function call (multi-op).

Self-contained module (json_fc 와 코드 공유 0 — wire-format self-contained
불변식): 포맷 규칙 텍스트·파서·recovery 문구·히스토리 round-trip 전부 이
파일이 소유. 설계는 docs/multi-wire-format/PHASE2.md (D4=산문 thought,
D6=관찰 되먹임 현행 유지).

Wire shape::

    <자유 산문 thought — 선택>

    <tool_call>
    <function=read_file>
    <parameter=path>src/main.c</parameter>
    </function>
    </tool_call>

- 멀티-op = ``<tool_call>`` 블록 반복. 종료 = 명시적 ``complete`` op.
- **파라미터 값은 raw 텍스트** (JSON 아님) — write_file content·complete
  result 의 literal-control-char / under-escape 실패 클래스가 구조적으로
  소멸. 트레이드: 값 안의 ``</parameter>`` 유사 텍스트와의 구분자 충돌은
  lookahead 앵커로 방어 (§5.3 — 닫는 태그 뒤에 구조 토큰이 따라올 때만
  경계로 인정).
- 타입은 스키마-주도 강제: 도구 스키마가 string 이 아닌 param 만 JSON
  parse 시도, 실패 시 raw 유지 (진단은 기존 A5 SCHEMA_MISMATCH 경로 —
  파서가 검증을 중복하지 않는다).
- thought = 첫 구조 토큰 앞 산문 (D4). ``<think>`` 는 wire 의 슬롯이
  아니다 — provider(openai)와 stage 0(``strip_thinking``) 두 층이 이미
  격리하는 CoT 채널이라, 요구하면 파서에 도달하기 전에 사라진다.
"""

from __future__ import annotations

import json
import re

from agent_cli.thinking_tags import ORPHAN_THINK_TAG_RE
from agent_cli.wire_formats.base import Op, ParsedAction, ParsedTurn, WireFormat

# ── 구조 토큰 ────────────────────────────────────────────────

_FUNC_OPEN = re.compile(r"<function=([\w.\-]*)>", re.I)
_FIRST_STRUCT = re.compile(r"<tool_call>|<function=", re.I)
_TOOL_CALL_OPEN = re.compile(r"<tool_call>", re.I)
_TOOL_CALL_CLOSE = re.compile(r"</tool_call>", re.I)
_FUNC_CLOSE = re.compile(r"</function>", re.I)
_PARAM_OPEN = re.compile(r"<parameter=([\w.\-]+)>", re.I)

# 닫힌 파라미터 — lookahead 앵커 (§5.3): closer 뒤에 (공백 지나) 다음
# 구조 토큰이 따라올 때만 경계. 값 안의 고아 closer 는 앵커 불일치로
# 값에 포함된다. 비탐욕이라 앵커를 만족하는 **첫** 경계에서 닫힘 —
# 값이 진짜 ``</parameter>\n<parameter=`` 시퀀스를 담는 최악 케이스는
# 잘못 잘리지만 도구 에러/diff 로 표면화 (조용한 오염 없음).
#
# closer 는 ``</parameter>`` 또는 **키-이름** ``</KEY>`` (백레퍼런스 \1):
# 실전(board 세션 2026-07-17) 35B 가 캐노니컬 블록 안에서
# ``<parameter=path>…</path>`` 로 닫아, 종전 strict 미닫힘-복구가
# ``</path>`` 를 값에 포함시킴 → 오염된 경로로 실행(ENOENT ×3).
# lenient 경로(아무-이름 closer)엔 있던 처리의 strict 대칭.
_PARAM_CLOSED = re.compile(
    r"<parameter=([\w.\-]+)>(.*?)</(?:parameter|\1)>\s*"
    r"(?=<parameter=|</function>|</tool_call>|<function=|<tool_call>|\Z)",
    re.S | re.I,
)

# thought 산문에 흘린 구조 센티널 라인 (sanitize — prior 재주입 방지).
_SENTINEL_LINE = re.compile(
    r"^\s*</?(?:tool_call|function(?:=[\w.\-]*)?|parameter(?:=[\w.\-]*)?)>\s*$",
    re.MULTILINE | re.I,
)

# format runaway: 빈 <tool_call> 골격 반복 (json_fc 헤더-반복 검사 동형,
# ≥2 임계 철학 동일).
_DEGEN_EMPTY_BLOCK = re.compile(r"<tool_call>\s*</tool_call>", re.I)
_DEGEN_OPEN_RUN = re.compile(r"<tool_call>(?=\s*<tool_call>)", re.I)

# JSON parse 를 시도할 스키마 타입 — string/미선언은 raw 유지.
_COERCE_TYPES = frozenset({"integer", "number", "boolean", "array", "object"})

# ── lenient 구제 (tool-name 태그 변종) ───────────────────────
# 2026-07-17 bakeoff 실측: Qwen3.6-35B-A3B 가 `<function=X>` 를 `<X>` 로,
# `<parameter=k>` 를 `<k>` 로 붕괴시키는 변종이 0-op(NO_ACTION) 마찰의
# 83% (30건 캡처 중 25). strict 경로가 0-op 일 때만 발화하는 최후 폴백 —
# 캐노니컬 emission 은 절대 이 경로에 안 들어온다 (bail-safe). 구제 턴은
# parse_stage=2(drift) 로 계수되고, prior 는 캐노니컬 shape 로 재렌더되어
# (B→C) 다음 턴부터 모델을 교정한다.

# lenient 파라미터 오픈: `<parameter=k>` 또는 plain `<k>`. 닫는 태그는
# 아무 이름이나 수용 (실측: `<parameter=line_start>1</line_start>` 처럼
# 키-이름으로 닫는 혼합 스타일 존재).
_LENIENT_PARAM_OPEN = re.compile(r"<(?:parameter=)?([\w.\-]+)>")
_LENIENT_VALUE_STOP = re.compile(r"</[^>]*>|<(?:parameter=)?[\w.\-]+>")


def _lenient_tool_open_re() -> re.Pattern:
    """라인-단독 ``<TOOLNAME>`` 오픈 — 등록 도구명만 (매 파스 재조립: MCP
    도구가 런타임에 등록될 수 있다). 라인-앵커가 산문 속 인라인 언급
    (``use the <shell> tool``)의 오인 구제를 차단한다."""
    from agent_cli.tools.registry import TOOLS

    names = "|".join(re.escape(n) for n in sorted(TOOLS, key=len, reverse=True))
    return re.compile(rf"^[ \t]*<({names})>[ \t]*\r?$", re.MULTILINE)


def _extract_params_lenient(segment: str) -> dict:
    """혼합 스타일 파라미터 추출 — plain ``<k>v</k>``, canonical
    ``<parameter=k>v</parameter>``, 키-이름 closer ``<parameter=k>v</k>``
    전부 수용. 값의 끝 = 다음 닫는 태그(이름 무관) / 다음 오픈(closer
    생략 변종) / 세그먼트 끝."""
    params: dict = {}
    pos = 0
    while True:
        m = _LENIENT_PARAM_OPEN.search(segment, pos)
        if m is None:
            break
        stop = _LENIENT_VALUE_STOP.search(segment, m.end())
        if stop is None:
            value, pos = segment[m.end() :], len(segment)
        elif stop.group(0).startswith("</"):
            value, pos = segment[m.end() : stop.start()], stop.end()
        else:
            # closer 생략 — 다음 오픈 직전까지가 값
            value, pos = segment[m.end() : stop.start()], stop.start()
        params[m.group(1)] = _trim_block(value.rstrip())
    return params


def _trim_block(value: str) -> str:
    """블록 스타일 허용: 여는 태그 직후·닫는 태그 직전 개행 1개만 트림.

    내부 공백/개행은 보존 — raw 값 계약. (render 측이 멀티라인 값을
    블록 스타일로 쓰므로 이 트림과 대칭 = round-trip 보존.)
    """
    if value.startswith("\n"):
        value = value[1:]
    if value.endswith("\n"):
        value = value[:-1]
    return value


def _coerce_params(action: str, params: dict) -> dict:
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


def _extract_params(segment: str) -> tuple[dict, bool]:
    """한 function 블록 본문에서 파라미터 추출 — ``(params, truncated)``.

    1) 닫힌 파라미터(lookahead 앵커) 전부, 2) 그 뒤 남은 미닫힘 open 들은
    다음 구조 토큰/EOF 까지를 값으로 회수(truncation·closer 생략 변종).
    """
    params: dict = {}
    truncated = False
    covered = 0
    for pm in _PARAM_CLOSED.finditer(segment):
        params[pm.group(1)] = _trim_block(pm.group(2))
        covered = pm.end()
    tail = segment[covered:]
    while True:
        om = _PARAM_OPEN.search(tail)
        if om is None:
            break
        rest = tail[om.end() :]
        stop = re.search(
            r"<parameter=|</function>|</tool_call>|<function=|<tool_call>",
            rest,
            re.I,
        )
        raw = rest[: stop.start()] if stop else rest
        params[om.group(1)] = _trim_block(raw.rstrip())
        truncated = True
        tail = rest[stop.start() :] if stop else ""
    return params, truncated


class XmlFcFormat(WireFormat):
    """태그-파라미터 function-call wire format (멀티-op)."""

    name = "xml_fc"
    thought_required = False
    action_required = False
    multi_op = True
    # exposes_complete 기본 True 상속 — 종료는 명시적 complete op.

    # ─── Provider hints ─────────────────────────────────────────

    def provider_call_kwargs(self, capabilities=None) -> dict:
        # JSON-object 모드는 선두 `{` 를 강제해 태그 envelope 이 불가능
        # (json_fc 의 markdown 과 같은 이유) — capability 무관 항상 끔.
        return {"json_mode": False}

    # ─── Prompt ─────────────────────────────────────────────────

    def format_rules(self) -> str:
        return _FORMAT_RULES

    def format_rules_anchor(self) -> str:
        return (
            "Write brief reasoning as plain prose, then emit one or more "
            "<tool_call> blocks (finish with a `complete` call)."
        )

    def format_rules_field_specific(self) -> str:
        return (
            "1. Optional reasoning is plain prose BEFORE the first <tool_call>.\n"
            "2. Each <tool_call> wraps one <function=NAME> with "
            "<parameter=KEY>value</parameter> lines."
        )

    def render_action_input(self, action_input) -> str:
        # 가이드는 wire-key prefixed dict 를 넘긴다 — flat 태그 콜로
        # (json_fc 의 un-prefix 스캔과 동형 계약, 구현은 self-contained).
        if isinstance(action_input, dict) and action_input:
            from agent_cli.tools.registry import TOOLS

            for tool_name in sorted(TOOLS, key=len, reverse=True):
                pfx = tool_name + "_"
                if all(k.startswith(pfx) for k in action_input):
                    flat = {k[len(pfx) :]: v for k, v in action_input.items()}
                    return _render_call(tool_name, flat)
        if isinstance(action_input, dict):
            return _render_params(action_input)
        return _render_params({"result": action_input})

    def render_full_example(self, *, thought, action: str, action_input: str) -> str:
        th = thought if thought is not None else "your reasoning"
        # action_input 이 이미 완성된 <function=…> 콜이면 그대로, 파라미터
        # 라인들만이면 action 으로 감싼다 (json_fc 의 action 스플라이스 동형).
        if "<function=" in action_input:
            call = action_input
        else:
            body = f"\n{action_input}" if action_input else ""
            call = f"<function={action}>{body}\n</function>"
        return f"{th}\n\n<tool_call>\n{call}\n</tool_call>"

    # ─── Parsing ────────────────────────────────────────────────

    def parse_turn(self, llm_text: str) -> ParsedTurn:
        # Stage 0 — thinking 격리 (D4 의 전제): think 산문이 <tool_call>
        # 유사 텍스트를 담아도 구조 파서가 오인하지 않게 먼저 벗긴다.
        text, thinking = self.strip_thinking(llm_text)

        first = _FIRST_STRUCT.search(text)
        if first is None:
            # 캐노니컬 구조 토큰 부재 — tool-name 태그 변종 lenient 구제 시도.
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

        thought = self.sanitize_thought(text[: first.start()]) or None
        body = text[first.start() :]
        ops, drifted, truncated_any = self._extract_ops(body)

        if not ops:
            # 구조 토큰은 있는데 쓸 수 있는 op 이 없음 — 래퍼 안에 tool-name
            # 태그 변종이 든 경우일 수 있으니 lenient 를 먼저 시도하고,
            # 그래도 없으면 thought 유무로 0-op 유효 턴 / 실패 분기.
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
        """tool-name 태그 변종 구제 — ``(None = 해당 shape 아님)``.

        strict 0-op 확정 후에만 호출되는 최후 폴백. 라인-단독 ``<TOOL>``
        (등록 도구명) 오픈들을 op 경계로, 혼합 스타일 파라미터를 수용해
        parse_stage=2 턴을 재조립한다. 파라미터가 하나도 없어도 op 는
        만든다 ({} — A5 가 required 누락을 도구-정밀 진단, generic
        NO_ACTION 루프보다 낫다).
        """
        opens = list(_lenient_tool_open_re().finditer(text))
        if not opens:
            return None
        thought = self.sanitize_thought(text[: opens[0].start()]) or None
        ops: list[Op] = []
        for i, m in enumerate(opens):
            seg_end = opens[i + 1].start() if i + 1 < len(opens) else len(text)
            segment = text[m.end() : seg_end]
            name = m.group(1)
            close_m = re.search(rf"</{re.escape(name)}>", segment, re.I)
            if close_m is not None:
                segment = segment[: close_m.start()]
            params = _coerce_params(name, _extract_params_lenient(segment))
            ops.append(Op(action=name, action_input=params, truncated=False))
        return ParsedTurn(
            thought=thought, ops=ops, terminal=False, raw=text, parse_stage=2
        )

    def _extract_ops(self, body: str) -> tuple[list[Op], bool, bool]:
        """``(ops, drifted, truncated_any)`` — function 블록 순회 추출.

        래퍼(``<tool_call>``) 는 파싱에 필수가 아니다: bare ``<function=``
        도 수용하되 drift(stage 2 신호)로 계수. 미닫힘 태그(EOF 트렁케이션)
        도 drift.
        """
        func_opens = list(_FUNC_OPEN.finditer(body))
        ops: list[Op] = []
        truncated_any = False
        for i, fm in enumerate(func_opens):
            seg_end = (
                func_opens[i + 1].start() if i + 1 < len(func_opens) else len(body)
            )
            segment = body[fm.end() : seg_end]
            params, trunc = _extract_params(segment)
            hybrid = False
            if not params:
                # 하이브리드 변종: 캐노니컬 <function=X> 안에 plain-tag
                # 파라미터(<path>aaa</path>). strict 는 <parameter= 만 알아
                # 조용히 유실됐었다(stage 1 "클린" — drift 신호도 없이).
                # strict 가 0개일 때만 lenient 폴백 — 값 안의 태그-유사
                # 텍스트(raw content) 오인은 strict 성공 케이스에선 원천
                # 차단되고, 폴백 범위도 </function> 앞으로 한정.
                inner = segment
                close_m = _FUNC_CLOSE.search(inner)
                if close_m is not None:
                    inner = inner[: close_m.start()]
                params = _extract_params_lenient(inner)
                hybrid = bool(params)
            name = fm.group(1).strip() or None
            if name is None and not params:
                continue  # 이름도 파라미터도 없는 빈 골격
            if name is not None:
                params = _coerce_params(name, params)
            # parse 불변식: name 무효여도 식별된 파라미터는 보존 —
            # infer_action / NO_ACTION echo 의 재료.
            ops.append(Op(action=name, action_input=params, truncated=trunc))
            truncated_any |= trunc or hybrid  # hybrid 회수도 drift 신호(stage 2)

        n_func = len(func_opens)
        n_tc_open = len(_TOOL_CALL_OPEN.findall(body))
        n_tc_close = len(_TOOL_CALL_CLOSE.findall(body))
        n_fn_close = len(_FUNC_CLOSE.findall(body))
        drifted = (
            n_tc_open != n_func  # 래퍼 생략(bare) 또는 빈 래퍼
            or n_tc_open != n_tc_close  # </tool_call> 누락
            or n_fn_close != n_func  # </function> 누락
        )
        return ops, drifted, truncated_any

    def parse(self, llm_text: str) -> ParsedAction:
        """Singular projection of :meth:`parse_turn` (first op) — ABC 계약용.

        history 직렬화는 아래 override 가 멀티-op 레코드로 처리하므로 이
        경로는 generic 단수 소비자 전용 (json_fc 동형).
        """
        t = self.parse_turn(llm_text)
        first = t.ops[0] if t.ops else None
        return ParsedAction(
            thought=t.thought,
            action=first.action if first else None,
            action_input=first.action_input if first else None,
            raw=t.raw,
            parse_stage=t.parse_stage,
            thinking=t.thinking,
            truncated=first.truncated if first else False,
        )

    def is_degenerate(self, text: str) -> bool:
        # 빈 <tool_call> 골격 반복 = format runaway (≥2 임계 — json_fc 동형).
        hits = len(_DEGEN_EMPTY_BLOCK.findall(text)) + len(
            _DEGEN_OPEN_RUN.findall(text)
        )
        return hits >= 2

    def sanitize_thought(self, thought: str | None) -> str | None:
        if not thought:
            return thought
        thought = ORPHAN_THINK_TAG_RE.sub("", thought)
        return _SENTINEL_LINE.sub("", thought).strip()

    # ─── History round-trip (multi-op record) ───────────────────
    # 레코드 shape 은 json_fc 와 동일한 ops 계약 (cross-format parity
    # 테스트로 고정) — iter_record_ops 등 shape-공용 reader 호환.

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
        return {
            "role": "assistant",
            "content": self.sanitize_thought(raw_text) or "",
        }

    def serialize_terminal_for_history(self, thought: str, result: str) -> dict:
        return {
            "role": "assistant",
            "thought": thought or "",
            "ops": [{"action": "complete", "action_input": {"result": result}}],
        }

    def render_assistant_from_history(self, record: dict) -> dict:
        ops = record.get("ops")
        if isinstance(ops, list) and ops:
            calls = "\n".join(
                "<tool_call>\n"
                + _render_call(o.get("action") or "", o.get("action_input") or {})
                + "\n</tool_call>"
                for o in ops
                if isinstance(o, dict)
            )
            thought = record.get("thought", "")
            content = f"{thought}\n\n{calls}" if thought else calls
            return {"role": "assistant", "content": content}
        # Legacy / 단수 레코드 (타 포맷이 쓴 세션의 포맷-전환 resume 등) —
        # base round-trip 폴백 (render_full_example 경유).
        return super().render_assistant_from_history(record)

    # ─── Recovery wording ───────────────────────────────────────

    def constraint_reminder_call(self) -> str:
        return (
            "Respond with one or more <tool_call> blocks, each containing "
            "<function=TOOL> with <parameter=NAME>value</parameter> lines. "
            "To finish, call <function=complete> with a result parameter."
        )

    def constraint_reminder_action_required(self) -> str:
        return (
            "Each <function=...> must name one tool from Available Tools. "
            "If the task is DONE, finish with <function=complete> and a "
            "<parameter=result>your final answer</parameter> — do not stop "
            "without it."
        )

    def failure_framing_parse_fail(self) -> str:
        return (
            "Your response contained no parseable <tool_call> block — "
            "tool calls must use <function=...> / <parameter=...> tags."
        )

    def failure_framing_no_action(self) -> str:
        return "Your tool call names no tool (empty or invalid <function=> tag)."

    def static_retry_hint_no_json(self) -> str:
        return f"{self.failure_framing_parse_fail()} {self.constraint_reminder_call()}"

    def static_retry_hint_no_action(self) -> str:
        return (
            f"{self.failure_framing_no_action()} "
            f"{self.constraint_reminder_action_required()}"
        )

    def system_user_prefixes(self) -> tuple[str, ...]:
        return (
            "Your response contained no parseable <tool_call> block",
            "Your tool call names no tool",
        )


def _fmt_value(value) -> str:
    """파라미터 값 렌더 — str 은 raw 그대로, 그 외는 JSON 인라인 (parse 의
    스키마-주도 강제와 대칭 = round-trip)."""
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False)


def _render_params(params: dict) -> str:
    lines = []
    for k, v in params.items():
        text = _fmt_value(v)
        if "\n" in text:
            # 멀티라인 값은 블록 스타일 — 파서의 개행-1-트림과 대칭.
            lines.append(f"<parameter={k}>\n{text}\n</parameter>")
        else:
            lines.append(f"<parameter={k}>{text}</parameter>")
    return "\n".join(lines)


def _render_call(name: str, params: dict) -> str:
    body = _render_params(params)
    inner = f"\n{body}" if body else ""
    return f"<function={name}>{inner}\n</function>"


_FORMAT_RULES = """\
## Response Format

Write brief reasoning as plain prose, then emit your tool calls:

Your reasoning goes here, as plain prose. No tags around it.

<tool_call>
<function=TOOL_NAME>
<parameter=PARAM_NAME>value</parameter>
</function>
</tool_call>

Parameter values are RAW text: write file contents, code, and multi-line
text directly between the tags with NO escaping — no \\n, no quote
escaping, no JSON. For a multi-line value put it on its own lines:
<parameter=content>
line one
line two
</parameter>
Use the parameter names shown in each tool's guide above (plain, no
prefix).

Batch independent work into ONE turn. Before you emit, look at everything
you intend to do: every operation that does NOT need another's output goes
in THIS turn as a separate <tool_call> block. Reading three files, or a
read plus an unrelated search, is ONE turn — not three; batching saves
turns and context budget. Split into separate turns ONLY when a later step
needs an earlier step's result (then emit just the first now — its
observation arrives next turn).

Rules:
1. Every turn must contain at least one <tool_call> block (work, or
   `complete` to finish).
2. Each <function=...> names exactly one tool from Available Tools.
3. Each call acts on ONE target. To read N files, emit N separate
   <tool_call> blocks in the SAME turn — never a list inside one call.
4. Close every tag: </parameter>, </function>, </tool_call>.
5. When the task is DONE, end with a `complete` call carrying your final
   answer:
<tool_call>
<function=complete>
<parameter=result>
your final answer
</parameter>
</function>
</tool_call>
   Always finish this way — do NOT just stop.
6. If an observation shows an error, fix parameters and retry.
7. Respond in the user's language.

Several independent operations in one turn (read two files at once — they
don't depend on each other):
To see how auth and session fit together I need both files; neither
depends on the other, so read them together.

<tool_call>
<function=read_file>
<parameter=path>src/auth.py</parameter>
</function>
</tool_call>
<tool_call>
<function=read_file>
<parameter=path>src/session.py</parameter>
</function>
</tool_call>

Finishing the task:
The login() function is implemented and the tests pass.

<tool_call>
<function=complete>
<parameter=result>
Implemented login() in src/auth.py; all tests pass.
</parameter>
</function>
</tool_call>"""
