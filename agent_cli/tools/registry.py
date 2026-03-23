"""Tool schema registry and input validation."""

from __future__ import annotations

from typing import Any

import json

from dataclasses import dataclass


@dataclass
class ToolSchema:
    name: str
    description: str
    parameters: dict  # JSON Schema


TOOL_SCHEMAS: dict[str, ToolSchema] = {
    "read_file": ToolSchema(
        name="read_file",
        description="Read file contents. Lines are tagged as LINE#HASH:content for editing. "
        "Use line_start/line_end for partial reads (1-based, inclusive).",
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File path to read"},
                "line_start": {
                    "type": "integer",
                    "description": "Start line number (1-based). Omit to read from beginning.",
                },
                "line_end": {
                    "type": "integer",
                    "description": "End line number (1-based, inclusive). Omit to read to end.",
                },
            },
            "required": ["path"],
        },
    ),
    "write_file": ToolSchema(
        name="write_file",
        description="Create or overwrite a file at the given path with raw content.",
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File path to save"},
                "content": {"type": "string", "description": "File content"},
            },
            "required": ["path", "content"],
        },
    ),
    "edit_file": ToolSchema(
        name="edit_file",
        description=(
            "Edit a file using hashline refs from read_file. "
            "Ops: replace, append, prepend. lines=[] to delete."
        ),
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File path"},
                "edits": {
                    "type": "array",
                    "description": "List of edit operations",
                    "items": {
                        "type": "object",
                        "properties": {
                            "op": {"type": "string"},
                            "pos": {"type": "string"},
                            "end": {"type": "string"},
                            "lines": {"type": "array", "items": {"type": "string"}},
                        },
                        "required": ["op", "pos"],
                    },
                },
            },
            "required": ["path", "edits"],
        },
    ),
    "shell": ToolSchema(
        name="shell",
        description="Run a shell command and return stdout/stderr.",
        parameters={
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "Shell command to run"},
                "timeout": {
                    "type": "integer",
                    "description": "Timeout in seconds (default 30)",
                },
            },
            "required": ["command"],
        },
    ),
    "complete": ToolSchema(
        name="complete",
        description="Call this tool when the task is done. Provide the final result.",
        parameters={
            "type": "object",
            "properties": {
                "result": {
                    "type": "string",
                    "description": "The final result or answer",
                },
            },
            "required": ["result"],
        },
    ),
    "read_context": ToolSchema(
        name="read_context",
        description="Read context from previous sessions. "
        "Use mode='list' to see session list, mode='detail' with session_id to read full log.",
        parameters={
            "type": "object",
            "properties": {
                "mode": {
                    "type": "string",
                    "description": "list or detail",
                },
                "session_id": {
                    "type": "string",
                    "description": "Session ID (required for mode=detail)",
                },
            },
            "required": ["mode"],
        },
    ),
    "ask": ToolSchema(
        name="ask",
        description="Ask the user a question and wait for their response. "
        "Use this when you need clarification or additional information.",
        parameters={
            "type": "object",
            "properties": {
                "question": {
                    "type": "string",
                    "description": "The question to ask the user",
                },
            },
            "required": ["question"],
        },
    ),
}


DELEGATE_TOOL_SCHEMA = ToolSchema(
    name="delegate",
    description=(
        "Delegate a self-contained subtask to an independent subagent. "
        "The subagent has NO context from this conversation — the task "
        "description must include ALL necessary details."
    ),
    parameters={
        "type": "object",
        "properties": {
            "task": {
                "type": "string",
                "description": "Fully self-contained task description",
            },
        },
        "required": ["task"],
    },
)


def _convert_tools(
    tool_names: list[str],
    include_delegate: bool,
    formatter,
) -> list[dict]:
    """Shared logic for converting tool schemas to provider-specific format."""
    schemas = [TOOL_SCHEMAS[n] for n in tool_names if n in TOOL_SCHEMAS]
    if include_delegate:
        schemas.append(DELEGATE_TOOL_SCHEMA)
    return [formatter(s) for s in schemas]


def convert_to_anthropic_tools(
    tool_names: list[str], include_delegate: bool = False
) -> list[dict]:
    """Convert tool schemas to Anthropic API tool format."""
    return _convert_tools(
        tool_names,
        include_delegate,
        lambda s: {
            "name": s.name,
            "description": s.description,
            "input_schema": s.parameters,
        },
    )


def convert_to_openai_tools(
    tool_names: list[str], include_delegate: bool = False
) -> list[dict]:
    """Convert tool schemas to OpenAI API tool format."""
    return _convert_tools(
        tool_names,
        include_delegate,
        lambda s: {
            "type": "function",
            "function": {
                "name": s.name,
                "description": s.description,
                "parameters": s.parameters,
            },
        },
    )


def get_tool_descriptions(
    tool_names: list[str] | None = None,
    include_delegate: bool = False,
) -> str:
    """Generate tool description text for system prompt.

    Args:
        tool_names: Filter to specific tools. None = all tools.
        include_delegate: Whether to include delegate tool.
    """
    names = tool_names if tool_names is not None else list(TOOL_SCHEMAS.keys())
    lines = []
    for name in names:
        schema = TOOL_SCHEMAS.get(name)
        if schema is None:
            continue
        params_str = json.dumps(
            {
                k: v.get("description", v.get("type", ""))
                for k, v in schema.parameters.get("properties", {}).items()
            },
        )
        lines.append(f"- {name}: {schema.description}\n  Input JSON: {params_str}")
    if include_delegate:
        lines.append(
            f"- delegate: {DELEGATE_TOOL_SCHEMA.description}\n"
            f'  Input JSON: {{"task": "fully self-contained task description"}}'
        )
    return "\n".join(lines)


def validate_tool_input(tool_name: str, action_input: Any) -> tuple[bool, str | None]:
    """Validate action_input against tool schema.

    Returns (True, None) on success, (False, error_message) on failure.
    Attempts auto-conversion of string inputs to dict.
    """
    schema = TOOL_SCHEMAS.get(tool_name)
    if schema is None:
        return (
            False,
            f"Unknown tool: '{tool_name}'. Available: {', '.join(TOOL_SCHEMAS)}",
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
                return False, (
                    f"action_input for '{tool_name}' must be a JSON object, "
                    f"got string: {action_input!r}"
                )

    if not isinstance(action_input, dict):
        return False, (
            f"action_input for '{tool_name}' must be a JSON object, "
            f"got {type(action_input).__name__}"
        )

    # Check required fields
    required = schema.parameters.get("required", [])
    missing = [f for f in required if f not in action_input]
    if missing:
        return False, (
            f"Missing required field(s) for '{tool_name}': {', '.join(missing)}. "
            f"Expected: {json.dumps(schema.parameters, indent=2)}"
        )

    # Type validation + auto-coercion
    properties = schema.parameters.get("properties", {})
    for key, value in list(action_input.items()):
        if key not in properties:
            continue
        expected_type = properties[key].get("type")
        if expected_type and not _check_type(value, expected_type):
            coerced = _try_coerce(value, expected_type)
            if coerced is not None:
                action_input[key] = coerced
            else:
                return False, (
                    f"Field '{key}' for '{tool_name}' expected {expected_type}, "
                    f"got {type(value).__name__}: {value!r}"
                )

    return True, None


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
    if expected_type == "string" and isinstance(value, (int, float)):
        return str(value)
    return None
