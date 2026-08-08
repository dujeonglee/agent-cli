#!/usr/bin/env python3
"""논문의 정량 주장을 커밋된 원시 데이터와 대조한다 (실험 없음, 순수 재계산).

`docs/research/19-claim-evidence-audit.md` 의 기계 검증 부분이다. 논문에
적힌 수를 여기 하드코딩하고 `out/` 의 산출물에서 같은 수를 다시 뽑아
비교한다. 두 값이 갈리면 **논문이 데이터를 앞질렀다는 뜻**이므로, 이
스크립트가 깨끗해야 초안을 손댈 수 있다.

수치가 아니라 *출처*가 어긋나는 종류(표는 재실행 값인데 옆 문장은 이전
실행 값)는 이 스크립트가 잡지 못한다. 그 감사는 사람이 했고 결과는 위
문서에 있다 — 실제로 그렇게 어긋난 문장이 하나 있었다.

사용: .venv/bin/python bench/multiuser/verify_paper_claims.py
종료 코드: 불일치가 있으면 1.
"""

from __future__ import annotations

import json
import statistics
import sys
from pathlib import Path

OUT = Path(__file__).parent / "out"


def J(name: str):
    return json.loads((OUT / name).read_text())


def L(name: str):
    return [json.loads(x) for x in (OUT / name).read_text().splitlines() if x.strip()]


ROWS: list[tuple] = []


def chk(sec, claim, paper, actual, tol=0.01):
    """논문 값 ``paper`` 와 raw 재계산 값 ``actual`` 비교. 수치는 상대 오차."""
    if isinstance(paper, (int, float)) and isinstance(actual, (int, float)):
        ok = abs(paper - actual) <= max(abs(paper) * tol, 1e-9)
    else:
        ok = paper == actual
    ROWS.append((sec, claim, paper, actual, ok))


def main() -> None:
    # ── §6.1 선행 차단 그리드 ──────────────────────────────
    e2 = J("e2-summary.json")
    sm = {(r["condition"], r["L"]): r for r in e2["summary"]}
    for cond, vals in (
        ("serial", [(2000, 2.08), (6000, 6.16), (15000, 15.34), (30000, 30.86)]),
        ("reject", [(2000, 2.31), (6000, 6.34), (15000, 15.42), (30000, 31.05)]),
    ):
        for ms, pv in vals:
            chk(
                "6.1",
                f"{cond} p50 L={ms // 1000}s",
                pv,
                round(sm[(cond, ms)]["p50"] / 1000, 2),
            )
    for ms, pv in [(2000, 0.292), (6000, 0.291), (15000, 0.291), (30000, 0.235)]:
        chk(
            "6.1",
            f"parallel p50 L={ms // 1000}s",
            pv,
            round(sm[("parallel", ms)]["p50"] / 1000, 3),
        )
    chk("6.1", "analysed runs of 240", 236, sum(r["n"] for r in e2["summary"]))
    for c, pv in (("serial", 1.028), ("reject", 1.027), ("parallel", -0.002)):
        chk("6.1", f"slope {c}", pv, round(e2["slope"][c], 3), tol=0.02)

    b = {c["k"]: c for c in J("e2b-summary.json")["cells"]}
    for k, pv in [(1, 15.10), (2, 7.30), (4, 3.39), (8, 1.44)]:
        chk("6.1", f"e2b inclusion k={k}", pv, round(b[k]["inclusionP50"] / 1000, 2))
    chk("6.1", "e2b k=1 ttft", 15.41, round(b[1]["ttftP50"] / 1000, 2))
    chk("6.1", "e2b k=1 injected rate", 0.0, b[1]["injectedRate"])
    chk("6.1", "e2b k=8 injected rate", 1.0, b[8]["injectedRate"])

    c = J("e2c-summary.json")
    rj = {r["intervalMs"]: r for r in c["reject"]}
    for iv, (pm, pf, pr) in {250: (172, 0.69, 61), 1000: (794, 0.79, 16)}.items():
        chk("6.1", f"e2c penalty {iv}ms", pm, round(rj[iv]["penaltyMs"]))
        chk(
            "6.1",
            f"e2c interval frac {iv}ms",
            pf,
            round(rj[iv]["penaltyFracOfInterval"], 2),
            tol=0.02,
        )
        chk("6.1", f"e2c retries {iv}ms", pr, int(rj[iv]["retriesP50"]))

    d = {x["N"]: x for x in J("e2d-summary.json")["cells"]}
    for N, pv in [(2, 236), (4, 265), (8, 328)]:
        chk("6.1", f"e2d p50 N={N}", pv, round(d[N]["p50"]))
    chk(
        "6.1",
        "e2d 7 questioners cost %",
        39,
        round(100 * (d[8]["p50"] / d[2]["p50"] - 1)),
    )

    # ── §6.2 무결성 절제 ──────────────────────────────────
    cell = {r["lock_scope"]: r for r in J("e1-ablation.json")["results"]}
    off = cell["off"]
    viol = off["counts"].get("mixed", 0) + off["counts"].get("broken", 0)
    chk(
        "6.2",
        "off violation rate %",
        8.2,
        round(100 * viol / off["snapshots_classified"], 1),
    )
    chk("6.2", "off violations", 9, viol)
    for scope, n in (("workspace", 181), ("conflict", 180)):
        chk("6.2", f"{scope} classified", n, cell[scope]["snapshots_classified"])
        chk(
            "6.2",
            f"{scope} violations",
            0,
            cell[scope]["counts"].get("mixed", 0)
            + cell[scope]["counts"].get("broken", 0),
        )

    # ── §6.3 붕괴 경계 ────────────────────────────────────
    sh = {x["cell"]: x for x in J("p2-shell-arms.json")}
    chk(
        "6.3",
        "eff par at 50% share",
        1.59,
        sh["shell_1s_50pct"]["effective_parallelism_p50"],
        tol=0.01,
    )
    chk(
        "6.3",
        "eff par at 90% share",
        1.10,
        sh["shell_3s_90pct"]["effective_parallelism_p50"],
        tol=0.01,
    )

    # ── §6.4 실 작동점 ────────────────────────────────────
    ps = J("p2-scope-real-summary.json")
    pc = {(x["paths"], x["lock_scope"]): x for x in ps["cells"]}
    for key, pv in (
        (("disjoint", "workspace"), 1.864),
        (("disjoint", "conflict"), 1.854),
        (("same", "workspace"), 1.873),
        (("same", "conflict"), 1.890),
    ):
        chk(
            "6.4",
            f"live eff par {key[0][:4]}/{key[1][:4]}",
            pv,
            pc[key]["effective_parallelism_p50"],
        )
    chk(
        "6.4",
        "live effect share",
        1e-05,
        pc[("disjoint", "workspace")]["effect_share_measured_p50"],
    )
    chk(
        "6.4",
        "live lock wait ms",
        0.1,
        pc[("disjoint", "workspace")]["lock_wait_p50_ms"],
    )
    chk(
        "6.4",
        "disjoint/workspace valid runs",
        2,
        pc[("disjoint", "workspace")]["n_valid_for_path_axis"],
    )
    spans = [r[k] for r in L("p2-scope-real.jsonl") for k in ("spanA_ms", "spanB_ms")]
    chk("6.4", "span range low s", 198, round(min(spans) / 1000))
    chk("6.4", "span range high s", 413, round(max(spans) / 1000))
    sc = {x["shellSleepMs"]: x for x in J("p2-shell-real.json")["cells"]}
    chk("6.4", "shell share 1s", 0.025, sc[1000]["effectShareP50"], tol=0.05)
    chk("6.4", "shell share 5s", 0.094, sc[5000]["effectShareP50"], tol=0.02)
    chk("6.4", "shell lock wait 5s ms", 4125, sc[5000]["lockWaitP50Ms"], tol=0.02)

    # ── §6.5 증분 재생 ────────────────────────────────────
    n4 = J("n4-replay.json")
    se = n4["stream_equality"]
    chk("6.5", "replayable events", 180, se["control_events"])
    chk("6.5", "identical incl payload", True, se["identical_including_payloads"])
    chk("6.5", "reconnects", 11, n4["cuts_observed"])

    # ── §6.6 동시 압축 ────────────────────────────────────
    n1 = J("n1-compaction.json")
    chk("6.6", "mock committed", 4, n1["compactions"]["committed"])
    chk("6.6", "mock stale", 0, n1["compactions"]["stale_retries"])
    chk(
        "6.6",
        "mock events inside windows",
        42,
        sum(w["turn_events_inside"] for w in n1["availability_windows"]),
    )
    chk(
        "6.6",
        "mock first tokens inside",
        11,
        sum(w["first_tokens_inside"] for w in n1["availability_windows"]),
    )
    chk("6.6", "mock queries lost", 0, n1["queries_lost"])
    adv = J("n1-compaction-adversarial.json")
    chk("6.6", "adversarial committed", 1, adv["compactions"]["committed"])
    chk("6.6", "adversarial stale", 5, adv["compactions"]["stale_retries"])
    nl = J("live/n1-compaction-real.json")
    chk("6.6", "live committed", 2, nl["compactions"]["committed"])
    chk("6.6", "live stale", 3, nl["compactions"]["stale_retries"])
    chk(
        "6.6",
        "live events inside",
        45,
        sum(w["turn_events_inside"] for w in nl["availability_windows"]),
    )
    chk(
        "6.6",
        "live first tokens inside",
        11,
        sum(w["first_tokens_inside"] for w in nl["availability_windows"]),
    )
    wins = [w["duration_ms"] / 1000 for w in nl["availability_windows"]]
    chk("6.6", "live window low s", 56, round(min(wins)))
    chk("6.6", "live window high s", 123, round(max(wins)))

    # ── §6.7 귀속·스코핑·staleness ────────────────────────
    st = J("n3-attribution.json")["structural"]
    chk("6.7", "mint chain checked", 100, st["mint_chain_checked"])
    chk("6.7", "mint chain errors", 0, st["mint_chain_errors"])
    chk("6.7", "reply_to duplicates", 0, st["reply_to_duplicates"])
    ab = {a["arm"]: a for a in J("n3b-scoping.json")["arms"]}
    chk(
        "6.7",
        "n3b off min/med/max",
        (0.13, 0.19, 0.21),
        (
            ab["off"]["mismatchMin"],
            ab["off"]["mismatchMedian"],
            ab["off"]["mismatchMax"],
        ),
    )
    chk("6.7", "n3b honor max", 0.0, ab["honor"]["mismatchMax"])
    arms = {a["scoping"]: a for a in J("postfix/n3c-scoping-real.json")["arms"]}
    chk("6.7", "confusable off wrote others", 25, arms["off"]["turnsWroteOthers"])
    chk("6.7", "confusable on wrote others", 0, arms["on"]["turnsWroteOthers"])
    chk("6.7", "confusable off own complete", 36, arms["off"]["turnsOwnComplete"])
    chk("6.7", "confusable off both correct", 16, arms["off"]["bothComplete"])
    chk(
        "6.7",
        "realistic off wrote others",
        31,
        J("postfix/n3c-realistic.json")["arms"][0]["turnsWroteOthers"],
    )
    chk(
        "6.7",
        "realistic on wrote others",
        0,
        J("postfix2/n3c-realistic.json")["arms"][0]["turnsWroteOthers"],
    )
    # 겹침 검증 — 표가 보고하는 실행에서 재계산해야 한다 (감사 발견 A)
    for label, path, arm, floor, med in (
        ("confusable off", "postfix/n3c-scoping-real.jsonl", "off", 53, 89),
        ("confusable on", "postfix/n3c-scoping-real.jsonl", "on", 61, 78),
        ("realistic off", "postfix/n3c-realistic.jsonl", "off", 76, 98),
        ("realistic on", "postfix2/n3c-realistic.jsonl", "on", 74, 77),
    ):
        rows = [r for r in L(path) if r["scoping"] == arm]
        cov = [
            min(r["spanA_ms"], r["spanB_ms"]) / max(r["spanA_ms"], r["spanB_ms"])
            for r in rows
        ]
        chk("6.7", f"overlap floor {label} %", floor, round(min(cov) * 100), tol=0.02)
        chk(
            "6.7",
            f"overlap median {label} %",
            med,
            round(statistics.median(cov) * 100),
            tol=0.02,
        )
    n5 = J("live/n5-staleness-real.json")
    par = [r for r in n5["reps"] if r["arm"] == "parallel"]
    chk("6.7", "live stale steps", 75, sum(r["stale_steps"] for r in par))
    chk("6.7", "live total steps", 92, sum(r["steps"] for r in par))
    chk("6.7", "live max depth", 10, max(r["stale_depth_max"] for r in par))
    chk(
        "6.7",
        "live serial control stale",
        0,
        sum(r["stale_steps"] for r in n5["reps"] if r["arm"] == "serial"),
    )
    m5 = J("n5-staleness.json")
    chk(
        "6.7",
        "mock stale of 500",
        498,
        sum(r["stale_steps"] for r in m5["reps"] if r["arm"] == "parallel"),
    )

    # ── §6.8 공정성 ───────────────────────────────────────
    p4 = J("p4-fairness.json")
    chk(
        "6.8",
        "mock gate_on short p50",
        76,
        round(p4["arms"]["gate_on"]["short_wait_p50"]),
    )
    chk(
        "6.8",
        "mock gate_off short p50",
        1807,
        round(p4["arms"]["gate_off"]["short_wait_p50"]),
    )
    chk(
        "6.8",
        "mock gate_off violations",
        20,
        p4["arms"]["gate_off"]["per_user_concurrency_violations"],
    )
    chk(
        "6.8",
        "mock gate_on violations",
        0,
        p4["arms"]["gate_on"]["per_user_concurrency_violations"],
    )
    pl = J("live/p4-fairness-real.json")
    chk(
        "6.8",
        "live gate_on short p50 ms",
        4.4,
        pl["arms"]["gate_on"]["short_wait_p50"],
        tol=0.05,
    )
    chk(
        "6.8",
        "live gate_off short p50 ms",
        145293,
        pl["arms"]["gate_off"]["short_wait_p50"],
    )
    chk(
        "6.8",
        "live gate_on short p95 ms",
        21862,
        pl["arms"]["gate_on"]["short_wait_p95"],
    )
    chk(
        "6.8",
        "live flood p50 s",
        84.3,
        round(pl["arms"]["gate_on"]["flood_wait_p50"] / 1000, 1),
    )
    ratio = (
        pl["arms"]["gate_off"]["short_wait_p50"]
        / pl["arms"]["gate_on"]["short_wait_p50"]
    )
    chk("6.8", "live ratio (thousands)", 33, round(ratio / 1000), tol=0.05)
    pb = {(x["shell_s"], x["arm"]): x for x in J("p4b-mixed.json")["cells"]}
    chk(
        "6.8",
        "mixed p95 at 5s shell ms",
        4992,
        round(pb[(5.0, "mixed")]["b_wait_p95_ms_median"]),
        tol=0.02,
    )

    # ── §6.9 수명주기 ─────────────────────────────────────
    p7 = J("p7-lifecycle.json")
    chk("6.9", "mock queries recorded", 204, p7["queries_recorded"])
    chk("6.9", "mock queries lost", 0, p7["queries_lost"])
    chk(
        "6.9",
        "mock compact committed",
        10,
        p7["phase_reports"][-1]["compact_committed_cumulative"],
    )
    chk(
        "6.9",
        "mock max tokens after",
        2610,
        p7["phase_reports"][-1]["max_tokens_after"],
    )
    p7l = J("live/p7-lifecycle-real.json")
    chk("6.9", "live queries recorded", 27, p7l["queries_recorded"])
    chk("6.9", "live queries lost", 0, p7l["queries_lost"])

    # ── §6.10 실모델 ──────────────────────────────────────
    p6 = J("p6-real-llm.json")
    hol = {r["contract"]: r for r in p6["hol_spot"]}
    chk("6.10", "serial p50 s", 38.2, round(hol["serial"]["bTtft_p50"] / 1000, 1))
    chk("6.10", "parallel p50 s", 10.8, round(hol["parallel"]["bTtft_p50"] / 1000, 1))
    chk("6.10", "serial reps", 12, hol["serial"]["n"])
    tc = {(r["contract"], r.get("warmup")): r for r in p6["token_cost"]}
    chk(
        "6.10",
        "premium empty session",
        1.49,
        round(
            tc[("parallel", 0)]["input_tokens_p50"]
            / tc[("serial", 0)]["input_tokens_p50"],
            2,
        ),
    )
    chk(
        "6.10",
        "premium after 5 turns",
        1.47,
        round(
            tc[("parallel", 5)]["input_tokens_p50"]
            / tc[("serial", 5)]["input_tokens_p50"],
            2,
        ),
    )
    by_n = {c["n"]: c for c in J("p6b-provider-concurrency.json")["cells"]}
    chk("6.10", "endpoint N=8 vs N=1", 2.68, by_n[8]["wallRatioToN1"], tol=0.02)
    chk("6.10", "endpoint N=1 wall s", 8.2, round(by_n[1]["wallP50Ms"] / 1000, 1))
    chk("6.10", "endpoint N=8 wall s", 21.8, round(by_n[8]["wallP50Ms"] / 1000, 1))
    chk("6.10", "throughput N=1", 14.7, by_n[1]["throughputPerS"])
    chk("6.10", "throughput N=8", 44.1, by_n[8]["throughputPerS"])

    # ── 보고 ──────────────────────────────────────────────
    bad = [r for r in ROWS if not r[4]]
    width = max(len(r[1]) for r in ROWS)
    for sec, claim, paper, actual, ok in ROWS:
        mark = "OK  " if ok else "FAIL"
        print(f"{mark} {sec:<5} {claim:<{width}}  paper={paper!s:<12} raw={actual!s}")
    print(f"\nchecked {len(ROWS)}, mismatches {len(bad)}")
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()
