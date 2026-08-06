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
  mixed   — 두 writer 마커 공존 (torn write) — **위반**
  broken  — 구조 파손/체크섬 불일치 — **위반**
  partial — 쓰기 진행 중 잘림. 락 ON 에서도 정상이라 위반으로 세지 않는다
            (open("w") 는 truncate 후 기록 — 중간 상태는 계약 위반이 아니다)

이 시스템의 물리 손상 메커니즘은 truncate+write 인터리브다(쓰기를 청크로
스트리밍하는 실행기라면 인터리브 창이 더 넓다 — 손상 강도는 플랫폼 속성이고
불변인 것은 방향과 0 이다). writer 별 본문 크기를 다르게 해
(w0=SIZE_KIB, w1=SIZE_KIB/2) 겹쳐쓰기 잔여(tail remnant)가 마커로 드러나게
한다.

사용: .venv/bin/python bench/multiuser/e1_ablation.py [--k 50] [--size-kib 1024]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
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


def run_arm(scope: str, k: int, size_kib: int, writers: int) -> dict:
    ws = Path(tempfile.mkdtemp(prefix=f"e1-{scope}-"))
    cwd = os.getcwd()
    os.chdir(ws)  # write_file/_confine 은 cwd 를 워크스페이스로 삼는다
    tool = WriteFileTool()
    counts = {"intact": 0, "mixed": 0, "broken": 0, "partial": 0, "empty": 0}
    stop = threading.Event()

    def sampler():
        target = ws / TARGET
        while not stop.is_set():
            try:
                text = target.read_text(encoding="utf-8", errors="replace")
                counts[classify(text) if text else "empty"] += 1
            except OSError:
                pass
            time.sleep(0.002)  # 2 ms 폴링

    def writer(idx: int):
        wid = f"w{idx}"
        size = size_kib if idx == 0 else max(1, size_kib // 2)
        for seq in range(k):
            args = {"path": TARGET, "content": build_payload(wid, seq, size)}
            intent = tool.effect_intent(args)
            # 제품 경로(tool_bridge._invoke_regular)와 동일한 배선:
            # effect_lock.hold(intent) 아래에서 실제 도구 실행.
            with effect_lock.hold(intent, key="e1"):
                tool._run(args)

    try:
        effect_lock.reset()
        effect_lock.set_scope(scope)
        smp = threading.Thread(target=sampler, daemon=True)
        smp.start()
        threads = [threading.Thread(target=writer, args=(i,)) for i in range(writers)]
        t0 = time.monotonic()
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
        "elapsed_s": round(elapsed, 2),
        "snapshots": sum(counts.values()),
        "snapshots_classified": classified,
        "counts": counts,
        "violations": violations,
        "violationRate": round(violations / classified, 4) if classified else None,
        "finalClass": final,
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--k", type=int, default=50)
    ap.add_argument("--size-kib", type=int, default=1024)
    ap.add_argument("--writers", type=int, default=2)
    ap.add_argument("--scopes", nargs="*", default=["off", "workspace", "conflict"])
    ap.add_argument("--out", type=Path, default=Path(__file__).parent / "out")
    args = ap.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)

    results = []
    for scope in args.scopes:
        r = run_arm(scope, args.k, args.size_kib, args.writers)
        results.append(r)
        print(json.dumps(r), flush=True)
    verdict = {
        r["lock_scope"]: ("VIOLATED" if r["violations"] else "CLEAN") for r in results
    }
    payload = {"results": results, "verdict": verdict}
    (args.out / "e1-ablation.json").write_text(
        json.dumps(payload, indent=2), encoding="utf-8"
    )
    print(json.dumps(verdict, indent=2))


if __name__ == "__main__":
    main()
