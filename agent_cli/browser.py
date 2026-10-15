"""헤드리스 브라우저 세션 — ``browser`` 도구의 엔진 (docs/browser/DESIGN.md).

페이지를 **접근성 트리 텍스트**로 모델에게 보여 주고, 요소마다 붙인
``[ref=eN]`` 번호로 조작한다. Playwright 의 **공개 API 만** 쓴다 —
``aria_snapshot()`` 으로 트리를 받고, ref 는 ``(role, name, nth)`` 로
``get_by_role(...).nth(n)`` 에 대응시킨다 (MCP 서버의 내부 ``snapshotForAI``
는 쓰지 않는다; 그 서버도 결국 ref 를 같은 로케이터로 바꿔 실행했다).

- 세션당 브라우저 하나. Playwright sync API 는 만든 스레드에서만 쓸 수
  있으므로 **전용 스레드 + 요청 큐** — 모든 호출(서브에이전트 포함)이
  큐를 지나 직렬이 된다.
- 첫 호출 때 띄운다(안 쓰는 세션은 비용 0). ``teardown_session`` 과
  ``atexit`` 가 닫는다.
- ref 는 **문서(페이지 로드) 단위**로 산다: 번호는 세션 내내 단조 증가라
  (스냅샷을 다시 찍어도, 페이지가 바뀌어도) 옛 번호의 뜻이 바뀌지 않고,
  ``open``·``back``·이동을 일으킨 조작 뒤엔 ``generation`` 이 올라 옛 ref 는
  "stale" 로 거부된다. 같은 문서 안에서는 DOM 이 바뀌어도 그 요소가 남아
  있는 한 role/name 로케이터가 계속 맞는다.
- 코드 실행 모드 없음, ``http(s)`` 만, 허용 호스트 목록(비면 전부),
  대화상자 자동 닫기, 다운로드 거부, 프로필은 메모리 전용.

순수 함수(``parse_aria``·``assign_refs``·``render_nodes``·``host_allowed``)는
Playwright 없이 단위 테스트한다; 엔진은 ``BrowserSession`` 이 감싼다.
"""

from __future__ import annotations

import atexit
import importlib.util
import queue
import re
import threading
from concurrent.futures import Future
from dataclasses import dataclass, field
from urllib.parse import urlparse

from agent_cli.constants import browser_knobs

#: 스냅샷에서 텍스트 노드·이름을 자르는 길이 — 긴 본문은 ``text`` 모드로.
TEXT_CAP = 80
#: 조작 가능한 role — ref 를 붙이고 click/type/select 를 허용한다.
INTERACTIVE_ROLES = frozenset(
    {
        "button",
        "link",
        "textbox",
        "searchbox",
        "checkbox",
        "radio",
        "combobox",
        "listbox",
        "option",
        "menuitem",
        "menuitemcheckbox",
        "menuitemradio",
        "tab",
        "switch",
        "slider",
        "spinbutton",
    }
)
#: 범위 지정(``snapshot(ref=)``·``text(ref=)``)과 ``find`` 의 위치 표시용 ref.
LANDMARK_ROLES = frozenset(
    {
        "main",
        "navigation",
        "form",
        "region",
        "article",
        "complementary",
        "contentinfo",
        "banner",
        "search",
        "heading",
        "table",
        "list",
        "dialog",
    }
)
NAVIGATION_TIMEOUT_MS = 30_000
ACTION_TIMEOUT_MS = 10_000
#: 큐 대기 상한 — Playwright 자체 타임아웃보다 넉넉히.
CALL_TIMEOUT_S = 90.0

_LINE_RE = re.compile(r"^(?P<indent>\s*)- (?P<body>.*)$")
_NODE_RE = re.compile(
    r'^(?P<role>[a-z]+)(?: "(?P<name>(?:[^"\\]|\\.)*)")?'
    r"(?P<attrs>(?: \[[^\]]*\])*)(?::\s*(?P<text>.*))?$"
)
_REF_RE = re.compile(r"^e\d+$")


class BrowserError(Exception):
    """모델에게 그대로 보여 줄 실패 — 메시지가 곧 관찰."""


@dataclass
class Node:
    """``aria_snapshot`` 의 한 줄. ``text`` 는 ``role: text`` 꼴의 인라인 글."""

    depth: int
    role: str
    name: str = ""
    attrs: str = ""
    text: str = ""
    prop: str = ""  # ``/url: …`` 같은 속성 줄 (role 없음)
    ref: str = ""


@dataclass
class _RefTarget:
    role: str
    name: str
    nth: int
    generation: int
    scope: str = ""  # 이 스냅샷이 찍힌 범위의 ref ("" = 페이지)


# ── 순수 함수 (Playwright 불필요) ──────────────────────────────────────


def host_allowed(url: str, hosts: tuple[str, ...]) -> str | None:
    """``url`` 을 열 수 있으면 None, 아니면 거절 사유."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return f"only http(s) URLs can be opened (got {parsed.scheme or 'no'} scheme)"
    host = (parsed.hostname or "").lower()
    if not host:
        return "the URL has no host"
    if hosts and not any(host == h or host.endswith("." + h) for h in hosts):
        return f"host '{host}' is not in AGENT_CLI_BROWSER_HOSTS ({', '.join(hosts)})"
    return None


def parse_aria(text: str) -> list[Node]:
    """``aria_snapshot()`` 텍스트 → 노드 목록 (들여쓰기 2칸 = 깊이 1)."""
    nodes: list[Node] = []
    for raw in text.splitlines():
        m = _LINE_RE.match(raw)
        if not m:
            continue
        depth = len(m.group("indent")) // 2
        body = m.group("body").strip()
        if body.startswith("/"):
            nodes.append(Node(depth=depth, role="", prop=body))
            continue
        nm = _NODE_RE.match(body)
        if not nm:
            nodes.append(Node(depth=depth, role="text", text=body))
            continue
        name = (nm.group("name") or "").replace('\\"', '"').replace("\\\\", "\\")
        nodes.append(
            Node(
                depth=depth,
                role=nm.group("role"),
                name=name,
                attrs=(nm.group("attrs") or "").strip(),
                text=(nm.group("text") or "").strip(),
            )
        )
    return nodes


def assign_refs(
    nodes: list[Node], *, start: int, generation: int, scope: str = ""
) -> dict[str, _RefTarget]:
    """조작·범위 role 에 ``eN`` 을 붙이고 ref → 로케이터 재료를 돌려준다.
    ``nth`` 는 같은 (role, name) 안에서의 순서 — DOM 순서라 ``get_by_role().nth``
    와 맞는다."""
    seen: dict[tuple[str, str], int] = {}
    out: dict[str, _RefTarget] = {}
    n = start
    for node in nodes:
        if node.role not in INTERACTIVE_ROLES and node.role not in LANDMARK_ROLES:
            continue
        key = (node.role, node.name)
        nth = seen.get(key, 0)
        seen[key] = nth + 1
        node.ref = f"e{n}"
        out[node.ref] = _RefTarget(node.role, node.name, nth, generation, scope)
        n += 1
    return out


def _cut(s: str) -> str:
    if len(s) <= TEXT_CAP:
        return s
    return f"{s[:TEXT_CAP]}…(+{len(s) - TEXT_CAP} chars)"


def render_node(node: Node, *, base_depth: int = 0) -> str:
    pad = "  " * max(0, node.depth - base_depth)
    if node.prop:
        return f"{pad}- {_cut(node.prop)}"
    line = f"{pad}- {node.role}"
    if node.name:
        line += f' "{_cut(node.name)}"'
    if node.attrs:
        line += f" {node.attrs}"
    if node.ref:
        line += f" [ref={node.ref}]"
    if node.text:
        line += f": {_cut(node.text)}"
    return line


def render_nodes(nodes: list[Node], *, depth: int | None = None) -> str:
    base = nodes[0].depth if nodes else 0
    lines = []
    for node in nodes:
        if depth is not None and node.depth - base > depth:
            continue
        lines.append(render_node(node, base_depth=base))
    return "\n".join(lines)


def find_nodes(nodes: list[Node], *, text: str = "", regex: str = "") -> list[str]:
    """``text``(대소문자 무시 부분일치) 또는 ``regex`` 에 맞는 노드를 조상
    경로와 함께 — ``main > list > listitem > link "X" [ref=e5]``."""
    pat = re.compile(regex, re.IGNORECASE) if regex else None
    needle = text.lower()
    stack: list[Node] = []
    out: list[str] = []
    for node in nodes:
        while stack and stack[-1].depth >= node.depth:
            stack.pop()
        # 스냅샷에 보이는 모양 그대로 맞춘다 (`link "Aurora Lamp"`) — ref 만 뺀다.
        hay = render_node(
            Node(0, node.role, node.name, node.attrs, node.text, node.prop)
        )[2:]
        hit = pat.search(hay) if pat else (needle and needle in hay.lower())
        if hit:
            path = [n.role + (f' "{_cut(n.name)}"' if n.name else "") for n in stack]
            path.append(render_node(node, base_depth=node.depth).lstrip("- "))
            out.append(" > ".join(path))
        if not node.prop:
            stack.append(node)
    return out


def browser_available() -> bool:
    """``Tool.requires_handler="browser"`` 의 판정 — 켜져 있고 playwright 가 있다.
    브라우저를 띄울 수 있는지는 여기서 보지 않는다(첫 호출이 사유와 함께 알린다)."""
    return (
        browser_knobs()["enabled"]
        and importlib.util.find_spec("playwright") is not None
    )


# ── 엔진 ─────────────────────────────────────────────────────────────────


def _first_line(exc: BaseException) -> str:
    return (str(exc).strip().splitlines() or [type(exc).__name__])[0]


@dataclass
class PageState:
    url: str = ""
    title: str = ""


@dataclass
class BrowserSession:
    """세션 하나의 브라우저. 모든 공개 메서드는 전용 스레드에서 실행된다."""

    knobs: dict = field(default_factory=browser_knobs)
    _q: queue.Queue = field(default_factory=queue.Queue, repr=False)
    _thread: threading.Thread | None = field(default=None, repr=False)
    _pw: object = field(default=None, repr=False)
    _browser: object = field(default=None, repr=False)
    _page: object = field(default=None, repr=False)
    engine: str = ""
    generation: int = 0
    _next_ref: int = 1
    _refs: dict[str, _RefTarget] = field(default_factory=dict, repr=False)
    _dialogs: list[str] = field(default_factory=list, repr=False)
    closed: bool = False

    # ── 스레드 ──
    def _call(self, fn):
        if self.closed:
            raise BrowserError("browser is closed — call mode 'open' to start again")
        if self._thread is None or not self._thread.is_alive():
            self._thread = threading.Thread(
                target=self._loop, name="agent-cli-browser", daemon=True
            )
            self._thread.start()
        fut: Future = Future()
        self._q.put((fn, fut))
        try:
            return fut.result(timeout=CALL_TIMEOUT_S)
        except TimeoutError as e:
            raise BrowserError("browser call timed out") from e

    def _loop(self):
        while True:
            fn, fut = self._q.get()
            if fn is None:
                fut.set_result(None)
                return
            try:
                fut.set_result(fn())
            except BaseException as e:  # 호출자에게 그대로 전달
                fut.set_exception(e)

    # ── 실행 ──
    def _ensure_page(self):
        if self._page is not None:
            return self._page
        from playwright.sync_api import sync_playwright

        pw = sync_playwright().start()
        tried = []
        candidates: list[tuple[str, dict]] = []
        if self.knobs.get("executable"):
            candidates.append(
                ("executable", {"executable_path": self.knobs["executable"]})
            )
        candidates += [("chrome", {"channel": "chrome"}), ("chromium", {})]
        browser = None
        for label, kw in candidates:
            try:
                browser = pw.chromium.launch(headless=True, **kw)
                self.engine = label
                break
            except Exception as e:
                tried.append(f"{label}: {_first_line(e)}")
        if browser is None:
            pw.stop()
            raise BrowserError(
                "no browser could be launched — "
                + "; ".join(tried)
                + ". Install one with `playwright install chromium` (or set "
                "AGENT_CLI_BROWSER_EXECUTABLE to an existing Chrome/Chromium)."
            )
        context = browser.new_context(accept_downloads=False)
        page = context.new_page()
        page.set_default_timeout(ACTION_TIMEOUT_MS)
        page.set_default_navigation_timeout(NAVIGATION_TIMEOUT_MS)
        page.on("dialog", self._on_dialog)
        self._pw, self._browser, self._page = pw, browser, page
        atexit.register(self.close)
        return page

    def _on_dialog(self, dialog):
        self._dialogs.append(f"{dialog.type}: {dialog.message}"[:200])
        try:
            dialog.dismiss()
        except Exception:
            pass

    def _state(self) -> PageState:
        page = self._page
        if page is None:
            return PageState()
        try:
            return PageState(url=page.url, title=page.title())
        except Exception:
            return PageState(url=page.url)

    def _new_document(self):
        """새 문서 — 옛 ref 는 남겨 두고 세대만 올린다. 번호는 세션 내내 단조
        증가라 옛 번호가 새 요소의 뜻으로 바뀌는 일이 없다(옛 번호 → stale)."""
        self.generation += 1

    def _check_host(self, url: str):
        why = host_allowed(url, self.knobs.get("hosts", ()))
        if why:
            raise BrowserError(f"refused: {why}")

    def _enforce_host_after(self, page):
        """이동·클릭이 허용 밖 호스트로 끝났으면 빈 페이지로 돌리고 거절."""
        why = host_allowed(page.url, self.knobs.get("hosts", ()))
        if why and page.url != "about:blank":
            page.goto("about:blank")
            self._new_document()
            raise BrowserError(f"left the page: {why}")

    def _header(self, state: PageState) -> str:
        head = (
            f"[page] {state.url} — {state.title}"
            if state.title
            else f"[page] {state.url}"
        )
        if self._dialogs:
            head += "\n[dialog dismissed] " + " | ".join(self._dialogs)
            self._dialogs = []
        return head

    def _locator(self, ref: str):
        if not _REF_RE.match(ref or ""):
            raise BrowserError(f"ref must look like e12 (got {ref!r})")
        target = self._refs.get(ref)
        if target is None:
            raise BrowserError(
                f"unknown ref {ref} — take a snapshot and use a ref from it"
            )
        if target.generation != self.generation:
            raise BrowserError(
                f"stale ref {ref} (the page has changed) — take a new snapshot"
            )
        root = self._locator(target.scope) if target.scope else self._page
        if target.name:
            loc = root.get_by_role(target.role, name=target.name, exact=True)
        else:
            loc = root.get_by_role(target.role)
        return loc.nth(target.nth)

    def _snapshot_text(self, ref: str = "", depth: int | None = None) -> str:
        page = self._page
        root = self._locator(ref) if ref else page.locator("body")
        nodes = parse_aria(root.aria_snapshot())
        refs = assign_refs(
            nodes, start=self._next_ref, generation=self.generation, scope=ref
        )
        self._refs.update(refs)
        self._next_ref += len(refs)
        body = render_nodes(nodes, depth=depth)
        return body or "(empty)"

    def _after_action(self, page, before: PageState, what: str) -> str:
        try:
            page.wait_for_load_state("domcontentloaded", timeout=ACTION_TIMEOUT_MS)
        except Exception:
            pass
        self._enforce_host_after(page)
        after = self._state()
        if after.url != before.url:
            self._new_document()
            return (
                f"{what} — page changed\n{self._header(after)}\n{self._snapshot_text()}"
            )
        return (
            f"{what} — same page ({after.url}); take a snapshot to see what changed"
            + ("\n" + self._header(after) if self._dialogs else "")
        )

    # ── 모드 ──
    def open(self, url: str) -> str:
        self._check_host(url)

        def go():
            page = self._ensure_page()
            page.goto(url, wait_until="domcontentloaded")
            self._enforce_host_after(page)
            self._new_document()
            return f"{self._header(self._state())}\n{self._snapshot_text()}"

        return self._call(go)

    def back(self) -> str:
        def go():
            page = self._need_page()
            page.go_back(wait_until="domcontentloaded")
            self._enforce_host_after(page)
            self._new_document()
            return f"{self._header(self._state())}\n{self._snapshot_text()}"

        return self._call(go)

    def snapshot(self, ref: str = "", depth: int | None = None) -> str:
        return self._call(
            lambda: (
                f"{self._header(self._state())}\n{self._snapshot_text(ref, depth)}"
                if self._need_page()
                else ""
            )
        )

    def find(self, *, text: str = "", regex: str = "") -> str:
        def go():
            page = self._need_page()
            nodes = parse_aria(page.locator("body").aria_snapshot())
            refs = assign_refs(nodes, start=self._next_ref, generation=self.generation)
            self._refs.update(refs)
            self._next_ref += len(refs)
            hits = find_nodes(nodes, text=text, regex=regex)
            if not hits:
                return f"no match for {regex or text!r} on {page.url}"
            return f"{len(hits)} match(es) on {page.url}:\n" + "\n".join(
                f"- {h}" for h in hits
            )

        return self._call(go)

    def text(self, ref: str = "") -> str:
        def go():
            page = self._need_page()
            loc = self._locator(ref) if ref else page.locator("body")
            raw = loc.inner_text()
            lines = [ln.rstrip() for ln in raw.splitlines()]
            body = re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()
            return f"[text of {page.url}{' ' + ref if ref else ''} — {len(body)} chars]\n{body}"

        return self._call(go)

    def click(self, ref: str) -> str:
        def go():
            page = self._need_page()
            before = self._state()
            target = self._interactive(ref)
            self._locator(ref).click()
            return self._after_action(page, before, f"clicked {target}")

        return self._call(go)

    def type(self, ref: str, text: str, submit: bool = False) -> str:
        def go():
            page = self._need_page()
            before = self._state()
            target = self._interactive(ref)
            loc = self._locator(ref)
            loc.fill(text)
            what = f"typed {text!r} into {target}"
            if submit:
                loc.press("Enter")
                what += " and pressed Enter"
            return self._after_action(page, before, what)

        return self._call(go)

    def select(self, ref: str, value: str) -> str:
        def go():
            page = self._need_page()
            before = self._state()
            target = self._interactive(ref)
            self._locator(ref).select_option(value)
            return self._after_action(page, before, f"selected {value!r} in {target}")

        return self._call(go)

    def press(self, key: str) -> str:
        def go():
            page = self._need_page()
            before = self._state()
            page.keyboard.press(key)
            return self._after_action(page, before, f"pressed {key}")

        return self._call(go)

    def close(self) -> str:
        if self.closed:
            return "browser already closed"
        self.closed = True
        if self._thread is None or not self._thread.is_alive():
            return "browser closed"

        def go():
            for obj in (self._browser, self._pw):
                try:
                    if obj is not None:
                        obj.close() if obj is self._browser else obj.stop()
                except Exception:
                    pass
            self._page = self._browser = self._pw = None

        fut: Future = Future()
        self._q.put((go, fut))
        stop: Future = Future()
        self._q.put((None, stop))
        try:
            fut.result(timeout=CALL_TIMEOUT_S)
        except Exception:
            pass
        return "browser closed"

    # ── 보조 ──
    def _need_page(self):
        if self._page is None:
            raise BrowserError("no page is open — call mode 'open' with a url first")
        return self._page

    def _interactive(self, ref: str) -> str:
        target = self._refs.get(ref)
        if target is not None and target.role not in INTERACTIVE_ROLES:
            raise BrowserError(
                f"{ref} is a {target.role}, not something you can act on — "
                "pick a button/link/textbox/… ref from the snapshot"
            )
        self._locator(ref)  # unknown/stale 검사
        return f'{target.role} "{_cut(target.name)}"' if target.name else target.role


# ── 세션 싱글턴 ──────────────────────────────────────────────────────────

_SESSION: BrowserSession | None = None
_LOCK = threading.Lock()


def get_browser(create: bool = False) -> BrowserSession | None:
    """세션의 브라우저 — ``create`` 면 없을 때(또는 닫혔을 때) 새로 만든다."""
    global _SESSION
    with _LOCK:
        if create and (_SESSION is None or _SESSION.closed):
            _SESSION = BrowserSession()
        return _SESSION


def reset_browser() -> None:
    """세션 종료 — 브라우저를 닫고 싱글턴을 비운다."""
    global _SESSION
    with _LOCK:
        s, _SESSION = _SESSION, None
    if s is not None:
        s.close()
