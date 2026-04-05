"""Scratchpad + Artifact persistent context management.

Scratchpad: always loaded into context window, survives compaction.
Artifacts: per-step detailed results, loaded selectively via frontmatter index.

File layout:
  .agent-cli/
    scratchpad.md            # always loaded (anchor)
    artifacts/
      turn_001.md            # per-step results with YAML frontmatter
      step_002.md
      ...
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

# ── Directory layout ─────────────────────────────────────────

_DEFAULT_BASE = Path(".agent-cli")


def session_scratchpad_dir(session_id: str, base: Path = _DEFAULT_BASE) -> Path:
    """Return the session-scoped scratchpad directory path."""
    return base / "sessions" / session_id


def _ensure_dirs(base: Path) -> Path:
    base.mkdir(parents=True, exist_ok=True)
    (base / "artifacts").mkdir(exist_ok=True)
    return base


# ── Data models ──────────────────────────────────────────────


@dataclass
class ArtifactMeta:
    """Frontmatter parsed from an artifact file."""

    entry_id: str
    step: int
    tags: list[str] = field(default_factory=list)
    summary: str = ""
    token_count: int = 0
    created_at: str = ""
    path: str = ""  # resolved file path


@dataclass
class ContextBudget:
    """Dynamic token budget allocation per section.

    Ratios adapt to total available tokens (model context_window).
    Smaller models get more budget for scratchpad (proportionally),
    larger models can afford more artifact loading.
    """

    total_tokens: int
    reserved_system: float = 0.12  # system prompt + tools
    reserved_response: float = 0.05  # response generation
    scratchpad_ratio: float = 0.05  # scratchpad (small, always loaded)
    artifact_ratio: float = 0.30  # selected artifacts
    conversation_ratio: float = 0.48  # conversation history

    @classmethod
    def for_model(cls, context_window: int) -> ContextBudget:
        """Create budget adapted to model size."""
        if context_window <= 8192:
            # Small model: prioritize scratchpad, minimal artifacts
            return cls(
                total_tokens=context_window,
                reserved_system=0.15,
                reserved_response=0.08,
                scratchpad_ratio=0.10,
                artifact_ratio=0.15,
                conversation_ratio=0.52,
            )
        elif context_window <= 32768:
            # Medium model: balanced
            return cls(
                total_tokens=context_window,
                reserved_system=0.12,
                reserved_response=0.06,
                scratchpad_ratio=0.06,
                artifact_ratio=0.25,
                conversation_ratio=0.51,
            )
        else:
            # Large model (128K+): generous artifact budget
            return cls(
                total_tokens=context_window,
                reserved_system=0.08,
                reserved_response=0.04,
                scratchpad_ratio=0.03,
                artifact_ratio=0.35,
                conversation_ratio=0.50,
            )

    @property
    def scratchpad_tokens(self) -> int:
        return int(self.total_tokens * self.scratchpad_ratio)

    @property
    def artifact_tokens(self) -> int:
        return int(self.total_tokens * self.artifact_ratio)

    @property
    def conversation_tokens(self) -> int:
        return int(self.total_tokens * self.conversation_ratio)

    def to_dict(self) -> dict[str, Any]:
        return {
            "total": self.total_tokens,
            "scratchpad": self.scratchpad_tokens,
            "artifacts": self.artifact_tokens,
            "conversation": self.conversation_tokens,
        }


# ── YAML frontmatter parsing ────────────────────────────────

_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)


def parse_frontmatter(text: str) -> tuple[dict, str]:
    """Parse YAML frontmatter from markdown text.

    Returns (metadata_dict, body_text).
    If no frontmatter, returns ({}, full_text).
    """
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return {}, text
    try:
        meta = yaml.safe_load(m.group(1)) or {}
    except yaml.YAMLError:
        meta = {}
    body = text[m.end() :]
    return meta, body


def render_frontmatter(meta: dict, body: str) -> str:
    """Render YAML frontmatter + markdown body."""
    fm = yaml.dump(meta, default_flow_style=False, allow_unicode=True).strip()
    return f"---\n{fm}\n---\n\n{body}"


# ── Scratchpad operations ────────────────────────────────────


def load_scratchpad(base: Path = _DEFAULT_BASE) -> str:
    """Load scratchpad content. Returns empty string if not exists."""
    path = base / "scratchpad.md"
    if path.is_file():
        return path.read_text(encoding="utf-8")
    return ""


def save_scratchpad(content: str, base: Path = _DEFAULT_BASE) -> None:
    """Save scratchpad content."""
    _ensure_dirs(base)
    path = base / "scratchpad.md"
    path.write_text(content, encoding="utf-8")


def init_scratchpad(base: Path = _DEFAULT_BASE) -> str:
    """Initialize a new scratchpad."""
    content = render_frontmatter(
        {
            "status": "in_progress",
            "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        },
        "## Progress\n\n## Decisions\n\n## Open Questions\n",
    )
    save_scratchpad(content, base)
    return content


def append_progress(
    step: int,
    summary: str,
    artifact_path: str | None = None,
    base: Path = _DEFAULT_BASE,
) -> None:
    """Append a progress line to the scratchpad."""
    content = load_scratchpad(base)
    if not content:
        return

    ref = f" → {artifact_path}" if artifact_path else ""
    line = f"- [step {step}] {summary}{ref}\n"

    # Append at end of "## Progress" section (chronological order)
    if "## Progress" in content:
        prog_start = content.index("## Progress")
        # Find the next section header (## Decisions, ## Open Questions, etc.)
        next_section = content.find("\n## ", prog_start + 1)
        if next_section >= 0:
            # Insert before the next section
            content = content[:next_section] + line + content[next_section:]
        else:
            # No next section — append at end
            if not content.endswith("\n"):
                content += "\n"
            content += line
    else:
        content += f"\n## Progress\n{line}"

    # Update timestamp in frontmatter
    meta, body = parse_frontmatter(content)
    if meta:
        meta["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        content = render_frontmatter(meta, body)

    save_scratchpad(content, base)


def append_decision(
    step: int,
    decision: str,
    base: Path = _DEFAULT_BASE,
) -> None:
    """Append a decision to the scratchpad."""
    content = load_scratchpad(base)
    if not content:
        return

    line = f"- [step {step}] {decision}\n"

    # Append at end of "## Decisions" section (chronological order)
    if "## Decisions" in content:
        dec_start = content.index("## Decisions")
        next_section = content.find("\n## ", dec_start + 1)
        if next_section >= 0:
            content = content[:next_section] + line + content[next_section:]
        else:
            if not content.endswith("\n"):
                content += "\n"
            content += line
    else:
        content += f"\n## Decisions\n{line}"

    meta, body = parse_frontmatter(content)
    if meta:
        meta["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        content = render_frontmatter(meta, body)

    save_scratchpad(content, base)


# ── Artifact operations ──────────────────────────────────────


def save_artifact(
    step: int,
    content: str,
    tags: list[str] | None = None,
    summary: str = "",
    base: Path = _DEFAULT_BASE,
    skill_name: str = "",
    parent_step: int = 0,
) -> str:
    """Save a step artifact with YAML frontmatter. Returns the file path.

    If skill_name is set, artifact is stored in a subdirectory:
      artifacts/step_{parent_step}_{skill_name}/step_{step}.md
    """
    _ensure_dirs(base)
    entry_id = f"step_{step:04d}"

    if skill_name and parent_step > 0:
        subdir = base / "artifacts" / f"step_{parent_step:04d}_{skill_name}"
        subdir.mkdir(parents=True, exist_ok=True)
        path = subdir / f"{entry_id}.md"
    else:
        path = base / "artifacts" / f"{entry_id}.md"

    # Estimate tokens (chars/4 heuristic matching token_estimator.py)
    token_count = len(content) // 4

    text = render_frontmatter(
        {
            "entry_id": entry_id,
            "step": step,
            "tags": tags or [],
            "summary": summary,
            "token_count": token_count,
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        },
        content,
    )
    path.write_text(text, encoding="utf-8")
    return str(path)


def load_artifact(path: str | Path) -> tuple[ArtifactMeta, str]:
    """Load an artifact file, return (metadata, body)."""
    p = Path(path)
    if not p.is_file():
        return ArtifactMeta(entry_id="", step=0), ""
    text = p.read_text(encoding="utf-8")
    meta_dict, body = parse_frontmatter(text)
    meta = ArtifactMeta(
        entry_id=meta_dict.get("entry_id", p.stem),
        step=meta_dict.get("step", 0),
        tags=meta_dict.get("tags", []),
        summary=meta_dict.get("summary", ""),
        token_count=meta_dict.get("token_count", 0),
        created_at=meta_dict.get("created_at", ""),
        path=str(p),
    )
    return meta, body


def build_artifact_index(base: Path = _DEFAULT_BASE) -> list[ArtifactMeta]:
    """Scan all artifacts and return frontmatter index (no body loaded)."""
    artifacts_dir = base / "artifacts"
    if not artifacts_dir.is_dir():
        return []

    index = []
    for f in sorted(artifacts_dir.rglob("step_*.md")):
        text = f.read_text(encoding="utf-8")
        meta_dict, _ = parse_frontmatter(text)
        if meta_dict:
            index.append(
                ArtifactMeta(
                    entry_id=meta_dict.get("entry_id", f.stem),
                    step=meta_dict.get("step", 0),
                    tags=meta_dict.get("tags", []),
                    summary=meta_dict.get("summary", ""),
                    token_count=meta_dict.get("token_count", 0),
                    created_at=meta_dict.get("created_at", ""),
                    path=str(f),
                )
            )
    return index


def select_artifacts(
    index: list[ArtifactMeta],
    current_tags: list[str],
    budget_tokens: int,
    recent_n: int = 3,
) -> list[ArtifactMeta]:
    """Select relevant artifacts within token budget.

    Strategy (tag-based, replaceable):
      1. Most recent N steps (recency bias)
      2. Tag overlap scoring for older steps
      3. Fill within budget
    """
    if not index:
        return []

    selected: list[ArtifactMeta] = []
    remaining = budget_tokens
    seen_ids: set[str] = set()

    # 1. Recent turns first
    recent = index[-recent_n:] if recent_n > 0 else []
    for entry in reversed(recent):
        if entry.token_count <= remaining:
            selected.append(entry)
            seen_ids.add(entry.entry_id)
            remaining -= entry.token_count

    # 2. Tag-scored older entries
    if current_tags:
        tag_set = set(current_tags)
        scored = []
        for entry in index:
            if entry.entry_id in seen_ids:
                continue
            overlap = len(set(entry.tags) & tag_set)
            if overlap > 0:
                scored.append((overlap, entry))
        scored.sort(key=lambda x: -x[0])

        for _, entry in scored:
            if entry.token_count <= remaining:
                selected.append(entry)
                seen_ids.add(entry.entry_id)
                remaining -= entry.token_count

    return selected


# ── Cleanup operations ──────────────────────────────────────


def clear_scratchpad(base: Path) -> None:
    """Remove scratchpad.md, artifacts/, and the session directory itself."""
    import shutil

    if not base.exists():
        return
    shutil.rmtree(base, ignore_errors=True)


def delete_artifact(path: str | Path) -> None:
    """Delete a single artifact file."""
    p = Path(path)
    if p.is_file():
        p.unlink()
