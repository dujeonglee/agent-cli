"""``web --base-path`` — serve the UI under a URL path prefix (for a reverse
proxy that routes ``/<prefix>/*`` to this instance and strips the prefix).

Approach (B), the web standard: all frontend asset/API URLs are RELATIVE
(``api/...``, ``static/...``) and the served index.html carries a
``<base href="<prefix>/">`` so the browser resolves them under the prefix. With
the default (no ``--base-path``), ``<base href="/">`` makes relative URLs
resolve to the origin root — i.e. behaviour is byte-for-byte equivalent to the
old absolute URLs. The regression guard here is that NO absolute ``/api`` or
``/static`` URL survives in the served frontend (a missed one would 404 behind
a path-prefix proxy).
"""

from __future__ import annotations

import re

from fastapi.testclient import TestClient

from agent_cli.render.web import WebRenderer
from agent_cli.web.server import WebServer, create_app


def _client(base_path=""):
    server = WebServer(WebRenderer(), token="t", base_path=base_path)
    return TestClient(create_app(server))


class TestBaseTag:
    def test_default_base_is_root(self):
        body = _client().get("/").text
        assert '<base href="/">' in body

    def test_base_path_prefix(self):
        body = _client(base_path="/s/doom").get("/").text
        assert '<base href="/s/doom/">' in body

    def test_base_path_trailing_slash_normalised(self):
        # accept --base-path with or without a trailing slash
        body = _client(base_path="/s/doom/").get("/").text
        assert '<base href="/s/doom/">' in body


class TestNoAbsoluteFrontendUrls:
    """The core regression guard: every API/static URL in the served frontend
    must be RELATIVE so it resolves under <base>. A leftover absolute
    ``/api``/``/static`` would break path-prefix proxying."""

    _ABS = re.compile(r"""["'(]/(?:api|static)/""")

    def test_index_html_has_no_absolute_api_or_static(self):
        body = _client().get("/").text
        leftovers = self._ABS.findall(body)
        assert not leftovers, leftovers

    def test_app_js_has_no_absolute_api_or_static(self):
        js = _client().get("/static/app.js").text
        leftovers = self._ABS.findall(js)
        assert not leftovers, leftovers


class TestRoutingUnaffected:
    def test_api_routes_still_served_at_absolute_path(self):
        # the SERVER routes are unchanged — a proxy strips the prefix, so the
        # instance still receives /api/... . (health is unauthenticated.)
        assert _client().get("/api/health").status_code == 200
