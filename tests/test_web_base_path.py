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
from tests.web_static import STATIC, js_text, module_order


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
        # v10.116.0: 진입 파일과 그것이 싣는 모듈 전부 — 서빙된 글자로 본다.
        client = _client()
        for path in ["app.js"] + [f"js/{n}.js" for n in module_order()]:
            js = client.get(f"/static/{path}").text
            leftovers = self._ABS.findall(js)
            assert not leftovers, (path, leftovers)

    def test_entry_imports_are_relative(self):
        """진입 파일의 import 는 전부 ``./`` 상대 경로 — 모듈 지정자는 그 파일 URL
        기준으로 풀리므로 ``<base href>`` 접두 밑에서도 맞다. ``/static/js/…`` 같은
        절대 경로면 접두 프록시 뒤에서 404 가 나 화면 전체가 안 뜬다."""
        src = (STATIC / "app.js").read_text(encoding="utf-8")
        specs = re.findall(r"""^import\s.*?["']([^"']+)["'];?$""", src, re.MULTILINE)
        assert specs and len(specs) == len(module_order())
        assert all(s.startswith("./js/") for s in specs), specs
        # 모듈끼리의 import 도 같은 규칙(지금은 knob_chips → knobs_client 하나).
        for n in module_order():
            for s in re.findall(
                r"""^import\s.*?["']([^"']+)["'];?$""", js_text(n), re.MULTILINE
            ):
                assert s.startswith("./"), (n, s)

    def test_modules_resolve_under_prefix(self):
        """접두 밑에서 진입 파일이 가리키는 모듈 URL 이 실제로 서빙된다 — 프록시가
        접두를 벗겨 넘기므로 인스턴스엔 ``/static/js/<name>.js`` 로 온다."""
        client = _client(base_path="/s/doom")
        body = client.get("/").text
        assert '<base href="/s/doom/">' in body and 'src="static/app.js"' in body
        for n in module_order():
            assert client.get(f"/static/js/{n}.js").status_code == 200, n


class TestRoutingUnaffected:
    def test_api_routes_still_served_at_absolute_path(self):
        # the SERVER routes are unchanged — a proxy strips the prefix, so the
        # instance still receives /api/... . (health is unauthenticated.)
        assert _client().get("/api/health").status_code == 200
