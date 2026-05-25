#!/usr/bin/env python3
"""Live tool-selection check for ``code_index``.

Runs a battery of prompts through ``agent-cli run`` against a real LLM
(omlx by default) and verifies the model:
  1) picks the ``code_index`` tool,
  2) picks the EXPECTED mode for the task,
  3) finishes the task (reaches a ``complete`` action) without obvious
     loop/retry pathology.

Each case launches a fresh agent-cli session in this repo, so the model
sees the real index of the live tree.  The script inspects the resulting
``.agent-cli/sessions/<id>/history.jsonl`` for the tool sequence.

Usage:
  python3 scripts/live_test_code_index.py                 # all cases
  python3 scripts/live_test_code_index.py P3-callers P5   # subset by id prefix

Provider config is read from the constants at the top — adjust them if
your omlx auth / model name differs.

Expected runtime: ~1–2 min per case on a local mlx server (35B A3B).
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

# ----- provider config (edit if your omlx setup differs) --------------------

PROVIDER = "openai"  # omlx exposes an OpenAI-compatible /v1/chat/completions
BASE_URL = "http://127.0.0.1:8000/v1"
API_KEY = "dbwlsgPfls0912!"  # noqa: S105 — local omlx auth, intentional
MODEL = "Qwen3.6-35B-A3B-MLX-8bit"

# Per-case turn cap. Tasks should land in ~4 turns each; the cap is a
# fuse against pathological loops, not a budget the model is expected
# to spend.
MAX_TURNS = 8
TIMEOUT_SEC = 180

REPO = Path(__file__).resolve().parent.parent
SESSIONS_DIR = REPO / ".agent-cli" / "sessions"

# Files used as fixtures for the out-of-root / scratch cases. Created
# fresh on each run so the test is hermetic.
SCRATCH_PY = Path("/tmp/scratch_live_test.py")
SCRATCH_PY_CONTENT = (
    "def scratch_helper(x):\n"
    "    return x * 2\n"
    "\n"
    "def scratch_main():\n"
    "    return scratch_helper(21)\n"
)


# ----- test cases ------------------------------------------------------------


def _cases():
    """Return the test case list.

    Each entry: (case_id, prompt, expected_tool, expected_modes)

    ``expected_modes`` is a set — some prompts could be satisfied by
    either of two modes (e.g. ``lookup`` with a kind filter vs ``kind``
    mode) and we don't want to fail a sensible alternative.
    """
    return [
        (
            "P1-list",
            "agent_cli/loop.py 파일의 outline 을 보여줘. 어떤 함수와 클래스가 있는지.",
            "code_index",
            {"list"},
        ),
        (
            "P2-fetch",
            "agent_cli/loop.py 안의 AgentLoop._call_llm 함수 본문을 보여줘.",
            "code_index",
            {"fetch"},
        ),
        (
            "P3-callers",
            "AgentLoop._call_llm 함수를 호출하는 다른 함수들이 뭐가 있어?",
            "code_index",
            {"callers"},
        ),
        (
            "P4-lookup-section",
            "README.md 와 docs/ 의 markdown 헤딩 중 이름이 'Setup' 인 것들을 모두 찾아줘.",
            "code_index",
            {"lookup", "kind"},
        ),
        (
            "P5-slice",
            "agent_cli/loop.py 의 AgentLoop._call_llm 과 그 callees 를 함께 보고 싶어. depth=2 까지 slice 로 보여줘.",
            "code_index",
            {"slice"},
        ),
        (
            "P7-out-of-root",
            f"{SCRATCH_PY} 파일의 outline 을 보여줘.",
            "code_index",
            {"list"},
        ),
        (
            "P8-md-heading",
            "README.md 의 '## 도구' 섹션 본문을 fetch 해줘.",
            "code_index",
            {"fetch"},
        ),
    ]


# ----- helpers ---------------------------------------------------------------


def _setup_scratch() -> None:
    SCRATCH_PY.write_text(SCRATCH_PY_CONTENT)


def _existing_session_ids() -> set[str]:
    if not SESSIONS_DIR.is_dir():
        return set()
    return {p.name for p in SESSIONS_DIR.iterdir() if p.is_dir()}


def _find_new_session(before: set[str]) -> Path | None:
    after = _existing_session_ids()
    new = after - before
    if not new:
        return None
    # If multiple, pick the lexicographically largest (= latest timestamp).
    return SESSIONS_DIR / max(new)


def _parse_actions(session_dir: Path) -> list[tuple[str, dict]]:
    hp = session_dir / "history.jsonl"
    if not hp.is_file():
        return []
    out: list[tuple[str, dict]] = []
    with open(hp, encoding="utf-8") as f:
        for line in f:
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            if msg.get("role") != "assistant":
                continue
            action = msg.get("action") or ""
            if not action:
                continue
            ai = msg.get("action_input")
            if not isinstance(ai, dict):
                ai = {}
            out.append((action, ai))
    return out


def _summarize_actions(actions: list[tuple[str, dict]]) -> str:
    """One-line per action: ``tool[mode]``. ``complete`` stays bare."""
    parts: list[str] = []
    for action, ai in actions:
        if action == "complete":
            parts.append("complete")
            continue
        mode = ai.get("mode")
        parts.append(f"{action}[{mode}]" if mode else action)
    return " → ".join(parts)


def _run_case(case_id: str, prompt: str, expected_tool: str, expected_modes: set[str]) -> dict:
    before = _existing_session_ids()
    # Use the installed ``agent-cli`` console script — ``python3 -m
    # agent_cli.main`` would import the module without invoking the
    # Typer ``app()``, since main.py has no ``if __name__ == '__main__'``
    # block. The console script wires through Typer's entry point.
    cmd = [
        "agent-cli",
        "run",
        prompt,
        "--provider",
        PROVIDER,
        "--base-url",
        BASE_URL,
        "--api-key",
        API_KEY,
        "--model",
        MODEL,
        "--max-turns",
        str(MAX_TURNS),
    ]
    t0 = time.time()
    proc = subprocess.run(
        cmd,
        cwd=REPO,
        capture_output=True,
        text=True,
        timeout=TIMEOUT_SEC,
    )
    elapsed = time.time() - t0
    session = _find_new_session(before)
    actions = _parse_actions(session) if session else []
    tool_modes = [
        (action, ai.get("mode")) for action, ai in actions if action == expected_tool
    ]
    matched_mode = any(m in expected_modes for _, m in tool_modes)
    completed = any(action == "complete" for action, _ in actions)
    return {
        "case_id": case_id,
        "elapsed_s": round(elapsed, 1),
        "exit_code": proc.returncode,
        "session": session.name if session else None,
        "n_actions": len(actions),
        "trace": _summarize_actions(actions),
        "expected_tool": expected_tool,
        "expected_modes": sorted(expected_modes),
        "code_index_calls": [m for _, m in tool_modes],
        "matched_mode": matched_mode,
        "completed": completed,
        "stderr_tail": proc.stderr.splitlines()[-3:] if proc.stderr else [],
    }


def _print_report(result: dict) -> None:
    case_id = result["case_id"]
    status_mode = "✓" if result["matched_mode"] else "✗"
    status_done = "✓" if result["completed"] else "✗"
    print(f"\n[{case_id}]  mode {status_mode}   complete {status_done}   "
          f"elapsed {result['elapsed_s']}s   actions {result['n_actions']}")
    print(f"  expected: {result['expected_tool']} mode in {result['expected_modes']}")
    print(f"  observed: code_index modes = {result['code_index_calls']}")
    print(f"  trace   : {result['trace']}")
    if result["exit_code"] != 0 and result["stderr_tail"]:
        print(f"  stderr  : {result['stderr_tail']}")


def main() -> int:
    filter_ids = sys.argv[1:]
    _setup_scratch()
    cases = _cases()
    if filter_ids:
        cases = [c for c in cases if any(c[0].startswith(f) for f in filter_ids)]
        if not cases:
            print(f"no case matched filters: {filter_ids}")
            return 2
    print(f"running {len(cases)} case(s) against {MODEL} @ {BASE_URL}")
    results: list[dict] = []
    for case_id, prompt, tool, modes in cases:
        print(f"\n→ {case_id}: {prompt[:90]}{'...' if len(prompt) > 90 else ''}")
        try:
            r = _run_case(case_id, prompt, tool, modes)
        except subprocess.TimeoutExpired:
            r = {
                "case_id": case_id,
                "elapsed_s": TIMEOUT_SEC,
                "exit_code": -1,
                "session": None,
                "n_actions": 0,
                "trace": "(timeout)",
                "expected_tool": tool,
                "expected_modes": sorted(modes),
                "code_index_calls": [],
                "matched_mode": False,
                "completed": False,
                "stderr_tail": [],
            }
        results.append(r)
        _print_report(r)

    n_mode_ok = sum(1 for r in results if r["matched_mode"])
    n_done = sum(1 for r in results if r["completed"])
    print("\n=== Summary ===")
    print(f"  mode-matched : {n_mode_ok}/{len(results)}")
    print(f"  completed    : {n_done}/{len(results)}")
    for r in results:
        flag = "✓" if r["matched_mode"] and r["completed"] else "✗"
        print(f"  {flag} {r['case_id']}: modes={r['code_index_calls']}, "
              f"complete={r['completed']}, {r['elapsed_s']}s")
    return 0 if (n_mode_ok == len(results) and n_done == len(results)) else 1


if __name__ == "__main__":
    sys.exit(main())
