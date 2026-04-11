"""Pluggable rendering system.

Exposes the same API as the old render.py module for backward compatibility.
All rendering goes through the active Renderer instance.
"""

from __future__ import annotations

from rich.console import Console

from agent_cli.render.base import Renderer
from agent_cli.render.minimal import MinimalRenderer
from agent_cli.render.fancy import FancyRenderer as FancyRenderer

# ── Global state ──────────────────────────────────
console = Console()

# Active renderer — swap this to change output style
_renderer: Renderer = MinimalRenderer(console)

# Color/icon constants (kept for backward compat with direct imports)
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

YES_MARK = "✓"
NO_MARK = "✗"


def set_renderer(renderer: Renderer) -> None:
    """Swap the active renderer."""
    global _renderer
    _renderer = renderer


def get_renderer() -> Renderer:
    """Get the active renderer."""
    return _renderer


# ── Delegating functions (backward-compatible API) ──


def render_header(
    provider: str,
    model: str,
    max_turns: int,
    skill_name: str = "",
    skill_args: str = "",
) -> None:
    _renderer.header(
        provider, model, max_turns, skill_name=skill_name, skill_args=skill_args
    )


def render_step(
    step_type: str,
    content: str,
    turn: int,
    tool_name: str | None = None,
    tool_input: str | None = None,
) -> None:
    try:
        if step_type == "thought":
            _renderer.thought(content, turn)
        elif step_type == "action":
            _renderer.action(tool_name or "", tool_input or "", turn)
        elif step_type == "observation":
            _renderer.observation(content, turn, tool_name)
        elif step_type == "final":
            _renderer.final(content, turn)
        elif step_type == "error":
            _renderer.error(content, turn)
        else:
            _renderer.status("info", content, turn)
    except Exception:
        # Fallback: print without markup to avoid Rich parsing crashes
        try:
            console.print(
                f"  [{step_type}] {tool_name or ''}: {content[:200]}",
                highlight=False,
                markup=False,
            )
        except Exception:
            import sys

            print(f"  [{step_type}] (render failed)", file=sys.stderr)


def render_raw(text: str, turn: int, verbose: bool) -> None:
    _renderer.raw(text, turn, verbose)


def render_turn_sep(turn: int) -> None:
    _renderer.turn_sep(turn)


def render_status(state: str, message: str, turn: int = 0) -> None:
    _renderer.status(state, message, turn)


def render_model_detected(
    model: str, capabilities, provider: str, saved_path: str
) -> None:
    _renderer.model_detected(model, capabilities, provider, saved_path)


def render_model_loaded(model: str, capabilities) -> None:
    _renderer.model_loaded(model, capabilities)


def render_context_dump(messages: list[dict], turn: int) -> None:
    _renderer.context_dump(messages, turn)


def render_spinner_start(message: str = "thinking...") -> None:
    _renderer.spinner_start(message)


def render_spinner_stop() -> None:
    _renderer.spinner_stop()


def render_stream_chunk(text: str) -> None:
    _renderer.stream_chunk(text)


def render_stream_end() -> None:
    _renderer.stream_end()


def render_dispatch_progress(
    label: str,
    turn: int,
    tool_name: str,
    detail: str = "",
    thought: str = "",
) -> None:
    _renderer.dispatch_progress(label, turn, tool_name, detail, thought)


def load_renderer_by_name(name: str) -> None:
    """Load and activate a renderer by filename (without .py).

    Looks for agent_cli/render/{name}.py with a class that subclasses Renderer.
    Example: load_renderer_by_name("fancy") → loads FancyRenderer from fancy.py
    """
    import importlib

    try:
        module = importlib.import_module(f"agent_cli.render.{name}")
    except ImportError:
        raise ValueError(
            f"Renderer '{name}' not found. "
            f"Expected module at agent_cli/render/{name}.py"
        )

    renderer_cls = None
    for attr_name in dir(module):
        attr = getattr(module, attr_name)
        if (
            isinstance(attr, type)
            and issubclass(attr, Renderer)
            and attr is not Renderer
        ):
            renderer_cls = attr
            break

    if renderer_cls is None:
        raise ValueError(f"No Renderer subclass found in agent_cli/render/{name}.py")

    set_renderer(renderer_cls(console))
