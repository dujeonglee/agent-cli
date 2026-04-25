"""Shared unified-diff formatter for write_file / edit_file output.

Renders the change between old and new file content as a unified diff
with Rich markup so the renderer colors `+` lines green and `-` lines
red — same visual model as `git diff`. Each line is prefixed with the
corresponding `OLD NEW` line numbers parsed out of the hunk header
(`@@ -A,B +C,D @@`) so the user can locate the change in the file
without counting lines manually. Both the user reading the terminal
and the LLM (which gets the same observation string) can verify what
actually changed.

Truncated past `MAX_DIFF_LINES` to keep the observation from
ballooning when an edit replaces an entire large file. The truncation
preserves the head of the diff and tells the caller how many lines
were dropped — full content is still on disk if needed.
"""

from __future__ import annotations

import difflib
import re

# Cap the diff at ~100 visible lines. Beyond this the LLM rarely
# benefits from seeing more (it already authored the change) and the
# token cost grows. The user can still read the file directly if they
# want to verify the tail.
MAX_DIFF_LINES = 100

# Parses a unified-diff hunk header. We only care about the two
# starting line numbers; counts are ignored because we track our own
# offset as we walk the hunk.
_HUNK_RE = re.compile(r"@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@")


def format_diff(old: str, new: str, path: str) -> str:
    """Return a Rich-marked unified diff between `old` and `new`.

    Empty if the two are identical (caller can branch on truthiness).
    Multi-line truncation appends a single summary line indicating how
    many lines were elided.
    """
    if old == new:
        return ""

    old_lines = old.splitlines(keepends=True) if old else []
    new_lines = new.splitlines(keepends=True) if new else []

    raw = list(
        difflib.unified_diff(
            old_lines,
            new_lines,
            fromfile=f"a/{path}",
            tofile=f"b/{path}",
            n=2,  # 2 lines of context — enough to orient, not enough to bloat
        )
    )

    if not raw:
        return ""

    rendered: list[str] = []
    old_ln = 0
    new_ln = 0
    for line in raw:
        # `unified_diff` keeps the trailing newline of the source line.
        # Strip it for clean Rich rendering (we add `\n` between).
        line = line.rstrip("\n")
        if line.startswith("+++") or line.startswith("---"):
            rendered.append(f"[bold]{_escape(line)}[/bold]")
        elif line.startswith("@@"):
            m = _HUNK_RE.match(line)
            if m:
                old_ln = int(m.group(1))
                new_ln = int(m.group(2))
            rendered.append(f"[cyan]{_escape(line)}[/cyan]")
        elif line.startswith("+"):
            rendered.append(
                f"[dim]{_lnum(None, new_ln)}[/dim][green]{_escape(line)}[/green]"
            )
            new_ln += 1
        elif line.startswith("-"):
            rendered.append(
                f"[dim]{_lnum(old_ln, None)}[/dim][red]{_escape(line)}[/red]"
            )
            old_ln += 1
        else:
            rendered.append(f"[dim]{_lnum(old_ln, new_ln)}[/dim]{_escape(line)}")
            old_ln += 1
            new_ln += 1

    if len(rendered) > MAX_DIFF_LINES:
        elided = len(rendered) - MAX_DIFF_LINES
        rendered = rendered[:MAX_DIFF_LINES]
        rendered.append(f"[dim]… diff truncated, {elided} more line(s) omitted[/dim]")

    return "\n".join(rendered)


def _lnum(old: int | None, new: int | None) -> str:
    """Render the `OLD NEW` line-number gutter.

    `None` columns become blanks so removed lines have no NEW number
    and added lines have no OLD number — the same convention as the
    GitHub side-by-side diff view, but inline."""
    o = f"{old:>4}" if old is not None else "    "
    n = f"{new:>4}" if new is not None else "    "
    return f"{o} {n}  "


def _escape(text: str) -> str:
    """Escape Rich markup metacharacters in user content so a stray
    `[bold]` in source code doesn't get interpreted as styling."""
    return text.replace("[", "\\[")
