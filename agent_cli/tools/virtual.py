"""Virtual tools — schema-only entries the loop intercepts before
dispatch.

``complete`` / ``ask`` / ``run_skill`` / ``ready_for_review`` never reach
``execute_tool``: the loop's ``_dispatch_text_path`` handles each by name
and returns early. Their :meth:`run` is a placeholder mirroring the old
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
        "Ask the user one or more questions and WAIT for their reply. "
        "Use only when you cannot proceed without specific input from the "
        "user; otherwise end with `complete`."
    )
    parameters = {
        "type": "object",
        "properties": {
            "questions": {
                "type": "array",
                "items": {"type": "string"},
                "description": "List of questions to ask the user",
            },
        },
        "required": ["questions"],
    }

    def _run(self, args: dict, *, session_dir: Path | None = None) -> ToolResult:
        # Placeholder for direct/test callers — the loop intercepts `ask`
        # before dispatch. Return the salient field (the `questions` list,
        # matching the schema) joined into one block; sibling virtual tools
        # do the same with their own salient field.
        questions = args.get("questions") or []
        if isinstance(questions, str):
            questions = [questions]
        return ToolResult(True, output="\n".join(str(q) for q in questions) or "(ask)")


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


class ReadyForReviewTool(Tool):
    name = "ready_for_review"
    description = (
        "Call this BEFORE complete to verify your work fulfills all requirements. "
        "The system will return the original request for you to review against. "
        "After reviewing, call complete if everything is done, or continue working if not."
    )
    parameters = {
        "type": "object",
        "properties": {
            "summary": {
                "type": "string",
                "description": "Brief summary of what you accomplished",
            },
        },
        "required": ["summary"],
    }

    def _run(self, args: dict, *, session_dir: Path | None = None) -> ToolResult:
        return ToolResult(True, output=args.get("summary", ""))
