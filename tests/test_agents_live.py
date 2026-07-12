"""상주 에이전트(agents_live) 계약 — 구 teammate P1 (docs/teammate/DESIGN.md §6.1, 5.0.0 에서 agent 로 통합).

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
from agent_cli.subagent.agents_live import (
    AgentRegistry,
    build_reply_record,
    tool_agent,
)


def wait_until(pred, timeout: float = 5.0) -> bool:
    # 전체 스위트 병렬 부하에서 worker 스레드 스케줄링이 늦어질 수 있어
    # 여유 있는 기본값 (단독 실행은 수 ms 에 끝난다 — flake 마진용).
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

    def begin_agent_work(self, **kw):
        self._rec("begin_agent_work", **kw)

    def end_agent_work(self, **kw):
        self._rec("end_agent_work", **kw)

    def agent_roster(self, roster):
        self._rec("agent_roster", roster=roster)

    def agent_message(self, **kw):
        self._rec("agent_message", **kw)

    def named(self, name):
        with self.lock:
            return [c for c in self.calls if c[0] == name]

    def __getattr__(self, name):
        # run 모드(일회성 엔진)가 부르는 그 외 렌더 표면(group_start,
        # begin_delegate_task, start_capture...)은 조용한 no-op — 없으면
        # AttributeError 가 도구 안전망에 잡혀 가짜 도구 에러가 된다.
        return lambda *a, **k: None


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
    reg = AgentRegistry(
        tmp_path,
        runtime={"model": "m", "timeout": 5, **runtime},
        runner=runner or make_runner(),
    )
    return reg


# ── 역할 로더 (D11(b)) ──────────────────────────


class TestRolesLoader:
    def _swap_loader(self, monkeypatch, tmp_path):
        import agent_cli.subagent.profiles as profiles_mod

        monkeypatch.setattr(profiles_mod, "_profile_loader", ResourceLoader([tmp_path]))
        return profiles_mod

    def test_loads_role_body_and_config(self, monkeypatch, tmp_path):
        (tmp_path / "researcher.md").write_text(
            "---\nallowed-tools:\n  - read_file\n---\nYou are a researcher.",
            encoding="utf-8",
        )
        roles = self._swap_loader(monkeypatch, tmp_path)
        body, config, error = roles.load_profile("researcher")
        assert error is None
        assert "researcher" in body
        assert config["allowed-tools"] == ["read_file"]

    def test_missing_role_reports_search_paths(self, monkeypatch, tmp_path):
        roles = self._swap_loader(monkeypatch, tmp_path)
        body, config, error = roles.load_profile("nope")
        assert body is None
        assert "not found" in error
        assert "agents" in error  # 5.0.0: 단일 프로파일 카탈로그 경로 안내

    def test_invalid_name_rejected(self, monkeypatch, tmp_path):
        roles = self._swap_loader(monkeypatch, tmp_path)
        _, _, error = roles.load_profile("../hack")
        assert "Invalid" in error


# ── 배달 레코드 계약 ────────────────────────────


class TestBuildReplyRecord:
    def _reply(self, **kw):
        return {
            "key": "agt-abc",
            "profile": "researcher",
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
        assert rec["tool"] == "agent"  # tool="" 금지 (형식-개입 마커)
        assert rec["success"] is True
        assert rec["source"] == "agent_reply"
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
        reg = AgentRegistry(None, runner=make_runner())
        key, err = reg.spawn()
        assert key == "" and "session dir" in err

    def test_spawn_limit(self, tmp_path, renderer, monkeypatch):
        monkeypatch.setenv("AGENT_CLI_MAX_AGENTS", "1")
        reg = make_registry(tmp_path)
        key, err = reg.spawn()
        assert err == ""
        key2, err2 = reg.spawn()
        assert key2 == "" and "limit" in err2
        reg.shutdown_all()

    def test_unknown_role_rejected(self, tmp_path, renderer, monkeypatch):
        import agent_cli.subagent.profiles as profiles_mod

        monkeypatch.setattr(
            profiles_mod, "_profile_loader", ResourceLoader([tmp_path / "empty"])
        )
        reg = make_registry(tmp_path)
        key, err = reg.spawn(profile="ghost")
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
        assert "agents" in str(path) and key in str(path)  # 5.0.0 디스크 명명
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
    def test_wait_reply_removed(self, tmp_path, renderer):
        # U-A (v4.63.0): wait 모드 제거 — stop_event 미감시 결함의 자연 해소.
        # 회신 수령은 turn-boundary drain 이 유일 경로다.
        reg = make_registry(tmp_path)
        assert not hasattr(reg, "wait_reply")
        reg.shutdown_all()

    def test_worker_owns_persistent_scope(self, tmp_path, renderer):
        # D9: 스코프는 worker 시작 시 1회 push, kill 시에만 고정 —
        # 요청 사이에도 살아 있다 (delegate 의 요청별 스코프와 다른 점).
        reg = make_registry(tmp_path)
        key, _ = reg.spawn(profile="")
        wait_until(lambda: reg.get(key).state == "idle")
        begins = renderer.named("begin_prompt_scope")
        assert [c[1]["scope"] for c in begins] == [key]
        assert begins[0][1]["label"] == "agent:anon"  # 5.0.0 라벨
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
        assert wait_until(lambda: bool(renderer.named("end_agent_work")))
        begin = renderer.named("begin_agent_work")[0][1]
        end = renderer.named("end_agent_work")[0][1]
        assert begin["key"] == key and begin["seq"] == 1 and begin["message"] == "job"
        assert end["key"] == key and end["success"] is True
        reg.shutdown_all()

    def test_format_status(self, tmp_path, renderer):
        reg = make_registry(tmp_path)
        assert "no live agents" in reg.format_status()
        key, _ = reg.spawn()
        wait_until(lambda: reg.get(key).state == "idle")
        s = reg.format_status()
        assert key in s and "idle" in s and "anon" in s
        assert "unknown" in reg.format_status("agt-nope")
        reg.shutdown_all()


# ── 도구 진입점 ─────────────────────────────────


class TestToolTeammate:
    def test_no_registry_rejected(self):
        # 5.0.0 모드 축소: 상주 모드는 main 전용 — run 안내와 함께 거부.
        r = tool_agent({"mode": "spawn"}, registry=None)
        assert not r.success and "main-session only" in r.error
        assert '"mode":"run"' in r.error

    def test_spawn_with_initial_task(self, tmp_path, renderer):
        reg = make_registry(tmp_path)
        r = tool_agent(
            {"mode": "spawn", "task": "explore"}, registry=reg, runtime=reg.runtime
        )
        assert r.success
        assert "spawned agent 'agt-" in r.output
        assert "initial task queued" in r.output
        assert wait_until(reg.has_pending_replies)
        reg.shutdown_all()

    def test_request_then_delivery_flow(self, tmp_path, renderer):
        reg = make_registry(tmp_path)
        key, _ = reg.spawn()
        r = tool_agent({"mode": "request", "key": key, "message": "go"}, registry=reg)
        assert r.success and "queued" in r.output
        assert "DELIVERED" not in r.output  # 안내는 도구 설명이 담당
        assert "wait" not in r.output  # U-A: wait 유도 문구 소멸
        assert wait_until(reg.has_pending_replies)
        reply = reg.drain_replies()[0]
        assert "done:go" in reply["output"]
        reg.shutdown_all()

    def test_wait_mode_rejected(self, tmp_path, renderer):
        # 스키마 밖 모드 — 의미론 검증과 디스패치 양쪽에서 거부.
        from agent_cli.tools.registry import TOOLS

        assert "unknown mode" in TOOLS["agent"].validate({"mode": "wait"})
        reg = make_registry(tmp_path)
        r = tool_agent({"mode": "wait", "key": "agt-x"}, registry=reg)
        assert not r.success and "unknown mode" in r.error

    def test_status_and_kill_modes(self, tmp_path, renderer):
        reg = make_registry(tmp_path)
        key, _ = reg.spawn()
        wait_until(lambda: reg.get(key).state == "idle")
        s = tool_agent({"mode": "status"}, registry=reg)
        assert s.success and key in s.output
        k = tool_agent({"mode": "kill", "key": key}, registry=reg)
        assert k.success and "terminated" in k.output
        assert wait_until(lambda: reg.get(key).state == "dead")

    def test_unknown_mode(self, tmp_path, renderer):
        reg = make_registry(tmp_path)
        r = tool_agent({"mode": "dance"}, registry=reg)
        assert not r.success and "unknown mode" in r.error


# ── 스키마 의미론 검증 (C7) ─────────────────────


class TestAgentToolValidate:
    def _tool(self):
        from agent_cli.tools.registry import TOOLS

        return TOOLS["agent"]

    def test_registered_in_tools(self):
        assert self._tool().name == "agent"

    def test_mode_enum(self):
        assert "unknown mode" in self._tool().validate({"mode": "fly"})
        assert self._tool().validate({"mode": "spawn"}) is None
        assert self._tool().validate({"mode": "status"}) is None

    def test_mode_conditional_required(self):
        t = self._tool()
        assert "requires" in t.validate({"mode": "request", "key": "agt-1"})
        assert "requires" in t.validate({"mode": "request", "message": "x"})
        assert t.validate({"mode": "request", "key": "agt-1", "message": "x"}) is None
        assert "requires" in t.validate({"mode": "resume"})
        assert "requires" in t.validate({"mode": "kill", "key": "  "})


# ── 루프 배선 ───────────────────────────────────


class TestLoopWiring:
    def test_tool_present_without_registry_mode_reduced(self):
        # 5.0.0 모드 축소: 레지스트리 없어도 agent 는 존재 (run 용) —
        # 상주 모드는 디스패치 거부 + 설명 축소가 담당.
        from unittest.mock import MagicMock

        from agent_cli.loop import AgentLoop

        loop = AgentLoop(query="Q", provider=MagicMock(), capabilities=None, model="m")
        assert "agent" in loop._config.tools_list

    def test_tool_stripped_at_depth_ceiling(self):
        # depth 상한 = run 도 불가 → 도구째 strip (delegate/run_skill 동형).
        from unittest.mock import MagicMock

        from agent_cli.loop import AgentLoop

        loop = AgentLoop(
            query="Q",
            provider=MagicMock(),
            capabilities=None,
            model="m",
            depth=2,
            max_depth=2,
        )
        assert "agent" not in loop._config.tools_list

    def test_tool_present_with_registry(self, tmp_path):
        from unittest.mock import MagicMock

        from agent_cli.loop import AgentLoop

        reg = AgentRegistry(tmp_path, runner=make_runner())
        loop = AgentLoop(
            query="Q",
            provider=MagicMock(),
            capabilities=None,
            model="m",
            agent_registry=reg,
        )
        assert "agent" in loop._config.tools_list

    def test_deliver_agent_mail_injects_records(self, tmp_path, renderer):
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
            _config=SimpleNamespace(agent_registry=reg),
            messages=[],
            ctx=fake_ctx,
            turn=3,
            _oversized_cap=10_000,
        )
        AgentLoop._deliver_agent_mail(fake_self)

        assert len(fake_self.messages) == 1
        assert "done:job" in fake_self.messages[0]["content"]
        assert len(added) == 1
        assert added[0]["tool"] == "agent"
        assert not is_format_intervention(added[0])
        assert not reg.has_pending_replies()  # 소비 완료
        # 레지스트리 없으면 no-op
        fake_self2 = SimpleNamespace(
            _config=SimpleNamespace(agent_registry=None), messages=[]
        )
        AgentLoop._deliver_agent_mail(fake_self2)
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
        reg = AgentRegistry(tmp_path / "sess", runner=make_runner())

        provider = MagicMock()

        def scripted(*args, **kwargs):
            n = provider.call.call_count
            if n == 1:
                return LLMResponse(
                    content=emit("agent", {"mode": "spawn", "task": "explore the repo"})
                )
            # 배달을 기다렸다가 complete — 실전에선 다른 작업을 하는 턴.
            wait_until(reg.has_pending_replies)
            if n == 2:
                return LLMResponse(content=emit("agent", {"mode": "status"}))
            return LLMResponse(content=emit("complete", {"result": "finished"}))

        provider.call = MagicMock(side_effect=scripted)

        result = run_loop(
            query="use a teammate",
            provider=provider,
            capabilities=self._caps(),
            model="test-model",
            ctx=ctx,
            agent_registry=reg,
        )
        assert result.success and result.output == "finished"

        # 회신이 턴 경계에서 ctx 레코드로 배달됐고 fold 오인이 없다
        delivered = [
            m for m in ctx.get_raw_messages() if m.get("source") == "agent_reply"
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

    def test_subloop_gets_reduced_agent_description(self, tmp_path):
        # 5.0.0 모드 축소: 서브루프(레지스트리 없음)의 Available Tools 는
        # run 전용 축소 설명, main 은 상주 모드 포함 전체 설명.
        from agent_cli.prompts.system_prompt import _build_tools_section
        from agent_cli.wire_formats import get as get_wf

        wf = get_wf("react")
        sub = _build_tools_section(["agent"], wf, has_agent_registry=False)
        main = _build_tools_section(["agent"], wf, has_agent_registry=True)
        assert 'mode:"run" only here' in sub
        assert "PERSISTENT" not in sub.split("Input JSON")[0]
        assert "spawn" in main and "PERSISTENT" in main


# ── P2: ask→main 양방향 라우팅 ──────────────────


def make_asking_runner(question="which branch?"):
    """ask_handler 를 실제로 호출하는 가짜 러너 — teammate 가 작업 중
    질문하고, 받은 답을 회신에 반영하는 시나리오."""

    def runner(query, ctx, **kwargs):
        answer = kwargs["ask_handler"](question)
        return _FakeLoopResult(output=f"resumed with: {answer}"), 0.01

    return runner


class TestAskRouting:
    def test_handle_ask_handler_branch(self):
        # dispatch 의 _handle_ask: handler 가 있으면 사용자 프롬프트 대신
        # 핸들러로 — Q/A 관찰 shape 은 사용자 경로와 동일.
        from agent_cli.loop.dispatch import _handle_ask

        out = _handle_ask(["1. which branch?"], handler=lambda q: "main branch")
        assert out == "Q: which branch?\nA: main branch"

    def test_handle_ask_handler_exception_is_no_response(self):
        from agent_cli.loop.dispatch import _handle_ask

        def broken(q):
            raise RuntimeError("mailbox gone")

        out = _handle_ask(["q?"], handler=broken)
        assert out.endswith("A: (no response)")

    def test_question_record_contract(self):
        rec = build_reply_record(
            {
                "kind": "question",
                "key": "agt-q1",
                "profile": "researcher",
                "success": True,
                "output": "which branch?",
            }
        )
        assert rec["tool"] == "agent"
        assert rec["source"] == "agent_question"
        assert not is_format_intervention(rec)
        assert "QUESTION" in rec["content"] and "which branch?" in rec["content"]
        # main 이 그대로 따라할 수 있는 답변 op 안내 포함
        assert '"mode":"request"' in rec["content"] and "agt-q1" in rec["content"]

    def test_full_ask_roundtrip(self, tmp_path, renderer):
        # teammate 가 질문 → main mailbox 에 kind:question → main 이
        # request 로 답변 → teammate 재개 → 최종 회신에 답 반영.
        reg = make_registry(tmp_path, runner=make_asking_runner())
        key, _ = reg.spawn()
        reg.request(key, "do the task")
        assert wait_until(lambda: reg.get(key).state == "waiting_ask")
        # 질문이 mailbox 에 떠 있다
        with reg._cv:
            kinds = [r.get("kind") for r in reg._pending]
        assert kinds == ["question"]
        # main 이 답변 — 도구 힌트가 "답변으로 소비" 를 알린다
        r = tool_agent(
            {"mode": "request", "key": key, "message": "main branch"}, registry=reg
        )
        assert r.success and "answer to its pending question" in r.output
        # teammate 재개 → 최종 회신에 답이 반영
        assert wait_until(
            lambda: any(x.get("kind") == "reply" for x in getattr(reg, "_pending", []))
        )
        items = reg.drain_replies()
        reply = next(x for x in items if x.get("kind") == "reply")
        assert reply["output"] == "resumed with: main branch"
        assert reg.get(key).state == "idle"
        reg.shutdown_all()

    def test_answer_attribution_for_non_main_author(self, tmp_path, renderer):
        # P4 인간 개입의 토대: main 외 화자의 답은 [author]: 접두로 전달.
        reg = make_registry(tmp_path, runner=make_asking_runner())
        key, _ = reg.spawn()
        reg.request(key, "task")
        assert wait_until(lambda: reg.get(key).state == "waiting_ask")
        reg.request(key, "the blue one", author="user:bob")
        assert wait_until(lambda: any(x.get("kind") == "reply" for x in reg._pending))
        reply = next(x for x in reg.drain_replies() if x.get("kind") == "reply")
        assert reply["output"] == "resumed with: [user:bob]: the blue one"
        reg.shutdown_all()

    def test_question_delivered_via_drain(self, tmp_path, renderer):
        # wait 제거 후에도 질문 왕복은 성립: 질문은 턴 경계 drain 으로
        # main 에 배달되고, main 이 request 로 답하면 재개된다.
        reg = make_registry(tmp_path, runner=make_asking_runner())
        key, _ = reg.spawn()
        reg.request(key, "task")
        assert wait_until(lambda: reg.get(key).state == "waiting_ask")
        q = next(r for r in reg.drain_replies() if r.get("kind") == "question")
        assert "which branch?" in q["output"]
        reg.request(key, "main branch")  # 답변 → 재개
        assert wait_until(lambda: reg.get(key).handled == 1)
        reg.shutdown_all()

    def test_status_shows_waiting_ask(self, tmp_path, renderer):
        reg = make_registry(tmp_path, runner=make_asking_runner())
        key, _ = reg.spawn()
        reg.request(key, "task")
        assert wait_until(lambda: reg.get(key).state == "waiting_ask")
        assert "waiting_ask" in reg.format_status(key)
        reg.shutdown_all()

    def test_shutdown_unblocks_pending_ask(self, tmp_path, renderer):
        # 답변 없이 세션 종료 — 핸들러가 즉시 풀리고 worker 가 dead 로.
        reg = make_registry(tmp_path, runner=make_asking_runner())
        key, _ = reg.spawn()
        reg.request(key, "task")
        assert wait_until(lambda: reg.get(key).state == "waiting_ask")
        t0 = time.monotonic()
        reg.shutdown_all()
        assert time.monotonic() - t0 < 4.0  # join(5s) 안에 즉시 종료
        assert reg.get(key).state == "dead"
        # 종료 답변이 회신에 반영됨 (터미널 no-response)
        replies = [x for x in reg.drain_replies() if x.get("kind") == "reply"]
        assert replies and "no response" in replies[0]["output"]

    def test_delegate_ask_path_unchanged(self):
        # handler=None(delegate/main) 이면 종전 사용자-프롬프트 경로 —
        # can_prompt False 환경에선 "(no response)" 폴백 그대로.
        from unittest.mock import MagicMock

        import agent_cli.loop.dispatch as dispatch_mod
        from agent_cli.loop.dispatch import _handle_ask

        fake = MagicMock()
        fake.can_prompt.return_value = False
        fake._prefix = ""
        assert dispatch_mod is not None  # import 경로 확인용
        import agent_cli.render as render_mod
        import pytest as _pytest

        mp = _pytest.MonkeyPatch()
        try:
            mp.setattr(render_mod, "get_renderer", lambda: fake)
            out = _handle_ask(["q?"])
        finally:
            mp.undo()
        assert out.endswith("A: (no response)")
        fake.announce_ask.assert_called_once()


# ── P3: 상태 영속 + resume 재생성 (D7) ──────────


class TestResumeRestore:
    def _state(self, tmp_path):
        import json

        return json.loads((tmp_path / "agents.json").read_text(encoding="utf-8"))

    def test_manifest_written_on_spawn(self, tmp_path, renderer):
        reg = make_registry(tmp_path)
        key, _ = reg.spawn()
        data = self._state(tmp_path)
        assert data["version"] == 1
        entry = next(e for e in data["agents"] if e["key"] == key)
        assert entry["state"] == "idle"
        reg.shutdown_all()

    def test_kill_is_permanent_in_manifest(self, tmp_path, renderer):
        reg = make_registry(tmp_path)
        key, _ = reg.spawn()
        wait_until(lambda: reg.get(key).state == "idle")
        reg.kill(key)
        entry = next(e for e in self._state(tmp_path)["agents"] if e["key"] == key)
        assert entry["state"] == "dead"

    def test_session_exit_keeps_teammate_revivable(self, tmp_path, renderer):
        reg = make_registry(tmp_path)
        key, _ = reg.spawn()
        wait_until(lambda: reg.get(key).state == "idle")
        reg.shutdown_all()  # 세션 종료 ≠ kill
        entry = next(e for e in self._state(tmp_path)["agents"] if e["key"] == key)
        assert entry["state"] == "idle"  # resume 대상

    def test_pending_mirrored_to_disk(self, tmp_path, renderer):
        reg = make_registry(tmp_path)
        key, _ = reg.spawn()
        reg.request(key, "hi")
        assert wait_until(reg.has_pending_replies)
        assert len(self._state(tmp_path)["pending"]) == 1
        reg.drain_replies()
        assert self._state(tmp_path)["pending"] == []
        reg.shutdown_all()

    def test_full_resume_roundtrip(self, tmp_path, renderer):
        # 세션 1: spawn → 문답 1회 → ctx 에 landmark → 미배달 회신 남기고 종료
        reg1 = make_registry(tmp_path)
        key, _ = reg1.spawn()
        assert wait_until(lambda: reg1.get(key).state == "idle")
        reg1.get(key).ctx.add({"role": "user", "content": "landmark-from-session-1"})
        reg1.request(key, "hello")
        assert wait_until(reg1.has_pending_replies)
        reg1.shutdown_all()

        # 세션 2 (같은 session_dir): restore → 재생성 + 미배달 복원
        reg2 = make_registry(tmp_path)
        revived = reg2.restore()
        assert revived == 1
        tm = reg2.get(key)
        assert wait_until(lambda: tm.state == "idle")
        # 이전 세션 문맥을 기억한 채 살아났다 (ctx resume)
        assert any(
            "landmark-from-session-1" in str(m) for m in tm.ctx.get_raw_messages()
        )
        # 미배달 회신은 첫 턴 경계 배달 대상으로 복원
        restored = reg2.drain_replies()
        assert [r["output"] for r in restored] == ["done:hello"]
        # seq 이어가기 — 새 요청은 reply-2 (기존 reply-1 안 덮음)
        assert tm.queued == tm.handled == 1
        reg2.request(key, "again")
        assert wait_until(reg2.has_pending_replies)
        again = reg2.drain_replies()[0]
        assert again["output"] == "done:again" and again["seq"] == 2
        reg2.shutdown_all()

    def test_killed_teammate_not_revived(self, tmp_path, renderer):
        reg1 = make_registry(tmp_path)
        k_alive, _ = reg1.spawn()
        k_dead, _ = reg1.spawn()
        wait_until(lambda: reg1.get(k_dead).state == "idle")
        reg1.kill(k_dead)
        reg1.shutdown_all()

        reg2 = make_registry(tmp_path)
        assert reg2.restore() == 1  # 산 것만 재기동
        assert wait_until(lambda: reg2.get(k_alive).state == "idle")
        tomb = reg2.get(k_dead)
        assert tomb is not None and tomb.state == "dead"  # 툼스톤 (status 가시성)
        assert "dead" in reg2.format_status(k_dead)
        reg2.shutdown_all()

    def test_stale_question_marked_on_restore(self, tmp_path, renderer):
        reg1 = make_registry(tmp_path, runner=make_asking_runner())
        key, _ = reg1.spawn()
        reg1.request(key, "task")
        assert wait_until(lambda: reg1.get(key).state == "waiting_ask")
        reg1.shutdown_all()  # 질문이 미배달인 채 종료

        reg2 = make_registry(tmp_path)
        reg2.restore()
        question = next(r for r in reg2.drain_replies() if r.get("kind") == "question")
        assert question.get("stale") is True
        rec = build_reply_record(question)
        assert "STALE" in rec["content"]
        assert "NO LONGER" in rec["content"]  # 답변 대기 아님을 명시
        reg2.shutdown_all()

    def test_role_prompt_survives_role_file_deletion(
        self, tmp_path, renderer, monkeypatch
    ):
        import agent_cli.subagent.profiles as profiles_mod

        roles_dir = tmp_path / "roles"
        roles_dir.mkdir()
        (roles_dir / "researcher.md").write_text(
            "---\nmodel: r-model\n---\nYou are THE researcher.", encoding="utf-8"
        )
        monkeypatch.setattr(
            profiles_mod, "_profile_loader", ResourceLoader([roles_dir])
        )

        reg1 = make_registry(tmp_path)
        key, err = reg1.spawn(profile="researcher")
        assert err == ""
        wait_until(lambda: reg1.get(key).state == "idle")
        reg1.shutdown_all()

        (roles_dir / "researcher.md").unlink()  # 역할 파일이 사라져도
        reg2 = make_registry(tmp_path)
        assert reg2.restore() == 1
        assert reg2.get(key).role_prompt == "You are THE researcher."
        reg2.shutdown_all()

    def test_restore_noop_without_file_or_bad_version(self, tmp_path, renderer):
        reg = make_registry(tmp_path / "fresh")
        assert reg.restore() == 0
        (tmp_path / "agents.json").write_text(
            '{"version": 99, "agents": [{"key": "agt-x"}]}', encoding="utf-8"
        )
        reg2 = make_registry(tmp_path)
        assert reg2.restore() == 0

    def test_tool_call_refreshes_runtime(self, tmp_path, renderer):
        # restore 된 teammate 가 스폰 없이도 현 세션 배선으로 돌 수 있도록
        # 모든 도구 호출이 runtime 을 갱신한다.
        reg = make_registry(tmp_path)
        r = tool_agent(
            {"mode": "status"}, registry=reg, runtime={"model": "fresh-model"}
        )
        assert r.success
        assert reg.runtime["model"] == "fresh-model"


# ── P4: WebUI 이벤트·비배달 규칙·idle 자동 재기동 ──


class TestHumanInterventionRouting:
    """D8: 인간(비-main 화자) 문답은 대화 창에만 — main mailbox 비오염."""

    def test_human_request_reply_not_delivered_to_main(self, tmp_path, renderer):
        reg = make_registry(tmp_path)
        key, _ = reg.spawn()
        wait_until(lambda: reg.get(key).state == "idle")
        reg.request(key, "hello from human", author="user:bob")
        assert wait_until(lambda: reg.get(key).handled == 1)
        # 창에는 out 메시지가 갔지만 main pending 은 비어 있다
        outs = [
            c for c in renderer.named("agent_message") if c[1]["direction"] == "out"
        ]
        assert outs and outs[0][1]["text"] == "done:[user:bob]: hello from human"
        assert not reg.has_pending_replies()
        reg.shutdown_all()

    def test_main_request_reply_still_delivered(self, tmp_path, renderer):
        reg = make_registry(tmp_path)
        key, _ = reg.spawn()
        reg.request(key, "hello")  # author 기본 main
        assert wait_until(reg.has_pending_replies)
        reg.shutdown_all()

    def test_question_during_human_request_stays_in_window(self, tmp_path, renderer):
        # 인간 발신 작업 중의 ask 질문은 main mailbox 로 가지 않는다 (D8 대칭)
        reg = make_registry(tmp_path, runner=make_asking_runner())
        key, _ = reg.spawn()
        wait_until(lambda: reg.get(key).state == "idle")
        reg.request(key, "human task", author="user:bob")
        assert wait_until(lambda: reg.get(key).state == "waiting_ask")
        assert not reg.has_pending_replies()  # main 비배달
        qs = [
            c
            for c in renderer.named("agent_message")
            if c[1]["direction"] == "question"
        ]
        assert qs and qs[0][1]["text"] == "which branch?"  # 창에는 표시
        # 인간이 창에서 답하면 재개되고, 회신도 창에만
        reg.request(key, "blue", author="user:bob")
        assert wait_until(lambda: reg.get(key).handled == 1)
        assert not reg.has_pending_replies()
        reg.shutdown_all()

    def test_question_during_main_request_reaches_main(self, tmp_path, renderer):
        reg = make_registry(tmp_path, runner=make_asking_runner())
        key, _ = reg.spawn()
        reg.request(key, "main task")
        assert wait_until(lambda: reg.get(key).state == "waiting_ask")
        assert reg.has_pending_replies()  # main 에도 배달
        reg.shutdown_all()


class TestP4Events:
    def test_request_emits_in_message_and_roster(self, tmp_path, renderer):
        reg = make_registry(tmp_path)
        key, _ = reg.spawn()
        assert renderer.named("agent_roster")  # spawn 시 roster
        reg.request(key, "job", author="user:kim")
        ins = [c for c in renderer.named("agent_message") if c[1]["direction"] == "in"]
        assert ins and ins[0][1]["author"] == "user:kim" and ins[0][1]["text"] == "job"
        reg.shutdown_all()

    def test_roster_reflects_states_and_kill(self, tmp_path, renderer):
        reg = make_registry(tmp_path)
        key, _ = reg.spawn()
        wait_until(lambda: reg.get(key).state == "idle")
        reg.kill(key)
        last = renderer.named("agent_roster")[-1][1]["roster"]
        entry = next(e for e in last if e["key"] == key)
        assert entry["state"] == "dead"


class TestMailWaker:
    def _waker(self, pending=lambda: True):
        from agent_cli.subagent.agents_live import MailWaker

        sent = []
        w = MailWaker(lambda conn, text: sent.append((conn, text)), pending)
        return w, sent

    def test_mail_while_idle_arms_once(self):
        w, sent = self._waker()
        w.idle.set()
        w.on_mail()
        w.on_mail()  # 중복 mail → wake 1개
        assert len(sent) == 1
        assert sent[0] == (None, w.WAKE_TEXT)

    def test_mail_while_busy_does_not_wake(self):
        w, sent = self._waker()
        w.on_mail()  # idle 아님
        assert sent == []

    def test_run_end_race_closure(self):
        # run 마지막 턴 경계 이후 도착분 — on_run_end 가 봉합
        w, sent = self._waker(pending=lambda: True)
        w.on_run_end()
        assert len(sent) == 1

    def test_run_end_noop_without_pending(self):
        w, sent = self._waker(pending=lambda: False)
        w.on_run_end()
        assert sent == []

    def test_handle_dequeued_verdicts(self):
        state = {"pending": True}
        w, sent = self._waker(pending=lambda: state["pending"])
        assert w.handle_dequeued("normal user message") is None
        w.idle.set()
        w.on_mail()
        # 잔여 있음 → run (그리고 disarm — 다음 mail 이 다시 wake 가능)
        assert w.handle_dequeued(w.WAKE_TEXT) == "run"
        w.on_mail()
        assert len(sent) == 2  # disarm 후 재무장 확인
        # 이미 배달됨 → skip
        state["pending"] = False
        assert w.handle_dequeued(w.WAKE_TEXT) == "skip"


# ── P5: CLI 큐 펌프 (quiescence) ────────────────


class TestQuiescenceHelpers:
    def test_active_while_busy_or_queued(self, tmp_path, renderer):
        gate = threading.Event()
        reg = make_registry(tmp_path, runner=make_runner(block=gate))
        key, _ = reg.spawn()
        wait_until(lambda: reg.get(key).state == "idle")
        assert not reg.has_active_work()
        reg.request(key, "long")
        assert reg.has_active_work()  # busy (또는 inbox 큐잉)
        gate.set()
        assert wait_until(reg.has_pending_replies)
        assert reg.has_active_work()  # 미배달 회신
        reg.drain_replies()
        assert wait_until(lambda: not reg.has_active_work())
        reg.shutdown_all()

    def test_waiting_ask_not_active_but_reported(self, tmp_path, renderer):
        reg = make_registry(tmp_path, runner=make_asking_runner())
        key, _ = reg.spawn()
        reg.request(key, "task")
        assert wait_until(lambda: reg.get(key).state == "waiting_ask")
        reg.drain_replies()  # 질문 배달 소비 후에는
        assert not reg.has_active_work()  # 교착 방지 — active 아님
        assert reg.waiting_ask_keys() == [key]  # 대신 경고 표시용으로 노출
        reg.shutdown_all()


class TestRunMessagePump:
    """CLI run 의 큐 펌프 — 초기 질의 1회 / teammate 회신 wake 재기동 /
    quiescence 종료 / 빈 wake skip. (main.py 모듈 함수라 실제 배선 검증.)"""

    class _FakeReg:
        def __init__(self):
            self.pending = False
            self.busy = False

        def has_pending_replies(self):
            return self.pending

        def has_active_work(self):
            return self.pending or self.busy

    def _pump(self, queue, waker, reg, run_one):
        from agent_cli.main import _run_message_pump

        _run_message_pump(queue, waker, reg, run_one, poll_secs=0.05)

    def test_plain_run_executes_once_and_exits(self):
        from agent_cli.input_queue import InputQueue
        from agent_cli.subagent.agents_live import MailWaker

        q = InputQueue()
        reg = self._FakeReg()
        waker = MailWaker(q.enqueue, reg.has_pending_replies)
        calls = []
        q.enqueue(None, "the query")
        self._pump(q, waker, reg, lambda text, *, wake: calls.append((text, wake)))
        assert calls == [("the query", False)]

    def test_wake_cycle_delivers_then_quiesces(self):
        from agent_cli.input_queue import InputQueue
        from agent_cli.subagent.agents_live import MailWaker

        q = InputQueue()
        reg = self._FakeReg()
        waker = MailWaker(q.enqueue, reg.has_pending_replies)
        calls = []

        def run_one(text, *, wake):
            calls.append((text, wake))
            if len(calls) == 1:
                # 첫 run 이 teammate 를 스폰해 busy 로 만들었다 치고,
                # 잠시 후 회신이 도착하는 백그라운드를 흉내낸다.
                reg.busy = True

                def later():
                    time.sleep(0.15)
                    reg.busy = False
                    reg.pending = True
                    waker.on_mail()  # registry.on_reply 배선과 동일

                threading.Thread(target=later, daemon=True).start()
            else:
                reg.pending = False  # wake run 의 턴 경계 배달을 흉내

        q.enqueue(None, "spawn and go")
        self._pump(q, waker, reg, run_one)
        assert len(calls) == 2
        assert calls[0] == ("spawn and go", False)
        assert calls[1][1] is True  # wake run
        assert calls[1][0] == waker.WAKE_TEXT

    def test_stale_wake_skipped(self):
        from agent_cli.input_queue import InputQueue
        from agent_cli.subagent.agents_live import MailWaker

        q = InputQueue()
        reg = self._FakeReg()
        waker = MailWaker(q.enqueue, reg.has_pending_replies)
        reg.pending = True
        waker.idle.set()
        waker.on_mail()  # wake 큐잉
        reg.pending = False  # 다른 경로가 이미 배달
        calls = []
        self._pump(q, waker, reg, lambda text, *, wake: calls.append(text))
        assert calls == []  # skip — 빈 run 없음

    def test_shutdown_exits(self):
        from agent_cli.input_queue import InputQueue
        from agent_cli.subagent.agents_live import MailWaker

        q = InputQueue()
        reg = self._FakeReg()
        reg.busy = True  # 활성 작업이 있어도 SHUTDOWN 이 우선
        waker = MailWaker(q.enqueue, reg.has_pending_replies)
        q.shutdown()
        self._pump(q, waker, reg, lambda text, *, wake: None)  # 즉시 반환


# ── 전문가 역할: died 통지·광고·auto-spawn (v4.59.0) ──


class TestWorkerDeathNotice:
    """Q4: worker 의 비정상 사망은 main 에 관찰(kind:"died")로 통지 —
    kill/세션 종료(의도된 종료)는 통지하지 않는다."""

    def test_ctx_creation_failure_notifies_main(self, tmp_path, renderer, monkeypatch):
        import agent_cli.subagent.runner as runner_mod

        monkeypatch.setattr(
            runner_mod, "create_subagent_ctx", lambda *a, **k: (None, "disk full")
        )
        reg = make_registry(tmp_path)
        key, err = reg.spawn()
        assert err == ""
        assert wait_until(reg.has_pending_replies)
        died = reg.drain_replies()[0]
        assert died["kind"] == "died" and "disk full" in died["output"]
        rec = build_reply_record(died)
        assert rec["source"] == "agent_died" and rec["success"] is False
        assert "DIED" in rec["content"]
        assert '"mode":"resume"' in rec["content"]  # 부활 안내 (v4.61.0)
        assert not is_format_intervention(rec)
        # 원인 미상 사망은 resume 이 되살리지 않는다
        assert wait_until(lambda: reg.get(key).state == "dead")
        import json

        entry = json.loads((tmp_path / "agents.json").read_text())["agents"][0]
        assert entry["state"] == "dead"

    def test_machinery_crash_notifies_main(self, tmp_path, renderer):
        # 요청 처리기(내부 try) 밖의 기계 사망 — begin_agent_work 폭발.
        def boom(**kw):
            raise MemoryError("simulated OOM in worker machinery")

        renderer.begin_agent_work = boom
        reg = make_registry(tmp_path)
        key, _ = reg.spawn()
        wait_until(lambda: reg.get(key).state == "idle")
        reg.request(key, "job")
        assert wait_until(reg.has_pending_replies)
        kinds = {r["kind"] for r in reg.drain_replies()}
        assert "died" in kinds
        assert reg.get(key).state == "dead"

    def test_kill_and_shutdown_do_not_notify(self, tmp_path, renderer):
        reg = make_registry(tmp_path)
        k1, _ = reg.spawn()
        k2, _ = reg.spawn()
        wait_until(lambda: reg.get(k1).state == "idle")
        wait_until(lambda: reg.get(k2).state == "idle")
        reg.kill(k1)
        reg.shutdown_all()
        assert not reg.has_pending_replies()  # 의도된 종료 — died mail 없음


class TestRoleDiscovery:
    def test_builtin_roles_loadable_and_advertised(self):
        from agent_cli.subagent.profiles import available_profiles, load_profile

        for name in ("researcher", "code-reviewer"):
            body, config, err = load_profile(name)
            assert err is None and body
            assert config.get("allowed-tools")
        advertised = dict(available_profiles())
        assert "researcher" in advertised and "code-reviewer" in advertised
        assert advertised["researcher"]  # description 필수 (발견 표면)

    def test_disable_model_invocation_hidden(self, tmp_path, monkeypatch):
        import agent_cli.subagent.profiles as profiles_mod

        (tmp_path / "hidden.md").write_text(
            "---\ndescription: secret\ndisable-model-invocation: true\n---\nbody",
            encoding="utf-8",
        )
        (tmp_path / "visible.md").write_text(
            "---\ndescription: shown\n---\nbody", encoding="utf-8"
        )
        monkeypatch.setattr(profiles_mod, "_profile_loader", ResourceLoader([tmp_path]))
        names = [n for n, _ in profiles_mod.available_profiles()]
        assert names == ["visible"]
        # auto-spawn 스캔용 include_meta 는 전체 반환
        all_names = [n for n, _ in profiles_mod.available_profiles(include_meta=True)]
        assert set(all_names) == {"hidden", "visible"}

    def test_prompt_section_lists_roles_with_spawn_example(self):
        from agent_cli.prompts.system_prompt import build_agent_profiles_section

        desc = build_agent_profiles_section()
        assert "## Agent Profiles" in desc
        assert "`researcher`" in desc and "`code-reviewer`" in desc
        assert '"mode"' in desc and "spawn" in desc  # 스폰 예시 포함

    def test_prompt_section_gated_on_teammate_tool(self, tmp_path):
        # 레지스트리 없는 루프(active_tools 에 teammate 부재)엔 광고 안 함.
        from agent_cli.prompts.system_prompt import build_system_prompt_sections
        from agent_cli.providers.capabilities import ModelCapabilities

        caps = ModelCapabilities(
            context_window=32768,
            max_output_tokens=4096,
            supports_structured_output=True,
            supports_thinking=False,
            thinking_budget=0,
            supports_strict_schema=False,
        )
        with_tool = build_system_prompt_sections(
            caps, active_tools=["read_file", "agent"]
        )
        without = build_system_prompt_sections(caps, active_tools=["read_file"])
        names_with = [n for n, _ in with_tool]
        names_without = [n for n, _ in without]
        assert "Agent Profiles" in names_with
        assert "Agent Profiles" not in names_without


class TestAutoSpawn:
    def _roles_dir(self, tmp_path, monkeypatch, *, flagged=True):
        import agent_cli.subagent.profiles as profiles_mod

        d = tmp_path / "roles"
        d.mkdir(exist_ok=True)
        flag = "auto-spawn: true\n" if flagged else ""
        (d / "concierge.md").write_text(
            f"---\ndescription: greeter\n{flag}---\nYou greet.", encoding="utf-8"
        )
        monkeypatch.setattr(profiles_mod, "_profile_loader", ResourceLoader([d]))
        return d

    def test_flagged_role_spawns_once(self, tmp_path, renderer, monkeypatch):
        self._roles_dir(tmp_path, monkeypatch)
        reg = make_registry(tmp_path)
        assert reg.auto_spawn() == 1
        tm = next(iter(reg._agents.values()))
        assert tm.profile_name == "concierge"
        assert reg.auto_spawn() == 0  # 살아있는 동안 중복 스폰 없음
        reg.shutdown_all()

    def test_unflagged_role_ignored(self, tmp_path, renderer, monkeypatch):
        self._roles_dir(tmp_path, monkeypatch, flagged=False)
        reg = make_registry(tmp_path)
        assert reg.auto_spawn() == 0

    def test_revived_role_not_duplicated(self, tmp_path, renderer, monkeypatch):
        # resume 재생성(P3) 후 auto_spawn — 같은 역할이 살아 있으면 skip.
        self._roles_dir(tmp_path, monkeypatch)
        reg1 = make_registry(tmp_path)
        assert reg1.auto_spawn() == 1
        key = next(iter(reg1._agents))
        wait_until(lambda: reg1.get(key).state == "idle")
        reg1.shutdown_all()

        reg2 = make_registry(tmp_path)
        assert reg2.restore() == 1  # 재생성으로 이미 상주
        assert reg2.auto_spawn() == 0  # 중복 없음
        reg2.shutdown_all()


# ── 다중 인스턴스 + Live Teammates 광고 (v4.60.0) ──


class TestMultiInstance:
    def test_same_role_spawns_multiple(self, tmp_path, renderer):
        reg = make_registry(tmp_path)
        k1, e1 = reg.spawn(profile="", name="ui")
        k2, e2 = reg.spawn(profile="", name="api")
        assert e1 == e2 == "" and k1 != k2
        assert reg.get(k1).instance_name == "ui"
        assert reg.get(k2).snapshot()["name"] == "api"
        reg.shutdown_all()

    def test_invalid_instance_name_rejected(self, tmp_path, renderer):
        reg = make_registry(tmp_path)
        key, err = reg.spawn(name="bad name!")
        assert key == "" and "invalid instance name" in err

    def test_label_in_reply_record(self):
        from agent_cli.subagent.agents_live import format_agent_label

        assert format_agent_label("agt-1", "coder", "ui") == "agt-1 (coder · ui)"
        assert format_agent_label("agt-1") == "agt-1"
        rec = build_reply_record(
            {
                "kind": "reply",
                "key": "agt-1",
                "profile": "coder",
                "name": "ui",
                "success": True,
                "output": "done",
            }
        )
        assert "agt-1 (coder · ui)" in rec["content"]

    def test_name_and_description_survive_resume(self, tmp_path, renderer, monkeypatch):
        import agent_cli.subagent.profiles as profiles_mod

        d = tmp_path / "roles"
        d.mkdir()
        (d / "coder.md").write_text(
            "---\ndescription: builds things\n---\nYou build.", encoding="utf-8"
        )
        monkeypatch.setattr(profiles_mod, "_profile_loader", ResourceLoader([d]))
        reg1 = make_registry(tmp_path)
        key, _ = reg1.spawn(profile="coder", name="ui")
        wait_until(lambda: reg1.get(key).state == "idle")
        reg1.shutdown_all()

        reg2 = make_registry(tmp_path)
        assert reg2.restore() == 1
        tm = reg2.get(key)
        assert tm.instance_name == "ui" and tm.description == "builds things"
        reg2.shutdown_all()


class TestLiveTeammatesSection:
    def test_lists_alive_with_labels_and_descriptions(self, tmp_path, renderer):
        from agent_cli.prompts.system_prompt import build_live_agents_section

        reg = make_registry(tmp_path)
        k1, _ = reg.spawn(name="ui")
        reg.get(k1).description = "x" * 200  # 광고 요약은 절단
        k2, _ = reg.spawn()
        wait_until(lambda: reg.get(k2).state == "idle")
        reg.kill(k2)  # dead 는 광고에서 제외

        desc = build_live_agents_section(reg)
        assert "## Live Agents" in desc
        assert f"`{k1}`" in desc and "(ui)" in desc
        assert f"`{k2}`" not in desc
        assert "..." in desc and "x" * 141 not in desc  # 140자 절단
        assert '"mode":"request"' in desc  # 재사용 유도
        assert "distinct `name`" in desc  # 다중 인스턴스 안내
        reg.shutdown_all()

    def test_empty_or_absent_registry_renders_nothing(self, tmp_path, renderer):
        from agent_cli.prompts.system_prompt import build_live_agents_section

        assert build_live_agents_section(None) == ""
        assert build_live_agents_section(make_registry(tmp_path)) == ""

    def test_subloop_sections_gating(self, tmp_path, renderer):
        # 5.0.0 게이트: 카탈로그(Agent Profiles)는 agent 도구가 보이는 모든
        # 루프에 (run 이 profile 을 받으므로 서브루프 포함), Live Agents 는
        # 레지스트리(main)에서만.
        from agent_cli.prompts.system_prompt import build_system_prompt_sections
        from agent_cli.providers.capabilities import ModelCapabilities

        caps = ModelCapabilities(
            context_window=32768,
            max_output_tokens=4096,
            supports_structured_output=True,
            supports_thinking=False,
            thinking_budget=0,
            supports_strict_schema=False,
        )
        reg = make_registry(tmp_path)
        reg.spawn(name="ui")
        # main 루프 (도구 + registry): 두 섹션 다 존재
        main_names = [
            n
            for n, _ in build_system_prompt_sections(
                caps, active_tools=["agent"], agent_registry=reg
            )
        ]
        assert "Agent Profiles" in main_names and "Live Agents" in main_names
        # 서브루프 (agent 도구 있음·registry 없음): 카탈로그 O / Live X
        sub_names = [
            n
            for n, _ in build_system_prompt_sections(
                caps, active_tools=["read_file", "agent"], agent_registry=None
            )
        ]
        assert "Agent Profiles" in sub_names  # 5.0.0: 카탈로그는 서브루프에도 (run 용)
        assert "Live Agents" not in sub_names
        reg.shutdown_all()

    def test_membership_flag_set_and_consumed(self, tmp_path, renderer):
        from agent_cli.subagent.agents_live import (
            consume_agents_reload,
            notify_agents_changed,
        )

        consume_agents_reload()  # 잔여 클리어
        assert consume_agents_reload() is False
        notify_agents_changed()
        assert consume_agents_reload() is True
        assert consume_agents_reload() is False  # 소비됨
        reg = make_registry(tmp_path)
        key, _ = reg.spawn()  # spawn 이 플래그를 세운다
        assert consume_agents_reload() is True
        wait_until(lambda: reg.get(key).state == "idle")
        reg.kill(key)  # kill 도
        assert consume_agents_reload() is True

    def test_full_loop_advertises_after_spawn(self, tmp_path, renderer):
        # 통합: 턴1 spawn → 멤버십 플래그 → 턴2 시스템 프롬프트에
        # Live Teammates 광고 (core consume → rebuild → prompt svc 관통).
        import json
        from unittest.mock import MagicMock

        from agent_cli.context.manager import ContextManager
        from agent_cli.loop import run_loop
        from agent_cli.providers.base import LLMResponse
        from agent_cli.providers.capabilities import ModelCapabilities

        caps = ModelCapabilities(
            context_window=32768,
            max_output_tokens=4096,
            supports_structured_output=True,
            supports_thinking=False,
            thinking_budget=0,
            supports_strict_schema=False,
        )

        def emit(action, ai):
            return json.dumps({"thought": "t", "action": action, "action_input": ai})

        ctx = ContextManager(tmp_path / "sess", max_context_tokens=30_000)
        reg = make_registry(tmp_path / "sess")
        provider = MagicMock()

        def scripted(*a, **k):
            if provider.call.call_count == 1:
                return LLMResponse(
                    content=emit("agent", {"mode": "spawn", "name": "ui"})
                )
            return LLMResponse(content=emit("complete", {"result": "ok"}))

        provider.call = MagicMock(side_effect=scripted)
        result = run_loop(
            query="spawn one",
            provider=provider,
            capabilities=caps,
            model="m",
            ctx=ctx,
            agent_registry=reg,
        )
        assert result.success
        # 턴 2 의 시스템 프롬프트(system kwarg)에 광고가 실렸다
        _, kwargs = provider.call.call_args_list[1]
        system = kwargs["system"]
        assert "## Live Agents" in system
        key = next(iter(reg._agents))
        assert key in system and "(ui)" in system
        reg.shutdown_all()


# ── v4.60.1 회귀: 실렌더러 시그니처 (worker 부트 사망 사고) ──


class TestRendererSignatureRegression:
    """status() 오호출이 (1) web 부트에서 worker 를 죽이고 (2) 📨 알림을
    try/except 가 조용히 삼키던 사고 — mock 이 아닌 **실렌더러**로 호출을
    고정한다."""

    def test_boot_announce_on_real_web_renderer(self):
        from agent_cli.main import _announce_agent_boot
        from agent_cli.render.web import WebRenderer

        r = WebRenderer()
        _announce_agent_boot(r, revived=2, auto=1)  # TypeError 면 즉사
        # transient status 이벤트가 실제로 흘렀는지까지 확인
        # (persistent 버퍼가 아닌 라이브 큐라 connection 으로 수신)
        from agent_cli.render.web import WebConnection

        conn = WebConnection(id="c")
        r.register_connection(conn)
        _announce_agent_boot(r, revived=1, auto=0)
        events = []
        while not conn.queue.empty():
            events.append(conn.queue.get_nowait())
        assert any(e == "status" and "재생성" in str(d) for e, d in events), (
            f"status 이벤트 미수신: {events[:5]}"
        )

    def test_boot_announce_on_real_minimal_renderer(self):
        import io

        from rich.console import Console

        from agent_cli.main import _announce_agent_boot
        from agent_cli.render.minimal import MinimalRenderer

        r = MinimalRenderer(Console(file=io.StringIO(), force_terminal=False))
        _announce_agent_boot(r, revived=1, auto=1)  # TypeError 면 즉사

    def test_reply_notice_actually_emits_on_web(self):
        # try/except 가 시그니처 에러를 삼켜 알림이 조용히 죽어 있었다 —
        # 실렌더러에서 이벤트가 실제로 나가는지 검사.
        from unittest.mock import patch

        from agent_cli.main import _agent_mail_notice
        from agent_cli.render.web import WebConnection, WebRenderer

        r = WebRenderer()
        conn = WebConnection(id="c")
        r.register_connection(conn)
        with patch("agent_cli.render.get_renderer", return_value=r):
            _agent_mail_notice({"kind": "reply", "key": "agt-1"})
            _agent_mail_notice({"kind": "question", "key": "agt-2"})
        events = []
        while not conn.queue.empty():
            events.append(conn.queue.get_nowait())
        texts = [str(d) for e, d in events if e == "status"]
        assert any("📨" in x and "agt-1" in x for x in texts), texts
        assert any("❓" in x and "agt-2" in x for x in texts), texts


# ── mode:"resume" — 죽은 teammate 부활 (v4.61.0) ──


class TestResumeMode:
    def test_killed_teammate_resumes_with_full_context(self, tmp_path, renderer):
        reg = make_registry(tmp_path)
        key, _ = reg.spawn(name="ui")
        assert wait_until(lambda: reg.get(key).state == "idle")
        reg.get(key).ctx.add({"role": "user", "content": "landmark-before-death"})
        reg.request(key, "first job")
        assert wait_until(lambda: reg.get(key).handled == 1)
        reg.drain_replies()
        reg.kill(key)
        assert reg.get(key).state == "dead"

        assert reg.resume_teammate(key) == ""
        tm = reg.get(key)
        assert wait_until(lambda: tm.state == "idle")
        # 이전 생의 기억을 그대로 갖고 살아났다
        assert any("landmark-before-death" in str(m) for m in tm.ctx.get_raw_messages())
        assert tm.instance_name == "ui"  # 라벨 보존
        # seq 이어가기 — 부활 후 첫 회신은 reply-2 (reply-1 안 덮음)
        reg.request(key, "second job")
        assert wait_until(reg.has_pending_replies)
        again = reg.drain_replies()[0]
        assert again["seq"] == 2
        assert (tm.home_dir / "replies" / "reply-1.md").is_file()
        reg.shutdown_all()

    def test_resume_guards(self, tmp_path, renderer, monkeypatch):
        reg = make_registry(tmp_path)
        assert "unknown" in reg.resume_teammate("agt-nope")
        key, _ = reg.spawn()
        wait_until(lambda: reg.get(key).state == "idle")
        assert "still alive" in reg.resume_teammate(key)  # 산 사람 부활 금지
        reg.kill(key)
        monkeypatch.setenv("AGENT_CLI_MAX_AGENTS", "1")
        k2, _ = reg.spawn()
        assert "limit" in reg.resume_teammate(key)  # 상한 존중
        reg.shutdown_all()

    def test_resumed_teammate_is_session_revivable_again(self, tmp_path, renderer):
        # 부활 → revivable 복귀 → 세션 resume 의 자동 재생성 대상.
        reg1 = make_registry(tmp_path)
        key, _ = reg1.spawn()
        wait_until(lambda: reg1.get(key).state == "idle")
        reg1.kill(key)
        assert reg1.resume_teammate(key) == ""
        wait_until(lambda: reg1.get(key).state == "idle")
        reg1.shutdown_all()

        reg2 = make_registry(tmp_path)
        assert reg2.restore() == 1  # 다시 자동 재생성 대상
        reg2.shutdown_all()

    def test_tombstone_from_session_restore_can_resume(self, tmp_path, renderer):
        # 세션 1 에서 kill → 세션 2 restore 는 툼스톤(dead)으로만 복원 —
        # 그 툼스톤도 mode:"resume" 으로 컨텍스트째 부활 가능.
        reg1 = make_registry(tmp_path)
        key, _ = reg1.spawn()
        assert wait_until(lambda: reg1.get(key).state == "idle")
        reg1.get(key).ctx.add({"role": "user", "content": "from-first-life"})
        reg1.kill(key)
        reg1.shutdown_all()

        reg2 = make_registry(tmp_path)
        reg2.restore()
        assert reg2.get(key).state == "dead"  # 툼스톤
        assert reg2.resume_teammate(key) == ""
        tm = reg2.get(key)
        assert wait_until(lambda: tm.state == "idle")
        assert any("from-first-life" in str(m) for m in tm.ctx.get_raw_messages())
        reg2.shutdown_all()

    def test_died_teammate_resume_clears_error(self, tmp_path, renderer, monkeypatch):
        import agent_cli.subagent.runner as runner_mod

        real = runner_mod.create_subagent_ctx
        monkeypatch.setattr(
            runner_mod, "create_subagent_ctx", lambda *a, **k: (None, "boom")
        )
        reg = make_registry(tmp_path)
        key, _ = reg.spawn()
        assert wait_until(lambda: reg.get(key).state == "dead")
        reg.drain_replies()  # died 통지 소비
        monkeypatch.setattr(runner_mod, "create_subagent_ctx", real)  # 원인 해소
        assert reg.resume_teammate(key) == ""
        tm = reg.get(key)
        assert wait_until(lambda: tm.state == "idle")
        assert tm.error == ""  # 새 생 — 사인 리셋
        reg.shutdown_all()

    def test_tool_mode_resume_with_task(self, tmp_path, renderer):
        reg = make_registry(tmp_path)
        key, _ = reg.spawn()
        wait_until(lambda: reg.get(key).state == "idle")
        reg.kill(key)
        r = tool_agent(
            {"mode": "resume", "key": key, "task": "continue the work"},
            registry=reg,
        )
        assert r.success
        assert "remembers ALL previous exchanges" in r.output
        assert "task queued" in r.output
        assert wait_until(reg.has_pending_replies)
        reg.shutdown_all()

    def test_tool_mode_resume_validation(self):
        from agent_cli.tools.registry import TOOLS

        tool = TOOLS["agent"]
        assert tool.validate({"mode": "resume"}) is not None  # key 필수
        assert tool.validate({"mode": "resume", "key": "agt-1"}) is None

    def test_died_notice_mentions_resume(self):
        rec = build_reply_record(
            {
                "kind": "died",
                "key": "agt-x",
                "profile": "",
                "success": False,
                "output": "boom",
            }
        )
        assert '"mode":"resume"' in rec["content"]


# ── 멤버십 변화의 인스펙터 즉시 반영 (v4.61.0) ──


class TestInspectorImmediateReflection:
    def _web_with_snapshot(self):
        from agent_cli.render.web import WebRenderer

        r = WebRenderer()
        r.note_system_prompt([("Base", "BASE"), ("Agent Profiles", "CATALOG")], turn=1)
        return r

    def test_update_prompt_section_insert_replace_remove(self):
        r = self._web_with_snapshot()
        # 신설 — 카탈로그 뒤에 삽입
        r.update_prompt_section("", "Live Agents", "- `agt-1` (coder)")
        names = [s["name"] for s in r.prompt_snapshot("")["sections"]]
        assert names == ["Base", "Agent Profiles", "Live Agents"]
        # 교체 + 총계 재계산
        r.update_prompt_section("", "Live Agents", "- `agt-1`\n- `agt-2`")
        snap = r.prompt_snapshot("")
        live = next(s for s in snap["sections"] if s["name"] == "Live Agents")
        assert "agt-2" in live["text"]
        assert snap["total_chars"] == sum(s["chars"] for s in snap["sections"]) + 4
        # 제거 (마지막 teammate 사망 → 섹션 소멸)
        r.update_prompt_section("", "Live Agents", "")
        names = [s["name"] for s in r.prompt_snapshot("")["sections"]]
        assert "Live Agents" not in names
        # 열린 인스펙터 재조회 신호가 흘렀다 (transient)
        # → connection 등록 후 한 번 더 갱신해 이벤트 수신 확인
        from agent_cli.render.web import WebConnection

        conn = WebConnection(id="c")
        r.register_connection(conn)
        r.update_prompt_section("", "Live Agents", "- back")
        events = []
        while not conn.queue.empty():
            events.append(conn.queue.get_nowait())
        assert any(e == "prompt_changed" for e, _ in events)

    def test_kill_reflects_in_main_snapshot_immediately(self, tmp_path, monkeypatch):
        # 대화 창/도구 kill 공통 경로 — 다음 턴을 기다리지 않고 main
        # 스냅샷의 Live Teammates 에서 즉시 사라진다.
        import agent_cli.render as render_mod

        r = self._web_with_snapshot()
        monkeypatch.setattr(render_mod, "get_renderer", lambda: r)
        reg = make_registry(tmp_path)
        key, _ = reg.spawn()
        wait_until(lambda: reg.get(key).state == "idle")
        live = next(
            s for s in r.prompt_snapshot("")["sections"] if s["name"] == "Live Agents"
        )
        assert key in live["text"]  # spawn 즉시 광고
        reg.kill(key)
        names = [s["name"] for s in r.prompt_snapshot("")["sections"]]
        assert "Live Agents" not in names  # 유일 멤버 사망 → 섹션 소멸

    def test_no_snapshot_is_safe_noop(self, tmp_path, monkeypatch):
        # CLI(minimal)·스냅샷 미존재 환경에서도 멤버십 변화가 안전.
        from agent_cli.render.web import WebRenderer

        import agent_cli.render as render_mod

        r = WebRenderer()  # note_system_prompt 안 함 — 스냅샷 없음
        monkeypatch.setattr(render_mod, "get_renderer", lambda: r)
        reg = make_registry(tmp_path)
        key, _ = reg.spawn()
        wait_until(lambda: reg.get(key).state == "idle")
        reg.kill(key)  # 예외 없이 통과하면 OK
        assert r.prompt_snapshot("") is None


# ── resume 유도 (v4.61.1 — 모델이 spawn 으로 새 키를 만들던 실사용 이슈) ──


class TestResumeGuidance:
    def test_kill_output_teaches_resume(self, tmp_path, renderer):
        reg = make_registry(tmp_path)
        key, _ = reg.spawn()
        wait_until(lambda: reg.get(key).state == "idle")
        r = tool_agent({"mode": "kill", "key": key}, registry=reg)
        assert '"mode":"resume"' in r.output and key in r.output
        assert "PRESERVED" in r.output

    def test_status_marks_dead_as_resumable(self, tmp_path, renderer):
        reg = make_registry(tmp_path)
        key, _ = reg.spawn()
        wait_until(lambda: reg.get(key).state == "idle")
        reg.kill(key)
        s = reg.format_status(key)
        assert "resumable" in s and '"mode":"resume"' in s

    def test_spawn_hints_when_same_role_dead_exists(
        self, tmp_path, renderer, monkeypatch
    ):
        import agent_cli.subagent.profiles as profiles_mod

        d = tmp_path / "roles"
        d.mkdir()
        (d / "comedian.md").write_text(
            "---\ndescription: gag\n---\nYou joke.", encoding="utf-8"
        )
        monkeypatch.setattr(profiles_mod, "_profile_loader", ResourceLoader([d]))
        reg = make_registry(tmp_path)
        k1, _ = reg.spawn(profile="comedian")
        wait_until(lambda: reg.get(k1).state == "idle")
        reg.kill(k1)
        # 같은 역할 재spawn — 실사용 시나리오 ("다시 시작하자" → 모델이 spawn)
        r = tool_agent({"mode": "spawn", "profile": "comedian"}, registry=reg)
        assert r.success
        assert "NO memory" in r.output and k1 in r.output
        assert '"mode":"resume"' in r.output
        # dead 없는 역할은 힌트 없음
        r2 = tool_agent({"mode": "spawn", "profile": "comedian"}, registry=reg)
        assert "NO memory" in r2.output  # k1 여전히 dead → 힌트 유지
        reg.shutdown_all()

    def test_spawn_without_dead_role_has_no_hint(self, tmp_path, renderer):
        reg = make_registry(tmp_path)
        r = tool_agent({"mode": "spawn"}, registry=reg)
        assert "NO memory" not in r.output
        reg.shutdown_all()


# ── @teammates / @agt-<key> 사용자 명령 (범위 B, v4.62.0) ──


class TestAtCommand:
    class _Out:
        def __init__(self):
            self.calls = []

        def list_agents(self, agents, live_status=""):
            self.calls.append(("list", agents, live_status))

        def agent_dispatch_result(self, text, success):
            self.calls.append(("result", text, success))

    def _dispatch(self, message, registry):
        from agent_cli.main import _try_dispatch_agent_command

        out = self._Out()
        handled = _try_dispatch_agent_command(message, out, registry)
        return handled, out.calls

    def test_at_agents_lists_catalog_and_roster(self, tmp_path, renderer):
        reg = make_registry(tmp_path)
        key, _ = reg.spawn(name="ui")
        wait_until(lambda: reg.get(key).state == "idle")
        handled, calls = self._dispatch("@agents", reg)
        assert handled and calls[0][0] == "list"
        assert isinstance(calls[0][1], list)  # 프로파일 카탈로그
        assert key in calls[0][2] and "ui" in calls[0][2]  # live roster
        reg.shutdown_all()

    def test_at_agents_without_registry(self):
        handled, calls = self._dispatch("@agents", None)
        assert handled and calls[0][0] == "list"
        assert calls[0][2] == ""  # live roster 없음

    def test_at_key_message_requests_as_user(self, tmp_path, renderer):
        reg = make_registry(tmp_path)
        key, _ = reg.spawn()
        wait_until(lambda: reg.get(key).state == "idle")
        handled, calls = self._dispatch(f"@{key} 이전에 뭐 했었지?", reg)
        assert handled and calls[0][2] is True  # success
        assert "main LLM 대화에는 섞이지 않음" in calls[0][1]
        # user 발신 → 처리되지만 main pending 비오염 (D8)
        assert wait_until(lambda: reg.get(key).handled == 1)
        assert not reg.has_pending_replies()
        # 회신 out 메시지에 to=user 태깅 (CLI 콘솔 수신 근거)
        outs = [
            c for c in renderer.named("agent_message") if c[1]["direction"] == "out"
        ]
        assert outs and outs[0][1]["to"] == "user"
        reg.shutdown_all()

    def test_at_key_without_message_shows_status(self, tmp_path, renderer):
        reg = make_registry(tmp_path)
        key, _ = reg.spawn()
        wait_until(lambda: reg.get(key).state == "idle")
        handled, calls = self._dispatch(f"@{key}", reg)
        assert handled and key in calls[0][1] and "idle" in calls[0][1]
        reg.shutdown_all()

    def test_at_key_unknown_is_error(self, tmp_path, renderer):
        reg = make_registry(tmp_path)
        handled, calls = self._dispatch("@agt-nope hello", reg)
        assert handled and calls[0][2] is False and "unknown" in calls[0][1]

    def test_at_profile_spawn_suffix(self, tmp_path, renderer, monkeypatch):
        import agent_cli.subagent.profiles as profiles_mod

        roles_dir = tmp_path / "roles"
        roles_dir.mkdir()
        (roles_dir / "coder.md").write_text("---\n---\nYou code.", encoding="utf-8")
        monkeypatch.setattr(
            profiles_mod, "_profile_loader", ResourceLoader([roles_dir])
        )
        monkeypatch.setattr(profiles_mod, "_PROFILE_SEARCH_PATHS", [roles_dir])
        reg = make_registry(tmp_path)
        handled, calls = self._dispatch("@coder-spawn 초기 작업 하나", reg)
        assert handled and calls[0][2] is True
        assert "상주 시작" in calls[0][1] and "초기 task 전달됨" in calls[0][1]
        tm = next(iter(reg._agents.values()))
        assert tm.profile_name == "coder"
        assert wait_until(lambda: tm.handled == 1)  # 초기 task 처리됨
        reg.shutdown_all()

    def test_at_profile_spawn_without_task(self, tmp_path, renderer, monkeypatch):
        import agent_cli.subagent.profiles as profiles_mod

        roles_dir = tmp_path / "roles"
        roles_dir.mkdir()
        (roles_dir / "coder.md").write_text("---\n---\nYou code.", encoding="utf-8")
        monkeypatch.setattr(
            profiles_mod, "_profile_loader", ResourceLoader([roles_dir])
        )
        monkeypatch.setattr(profiles_mod, "_PROFILE_SEARCH_PATHS", [roles_dir])
        reg = make_registry(tmp_path)
        handled, calls = self._dispatch("@coder-spawn", reg)
        assert handled and calls[0][2] is True
        assert "초기 task" not in calls[0][1]
        reg.shutdown_all()

    def test_at_profile_spawn_unknown_profile(self, tmp_path, renderer):
        reg = make_registry(tmp_path)
        handled, calls = self._dispatch("@nosuch-spawn 작업", reg)
        assert handled and calls[0][2] is False

    def test_at_teammates_removed(self, tmp_path, renderer):
        # 5.0.0 하드컷 — @teammates 는 더 이상 명령이 아니다 (run 폴스루).
        reg = make_registry(tmp_path)
        handled, calls = self._dispatch("@teammates", reg)
        assert handled is False and calls == []

    def test_non_command_at_falls_through(self, tmp_path, renderer):
        reg = make_registry(tmp_path)
        handled, calls = self._dispatch("@explorer 조사해줘", reg)
        assert handled is False and calls == []  # run 경로로 폴스루


class TestParseAtProfile:
    """``@<profile>[-run|-spawn]`` 접미사 파싱 (설계 §3.5)."""

    def _with_profiles(self, monkeypatch, tmp_path, names):
        import agent_cli.subagent.profiles as profiles_mod

        roles_dir = tmp_path / "roles"
        roles_dir.mkdir(exist_ok=True)
        for n in names:
            (roles_dir / f"{n}.md").write_text("---\n---\nbody", encoding="utf-8")
        monkeypatch.setattr(
            profiles_mod, "_profile_loader", ResourceLoader([roles_dir])
        )
        monkeypatch.setattr(profiles_mod, "_PROFILE_SEARCH_PATHS", [roles_dir])

    def test_plain_name_is_run(self, monkeypatch, tmp_path):
        from agent_cli.main import _parse_at_profile

        self._with_profiles(monkeypatch, tmp_path, ["coder"])
        assert _parse_at_profile("coder") == ("coder", "run")

    def test_suffixes(self, monkeypatch, tmp_path):
        from agent_cli.main import _parse_at_profile

        self._with_profiles(monkeypatch, tmp_path, ["coder"])
        assert _parse_at_profile("coder-run") == ("coder", "run")
        assert _parse_at_profile("coder-spawn") == ("coder", "spawn")

    def test_exact_profile_wins_over_suffix(self, monkeypatch, tmp_path):
        # 프로파일 이름 자체가 -run 으로 끝나는 극단 케이스 — 실존 우선.
        from agent_cli.main import _parse_at_profile

        self._with_profiles(monkeypatch, tmp_path, ["foo-run"])
        assert _parse_at_profile("foo-run") == ("foo-run", "run")

    def test_hyphenated_profile_with_suffix(self, monkeypatch, tmp_path):
        from agent_cli.main import _parse_at_profile

        self._with_profiles(monkeypatch, tmp_path, ["code-reviewer"])
        assert _parse_at_profile("code-reviewer-spawn") == ("code-reviewer", "spawn")

    def test_unknown_name_passes_through(self, monkeypatch, tmp_path):
        from agent_cli.main import _parse_at_profile

        self._with_profiles(monkeypatch, tmp_path, [])
        assert _parse_at_profile("ghost") == ("ghost", "run")


class TestMinimalConsoleReception:
    def _minimal(self):
        import io

        from rich.console import Console

        from agent_cli.render.minimal import MinimalRenderer

        buf = io.StringIO()
        r = MinimalRenderer(Console(file=buf, force_terminal=False))
        return r, buf

    def test_prints_user_directed_reply(self):
        r, buf = self._minimal()
        r.agent_message(
            key="agt-1",
            direction="out",
            author="agt-1",
            text="답변입니다",
            to="user",
        )
        out = buf.getvalue()
        assert "agt-1 → user" in out and "답변입니다" in out

    def test_skips_main_directed_and_inbound(self):
        r, buf = self._minimal()
        r.agent_message(
            key="agt-1", direction="out", author="agt-1", text="X", to="main"
        )
        r.agent_message(
            key="agt-1", direction="in", author="user", text="Y", to="agt-1"
        )
        assert buf.getvalue() == ""  # 이중 표시/자기 메시지 방지

    def test_prints_user_directed_question(self):
        r, buf = self._minimal()
        r.agent_message(
            key="agt-1",
            direction="question",
            author="agt-1",
            text="어느 파일요?",
            to="user:bob",
        )
        out = buf.getvalue()
        assert "❓" in out and "어느 파일요?" in out


class TestRosterInitialIdle:
    def test_roster_reflects_idle_after_startup(self, tmp_path, renderer):
        # v4.62.1 회귀: task 없는 spawn(재생성 동형)의 starting→idle 전환이
        # roster 에 브로드캐스트되어야 한다 — 안 하면 UI 가 starting 고착.
        reg = make_registry(tmp_path)
        key, _ = reg.spawn()
        assert wait_until(lambda: reg.get(key).state == "idle")
        assert wait_until(
            lambda: any(
                e["key"] == key and e["state"] == "idle"
                for c in renderer.named("agent_roster")[-1:]
                for e in c[1]["roster"]
            )
        )
        reg.shutdown_all()

    def test_restored_agent_roster_shows_idle(self, tmp_path, renderer):
        reg1 = make_registry(tmp_path)
        key, _ = reg1.spawn()
        wait_until(lambda: reg1.get(key).state == "idle")
        reg1.shutdown_all()
        reg2 = make_registry(tmp_path)
        assert reg2.restore() == 1
        assert wait_until(lambda: reg2.get(key).state == "idle")
        last = renderer.named("agent_roster")[-1][1]["roster"]
        entry = next(e for e in last if e["key"] == key)
        assert entry["state"] == "idle"  # starting 고착 없음
        reg2.shutdown_all()


# ── instant-agent: instructions 인라인 프로파일 (U-A, v4.63.0) ──


class TestInstantAgent:
    def test_compose_rules(self):
        from agent_cli.subagent.agents_live import compose_role_prompt

        both = compose_role_prompt("FILE BODY", "INLINE")
        assert both.startswith("FILE BODY")
        assert "## Additional instructions\nINLINE" in both  # 파일→인라인 순
        assert compose_role_prompt("", "ONLY INLINE") == "ONLY INLINE"
        assert compose_role_prompt("ONLY FILE", "") == "ONLY FILE"
        assert compose_role_prompt("", "") == ""

    def test_instructions_only_spawn_becomes_role(self, tmp_path, renderer):
        reg = make_registry(tmp_path)
        key, err = reg.spawn(instructions="너는 wire-format 전문가다.")
        assert err == ""
        tm = reg.get(key)
        assert tm.role_prompt == "너는 wire-format 전문가다."
        assert tm.instructions == "너는 wire-format 전문가다."
        reg.shutdown_all()

    def test_profile_plus_instructions_overlay(self, tmp_path, renderer, monkeypatch):
        import agent_cli.subagent.profiles as profiles_mod

        d = tmp_path / "roles"
        d.mkdir()
        (d / "coder.md").write_text(
            "---\ndescription: builds\n---\nYou build things.", encoding="utf-8"
        )
        monkeypatch.setattr(profiles_mod, "_profile_loader", ResourceLoader([d]))
        reg = make_registry(tmp_path)
        key, _ = reg.spawn(profile="coder", instructions="이 세션에선 테스트만 담당.")
        tm = reg.get(key)
        assert tm.role_prompt.startswith("You build things.")
        assert tm.role_prompt.endswith("이 세션에선 테스트만 담당.")
        assert "## Additional instructions" in tm.role_prompt
        reg.shutdown_all()

    def test_instructions_survive_resume_and_revival(self, tmp_path, renderer):
        # 요구 핵심: resume/부활 시 동일 system prompt (manifest 영속).
        reg1 = make_registry(tmp_path)
        key, _ = reg1.spawn(instructions="INSTANT ROLE")
        wait_until(lambda: reg1.get(key).state == "idle")
        reg1.shutdown_all()

        reg2 = make_registry(tmp_path)
        assert reg2.restore() == 1
        tm = reg2.get(key)
        assert tm.role_prompt == "INSTANT ROLE" and tm.instructions == "INSTANT ROLE"
        wait_until(lambda: tm.state == "idle")
        reg2.kill(key)
        assert reg2.resume_teammate(key) == ""  # 부활도 동일 정체성
        assert reg2.get(key).role_prompt == "INSTANT ROLE"
        reg2.shutdown_all()

    def test_tool_spawn_threads_instructions(self, tmp_path, renderer):
        # spy 러너를 처음부터 주입 — worker 가 실제로 합성 역할(agent_role)
        # 로 도는지 러너 수신 kwargs 로 검증 (교체 타이밍 레이스 없음).
        captured = {}

        def spy_runner(query, ctx, **kw):
            captured.update(kw)
            return _FakeLoopResult(output="ok"), 0.01

        reg = make_registry(tmp_path, runner=spy_runner)
        r = tool_agent(
            {"mode": "spawn", "instructions": "ad-hoc 전문가", "task": "일해"},
            registry=reg,
        )
        assert r.success
        key = next(iter(reg._agents))
        assert reg.get(key).role_prompt == "ad-hoc 전문가"
        assert wait_until(lambda: reg.get(key).handled == 1)
        assert captured.get("agent_role") == "ad-hoc 전문가"
        reg.shutdown_all()


# ── mode:"run" — 일회성 흡수 + mode-aware 배칭 (PR-3b, 5.0.0) ──


class TestRunMode:
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

    def test_parallel_batchable_is_mode_aware(self):
        from agent_cli.tools.registry import TOOLS

        tool = TOOLS["agent"]
        assert tool.parallel_safe is True
        assert tool.parallel_batchable({"mode": "run", "task": "x"}) is True
        assert tool.parallel_batchable({"mode": "spawn"}) is False
        assert tool.parallel_batchable({"mode": "request", "key": "k"}) is False

    def test_run_validate_requires_task(self):
        from agent_cli.tools.registry import TOOLS

        tool = TOOLS["agent"]
        assert "requires" in tool.validate({"mode": "run"})
        assert tool.validate({"mode": "run", "task": "do"}) is None

    def test_run_single_through_full_loop(self, tmp_path, renderer):
        # main 이 run op → 같은 provider 로 서브루프가 돌아 결과가 그 턴
        # 관찰(STATUS/RESULT)로 — 구 delegate 의미 그대로.
        import json
        from unittest.mock import MagicMock

        from agent_cli.context.manager import ContextManager
        from agent_cli.loop import run_loop
        from agent_cli.providers.base import LLMResponse

        def emit(action, ai):
            return json.dumps({"thought": "t", "action": action, "action_input": ai})

        ctx = ContextManager(tmp_path / "sess", max_context_tokens=30_000)
        provider = MagicMock()
        provider.call.side_effect = [
            LLMResponse(content=emit("agent", {"mode": "run", "task": "count to 3"})),
            LLMResponse(content=emit("complete", {"result": "1 2 3"})),  # 서브루프
            LLMResponse(content=emit("complete", {"result": "done"})),  # main
        ]
        result = run_loop(
            query="use run",
            provider=provider,
            capabilities=self._caps(),
            model="m",
            ctx=ctx,
        )
        assert result.success and result.output == "done"
        obs = [
            m
            for m in ctx.get_raw_messages()
            if m.get("role") == "user" and m.get("tool") == "agent"
        ]
        assert obs and "STATUS: success" in obs[0]["content"]
        assert "1 2 3" in obs[0]["content"]

    def test_run_carries_instructions_to_subagent(self, tmp_path, renderer):
        # instant + run: 인라인 지시가 서브루프 system 의 Role 로.
        import json
        from unittest.mock import MagicMock

        from agent_cli.context.manager import ContextManager
        from agent_cli.loop import run_loop
        from agent_cli.providers.base import LLMResponse

        def emit(action, ai):
            return json.dumps({"thought": "t", "action": action, "action_input": ai})

        ctx = ContextManager(tmp_path / "sess", max_context_tokens=30_000)
        provider = MagicMock()
        provider.call.side_effect = [
            LLMResponse(
                content=emit(
                    "agent",
                    {"mode": "run", "task": "t", "instructions": "MARKER-ROLE-9"},
                )
            ),
            LLMResponse(content=emit("complete", {"result": "sub done"})),
            LLMResponse(content=emit("complete", {"result": "done"})),
        ]
        run_loop(
            query="q",
            provider=provider,
            capabilities=self._caps(),
            model="m",
            ctx=ctx,
        )
        # 두 번째 LLM 호출(서브루프)의 system 에 인라인 역할이 실렸다
        _, kwargs = provider.call.call_args_list[1]
        assert "MARKER-ROLE-9" in kwargs["system"]
        # main(1·3번째)에는 없음
        assert "MARKER-ROLE-9" not in provider.call.call_args_list[0].kwargs["system"]

    def test_run_fanout_multiop_parallel_batch(self, tmp_path, renderer):
        # 멀티-op run ×2 = 한 배치(병렬 엔진) → [1/2][2/2] 결합 관찰.
        import json
        from unittest.mock import MagicMock

        from agent_cli.context.manager import ContextManager
        from agent_cli.loop import AgentLoop
        from agent_cli.providers.base import LLMResponse
        from agent_cli.wire_formats import get as get_wf

        wf = get_wf("md_array")

        def turn(ops):
            # md_array 실제 envelope: ## Thought / ## Action + flat op 배열
            return "## Thought\nt\n\n## Action\n" + json.dumps(ops)

        ctx = ContextManager(tmp_path / "sess", max_context_tokens=30_000)
        provider = MagicMock()
        responses = [
            turn(
                [
                    {"action": "agent", "mode": "run", "task": "job A"},
                    {"action": "agent", "mode": "run", "task": "job B"},
                ]
            ),
            # 서브루프 2개 (병렬 — 순서 무관하게 각자 complete)
            turn([{"action": "complete", "result": "A-done"}]),
            turn([{"action": "complete", "result": "B-done"}]),
            turn([{"action": "complete", "result": "main-done"}]),
        ]
        provider.call.side_effect = [LLMResponse(content=r) for r in responses]
        result = AgentLoop(
            query="fan out",
            provider=provider,
            capabilities=self._caps(),
            model="m",
            ctx=ctx,
            wire_format=wf,
        ).run()
        assert result.success
        combined = [
            m
            for m in ctx.get_raw_messages()
            if m.get("role") == "user" and "[Task 1]" in str(m.get("content"))
        ]
        assert len(combined) == 1  # 배치 = 결합 관찰 1개 (병렬 엔진 관통)
        c = combined[0]["content"]
        assert "[Task 2]" in c and "A-done" in c and "B-done" in c
        # 두 서브루프가 실제로 각각 돌았다 (main 2 + sub 2 = 4 콜)
        assert provider.call.call_count == 4


class TestToolEventSubscriptions:
    """도구 이벤트 구독 (5.8.0) — spawn 선언·팬아웃 배칭·LGTM 억제·영속."""

    def _spawn_watcher(self, reg, subs, runner_reply="LGTM"):
        key, err = reg.spawn(subscribe=subs)
        assert err == ""
        wait_until(lambda: reg.get(key).state == "idle")
        return key

    def test_spawn_param_sets_subscriptions(self, tmp_path, renderer):
        reg = make_registry(tmp_path)
        key = self._spawn_watcher(reg, ["write_file", "edit_file"])
        assert reg.get(key).subscriptions == ["write_file", "edit_file"]
        assert "watching: write_file, edit_file" in reg.format_status(key)
        reg.shutdown_all()

    def test_profile_frontmatter_subscribes(self, tmp_path, renderer, monkeypatch):
        import agent_cli.subagent.profiles as profiles_mod

        roles_dir = tmp_path / "roles"
        roles_dir.mkdir()
        (roles_dir / "watcher.md").write_text(
            "---\nsubscribes:\n  - shell\n---\nYou watch.", encoding="utf-8"
        )
        monkeypatch.setattr(
            profiles_mod, "_profile_loader", ResourceLoader([roles_dir])
        )
        monkeypatch.setattr(profiles_mod, "_PROFILE_SEARCH_PATHS", [roles_dir])
        reg = make_registry(tmp_path)
        key, err = reg.spawn(profile="watcher")
        assert err == "" and reg.get(key).subscriptions == ["shell"]
        # 명시 파라미터가 frontmatter 를 오버라이드
        key2, _ = reg.spawn(profile="watcher", subscribe=["*"])
        assert reg.get(key2).subscriptions == ["*"]
        reg.shutdown_all()

    def test_invalid_subscribe_rejected(self, tmp_path, renderer):
        reg = make_registry(tmp_path)
        key, err = reg.spawn(subscribe=[1, 2])  # type: ignore[list-item]
        assert key == "" and "subscribe" in err

    def test_publish_batches_per_subscriber_and_wildcard(self, tmp_path, renderer):
        reg = make_registry(tmp_path)
        k_write = self._spawn_watcher(reg, ["write_file"])
        k_all = self._spawn_watcher(reg, ["*"])
        k_none = self._spawn_watcher(reg, ["fetch"])
        events = [
            {
                "tool": "write_file",
                "summary": "a.py",
                "body": "diff-A",
                "success": True,
            },
            {
                "tool": "edit_file",
                "summary": "b.py",
                "body": "diff-B",
                "success": False,
            },
        ]
        n = reg.publish_tool_events(events, turn=3)
        assert n == 2  # write 구독자(1건 매칭) + 와일드카드(2건) — fetch 구독자 제외
        assert wait_until(lambda: reg.get(k_write).handled == 1)
        assert wait_until(lambda: reg.get(k_all).handled == 1)
        assert reg.get(k_none).handled == 0
        # 수신 내용 검증 — 러너가 받은 메시지 (in 방향 렌더 기록)
        ins = [
            c[1] for c in renderer.named("agent_message") if c[1]["direction"] == "in"
        ]
        w_msg = next(m["text"] for m in ins if m["key"] == k_write)
        assert "✓ write_file a.py" in w_msg and "diff-A" in w_msg
        assert "edit_file" not in w_msg  # 구독 밖 이벤트는 미포함
        all_msg = next(m["text"] for m in ins if m["key"] == k_all)
        assert "✗ edit_file b.py" in all_msg and "2건" in all_msg
        reg.shutdown_all()

    def test_lgtm_reply_suppressed_from_main(self, tmp_path, renderer):
        # LGTM 회신 → 창에만 (main mailbox 비오염); 발견 회신 → main 배달.
        replies = iter(["LGTM", "MAJOR: a.py:3 — off-by-one"])

        def runner(query, ctx, **kw):
            class R:
                success = True
                output = next(replies)

            return R(), 0.01

        reg = make_registry(tmp_path, runner=runner)
        key = self._spawn_watcher(reg, ["write_file"])
        reg.publish_tool_events(
            [{"tool": "write_file", "summary": "a.py", "body": "", "success": True}]
        )
        assert wait_until(lambda: reg.get(key).handled == 1)
        assert not reg.has_pending_replies()  # LGTM 억제
        reg.publish_tool_events(
            [{"tool": "write_file", "summary": "a.py", "body": "", "success": True}]
        )
        assert wait_until(lambda: reg.get(key).handled == 2)
        assert wait_until(lambda: reg.has_pending_replies())  # 발견은 배달
        out = reg.drain_replies()
        assert "off-by-one" in out[0]["output"]
        reg.shutdown_all()

    def test_subscriptions_survive_manifest_roundtrip(self, tmp_path, renderer):
        reg1 = make_registry(tmp_path)
        key = self._spawn_watcher(reg1, ["write_file", "complete"])
        reg1.shutdown_all()
        reg2 = make_registry(tmp_path)
        assert reg2.restore() == 1
        assert reg2.get(key).subscriptions == ["write_file", "complete"]
        reg2.shutdown_all()

    def test_wants_tool_events_gate(self, tmp_path, renderer):
        reg = make_registry(tmp_path)
        assert reg.wants_tool_events() is False
        key = self._spawn_watcher(reg, ["shell"])
        assert reg.wants_tool_events() is True
        reg.kill(key)
        wait_until(lambda: reg.get(key).state == "dead")
        assert reg.wants_tool_events() is False
        reg.shutdown_all()


class TestLoopToolEventTap:
    """루프 탭 통합 — 도구 실행/complete 이 턴 경계에 registry 로 발행."""

    class _FakeRegistry:
        def __init__(self):
            self.published = []

        def wants_tool_events(self):
            return True

        def publish_tool_events(self, events, *, turn=0):
            self.published.append((turn, events))
            return 1

        def drain_replies(self):
            return []

    def _run_loop(self, responses, tmp_path, registry):
        from unittest.mock import MagicMock

        from agent_cli.context.manager import ContextManager
        from agent_cli.loop import AgentLoop
        from agent_cli.providers.base import LLMResponse
        from agent_cli.providers.capabilities import ModelCapabilities

        provider = MagicMock()
        provider.call.side_effect = [LLMResponse(content=r) for r in responses]
        caps = ModelCapabilities(
            context_window=32768,
            max_output_tokens=4096,
            supports_structured_output=False,
            supports_thinking=False,
            thinking_budget=0,
            supports_strict_schema=False,
        )
        from agent_cli import wire_formats

        loop = AgentLoop(
            query="Q",
            provider=provider,
            capabilities=caps,
            model="m",
            ctx=ContextManager(session_dir=tmp_path),
            max_turns=5,
            wire_format=wire_formats.get("react"),
            agent_registry=registry,
        )
        return loop.run()

    def test_tool_and_complete_events_published(self, tmp_path, renderer):
        import json

        reg = self._FakeRegistry()
        turn1 = "## Thought\n쓰기\n\n## Action\n" + json.dumps(
            [
                {
                    "action": "write_file",
                    "path": str(tmp_path / "a.py"),
                    "content": "x = 1\n",
                }
            ]
        )
        turn2 = "## Thought\n끝\n\n## Action\n" + json.dumps(
            [{"action": "complete", "result": "done"}]
        )
        result = self._run_loop([turn1, turn2], tmp_path, reg)
        assert result.success
        # 턴 1: write_file 이벤트 / 턴 2(최종): complete 이벤트 — 둘 다 발행
        tools_by_turn = {
            turn: [e["tool"] for e in events] for turn, events in reg.published
        }
        assert tools_by_turn.get(1) == ["write_file"]
        assert tools_by_turn.get(2) == ["complete"]
        ev = reg.published[0][1][0]
        assert ev["success"] is True and "a.py" in ev["summary"]
