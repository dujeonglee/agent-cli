#!/usr/bin/env python3
"""P6b — 제공자는 실제로 몇 개를 동시에 디코드하는가.

§6.10 은 "A 의 *존재*로부터의 독립은 제공자의 동시성이 정하는 서빙 계층의
성질이며 세션 계약이 만들어낼 수 없다"고 적었다. 그 문장은 옳지만 수치가
없었다 — 심사자가 "그래서 그 동시성이 얼마냐"고 물으면 답할 것이 없었다.
이 프로브가 그 숫자를 만든다.

방법은 세션도 에이전트도 거치지 않고 **엔드포인트를 직접** 때린다. 동일한
고정 길이 생성 요청 N 개를 동시에 던지고 벽시계를 본다:

  완전 직렬화       → 총 시간이 N 에 비례, 요청당 시간도 N 에 비례
  완전 동시(배치)   → 총 시간이 거의 평평, 요청당 시간도 평평
  그 사이           → 슬롯 수만큼 평평하다가 그 뒤로 계단

세션 계약은 이 곡선을 바꿀 수 없다. 병렬 계약이 사 주는 것은 "B 의 요청이
서버까지 **도달**한다"이지 "서버가 그것을 A 와 동시에 **처리**한다"가
아니기 때문이다. 그래서 이 수치는 §6.10 의 병렬 절대값(7.0 s)을 해석하는
분모이지, 계약의 성적표가 아니다.

사용: AGENT_CLI_BASE_URL/API_KEY/MODEL 설정 후
  .venv/bin/python bench/multiuser/p6b_provider_concurrency.py [--reps 3]
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

#: 요청당 생성 토큰 수 — 크면 디코드 구간이 길어져 동시성 효과가 또렷해지고,
#: 너무 크면 프로브가 느려진다.
MAX_TOKENS = 120
LEVELS = (1, 2, 4, 8)
PROMPT = "Count from 1 to 60, one number per line. Output nothing else."


def env() -> tuple[str, str, str]:
    try:
        return (
            os.environ["AGENT_CLI_BASE_URL"].rstrip("/"),
            os.environ["AGENT_CLI_API_KEY"],
            os.environ["AGENT_CLI_MODEL"],
        )
    except KeyError as e:
        sys.exit(f"missing env {e} — set AGENT_CLI_BASE_URL/API_KEY/MODEL")


def one_call(base: str, key: str, model: str) -> tuple[float, int]:
    body = json.dumps(
        {
            "model": model,
            "messages": [{"role": "user", "content": PROMPT}],
            "max_tokens": MAX_TOKENS,
            "temperature": 0,
        }
    ).encode()
    req = urllib.request.Request(
        f"{base}/chat/completions",
        data=body,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {key}",
        },
    )
    t0 = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=300) as r:
            payload = json.loads(r.read())
    except (urllib.error.URLError, TimeoutError, OSError):
        return (time.monotonic() - t0) * 1000, 0
    out = int((payload.get("usage") or {}).get("output_tokens", 0) or 0)
    return (time.monotonic() - t0) * 1000, out


def level(base: str, key: str, model: str, n: int) -> dict:
    lat: list[float] = [0.0] * n
    toks: list[int] = [0] * n
    gate = threading.Barrier(n)

    def work(i: int) -> None:
        gate.wait()  # 동시에 출발시키는 것이 이 실험의 전부다
        lat[i], toks[i] = one_call(base, key, model)

    threads = [threading.Thread(target=work, args=(i,)) for i in range(n)]
    t0 = time.monotonic()
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    wall = (time.monotonic() - t0) * 1000
    return {
        "n": n,
        "wallMs": round(wall, 1),
        "perRequestP50Ms": round(statistics.median(lat), 1),
        "perRequestMaxMs": round(max(lat), 1),
        "outputTokensTotal": sum(toks),
        "failures": sum(1 for t in toks if t == 0),
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--reps", type=int, default=3)
    ap.add_argument("--out", type=Path, default=Path(__file__).parent / "out")
    ap.add_argument("--levels", type=int, nargs="*", default=list(LEVELS))
    args = ap.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    base, key, model = env()

    rows = []
    for rep in range(1, args.reps + 1):
        for n in args.levels:
            row = {"rep": rep, **level(base, key, model, n)}
            rows.append(row)
            print(json.dumps(row), flush=True)

    cells = []
    base_wall = None
    for n in sorted({r["n"] for r in rows}):
        sub = [r for r in rows if r["n"] == n]
        wall = statistics.median(r["wallMs"] for r in sub)
        per = statistics.median(r["perRequestP50Ms"] for r in sub)
        if n == 1:
            base_wall = wall
        cells.append(
            {
                "n": n,
                "wallP50Ms": round(wall, 1),
                "perRequestP50Ms": round(per, 1),
                # 1 이면 완전 동시(N 개를 1 개 시간에), N 이면 완전 직렬.
                "wallRatioToN1": round(wall / base_wall, 3) if base_wall else None,
                "throughputPerS": round(
                    sum(r["outputTokensTotal"] for r in sub)
                    / max(1e-9, sum(r["wallMs"] for r in sub) / 1000),
                    1,
                ),
                "failures": sum(r["failures"] for r in sub),
            }
        )
    summary = {
        "model": model,
        "maxTokensPerRequest": MAX_TOKENS,
        "reps": args.reps,
        "cells": cells,
        "note": (
            "wallRatioToN1 near 1 means the endpoint decodes the batch "
            "concurrently; near N means it serialises. This bounds how much "
            "of the session contract's parallelism a user can actually feel, "
            "and it is a property of the serving stack, not of the contract."
        ),
    }
    (args.out / "p6b-provider-concurrency.json").write_text(
        json.dumps(summary, indent=2), encoding="utf-8"
    )
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
