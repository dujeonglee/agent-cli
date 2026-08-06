#!/usr/bin/env python3
"""P6 — 실 LLM 검증 + 토큰 비용: 모의 결과의 외적 타당성 + 한계 (1) 정량화.

두 팔 모두 온프렘 실 LLM(AGENT_CLI_BASE_URL/KEY/MODEL env)을 향한다.
모의와 달리 지연·응답 길이가 요동하므로 여기서 보는 것은 **순위 보존**과
**총 토큰 계정**이지 절대값이 아니다(§6 "모의/실측 이중 보고" 원칙).

  (a) HOL 스팟체크: A 가 긴 생성 턴을 시작하고 2s 뒤 B 가 한 줄 질문 —
      직렬 vs 병렬에서 B 의 TTFT. 가설: 병렬 ≪ 직렬 (순위 보존).
  (b) 토큰 비용: 같은 3-메시지 워크로드를 직렬/병렬로 처리했을 때
      llm_call usage 합계(입력/출력). N-병렬의 "N배 비용" 주장의 실측 —
      공유 트랜스크립트에서는 직렬도 뒤 턴이 앞 턴의 산출을 입력으로
      읽으므로, 차이는 배수가 아니라 스냅샷 겹침의 상수 오버헤드에
      가깝다는 것이 가설.

사용: AGENT_CLI_* env 를 설정하고
  .venv/bin/python bench/multiuser/p6_real_llm.py [--reps 5] [--cost-reps 3]
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

from driver import AgentServer, median, ttft_ms, turn_chain

LONG_TASK = (
    "Count from 1 to 120, one number per line, in plain text. "
    "Do not use any tools. When finished call complete with result 'counted'."
)
SHORT_TASK = "Reply with just the word pong. Then call complete with result 'pong'."
COST_TASKS = [
    "In one sentence, explain what a mutex is. Then call complete.",
    "In one sentence, explain what a semaphore is. Then call complete.",
    "In one sentence, explain what a spinlock is. Then call complete.",
]


def real_llm_from_env() -> dict:
    try:
        return {
            "base_url": os.environ["AGENT_CLI_BASE_URL"],
            "api_key": os.environ["AGENT_CLI_API_KEY"],
            "model": os.environ["AGENT_CLI_MODEL"],
        }
    except KeyError as e:
        sys.exit(f"missing env {e} — set AGENT_CLI_BASE_URL/API_KEY/MODEL")


def hol_arm(llm: dict, contract: str, reps: int) -> dict:
    ttfts = []
    for rep in range(1, reps + 1):
        ws = Path(tempfile.mkdtemp(prefix=f"p6hol-{contract}-{rep}-"))
        server = AgentServer(ws, None, contract=contract, max_turns=2, real_llm=llm)
        try:
            before = len(server.events())
            assert server.chat(LONG_TASK, f"A-{rep}") == 200
            time.sleep(2.0)
            assert server.chat(SHORT_TASK, f"B-{rep}") == 200
            events = server.wait_completes_since(before, 2, timeout=600)
            t = ttft_ms(turn_chain(events, f"B-{rep}"))
            if t is not None:
                ttfts.append(round(t, 1))
            print(
                json.dumps(
                    {"arm": contract, "rep": rep, "bTtftMs": ttfts[-1] if t else None}
                ),
                flush=True,
            )
        finally:
            server.stop()
            shutil.rmtree(ws, ignore_errors=True)
    return {
        "contract": contract,
        "n": len(ttfts),
        "bTtft_p50": round(median(ttfts), 1) if ttfts else None,
        "bTtft_all": ttfts,
    }


def cost_arm(llm: dict, contract: str, reps: int) -> dict:
    totals = []
    for rep in range(1, reps + 1):
        ws = Path(tempfile.mkdtemp(prefix=f"p6cost-{contract}-{rep}-"))
        server = AgentServer(ws, None, contract=contract, max_turns=3, real_llm=llm)
        try:
            before = len(server.events())
            for u, task in enumerate(COST_TASKS):
                assert server.chat(task, f"user{u}") == 200
            # 직렬 계약은 mid-run 주입으로 뒤 메시지가 앞 런에 흡수될 수
            # 있어(worker complete < 메시지 수) 정지 판정으로 기다린다 —
            # 그것이 곧 직렬 계약의 실제 처리 방식이며 비용 계정에는 그
            # 방식 그대로가 옳다.
            events = server.wait_quiescent(min_completes=1, timeout=900)
            calls = [
                e
                for e in events[before:]
                if e.get("event") == "llm_call" and "depth" not in e
            ]
            totals.append(
                {
                    "rep": rep,
                    "llm_calls": len(calls),
                    "input_tokens": sum(e.get("input_tokens", 0) for e in calls),
                    "output_tokens": sum(e.get("output_tokens", 0) for e in calls),
                }
            )
            print(json.dumps({"arm": contract, **totals[-1]}), flush=True)
        finally:
            server.stop()
            shutil.rmtree(ws, ignore_errors=True)
    return {
        "contract": contract,
        "n": len(totals),
        "input_tokens_p50": median([t["input_tokens"] for t in totals]),
        "output_tokens_p50": median([t["output_tokens"] for t in totals]),
        "llm_calls_p50": median([t["llm_calls"] for t in totals]),
        "reps": totals,
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--reps", type=int, default=5, help="HOL 스팟 반복")
    ap.add_argument("--cost-reps", type=int, default=3, help="토큰 비용 반복")
    ap.add_argument("--out", type=Path, default=Path(__file__).parent / "out")
    args = ap.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    llm = real_llm_from_env()

    out_path = args.out / "p6-real-llm.json"
    if args.reps > 0:
        hol = [hol_arm(llm, c, args.reps) for c in ("serial", "parallel")]
    else:
        # --reps 0: HOL 팔 생략 — 기존 결과 재사용 (부분 재실행용).
        hol = json.loads(out_path.read_text(encoding="utf-8"))["hol_spot"]
    cost = [cost_arm(llm, c, args.cost_reps) for c in ("serial", "parallel")]
    result = {"model": llm["model"], "hol_spot": hol, "token_cost": cost}
    print(json.dumps(result, indent=2, ensure_ascii=False))
    (args.out / "p6-real-llm.json").write_text(
        json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8"
    )


if __name__ == "__main__":
    main()
