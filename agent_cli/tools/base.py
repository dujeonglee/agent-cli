"""Tool abstraction surface.

Each tool is a :class:`Tool` subclass that owns its schema, its dispatch,
and its wire-key namespace in one place:

- **schema** (``name`` / ``description`` / ``parameters``) — what used to
  live in the central ``registry.TOOL_SCHEMAS`` dict,
- **dispatch** (``_run``) — what used to be a free ``tool_*`` function
  referenced from the central ``__init__.TOOLS`` dict,
- **prefix** — the wire surface namespaces ``action_input`` keys as
  ``{name}_{param}`` (e.g. ``read_file_path``). Everything prefix-related
  is derived from ``name`` on this base class: :meth:`strip_prefix`
  (wire → standard keys, applied in :meth:`run`) and :meth:`claims`
  (does this payload's key shape belong to me, for recovering a dropped
  action name). Subclasses never override them — they just set ``name``.

``Tool`` instances are the values of ``registry.TOOL_SCHEMAS`` (and
``TOOLS``): they expose the same ``.name`` / ``.description`` /
``.parameters`` attributes the old ``ToolSchema`` dataclass did, so every
schema consumer (system prompt, input validation, MCP adapter) keeps
working unchanged.

Virtual tools (complete/ask/...) keep standard keys, so for them
:meth:`strip_prefix` is a no-op and :meth:`claims` is always False — they
fall through to the normal NO_ACTION recovery rather than being inferred.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path

from agent_cli.tools.result import ToolResult


def default_oversized_nudge(tool_name: str, tokens: int, cap: int) -> str:
    """The generic over-cap observation: the whole tool output is dropped and
    the model is steered to re-request a narrower slice. Shared default behind
    :meth:`Tool.render_oversized` (and the loop's no-tool fallback), so tools
    that don't override the seam keep the historical behaviour byte-for-byte."""
    return (
        f"[{tool_name or 'tool'}: output too large — ~{tokens:,} tokens "
        f"> cap {cap:,} (context_window/10). NOT added to context; the call "
        f"itself succeeded. Large outputs crowd out reasoning and lower quality. "
        f"Re-request a narrower slice: read a specific line range or symbols, add "
        f"a LIMIT / tighter filter, or pipe through `head`/`grep`. To keep a full "
        f"large result, write it to a file (e.g. `… | tee /tmp/out.txt`) then read "
        f"specific parts with read_file.]"
    )


def on_disk_oversized_nudge(
    tool_name: str,
    subject: str,
    location: str,
    read_path: str,
    tokens: int,
    cap: int,
    tools_available: frozenset[str],
    *,
    part_extra: str = "",
    tail_bullets: tuple[str, ...] = (),
) -> str:
    """Shared over-cap nudge for tools whose large output is ON DISK.

    The invariant behind read_file / shell / delegate: once the bulky content
    is a file at ``read_path``, recovery is the same shape — (a) read a SPECIFIC
    part (range / search), or (b) fan out with delegate over SECTIONS of that
    file (each subagent returns only the distilled result, so the parent never
    holds the raw bulk). The fan-out bullet is emitted only when ``delegate`` is
    callable in the current loop (``tools_available``), so it never points at a
    tool the model cannot invoke. ``subject`` names the thing (``'big.py'`` /
    ``command output`` / ``subagent answer``); ``location`` is the on-disk
    clause; ``part_extra`` appends a tool-specific narrowing to bullet (a);
    ``tail_bullets`` adds extra options (e.g. delegate's re-delegate-narrower)."""
    lines = [
        f"[{tool_name}: {subject} is ~{tokens:,} tokens — too large for one "
        f"context (cap {cap:,}). NOT added to context; {location}.",
        f"· Need a SPECIFIC part? read_file '{read_path}' with a line range"
        + (f", or {part_extra}" if part_extra else "")
        + ".",
    ]
    if "delegate" in tools_available:
        lines.append(
            "· Need the WHOLE thing analysed/searched? Fan out with delegate: "
            f"subagents each read_file a section of '{read_path}' and return "
            "only what you need (matches / a summary / the answer) — you get "
            f"the distilled results, not the raw {tokens:,} tokens."
        )
    lines.extend(f"· {b}" for b in tail_bullets)
    return "\n".join(lines) + "]"


class Tool(ABC):
    """Base class for every dispatchable tool.

    Subclasses set ``name`` / ``description`` / ``parameters`` as class
    attributes and implement :meth:`_run`. ``parameters`` is a JSON Schema
    object identical in shape to what the old ``ToolSchema.parameters``
    held.
    """

    name: str
    description: str
    parameters: dict

    #: Whether a turn's consecutive ops of THIS tool may run concurrently.
    #: Default False — ops dispatch sequentially, which is the correctness
    #: guarantee for side-effecting / order-dependent tools (write_file,
    #: edit_file, shell: e.g. write-then-edit the same file, or mkdir-then-
    #: touch, must run in order). Only side-effect-free / independent tools
    #: set this True. The loop reads it to batch a run of same-tool ops into
    #: one concurrent dispatch (see ``AgentLoop._dispatch_parallel_batch``).
    #: Today only ``delegate`` opts in (independent subagents = the case where
    #: concurrency is both safe and worth the wall-clock win).
    parallel_safe: bool = False

    #: Whether the oversized-observation cap applies to THIS tool's
    #: observation. Default True → the cap (context_window/10) is enforced
    #: consistently for every tool: an observation over the cap is replaced
    #: with a narrow-it nudge instead of crowding out the context. A tool
    #: whose large output is genuinely essential can opt out by setting
    #: this False. The loop reads it at the result→observation seam.
    apply_oversized_cap: bool = True

    @property
    def key_prefix(self) -> str:
        """Wire-key namespace for this tool: ``{name}_``."""
        return self.name + "_"

    def strip_prefix(self, args: dict) -> dict:
        """Strip ``key_prefix`` from top-level ``args`` keys (wire →
        standard). Keys without the prefix pass through unchanged — a
        model that emits a bare standard key still works — and nested
        keys inside arrays/objects are never touched.
        """
        if not isinstance(args, dict):
            return args
        p = self.key_prefix
        return {(k[len(p) :] if k.startswith(p) else k): v for k, v in args.items()}

    def add_prefix(self, args: dict) -> dict:
        """Inverse of :meth:`strip_prefix`: namespace top-level ``args``
        keys with ``key_prefix``. Idempotent — keys already carrying the
        prefix are left as-is, and nested keys are untouched. Used to
        render inline-guide examples that are authored in standard keys.
        """
        if not isinstance(args, dict):
            return args
        p = self.key_prefix
        return {(k if k.startswith(p) else p + k): v for k, v in args.items()}

    def claims(self, action_input: dict) -> bool:
        """Whether *action_input* belongs to this tool by key shape — the
        per-tool vote behind ``registry.infer_action`` (dropped-action recovery
        seam, parse_stage 3). True iff any top-level key carries this tool's
        prefix; ``infer_action`` selects a tool only when exactly one claims, so
        the prefix namespace keeps claims mutually exclusive by construction.

        As of consolidation Step 3 every builtin tool is flat-native (no
        prefix), so this is False for all builtin payloads — the seam is latent,
        kept live for a FUTURE wire-key-prefixed tool/format (see
        ``infer_action``). MCP tools are prefix-less by design and never claim.
        """
        if not isinstance(action_input, dict):
            return False
        return any(k.startswith(self.key_prefix) for k in action_input)

    def render_action_input_for_context(self, action_input: dict) -> dict:
        """This tool's ``action_input`` as it should appear when the assistant
        turn is RE-FED to the LLM each subsequent turn (the symmetric
        counterpart to :meth:`render_observation` — action side vs result side).

        Default: identity (the action_input unchanged — same object). A tool
        whose action carries a bulky body (write_file's ``content``,
        edit_file's ``lines``) overrides to replace that value with a short
        marker while KEEPING the op shape (so format self-reinforcement
        survives) — the file is on disk, so re-feeding the body verbatim every
        turn only crowds out context. Must NOT mutate ``action_input`` (the
        caller passes the stored record; history.jsonl stays faithful) — return
        a copy when changing anything.
        """
        return action_input

    def render_observation(self, result: ToolResult, args: dict) -> str:
        """Render this tool's result into the observation body that enters
        the context + the LLM (the text after ``Observation: ``).

        Default reproduces the historical behaviour: the output on success,
        the error on failure. This is the single per-tool seam for "what this
        tool contributes to context" — override to customise (e.g. a tool that
        echoes a large artifact can trim it here, keeping its confirmation but
        pointing back to the file/refs instead of dumping the whole thing).
        ``args`` are the standard (prefix-stripped) action_input keys.
        """
        return result.output if result.success else result.error

    def render_oversized(
        self,
        result: ToolResult,
        args: dict,
        *,
        body: str,
        tokens: int,
        cap: int,
        tools_available: frozenset[str],
        session_dir: "str | Path | None" = None,
    ) -> str:
        """Observation substituted when THIS tool's output exceeds the oversized
        cap (``context_window / 10``). The tool OWNS the full over-cap policy.

        Default: :func:`default_oversized_nudge` — the whole body is dropped and
        the model is steered to re-request a narrower slice. An override may
        return tool-specific recovery guidance, or a BOUNDED slice of ``body``
        plus a pointer (``body`` is passed so a tool can show a head without
        re-running). ``tools_available`` is the set of tool names callable in
        the CURRENT loop (e.g. ``delegate`` is absent inside a depth-limited
        subagent), so guidance can name only tools the model can actually use.
        ``session_dir`` is the current session directory (or None in headless /
        no-ctx runs) — a tool whose output is not already on disk (shell) can
        persist ``body`` there and point at it, uniform with tools whose output
        already is a file (read_file, delegate's ``result.md``). Fires only when
        :attr:`apply_oversized_cap` is True AND the body is over cap — so an
        override never needs to re-check either condition.
        """
        return default_oversized_nudge(self.name, tokens, cap)

    def run(self, args: dict, *, session_dir: Path | None = None) -> ToolResult:
        """Public dispatch: strip the tool-name prefix from ``action_input``
        keys, then hand standard keys to :meth:`_run`. ``session_dir`` is
        forwarded uniformly; tools that do not need it ignore it."""
        return self._run(self.strip_prefix(args), session_dir=session_dir)

    def wrap_single_op(self, flat: dict) -> dict:
        """Convert a multi-op format's flat single-target op into this tool's
        canonical (wire-key-prefixed) input.

        Multi-op formats emit ONE target per op with plain standard keys
        (``{"path": "x"}``) — the turn's op array is the batch mechanism, so
        their ops never carry the per-tool batch wrapper. Batch-shaped tools
        override this to re-wrap (``{"read_file_reads": [{"path": "x"}]}``)
        so the existing validate → strip → run pipeline applies unchanged.

        Default: prefix the keys (no structural change) — right for tools
        whose canonical input is already flat (shell, write_file, ask, ...).
        Overrides must be tolerant of an already-canonical input (idempotent)
        so a model that emits the batch shape anyway still works. Only called
        on the multi-op dispatch path; single-action formats bypass it.
        """
        if not isinstance(flat, dict):
            return flat
        return self.add_prefix(flat)

    def touched_paths(self, action_input: dict) -> list[str]:
        """File-list entries this action contributes during compaction.

        Default: none. Path-handling tools override to pull paths out of
        their OWN action_input shape (prefixed keys, arrays) — keeping that
        schema knowledge in the tool itself, not duplicated in the
        compaction extractor (:func:`context._file_extract`). Overrides
        should use :meth:`strip_prefix` so they read standard keys.
        """
        return []

    def summary_arg(self, action_input: dict) -> str:
        """Short label for this action in the compaction transcript /
        observation header (e.g. ``write_file(src/x.c)``).

        Default: the first non-empty string value (after ``strip_prefix``),
        capped at 60 chars. Tools with a salient field (path / command /
        agent) override to pick it deterministically. Sibling of
        :meth:`touched_paths` — both read the tool's OWN action_input shape.
        """
        for v in self.strip_prefix(action_input).values():
            if isinstance(v, str) and v:
                return v[:60]
        return ""

    @abstractmethod
    def _run(self, args: dict, *, session_dir: Path | None = None) -> ToolResult:
        """Execute the tool with standard (un-prefixed) keys."""
