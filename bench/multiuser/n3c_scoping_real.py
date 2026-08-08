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
구분할 수 없다. 그래서 쓰기 횟수와 줄 수를 줄여 반복을 벌었다.

**워크로드 축 (`--workload`)** — 리뷰 R2-W3 대응:

  confusable  두 지시가 태그와 파일 이름만 다르다. 혼선 **최악 조건**이며,
              완화책을 시험하는 자리에서 조건을 쉽게 만들면 아무것도
              증명하지 못하므로 완화 실험(off/on)의 기본값이다.
  realistic   두 지시가 실제로 다른 일이다 — 다른 주제(파서 토큰 정의 대
              사용자 문서), 다른 파일 이름, 다른 줄 내용, 다른 완료 태그.
              "정상적으로 구분되는 작업에서는 혼선이 얼마나 나는가"라는
              배치 관점의 질문에 답한다.

  두 워크로드는 **작업량의 모양을 공유한다**(파일 2개 × 8줄). 그래야 스팬과
  표본이 비교 가능하기 때문이며, 따라서 이 축이 바꾸는 것은 작업의 크기가
  아니라 **지시가 서로 헷갈릴 만한가**뿐이다. 그것이 재려는 변수다.

지표:
  cross_task   한 턴이 상대의 파일을 썼는가 (완화 대상)
  own_complete 각 턴이 자기 파일을 전부 썼는가 (완화가 과제 수행을 망치지
               않았는지 — 스코핑이 모델을 과하게 위축시키면 여기서 드러난다)

사용: AGENT_CLI_BASE_URL/API_KEY/MODEL 설정 후
  .venv/bin/python bench/multiuser/n3c_scoping_real.py [--reps 12]
  .venv/bin/python bench/multiuser/n3c_scoping_real.py --workload realistic \
      --arms off --reps 20
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
from dataclasses import dataclass
from pathlib import Path

from driver import AgentServer, turn_chain

#: §6.4 는 4회×40줄이라 턴당 ~4분이었다. 반복을 벌기 위해 줄인다.
WRITES_PER_TURN = 2
LINES = 8


@dataclass(frozen=True)
class Task:
    """한 사용자에게 줄 지시 하나 + 객관적 채점표.

    ``files`` 는 그 지시가 만들라고 한 파일들의 basename 이다. 채점(자기 일
    완수 / 남의 파일 침범)은 이 집합의 포함 관계로만 이뤄지므로 모델도 사람도
    판정에 개입하지 않는다. ``files[0]`` 은 질의 본문에 반드시 등장하므로
    귀속(:func:`_attribute`)이 질의 → 과제를 되찾는 표식으로도 쓴다.
    """

    key: str
    prompt: str
    files: tuple[str, ...]


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


def _confusable(tag: str, stem: str) -> Task:
    return Task(
        key=stem,
        prompt=task_for(tag, stem),
        files=tuple(f"{stem}{i}.txt" for i in range(1, WRITES_PER_TURN + 1)),
    )


def _realistic(key: str, subject: str, files: tuple[str, str], lines: tuple[str, str]):
    """실제로 다른 일 하나. 주제·파일명·줄 내용·완료 태그가 모두 다르다."""
    return Task(
        key=key,
        prompt=(
            f"You are working on {subject}. Use the write_file tool twice: "
            f"create {files[0]} with exactly {LINES} lines where every line is "
            f"'{lines[0]}' with N replaced by the line number, and create "
            f"{files[1]} with exactly {LINES} lines where every line is "
            f"'{lines[1]}' with N replaced by the line number. "
            "Do not read any file. Do not use the shell. "
            f"When both files are written, call complete with result '{key} done'."
        ),
        files=files,
    )


#: 워크로드 = 동시에 던질 지시 두 개. 첫째가 사용자 A, 둘째가 B.
WORKLOADS: dict[str, tuple[Task, Task]] = {
    # 태그와 파일 이름만 다르다 — 혼선 최악 조건 (§6.7 완화 실험의 기본값).
    "confusable": (_confusable("AAA", "alpha"), _confusable("BBB", "beta")),
    # 서로 다른 모듈에 서로 다른 동사 — 정상적으로 구분되는 작업 (R2-W3).
    "realistic": (
        _realistic(
            "parser",
            "the tokenizer of a small expression parser",
            ("parser_tokens.txt", "parser_rules.txt"),
            ("TOKEN_N", "rule N: expr"),
        ),
        _realistic(
            "readme",
            "the user-facing documentation of a command line tool",
            ("readme_intro.txt", "readme_usage.txt"),
            ("intro paragraph N", "usage step N"),
        ),
    ),
}


def real_llm_from_env() -> dict:
    try:
        return {
            "base_url": os.environ["AGENT_CLI_BASE_URL"],
            "api_key": os.environ["AGENT_CLI_API_KEY"],
            "model": os.environ["AGENT_CLI_MODEL"],
        }
    except KeyError as e:
        sys.exit(f"missing env {e} — set AGENT_CLI_BASE_URL/API_KEY/MODEL")


def _want(task: Task) -> set[str]:
    return set(task.files)


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


def _attribute(records: list[dict], tasks: tuple[Task, Task]) -> list[dict]:
    """질의별로 (그 질의를 처리하던 턴이) 실제로 쓴 파일 집합.

    `reply_to` 는 "이 레코드가 어느 유저 요청을 처리하는 동안 생겼는가"이고
    `files` 는 그 레코드가 만진 경로다(둘 다 `_enrich_record` 가 붙인다).
    질의 본문에서 과제의 첫 파일명을 읽어 오는 이유는 지시문이 그 이름을
    담고 있어서다 — 별도 매핑을 들고 다닐 필요가 없다.
    """
    targets: dict[str, str] = {}
    for r in records:
        if r.get("kind") == "query" and r.get("id"):
            text = str(r.get("text", ""))
            for task in tasks:
                if task.files[0] in text:
                    targets[r["id"]] = task.key
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


def run_rep(
    llm: dict, scoping: bool, rep: int, tasks: tuple[Task, Task]
) -> dict | None:
    ws = Path(tempfile.mkdtemp(prefix=f"n3c-{'on' if scoping else 'off'}-{rep}-"))
    server = AgentServer(
        ws,
        None,
        contract="parallel",
        max_turns=2,
        real_llm=llm,
        extra=["--turn-scoping"] if scoping else ["--no-turn-scoping"],
    )
    a_conn, b_conn = f"A-{rep}", f"B-{rep}"
    try:
        before = len(server.events())
        results: dict[str, int] = {}
        gate = threading.Barrier(2)

        def submit(conn: str, task: Task) -> None:
            gate.wait()
            results[conn] = server.chat(task.prompt, conn)

        threads = [
            threading.Thread(target=submit, args=(a_conn, tasks[0])),
            threading.Thread(target=submit, args=(b_conn, tasks[1])),
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
        records = _read_history(server.session_dir)
        turns = _attribute(records, tasks)
        if len(turns) != 2:
            return None  # 두 질의가 다 기록되지 않았다면 판정 불가
        # 턴별 응답 텍스트 보존 (플랜3 R3-W5): 커밋 raw 에 경로만 있으면
        # 사후 텍스트 수준 판정이 불가능하다. reply_to 사슬로 질의별 후속
        # 레코드의 text 를 모아 유계로 남긴다.
        answers: dict[str, str] = {}
        for r in records:
            owner = r.get("reply_to")
            if owner and r.get("text"):
                answers[owner] = (answers.get(owner, "") + "\n" + str(r["text"]))[:4000]
        by_key = {t.key: t for t in tasks}
        per_turn = []
        for t in turns:
            own_task = by_key[t["target"]]
            other_task = next(x for x in tasks if x.key != t["target"])
            own, other = _want(own_task), _want(other_task)
            per_turn.append(
                {
                    "target": t["target"],
                    "wrote": sorted(t["files"]),
                    "ownComplete": own <= t["files"],
                    "wroteOthers": bool(t["files"] & other),
                    "answerText": answers.get(t["query"], ""),
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
        if not n:
            continue  # --arms off 로 한 팔만 돌린 경우
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
        "--workload",
        choices=sorted(WORKLOADS),
        default="confusable",
        help="confusable=혼선 최악(기본) / realistic=정상적으로 구분되는 작업",
    )
    ap.add_argument(
        "--arms",
        choices=("both", "off", "on"),
        default="both",
        help="both=완화 절제(기본) / off=기저율만 (엔드포인트 시간 절약)",
    )
    ap.add_argument(
        "--rederive",
        action="store_true",
        help="실행 없이 커밋된 원시 JSONL 에서 요약만 재도출 (리포 규약).",
    )
    ap.add_argument(
        "--out-tag",
        default="",
        help="산출물 파일명 접미 — 다른 모델(J2)로 돌릴 때 커밋된 원 측정을 "
        "보존하기 위한 것 (예: --out-tag qwen14b).",
    )
    args = ap.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    tasks = WORKLOADS[args.workload]
    stem = (
        "n3c-scoping-real" if args.workload == "confusable" else f"n3c-{args.workload}"
    )
    if args.out_tag:
        stem = f"{stem}-{args.out_tag}"
    raw_path = args.out / f"{stem}.jsonl"

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
        arms = {"both": (False, True), "off": (False,), "on": (True,)}[args.arms]
        for rep in range(1, args.reps + 1):
            for scoping in arms:
                row = run_rep(llm, scoping, rep, tasks)
                if row is None:
                    continue
                rows.append(row)
                print(json.dumps(row), flush=True)
        raw_path.write_text(
            "".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8"
        )
        print(f"# elapsed {round(time.time() - t0, 1)}s", flush=True)

    workload_note = {
        "confusable": (
            "The two instructions differ only in a tag and a filename, which "
            "is close to the worst case for confusion and is kept that way "
            "deliberately."
        ),
        "realistic": (
            "The two instructions are genuinely different work (different "
            "subject, filenames, line content and completion tag), so this "
            "arm measures the base rate a deployment would actually see. "
            "Only the shape of the work is shared (two files of eight lines) "
            "so that spans and sample sizes stay comparable."
        ),
    }[args.workload]
    summary = {
        "workload": args.workload,
        "tasks": {t.key: list(t.files) for t in tasks},
        "writesPerTurn": WRITES_PER_TURN,
        "lines": LINES,
        "arms": summarize(rows),
        "note": (
            "crossTask = one turn carried out the other user's task (wrote "
            "their files instead of its own), judged from filenames alone. "
            f"{workload_note} bothComplete guards the other direction: scoping "
            "must not make the model so cautious that it stops doing its job."
        ),
    }
    (args.out / f"{stem}.json").write_text(
        json.dumps(summary, indent=2), encoding="utf-8"
    )
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
