"""Unit tests for tool args summarization.

Direct tests for the per-tool branches in
``agent_cli.tools.action_summary``. The branches were previously covered
only indirectly via ``ContextManager`` integration tests; pinning each
branch in isolation here protects against regressions when new tools
are added.
"""

from __future__ import annotations

from agent_cli.tools.action_summary import summarize_tool_args


class TestSummarizeToolArgs:
    """Branches keyed on observation record ``tool`` name."""

    def test_read_file_returns_path(self):
        assert summarize_tool_args("read_file", {"path": "src/x.py"}) == "src/x.py"

    def test_write_file_returns_path(self):
        assert summarize_tool_args("write_file", {"path": "out.txt"}) == "out.txt"

    def test_edit_file_returns_path(self):
        assert summarize_tool_args("edit_file", {"path": "a.py"}) == "a.py"

    def test_shell_truncates_to_60(self):
        out = summarize_tool_args("shell", {"command": "y" * 200})
        assert len(out) == 60

    def test_delegate_returns_agent(self):
        # observation-side delegate has already been resolved to a single
        # agent.
        assert summarize_tool_args("delegate", {"agent": "explorer"}) == "explorer"

    def test_run_skill_returns_name_only(self):
        # observation-side run_skill drops ``arguments`` from the header.
        assert (
            summarize_tool_args("run_skill", {"name": "summarize", "arguments": "x"})
            == "summarize"
        )

    def test_unknown_tool_returns_first_string_value(self):
        assert summarize_tool_args("x", {"flag": True, "key": "v"}) == "v"

    def test_unknown_tool_no_strings_returns_empty(self):
        assert summarize_tool_args("x", {"flag": True}) == ""
