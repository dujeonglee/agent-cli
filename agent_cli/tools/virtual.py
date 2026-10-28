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
    # result 가 답으로 전달되는 유일한 글이다 — 호출 밖에 쓴 산문은 화면에 접혀
    # 보일 뿐 답이 아니고, 서브에이전트의 호출자는 result 만 받는다. 그래서 결과와
    # 그것이 기대는 근거를 함께 쓰게 한다 — 추론 과정 전체는 아니다(사용자 결정
    # 2026-10-26). 근거는 고정 틀 두 줄(Checked / Not checked): 원칙 문장 판은
    # GLM·35B 40런에서 "확인 안 한 것" 을 한 번도 쓰지 않았다(35B 는 원칙보다
    # 형식을 따른다 — monitor 평평한 인자 A/B 와 같은 교훈).
    description = (
        "Call this tool when the task is done — always the LAST call. `result` "
        "is the only text delivered as your answer — to the user, or to the "
        "agent that called you; what you wrote outside it is not. Put the "
        "answer first. When it rests on work you did, end with two lines:\n"
        "Checked: <what you ran or inspected, and what it showed>\n"
        "Not checked: <what you inferred from reading rather than running, or "
        "did not test — 'nothing' if so>\n"
        "A reply with nothing to check has neither line. "
        "When `## Open Items` lists items closed by "
        "`complete`, `answers` is REQUIRED: list the ids your result covers. "
        "Ids you leave out stay open. Not for waiting — to pause until a job, "
        "monitor or agent reports back, call `wait`."
    )
    parameters: ClassVar[dict] = {
        "type": "object",
        "properties": {
            "result": {
                "type": "string",
                "description": "The answer, then the Checked / Not checked lines",
            },
            "answers": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Ids from `## Open Items` (`u-…` user requests, `n-…` mail) "
                    "that this result covers."
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


class WaitTool(Tool):
    """``wait`` (v10.65.0, docs/wait/DESIGN.md) — "지금은 더 할 게 없다, 깨워 달라".

    종전엔 작업·모니터·에이전트 회신을 기다릴 때도 ``complete`` 를 쓰라고 했다
    (v10.47.1·에이전트 status 힌트). 그런데 장부(P3)는 ``complete`` 를 "열린 항목을
    닫는 행위" 로 보고 ``answers`` 없는 complete 를 8번까지 되돌린다 — 벤치 실측
    (video, 2026-10-22): 설치 작업을 기다리는 같은 complete 를 9번 보내고 예산이
    끝나서야 런이 끝났다. 동사 하나에 뜻이 둘이었다. ``wait`` 는 아무것도 닫지
    않고 런을 끝낸다; 열린 항목은 다음 깨우기 런으로 넘어간다. 깨울 것이 없으면
    거부한다(막히면 wait 로 도피하는 길을 막는다). 디스패치가 가로챈다
    (`dispatch._op_wait`) — ``_run`` 은 안 불린다."""

    name = "wait"
    terminal = True
    #: 깨울 길이 있는 루프에만 (`LoopPorts.wakeable`) — 스킬·일회성 런은 결과가
    #: 호출자에게 돌아가므로 "기다린다" 가 성립하지 않는다.
    requires_handler = "wake"
    description = (
        "Nothing more to do until something you started reports back — a "
        "background job, a monitor, an agent you asked. Ends this turn WITHOUT "
        "closing any open item; the report wakes you with the items still "
        "open. Refused when nothing will wake you: then keep working, or call "
        "`complete`."
    )
    parameters: ClassVar[dict] = {
        "type": "object",
        "properties": {
            "reason": {
                "type": "string",
                "description": "One line: what you are waiting for (shown to the user)",
            },
        },
        "required": [],
    }

    def _run(self, args: dict, *, ctx=None) -> ToolResult:
        return ToolResult(True, output=str(args.get("reason") or "waiting"))


class AskTool(Tool):
    name = "ask"
    requires_handler = "ctx"  # 비대화형 루프(ctx 없음)에선 목록에서 제거
    # Compact gate only. The full ask-vs-complete decision tree (examples,
    # rule of thumb) lives in the inline guide ``_ASK_INLINE`` in
    # ``prompts/system_prompt.py``, which is always rendered right after
    # this description — keeping the prose in one place avoids the two
    # surfaces teaching the same distinction back to back.
    description = (
        "Ask the user ONE question and WAIT for their reply. One question per call — to"
        " ask several, make several `ask` calls in one turn (each is answered in turn)."
        " Use only when you cannot proceed without specific input; otherwise end with "
        "`complete`."
    )
    # 상주 에이전트용 (docs/agent-ask/DESIGN.md §3.1). 거기서 ``ask`` 는
    # **막지 않는다** — 질문을 사람의 트레이에 올리고 즉시 돌아온다. 마지막
    # 문장이 중요하다: ask 는 보통 "막혔다" 는 뜻이라 "계속하라" 고만 하면 작은
    # 모델이 추측으로 메우고 끝낸다. v10.77.0 (B): 주소는 언제나 사람 — 에이전트·
    # main 에게서 무언가 필요하면 `message` 다(요청 → 상대가 `reply` 를 빚진다).
    RESIDENT_DESCRIPTION = (
        "Ask the person ONE question and KEEP WORKING — you are NOT blocked. It "
        "goes to their question tray; the answer arrives later as a NEW message "
        "and you continue from there. Carry on with anything that does not depend "
        "on it. If nothing else can proceed, `complete`. To get something from "
        "main or another agent, send it a `message` — its `reply` comes back to you."
    )
    parameters: ClassVar[dict] = {
        "type": "object",
        "properties": {
            "question": {
                "type": "string",
                "minLength": 1,
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
        'Message another running agent (a peer, or "main"). By default it is a REQUEST:'
        " you NEED something from them — a decision, an answer, their next move. The "
        "result names its id (e.g. r-17); they owe you a `reply` for it, which comes "
        "back to YOU as a NEW message marked with that id, and you are NOT blocked. "
        "With `expects_reply` false it is a NOTICE: a status update, a hand-off note, "
        "a goodbye — nothing is owed back and the receiver must not acknowledge it. "
        "To answer a request you received, use `reply`. `complete` alone reports to "
        "no one. See `## Live Agents` for who is running. Never send thanks or "
        "acknowledgements."
    )
    parameters: ClassVar[dict] = {
        "type": "object",
        "properties": {
            "to": {
                "type": "string",
                "minLength": 1,
                "description": 'Target agent: its key or name (from `## Live Agents`), or "main".',
            },
            "text": {
                "type": "string",
                "minLength": 1,
                "description": "The message to send.",
            },
            "expects_reply": {
                "type": "boolean",
                "description": (
                    "true (default): a request — the receiver owes you a reply. "
                    "false: a notice — nothing owed, no acknowledgement wanted."
                ),
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
    """빚진 회신을 갚는다 (v9.21.0) — `message` 와 짝이다. `message` 는 상대에게
    회신을 **빚지우고**, `reply` 는 내 빚을 **갚는다**(돌려받을 것 없음). id 가
    전역에서 유일해 그것만으로 상대가 정해진다 — v10.77.0 에서 `to` 를 지웠다.
    빚진 게 없는 런(회신·통지·답으로 시작)에서 부르면 거부 — "확인했습니다"
    류의 ack 가 정확히 거기서 새어 나온다(사용자 결정)."""

    name = "reply"
    # v10.56.0 (장부 P3 §11.3): main 에도 붙는다 — 빚을 갚는 도구는 루프와 무관하게
    # 하나다. 종전엔 상주 전용(``message_handler``)이었고 main 은 `agent request`
    # 로 "가장 오래된 빚" 을 갚았다. 질문 포트가 있는 루프(main·상주)에 붙는다.
    requires_handler = "questions"
    force_mount = True
    description = (
        "Reply to a request someone is waiting on — an agent's `message` that expects "
        "an answer, or the request you were given. `id` is the request's `r-…` from "
        "`## Open Items`; it alone says where the reply goes, and each request gets "
        "its own reply. Nothing is expected back, so this ends that exchange cleanly. "
        'Use `message` (or `agent` with `mode` "request") instead when you NEED '
        "something from them. `complete` alone reports to no one: reply BEFORE you "
        "complete. Refused when nothing is owed."
    )
    parameters: ClassVar[dict] = {
        "type": "object",
        "properties": {
            "id": {
                "type": "string",
                "minLength": 1,
                "description": 'The request id from `## Open Items`, e.g. "r-12".',
            },
            "text": {"type": "string", "minLength": 1, "description": "Your reply."},
        },
        "required": ["id", "text"],
    }

    def _run(self, args: dict, *, ctx=None) -> ToolResult:
        # Placeholder — the loop intercepts `reply` before dispatch.
        return ToolResult(True, output=str(args.get("text", "")) or "(reply)")


class RunSkillTool(Tool):
    name = "run_skill"
    terminal = True  # 턴 종결 (complete 와 동형 — flush 후 디스패치)
    depth_gated = True  # 결합 깊이 상한에서 제거 (스킬도 depth 계수)
    oversized_retry_hint = (
        "run the skill against a smaller target, or ask it (via arguments) for "
        "a summary instead of full output."
    )
    description = (
        "Run a registered skill by name — a prompt-based workflow listed under "
        "Available Skills."
    )
    parameters: ClassVar[dict] = {
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "minLength": 1,
                "description": "Skill name, exactly as listed under Available Skills",
            },
            "arguments": {
                "type": "string",
                "description": "Arguments to pass to the skill (e.g. a file path)",
            },
        },
        "required": ["name"],
    }

    def _run(self, args: dict, *, ctx=None) -> ToolResult:
        return ToolResult(True, output="(run_skill: intercepted by loop)")
