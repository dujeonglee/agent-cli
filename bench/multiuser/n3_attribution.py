#!/usr/bin/env python3
"""N3 — 병렬 턴의 결정적 응답↔질문 귀속(reply_to): 구조 정확도 + 한계 정량화.

논문 초안의 한계 (9) "병렬 귀속은 휴리스틱"을 기여로 뒤집는 실험. 본류의
귀속은 threading.local 기반 **구조적 메커니즘**(A6×A1)이라 모델 판단과
무관하게 결정적이다 — 검증도 구조적으로 한다. 두 지표를 분리한다:

① **구조 귀속 (A6 의 주장 — 가설: 완전)**
   - 발급 정합: 각 턴 스레드가 **자기 큐 항목의** 질의 id 를 발급했는가.
     사슬 = enqueue(conn, queue_id) → dispatch(turn_id, queue_id) →
     query_added(msg_id, thread=agent-turn-{turn_id}) → history query(text 마커).
     conn 별 k번째 enqueue 는 k번째 마커와 대응(제출 순서 = FIFO 큐 순서).
   - 응답 전단사: final 레코드들의 reply_to 가 질의 id 전체와 1:1 (중복/누락 0).

② **내용 일치 (알려진 한계의 정량화 — 트랜스크립트 공유 효과)**
   final 이 실제로 답한 질문(목 LLM 결과의 id 마커)이 reply_to 의 질문과
   같은가. 목은 스냅샷의 **가장 최신** 지시자에 답하도록 결정적이므로 이
   수치는 "모델이 남의 질문에 답하는" 현상의 **최악 케이스** 발생률이다 —
   reply_to 는 그때에도 "이 응답이 어느 요청의 처리 중 생성됐는가"를
   정확히 남긴다(①이 그것을 증명한다).

사용: .venv/bin/python bench/multiuser/n3_attribution.py [--users 4] [--rounds 25]
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import tempfile
import time
from collections import Counter
from pathlib import Path

from driver import AgentServer, MockLlm

MARKER_RE = re.compile(r"id=([A-Za-z0-9-]+)")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--users", type=int, default=4)
    ap.add_argument("--rounds", type=int, default=25)
    ap.add_argument("--out", type=Path, default=Path(__file__).parent / "out")
    args = ap.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)

    mock = MockLlm()
    ws = Path(tempfile.mkdtemp(prefix="n3-attr-"))
    server = AgentServer(ws, mock.port, contract="parallel", max_turns=args.users)
    total = args.users * args.rounds
    try:
        for r in range(args.rounds):
            for u in range(args.users):
                marker = f"m{u}-{r}"
                msg = f"question {marker} [[bench ttft=30 tok=1 n=6 id={marker}]]"
                assert server.chat(msg, f"user{u}") == 200
            time.sleep(0.05)  # 라운드 간 미세 시차 — 큐 혼합 유지
        events = server.wait_completes(total, timeout=600)

        hist = [
            json.loads(x)
            for x in (server.session_dir / "history.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
            if x.strip()
        ]
        queries = {r["id"]: r for r in hist if r.get("kind") == "query"}
        finals = [r for r in hist if r.get("kind") == "final"]

        # ── ① 발급 정합: enqueue→dispatch→query_added→query(text) 사슬 ──
        # conn 별 enqueue 순서 = 제출한 마커 순서 (드라이버가 순서대로 보냄).
        enq_by_conn: dict[str, list[dict]] = {}
        for e in events:
            if e.get("event") == "turn" and e.get("phase") == "enqueue":
                enq_by_conn.setdefault(e.get("conn_id", ""), []).append(e)
        queue_to_marker: dict[str, str] = {}
        for conn, lst in enq_by_conn.items():
            if not conn.startswith("user"):
                continue
            u = int(conn.removeprefix("user"))
            lst.sort(key=lambda e: e["mono_ms"])
            for k, e in enumerate(lst):
                queue_to_marker[e.get("queue_id", "")] = f"m{u}-{k}"
        turn_to_queue = {
            e.get("turn_id"): e.get("queue_id")
            for e in events
            if e.get("event") == "turn" and e.get("phase") == "dispatch"
        }
        mint_checked = mint_errors = 0
        for e in events:
            if e.get("event") != "turn" or e.get("phase") != "query_added":
                continue
            thread = e.get("thread", "")
            if not thread.startswith("agent-turn-"):
                continue
            tid = thread.removeprefix("agent-turn-")
            expected = queue_to_marker.get(turn_to_queue.get(tid, ""), None)
            q = queries.get(e.get("msg_id"))
            if expected is None or q is None:
                continue
            mint_checked += 1
            if expected not in str(q.get("text", "")):
                mint_errors += 1

        # ── ① 응답 전단사: finals.reply_to ↔ 질의 id 1:1 ──
        reply_counts = Counter(f.get("reply_to") for f in finals)
        duplicates = sum(1 for c in reply_counts.values() if c > 1)
        unmatched = sum(1 for k in reply_counts if k not in queries)
        missing = len(queries) - sum(1 for k in reply_counts if k in queries)

        # ── ② 내용 일치 (알려진 한계 정량화) ──
        content_checked = content_mismatch = 0
        for f in finals:
            m = MARKER_RE.search(str(f.get("text", "")))
            q = queries.get(f.get("reply_to"))
            if m is None or q is None:
                continue
            content_checked += 1
            if m.group(1) not in str(q.get("text", "")):
                content_mismatch += 1

        result = {
            "users": args.users,
            "rounds": args.rounds,
            "turns": total,
            "structural": {
                "query_ids_unique": len(queries) == total,
                "mint_chain_checked": mint_checked,
                "mint_chain_errors": mint_errors,
                "finals": len(finals),
                "reply_to_duplicates": duplicates,
                "reply_to_unmatched": unmatched,
                "queries_without_final": missing,
            },
            "content_match_known_limitation": {
                "checked": content_checked,
                "answered_other_users_question": content_mismatch,
                "rate": round(content_mismatch / content_checked, 4)
                if content_checked
                else None,
                "note": "결정적 목이 '항상 최신 질문에 답함' — 최악 케이스 상한",
            },
        }
        print(json.dumps(result, indent=2, ensure_ascii=False))
        (args.out / "n3-attribution.json").write_text(
            json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8"
        )
    finally:
        server.stop()
        mock.stop()
        shutil.rmtree(ws, ignore_errors=True)


if __name__ == "__main__":
    main()
