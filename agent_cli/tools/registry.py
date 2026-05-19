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
        description=(
            "Read file contents. Lines are tagged as LINE#HASH:content for editing. "
            "For unknown/large files, start with stat=true to inspect size before reading. "
            "stat returns metadata (line count, file size) and is not a read — follow it with a full read, line_start/line_end range, or search. "
            "Use search='keyword' to find targeted content without reading the whole file. "
            "Use line_start/line_end for partial reads (1-based, inclusive). "
            "Bare full reads on large files (~300+ lines) are refused with a stat-style response that lists the available recovery options — follow them."
        ),
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File path to read"},
                "stat": {
                    "type": "boolean",
                    "description": "Metadata query only — returns line count, file size, and the first 20 lines. Not a substitute for reading: after stat, pick a real read mode (full / line_start+line_end / search).",
                },
                "search": {
                    "type": "string",
                    "description": "Regex pattern. Returns only matching lines with surrounding context. Efficient for targeted lookups.",
                },
                "context": {
                    "type": "integer",
                    "description": "Lines of context before/after each search match (default 5).",
                },
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
    "read_symbols": ToolSchema(
        name="read_symbols",
        description=(
            "Structure-aware reader for source/markdown files using tree-sitter. "
            "Two modes: mode='list' returns the file outline (functions, classes, "
            "methods, structs, typedefs, #defines, headings) with line ranges; "
            "mode='fetch' returns the body of one named symbol from that outline. "
            "When a name has both a declaration and a definition, the definition wins. "
            "Languages: Python (.py), JavaScript (.js/.jsx), TypeScript (.ts/.tsx), "
            "C/C++ (.c/.cpp/.h/.hpp/...), Markdown (.md). Other formats → use read_file."
        ),
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File path"},
                "mode": {
                    "type": "string",
                    "enum": ["list", "fetch"],
                    "description": "'list' for outline (default), 'fetch' for one symbol's body",
                },
                "name": {
                    "type": "string",
                    "description": (
                        "Required for mode='fetch'. Exact symbol name as shown by "
                        "mode='list' (e.g. 'Foo.bar', 'ns::Foo::bar', '## Setup')"
                    ),
                },
            },
            "required": ["path"],
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
        description="Read context from sessions. "
        "mode='list': session list. mode='search': structured keyword search "
        "(default current session; pass 'scope'/'sessions' to restrict). "
        "mode='fetch': retrieve full turn(s) at given loc (use search results' "
        "loc string verbatim; add 'range' to include adjacent turns).",
        parameters={
            "type": "object",
            "properties": {
                "mode": {
                    "type": "string",
                    "description": "list, search, or fetch",
                },
                "keyword": {
                    "type": "string",
                    "description": "Search keyword (required for mode=search)",
                },
                "scope": {
                    "type": "array",
                    "items": {
                        "type": "string",
                        "enum": [
                            "reasoning",
                            "tool",
                            "observation",
                            "query",
                        ],
                    },
                    "description": (
                        "Optional field filter for mode=search. "
                        "reasoning=assistant.thought, tool=action+input, "
                        "observation=tool results, query=user input. "
                        "Default: all four. Single string accepted (auto-promoted)."
                    ),
                },
                "sessions": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Optional session selector for mode=search. "
                        "Default: current session only. "
                        "Pass 'all' (single value) to search every session, "
                        "or specific session_id(s) to scope. "
                        "Single string accepted (auto-promoted)."
                    ),
                },
                "loc": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Required for mode=fetch. Location(s) returned by "
                        "search: '{session_id}/{rel_path}:{line_num}'. "
                        "Single string accepted (auto-promoted). Max 10 entries."
                    ),
                },
                "range": {
                    "type": "integer",
                    "description": (
                        "Optional for mode=fetch. Include +/-N adjacent turns "
                        "around each loc. Default 0 (target only). Max 5."
                    ),
                },
            },
            "required": ["mode"],
        },
    ),
    "ask": ToolSchema(
        name="ask",
        description=(
            "Ask the user one or more questions and WAIT for their reply. "
            "Use ONLY when you cannot proceed without specific input from the "
            "user — a missing requirement, an ambiguous instruction, or a "
            "decision among alternatives you cannot resolve yourself. "
            "DO NOT use for goodbyes, pleasantries, acknowledgements, or "
            "checking whether your answer was satisfactory — those are "
            "conversational closers and belong in `complete`. If you have "
            "nothing actionable that requires user input, end the turn with "
            "`complete`; the user can always reply if they want to continue."
        ),
        parameters={
            "type": "object",
            "properties": {
                "questions": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "List of questions to ask the user",
                },
            },
            "required": ["questions"],
        },
    ),
    "run_skill": ToolSchema(
        name="run_skill",
        description="Run a registered skill by name. Use this to invoke specialized "
        "prompt-based workflows like code review, optimization, or test generation.",
        parameters={
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Skill name (e.g. 'optimize', 'review-code', 'summarize', 'test')",
                },
                "arguments": {
                    "type": "string",
                    "description": "Arguments to pass to the skill (e.g. file path)",
                },
            },
            "required": ["name"],
        },
    ),
    "ready_for_review": ToolSchema(
        name="ready_for_review",
        description="Call this BEFORE complete to verify your work fulfills all requirements. "
        "The system will return the original request for you to review against. "
        "After reviewing, call complete if everything is done, or continue working if not.",
        parameters={
            "type": "object",
            "properties": {
                "summary": {
                    "type": "string",
                    "description": "Brief summary of what you accomplished",
                },
            },
            "required": ["summary"],
        },
    ),
    "fetch": ToolSchema(
        name="fetch",
        description="Fetch a web page and return its content as markdown. "
        "Supports recursive fetching of same-domain links via depth parameter. "
        "Full content returned inline; long pages are subject to the loop's "
        "FIFO context-budget eviction like any other observation.",
        parameters={
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "URL to fetch",
                },
                "depth": {
                    "type": "integer",
                    "description": "Recursive depth: 0 = current page only (default), 1+ = follow same-domain links",
                },
            },
            "required": ["url"],
        },
    ),
    "delegate": ToolSchema(
        name="delegate",
        description=(
            "Delegate tasks to subagents. "
            "Single task = sync, multiple tasks = parallel. "
            "Use context mode to control what the subagent knows."
        ),
        parameters={
            "type": "object",
            "properties": {
                "tasks": {
                    "type": "array",
                    "description": "List of tasks. Single item = sync, multiple = parallel.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "task": {
                                "type": "string",
                                "description": "Task description for the subagent",
                            },
                            "context": {
                                "type": "string",
                                "enum": ["none", "fork"],
                                "description": "none (independent), fork (copy conversation history)",
                            },
                            "tools": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": "Allowed tools (omit for default set)",
                            },
                            "agent": {
                                "type": "string",
                                "description": "Agent name to load role/config from .agent-cli/agents/{name}.md",
                            },
                        },
                        "required": ["task"],
                    },
                },
            },
            "required": ["tasks"],
        },
    ),
}


# Tools always included in API tool list regardless of allowed_tools
_ALWAYS_INCLUDE = ("complete", "ready_for_review")


def _convert_tools(
    tool_names: list[str],
    formatter,
) -> list[dict]:
    """Shared logic for converting tool schemas to provider-specific format."""
    schemas = [TOOL_SCHEMAS[n] for n in tool_names if n in TOOL_SCHEMAS]
    # Always include essential tools
    for name in _ALWAYS_INCLUDE:
        if name not in tool_names and name in TOOL_SCHEMAS:
            schemas.append(TOOL_SCHEMAS[name])
    return [formatter(s) for s in schemas]


def convert_to_anthropic_tools(tool_names: list[str]) -> list[dict]:
    """Convert tool schemas to Anthropic API tool format."""
    return _convert_tools(
        tool_names,
        lambda s: {
            "name": s.name,
            "description": s.description,
            "input_schema": s.parameters,
        },
    )


def convert_to_openai_tools(tool_names: list[str]) -> list[dict]:
    """Convert tool schemas to OpenAI API tool format."""
    return _convert_tools(
        tool_names,
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
    inline_guides: dict[str, str] | None = None,
) -> str:
    """Generate tool description text for system prompt.

    Args:
        tool_names: Filter to specific tools. None = all tools.
        inline_guides: Map of tool name → extra guide text to append.

    Tools are ordered: always-present first (KV cache stable),
    conditional (edit_file, delegate) last.
    """
    guides = inline_guides or {}
    names = tool_names if tool_names is not None else list(TOOL_SCHEMAS.keys())
    # Always include essential tools in descriptions
    for t in _ALWAYS_INCLUDE:
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
        params_str = json.dumps(
            {
                k: v.get("description", v.get("type", ""))
                for k, v in schema.parameters.get("properties", {}).items()
            },
        )
        entry = f"- {name}: {schema.description}\n  Input JSON: {params_str}"
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
