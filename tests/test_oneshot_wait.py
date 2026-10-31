"""one-shot 의 `wait` 는 자기 메일함을 기다린다 (v10.84.0, docs/wait/DESIGN.md §7).

one-shot·하니스 작업은 private 소유자 주소(``run:<dir>``·``harness:<job>``)를 받아
잡·모니터·체크인의 통지가 자기 메일함으로 오고, `wait` 는 런을 끝내는 대신 그
메일함에 통지가 올 때까지 막은 뒤 런을 잇는다. 깨울 길이 없는 루프(스킬)는 `wait`
를 명시 거부한다. 끝날 때는 그 주소가 가진 것을 전부 거둔다(사용자 결정 D1~D3).
"""

from __future__ import annotations

import io
import json
import threading
from unittest.mock import MagicMock

import pytest
from rich.console import Console

from agent_cli.constants import RUN_OWNER_PREFIX, is_private_owner
from agent_cli.context.manager import ContextManager
from agent_cli.dialects import get as get_dialect
from agent_cli.jobs import JobRegistry, set_job_registry
from agent_cli.loop import AgentLoop
from agent_cli.loop.kind import PrivateLoop
from agent_cli.providers.base import LLMResponse
from agent_cli.providers.capabilities import ModelCapabilities
from agent_cli.render.minimal import MinimalRenderer
from agent_cli.runtime import (
    mail_record,
    release_private_owner,
    wake_sources,
)
from agent_cli.subagent.agents_live import set_main_registry
from agent_cli.tools.result import ToolResult
from tests.loop_kinds import make_caller
from tests.loop_ports import TEST_PORTS
from tests.test_agents_live import make_registry


@pytest.fixture
def reg(tmp_path):
    r = make_registry(tmp_path / "reg")
    set_main_registry(r)
    yield r
    set_main_registry(None)


@pytest.fixture
def jobs(tmp_path, reg):
    j = JobRegistry(tmp_path)
    j.deliver = reg.deliver
    set_job_registry(j)
    yield j
    j.stop_all()
    set_job_registry(None)


def _loop(provider, ports, *, ctx=None):
    return AgentLoop(
        ports=ports,
        query="Q",
        query_request_id="",
        provider=provider,
        capabilities=ModelCapabilities(
            context_window=32768, max_output_tokens=4096, supports_thinking=False
        ),
        model="m",
        dialect=get_dialect("json_fc"),
        ctx=ctx,
    )


def _provider(*replies: dict):
    p = MagicMock()
    p.call.side_effect = [LLMResponse(content=json.dumps(r)) for r in replies]
    return p


def _records(tmp_path):
    return [
        json.loads(line)
        for line in (tmp_path / "s" / "history.jsonl").read_text().splitlines()
    ]


class TestAddresses:
    def test_private_is_run_or_harness_only(self):
        assert is_private_owner("run:run_task_1") and is_private_owner(
            "harness:auditor"
        )
        assert not is_private_owner("main") and not is_private_owner("agent:k")
        assert not is_private_owner("foo")  # 모르는 주소는 여전히 unroutable


class TestPrivateMailbox:
    def _mail(self, text="🧵 job #1 finished rc=0", kind="job"):
        return {"kind": kind, "success": True, "output": text}

    def test_deliver_counts_and_take_empties(self, reg):
        assert (
            reg.deliver(
                "run:x", mail=self._mail(), text="t", author="main", expects_reply=False
            )
            == ""
        )
        assert reg.arrived_for("run:x") == 1
        got = reg.take_mail("run:x")
        assert [m["output"] for m in got] == ["🧵 job #1 finished rc=0"]
        assert got[0]["item_id"].startswith("n-")  # main 메일과 같은 도장
        assert reg.arrived_for("run:x") == 0 and reg.take_mail("run:x") == []

    def test_harness_address_is_routable_now(self, reg):
        assert (
            reg.deliver(
                "harness:auditor",
                mail=self._mail(),
                text="t",
                author="main",
                expects_reply=False,
            )
            == ""
        )
        assert reg.arrived_for("harness:auditor") == 1

    def test_unknown_address_is_still_unroutable(self, reg):
        assert "unroutable" in reg.deliver(
            "foo", mail=self._mail(), text="t", author="main", expects_reply=False
        )

    def test_wait_mail_wakes_on_arrival(self, reg):
        def later():
            reg.deliver(
                "run:x", mail=self._mail(), text="t", author="main", expects_reply=False
            )

        threading.Timer(0.3, later).start()
        why = reg.wait_mail(
            "run:x",
            stop_event=threading.Event(),
            still_waiting=lambda: True,
            poll_s=0.05,
        )
        assert why == "mail" and reg.arrived_for("run:x") == 1

    def test_wait_mail_returns_on_stop_and_on_nothing_left(self, reg):
        ev = threading.Event()
        ev.set()
        assert (
            reg.wait_mail(
                "run:x", stop_event=ev, still_waiting=lambda: True, poll_s=0.05
            )
            == "stopped"
        )
        assert (
            reg.wait_mail(
                "run:x",
                stop_event=threading.Event(),
                still_waiting=lambda: False,
                poll_s=0.05,
            )
            == "nothing"
        )

    def test_drop_mailbox_discards_and_cancels_checkin(self, reg, jobs):
        jobs.start("sleep 30", owner="run:x")
        assert reg.arm_checkin("run:x")
        reg.deliver(
            "run:x", mail=self._mail(), text="t", author="main", expects_reply=False
        )
        assert reg.drop_mailbox("run:x") == 1
        assert reg.arrived_for("run:x") == 0
        assert reg.fire_checkin("run:x") == ""  # 걸린 것이 풀렸다

    def test_checkin_lands_in_the_private_mailbox(self, reg, jobs):
        jobs.start("sleep 30", owner="run:x")
        assert reg.arm_checkin("run:x")
        text = reg.fire_checkin("run:x")
        assert "still running" in text
        got = reg.take_mail("run:x")
        assert (
            len(got) == 1 and got[0]["kind"] == "checkin" and got[0]["ledger"] == "none"
        )


class TestMailRecord:
    def test_harness_notice_is_the_bare_report_with_the_kinds_tool(self):
        rec = mail_record(
            {
                "kind": "job",
                "success": False,
                "output": "🧵 job #1 finished rc=1",
                "item_id": "n-7",
            }
        )["record"]
        assert (
            rec["tool"] == "job" and rec["success"] is False and rec["role"] == "user"
        )
        assert rec["content"] == "🧵 job #1 finished rc=1"  # 봉투 없음 — main 과 같다
        assert rec["source"] == "harness_mail"

    def test_checkin_is_the_wait_tool(self):
        rec = mail_record({"kind": "checkin", "output": "⏰ Check-in after 5m"})[
            "record"
        ]
        assert rec["tool"] == "wait" and rec["content"] == "⏰ Check-in after 5m"

    def test_unknown_kind_falls_back_to_the_kind_name(self):
        rec = mail_record({"text": "hello"})["record"]
        assert rec["tool"] == "mail" and rec["content"] == "hello"
        assert rec["success"] is True


class TestOneContractForHarnessNotices:
    """사용자 결정 C: 하니스 통지는 main·상주·one-shot 어디서든 같은 레코드 —
    봉투 없이 보고 그대로, tool 은 `HARNESS_MAIL_TOOL`. 종전 상주는 `deliver` 가 kind 를
    떨어뜨려 잡·모니터가 `agent` 레코드였고 체크인만 봉투였다."""

    def _busy(self, tmp_path):
        from tests.test_agents_live import make_runner, wait_until

        gate = threading.Event()
        reg = make_registry(tmp_path / "busy", runner=make_runner(block=gate))
        key, _ = reg.spawn()
        wait_until(lambda: reg.get(key).state == "idle")
        assert reg.request(key, "첫 일감", author="main") == ""
        assert wait_until(lambda: reg.get(key).current_item == "r-1")
        return reg, key, gate

    def test_job_mail_to_a_resident_absorbs_like_main_receives_it(self, tmp_path):
        from agent_cli.subagent.agents_live import build_reply_record

        reg, key, gate = self._busy(tmp_path)
        mail = {"kind": "job", "success": True, "output": "🧵 job #1 finished rc=0"}
        err = reg.deliver(
            f"agent:{key}",
            mail=dict(mail),
            text=mail["output"],
            author="main",
            expects_reply=False,
        )
        assert err == ""
        rec = reg.absorb_pending(reg.get(key))[0]["record"]
        main_rec = build_reply_record(dict(mail, item_id="n-9"))
        assert (rec["tool"], rec["content"]) == (main_rec["tool"], main_rec["content"])
        assert rec["tool"] == "job" and rec["content"] == "🧵 job #1 finished rc=0"
        assert rec["source"] == "harness_mail"
        gate.set()
        reg.shutdown_all()

    def test_checkin_to_a_resident_is_a_wait_record_without_envelope(self, tmp_path):
        reg, key, gate = self._busy(tmp_path)
        err = reg.request(
            key,
            "⏰ Check-in after 5m — still running: …",
            author="main",
            expects_reply=False,
            kind="checkin",
            ledger="none",
        )
        assert err == ""
        rec = reg.absorb_pending(reg.get(key))[0]["record"]
        assert rec["tool"] == "wait" and rec["content"].startswith(
            "⏰ Check-in after 5m"
        )
        gate.set()
        reg.shutdown_all()

    def test_peer_mail_keeps_the_agent_record(self, tmp_path):
        reg, key, gate = self._busy(tmp_path)
        assert (
            reg.request(key, "회신 본문", author="agent:peer", expects_reply=False)
            == ""
        )
        rec = reg.absorb_pending(reg.get(key))[0]["record"]
        assert rec["tool"] == "agent" and rec["source"] == "agent_mail"
        gate.set()
        reg.shutdown_all()


class TestWakeItemsForPrivateOwners:
    def test_schedules_do_not_wake_a_private_owner(self, monkeypatch, reg):
        set_job_registry(None)

        class Sched:
            def has_active_work(self):
                return True

        monkeypatch.setattr(
            "agent_cli.schedule.runtime.get_schedule_registry", lambda: Sched()
        )
        assert "a schedule" in wake_sources("main")
        assert (
            wake_sources("run:x") == []
        )  # 빼지 않으면 잡 없이도 wait 가 영원히 막힌다


class TestWaitInPlace:
    def test_oneshot_wait_blocks_until_its_job_reports_then_continues(
        self, jobs, tmp_path
    ):
        jobs.start("sleep 1", owner="run:t1")
        ctx = ContextManager(tmp_path / "s", max_context_tokens=10000)
        loop = _loop(
            _provider(
                {"action": "wait", "reason": "job"},
                {"action": "complete", "result": "done"},
            ),
            PrivateLoop.make_ports("run:t1"),
            ctx=ctx,
        )
        assert "wait" in loop.tools_list  # 깨울 길이 있으니 광고된다
        res = loop.run()
        assert res.success and not getattr(res, "waiting", False)
        assert res.output == "done" and loop.turn == 2
        recs = _records(tmp_path)
        waits = [r for r in recs if r.get("tool") == "wait"]
        assert waits and "will wake on: job #1" in waits[0]["content"]
        assert "next run" not in waits[0]["content"]  # 장부를 넘기지 않는다
        job_mail = [r for r in recs if r.get("tool") == "job"]
        assert job_mail and "job #1 finished" in job_mail[0]["content"]
        assert ctx.take_parked_requests() == []

    def test_job_killed_from_outside_ends_the_wait_with_a_fact(self, jobs, tmp_path):
        job = jobs.start("sleep 30", owner="run:t2")
        ctx = ContextManager(tmp_path / "s", max_context_tokens=10000)
        loop = _loop(
            _provider({"action": "wait"}, {"action": "complete", "result": "done"}),
            PrivateLoop.make_ports("run:t2"),
            ctx=ctx,
        )
        threading.Timer(0.4, lambda: jobs.kill(job)).start()
        res = loop.run()
        assert res.success and res.output == "done"
        texts = [r["content"] for r in _records(tmp_path) if r.get("tool") == "wait"]
        assert any(
            "nothing you were waiting on is running any more" in t for t in texts
        )


class TestInlineLoopsRefuseWait:
    def test_unwakeable_loop_refuses_and_goes_on(self, jobs, tmp_path):
        jobs.start("sleep 30", owner="main")
        ctx = ContextManager(tmp_path / "s", max_context_tokens=10000)
        loop = _loop(
            _provider({"action": "wait"}, {"action": "complete", "result": "done"}),
            TEST_PORTS,
            ctx=ctx,
        )
        assert "wait" not in loop.tools_list
        res = loop.run()
        assert res.success and res.output == "done" and loop.turn == 2
        texts = [r["content"] for r in _records(tmp_path) if r.get("tool") == "wait"]
        assert texts and "runs inline inside its caller" in texts[0]
        assert "finish with `complete` and the caller waits" in texts[0]


class TestReleasePrivateOwner:
    def test_leftover_job_is_stopped_and_named(self, jobs, reg):
        jobs.start("sleep 30", owner="run:z")
        reg.deliver(
            "run:z",
            mail={"kind": "job", "output": "x"},
            text="x",
            author="main",
            expects_reply=False,
        )
        note = release_private_owner("run:z")
        assert (
            note
            == "[1 background job still running was stopped when this agent finished: `sleep 30`]"
        )
        assert jobs.running("run:z") == [] and reg.arrived_for("run:z") == 0

    def test_nothing_left_is_silent(self, jobs, reg):
        assert release_private_owner("run:empty") == ""


class TestOneShotHasItsOwnAddress:
    def test_run_single_uses_a_run_address_and_cleans_up(self, monkeypatch, jobs, reg):
        from agent_cli.subagent.oneshot import OneShotLoop
        from tests.binding_support import make_binding

        seen = {}

        def fake_run_loop(**kwargs):
            ports = kwargs["ports"]
            seen["owner"] = ports.owner
            seen["absorb"] = ports.absorb_inbox
            jobs.start("sleep 30", owner=ports.owner)  # 서브에이전트가 잡을 남기고 끝남
            return ToolResult(True, output="done")

        monkeypatch.setattr("agent_cli.loop.run_loop", fake_run_loop)
        caps = ModelCapabilities(
            context_window=32768, max_output_tokens=4096, supports_thinking=False
        )
        result = OneShotLoop(
            {"task": "go"},
            make_caller(
                binding=make_binding(provider=object(), model="m", capabilities=caps)
            ),
        ).run()
        assert result.success
        assert seen["owner"].startswith(RUN_OWNER_PREFIX) and seen["absorb"] is not None
        assert jobs.running(seen["owner"]) == []
        assert "1 background job still running was stopped" in result.output


class TestParallelPanelMarkup:
    def test_task_text_with_rich_tags_does_not_raise(self):
        r = MinimalRenderer(Console(file=io.StringIO(), force_terminal=False))
        r.begin_scope(task_id="t1", index=0, agent="", label="print [/red] and [b]")
        r.end_scope(
            task_id="t1", success=True, duration_s=0.1
        )  # MarkupError 였다 (v10.65.0 벤치)
