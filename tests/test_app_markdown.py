"""Optional Node-driven tests for the web front's pure helpers — node **imports the
real modules** (v10.117.0, docs/web-modules/DESIGN.md §8.6).

These tests run only when a working ``node`` binary is on ``PATH``.

- ``js/markdown.js`` 는 순수 모듈이다(import 없음, top-level 에서 DOM·window 를
  건드리지 않음). 하니스가 DOM 대역 없이 그 파일을 그대로 import 한다 — 대역이
  필요해지면 import 시점에 브라우저 전역을 건드린다는 뜻이라 테스트가 깨진다.
- ``obsSummary``(cards.js)·에이전트 아이콘(channels.js)은 순환 묶음 안에 있어
  메인 화면 모듈 그래프 전체가 딸려 온다. 하니스는 DOM 대역(``_DOM_STUB``)을 먼저
  평가하고 메인 화면 모듈을 **app.js 순서 그대로** import 한다 — 같은 순서의
  평가가 TDZ 없이 끝나는 것 자체가 ``test_main_screen_graph_evaluates`` 의 단정이다.

v10.116.0 까지는 ``main.js`` 소스를 긁어 함수로 감싸거나(``(function(){…})()``),
``js_fn_body`` 로 함수 하나씩 떼어 이어 붙였다. 이제 긁기는 없다 — 브라우저와 같은
파일을 같은 ES 모듈 의미론으로 실행한다.

If ``node`` is missing (clean dev box, CI without Node), the whole
module is skipped — the markdown contract is then validated via the
manual checklist in ``docs/web-fixes-3/TEST_PLAN.md`` §1.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

from tests.web_static import STATIC, module_order

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None,
    reason="node not installed — markdown smoke tests skipped",
)

# 하니스가 모듈에서 import 하는 이름 — 다른 모듈이 쓰지 않아도 export 되는 것은
# 이것뿐이다(tests/test_web_static_modules.py 의 "쓰이지 않는 export 금지" 가 센다).
HARNESS_IMPORTS = {
    "markdown": (
        "escapeAndFormat",
        "extractCodeFences",
        "renderHeadings",
    ),
    "cards": ("obsSummary",),
    "share": ("usageLine", "usageTooltip"),
    "channels": (
        "ovAgentIcon",
        "ovAgentNickname",
        "OV_AGENT_ICONS",
        "OV_AGENT_ANIMALS",
    ),
}

# 브라우저 전역 대역 — 메인 화면 모듈의 top-level 코드(DOM 참조·리스너 등록·
# 관찰자·EventSource·첫 fetch)가 node 에서 돌 만큼만. 하니스의 첫 import 로
# 평가되므로 다른 모듈보다 먼저 전역이 선다.
_DOM_STUB = """\
function _stubEl() {
  return new Proxy({}, {
    get: function (t, k) {
      if (k === "classList") return { add: function () {}, remove: function () {}, toggle: function () {} };
      if (k === "addEventListener") return function () {};
      if (k === "appendChild") return function () {};
      if (k === "insertBefore") return function () {};
      if (k === "removeChild") return function () {};
      if (k === "parentNode") return _stubEl();
      if (k === "querySelector") return function () { return null; };
      if (k === "querySelectorAll") return function () { return []; };
      if (k === "remove") return function () {};
      if (k === "style") return {};
      // v9.26.0 따라가기: #messages[data-pinned] 상태 노출 + 버튼 배치
      if (k === "dataset") return t.__ds || (t.__ds = {});
      if (k === "getBoundingClientRect") return function () { return { left: 0, top: 0, width: 0, height: 0, bottom: 0 }; };
      return t[k];
    },
    set: function (t, k, v) { t[k] = v; return true; },
  });
}
globalThis.window = {
  location: { search: "?token=t", pathname: "/", hash: "", reload: function () {} },
  addEventListener: function () {},
};
// boot.js 가 주소창의 ?token= 을 history.replaceState 로 걷는다.
globalThis.history = { replaceState: function () {} };
globalThis.document = {
  getElementById: function () { return _stubEl(); },
  createElement: function () { return _stubEl(); },
  body: _stubEl(),
  // v10.119.0: channels.js 가 `agentcli:jump`(📋 띠의 점프)를 document 에서 듣는다.
  addEventListener: function () {},
};
globalThis.EventSource = function () { return _stubEl(); };
// v9.26.0 따라가기가 로드 시 붙이는 관찰자 — 여기선 아무것도 안 본다.
globalThis.MutationObserver = function () { return { observe: function () {} }; };
globalThis.ResizeObserver = function () { return { observe: function () {} }; };
globalThis.fetch = function () { return Promise.resolve({}); };
"""


def _module_url(name: str) -> str:
    return (STATIC / "js" / f"{name}.js").as_uri()


def _main_screen() -> list[str]:
    """app.js 적재 순서 중 메인 화면 모듈 — 기능 모듈(``directives`` 부터)의 앞."""
    order = module_order()
    return order[: order.index("directives")]


def _harness_import(module: str, *names: str) -> str:
    assert set(names) <= set(HARNESS_IMPORTS[module]), (module, names)
    return "import { " + ", ".join(names) + ' } from "' + _module_url(module) + '";\n'


def _run_node_harness(source: str, *, dom_stub: bool = False) -> str:
    """ES 모듈 소스 ``source`` 를 node 로 실행해 stdout 을 돌려준다.

    ``dom_stub`` 이면 브라우저 전역 대역을 먼저 평가하고 메인 화면 모듈을 app.js
    순서로 import 한 뒤 ``source`` 를 잇는다(정적 import 는 적힌 순서로 평가된다).
    """
    with tempfile.TemporaryDirectory() as tmp:
        if dom_stub:
            (Path(tmp) / "stub.mjs").write_text(_DOM_STUB, encoding="utf-8")
            source = (
                'import "./stub.mjs";\n'
                + "".join(f'import "{_module_url(m)}";\n' for m in _main_screen())
                + source
            )
        # 파일로 넘긴다 — ``node -e <script>`` 는 리눅스의 인자 **1개** 상한
        # (MAX_ARG_STRLEN = 32페이지 = 128 KiB)에 걸린다. app.js 가 커지다
        # 128,642 바이트에서 벼랑 끝에 서 있었고, 1 KB 를 더한 커밋이 CI 를
        # 통째로 빨갛게 만들었다(macOS 엔 그 상한이 없어 로컬은 초록 — "로컬
        # 통과는 증거가 아니다" 의 교과서적 사례). 파일에는 상한이 없다.
        # ``.mjs`` 라 node 가 ES 모듈로 읽는다.
        script = str(Path(tmp) / "harness.mjs")
        Path(script).write_text(source, encoding="utf-8")
        result = subprocess.run(
            ["node", script],
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


def _write_out(expr: str) -> str:
    return (
        f"const out = {expr};\n"
        "process.stdout.write(typeof out === 'string' ? out : JSON.stringify(out));\n"
    )


def _markdown(name: str, call: str, input_value: str) -> str:
    """markdown.js 의 ``name`` 을 DOM 대역 **없이** import 해 ``call`` 을 평가한다."""
    return _run_node_harness(
        _harness_import("markdown", name)
        + "const input = "
        + json.dumps(input_value)
        + ";\n"
        + _write_out(call)
    )


def _format(text: str) -> str:
    return _markdown("escapeAndFormat", "escapeAndFormat(input)", text)


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

    def test_stars_with_spaces_are_not_emphasis(self):
        """v9.22.3 CommonMark flanking: 공백을 사이에 둔 별표 쌍은 강조가
        아니다 — cron 식·곱셈이 사용자 말풍선에서 사라지던 자리."""
        for text in ("0 9 * * 1-5", "*/5 * * * *", "2 * 3 * 4", "a ** b ** c"):
            out = _format(text)
            assert "<em>" not in out and "<strong>" not in out, (text, out)
            assert out.count("*") == text.count("*"), (text, out)
        # 진짜 강조는 그대로
        assert "<em>단어</em>" in _format("앞 *단어* 뒤")
        assert "<strong>굵게</strong>" in _format("앞 **굵게** 뒤")

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
        out = _markdown(
            "extractCodeFences",
            "extractCodeFences(input).stripped",
            "before\n```\ninside\n```\nafter",
        )
        # The fence is replaced by an HTML comment placeholder.
        assert "<!--cf:" in out
        assert "inside" not in out
        assert "before" in out and "after" in out

    def test_render_headings_only_h1_h3(self):
        out = _markdown(
            "renderHeadings",
            "renderHeadings(input)",
            "# a\n## b\n### c\n#### d\n##### e",
        )
        assert "<h1>a</h1>" in out
        assert "<h2>b</h2>" in out
        assert "<h3>c</h3>" in out
        assert "<h4>" not in out
        assert "<h5>" not in out
        # ``####`` row stays raw.
        assert "#### d" in out


def test_markdown_module_is_pure():
    """markdown.js 는 import 가 없다 — node 가 DOM 대역 없이 import 하는 전제.
    (실행 쪽 증거는 위 테스트들: 대역 없이 import 해 돌린다.)"""
    src = (STATIC / "js" / "markdown.js").read_text(encoding="utf-8")
    assert not re.search(r"^import\b", src, re.MULTILINE)


def test_main_screen_graph_evaluates():
    """메인 화면 모듈 그래프가 app.js 순서로 TDZ 없이 평가된다 (DESIGN §8.4).

    모듈끼리 함수 호출로 순환하고, 순환에서는 처음 import 된 모듈이 마지막에
    평가된다. top-level 에서 순환 안 함수를 부르는 채널 바 IIFE(channels.js)가
    다른 모듈의 const(frames 등)를 읽으므로, 진입점이 바뀌어 channels 가 먼저
    평가되면 ``ReferenceError: Cannot access 'frames' before initialization`` 이
    난다 — 브라우저에선 화면이 통째로 안 뜬다."""
    out = _run_node_harness("process.stdout.write('ok');\n", dom_stub=True)
    assert out == "ok"


_PARITY_KEYS = [
    "agt-c83d4f82",
    "agt-9859a1e1",
    "agt-ba9813fa",
    "x",
    "agt-deadbeef",
    "agt-00000000",
    "",
    "agent-writer#3",
]


def _channels(names: tuple[str, ...], body: str) -> str:
    """channels.js 의 ``names`` 를 메인 화면 그래프째(DOM 대역 위) import 해 ``body`` 를 돌린다."""
    return _run_node_harness(_harness_import("channels", *names) + body, dom_stub=True)


class TestAgentIconParity:
    """에이전트별 결정적 **시각 정체성** — JS 와 Python 이 같은 key 에 같은
    아이콘·같은 별명을 내야 한다(서버 스윔레인·주체 배지·CLI 문답 창 ↔ 웹 개요
    채널 일치). 풀/해시가 어긋나면 같은 agent 가 화면마다 다른 얼굴이 된다.

    별명은 **표시 전용**이고 주소는 언제나 key 다 — 그래서 이 함수들은 순수
    함수(로스터 미참조)이고, 양쪽이 같은 산술만 하면 일치가 보장된다."""

    def test_js_python_icon_parity(self):
        from agent_cli.agent_icon import agent_icon

        out = _channels(
            ("ovAgentIcon",),
            f"const ks = {json.dumps(_PARITY_KEYS)};\n"
            "process.stdout.write(ks.map((k) => ovAgentIcon(k)).join('\\n'));\n",
        ).splitlines()
        assert len(out) == len(_PARITY_KEYS)
        for key, js_icon in zip(_PARITY_KEYS, out):
            assert agent_icon(key) == js_icon, (
                f"{key}: py={agent_icon(key)} js={js_icon}"
            )

    def test_js_python_nickname_parity(self):
        """v9.9.0 — 별명 풀·해시도 양쪽이 같아야 한다."""
        from agent_cli.agent_icon import agent_nickname

        out = _channels(
            ("ovAgentNickname",),
            f"const ks = {json.dumps(_PARITY_KEYS)};\n"
            "process.stdout.write(ks.map((k) => ovAgentNickname(k)).join('\\n'));\n",
        ).splitlines()
        assert len(out) == len(_PARITY_KEYS)
        for key, js_nick in zip(_PARITY_KEYS, out):
            assert agent_nickname(key) == js_nick, (
                f"{key}: py={agent_nickname(key)} js={js_nick}"
            )

    def test_animal_pool_is_paired_with_the_icon_pool(self):
        """ "🦊 날쌘 여우" 가 어긋나지 않으려면 같은 인덱스여야 한다 — 길이
        불일치는 조용히 짝을 깨므로(아이콘만 늘리면 % 가 달라진다) 고정한다."""
        from agent_cli.agent_icon import AGENT_ANIMALS, AGENT_ICONS

        assert len(AGENT_ICONS) == len(AGENT_ANIMALS)
        pools = json.loads(
            _channels(
                ("OV_AGENT_ICONS", "OV_AGENT_ANIMALS"),
                "process.stdout.write(JSON.stringify("
                "{ OV_AGENT_ICONS, OV_AGENT_ANIMALS }));\n",
            )
        )
        for py_pool, js_name in (
            (AGENT_ICONS, "OV_AGENT_ICONS"),
            (AGENT_ANIMALS, "OV_AGENT_ANIMALS"),
        ):
            assert pools[js_name] == py_pool, f"{js_name}: 풀이 서버와 다르다"


def test_harness_does_not_pass_the_script_as_an_argv_string():
    """``node -e <script>`` 를 쓰면 안 된다 — 리눅스 인자 1개 상한(128 KiB).

    이 가드는 **플랫폼 독립**이어야 한다. 실제 실행 테스트는 macOS 에서
    상한이 없어 전부 초록이고 리눅스 CI 에서만 빨갛다 — app.js 가 커지다
    128,642 바이트에서 벼랑에 걸렸고, 1 KB 를 더한 커밋이 CI 를 통째로
    무너뜨렸다. 소스를 직접 보는 것만이 어느 OS 에서든 잡는다.

    ``node`` 유무와도 무관해야 하므로 모듈의 skipif 를 타지 않게 별도
    함수로 둔다(모듈 skipif 는 여전히 적용되지만, 이 검사는 실행이 아니라
    소스만 본다 — node 없는 개발기에서 건너뛰어도 CI 가 잡는다).
    """
    import inspect

    src = inspect.getsource(_run_node_harness)
    assert '["node", "-e"' not in src, "하네스가 스크립트를 argv 로 넘긴다"
    assert '["node", script]' in src, "하네스가 파일 실행이 아니다"


class TestObsSummaryAbsorbedRequest:
    """런 도중 흡수된 요청 관찰의 한 줄 요약 (v9.26.1).

    "성공은 마지막 줄" 규칙이 이 관찰에선 꼬리 상용구 `(You now owe …)` 만
    보여줘, 2an3cv 에서 33건이 전부 같은 한 줄로 보였다 — 무엇을 요청받았는지
    카드를 펼치기 전엔 알 수 없었다."""

    ABSORBED = (
        "Observation: ── request [6] from agent:agt-6700099c "
        "(arrived while you were working) ──\n"
        "FREEZE AMENDMENT — main has ruled on the contract shape.\n"
        "Second paragraph that must not be picked.\n"
        "(You now owe agent:agt-6700099c a reply for [6] as well — "
        'reply(id="6", text="...") when you have it. Your current task stays '
        "open; see ## Owed Replies.)"
    )

    def _sum(self, text, failed=False):
        return _run_node_harness(
            _harness_import("cards", "obsSummary")
            + f"const input = {json.dumps(text)};\n"
            + _write_out(f"obsSummary(input, {'true' if failed else 'false'})"),
            dom_stub=True,
        )

    def test_shows_who_and_the_first_line_of_the_request(self):
        assert (
            self._sum(self.ABSORBED)
            == "[6] agent:agt-6700099c: FREEZE AMENDMENT — main has ruled on the contract shape."
        )

    def test_human_request_from_main(self):
        text = (
            "── request [22] from main (arrived while you were working) ──\n"
            "[ORCH DIRECTIVE — pixel byte-order fix]\n"
            '(You now owe main a reply for [22] as well — reply(id="22", text="...").)'
        )
        assert self._sum(text) == "[22] main: [ORCH DIRECTIVE — pixel byte-order fix]"

    def test_other_observations_fold_to_their_first_line(self):
        # v10.74.1: 성공도 첫 줄 — 접힌 줄은 사람이 보는 것(마지막 줄은 read_file 의 `}`,
        # `/sh ls` 의 마지막 항목 같은 임의의 줄이었다). `Observation:` 접두는 뗀다.
        assert self._sum("Observation: first\nlast line") == "first"
        assert (
            self._sum("Wrote 3 lines to a.py\n1#ab:x = 1\n3#cd:}")
            == "Wrote 3 lines to a.py"
        )
        assert self._sum("🔔 monitor m-1 fired\n  ↳ retired") == "🔔 monitor m-1 fired"
        assert self._sum("\n\n") == "(no output)"

    def test_failed_path_is_untouched(self):
        assert self._sum("Observation: boom", failed=True) == "boom"


class TestImages:
    """``![alt](src)`` (v10.64.0) — 그림은 그려지는 순간 요청을 내므로 출처를 좁힌다:
    작업 폴더 경로와 래스터 ``data:`` 만 ``<img>``, 외부 URL 은 링크, 나머지는 글."""

    PNG = "data:image/png;base64,iVBORw0KGgo="

    def test_workspace_path_goes_through_the_image_endpoint(self):
        out = _format("see ![chart](out/a&b.png)")
        # ``&t=<그린 시각>`` (v10.100.1 ``v`` → v10.101.0 ``t``) — 덮어쓴 그림이 메모리 캐시의 옛 그림으로
        # 나오지 않게 그릴 때마다 주소가 다르다.
        m = re.search(
            r'src="api/workspace/file\?path=out%2Fa%26b\.png&amp;t=([0-9a-z]+)"', out
        )
        assert m, out
        assert 'alt="chart"' in out and 'target="_blank"' in out
        # 링크(원본 보기)와 그림이 같은 주소다.
        assert (
            out.count(
                f'href="api/workspace/file?path=out%2Fa%26b.png&amp;t={m.group(1)}"'
            )
            == 1
        )

    def test_raster_data_uri_is_inlined(self):
        out = _format(f"![dot]({self.PNG})")
        assert f'<img class="md-img" src="{self.PNG}" alt="dot">' in out

    def test_svg_data_uri_stays_text(self):
        out = _format("![x](data:image/svg+xml;base64,PHN2Zz4=)")
        assert "<img" not in out and "![x](data:image/svg+xml" in out

    def test_external_url_is_a_link_not_an_image(self):
        out = _format("![leak](https://evil.example/p.png?d=secret)")
        assert "<img" not in out
        assert 'href="https://evil.example/p.png?d=secret"' in out
        assert 'rel="noopener noreferrer"' in out and "🖼 leak" in out

    def test_other_schemes_and_protocol_relative_stay_text(self):
        for src in ("javascript:alert(1)", "//evil.example/x.png", "file:///etc/x.png"):
            out = _format(f"![x]({src})")
            assert "<img" not in out and "<a" not in out, src

    def test_quotes_cannot_break_out_of_the_attribute(self):
        out = _format('![a" onerror="x](p.png)')
        assert 'onerror="x"' not in out and "&quot;" in out

    def test_inline_code_and_fences_are_left_alone(self):
        assert "<img" not in _format("`![x](p.png)`")
        assert "<img" not in _format("```\n![x](p.png)\n```")


def _usage_js(fn: str, payload: dict) -> str:
    """share.js 의 상단 사용량 칩 글/툴팁 — 메인 화면 그래프째(DOM 대역 위) import."""
    return _run_node_harness(
        _harness_import("share", fn)
        + "const input = "
        + json.dumps(payload)
        + ";\n"
        + _write_out(f"{fn}(input)"),
        dom_stub=True,
    )


_USAGE_FULL = {
    "in": 41_000,
    "out": 812,
    "context_window": 128_000,
    "out_speed": 38.4,
    "in_speed": 1520.2,
    "ttft_ms": 830.6,
    "total_in": 1_430_000,
    "total_cached": 1_100_000,
    "total_out": 9_100,
    "calls": 23,
}


class TestUsageTopBar:
    """웹 상단 사용량 칩 (v10.136.0) — 출력 속도·Σ↑(루프 입력 합) 추가, 0/없는 조각 생략."""

    FULL = _USAGE_FULL

    def test_line_with_speed(self):
        out = _usage_js("usageLine", self.FULL)
        assert out == "ctx 41.0K/128.0K (32%) · ↓812 · 38 tok/s · Σ↑1.4M · Σ↓9.1K"

    def test_line_without_times_drops_speed(self):
        d = {
            k: v
            for k, v in self.FULL.items()
            if k not in ("out_speed", "in_speed", "ttft_ms")
        }
        out = _usage_js("usageLine", d)
        assert out == "ctx 41.0K/128.0K (32%) · ↓812 · Σ↑1.4M · Σ↓9.1K"

    def test_line_old_payload_has_no_sums(self):
        # 이전 판이 남긴 스냅샷(resume) — 누적 키가 없으면 그 조각은 없다
        out = _usage_js("usageLine", {"in": 5200, "out": 320, "context_window": 262144})
        assert out == "ctx 5.2K/262.1K (2%) · ↓320"

    def test_tooltip_full(self):
        out = _usage_js("usageTooltip", self.FULL)
        assert out.split("\n") == [
            "context 41.0K / 128.0K",
            "turn out 812 (38 tok/s)",
            "ttft 831 ms",
            "prefill 1520 tok/s",
            "run in 1.4M (cached 1.1M) · out 9.1K · 23 calls",
        ]

    def test_tooltip_without_cache_or_times(self):
        d = {
            "in": 900,
            "out": 5,
            "context_window": 4096,
            "total_in": 900,
            "total_out": 5,
            "calls": 1,
        }
        out = _usage_js("usageTooltip", d)
        assert out.split("\n") == [
            "context 900 / 4.1K",
            "turn out 5",
            "run in 900 · out 5 · 1 call",
        ]
