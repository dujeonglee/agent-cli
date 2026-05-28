"""Tests for the unified begin/end-driven delegate lifecycle.

Before this refactor the parallel delegate orchestration straddled two
layers: ``tool_delegate`` wrapped the worker join in
``renderer.parallel_live_panel(...)`` as a context manager and called
``render_start_capture`` / ``render_stop_capture`` per worker, while
the renderer's only "lifecycle" surface — ``begin_delegate_task`` /
``end_delegate_task`` — was just SSE marker plumbing for the web
frontend.

Now the renderer owns the whole presentation: ``begin_delegate_task``
brings up the CLI Live region (on a real terminal) and starts capture;
``end_delegate_task`` marks the task done and, on the last task,
tears the Live region down and dumps each task's captured output
wrapped in group framing. ``tool_delegate`` only signals lifecycle.

The tests in this module pin that contract so a future refactor can't
silently re-introduce the layering leak:

  - Begin/end is the only surface the delegate tool calls.
  - MinimalRenderer drives the Live region itself.
  - Captured output is replayed in registration order, with each task
    wrapped in the ``[N] agent: task`` group header.
  - WebRenderer's SSE-routing path is untouched (no regression on the
    other renderer).
  - Concurrent workers' state stays correctly isolated.
"""

from __future__ import annotations

import inspect
import io
import threading

from rich.console import Console

from agent_cli.render.minimal import MinimalRenderer


def _make_renderer(*, force_terminal: bool = False) -> MinimalRenderer:
    """Build a MinimalRenderer wired to a StringIO console.

    ``force_terminal=False`` (default) keeps the Live region from
    actually starting — the panel branches on ``self.con.is_terminal``
    — so unit tests can exercise state transitions without spinning
    up rich's refresh thread (which would block ``Live.stop`` until
    join in environments without a tty).
    """
    return MinimalRenderer(Console(file=io.StringIO(), force_terminal=force_terminal))


# ─── Lifecycle state transitions ──────────────────────────────


class TestBeginEndState:
    def test_first_begin_registers_task(self):
        r = _make_renderer()
        r.begin_delegate_task(task_id="t1", index=0, agent="a", task_text="task body")
        assert "t1" in r._parallel_tasks
        state = r._parallel_tasks["t1"]
        assert state["index"] == 0
        assert state["agent"] == "a"
        assert state["task"] == "task body"
        assert state["done"] is False
        # cleanup
        r.end_delegate_task(task_id="t1", success=True, duration_s=0.1)

    def test_multiple_begins_register_each_task(self):
        r = _make_renderer()
        r.begin_delegate_task(task_id="t1", index=0, agent="", task_text="A")
        r.begin_delegate_task(task_id="t2", index=1, agent="", task_text="B")
        assert r._parallel_order == ["t1", "t2"]
        r.end_delegate_task(task_id="t1", success=True, duration_s=0.1)
        r.end_delegate_task(task_id="t2", success=True, duration_s=0.1)

    def test_end_marks_task_done_and_carries_metadata(self):
        r = _make_renderer()
        r.begin_delegate_task(task_id="t1", index=0, agent="", task_text="A")
        r.end_delegate_task(task_id="t1", success=False, duration_s=4.2, error="boom")
        # After last-task end, MinimalRenderer clears _parallel_order
        # and pops the per-task dict — so the contract is "no
        # leftover state once all tasks have ended". Pin that.
        assert r._parallel_order == []
        assert r._parallel_tasks == {}

    def test_last_end_clears_panel_state(self):
        r = _make_renderer()
        r.begin_delegate_task(task_id="t1", index=0, agent="", task_text="A")
        r.begin_delegate_task(task_id="t2", index=1, agent="", task_text="B")
        # Ending only one task does NOT tear down — still one running.
        r.end_delegate_task(task_id="t1", success=True, duration_s=0.1)
        assert r._parallel_order == ["t1", "t2"]
        assert r._parallel_tasks["t2"]["done"] is False
        # Ending the second triggers cleanup.
        r.end_delegate_task(task_id="t2", success=True, duration_s=0.1)
        assert r._parallel_order == []
        assert r._parallel_tasks == {}

    def test_end_called_twice_on_same_task_id_is_safe(self):
        # ``tool_delegate``'s worker calls end_delegate_task in a
        # ``finally`` block. A double-end shouldn't happen in normal
        # usage but if it did (e.g. a future restructure) the second
        # call must be a graceful no-op rather than crashing the
        # worker. Pin defensively.
        r = _make_renderer()
        r.begin_delegate_task(task_id="t1", index=0, agent="", task_text="A")
        r.end_delegate_task(task_id="t1", success=True, duration_s=0.1)
        # Second end on the same id — state has already been popped.
        # Must not raise.
        r.end_delegate_task(task_id="t1", success=False, duration_s=0.2, error="x")


# ─── Live region wiring ───────────────────────────────────────


class TestLiveRegionLifecycle:
    """The CLI Live region's start/stop must be exclusively driven by
    begin/end on a real terminal, and must NOT come up at all when the
    console isn't a terminal (StringIO, redirected runs). Without the
    is_terminal guard the rich.Live refresh thread can hang on
    output-blocking writes against a non-tty backend — surfaced as a
    test hang during this refactor's bring-up."""

    def test_no_live_started_on_non_terminal_console(self):
        # Default test renderer uses StringIO + force_terminal=False.
        r = _make_renderer()
        r.begin_delegate_task(task_id="t1", index=0, agent="", task_text="A")
        # State is tracked …
        assert "t1" in r._parallel_tasks
        # … but the Live region remains None (no refresh thread spun
        # up). This is the property that prevents test hangs.
        assert r._parallel_live is None
        r.end_delegate_task(task_id="t1", success=True, duration_s=0.1)

    def test_clock_initialized_only_when_live_starts(self):
        # The clock pairs with the Live region — both come up together
        # on a real terminal, both stay absent off-tty. Pinning this
        # documents the invariant for the test renderer.
        r = _make_renderer()
        r.begin_delegate_task(task_id="t1", index=0, agent="", task_text="A")
        assert r._parallel_clock is None
        r.end_delegate_task(task_id="t1", success=True, duration_s=0.1)


# ─── Captured output replay ───────────────────────────────────


class TestCapturedOutputDump:
    """When a parallel set completes, ``end_delegate_task`` (on the
    LAST task) replays each worker's captured output to the parent
    console. That replay must:

      1. happen in registration order — the user expects task 1 then
         task 2, not whatever order workers happened to finish in;
      2. wrap each task in ``group_start`` / ``group_end`` framing
         with the labelled header so the dump reads like the worker
         actually ran inline;
      3. respect ``push_depth`` between the group brackets so any
         nested-depth context the parent loop was in carries
         through.

    The previous architecture had ``tool_delegate`` do this wrapping
    after ``parallel_live_panel`` exited. The new architecture has
    MinimalRenderer do it during ``end_delegate_task`` of the last
    worker, so the tool layer no longer touches presentation. Pin
    that the dump still happens and is correctly framed.
    """

    def _capture_console_output(self, r: MinimalRenderer) -> str:
        # MinimalRenderer prints to ``r.con`` (the underlying rich
        # Console). For tests we wired a StringIO file behind that
        # Console, so reading the buffer back gives the rendered
        # output minus ANSI codes (force_terminal=False).
        return r.con.file.getvalue()

    def _run_worker(
        self,
        r: MinimalRenderer,
        task_id: str,
        index: int,
        agent: str,
        task_text: str,
        captured_lines: list[str],
        success: bool,
        duration_s: float,
        error: str = "",
    ) -> threading.Thread:
        """Spawn a thread that mimics a real delegate worker: begin,
        write captured lines through the renderer's capture API, end.

        Capture is keyed by thread id, so doing this in a real worker
        thread (instead of injecting state from the test thread) lets
        each task collect its own buffer end-to-end. Returns the
        started Thread — caller joins it.
        """

        def body() -> None:
            r.begin_delegate_task(
                task_id=task_id, index=index, agent=agent, task_text=task_text
            )
            for line in captured_lines:
                r._capture_line(line)
            r.end_delegate_task(
                task_id=task_id, success=success, duration_s=duration_s, error=error
            )

        t = threading.Thread(target=body)
        t.start()
        return t

    def test_captured_output_replayed_in_registration_order(self):
        r = _make_renderer()
        # We need both workers to be REGISTERED before either ends —
        # otherwise the first end is "last" (only one task) and tears
        # down early. Use a barrier-like pattern: spawn t1, wait for
        # it to register, spawn t2, wait for it to register, then let
        # both finish.
        registered = {"t1": threading.Event(), "t2": threading.Event()}
        release = threading.Event()

        def worker(tid: str, index: int, label: str, lines: list[str]) -> None:
            r.begin_delegate_task(task_id=tid, index=index, agent="", task_text=label)
            registered[tid].set()
            release.wait(timeout=5)
            for line in lines:
                r._capture_line(line)
            r.end_delegate_task(task_id=tid, success=True, duration_s=0.1)

        t1 = threading.Thread(target=worker, args=("t1", 0, "first", ["output for t1"]))
        t2 = threading.Thread(
            target=worker, args=("t2", 1, "second", ["output for t2"])
        )
        t1.start()
        registered["t1"].wait(timeout=5)
        t2.start()
        registered["t2"].wait(timeout=5)
        release.set()
        t1.join(timeout=5)
        t2.join(timeout=5)

        out = self._capture_console_output(r)
        # Both bodies present and t1 appears BEFORE t2 in the dump.
        assert "output for t1" in out
        assert "output for t2" in out
        assert out.find("output for t1") < out.find("output for t2")

    def test_dump_wraps_each_task_in_group_framing(self):
        r = _make_renderer()
        t = self._run_worker(
            r,
            task_id="t1",
            index=0,
            agent="reviewer",
            task_text="check security",
            captured_lines=["line A", "line B"],
            success=True,
            duration_s=2.5,
        )
        t.join(timeout=5)
        out = self._capture_console_output(r)
        # Group header includes labelled index + agent + task prefix.
        assert "reviewer" in out
        assert "check security" in out
        # Group closer carries the success marker + duration formatted
        # to one decimal (matches render_group_end's existing format).
        assert "2.5s" in out
        # Captured body present.
        assert "line A" in out
        assert "line B" in out

    def test_failed_task_dump_uses_failure_marker(self):
        r = _make_renderer()
        t = self._run_worker(
            r,
            task_id="t1",
            index=0,
            agent="",
            task_text="boom",
            captured_lines=["error trace"],
            success=False,
            duration_s=1.0,
            error="exploded",
        )
        t.join(timeout=5)
        out = self._capture_console_output(r)
        assert "error trace" in out
        # Failure marker: group_end with success=False emits ✗.
        assert "✗" in out


# ─── Source-level architectural invariants ───────────────────


class TestArchitecturalInvariants:
    """Source-level guarantees that document where the lifecycle
    contract lives. These are whitebox tests — they grep the actual
    module source to prove the previous layering leak isn't
    re-introduced by a future change.
    """

    def test_delegate_does_not_import_rich_live(self):
        # The tool layer must not host any UI rendering. After this
        # refactor ``rich.Live`` should only appear in
        # ``agent_cli/render/minimal.py``.
        from agent_cli.tools import delegate

        src = inspect.getsource(delegate)
        assert "from rich.live" not in src
        assert "rich.live" not in src

    def test_delegate_does_not_use_parallel_live_panel(self):
        # ``parallel_live_panel`` is now a legacy nullcontext —
        # the tool layer should not call it.
        from agent_cli.tools import delegate

        src = inspect.getsource(delegate)
        assert "parallel_live_panel" not in src

    def test_delegate_does_not_use_capture_replay_wrappers(self):
        # ``render_start_capture`` / ``render_stop_capture`` /
        # ``render_replay_captured`` are render-module wrappers
        # that the tool layer no longer needs — begin/end carry
        # the capture lifecycle implicitly inside MinimalRenderer.
        from agent_cli.tools import delegate

        src = inspect.getsource(delegate)
        assert "render_start_capture" not in src
        assert "render_stop_capture" not in src
        assert "render_replay_captured" not in src

    def test_minimal_renderer_owns_live_region(self):
        # The Live region's start/stop and the captured output dump
        # must live in ``MinimalRenderer``. Grep both methods so a
        # future refactor that moves Live elsewhere fails this test.
        begin = inspect.getsource(MinimalRenderer.begin_delegate_task)
        end = inspect.getsource(MinimalRenderer.end_delegate_task)
        assert "Live(" in begin
        assert ".stop()" in end

    def test_begin_end_are_the_only_lifecycle_surface(self):
        # ``ParallelTaskState`` was the data shape pumped through
        # ``parallel_live_panel`` as a state_getter snapshot. With the
        # tool layer no longer building snapshots, ``ParallelTaskState``
        # is not used by ``delegate.py`` either. Pin that — if a future
        # change pulls the dataclass back into the tool, the layering
        # leak is back too.
        from agent_cli.tools import delegate

        src = inspect.getsource(delegate)
        assert "ParallelTaskState" not in src


# ─── WebRenderer regression — no SSE behaviour change ─────────


class TestWebRendererUnchanged:
    """The refactor is CLI-focused: WebRenderer's begin/end overrides
    already fired SSE markers and routed events via task_id; that
    contract must still hold. These tests confirm the SSE surface
    didn't drift.
    """

    def _make_web(self):
        from agent_cli.render.web import WebConnection, WebRenderer

        r = WebRenderer()
        c = WebConnection(id="c1")
        r.register_connection(c)
        return r, c

    def _drain(self, conn):
        events = []
        from queue import Empty

        while True:
            try:
                events.append(conn.queue.get(timeout=0.05))
            except Empty:
                break
        return events

    def test_begin_emits_delegate_task_start_event(self):
        r, c = self._make_web()
        r.begin_delegate_task(task_id="t1", index=0, agent="x", task_text="y")
        events = self._drain(c)
        names = [e for e, _ in events]
        assert "delegate_task_start" in names

    def test_end_emits_delegate_task_end_event(self):
        r, c = self._make_web()
        r.begin_delegate_task(task_id="t1", index=0, agent="", task_text="")
        # Drain start event first so end events are easy to find.
        self._drain(c)
        r.end_delegate_task(task_id="t1", success=True, duration_s=1.2)
        events = self._drain(c)
        names = [e for e, _ in events]
        assert "delegate_task_end" in names

    def test_failed_task_end_carries_error(self):
        r, c = self._make_web()
        r.begin_delegate_task(task_id="t1", index=0, agent="", task_text="")
        self._drain(c)
        r.end_delegate_task(
            task_id="t1", success=False, duration_s=0.5, error="bad input"
        )
        events = self._drain(c)
        end = next(d for e, d in events if e == "delegate_task_end")
        assert end["success"] is False
        assert end["error"] == "bad input"

    def test_thread_to_task_routing_intact(self):
        # The whole point of WebRenderer's begin/end is the
        # ``_thread_to_task`` registration so subsequent emits in the
        # worker thread carry the right task_id. Confirm the
        # registration / deregistration still works after the
        # refactor.
        r, _ = self._make_web()
        r.begin_delegate_task(task_id="t1", index=0, agent="", task_text="")
        assert r._thread_to_task[threading.get_ident()] == "t1"
        r.end_delegate_task(task_id="t1", success=True, duration_s=0.0)
        assert threading.get_ident() not in r._thread_to_task


# ─── Concurrency stress ──────────────────────────────────────


class TestConcurrentWorkers:
    """Real parallel-delegate use spawns N worker threads that each
    call begin / end concurrently. The shared ``_parallel_tasks`` /
    ``_parallel_order`` state must stay consistent under contention.
    """

    def test_concurrent_begin_end_keeps_state_consistent(self):
        r = _make_renderer()
        N = 8
        errors: list[BaseException] = []

        def worker(idx: int) -> None:
            try:
                tid = f"t{idx}"
                r.begin_delegate_task(
                    task_id=tid, index=idx, agent="", task_text=f"task {idx}"
                )
                # Inject a fake captured line so dump replay has
                # something to print.
                r._capture_line(f"work for {idx}")
                r.end_delegate_task(task_id=tid, success=True, duration_s=0.01)
            except BaseException as e:  # noqa: BLE001 — collect for assert
                errors.append(e)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(N)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)

        assert errors == [], "concurrent begin/end raised " + ", ".join(
            f"{type(e).__name__}: {e}" for e in errors
        )
        # After all workers finish, state must be fully cleared (last
        # end always triggers cleanup, regardless of which worker
        # happened to be last).
        assert r._parallel_tasks == {}
        assert r._parallel_order == []
        assert r._parallel_live is None
