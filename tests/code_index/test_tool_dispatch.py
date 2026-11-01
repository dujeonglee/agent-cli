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


class TestCallgraphOutput:
    """v10.96.0 — 정의마다 머리, 해석된 이웃 줄, 타입 모를 객체는 ``?`` 줄."""

    @pytest.fixture
    def tree(self, tmp_path, monkeypatch):
        _write(
            tmp_path / "a.py",
            "def get():\n    return 1\n\n\nclass Box:\n    def get(self):\n"
            "        return 2\n\n    def use(self):\n        return self.get()\n",
        )
        _write(
            tmp_path / "b.py",
            "from a import get\n\n\ndef caller(obj):\n    get()\n"
            "    return obj.get()\n",
        )
        _write(
            tmp_path / "c.js",
            "function shared() { return 1; }\nfunction usesIt() { return shared(); }\n",
        )
        monkeypatch.chdir(tmp_path)
        return tmp_path

    def test_each_definition_is_a_section_and_unknown_calls_are_marked(self, tree):
        out = _dispatch_one({"mode": "callers", "name": "get"}).output
        assert out == (
            "get — a.py:1-2\n"
            "caller  (1×)  b.py:5\n\n"
            "Box.get — a.py:6-7\n"
            "Box.use  (1×)  a.py:10\n\n"
            '? "get" on an object of unknown type — any of 2 definitions:  b.py:6'
        )

    def test_a_qualified_name_picks_one_definition(self, tree):
        out = _dispatch_one({"mode": "callers", "name": "Box.get"}).output
        assert out.startswith("Box.get — a.py:6-7\nBox.use  (1×)  a.py:10")
        assert "\ncaller " not in out

    def test_callees_mark_unknown_type_calls(self, tree):
        out = _dispatch_one({"mode": "callees", "name": "caller"}).output
        assert out == (
            "caller — b.py:4-6\n"
            "get  (1×)  b.py:5\n"
            "? get  (1×, any of 2 definitions)  b.py:6"
        )

    def test_languages_without_scope_rules_say_so(self, tree):
        out = _dispatch_one({"mode": "callers", "name": "shared"}).output
        assert out == (
            "shared — c.js:1-1, matched by name only (javascript is not "
            "scope-resolved)\nusesIt  (1×)  c.js:2"
        )


class TestPickingADefinition:
    """v10.97.0 (DESIGN §21) — `refs` 태그, `path`·모듈로 한정한 이름으로 정의 고르기."""

    @pytest.fixture
    def tree(self, tmp_path, monkeypatch):
        _write(tmp_path / "pkg/__init__.py", "")
        _write(
            tmp_path / "pkg/fsio.py",
            "def _write(p):\n    return p\n\n\ndef save(p):\n    return _write(p)\n",
        )
        _write(
            tmp_path / "pkg/other.py",
            "def _write(q):\n    return q\n\n\ndef dump(q):\n    return _write(q)\n",
        )
        _write(
            tmp_path / "user.py",
            "from pkg.fsio import _write\n\n\ndef go(obj, cb):\n"
            "    _write(1)\n    obj._write(2)\n    cb()\n    return len(obj)\n",
        )
        monkeypatch.chdir(tmp_path)
        return tmp_path

    def test_refs_are_tagged_with_what_they_resolve_to(self, tree):
        out = _dispatch_one({"mode": "refs", "name": "_write"}).output
        assert out.splitlines() == [
            "pkg/fsio.py:6:11 call _write → _write  @pkg/fsio.py",
            "pkg/other.py:6:11 call _write → _write  @pkg/other.py",
            "user.py:5:4 call _write → _write  @pkg/fsio.py",
            "user.py:6:8 call _write → ?",
            '? = "_write" on an object of unknown type — any of 2 definitions',
        ]

    def test_local_and_external_tags(self, tree):
        out = _dispatch_one({"mode": "refs", "name": "cb"}).output
        assert out == "user.py:7:4 call cb → local"
        out = _dispatch_one({"mode": "refs", "name": "len"}).output
        assert out == "user.py:8:11 call len → external"

    def test_module_qualified_name_picks_one(self, tree):
        out = _dispatch_one({"mode": "refs", "name": "fsio._write"}).output
        assert out.splitlines() == [
            "pkg/fsio.py:6:11 call _write → _write  @pkg/fsio.py",
            "user.py:5:4 call _write → _write  @pkg/fsio.py",
            (
                '? 1 more uses of "_write" on objects of unknown type may reach it — '
                '"refs" with the bare name lists them.'
            ),
        ]
        out = _dispatch_one({"mode": "callers", "name": "pkg.other._write"}).output
        assert out == (
            "_write — pkg/other.py:1-2\ndump  (1×)  pkg/other.py:6\n\n"
            '? "_write" on an object of unknown type — any of 2 definitions:  '
            "user.py:6"
        )

    def test_path_picks_the_definition_in_that_file(self, tree):
        out = _dispatch_one(
            {"mode": "callers", "name": "_write", "path": "pkg/fsio.py"}
        ).output
        assert out.startswith("_write — pkg/fsio.py:1-2\n")
        assert "pkg/other.py" not in out.split("\n\n")[0]
        out = _dispatch_one(
            {"mode": "refs", "name": "_write", "path": "pkg/other.py"}
        ).output
        assert out.splitlines()[0] == (
            "pkg/other.py:6:11 call _write → _write  @pkg/other.py"
        )

    def test_path_without_that_definition_says_where_it_is(self, tree):
        r = _dispatch_one({"mode": "callees", "name": "_write", "path": "user.py"})
        assert not r.success
        assert r.error == (
            '"_write" is not defined in user.py — it is defined in pkg/fsio.py, '
            "pkg/other.py"
        )
        r = _dispatch_one({"mode": "refs", "name": "_write", "path": "nowhere.py"})
        assert not r.success and "is not a file in the index" in r.error

    def test_slice_takes_the_picked_definition(self, tree):
        out = _dispatch_one(
            {"mode": "slice", "name": "_write", "path": "pkg/other.py"}
        ).output
        assert "pkg/other.py:1-2" in out and "pkg/fsio.py" not in out
        out = _dispatch_one({"mode": "slice", "name": "_write"}).output
        assert "1 more definitions share this name" in out

    def test_slice_says_what_it_did_not_follow(self, tree):
        out = _dispatch_one(
            {"mode": "slice", "name": "go", "with_callees": True}
        ).output
        assert "_? 1 callee sites on objects of unknown type not followed._" in out


class TestPickingReviewCases:
    """v10.97.0 리뷰 — 규칙 없는 언어의 꼬리 줄·언어로 좁히기, C++ ``::``, `path` 와
    없는 한정 이름, 모호한 `path`, note 의 수, slice 의 겹친 자리."""

    @pytest.fixture
    def tree(self, tmp_path, monkeypatch):
        _write(
            tmp_path / "c/a.c",
            "int foo(void) { return 1; }\nint bar(void) { return foo(); }\n",
        )
        _write(tmp_path / "web/utils.js", "function foo() { return 1; }\n")
        _write(tmp_path / "web/main.js", "function run() { return foo(); }\n")
        _write(
            tmp_path / "cpp/x.cpp",
            "namespace outer { namespace ns { int f() { return 1; } } }\n"
            "int g() { return outer::ns::f(); }\n",
        )
        _write(tmp_path / "pkg/__init__.py", "")
        _write(tmp_path / "app/__init__.py", "")
        _write(
            tmp_path / "pkg/core.py",
            "def helper():\n    return 1\n\n\nclass Box:\n    def get(self):\n"
            "        return helper()\n\n    def save(self):\n        return helper()\n"
            "\n\nclass Other:\n    def save(self):\n        return helper()\n",
        )
        _write(
            tmp_path / "app/use.py",
            "def go(o):\n    o.get()\n    o.save()\n    return o.save()\n\n\nx.get()\n",
        )
        monkeypatch.chdir(tmp_path)
        return tmp_path

    def test_name_only_languages_say_so_once_per_language(self, tree):
        lines = _dispatch_one({"mode": "refs", "name": "foo"}).output.splitlines()
        tail = '? = "foo" matched by name only ({} is not scope-resolved) — any of 1'
        assert lines[-2:] == [
            tail.format("c") + " definitions",
            tail.format("javascript") + " definitions",
        ]

    def test_a_picked_definition_keeps_its_language(self, tree):
        out = _dispatch_one({"mode": "refs", "name": "utils.foo"}).output
        assert "c/a.c" not in out and "web/main.js" in out
        out = _dispatch_one({"mode": "refs", "name": "a.foo"}).output
        assert "web/" not in out and "c/a.c" in out

    def test_cpp_qualified_suffix(self, tree):
        from agent_cli.tools.code_index import _ensure_index

        store, _ = _ensure_index()
        picked = store.definitions("ns::f", fallback=False)
        assert [d["qualified_name"] for d in picked] == ["outer::ns::f"]

    def test_path_with_a_missing_qualified_name_is_an_error(self, tree):
        r = _dispatch_one({"mode": "refs", "name": "Nope.get", "path": "pkg/core.py"})
        assert not r.success and r.error == '"Nope.get" is not defined in pkg/core.py'

    def test_an_ambiguous_path_lists_the_files(self, tree):
        r = _dispatch_one({"mode": "callers", "name": "helper", "path": "__init__.py"})
        assert not r.success and r.error == (
            '`path` "__init__.py" matches several files: app/__init__.py, '
            "pkg/__init__.py"
        )

    def test_the_note_counts_what_bare_refs_marks(self, tree):
        out = _dispatch_one({"mode": "refs", "name": "Box.get"}).output
        bare = _dispatch_one({"mode": "refs", "name": "get"}).output
        n = sum(1 for ln in bare.splitlines() if ln.endswith("→ ?"))
        assert n == 2
        assert f'? {n} more uses of "get"' in out

    def test_slice_with_path_takes_a_class(self, tree):
        out = _dispatch_one({"mode": "slice", "name": "Box", "path": "pkg/core.py"})
        assert out.success and "### Box  (type)  — pkg/core.py:5-" in out.output
        r = _dispatch_one({"mode": "slice", "name": "Box", "path": "app/use.py"})
        assert not r.success and r.error == (
            '"Box" is not defined in app/use.py — it is defined in pkg/core.py'
        )

    def test_slice_counts_each_unfollowed_site_once(self, tree):
        out = _dispatch_one(
            {"mode": "slice", "name": "helper", "with_callers": True, "depth": 2}
        ).output
        # o.get() 하나 + o.save() 둘 — save 의 정의가 둘이라 겹쳐 세면 5
        assert "_? 3 caller sites on objects of unknown type not followed._" in out


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
