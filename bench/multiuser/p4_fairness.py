#!/usr/bin/env python3
"""P4 — 사용자별 공정성: per-user 1활성턴 게이트 on/off 대조.

시나리오(U2 워룸 프로파일): 플러더 1명이 턴 5건을 연속 적재한 직후 N 명이
각각 짧은 턴 1건을 보낸다.

  gate on  (기본): 플러더는 동시 1턴만 활성 — 나머지 슬롯을 단기 사용자들이
                   즉시 가져간다. 플러더의 백로그는 자기 비용.
  gate off (ablation, --no-per-user-gate): 순수 FIFO+cap — 플러더가 cap 을
                   독식해 단기 사용자들이 백로그 뒤에 줄을 선다.

지표: 단기 사용자 대기 p50/p95 (핵심 — 절대값 비교), 플러더 자기 대기,
전(全)사용자 Jain(참고용 — 대기가 ~0 인 팔에서는 노이즈 지배라 부차 지표),
per-user 동시 활성 위반 수(게이트 on 팔에서 0 이어야 함).

사용: .venv/bin/python bench/multiuser/p4_fairness.py [--users 4] [--cap 3]
"""

from __future__ import annotations

import argparse
import itertools
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

from driver import AgentServer, MockLlm, median, percentile


def real_llm_from_env() -> dict:
    try:
        return {
            "base_url": os.environ["AGENT_CLI_BASE_URL"],
            "api_key": os.environ["AGENT_CLI_API_KEY"],
            "model": os.environ["AGENT_CLI_MODEL"],
        }
    except KeyError as e:
        sys.exit(f"missing env {e} — set AGENT_CLI_BASE_URL/API_KEY/MODEL")


def jain(xs: list[float]) -> float:
    if not xs:
        return float("nan")
    s, s2 = sum(xs), sum(x * x for x in xs)
    return (s * s) / (len(xs) * s2) if s2 else 1.0


def long_task(i: int, llm: dict | None) -> str:
    """플러더의 긴 턴 — 목은 스크립트, 실모델은 실제로 오래 걸리는 작업."""
    if llm is None:
        return f"long {i} [[bench ttft=100 tok=10 n=150 id=f{i}]]"
    return (
        f"Use the write_file tool three times: create long{i}_1.txt, "
        f"long{i}_2.txt and long{i}_3.txt, each with exactly 12 lines where "
        f"every line is 'long {i} line N' with N replaced by the line number. "
        "Do not read any file. Do not use the shell. "
        f"When all three files are written, call complete with result 'long {i} done'."
    )


def short_task(u: int, llm: dict | None) -> str:
    """단기 사용자의 짧은 턴 — 게이트가 지키려는 대상."""
    if llm is None:
        return f"quick {u} [[bench ttft=100 tok=2 n=8 id=s{u}]]"
    return f"Do not use any tool. Immediately call complete with result 'quick {u}'."


def run_arm(args, mock: MockLlm | None, gate: bool, llm: dict | None = None) -> dict:
    per_user_waits: dict[str, list[float]] = {}
    violations = 0
    timeout = 300 if llm is None else 2400
    for rep in range(1, args.reps + 1):
        ws = Path(tempfile.mkdtemp(prefix=f"p4-{'on' if gate else 'off'}-{rep}-"))
        server = AgentServer(
            ws,
            None if mock is None else mock.port,
            contract="parallel",
            max_turns=args.cap,
            extra=[] if gate else ["--no-per-user-gate"],
            real_llm=llm,
        )
        try:
            flooder = f"flood-{rep}"
            for i in range(args.flood):
                assert server.chat(long_task(i, llm), flooder) == 200
            time.sleep(0.15)
            for u in range(args.users):
                conn = f"short{u}-{rep}"
                assert server.chat(short_task(u, llm), conn) == 200
            events = server.wait_completes(args.flood + args.users, timeout=timeout)

            enq = {
                e["queue_id"]: e
                for e in events
                if e.get("phase") == "enqueue" and e.get("queue_id")
            }
            intervals: dict[str, list[tuple[float, float]]] = {}
            for e in events:
                if e.get("phase") != "dispatch":
                    continue
                q = enq.get(e.get("queue_id"))
                if q is None:
                    continue
                conn = q.get("conn_id", "")
                per_user_waits.setdefault(conn, []).append(e["mono_ms"] - q["mono_ms"])
                tid = e.get("turn_id")
                comp = next(
                    (
                        c["mono_ms"]
                        for c in events
                        if c.get("phase") == "complete" and c.get("turn_id") == tid
                    ),
                    None,
                )
                if comp is not None:
                    intervals.setdefault(conn, []).append((e["mono_ms"], comp))
            for spans in intervals.values():
                spans.sort()
                for (_s1, e1), (s2, _e2) in itertools.pairwise(spans):
                    if s2 < e1:
                        violations += 1
        finally:
            server.stop()
            shutil.rmtree(ws, ignore_errors=True)

    short_all = [
        w
        for conn, ws_ in per_user_waits.items()
        if conn.startswith("short")
        for w in ws_
    ]
    flood_all = [
        w
        for conn, ws_ in per_user_waits.items()
        if conn.startswith("flood")
        for w in ws_
    ]
    per_user_mean = [sum(v) / len(v) for v in per_user_waits.values() if v]
    return {
        "per_user_gate": gate,
        "jain_all_users": round(jain(per_user_mean), 4),
        "short_wait_p50": round(median(short_all), 1) if short_all else None,
        "short_wait_p95": round(percentile(short_all, 0.95), 1) if short_all else None,
        "flood_wait_p50": round(median(flood_all), 1) if flood_all else None,
        "flood_wait_max": round(max(flood_all), 1) if flood_all else None,
        "per_user_concurrency_violations": violations,
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--users", type=int, default=4, help="단기 사용자 수(플러더 제외)")
    ap.add_argument("--cap", type=int, default=3)
    ap.add_argument("--flood", type=int, default=5, help="플러더의 연속 적재 수")
    ap.add_argument("--reps", type=int, default=5)
    ap.add_argument(
        "--real",
        action="store_true",
        help="목 대신 실모델 — 플러더의 턴이 실제로 분 단위일 때 단기 사용자가 "
        "겪는 절대 대기를 잰다(배포에서 의미 있는 수).",
    )
    ap.add_argument("--out", type=Path, default=Path(__file__).parent / "out")
    args = ap.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    llm = real_llm_from_env() if args.real else None

    mock = MockLlm() if llm is None else None
    arms = {}
    try:
        for gate in (True, False):
            arms["gate_on" if gate else "gate_off"] = run_arm(args, mock, gate, llm)
    finally:
        if mock is not None:
            mock.stop()

    result = {
        "users_short": args.users,
        "cap": args.cap,
        "flood": args.flood,
        "reps": args.reps,
        "model": "mock" if llm is None else llm["model"],
        "arms": arms,
    }
    print(json.dumps(result, indent=2, ensure_ascii=False))
    name = "p4-fairness-real.json" if args.real else "p4-fairness.json"
    (args.out / name).write_text(
        json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8"
    )


if __name__ == "__main__":
    main()
