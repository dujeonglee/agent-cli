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
- **종료 = 명시적 ``complete`` op** 가 canonical. 단 **순수 산문 최종답변**
  (액션 흔적 없는 자연어를 내고 complete op 만 누락)은 ``prose_completion``
  으로 그 산문을 complete result 로 수용(v7.14, 넛지 왕복 절약). 깨진 액션
  잔해(op-array/JSON 흔적)·빈 출력은 여전히 NO_ACTION 넛지 — 삼키면 의도한
  액션 조기종료(bakeoff 35B ~460런 실측: no_action 자연어는 전부 최종답변
  C, 중간서술 B=0). md_array 시절 thought-only **무조건** 종결의 false-
  terminate 교훈(DESIGN Exp 8)은 '깨진 액션은 넛지' 필터로 방어.
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
    TRAILING_THINK_TAG_RE as _TRAILING_THINK_TAG,
)
from agent_cli.wire_formats._json_diag import describe_json_error
from agent_cli.wire_formats._json_repair import (
    close_unbalanced,
    drop_unbalanced_closers,
    fix_invalid_escapes,
    repair_value_quotes,
)
from agent_cli.wire_formats.base import Op, ParsedAction, ParsedTurn, WireFormat

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

# prose_completion 필터 — thought 에 이 흔적이 있으면 '자연어 최종답변'이
# 아니라 파싱 실패한 액션 잔해(op array/JSON 객체)라 종결 대상에서 제외.
# 보수적: 조금이라도 있으면 산문 아님 → NO_ACTION 넛지 유지.
_JSON_ACTION_RESIDUE = re.compile(r'\[\s*\{|\{\s*"|"action"|action\s*=')

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


def _extract_first_json(body: str, *, strict: bool = True):
    """Parse the first balanced ``[...]`` or ``{...}`` from body, or None.

    ``strict`` is forwarded to ``json.loads``: with ``strict=False`` the parser
    accepts literal control characters (raw newlines/tabs) inside string values
    — a leniency :func:`_extract_op_json` uses as a last-resort repair (see
    there). The balanced-brace scan itself is unaffected (it already tracks
    strings), so only the final ``json.loads`` behaviour changes."""
    if body.startswith("```"):
        body = body.split("\n", 1)[1] if "\n" in body else body
        if body.rfind("```") > 0:
            body = body[: body.rfind("```")]
    opens = {"[": "]", "{": "}"}
    start = next((i for i, c in enumerate(body) if c in opens), -1)
    if start < 0:
        return None
    open_c, close_c = body[start], opens[body[start]]
    depth = 0
    in_str = False
    escape = False
    for i in range(start, len(body)):
        c = body[i]
        if in_str:
            if escape:
                escape = False
            elif c == "\\":
                escape = True
            elif c == '"':
                in_str = False
            continue
        if c == '"':
            in_str = True
        elif c == open_c:
            depth += 1
        elif c == close_c:
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(body[start : i + 1], strict=strict)
                except json.JSONDecodeError:
                    return None
    return None


def _merge_reopened_op_arrays(text: str) -> tuple[str, bool]:
    """Merge multiple top-level op-arrays the model emitted as SEPARATE arrays
    instead of one. Measured shape: a 27B write_file batch split across three
    lines, each its own ``[{...}`` (session 1783129061 — the model reopened the
    array per op rather than emitting one ``[op, op, op]``).

    The tell is a structural ``}`` (an op close) followed — outside any string —
    by an optional array close ``]``, whitespace, then ``[{`` (a new op-array
    open). In valid JSON a ``}`` is only ever followed by ``,`` ``]`` ``}``, so
    ``}`` … ``[{`` at object level is unambiguously a spurious re-open →
    rewrite the boundary to ``},{`` so the ops fold into one array. String-aware
    (braces/brackets inside a ``content`` value never trigger it), and a no-op
    when the pattern is absent (``changed=False``). Compose with
    :func:`close_unbalanced` for the array's own missing ``]``.
    """
    out: list[str] = []
    i = 0
    n = len(text)
    in_str = escape = False
    changed = False
    while i < n:
        ch = text[i]
        if in_str:
            out.append(ch)
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_str = False
            i += 1
            continue
        if ch == '"':
            in_str = True
            out.append(ch)
            i += 1
            continue
        if ch == "}":
            j = i + 1
            while j < n and text[j].isspace():
                j += 1
            if j < n and text[j] == "]":  # optional close of the prior array
                j += 1
                while j < n and text[j].isspace():
                    j += 1
            if j + 1 < n and text[j] == "[" and text[j + 1] == "{":
                out.append("},{")  # fold the re-opened array into this one
                i = j + 2
                changed = True
                continue
        out.append(ch)
        i += 1
    return ("".join(out), changed) if changed else (text, False)


def _repair_anonymous_op_objects(text: str, *, drop_close: bool) -> str:
    """Remove the spurious ``{`` the model inserts in object-KEY position when
    it wraps an op's params in an anonymous nested object (DESIGN Exp 8). Two
    shapes are seen, and the model is consistent within one emission:

      A. ``{"action": X, {params}}``  (anon AND op both close — 27B)
      B. ``{"action": X, {params}``   (one ``}`` — the model reuses the anon
         close AS the op close — 35B; the array then has N unbalanced ``{``)

    The repair removes the spurious ``{`` either way. ``drop_close`` switches
    the two:
      - ``True``  → variant A: the anon ``{`` opens a frame whose matching
        ``}`` is ALSO dropped, leaving the op's own ``}`` (``{X, params}``).
      - ``False`` → variant B: the anon ``{`` opens NO frame, so the single
        ``}`` that follows closes the op (``{X, params}``).
    The caller tries both and keeps whichever parses (``_extract_op_json``).

    Context- and string-aware single pass: only a ``{`` where an object key is
    expected (object start / right after a comma at object level) is treated as
    the bug; a ``{`` after ``:`` (a legit nested value) or inside an array
    element is left alone, and braces inside string literals (C code in a
    ``content`` value) never affect matching. No-op when the pattern is absent.
    """
    out: list[str] = []
    frames: list[dict] = []  # {"type": "obj"|"arr", "expect_key": bool, "drop": bool}
    in_str = escape = False
    for ch in text:
        if in_str:
            out.append(ch)
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
            if frames and frames[-1]["type"] == "obj":
                frames[-1]["expect_key"] = False  # the key/value string starts
            out.append(ch)
        elif ch == "{":
            top = frames[-1] if frames else None
            if top and top["type"] == "obj" and top["expect_key"]:
                # spurious anonymous-object open in key position → drop it.
                if drop_close:
                    # variant A: track it so its matching `}` is dropped too.
                    frames.append({"type": "obj", "expect_key": True, "drop": True})
                # variant B: push nothing — the op frame keeps absorbing the
                # params and its own `}` (the next one) closes it.
            else:
                frames.append({"type": "obj", "expect_key": True, "drop": False})
                out.append(ch)
        elif ch == "}":
            top = frames.pop() if frames else {"drop": False}
            if not top.get("drop"):
                out.append(ch)
            if frames and frames[-1]["type"] == "obj":
                frames[-1]["expect_key"] = False
        elif ch == "[":
            frames.append({"type": "arr", "expect_key": False, "drop": False})
            out.append(ch)
        elif ch == "]":
            if frames:
                frames.pop()
            if frames and frames[-1]["type"] == "obj":
                frames[-1]["expect_key"] = False
            out.append(ch)
        elif ch == ":":
            if frames and frames[-1]["type"] == "obj":
                frames[-1]["expect_key"] = False
            out.append(ch)
        elif ch == ",":
            if frames and frames[-1]["type"] == "obj":
                frames[-1]["expect_key"] = True
            out.append(ch)
        else:
            out.append(ch)
    return "".join(out)


def _op_count(parsed) -> int:
    """Number of dict-shaped ops in an extracted payload (bare object = 1)."""
    if isinstance(parsed, list):
        return sum(1 for x in parsed if isinstance(x, dict))
    return 1 if isinstance(parsed, dict) else 0


def _extract_op_json(text: str):
    """``_extract_first_json`` with an anonymous-nested-object repair fallback.

    Returns ``(parsed, repaired)`` — ``repaired`` is True iff the strict parse
    failed but the unwrap repair recovered it (parse_stage 2). Tries both op
    shapes — ``{"action":X, {params}}`` (A) and ``{"action":X, {params}`` (B) —
    and keeps whichever parses."""
    parsed = _extract_first_json(text)
    if parsed is not None:
        # The first array parsed cleanly — but the model may have split the
        # batch into several SEPARATE top-level arrays (`[{op1}] [{op2}]`), and
        # `_extract_first_json` stops at the first, silently dropping the rest
        # (measured happy-path op loss — no error, parse_stage would be 1). If
        # folding whitespace-adjacent re-opened arrays yields MORE ops, take the
        # merged parse as a drift recovery (parse_stage 2). Conservative: the
        # fold only fires on `}` …`[{` (never a valid single array's `},{`), and
        # only when it strictly gains ops — trailing `</think>`/prose never
        # matches, so that defense (first-array-wins) is preserved.
        merged, changed = _merge_reopened_op_arrays(text)
        if changed:
            remerged = _extract_first_json(merged)
            if remerged is not None and _op_count(remerged) > _op_count(parsed):
                return remerged, True
        return parsed, False
    # Strict parse failed: the JSON is broken. A reasoning model often leaks a
    # trailing ``</think>`` after the array, which defeats the unclosed-array
    # closer below (it appends ``]`` past the tag). Drop trailing think tags so
    # the repair path sees clean JSON. Done only AFTER the strict parse, so valid
    # JSON carrying ``<think>`` inside a string value is preserved.
    text = _TRAILING_THINK_TAG.sub("", text)
    for drop_close in (True, False):
        fixed = _repair_anonymous_op_objects(text, drop_close=drop_close)
        if fixed != text:
            # Compose with merge + close_unbalanced: the model often forgets BOTH
            # the params-wrapping AND the array's ``]`` — and sometimes splits the
            # batch into several separate ``[{...}`` arrays (measured — session
            # 1783129061, a 27B write_file batch: three lines of
            # `[{"action":X, {params}}` with no trailing `]`). The unwrap yields
            # `[{...}` per op (still unclosed / multi-array), so fold re-opened
            # arrays into one and append the missing closer before re-parsing.
            merged = _merge_reopened_op_arrays(fixed)[0]
            seen: set[str] = set()
            for base in (fixed, merged):
                for cand in (base, close_unbalanced(base)[0]):
                    if cand in seen:
                        continue
                    seen.add(cand)
                    for strict in (True, False):
                        parsed = _extract_first_json(cand, strict=strict)
                        if parsed is not None:
                            return parsed, True
    # Last resort: the model emitted literal control chars (raw newlines/tabs)
    # inside a string value — common in big `result`/`content` markdown blobs
    # written without `\n` escaping — which strict json.loads rejects
    # ("Invalid control character"). Re-parse leniently; a recovery, so
    # repaired=True keeps parse_stage 2 as the signal that JSON was non-strict.
    # Note: strict=False ONLY relaxes control chars — genuinely broken JSON
    # (missing value, bad brace) still returns None, so this never force-parses
    # garbage into bogus ops.
    parsed = _extract_first_json(text, strict=False)
    if parsed is not None:
        return parsed, True
    # Invalid escapes: the model under-escaped a regex ``\s`` / ``\d`` / ``\x`` /
    # ``\.`` (raw strings, char classes, Windows paths) → strict json.loads raises
    # "Invalid \escape" (the measured dominant backslash-heavy failure). Double
    # the lone backslashes and re-parse; compose with the unclosed-array closer so
    # a payload that is BOTH under-escaped AND missing its ``]`` still recovers.
    esc_fixed, changed = fix_invalid_escapes(text)
    if changed:
        parsed = _extract_first_json(esc_fixed, strict=False)
        if parsed is not None:
            return parsed, True
        closed, ch2 = close_unbalanced(esc_fixed)
        if ch2:
            parsed = _extract_first_json(closed, strict=False)
            if parsed is not None:
                return parsed, True
    # Last resort: a well-formed op array the model finished but forgot to
    # close (measured dominant NO_JSON shape — session 1781336790, a 6-op
    # read_file batch missing its trailing `]`). `_extract_first_json` returns
    # None on an unclosed array (depth never returns to 0), so close the
    # unbalanced brackets at EOF (string-aware, depth-stack → deterministic
    # closer) and re-parse (strict, then control-char-lenient). Accept only if
    # it now validates; a deeper break (truncated mid-op) keeps it None →
    # diagnostic+retry, never a forced bogus op.
    closed, changed = close_unbalanced(text)
    if changed:
        for strict in (True, False):
            parsed = _extract_first_json(closed, strict=strict)
            if parsed is not None:
                return parsed, True
    # Last resort: an OVER-closed payload — the model doubled an op's close
    # brace (`[{...}}]`, session 1783001191 — a 27B shell op emitted `}}]` →
    # NO_JSON). The mirror of close_unbalanced: drop the spurious closers
    # (string-aware, so content braces are safe) and re-parse, composed with
    # close_unbalanced so a payload BOTH over-closed early AND unclosed at EOF
    # still recovers. bail-if-invalid: a wrong drop falls through to retry.
    dropped, changed = drop_unbalanced_closers(text)
    if changed:
        for cand in (dropped, close_unbalanced(dropped)[0]):
            for strict in (True, False):
                parsed = _extract_first_json(cand, strict=strict)
                if parsed is not None:
                    return parsed, True
    # Last resort: a string value/key missing ONE quote (open or close) —
    # ``"path": mgt.c"`` / ``"path": "mgt.c}``. Error-position-guided requote
    # (string-aware), composed with close_unbalanced so a payload that is BOTH
    # quote-broken AND unclosed still recovers. Accept only if it validates
    # (bail-if-invalid) → a wrong guess falls through to diagnostic+retry.
    requoted, changed = repair_value_quotes(text)
    if changed:
        for cand in (requoted, close_unbalanced(requoted)[0]):
            for strict in (True, False):
                parsed = _extract_first_json(cand, strict=strict)
                if parsed is not None:
                    return parsed, True
    return None, False


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
    thought_required = False
    action_required = False
    multi_op = True
    # 미닫힘 <think> 뒤의 bare 배열(라인 선두 `[`)을 EOF-삼킴에서 보호.
    thinking_stop = re.compile(r"(?m)^\s*\[")
    # exposes_complete 기본 True 상속 — 종료는 명시적 `complete` op.

    # ─── Prompt ─────────────────────────────────────────────────

    def format_rules(self) -> str:
        return _FORMAT_RULES

    def format_rules_anchor(self) -> str:
        return (
            "Write brief reasoning as plain prose, then end the turn with "
            "ONE JSON array of ops (finish with a `complete` op)."
        )

    def format_rules_field_specific(self) -> str:
        return (
            "1. Optional reasoning is plain prose BEFORE the array.\n"
            '2. The turn ends with a JSON array of {"action": ..., params} ops.'
        )

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
        def _ops(items) -> list:
            return [
                Op(
                    action=(
                        it.get("action") if isinstance(it.get("action"), str) else None
                    ),
                    action_input={k: v for k, v in it.items() if k != "action"},
                )
                for it in items
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
                            ops=_ops(items),
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

        # ── 캐노니컬: 산문 + 첫 JSON 배열/객체 (bare 객체 = 1-op 관용)
        start = next((i for i, c in enumerate(llm_text) if c in "[{"), -1)
        if start >= 0:
            thought = self.sanitize_thought(llm_text[:start]) or None
            body = llm_text[start:]
            parsed, repaired = _extract_op_json(body)
            if parsed is not None:
                arr = parsed if isinstance(parsed, list) else [parsed]
                items = [x for x in arr if isinstance(x, dict)]
                stage = 2 if repaired else 1
                # `any("action")` 가드: 산문 속 `[1,2,3]` 같은 비-op 배열은
                # 통과시키지 않고 thought-only 로 (NO_ACTION 넛지).
                if any("action" in it for it in items):
                    return ParsedTurn(
                        thought=thought,
                        ops=_ops(items),
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

    def prose_completion(self, turn) -> str | None:
        """산문-only 최종답변 종결 (base 참조). json_fc 산문은 thought 로
        파싱된다(bare 배열 없으면 stage=1·ops=[]·thought=산문). 액션 잔해
        (op array/JSON 객체 흔적)가 없는 순수 자연어면 그 산문을 반환."""
        prose = (turn.thought or "").strip()
        if not prose or _JSON_ACTION_RESIDUE.search(prose):
            return None
        return prose

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

    def serialize_terminal_for_history(self, thought: str, result: str) -> dict:
        return {
            "role": "assistant",
            "thought": thought or "",
            "ops": [{"action": "complete", "action_input": {"result": result}}],
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
        return (
            'Each array element must include an "action" field naming '
            "one tool from Available Tools. If the task is DONE, emit a "
            '`complete` op: {"action": "complete", "result": "<final answer>"} '
            "— do not stop without it."
        )

    def failure_framing_parse_fail(self) -> str:
        return (
            "Your response did not match the expected format — it must end "
            "with a valid JSON array of tool calls."
        )

    def failure_framing_no_action(self) -> str:
        return 'Your JSON array had no usable tool call (missing or unknown "action").'

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
            "Your JSON array had no usable tool call",
        )
