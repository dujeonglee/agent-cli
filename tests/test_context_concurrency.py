"""ContextManager 동시 턴 안전 (v7.29.0) — 다중 사용자 병렬 턴(A1)의 전제.

세 가지 계약을 고정한다:
  1. :meth:`commit_atomic` — 레코드 묶음 사이에 다른 턴이 끼지 못한다.
  2. :meth:`get_messages` — 반환된 리스트는 이후 변형에 영향받지 않는 스냅샷.
  3. 압축 — LLM 요약 구간에 **락을 쥐지 않고**(이벤트 루프 정지 방지),
     그 동안 도착한 append 를 꼬리로 흡수하며, 캐시가 통째로 갈리면 폐기한다.

근거: docs/research/11-upstream-merge-plan.md §2 M3, 포크 ``piWorker.mjs``
의 스냅샷 읽기/원자 커밋 불변식(188-190, 380-381).
"""

import json
import threading
import time

import pytest

from agent_cli.context.manager import ContextManager


@pytest.fixture
def ctx(tmp_path):
    return ContextManager(tmp_path / "sess", max_context_tokens=10_000)


def _records(ctx):
    with open(ctx.history_path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


class TestCommitAtomic:
    def test_group_is_not_split_by_concurrent_adds(self, tmp_path):
        """assistant + 그 관찰들 사이에 다른 턴의 레코드가 끼지 않는다.

        포크가 실측으로 확인한 짝 정합 불변식(``piWorker.mjs:380-381``).
        본류는 자연어 재렌더링이라 400 이 아니라 **조용한 문맥 오염**으로 난다.
        """
        ctx = ContextManager(tmp_path / "sess", max_context_tokens=10_000_000)
        group_size = 4
        writers = 6
        rounds = 5
        barrier = threading.Barrier(writers)

        def w(i):
            barrier.wait()
            for r in range(rounds):
                ctx.commit_atomic(
                    [
                        {"role": "assistant", "content": f"w{i}r{r}#{k}"}
                        for k in range(group_size)
                    ]
                )

        ts = [threading.Thread(target=w, args=(i,)) for i in range(writers)]
        [t.start() for t in ts]
        [t.join() for t in ts]

        recs = _records(ctx)
        assert len(recs) == writers * rounds * group_size
        # 묶음 단위로 잘라 보면, 각 묶음은 한 writer/round 의 0..N-1 이어야 한다.
        for off in range(0, len(recs), group_size):
            chunk = [r["content"] for r in recs[off : off + group_size]]
            prefix = chunk[0].rsplit("#", 1)[0]
            assert chunk == [f"{prefix}#{k}" for k in range(group_size)], chunk

    def test_cache_order_matches_history(self, ctx):
        ctx.commit_atomic(
            [
                {"role": "assistant", "content": "a"},
                {"role": "user", "tool": "shell", "success": True, "content": "b"},
            ]
        )
        assert [m["content"] for m in ctx.get_raw_messages()] == ["a", "b"]
        assert [r["content"] for r in _records(ctx)] == ["a", "b"]

    def test_empty_is_noop(self, ctx):
        assert ctx.commit_atomic([]) == []
        assert ctx.get_raw_messages() == []

    def test_equivalent_to_sequential_add(self, tmp_path):
        """직렬 모드에서 ``add`` 반복과 저장 결과가 같다(동작 무변)."""
        msgs = [
            {"role": "user", "content": "q"},
            {"role": "assistant", "content": "a"},
        ]
        c1 = ContextManager(tmp_path / "s1", max_context_tokens=10_000)
        for m in msgs:
            c1.add(dict(m))
        c2 = ContextManager(tmp_path / "s2", max_context_tokens=10_000)
        c2.commit_atomic([dict(m) for m in msgs])

        def strip(rs):
            return [{k: v for k, v in r.items() if k != "ts"} for r in rs]

        assert strip(_records(c1)) == strip(_records(c2))


class TestSnapshot:
    def test_returned_list_is_immune_to_later_mutation(self, ctx):
        """``get_messages`` 결과는 스냅샷 — 이후 append/압축이 흔들지 못한다.

        별도 ``snapshot()`` 메서드를 두지 않은 근거이기도 하다(순수 별칭이 됨).
        """
        ctx.add({"role": "user", "content": "first"})
        snap = ctx.get_messages()
        n = len(snap)

        ctx.add({"role": "user", "content": "second"})
        assert len(snap) == n  # 쥐고 있던 리스트는 그대로
        assert len(ctx.get_messages()) == n + 1

    def test_reads_are_consistent_under_concurrent_writes(self, ctx):
        """동시 append 중에도 찢어진 뷰(길이 불일치·None 렌더)가 안 나온다."""
        stop = threading.Event()
        errors: list[BaseException] = []

        def writer():
            i = 0
            while not stop.is_set():
                ctx.add({"role": "user", "content": f"m{i}"})
                i += 1

        def reader():
            try:
                for _ in range(300):
                    msgs = ctx.get_messages()
                    assert all(isinstance(m, dict) for m in msgs)
                    assert all("content" in m for m in msgs)
            except BaseException as e:
                errors.append(e)

        wt = threading.Thread(target=writer, daemon=True)
        wt.start()
        rts = [threading.Thread(target=reader) for _ in range(4)]
        [t.start() for t in rts]
        [t.join() for t in rts]
        stop.set()
        wt.join(timeout=5)
        assert not errors


class TestCompactionBarrier:
    """압축이 락을 어떻게 쓰는가 — 설계의 핵심."""

    def _fill(self, ctx, n=12):
        for i in range(n):
            ctx.add({"role": "user", "content": f"filler-{i} " + "x" * 400})

    def test_lock_is_released_during_summarisation(self, ctx):
        """요약(수 초)이 도는 동안 다른 스레드가 ctx 를 계속 쓸 수 있다.

        이걸 어기면 ``/api/debug/prompt``(async 라우트가 이벤트 루프에서
        ``get_messages`` 동기 호출)가 압축 내내 이벤트 루프를 멈춰 모든 뷰어의
        SSE 가 끊긴다 — 배리어를 낙관적으로 바꾼 이유.
        """
        entered = threading.Event()
        release = threading.Event()
        progressed = threading.Event()

        def slow_summariser(_messages):
            entered.set()
            release.wait(timeout=5)
            return "SUMMARY"

        ctx.set_compactor(slow_summariser)
        self._fill(ctx)

        t = threading.Thread(target=lambda: ctx.ensure_within(50), daemon=True)
        t.start()
        assert entered.wait(timeout=5), "요약 콜백에 진입하지 못함"

        # 요약이 진행 중인 바로 지금 — 락이 풀려 있어야 한다.
        def other_turn():
            ctx.add({"role": "user", "content": "다른 턴의 입력"})
            ctx.get_messages()
            progressed.set()

        ot = threading.Thread(target=other_turn, daemon=True)
        ot.start()
        assert progressed.wait(timeout=3), "요약 중 락이 잡혀 다른 턴이 막혔다"

        release.set()
        t.join(timeout=5)
        ot.join(timeout=5)

    def test_records_arriving_during_summarisation_survive(self, ctx):
        """꼬리 흡수 — 요약 중 도착한 레코드가 압축 커밋에 버려지지 않는다."""
        entered = threading.Event()
        release = threading.Event()

        def slow_summariser(_messages):
            entered.set()
            release.wait(timeout=5)
            return "SUMMARY"

        ctx.set_compactor(slow_summariser)
        self._fill(ctx)

        t = threading.Thread(target=lambda: ctx.ensure_within(50), daemon=True)
        t.start()
        assert entered.wait(timeout=5)

        ctx.add({"role": "user", "content": "MIDFLIGHT"})
        release.set()
        t.join(timeout=5)

        contents = [m.get("content", "") for m in ctx.get_raw_messages()]
        assert any("MIDFLIGHT" in c for c in contents), (
            "요약 중 도착한 레코드가 소실됐다 (꼬리 흡수 실패)"
        )
        assert ctx.compaction_count == 1

    def test_bulk_mutation_during_summarisation_discards_the_pass(self, ctx):
        """캐시가 통째로 갈리면(FIFO 등) 그 압축 결과는 폐기된다.

        흡수로 복구할 수 없는 변형이라, 조용히 틀린 커밋보다 낭비를 택한다.
        """
        entered = threading.Event()
        release = threading.Event()

        def slow_summariser(_messages):
            entered.set()
            release.wait(timeout=5)
            return "SUMMARY"

        ctx.set_compactor(slow_summariser)
        self._fill(ctx)

        t = threading.Thread(target=lambda: ctx.ensure_within(50), daemon=True)
        t.start()
        assert entered.wait(timeout=5)

        # 요약 중 벌크 변형 발생 → 세대 증가.
        with ctx._lock:
            ctx._evict_fifo(1)

        release.set()
        t.join(timeout=5)

        assert ctx.summary == "", "stale 압축이 커밋됐다"
        assert ctx.compaction_count == 0

    def test_only_one_compaction_runs_at_a_time(self, ctx):
        """동시 진입 게이트 — 같은 요약을 N번 결제하지 않는다."""
        calls = []
        release = threading.Event()

        def counting_summariser(_messages):
            calls.append(1)
            release.wait(timeout=5)
            return "SUMMARY"

        ctx.set_compactor(counting_summariser)
        self._fill(ctx)

        ts = [
            threading.Thread(target=lambda: ctx.ensure_within(50), daemon=True)
            for _ in range(4)
        ]
        [t.start() for t in ts]
        time.sleep(0.3)  # 전원이 ensure_within 에 도달할 시간
        release.set()
        [t.join(timeout=5) for t in ts]

        assert len(calls) == 1, f"요약 콜이 {len(calls)}회 — 게이트가 새고 있다"

    def test_serial_compaction_result_is_unchanged(self, ctx):
        """직렬 모드: 경쟁자가 없으면 종전과 동일한 결과."""
        ctx.set_compactor(lambda _m: "SUMMARY TEXT")
        self._fill(ctx)
        ctx.ensure_within(50)
        assert ctx.summary == "SUMMARY TEXT"
        assert ctx.compaction_count == 1
        assert ctx.get_estimated_tokens() <= ctx.get_estimated_tokens()


class TestConcurrentCompactionCommitStarvation:
    """N1 실측이 찾은 커밋 기아의 수리 계약 (v7.30.0).

    압축이 비행 중일 때 동시 호출자의 belt-and-braces FIFO 가 세대를 올리면
    낙관 커밋이 매번 stale 로 죽는다(실측 28/28). 수리: 비행 중에는
    ``target/compaction_ratio``(진짜 한계)까지의 초과를 관용하고 커밋에
    양보한다. 진짜 한계 초과는 종전대로 FIFO(오버플로 방지 우선).
    """

    def _fill(self, ctx, n=12):
        for i in range(n):
            ctx.add({"role": "user", "content": f"filler-{i} " + "x" * 400})

    def test_concurrent_caller_yields_within_margin(self, ctx):
        """마진 내 초과 + 압축 비행 중 → FIFO 안 함 → 커밋이 산다."""
        entered = threading.Event()
        release = threading.Event()

        def slow_summariser(_messages):
            entered.set()
            release.wait(timeout=5)
            return "SUMMARY"

        ctx.set_compactor(slow_summariser)
        self._fill(ctx)
        target = 50  # 진짜 한계 = 50/0.8 = 62.5 — 캐시는 그보다 크게 유지

        compactor = threading.Thread(
            target=lambda: ctx.ensure_within(target), daemon=True
        )
        compactor.start()
        assert entered.wait(timeout=5)

        # 동시 호출자: 마진 판정을 위해 target 을 "캐시가 마진 안에 들도록"
        # 잡는다 — hard = target/0.8 ≥ 현재 캐시.
        margin_target = int(ctx.get_estimated_tokens() * 0.9)
        gen_before = ctx._bulk_gen
        ctx.ensure_within(margin_target)
        assert ctx._bulk_gen == gen_before, (
            "압축 비행 중 마진 내 초과가 FIFO 를 발동시켰다 — 커밋 기아 재발"
        )

        release.set()
        compactor.join(timeout=5)
        assert ctx.compaction_count == 1, "낙관 커밋이 stale 로 죽었다"
        assert ctx.summary == "SUMMARY"

    def test_concurrent_caller_still_evicts_beyond_hard_limit(self, ctx):
        """진짜 한계(target/ratio) 초과는 압축 중이어도 FIFO — 오버플로 방지."""
        entered = threading.Event()
        release = threading.Event()

        def slow_summariser(_messages):
            entered.set()
            release.wait(timeout=5)
            return "SUMMARY"

        ctx.set_compactor(slow_summariser)
        self._fill(ctx, n=20)

        compactor = threading.Thread(target=lambda: ctx.ensure_within(60), daemon=True)
        compactor.start()
        assert entered.wait(timeout=5)

        # 아주 작은 target — hard 한계(=target/0.8)보다 캐시가 훨씬 크다.
        tokens_before = ctx.get_estimated_tokens()
        ctx.ensure_within(10)
        assert ctx.get_estimated_tokens() < tokens_before, (
            "진짜 한계 초과인데도 FIFO 가 양보했다 — 오버플로 방어 구멍"
        )
        release.set()
        compactor.join(timeout=5)

    def test_serial_path_unchanged(self, ctx):
        """직렬: 같은 스레드가 압축을 마친 뒤라 관용 분기를 타지 않는다."""
        ctx.set_compactor(lambda _m: "S")
        self._fill(ctx)
        ctx.ensure_within(50)
        assert ctx.compaction_count == 1
        assert ctx.get_estimated_tokens() <= 50 or ctx.summary == "S"
