"""agent 도구 스키마 (docs/agent-unification/DESIGN.md §3, docs/agent-engine/DESIGN.md §1).

실행은 :func:`agent_cli.subagent.agents_live.tool_agent` — 루프(tool_bridge)가 인터셉트해
호출자·배선을 주입한다. 이 모듈은 스키마·의미론 검증(C7 ``validate``)만 소유한다.
모든 루프가 같은 표면이다(v10.141.0) — 서브루프용 축소 스키마는 없다. 에이전트에게 일을
주고받는 말은 `message`/`reply` 한 쌍이고, 이 도구는 에이전트를 띄우고(run·spawn) 다룬다.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from agent_cli.constants import has_words
from agent_cli.tools.base import Tool
from agent_cli.tools.result import ToolResult


@dataclass(frozen=True)
class AgentMode:
    """agent 도구 모드 1종의 선언 (모드 테이블화 — 리뷰 §4.4 T3).

    스키마 enum·validate 필수 필드가 이 테이블에서 파생되고, 실행은 같은 이름의
    `agents_live._MODE_HANDLERS` 항목이다(정합은 테스트가 고정).

    - ``required``: 모드별 필수 필드 (C7 의미론 검증).
    """

    required: tuple[str, ...] = ()


# 삽입 순서 = 스키마 enum 노출 순서 (프롬프트 렌더 안정성).
AGENT_MODES: dict[str, AgentMode] = {
    "run": AgentMode(required=("task",)),
    "spawn": AgentMode(),
    "status": AgentMode(),
    "resume": AgentMode(required=("key",)),
    "kill": AgentMode(required=("key",)),
}


class AgentTool(Tool):
    name = "agent"
    #: 지연 (v10.110.0, docs/tool-help/DESIGN.md D1) — 프롬프트엔 이 한 줄과 인자 뼈대,
    #: 전문은 `tool_help`.
    deferred = True
    summary = "Hand a task to another agent that works in its own context; the profiles are listed under `## Agent Profiles`."
    depth_gated = True  # 결합 깊이 상한에서 제거 (T3 선언화 — run 도 불가)
    # run 의 설명 (v9.24.1) — 초점은 동시성이 아니라 **분리된 컨텍스트**: 종전 "fan out
    # … in PARALLEL" 은 속도로 읽혔고, 로컬 모델 서버 하나에서 run 둘은 처리량을 반씩
    # 나눠(실측 11+11 tok/s vs 단독 20) 빨라지지 않았다 (Harbor v4 extract-elf). context 는
    # none(기본)·fork 둘 다 가능 — "empty" 로 단정하지 않는다. v10.141.0: 결과는 회신이다 —
    # run 은 바로 돌아오고, 기다리는 법은 꼬리(`## Open Items`·`## Live Agents`)가 말한다.
    RUN_DESCRIPTION = (
        '`mode` "run" gives one task to a new sub-agent that ends after it replies, '
        "and you get back only its reply: whatever it read stays in its window, both "
        "handoffs drop detail, and its mistakes return in the same confident tone as "
        "its findings. Delegate when answering would mean reading across many "
        "files and you want the conclusion, not the file dumps. Do it "
        "yourself when it is a handful of calls or a lookup whose target you "
        'already know; when in doubt, do it yourself. Several "run" calls in one '
        "turn are for independent parts that would each flood your context — "
        "not for speed: on a single local model server they share throughput."
    )
    description = (
        "Start and manage sub-agents, one-shot or persistent. "
        + RUN_DESCRIPTION
        + ' Spawn (`mode` "spawn") only when the same area will take several '
        "rounds of questions — an agent that has studied one area answers "
        "follow-ups from full detail; send it work with `message`. One agent per "
        "area (disjoint files/subsystems). Once you have handed work to an agent, "
        "do not also do it yourself; how to wait for its reply is in the `## Live "
        "Agents` section at the end of the prompt."
    )
    # v10.85.0 (agent 검토 PR B, T2): "기다리는 법" 네 문장(finish what is yours, then
    # `wait` · 깨어난다 · 회신을 예측하지 않는다 · 의존 요청은 읽은 뒤에)은 설명이
    # 아니라 상태 블록의 말이라 꼬리 `## Live Agents` 머리글로 옮겼다(사용자 결정,
    # 삭제가 아니라 이동 — v10.73.0 자리 원칙). 에이전트가 하나도 없을 때까지 모든
    # 프롬프트에 실리던 것이, 에이전트가 있을 때만 보인다.
    parameters: ClassVar[dict] = {
        "type": "object",
        "properties": {
            "mode": {
                "type": "string",
                "enum": list(AGENT_MODES),
                "description": (
                    '"run": one task for a new agent that ends after its reply — '
                    "returns at once; the reply comes to you later as a message. "
                    '"spawn": create a persistent agent that keeps its context '
                    "between requests (returns its key; send it work with "
                    '`message`). "status": list live agents and their state. '
                    '"resume": bring a dead agent back with its full prior '
                    'context. "kill": terminate one.'
                ),
            },
            "profile": {
                "type": "string",
                "description": (
                    '"run"/"spawn": profile from .agent-cli/agents/{profile}.md '
                    "(loaded into the sub-agent's system prompt; omit for a "
                    "generalist). The same profile may be spawned more than "
                    "once — give each instance a distinct `name`."
                ),
            },
            "name": {
                "type": "string",
                "description": (
                    '"spawn": optional instance label to tell same-profile agents '
                    'apart (e.g. two coders as "ui" and "api"). Display only — '
                    "always address agents by their `key`."
                ),
            },
            "task": {
                "type": "string",
                "description": (
                    '"run": the task (required); "spawn"/"resume": optional first '
                    "request. Brief it like a peer: the goal, what you already ruled out, and "
                    "which files to read — point at them, do not retype them. "
                    "Give it the code, not your conclusion: an agent handed "
                    "your hypothesis tends to return it confirmed."
                ),
            },
            "instructions": {
                "type": "string",
                "description": (
                    '"run"/"spawn": inline role text that becomes part '
                    "of the sub-agent's system prompt (for a spawned agent: "
                    "its whole life, survives resume). Use alone for an "
                    "ad-hoc specialist, or with `profile` to append "
                    "session-specific directions."
                ),
            },
            "key": {
                "type": "string",
                "description": (
                    'agent key returned by "run"/"spawn" (required for "resume"/'
                    '"kill"; optional filter for "status")'
                ),
            },
            "tools": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    '"run"/"spawn": tools the sub-agent may use. Omit for the '
                    "profile's set; without a profile the sub-agent has every "
                    "tool. With a profile it can only narrow that set."
                ),
            },
            "context": {
                "type": "string",
                "enum": ["none", "fork"],
                "description": (
                    '"run"/"spawn": "none" (default) — the agent starts fresh and '
                    'knows only the task text; "fork" — it gets a copy of your '
                    "conversation so far."
                ),
            },
        },
        "required": ["mode"],
    }

    def validate(self, args: dict) -> str | None:
        # 모드 유효성·필수 필드는 AGENT_MODES 테이블에서 파생 (C7 의미론
        # 검증 훅 — shape 는 중앙 1~5단계).
        mode = args.get("mode")
        if mode not in AGENT_MODES:
            modes = ", ".join(f'"{m}"' for m in AGENT_MODES)
            return f'unknown `mode` "{mode}" — must be one of {modes}'
        if "message" in args:
            # 통일 어휘 (v8.0.0): 지시는 모든 모드에서 ``task`` 하나다. 과거
            # 한 모드만 ``message`` 를 써서, 모델이 spawn/resume 에도 그 습관을
            # 이어가면 **조용히 유실**됐다(라이브 재현). 별칭 수용(v7.29.1)은
            # 이중 어휘를 영구화하므로 철회 — 정밀 에러를 A5 로 되먹여 다음
            # 턴에 자가 교정시킨다(도구-입력 의미론은 엄격, wire 문법만 관용).
            return (
                "field `message` is not accepted — put the instruction in "
                "`task` (every agent mode uses `task`)"
            )
        for field in AGENT_MODES[mode].required:
            value = args.get(field)
            if not isinstance(value, str) or not value.strip():
                return f'`mode` "{mode}" requires a non-empty `{field}`'
        # v10.1.2: 구두점뿐인 지시(실측 `>`)는 빈 지시다 — 받는 에이전트는 내
        # 컨텍스트를 못 보므로 뜻을 찾아 헤맨다. 어느 모드든 task 가 있으면 검사.
        task = args.get("task")
        if isinstance(task, str) and task.strip() and not has_words(task):
            return (
                f"`task` has no words ({task.strip()[:20]!r}) — write the full "
                "instruction in words; the agent cannot see your context"
            )
        return None

    def touched_paths(self, action_input: dict) -> list[str]:
        # 파일 경로가 없는 도구 — compaction file-list 에 스폰/요청 흔적 마커.
        args = self.strip_prefix(action_input)
        target = args.get("key") or args.get("profile")
        return [f"<agent:{target}>"] if isinstance(target, str) and target else []

    def summary_arg(self, action_input: dict) -> str:
        args = self.strip_prefix(action_input)
        mode = args.get("mode", "")
        target = args.get("key") or args.get("profile") or ""
        return f"{mode} {target}".strip()

    def wrap_single_op(self, flat: dict) -> dict:
        return flat

    def _run(self, args: dict, *, ctx=None) -> ToolResult:
        # 루프가 인터셉트 (registry/provider 필요) — 직접/테스트 호출자용.
        return ToolResult(True, output="(agent: intercepted by loop)")
