#!/usr/bin/env python3
"""통계 재계산 — 논문이 인용하는 핵심 대비에 검정/신뢰구간을 붙인다 (재실험 없음).

리뷰(R2-W6)의 지적: 중앙값·범위·R² 는 보고하면서 인용되는 대비(26/40 대
0/40 등)에 검정이 하나도 없다. 이 스크립트는 **커밋된 raw 파일만** 읽어
다음을 계산하고 ``out/stats-recompute.json`` 에 남긴다:

  1. §6.7 라이브 스코핑 대비 — Fisher 정확검정 (양측):
     남의 파일을 쓴 턴 26/40 vs 0/40, 자기 과제 완수 34/40 vs 40/40,
     두 과제 모두 옳은 실행 14/20 vs 20/20  (`n3c-scoping-real.jsonl`)
  2. §6.2 무결성 절제 — 위반 9/110 (off) vs 0/361 (락 켬 합산) Fisher
     (`e1-ablation.json`)
  3. §6.10 라이브 랭킹 — 계약별 12회 TTFT 중앙값의 퍼센타일 부트스트랩
     95% CI, 10,000 재표집, 고정 시드 (`p6-real-llm.json`)

정책 문장(논문 §6 Setup): 쌍 비교는 양측 Fisher 정확검정, 중앙값 구간은
고정 시드 퍼센타일 부트스트랩 95% CI — 원시 표본이 커밋된 대비에만 적용한다
(§6.8 처럼 집계만 커밋된 실험은 대상이 아니다).

stdlib 만 사용한다 (Fisher 는 초기하분포를 ``math.comb`` 로 직접).

사용: .venv/bin/python bench/multiuser/stats_recompute.py
"""

from __future__ import annotations

import json
import random
import statistics
from math import comb
from pathlib import Path

OUT = Path(__file__).parent / "out"
SEED = 20260807
RESAMPLES = 10_000


def fisher_exact_two_sided(a: int, b: int, c: int, d: int) -> float:
    """2×2 표 [[a, b], [c, d]] 의 양측 Fisher 정확검정 p-값.

    관례적 정의: 관측 표보다 확률이 크지 않은 모든 표의 초기하 확률 합.
    부동소수 동률은 (1+1e-9) 관용으로 흡수한다 (R/scipy 와 같은 규약).
    """
    n = a + b + c + d
    r1 = a + b
    c1 = a + c

    def p_of(x: int) -> float:
        return comb(c1, x) * comb(n - c1, r1 - x) / comb(n, r1)

    lo = max(0, r1 + c1 - n)
    hi = min(r1, c1)
    p_obs = p_of(a)
    return min(
        1.0, sum(p_of(x) for x in range(lo, hi + 1) if p_of(x) <= p_obs * (1 + 1e-9))
    )


def bootstrap_median_ci(values: list[float]) -> dict:
    rng = random.Random(SEED)
    n = len(values)
    medians = sorted(
        statistics.median(rng.choices(values, k=n)) for _ in range(RESAMPLES)
    )
    return {
        "n": n,
        "median": statistics.median(values),
        "ci95": [medians[int(RESAMPLES * 0.025)], medians[int(RESAMPLES * 0.975)]],
        "resamples": RESAMPLES,
        "seed": SEED,
    }


def scoping_contrasts() -> dict:
    rows = [
        json.loads(line)
        for line in (OUT / "n3c-scoping-real.jsonl").read_text().splitlines()
        if line.strip()
    ]
    tally = {
        arm: {"turns": 0, "wrote_others": 0, "own_complete": 0, "runs": 0, "both": 0}
        for arm in ("off", "on")
    }
    for r in rows:
        t = tally[r["scoping"]]
        t["runs"] += 1
        t["both"] += 1 if r["bothComplete"] else 0
        for turn in r["turns"]:
            t["turns"] += 1
            t["wrote_others"] += 1 if turn["wroteOthers"] else 0
            t["own_complete"] += 1 if turn["ownComplete"] else 0
    off, on = tally["off"], tally["on"]

    def contrast(name, a, na, b, nb):
        return {
            "contrast": name,
            "off": f"{a}/{na}",
            "on": f"{b}/{nb}",
            "fisher_p_two_sided": fisher_exact_two_sided(a, na - a, b, nb - b),
        }

    return {
        "tally": tally,
        "tests": [
            contrast(
                "wrote another user's files (turns)",
                off["wrote_others"],
                off["turns"],
                on["wrote_others"],
                on["turns"],
            ),
            contrast(
                "completed own task (turns)",
                off["own_complete"],
                off["turns"],
                on["own_complete"],
                on["turns"],
            ),
            contrast(
                "both tasks correct (runs)",
                off["both"],
                off["runs"],
                on["both"],
                on["runs"],
            ),
        ],
    }


def integrity_contrast() -> dict:
    data = json.loads((OUT / "e1-ablation.json").read_text())
    cells = {}
    for row in data["results"]:
        counts = row["counts"]
        violations = counts.get("mixed", 0) + counts.get("broken", 0)
        cells[row["lock_scope"]] = {
            "classified": row["snapshots_classified"],
            "violations": violations,
        }
    off = cells["off"]
    locked_n = sum(c["classified"] for k, c in cells.items() if k != "off")
    locked_v = sum(c["violations"] for k, c in cells.items() if k != "off")
    return {
        "cells": cells,
        "fisher_p_two_sided": fisher_exact_two_sided(
            off["violations"],
            off["classified"] - off["violations"],
            locked_v,
            locked_n - locked_v,
        ),
        "note": "off violations vs both locked scopes pooled",
    }


def ranking_cis() -> dict:
    data = json.loads((OUT / "p6-real-llm.json").read_text())
    out = {}
    for row in data["hol_spot"]:
        out[row["contract"]] = bootstrap_median_ci([float(v) for v in row["bTtft_all"]])
    return out


def main() -> None:
    result = {
        "scoping_6_7": scoping_contrasts(),
        "integrity_6_2": integrity_contrast(),
        "ranking_6_10_ttft_ms": ranking_cis(),
    }
    out_path = OUT / "stats-recompute.json"
    out_path.write_text(json.dumps(result, indent=2, ensure_ascii=False))
    print(json.dumps(result, indent=2, ensure_ascii=False))
    print(f"\nwrote {out_path}")


if __name__ == "__main__":
    main()
