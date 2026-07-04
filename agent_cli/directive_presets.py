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

A handful of **built-in** presets ship inside the package
(``directive_presets_builtin/<axis>/<name>.md``) so every install starts with a
few good persona/task fragments. They are read-only: ``list_presets`` merges
them in as ``source:"builtin"``, ``load`` falls back to them, but ``save``/
``delete`` only ever touch the user's home store — a same-name user preset
simply shadows the built-in.
"""

from __future__ import annotations

from pathlib import Path

_PRESETS_SUBDIR = "directive-presets"

# Built-in presets bundled in the wheel (see pyproject ``package-data``). Read
# from here as a fallback so a fresh install has presets with no home dir.
_BUILTIN_ROOT = Path(__file__).resolve().parent / "directive_presets_builtin"

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


def _builtin_axis_dir(axis: str) -> Path:
    return _BUILTIN_ROOT / _safe_axis(axis)


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
    ``../…`` id raises instead of reading outside the presets dir. A user preset
    wins over a built-in of the same id (the user file shadows it)."""
    name = _safe_name(preset_id)
    f = _axis_dir(axis) / f"{name}.md"
    if f.is_file():
        return f.read_text(encoding="utf-8")
    bf = _builtin_axis_dir(axis) / f"{name}.md"
    return bf.read_text(encoding="utf-8") if bf.is_file() else None


def list_presets(axis: str) -> list[dict]:
    """Built-in + user presets for ``axis`` as ``[{id, label, source}]``, sorted.

    Built-ins (shipped in the package) come first as ``source:"builtin"``; a
    same-id user preset shadows the built-in and is marked ``source:"user"``."""
    by_id: dict[str, dict] = {}
    bd = _builtin_axis_dir(axis)
    if bd.is_dir():
        for f in sorted(bd.glob("*.md")):
            by_id[f.stem] = {"id": f.stem, "label": f.stem, "source": "builtin"}
    ud = _axis_dir(axis)
    if ud.is_dir():
        for f in sorted(ud.glob("*.md")):
            by_id[f.stem] = {"id": f.stem, "label": f.stem, "source": "user"}
    return sorted(by_id.values(), key=lambda p: p["id"])


def delete(axis: str, preset_id: str) -> bool:
    """Remove a USER axis preset; return ``True`` if it existed, else ``False``.

    Only the home store is touched — built-ins are read-only, so deleting a
    built-in id returns ``False`` (nothing to remove)."""
    f = _axis_dir(axis) / f"{_safe_name(preset_id)}.md"
    if not f.is_file():
        return False
    f.unlink()
    return True
