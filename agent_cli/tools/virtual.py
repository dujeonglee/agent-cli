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
        "Ask a question and KEEP WORKING — you are NOT blocked. By default the "
        "question goes to whoever gave you this task (main or a person); set "
        '`to: "user"` to ask the person directly. The answer arrives later as '
        "a NEW message and you continue from there. Carry on with anything that "
        "does not depend on it. If nothing else can proceed, `complete`."
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
    # 상주 에이전트용 스키마 (v9.20.0). ``to`` 는 상주에게만 뜻이 있다 —
    # main 의 ``ask`` 는 언제나 사람에게 가므로 공용 스키마에 실으면 main
    # 이 무의미한 필드를 본다. 프롬프트 렌더가 ``parameter_overrides`` 로
    # 이 스키마를 고른다(``description_overrides`` 와 같은 자리).
    #
    # 왜 필요한가 — 실측(프로브 1790070684): main 이 "사용자한테 질문해 봐"
    # 라고 시킨 에이전트의 ``ask`` 가 **main 에게** 갔다(주소 = 원 요청자 =
    # main). 트레이는 사람 주소 질문만 보이므로 사람은 아무것도 못 봤고,
    # main 은 그 질문에 사용자 대신 답을 **지어냈다**. 종전 파라미터 설명
    # "The question to ask the user." 는 그 상황에서 거짓말이었다.
    RESIDENT_PARAMETERS: ClassVar[dict] = {
        "type": "object",
        "properties": {
            "question": {"type": "string", "description": "The question."},
            "to": {
                "type": "string",
                "enum": ["requester", "user"],
                "description": (
                    "Who receives it. `requester` (default): whoever gave you "
                    "this task — main or a person. `user`: the person directly; "
                    "it appears in their question tray and their reply comes "
                    "back to you. Use `user` whenever a human must decide."
                ),
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
        "Message another running agent (a peer, or `main`) when you NEED "
        "something from them — a decision, an answer, their next move. They "
        "owe you a reply, which comes back to YOU as a NEW message; you are "
        "NOT blocked. Sent to whoever requested your current work, it also "
        "counts as your reply to them. To report a result with nothing "
        "expected back, use `reply`. `complete` alone reports to no one. See "
        "`## Live Agents` for who is running. Never send acknowledgements."
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


class ReplyTool(Tool):
    """상주 에이전트 전용 (v9.21.0). 이 런을 시킨 쪽에게 답한다 — `message`
    와 짝이다(`ask` ↔ `answer` 처럼). `message` 는 상대에게 회신을 **빚지우고**,
    `reply` 는 내 빚을 **갚는다**(돌려받을 것 없음). 요청자는 하네스가 아니까
    `to` 가 없다. 빚진 게 없는 런(회신·질문·독촉으로 시작)에서 부르면 거부
    — "확인했습니다" 류의 ack 가 정확히 거기서 새어 나온다(사용자 결정)."""

    name = "reply"
    requires_handler = "message_handler"  # 상주에만 — main 은 요청자가 없다
    force_mount = True
    description = (
        "Reply to whoever requested your current work — your result, your "
        "answer, your report. Nothing is expected back, so this ends the "
        "exchange cleanly. Use `message` instead when you NEED something from "
        "them (a decision, their next move). `complete` alone reports to no "
        "one: reply BEFORE you complete. Refused when nothing is owed."
    )
    parameters: ClassVar[dict] = {
        "type": "object",
        "properties": {
            "text": {"type": "string", "description": "Your reply."},
        },
        "required": ["text"],
    }

    def _run(self, args: dict, *, ctx=None) -> ToolResult:
        # Placeholder — the loop intercepts `reply` before dispatch.
        return ToolResult(True, output=str(args.get("text", "")) or "(reply)")


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
