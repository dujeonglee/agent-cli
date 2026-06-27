"""``web --trust-local`` — loopback requests skip token auth.

When an instance binds 127.0.0.1 behind a trusted gateway, only the gateway can
reach it, and the gateway already authenticated the user. ``--trust-local`` lets
such loopback requests through WITHOUT a token, so the gateway proxies plainly
(no per-request token injection). Safety: ONLY loopback bypasses — anything else
(and the whole feature when off) still requires the token.
"""

from __future__ import annotations

import httpx
import pytest

from agent_cli.render.web import WebRenderer
from agent_cli.web.server import WebServer, _with_token_query, create_app


class TestIsTrustedClient:
    def _server(self, trust):
        return WebServer(WebRenderer(), token="secret", trust_local=trust)

    def test_off_trusts_nobody(self):
        s = self._server(False)
        for host in ("127.0.0.1", "::1", "10.0.0.5", "testclient", None):
            assert s.is_trusted_client(host) is False, host

    def test_on_trusts_only_loopback(self):
        s = self._server(True)
        assert s.is_trusted_client("127.0.0.1") is True
        assert s.is_trusted_client("::1") is True
        assert s.is_trusted_client("10.0.0.5") is False
        assert s.is_trusted_client("testclient") is False
        assert s.is_trusted_client(None) is False


class TestWithTokenQuery:
    def test_empty_query(self):
        assert _with_token_query(b"", "secret") == b"token=secret"

    def test_preserves_other_params(self):
        out = _with_token_query(b"path=src&x=1", "secret").decode()
        assert "token=secret" in out and "path=src" in out and "x=1" in out

    def test_strips_existing_token(self):
        # any client-supplied (possibly wrong) token is replaced by ours
        out = _with_token_query(b"token=wrong&path=src", "secret").decode()
        from urllib.parse import parse_qs

        assert parse_qs(out)["token"] == ["secret"]


def _app(trust):
    return create_app(WebServer(WebRenderer(), token="secret", trust_local=trust))


def _client(app, host):
    transport = httpx.ASGITransport(app=app, client=(host, 5555))
    return httpx.AsyncClient(transport=transport, base_url="http://t")


class TestTrustLocalEndToEnd:
    @pytest.mark.asyncio
    async def test_loopback_bypasses_token(self):
        async with _client(_app(True), "127.0.0.1") as c:
            r = await c.get("/api/debug/prompt/scopes")  # NO token
            assert r.status_code == 200

    @pytest.mark.asyncio
    async def test_non_loopback_is_not_bypassed(self):
        # no token + non-loopback → rejected (422 missing / 401 wrong), NOT 200
        async with _client(_app(True), "10.0.0.5") as c:
            assert (await c.get("/api/debug/prompt/scopes")).status_code >= 400
            # wrong token from non-loopback → token is actually validated (401)
            r = await c.get("/api/debug/prompt/scopes?token=nope")
            assert r.status_code == 401

    @pytest.mark.asyncio
    async def test_trust_off_loopback_is_not_bypassed(self):
        async with _client(_app(False), "127.0.0.1") as c:
            assert (await c.get("/api/debug/prompt/scopes")).status_code >= 400
            r = await c.get("/api/debug/prompt/scopes?token=nope")
            assert r.status_code == 401

    @pytest.mark.asyncio
    async def test_valid_token_still_works_from_anywhere(self):
        async with _client(_app(True), "10.0.0.5") as c:
            r = await c.get("/api/debug/prompt/scopes?token=secret")
            assert r.status_code == 200
