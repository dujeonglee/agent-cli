"""Fetch tool — retrieve web page content as markdown (HTML) or as text (JSON,
XML, plain text)."""

from __future__ import annotations

import base64
import binascii
import json
import re
from html.parser import HTMLParser
from typing import ClassVar
from urllib.parse import parse_qs, unquote, urljoin, urlparse

import requests

from agent_cli.tools.base import Tool
from agent_cli.tools.result import ToolResult

# ── Configuration ──────────────────────────────
FETCH_TIMEOUT = 15
MAX_DEPTH = 3
MAX_PAGES = 10  # Total pages fetched in recursive mode
USER_AGENT = "agent-cli/2.0 (fetch tool)"


def _body_kind(content_type: str) -> str | None:
    """How to read a response — ``html`` / ``json`` / ``text``, or None (refuse).

    v10.52.0: until then only ``text/html`` and ``text/plain`` were read, so every
    machine endpoint that needs no browser — PyPI, npm, Wikipedia REST, OSV,
    GitHub API, Stack Exchange — came back "Unsupported content type" and the
    model fell back to ``shell curl``. XML (arXiv Atom, RSS) is readable as text.
    Binary (PDF, images, archives) stays refused."""
    ct = content_type.split(";", 1)[0].strip().lower()
    if ct in ("text/html", "application/xhtml+xml"):
        return "html"
    if ct == "application/json" or ct.endswith("+json"):
        return "json"
    if ct in ("application/xml", "text/xml") or ct.endswith("+xml"):
        return "text"
    if ct.startswith("text/"):
        return "text"
    return None


def _decode(resp, content_type: str, kind: str) -> str:
    """Bytes → text, choosing the charset ourselves.

    ``requests`` follows RFC 2616 and picks ISO-8859-1 for a ``text/*`` response
    without a ``charset`` — a UTF-8 page then reads ``â\x80\x94`` for "—"
    (docs.python.org, measured 2026-10-18; Korean pages too). Order: header
    ``charset`` → HTML ``<meta charset>`` → UTF-8 if it decodes cleanly →
    the detected encoding. JSON is UTF-8 by spec (RFC 8259) and lands on the
    clean-UTF-8 step."""
    raw = resp.content or b""
    m = re.search(r"charset=[\"']?([\w.:-]+)", content_type, re.IGNORECASE)
    enc = m.group(1) if m else None
    if enc is None and kind == "html":
        mm = re.search(
            rb"<meta[^>]+charset=[\"']?([\w.:-]+)", raw[:4096], re.IGNORECASE
        )
        if mm:
            enc = mm.group(1).decode("ascii", "ignore")
    if enc is None:
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError:
            enc = getattr(resp, "apparent_encoding", None) or "utf-8"
    try:
        return raw.decode(enc, errors="replace")
    except LookupError:  # an unknown charset name in the header / meta
        return raw.decode("utf-8", errors="replace")


#: 내용이 아닌 요소 — 통째로 건너뛴다.
_SKIP_TAGS = frozenset(
    ("script", "style", "nav", "footer", "header", "noscript", "template")
)
#: 닫는 태그가 없는 요소 — 건너뛰기 깊이를 세지 않는다.
_VOID_TAGS = frozenset(
    ("area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta")
    + ("param", "source", "track", "wbr")
)
#: 닫는 태그를 생략할 수 있는 요소 — 숨김이어도 건너뛰지 않는다. 닫히지 않은
#: ``<p hidden>`` 을 건너뛰기 시작하면 페이지 끝까지 사라진다.
_OPTIONAL_END = frozenset(
    ("p", "li", "dt", "dd", "tr", "td", "th", "option", "thead", "tbody", "tfoot")
)
#: 경계에서 줄을 바꾸는 블록 요소 (v10.57.2). 종전엔 ``p``·``li``·제목·표만
#: 바꿔 Scholar 의 ``<div>`` 제목과 저자가 ``…networksS Jung`` 으로 붙었다.
_BLOCK_TAGS = frozenset(
    ("div", "section", "article", "main", "aside", "ul", "ol", "dl", "dt", "dd")
    + ("blockquote", "figure", "figcaption", "form", "fieldset", "details")
    + ("summary", "table", "address", "hr")
)
_WS = re.compile(r"[ \t\r\n\f]+")  # HTML 의 공백 — nbsp 는 접지 않는다
_EMPTY_MARK = re.compile(r"[-*]|#{1,6}")  # 글 없는 목록·제목 표시만 남은 줄


def _hidden(attrs: dict) -> bool:
    """화면에 그려지지 않는 요소 — ``hidden``·``aria-hidden``·인라인 ``display:none``.

    Scholar 는 정상 결과 위에 ``<div style="display:none">현재 시스템이 작동되지
    않습니다…</div>`` 를 늘 싣는다 — 읽으면 막힌 페이지로 오해한다."""
    if "hidden" in attrs:
        return True
    if (attrs.get("aria-hidden") or "").strip().lower() == "true":
        return True
    style = "".join((attrs.get("style") or "").lower().split())
    return "display:none" in style or "visibility:hidden" in style


def _unwrap_redirect(url: str) -> str:
    """검색 엔진의 추적 주소 → 실제 주소 (v10.57.2). 모르는 모양은 그대로.

    Bing 결과 링크는 ``/ck/a?…&u=a1<base64>`` 300자 안팎(실측) — 그대로 실으면
    결과 하나에 100토큰 넘게 들고, 따라가도 JS 리다이렉트 페이지다."""
    parsed = urlparse(url)
    host, path = parsed.netloc.lower(), parsed.path
    q = parse_qs(parsed.query)
    if host.endswith("bing.com") and path == "/ck/a" and q.get("u"):
        u = q["u"][0]
        if u.startswith("a1"):
            b = u[2:]
            try:
                out = base64.urlsafe_b64decode(b + "=" * (-len(b) % 4)).decode()
            except (binascii.Error, ValueError):
                return url
            if out.startswith(("http://", "https://")):
                return out
    if "google." in host and path == "/url":
        for key in ("q", "url"):
            if q.get(key, [""])[0].startswith(("http://", "https://")):
                return q[key][0]
    if "duckduckgo.com" in host and path.startswith("/l/") and q.get("uddg"):
        return q["uddg"][0]
    return url


def _url_repeats_text(url: str, text: str) -> bool:
    """주소가 링크 글 이상을 말하지 않는다 — 글이 곧 주소, 또는 **같은 사이트**
    (``/경로``) 이고 경로의 마지막 조각이 글과 같다(``/wiki/Shor%27s_algorithm`` ↔
    "Shor's algorithm"). 커밋 로그처럼 주소에만 있는 정보(해시)가 있으면 싣는다."""
    if text in (url, url.rstrip("/")):
        return True
    if not url.startswith("/"):
        return False
    path = urlparse(url).path.rstrip("/")
    last = unquote(path.rsplit("/", 1)[-1]).replace("_", " ").replace("-", " ")
    return last.casefold() == " ".join(text.replace("-", " ").split()).casefold()


class _HTMLToMarkdown(HTMLParser):
    """Minimal HTML to markdown converter.

    v10.57.2 (x8jkpw 기록 37건 점검): 링크를 ``[글](주소)`` 로 싣고(같은 사이트는
    ``/경로``), 블록 경계에서 줄을 바꾸고, 숨김 요소를 건너뛰고, 공백을 HTML 처럼
    접는다. 종전엔 링크가 본문에 없어 검색 결과를 따라갈 수 없었고, 탭만 있는 줄과
    빈 ``- `` 줄이 HTML 결과의 40~70% 였다(Wikipedia 본문이 266번째 줄부터)."""

    def __init__(self, base_url: str = ""):
        super().__init__()
        self.result: list[str] = []
        self._base = base_url
        self._host = urlparse(base_url).netloc
        #: 건너뛰는 요소와 그 안에 다시 열린 같은 이름의 수 — 종전의 불 값은
        #: ``<nav>`` 안의 ``<script>`` 가 닫힐 때 건너뛰기를 끝냈다.
        self._skip_tag: str | None = None
        self._skip_depth = 0
        self._in_pre = False
        self._in_code = False
        self._links: list[str] = []
        self._anchor: tuple[int, str] | None = None  # (글 시작 위치, 실을 주소)
        # 표 (v10.56.4): 행마다 한 줄, 글 있는 칸 사이만 " | ". 칸마다 [시작
        # 위치, 안에서 닫힌 행 수], 행마다 채워진 칸 수를 스택으로 — 표 안의 표도
        # 같은 규칙.
        self._cells: list[list[int]] = []
        self._rows: list[int] = []

    def handle_starttag(self, tag, attrs):
        if self._skip_tag:
            if tag == self._skip_tag and tag not in _VOID_TAGS:
                self._skip_depth += 1
            return
        attrs_dict = dict(attrs)
        if tag in _SKIP_TAGS or (
            tag not in _VOID_TAGS and tag not in _OPTIONAL_END and _hidden(attrs_dict)
        ):
            if tag not in _VOID_TAGS:
                self._skip_tag, self._skip_depth = tag, 1
            return
        if tag in _BLOCK_TAGS:
            self._break()
        if tag == "img":
            # 그림의 대체 글 — Wikipedia 수식은 보이는 것이 ``<img alt="{\\displaystyle
            # …}">`` 이고 MathML 은 숨김이다. 숨김을 건너뛰면 alt 가 유일한 수식.
            alt = " ".join((attrs_dict.get("alt") or "").split())
            if alt:
                self.result.append(alt)
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
        elif tag == "tr":
            self._rows.append(0)
        elif tag in ("td", "th"):
            self._cells.append([len(self.result), 0])
        elif tag == "a":
            href = (attrs_dict.get("href") or "").strip()
            if href and not href.startswith(("#", "javascript:")):
                self._links.append(href)
                if not self._in_pre:
                    self._anchor = (len(self.result), self._shown_url(href))

    def _break(self) -> None:
        """블록 경계 — 이미 줄 첫머리면 그대로(이웃한 ``div`` 가 빈 줄을 쌓지 않게)."""
        for chunk in reversed(self.result):
            text = chunk.rstrip(" ")
            if text:
                if not text.endswith("\n"):
                    self.result.append("\n")
                return

    def _shown_url(self, href: str) -> str:
        """본문에 실을 주소 — 추적 주소는 풀고, 같은 사이트는 ``/경로`` 만."""
        full = _unwrap_redirect(urljoin(self._base, href) if self._base else href)
        p = urlparse(full)
        if self._host and p.netloc == self._host and p.scheme in ("http", "https"):
            return full[len(f"{p.scheme}://{p.netloc}") :] or "/"
        return full

    def handle_endtag(self, tag):
        if self._skip_tag:
            if tag == self._skip_tag:
                self._skip_depth -= 1
                if self._skip_depth == 0:
                    self._skip_tag = None
            return
        if tag in _BLOCK_TAGS:
            self._break()
        if tag == "pre":
            self._in_pre = False
            self.result.append("\n```\n")
        elif tag == "code" and not self._in_pre:
            self._in_code = False
            self.result.append("`")
        elif tag == "a" and self._anchor:
            start, url = self._anchor
            self._anchor = None
            text = " ".join("".join(self.result[start:]).split())
            # 글이 없는 링크(아이콘)·주소가 글을 되풀이할 뿐인 링크는 주소를
            # 싣지 않는다 — Wikipedia 본문 링크 960개를 다 실으면 토큰이 1.7배.
            if (
                text
                and "\n" not in "".join(self.result[start:]).strip()
                and not _url_repeats_text(url, text)
            ):
                del self.result[start:]
                self.result.append(f"[{text}]({url})")
        elif tag in ("td", "th") and self._cells:
            start, inner_rows = self._cells.pop()
            body = "".join(self.result[start:])
            del self.result[start:]
            # 빈 칸은 지운다 — 코드 보기의 줄 번호 칸(번호는 속성에만). 칸 안의
            # 줄바꿈은 접는다 — 남기면 한 행이 여러 줄로 갈라진다(Wikipedia 표).
            # 칸 안에 표가 있으면 그 표의 행은 줄로 남긴다.
            if inner_rows:
                text = "\n".join(ln.strip() for ln in body.split("\n") if ln.strip())
            else:
                text = " ".join(body.split())
            if text and self._rows:
                if self._rows[-1]:
                    self.result.append(" | ")
                self.result.append(text)
                self._rows[-1] += 1
            elif text:
                self.result.append(text)
        elif tag == "tr" and self._rows:
            self._rows.pop()
            self.result.append("\n")
            if self._cells:  # 열린 칸 안에서 닫힌 행 — 칸 안의 표
                self._cells[-1][1] += 1

    def handle_data(self, data):
        if self._skip_tag:
            return
        if self._in_pre:
            self.result.append(data)
            return
        data = _WS.sub(" ", data)
        if data == " " and len(self._rows) > len(self._cells):
            return  # 행 안·칸 밖의 공백(태그 사이 줄바꿈) — 칸 사이가 "  | " 가 된다
        self.result.append(data)

    def get_markdown(self) -> str:
        """줄 정리 — 코드 블록 밖에서 줄 앞뒤 공백을 걷고, 글 없는 줄(빈 ``-``·
        ``#``)을 빈 줄로, 빈 줄은 하나로."""
        out: list[str] = []
        fenced = False
        for line in "".join(self.result).split("\n"):
            if line.strip() == "```":
                fenced = not fenced
                out.append("```")
                continue
            if fenced:
                out.append(line)
                continue
            line = line.strip()
            if _EMPTY_MARK.fullmatch(line):
                line = ""
            if line or (out and out[-1]):
                out.append(line)
        return "\n".join(out).strip()

    def get_links(self) -> list[str]:
        return self._links


#: 봇 확인 페이지의 흔적. **보이는 글**에서 찾는 문구와 **HTML** 에서 찾는 확인
#: 위젯 이름을 나눈다(v10.56.4) — 종전엔 원본 HTML 전체에서 ``captcha`` 를 찾아
#: GitHub blob 페이지의 기능 플래그 ``octocaptcha_origin_optimization`` 에 걸려
#: 진짜 README 를 막힘으로 돌려줬다. 둘 다 본문이 짧을 때만 본다 — CAPTCHA 를
#: 다루는 긴 글(위키 문서 등)을 막힘으로 오인하지 않게.
_BOT_TEXT_MARKERS = (
    "captcha",
    "unusual traffic",
    "made by a human",
    "verify you are human",
    "are you a robot",
)
_BOT_HTML_MARKERS = (
    "cf-chl",
    "challenge-platform",
    "anomaly-modal",
    "g-recaptcha",
    "h-captcha",
)
_BOT_CHECK_MAX_TEXT = 3000  # 확인 페이지는 짧다(DuckDuckGo 실측 ~1K, 공백 접은 글자)
_JS_SHELL_MIN_HTML = 10_000  # HTML 은 큰데
_JS_SHELL_MAX_TEXT = (
    200  # 글이 거의 없다 — 브라우저가 그리는 페이지(Google 검색 실측 89자)
)


def _disguised_block(status: int, html: str, text: str) -> str | None:
    """성공 코드인데 내용이 아닌 응답 — 실패 문구, 아니면 None (v10.56.3).

    4xx 는 코드가 말해 주지만(모델이 뜻을 안다) 이 둘은 200·202 로 와서 그동안
    **성공**으로 넘어갔다: 모델이 확인 페이지 글자나 빈 껍데기를 내용으로 읽었다.
    대안은 적지 않는다 — 어디로 갈지는 정보원 목록의 몫이다."""
    flat = " ".join(text.split())
    if len(flat) <= _BOT_CHECK_MAX_TEXT:
        seen, low = flat.lower(), html.lower()
        hit = next((m for m in _BOT_TEXT_MARKERS if m in seen), None) or next(
            (m for m in _BOT_HTML_MARKERS if m in low), None
        )
        if hit:
            return (
                f"Bot check: {{url}} returned a bot-check page, not the content "
                f'(HTTP {status}, "{hit}")'
            )
    if len(html) >= _JS_SHELL_MIN_HTML and len(flat) <= _JS_SHELL_MAX_TEXT:
        return (
            "Needs JavaScript: {url} renders in a browser; the fetched HTML "
            f"({len(html):,} chars) has {len(flat)} chars of text"
        )
    return None


def _gitiles_blob(text: str, url: str) -> tuple[str, list[str], str | None]:
    """gitiles ``?format=TEXT`` — 파일 원문이 base64 로 온다 (v10.56.4).

    gitiles 에는 raw 주소가 없고 이것이 원문을 받는 공식 길이다. 판정은 URL
    모양이 아니라 서버가 단 ``x-gitiles-object-type: blob`` 헤더(android·
    chromium·kernel googlesource 공통). base64 가 아니면 원문 그대로, 풀린
    내용이 텍스트가 아니면(바이너리 파일) 실패 — 깨진 글자를 넘기지 않는다."""
    try:
        raw = base64.b64decode("".join(text.split()), validate=True)
    except (binascii.Error, ValueError):
        return text, [], None
    try:
        return raw.decode("utf-8"), [], None
    except UnicodeDecodeError:
        return "", [], f"Unsupported content: {url} is a binary file (gitiles blob)"


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
    kind = _body_kind(content_type)
    if kind is None:
        return "", [], f"Unsupported content type: {content_type} for {url}"

    text = _decode(resp, content_type, kind)
    if kind == "json":
        body = text
        if body.startswith(")]}'"):  # XSSI 접두(Gerrit·gitiles) — 첫 줄을 버린다
            body = body.split("\n", 1)[1] if "\n" in body else ""
        try:  # one value per line, non-ASCII kept — readable and still valid JSON
            text = json.dumps(json.loads(body), indent=1, ensure_ascii=False)
        except ValueError:
            pass  # not actually JSON — hand it over as text
        return text, [], None
    if kind == "text":
        if (resp.headers.get("x-gitiles-object-type") or "") == "blob":
            return _gitiles_blob(text, url)
        return text, [], None

    # 본문은 자르지 않는다(v10.56.2). 종전엔 페이지마다 20만 자에서 **표시 없이**
    # 잘랐다 — 줄바꿈한 JSON 은 중간에서 끊겨 깨진 채로(PyPI `requests` 193KB),
    # HTML 은 **변환 전** 원본에서 잘려 본문 뒷부분이 사라졌다(Wikipedia 1MB →
    # 앞 1/5). 큰 결과는 공용 `Tool.render_oversized` 가 전체를 파일로 남기고
    # 발췌를 돌려준다 — 잘린 것은 그 파일까지 잘려 있었다.
    final = getattr(resp, "url", None)
    parser = _HTMLToMarkdown(final if isinstance(final, str) and final else url)
    try:
        parser.feed(text)
    except Exception:
        return text, [], None

    markdown = parser.get_markdown()
    blocked = _disguised_block(resp.status_code, text, markdown)
    if blocked:
        return "", [], blocked.format(url=url)
    return markdown, parser.get_links(), None


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
    # oversized cap, the shared ``Tool.render_oversized`` policy persists it to
    # a file and returns an excerpt + how to mine it (identical for every tool);
    # otherwise it enters context verbatim.
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
        "Fetch a URL: an HTML page comes back as markdown with links as [text](url) "
        "(same-site links as /path), JSON (pretty-printed), "
        "XML and plain text as text. "
        "Supports recursive fetching of same-domain links via the depth parameter. "
        "Full content returned inline; an over-cap result is saved to a file and "
        "replaced with an excerpt + how to mine it (read it in ranges)."
    )
    oversized_retry_hint = (
        "re-fetch a more specific URL, or a shallower depth, so less is pulled."
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
                "minLength": 1,
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

    def _run(self, args: dict, *, ctx=None) -> ToolResult:
        return tool_fetch(args)
