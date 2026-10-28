"""``view_image`` — 이미지 파일을 모델에게 보인다 (v10.77.0, docs/vision/DESIGN.md §12).

v10.59.0 부터 ``read_file`` 의 한 분기였던 것을 도구로 뺐다. 텍스트와 이미지는
결과의 모양(해시라인 대 이미지 조각)도, 컨텍스트 비용도 다르다. 이미지는 붙은
결과 레코드와 운명을 같이한다 — 압축이 그 레코드를 내보낼 때만 사라지고(대화
중간이 바뀌지 않아 KV 캐시가 깨지지 않는다), 하니스가 따로 빼지 않는다.

모델이 습관대로 ``read_file`` 로 이미지를 열면 도구를 바꾸는 층
(``agent_cli/loop/reroute.py``)이 이 도구의 호출로 바꾼다.
"""

from __future__ import annotations

from typing import ClassVar

from agent_cli import images as _images
from agent_cli.tools.base import RunContext, Tool
from agent_cli.tools.result import ToolResult

#: 목록 등 문자열이 아닌 ``path`` 의 오류 — 구제하지 않고 다시 낼 방법을 말한다
#: (관측되면 그때 구제, 2026-10-28 사용자 결정).
PATH_NOT_ONE = (
    "view_image: path must be one file path; to view several images, "
    "call `view_image` once per image"
)


class ViewImageTool(Tool):
    name = "view_image"
    description = (
        "Show an image file to you. A PNG, JPEG, GIF or WebP file is attached to this "
        "result as an image and stays in your context like any other result; read text "
        "files with `read_file`."
    )
    # 못 보는 모델(`supports_vision: false`)이면 광고하지 않는다 — 판정은
    # 루프가 바인딩의 능력에서 한다(`loop/core.py` 의 ``vision`` 자원).
    requires_handler = "vision"
    parameters: ClassVar[dict] = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "minLength": 1,
                "description": "Image file path.",
            },
        },
        "required": ["path"],
    }

    def wrap_single_op(self, flat: dict) -> dict:
        return flat

    def touched_paths(self, action_input: dict) -> list[str]:
        p = self.strip_prefix(action_input).get("path")
        return [p] if isinstance(p, str) and p else []

    def summary_arg(self, action_input: dict) -> str:
        p = self.strip_prefix(action_input).get("path", "")
        return p if isinstance(p, str) else ""

    def validate(self, args: dict) -> str | None:
        if not isinstance(args.get("path"), str):
            return PATH_NOT_ONE
        return None

    def _run(self, args: dict, *, ctx: RunContext | None = None) -> ToolResult:
        path = args["path"]
        # 민감 경로 게이트 — read_file 과 같은 축(자격증명 사전)만 건다.
        from agent_cli.tools._confine import guard as _path_guard

        denied = _path_guard([path], "view_image", check_workspace=False)
        if denied:
            return ToolResult(False, error=denied)
        try:
            with open(path, "rb") as f:
                head = f.read(16)
        except OSError as e:
            return ToolResult(False, error=f"view_image failed: {e}")
        if _images.sniff_image(head) is None:
            hint = ""
            if ctx is None or "read_file" in ctx.tools_available:
                hint = " — read text files with `read_file`"
            return ToolResult(
                False,
                error=f"view_image: {path} is not a PNG, JPEG, GIF or WebP image{hint}",
            )
        try:
            ref = _images.make_ref(path)
        except (ValueError, OSError) as e:
            return ToolResult(False, error=f"view_image failed: {e}")
        return ToolResult(
            True,
            output=(
                f"{ref.placeholder}\n{ref.mime}, {ref.bytes:,} bytes — "
                "the image is attached to this result."
            ),
            images=[ref.to_dict()],
        )
