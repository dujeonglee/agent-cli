"""MCP tool adapter — wraps MCP tools as :class:`~agent_cli.tools.base.Tool`
instances.

Registers MCP tools into the ``TOOLS`` dict so they appear in Available
Tools and are dispatched by the agent loop like any built-in tool. Since
the Tool-ABC refactor (423608e) the registry expects every ``TOOLS`` value
to be a ``Tool`` subclass — it reads ``.parameters`` for input validation
(``validate_tool_input``) and calls ``.run()`` for dispatch
(``_execute_tool``). MCP tools are therefore ``Tool`` subclasses too, not
bare callables, so they flow through the exact same validation/dispatch
path with no special-casing.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any

from agent_cli.mcp.client import McpClientManager
from agent_cli.tools.base import Tool
from agent_cli.tools.result import ToolResult

#: Every MCP tool name starts with this — a hook matcher or an
#: ``allowed-tools`` reader can tell an MCP tool from a built-in by name alone.
MCP_PREFIX = "mcp__"

#: Function-name limit of the OpenAI and Anthropic tool APIs.
_NAME_MAX = 64
_NAME_UNSAFE = re.compile(r"[^A-Za-z0-9_-]")


def mcp_tool_name(server: str, tool: str) -> str:
    """The name a model calls an MCP tool by: ``mcp__{server}__{tool}``.

    It has to be a legal function name for a server-parsed dialect
    (``^[A-Za-z0-9_-]{1,64}$``), so one name works in every dialect. MCP
    itself allows more (dots, up to 128 characters): other characters become
    ``_``, and a name over the limit keeps its head and ends in a hash of the
    full name so two long names stay distinct.
    """
    name = f"{MCP_PREFIX}{_NAME_UNSAFE.sub('_', server)}__{_NAME_UNSAFE.sub('_', tool)}"
    if len(name) <= _NAME_MAX:
        return name
    digest = hashlib.sha1(f"{server}\0{tool}".encode()).hexdigest()[:8]
    return f"{name[: _NAME_MAX - 9]}_{digest}"


class McpTool(Tool):
    """A connected MCP tool exposed as an agent-cli :class:`Tool`.

    ``name`` is ``mcp__{server}__{tool}`` (:func:`mcp_tool_name`) so it never
    collides with a native tool. ``parameters`` is the server-advertised JSON Schema, so
    the registry validates MCP input the same way it validates native
    tools. ``_run`` forwards the (prefix-stripped) args to the MCP server.

    Wire keys: MCP is prefix-less. Servers advertise bare schema keys
    (``query``), so the model emits them bare — the same shape virtual
    tools (``complete`` / ``ask``) use. The base ``key_prefix`` (``{name}_``)
    is therefore a no-op here: bare keys don't carry it, so ``strip_prefix``
    passes them through unchanged and ``claims`` stays False (MCP never
    participates in ``infer_action`` dropped-name recovery). No prefix is
    added or expected — same mechanism as virtual tools.
    """

    def __init__(
        self,
        manager: McpClientManager,
        server: str,
        tool_name: str,
        description: str,
        parameters: dict,
    ) -> None:
        self.name = mcp_tool_name(server, tool_name)
        self.description = description or "(no description)"
        self.parameters = parameters or {"type": "object", "properties": {}}
        self._manager = manager
        self._server = server
        self._tool_name = tool_name

    def wrap_single_op(self, flat: dict) -> dict:
        # MCP is prefix-less (bare schema keys), so a multi-op flat op IS the
        # canonical input — identity, like the flat-native builtin tools.
        #
        # This override predates the base default being flipped to identity
        # (v9.12.0): back then the default was ``add_prefix``, which namespaced
        # the bare keys (``{query}`` → ``{srv.tool_query}``) and made the
        # prefixed schema-less input fail validate — MCP hit the same trap that
        # killed ``monitor`` and worked around it here. The default no longer
        # bites, so this is now redundant; kept as an explicit statement of the
        # shape (a prefix-less tool's flat op needs no re-wrap), and pinned by
        # tests/test_tool_wire_contract.py either way.
        return flat

    def _run(self, args: dict, *, ctx=None) -> ToolResult:
        # ctx (RunContext) is accepted for the uniform Tool.run signature; MCP
        # dispatch is location-independent and ignores it.
        try:
            result = self._manager.call_tool(self._server, self._tool_name, args)
            text, images = _extract_mcp_result(
                result, f"{self._server}-{self._tool_name}"
            )
            return ToolResult(True, output=text, images=images)
        except Exception as e:
            return ToolResult(False, error=f"MCP {self.name} failed: {e}")


def _extract_mcp_result(result: Any, label: str = "mcp") -> tuple[str, list[dict]]:
    """MCP 결과 → ``(글, 이미지 참조 목록)``.

    글 조각은 그대로. **이미지 조각**(``type == "image"``, base64 ``data`` +
    ``mimeType``)은 v10.61.0 부터 파일로 둔다 — ``.agent-cli/tmp/mcp/<epoch>-
    <label>-<n>.<ext>`` — 그리고 글에는 자리표시 줄 + 저장 위치, ``images`` 에
    참조(docs/vision/DESIGN.md §5). 종전엔 base64 전체가 글로 들어가 수만
    토큰을 먹고 모델은 보지도 못했다. 보는 형식(PNG/JPEG/GIF/WebP)이 아니거나
    base64 가 깨졌으면 저장만 하거나 사유 한 줄. ``label`` 은 파일 이름용
    (``<server>-<tool>``).
    """
    if result is None:
        return "(no output)", []

    # MCP SDK returns CallToolResult with content list
    if hasattr(result, "content"):
        parts: list[str] = []
        images: list[dict] = []
        n = 0
        for item in result.content:
            if hasattr(item, "text"):
                parts.append(item.text)
            elif getattr(item, "type", None) == "image" and isinstance(
                getattr(item, "data", None), str
            ):
                n += 1
                text, ref = _save_mcp_image(item, label, n)
                parts.append(text)
                if ref is not None:
                    images.append(ref)
            elif hasattr(item, "data"):
                parts.append(str(item.data))
            else:
                parts.append(str(item))
        return ("\n".join(parts) if parts else "(no output)"), images

    return str(result), []


def _save_mcp_image(item: Any, label: str, n: int) -> tuple[str, dict | None]:
    """이미지 조각 하나를 파일로 — ``(글 한두 줄, ImageRef dict | None)``."""
    import base64
    import binascii
    from pathlib import Path

    from agent_cli import images as _images

    mime = str(getattr(item, "mimeType", "") or "")
    try:
        data = base64.b64decode(item.data, validate=True)
    except (binascii.Error, ValueError):
        return (
            f"(image #{n}: {mime or 'unknown type'} — base64 data was invalid, not saved)",
            None,
        )
    ext = _images.IMAGE_EXT.get(mime) or (
        mime.split("/", 1)[1] if "/" in mime else "bin"
    )
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", label) or "mcp"
    dest = _images.fresh_upload_path(
        Path.cwd(), f"{safe}-{n}.{ext}", subdir=_images.MCP_IMAGE_DIR
    )
    dest.write_bytes(data)
    try:
        ref = _images.make_ref(dest)
    except ValueError:
        from agent_cli.tools.base import display_path

        note = (
            f"(image #{n}: {mime or 'unknown type'}, {len(data):,} bytes — saved to "
            f"{display_path(dest)}; not a PNG/JPEG/GIF/WebP so it cannot be shown)"
        )
        return note, None
    text = (
        f"{ref.placeholder}\n{ref.mime}, {ref.bytes:,} bytes — saved to {ref.path}; "
        "the image is attached to this result."
    )
    return text, ref.to_dict()


def register_mcp_tools(
    manager: McpClientManager,
) -> dict[str, Tool]:
    """Register all connected MCP tools as :class:`McpTool` instances.

    Returns dict of ``{"mcp__{server}__{tool}": McpTool}`` ready to merge into
    ``TOOLS``. Values are ``Tool`` subclasses (not bare callables) so they
    satisfy the registry's ``.parameters`` / ``.run()`` contract.
    """
    tools: dict[str, Tool] = {}
    for tool_info in manager.list_tools():
        tool = McpTool(
            manager,
            tool_info.server,
            tool_info.name,
            tool_info.description,
            tool_info.input_schema,
        )
        tools[tool.name] = tool
    return tools
