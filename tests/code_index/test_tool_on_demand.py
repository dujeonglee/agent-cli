"""Tests for the on-demand parse fallback when ``code_index`` is asked
about a file outside the indexed root.

The indexed root is whatever directory the resolver finds (cwd or
nearest ancestor with ``.agent-cli/``). Files outside that root cannot
participate in cross-file queries (lookup, refs, callers, callees,
slice, build, file) — those return a clear error. Per-file modes
(``list``, ``fetch``) fall through to an in-process tree-sitter parse
that does NOT write the DB.
"""

from __future__ import annotations

import pytest

from agent_cli.tools.code_index import _dispatch_one


def _write(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


@pytest.fixture
def isolated_root(tmp_path, monkeypatch):
    """Set up an indexed root in ``tmp_path/proj/`` and chdir into it.
    Returns ``(proj_root, outside_dir)``."""
    proj = tmp_path / "proj"
    outside = tmp_path / "outside"
    proj.mkdir()
    outside.mkdir()
    # Trivial in-root file so the index builds successfully.
    _write(proj / "in_root.py", "def in_root_fn():\n    pass\n")
    # File OUTSIDE the indexed root.
    _write(outside / "stranger.py", "def outside_fn():\n    return 42\n")
    monkeypatch.chdir(proj)
    return proj, outside


class TestOnDemandList:
    def test_list_outside_root_uses_parse_fallback(self, isolated_root):
        _proj, outside = isolated_root
        r = _dispatch_one({"mode": "list", "path": str(outside / "stranger.py")})
        assert r.success
        # The symbol defined in the out-of-root file shows up.
        assert "outside_fn" in r.output

    def test_list_inside_root_uses_index(self, isolated_root):
        r = _dispatch_one({"mode": "list", "path": "in_root.py"})
        assert r.success
        assert "in_root_fn" in r.output


class TestOnDemandFetch:
    def test_fetch_outside_root_returns_body(self, isolated_root):
        _proj, outside = isolated_root
        r = _dispatch_one(
            {
                "mode": "fetch",
                "path": str(outside / "stranger.py"),
                "name": "outside_fn",
            }
        )
        assert r.success
        # Hashline-formatted body — the defining keyword is in there.
        assert "outside_fn" in r.output
        assert "return 42" in r.output

    def test_fetch_outside_root_symbol_not_found(self, isolated_root):
        _proj, outside = isolated_root
        r = _dispatch_one(
            {
                "mode": "fetch",
                "path": str(outside / "stranger.py"),
                "name": "nope",
            }
        )
        assert r.success is False
        assert "symbol not found" in r.error


class TestOutOfRootRejected:
    """Modes that require the index reject out-of-root paths cleanly."""

    def test_file_mode_rejects_out_of_root_path(self, isolated_root):
        _proj, outside = isolated_root
        r = _dispatch_one({"mode": "file", "path": str(outside / "stranger.py")})
        assert r.success is False
        assert "outside" in r.error


class TestOnDemandNoDbWrite:
    """The on-demand parse path must NOT persist symbols to the DB.
    A subsequent index-scoped lookup for the out-of-root symbol should
    still come up empty."""

    def test_out_of_root_symbol_does_not_leak_into_index(self, isolated_root):
        _proj, outside = isolated_root
        # Touch the out-of-root file via on-demand list/fetch.
        _dispatch_one({"mode": "list", "path": str(outside / "stranger.py")})
        _dispatch_one(
            {
                "mode": "fetch",
                "path": str(outside / "stranger.py"),
                "name": "outside_fn",
            }
        )
        # Now look it up through the index — should NOT be found.
        r = _dispatch_one({"mode": "lookup", "name": "outside_fn"})
        assert r.success
        assert "no symbols" in r.output
