"""``tool_help`` — the full guide of one tool (v10.109.0, docs/tool-help/DESIGN.md).

지연 도구(``Tool.deferred``)는 프롬프트에 한 줄 ``summary`` 와 인자 뼈대만 싣는다.
전문은 이 도구가 **관찰**로 준다 — 시스템 프롬프트는 런 동안 고정이라(KV 프리픽스)
펼칠 수 없다. 결과는 따로 쓴 글이 아니라 지금 루프의 ``## Available Tools`` 에 실렸을
바로 그 블록(``registry.render_tool_block(full=True)``)이라 두 벌이 생기지 않는다.
루프가 묶어 둔 ``RunContext.tool_guide`` 를 부른다 — 방언·설명/스키마 교체·인라인
안내는 루프마다 다르다. 모르는 이름일 때 나열하는 목록도 루프가 묶어 준
``RunContext.tool_names`` 다.

``requires_handler="deferred"`` + ``force_mount`` — 지연 도구가 남은 루프에만, 그리고
그런 루프라면 프로파일의 ``allowed-tools`` 와 무관하게 붙는다(D4: 모든 루프 같은 규칙).
"""

from __future__ import annotations

from typing import ClassVar

from agent_cli.tools.base import Tool
from agent_cli.tools.result import ToolResult

#: 결과의 머리 줄 — 관찰 카드와 압축 요약이 어느 도구의 전문인지 알아보게.
HEADER = "[tool_help] {name}"
#: 모르는 이름 — 이 루프에서 부를 수 있는 이름을 함께 준다.
UNKNOWN = 'No tool named "{name}" in this loop. Tools: {names}'
#: 루프가 전문을 묶어 주지 않았다(테스트·외부 호출).
NO_GUIDE = "tool_help: tool guides are not available in this loop."


class ToolHelpTool(Tool):
    name = "tool_help"
    requires_handler = "deferred"
    force_mount = True
    description = (
        "Full guide for one tool — what each argument means and how its results "
        "look. Call it before the first use of a tool described here in one line."
    )
    parameters: ClassVar[dict] = {
        "type": "object",
        "properties": {
            "name": {"type": "string", "minLength": 1, "description": "tool name"},
        },
        "required": ["name"],
    }

    def summary_arg(self, action_input: dict) -> str:
        return str(self.strip_prefix(action_input).get("name", ""))

    def _run(self, args: dict, *, ctx=None) -> ToolResult:
        name = str(args.get("name") or "").strip()
        guide_of = getattr(ctx, "tool_guide", None)
        if guide_of is None:
            return ToolResult(False, error=NO_GUIDE)
        # 이 루프가 광고하는 이름 — 루프가 묶어 준 목록(`RunContext.tool_names`)
        names = getattr(ctx, "tool_names", ())
        text = guide_of(name) if name in names else None
        if text is None:
            return ToolResult(
                False,
                error=UNKNOWN.format(
                    name=name, names=", ".join(f"`{n}`" for n in names)
                ),
            )
        return ToolResult(True, output=f"{HEADER.format(name=name)}\n{text}")
