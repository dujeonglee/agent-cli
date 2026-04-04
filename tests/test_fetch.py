"""Tests for the fetch tool."""

from unittest.mock import MagicMock, patch


from agent_cli.tools.fetch import (
    tool_fetch,
    _fetch_single,
    _resolve_links,
    _HTMLToMarkdown,
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
        mock_resp.text = "<h1>Hello</h1><p>World</p>"

        with patch("agent_cli.tools.fetch.requests.get", return_value=mock_resp):
            content, links, error = _fetch_single("https://example.com")
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
        mock_resp.text = "Plain text content"

        with patch("agent_cli.tools.fetch.requests.get", return_value=mock_resp):
            content, _, error = _fetch_single("https://example.com/file.txt")
            assert error is None
            assert "Plain text content" in content


class TestToolFetch:
    def test_empty_url(self):
        result = tool_fetch({"url": ""})
        assert not result.success
        assert "required" in result.error

    def test_auto_https(self):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.headers = {"content-type": "text/html"}
        mock_resp.text = "<p>content</p>"

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
        mock_resp.text = "<p>" + "x" * 10000 + "</p>"

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
        mock_resp.text = '<p>main</p><a href="/page2">link</a>'

        with patch(
            "agent_cli.tools.fetch.requests.get", return_value=mock_resp
        ) as mock_get:
            tool_fetch({"url": "https://example.com", "depth": 0})
            assert mock_get.call_count == 1

    def test_depth_1_follows_links(self):
        main_resp = MagicMock()
        main_resp.status_code = 200
        main_resp.headers = {"content-type": "text/html"}
        main_resp.text = '<p>main</p><a href="/page2">link</a>'

        child_resp = MagicMock()
        child_resp.status_code = 200
        child_resp.headers = {"content-type": "text/html"}
        child_resp.text = "<p>child page</p>"

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
        mock_resp.text = "<p>content</p>"

        with patch("agent_cli.tools.fetch.requests.get", return_value=mock_resp):
            result = tool_fetch({"url": "https://example.com"})
            assert "Fetched 1 page" in result.output
