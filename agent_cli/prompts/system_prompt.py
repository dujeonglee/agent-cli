"""Conditional system prompt builder adapted to model capabilities.

Layout (optimized for LLM attention):
  Primacy  — Role, Context Discipline, Task Guidelines, Format Rules
  Middle   — Available Tools (guides inlined), MCP Tools, Skills, Agents
  Recency  — Environment, Context Recovery, Directives, Execution Context

Recency ordering rationale (passive → active, persistent → immediate):
  Environment        — passive reference (where you are)
  Context Recovery   — passive fallback (how to recover dropped context)
  Directives         — user-authored persistent rules (override defaults)
  Execution Context  — current call-stack constraint (most immediate)
Execution Context is also the only section that mutates within a session
(skill/agent boundaries) — putting it last keeps the preceding three as
a stable prefix for KV cache reuse across turns.
"""

from __future__ import annotations

import platform
from pathlib import Path

from agent_cli.providers.compat import ModelCapabilities
from agent_cli.tools.registry import get_tool_descriptions

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

Your context window is your single most important resource. Every thought,
tool call, and observation accumulates across turns. When it fills, older
information drops — and reasoning quality drops with it.

Treat every token you add as a cost:

- Read only what you need. Prefer search or targeted reads over full reads;
  narrow shell commands at the source rather than dumping output.
- Keep `thought` short. Do not restate what the observation already shows.
- Large irrelevant context (off-topic content, huge dumps, verbose logs)
  crowds out what you actually need. Filter at the source."""

# ── Section 3: Task Guidelines ───────────────────
TASK_GUIDELINES = """\
## Task Guidelines
- Read a file before changing it — code, config, docs, anything. Do not edit what you have not read.
- Keep changes tightly scoped to the request. Do not bundle unrelated cleanup or refactoring.
- Do not create new files unless the task requires it.
- If an approach fails, diagnose the cause before switching tactics.
- Do not introduce new security vulnerabilities.
- Do not invoke agent-cli recursively via shell — that re-enters this same loop.
- Report outcomes honestly — if verification failed or was not run, say so explicitly."""

# ── Section 4: Format Rules ──────────────────────
FORMAT_RULES = """\
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
  - Use write_file only for creating new files, not for editing existing ones.

  Multi-edit notes:
  - Each edit in `edits` references the ORIGINAL file state — the array
    is NOT a sequential "apply then re-read" pipeline.
  - Edits that overlap (same region or ref string) are rejected —
    combine them into a single `replace` op with the final intended
    content.
  - If a later edit depends on the RESULT of an earlier edit (e.g.,
    modifying a line that an earlier edit just created), use separate
    edit_file calls with read_file between them. Observation sync is
    how you "see" the intermediate state."""

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

  Pick the right mode for the question — full reads burn context budget,
  but reading too little costs turns:

  1. stat — metadata query, NOT a read (like Unix `stat`). Returns line
     count + size + the first 20 lines so you can pick a real read mode.
       {"path": "app.py", "stat": true}
  2. search — grep-style targeted lookup. Returns only matching regions
     with surrounding context. Prefer this when the user names a
     specific function, class, or symbol — even if the file looks small.
       {"path": "app.py", "search": "login", "context": 5}
  3. Partial — you know the exact region. Aim for ~500 lines at a time
     so you capture surrounding context. Reading 30-50 lines just to
     peek at one function usually costs more turns when you have to
     come back for context.
       {"path": "app.py", "line_start": 100, "line_end": 600}
  4. Full — the file is known-small or central to the task.
       {"path": "app.py"}

  Flow: for an unknown file, stat first to get its size, then pick one
  of modes 2–4. stat alone is never enough — if you stop after stat,
  you have only seen the first 20 lines. A bare full read on a large
  file (~300+ lines) will be refused with instructions; follow them."""

_READ_SYMBOLS_INLINE = """\

  Structure-aware file reader. Two modes:

  1. mode='list' — outline of the file (functions, classes, methods,
     structs/enums/typedefs, #defines, markdown headings). Each line is
     ``name (kind) :start-end``. Use this in place of read_file:stat
     when the file is a supported language.
       {"path": "auth.py", "mode": "list"}
       {"path": "src/foo.cpp", "mode": "list"}
       {"path": "README.md", "mode": "list"}
  2. mode='fetch' — body of one named symbol from the outline. The
     ``name`` must match the outline verbatim. When the same name has
     both a declaration and a definition (e.g. .h prototype + .cpp
     body), the definition is returned.
       {"path": "auth.py", "mode": "fetch", "name": "User.login"}
       {"path": "src/foo.cpp", "mode": "fetch", "name": "ns::Foo::bar"}
       {"path": "README.md", "mode": "fetch", "name": "## Setup"}

  Naming follows each language's convention:
  - Python / JavaScript / TypeScript: ``Class.method``
  - C / C++: ``namespace::Class::method``
  - Markdown: heading marker + text (``## Setup``)

  Supported extensions: .py, .js/.jsx/.mjs/.cjs, .ts/.tsx, .c/.cc/.cpp/
  .cxx/.h/.hh/.hpp/.hxx, .md/.markdown. C and C++ both use the C++
  parser. For other formats, use read_file."""

_ASK_INLINE = """\

  `ask` vs `complete` — pick by intent, not tone:
  - `ask`: you GENUINELY cannot proceed without information from the
    user. A real question with real alternatives where you don't know
    the right answer. "Which of these two paths should I take?",
    "What's the production database name?", "Should I overwrite this
    file or keep both?".
  - `complete`: every other ending. Task done, user said goodbye, user
    said thanks, user gave a casual reply, you finished your answer
    and have nothing else to do. The conversation does NOT need a
    question to continue — the user can simply reply at the next
    prompt if they want more.

  Common mistakes that keep the loop alive when it should end:
  - "Was that helpful?" / "Anything else?" / "Let me know if you have
    questions" — these are pleasantries, not questions. Use `complete`.
  - "Goodbye!" / "See you next time!" / "👋" — closing remarks. Use
    `complete`.
  - Restating the user's last message back as a question
    ("So you want X?") when their meaning was already clear. Use
    `complete` and answer.

  Rule of thumb: if your "question" could be a statement and the
  conversation would still flow, it's not a real question — use
  `complete`."""

# Map tool names to their inline guides
_TOOL_INLINE_GUIDES: dict[str, str] = {
    "read_file": _READ_FILE_INLINE,
    "edit_file": _HASHLINE_INLINE,
    "delegate": _DELEGATE_INLINE,
    "ask": _ASK_INLINE,
    "read_symbols": _READ_SYMBOLS_INLINE,
}


def _build_tools_section(active_tools: list[str]) -> str:
    """Build Available Tools section with inline guides.

    Static tools come first (stable for KV cache), conditional tools last.
    """
    tool_block = get_tool_descriptions(active_tools, inline_guides=_TOOL_INLINE_GUIDES)
    return f"## Available Tools\n{tool_block}"


def _build_environment_section() -> str:
    """Build environment context section with CWD and platform.

    Date is intentionally omitted: it has no programmatic consumer and
    its daily rollover invalidates provider-side prefix caches across
    midnight. Tasks that genuinely need today's date can call shell `date`.
    """
    lines = ["## Environment"]
    lines.append(f"- Working directory: {Path.cwd()}")
    lines.append(f"- Platform: {platform.system().lower()} ({platform.release()})")
    return "\n".join(lines)


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

    # ── Recency: passive reference → active rules → immediate constraint ──
    sections.append(_build_environment_section())

    # Context Recovery Guide (replaces session_id + git context)
    if session_dir:
        sections.append(_build_context_recovery(session_dir))

    directives = _load_directives()
    if directives:
        sections.append(directives)

    # Execution context: tell LLM where it is in the call stack.
    # Last because it's the only Recency section that mutates within a
    # session — keeping it last leaves the preceding three as a stable
    # KV-cache-friendly prefix.
    exec_ctx = _build_execution_context(skill_stack, agent_stack)
    if exec_ctx:
        sections.append(exec_ctx)

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
