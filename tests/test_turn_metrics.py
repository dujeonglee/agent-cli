"""M2 동시성 계측(turn_metrics) 테스트.

축:
  - 모듈 계약: 기본 off(no-op), enable/emit/None 필드 생략, 동시 emit 무손상
  - 발화 지점 통합: TurnRegistry(dispatch/complete/interrupt),
    effect_lock(enqueue/acquire/release), ContextManager(compact begin/commit,
    query_added), 웹 서버(enqueue, reject 게이트)
  - 직렬 보존: 계측 off 일 때 어떤 이벤트 파일도 생기지 않는다

first_token 발화(loop/llm.py)는 병렬 e2e 스위트가 실 run_loop 로 커버한다
(``test_parallel_turns_e2e.py`` 쪽 추가 테스트).
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest

from agent_cli import turn_metrics
from agent_cli.tools import effect_lock
from agent_cli.tools.effect import EffectIntent, EffectKind


@pytest.fixture(autouse=True)
def _reset():
    """전역 상태 격리 — 계측·효과 락 모두 프로세스 전역이다."""
    turn_metrics.disable()
    effect_lock.reset()
    yield
    turn_metrics.disable()
    effect_lock.reset()


def _read_events(session_dir: Path) -> list[dict]:
    path = session_dir / "turns.jsonl"
    if not path.is_file():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


# ── 모듈 계약 ─────────────────────────────────────────


class TestModuleContract:
    def test_disabled_by_default_emit_is_noop(self, tmp_path):
        turn_metrics.emit("turn", phase="dispatch", turn_id="t1")
        assert not (tmp_path / "turns.jsonl").exists()
        assert not turn_metrics.is_enabled()

    def test_enable_then_emit_writes_jsonl(self, tmp_path):
        turn_metrics.enable(tmp_path)
        turn_metrics.emit("turn", phase="enqueue", queue_id="1", author="alice")
        events = _read_events(tmp_path)
        assert len(events) == 1
        ev = events[0]
        assert ev["event"] == "turn"
        assert ev["phase"] == "enqueue"
        assert ev["queue_id"] == "1"
        assert ev["author"] == "alice"
        assert "timestamp" in ev
        assert isinstance(ev["mono_ms"], float)

    def test_none_fields_are_omitted(self, tmp_path):
        """ "모름"(None)과 "0"이 파일에서 구별돼야 한다 — 직렬 모드의
        active_turns 처럼 프로바이더가 없는 값은 키 자체가 없어야 한다."""
        turn_metrics.enable(tmp_path)
        turn_metrics.emit("compact", phase="begin", active_turns=None, generation=0)
        ev = _read_events(tmp_path)[0]
        assert "active_turns" not in ev
        assert ev["generation"] == 0

    def test_disable_stops_recording(self, tmp_path):
        turn_metrics.enable(tmp_path)
        turn_metrics.emit("reject")
        turn_metrics.disable()
        turn_metrics.emit("reject")
        assert len(_read_events(tmp_path)) == 1

    def test_active_turns_provider(self, tmp_path):
        assert turn_metrics.active_turns() is None
        turn_metrics.set_active_turns_provider(lambda: 3)
        assert turn_metrics.active_turns() == 3

        def boom():
            raise RuntimeError("provider died")

        turn_metrics.set_active_turns_provider(boom)
        assert turn_metrics.active_turns() is None  # 계측 실패는 삼킨다

    def test_disable_clears_provider(self, tmp_path):
        turn_metrics.set_active_turns_provider(lambda: 1)
        turn_metrics.disable()
        assert turn_metrics.active_turns() is None

    def test_concurrent_emits_all_lines_intact(self, tmp_path):
        """스레드 8개 × 50건 — fsio append 직렬화 위에서 전 행이 온전해야
        한다 (계측 자체가 손상되면 실험 데이터가 못 쓰게 된다)."""
        turn_metrics.enable(tmp_path)

        def worker(n: int):
            for i in range(50):
                turn_metrics.emit("lock", phase="acquire", thread=f"w{n}", i=i)

        threads = [threading.Thread(target=worker, args=(n,)) for n in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        events = _read_events(tmp_path)
        assert len(events) == 400
        assert all(e["event"] == "lock" for e in events)


# ── TurnRegistry 발화 ────────────────────────────────


class TestTurnRegistryEvents:
    def test_dispatch_and_complete_events(self, tmp_path):
        from agent_cli.loop.turns import TurnRegistry

        turn_metrics.enable(tmp_path)
        done = threading.Event()

        reg = TurnRegistry(lambda turn: None, max_concurrent=2)
        reg.submit("hello", author="alice", conn_id="c1", queue_id="q7")
        assert reg.wait_idle(timeout=5)
        done.set()
        events = _read_events(tmp_path)
        dispatch = [e for e in events if e.get("phase") == "dispatch"]
        complete = [e for e in events if e.get("phase") == "complete"]
        assert len(dispatch) == 1
        assert dispatch[0]["turn_id"] == "t1"
        assert dispatch[0]["queue_id"] == "q7"
        assert dispatch[0]["author"] == "alice"
        assert dispatch[0]["conn_id"] == "c1"
        assert len(complete) == 1
        assert complete[0]["turn_id"] == "t1"
        assert "interrupted" not in complete[0]  # 정상 완료 → 필드 생략
        # dispatch 가 complete 보다 먼저 (mono_ms 단조)
        assert dispatch[0]["mono_ms"] <= complete[0]["mono_ms"]
        reg.shutdown()

    def test_interrupt_event_and_flag(self, tmp_path):
        from agent_cli.loop.turns import TurnRegistry

        turn_metrics.enable(tmp_path)
        started = threading.Event()
        release = threading.Event()

        def runner(turn):
            started.set()
            release.wait(timeout=5)

        reg = TurnRegistry(runner, max_concurrent=1)
        reg.submit("x", conn_id="c1", queue_id="q1")
        assert started.wait(timeout=5)
        tid = reg.active_ids()[0]
        assert reg.interrupt(tid)
        release.set()
        assert reg.wait_idle(timeout=5)
        events = _read_events(tmp_path)
        assert any(
            e.get("phase") == "interrupt" and e["turn_id"] == tid for e in events
        )
        complete = next(e for e in events if e.get("phase") == "complete")
        assert complete["interrupted"] is True
        reg.shutdown()

    def test_no_metrics_no_events(self, tmp_path):
        """계측 off — 레지스트리가 파일을 만들지 않는다 (opt-in 보존)."""
        from agent_cli.loop.turns import TurnRegistry

        reg = TurnRegistry(lambda turn: None)
        reg.submit("x", queue_id="q1")
        assert reg.wait_idle(timeout=5)
        assert not (tmp_path / "turns.jsonl").exists()
        reg.shutdown()


# ── effect_lock 발화 ─────────────────────────────────


class TestEffectLockEvents:
    def test_lock_lifecycle_events(self, tmp_path):
        turn_metrics.enable(tmp_path)
        effect_lock.set_scope("conflict")
        intent = EffectIntent(EffectKind.FILE_WRITE, "a.txt")
        with effect_lock.hold(intent, key="k"):
            pass
        events = _read_events(tmp_path)
        phases = [e["phase"] for e in events]
        assert phases == ["enqueue", "acquire", "release"]
        acquire = events[1]
        assert acquire["kind"] == "FILE_WRITE"
        assert acquire["exclusive"] is False
        assert acquire["wait_ms"] >= 0
        assert events[2]["held_ms"] >= 0
        # 경로는 정규화된 절대경로 — 키 동일성 규약 그대로 기록된다.
        assert acquire["path"].endswith("a.txt")

    def test_off_scope_emits_nothing(self, tmp_path):
        turn_metrics.enable(tmp_path)
        intent = EffectIntent(EffectKind.FILE_WRITE, "a.txt")
        with effect_lock.hold(intent, key="k"):
            pass
        assert _read_events(tmp_path) == []

    def test_unknown_kind_emits_nothing(self, tmp_path):
        turn_metrics.enable(tmp_path)
        effect_lock.set_scope("conflict")
        with effect_lock.hold(EffectIntent(EffectKind.UNKNOWN, ""), key="k"):
            pass
        assert _read_events(tmp_path) == []

    def test_wait_ms_reflects_contention(self, tmp_path):
        """배타 락 뒤에 줄 선 대기자의 wait_ms 가 실제 대기를 반영한다."""
        turn_metrics.enable(tmp_path)
        effect_lock.set_scope("conflict")
        holder_in = threading.Event()
        release = threading.Event()
        shell = EffectIntent(EffectKind.SHELL, "")

        def holder():
            with effect_lock.hold(shell, key="k"):
                holder_in.set()
                release.wait(timeout=5)

        t = threading.Thread(target=holder)
        t.start()
        assert holder_in.wait(timeout=5)
        time.sleep(0.05)
        release_t = threading.Timer(0.05, release.set)
        release_t.start()
        with effect_lock.hold(EffectIntent(EffectKind.FILE_WRITE, "b.txt"), key="k"):
            pass
        t.join(timeout=5)
        acquires = [
            e
            for e in _read_events(tmp_path)
            if e["phase"] == "acquire" and e["kind"] == "FILE_WRITE"
        ]
        assert len(acquires) == 1
        assert acquires[0]["wait_ms"] >= 40  # ≥ Timer 지연 근사


# ── ContextManager 발화 ──────────────────────────────


class TestContextEvents:
    def test_query_added_event_links_msg_id_and_thread(self, tmp_path):
        from agent_cli.context.manager import ContextManager

        turn_metrics.enable(tmp_path)
        ctx = ContextManager(tmp_path / "session", max_context_tokens=5000)
        ctx.add({"role": "user", "content": "[alice]: hi"})
        events = [e for e in _read_events(tmp_path) if e.get("phase") == "query_added"]
        assert len(events) == 1
        assert events[0]["msg_id"] == "u1"
        assert events[0]["thread"] == threading.current_thread().name

    def test_compact_begin_and_commit_events(self, tmp_path):
        from agent_cli.context.manager import ContextManager

        turn_metrics.enable(tmp_path)
        turn_metrics.set_active_turns_provider(lambda: 2)
        ctx = ContextManager(tmp_path / "session", max_context_tokens=100)
        ctx.set_compactor(lambda messages: "summary")
        for i in range(12):
            ctx.add({"role": "user", "content": f"message number {i} " + "x" * 40})
            ctx.ensure_within(ctx.max_context_tokens)
        events = _read_events(tmp_path)
        begins = [
            e for e in events if e["event"] == "compact" and e["phase"] == "begin"
        ]
        commits = [
            e for e in events if e["event"] == "compact" and e["phase"] == "commit"
        ]
        assert begins, "압축이 한 번도 발화하지 않았다 — 예산/트리거 확인"
        assert commits
        assert begins[0]["tokens_before"] > 0
        assert begins[0]["active_turns"] == 2
        assert "generation" in begins[0]
        assert commits[0]["duration_ms"] >= 0
        # begin 과 commit 의 세대가 같은 압축 패스를 가리킨다
        assert commits[0]["generation"] == begins[0]["generation"]


class TestLlmCallUsageEvent:
    """P6: llm_call usage 이벤트 — 실측 토큰 비용의 데이터 소스."""

    def test_llm_call_event_recorded_with_usage(self, tmp_path):
        from unittest.mock import MagicMock

        from agent_cli.context.manager import ContextManager
        from agent_cli.loop import run_loop
        from agent_cli.providers.base import LLMResponse, TokenUsage
        from agent_cli.providers.capabilities import ModelCapabilities

        turn_metrics.enable(tmp_path)
        caps = ModelCapabilities(
            context_window=32768,
            max_output_tokens=4096,
            supports_thinking=False,
            thinking_budget=0,
        )
        provider = MagicMock()
        provider.call.return_value = LLMResponse(
            content='[{"action": "complete", "result": "ok"}]',
            usage=TokenUsage(input_tokens=1234, output_tokens=56),
        )
        ctx = ContextManager(tmp_path / "sess", max_context_tokens=1_000_000)
        run_loop(
            query="hello",
            provider=provider,
            capabilities=caps,
            model="test-model",
            ctx=ctx,
            record_turns=False,
            origin_turn="t9",
        )
        events = _read_events(tmp_path)
        calls = [e for e in events if e["event"] == "llm_call"]
        assert len(calls) == 1
        assert calls[0]["turn_id"] == "t9"
        assert calls[0]["input_tokens"] == 1234
        assert calls[0]["output_tokens"] == 56
        assert "cache_read_tokens" not in calls[0]  # 0 → 생략
        assert "depth" not in calls[0]  # depth 0 → 생략

    def test_no_usage_no_event(self, tmp_path):
        from unittest.mock import MagicMock

        from agent_cli.context.manager import ContextManager
        from agent_cli.loop import run_loop
        from agent_cli.providers.base import LLMResponse
        from agent_cli.providers.capabilities import ModelCapabilities

        turn_metrics.enable(tmp_path)
        caps = ModelCapabilities(
            context_window=32768,
            max_output_tokens=4096,
            supports_thinking=False,
            thinking_budget=0,
        )
        provider = MagicMock()
        provider.call.return_value = LLMResponse(
            content='[{"action": "complete", "result": "ok"}]'
        )
        ctx = ContextManager(tmp_path / "sess", max_context_tokens=1_000_000)
        run_loop(
            query="hello",
            provider=provider,
            capabilities=caps,
            model="test-model",
            ctx=ctx,
            record_turns=False,
        )
        assert [e for e in _read_events(tmp_path) if e["event"] == "llm_call"] == []


class TestCtxSeqEvents:
    """N5: 컨텍스트 스냅샷/커밋 seq 이벤트 — 스냅샷 staleness 의 데이터 소스.

    staleness(스텝) = (커밋 seq − 1) − (스냅샷 seq): 이 턴의 프롬프트가
    찍힌 뒤 커밋 전에 컨텍스트에 들어간 변형(남의 블록·메시지) 수.
    """

    def test_ctx_seq_snapshot_append_commit(self, tmp_path):
        from agent_cli.context.manager import ContextManager

        turn_metrics.enable(tmp_path)
        ctx = ContextManager(tmp_path / "session", max_context_tokens=5000)
        ctx.get_messages()
        ctx.add({"role": "user", "content": "hi"})
        ctx.commit_atomic(
            [
                {"role": "assistant", "content": "a"},
                {"role": "user", "tool": "shell", "success": True, "content": "o"},
            ]
        )
        ctx.get_messages()
        evs = [e for e in _read_events(tmp_path) if e.get("event") == "ctx"]
        assert [(e["phase"], e["seq"]) for e in evs] == [
            ("snapshot", 0),
            ("append", 1),
            ("commit", 2),
            ("snapshot", 2),
        ]
        commit = next(e for e in evs if e["phase"] == "commit")
        assert commit["records"] == 2
        assert commit["thread"] == threading.current_thread().name

    def test_ctx_staleness_arithmetic_across_threads(self, tmp_path):
        from agent_cli.context.manager import ContextManager

        turn_metrics.enable(tmp_path)
        ctx = ContextManager(tmp_path / "session", max_context_tokens=5000)
        ctx.get_messages()  # 내 스냅샷: seq 0
        t = threading.Thread(
            target=lambda: ctx.add({"role": "user", "content": "other"}),
            name="other-turn",
        )
        t.start()
        t.join()  # 남의 변형: seq 1
        ctx.commit_atomic([{"role": "assistant", "content": "mine"}])  # 내 커밋: seq 2
        evs = [e for e in _read_events(tmp_path) if e.get("event") == "ctx"]
        me = threading.current_thread().name
        snap = [e for e in evs if e["phase"] == "snapshot" and e["thread"] == me][-1]
        commit = next(e for e in evs if e["phase"] == "commit" and e["thread"] == me)
        assert commit["seq"] - 1 - snap["seq"] == 1
        assert next(e for e in evs if e["phase"] == "append")["thread"] == "other-turn"

    def test_ctx_events_absent_when_disabled(self, tmp_path):
        from agent_cli.context.manager import ContextManager

        ctx = ContextManager(tmp_path / "session", max_context_tokens=5000)
        ctx.get_messages()
        ctx.commit_atomic([{"role": "assistant", "content": "a"}])
        assert _read_events(tmp_path) == []

    def test_ctx_empty_commit_emits_nothing(self, tmp_path):
        from agent_cli.context.manager import ContextManager

        turn_metrics.enable(tmp_path)
        ctx = ContextManager(tmp_path / "session", max_context_tokens=5000)
        ctx.commit_atomic([])
        assert [e for e in _read_events(tmp_path) if e.get("event") == "ctx"] == []
