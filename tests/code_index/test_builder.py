"""Tests for ``agent_cli.code_index.builder.build``.

Coverage per DESIGN.md §12.2: full build from scratch, no-op
incremental, 1-file modify, file delete/rename, the three invalidation
paths (schema_version / root / preproc_fingerprint), ``force_full``,
and Option-B re-Pass2 (a name newly added in file A causes file B to
be re-walked even though B itself hasn't changed).

Each test prepares its own tmp tree so it's hermetic — no shared
fixture mutation pitfalls.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent_cli.code_index import build, load_index
from agent_cli.code_index._sqlite import sqlite3

# ----- small helpers ----------------------------------------------------------


def _write(p: Path, content: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content)


def _names_in_file(store, rel: str) -> set[str]:
    return {s["name"] for s in store.find_symbols(file=rel)}


def _files_in(store) -> set[str]:
    return {f["path"] for f in store.files}


def _build(root: Path, db: Path, **kw) -> object:
    """Wrapper that calls ``build`` with verbose=False and defs_path=None."""
    kw.setdefault("verbose", False)
    kw.setdefault("defs_path", None)
    build(root, db, **kw)
    return load_index(db)


# ----- full build ------------------------------------------------------------


class TestFullBuild:
    def test_first_build_indexes_all_python_files(self, tmp_path):
        _write(tmp_path / "a.py", "def alpha():\n    pass\n")
        _write(tmp_path / "sub" / "b.py", "def beta():\n    pass\n")
        idx = _build(tmp_path, tmp_path / ".db")
        assert {f["path"] for f in idx.files} == {"a.py", "sub/b.py"}
        assert {s["name"] for s in idx.all_symbols()} == {"alpha", "beta"}

    def test_meta_records_root_and_schema_version(self, tmp_path):
        _write(tmp_path / "a.py", "x = 1\n")
        idx = _build(tmp_path, tmp_path / ".db")
        assert idx.meta["root"] == str(tmp_path.resolve())
        assert idx.meta["schema_version"] == 3

    def test_unsupported_extension_is_ignored(self, tmp_path):
        _write(tmp_path / "data.txt", "not a source file\n")
        _write(tmp_path / "code.py", "def f():\n    pass\n")
        idx = _build(tmp_path, tmp_path / ".db")
        assert _files_in(idx) == {"code.py"}


# ----- incremental: no-op / modify / delete / rename --------------------------


class TestIncremental:
    def test_noop_incremental_reuses_all_files(self, tmp_path):
        _write(tmp_path / "a.py", "def alpha():\n    pass\n")
        _build(tmp_path, tmp_path / ".db")
        # Run a second time with no source changes — sha1 should match for
        # every file and the symbol/ref shape stays identical.
        before_idx = load_index(tmp_path / ".db")
        before_syms = list(before_idx.all_symbols())
        idx2 = _build(tmp_path, tmp_path / ".db")
        after_syms = list(idx2.all_symbols())
        assert before_syms == after_syms

    def test_one_file_modified_only_that_file_rewalked(self, tmp_path):
        _write(tmp_path / "a.py", "def alpha():\n    pass\n")
        _write(tmp_path / "b.py", "def beta():\n    pass\n")
        _build(tmp_path, tmp_path / ".db")
        # Modify only a.py — rename alpha → alpha2.
        _write(tmp_path / "a.py", "def alpha2():\n    pass\n")
        idx = _build(tmp_path, tmp_path / ".db")
        names = {s["name"] for s in idx.all_symbols()}
        assert names == {"alpha2", "beta"}
        # b.py's sha1 stayed the same — its records survived.
        beta = idx.find_symbols(name="beta")
        assert len(beta) == 1
        assert beta[0]["file"] == "b.py"

    def test_file_deletion_purges_records(self, tmp_path):
        _write(tmp_path / "a.py", "def alpha():\n    pass\n")
        _write(tmp_path / "b.py", "def beta():\n    pass\n")
        _build(tmp_path, tmp_path / ".db")
        (tmp_path / "b.py").unlink()
        idx = _build(tmp_path, tmp_path / ".db")
        assert _files_in(idx) == {"a.py"}
        assert idx.find_symbols(name="beta") == []

    def test_file_rename_indexes_new_path_and_drops_old(self, tmp_path):
        _write(tmp_path / "old.py", "def alpha():\n    pass\n")
        _build(tmp_path, tmp_path / ".db")
        (tmp_path / "old.py").rename(tmp_path / "new.py")
        idx = _build(tmp_path, tmp_path / ".db")
        assert _files_in(idx) == {"new.py"}
        hit = idx.find_symbols(name="alpha")
        assert len(hit) == 1
        assert hit[0]["file"] == "new.py"


class TestStatFirstRefresh:
    """v10.93.0 — 파일을 읽기 전에 stat 으로 가른다 (docs/code-index/DESIGN.md §18)."""

    def test_unchanged_tree_writes_nothing(self, tmp_path, monkeypatch):
        _write(tmp_path / "a.py", "def alpha():\n    pass\n")
        db = tmp_path / ".db"
        stats = build(tmp_path, db, defs_path=None, verbose=False)
        assert stats["unchanged"] is False and stats["changed"] == 1
        before = db.stat()
        import agent_cli.code_index.builder as b

        monkeypatch.setattr(
            b, "write_sqlite_index", lambda *a, **k: pytest.fail("wrote the DB")
        )
        monkeypatch.setattr(
            Path, "read_bytes", lambda self: pytest.fail(f"read {self}")
        )
        stats = build(tmp_path, db, defs_path=None, verbose=False)
        assert stats == {
            "unchanged": True,
            "reused": 1,
            "changed": 0,
            "affected": 0,
            "removed": 0,
        }
        assert db.stat().st_mtime_ns == before.st_mtime_ns

    def test_touched_file_with_same_content_is_reused(self, tmp_path):
        _write(tmp_path / "a.py", "def alpha():\n    pass\n")
        db = tmp_path / ".db"
        build(tmp_path, db, defs_path=None, verbose=False)
        # mtime 만 바꾼다 — 내용은 그대로: sha1 이 가르고, 새 mtime 이 저장된다
        import os

        os.utime(tmp_path / "a.py", ns=(10**18, 10**18))
        stats = build(tmp_path, db, defs_path=None, verbose=False)
        assert stats["unchanged"] is False and stats["reused"] == 1
        idx = load_index(db)
        assert idx.files[0]["mtime_ns"] == 10**18
        assert _names_in_file(idx, "a.py") == {"alpha"}
        assert build(tmp_path, db, defs_path=None, verbose=False)["unchanged"] is True

    def test_a_file_with_no_symbols_is_reused_too(self, tmp_path):
        """종전엔 기호가 없는 파일(설정만 든 JS 등)은 재사용 조건(`rel in
        old_syms_by_file`)에 걸려 쿼리마다 다시 파싱됐다 — 프로파일의 JS 95회."""
        _write(tmp_path / "empty.js", "// nothing to index\n")
        _write(tmp_path / "a.py", "def alpha():\n    pass\n")
        db = tmp_path / ".db"
        build(tmp_path, db, defs_path=None, verbose=False)
        stats = build(tmp_path, db, defs_path=None, verbose=False)
        assert stats["unchanged"] is True and stats["reused"] == 2

    def test_changed_file_rows_replaced_and_the_rest_copied(self, tmp_path):
        _write(tmp_path / "a.py", "def alpha():\n    helper()\n")
        _write(tmp_path / "b.py", "def beta():\n    pass\n")
        _write(tmp_path / "c.py", "def helper():\n    pass\n")
        db = tmp_path / ".db"
        build(tmp_path, db, defs_path=None, verbose=False)
        full = load_index(db)
        full_refs = sorted(r.items() for r in full.all_refs())
        _write(tmp_path / "b.py", "def beta2():\n    pass\n")
        stats = build(tmp_path, db, defs_path=None, verbose=False)
        assert stats["changed"] == 1 and stats["reused"] == 2 and stats["removed"] == 0
        idx = load_index(db)
        assert {s["name"] for s in idx.all_symbols()} == {"alpha", "beta2", "helper"}
        # 바뀌지 않은 파일의 참조 행은 그대로 복사됐다
        assert sorted(r.items() for r in idx.all_refs()) == full_refs
        # 처음부터 다시 만든 것과 같다
        build(tmp_path, tmp_path / ".full", defs_path=None, verbose=False)
        again = load_index(tmp_path / ".full")

        def strip(rows):
            rows = sorted(rows, key=lambda s: (s["file"], s["line"], s["name"]))
            return [{k: v for k, v in r.items() if k != "id"} for r in rows]

        assert strip(idx.all_symbols()) == strip(again.all_symbols())
        assert strip(idx.all_refs()) == strip(again.all_refs())

    def test_new_name_re_refs_the_files_that_mention_it_once(self, tmp_path):
        """새 이름이 생기면 그 이름을 쓰는 재사용 파일은 Pass 2 를 다시 돈다 — 그
        파일의 참조 행은 복사하지 않고 새로 쓴 것만 남는다(중복 0)."""
        _write(tmp_path / "a.py", "def alpha():\n    later()\n")
        db = tmp_path / ".db"
        build(tmp_path, db, defs_path=None, verbose=False)
        before = load_index(db).find_refs(name="later")
        _write(tmp_path / "b.py", "def later():\n    pass\n")
        stats = build(tmp_path, db, defs_path=None, verbose=False)
        assert stats["changed"] == 1 and stats["affected"] == 1
        refs = load_index(db).find_refs(name="later")

        def rows(rs):
            return sorted((r["file"], r["line"], r["kind"]) for r in rs)

        # 정의되기 전엔 call 하나, 정의된 뒤엔 name 참조가 더해진다 — 영향 파일의
        # Pass 2 가 다시 돌았고, 복사 + 재생성으로 겹친 행은 없다
        assert rows(before) == [("a.py", 2, "call")]
        assert rows(refs) == [("a.py", 2, "call"), ("a.py", 2, "name")]

    def test_removed_file_is_counted_and_gone(self, tmp_path):
        _write(tmp_path / "a.py", "def alpha():\n    pass\n")
        _write(tmp_path / "b.py", "def beta():\n    pass\n")
        db = tmp_path / ".db"
        build(tmp_path, db, defs_path=None, verbose=False)
        (tmp_path / "b.py").unlink()
        stats = build(tmp_path, db, defs_path=None, verbose=False)
        assert stats["removed"] == 1 and stats["reused"] == 1
        assert _files_in(load_index(db)) == {"a.py"}


# ----- invalidation paths ----------------------------------------------------


class TestInvalidation:
    def test_schema_version_mismatch_forces_full_rebuild(self, tmp_path):
        _write(tmp_path / "a.py", "def alpha():\n    pass\n")
        _build(tmp_path, tmp_path / ".db")
        # Tamper with the stored schema_version so the next build sees a
        # mismatch and discards the old index.
        conn = sqlite3.connect(str(tmp_path / ".db"))
        try:
            conn.execute(
                "UPDATE meta SET value = ? WHERE key = 'schema_version'",
                (json.dumps(999),),
            )
            conn.commit()
        finally:
            conn.close()
        # Modify the file content so the rebuild has observable effect on
        # symbol names — proves the symbols are re-extracted, not reused.
        _write(tmp_path / "a.py", "def alpha2():\n    pass\n")
        idx = _build(tmp_path, tmp_path / ".db")
        assert idx.meta["schema_version"] == 3
        assert {s["name"] for s in idx.all_symbols()} == {"alpha2"}

    def test_root_mismatch_forces_full_rebuild(self, tmp_path):
        # Build over root A.
        root_a = tmp_path / "ra"
        _write(root_a / "a.py", "def alpha():\n    pass\n")
        _build(root_a, tmp_path / ".db")
        # Now point build at a DIFFERENT root with different content
        # while keeping the same DB path. The stored meta.root differs
        # → full rebuild, and the symbols come from root_b.
        root_b = tmp_path / "rb"
        _write(root_b / "b.py", "def beta():\n    pass\n")
        idx = _build(root_b, tmp_path / ".db")
        assert idx.meta["root"] == str(root_b.resolve())
        assert {s["name"] for s in idx.all_symbols()} == {"beta"}

    def test_force_full_rebuild_ignores_reusable_index(self, tmp_path):
        _write(tmp_path / "a.py", "def alpha():\n    pass\n")
        idx1 = _build(tmp_path, tmp_path / ".db")
        # Assert the rebuild succeeded functionally: with the same file
        # content and ``force_full=True``, symbols come back identical
        # (proving the rebuild ran without erroring out).
        idx2 = _build(tmp_path, tmp_path / ".db", force_full=True)
        names_before = {s["name"] for s in idx1.all_symbols()}
        names_after = {s["name"] for s in idx2.all_symbols()}
        assert names_after == names_before
        # The built_at field is rewritten — it's either equal (sub-second
        # rebuild) or later. We just assert it exists.
        assert idx2.meta["built_at"] is not None
        # Pin: force_full overrides reuse — verify by tampering with the
        # stored sha1 so reuse WOULD pick stale data if not for the flag.
        conn = sqlite3.connect(str(tmp_path / ".db"))
        try:
            conn.execute("UPDATE files SET sha1 = 'bogus_sha' WHERE path = 'a.py'")
            conn.commit()
        finally:
            conn.close()
        # With force_full=True, the bogus sha1 is irrelevant; the file is
        # rewalked anyway.
        idx3 = _build(tmp_path, tmp_path / ".db", force_full=True)
        assert {s["name"] for s in idx3.all_symbols()} == {"alpha"}
        # The DB rewrite recorded the correct sha1.
        files = {f["path"]: f for f in idx3.files}
        assert files["a.py"]["sha1"] != "bogus_sha"


# ----- Option-B re-Pass2 -----------------------------------------------------


class TestOptionBRefRecomputation:
    """When a new top-level name is added in file A, any unchanged file
    that mentions that identifier in its source must be re-walked so the
    new cross-file ``kind='name'`` ref appears."""

    def test_added_name_reaches_unchanged_caller(self, tmp_path):
        # py_walk_refs emits ``kind='call'`` unconditionally for call
        # expressions, but ``kind='name'`` (plain identifier mention)
        # only when the identifier appears in ``defined_names``. So the
        # Option-B re-Pass2 path is observable via the ``kind='name'``
        # path: an unchanged file mentioning an identifier that becomes
        # newly defined elsewhere should pick up a ``kind='name'`` ref
        # after the rebuild, even though its own sha1 didn't change.
        _write(
            tmp_path / "a.py",
            "def existing():\n    pass\n",
        )
        _write(
            tmp_path / "b.py",
            # Bare identifier mention (not a call) — function reference
            # passed around as a value, the canonical ``kind='name'`` case.
            "callback = new_helper\n",
        )
        idx1 = _build(tmp_path, tmp_path / ".db")
        # No ref of kind='name' yet — new_helper isn't defined anywhere.
        before_name_refs = [
            r for r in idx1.find_refs(name="new_helper") if r["kind"] == "name"
        ]
        assert before_name_refs == []

        # Add `new_helper` to a.py without touching b.py.
        _write(
            tmp_path / "a.py",
            "def existing():\n    pass\ndef new_helper():\n    pass\n",
        )
        idx2 = _build(tmp_path, tmp_path / ".db")

        # Re-Pass2 should have re-walked b.py because `new_helper` (a
        # newly-added name) appears in b.py's identifier set. The
        # plain-identifier reference site in b.py is now recorded as
        # ``kind='name'``.
        after_name_refs = [
            r for r in idx2.find_refs(name="new_helper") if r["kind"] == "name"
        ]
        files = {r["file"] for r in after_name_refs}
        assert "b.py" in files


# ----- preproc_fingerprint invalidation --------------------------------------


class TestPreprocFingerprintInvalidation:
    """A change to the preprocessing config (defs_path content) bumps
    meta.preproc_fingerprint, which forces a full rebuild even if every
    file's sha1 is unchanged. Without a defs_path the fingerprint is
    a constant for a given SCHEMA_VERSION (no flags → empty join),
    so we have to provide one to vary it."""

    def test_defs_file_content_change_changes_fingerprint(self, tmp_path):
        _write(tmp_path / "a.c", "int main(void) { return 0; }\n")
        defs = tmp_path / "preproc.defs"
        defs.write_text("#define CONFIG_A 1\n")
        build(
            tmp_path,
            tmp_path / ".db",
            defs_path=defs,
            verbose=False,
            undef_unknown_configs=False,
        )
        before_fp = load_index(tmp_path / ".db").meta.get("preproc_fingerprint")

        # Change the defs file content — different unifdef flag set →
        # different sorted-joined hash input → different fingerprint.
        defs.write_text("#define CONFIG_B 1\n")
        build(
            tmp_path,
            tmp_path / ".db",
            defs_path=defs,
            verbose=False,
            undef_unknown_configs=False,
        )
        after_fp = load_index(tmp_path / ".db").meta.get("preproc_fingerprint")
        assert before_fp != after_fp
