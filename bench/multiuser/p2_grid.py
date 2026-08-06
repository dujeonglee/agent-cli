#!/usr/bin/env python3
"""P2 — 워크로드 혼합비 그리드: 병렬 이점의 경계 매핑 (XC-SCOPE 반영 재설계).

가장 강한 반론("파일 작업이 많으면 병렬이 무의미하지 않나")에 대한 방어.
초기 탐색에서 관찰된 것: 양쪽 턴이 도구를 쓰면 병렬 이점이 붕괴하고, 서로
다른 파일이어도 셸이 지배하는 워크로드에서는 종단 이득이 없다. 그래서 이
그리드의 프레이밍은 "병렬이 항상 빠르다"가 아니라 **이득은 추론 비중의 함수이고,
락의 가치는 지연이 아니라 무결성**이다.

설계: 동시 턴 2개 × 부수효과 스텝 수 w ∈ {0, 2, 6} × 충돌 c ∈ {0(다른
파일), 1(같은 파일)} × lock-scope ∈ {workspace, conflict}. 각 턴 =
(w 회 write_file + 추론 스트리밍). 지표:

  speedup   = (dur_A + dur_B) / makespan — 1.0=완전 직렬화, 2.0=완전 병렬
  lock_wait = 두 턴의 effect_lock 대기 총합 (M2 lock 이벤트)

사용: .venv/bin/python bench/multiuser/p2_grid.py [--reps 5]
"""

from __future__ import annotations

import argparse
import json
import shutil
import tempfile
from pathlib import Path

from driver import AgentServer, MockLlm, median, turn_chain

WRITES = (0, 2, 6)
CONFLICTS = (0, 1)
SCOPES = ("workspace", "conflict")
#: 추론 성분 — 두 턴 모두 동일 (w 가 커질수록 부수효과 비중 m 이 커진다).
INFER = "ttft=100 tok=10 n=60"


def run_cell(server: AgentServer, w: int, conflict: int, rep: int) -> dict | None:
    pa = "shared.txt" if conflict else f"a-{rep}.txt"
    pb = "shared.txt" if conflict else f"b-{rep}.txt"
    a_conn, b_conn = f"A-{w}-{conflict}-{rep}", f"B-{w}-{conflict}-{rep}"
    fw = f"fwrite={w} lines=32" if w else ""
    before = len(server.events())
    assert (
        server.chat(f"work [[bench {INFER} {fw} fpath={pa} marker=AA id=a]]", a_conn)
        == 200
    )
    assert (
        server.chat(f"work [[bench {INFER} {fw} fpath={pb} marker=BB id=b]]", b_conn)
        == 200
    )
    events = server.wait_completes_since(before, 2, timeout=300)
    ca, cb = turn_chain(events, a_conn), turn_chain(events, b_conn)
    if None in (ca["dispatch"], ca["complete"], cb["dispatch"], cb["complete"]):
        return None
    dur_a = ca["complete"] - ca["dispatch"]
    dur_b = cb["complete"] - cb["dispatch"]
    makespan = max(ca["complete"], cb["complete"]) - min(ca["dispatch"], cb["dispatch"])
    lock_wait = sum(
        e.get("wait_ms", 0.0)
        for e in events[before:]
        if e.get("event") == "lock" and e.get("phase") == "acquire"
    )
    return {
        "writes": w,
        "conflict": conflict,
        "rep": rep,
        "durA_ms": round(dur_a, 1),
        "durB_ms": round(dur_b, 1),
        "makespan_ms": round(makespan, 1),
        "speedup": round((dur_a + dur_b) / makespan, 3) if makespan else None,
        "lock_wait_ms": round(lock_wait, 1),
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--reps", type=int, default=5)
    ap.add_argument("--out", type=Path, default=Path(__file__).parent / "out")
    args = ap.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)

    mock = MockLlm()
    rows = []
    try:
        for scope in SCOPES:
            for w in WRITES:
                for conflict in CONFLICTS:
                    if w == 0 and conflict:
                        continue  # 쓰기 0 이면 충돌 축이 무의미
                    ws = Path(tempfile.mkdtemp(prefix=f"p2-{scope}-{w}-{conflict}-"))
                    server = AgentServer(
                        ws,
                        mock.port,
                        contract="parallel",
                        lock_scope=scope,
                        max_turns=2,
                    )
                    try:
                        for rep in range(1, args.reps + 1):
                            row = run_cell(server, w, conflict, rep)
                            if row is not None:
                                row["lock_scope"] = scope
                                rows.append(row)
                                print(json.dumps(row), flush=True)
                    finally:
                        server.stop()
                        shutil.rmtree(ws, ignore_errors=True)
    finally:
        mock.stop()

    (args.out / "p2-grid.jsonl").write_text(
        "".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8"
    )
    summary = []
    for scope in SCOPES:
        for w in WRITES:
            for conflict in CONFLICTS:
                cell = [
                    r
                    for r in rows
                    if r["lock_scope"] == scope
                    and r["writes"] == w
                    and r["conflict"] == conflict
                ]
                if not cell:
                    continue
                summary.append(
                    {
                        "lock_scope": scope,
                        "writes": w,
                        "conflict": conflict,
                        "n": len(cell),
                        "speedup_p50": round(median([r["speedup"] for r in cell]), 3),
                        "lock_wait_p50": round(
                            median([r["lock_wait_ms"] for r in cell]), 1
                        ),
                    }
                )
    (args.out / "p2-summary.json").write_text(
        json.dumps(summary, indent=2), encoding="utf-8"
    )
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
