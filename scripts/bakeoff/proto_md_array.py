"""THROWAWAY prototype bakeoff — markdown envelope + flat action-array.

NOT production. The shape under test (registered at runtime, never shipped):

    ## Thought
    read auth.py and list src
    ## Action
    [{"action": "read_file", "path": "src/auth.py"},
     {"action": "shell", "command": "ls src/"}]

  - markdown envelope (## Thought / ## Action) — the prefix_md shape the model
    already emits reliably (solves the pure-JSON terminal envelope-drop).
  - ## Action body = JSON array of flat {action, ...params} ops. Multi-op via
    array. Explicit per-op `action`; plain param keys (NO wire-key prefix).
  - terminal = thought-only (## Action omitted). The thought is the final answer.
  - `complete` not exposed; `ready_for_review` kept as an op.

Measures: work array compliance, ops that drop `action` (the cost of dropping
the prefix), thought-only termination, false-terminate on work tasks.

    OMLX_API_KEY=... python scripts/bakeoff/proto_md_array.py
"""

from __future__ import annotations

import json
import os
import re
import statistics
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field

from agent_cli import wire_formats
from agent_cli.prompts.system_prompt import build_system_prompt
from agent_cli.providers.capabilities import ModelCapabilities
from agent_cli.tools.registry import TOOLS
from agent_cli.wire_formats.base import ParsedAction, WireFormat

BASE_URL = os.environ.get("OMLX_BASE_URL", "http://192.168.0.44:8000/v1")
API_KEY = os.environ.get("OMLX_API_KEY", "")  # secret: env only, never committed
CHAT_URL = BASE_URL.rstrip("/") + "/chat/completions"
MODELS = [
    m.strip()
    for m in os.environ.get("BAKEOFF_MODELS", "Qwen3.6-27B-MLX-8bit").split(",")
    if m.strip()
]
N_RUNS = int(os.environ.get("BAKEOFF_N_RUNS", "3"))
TEMPERATURE = float(os.environ.get("BAKEOFF_TEMP", "0.0"))
# no-batch variant: drop per-tool batch (reads list); one target per op, so
# the op-array IS the batch mechanism (N files = N read_file ops).
NO_BATCH = os.environ.get("BAKEOFF_NO_BATCH") == "1"
FAIL_DUMP = os.environ.get("BAKEOFF_FAIL_DUMP", "/tmp/proto_md_array_fails.txt")

CAPS = ModelCapabilities(
    context_window=262144,
    max_output_tokens=4096,
    supports_structured_output=False,
    supports_thinking=False,
    thinking_budget=0,
    supports_strict_schema=False,
)

_THOUGHT_RE = re.compile(r"^##\s*Thought\s*$", re.MULTILINE)
_ACTION_RE = re.compile(r"^##\s*Action\s*$", re.MULTILINE)


def _flatten(prefixed: dict) -> tuple[str | None, dict]:
    """{read_file_path: x} -> ('read_file', {path: x}). The tool is the longest
    registered name whose ``{name}_`` prefix every key shares."""
    if not isinstance(prefixed, dict) or not prefixed:
        return None, prefixed
    for name in sorted(TOOLS, key=len, reverse=True):
        pfx = name + "_"
        if all(k.startswith(pfx) for k in prefixed):
            return name, {k[len(pfx) :]: v for k, v in prefixed.items()}
    return None, prefixed


class MdArrayFormat(WireFormat):
    name = "md_array"
    thought_required = False
    action_required = False

    def render_action_input(self, action_input: dict) -> str:
        # Tool guides pass an add_prefix'd dict; render it in this format's flat
        # op shape so the guides match the format rules.
        action, params = _flatten(action_input)
        if action is not None:
            return json.dumps({"action": action, **params}, ensure_ascii=False)
        return json.dumps(action_input, ensure_ascii=False)

    def render_full_example(self, *, thought, action: str, action_input: str) -> str:
        th = thought if thought is not None else "your reasoning"
        # action_input is already this format's flat op JSON (via render_action_input
        # when called through the builder); wrap a single op into the array.
        return f"## Thought\n{th}\n\n## Action\n[{action_input}]"

    def format_rules(self) -> str:
        return (
            "Respond in TWO markdown sections:\n"
            "\n"
            "## Thought\n"
            "<your reasoning>\n"
            "\n"
            "## Action\n"
            "<a JSON array of one or more tool calls>\n"
            "\n"
            'Each array element is one tool call: {"action": "<tool name>", '
            "<its parameters>}. Use the parameter names shown in each tool's "
            "guide above (plain, no prefix). For several INDEPENDENT operations "
            "in one turn, add multiple elements. If a later op depends on an "
            "earlier op's result, emit only the first now — its observation "
            "comes next turn.\n"
            "\n"
            "Rules:\n"
            "1. Always include a `## Thought`.\n"
            '2. Each `## Action` element must have an "action" naming one tool.\n'
            "3. When the task is DONE and nothing remains to run, OMIT the "
            "`## Action` section entirely. A `## Thought`-only response means "
            "the task is complete, and your thought is the final answer.\n"
            "4. As long as work remains, you MUST include `## Action`.\n"
            "5. Respond in the user's language.\n"
            + (
                "6. IMPORTANT: each op acts on ONE target. To read N files, emit "
                'N separate {"action":"read_file","path":...} ops. NEVER put a '
                "list of items inside a single op (no nested arrays).\n"
                if NO_BATCH
                else ""
            )
            + "\n"
            "Several independent operations:\n"
            "## Thought\nRead auth.py and list src/.\n"
            "## Action\n"
            '[{"action": "read_file", "path": "src/auth.py"}, '
            '{"action": "shell", "command": "ls src/"}]\n'
            "\n"
            "Done (no action):\n"
            "## Thought\nThe login() function is implemented and tests pass."
        )

    def parse(self, llm_text: str) -> ParsedAction:
        thought, ops, status = _parse_md_array(llm_text)
        if status == "ops":
            return ParsedAction(
                thought=thought,
                action=ops[0].get("action"),
                action_input=ops,
                raw=llm_text,
                parse_stage=1,
            )
        if status == "terminal":
            return ParsedAction(thought=thought, raw=llm_text, parse_stage=1)
        return ParsedAction(raw=llm_text, parse_stage=0)  # malformed

    # abstract stubs
    def format_rules_anchor(self) -> str:
        return "Respond with ## Thought and (if work remains) ## Action."

    def format_rules_field_specific(self) -> str:
        return "1. Populate ## Thought.\n2. ## Action is a JSON array of ops."

    def constraint_reminder_call(self) -> str:
        return ""

    def constraint_reminder_action_required(self) -> str:
        return ""

    def failure_framing_parse_fail(self) -> str:
        return "Your last response was not in the expected format."

    def failure_framing_no_action(self) -> str:
        return "Your `## Action` had no usable tool call."

    def static_retry_hint_no_json(self) -> str:
        return "Emit ## Thought and ## Action (a JSON array of ops)."

    def static_retry_hint_no_action(self) -> str:
        return 'Each ## Action element needs an "action" naming a tool.'

    def system_user_prefixes(self) -> tuple[str, ...]:
        return ()


def _extract_first_json(body: str):
    """Parse the first balanced [...] or {...} from body, or None."""
    if body.startswith("```"):
        body = body.split("\n", 1)[1] if "\n" in body else body
        if body.rfind("```") > 0:
            body = body[: body.rfind("```")]
    opens = {"[": "]", "{": "}"}
    start = next((i for i, c in enumerate(body) if c in opens), -1)
    if start < 0:
        return None
    open_c = body[start]
    close_c = opens[open_c]
    depth = 0
    for i in range(start, len(body)):
        if body[i] == open_c:
            depth += 1
        elif body[i] == close_c:
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(body[start : i + 1])
                except json.JSONDecodeError:
                    return None
    return None


def _parse_md_array(text: str) -> tuple[str | None, list, str]:
    """Return (thought, ops, status). status ∈ {ops, terminal, malformed}.
    - terminal: no ## Action, or an empty ## Action body (= no work, done).
      Bare text with no ## Thought also terminal (whole text is the answer).
    - ops: ## Action parsed to ≥1 op dict (array, OR a bare object = 1 op).
    - malformed: ## Action had non-empty content that didn't parse to ops."""
    tm = _THOUGHT_RE.search(text)
    am = _ACTION_RE.search(text)
    thought = None
    if tm:
        end = am.start() if (am and am.start() > tm.end()) else len(text)
        thought = text[tm.end() : end].strip()
    elif not am:
        thought = text.strip()  # plain text, no headers → the answer
    if not am:
        return thought, [], "terminal"
    body = text[am.end() :].strip()
    if not body:
        return thought, [], "terminal"  # empty ## Action = done
    # (a) lenient terminal: explicit "no action" markers the model writes
    # instead of omitting the section.
    if body.lower().rstrip(".").strip() in ("none", "n/a", "null", "nothing"):
        return thought, [], "terminal"
    parsed = _extract_first_json(body)
    if parsed is None:
        return thought, [], "malformed"
    arr = parsed if isinstance(parsed, list) else [parsed]  # bare object = 1 op
    ops = [x for x in arr if isinstance(x, dict)]
    if not ops:
        return thought, [], "malformed"
    # (a) a single result-bearing object with no `action` is a completion
    # attempt (the model reaching for an explicit answer) → terminal, answer
    # taken from `result`. (A no-action op WITHOUT result is a work op that
    # dropped its action — that stays "ops" and is measured as such.)
    if len(ops) == 1 and "action" not in ops[0] and "result" in ops[0]:
        return (ops[0].get("result") or thought), [], "terminal"
    return thought, ops, "ops"


@dataclass(frozen=True)
class Task:
    id: str
    query: str
    active_tools: tuple[str, ...]
    expect_min_ops: int
    expect_actions: tuple[str, ...]


_TOOLS = ("read_file", "shell", "code_index", "edit_file", "ready_for_review")
TASKS = [
    Task(
        "single_read",
        "Read src/auth.py to understand the auth flow.",
        _TOOLS,
        1,
        ("read_file",),
    ),
    Task("shell_only", "List every Python file under src/.", _TOOLS, 1, ("shell",)),
    Task("two_files", "Read src/a.py and src/b.py.", _TOOLS, 1, ("read_file",)),
    Task(
        "batch_read_three",
        "Read these three files: src/a.py, src/b.py, src/c.py.",
        _TOOLS,
        1,
        ("read_file",),
    ),
    Task(
        "multi_read_shell",
        "Do these two INDEPENDENT things: read src/auth.py, and separately list src/ with `ls src/`.",
        _TOOLS,
        2,
        ("read_file", "shell"),
    ),
    Task(
        "multi_three",
        "Do these three INDEPENDENT things in one go: read src/auth.py; list src/ with `ls src/`; "
        "and look up the symbol `login` in the code index.",
        _TOOLS,
        3,
        ("read_file", "shell", "code_index"),
    ),
    Task(
        "mixed_indep",
        "Two independent things: search where `login` is defined, and separately list tests/.",
        _TOOLS,
        2,
        (),
    ),
    Task(
        "dependent_read_edit",
        "Read src/auth.py and fix the typo in its docstring.",
        _TOOLS,
        1,
        ("read_file",),
    ),
    Task(
        "dependent_chain",
        "Find which file defines `login()` and then read that file. (You do not know the file yet.)",
        _TOOLS,
        1,
        (),
    ),
    # terminal (thought-only)
    Task(
        "greet_only", "Just greet me with a short hello. No work needed.", _TOOLS, 0, ()
    ),
    Task(
        "finish_after_work",
        "You just finished implementing login() and it passes all tests. Nothing is left — wrap up.",
        _TOOLS,
        0,
        (),
    ),
]
_NO_OVERBATCH = {"dependent_read_edit", "dependent_chain"}
_TERMINAL = {"greet_only", "finish_after_work"}


@dataclass
class Run:
    raw: str
    n_ops: int
    valid_action_ops: int  # ops with a known `action`
    actions: tuple[str, ...]
    parse_ok: bool
    terminal: bool = False
    error: str | None = None


def _dump(model, task, raw):
    with open(FAIL_DUMP, "a", encoding="utf-8") as f:
        f.write(f"\n=== {model} | {task.id} ===\n{raw}\n")


_PREFIXES = ("read_file_", "shell_", "code_index_", "edit_file_", "delegate_")


def _clean_prompt(sp: str) -> str:
    """Make build_system_prompt's output consistent with THIS format:
      - don't expose `complete` (the design doesn't),
      - strip leaked wire-key prefixes from tool-guide prose (read_file_reads ->
        reads) so the guides match the flat {action, plain} convention.
    Production refactor (format-aware guides) is deferred to real implementation;
    this keeps the prod prompt structure but removes the convention leaks for a
    fair measurement."""
    out = []
    for line in sp.splitlines():
        if line.strip().startswith("- complete:"):
            continue
        line = line.replace(
            "Call this BEFORE complete to verify", "Call this to verify when done"
        )
        for p in _PREFIXES:
            line = line.replace(p, "")
        if NO_BATCH:
            # neutralize read_file's batch language so the guide matches the
            # one-file-per-op rule.
            line = line.replace(
                "Read one or more files in a single call. Provide reads as a "
                "list; each item reads one file with an optional mode.",
                "Read a single file by `path` (optional line_start/line_end/"
                "search/mode).",
            )
        out.append(line)
    return "\n".join(out)


def call_once(model: str, task: Task) -> Run:
    plugin = wire_formats.get("md_array")
    system = _clean_prompt(
        build_system_prompt(
            capabilities=CAPS, active_tools=list(task.active_tools), wire_format=plugin
        )
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

    thought, ops, status = _parse_md_array(raw)
    if status == "ops":
        actions = [o.get("action") for o in ops]
        valid = [a for a in actions if a in TOOLS]
        if len(valid) != len(ops):
            _dump(model, task, raw)
        return Run(raw, len(ops), len(valid), tuple(a for a in actions if a), True)
    if status == "terminal":
        return Run(raw, 0, 0, (), True, terminal=True)
    _dump(model, task, raw)  # malformed
    return Run(raw, 0, 0, (), False)


@dataclass
class Cell:
    runs: list[Run] = field(default_factory=list)

    def rate(self, pred):
        return (
            (sum(1 for r in self.runs if pred(r)) / len(self.runs))
            if self.runs
            else 0.0
        )

    def mean_ops(self):
        v = [r.n_ops for r in self.runs if r.parse_ok and not r.terminal]
        return statistics.mean(v) if v else 0.0


def main():
    cells = {}
    total = len(MODELS) * len(TASKS) * N_RUNS
    done = 0
    t0 = time.monotonic()
    for model in MODELS:
        print(f"\n=== {model} ===", flush=True)
        for task in TASKS:
            c = cells.setdefault((model, task.id), Cell())
            for _ in range(N_RUNS):
                r = call_once(model, task)
                c.runs.append(r)
                done += 1
                print(
                    f"  [{done:3d}/{total}] {task.id:20s} ok={r.parse_ok} "
                    f"term={r.terminal} n_ops={r.n_ops} valid={r.valid_action_ops} "
                    f"actions={','.join(r.actions) or '-'}"
                    + (f" ERR={r.error}" if r.error else ""),
                    flush=True,
                )
    print(f"\n{done} calls in {(time.monotonic() - t0) / 60:.1f} min\n")

    print(f"# md_array bakeoff (temp={TEMPERATURE}, N={N_RUNS})\n")
    for model in MODELS:
        print(f"## {model}\n")
        print(
            "| task | kind | parse_ok | terminal% | mean_ops | all-valid-action | correct |"
        )
        print("|---|---|---|---|---|---|---|")
        for task in TASKS:
            c = cells[(model, task.id)]
            parse_ok = c.rate(lambda r: r.parse_ok)
            term = c.rate(lambda r: r.terminal)
            allvalid = c.rate(
                lambda r: r.parse_ok and r.n_ops > 0 and r.valid_action_ops == r.n_ops
            )
            if task.id in _TERMINAL:
                kind, correct = "terminal", c.rate(lambda r: r.terminal)
            elif task.id in _NO_OVERBATCH:
                kind = "work/dep"
                correct = c.rate(
                    lambda r: r.parse_ok and not r.terminal and r.n_ops <= 1
                )
            else:
                kind = "work"
                correct = c.rate(
                    lambda r, t=task: (
                        r.parse_ok
                        and not r.terminal
                        and r.n_ops >= t.expect_min_ops
                        and all(a in r.actions for a in t.expect_actions)
                    )
                )
            print(
                f"| {task.id} | {kind} | {parse_ok * 100:.0f}% | {term * 100:.0f}% | "
                f"{c.mean_ops():.1f} | {allvalid * 100:.0f}% | {correct * 100:.0f}% |"
            )
        print()


if __name__ == "__main__":
    wire_formats.register(MdArrayFormat())
    main()
