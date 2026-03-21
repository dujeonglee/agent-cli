"""Write file tool."""
from __future__ import annotations

from pathlib import Path


def tool_write_file(args: dict) -> str:
    """Create or overwrite a file with raw content."""
    path = args.get("path", "")
    content = args.get("content", "")
    try:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        return f"File saved: {path} ({len(content)} bytes)"
    except Exception as e:
        raise RuntimeError(f"write_file failed: {e}")
