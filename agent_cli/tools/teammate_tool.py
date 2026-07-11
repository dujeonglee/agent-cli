"""teammate 도구 스키마 (P1, docs/teammate/DESIGN.md §4.3).

실행은 :func:`agent_cli.subagent.teammate.tool_teammate` — delegate 처럼
루프(tool_bridge)가 인터셉트해 registry/provider 배선을 주입한다. 이
모듈은 스키마·의미론 검증(C7 ``validate``)·over-cap 표면만 소유한다.
레지스트리가 없는 루프(서브에이전트·headless)에서는 AgentLoop 초기화가
도구 목록에서 teammate 를 제거하므로 모델에게 보이지 않는다 (P1 경계:
teammate 안 teammate 금지).
"""

from __future__ import annotations

from pathlib import Path

from agent_cli.tools.base import (
    Tool,
    default_oversized_nudge,
    on_disk_oversized_nudge,
)
from agent_cli.tools.result import ToolResult


class TeammateTool(Tool):
    name = "teammate"
    description = (
        "Manage persistent teammate agents that KEEP their context between "
        "requests. Unlike `delegate` (one-shot: the subagent answers once and "
        "vanishes), a teammate stays alive — spawn it once, send follow-up "
        "requests any time, and its replies are delivered to you automatically "
        "as observations when ready (no polling). Use delegate for independent "
        "one-shot tasks; use teammate for iterative collaboration on evolving "
        "work."
    )
    parameters = {
        "type": "object",
        "properties": {
            "mode": {
                "type": "string",
                "enum": ["spawn", "request", "wait", "status", "kill"],
                "description": (
                    "spawn: create a teammate (returns its key). "
                    "request: send it a message — returns immediately, the "
                    "reply arrives later as an observation. "
                    "wait: block until its next reply (only when you have "
                    "nothing else to do). "
                    "status: list teammates and their state. "
                    "kill: terminate one."
                ),
            },
            "role": {
                "type": "string",
                "description": (
                    "spawn: role definition from .agent-cli/teammates/{role}.md "
                    "(loaded into the teammate's system prompt; omit for a "
                    "generalist). The SAME role may be spawned multiple times "
                    "for parallel independent workstreams — give each instance "
                    "a distinct `name`."
                ),
            },
            "name": {
                "type": "string",
                "description": (
                    "spawn: optional instance label to tell same-role teammates "
                    "apart (e.g. two coders as 'ui' and 'api'). Display only — "
                    "always address teammates by their key."
                ),
            },
            "task": {
                "type": "string",
                "description": "spawn: optional initial request queued right away",
            },
            "key": {
                "type": "string",
                "description": (
                    "teammate key returned by spawn (required for request/wait/"
                    "kill; optional filter for status)"
                ),
            },
            "message": {
                "type": "string",
                "description": "request: the message to send",
            },
            "tools": {
                "type": "array",
                "items": {"type": "string"},
                "description": "spawn: allowed tools (omit for the default set)",
            },
            "context": {
                "type": "string",
                "enum": ["none", "fork"],
                "description": (
                    "spawn: none (fresh context) or fork (copy of the current "
                    "conversation history)"
                ),
            },
        },
        "required": ["mode"],
    }

    # mode 별 조건부 필수 필드 — C7 의미론 검증 훅 (shape 는 중앙 1~5단계).
    _MODE_REQUIRED = {
        "request": ("key", "message"),
        "wait": ("key",),
        "kill": ("key",),
    }

    def validate(self, args: dict) -> str | None:
        mode = args.get("mode")
        valid = ("spawn", "request", "wait", "status", "kill")
        if mode not in valid:
            return f"unknown mode '{mode}' — must be one of {', '.join(valid)}"
        for field in self._MODE_REQUIRED.get(mode, ()):
            value = args.get(field)
            if not isinstance(value, str) or not value.strip():
                return f"mode '{mode}' requires a non-empty '{field}'"
        return None

    def touched_paths(self, action_input: dict) -> list[str]:
        # 파일 경로가 없는 도구 — compaction file-list 에 스폰/요청 흔적 마커.
        args = self.strip_prefix(action_input)
        target = args.get("key") or args.get("role")
        return [f"<teammate:{target}>"] if isinstance(target, str) and target else []

    def summary_arg(self, action_input: dict) -> str:
        args = self.strip_prefix(action_input)
        mode = args.get("mode", "")
        target = args.get("key") or args.get("role") or ""
        return f"{mode} {target}".strip()

    def wrap_single_op(self, flat: dict) -> dict:
        return flat

    def render_oversized(self, result, args, *, body, tokens, ctx) -> str:
        """mode:"wait" 의 큰 회신: worker 가 이미 전문을
        ``teammates/<key>/replies/reply-<seq>.md``(=``result.artifact``)에
        영속했으므로 그 파일을 가리키는 on-disk nudge 로 치환."""
        artifact = getattr(result, "artifact", "") or ""
        if artifact and Path(artifact).is_file():
            return on_disk_oversized_nudge(
                "teammate",
                "teammate reply",
                f"full reply saved to '{artifact}'",
                artifact,
                tokens,
                ctx.oversized_cap,
                ctx.tools_available,
                nlines=body.count("\n") + 1,
                tail_bullets=(
                    "Or send the teammate a NARROWER follow-up request so it "
                    "returns a focused reply.",
                ),
            )
        return default_oversized_nudge("teammate", tokens, ctx.oversized_cap)

    def _run(self, args: dict, *, ctx=None) -> ToolResult:
        # 루프가 인터셉트 (registry/provider 필요) — 직접/테스트 호출자용.
        return ToolResult(True, output="(teammate: intercepted by loop)")
