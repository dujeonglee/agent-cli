#!/usr/bin/env python3
"""E2c — 거부 계약의 위상 벌점은 재시도 간격의 함수인가.

§6.1 은 거부-후-재시도를 250 ms 간격 하나로만 쟀고, 벌점(+79~232 ms)을
"간격 1개분의 위상 의존 분수"로 해석했다. 그 해석이 맞다면 벌점은 간격에
비례해야 한다 — 간격을 4배로 늘리면 벌점도 대략 4배가 되고, 상한은 여전히
간격 하나다. 이 실험은 그 예측을 직접 확인해서 "거부는 직렬보다 나은 적이
없다"는 주장이 특정 간격 값에 기대고 있지 않음을 보인다.

L 은 §6.1 의 15 s 셀로 고정하고 간격만 바꾼다. 대조군으로 같은 세션에서
직렬 팔도 함께 재서, 벌점을 (거부 − 직렬) 로 **같은 측정 세션 안에서**
계산한다 — §6 의 고정 오버헤드 드리프트가 상쇄된다.

사용: .venv/bin/python bench/multiuser/e2c_retry.py [--reps 10]
"""

from __future__ import annotations

import argparse
import json
import shutil
import tempfile
import time
from pathlib import Path

from driver import AgentServer, MockLlm, median, percentile, ttft_ms, turn_chain

LEVEL_MS = 15000
A_TOK_MS = 25
B_DIRECTIVE = "[[bench ttft=200 tok=5 n=8 id=b]]"
B_DELAY_S = 0.5
#: 재시도 간격(초). 250 ms 는 §6.1 이 쓴 값.
INTERVALS = (0.25, 1.0)


def run_cell(server: AgentServer, contract: str, interval: float, rep: int) -> dict:
    a_conn, b_conn = f"A-{rep}", f"B-{rep}"
    n_tokens = max(2, LEVEL_MS // A_TOK_MS)
    # 이번 rep 이 시작되기 전의 이벤트 수 — 0 으로 두면 이전 rep 의 complete 가
    # 이미 2 개 쌓여 있어 wait_completes_since 가 즉시 반환하고, 다음 rep 의
    # A 제출이 아직 도는 실행과 겹쳐 거부(409)를 받는다.
    before = len(server.events())
    assert (
        server.chat(
            f"long task [[bench ttft=200 tok={A_TOK_MS} n={n_tokens} id=a]]", a_conn
        )
        == 200
    )
    time.sleep(B_DELAY_S)
    retries = 0
    if contract == "reject":
        retries = server.chat_retry(
            f"quick question {B_DIRECTIVE}", b_conn, interval=interval
        )
    else:
        assert server.chat(f"quick question {B_DIRECTIVE}", b_conn) == 200

    events = server.wait_completes_since(before, 2)
    ttft = ttft_ms(turn_chain(events, b_conn))
    return {
        "condition": contract,
        "intervalMs": int(interval * 1000) if contract == "reject" else None,
        "L": LEVEL_MS,
        "rep": rep,
        "bTtftMs": round(ttft, 1) if ttft is not None else None,
        "retries": retries,
        "ok": ttft is not None,
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--reps", type=int, default=10)
    ap.add_argument("--out", type=Path, default=Path(__file__).parent / "out")
    args = ap.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)

    mock = MockLlm()
    rows: list[dict] = []
    t_start = time.time()
    # (계약, 간격) 조건 목록 — 직렬은 같은 세션의 기준선으로 한 번만.
    conditions: list[tuple[str, float]] = [("serial", 0.0)]
    conditions += [("reject", iv) for iv in INTERVALS]
    try:
        for contract, interval in conditions:
            ws = Path(
                tempfile.mkdtemp(prefix=f"e2c-{contract}-{int(interval * 1000)}-")
            )
            server = AgentServer(ws, mock.port, contract=contract, max_turns=4)
            try:
                for rep in range(1, args.reps + 1):
                    row = run_cell(server, contract, interval, rep)
                    rows.append(row)
                    print(json.dumps(row), flush=True)
            finally:
                server.stop()
                shutil.rmtree(ws, ignore_errors=True)
    finally:
        mock.stop()

    (args.out / "e2c-retry.jsonl").write_text(
        "".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8"
    )

    def cell(pred) -> dict:
        v = [r["bTtftMs"] for r in rows if pred(r) and r["ok"]]
        return {
            "n": len(v),
            "p50": round(median(v), 1) if v else None,
            "p95": round(percentile(v, 0.95), 1) if v else None,
        }

    serial = cell(lambda r: r["condition"] == "serial")
    summary = {
        "L_ms": LEVEL_MS,
        "reps": args.reps,
        "serial": serial,
        "reject": [],
    }
    for iv in INTERVALS:
        ms = int(iv * 1000)
        c = cell(lambda r, ms=ms: r["condition"] == "reject" and r["intervalMs"] == ms)
        c["intervalMs"] = ms
        c["penaltyMs"] = (
            round(c["p50"] - serial["p50"], 1)
            if c["p50"] is not None and serial["p50"] is not None
            else None
        )
        c["penaltyFracOfInterval"] = (
            round(c["penaltyMs"] / ms, 3) if c["penaltyMs"] is not None else None
        )
        c["retriesP50"] = round(
            median(
                [
                    r["retries"]
                    for r in rows
                    if r["condition"] == "reject" and r["intervalMs"] == ms
                ]
            ),
            1,
        )
        summary["reject"].append(c)
    summary["elapsed_s"] = round(time.time() - t_start, 1)
    (args.out / "e2c-summary.json").write_text(
        json.dumps(summary, indent=2), encoding="utf-8"
    )
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
