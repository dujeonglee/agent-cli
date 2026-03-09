"""
Agentic Loop CLI — Typer + Rich
ReAct pattern, text parsing (no tool call API)

Supported LLM : Anthropic / OpenAI-compatible / Ollama
Supported Tool : read_file / write_file / edit_file / shell
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import textwrap
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
app     = typer.Typer(help="Agentic Loop CLI — ReAct text parsing, no tool-call API")
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
# Shell Command Auto-detection
# ─────────────────────────────────────────────
def is_shell_command(text: str) -> bool:
    """Check if the first token of *text* looks like a shell command."""
    first_token = text.split()[0] if text.strip() else ""
    if not first_token:
        return False
    # Check if executable exists on PATH
    if shutil.which(first_token) is not None:
        return True
    # Handle path-style commands like ./script.sh or /usr/bin/env
    if first_token.startswith("./") or first_token.startswith("/"):
        return True
    return False


# ─────────────────────────────────────────────
# Rich Render Helpers
# ─────────────────────────────────────────────
def render_header(provider: str, model: str, max_iter: int) -> None:
    console.print()
    t = Text(justify="center")
    t.append("AGENTIC LOOP", style="bold bright_cyan")
    t.append("  ·  Typer + Rich", style="grey50")
    console.print(Panel(
        t,
        subtitle=Text(
            f"provider={provider}  model={model}  max_iter={max_iter}  "
            "ReAct·TextParsing·NoToolAPI",
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
# TOOLS
# ─────────────────────────────────────────────
def tool_read_file(args: dict) -> str:
    path = args.get("path", "")
    try:
        return Path(path).read_text(encoding="utf-8")
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
    """Replace old_str with new_str (exactly one match required)"""
    path    = args.get("path", "")
    old_str = args.get("old_str", "")
    new_str = args.get("new_str", "")
    try:
        text = Path(path).read_text(encoding="utf-8")
        count = text.count(old_str)
        if count == 0:
            raise RuntimeError("old_str not found in file.")
        if count > 1:
            raise RuntimeError(f"old_str found {count} times. Please be more specific.")
        result = text.replace(old_str, new_str, 1)
        Path(path).write_text(result, encoding="utf-8")
        return f"Edit complete: {path}"
    except RuntimeError:
        raise
    except Exception as e:
        raise RuntimeError(f"edit_file failed: {e}")


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
    "edit_file":  '{"path": "file path", "old_str": "text to replace", "new_str": "new text"}',
    "shell":      '{"command": "shell command to run", "timeout": 30}',
}

TOOL_DESCS = {
    "read_file":  "Read and return file contents.",
    "write_file": "Create or overwrite a file at the given path.",
    "edit_file":  "Replace old_str with new_str exactly once in the file.",
    "shell":      "Run a shell command and return stdout/stderr.",
}


# ─────────────────────────────────────────────
# SYSTEM PROMPT
# ─────────────────────────────────────────────
def build_system_prompt() -> str:
    tool_block = "\n".join(
        f"- {name}: {TOOL_DESCS[name]}\n  Input JSON: {TOOL_SCHEMAS[name]}"
        for name in TOOLS
    )
    return textwrap.dedent(f"""
        You are an AI assistant that solves tasks step-by-step using available tools.

        ## Response Format (STRICT)
        Always respond in exactly one of these two formats:

        Format A — use a tool:
        Thought: [reasoning]
        Action: [tool_name]
        Action Input: [JSON]

        Format B — final answer:
        Thought: [reasoning]
        Final Answer: [complete answer]

        ## Available Tools
        {tool_block}

        ## Rules
        1. Always start with "Thought:"
        2. Action Input must be valid JSON
        3. If Observation shows STATUS: error, fix parameters and retry
        4. Respond in the same language as the user
        5. Do NOT write "Observation:" — that is injected by the system
        6. Output ONLY the format above, nothing else
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
    r = requests.post(url, headers=headers, json=body, timeout=120)
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
    body    = {"model": model, "max_tokens": 2048, "messages": msgs}
    r = requests.post(url, headers=headers, json=body, timeout=120)
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
    body = {"model": model, "stream": False, "messages": msgs}
    r = requests.post(url, json=body, timeout=300)
    r.raise_for_status()
    return r.json()["message"]["content"]


LLM_CALLERS = {
    "anthropic": call_anthropic,
    "openai":    call_openai,
    "ollama":    call_ollama,
}


# ─────────────────────────────────────────────
# REACT PARSER
# ─────────────────────────────────────────────
def parse_react(text: str) -> dict:
    result = {"thought": None, "action": None, "action_input": None, "final": None, "raw": text}

    m = re.search(r"Thought:\s*([\s\S]*?)(?=\n(?:Action:|Final Answer:)|$)", text, re.I)
    if m:
        result["thought"] = m.group(1).strip()

    m = re.search(r"Final Answer:\s*([\s\S]+)$", text, re.I)
    if m:
        result["final"] = m.group(1).strip()
        return result

    m = re.search(r"Action:\s*([^\n]+)", text, re.I)
    if m:
        result["action"] = m.group(1).strip()

    m = re.search(r"Action Input:\s*([\s\S]+?)(?=\n\n|$)", text, re.I)
    if m:
        raw_input = m.group(1).strip()
        try:
            result["action_input"] = json.loads(raw_input)
        except json.JSONDecodeError:
            json_match = re.search(r"\{[\s\S]*\}", raw_input)
            if json_match:
                try:
                    result["action_input"] = json.loads(json_match.group())
                except json.JSONDecodeError:
                    result["action_input"] = raw_input
            else:
                result["action_input"] = raw_input

    return result


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
) -> str | None:
    """Run one agentic loop. Returns the final answer string, or None."""
    render_header(provider, model, max_iter)
    render_status("running", "Initializing loop...")
    console.print()

    system = build_system_prompt()
    caller = LLM_CALLERS[provider]

    # If a ContextManager is provided, add the query and use its buffer;
    # otherwise fall back to a plain list (single-shot mode).
    if ctx is not None:
        ctx.add("user", query)
        messages = ctx.get_messages()
    else:
        messages = [{"role": "user", "content": query}]

    iteration = 0

    while iteration < max_iter:
        iteration += 1
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
            render_step("error", f"LLM call failed: {e}", iteration)
            render_status("error", str(e))
            return None

        render_raw(llm_text, iteration, verbose)

        # ── 2. Parse
        parsed = parse_react(llm_text)

        if parsed["thought"]:
            render_step("thought", parsed["thought"], iteration)

        # ── 3. Final Answer
        if parsed["final"]:
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

            render_step("action", "", iteration, tool_name=tool_name, tool_input=input_str)

            tool_fn = TOOLS.get(tool_name)
            if tool_fn is None:
                observation = (
                    f"STATUS: error\nERROR: Unknown tool '{tool_name}'\n"
                    f"HINT: Available tools: {', '.join(TOOLS)}"
                )
            else:
                render_status("running", f"Executing {tool_name}...", iteration)
                try:
                    obs = tool_fn(tool_input if isinstance(tool_input, dict) else {})
                    observation = f"STATUS: success\nRESULT:\n{obs}"
                except Exception as e:
                    observation = (
                        f"STATUS: error\nERROR: {e}\n"
                        f"HINT: Check parameters and try again."
                    )

            render_step("observation", observation, iteration)

            # ── 5. Inject Observation into message history
            messages.append({"role": "assistant", "content": llm_text})
            messages.append({
                "role": "user",
                "content": f"Observation: {observation}\n\nContinue with the next step.",
            })

            # Mirror into ContextManager so it persists across turns
            if ctx is not None:
                ctx.add("assistant", llm_text)
                ctx.add("user", f"Observation: {observation}\n\nContinue with the next step.")

        else:
            render_step(
                "error",
                f"Parse failed: Could not find Action or Final Answer.\n\n{llm_text}",
                iteration,
            )
            render_status("error", "Parse error")
            return None

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
        10, "--max-iter", "-n",
        help="Maximum iterations",
    ),
    verbose: bool = typer.Option(
        False, "--verbose", "-v",
        help="Show raw LLM response",
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
    """
    # ── /sh prefix: Run shell command directly without LLM
    if query.startswith("/sh ") or query == "/sh":
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

    run_loop(
        query    = query,
        provider = provider,
        model    = resolved_model,
        base_url = resolved_url,
        api_key  = api_key,
        max_iter = max_iter,
        verbose  = verbose,
    )


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
        10, "--max-iter", "-n",
        help="Maximum iterations per turn",
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
        )


if __name__ == "__main__":
    app()
