"""Compose the system prompt's ``## Response Format`` section.

Every wire-format plugin contributes the same logical pieces of the
section (anchor line, schema example, completion examples, two
field-specific rules), and the rest is shared text. Hard-coding the
whole section per plugin lets drift creep in — small word choices and
placeholder patterns vary across plugins, polluting any side-by-side
measurement of model behaviour. This module is the single seam that
keeps the *common* parts identical and lets plugins control only the
parts that genuinely depend on their wire shape.

Plugin contract used by :func:`build_format_rules`:

  - ``format_rules_anchor() -> str``
        First line of the section after the heading. Tells the model
        what the wire shape is in one sentence.
  - ``render_full_example(*, thought, action, action_input) -> str``
        Render one example of the wire shape carrying the given
        logical fields. ``thought=None`` means "invocation only" and
        the plugin chooses how to handle the absent reasoning slot
        (ReAct omits the field, envelope inserts a short placeholder).
  - ``format_rules_field_specific() -> str``
        Lines for Rules 1 and 2 — the field names ("thought" /
        "reasoning text", "action_input" / "JSON dict") differ by
        wire shape, but the obligation each rule expresses is the
        same logical content.

The example *inputs* are constants in this module. Every plugin sees
the same ``(thought, action, action_input)`` triples; only the
rendering differs. That is what makes the resulting prompts comparable.
"""

from __future__ import annotations


# ── Shared text (same bytes for every plugin) ────────────────

COMPLETION_INTRO = (
    "When the task is done, first verify with `ready_for_review`, then call `complete`:"
)

SHARED_RULES_TAIL = """\
3. If an observation shows an error, fix parameters and retry.
4. Exactly ONE action per turn — multiple tools = multiple turns; each turn's observation informs the next.
5. Make that one action count — pick the most efficient path:
   - Use batch input fields (`edit_file.edits`, `delegate.tasks`) instead of repeating the same tool across turns.
   - Combine shell operations into a single call (pipelines, multi-file surveys, batch listings) — one shell call often replaces many `read_file` turns.
   - Pick the narrowest read mode that answers the question (search > targeted line range > full file).
   - Do not "peek" with one tool only to redo the work with another.
6. Respond in the user's language."""


# ── Example inputs (shared logical content) ──────────────────
# Each plugin's ``render_full_example`` is called with these exact
# triples. The output may differ wildly per wire shape, but the input
# does not — measurement runs see the same intent every time.

SCHEMA_EXAMPLE_INPUT = {
    "thought": "your reasoning",
    "action": "tool_name",
    "action_input": "{...}",
}

READY_FOR_REVIEW_EXAMPLE_INPUT = {
    "thought": "summary of what I did",
    "action": "ready_for_review",
    "action_input": '{"summary": "..."}',
}

COMPLETE_EXAMPLE_INPUT = {
    "thought": "confirmed all requirements met",
    "action": "complete",
    "action_input": '{"result": "..."}',
}


# ── Builder ──────────────────────────────────────────────────


def build_format_rules(plugin) -> str:
    """Assemble the ``## Response Format`` section for one plugin.

    The plugin renders the parts that depend on its wire shape; this
    function provides the surrounding structure and the shared text.
    See module docstring for the contract the plugin must satisfy.
    """
    schema = plugin.render_full_example(**SCHEMA_EXAMPLE_INPUT)
    rfr = plugin.render_full_example(**READY_FOR_REVIEW_EXAMPLE_INPUT)
    complete = plugin.render_full_example(**COMPLETE_EXAMPLE_INPUT)

    return (
        "## Response Format\n"
        f"{plugin.format_rules_anchor()}\n"
        "\n"
        f"{schema}\n"
        "\n"
        f"{COMPLETION_INTRO}\n"
        f"{rfr}\n"
        f"{complete}\n"
        "\n"
        "Rules:\n"
        f"{plugin.format_rules_field_specific()}\n"
        f"{SHARED_RULES_TAIL}"
    )
