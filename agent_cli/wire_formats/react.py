"""ReAct wire format — the default plugin.

Self-contained module owning every string and behavior that defines the
ReAct wire format: format rules text, recovery wording, parser
adaptation, and lifecycle hooks. Removing this file would remove ReAct
support entirely (with no edits anywhere else needed) — the boundary
the plugin system promises.

Inherits from :class:`agent_cli.wire_formats.base.WireFormat` ABC. The
history pipeline defaults (``serialize_assistant_for_history`` /
``render_assistant_from_history`` — which also build the next-turn prior),
identity hooks (``render_action_input``, ``sanitize_thought``), and
provider/lifecycle hooks (``prefill``, ``provider_call_kwargs``,
``format_rules``) all come from the base — ReAct only specifies what
makes its wire shape unique.
"""

from __future__ import annotations

import json
import re

from agent_cli.recovery.intervention import Intervention
from agent_cli.recovery.primitives import echo_prior_output
from agent_cli.wire_formats._json_diag import describe_json_error
from agent_cli.wire_formats.base import Op, ParsedAction, ParsedTurn, WireFormat


def _ops_from_items(items: list) -> list[Op]:
    """Convert a list of op-dicts (``{"action": tool, ...flat params}``) into
    ``Op`` objects. Self-contained copy of the same extraction md_array uses —
    the op SHAPE is a cross-format contract (guarded by a parity test), but the
    code stays per-plugin so the two formats evolve independently."""
    return [
        Op(
            action=(it.get("action") if isinstance(it.get("action"), str) else None),
            action_input={k: v for k, v in it.items() if k != "action"},
        )
        for it in items
        if isinstance(it, dict)
    ]


# ── ReAct parser ─────────────────────────────────────────────
# 3-stage fallback parser plus its stage-2 JSON repair helper. Lives
# entirely in this module (no ``parsing/`` package, no shared
# ``json_repair`` module) so the whole ReAct format — parser,
# repair, format rules, recovery wording, history rendering — is
# folder-deletable as a single boundary. If a future plugin needs the
# same JSON repair algorithm we re-evaluate sharing at that point;
# pre-emptive extraction would impose ReAct's repair policy on
# wire formats that may want a different recovery strategy.

# Known thinking/reasoning block tag names (case-insensitive)
_THINKING_TAGS = ["think", "thinking", "reasoning", "reflection"]

# Build regex that matches any of the known thinking tags
_THINKING_PATTERN = re.compile(
    r"<(" + "|".join(_THINKING_TAGS) + r")>(.*?)</\1>",
    re.S | re.I,
)


def _sanitize_surrogates(text: str) -> str:
    """Remove unpaired Unicode surrogates that break JSON parsing."""
    return re.sub(r"[\ud800-\udfff]", "", text)


def _strip_thinking_blocks(text: str) -> tuple[str, str | None]:
    """Strip thinking/reasoning blocks from LLM output.

    Handles: <think>...</think>, <thinking>...</thinking>,
             <reasoning>...</reasoning>, <reflection>...</reflection>

    Returns: (text_without_blocks, extracted_thinking_content or None)
    """
    thinking_parts: list[str] = []

    def _collect(match):
        content = match.group(2).strip()
        if content:
            thinking_parts.append(content)
        return ""

    cleaned = _THINKING_PATTERN.sub(_collect, text).strip()

    if thinking_parts:
        return cleaned, "\n\n".join(thinking_parts)
    return text, None


def parse_react(text: str) -> ParsedAction:
    """Parse an LLM response into a :class:`ParsedAction` using 3-stage fallback.

    Stage 0: strip ``<think>...</think>`` and similar thinking blocks.
    Stage 1: ``json.loads`` after markdown-fence strip — fast path.
    Stage 2: :func:`repair_json` — close unterminated strings, brackets,
             fix quotes/unquoted keys/trailing commas, extract embedded block.
    Stage 3: regex field extraction — last-resort recovery.

    Returns a :class:`ParsedAction` with ``parse_stage`` set to the
    successful stage (``0`` if all three failed).
    """
    text = _sanitize_surrogates(text)
    text, thinking = _strip_thinking_blocks(text)
    result = ParsedAction(raw=text, thinking=thinking)

    # Stage 1: Direct JSON parse
    data = _try_json_parse(text)
    if data is not None:
        _populate_from_dict(result, data)
        result.parse_stage = 1
        return result

    # Stage 2a: lenient parse — the model wrote literal control characters
    # (raw newlines/tabs) inside a string value (big `content`/`result`
    # blobs written without `\n` escaping), which strict json.loads rejects
    # ("Invalid control character"). Re-parse with strict=False; a recovery,
    # so parse_stage 2. Without this the action_input falls to the stage-3
    # regex as a raw string (unusable by the tool).
    data = _try_json_parse(text, strict=False)
    if data is not None:
        _populate_from_dict(result, data)
        result.parse_stage = 2
        return result

    # Stage 2b: JSON repair
    data, was_truncated = repair_json(text)
    if data is not None:
        _populate_from_dict(result, data)
        result.parse_stage = 2
        result.truncated = was_truncated
        return result

    # Stage 3: Regex extraction
    extracted = _regex_extract(text)
    if extracted:
        _populate_from_dict(result, extracted)
        result.parse_stage = 3
        return result

    return result


def _try_json_parse(text: str, *, strict: bool = True) -> dict | None:
    """Direct JSON parse. ``strict`` is forwarded to ``json.loads`` — with
    ``strict=False`` literal control characters inside string values are
    accepted (the lenient stage-2 recovery; see :func:`parse_react`)."""
    stripped = strip_markdown_fences(text)

    try:
        data = json.loads(stripped, strict=strict)
        if isinstance(data, dict):
            return data
    except (json.JSONDecodeError, ValueError):
        pass

    # Try extracting first { ... } block using balanced brace extraction
    extracted = _extract_json_block(stripped)
    if extracted != stripped:
        try:
            data = json.loads(extracted, strict=strict)
            if isinstance(data, dict):
                return data
        except (json.JSONDecodeError, ValueError):
            pass

    return None


def _regex_extract(text: str) -> dict | None:
    """Stage 3: Extract fields via regex patterns."""
    result: dict = {}

    m = re.search(r'"thought"\s*:\s*"((?:[^"\\]|\\.)*)"', text, re.S)
    if m:
        result["thought"] = m.group(1).replace('\\"', '"').replace("\\n", "\n")

    m = re.search(r'"action"\s*:\s*"((?:[^"\\]|\\.)*)"', text, re.S)
    if m:
        result["action"] = m.group(1).replace('\\"', '"')

    m = re.search(r'"action_input"\s*:\s*(\{[^}]*\})', text, re.S)
    if m:
        try:
            result["action_input"] = json.loads(m.group(1))
        except (json.JSONDecodeError, ValueError):
            result["action_input"] = m.group(1)

    return result if result else None


# Keys that are part of the ReAct protocol or are reserved for internal
# use. They must NEVER be hoisted into action_input even when siblings
# of `action`. Seeding this blacklist defensively:
#   - thought / action / action_input : the ReAct protocol trio
#   - observation: system prompt forbids it in model output; if emitted
#     it's a drift, not a tool arg
#   - reasoning / reflection: thinking-tag variants (already stripped by
#     _strip_thinking_blocks when they appear as tags, but models
#     occasionally emit them as top-level keys too)
#   - role / _meta: added by our own storage / session layer; a confused
#     model could echo them back
_REACT_RESERVED: frozenset[str] = frozenset(
    {
        "thought",
        "action",
        "action_input",
        "observation",
        "reasoning",
        "reflection",
        "role",
        "_meta",
    }
)


# Virtual-tool payload hoisting map.
#
# Some models (observed with qwen3 family) emit responses like:
#   {"thought": "...", "action": "complete", "result": "final answer"}
# where the payload key is at the top level instead of nested inside
# action_input. This is valid JSON and the action name is correct, so
# nothing downstream catches it — the complete handler just sees
# action_input=None and reports "Completed without result".
#
# Entry shape: action_name -> (target_key_in_action_input, top_level_fallback_keys)
# The first matching top-level key's value is placed under target_key.
# For virtual tools we deliberately do NOT fall through to the real-tool
# bundling rule: if none of the known alias keys are present, leave
# action_input as None so the downstream handler can render its "no
# payload" path rather than dispatching with an arbitrary sibling.
_VIRTUAL_TOOL_PAYLOAD_HOIST: dict[str, tuple[str, tuple[str, ...]]] = {
    "complete": ("result", ("result", "answer", "response", "final", "output")),
    "ready_for_review": ("summary", ("summary",)),
    # For ask, _extract_questions in loop.py already treats "questions" and
    # "question" interchangeably, so placing the hoisted value under
    # "questions" is safe regardless of which top-level key the model used.
    "ask": ("questions", ("questions", "question")),
}


def _normalize_action_input(result: ParsedAction, data: dict) -> None:
    """Normalize sibling-emitted tool arguments back into action_input.

    Two layers:

    1. **Virtual tools** (complete / ready_for_review / ask). The payload
       key can drift under several aliases (complete's `result` ↔
       `answer` ↔ `response`); map the first matching alias back to the
       canonical key. If no alias matches, leave action_input=None so
       the downstream handler shows its "no payload" message.

    2. **Real tools and unknown actions**. Bundle every non-reserved
       top-level key into action_input. This catches the pcie_scsc-style
       drift where a model emits:
           {"thought":"...","action":"shell","command":"ls"}
       with `command` as a sibling of `action` rather than nested inside
       action_input. Reserved keys (_REACT_RESERVED) are filtered out so
       protocol fields or meta keys can't poison tool input.

       Layer 2 runs even when ``action`` is absent: a dropped action name
       with bundled siblings (e.g. a prefixed ``{"xtool_arg":"v"}``) must
       still surface action_input so the loop's ``infer_action`` can recover
       the tool (wire-key prefix → tool) under ``action_required=False`` — the
       parser-side half of the preservation invariant: a dropped action is
       recoverable from the preserved action_input keys. (All builtin tools
       are flat-native as of consolidation Step 3, so this prefix-recovery
       path is latent — it serves MCP / future prefixed tools.)

    Precedence rule: if action_input is already present and truthy, use
    it verbatim and ignore any siblings. Empty dicts and None both
    trigger the layer logic.
    """
    if result.action_input:
        return

    # Layer 1: virtual tool alias mapping — action-specific, so only when
    # an action name is present.
    if result.action:
        spec = _VIRTUAL_TOOL_PAYLOAD_HOIST.get(result.action)
        if spec is not None:
            target_key, candidates = spec
            for key in candidates:
                if key in data:
                    result.action_input = {target_key: data[key]}
                    return
            # Known virtual tool with no alias match — leave action_input
            # None rather than bundling stray siblings as payload.
            return

    # Layer 2: real tool / unknown / dropped action sibling bundling.
    extras = {k: v for k, v in data.items() if k not in _REACT_RESERVED}
    if extras:
        result.action_input = extras


def _populate_from_dict(result: ParsedAction, data: dict) -> None:
    """Fill a ParsedAction from a parsed dict."""
    result.thought = data.get("thought")
    result.action = data.get("action")
    result.action_input = data.get("action_input")
    _normalize_action_input(result, data)


# ── Prompt section ───────────────────────────────────────────
# The Format Rules section is now composed by the shared builder
# ``_format_rules_builder.build_format_rules`` so that two plugins
# rendering "the same logical content" produce byte-equivalent shared
# text and only differ in the wire-shape-specific fragments returned
# by ``format_rules_anchor`` / ``render_full_example`` /
# ``format_rules_field_specific`` below.

_FORMAT_RULES_ANCHOR = (
    "You MUST output a single JSON object only — no markdown fences, no surrounding\n"
    "text, no `observation` field (it is injected by the system):"
)

# Split into per-field clauses so Rule 1 (thought) can be gated on
# ``thought_required`` via ``WireFormat._gated_rule``. Rule 2 is the
# action_input contract — not an action-presence obligation — so
# ``action_required`` has no clause to soften here (react expresses the
# action requirement through the JSON-envelope anchor, not a numbered rule).
# The composed string is byte-identical to the previous single constant.
_THOUGHT_RULE = (
    "`thought` MUST state purpose (what you want to achieve) and reason "
    "(why this action). Do not leave it empty."
)
_ACTION_INPUT_RULE = (
    "`action_input` MUST match the tool's input schema. For `complete`, "
    "`result` is a plain text string — write the answer directly. Do NOT "
    'wrap it in another JSON envelope like `{"result": "{\\"result\\": ...}"}`.'
)


# ── Recovery fragments ────────────────────────────────────────
# Mirror the bodies in ``recovery/primitives.constrain_format_json`` and
# ``recovery/primitives.constrain_action_required``. Step 3 removes
# those once ``recovery/builders`` start calling the wire format method.
_CONSTRAINT_REMINDER_CALL = (
    "Output ONLY a JSON object: "
    '{"thought": "...", "action": "tool_name", "action_input": {...}}. '
    "No markdown fences, no extra text."
)

_CONSTRAINT_REMINDER_ACTION_REQUIRED = (
    "You MUST include an action. Either use a tool: "
    '{"thought": "...", "action": "tool_name", "action_input": {...}} '
    "or complete the task: "
    '{"thought": "...", "action": "complete", "action_input": {"result": "..."}}'
)

# Opening lines used by the recovery builders to frame the failure
# (parse-fail vs. action-missing). Match the legacy hardcoded strings
# in ``recovery/builders.format_no_json_retry`` and ``…no_action_retry``.
_FAILURE_FRAMING_PARSE_FAIL = "Your response was not valid JSON."
_FAILURE_FRAMING_NO_ACTION = "Your JSON was parsed but has no action."

# Static fallbacks injected when there is nothing meaningful to echo
# back (empty/whitespace prior emission). Match ``constants.RETRY_HINT_*``.
_STATIC_RETRY_HINT_NO_JSON = (
    "Your response was not valid JSON. "
    "Output ONLY a JSON object: "
    '{"thought": "...", "action": "tool_name", "action_input": {...}}. '
    "No markdown fences, no extra text."
)

_STATIC_RETRY_HINT_NO_ACTION = (
    "Your JSON was parsed but has no action. "
    "You MUST include an action. Either use a tool: "
    '{"thought": "...", "action": "tool_name", "action_input": {...}} '
    "or complete the task: "
    '{"thought": "...", "action": "complete", "action_input": {"result": "..."}}'
)


# Recovery framings emitted by THIS plugin. Format-agnostic prefixes
# (``"You have called"``, ``"You were asked to:"``, plus the interrupt
# notice) live in ``wire_formats._FORMAT_AGNOSTIC_USER_PREFIXES`` and
# are unioned with this list at consume time via
# ``wire_formats.all_system_user_prefixes()``. Includes the NO_THOUGHT
# framing because thought is a required field in this format.
_SYSTEM_USER_PREFIXES = (
    "Your response was not valid JSON.",
    "Your JSON was parsed but has no action.",
    "Your JSON was missing the 'thought' field.",
)


# ── NO_THOUGHT recovery (ReAct-only) ──────────────────────────
# ``thought`` is a required field in the ReAct schema, so emitting
# an action without it triggers a retry. The constraint and framing
# live here because no other plugin shares them — envelope plugins
# carry thought as preceding free text, where its absence is valid.
_NO_THOUGHT_FRAMING = "Your JSON was missing the 'thought' field."

_NO_THOUGHT_CONSTRAINT = (
    "Your JSON must include a 'thought' field stating purpose "
    "(what you want to achieve) and reason (why this specific action). "
    "Do not omit it."
)


# ── Multi-op Response Format (JSON twin of md_array) ─────────
# react owns its own format-rules text rather than composing via
# ``build_format_rules`` — the shared builder's tail hardcodes "Exactly ONE
# action per turn", which is single-op. Self-contained per the plugin
# philosophy: react and md_array carry the same multi-op CONTRACT but each
# owns its wording so either can change without touching the other.
_FORMAT_RULES = """\
## Response Format

Output a single JSON object only — no markdown fences, no surrounding text,
no `observation` field (it is injected by the system):

{"thought": "<your reasoning>", "actions": [<one or more tool calls>]}

Each element of `actions` is one tool call: {"action": "<tool name>", <its
parameters>}. Use the parameter names shown in each tool's guide above
(plain, no prefix).

Batch independent work into ONE turn. Before you emit, look at everything you
intend to do: every operation that does NOT need another's output goes in
THIS turn as a separate `actions` element. Reading three files, or a read
plus an unrelated search, is ONE turn — not three; batching saves turns and
context budget. Split into separate turns ONLY when a later step needs an
earlier step's result (then emit just the first now — its observation arrives
next turn).

Rules:
1. Always include a `thought` stating purpose (what you want to achieve) and
   reason (why this action). Do not leave it empty.
2. Each `actions` element must have an "action" naming one tool, and its
   parameters must match that tool's input schema.
3. Each op acts on ONE target. To read N files, emit N separate
   {"action": "read_file", "path": ...} ops in the SAME turn. NEVER put a
   list of items inside a single op (no nested arrays).
4. When the task is DONE, end with a `complete` op carrying your final
   answer: {"action": "complete", "result": "<your final answer>"}. Always
   finish this way — do NOT just stop or omit `actions`.
5. Every turn must include `actions` with at least one op (work, or `complete`
   to finish).
6. If an observation shows an error, fix parameters and retry.
7. Respond in the user's language.

Several independent operations in one turn (read three files at once — they
don't depend on each other):
{"thought": "To see how auth, session, and the login route fit together I need all three files; none depends on another's output, so read them together.", "actions": [{"action": "read_file", "path": "src/auth.py"}, {"action": "read_file", "path": "src/session.py"}, {"action": "read_file", "path": "src/routes/login.py"}]}

Finishing the task:
{"thought": "The login() function is implemented and the tests pass.", "actions": [{"action": "complete", "result": "Implemented login() in src/auth.py; all tests pass."}]}"""


class ReActFormat(WireFormat):
    """Reference plugin — preserves pre-plugin behavior.

    Inner shape: ``{"thought": ..., "action": ..., "action_input": ...}``.
    Parser: 3-stage fallback (``parse_react``). Recovery: the
    long-standing ``constrain_format_json`` / ``constrain_action_required``
    text. Provider quirks: none (JSON mode stays active when
    capabilities support it).

    Inherits the history pipeline defaults from ``WireFormat``:
    ``serialize_assistant_for_history`` runs ``parse_react`` and stores
    structured fields; ``render_assistant_from_history`` re-emits those
    fields through ``render_full_example`` (JSON wire shape) — this is also
    how the next-turn prior is built. Identity defaults for ``prefill``,
    ``provider_call_kwargs``, ``render_action_input``, and
    ``sanitize_thought`` also apply (react's thought is a JSON string, so
    there are no ``##`` sentinels to strip) — ReAct overrides none of them.

    The class has no constructor parameters: registration is a single
    ``register(ReActFormat())`` call at module import time (see
    ``agent_cli/wire_formats/__init__.py``).
    """

    name = "react"
    # Both fields optional: a missing thought or a dropped action no longer
    # force a retry by themselves. A dropped
    # action is recovered via infer_action on the preserved action_input
    # (the parser keeps non-reserved siblings bundled even without an
    # action — see _normalize_action_input). NO_THOUGHT / NO_ACTION
    # recovery still fires for any plugin that sets these True (tested via
    # a synthetic plugin so the True paths stay covered).
    thought_required = False
    action_required = False
    # Multi-op: a turn carries `actions: [op, ...]`. Setting this engages the
    # shared multi-op machinery — prompt renders flat per-tool params
    # (`_multi_op_flat_params`) and dispatch re-wraps flat ops to canonical
    # input (`wrap_single_op`), the same path md_array uses.
    multi_op = True

    # ─── Prompt ────────────────────────────────────────────────

    def format_rules(self) -> str:
        # Own multi-op rules (not the single-op shared builder). See
        # ``_FORMAT_RULES`` above.
        return _FORMAT_RULES

    def render_action_input(self, action_input: dict) -> str:
        # Inline guides hand in a wire-key-prefixed dict (`_rai_prefixed`);
        # render it as a flat op: {"action": tool, plain params} — the same
        # multi-op op shape parse_turn reads. Self-contained copy of md_array's
        # transform (the op shape is a cross-format contract; the code stays
        # per-plugin). Without this react's inline examples render the prefixed
        # `{"delegate_task": ...}` shape, not the flat `{"action": ...}` op.
        if isinstance(action_input, dict) and action_input:
            from agent_cli.tools.registry import TOOLS

            for tool_name in sorted(TOOLS, key=len, reverse=True):
                pfx = tool_name + "_"
                if all(k.startswith(pfx) for k in action_input):
                    flat = {k[len(pfx) :]: v for k, v in action_input.items()}
                    return json.dumps({"action": tool_name, **flat}, ensure_ascii=False)
        return json.dumps(action_input, ensure_ascii=False)

    def format_rules_anchor(self) -> str:
        return _FORMAT_RULES_ANCHOR

    def format_rules_field_specific(self) -> str:
        # Rule 1 (thought) gated on thought_required; currently resolves to
        # the strong wording (no soft variant) so the section is unchanged.
        return (
            f"1. {self._gated_rule(self.thought_required, _THOUGHT_RULE)}\n"
            f"2. {_ACTION_INPUT_RULE}"
        )

    def render_full_example(self, *, thought, action: str, action_input: str) -> str:
        # Multi-op JSON: {"thought": ..., "actions": [{"action": tool, ...flat
        # params}]}. ``action_input`` is a JSON string of the params; splice
        # the action into it to form one flat op, then wrap in the actions
        # array (a one-op example of the multi-op shape — mirrors md_array).
        # ``thought=None`` (skill / agent invocation example) substitutes a
        # short placeholder so the reasoning slot stays visible.
        reasoning = thought if thought is not None else "reasoning here"
        op = action_input
        if '"action"' not in op:
            try:
                obj = json.loads(op)
            except (json.JSONDecodeError, ValueError):
                obj = None  # unparseable placeholder like "{...}" — leave as-is
            else:
                if isinstance(obj, dict):
                    op = json.dumps({"action": action, **obj}, ensure_ascii=False)
                else:
                    # Non-dict params (e.g. legacy complete with a bare string):
                    # keep the action, nest the value so the op stays valid JSON.
                    op = json.dumps(
                        {"action": action, "action_input": obj}, ensure_ascii=False
                    )
        return f'{{"thought": "{reasoning}", "actions": [{op}]}}'

    # ─── Parsing ───────────────────────────────────────────────

    def parse(self, llm_text: str) -> ParsedAction:
        return parse_react(llm_text)

    def parse_turn(self, llm_text: str) -> ParsedTurn:
        """Multi-op JSON: ``{"thought": ..., "actions": [{"action": ...,
        ...flat params}, ...]}`` — several independent ops in one turn, each op
        the same flat shape md_array uses.

        Falls back to the CLASSIC single-op shape ``{"thought": ..., "action":
        ..., "action_input": ...}`` as a one-op turn. That shape is the most
        heavily-trained ReAct prior, so accepting it is genuine resilience (not
        just back-compat). Discrimination is unambiguous: an ``actions`` array
        → multi-op; anything else → the base wraps ``parse()`` as one op.
        Completion is an explicit ``complete`` op (parity with md_array)."""
        text = _sanitize_surrogates(llm_text)
        stripped, thinking = _strip_thinking_blocks(text)
        data = _try_json_parse(stripped)
        stage = 1
        if data is None:
            data = _try_json_parse(stripped, strict=False)
            stage = 2
        if data is None:
            repaired, _ = repair_json(stripped)
            if isinstance(repaired, dict):
                data, stage = repaired, 2
        if isinstance(data, dict) and isinstance(data.get("actions"), list):
            items = [it for it in data["actions"] if isinstance(it, dict)]
            thought = data.get("thought")
            clean = self.sanitize_thought(thought if isinstance(thought, str) else None)
            return ParsedTurn(
                thought=clean,
                ops=_ops_from_items(items),
                raw=llm_text,
                parse_stage=stage,
                thinking=thinking,
            )
        # Classic single-op (or unparseable) → base wraps parse() as 1 op.
        return super().parse_turn(llm_text)

    # ─── History round-trip (multi-op record) ──────────────────
    # Self-contained: react stores the same logical record shape as md_array
    # ({role, thought, ops}) but owns the code, and renders it back as a JSON
    # object (md_array renders markdown). The op shape is the cross-format
    # contract; the envelope is per-plugin.

    def serialize_assistant_for_history(self, raw_text: str) -> dict:
        turn = self.parse_turn(raw_text)
        # Store an ops record only when at least one op names a tool. react's
        # parser bundles bare non-reserved siblings as an actionless op (for
        # live dropped-action infer), but for HISTORY a no-tool emission is
        # drift — keep the raw text as content so it survives verbatim.
        if turn.ops and any(op.action for op in turn.ops):
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
            flat = []
            for o in ops:
                if not isinstance(o, dict):
                    continue
                op = {"action": o.get("action")}
                ai = o.get("action_input")
                if isinstance(ai, dict):
                    op.update(ai)
                elif ai is not None:
                    op["action_input"] = ai  # non-dict (legacy) — keep nested
                flat.append(op)
            content = json.dumps(
                {"thought": record.get("thought", ""), "actions": flat},
                ensure_ascii=False,
            )
            return {"role": "assistant", "content": content}
        # Legacy single-op record ({thought, action, action_input}) → base
        # round-trip via render_full_example (renders as a 1-op actions array).
        return super().render_assistant_from_history(record)

    # ─── Recovery ──────────────────────────────────────────────

    def constraint_reminder_call(self) -> str:
        return _CONSTRAINT_REMINDER_CALL

    def constraint_reminder_action_required(self) -> str:
        return _CONSTRAINT_REMINDER_ACTION_REQUIRED

    def failure_framing_parse_fail(self) -> str:
        return _FAILURE_FRAMING_PARSE_FAIL

    def failure_framing_no_action(self) -> str:
        return _FAILURE_FRAMING_NO_ACTION

    def static_retry_hint_no_json(self) -> str:
        return _STATIC_RETRY_HINT_NO_JSON

    def static_retry_hint_no_action(self) -> str:
        return _STATIC_RETRY_HINT_NO_ACTION

    def diagnose_syntax_error(self, prior_content: str) -> str | None:
        # The whole emission is one JSON object — strip fences and take the
        # outermost { ... } block (the same candidate parse_turn tried), then
        # let the shared formatter locate the break.
        candidate = _extract_json_block(strip_markdown_fences(prior_content))
        return describe_json_error(candidate)

    def system_user_prefixes(self) -> tuple[str, ...]:
        return _SYSTEM_USER_PREFIXES

    # ─── ReAct-only recovery (NO_THOUGHT) ──────────────────────
    # Not part of the WireFormat base interface — only plugins with
    # ``thought_required=True`` emit this intervention, and the loop
    # gates the call on that flag. Plugins where thought is preceding
    # free text (not a structured schema field) set
    # ``thought_required=False`` and never reach this method. Adding
    # it to the base would force every plugin to implement a no-op,
    # so duck typing wins here.

    def format_no_thought_retry(self, *, prior_content: str = "") -> Intervention:
        """Build the Intervention when an action was emitted without
        the required ``thought`` field.

        Same failure-grounding shape as the format_no_*_retry builders
        in ``recovery.wf_recovery``: echo the prior output so the model
        sees its own omission, then restate the constraint. Inlined
        rather than promoted to a primitive — adding a primitive for a
        single caller violates the "primitive reused by ≥2 failures"
        anti-patchwork invariant in DESIGN.md §4.
        """
        echo = echo_prior_output(prior_content)
        if not echo:
            return Intervention(
                message=f"{_NO_THOUGHT_FRAMING} {_NO_THOUGHT_CONSTRAINT}",
                primitives=[],
            )

        msg = "\n".join(
            [
                _NO_THOUGHT_FRAMING,
                "",
                echo,
                "Honor that. " + _NO_THOUGHT_CONSTRAINT,
            ]
        )
        return Intervention(
            message=msg,
            primitives=["echo_prior_output"],
        )


# ── Stage 2 helper: repair malformed JSON ────────────────────
# Used by ``parse_react`` stage 2. Kept module-public (no underscore
# prefix) because ``test_json_repair`` exercises it directly — that
# test is part of the ReAct plugin's coverage. Other plugins import
# this only if they explicitly opt in to ReAct's repair policy; new
# plugins are encouraged to define their own recovery rather than
# share by default.
#
# Handles common issues seen in small-model output:
#   1. Unclosed strings
#   2. Missing closing brackets
#   3. Trailing commas
#   4. Single quotes instead of double quotes
#   5. Unquoted keys
#   6. JSON embedded in surrounding text (markdown fences, prose)


def repair_json(text: str) -> tuple[dict | None, bool]:
    """Attempt to repair malformed JSON text into a valid dict.

    Returns (parsed_dict, was_truncated).
    was_truncated is True if brackets/strings had to be closed.
    """
    cleaned = _extract_json_block(text)
    cleaned = _fix_quotes(cleaned)
    cleaned = _fix_unquoted_keys(cleaned)
    cleaned = _fix_trailing_commas(cleaned)
    cleaned, str_closed = _fix_unclosed_strings(cleaned)
    cleaned, brackets_closed = _fix_missing_brackets(cleaned)
    was_truncated = str_closed or brackets_closed

    try:
        result = json.loads(cleaned)
        if isinstance(result, dict):
            return result, was_truncated
    except (json.JSONDecodeError, ValueError):
        pass

    return None, False


def strip_markdown_fences(text: str) -> str:
    """Remove `````json ... ````` wrapping."""
    stripped = re.sub(r"^```(?:json)?\s*", "", text.strip(), flags=re.I)
    stripped = re.sub(r"\s*```\s*$", "", stripped)
    return stripped


def _extract_json_block(text: str) -> str:
    """Find the outermost { ... } block in the text."""
    text = strip_markdown_fences(text)

    start = text.find("{")
    if start == -1:
        return text

    depth = 0
    in_string = False
    escape_next = False
    last_close = -1

    for i in range(start, len(text)):
        ch = text[i]
        if escape_next:
            escape_next = False
            continue
        if ch == "\\":
            if in_string:
                escape_next = True
            continue
        if ch == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            last_close = i
            if depth == 0:
                return text[start : i + 1]

    if last_close > start:
        return text[start : last_close + 1]
    return text[start:]


def _fix_quotes(text: str) -> str:
    """Replace single-quoted strings with double-quoted strings."""
    result = []
    in_double = False
    in_single = False
    escape_next = False

    for ch in text:
        if escape_next:
            result.append(ch)
            escape_next = False
            continue
        if ch == "\\":
            result.append(ch)
            escape_next = True
            continue
        if ch == '"' and not in_single:
            in_double = not in_double
            result.append(ch)
        elif ch == "'" and not in_double:
            in_single = not in_single
            result.append('"')
        else:
            result.append(ch)

    return "".join(result)


def _fix_unquoted_keys(text: str) -> str:
    """Add double quotes around unquoted JSON keys."""
    return re.sub(
        r"([{,]\s*)([a-zA-Z_]\w*)(\s*:)",
        r'\1"\2"\3',
        text,
    )


def _fix_trailing_commas(text: str) -> str:
    """Remove trailing commas before } or ]."""
    return re.sub(r",\s*([}\]])", r"\1", text)


def _fix_unclosed_strings(text: str) -> tuple[str, bool]:
    """Close unclosed string literals at end of text.

    Returns (fixed_text, was_closed).
    """
    in_string = False
    escape_next = False

    for ch in text:
        if escape_next:
            escape_next = False
            continue
        if ch == "\\":
            escape_next = True
            continue
        if ch == '"':
            in_string = not in_string

    if in_string:
        return text + '"', True

    return text, False


def _fix_missing_brackets(text: str) -> tuple[str, bool]:
    """Add missing closing brackets/braces.

    Returns (fixed_text, was_closed).
    """
    stack: list[str] = []
    in_string = False
    escape_next = False

    for ch in text:
        if escape_next:
            escape_next = False
            continue
        if ch == "\\":
            if in_string:
                escape_next = True
            continue
        if ch == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch == "{":
            stack.append("}")
        elif ch == "[":
            stack.append("]")
        elif ch in ("}", "]"):
            if stack and stack[-1] == ch:
                stack.pop()

    if stack:
        return text + "".join(reversed(stack)), True

    return text, False
