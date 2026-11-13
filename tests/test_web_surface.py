"""``WebSurface`` — web 의 펌프 표면 (v10.33.1, docs/pump/DESIGN.md).

루프는 ``tests/test_pump.py`` 가 고정한다. 여기서는 web 이 펌프에 꽂는 표면이
종전 ``_worker_loop`` 가 하던 일을 그대로 하는지 본다: 전송 버튼 상태,
말풍선/깨우기 카드, Stop 핸들, 슬래시 → 디스패치 순서, 예외는 알리고 계속.
"""

from __future__ import annotations

import threading
from unittest.mock import MagicMock, patch

import pytest

from agent_cli.web.surface import WebSurface


@pytest.fixture
def parts():
    renderer, server, ctx = MagicMock(), MagicMock(), MagicMock(next_ordinal=3)
    dispatch = MagicMock(return_value=False)
    surface = WebSurface(
        renderer=renderer,
        server=server,
        ctx=ctx,
        binding=MagicMock(name="binding"),
        dispatch=dispatch,
    )
    return surface, renderer, server, ctx, dispatch


class TestSignals:
    def test_idle_and_busy_drive_the_send_button(self, parts):
        surface, renderer, *_ = parts
        surface.idle()
        renderer.worker_idle.assert_called_once()
        surface.busy()
        renderer.worker_busy.assert_called_once()

    def test_stop_handle_goes_to_the_server_and_to_dispatch(self, parts):
        surface, _, server, _, dispatch = parts
        ev = threading.Event()
        surface.bind_stop(ev)
        server.set_stop_handle.assert_called_with(ev)
        surface.route("@worker do x")
        dispatch.assert_called_once_with("@worker do x", ev)
        surface.bind_stop(None)
        server.set_stop_handle.assert_called_with(None)

    def test_graceful_interrupt_is_the_stop_button_contract(self, parts):
        assert parts[0].graceful_interrupt is True


class TestEcho:
    def test_user_message_is_a_bubble_with_its_nickname(self, parts):
        surface, renderer, *_ = parts
        surface.echo({"text": "hello", "nickname": "dj"}, wake=False)
        renderer.push_user_message.assert_called_once_with(
            "[dj]: hello", author="dj", hidx=3, images=None
        )

    def test_wake_draws_nothing(self, parts):
        """그 런의 관찰 줄이 메일이다 (v10.51.0)."""
        surface, renderer, *_ = parts
        surface.echo({"text": "WAKE"}, wake=True)
        assert renderer.method_calls == []


class TestRoute:
    def test_web_slash_commands_come_before_the_shared_dispatch(self, parts):
        surface, renderer, _, ctx, dispatch = parts
        with patch("agent_cli.web.slash.handle_slash_command", return_value=True) as h:
            assert surface.route("/help") is True
            h.assert_called_once_with("/help", renderer, ctx=ctx)
        dispatch.assert_not_called()

    def test_falls_through_to_the_shared_dispatch(self, parts):
        surface, _, _, _, dispatch = parts
        dispatch.return_value = True
        with patch("agent_cli.web.slash.handle_slash_command", return_value=False):
            assert surface.route("/skill a") is True
        dispatch.assert_called_once()

    def test_plain_chat_is_not_routed(self, parts):
        surface, *_ = parts
        with patch("agent_cli.web.slash.handle_slash_command", return_value=False):
            assert surface.route("hello there") is False


class TestRunOutcome:
    def test_errors_are_shown_and_swallowed(self, parts):
        surface, renderer, *_ = parts
        assert surface.run_failed(RuntimeError("boom")) is True
        renderer.error.assert_called_once()
        assert "boom" in renderer.error.call_args.args[0]

    def test_no_result_file(self, parts):
        surface, renderer, *_ = parts
        surface.run_ended(MagicMock(success=True, output="x"), MagicMock())
        renderer.error.assert_not_called()


class TestRunStarting:
    """런 시작 알림 (v10.130.6) — 종전 web `_run_main` 의 비전 알림이 표면으로 왔다.
    문구·조건·이벤트 종류(``status("error", …)``)는 그대로."""

    def _req(self, images):
        return MagicMock(images=images)

    def test_images_to_a_blind_model_are_announced(self, parts):
        from agent_cli.web.surface import NO_VISION_NOTICE

        surface, renderer, *_ = parts
        with patch(
            "agent_cli.providers.capabilities.binding_sees_images", return_value=False
        ) as sees:
            surface.run_starting(self._req([{"path": "a.png"}]))
        sees.assert_called_once_with(surface._binding)
        renderer.status.assert_called_once_with("error", NO_VISION_NOTICE)

    def test_a_seeing_model_gets_no_notice(self, parts):
        surface, renderer, *_ = parts
        with patch(
            "agent_cli.providers.capabilities.binding_sees_images", return_value=True
        ):
            surface.run_starting(self._req([{"path": "a.png"}]))
        renderer.status.assert_not_called()

    def test_no_images_no_probe(self, parts):
        """이미지가 없으면 능력을 묻지도 않는다 — 프로브는 이미지 런에서만."""
        surface, renderer, *_ = parts
        with patch("agent_cli.providers.capabilities.binding_sees_images") as sees:
            surface.run_starting(self._req([]))
        sees.assert_not_called()
        renderer.status.assert_not_called()


class TestWebServerQueueContract:
    def test_dequeue_blocking_accepts_the_pumps_timeout(self):
        """펌프는 ``dequeue_blocking(timeout=…)`` 으로 부른다 — 서버의 큐가 펌프의
        큐이므로 그 시그니처를 받아야 한다(ForeverPolicy 는 None). 서버의
        ``enqueue``(닉네임 해석)로 넣은 것이 같은 큐에서 나온다."""
        from agent_cli.web.server import WebServer

        s = WebServer(MagicMock(), token="t")
        assert s.queue.dequeue_blocking(timeout=0.01) is None
        s.enqueue("c", "hi")
        assert s.queue.dequeue_blocking(timeout=None)["text"] == "hi"


class TestEchoImages:
    def test_queued_images_ride_on_the_bubble(self, parts):
        """v10.60.0: 큐 아이템의 ``images`` 가 에코 카드의 썸네일이 된다."""
        surface, renderer, *_ = parts
        ref = {"path": "a.png", "width": 1, "height": 1}
        surface.echo({"text": "look", "nickname": "dj", "images": [ref]}, wake=False)
        renderer.push_user_message.assert_called_once_with(
            "[dj]: look", author="dj", hidx=3, images=[ref]
        )
