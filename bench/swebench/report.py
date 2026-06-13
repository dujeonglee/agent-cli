#!/usr/bin/env python3
"""Summarize a SWE-bench run: resolution score (from the official harness
report) + agent-cli operational health (from health.json).

  bench/.venv/bin/python bench/swebench/report.py --out bench/runs/smoke
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="bench/runs/smoke")
    args = ap.parse_args()
    out = Path(args.out)

    health = json.loads((out / "health.json").read_text())
    n = len(health)
    empty = sum(1 for h in health if h.get("empty_patch"))
    errored = sum(1 for h in health if h.get("error"))
    timed_out = sum(1 for h in health if h.get("timed_out"))
    turns = [h["turns"] for h in health if h.get("turns")]
    fails: Counter = Counter()
    stages: Counter = Counter()
    for h in health:
        for k, v in (h.get("failures") or {}).items():
            fails[k] += v
        for k, v in (h.get("parse_stage") or {}).items():
            stages[k] += v
    total_turns = sum(turns)

    print("=" * 60)
    print(f"SWE-bench run: {out}")
    print("=" * 60)
    print("\n[agent-cli 운영 헬스]")
    print(f"  인스턴스: {n}  (에러 {errored}, 타임아웃 {timed_out})")
    print(f"  빈 패치(no diff): {empty}/{n}")
    if turns:
        print(f"  총 턴: {total_turns}  (평균 {total_turns / len(turns):.1f}/인스턴스)")
    print(f"  형식 실패: {dict(fails) or '0'}")
    if total_turns:
        fr = sum(fails.values()) / total_turns * 100
        print(f"    → 형식실패율 {sum(fails.values())}/{total_turns} ({fr:.1f}%)")
    print(f"  parse_stage: {dict(stages)}  (0=실패 1=정상 2=drift복구)")

    # official harness report (written by run_evaluation as
    # <model_name>.<run_id>.json in cwd, or a results dir)
    reports = list(Path(".").glob("*.json")) + list(out.glob("*report*.json"))
    report = None
    for p in reports:
        try:
            d = json.loads(p.read_text())
            if "resolved_instances" in d or "resolved" in d:
                report = (p, d)
                break
        except Exception:
            pass
    print("\n[채점 결과 (공식 하니스)]")
    if report:
        p, d = report
        res = d.get("resolved_instances", d.get("resolved"))
        sub = d.get("submitted_instances", d.get("submitted", n))
        print(f"  {p.name}: resolved {res}/{sub}")
        if "completed_instances" in d:
            print(f"  completed: {d['completed_instances']}, error: "
                  f"{d.get('error_instances')}")
    else:
        print("  (아직 eval 미실행 — run_evaluation 후 다시 report 실행)")
    print()


if __name__ == "__main__":
    main()
