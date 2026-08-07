#!/usr/bin/env python3
"""N3c — 턴 스코핑이 **실모델**에서 남의 과제 수행을 줄이는가.

`n3b_scoping.py` 는 목으로 양 끝(bracket)만 쟀다: 지시를 안 읽는 모델은
그대로, 따르는 모델은 0. 그 사이 어디에 실모델이 앉는지는 목이 답할 수
없다 — 순응이 시험 대상인데 목의 순응은 우리가 코딩하는 것이기 때문이다.
이 스크립트가 그 열린 절반을 담당한다.

**측정 대상은 답변의 내용이 아니라 부수효과다.** §6.4 가 실모델에서 관측한
현상(한 턴이 자기 파일 대신 상대의 파일을 썼다, 12회 중 1회)이 그대로
지표가 된다 — 파일 이름은 객관적이라 판정에 모델도 사람도 필요 없다.

구성은 §6.4 의 실모델 팔과 같되 **짧게** 만들었다. 거기서는 턴 하나가 4분
가까이 걸려 12 회가 한계였고, 8% 근처의 기저율을 그 표본으로는 두 팔 사이에서
구분할 수 없다. 그래서 쓰기 횟수와 줄 수를 줄여 반복을 벌었다. 두 사용자의
지시는 태그와 파일 이름만 다른 **혼선 최악 조건** 그대로 둔다 — 완화책을
시험하는 자리에서 조건을 쉽게 만들면 아무것도 증명하지 못한다.

지표:
  cross_task   한 턴이 상대의 파일을 썼는가 (완화 대상)
  own_complete 각 턴이 자기 파일을 전부 썼는가 (완화가 과제 수행을 망치지
               않았는지 — 스코핑이 모델을 과하게 위축시키면 여기서 드러난다)

사용: AGENT_CLI_BASE_URL/API_KEY/MODEL 설정 후
  .venv/bin/python bench/multiuser/n3c_scoping_real.py [--reps 12]
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
import threading
import time
from pathlib import Path

from driver import AgentServer, turn_chain

#: §6.4 는 4회×40줄이라 턴당 ~4분이었다. 반복을 벌기 위해 줄인다.
WRITES_PER_TURN = 2
LINES = 8


def task_for(tag: str, target: str) -> str:
    return (
        f"Use the write_file tool {WRITES_PER_TURN} times, once for each of the "
        f"files {', '.join(f'{target}{i}.txt' for i in range(1, WRITES_PER_TURN + 1))}. "
        f"Each file must contain exactly {LINES} lines, and every line must be "
        f"'{tag} line N of {LINES}' with N replaced by the line number. "
        "Do not read any file. Do not use the shell. "
        f"When all {WRITES_PER_TURN} files are written, call complete with result "
        f"'{tag} done'."
    )


def real_llm_from_env() -> dict:
    try:
        return {
            "base_url": os.environ["AGENT_CLI_BASE_URL"],
            "api_key": os.environ["AGENT_CLI_API_KEY"],
            "model": os.environ["AGENT_CLI_MODEL"],
        }
    except KeyError as e:
        sys.exit(f"missing env {e} — set AGENT_CLI_BASE_URL/API_KEY/MODEL")


def _want(stem: str) -> set[str]:
    return {f"{stem}{i}.txt" for i in range(1, WRITES_PER_TURN + 1)}


def _read_history(session_dir: Path) -> list[dict]:
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


def _attribute(records: list[dict]) -> list[dict]:
    """질의별로 (그 질의를 처리하던 턴이) 실제로 쓴 파일 집합.

    `reply_to` 는 "이 레코드가 어느 요청을 처리하는 동안 생겼는가"이고
    `files` 는 그 레코드가 만진 경로다(둘 다 `_enrich_record` 가 붙인다).
    질의 본문에서 목표 stem 을 읽어 오는 이유는 지시문이 그 이름을 담고
    있어서다 — 별도 매핑을 들고 다닐 필요가 없다.
    """
    targets: dict[str, str] = {}
    for r in records:
        if r.get("kind") == "query" and r.get("id"):
            text = str(r.get("text", ""))
            for stem in ("alpha", "beta"):
                if f"{stem}1.txt" in text:
                    targets[r["id"]] = stem
                    break
    files: dict[str, set[str]] = {q: set() for q in targets}
    for r in records:
        owner = r.get("reply_to")
        if owner not in files:
            continue
        for p in r.get("files") or []:
            files[owner].add(Path(str(p)).name)
    return [
        {"query": q, "target": targets[q], "files": files[q]} for q in sorted(targets)
    ]


def run_rep(llm: dict, scoping: bool, rep: int) -> dict | None:
    ws = Path(tempfile.mkdtemp(prefix=f"n3c-{'on' if scoping else 'off'}-{rep}-"))
    server = AgentServer(
        ws,
        None,
        contract="parallel",
        max_turns=2,
        real_llm=llm,
        extra=["--turn-scoping"] if scoping else [],
    )
    a_conn, b_conn = f"A-{rep}", f"B-{rep}"
    try:
        before = len(server.events())
        results: dict[str, int] = {}
        gate = threading.Barrier(2)

        def submit(conn: str, tag: str, target: str) -> None:
            gate.wait()
            results[conn] = server.chat(task_for(tag, target), conn)

        threads = [
            threading.Thread(target=submit, args=(a_conn, "AAA", "alpha")),
            threading.Thread(target=submit, args=(b_conn, "BBB", "beta")),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        if set(results.values()) != {200}:
            return None
        events = server.wait_completes_since(before, 2, timeout=900)
        ca, cb = turn_chain(events, a_conn), turn_chain(events, b_conn)
        if None in (ca["dispatch"], ca["complete"], cb["dispatch"], cb["complete"]):
            return None

        # **턴별 귀속으로 판정한다.** "워크스페이스에 한쪽 파일만 있다"로
        # 추론하면 *남의 과제를 했다* 와 *자기 과제에 실패했다* 가 구분되지
        # 않는다 — 완화책을 시험하는 자리에서 그 둘을 섞으면 측정이 무의미
        # 하다. history.jsonl 의 레코드는 `reply_to`(어느 요청을 처리 중이던
        # 턴인가)와 `files`(그 레코드가 만진 경로)를 함께 들고 있으므로,
        # 둘을 조인하면 "이 턴이 실제로 어느 파일을 썼는가"가 나온다.
        turns = _attribute(_read_history(server.session_dir))
        if len(turns) != 2:
            return None  # 두 질의가 다 기록되지 않았다면 판정 불가
        per_turn = []
        for t in turns:
            own, other = (
                _want(t["target"]),
                _want("beta" if t["target"] == "alpha" else "alpha"),
            )
            per_turn.append(
                {
                    "target": t["target"],
                    "wrote": sorted(t["files"]),
                    "ownComplete": own <= t["files"],
                    "wroteOthers": bool(t["files"] & other),
                }
            )
        return {
            "scoping": "on" if scoping else "off",
            "rep": rep,
            "spanA_ms": round(ca["complete"] - ca["dispatch"], 1),
            "spanB_ms": round(cb["complete"] - cb["dispatch"], 1),
            "turns": per_turn,
            # 완화 대상: 어느 턴이든 남의 파일을 건드렸는가.
            "crossTask": any(t["wroteOthers"] for t in per_turn),
            # 반대 방향 가드: 스코핑이 모델을 위축시켜 제 일을 못 하게
            # 만들지는 않았는가.
            "bothComplete": all(t["ownComplete"] for t in per_turn),
        }
    finally:
        server.stop()
        shutil.rmtree(ws, ignore_errors=True)


def summarize(rows: list[dict]) -> list[dict]:
    arms = []
    for arm in ("off", "on"):
        sub = [r for r in rows if r["scoping"] == arm]
        n = len(sub)
        cross = sum(1 for r in sub if r["crossTask"])
        both = sum(1 for r in sub if r["bothComplete"])
        turns = [t for r in sub for t in r["turns"]]
        arms.append(
            {
                "scoping": arm,
                "reps": n,
                "turns": len(turns),
                "crossTask": cross,
                "crossTaskRate": round(cross / n, 4) if n else None,
                # 턴 단위 비율도 함께 — rep 단위는 "둘 중 하나라도" 라
                # 표본이 절반이 된다.
                "turnsWroteOthers": sum(1 for t in turns if t["wroteOthers"]),
                "turnsOwnComplete": sum(1 for t in turns if t["ownComplete"]),
                "bothComplete": both,
                "bothCompleteRate": round(both / n, 4) if n else None,
                "medianSpanMs": round(sorted(r["spanA_ms"] for r in sub)[n // 2], 1)
                if n
                else None,
            }
        )
    return arms


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--reps", type=int, default=12)
    ap.add_argument("--out", type=Path, default=Path(__file__).parent / "out")
    ap.add_argument(
        "--rederive",
        action="store_true",
        help="실행 없이 커밋된 원시 JSONL 에서 요약만 재도출 (리포 규약).",
    )
    args = ap.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    raw_path = args.out / "n3c-scoping-real.jsonl"

    if args.rederive:
        rows = [
            json.loads(x)
            for x in raw_path.read_text(encoding="utf-8").splitlines()
            if x.strip()
        ]
    else:
        llm = real_llm_from_env()
        rows = []
        t0 = time.time()
        # 팔을 rep 단위로 번갈아 돈다 — 한 팔을 몰아서 돌리면 그 사이의
        # 서버 부하 변화가 통째로 팔 사이 차이로 오인된다.
        for rep in range(1, args.reps + 1):
            for scoping in (False, True):
                row = run_rep(llm, scoping, rep)
                if row is None:
                    continue
                rows.append(row)
                print(json.dumps(row), flush=True)
        raw_path.write_text(
            "".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8"
        )
        print(f"# elapsed {round(time.time() - t0, 1)}s", flush=True)

    summary = {
        "writesPerTurn": WRITES_PER_TURN,
        "lines": LINES,
        "arms": summarize(rows),
        "note": (
            "crossTask = one turn carried out the other user's task (wrote "
            "their files instead of its own), judged from filenames alone. "
            "The two instructions differ only in a tag and a filename, which "
            "is close to the worst case for confusion and is kept that way "
            "deliberately. bothComplete guards the other direction: scoping "
            "must not make the model so cautious that it stops doing its job."
        ),
    }
    (args.out / "n3c-scoping-real.json").write_text(
        json.dumps(summary, indent=2), encoding="utf-8"
    )
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
