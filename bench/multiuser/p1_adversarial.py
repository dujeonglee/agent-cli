#!/usr/bin/env python3
"""Deterministic adversarial checks for P1 turn isolation invariants."""

from __future__ import annotations

import argparse
import json
import tempfile
import threading
import time
from pathlib import Path

from agent_cli.loop.state import LoopConfig, LoopState
from agent_cli.loop.tool_bridge import ToolBridge
from agent_cli.tools import RunContext, _execute_tool
from agent_cli.tools.effect import EffectIntent, EffectKind
from agent_cli.tools.result import ToolResult
from agent_cli.tools.turn_isolation import TurnIsolation, TurnIsolationPolicy


def _row(name: str, expected, observed) -> dict:
    return {
        "invariant": name,
        "expected": expected,
        "observed": observed,
        "pass": expected == observed,
    }


def run() -> list[dict]:
    rows: list[dict] = []
    # Keep the disposable workspace under the launch workspace so the normal
    # confinement layer and the P1 capability layer are both active.
    with tempfile.TemporaryDirectory(prefix="p1-adversarial-", dir=Path.cwd()) as raw:
        root = Path(raw)
        mine = root / "mine.txt"
        other = root / "other.txt"
        mine.write_text("old", encoding="utf-8")
        isolation = TurnIsolation(
            TurnIsolationPolicy(
                "t1", [mine], expected_contents={mine: "new"}, workspace_root=root
            )
        )
        bridge = ToolBridge(
            LoopConfig(
                tools_list=["write_file", "shell", "agent"], turn_isolation=isolation
            ),
            LoopState(),
            None,
            None,
        )
        with isolation:
            staged = bridge._dispatch_tool_with_hooks(
                "write_file", {"path": str(mine), "content": "new"}
            )
            rows.append(_row("staged write succeeds", True, staged.success))
            rows.append(
                _row(
                    "staged write is not prematurely visible",
                    "old",
                    mine.read_text(encoding="utf-8"),
                )
            )
            outside = bridge._dispatch_tool_with_hooks(
                "write_file", {"path": str(other), "content": "intrude"}
            )
            rows.append(_row("out-of-scope path is blocked", False, outside.success))
            traversal = bridge._dispatch_tool_with_hooks(
                "write_file", {"path": "../escape.txt", "content": "intrude"}
            )
            rows.append(_row("parent traversal is blocked", False, traversal.success))
            shell = bridge._dispatch_tool_with_hooks(
                "shell", {"command": "touch escape"}
            )
            rows.append(_row("shell bypass is blocked", False, shell.success))
            direct_shell = _execute_tool(
                "shell",
                {"command": "touch direct-escape"},
                ctx=RunContext(turn_isolation=isolation),
            )
            rows.append(
                _row(
                    "direct dispatch cannot bypass shell guard",
                    False,
                    direct_shell.success,
                )
            )
            composite = isolation.authorize_tool(
                "agent", {}, EffectIntent(EffectKind.NON_WORKSPACE_OR_COMPOSITE)
            )
            rows.append(
                _row("composite bypass is blocked", True, composite is not None)
            )
            unknown = isolation.authorize_tool(
                "plugin_x", {}, EffectIntent(EffectKind.UNKNOWN_WORKSPACE_EFFECT)
            )
            rows.append(
                _row("unclassified plugin fails closed", True, unknown is not None)
            )
            published = isolation.finish(ToolResult(True, output="done"))
            rows.append(_row("validated write publishes", True, published.success))
        rows.append(
            _row(
                "published content matches oracle",
                "new",
                mine.read_text(encoding="utf-8"),
            )
        )
        rows.append(_row("blocked target remains absent", False, other.exists()))

        rejected = root / "rejected.txt"
        failed = TurnIsolation(
            TurnIsolationPolicy(
                "t2",
                [rejected],
                expected_contents={rejected: "wanted"},
                workspace_root=root,
            )
        )
        with failed:
            _execute_tool(
                "write_file",
                {"path": str(rejected), "content": "wrong"},
                ctx=RunContext(turn_isolation=failed),
            )
            verdict = failed.finish(ToolResult(True, output="done"))
        rows.append(
            _row("failed validation rejects publication", False, verdict.success)
        )
        rows.append(
            _row(
                "failed validation leaves workspace unchanged", False, rejected.exists()
            )
        )

        conflict = root / "conflict.txt"
        conflict.write_text("base", encoding="utf-8")
        versioned = TurnIsolation(
            TurnIsolationPolicy(
                "t3",
                [conflict],
                expected_contents={conflict: "mine"},
                workspace_root=root,
            )
        )
        with versioned:
            _execute_tool(
                "write_file",
                {"path": str(conflict), "content": "mine"},
                ctx=RunContext(turn_isolation=versioned),
            )
            conflict.write_text("external", encoding="utf-8")
            verdict = versioned.finish(ToolResult(True, output="done"))
        rows.append(_row("version conflict rejects commit", False, verdict.success))
        rows.append(
            _row(
                "version conflict preserves external write",
                "external",
                conflict.read_text(encoding="utf-8"),
            )
        )

        alias = root / "hardlink.txt"
        alias.hardlink_to(conflict)
        first = TurnIsolation(
            TurnIsolationPolicy(
                "t4", [conflict], validator=lambda _: True, workspace_root=root
            )
        )
        second = TurnIsolation(
            TurnIsolationPolicy(
                "t5", [alias], validator=lambda _: True, workspace_root=root
            )
        )
        active = 0
        max_active = 0
        lock = threading.Lock()
        first_entered = threading.Event()

        def hold(isolation: TurnIsolation, mark: bool) -> None:
            nonlocal active, max_active
            with isolation:
                with lock:
                    active += 1
                    max_active = max(max_active, active)
                if mark:
                    first_entered.set()
                    time.sleep(0.08)
                with lock:
                    active -= 1

        ta = threading.Thread(target=hold, args=(first, True))
        tb = threading.Thread(
            target=lambda: (first_entered.wait(), hold(second, False))
        )
        ta.start()
        tb.start()
        ta.join()
        tb.join()
        rows.append(_row("hard-link aliases never reserve concurrently", 1, max_active))

        # Two legitimate turns may request the exact same path.  They must not
        # stage/publish concurrently, and the successor's dispatch-time
        # baseline must be taken after the predecessor publishes so that the
        # queued write is not rejected as a false version conflict.
        shared = root / "shared.txt"
        shared.write_text("base", encoding="utf-8")
        predecessor = TurnIsolation(
            TurnIsolationPolicy(
                "t6",
                [shared],
                expected_contents={shared: "first"},
                workspace_root=root,
            )
        )
        successor = TurnIsolation(
            TurnIsolationPolicy(
                "t7",
                [shared],
                expected_contents={shared: "second"},
                workspace_root=root,
            )
        )
        staged_first = threading.Event()
        release_first = threading.Event()
        entered_second = threading.Event()
        verdicts: list[ToolResult] = []

        def write_first() -> None:
            with predecessor:
                _execute_tool(
                    "write_file",
                    {"path": str(shared), "content": "first"},
                    ctx=RunContext(turn_isolation=predecessor),
                )
                staged_first.set()
                release_first.wait(2)
                verdicts.append(predecessor.finish(ToolResult(True, output="done")))

        def write_second() -> None:
            staged_first.wait(2)
            with successor:
                entered_second.set()
                _execute_tool(
                    "write_file",
                    {"path": str(shared), "content": "second"},
                    ctx=RunContext(turn_isolation=successor),
                )
                verdicts.append(successor.finish(ToolResult(True, output="done")))

        ta = threading.Thread(target=write_first)
        tb = threading.Thread(target=write_second)
        ta.start()
        tb.start()
        staged_first.wait(2)
        rows.append(
            _row(
                "same-path successor waits for predecessor",
                False,
                entered_second.wait(0.05),
            )
        )
        release_first.set()
        ta.join()
        tb.join()
        rows.append(
            _row(
                "queued same-path publications both validate",
                True,
                len(verdicts) == 2 and all(verdict.success for verdict in verdicts),
            )
        )
        rows.append(
            _row(
                "queued same-path publication has a serial final value",
                "second",
                shared.read_text(encoding="utf-8"),
            )
        )
    return rows


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--out",
        type=Path,
        default=Path(__file__).parent / "out" / "p1-adversarial.json",
    )
    args = parser.parse_args()
    rows = run()
    report = {
        "suite": "P1 deterministic adversarial invariants",
        "checks": len(rows),
        "passed": sum(row["pass"] for row in rows),
        "rows": rows,
        "statisticalInterpretation": "deterministic invariant checks; no population p-value",
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))
    raise SystemExit(0 if report["passed"] == report["checks"] else 1)


if __name__ == "__main__":
    main()
