"""Atomic-write + concurrency contract for ``write_sqlite_index``.

Before this fix the function did ``path.unlink()`` then re-opened the
same path with a fresh schema. Two parallel delegate workers landing
on ``code_index`` in the same instant produced
``sqlite3.OperationalError: disk I/O error`` because one worker's
unlink ripped the file out from under the other's open connection.

The tests below pin the new contract:

  1. Successful write never leaves a tmp file behind.
  2. Failure during write removes the tmp file (no leak into
     ``.agent-cli/``) AND leaves the previous active DB intact.
  3. The active ``path`` is never opened in truncate mode and never
     unlinked — its inode is preserved until the final
     ``os.replace`` swaps in the new file.
  4. N concurrent ``_ensure_index`` calls from threads all complete
     without exceptions and converge on a valid final DB.
  5. A reader that opens the DB while a build is in flight always
     sees a *complete* snapshot — never an in-progress half-built
     schema.

The wonder driver project (~300 files) reproduced the original race
about half the time with delegate width 2-4; the threaded stress test
in this file forces the same condition deterministically with a tight
loop, so a future regression won't need a 10s real-project run to
surface.
"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

import pytest

from agent_cli.code_index import build, load_index
from agent_cli.code_index.builder import _new_tmp_path, write_sqlite_index


# ─── Fixtures ──────────────────────────────────────────────


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


@pytest.fixture
def project(tmp_path: Path) -> Path:
    """Small but real source tree the indexer can chew on.

    Two Python modules + one markdown so the build pass has actual
    symbols and refs to emit — testing atomic write on an empty
    index would miss the bulk-INSERT path where the race tended to
    manifest under load.
    """
    _write(
        tmp_path / "alpha.py",
        "def alpha():\n    return helper()\n\ndef helper():\n    return 1\n",
    )
    _write(
        tmp_path / "beta.py",
        "class Beta:\n    def run(self):\n        return alpha()\n",
    )
    _write(tmp_path / "doc.md", "# Top\n\n## Setup\n\nbody\n")
    return tmp_path


@pytest.fixture
def db_path(project: Path) -> Path:
    """Standard ``.agent-cli/code_index.db`` slot for the project."""
    return project / ".agent-cli" / "code_index.db"


def _minimal_top() -> dict:
    """Smallest valid `top` dict ``write_sqlite_index`` will accept.

    Lets tests exercise the atomic-write contract without paying for
    a full ``build()`` parse. Schema version + empty file/symbol/ref
    lists are enough — the function only cares about the shape.
    """
    return {
        "schema_version": 2,
        "root": "/tmp",
        "built_at": "2026-01-01T00:00:00",
        "elapsed_seconds": 0.0,
        "preprocessing": {},
        "preproc_fingerprint": "x",
        "files": [],
        "symbols": [],
        "refs": [],
    }


# ─── 1) Tmp cleanup on success ─────────────────────────────


class TestTmpFileLifecycle:
    """After a successful write the tmp file must not survive — the
    operator's ``.agent-cli/`` directory shouldn't grow per-build
    detritus."""

    def test_no_tmp_files_after_successful_write(self, project, db_path):
        write_sqlite_index(db_path, _minimal_top())
        # The target DB exists …
        assert db_path.is_file()
        # … and no sibling .tmp file does.
        leftover = list(db_path.parent.glob(db_path.name + "*.tmp"))
        assert leftover == [], f"leaked tmp files: {leftover}"

    def test_tmp_path_uses_pid_tid_so_concurrent_writers_dont_collide(self, db_path):
        # Spot-check the naming scheme — two consecutive calls from the
        # same thread MUST land on different paths so a parallel build
        # by two threads can't pick the same tmp name. Whitebox-level
        # but worth pinning: a collision here would silently corrupt
        # one of the writers.
        a = _new_tmp_path(db_path)
        b = _new_tmp_path(db_path)
        assert a != b
        # Same directory so the final os.replace is same-filesystem
        # (POSIX atomicity guarantee).
        assert a.parent == db_path.parent


# ─── 2) Tmp cleanup on failure ─────────────────────────────


class TestFailureRollback:
    """If the build raises mid-write the operator must keep their
    previous index AND not collect orphan tmp files."""

    def test_existing_db_preserved_on_write_failure(
        self, project, db_path, monkeypatch
    ):
        # First, lay down a known-good index by hand.
        write_sqlite_index(db_path, _minimal_top())
        original_inode = db_path.stat().st_ino
        original_bytes = db_path.read_bytes()

        # Now arrange for the *next* write to blow up partway through
        # the INSERT pass by feeding it a bad row (missing a required
        # column in the symbols list). The write must:
        #   1. raise (so the operator sees the failure),
        #   2. leave the original DB byte-identical,
        #   3. clean up its tmp file.
        bad_top = _minimal_top()
        bad_top["symbols"] = [{"name": "broken"}]  # missing keys → KeyError
        with pytest.raises(Exception):
            write_sqlite_index(db_path, bad_top)

        # Active DB untouched.
        assert db_path.stat().st_ino == original_inode
        assert db_path.read_bytes() == original_bytes

        # No tmp file lying around.
        leftover = list(db_path.parent.glob(db_path.name + "*.tmp"))
        assert leftover == [], f"leaked tmp files: {leftover}"


# ─── 3) Inode preservation ─────────────────────────────────


class TestNoUnlink:
    """The previous implementation unlinked ``path`` then re-created
    it; this one only ever touches the tmp side. Pin the invariant
    via inode comparison — same file means same inode."""

    def test_concurrent_writes_swap_inode_atomically(self, project, db_path):
        # First write creates the file (or reuses an existing one).
        write_sqlite_index(db_path, _minimal_top())
        first_inode = db_path.stat().st_ino

        # Subsequent write should land on a NEW inode (because we
        # ``os.replace``'d the file) but the path must always be
        # readable — no window where it's gone.
        write_sqlite_index(db_path, _minimal_top())
        second_inode = db_path.stat().st_ino
        assert second_inode != first_inode, (
            "os.replace should swap the inode; if equal the writer "
            "is mutating in place and the race window is still open"
        )

        # And the file remains a valid SQLite DB.
        conn = sqlite3.connect(str(db_path))
        try:
            tables = {
                r[0]
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
        finally:
            conn.close()
        assert {"meta", "files", "symbols", "refs"}.issubset(tables)


# ─── 4) Thread stress ─────────────────────────────────────


class TestConcurrentBuilds:
    """The actual symptom that motivated the fix: parallel delegate
    workers all calling ``code_index`` at once → ``sqlite3.
    OperationalError: disk I/O error`` somewhere in the bunch. The
    fix should let N workers run end to end with no exceptions and
    produce a valid final DB."""

    def _run_full_build(self, project: Path, db_path: Path):
        """Invoke ``build()`` once. Exposed as a method so the thread
        target can be a small lambda and any exception propagates
        through the captured list."""
        build(project, db_path, defs_path=None, verbose=False)

    def test_8_threads_all_succeed(self, project, db_path):
        errors: list[BaseException] = []

        def worker():
            try:
                self._run_full_build(project, db_path)
            except BaseException as e:  # noqa: BLE001 — capture all
                errors.append(e)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        assert errors == [], "concurrent builds raised " + ", ".join(
            f"{type(e).__name__}: {e}" for e in errors
        )

        # And the final DB is valid + populated.
        store = load_index(db_path)
        assert store.n_symbols() > 0
        # alpha + helper + Beta + run + 2 headings = at least 4.
        names = {s["name"] for s in store.all_symbols()}
        assert "alpha" in names
        assert "helper" in names
        assert "Beta" in names


# ─── 5) Reader-during-write consistency ───────────────────


class TestReaderConsistency:
    """An external reader (e.g. another agent-cli process, or this
    one re-opening the DB shortly after a post-hook) must never
    observe a half-built file. The atomic ``os.replace`` is what
    makes this work — the reader sees the old inode until the
    rename, then the new inode."""

    def test_reader_never_sees_half_built_db(self, project, db_path):
        # Seed a valid DB so the reader has something to open from
        # turn 1.
        write_sqlite_index(db_path, _minimal_top())

        stop = threading.Event()
        reader_errors: list[BaseException] = []

        def reader():
            # Tight loop: open the DB, count rows, close. Any time
            # the file is being half-written, sqlite3.connect or the
            # SELECT would surface a malformed-db / no-such-table /
            # disk-I/O error. None of those are acceptable.
            while not stop.is_set():
                try:
                    conn = sqlite3.connect(str(db_path))
                    try:
                        # Must have the schema laid down. If the
                        # writer ever exposes a connection-mid-script
                        # state, this would raise OperationalError.
                        conn.execute("SELECT COUNT(*) FROM symbols").fetchone()
                        conn.execute("SELECT COUNT(*) FROM refs").fetchone()
                        conn.execute("SELECT COUNT(*) FROM meta").fetchone()
                        conn.execute("SELECT COUNT(*) FROM files").fetchone()
                    finally:
                        conn.close()
                except BaseException as e:  # noqa: BLE001
                    reader_errors.append(e)
                    return

        reader_thread = threading.Thread(target=reader)
        reader_thread.start()

        try:
            # Hammer the writer for a bit while the reader spins.
            for _ in range(20):
                build(project, db_path, defs_path=None, verbose=False)
        finally:
            stop.set()
            reader_thread.join(timeout=5)

        assert reader_errors == [], (
            "reader observed an in-progress DB state: "
            + ", ".join(f"{type(e).__name__}: {e}" for e in reader_errors)
        )


# ─── 6) Tool-layer integration ────────────────────────────


class TestToolLayerSerialization:
    """``_ensure_index`` is the entry point parallel workers actually
    hit. The module-level ``_BUILD_LOCK`` should keep the writes
    serialized within one process while ``load_index`` (which is
    read-only) runs unlocked. End-to-end check: many threads call
    ``tool_code_index({'mode': 'lookup', ...})`` and all return
    consistent results without raising."""

    def test_parallel_tool_calls_dont_crash(self, project, monkeypatch):
        monkeypatch.chdir(project)
        from agent_cli.tools.code_index import _dispatch_one

        errors: list[BaseException] = []
        results: list = []

        def worker():
            try:
                r = _dispatch_one({"mode": "lookup", "name": "helper"})
                results.append(r)
            except BaseException as e:  # noqa: BLE001
                errors.append(e)

        threads = [threading.Thread(target=worker) for _ in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        assert errors == [], "tool-layer concurrent calls raised " + ", ".join(
            f"{type(e).__name__}: {e}" for e in errors
        )
        # Every worker should find ``helper`` — the build either
        # already existed before the worker hit it, or the worker
        # produced it, but the lookup must succeed in both cases.
        assert all(r.success for r in results)
        assert all("helper" in r.output for r in results)


# ─── 7) Filesystem-edge cases ─────────────────────────────


class TestEdgeCases:
    """Sundries the atomic write must still handle gracefully."""

    def test_first_write_creates_path(self, project, db_path):
        # No prior DB exists. write must create it without complaint
        # — the tmp path is in the same directory and replace works
        # whether the destination exists or not.
        assert not db_path.exists()
        write_sqlite_index(db_path, _minimal_top())
        assert db_path.is_file()

    def test_parent_dir_created_if_missing(self, project):
        # If a caller passes a path whose parent ``.agent-cli/`` was
        # cleaned by an external process between resolve and write,
        # the function self-heals via the mkdir guard rather than
        # bubbling a FileNotFoundError up to the worker loop.
        nested = project / "deeply" / "nested" / "code_index.db"
        assert not nested.parent.exists()
        write_sqlite_index(nested, _minimal_top())
        assert nested.is_file()

    def test_can_overwrite_existing_invalid_db_file(self, project, db_path):
        # If the previous file is corrupt or truncated (e.g. killed
        # mid-write under the old implementation, leaving a junk
        # file behind), the atomic write still replaces it cleanly.
        db_path.parent.mkdir(parents=True, exist_ok=True)
        db_path.write_bytes(b"this is not a sqlite database at all")
        write_sqlite_index(db_path, _minimal_top())
        # Now openable as SQLite.
        conn = sqlite3.connect(str(db_path))
        try:
            conn.execute("SELECT COUNT(*) FROM symbols").fetchone()
        finally:
            conn.close()


# ─── 8) Race-window regression ────────────────────────────


class TestNoUnlinkDuringBuild:
    """Whitebox: confirm the function source no longer calls
    ``path.unlink()`` on the target. A future change that
    re-introduces the unlink would reopen the original race even if
    the surrounding code looks atomic, so we pin the absence
    explicitly."""

    def test_source_does_not_unlink_target_path(self):
        import inspect
        import re

        from agent_cli.code_index import builder

        src = inspect.getsource(builder.write_sqlite_index)
        # Strip docstring + comments so the historical-pattern
        # explanation in the docs doesn't trip the assertion. We're
        # checking the executable source only.
        lines = [line for line in src.splitlines() if not line.strip().startswith("#")]
        # Drop the triple-quoted docstring (first contiguous run of
        # ``"""...`"""``` lines after the signature). Crude but
        # adequate — the docstring shouldn't contain executable code.
        in_docstring = False
        executable: list[str] = []
        for line in lines:
            stripped = line.strip()
            if not in_docstring and stripped.startswith('"""'):
                in_docstring = True
                if stripped.count('"""') >= 2:  # single-line docstring
                    in_docstring = False
                continue
            if in_docstring:
                if stripped.endswith('"""'):
                    in_docstring = False
                continue
            executable.append(line)
        executable_src = "\n".join(executable)

        # The bug pattern: ``path.unlink()`` (or with the exists check)
        # on the build target. ``tmp.unlink()`` for failure cleanup is
        # legitimate and explicitly allowed.
        assert not re.search(r"\bpath\.unlink\b", executable_src), (
            "write_sqlite_index must not unlink the target path — "
            "use os.replace from a tmp file instead. The previous "
            "unlink + reconnect pattern caused 'disk I/O error' "
            "races under parallel delegate workers."
        )
        # And the atomic rename has to be there.
        assert "os.replace" in executable_src

    def test_target_inode_persists_during_concurrent_writes(self, project, db_path):
        # End-to-end version of the same invariant: observe the
        # inode while a writer hammers the file. Even if a reader
        # holds the *old* inode open across the swap, that handle
        # remains valid until close — the underlying race the bug
        # report described would only happen if a writer's
        # ``unlink`` ripped the file out while another writer's
        # connection was mid-write. We assert here that doesn't
        # happen by checking the file always exists.
        write_sqlite_index(db_path, _minimal_top())

        seen_missing = []
        stop = threading.Event()

        def observer():
            while not stop.is_set():
                if not db_path.exists():
                    seen_missing.append(True)
                    return

        obs = threading.Thread(target=observer)
        obs.start()
        try:
            for _ in range(30):
                write_sqlite_index(db_path, _minimal_top())
        finally:
            stop.set()
            obs.join(timeout=5)

        assert seen_missing == [], (
            "observer saw db_path missing during concurrent writes — "
            "atomic replace contract violated"
        )
