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

from typing import ClassVar

from agent_cli.tools.base import Tool
from agent_cli.tools.result import ToolResult


class CompleteTool(Tool):
    non_workspace_or_composite = True
    name = "complete"
    description = "Call this tool when the task is done. Provide the final result."
    parameters: ClassVar[dict] = {
        "type": "object",
        "properties": {
            "result": {"type": "string", "description": "The final result or answer"},
        },
        "required": ["result"],
    }

    def _run(self, args: dict, *, ctx=None) -> ToolResult:
        return ToolResult(
            True,
            output=args.get(
                "result",
                "(Completed without result — model may lack capability for this task)",
            ),
        )


class AskTool(Tool):
    non_workspace_or_composite = True
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
    parameters: ClassVar[dict] = {
        "type": "object",
        "properties": {
            "question": {
                "type": "string",
                "description": "The question to ask the user.",
            },
        },
        "required": ["question"],
    }

    def _run(self, args: dict, *, ctx=None) -> ToolResult:
        # Placeholder for direct/test callers — the loop intercepts `ask`
        # before dispatch. ``question`` is the flat single-question field; the
        # legacy ``questions`` list is still tolerated (loop's _extract_questions
        # accepts both) so older emissions don't break.
        q = args.get("question") or args.get("questions") or []
        if isinstance(q, str):
            q = [q]
        return ToolResult(True, output="\n".join(str(x) for x in q) or "(ask)")


class MessageTool(Tool):
    non_workspace_or_composite = True
    name = "message"
    # Present ONLY for resident (persistent) sub-agents — the loop injects a
    # ``message_handler`` and force-adds this tool for them, and strips it
    # everywhere else (the main agent talks to agents via the ``agent`` tool).
    description = (
        "Message another running agent (a peer, or `main`) and keep working. "
        "Async: your message is delivered to them and its reply comes back to "
        "YOU as a NEW message — you are NOT blocked. See `## Live Agents` for "
        "who is running and their roles. Use to consult a specialist, hand off "
        "a sub-question, or report a result. When you have nothing to add to a "
        "peer's reply, just `complete` (or reply `LGTM`) so the exchange ends."
    )
    parameters: ClassVar[dict] = {
        "type": "object",
        "properties": {
            "to": {
                "type": "string",
                "description": "Target agent key (from `## Live Agents`), or 'main'.",
            },
            "text": {
                "type": "string",
                "description": "The message to send.",
            },
        },
        "required": ["to", "text"],
    }

    def _run(self, args: dict, *, ctx=None) -> ToolResult:
        # Placeholder — the loop intercepts `message` before dispatch and
        # routes it through the injected ``message_handler``. Direct/test
        # callers get a benign echo.
        to = args.get("to", "")
        return ToolResult(True, output=f"(message to {to}: intercepted by loop)")


class RunSkillTool(Tool):
    non_workspace_or_composite = True
    name = "run_skill"
    description = (
        "Run a registered skill by name. Use this to invoke specialized "
        "prompt-based workflows like code review, optimization, or test generation."
    )
    parameters: ClassVar[dict] = {
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

    def _run(self, args: dict, *, ctx=None) -> ToolResult:
        return ToolResult(True, output="(run_skill: intercepted by loop)")
