#!/usr/bin/env python3
"""P7 — 장기 세션 수명주기: 다중 사용자 200+턴 + suspend/resume + 압축 지속.

U5(오픈 커뮤니티 스레드) 대응: 세션이 간헐적 활동으로 오래 살아남는가.
이 시스템은 토큰 예산 압축을 갖고 있으므로 질문은 포화 시점이 아니다 —
**압축이 있는 공유 세션은 장기 운행에서 유계를 유지하며, suspend→resume 이
무손실인가.**

프로토콜: 3 사용자 × 4 페이즈 × 17 라운드(= 204 턴), 페이즈 사이마다
서버 종료 → ``--resume <id>`` 재기동. 목 LLM 창 16384 로 압축 유발.

검증 축:
  보존   : 재개 직후 history 의 query 수 == 지금까지 보낸 수, id 전역 유일
           (resume 이 카운터를 잘못 이어받으면 u{n} 재발급 → 중복).
  지속   : 재개 후 새 턴이 정상 완료 (각 페이즈가 곧 그 증거).
  유계   : 압축 커밋이 전 구간에서 발생하고 tokens_after 가 예산 내 —
           세션이 포화로 죽지 않는다.

사용: .venv/bin/python bench/multiuser/p7_lifecycle.py [--phases 4] [--rounds 17]
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

from driver import AgentServer, MockLlm

USERS = 3


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
    """실모델용 — 목의 산문 응답 턴과 같은 역할(도구 없이 컨텍스트만 채운다)."""
    return (
        f"Task id={marker}: without using any tool, write two sentences about "
        "why a shared agent session needs durable history. Then call complete "
        f"with a result that starts with the exact text 'id={marker}'."
    )


def read_history(session_dir: Path) -> list[dict]:
    path = session_dir / "history.jsonl"
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


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--phases", type=int, default=4)
    ap.add_argument("--rounds", type=int, default=17, help="페이즈당 라운드(×3 사용자)")
    ap.add_argument("--ctx", type=int, default=16384)
    ap.add_argument(
        "--real",
        action="store_true",
        help="목 대신 실모델 — 실제 세션이 중단·재개를 관통하는지 확인한다 "
        "(204턴 라이브는 비현실적이라 --phases/--rounds 를 줄여 쓴다).",
    )
    ap.add_argument("--out", type=Path, default=Path(__file__).parent / "out")
    args = ap.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    llm = real_llm_from_env() if args.real else None

    mock = (
        MockLlm(env={"MOCK_LLM_CTX": str(args.ctx), "MOCK_LLM_SUM_MS": "200"})
        if llm is None
        else None
    )
    ws = Path(tempfile.mkdtemp(prefix="p7-life-"))
    sent = 0
    session_id: str | None = None
    phases_report = []
    try:
        for phase in range(1, args.phases + 1):
            server = AgentServer(
                ws,
                None if mock is None else mock.port,
                contract="parallel",
                max_turns=USERS,
                resume=session_id,
                real_llm=llm,
            )
            try:
                if session_id is None:
                    session_id = server.session_dir.name
                assert server.session_dir.name == session_id, (
                    f"resume 이 다른 세션을 열었다: {server.session_dir.name}"
                )
                for r in range(args.rounds):
                    for u in range(USERS):
                        marker = f"p{phase}-{u}-{r}"
                        msg = (
                            f"talk {marker} [[bench ttft=10 tok=0 n=400 id={marker}]]"
                            if llm is None
                            else live_task(marker)
                        )
                        assert server.chat(msg, f"user{u}") == 200
                        sent += 1
                    time.sleep(0.03)
                # turns.jsonl 은 resume 을 관통해 누적된다 — 전체 파일 기준
                # complete 수가 지금까지 보낸 총수에 도달할 때까지 대기.
                server.wait_completes(sent, timeout=600 if llm is None else 3600)

                hist = read_history(server.session_dir)
                q_ids = [r["id"] for r in hist if r.get("kind") == "query"]
                events = server.events()
                compacts = [e for e in events if e.get("event") == "compact"]
                phases_report.append(
                    {
                        "phase": phase,
                        "sent_cumulative": sent,
                        "queries_recorded": len(q_ids),
                        "query_ids_unique": len(set(q_ids)) == len(q_ids),
                        "compact_committed_cumulative": sum(
                            1 for c in compacts if c["phase"] == "commit"
                        ),
                        "compact_stale_cumulative": sum(
                            1 for c in compacts if c["phase"] == "stale"
                        ),
                        "compact_failed_cumulative": sum(
                            1 for c in compacts if c["phase"] == "failed"
                        ),
                        "max_tokens_after": max(
                            (
                                c["tokens_after"]
                                for c in compacts
                                if c["phase"] == "commit"
                            ),
                            default=None,
                        ),
                    }
                )
                print(json.dumps(phases_report[-1]), flush=True)
                final_session = server.session_dir
            finally:
                server.stop()  # suspend — 다음 페이즈가 --resume 으로 재개

        hist = read_history(final_session)
        q_ids = [r["id"] for r in hist if r.get("kind") == "query"]
        result = {
            "users": USERS,
            "phases": args.phases,
            "rounds_per_phase": args.rounds,
            "turns_sent": sent,
            "resumes": args.phases - 1,
            "queries_recorded": len(q_ids),
            "queries_lost": sent - len(q_ids),
            "query_ids_unique_across_resumes": len(set(q_ids)) == len(q_ids),
            "phase_reports": phases_report,
        }
        print(json.dumps(result, indent=2, ensure_ascii=False))
        (
            args.out / ("p7-lifecycle-real.json" if args.real else "p7-lifecycle.json")
        ).write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    finally:
        if mock is not None:
            mock.stop()
        shutil.rmtree(ws, ignore_errors=True)


if __name__ == "__main__":
    main()
