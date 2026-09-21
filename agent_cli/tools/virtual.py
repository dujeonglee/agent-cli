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
    name = "complete"
    terminal = True  # 턴 종결 (T3 선언화 — dispatch 가 이 속성으로 flush/종료)
    description = (
        "Call this tool when the task is done. Provide the final result. "
        "When the tail lists Open Requests, `answers` is REQUIRED: "
        "list the ids you actually answered. Ids you leave out are reported "
        "to the user as unanswered."
    )
    parameters: ClassVar[dict] = {
        "type": "object",
        "properties": {
            "result": {"type": "string", "description": "The final result or answer"},
            "answers": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Ids of the user requests this result answers. Required "
                    "whenever the tail lists Open Requests — omitting "
                    "it there means you answered none of them."
                ),
            },
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
    name = "ask"
    requires_handler = "ctx"  # 비대화형 루프(ctx 없음)에선 목록에서 제거
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
    # 상주 에이전트용 (docs/agent-ask/DESIGN.md §3.1). 거기서 ``ask`` 는
    # **막지 않는다** — 질문을 등록하고 즉시 돌아온다. 마지막 문장이
    # 중요하다: ask 는 보통 "막혔다" 는 뜻이라 "계속하라" 고만 하면 작은
    # 모델이 추측으로 메우고 끝낸다. complete 해도 이어진다는 걸 알린다.
    RESIDENT_DESCRIPTION = (
        "Ask a question and KEEP WORKING — you are NOT blocked. The question "
        "goes to whoever requested your current task; their answer arrives "
        "later as a NEW message and you continue from there. Carry on with "
        "anything that does not depend on it. If nothing else can proceed, "
        "`complete` — you will be resumed when the answer arrives."
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
    name = "message"
    # Present ONLY for resident (persistent) sub-agents — the loop injects a
    # ``message_handler`` and force-adds this tool for them, and strips it
    # everywhere else (the main agent talks to agents via the ``agent`` tool).
    # 이 정책은 아래 두 선언에서 파생된다 (T3 선언화).
    requires_handler = "message_handler"
    force_mount = True
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


class AnswerTool(Tool):
    name = "answer"
    # ``MessageTool`` 과 같은 선언 (T3): 질문 포트가 주입된 루프에만 붙고
    # 그 외에서는 목록에서 제거된다. 포트는 상주 에이전트와 main 이 받는다
    # — main 도 자기 앞으로 온 질문에 답해야 한다.
    requires_handler = "questions"
    force_mount = True
    description = (
        "Answer a question that was addressed to you. Its id arrived with the "
        "question (e.g. `[question q-1a2b from agt-x9]`). The asker is NOT "
        "blocked and kept working, but it cannot finish the part that depends "
        "on your answer. If you genuinely cannot answer, say so with `answer` "
        "rather than ignoring it — silence only stalls them."
    )
    parameters: ClassVar[dict] = {
        "type": "object",
        "properties": {
            "id": {
                "type": "string",
                "description": "The question id, e.g. 'q-1a2b'.",
            },
            "text": {
                "type": "string",
                "description": "Your answer.",
            },
        },
        "required": ["id", "text"],
    }

    def _run(self, args: dict, *, ctx=None) -> ToolResult:
        # Placeholder — the loop intercepts `answer` before dispatch and
        # routes it through the injected question port.
        return ToolResult(
            True, output=f"(answer to {args.get('id', '?')}: intercepted by loop)"
        )


class RunSkillTool(Tool):
    name = "run_skill"
    terminal = True  # 턴 종결 (complete 와 동형 — flush 후 디스패치)
    depth_gated = True  # 결합 깊이 상한에서 제거 (스킬도 depth 계수)
    oversized_retry_hint = (
        "run the skill against a smaller target, or ask it (via arguments) for "
        "a summary instead of full output."
    )
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
