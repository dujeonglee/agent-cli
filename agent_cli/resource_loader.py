"""ResourceLoader — scan multiple paths for .md files with optional YAML frontmatter.

Used by skills, agents, and directives to discover files with priority ordering.
Higher-priority paths override lower-priority paths when names collide.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

try:
    import yaml
except ImportError:
    yaml = None  # type: ignore

_FRONTMATTER_PATTERN = re.compile(r"^---\s*\n(.*?)\n---\s*\n(.*)", re.S)


@dataclass
class Resource:
    """A discovered resource file."""

    name: str
    body: str
    meta: dict = field(default_factory=dict)
    source_path: str = ""


class ResourceLoader:
    """Scan ordered search paths for .md resources.

    Paths are in priority order: first path = highest priority.
    When the same name appears in multiple paths, the highest priority wins.
    Supports flat files (name.md) and directory structure (name/SKILL.md).
    """

    def __init__(
        self,
        search_paths: list[Path],
        pattern: str = "*.md",
        dir_entry: str = "",
    ):
        """
        Args:
            search_paths: Ordered paths to scan (first = highest priority).
            pattern: Glob pattern for flat files (default: *.md).
            dir_entry: If set, also scan subdirectories for this filename
                       (e.g. "SKILL.md" for skills/<name>/SKILL.md).
        """
        self.search_paths = search_paths
        self.pattern = pattern
        self.dir_entry = dir_entry

    def load_all(self) -> dict[str, Resource]:
        """Load all resources. Higher-priority paths override lower ones."""
        results: dict[str, Resource] = {}

        # Reverse so higher-priority paths overwrite lower ones
        for search_dir in reversed(self.search_paths):
            if not search_dir.is_dir():
                continue

            # Flat files: *.md
            for md_file in sorted(search_dir.glob(self.pattern)):
                if md_file.is_file():
                    resource = self._parse_file(md_file)
                    if resource:
                        results[resource.name] = resource

            # Directory entries: name/SKILL.md
            if self.dir_entry:
                for subdir in sorted(search_dir.iterdir()):
                    if subdir.is_dir():
                        entry_file = subdir / self.dir_entry
                        if entry_file.is_file():
                            resource = self._parse_file(entry_file)
                            if resource:
                                results[resource.name] = resource

        return results

    def load_one(self, name: str) -> Resource | None:
        """Load a single resource by name. First match wins (highest priority)."""
        for search_dir in self.search_paths:
            if not search_dir.is_dir():
                continue

            # Flat file
            flat = search_dir / f"{name}.md"
            if flat.is_file():
                return self._parse_file(flat)

            # Directory entry
            if self.dir_entry:
                dir_entry = search_dir / name / self.dir_entry
                if dir_entry.is_file():
                    return self._parse_file(dir_entry)

        return None

    def list_names(self) -> list[str]:
        """List all available resource names (deduplicated, priority-ordered)."""
        return list(self.load_all().keys())

    @staticmethod
    def _parse_file(path: Path) -> Resource | None:
        """Parse a markdown file with optional YAML frontmatter."""
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            return None

        match = _FRONTMATTER_PATTERN.match(text)
        if match and yaml is not None:
            try:
                meta = yaml.safe_load(match.group(1))
                if not isinstance(meta, dict):
                    meta = {}
            except Exception:
                meta = {}
            body = match.group(2).strip()
        else:
            meta = {}
            body = text.strip()

        if not body:
            return None

        # Name: from meta, or directory name (for SKILL.md), or filename stem
        name = meta.get("name", "")
        if not name:
            if path.name in ("SKILL.md", "AGENT.md"):
                name = path.parent.name
            else:
                name = path.stem

        return Resource(
            name=name,
            body=body,
            meta=meta,
            source_path=str(path),
        )
