#!/usr/bin/env python3
"""수리 전/후 라이브 결과 대조 — §4.3 배선 수리(v7.30.2)가 수치를 바꿨는가.

플랜2 H2b: "공개는 수정의 대체물이 아니다." 스텝 seam 의 원자 커밋 미배선을
고친 뒤, 그 결함이 닿을 수 있었던 라이브 실험을 같은 설정으로 재실행하고
(``out/postfix/``) 커밋된 수리 전 결과(``out/``)와 나란히 놓는다.

읽는 법 — 이 결함이 바꿀 수 있었던 것은 병렬 계약의 **히스토리 레코드
순서**뿐이다. 따라서 기대는 다음과 같고, 어긋나면 그 자체가 발견이다:

  구조/귀속 지표   같아야 한다 (판정이 reply_to×files 조인이라 순서 무관)
  타이밍/효과 비중 재현 범위 안이어야 한다 (호스트 부하의 함수)
  내용 지표        표본마다 흔들린다 (§6.7 이 이미 분포로 보고하는 양)

사용: .venv/bin/python bench/multiuser/compare_prepost.py
"""

from __future__ import annotations

import json
from pathlib import Path

OUT = Path(__file__).parent / "out"
POST = OUT / "postfix"


def load_pair(name: str) -> tuple[dict | None, dict | None]:
    pre = OUT / name
    post = POST / name
    return (
        json.loads(pre.read_text()) if pre.is_file() else None,
        json.loads(post.read_text()) if post.is_file() else None,
    )


def row(label: str, pre, post, *, same_expected: bool) -> dict:
    verdict = "—"
    if pre is not None and post is not None:
        if same_expected:
            verdict = "SAME" if pre == post else "DIFFERS"
        else:
            verdict = "(varies)"
    return {
        "metric": label,
        "pre": pre,
        "post": post,
        "expect_same": same_expected,
        "verdict": verdict,
    }


def scoping() -> list[dict]:
    pre, post = load_pair("n3c-scoping-real.json")
    if not (pre and post):
        return []
    out = []
    for arm in ("off", "on"):
        a = next((x for x in pre["arms"] if x["scoping"] == arm), {})
        b = next((x for x in post["arms"] if x["scoping"] == arm), {})
        for key, same in (
            ("turnsWroteOthers", False),
            ("turnsOwnComplete", False),
            ("bothComplete", False),
            ("turns", True),
            ("medianSpanMs", False),
        ):
            out.append(
                row(f"n3c[{arm}].{key}", a.get(key), b.get(key), same_expected=same)
            )
    return out


def scope_real() -> list[dict]:
    pre, post = load_pair("p2-scope-real-summary.json")
    if not (pre and post):
        return []
    out = [
        row(
            "p2scope.runs_used",
            pre.get("runs_used"),
            post.get("runs_used"),
            same_expected=True,
        )
    ]
    for pc, qc in zip(pre["cells"], post["cells"]):
        tag = f"p2scope[{pc['paths']}/{pc['lock_scope']}]"
        for key in (
            "effect_share_measured_p50",
            "lock_wait_p50_ms",
            "effective_parallelism_p50",
        ):
            out.append(
                row(f"{tag}.{key}", pc.get(key), qc.get(key), same_expected=False)
            )
    return out


def ranking() -> list[dict]:
    pre, post = load_pair("p6-real-llm.json")
    if not (pre and post):
        return []
    out = []
    for pc, qc in zip(pre["hol_spot"], post["hol_spot"]):
        out.append(
            row(
                f"p6.hol[{pc['contract']}].p50_ms",
                pc.get("bTtft_p50"),
                qc.get("bTtft_p50"),
                same_expected=False,
            )
        )
    for pc, qc in zip(pre["token_cost"], post["token_cost"]):
        tag = f"p6.cost[{pc['contract']}/warmup{pc.get('warmup')}]"
        out.append(
            row(
                f"{tag}.calls_p50",
                pc.get("llm_calls_p50"),
                qc.get("llm_calls_p50"),
                same_expected=True,
            )
        )
        out.append(
            row(
                f"{tag}.input_p50",
                pc.get("input_tokens_p50"),
                qc.get("input_tokens_p50"),
                same_expected=False,
            )
        )
    return out


def shell() -> list[dict]:
    pre, post = load_pair("p2-shell-real.json")
    if not (pre and post):
        return []
    out = []
    for pc, qc in zip(pre["cells"], post["cells"]):
        tag = f"p2shell[{pc['shellSleepMs']}ms]"
        for key in ("effectShareP50", "lockWaitP50Ms", "effectiveParallelismP50"):
            out.append(
                row(f"{tag}.{key}", pc.get(key), qc.get(key), same_expected=False)
            )
    return out


def realistic() -> dict | None:
    path = POST / "n3c-realistic.json"
    if not path.is_file():
        return None
    d = json.loads(path.read_text())
    arm = d["arms"][0] if d.get("arms") else {}
    return {
        "workload": d.get("workload"),
        "tasks": d.get("tasks"),
        "reps": arm.get("reps"),
        "turns": arm.get("turns"),
        "turnsWroteOthers": arm.get("turnsWroteOthers"),
        "turnsOwnComplete": arm.get("turnsOwnComplete"),
        "crossTask_runs": arm.get("crossTask"),
        "bothComplete": arm.get("bothComplete"),
        "medianSpanMs": arm.get("medianSpanMs"),
    }


def main() -> None:
    rows = scoping() + scope_real() + ranking() + shell()
    report = {
        "note": (
            "pre = committed pre-fix run (out/), post = same settings re-run on "
            "the repaired step seam (out/postfix/). The fix could only change "
            "history record ORDER under the parallel contract, so structural "
            "counts must match while timing and content rates redraw."
        ),
        "rows": rows,
        "realistic_base_rate": realistic(),
    }
    (POST / "compare-prepost.json").write_text(
        json.dumps(report, indent=2, ensure_ascii=False)
    )
    width = max((len(r["metric"]) for r in rows), default=10)
    for r in rows:
        flag = "" if r["verdict"] in ("SAME", "(varies)", "—") else "  <-- CHECK"
        print(
            f"{r['metric']:<{width}}  pre={r['pre']!s:<12} post={r['post']!s:<12} {r['verdict']}{flag}"
        )
    if report["realistic_base_rate"]:
        print("\n== realistic base rate (no pre-fix counterpart) ==")
        print(json.dumps(report["realistic_base_rate"], indent=2, ensure_ascii=False))
    print(f"\nwrote {POST / 'compare-prepost.json'}")


if __name__ == "__main__":
    main()
