"""Agent loop: ReAct pattern with M1/M2 module integration."""

from __future__ import annotations


from agent_cli.prompts.system_prompt import build_system_prompt_sections


# Max shrink-and-retry attempts per turn when the server rejects the
# prompt as too long (flow 2 reactive recovery). Each attempt sheds more
# history via ``ContextManager.force_fit``; the bound stops a runaway
# loop when the cache cannot shrink enough or the server keeps rejecting.
from agent_cli.loop.state import LoopConfig


class SystemPromptSvc:
    """시스템 프롬프트 소유자 (C1 PR-2 승격 — 교차호출 0 실측 클러스터).

    ``sections``(이름 붙은 섹션 리스트)가 단일 진실이고 ``system`` 은 항상
    join 파생 — Inspector 뷰와 LLM 수신 문자열이 구조적으로 일치한다는
    기존 불변식을 클래스 경계로 승격. AgentLoop 는 property 브리지로 기존
    ``self.system``/``self._system_sections`` 표면을 유지한다.
    """

    def __init__(self, config: LoopConfig, ctx) -> None:
        self.cfg = config
        self.ctx = ctx
        self.sections: list[tuple[str, str]] = []
        self.system: str = ""

    def rebuild(self) -> None:
        """(Re)build the static sections from scratch and derive ``system``.
        Run once at setup, and again when DIRECTIVE.md is edited via the
        Prompt Inspector. Hook sections are re-folded by the next
        ``apply_hook_sections``."""
        session_dir = str(self.ctx.session_dir) if self.ctx else ""
        self.sections = build_system_prompt_sections(
            capabilities=self.cfg.capabilities,
            active_tools=self.cfg.tools_list,
            skill_stack=self.cfg.skill_stack,
            agent_stack=self.cfg.agent_stack,
            agent_role=self.cfg.agent_role,
            session_dir=session_dir,
            mcp_manager=self.cfg.mcp_manager,
            wire_format=self.cfg.wire_format,
            depth=self.cfg.depth,
            max_depth=self.cfg.max_depth,
            agent_registry=self.cfg.agent_registry,
        )
        self.system = "\n\n".join(t for _, t in self.sections)

    def apply_hook_sections(self, hook_ctx) -> None:
        """Apply dynamic sections from hook context — idempotent across turns
        (previous ``Hook: `` sections are replaced). The ``<!-- HOOK_SECTIONS
        -->`` marker keeps the joined string byte-identical to the historical
        format."""
        if not hook_ctx or not hook_ctx.system_sections:
            return
        # Callers that set ``system`` directly (tests, embedders) without
        # going through rebuild get a single seeded section so the
        # single-source invariant still holds.
        if not self.sections and self.system:
            self.sections = [("Base", self.system)]
        static = [s for s in self.sections if not s[0].startswith("Hook: ")]
        hook_sections = [
            (f"Hook: {title}", f"## {title}\n{content}")
            for title, content in hook_ctx.system_sections.items()
        ]
        first_name, first_text = hook_sections[0]
        hook_sections[0] = (
            first_name,
            f"<!-- HOOK_SECTIONS -->\n{first_text}",
        )
        self.sections = static + hook_sections
        self.system = "\n\n".join(t for _, t in self.sections)


def build_inspector_sections(system_sections, ctx):
    """Prompt Inspector sections = system-prompt sections + compaction-
    injected context (running summary + touched-file list).

    The summary and file list are injected as ``role=user`` messages right
    after the system prompt (``ContextManager.get_messages``), so they are
    NOT part of ``self.system`` — but they DO consume the context window and
    shape the turn, so the inspector surfaces them as clearly-labelled extra
    sections. Returns a NEW list; never mutates ``system_sections`` (which
    is the single source ``self.system`` derives from).
    """
    sections = list(system_sections)
    if ctx is None:
        return sections
    summary = getattr(ctx, "summary", "")
    if summary:
        sections.append(("⊙ Compaction summary (user-injected)", summary))
    file_list = getattr(ctx, "file_list", None) or []
    if file_list:
        listing = "\n".join(f"- {p}" for p in file_list)
        sections.append(("⊙ Files touched (user-injected)", listing))
    return sections


# Fields a model might use to wrap a question's text inside a dict —
# e.g. `questions=[{"question": "..."}]` drift observed with qwen3.6
# in S25FE-kernel session 1776954600. Checked in priority order, so a
# `question` key wins over `text` when both are present.
