#!/usr/bin/env python3
"""인터뷰 사건 후보 뽑기 — first-use study 진행자용 (비전문가 사용 전제).

세션 디렉토리(`turns.jsonl` + `history.jsonl`이 있는 폴더)를 주면, 인터뷰에서
물어볼 만한 사건 후보를 우선순위 순서로 한국어로 출력한다. 로그를 직접 읽을
필요가 없도록 만든 도구다 (`22-study-run-kit.md` §6-1).

사용:
  .venv/bin/python bench/study/pick_events.py study-data/team1/A
  .venv/bin/python bench/study/pick_events.py study-data/team1/C   # 모듈 C 는 전수 확인

출력의 시각은 "세션 시작 후 경과 시간(분:초)"이다 — 녹화 영상에서 해당 장면을
찾을 때 그대로 쓰면 된다.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path


def _load(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return out


def _fmt(ms: float) -> str:
    s = int(ms / 1000)
    return f"{s // 60}:{s % 60:02d}"


def main() -> None:
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    d = Path(sys.argv[1])
    turns = _load(d / "turns.jsonl")
    history = _load(d / "history.jsonl")
    if not turns and not history:
        sys.exit(
            f"{d} 에 turns.jsonl / history.jsonl 이 없다 — 세션 폴더가 맞는지 확인"
        )

    t0 = min((e["ts"] for e in turns if "ts" in e), default=0.0)

    def rel(e: dict) -> str:
        return _fmt(e.get("ts", t0) - t0)

    print(f"=== 인터뷰 사건 후보: {d} ===\n")

    # ── 1순위: 오염 — 한 파일을 두 사람의 턴이 건드림 ──────────────
    queries: dict[str, str] = {}
    files_by_owner: dict[str, set[str]] = {}
    for r in history:
        if r.get("kind") == "query" and r.get("id"):
            queries[r["id"]] = str(r.get("text", "")).replace("\n", " ")[:60]
        owner = r.get("reply_to")
        if owner:
            for p in r.get("files") or []:
                files_by_owner.setdefault(owner, set()).add(Path(str(p)).name)
    print("[1순위] 각 요청(질문)별로 실제로 건드린 파일:")
    for qid, text in sorted(queries.items()):
        fs = sorted(files_by_owner.get(qid, []))
        print(f'  요청 {qid} "{text}…"')
        print(f"    → 건드린 파일: {', '.join(fs) if fs else '(없음)'}")
    touched: dict[str, list[str]] = {}
    for owner, fs in files_by_owner.items():
        for f in fs:
            touched.setdefault(f, []).append(owner)
    dup = {f: os for f, os in touched.items() if len(set(os)) > 1}
    if dup:
        print(
            "  ⚠ 두 요청이 같은 파일을 건드림 (오염 의심 — 반드시 인터뷰에서 물을 것):"
        )
        for f, os in sorted(dup.items()):
            print(f"    {f} ← 요청 {', '.join(sorted(set(os)))}")
    else:
        print("  같은 파일을 두 요청이 건드린 경우: 없음")
        print("  ※ 상대 '영역'의 파일을 건드렸는지는 정답지(bench/study/README.md 의")
        print("     카드별 대상 파일 표)와 위 목록을 대조해서 판단한다.")

    # ── 2순위: 오래 기다린 요청 ────────────────────────────────────
    enq: dict = {}
    waits = []
    for e in turns:
        if e.get("event") != "turn":
            continue
        if e.get("phase") == "enqueue" and e.get("queue_id"):
            enq[e["queue_id"]] = e
        elif e.get("phase") == "dispatch" and e.get("queue_id") in enq:
            w = e["ts"] - enq[e["queue_id"]]["ts"]
            waits.append((w, enq[e["queue_id"]], e))
    waits.sort(reverse=True, key=lambda x: x[0])
    print("\n[2순위] 제출 후 처리 시작까지 오래 기다린 요청 상위 3:")
    if waits:
        for w, eq, _dp in waits[:3]:
            print(f"  {rel(eq)} 에 제출된 요청 → {_fmt(w)} 대기 후 시작")
    else:
        print("  (대기 없음 또는 기록 없음)")

    # ── 3순위: 중단(인터럽트)과 거부 ───────────────────────────────
    interrupts = [
        e for e in turns if e.get("event") == "turn" and e.get("phase") == "interrupt"
    ]
    rejects = [e for e in turns if e.get("event") == "reject"]
    print("\n[3순위] 중단·거부:")
    for e in interrupts:
        print(f"  {rel(e)} 사용자가 턴 {e.get('turn_id')} 을 중단시킴")
    for e in rejects:
        print(f"  {rel(e)} 요청이 거부됨 (conn={e.get('conn_id')})")
    if not interrupts and not rejects:
        print("  없음")

    # ── 4순위: 컨텍스트 압축 ───────────────────────────────────────
    compacts = [e for e in turns if e.get("event") == "compact"]
    print("\n[4순위] 컨텍스트 압축(요약) 발생 구간:")
    if compacts:
        for e in compacts:
            print(f"  {rel(e)} compact {e.get('phase')}")
    else:
        print("  없음")

    # ── 5순위: 두 사람의 턴이 가장 크게 겹친 구간 ──────────────────
    spans: dict = {}
    for e in turns:
        if e.get("event") != "turn" or not e.get("turn_id"):
            continue
        s = spans.setdefault(e["turn_id"], {})
        if e.get("phase") == "dispatch":
            s["a"] = e["ts"]
        elif e.get("phase") == "complete":
            s["b"] = e["ts"]
    ivs = [(v["a"], v["b"], k) for k, v in spans.items() if "a" in v and "b" in v]
    best = None
    for i in range(len(ivs)):
        for j in range(i + 1, len(ivs)):
            lo = max(ivs[i][0], ivs[j][0])
            hi = min(ivs[i][1], ivs[j][1])
            if hi > lo and (best is None or hi - lo > best[0]):
                best = (hi - lo, lo, ivs[i][2], ivs[j][2])
    print("\n[5순위] 두 턴이 가장 길게 동시에 돌던 구간:")
    if best:
        print(
            f"  {_fmt(best[1] - t0)} 부터 {_fmt(best[0])} 동안 (턴 {best[2]} ↔ 턴 {best[3]})"
        )
    else:
        print("  (겹침 없음 — 직렬 조건이면 정상)")

    print("\n=== 끝. 위에서 3~5개를 골라 킷 §6 의 질문 절차로 진행 ===")


if __name__ == "__main__":
    main()
