"""Tests for ``agent_cli.tools.code_index._dispatch_one`` — the per-query
mode dispatch (code_index is flat-native, Step 3: one op = one query, so
``_dispatch_one`` is the entry point; the old batch ``tool_code_index``
wrapper was removed).

Tests use ``monkeypatch.chdir(tmp_path)`` so each test runs in an
isolated index root (the tool resolves cwd to find ``.agent-cli/``).
The default ``tmp_path`` has no ``.agent-cli/`` so the resolver falls
back to cwd, which is the tmp dir — the build is hermetic per test.
"""

from __future__ import annotations

import pytest

from agent_cli.tools.code_index import _dispatch_one


def _write(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


@pytest.fixture
def project(tmp_path, monkeypatch):
    """Build a tiny project tree and chdir into it. Returns the project root."""
    _write(
        tmp_path / "alpha.py",
        "def alpha():\n    return helper()\n\ndef helper():\n    return 1\n",
    )
    _write(
        tmp_path / "sub" / "beta.py",
        "class Beta:\n    def run(self):\n        alpha()\n",
    )
    _write(
        tmp_path / "doc.md",
        "# Top\n\n## Setup\n\nbody\n\n### Install\n\nmore\n",
    )
    monkeypatch.chdir(tmp_path)
    return tmp_path


class TestDispatchValidation:
    def test_query_must_be_dict(self):
        r = _dispatch_one("not a dict")
        assert r.success is False
        assert "must be an object" in r.error

    def test_mode_required(self):
        r = _dispatch_one({})
        assert r.success is False
        assert "mode" in r.error

    def test_unknown_mode_rejected(self):
        r = _dispatch_one({"mode": "explode"})
        assert r.success is False
        assert "unknown mode" in r.error


class TestList:
    def test_list_emits_outline(self, project):
        r = _dispatch_one({"mode": "list", "path": "alpha.py"})
        assert r.success
        # Both functions appear in outline form.
        assert "alpha" in r.output
        assert "helper" in r.output

    def test_list_requires_path(self, project):
        r = _dispatch_one({"mode": "list"})
        assert r.success is False
        assert "path" in r.error

    def test_list_path_not_found(self, project):
        r = _dispatch_one({"mode": "list", "path": "missing.py"})
        assert r.success is False
        assert "file not found" in r.error

    def test_list_unsupported_extension(self, project):
        _write(project / "data.txt", "irrelevant\n")
        r = _dispatch_one({"mode": "list", "path": "data.txt"})
        assert r.success is False
        assert "unsupported" in r.error

    def test_list_search_regex(self, project):
        r = _dispatch_one({"mode": "list", "path": "alpha.py", "search": r"^help"})
        assert r.success
        # Filtered to symbols whose name starts with `help`. The file
        # itself is alpha.py, so "alpha" appears in every output line
        # (as the file path) — assert on symbol-name presence/absence
        # via the leading column.
        names = [line.split(" ", 1)[0] for line in r.output.splitlines()]
        assert "helper" in names
        assert "alpha" not in names

    def test_list_invalid_search_regex(self, project):
        r = _dispatch_one({"mode": "list", "path": "alpha.py", "search": "(unclosed"})
        assert r.success is False
        assert "invalid search" in r.error


class TestFetch:
    def test_fetch_returns_hashline_body(self, project):
        r = _dispatch_one({"mode": "fetch", "path": "alpha.py", "name": "helper"})
        assert r.success
        # Header line + hashlined body (each line tagged like `5#XY:content`).
        assert "helper" in r.output.splitlines()[0]
        body = "\n".join(r.output.splitlines()[1:])
        assert "#" in body  # hashline marker
        # The defining keyword appears in the body.
        assert "def helper" in body

    def test_fetch_requires_name(self, project):
        r = _dispatch_one({"mode": "fetch", "path": "alpha.py"})
        assert r.success is False
        assert "name" in r.error

    def test_fetch_markdown_accepts_marker_form(self, project):
        # `## Setup` and `Setup` should both resolve to the same heading.
        r1 = _dispatch_one({"mode": "fetch", "path": "doc.md", "name": "## Setup"})
        r2 = _dispatch_one({"mode": "fetch", "path": "doc.md", "name": "Setup"})
        assert r1.success and r2.success
        assert r1.output == r2.output

    def test_fetch_symbol_not_found(self, project):
        r = _dispatch_one({"mode": "fetch", "path": "alpha.py", "name": "nonexistent"})
        assert r.success is False
        assert "symbol not found" in r.error


class TestLookup:
    def test_lookup_finds_by_name(self, project):
        r = _dispatch_one({"mode": "lookup", "name": "helper"})
        assert r.success
        assert "helper" in r.output
        assert "alpha.py" in r.output

    def test_lookup_requires_name(self, project):
        r = _dispatch_one({"mode": "lookup"})
        assert r.success is False

    def test_lookup_with_symbol_kind_filter(self, project):
        # `Beta` is a class → kind='type'. Filtering by 'function' should
        # not return it.
        r_fn = _dispatch_one(
            {"mode": "lookup", "name": "Beta", "symbol_kind": "function"}
        )
        r_ty = _dispatch_one({"mode": "lookup", "name": "Beta", "symbol_kind": "type"})
        assert r_fn.success and "(no symbols match" in r_fn.output
        assert r_ty.success and "Beta" in r_ty.output

    def test_lookup_rejects_unknown_symbol_kind(self, project):
        r = _dispatch_one({"mode": "lookup", "name": "helper", "symbol_kind": "bogus"})
        assert r.success is False
        assert "invalid symbol_kind" in r.error


class TestKind:
    def test_kind_lists_section_symbols(self, project):
        r = _dispatch_one({"mode": "kind", "symbol_kind": "section"})
        assert r.success
        # The markdown fixture has three headings: Top, Setup, Install.
        assert "Top" in r.output
        assert "Setup" in r.output
        assert "Install" in r.output

    def test_kind_requires_symbol_kind(self, project):
        r = _dispatch_one({"mode": "kind"})
        assert r.success is False
        assert "symbol_kind" in r.error


class TestFile:
    def test_file_lists_symbols_in_file(self, project):
        r = _dispatch_one({"mode": "file", "path": "sub/beta.py"})
        assert r.success
        assert "Beta" in r.output
        assert "run" in r.output

    def test_file_out_of_root_returns_error(self, project, tmp_path_factory):
        # Build a separate tmp dir entirely outside the indexed root.
        other = tmp_path_factory.mktemp("other_proj")
        _write(other / "x.py", "def x(): pass\n")
        r = _dispatch_one({"mode": "file", "path": str(other / "x.py")})
        assert r.success is False
        assert "outside" in r.error


class TestRefs:
    def test_refs_returns_call_sites(self, project):
        r = _dispatch_one({"mode": "refs", "name": "helper"})
        assert r.success
        # `helper` is called from `alpha`.
        assert "alpha.py" in r.output

    def test_refs_kind_filter(self, project):
        r = _dispatch_one({"mode": "refs", "name": "helper", "ref_kind": "call"})
        assert r.success
        for line in r.output.splitlines():
            assert " call " in line

    def test_refs_rejects_unknown_ref_kind(self, project):
        r = _dispatch_one({"mode": "refs", "name": "helper", "ref_kind": "bogus"})
        assert r.success is False
        assert "invalid ref_kind" in r.error


class TestCallgraph:
    def test_callers_finds_alpha_calls_helper(self, project):
        r = _dispatch_one({"mode": "callers", "name": "helper"})
        assert r.success
        assert "alpha" in r.output

    def test_callees_finds_alpha_calls_helper(self, project):
        r = _dispatch_one({"mode": "callees", "name": "alpha"})
        assert r.success
        assert "helper" in r.output

    def test_callers_of_uncalled_function(self, project):
        # `run` is a method but isn't called from anywhere in the fixture.
        r = _dispatch_one({"mode": "callers", "name": "run"})
        assert r.success
        assert "no callers" in r.output


class TestSlice:
    def test_slice_returns_markdown_blob(self, project):
        r = _dispatch_one({"mode": "slice", "name": "helper"})
        assert r.success
        # Slice output is a markdown blob with the "Slice:" header.
        assert "# Slice:" in r.output
        assert "helper" in r.output

    def test_slice_no_such_symbol(self, project):
        # cmd_slice doesn't raise — it returns a "no symbol" message.
        r = _dispatch_one({"mode": "slice", "name": "ghost"})
        assert r.success  # not an error per cmd_slice's contract
        assert "no symbol" in r.output


class TestBuild:
    def test_build_forces_full_rebuild(self, project):
        r = _dispatch_one({"mode": "build"})
        assert r.success
        assert "Rebuilt index" in r.output
        # Symbol count from the fixture (3 funcs + 1 class + 1 method + 3
        # markdown headings = 8). Exact value is fixture-dependent; just
        # confirm a non-zero count was reported.
        assert "symbols" in r.output


class TestDefconfigWiring:
    """`<root>/.agent-cli/defconfig` opt-in wiring.

    Earlier the tool wrapper hard-coded ``defs_path=None`` for every
    ``build()`` call, so user-authored defconfigs had no reachable path
    through ``code_index`` modes — kernel-style C with ``#ifdef CONFIG_X``
    around function signatures parsed as ERROR runs and silently dropped
    the definition from the index.
    """

    def test_build_reports_no_defconfig_when_absent(self, project):
        r = _dispatch_one({"mode": "build"})
        assert r.success
        assert "defconfig: (none)" in r.output

    def test_build_reports_defconfig_path_when_present(self, project):
        defs = project / ".agent-cli" / "defconfig"
        defs.parent.mkdir(parents=True, exist_ok=True)
        defs.write_text("#define CONFIG_EXAMPLE\n")
        r = _dispatch_one({"mode": "build"})
        assert r.success
        assert "defconfig:" in r.output
        assert str(defs) in r.output

    def test_resolve_defs_path_returns_none_when_missing(self, project):
        from agent_cli.tools.code_index import _resolve_defs_path

        assert _resolve_defs_path(project) is None

    def test_resolve_defs_path_returns_path_when_present(self, project):
        from agent_cli.tools.code_index import _resolve_defs_path

        defs = project / ".agent-cli" / "defconfig"
        defs.parent.mkdir(parents=True, exist_ok=True)
        defs.write_text("#define CONFIG_FOO\n")
        resolved = _resolve_defs_path(project)
        assert resolved == defs
