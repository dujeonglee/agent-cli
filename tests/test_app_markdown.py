"""Optional Node-driven smoke tests for the markdown helpers in
``agent_cli/web/static/app.js``.

These tests run only when a working ``node`` binary is on ``PATH``.
They extract the markdown helper functions from ``app.js`` and
evaluate them in a Node VM — that way we exercise the same source
the browser does, without duplicating the regex logic in Python (the
"dual source of truth" trap the design called out).

Each test ships a small JS harness that requires the function under
test, runs it on a known input, and prints the result. The Python
side captures stdout and asserts on the rendered HTML.

If ``node`` is missing (clean dev box, CI without Node), the whole
module is skipped — the markdown contract is then validated via the
manual checklist in ``docs/web-fixes-3/TEST_PLAN.md`` §1.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

_APP_JS = (
    Path(__file__).resolve().parent.parent / "agent_cli" / "web" / "static" / "app.js"
)

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None,
    reason="node not installed — markdown smoke tests skipped",
)


def _extract_iife_body() -> str:
    """Return the body of app.js's IIFE so a Node harness can run the
    helper functions defined inside. Strips the opening
    ``(function () { "use strict";`` and the trailing ``})();`` so the
    body can be wrapped in a different shell that exposes the helpers
    for testing.
    """
    src = _APP_JS.read_text(encoding="utf-8")
    # Match the FIRST IIFE (the chat client, which owns the markdown
    # helpers) and cut at ITS closer — the first ``})();`` after the
    # opener. app.js now contains additional sibling IIFEs (e.g. the
    # Prompt Inspector) that touch the DOM at load time; taking the
    # last closer would pull them into the Node harness, which has no
    # ``document``.
    m = re.search(r"\(function \(\) \{\s*(?:\"use strict\";)?\s*", src)
    assert m, "could not find IIFE opener in app.js"
    start = m.end()
    end = src.find("})();", start)
    assert end > start, "could not find IIFE closer in app.js"
    return src[start:end]


def _run_node_harness(call_expr: str, input_value: str) -> str:
    """Evaluate ``call_expr(input_value)`` in Node and return stdout.

    The IIFE body is wrapped in a function that stops short of the SSE
    setup (which expects ``window`` / ``document``) by short-circuiting
    on the first ``document.getElementById`` lookup. We only need the
    pure-string helpers defined near the top of the file.
    """
    body = _extract_iife_body()
    # Stop the IIFE from touching browser-only globals. Replace the
    # DOM ref block with stubs that throw on access; the helper
    # functions we test don't touch them. The harness then exposes
    # the named helper via ``globalThis``.
    stub = (
        "var window = { location: { search: '?token=t', pathname: '/', hash: '', "
        "reload: function(){} },\n"
        "  addEventListener: function(){} };\n"
        # app.js's bootstrap strips ?token= from the URL via history.replaceState.
        "var history = { replaceState: function(){} };\n"
        "function _stubEl(){ return new Proxy({}, {\n"
        "  get: function(t, k){\n"
        "    if (k === 'classList') return { add: function(){}, remove: function(){}, "
        "toggle: function(){} };\n"
        "    if (k === 'addEventListener') return function(){};\n"
        "    if (k === 'appendChild') return function(){};\n"
        "    if (k === 'insertBefore') return function(){};\n"
        "    if (k === 'removeChild') return function(){};\n"
        "    if (k === 'parentNode') return _stubEl();\n"
        "    if (k === 'querySelector') return function(){ return null; };\n"
        "    if (k === 'querySelectorAll') return function(){ return []; };\n"
        "    if (k === 'remove') return function(){};\n"
        "    if (k === 'style') return {};\n"
        "    return t[k];\n"
        "  },\n"
        "  set: function(t, k, v){ t[k] = v; return true; }\n"
        "}); }\n"
        "var document = { getElementById: function(){ return _stubEl(); },\n"
        "  createElement: function(){ return _stubEl(); },\n"
        "  body: _stubEl() };\n"
        # Native URLSearchParams (app.js uses .has/.delete/.toString for the
        # bootstrap-token strip, not just .get).
        "var URLSearchParams = globalThis.URLSearchParams;\n"
        "var EventSource = function(){ return _stubEl(); };\n"
        "var fetch = function(){ return Promise.resolve({}); };\n"
    )
    expose = (
        "\nglobalThis.__escapeAndFormat = escapeAndFormat;\n"
        "globalThis.__extractCodeFences = extractCodeFences;\n"
        "globalThis.__restoreCodeFences = restoreCodeFences;\n"
        "globalThis.__renderHeadings = renderHeadings;\n"
        "globalThis.__renderTables = renderTables;\n"
        "globalThis.__renderLists = renderLists;\n"
        "globalThis.__renderEmphasis = renderEmphasis;\n"
        "globalThis.__markdownInline = markdownInline;\n"
    )
    harness = (
        stub
        + "(function(){\n"
        + body
        + expose
        + "})();\n"
        + "const input = "
        + json.dumps(input_value)
        + ";\n"
        + f"const out = {call_expr};\n"
        + "process.stdout.write(typeof out === 'string' ? out : JSON.stringify(out));\n"
    )
    result = subprocess.run(
        ["node", "-e", harness],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    if result.returncode != 0:
        raise AssertionError(
            f"node harness failed: {result.stderr.strip()}\nstdout: {result.stdout!r}"
        )
    return result.stdout


def _format(text: str) -> str:
    return _run_node_harness("globalThis.__escapeAndFormat(input)", text)


class TestEscapeAndFormat:
    """End-to-end pipeline. The composition is what the live
    renderer calls; individual helpers below pin corner cases."""

    def test_heading_levels_1_2_3(self):
        out = _format("# Big\n## Sub\n### Tiny")
        assert "<h1>Big</h1>" in out
        assert "<h2>Sub</h2>" in out
        assert "<h3>Tiny</h3>" in out

    def test_four_hashes_stays_raw(self):
        """Only h1-h3 are recognised; deeper headers stay as text."""
        out = _format("#### NotAHeader")
        assert "<h4>" not in out
        assert "#### NotAHeader" in out

    def test_bold_and_italic(self):
        out = _format("**bold** and *italic*")
        assert "<strong>bold</strong>" in out
        assert "<em>italic</em>" in out

    def test_unordered_list(self):
        out = _format("- one\n- two\n- three")
        assert "<ul>" in out
        assert "<li>one</li>" in out
        assert "<li>two</li>" in out
        assert "<li>three</li>" in out

    def test_ordered_list(self):
        out = _format("1. first\n2. second")
        assert "<ol>" in out
        assert "<li>first</li>" in out
        assert "<li>second</li>" in out

    def test_pipe_table(self):
        out = _format("| Name | Age |\n|------|-----|\n| Bob  | 30  |\n| Eve  | 25  |")
        assert "<table>" in out
        assert "<th>Name</th>" in out
        assert "<th>Age</th>" in out
        assert "<td>Bob</td>" in out
        assert "<td>30</td>" in out
        assert "<td>Eve</td>" in out

    def test_code_fence_preserves_inner_tokens(self):
        """Markdown tokens inside fenced code MUST not be converted —
        ``##`` and ``|`` stay literal inside the ``<pre>`` block."""
        src = "```\n## Inside should stay\n| not | a | table |\n```"
        out = _format(src)
        # The fence is rendered as a <pre><code> block.
        assert "<pre" in out
        # Heading marker stays raw inside the fence.
        assert "## Inside should stay" in out
        # No <h2> conversion happened on that line.
        assert "<h2>" not in out
        # No <table> built from the pipe row.
        assert "<table>" not in out

    def test_code_fence_with_hyphen_lang_tag(self):
        """Hyphenated language tags (``objective-c``, ``f-sharp``,
        ``x-yaml``) are common and the DESIGN-spec regex (``[\\w-]*``)
        accepts them. Without the hyphen class the fence boundary
        would be lost and inner ``##`` would leak into heading
        conversion, breaking M-5 (code fence preservation)."""
        src = "```objective-c\n## inside\n```"
        out = _format(src)
        assert "<pre" in out
        assert "## inside" in out
        assert "<h2>" not in out

    def test_xss_safety_script_stays_escaped(self):
        """Untrusted ``<script>`` must remain HTML-escaped after the
        markdown pipeline runs. Any new transform that revives raw HTML
        from already-escaped text is a vulnerability — this test pins
        the contract.
        """
        out = _format("<script>alert(1)</script>")
        assert "<script>" not in out
        assert "&lt;script&gt;" in out
        assert "alert(1)" in out

    def test_xss_in_heading_payload_stays_escaped(self):
        out = _format("### Header <img onerror=x>")
        # Header IS converted; payload IS escaped.
        assert "<h3>" in out
        assert "<img" not in out
        assert "&lt;img" in out


class TestMarkdownHelpers:
    """Direct invocations to pin behaviour of individual helpers."""

    def test_extract_code_fences_replaces_with_placeholder(self):
        out = _run_node_harness(
            "globalThis.__extractCodeFences(input).stripped",
            "before\n```\ninside\n```\nafter",
        )
        # The fence is replaced by an HTML comment placeholder.
        assert "<!--cf:" in out
        assert "inside" not in out
        assert "before" in out and "after" in out

    def test_render_headings_only_h1_h3(self):
        out = _run_node_harness(
            "globalThis.__renderHeadings(input)",
            "# a\n## b\n### c\n#### d\n##### e",
        )
        assert "<h1>a</h1>" in out
        assert "<h2>b</h2>" in out
        assert "<h3>c</h3>" in out
        assert "<h4>" not in out
        assert "<h5>" not in out
        # ``####`` row stays raw.
        assert "#### d" in out


def _extract_fn(name: str) -> str:
    """Return the source of a top-level ``function <name>(...) { ... }`` from
    app.js by brace-matching. ``ovBuildBlocks`` lives past the first ``})();``
    (a nested IIFE closer), so the whole-IIFE harness can't reach it — but the
    function only closes over ``ovEntries``, so we run it standalone."""
    src = _APP_JS.read_text(encoding="utf-8")
    m = re.search(r"\n  function " + re.escape(name) + r"\(", src)
    assert m, f"could not find function {name} in app.js"
    brace = src.index("{", m.end())
    depth = 0
    for i in range(brace, len(src)):
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
            if depth == 0:
                return src[m.start() : i + 1]
    raise AssertionError(f"unbalanced braces for {name}")


def _ov_blocks(entries):
    """Run app.js's REAL ovBuildBlocks over a flat entry list → derived blocks.
    ovBuildBlocks closes over ``ovEntries`` only, so we inject it and run the
    extracted source standalone in Node (same source the browser executes)."""
    fn = _extract_fn("ovBuildBlocks")
    harness = (
        "var ovEntries = " + json.dumps(entries) + ";\n" + fn + "\n"
        "process.stdout.write(JSON.stringify(ovBuildBlocks()));\n"
    )
    result = subprocess.run(
        ["node", "-e", harness], capture_output=True, text=True, timeout=10, check=False
    )
    if result.returncode != 0:
        raise AssertionError(f"node failed: {result.stderr.strip()}")
    return json.loads(result.stdout)


class TestOverviewFlatGrouping:
    """개요 플랫 로그 모델(v8.15.0): ovEntries(사용자 입력 + resp)를 렌더 시점에
    [연속 user + 뒤따르는 resp] 블록으로 그룹핑. 짝짓기를 저장하지 않으므로 정체·
    오귀속이 없다 — 여기서 REAL ovBuildBlocks 로직을 실행해 검증한다."""

    def test_users_then_response_group_into_one_block(self):
        blocks = _ov_blocks(
            [
                {"kind": "user", "who": "A", "text": "q1", "tm": ""},
                {"kind": "user", "who": "A", "text": "q2", "tm": ""},
                {"kind": "resp", "text": "answer", "status": "done", "answers": []},
            ]
        )
        assert len(blocks) == 1
        assert [q["t"] for q in blocks[0]["queries"]] == ["q1", "q2"]
        assert blocks[0]["text"] == "answer"
        assert blocks[0]["status"] == "done"

    def test_two_runs_do_not_cross_contaminate(self):
        # q1→a1, then q2→a2: each response pairs with only its preceding queries.
        blocks = _ov_blocks(
            [
                {"kind": "user", "who": "A", "text": "q1", "tm": ""},
                {"kind": "resp", "text": "a1", "status": "done", "answers": []},
                {"kind": "user", "who": "A", "text": "q2", "tm": ""},
                {"kind": "resp", "text": "a2", "status": "done", "answers": []},
            ]
        )
        assert len(blocks) == 2
        assert [q["t"] for q in blocks[0]["queries"]] == ["q1"]
        assert blocks[0]["text"] == "a1"
        assert [q["t"] for q in blocks[1]["queries"]] == ["q2"]
        assert blocks[1]["text"] == "a2"

    def test_trailing_users_without_response_are_a_wait_block(self):
        # A request with no response yet → a wait block (never stuck: it just
        # trails until a resp entry arrives). This is the "대기" case that used to
        # get stuck; now it is purely render-derived, not stateful.
        blocks = _ov_blocks(
            [
                {"kind": "resp", "text": "a1", "status": "done", "answers": []},
                {"kind": "user", "who": "A", "text": "q2", "tm": ""},
            ]
        )
        assert len(blocks) == 2
        assert blocks[1]["status"] == "wait"
        assert blocks[1]["text"] == ""
        assert [q["t"] for q in blocks[1]["queries"]] == ["q2"]

    def test_response_with_no_preceding_query(self):
        # A scoped/dispatched complete may land with no immediately-preceding
        # user entry (e.g. its query was consumed by a prior block) — still a
        # valid response block with empty queries, never dropped or stuck.
        blocks = _ov_blocks(
            [{"kind": "resp", "text": "scoped result", "status": "done", "answers": []}]
        )
        assert len(blocks) == 1
        assert blocks[0]["queries"] == []
        assert blocks[0]["text"] == "scoped result"

    def test_empty_log_yields_no_blocks(self):
        assert _ov_blocks([]) == []
