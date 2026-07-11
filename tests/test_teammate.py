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

    def begin_teammate_work(self, **kw):
        self._rec("begin_teammate_work", **kw)

    def end_teammate_work(self, **kw):
        self._rec("end_teammate_work", **kw)

    def teammate_roster(self, roster):
        self._rec("teammate_roster", roster=roster)

    def teammate_message(self, **kw):
        self._rec("teammate_message", **kw)

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
                "role": "researcher",
                "success": True,
                "output": "which branch?",
            }
        )
        assert rec["tool"] == "teammate"
        assert rec["source"] == "teammate_question"
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
        r = tool_teammate(
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

    def test_wait_returns_question_first(self, tmp_path, renderer):
        # 교착 방지: wait 중 질문이 먼저 오면 그걸 반환해야 main 이 답할
        # 기회를 얻는다 (질문을 건너뛰면 상호 대기).
        reg = make_registry(tmp_path, runner=make_asking_runner())
        key, _ = reg.spawn()
        reg.request(key, "task")
        assert wait_until(lambda: reg.get(key).state == "waiting_ask")
        r = tool_teammate({"mode": "wait", "key": key}, registry=reg)
        assert r.success
        assert "STATUS: question" in r.output and "which branch?" in r.output
        assert '"mode":"request"' in r.output  # 답변 방법 안내
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

        return json.loads((tmp_path / "teammates.json").read_text(encoding="utf-8"))

    def test_manifest_written_on_spawn(self, tmp_path, renderer):
        reg = make_registry(tmp_path)
        key, _ = reg.spawn()
        data = self._state(tmp_path)
        assert data["version"] == 1
        entry = next(e for e in data["teammates"] if e["key"] == key)
        assert entry["state"] == "idle"
        reg.shutdown_all()

    def test_kill_is_permanent_in_manifest(self, tmp_path, renderer):
        reg = make_registry(tmp_path)
        key, _ = reg.spawn()
        wait_until(lambda: reg.get(key).state == "idle")
        reg.kill(key)
        entry = next(e for e in self._state(tmp_path)["teammates"] if e["key"] == key)
        assert entry["state"] == "dead"

    def test_session_exit_keeps_teammate_revivable(self, tmp_path, renderer):
        reg = make_registry(tmp_path)
        key, _ = reg.spawn()
        wait_until(lambda: reg.get(key).state == "idle")
        reg.shutdown_all()  # 세션 종료 ≠ kill
        entry = next(e for e in self._state(tmp_path)["teammates"] if e["key"] == key)
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
        import agent_cli.subagent.roles as roles_mod

        roles_dir = tmp_path / "roles"
        roles_dir.mkdir()
        (roles_dir / "researcher.md").write_text(
            "---\nmodel: r-model\n---\nYou are THE researcher.", encoding="utf-8"
        )
        monkeypatch.setattr(roles_mod, "_teammate_loader", ResourceLoader([roles_dir]))

        reg1 = make_registry(tmp_path)
        key, err = reg1.spawn(role="researcher")
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
        (tmp_path / "teammates.json").write_text(
            '{"version": 99, "teammates": [{"key": "agt-x"}]}', encoding="utf-8"
        )
        reg2 = make_registry(tmp_path)
        assert reg2.restore() == 0

    def test_tool_call_refreshes_runtime(self, tmp_path, renderer):
        # restore 된 teammate 가 스폰 없이도 현 세션 배선으로 돌 수 있도록
        # 모든 도구 호출이 runtime 을 갱신한다.
        reg = make_registry(tmp_path)
        r = tool_teammate(
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
            c for c in renderer.named("teammate_message") if c[1]["direction"] == "out"
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
            for c in renderer.named("teammate_message")
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
        assert renderer.named("teammate_roster")  # spawn 시 roster
        reg.request(key, "job", author="user:kim")
        ins = [
            c for c in renderer.named("teammate_message") if c[1]["direction"] == "in"
        ]
        assert ins and ins[0][1]["author"] == "user:kim" and ins[0][1]["text"] == "job"
        reg.shutdown_all()

    def test_roster_reflects_states_and_kill(self, tmp_path, renderer):
        reg = make_registry(tmp_path)
        key, _ = reg.spawn()
        wait_until(lambda: reg.get(key).state == "idle")
        reg.kill(key)
        last = renderer.named("teammate_roster")[-1][1]["roster"]
        entry = next(e for e in last if e["key"] == key)
        assert entry["state"] == "dead"


class TestMailWaker:
    def _waker(self, pending=lambda: True):
        from agent_cli.subagent.teammate import MailWaker

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
        from agent_cli.subagent.teammate import MailWaker

        q = InputQueue()
        reg = self._FakeReg()
        waker = MailWaker(q.enqueue, reg.has_pending_replies)
        calls = []
        q.enqueue(None, "the query")
        self._pump(q, waker, reg, lambda text, *, wake: calls.append((text, wake)))
        assert calls == [("the query", False)]

    def test_wake_cycle_delivers_then_quiesces(self):
        from agent_cli.input_queue import InputQueue
        from agent_cli.subagent.teammate import MailWaker

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
        from agent_cli.subagent.teammate import MailWaker

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
        from agent_cli.subagent.teammate import MailWaker

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
        assert rec["source"] == "teammate_died" and rec["success"] is False
        assert "DIED" in rec["content"]
        assert '"mode":"resume"' in rec["content"]  # 부활 안내 (v4.61.0)
        assert not is_format_intervention(rec)
        # 원인 미상 사망은 resume 이 되살리지 않는다
        assert wait_until(lambda: reg.get(key).state == "dead")
        import json

        entry = json.loads((tmp_path / "teammates.json").read_text())["teammates"][0]
        assert entry["state"] == "dead"

    def test_machinery_crash_notifies_main(self, tmp_path, renderer):
        # 요청 처리기(내부 try) 밖의 기계 사망 — begin_teammate_work 폭발.
        def boom(**kw):
            raise MemoryError("simulated OOM in worker machinery")

        renderer.begin_teammate_work = boom
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
        from agent_cli.subagent.roles import available_roles, load_teammate_role

        for name in ("researcher", "code-reviewer"):
            body, config, err = load_teammate_role(name)
            assert err is None and body
            assert config.get("allowed-tools")
        advertised = dict(available_roles())
        assert "researcher" in advertised and "code-reviewer" in advertised
        assert advertised["researcher"]  # description 필수 (발견 표면)

    def test_disable_model_invocation_hidden(self, tmp_path, monkeypatch):
        import agent_cli.subagent.roles as roles_mod

        (tmp_path / "hidden.md").write_text(
            "---\ndescription: secret\ndisable-model-invocation: true\n---\nbody",
            encoding="utf-8",
        )
        (tmp_path / "visible.md").write_text(
            "---\ndescription: shown\n---\nbody", encoding="utf-8"
        )
        monkeypatch.setattr(roles_mod, "_teammate_loader", ResourceLoader([tmp_path]))
        names = [n for n, _ in roles_mod.available_roles()]
        assert names == ["visible"]
        # auto-spawn 스캔용 include_meta 는 전체 반환
        all_names = [n for n, _ in roles_mod.available_roles(include_meta=True)]
        assert set(all_names) == {"hidden", "visible"}

    def test_prompt_section_lists_roles_with_spawn_example(self):
        from agent_cli.prompts.system_prompt import build_teammate_role_descriptions

        desc = build_teammate_role_descriptions()
        assert "## Available Teammate Roles" in desc
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
            caps, active_tools=["read_file", "teammate"]
        )
        without = build_system_prompt_sections(caps, active_tools=["read_file"])
        names_with = [n for n, _ in with_tool]
        names_without = [n for n, _ in without]
        assert "Teammate Roles" in names_with
        assert "Teammate Roles" not in names_without


class TestAutoSpawn:
    def _roles_dir(self, tmp_path, monkeypatch, *, flagged=True):
        import agent_cli.subagent.roles as roles_mod

        d = tmp_path / "roles"
        d.mkdir(exist_ok=True)
        flag = "auto-spawn: true\n" if flagged else ""
        (d / "concierge.md").write_text(
            f"---\ndescription: greeter\n{flag}---\nYou greet.", encoding="utf-8"
        )
        monkeypatch.setattr(roles_mod, "_teammate_loader", ResourceLoader([d]))
        return d

    def test_flagged_role_spawns_once(self, tmp_path, renderer, monkeypatch):
        self._roles_dir(tmp_path, monkeypatch)
        reg = make_registry(tmp_path)
        assert reg.auto_spawn() == 1
        tm = next(iter(reg._teammates.values()))
        assert tm.role_name == "concierge"
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
        key = next(iter(reg1._teammates))
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
        k1, e1 = reg.spawn(role="", name="ui")
        k2, e2 = reg.spawn(role="", name="api")
        assert e1 == e2 == "" and k1 != k2
        assert reg.get(k1).instance_name == "ui"
        assert reg.get(k2).snapshot()["name"] == "api"
        reg.shutdown_all()

    def test_invalid_instance_name_rejected(self, tmp_path, renderer):
        reg = make_registry(tmp_path)
        key, err = reg.spawn(name="bad name!")
        assert key == "" and "invalid instance name" in err

    def test_label_in_reply_record(self):
        from agent_cli.subagent.teammate import format_teammate_label

        assert format_teammate_label("agt-1", "coder", "ui") == "agt-1 (coder · ui)"
        assert format_teammate_label("agt-1") == "agt-1"
        rec = build_reply_record(
            {
                "kind": "reply",
                "key": "agt-1",
                "role": "coder",
                "name": "ui",
                "success": True,
                "output": "done",
            }
        )
        assert "agt-1 (coder · ui)" in rec["content"]

    def test_name_and_description_survive_resume(self, tmp_path, renderer, monkeypatch):
        import agent_cli.subagent.roles as roles_mod

        d = tmp_path / "roles"
        d.mkdir()
        (d / "coder.md").write_text(
            "---\ndescription: builds things\n---\nYou build.", encoding="utf-8"
        )
        monkeypatch.setattr(roles_mod, "_teammate_loader", ResourceLoader([d]))
        reg1 = make_registry(tmp_path)
        key, _ = reg1.spawn(role="coder", name="ui")
        wait_until(lambda: reg1.get(key).state == "idle")
        reg1.shutdown_all()

        reg2 = make_registry(tmp_path)
        assert reg2.restore() == 1
        tm = reg2.get(key)
        assert tm.instance_name == "ui" and tm.description == "builds things"
        reg2.shutdown_all()


class TestLiveTeammatesSection:
    def test_lists_alive_with_labels_and_descriptions(self, tmp_path, renderer):
        from agent_cli.prompts.system_prompt import build_live_teammates_section

        reg = make_registry(tmp_path)
        k1, _ = reg.spawn(name="ui")
        reg.get(k1).description = "x" * 200  # 광고 요약은 절단
        k2, _ = reg.spawn()
        wait_until(lambda: reg.get(k2).state == "idle")
        reg.kill(k2)  # dead 는 광고에서 제외

        desc = build_live_teammates_section(reg)
        assert "## Live Teammates" in desc
        assert f"`{k1}`" in desc and "(ui)" in desc
        assert f"`{k2}`" not in desc
        assert "..." in desc and "x" * 141 not in desc  # 140자 절단
        assert '"mode":"request"' in desc  # 재사용 유도
        assert "distinct `name`" in desc  # 다중 인스턴스 안내
        reg.shutdown_all()

    def test_empty_or_absent_registry_renders_nothing(self, tmp_path, renderer):
        from agent_cli.prompts.system_prompt import build_live_teammates_section

        assert build_live_teammates_section(None) == ""
        assert build_live_teammates_section(make_registry(tmp_path)) == ""

    def test_hidden_from_teammate_subloops(self, tmp_path, renderer):
        # 이중 게이트: teammate 서브루프는 도구 strip + registry None —
        # 카탈로그도 Live 목록도 자신에겐 보이지 않는다.
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
                caps, active_tools=["teammate"], teammate_registry=reg
            )
        ]
        assert "Teammate Roles" in main_names and "Live Teammates" in main_names
        # teammate 서브루프 (도구 strip → active_tools 에 없음): 둘 다 부재
        sub_names = [
            n
            for n, _ in build_system_prompt_sections(
                caps, active_tools=["read_file"], teammate_registry=None
            )
        ]
        assert "Teammate Roles" not in sub_names
        assert "Live Teammates" not in sub_names
        reg.shutdown_all()

    def test_membership_flag_set_and_consumed(self, tmp_path, renderer):
        from agent_cli.subagent.teammate import (
            consume_teammates_reload,
            notify_teammates_changed,
        )

        consume_teammates_reload()  # 잔여 클리어
        assert consume_teammates_reload() is False
        notify_teammates_changed()
        assert consume_teammates_reload() is True
        assert consume_teammates_reload() is False  # 소비됨
        reg = make_registry(tmp_path)
        key, _ = reg.spawn()  # spawn 이 플래그를 세운다
        assert consume_teammates_reload() is True
        wait_until(lambda: reg.get(key).state == "idle")
        reg.kill(key)  # kill 도
        assert consume_teammates_reload() is True

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
                    content=emit("teammate", {"mode": "spawn", "name": "ui"})
                )
            return LLMResponse(content=emit("complete", {"result": "ok"}))

        provider.call = MagicMock(side_effect=scripted)
        result = run_loop(
            query="spawn one",
            provider=provider,
            capabilities=caps,
            model="m",
            ctx=ctx,
            teammate_registry=reg,
        )
        assert result.success
        # 턴 2 의 시스템 프롬프트(system kwarg)에 광고가 실렸다
        _, kwargs = provider.call.call_args_list[1]
        system = kwargs["system"]
        assert "## Live Teammates" in system
        key = next(iter(reg._teammates))
        assert key in system and "(ui)" in system
        reg.shutdown_all()


# ── v4.60.1 회귀: 실렌더러 시그니처 (worker 부트 사망 사고) ──


class TestRendererSignatureRegression:
    """status() 오호출이 (1) web 부트에서 worker 를 죽이고 (2) 📨 알림을
    try/except 가 조용히 삼키던 사고 — mock 이 아닌 **실렌더러**로 호출을
    고정한다."""

    def test_boot_announce_on_real_web_renderer(self):
        from agent_cli.main import _announce_teammate_boot
        from agent_cli.render.web import WebRenderer

        r = WebRenderer()
        _announce_teammate_boot(r, revived=2, auto=1)  # TypeError 면 즉사
        # transient status 이벤트가 실제로 흘렀는지까지 확인
        # (persistent 버퍼가 아닌 라이브 큐라 connection 으로 수신)
        from agent_cli.render.web import WebConnection

        conn = WebConnection(id="c")
        r.register_connection(conn)
        _announce_teammate_boot(r, revived=1, auto=0)
        events = []
        while not conn.queue.empty():
            events.append(conn.queue.get_nowait())
        assert any(e == "status" and "재생성" in str(d) for e, d in events), (
            f"status 이벤트 미수신: {events[:5]}"
        )

    def test_boot_announce_on_real_minimal_renderer(self):
        import io

        from rich.console import Console

        from agent_cli.main import _announce_teammate_boot
        from agent_cli.render.minimal import MinimalRenderer

        r = MinimalRenderer(Console(file=io.StringIO(), force_terminal=False))
        _announce_teammate_boot(r, revived=1, auto=1)  # TypeError 면 즉사

    def test_reply_notice_actually_emits_on_web(self):
        # try/except 가 시그니처 에러를 삼켜 알림이 조용히 죽어 있었다 —
        # 실렌더러에서 이벤트가 실제로 나가는지 검사.
        from unittest.mock import patch

        from agent_cli.main import _teammate_reply_notice
        from agent_cli.render.web import WebConnection, WebRenderer

        r = WebRenderer()
        conn = WebConnection(id="c")
        r.register_connection(conn)
        with patch("agent_cli.render.get_renderer", return_value=r):
            _teammate_reply_notice({"kind": "reply", "key": "agt-1"})
            _teammate_reply_notice({"kind": "question", "key": "agt-2"})
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
        monkeypatch.setenv("AGENT_CLI_MAX_TEAMMATES", "1")
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
        r = tool_teammate(
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

        tool = TOOLS["teammate"]
        assert tool.validate({"mode": "resume"}) is not None  # key 필수
        assert tool.validate({"mode": "resume", "key": "agt-1"}) is None

    def test_died_notice_mentions_resume(self):
        rec = build_reply_record(
            {
                "kind": "died",
                "key": "agt-x",
                "role": "",
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
        r.note_system_prompt([("Base", "BASE"), ("Teammate Roles", "CATALOG")], turn=1)
        return r

    def test_update_prompt_section_insert_replace_remove(self):
        r = self._web_with_snapshot()
        # 신설 — 카탈로그 뒤에 삽입
        r.update_prompt_section("", "Live Teammates", "- `agt-1` (coder)")
        names = [s["name"] for s in r.prompt_snapshot("")["sections"]]
        assert names == ["Base", "Teammate Roles", "Live Teammates"]
        # 교체 + 총계 재계산
        r.update_prompt_section("", "Live Teammates", "- `agt-1`\n- `agt-2`")
        snap = r.prompt_snapshot("")
        live = next(s for s in snap["sections"] if s["name"] == "Live Teammates")
        assert "agt-2" in live["text"]
        assert snap["total_chars"] == sum(s["chars"] for s in snap["sections"]) + 4
        # 제거 (마지막 teammate 사망 → 섹션 소멸)
        r.update_prompt_section("", "Live Teammates", "")
        names = [s["name"] for s in r.prompt_snapshot("")["sections"]]
        assert "Live Teammates" not in names
        # 열린 인스펙터 재조회 신호가 흘렀다 (transient)
        # → connection 등록 후 한 번 더 갱신해 이벤트 수신 확인
        from agent_cli.render.web import WebConnection

        conn = WebConnection(id="c")
        r.register_connection(conn)
        r.update_prompt_section("", "Live Teammates", "- back")
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
            s
            for s in r.prompt_snapshot("")["sections"]
            if s["name"] == "Live Teammates"
        )
        assert key in live["text"]  # spawn 즉시 광고
        reg.kill(key)
        names = [s["name"] for s in r.prompt_snapshot("")["sections"]]
        assert "Live Teammates" not in names  # 유일 멤버 사망 → 섹션 소멸

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
        r = tool_teammate({"mode": "kill", "key": key}, registry=reg)
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
        import agent_cli.subagent.roles as roles_mod

        d = tmp_path / "roles"
        d.mkdir()
        (d / "comedian.md").write_text(
            "---\ndescription: gag\n---\nYou joke.", encoding="utf-8"
        )
        monkeypatch.setattr(roles_mod, "_teammate_loader", ResourceLoader([d]))
        reg = make_registry(tmp_path)
        k1, _ = reg.spawn(role="comedian")
        wait_until(lambda: reg.get(k1).state == "idle")
        reg.kill(k1)
        # 같은 역할 재spawn — 실사용 시나리오 ("다시 시작하자" → 모델이 spawn)
        r = tool_teammate({"mode": "spawn", "role": "comedian"}, registry=reg)
        assert r.success
        assert "NO memory" in r.output and k1 in r.output
        assert '"mode":"resume"' in r.output
        # dead 없는 역할은 힌트 없음
        r2 = tool_teammate({"mode": "spawn", "role": "comedian"}, registry=reg)
        assert "NO memory" in r2.output  # k1 여전히 dead → 힌트 유지
        reg.shutdown_all()

    def test_spawn_without_dead_role_has_no_hint(self, tmp_path, renderer):
        reg = make_registry(tmp_path)
        r = tool_teammate({"mode": "spawn"}, registry=reg)
        assert "NO memory" not in r.output
        reg.shutdown_all()
