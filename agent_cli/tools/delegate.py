"""Subagent delegation tool."""

from __future__ import annotations

import os
import re
import subprocess
import sys

_VAGUE_REFS = re.compile(
    r"\b(it|this|that|these|those|above|previous|earlier|the same)\b", re.I
)


def _validate_subtask(task: str) -> str | None:
    """Return an error string if task looks under-specified, else None."""
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


def _build_subprocess_cmd(args: list[str]) -> list[str]:
    """Build the subprocess command, auto-detecting wrapper vs package mode."""
    if getattr(sys, "frozen", False):
        return [sys.executable] + args
    elif os.path.basename(sys.argv[0]) == "agent-cli.py":
        return [sys.executable, sys.argv[0]] + args
    else:
        return [sys.executable, "-m", "agent_cli"] + args


def tool_delegate(
    args: dict,
    provider: str,
    model: str,
    base_url: str,
    api_key: str,
    timeout: int = 300,
) -> str:
    """Delegate a self-contained subtask to an independent subagent."""
    task_str = args.get("task", "")
    validation_err = _validate_subtask(task_str)
    if validation_err:
        raise RuntimeError(f"Delegation rejected: {validation_err}")

    cmd_args = [
        "run",
        task_str,
        "--provider",
        provider,
        "--model",
        model,
        "--base-url",
        base_url,
        "--quiet",
    ]
    if api_key:
        cmd_args.extend(["--api-key", api_key])

    cmd = _build_subprocess_cmd(cmd_args)

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        stdout = result.stdout.strip()
        stderr = result.stderr.strip()
        if result.returncode == 0 and stdout:
            return f"STATUS: success\nRESULT:\n{stdout}"
        else:
            err_msg = stderr or stdout or "(no output)"
            return f"STATUS: error\nERROR: Subagent failed\n{err_msg}"
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"Subagent timed out ({timeout}s)")
