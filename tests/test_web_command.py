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
