"""``browser`` tool + ``agent_cli.browser`` engine — without Playwright.

The pure functions (aria parsing, ref assignment, rendering, find, host
allow-list) are tested directly; the session is driven with a fake page that
mimics the slice of the Playwright API the engine uses, so the thread/queue,
ref generations, host enforcement and observation wording all run for real.
"""

from __future__ import annotations

import pytest

from agent_cli import browser as B
from agent_cli.tools.base import RunContext
from agent_cli.tools.browser_tool import MODES, BrowserTool
from agent_cli.tools.registry import _BUILTIN_ORDER, TOOLS

SHOP = """\
- navigation:
  - link "Home":
    - /url: /a
- main:
  - heading "Shop" [level=1]
  - paragraph: {long}
  - table:
    - rowgroup:
      - row "Aurora Lamp $39.50":
        - cell "Aurora Lamp":
          - link "Aurora Lamp":
            - /url: item.html?n=Aurora
  - text: Email
  - textbox "Email"
  - combobox "Branch":
    - option "All" [selected]
    - option "North"
  - button "Search"
  - button "Search"
  - button "Say \\"hi\\""
""".format(long="Long paragraph text. " * 8)

ITEM = """\
- heading "Aurora Lamp" [level=1]
- button "Show specifications"
"""


# ── 순수 함수 ──────────────────────────────────────────────────────────────


class TestHostAllowed:
    def test_http_and_file_only(self):
        assert B.host_allowed("javascript:alert(1)", ()) is not None
        assert "http(s) and file" in B.host_allowed("ftp://x/", ())
        assert B.host_allowed("https://example.com/x", ()) is None
        assert B.host_allowed("file:///etc/passwd", ()) is None
        assert B.host_allowed("file://localhost/tmp/x.html", ()) is None

    def test_file_ignores_the_host_list_but_not_a_host(self):
        """로컬 파일은 허용 목록과 무관; ``file://name/x`` 는 경로가 아니다."""
        assert B.host_allowed("file:///r/report.html", ("example.com",)) is None
        why = B.host_allowed("file://report.html", ())
        assert "file:///path" in why and "report.html" in why

    def test_no_scheme_and_failed_load_say_what_happened(self):
        why = B.host_allowed("nope.html", ())
        assert '"nope.html" is neither a URL' in why and "file:///path" in why
        why = B.host_allowed("chrome-error://chromewebdata/", ())
        assert "failed to load" in why

    def test_no_host(self):
        assert "no host" in B.host_allowed("http:///x", ())

    def test_allow_list_with_subdomains(self):
        hosts = ("example.com", "localhost")
        assert B.host_allowed("http://example.com/", hosts) is None
        assert B.host_allowed("http://a.b.example.com/", hosts) is None
        assert B.host_allowed("http://localhost:8765/x", hosts) is None
        why = B.host_allowed("http://evil-example.com/", hosts)
        assert "evil-example.com" in why and "AGENT_CLI_BROWSER_HOSTS" in why

    def test_knobs_parse_hosts(self, monkeypatch):
        from agent_cli.constants import browser_knobs

        monkeypatch.setenv("AGENT_CLI_BROWSER_HOSTS", " Example.COM, .b.org ,, ")
        monkeypatch.setenv("AGENT_CLI_BROWSER_TOOL", "off")
        k = browser_knobs()
        assert k["hosts"] == ("example.com", "b.org") and k["enabled"] is False


class TestResolveTarget:
    def test_web_urls_pass_through(self, tmp_path):
        assert B.resolve_target("https://x.test/a", tmp_path) == (
            "https://x.test/a",
            None,
        )
        assert B.resolve_target("example.com", tmp_path) == ("example.com", None)

    def test_bare_and_file_paths_become_absolute_file_uris(self, tmp_path):
        f = tmp_path / "out dir" / "r.html"
        f.parent.mkdir()
        f.write_text("<h1>x</h1>")
        want = (f.resolve().as_uri(), str(f.resolve()))
        assert B.resolve_target("out dir/r.html", tmp_path) == want
        assert B.resolve_target("./out dir/r.html", tmp_path) == want
        assert B.resolve_target(str(f), tmp_path) == want
        assert B.resolve_target(f.as_uri(), tmp_path) == want
        assert "%20" in want[0]

    def test_missing_file_is_one_line(self, tmp_path):
        with pytest.raises(B.BrowserError, match="no such file: .*nope.html"):
            B.resolve_target("./nope.html", tmp_path)
        with pytest.raises(B.BrowserError, match="no such file"):
            B.resolve_target("file:///definitely/not/here.html", tmp_path)

    def test_file_url_with_a_host_is_left_to_host_allowed(self, tmp_path):
        assert B.resolve_target("file://r.html", tmp_path) == ("file://r.html", None)


class TestParseAria:
    def test_roles_names_attrs_text_props(self):
        nodes = B.parse_aria(SHOP)
        by = {(n.depth, n.role, n.name): n for n in nodes}
        assert by[(0, "navigation", "")].depth == 0
        assert by[(1, "link", "Home")].depth == 1
        assert nodes[2].prop == "/url: /a" and nodes[2].depth == 2
        assert by[(1, "heading", "Shop")].attrs == "[level=1]"
        assert by[(1, "paragraph", "")].text.startswith("Long paragraph")
        assert by[(1, "text", "")].text == "Email"
        assert by[(2, "option", "All")].attrs == "[selected]"
        assert by[(1, "button", 'Say "hi"')].name == 'Say "hi"'

    def test_unparseable_line_becomes_text(self):
        nodes = B.parse_aria("- 123 weird\nnot a list line\n")
        assert (
            len(nodes) == 1 and nodes[0].role == "text" and nodes[0].text == "123 weird"
        )


class TestRefs:
    def test_interactive_and_landmarks_get_refs_generic_do_not(self):
        nodes = B.parse_aria(SHOP)
        refs = B.assign_refs(nodes, start=1, generation=3)
        roles = {r.role for r in refs.values()}
        assert {
            "navigation",
            "link",
            "main",
            "heading",
            "table",
            "textbox",
            "combobox",
            "option",
            "button",
        } <= roles
        assert "paragraph" not in roles and "cell" not in roles and "row" not in roles
        assert all(r.generation == 3 for r in refs.values())
        assert [n.ref for n in nodes if n.role == "paragraph"] == [""]

    def test_nth_counts_same_role_and_name(self):
        nodes = B.parse_aria(SHOP)
        refs = B.assign_refs(nodes, start=10, generation=1)
        searches = [
            (k, r.nth)
            for k, r in refs.items()
            if r.role == "button" and r.name == "Search"
        ]
        assert [n for _, n in searches] == [0, 1]
        assert searches[0][0] == "e20" and searches[1][0] == "e21"  # start=10 honoured
        assert refs["e10"].role == "navigation"

    def test_scope_is_recorded(self):
        nodes = B.parse_aria(ITEM)
        refs = B.assign_refs(nodes, start=1, generation=1, scope="e7")
        assert all(r.scope == "e7" for r in refs.values())


class TestRender:
    def test_truncates_long_text_and_marks_refs(self):
        nodes = B.parse_aria(SHOP)
        B.assign_refs(nodes, start=1, generation=1)
        out = B.render_nodes(nodes)
        para = next(ln for ln in out.splitlines() if "paragraph" in ln)
        assert "…(+" in para and len(para) < 140
        assert '- heading "Shop" [level=1] [ref=e4]' in out
        assert '- button "Search" [ref=' in out
        assert "    - /url: /a" in out

    def test_depth_filter_relative_to_root(self):
        nodes = B.parse_aria(SHOP)
        out = B.render_nodes(nodes, depth=0)
        assert out.splitlines() == ["- navigation", "- main"]
        nodes = B.parse_aria('  - main:\n    - heading "X"\n      - text: deep\n')
        assert B.render_nodes(nodes, depth=1) == '- main\n  - heading "X"'

    def test_long_name_is_cut_in_render_only(self):
        name = "x" * 100
        nodes = B.parse_aria(f'- button "{name}"\n')
        refs = B.assign_refs(nodes, start=1, generation=1)
        assert refs["e1"].name == name  # locator keeps the full name
        assert "…(+20 chars)" in B.render_nodes(nodes)

    def test_render_within_keeps_the_deepest_depth_that_fits(self):
        nodes = B.parse_aria(SHOP)
        full = B.render_nodes(nodes)
        assert B.render_within(nodes, 0) == (full, None)  # 0 = no budget
        assert B.render_within(nodes, B.estimate_tokens(full)) == (full, None)
        d1 = B.render_nodes(nodes, depth=1)
        assert B.estimate_tokens(d1) < B.estimate_tokens(full)
        assert B.render_within(nodes, B.estimate_tokens(d1)) == (d1, 1)
        # a budget too small even for depth 1 still shows the roots, never ""
        assert B.render_within(nodes, 1) == ("- navigation\n- main", 0)
        assert B.render_within([], 1) == ("", None)


class TestFind:
    def test_text_match_with_path_and_ref(self):
        nodes = B.parse_aria(SHOP)
        B.assign_refs(nodes, start=1, generation=1)
        hits = B.find_nodes(nodes, text="aurora lamp")
        assert any(h.startswith("main > table > rowgroup > row") for h in hits)
        link = [h for h in hits if '> link "Aurora Lamp" [ref=' in h]
        assert link, hits

    def test_regex_matches_rendered_form(self):
        nodes = B.parse_aria(SHOP)
        hits = B.find_nodes(nodes, regex=r'^link "Aurora')
        assert hits == [
            'main > table > rowgroup > row "Aurora Lamp $39.50" > cell "Aurora Lamp" > link "Aurora Lamp"'
        ]

    def test_no_match(self):
        assert B.find_nodes(B.parse_aria(SHOP), text="zzz") == []


class TestChallenge:
    SORRY = "https://www.google.com/sorry/index?continue=https://www.google.com/search"
    DDG = (
        "DuckDuckGo Unfortunately, bots use DuckDuckGo too. Please complete the "
        "following challenge to confirm this search was made by a human."
    )

    def test_google_sorry_path_is_a_block_whatever_the_text(self):
        assert B.is_challenge(self.SORRY, "", "") == "unusual-traffic block"

    def test_short_page_with_a_marker(self):
        assert B.is_challenge("https://x.test/", "DuckDuckGo", self.DDG).startswith(
            "bot challenge ("
        )
        assert B.is_challenge("https://x.test/", "Just a moment…", "") == (
            'bot challenge ("just a moment")'
        )
        assert B.is_challenge("https://x.test/", "", "비정상적인 트래픽이 감지") == (
            'bot challenge ("비정상적인 트래픽")'
        )

    def test_long_page_about_captchas_is_not_a_challenge(self):
        article = ("A CAPTCHA asks you to prove you are not a robot. " * 80)[:3000]
        assert len(article) > B.CHALLENGE_TEXT_CAP
        assert (
            B.is_challenge("https://en.wikipedia.org/wiki/CAPTCHA", "CAPTCHA", article)
            == ""
        )

    def test_ordinary_short_page(self):
        assert B.is_challenge("https://x.test/", "Shop", "Aurora Lamp $39.50") == ""


class TestAvailability:
    def test_off_by_knob(self, monkeypatch):
        monkeypatch.setenv("AGENT_CLI_BROWSER_TOOL", "0")
        assert B.browser_available() is False

    def test_absent_without_playwright(self, monkeypatch):
        monkeypatch.delenv("AGENT_CLI_BROWSER_TOOL", raising=False)
        monkeypatch.setattr(B.importlib.util, "find_spec", lambda name: None)
        assert B.browser_available() is False

    def test_present(self, monkeypatch):
        monkeypatch.delenv("AGENT_CLI_BROWSER_TOOL", raising=False)
        monkeypatch.setattr(B.importlib.util, "find_spec", lambda name: object())
        assert B.browser_available() is True


# ── 가짜 페이지로 도는 세션 ────────────────────────────────────────────────


class FakeLocator:
    def __init__(self, page, role=None, name=None, nth=0, snapshot=None):
        self.page, self.role, self.name, self.index = page, role, name, nth
        self._snapshot = snapshot

    def nth(self, n):
        return FakeLocator(self.page, self.role, self.name, n, self._snapshot)

    def get_by_role(self, role, name=None, exact=False):
        self.page.lookups.append((role, name, exact))
        return FakeLocator(self.page, role, name)

    def aria_snapshot(self):
        if self._snapshot is not None:
            return self._snapshot
        return self.page.scoped.get((self.role, self.name), self.page.tree)

    def inner_text(self):
        if self.role is None:  # body — per page, so a challenge page can be told
            return self.page.body_text.get(self.page.url, "BODY TEXT\n\n\n\nmore")
        return self.page.text_of.get(
            (self.role, self.name or ""), "BODY TEXT\n\n\n\nmore"
        )

    def _act(self, what):
        self.page.actions.append((what, self.role, self.name, self.index))
        nav = self.page.navigates.get((self.role, self.name, self.index))
        if nav:
            self.page.load(*nav)
        self.page.mutate((self.role, self.name, self.index))

    def click(self):
        self._act("click")

    def fill(self, text):
        self._act(("fill", text))

    def press(self, key):
        self._act(("press", key))

    def select_option(self, value):
        self._act(("select", value))


class FakeKeyboard:
    def __init__(self, page):
        self.page = page

    def press(self, key):
        self.page.actions.append(("key", key))
        self.page.mutate(("key", key))


class FakePage:
    def __init__(self):
        self.url, self._title, self.tree = "about:blank", "", ""
        self.pages = {}  # url -> (title, tree)
        self.scoped = {}  # (role, name) -> subtree
        self.text_of = {}
        self.body_text = {}  # url -> inner_text of body
        self.navigates = {}  # (role, name, nth) -> (url)
        #: (role, name, nth) 또는 ("key", key) -> 같은 URL 에서 바뀐 트리 (v10.106.0)
        self.mutates = {}
        #: 같은 키 -> 기다린 뒤에야(``wait_for_timeout``) 보이는 트리 — setTimeout 식 늦은 갱신
        self.mutates_late = {}
        self._pending = None
        self.settles, self.waits = 0, []
        #: 같은 키 -> 그 조작이 띄우는 대화상자 (닫히고 머리에 실린다)
        self.dialog_on = set()
        self.context = FakeContext(None, None)
        self.actions, self.lookups, self.gotos = [], [], []
        self.keyboard = FakeKeyboard(self)
        self.handlers = {}
        self.history = []
        self.closed = False  # 사용자가 창을 닫았다

    def is_closed(self):
        return self.closed

    def mutate(self, key):
        if key in self.mutates:
            self.tree = self.mutates[key]
        if key in self.mutates_late:
            self._pending = self.mutates_late[key]
        if key in self.dialog_on:
            self.handlers["dialog"](FakeDialog())

    def load(self, url):
        self.history.append(self.url)
        self.url = url
        self._title, self.tree = self.pages.get(url, ("", ""))

    # playwright surface
    def title(self):
        return self._title

    def goto(self, url, **kw):
        self.gotos.append(url)
        self.load(self.pages.get(url, (None,))[0] is None and url or url)

    def go_back(self, **kw):
        self.load(self.history.pop())

    def locator(self, sel):
        assert sel == "body"
        return FakeLocator(self, snapshot=None)

    def get_by_role(self, role, name=None, exact=False):
        self.lookups.append((role, name, exact))
        return FakeLocator(self, role, name)

    def wait_for_load_state(self, *a, **k):
        pass

    def evaluate(self, js):
        """정착(rAF 두 프레임) 스텁 — 호출 수만 센다."""
        assert "requestAnimationFrame" in js
        self.settles += 1

    def wait_for_timeout(self, ms):
        self.waits.append(ms)
        if self._pending is not None:
            self.tree, self._pending = self._pending, None

    def set_default_timeout(self, ms):
        pass

    def set_default_navigation_timeout(self, ms):
        pass

    def on(self, event, handler):
        self.handlers[event] = handler

    def screenshot(self, *, path, full_page=False):
        """v10.61.0 — 진짜 PNG 를 쓴다(도구가 크기를 읽고 참조를 만든다)."""
        from PIL import Image

        self.actions.append(("screenshot", full_page))
        Image.new("RGB", (320, 200), (0, 0, 255)).save(path, format="PNG")


class FakeContext:
    def __init__(self, browser, storage_state):
        self.browser, self.storage_state_in = browser, storage_state

    def storage_state(self):
        return {"cookies": [{"name": "seen"}]}

    def new_page(self):
        page = self.browser.make_page()
        page.context = self
        return page


class FakeBrowser:
    """``_launch`` 의 대역 — 새 페이지는 같은 사이트(pages)를 본다."""

    def __init__(self, make_page, headless):
        self.make_page, self.headless, self.closed = make_page, headless, False
        self.context_kw = None

    def new_context(self, **kw):
        self.context_kw = kw
        return FakeContext(self, kw.get("storage_state"))

    def close(self):
        self.closed = True

    def is_connected(self):
        return not self.closed


class FakeDialog:
    type, message = "alert", "hello"

    def __init__(self):
        self.dismissed = False

    def dismiss(self):
        self.dismissed = True


@pytest.fixture
def fake():
    page = FakePage()
    page.pages["http://shop.test/"] = ("Gadget Shop", SHOP)
    page.pages["http://shop.test/item"] = ("Item", ITEM)
    page.navigates[("link", "Aurora Lamp", 0)] = ("http://shop.test/item",)
    page.scoped[("combobox", "Branch")] = (
        '- combobox "Branch":\n  - option "All" [selected]\n  - option "North"\n'
    )
    page.text_of[("main", "")] = "MAIN ONLY"
    session = B.BrowserSession(
        knobs={"enabled": True, "hosts": ("shop.test",), "executable": ""}
    )

    def ensure():  # the real one also registers the dialog handler
        session._page = page
        page.on("dialog", session._on_dialog)
        return page

    session._ensure_page = ensure  # type: ignore[method-assign]
    yield session, page
    session.close()


class TestSessionWithFakePage:
    def test_open_returns_header_and_refs(self, fake):
        s, page = fake
        out = s.open("http://shop.test/")
        assert out.startswith("[page] http://shop.test/ — Gadget Shop\n")
        assert '- link "Home" [ref=e2]' in out and '- button "Search" [ref=e' in out
        assert page.gotos == ["http://shop.test/"] and s.generation == 1

    def test_refused_url_never_reaches_the_page(self, fake):
        s, page = fake
        with pytest.raises(B.BrowserError, match="AGENT_CLI_BROWSER_HOSTS"):
            s.open("http://other.test/")
        with pytest.raises(B.BrowserError, match="http\\(s\\) and file"):
            s.open("ftp://x/")
        assert page.gotos == []

    def test_file_page_opens_and_a_link_to_a_secret_is_refused(self, fake):
        """로컬 파일은 허용 목록 밖이어도 열린다; 링크로 도착한 자격증명 경로는
        물을 수 없으니 빈 페이지로 돌리고 거절한다."""
        s, page = fake
        page.pages["file:///r/report.html"] = ("Report", ITEM)
        page.navigates[("button", "Show specifications", 0)] = (
            "file:///Users/u/.ssh/id_rsa",
        )
        out = s.open("file:///r/report.html")
        assert out.startswith("[page] file:///r/report.html — Report")
        btn = next(k for k, r in s._refs.items() if r.role == "button")
        with pytest.raises(B.BrowserError, match="sensitive path"):
            s.click(btn)
        assert page.url == "about:blank"

    def test_refs_are_monotonic_across_snapshots(self, fake):
        s, _ = fake
        first = s.open("http://shop.test/")
        second = s.snapshot()
        assert "[ref=e1]" in first and "[ref=e1]" not in second
        n = len(s._refs)
        assert f"[ref=e{n}]" in second

    def test_click_non_interactive_is_refused(self, fake):
        s, _ = fake
        s.open("http://shop.test/")
        with pytest.raises(
            B.BrowserError, match="heading, not something you can act on"
        ):
            s.click("e4")

    def test_unknown_and_malformed_refs(self, fake):
        s, _ = fake
        s.open("http://shop.test/")
        with pytest.raises(B.BrowserError, match="unknown ref e99"):
            s.click("e99")
        with pytest.raises(B.BrowserError, match="ref must look like e12"):
            s.click("Search")

    def test_click_navigation_bumps_generation_and_stales_old_refs(self, fake):
        s, page = fake
        s.open("http://shop.test/")
        link = next(
            k
            for k, r in s._refs.items()
            if r.role == "link" and r.name == "Aurora Lamp"
        )
        out = s.click(link)
        assert out.startswith(
            'clicked link "Aurora Lamp" — page changed\n[page] http://shop.test/item — Item'
        )
        assert '- button "Show specifications" [ref=' in out
        assert page.lookups[-1] == ("link", "Aurora Lamp", True) and s.generation == 2
        with pytest.raises(B.BrowserError, match="stale ref .*take a new snapshot"):
            s.click(link)

    def test_auto_snapshot_is_budgeted_but_refs_cover_hidden_nodes(self, fake):
        """큰 페이지: 이동 뒤 자동 스냅샷은 예산 안의 깊이까지만 — 숨은 노드도
        ref 는 받았으니 find/click 이 바로 되고, 명시적 snapshot 은 전체."""
        s, _ = fake
        nodes = B.parse_aria(SHOP)
        B.assign_refs(nodes, start=1, generation=1)  # the session's refs match
        budget = B.estimate_tokens(B.render_nodes(nodes, depth=1))
        out = s.open("http://shop.test/", budget=budget)
        assert out.startswith("[page] http://shop.test/ — Gadget Shop\n")
        assert '- link "Home" [ref=e2]' in out and '- link "Aurora Lamp"' not in out
        assert out.endswith(
            f"[snapshot cut to depth 1: {out.count(chr(10)) - 1} of {len(nodes)} nodes "
            f"(whole tree ~{B.estimate_tokens(B.render_nodes(nodes)):,} tokens) — use "
            '"find" with `text`, "text" with `ref`, or "snapshot" with a landmark `ref` '
            "for the part you need]"
        )
        link = next(
            k
            for k, r in s._refs.items()
            if r.role == "link" and r.name == "Aurora Lamp"
        )
        assert 'link "Aurora Lamp" [ref=e' in s.find(text="aurora")
        assert "[snapshot cut" not in s.snapshot()  # explicit = whole tree
        # the ref assigned to the hidden link at open time is live for click
        out = s.click(link, budget=1)  # navigation → budgeted again, depth 0 floor
        assert out.startswith('clicked link "Aurora Lamp" — page changed\n')
        assert "[snapshot cut to depth 0: 2 of 2 nodes" in out

    def test_headless_knob_default_is_auto(self, monkeypatch):
        from agent_cli.constants import browser_knobs

        monkeypatch.delenv("AGENT_CLI_BROWSER_HEADLESS", raising=False)
        assert browser_knobs()["headless"] == "auto"
        for raw, want in (
            ("0", "0"),
            ("off", "0"),
            ("1", "1"),
            ("true", "1"),
            ("x", "auto"),
        ):
            monkeypatch.setenv("AGENT_CLI_BROWSER_HEADLESS", raw)
            assert browser_knobs()["headless"] == want

    def test_nth_is_passed_to_the_locator(self, fake):
        s, page = fake
        s.open("http://shop.test/")
        second = [
            k for k, r in s._refs.items() if r.role == "button" and r.name == "Search"
        ][1]
        out = s.click(second)
        assert out == (
            'clicked button "Search" — same page (http://shop.test/); the '
            "accessibility tree is unchanged — a canvas or picture may still have "
            'changed, which only "screenshot" shows'
        )
        assert page.actions[-1] == ("click", "button", "Search", 1)

    def test_type_select_press(self, fake):
        s, page = fake
        s.open("http://shop.test/")
        box = next(k for k, r in s._refs.items() if r.role == "textbox")
        combo = next(k for k, r in s._refs.items() if r.role == "combobox")
        assert s.type(box, "glass", submit=True).startswith(
            'typed "glass" into textbox "Email" and pressed Enter — same page ('
        )
        assert page.actions[-2:] == [
            (("fill", "glass"), "textbox", "Email", 0),
            (("press", "Enter"), "textbox", "Email", 0),
        ]
        assert s.select(combo, "North").startswith(
            'selected "North" in combobox "Branch" — same page ('
        )
        assert s.press("Tab").startswith("pressed Tab — same page (")
        assert page.actions[-1] == ("key", "Tab")

    def test_same_page_change_carries_header_and_budgeted_snapshot(self, fake):
        """v10.106.0: 같은 페이지에서 트리가 바뀌면 머리 + 예산 스냅샷이 실린다 —
        예산을 넘으면 자동 스냅샷과 같은 ``[snapshot cut …]`` 꼬리."""
        s, page = fake
        s.open("http://shop.test/")
        page.mutates[("key", "ArrowDown")] = SHOP + '- status: "Moved to row 2"\n'
        out = s.press("ArrowDown")
        head, page_line, *rest = out.split("\n")
        assert head == "pressed ArrowDown — same page, changed in place"
        assert page_line == "[page] http://shop.test/ — Gadget Shop"
        assert '- status: "Moved to row 2"' in rest and "[snapshot cut" not in out
        # 예산을 넘는 변화 — 깊이 0 바닥까지 잘리고 꼬리가 붙는다
        page.mutates[("key", "ArrowUp")] = SHOP
        out = s.press("ArrowUp", budget=1)
        assert out.startswith("pressed ArrowUp — same page, changed in place\n[page]")
        assert "\n[snapshot cut to depth 0: " in out and out.endswith(
            "for the part you need]"
        )

    def test_select_counts_as_a_change(self, fake):
        """드롭다운 값이 바뀌는 작은 변화도 "changed" — 크기는 예산이 막는다."""
        s, page = fake
        s.open("http://shop.test/")
        combo = next(k for k, r in s._refs.items() if r.role == "combobox")
        page.mutates[("combobox", "Branch", 0)] = SHOP.replace(
            '- option "All" [selected]\n    - option "North"',
            '- option "All"\n    - option "North" [selected]',
        )
        out = s.select(combo, "North")
        assert out.startswith(
            'selected "North" in combobox "Branch" — same page, changed in place\n'
        )
        assert '- option "North" [selected]' in out

    def test_unchanged_tree_ignores_ref_numbers(self, fake):
        """비교는 ref 번호를 뺀 렌더 — 읽을 때마다 번호가 새로 찍혀도 같은 트리는
        "unchanged". 조작 직전에 다시 읽지 않는다(조작마다 트리 읽기 한 번)."""
        s, page = fake
        s.open("http://shop.test/")
        per_read = s._next_ref - 1  # 한 번 읽을 때 찍히는 ref 수
        s.snapshot()  # 번호가 새로 찍힌 전체 읽기 — 기준이 이것으로 바뀐다
        before = s._next_ref
        out = s.press("Tab")
        assert "the accessibility tree is unchanged" in out
        # unchanged 는 정착 뒤 읽기 + 200 ms 뒤 재읽기 — 정확히 두 번
        assert s._next_ref == before + 2 * per_read
        assert page.settles == 1 and page.waits == [B.SETTLE_RETRY_MS]
        out = s.press("Tab")
        assert "the accessibility tree is unchanged" in out

    def test_late_change_is_caught_by_the_one_retry(self, fake):
        """정착 뒤 첫 읽기에선 그대로, 기다린 뒤 바뀌는 페이지(setTimeout·rAF 루프) —
        재읽기 한 번이 잡아 "changed in place"; 다음 조작에 밀려 귀속되지 않는다."""
        s, page = fake
        s.open("http://shop.test/")
        page.mutates_late[("key", "r")] = SHOP + '- status: "Floor 2"\n'
        out = s.press("r")
        assert out.startswith("pressed r — same page, changed in place\n[page]")
        assert '- status: "Floor 2"' in out and page.waits == [B.SETTLE_RETRY_MS]
        out = s.press("Tab")  # 늦은 변화는 이미 앞 조작에 귀속됐다
        assert "the accessibility tree is unchanged" in out

    def test_immediate_change_skips_the_wait(self, fake):
        s, page = fake
        s.open("http://shop.test/")
        page.mutates[("key", "x")] = ITEM
        assert "changed in place" in s.press("x")
        assert page.settles == 1 and page.waits == []  # 빠른 길: 재읽기 없음

    def test_unread_document_counts_as_changed(self, fake):
        """새 문서(이동)가 기준을 비운다 — 비교할 것이 없으면 스냅샷을 싣는다."""
        s, _ = fake
        s.open("http://shop.test/")
        s._new_document()  # 같은 URL 의 새 문서(예: 다시 열기 직후, 트리 미독)
        out = s.press("Tab")
        assert out.startswith("pressed Tab — same page, changed in place\n[page]")

    def test_same_page_dialog_header_is_kept(self, fake):
        s, page = fake
        s.open("http://shop.test/")
        page.dialog_on.add(("key", "Enter"))
        out = s.press("Enter")
        assert out.split("\n")[1:] == [
            "[page] http://shop.test/ — Gadget Shop",
            "[dialog dismissed] alert: hello",
        ]
        assert "the accessibility tree is unchanged" in out.split("\n")[0]
        page.dialog_on.add(("key", "Escape"))
        page.mutates[("key", "Escape")] = ITEM
        out = s.press("Escape")
        assert out.split("\n")[:3] == [
            "pressed Escape — same page, changed in place",
            "[page] http://shop.test/ — Gadget Shop",
            "[dialog dismissed] alert: hello",
        ]

    def test_find_and_text(self, fake):
        s, _ = fake
        s.open("http://shop.test/")
        out = s.find(text="aurora")
        assert out.startswith("4 match(es) on http://shop.test/:")
        assert '> link "Aurora Lamp" [ref=' in out
        assert s.find(regex="zzz") == 'no match for "zzz" on http://shop.test/'
        text = s.text()
        assert (
            text.startswith("[text of http://shop.test/ — ")
            and "BODY TEXT\n\nmore" in text
        )

    def test_scoped_snapshot_and_text_resolve_through_the_scope(self, fake):
        s, page = fake
        s.open("http://shop.test/")
        combo = next(k for k, r in s._refs.items() if r.role == "combobox")
        out = s.snapshot(combo)
        assert (
            out.splitlines()[1]
            == '- combobox "Branch" [ref=e' + str(len(s._refs) - 2) + "]"
        )
        north = next(
            k for k, r in s._refs.items() if r.name == "North" and r.scope == combo
        )
        s.click(north)
        # resolved as combobox → option: two lookups, the inner one last
        assert page.lookups[-1] == ("option", "North", True) and page.lookups[-2] == (
            "combobox",
            "Branch",
            True,
        )
        main = next(k for k, r in s._refs.items() if r.role == "main")
        assert s.text(main).endswith("MAIN ONLY")

    def test_back_reloads_previous_document(self, fake):
        s, _ = fake
        s.open("http://shop.test/")
        link = next(k for k, r in s._refs.items() if r.name == "Aurora Lamp")
        s.click(link)
        out = s.back()
        assert (
            out.startswith("[page] http://shop.test/ — Gadget Shop")
            and s.generation == 3
        )

    def test_redirect_to_disallowed_host_is_undone(self, fake):
        s, page = fake
        page.pages["http://shop.test/go"] = ("Go", "")
        page.navigates[("link", "Home", 0)] = ("http://tracker.example/",)
        s.open("http://shop.test/")
        home = next(k for k, r in s._refs.items() if r.name == "Home")
        with pytest.raises(
            B.BrowserError, match='left the page: host "tracker.example"'
        ):
            s.click(home)
        assert page.url == "about:blank" and page.gotos[-1] == "about:blank"

    def test_dialog_is_dismissed_and_reported_once(self, fake):
        s, page = fake
        s.open("http://shop.test/")
        d = FakeDialog()
        page.handlers["dialog"](d)
        assert d.dismissed
        out = s.snapshot()
        assert "[dialog dismissed] alert: hello" in out
        assert "[dialog dismissed]" not in s.snapshot()

    def test_close_then_calls_fail_and_need_page_before_open(self, fake):
        s, _ = fake
        with pytest.raises(B.BrowserError, match="no page is open"):
            s.snapshot()
        s.open("http://shop.test/")
        assert s.close() == "browser closed" and s.closed
        assert s.close() == "browser already closed"
        with pytest.raises(B.BrowserError, match="browser is closed"):
            s.snapshot()

    def test_owner_thread_serialises_calls(self, fake):
        s, _ = fake
        s.open("http://shop.test/")
        import threading

        seen = []

        def worker():
            seen.append(threading.get_ident() != threading.main_thread().ident)
            s.snapshot()

        ts = [threading.Thread(target=worker) for _ in range(4)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        assert all(seen) and s._thread.name == "agent-cli-browser"


SORRY_URL = "http://shop.test/sorry/index?continue=/"
SORRY_TEXT = "페이지 정보 Google의 시스템이 컴퓨터 네트워크에서 비정상적인 트래픽을 감지했습니다."


@pytest.fixture
def blocked(monkeypatch):
    """사람 확인에 걸리는 사이트 + ``_launch`` 대역: 어떤 창이 떴는지 기록한다."""
    pages = {
        "http://shop.test/": ("Gadget Shop", SHOP),
        "http://shop.test/item": ("Item", ITEM),
        SORRY_URL: ("", '- text: 비정상적인 트래픽\n- link "왜"\n'),
    }
    body = {SORRY_URL: SORRY_TEXT}
    launches: list[FakeBrowser] = []

    def make_page():
        page = FakePage()
        page.pages, page.body_text = pages, body
        page.navigates[("link", "Aurora Lamp", 0)] = (SORRY_URL,)
        return page

    def launch(self, *, headless):
        b = FakeBrowser(make_page, headless)
        launches.append(b)
        return b

    monkeypatch.setattr(B.BrowserSession, "_launch", launch)

    def make(headless_knob):
        s = B.BrowserSession(
            knobs={
                "enabled": True,
                "hosts": ("shop.test",),
                "executable": "",
                "headless": headless_knob,
            }
        )
        return s

    yield make, launches, body
    B.reset_browser()


class TestVerificationSwitch:
    def test_auto_opens_a_window_carries_state_and_refuses_actions(self, blocked):
        make, launches, _ = blocked
        s = make("auto")
        try:
            out = s.open("http://shop.test/")
            assert out.startswith("[page] http://shop.test/") and s.visible is False
            assert [b.headless for b in launches] == [True]
            link = next(k for k, r in s._refs.items() if r.name == "Aurora Lamp")
            out = s.click(link)
            assert out.startswith(
                'clicked link "Aurora Lamp" — page changed\n[verification needed] '
            )
            assert (
                "unusual-traffic block" in out
                and "A visible browser window was opened" in out
            )
            assert "stays visible for the rest of this session" in out
            # a second, headed browser with the first one's cookies; the first is closed
            assert [b.headless for b in launches] == [True, False]
            assert launches[0].closed and launches[1].context_kw["storage_state"] == {
                "cookies": [{"name": "seen"}]
            }
            assert s.visible and s._page.url == SORRY_URL and s.generation == 3
            # acting on the challenge page is refused before anything else
            with pytest.raises(B.BrowserError, match="human-verification challenge"):
                s.click(link)
            s.snapshot()
            ref = next(k for k, r in s._refs.items() if r.name == "왜")
            with pytest.raises(
                B.BrowserError,
                match="human-verification challenge .*complete it in the visible browser window",
            ):
                s.click(ref)
            # still pending: the snapshot header says so
            assert "[browser] visible — verification still pending" in s.snapshot()
            # the user solves it: the page moves on; the next header says passed, once
            s._page.load("http://shop.test/item")
            out = s.snapshot()
            assert (
                "[browser] visible — verification passed; staying visible for this session"
                in out
            )
            assert "[browser]" not in s.snapshot()
            assert s.visible  # never back to headless
        finally:
            s.close()
        assert launches[1].closed

    def test_headless_1_reports_blocked_without_a_window(self, blocked):
        make, launches, _ = blocked
        s = make("1")
        try:
            out = s.open(SORRY_URL)
            assert out.startswith(f"[blocked] {SORRY_URL} — unusual-traffic block.")
            assert "AGENT_CLI_BROWSER_HEADLESS=1" in out and "official API" in out
            assert len(launches) == 1 and s.visible is False
            with pytest.raises(
                B.BrowserError, match="Ask the user, use the site's official API"
            ):
                s.press("Enter")
        finally:
            s.close()

    def test_auto_falls_back_to_blocked_when_no_window_can_open(
        self, blocked, monkeypatch
    ):
        make, launches, _ = blocked
        s = make("auto")
        real = B.BrowserSession._launch

        def launch(self, *, headless):
            if not headless:
                raise B.BrowserError(
                    "no browser could be launched — chrome: no display"
                )
            return real(self, headless=headless)

        monkeypatch.setattr(B.BrowserSession, "_launch", launch)
        try:
            out = s.open(SORRY_URL)
            assert out.startswith("[blocked] ") and "no display" in out
            assert s.visible is False and not launches[0].closed
        finally:
            s.close()

    def test_headless_0_starts_visible_and_only_asks(self, blocked):
        make, launches, _ = blocked
        s = make("0")
        try:
            out = s.open(SORRY_URL)
            assert [b.headless for b in launches] == [False] and s.visible
            assert (
                out.startswith("[verification needed] ")
                and "window is visible on this machine" in out
            )
        finally:
            s.close()


def _fake_playwright(monkeypatch, *, has_aria: bool, version="1.48.0"):
    """``playwright.sync_api`` 대역 — ``Locator`` 의 ``aria_snapshot`` 유무와 설치
    버전을 고른다. ``sync_playwright`` 가 불리면 실패한다(낡은 판에서 드라이버를 띄우면
    안 된다)."""
    import importlib.metadata
    import sys
    import types

    attrs = {"aria_snapshot": lambda self: ""} if has_aria else {}

    def must_not_start():
        raise AssertionError("sync_playwright() was called on an old Playwright")

    mod = types.SimpleNamespace(
        Locator=type("Locator", (), attrs), sync_playwright=must_not_start
    )
    monkeypatch.setitem(sys.modules, "playwright.sync_api", mod)
    monkeypatch.setitem(sys.modules, "playwright", types.SimpleNamespace(sync_api=mod))
    real = importlib.metadata.version
    monkeypatch.setattr(
        importlib.metadata,
        "version",
        lambda name: version if name == "playwright" else real(name),
    )


class TestPlaywrightMinVersion:
    """v10.112.1: ``Locator.aria_snapshot`` 은 Playwright 1.49 부터 — 낮으면 첫 호출이
    설치 버전과 함께 거절한다(속성 오류 원문 대신). 판정은 능력, 버전은 문구용."""

    def test_old_playwright_names_the_installed_version(self, monkeypatch):
        _fake_playwright(monkeypatch, has_aria=False)
        msg = B.playwright_too_old()
        assert msg == B.PLAYWRIGHT_TOO_OLD.format(have="1.48.0", need="1.49")
        assert "playwright 1.48.0 is installed" in msg
        assert "`pip install -U 'playwright>=1.49'`" in msg

    def test_capability_decides_not_the_version_string(self, monkeypatch):
        # 버전 문자열이 낡아 보여도 속성이 있으면 통과한다
        _fake_playwright(monkeypatch, has_aria=True, version="1.40.0")
        assert B.playwright_too_old() is None

    def test_unknown_version_still_refuses(self, monkeypatch):
        import importlib.metadata

        _fake_playwright(monkeypatch, has_aria=False)

        def boom(name):
            raise importlib.metadata.PackageNotFoundError(name)

        monkeypatch.setattr(importlib.metadata, "version", boom)
        assert "playwright unknown is installed" in B.playwright_too_old()

    def test_launch_refuses_before_starting_the_driver(self, monkeypatch):
        _fake_playwright(monkeypatch, has_aria=False)
        s = B.BrowserSession(knobs={"enabled": True, "hosts": (), "executable": ""})
        try:
            with pytest.raises(B.BrowserError, match="1.48.0 is installed"):
                s.open("http://x.test/")
            assert s._pw is None  # 드라이버를 띄우지 않았다(되감을 것 없음)
        finally:
            s.close()

    def test_tool_result_says_why(self, monkeypatch):
        from agent_cli import browser as browser_mod

        _fake_playwright(monkeypatch, has_aria=False)
        monkeypatch.setattr(browser_mod, "_SESSION", None)
        try:
            r = BrowserTool().run({"mode": "open", "url": "http://x.test/"})
            assert not r.success
            assert r.error.startswith(
                "browser: playwright 1.48.0 is installed; agent-cli needs 1.49 or newer"
            )
            assert "AttributeError" not in r.error
        finally:
            browser_mod.reset_browser()

    def test_the_tool_is_still_advertised(self, monkeypatch):
        """숨기지 않는다 — 깔아 둔 사용자에게 도구가 말없이 사라지는 것보다 낫다."""
        _fake_playwright(monkeypatch, has_aria=False)
        monkeypatch.setattr(B.importlib.util, "find_spec", lambda name: object())
        monkeypatch.setattr(B, "browser_knobs", lambda: {"enabled": True})
        assert B.browser_available() is True


class TestLaunchFallback:
    def test_tries_executable_chrome_then_chromium_and_reports_all(self, monkeypatch):
        calls = []

        class FailChromium:
            def launch(self, **kw):
                calls.append(kw)
                raise RuntimeError("nope\nsecond line")

        class PW:
            chromium = FailChromium()

            def stop(self):
                calls.append("stopped")

        import types

        fake_mod = types.SimpleNamespace(
            sync_playwright=lambda: types.SimpleNamespace(start=lambda: PW()),
            # v10.112.1: `_launch` 가 먼저 능력을 본다 — 1.49+ 처럼 aria_snapshot 이 있다
            Locator=type("Locator", (), {"aria_snapshot": lambda self: ""}),
        )
        monkeypatch.setitem(__import__("sys").modules, "playwright.sync_api", fake_mod)
        monkeypatch.setitem(
            __import__("sys").modules,
            "playwright",
            types.SimpleNamespace(sync_api=fake_mod),
        )
        s = B.BrowserSession(
            knobs={"enabled": True, "hosts": (), "executable": "/opt/chrome"}
        )
        with pytest.raises(B.BrowserError) as ei:
            s.open("http://x.test/")
        msg = str(ei.value)
        assert (
            "executable: nope" in msg
            and "chrome: nope" in msg
            and "chromium: nope" in msg
        )
        assert "second line" not in msg and "playwright install chromium" in msg
        assert (
            calls[0]["executable_path"] == "/opt/chrome"
            and calls[1]["channel"] == "chrome"
            and calls[-1] == "stopped"
        )
        s.close()


# ── 도구 표면 ────────────────────────────────────────────────────────────


class TestToolSchema:
    def test_registered_after_fetch_and_gated(self):
        assert isinstance(TOOLS["browser"], BrowserTool)
        order = list(_BUILTIN_ORDER)
        assert order.index("browser") == order.index("fetch") + 1
        assert TOOLS["browser"].requires_handler == "browser"

    def test_schema_shape(self):
        p = TOOLS["browser"].parameters
        assert p["required"] == ["mode"]
        assert p["properties"]["mode"]["enum"] == list(MODES)
        assert set(p["properties"]) == {
            "mode",
            "url",
            "ref",
            "text",
            "regex",
            "submit",
            "value",
            "key",
            "depth",
            "full_page",
        }

    @pytest.mark.parametrize(
        "args, msg",
        [
            ({"mode": "fly"}, "mode must be one of"),
            ({"mode": "open"}, "open requires `url`"),
            ({"mode": "click"}, "click requires `ref`"),
            ({"mode": "type", "ref": "e1"}, "type requires `text`"),
            ({"mode": "select", "ref": "e1"}, "select requires `value`"),
            ({"mode": "press"}, "press requires `key`"),
            ({"mode": "find"}, "find requires `text` or `regex`"),
        ],
    )
    def test_validate_per_mode(self, args, msg):
        assert msg in TOOLS["browser"].validate(args)

    def test_validate_ok(self):
        t = TOOLS["browser"]
        for a in (
            {"mode": "snapshot"},
            {"mode": "back"},
            {"mode": "find", "regex": "x"},
            {"mode": "type", "ref": "e1", "text": "x"},
        ):
            assert t.validate(a) is None

    def test_summary_arg(self):
        t = TOOLS["browser"]
        assert t.summary_arg({"mode": "open", "url": "http://x/"}) == "open http://x/"
        assert t.summary_arg({"mode": "click", "ref": "e3"}) == "click e3"
        assert t.summary_arg({"mode": "back"}) == "back"

    def test_retry_hint_names_the_narrowing_modes(self):
        hint = TOOLS["browser"].oversized_retry_hint
        assert '"snapshot"' in hint and '"find"' in hint and '"text" with `ref`' in hint
        assert "find(" not in hint and "ref=" not in hint  # 호출 모양 대신 문장


class TestToolDispatch:
    def test_action_before_open(self, monkeypatch):
        monkeypatch.setattr(B, "_SESSION", None)
        r = TOOLS["browser"].run({"mode": "snapshot"})
        assert not r.success and "no page is open" in r.error

    def test_open_creates_a_session_and_errors_are_observations(
        self, monkeypatch, fake
    ):
        session, _ = fake
        monkeypatch.setattr(B, "_SESSION", None)
        monkeypatch.setattr(B, "get_browser", lambda create=False: session)
        t = TOOLS["browser"]
        r = t.run({"mode": "open", "url": "http://shop.test/"})
        assert r.success and r.output.startswith("[page] http://shop.test/")
        r = t.run({"mode": "click", "ref": "e4"})
        assert not r.success and r.error.startswith("browser: e4 is a heading")

        def boom():
            raise RuntimeError(
                "Locator.click: Timeout 10000ms exceeded.\nCall log:\n  - waiting"
            )

        monkeypatch.setattr(session, "click", lambda ref, **kw: boom())
        r = t.run({"mode": "click", "ref": "e2"})
        assert (
            r.error == "browser: RuntimeError: Locator.click: Timeout 10000ms exceeded."
        )
        r = t.run({"mode": "snapshot", "depth": "1"})
        assert r.success and "- navigation" in r.output
        box = next(k for k, r in session._refs.items() if r.role == "textbox")
        r = t.run({"mode": "type", "ref": box, "text": "x", "submit": True})
        assert r.success and r.output.startswith('typed "x" into')

    def test_open_local_file_resolves_and_gates_secrets(
        self, monkeypatch, fake, tmp_path
    ):
        """맨 경로 → 절대 file URI; 자격증명 사전에 걸리면 ``read_file`` 과 같은
        게이트(여기선 물을 수 없어 거절)가 페이지에 닿기 전에 막는다."""
        from agent_cli.tools import _confine

        session, page = fake
        monkeypatch.setattr(B, "get_browser", lambda create=False: session)
        monkeypatch.chdir(tmp_path)
        (tmp_path / "r.html").write_text("<p>x</p>")
        uri = (tmp_path / "r.html").resolve().as_uri()
        page.pages[uri] = ("R", ITEM)
        t = TOOLS["browser"]
        r = t.run({"mode": "open", "url": "r.html"})
        assert r.success and r.output.startswith(f"[page] {uri} — R")
        r = t.run({"mode": "open", "url": "./missing.html"})
        assert not r.success and r.error.startswith("browser: no such file:")
        seen = []

        def gate(paths, action, **kw):
            seen.append((paths, action, kw))
            return "User denied browser — sensitive path: x"

        monkeypatch.setattr(_confine, "guard", gate)
        n = len(page.gotos)
        r = t.run({"mode": "open", "url": "r.html"})
        assert not r.success and r.error.startswith("User denied browser")
        assert seen == [
            (
                [str((tmp_path / "r.html").resolve())],
                "browser",
                {"check_workspace": False},
            )
        ]
        assert len(page.gotos) == n
        r = t.run({"mode": "open", "url": "http://shop.test/"})
        assert r.success and len(seen) == 1  # 웹 주소는 경로 게이트를 안 지난다

    def test_budget_comes_from_the_loop_cap(self, monkeypatch, fake):
        """예산 = 과대 출력 캡 × AUTO_SNAPSHOT_SHARE; ctx 가 없으면(캡 0) 전체."""
        session, _ = fake
        monkeypatch.setattr(B, "get_browser", lambda create=False: session)
        seen = []

        def spy(url, *, budget=0):
            seen.append(budget)
            return "[page] x"

        monkeypatch.setattr(session, "open", spy)
        t = TOOLS["browser"]
        t.run({"mode": "open", "url": "http://shop.test/"})
        t.run(
            {"mode": "open", "url": "http://shop.test/"},
            ctx=RunContext(oversized_cap=3_276),
        )
        assert seen == [0, int(3_276 * B.AUTO_SNAPSHOT_SHARE)]
        assert B.AUTO_SNAPSHOT_SHARE == 0.25

    def test_get_browser_singleton_and_reset(self, monkeypatch):
        monkeypatch.setattr(B, "_SESSION", None)
        assert B.get_browser() is None
        a = B.get_browser(create=True)
        assert B.get_browser() is a and B.get_browser(create=True) is a
        a.closed = True
        b = B.get_browser(create=True)
        assert b is not a
        B.reset_browser()
        assert B.get_browser() is None and b.closed


class TestAdvertising:
    def _tools(self):
        from unittest.mock import MagicMock

        from agent_cli.providers.capabilities import ModelCapabilities
        from tests.loop_ports import split_ports
        from tests.loopkit import make_loop

        caps = ModelCapabilities(
            context_window=4096, max_output_tokens=2048, supports_thinking=False
        )
        loop = make_loop(
            query="q",
            provider=MagicMock(),
            capabilities=caps,
            model="m",
            **split_ports({}),
        )
        return list(loop._config.tools_list)

    def test_absent_when_unavailable(self, monkeypatch):
        monkeypatch.setattr(B, "browser_available", lambda: False)
        assert "browser" not in self._tools()

    def test_present_when_available(self, monkeypatch):
        monkeypatch.setattr(B, "browser_available", lambda: True)
        tools = self._tools()
        assert "browser" in tools and tools.index("browser") == tools.index("fetch") + 1

    def test_teardown_closes_the_browser(self, monkeypatch):
        from agent_cli import runtime

        closed = []
        monkeypatch.setattr(B, "reset_browser", lambda: closed.append(True))
        runtime.teardown_session(None, None)
        assert closed == [True]


class TestWindowClosed:
    """보이는 창을 사용자가 닫거나 브라우저가 죽은 뒤 (v10.41.1). 실측(다나와 세션):
    죽은 페이지를 그대로 쓰면 ``TargetClosedError`` 가 ``close`` 할 때까지 매 턴
    반복됐다. 이제 ``open`` 은 같은 가시성으로 새 창을 띄우고 머리에 한 줄 알리며,
    다른 동작은 "창이 닫혔다 — open 으로 새로" 로 거부한다."""

    def _visible_session(self, blocked):
        make, launches, _ = blocked
        s = make("auto")
        s.open("http://shop.test/")
        link = next(k for k, r in s._refs.items() if r.name == "Aurora Lamp")
        s.click(link)  # 챌린지 → 보이는 창으로 전환
        assert s.visible and [b.headless for b in launches] == [True, False]
        return s, launches

    def test_open_after_the_user_closed_the_window_relaunches_visible(self, blocked):
        s, launches = self._visible_session(blocked)
        try:
            gen = s.generation
            s._page.closed = True  # 사용자가 창을 닫았다
            out = s.open("http://shop.test/item")
            assert out.startswith("[page] http://shop.test/item — Item\n")
            assert (
                "[browser] the previous window was closed (by the user or a crash); "
                "this is a new one — cookies and logins from the old window are gone"
                in out
            )
            # 세 번째 브라우저, 역시 보이는 창; 옛 것은 닫았고 쿠키는 옮기지 않았다
            assert [b.headless for b in launches] == [True, False, False]
            assert launches[1].closed and launches[2].context_kw == {
                "accept_downloads": False
            }
            assert s.visible and s.generation > gen
            assert not s._verification_pending and s._challenge == ""
            # 알림은 한 번뿐
            assert "[browser]" not in s.snapshot()
        finally:
            s.close()

    def test_actions_on_a_closed_window_say_so_instead_of_target_closed(self, blocked):
        s, _ = self._visible_session(blocked)
        try:
            s._page.closed = True
            for call in (s.snapshot, lambda: s.find(text="x"), s.text, s.back):
                with pytest.raises(
                    B.BrowserError,
                    match='browser window was closed .*`mode` "open"',
                ):
                    call()
            with pytest.raises(B.BrowserError, match="window was closed"):
                s.click("e1")
        finally:
            s.close()

    def test_dead_headless_browser_is_relaunched_headless(self, blocked):
        make, launches, _ = blocked
        s = make("1")
        try:
            s.open("http://shop.test/")
            launches[0].closed = True  # 브라우저 프로세스가 죽었다 (is_connected False)
            out = s.open("http://shop.test/")
            assert "[browser] the previous window was closed" in out
            assert [b.headless for b in launches] == [True, True] and not s.visible
        finally:
            s.close()

    def test_live_window_is_reused(self, blocked):
        make, launches, _ = blocked
        s = make("1")
        try:
            s.open("http://shop.test/")
            s.open("http://shop.test/item")
            assert len(launches) == 1 and "[browser]" not in s.snapshot()
        finally:
            s.close()


class TestScreenshot:
    """`screenshot` 모드 (v10.61.0, docs/vision/DESIGN.md §5): PNG 를
    `.agent-cli/tmp/screenshots/` 에 두고 결과에 이미지 참조를 싣는다."""

    def test_schema_and_validation(self):
        t = TOOLS["browser"]
        assert "screenshot" in MODES
        assert t.parameters["properties"]["full_page"]["type"] == "boolean"
        assert t.validate({"mode": "screenshot"}) is None
        assert "screenshot" in t.description

    def test_saves_png_and_attaches_ref(self, monkeypatch, fake, tmp_path):
        session, page = fake
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(B, "get_browser", lambda create=False: session)
        t = TOOLS["browser"]
        t.run({"mode": "open", "url": "http://shop.test/"})
        r = t.run({"mode": "screenshot"})
        assert r.success, r.error
        files = list((tmp_path / ".agent-cli" / "tmp" / "screenshots").iterdir())
        assert len(files) == 1 and files[0].name.endswith("-shot.png")
        ref = r.images[0]
        assert (ref["width"], ref["height"], ref["mime"]) == (320, 200, "image/png")
        assert r.output.splitlines()[0] == f"[image: {ref['path']} (320×200)]"
        assert "[screenshot of http://shop.test/]" in r.output
        assert "attached" in r.output
        assert ("screenshot", False) in page.actions
        r2 = t.run({"mode": "screenshot", "full_page": True})
        assert r2.success and "(full page)" in r2.output
        assert ("screenshot", True) in page.actions

    def test_before_open_is_an_observation(self, monkeypatch):
        monkeypatch.setattr(B, "_SESSION", None)
        r = TOOLS["browser"].run({"mode": "screenshot"})
        assert not r.success and "no page is open" in r.error
