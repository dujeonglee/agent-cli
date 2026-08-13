"""P1 enforced turn-local context and validated file publication."""

from __future__ import annotations

import threading
import time

from agent_cli.context.manager import ContextManager
from agent_cli.loop.llm import _fit_turn_local_view
from agent_cli.loop.state import LoopConfig, LoopState
from agent_cli.loop.tool_bridge import ToolBridge
from agent_cli.tools import RunContext, _execute_tool
from agent_cli.tools.effect import EffectIntent, EffectKind
from agent_cli.tools.result import ToolResult
from agent_cli.tools.turn_isolation import TurnIsolation, TurnIsolationPolicy


def _text(messages: list[dict]) -> str:
    return "\n".join(str(m.get("content", "")) for m in messages)


def test_ephemeral_context_fit_preserves_system_and_newest_without_mutating():
    messages = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "old " * 100},
        {"role": "assistant", "content": "middle " * 100},
        {"role": "user", "content": "NEWEST"},
    ]
    original = [dict(message) for message in messages]
    fitted = _fit_turn_local_view(messages, 10)
    assert messages == original
    assert fitted[0]["content"] == "system"
    assert fitted[-1]["content"] == "NEWEST"
    assert "old" not in _text(fitted) and "middle" not in _text(fitted)


def test_turn_local_context_hides_only_other_inflight_records(tmp_path):
    ctx = ContextManager(tmp_path / "session")
    both_added = threading.Barrier(2)
    release = threading.Event()
    seen: dict[str, str] = {}

    def worker(turn_id: str, own: str) -> None:
        with ctx.turn_scope(turn_id):
            ctx.add({"role": "user", "content": own})
            both_added.wait()
            seen[turn_id] = _text(
                ctx.get_messages(origin_turn=turn_id, filter_inflight=True)
            )
            release.wait(2)

    a = threading.Thread(target=worker, args=("t1", "ONLY-ALPHA"))
    b = threading.Thread(target=worker, args=("t2", "ONLY-BETA"))
    a.start()
    b.start()
    while len(seen) < 2:
        time.sleep(0.005)
    release.set()
    a.join()
    b.join()

    assert "ONLY-ALPHA" in seen["t1"] and "ONLY-BETA" not in seen["t1"]
    assert "ONLY-BETA" in seen["t2"] and "ONLY-ALPHA" not in seen["t2"]
    # Durable shared history is unchanged; once both scopes finish, both are
    # committed activity visible to the next turn.
    completed = _text(ctx.get_messages(origin_turn="t3", filter_inflight=True))
    assert "ONLY-ALPHA" in completed and "ONLY-BETA" in completed


def test_history_records_keep_public_turn_attribution_not_internal_key(tmp_path):
    ctx = ContextManager(tmp_path / "session")
    with ctx.turn_scope("t7"):
        ctx.add({"role": "user", "content": "hello"})
    raw = ctx.history_path.read_text(encoding="utf-8")
    assert '"origin_turn": "t7"' in raw
    assert "_origin_turn" not in raw


def test_filtered_view_omits_unattributed_compaction_aggregates(tmp_path):
    ctx = ContextManager(tmp_path / "session")
    ctx._summary = "summary may contain ACTIVE-OTHER"
    ctx._file_list = ["active-other.txt"]
    entered = threading.Event()
    release = threading.Event()

    def other() -> None:
        with ctx.turn_scope("t2"):
            entered.set()
            release.wait(2)

    thread = threading.Thread(target=other)
    thread.start()
    assert entered.wait(1)
    with ctx.turn_scope("t1"):
        view = _text(ctx.get_messages(origin_turn="t1", filter_inflight=True))
    release.set()
    thread.join()
    assert "ACTIVE-OTHER" not in view
    assert "active-other.txt" not in view


def test_write_is_invisible_until_exact_oracle_passes(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_CLI_WORKSPACE_CONFINE", "1")
    target = tmp_path / "answer.txt"
    target.write_text("old", encoding="utf-8")
    isolation = TurnIsolation(
        TurnIsolationPolicy(
            "t1",
            [target],
            expected_contents={target: "correct"},
            workspace_root=tmp_path,
        )
    )
    with isolation:
        ctx = RunContext(turn_isolation=isolation)
        result = _execute_tool(
            "write_file", {"path": str(target), "content": "correct"}, ctx=ctx
        )
        assert result.success
        assert target.read_text(encoding="utf-8") == "old"
        staged_read = _execute_tool("read_file", {"path": str(target)}, ctx=ctx)
        assert staged_read.success and "correct" in staged_read.output
        done = isolation.finish(ToolResult(True, output="done"))
        assert done.success
        assert target.read_text(encoding="utf-8") == "correct"
    phases = [e["phase"] for e in isolation.events]
    assert phases == [
        "capability_granted",
        "validation_passed",
        "write_set_published",
    ]


def test_failed_oracle_does_not_publish(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_CLI_WORKSPACE_CONFINE", "1")
    target = tmp_path / "answer.txt"
    target.write_text("old", encoding="utf-8")
    isolation = TurnIsolation(
        TurnIsolationPolicy(
            "t1",
            [target],
            expected_contents={target: "wanted"},
            workspace_root=tmp_path,
        )
    )
    with isolation:
        result = _execute_tool(
            "write_file",
            {"path": str(target), "content": "wrong"},
            ctx=RunContext(turn_isolation=isolation),
        )
        assert result.success
        done = isolation.finish(ToolResult(True, output="done"))
        assert not done.success
        assert target.read_text(encoding="utf-8") == "old"
    assert any(e["phase"] == "validation_failed" for e in isolation.events)


def test_exact_text_oracle_normalizes_one_final_newline_only(tmp_path):
    target = tmp_path / "answer.txt"
    isolation = TurnIsolation(
        TurnIsolationPolicy(
            "t1", [target], expected_contents={target: "line"}, workspace_root=tmp_path
        )
    )
    with isolation:
        _execute_tool(
            "write_file",
            {"path": str(target), "content": "line\n"},
            ctx=RunContext(turn_isolation=isolation),
        )
        assert isolation.finish(ToolResult(True, output="done")).success
    assert target.read_text(encoding="utf-8") == "line\n"


def test_no_oracle_means_no_automatic_publication(tmp_path):
    target = tmp_path / "answer.txt"
    isolation = TurnIsolation(
        TurnIsolationPolicy("t1", [target], workspace_root=tmp_path)
    )
    with isolation:
        _execute_tool(
            "write_file",
            {"path": str(target), "content": "unvalidated"},
            ctx=RunContext(turn_isolation=isolation),
        )
        done = isolation.finish(ToolResult(True, output="done"))
        assert not done.success
        assert not target.exists()


def test_missing_parent_directory_is_not_implicitly_created(tmp_path):
    target = tmp_path / "new-dir" / "answer.txt"
    try:
        TurnIsolation(
            TurnIsolationPolicy(
                "t1",
                [target],
                expected_contents={target: "answer"},
                workspace_root=tmp_path,
            )
        )
    except ValueError as exc:
        assert "parent does not exist" in str(exc)
    else:
        raise AssertionError("missing parent directory capability was accepted")
    assert not target.parent.exists()


def test_out_of_scope_write_and_shell_fail_closed_at_bridge(tmp_path):
    allowed = tmp_path / "mine.txt"
    other = tmp_path / "theirs.txt"
    isolation = TurnIsolation(
        TurnIsolationPolicy(
            "t1",
            [allowed],
            expected_contents={allowed: "mine"},
            workspace_root=tmp_path,
        )
    )
    bridge = ToolBridge(
        LoopConfig(tools_list=["write_file", "shell"], turn_isolation=isolation),
        LoopState(),
        None,
        None,
    )
    with isolation:
        denied = bridge._dispatch_tool_with_hooks(
            "write_file", {"path": str(other), "content": "intrude"}
        )
        shell = bridge._dispatch_tool_with_hooks("shell", {"command": "touch nope"})
        assert not denied.success and "outside approved write set" in denied.error
        assert not shell.success and "unscoped workspace effect" in shell.error
        assert not other.exists()
    blocked = [e for e in isolation.events if e["phase"] == "effect_blocked"]
    assert len(blocked) == 2


def test_dispatch_primitive_cannot_bypass_shell_guard(tmp_path):
    target = tmp_path / "mine.txt"
    isolation = TurnIsolation(
        TurnIsolationPolicy(
            "t1", [target], expected_contents={target: "mine"}, workspace_root=tmp_path
        )
    )
    with isolation:
        result = _execute_tool(
            "shell",
            {"command": f"touch {tmp_path / 'escaped.txt'}"},
            ctx=RunContext(turn_isolation=isolation),
        )
        assert not result.success
        assert not (tmp_path / "escaped.txt").exists()


def test_symlink_alias_canonicalizes_to_approved_target(tmp_path):
    real = tmp_path / "real.txt"
    real.write_text("old", encoding="utf-8")
    alias = tmp_path / "alias.txt"
    alias.symlink_to(real)
    isolation = TurnIsolation(
        TurnIsolationPolicy(
            "t1", [alias], expected_contents={real: "new"}, workspace_root=tmp_path
        )
    )
    with isolation:
        assert (
            isolation.authorize_tool(
                "write_file",
                {"path": str(real)},
                EffectIntent(EffectKind.FILE_WRITE, str(real)),
            )
            is None
        )


def test_external_version_change_blocks_commit(tmp_path):
    target = tmp_path / "answer.txt"
    target.write_text("base", encoding="utf-8")
    isolation = TurnIsolation(
        TurnIsolationPolicy(
            "t1", [target], expected_contents={target: "mine"}, workspace_root=tmp_path
        )
    )
    with isolation:
        _execute_tool(
            "write_file",
            {"path": str(target), "content": "mine"},
            ctx=RunContext(turn_isolation=isolation),
        )
        target.write_text("external", encoding="utf-8")
        done = isolation.finish(ToolResult(True, output="done"))
        assert not done.success and "commit conflict" in done.error
        assert target.read_text(encoding="utf-8") == "external"


def test_overlapping_hardlink_reservations_never_enter_together(tmp_path):
    first = tmp_path / "first.txt"
    alias = tmp_path / "alias.txt"
    first.write_text("x", encoding="utf-8")
    alias.hardlink_to(first)
    iso_a = TurnIsolation(
        TurnIsolationPolicy(
            "t1", [first], validator=lambda _: True, workspace_root=tmp_path
        )
    )
    iso_b = TurnIsolation(
        TurnIsolationPolicy(
            "t2", [alias], validator=lambda _: True, workspace_root=tmp_path
        )
    )
    entered_a = threading.Event()
    release_a = threading.Event()
    entered_b = threading.Event()

    def a() -> None:
        with iso_a:
            entered_a.set()
            release_a.wait(2)

    def b() -> None:
        entered_a.wait(2)
        with iso_b:
            entered_b.set()

    ta = threading.Thread(target=a)
    tb = threading.Thread(target=b)
    ta.start()
    tb.start()
    assert entered_a.wait(1)
    assert not entered_b.wait(0.05)
    release_a.set()
    assert entered_b.wait(1)
    ta.join()
    tb.join()


def test_queued_conflicting_turn_uses_predecessors_committed_version(tmp_path):
    target = tmp_path / "shared.txt"
    target.write_text("base", encoding="utf-8")
    first = TurnIsolation(
        TurnIsolationPolicy(
            "t1", [target], expected_contents={target: "first"}, workspace_root=tmp_path
        )
    )
    second = TurnIsolation(
        TurnIsolationPolicy(
            "t2",
            [target],
            expected_contents={target: "second"},
            workspace_root=tmp_path,
        )
    )
    first_staged = threading.Event()
    release_first = threading.Event()
    results = []

    def run_first() -> None:
        with first:
            _execute_tool(
                "write_file",
                {"path": str(target), "content": "first"},
                ctx=RunContext(turn_isolation=first),
            )
            first_staged.set()
            release_first.wait(2)
            results.append(first.finish(ToolResult(True, output="first")))

    def run_second() -> None:
        first_staged.wait(2)
        with second:
            _execute_tool(
                "write_file",
                {"path": str(target), "content": "second"},
                ctx=RunContext(turn_isolation=second),
            )
            results.append(second.finish(ToolResult(True, output="second")))

    ta = threading.Thread(target=run_first)
    tb = threading.Thread(target=run_second)
    ta.start()
    tb.start()
    assert first_staged.wait(1)
    release_first.set()
    ta.join()
    tb.join()
    assert all(result.success for result in results)
    assert target.read_text(encoding="utf-8") == "second"
    assert any(e["phase"] == "reservation_wait" for e in second.events)


def test_run_loop_capability_implies_filtered_context_and_records_rejection(
    tmp_path, monkeypatch
):
    import agent_cli.loop.run as run_module

    captured = {}

    class FakeLoop:
        turn = 1

        def run(self):
            return ToolResult(True, output="model said done")

    def build_loop(**kwargs):
        captured.update(kwargs)
        return FakeLoop()

    monkeypatch.setattr(run_module, "AgentLoop", build_loop)
    ctx = ContextManager(tmp_path / "session")
    target = tmp_path / "missing.txt"
    isolation = TurnIsolation(
        TurnIsolationPolicy(
            "t9",
            [target],
            expected_contents={target: "required"},
            workspace_root=tmp_path,
        )
    )
    result = run_module.run_loop(
        query="write required file",
        provider=object(),
        capabilities=object(),
        model="fake",
        ctx=ctx,
        origin_turn="t9",
        turn_isolation=isolation,
    )
    assert not result.success
    assert captured["turn_local_context"] is True
    assert captured["turn_scoping"] is True
    records = ctx.history_path.read_text(encoding="utf-8")
    assert '"tool": "turn_isolation"' in records
    assert '"origin_turn": "t9"' in records


def test_run_loop_rejects_executable_hooks_before_agent_starts(tmp_path, monkeypatch):
    import agent_cli.loop.run as run_module

    started = False

    def forbidden_loop(**kwargs):
        nonlocal started
        started = True
        raise AssertionError("agent loop must not start with executable hooks")

    monkeypatch.setattr(run_module, "AgentLoop", forbidden_loop)
    target = tmp_path / "answer.txt"
    isolation = TurnIsolation(
        TurnIsolationPolicy(
            "t1",
            [target],
            expected_contents={target: "answer"},
            workspace_root=tmp_path,
        )
    )
    result = run_module.run_loop(
        query="write",
        provider=object(),
        capabilities=object(),
        model="fake",
        turn_isolation=isolation,
        hooks_config={"PreToolUse": [{"command": "touch escaped"}]},
    )
    assert not result.success and "hooks" in result.error
    assert started is False
