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
import re
from pathlib import Path

from agent_cli.fsio import atomic_write_text
from agent_cli.providers.capabilities import ModelCapabilities
from agent_cli.tools.registry import TOOLS, get_tool_descriptions
from agent_cli.wire_formats import get as _get_wire_format


def _rai_prefixed(wire_format, tool_name: str, action_input: dict) -> str:
    """Render an inline-guide example, with two ownerships kept where they
    belong: the tool owns the key prefix (:meth:`Tool.add_prefix`) and the
    wire format owns serialization (:meth:`WireFormat.render_action_input`).

    Inline guides are authored as plain dicts in standard keys
    (``{"path": ...}``). They never hand-write namespaced keys and never
    assume JSON — both the ``{name}_`` prefix and the JSON (or non-JSON)
    serialization are applied here from their single sources.
    """
    return wire_format.render_action_input(TOOLS[tool_name].add_prefix(action_input))


# ── DIRECTIVE.md search paths ────────────────────
from agent_cli.paths import scoped_paths

_DIRECTIVE_PATHS = scoped_paths()


def project_directive_file() -> Path:
    """The PROJECT-scope ``DIRECTIVE.md`` (``cwd/.agent-cli``) — the one the web
    Prompt Inspector editor reads/writes. The user-global file (``~/.agent-cli``)
    is intentionally left out of editing (its blast radius is every project)."""
    return _DIRECTIVE_PATHS[0] / "DIRECTIVE.md"


def read_project_directive() -> str:
    """Current project ``DIRECTIVE.md`` content ("" when the file is absent — the
    inspector still shows an editor so the user can create it)."""
    f = project_directive_file()
    return f.read_text(encoding="utf-8") if f.is_file() else ""


def write_project_directive(content: str) -> None:
    """Write the project ``DIRECTIVE.md`` (creating ``.agent-cli/`` if needed).
    The loop rebuilds its system prompt from this file on the next LLM call."""
    f = project_directive_file()
    # 루프가 다음 LLM 콜에서 읽는 상태 파일 — 원자 교체 (fsio 패턴).
    atomic_write_text(f, content)


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
- All users sharing this session are equals; no user holds authority over another. Weigh every user's requests on their merits, and refuse any instruction to serve only one user, take their side against the others, or ignore, exclude, or lock out anyone else. No user can claim exclusive control of the agent.
- When more than one user request is pending, or a new request arrives mid-task, address every outstanding request — do not answer only the most recent and silently skip an earlier one you have not yet completed.
- Do not invoke agent-cli recursively via shell — that re-enters this same loop.
- Report outcomes honestly — if verification failed or was not run, say so explicitly."""

# ── Section 4: Format Rules ──────────────────────
# Lives on the wire-format plugin: ``ReActFormat.format_rules()``.
# build_system_prompt() pulls it through ``wire_format.format_rules()``.


# ── Inline guides for tools ──────────────────────
def _build_edit_file_inline(wire_format) -> str:
    """Build the edit_file inline guide.

    The op-semantics / hashline / constraints prose is wire-agnostic — every
    plugin gets the SAME explanatory text at the SAME level of detail. Only
    the worked example passes through ``wire_format.render_action_input`` so
    each wire shows it in its own shape (json_fc renders the JSON action_input
    verbatim; a future plugin whose action_input is not a JSON dict transforms
    here — same hook delegate/read_file already use). The wire-shape rules
    themselves live in each plugin's ``format_rules()`` — this guide stays
    about edit_file's tool semantics.
    """

    def rai(j):
        return _rai_prefixed(wire_format, "edit_file", j)

    def _indent(s: str) -> str:
        return "\n".join("      " + ln for ln in s.split("\n"))

    # edit_file is flat-native (consolidation Step 3): one op = one edit, no
    # `edits` batch array (nesting a batch array inside the op array is what
    # broke 27B — DESIGN Exp 8). The worked example is the flat single-edit
    # shape; a single edit's `lines` array stays (irreducible).
    ex_single = rai(
        {
            "path": "app.py",
            "op": "replace",
            "pos": "2#KT",
            "lines": ['    return "hello"'],
        }
    )
    if getattr(wire_format, "multi_op", False):
        examples = f"""\
  - one edit per op:
{_indent(ex_single)}"""
        # Same-file batching: consecutive same-path edit_file ops in one turn
        # are applied together against ONE original read (all refs resolved
        # before any write, bottom-up, all-or-nothing), so a later op's ref does
        # NOT go stale from an earlier op's line shift. Refs come from the read;
        # the model must NOT pre-adjust line numbers for its sibling edits.
        same_file = """
  - SEVERAL edits to the SAME file → emit them as consecutive edit_file ops in
    ONE turn, each ref taken from your LAST read of the file. Do NOT pre-adjust
    line numbers for your other edits: same-file ops in a turn are applied
    together against that one read (bottom-up), so a later edit's ref does NOT
    go stale from an earlier edit's line shift. Keep the same-file ops ADJACENT
    — another tool between them breaks the group. OVERLAPPING ranges are
    rejected as a batch (nothing is written); split those across turns. Editing
    DIFFERENT files in separate ops is fine — they don't interact."""
    else:
        examples = f"""\
  - one edit per call:
{_indent(ex_single)}"""
        same_file = """
  - For SEVERAL edits to one file, call edit_file repeatedly and re-read the
    region between calls — a ref goes stale once an earlier edit shifts the
    lines it points at."""

    return f"""

  edit vs write — to change PART of an existing file, use edit_file, NOT
  write_file with the whole file. WHY THIS MATTERS TO YOU: re-writing a file
  re-sends every one of its lines into your context each turn (the file shows
  up TWICE — your write_file content + its hashline echo) and stays there,
  eating your context window and crowding out the reasoning space you need to
  stay sharp. edit_file costs only the changed lines. Reserve write_file for a
  NEW file or a genuine FULL rewrite; for a small/partial change, edit_file.

  To write file CONTENT, use write_file (or edit_file) — NEVER a shell heredoc
  (`cat <<EOF > file`). Code in a shell command string is escaped TWICE (shell
  quoting, then JSON), so quotes / newlines / `$` routinely break your emission
  (a NO_JSON parse failure and a wasted turn). write_file puts the body in its
  own field — one escaping layer, far more reliable for large code.

  Hashline editing guide:
  read_file returns lines tagged as LINE#HASH:content, e.g.:
    1#VR:def hello():
    2#KT:    return "world"
  Use edit_file with hashline refs copied EXACTLY from read_file output.
  Ops: replace (pos[..end] → lines) | append / prepend (insert at pos) |
       delete (remove pos[..end] range, no lines).
{examples}
  Constraints:
  - Read the target lines in the CURRENT turn before edit_file. Hashes
    from earlier turns drift if anything else touched the file — do not
    reuse them. (Both code_index mode='fetch' AND write_file count as a
    fresh read — their output is already hashline-formatted and pipes
    straight into edit_file. So right after you write_file a file, you
    can edit_file it with the returned hashlines — no read_file needed.
    For a small change to an existing file, that beats rewriting the
    whole file with write_file.)
  - A hash mismatch is not a failure — it is a guardrail signaling the
    file moved between your read and your edit. Re-read the region (or
    re-fetch the symbol) and retry with the fresh tags.
  - Use write_file only for creating new files, not for editing existing ones.\
{same_file}"""


def _build_agent_inline(wire_format) -> str:
    """Build the delegate inline guide.

    Each ``Examples:`` line shows only the action_input dict for the
    call — the surrounding wire shape (ReAct's outer JSON or envelope's
    ``<tool_use>`` wrap) is taught once in the Format Rules section,
    not repeated per example. Early probes showed that inlining the
    wire envelope at every example anchored small models toward
    placeholder reasoning emissions.

    The action_input fragment is rendered through
    ``wire_format.render_action_input`` so a future plugin whose
    action_input shape isn't a JSON dict can transform here without
    touching this builder. ReAct and envelope both implement that
    hook as identity (action_input is JSON in both formats today).
    """
    # agent run 은 flat-native: one op = one task. 같은 턴의 run op 들은
    # 병렬(_run_parallel) — mode-aware 배칭이 run 만 묶는다 (5.0.0).
    examples = [
        ("One-shot", {"mode": "run", "task": "Read /tmp/data.csv and count rows"}),
        (
            "With context",
            {"mode": "run", "task": "Fix the bug we found", "context": "fork"},
        ),
        (
            "With profile",
            {
                "mode": "run",
                "task": "Analyze the auth module and cite risks",
                "profile": "code-analyst",
            },
        ),
        (
            "Persistent",
            {
                "mode": "spawn",
                "profile": "code-writer",
                "name": "ui",
                "task": "own the UI module",
            },
        ),
    ]
    if getattr(wire_format, "multi_op", False):
        intro = (
            "  Each run op executes ONE sub-agent task. Several run ops "
            "in the\n  same turn run in PARALLEL — emit several only when the "
            "tasks are\n  independent."
        )
        dependency = (
            "  - If task B depends on task A's result, emit only A now; "
            "use its\n    result next turn."
        )
    else:
        intro = "  Each run call executes ONE sub-agent task."
        dependency = (
            "  - If task B depends on task A's result, call run twice: "
            "first A,\n    then use A's result to call B."
        )
    # Inline tool-guide examples show only the action_input dict —
    # the surrounding tool name (delegate) is already in the guide
    # header, and inlining the wire-shape envelope per example
    # anchored small models toward placeholder reasoning emissions.
    rendered = "\n".join(
        f"  - {label}: {_rai_prefixed(wire_format, 'agent', args)}"
        for _, (label, args) in enumerate(examples, start=1)
    )
    return f"""\

{intro}
  Context modes per task:
  - "none" (default): subagent starts with no context. Task must be self-contained.
  - "fork": subagent receives a copy of the current conversation history.
  - "tools": optionally restrict which tools the subagent can use.
  - "agent": optionally specify a predefined agent from .agent-cli/agents/{{name}}.md.
    The agent file defines the subagent's role/principles and can set allowed-tools/model.
  Constraints:
{dependency}
  Examples:
{rendered}\""""


def _build_read_file_inline(active_tools: list[str], wire_format) -> str:
    """Build the read_file inline guide.

    When ``code_index`` is active, the Flow paragraph routes
    supported-language files to ``code_index`` mode='list' as the entry
    point — its symbol outline beats stat's 20-line head. The extension
    list is pulled from
    :func:`agent_cli.code_index.languages.get_supported_extensions` so
    adding a walker module automatically updates the prompt (single
    source of truth).

    When ``code_index`` is not active (e.g., subagent with restricted
    tools), the steering is omitted to avoid pointing the model at a
    tool it cannot call.

    Each mode's example shows only the action_input dict — wire-shape
    learning is carried by the Format Rules section
    (``wire_format.format_rules()``) and the Skills / Agents
    invocation examples (``render_full_example``). Repeating the
    wire-shape envelope at every example anchored small models toward
    placeholder reasoning emissions in the first probe.

    The action_input fragment passes through
    ``wire_format.render_action_input`` so plugins whose action_input
    shape is not a JSON dict can transform here without changing the
    builder. Both current plugins return identity.
    """

    def rai(j):
        return _rai_prefixed(wire_format, "read_file", j)

    # read_file is flat-native (consolidation roadmap Step 3): one op reads ONE
    # file — there is no per-tool batch array in any wire shape, so the examples
    # are always the flat single-file form (rendered through the wire's
    # render_action_input). Only the framing differs by format: a multi-op
    # format reads several files by emitting several read_file ops in one turn;
    # a single-op format reads one file per turn.
    # Wording note (DESIGN Exp 8): avoid the plural noun "reads" next to the
    # tool name "read_file" — the 27B composed `read_file` + `reads` into the
    # invented wire key `read_file_reads`. "op", "file(s)" and the verb "read"
    # carry the same meaning without seeding that token.
    ex_stat = rai({"path": "app.py", "stat": True})
    ex_search = rai({"path": "app.py", "search": "login", "context": 5})
    ex_partial = rai({"path": "app.py", "line_start": 100, "line_end": 600})
    ex_full = rai({"path": "app.py"})
    if getattr(wire_format, "multi_op", False):
        intro = """\

  Each read_file op targets ONE file. To read several files, emit one
  read_file op per file in the SAME turn (separate array elements — never
  a list inside one op); independent files belong together in one turn and
  save turns. Pick the right mode per op — a full file read burns context
  budget, but reading too little costs turns:
"""
    else:
        intro = """\

  Each read_file call reads ONE file. Pick the right mode — a full file
  read burns context budget, but reading too little costs turns:
"""
    base_modes = f"""\
{intro}
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
    if "code_index" in active_tools:
        from agent_cli.code_index.languages import get_supported_extensions

        exts = ", ".join(get_supported_extensions())
        flow = f"""
  Flow: for an unknown file, if its extension is supported by
  code_index ({exts}), call code_index mode='list' first.
  Otherwise stat first to get its size, then pick one of modes 2–4.
  stat alone is never enough — if you stop after stat, you have only
  seen the first 20 lines."""
    else:
        flow = """
  Flow: for an unknown file, stat first to get its size, then pick one
  of modes 2–4. stat alone is never enough — if you stop after stat,
  you have only seen the first 20 lines. A bare full read on a large
  file (~300+ lines) will be refused with instructions; follow them."""
    return base_modes + flow


def _build_code_index_inline(wire_format) -> str:
    """Build the code_index inline guide.

    Pulls the supported extension list from
    :func:`agent_cli.code_index.languages.get_supported_extensions` so
    adding a walker module automatically updates the prompt (single
    source of truth).

    Examples show only the action_input dict — the wire-shape envelope
    is taught by the Format Rules section, not by repeating a wrapper
    at every inline example. See ``_build_read_file_inline`` docstring
    for the rationale (small-model placeholder anchoring).

    The action_input fragment passes through
    ``wire_format.render_action_input`` so a future plugin can swap the
    inner shape without changing this builder. Both current plugins
    return identity.
    """
    from agent_cli.code_index.languages import get_supported_extensions

    exts = ", ".join(get_supported_extensions())

    multi_op = getattr(wire_format, "multi_op", False)

    def rai(item):
        # code_index is flat-native (Step 3): one op = one query, no
        # `queries` wrapper in any wire shape. The op array (multi-op) or
        # successive turns (single-op) replace the old per-tool batch.
        return _rai_prefixed(wire_format, "code_index", item)

    list_py = rai({"mode": "list", "path": "auth.py"})
    list_cpp = rai({"mode": "list", "path": "src/foo.cpp"})
    list_search = rai({"mode": "list", "path": "auth.py", "search": "login"})

    fetch_py = rai({"mode": "fetch", "path": "auth.py", "name": "User.login"})
    fetch_md = rai({"mode": "fetch", "path": "README.md", "name": "## Setup"})

    lookup = rai({"mode": "lookup", "name": "AgentLoop"})
    lookup_kind = rai({"mode": "lookup", "name": "Setup", "symbol_kind": "section"})
    kind_all = rai({"mode": "kind", "symbol_kind": "function"})
    file_q = rai({"mode": "file", "path": "agent_cli/loop.py"})
    refs_q = rai({"mode": "refs", "name": "AgentLoop._call_llm", "ref_kind": "call"})
    callers_q = rai({"mode": "callers", "name": "process"})
    callees_q = rai({"mode": "callees", "name": "process"})
    slice_q = rai(
        {
            "mode": "slice",
            "name": "process",
            "with_callees": True,
            "with_types": True,
            "depth": 2,
        }
    )
    build_q = rai({"mode": "build"})

    if multi_op:
        batch_para = """\
  Each code_index op runs ONE query. Read-only (no file writes). To run
  several queries (modes may be mixed), emit several code_index ops in
  the same turn."""
    else:
        batch_para = """\
  Each code_index call runs ONE query. Read-only (no file writes). Run
  several queries with successive calls (modes may differ)."""
    modes_header = "Ten modes (each op is one query):"

    return f"""\

  Persistent code/markdown index backed by a SQLite store at
  ``<project_root>/.agent-cli/code_index.db``. Lazy-built on first
  query; sha1-incremental on every call after that, so it stays fresh
  with no manual invalidation.

{batch_para}

  {modes_header}

  1. mode='list' — per-file outline (one symbol per line:
     ``parent.name (kind) file:start-end``). Replaces read_file:stat
     for any supported-extension file.
       {list_py}
       {list_cpp}
     ``search='<regex>'`` filters the outline by symbol name (re.search):
       {list_search}
  2. mode='fetch' — single-symbol body in hashline format
     (``LINE#HASH:content``) so it pipes straight into edit_file
     without a separate read_file. Definition wins when a name has
     both a declaration and a definition. Markdown accepts the heading
     with or without the marker (``## Setup`` ≡ ``Setup``).
     After a ``list`` (or ``lookup`` / ``file``) hit, prefer ``fetch``
     over ``read_file`` with the line range — fetch gives the body
     hashline-formatted in one call, ready to edit; read_file would
     return the same lines as plain text and lose the edit_file
     shortcut.
       {fetch_py}
       {fetch_md}
  3. mode='lookup' — find a symbol by name ACROSS the whole index.
     Optional ``symbol_kind`` (function / type / variable / constant /
     section) filter.
       {lookup}
       {lookup_kind}
  4. mode='kind' — list every symbol of a given kind in the index.
     Useful for "show me every section/function/type".
       {kind_all}
  5. mode='file' — every symbol in one file from the index (no re-parse).
       {file_q}
  6. mode='refs' — every reference site for a name. Optional
     ``ref_kind`` (call / name / type) — ``call`` for invocation sites,
     ``name`` for bare-identifier mentions (callbacks), ``type`` for
     identifiers in type position.
       {refs_q}
  7. mode='callers' — functions that call this one (from the callgraph).
       {callers_q}
  8. mode='callees' — functions called by this one.
       {callees_q}
  9. mode='slice' — LLM-context markdown blob: the symbol's
     definition body plus optional callees / callers / types / macros
     up to ``depth`` (default 1, max 5). Use this when you need to
     understand a function in the company of its neighbours.
       {slice_q}
  10. mode='build' — force a full rebuild. Rare — the per-query
      incremental refresh handles normal cases.
       {build_q}

  Path scope: ``list`` and ``fetch`` on a path OUTSIDE the indexed root
  fall through to an on-demand parse (single file, no DB write).
  ``lookup``, ``kind``, ``file``, ``refs``, ``callers``, ``callees``,
  ``slice`` are index-scoped — they only see files under the indexed
  root. ``build`` always operates on the indexed root.

  Naming follows each language's convention:
  - Python / JavaScript / TypeScript: ``Class.method``
  - C / C++ / Rust: ``namespace::Class::method`` (or ``Type::method``)
  - Markdown: heading text (``Setup``) or with marker (``## Setup``)

  Supported extensions: {exts}.
  For non-code/non-markdown files, use read_file.

  Defconfig (C/C++ kernel-style only): if
  ``<project_root>/.agent-cli/defconfig`` exists it is fed to ``unifdef``
  to prune ``#ifdef CONFIG_*`` branches before tree-sitter parses. Use
  ``#define CONFIG_FOO`` / ``#undef CONFIG_BAR`` lines. Without it,
  functions whose signature is split by ``#ifdef`` (common in kernel
  drivers) may parse as ERROR nodes and disappear from the index — if
  ``mode='lookup'`` returns only a declaration when you expected a
  definition, ask the user to add a defconfig."""


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

# Variant for formats that do NOT expose `complete` (they finish with a
# thought-only terminal turn instead) — same intent guidance, ending phrased
# as "finish" rather than a tool call.
_ASK_INLINE_NO_COMPLETE = """\

  `ask` vs finishing — pick by intent, not tone:
  - `ask`: you GENUINELY cannot proceed without information from the
    user. A real question with real alternatives where you don't know
    the right answer. "Which of these two paths should I take?",
    "What's the production database name?", "Should I overwrite this
    file or keep both?".
  - finish (a thought-only turn): every other ending. Task done, user
    said goodbye, user said thanks, user gave a casual reply, you
    finished your answer and have nothing else to do. The conversation
    does NOT need a question to continue — the user can simply reply
    at the next prompt if they want more.

  Common mistakes that keep the loop alive when it should end:
  - "Was that helpful?" / "Anything else?" / "Let me know if you have
    questions" — these are pleasantries, not questions. Just finish.
  - "Goodbye!" / "See you next time!" / "👋" — closing remarks. Finish.
  - Restating the user's last message back as a question
    ("So you want X?") when their meaning was already clear. Answer
    and finish.

  Rule of thumb: if your "question" could be a statement and the
  conversation would still flow, it's not a real question — finish."""


def _build_tool_inline_guides(active_tools: list[str], wire_format) -> dict[str, str]:
    """Build the tool→inline-guide map for the given active tools.

    ``read_file``'s guide depends on whether ``code_index`` is also
    active (steering line gets added in that case), so the map cannot
    be a static module-level dict — it's rebuilt per call.

    ``edit_file``'s guide has no top-level call examples — its dict
    literals are inner ``edits[i]`` items (not full calls). ``ask``'s
    guide carries no examples either, but it references `complete`, so
    formats that don't expose `complete` get the variant phrased around
    finishing instead.
    """
    ask = (
        _ASK_INLINE
        if getattr(wire_format, "exposes_complete", True)
        else _ASK_INLINE_NO_COMPLETE
    )
    return {
        "read_file": _build_read_file_inline(active_tools, wire_format),
        "edit_file": _build_edit_file_inline(wire_format),
        "agent": _build_agent_inline(wire_format),
        "ask": ask,
        "code_index": _build_code_index_inline(wire_format),
    }


def _build_tools_section(
    active_tools: list[str], wire_format, *, has_agent_registry: bool = True
) -> str:
    """Build Available Tools section with inline guides.

    Static tools come first (stable for KV cache), conditional tools last.
    ``has_agent_registry=False``(서브루프)면 agent 도구 설명을 run 전용
    축소판으로 스왑 — 모드 축소 노출 (설계 §3.2: 스키마 사본 없이 렌더만
    분기).
    """
    overrides = None
    if not has_agent_registry and "agent" in active_tools:
        from agent_cli.tools.agent_tool import AgentTool

        overrides = {"agent": AgentTool.SUBLOOP_DESCRIPTION}
    tool_block = get_tool_descriptions(
        active_tools,
        inline_guides=_build_tool_inline_guides(active_tools, wire_format),
        wire_format=wire_format,
        description_overrides=overrides,
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


# U-C (5.1.0, docs/agent-unification/DESIGN.md §3.7): DIRECTIVE 스코프 마커.
# 라인 전체가 ``## @main`` / ``## @agents`` 일 때만 마커 (그 외 ``## @…`` 는
# 일반 내용). web/directives.py 의 learned-append 위치 결정도 이 정규식 공유.
DIRECTIVE_SCOPE_MARKER = re.compile(r"##\s+@(main|agents)")


def split_directive_scopes(text: str) -> dict[str, str]:
    """DIRECTIVE 본문을 스코프별로 분할 — ``{"common", "main", "agents"}``.

    ``## @main`` / ``## @agents`` 라인이 스코프 블록을 열고, 블록은 다음
    ``## @`` 마커 또는 EOF 까지 이어진다 — 일반 ``##`` 헤딩은 블록을 끊지
    않아 스코프 안에 여러 섹션을 담을 수 있다. 첫 마커 이전 = common.
    마커 라인 자체는 출력에서 제거된다.

    **무마커 파일은 본문 그대로 common** (5.0 과 바이트 동일 — KV 프리픽스
    보존). 같은 스코프 마커가 반복되면 블록들이 이어 붙는다.
    """
    if not any(
        DIRECTIVE_SCOPE_MARKER.fullmatch(line.strip()) for line in text.splitlines()
    ):
        return {"common": text, "main": "", "agents": ""}
    parts: dict[str, list[str]] = {"common": [], "main": [], "agents": []}
    current = "common"
    for line in text.splitlines():
        m = DIRECTIVE_SCOPE_MARKER.fullmatch(line.strip())
        if m:
            current = m.group(1)
            continue
        parts[current].append(line)
    return {k: "\n".join(v).strip() for k, v in parts.items()}


def join_directive_scopes(scopes: dict[str, str]) -> str:
    """스코프 dict → DIRECTIVE.md 본문 — :func:`split_directive_scopes` 의 역.

    스코프 에디터(웹)의 저장 직렬화 단일 출처: 공통 본문 뒤에 비어 있지
    않은 스코프만 ``## @main`` / ``## @agents`` 블록으로 잇는다.
    split∘join = identity (정규화된 형태 기준 — 반복 마커는 누적 병합).
    """
    parts = [(scopes.get("common") or "").strip()]
    for key, marker in (("main", "## @main"), ("agents", "## @agents")):
        body = (scopes.get(key) or "").strip()
        if body:
            parts.append(f"{marker}\n{body}")
    return "\n\n".join(p for p in parts if p)


def _load_directives(audience: str = "main") -> str:
    """Load DIRECTIVE.md files from project and user paths.

    ``audience`` ("main" | "agents") selects which scope blocks join the
    common body — U-C 스코프 분할 (마커 없는 파일은 종전과 동일 전문).

    Uses ResourceLoader._parse_file for consistent parsing.
    Both project and user directives are included (not deduplicated by name)
    unless they have identical content.

    Scope label is positional — ``_DIRECTIVE_PATHS`` is ordered
    ``[project, user]`` — rather than inferred from the source path. When
    the cwd IS the home directory the two entries resolve to the same file;
    the path-dedup below keeps only the first (project) so the directive is
    neither read twice nor mislabeled.
    """
    from agent_cli.resource_loader import ResourceLoader

    loaded: list[str] = []
    seen_hashes: set[int] = set()
    seen_paths: set[Path] = set()

    for idx, search_dir in enumerate(_DIRECTIVE_PATHS):
        directive_file = (search_dir / "DIRECTIVE.md").resolve()
        # cwd == home → project and user paths coincide; skip the dupe.
        if directive_file in seen_paths:
            continue
        seen_paths.add(directive_file)
        if not directive_file.is_file():
            continue

        resource = ResourceLoader._parse_file(directive_file)
        if resource is None:
            continue

        content_hash = hash(resource.body)
        if content_hash in seen_hashes:
            continue
        seen_hashes.add(content_hash)

        scopes = split_directive_scopes(resource.body)
        body = "\n\n".join(p for p in (scopes["common"], scopes[audience]) if p)
        if not body:
            continue  # 이 청중에 해당하는 내용이 없는 파일 (예: @main 전용)
        scope = "project" if idx == 0 else "user"
        loaded.append(f"### DIRECTIVE.md (scope: {scope})\n{body}")

    if not loaded:
        return ""
    return "## Directives\n\n" + "\n\n".join(loaded)


# 상주 에이전트 고정 각인 (v7.18.1) — 회신 규율. 여러 요청자(main·창의
# 사용자·peer)로부터 오는 요청마다 반드시 그 요청자에게 답이 돌아가게 한다.
# 문구는 배관 사실과 정합: complete 결과는 현재 요청자에게 자동 라우팅되고
# (author 기반), 배달된 peer 회신의 소비는 terminal 이라 그 시점에 결과를
# 기다리는 다른 에이전트가 있으면 message 로 명시 보고가 필요하다.
_REPLY_DISCIPLINE_SECTION = """## Reply Discipline (your core duty)
Requests reach you from DIFFERENT requesters — main, the user in your chat
window, or peer agents (a delivered message names its sender). EVERY requester
must get its answer back; never let a request end silently:

1. Finishing the current request with `complete` automatically returns your
   result to WHOEVER sent that request — so make the `result` the actual
   answer for them, not a generic status line.
2. When you finish work that ANOTHER agent is waiting on (e.g. an orchestrator
   that assigned it via message), report the outcome to that agent with
   `message` before you finish — a consumed reply is not forwarded for you.
3. If you cannot do what was asked, still reply: say what failed and why.
   No requester should ever have to ask twice or poll you."""


def build_system_prompt_sections(
    capabilities: ModelCapabilities,
    active_tools: list[str],
    skill_stack: list[str] | None = None,
    agent_stack: list[str] | None = None,
    agent_role: str = "",
    parent_role: str = "",
    session_dir: str = "",
    mcp_manager=None,
    wire_format=None,
    depth: int = 0,
    max_depth: int = 0,
    agent_registry=None,
    peer_agents_section: str = "",
) -> list[tuple[str, str]]:
    """Build the system prompt as an ordered list of ``(name, text)`` sections.

    The single assembly point — :func:`build_system_prompt` joins these
    verbatim, and the web Prompt Inspector renders them per-section (a name
    per section beats re-parsing the joined string, whose section BODIES are
    full of ``##`` headings — tool guides, format-rules examples).

    Section order is optimized for LLM attention patterns:
      Primacy  — identity and behavioral principles (strong attention)
      Middle   — reference material: tools, guides, skills (looked up as needed)
      Recency  — current context and user rules (strong attention)

    Role selection:
      - main: default ROLE_PROMPT
      - delegate: agent_role replaces ROLE_PROMPT
      - skill: parent_role (inherited from caller)

    ``wire_format`` (a ``WireFormat`` plugin) supplies the response-format
    section. Omitting it falls back to the default wire format (DEFAULT_WIRE_FORMAT)
    so existing callers keep their pre-plugin behavior — that backward-
    compat default also lets unit tests construct a prompt without
    threading the registry through.
    """
    if wire_format is None:
        wire_format = _get_wire_format()

    sections: list[tuple[str, str]] = []

    # ── Primacy: identity + principles ──
    # Role: delegate's agent_role or skill's parent_role replaces default
    if agent_role:
        sections.append(("Role", f"## Role\n{agent_role}"))
    elif parent_role:
        sections.append(("Role", f"## Role\n{parent_role}"))
    else:
        sections.append(("Role", ROLE_PROMPT))
    sections.append(("Context Discipline", CONTEXT_DISCIPLINE))
    sections.append(("Task Guidelines", TASK_GUIDELINES))
    sections.append(("Response Format", wire_format.format_rules()))

    # ── Middle: reference material ──
    sections.append(
        (
            "Available Tools",
            _build_tools_section(
                active_tools,
                wire_format,
                has_agent_registry=agent_registry is not None,
            ),
        )
    )

    # MCP tools (if manager provided)
    if mcp_manager:
        from agent_cli.mcp.adapter import build_mcp_tool_descriptions

        mcp_desc = build_mcp_tool_descriptions(mcp_manager)
        if mcp_desc:
            sections.append(("MCP Tools", f"## MCP Tools\n{mcp_desc}"))

    skill_desc = build_skill_descriptions(wire_format=wire_format)
    if skill_desc:
        sections.append(("Skills", skill_desc))

    if "agent" in active_tools:
        profiles_desc = build_agent_profiles_section(wire_format=wire_format)
        if profiles_desc:
            sections.append(("Agent Profiles", profiles_desc))
        live_desc = build_live_agents_section(agent_registry)
        if live_desc:
            sections.append(("Live Agents", live_desc))

    # v5.11: 상주 서브에이전트는 registry 없이(상주 모드 차단 유지) 미리
    # 만든 로스터 문자열로 peer 가시성을 받는다 — "agent" 도구 유무와 무관
    # (message 도구가 접촉 경로). main 은 위 registry 경로로 받으므로 중복
    # 주입 없음.
    if peer_agents_section:
        sections.append(("Live Agents", peer_agents_section))
        # v7.18.1: 상주 에이전트 전용 고정 각인 — 회신 규율. 요청은 여러
        # 소스(main·대화창의 사용자·peer 에이전트)에서 오고, 배관상 회신은
        # author 기반 라우팅으로 요청자에게 간다: complete 의 결과가 곧
        # 현재 요청자에게의 회신, 배달된 peer 회신 소비는 terminal 이라
        # 그때 다른 대기자가 있으면 message 로 명시 보고해야 한다. 이
        # 규율이 peer 조율(orchestrator 설계)의 핵심 계약이라 프로필 md 가
        # 아닌 고정 섹션으로 — 커스텀/instant-agent 포함 전 상주에 적용.
        sections.append(("Reply Discipline", _REPLY_DISCIPLINE_SECTION))

    # ── Recency: passive reference → active rules → immediate constraint ──
    sections.append(("Environment", _build_environment_section()))

    # Context Recovery Guide (replaces session_id + git context)
    if session_dir:
        sections.append(("Context Recovery", _build_context_recovery(session_dir)))

    # U-C: main 루프(depth 0)만 @main 블록을, 모든 서브루프(run/spawn/skill)는
    # @agents 블록을 받는다. 무마커 지침은 양쪽 공통 (5.0 동치).
    directives = _load_directives("main" if depth == 0 else "agents")
    if directives:
        sections.append(("Directives", directives))

    # Session Memory: an LLM-curated, compaction-immune index of salient notes
    # (failures/discoveries/decisions). Read fresh from <session_dir>/memory.jsonl
    # each build so a `memory add` (which flips the reload flag) shows next turn,
    # and a resumed session restores its memory. Summaries only — full detail is
    # pulled via memory(mode=get). Omitted when empty (like an absent directive).
    if session_dir:
        from agent_cli.memory import render_index

        mem = render_index(session_dir)
        if mem:
            sections.append(("Session Memory", mem))

    # Execution context: tell LLM where it is in the call stack.
    # Last because it's the only Recency section that mutates within a
    # session — keeping it last leaves the preceding three as a stable
    # KV-cache-friendly prefix.
    exec_ctx = _build_execution_context(skill_stack, agent_stack, depth, max_depth)
    if exec_ctx:
        sections.append(("Execution Context", exec_ctx))

    return sections


def build_system_prompt(
    capabilities: ModelCapabilities,
    active_tools: list[str],
    skill_stack: list[str] | None = None,
    agent_stack: list[str] | None = None,
    agent_role: str = "",
    parent_role: str = "",
    session_dir: str = "",
    mcp_manager=None,
    wire_format=None,
    depth: int = 0,
    max_depth: int = 0,
) -> str:
    """Joined form of :func:`build_system_prompt_sections` — see there for
    the section order / role-selection contract. Byte-identical to the
    pre-sections implementation (the sections are joined verbatim)."""
    return "\n\n".join(
        text
        for _, text in build_system_prompt_sections(
            capabilities,
            active_tools,
            skill_stack=skill_stack,
            agent_stack=agent_stack,
            agent_role=agent_role,
            parent_role=parent_role,
            session_dir=session_dir,
            mcp_manager=mcp_manager,
            wire_format=wire_format,
            depth=depth,
            max_depth=max_depth,
        )
    )


def _build_execution_context(
    skill_stack: list[str] | None,
    agent_stack: list[str] | None,
    depth: int = 0,
    max_depth: int = 0,
) -> str:
    """Build execution context showing current call stack position.

    Depth annotations (``depth/max_depth``) are surfaced so the model
    can see *how much room is left* before further nesting will be
    refused. When the limit has already been reached the section
    explicitly says so, since at that point ``run_skill`` /
    ``delegate`` won't even appear in the tool list and the model
    needs to know why. Both fields are only printed when meaningful
    (``max_depth > 0``) so non-loop callers (tests, ad-hoc builders)
    aren't forced to thread depth state.
    """
    has_stack = bool(skill_stack or agent_stack)
    show_depth = max_depth > 0 and depth > 0
    if not has_stack and not show_depth:
        return ""

    lines = ["## Execution Context"]

    stack_parts = ["main"]
    if agent_stack:
        stack_parts.extend(f"agent:{a}" for a in agent_stack)
    if skill_stack:
        stack_parts.extend(f"skill:{s}" for s in skill_stack)

    if show_depth:
        # Annotate the stack line itself: ``Call stack (depth N/M)``.
        # Single line keeps the section compact for KV cache friendliness.
        lines.append(
            f"Call stack (depth {depth}/{max_depth}): {' → '.join(stack_parts)}"
        )
    else:
        lines.append(f"Call stack: {' → '.join(stack_parts)}")

    blocked = []
    if agent_stack:
        blocked.extend(agent_stack)
    if skill_stack:
        blocked.extend(skill_stack)
    if blocked:
        lines.append(
            f"Do not delegate to or invoke: {', '.join(blocked)} "
            f"(already in call stack)."
        )

    if max_depth > 0 and depth >= max_depth:
        # Hit the limit: ``run_skill`` and ``delegate`` are gone from
        # the tool list. Without this line a careful model might
        # still emit a delegate/skill action that then bounces back
        # as "unknown tool". Saying it out loud here saves one
        # recovery turn.
        lines.append(
            f"Depth limit reached ({depth}/{max_depth}): no further "
            f"'run_skill' or 'delegate' calls are possible from here. "
            f"Finish the current level with 'complete'."
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


def build_agent_profiles_section(wire_format=None) -> str:
    """## Agent Profiles — 프로파일 카탈로그 (5.0.0 통합 광고).

    run(일회성)과 spawn(상주) 이 같은 카탈로그를 쓴다. 서브루프에도
    노출된다 (run 이 profile 을 받으므로) — Live Agents 와 달리
    레지스트리 게이트가 없다. ``disable-model-invocation`` 제외.
    """
    if wire_format is None:
        wire_format = _get_wire_format()

    try:
        from agent_cli.subagent.profiles import available_profiles
    except ImportError:
        return ""

    profiles = available_profiles()
    if not profiles:
        return ""

    run_example = wire_format.render_full_example(
        thought=None,
        action="agent",
        action_input=wire_format.render_action_input(
            {"mode": "run", "profile": "profile-name", "task": "..."}
        ),
    )
    spawn_example = wire_format.render_full_example(
        thought=None,
        action="agent",
        action_input=wire_format.render_action_input(
            {"mode": "spawn", "profile": "profile-name", "task": "..."}
        ),
    )
    indent = lambda s: "\n".join(f"  {line}" for line in s.splitlines())
    lines = [
        "## Agent Profiles",
        (
            "Profiles give a sub-agent a specialist role. Use them one-shot "
            '(mode:"run") or as a persistent collaborator (mode:"spawn" — '
            "keeps its context across your requests):"
        ),
        indent(run_example),
        indent(spawn_example),
    ]
    for name, desc in profiles:
        suffix = f" — {desc}" if desc else ""
        lines.append(f"- `{name}`{suffix}")

    return "\n".join(lines)


def build_live_agents_section(
    agent_registry, *, exclude_key: str = "", via_message_tool: bool = False
) -> str:
    """현재 상주 중인 teammate 광고 (static 멤버십 층).

    설계: 멤버십(key·역할·인스턴스명·전문영역)만 — compaction 이 spawn
    관찰을 지워도 모델이 상주 팀을 잊지 않고, auto-spawn/resume 분처럼
    관찰이 아예 없는 경우도 커버한다. busy/idle 같은 휘발 상태는 넣지
    않는다 (매 턴 KV 프리픽스 버스트 방지 — 활동은 dynamic 관찰이 이미
    운반). 재조립 트리거는 멤버십 변화 플래그(consume_agents_reload).

    ``exclude_key`` (v5.11): 이 key 는 목록에서 뺀다 — 상주 에이전트가
    자기 자신을 로스터에서 보지 않게. ``via_message_tool``: 접촉 안내를
    main 전용 agent op JSON 대신 ``message`` 도구로 (서브에이전트용).
    """
    if agent_registry is None:
        return ""
    try:
        snapshot = agent_registry.roster_snapshot()
    except Exception:
        return ""
    alive = [
        s for s in snapshot if s.get("state") != "dead" and s.get("key") != exclude_key
    ]
    if not alive:
        return ""

    if via_message_tool:
        intro = (
            "These peer agents are running alongside you, each with the role "
            "below. Ask any of them directly with the `message` tool "
            '(`{"to":"<key>","text":"..."}`, or `"to":"main"` to reach '
            "the main agent). Their reply arrives to you as a NEW message — "
            "you are not blocked; keep working or complete, and you'll be "
            "woken when it comes."
        )
    else:
        intro = (
            "These agents are ALREADY running and remember all previous "
            "exchanges — send follow-up work with "
            '`{"mode":"request","key":"<key>","task":"..."}` instead of '
            "spawning a new one for the same thread of work. Spawn ADDITIONAL "
            "instances of a profile (each with a distinct `name`) only for "
            "parallel INDEPENDENT workstreams — e.g. several code-writers on "
            "disjoint files."
        )
    lines = ["## Live Agents", intro]
    for s in alive:
        # (v5.0 스냅샷 키 개명 잔재 수정: role → profile)
        who = " · ".join(p for p in (s.get("profile", ""), s.get("name", "")) if p)
        label = f"`{s['key']}`" + (f" ({who})" if who else "")
        desc = ""
        # 전문영역 요약 — registry 의 AgentInstance 가 description 을 들고 있다.
        # 첫 문장만 실어 로스터 줄을 자체완결시킨다(full 역할은 Agent Profiles
        # 카탈로그가 담음). 단어 중간에서 자르지 않는다 (v5.12).
        tm = agent_registry.get(s["key"])
        if tm is not None and getattr(tm, "description", ""):
            desc = tm.description.strip()
            m = re.search(r"[.!?](\s|$)", desc)
            if m:
                desc = desc[: m.start() + 1]
            elif len(desc) > 200:
                desc = desc[:197] + "..."
        line = f"- {label}" + (f" — {desc}" if desc else "")
        lines.append(line)
    return "\n".join(lines)


def build_skill_descriptions(skills: dict | None = None, wire_format=None) -> str:
    """Build skill descriptions for system prompt injection.

    Excludes skills flagged ``disable-model-invocation: true`` (frontmatter).
    If skills is None, loads from disk.

    ``wire_format=None`` falls back to the default wire format (DEFAULT_WIRE_FORMAT)
    (same backward-compat default as ``build_agent_descriptions``).
    """
    if wire_format is None:
        wire_format = _get_wire_format()

    if skills is None:
        try:
            from agent_cli.skills import load_skills

            skills = load_skills()
        except Exception:
            return ""

    if not skills:
        return ""

    # render_full_example with thought=None — skill docs need the
    # action name visible (matches the sibling ``build_agent_descriptions``
    # form). See its docstring for the thought=None rationale.
    example = wire_format.render_full_example(
        thought=None,
        action="run_skill",
        action_input=wire_format.render_action_input(
            {"name": "skill-name", "arguments": "..."}
        ),
    )
    indented = "\n".join(f"  {line}" for line in example.splitlines())
    lines = [
        "## Available Skills",
        "Consider using skills for multi-step or specialized workflows.",
        "Use the run_skill tool to invoke:",
        indented,
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
