"""Phase 2 bakeoff — multi-turn wire-format compliance through the real loop.

Where Phase 1 measured "does the model emit the wire shape on the
first try?", Phase 2 measures "does the model *keep* emitting the
wire shape across multiple turns, including after the recovery layer
intervenes?". Each cell runs the real :class:`AgentLoop` with the
plugin's recovery wording, prefill, and provider hints all flowing
through exactly the way the live ``run`` / ``chat`` CLI commands do.

Tools are stubbed by monkey-patching ``agent_cli.tools.TOOLS`` —
real tool execution would touch the file system / shell, and the
goal here is to measure *the model's compliance*, not to verify the
tools. Stubs return generic success ToolResults so the model sees
deterministic observations and the loop progresses naturally.

Headline metrics:
  - **completed** — fraction of runs that hit the ``complete`` action
    inside ``MAX_TURNS``. Production-relevant: a format that can't
    finish a task is not a candidate regardless of single-turn rates.
  - **mean_turns** — average turns until completion (or MAX_TURNS).
  - **parse_failures_per_run** — sum of ``parse_stage == 0`` events
    across all turns, divided by N. Captures recovery load.
  - **recovery_invocations_per_run** — number of recovery messages
    the loop emitted. Detects "format ok but missing field" loops.
  - **thought_present_rate** — across all assistant turns of all
    runs, fraction with non-empty thought / reasoning.

Phase 2 does NOT execute concurrently with Phase 1 — both call
the same Ollama instance and weight-swapping serialised guarantees
the schedule is deterministic.
"""

from __future__ import annotations

import json
import statistics
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from agent_cli.context.manager import ContextManager
from agent_cli.loop import run_loop
from agent_cli.providers.ollama import OllamaProvider
from agent_cli.providers.compat import ModelCapabilities
from agent_cli.tools.result import ToolResult


# ── Configuration ────────────────────────────────────────────

MODELS = [
    "qwen3.6:35b-a3b-bf16",
    "qwen3.6:27b-bf16",
    "mistral-medium-3.5:128b-q4_K_M",
]

PLUGINS = ["react", "envelope"]

N_RUNS = 5
MAX_TURNS = 10

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
    id: str
    query: str
    active_tools: tuple[str, ...]


TASKS: list[Task] = [
    Task(
        id="read_then_complete",
        query=(
            "Read src/auth.py and tell me the name of the password-hashing "
            "function it defines. Then complete the task with that name."
        ),
        active_tools=("read_file", "shell"),
    ),
    Task(
        id="read_then_edit_then_review",
        query=(
            "In src/auth.py, replace `return md5(password.encode()).hexdigest()` "
            "with `return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()`. "
            "Read the file first to confirm the line, then edit it, then verify with "
            "ready_for_review before completing."
        ),
        active_tools=("read_file", "edit_file", "shell"),
    ),
    Task(
        id="shell_inspect",
        query=(
            "Use `ls -la src/` to show me what's inside the src directory, then "
            "report the listing back as your final answer."
        ),
        active_tools=("read_file", "shell"),
    ),
    Task(
        id="delegate_then_complete",
        query=(
            "Delegate to the explorer agent to scan src/auth.py for which "
            "functions depend on the md5 hash, then complete the task with the "
            "delegate's findings."
        ),
        active_tools=("read_file", "delegate", "shell"),
    ),
    Task(
        id="complete_direct",
        query="What is 2 + 2? Answer immediately with `complete`.",
        active_tools=("read_file", "shell"),
    ),
    Task(
        id="ready_for_review_path",
        query=(
            "I just finished updating the README. Confirm with the user that "
            "the documentation looks correct using ready_for_review, then "
            "complete the task."
        ),
        active_tools=("read_file", "shell"),
    ),
    Task(
        id="recovery_after_misuse",
        query=(
            "Read app.py at line 1-50, then summarise what you find. "
            "If you see no obvious issue, complete the task."
        ),
        active_tools=("read_file", "shell"),
    ),
]


# ── Mock tools ───────────────────────────────────────────────


def _make_mock_tool(name: str) -> Callable[[dict | str], ToolResult]:
    """Return a stub for ``name`` that produces a generic success
    observation so the loop can advance without touching the real fs.

    Stubs deliberately return short, predictable text — the model's
    handling of the observation is what we measure, not the depth of
    the simulated tool result.
    """
    fakes = {
        "read_file": (
            "1\tdef hash_password(password: str) -> str:\n"
            "2\t    return md5(password.encode()).hexdigest()\n"
        ),
        "write_file": "wrote 42 bytes",
        "edit_file": "applied 1 edit (1 region replaced)",
        "shell": "(mock shell output)\nexit_code=0",
        "read_symbols": "hash_password (function) :1-2",
        "read_context": "(mock context)",
        "fetch": "(mock fetch: 200 OK, 1234 bytes)",
        # Virtual tools (delegate / run_skill) are intercepted by the
        # loop before TOOLS lookup, so the entries below are only used
        # when the loop's interception path is bypassed.
        "delegate": "(mock delegate: subagent reported '...')",
        "run_skill": "(mock run_skill: skill reported '...')",
    }

    def _tool(args: dict | str) -> ToolResult:  # noqa: ARG001
        return ToolResult(success=True, output=fakes.get(name, f"(mock {name} ok)"))

    return _tool


def install_mock_tools() -> dict[str, Any]:
    """Replace every entry in ``agent_cli.tools.TOOLS`` with a stub.

    Virtual tools (complete, ready_for_review, ask) are kept real so
    the loop's intercept logic still recognises completion. Returns
    the original mapping so callers can restore it.
    """
    import agent_cli.tools as tools_module

    original = dict(tools_module.TOOLS)
    keep_real = {"complete", "ready_for_review", "ask"}
    new_tools: dict[str, Any] = {}
    for name, func in original.items():
        if name in keep_real:
            new_tools[name] = func
        else:
            new_tools[name] = _make_mock_tool(name)
    tools_module.TOOLS = new_tools
    return original


def restore_tools(original: dict[str, Any]) -> None:
    import agent_cli.tools as tools_module

    tools_module.TOOLS = original


# ── Per-run measurement ──────────────────────────────────────


@dataclass
class RunResult:
    """One full multi-turn run against (task, model, plugin)."""

    completed: bool
    iterations: int
    parse_failures: int
    no_action_events: int
    recovery_messages: int
    thought_present_count: int
    assistant_turns: int
    output: str
    elapsed_seconds: float
    error: str | None = None
    turns_jsonl_path: str | None = None


def _count_signals_from_turns_jsonl(path: Path) -> dict[str, int]:
    """Walk the per-turn observability log and count failure signals.

    The TurnRecorder writes one JSON object per turn with at least:
      - ``parse_stage``: 0 means parse failed
      - ``failure_signal``: NO_JSON / NO_ACTION / NO_THOUGHT / etc.
      - ``primitives_applied``: list (non-empty when recovery fired)

    We tally the three counters Phase 2 reports.
    """
    parse_failures = 0
    no_action_events = 0
    recovery_messages = 0
    if not path.is_file():
        return {
            "parse_failures": 0,
            "no_action_events": 0,
            "recovery_messages": 0,
        }
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if obj.get("parse_stage", 1) == 0:
            parse_failures += 1
        sig = obj.get("failure_signal")
        if sig == "NO_ACTION":
            no_action_events += 1
        if obj.get("primitives_applied"):
            recovery_messages += 1
    return {
        "parse_failures": parse_failures,
        "no_action_events": no_action_events,
        "recovery_messages": recovery_messages,
    }


def _count_thought_signals(history_jsonl: Path) -> tuple[int, int]:
    """Count assistant turns and how many had a non-empty thought."""
    if not history_jsonl.is_file():
        return 0, 0
    assistant = 0
    with_thought = 0
    for line in history_jsonl.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if obj.get("role") != "assistant":
            continue
        assistant += 1
        thought = obj.get("thought") or ""
        if isinstance(thought, str) and thought.strip():
            with_thought += 1
    return assistant, with_thought


def call_once(
    model: str,
    plugin_name: str,
    task: Task,
    *,
    base_url: str = "http://localhost:11434",
) -> RunResult:
    """Run one (task, model, plugin) cell through the real AgentLoop."""
    provider = OllamaProvider(base_url=base_url)
    with tempfile.TemporaryDirectory(prefix="bakeoff-phase2-") as tmpdir:
        ctx_dir = Path(tmpdir)
        ctx = ContextManager(
            session_dir=ctx_dir,
            max_context_tokens=200_000,
        )

        original_tools = install_mock_tools()
        t0 = time.monotonic()
        try:
            result = run_loop(
                query=task.query,
                provider=provider,
                capabilities=CAPS,
                model=model,
                provider_name="ollama",
                base_url=base_url,
                api_key="",
                max_turns=MAX_TURNS,
                ctx=ctx,
                active_tools=list(task.active_tools),
                wire_format=plugin_name,
                record_turns=True,
            )
            elapsed = time.monotonic() - t0
            error = None
            output = result.output if result.success else (result.error or "")
            completed = result.success
        except Exception as exc:
            elapsed = time.monotonic() - t0
            error = str(exc)
            output = ""
            completed = False
            result = None
        finally:
            restore_tools(original_tools)

        # The TurnRecorder writes turns.jsonl alongside history.jsonl.
        turns_path = ctx_dir / "turns.jsonl"
        history_path = ctx_dir / "history.jsonl"
        signals = _count_signals_from_turns_jsonl(turns_path)
        assistant_turns, with_thought = _count_thought_signals(history_path)

        # Iteration count: line count of turns.jsonl is the most
        # accurate; fallback to a constant when the file is missing.
        iterations = 0
        if turns_path.is_file():
            iterations = sum(
                1 for ln in turns_path.read_text().splitlines() if ln.strip()
            )

        return RunResult(
            completed=bool(completed),
            iterations=iterations,
            parse_failures=signals["parse_failures"],
            no_action_events=signals["no_action_events"],
            recovery_messages=signals["recovery_messages"],
            thought_present_count=with_thought,
            assistant_turns=assistant_turns,
            output=output,
            elapsed_seconds=elapsed,
            error=error,
            turns_jsonl_path=str(turns_path) if turns_path.exists() else None,
        )


# ── Aggregation ──────────────────────────────────────────────


@dataclass
class CellResult:
    runs: list[RunResult] = field(default_factory=list)

    @property
    def n(self) -> int:
        return len(self.runs)

    @property
    def completed_rate(self) -> float:
        if not self.runs:
            return 0.0
        return sum(1 for r in self.runs if r.completed) / self.n

    @property
    def mean_iterations(self) -> float:
        if not self.runs:
            return 0.0
        return statistics.mean(r.iterations for r in self.runs)

    @property
    def parse_failures_per_run(self) -> float:
        if not self.runs:
            return 0.0
        return statistics.mean(r.parse_failures for r in self.runs)

    @property
    def recovery_per_run(self) -> float:
        if not self.runs:
            return 0.0
        return statistics.mean(r.recovery_messages for r in self.runs)

    @property
    def thought_present_rate(self) -> float:
        total_assistant = sum(r.assistant_turns for r in self.runs)
        if total_assistant == 0:
            return 0.0
        total_with = sum(r.thought_present_count for r in self.runs)
        return total_with / total_assistant

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
    """Outer loop is ``model`` so Ollama doesn't swap weights every call."""
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
                    flag = "✓" if res.completed else "·"
                    err = " ERR" if res.error else ""
                    print(
                        f"  [{done:3d}/{total}] {plugin_name:8s} {task.id:28s} "
                        f"run={run_idx + 1}/{N_RUNS} {flag} "
                        f"iters={res.iterations:2d} pf={res.parse_failures:2d} "
                        f"rec={res.recovery_messages:2d}{err}  "
                        f"({res.elapsed_seconds:5.1f}s, ETA {eta / 60:4.1f}m)",
                        flush=True,
                    )

    print(f"\nTotal {done} runs in {(time.monotonic() - started) / 60:.1f} minutes")
    return cells


# ── Reporting ────────────────────────────────────────────────


def format_pct(x: float) -> str:
    return f"{x * 100:5.1f}%"


def format_markdown_report(
    cells: dict[tuple[str, str, str], CellResult],
) -> str:
    lines: list[str] = []
    lines.append("# Bakeoff Phase 2 — Multi-turn loop compliance")
    lines.append("")
    lines.append(
        f"_{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}, "
        f"{N_RUNS} runs per cell, max_turns={MAX_TURNS}, "
        f"tools mocked (real virtual tools: complete / ready_for_review / ask)_"
    )
    lines.append("")
    lines.append(
        "Metrics: **completed** = ``complete`` action emitted within max_turns; "
        "**iters** = mean turn count (lower = more efficient); "
        "**pf/run** = mean ``parse_stage==0`` events per run; "
        "**rec/run** = mean recovery interventions per run; "
        "**thought** = fraction of assistant turns with non-empty thought / reasoning."
    )
    lines.append("")

    for model in MODELS:
        lines.append(f"## {model}")
        lines.append("")
        lines.append(
            "| task | plugin | completed | iters | pf/run | rec/run | thought |"
            " mean_s |"
        )
        lines.append(
            "|------|--------|----------:|------:|-------:|--------:|--------:|"
            "-------:|"
        )
        for task in TASKS:
            for plugin_name in PLUGINS:
                cell = cells.get((model, plugin_name, task.id))
                if cell is None or not cell.runs:
                    lines.append(
                        f"| {task.id} | {plugin_name} | — | — | — | — | — | — |"
                    )
                    continue
                row = (
                    f"| {task.id} | {plugin_name} | "
                    f"{format_pct(cell.completed_rate)} | "
                    f"{cell.mean_iterations:5.1f} | "
                    f"{cell.parse_failures_per_run:5.2f} | "
                    f"{cell.recovery_per_run:5.2f} | "
                    f"{format_pct(cell.thought_present_rate)} | "
                    f"{cell.mean_elapsed:5.1f} |"
                )
                lines.append(row)
        lines.append("")

    lines.append("## Per-plugin summary (averaged across models and tasks)")
    lines.append("")
    lines.append("| plugin | completed | iters | pf/run | rec/run | thought |")
    lines.append("|--------|----------:|------:|-------:|--------:|--------:|")
    for plugin_name in PLUGINS:
        all_cells = [
            cell for (m, p, _), cell in cells.items() if p == plugin_name and cell.runs
        ]
        if not all_cells:
            lines.append(f"| {plugin_name} | — | — | — | — | — |")
            continue
        completed = statistics.mean(c.completed_rate for c in all_cells)
        iters = statistics.mean(c.mean_iterations for c in all_cells)
        pf = statistics.mean(c.parse_failures_per_run for c in all_cells)
        rec = statistics.mean(c.recovery_per_run for c in all_cells)
        thought = statistics.mean(c.thought_present_rate for c in all_cells)
        lines.append(
            f"| {plugin_name} | "
            f"{format_pct(completed)} | "
            f"{iters:5.1f} | "
            f"{pf:5.2f} | "
            f"{rec:5.2f} | "
            f"{format_pct(thought)} |"
        )
    lines.append("")
    return "\n".join(lines)


def dump_raw_json(cells: dict[tuple[str, str, str], CellResult], path: Path) -> None:
    out: list[dict] = []
    for (model, plugin_name, task_id), cell in cells.items():
        for run_idx, r in enumerate(cell.runs):
            out.append(
                {
                    "model": model,
                    "plugin": plugin_name,
                    "task_id": task_id,
                    "run_idx": run_idx,
                    "completed": r.completed,
                    "iterations": r.iterations,
                    "parse_failures": r.parse_failures,
                    "no_action_events": r.no_action_events,
                    "recovery_messages": r.recovery_messages,
                    "assistant_turns": r.assistant_turns,
                    "thought_present_count": r.thought_present_count,
                    "output": r.output,
                    "elapsed_seconds": r.elapsed_seconds,
                    "error": r.error,
                    "turns_jsonl_path": r.turns_jsonl_path,
                }
            )
    path.write_text(json.dumps(out, ensure_ascii=False, indent=2))


# ── Main ─────────────────────────────────────────────────────


def main() -> None:
    out_dir = Path(__file__).parent / "results"
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    cells = run_all()

    md_path = out_dir / f"phase2_{stamp}.md"
    md_path.write_text(format_markdown_report(cells))
    print(f"\nReport: {md_path}")

    json_path = out_dir / f"phase2_{stamp}.json"
    dump_raw_json(cells, json_path)
    print(f"Raw:    {json_path}")

    for stem, path in [
        ("phase2_latest.md", md_path),
        ("phase2_latest.json", json_path),
    ]:
        link = out_dir / stem
        if link.exists() or link.is_symlink():
            link.unlink()
        link.symlink_to(path.name)
    print(f"Symlinks: {out_dir / 'phase2_latest.md'}, {out_dir / 'phase2_latest.json'}")

    print("\n" + format_markdown_report(cells))


if __name__ == "__main__":
    main()
