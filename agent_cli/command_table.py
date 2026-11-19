"""사용자 명령 표 하나 (v10.127.1) — 웹 ``/help`` 와 ``run "/help"`` 가 여기서 나온다.

종전엔 웹만 도움말 문자열을 손으로 들고 있었고 ``run "/help"`` 는 ``Unknown command: /help``
였다. 한 줄 = 이름·설명·되는 표면. 웹 전용(``/compact``)은 run 도움말에 "— web only" 로 적는다.
표를 고치면 두 도움말이 같이 바뀐다 — 웹 문자열은 ``tests/test_command_table.py`` 가 고정한다.

사람의 말이 공용 명령인가(`command_kind`, `looks_like_slash_command`)도 여기 있다 — 실행 없이 읽기만 하는 판정이라
표면(웹·run)과 디스패치(`main.try_dispatch_agent_or_skill`)가 같이 쓴다.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

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
    Command(
        "/stop <agent>|all",
        "Stop an agent's run (all: every working agent)",
        frozenset({WEB, RUN}),
    ),
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


_SLASH_CMD_NAME = re.compile(r"^[A-Za-z0-9_-]+$")


def looks_like_slash_command(message: str) -> bool:
    """``/...`` 메시지가 **명령**인지 — 경로/일반 텍스트 오인 방지 (v4.51.1).

    이전엔 ``startswith("/")`` 만으로 skill dispatch 로 보내서
    ``/Users/me/proj 분석해줘`` 같은 경로-선두 메시지가 SKILL_NOT_FOUND
    로 먹혔다. 판별 규칙(보수적 — 명령이라고 확신할 때만 True):

    1. 첫 토큰에 두 번째 ``/`` 가 있으면 경로 (``/tmp/a.py``) → False
    2. 첫 토큰이 파일시스템에 실존하면 경로 (``/etc``, ``/Users``) → False
    3. 이름이 명령 문법(영숫자·-_)이 아니면 → False
    4. 그 외 → True — unknown 이름은 기존대로 not-found 안내(오타 안전망)

    ``@agent`` 라우팅과 web 의 정확-매치 명령(/help·/sh·/compact)은 무관.
    """
    first = message.split(maxsplit=1)[0]
    body = first[1:]
    if "/" in body:  # 규칙 1
        return False
    if not _SLASH_CMD_NAME.match(body):  # 규칙 3 (빈 문자열 포함)
        return False
    try:
        if Path(first).exists():  # 규칙 2 — /etc, /Users 등 한 단어 경로
            return False
    except OSError:
        pass
    return True


def command_kind(message: str) -> str | None:
    """사람의 말이 공용 명령인가 — 그 갈래(``"agent"``·``"stop"``·``"skill"``) 또는 None. 읽기만 한다(실행·출력
    없음). `try_dispatch_agent_or_skill` 이 갈래를 고르는 규칙 그대로다 — 표면의 `is_command` 가 실행 전에 이것을
    묻는다(제자리 대기 중 명령은 꺼내기 전에 판정해야 한다)."""
    if message.startswith("@"):
        return "agent"
    head = message.split(maxsplit=1)
    if head and head[0] == "/stop":
        return "stop"
    if message.startswith("/") and looks_like_slash_command(message):
        return "skill"
    return None


#: 이미지를 받지 않는 명령에 이미지가 딸려 왔을 때의 한 줄 (v10.149.3) — 명령은 그대로 실행된다.
IMAGES_NOT_TAKEN = "This command does not take images — the attachment was not sent"


def is_web_command(message: str) -> bool:
    """웹 전용 명령(``/help``·``/compact``·``/sh``)인가 — 웹 표면이 공용 디스패치보다 먼저 받는 글 그대로. 읽기만 한다."""
    return message in ("/help", "/compact") or message.startswith(("/compact ", "/sh"))


def command_takes_images(message: str) -> bool:
    """명령이 사람의 말에 딸린 이미지를 그 말과 함께 보내는가 — 말이 에이전트·스킬 런의 시작 메시지가 되는 갈래
    (``@agt-<key> 글``·``@<profile>-spawn 과제``·``@<profile> 과제``·``/<skill> …``)만 True. 목록·상태·``/stop``·웹
    전용 명령은 말이 갈 곳이 없어 False. 명령인 글에만 묻는다. 읽기만 한다."""
    if is_web_command(message):
        return False
    kind = command_kind(message)
    parts = message.split(maxsplit=1)
    if kind == "skill":
        return parts[0] != "/skills"
    if kind == "agent":
        name = parts[0][1:]
        return bool(name) and name != "agents" and len(parts) > 1
    return False
