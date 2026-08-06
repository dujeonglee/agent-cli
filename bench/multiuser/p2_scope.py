#!/usr/bin/env python3
"""P2-SCOPE — 왜 락 경계를 워크스페이스에서 충돌 단위로 좁혔는가 (§4.4 근거).

효과 락 v1 은 워크스페이스 단위 **단일 mutex** 였다. 그 설계의 문제는
"동시 쓰기를 막는다"가 아니라 **서로소 경로까지 줄 세운다**는 것이다. 이
실험은 그 비용을 자체 실측으로 값매김한다 — 같은 워크로드를
``--lock-scope workspace`` 와 ``conflict`` 로 각각 돌려 유효 병렬도를 비교.

측정 층은 E1 과 같다 (``e1_ablation.py`` 독스트링): **에이전트/LLM 스택을
통과시키지 않고** 실제 도구(write_file)와 락 프리미티브(effect_lock)를 직접
구동한다. 두 가지 이유가 있다. (a) 공유 트랜스크립트에서 목 LLM 을 끼우면
동시 턴들이 "최신 지시자"로 붕괴해 서로 다른 경로에 쓰는 경합 자체가 성립하지
않는다(E1 에서 실측한 함정). (b) 대용량 쓰기로 효과 시간을 늘리려면 그 내용이
LLM 응답으로 스트리밍되고 컨텍스트에 누적돼, 재려는 것(효과 구간)이 아니라
추론·프롬프트 쪽이 함께 부풀어 교란된다.

워크로드 — "턴" 2개를 스레드로 모사한다. 각 턴은 K 라운드를 돈다::

    sleep(infer_ms)            # 추론 구간 — 락 밖 (병렬이 이득을 내는 곳)
    with effect_lock.hold():   # 효과 구간 — 락 안 (계약이 직렬화하는 곳)
        write_file(size_kib)

축:
  paths  — ``disjoint``(턴마다 다른 파일) vs ``same``(같은 파일)
  scope  — ``workspace``(v1 전역 mutex) vs ``conflict``(v2 충돌 단위)
           + 참조용 ``off``(락 없음 = 병렬도 상한; 무결성은 E1 이 이 팔에서
           위반을 보인다 — 성능 상한 참조일 뿐 권장 설정이 아니다)
  share  — 턴당 **효과 시간 비중** 목표(%). ``infer_ms`` 를 효과 실측
           지속시간에 맞춰 정해 만든다. 붕괴가 이 비중의 함수라는 것이
           P2 그리드/셸 팔의 결론이므로 여기서도 같은 축으로 쓸어본다.

지표:
  effective_parallelism = (work_A + work_B) / makespan   (1.0 직렬 … 2.0 병렬)
      ``work`` 는 스레드의 벽시계 구간에서 **락 대기를 뺀** 값이다. 구간을
      그대로 쓰면 락에 막혀 서 있는 스레드도 "일하는 중"으로 세어져 완전
      직렬화된 실행조차 2.0 으로 나온다 (초안에서 실제로 그랬다).
  effect_share_measured = 락 보유 시간 합 / (work_A + work_B)

읽는 법 — 턴 2개에 대한 병렬도 상한은 ``2 / max(1, 2s)`` 다(s = 효과 비중).
s ≤ 0.5 면 효과를 **완전히 직렬화해도 손해가 0** 이라 스코프가 구분되지
않는다: 한 턴의 추론이 다른 턴의 효과 뒤에 통째로 숨기 때문이다. 전역 락의
비용은 효과 비중이 그 선을 넘어야 나타난다.

기대(가설): ``disjoint`` 에서 conflict ≫ workspace, ``same`` 에서 둘이 동일.
같은 파일 셀이 대조군이다 — 차이가 "스코프 오버헤드"가 아니라 **서로소
경로를 줄 세우느냐**에서 온다는 것을 그 셀이 보인다.

사용: .venv/bin/python bench/multiuser/p2_scope.py [--reps 5] [--rounds 40]
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
import time
from pathlib import Path

# 리포 루트 import 경로 (bench 는 패키지 밖)
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from agent_cli.tools import effect_lock
from agent_cli.tools.write_file import WriteFileTool

SCOPES = ("workspace", "conflict", "off")
PATHS = ("disjoint", "same")
#: 효과 시간 비중(%) — 스코프 차이는 이 축에서만 드러난다. 턴 2개에 대한
#: 상한은 ``2 / max(1, 2s)`` 이므로 s ≤ 0.5 에서는 효과를 **완전히 직렬화해도
#: 손해가 0** 이다(한 턴의 추론이 다른 턴의 효과 뒤에 숨는다).
#:
#: 25% 를 넣는 이유: 무릎 **아래**를 주장이 아니라 측정으로 채우기 위해서다.
#: 실 LLM 작동점은 이보다도 몇 자릿수 낮으므로(`p2_scope_real.py`), 25% 셀이
#: 없으면 "실제 워크로드는 경계에서 멀다"는 결론이 50% 한 점에서 외삽된다.
#: 25% 와 50% 가 나란히 평탄해야 그 외삽이 관측으로 바뀐다.
SHARES = (25, 50, 75, 90)
_LINE = "w:" + "ab" * 32 + "\n"


def payload(size_kib: int, tag: str, seq: int) -> str:
    body = _LINE * max(1, (size_kib * 1024) // len(_LINE))
    return f"#T {tag} {seq}\n{body}"


def calibrate(size_kib: int, samples: int = 8) -> float:
    """이 머신에서 ``size_kib`` 쓰기 1회가 실제로 몇 ms 인가 (p50).

    효과 시간 비중을 **하드코딩한 상수가 아니라 실측**에서 잡는다 — 디스크가
    다르면 같은 크기가 다른 시간이고, 그러면 share 축이 이름만 맞고 내용이
    달라진다. 정상상태(덮어쓰기) 경로를 잰다: write_file 은 기존 파일이 있으면
    변경 에코용 diff 를 함께 계산하므로 첫 생성보다 느리고, 워크로드가 도는
    구간은 정상상태 쪽이다.
    """
    ws = Path(tempfile.mkdtemp(prefix="p2scope-cal-"))
    cwd = os.getcwd()
    os.chdir(ws)
    tool = WriteFileTool()
    try:
        tool._run({"path": "cal.txt", "content": payload(size_kib, "cal", 0)})
        ds = []
        for i in range(samples):
            t0 = time.perf_counter()
            tool._run({"path": "cal.txt", "content": payload(size_kib, "cal", i + 1)})
            ds.append((time.perf_counter() - t0) * 1000.0)
        return statistics.median(ds)
    finally:
        os.chdir(cwd)
        shutil.rmtree(ws, ignore_errors=True)


def run_rep(
    scope: str, paths: str, infer_ms: float, rounds: int, size_kib: int
) -> dict:
    """턴 2개를 동시에 돌리고 한 번의 관측치를 낸다."""
    ws = Path(tempfile.mkdtemp(prefix=f"p2scope-{scope}-{paths}-"))
    cwd = os.getcwd()
    os.chdir(ws)  # write_file/_confine 은 cwd 를 워크스페이스로 삼는다
    tool = WriteFileTool()
    stats: dict[int, dict] = {}
    start_gate = threading.Barrier(2)

    def turn(idx: int) -> None:
        target = "shared.txt" if paths == "same" else f"t{idx}.txt"
        tag = f"w{idx}"
        wait_total = held_total = 0.0
        start_gate.wait()  # 두 턴이 같은 순간에 출발 (겹침 구간 최대화)
        t_begin = time.perf_counter()
        for seq in range(rounds):
            time.sleep(infer_ms / 1000.0)  # 추론 — 락 밖
            args = {"path": target, "content": payload(size_kib, tag, seq)}
            intent = tool.effect_intent(args)
            t_req = time.perf_counter()
            # 제품 경로(tool_bridge._invoke_regular)와 동일한 배선.
            with effect_lock.hold(intent, key="p2scope"):
                t_in = time.perf_counter()
                tool._run(args)
                held_total += time.perf_counter() - t_in
            wait_total += t_in - t_req
        stats[idx] = {
            "begin": t_begin,
            "end": time.perf_counter(),
            "wait_ms": wait_total * 1000.0,
            "held_ms": held_total * 1000.0,
        }

    try:
        effect_lock.reset()
        effect_lock.set_scope(scope)
        threads = [threading.Thread(target=turn, args=(i,)) for i in (0, 1)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    finally:
        effect_lock.reset()
        os.chdir(cwd)
        shutil.rmtree(ws, ignore_errors=True)

    a, b = stats[0], stats[1]
    span_a = (a["end"] - a["begin"]) * 1000.0
    span_b = (b["end"] - b["begin"]) * 1000.0
    # ★ 유효 병렬도의 분자는 벽시계 구간이 아니라 **일한 시간**이다. 락에
    # 막혀 있는 스레드도 벽시계로는 계속 "돌고" 있으므로, 구간을 그대로
    # 쓰면 완전 직렬화된 실행조차 2.0 으로 나온다(초안에서 실제로 그랬다).
    # 대기를 빼면 정의가 제자리를 찾는다: 완전 겹침 2.0, 완전 직렬 1.0.
    work_a = span_a - a["wait_ms"]
    work_b = span_b - b["wait_ms"]
    makespan = (max(a["end"], b["end"]) - min(a["begin"], b["begin"])) * 1000.0
    held = a["held_ms"] + b["held_ms"]
    return {
        "workA_ms": round(work_a, 1),
        "workB_ms": round(work_b, 1),
        "spanA_ms": round(span_a, 1),
        "spanB_ms": round(span_b, 1),
        "makespan_ms": round(makespan, 1),
        "effective_parallelism": round((work_a + work_b) / makespan, 3)
        if makespan
        else None,
        "lock_wait_ms": round(a["wait_ms"] + b["wait_ms"], 1),
        "lock_held_ms": round(held, 1),
        "effect_share_measured": round(held / (work_a + work_b), 3)
        if (work_a + work_b)
        else None,
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--reps", type=int, default=5)
    ap.add_argument("--rounds", type=int, default=40)
    ap.add_argument("--size-kib", type=int, default=1024)
    ap.add_argument("--shares", type=int, nargs="*", default=list(SHARES))
    ap.add_argument("--out", type=Path, default=Path(__file__).parent / "out")
    args = ap.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)

    effect_ms = calibrate(args.size_kib)
    print(
        json.dumps(
            {
                "calibration": {
                    "size_kib": args.size_kib,
                    "write_p50_ms": round(effect_ms, 2),
                }
            }
        ),
        flush=True,
    )

    rows: list[dict] = []
    t_start = time.time()
    for share in args.shares:
        # share = effect / (effect + infer)  →  infer = effect × (1/share − 1)
        infer_ms = effect_ms * (100.0 / share - 1.0)
        for paths in PATHS:
            for scope in SCOPES:
                for rep in range(1, args.reps + 1):
                    row = run_rep(scope, paths, infer_ms, args.rounds, args.size_kib)
                    row.update(
                        {
                            "share_target_pct": share,
                            "paths": paths,
                            "lock_scope": scope,
                            "rep": rep,
                            "infer_ms": round(infer_ms, 2),
                            "rounds": args.rounds,
                            "size_kib": args.size_kib,
                        }
                    )
                    rows.append(row)
                    print(json.dumps(row), flush=True)

    (args.out / "p2-scope-motivation.jsonl").write_text(
        "".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8"
    )

    cells = []
    for share in args.shares:
        for paths in PATHS:
            for scope in SCOPES:
                cell = [
                    r
                    for r in rows
                    if r["share_target_pct"] == share
                    and r["paths"] == paths
                    and r["lock_scope"] == scope
                ]
                if not cell:
                    continue
                cells.append(
                    {
                        "share_target_pct": share,
                        "paths": paths,
                        "lock_scope": scope,
                        "n": len(cell),
                        "effective_parallelism_p50": round(
                            statistics.median(
                                [r["effective_parallelism"] for r in cell]
                            ),
                            3,
                        ),
                        "effect_share_measured_p50": round(
                            statistics.median(
                                [r["effect_share_measured"] for r in cell]
                            ),
                            3,
                        ),
                        "lock_wait_p50_ms": round(
                            statistics.median([r["lock_wait_ms"] for r in cell]), 1
                        ),
                    }
                )

    def pick(share: int, paths: str, scope: str) -> float | None:
        for c in cells:
            if (
                c["share_target_pct"] == share
                and c["paths"] == paths
                and c["lock_scope"] == scope
            ):
                return c["effective_parallelism_p50"]
        return None

    # 핵심 대조: 서로소 경로에서 스코프를 좁히면 병렬도가 얼마나 회복되는가.
    # 같은 파일 행이 ~1.0 이어야 그 이득이 "무결성을 판 것"이 아님이 보인다.
    contrast = []
    for share in args.shares:
        for paths in PATHS:
            w, c = pick(share, paths, "workspace"), pick(share, paths, "conflict")
            contrast.append(
                {
                    "share_target_pct": share,
                    "paths": paths,
                    "workspace": w,
                    "conflict": c,
                    "conflict_over_workspace": round(c / w, 3) if w else None,
                    "off_reference": pick(share, paths, "off"),
                }
            )

    summary = {
        "calibration_write_p50_ms": round(effect_ms, 2),
        "reps": args.reps,
        "rounds": args.rounds,
        "size_kib": args.size_kib,
        "cells": cells,
        "contrast": contrast,
        "elapsed_s": round(time.time() - t_start, 1),
    }
    (args.out / "p2-scope-summary.json").write_text(
        json.dumps(summary, indent=2), encoding="utf-8"
    )
    print(json.dumps(contrast, indent=2))


if __name__ == "__main__":
    main()
