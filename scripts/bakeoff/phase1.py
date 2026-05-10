"""Phase 1 bakeoff — single-turn wire-format compliance measurement.

For each (task, model, plugin) combination we make N runs through the
real production code path:

  1. ``build_system_prompt(wire_format=plugin)`` produces the system
     prompt — this is the same prompt the live ``run`` / ``chat``
     CLI commands would build, so format-rules text, tool inline
     guides, and skill / agent docs all flow from the plugin.
  2. The plugin's ``provider_call_kwargs`` and ``prefill`` are applied
     exactly the way ``AgentLoop`` applies them.
  3. The model emits one response. We parse it with ``plugin.parse``.
  4. The resulting ``ParsedAction`` is scored against the task's
     expected action and input shape.

The output is a Markdown summary table plus a JSON dump of every raw
response so we can drill into individual failures after the fact.

Phase 1 is single-turn only — no recovery, no loop. It answers
"does the model emit the wire shape correctly on the first try?".
Phase 2 (separate file) covers the full loop including recovery.
"""

from __future__ import annotations

import json
import statistics
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from agent_cli import wire_formats
from agent_cli.prompts.system_prompt import build_system_prompt
from agent_cli.providers.compat import ModelCapabilities
from agent_cli.wire_formats.base import ParsedAction


# ── Configuration ────────────────────────────────────────────

OLLAMA_URL = "http://localhost:11434/api/chat"

MODELS = [
    "qwen3.6:35b-a3b-bf16",
    "qwen3.6:27b-bf16",
    "mistral-medium-3.5:128b-q4_K_M",
]

PLUGINS = ["react"]

N_RUNS = 5

# Capabilities for the prompt builder. Real values would come from
# ModelCapabilities probes; for the bakeoff we use a single shape so
# the prompt is identical across models — that's what we want, the
# only variable is the model itself.
CAPS = ModelCapabilities(
    context_window=262144,
    max_output_tokens=4096,
    supports_structured_output=True,
    supports_thinking=False,
    thinking_budget=0,
    supports_strict_schema=False,
)


# ── Tasks ────────────────────────────────────────────────────


@dataclass(frozen=True)
class Task:
    """One bakeoff scenario.

    ``expected_action_input_keys`` is a *subset* check — the parsed
    action_input must include every listed key (extras are fine).
    None for tasks where any input shape is acceptable (e.g. the
    zero-tool ``complete`` direct path).
    """

    id: str
    query: str
    active_tools: list[str]
    expected_action: str
    expected_action_input_keys: tuple[str, ...] | None


TASKS: list[Task] = [
    Task(
        id="read_file_simple",
        query="Read src/auth.py to understand the authentication flow.",
        active_tools=["read_file", "shell"],
        expected_action="read_file",
        expected_action_input_keys=("path",),
    ),
    Task(
        id="read_file_search",
        query=(
            "In app.py, find every place where the substring 'login' "
            "appears. Use the read_file tool with its search mode."
        ),
        active_tools=["read_file", "shell"],
        expected_action="read_file",
        expected_action_input_keys=("path", "search"),
    ),
    Task(
        id="shell_run_tests",
        query="Run the test suite with `pytest tests/ -v` and tell me what fails.",
        active_tools=["read_file", "shell"],
        expected_action="shell",
        expected_action_input_keys=("command",),
    ),
    Task(
        id="edit_file_nested",
        query=(
            "In src/auth.py, replace `return md5(password.encode()).hexdigest()` "
            "with `return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()`. "
            "Use one edit_file call. The function name and signature stay the same."
        ),
        active_tools=["read_file", "edit_file", "shell"],
        expected_action="edit_file",
        expected_action_input_keys=("path", "edits"),
    ),
    Task(
        id="delegate_nested",
        query=(
            "Delegate to the explorer agent to scan src/auth.py "
            "for which functions depend on the md5 hash."
        ),
        active_tools=["read_file", "delegate", "shell"],
        expected_action="delegate",
        expected_action_input_keys=("tasks",),
    ),
    Task(
        id="complete_direct",
        query="What is 2 + 2?",
        active_tools=["read_file", "shell"],
        expected_action="complete",
        expected_action_input_keys=("result",),
    ),
    Task(
        id="ready_for_review_path",
        query=(
            "I just finished reviewing the README. Confirm with the user "
            "that the documentation looks correct before I close out the task."
        ),
        active_tools=["read_file", "shell"],
        expected_action="ready_for_review",
        expected_action_input_keys=("summary",),
    ),
]


# ── Provider call (mirrors AgentLoop's wiring) ───────────────


@dataclass
class CallResult:
    raw: str
    parsed: ParsedAction
    elapsed_seconds: float
    error: str | None = None


def call_once(model: str, plugin_name: str, task: Task) -> CallResult:
    """Make one provider call and parse the response.

    Mirrors ``AgentLoop._call_llm`` for prompt / kwargs / prefill.
    """
    plugin = wire_formats.get(plugin_name)
    system = build_system_prompt(
        capabilities=CAPS,
        active_tools=task.active_tools,
        wire_format=plugin,
    )

    extra = plugin.provider_call_kwargs()
    skip_json = bool(extra.get("skip_json_format"))

    body: dict = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": task.query},
        ],
        "stream": False,
        "options": {"temperature": 0.0},
    }
    if not skip_json and CAPS.supports_structured_output:
        body["format"] = "json"

    prefill = plugin.prefill()
    if prefill:
        body["messages"].append({"role": "assistant", "content": prefill})

    req = urllib.request.Request(
        OLLAMA_URL,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )

    t0 = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=300) as r:
            payload = json.loads(r.read())
        raw = payload["message"]["content"]
    except (urllib.error.URLError, urllib.error.HTTPError, OSError) as exc:
        elapsed = time.monotonic() - t0
        return CallResult(
            raw="", parsed=ParsedAction(), elapsed_seconds=elapsed, error=str(exc)
        )

    elapsed = time.monotonic() - t0
    full = (prefill + raw) if prefill else raw
    parsed = plugin.parse(full)
    return CallResult(raw=full, parsed=parsed, elapsed_seconds=elapsed)


# ── Aggregation ──────────────────────────────────────────────


@dataclass
class CellResult:
    """Results for one (task, plugin, model) cell — N runs aggregated."""

    runs: list[CallResult] = field(default_factory=list)

    @property
    def n(self) -> int:
        return len(self.runs)

    @property
    def parse_success_rate(self) -> float:
        if not self.runs:
            return 0.0
        ok = sum(1 for r in self.runs if r.parsed.parse_stage > 0)
        return ok / self.n

    @property
    def parse_stage_distribution(self) -> dict[int, int]:
        dist: dict[int, int] = {}
        for r in self.runs:
            dist[r.parsed.parse_stage] = dist.get(r.parsed.parse_stage, 0) + 1
        return dist

    @property
    def thought_present_rate(self) -> float:
        if not self.runs:
            return 0.0
        ok = sum(
            1
            for r in self.runs
            if isinstance(r.parsed.thought, str) and r.parsed.thought.strip()
        )
        return ok / self.n

    def action_match_rate(self, expected: str) -> float:
        if not self.runs:
            return 0.0
        ok = sum(1 for r in self.runs if r.parsed.action == expected)
        return ok / self.n

    def action_input_valid_rate(self, expected_keys: tuple[str, ...] | None) -> float:
        """Fraction of runs where action_input is a dict containing
        every expected key. ``expected_keys=None`` accepts any dict."""
        if not self.runs:
            return 0.0
        ok = 0
        for r in self.runs:
            ai = r.parsed.action_input
            if not isinstance(ai, dict):
                continue
            if expected_keys is None:
                ok += 1
                continue
            if all(k in ai for k in expected_keys):
                ok += 1
        return ok / self.n

    @property
    def mean_elapsed(self) -> float:
        if not self.runs:
            return 0.0
        return statistics.mean(r.elapsed_seconds for r in self.runs)

    @property
    def error_count(self) -> int:
        return sum(1 for r in self.runs if r.error is not None)


# ── Runner ───────────────────────────────────────────────────


def run_all() -> dict[tuple[str, str, str], CellResult]:
    """Execute every (model, plugin, task) cell N_RUNS times.

    Outer loop is ``model`` so the Ollama server doesn't have to swap
    weights between every call; inner loops are plugin / task / run.
    Progress is printed to stdout so a 30-minute run is observable.
    """
    cells: dict[tuple[str, str, str], CellResult] = {}
    total = len(MODELS) * len(PLUGINS) * len(TASKS) * N_RUNS
    done = 0
    started = time.monotonic()

    for model in MODELS:
        print(f"\n=== model: {model} ===", flush=True)
        for plugin_name in PLUGINS:
            for task in TASKS:
                key = (model, plugin_name, task.id)
                cell = cells.setdefault(key, CellResult())
                for run_idx in range(N_RUNS):
                    res = call_once(model, plugin_name, task)
                    cell.runs.append(res)
                    done += 1
                    elapsed = time.monotonic() - started
                    eta = (total - done) * (elapsed / done) if done else 0.0
                    short_action = (res.parsed.action or "—")[:18]
                    err = "ERR" if res.error else "ok"
                    print(
                        f"  [{done:3d}/{total}] {plugin_name:8s} {task.id:24s} "
                        f"run={run_idx + 1}/{N_RUNS} stage={res.parsed.parse_stage} "
                        f"action={short_action:18s} {err}  "
                        f"({res.elapsed_seconds:5.1f}s, ETA {eta / 60:4.1f}m)",
                        flush=True,
                    )
    print(f"\nTotal {done} calls in {(time.monotonic() - started) / 60:.1f} minutes")
    return cells


# ── Reporting ────────────────────────────────────────────────


def task_by_id(task_id: str) -> Task:
    return next(t for t in TASKS if t.id == task_id)


def format_pct(x: float) -> str:
    return f"{x * 100:5.1f}%"


def format_markdown_report(
    cells: dict[tuple[str, str, str], CellResult],
) -> str:
    """Build a model × plugin × task table with the headline metrics."""
    lines: list[str] = []
    lines.append("# Bakeoff Phase 1 — Single-turn wire-format compliance")
    lines.append("")
    lines.append(
        f"_{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}, "
        f"{N_RUNS} runs per cell, temperature=0.0_"
    )
    lines.append("")
    lines.append(
        "Metrics: **parse_ok** = `parse_stage > 0`; **thought** = thought "
        "field non-empty; **action✓** = parsed action matches expected; "
        "**input✓** = action_input is a dict with every expected key."
    )
    lines.append("")

    for model in MODELS:
        lines.append(f"## {model}")
        lines.append("")
        lines.append(
            "| task | plugin | parse_ok | thought | action✓ | input✓ | mean_s |"
        )
        lines.append(
            "|------|--------|---------:|--------:|--------:|-------:|-------:|"
        )
        for task in TASKS:
            for plugin_name in PLUGINS:
                cell = cells.get((model, plugin_name, task.id))
                if cell is None or not cell.runs:
                    lines.append(f"| {task.id} | {plugin_name} | — | — | — | — | — |")
                    continue
                row = (
                    f"| {task.id} | {plugin_name} | "
                    f"{format_pct(cell.parse_success_rate)} | "
                    f"{format_pct(cell.thought_present_rate)} | "
                    f"{format_pct(cell.action_match_rate(task.expected_action))} | "
                    f"{format_pct(cell.action_input_valid_rate(task.expected_action_input_keys))} | "
                    f"{cell.mean_elapsed:5.1f} |"
                )
                lines.append(row)
        lines.append("")

    # Per-plugin aggregate across all (model, task) cells.
    lines.append("## Per-plugin summary (averaged across models and tasks)")
    lines.append("")
    lines.append("| plugin | parse_ok | thought | action✓ | input✓ |")
    lines.append("|--------|---------:|--------:|--------:|-------:|")
    for plugin_name in PLUGINS:
        all_cells = [
            cell for (m, p, _), cell in cells.items() if p == plugin_name and cell.runs
        ]
        if not all_cells:
            lines.append(f"| {plugin_name} | — | — | — | — |")
            continue
        parse_ok = statistics.mean(c.parse_success_rate for c in all_cells)
        thought = statistics.mean(c.thought_present_rate for c in all_cells)

        def _action_avg() -> float:
            vals = []
            for (m, p, t_id), cell in cells.items():
                if p != plugin_name or not cell.runs:
                    continue
                vals.append(cell.action_match_rate(task_by_id(t_id).expected_action))
            return statistics.mean(vals) if vals else 0.0

        def _input_avg() -> float:
            vals = []
            for (m, p, t_id), cell in cells.items():
                if p != plugin_name or not cell.runs:
                    continue
                vals.append(
                    cell.action_input_valid_rate(
                        task_by_id(t_id).expected_action_input_keys
                    )
                )
            return statistics.mean(vals) if vals else 0.0

        lines.append(
            f"| {plugin_name} | "
            f"{format_pct(parse_ok)} | "
            f"{format_pct(thought)} | "
            f"{format_pct(_action_avg())} | "
            f"{format_pct(_input_avg())} |"
        )
    lines.append("")
    return "\n".join(lines)


def dump_raw_json(cells: dict[tuple[str, str, str], CellResult], path: Path) -> None:
    """Dump every (raw, parsed) pair so failures can be inspected later."""
    out: list[dict] = []
    for (model, plugin_name, task_id), cell in cells.items():
        for run_idx, r in enumerate(cell.runs):
            p = r.parsed
            out.append(
                {
                    "model": model,
                    "plugin": plugin_name,
                    "task_id": task_id,
                    "run_idx": run_idx,
                    "raw": r.raw,
                    "parse_stage": p.parse_stage,
                    "thought": p.thought,
                    "action": p.action,
                    "action_input": p.action_input,
                    "thinking": p.thinking,
                    "truncated": p.truncated,
                    "elapsed_seconds": r.elapsed_seconds,
                    "error": r.error,
                }
            )
    path.write_text(json.dumps(out, ensure_ascii=False, indent=2))


# ── Main ─────────────────────────────────────────────────────


def main() -> None:
    out_dir = Path(__file__).parent / "results"
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    cells = run_all()

    md_path = out_dir / f"phase1_{stamp}.md"
    md_path.write_text(format_markdown_report(cells))
    print(f"\nReport: {md_path}")

    json_path = out_dir / f"phase1_{stamp}.json"
    dump_raw_json(cells, json_path)
    print(f"Raw:    {json_path}")

    # Convenience aliases pointing to the latest run.
    for stem, path in [
        ("phase1_latest.md", md_path),
        ("phase1_latest.json", json_path),
    ]:
        link = out_dir / stem
        if link.exists() or link.is_symlink():
            link.unlink()
        link.symlink_to(path.name)
    print(f"Symlinks: {out_dir / 'phase1_latest.md'}, {out_dir / 'phase1_latest.json'}")

    print("\n" + format_markdown_report(cells))


if __name__ == "__main__":
    main()
