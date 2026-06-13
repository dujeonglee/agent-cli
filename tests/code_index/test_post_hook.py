"""Tests for the code_index post-hook wiring on edit_file / write_file.

After a successful edit_file or write_file, the post-hook calls
``code_index.post_hook(path)`` which runs ``build()`` on the existing
index DB so the next query sees the change without the model having to
trigger ``mode='build'`` itself.

Key contracts:

  - The hook runs only when an index DB already exists. A no-op edit
    on a brand-new tree does NOT auto-create the index (lazy build is
    a model-initiated action).
  - Out-of-root paths are no-ops; we don't track files we don't index.
  - Failed edits (no write happened) do NOT trigger the hook because
    edit_file returns early before reaching it.
  - The hook is best-effort: a buggy build call never breaks the
    user-facing edit.
"""

from __future__ import annotations

import pytest

from agent_cli.tools.code_index import (
    _dispatch_one,
    _resolve_index_root,
    post_hook,
)
from agent_cli.tools.edit_file import tool_edit_file
from agent_cli.tools.read_file import compute_line_hash
from agent_cli.tools.write_file import tool_write_file


def _write(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


@pytest.fixture
def project(tmp_path, monkeypatch):
    """Tiny project + chdir + initial build (so the DB exists)."""
    _write(tmp_path / "mod.py", "def alpha():\n    pass\n")
    monkeypatch.chdir(tmp_path)
    # Trigger lazy build so the DB exists for subsequent hooks to find.
    _dispatch_one({"mode": "build"})
    return tmp_path


class TestEditFileTriggersRefresh:
    def test_edit_updates_index_for_changed_symbol(self, project):
        # Confirm initial state.
        r = _dispatch_one({"mode": "lookup", "name": "alpha"})
        assert r.success and "alpha" in r.output

        # Replace the function name via edit_file.
        lines = (project / "mod.py").read_text().split("\n")
        h1 = compute_line_hash(1, lines[0])
        r_edit = tool_edit_file(
            {
                "path": "mod.py",
                "op": "replace",
                "pos": f"1#{h1}",
                "lines": ["def beta():"],
            }
        )
        assert r_edit.success, r_edit.error

        # The post-hook should have refreshed the index — the new name
        # is now lookup-able and the old name is gone.
        r_new = _dispatch_one({"mode": "lookup", "name": "beta"})
        assert r_new.success
        assert "beta" in r_new.output
        r_old = _dispatch_one({"mode": "lookup", "name": "alpha"})
        assert r_old.success
        assert "no symbols match" in r_old.output


class TestWriteFileTriggersRefresh:
    def test_write_new_file_picked_up_immediately(self, project):
        # The new file is not yet in the index.
        r_before = _dispatch_one({"mode": "lookup", "name": "gamma"})
        assert r_before.success and "no symbols match" in r_before.output

        # Create a new file with a new symbol via write_file.
        r_write = tool_write_file(
            {"path": "new_mod.py", "content": "def gamma():\n    return 7\n"}
        )
        assert r_write.success

        # Post-hook should have indexed it.
        r_after = _dispatch_one({"mode": "lookup", "name": "gamma"})
        assert r_after.success
        assert "gamma" in r_after.output

    def test_write_overwrite_updates_symbols(self, project):
        # Overwrite mod.py with different contents.
        r_write = tool_write_file(
            {"path": "mod.py", "content": "def delta():\n    return 9\n"}
        )
        assert r_write.success
        r_lookup = _dispatch_one({"mode": "lookup", "name": "delta"})
        assert r_lookup.success
        assert "delta" in r_lookup.output
        # alpha should be gone after the overwrite.
        r_alpha = _dispatch_one({"mode": "lookup", "name": "alpha"})
        assert "no symbols match" in r_alpha.output


class TestPostHookContracts:
    def test_no_db_yet_is_noop(self, tmp_path, monkeypatch):
        # Fresh tree, no .agent-cli/ anywhere, no build ever ran.
        monkeypatch.chdir(tmp_path)
        _write(tmp_path / "lonely.py", "def x(): pass\n")
        # Hook should silently do nothing — no DB to refresh.
        post_hook(tmp_path / "lonely.py")
        # No DB should have been created behind our back.
        assert not (tmp_path / ".agent-cli" / "code_index.db").is_file()

    def test_out_of_root_path_is_noop(self, project, tmp_path_factory):
        # Path outside the indexed root → no-op.
        other = tmp_path_factory.mktemp("elsewhere")
        _write(other / "z.py", "def z(): pass\n")
        # Should silently succeed without touching the indexed root's
        # DB. Verify by snapshotting symbol count before/after.
        n_before = _dispatch_one({"mode": "kind", "symbol_kind": "function"})
        post_hook(other / "z.py")
        n_after = _dispatch_one({"mode": "kind", "symbol_kind": "function"})
        assert n_before.output == n_after.output

    def test_failed_edit_does_not_run_hook(self, project):
        # An edit with a stale hash fails BEFORE the write happens →
        # post_hook is not called → the index is unchanged. We assert
        # by passing a wrong hash and checking alpha is still there.
        r_edit = tool_edit_file(
            {
                "path": "mod.py",
                # `AA` is not the real hash for line 1; edit_file
                # rejects the call before mutating the file.
                "op": "replace",
                "pos": "1#AA",
                "lines": ["def beta():"],
            }
        )
        assert r_edit.success is False
        # The original symbol is still discoverable.
        r = _dispatch_one({"mode": "lookup", "name": "alpha"})
        assert "alpha" in r.output

    def test_post_hook_swallows_internal_exceptions(self, project, monkeypatch):
        # If build() somehow raises, post_hook MUST NOT propagate. We
        # patch the build symbol that post_hook resolves at call time
        # so the exception goes through the swallow path.
        import agent_cli.tools.code_index as ci_mod

        def boom(*_a, **_kw):
            raise RuntimeError("simulated build failure")

        monkeypatch.setattr(ci_mod, "build", boom)
        # The call should still return None cleanly.
        result = post_hook("mod.py")
        assert result is None


class TestPostHookResolverConsistency:
    def test_hook_uses_same_root_as_tool(self, project):
        # _resolve_index_root is what both tool_code_index and
        # post_hook consult; sanity-check that the chdir into the
        # project tree resolves to the project root (no surprise
        # parent-dir .agent-cli/ pickup).
        assert _resolve_index_root() == project
