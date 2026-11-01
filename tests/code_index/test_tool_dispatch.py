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
        assert r.error.startswith('`mode` is required — one of "list", "fetch"')
        assert '"build"' not in r.error  # v10.92.0: 모드 삭제

    def test_unknown_mode_rejected(self):
        r = _dispatch_one({"mode": "explode"})
        assert r.success is False
        assert r.error.startswith('`mode` "explode" is not one of "list", "fetch"')
        assert "'" not in r.error  # 표기 규칙 — Python repr 아님


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
        assert r_fn.success and r_fn.output.startswith(
            'No "function" named "Beta" among the '
        )
        assert "indexed symbols (" in r_fn.output and "files under " in r_fn.output
        assert r_ty.success and "Beta" in r_ty.output

    def test_lookup_rejects_unknown_symbol_kind(self, project):
        r = _dispatch_one({"mode": "lookup", "name": "helper", "symbol_kind": "bogus"})
        assert r.success is False
        assert r.error.startswith('`symbol_kind` "bogus" is not one of ')


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
        assert r.error.startswith('`ref_kind` "bogus" is not one of ')


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
        assert r.output.startswith('No indexed function calls "run" among the ')


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


class TestEmptyIndexSaysSo:
    """v10.92.0 — 빈 결과는 색인의 범위를 말한다. 벤치에서 lookup 5·refs 3 이 전부
    빈 결과였는데 색인에 파일이 하나도 없다는 사실은 어디에도 없었다."""

    def test_no_files_at_all(self, tmp_path, monkeypatch):
        (tmp_path / "notes.txt").write_text("plain text only\n")
        monkeypatch.chdir(tmp_path)
        for q in (
            {"mode": "lookup", "name": "alpha"},
            {"mode": "refs", "name": "alpha", "ref_kind": "call"},
            {"mode": "callers", "name": "alpha"},
            {"mode": "callees", "name": "alpha"},
            {"mode": "kind", "symbol_kind": "function"},
        ):
            r = _dispatch_one(q)
            assert r.success, q
            assert r.output == (
                f"The index has no files: nothing under {tmp_path} has a "
                "supported extension."
            ), q

    def test_refs_and_callees_name_the_scope(self, project):
        r = _dispatch_one({"mode": "refs", "name": "ghost", "ref_kind": "call"})
        assert r.output.startswith('No "call" use of "ghost" among the ')
        r = _dispatch_one({"mode": "refs", "name": "ghost"})
        assert r.output.startswith('No use of "ghost" among the ')
        r = _dispatch_one({"mode": "callees", "name": "ghost"})
        assert r.output.startswith('"ghost" calls no indexed function among the ')
        r = _dispatch_one({"mode": "kind", "symbol_kind": "constant", "search": "zz"})
        assert r.output.startswith(
            'No "constant" symbol whose name matches "zz" among the '
        )

    def test_build_mode_is_gone(self, project):
        r = _dispatch_one({"mode": "build"})
        assert not r.success and r.error.startswith('`mode` "build" is not one of ')


class TestDefconfigWiring:
    """`<root>/.agent-cli/defconfig` opt-in wiring.

    Earlier the tool wrapper hard-coded ``defs_path=None`` for every
    ``build()`` call, so user-authored defconfigs had no reachable path
    through ``code_index`` modes — kernel-style C with ``#ifdef CONFIG_X``
    around function signatures parsed as ERROR runs and silently dropped
    the definition from the index.
    """

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
