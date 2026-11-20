"""세션 조립 함수 넷 (`runtime.build_session_stores`·`build_session_owners`·
`wire_session`·`close_session`, v10.131.0) — run·web 이 같은 함수로 조립한다.

구조 고정(호스트가 이 함수들만 부른다)은 ``tests/test_runtime_assembly.py`` 가 본다.
여기서는 함수 자체의 계약: 순서, 같은 큐, 도중에 죽었을 때, 호스트별 훅을 안 줄 때.
"""

from __future__ import annotations

from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from agent_cli import runtime as R
from agent_cli.main_input import MainInput
from agent_cli.runtime import (
    SessionOwners,
    SessionSpec,
    SessionStores,
    build_session_owners,
    build_session_stores,
    close_session,
    wire_session,
)


def _spec(**kw) -> SessionSpec:
    base = {
        "binding": MagicMock(name="binding"),
        "session": SimpleNamespace(session_id="sess-1"),
        "ctx": SimpleNamespace(
            session_dir="/sess", last_ordinal=None
        ),  # 새 세션 — 재시작 기록 없음
        "mcp_manager": "MCP",
        "knob_args": {"max_agents": 3},
        "hooks_config": {"PreToolUse": ["h"]},
        "max_turns": 5,
        "max_depth": 2,
        "verbose": False,
        "record_turns": True,
    }
    base.update(kw)
    return SessionSpec(**base)


class _Schedules:
    """예약 대역 — 대입된 발화 자리와 `start` 를 기록한다."""

    def __init__(self, order):
        self.order = order
        self.enqueue = None
        self.on_change = None

    def start(self):
        self.order.append("schedules.start")


class _Surface:
    graceful_interrupt = False

    def is_command(self, text):
        return False


def _patch_parts(st: ExitStack, order: list, *, mainloop_boom=False):
    """조립 부품을 기록하는 대역으로 — 무엇이 어떤 순서로 불리는지."""

    def rec(name, ret=None):
        def f(*a, **k):
            order.append(name)
            return ret() if callable(ret) else ret

        return f

    jobs = SimpleNamespace(on_change=None)
    schedules = _Schedules(order)
    registry = MagicMock(name="registry")
    st.enter_context(
        patch.object(R, "build_monitor_registry", rec("monitors", lambda: "MON"))
    )
    st.enter_context(patch.object(R, "build_job_registry", rec("jobs", lambda: jobs)))
    st.enter_context(patch.object(R, "build_hook_runner", rec("hook_runner")))
    st.enter_context(patch.object(R, "build_snapshot_store", rec("snapshot")))
    st.enter_context(
        patch.object(R, "build_schedule_registry", rec("schedules", lambda: schedules))
    )
    st.enter_context(
        patch.object(R, "build_agent_registry", rec("registry", lambda: registry))
    )
    st.enter_context(
        patch("agent_cli.harness.auditor.build_auditor", rec("auditor", lambda: "AUD"))
    )
    st.enter_context(patch("agent_cli.knobs.apply_all", rec("knobs")))
    st.enter_context(patch.object(R, "wire_agent_mail", rec("mail", lambda: (1, 2))))

    def _mainloop(**kw):
        order.append("MainLoop")
        if mainloop_boom:
            raise RuntimeError("MainLoop failed")
        return MagicMock(name="main_kind")

    st.enter_context(patch.object(R, "MainLoop", _mainloop))
    st.enter_context(
        patch("agent_cli.pump.MainActor", rec("pump", lambda: MagicMock()))
    )
    return SimpleNamespace(jobs=jobs, schedules=schedules, registry=registry)


class TestOrder:
    def test_stores_owners_knobs_mail_loop_pump_then_schedule_thread(self):
        """저장소 → 레지스트리 → 감사자 → 노브 → 메일 → MainLoop → 펌프 → 예약
        `start()` — 소비자를 다 세운 뒤에 생산자를 켠다."""
        order: list[str] = []
        with ExitStack() as st:
            _patch_parts(st, order)
            spec = _spec()
            stores = build_session_stores(spec, main_input=MainInput())
            owners = build_session_owners(spec, stores, renderer="R")
            wired = wire_session(
                spec,
                stores,
                owners,
                surface=_Surface(),
                policy=None,
            )
        assert order == [
            "monitors",
            "jobs",
            "hook_runner",
            "snapshot",
            "schedules",
            "registry",
            "auditor",
            "knobs",
            "mail",
            "MainLoop",
            "pump",
            "schedules.start",
        ]
        assert (wired.revived, wired.auto) == (1, 2)

    def test_a_wire_that_dies_never_starts_the_schedule_thread(self):
        order: list[str] = []
        with ExitStack() as st:
            _patch_parts(st, order, mainloop_boom=True)
            spec = _spec()
            stores = build_session_stores(spec, main_input=MainInput())
            owners = build_session_owners(spec, stores, renderer="R")
            with pytest.raises(RuntimeError):
                wire_session(
                    spec,
                    stores,
                    owners,
                    surface=_Surface(),
                    policy=None,
                )
        assert "MainLoop" in order and "schedules.start" not in order
        assert "pump" not in order


class TestRestartRecord:
    """재시작(5c-0): 디스크에서 올라온 main 컨텍스트 끝에 사실 기록 한 줄과 관찰 카드 — 메일 배선(복원)보다 먼저."""

    def _wire(self, ctx):
        order: list[str] = []
        cards: list = []
        with ExitStack() as st:
            _patch_parts(st, order)
            st.enter_context(
                patch.object(
                    R,
                    "render_step",
                    lambda *a, **k: (order.append("card"), cards.append((a, k))),
                )
            )
            spec = _spec(ctx=ctx)
            stores = build_session_stores(spec, main_input=MainInput())
            owners = build_session_owners(spec, stores, renderer="R")
            wire_session(spec, stores, owners, surface=_Surface(), policy=None)
        return order, cards

    def test_a_restored_context_gets_the_record_and_its_card_before_restore(
        self, tmp_path
    ):
        from agent_cli.constants import RESTART_NOTICE
        from agent_cli.context.manager import ContextManager

        ContextManager(tmp_path, max_context_tokens=10000).add(
            {"role": "user", "content": "before the cut"}
        )
        ctx = ContextManager(tmp_path, max_context_tokens=10000, resume=True)
        order, cards = self._wire(ctx)
        assert order.index("card") < order.index("mail")
        ((args, kw),) = cards
        assert args[:2] == ("observation", RESTART_NOTICE)
        assert kw["tool_name"] == "interrupt" and kw["hidx"] == ctx.last_ordinal
        (last,) = ctx.records_since(ctx.last_ordinal)
        assert last["content"] == RESTART_NOTICE

    def test_a_new_session_gets_neither(self, tmp_path):
        from agent_cli.context.manager import ContextManager

        ctx = ContextManager(tmp_path, max_context_tokens=10000)
        order, cards = self._wire(ctx)
        assert "card" not in order and cards == [] and ctx.last_ordinal is None


class TestOneQueue:
    @staticmethod
    def _wire(q, *, mail=None):
        """조립 부품 몇을 대역으로 두고 ``q`` 하나로 세션을 묶는다 — ``(stores, wired, main_loop)``.
        ``mail`` 을 주면 메일 배선(`wire_agent_mail`)을 그것으로 바꾼다."""
        spec = _spec(ctx=None, session=SimpleNamespace(session_id="s"))
        loops: list = []
        real_loop = R.MainLoop

        def _loop(**kw):
            loops.append(real_loop(**kw))
            return loops[-1]

        with ExitStack() as st:
            st.enter_context(patch.object(R, "build_snapshot_store"))
            st.enter_context(patch.object(R, "build_hook_runner"))
            st.enter_context(
                patch.object(R, "build_agent_registry", return_value=MagicMock())
            )
            st.enter_context(
                patch("agent_cli.harness.auditor.build_auditor", return_value=None)
            )
            st.enter_context(patch("agent_cli.knobs.apply_all"))
            if mail is not None:
                st.enter_context(patch.object(R, "wire_agent_mail", mail))
            st.enter_context(patch.object(R, "MainLoop", _loop))
            stores = build_session_stores(spec, main_input=q)
            owners = build_session_owners(spec, stores, renderer=MagicMock())
            stores.schedules.start = lambda: None  # 스레드는 띄우지 않는다
            wired = wire_session(spec, stores, owners, surface=_Surface(), policy=None)
        return stores, wired, loops[0]

    def test_schedule_and_loop_ports_reach_the_same_main_input(self, tmp_path):
        """예약 `enqueue`·`MainLoop` 포트·펌프가 **같은 main 앞 사람의 말**에 닿는다 — 각각 넣어 보고 거기서 꺼낸다."""
        from agent_cli.session_slots import set_schedule_registry

        q = MainInput()
        stores = None
        try:
            stores, wired, main_loop = self._wire(q)
            stores.schedules.enqueue("weekly", "⏰ Scheduler", "s-1")
            assert wired.pump.deps.main_input is q  # 펌프가 런을 열 것을 꺼내는 자리
            main_kind_ports = main_loop.ports
            main_kind_ports.enqueue_user_message("hint", "dj")
            # 둘 다 사람 입력과 같은 줄 — 화면 대기열에 보인다
            assert [(i["text"], i["nickname"]) for i in q.snapshot()] == [
                ("weekly", "⏰ Scheduler"),
                ("hint", "dj"),
            ]

            first = q.wait_to_open(0)
            assert (first["text"], first["nickname"]) == ("weekly", "⏰ Scheduler")
            assert (
                main_kind_ports.mailbox.has_inbox
            )  # 턴 경계는 레지스트리의 main 상자 흡수
        finally:
            if stores is not None:
                stores.monitors.stop()
            set_schedule_registry(None)

    def test_the_registry_gets_the_same_post_office(self, tmp_path):
        """레지스트리가 main 앞 사람의 말과 **같은 창구**를 받는다 — 메일과 사람의 말이 한 상자에 서야 펌프가 둘을
        한 자리에서 기다린다."""
        from agent_cli.session_slots import set_schedule_registry

        q = MainInput()
        seen: dict = {}

        def _registry(session_dir, runtime, **kw):
            seen.update(kw)
            return MagicMock()

        stores = None
        try:
            with patch.object(R, "build_agent_registry", _registry):
                spec = _spec(ctx=None, session=SimpleNamespace(session_id="s"))
                with (
                    patch.object(R, "build_snapshot_store"),
                    patch.object(R, "build_hook_runner"),
                    patch("agent_cli.harness.auditor.build_auditor", return_value=None),
                    patch("agent_cli.knobs.apply_all"),
                ):
                    stores = build_session_stores(spec, main_input=q)
                    build_session_owners(spec, stores, renderer=MagicMock())
            assert seen["post"] is q.post
        finally:
            if stores is not None:
                stores.monitors.stop()
            set_schedule_registry(None)


class TestCloseSession:
    @pytest.mark.parametrize("has_stores", [True, False])
    @pytest.mark.parametrize("has_owners", [True, False])
    def test_tolerates_none_and_tears_down_only_what_exists(
        self, has_stores, has_owners
    ):
        """조립 도중 죽은 경우 — 있는 것만 정리하고, MCP 해제·세션 마감은 늘 돈다."""
        spec = _spec()
        stores = (
            SessionStores(monitors="MON", jobs="JOBS", schedules="SCH", main_input="MI")
            if has_stores
            else None
        )
        owners = (
            SessionOwners(agent_registry="REG", auditor="AUD") if has_owners else None
        )
        with patch.object(R, "teardown_session") as td:
            close_session(spec, stores, owners, warn_stuck=has_owners)
        td.assert_called_once()
        assert td.call_args.args == (spec.session, spec.ctx)
        kw = td.call_args.kwargs
        assert kw["mcp_manager"] == "MCP"
        assert kw["warn_stuck"] is has_owners
        assert (kw["monitors"], kw["jobs"], kw["schedules"]) == (
            ("MON", "JOBS", "SCH") if has_stores else (None, None, None)
        )
        assert (kw["agent_registry"], kw["harness"]) == (
            ("REG", "AUD") if has_owners else (None, None)
        )


class TestHostHooks:
    def test_without_host_hooks_nothing_is_attached(self):
        """호스트별 훅(`on_jobs_change`·`on_schedules_change`·`on_harness_change`·
        `on_mail_notice`)을 안 주면 아무것도 대입하지 않는다 — run 이 지금 안 꽂는
        것을 꽂게 되지 않는다."""
        order: list[str] = []
        with ExitStack() as st:
            parts = _patch_parts(st, order)
            auditor_kw: dict = {}
            mail_kw: dict = {}
            st.enter_context(
                patch(
                    "agent_cli.harness.auditor.build_auditor",
                    lambda reg, **kw: auditor_kw.update(kw),
                )
            )
            st.enter_context(
                patch.object(
                    R,
                    "wire_agent_mail",
                    lambda reg, **kw: (mail_kw.update(kw), (0, 0))[1],
                )
            )
            spec = _spec()
            stores = build_session_stores(spec, main_input=MainInput())
            owners = build_session_owners(spec, stores, renderer="R")
            wire_session(
                spec,
                stores,
                owners,
                surface=_Surface(),
                policy=None,
            )
        assert parts.jobs.on_change is None
        assert parts.schedules.on_change is None
        assert parts.schedules.enqueue is not None  # 발화 자리는 늘 묶는다
        assert auditor_kw["on_change"] is None
        assert mail_kw["on_mail_notice"] is None

    def test_host_hooks_are_attached_where_given(self):
        order: list[str] = []
        jobs_cb, sched_cb, harness_cb = object(), object(), object()
        with ExitStack() as st:
            parts = _patch_parts(st, order)
            seen: dict = {}
            st.enter_context(
                patch(
                    "agent_cli.harness.auditor.build_auditor",
                    lambda reg, **kw: seen.update(kw),
                )
            )
            spec = _spec()
            stores = build_session_stores(
                spec,
                main_input=MainInput(),
                on_jobs_change=jobs_cb,
                on_schedules_change=sched_cb,
            )
            build_session_owners(
                spec, stores, renderer="R", on_harness_change=harness_cb
            )
        assert parts.jobs.on_change is jobs_cb
        assert parts.schedules.on_change is sched_cb
        assert seen["on_change"] is harness_cb


class TestSpecHooks:
    def test_disk_hooks_reach_the_registry_runtime_and_the_main_loop(self):
        """`load_hooks` 결과가 `SessionSpec.hooks_config` 로 들어가 `AgentRuntime`
        (상주 에이전트)과 `MainLoop`(main 루프) 양쪽에 닿는다 — v8.39.0 수리(web 이
        훅을 안 실었다)의 계약을 조립 함수에서 고정한다."""
        hooks = {"PostToolUse": ["m"]}
        with patch("agent_cli.hooks.load_hooks", return_value=hooks):
            spec = SessionSpec.for_session(
                binding="B",
                session=SimpleNamespace(session_id="s"),
                ctx=None,
                mcp_manager=None,
                knob_args=None,
                max_turns=0,
                max_depth=2,
                verbose=False,
                record_turns=True,
            )
        assert spec.hooks_config is hooks
        with patch("agent_cli.hooks.load_hooks", return_value={}):
            empty = SessionSpec.for_session(
                binding="B",
                session=None,
                ctx=None,
                mcp_manager=None,
                knob_args=None,
                max_turns=0,
                max_depth=2,
                verbose=False,
                record_turns=True,
            )
        assert empty.hooks_config is None  # 빈 설정은 None — 종전 `or None`

        runtimes: list = []
        loops: list = []
        with (
            patch.object(
                R,
                "build_agent_registry",
                lambda d, rt, **kw: (runtimes.append(rt), MagicMock())[1],
            ),
            patch("agent_cli.harness.auditor.build_auditor", return_value=None),
            patch("agent_cli.knobs.apply_all"),
            patch.object(R, "wire_agent_mail", return_value=(0, 0)),
            patch.object(R, "MainLoop", lambda **kw: loops.append(kw)),
            patch("agent_cli.pump.MainActor"),
        ):
            stores = SessionStores(
                monitors=None,
                jobs=None,
                schedules=MagicMock(name="sch"),
                main_input=MainInput(),
            )
            owners = build_session_owners(spec, stores, renderer="R")
            wire_session(
                spec,
                stores,
                owners,
                surface=_Surface(),
                policy=None,
            )
        assert runtimes[0].hooks_config is hooks
        assert loops[0]["hooks_config"] is hooks


class TestOnePostOfficeForReal:
    """실제 조립(대역 없음)에서 레지스트리의 창구가 main 앞 사람의 말의 창구다 — run 수명(QuietPolicy)과 web 수명
    (ForeverPolicy) 둘 다. 따로 서면 메일이 다른 상자로 가 한가한 펌프가 깨지 않는다."""

    @pytest.mark.parametrize("path", ["run", "web"])
    def test_registry_and_main_input_share_the_post_office(
        self, tmp_path, monkeypatch, path
    ):
        from agent_cli.render import get_renderer, set_renderer
        from tests.oracle.driver import Provider, Session

        before = get_renderer()
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("AGENT_CLI_SNAPSHOT", "0")
        s = Session(tmp_path, Provider({"MAIN": []}), path=path)
        try:
            assert s.reg._post is s.main_input.post
            assert s.wired.pump.deps.main_input is s.main_input
        finally:
            s.close()
            set_renderer(before)

    @pytest.mark.parametrize("path", ["run", "web"])
    def test_the_main_loops_turn_boundary_absorbs_the_main_box(
        self, tmp_path, monkeypatch, path
    ):
        """펌프가 돌리는 main 종류의 턴 경계가 그 상자를 한 번에 흡수한다 — 사람의 말 먼저, 메일 뒤."""
        from agent_cli.render import get_renderer, set_renderer
        from tests.mail_support import to_main
        from tests.oracle.driver import Provider, Session

        before = get_renderer()
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("AGENT_CLI_SNAPSHOT", "0")
        s = Session(tmp_path, Provider({"MAIN": []}), path=path)
        try:
            main = s.wired.pump.deps.main
            to_main(s.reg, {"kind": "job", "output": "job done", "ledger": "none"})
            s.main_input.enqueue(None, "later words")
            got = main.intake()
            assert [next(iter(it)) for it in got] == ["message", "mail"]
            assert got[0]["message"]["text"] == "later words"
            assert (
                s.main_input.pending_count() == 0
                and s.reg.arrived_for("main", waking_only=False, words=False) == 0
            )
        finally:
            s.close()
            set_renderer(before)
