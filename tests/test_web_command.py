"""``agent-cli web`` helpers — browser auto-open gating.

The browser is auto-opened only for a local-machine bind (loopback or the
wildcards, reachable at localhost on the same box). A specific non-loopback
``--host <ip>`` is a remote bind: the operator browses from elsewhere, so
auto-opening a browser on the server is useless and was reported as annoying.
"""

from __future__ import annotations

from agent_cli.main import _is_local_bind


class TestIsLocalBind:
    def test_wildcards_are_local(self):
        # default `agent-cli web` binds 0.0.0.0 on your own machine → open
        assert _is_local_bind("0.0.0.0")
        assert _is_local_bind("::")

    def test_loopback_is_local(self):
        assert _is_local_bind("127.0.0.1")
        assert _is_local_bind("localhost")
        assert _is_local_bind("::1")
        assert _is_local_bind("LocalHost")  # case-insensitive
        assert _is_local_bind(" 127.0.0.1 ")  # tolerant of stray spaces

    def test_specific_ip_is_remote(self):
        # remote bind → do NOT auto-open
        assert not _is_local_bind("192.168.1.5")
        assert not _is_local_bind("10.0.0.3")
        assert not _is_local_bind("203.0.113.7")


class TestPickPort:
    """``pick_port`` prefers the requested port, else falls back to a free one
    (the dynamic collision avoidance the web command relies on)."""

    def test_free_preferred_is_used(self):
        import socket

        from agent_cli.web.server import pick_port

        # find a currently-free port, then ask pick_port for it
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            free = s.getsockname()[1]
        assert pick_port("127.0.0.1", free) == free

    def test_busy_preferred_falls_back(self):
        import socket

        from agent_cli.web.server import pick_port

        # hold a port with a LIVE listener, then ask for it → must differ
        srv = socket.socket()
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        busy = srv.getsockname()[1]
        try:
            got = pick_port("127.0.0.1", busy)
            assert got != busy and got > 0  # OS-assigned fallback
        finally:
            srv.close()


# ── C4: run/web 공용 부트스트랩 + --resume 공용 경로 + 예산 통일 ──────


class TestC4Bootstrap:
    def _fake_setup(self, monkeypatch, context_window=100_000):
        import agent_cli.main as m

        class _Caps:
            pass

        caps = _Caps()
        caps.context_window = context_window
        caps.max_output_tokens = 8_192
        monkeypatch.setattr(
            m,
            "_setup_provider",
            lambda *a, **k: ("PROV", caps, "m1", "http://u", "k", "openai"),
        )
        return m

    def test_bootstrap_bundles_and_budget_fallback(self, monkeypatch):
        m = self._fake_setup(monkeypatch)
        boot = m._bootstrap_provider(None, None, "", "", "md_array", 0)
        assert boot.resolved_model == "m1" and boot.provider_name == "openai"
        assert boot.wire_format.name == "md_array"
        # 예산 폴백 = 70% 통일 공식 (run/web 동일)
        assert boot.max_context_tokens == (100_000 * 7) // 10

    def test_bootstrap_explicit_budget_respected(self, monkeypatch):
        m = self._fake_setup(monkeypatch)
        boot = m._bootstrap_provider(None, None, "", "", "md_array", 12_345)
        assert boot.max_context_tokens == 12_345

    def test_bootstrap_unknown_format_fails_fast(self, monkeypatch):
        import typer

        m = self._fake_setup(monkeypatch)
        try:
            m._bootstrap_provider(None, None, "", "", "no_such_format", 0)
        except typer.Exit as e:
            assert e.exit_code == 2
        else:  # pragma: no cover
            raise AssertionError("unknown format must exit(2)")

    def test_load_resume_session_fail_fast(self):
        import typer

        import agent_cli.main as m

        try:
            m._load_resume_session("no-such-session-id-000")
        except typer.Exit as e:
            assert e.exit_code == 1
        else:  # pragma: no cover
            raise AssertionError("unknown session must exit(1)")

    def test_compute_token_budget_unified_formula(self):
        from agent_cli.context.manager import compute_token_budget

        assert compute_token_budget(262_144) == (262_144 * 7) // 10
        assert compute_token_budget(1_000) == 4_000  # floor

    def test_run_and_web_have_resume_option(self):
        # run 도 web 과 동일하게 --resume 을 노출 (C4 ③)
        import inspect

        import agent_cli.main as m

        assert "resume" in inspect.signature(m.run).parameters
        assert "resume" in inspect.signature(m.web).parameters
