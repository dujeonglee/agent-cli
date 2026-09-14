"""Streaming idle detection + reconnect (DESIGN: 30s connect/header bound,
patient body via socket reset, idle ticks → reconnect after ~10min).

Covers the three new pieces:
- ``interruptible_lines`` idle detection — fires ``on_idle`` every threshold
  of silence, raises ``StreamIdleTimeout`` after max ticks, resets on data.
- ``make_stream_patient`` — relaxes the post's short read timeout once headers
  are in (best-effort, no-throw on urllib3 drift).
- ``OpenAIProvider`` streaming reconnect loop — re-sends on StreamIdleTimeout
  for ``STREAM_MAX_ATTEMPTS`` TOTAL attempts (first send included), then
  propagates. The count is a session knob (``CallSettings.stream_max_attempts``).
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
        yield from self._pre
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
                # Manual loop (not list(gen)): must capture partial results
                # into `got` before the expected mid-stream raise.
                got.append(line)  # noqa: PERF402
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
            supports_thinking=False,
        )
        return {
            "messages": [{"role": "user", "content": "hi"}],
            "system": "s",
            "model": "m",
            "capabilities": caps,
            "on_chunk": lambda *a, **k: None,
        }

    def test_reconnects_then_succeeds(self):
        from agent_cli.providers.base import LLMResponse

        prov = self._provider()
        ok = LLMResponse(content="done")
        # _handle_stream stalls twice, then succeeds on the 3rd connection.
        stream = MagicMock(
            side_effect=[StreamIdleTimeout(600), StreamIdleTimeout(600), ok]
        )
        with (
            patch("agent_cli.providers.http.post_with_retry") as post,
            patch("agent_cli.providers.http.make_stream_patient"),
            patch.object(type(prov), "_handle_stream", stream),
            patch("agent_cli.render.render_stream_stall") as stall,
        ):
            result = prov.call(**self._args())
        assert result is ok
        assert post.call_count == 3  # initial + 2 reconnects
        # A resend notice per reconnect, counted on TOTAL attempts (n/N) and
        # naming the attempt ABOUT to start — not the one that just failed.
        resends = [c for c in stall.call_args_list if c.kwargs["kind"] == "resend"]
        assert [(c.kwargs["attempt"], c.kwargs["attempts"]) for c in resends] == [
            (2, 4),
            (3, 4),
        ]
        # The in-place wait line is cleared once the stream finally succeeds,
        # so a stale "응답 대기 중" never outlives the wait.
        assert stall.call_args_list[-1].kwargs["kind"] == "clear"

    def test_exhausts_attempts_then_raises(self):
        from agent_cli.constants import STREAM_MAX_ATTEMPTS

        prov = self._provider()
        stream = MagicMock(side_effect=StreamIdleTimeout(600))  # always stalls
        with (
            patch("agent_cli.providers.http.post_with_retry") as post,
            patch("agent_cli.providers.http.make_stream_patient"),
            patch.object(type(prov), "_handle_stream", stream),
            patch("agent_cli.render.render_stream_stall") as stall,
            pytest.raises(StreamIdleTimeout),
        ):
            prov.call(**self._args())
        # STREAM_MAX_ATTEMPTS is the TOTAL, first send included — no (N+1).
        assert post.call_count == STREAM_MAX_ATTEMPTS
        # One resend notice FEWER than attempts: the last failure gives up
        # rather than announcing a re-send that never happens.
        resends = [c for c in stall.call_args_list if c.kwargs["kind"] == "resend"]
        assert len(resends) == STREAM_MAX_ATTEMPTS - 1
        # ...and the wait line is cleared on the way out, not left hanging.
        assert stall.call_args_list[-1].kwargs["kind"] == "clear"

    def test_max_attempts_knob_overrides_default(self):
        """세션 노브(CallSettings)가 전송 횟수를 실제로 바꾼다 — 상수 기본값
        이 아니라 ctx 값이 루프를 돈다."""
        from agent_cli.providers.base import CallSettings

        prov = self._provider()
        stream = MagicMock(side_effect=StreamIdleTimeout(600))
        with (
            patch("agent_cli.providers.http.post_with_retry") as post,
            patch("agent_cli.providers.http.make_stream_patient"),
            patch.object(type(prov), "_handle_stream", stream),
            patch("agent_cli.render.render_stream_stall"),
            pytest.raises(StreamIdleTimeout),
        ):
            prov.call(**self._args(), settings=CallSettings(stream_max_attempts=2))
        assert post.call_count == 2

    def test_single_attempt_never_resends(self):
        """1 = 재전송 없음. 경계값이라 off-by-one 이 숨기 쉬운 자리."""
        from agent_cli.providers.base import CallSettings

        prov = self._provider()
        stream = MagicMock(side_effect=StreamIdleTimeout(600))
        with (
            patch("agent_cli.providers.http.post_with_retry") as post,
            patch("agent_cli.providers.http.make_stream_patient"),
            patch.object(type(prov), "_handle_stream", stream),
            patch("agent_cli.render.render_stream_stall") as stall,
            pytest.raises(StreamIdleTimeout),
        ):
            prov.call(**self._args(), settings=CallSettings(stream_max_attempts=1))
        assert post.call_count == 1
        assert not [c for c in stall.call_args_list if c.kwargs["kind"] == "resend"]

    def test_wait_line_carries_attempt_coordinates(self):
        """대기 줄의 n/N 은 재연결 루프가 아는 좌표 — provider 를 거쳐
        run_sse_stream 까지 내려가야 한다(끊기면 항상 1/1 로 보인다)."""
        seen = {}

        def fake_handle(r, *a, attempt=None, attempts=None, **kw):
            seen["coords"] = (attempt, attempts)
            from agent_cli.providers.base import LLMResponse

            return LLMResponse(content="ok")

        prov = self._provider()
        with (
            patch("agent_cli.providers.http.post_with_retry"),
            patch("agent_cli.providers.http.make_stream_patient"),
            patch.object(type(prov), "_handle_stream", fake_handle),
            patch("agent_cli.render.render_stream_stall"),
        ):
            prov.call(**self._args())
        assert seen["coords"] == (1, 4)


class TestStreamMaxAttemptsKnob:
    """v8.60.0: ``STREAM_MAX_ATTEMPTS`` 는 **총 시도**(첫 전송 포함)이고,
    한도(Stall)와 한 쌍인 세션 노브다. 최대 대기 = 한도 × 시도."""

    def test_constant_is_total_attempts_not_reconnects(self):
        """재정의의 핵심: 값 자체가 총 시도라서 표시(n/N)와 나눗수가 같다.
        종전 ``STREAM_MAX_RECONNECTS`` 는 남아 있으면 안 된다 — 두 의미가
        공존하면 `(N+1)` 변환이 어디선가 부활한다."""
        from agent_cli import constants

        assert constants.STREAM_MAX_ATTEMPTS == 4
        assert not hasattr(constants, "STREAM_MAX_RECONNECTS")

    def test_max_wait_is_limit_times_attempts(self):
        """사용자가 실제로 정하는 값(최대 대기)이 두 노브의 곱이라는 계약.
        기본값 조합이 종전 동작(10분 × 4 = 40분)과 같아야 회귀가 아니다."""
        from agent_cli.constants import (
            DEFAULT_STREAM_IDLE_TIMEOUT_S,
            STREAM_MAX_ATTEMPTS,
        )

        assert DEFAULT_STREAM_IDLE_TIMEOUT_S * STREAM_MAX_ATTEMPTS == 2400  # 40분

    @pytest.mark.parametrize(
        "raw,expected",
        [(0, 1), (-5, 1), (1, 1), (4, 4), (10, 10), (11, 10), (999, 10)],
    )
    def test_clamp(self, raw, expected):
        """0 은 '끔'이 아니라 1 로 올라간다 — 시도 0회는 콜을 아예 안 한다는
        뜻이 되어버린다. 감지를 끄는 축은 한도(0=끔) 하나뿐."""
        from agent_cli.context.manager import clamp_stream_max_attempts

        assert clamp_stream_max_attempts(raw) == expected

    @pytest.mark.parametrize(
        "env,expected", [(None, 4), ("6", 6), ("99", 10), ("0", 1), ("abc", 4), ("", 4)]
    )
    def test_env_default(self, monkeypatch, env, expected):
        from agent_cli.context.manager import default_stream_max_attempts

        if env is None:
            monkeypatch.delenv("AGENT_CLI_STREAM_MAX_ATTEMPTS", raising=False)
        else:
            monkeypatch.setenv("AGENT_CLI_STREAM_MAX_ATTEMPTS", env)
        assert default_stream_max_attempts() == expected

    def test_ctx_default_and_setter(self, tmp_path):
        from agent_cli.context.manager import ContextManager

        ctx = ContextManager(session_dir=tmp_path / "s")
        assert ctx.stream_max_attempts == 4
        assert ctx.set_stream_max_attempts(7) == 7
        assert ctx.set_stream_max_attempts(0) == 1  # clamped, not "off"

    def test_callsettings_default_matches_constant(self):
        from agent_cli.constants import STREAM_MAX_ATTEMPTS
        from agent_cli.providers.base import CallSettings

        assert CallSettings().stream_max_attempts == STREAM_MAX_ATTEMPTS

    def test_subagent_inherits_attempts(self, tmp_path):
        """한도와 동형 — spawn 시점 스냅샷 상속. 한 축만 상속되면 서브에이전트의
        최대 대기가 부모와 달라진다(곱의 한쪽만 물려받으므로)."""
        from agent_cli.context.manager import ContextManager
        from agent_cli.subagent.runner import create_subagent_ctx

        parent = ContextManager(session_dir=tmp_path / "p")
        parent.set_stream_idle_timeout(120)
        parent.set_stream_max_attempts(7)
        sub, err = create_subagent_ctx("none", parent, tmp_path / "s")
        assert sub is not None, err
        assert (sub.stream_idle_timeout_s, sub.stream_max_attempts) == (120, 7)


class TestStallCliFlags:
    """v8.60.0: ``--stall`` / ``--stall-attempts`` (run·web 공통).

    한도는 종전에 env·웹 노브만 있었다. 한 노브로 합친 이상 CLI 에서 한
    축만 플래그이고 다른 축은 env 인 비대칭이 그대로 드러나므로 같이 넣는다."""

    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("600", 600),
            ("10m", 600),
            ("5m", 300),
            ("45s", 45),
            ("0", 0),  # 감지 끔 — None(미지정)과 구분되어야 한다
            (None, None),
            ("", None),
            ("   ", None),
        ],
    )
    def test_parse_stall(self, raw, expected):
        from agent_cli.main import _parse_stall

        assert _parse_stall(raw) == expected

    @pytest.mark.parametrize("bad", ["abc", "10x", "-1", "1.5m"])
    def test_parse_stall_rejects_garbage(self, bad):
        """조용히 기본값으로 떨어지면 헤드리스에서 오타가 드러나지 않는다 —
        harbor 러너가 의도한 한도 없이 몇 시간을 돈 뒤에야 알게 된다."""
        import typer

        from agent_cli.main import _parse_stall

        with pytest.raises(typer.BadParameter):
            _parse_stall(bad)

    def test_flags_registered_on_run_and_web(self):
        """두 명령 모두에 있어야 한다 — web 에만 있으면 harbor·스크립트
        경로가 env 로만 조절 가능해진다."""
        from typer.testing import CliRunner

        from agent_cli.main import app

        runner = CliRunner()
        for cmd in ("run", "web"):
            out = runner.invoke(app, [cmd, "--help"]).output
            assert "--stall" in out, cmd
            assert "--stall-attempts" in out, cmd

    def _ctx(self, tmp_path, **kw):
        """``_build_context`` 를 세션 디스크 조립 없이 호출 — 검증 대상은
        노브 적용뿐이다."""
        from agent_cli.main import _build_context

        boot = MagicMock(max_context_tokens=100_000, wire_format=None)
        with patch("agent_cli.context.session.get_session_dir", return_value=tmp_path):
            return _build_context(MagicMock(), boot, **kw)

    def test_build_context_applies_flags(self, tmp_path):
        ctx = self._ctx(tmp_path, stall="5m", stall_attempts=6)
        assert (ctx.stream_idle_timeout_s, ctx.stream_max_attempts) == (300, 6)

    def test_flags_beat_env(self, monkeypatch, tmp_path):
        """우선순위 CLI > env > 기본값. env 가 이기면 헤드리스에서 플래그가
        조용히 무시된다."""
        monkeypatch.setenv("AGENT_CLI_STREAM_IDLE_TIMEOUT_S", "900")
        monkeypatch.setenv("AGENT_CLI_STREAM_MAX_ATTEMPTS", "2")
        ctx = self._ctx(tmp_path, stall="120", stall_attempts=8)
        assert (ctx.stream_idle_timeout_s, ctx.stream_max_attempts) == (120, 8)

    def test_env_still_applies_when_flags_absent(self, monkeypatch, tmp_path):
        monkeypatch.setenv("AGENT_CLI_STREAM_IDLE_TIMEOUT_S", "900")
        monkeypatch.setenv("AGENT_CLI_STREAM_MAX_ATTEMPTS", "2")
        ctx = self._ctx(tmp_path)
        assert (ctx.stream_idle_timeout_s, ctx.stream_max_attempts) == (900, 2)

    def test_stall_zero_flag_disables_detection(self, tmp_path):
        """``--stall 0`` 은 미지정이 아니라 명시적 '끔' — sentinel 혼동이
        생기면 끄려는 사용자가 기본 10분을 받는다."""
        ctx = self._ctx(tmp_path, stall="0")
        assert ctx.stream_idle_timeout_s == 0
