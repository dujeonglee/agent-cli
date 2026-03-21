"""Tests for tools/truncation."""

from agent_cli.tools.truncation import (
    TruncationConfig,
    get_truncation_config,
    truncate_output,
)
from agent_cli.providers.compat import ModelCapabilities


def _make_caps(ctx_window: int) -> ModelCapabilities:
    return ModelCapabilities(
        context_window=ctx_window,
        max_output_tokens=2048,
        supports_structured_output=False,
        supports_tool_calling=False,
        supports_thinking=False,
        thinking_budget=0,
        supports_strict_schema=False,
    )


class TestTruncateOutput:
    def test_no_truncation_needed(self):
        text = "line1\nline2\nline3"
        config = TruncationConfig(max_lines=10, max_bytes=1000)
        assert truncate_output(text, config) == text

    def test_head_truncation(self):
        text = "\n".join(f"line{i}" for i in range(100))
        config = TruncationConfig(max_lines=10, max_bytes=100_000, direction="head")
        result = truncate_output(text, config)
        assert "line0" in result
        assert "line99" not in result
        assert "truncated" in result

    def test_tail_truncation(self):
        text = "\n".join(f"line{i}" for i in range(100))
        config = TruncationConfig(max_lines=10, max_bytes=100_000, direction="tail")
        result = truncate_output(text, config)
        assert "line99" in result
        assert "line0" not in result
        assert "truncated" in result

    def test_empty_text(self):
        assert truncate_output("", TruncationConfig()) == ""

    def test_no_notice(self):
        text = "\n".join(f"line{i}" for i in range(100))
        config = TruncationConfig(max_lines=10, max_bytes=100_000, show_notice=False)
        result = truncate_output(text, config)
        assert "truncated" not in result


class TestGetTruncationConfig:
    def test_small_model(self):
        cfg = get_truncation_config(_make_caps(4096), "read_file")
        assert cfg.max_lines == 50
        assert cfg.max_bytes == 2_000
        assert cfg.direction == "head"

    def test_medium_model(self):
        cfg = get_truncation_config(_make_caps(32768), "read_file")
        assert cfg.max_lines == 100

    def test_large_model(self):
        cfg = get_truncation_config(_make_caps(128000), "read_file")
        assert cfg.max_lines == 200

    def test_shell_direction(self):
        cfg = get_truncation_config(_make_caps(32768), "shell")
        assert cfg.direction == "tail"
