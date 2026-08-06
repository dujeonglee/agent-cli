"""병렬 턴 E2E — TurnRegistry × run_loop × 공유 ContextManager (A1, v7.29.0).

단위 테스트(``test_turn_registry.py``)가 디스패처만 본다면, 여기서는 **실제
``run_loop`` 여러 개가 하나의 ctx 위에서 동시에 돌 때** 히스토리·귀속·격리가
버티는지를 본다 — 앞서 넣은 세 계약(fsio append 직렬화, ctx 원자 커밋/낙관적
압축, A6 reply_to)이 실제 병렬 부하에서 함께 성립하는지 확인하는 자리다.
"""

import json
import threading
from unittest.mock import MagicMock

import pytest

from agent_cli.context.manager import ContextManager
from agent_cli.loop import run_loop
from agent_cli.loop.turns import TurnRegistry
from agent_cli.providers.base import LLMResponse
from agent_cli.providers.capabilities import ModelCapabilities


@pytest.fixture
def caps():
    return ModelCapabilities(
        context_window=32768,
        max_output_tokens=4096,
        supports_thinking=False,
        thinking_budget=0,
    )


def _complete(result: str) -> str:
    return json.dumps({"action": "complete", "result": result})


class _SlowProvider:
    """호출마다 잠깐 머무는 provider — 턴이 실제로 겹치게 만든다.

    ``MagicMock(side_effect=[...])`` 를 안 쓰는 이유: 병렬 호출에서는 소비
    순서가 비결정적이라 시퀀스 대응이 성립하지 않는다. 여기서는 쿼리 텍스트로
    응답을 만들어 어느 순서로 불려도 옳은 답이 나가게 한다.
    """

    def __init__(self, gate: threading.Event | None = None):
        self.gate = gate
        self.calls = 0
        self.peak = 0
        self._live = 0
        self._lock = threading.Lock()

    def call(self, messages=None, **kwargs):
        msgs = messages if messages is not None else kwargs.get("messages") or []
        with self._lock:
            self.calls += 1
            self._live += 1
            self.peak = max(self.peak, self._live)
        try:
            if self.gate is not None:
                self.gate.wait(timeout=5)
            # 마지막 user 메시지에서 turn 식별자를 뽑아 그대로 답한다.
            text = ""
            for m in reversed(msgs):
                if m.get("role") == "user":
                    text = str(m.get("content", ""))
                    break
            marker = text.strip().split()[-1] if text.strip() else "?"
            return LLMResponse(content=_complete(f"done:{marker}"))
        finally:
            with self._lock:
                self._live -= 1


def _runner_factory(ctx, provider, caps, seen, lock):
    def run(turn):
        result = run_loop(
            query=turn.text,
            query_author=turn.author,
            provider=provider,
            capabilities=caps,
            model="test-model",
            ctx=ctx,
            stop_event=turn.stop_event,
            origin_turn=turn.id,
            record_turns=False,
        )
        with lock:
            seen.append((turn.id, result.output))

    return run


def _history(ctx):
    with open(ctx.history_path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


class TestParallelTurnsShareOneContext:
    def test_turns_actually_overlap(self, tmp_path, caps):
        gate = threading.Event()
        provider = _SlowProvider(gate)
        ctx = ContextManager(tmp_path / "sess", max_context_tokens=1_000_000)
        seen, lock = [], threading.Lock()
        reg = TurnRegistry(
            _runner_factory(ctx, provider, caps, seen, lock), max_concurrent=3
        )

        for i in range(3):
            reg.submit(f"question {i}", author=f"user{i}")
        # 셋이 동시에 provider 안에 있어야 한다.
        deadline = threading.Event()
        for _ in range(500):
            if provider.peak >= 3:
                break
            deadline.wait(0.01)
        gate.set()
        assert reg.wait_idle(timeout=15)
        assert provider.peak == 3, f"동시 진입 최대 {provider.peak} — 병렬이 아니다"
        assert len(seen) == 3

    def test_history_is_intact_and_complete(self, tmp_path, caps):
        """동시 append 에도 줄이 깨지거나 유실되지 않는다 (fsio 직렬화 + ctx 락)."""
        provider = _SlowProvider()
        ctx = ContextManager(tmp_path / "sess", max_context_tokens=1_000_000)
        seen, lock = [], threading.Lock()
        reg = TurnRegistry(
            _runner_factory(ctx, provider, caps, seen, lock), max_concurrent=4
        )

        n = 8
        for i in range(n):
            reg.submit(f"question {i}", author=f"user{i}")
        assert reg.wait_idle(timeout=30)

        recs = _history(ctx)  # 깨진 줄이 있으면 여기서 JSONDecodeError
        queries = [r for r in recs if r.get("kind") == "query"]
        assert len(queries) == n, f"질의 레코드 {len(queries)}/{n} — 유실"
        # 각 질의는 유일한 id 를 받았다.
        ids = [r["id"] for r in queries]
        assert len(set(ids)) == n

    def test_every_answer_is_attributed_to_a_query(self, tmp_path, caps):
        """A6 귀속이 병렬에서도 성립 — 모든 응답 레코드가 실재 질의를 가리킨다."""
        provider = _SlowProvider()
        ctx = ContextManager(tmp_path / "sess", max_context_tokens=1_000_000)
        seen, lock = [], threading.Lock()
        reg = TurnRegistry(
            _runner_factory(ctx, provider, caps, seen, lock), max_concurrent=4
        )
        for i in range(6):
            reg.submit(f"question {i}", author=f"user{i}")
        assert reg.wait_idle(timeout=30)

        recs = _history(ctx)
        query_ids = {r["id"] for r in recs if r.get("kind") == "query"}
        replies = [r for r in recs if "reply_to" in r]
        assert replies, "reply_to 를 가진 레코드가 하나도 없다"
        dangling = [r["reply_to"] for r in replies if r["reply_to"] not in query_ids]
        assert not dangling, f"실재하지 않는 질의를 가리킴: {dangling}"

    def test_every_turn_completes_without_starving(self, tmp_path, caps):
        """제출한 턴이 하나도 빠짐없이 완료된다 (슬롯 회수·펌프 정합)."""
        provider = _SlowProvider()
        ctx = ContextManager(tmp_path / "sess", max_context_tokens=1_000_000)
        seen, lock = [], threading.Lock()
        reg = TurnRegistry(
            _runner_factory(ctx, provider, caps, seen, lock), max_concurrent=4
        )
        for i in range(6):
            reg.submit(f"question {i}", author=f"user{i}")
        assert reg.wait_idle(timeout=30)

        assert len(seen) == 6
        assert {tid for tid, _out in seen} == {f"t{i}" for i in range(1, 7)}
        assert all(out.startswith("done:") for _t, out in seen)

    def test_known_limitation_shared_transcript_tail(self, tmp_path, caps):
        """**알려진 한계 고정**: 병렬 턴의 프롬프트 꼬리는 자기 질문이 아닐 수 있다.

        트랜스크립트는 의도적으로 공유된다(포크 ``piWorker.mjs:188-190`` 의
        단일 ``convo`` 와 같은 설계) — 그래서 턴 B 가 LLM 을 부를 때 마지막
        user 메시지가 사용자 A 의 것일 수 있다. 즉 "어느 질문에 답할지"는
        현재 **모델의 판단**에 맡겨져 있고, 하네스는 ``[닉네임]:`` 라벨과
        A6 ``reply_to``(의도된 짝의 기록)까지만 제공한다.

        이 테스트는 그 성질을 **버그가 아니라 계약으로 못박아** 둔다. 프롬프트
        수준에서 "이번 턴이 답할 메시지"를 명시하는 것은 KV 캐시·직렬 동작에
        영향을 주므로 별도 작업이다(어느 마일스톤에도 아직 없음).
        """
        gate = threading.Event()
        provider = _SlowProvider(gate)
        ctx = ContextManager(tmp_path / "sess", max_context_tokens=1_000_000)
        seen, lock = [], threading.Lock()
        reg = TurnRegistry(
            _runner_factory(ctx, provider, caps, seen, lock), max_concurrent=2
        )
        reg.submit("question 0", author="user0")
        reg.submit("question 1", author="user1")
        for _ in range(500):
            if provider.peak >= 2:
                break
            threading.Event().wait(0.01)
        gate.set()
        assert reg.wait_idle(timeout=15)

        # 두 질의 모두 공유 트랜스크립트에 들어갔다 — 그것이 공유의 정의다.
        contents = [
            r.get("content", "") for r in _history(ctx) if r.get("kind") == "query"
        ]
        assert any("question 0" in c for c in contents)
        assert any("question 1" in c for c in contents)
        # 그리고 각 질의는 발화자 라벨을 달고 있다 — 모델이 구분할 유일한 단서.
        assert all(c.startswith("[user") for c in contents)

    def test_interrupt_stops_only_its_turn(self, tmp_path, caps):
        gate = threading.Event()
        provider = _SlowProvider(gate)
        ctx = ContextManager(tmp_path / "sess", max_context_tokens=1_000_000)
        seen, lock = [], threading.Lock()
        reg = TurnRegistry(
            _runner_factory(ctx, provider, caps, seen, lock), max_concurrent=2
        )
        reg.submit("question a", author="a")
        reg.submit("question b", author="b")
        for _ in range(500):
            if reg.active_count() == 2:
                break
            threading.Event().wait(0.01)

        victim = min(reg.active_ids())
        assert reg.interrupt(victim) is True
        gate.set()
        assert reg.wait_idle(timeout=15)
        # 둘 다 끝났고(행 없음), 최소 하나는 정상 완료했다.
        assert len(seen) == 2
        assert any(out.startswith("done:") for _t, out in seen)


class TestSerialModeUnaffected:
    def test_origin_turn_defaults_to_empty(self, tmp_path, caps):
        """직렬 경로: origin_turn 미지정 → history 에 turn 흔적이 남지 않는다."""
        provider = MagicMock()
        provider.call.side_effect = [LLMResponse(content=_complete("ok"))]
        ctx = ContextManager(tmp_path / "sess", max_context_tokens=1_000_000)
        run_loop(
            query="hello",
            provider=provider,
            capabilities=caps,
            model="test-model",
            ctx=ctx,
            record_turns=False,
        )
        recs = _history(ctx)
        assert recs, "히스토리가 비었다"
        assert all("origin_turn" not in r for r in recs)


class TestTurnMetricsE2E:
    """M2 계측 × 실 run_loop — TTFT 사슬(dispatch→first_token→complete)이
    병렬 턴마다 정확히 한 번씩, 올바른 turn_id 로 남는지."""

    def test_first_token_once_per_turn_with_chain_order(self, tmp_path, caps):
        from agent_cli import turn_metrics

        class _StreamingProvider(_SlowProvider):
            """첫 청크를 스트리밍하는 변형 — first_token 발화 경로를 지난다."""

            def call(self, messages=None, on_chunk=None, **kwargs):
                if on_chunk is not None:
                    on_chunk("chunk")
                return super().call(messages=messages, **kwargs)

        metrics_dir = tmp_path / "metrics"
        metrics_dir.mkdir()
        turn_metrics.enable(metrics_dir)
        try:
            provider = _StreamingProvider()
            ctx = ContextManager(tmp_path / "sess", max_context_tokens=1_000_000)
            seen, lock = [], threading.Lock()
            reg = TurnRegistry(
                _runner_factory(ctx, provider, caps, seen, lock), max_concurrent=3
            )
            for i in range(3):
                reg.submit(f"question {i}", author=f"user{i}", queue_id=f"q{i}")
            assert reg.wait_idle(timeout=15)
            reg.shutdown()

            with open(metrics_dir / "turns.jsonl", encoding="utf-8") as f:
                events = [json.loads(line) for line in f if line.strip()]

            first_tokens = [e for e in events if e.get("phase") == "first_token"]
            assert len(first_tokens) == 3, (
                f"first_token {len(first_tokens)}건 — 턴당 1건이어야 한다"
            )
            assert {e["turn_id"] for e in first_tokens} == {"t1", "t2", "t3"}

            # 턴별 사슬 순서: dispatch < first_token < complete (mono_ms)
            for tid in ("t1", "t2", "t3"):
                chain = {
                    e["phase"]: e["mono_ms"] for e in events if e.get("turn_id") == tid
                }
                assert set(chain) >= {"dispatch", "first_token", "complete"}
                assert chain["dispatch"] <= chain["first_token"] <= chain["complete"]

            # 귀속 사슬(N3): query_added 가 턴 스레드 이름으로 남아
            # dispatch(turn_id) ↔ msg_id(u{n}) 조인이 성립한다.
            q_added = [e for e in events if e.get("phase") == "query_added"]
            assert len(q_added) == 3
            threads = {e["thread"] for e in q_added}
            assert threads == {"agent-turn-t1", "agent-turn-t2", "agent-turn-t3"}
        finally:
            turn_metrics.disable()


class _ToolThenComplete:
    """첫 콜은 도구 op, 그다음부터 complete.

    턴 식별을 **스레드 이름**으로 하는 이유: 트랜스크립트는 의도적으로 공유되므로
    (``test_known_limitation_shared_transcript_tail``) 마지막 user 메시지로는 어느
    턴의 콜인지 알 수 없다. ``TurnRegistry`` 가 워커를 ``agent-turn-t{n}`` 으로
    명명하므로 그것이 병렬에서 신뢰할 수 있는 유일한 턴 키다.
    """

    def __init__(self, path_for):
        self.path_for = path_for
        self._started: set[str] = set()
        self._lock = threading.Lock()

    def call(self, messages=None, **kwargs):
        name = threading.current_thread().name
        with self._lock:
            first = name not in self._started
            self._started.add(name)
        if first:
            # json_fc 의 op 은 **평평하다** — ``_complete`` 와 같은 모양.
            return LLMResponse(
                content=json.dumps(
                    {
                        "action": "write_file",
                        "path": self.path_for(name),
                        "content": f"written by {name}\n",
                    }
                )
            )
        return LLMResponse(content=_complete(f"done:{name}"))


class TestEffectLockUnderRealParallelTurns:
    """M4 × A1 — "병렬 추론 + 직렬 부수효과"를 **실제 run_loop 경로로** 확인.

    단위 테스트(``test_effect_lock.py``)는 ``hold`` 를 직접 부르고, E2E 의 다른
    테스트들은 도구를 전혀 쓰지 않는다. 그 사이에 구멍이 있다: 락이 옳고 병렬이
    옳아도 **``run_loop`` → ``ToolBridge`` 배선이 끊기면** 논문의 중심 주장이
    조용히 거짓이 된다. 여기서 그 배선을 잰다.

    측정은 도구 실행 자체의 동시 진입 최대치(peak)로 한다 — 락이 감싸는 구간이
    바로 그것이라 "정말 직렬화됐는가"의 직접 증거다.
    """

    def _run(self, tmp_path, caps, monkeypatch, *, scope, same_file, n=3):
        from agent_cli.loop import tool_bridge
        from agent_cli.tools import effect_lock

        effect_lock.reset()
        effect_lock.set_scope(scope)

        workspace = tmp_path / "ws"
        workspace.mkdir()
        monkeypatch.chdir(workspace)

        peak = {"v": 0}
        live = {"v": 0}
        m = threading.Lock()
        real = tool_bridge._execute_tool

        def spy(name, tool_input, ctx=None):
            with m:
                live["v"] += 1
                peak["v"] = max(peak["v"], live["v"])
            try:
                threading.Event().wait(0.05)  # 겹칠 기회를 넉넉히 준다
                return real(name, tool_input, ctx=ctx)
            finally:
                with m:
                    live["v"] -= 1

        monkeypatch.setattr(tool_bridge, "_execute_tool", spy)

        def path_for(thread_name):
            return "shared.txt" if same_file else f"{thread_name}.txt"

        provider = _ToolThenComplete(path_for)
        ctx = ContextManager(tmp_path / "sess", max_context_tokens=1_000_000)
        seen, lock = [], threading.Lock()
        reg = TurnRegistry(
            _runner_factory(ctx, provider, caps, seen, lock), max_concurrent=n
        )
        for i in range(n):
            reg.submit(f"task {i}", author=f"user{i}", conn_id=f"c{i}")
        assert reg.wait_idle(timeout=30), "턴이 시간 내에 끝나지 않았다 (교착?)"
        effect_lock.reset()
        return peak["v"], len(seen), workspace

    def test_same_file_writes_are_serialised(self, tmp_path, caps, monkeypatch):
        """같은 경로 → 동시 진입 0. 이것이 P3 의 '유실 0' 주장의 기계적 근거다."""
        peak, done, _ws = self._run(
            tmp_path, caps, monkeypatch, scope="conflict", same_file=True
        )
        assert done == 3, "일부 턴이 완료되지 않았다"
        assert peak == 1, f"같은 파일에 동시 진입 {peak}건 — 직렬화 실패"

    def test_different_files_still_run_in_parallel(self, tmp_path, caps, monkeypatch):
        """서로 다른 경로 → 겹친다. 여기가 이득의 원천이고, 이게 1 이면 계층
        락이 단순 mutex 로 퇴화한 것이다(포크 v1 이 1.07× 로 붕괴한 지점)."""
        peak, done, ws = self._run(
            tmp_path, caps, monkeypatch, scope="conflict", same_file=False
        )
        assert done == 3
        assert peak > 1, f"서로 다른 파일인데 직렬화됐다 (peak={peak})"
        assert len(list(ws.glob("*.txt"))) == 3, "쓰기가 실제로 일어나지 않았다"

    def test_workspace_scope_serialises_even_disjoint_files(
        self, tmp_path, caps, monkeypatch
    ):
        """ablation 대조군(포크 v1 재현) — 스코프 전환이 E2E 에서 실제로
        동작을 바꾸는지. 안 바뀌면 실험의 두 팔이 같은 것을 재고 있다."""
        peak, done, _ws = self._run(
            tmp_path, caps, monkeypatch, scope="workspace", same_file=False
        )
        assert done == 3
        assert peak == 1, f"workspace 스코프인데 겹쳤다 (peak={peak})"

    def test_off_scope_lets_same_file_writes_overlap(self, tmp_path, caps, monkeypatch):
        """음성 대조 — 락을 끄면 같은 파일도 겹친다. 이게 겹치지 않으면 위
        테스트들이 락이 아니라 다른 무언가(예: 우연한 직렬 실행)를 재고 있다는
        뜻이라, 통과해도 아무것도 증명하지 못한다."""
        peak, done, _ws = self._run(
            tmp_path, caps, monkeypatch, scope="off", same_file=True
        )
        assert done == 3
        assert peak > 1, f"락 없이도 직렬이었다 — 위 테스트들이 무의미 (peak={peak})"
