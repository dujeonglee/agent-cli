"""THROWAWAY prototype bakeoff — "inputs array, no action" wire shape.

NOT production. Measures one question before any loop surgery:

  Do the omlx models reliably emit a schema that DROPS the `action` field
  and instead carries an ARRAY of tool inputs, each identified purely by
  its wire-key prefix (read_file_path → read_file)?  And can they put
  MULTIPLE independent tools in one turn's array (the novel claim)?

Shape under test (registered at runtime, never shipped):

    {"thought": "...",
     "inputs": [{"read_file_path": "a.py"}, {"shell_command": "ls src/"}]}

Baseline = the current default `prefix_md` on the same tasks (single action
per turn) for a turn-cost / compliance comparison.

Scoring is format-compliance only (this is the gate from
prefix_md_full_dropped: "a new wire format must bakeoff first — sanity ≠
real"). Run:

    OMLX_API_KEY=... python scripts/bakeoff/proto_inputs_array.py
"""

from __future__ import annotations

import json
import os
import statistics
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field

from agent_cli import wire_formats
from agent_cli.prompts.system_prompt import build_system_prompt
from agent_cli.providers.capabilities import ModelCapabilities
from agent_cli.tools.registry import infer_action
from agent_cli.wire_formats.base import ParsedAction, WireFormat

# BASE_URL is a LAN address — safe to default. The API key is a secret: read
# from the environment only, never committed (mirrors phase1.py).
#   export OMLX_API_KEY=...
BASE_URL = os.environ.get("OMLX_BASE_URL", "http://192.168.0.44:8000/v1")
API_KEY = os.environ.get("OMLX_API_KEY", "")
CHAT_URL = BASE_URL.rstrip("/") + "/chat/completions"
MODELS = [
    m.strip()
    for m in os.environ.get("BAKEOFF_MODELS", "Qwen3.6-27B-MLX-8bit").split(",")
    if m.strip()
]
N_RUNS = int(os.environ.get("BAKEOFF_N_RUNS", "3"))
# temp=0 → deterministic greedy (compliance gate). temp>0 → real sampling
# variance, the actual reliability estimate.
TEMPERATURE = float(os.environ.get("BAKEOFF_TEMP", "0.0"))
FAIL_DUMP = os.environ.get("BAKEOFF_FAIL_DUMP", "/tmp/proto_bakeoff_fails.txt")

CAPS = ModelCapabilities(
    context_window=262144,
    max_output_tokens=4096,
    supports_structured_output=True,
    supports_thinking=False,
    thinking_budget=0,
    supports_strict_schema=False,
)


# ── The prototype wire format ────────────────────────────────
class InputsArrayFormat(WireFormat):
    """`{thought, inputs:[{prefixed_keys}, ...]}` — no `action` field."""

    name = "inputs_array"
    thought_required = False
    action_required = False

    def render_full_example(self, *, thought, action: str, action_input: str) -> str:
        th = thought if thought is not None else "your reasoning"
        # action_input is a JSON string of the (prefixed) input dict — splice
        # it verbatim as the single element of the inputs array.
        return f'{{\n  "thought": "{th}",\n  "inputs": [\n    {action_input}\n  ]\n}}'

    def format_rules(self) -> str:
        return (
            "Output your response as a SINGLE JSON object with two keys:\n"
            '  - "thought": your reasoning (string)\n'
            '  - "inputs": a JSON array of one or more operations for this turn\n'
            "\n"
            'There is NO "action" field. Each element of "inputs" is ONE tool '
            "call, and the tool is identified by its key PREFIX (keys starting "
            'with "read_file_" call read_file; "shell_" calls shell; etc.). Use '
            "the exact prefixed key names shown in each tool's guide above.\n"
            "\n"
            "Rules:\n"
            '1. Always include a "thought".\n'
            '2. Each "inputs" element uses exactly one tool\'s prefixed keys.\n'
            "3. To do several INDEPENDENT operations in one turn, add multiple "
            'elements to "inputs" (e.g. read two files AND run one shell command).\n'
            "4. If a later operation DEPENDS on an earlier one's result, do NOT "
            "batch them — emit only the first now; its observation comes next turn.\n"
            "5. If an observation shows an error, fix parameters and retry.\n"
            "6. Respond in the user's language.\n"
            "\n"
            "Finishing the task:\n"
            "- When the task is DONE and you have nothing more to run, emit ONLY "
            'a thought: {"thought": "<your final wrap-up / answer to the user>"} '
            'with NO "inputs" (or an empty array). Thought-only means the task is '
            "complete and the loop ends.\n"
            "- As long as there is work to do, you MUST include the tool calls in "
            '"inputs". Never stop early with a thought while work remains.\n'
            "\n"
            "One operation:\n"
            '{"thought": "...", "inputs": [{"read_file_path": "src/auth.py"}]}\n'
            "\n"
            "Several independent operations in one turn:\n"
            '{"thought": "...", "inputs": [{"read_file_path": "src/a.py"}, '
            '{"shell_command": "ls src/"}]}'
        )

    # abstract stubs — unused because format_rules() is overridden, but the
    # ABC requires them to exist.
    def format_rules_anchor(self) -> str:
        return 'Output a single JSON object: {"thought": ..., "inputs": [...]}.'

    def format_rules_field_specific(self) -> str:
        return '1. Populate "thought".\n2. Populate "inputs" with ≥1 element.'

    def parse(self, llm_text: str) -> ParsedAction:
        obj = _extract_json(llm_text)
        if not isinstance(obj, dict):
            return ParsedAction(raw=llm_text, parse_stage=0)
        inputs = obj.get("inputs")
        thought = obj.get("thought")
        if isinstance(inputs, list) and inputs:
            # Singular ParsedAction can't hold the array; for the bakeoff we
            # only need parse_stage>0 to mean "valid shape". Detailed scoring
            # reads the array via analyze().
            return ParsedAction(
                thought=thought if isinstance(thought, str) else None,
                action=None,
                action_input=inputs,  # list, observability-only here
                raw=llm_text,
                parse_stage=1,
            )
        return ParsedAction(raw=llm_text, parse_stage=0)

    # recovery wordings — unused in single-turn bakeoff
    def constraint_reminder_call(self) -> str:
        return ""

    def constraint_reminder_action_required(self) -> str:
        return ""

    def failure_framing_parse_fail(self) -> str:
        return "Your last response was not valid JSON."

    def failure_framing_no_action(self) -> str:
        return "Your last response had no inputs."

    def static_retry_hint_no_json(self) -> str:
        return 'Emit {"thought": ..., "inputs": [...]}.'

    def static_retry_hint_no_action(self) -> str:
        return 'Add at least one element to "inputs".'

    def system_user_prefixes(self) -> tuple[str, ...]:
        return ()


def _extract_json(text: str):
    """Strip ``` fences and parse the first balanced JSON object."""
    s = text.strip()
    if s.startswith("```"):
        s = s.split("\n", 1)[1] if "\n" in s else s
        if s.endswith("```"):
            s = s[: s.rfind("```")]
    start = s.find("{")
    if start < 0:
        return None
    depth = 0
    for i in range(start, len(s)):
        if s[i] == "{":
            depth += 1
        elif s[i] == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(s[start : i + 1])
                except json.JSONDecodeError:
                    return None
    return None


# ── Tasks ────────────────────────────────────────────────────
@dataclass(frozen=True)
class Task:
    id: str
    query: str
    active_tools: tuple[str, ...]
    expect_min_inputs: int  # how many independent ops a correct answer batches
    expect_tools: tuple[str, ...]  # tools that should appear (subset check)


# No `complete` tool — completion is signalled by a thought-only emission.
_ALL = ("read_file", "shell", "code_index", "edit_file")
TASKS = [
    # ── terminal = thought-only (no tool) — should TERMINATE ──
    Task(
        "greet_only",
        "Just greet me with a short hello. No work or tools are needed.",
        _ALL,
        0,
        (),
    ),
    Task(
        "finish_after_work",
        "You have just finished implementing the login() function and it "
        "passes all tests. Nothing is left to do — wrap up.",
        _ALL,
        0,
        (),
    ),
    Task(
        "finalize_after_review",
        "[Observation]: Checklist verified — code written, tests pass, docs "
        "updated. Nothing is missing. Finalize.",
        _ALL,
        0,
        (),
    ),
    # ── regression: must NOT false-terminate while work remains ──
    Task(
        "multi_read_shell",
        "Do these two INDEPENDENT things: read src/auth.py, and separately "
        "list the src/ directory with `ls src/`.",
        _ALL,
        2,
        ("read_file", "shell"),
    ),
    Task(
        "single_read",
        "Read src/auth.py to understand the auth flow.",
        _ALL,
        1,
        ("read_file",),
    ),
    Task(
        "dependent_chain",
        "Find which file defines `login()` and then read that file. "
        "(You do not know the file yet.)",
        _ALL,
        1,
        (),
    ),
]
_NO_OVERBATCH = {"dependent_chain"}
# Tasks whose CORRECT answer is a thought-only terminal (no inputs).
_TERMINAL = {"greet_only", "finish_after_work", "finalize_after_review"}


# ── Call + analyze ───────────────────────────────────────────
@dataclass
class Run:
    raw: str
    n_inputs: int  # elements in inputs[] (inputs_array) or 1 (prefix_md parsed)
    resolved: int  # elements whose prefix → exactly one tool
    tools: tuple[str, ...]
    parse_ok: bool
    terminal: bool = False  # thought-only, no inputs → task done
    had_action_field: bool = False  # model added a stray "action" key (ReAct leak)
    error: str | None = None


def call_once(model: str, plugin_name: str, task: Task) -> Run:
    plugin = wire_formats.get(plugin_name)
    system = build_system_prompt(
        capabilities=CAPS, active_tools=list(task.active_tools), wire_format=plugin
    )
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": task.query},
        ],
        "max_tokens": CAPS.max_output_tokens,
        "temperature": TEMPERATURE,
        "stream": False,
        "response_format": {"type": "json_object"},
    }
    headers = {"Content-Type": "application/json"}
    if API_KEY:
        headers["Authorization"] = f"Bearer {API_KEY}"
    req = urllib.request.Request(
        CHAT_URL, data=json.dumps(body).encode(), headers=headers
    )
    try:
        with urllib.request.urlopen(req, timeout=300) as r:
            raw = json.loads(r.read())["choices"][0]["message"]["content"]
    except (urllib.error.URLError, OSError, KeyError) as e:
        return Run("", 0, 0, (), False, error=str(e))

    if plugin_name == "inputs_array":
        obj = _extract_json(raw)
        if not isinstance(obj, dict):
            _dump_fail(model, task, "inputs_array", raw)
            return Run(raw, 0, 0, (), False)
        leak = "action" in obj  # model reverted to a top-level action field
        inputs = obj.get("inputs")
        thought = obj.get("thought")
        if isinstance(inputs, list) and len(inputs) > 0:
            items = [x for x in inputs if isinstance(x, dict)]
            tools = []
            resolved = 0
            for it in items:
                t = infer_action(it)
                if t:
                    resolved += 1
                    tools.append(t)
            if resolved != len(inputs) or leak:
                _dump_fail(model, task, "inputs_array", raw)
            return Run(
                raw, len(inputs), resolved, tuple(tools), True, had_action_field=leak
            )
        # thought-only (no/empty inputs) = terminal completion
        if isinstance(thought, str) and thought.strip() and inputs in (None, []):
            return Run(raw, 0, 0, (), True, terminal=True, had_action_field=leak)
        # neither work nor a clean thought-only terminal → real failure
        _dump_fail(model, task, "inputs_array", raw)
        return Run(raw, 0, 0, (), False, had_action_field=leak)
    else:
        parsed = plugin.parse(raw)
        ok = parsed.parse_stage > 0
        action = parsed.action
        if not action and isinstance(parsed.action_input, dict):
            action = infer_action(parsed.action_input)
        tools = (action,) if action else ()
        return Run(raw, 1 if ok else 0, 1 if action else 0, tools, ok)


def _dump_fail(model: str, task: Task, fmt: str, raw: str) -> None:
    with open(FAIL_DUMP, "a", encoding="utf-8") as f:
        f.write(f"\n=== {model} | {fmt} | {task.id} ===\n{raw}\n")


@dataclass
class Cell:
    runs: list[Run] = field(default_factory=list)

    def rate(self, pred) -> float:
        return (
            (sum(1 for r in self.runs if pred(r)) / len(self.runs))
            if self.runs
            else 0.0
        )

    def mean_inputs(self) -> float:
        vals = [r.n_inputs for r in self.runs if r.parse_ok]
        return statistics.mean(vals) if vals else 0.0


def main() -> None:
    cells: dict[tuple[str, str, str], Cell] = {}
    formats = [
        f.strip()
        for f in os.environ.get("BAKEOFF_FORMATS", "inputs_array,prefix_md").split(",")
        if f.strip()
    ]
    total = len(MODELS) * len(formats) * len(TASKS) * N_RUNS
    done = 0
    t0 = time.monotonic()
    for model in MODELS:
        print(f"\n=== {model} ===", flush=True)
        for fmt in formats:
            for task in TASKS:
                cell = cells.setdefault((model, fmt, task.id), Cell())
                for _ in range(N_RUNS):
                    r = call_once(model, fmt, task)
                    cell.runs.append(r)
                    done += 1
                    print(
                        f"  [{done:3d}/{total}] {fmt:13s} {task.id:18s} "
                        f"ok={r.parse_ok} n={r.n_inputs} resolved={r.resolved} "
                        f"tools={','.join(r.tools) or '—'}"
                        + (f" ERR={r.error}" if r.error else ""),
                        flush=True,
                    )
    print(f"\n{done} calls in {(time.monotonic() - t0) / 60:.1f} min\n")

    # ── report ──
    print(
        f"# Prototype bakeoff — inputs-array (no action) vs prefix_md "
        f"(temp={TEMPERATURE}, N={N_RUNS})\n"
    )
    for model in MODELS:
        print(f"## {model}\n")
        print(
            "| task | kind | parse_ok | terminal% | mean #inputs | all-resolve | "
            "correct |"
        )
        print("|---|---|---|---|---|---|---|")
        for task in TASKS:
            for fmt in formats:
                c = cells[(model, fmt, task.id)]
                parse_ok = c.rate(lambda r: r.parse_ok)
                term = c.rate(lambda r: r.terminal)
                allres = c.rate(
                    lambda r: r.parse_ok and r.n_inputs > 0 and r.resolved == r.n_inputs
                )
                if task.id in _TERMINAL:
                    kind = "terminal"
                    # correct = cleanly terminated (thought-only)
                    correct = c.rate(lambda r: r.terminal)
                elif task.id in _NO_OVERBATCH:
                    kind = "work/dep"
                    # correct = did work, ≤1 op, did NOT false-terminate
                    correct = c.rate(
                        lambda r: r.parse_ok and not r.terminal and r.n_inputs <= 1
                    )
                else:
                    kind = "work"
                    correct = c.rate(
                        lambda r, t=task: (
                            r.parse_ok
                            and not r.terminal
                            and r.n_inputs >= t.expect_min_inputs
                            and all(x in r.tools for x in t.expect_tools)
                        )
                    )
                print(
                    f"| {task.id} | {kind} | {parse_ok * 100:.0f}% | "
                    f"{term * 100:.0f}% | {c.mean_inputs():.1f} | "
                    f"{allres * 100:.0f}% | {correct * 100:.0f}% |"
                )
        print()


if __name__ == "__main__":
    wire_formats.register(InputsArrayFormat())
    main()
