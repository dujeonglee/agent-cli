"""Unit tests for ``Tool.summary_arg`` — the short per-tool label used in
the compaction transcript (``_to_summary_text``) and observation headers.

Uses the REAL ``action_input`` shape each tool receives. Flat-native tools
(write_file, read_file, edit_file, code_index — Step 3) take plain
``{path/mode, ...}``; the remaining batch tool still uses a prefixed array
(``delegate_tasks``). The previous version tested ``summarize_tool_args``
with a hand-invented bare-key shape for batch tools that never occurred in
history.jsonl, so it passed while the function returned "" for every real
record, masking the prefix regression.
Each tool now owns its own label via ``Tool.summary_arg`` (sibling of
``Tool.touched_paths``).
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
        # Flat-native (Step 3): edit_file takes flat {path, op, pos, ...}.
        assert _sa("edit_file", {"path": "a.py", "op": "replace", "pos": "1#AA"}) == (
            "a.py"
        )

    def test_read_file_flat_path(self):
        # Flat-native (Step 3): one op reads one file → summary is its path.
        assert _sa("read_file", {"path": "a.c"}) == "a.c"

    def test_code_index_mode_and_path(self):
        # Flat-native (Step 3): code_index takes a flat single query.
        assert _sa("code_index", {"mode": "fetch", "path": "x.c"}) == "fetch x.c"

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
