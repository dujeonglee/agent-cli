"""Conditional system prompt builder adapted to model capabilities.

Layout (optimized for LLM attention):
  Primacy  — Role, Task Guidelines, Format Rules
  Middle   — Available Tools (guides inlined), Skills
  Recency  — Session, Environment, Directives
"""

from __future__ import annotations

import platform
from datetime import datetime
from pathlib import Path

from agent_cli.providers.compat import ModelCapabilities
from agent_cli.tools.registry import get_tool_descriptions

# ── DIRECTIVE.md budget ──────────────────────────
MAX_DIRECTIVE_FILE_CHARS = 4000
MAX_DIRECTIVE_TOTAL_CHARS = 8000

# ── DIRECTIVE.md search paths ────────────────────
_DIRECTIVE_PATHS = [
    Path.cwd() / ".agent-cli" / "DIRECTIVE.md",
    Path.home() / ".agent-cli" / "DIRECTIVE.md",
]

# ── Section 1: Role ──────────────────────────────
ROLE_PROMPT = """\
You are an AI assistant that solves tasks step-by-step using available tools."""

# ── Section 2: Task Guidelines ───────────────────
TASK_GUIDELINES = """\
## Task Guidelines
- Read relevant code before changing it. Do not modify files you have not read.
- Keep changes tightly scoped to the request. Do not add unrelated cleanup or refactoring.
- Do not create new files unless required to complete the task.
- If an approach fails, diagnose the cause before switching tactics.
- Be careful not to introduce security vulnerabilities (command injection, path traversal, etc.).
- Report outcomes honestly — if verification failed or was not run, say so explicitly."""

# ── Section 3: Format Rules ──────────────────────
FORMAT_RULES = """\
## Response Format
You MUST respond with a single JSON object and nothing else.
No markdown fences, no extra text — ONLY the JSON object.

{"thought": "your reasoning", "action": "tool_name", "action_input": {...}}

When the task is done, first call "ready_for_review" to verify:
{"thought": "summary of what I did", "action": "ready_for_review", "action_input": {"summary": "brief summary"}}

After reviewing, call "complete" to finish:
{"thought": "confirmed all requirements met", "action": "complete", "action_input": {"result": "your answer"}}

Rules:
1. Always include "thought" in your JSON.
2. "action_input" must match the tool's input schema.
3. If an observation shows an error, fix parameters and retry.
4. Respond in the same language as the user.
5. Do NOT include "observation" — it is injected by the system.
6. Output ONLY valid JSON, nothing else.
7. NEVER invoke yourself recursively — do NOT run agent-cli or any command that starts this tool again via shell.
8. Before calling complete, ALWAYS call ready_for_review first to verify your work."""

# ── Inline guides for tools ──────────────────────
_HASHLINE_INLINE = """\

  Hashline editing guide:
  read_file returns lines tagged as LINE#HASH:content, e.g.:
    1#VR:def hello():
    2#KT:    return "world"
    3#ZZ:
  Use edit_file with hashline refs copied EXACTLY from read_file output.
  - replace single line:  {"op": "replace", "pos": "2#KT", "lines": ["    return \\"hello\\""]}
  - replace range:        {"op": "replace", "pos": "1#VR", "end": "3#ZZ", "lines": ["def greet():", "    pass"]}
  - delete lines:         {"op": "replace", "pos": "2#KT", "lines": []}
  - insert after:         {"op": "append", "pos": "1#VR", "lines": ["    # new comment"]}
  - insert before:        {"op": "prepend", "pos": "1#VR", "lines": ["# header"]}
  - append to EOF:        {"op": "append", "lines": ["# end of file"]}
  IMPORTANT: Always read the file first to get current hashline tags.
  If a hash mismatch error occurs, re-read the file and retry with fresh tags.
  Use write_file only for creating NEW files, not for editing existing ones."""

_DELEGATE_INLINE = """\

  Always use the "tasks" array format. Single item = sync, multiple = parallel.
  Context modes per task:
  - "none" (default): subagent starts with no context. Task must be self-contained.
  - "fork": subagent receives a copy of the current conversation context.
  - "inherit": subagent shares your context directly (single task only, not parallel).
  - "tools": optionally restrict which tools the subagent can use.
  Note: inherit cannot be used with multiple tasks (parallel).
  Examples:
  - Single: {"tasks": [{"task": "Read /tmp/data.csv and count rows"}]}
  - With context: {"tasks": [{"task": "Fix the bug we found", "context": "fork"}]}
  - Parallel: {"tasks": [{"task": "Analyze A", "context": "fork"}, {"task": "Analyze B", "context": "fork"}]}
  - Read-only: {"tasks": [{"task": "Review changes", "context": "fork", "tools": ["read_file", "shell"]}]}\""""

_ARTIFACT_INLINE = """\

  A scratchpad tracks your task progress and decisions.
  Each tool result is saved as an artifact file on disk.
  The scratchpad Progress section shows what was done and where the artifact is stored.
  To load details from a previous step:
  - Read artifact: {"action": "read_artifact", "action_input": {"path": "artifacts/turn_0003.md"}}
  - List artifacts: {"action": "read_artifact", "action_input": {"mode": "list"}}
  - Search by tag:  {"action": "read_artifact", "action_input": {"mode": "search", "tag": "filename.py"}}
  Do NOT read all artifacts — only load what is needed for the current step."""

# Map tool names to their inline guides
_TOOL_INLINE_GUIDES: dict[str, str] = {
    "edit_file": _HASHLINE_INLINE,
    "delegate": _DELEGATE_INLINE,
    "read_artifact": _ARTIFACT_INLINE,
}


def _build_tools_section(
    active_tools: list[str],
    include_delegate: bool = False,
) -> str:
    """Build Available Tools section with inline guides.

    Static tools come first (stable for KV cache), conditional tools last.
    """
    tool_block = get_tool_descriptions(
        active_tools, include_delegate, inline_guides=_TOOL_INLINE_GUIDES
    )
    return f"## Available Tools\n{tool_block}"


def _build_environment_section() -> str:
    """Build environment context section with CWD, date, platform."""
    lines = ["## Environment"]
    lines.append(f"- Working directory: {Path.cwd()}")
    lines.append(f"- Date: {datetime.now().strftime('%Y-%m-%d')}")
    lines.append(f"- Platform: {platform.system().lower()} ({platform.release()})")
    return "\n".join(lines)


def _load_directives() -> str:
    """Load DIRECTIVE.md files from project and user paths.

    Returns formatted section string, or empty string if no files found.
    Budget: per-file MAX_DIRECTIVE_FILE_CHARS, total MAX_DIRECTIVE_TOTAL_CHARS.
    """
    loaded: list[str] = []
    total_chars = 0
    seen_hashes: set[int] = set()

    for path in _DIRECTIVE_PATHS:
        if not path.is_file():
            continue
        try:
            content = path.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if not content:
            continue

        # Content-hash dedup
        content_hash = hash(content)
        if content_hash in seen_hashes:
            continue
        seen_hashes.add(content_hash)

        # Per-file budget
        if len(content) > MAX_DIRECTIVE_FILE_CHARS:
            content = content[:MAX_DIRECTIVE_FILE_CHARS] + "\n\n[truncated]"

        # Total budget
        if total_chars + len(content) > MAX_DIRECTIVE_TOTAL_CHARS:
            remaining = MAX_DIRECTIVE_TOTAL_CHARS - total_chars
            if remaining > 100:
                content = (
                    content[:remaining] + "\n\n[truncated — directive budget exceeded]"
                )
            else:
                break
        total_chars += len(content)

        scope = "project" if path.parent == Path.cwd() / ".agent-cli" else "user"
        loaded.append(f"### {path.name} (scope: {scope})\n{content}")

    if not loaded:
        return ""
    return "## Directives\n\n" + "\n\n".join(loaded)


def build_system_prompt(
    capabilities: ModelCapabilities,
    active_tools: list[str],
    include_delegate: bool = False,
    skill_stack: list[str] | None = None,
    session_id: str = "",
) -> str:
    """Build a system prompt adapted to model capabilities and active tools.

    Section order is optimized for LLM attention patterns:
      Primacy  — identity and behavioral principles (strong attention)
      Middle   — reference material: tools, guides, skills (looked up as needed)
      Recency  — current context and user rules (strong attention)
    """
    sections: list[str] = []

    # ── Primacy: identity + principles ──
    sections.append(ROLE_PROMPT)
    sections.append(TASK_GUIDELINES)
    sections.append(FORMAT_RULES)

    # ── Middle: reference material ──
    sections.append(_build_tools_section(active_tools, include_delegate))

    skill_desc = build_skill_descriptions(exclude_names=skill_stack)
    if skill_desc:
        sections.append(skill_desc)

    # ── Recency: current context + user rules ──
    if session_id:
        sections.append(f"## Session\nCurrent session ID: {session_id}")

    sections.append(_build_environment_section())

    directives = _load_directives()
    if directives:
        sections.append(directives)

    return "\n\n".join(sections)


def build_skill_descriptions(
    skills: dict | None = None,
    exclude_names: list[str] | None = None,
) -> str:
    """Build skill descriptions for system prompt injection.

    Excludes skills with disable_model_invocation=True and
    skills in exclude_names (e.g. current skill_stack for recursion prevention).
    If skills is None, loads from disk.
    """
    if skills is None:
        try:
            from agent_cli.skills import load_skills

            skills = load_skills()
        except Exception:
            return ""

    if not skills:
        return ""

    excluded = set(exclude_names or [])

    lines = [
        "## Available Skills",
        "Use the run_skill tool to invoke these skills:",
        '  Example: {"action": "run_skill", "action_input": {"name": "skill-name", "arguments": "..."}}',
    ]
    for skill in skills.values():
        if skill.disable_model_invocation:
            continue
        if skill.name in excluded:
            continue
        hint = f" {skill.argument_hint}" if skill.argument_hint else ""
        lines.append(f"- `{skill.name}{hint}` — {skill.description}")

    # If all skills are disabled, return empty
    if len(lines) <= 2:
        return ""

    return "\n".join(lines)
