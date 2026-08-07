#!/usr/bin/env python3
"""E2d — 병렬 계약의 지연 독립성은 동시 사용자 수 N 에서도 유지되는가.

§6.1 은 사용자 2명(A 1명 + 질문자 1명)에서 기울기 0 을 보였다. 리뷰의 정당한
반론은 규모다: 그 평탄함이 N 이 커져도 남는가, 아니면 2명짜리 성질인가.

구성은 §6.1 을 그대로 늘린다. A 가 L = 15 s 짜리 작업을 시작하고, 0.5 s 뒤
**N−1 명의 질문자**가 동시에 한 줄짜리 질문을 던진다. 각 질문자의 TTFT 를
전부 잰다. 상한(``--max-concurrent-turns``)은 N 으로 둬서 입장 게이트가
아니라 계약 자체를 보게 한다(게이트의 효과는 §6.8 이 따로 잰다).

**목 붕괴가 없는 이유**(§5 의 함정): 모든 턴이 도구 스텝 0 인 단일 콜이라
continuation call 이 없다. 게다가 질문자들의 지시자는 전부 동일하므로,
공유 트랜스크립트에서 어느 것이 마지막이 되든 같은 스크립트다. A 의 메시지는
질문자들보다 먼저 덧붙으므로 마지막이 되지 않는다.

사용: .venv/bin/python bench/multiuser/e2d_nscale.py [--reps 10]
"""

from __future__ import annotations

import argparse
import json
import shutil
import tempfile
import threading
import time
from pathlib import Path

from driver import AgentServer, MockLlm, median, percentile, ttft_ms, turn_chain

LEVEL_MS = 15000
A_TOK_MS = 25
B_DIRECTIVE = "[[bench ttft=200 tok=5 n=8 id=b]]"
B_DELAY_S = 0.5
#: 총 동시 사용자 수 = A 1명 + (N−1) 질문자.
N_VALUES = (2, 4, 8)


def run_cell(server: AgentServer, n_users: int, rep: int) -> list[dict]:
    a_conn = f"A-{n_users}-{rep}"
    n_tokens = max(2, LEVEL_MS // A_TOK_MS)
    before = len(server.events())
    assert (
        server.chat(
            f"long task [[bench ttft=200 tok={A_TOK_MS} n={n_tokens} id=a]]", a_conn
        )
        == 200
    )
    time.sleep(B_DELAY_S)

    # 질문자들은 **동시에** 던진다 — 순차 제출이면 뒤쪽 질문자가 앞쪽의
    # 처리 시간만큼 유리해져 N 의 효과와 섞인다.
    q_conns = [f"Q{i}-{n_users}-{rep}" for i in range(1, n_users)]
    barrier = threading.Barrier(len(q_conns))
    results: dict[str, int] = {}

    def submit(conn: str) -> None:
        barrier.wait()
        results[conn] = server.chat(f"quick question {B_DIRECTIVE}", conn)

    threads = [threading.Thread(target=submit, args=(c,)) for c in q_conns]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert all(v == 200 for v in results.values()), f"submit failed: {results}"

    # A + 질문자 전원의 complete 를 기다린다.
    events = server.wait_completes_since(before, n_users)
    rows = []
    for conn in q_conns:
        ttft = ttft_ms(turn_chain(events, conn))
        rows.append(
            {
                "N": n_users,
                "rep": rep,
                "conn": conn,
                "L": LEVEL_MS,
                "ttftMs": round(ttft, 1) if ttft is not None else None,
                "ok": ttft is not None,
            }
        )
    return rows


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--reps", type=int, default=10)
    ap.add_argument("--out", type=Path, default=Path(__file__).parent / "out")
    ap.add_argument("--ns", type=int, nargs="*", default=list(N_VALUES))
    args = ap.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)

    mock = MockLlm()
    rows: list[dict] = []
    t_start = time.time()
    try:
        for n_users in args.ns:
            ws = Path(tempfile.mkdtemp(prefix=f"e2d-n{n_users}-"))
            server = AgentServer(ws, mock.port, contract="parallel", max_turns=n_users)
            try:
                for rep in range(1, args.reps + 1):
                    cell = run_cell(server, n_users, rep)
                    rows.extend(cell)
                    print(
                        json.dumps({"N": n_users, "rep": rep, "cell": cell}), flush=True
                    )
            finally:
                server.stop()
                shutil.rmtree(ws, ignore_errors=True)
    finally:
        mock.stop()

    (args.out / "e2d-nscale.jsonl").write_text(
        "".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8"
    )

    summary = {"L_ms": LEVEL_MS, "reps": args.reps, "cells": []}
    for n_users in sorted({r["N"] for r in rows}):
        v = [r["ttftMs"] for r in rows if r["N"] == n_users and r["ok"]]
        summary["cells"].append(
            {
                "N": n_users,
                "questioners": n_users - 1,
                "n": len(v),
                "p50": round(median(v), 1) if v else None,
                "p95": round(percentile(v, 0.95), 1) if v else None,
                "max": round(max(v), 1) if v else None,
            }
        )
    summary["elapsed_s"] = round(time.time() - t_start, 1)
    (args.out / "e2d-summary.json").write_text(
        json.dumps(summary, indent=2), encoding="utf-8"
    )
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
