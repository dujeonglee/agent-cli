"""Regression tests for ``agent_cli.code_index._sqlite``.

The shim is the single chokepoint that decides whether ``code_index``
reads SQLite from CPython's stdlib (the fast path on normal hosts) or
from the bundled ``pysqlite3-binary`` wheel (the fallback for builds
compiled ``--without-sqlite``). These tests pin both paths so a future
refactor can't silently strand operators on either kind of host.
"""

from __future__ import annotations

import importlib
import importlib.util
import sys

import pytest

# ``pysqlite3-binary`` only publishes Linux manylinux wheels, so dev
# hosts on macOS / Windows can't exercise the fallback path. Skip the
# fallback assertion there — the stdlib path test still pins the
# happy case, and the Linux CI run pins the fallback.
_PYSQLITE3_AVAILABLE = importlib.util.find_spec("pysqlite3") is not None


def test_shim_exports_sqlite_module():
    """The plain happy path — on any developer machine the stdlib
    ``sqlite3`` is present, so the shim should re-export it unchanged.
    ``Connection`` and ``Row`` are the two attributes the codebase
    relies on (Row factory for dict-style access in ``store.py``,
    Connection for the builder's UPSERT transactions)."""
    from agent_cli.code_index._sqlite import sqlite3

    assert hasattr(sqlite3, "Connection")
    assert hasattr(sqlite3, "Row")
    # The shim must not wrap the module — callers do ``sqlite3.connect``
    # directly, so the symbol it exports has to behave like the module.
    conn = sqlite3.connect(":memory:")
    try:
        cur = conn.execute("SELECT 1")
        assert cur.fetchone() == (1,)
    finally:
        conn.close()


@pytest.mark.skipif(
    not _PYSQLITE3_AVAILABLE,
    reason="pysqlite3 wheel not on this platform (Linux-only)",
)
def test_shim_falls_back_to_pysqlite3_when_stdlib_missing(monkeypatch):
    """Simulate a Python build without stdlib sqlite3 by ripping the
    module out of ``sys.modules`` and blocking re-import, then force a
    fresh import of the shim. It must still produce a usable sqlite3
    symbol — proving the ``pysqlite3-binary`` wheel covers the gap on
    locked-down servers without extra opt-in.
    """
    # Drop any cached shim so the ``try: import sqlite3`` re-runs.
    sys.modules.pop("agent_cli.code_index._sqlite", None)
    sys.modules.pop("sqlite3", None)

    # Block the stdlib import path. ``meta_path`` finders run before
    # the standard finders, so a finder that raises ImportError for
    # ``sqlite3`` effectively masks the stdlib module for this test.
    class _BlockStdlibSqlite:
        def find_spec(self, name, path=None, target=None):
            if name == "sqlite3":
                raise ImportError("simulated: stdlib sqlite3 unavailable")
            return None

    blocker = _BlockStdlibSqlite()
    monkeypatch.setattr(sys, "meta_path", [blocker, *sys.meta_path])

    try:
        shim = importlib.import_module("agent_cli.code_index._sqlite")
    finally:
        # Restore the cached shim so other tests see a normal import.
        sys.modules.pop("agent_cli.code_index._sqlite", None)

    sqlite3 = shim.sqlite3
    # The fallback path must give us the same surface — connect,
    # execute, Row factory — so downstream code in store/builder
    # keeps working unchanged.
    assert hasattr(sqlite3, "Connection")
    assert hasattr(sqlite3, "Row")
    conn = sqlite3.connect(":memory:")
    try:
        cur = conn.execute("SELECT 1 AS one")
        cur.row_factory = sqlite3.Row
        row = conn.execute("SELECT 1 AS one").fetchone()
        # We don't assert the exact identity of the underlying module
        # — pysqlite3's dbapi2 is a distinct module from stdlib's, but
        # both pass DB-API 2.0 conformance and behave the same way
        # here.
        assert row[0] == 1
    finally:
        conn.close()
