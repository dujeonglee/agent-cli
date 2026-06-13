"""Tests for the hashline-format output of ``code_index mode='fetch'``.

The hashline format (``LINE#HASH:content``) is the protocol that
``edit_file`` consumes — the index tool's fetch output should be
directly editable without a separate ``read_file`` round-trip. These
tests pin the format shape and the round-trip with ``edit_file``.
"""

from __future__ import annotations

import re

import pytest

from agent_cli.tools.code_index import _dispatch_one
from agent_cli.tools.edit_file import tool_edit_file


def _write(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


@pytest.fixture
def project(tmp_path, monkeypatch):
    _write(
        tmp_path / "mod.py",
        "def greet(name):\n    return f'hi {name}'\n",
    )
    monkeypatch.chdir(tmp_path)
    return tmp_path


_HASHLINE_RE = re.compile(r"^(\d+)#([A-Z]{2}):")


class TestHashlineFormat:
    def test_fetch_body_lines_have_hashline_prefix(self, project):
        r = _dispatch_one({"mode": "fetch", "path": "mod.py", "name": "greet"})
        assert r.success
        # First output line is the header (no hashline); subsequent
        # lines are the body with `LINE#HH:` prefix.
        body_lines = r.output.splitlines()[1:]
        assert body_lines, "expected at least one body line"
        for line in body_lines:
            assert _HASHLINE_RE.match(line), (
                f"body line missing hashline prefix: {line!r}"
            )

    def test_fetch_header_carries_symbol_metadata(self, project):
        r = _dispatch_one({"mode": "fetch", "path": "mod.py", "name": "greet"})
        assert r.success
        header = r.output.splitlines()[0]
        assert "greet" in header
        assert "function" in header
        assert ":1-2" in header  # 1-indexed inclusive line range


class TestEditFileRoundTrip:
    """``code_index fetch`` → ``edit_file`` chained call should work
    without a separate ``read_file`` between them."""

    def test_fetch_then_edit_replaces_line(self, project):
        # 1) Fetch the function body — hashline-formatted.
        r_fetch = _dispatch_one({"mode": "fetch", "path": "mod.py", "name": "greet"})
        assert r_fetch.success
        body_lines = r_fetch.output.splitlines()[1:]
        # Pick the line containing the return; pull its hashline ref.
        return_line = next(line for line in body_lines if "return" in line)
        m = _HASHLINE_RE.match(return_line)
        assert m
        ref = f"{m.group(1)}#{m.group(2)}"

        # 2) Use edit_file to replace that exact line. edit_file is
        # flat-native — one op = one edit ({path, op, pos, lines}).
        r_edit = tool_edit_file(
            {
                "path": "mod.py",
                "op": "replace",
                "pos": ref,
                "lines": ["    return name.upper()"],
            }
        )
        assert r_edit.success, r_edit.error

        # 3) Confirm the file was modified.
        text = (project / "mod.py").read_text()
        assert "return name.upper()" in text
        assert "f'hi {name}'" not in text


class TestHashlineForOnDemandFetch:
    """The same hashline format applies to out-of-root fetches via the
    on-demand parse path."""

    def test_out_of_root_fetch_uses_hashlines(self, tmp_path, monkeypatch):
        proj = tmp_path / "proj"
        proj.mkdir()
        monkeypatch.chdir(proj)
        outside = tmp_path / "outside"
        outside.mkdir()
        _write(outside / "lib.py", "def util():\n    return 7\n")
        r = _dispatch_one(
            {"mode": "fetch", "path": str(outside / "lib.py"), "name": "util"}
        )
        assert r.success
        body_lines = r.output.splitlines()[1:]
        assert body_lines
        for line in body_lines:
            assert _HASHLINE_RE.match(line)
