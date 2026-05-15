"""Tool args summarization for observation natural-language display.

``ContextManager`` renders observation records with a ``[<tool>] <args>``
header — :func:`summarize_tool_args` produces the short args label by
branching on tool name. The function lives under ``tools/`` rather than
``context/`` (where it used to live, dragging a manager → plugin lazy
import behind it) because the branching is on **tool name**, not on
wire format or message role.

A sibling ``summarize_action_args`` used to live here for assistant-emission
display. It was retired when the wire-format ``render_assistant_from_history``
moved to round-trip the structured record back to the JSON wire shape
instead of synthesising a natural-language summary — assistant emissions
no longer need a "→ action(...)" string. Observations still do, hence
this module's continued (single-function) existence.
"""

from __future__ import annotations


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
