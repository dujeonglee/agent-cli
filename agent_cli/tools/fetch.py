"""Fetch tool — retrieve web page content as markdown."""

from __future__ import annotations

import hashlib
import re
from html.parser import HTMLParser
from pathlib import Path
from typing import ClassVar
from urllib.parse import urljoin, urlparse

import requests

from agent_cli.tools.base import (
    Tool,
    default_oversized_nudge,
    on_disk_oversized_nudge,
)
from agent_cli.tools.result import ToolResult

# ── Configuration ──────────────────────────────
FETCH_TIMEOUT = 15
MAX_CONTENT_CHARS = 200_000  # Per page raw HTML cap
MAX_DEPTH = 3
MAX_PAGES = 10  # Total pages fetched in recursive mode
USER_AGENT = "agent-cli/2.0 (fetch tool)"


class _HTMLToMarkdown(HTMLParser):
    """Minimal HTML to markdown converter."""

    def __init__(self):
        super().__init__()
        self.result: list[str] = []
        self._skip = False
        self._in_pre = False
        self._in_code = False
        self._links: list[str] = []

    def handle_starttag(self, tag, attrs):
        attrs_dict = dict(attrs)
        if tag in ("script", "style", "nav", "footer", "header", "noscript"):
            self._skip = True
            return
        if tag == "pre":
            self._in_pre = True
            self.result.append("\n```\n")
        elif tag == "code" and not self._in_pre:
            self._in_code = True
            self.result.append("`")
        elif tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            level = int(tag[1])
            self.result.append(f"\n{'#' * level} ")
        elif tag == "p":
            self.result.append("\n\n")
        elif tag == "br":
            self.result.append("\n")
        elif tag == "li":
            self.result.append("\n- ")
        elif tag == "a":
            href = attrs_dict.get("href", "")
            if href and not href.startswith(("#", "javascript:")):
                self._links.append(href)

    def handle_endtag(self, tag):
        if tag in ("script", "style", "nav", "footer", "header", "noscript"):
            self._skip = False
            return
        if tag == "pre":
            self._in_pre = False
            self.result.append("\n```\n")
        elif tag == "code" and not self._in_pre:
            self._in_code = False
            self.result.append("`")

    def handle_data(self, data):
        if self._skip:
            return
        self.result.append(data)

    def get_markdown(self) -> str:
        text = "".join(self.result)
        # Clean up excessive whitespace
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()

    def get_links(self) -> list[str]:
        return self._links


def _fetch_single(url: str) -> tuple[str, list[str], str | None]:
    """Fetch a single URL.

    Returns (markdown_content, links_found, error_or_none).
    """
    try:
        resp = requests.get(
            url,
            timeout=FETCH_TIMEOUT,
            headers={"User-Agent": USER_AGENT},
            allow_redirects=True,
        )
    except requests.exceptions.ConnectionError:
        return "", [], f"Connection failed: {url} (DNS or network error)"
    except requests.exceptions.Timeout:
        return "", [], f"Timeout after {FETCH_TIMEOUT}s: {url}"
    except requests.exceptions.SSLError as e:
        return "", [], f"SSL error: {url} ({e})"
    except requests.exceptions.RequestException as e:
        return "", [], f"Request failed: {url} ({e})"

    if resp.status_code >= 400:
        return "", [], f"HTTP {resp.status_code}: {url}"

    content_type = resp.headers.get("content-type", "")
    if "text/html" not in content_type and "text/plain" not in content_type:
        return "", [], f"Unsupported content type: {content_type} for {url}"

    html = resp.text[:MAX_CONTENT_CHARS]

    if "text/plain" in content_type:
        return html, [], None

    parser = _HTMLToMarkdown()
    try:
        parser.feed(html)
    except Exception:
        return html[:MAX_CONTENT_CHARS], [], None

    return parser.get_markdown(), parser.get_links(), None


def _resolve_links(base_url: str, links: list[str]) -> list[str]:
    """Resolve relative links and filter to same domain only."""
    base_domain = urlparse(base_url).netloc
    resolved = []
    seen = set()
    for href in links:
        full = urljoin(base_url, href)
        parsed = urlparse(full)
        # Same domain only
        if parsed.netloc != base_domain:
            continue
        # Remove fragments
        clean = f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
        if parsed.query:
            clean += f"?{parsed.query}"
        if clean not in seen:
            seen.add(clean)
            resolved.append(clean)
    return resolved


def tool_fetch(args: dict) -> ToolResult:
    """Fetch a web page and return content as markdown.

    Args:
        url: URL to fetch
        depth: Recursive depth (0 = current page only, default 0)
    """
    url = args.get("url", "").strip()
    if not url:
        return ToolResult(False, error="url is required")

    # Ensure scheme
    if not url.startswith(("http://", "https://")):
        url = f"https://{url}"

    depth = min(int(args.get("depth", 0)), MAX_DEPTH)

    # Fetch main page
    content, links, error = _fetch_single(url)
    if error:
        hints = []
        if "Connection failed" in error:
            hints.append("Check the URL spelling or your network connection.")
        elif "Timeout" in error:
            hints.append("The server may be slow. Try again later.")
        elif "SSL error" in error:
            hints.append("The site may have an invalid certificate.")
        elif "HTTP 403" in error:
            hints.append("Access denied. The site may block automated requests.")
        elif "HTTP 404" in error:
            hints.append("Page not found. Check the URL.")
        elif "HTTP" in error:
            hints.append("Server error. Try again later.")

        error_msg = error
        if hints:
            error_msg += f"\nHint: {hints[0]}"
        return ToolResult(False, error=error_msg)

    pages = [{"url": url, "content": content}]

    # Recursive fetch (depth > 0)
    if depth > 0 and links:
        child_urls = _resolve_links(url, links)
        fetched = {url}
        for child_url in child_urls:
            if len(pages) >= MAX_PAGES:
                break
            if child_url in fetched:
                continue
            fetched.add(child_url)

            child_content, child_links, child_error = _fetch_single(child_url)
            if child_error:
                pages.append({"url": child_url, "content": f"[Error: {child_error}]"})
                continue
            pages.append({"url": child_url, "content": child_content})

            # Deeper recursion
            if depth > 1:
                grandchild_urls = _resolve_links(child_url, child_links)
                for gc_url in grandchild_urls:
                    if len(pages) >= MAX_PAGES:
                        break
                    if gc_url in fetched:
                        continue
                    fetched.add(gc_url)
                    gc_content, _, gc_error = _fetch_single(gc_url)
                    if gc_error:
                        pages.append({"url": gc_url, "content": f"[Error: {gc_error}]"})
                    else:
                        pages.append({"url": gc_url, "content": gc_content})

    # Build full content — no truncation here. If the result is over the
    # oversized cap, ``FetchTool.render_oversized`` persists it to a file in the
    # session dir and returns a narrow-it / fan-out nudge (same on-disk pattern
    # as read_file / shell / delegate); otherwise it enters context verbatim.
    if len(pages) == 1:
        full_content = pages[0]["content"]
    else:
        parts = []
        for p in pages:
            parts.append(f"## {p['url']}\n\n{p['content']}")
        full_content = "\n\n---\n\n".join(parts)

    header = f"[Fetched {len(pages)} page(s), {len(full_content)} chars from {url}]\n\n"
    return ToolResult(True, output=header + full_content)


class FetchTool(Tool):
    name = "fetch"
    description = (
        "Fetch a web page and return its content as markdown. "
        "Supports recursive fetching of same-domain links via the depth parameter. "
        "Full content returned inline; an over-cap result is saved to a file and "
        "replaced with a narrow-it / delegate-fan-out nudge (read it in ranges)."
    )
    # Flat-native (consolidation roadmap Step 3 completion): the schema is the
    # plain ``{url, depth?}`` shape — the ``fetch_`` wire-key prefix is gone.
    # fetch was the LAST builtin still advertising prefixed keys, which kept
    # ``claims``/``infer_action`` live for it alone and contradicted the
    # "every builtin is flat-native" invariant documented in base/registry.
    # ``wrap_single_op`` is identity; ``key_prefix`` stays at its default so
    # ``strip_prefix`` still tolerates a legacy ``fetch_url`` emission (old
    # sessions' re-fed priors) and ``claims`` is False for a flat ``{url}``.
    parameters: ClassVar[dict] = {
        "type": "object",
        "properties": {
            "url": {
                "type": "string",
                "description": "URL to fetch",
            },
            "depth": {
                "type": "integer",
                "description": "Recursive depth: 0 = current page only (default), 1+ = follow same-domain links",
            },
        },
        "required": ["url"],
    }

    def wrap_single_op(self, flat: dict) -> dict:
        return flat

    def render_oversized(self, result, args, *, body, tokens, ctx) -> str:
        """Over-cap policy for a large fetch: fetched content is ephemeral (like
        shell output), so persist it to a file in the session dir (LAZILY — only
        here, over cap) and reuse the shared on-disk nudge — (a) read a specific
        part / search the file, (b) delegate fan-out over its sections — plus a
        fetch-specific option to re-fetch a narrower URL / shallower depth
        (attack the root cause). Headless / write failure → generic fallback."""
        url = (self.strip_prefix(args).get("url") or "").strip()
        if ctx.session_dir:
            digest = hashlib.sha1(
                (url + "\x00" + body).encode("utf-8", "replace")
            ).hexdigest()[:8]
            out_path = Path(ctx.session_dir) / f"fetch-output-{digest}.txt"
            try:
                out_path.write_text(body, encoding="utf-8")
            except OSError:
                pass
            else:
                path = str(out_path)
                return on_disk_oversized_nudge(
                    "fetch",
                    "fetched content",
                    f"full content saved to '{path}'",
                    path,
                    tokens,
                    ctx.oversized_cap,
                    ctx.tools_available,
                    nlines=body.count("\n") + 1,
                    part_extra=f"search it (read_file path='{path}', search='…')",
                    tail_bullets=(
                        (
                            "Or re-fetch a more specific URL / shallower depth "
                            "so the result is smaller."
                        ),
                    ),
                )
        return default_oversized_nudge("fetch", tokens, ctx.oversized_cap)

    def _run(self, args: dict, *, ctx=None) -> ToolResult:
        return tool_fetch(args)
