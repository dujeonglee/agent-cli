"""Virtual tools — schema-only entries the loop intercepts before
dispatch.

``complete`` / ``ask`` / ``run_skill`` never reach ``execute_tool``: the
loop's ``_dispatch_text_path`` handles each by name and returns early.
Their :meth:`run` is a placeholder mirroring the old
``__init__`` lambdas (returns the salient field) so direct callers and
tests still get a sane ToolResult, but the real behaviour lives in the
loop. They carry full schemas so the registry, system prompt, and input
validation treat them uniformly with executable tools.
"""

from __future__ import annotations

from pathlib import Path

from agent_cli.tools.base import Tool
from agent_cli.tools.result import ToolResult


class CompleteTool(Tool):
    name = "complete"
    description = "Call this tool when the task is done. Provide the final result."
    parameters = {
        "type": "object",
        "properties": {
            "result": {"type": "string", "description": "The final result or answer"},
        },
        "required": ["result"],
    }

    def _run(self, args: dict, *, session_dir: Path | None = None) -> ToolResult:
        return ToolResult(
            True,
            output=args.get(
                "result",
                "(Completed without result — model may lack capability for this task)",
            ),
        )


class AskTool(Tool):
    name = "ask"
    # Compact gate only. The full ask-vs-complete decision tree (examples,
    # rule of thumb) lives in the inline guide ``_ASK_INLINE`` in
    # ``prompts/system_prompt.py``, which is always rendered right after
    # this description — keeping the prose in one place avoids the two
    # surfaces teaching the same distinction back to back.
    description = (
        "Ask the user ONE question and WAIT for their reply. One question per "
        "op — to ask several, emit several `ask` ops in the array (each is "
        "answered in turn), the same way you batch read_file. Use only when you "
        "cannot proceed without specific input; otherwise end with `complete`."
    )
    parameters = {
        "type": "object",
        "properties": {
            "question": {
                "type": "string",
                "description": "The question to ask the user.",
            },
        },
        "required": ["question"],
    }

    def _run(self, args: dict, *, session_dir: Path | None = None) -> ToolResult:
        # Placeholder for direct/test callers — the loop intercepts `ask`
        # before dispatch. ``question`` is the flat single-question field; the
        # legacy ``questions`` list is still tolerated (loop's _extract_questions
        # accepts both) so older emissions don't break.
        q = args.get("question") or args.get("questions") or []
        if isinstance(q, str):
            q = [q]
        return ToolResult(True, output="\n".join(str(x) for x in q) or "(ask)")


class RunSkillTool(Tool):
    name = "run_skill"
    description = (
        "Run a registered skill by name. Use this to invoke specialized "
        "prompt-based workflows like code review, optimization, or test generation."
    )
    parameters = {
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": "Skill name (e.g. 'optimize', 'review-code', 'summarize', 'test')",
            },
            "arguments": {
                "type": "string",
                "description": "Arguments to pass to the skill (e.g. file path)",
            },
        },
        "required": ["name"],
    }

    def _run(self, args: dict, *, session_dir: Path | None = None) -> ToolResult:
        return ToolResult(True, output="(run_skill: intercepted by loop)")
