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
    # helpers) and cut at ITS closer. Sibling IIFEs (Prompt Inspector 등)는
    # 로드 시점에 DOM 을 건드리므로 하네스에 딸려 오면 안 된다.
    #
    # 닫는 위치는 **열 0 의** ``})();`` 로 찾는다 (v9.4.0 ②). 종전엔 첫
    # ``})();`` 를 그냥 찾았는데, 그게 실제로는 app.js 주석 안의 문자열
    # ("first })(); = main closer" 라고 적힌 그 주석!)에 걸려 있었다 —
    # 줄 주석이라 뒤가 통째로 삼켜져 우연히 파싱이 됐을 뿐이다. 그 주석을
    # 지우자 첫 매치가 **중첩 IIFE 의 닫는 줄**로 옮겨가 본문이 불균형해졌다.
    # 메인 IIFE 의 닫기는 열 0, 중첩은 들여쓰기이므로 이 구분이 견고하다.
    m = re.search(r"\(function \(\) \{\s*(?:\"use strict\";)?\s*", src)
    assert m, "could not find IIFE opener in app.js"
    start = m.end()
    end = src.find("\n})();", start)
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


def _ov_render_html(name, entry, *, is_hero=False):
    """Run app.js's REAL ovUserHtml/ovRespHtml (pure string builders) standalone.
    They only close over escapeHtml/escapeAndFormat, which we extract alongside —
    exercising the same source the browser runs. The flat log has NO grouping
    function anymore (no pairing), so we test the per-entry renderers directly."""
    deps = _extract_fn("escapeHtml") + "\n"
    if name == "ovRespHtml":
        # ovRespHtml uses the markdown pipeline; pull the whole chain.
        for dep in (
            "renderTables",
            "renderHeadings",
            "renderLists",
            "renderEmphasis",
            "markdownInline",
            "extractCodeFences",
            "restoreCodeFences",
            "escapeAndFormat",
        ):
            deps += _extract_fn(dep) + "\n"
    fn = _extract_fn(name)
    call = (
        name
        + "("
        + json.dumps(entry)
        + ((", " + ("true" if is_hero else "false")) if name == "ovRespHtml" else "")
        + ")"
    )
    harness = deps + fn + "\n" + "process.stdout.write(" + call + ");\n"
    result = subprocess.run(
        ["node", "-e", harness], capture_output=True, text=True, timeout=10, check=False
    )
    if result.returncode != 0:
        raise AssertionError(f"node failed: {result.stderr.strip()}")
    return result.stdout


class TestAgentIconParity:
    """에이전트별 결정적 아이콘 — JS(ovAgentIcon)와 Python(agent_icon)이 같은
    key 에 같은 아이콘을 내야 한다(서버 스윔레인·주체 배지 ↔ 웹 개요 채널 일치).
    풀/해시가 어긋나면 같은 agent 가 두 아이콘으로 보인다."""

    def test_js_python_icon_parity(self):
        import re

        from agent_cli.agent_icon import agent_icon

        src = _APP_JS.read_text(encoding="utf-8")
        pool = re.search(r"var OV_AGENT_ICONS = (\[[\s\S]*?\]);", src).group(1)
        fn = re.search(r"(function ovAgentIcon\(key\) \{[\s\S]*?\n  \})", src).group(1)
        keys = [
            "agt-c83d4f82",
            "agt-9859a1e1",
            "agt-ba9813fa",
            "x",
            "agt-deadbeef",
            "agt-00000000",
            "",
            "agent-writer#3",
        ]
        harness = (
            f"var OV_AGENT_ICONS = {pool};\n{fn}\n"
            + "const ks="
            + json.dumps(keys)
            + ";\n"
            + "process.stdout.write(ks.map(k=>ovAgentIcon(k)).join('\\n'));"
        )
        out = subprocess.run(
            ["node", "-e", harness],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        ).stdout.splitlines()
        assert len(out) == len(keys)
        for key, js_icon in zip(keys, out):
            assert agent_icon(key) == js_icon, (
                f"{key}: py={agent_icon(key)} js={js_icon}"
            )
