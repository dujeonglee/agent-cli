# Originally ported from minish.ai/Agent-tools tsindex.py (Apache 2.0).
# See NOTICE for the list of modifications.
"""Persistent tree-sitter SQLite index for source/markdown files.

Public API (re-exported here):

    build(root, out_path, ...)        # incremental SQLite index builder
    load_index(path) -> IndexStore    # open an existing index for queries
    build_callgraph(idx)              # (calls_of, callers_of, sites_of)
    cmd_slice(idx, name, ...)         # LLM-context markdown blob renderer

Lower-level modules:

    schema      — Symbol/Ref dataclasses, SCHEMA_VERSION, NAME_KINDS
    languages   — LangSpec dataclass, LANGUAGES registry, helpers
    preproc     — C/C++ unifdef driver + rewrite chain (PR-1.c)
    store       — IndexStore (SQLite reader + query methods) (PR-1.c)
    builder     — build() implementation (Pass-1 / Pass-2 / incremental) (PR-1.c)
    callgraph   — build_callgraph() + helpers (PR-1.c)
    slice       — cmd_slice() (PR-1.c)

The `code_index` *tool* (agent_cli/tools/code_index.py) wraps this package
with a mode-dispatch interface for the agent loop. Direct callers should
prefer the lower-level helpers below.

This package is the supersession of agent_cli.tools.symbols (read_symbols),
which is removed in PR-3. See docs/code-index/DESIGN.md for the full
contract.
"""

from agent_cli.code_index.builder import build
from agent_cli.code_index.callgraph import build_callgraph
from agent_cli.code_index.schema import (
    CODE_NAME_KINDS,
    NAME_KINDS,
    REF_KINDS,
    SCHEMA_VERSION,
    Ref,
    Symbol,
)
from agent_cli.code_index.slice import cmd_slice
from agent_cli.code_index.store import IndexStore, load_index

__all__ = [
    "CODE_NAME_KINDS",
    "IndexStore",
    "NAME_KINDS",
    "REF_KINDS",
    "Ref",
    "SCHEMA_VERSION",
    "Symbol",
    "build",
    "build_callgraph",
    "cmd_slice",
    "load_index",
]
