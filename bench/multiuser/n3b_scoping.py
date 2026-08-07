#!/usr/bin/env python3
"""N3b — 턴 스코핑(`--turn-scoping`)의 절제: 무엇을 재고 무엇을 못 재는가.

§6.7 은 공유 트랜스크립트에서 모델이 **남의 동시 질문에 답하는** 현상을
최악 38% 로 정량화했다. 리뷰의 요구는 완화 시도다. 완화는 프롬프트 수준
(각 턴의 시스템 프롬프트에 그 턴의 요청을 못 박기)이고, 여기서 정직하게
말해야 할 한계가 있다:

**목 모델로는 완화의 *효과*를 잴 수 없다.** 효과는 모델의 지시 순응이라는
성질이고, 목은 순응 여부를 우리가 코딩해 넣는 대상이기 때문이다. 그래서
이 실험은 효과가 아니라 **양 끝(bracket)** 을 잰다:

- ``ignore`` 팔 (기본 목): 시스템 프롬프트를 아예 읽지 않는 모델. 스코핑을
  켜도 내용 혼선은 그대로여야 한다 — 완화의 **최악 케이스 하한**.
- ``honor`` 팔 (``MOCK_LLM_HONOR_SCOPE=1``): 스코프를 따르는 모델. 혼선이
  0 이어야 한다 — 메커니즘이 **원리상 충분함**의 상한.

두 팔 사이의 실제 지점은 실모델만 답할 수 있고, 그것은 라이브 팔의 몫이다.

이 실험이 실제로 **증명**하는 것은 따로 있고 그게 사소하지 않다: 동시 턴
각각의 프롬프트에 **자기 자신의** 요청이 실렸는가. 세션 전역 필드로 귀속을
관리하던 최초 구현이 동시 3턴에서 전부 마지막 질의를 가리켰던 그 버그
(§5 의 thread-local 수리)와 정확히 같은 실패 표면이다. ``honor`` 팔의
혼선 0 은 곧 "스코프가 턴별로 옳게 도달했다"는 종단 확인이다.

사용: .venv/bin/python bench/multiuser/n3b_scoping.py [--users 4] [--rounds 25]
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import tempfile
import time
from pathlib import Path

from driver import AgentServer, MockLlm

MARKER_RE = re.compile(r"id=([A-Za-z0-9-]+)")


def _history(session_dir: Path) -> list[dict]:
    path = session_dir / "history.jsonl"
    return [
        json.loads(x)
        for x in path.read_text(encoding="utf-8").splitlines()
        if x.strip()
    ]


def run_arm(arm: str, users: int, rounds: int, rep: int) -> dict:
    """arm ∈ {off, ignore, honor}.

    - ``off``   스코핑 미적용 = §6.7 의 구성 그대로인 **진짜 기준선**.
    - ``ignore`` 스코핑 적용 + 시스템 프롬프트를 안 읽는 목 = 완화 불가 하한.
    - ``honor``  스코핑 적용 + 스코프를 따르는 목 = 메커니즘 충분성 상한.
    """
    env = {"MOCK_LLM_HONOR_SCOPE": "1"} if arm == "honor" else {}
    mock = MockLlm(env=env)
    ws = Path(tempfile.mkdtemp(prefix=f"n3b-{arm}-{rep}-"))
    server = AgentServer(
        ws,
        mock.port,
        contract="parallel",
        max_turns=users,
        extra=[] if arm == "off" else ["--turn-scoping"],
    )
    total = users * rounds
    try:
        for r in range(rounds):
            for u in range(users):
                marker = f"m{u}-{r}"
                msg = f"question {marker} [[bench ttft=30 tok=1 n=6 id={marker}]]"
                assert server.chat(msg, f"user{u}") == 200
            time.sleep(0.05)  # 라운드 간 미세 시차 — 큐 혼합 유지
        server.wait_completes(total, timeout=600)
        records = _history(server.session_dir)
    finally:
        server.stop()
        mock.stop()
        shutil.rmtree(ws, ignore_errors=True)

    # 채점은 N3 와 **동일한 지표**여야 한다 — 그래야 ignore 팔이 §6.7 의
    # 기준선을 재현하고 두 팔의 차이가 스코핑에만 귀속된다.
    queries = {r["id"]: r for r in records if r.get("kind") == "query"}
    finals = [r for r in records if r.get("kind") == "final"]

    # 구조 귀속: reply_to 전단사 (스코핑이 이걸 깨뜨리지 않았는지 확인).
    seen: dict[str, int] = {}
    for f in finals:
        seen[f.get("reply_to")] = seen.get(f.get("reply_to"), 0) + 1
    duplicates = sum(1 for c in seen.values() if c > 1)
    unmatched = sum(1 for k in seen if k not in queries)

    # 내용 일치: 실제로 답한 마커가 reply_to 질문의 마커인가.
    checked = mismatch = 0
    for f in finals:
        m = MARKER_RE.search(str(f.get("text", "")))
        q = queries.get(f.get("reply_to"))
        if m is None or q is None:
            continue
        checked += 1
        if m.group(1) not in str(q.get("text", "")):
            mismatch += 1
    return {
        "arm": arm,
        "rep": rep,
        "users": users,
        "rounds": rounds,
        "queries": len(queries),
        "finals": len(finals),
        "replyToDuplicates": duplicates,
        "replyToUnmatched": unmatched,
        "contentChecked": checked,
        "answeredOtherUsersQuestion": mismatch,
        "mismatchRate": round(mismatch / checked, 4) if checked else None,
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--users", type=int, default=4)
    ap.add_argument("--rounds", type=int, default=25)
    ap.add_argument("--reps", type=int, default=5)
    ap.add_argument("--out", type=Path, default=Path(__file__).parent / "out")
    args = ap.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)

    t0 = time.time()
    rows: list[dict] = []
    # 반복이 필수인 이유(실측): 같은 구성의 재실행에서 내용 혼선률이 10% 와
    # 38% 사이를 오간다. 혼선은 턴이 실제로 얼마나 겹쳤는지의 함수이고
    # 그것은 호스트 스케줄링의 성질이라, 단발 측정은 팔 사이의 차이와
    # 실행 간 변동을 구분하지 못한다.
    for rep in range(1, args.reps + 1):
        for arm in ("off", "ignore", "honor"):
            row = run_arm(arm, args.users, args.rounds, rep)
            rows.append(row)
            print(json.dumps(row), flush=True)

    arms = []
    for arm in ("off", "ignore", "honor"):
        vals = sorted(r["mismatchRate"] for r in rows if r["arm"] == arm)
        arms.append(
            {
                "arm": arm,
                "reps": len(vals),
                "mismatchMin": vals[0] if vals else None,
                "mismatchMedian": vals[len(vals) // 2] if vals else None,
                "mismatchMax": vals[-1] if vals else None,
                "replyToDuplicates": sum(
                    r["replyToDuplicates"] for r in rows if r["arm"] == arm
                ),
                "replyToUnmatched": sum(
                    r["replyToUnmatched"] for r in rows if r["arm"] == arm
                ),
            }
        )
    summary = {
        "users": args.users,
        "rounds": args.rounds,
        "reps": args.reps,
        "arms": arms,
        "runs": rows,
        "note": (
            "off = no scoping (the §6.7 baseline configuration). ignore = "
            "scoping on but the model never reads the system prompt (worst "
            "case: the mitigation cannot possibly help). honor = scoping on "
            "and the model follows it (upper bound: the mechanism suffices in "
            "principle). A mock cannot measure real compliance, which sits "
            "between and needs a live model. The off/ignore spread is "
            "run-to-run variance in how much turns actually overlap, not an "
            "effect of scoping."
        ),
        "elapsed_s": round(time.time() - t0, 1),
    }
    (args.out / "n3b-scoping.json").write_text(
        json.dumps(summary, indent=2), encoding="utf-8"
    )
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
