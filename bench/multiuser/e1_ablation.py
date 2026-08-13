#!/usr/bin/env python3
"""P3 — 효과 락 ablation: 동시 파일 쓰기 무결성.

측정 층 선택: **에이전트/LLM 스택을
통과시키지 않고** 실제 도구(write_file)와 락 프리미티브(effect_lock)를 직접
구동한다. E1 은 모델 행동이 아니라 I/O 경합을 재는 실험이므로 결정적 구동이
옳다 — 특히 본류의 공유 트랜스크립트에서는 목 LLM 을 끼우면 동시 턴들이
"최신 지시자"로 붕괴해(모델이 최신 질문에 답하는 알려진 한계의 재현) 서로
다른 내용의 경합 자체가 성립하지 않는다. 실측으로 확인한 함정이다.

워크로드 W1: writer 2개가 같은 파일에 K회 전체 쓰기를 동시에
수행. 페이로드 = ``#HEADER writer seq`` + 본문(결정적 패턴, ~SIZE_KIB) +
``#FOOTER sha256(본문)``. 스냅샷 분류:

  intact  — 헤더/푸터 1쌍 + 체크섬 일치 + 본문 전체가 한 writer 소유
  mixed   — 두 writer 마커 공존 (외부 reader의 혼합 가시성)
  broken  — 구조 파손/체크섬 불일치 (외부 reader의 파손 가시성)
  partial — 쓰기 진행 중 잘림. 락 ON 에서도 보일 수 있다

게이트의 직접 불변식은 참여 writer 임계영역의 비중첩이다. sampler는 게이트에
참여하지 않으므로 mixed/broken/partial은 별도의 reader-visibility 결과이며,
각 1/2/5/10ms snapshot을 독립 시행으로 세지 않고 run당 한 binary 결과로 줄인다.
최종 파일 상태도 세 번째 결과로 분리한다.

이 시스템의 물리 손상 메커니즘은 truncate+write 인터리브다(쓰기를 청크로
스트리밍하는 실행기라면 인터리브 창이 더 넓다 — 손상 강도는 플랫폼 속성이고
불변인 것은 방향과 0 이다). writer 별 본문 크기를 다르게 해
(w0=SIZE_KIB, w1=SIZE_KIB/2) 겹쳐쓰기 잔여(tail remnant)가 마커로 드러나게
한다.

사용: .venv/bin/python bench/multiuser/e1_ablation.py --reps 30 \
        --k 8 --size-kib 128 --sample-ms 1 2 5 10
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import multiprocessing
import os
import random
import re
import shutil
import sys
import tempfile
import threading
import time
from pathlib import Path

# 리포 루트 import 경로 (bench 는 패키지 밖)
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from agent_cli.tools import effect_lock
from agent_cli.tools.write_file import WriteFileTool

TARGET = "x.txt"
_HDR = re.compile(r"^#HEADER (\S+) (\d+)$", re.MULTILINE)
_FTR = re.compile(r"^#FOOTER ([0-9a-f]{64})$", re.MULTILINE)
_BODYLINE = re.compile(r"^(w\d+):\d+:", re.MULTILINE)


def build_payload(writer: str, seq: int, size_kib: int) -> str:
    line = f"{writer}:{seq}:{'ab' * 32}\n"
    repeat = max(1, (size_kib * 1024) // len(line))
    body = line * repeat
    sha = hashlib.sha256(body.encode()).hexdigest()
    return f"#HEADER {writer} {seq}\n{body}#FOOTER {sha}\n"


def classify(text: str) -> str:
    headers = list(_HDR.finditer(text))
    footers = list(_FTR.finditer(text))
    writers = {m.group(1) for m in headers}
    writers.update(m.group(1) for m in _BODYLINE.finditer(text))
    if len(writers) > 1 or len(headers) > 1 or len(footers) > 1:
        return "mixed"
    if not headers or not footers:
        return "partial"
    h, f = headers[0], footers[0]
    body_start = h.end() + 1
    if f.start() < body_start:
        return "broken"
    body = text[body_start : f.start()]
    if hashlib.sha256(body.encode()).hexdigest() != f.group(1):
        return "broken"
    return "intact"


def run_trace(
    scope: str, k: int, size_kib: int, writers: int, sample_ms: float
) -> dict:
    ws = Path(tempfile.mkdtemp(prefix=f"e1-{scope}-"))
    cwd = os.getcwd()
    os.chdir(ws)  # write_file/_confine 은 cwd 를 워크스페이스로 삼는다
    tool = WriteFileTool()
    counts = {"intact": 0, "mixed": 0, "broken": 0, "partial": 0, "empty": 0}
    stop = threading.Event()
    first_violation_ms: float | None = None
    t0 = 0.0
    live_writers = 0
    peak_writers = 0
    writer_tracker = threading.Lock()

    def sampler():
        nonlocal first_violation_ms
        target = ws / TARGET
        while not stop.is_set():
            try:
                text = target.read_text(encoding="utf-8", errors="replace")
                cls = classify(text) if text else "empty"
                counts[cls] += 1
                if cls in ("mixed", "broken") and first_violation_ms is None:
                    first_violation_ms = (time.monotonic() - t0) * 1000.0
            except OSError:
                pass
            time.sleep(sample_ms / 1000.0)

    def writer(idx: int):
        nonlocal live_writers, peak_writers
        wid = f"w{idx}"
        size = size_kib if idx == 0 else max(1, size_kib // 2)
        for seq in range(k):
            args = {"path": TARGET, "content": build_payload(wid, seq, size)}
            intent = tool.effect_intent(args)
            # 제품 경로(tool_bridge._invoke_regular)와 동일한 배선:
            # effect_lock.hold(intent) 아래에서 실제 도구 실행.
            with effect_lock.hold(intent, key="e1"):
                with writer_tracker:
                    live_writers += 1
                    peak_writers = max(peak_writers, live_writers)
                try:
                    tool._run(args)
                finally:
                    with writer_tracker:
                        live_writers -= 1

    try:
        effect_lock.reset()
        effect_lock.set_scope(scope)
        t0 = time.monotonic()
        smp = threading.Thread(target=sampler, daemon=True)
        smp.start()
        threads = [threading.Thread(target=writer, args=(i,)) for i in range(writers)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        elapsed = time.monotonic() - t0
        stop.set()
        smp.join(timeout=2)
        final = classify((ws / TARGET).read_text(encoding="utf-8"))
    finally:
        stop.set()
        effect_lock.reset()
        os.chdir(cwd)
        shutil.rmtree(ws, ignore_errors=True)

    classified = counts["intact"] + counts["mixed"] + counts["broken"]
    violations = counts["mixed"] + counts["broken"]
    return {
        "lock_scope": scope,
        "writers": writers,
        "k_per_writer": k,
        "size_kib": size_kib,
        "sample_ms": sample_ms,
        "elapsed_s": round(elapsed, 2),
        "snapshots": sum(counts.values()),
        "snapshots_classified": classified,
        "counts": counts,
        "violations": violations,
        "externalMixedOrBrokenObserved": violations > 0,
        "peakParticipatingWriters": peak_writers,
        "participatingWriterOverlap": peak_writers > 1,
        "partialObserved": counts["partial"] > 0 or counts["empty"] > 0,
        "estimatedViolationExposureMs": round(violations * sample_ms, 3),
        "firstViolationMs": round(first_violation_ms, 3)
        if first_violation_ms is not None
        else None,
        "violationRate": round(violations / classified, 4) if classified else None,
        "finalClass": final,
    }


def _run_child(conn, scope, k, size_kib, writers, sample_ms):
    """One trace per fresh spawned process: process is the experimental unit."""
    try:
        conn.send(run_trace(scope, k, size_kib, writers, sample_ms))
    except BaseException as exc:
        conn.send({"error": f"{type(exc).__name__}: {exc}"})
    finally:
        conn.close()


def run_independent(
    scope: str, k: int, size_kib: int, writers: int, sample_ms: float
) -> dict:
    ctx = multiprocessing.get_context("spawn")
    parent, child = ctx.Pipe(duplex=False)
    proc = ctx.Process(
        target=_run_child,
        args=(child, scope, k, size_kib, writers, sample_ms),
    )
    proc.start()
    child.close()
    row = parent.recv()
    proc.join(timeout=120)
    if proc.is_alive():
        proc.terminate()
        proc.join()
        raise RuntimeError(f"trace timed out: {scope}, {sample_ms} ms")
    if proc.exitcode != 0 or "error" in row:
        raise RuntimeError(row.get("error", f"child exit {proc.exitcode}"))
    return row


def _binom_cdf(k: int, n: int, p: float) -> float:
    return sum(
        math.comb(n, i) * p**i * (1.0 - p) ** (n - i) for i in range(k + 1)
    )


def exact_binomial_ci(k: int, n: int, alpha: float = 0.05) -> list[float]:
    """Two-sided Clopper-Pearson interval, computed with stdlib only."""

    def bisect(fn, increasing: bool) -> float:
        lo, hi = 0.0, 1.0
        target = alpha / 2.0
        for _ in range(80):
            mid = (lo + hi) / 2.0
            value = fn(mid)
            if (value < target) == increasing:
                lo = mid
            else:
                hi = mid
        return (lo + hi) / 2.0

    lower = 0.0 if k == 0 else bisect(
        lambda p: 1.0 - _binom_cdf(k - 1, n, p), True
    )
    upper = 1.0 if k == n else bisect(lambda p: _binom_cdf(k, n, p), False)
    return [round(lower, 4), round(upper, 4)]


def exact_mcnemar_p(discordant_a: int, discordant_b: int) -> float:
    """Two-sided exact McNemar test over paired binary run outcomes."""
    n = discordant_a + discordant_b
    if not n:
        return 1.0
    tail = sum(math.comb(n, i) for i in range(min(discordant_a, discordant_b) + 1))
    return min(1.0, 2.0 * tail / (2**n))


def summarize(rows: list[dict]) -> list[dict]:
    out = []
    for sample_ms in sorted({r["sample_ms"] for r in rows}):
        for scope in ("off", "workspace", "conflict"):
            sub = [
                r
                for r in rows
                if r["sample_ms"] == sample_ms and r["lock_scope"] == scope
            ]
            if not sub:
                continue
            k = sum(r["externalMixedOrBrokenObserved"] for r in sub)
            overlap = sum(r["participatingWriterOverlap"] for r in sub)
            partial = sum(r["partialObserved"] for r in sub)
            finals = {name: sum(r["finalClass"] == name for r in sub) for name in (
                "intact", "mixed", "broken", "partial"
            )}
            out.append(
                {
                    "sample_ms": sample_ms,
                    "lock_scope": scope,
                    "runs": len(sub),
                    "runsWithViolation": k,
                    "runViolationRate": round(k / len(sub), 4),
                    "runViolationRateExactCI95": exact_binomial_ci(k, len(sub)),
                    "runsWithPartialVisibility": partial,
                    "runsWithParticipatingWriterOverlap": overlap,
                    "writerOverlapRateExactCI95": exact_binomial_ci(
                        overlap, len(sub)
                    ),
                    "medianEstimatedViolationExposureMs": sorted(
                        r["estimatedViolationExposureMs"] for r in sub
                    )[len(sub) // 2],
                    "finalClasses": finals,
                    "snapshotCountsDescriptiveOnly": {
                        name: sum(r["counts"][name] for r in sub)
                        for name in ("intact", "mixed", "broken", "partial", "empty")
                    },
                }
            )
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--k", type=int, default=50)
    ap.add_argument("--size-kib", type=int, default=1024)
    ap.add_argument("--writers", type=int, default=2)
    ap.add_argument("--reps", type=int, default=30)
    ap.add_argument(
        "--sample-ms",
        nargs="+",
        type=float,
        default=[2.0],
        help="polling interval(s); snapshots are descriptive, run is the unit",
    )
    ap.add_argument("--seed", type=int, default=20260813)
    ap.add_argument("--scopes", nargs="*", default=["off", "workspace", "conflict"])
    ap.add_argument("--out", type=Path, default=Path(__file__).parent / "out")
    args = ap.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)

    rng = random.Random(args.seed)
    results = []
    for sample_ms in args.sample_ms:
        for rep in range(1, args.reps + 1):
            block = list(args.scopes)
            rng.shuffle(block)
            for order, scope in enumerate(block, 1):
                r = run_independent(
                    scope, args.k, args.size_kib, args.writers, sample_ms
                )
                r.update({"rep": rep, "blockOrder": order})
                results.append(r)
                print(json.dumps(r), flush=True)
    summary = summarize(results)
    paired = []
    for sample_ms in args.sample_ms:
        by = {
            (r["rep"], r["lock_scope"]): r["externalMixedOrBrokenObserved"]
            for r in results
            if r["sample_ms"] == sample_ms
        }
        for locked in ("workspace", "conflict"):
            a = sum(
                by[(rep, "off")] and not by[(rep, locked)]
                for rep in range(1, args.reps + 1)
            )
            b = sum(
                by[(rep, locked)] and not by[(rep, "off")]
                for rep in range(1, args.reps + 1)
            )
            paired.append(
                {
                    "sample_ms": sample_ms,
                    "contrast": f"off vs {locked}",
                    "discordantOffOnly": a,
                    "discordantLockedOnly": b,
                    "exactPairedP": exact_mcnemar_p(a, b),
                }
            )
    payload = {
        "experimentalUnit": "one trace in a fresh process and temporary workspace",
        "randomization": f"arm order randomized within rep; seed {args.seed}",
        "primaryOutcomes": {
            "writerOrdering": "whether participating writer critical sections overlapped",
            "externalVisibility": "whether any mixed/broken read was observed in a run",
            "finalState": "classification after both writers completed",
        },
        "snapshotPolicy": (
            "correlated snapshot counts are descriptive only; external visibility "
            "is reduced to one binary outcome per independent run"
        ),
        "results": results,
        "summary": summary,
        "pairedContrasts": paired,
    }
    (args.out / "e1-ablation-p0.json").write_text(
        json.dumps(payload, indent=2), encoding="utf-8"
    )
    (args.out / "e1-ablation-p0.jsonl").write_text(
        "".join(json.dumps(r) + "\n" for r in results), encoding="utf-8"
    )
    print(json.dumps({"summary": summary, "pairedContrasts": paired}, indent=2))


if __name__ == "__main__":
    main()
