"""Unit tests for tool action / args summarization.

Direct tests for the per-tool branches in
``agent_cli.tools.action_summary``. The branches were previously covered
only indirectly via ``ContextManager`` integration tests; pinning each
branch in isolation here protects against regressions when new tools
are added.
"""

from __future__ import annotations

from agent_cli.tools.action_summary import (
    summarize_action_args,
    summarize_tool_args,
)


# ─── summarize_action_args ─────────────────────────────


class TestSummarizeActionArgs:
    """Branches keyed on assistant emission ``action`` name."""

    def test_non_dict_input_truncated_to_80_chars(self):
        assert summarize_action_args("foo", "x" * 200) == "x" * 80

    def test_non_dict_empty_returns_empty(self):
        assert summarize_action_args("foo", None) == ""
        assert summarize_action_args("foo", "") == ""

    def test_read_file_returns_path(self):
        assert summarize_action_args("read_file", {"path": "src/x.py"}) == "src/x.py"

    def test_write_file_returns_path(self):
        assert summarize_action_args("write_file", {"path": "out.txt"}) == "out.txt"

    def test_edit_file_returns_path(self):
        assert summarize_action_args("edit_file", {"path": "a.py"}) == "a.py"

    def test_shell_truncates_command_to_60_chars(self):
        long_cmd = "echo " + "y" * 200
        out = summarize_action_args("shell", {"command": long_cmd})
        assert len(out) == 60
        assert out.startswith("echo ")

    def test_shell_empty_command_returns_empty(self):
        assert summarize_action_args("shell", {"command": ""}) == ""

    def test_delegate_single_task(self):
        out = summarize_action_args(
            "delegate",
            {"tasks": [{"agent": "explorer", "task": "find auth path"}]},
        )
        assert out == 'explorer, "find auth path"'

    def test_delegate_truncates_task_to_40_chars(self):
        out = summarize_action_args(
            "delegate", {"tasks": [{"agent": "x", "task": "z" * 100}]}
        )
        # `task` is sliced before quoting → 40 z's between quotes.
        assert out == 'x, "' + "z" * 40 + '"'

    def test_delegate_multi_task_shows_count(self):
        out = summarize_action_args(
            "delegate",
            {
                "tasks": [
                    {"agent": "a", "task": "t1"},
                    {"agent": "b", "task": "t2"},
                    {"agent": "c", "task": "t3"},
                ]
            },
        )
        assert out == 'a, "t1" +2 more'

    def test_delegate_no_tasks_returns_empty(self):
        assert summarize_action_args("delegate", {"tasks": []}) == ""
        assert summarize_action_args("delegate", {}) == ""

    def test_run_skill_with_arguments(self):
        out = summarize_action_args(
            "run_skill", {"name": "summarize", "arguments": "report.md"}
        )
        assert out == "summarize(report.md)"

    def test_run_skill_no_arguments(self):
        out = summarize_action_args("run_skill", {"name": "summarize"})
        assert out == "summarize"

    def test_unknown_action_returns_first_string_value(self):
        out = summarize_action_args(
            "totally_new_tool", {"flag": True, "label": "hello world"}
        )
        # First string value wins; non-strings skipped.
        assert out == "hello world"

    def test_unknown_action_no_string_values_returns_empty(self):
        assert summarize_action_args("x", {"flag": True}) == ""


# ─── summarize_tool_args ───────────────────────────────


class TestSummarizeToolArgs:
    """Branches keyed on observation record ``tool`` name. Shape is
    different from the assistant-emission side for ``delegate`` and
    ``run_skill``, hence a separate function."""

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
        # agent — different shape from the emission-side ``tasks`` list.
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
