"""Streaming idle detection + reconnect (DESIGN: 30s connect/header bound,
patient body via socket reset, idle ticks → reconnect after ~10min).

Covers the three new pieces:
- ``interruptible_lines`` idle detection — fires ``on_idle`` every threshold
  of silence, raises ``StreamIdleTimeout`` after max ticks, resets on data.
- ``make_stream_patient`` — relaxes the post's short read timeout once headers
  are in (best-effort, no-throw on urllib3 drift).
- ``OpenAIProvider`` streaming reconnect loop — re-sends on StreamIdleTimeout
  up to ``STREAM_MAX_RECONNECTS``, then propagates.
"""

from __future__ import annotations

import threading
from unittest.mock import MagicMock, patch

import pytest

from agent_cli.providers.http import (
    StreamIdleTimeout,
    interruptible_lines,
    make_stream_patient,
)


class _StallResponse:
    """Fake streaming response: yields ``pre`` lines, then blocks (a stall)
    until ``close()`` — mimics a server that goes silent mid-stream."""

    def __init__(self, pre=()):
        self._pre = list(pre)
        self._unblock = threading.Event()
        self.closed = False

    def iter_lines(self):
        for ln in self._pre:
            yield ln
        self._unblock.wait(timeout=5)  # stall until close() (cap for safety)

    def close(self):
        self.closed = True
        self._unblock.set()


class TestInterruptibleLinesIdle:
    def test_idle_ticks_then_raises(self):
        ticks = []
        r = _StallResponse(pre=[b"data: hi"])  # one line, then stall
        gen = interruptible_lines(
            r,
            interrupt_check=None,
            poll_interval=0.01,
            idle_threshold=0.05,
            max_idle_ticks=3,
            on_idle=lambda tick, secs: ticks.append(tick),
        )
        got = []
        with pytest.raises(StreamIdleTimeout):
            for line in gen:
                got.append(line)
        assert got == [b"data: hi"]  # the pre-stall line was yielded
        assert ticks == [1, 2, 3]  # one notice per idle interval
        assert r.closed  # the response was closed before raising

    def test_data_resets_idle_no_raise(self):
        # A stream that keeps delivering lines faster than the threshold never
        # accumulates an idle interval → no ticks, no StreamIdleTimeout.
        ticks = []
        r = _StallResponse(pre=[b"a", b"b", b"c"])
        # close() right away so the post-`pre` wait returns immediately (DONE)
        r.close()
        got = list(
            interruptible_lines(
                r,
                interrupt_check=None,
                poll_interval=0.01,
                idle_threshold=0.5,  # large vs the instant delivery
                max_idle_ticks=3,
                on_idle=lambda tick, secs: ticks.append(tick),
            )
        )
        assert got == [b"a", b"b", b"c"]
        assert ticks == []

    def test_plain_passthrough_without_idle_or_interrupt(self):
        # No interrupt_check and no idle_threshold → simple iter_lines passthrough
        # (no reader thread).
        r = MagicMock()
        r.iter_lines.return_value = iter([b"x", b"y"])
        assert list(interruptible_lines(r)) == [b"x", b"y"]


class TestMakeStreamPatient:
    def test_resets_socket_timeout(self):
        r = MagicMock()
        sock = MagicMock()
        r.raw._connection.sock = sock
        make_stream_patient(r, 1200)
        sock.settimeout.assert_called_once_with(1200)

    def test_best_effort_on_missing_socket(self):
        # urllib3 internals drift → no socket attr → must not raise.
        r = MagicMock()
        del r.raw._connection  # AttributeError path
        make_stream_patient(r, 1200)  # no exception


class TestStreamingReconnect:
    def _provider(self):
        from agent_cli.providers.openai import OpenAIProvider

        return OpenAIProvider("http://x", "")

    def _args(self):
        from agent_cli.providers.capabilities import ModelCapabilities

        caps = ModelCapabilities(
            context_window=4096,
            max_output_tokens=256,
            supports_structured_output=False,
            supports_thinking=False,
            thinking_budget=0,
            supports_strict_schema=False,
        )
        return dict(
            messages=[{"role": "user", "content": "hi"}],
            system="s",
            model="m",
            capabilities=caps,
            on_chunk=lambda *a, **k: None,
        )

    def test_reconnects_then_succeeds(self):
        from agent_cli.providers.base import LLMResponse

        prov = self._provider()
        ok = LLMResponse(content="done")
        # _handle_stream stalls twice, then succeeds on the 3rd connection.
        stream = MagicMock(
            side_effect=[StreamIdleTimeout(600), StreamIdleTimeout(600), ok]
        )
        with (
            patch("agent_cli.providers.openai.post_with_retry") as post,
            patch("agent_cli.providers.openai.make_stream_patient"),
            patch.object(type(prov), "_handle_stream", stream),
            patch("agent_cli.render.render_status") as status,
        ):
            result = prov.call(**self._args())
        assert result is ok
        assert post.call_count == 3  # initial + 2 reconnects
        # a reconnect notice was rendered each time
        msgs = [c.args[1] for c in status.call_args_list if c.args[0] == "running"]
        assert any("재연결" in m for m in msgs)

    def test_exhausts_reconnects_then_raises(self):
        from agent_cli.constants import STREAM_MAX_RECONNECTS

        prov = self._provider()
        stream = MagicMock(side_effect=StreamIdleTimeout(600))  # always stalls
        with (
            patch("agent_cli.providers.openai.post_with_retry") as post,
            patch("agent_cli.providers.openai.make_stream_patient"),
            patch.object(type(prov), "_handle_stream", stream),
            patch("agent_cli.render.render_status"),
        ):
            with pytest.raises(StreamIdleTimeout):
                prov.call(**self._args())
        # initial + STREAM_MAX_RECONNECTS attempts, all stalled
        assert post.call_count == STREAM_MAX_RECONNECTS + 1
