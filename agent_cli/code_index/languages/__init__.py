# Originally ported from minish.ai/Agent-tools tsindex.py (Apache 2.0).
# See NOTICE for the list of modifications.
"""Per-language registry and extension lookup for the code_index package.

Each supported language has a dedicated module under this package
(c.py / cpp.py / python.py / ...) that defines:

    - a `LANG` LangSpec instance, registered into `LANGUAGES` at import
    - `walk_definitions(root, src, rel, syms)` — Pass-1 collector
    - `walk_refs(root, src, rel, refs, defined_names, idents_out, language=...)`
      — Pass-2 collector

The walker modules are added in PR-1.b. PR-1.a only ships this
scaffolding (LangSpec dataclass, empty registry, helpers) so the rest
of the package can import the contract.

Lazy grammar import
-------------------

`LangSpec.grammar_factory` is a zero-arg callable that returns the
parsed tree-sitter `Language` object. It is invoked at most once per
language per process (see `get_parser` in builder.py). The indirection
exists so that importing the package does NOT trigger every tree-sitter
grammar wheel to load — a Python-only project never pays the cost of
loading the Rust grammar, etc.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional


@dataclass(frozen=True)
class LangSpec:
    """Registration record for one language.

    name             logical key in LANGUAGES, e.g. 'python', 'cpp'.
    exts             file extensions handled by this spec, e.g. ('.py', '.pyi').
    grammar_factory  zero-arg callable returning a tree_sitter.Language
                     for this spec. Invoked lazily; never call at import.
    walk_definitions Pass-1 collector. Signature:
                       (root_node, src_bytes, rel_path, syms_out) -> None
                     Appends Symbol instances to syms_out.
    walk_refs        Pass-2 collector. Signature:
                       (root_node, src_bytes, rel_path, refs_out,
                        defined_names, idents_out, language=...) -> None
                     Appends Ref instances to refs_out. May add to
                     idents_out (a set of identifiers seen in this file)
                     to support the Option-B incremental rebuild path
                     in builder.py.
    preprocess       Optional source preprocessor.
                       (src_bytes, unifdef_flags) -> bytes
                     Returns possibly-rewritten source bytes for
                     tree-sitter to parse. Use `noop_preprocess` for
                     languages that do not need preprocessing.
    """

    name: str
    exts: tuple[str, ...]
    grammar_factory: Callable[[], object]
    walk_definitions: Callable[..., None]
    walk_refs: Callable[..., None]
    preprocess: Callable[[bytes, list[str]], bytes]


# Filled by each agent_cli.code_index.languages.<name> module at import.
# Walker modules are imported on first use (see _ensure_loaded below) so
# importing this package is cheap.
LANGUAGES: dict[str, LangSpec] = {}


def noop_preprocess(src: bytes, _flags: list[str]) -> bytes:
    """Identity preprocessor for languages without macros.

    Exported because every non-C/C++ LangSpec uses this verbatim; keeping
    it here avoids cross-module imports in the small walker files.
    """
    return src


# Walker modules to import. Order is not significant — each registers
# itself into LANGUAGES on import. Listed explicitly (rather than
# pkgutil.iter_modules) so the closed set is auditable.
_WALKER_MODULES: tuple[str, ...] = (
    "c",
    "cpp",
    "python",
    "go",
    "rust",
    "java",
    "javascript",
    "typescript",
    "markdown",
)

_loaded = False


def _ensure_loaded() -> None:
    """Import every walker module so LANGUAGES is fully populated.

    Called by `language_of` and `get_supported_extensions` before any
    lookup. Walker modules register themselves into LANGUAGES at import
    time, so a missing module silently disappears from the supported set
    — caller surfaces "unsupported extension" as the user-visible error.
    """
    global _loaded
    if _loaded:
        return
    import importlib

    for mod_name in _WALKER_MODULES:
        try:
            importlib.import_module(f"agent_cli.code_index.languages.{mod_name}")
        except ImportError:
            # PR-1.a scaffolding: walker modules don't exist yet. Silently
            # skip so the rest of the package can be imported and tested.
            # PR-1.b will land the walkers; from then on every entry in
            # _WALKER_MODULES is expected to import cleanly.
            continue
    _loaded = True


def language_of(path: Path) -> Optional[str]:
    """Return the LANGUAGES key for `path`'s extension, or None.

    Lowercases the extension, so '.PY' and '.py' both resolve. Returns
    None for any extension not claimed by a registered walker — callers
    should treat this as "use read_file instead".
    """
    _ensure_loaded()
    ext = path.suffix.lower()
    for spec in LANGUAGES.values():
        if ext in spec.exts:
            return spec.name
    return None


def get_supported_extensions() -> list[str]:
    """Return the sorted list of file extensions handled by the index.

    Single source of truth for:
      - the `code_index` tool's "unsupported extension" error message,
      - the system prompt's `code_index` inline guide,
      - the `read_file` flow-steering branch that routes supported files
        to `code_index mode='list'` rather than line-based read.

    Adding a new walker module + LangSpec automatically extends the
    returned list — no other surfaces need editing.
    """
    _ensure_loaded()
    exts: set[str] = set()
    for spec in LANGUAGES.values():
        exts.update(spec.exts)
    return sorted(exts)
