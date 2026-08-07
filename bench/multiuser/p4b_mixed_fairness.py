#!/usr/bin/env python3
"""P4b — 혼합 워크로드의 효과층 대기: admission 공정성이 다스리지 않는 층 (§6.8 보강).

§6.8 의 사용자별 게이트는 **디스패치**를 공정하게 만든다. 그러나 §4.4 의
효과 락은 셸을 배타로 만들고 추월을 금지하므로(strict FIFO), 한 사용자의
셸이 도는 동안 다른 사용자의 파일 효과는 admission 과 무관하게 줄을 선다.
이 실험은 그 비용을 값매김한다 — 리뷰(R2-W5, 1차 Q5)가 지적한 층이다.

측정 층은 P2-SCOPE 와 같다: 에이전트/LLM 스택을 우회해 실제 도구와 락
프리미티브를 직접 구동한다(공유 트랜스크립트에서 서로 **다른** 두 워크로드를
목으로 스크립트할 수 없다는 §5 의 실측 함정 때문 — 셸 위주 A 와 파일 위주 B
가 정확히 그 경우다).

워크로드::

    A (셸 위주):   3 × [ sleep 1 s (추론);  hold(SHELL 배타) { sleep shell_s } ]
    B (파일 위주): A 가 끝날 때까지 [ sleep 0.2 s (추론); hold(FILE_WRITE b.txt)
                   { write_file } ] — 획득 대기를 요청마다 기록

팔:
  shell_s ∈ {1, 5} — §6.4 셸 작동점(F2)과 같은 두 hold 길이
  mixed / baseline — baseline 은 A 없이 같은 시간 동안 B 만 돈다
                     (B 대기의 바닥 = 락 오버헤드 자체)

예상 상계: B 의 최악 대기 ≈ shell_s (요청이 hold 시작 직후 도착한 경우).
strict FIFO 의 가격이 "최대 셸 hold 하나"임을 분포로 확인하는 것이 목적이며,
결과가 크더라도 그것이 §4.4 가 공정성 보장을 위해 의도적으로 지불한 값이다.

사용: .venv/bin/python bench/multiuser/p4b_mixed_fairness.py [--reps 5]
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import statistics
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from agent_cli.tools import effect_lock
from agent_cli.tools.effect import EffectIntent, EffectKind
from agent_cli.tools.write_file import WriteFileTool

SHELL_ROUNDS = 3
A_THINK_S = 1.0
B_THINK_S = 0.2


def run_rep(shell_s: float, with_a: bool) -> dict:
    ws = Path(tempfile.mkdtemp(prefix=f"p4b-{'mixed' if with_a else 'base'}-"))
    cwd = os.getcwd()
    os.chdir(ws)
    tool = WriteFileTool()
    b_waits: list[float] = []
    b_holds: list[float] = []
    duration_s = SHELL_ROUNDS * (A_THINK_S + shell_s)
    done = threading.Event()
    start_gate = threading.Barrier(2 if with_a else 1)

    def turn_a() -> None:
        start_gate.wait()
        for _ in range(SHELL_ROUNDS):
            time.sleep(A_THINK_S)
            with effect_lock.hold(EffectIntent(kind=EffectKind.SHELL), key="p4b"):
                time.sleep(shell_s)
        done.set()

    def turn_b() -> None:
        start_gate.wait()
        t_end = time.perf_counter() + duration_s
        seq = 0
        # mixed 는 A 종료까지, baseline 은 같은 벽시계 시간 동안 돈다 —
        # 두 팔의 B 요청 수가 비슷해야 분포 비교가 성립한다.
        while (not done.is_set()) if with_a else (time.perf_counter() < t_end):
            time.sleep(B_THINK_S)
            args = {"path": "b.txt", "content": f"#B {seq}\n" + "x" * 256 + "\n"}
            intent = tool.effect_intent(args)
            t_req = time.perf_counter()
            with effect_lock.hold(intent, key="p4b"):
                t_in = time.perf_counter()
                tool._run(args)
                b_holds.append((time.perf_counter() - t_in) * 1000.0)
            b_waits.append((t_in - t_req) * 1000.0)
            seq += 1

    try:
        effect_lock.reset()
        effect_lock.set_scope("conflict")  # 출하 기본 스코프 — §4.4 v2
        threads = [threading.Thread(target=turn_b)]
        if with_a:
            threads.append(threading.Thread(target=turn_a))
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    finally:
        effect_lock.reset()
        os.chdir(cwd)
        shutil.rmtree(ws, ignore_errors=True)

    return {
        "b_requests": len(b_waits),
        "b_wait_p50_ms": round(statistics.median(b_waits), 2) if b_waits else None,
        "b_wait_p95_ms": round(_pct(b_waits, 95), 2) if b_waits else None,
        "b_wait_max_ms": round(max(b_waits), 2) if b_waits else None,
        "b_hold_p50_ms": round(statistics.median(b_holds), 3) if b_holds else None,
    }


def _pct(values: list[float], p: float) -> float:
    xs = sorted(values)
    k = (len(xs) - 1) * p / 100.0
    lo, hi = int(k), min(int(k) + 1, len(xs) - 1)
    return xs[lo] + (xs[hi] - xs[lo]) * (k - lo)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--reps", type=int, default=5)
    ap.add_argument("--out", type=Path, default=Path(__file__).parent / "out")
    args = ap.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)

    cells = []
    for shell_s in (1.0, 5.0):
        for with_a in (True, False):
            rows = [run_rep(shell_s, with_a) for _ in range(args.reps)]
            cell = {
                "shell_s": shell_s,
                "arm": "mixed" if with_a else "baseline",
                "reps": args.reps,
                "rows": rows,
                "b_wait_p50_ms_median": statistics.median(
                    r["b_wait_p50_ms"] for r in rows
                ),
                "b_wait_p95_ms_median": statistics.median(
                    r["b_wait_p95_ms"] for r in rows
                ),
                "b_wait_max_ms_max": max(r["b_wait_max_ms"] for r in rows),
            }
            cells.append(cell)
            print(
                json.dumps(
                    {k: v for k, v in cell.items() if k != "rows"}, ensure_ascii=False
                )
            )

    out = {
        "config": {
            "shell_rounds": SHELL_ROUNDS,
            "a_think_s": A_THINK_S,
            "b_think_s": B_THINK_S,
            "lock_scope": "conflict",
        },
        "cells": cells,
    }
    out_path = args.out / "p4b-mixed.json"
    out_path.write_text(json.dumps(out, indent=2, ensure_ascii=False))
    print(f"wrote {out_path}")


if __name__ == "__main__":
    main()
