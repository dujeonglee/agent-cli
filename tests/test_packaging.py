"""Packaging guard: non-.py resources must be declared in package-data.

The built-in agents (reviewer/explorer) and skills (create-*, plan) are ``.md``
files — setuptools won't ship them in the wheel unless they're listed under
``[tool.setuptools.package-data]``. They were silently dropped once (pip
installs had no reviewer → auto-review broke; editable installs hid it because
they read straight from the source tree). This test fails if any built-in
resource on disk is NOT covered by a package-data glob.
"""

from __future__ import annotations

import fnmatch
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # py3.10
    import tomli as tomllib

_PKG = Path(__file__).resolve().parent.parent / "agent_cli"
_PYPROJECT = Path(__file__).resolve().parent.parent / "pyproject.toml"


def _package_data_patterns() -> list[str]:
    data = tomllib.loads(_PYPROJECT.read_text(encoding="utf-8"))
    return data["tool"]["setuptools"]["package-data"]["agent_cli"]


def _covered(rel: str, patterns: list[str]) -> bool:
    # setuptools treats ``**`` as "any number of dirs"; fnmatch doesn't, so
    # normalise ``a/**/b`` to also match the zero-dir case ``a/b``.
    for pat in patterns:
        if fnmatch.fnmatch(rel, pat):
            return True
        if "**/" in pat and fnmatch.fnmatch(rel, pat.replace("**/", "")):
            return True
    return False


class TestBuiltinResourcesPackaged:
    def _builtin_files(self) -> list[str]:
        roots = [_PKG / "agents" / "builtin", _PKG / "skills" / "builtin"]
        out: list[str] = []
        for root in roots:
            for f in root.rglob("*.md"):
                out.append(f.relative_to(_PKG).as_posix())
        return out

    def test_every_builtin_md_is_in_package_data(self):
        patterns = _package_data_patterns()
        files = self._builtin_files()
        assert files, "no built-in .md files found — test wired wrong?"
        missing = [rel for rel in files if not _covered(rel, patterns)]
        assert not missing, (
            "built-in resources NOT covered by package-data (won't ship in the "
            f"wheel): {missing}\npatterns: {patterns}"
        )

    def test_nested_skill_reference_is_covered(self):
        # The directory-style skills keep references in a nested dir — the
        # canary that a non-recursive glob would miss.
        patterns = _package_data_patterns()
        nested = "skills/builtin/create-skill/references/format.md"
        assert (_PKG / nested).exists()
        assert _covered(nested, patterns)
