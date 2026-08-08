#!/usr/bin/env python3
"""P2-SCOPE-REAL — 실 LLM 이 도는 진짜 턴에서 효과 시간 비중은 얼마인가.

``p2_scope.py`` 는 락을 직접 구동해 붕괴 **법칙**(효과 비중 s 에 대해 유효
병렬도 상한 2/max(1,2s))을 전 구간에서 측정한다. 그 실험이 답하지 않는 것이
하나 있다: **실제 워크로드는 그 곡선 위 어디에 앉는가.** 이 스크립트가 그
질문을 담당한다. 목 LLM 도 우회도 없이, 실 온프렘 모델이 도는 상태에서 두
사용자가 동시에 파일을 만들게 하고, 서버 계측(`turns.jsonl`)에서 턴별
락 보유 시간과 턴 길이를 읽어 **효과 시간 비중을 실측**한다.

왜 이 실험을 목 LLM 으로는 못 하는가(실측으로 확인한 제약): 목은 대화에서
진행도를 읽는데(마지막 `[[bench]]` 지시자 이후의 관찰 수), 컨텍스트가
**공유**되므로 동시 턴들의 관찰이 한 계수기에 섞인다. 실측하면 첫 호출은
각자 자기 지시자를 고르지만 연속 호출부터 최신 지시자로 붕괴하고, 그 결과
한 턴이 남의 경로에 쓰다가 루프 감지에 걸린다. 실 모델은 자기 턴의 요청을
읽고 답하므로 이 제약이 없다 — 그래서 "실제 비중"은 실 모델로만 물을 수 있다.

측정: 락 이벤트의 ``thread``(= ``agent-turn-{turn_id}``)로 보유/대기를 턴에
귀속시키고, 턴 사슬(dispatch→complete)로 구간을 잡는다.

  effect_share = Σ held_ms / (구간 − Σ wait_ms)
  effective_parallelism = (work_A + work_B) / makespan

축: lock-scope {workspace, conflict} × 경로 {disjoint, same}. 가설: 실제
비중이 0.5 를 한참 밑돌면 두 스코프는 **구분되지 않아야 한다**(법칙의 예측).
그렇게 나오면 그것은 널 결과가 아니라 §6.4 의 통제 실험이 예측한 지점을
실제 시스템이 실제로 차지한다는 확인이다.

사용: AGENT_CLI_BASE_URL/API_KEY/MODEL 을 설정하고
  .venv/bin/python bench/multiuser/p2_scope_real.py [--reps 3]
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

from driver import AgentServer, turn_chain
from n3c_scoping_real import _read_history

SCOPES = ("workspace", "conflict")
PATHS = ("disjoint", "same")
#: 원시 행에 보존하는 턴별 응답 텍스트 상한 — 사후 텍스트 수준 판정용이며
#: (플랜3 R3-W5: 커밋 raw 에 텍스트가 없어 사후 분석이 불가능했다) 원시
#: 파일 크기를 유계로 유지한다.
TEXT_KEEP = 4000
#: 턴당 쓰기를 여러 번 시키는 이유: 실 모델은 한 번에 수 KB 이상을 내놓지
#: 못하므로, 효과 시간 비중을 조금이라도 끌어올리려면 횟수로 가야 한다.
#: (그래도 추론이 지배한다는 것이 이 실험의 예상 결과이자 요점이다.)
WRITES_PER_TURN = 4
LINES = 40


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


def classify(row: dict) -> str:
    """이 rep 이 의도한 경로 조건을 실제로 구현했는가.

    실 모델도 공유 트랜스크립트에서 **남의 과제를 수행**할 수 있다(실측:
    한 턴이 자기 alpha 대신 상대의 beta 에 썼다). 그러면 그 rep 은 "서로소
    경로" 관측이 아니라 사실상 동일 경로 조건이므로 경로 축 비교에서 섞으면
    안 된다. 반면 **효과 비중과 락 대기는 그 rep 에서도 유효**하다 — 쓰기가
    실제로 일어났고 같은 락을 통과했기 때문이다. 그래서 버리지 않고 표시만
    하고, 아래 요약이 두 집계를 분리한다.
    """
    files = set(row.get("files_written") or [])
    want = lambda stem: {f"{stem}{i}.txt" for i in range(1, WRITES_PER_TURN + 1)}
    if row["paths"] == "same":
        return "same_ok" if want("shared") <= files else "partial"
    a_ok, b_ok = want("alpha") <= files, want("beta") <= files
    if a_ok and b_ok:
        return "disjoint_ok"
    if a_ok != b_ok:
        return "cross_task"  # 한 턴이 남의 과제를 수행
    return "partial"


def lock_totals(events: list[dict], offset: int) -> dict[str, dict]:
    """스레드별 락 대기/보유 합계. 병렬 턴 스레드 이름은 ``agent-turn-{id}``
    이므로 그 접미사가 곧 turn_id 다."""
    per: dict[str, dict] = {}
    for e in events[offset:]:
        if e.get("event") != "lock":
            continue
        thread = str(e.get("thread", ""))
        if not thread.startswith("agent-turn-"):
            continue
        turn_id = thread.removeprefix("agent-turn-")
        slot = per.setdefault(turn_id, {"wait_ms": 0.0, "held_ms": 0.0, "n": 0})
        if e.get("phase") == "acquire":
            slot["wait_ms"] += float(e.get("wait_ms") or 0.0)
            slot["n"] += 1
        elif e.get("phase") == "release":
            slot["held_ms"] += float(e.get("held_ms") or 0.0)
    return per


def run_rep(
    llm: dict, scope: str, paths: str, rep: int, *, scoping: bool = False
) -> dict | None:
    ws = Path(tempfile.mkdtemp(prefix=f"p2sr-{scope}-{paths}-"))
    server = AgentServer(
        ws,
        None,
        contract="parallel",
        lock_scope=scope,
        max_turns=2,
        real_llm=llm,
        # 명시적 고정: 원 측정은 스코핑 이전(off)이었고, 스코핑 팔(J1)은
        # §6.7 의 완화가 경로 조건(서로소)을 복원하는지를 묻는다.
        extra=["--turn-scoping"] if scoping else ["--no-turn-scoping"],
    )
    a_conn, b_conn = f"A-{rep}", f"B-{rep}"
    try:
        before = len(server.events())
        # 두 턴을 최대한 같은 순간에 넣는다 — 겹침 구간이 곧 측정 대상이다.
        results: dict[str, int] = {}
        gate = threading.Barrier(2)

        def submit(conn: str, tag: str, target: str) -> None:
            gate.wait()
            results[conn] = server.chat(task_for(tag, target), conn)

        target_a = "shared" if paths == "same" else "alpha"
        target_b = "shared" if paths == "same" else "beta"
        threads = [
            threading.Thread(target=submit, args=(a_conn, "AAA", target_a)),
            threading.Thread(target=submit, args=(b_conn, "BBB", target_b)),
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
        locks = lock_totals(events, before)
        span_a = ca["complete"] - ca["dispatch"]
        span_b = cb["complete"] - cb["dispatch"]
        la = locks.get(str(ca["turn_id"]), {"wait_ms": 0.0, "held_ms": 0.0, "n": 0})
        lb = locks.get(str(cb["turn_id"]), {"wait_ms": 0.0, "held_ms": 0.0, "n": 0})
        work_a = span_a - la["wait_ms"]
        work_b = span_b - lb["wait_ms"]
        makespan = max(ca["complete"], cb["complete"]) - min(
            ca["dispatch"], cb["dispatch"]
        )
        held = la["held_ms"] + lb["held_ms"]
        files = sorted(p.name for p in ws.glob("*.txt"))
        # 턴별 응답 텍스트 보존: history.jsonl 의 reply_to 사슬로 질의별
        # 후속 레코드의 text 를 모은다. 사후 텍스트 수준 판정(누구의 태그를
        # 말했는가)을 커밋 raw 만으로 가능하게 하는 계기다.
        records = _read_history(server.session_dir)
        answers: dict[str, str] = {}
        for r in records:
            owner = r.get("reply_to")
            if not owner or not r.get("text"):
                continue
            answers[owner] = (answers.get(owner, "") + "\n" + str(r["text"]))[
                :TEXT_KEEP
            ]
        queries = {
            r["id"]: str(r.get("text", ""))[:400]
            for r in records
            if r.get("kind") == "query" and r.get("id")
        }
        # 턴별 파일 귀속 (reply_to × files 조인, n3c 판정기와 같은 원리).
        # 워크스페이스 합집합(classify)은 "둘 다 함" 실패 양상 — §6.7 의
        # 지배적 양상 — 을 disjoint_ok 로 오판하므로, 경로 조건의 턴 수준
        # 판정에는 이 필드가 필요하다.
        turn_files: dict[str, list[str]] = {}
        for r in records:
            owner = r.get("reply_to")
            if not owner:
                continue
            for p in r.get("files") or []:
                turn_files.setdefault(owner, []).append(Path(str(p)).name)
        turn_files = {k: sorted(set(v)) for k, v in turn_files.items()}
        return {
            "lock_scope": scope,
            "paths": paths,
            "rep": rep,
            "turn_scoping": scoping,
            "queries": queries,
            "answer_texts": answers,
            "turn_files": turn_files,
            "spanA_ms": round(span_a, 1),
            "spanB_ms": round(span_b, 1),
            "workA_ms": round(work_a, 1),
            "workB_ms": round(work_b, 1),
            "makespan_ms": round(makespan, 1),
            "effective_parallelism": round((work_a + work_b) / makespan, 3)
            if makespan
            else None,
            "lock_acquisitions": la["n"] + lb["n"],
            "lock_wait_ms": round(la["wait_ms"] + lb["wait_ms"], 1),
            "lock_held_ms": round(held, 2),
            "effect_share_measured": round(held / (work_a + work_b), 5)
            if (work_a + work_b)
            else None,
            "files_written": files,
        }
    finally:
        server.stop()
        shutil.rmtree(ws, ignore_errors=True)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--reps", type=int, default=3)
    ap.add_argument("--out", type=Path, default=Path(__file__).parent / "out")
    ap.add_argument(
        "--turn-scoping",
        action="store_true",
        help="스코핑을 켠 팔(J1). §6.7 의 완화가 §6.4 서로소 경로 조건을 "
        "복원하는지 측정한다. 산출물은 별도 파일(-scoped)로 나가 커밋된 "
        "원 측정을 보존한다.",
    )
    ap.add_argument(
        "--paths",
        choices=("both", "disjoint", "same"),
        default="both",
        help="경로 조건 선택 — J1 은 disjoint 만 재실행한다.",
    )
    ap.add_argument(
        "--rederive",
        action="store_true",
        help="아무것도 실행하지 않고 커밋된 원시 JSONL 에서 요약만 재도출한다. "
        "요약은 언제나 원시 파일에서 나온다는 리포 규약을 분석 로직이 바뀌었을 때 "
        "1시간짜리 실 LLM 실행 없이 지키기 위한 것.",
    )
    args = ap.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)

    stem = "p2-scope-real-scoped" if args.turn_scoping else "p2-scope-real"
    raw_path = args.out / f"{stem}.jsonl"
    if args.rederive:
        rows = [
            json.loads(x)
            for x in raw_path.read_text(encoding="utf-8").splitlines()
            if x.strip()
        ]
        llm = {"model": "(re-derived from committed raw data)"}
        t_start = time.time()
        summarize(rows, args, llm, t_start, raw_path, write_raw=False)
        return

    llm = real_llm_from_env()
    rows = []
    t_start = time.time()
    path_axis = PATHS if args.paths == "both" else (args.paths,)
    for paths in path_axis:
        for scope in SCOPES:
            for rep in range(1, args.reps + 1):
                row = run_rep(llm, scope, paths, rep, scoping=args.turn_scoping)
                if row is None:
                    print(
                        json.dumps(
                            {"skipped": {"scope": scope, "paths": paths, "rep": rep}}
                        ),
                        flush=True,
                    )
                    continue
                rows.append(row)
                print(json.dumps(row), flush=True)

    summarize(rows, args, llm, t_start, raw_path, write_raw=True)


def summarize(rows, args, llm, t_start, raw_path, *, write_raw: bool) -> None:
    """원시 행 → 요약. 실행 직후에도, ``--rederive`` 로도 같은 함수를 탄다."""
    for r in rows:
        r["validity"] = classify(r)
    if write_raw:
        raw_path.write_text(
            "".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8"
        )

    def med(cell: list, k: str) -> float:
        return statistics.median([r[k] for r in cell])

    # 경로 축 비교는 **의도한 조건을 실제로 구현한 rep 만** 쓴다. 효과 비중과
    # 락 대기는 전 rep 을 쓴다 (쓰기가 실제로 일어나 같은 락을 통과했다).
    cells = []
    for paths in PATHS:
        ok = "disjoint_ok" if paths == "disjoint" else "same_ok"
        for scope in SCOPES:
            same_cond = [
                r for r in rows if r["paths"] == paths and r["lock_scope"] == scope
            ]
            valid = [r for r in same_cond if r["validity"] == ok]
            if not same_cond:
                continue
            cells.append(
                {
                    "paths": paths,
                    "lock_scope": scope,
                    "n_runs": len(same_cond),
                    "n_valid_for_path_axis": len(valid),
                    "effective_parallelism_p50": round(
                        med(valid, "effective_parallelism"), 3
                    )
                    if valid
                    else None,
                    "effect_share_measured_p50": round(
                        med(same_cond, "effect_share_measured"), 5
                    ),
                    "lock_wait_p50_ms": round(med(same_cond, "lock_wait_ms"), 1),
                    "lock_held_p50_ms": round(med(same_cond, "lock_held_ms"), 2),
                    "lock_acquisitions_p50": med(same_cond, "lock_acquisitions"),
                    "turn_span_p50_ms": round(
                        med(same_cond, "spanA_ms") + med(same_cond, "spanB_ms"), 1
                    )
                    / 2,
                }
            )

    shares = [r["effect_share_measured"] for r in rows if r["effect_share_measured"]]
    # 유효 병렬도가 락과 무관함의 직접 증거: 락 대기≈0 이면 makespan=max(span)
    # 이므로 지표는 항등적으로 1+짧은턴/긴턴 이어야 한다. 그 항등식에서 벗어난
    # 크기가 곧 락이 기여한 몫의 상한이다.
    devs = []
    for r in rows:
        lo, hi = sorted((r["spanA_ms"], r["spanB_ms"]))
        if hi and r["effective_parallelism"] is not None:
            devs.append(abs((1 + lo / hi) - r["effective_parallelism"]))
    counts: dict[str, int] = {}
    for r in rows:
        counts[r["validity"]] = counts.get(r["validity"], 0) + 1

    summary = {
        "model": llm["model"],
        "turn_scoping": bool(rows and rows[0].get("turn_scoping")),
        "runs_used": len(rows),
        "writes_per_turn_requested": WRITES_PER_TURN,
        "lines_per_file": LINES,
        "validity_counts": counts,
        "cross_task_note": (
            "실 모델도 공유 트랜스크립트에서 남의 과제를 수행할 수 있다. "
            "두 턴의 지시가 태그와 대상 파일명만 다른 거의 동일한 문장이라 "
            "이 비율은 일반 추정치가 아니라 이 조건의 관측이다."
        ),
        "cells": cells,
        "effect_share_overall_p50": round(statistics.median(shares), 5)
        if shares
        else None,
        "effect_share_overall_max": round(max(shares), 5) if shares else None,
        "parallelism_identity_max_deviation": round(max(devs), 4) if devs else None,
        "parallelism_identity_note": (
            "지표 = 1 + 짧은턴/긴턴 과의 최대 편차. 이 값이 0 에 붙어 있으면 "
            "유효 병렬도 손실이 전부 턴 길이 비대칭이고 락 기여는 그 아래다."
        ),
        "elapsed_s": round(time.time() - t_start, 1),
    }
    (
        raw_path.parent / f"{raw_path.stem.replace('.jsonl', '')}-summary.json"
    ).write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(summary, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
