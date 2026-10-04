"""모델 시점 틀 지원 — 시작 시 시스템 프롬프트 캡처.

C3: web 전송 계층(server.py)에서 분리된 도메인 로직. FastAPI 무의존.
"""

from __future__ import annotations

from agent_cli.render.web import WebRenderer


def capture_startup_system_prompt(
    renderer: WebRenderer,
    *,
    capabilities,
    dialect,
    session_dir: str,
    max_depth: int,
) -> None:
    """Build + capture the system-prompt snapshot at web startup so the Prompt
    Inspector is populated BEFORE the first message (the loop only captures on
    an LLM call). This mirrors what the main loop builds at depth 0 with all
    tools (web chat uses ``active_tools=None`` → all, MCP tools included —
    they sit in ``TOOLS`` by the time this runs).
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
            dialect=dialect,
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
