#!/usr/bin/env python3
"""P1 live comparison: scoped prompt, filtered context, enforced publication.

The experimental unit is one concurrent two-request run.  Arms are alternated
within each repetition to reduce endpoint-time confounding:

* scoped: current shipped prompt scoping;
* filtered: scoped prompt + omission of other still-active turns;
* enforced: filtered context + exact path capability + staged exact oracle.

Usage (requires AGENT_CLI_BASE_URL/API_KEY/MODEL):
  .venv/bin/python bench/multiuser/p1_isolation_real.py --reps 20
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import shutil
import statistics
import threading
import time
from pathlib import Path

from driver import AgentServer, turn_chain
from e1_ablation import exact_binomial_ci, exact_mcnemar_p
from n3c_scoping_real import (
    WORKLOADS,
    Task,
    _attribute,
    _read_history,
    _want,
    real_llm_from_env,
)

ARMS = ("scoped", "filtered", "enforced")


def _implementation_digest() -> str:
    """Fingerprint the executable Python sources used by each run."""
    root = Path(__file__).resolve().parents[2]
    paths = sorted((root / "agent_cli").rglob("*.py")) + [Path(__file__).resolve()]
    digest = hashlib.sha256()
    for path in paths:
        digest.update(str(path.relative_to(root)).encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


def _arm_order(rep: int) -> tuple[str, ...]:
    shift = (rep - 1) % len(ARMS)
    order = ARMS[shift:] + ARMS[:shift]
    return order if ((rep - 1) // len(ARMS)) % 2 == 0 else tuple(reversed(order))


def run_rep(llm: dict, arm: str, rep: int, tasks: tuple[Task, Task]) -> dict | None:
    import tempfile

    ws = Path(tempfile.mkdtemp(prefix=f"p1-{arm}-{rep}-"))
    extra = ["--turn-scoping"]
    if arm in {"filtered", "enforced"}:
        extra.append("--turn-local-context")
    server = AgentServer(
        ws,
        None,
        contract="parallel",
        max_turns=2,
        real_llm=llm,
        extra=extra,
    )
    connections = (f"A-{rep}", f"B-{rep}")
    try:
        before = len(server.events())
        statuses: dict[str, int] = {}
        gate = threading.Barrier(2)

        def submit(conn: str, task: Task) -> None:
            gate.wait()
            kwargs = {}
            if arm == "enforced":
                kwargs = {
                    "write_paths": list(task.files),
                    "expected_contents": dict(task.expected),
                }
            statuses[conn] = server.chat(task.prompt, conn, **kwargs)

        threads = [
            threading.Thread(target=submit, args=(conn, task))
            for conn, task in zip(connections, tasks, strict=True)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        if set(statuses.values()) != {200}:
            return None
        events = server.wait_completes_since(before, 2, timeout=900)
        chains = [turn_chain(events, conn) for conn in connections]
        if any(c["dispatch"] is None or c["complete"] is None for c in chains):
            return None

        records = _read_history(server.session_dir)
        attributed = _attribute(records, tasks)
        if len(attributed) != 2:
            return None
        answers: dict[str, str] = {}
        for record in records:
            owner = record.get("reply_to")
            if owner and record.get("text"):
                answers[owner] = (answers.get(owner, "") + "\n" + str(record["text"]))[
                    -4000:
                ]
        by_key = {task.key: task for task in tasks}
        per_turn = []
        for turn in attributed:
            own_task = by_key[turn["target"]]
            other_task = next(t for t in tasks if t.key != turn["target"])
            own, other = _want(own_task), _want(other_task)
            content_correct = all(
                turn["writes"].get(name, "").rstrip("\n") == expected.rstrip("\n")
                for name, expected in own_task.expected.items()
            )
            answer = answers.get(turn["query"], "")
            per_turn.append(
                {
                    "target": turn["target"],
                    "attemptedPaths": sorted(turn["files"]),
                    "attemptedOutsideCapability": bool(turn["files"] & other),
                    "wroteAllAssignedTargetPaths": own <= turn["files"],
                    "assignedContentCorrect": content_correct,
                    "taskCorrect": own <= turn["files"] and content_correct,
                    "responseMentionsOtherCompletionTag": (
                        other_task.completion_tag in answer
                    ),
                }
            )

        final_correct = True
        for task in tasks:
            for name, expected in task.expected.items():
                try:
                    actual = (ws / name).read_text(encoding="utf-8")
                except OSError:
                    final_correct = False
                    continue
                final_correct &= actual.rstrip("\n") == expected.rstrip("\n")

        run_events = events[before:]
        isolation_events = [e for e in run_events if e.get("event") == "isolation"]
        llm_calls = [e for e in run_events if e.get("event") == "llm_call"]
        blocked = [e for e in isolation_events if e.get("phase") == "effect_blocked"]
        published = [
            e for e in isolation_events if e.get("phase") == "write_set_published"
        ]
        validations_failed = [
            e for e in isolation_events if e.get("phase") == "validation_failed"
        ]
        approved_by_turn = {
            chain["turn_id"]: set(task.files)
            for chain, task in zip(chains, tasks, strict=True)
        }
        enforced_published_outside = any(
            Path(path).name not in approved_by_turn.get(event.get("turn_id"), set())
            for event in published
            for path in event.get("paths", [])
        )
        return {
            "arm": arm,
            "rep": rep,
            "spanMs": [round(c["complete"] - c["dispatch"], 1) for c in chains],
            "turns": per_turn,
            "runAttemptedOutsideCapability": any(
                t["attemptedOutsideCapability"] for t in per_turn
            ),
            # In the first two cooperative-tool arms, a write_file action is
            # immediately published. In the enforced arm, publication events
            # are authoritative and can contain only requester-approved paths.
            "runPublishedOutsideCapability": (
                any(t["attemptedOutsideCapability"] for t in per_turn)
                if arm != "enforced"
                else enforced_published_outside
            ),
            "bothTasksCorrect": all(t["taskCorrect"] for t in per_turn),
            "repositoryCorrect": bool(final_correct),
            "anyResponseCrossTag": any(
                t["responseMentionsOtherCompletionTag"] for t in per_turn
            ),
            "blockedEffects": len(blocked),
            "publishedWriteSets": len(published),
            "validationFailures": len(validations_failed),
            "llmCalls": len(llm_calls),
            "inputTokens": sum(int(e.get("input_tokens", 0)) for e in llm_calls),
            "outputTokens": sum(int(e.get("output_tokens", 0)) for e in llm_calls),
            "implementationDigest": _implementation_digest(),
        }
    finally:
        server.stop()
        shutil.rmtree(ws, ignore_errors=True)


def summarize(rows: list[dict]) -> list[dict]:
    summary = []
    for arm in ARMS:
        sub = [row for row in rows if row["arm"] == arm]
        if not sub:
            continue
        n = len(sub)
        attempted = sum(r["runAttemptedOutsideCapability"] for r in sub)
        published = sum(r["runPublishedOutsideCapability"] for r in sub)
        correct = sum(r["bothTasksCorrect"] for r in sub)
        repository = sum(r["repositoryCorrect"] for r in sub)
        cross_tag = sum(r["anyResponseCrossTag"] for r in sub)
        summary.append(
            {
                "arm": arm,
                "runs": n,
                "runsAttemptedOutsideCapability": attempted,
                "attemptedExactCI95": exact_binomial_ci(attempted, n),
                "runsPublishedOutsideCapability": published,
                "publishedExactCI95": exact_binomial_ci(published, n),
                "bothTasksCorrect": correct,
                "correctExactCI95": exact_binomial_ci(correct, n),
                "repositoryCorrect": repository,
                "repositoryExactCI95": exact_binomial_ci(repository, n),
                "runsWithResponseCrossTag": cross_tag,
                "blockedEffects": sum(r["blockedEffects"] for r in sub),
                "validationFailures": sum(r["validationFailures"] for r in sub),
                "medianLongerTurnSpanMs": round(
                    statistics.median(max(r["spanMs"]) for r in sub), 1
                ),
                "medianLlmCalls": statistics.median(r["llmCalls"] for r in sub),
                "medianInputTokens": statistics.median(r["inputTokens"] for r in sub),
                "medianOutputTokens": statistics.median(r["outputTokens"] for r in sub),
            }
        )
    return summary


def paired(rows: list[dict]) -> list[dict]:
    results = []
    for contrast in ("filtered", "enforced"):
        common = sorted(
            rep
            for rep in {r["rep"] for r in rows}
            if {r["arm"] for r in rows if r["rep"] == rep} >= {"scoped", contrast}
        )
        for outcome in (
            "runAttemptedOutsideCapability",
            "runPublishedOutsideCapability",
            "bothTasksCorrect",
            "repositoryCorrect",
            "anyResponseCrossTag",
        ):
            scoped_only = contrast_only = 0
            for rep in common:
                by_arm = {r["arm"]: r for r in rows if r["rep"] == rep}
                a, b = bool(by_arm["scoped"][outcome]), bool(by_arm[contrast][outcome])
                scoped_only += a and not b
                contrast_only += b and not a
            results.append(
                {
                    "contrast": f"scoped vs {contrast}",
                    "outcome": outcome,
                    "pairedRuns": len(common),
                    "scopedOnly": scoped_only,
                    "contrastOnly": contrast_only,
                    "exactMcNemarP": exact_mcnemar_p(scoped_only, contrast_only),
                }
            )
    return results


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--reps", type=int, default=20)
    parser.add_argument("--retry", type=int, default=1)
    parser.add_argument("--workload", choices=sorted(WORKLOADS), default="realistic")
    parser.add_argument("--out", type=Path, default=Path(__file__).parent / "out")
    parser.add_argument("--rederive", action="store_true")
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    raw_path = args.out / f"p1-isolation-{args.workload}.jsonl"
    summary_path = args.out / f"p1-isolation-{args.workload}.json"
    failures = []
    if args.rederive:
        rows = [
            json.loads(line)
            for line in raw_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
    else:
        llm = real_llm_from_env()
        tasks = WORKLOADS[args.workload]
        rows = []
        raw_path.write_text("", encoding="utf-8")
        started = time.time()
        for rep in range(1, args.reps + 1):
            for arm in _arm_order(rep):
                row = None
                for attempt in range(1, args.retry + 2):
                    row = run_rep(llm, arm, rep, tasks)
                    if row is not None:
                        row["attempt"] = attempt
                        break
                if row is None:
                    failures.append({"rep": rep, "arm": arm})
                    continue
                rows.append(row)
                with raw_path.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(row) + "\n")
                print(json.dumps(row), flush=True)
        print(f"# elapsed {time.time() - started:.1f}s", flush=True)

    report = {
        "workload": args.workload,
        "experimentalUnit": "one concurrent two-request run/pair",
        "armOrder": "rotating balanced order by repetition",
        "requestedRunsPerArm": args.reps,
        "arms": summarize(rows),
        "pairedContrasts": paired(rows),
        "failedRuns": failures,
        "implementationDigests": sorted(
            {row.get("implementationDigest", "") for row in rows}
        ),
        "model": os.environ.get("AGENT_CLI_MODEL", ""),
        "host": {"platform": platform.platform(), "logicalCpuCount": os.cpu_count()},
        "runDate": time.strftime("%Y-%m-%d"),
        "claimBoundary": (
            "Enforced publication covers cooperative registered tools, canonical paths, "
            "and path stability checked at commit. It does not claim general response "
            "semantics, external processes, or crash-atomic multi-file transactions."
        ),
    }
    summary_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
