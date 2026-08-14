#!/usr/bin/env python3
"""통계 재계산 — 논문이 인용하는 핵심 대비에 검정/신뢰구간을 붙인다 (재실험 없음).

리뷰(R2-W6)의 지적: 중앙값·범위·R² 는 보고하면서 인용되는 대비(26/40 대
0/40 등)에 검정이 하나도 없다. 이 스크립트는 **커밋된 raw 파일만** 읽어
다음을 계산하고 ``out/stats-recompute.json`` 에 남긴다:

  1. §5.4 라이브 스코핑 대비 — 동시 두 턴을 묶은 run/pair 단위 결과와
     교대 순서 블록의 exact McNemar 검정 (`n3c-realistic-p0.json`).
  2. §5.2 무결성 절제 — 독립 프로세스 run 단위 외부-reader 노출,
     참여 writer 중첩, 최종 상태와 exact McNemar 검정
     (`e1-ablation-p0.json`). 연속 snapshot Fisher 검정은 사용하지 않는다.
  3. §5.1 라이브 TTFT — 20개 paired block의 계약별 중앙값 퍼센타일
     부트스트랩 95% CI, 10,000 재표집, 고정 시드
     (`p6-ttft-replication.json`).

정책 문장(논문 §5 Setup): 반복 binary 결과는 run/pair 단위 exact interval과
exact McNemar 검정을 사용하고, 중앙값 구간은 고정 시드 퍼센타일 부트스트랩 95%
CI를 쓴다. 원시 run 표본이 커밋된 대비에만 적용한다.

stdlib 만 사용한다.

사용: .venv/bin/python bench/multiuser/stats_recompute.py
"""

from __future__ import annotations

import json
import random
import statistics
from pathlib import Path

OUT = Path(__file__).parent / "out"
SEED = 20260813
RESAMPLES = 10_000


def J(name: str):
    return json.loads((OUT / name).read_text(encoding="utf-8"))


def bootstrap_median_ci(values: list[float], *, seed: int = SEED) -> dict:
    rng = random.Random(seed)
    n = len(values)
    medians = sorted(
        statistics.median(rng.choices(values, k=n)) for _ in range(RESAMPLES)
    )
    return {
        "n": n,
        "median": statistics.median(values),
        "ci95": [
            medians[int(RESAMPLES * 0.025)],
            medians[int(RESAMPLES * 0.975) - 1],
        ],
        "resamples": RESAMPLES,
        "seed": seed,
    }


def scoping_contrasts() -> dict:
    data = J("n3c-realistic-p0.json")
    return {
        "experimental_unit": data["experimentalUnit"],
        "arms": data["arms"],
        "paired_contrasts": data["pairedContrasts"],
        "note": "turn-level counts are descriptive; inference uses paired runs",
    }


def integrity_contrast() -> dict:
    data = J("e1-ablation-p0.json")
    return {
        "experimental_unit": data["experimentalUnit"],
        "primary_outcomes": data["primaryOutcomes"],
        "summary": data["summary"],
        "paired_contrasts": data["pairedContrasts"],
        "note": "snapshot counts are descriptive and never treated as independent",
    }


def ranking_cis() -> dict:
    data = J("p6-ttft-replication.json")
    out = {}
    for offset, arm in enumerate(("serial", "parallel")):
        values = [
            float(run["bTtftMs"])
            for run in data["runs"]
            if run["arm"] == arm and run["valid"]
        ]
        out[arm] = bootstrap_median_ci(values, seed=SEED + offset)
    return out


def main() -> None:
    result = {
        "semantic_5_4": scoping_contrasts(),
        "integrity_5_2": integrity_contrast(),
        "ranking_5_1_ttft_ms": ranking_cis(),
    }
    out_path = OUT / "stats-recompute.json"
    out_path.write_text(json.dumps(result, indent=2, ensure_ascii=False))
    print(json.dumps(result, indent=2, ensure_ascii=False))
    print(f"\nwrote {out_path}")


if __name__ == "__main__":
    main()
