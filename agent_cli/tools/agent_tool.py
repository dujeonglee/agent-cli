"""agent 도구 스키마 (docs/agent-unification/DESIGN.md §3).

실행은 :func:`agent_cli.subagent.agents_live.tool_agent` — delegate 처럼
루프(tool_bridge)가 인터셉트해 registry/provider 배선을 주입한다. 이
모듈은 스키마·의미론 검증(C7 ``validate``)·over-cap 표면만 소유한다.
레지스트리가 없는 루프(서브에이전트·headless)에서는 상주 모드가
디스패치에서 거부되고 프롬프트는 SUBLOOP_DESCRIPTION(run 만 문서화)으로
렌더된다 (§3.2 모드 축소 노출 — 도구 인스턴스는 하나).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar

from agent_cli.tools.base import (
    Tool,
    default_oversized_nudge,
    on_disk_oversized_nudge,
)
from agent_cli.tools.result import ToolResult


@dataclass(frozen=True)
class AgentMode:
    """agent 도구 모드 1종의 선언 (모드 테이블화 — 리뷰 §4.4 T3).

    스키마 enum·validate 필수 필드·병렬 배치 합류·tool_bridge 라우팅이
    전부 이 테이블에서 파생된다 — 종전엔 5곳(enum/validate 재나열/
    parallel_batchable/브리지 분기/agents_live if-체인)에 각자 나열돼
    새 모드 추가 시 동기 수정 누락이 조용한 폴스루로 새던 구조.

    - ``required``: 모드별 필수 필드 (C7 의미론 검증).
    - ``engine``: "oneshot"=일회성 병렬 엔진(tool_bridge 가 tool_delegate
      로 라우팅) / "registry"=상주 레지스트리(agents_live.tool_agent 의
      핸들러 테이블로 라우팅).
    - ``batchable``: 같은 턴 연속 op 의 병렬 배치 합류 여부 (설계 §3.6 —
      run 만; 상주 모드는 즉시 반환이라 병렬 이득 없음).
    """

    required: tuple[str, ...] = ()
    engine: str = "registry"
    batchable: bool = False


# 삽입 순서 = 스키마 enum 노출 순서 (프롬프트 렌더 안정성).
AGENT_MODES: dict[str, AgentMode] = {
    "run": AgentMode(required=("task",), engine="oneshot", batchable=True),
    "spawn": AgentMode(),
    "request": AgentMode(required=("key", "task")),
    "status": AgentMode(),
    "resume": AgentMode(required=("key",)),
    "kill": AgentMode(required=("key",)),
}

# 상주(registry) 모드 이름들 — 에러 문구·핸들러 커버리지 검증용 파생.
REGISTRY_MODES: tuple[str, ...] = tuple(
    m for m, spec in AGENT_MODES.items() if spec.engine == "registry"
)


class AgentTool(Tool):
    name = "agent"
    depth_gated = True  # 결합 깊이 상한에서 제거 (T3 선언화 — run 도 불가)
    # 서브루프(레지스트리 없는 루프)용 축소 설명 — run 만 문서화 (설계
    # §3.2 모드 축소 노출: 도구 인스턴스는 하나, 프롬프트 렌더만 분기).
    SUBLOOP_DESCRIPTION = (
        "Run ONE task in a fresh sub-agent and get the result back in this "
        'turn (mode:"run" only here). Emit several run ops in the same '
        "turn to fan out independent tasks in PARALLEL. Optional profile/"
        "instructions give the sub-agent a role."
    )
    description = (
        'Work with sub-agents, one-shot or persistent. mode:"run" executes '
        "ONE task in a fresh sub-agent and returns its result in this turn — "
        "emit several run ops in the same turn to fan out independent tasks "
        'in PARALLEL. mode:"spawn" creates a PERSISTENT agent that keeps '
        'its context between requests: send follow-ups with mode:"request" '
        "any time and its replies are delivered to you automatically as "
        "observations (no polling). Use run for independent one-shot tasks; "
        "use spawn/request for iterative work. Spawning scales the team's "
        "working memory: each agent has its OWN full context window, so "
        "splitting a large job across N specialists gives N windows of "
        "combined context while yours stays clean — agents absorb the raw "
        "exploration (file contents, search results, dead ends) and send "
        "back distilled answers. An agent that has studied one area answers "
        "follow-ups from full detail, more precisely than re-deriving from "
        "your compacted history. Prefer spawn when work is large, "
        "multi-part, or will need follow-ups; give each agent ONE area "
        "(disjoint files/subsystems). For a small one-off lookup, run — or "
        "doing it yourself — is cheaper than coordinating an agent."
    )
    parameters: ClassVar[dict] = {
        "type": "object",
        "properties": {
            "mode": {
                "type": "string",
                "enum": list(AGENT_MODES),
                "description": (
                    "run: execute ONE task in a fresh one-shot sub-agent "
                    "(blocking — result returns in this turn; several run ops "
                    "in one turn execute in parallel). "
                    "spawn: create a PERSISTENT agent (returns its key). "
                    "request: send it a message — returns immediately; the "
                    "reply is DELIVERED to you automatically when ready "
                    "(never poll, never wait — keep working or complete). "
                    "status: list live agents and their state. "
                    "resume: bring a DEAD agent back to life with its full "
                    "prior context (it remembers everything). "
                    "kill: terminate one."
                ),
            },
            "profile": {
                "type": "string",
                "description": (
                    "run/spawn: profile from .agent-cli/agents/{profile}.md "
                    "(loaded into the sub-agent's system prompt; omit for a "
                    "generalist). The SAME profile may be spawned multiple times "
                    "for parallel independent workstreams — give each instance "
                    "a distinct `name`."
                ),
            },
            "name": {
                "type": "string",
                "description": (
                    "spawn: optional instance label to tell same-profile agents "
                    "apart (e.g. two coders as 'ui' and 'api'). Display only — "
                    "always address agents by their key."
                ),
            },
            "task": {
                "type": "string",
                "description": (
                    "the instruction for the agent — run: the task to execute "
                    "(required); request: the message to deliver (required); "
                    "spawn/resume: optional initial request queued right away"
                ),
            },
            "instructions": {
                "type": "string",
                "description": (
                    "run/spawn: inline role text (instant-agent) — becomes part "
                    "of the sub-agent's system prompt (for a spawned agent: "
                    "its WHOLE life, survives resume). Use alone for an "
                    "ad-hoc specialist, or with `profile` to append "
                    "session-specific directions."
                ),
            },
            "key": {
                "type": "string",
                "description": (
                    "agent key returned by spawn (required for request/"
                    "resume/kill; optional filter for status)"
                ),
            },
            "tools": {
                "type": "array",
                "items": {"type": "string"},
                "description": "run/spawn: allowed tools (omit for the default set)",
            },
            "context": {
                "type": "string",
                "enum": ["none", "fork"],
                "description": (
                    "run/spawn: none (fresh context) or fork (copy of the "
                    "current conversation history)"
                ),
            },
        },
        "required": ["mode"],
    }

    # run fan-out 용 — 배치 합류 여부는 parallel_batchable(mode-aware)이 결정.
    parallel_safe = True

    def validate(self, args: dict) -> str | None:
        # 모드 유효성·필수 필드는 AGENT_MODES 테이블에서 파생 (C7 의미론
        # 검증 훅 — shape 는 중앙 1~5단계).
        mode = args.get("mode")
        if mode not in AGENT_MODES:
            return f"unknown mode '{mode}' — must be one of {', '.join(AGENT_MODES)}"
        if "message" in args:
            # 통일 어휘 (v8.0.0): 지시는 모든 모드에서 ``task`` 하나다. 과거
            # request 만 ``message`` 를 써서, 모델이 spawn/resume 에도 그 습관을
            # 이어가면 **조용히 유실**됐다(라이브 재현). 별칭 수용(v7.29.1)은
            # 이중 어휘를 영구화하므로 철회 — 정밀 에러를 A5 로 되먹여 다음
            # 턴에 자가 교정시킨다(도구-입력 의미론은 엄격, wire 문법만 관용).
            return (
                "field 'message' is not accepted — put the instruction in "
                "'task' (every agent mode uses 'task')"
            )
        for field in AGENT_MODES[mode].required:
            value = args.get(field)
            if not isinstance(value, str) or not value.strip():
                return f"mode '{mode}' requires a non-empty '{field}'"
        return None

    def parallel_batchable(self, action_input: dict) -> bool:
        # mode-aware 배칭 — AGENT_MODES.batchable 파생 (설계 §3.6: run 만).
        args = self.strip_prefix(action_input) if isinstance(action_input, dict) else {}
        spec = AGENT_MODES.get(args.get("mode"))
        return spec is not None and spec.batchable

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

    def render_oversized(self, result, args, *, body, tokens, ctx) -> str:
        """run 모드의 큰 결과: 일회성 엔진이 이미 전문을
        ``<run_dir>/result.md``(상대경로 = ``result.artifact``)에 영속했으므로
        그 파일을 가리키는 on-disk nudge — 구 DelegateTool 의 정책 이식
        (PR-4 하드컷 때 누락됐던 것을 테스트가 잡음)."""
        artifact = getattr(result, "artifact", "") or ""
        if ctx.session_dir and artifact:
            path = str(Path(ctx.session_dir) / artifact / "result.md")
            return on_disk_oversized_nudge(
                "agent",
                "sub-agent answer",
                f"full answer saved to '{path}'",
                path,
                tokens,
                ctx.oversized_cap,
                ctx.tools_available,
                nlines=body.count("\n") + 1,
                tail_bullets=(
                    (
                        "Or re-run a NARROWER task so the sub-agent returns a "
                        "focused result (attack the root cause: the task was too "
                        "broad)."
                    ),
                ),
            )
        return default_oversized_nudge("agent", tokens, ctx.oversized_cap)

    def _run(self, args: dict, *, ctx=None) -> ToolResult:
        # 루프가 인터셉트 (registry/provider 필요) — 직접/테스트 호출자용.
        return ToolResult(True, output="(agent: intercepted by loop)")
