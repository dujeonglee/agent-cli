"""Tests for the fetch tool."""

import json
from unittest.mock import MagicMock, patch

from agent_cli.tools.fetch import (
    FetchTool,
    _body_kind,
    _fetch_single,
    _HTMLToMarkdown,
    _resolve_links,
    tool_fetch,
)


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

    def test_extracts_links(self):
        parser = _HTMLToMarkdown()
        parser.feed('<a href="/page2">link</a><a href="#anchor">skip</a>')
        links = parser.get_links()
        assert "/page2" in links
        assert "#anchor" not in links


class TestResolveLinks:
    def test_same_domain_kept(self):
        links = _resolve_links("https://example.com/page1", ["/page2", "/page3"])
        assert "https://example.com/page2" in links
        assert "https://example.com/page3" in links

    def test_external_domain_filtered(self):
        links = _resolve_links(
            "https://example.com/page1",
            ["https://other.com/page", "/local"],
        )
        assert len([link for link in links if "other.com" in link]) == 0
        assert any("local" in link for link in links)

    def test_dedup(self):
        links = _resolve_links(
            "https://example.com/",
            ["/page", "/page", "/page"],
        )
        assert len(links) == 1

    def test_fragments_removed(self):
        links = _resolve_links(
            "https://example.com/",
            ["/page#section1", "/page#section2"],
        )
        # Both resolve to /page, deduped
        assert len(links) == 1


class TestFetchSingle:
    def test_success(self):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.headers = {"content-type": "text/html"}
        mock_resp.content = b"<h1>Hello</h1><p>World</p>"

        with patch("agent_cli.tools.fetch.requests.get", return_value=mock_resp):
            content, _links, error = _fetch_single("https://example.com")
            assert error is None
            assert "Hello" in content

    def test_404(self):
        mock_resp = MagicMock()
        mock_resp.status_code = 404

        with patch("agent_cli.tools.fetch.requests.get", return_value=mock_resp):
            _, _, error = _fetch_single("https://example.com/missing")
            assert "404" in error

    def test_timeout(self):
        import requests as req

        with patch(
            "agent_cli.tools.fetch.requests.get",
            side_effect=req.exceptions.Timeout(),
        ):
            _, _, error = _fetch_single("https://slow.example.com")
            assert "Timeout" in error

    def test_connection_error(self):
        import requests as req

        with patch(
            "agent_cli.tools.fetch.requests.get",
            side_effect=req.exceptions.ConnectionError(),
        ):
            _, _, error = _fetch_single("https://nonexistent.example.com")
            assert "Connection failed" in error

    def test_plain_text(self):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.headers = {"content-type": "text/plain"}
        mock_resp.content = b"Plain text content"

        with patch("agent_cli.tools.fetch.requests.get", return_value=mock_resp):
            content, _, error = _fetch_single("https://example.com/file.txt")
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
        c, links, err = _get(_resp("application/json", '{"title":"서울","n":[1,2]}'))
        assert err is None and links == []
        assert c == '{\n "title": "서울",\n "n": [\n  1,\n  2\n ]\n}'

    def test_invalid_json_is_handed_over_as_text(self):
        c, _, err = _get(_resp("application/json", "not json {"))
        assert err is None and c == "not json {"

    def test_xml_is_text(self):
        c, _, err = _get(_resp("application/atom+xml", "<feed><title>t</title></feed>"))
        assert err is None and c == "<feed><title>t</title></feed>"

    def test_binary_is_still_refused(self):
        _, _, err = _get(_resp("application/pdf", b"%PDF-1.7"))
        assert err.startswith("Unsupported content type: application/pdf")

    def test_description_names_the_new_kinds(self):
        assert "JSON" in FetchTool.description and "XML" in FetchTool.description


class TestNoSilentCut:
    """v10.56.2: 페이지마다 20만 자에서 표시 없이 자르던 것을 없앴다 — 큰 결과는
    공용 과대 출력 정책(파일 + 발췌)이 전체를 보존한다."""

    def test_big_json_comes_back_whole_and_valid(self):
        body = json.dumps(
            {"releases": {f"1.{i}": [{"url": "x" * 50}] for i in range(4000)}}
        )
        c, _, err = _get(_resp("application/json", body))
        assert err is None and len(c) > 200_000
        assert len(json.loads(c)["releases"]) == 4000  # 끝까지 — 깨지지 않았다

    def test_big_html_keeps_the_tail(self):
        body = (
            "<html><body>"
            + "<div class='x'>filler</div>" * 20_000
            + "<p>THE END</p></body></html>"
        )
        assert len(body) > 200_000
        c, _, err = _get(_resp("text/html; charset=utf-8", body))
        assert err is None and c.endswith("THE END")

    def test_big_text_is_whole(self):
        body = "a" * 300_000 + "TAIL"
        c, _, _ = _get(_resp("text/plain; charset=utf-8", body))
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
        c, _, err = _get(self._html(body, 202))
        assert c == "" and err.startswith("Bot check: https://example.com/x")
        assert '(HTTP 202, "anomaly-modal")' in err

    def test_js_shell_is_a_failure(self):
        body = (
            "<html><script>" + "x" * 20_000 + "</script><div>Google Search</div></html>"
        )
        c, _, err = _get(self._html(body))
        assert c == "" and err.startswith("Needs JavaScript: https://example.com/x")

    def test_a_long_article_about_captcha_is_content(self):
        body = (
            "<html><p>"
            + "A CAPTCHA is a challenge-response test. " * 200
            + "</p></html>"
        )
        c, _, err = _get(self._html(body))
        assert err is None and "CAPTCHA" in c

    def test_a_small_plain_page_is_content(self):
        c, _, err = _get(self._html("<html><h1>Example Domain</h1></html>"))
        assert err is None and "Example Domain" in c


class TestCharset:
    """v10.52.0: charset 없는 text/* 를 requests 는 ISO-8859-1 로 읽어 UTF-8 이
    깨졌다(docs.python.org 의 '—' → 'â\\x80\\x94')."""

    def test_utf8_without_charset_is_not_mojibake(self):
        c, _, _ = _get(_resp("text/html", "<p>statistics — 통계</p>"))
        assert "statistics — 통계" in c and "â" not in c

    def test_header_charset_wins(self):
        c, _, _ = _get(_resp("text/plain; charset=euc-kr", "한글".encode("euc-kr")))
        assert c == "한글"

    def test_meta_charset_when_header_has_none(self):
        body = '<meta charset="euc-kr"><p>한글</p>'.encode("euc-kr")
        c, _, _ = _get(_resp("text/html", body))
        assert "한글" in c

    def test_not_utf8_falls_back_to_detected_encoding(self):
        c, _, _ = _get(
            _resp("text/plain", "café".encode("latin-1"), apparent="latin-1")
        )
        assert c == "café"

    def test_unknown_charset_name_does_not_crash(self):
        c, _, err = _get(_resp("text/plain; charset=bogus-9", "ok"))
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

    def test_error_with_hint(self):
        import requests as req

        with patch(
            "agent_cli.tools.fetch.requests.get",
            side_effect=req.exceptions.ConnectionError(),
        ):
            result = tool_fetch({"url": "https://bad.example.com"})
            assert not result.success
            assert "Hint" in result.error

    def test_depth_0_no_recursion(self):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.headers = {"content-type": "text/html"}
        mock_resp.content = b'<p>main</p><a href="/page2">link</a>'

        with patch(
            "agent_cli.tools.fetch.requests.get", return_value=mock_resp
        ) as mock_get:
            tool_fetch({"url": "https://example.com", "depth": 0})
            assert mock_get.call_count == 1

    def test_depth_1_follows_links(self):
        main_resp = MagicMock()
        main_resp.status_code = 200
        main_resp.headers = {"content-type": "text/html"}
        main_resp.content = b'<p>main</p><a href="/page2">link</a>'

        child_resp = MagicMock()
        child_resp.status_code = 200
        child_resp.headers = {"content-type": "text/html"}
        child_resp.content = b"<p>child page</p>"

        with patch(
            "agent_cli.tools.fetch.requests.get",
            side_effect=[main_resp, child_resp],
        ):
            result = tool_fetch({"url": "https://example.com", "depth": 1})
            assert result.success
            assert "child page" in result.output

    def test_header_in_output(self):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.headers = {"content-type": "text/html"}
        mock_resp.content = b"<p>content</p>"

        with patch("agent_cli.tools.fetch.requests.get", return_value=mock_resp):
            result = tool_fetch({"url": "https://example.com"})
            assert "Fetched 1 page" in result.output


# ── flat-native schema (Step 3 completion, v4.38.0) ──────────────────


class TestFetchFlatNative:
    """fetch was the LAST builtin still advertising prefixed keys
    (`fetch_url`/`fetch_depth`) — flattening it makes the documented
    "every builtin is flat-native" invariant actually true."""

    def _tool(self):
        from agent_cli.tools.fetch import FetchTool

        return FetchTool()

    def test_schema_is_flat(self):
        t = self._tool()
        props = t.parameters["properties"]
        assert set(props) == {"url", "depth"}
        assert t.parameters["required"] == ["url"]

    def test_wrap_single_op_is_identity(self):
        flat = {"url": "http://x", "depth": 1}
        assert self._tool().wrap_single_op(flat) == flat

    def test_claims_false_for_flat_payload(self):
        # claims (prefix matching) must be latent for the flat shape —
        # infer_action no longer resolves a bare {url} to fetch.
        assert self._tool().claims({"url": "http://x"}) is False

    def test_legacy_prefixed_keys_still_strip(self):
        # Old sessions' re-fed priors may emit fetch_url — strip_prefix
        # keeps translating them to standard keys at dispatch.
        t = self._tool()
        assert t.strip_prefix({"fetch_url": "http://x", "fetch_depth": 2}) == {
            "url": "http://x",
            "depth": 2,
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
