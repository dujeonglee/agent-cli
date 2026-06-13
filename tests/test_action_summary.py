"""Unit tests for ``Tool.summary_arg`` — the short per-tool label used in
the compaction transcript (``_to_summary_text``) and observation headers.

Uses the REAL ``action_input`` shape (wire-key prefix + arrays), NOT the
old hand-invented bare-key shape. The previous version tested
``summarize_tool_args`` with ``{"path": ...}`` — a shape that never occurs
in history.jsonl (real keys are ``write_file_path`` / ``read_file_reads[]``)
— so it passed while the function returned "" for every real record,
masking the prefix regression. Each tool now owns its own label via
``Tool.summary_arg`` (sibling of ``Tool.touched_paths``).
"""

from __future__ import annotations

from agent_cli.tools.registry import TOOLS


def _sa(tool: str, action_input: dict) -> str:
    return TOOLS[tool].summary_arg(action_input)


class TestToolSummaryArg:
    def test_write_file_path(self):
        # Flat-native (Step 3): write_file takes flat {path, content}.
        assert _sa("write_file", {"path": "out.txt", "content": "x"}) == "out.txt"

    def test_edit_file_path(self):
        assert (
            _sa("edit_file", {"edit_file_path": "a.py", "edit_file_edits": []})
            == "a.py"
        )

    def test_read_file_single(self):
        assert _sa("read_file", {"read_file_reads": [{"path": "a.c"}]}) == "a.c"

    def test_read_file_multiple(self):
        assert (
            _sa("read_file", {"read_file_reads": [{"path": "a.c"}, {"path": "b.c"}]})
            == "2 files"
        )

    def test_code_index_mode_and_path(self):
        assert (
            _sa(
                "code_index", {"code_index_queries": [{"mode": "fetch", "path": "x.c"}]}
            )
            == "fetch x.c"
        )

    def test_shell_truncates_to_60(self):
        out = _sa("shell", {"shell_command": "y" * 200})
        assert len(out) == 60

    def test_delegate_agent(self):
        assert (
            _sa("delegate", {"delegate_tasks": [{"agent": "explorer", "task": "find"}]})
            == "explorer"
        )

    def test_run_skill_name(self):
        assert (
            _sa(
                "run_skill", {"run_skill_name": "summarize", "run_skill_arguments": "x"}
            )
            == "summarize"
        )

    def test_base_fallback_first_string(self):
        # Tools without an explicit override (complete/ask/...) fall back to
        # the first string value via the base implementation.
        assert _sa("complete", {"result": "done"}) == "done"

    def test_base_fallback_no_strings_empty(self):
        assert _sa("ready_for_review", {}) == ""
