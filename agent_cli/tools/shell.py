"""Shell command execution tool."""
from __future__ import annotations

import subprocess


def tool_shell(args: dict) -> str:
    """Run a shell command and return stdout/stderr."""
    cmd = args.get("command", "")
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
        return "\n".join(parts) if parts else "(no output)"
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"Command timed out ({timeout}s)")
    except Exception as e:
        raise RuntimeError(f"shell failed: {e}")
