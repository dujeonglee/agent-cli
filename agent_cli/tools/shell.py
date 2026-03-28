"""Shell command execution tool."""

from __future__ import annotations

import subprocess

from agent_cli.tools.result import ToolResult


def tool_shell(args: dict) -> ToolResult:
    """Run a shell command and return stdout/stderr."""

    cmd = args.get("command", "")
    if not cmd or not cmd.strip():
        return ToolResult(
            False, error="Empty command. Provide a shell command to execute."
        )
    timeout = int(args.get("timeout", 30))
    try:
        result = subprocess.run(
            cmd,
            shell=True,
            capture_output=True,
            text=True,
            timeout=timeout,
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
        return ToolResult(True, output="\n".join(parts) if parts else "(no output)")
    except subprocess.TimeoutExpired:
        return ToolResult(False, error=f"Command timed out ({timeout}s)")
    except Exception as e:
        return ToolResult(False, error=f"shell failed: {e}")
