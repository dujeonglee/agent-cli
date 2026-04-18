"""Conditional system prompt builder adapted to model capabilities.

Layout (optimized for LLM attention):
  Primacy  — Role, Task Guidelines, Format Rules
  Middle   — Available Tools (guides inlined), Skills
  Recency  — Session, Environment, Directives
"""

from __future__ import annotations

import platform
import shutil
import subprocess
from datetime import datetime
from pathlib import Path

from agent_cli.providers.compat import ModelCapabilities
from agent_cli.tools.registry import get_tool_descriptions

# ── Git Context budget ─────────────────────────
MAX_GIT_DIFF_CHARS = 4000
_GIT_CMD_TIMEOUT = 3  # seconds

# ── DIRECTIVE.md search paths ────────────────────
_DIRECTIVE_PATHS = [
    Path.cwd() / ".agent-cli",
    Path.home() / ".agent-cli",
]

# ── Section 1: Role ──────────────────────────────
ROLE_PROMPT = """\
You are an AI assistant that solves tasks step-by-step using available tools."""

# ── Section 2: Context Window Discipline ─────────
CONTEXT_DISCIPLINE = """\
## Context Window Discipline

Your context window is your single most important resource for solving this
task accurately. It accumulates everything across every turn — your thoughts,
the tools you call, and every observation returned (file contents, shell
output, search results, tool errors). Once the window fills up, earlier
information gets dropped and reasoning quality degrades with it.

Treat every token you add as a cost:

- Read only what you need. Prefer read_file preview or search over full
  reads; prefer specific grep patterns over dumping whole files; narrow
  shell commands at the source instead of scrolling past noise.
- Keep thoughts focused. State purpose and reason in one short paragraph
  — do not restate what the observation already shows.
- Large irrelevant context (unrelated code, huge JSON dumps, verbose logs)
  actively harms later steps by crowding out the specific information you
  need. Filter at the source."""

# ── Section 3: Task Guidelines ───────────────────
TASK_GUIDELINES = """\
## Task Guidelines
- Read relevant code before changing it. Do not modify files you have not read.
- Keep changes tightly scoped to the request. Do not add unrelated cleanup or refactoring.
- Do not create new files unless required to complete the task.
- If an approach fails, diagnose the cause before switching tactics.
- Do not introduce new security vulnerabilities in your changes.
- Report outcomes honestly — if verification failed or was not run, say so explicitly.
- Before starting complex work, check Available Skills and Agents below — they often handle the task better than manual tool sequences."""

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
1. Always include "thought" in your JSON. Your thought MUST include:
   - What you are trying to achieve (purpose)
   - Why you chose this specific action (reason)
2. "action_input" must match the tool's input schema.
3. If an observation shows an error, fix parameters and retry.
4. Respond in the same language as the user.
5. Do not include "observation" — it is injected by the system.
6. Output only valid JSON, nothing else.
7. Do not invoke yourself recursively — do not run agent-cli or any command that starts this tool again via shell.
8. Before calling complete, always call ready_for_review first to verify your work."""

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
  Constraints:
  - Always read the file first to get current hashline tags.
  - If a hash mismatch error occurs, re-read the file and retry with fresh tags.
  - Use write_file only for creating new files, not for editing existing ones."""

_DELEGATE_INLINE = """\

  Always use the "tasks" array format. Single item = sync, multiple = parallel.
  Context modes per task:
  - "none" (default): subagent starts with no context. Task must be self-contained.
  - "fork": subagent receives a copy of the current conversation history.
  - "tools": optionally restrict which tools the subagent can use.
  - "agent": optionally specify a predefined agent from .agent-cli/agents/{name}.md.
    The agent file defines the subagent's role/principles and can set allowed-tools/model.
  Constraints:
  - Multiple tasks run in PARALLEL. If task B depends on task A's result,
    call delegate twice: first A, then use A's result to call B.
  Examples:
  - Single: {"tasks": [{"task": "Read /tmp/data.csv and count rows"}]}
  - With context: {"tasks": [{"task": "Fix the bug we found", "context": "fork"}]}
  - With agent: {"tasks": [{"task": "Review this code for vulnerabilities", "agent": "security-reviewer"}]}
  - Agent + context: {"tasks": [{"task": "Fix the bug", "agent": "fixer", "context": "fork"}]}
  - Parallel (independent): {"tasks": [{"task": "Analyze A", "context": "fork"}, {"task": "Analyze B", "context": "fork"}]}
  - Read-only: {"tasks": [{"task": "Review changes", "context": "fork", "tools": ["read_file", "shell"]}]}\""""

_READ_FILE_INLINE = """\

  IMPORTANT: Full reads waste context window. Pick the smallest mode that answers the question:

  1. Unknown file size? Start with preview.
       {"path": "app.py", "preview": true}
       → returns line count + size + first 20 lines
  2. Looking for something specific? Use search.
       {"path": "app.py", "search": "login", "context": 5}
       → returns only matching regions with surrounding lines
  3. Know the exact range? Partial read.
       {"path": "app.py", "line_start": 100, "line_end": 200}
  4. Full read: only when the file is small (< ~100 lines) or you genuinely need everything.
       {"path": "app.py"}

  Default: if you don't know the file's size, use preview FIRST. Do not full-read unknown files."""

# Map tool names to their inline guides
_TOOL_INLINE_GUIDES: dict[str, str] = {
    "read_file": _READ_FILE_INLINE,
    "edit_file": _HASHLINE_INLINE,
    "delegate": _DELEGATE_INLINE,
}


def _build_tools_section(active_tools: list[str]) -> str:
    """Build Available Tools section with inline guides.

    Static tools come first (stable for KV cache), conditional tools last.
    """
    tool_block = get_tool_descriptions(active_tools, inline_guides=_TOOL_INLINE_GUIDES)
    return f"## Available Tools\n{tool_block}"


def _build_environment_section() -> str:
    """Build environment context section with CWD, date, platform."""
    lines = ["## Environment"]
    lines.append(f"- Working directory: {Path.cwd()}")
    lines.append(f"- Date: {datetime.now().strftime('%Y-%m-%d')}")
    lines.append(f"- Platform: {platform.system().lower()} ({platform.release()})")
    return "\n".join(lines)


def _run_git_cmd(args: list[str]) -> str | None:
    """Run a git command and return stdout, or None on failure."""
    try:
        result = subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=_GIT_CMD_TIMEOUT,
        )
        if result.returncode != 0:
            return None
        return result.stdout
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return None


def _build_git_context_section() -> str:
    """Build Git context section with current branch and diff.

    Returns formatted section string, or empty string if:
    - git is not installed
    - CWD is not a git repository
    - git commands fail or timeout
    """
    if shutil.which("git") is None:
        return ""

    status_output = _run_git_cmd(["git", "status", "--short", "--branch"])
    if status_output is None:
        return ""

    lines = ["## Git Context"]
    lines.append(f"$ git status --short --branch\n{status_output.rstrip()}")

    diff_output = _run_git_cmd(["git", "diff", "HEAD"])
    if diff_output:
        if len(diff_output) > MAX_GIT_DIFF_CHARS:
            total = len(diff_output)
            diff_output = (
                diff_output[:MAX_GIT_DIFF_CHARS]
                + f"\n[diff truncated — {total}chars total]"
            )
        lines.append(f"$ git diff HEAD\n{diff_output.rstrip()}")

    return "\n\n".join(lines)


def _load_directives() -> str:
    """Load DIRECTIVE.md files from project and user paths.

    Uses ResourceLoader._parse_file for consistent parsing.
    Both project and user directives are included (not deduplicated by name)
    unless they have identical content.
    """
    from agent_cli.resource_loader import ResourceLoader

    loaded: list[str] = []
    seen_hashes: set[int] = set()

    for search_dir in _DIRECTIVE_PATHS:
        directive_file = search_dir / "DIRECTIVE.md"
        if not directive_file.is_file():
            continue

        resource = ResourceLoader._parse_file(directive_file)
        if resource is None:
            continue

        content_hash = hash(resource.body)
        if content_hash in seen_hashes:
            continue
        seen_hashes.add(content_hash)

        scope = (
            "project"
            if str(Path.cwd() / ".agent-cli") in resource.source_path
            else "user"
        )
        loaded.append(f"### DIRECTIVE.md (scope: {scope})\n{resource.body}")

    if not loaded:
        return ""
    return "## Directives\n\n" + "\n\n".join(loaded)


def build_system_prompt(
    capabilities: ModelCapabilities,
    active_tools: list[str],
    skill_stack: list[str] | None = None,
    agent_stack: list[str] | None = None,
    session_id: str = "",
    agent_role: str = "",
    parent_role: str = "",
    session_dir: str = "",
    mcp_manager=None,
) -> str:
    """Build a system prompt adapted to model capabilities and active tools.

    Section order is optimized for LLM attention patterns:
      Primacy  — identity and behavioral principles (strong attention)
      Middle   — reference material: tools, guides, skills (looked up as needed)
      Recency  — current context and user rules (strong attention)

    Role selection:
      - main: default ROLE_PROMPT
      - delegate: agent_role replaces ROLE_PROMPT
      - skill: parent_role (inherited from caller)
    """
    sections: list[str] = []

    # ── Primacy: identity + principles ──
    # Role: delegate's agent_role or skill's parent_role replaces default
    if agent_role:
        sections.append(f"## Role\n{agent_role}")
    elif parent_role:
        sections.append(f"## Role\n{parent_role}")
    else:
        sections.append(ROLE_PROMPT)
    sections.append(CONTEXT_DISCIPLINE)
    sections.append(TASK_GUIDELINES)
    sections.append(FORMAT_RULES)

    # ── Middle: reference material ──
    sections.append(_build_tools_section(active_tools))

    # MCP tools (if manager provided)
    if mcp_manager:
        from agent_cli.mcp.adapter import build_mcp_tool_descriptions

        mcp_desc = build_mcp_tool_descriptions(mcp_manager)
        if mcp_desc:
            sections.append(f"## MCP Tools\n{mcp_desc}")

    skill_desc = build_skill_descriptions()
    if skill_desc:
        sections.append(skill_desc)

    if "delegate" in active_tools:
        agent_desc = build_agent_descriptions()
        if agent_desc:
            sections.append(agent_desc)

    # ── Recency: current context + user rules ──

    # Execution context: tell LLM where it is in the call stack
    exec_ctx = _build_execution_context(skill_stack, agent_stack)
    if exec_ctx:
        sections.append(exec_ctx)

    directives = _load_directives()
    if directives:
        sections.append(directives)

    sections.append(_build_environment_section())

    # Context Recovery Guide (replaces session_id + git context)
    if session_dir:
        sections.append(_build_context_recovery(session_dir))

    return "\n\n".join(sections)


def _build_execution_context(
    skill_stack: list[str] | None, agent_stack: list[str] | None
) -> str:
    """Build execution context showing current call stack position."""
    if not skill_stack and not agent_stack:
        return ""

    lines = ["## Execution Context"]

    stack_parts = ["main"]
    if agent_stack:
        stack_parts.extend(f"agent:{a}" for a in agent_stack)
    if skill_stack:
        stack_parts.extend(f"skill:{s}" for s in skill_stack)
    lines.append(f"Call stack: {' → '.join(stack_parts)}")

    blocked = []
    if agent_stack:
        blocked.extend(agent_stack)
    if skill_stack:
        blocked.extend(skill_stack)
    lines.append(
        f"Do not delegate to or invoke: {', '.join(blocked)} (already in call stack)."
    )

    return "\n".join(lines)


def _build_context_recovery(session_dir: str) -> str:
    """Build Context Recovery Guide for system prompt."""
    return (
        "## Context Recovery\n"
        "Older messages may have been dropped from this conversation.\n"
        "Only use this if the user references something you cannot find in the current messages:\n"
        f'  read_file("{session_dir}/history.jsonl")'
    )


def build_agent_descriptions() -> str:
    """Build agent descriptions for system prompt injection.

    Uses the delegate module's agent loader to discover available agents.
    """
    try:
        from agent_cli.tools.delegate import _agent_loader
    except ImportError:
        return ""

    resources = _agent_loader.load_all()
    agents = [
        (name, res.meta.get("description", "")) for name, res in resources.items()
    ]

    if not agents:
        return ""

    lines = [
        "## Available Agents",
        "Consider delegating parallelizable or independent subtasks to agents.",
        '  {"tasks": [{"task": "...", "agent": "agent-name", "context": "fork"}]}',
    ]
    for name, desc in agents:
        suffix = f" — {desc}" if desc else ""
        lines.append(f"- `{name}`{suffix}")

    return "\n".join(lines)


def build_skill_descriptions(skills: dict | None = None) -> str:
    """Build skill descriptions for system prompt injection.

    Excludes skills with disable_model_invocation=True.
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

    lines = [
        "## Available Skills",
        "Consider using skills for multi-step or specialized workflows.",
        "Use the run_skill tool to invoke:",
        '  {"action": "run_skill", "action_input": {"name": "skill-name", "arguments": "..."}}',
    ]
    for skill in skills.values():
        if skill.disable_model_invocation:
            continue
        hint = f" {skill.argument_hint}" if skill.argument_hint else ""
        lines.append(f"- `{skill.name}{hint}` — {skill.description}")

    # If all skills are disabled, return empty
    if len(lines) <= 2:
        return ""

    return "\n".join(lines)
