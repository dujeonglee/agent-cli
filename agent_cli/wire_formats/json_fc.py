"""json_fc wire format — 산문 thought + flat op JSON 배열 (multi-op).

md_array 의 후계 (multi-wire-format PHASE4 — v6.0.0 리네임+리셰이프):
마크다운 envelope(`## Thought`/`## Action`)를 제거하고 xml_fc 의 D4 와
동형인 "산문 + 구조 블록" shape 로 통일했다. shape::

    read auth.py and list src/ — independent, so one turn.

    [{"action": "read_file", "path": "src/auth.py"},
     {"action": "shell", "command": "ls src/"}]

- **thought = 배열 앞 자유 산문** (선택). 헤더-runaway 실패 클래스
  (`## Thought` 반복 등)가 shape 차원에서 소멸.
- **body = flat ``{action, ...params}`` op 들의 bare JSON 배열** —
  md_array body 와 동일 (D7 결정: ``{"tool_call": […]}`` 래퍼 기각 —
  중첩 민감성 실측 + 검증된 body 승계). bare 객체 = 1-op 관용.
  op 하나 = 대상 하나 (배치 중첩 금지 — 27B 90% 파괴 실측).
- **종료 = 명시적 ``complete`` op 만** (v8.4.0). v7.14 의 산문-only 암묵
  완료(``prose_completion``)는 프로덕션 반례("Now let me write the plan
  document:" 를 스킬 결과로 수용 — bakeoff 가 0건으로 측정했던 중간 서술
  부류)로 제거. action-less 턴은 예외 없이 NO_ACTION 넛지 — 넛지 문구
  (``constraint_reminder_action_required``)가 산문 답변의 complete 재방출
  경로를 직접 안내한다.
- **legacy 관용**: 구 md_array 헤더 emission 은 stage 2(drift) 로 계속
  수용 — 전환기 모델 습관 + foreign 누출 실측 shape. prior 는 캐노니컬
  (헤더 없는) shape 로 재렌더 (B→C 자기 교정).
- JSON 수리 기계(_extract_op_json 이하)는 md_array 의 실전 검증분을
  그대로 승계 — body 가 동일하므로 무변경.
"""

from __future__ import annotations

import json
import re

from agent_cli.thinking_tags import (
    ORPHAN_THINK_TAG_RE as _ORPHAN_THINK_TAG,
)
from agent_cli.wire_formats.base import (
    NO_ACTION_FRAMING,
    Op,
    ParsedAction,
    ParsedTurn,
    WireFormat,
    _terminal_input,
)
from agent_cli.wire_formats.recovery.json import (
    _extract_op_json,
    _op_anchor,
    close_unbalanced,
    describe_json_error,
)

_THOUGHT_RE = re.compile(r"^##\s*Thought\s*$", re.MULTILINE)
_ACTION_RE = re.compile(r"^##\s*Action\s*$", re.MULTILINE)

# Lone wire-sentinel lines leaked into a thought (same self-reinforcement
# risk as prefix_md: raw riding back into the prior re-teaches the runaway).
# ``Input`` is included although it is not part of THIS format — it is the
# models' prefix_md prior leaking through (observed in Phase-2).
_SENTINEL_LINE = re.compile(r"^\s*##\s*(?:Thought|Action|Input)\s*$", re.MULTILINE)

# Orphan thinking/reasoning tags a thinking-trained model leaks into the
# visible thought — most often a lone closing ``</thinking>`` whose opener was
# consumed by the provider's reasoning channel (observed dominating NO_JSON
# co-occurrence on Qwen3.6, session 1782027249). md_array splits on ``##``
# headers, never on these tags, so they ride into the thought as cosmetic
# noise (and into the next-turn prior). Drop the bare tags, keep the text.
#
# Same leak, but TRAILING the op array (``[{...}</think>``): the closer that
# repairs an unclosed array appends ``]`` at the very end — AFTER the tag — so
# the result stays invalid (``[{...}</think>]``) and the op is lost → NO_JSON
# retry loop (observed hang on Qwen3.6). A think tag past the array is never
# part of the JSON, so strip trailing ones before the repair path. Anchored to
# the end → never touches a ``<think>`` inside a string value (valid JSON parses
# strictly first and is returned untouched).
#
# 정규식은 thinking_tags 단일 소스에서 (모듈 상단 import — vocab 드리프트
# 방지, 선행 리팩토링); **적용 지점**(sanitize_thought / repair 파이프라인
# 직전)은 이 포맷 소유 그대로.

# Format runaway: an empty envelope section immediately followed by another
# header (mirrors prefix_md's _DEGEN_RUNAWAY; Input included for the same
# prior-leak reason as _SENTINEL_LINE).
_DEGEN_RUNAWAY = re.compile(
    r"##\s*(?:Thought|Action|Input)(?=\s*##\s*(?:Thought|Action|Input))"
)

# Stray `## Input` header inside the ## Action body — the models' prefix_md
# prior resurfaces (Phase-2). Stripped so a body that was ONLY that residue
# parses cleanly (→ NO_ACTION nudge, not a spurious NO_JSON).
_INPUT_RESIDUE = re.compile(r"^\s*##\s*Input\s*$", re.MULTILINE)

_FORMAT_RULES = """\
## Response Format

Write brief reasoning as plain prose, then end your turn with ONE JSON
array of tool calls:

Your reasoning goes here, as plain prose.

[{"action": "<tool name>", <its parameters>}]

Each array element is one tool call: {"action": ..., params}. Use the
parameter names shown in each tool's guide above (plain, no prefix).

Batch independent work into ONE turn. Before you emit, look at everything
you intend to do: every operation that does NOT need another's output goes
in THIS turn as a separate array element. Reading three files, or a read
plus an unrelated search, is ONE turn — not three; batching saves turns and
context budget. Split into separate turns ONLY when a later step needs an
earlier step's result (then emit just the first now — its observation
arrives next turn).

Rules:
1. Reasoning (optional) is plain prose BEFORE the array — never after it.
2. Every turn must END with one JSON array containing at least one op
   (work, or `complete` to finish). Do NOT just stop after prose.
3. Each op must have an "action" naming one tool.
4. Each op acts on ONE target. To read N files, emit N separate
   {"action": "read_file", "path": ...} ops in the SAME array. NEVER put a
   list of items inside a single op (no nested arrays).
5. When the task is DONE, end with a `complete` op carrying your final
   answer: {"action": "complete", "result": "<your final answer>"}.
6. NEVER use HTML/XML tags of ANY kind — no <tool_call>, <function_call>,
   <div>, <answer>, or anything tag-shaped — and NO markdown headers
   (## ...). This protocol is plain prose + ONE JSON array, NOTHING else.
   A turn containing tags or headers is UNPARSEABLE and completely wasted —
   if you feel the urge to open a tag, write the JSON array instead.
7. If an observation shows an error, fix parameters and retry.
8. Respond in the user's language.

Several independent operations in one turn (read three files at once —
they don't depend on each other):
To see how auth, session, and the login route fit together I need all
three files; none depends on another's output, so read them together.

[{"action": "read_file", "path": "src/auth.py"}, {"action": "read_file", "path": "src/session.py"}, {"action": "read_file", "path": "src/routes/login.py"}]

Finishing the task:
The login() function is implemented and the tests pass.

[{"action": "complete", "result": "Implemented login() in src/auth.py; all tests pass."}]"""


def _split_sections(text: str) -> tuple[str | None, str | None, bool]:
    """Return ``(thought, action_body, has_action_header)``.

    ``thought`` is the ``## Thought`` body — or, when ``## Action`` is present
    but ``## Thought`` is not, the leading text before ``## Action`` (the model
    emitted its reasoning without the header — recover it as the thought rather
    than drop it; also salvages a mistyped Thought header). With no headers at
    all, the whole text (a header-less terminal answer). ``action_body`` is the
    text after ``## Action`` (None when the header is absent).
    """
    tm = _THOUGHT_RE.search(text)
    am = _ACTION_RE.search(text)
    thought: str | None = None
    if tm:
        end = am.start() if (am and am.start() > tm.end()) else len(text)
        thought = text[tm.end() : end].strip()
    elif am:
        # `## Action` present, `## Thought` absent → leading prose is the thought.
        thought = text[: am.start()].strip() or None
    else:
        thought = text.strip()  # plain text, no headers → the answer
    if not am:
        return thought, None, False
    return thought, text[am.end() :].strip(), True


class JsonFcFormat(WireFormat):
    """산문 thought + flat action-array (multi-op, complete 종결)."""

    name = "json_fc"
    action_required = False
    multi_op = True
    # 미닫힘 <think> 뒤의 bare 배열(라인 선두 `[`)을 EOF-삼킴에서 보호.
    thinking_stop = re.compile(r"(?m)^\s*\[")
    # exposes_complete 기본 True 상속 — 종료는 명시적 `complete` op.

    # ─── Prompt ─────────────────────────────────────────────────

    def format_rules(self) -> str:
        return _FORMAT_RULES

    def render_action_input(self, action_input: dict) -> str:
        # Guides hand in a wire-key-prefixed dict (`_rai_prefixed`); render it
        # as this format's flat op: {"action": tool, plain params}.
        if isinstance(action_input, dict) and action_input:
            from agent_cli.tools.registry import TOOLS

            for tool_name in sorted(TOOLS, key=len, reverse=True):
                pfx = tool_name + "_"
                if all(k.startswith(pfx) for k in action_input):
                    flat = {k[len(pfx) :]: v for k, v in action_input.items()}
                    return json.dumps({"action": tool_name, **flat}, ensure_ascii=False)
        return json.dumps(action_input, ensure_ascii=False)

    def grammar(self, tools, *, thinking_open: bool = False) -> str | None:
        """prose → one bare JSON array of ``{"action": <name>, <params>}`` ops,
        a terminal op (``complete``) only last.

        Tool and key names are enumerated (an unknown tool or key cannot be
        emitted); values follow their declared JSON type; required-ness and
        duplicates are left to the validator (enforcing required keys in
        arbitrary order is combinatorial for no gain). Prose may not contain
        ``[`` so the first ``[`` is the array — and is bounded, which is the
        whole point against reasoning runaways."""
        from agent_cli.wire_formats.grammar import (
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

        pre, pre_rules = think_prefix(thinking_open)
        # EBNF literals: ``\n`` and ``\"`` are written as the two-character
        # escapes the grammar dialect reads (raw strings below), never as the
        # Python characters — a raw newline inside an EBNF ``"…"`` is invalid.
        # root: 배열로 바로 시작 | 산문 + 빈 줄 + 배열 | 산문만. 호출은 선택
        # (필수로 하면 EOS 가 마스킹돼 폭주·가짜 호출). 빈 배열도 허용 —
        # `[` 를 연 순간 op 하나를 지어내게 강제하지 않는다(NO_ACTION 넛지가
        # 구제). 산문 뒤 개행 하나짜리 `\n[` 는 산문으로 흡수된다(제약이 그
        # 턴에서 꺼질 뿐 파서는 읽는다) — 줄 첫 `[` 를 산문에서 막는 대가보다
        # 낫다.
        lines = [
            rf'root ::= {pre}( ops | prose "\n\n" ops | prose )',
            prose_rule("prose", "[", after_blank_line=True),
            # 종결 도구(complete·run_skill)는 배열의 마지막 op 로만 — 뒤엔
            # `]` 와 EOS 뿐이다(v9.24.6, grammar.call_sequence_rules).
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
            # 열거값·비어있지 않음·강제 존재(v9.24.4)는 공용 조각이 정하고,
            # 여기는 이 형식에서 인자 하나를 쓰는 모양(`"k": value`)만 준다.
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

    def render_full_example(self, *, thought, action: str, action_input: str) -> str:
        th = thought if thought is not None else "your reasoning"
        # ``action_input`` is already this format's flat op JSON (via
        # render_action_input); wrap as a one-element array, splicing the
        # action in for standard-key inputs (md_array 동형).
        op = action_input
        if '"action"' not in op:
            try:
                obj = json.loads(op)
                if isinstance(obj, dict):
                    op = json.dumps({"action": action, **obj}, ensure_ascii=False)
            except json.JSONDecodeError:
                pass
        return f"{th}\n\n[{op}]"

    # ─── Parsing ────────────────────────────────────────────────

    def parse_turn(self, llm_text: str) -> ParsedTurn:
        # Stage 0 — thinking 격리 (provider 미경유 경로의 유일 방어).
        llm_text, thinking = self.strip_thinking(llm_text)
        turn = self._parse_turn_stripped(llm_text)
        turn.thinking = thinking
        return turn

    def _parse_turn_stripped(self, llm_text: str) -> ParsedTurn:
        def _ops(items, truncated_last: bool = False) -> list:
            # P0-3: ``truncated_last`` — 본문이 EOF 절단 증거(미닫힘 괄호를
            # close_unbalanced 로 복구)를 보였으면 **마지막 op 에만** truncated
            # 를 표시한다. 절단은 EOF 에서 일어나므로 앞선 op 들은 온전하고,
            # 전 op 에 걸면 dispatch 의 절단 새니타이저가 멀쩡한 edit 의 마지막
            # 줄까지 깎는다(과잉 수리). 종전엔 json_fc 가 이 플래그를 아예 안
            # 세워 기본 포맷에서 새니타이저가 상시 무발화였다(xml_fc 만 전파).
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

        # ── legacy 관용: 구 md_array 헤더 (`## Action`) — 전환기 모델
        # 습관 + foreign 누출 실측 shape. 성공해도 **stage 2** (drift 신호);
        # prior 는 캐노니컬(헤더 없는) shape 로 재렌더 → 자기 교정.
        thought_h, body_h, has_action = _split_sections(llm_text)
        if has_action:
            clean_thought = self.sanitize_thought(thought_h)
            body = _INPUT_RESIDUE.sub("", body_h or "").strip()
            if body:
                parsed, _repaired = _extract_op_json(body)
                if parsed is not None:
                    arr = parsed if isinstance(parsed, list) else [parsed]
                    items = [x for x in arr if isinstance(x, dict)]
                    if items and not all(not it for it in items):
                        return ParsedTurn(
                            thought=clean_thought,
                            ops=_ops(
                                items,
                                truncated_last=_repaired and close_unbalanced(body)[1],
                            ),
                            raw=llm_text,
                            parse_stage=2,
                        )
            if body:
                # 헤더로 action body 를 선언했는데 수리 기계까지 실패한
                # 진짜 파손 — NO_JSON 진단 대상 (구 md_array 의미 유지).
                return ParsedTurn(thought=clean_thought, raw=llm_text, parse_stage=0)
            if clean_thought and clean_thought.strip():
                return ParsedTurn(
                    thought=clean_thought, ops=[], raw=llm_text, parse_stage=2
                )
            return ParsedTurn(raw=llm_text, parse_stage=0)

        # ── 캐노니컬: 산문 + op JSON 배열/객체 (bare 객체 = 1-op 관용)
        #
        # 앵커는 `_op_anchor` 가 고른다 — 옛 코드는 **본문 첫 `[`/`{`** 를 op
        # 시작으로 못박아, 산문이 코드 얘기를 하면(`board[i][j]`, `- [ ]`,
        # `{ passive: false }`, `[문서](url)`) 그 괄호를 op 으로 집고 뒤의 진짜
        # 배열을 못 봤다 — stage 0(턴 낭비) 또는 산문 조각을 op 으로 채택
        # (action 없음 → 진짜 complete 유실). thought 도 그 괄호에서 잘렸다.
        start = _op_anchor(llm_text)
        if start >= 0:
            thought = self.sanitize_thought(llm_text[:start]) or None
            body = llm_text[start:]
            parsed, repaired = _extract_op_json(body)
            if parsed is not None:
                arr = parsed if isinstance(parsed, list) else [parsed]
                items = [x for x in arr if isinstance(x, dict)]
                stage = 2 if repaired else 1
                # P0-3: EOF 절단 증거 = "수리가 필요했고 + 본문 괄호가 EOF 에서
                # 미닫힘(close_unbalanced 가 changed)". 과닫힘(drop_closers)·
                # 제어문자·이스케이프 수리 같은 비-절단 수리는 changed=False 라
                # 플래그되지 않는다 — 마지막 op 만 truncated (아래 _ops 주석).
                trunc = repaired and close_unbalanced(body)[1]
                # `any("action")` 가드: 산문 속 `[1,2,3]` 같은 비-op 배열은
                # 통과시키지 않고 thought-only 로 (NO_ACTION 넛지).
                if any("action" in it for it in items):
                    return ParsedTurn(
                        thought=thought,
                        ops=_ops(items, truncated_last=trunc),
                        raw=llm_text,
                        parse_stage=stage,
                    )
                # actionless-op 보존 불변식 (ABC parse 계약): action 은 없지만
                # input 을 실은 dict-op 는 infer/NO_ACTION echo 의 재료로
                # 보존한다. 구 md_array 는 `## Action` 헤더가 "이건 action
                # body" 신호였는데, 캐노니컬은 **위치 신호**로 대체 — 배열이
                # emission 의 끝이면(rule 2: 턴은 배열로 끝난다) op 의도.
                # 산문 중간의 예시 dict-배열은 이 앵커에 안 걸린다.
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
                # op 시도 흔적("action" 키)이 있는 깨진 JSON — 수리 기계까지
                # 전부 실패한 진짜 파손. thought-only 로 삼키지 않고 stage 0
                # (NO_JSON 진단 + 캐럿이 위치를 짚음).
                return ParsedTurn(thought=thought, raw=llm_text, parse_stage=0)

        # ── thought-only / blank — 완료가 아니라 NO_ACTION 넛지 대상.
        thought = self.sanitize_thought(llm_text)
        if thought and thought.strip():
            return ParsedTurn(thought=thought, ops=[], raw=llm_text, parse_stage=1)
        return ParsedTurn(raw=llm_text, parse_stage=0)

    def parse(self, llm_text: str) -> ParsedAction:
        """Singular projection of :meth:`parse_turn` (first op) — ABC 계약용."""
        t = self.parse_turn(llm_text)
        first = t.ops[0] if t.ops else None
        return ParsedAction(
            thought=t.thought,
            action=first.action if first else None,
            action_input=first.action_input if first else None,
            raw=t.raw,
            parse_stage=t.parse_stage,
            thinking=t.thinking,
            truncated=first.truncated if first else False,  # P0-3 (xml_fc 동형)
        )

    def is_degenerate(self, text: str) -> bool:
        # legacy 헤더-반복 runaway 검출 유지 (구 프라이어 누출은 계속
        # 가능); 캐노니컬 shape 의 러너웨이 패턴은 실측 후 추가.
        return len(_DEGEN_RUNAWAY.findall(text)) >= 2

    def sanitize_thought(self, thought: str | None) -> str | None:
        if not thought:
            return thought
        thought = _ORPHAN_THINK_TAG.sub("", thought)
        return _SENTINEL_LINE.sub("", thought).strip()

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
        return {
            "role": "assistant",
            "content": self.sanitize_thought(raw_text) or "",
        }

    def serialize_terminal_for_history(
        self, thought: str, result: str, answers: list[str] | None = None
    ) -> dict:
        return {
            "role": "assistant",
            "thought": thought or "",
            "ops": [
                {"action": "complete", "action_input": _terminal_input(result, answers)}
            ],
        }

    def render_assistant_from_history(self, record: dict) -> dict:
        ops = record.get("ops")
        if isinstance(ops, list) and ops:
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
        # Legacy / singular-shaped records — base round-trip 폴백.
        return super().render_assistant_from_history(record)

    # ─── Recovery wording ───────────────────────────────────────

    def constraint_reminder_call(self) -> str:
        return (
            "Respond with plain-prose reasoning followed by ONE JSON array "
            'of {"action": ..., params} ops. To finish, use a `complete` op: '
            '{"action": "complete", "result": "<final answer>"}.'
        )

    def constraint_reminder_action_required(self) -> str:
        # "re-emit that answer" 문구 (v8.4.0): 산문-only 턴은 이제 절대 암묵
        # 완료되지 않으므로, 방금 산문이 최종답변이었던 모델이 한 턴 안에
        # 확실히 수렴하도록 경로를 직접 가리킨다.
        return (
            'Each array element must include an "action" field naming '
            "one tool from Available Tools. If the task is DONE, emit a "
            '`complete` op: {"action": "complete", "result": "<final answer>"}. '
            "If your last message already was the final answer in plain "
            "prose, re-emit that answer as the `result`. If you were about "
            "to do something, emit that tool call now. Never stop without "
            "an explicit `complete`."
        )

    def failure_framing_parse_fail(self) -> str:
        return (
            "Your response did not match the expected format — it must end "
            "with a valid JSON array of tool calls."
        )

    def no_action_detail(self) -> str:
        return (
            'no JSON array, or no op whose "action" names a tool from Available Tools'
        )

    def static_retry_hint_no_json(self) -> str:
        return (
            f"{self.failure_framing_parse_fail()} {self.constraint_reminder_call()} "
            "Plain prose then ONE JSON array — no markdown headers "
            "(## ...), no HTML/XML tags."
        )

    def static_retry_hint_no_action(self) -> str:
        return (
            f"{self.failure_framing_no_action()} "
            f"{self.constraint_reminder_action_required()}"
        )

    def diagnose_syntax_error(self, prior_content: str) -> str | None:
        # 헤더(legacy)가 있으면 그 body, 아니면 첫 브레이스부터가 JSON 후보.
        _, body, has = _split_sections(prior_content)
        if has and body:
            candidate = _INPUT_RESIDUE.sub("", body).strip()
        else:
            start = next((i for i, c in enumerate(prior_content) if c in "[{"), -1)
            candidate = prior_content[start:] if start >= 0 else prior_content
        return describe_json_error(candidate)

    def system_user_prefixes(self) -> tuple[str, ...]:
        return (
            "Your response did not match the expected format",
            NO_ACTION_FRAMING,
            "Your JSON array had no usable tool call",  # ≤ v9.24.2 문구 — 옛 세션 resume
        )
