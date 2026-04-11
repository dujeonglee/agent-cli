"""Hook loader — scan and load Python hook files from disk."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Callable

from agent_cli.hooks.events import ALL_EVENTS, EVENT_TO_FUNC


def _hook_dirs() -> list[Path]:
    """Return hook directories in execution order: project → user."""
    return [
        Path.cwd() / ".agent-cli" / "hooks",
        Path.home() / ".agent-cli" / "hooks",
    ]


def _scan_hook_files(dirs: list[Path] | None = None) -> list[Path]:
    """Scan directories for *.py hook files, sorted by filename.

    Files are sorted by name (numeric prefix → alpha), with project hooks
    before user hooks.
    """
    if dirs is None:
        dirs = _hook_dirs()

    files: list[Path] = []
    for d in dirs:
        if not d.is_dir():
            continue
        found = sorted(d.glob("*.py"), key=lambda p: p.name)
        files.extend(found)
    return files


def _load_module(path: Path) -> object | None:
    """Load a Python file as a module. Returns None on failure."""
    module_name = f"_agent_cli_hook_{path.stem}_{id(path)}"
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        return None
    try:
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)  # type: ignore[union-attr]
        return module
    except Exception:
        # Bad hook file — skip silently
        return None


def load_python_hooks(
    dirs: list[Path] | None = None,
) -> dict[str, list[Callable]]:
    """Load all Python hook files and return event→[callable] mapping.

    Each hook file declares ``EVENTS = ["EventName", ...]`` and defines
    functions named after the event's snake_case form (see EVENT_TO_FUNC).

    Returns:
        Mapping from event name to ordered list of callables.
    """
    result: dict[str, list[Callable]] = {ev: [] for ev in ALL_EVENTS}

    for path in _scan_hook_files(dirs):
        module = _load_module(path)
        if module is None:
            continue

        # Read EVENTS declaration
        declared = getattr(module, "EVENTS", None)
        if not isinstance(declared, (list, tuple)):
            continue

        for event_name in declared:
            if event_name not in ALL_EVENTS:
                continue
            func_name = EVENT_TO_FUNC.get(event_name)
            if func_name is None:
                continue
            func = getattr(module, func_name, None)
            if callable(func):
                result[event_name].append(func)

    return result
