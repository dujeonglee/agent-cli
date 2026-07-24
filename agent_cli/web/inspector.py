"""Prompt Inspector 지원 — 동적 컨텍스트 섹션 + 시작 시 프롬프트 캡처.

C3: web 전송 계층(server.py)에서 분리된 도메인 로직. FastAPI 무의존.
"""

from __future__ import annotations

from agent_cli.render.web import WebRenderer


def _dynamic_context_sections(ctx) -> list[dict]:
    """The Prompt Inspector's DYNAMIC half: the conversation + observations
    currently in the context window (``ctx.get_messages()`` minus the system
    prompt, which the inspector shows separately as ``kind="system"``).

    One message → one section, the SAME shape as the system sections so the
    frontend renders them identically (no new render path). ``kind="dynamic"``
    marks them. Reads a snapshot copy of the cache (``list(...)``) to avoid a
    rare race with the worker thread appending mid-read (debug view — best
    effort, no lock)."""
    if ctx is None:
        return []
    from agent_cli.context.token_estimator import estimate_tokens

    sections: list[dict] = []
    try:
        messages = list(ctx.get_messages())
    except Exception:
        return []
    for m in messages:
        if m.get("role") == "system":
            continue  # already shown as the system snapshot
        content = m.get("content", "")
        if not isinstance(content, str):
            content = str(content)
        role = m.get("role", "?")
        first = content.strip().split("\n", 1)[0][:60]
        name = f"[{role}] {first}" if first else f"[{role}]"
        sections.append(
            {
                "name": name,
                "text": content,
                "chars": len(content),
                "est_tokens": estimate_tokens(content),
                "kind": "dynamic",
            }
        )
    return sections


def capture_startup_system_prompt(
    renderer: WebRenderer,
    *,
    capabilities,
    wire_format,
    session_dir: str,
    max_depth: int,
    mcp_manager=None,
) -> None:
    """Build + capture the system-prompt snapshot at web startup so the Prompt
    Inspector is populated BEFORE the first message (the loop only captures on
    an LLM call). This mirrors what the main loop builds at depth 0 with all
    tools (web chat uses ``active_tools=None`` → all; ``mcp_manager`` 는 v4.46.0 부터 web 도 배선).
    The first real LLM call rebuilds + overwrites this — including the per-turn
    ``Hook:`` sections, which only exist after ``PreLLMCall`` and so are absent
    from this static preview. Best-effort: a build error must not block
    startup."""
    try:
        from agent_cli.prompts.system_prompt import build_system_prompt_sections
        from agent_cli.tools.registry import TOOLS

        sections = build_system_prompt_sections(
            capabilities=capabilities,
            active_tools=list(TOOLS.keys()),
            session_dir=session_dir,
            mcp_manager=mcp_manager,
            wire_format=wire_format,
            depth=0,
            max_depth=max_depth,
        )
        renderer.note_system_prompt(sections, turn=0)
    except Exception:
        pass


# ``no-cache`` (revalidate-required) rather than ``no-store`` so the
# browser can still take a 304 fast path when nothing changed, but a
# CSS/JS edit lands without forcing the operator to hard-refresh.
# Editable installs serve files straight from the git checkout, so an
# in-session iteration would otherwise be invisible until the operator
# bypassed cache manually.
