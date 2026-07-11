"""teammate 도구 스키마 (P1, docs/teammate/DESIGN.md §4.3).

실행은 :func:`agent_cli.subagent.agents_live.tool_agent` — delegate 처럼
루프(tool_bridge)가 인터셉트해 registry/provider 배선을 주입한다. 이
모듈은 스키마·의미론 검증(C7 ``validate``)·over-cap 표면만 소유한다.
레지스트리가 없는 루프(서브에이전트·headless)에서는 AgentLoop 초기화가
도구 목록에서 teammate 를 제거하므로 모델에게 보이지 않는다 (P1 경계:
teammate 안 teammate 금지).
"""

from __future__ import annotations

from agent_cli.tools.base import Tool
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
                "enum": ["spawn", "request", "status", "resume", "kill"],
                "description": (
                    "spawn: create a teammate (returns its key). "
                    "request: send it a message — returns immediately; the "
                    "reply is DELIVERED to you automatically when ready "
                    "(never poll, never wait — keep working or complete). "
                    "status: list teammates and their state. "
                    "resume: bring a DEAD teammate back to life with its full "
                    "prior context (it remembers everything). "
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
                "description": (
                    "spawn/resume: optional initial request queued right away"
                ),
            },
            "instructions": {
                "type": "string",
                "description": (
                    "spawn: inline role text (instant-agent) — becomes part of "
                    "the teammate's system prompt for its WHOLE life and "
                    "survives resume. Use alone for an ad-hoc specialist, or "
                    "with `role` to append session-specific directions to a "
                    "profile."
                ),
            },
            "key": {
                "type": "string",
                "description": (
                    "teammate key returned by spawn (required for request/"
                    "resume/kill; optional filter for status)"
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
        "resume": ("key",),
        "kill": ("key",),
    }

    def validate(self, args: dict) -> str | None:
        mode = args.get("mode")
        valid = ("spawn", "request", "status", "resume", "kill")
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

    def _run(self, args: dict, *, ctx=None) -> ToolResult:
        # 루프가 인터셉트 (registry/provider 필요) — 직접/테스트 호출자용.
        return ToolResult(True, output="(teammate: intercepted by loop)")
