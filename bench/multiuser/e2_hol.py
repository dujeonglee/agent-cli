#!/usr/bin/env python3
"""P1/N2 — HOL(head-of-line) 지연 실험: 3계약 × 선행 작업 길이 L 그리드.

포크 ``e2-hol.mjs`` 의 본류 재현(N2 교차 구현 검증). 시나리오는 동일하다:
사용자 A 가 L ms 걸리는 턴을 시작하고, 0.5s 뒤 사용자 B 가 짧은 질문을
던진다. **B 의 TTFT 가 L 에 얼마나 끌려가는가(회귀 기울기)** 가 핵심 지표 —
포크 실측: 직렬 1.000 / 거부+재시도 1.010 / 병렬 0.000.

A 의 "길이"는 순수 추론 스트리밍(토큰 수 × 간격)으로 만든다. 포크는 bash
sleep 도구를 썼지만, 직렬 계약의 HOL 은 "워커가 L 동안 점유된다"는 사실에서
오지 그 원인(도구/추론)과 무관하고, 순수 추론이면 mid-run 주입·효과 락 등
교란 변수가 없다. (도구 혼합의 효과는 P2 그리드가 별도로 잰다.)

측정은 전부 서버 내부 계측(turns.jsonl, M2) — 거부 계약의 재시도 대기는
reject 이벤트(첫 409 시각)부터 first_token 까지로 잡힌다.

사용: .venv/bin/python bench/multiuser/e2_hol.py [--reps 20] [--out bench/multiuser/out]
"""

from __future__ import annotations

import argparse
import json
import shutil
import tempfile
import time
from pathlib import Path

from driver import (
    AgentServer,
    MockLlm,
    median,
    percentile,
    slope,
    ttft_ms,
    turn_chain,
)

CONTRACTS = ("serial", "reject", "parallel")
#: L 그리드. 30 s 는 v0.8 에서 추가됐다 — 2~15 s 만으로도 기울기는 명확하지만,
#: "긴 작업"의 상한을 한 자릿수 초에 두면 직렬 계약의 비용이 실제 운용
#: (빌드·테스트·대규모 편집)보다 작게 읽힌다.
LEVELS_MS = (2000, 6000, 15000, 30000)
B_DELAY_S = 0.5  # A 제출 후 B 제출까지 (포크와 동일)
A_TOK_MS = 25  # A 스트리밍 토큰 간격 — n = L/25 개로 L ms 를 채운다
B_DIRECTIVE = "[[bench ttft=200 tok=5 n=8 id=b]]"


def run_cell(server: AgentServer, contract: str, level_ms: int, rep: int) -> dict:
    a_conn, b_conn = f"A-{level_ms}-{rep}", f"B-{level_ms}-{rep}"
    n_tokens = max(2, level_ms // A_TOK_MS)
    a_msg = f"long task [[bench ttft=200 tok={A_TOK_MS} n={n_tokens} id=a]]"
    b_msg = f"quick question {B_DIRECTIVE}"

    before = len(server.events())
    status = server.chat(a_msg, a_conn)
    assert status == 200, f"A submit failed: {status}"
    time.sleep(B_DELAY_S)
    retries = 0
    if contract == "reject":
        retries = server.chat_retry(b_msg, b_conn)
    else:
        status = server.chat(b_msg, b_conn)
        assert status == 200, f"B submit failed: {status}"

    events = server.wait_completes_since(before, 2)
    chain_b = turn_chain(events, b_conn)
    ttft = ttft_ms(chain_b)
    wall = (
        chain_b["complete"] - chain_b["enqueue"]
        if chain_b["complete"] is not None and chain_b["enqueue"] is not None
        else None
    )
    return {
        "condition": contract,
        "L": level_ms,
        "rep": rep,
        "bTtftMs": round(ttft, 1) if ttft is not None else None,
        "retries": retries,
        "wallMs": round(wall, 1) if wall is not None else None,
        "ok": ttft is not None,
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--reps", type=int, default=20)
    ap.add_argument("--out", type=Path, default=Path(__file__).parent / "out")
    ap.add_argument("--levels", type=int, nargs="*", default=list(LEVELS_MS))
    ap.add_argument(
        "--append",
        action="store_true",
        help="이미 커밋된 e2-hol.jsonl 에 이번 셀을 **합쳐서** 저장하고, 요약은 "
        "합집합 전체에서 재도출한다. 새 L 하나를 추가하려고 30분짜리 전체 "
        "그리드를 다시 돌리지 않기 위한 것 — 요약이 언제나 원시 파일에서 "
        "재도출된다는 규약은 그대로다.",
    )
    args = ap.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)

    mock = MockLlm()
    rows: list[dict] = []
    t_start = time.time()
    try:
        for contract in CONTRACTS:
            for level in args.levels:
                ws = Path(tempfile.mkdtemp(prefix=f"e2hol-{contract}-{level}-"))
                server = AgentServer(ws, mock.port, contract=contract, max_turns=4)
                try:
                    for rep in range(1, args.reps + 1):
                        row = run_cell(server, contract, level, rep)
                        rows.append(row)
                        print(json.dumps(row), flush=True)
                finally:
                    server.stop()
                    shutil.rmtree(ws, ignore_errors=True)
    finally:
        mock.stop()

    raw_path = args.out / "e2-hol.jsonl"
    if args.append and raw_path.is_file():
        # 같은 (계약, L, rep) 는 이번 실행 것이 이긴다 — 재실행이 곧 갱신.
        prior = [
            json.loads(x)
            for x in raw_path.read_text(encoding="utf-8").splitlines()
            if x.strip()
        ]
        merged = {(r["condition"], r["L"], r["rep"]): r for r in prior}
        merged.update({(r["condition"], r["L"], r["rep"]): r for r in rows})
        rows = [merged[k] for k in sorted(merged, key=lambda k: (k[0], k[1], k[2]))]
    raw_path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")

    # 요약의 L 목록은 인자가 아니라 **원시 데이터에 실제로 있는 L** 이다 —
    # --append 로 한 셀만 돌렸을 때 요약이 그 셀만 담으면 안 된다.
    levels = sorted({r["L"] for r in rows})
    summary = {"levels": levels, "reps": args.reps, "summary": [], "slope": {}}
    for contract in CONTRACTS:
        pts = []
        for level in levels:
            vals = [
                r["bTtftMs"]
                for r in rows
                if r["condition"] == contract and r["L"] == level and r["ok"]
            ]
            summary["summary"].append(
                {
                    "condition": contract,
                    "L": level,
                    "n": len(vals),
                    "p50": round(median(vals), 1) if vals else None,
                    "p95": round(percentile(vals, 0.95), 1) if vals else None,
                }
            )
            if vals:
                pts.append((float(level), median(vals)))
        summary["slope"][contract] = round(slope(pts), 4)
    summary["elapsed_s"] = round(time.time() - t_start, 1)
    (args.out / "e2-summary.json").write_text(
        json.dumps(summary, indent=2), encoding="utf-8"
    )
    print(json.dumps(summary["slope"], indent=2))


if __name__ == "__main__":
    main()
