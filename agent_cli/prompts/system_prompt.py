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
- Don't add features, refactor, or introduce abstractions beyond what the task requires. A bug fix doesn't need surrounding cleanup; a one-shot operation doesn't need a helper. Don't design for hypothetical future requirements. Three similar lines is better than a premature abstraction.
- Don't add error handling, fallbacks, or validation for scenarios that can't happen. Trust internal code and framework guarantees. Only validate at system boundaries (user input, external APIs).
- Do not create new files unless the task requires it.
- Remove imports/variables/functions that YOUR change made unused. Don't delete pre-existing dead code without asking.
- If an approach fails, diagnose the cause before switching tactics.
- Do not introduce new security vulnerabilities.
- Do not invoke agent-cli recursively via shell — that re-enters this same loop.
- Report outcomes honestly — if verification failed or was not run, say so explicitly."""

# ── Section 4: Format Rules ──────────────────────
# Lives on the wire-format plugin: ``ReActFormat.format_rules()``.
# build_system_prompt() pulls it through ``wire_format.format_rules()``.

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
  - Read the target lines in the CURRENT turn before edit_file. Hashes
    from earlier turns drift if anything else touched the file — do not
    reuse them. (read_symbols fetch counts as a fresh read; its output
    is already hashline-formatted and pipes straight into edit_file.)
  - A hash mismatch is not a failure — it is a guardrail signaling the
    file moved between your read and your edit. Re-read the region (or
    re-fetch the symbol) and retry with the fresh tags.
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


def _build_delegate_inline(wire_format) -> str:
    """Build the delegate inline guide.

    Each ``Examples:`` line shows a complete delegate call. The plugin
    decides whether each example is shown as a bare action_input dict
    (ReAct identity) or wrapped in an envelope (future plugins).
    Distinct ``idval`` values per example so envelope plugins that
    require unique ids stay valid.
    """
    examples = [
        ("Single", '{"tasks": [{"task": "Read /tmp/data.csv and count rows"}]}'),
        (
            "With context",
            '{"tasks": [{"task": "Fix the bug we found", "context": "fork"}]}',
        ),
        (
            "With agent",
            '{"tasks": [{"task": "Review this code for vulnerabilities", "agent": "security-reviewer"}]}',
        ),
        (
            "Agent + context",
            '{"tasks": [{"task": "Fix the bug", "agent": "fixer", "context": "fork"}]}',
        ),
        (
            "Parallel (independent)",
            '{"tasks": [{"task": "Analyze A", "context": "fork"}, {"task": "Analyze B", "context": "fork"}]}',
        ),
        (
            "Read-only",
            '{"tasks": [{"task": "Review changes", "context": "fork", "tools": ["read_file", "shell"]}]}',
        ),
    ]
    rendered = "\n".join(
        f"  - {label}: {wire_format.wrap_action_input_example('delegate', args, f'd{i}')}"
        for i, (label, args) in enumerate(examples, start=1)
    )
    return f"""\

  Always use the "tasks" array format. Single item = sync, multiple = parallel.
  Context modes per task:
  - "none" (default): subagent starts with no context. Task must be self-contained.
  - "fork": subagent receives a copy of the current conversation history.
  - "tools": optionally restrict which tools the subagent can use.
  - "agent": optionally specify a predefined agent from .agent-cli/agents/{{name}}.md.
    The agent file defines the subagent's role/principles and can set allowed-tools/model.
  Constraints:
  - Multiple tasks run in PARALLEL. If task B depends on task A's result,
    call delegate twice: first A, then use A's result to call B.
  Examples:
{rendered}\""""


def _build_read_file_inline(active_tools: list[str], wire_format) -> str:
    """Build the read_file inline guide.

    When ``read_symbols`` is active, the Flow paragraph routes
    supported-language files to ``read_symbols`` mode='list' as the
    entry point — its symbol outline beats stat's 20-line head. The
    extension list is pulled from
    :func:`agent_cli.tools.symbols.get_supported_extensions` so a new
    grammar in ``_EXT_TO_LANG`` updates the prompt automatically
    (single source of truth).

    When ``read_symbols`` is not active (e.g., subagent with restricted
    tools), the steering is omitted to avoid pointing the model at a
    tool it cannot call.

    Each mode's call example is rendered through
    ``wire_format.wrap_action_input_example`` so envelope plugins (future) can
    show the full wire-shape; ReAct's identity wrap keeps the bare
    action_input dict the plugin's prior already wraps.
    """
    ex_stat = wire_format.wrap_action_input_example(
        "read_file", '{"path": "app.py", "stat": true}', "r1"
    )
    ex_search = wire_format.wrap_action_input_example(
        "read_file", '{"path": "app.py", "search": "login", "context": 5}', "r2"
    )
    ex_partial = wire_format.wrap_action_input_example(
        "read_file",
        '{"path": "app.py", "line_start": 100, "line_end": 600}',
        "r3",
    )
    ex_full = wire_format.wrap_action_input_example(
        "read_file", '{"path": "app.py"}', "r4"
    )
    base_modes = f"""\

  Pick the right mode for the question — full reads burn context budget,
  but reading too little costs turns:

  1. stat — metadata query, NOT a read (like Unix `stat`). Returns line
     count + size + the first 20 lines so you can pick a real read mode.
       {ex_stat}
  2. search — grep-style targeted lookup. Returns only matching regions
     with surrounding context. Prefer this when the user names a
     specific function, class, or symbol — even if the file looks small.
       {ex_search}
  3. Partial — you know the exact region. Aim for ~500 lines at a time
     so you capture surrounding context. Reading 30-50 lines just to
     peek at one function usually costs more turns when you have to
     come back for context.
       {ex_partial}
  4. Full — the file is known-small or central to the task.
       {ex_full}
"""
    if "read_symbols" in active_tools:
        from agent_cli.tools.symbols import get_supported_extensions

        exts = ", ".join(get_supported_extensions())
        flow = f"""
  Flow: for an unknown file, if its extension is supported by
  read_symbols ({exts}), call read_symbols mode='list' first.
  Otherwise stat first to get its size, then pick one of modes 2–4.
  stat alone is never enough — if you stop after stat, you have only
  seen the first 20 lines. A bare full read on a large file (~300+
  lines) will be refused with instructions; follow them."""
    else:
        flow = """
  Flow: for an unknown file, stat first to get its size, then pick one
  of modes 2–4. stat alone is never enough — if you stop after stat,
  you have only seen the first 20 lines. A bare full read on a large
  file (~300+ lines) will be refused with instructions; follow them."""
    return base_modes + flow


def _build_read_symbols_inline(wire_format) -> str:
    """Build the read_symbols inline guide.

    Pulls the supported extension list from
    :func:`agent_cli.tools.symbols.get_supported_extensions` so adding a
    grammar to ``_EXT_TO_LANG`` automatically updates the prompt.

    Each call example is rendered through ``wire_format.wrap_action_input_example``
    so envelope plugins (future) can show the full wire-shape; ReAct's
    identity wrap keeps the bare action_input dict.
    """
    from agent_cli.tools.symbols import get_supported_extensions

    exts = ", ".join(get_supported_extensions())
    list_py = wire_format.wrap_action_input_example(
        "read_symbols", '{"path": "auth.py", "mode": "list"}', "rs1"
    )
    list_cpp = wire_format.wrap_action_input_example(
        "read_symbols", '{"path": "src/foo.cpp", "mode": "list"}', "rs2"
    )
    list_md = wire_format.wrap_action_input_example(
        "read_symbols", '{"path": "README.md", "mode": "list"}', "rs3"
    )
    list_search1 = wire_format.wrap_action_input_example(
        "read_symbols",
        '{"path": "auth.py", "mode": "list", "search": "login"}',
        "rs4",
    )
    list_search2 = wire_format.wrap_action_input_example(
        "read_symbols",
        '{"path": "src/foo.cpp", "mode": "list", "search": "^ns::Foo::"}',
        "rs5",
    )
    list_search3 = wire_format.wrap_action_input_example(
        "read_symbols",
        '{"path": "tests/test_loop.py", "mode": "list", "search": "^test_"}',
        "rs6",
    )
    fetch_py = wire_format.wrap_action_input_example(
        "read_symbols",
        '{"path": "auth.py", "mode": "fetch", "name": "User.login"}',
        "rs7",
    )
    fetch_cpp = wire_format.wrap_action_input_example(
        "read_symbols",
        '{"path": "src/foo.cpp", "mode": "fetch", "name": "ns::Foo::bar"}',
        "rs8",
    )
    fetch_md = wire_format.wrap_action_input_example(
        "read_symbols",
        '{"path": "README.md", "mode": "fetch", "name": "## Setup"}',
        "rs9",
    )
    return f"""\

  Structure-aware file reader. Two modes:

  1. mode='list' — outline of the file (functions, classes, methods,
     structs/enums/typedefs, #defines, markdown headings). Each line is
     ``name (kind) :start-end``. Use this in place of read_file:stat
     when the file is a supported language.
       {list_py}
       {list_cpp}
       {list_md}
     With optional ``search='<regex>'`` the outline is filtered to
     symbols whose name matches the regex (re.search semantics) — prefer
     this over piping list output through shell grep. Patterns scale
     from substrings to anchored prefixes to grouped families:
       {list_search1}
       {list_search2}
       {list_search3}
  2. mode='fetch' — body of one named symbol from the outline. The
     ``name`` must match the outline verbatim. The body is returned in
     hashline format (LINE#HASH:content), so you can pipe it straight
     into edit_file without a separate read_file. When the same name
     has both a declaration and a definition (e.g. .h prototype + .cpp
     body), the definition is returned.
       {fetch_py}
       {fetch_cpp}
       {fetch_md}

  Naming follows each language's convention:
  - Python / JavaScript / TypeScript: ``Class.method``
  - C / C++: ``namespace::Class::method``
  - Markdown: heading marker + text (``## Setup``)

  Supported extensions: {exts}. C and C++ both use the C++ parser.
  For other formats, use read_file.

  Scope: indexes definitions and structural symbols only. For *call
  sites* (where a name is invoked) or any text occurrence (comments,
  strings, identifiers in use) use read_file search — read_symbols will
  not surface those."""


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


def _build_tool_inline_guides(active_tools: list[str], wire_format) -> dict[str, str]:
    """Build the tool→inline-guide map for the given active tools.

    ``read_file``'s guide depends on whether ``read_symbols`` is also
    active (steering line gets added in that case), and call examples
    in every guide are rendered through ``wire_format.wrap_action_input_example``,
    so the map cannot be a static module-level dict — it's rebuilt per
    call.

    ``edit_file`` and ``ask`` guides have no top-level call examples —
    edit_file's dict literals are inner ``edits[i]`` items (not full
    calls) and ask carries no examples — so those guides remain
    plugin-agnostic constants.
    """
    return {
        "read_file": _build_read_file_inline(active_tools, wire_format),
        "edit_file": _HASHLINE_INLINE,
        "delegate": _build_delegate_inline(wire_format),
        "ask": _ASK_INLINE,
        "read_symbols": _build_read_symbols_inline(wire_format),
    }


def _build_tools_section(active_tools: list[str], wire_format) -> str:
    """Build Available Tools section with inline guides.

    Static tools come first (stable for KV cache), conditional tools last.
    """
    tool_block = get_tool_descriptions(
        active_tools,
        inline_guides=_build_tool_inline_guides(active_tools, wire_format),
    )
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
    wire_format=None,
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

    ``wire_format`` (a ``WireFormat`` plugin) supplies the response-format
    section. Omitting it falls back to the registered ``"react"`` plugin
    so existing callers keep their pre-plugin behavior — that backward-
    compat default also lets unit tests construct a prompt without
    threading the registry through.
    """
    if wire_format is None:
        # Lazy import keeps a cycle from forming if a wire_format plugin
        # ever needs to import system_prompt for any reason.
        from agent_cli import wire_formats

        wire_format = wire_formats.get("react")

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
    sections.append(wire_format.format_rules())

    # ── Middle: reference material ──
    sections.append(_build_tools_section(active_tools, wire_format))

    # MCP tools (if manager provided)
    if mcp_manager:
        from agent_cli.mcp.adapter import build_mcp_tool_descriptions

        mcp_desc = build_mcp_tool_descriptions(mcp_manager)
        if mcp_desc:
            sections.append(f"## MCP Tools\n{mcp_desc}")

    skill_desc = build_skill_descriptions(wire_format=wire_format)
    if skill_desc:
        sections.append(skill_desc)

    if "delegate" in active_tools:
        agent_desc = build_agent_descriptions(wire_format=wire_format)
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


def build_agent_descriptions(wire_format=None) -> str:
    """Build agent descriptions for system prompt injection.

    Uses the delegate module's agent loader to discover available agents.
    Uses ``wrap_full_call_example`` so the example shows
    ``{"action": "delegate", "action_input": {...}}`` — matching the
    sibling ``build_skill_descriptions`` form. The legacy literal in
    this function omitted the ``action`` key (showed only the inner
    ``{"tasks": ...}`` dict), which was inconsistent with the skill
    section and slightly less informative for the model. The plugin
    extraction takes the opportunity to unify the two doc sections on
    the more explicit ``full_call`` shape.

    ``wire_format=None`` falls back to the registered ``"react"`` plugin
    so test callers don't have to thread the registry through.
    """
    if wire_format is None:
        from agent_cli import wire_formats

        wire_format = wire_formats.get("react")

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

    example = wire_format.wrap_full_call_example(
        "delegate",
        '{"tasks": [{"task": "...", "agent": "agent-name", "context": "fork"}]}',
        "ag1",
    )
    lines = [
        "## Available Agents",
        "Consider delegating parallelizable or independent subtasks to agents.",
        f"  {example}",
    ]
    for name, desc in agents:
        suffix = f" — {desc}" if desc else ""
        lines.append(f"- `{name}`{suffix}")

    return "\n".join(lines)


def build_skill_descriptions(skills: dict | None = None, wire_format=None) -> str:
    """Build skill descriptions for system prompt injection.

    Excludes skills with disable_model_invocation=True.
    If skills is None, loads from disk.

    ``wire_format=None`` falls back to the registered ``"react"`` plugin
    (same backward-compat default as ``build_agent_descriptions``).
    """
    if wire_format is None:
        from agent_cli import wire_formats

        wire_format = wire_formats.get("react")

    if skills is None:
        try:
            from agent_cli.skills import load_skills

            skills = load_skills()
        except Exception:
            return ""

    if not skills:
        return ""

    # ``wrap_full_call_example`` (not ``wrap_action_input_example``) —
    # skill docs need the action name visible. See the rationale in
    # ``build_agent_descriptions``'s docstring.
    example = wire_format.wrap_full_call_example(
        "run_skill",
        '{"name": "skill-name", "arguments": "..."}',
        "sk1",
    )
    lines = [
        "## Available Skills",
        "Consider using skills for multi-step or specialized workflows.",
        "Use the run_skill tool to invoke:",
        f"  {example}",
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
