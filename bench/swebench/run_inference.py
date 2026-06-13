#!/usr/bin/env python3
"""SWE-bench inference adapter for agent-cli.

For each SWE-bench task instance: check out the repo at base_commit, run
`agent-cli run "<issue>"` in it, capture `git diff` as the model patch, and
record agent-cli's operational health (turns.jsonl) alongside. Produces a
``predictions.jsonl`` the official swebench harness can evaluate.

Runs in the bench venv (swebench/datasets); calls the installed `agent-cli`
command as a subprocess, so the product env stays untouched.

  bench/.venv/bin/python bench/swebench/run_inference.py --n 5 --out bench/runs/smoke
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import time
from pathlib import Path

from datasets import load_dataset

REPO_CACHE = Path("bench/.cache/repos")
CLONE_URL = "https://github.com/{repo}.git"

PROMPT = """\
Resolve the following GitHub issue in this repository. Read the relevant \
source files, make the necessary code changes to fix the issue, then call \
`complete`. Modify only the library/source code needed to fix the issue — do \
not edit the test suite. Keep the change minimal and focused.

--- ISSUE ---
{problem_statement}
"""


def sh(cmd, cwd=None, timeout=None, check=False):
    return subprocess.run(
        cmd, cwd=cwd, timeout=timeout, check=check,
        capture_output=True, text=True,
    )


def ensure_repo_clone(repo: str) -> Path:
    """A full working clone per repo, cached and reused across instances."""
    dest = REPO_CACHE / repo.replace("/", "__")
    if not dest.exists():
        dest.parent.mkdir(parents=True, exist_ok=True)
        print(f"    cloning {repo} (first time, may be large)…", flush=True)
        r = sh(["git", "clone", CLONE_URL.format(repo=repo), str(dest)], timeout=1800)
        if r.returncode != 0:
            raise RuntimeError(f"clone failed: {r.stderr[-500:]}")
    return dest


def checkout(clone: Path, base_commit: str):
    sh(["git", "checkout", "-f", base_commit], cwd=clone, check=True)
    sh(["git", "clean", "-fdx"], cwd=clone)
    # keep agent-cli's session dir out of the diff without touching tracked files
    excl = clone / ".git" / "info" / "exclude"
    text = excl.read_text() if excl.exists() else ""
    if ".agent-cli/" not in text:
        excl.write_text(text + "\n.agent-cli/\n")


def extract_patch(clone: Path) -> str:
    sh(["git", "add", "-A"], cwd=clone)
    r = sh(["git", "diff", "--cached", "HEAD"], cwd=clone)
    return r.stdout


def latest_session(clone: Path) -> Path | None:
    sdir = clone / ".agent-cli" / "sessions"
    if not sdir.exists():
        return None
    sessions = [p for p in sdir.iterdir() if p.is_dir()]
    return max(sessions, key=lambda p: p.stat().st_mtime, default=None)


def health_from_turns(session: Path | None) -> dict:
    if not session:
        return {"turns": 0, "note": "no session"}
    tf = session / "turns.jsonl"
    if not tf.exists():
        return {"turns": 0, "note": "no turns.jsonl"}
    fails: dict[str, int] = {}
    stages: dict[str, int] = {}
    total = 0
    for line in tf.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
        except Exception:
            continue
        total += 1
        stages[str(r.get("parse_stage"))] = stages.get(str(r.get("parse_stage")), 0) + 1
        s = r.get("failure_signal")
        if s:
            fails[s] = fails.get(s, 0) + 1
    return {"turns": total, "failures": fails, "parse_stage": stages}


def run_instance(inst: dict, args) -> dict:
    iid = inst["instance_id"]
    print(f"  [{iid}] {inst['repo']} @ {inst['base_commit'][:8]}", flush=True)
    clone = ensure_repo_clone(inst["repo"])
    checkout(clone, inst["base_commit"])

    prompt = PROMPT.format(problem_statement=inst["problem_statement"])
    cmd = ["agent-cli", "run", prompt, "--max-turns", str(args.max_turns)]
    if args.model:
        cmd += ["--model", args.model]

    t0 = time.time()
    timed_out = False
    try:
        proc = sh(cmd, cwd=clone, timeout=args.timeout)
        rc = proc.returncode
    except subprocess.TimeoutExpired:
        timed_out = True
        rc = -1
    elapsed = round(time.time() - t0, 1)

    patch = extract_patch(clone)
    session = latest_session(clone)
    health = health_from_turns(session)
    print(
        f"    → {elapsed}s rc={rc} patch={len(patch)}B "
        f"turns={health.get('turns')} fails={health.get('failures', {})}"
        + (" [TIMEOUT]" if timed_out else ""),
        flush=True,
    )
    return {
        "prediction": {
            "instance_id": iid,
            "model_name_or_path": args.model_name,
            "model_patch": patch,
        },
        "health": {
            "instance_id": iid,
            "repo": inst["repo"],
            "elapsed_s": elapsed,
            "returncode": rc,
            "timed_out": timed_out,
            "patch_bytes": len(patch),
            "empty_patch": not patch.strip(),
            **health,
        },
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dataset", default="princeton-nlp/SWE-bench_Lite")
    ap.add_argument("--split", default="test")
    ap.add_argument("--n", type=int, default=5, help="number of instances")
    ap.add_argument("--repo", default=None, help="filter to a single repo (org/name)")
    ap.add_argument("--instances", default=None, help="comma-separated instance_ids")
    ap.add_argument("--max-turns", type=int, default=25)
    ap.add_argument("--timeout", type=int, default=1200, help="per-instance wall (s)")
    ap.add_argument("--model", default=None, help="override agent-cli model")
    ap.add_argument("--model-name", default="agent-cli-qwen27b")
    ap.add_argument("--out", default="bench/runs/smoke")
    args = ap.parse_args()

    ds = load_dataset(args.dataset, split=args.split)
    rows = list(ds)
    if args.instances:
        want = set(args.instances.split(","))
        rows = [r for r in rows if r["instance_id"] in want]
    elif args.repo:
        rows = [r for r in rows if r["repo"] == args.repo][: args.n]
    else:
        rows = rows[: args.n]

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    print(f"인스턴스 {len(rows)}개 → {out}", flush=True)

    preds, healths = [], []
    for inst in rows:
        try:
            res = run_instance(inst, args)
            preds.append(res["prediction"])
            healths.append(res["health"])
        except Exception as e:
            print(f"    ERROR {inst['instance_id']}: {e}", flush=True)
            healths.append({"instance_id": inst["instance_id"], "error": str(e)})

    (out / "predictions.jsonl").write_text(
        "\n".join(json.dumps(p, ensure_ascii=False) for p in preds) + "\n"
    )
    (out / "health.json").write_text(json.dumps(healths, ensure_ascii=False, indent=2))
    print(f"\n완료: predictions={len(preds)} → {out}/predictions.jsonl", flush=True)


if __name__ == "__main__":
    main()
