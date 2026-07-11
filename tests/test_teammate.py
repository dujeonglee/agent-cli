"""teammate P1 계약 (docs/teammate/DESIGN.md §6.1).

레지스트리·worker 스레딩은 가짜 러너(DI seam)로 LLM 없이 검증한다.
핵심 계약: 배달 레코드가 형식-개입(fold, v4.51.0)으로 오인되지 않을 것,
스코프는 worker 스레드가 상시 보유할 것, main 인터럽트와 teammate
stop 이 분리될 것.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

import agent_cli.render as render_mod
from agent_cli.context.records import is_format_intervention
from agent_cli.resource_loader import ResourceLoader
from agent_cli.subagent.teammate import (
    TeammateRegistry,
    build_reply_record,
    tool_teammate,
)


def wait_until(pred, timeout: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.01)
    return False


class RecordingRenderer:
    """worker 가 부르는 렌더러 표면만 흉내 — 호출 순서를 기록."""

    def __init__(self):
        self.calls = []
        self.lock = threading.Lock()

    def _rec(self, name, **kw):
        with self.lock:
            self.calls.append((name, kw))

    def begin_prompt_scope(self, scope_id, label=""):
        self._rec("begin_prompt_scope", scope=scope_id, label=label)

    def end_prompt_scope(self, scope_id):
        self._rec("end_prompt_scope", scope=scope_id)

    def note_scope_ctx(self, ctx):
        self._rec("note_scope_ctx", ctx=ctx)

    def begin_teammate_work(self, **kw):
        self._rec("begin_teammate_work", **kw)

    def end_teammate_work(self, **kw):
        self._rec("end_teammate_work", **kw)

    def named(self, name):
        with self.lock:
            return [c for c in self.calls if c[0] == name]


@pytest.fixture
def renderer(monkeypatch):
    r = RecordingRenderer()
    monkeypatch.setattr(render_mod, "get_renderer", lambda: r)
    return r


class _FakeLoopResult:
    def __init__(self, success=True, output="done"):
        self.success = success
        self.output = output


def make_runner(reply_text="done", block: threading.Event | None = None, exc=None):
    """가짜 run_subagent_message — query 를 echo 하는 회신."""

    def runner(query, ctx, **kwargs):
        if block is not None:
            block.wait(3)
        if exc is not None:
            raise exc
        return _FakeLoopResult(output=f"{reply_text}:{query}"), 0.01

    return runner


def make_registry(tmp_path, *, runner=None, **runtime):
    reg = TeammateRegistry(
        tmp_path,
        runtime={"model": "m", "timeout": 5, **runtime},
        runner=runner or make_runner(),
    )
    return reg


# ── 역할 로더 (D11(b)) ──────────────────────────


class TestRolesLoader:
    def _swap_loader(self, monkeypatch, tmp_path):
        import agent_cli.subagent.roles as roles_mod

        monkeypatch.setattr(roles_mod, "_teammate_loader", ResourceLoader([tmp_path]))
        return roles_mod

    def test_loads_role_body_and_config(self, monkeypatch, tmp_path):
        (tmp_path / "researcher.md").write_text(
            "---\nallowed-tools:\n  - read_file\n---\nYou are a researcher.",
            encoding="utf-8",
        )
        roles = self._swap_loader(monkeypatch, tmp_path)
        body, config, error = roles.load_teammate_role("researcher")
        assert error is None
        assert "researcher" in body
        assert config["allowed-tools"] == ["read_file"]

    def test_missing_role_reports_search_paths(self, monkeypatch, tmp_path):
        roles = self._swap_loader(monkeypatch, tmp_path)
        body, config, error = roles.load_teammate_role("nope")
        assert body is None
        assert "not found" in error
        assert "teammates" in error  # agents/ 가 아니라 전용 디렉토리 안내

    def test_invalid_name_rejected(self, monkeypatch, tmp_path):
        roles = self._swap_loader(monkeypatch, tmp_path)
        _, _, error = roles.load_teammate_role("../hack")
        assert "Invalid" in error


# ── 배달 레코드 계약 ────────────────────────────


class TestBuildReplyRecord:
    def _reply(self, **kw):
        return {
            "key": "agt-abc",
            "role": "researcher",
            "seq": 1,
            "success": True,
            "output": "findings",
            "duration_s": 0.5,
            "reply_path": "/tmp/x/reply-1.md",
            **kw,
        }

    def test_record_shape(self):
        rec = build_reply_record(self._reply())
        assert rec["role"] == "user"
        assert rec["tool"] == "teammate"  # tool="" 금지 (형식-개입 마커)
        assert rec["success"] is True
        assert rec["source"] == "teammate_reply"
        assert "agt-abc (researcher)" in rec["content"]
        assert "findings" in rec["content"]

    def test_never_mistaken_for_format_intervention(self):
        # v4.51.0 fold 가 이 레코드를 접으면 회신이 증발한다 — 핵심 계약.
        ok = build_reply_record(self._reply())
        failed = build_reply_record(self._reply(success=False))
        assert not is_format_intervention(ok)
        assert not is_format_intervention(failed)

    def test_failure_reply_marked(self):
        rec = build_reply_record(self._reply(success=False, output="boom"))
        assert rec["success"] is False
        assert "(error)" in rec["content"]

    def test_over_cap_replaced_with_pointer(self):
        big = "x" * 40_000  # ~10k tokens
        rec = build_reply_record(self._reply(output=big), cap=100)
        assert "reply-1.md" in rec["content"]
        assert len(rec["content"]) < len(big)
        assert "head excerpt" in rec["content"]


# ── 레지스트리 수명 ─────────────────────────────


class TestRegistryLifecycle:
    def test_spawn_returns_key_and_goes_idle(self, tmp_path, renderer):
        reg = make_registry(tmp_path)
        key, err = reg.spawn()
        assert err == "" and key.startswith("agt-")
        assert wait_until(lambda: reg.get(key).state == "idle")
        reg.shutdown_all()

    def test_spawn_fork_without_parent_rejected(self, tmp_path, renderer):
        reg = make_registry(tmp_path)
        key, err = reg.spawn(context_mode="fork", parent_ctx=None)
        assert key == "" and "fork" in err

    def test_spawn_without_session_dir_rejected(self, renderer):
        reg = TeammateRegistry(None, runner=make_runner())
        key, err = reg.spawn()
        assert key == "" and "session dir" in err

    def test_spawn_limit(self, tmp_path, renderer, monkeypatch):
        monkeypatch.setenv("AGENT_CLI_MAX_TEAMMATES", "1")
        reg = make_registry(tmp_path)
        key, err = reg.spawn()
        assert err == ""
        key2, err2 = reg.spawn()
        assert key2 == "" and "limit" in err2
        reg.shutdown_all()

    def test_unknown_role_rejected(self, tmp_path, renderer, monkeypatch):
        import agent_cli.subagent.roles as roles_mod

        monkeypatch.setattr(
            roles_mod, "_teammate_loader", ResourceLoader([tmp_path / "empty"])
        )
        reg = make_registry(tmp_path)
        key, err = reg.spawn(role="ghost")
        assert key == "" and "not found" in err

    def test_request_reply_roundtrip(self, tmp_path, renderer):
        reg = make_registry(tmp_path)
        key, _ = reg.spawn()
        assert reg.request(key, "hello") == ""
        assert wait_until(reg.has_pending_replies)
        replies = reg.drain_replies()
        assert len(replies) == 1
        r = replies[0]
        assert r["key"] == key and r["success"] and r["output"] == "done:hello"
        assert not reg.has_pending_replies()  # drain 은 소비
        reg.shutdown_all()

    def test_reply_persisted_to_disk(self, tmp_path, renderer):
        reg = make_registry(tmp_path)
        key, _ = reg.spawn()
        reg.request(key, "hi")
        assert wait_until(reg.has_pending_replies)
        reply = reg.drain_replies()[0]
        path = Path(reply["reply_path"])
        assert path.is_file()
        assert path.read_text(encoding="utf-8") == "done:hi"
        assert "teammates" in str(path) and key in str(path)
        reg.shutdown_all()

    def test_on_reply_callback_fires(self, tmp_path, renderer):
        reg = make_registry(tmp_path)
        seen = []
        reg.on_reply = seen.append
        key, _ = reg.spawn()
        reg.request(key, "ping")
        assert wait_until(lambda: len(seen) == 1)
        assert seen[0]["key"] == key
        reg.shutdown_all()

    def test_request_unknown_and_empty(self, tmp_path, renderer):
        reg = make_registry(tmp_path)
        assert "unknown" in reg.request("agt-nope", "x")
        key, _ = reg.spawn()
        assert "empty" in reg.request(key, "   ")
        reg.shutdown_all()

    def test_busy_state_while_processing(self, tmp_path, renderer):
        gate = threading.Event()
        reg = make_registry(tmp_path, runner=make_runner(block=gate))
        key, _ = reg.spawn()
        wait_until(lambda: reg.get(key).state == "idle")
        reg.request(key, "long job")
        assert wait_until(lambda: reg.get(key).state == "busy")
        gate.set()
        assert wait_until(lambda: reg.get(key).state == "idle")
        reg.shutdown_all()

    def test_runner_exception_becomes_failure_reply(self, tmp_path, renderer):
        reg = make_registry(tmp_path, runner=make_runner(exc=RuntimeError("boom")))
        key, _ = reg.spawn()
        reg.request(key, "x")
        assert wait_until(reg.has_pending_replies)
        r = reg.drain_replies()[0]
        assert not r["success"] and "boom" in r["output"]
        # worker 는 죽지 않는다 — 다음 요청도 받는다
        assert reg.get(key).state == "idle"
        reg.shutdown_all()

    def test_kill_and_idempotence(self, tmp_path, renderer):
        reg = make_registry(tmp_path)
        key, _ = reg.spawn()
        wait_until(lambda: reg.get(key).state == "idle")
        assert reg.kill(key) == ""
        assert wait_until(lambda: reg.get(key).state == "dead")
        assert reg.kill(key) == ""  # 멱등
        assert "dead" in reg.request(key, "hello")  # 사후 요청 거부
        assert "unknown" in reg.kill("agt-nope")

    def test_main_stop_does_not_touch_teammates(self, tmp_path, renderer):
        # 인터럽트 분리 계약 — teammate 는 자기 stop_event 만 본다.
        reg = make_registry(tmp_path)
        key, _ = reg.spawn()
        wait_until(lambda: reg.get(key).state == "idle")
        main_stop = threading.Event()
        main_stop.set()  # main 인터럽트가 발생해도
        assert reg.get(key).state == "idle"  # teammate 무영향
        assert not reg.get(key).stop_event.is_set()
        reg.shutdown_all()

    def test_shutdown_all(self, tmp_path, renderer):
        reg = make_registry(tmp_path)
        k1, _ = reg.spawn()
        k2, _ = reg.spawn()
        reg.shutdown_all()
        assert reg.get(k1).state == "dead"
        assert reg.get(k2).state == "dead"
        assert reg.alive_count() == 0


# ── wait / 스코프 / status ──────────────────────


class TestWaitAndScope:
    def test_wait_reply_returns_matching_only(self, tmp_path, renderer):
        reg = make_registry(tmp_path)
        k1, _ = reg.spawn()
        k2, _ = reg.spawn()
        reg.request(k2, "other")  # 다른 teammate 의 회신이 먼저 도착해도
        assert wait_until(reg.has_pending_replies)
        reg.request(k1, "mine")
        got = reg.wait_reply(k1, timeout=3)
        assert got is not None and got["key"] == k1
        # k2 의 회신은 pending 에 남아 다음 턴 경계에 정상 배달된다
        leftover = reg.drain_replies()
        assert [r["key"] for r in leftover] == [k2]
        reg.shutdown_all()

    def test_wait_reply_timeout(self, tmp_path, renderer):
        reg = make_registry(tmp_path)
        key, _ = reg.spawn()
        assert reg.wait_reply(key, timeout=0.1) is None
        reg.shutdown_all()

    def test_worker_owns_persistent_scope(self, tmp_path, renderer):
        # D9: 스코프는 worker 시작 시 1회 push, kill 시에만 고정 —
        # 요청 사이에도 살아 있다 (delegate 의 요청별 스코프와 다른 점).
        reg = make_registry(tmp_path)
        key, _ = reg.spawn(role="")
        wait_until(lambda: reg.get(key).state == "idle")
        begins = renderer.named("begin_prompt_scope")
        assert [c[1]["scope"] for c in begins] == [key]
        assert begins[0][1]["label"] == "teammate:anon"
        assert renderer.named("note_scope_ctx")  # live ctx 등록
        reg.request(key, "one")
        assert wait_until(lambda: reg.get(key).handled == 1)
        assert renderer.named("end_prompt_scope") == []  # 요청 후에도 유지
        reg.kill(key)
        assert wait_until(lambda: bool(renderer.named("end_prompt_scope")))
        assert renderer.named("end_prompt_scope")[0][1]["scope"] == key

    def test_per_request_work_markers(self, tmp_path, renderer):
        reg = make_registry(tmp_path)
        key, _ = reg.spawn()
        reg.request(key, "job")
        assert wait_until(lambda: bool(renderer.named("end_teammate_work")))
        begin = renderer.named("begin_teammate_work")[0][1]
        end = renderer.named("end_teammate_work")[0][1]
        assert begin["key"] == key and begin["seq"] == 1 and begin["message"] == "job"
        assert end["key"] == key and end["success"] is True
        reg.shutdown_all()

    def test_format_status(self, tmp_path, renderer):
        reg = make_registry(tmp_path)
        assert "no teammates" in reg.format_status()
        key, _ = reg.spawn()
        wait_until(lambda: reg.get(key).state == "idle")
        s = reg.format_status()
        assert key in s and "idle" in s and "anon" in s
        assert "unknown" in reg.format_status("agt-nope")
        reg.shutdown_all()


# ── 도구 진입점 ─────────────────────────────────


class TestToolTeammate:
    def test_no_registry_rejected(self):
        r = tool_teammate({"mode": "spawn"}, registry=None)
        assert not r.success and "unavailable" in r.error

    def test_spawn_with_initial_task(self, tmp_path, renderer):
        reg = make_registry(tmp_path)
        r = tool_teammate(
            {"mode": "spawn", "task": "explore"}, registry=reg, runtime=reg.runtime
        )
        assert r.success
        assert "spawned teammate 'agt-" in r.output
        assert "initial task queued" in r.output
        assert wait_until(reg.has_pending_replies)
        reg.shutdown_all()

    def test_request_and_wait_flow(self, tmp_path, renderer):
        reg = make_registry(tmp_path)
        key, _ = reg.spawn()
        r = tool_teammate(
            {"mode": "request", "key": key, "message": "go"}, registry=reg
        )
        assert r.success and "queued" in r.output
        w = tool_teammate({"mode": "wait", "key": key}, registry=reg)
        assert w.success and "done:go" in w.output
        assert w.artifact.endswith("reply-1.md")
        reg.shutdown_all()

    def test_wait_timeout_is_actionable_failure(self, tmp_path, renderer):
        reg = make_registry(tmp_path)
        reg.runtime["timeout"] = 0.1
        key, _ = reg.spawn()
        r = tool_teammate({"mode": "wait", "key": key}, registry=reg)
        assert not r.success and "still" in r.error
        reg.shutdown_all()

    def test_status_and_kill_modes(self, tmp_path, renderer):
        reg = make_registry(tmp_path)
        key, _ = reg.spawn()
        wait_until(lambda: reg.get(key).state == "idle")
        s = tool_teammate({"mode": "status"}, registry=reg)
        assert s.success and key in s.output
        k = tool_teammate({"mode": "kill", "key": key}, registry=reg)
        assert k.success and "terminated" in k.output
        assert wait_until(lambda: reg.get(key).state == "dead")

    def test_unknown_mode(self, tmp_path, renderer):
        reg = make_registry(tmp_path)
        r = tool_teammate({"mode": "dance"}, registry=reg)
        assert not r.success and "unknown mode" in r.error


# ── 스키마 의미론 검증 (C7) ─────────────────────


class TestTeammateToolValidate:
    def _tool(self):
        from agent_cli.tools.registry import TOOLS

        return TOOLS["teammate"]

    def test_registered_in_tools(self):
        assert self._tool().name == "teammate"

    def test_mode_enum(self):
        assert "unknown mode" in self._tool().validate({"mode": "fly"})
        assert self._tool().validate({"mode": "spawn"}) is None
        assert self._tool().validate({"mode": "status"}) is None

    def test_mode_conditional_required(self):
        t = self._tool()
        assert "requires" in t.validate({"mode": "request", "key": "agt-1"})
        assert "requires" in t.validate({"mode": "request", "message": "x"})
        assert t.validate({"mode": "request", "key": "agt-1", "message": "x"}) is None
        assert "requires" in t.validate({"mode": "wait"})
        assert "requires" in t.validate({"mode": "kill", "key": "  "})


# ── 루프 배선 ───────────────────────────────────


class TestLoopWiring:
    def test_tool_stripped_without_registry(self):
        from unittest.mock import MagicMock

        from agent_cli.loop import AgentLoop

        loop = AgentLoop(query="Q", provider=MagicMock(), capabilities=None, model="m")
        assert "teammate" not in loop._config.tools_list

    def test_tool_present_with_registry(self, tmp_path):
        from unittest.mock import MagicMock

        from agent_cli.loop import AgentLoop

        reg = TeammateRegistry(tmp_path, runner=make_runner())
        loop = AgentLoop(
            query="Q",
            provider=MagicMock(),
            capabilities=None,
            model="m",
            teammate_registry=reg,
        )
        assert "teammate" in loop._config.tools_list

    def test_deliver_teammate_replies_injects_records(self, tmp_path, renderer):
        # 턴 경계 배달 (D2): unbound 메서드를 fake self 로 구동 — 레지스트리
        # pending → messages + ctx.add, 형식-개입 오인 없음.
        from types import SimpleNamespace

        from agent_cli.loop.core import AgentLoop

        reg = make_registry(tmp_path)
        key, _ = reg.spawn()
        reg.request(key, "job")
        assert wait_until(reg.has_pending_replies)

        added = []
        fake_ctx = SimpleNamespace(add=lambda rec: added.append(rec) or rec)
        fake_self = SimpleNamespace(
            _config=SimpleNamespace(teammate_registry=reg),
            messages=[],
            ctx=fake_ctx,
            turn=3,
            _oversized_cap=10_000,
        )
        AgentLoop._deliver_teammate_replies(fake_self)

        assert len(fake_self.messages) == 1
        assert "done:job" in fake_self.messages[0]["content"]
        assert len(added) == 1
        assert added[0]["tool"] == "teammate"
        assert not is_format_intervention(added[0])
        assert not reg.has_pending_replies()  # 소비 완료
        # 레지스트리 없으면 no-op
        fake_self2 = SimpleNamespace(
            _config=SimpleNamespace(teammate_registry=None), messages=[]
        )
        AgentLoop._deliver_teammate_replies(fake_self2)
        assert fake_self2.messages == []
        reg.shutdown_all()


# ── 풀-루프 통합 (스크립트된 LLM) ───────────────


class TestFullLoopIntegration:
    """실제 AgentLoop 관통: 파싱 → 중앙 validate → tool_bridge 인터셉트 →
    spawn/request → 턴 경계 배달 → 다음 턴 프롬프트에 회신 노출."""

    def _caps(self):
        from agent_cli.providers.capabilities import ModelCapabilities

        return ModelCapabilities(
            context_window=32768,
            max_output_tokens=4096,
            supports_structured_output=True,
            supports_thinking=False,
            thinking_budget=0,
            supports_strict_schema=False,
        )

    def test_spawn_request_delivery_roundtrip(self, tmp_path):
        import json
        from unittest.mock import MagicMock

        from agent_cli.context.manager import ContextManager
        from agent_cli.loop import run_loop
        from agent_cli.providers.base import LLMResponse

        def emit(action, action_input):
            return json.dumps(
                {"thought": "t", "action": action, "action_input": action_input}
            )

        ctx = ContextManager(tmp_path / "sess", max_context_tokens=30_000)
        reg = TeammateRegistry(tmp_path / "sess", runner=make_runner())

        provider = MagicMock()

        def scripted(*args, **kwargs):
            n = provider.call.call_count
            if n == 1:
                return LLMResponse(
                    content=emit(
                        "teammate", {"mode": "spawn", "task": "explore the repo"}
                    )
                )
            # 배달을 기다렸다가 complete — 실전에선 다른 작업을 하는 턴.
            wait_until(reg.has_pending_replies)
            if n == 2:
                return LLMResponse(content=emit("teammate", {"mode": "status"}))
            return LLMResponse(content=emit("complete", {"result": "finished"}))

        provider.call = MagicMock(side_effect=scripted)

        result = run_loop(
            query="use a teammate",
            provider=provider,
            capabilities=self._caps(),
            model="test-model",
            ctx=ctx,
            teammate_registry=reg,
        )
        assert result.success and result.output == "finished"

        # 회신이 턴 경계에서 ctx 레코드로 배달됐고 fold 오인이 없다
        delivered = [
            m for m in ctx.get_raw_messages() if m.get("source") == "teammate_reply"
        ]
        assert len(delivered) == 1
        assert "done:explore the repo" in delivered[0]["content"]
        assert not is_format_intervention(delivered[0])

        # 배달 이후 LLM 호출의 프롬프트에 회신이 실제로 노출됐다
        later_calls = provider.call.call_args_list[2:]
        assert any("done:explore the repo" in str(c) for c in later_calls), (
            "delivered reply never reached a later LLM prompt"
        )
        reg.shutdown_all()

    def test_teammate_invisible_in_subagent_prompt(self, tmp_path):
        # P1 경계: 레지스트리 없는 루프의 시스템 프롬프트에 teammate 부재.

        from unittest.mock import MagicMock

        from agent_cli.loop import AgentLoop

        loop = AgentLoop(query="Q", provider=MagicMock(), capabilities=None, model="m")
        assert "teammate" not in loop._config.tools_list

        reg = TeammateRegistry(tmp_path, runner=make_runner())
        loop2 = AgentLoop(
            query="Q",
            provider=MagicMock(),
            capabilities=None,
            model="m",
            teammate_registry=reg,
        )
        assert "teammate" in loop2._config.tools_list
