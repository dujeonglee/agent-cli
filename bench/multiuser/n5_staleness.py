#!/usr/bin/env python3
"""N5 — 스냅샷 staleness 실측: §4.3 의 "유계 staleness" 공개를 수치로 바꾼다.

§4.3 의 계약은 스냅샷 읽기 + 완료순 원자 커밋이고, 알려진 비용은 staleness 다:
턴의 프롬프트(스냅샷)가 찍힌 뒤 그 스텝이 커밋되기 전에 다른 턴이 커밋한
블록은 이 턴의 프롬프트에 없다. 논문은 지금까지 이를 공개만 하고(한계 5)
측정하지 않았다 — 이 실험이 그 빈도와 깊이를 잰다.

데이터 소스는 v7.30.2 의 ``ctx`` 계측 이벤트다. ContextManager 는 변형
카운터 ``_commit_seq`` 를 유지하고, ``get_messages``(스냅샷)가 읽은 시점 값을,
``add``/``commit_atomic``(변형)이 커밋 시점 값을 turns.jsonl 에 남긴다. 스레드
하나 = 턴 하나(A1)이므로 스텝의 staleness 는 파일 조인 없이 산술로 나온다::

    stale(스텝) = (그 스텝 변형의 seq − 1) − (같은 스레드의 직전 스냅샷 seq)

짝짓기 규칙: 스레드별로 시간순 순회하며 마지막 스냅샷을 기억하고, 변형
이벤트를 만나면 그 스냅샷과 짝지은 뒤 스냅샷을 소모한다(스냅샷 하나 =
스텝 하나). 턴 자신의 질의 append 는 첫 스냅샷 이전이라 자연히 제외된다.

팔:
  parallel — 측정 대상. 동시 턴이 서로의 스냅샷-커밋 창에 커밋을 떨어뜨린다.
  serial   — 음성 대조군. 턴이 하나씩 돌므로 stale 은 구성상 0 이어야 하고,
             0 이 아니면 지표 자체가 잘못된 것이다.

겹침 증거: turn dispatch/complete 이벤트로 최대 동시 턴 수를 함께 보고한다 —
parallel 팔의 stale 이 낮게 나올 때 "겹치지 않아서"인지 구별하기 위해서다.

사용: .venv/bin/python bench/multiuser/n5_staleness.py [--users 4] [--rounds 25]
      [--reps 5] [--serial-reps 2]
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import statistics
import tempfile
import time
from pathlib import Path

from driver import AgentServer, MockLlm

TURN_THREAD_RE = re.compile(r"agent-turn-t\d+$")


def stale_pairs(events: list[dict], *, turn_threads_only: bool) -> dict[str, list[int]]:
    """스레드별 (스냅샷, 다음 변형) 짝의 stale 값 목록."""
    by_thread: dict[str, list[dict]] = {}
    for e in events:
        if e.get("event") != "ctx":
            continue
        by_thread.setdefault(e.get("thread", "?"), []).append(e)
    out: dict[str, list[int]] = {}
    for thread, evs in by_thread.items():
        if turn_threads_only and not TURN_THREAD_RE.search(thread):
            continue
        evs.sort(key=lambda e: e["mono_ms"])
        pairs: list[int] = []
        last_snap: dict | None = None
        for e in evs:
            if e["phase"] == "snapshot":
                last_snap = e
            elif e["phase"] in ("append", "commit") and last_snap is not None:
                pairs.append(e["seq"] - 1 - last_snap["seq"])
                last_snap = None
        if pairs:
            out[thread] = pairs
    return out


def peak_concurrency(events: list[dict]) -> int:
    marks = [
        (e["mono_ms"], 1 if e["phase"] == "dispatch" else -1)
        for e in events
        if e.get("event") == "turn" and e.get("phase") in ("dispatch", "complete")
    ]
    n = peak = 0
    for _, d in sorted(marks):
        n += d
        peak = max(peak, n)
    return peak


def run_rep(arm: str, users: int, rounds: int) -> dict:
    mock = MockLlm()
    ws = Path(tempfile.mkdtemp(prefix=f"n5-{arm}-"))
    server = AgentServer(ws, mock.port, contract=arm, max_turns=users)
    total = users * rounds
    try:
        for r in range(rounds):
            for u in range(users):
                marker = f"m{u}-{r}"
                msg = f"question {marker} [[bench ttft=30 tok=1 n=6 id={marker}]]"
                assert server.chat(msg, f"user{u}") == 200
            time.sleep(0.05)
        if arm == "parallel":
            events = server.wait_completes(total, timeout=600)
        else:
            # 직렬은 mid-run 주입이 뒤 메시지를 앞 런에 흡수해 complete 수가
            # 메시지 수보다 적을 수 있다 — 정지 판정으로 대기 (driver 규약).
            events = server.wait_quiescent(min_completes=1, timeout=900)
    finally:
        server.stop()
        mock.stop()
        shutil.rmtree(ws, ignore_errors=True)

    pairs = stale_pairs(events, turn_threads_only=(arm == "parallel"))
    flat = [v for vs in pairs.values() for v in vs]
    stale = [v for v in flat if v > 0]
    return {
        "arm": arm,
        "turn_threads": len(pairs),
        "steps": len(flat),
        "stale_steps": len(stale),
        "stale_share": round(len(stale) / len(flat), 4) if flat else None,
        "stale_depth_p50": statistics.median(stale) if stale else 0,
        "stale_depth_max": max(stale) if stale else 0,
        "peak_concurrency": peak_concurrency(events),
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--users", type=int, default=4)
    ap.add_argument("--rounds", type=int, default=25)
    ap.add_argument("--reps", type=int, default=5)
    ap.add_argument("--serial-reps", type=int, default=2)
    ap.add_argument("--out", type=Path, default=Path(__file__).parent / "out")
    args = ap.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)

    reps: list[dict] = []
    for arm, n in (("parallel", args.reps), ("serial", args.serial_reps)):
        for rep in range(n):
            row = run_rep(arm, args.users, args.rounds)
            row["rep"] = rep
            reps.append(row)
            print(json.dumps(row, ensure_ascii=False))

    par = [r for r in reps if r["arm"] == "parallel"]
    ser = [r for r in reps if r["arm"] == "serial"]
    summary = {
        "config": {"users": args.users, "rounds": args.rounds},
        "parallel": {
            "reps": len(par),
            "stale_share_min": min(r["stale_share"] for r in par),
            "stale_share_median": statistics.median(r["stale_share"] for r in par),
            "stale_share_max": max(r["stale_share"] for r in par),
            "stale_depth_max": max(r["stale_depth_max"] for r in par),
            "peak_concurrency_min": min(r["peak_concurrency"] for r in par),
        },
        "serial_control": {
            "reps": len(ser),
            "stale_steps_total": sum(r["stale_steps"] for r in ser),
            "steps_total": sum(r["steps"] for r in ser),
        },
        "reps": reps,
    }
    out_path = args.out / "n5-staleness.json"
    out_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False))
    print(f"\nwrote {out_path}")
    print(json.dumps({k: v for k, v in summary.items() if k != "reps"}, indent=2))


if __name__ == "__main__":
    main()
