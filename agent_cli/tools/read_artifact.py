"""read_artifact tool — lets LLM recover context from saved artifacts.

Artifacts are tool outputs saved per-iteration with YAML frontmatter.
Unlike read_file, this returns raw content WITHOUT hashline formatting.
"""

from __future__ import annotations

from pathlib import Path


def tool_read_artifact(args: dict, **kwargs) -> str:
    """Read artifact content, list artifacts, or search by tag.

    Modes:
      - path: read a specific artifact file (no hashlines)
      - list: list all artifacts in the current session
      - search: find artifacts matching a tag
    """
    mode = args.get("mode", "")
    path = args.get("path", "")
    tag = args.get("tag", "")

    # Default mode: if path is given, read it
    if path and not mode:
        mode = "read"
    if not mode:
        mode = "list"

    ctx = kwargs.get("ctx")
    scratchpad_dir = _get_scratchpad_dir(ctx)

    if mode == "read":
        return _read_artifact(path, scratchpad_dir)
    elif mode == "list":
        return _list_artifacts(scratchpad_dir)
    elif mode == "search":
        return _search_artifacts(tag, scratchpad_dir)
    else:
        return f"Error: unknown mode '{mode}'. Use 'list', 'search', or provide 'path'."


def _get_scratchpad_dir(ctx) -> Path | None:
    """Extract scratchpad dir from context manager."""
    if ctx and hasattr(ctx, "_scratchpad_dir"):
        return ctx._scratchpad_dir
    return None


def _read_artifact(path: str, scratchpad_dir: Path | None) -> str:
    """Read a single artifact file without hashlines."""
    if not path:
        return "Error: 'path' is required for reading an artifact."

    p = Path(path)

    # Try relative to scratchpad_dir if not absolute
    if not p.is_absolute() and scratchpad_dir:
        candidate = scratchpad_dir / path
        if candidate.is_file():
            p = candidate

    if not p.is_file():
        return f"Error: artifact not found at '{path}'."

    try:
        from agent_cli.context.scratchpad import parse_frontmatter

        text = p.read_text(encoding="utf-8")
        meta, body = parse_frontmatter(text)

        # Build header from frontmatter
        header_parts = []
        if meta.get("summary"):
            header_parts.append(f"Summary: {meta['summary']}")
        if meta.get("tags"):
            header_parts.append(f"Tags: {', '.join(meta['tags'])}")
        if meta.get("turn"):
            header_parts.append(f"Turn: {meta['turn']}")

        header = " | ".join(header_parts) if header_parts else ""
        if header:
            return f"[{header}]\n{body}"
        return body
    except Exception as e:
        return f"Error reading artifact: {e}"


def _list_artifacts(scratchpad_dir: Path | None) -> str:
    """List all artifacts in the current session."""
    if not scratchpad_dir:
        return "No artifacts available (no active session)."

    from agent_cli.context.scratchpad import build_artifact_index

    index = build_artifact_index(scratchpad_dir)
    if not index:
        return "No artifacts found in this session."

    lines = [f"Artifacts ({len(index)} total):"]
    for meta in index:
        tags_str = ", ".join(meta.tags) if meta.tags else ""
        summary = meta.summary or "(no summary)"
        lines.append(f"- {meta.entry_id} [{tags_str}] {summary}")
        lines.append(f"  path: {meta.path}")

    return "\n".join(lines)


def _search_artifacts(tag: str, scratchpad_dir: Path | None) -> str:
    """Search artifacts by tag."""
    if not tag:
        return "Error: 'tag' is required for search mode."

    if not scratchpad_dir:
        return "No artifacts available (no active session)."

    from agent_cli.context.scratchpad import build_artifact_index

    index = build_artifact_index(scratchpad_dir)
    matched = [a for a in index if tag in a.tags]

    if not matched:
        return f"No artifacts found with tag '{tag}'."

    lines = [f"Artifacts matching '{tag}' ({len(matched)} found):"]
    for meta in matched:
        summary = meta.summary or "(no summary)"
        lines.append(f"- {meta.entry_id} {summary}")
        lines.append(f"  path: {meta.path}")

    return "\n".join(lines)
