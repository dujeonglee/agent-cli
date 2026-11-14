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
from agent_cli.input_queue import InputQueue
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
        "ctx": SimpleNamespace(session_dir="/sess"),
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
    st.enter_context(
        patch.object(R, "wire_agent_mail", rec("mail", lambda: ("WAKER", 1, 2)))
    )

    def _mainloop(**kw):
        order.append("MainLoop")
        if mainloop_boom:
            raise RuntimeError("MainLoop failed")
        return MagicMock(name="main_kind")

    st.enter_context(patch.object(R, "MainLoop", _mainloop))
    st.enter_context(
        patch("agent_cli.pump.SessionPump", rec("pump", lambda: MagicMock()))
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
            stores = build_session_stores(spec, queue=InputQueue())
            owners = build_session_owners(spec, stores, renderer="R")
            wired = wire_session(
                spec,
                stores,
                owners,
                queue=InputQueue(),
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
            stores = build_session_stores(spec, queue=InputQueue())
            owners = build_session_owners(spec, stores, renderer="R")
            with pytest.raises(RuntimeError):
                wire_session(
                    spec,
                    stores,
                    owners,
                    queue=InputQueue(),
                    surface=_Surface(),
                    policy=None,
                )
        assert "MainLoop" in order and "schedules.start" not in order
        assert "pump" not in order


class TestOneQueue:
    def test_schedule_wake_and_loop_ports_reach_the_same_queue(self, tmp_path):
        """예약 `enqueue`·깨우기·`MainLoop` 포트·펌프가 **같은 큐 객체**에 닿는다 —
        각각 넣어 보고 그 큐에서 꺼낸다."""
        from agent_cli.session_slots import set_schedule_registry

        q = InputQueue()
        spec = _spec(ctx=None, session=SimpleNamespace(session_id="s"))
        captured: dict = {}

        def _mail(registry, **kw):
            captured.update(kw)
            return MagicMock(WAKE_TEXT="WAKE"), 0, 0

        loops: list = []
        real_loop = R.MainLoop

        def _loop(**kw):
            loops.append(real_loop(**kw))
            return loops[-1]

        try:
            with (
                patch.object(R, "build_snapshot_store"),
                patch.object(R, "build_hook_runner"),
                patch.object(R, "build_agent_registry", return_value=MagicMock()),
                patch("agent_cli.harness.auditor.build_auditor", return_value=None),
                patch("agent_cli.knobs.apply_all"),
                patch.object(R, "wire_agent_mail", _mail),
                patch.object(R, "MainLoop", _loop),
            ):
                stores = build_session_stores(spec, queue=q)
                owners = build_session_owners(spec, stores, renderer=MagicMock())
                stores.schedules.start = lambda: None  # 스레드는 띄우지 않는다
                wired = wire_session(
                    spec, stores, owners, queue=q, surface=_Surface(), policy=None
                )
            stores.schedules.enqueue("weekly", "⏰ Scheduler")
            captured["enqueue_wake"](None, "WAKE")
            assert wired.pump.deps.queue is q  # 펌프가 꺼내는 큐
            main_kind_ports = loops[0].ports
            main_kind_ports.enqueue_user_message("hint", "dj")

            first = q.dequeue_nowait()
            assert (first["text"], first["nickname"], first["system"]) == (
                "weekly",
                "⏰ Scheduler",
                False,
            )
            wake = q.dequeue_nowait()
            assert (wake["text"], wake["nickname"], wake["system"]) == (
                "WAKE",
                None,
                True,
            )
            assert main_kind_ports.dequeue_user_message()["text"] == "hint"
            assert q.dequeue_nowait() is None
        finally:
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
            SessionStores(monitors="MON", jobs="JOBS", schedules="SCH")
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
                    lambda reg, **kw: (mail_kw.update(kw), ("W", 0, 0))[1],
                )
            )
            spec = _spec()
            stores = build_session_stores(spec, queue=InputQueue())
            owners = build_session_owners(spec, stores, renderer="R")
            wire_session(
                spec,
                stores,
                owners,
                queue=InputQueue(),
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
                queue=InputQueue(),
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
            patch.object(R, "wire_agent_mail", return_value=("W", 0, 0)),
            patch.object(R, "MainLoop", lambda **kw: loops.append(kw)),
            patch("agent_cli.pump.SessionPump"),
        ):
            stores = SessionStores(
                monitors=None, jobs=None, schedules=MagicMock(name="sch")
            )
            owners = build_session_owners(spec, stores, renderer="R")
            wire_session(
                spec,
                stores,
                owners,
                queue=InputQueue(),
                surface=_Surface(),
                policy=None,
            )
        assert runtimes[0].hooks_config is hooks
        assert loops[0]["hooks_config"] is hooks
