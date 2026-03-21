"""Multi-layer tool output truncation with model-adaptive limits."""
from __future__ import annotations

from dataclasses import dataclass

from agent_cli.constants import SMALL_MODEL_CONTEXT, MEDIUM_MODEL_CONTEXT
from agent_cli.providers.compat import ModelCapabilities


@dataclass
class TruncationConfig:
    max_lines: int = 200
    max_bytes: int = 8_000
    direction: str = "head"  # "head" | "tail"
    show_notice: bool = True


# Direction rules per tool
_TOOL_DIRECTIONS = {
    "read_file": "head",
    "shell": "tail",
    "write_file": "head",
    "edit_file": "head",
    "delegate": "tail",
}


def get_truncation_config(
    capabilities: ModelCapabilities, tool_name: str
) -> TruncationConfig:
    """Get truncation config adapted to model context window."""
    direction = _TOOL_DIRECTIONS.get(tool_name, "head")

    if capabilities.context_window <= SMALL_MODEL_CONTEXT:
        return TruncationConfig(
            max_lines=50, max_bytes=2_000, direction=direction
        )
    elif capabilities.context_window <= MEDIUM_MODEL_CONTEXT:
        return TruncationConfig(
            max_lines=100, max_bytes=4_000, direction=direction
        )
    else:
        return TruncationConfig(
            max_lines=200, max_bytes=8_000, direction=direction
        )


def truncate_output(text: str, config: TruncationConfig) -> str:
    """Truncate text by line count and byte size.

    Respects UTF-8 boundaries. Adds notice when truncated.
    """
    if not text:
        return text

    lines = text.split("\n")
    total_lines = len(lines)
    total_bytes = len(text.encode("utf-8"))

    truncated = False

    # Step 1: Line limit
    if total_lines > config.max_lines:
        if config.direction == "tail":
            lines = lines[-config.max_lines :]
        else:
            lines = lines[: config.max_lines]
        truncated = True

    # Step 2: Byte limit
    result = "\n".join(lines)
    result_bytes = len(result.encode("utf-8"))

    if result_bytes > config.max_bytes:
        encoded = result.encode("utf-8")
        # Truncate at byte boundary, then decode safely
        if config.direction == "tail":
            cut = encoded[-config.max_bytes :]
            result = cut.decode("utf-8", errors="ignore")
            # Trim to nearest newline to avoid partial lines
            nl = result.find("\n")
            if nl >= 0:
                result = result[nl + 1 :]
        else:
            cut = encoded[: config.max_bytes]
            result = cut.decode("utf-8", errors="ignore")
            # Trim to nearest newline
            nl = result.rfind("\n")
            if nl >= 0:
                result = result[: nl]
        truncated = True

    # Step 3: Add notice
    if truncated and config.show_notice:
        shown_lines = len(result.split("\n"))
        if config.direction == "tail":
            notice = (
                f"[... truncated: showing last {shown_lines} of "
                f"{total_lines} lines ({total_bytes} bytes total)]"
            )
            result = notice + "\n" + result
        else:
            notice = (
                f"[... truncated: showing first {shown_lines} of "
                f"{total_lines} lines ({total_bytes} bytes total)]"
            )
            result = result + "\n" + notice

    return result
