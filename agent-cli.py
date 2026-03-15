"""
Agentic Loop CLI — Typer + Rich
ReAct pattern, JSON response format (no tool call API)

Supported LLM : Anthropic / OpenAI-compatible / Ollama
Supported Tool : read_file / write_file / edit_file / shell / delegate
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import textwrap
import zlib
from pathlib import Path
from typing import Optional

import requests
import typer
from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.rule import Rule
from rich.text import Text

# ─────────────────────────────────────────────
# App / Console
# ─────────────────────────────────────────────
app     = typer.Typer(help="Agentic Loop CLI — ReAct JSON format, no tool-call API")
console = Console()

# ─────────────────────────────────────────────
# Color Theme
# ─────────────────────────────────────────────
C = {
    "thought":     "cyan",
    "action":      "green",
    "observation": "medium_purple",
    "final":       "yellow",
    "error":       "red",
    "raw":         "grey50",
    "muted":       "grey46",
    "accent":      "bright_cyan",
}
ICONS = {
    "thought":     "💭",
    "action":      "⚡",
    "observation": "👁 ",
    "final":       "✅",
    "error":       "⚠ ",
    "raw":         "📄",
}



# ─────────────────────────────────────────────
# Rich Render Helpers
# ─────────────────────────────────────────────
def render_header(provider: str, model: str, max_iter: int) -> None:
    console.print()
    t = Text(justify="center")
    t.append("AGENTIC LOOP", style="bold bright_cyan")
    t.append("  ·  Typer + Rich", style="grey50")
    iter_label = str(max_iter) if max_iter > 0 else "∞"
    console.print(Panel(
        t,
        subtitle=Text(
            f"provider={provider}  model={model}  max_iter={iter_label}  "
            "ReAct·JSONFormat·NoToolAPI",
            style=C["muted"], justify="center",
        ),
        border_style="bright_cyan",
        box=box.DOUBLE_EDGE,
        padding=(0, 2),
    ))
    console.print()


def render_step(
    step_type: str,
    content: str,
    iteration: int,
    tool_name: str | None = None,
    tool_input: str | None = None,
) -> None:
    color = C[step_type]
    header = Text()
    header.append(f"{ICONS[step_type]} {step_type.upper()}", style=f"bold {color}")
    header.append(f"  iter {iteration}", style=C["muted"])

    if step_type == "action" and tool_name:
        body = Text()
        body.append(tool_name, style=f"bold {color}")
        body.append("\n")
        body.append(tool_input or "", style="bright_green")
    else:
        body = Text(content, style="white")

    console.print(Panel(
        body,
        title=header, title_align="left",
        border_style=color, box=box.ROUNDED, padding=(0, 1),
    ))


def render_raw(text: str, iteration: int, verbose: bool) -> None:
    if not verbose:
        console.print(
            f"  [{C['muted']}]{ICONS['raw']} RAW LLM RESPONSE  iter {iteration}  "
            f"[dim](use --verbose to view)[/dim][/]"
        )
        return
    console.print(Panel(
        Text(text, style=C["raw"]),
        title=Text(f"{ICONS['raw']} RAW LLM RESPONSE  iter {iteration}", style=C["raw"]),
        title_align="left",
        border_style=C["raw"], box=box.ROUNDED, padding=(0, 1),
    ))


def render_iter_sep(iteration: int) -> None:
    console.print(Rule(
        f"[{C['muted']}]ITERATION {iteration}[/]", style=C["muted"],
    ))


def render_status(state: str, message: str, iteration: int = 0) -> None:
    dot = {"running": "bright_cyan", "done": "green", "error": "red"}.get(state, "grey50")
    it  = f"  [bright_cyan]ITER {iteration}[/]" if iteration else ""
    console.print(f"[{dot}]●[/] {message}{it}", highlight=False)


# ─────────────────────────────────────────────
# HASHLINE
# ─────────────────────────────────────────────
_NIBBLE = "ZPMQVRWSNKTXJBYH"
_DICT = [f"{_NIBBLE[i >> 4]}{_NIBBLE[i & 0x0F]}" for i in range(256)]
_RE_SIGNIFICANT = re.compile(r"[\w\d]", re.UNICODE)


def compute_line_hash(idx: int, line: str) -> str:
    """Return a 2-char hash tag for *line* at 1-based *idx*."""
    line = line.rstrip("\r\n").rstrip()
    seed = 0 if _RE_SIGNIFICANT.search(line) else idx
    # CRC32 seeded by XOR-ing the seed into the content bytes
    data = line.encode("utf-8")
    h = zlib.crc32(data, seed) & 0xFF
    return _DICT[h]


def format_hashlines(text: str) -> str:
    """Format file content with hashline tags: LINE#HASH:content"""
    lines = text.split("\n")
    out = []
    for i, line in enumerate(lines, 1):
        tag = compute_line_hash(i, line)
        out.append(f"{i}#{tag}:{line}")
    return "\n".join(out)


def _parse_ref(ref: str) -> tuple[int, str]:
    """Parse a hashline ref like '5#VR' → (5, 'VR')."""
    m = re.match(r"^(\d+)#([A-Z]{2})$", ref)
    if not m:
        raise RuntimeError(f"Invalid hashline ref: '{ref}'. Expected format: LINE#HASH (e.g. 5#VR)")
    return int(m.group(1)), m.group(2)


def _verify_ref(lines: list[str], ref: str) -> int:
    """Verify a hashline ref against actual content. Return 0-based index."""
    line_num, expected_hash = _parse_ref(ref)
    if line_num < 1 or line_num > len(lines):
        raise RuntimeError(f"Line {line_num} out of range (file has {len(lines)} lines)")
    actual_hash = compute_line_hash(line_num, lines[line_num - 1])
    if actual_hash != expected_hash:
        raise RuntimeError(
            f"Hash mismatch at line {line_num}: expected {expected_hash}, "
            f"got {actual_hash}. The file may have changed. "
            f"Re-read the file to get current hashline tags."
        )
    return line_num - 1  # 0-based


# ─────────────────────────────────────────────
# TOOLS
# ─────────────────────────────────────────────
def tool_read_file(args: dict) -> str:
    path = args.get("path", "")
    try:
        text = Path(path).read_text(encoding="utf-8")
        return format_hashlines(text)
    except Exception as e:
        raise RuntimeError(f"read_file failed: {e}")


def tool_write_file(args: dict) -> str:
    path    = args.get("path", "")
    content = args.get("content", "")
    try:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        return f"File saved: {path} ({len(content)} bytes)"
    except Exception as e:
        raise RuntimeError(f"write_file failed: {e}")


def tool_edit_file(args: dict) -> str:
    """Apply hashline-based edits to a file.

    Each edit has: op (replace|append|prepend), pos, end (optional), lines.
    Hash refs are verified before any mutation.
    """
    path  = args.get("path", "")
    edits = args.get("edits", [])
    if not edits:
        raise RuntimeError("No edits provided.")
    try:
        text = Path(path).read_text(encoding="utf-8")
    except Exception as e:
        raise RuntimeError(f"edit_file: cannot read '{path}': {e}")

    file_lines = text.split("\n")

    # Pre-validate all refs before mutating
    for edit in edits:
        op  = edit.get("op", "")
        pos = edit.get("pos")
        end = edit.get("end")
        if op not in ("replace", "append", "prepend"):
            raise RuntimeError(f"Unknown edit op: '{op}'. Use replace|append|prepend.")
        if pos:
            _verify_ref(file_lines, pos)
        if end:
            _verify_ref(file_lines, end)

    # Sort edits bottom-up so earlier splices don't shift later indices
    def _sort_key(edit):
        pos = edit.get("pos")
        if pos:
            n, _ = _parse_ref(pos)
            return -n
        return 0

    sorted_edits = sorted(edits, key=_sort_key)

    for edit in sorted_edits:
        op       = edit["op"]
        pos      = edit.get("pos")
        end      = edit.get("end")
        new_lines = edit.get("lines")
        if isinstance(new_lines, str):
            new_lines = new_lines.split("\n")
        if new_lines is None:
            new_lines = []

        if op == "replace":
            if not pos:
                raise RuntimeError("replace requires 'pos'.")
            start_idx = _verify_ref(file_lines, pos)
            if end:
                end_idx = _verify_ref(file_lines, end)
                file_lines[start_idx:end_idx + 1] = new_lines
            else:
                file_lines[start_idx:start_idx + 1] = new_lines

        elif op == "append":
            if pos:
                idx = _verify_ref(file_lines, pos)
                file_lines[idx + 1:idx + 1] = new_lines
            else:
                file_lines.extend(new_lines)

        elif op == "prepend":
            if pos:
                idx = _verify_ref(file_lines, pos)
                file_lines[idx:idx] = new_lines
            else:
                file_lines[0:0] = new_lines

    result = "\n".join(file_lines)
    try:
        Path(path).write_text(result, encoding="utf-8")
    except Exception as e:
        raise RuntimeError(f"edit_file: cannot write '{path}': {e}")

    return f"Edit complete: {path} ({len(file_lines)} lines)"


def tool_shell(args: dict) -> str:
    cmd     = args.get("command", "")
    timeout = int(args.get("timeout", 30))
    try:
        result = subprocess.run(
            cmd, shell=True, capture_output=True,
            text=True, timeout=timeout,
        )
        out = result.stdout.strip()
        err = result.stderr.strip()
        parts = []
        if out:
            parts.append(out)
        if err:
            parts.append(f"[stderr]\n{err}")
        if result.returncode != 0:
            parts.append(f"[exit code: {result.returncode}]")
        return "\n".join(parts) if parts else "(no output)"
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"Command timed out ({timeout}s)")
    except Exception as e:
        raise RuntimeError(f"shell failed: {e}")


TOOLS: dict[str, callable] = {
    "read_file":  tool_read_file,
    "write_file": tool_write_file,
    "edit_file":  tool_edit_file,
    "shell":      tool_shell,
}

TOOL_SCHEMAS = {
    "read_file":  '{"path": "file path to read"}',
    "write_file": '{"path": "file path to save", "content": "file content"}',
    "edit_file":  '{"path": "file path", "edits": [{"op": "replace|append|prepend", "pos": "LINE#HASH", "end": "LINE#HASH (optional, for range replace)", "lines": ["new lines"]}]}',
    "shell":      '{"command": "shell command to run", "timeout": 30}',
}

TOOL_DESCS = {
    "read_file":  "Read file contents. Lines are tagged as LINE#HASH:content for editing.",
    "write_file": "Create or overwrite a file at the given path with raw content.",
    "edit_file":  "Edit a file using hashline refs from read_file. Ops: replace (pos, optional end for range), append (insert after pos), prepend (insert before pos). lines=[] or null to delete.",
    "shell":      "Run a shell command and return stdout/stderr.",
}

# ─────────────────────────────────────────────
# DELEGATE (subagent)
# ─────────────────────────────────────────────
_VAGUE_REFS = re.compile(
    r"\b(it|this|that|these|those|above|previous|earlier|the same)\b", re.I,
)

def _validate_subtask(task: str) -> str | None:
    """Return an error string if *task* looks under-specified, else None."""
    if len(task.split()) < 5:
        return (
            "Task is too short. The subagent has NO context from this conversation. "
            "Include all necessary details: file paths, specific instructions, etc."
        )
    if _VAGUE_REFS.search(task):
        return (
            "Task contains vague references (e.g. 'it', 'this', 'above') that the "
            "subagent cannot resolve. Rewrite with explicit, self-contained details."
        )
    return None


DELEGATE_SCHEMA = '{"task": "fully self-contained task description"}'
DELEGATE_DESC = (
    "Delegate a self-contained subtask to an independent subagent. "
    "The subagent has NO context from this conversation — the task "
    "description must include ALL necessary details (file paths, content, "
    "specific instructions). Do NOT reference prior conversation."
)


# ─────────────────────────────────────────────
# SYSTEM PROMPT
# ─────────────────────────────────────────────
def build_system_prompt(include_delegate: bool = False) -> str:
    tool_block = "\n".join(
        f"- {name}: {TOOL_DESCS[name]}\n  Input JSON: {TOOL_SCHEMAS[name]}"
        for name in TOOLS
    )
    if include_delegate:
        tool_block += f"\n- delegate: {DELEGATE_DESC}\n  Input JSON: {DELEGATE_SCHEMA}"

    delegate_rules = ""
    if include_delegate:
        delegate_rules = textwrap.dedent("""
            ## Delegation Rules
            - Only delegate tasks that are fully independent and self-contained
            - The subagent has NO memory of this conversation
            - Include ALL details: file paths, content, specific instructions
            - NEVER use pronouns or references to prior context in the task
            - Good: "Read /tmp/data.csv and count the number of rows"
            - Bad: "Analyze the file we discussed earlier"
        """)

    return textwrap.dedent(f"""
        You are an AI assistant that solves tasks step-by-step using available tools.

        ## Response Format (STRICT)
        You MUST respond with a single JSON object and nothing else.
        No markdown fences, no extra text — ONLY the JSON object.

        Format A — use a tool:
        {{"thought": "your reasoning", "action": "tool_name", "action_input": {{...}}}}

        Format B — final answer:
        {{"thought": "your reasoning", "final_answer": "your complete answer"}}

        ## Available Tools
        {tool_block}

        ## Hashline Editing
        read_file returns lines tagged as LINE#HASH:content, e.g.:
          1#VR:def hello():
          2#KT:    return "world"
          3#ZZ:

        To edit, use edit_file with hashline refs copied EXACTLY from read_file output.
        - replace single line:  {{"op": "replace", "pos": "2#KT", "lines": ["    return \\"hello\\""]}}
        - replace range:        {{"op": "replace", "pos": "1#VR", "end": "3#ZZ", "lines": ["def greet():", "    pass"]}}
        - delete lines:         {{"op": "replace", "pos": "2#KT", "lines": []}}
        - insert after:         {{"op": "append", "pos": "1#VR", "lines": ["    # new comment"]}}
        - insert before:        {{"op": "prepend", "pos": "1#VR", "lines": ["# header"]}}
        - append to EOF:        {{"op": "append", "lines": ["# end of file"]}}

        IMPORTANT: Always read the file first to get current hashline tags.
        If a hash mismatch error occurs, re-read the file and retry with fresh tags.
        Use write_file only for creating NEW files, not for editing existing ones.
        {delegate_rules}
        ## Rules
        1. Always include "thought" in your JSON
        2. "action_input" must match the tool's input schema
        3. If observation shows error, fix parameters and retry
        4. Respond in the same language as the user
        5. Do NOT include "observation" — that is injected by the system
        6. Output ONLY valid JSON, nothing else
    """).strip()


# ─────────────────────────────────────────────
# LLM ADAPTERS
# ─────────────────────────────────────────────
def call_anthropic(
    messages: list[dict],
    system: str,
    model: str,
    base_url: str,
    api_key: str,
) -> str:
    url     = base_url.rstrip("/") + "/messages"
    headers = {
        "Content-Type":      "application/json",
        "x-api-key":         api_key,
        "anthropic-version": "2023-06-01",
    }
    body = {"model": model, "max_tokens": 2048, "system": system, "messages": messages}
    r = requests.post(url, headers=headers, json=body, timeout=600)
    r.raise_for_status()
    return r.json()["content"][0]["text"]


def call_openai(
    messages: list[dict],
    system: str,
    model: str,
    base_url: str,
    api_key: str,
) -> str:
    url     = base_url.rstrip("/") + "/chat/completions"
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"}
    msgs    = [{"role": "system", "content": system}] + messages
    body    = {"model": model, "max_tokens": 2048, "messages": msgs,
               "response_format": {"type": "json_object"}}
    r = requests.post(url, headers=headers, json=body, timeout=600)
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"]


def call_ollama(
    messages: list[dict],
    system: str,
    model: str,
    base_url: str,
    **_,
) -> str:
    url  = base_url.rstrip("/") + "/api/chat"
    msgs = [{"role": "system", "content": system}] + messages
    body = {"model": model, "stream": False, "messages": msgs, "format": "json"}
    r = requests.post(url, json=body, timeout=600)
    r.raise_for_status()
    return r.json()["message"]["content"]


LLM_CALLERS = {
    "anthropic": call_anthropic,
    "openai":    call_openai,
    "ollama":    call_ollama,
}


# ─────────────────────────────────────────────
# REACT JSON PARSER
# ─────────────────────────────────────────────
def parse_react(text: str) -> dict:
    """Parse the LLM response as a JSON object.

    Returns a dict with keys: thought, action, action_input, final, raw.
    On parse failure all fields except *raw* are None.
    """
    result = {"thought": None, "action": None, "action_input": None, "final": None, "raw": text}

    # Strip markdown fences if the LLM wraps JSON in ```json ... ```
    stripped = re.sub(r"^```(?:json)?\s*", "", text.strip(), flags=re.I)
    stripped = re.sub(r"\s*```\s*$", "", stripped)

    # Try parsing directly
    data = None
    try:
        data = json.loads(stripped)
    except json.JSONDecodeError:
        # Fallback: extract the first { ... } block
        m = re.search(r"\{[\s\S]*\}", stripped)
        if m:
            try:
                data = json.loads(m.group())
            except json.JSONDecodeError:
                pass

    if not isinstance(data, dict):
        return result

    result["thought"] = data.get("thought")
    result["final"] = data.get("final_answer")
    result["action"] = data.get("action")
    result["action_input"] = data.get("action_input")
    return result


# Keywords in user queries that imply tool usage is required
_ACTION_KEYWORDS = re.compile(
    r"\b(write|create|save|make|generate|edit|modify|update|delete|remove|run|execute)\b",
    re.I,
)


def _needs_tool_action(query: str) -> bool:
    """Heuristic: does the user query imply a side-effect (file write, shell, etc.)?"""
    return bool(_ACTION_KEYWORDS.search(query))


# ─────────────────────────────────────────────
# CONTEXT MANAGER
# ─────────────────────────────────────────────
COMPRESS_PROMPT = textwrap.dedent("""
    Summarize the following conversation concisely.
    Preserve all key facts, decisions, tool results, and context
    needed to continue the conversation. Drop verbose tool outputs
    and redundant reasoning. Reply with ONLY the summary.
""").strip()

DEFAULT_MAX_CONTEXT_CHARS = 50_000          # ~12 500 tokens


class ContextManager:
    """Manages conversation message history with automatic compression.

    When the total character count of messages exceeds *max_context_chars*,
    older messages are compressed into a single summary message via the LLM,
    while the most recent *keep_recent* message pairs are preserved verbatim.
    """

    def __init__(
        self,
        caller,
        model: str,
        base_url: str,
        api_key: str,
        system: str,
        max_context_chars: int = DEFAULT_MAX_CONTEXT_CHARS,
        keep_recent: int = 4,
    ):
        self.caller    = caller
        self.model     = model
        self.base_url  = base_url
        self.api_key   = api_key
        self.system    = system
        self.max_context_chars = max_context_chars
        self.keep_recent       = keep_recent
        self.messages: list[dict] = []
        self._summary: str | None = None

    # ── public helpers ──────────────────────────

    def add(self, role: str, content: str) -> None:
        self.messages.append({"role": role, "content": content})
        if self._total_chars() > self.max_context_chars:
            self._compress()

    def get_messages(self) -> list[dict]:
        msgs: list[dict] = []
        if self._summary:
            msgs.append({
                "role": "user",
                "content": f"[Previous conversation summary]\n{self._summary}",
            })
            msgs.append({
                "role": "assistant",
                "content": "Understood. I have the context from our previous conversation.",
            })
        msgs.extend(self.messages)
        return msgs

    # ── internals ───────────────────────────────

    def _total_chars(self) -> int:
        extra = len(self._summary) if self._summary else 0
        return extra + sum(len(m["content"]) for m in self.messages)

    def _compress(self) -> None:
        # Keep the last *keep_recent* messages untouched
        keep = self.keep_recent * 2          # pairs of user+assistant
        if len(self.messages) <= keep:
            return                            # nothing old enough to compress

        old_msgs  = self.messages[:-keep]
        kept_msgs = self.messages[-keep:]

        # Build text to summarize
        parts = []
        if self._summary:
            parts.append(f"[Prior summary]\n{self._summary}")
        for m in old_msgs:
            parts.append(f"{m['role'].upper()}: {m['content']}")
        text_to_summarize = "\n\n".join(parts)

        render_status("running", "Compressing context...")
        try:
            summary = self.caller(
                messages=[{"role": "user", "content": text_to_summarize}],
                system=COMPRESS_PROMPT,
                model=self.model,
                base_url=self.base_url,
                api_key=self.api_key,
            )
            self._summary = summary
            self.messages = kept_msgs
            render_status("done", f"Context compressed ({len(summary)} chars)")
        except Exception as e:
            render_status("error", f"Context compression failed: {e}")


# ─────────────────────────────────────────────
# AGENTIC LOOP
# ─────────────────────────────────────────────
def run_loop(
    query: str,
    provider: str,
    model: str,
    base_url: str,
    api_key: str,
    max_iter: int,
    verbose: bool,
    ctx: ContextManager | None = None,
    quiet: bool = False,
    depth: int = 0,
    max_depth: int = 2,
    delegate_timeout: int = 300,
) -> str | None:
    """Run one agentic loop. Returns the final answer string, or None."""
    include_delegate = depth < max_depth
    if not quiet:
        render_header(provider, model, max_iter)
        render_status("running", "Initializing loop...")
        console.print()

    system = build_system_prompt(include_delegate=include_delegate)
    caller = LLM_CALLERS[provider]

    # If a ContextManager is provided, add the query and use its buffer;
    # otherwise fall back to a plain list (single-shot mode).
    if ctx is not None:
        ctx.add("user", query)
        messages = ctx.get_messages()
    else:
        messages = [{"role": "user", "content": query}]

    # Build available tool names for error hints
    available_tools = list(TOOLS)
    if include_delegate:
        available_tools.append("delegate")

    iteration = 0
    tools_called: list[str] = []          # track which tools were actually used

    while max_iter <= 0 or iteration < max_iter:
        iteration += 1
        if not quiet:
            if iteration > 1:
                render_iter_sep(iteration)
            render_status("running", f"Calling LLM...", iteration)

        # ── 1. Call LLM
        try:
            llm_text = caller(
                messages=messages,
                system=system,
                model=model,
                base_url=base_url,
                api_key=api_key,
            )
        except Exception as e:
            if not quiet:
                render_step("error", f"LLM call failed: {e}", iteration)
                render_status("error", str(e))
            return None

        if not quiet:
            render_raw(llm_text, iteration, verbose)

        # ── 2. Parse
        parsed = parse_react(llm_text)

        if not quiet and parsed["thought"]:
            render_step("thought", parsed["thought"], iteration)

        # ── 3. Final Answer
        if parsed["final"]:
            # Fulfillment guard: if the task required tool actions (write, create,
            # etc.) but no tool was ever called, reject the final answer and ask
            # the LLM to actually perform the action.
            if not tools_called and _needs_tool_action(query):
                if not quiet:
                    render_status(
                        "running",
                        "Final answer given but task requires tool action — sending back...",
                        iteration,
                    )
                nudge = (
                    "You provided a final_answer, but the task requires you to "
                    "actually USE a tool (e.g. write_file, shell) to complete it. "
                    "Do NOT put file contents in final_answer. "
                    "Instead, respond with {\"thought\": ..., \"action\": ..., \"action_input\": ...}."
                )
                messages.append({"role": "assistant", "content": llm_text})
                messages.append({"role": "user", "content": nudge})
                if ctx is not None:
                    ctx.add("assistant", llm_text)
                    ctx.add("user", nudge)
                continue

            if not quiet:
                render_step("final", parsed["final"], iteration)
                render_status("done", "Loop completed successfully", iteration)
                console.print()
            if ctx is not None:
                ctx.add("assistant", llm_text)
            return parsed["final"]

        # ── 4. Execute Tool
        if parsed["action"]:
            tool_name  = parsed["action"]
            tool_input = parsed["action_input"] or {}
            input_str  = json.dumps(tool_input, ensure_ascii=False, indent=2) \
                         if isinstance(tool_input, dict) else str(tool_input)

            if not quiet:
                render_step("action", "", iteration, tool_name=tool_name, tool_input=input_str)

            tools_called.append(tool_name)

            # ── Handle delegate tool
            if tool_name == "delegate" and include_delegate:
                task_str = tool_input.get("task", "") if isinstance(tool_input, dict) else ""
                validation_err = _validate_subtask(task_str)
                if validation_err:
                    observation = f"STATUS: error\nERROR: {validation_err}"
                else:
                    if not quiet:
                        render_status("running", f"Delegating subtask (depth {depth + 1})...", iteration)
                    # Spawn subagent as a subprocess
                    cmd = [
                        sys.executable, os.path.abspath(__file__),
                        "run", task_str,
                        "--provider", provider,
                        "--model", model,
                        "--base-url", base_url,
                        "--api-key", api_key or "",
                        "--max-depth", str(max_depth),
                        "--depth", str(depth + 1),
                        "--delegate-timeout", str(delegate_timeout),
                        "--quiet",
                    ]
                    if max_iter > 0:
                        cmd.extend(["--max-iter", str(max_iter)])
                    try:
                        result = subprocess.run(
                            cmd, capture_output=True, text=True,
                            timeout=delegate_timeout,
                        )
                        stdout = result.stdout.strip()
                        stderr = result.stderr.strip()
                        if result.returncode == 0 and stdout:
                            observation = f"STATUS: success\nRESULT:\n{stdout}"
                        else:
                            err_detail = stderr or stdout or "Subagent returned no output"
                            observation = f"STATUS: error\nERROR: {err_detail}"
                    except subprocess.TimeoutExpired:
                        observation = (
                            f"STATUS: error\nERROR: Subagent timed out ({delegate_timeout}s)"
                        )

            # ── Handle regular tools
            else:
                tool_fn = TOOLS.get(tool_name)
                if tool_fn is None:
                    observation = (
                        f"STATUS: error\nERROR: Unknown tool '{tool_name}'\n"
                        f"HINT: Available tools: {', '.join(available_tools)}"
                    )
                else:
                    if not quiet:
                        render_status("running", f"Executing {tool_name}...", iteration)
                    try:
                        obs = tool_fn(tool_input if isinstance(tool_input, dict) else {})
                        observation = f"STATUS: success\nRESULT:\n{obs}"
                    except Exception as e:
                        observation = (
                            f"STATUS: error\nERROR: {e}\n"
                            f"HINT: Check parameters and try again."
                        )

            if not quiet:
                render_step("observation", observation, iteration)

            # ── 5. Inject Observation into message history
            messages.append({"role": "assistant", "content": llm_text})
            obs_msg = f"Observation: {observation}\n\nContinue with the next step. Respond with JSON only."
            messages.append({"role": "user", "content": obs_msg})

            # Mirror into ContextManager so it persists across turns
            if ctx is not None:
                ctx.add("assistant", llm_text)
                ctx.add("user", obs_msg)

        else:
            # ── Retry with a format reminder
            if not quiet:
                render_status("running", "Response is not valid JSON, retrying...", iteration)
            retry_msg = (
                "Your response was not valid JSON. "
                "You MUST respond with a single JSON object and nothing else.\n\n"
                'Format A (use a tool):\n'
                '{"thought": "reasoning", "action": "tool_name", "action_input": {...}}\n\n'
                'Format B (final answer):\n'
                '{"thought": "reasoning", "final_answer": "answer"}\n\n'
                "Output ONLY the JSON object, no markdown fences or extra text."
            )
            messages.append({"role": "assistant", "content": llm_text})
            messages.append({"role": "user", "content": retry_msg})
            if ctx is not None:
                ctx.add("assistant", llm_text)
                ctx.add("user", retry_msg)
            iteration -= 1  # Don't count format retries as iterations

    if not quiet:
        render_step("error", f"Maximum iterations ({max_iter}) reached.", iteration)
        render_status("error", f"Max iterations ({max_iter}) reached")
        console.print()
    return None


# ─────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────
PROVIDER_DEFAULTS = {
    "anthropic": ("https://api.anthropic.com/v1",    "claude-sonnet-4-20250514"),
    "openai":    ("https://api.openai.com/v1",        "gpt-4o"),
    "ollama":    ("http://localhost:11434",            "qwen3:32b"),
}

def _resolve_provider(provider, model, base_url, api_key):
    """Shared provider resolution for both commands."""
    if provider not in LLM_CALLERS:
        console.print(f"[red]Unsupported provider: {provider}[/red]")
        console.print(f"Available: {', '.join(LLM_CALLERS)}")
        raise typer.Exit(1)

    default_url, default_model = PROVIDER_DEFAULTS[provider]
    resolved_url   = base_url or default_url
    resolved_model = model    or default_model

    if api_key is None:
        env_map = {"anthropic": "ANTHROPIC_API_KEY", "openai": "OPENAI_API_KEY"}
        api_key = os.environ.get(env_map.get(provider, ""), "")

    return resolved_url, resolved_model, api_key


@app.command()
def run(
    query: str = typer.Argument(..., help="Task to execute"),

    provider: str = typer.Option(
        "ollama", "--provider", "-p",
        help="LLM provider: anthropic | openai | ollama",
    ),
    model: Optional[str] = typer.Option(
        None, "--model", "-m",
        help="Model ID (uses provider default if not specified)",
    ),
    base_url: Optional[str] = typer.Option(
        None, "--base-url",
        help="API base URL (uses provider default if not specified)",
    ),
    api_key: Optional[str] = typer.Option(
        None, "--api-key",
        help="API key (auto-detects from environment if not specified)",
    ),
    max_iter: int = typer.Option(
        0, "--max-iter", "-n",
        help="Maximum iterations (0 = unlimited)",
    ),
    max_depth: int = typer.Option(
        2, "--max-depth",
        help="Maximum subagent nesting depth",
    ),
    delegate_timeout: int = typer.Option(
        300, "--delegate-timeout",
        help="Timeout in seconds for subagent delegation",
    ),
    verbose: bool = typer.Option(
        False, "--verbose", "-v",
        help="Show raw LLM response",
    ),
    quiet: bool = typer.Option(
        False, "--quiet", hidden=True,
        help="Output only the final answer (used internally by subagents)",
    ),
    depth: int = typer.Option(
        0, "--depth", hidden=True,
        help="Current nesting depth (used internally by subagents)",
    ),
):
    """
    ReAct pattern Agentic Loop (single-shot).

    \b
    Examples:
      agent run "List files in the current directory and read README.md"
      agent run "Create test.py with a hello world" -p ollama -m qwen3:8b
      agent run "..." -p anthropic --api-key sk-ant-...
      agent run "/sh ls -la"           # Run shell command directly without LLM

    \b
    vLLM support:
      vLLM exposes an OpenAI-compatible API, so use -p openai with --base-url:
      agent run "..." -p openai --base-url http://localhost:8000/v1 -m your-model
    """
    # ── /sh prefix: Run shell command directly without LLM
    if not quiet and (query.startswith("/sh ") or query == "/sh"):
        cmd = query[3:].strip()
        if not cmd:
            console.print(f"[{C['error']}]No command to execute.[/]")
            raise typer.Exit(1)
        console.print(f"[{C['action']}]⚡ SHELL:[/] {cmd}")
        try:
            result = subprocess.run(
                cmd, shell=True, capture_output=True, text=True, timeout=30,
            )
            if result.stdout:
                console.print(result.stdout, end="", highlight=False)
            if result.stderr:
                console.print(f"[{C['error']}]{result.stderr}[/]", end="")
            if result.returncode != 0:
                console.print(f"[{C['muted']}][exit code: {result.returncode}][/]")
        except subprocess.TimeoutExpired:
            console.print(f"[{C['error']}]Command timed out (30s)[/]")
        raise typer.Exit(0)

    resolved_url, resolved_model, api_key = _resolve_provider(
        provider, model, base_url, api_key,
    )

    answer = run_loop(
        query    = query,
        provider = provider,
        model    = resolved_model,
        base_url = resolved_url,
        api_key  = api_key,
        max_iter = max_iter,
        verbose  = verbose,
        quiet    = quiet,
        depth    = depth,
        max_depth = max_depth,
        delegate_timeout = delegate_timeout,
    )

    # In quiet mode, print only the final answer to stdout (for subagent capture)
    if quiet and answer:
        print(answer)


@app.command()
def chat(
    provider: str = typer.Option(
        "ollama", "--provider", "-p",
        help="LLM provider: anthropic | openai | ollama",
    ),
    model: Optional[str] = typer.Option(
        None, "--model", "-m",
        help="Model ID (uses provider default if not specified)",
    ),
    base_url: Optional[str] = typer.Option(
        None, "--base-url",
        help="API base URL (uses provider default if not specified)",
    ),
    api_key: Optional[str] = typer.Option(
        None, "--api-key",
        help="API key (auto-detects from environment if not specified)",
    ),
    max_iter: int = typer.Option(
        0, "--max-iter", "-n",
        help="Maximum iterations per turn (0 = unlimited)",
    ),
    max_depth: int = typer.Option(
        2, "--max-depth",
        help="Maximum subagent nesting depth",
    ),
    delegate_timeout: int = typer.Option(
        300, "--delegate-timeout",
        help="Timeout in seconds for subagent delegation",
    ),
    max_context: int = typer.Option(
        DEFAULT_MAX_CONTEXT_CHARS, "--max-context",
        help="Max context size in chars before compression",
    ),
    verbose: bool = typer.Option(
        False, "--verbose", "-v",
        help="Show raw LLM response",
    ),
):
    """
    Interactive chat with persistent context and automatic compression.

    \b
    Commands inside chat:
      /quit, /exit   — end the session
      /clear         — reset conversation context
      /sh <cmd>      — run a shell command directly

    \b
    vLLM support:
      vLLM exposes an OpenAI-compatible API, so use -p openai with --base-url:
      agent chat -p openai --base-url http://localhost:8000/v1 -m your-model
    """
    resolved_url, resolved_model, api_key = _resolve_provider(
        provider, model, base_url, api_key,
    )

    caller = LLM_CALLERS[provider]
    system = build_system_prompt()
    ctx = ContextManager(
        caller=caller,
        model=resolved_model,
        base_url=resolved_url,
        api_key=api_key,
        system=system,
        max_context_chars=max_context,
    )

    console.print()
    console.print(Panel(
        Text("Interactive Chat Mode", justify="center", style="bold bright_cyan"),
        subtitle=Text(
            f"provider={provider}  model={resolved_model}  "
            f"max_context={max_context}  /quit to exit",
            style=C["muted"], justify="center",
        ),
        border_style="bright_cyan",
        box=box.DOUBLE_EDGE,
        padding=(0, 2),
    ))
    console.print()

    turn = 0
    while True:
        try:
            query = console.input(f"[bold bright_cyan]You:[/] ").strip()
        except (EOFError, KeyboardInterrupt):
            console.print(f"\n[{C['muted']}]Session ended.[/]")
            break

        if not query:
            continue

        # ── in-chat commands
        if query in ("/quit", "/exit"):
            console.print(f"[{C['muted']}]Session ended.[/]")
            break

        if query == "/clear":
            ctx = ContextManager(
                caller=caller,
                model=resolved_model,
                base_url=resolved_url,
                api_key=api_key,
                system=system,
                max_context_chars=max_context,
            )
            console.print(f"[{C['accent']}]Context cleared.[/]")
            turn = 0
            continue

        if query.startswith("/sh "):
            cmd = query[4:].strip()
            if cmd:
                console.print(f"[{C['action']}]⚡ SHELL:[/] {cmd}")
                try:
                    result = subprocess.run(
                        cmd, shell=True, capture_output=True, text=True, timeout=30,
                    )
                    if result.stdout:
                        console.print(result.stdout, end="", highlight=False)
                    if result.stderr:
                        console.print(f"[{C['error']}]{result.stderr}[/]", end="")
                except subprocess.TimeoutExpired:
                    console.print(f"[{C['error']}]Command timed out (30s)[/]")
            continue

        turn += 1
        console.print(Rule(
            f"[{C['muted']}]TURN {turn}[/]", style=C["muted"],
        ))

        run_loop(
            query    = query,
            provider = provider,
            model    = resolved_model,
            base_url = resolved_url,
            api_key  = api_key,
            max_iter = max_iter,
            verbose  = verbose,
            ctx      = ctx,
            max_depth = max_depth,
            delegate_timeout = delegate_timeout,
        )


if __name__ == "__main__":
    app()
