#!/usr/bin/env python3
"""N1 — 동시 턴 하의 낙관적 컨텍스트 압축: 정확성 + 가용성.

논문 초안의 한계 (2) "compaction 부재"를 기여로 뒤집는 실험 — 관련 연구에
"동시 사용자 턴이 도는 중의 공유 대화 압축"은 부재(2026-08 서베이). 본류의
낙관적 3단계 압축(무락 요약 → 세대 재검증 커밋 + 꼬리 흡수)이 대상이다.

방법: 목 LLM 의 컨텍스트 창을 작게(MOCK_LLM_CTX) 광고해 압축을 유발하고,
요약 지연(MOCK_LLM_SUM_MS)으로 무락 구간을 늘린 상태에서 N 사용자가 장문
응답 턴을 계속 흘린다. turns.jsonl 로 판정:

  가용성  : compact begin↔commit 창 안에서 **다른 턴의 이벤트**(first_token/
            dispatch/complete)가 계속 발생하는가 — 배리어라면 0 이어야 할 값.
  재시도  : phase=stale 계수 (낙관적 설계의 비용).
  정확성  : 전체 query 유실 0 (history.jsonl 의 query 수 == 보낸 수),
            final 의 reply_to 정합 (N3 과 같은 마커 검사).

사용: .venv/bin/python bench/multiuser/n1_compaction.py [--users 3] [--rounds 30]
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import tempfile
import time
from pathlib import Path

from driver import AgentServer, MockLlm


def real_llm_from_env() -> dict:
    try:
        return {
            "base_url": os.environ["AGENT_CLI_BASE_URL"],
            "api_key": os.environ["AGENT_CLI_API_KEY"],
            "model": os.environ["AGENT_CLI_MODEL"],
        }
    except KeyError as e:
        sys.exit(f"missing env {e} — set AGENT_CLI_BASE_URL/API_KEY/MODEL")


def live_task(marker: str) -> str:
    """실모델용 — 컨텍스트를 착실히 채우는 산문 응답 한 턴.

    목 팔의 `n=400` 스크립트와 같은 역할이다. 도구를 쓰지 않게 해 압축과
    동시 턴의 상호작용만 남긴다(도구 스텝은 §6.4 가 따로 잰다)."""
    return (
        f"Task id={marker}: without using any tool, write a short paragraph of "
        "about six sentences explaining why ordering side effects is easier "
        "than ordering inference in a shared agent session. Then call complete "
        f"with a result that starts with the exact text 'id={marker}' followed "
        "by that paragraph."
    )


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--users", type=int, default=3)
    ap.add_argument("--rounds", type=int, default=30)
    ap.add_argument("--ctx", type=int, default=16384, help="목이 광고할 컨텍스트 창")
    ap.add_argument("--sum-ms", type=int, default=800, help="요약 콜 지연(무락 구간)")
    ap.add_argument(
        "--real",
        action="store_true",
        help="목 대신 실모델 — 요약 콜이 진짜로 수 초 걸리는 조건에서 "
        "무락 요약 구간의 가용성을 확인한다. 압축은 --max-context-tokens 로 강제.",
    )
    ap.add_argument(
        "--budget",
        type=int,
        default=3000,
        help="--real 일 때 압축을 유발할 컨텍스트 토큰 예산",
    )
    ap.add_argument("--out", type=Path, default=Path(__file__).parent / "out")
    args = ap.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    llm = real_llm_from_env() if args.real else None

    mock = (
        MockLlm(
            env={"MOCK_LLM_CTX": str(args.ctx), "MOCK_LLM_SUM_MS": str(args.sum_ms)}
        )
        if llm is None
        else None
    )
    ws = Path(tempfile.mkdtemp(prefix="n1-compact-"))
    server = AgentServer(
        ws,
        None if mock is None else mock.port,
        contract="parallel",
        max_turns=args.users,
        real_llm=llm,
        # 실모델의 광고 창은 262K 라 압축이 영영 안 걸린다. 압축 예산을 직접
        # 조여 목 팔과 같은 압력을 만든다 — 재는 것은 창의 크기가 아니라
        # "요약이 도는 동안 턴이 계속 흐르는가"이므로 이 대체가 유효하다.
        extra=["--max-context-tokens", str(args.budget)] if llm else None,
    )
    total = args.users * args.rounds
    timeout = 900 if llm is None else 3600
    try:
        for r in range(args.rounds):
            for u in range(args.users):
                marker = f"c{u}-{r}"
                # n=400/tok=1 → 응답 ~400자, 완만한 스트리밍 — 컨텍스트를
                # 착실히 채우되 적대적 폭주는 아님 (적대 팔은 tok=0 n=600 을
                # 인자로 별도 실행: out/n1-compaction-adversarial.json).
                msg = (
                    f"talk {marker} [[bench ttft=20 tok=1 n=400 id={marker}]]"
                    if llm is None
                    else live_task(marker)
                )
                assert server.chat(msg, f"user{u}") == 200
            time.sleep(0.05)
        events = server.wait_completes(total, timeout=timeout)

        compacts = [e for e in events if e.get("event") == "compact"]
        begins = [e for e in compacts if e["phase"] == "begin"]
        commits = [e for e in compacts if e["phase"] == "commit"]
        stales = [e for e in compacts if e["phase"] == "stale"]
        faileds = [e for e in compacts if e["phase"] == "failed"]

        # 가용성: 각 begin→commit/stale 창에서 다른 턴 이벤트 수
        windows = []
        for b in begins:
            end = next(
                (
                    c["mono_ms"]
                    for c in compacts
                    if c["phase"] in ("commit", "stale", "failed")
                    and c["generation"] == b["generation"]
                    and c["mono_ms"] >= b["mono_ms"]
                ),
                None,
            )
            if end is None:
                continue
            inside = [
                e
                for e in events
                if e.get("event") == "turn" and b["mono_ms"] < e["mono_ms"] < end
            ]
            windows.append(
                {
                    "generation": b["generation"],
                    "duration_ms": round(end - b["mono_ms"], 1),
                    "active_turns_at_begin": b.get("active_turns"),
                    "turn_events_inside": len(inside),
                    "first_tokens_inside": sum(
                        1 for e in inside if e.get("phase") == "first_token"
                    ),
                }
            )

        hist = [
            json.loads(x)
            for x in (server.session_dir / "history.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
            if x.strip()
        ]
        queries = {r["id"]: r for r in hist if r.get("kind") == "query"}
        finals = [r for r in hist if r.get("kind") == "final"]
        checked = mismatches = 0
        for f in finals:
            m = re.search(r"id=([A-Za-z0-9-]+)", str(f.get("text", "")))
            q = queries.get(f.get("reply_to"))
            if m is None or q is None:
                continue
            checked += 1
            if m.group(1) not in str(q.get("text", "")):
                mismatches += 1

        result = {
            "users": args.users,
            "rounds": args.rounds,
            "model": "mock" if llm is None else llm["model"],
            "ctx_advertised": args.ctx if llm is None else None,
            "context_budget_tokens": args.budget if llm else None,
            "summary_delay_ms": args.sum_ms if llm is None else "real summarizer call",
            "turns_sent": total,
            "queries_recorded": len(queries),
            "queries_lost": total - len(queries),
            "compactions": {
                "begun": len(begins),
                "committed": len(commits),
                "stale_retries": len(stales),
                "failed": len(faileds),
            },
            "availability_windows": windows,
            "concurrent_compactions": sum(
                1 for w in windows if (w["active_turns_at_begin"] or 0) > 0
            ),
            "attribution_checked": checked,
            "attribution_mismatches": mismatches,
        }
        print(json.dumps(result, indent=2, ensure_ascii=False))
        name = "n1-compaction-real.json" if args.real else "n1-compaction.json"
        (args.out / name).write_text(
            json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8"
        )
    finally:
        server.stop()
        if mock is not None:
            mock.stop()
        shutil.rmtree(ws, ignore_errors=True)


if __name__ == "__main__":
    main()
