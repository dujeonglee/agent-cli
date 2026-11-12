"""사용자 명령 표 하나 (v10.127.1) — 웹 ``/help`` 와 ``run "/help"`` 가 여기서 나온다.

종전엔 웹만 도움말 문자열을 손으로 들고 있었고 ``run "/help"`` 는 ``Unknown command: /help``
였다. 한 줄 = 이름·설명·되는 표면. 웹 전용(``/compact``)은 run 도움말에 "— web only" 로 적는다.
표를 고치면 두 도움말이 같이 바뀐다 — 웹 문자열은 ``tests/test_command_table.py`` 가 고정한다.
"""

from __future__ import annotations

from dataclasses import dataclass

WEB = "web"
RUN = "run"


@dataclass(frozen=True)
class Command:
    usage: str
    description: str
    surfaces: frozenset[str]


COMMANDS: tuple[Command, ...] = (
    Command("/help", "Show this help", frozenset({WEB, RUN})),
    Command(
        "/compact", "Compact context now (summarise oldest half)", frozenset({WEB})
    ),
    Command(
        "/sh <command>",
        "Run a shell command directly (LLM bypass)",
        frozenset({WEB, RUN}),
    ),
    Command("/skills", "List available skills", frozenset({WEB, RUN})),
    Command("/<skill> <args>", "Invoke a skill directly", frozenset({WEB, RUN})),
    Command("@agents", "List available agents", frozenset({WEB, RUN})),
    Command("@<agent> <task>", "Delegate a task to an agent", frozenset({WEB, RUN})),
)

_HEADER = {
    WEB: "Web mode commands:",
    RUN: 'Commands for agent-cli run "<command>":',
}
_FOOTER = {
    WEB: "Any other input goes to the LLM as a chat turn.",
    RUN: "Any other input goes to the LLM as the task.",
}


def help_text(surface: str) -> str:
    """그 표면의 도움말 — 웹 전용 명령은 run 쪽에 "— web only" 로 남긴다."""
    lines = [_HEADER[surface]]
    for cmd in COMMANDS:
        note = ""
        if surface not in cmd.surfaces:
            if surface == WEB:
                continue
            note = " — web only"
        lines.append(f"  {cmd.usage.ljust(25)}{cmd.description}{note}")
    return "\n".join([*lines, "", _FOOTER[surface]])
