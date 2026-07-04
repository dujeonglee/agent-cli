"""Named DIRECTIVE presets — per-axis libraries shared across all agent-cli
instances.

The Directives editor (web Prompt Inspector) can save each of a directive's
three axes — 성격(persona), 업무(task), 학습된 지침(learned guidance) — under a
name and reload it later from any room. Presets are plain Markdown files in
``~/.agent-cli/directive-presets/<axis>/<name>.md``; the store is stateless
(every op hits the filesystem) so instances stay in sync with no cache.

This is deliberately separate from the always-on ``~/.agent-cli/DIRECTIVE.md``:
that file is applied every session, whereas presets are a pick-and-load library
of per-axis fragments you switch between per task (e.g. a "커널 드라이버"
persona, a "라이브러리 작성" task, a saved 학습된 지침 set).

The ``axis`` is a fixed enum (validated), and names double as filesystem ids —
validated to block path traversal (no ``/`` ``\\``, no ``.``/``..``/dotfiles)
while still allowing Unicode letters and spaces, so Korean preset names
round-trip unchanged.
"""

from __future__ import annotations

from pathlib import Path

_PRESETS_SUBDIR = "directive-presets"

# The three directive axes, each its own preset sub-library. A fixed set (not
# user input) so an ``axis`` from a URL can never traverse outside the store.
AXES = ("persona", "task", "learned")


def _presets_root() -> Path:
    """Root of the preset library. Broken out so tests can point it at a tmp
    dir instead of the real home (never write to the user's ~ in a test)."""
    return Path.home() / ".agent-cli" / _PRESETS_SUBDIR


def _safe_axis(axis: str) -> str:
    """Validate ``axis`` against the fixed enum, or raise ``ValueError``."""
    if axis not in AXES:
        raise ValueError(f"unknown preset axis: {axis!r}")
    return axis


def _axis_dir(axis: str) -> Path:
    return _presets_root() / _safe_axis(axis)


def _safe_name(name: str) -> str:
    """Validate a preset name for use as a filename, or raise ``ValueError``.

    The name IS the id (URL path segment + filename stem), so it must not let a
    caller escape the presets dir. We reject path separators, ``.``/``..``, and
    leading-dot (hidden) names; everything else — Unicode letters, digits,
    spaces — is kept verbatim so display names survive the round trip.
    """
    n = (name or "").strip()
    if not n or "/" in n or "\\" in n or n.startswith(".") or n in (".", ".."):
        raise ValueError(f"invalid preset name: {name!r}")
    return n


def save(axis: str, name: str, content: str) -> str:
    """Write a preset for ``axis`` (overwriting any same-name one); return id."""
    d = _axis_dir(axis)
    n = _safe_name(name)
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{n}.md").write_text(content, encoding="utf-8")
    return n


def load(axis: str, preset_id: str) -> str | None:
    """Return an axis preset's body by id, or ``None`` if it doesn't exist.

    ``preset_id`` is re-validated (not just trusted from the URL) so a crafted
    ``../…`` id raises instead of reading outside the presets dir."""
    f = _axis_dir(axis) / f"{_safe_name(preset_id)}.md"
    return f.read_text(encoding="utf-8") if f.is_file() else None


def list_presets(axis: str) -> list[dict]:
    """All user-saved presets for ``axis`` as ``[{id, label, source}]``, sorted."""
    d = _axis_dir(axis)
    if not d.is_dir():
        return []
    return [
        {"id": f.stem, "label": f.stem, "source": "user"}
        for f in sorted(d.glob("*.md"))
    ]


def delete(axis: str, preset_id: str) -> bool:
    """Remove an axis preset; return ``True`` if it existed, else ``False``."""
    f = _axis_dir(axis) / f"{_safe_name(preset_id)}.md"
    if not f.is_file():
        return False
    f.unlink()
    return True
