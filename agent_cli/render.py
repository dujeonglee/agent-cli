"""Terminal rendering helpers — minimal indent style, no boxes."""

from __future__ import annotations

from rich.console import Console

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
    "observation": "✓",
    "final": "✅",
    "error": "✗",
    "raw": "📄",
}


def render_header(
    provider: str,
    model: str,
    max_iter: int,
    skill_name: str = "",
    skill_args: str = "",
) -> None:
    """Render session header — one line, no box."""
    console.print()
    if skill_name:
        args_label = f"({skill_args})" if skill_args else ""
        console.print(
            f"  ● skill:{skill_name}{args_label}  "
            f"[{C['muted']}]{provider} · {model}[/]",
            highlight=False,
        )
    else:
        iter_label = str(max_iter) if max_iter > 0 else "∞"
        console.print(
            f"  ● agent-cli  "
            f"[{C['muted']}]{provider} · {model} · max_iter={iter_label}[/]",
            highlight=False,
        )
    console.print()


def render_step(
    step_type: str,
    content: str,
    iteration: int,
    tool_name: str | None = None,
    tool_input: str | None = None,
) -> None:
    """Render a step with 2-space indent, icon prefix, no box."""
    icon = ICONS.get(step_type, "●")

    # Observation: compact one-line status
    if step_type == "observation":
        _render_observation_compact(content, iteration, tool_name)
        return

    # Action: tool name + input on one block
    if step_type == "action" and tool_name:
        console.print(f"  {icon} {tool_name} → {tool_input or ''}", highlight=False)
        return

    # Thought: icon + indented text
    if step_type == "thought":
        console.print()
        console.print(f"  {icon} ", end="", highlight=False)
        # Indent continuation lines
        lines = content.split("\n")
        console.print(lines[0], highlight=False)
        for line in lines[1:]:
            console.print(f"     {line}", highlight=False)
        console.print()
        return

    # Final: icon + text
    if step_type == "final":
        console.print()
        console.print(f"  {icon} ", end="", highlight=False)
        lines = content.split("\n")
        console.print(lines[0], highlight=False)
        for line in lines[1:]:
            console.print(f"     {line}", highlight=False)
        console.print()
        return

    # Fallback
    console.print(f"  {icon} {content}", highlight=False)


def _render_observation_compact(
    content: str, iteration: int, tool_name: str | None = None
) -> None:
    """Render observation as a compact one-line status."""
    first_line = content.split("\n", 1)[0].strip()
    if first_line.startswith("STATUS:"):
        status = first_line.split(":", 1)[1].strip().lower()
    else:
        status = "done"

    if status == "success":
        icon = "✓"
    elif status == "error":
        icon = "✗"
    else:
        icon = "●"

    tool_label = f" {tool_name}" if tool_name else ""
    detail = ""
    if status == "error":
        for line in content.split("\n"):
            if line.startswith("ERROR:"):
                detail = f"  {line}"
                break

    console.print(
        f"  {icon}{tool_label}  {status}{detail}",
        highlight=False,
    )


def render_raw(text: str, iteration: int, verbose: bool) -> None:
    """Render raw LLM response — verbose shows full, otherwise one-line note."""
    if not verbose:
        console.print(
            f"  [{C['muted']}]📄 raw response iter {iteration} (use --verbose to view)[/]"
        )
        return
    console.print(f"\n  [{C['muted']}]── raw response iter {iteration} ──[/]")
    for line in text.split("\n"):
        console.print(f"  [{C['raw']}]{line}[/]")
    console.print(f"  [{C['muted']}]── end raw ──[/]\n")


def render_iter_sep(iteration: int) -> None:
    """Render iteration separator — simple arrow + number."""
    console.print(f"\n[{C['muted']}]→ iter {iteration}[/]\n")


def render_status(state: str, message: str, iteration: int = 0) -> None:
    """Render status message (running/done/error)."""
    it = f"  iter {iteration}" if iteration else ""
    console.print(f"  ● {message}{it}", highlight=False)


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

    console.print()
    console.print("  ● Model Detected", highlight=False)
    console.print(f"    {model} ({provider})", highlight=False)
    console.print(
        f"    context={capabilities.context_window:,}  "
        f"output={capabilities.max_output_tokens:,}  "
        f"structured={'yes' if capabilities.supports_structured_output else 'no'}  "
        f"tools={'yes' if capabilities.supports_tool_calling else 'no'}  "
        f"thinking={thinking_info}",
        highlight=False,
    )
    console.print(f"    [{C['muted']}]saved to {saved_path}[/]")
    console.print()


def render_model_loaded(model: str, capabilities) -> None:
    """Display one-line model summary when loading from registry."""
    yes, no = YES_MARK, NO_MARK
    thinking = f"thinking={yes}" if capabilities.supports_thinking else f"thinking={no}"
    console.print(
        f"  ● {model} (ctx={capabilities.context_window:,}, {thinking})",
        highlight=False,
    )


def render_context_dump(messages: list[dict], iteration: int) -> None:
    """Dump full context window contents (verbose mode)."""
    console.print(
        f"\n  [{C['muted']}]── context dump (iter {iteration}, {len(messages)} msgs) ──[/]"
    )
    for i, m in enumerate(messages):
        role = m.get("role", "?")
        content = m.get("content", "")
        if isinstance(content, str):
            preview = content[:200].replace("\n", "\\n")
            if len(content) > 200:
                preview += f"... ({len(content)} chars)"
        else:
            preview = str(content)[:200]
        console.print(f"  [{C['muted']}][{i}] {role}: {preview}[/]")
    console.print(f"  [{C['muted']}]── end dump ──[/]\n")
