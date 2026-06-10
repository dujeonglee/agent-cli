"""md_array wire format — markdown envelope + flat action-array (multi-op).

The shape (validated by the single-turn bakeoffs in
docs/inputs-array-schema/DESIGN.md §3; experimental until the Phase-2
full-loop bakeoff passes):

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
- Terminal = thought-only (``## Action`` omitted), parsed leniently
  (DESIGN §4.3): an empty / ``None``-marker action body, a bare
  result-bearing object, and header-less plain text all read as completion.
  The thought is the final answer.
- ``complete`` is NOT exposed (``exposes_complete=False``); the loop still
  honors a complete op the model invents (lenient). ``ready_for_review``
  stays an op-callable tool and gates termination (loop-side).
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
# prior resurfaces exactly when they mean "no action / done" (Phase-2: the
# dominant failure was `## Action\n\n## Input\n{}` looping NO_ACTION recovery).
_INPUT_RESIDUE = re.compile(r"^\s*##\s*Input\s*$", re.MULTILINE)

# "No action" markers the model writes instead of omitting the section.
_NONE_MARKERS = ("none", "n/a", "null", "nothing")

_FORMAT_RULES = """\
## Response Format

Respond in TWO markdown sections:

## Thought
<your reasoning>

## Action
<a JSON array of one or more tool calls>

Each array element is one tool call: {"action": "<tool name>", <its
parameters>}. Use the parameter names shown in each tool's guide above
(plain, no prefix). For several INDEPENDENT operations in one turn, add
multiple elements. If a later op depends on an earlier op's result, emit
only the first now — its observation comes next turn.

Rules:
1. Always include a `## Thought`.
2. Each `## Action` element must have an "action" naming one tool.
3. Each op acts on ONE target. To read N files, emit N separate
   {"action": "read_file", "path": ...} ops. NEVER put a list of items
   inside a single op (no nested arrays).
4. When the task is DONE and nothing remains to run, OMIT the `## Action`
   section entirely. A `## Thought`-only response means the task is
   complete, and your thought is the final answer.
5. As long as work remains, you MUST include `## Action`.
6. If an observation shows an error, fix parameters and retry.
7. Respond in the user's language.

Several independent operations:
## Thought
Read auth.py and list src/.

## Action
[{"action": "read_file", "path": "src/auth.py"}, {"action": "shell", "command": "ls src/"}]

Done (no action):
## Thought
The login() function is implemented and tests pass."""


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
    """Markdown envelope + flat action-array (multi-op, thought-only終)."""

    name = "md_array"
    thought_required = False
    action_required = False
    multi_op = True
    exposes_complete = False

    # ─── Provider hints ─────────────────────────────────────────

    def provider_call_kwargs(self, capabilities=None) -> dict:
        # Same rationale as prefix_md: JSON-object mode forces a leading `{`,
        # which makes the markdown envelope (`## ` headers) impossible — the
        # model is then locked into bare JSON and every turn misparses.
        # Phase-2 bakeoff caught exactly this: the base default leaked
        # json_mode=True and 100% of turns degraded to header-less JSON that
        # the lenient terminal mistook for completion.
        return {"json_mode": False}

    # ─── Prompt ─────────────────────────────────────────────────

    def format_rules(self) -> str:
        return _FORMAT_RULES

    def format_rules_anchor(self) -> str:
        return (
            "Respond with `## Thought` and — while work remains — a "
            "`## Action` JSON array of ops."
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

        def turn(ops, terminal, stage=1):
            return ParsedTurn(
                thought=clean_thought,
                ops=ops,
                terminal=terminal,
                raw=llm_text,
                parse_stage=stage,
            )

        if not has_action or not body:
            # Header-less bare JSON that carries tool ops: the model dropped
            # the envelope (drift) but its intent is clearly WORK — read it as
            # ops, never as a terminal answer. (Phase-2 caught the inverse
            # failure: header-less op JSON swallowed as "completion" made every
            # metric look perfect while no tool ever ran.)
            if not has_action and (llm_text.strip()[:1] in ("[", "{")):
                bare = _extract_first_json(llm_text.strip())
                bare_items = (
                    [x for x in bare if isinstance(x, dict)]
                    if isinstance(bare, list)
                    else ([bare] if isinstance(bare, dict) else [])
                )
                if any("action" in it for it in bare_items):
                    ops = [
                        Op(
                            action=(
                                it.get("action")
                                if isinstance(it.get("action"), str)
                                else None
                            ),
                            action_input={k: v for k, v in it.items() if k != "action"},
                        )
                        for it in bare_items
                    ]
                    return ParsedTurn(
                        thought=None, ops=ops, raw=llm_text, parse_stage=2
                    )
            # Thought-only (or empty ## Action): terminal — IF there is any
            # text at all. A blank emission is a parse failure (NO_OUTPUT).
            if clean_thought and clean_thought.strip():
                return turn([], True)
            return ParsedTurn(raw=llm_text, parse_stage=0)
        if body.lower().rstrip(".").strip() in _NONE_MARKERS:
            return turn([], True)
        # prefix_md-residue tolerance: strip stray `## Input` header lines the
        # model appends when it means "no action" (its prefix_md prior). An
        # ## Action body that was ONLY that residue is a terminal turn.
        body = _INPUT_RESIDUE.sub("", body).strip()
        if not body:
            return (
                turn([], True)
                if clean_thought
                else ParsedTurn(raw=llm_text, parse_stage=0)
            )

        parsed = _extract_first_json(body)
        if parsed is None:
            return ParsedTurn(thought=clean_thought, raw=llm_text, parse_stage=0)
        arr = parsed if isinstance(parsed, list) else [parsed]  # bare obj = 1 op
        items = [x for x in arr if isinstance(x, dict)]
        if not items:
            return ParsedTurn(thought=clean_thought, raw=llm_text, parse_stage=0)
        # Empty ops (`{}` / `[{}]` — typically the `## Input\n{}` residue):
        # nothing to run = a completion attempt, not a missing action. Items
        # that DO carry input but no action stay ops (NO_ACTION recovery).
        if clean_thought and all(not it for it in items):
            return turn([], True)
        # Lenient terminal: one result-bearing object with no `action` is a
        # completion attempt — answer = its result (DESIGN §4.3).
        if len(items) == 1 and "action" not in items[0] and "result" in items[0]:
            result = items[0].get("result")
            answer = str(result) if result else clean_thought
            return ParsedTurn(
                thought=answer, ops=[], terminal=True, raw=llm_text, parse_stage=1
            )
        ops = [
            Op(
                action=(
                    it.get("action") if isinstance(it.get("action"), str) else None
                ),
                action_input={k: v for k, v in it.items() if k != "action"},
            )
            for it in items
        ]
        return turn(ops, False)

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
        if turn.terminal:
            return {
                "role": "assistant",
                "thought": turn.thought or "",
                "terminal": True,
            }
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
        if record.get("terminal"):
            return {
                "role": "assistant",
                "content": f"## Thought\n{record.get('thought', '')}",
            }
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
            '{"action": ..., params} ops (or `## Thought` only when done).'
        )

    def constraint_reminder_action_required(self) -> str:
        # The DONE clause matters: Phase-2 showed the dominant NO_ACTION loop
        # was the model trying to FINISH (empty `## Action` + stray
        # `## Input {}`) while this reminder kept demanding an action.
        return (
            'Each `## Action` element must include an "action" field naming '
            "one tool from Available Tools. If the task is DONE and nothing "
            "remains to run, OMIT the `## Action` section entirely — a "
            "`## Thought`-only response finishes the task."
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
