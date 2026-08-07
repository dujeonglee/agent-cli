#!/usr/bin/env python3
"""E2b — 직렬 계약의 진짜 상한: 경계 밀도(boundary density) 축.

§6.1 의 HOL 그리드는 A 의 작업을 **도구 스텝 0 인 단일 생성**으로 만든다.
그래서 직렬 계약의 실행 중 주입(mid-run injection, `loop/core.py`
``_inject_queued_messages``)은 켜져 있어도 발동할 턴 경계가 없고, B 는 A 의
실행 전체를 기다린다. 즉 그 그리드는 직렬을 **가장 취약한 지점**에서 잰다.

이 실험은 그 그리드가 고정한 축을 푼다: **총 길이 L 을 고정한 채 턴 경계
수 k 를 바꾼다.** 예측은 "직렬의 노출은 L 이 아니라 경계 간격 L/k 가
좌우한다" 이고, k=1 셀은 §6.1 의 L=15s 직렬 셀을 재현해야 한다(정합성 체크).

**지표: time-to-inclusion.** B 의 큐 진입부터 B 의 질문이 실제로 모델 콜의
프롬프트에 들어간 순간(``query_added``)까지. 왜 TTFT 가 아닌가 — 계측의
``first_token`` 은 run_loop 당 1회 래치라(``loop/llm.py``) 주입된 메시지에는
귀속 first_token 이 없다. 주입은 턴 경계 직후 곧바로 다음 콜을 띄우므로
B 의 TTFT = inclusion + 콜 1회의 TTFT(여기서는 스크립트된 ttft) 다.
k=1 에서는 B 가 자기 run 을 얻으므로 기존 ``ttft_ms`` 도 함께 뽑아
inclusion + 상수 ≈ TTFT 를 교차 검증한다.

**해석의 한계(정직하게).** 목 모델은 주입된 B 의 질문에 *답하지* 않고 A 의
스크립트를 계속 흘린다. 따라서 inclusion 은 "B 의 질문이 반영된 첫 콜이
시작된 시각"이지 "B 가 답을 받기 시작한 시각"이 아니다. 이 수치는 직렬에
**유리한 하한**이며, 그래도 결론은 바뀌지 않는다: 그 하한조차 L 이 아니라
L/k 를 따른다.

B 의 메시지에는 ``[[bench]]`` 지시자를 넣지 않는다 — 넣으면 공유
트랜스크립트에서 A 의 후속 콜이 B 의 지시자로 붕괴한다(§5 의 그 함정).

사용: .venv/bin/python bench/multiuser/e2b_injection.py [--reps 10]
"""

from __future__ import annotations

import argparse
import json
import shutil
import tempfile
import time
from pathlib import Path

from driver import AgentServer, MockLlm, median, percentile, ttft_ms, turn_chain

#: 총 작업 길이 — §6.1 의 L=15s 셀과 맞춘다(k=1 재현 대조를 위해).
LEVEL_MS = 15000
#: 턴 경계 수 = LLM 콜 수. fwrite=k-1 개의 쓰기 스텝 + complete 1회.
K_VALUES = (1, 2, 4, 8)
B_DELAY_S = 0.5  # §6.1 과 동일
TTFT_MS = 200  # 콜당 첫 토큰 지연 (스크립트)
#: §6.1 과 같은 토큰 간격. 5ms 로 두면 같은 L 을 채우는 데 청크가 5배로
#: 늘어 청크당 오버헤드가 누적되고(실측 L=15s 에서 +1.1s), k=1 셀이 §6.1
#: 직렬 셀을 재현하는지 보는 정합성 체크가 흐려진다.
TOK_MS = 25


def a_directive(k: int) -> str:
    """총 L 을 k 개의 콜에 균등 배분. 콜 1회 = ttft + n×tok."""
    per_call_ms = LEVEL_MS / k
    n = max(2, round((per_call_ms - TTFT_MS) / TOK_MS))
    return (
        f"[[bench ttft={TTFT_MS} tok={TOK_MS} n={n} fwrite={k - 1} fpath=a.txt id=a]]"
    )


def _mono_events(server: AgentServer) -> list[dict]:
    """turns.jsonl 에는 TurnRecord 행(계측 아님)도 섞여 있다 — mono_ms 가
    있는 계측 행만."""
    return [e for e in server.events() if "mono_ms" in e]


def _b_inclusion(events: list[dict], b_conn: str) -> tuple[float | None, float | None]:
    """(B 의 enqueue mono, B 질문의 query_added mono).

    B 의 query_added 는 "B 의 enqueue 이후 처음 나오는 query_added" 다 —
    A 의 것(u1)은 실행 시작 시각에 이미 찍혔다.
    """
    enq = None
    for e in events:
        if (
            e.get("event") == "turn"
            and e.get("phase") == "enqueue"
            and e.get("conn_id") == b_conn
        ):
            enq = e["mono_ms"]
            break
    if enq is None:
        return None, None
    for e in events:
        if (
            e.get("event") == "turn"
            and e.get("phase") == "query_added"
            and e["mono_ms"] > enq
        ):
            return enq, e["mono_ms"]
    return enq, None


def run_cell(server: AgentServer, k: int, rep: int, timeout: float = 90.0) -> dict:
    a_conn, b_conn = f"A-{k}-{rep}", f"B-{k}-{rep}"
    assert server.chat(f"long task {a_directive(k)}", a_conn) == 200
    time.sleep(B_DELAY_S)
    assert server.chat("quick question", b_conn) == 200

    # 종료 판정: B 의 질문이 컨텍스트에 들어간 뒤, 그보다 나중의 complete 가
    # 한 번 찍히면 끝. k=1 은 B 가 자기 run 을 갖고(2 completes), k≥2 는 A 의
    # run 하나에 접혀 들어가므로(1 complete) 고정 개수로는 못 센다.
    deadline = time.monotonic() + timeout
    evs: list[dict] = []
    inc = None
    while time.monotonic() < deadline:
        evs = _mono_events(server)
        _, inc = _b_inclusion(evs, b_conn)
        if inc is not None and any(
            e.get("event") == "turn"
            and e.get("phase") == "complete"
            and e["mono_ms"] > inc
            for e in evs
        ):
            break
        time.sleep(0.1)

    enq, inc = _b_inclusion(evs, b_conn)
    chain_b = turn_chain(evs, b_conn)
    ttft = ttft_ms(chain_b)
    # 주입 발동 여부: B 가 자기 dispatch 를 받았으면 주입이 아니라 새 run.
    injected = inc is not None and chain_b["dispatch"] is None
    return {
        "k": k,
        "rep": rep,
        "L": LEVEL_MS,
        "boundaryIntervalMs": round(LEVEL_MS / k, 1),
        "inclusionMs": round(inc - enq, 1) if (inc and enq) else None,
        "ttftMs": round(ttft, 1) if ttft is not None else None,
        "injected": injected,
        "ok": inc is not None and enq is not None,
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--reps", type=int, default=10)
    ap.add_argument("--out", type=Path, default=Path(__file__).parent / "out")
    ap.add_argument("--ks", type=int, nargs="*", default=list(K_VALUES))
    args = ap.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)

    mock = MockLlm()
    rows: list[dict] = []
    t_start = time.time()
    try:
        for k in args.ks:
            ws = Path(tempfile.mkdtemp(prefix=f"e2b-k{k}-"))
            # max_turns 는 k 보다 커야 한다 — 작으면 A 의 실행이 잘려
            # 경계 수가 의도와 달라진다.
            server = AgentServer(ws, mock.port, contract="serial", max_turns=k + 4)
            try:
                for rep in range(1, args.reps + 1):
                    row = run_cell(server, k, rep)
                    rows.append(row)
                    print(json.dumps(row), flush=True)
            finally:
                server.stop()
                shutil.rmtree(ws, ignore_errors=True)
    finally:
        mock.stop()

    (args.out / "e2b-injection.jsonl").write_text(
        "".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8"
    )

    summary = {"L_ms": LEVEL_MS, "reps": args.reps, "cells": []}
    for k in sorted({r["k"] for r in rows}):
        vals = [r["inclusionMs"] for r in rows if r["k"] == k and r["ok"]]
        tt = [r["ttftMs"] for r in rows if r["k"] == k and r["ttftMs"] is not None]
        summary["cells"].append(
            {
                "k": k,
                "boundaryIntervalMs": round(LEVEL_MS / k, 1),
                "n": len(vals),
                "inclusionP50": round(median(vals), 1) if vals else None,
                "inclusionP95": round(percentile(vals, 0.95), 1) if vals else None,
                "ttftP50": round(median(tt), 1) if tt else None,
                "injectedRate": round(
                    sum(1 for r in rows if r["k"] == k and r["injected"])
                    / max(1, sum(1 for r in rows if r["k"] == k)),
                    3,
                ),
            }
        )
    summary["elapsed_s"] = round(time.time() - t_start, 1)
    (args.out / "e2b-summary.json").write_text(
        json.dumps(summary, indent=2), encoding="utf-8"
    )
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
