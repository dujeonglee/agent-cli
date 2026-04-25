"""Shell command execution tool."""

from __future__ import annotations

import os
import shlex
import subprocess
import sys

from agent_cli.tools.result import ToolResult


# Commands that destroy or move files irreversibly. Detection works on
# shell-tokenized form so we catch real invocations (`rm -rf x`,
# `xargs rm`, `find -exec rm {}`, `cd /tmp && rm x`) without flagging
# similarly-spelled non-commands (`rm-helper.sh`, `format-firmware`,
# string literals like `echo "rm files"`). Intentionally narrow — once
# users start mashing 'y' through warnings the protection is gone.
_DANGEROUS_KEYWORDS = frozenset(("rm", "rmdir", "mv"))

# Per-process "always allow" set: keywords the user has greenlit for the
# rest of this CLI session. Cleared when the process exits.
_session_allowlist: set[str] = set()


def _confirmation_enabled() -> bool:
    """Default-on. Set AGENT_CLI_DANGEROUS_SHELL_CONFIRM=0 to disable —
    useful for batch/CI runs where there is no human to answer y/n."""
    return os.environ.get("AGENT_CLI_DANGEROUS_SHELL_CONFIRM", "1") != "0"


def _detect_dangerous(cmd: str) -> str | None:
    """Return the first dangerous keyword found in `cmd`, or None.

    Tokenize with shlex (POSIX rules). The keyword must appear as its
    own token to match — operator tokens (`|` `&&` `;`) are tokens
    themselves so a piped/sequenced `rm` is caught, but quoted string
    literals (`echo "rm files"`) collapse into one token and don't
    match. Edge cases not caught: shell-execution wrappers like
    `bash -c "rm foo"` (the inner string is opaque to shlex) and
    `$(rm x)` substitution. Adding coverage if those start showing up
    is fine — keep the matcher narrow until the data demands more.
    """
    try:
        tokens = shlex.split(cmd, posix=True)
    except ValueError:
        # Unbalanced quotes — fall back to whitespace split. Better to
        # over-trigger than miss.
        tokens = cmd.split()
    for tok in tokens:
        if tok in _DANGEROUS_KEYWORDS:
            return tok
    return None


def _ask_confirmation(cmd: str, keyword: str) -> str:
    """Prompt user for y / n / a. Returns the lowered single-char
    decision. Treats EOF/Ctrl+C as "n" (deny) — the safe default when
    the user can't actually answer."""
    prompt = (
        f"\n⚠ Dangerous command detected:\n"
        f"  $ {cmd}\n"
        f"Allow? (y=once, n=deny, a=always allow `{keyword}` this session) [y/n/a]: "
    )
    try:
        answer = input(prompt).strip().lower()
    except (EOFError, KeyboardInterrupt):
        print("", file=sys.stderr)
        return "n"
    if answer in ("y", "yes"):
        return "y"
    if answer in ("a", "always"):
        return "a"
    return "n"


def _is_tty() -> bool:
    """No human attached → can't prompt. We treat this as a deny rather
    than silently bypassing the check."""
    try:
        return sys.stdin.isatty() and sys.stdout.isatty()
    except Exception:
        return False


def tool_shell(args: dict) -> ToolResult:
    """Run a shell command and return stdout/stderr."""

    cmd = args.get("command", "")
    if not cmd or not cmd.strip():
        return ToolResult(
            False, error="Empty command. Provide a shell command to execute."
        )

    if _confirmation_enabled():
        keyword = _detect_dangerous(cmd)
        if keyword and keyword not in _session_allowlist:
            if not _is_tty():
                return ToolResult(
                    False,
                    error=(
                        f"Refused: command contains `{keyword}` and no TTY is "
                        "available to confirm. Set "
                        "AGENT_CLI_DANGEROUS_SHELL_CONFIRM=0 to bypass for "
                        "non-interactive runs."
                    ),
                )
            decision = _ask_confirmation(cmd, keyword)
            if decision == "n":
                return ToolResult(
                    False,
                    error=f"User denied command containing `{keyword}`: {cmd}",
                )
            if decision == "a":
                _session_allowlist.add(keyword)

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
