"""ReAct wire format — the default plugin.

Self-contained module owning every string and behavior that defines the
ReAct wire format: format rules text, recovery wording, parser
adaptation, and lifecycle hooks. Removing this file would remove ReAct
support entirely (with no edits anywhere else needed) — the boundary
the plugin system promises.

During the multi-step refactor (Steps 2 → 5) the same strings still
live in ``prompts/system_prompt.FORMAT_RULES``, ``constants.py``, and
``recovery/primitives.py`` because callers haven't been migrated yet.
That duplication is intentional and short-lived: each later step
removes one of those legacy locations as its caller switches to
``wire_format.<method>()``. The final state has every string in this
module only.
"""

from __future__ import annotations

import json

from agent_cli.parsing.react_parser import parse_react
from agent_cli.recovery.intervention import Intervention
from agent_cli.recovery.primitives import echo_prior_output
from agent_cli.tools.action_summary import summarize_action_args
from agent_cli.wire_formats.base import ParsedAction


# ── Prompt section ───────────────────────────────────────────
# Mirrors ``prompts/system_prompt.FORMAT_RULES`` verbatim. Will become
# the single source of truth in Step 4 when ``build_system_prompt``
# switches to ``wire_format.format_rules()``.
_FORMAT_RULES = """\
## Response Format
You MUST output a single JSON object only — no markdown fences, no surrounding
text, no `observation` field (it is injected by the system):

{"thought": "your reasoning", "action": "tool_name", "action_input": {...}}

When the task is done, first verify with `ready_for_review`, then call `complete`:
{"thought": "summary of what I did", "action": "ready_for_review", "action_input": {"summary": "..."}}
{"thought": "confirmed all requirements met", "action": "complete", "action_input": {"result": "..."}}

Rules:
1. `thought` MUST state purpose (what you want to achieve) and reason (why this action).
2. `action_input` MUST match the tool's input schema.
3. If an observation shows an error, fix parameters and retry.
4. Exactly ONE action per turn. Do not use an `actions` array or list in `action` —
   multiple tools = multiple turns; each turn's observation informs the next.
5. Make that one action count — pick the most efficient path:
   - Use batch input fields (`edit_file.edits`, `delegate.tasks`) instead of repeating the same tool across turns.
   - Combine shell operations into a single call (pipelines, multi-file surveys, batch listings) — one shell call often replaces many `read_file` turns.
   - Pick the narrowest read mode that answers the question (search > targeted line range > full file).
   - Do not "peek" with one tool only to redo the work with another.
6. Respond in the user's language."""


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


class ReActFormat:
    """Reference plugin — preserves pre-plugin behavior.

    Inner shape: ``{"thought": ..., "action": ..., "action_input": ...}``.
    Parser: 3-stage fallback (``parse_react``). Recovery: the
    long-standing ``constrain_format_json`` / ``constrain_action_required``
    text. Provider quirks: none (JSON mode stays active when
    capabilities support it).

    The class deliberately has no constructor parameters: registration
    is a single ``register(ReActFormat())`` call at module import time
    (see ``agent_cli/wire_formats/__init__.py``).
    """

    name = "react"
    thought_required = True

    # ─── Prompt ────────────────────────────────────────────────

    def format_rules(self) -> str:
        return _FORMAT_RULES

    def wrap_action_input_example(self, action: str, args_json: str, idval: str) -> str:
        # Inline tool guide example — show ONLY the action_input dict.
        # ReAct's surrounding ``{"thought","action","action_input"}``
        # envelope is described in ``format_rules``; the model's prior
        # already wraps. Identity preserves legacy guide output.
        return args_json

    def wrap_full_call_example(self, action: str, args_json: str, idval: str) -> str:
        # Skill/agent invocation example — must show action + action_input
        # so the reader knows which tool to call. Returns the legacy
        # bare-ReAct literal (``{"action":...,"action_input":...}``)
        # without ``thought`` because skill/agent doc examples have
        # historically omitted thought (it's the user's reasoning,
        # not part of the invocation template).
        return f'{{"action": "{action}", "action_input": {args_json}}}'

    # ─── Parsing ───────────────────────────────────────────────

    def parse(self, llm_text: str) -> ParsedAction:
        # Adapter: keep ``parse_react``'s ReActResult internal to this
        # module and expose only the ``ParsedAction`` boundary type.
        # The two dataclasses share field names by design; the copy is
        # field-by-field rather than attribute alias so future drift
        # (e.g. ReActResult gaining an internal-only field) doesn't
        # leak across the boundary.
        r = parse_react(llm_text)
        return ParsedAction(
            thought=r.thought,
            action=r.action,
            action_input=r.action_input,
            raw=r.raw,
            parse_stage=r.parse_stage,
            thinking=r.thinking,
            truncated=r.truncated,
        )

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

    def system_user_prefixes(self) -> tuple[str, ...]:
        return _SYSTEM_USER_PREFIXES

    # ─── ReAct-only recovery (NO_THOUGHT) ──────────────────────
    # Not part of the WireFormat Protocol — only plugins with
    # ``thought_required=True`` emit this intervention, and the loop
    # gates the call on that flag. Envelope plugins set
    # ``thought_required=False`` and never reach this method. Adding it
    # to the Protocol would force every plugin to implement a no-op,
    # so duck typing wins here.

    def format_no_thought_retry(self, *, prior_content: str = "") -> Intervention:
        """Build the Intervention when an action was emitted without
        the required ``thought`` field.

        Same failure-grounding shape as the format_no_*_retry builders
        in ``recovery/builders``: echo the prior output so the model
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

    # ─── Provider / lifecycle ──────────────────────────────────

    def prefill(self) -> str:
        # No prefill — ReAct's prior produces ReAct shape on its own.
        # Envelope plugins override this to force their wire shape from
        # the first generated token.
        return ""

    def provider_call_kwargs(self) -> dict:
        # ReAct is fully JSON-shaped; keep capability-driven
        # ``format=json`` mode active by passing no overrides.
        return {}

    # ─── History / context-window policy ──────────────────────
    # ``normalize_assistant_for_messages`` (H3) and
    # ``serialize_assistant_for_history`` (H2) are the sole owners of
    # their respective conversions. ``render_assistant_from_history``
    # still has body duplication with ``manager._to_natural_language``
    # (assistant branch); H4 will switch that call site and H5 will
    # remove the duplicate.

    def normalize_assistant_for_messages(self, raw: str) -> str:
        # ReAct: raw IS the on-the-wire shape, so identity preserves
        # self-reinforcement (the model's prior teaches the format
        # we want it to keep emitting). Envelope plugins re-render
        # to repair drift at the boundary.
        return raw

    def serialize_assistant_for_history(self, raw_text: str) -> dict:
        # JSON-parse the ReAct emission and surface
        # ``thought / action / action_input`` as top-level fields so
        # ``manager._to_natural_language`` can dispatch on them. Falls
        # back to bare ``content`` when the text isn't parseable so
        # corrupt emissions still survive in the log for postmortem.
        try:
            data = json.loads(raw_text)
            if isinstance(data, dict) and ("thought" in data or "action" in data):
                data["role"] = "assistant"
                return data
        except (json.JSONDecodeError, TypeError):
            pass
        return {"role": "assistant", "content": raw_text}

    def render_assistant_from_history(self, record: dict) -> dict:
        # Mirror the assistant branch of ``manager._to_natural_language``.
        # Returns a chat-completion-shaped ``{"role": "assistant",
        # "content": …}`` dict; the content is a natural-language
        # summary so the post-overflow / post-resume model still
        # understands what happened, at the cost of losing wire-format
        # self-reinforcement at the boundary.
        thought = record.get("thought", "")
        action = record.get("action", "")
        action_input = record.get("action_input", {})

        if action == "complete":
            result = ""
            if isinstance(action_input, dict):
                result = action_input.get("result", "")
            elif isinstance(action_input, str):
                result = action_input
            if thought:
                content = f"thought: {thought}\n\n{result}"
            else:
                content = result
            return {"role": "assistant", "content": content.strip()}

        if action:
            args_summary = summarize_action_args(action, action_input)
            parts = []
            if thought:
                parts.append(f"thought: {thought}")
            parts.append(f"action: {action}({args_summary})")
            return {"role": "assistant", "content": "\n".join(parts)}

        content = record.get("content", thought)
        return {"role": "assistant", "content": content}
