"""Prompt order of the tools (v10.13.0) — related tools sit together.

The order used to be the order tools happened to be added, with ``edit_file``
and ``agent`` moved to the end. The pairs a model has to choose between or use
together (edit/write, ask/answer, message/reply) were introduced far apart.
"""

from __future__ import annotations

from agent_cli.tools.registry import TOOLS, effective_tool_names

ORDER = list(TOOLS)


def _adjacent(a: str, b: str) -> bool:
    return ORDER.index(b) - ORDER.index(a) == 1


class TestBuiltinOrder:
    def test_file_tools_read_edit_write(self):
        """edit_file's guide builds on read_file's hashline output, and edit is
        the default way to change a file — so it comes before write_file."""
        assert ORDER[:4] == ["read_file", "edit_file", "write_file", "code_index"]

    def test_pairs_are_adjacent(self):
        assert _adjacent("ask", "answer")
        assert _adjacent("message", "reply")
        assert _adjacent("monitor", "schedule")
        assert _adjacent("read_context", "memory")

    def test_complete_is_last(self):
        assert ORDER[-1] == "complete"


class TestEffectiveOrder:
    def test_caller_order_does_not_decide_prompt_order(self):
        asked = ["complete", "shell", "write_file", "agent", "read_file", "edit_file"]
        assert effective_tool_names(asked) == [
            "read_file",
            "edit_file",
            "write_file",
            "shell",
            "agent",
            "complete",
        ]

    def test_unknown_names_keep_caller_order_after_the_builtins(self):
        """MCP tools join ``TOOLS`` at boot — the registry has no rank for
        them, so they follow the built-ins in the order given."""
        got = effective_tool_names(["mcp__b__x", "shell", "mcp__a__y"])
        assert got == ["shell", "complete", "mcp__b__x", "mcp__a__y"]

    def test_none_means_every_tool_in_registry_order(self):
        assert effective_tool_names(None) == ORDER

    def test_a_missing_tool_just_leaves_a_gap(self):
        """A loop without edit_file keeps read and write next to each other."""
        assert effective_tool_names(["write_file", "read_file"]) == [
            "read_file",
            "write_file",
            "complete",
        ]
