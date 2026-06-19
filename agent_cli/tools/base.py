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
