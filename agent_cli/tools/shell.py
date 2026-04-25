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


def _ask_confirmation(cmd: str, keyword: str) -> tuple[str, str]:
    """Prompt user for y / n / a, optionally followed by a comment.

    Returns ``(decision, comment)``. ``decision`` is one of
    ``"y"`` / ``"n"`` / ``"a"``. ``comment`` is whatever the user
    typed after the decision token (may be empty).

    Format: ``y do this next``, ``n wrong path``, ``a only in /tmp``.
    Pure ``y`` / ``yes`` / ``n`` / ``a`` works as before with empty
    comment. EOF/Ctrl+C → ``("n", "")`` (safe default — never run a
    dangerous command on input failure). If the first token isn't
    y/n/a we treat it as ``n`` and keep the full input as the comment
    so the user's reasoning still surfaces to the LLM.
    """
    prompt = (
        f"\n⚠ Dangerous command detected:\n"
        f"  $ {cmd}\n"
        f"Allow? (y=once, n=deny, a=always allow `{keyword}` this session)\n"
        f"  [y/n/a, optional comment after]: "
    )
    try:
        raw = input(prompt).strip()
    except (EOFError, KeyboardInterrupt):
        print("", file=sys.stderr)
        return ("n", "")
    if not raw:
        return ("n", "")

    parts = raw.split(maxsplit=1)
    first = parts[0].lower()
    comment = parts[1].strip() if len(parts) > 1 else ""

    if first in ("y", "yes"):
        return ("y", comment)
    if first in ("a", "always"):
        return ("a", comment)
    if first in ("n", "no"):
        return ("n", comment)
    # Unrecognized first token — treat as deny but preserve the full
    # input as the comment so the user's intent isn't dropped.
    return ("n", raw)


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

    user_comment = ""
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
            decision, user_comment = _ask_confirmation(cmd, keyword)
            if decision == "n":
                err = f"User denied command containing `{keyword}`: {cmd}"
                if user_comment:
                    err += f". User said: {user_comment}"
                return ToolResult(False, error=err)
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
        # When the user approved with a comment, surface it after the
        # command output so the LLM sees the additional instruction
        # alongside the result.
        if user_comment:
            parts.append(f"[User note when approving: {user_comment}]")
        return ToolResult(True, output="\n".join(parts) if parts else "(no output)")
    except subprocess.TimeoutExpired:
        return ToolResult(False, error=f"Command timed out ({timeout}s)")
    except Exception as e:
        return ToolResult(False, error=f"shell failed: {e}")
