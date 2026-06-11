"""md_array wire format — markdown envelope + flat action-array (multi-op).

The shape (DESIGN §3; multi-op validated by single-turn bakeoffs):

    ## Thought
    read auth.py and list src/

    ## Action
    [{"action": "read_file", "path": "src/auth.py"},
     {"action": "shell", "command": "ls src/"}]

- ``## Thought`` / ``## Action`` markdown envelope — the prefix_md shape the
  models already emit reliably (fixes the pure-JSON terminal envelope-drop).
- ``## Action`` body = JSON array of flat ``{action, ...params}`` ops.
  Several INDEPENDENT ops per turn; plain param keys (no wire-key prefix);
  ONE target per op (no per-tool batch — nesting a batch array inside the op
  array is what broke the 27B model, DESIGN §3 Exp 4/5). A bare object is
  accepted as a one-op array (the model's natural form for one op).
- Termination = an explicit ``complete`` op (``exposes_complete=True``), the
  proven prefix_md/react model. md_array originally ended thought-only
  (``## Action`` omitted) with a loop-side ready_for_review gate, but that
  produced a recurring class of finish bugs (false-terminate, NO_JSON
  finishing-transitions, empty ``[]``, a review-instruction mismatch that lost
  the deliverable — DESIGN Exp 8). Reviving ``complete`` fixes the class at the
  origin and lets the lenient-terminal parsing + the gate be removed. A
  thought-only / actionless turn is now a NO_ACTION nudge (call ``complete`` or
  emit ops), never a silent completion. ``ready_for_review`` reverts to a
  model-invoked pre-complete check (parity with prefix_md/react).
"""

from __future__ import annotations

import json
import re

from agent_cli.wire_formats.base import Op, ParsedAction, ParsedTurn, WireFormat

_THOUGHT_RE = re.compile(r"^##\s*Thought\s*$", re.MULTILINE)
_ACTION_RE = re.compile(r"^##\s*Action\s*$", re.MULTILINE)

# Lone wire-sentinel lines leaked into a thought (same self-reinforcement
# risk as prefix_md: raw riding back into the prior re-teaches the runaway).
# ``Input`` is included although it is not part of THIS format — it is the
# models' prefix_md prior leaking through (observed in Phase-2).
_SENTINEL_LINE = re.compile(r"^\s*##\s*(?:Thought|Action|Input)\s*$", re.MULTILINE)

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

Respond in TWO markdown sections:

## Thought
<your reasoning>

## Action
<a JSON array of one or more tool calls>

Each array element is one tool call: {"action": "<tool name>", <its
parameters>}. Use the parameter names shown in each tool's guide above
(plain, no prefix).

Batch independent work into ONE turn. Before you emit, look at everything
you intend to do: every operation that does NOT need another's output goes
in THIS turn as a separate array element. Reading three files, or a read
plus an unrelated search, is ONE turn — not three; batching saves turns and
context budget. Split into separate turns ONLY when a later step needs an
earlier step's result (then emit just the first now — its observation
arrives next turn).

Rules:
1. Always include a `## Thought`.
2. Each `## Action` element must have an "action" naming one tool.
3. Each op acts on ONE target. To read N files, emit N separate
   {"action": "read_file", "path": ...} ops in the SAME turn. NEVER put a
   list of items inside a single op (no nested arrays).
4. When the task is DONE, end with a `complete` op carrying your final
   answer: {"action": "complete", "result": "<your final answer>"}.
   Always finish this way — do NOT just stop or omit `## Action`.
5. Every turn must include a `## Action` with at least one op (work, or
   `complete` to finish).
6. If an observation shows an error, fix parameters and retry.
7. Respond in the user's language.

Several independent operations in one turn (read three files at once —
they don't depend on each other):
## Thought
To see how auth, session, and the login route fit together I need all
three files; none depends on another's output, so read them together.

## Action
[{"action": "read_file", "path": "src/auth.py"}, {"action": "read_file", "path": "src/session.py"}, {"action": "read_file", "path": "src/routes/login.py"}]

Finishing the task:
## Thought
The login() function is implemented and the tests pass.

## Action
[{"action": "complete", "result": "Implemented login() in src/auth.py; all tests pass."}]"""


def _extract_first_json(body: str):
    """Parse the first balanced ``[...]`` or ``{...}`` from body, or None."""
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
                    return json.loads(body[start : i + 1])
                except json.JSONDecodeError:
                    return None
    return None


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


def _extract_op_json(text: str):
    """``_extract_first_json`` with an anonymous-nested-object repair fallback.

    Returns ``(parsed, repaired)`` — ``repaired`` is True iff the strict parse
    failed but the unwrap repair recovered it (parse_stage 2). Tries both op
    shapes — ``{"action":X, {params}}`` (A) and ``{"action":X, {params}`` (B) —
    and keeps whichever parses."""
    parsed = _extract_first_json(text)
    if parsed is not None:
        return parsed, False
    for drop_close in (True, False):
        fixed = _repair_anonymous_op_objects(text, drop_close=drop_close)
        if fixed != text:
            parsed = _extract_first_json(fixed)
            if parsed is not None:
                return parsed, True
    return None, False


def _split_sections(text: str) -> tuple[str | None, str | None, bool]:
    """Return ``(thought, action_body, has_action_header)``.

    ``thought`` is the ``## Thought`` body — or, with no headers at all, the
    whole text (a header-less terminal answer). ``action_body`` is the text
    after ``## Action`` (None when the header is absent).
    """
    tm = _THOUGHT_RE.search(text)
    am = _ACTION_RE.search(text)
    thought: str | None = None
    if tm:
        end = am.start() if (am and am.start() > tm.end()) else len(text)
        thought = text[tm.end() : end].strip()
    elif not am:
        thought = text.strip()  # plain text, no headers → the answer
    if not am:
        return thought, None, False
    return thought, text[am.end() :].strip(), True


class MdArrayFormat(WireFormat):
    """Markdown envelope + flat action-array (multi-op, complete-terminated)."""

    name = "md_array"
    thought_required = False
    action_required = False
    multi_op = True
    # exposes_complete inherits the default True: completion is an explicit
    # `complete` op (the proven prefix_md/react model), not thought-only.

    # ─── Provider hints ─────────────────────────────────────────

    def provider_call_kwargs(self, capabilities=None) -> dict:
        # Same rationale as prefix_md: JSON-object mode forces a leading `{`,
        # which makes the markdown envelope (`## ` headers) impossible — the
        # model is then locked into bare JSON and every turn misparses.
        # Phase-2 bakeoff caught exactly this: the base default leaked
        # json_mode=True and 100% of turns degraded to header-less JSON.
        return {"json_mode": False}

    # ─── Prompt ─────────────────────────────────────────────────

    def format_rules(self) -> str:
        return _FORMAT_RULES

    def format_rules_anchor(self) -> str:
        return (
            "Respond with `## Thought` and a `## Action` JSON array of ops "
            "(finish with a `complete` op)."
        )

    def format_rules_field_specific(self) -> str:
        return (
            "1. Always include a `## Thought`.\n"
            '2. `## Action` is a JSON array of {"action": ..., params} ops.'
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
        # ``action_input`` is already this format's flat op JSON (it comes
        # through render_action_input); wrap it as a one-element array. The
        # op carries its own "action" except for virtual tools whose input
        # was authored with standard keys — splice the action in then.
        op = action_input
        if '"action"' not in op:
            try:
                obj = json.loads(op)
                if isinstance(obj, dict):
                    op = json.dumps({"action": action, **obj}, ensure_ascii=False)
            except json.JSONDecodeError:
                pass
        return f"## Thought\n{th}\n\n## Action\n[{op}]"

    # ─── Parsing ────────────────────────────────────────────────

    def parse_turn(self, llm_text: str) -> ParsedTurn:
        thought, body, has_action = _split_sections(llm_text)
        clean_thought = self.sanitize_thought(thought)

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

        if not has_action or not body:
            # Header-less op JSON: the model dropped the `## Action` envelope
            # but emitted a JSON op array. Read it as ops — even when prose
            # precedes the array (a FINISHING model often writes its reasoning
            # then appends `[{"action":"complete","result":<full answer>}]`
            # with no header; the deliverable lives in `result` and must not be
            # discarded). Extract the first JSON anywhere in the text, not only
            # at position 0. The `any("action")` guard means a stray bracket in
            # prose (`[1,2,3]`) falls through to the NO_ACTION nudge.
            if not has_action:
                # (repair fallback covers the malformed `{"action":X, {params}}`
                # shape the model emits header-less too — DESIGN Exp 8.)
                bare, _ = _extract_op_json(llm_text.strip())
                bare_items = (
                    [x for x in bare if isinstance(x, dict)]
                    if isinstance(bare, list)
                    else ([bare] if isinstance(bare, dict) else [])
                )
                if any("action" in it for it in bare_items):
                    return ParsedTurn(
                        thought=None, ops=_ops(bare_items), raw=llm_text, parse_stage=2
                    )
            # No `## Action` (thought-only) or an empty one: a valid markdown
            # parse with no op. NOT a completion — completion is an explicit
            # `complete` op. 0 ops → the loop's NO_ACTION recovery nudges the
            # model to call `complete` or emit work. A truly blank emission is
            # a parse failure (NO_OUTPUT / NO_JSON, stage 0).
            if clean_thought and clean_thought.strip():
                return ParsedTurn(
                    thought=clean_thought, ops=[], raw=llm_text, parse_stage=1
                )
            return ParsedTurn(raw=llm_text, parse_stage=0)
        # prefix_md-residue tolerance: strip stray `## Input` header lines the
        # model appends (its prefix_md prior) so a body that was ONLY residue
        # parses cleanly to 0 ops (→ NO_ACTION nudge), not a spurious NO_JSON.
        body = _INPUT_RESIDUE.sub("", body).strip()
        if not body:
            return (
                ParsedTurn(thought=clean_thought, ops=[], raw=llm_text, parse_stage=1)
                if clean_thought
                else ParsedTurn(raw=llm_text, parse_stage=0)
            )

        # Strict parse, then the anonymous-nested-object repair (DESIGN Exp 8):
        # `{"action":X, {params}}` → `{"action":X, params}`. A repaired turn is
        # a drift-recovery (parse_stage 2) for observability.
        parsed, repaired = _extract_op_json(body)
        if parsed is None:
            return ParsedTurn(thought=clean_thought, raw=llm_text, parse_stage=0)
        arr = parsed if isinstance(parsed, list) else [parsed]  # bare obj = 1 op
        items = [x for x in arr if isinstance(x, dict)]
        # No usable ops: an empty array `[]`, an empty op `{}`/`[{}]`, or a
        # non-dict payload. With a thought this is a valid 0-op turn (NO_ACTION
        # nudge to call `complete`); blank → parse failure. Items that DO carry
        # input but no action stay ops (NO_ACTION recovery / infer).
        if not items or all(not it for it in items):
            return ParsedTurn(
                thought=clean_thought,
                ops=[],
                raw=llm_text,
                parse_stage=1 if clean_thought else 0,
            )
        return ParsedTurn(
            thought=clean_thought,
            ops=_ops(items),
            raw=llm_text,
            parse_stage=2 if repaired else 1,
        )

    def parse(self, llm_text: str) -> ParsedAction:
        """Singular projection of :meth:`parse_turn` (first op).

        The loop dispatches via ``parse_turn``; this exists for the
        ``WireFormat`` ABC and any generic single-action consumer. History
        serialization is overridden below (multi-op record), so this is not
        on the history path.
        """
        t = self.parse_turn(llm_text)
        first = t.ops[0] if t.ops else None
        return ParsedAction(
            thought=t.thought,
            action=first.action if first else None,
            action_input=first.action_input if first else None,
            raw=t.raw,
            parse_stage=t.parse_stage,
        )

    def is_degenerate(self, text: str) -> bool:
        # Same threshold philosophy as prefix_md (≥2 empty envelope blocks);
        # see the prefix_md rationale for why ≥2 is kept.
        return len(_DEGEN_RUNAWAY.findall(text)) >= 2

    def sanitize_thought(self, thought: str | None) -> str | None:
        if not thought:
            return thought
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
            return {
                "role": "assistant",
                "content": (
                    f"## Thought\n{record.get('thought', '')}\n\n## Action\n{rendered}"
                ),
            }
        # Legacy / singular-shaped records (e.g. written by a corrected-record
        # rewrite or another format): fall back to the base round-trip.
        return super().render_assistant_from_history(record)

    # ─── Recovery wording ───────────────────────────────────────

    def constraint_reminder_call(self) -> str:
        return (
            "Respond with `## Thought` and a `## Action` JSON array of "
            '{"action": ..., params} ops. To finish, use a `complete` op: '
            '{"action": "complete", "result": "<final answer>"}.'
        )

    def constraint_reminder_action_required(self) -> str:
        # The DONE clause matters: a finishing model that emits no runnable op
        # must be pointed at `complete` (not left demanding generic "an
        # action"), or it loops trying to stop.
        return (
            'Each `## Action` element must include an "action" field naming '
            "one tool from Available Tools. If the task is DONE, emit a "
            '`complete` op: {"action": "complete", "result": "<final answer>"} '
            "— do not stop without it."
        )

    def failure_framing_parse_fail(self) -> str:
        return (
            "Your response did not match the expected format — `## Action` "
            "must contain a valid JSON array of tool calls."
        )

    def failure_framing_no_action(self) -> str:
        return (
            "Your `## Action` section had no usable tool call (missing or "
            'unknown "action").'
        )

    def static_retry_hint_no_json(self) -> str:
        return f"{self.failure_framing_parse_fail()} {self.constraint_reminder_call()}"

    def static_retry_hint_no_action(self) -> str:
        return (
            f"{self.failure_framing_no_action()} "
            f"{self.constraint_reminder_action_required()}"
        )

    def system_user_prefixes(self) -> tuple[str, ...]:
        return (
            "Your response did not match the expected format",
            "Your `## Action` section had no usable tool call",
        )
