"""Tests for the fetch tool."""

import json
from typing import ClassVar
from unittest.mock import MagicMock, patch

import pytest

from agent_cli.tools.fetch import (
    _PROXY_VARS,
    FetchTool,
    _body_kind,
    _fetch_single,
    _HTMLToMarkdown,
    tool_fetch,
)


@pytest.fixture(autouse=True)
def _no_proxy_env(monkeypatch):
    """v10.118.0: 연결 실패 문구는 환경의 프록시 변수를 사실로 싣는다 — 테스트를
    돌리는 셸의 프록시 설정이 단정에 새지 않게 모두 지운다(필요한 테스트가 다시 넣는다)."""
    for name in (*_PROXY_VARS, "NO_PROXY", "no_proxy"):
        monkeypatch.delenv(name, raising=False)


class TestHTMLToMarkdown:
    def test_basic_text(self):
        parser = _HTMLToMarkdown()
        parser.feed("<p>Hello world</p>")
        assert "Hello world" in parser.get_markdown()

    def test_headings(self):
        parser = _HTMLToMarkdown()
        parser.feed("<h1>Title</h1><h2>Subtitle</h2>")
        md = parser.get_markdown()
        assert "# Title" in md
        assert "## Subtitle" in md

    def test_code_blocks(self):
        parser = _HTMLToMarkdown()
        parser.feed("<pre>code here</pre>")
        assert "```" in parser.get_markdown()

    def test_inline_code(self):
        parser = _HTMLToMarkdown()
        parser.feed("<p>Use <code>print()</code> function</p>")
        assert "`print()`" in parser.get_markdown()

    def test_strips_scripts(self):
        parser = _HTMLToMarkdown()
        parser.feed("<p>visible</p><script>alert('xss')</script><p>also visible</p>")
        md = parser.get_markdown()
        assert "visible" in md
        assert "alert" not in md

    def test_strips_styles(self):
        parser = _HTMLToMarkdown()
        parser.feed("<style>body{color:red}</style><p>content</p>")
        md = parser.get_markdown()
        assert "content" in md
        assert "color" not in md

    def test_lists(self):
        parser = _HTMLToMarkdown()
        parser.feed("<ul><li>one</li><li>two</li></ul>")
        md = parser.get_markdown()
        assert "- one" in md
        assert "- two" in md

    def test_anchor_links_are_text_only(self):
        parser = _HTMLToMarkdown()
        parser.feed('<a href="/page2">link</a><a href="#anchor">skip</a>')
        assert parser.get_markdown() == "[link](/page2)skip"


class TestFetchSingle:
    def test_success(self):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.headers = {"content-type": "text/html"}
        mock_resp.content = b"<h1>Hello</h1><p>World</p>"

        with patch("agent_cli.tools.fetch.requests.get", return_value=mock_resp):
            content, error = _fetch_single("https://example.com")
            assert error is None
            assert "Hello" in content

    def test_404(self):
        mock_resp = MagicMock()
        mock_resp.status_code = 404

        with patch("agent_cli.tools.fetch.requests.get", return_value=mock_resp):
            _, error = _fetch_single("https://example.com/missing")
            assert error.startswith(
                "Fetch failed (http-missing): https://example.com/missing\n"
            )

    def test_timeout(self):
        import requests as req

        with patch(
            "agent_cli.tools.fetch.requests.get",
            side_effect=req.exceptions.Timeout(),
        ):
            _, error = _fetch_single("https://slow.example.com")
            assert error.startswith(
                "Fetch failed (timeout): https://slow.example.com\n"
            )

    def test_connection_error(self):
        import requests as req

        with patch(
            "agent_cli.tools.fetch.requests.get",
            side_effect=req.exceptions.ConnectionError(),
        ):
            _, error = _fetch_single("https://nonexistent.example.com")
            assert error.startswith(
                "Fetch failed (connect): https://nonexistent.example.com\n"
            )

    def test_plain_text(self):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.headers = {"content-type": "text/plain"}
        mock_resp.content = b"Plain text content"

        with patch("agent_cli.tools.fetch.requests.get", return_value=mock_resp):
            content, error = _fetch_single("https://example.com/file.txt")
            assert error is None
            assert "Plain text content" in content


def _resp(ct: str, body, *, apparent: str = "utf-8"):
    r = MagicMock()
    r.status_code = 200
    r.headers = {"content-type": ct}
    r.content = body if isinstance(body, bytes) else body.encode("utf-8")
    r.apparent_encoding = apparent
    return r


def _get(resp, url="https://example.com/x"):
    with patch("agent_cli.tools.fetch.requests.get", return_value=resp):
        return _fetch_single(url)


class TestBodyKinds:
    """v10.52.0: JSON/XML 를 받는다 — 종전엔 html·plain 만 받아 브라우저가 필요 없는
    기계 엔드포인트(PyPI·Wikipedia REST·OSV·GitHub API)가 전부 거절됐다."""

    def test_kinds(self):
        assert _body_kind("text/html; charset=utf-8") == "html"
        assert _body_kind("application/xhtml+xml") == "html"
        assert _body_kind("application/json") == "json"
        assert _body_kind("application/vnd.github+json") == "json"
        assert _body_kind("application/atom+xml; charset=UTF-8") == "text"
        assert _body_kind("text/xml") == "text"
        assert _body_kind("text/markdown") == "text"
        assert _body_kind("application/pdf") is None
        assert _body_kind("image/png") is None

    def test_json_is_pretty_printed_and_keeps_non_ascii(self):
        c, err = _get(_resp("application/json", '{"title":"서울","n":[1,2]}'))
        assert err is None
        assert c == '{\n "title": "서울",\n "n": [\n  1,\n  2\n ]\n}'

    def test_invalid_json_is_handed_over_as_text(self):
        c, err = _get(_resp("application/json", "not json {"))
        assert err is None and c == "not json {"

    def test_xml_is_text(self):
        c, err = _get(_resp("application/atom+xml", "<feed><title>t</title></feed>"))
        assert err is None and c == "<feed><title>t</title></feed>"

    def test_binary_is_downloaded_not_refused(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        c, err = _get(_resp("application/pdf", b"%PDF-1.7\xff\xfe"))
        assert err is None
        assert c.startswith("[Binary content (application/pdf), not shown")
        saved = tmp_path / ".agent-cli/tmp/downloads/x.pdf"
        assert saved.read_bytes() == b"%PDF-1.7\xff\xfe"

    def test_description_names_the_new_kinds(self):
        assert "JSON" in FetchTool.description and "XML" in FetchTool.description


class TestNoSilentCut:
    """v10.56.2: 페이지마다 20만 자에서 표시 없이 자르던 것을 없앴다 — 큰 결과는
    공용 과대 출력 정책(파일 + 발췌)이 전체를 보존한다."""

    def test_big_json_comes_back_whole_and_valid(self):
        body = json.dumps(
            {"releases": {f"1.{i}": [{"url": "x" * 50}] for i in range(4000)}}
        )
        c, err = _get(_resp("application/json", body))
        assert err is None and len(c) > 200_000
        assert len(json.loads(c)["releases"]) == 4000  # 끝까지 — 깨지지 않았다

    def test_big_html_keeps_the_tail(self):
        body = (
            "<html><body>"
            + "<div class='x'>filler</div>" * 20_000
            + "<p>THE END</p></body></html>"
        )
        assert len(body) > 200_000
        c, err = _get(_resp("text/html; charset=utf-8", body))
        assert err is None and c.endswith("THE END")

    def test_big_text_is_whole(self):
        body = "a" * 300_000 + "TAIL"
        c, _ = _get(_resp("text/plain; charset=utf-8", body))
        assert len(c) == 300_004 and c.endswith("TAIL")


class TestDisguisedBlocks:
    """v10.56.3: 200·202 로 오는 봇 확인·JS 껍데기는 성공이 아니라 실패다 — 종전엔
    확인 페이지 글자나 빈 껍데기가 내용으로 넘어갔다(DuckDuckGo 202 · Google 89자)."""

    def _html(self, body, status=200):
        r = _resp("text/html; charset=utf-8", body)
        r.status_code = status
        return r

    def test_bot_check_page_is_a_failure(self):
        body = (
            "<html><div class='anomaly-modal'>Unfortunately, bots use DuckDuckGo too. "
            "Please confirm this search was made by a human.</div></html>"
        )
        c, err = _get(self._html(body, 202))
        assert c == "" and err.startswith(
            "Fetch failed (bot-check): https://example.com/x\n"
        )
        assert '("made by a human")' in err  # 보이는 글이 먼저

    def test_js_shell_is_a_failure(self):
        body = (
            "<html><script>" + "x" * 20_000 + "</script><div>Google Search</div></html>"
        )
        c, err = _get(self._html(body))
        assert c == "" and err.startswith(
            "Fetch failed (needs-js): https://example.com/x\n"
        )

    def test_a_long_article_about_captcha_is_content(self):
        body = (
            "<html><p>"
            + "A CAPTCHA is a challenge-response test. " * 200
            + "</p></html>"
        )
        c, err = _get(self._html(body))
        assert err is None and "CAPTCHA" in c

    def test_a_widget_name_alone_marks_the_check(self):
        body = "<html><div class='cf-chl-widget'>Just a moment...</div></html>"
        _, err = _get(self._html(body, 200))
        assert '"cf-chl"' in err

    def test_captcha_inside_a_script_is_not_a_check(self):
        """GitHub blob 페이지 실측 — 기능 플래그 `octocaptcha_origin_optimization`."""
        body = (
            '<html><script>{"flags":["octocaptcha_origin_optimization"]}</script>'
            "<h1>Requests</h1><p>Requests is a simple, yet elegant, HTTP library.</p></html>"
        )
        c, err = _get(self._html(body))
        assert err is None and "elegant" in c

    def test_a_small_plain_page_is_content(self):
        c, err = _get(self._html("<html><h1>Example Domain</h1></html>"))
        assert err is None and "Example Domain" in c


class TestTables:
    """v10.56.4: 표의 행이 한 줄로 붙던 것 — 코드 보기(gitiles·cgit)와 정보상자."""

    def _md(self, html):
        p = _HTMLToMarkdown()
        p.feed(html)
        return p.get_markdown()

    def test_code_view_rows_are_lines_and_empty_number_cells_vanish(self):
        html = (
            "<table>"
            + "".join(
                f'<tr><td data-line-number="{i}"></td><td><span>{t}</span></td></tr>'
                for i, t in enumerate(
                    ["wpa_supplicant", "==============", "Copyright"], 1
                )
            )
            + "</table>"
        )
        assert self._md(html) == "wpa_supplicant\n==============\nCopyright"

    def test_cells_with_text_are_joined_by_a_bar(self):
        html = (
            "<table><tr><th>Designed by</th><td>Guido van Rossum</td></tr>"
            "<tr><th>Developer</th><td>PSF</td></tr></table>"
        )
        assert self._md(html) == "Designed by | Guido van Rossum\nDeveloper | PSF"

    def test_line_breaks_inside_a_cell_stay_in_the_row(self):
        # Wikipedia 키 크기 표 — "ML-DSA[48]\\n | Lattice\\n | …" 로 갈라졌다
        html = (
            "<table>\n<tr>\n<td>ML-DSA<sup>[48]</sup>\n</td>\n<td>Lattice\n</td>\n"
            "<td>1,312<br>B</td>\n</tr>\n</table>"
        )
        assert self._md(html) == "ML-DSA[48] | Lattice | 1,312 B"

    def test_a_multi_row_table_inside_a_cell_keeps_its_rows(self):
        html = (
            "<table><tr><td>outer</td><td><table><tr><td>a</td><td>b</td></tr>"
            "<tr><td>c</td><td>d</td></tr></table></td></tr></table>"
        )
        assert self._md(html) == "outer | a | b\nc | d"

    def test_a_table_inside_a_cell_keeps_its_own_rows(self):
        html = (
            "<table><tr><td>outer</td><td><table><tr><td>a</td><td>b</td></tr>"
            "</table></td></tr></table>"
        )
        out = self._md(html)
        assert "a | b" in out and out.startswith("outer | ")


def _md(html, base=""):
    p = _HTMLToMarkdown(base)
    p.feed(html)
    return p.get_markdown()


class TestLinks:
    """v10.57.2: 링크가 본문에 없어 검색 결과를 따라갈 수 없었다(x8jkpw Bing·Scholar)."""

    BASE = "https://git.kernel.org/pub/scm/x.git/log/"

    def test_off_site_link_is_absolute(self):
        out = _md(
            '<p>see <a href="https://doi.org/10.1/x">the paper</a></p>', self.BASE
        )
        assert out == "see [the paper](https://doi.org/10.1/x)"

    def test_same_site_link_is_a_path(self):
        out = _md(
            '<a href="../commit/?id=8070ed57">calipso: keep BIG TCP</a>', self.BASE
        )
        assert out == "[calipso: keep BIG TCP](/pub/scm/x.git/commit/?id=8070ed57)"

    def test_same_site_link_that_repeats_its_text_keeps_only_the_text(self):
        base = "https://en.wikipedia.org/wiki/Post-quantum_cryptography"
        out = _md(
            '<p><a href="/wiki/Shor%27s_algorithm">Shor\'s algorithm</a> and '
            '<a href="/wiki/Quantum_computing">quantum computer</a></p>',
            base,
        )
        assert out == (
            "Shor's algorithm and [quantum computer](/wiki/Quantum_computing)"
        )

    def test_icon_link_and_bare_url_text_add_no_brackets(self):
        out = _md(
            '<a href="https://x.org/a"><img src="i.png"></a>'
            '<a href="https://x.org/b">https://x.org/b</a>'
        )
        assert out == "https://x.org/b"

    def test_link_inside_pre_is_left_as_code(self):
        out = _md('<pre>call <a href="/f">f</a>()</pre>', self.BASE)
        assert "](" not in out and "call f()" in out

    def test_bing_tracking_link_is_unwrapped(self):
        import base64

        target = "https://support.google.com/business/answer/2853879?hl=th"
        u = "a1" + base64.urlsafe_b64encode(target.encode()).decode().rstrip("=")
        href = f"https://www.bing.com/ck/a?!&amp;&amp;p=1bb3f2&amp;u={u}&amp;ntb=1"
        out = _md(
            f'<h2><a href="{href}">Business</a></h2>', "https://www.bing.com/search"
        )
        assert out == f"## [Business]({target})"

    def test_google_and_duckduckgo_redirects_are_unwrapped(self):
        g = _md(
            '<a href="/url?q=https://a.org/p&amp;sa=U">A</a>',
            "https://www.google.com/search",
        )
        d = _md(
            '<a href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fb.org%2Fq&amp;rut=1">B</a>',
            "https://html.duckduckgo.com/html/",
        )
        assert g == "[A](https://a.org/p)" and d == "[B](https://b.org/q)"

    def test_fetch_single_uses_the_final_url_after_redirects(self):
        r = _resp("text/html", '<a href="https://new.example.com/p">next page</a>')
        r.url = "https://new.example.com/start"
        c, err = _get(r, "https://old.example.com/start")
        assert err is None and c == "[next page](/p)"


class TestLayout:
    """v10.57.2: 블록 경계·숨김 요소·공백 — HTML 결과의 40~70% 가 빈 줄이었다."""

    def test_block_elements_end_lines(self):
        # Scholar 결과 한 건의 모양 — 제목과 저자가 "…networksS Jung" 으로 붙었다
        out = _md(
            '<div class="gs_ri"><h3><a href="https://ieeexplore.ieee.org/d/1">'
            "Distributed routing</a></h3><div>S Jung, D Lee - 2009</div>"
            "<div>Congested hot spots</div></div>"
        )
        assert out.split("\n") == [
            "### [Distributed routing](https://ieeexplore.ieee.org/d/1)",
            "S Jung, D Lee - 2009",
            "Congested hot spots",
        ]

    def test_hidden_elements_are_skipped(self):
        out = _md(
            '<div style="display: none">현재 시스템이 작동되지 않습니다</div>'
            '<span hidden>h</span><i aria-hidden="true">icon</i>'
            '<span style="visibility:hidden">v</span><p>결과 13개</p>'
        )
        assert out == "결과 13개"

    def test_skip_ends_at_its_own_closing_tag(self):
        # 종전의 불 값 — nav 안의 script 가 닫히면서 nav 의 나머지가 새어 나왔다
        out = _md(
            "<nav><script>x()</script>menu<div><nav>inner</nav>still menu</div></nav>"
            "<p>body</p>"
        )
        assert out == "body"

    def test_hidden_on_an_element_with_optional_end_tag_is_ignored(self):
        # 닫히지 않은 <p hidden> 을 건너뛰기 시작하면 페이지 끝까지 사라진다
        assert "rest" in _md("<p hidden>x<p>rest")

    def test_whitespace_folds_like_html(self):
        out = _md(
            "<ul>\n\t\t<li>\n\t</li>\n<li>one\n   two</li></ul>\n\t\n\n\n<p>end</p>"
        )
        assert out == "- one two\n\nend"

    def test_pre_keeps_its_whitespace(self):
        out = _md("<p>x</p><pre>a\n\n\n    b\t c</pre>")
        assert "a\n\n\n    b\t c" in out

    def test_image_alt_is_the_text(self):
        # Wikipedia 수식: MathML 은 숨김, 보이는 것은 alt 의 TeX
        out = _md(
            '<p>using <span style="display:none"><math>O(√N)</math></span>'
            '<img alt="{\\displaystyle O({\\sqrt {N}})}" src="m.svg"> calls</p>'
        )
        assert out == "using {\\displaystyle O({\\sqrt {N}})} calls"


class TestGoogleResponses:
    """v10.56.4: Gerrit·gitiles — XSSI 접두 JSON 과 base64 파일 원문."""

    def test_xssi_prefixed_json_is_parsed(self):
        c, err = _get(_resp("application/json", ')]}\'\n{"log":[{"commit":"ab49"}]}'))
        assert err is None and json.loads(c) == {"log": [{"commit": "ab49"}]}
        assert c.startswith("{\n")

    def test_prefix_with_broken_json_is_handed_over_as_is(self):
        raw = ")]}'\n{broken"
        c, _ = _get(_resp("application/json", raw))
        assert c == raw

    def _blob(self, body):
        r = _resp("text/plain; charset=utf-8", body)
        r.headers["x-gitiles-object-type"] = "blob"
        return r

    def test_gitiles_blob_is_decoded(self):
        import base64

        enc = base64.b64encode("wpa_supplicant\n=====\n한글\n".encode()).decode()
        c, err = _get(self._blob(enc))
        assert err is None and c == "wpa_supplicant\n=====\n한글\n"

    def test_same_body_without_the_header_is_left_alone(self):
        c, _ = _get(_resp("text/plain; charset=utf-8", "d3BhX3N1cHBsaWNhbnQ="))
        assert c == "d3BhX3N1cHBsaWNhbnQ="

    def test_binary_blob_is_downloaded_decoded(self, tmp_path, monkeypatch):
        import base64

        monkeypatch.chdir(tmp_path)
        png = b"\x89PNG\r\n\x1a\n\xff\xfe"
        c, err = _get(
            self._blob(base64.b64encode(png).decode()),
            url="https://android.googlesource.com/p/+/main/logo.png?format=TEXT",
        )
        assert err is None and "downloaded" in c
        assert (tmp_path / ".agent-cli/tmp/downloads/logo.png").read_bytes() == png

    def test_not_base64_is_left_alone(self):
        c, err = _get(self._blob("plain words here!"))
        assert err is None and c == "plain words here!"


class TestCharset:
    """v10.52.0: charset 없는 text/* 를 requests 는 ISO-8859-1 로 읽어 UTF-8 이
    깨졌다(docs.python.org 의 '—' → 'â\\x80\\x94')."""

    def test_utf8_without_charset_is_not_mojibake(self):
        c, _ = _get(_resp("text/html", "<p>statistics — 통계</p>"))
        assert "statistics — 통계" in c and "â" not in c

    def test_header_charset_wins(self):
        c, _ = _get(_resp("text/plain; charset=euc-kr", "한글".encode("euc-kr")))
        assert c == "한글"

    def test_meta_charset_when_header_has_none(self):
        body = '<meta charset="euc-kr"><p>한글</p>'.encode("euc-kr")
        c, _ = _get(_resp("text/html", body))
        assert "한글" in c

    def test_not_utf8_falls_back_to_detected_encoding(self):
        c, _ = _get(_resp("text/plain", "café".encode("latin-1"), apparent="latin-1"))
        assert c == "café"

    def test_unknown_charset_name_does_not_crash(self):
        c, err = _get(_resp("text/plain; charset=bogus-9", "ok"))
        assert err is None and c == "ok"


class TestToolFetch:
    def test_empty_url(self):
        result = tool_fetch({"url": ""})
        assert not result.success
        assert "required" in result.error

    def test_auto_https(self):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.headers = {"content-type": "text/html"}
        mock_resp.content = b"<p>content</p>"

        with patch(
            "agent_cli.tools.fetch.requests.get", return_value=mock_resp
        ) as mock_get:
            tool_fetch({"url": "example.com"})
            called_url = mock_get.call_args[0][0]
            assert called_url.startswith("https://")

    def test_full_content_returned(self):
        """No truncation — full content returned like read_file."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.headers = {"content-type": "text/html"}
        mock_resp.content = ("<p>" + "x" * 10000 + "</p>").encode()

        with patch("agent_cli.tools.fetch.requests.get", return_value=mock_resp):
            result = tool_fetch({"url": "https://example.com"})
            assert result.success
            assert "x" * 100 in result.output  # Full content, not truncated

    def test_errors_state_facts_without_advice(self):
        """v10.92.0 — "Hint: … Try again later." 가 없다. v10.118.0 부터 실패는 세 줄
        틀이고(`_HTTP_FACTS` 의 403·404 사실은 둘째 줄로 흡수), `tool_fetch` 는 문구를
        덧붙이지 않고 그대로 넘긴다."""
        import requests as req

        with patch(
            "agent_cli.tools.fetch.requests.get",
            side_effect=req.exceptions.ConnectionError(),
        ):
            r = tool_fetch({"url": "https://bad.example.com"})
        assert r.error == (
            "Fetch failed (connect): https://bad.example.com\n"
            "No connection to the host.\n"
            "One retry may succeed; after that, assume the host is unreachable from here."
        )
        for code, cause, fact in (
            (
                403,
                "http-auth",
                "The server refused this client (HTTP 403: login or bot protection).",
            ),
            (
                404,
                "http-missing",
                "The page does not exist at this address (HTTP 404).",
            ),
            (500, "http-server", "The server failed (HTTP 500)."),
        ):
            resp = MagicMock()
            resp.status_code = code
            resp.headers = {"content-type": "text/html"}
            resp.content = b"x"
            with patch("agent_cli.tools.fetch.requests.get", return_value=resp):
                r = tool_fetch({"url": "https://example.com/p"})
            head, second, _ = r.error.split("\n")
            assert (head, second) == (
                f"Fetch failed ({cause}): https://example.com/p",
                fact,
            )
            assert " — " not in r.error
        assert "Hint" not in r.error and "Try again" not in r.error

    def test_a_page_is_one_request(self):
        """v10.92.0 — `depth` 가 없다: 링크를 따라가지 않는다."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.headers = {"content-type": "text/html"}
        mock_resp.content = b'<p>main</p><a href="/page2">link</a>'

        with patch(
            "agent_cli.tools.fetch.requests.get", return_value=mock_resp
        ) as mock_get:
            r = tool_fetch({"url": "https://example.com", "depth": 1})
            assert mock_get.call_count == 1
        assert r.output == (
            "[Fetched 18 chars from https://example.com]\n\nmain[link](/page2)"
        )

    def test_header_in_output(self):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.headers = {"content-type": "text/html"}
        mock_resp.content = b"<p>content</p>"

        with patch("agent_cli.tools.fetch.requests.get", return_value=mock_resp):
            result = tool_fetch({"url": "https://example.com"})
            assert result.output.startswith(
                "[Fetched 7 chars from https://example.com]"
            )


# ── flat-native schema (Step 3 completion, v4.38.0) ──────────────────


class TestFetchFlatNative:
    """fetch was the LAST builtin still advertising prefixed keys
    (`fetch_url`) — flattening it makes the documented "every builtin is
    flat-native" invariant actually true."""

    def _tool(self):
        from agent_cli.tools.fetch import FetchTool

        return FetchTool()

    def test_schema_is_flat(self):
        t = self._tool()
        props = t.parameters["properties"]
        assert set(props) == {"url", "download_to"}
        assert t.parameters["required"] == ["url"]

    def test_wrap_single_op_is_identity(self):
        flat = {"url": "http://x", "download_to": "a"}
        assert self._tool().wrap_single_op(flat) == flat

    def test_claims_false_for_flat_payload(self):
        # claims (prefix matching) must be latent for the flat shape —
        # infer_action no longer resolves a bare {url} to fetch.
        assert self._tool().claims({"url": "http://x"}) is False

    def test_legacy_prefixed_keys_still_strip(self):
        # Old sessions' re-fed priors may emit fetch_url — strip_prefix
        # keeps translating them to standard keys at dispatch.
        t = self._tool()
        assert t.strip_prefix({"fetch_url": "http://x", "fetch_download_to": "a"}) == {
            "url": "http://x",
            "download_to": "a",
        }

    def test_no_builtin_claims_any_flat_payload(self):
        # The invariant the docs state: every builtin is flat-native, so
        # claims() is False for every builtin against its own flat input.
        from agent_cli.tools import TOOLS

        samples = {
            "fetch": {"url": "http://x"},
            "read_file": {"path": "a.py"},
            "shell": {"command": "ls"},
            "write_file": {"path": "a", "content": "x"},
            "agent": {"mode": "run", "task": "t"},
        }
        for name, payload in samples.items():
            assert TOOLS[name].claims(payload) is False, name


class TestDownload:
    """v10.58.0: ``download_to`` 는 응답을 변환 없이 파일로, 바이너리는 묻지 않아도
    ``.agent-cli/tmp/downloads/`` 로 — 종전엔 바이너리를 거절했다."""

    def _run(self, resp, args):
        with patch("agent_cli.tools.fetch.requests.get", return_value=resp):
            return tool_fetch(args)

    def test_html_is_saved_raw_not_converted(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        html = "<html><body><h1>T</h1><p>body</p></body></html>"
        r = self._run(
            _resp("text/html", html),
            {"url": "https://example.com/a", "download_to": "out/a.html"},
        )
        assert r.success
        assert (tmp_path / "out/a.html").read_text() == html
        assert r.output.startswith("[Downloaded https://example.com/a → out/a.html")
        assert "unchanged" in r.output and "First lines:\n<html>" in r.output

    def test_json_is_saved_as_sent(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        body = '{"a":1,"b":[2,3]}'
        r = self._run(
            _resp("application/json", body),
            {"url": "https://example.com/j", "download_to": "j.json"},
        )
        assert r.success and (tmp_path / "j.json").read_text() == body

    def test_binary_to_path_has_no_excerpt(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        r = self._run(
            _resp("application/zip", b"PK\x03\x04\x00\xff"),
            {"url": "https://example.com/z", "download_to": "z.zip"},
        )
        assert r.success and "First lines" not in r.output
        assert "6 bytes, application/zip" in r.output

    def test_gitiles_blob_is_saved_decoded(self, tmp_path, monkeypatch):
        import base64

        monkeypatch.chdir(tmp_path)
        resp = _resp("text/plain; charset=utf-8", base64.b64encode(b"src\n").decode())
        resp.headers["x-gitiles-object-type"] = "blob"
        r = self._run(resp, {"url": "https://g/x?format=TEXT", "download_to": "x.c"})
        assert r.success and (tmp_path / "x.c").read_bytes() == b"src\n"

    def test_bot_check_page_is_not_saved(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        page = "<html><body><p>Please verify you are human</p></body></html>"
        r = self._run(
            _resp("text/html", page),
            {"url": "https://example.com/s", "download_to": "s.html"},
        )
        assert not r.success
        assert r.error.startswith("Fetch failed (bot-check): https://example.com/s\n")
        assert not (tmp_path / "s.html").exists()

    def test_http_error_saves_nothing(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        resp = _resp("text/html", "nope")
        resp.status_code = 404
        r = self._run(resp, {"url": "https://example.com/n", "download_to": "n"})
        assert not r.success
        assert r.error.startswith(
            "Fetch failed (http-missing): https://example.com/n\n"
        )
        assert not (tmp_path / "n").exists()

    def test_empty_download_to_is_rejected(self):
        r = tool_fetch({"url": "https://example.com", "download_to": " "})
        assert not r.success and "file path" in r.error

    def test_auto_name_numbers_a_clash_and_guesses_the_extension(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.chdir(tmp_path)
        for _ in range(2):
            _get(_resp("image/png", b"\x89PNG\xff"), url="https://e.com/img/")
        names = sorted(
            p.name for p in (tmp_path / ".agent-cli/tmp/downloads").iterdir()
        )
        assert names == ["img-1.png", "img.png"]

    def test_auto_name_without_a_path_segment(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        c, _ = _get(_resp("application/octet-stream", b"\xff\x00"), url="https://e.com")
        assert "downloads/download" in c

    def test_auto_name_adds_the_type_extension(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        c, _ = _get(
            _resp("application/pdf", b"%PDF\xff"),
            url="https://arxiv.org/pdf/1706.03762",
        )
        assert "downloads/1706.03762.pdf" in c  # .03762 는 확장자가 아니다
        c, _ = _get(
            _resp("application/octet-stream", b"\x1f\x8b\xff"),
            url="https://e.com/a.tar.gz",
        )
        assert "downloads/a.tar.gz" in c  # octet-stream 은 이름 그대로

    def test_touched_paths_names_the_target(self):
        t = FetchTool()
        assert t.touched_paths({"url": "u", "download_to": "a/b.pdf"}) == ["a/b.pdf"]
        assert t.touched_paths({"url": "u"}) == []

    def test_schema_and_description(self):
        assert "download_to" in FetchTool.parameters["properties"]
        assert "download_to" in FetchTool.description
        assert ".agent-cli/tmp/downloads/" in FetchTool.description


# ── v10.118.0 실패 문구: 세 줄 틀 + 원인 분류 ──────────────────────────
# 설계 harness-top3 §3 의 확정 문구를 글자 그대로 고정한다 — 모델이 받는 결과다.
# 네트워크는 쓰지 않는다: `requests.get` 을 가짜로 바꾸고, 예외는 requests·urllib3
# 가 실제로 감싸는 모양(ConnectionError(MaxRetryError(reason=…)))으로 만든다.

URL = "https://example.com/p"
FIRST_LINE = __import__("re").compile(r"^Fetch failed \([a-z-]+\): \S+$")


def _dns_error():
    import socket

    import requests as req
    import urllib3.exceptions as u3

    try:
        try:
            raise socket.gaierror(8, "nodename nor servname provided, or not known")
        except socket.gaierror as e:
            raise u3.NameResolutionError("example.com", None, e) from e
    except u3.NameResolutionError as nre:
        return req.exceptions.ConnectionError(u3.MaxRetryError(None, "/p", nre))


def _connect_error():
    import requests as req
    import urllib3.exceptions as u3

    try:
        try:
            raise ConnectionRefusedError(61, "Connection refused")
        except OSError as e:
            raise u3.NewConnectionError(None, f"Failed to establish: {e}") from e
    except u3.NewConnectionError as nce:
        return req.exceptions.ConnectionError(u3.MaxRetryError(None, "/p", nce))


def _tls_error():
    import ssl

    import requests as req
    import urllib3.exceptions as u3

    inner = ssl.SSLCertVerificationError(
        1,
        "[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: "
        "self-signed certificate in certificate chain (_ssl.c:1006)",
    )
    inner.verify_message = "self-signed certificate in certificate chain"
    return req.exceptions.SSLError(u3.MaxRetryError(None, "/p", u3.SSLError(inner)))


def _raise(exc, url=URL):
    with patch("agent_cli.tools.fetch.requests.get", side_effect=exc):
        return tool_fetch({"url": url}).error


def _status(code, headers=None, url=URL):
    resp = MagicMock()
    resp.status_code = code
    resp.headers = {"content-type": "text/html", **(headers or {})}
    resp.content = b"x"
    with patch("agent_cli.tools.fetch.requests.get", return_value=resp):
        return tool_fetch({"url": url}).error


def _page(html, *, final_url=URL, status=200):
    resp = _resp("text/html; charset=utf-8", html)
    resp.status_code = status
    resp.url = final_url
    with patch("agent_cli.tools.fetch.requests.get", return_value=resp):
        return tool_fetch({"url": URL})


_BOT_PAGE = "<html><p>Please verify you are human</p></html>"
_JS_SHELL = (
    "<html><script>" + "x" * 20_000 + "</script><div>" + "y" * 110 + "</div></html>"
)
_LOGIN_FORM = (
    "<html><form><p>Sign in to continue</p><input name=u>"
    "<input type='password' name=p></form></html>"
)


def _all_thirteen():
    """cause 13종 각각의 실제 결과 — (cause, 결과 문구)."""
    import requests as req

    return [
        ("dns", _raise(_dns_error())),
        ("connect", _raise(_connect_error())),
        ("timeout", _raise(req.exceptions.ReadTimeout())),
        ("tls", _raise(_tls_error())),
        ("http-auth", _status(403)),
        ("http-missing", _status(404)),
        ("http-rate", _status(429, {"Retry-After": "42"})),
        ("http-server", _status(503)),
        ("http-other", _status(418)),
        ("bot-check", _page(_BOT_PAGE).error),
        ("needs-js", _page(_JS_SHELL).error),
        ("login-wall", _page(_LOGIN_FORM).error),
        ("request", _raise(req.exceptions.TooManyRedirects("Exceeded 30 redirects."))),
    ]


class TestFailureCauses:
    """v10.118.0: 실패는 `Fetch failed (<cause>): <url>` / 사실 / 재시도 판정."""

    EXPECTED: ClassVar[dict[str, tuple[str, str]]] = {
        "dns": (
            "The host name did not resolve.",
            "Retrying the same URL will not help; check the address.",
        ),
        "connect": (
            "No connection to the host.",
            "One retry may succeed; after that, assume the host is unreachable from here.",
        ),
        "timeout": ("The host did not answer within 15s.", "One retry may succeed."),
        "tls": (
            (
                "The TLS handshake failed (certificate verify failed: self-signed "
                "certificate in certificate chain)."
            ),
            "Retrying will not help from this client.",
        ),
        "http-auth": (
            "The server refused this client (HTTP 403: login or bot protection).",
            "Retrying the same URL will not help.",
        ),
        "http-missing": (
            "The page does not exist at this address (HTTP 404).",
            "Retrying will not help; the address is wrong or the page moved.",
        ),
        "http-rate": (
            "Rate limited; the server asks to wait 42s (Retry-After).",
            "Retry after that wait, once.",
        ),
        "http-server": (
            "The server failed (HTTP 503).",
            "One retry after a short wait may succeed.",
        ),
        "http-other": (
            "The server returned HTTP 418.",
            "Retrying the same URL will not help.",
        ),
        "bot-check": (
            'The page is a bot check, not the content ("verify you are human").',
            "The harness does not pass bot checks; retrying will not help.",
        ),
        "needs-js": (
            "The page renders in a browser; the HTML has 110 chars of text.",
            "Retrying will not help.",
        ),
        "login-wall": (
            "The page is a sign-in form, not the content.",
            "Retrying will not help; this source needs an account.",
        ),
        "request": (
            "The request failed (Exceeded 30 redirects.).",
            "Retrying the same URL will not help.",
        ),
    }

    def test_every_cause_is_three_exact_lines(self):
        got = _all_thirteen()
        assert [c for c, _ in got] == list(self.EXPECTED)  # 13종 빠짐없이
        for cause, text in got:
            fact, verdict = self.EXPECTED[cause]
            assert text == f"Fetch failed ({cause}): {URL}\n{fact}\n{verdict}", cause

    def test_first_line_grammar_holds_for_every_cause(self):
        for cause, text in _all_thirteen():
            first = text.split("\n")[0]
            assert FIRST_LINE.match(first), first
            assert text.count("\n") == 2, cause

    def test_connect_timeout_is_a_timeout_and_tls_is_not_connect(self):
        """requests 에서 `ConnectTimeout`·`SSLError` 는 `ConnectionError` 의 하위
        클래스 — 종전 순서에선 둘 다 "Connection failed" 였다."""
        import requests as req

        assert _raise(req.exceptions.ConnectTimeout()).startswith(
            "Fetch failed (timeout)"
        )
        assert _raise(_tls_error()).startswith("Fetch failed (tls)")

    def test_dns_versus_connect(self):
        import requests as req

        assert _raise(_dns_error()).startswith("Fetch failed (dns)")
        assert _raise(_connect_error()).startswith("Fetch failed (connect)")
        assert _raise(req.exceptions.ConnectionError()).startswith(
            "Fetch failed (connect)"
        )

    def test_http_code_families(self):
        for code, cause in (
            (401, "http-auth"),
            (410, "http-missing"),
            (500, "http-server"),
            (502, "http-server"),
            (400, "http-other"),
            (405, "http-other"),
        ):
            assert _status(code).startswith(f"Fetch failed ({cause}): {URL}\n"), code


class TestRetryAfter:
    def test_seconds_header(self):
        assert _status(429, {"Retry-After": "120"}).split("\n")[1:] == [
            "Rate limited; the server asks to wait 120s (Retry-After).",
            "Retry after that wait, once.",
        ]

    def test_http_date_header(self):
        from datetime import datetime, timedelta, timezone
        from email.utils import format_datetime

        when = datetime.now(timezone.utc) + timedelta(seconds=90)
        second = _status(429, {"Retry-After": format_datetime(when, usegmt=True)})
        secs = int(second.split("\n")[1].split("wait ")[1].split("s ")[0])
        assert 85 <= secs <= 91

    @pytest.mark.parametrize(
        "headers", [{}, {"Retry-After": "soon"}, {"Retry-After": ""}]
    )
    def test_missing_or_unreadable_header_says_so(self, headers):
        assert _status(429, headers) == (
            f"Fetch failed (http-rate): {URL}\n"
            "Rate limited; the server did not say how long to wait.\n"
            "Retry once after about a minute."
        )


class TestProxySentence:
    """프록시 문장은 환경에 변수가 실제로 있을 때만 — 이름만, 값은 적지 않는다."""

    def test_no_proxy_variables_no_sentence(self):
        assert "proxy" not in _raise(_connect_error()).lower()

    @pytest.mark.parametrize("name", ["HTTPS_PROXY", "http_proxy", "ALL_PROXY"])
    def test_configured_proxy_is_named_not_its_value(self, monkeypatch, name):
        monkeypatch.setenv(name, "http://secret-user:pw@proxy.corp:3128")
        text = _raise(_connect_error())
        assert text.split("\n")[1] == (
            f"No connection to the host. A proxy is configured ({name})."
        )
        assert "secret" not in text and "proxy.corp" not in text

    def test_no_proxy_star_is_stated(self, monkeypatch):
        monkeypatch.setenv("HTTPS_PROXY", "http://proxy.corp:3128")
        monkeypatch.setenv("NO_PROXY", "*")
        assert _raise(_dns_error()).split("\n")[1] == (
            "The host name did not resolve. A proxy is configured (HTTPS_PROXY). "
            "NO_PROXY=* disables fetching through the proxy."
        )

    def test_no_proxy_star_without_a_proxy_says_nothing(self, monkeypatch):
        monkeypatch.setenv("no_proxy", "*")
        assert _raise(_dns_error()).split("\n")[1] == "The host name did not resolve."

    def test_proxy_sentence_only_on_connection_causes(self, monkeypatch):
        import requests as req

        monkeypatch.setenv("HTTPS_PROXY", "http://proxy.corp:3128")
        assert "proxy" not in _raise(req.exceptions.ReadTimeout()).lower()
        assert "proxy" not in _status(403).lower()


class TestLoginWall:
    def test_login_path_after_redirect(self):
        body = (
            "<html><h1>Welcome</h1><p>"
            + "Our product does many things. " * 20
            + "</p></html>"
        )
        r = _page(body, final_url="https://example.com/accounts/sign-in?next=/p")
        assert not r.success and r.error.startswith(
            f"Fetch failed (login-wall): {URL}\n"
        )

    def test_short_page_with_password_input(self):
        r = _page(_LOGIN_FORM)
        assert not r.success and r.error.startswith("Fetch failed (login-wall)")

    def test_long_page_with_a_password_input_is_content(self):
        body = (
            "<html><p>"
            + "This guide explains how password fields work in HTML forms. " * 80
            + "</p><input type='password'></html>"
        )
        r = _page(body)
        assert r.success and "password fields" in r.output

    def test_a_segment_that_only_contains_login_is_content(self):
        """조각이 통째로 같을 때만 — 글 주소의 낱말(``how-to-login-x``)은 로그인 벽이 아니다."""
        body = (
            "<html><h1>How to log in</h1><p>"
            + "Step one, open the app. " * 30
            + "</p></html>"
        )
        for final in (
            "https://example.com/blog/how-to-login-x",
            "https://example.com/login.php-guide/notes",
            "https://example.com/oauth/intro",
        ):
            r = _page(body, final_url=final)
            assert r.success and "How to log in" in r.output, final

    def test_whole_segment_match_ignores_case(self):
        r = _page(
            "<html><p>Welcome back.</p></html>", final_url="https://example.com/x/Auth/"
        )
        assert r.error.startswith("Fetch failed (login-wall)")

    def test_ordinary_page_is_content(self):
        r = _page(
            "<html><h1>Release notes</h1><p>Fixed the author page.</p></html>",
            final_url="https://example.com/author/notes",
        )
        assert r.success and "Release notes" in r.output

    def test_bot_check_wins_over_login_wall(self):
        r = _page(_BOT_PAGE, final_url="https://example.com/login")
        assert r.error.startswith("Fetch failed (bot-check)")


class TestDownloadFailureText:
    def test_download_uses_the_same_frame(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        resp = _resp("text/html", _LOGIN_FORM)
        resp.url = "https://example.com/login"
        with patch("agent_cli.tools.fetch.requests.get", return_value=resp):
            r = tool_fetch({"url": URL, "download_to": "p.html"})
        assert r.error == (
            f"Fetch failed (login-wall): {URL}\n"
            "The page is a sign-in form, not the content.\n"
            "Retrying will not help; this source needs an account."
        )
        assert not (tmp_path / "p.html").exists()
