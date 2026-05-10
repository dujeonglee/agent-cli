"""Tool action / args summarization helpers for natural-language display.

Both helpers boil down a tool invocation to a short, single-line label —
the ``read_file(path)`` / ``[shell] cmd`` style that ``ContextManager``
uses when rendering history records to the LLM and that wire-format
plugins use when rendering an assistant turn.

The branches here are keyed on **tool name**, not on wire format or
message role. That is why the helpers live under ``tools/`` rather than
under ``context/`` (where they used to live, dragging a manager →
plugin lazy import behind them) or under ``wire_formats/`` (where they
would create a cross-package import for a function that has nothing to
do with the on-the-wire shape).

Two near-identical functions exist because the two callers see the
input dict under slightly different keys / shapes:

  - :func:`summarize_action_args` is called against an assistant
    emission ``{"action": "<tool>", "action_input": {...}}``. ``delegate``
    here carries a ``tasks`` list and ``run_skill`` carries optional
    ``arguments`` text — both are emission-side concepts.
  - :func:`summarize_tool_args` is called against an observation record
    ``{"tool": "<tool>", "args": {...}}``. ``delegate`` here is the
    already-resolved single ``agent`` string and ``run_skill`` is just
    the ``name``.

Unifying the two would require collapsing those shape differences and is
a separate concern from the H4 layering cleanup; tracked as future work.
"""

from __future__ import annotations


def summarize_action_args(action: str, action_input) -> str:
    """Summarize ``action_input`` for the ``→ action(...)`` display."""
    if not isinstance(action_input, dict):
        return str(action_input)[:80] if action_input else ""

    if action in ("read_file", "write_file", "edit_file"):
        return action_input.get("path", "")
    if action == "shell":
        cmd = action_input.get("command", "")
        return cmd[:60] if cmd else ""
    if action == "delegate":
        tasks = action_input.get("tasks", [])
        if tasks and isinstance(tasks, list):
            first = tasks[0] if isinstance(tasks[0], dict) else {}
            agent = first.get("agent", "")
            task = first.get("task", "")[:40]
            if len(tasks) > 1:
                return f'{agent}, "{task}" +{len(tasks) - 1} more'
            return f'{agent}, "{task}"'
        return ""
    if action == "run_skill":
        name = action_input.get("name", "")
        arguments = action_input.get("arguments", "")
        return f"{name}({arguments})" if arguments else name

    for v in action_input.values():
        if isinstance(v, str) and v:
            return v[:60]
    return ""


def summarize_tool_args(tool: str, args: dict) -> str:
    """Summarize tool ``args`` for the ``[{tool}]`` observation header."""
    if tool in ("read_file", "write_file", "edit_file"):
        return args.get("path", "")
    if tool == "shell":
        return args.get("command", "")[:60]
    if tool == "delegate":
        return args.get("agent", "")
    if tool == "run_skill":
        return args.get("name", "")
    for v in args.values():
        if isinstance(v, str) and v:
            return v[:60]
    return ""
