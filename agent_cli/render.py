"""Rich terminal rendering helpers."""

from __future__ import annotations

from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.rule import Rule
from rich.text import Text

console = Console()

C = {
    "thought": "cyan",
    "action": "green",
    "observation": "medium_purple",
    "final": "yellow",
    "error": "red",
    "raw": "grey50",
    "muted": "grey46",
    "accent": "bright_cyan",
}

ICONS = {
    "thought": "💭",
    "action": "⚡",
    "observation": "👁 ",
    "final": "✅",
    "error": "⚠ ",
    "raw": "📄",
}


def render_header(
    provider: str,
    model: str,
    max_iter: int,
    skill_name: str = "",
    skill_args: str = "",
) -> None:
    console.print()
    t = Text(justify="center")
    if skill_name:
        args_label = f"({skill_args})" if skill_args else ""
        t.append(f"SKILL: {skill_name}{args_label}", style="bold bright_cyan")
    else:
        t.append("AGENTIC LOOP", style="bold bright_cyan")
        t.append("  ·  Typer + Rich", style="grey50")
    iter_label = str(max_iter) if max_iter > 0 else "∞"
    console.print(
        Panel(
            t,
            subtitle=Text(
                f"provider={provider}  model={model}  max_iter={iter_label}  "
                "ReAct·JSONFormat·NoToolAPI",
                style=C["muted"],
                justify="center",
            ),
            border_style="bright_cyan",
            box=box.DOUBLE_EDGE,
            padding=(0, 2),
        )
    )
    console.print()


def render_step(
    step_type: str,
    content: str,
    iteration: int,
    tool_name: str | None = None,
    tool_input: str | None = None,
) -> None:
    color = C.get(step_type, "white")

    # Observation: compact one-line status instead of full panel
    if step_type == "observation":
        _render_observation_compact(content, iteration, tool_name)
        return

    header = Text()
    header.append(
        f"{ICONS.get(step_type, '')} {step_type.upper()}", style=f"bold {color}"
    )
    header.append(f"  iter {iteration}", style=C["muted"])

    if step_type == "action" and tool_name:
        body = Text()
        body.append(tool_name, style=f"bold {color}")
        body.append("\n")
        body.append(tool_input or "", style="bright_green")
    else:
        body = Text(content, style="white")

    console.print(
        Panel(
            body,
            title=header,
            title_align="left",
            border_style=color,
            box=box.ROUNDED,
            padding=(0, 1),
        )
    )


def _render_observation_compact(
    content: str, iteration: int, tool_name: str | None = None
) -> None:
    """Render observation as a compact one-line status."""
    first_line = content.split("\n", 1)[0].strip()
    # Extract status from "STATUS: success" / "STATUS: error" format
    if first_line.startswith("STATUS:"):
        status = first_line.split(":", 1)[1].strip().lower()
    else:
        status = "done"

    if status == "success":
        icon, style = "✓", "green"
    elif status == "error":
        icon, style = "✗", "red"
    else:
        icon, style = "●", C["muted"]

    tool_label = f" {tool_name}" if tool_name else ""
    # For errors, append the error message for quick diagnosis
    detail = ""
    if status == "error":
        for line in content.split("\n"):
            if line.startswith("ERROR:"):
                detail = f"  {line}"
                break

    console.print(
        f"  [{style}]{icon}[/] [{C['muted']}]OBS iter {iteration}[/]"
        f"[bold {style}]{tool_label}[/]"
        f"  [{style}]{status}{detail}[/]",
        highlight=False,
    )


def render_raw(text: str, iteration: int, verbose: bool) -> None:
    if not verbose:
        console.print(
            f"  [{C['muted']}]{ICONS['raw']} RAW LLM RESPONSE  iter {iteration}  "
            f"[dim](use --verbose to view)[/dim][/]"
        )
        return
    console.print(
        Panel(
            Text(text, style=C["raw"]),
            title=Text(
                f"{ICONS['raw']} RAW LLM RESPONSE  iter {iteration}", style=C["raw"]
            ),
            title_align="left",
            border_style=C["raw"],
            box=box.ROUNDED,
            padding=(0, 1),
        )
    )


def render_iter_sep(iteration: int) -> None:
    console.print(Rule(f"[{C['muted']}]ITERATION {iteration}[/]", style=C["muted"]))


def render_status(state: str, message: str, iteration: int = 0) -> None:
    dot = {"running": "bright_cyan", "done": "green", "error": "red"}.get(
        state, "grey50"
    )
    it = f"  [bright_cyan]ITER {iteration}[/]" if iteration else ""
    console.print(f"[{dot}]●[/] {message}{it}", highlight=False)


# ── Model info rendering ─────────────────────────
YES_MARK = "✓"
NO_MARK = "✗"


def render_model_detected(
    model: str, capabilities, provider: str, saved_path: str
) -> None:
    """Display detailed model info when newly detected at runtime."""
    yes, no = YES_MARK, NO_MARK
    thinking_info = (
        f"{yes} (budget: {capabilities.thinking_budget:,}, format: {capabilities.thinking_format})"
        if capabilities.supports_thinking
        else no
    )

    body = Text()
    body.append(f"  {model}", style="bold bright_cyan")
    body.append(f" ({provider})\n\n", style=C["muted"])
    body.append(f"  Context Window:    {capabilities.context_window:,} tokens\n")
    body.append(f"  Max Output:        {capabilities.max_output_tokens:,} tokens\n")
    body.append(
        f"  Structured Output: {yes if capabilities.supports_structured_output else no}\n"
    )
    body.append(
        f"  Tool Calling:      {yes if capabilities.supports_tool_calling else no}\n"
    )
    body.append(f"  Thinking:          {thinking_info}\n\n")
    body.append(f"  Saved to {saved_path}", style=C["muted"])

    console.print(
        Panel(
            body,
            title=Text("Model Detected", style="bold bright_cyan"),
            title_align="left",
            border_style="bright_cyan",
            box=box.ROUNDED,
            padding=(0, 1),
        )
    )


def render_model_loaded(model: str, capabilities) -> None:
    """Display one-line model summary when loading from registry."""
    yes, no = YES_MARK, NO_MARK
    thinking = f"thinking={yes}" if capabilities.supports_thinking else f"thinking={no}"
    console.print(
        f"[{C['accent']}]●[/] Model: {model} "
        f"(ctx={capabilities.context_window:,}, {thinking})",
        highlight=False,
    )


# ── Plan rendering ──────────────────────────────

_STATUS_ICONS = {
    "pending": "[ ]",
    "in_progress": "[→]",
    "done": "[✓]",
    "failed": "[✗]",
    "skipped": "[~]",
}

_STATUS_COLORS = {
    "pending": "white",
    "in_progress": "bright_cyan",
    "done": "green",
    "failed": "red",
    "skipped": "grey50",
}


def render_plan(plan) -> None:
    """Display plan with status indicators."""

    t = Text(justify="center")
    t.append(f"PLAN · {len(plan.steps)} steps", style="bold bright_cyan")

    console.print(
        Panel(
            t,
            border_style="bright_cyan",
            box=box.DOUBLE_EDGE,
            padding=(0, 2),
        )
    )
    console.print()

    for step in plan.steps:
        icon = _STATUS_ICONS.get(step.status, "[ ]")
        color = _STATUS_COLORS.get(step.status, "white")
        console.print(f"  {step.id}. [{color}]{icon}[/] {step.description}")

    console.print()


def render_context_dump(messages: list[dict], iteration: int) -> None:
    """Dump full context window contents (verbose mode, before each LLM call)."""
    console.print(
        Rule(
            f"[{C['muted']}]context dump (iter {iteration}, {len(messages)} msgs)[/]",
            style=C["muted"],
        )
    )
    for i, m in enumerate(messages):
        role = m.get("role", "?")
        content = m.get("content", "")
        # Summarize content: show first 200 chars
        if isinstance(content, str):
            preview = content[:200].replace("\n", "\\n")
            if len(content) > 200:
                preview += f"... ({len(content)} chars)"
        else:
            preview = str(content)[:200]
        console.print(f"  [{C['muted']}][{i}] {role}: {preview}[/]")
    console.print(Rule(style=C["muted"]))


def render_plan_progress(plan) -> None:
    """Display plan progress during execution."""
    for step in plan.steps:
        icon = _STATUS_ICONS.get(step.status, "[ ]")
        color = _STATUS_COLORS.get(step.status, "white")
        line = f"  {step.id}. [{color}]{icon}[/] {step.description}"
        console.print(line)
        if step.result and step.status in ("done", "failed"):
            summary = step.result[:80].replace("\n", " ")
            console.print(f"      [{C['muted']}]└─ {summary}[/]")
