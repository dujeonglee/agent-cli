"""Tool registry: collects Tool instances into dispatch + schema views.

Each tool now owns its own schema (``name`` / ``description`` /
``parameters``) and ``run`` on a :class:`~agent_cli.tools.base.Tool`
subclass. This module instantiates them once into ``TOOLS`` and derives
the schema-facing helpers (system-prompt descriptions, input validation,
action inference) from that single collection.

``TOOL_SCHEMAS`` is a back-compat alias for ``TOOLS``: callers that used
to read ``ToolSchema`` objects keep working because ``Tool`` instances
expose the same ``.name`` / ``.description`` / ``.parameters`` attributes.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import json

from agent_cli.tools.base import Tool
from agent_cli.tools.code_index import CodeIndexTool
from agent_cli.tools.context import ReadContextTool
from agent_cli.tools.delegate import DelegateTool
from agent_cli.tools.edit_file import EditFileTool
from agent_cli.tools.fetch import FetchTool
from agent_cli.tools.read_file import ReadFileTool
from agent_cli.tools.result import ToolResult
from agent_cli.tools.shell import ShellTool
from agent_cli.tools.virtual import (
    AskTool,
    CompleteTool,
    ReadyForReviewTool,
    RunSkillTool,
)
from agent_cli.tools.write_file import WriteFileTool

# Instantiated once. Insertion order is preserved into ``TOOLS`` (dict
# keeps order) and matches the historical ``TOOL_SCHEMAS`` ordering for
# KV-cache stability in the system prompt.
_ALL_TOOLS: list[Tool] = [
    ReadFileTool(),
    WriteFileTool(),
    EditFileTool(),
    ShellTool(),
    CodeIndexTool(),
    CompleteTool(),
    ReadContextTool(),
    AskTool(),
    RunSkillTool(),
    ReadyForReviewTool(),
    FetchTool(),
    DelegateTool(),
]

TOOLS: dict[str, Tool] = {t.name: t for t in _ALL_TOOLS}

# Back-compat alias — schema consumers (system prompt, MCP adapter, input
# validation) read .name/.description/.parameters, which Tool instances
# expose identically to the old ToolSchema dataclass.
TOOL_SCHEMAS: dict[str, Tool] = TOOLS


def _execute_tool(
    tool_name: str,
    action_input: dict,
    *,
    session_dir: Path | None = None,
) -> ToolResult:
    """Dispatch primitive — run a registered tool.

    Caller contract: ``tool_name`` MUST exist in ``TOOLS``. The loop's
    recovery layer (``detect_unknown_tool``) is the single source of
    truth for that validation; bad names never reach this function from
    the live loop. A ``KeyError`` on a missing name is the intended
    failure mode. ``session_dir`` is forwarded uniformly; tools that do
    not need it ignore the keyword.
    """
    return TOOLS[tool_name].run(action_input, session_dir=session_dir)


def infer_action(action_input: Any) -> str | None:
    """Recover a missing action name from the shape of *action_input*.

    When the wire format drops the action name (parse_stage 3 — a
    ``## Action`` header with an empty tool slot) but the input is a
    well-formed dict, each tool's :meth:`Tool.claims` predicate votes on
    whether the payload is its own. Returns the tool name iff **exactly
    one** tool claims it; ``None`` on 0 or 2+ matches (ambiguous → leave
    it to the normal NO_ACTION recovery).
    """
    if not isinstance(action_input, dict):
        return None
    hits = [name for name, tool in TOOLS.items() if tool.claims(action_input)]
    return hits[0] if len(hits) == 1 else None


# Tools always included in API tool list regardless of allowed_tools
_ALWAYS_INCLUDE = ("complete", "ready_for_review")


def _render_object_keys(obj_schema: dict) -> str:
    """Render an object schema's property keys as ``object{k1, k2?, ...}``.

    ``?`` suffixes keys NOT in the object's ``required`` list. Only key
    *names* are surfaced — their per-key descriptions stay in the inline
    guides, keeping this the authoritative compact schema and the guide
    the owner of usage prose. An object with no declared properties falls
    back to the bare type name.
    """
    props = obj_schema.get("properties", {})
    if not props:
        return obj_schema.get("type", "")
    req = set(obj_schema.get("required", []))
    keys = [k if k in req else f"{k}?" for k in props]
    return "object{" + ", ".join(keys) + "}"


def _render_type_spec(prop_schema: dict) -> str:
    """Render the shape that the old flattening dropped: type + item shape.

    - array of objects  → ``array<object{k1, k2?, ...}>``
    - array of scalars  → ``array<string>`` (or bare ``array`` if unknown)
    - object            → ``object{k1, k2?, ...}``
    - scalar            → ``string`` / ``integer`` / ...
    """
    t = prop_schema.get("type", "")
    if t == "array":
        items = prop_schema.get("items", {})
        inner = (
            _render_object_keys(items)
            if items.get("type") == "object"
            else items.get("type", "")
        )
        return f"array<{inner}>" if inner else "array"
    if t == "object":
        return _render_object_keys(prop_schema)
    return t


def render_param_value(prop_schema: dict, required: bool) -> str:
    """Render one JSON-Schema property into the prompt's ``Input JSON`` value.

    Shape: ``<type-spec>[, required] — <description>``. Surfaces the
    ``type``, the ``required`` marker, and (for arrays/objects) the nested
    item-key shape — all three of which the previous
    ``v.get("description", v.get("type", ""))`` flattening discarded.

    Shared by :func:`get_tool_descriptions` (native tools) and the MCP
    adapter so both tool surfaces render identically. MCP keys are not
    prefixed; this helper operates on the property schema only and is
    agnostic to the key namespace.
    """
    type_spec = _render_type_spec(prop_schema)
    head_parts = [p for p in (type_spec, "required" if required else "") if p]
    head = ", ".join(head_parts)
    desc = prop_schema.get("description", "")
    if head and desc:
        return f"{head} — {desc}"
    return head or desc


def _strip_own_prefix(name: str, text: str) -> str:
    """Strip a tool's own wire-key prefix (``{name}_``) from text — used for
    multi-op formats whose convention is flat ``{action, plain-keys}`` rather
    than ``{name}_param``. Only the tool's own prefix is touched (so the bare
    tool name, e.g. ``read_file``, is preserved)."""
    return text.replace(f"{name}_", "")


def _multi_op_flat_params(name: str, props: dict, required: set) -> dict:
    """Render a batch tool's params as its FLAT single-op shape for multi-op
    formats.

    A batch tool's schema declares its native (batch) interface: a single
    array param (``read_file_reads``, ``code_index_queries``,
    ``delegate_tasks``) whose items are the per-call shape, sometimes beside a
    scalar (``edit_file_path``). Under a multi-op format one op IS one item,
    so the prompt must advertise the item fields at the top level — NOT the
    array wrapper. This unwraps the array param into its item-object
    properties and keeps scalars (prefix-stripped), exactly mirroring
    :meth:`Tool.wrap_single_op`'s flat→batch mapping. The item schema already
    lives in ``items.properties``, so no per-tool declaration is needed.

    Not doing this is what let the 27B copy the advertised ``reads`` array and
    emit the old ``read_file_reads`` wrapper under md_array (DESIGN Exp 8).
    """
    out: dict[str, str] = {}
    for k, v in props.items():
        items = v.get("items") if v.get("type") == "array" else None
        if isinstance(items, dict) and items.get("properties"):
            item_required = set(items.get("required", []))
            for ik, iv in items["properties"].items():
                out[ik] = render_param_value(iv, ik in item_required)
        else:
            out[_strip_own_prefix(name, k)] = render_param_value(v, k in required)
    return out


# Batch-framing sentences to drop from a tool's description under multi-op
# formats (one op = one item, so "pass a list" / "in a single call" guidance
# is wrong). Keyed by tool name; applied AFTER the wire-prefix strip, so the
# match text uses the stripped param names (``reads``, not ``read_file_reads``).
# ``TestMultiOpToolDescriptions`` asserts no batch phrasing survives, so a
# description edit that outdates a key fails loudly instead of silently
# leaking the old shape.
_MULTI_OP_DESC_REWRITES = {
    # read_file is flat-native (Step 3): its description is already the plain
    # single-file shape, so no batch-phrasing rewrite is needed. The remaining
    # batch tools (code_index, edit_file, delegate) still declare array
    # interfaces and rewrite their "pass a list" prose here.
    "code_index": [
        (
            "Provide queries as a LIST; each item is one query with its own "
            "mode. One call can run many queries (modes may be mixed). For a "
            "single query, pass a one-element list.",
            "Each op runs one query.",
        ),
        ("index queries via", "index query via"),
        ("Modes (per item):", "Modes:"),
    ],
}


def get_tool_descriptions(
    tool_names: list[str] | None = None,
    inline_guides: dict[str, str] | None = None,
    wire_format=None,
) -> str:
    """Generate tool description text for system prompt.

    Args:
        tool_names: Filter to specific tools. None = all tools.
        inline_guides: Map of tool name → extra guide text to append.
        wire_format: the active ``WireFormat``. When it is multi-op, each
            tool's own wire-key prefix is stripped from its description and
            param keys (flat ``{action, params}`` convention); when it does not
            expose ``complete``, that tool is omitted from the always-included
            set. ``None`` keeps the default (prefixed, ``complete`` shown).

    Tools are ordered: always-present first (KV cache stable),
    conditional (edit_file, delegate) last.
    """
    guides = inline_guides or {}
    multi_op = bool(getattr(wire_format, "multi_op", False))
    exposes_complete = getattr(wire_format, "exposes_complete", True)
    names = tool_names if tool_names is not None else list(TOOL_SCHEMAS.keys())
    # Always include essential tools — except `complete` when the format does
    # not expose it (it completes another way, e.g. a thought-only terminal).
    for t in _ALWAYS_INCLUDE:
        if t == "complete" and not exposes_complete:
            names = [n for n in names if n != "complete"]
            continue
        if t not in names:
            names = [*names, t]

    # Partition: static tools first, conditional tools last
    conditional = {"edit_file", "delegate"}
    static_names = [n for n in names if n not in conditional]
    cond_names = [n for n in names if n in conditional]
    ordered = static_names + cond_names

    lines = []
    for name in ordered:
        schema = TOOL_SCHEMAS.get(name)
        if schema is None:
            continue
        props = schema.parameters.get("properties", {})
        required = set(schema.parameters.get("required", []))
        if multi_op:
            params = _multi_op_flat_params(name, props, required)
        else:
            params = {k: render_param_value(v, k in required) for k, v in props.items()}
        params_str = json.dumps(params, ensure_ascii=False)
        if multi_op:
            description = _strip_own_prefix(name, schema.description)
            for old, new in _MULTI_OP_DESC_REWRITES.get(name, []):
                description = description.replace(old, new)
        else:
            description = schema.description
        if not exposes_complete:
            # ready_for_review's description sequences itself before
            # `complete`; without that tool the reference would teach a
            # nonexistent call, so rephrase the ending neutrally.
            description = description.replace(
                "Call this BEFORE complete to verify",
                "When you believe the task is done, call this to verify",
            )
        entry = f"- {name}: {description}\n  Input JSON: {params_str}"
        if name in guides:
            entry += guides[name]
        lines.append(entry)
    return "\n".join(lines)


def validate_tool_input(
    tool_name: str, action_input: Any
) -> tuple[bool, str | None, Any]:
    """Validate action_input against tool schema.

    Returns (True, None, converted_input) on success,
    (False, error_message, original_input) on failure.
    Attempts auto-conversion of string inputs to dict.
    """
    schema = TOOL_SCHEMAS.get(tool_name)
    if schema is None:
        return (
            False,
            f"Unknown tool: '{tool_name}'. Available: {', '.join(TOOL_SCHEMAS)}",
            action_input,
        )

    # Auto-convert string to dict (common small model error)
    if isinstance(action_input, str):
        try:
            action_input = json.loads(action_input)
        except (json.JSONDecodeError, ValueError):
            # Try treating as the first required param
            required = schema.parameters.get("required", [])
            if required:
                action_input = {required[0]: action_input}
            else:
                return (
                    False,
                    (
                        f"action_input for '{tool_name}' must be a JSON object, "
                        f"got string: {action_input!r}"
                    ),
                    action_input,
                )

    if not isinstance(action_input, dict):
        return (
            False,
            (
                f"action_input for '{tool_name}' must be a JSON object, "
                f"got {type(action_input).__name__}"
            ),
            action_input,
        )

    # Check required fields
    required = schema.parameters.get("required", [])
    missing = [f for f in required if f not in action_input]
    if missing:
        return (
            False,
            (
                f"Missing required field(s) for '{tool_name}': {', '.join(missing)}. "
                f"Expected: {json.dumps(schema.parameters, indent=2)}"
            ),
            action_input,
        )

    # Strip empty strings from optional fields (LLMs send "" for omitted params)
    properties = schema.parameters.get("properties", {})
    for key in list(action_input.keys()):
        if key not in required and action_input[key] == "":
            del action_input[key]

    # Type validation + auto-coercion
    for key, value in list(action_input.items()):
        if key not in properties:
            continue
        expected_type = properties[key].get("type")
        if expected_type and not _check_type(value, expected_type):
            coerced = _try_coerce(value, expected_type)
            if coerced is not None:
                action_input[key] = coerced
            else:
                return (
                    False,
                    (
                        f"Field '{key}' for '{tool_name}' expected {expected_type}, "
                        f"got {type(value).__name__}: {value!r}"
                    ),
                    action_input,
                )

    return True, None, action_input


# Type mapping for validation
_TYPE_MAP = {
    "string": str,
    "integer": int,
    "number": (int, float),
    "array": list,
    "object": dict,
    "boolean": bool,
}


def _check_type(value: Any, expected_type: str) -> bool:
    """Check if value matches expected JSON Schema type."""
    py_type = _TYPE_MAP.get(expected_type)
    if py_type is None:
        return True  # unknown type, skip check
    return isinstance(value, py_type)


def _try_coerce(value: Any, expected_type: str) -> Any | None:
    """Try to coerce value to expected type. Returns None on failure."""
    if expected_type == "integer" and isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return None
    if expected_type == "number" and isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    if expected_type == "array" and isinstance(value, dict):
        return [value]  # single dict → [dict]
    if expected_type == "array" and isinstance(value, str):
        return [value]  # single str → [str]  (lenient for "scope": "x" patterns)
    if expected_type == "string" and isinstance(value, (int, float)):
        return str(value)
    return None
