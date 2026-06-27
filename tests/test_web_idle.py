"""IdleMonitor — self-reap decision logic for ``agent-cli web --idle-timeout``.

Pure + clock-injected so the timeout is tested without real waiting. The web
server polls ``tick()`` periodically; once the instance has been inactive (no
live viewers AND worker not busy AND empty queue) continuously for
``timeout_s``, it fires ``on_idle`` exactly once — which the server wires to
``uvicorn should_exit`` (graceful shutdown + session save via the existing
finally block). ``timeout_s<=0`` disables it (default: ``agent-cli web`` runs
forever, unchanged).
"""

from __future__ import annotations

from agent_cli.web.idle import IdleMonitor


class _Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    def advance(self, dt):
        self.t += dt


class TestIdleMonitor:
    def test_does_not_fire_while_active(self):
        clk = _Clock()
        fired = []
        m = IdleMonitor(
            is_active=lambda: True,
            timeout_s=60,
            on_idle=lambda: fired.append(1),
            now=clk,
        )
        for _ in range(5):
            clk.advance(100)
            m.tick()
        assert fired == []

    def test_fires_after_timeout_when_inactive(self):
        clk = _Clock()
        fired = []
        m = IdleMonitor(
            is_active=lambda: False,
            timeout_s=60,
            on_idle=lambda: fired.append(1),
            now=clk,
        )
        clk.advance(59)
        m.tick()
        assert fired == []  # not yet
        clk.advance(1)
        m.tick()
        assert fired == [1]  # 60s inactive → reap

    def test_activity_resets_the_timer(self):
        clk = _Clock()
        fired = []
        active = [False]
        m = IdleMonitor(
            is_active=lambda: active[0],
            timeout_s=60,
            on_idle=lambda: fired.append(1),
            now=clk,
        )
        clk.advance(40)
        active[0] = True
        m.tick()  # activity resets last_active
        active[0] = False
        clk.advance(40)
        m.tick()  # only 40s since the reset
        assert fired == []
        clk.advance(21)
        m.tick()  # 61s since the reset → reap
        assert fired == [1]

    def test_fires_only_once(self):
        clk = _Clock()
        fired = []
        m = IdleMonitor(
            is_active=lambda: False,
            timeout_s=10,
            on_idle=lambda: fired.append(1),
            now=clk,
        )
        clk.advance(100)
        m.tick()
        clk.advance(100)
        m.tick()
        assert fired == [1]

    def test_disabled_never_fires(self):
        for disabled in (0, -1, None):
            clk = _Clock()
            fired = []
            m = IdleMonitor(
                is_active=lambda: False,
                timeout_s=disabled,
                on_idle=lambda: fired.append(1),
                now=clk,
            )
            clk.advance(10_000)
            m.tick()
            assert fired == [], disabled

    def test_busy_keeps_it_alive_past_timeout(self):
        # the whole point: a running agent (busy) with no viewers must NOT be
        # reaped mid-task
        clk = _Clock()
        fired = []
        m = IdleMonitor(
            is_active=lambda: True,  # busy/working
            timeout_s=30,
            on_idle=lambda: fired.append(1),
            now=clk,
        )
        clk.advance(10_000)
        m.tick()
        assert fired == []


class TestActivityAccessors:
    """The three signals the web ``is_active`` predicate is built from. Each is
    a small, directly-testable accessor so the idle wiring needs no live
    server."""

    def _renderer(self):
        from agent_cli.render.web import WebRenderer

        return WebRenderer()

    def test_worker_busy_flag_tracks_busy_idle(self):
        r = self._renderer()
        assert r.worker_is_busy() is False  # fresh = idle
        r.worker_busy()
        assert r.worker_is_busy() is True
        r.worker_idle()
        assert r.worker_is_busy() is False

    def test_has_live_connections_empty(self):
        r = self._renderer()
        assert r.has_live_connections() is False

    def test_has_live_connections_with_open_conn(self):
        from agent_cli.render.web import WebConnection

        r = self._renderer()
        conn = WebConnection(id="c1")
        r.register_connection(conn)
        assert r.has_live_connections() is True
        conn.closed.set()  # client gone
        assert r.has_live_connections() is False

    def test_pending_count(self):
        from agent_cli.web.server import WebServer

        s = WebServer(self._renderer(), token="t")
        assert s.pending_count() == 0
        s.enqueue("c1", "hi")
        assert s.pending_count() == 1
