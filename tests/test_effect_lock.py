"""부수효과 계층 락 (M4/A3, v7.29.0).

계약 근거는 포크(Coagora) ``backend/src/agent/sandboxLock.ts``:
  - 호환성 행렬 (16-22)
  - 엄격 FIFO·추월 금지 = 기아 방지 (24-25, 76-87)
  - 실패해도 다음 대기자 진행 (114-126)
  - 모든 작업 종료 시 키 정리 (86)

**ablation 재현**(P3): 락 없이 같은 파일을 동시 수정하면 유실이 나고, 락을 켜면
0 이 된다 — 포크의 74%→0% 에 대응하는 본류판 측정이다.
"""

import threading
import time

import pytest

from agent_cli.tools import effect_lock
from agent_cli.tools.effect import EffectIntent, EffectKind


@pytest.fixture(autouse=True)
def _clean():
    effect_lock.reset()
    yield
    effect_lock.reset()


W = EffectIntent(EffectKind.FILE_WRITE, "a.py")
W2 = EffectIntent(EffectKind.FILE_WRITE, "b.py")
R = EffectIntent(EffectKind.FILE_READ, "a.py")
SH = EffectIntent(EffectKind.SHELL)
UNK = EffectIntent(EffectKind.UNKNOWN_WORKSPACE_EFFECT)
NON_WS = EffectIntent(EffectKind.NON_WORKSPACE_OR_COMPOSITE)


class _Tracker:
    """동시 진입 최대치를 재는 헬퍼."""

    def __init__(self):
        self.peak = 0
        self._live = 0
        self._lock = threading.Lock()
        self.order: list[str] = []

    def enter(self, label):
        with self._lock:
            self._live += 1
            self.peak = max(self.peak, self._live)
            self.order.append(label)

    def exit(self):
        with self._lock:
            self._live -= 1


def _spawn(intents_with_labels, tracker, hold_s=0.05, scope="conflict"):
    effect_lock.set_scope(scope)
    barrier = threading.Barrier(len(intents_with_labels))

    def work(intent, label):
        barrier.wait(timeout=5)
        with effect_lock.hold(intent):
            tracker.enter(label)
            time.sleep(hold_s)
            tracker.exit()

    ts = [
        threading.Thread(target=work, args=(i, lbl)) for i, lbl in intents_with_labels
    ]
    [t.start() for t in ts]
    [t.join(timeout=15) for t in ts]
    return tracker


def _assert_blocks(first, second, *, expect_blocked=True, note=""):
    """``first`` 를 먼저 잡은 상태에서 ``second`` 가 막히는지 **결정적으로** 검사.

    ``_spawn`` 처럼 동시 출발시키면 큐 진입 순서가 비결정적이라("엄격 FIFO 라
    나중 것이 먼저 큐에 들어가면 그쪽이 먼저 도는 게 정상") 배타성 검사가
    flaky 해진다. 여기서는 순서를 고정해 행렬만 본다.
    """
    effect_lock.set_scope("conflict")
    holding = threading.Event()
    release = threading.Event()
    entered = threading.Event()

    def hold_first():
        with effect_lock.hold(first):
            holding.set()
            release.wait(timeout=5)

    def try_second():
        with effect_lock.hold(second):
            entered.set()

    t1 = threading.Thread(target=hold_first)
    t1.start()
    assert holding.wait(timeout=5)
    t2 = threading.Thread(target=try_second)
    t2.start()

    got_in = entered.wait(timeout=0.3)
    if expect_blocked:
        assert not got_in, f"배타여야 하는데 동시 진입함 — {note}"
    else:
        assert got_in, f"병렬 가능해야 하는데 막힘 — {note}"

    release.set()
    t1.join(timeout=5)
    t2.join(timeout=5)
    assert entered.wait(timeout=5), "선행 해제 후에도 진입하지 못함"


class TestCompatibilityMatrix:
    def test_different_paths_run_in_parallel(self):
        """이번 이득의 원천 — 서로 다른 파일은 줄 서지 않는다."""
        t = _spawn([(W, "a"), (W2, "b")], _Tracker())
        assert t.peak == 2

    def test_same_path_is_serialised(self):
        t = _spawn([(W, "a1"), (W, "a2")], _Tracker())
        assert t.peak == 1

    def test_read_and_write_on_same_path_serialise(self):
        t = _spawn([(W, "w"), (R, "r")], _Tracker())
        assert t.peak == 1

    def test_reads_on_different_paths_are_parallel(self):
        r2 = EffectIntent(EffectKind.FILE_READ, "b.py")
        t = _spawn([(R, "r1"), (r2, "r2")], _Tracker())
        assert t.peak == 2

    def test_shell_is_exclusive_against_everything(self):
        _assert_blocks(SH, W2, note="SHELL 은 어떤 파일을 만질지 알 수 없다")

    def test_delete_is_exclusive(self):
        """삭제는 디렉토리를 지울 수 있어 경로 키가 달라도 배타여야 한다
        (``rm -r src/`` vs ``write src/x.py`` 의 ENOENT 레이스)."""
        d = EffectIntent(EffectKind.FILE_DELETE, "src")
        _assert_blocks(d, W2, note="FILE_DELETE 는 새 레이스 클래스를 만든다")

    def test_empty_path_falls_back_to_exclusive(self):
        empty = EffectIntent(EffectKind.FILE_WRITE, "")
        _assert_blocks(empty, W2, note="빈 경로는 락 키로 신뢰할 수 없다")

    def test_a_path_effect_does_not_block_a_disjoint_one(self):
        """위 배타 검사의 대조 — 경로가 다르면 막지 않는다(행렬의 다른 쪽)."""
        _assert_blocks(W, W2, expect_blocked=False)

    def test_relative_and_absolute_same_file_share_a_key(self):
        """같은 파일을 상대/절대로 부르면 키가 갈려 보호가 새면 안 된다."""
        import os

        rel = EffectIntent(EffectKind.FILE_WRITE, "x.py")
        absolute = EffectIntent(
            EffectKind.FILE_WRITE, os.path.join(os.getcwd(), "x.py")
        )
        t = _spawn([(rel, "rel"), (absolute, "abs")], _Tracker())
        assert t.peak == 1

    def test_symlink_aliases_share_a_key(self, tmp_path):
        target = tmp_path / "target.txt"
        target.write_text("x", encoding="utf-8")
        alias = tmp_path / "alias.txt"
        alias.symlink_to(target)
        a = EffectIntent(EffectKind.FILE_WRITE, str(target))
        b = EffectIntent(EffectKind.FILE_WRITE, str(alias))
        t = _spawn([(a, "target"), (b, "symlink")], _Tracker())
        assert t.peak == 1

    def test_hard_link_aliases_share_a_key(self, tmp_path):
        import os

        target = tmp_path / "target.txt"
        target.write_text("x", encoding="utf-8")
        alias = tmp_path / "hardlink.txt"
        os.link(target, alias)
        a = EffectIntent(EffectKind.FILE_WRITE, str(target))
        b = EffectIntent(EffectKind.FILE_WRITE, str(alias))
        t = _spawn([(a, "target"), (b, "hardlink")], _Tracker())
        assert t.peak == 1

    def test_new_paths_through_symlinked_parent_share_a_key(self, tmp_path):
        target_dir = tmp_path / "real"
        target_dir.mkdir()
        alias_dir = tmp_path / "alias"
        alias_dir.symlink_to(target_dir, target_is_directory=True)
        a = EffectIntent(EffectKind.FILE_WRITE, str(target_dir / "new.txt"))
        b = EffectIntent(EffectKind.FILE_WRITE, str(alias_dir / "new.txt"))
        t = _spawn([(a, "target"), (b, "symlink-parent")], _Tracker())
        assert t.peak == 1

    def test_cyclic_symlink_fails_closed(self, tmp_path):
        first = tmp_path / "first"
        second = tmp_path / "second"
        first.symlink_to(second)
        second.symlink_to(first)
        cyclic = EffectIntent(EffectKind.FILE_WRITE, str(first))
        _assert_blocks(cyclic, W2, note="canonicalization failure must be exclusive")

    def test_rename_after_key_construction_is_an_explicit_toctou_boundary(
        self, tmp_path
    ):
        """Canonicalization is not a filesystem transaction.

        This adversarial check deliberately moves the resource after the first
        key has been constructed.  The new name has a different key; A3 in the
        paper therefore requires resource identity to remain stable between
        key construction and execution.
        """
        old = tmp_path / "old.txt"
        new = tmp_path / "new.txt"
        old.write_text("x", encoding="utf-8")
        old_key = effect_lock.normalize_lock_path(str(old))
        old.rename(new)
        new_key = effect_lock.normalize_lock_path(str(new))
        assert old_key != new_key


class TestFairness:
    def test_strict_fifo_no_overtaking(self):
        """머리가 막히면 뒤도 대기 — SHELL 이 파일 작업에 굶지 않는다.

        순서: writer(a.py) 진행 중 → SHELL 대기(배타라 막힘) → writer(b.py).
        추월을 허용하면 b.py 가 먼저 들어가 SHELL 이 계속 밀린다.
        """
        effect_lock.set_scope("conflict")
        order: list[str] = []
        order_lock = threading.Lock()
        first_in = threading.Event()
        release_first = threading.Event()

        def first():
            with effect_lock.hold(W):
                first_in.set()
                release_first.wait(timeout=5)
                with order_lock:
                    order.append("first")

        def sh():
            with effect_lock.hold(SH):
                with order_lock:
                    order.append("shell")
                time.sleep(0.02)

        def third():
            with effect_lock.hold(W2), order_lock:
                order.append("other-file")

        t1 = threading.Thread(target=first)
        t1.start()
        assert first_in.wait(timeout=5)

        t2 = threading.Thread(target=sh)
        t2.start()
        time.sleep(0.05)  # shell 이 큐 머리에 자리잡을 시간
        t3 = threading.Thread(target=third)
        t3.start()
        time.sleep(0.05)

        release_first.set()
        [t.join(timeout=10) for t in (t1, t2, t3)]

        assert order[0] == "first"
        assert order[1] == "shell", f"SHELL 이 추월당했다: {order}"
        assert order[2] == "other-file"


class TestScopes:
    def test_off_does_not_lock(self):
        t = _spawn([(W, "a1"), (W, "a2")], _Tracker(), scope="off")
        assert t.peak == 2  # 보호 없음 — ablation 대조군

    def test_workspace_serialises_everything(self):
        """포크 v1 의 단순 mutex 재현 — 다른 파일도 줄을 선다(E2-B 붕괴 조건)."""
        t = _spawn([(W, "a"), (W2, "b")], _Tracker(), scope="workspace")
        assert t.peak == 1

    def test_conflict_beats_workspace_on_disjoint_files(self):
        """행렬을 좁힌 이유를 수치로: 같은 작업이 workspace 에선 직렬, conflict
        에선 병렬 — 포크가 E2-B 실측으로 1.07× 붕괴를 확인하고 내린 결정."""
        ws = _spawn([(W, "a"), (W2, "b")], _Tracker(), scope="workspace")
        effect_lock.reset()
        cf = _spawn([(W, "a"), (W2, "b")], _Tracker(), scope="conflict")
        assert ws.peak == 1 and cf.peak == 2

    def test_unknown_workspace_effect_fails_closed(self):
        """미분류 plugin/tool 효과는 누락 실수여도 배타로 떨어진다."""
        t = _spawn([(UNK, "u1"), (UNK, "u2")], _Tracker(), scope="conflict")
        assert t.peak == 1

    def test_explicit_non_workspace_effect_does_not_lock(self):
        t = _spawn([(NON_WS, "u"), (SH, "sh")], _Tracker(), scope="conflict")
        assert t.peak == 2

    def test_only_explicit_non_workspace_kind_yields_false(self):
        effect_lock.set_scope("conflict")
        with effect_lock.hold(NON_WS) as locked:
            assert locked is False
        with effect_lock.hold(UNK) as locked:
            assert locked is True
        with effect_lock.hold(W) as locked:
            assert locked is True

    def test_bad_scope_is_rejected(self):
        with pytest.raises(ValueError):
            effect_lock.set_scope("bogus")


class TestReleaseAndCleanup:
    def test_exception_still_releases(self):
        """실패해도 다음 대기자는 정상 진행한다 (직렬성 보존)."""
        effect_lock.set_scope("conflict")

        raised = []

        def boom():
            try:
                with effect_lock.hold(W):
                    raise RuntimeError("boom")
            except RuntimeError:
                raised.append(True)  # 예외는 호출자에게 그대로 전파돼야 한다

        t = threading.Thread(target=boom)
        t.start()
        t.join(timeout=5)
        assert raised, "hold 가 예외를 삼켰다"

        done = threading.Event()

        def after():
            with effect_lock.hold(W):
                done.set()

        t2 = threading.Thread(target=after)
        t2.start()
        t2.join(timeout=5)
        assert done.is_set(), "선행 실패가 락을 영구 점유했다"

    def test_keys_are_cleaned_up(self):
        effect_lock.set_scope("conflict")
        with effect_lock.hold(W):
            assert effect_lock.active_keys() == 1
        assert effect_lock.active_keys() == 0, "락 상태가 남아 누수"

    def test_running_count_observable(self):
        effect_lock.set_scope("conflict")
        with effect_lock.hold(W):
            assert effect_lock.running_count() == 1


class TestAblationLostUpdate:
    """P3 ablation 의 본류판 — 락이 실제로 유실을 막는가.

    두 턴이 같은 파일을 read-modify-write 하면, 락이 없으면 한쪽 갱신이
    덮여 사라진다(lost update). 포크의 74%→0% 에 대응하는 측정이다.
    """

    def _run(self, tmp_path, scope, workers=8, rounds=6):
        effect_lock.set_scope(scope)
        target = tmp_path / "counter.txt"
        target.write_text("0", encoding="utf-8")
        intent = EffectIntent(EffectKind.FILE_WRITE, str(target))
        barrier = threading.Barrier(workers)

        def bump():
            barrier.wait(timeout=5)
            for _ in range(rounds):
                with effect_lock.hold(intent):
                    # read-modify-write — 락이 없으면 여기서 겹친다.
                    raw = target.read_text(encoding="utf-8")
                    # 대조군에서는 쓰기 도중의 **찢어진 파일**을 읽을 수도 있다.
                    # 그것 자체가 손상의 증거이므로 예외로 스레드를 죽이지 않고
                    # 0 으로 세어 계속 간다(락이 켜지면 절대 발생하지 않는다).
                    n = int(raw) if raw.strip().isdigit() else 0
                    time.sleep(0.001)  # 레이스 창을 벌린다
                    target.write_text(str(n + 1), encoding="utf-8")

        ts = [threading.Thread(target=bump) for _ in range(workers)]
        [t.start() for t in ts]
        [t.join(timeout=30) for t in ts]
        return int(target.read_text(encoding="utf-8")), workers * rounds

    def test_conflict_scope_loses_nothing(self, tmp_path):
        got, expected = self._run(tmp_path, "conflict")
        assert got == expected, f"락이 켜졌는데 {expected - got}건 유실"

    def test_off_scope_loses_updates(self, tmp_path):
        """대조군 — 보호가 없으면 실제로 유실이 난다(가드가 하중을 받는 증거)."""
        got, expected = self._run(tmp_path, "off")
        assert got < expected, (
            "락 없이도 유실이 안 났다 — 레이스 창이 좁아 ablation 이 무의미하다"
        )


class TestNoDeadlockWithNestedTools:
    """복합 도구가 락을 잡으면 **교착**이다 — 구조적으로 불가능함을 고정.

    ``agent``/``run_skill`` 은 중첩 루프를 띄우고 그 안의 잎 도구가 각자 락을
    잡는다. 부모가 배타 락을 쥔 채 자식이 요구하면(자식은 다른 스레드라 재진입도
    안 통한다) 영원히 멈춘다. 그래서 복합 도구는 명시적인
    NON_WORKSPACE_OR_COMPOSITE = 부모 정렬 대상 아님이다.
    """

    def test_composite_tools_declare_no_orderable_effect(self):
        from agent_cli.tools import TOOLS

        for name in ("agent", "run_skill", "ask", "complete", "message"):
            intent = TOOLS[name].effect_intent({})
            assert intent.kind is EffectKind.NON_WORKSPACE_OR_COMPOSITE, name

    def test_nested_leaf_call_inside_a_held_lock_does_not_deadlock(self):
        """부모 스레드가 락을 쥔 동안 자식 스레드의 잎 호출이 진행되는가.

        복합 도구가 UNKNOWN 이라 락을 안 잡으므로, 실제 배선에서 이 상황
        자체가 생기지 않는다. 여기서는 그 전제가 깨졌을 때(복합 도구가 배타
        락을 잡게 되면) 무슨 일이 나는지를 드러내 두어, 미래에 누군가
        ``agent`` 의 intent 를 바꾸면 이 테스트가 먼저 비명을 지르게 한다.
        """
        from agent_cli.tools import TOOLS

        effect_lock.set_scope("conflict")
        parent_intent = TOOLS["agent"].effect_intent({"mode": "run", "task": "x"})
        child_done = threading.Event()

        with effect_lock.hold(parent_intent) as parent_locked:
            assert parent_locked is False, (
                "복합 도구가 락을 잡았다 — 자식 잎 호출과 교착한다"
            )

            def child():
                with effect_lock.hold(EffectIntent(EffectKind.FILE_WRITE, "child.py")):
                    child_done.set()

            t = threading.Thread(target=child)
            t.start()
            t.join(timeout=3)

        assert child_done.is_set(), "중첩 잎 호출이 교착했다"


class TestCooperativeProcessBoundary:
    def test_detached_work_outlives_the_shell_gate(self):
        """The gate covers a foreground shell call, not work it detaches.

        This pins assumption A4: a background process/thread can continue after
        the declaring shell critical section releases, so the paper must never
        describe the in-process gate as process-external containment.
        """
        effect_lock.set_scope("conflict")
        released = threading.Event()
        detached_done = threading.Event()

        def detached():
            released.wait(timeout=5)
            detached_done.set()

        with effect_lock.hold(SH):
            t = threading.Thread(target=detached)
            t.start()
            assert not detached_done.wait(timeout=0.05)
        released.set()
        t.join(timeout=5)
        assert detached_done.is_set()


class TestLoopIntegration:
    """루프 배선 — 락이 실제 도구 실행 경로에서 걸리는가.

    행렬이 아무리 옳아도 ``_invoke_regular`` 에서 안 불리면 무의미하다.
    ``ToolBridge`` 는 단독 생성 가능하도록 설계돼 있어(cfg/state/ctx/provider
    명시 주입) AgentLoop 없이 그 seam 만 검사할 수 있다.
    """

    def _bridge(self):
        from agent_cli.loop.state import LoopConfig, LoopState
        from agent_cli.loop.tool_bridge import ToolBridge

        return ToolBridge(LoopConfig(), LoopState(query="q"), None, None)

    def test_write_file_acquires_a_scoped_lock(self, tmp_path, monkeypatch):
        seen = []
        real_hold = effect_lock.hold

        def spy(intent, **kw):
            seen.append(intent)
            return real_hold(intent, **kw)

        monkeypatch.setattr(effect_lock, "hold", spy)
        effect_lock.set_scope("conflict")

        target = tmp_path / "out.txt"
        res = self._bridge()._invoke_regular(
            "write_file", {"path": str(target), "content": "hi"}
        )
        assert res.success, res.error
        assert seen, "효과 락을 거치지 않았다 — 배선 누락"
        assert seen[0].kind is EffectKind.FILE_WRITE
        assert seen[0].path == str(target)

    def test_composite_tool_bypasses_the_locked_path(self, monkeypatch):
        """``agent`` 는 ``_invoke_agent`` 로 갈라져 락이 걸린 경로를 안 탄다.

        이것이 중첩 교착을 막는 **구조적** 보장이다 — 분기가 사라지면 복합
        도구가 잎 경로로 흘러 자식과 같은 락을 두고 싸운다.
        """
        from agent_cli.tools.result import ToolResult

        bridge = self._bridge()
        routed = []
        monkeypatch.setattr(
            bridge,
            "_invoke_agent",
            lambda ti, idict: routed.append("agent") or ToolResult(True, output="ok"),
        )
        monkeypatch.setattr(
            bridge,
            "_invoke_regular",
            lambda name, ti: routed.append(f"regular:{name}") or ToolResult(True, ""),
        )

        bridge._dispatch_tool_with_hooks("agent", {"mode": "run", "task": "x"})
        assert routed == ["agent"], f"복합 도구가 잎 경로로 샜다: {routed}"

        routed.clear()
        bridge._dispatch_tool_with_hooks("shell", {"command": "true"})
        assert routed == ["regular:shell"]


class TestEditBatchPath:
    """M4: 같은-파일 연속 ``edit_file`` 은 ``_invoke_regular`` 를 **거치지 않고**
    순수 ``apply_edits_batch`` 를 직접 부른다(``dispatch._dispatch_edit_batch``).
    그래서 락을 그 자리에서 따로 잡아야 하며, 빠뜨리면 **배치 편집만** 보호
    밖으로 샌다 — 단건 편집 테스트로는 절대 드러나지 않는 구멍이다."""

    def _dispatcher(self):
        from agent_cli.loop.dispatch import TurnDispatcher
        from agent_cli.loop.state import LoopConfig, LoopState
        from agent_cli.loop.tool_bridge import ToolBridge

        cfg, state = LoopConfig(), LoopState(query="q")
        return TurnDispatcher(
            cfg, state, None, ToolBridge(cfg, state, None, None), None
        )

    def _ops(self, path, pairs):
        from agent_cli.wire_formats.base import Op

        return [
            Op(
                action="edit_file",
                action_input={
                    "path": path,
                    "op": "replace",
                    "ref": ref,
                    "content": content,
                },
            )
            for ref, content in pairs
        ]

    def test_batch_acquires_a_write_lock_on_the_target(self, tmp_path, monkeypatch):
        seen = []
        real_hold = effect_lock.hold

        def spy(intent, **kw):
            seen.append(intent)
            return real_hold(intent, **kw)

        monkeypatch.setattr(effect_lock, "hold", spy)
        effect_lock.set_scope("conflict")

        target = tmp_path / "batch.txt"
        target.write_text("a\nb\nc\n", encoding="utf-8")
        results: list[dict] = []
        self._dispatcher()._dispatch_edit_batch(
            "",
            0,
            self._ops(str(target), [("1", "A"), ("2", "B")]),
            {},
            accumulate=results,
        )
        assert seen, "배치 편집이 효과 락을 거치지 않았다 — 배선 누락"
        assert seen[0].kind is EffectKind.FILE_WRITE
        assert seen[0].path == str(target)

    def test_batch_lock_key_matches_a_single_edit_on_the_same_file(self, tmp_path):
        """배치와 단건이 같은 키로 모여야 서로를 막는다 — 키가 갈리면
        '단건 편집 + 배치 편집'이 나란히 진입해 보호가 통째로 샌다."""
        target = tmp_path / "same.txt"
        from agent_cli.tools.edit_file import EditFileTool

        single = EditFileTool().effect_intent({"path": str(target)})
        batch = EffectIntent(EffectKind.FILE_WRITE, str(target))
        assert effect_lock.normalize_lock_path(
            single.path
        ) == effect_lock.normalize_lock_path(batch.path)

    def test_concurrent_batches_on_one_file_are_serialised(self, tmp_path):
        """실제 유실 재현: 락 없이 두 배치가 같은 파일을 동시에 고치면
        나중 쓰기가 앞의 것을 덮는다."""
        effect_lock.set_scope("conflict")
        target = tmp_path / "race.txt"
        target.write_text("\n".join(f"line{i}" for i in range(1, 5)) + "\n", "utf-8")

        tracker = _Tracker()
        intent = EffectIntent(EffectKind.FILE_WRITE, str(target))

        def worker(label):
            with effect_lock.hold(intent):
                tracker.enter(label)
                time.sleep(0.05)
                tracker.exit()

        threads = [threading.Thread(target=worker, args=(f"w{i}",)) for i in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)
        assert tracker.peak == 1, f"같은 파일 배치가 동시에 진입했다: {tracker.peak}"
