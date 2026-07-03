"""Workspace path confinement (default-on).

Gate the filesystem-MUTATING tools (write_file / edit_file) and shell to the
launch workspace: an op whose path resolves OUTSIDE the workspace root asks the
user for confirmation before proceeding. Reuses the same confirm / allowlist
infrastructure the dangerous-command guard uses (``renderer.confirm``,
``can_prompt``, ``interactive_lock``).

Threat model: accident prevention + a speed bump against an agent wandering out
of its workspace. This is NOT a sandbox — shell path extraction is best-effort
(it cannot see paths inside ``$(...)``, ``python -c "..."``, or shell variables);
true isolation requires an OS sandbox. ``read_file`` is intentionally NOT gated:
driver / kernel work reads toolchains and headers outside the workspace by the
dozen, so gating reads would be a prompt storm that just trains allow-all
fatigue. Only mutations and shell (which can write / exfiltrate) are gated.

Root = the process cwd at call time, or ``AGENT_CLI_WORKSPACE_ROOT`` if set.
agent-board spawns each instance with cwd = its post workspace, so the default
lands exactly right. Disable entirely with ``AGENT_CLI_WORKSPACE_CONFINE=0``.
"""

from __future__ import annotations

import os
import re
import shlex
from pathlib import Path

_ENABLE_ENV = "AGENT_CLI_WORKSPACE_CONFINE"
_ROOT_OVERRIDE_ENV = "AGENT_CLI_WORKSPACE_ROOT"

# Session "always allow" roots: resolved directory subtrees the user has
# greenlit for the rest of this process (the path form of shell.py's
# keyword-based ``_session_allowlist``). A path passes the gate if it resolves
# under any allowlisted root. Cleared when the process exits.
_session_root_allowlist: set[str] = set()


def enabled() -> bool:
    """Default-on. Set ``AGENT_CLI_WORKSPACE_CONFINE=0`` to disable — for batch /
    CI runs with no human to answer, or deployments that confine another way."""
    return os.environ.get(_ENABLE_ENV, "1") != "0"


def workspace_root() -> Path:
    """The confinement root: ``AGENT_CLI_WORKSPACE_ROOT`` if set, else the
    process cwd. Resolved (``..`` collapsed, symlinks followed) so containment
    checks compare canonical paths."""
    root = os.environ.get(_ROOT_OVERRIDE_ENV) or os.getcwd()
    return Path(root).resolve()


def resolve_within(path: str, *, root: Path | None = None) -> tuple[Path, bool]:
    """Resolve ``path`` (relative → against ``root``) and report whether it lands
    inside the workspace root. ``resolve()`` collapses ``..`` and follows
    symlinks, so both traversal and symlink escapes are caught. A nonexistent
    target (writing a new file) still resolves — its would-be location is what
    gets checked."""
    r = root or workspace_root()
    p = Path(path)
    resolved = (p if p.is_absolute() else r / p).resolve()
    inside = resolved == r or r in resolved.parents
    return resolved, inside


def _allowlisted(resolved: Path) -> bool:
    for root in _session_root_allowlist:
        rp = Path(root)
        if resolved == rp or rp in resolved.parents:
            return True
    return False


# A shell token that names a path we can meaningfully resolve against the
# workspace: an absolute path (``/…`` or ``~/…``) or an explicit ``..`` escape.
# Bare relative names (``foo.txt``, ``src/main.c``) resolve INSIDE the workspace
# so they never gate; we only pull out tokens that could point outside.
def _path_candidate(tok: str) -> str | None:
    t = tok.strip("'\"").lstrip("<>")
    # --flag=/path  →  /path
    if t.startswith("--") and "=" in t:
        t = t.split("=", 1)[1]
    # short flag with attached value: -I/usr/include, -C/usr/src  →  the path.
    # A bare short flag (-rf, -C) has no path once stripped → skip.
    elif len(t) >= 2 and t[0] == "-" and t[1].isalpha():
        t = re.sub(r"^-[A-Za-z]+", "", t)
        if not t:
            return None
    if not t:
        return None
    if t.startswith("/") or t.startswith("~/") or t == "~":
        return t
    if t == ".." or t.startswith("../") or "/../" in t:
        return t
    return None


def extract_shell_paths(cmd: str) -> list[str]:
    """Best-effort: pull absolute paths and ``../`` escapes out of a shell
    command so :func:`guard` can check them. Deliberately narrow — it sees only
    literal path tokens, NOT paths inside ``$(...)``, ``python -c "..."``,
    variables, or globs. Those are a documented blind spot (they need an OS
    sandbox, not a string matcher); the gate is a speed bump, not a jail."""
    try:
        tokens = shlex.split(cmd, posix=True)
    except ValueError:
        # Unbalanced quotes — fall back to whitespace split. Better to
        # over-extract (an extra prompt) than to miss a path silently.
        tokens = cmd.split()
    found: list[str] = []
    for tok in tokens:
        cand = _path_candidate(tok)
        if cand is not None:
            found.append(cand)
    return found


def guard(paths, action: str) -> str | None:
    """Gate ``paths`` for tool ``action`` (e.g. ``"write_file"``). Returns
    ``None`` when the gate is disabled, or every path is inside the workspace /
    already allowlisted — the zero-overhead common case (no renderer import, no
    lock). Otherwise prompts ONCE for the outside paths and returns ``None`` on
    allow, or an error string on deny / when no prompt can be shown.

    ``a=always`` adds each outside path's directory subtree to the session
    allowlist so later ops there don't re-prompt (mirrors the dangerous-command
    guard's ``a``, keyed on resolved paths instead of a keyword)."""
    if not enabled():
        return None
    root = workspace_root()
    outside: list[Path] = []
    for path in paths:
        if not path:
            continue
        resolved, inside = resolve_within(path, root=root)
        if inside or _allowlisted(resolved):
            continue
        if resolved not in outside:
            outside.append(resolved)
    if not outside:
        return None

    from agent_cli.render import get_renderer
    from agent_cli.render.base import ConfirmOption, interactive_lock

    # Hold the shared interactive lock across the re-check + prompt: it
    # serializes against confirm/ask everywhere, and ``renderer.confirm``
    # re-acquires it internally on this same thread (re-entrant).
    with interactive_lock:
        # Re-check under the lock — another worker may have allowlisted these
        # while we waited.
        outside = [p for p in outside if not _allowlisted(p)]
        if not outside:
            return None
        renderer = get_renderer()
        joined = ", ".join(str(p) for p in outside)
        if not renderer.can_prompt():
            return (
                f"Refused: {action} touches path(s) outside the workspace "
                f"({joined}) and this interface can't prompt for confirmation "
                f"right now (non-interactive shell, or no connected client). "
                f"Set AGENT_CLI_WORKSPACE_CONFINE=0 to disable workspace "
                f"confinement for non-interactive runs."
            )
        listing = "\n".join(f"  {p}" for p in outside)
        prompt = (
            f"\n⚠ {action} touches path(s) OUTSIDE the workspace\n"
            f"  (workspace: {root}):\n{listing}\n"
            f"Allow? (y=once, n=deny, a=always allow these locations this "
            f"session)\n  [y/n/a, optional comment after]: "
        )
        options = [
            ConfirmOption(
                key="y",
                label="once (allow this op)",
                aliases=("yes", "ok", "okay", "yep", "yeah", "sure"),
            ),
            ConfirmOption(key="n", label="deny", aliases=("no", "nope")),
            ConfirmOption(
                key="a",
                label="always allow these locations this session",
                aliases=("always", "allow"),
            ),
        ]
        decision, comment = renderer.confirm(prompt, options, default_key="n")
        if decision == "n":
            err = f"User denied {action} outside workspace: {joined}"
            if comment:
                err += f". User said: {comment}"
            return err
        if decision == "a":
            for p in outside:
                # Allowlist the directory subtree: a file → its parent dir, a
                # directory → itself. Sibling ops there then pass without a
                # re-prompt (the header-storm mitigation).
                d = p if p.is_dir() else p.parent
                _session_root_allowlist.add(str(d))
    return None
