"""main 입력창의 명령에 딸린 이미지 (v10.149.3) — 사람의 말에 딸린 이미지는 그 말이 가는 곳으로 간다.

`@agt-<key> 글`·`@<profile>-spawn 과제`·`@<profile> 과제`·`/<skill> …` 은 말이 에이전트·스킬 런의 시작
메시지가 되므로 이미지도 함께 간다(에이전트 채널 v10.147.2 와 같은 `Start.images`). 말이 갈 곳 없는 명령(목록·상태·
`/stop`·웹 전용)은 그대로 실행되고 사실 한 줄로 알린다. 종전엔 라우팅이 글만 받아 세 길(펌프가 연 차례·런 도중
턴 경계·제자리 대기) 모두 이미지를 버렸다.
"""

from __future__ import annotations

import json
import threading
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from agent_cli.command_table import (
    IMAGES_NOT_TAKEN,
    command_takes_images,
    web_input_is_command,
)
from agent_cli.images import ImageRef
from agent_cli.main import _command_dispatch, _ConsoleSurface
from agent_cli.main_input import MainInput
from agent_cli.runtime import MainLoop, wait_for_human_words
from agent_cli.web.surface import WebSurface
from tests.test_agents_live import _FakeLoopResult, make_registry
from tests.test_engine_p2 import wait_until

REF = ImageRef("a.png", "image/png", 10, 10, 1, "").to_dict()


def _spec():
    return SimpleNamespace(
        binding=None, max_turns=0, verbose=False, max_depth=2, ctx=None, session=None
    )


@pytest.fixture
def live(tmp_path):
    """실제 레지스트리 + 상주 하나 + 실제 WebSurface — 에이전트 런이 받은 (시작 글, 이미지) 를 적는다."""
    seen: list = []

    def runner(kind, start, ctx):
        seen.append((start.query, list(start.images)))
        return _FakeLoopResult(output="ok"), 0.01

    reg = make_registry(tmp_path, runner=runner)
    key, err = reg.spawn(name="a")
    assert not err
    assert wait_until(lambda: reg.get(key).state == "idle")
    renderer = MagicMock()
    surface = WebSurface(
        renderer=renderer,
        server=MagicMock(),
        ctx=None,
        binding=None,
        dispatch=_command_dispatch(
            _spec(),
            SimpleNamespace(agent_registry=reg),
            MagicMock,
            graceful_interrupt=True,
        ),
    )
    yield SimpleNamespace(
        reg=reg, key=key, surface=surface, renderer=renderer, seen=seen
    )
    reg.shutdown_all()


def _item(text, images=(REF,)):
    """명령인 말의 항목 — 들어오는 문(`MainInput`)이 명령으로 표시한 모양(5b-3)."""
    return {
        "id": "u-1",
        "text": text,
        "nickname": None,
        "images": list(images),
        "command": True,
    }


class TestThreeWays:
    def test_pump_opened_turn(self, live):
        item = _item(f"@{live.key} look at this")
        start = MainLoop.start(item, threading.Event())
        assert live.surface.route(start.query, start, item=item) is True
        assert wait_until(lambda: live.seen)
        assert live.seen[-1] == ("[user]: look at this", [REF])

    def test_turn_boundary(self, live):
        from agent_cli.providers.base import LLMResponse
        from agent_cli.providers.capabilities import ModelCapabilities
        from tests.loop_ports import make_ports, queue_inbox
        from tests.loopkit import run_loop

        provider = MagicMock()
        provider.call.side_effect = [
            LLMResponse(
                content=json.dumps(
                    {"action": "complete", "result": "ok", "answers": []}
                )
            )
        ] * 5
        pending = [_item(f"@{live.key} look at this")]
        run_loop(
            query="do Y",
            query_author="dj",
            ports=make_ports(
                inbox=queue_inbox(lambda: pending.pop(0) if pending else None),
                # runtime 의 배선과 같이 — 포트는 (글, 그 말의 항목) 을 표면의 route 로
                route_message=lambda text, item: live.surface.route(text, item=item),
            ),
            provider=provider,
            capabilities=ModelCapabilities(32768, 4096, False),
            model="m",
            max_turns=2,
        )
        assert wait_until(lambda: live.seen)
        assert live.seen[-1] == ("[user]: look at this", [REF])

    def test_in_place_wait(self, live):
        mi = MainInput(is_command=web_input_is_command)
        mi.enqueue("c", f"@{live.key} look at this", images=[REF])
        assert wait_for_human_words(mi, live.surface)(0) is False
        assert wait_until(lambda: live.seen)
        assert live.seen[-1] == ("[user]: look at this", [REF])


def test_same_start_shape_as_the_agent_channel(live):
    """에이전트 채널(사람이 그 채널에 쓴 것, v10.147.2)과 같은 모양 — 시작 글의 라벨 뒤 본문과 이미지 목록."""
    assert live.reg.request(live.key, "look at this", author="user", images=[REF]) == ""
    assert wait_until(lambda: live.seen)
    channel = live.seen[-1]
    live.surface.route(
        f"@{live.key} look at this", item=_item(f"@{live.key} look at this")
    )
    assert wait_until(lambda: len(live.seen) == 2)
    assert live.seen[-1] == channel


def test_spawn_with_a_task_carries_the_images(tmp_path, monkeypatch):
    seen: list = []

    def runner(kind, start, ctx):
        seen.append((start.query, list(start.images)))
        return _FakeLoopResult(output="ok"), 0.01

    reg = make_registry(tmp_path, runner=runner)
    monkeypatch.setattr(
        "agent_cli.main._parse_at_profile",
        lambda name: (name[: -len("-spawn")], "spawn"),
    )
    monkeypatch.setattr(reg, "spawn", _spawn_named(reg))
    surface = WebSurface(
        renderer=MagicMock(),
        server=MagicMock(),
        ctx=None,
        binding=None,
        dispatch=_command_dispatch(
            _spec(),
            SimpleNamespace(agent_registry=reg),
            MagicMock,
            graceful_interrupt=True,
        ),
    )
    try:
        assert (
            surface.route("@helper-spawn look", item=_item("@helper-spawn look"))
            is True
        )
        assert wait_until(lambda: seen)
        assert seen[-1][1] == [REF] and seen[-1][0].endswith("look")
    finally:
        reg.shutdown_all()


def _spawn_named(reg):
    """프로파일 파일 없이 spawn — 이름만 단 상주(`spawn(name=…)`)로 바꾼다."""
    real = reg.spawn

    def spawn(profile="", **kw):
        return real(name=profile)

    return spawn


def test_one_shot_run_agent_task_carries_the_images(tmp_path, monkeypatch):
    """`@<profile> 과제` — run 에이전트(`launch_run`)의 과제 요청에 이미지가 실린다(실제 레지스트리)."""
    seen: list = []

    def runner(kind, start, ctx):
        seen.append((start.query, list(start.images)))
        return _FakeLoopResult(output="done"), 0.01

    reg = make_registry(tmp_path, runner=runner)
    real_spawn = reg.spawn

    def spawn(
        *, profile="", **kw
    ):  # 프로파일 파일 없이 — 나머지 인자(run 표지 등)는 그대로
        kw.pop("name", None)
        return real_spawn(name=profile, **kw)

    monkeypatch.setattr(reg, "spawn", spawn)
    monkeypatch.setattr("agent_cli.main._parse_at_profile", lambda name: (name, "run"))
    out = MagicMock()
    from agent_cli.context.manager import ContextManager
    from agent_cli.main import try_dispatch_agent_or_skill
    from agent_cli.providers.capabilities import ModelCapabilities
    from tests.binding_support import make_binding

    try:
        handled = try_dispatch_agent_or_skill(
            "@helper find the bug",
            out,
            binding=make_binding(
                provider=MagicMock(), capabilities=ModelCapabilities(32768, 4096, False)
            ),
            max_turns=0,
            verbose=False,
            max_depth=2,
            ctx=ContextManager(session_dir=tmp_path / "ctx"),
            session=None,
            agent_registry=reg,
            images=[REF],
        )
        assert handled is True, out.method_calls
        assert seen and seen[0][1] == [REF], out.method_calls
        assert "find the bug" in seen[0][0]
    finally:
        reg.shutdown_all()


def test_skill_run_starts_with_the_images(monkeypatch):
    from agent_cli.skills.executor import SkillLoop
    from agent_cli.skills.models import Skill

    skill = Skill(name="look", description="", prompt_template="Look: $ARGUMENTS")
    caller = MagicMock()
    caller.depth, caller.max_depth, caller.max_turns, caller.binding = (
        0,
        2,
        0,
        MagicMock(),
    )
    caller.hooks_config = None
    start = SkillLoop(skill, "now", caller, images=[REF]).start()
    assert start.images == [REF] and "now" in start.query
    # 명령 길이 이미지를 스킬 런까지 넘긴다
    made: dict = {}

    class _Loop:
        def __init__(self, skill, arguments, caller, **kw):
            made.update(kw, arguments=arguments)

        def run(self):
            return SimpleNamespace(success=True, output="ok", artifact="")

    monkeypatch.setattr("agent_cli.skills.executor.SkillLoop", _Loop)
    monkeypatch.setattr("agent_cli.skills.load_skills", lambda: {"look": skill})
    from agent_cli.main import _dispatch_skill

    _dispatch_skill("/look now", None, images=[REF])
    assert made["images"] == [REF] and made["arguments"] == "now"


@pytest.mark.parametrize(
    ("text", "takes"),
    [
        ("@agt-1234abcd look", True),
        ("@helper-spawn task", True),
        ("@helper find it", True),
        ("/review now", True),
        ("/review", True),
        ("@agt-1234abcd", False),
        ("@", False),
        ("@agents", False),
        ("@helper", False),
        ("/skills", False),
        ("/stop agt-1", False),
        ("/stop", False),
        ("/help", False),
        ("/compact", False),
        ("/sh ls", False),
    ],
)
def test_which_commands_take_images(text, takes):
    assert command_takes_images(text) is takes


class TestCommandsThatTakeNoImages:
    @pytest.mark.parametrize("text", ["@agents", "/skills", "/help", "/stop agt-zz"])
    def test_web_runs_the_command_and_says_the_attachment_was_not_sent(
        self, live, text
    ):
        assert live.surface.route(text, item=_item(text)) is True
        notes = [
            c
            for c in live.renderer.observation.call_args_list
            if c.args[0] == IMAGES_NOT_TAKEN
        ]
        assert len(notes) == 1

    def test_status_of_an_agent_with_only_an_image(self, live):
        text = f"@{live.key}"
        assert live.surface.route(text, item=_item(text)) is True
        texts = [c.args[0] for c in live.renderer.observation.call_args_list]
        assert texts == [IMAGES_NOT_TAKEN]  # 상태는 디스패치 출력으로, 알림은 한 줄
        assert live.seen == []  # 상태만 — 요청은 가지 않았다

    def test_no_notice_without_images_or_when_the_command_takes_them(self, live):
        live.surface.route("@agents", item=_item("@agents", images=()))
        live.surface.route(f"@{live.key} hi", item=_item(f"@{live.key} hi"))
        texts = [c.args[0] for c in live.renderer.observation.call_args_list]
        assert IMAGES_NOT_TAKEN not in texts

    def test_console_says_it_without_touching_the_result(self, monkeypatch):
        printed: list = []
        monkeypatch.setattr(
            "agent_cli.main.console.print",
            lambda *a, **k: printed.append(a[0] if a else ""),
        )
        routed: list = []
        surface = _ConsoleSurface(
            result_file="", dispatch=lambda text, stop, item=None: True
        )
        surface.routed = lambda answer, ok: routed.append(ok)
        assert surface.route("/stop agt-1", item=_item("/stop agt-1")) is True
        assert printed == [IMAGES_NOT_TAKEN]
        assert routed == []  # 알림은 판정·결과 파일에 들지 않는다
