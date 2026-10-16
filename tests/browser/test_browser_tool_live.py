"""``browser`` tool against a real headless browser (opt-in: AGENT_CLI_BROWSER_TESTS=1).

Three local pages the unit layer cannot exercise: a table drawn by JavaScript
after a delay, a section revealed by a button, and a form with a dropdown.
Served from a thread-local ``http.server`` so the tool's http(s)-only rule holds.
"""

from __future__ import annotations

import http.server
import socket
import threading
import time

import pytest

from agent_cli import browser as B

pytestmark = pytest.mark.browser

PAGES = {
    "/shop.html": """<!doctype html><title>Gadget Shop</title><h1>Gadget Shop</h1><div id=l>Loading…</div>
<script>setTimeout(()=>{document.getElementById('l').innerHTML='<table><tr><th>Item</th><th>Price</th></tr>'
+'<tr><td><a href="item.html">Aurora Lamp</a></td><td>$39.50</td></tr></table>'},300)</script>""",
    "/item.html": """<!doctype html><title>Item</title><h1>Aurora Lamp</h1><button id=b>Show specifications</button>
<div id=s hidden></div><script>b.onclick=()=>{s.hidden=false;s.innerHTML='<p>SKU: AL-4471</p>'}</script>""",
    "/search.html": """<!doctype html><title>Library</title><form id=f><label>Title <input id=q></label>
<label>Branch <select id=b><option value=all>All</option><option value=river>Riverside</option></select></label>
<button>Search</button></form><div id=r></div><script>f.onsubmit=e=>{e.preventDefault();
r.textContent=q.value+' @ '+b.value+' → Shelf R-03'}</script>""",
    "/challenge.html": """<!doctype html><title>Just a moment…</title><p>Unfortunately, bots use this site too.
Please complete the following challenge to confirm you are human.</p><input type=checkbox id=c><label for=c>I am not a robot</label>""",
}


class _Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):  # http.server API name
        body = PAGES.get(self.path.split("?")[0])
        self.send_response(200 if body else 404)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write((body or "").encode())

    def log_message(self, *a):  # quiet
        pass


@pytest.fixture(scope="module")
def site():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", port), _Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{port}"
    srv.shutdown()


@pytest.fixture
def session():
    pytest.importorskip("playwright.sync_api")
    s = B.BrowserSession(
        knobs={"enabled": True, "hosts": ("127.0.0.1",), "executable": ""}
    )
    yield s
    s.close()


def _ref(s, role, name):
    return next(k for k, r in s._refs.items() if r.role == role and r.name == name)


def test_delayed_table_link_and_revealed_section(session, site):
    s = session
    out = s.open(f"{site}/shop.html")
    assert out.startswith(f"[page] {site}/shop.html — Gadget Shop")
    time.sleep(0.6)
    out = s.snapshot()
    assert '- link "Aurora Lamp" [ref=' in out and 'cell "$39.50"' in out
    out = s.click(_ref(s, "link", "Aurora Lamp"))
    assert (
        out.startswith('clicked link "Aurora Lamp" — page changed')
        and "/item.html" in out
    )
    out = s.click(_ref(s, "button", "Show specifications"))
    assert "same page" in out
    assert "SKU: AL-4471" in s.text()
    assert "SKU: AL-4471" in s.find(text="SKU")


def test_form_type_select_submit(session, site):
    s = session
    s.open(f"{site}/search.html")
    s.type(_ref(s, "textbox", "Title"), "glass")
    s.select(_ref(s, "combobox", "Branch"), "Riverside")
    out = s.click(_ref(s, "button", "Search"))
    assert "same page" in out
    assert "glass @ river → Shelf R-03" in s.text()


def test_challenge_page_is_blocked_when_headless_is_pinned(site):
    """사람 확인 페이지: ``AGENT_CLI_BROWSER_HEADLESS=1`` 이면 창 없이 ``[blocked]``,
    조작은 거부 (auto 의 창 띄우기는 화면이 필요해 여기서 안 돈다)."""
    pytest.importorskip("playwright.sync_api")
    s = B.BrowserSession(
        knobs={
            "enabled": True,
            "hosts": ("127.0.0.1",),
            "executable": "",
            "headless": "1",
        }
    )
    try:
        out = s.open(f"{site}/challenge.html")
        assert out.startswith(f"[blocked] {site}/challenge.html — bot challenge (")
        assert "AGENT_CLI_BROWSER_HEADLESS=1" in out and not s.visible
        with pytest.raises(B.BrowserError, match="human-verification challenge"):
            s.press("Enter")
        assert "I am not a robot" in s.text()  # reading is still allowed
    finally:
        s.close()


def test_host_rule_and_engine_label(session, site):
    s = session
    with pytest.raises(B.BrowserError, match="AGENT_CLI_BROWSER_HOSTS"):
        s.open("http://example.com/")
    s.open(f"{site}/item.html")
    assert s.engine in ("chrome", "chromium")
