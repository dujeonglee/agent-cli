"""Named DIRECTIVE presets — a library shared across all agent-cli instances.

The Directives editor (web Prompt Inspector) can save the current directive
under a name and reload it later from any room. Presets are plain Markdown
files in ``~/.agent-cli/directive-presets/<name>.md``; the store is stateless
(every op hits the filesystem) so instances stay in sync with no cache.

This is deliberately separate from the always-on ``~/.agent-cli/DIRECTIVE.md``:
that file is applied every session, whereas presets are a pick-and-load library
of directives you switch between per task (e.g. "커널 드라이버", "라이브러리 작성").

Names double as filesystem ids, so they are validated to block path traversal
(no ``/`` ``\\``, no ``.``/``..``/dotfiles) while still allowing Unicode letters
and spaces — Korean preset names round-trip unchanged.
"""

from __future__ import annotations

from pathlib import Path

_PRESETS_SUBDIR = "directive-presets"


def _presets_dir() -> Path:
    """Root of the preset library. Broken out so tests can point it at a tmp
    dir instead of the real home (never write to the user's ~ in a test)."""
    return Path.home() / ".agent-cli" / _PRESETS_SUBDIR


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


def save(name: str, content: str) -> str:
    """Write a preset (overwriting any same-name one); return its id."""
    n = _safe_name(name)
    root = _presets_dir()
    root.mkdir(parents=True, exist_ok=True)
    (root / f"{n}.md").write_text(content, encoding="utf-8")
    return n


def load(preset_id: str) -> str | None:
    """Return a preset's body by id, or ``None`` if it doesn't exist.

    ``preset_id`` is re-validated (not just trusted from the URL) so a crafted
    ``../…`` id raises instead of reading outside the presets dir."""
    n = _safe_name(preset_id)
    f = _presets_dir() / f"{n}.md"
    return f.read_text(encoding="utf-8") if f.is_file() else None


def list_presets() -> list[dict]:
    """All user-saved presets as ``[{id, label, source}]``, id-sorted."""
    root = _presets_dir()
    if not root.is_dir():
        return []
    return [
        {"id": f.stem, "label": f.stem, "source": "user"}
        for f in sorted(root.glob("*.md"))
    ]


def delete(preset_id: str) -> bool:
    """Remove a preset; return ``True`` if it existed, ``False`` otherwise."""
    n = _safe_name(preset_id)
    f = _presets_dir() / f"{n}.md"
    if not f.is_file():
        return False
    f.unlink()
    return True
