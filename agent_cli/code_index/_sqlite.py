"""SQLite import shim for code_index storage.

Some Python distributions (locked-down servers, minimal Alpine builds,
custom ``--without-sqlite`` rebuilds) ship CPython without the
``sqlite3`` extension module. ``code_index`` would otherwise refuse to
load on those hosts even though every other agent-cli feature works.

This shim prefers stdlib ``sqlite3`` whenever available — zero overhead
on normal systems — and falls back to the ``pysqlite3-binary`` wheel
(which bundles its own SQLite C library inside the package) only when
the stdlib import raises. Both expose the DB-API 2.0 surface plus the
small set of SQLite extensions ``store.py`` / ``builder.py`` use
(``Row`` factory, ``executemany``, ``UPSERT`` syntax), so callers see
no behavioural difference between the two paths.

Use:

    from agent_cli.code_index._sqlite import sqlite3

NOT a plain ``import sqlite3`` — the latter would crash on the very
hosts this shim exists to support.
"""

from __future__ import annotations

try:
    import sqlite3
except ImportError:  # pragma: no cover — exercised by test_sqlite_fallback
    # ``pysqlite3-binary`` is a hard base dep (see pyproject.toml), so
    # this import is expected to succeed on every install. If the wheel
    # itself is missing we let the ImportError propagate — agent-cli
    # isn't usable without SQLite either way and a clear traceback
    # beats a deferred, mysterious failure later in the build pipeline.
    from pysqlite3 import dbapi2 as sqlite3

__all__ = ["sqlite3"]
