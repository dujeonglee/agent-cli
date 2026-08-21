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


class TestOverviewFlatRender:
    """개요 = 순수 플랫 로그(v8.15.0): 각 항목(사용자 입력/응답)을 도착 순서대로 독립
    렌더 — **그룹핑도 귀속(↳)도 없다**(짝짓기 안 함). 여기서 REAL 렌더 함수를 실행해
    '짝지어 보이는' 요소가 없음을 검증한다."""

    def test_user_line_is_standalone_no_group_box(self):
        out = _ov_render_html(
            "ovUserHtml", {"who": "Grumpy Walrus", "text": "하이하이", "tm": "1:36"}
        )
        assert 'class="ov-umsg"' in out
        assert "Grumpy Walrus" in out and "하이하이" in out
        assert "1:36" in out
        # 그룹핑/짝짓기 흔적이 없어야 한다.
        assert "이 응답의 요청" not in out
        assert "ov-qb" not in out and "ov-q" not in out.replace("ov-umsg", "")

    def test_user_line_agent_wake_shown_without_person_icon(self):
        # 🤝 agent wake 도 그대로 보여주되(사용자가 원함) 사람(👤) 아이콘은 안 붙인다.
        out = _ov_render_html(
            "ovUserHtml", {"who": "🤝 agent", "text": "New agent mail", "tm": ""}
        )
        assert "🤝 agent" in out and "New agent mail" in out
        assert "👤" not in out

    def test_user_line_target_badge_in_agent_channel(self):
        # agent 채널의 발신 메시지엔 대상 배지(→ 🤝 agent). 없으면 미출력.
        out = _ov_render_html(
            "ovUserHtml",
            {"who": "소연", "text": "안녕", "target": "🤝 pudding", "tm": ""},
        )
        assert 'class="ov-umsg-to"' in out and "→ 🤝 pudding" in out
        plain = _ov_render_html("ovUserHtml", {"who": "소연", "text": "안녕", "tm": ""})
        assert "ov-umsg-to" not in plain

    def test_response_block_has_no_attribution_or_queries(self):
        out = _ov_render_html(
            "ovRespHtml",
            {"text": "answer", "status": "done", "answers": ["Grumpy Walrus"]},
            is_hero=True,
        )
        assert 'class="ov-block resp' in out and "hero" in out
        assert "answer" in out
        # 짝지어 보이게 하던 ↳ 귀속·요청 박스가 없어야 한다.
        assert "↳" not in out
        assert "이 응답의 요청" not in out

    def test_response_block_always_done_with_actions(self):
        # complete 시 한 번에 append → resp 는 항상 done(라이브 타이핑 없음), 액션 포함.
        out = _ov_render_html("ovRespHtml", {"text": "answer", "reasoning": ""})
        assert "ov-caret" not in out  # 라이브 타이핑 caret 없음
        assert "복사" in out and "전체 대화" in out

    def test_response_source_badge_main(self):
        # 회신 주체 배지: 메인 LLM 은 "main"(muted).
        out = _ov_render_html("ovRespHtml", {"text": "a", "source": "main"})
        assert 'class="ov-src ov-src-main"' in out
        assert ">main<" in out

    def test_response_source_badge_agent(self):
        # agent 응답은 서버가 준 표시명(🤝 pudding)을 accent 배지로.
        out = _ov_render_html("ovRespHtml", {"text": "a", "source": "🤝 pudding"})
        assert 'class="ov-src ov-src-agent"' in out
        assert "🤝 pudding" in out
        assert "ov-src-main" not in out

    def test_response_source_badge_absent_when_no_source(self):
        # source 없으면 배지 미출력(과거 엔트리·회귀 안전).
        out = _ov_render_html("ovRespHtml", {"text": "a"})
        assert "ov-src" not in out


def _ov_act_html(act):
    """Run app.js's REAL ovActHtml with an injected ovAct → activity-strip HTML.
    (진행 중 도구 호출 축약 스트립: 누적 카운트 + 현재 배치 칩.)"""
    esc = _extract_fn("escapeHtml")
    fn = _extract_fn("ovActHtml")
    harness = (
        esc + "\n" + "var ovAct = " + json.dumps(act) + ";\n" + fn + "\n"
        "process.stdout.write(ovActHtml());\n"
    )
    result = subprocess.run(
        ["node", "-e", harness], capture_output=True, text=True, timeout=10, check=False
    )
    if result.returncode != 0:
        raise AssertionError(f"node failed: {result.stderr.strip()}")
    return result.stdout


class TestOverviewActivityStrip:
    """실행 중 = 진행 내용(원시 스트림) 대신 도구 호출을 축약한 활동 스트립
    (누적 카운트 + 현재 배치 칩). complete 시 사라지고 응답 블록으로 대체된다."""

    def test_strip_shows_total_and_current_batch_chips(self):
        out = _ov_act_html(
            {
                "total": 3,
                "turn": 2,
                "batch": [
                    {"key": "a", "icon": "✏️", "label": "index.html", "n": 2},
                    {"key": "b", "icon": "📖", "label": "game.js", "n": 1},
                ],
            }
        )
        assert "ov-act-strip" in out
        assert "도구 3회" in out  # 누적 카운트
        assert "index.html" in out and "×2" in out  # 배치 칩 + 중복 집계
        assert "game.js" in out

    def test_strip_empty_when_no_activity(self):
        # ovAct null → 스트립 미표시(전체 로직은 ovRender 가 판단; 여기선 null 가드).
        assert _ov_act_html(None) == ""


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
