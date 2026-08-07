#!/usr/bin/env python3
"""P2-SHELL-REAL — 셸이 지배하는 워크로드의 실제 효과 시간 비중.

§6.4 의 실모델 팔은 **파일 쓰기** 워크로드에서 효과 비중 10⁻⁵ 를 재고,
"실제 작업은 붕괴 지점(50%)보다 네 자릿수 아래에 앉는다"고 결론지었다.
그 결론에는 정당한 반론이 있다: 코딩 에이전트의 지배적 효과는 파일 쓰기가
아니라 **셸**(빌드·테스트)이고, §4.4 의 호환성 행렬에서 셸은 **배타**다
(파일 발자국을 정적으로 알 수 없으므로). 셸이 턴 시간의 큰 몫을 차지하면
운용점이 붕괴 지점 쪽으로 이동할 수 있다.

이 스크립트가 그 두 번째 운용점을 찍는다. 구성은 §6.4 의 실모델 팔과
동형이되 과제만 바꾼다: 두 사용자가 각자 **셸 명령을 여러 번** 돌린다.
지표도 동일하다 — 락 이벤트의 ``thread`` 로 보유·대기를 턴에 귀속시키고

  effect_share = Σ held_ms / (구간 − Σ wait_ms)
  effective_parallelism = (work_A + work_B) / makespan

셸 명령의 길이(``--sleep-ms``)가 축이다. 짧으면 파일 쓰기와 비슷한 자리,
길면 배타 구간이 길어져 비중이 올라간다. 예측은 §6.3 의 법칙 그대로
2/max(1,2s) 이며, 여기서 확인하려는 것은 **실제 코딩 워크로드가 그 곡선
위 어디로 이동하는가**이지 법칙 자체가 아니다.

사용: AGENT_CLI_BASE_URL/API_KEY/MODEL 설정 후
  .venv/bin/python bench/multiuser/p2_shell_real.py [--reps 4] [--sleep-ms 3000]
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
from pathlib import Path

from driver import AgentServer, turn_chain

SHELL_CALLS = 3


def task_for(tag: str, sleep_ms: int) -> str:
    sec = sleep_ms / 1000.0
    return (
        f"Use the shell tool exactly {SHELL_CALLS} times. Each time run the "
        f"command: sleep {sec:g} && echo {tag}-done. "
        "Do not write or read any file. "
        f"After the {SHELL_CALLS}th shell call, call complete with result "
        f"'{tag} done'."
    )


def real_llm_from_env() -> dict:
    try:
        return {
            "base_url": os.environ["AGENT_CLI_BASE_URL"],
            "api_key": os.environ["AGENT_CLI_API_KEY"],
            "model": os.environ["AGENT_CLI_MODEL"],
        }
    except KeyError as e:
        sys.exit(f"missing env {e} — set AGENT_CLI_BASE_URL/API_KEY/MODEL")


def lock_totals(events: list[dict], offset: int) -> dict[str, dict]:
    per: dict[str, dict] = {}
    for e in events[offset:]:
        if e.get("event") != "lock":
            continue
        thread = str(e.get("thread", ""))
        if not thread.startswith("agent-turn-"):
            continue
        turn_id = thread.removeprefix("agent-turn-")
        slot = per.setdefault(turn_id, {"wait_ms": 0.0, "held_ms": 0.0, "n": 0})
        if e.get("phase") == "acquire":
            slot["wait_ms"] += float(e.get("wait_ms") or 0.0)
            slot["n"] += 1
        elif e.get("phase") == "release":
            slot["held_ms"] += float(e.get("held_ms") or 0.0)
    return per


def run_rep(llm: dict, sleep_ms: int, rep: int) -> dict | None:
    ws = Path(tempfile.mkdtemp(prefix=f"p2sh-{sleep_ms}-{rep}-"))
    # 셸은 어느 스코프에서도 배타이므로 스코프 축은 의미가 없다 —
    # 출하 기본값(conflict)으로 고정한다.
    server = AgentServer(
        ws, None, contract="parallel", lock_scope="conflict", max_turns=2, real_llm=llm
    )
    a_conn, b_conn = f"A-{rep}", f"B-{rep}"
    try:
        before = len(server.events())
        results: dict[str, int] = {}
        gate = threading.Barrier(2)

        def submit(conn: str, tag: str) -> None:
            gate.wait()
            results[conn] = server.chat(task_for(tag, sleep_ms), conn)

        threads = [
            threading.Thread(target=submit, args=(a_conn, "AAA")),
            threading.Thread(target=submit, args=(b_conn, "BBB")),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        if set(results.values()) != {200}:
            return None
        events = server.wait_completes_since(before, 2, timeout=1200)
        ca, cb = turn_chain(events, a_conn), turn_chain(events, b_conn)
        if None in (ca["dispatch"], ca["complete"], cb["dispatch"], cb["complete"]):
            return None
        locks = lock_totals(events, before)
        la = locks.get(str(ca["turn_id"]), {"wait_ms": 0.0, "held_ms": 0.0, "n": 0})
        lb = locks.get(str(cb["turn_id"]), {"wait_ms": 0.0, "held_ms": 0.0, "n": 0})
        span_a = ca["complete"] - ca["dispatch"]
        span_b = cb["complete"] - cb["dispatch"]
        work_a = span_a - la["wait_ms"]
        work_b = span_b - lb["wait_ms"]
        makespan = max(ca["complete"], cb["complete"]) - min(
            ca["dispatch"], cb["dispatch"]
        )
        held = la["held_ms"] + lb["held_ms"]
        return {
            "sleep_ms": sleep_ms,
            "rep": rep,
            "spanA_ms": round(span_a, 1),
            "spanB_ms": round(span_b, 1),
            "makespan_ms": round(makespan, 1),
            "lock_acquisitions": la["n"] + lb["n"],
            "lock_wait_ms": round(la["wait_ms"] + lb["wait_ms"], 1),
            "lock_held_ms": round(held, 1),
            "effect_share_measured": round(held / (work_a + work_b), 5)
            if (work_a + work_b)
            else None,
            "effective_parallelism": round((work_a + work_b) / makespan, 3)
            if makespan
            else None,
        }
    finally:
        server.stop()
        shutil.rmtree(ws, ignore_errors=True)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--reps", type=int, default=4)
    ap.add_argument("--sleep-ms", type=int, nargs="*", default=[1000, 5000])
    ap.add_argument("--out", type=Path, default=Path(__file__).parent / "out")
    ap.add_argument("--rederive", action="store_true")
    args = ap.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    raw_path = args.out / "p2-shell-real.jsonl"

    if args.rederive:
        rows = [
            json.loads(x)
            for x in raw_path.read_text(encoding="utf-8").splitlines()
            if x.strip()
        ]
    else:
        llm = real_llm_from_env()
        rows = []
        for sleep_ms in args.sleep_ms:
            for rep in range(1, args.reps + 1):
                row = run_rep(llm, sleep_ms, rep)
                if row is None:
                    continue
                rows.append(row)
                print(json.dumps(row), flush=True)
        raw_path.write_text(
            "".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8"
        )

    cells = []
    for sleep_ms in sorted({r["sleep_ms"] for r in rows}):
        sub = [r for r in rows if r["sleep_ms"] == sleep_ms]
        shares = [r["effect_share_measured"] for r in sub if r["effect_share_measured"]]
        pars = [r["effective_parallelism"] for r in sub if r["effective_parallelism"]]
        s = statistics.median(shares) if shares else None
        cells.append(
            {
                "shellSleepMs": sleep_ms,
                "shellCallsPerTurn": SHELL_CALLS,
                "n": len(sub),
                "effectShareP50": round(s, 5) if s else None,
                # §6.3 의 법칙: 두 턴에서 효과 비중 s 일 때 상한 2/max(1,2s).
                "predictedCeiling": round(2 / max(1.0, 2 * s), 3) if s else None,
                "effectiveParallelismP50": round(statistics.median(pars), 3)
                if pars
                else None,
                "lockWaitP50Ms": round(
                    statistics.median(r["lock_wait_ms"] for r in sub), 1
                ),
                "spanP50Ms": round(statistics.median(r["spanA_ms"] for r in sub), 1),
            }
        )
    summary = {
        "shellCallsPerTurn": SHELL_CALLS,
        "cells": cells,
        "note": (
            "The file-write operating point of §6.4 is 1e-5. This asks where a "
            "shell-dominated workload sits instead, because shell is exclusive "
            "under the compatibility matrix and is what build/test actually "
            "use. predictedCeiling applies the §6.3 law 2/max(1,2s) to the "
            "measured share, so the gap between it and effectiveParallelismP50 "
            "is what the lock costs beyond the law."
        ),
    }
    (args.out / "p2-shell-real.json").write_text(
        json.dumps(summary, indent=2), encoding="utf-8"
    )
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
