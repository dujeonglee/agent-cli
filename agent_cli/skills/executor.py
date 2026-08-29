"""Skill executor — substitute arguments and call run_loop."""

from __future__ import annotations

import re
import subprocess

# execute_skill 의 agent_registry 기본값 sentinel — "미지정"(main 슬롯 자동
# 상속)과 "명시 None"(registry 없음 = 서브에이전트 run-only 경계)을 구분한다.
_INHERIT = object()

from agent_cli.constants import AGENT_DEFAULT_TIMEOUT, SHELL_COMMAND_TIMEOUT
from agent_cli.context.manager import ContextManager
from agent_cli.loop import run_loop
from agent_cli.providers.base import LLMProvider
from agent_cli.providers.capabilities import ModelCapabilities
from agent_cli.skills.models import Skill
from agent_cli.tools.result import ToolResult

# Pattern for !`command` dynamic context injection
_SHELL_INJECT_PATTERN = re.compile(r"!`([^`]+)`")

# Injected shell output is spliced straight into a skill's prompt, so it has no
# tool-observation seam and no oversized cap in front of it — an unbounded
# `!`git log`` or `!`cat …`` used to land in the sub-loop's prompt whole. Cap it
# here, keeping head AND tail (a command's summary/exit state is usually last).
_INJECT_MAX_CHARS = 4000
_INJECT_HEAD_CHARS = 1500
_INJECT_TAIL_CHARS = _INJECT_MAX_CHARS - _INJECT_HEAD_CHARS


def _clamp_injected(cmd: str, output: str) -> str:
    """Bound one injected command's output to ``_INJECT_MAX_CHARS``, keeping a
    head and a tail with an explicit marker between them so the skill author
    (and the model) can see that the middle was dropped."""
    if len(output) <= _INJECT_MAX_CHARS:
        return output
    omitted = len(output) - _INJECT_HEAD_CHARS - _INJECT_TAIL_CHARS
    return (
        f"{output[:_INJECT_HEAD_CHARS]}\n"
        f"[... {omitted:,} chars omitted from `{cmd}` output — injected context "
        f"is capped at {_INJECT_MAX_CHARS:,} chars; run the command with a "
        f"narrower scope if you need the middle ...]\n"
        f"{output[-_INJECT_TAIL_CHARS:]}"
    )


def _execute_shell_inject(m: re.Match) -> str:
    """Execute a shell command and return its output for template injection."""
    cmd = m.group(1)
    try:
        result = subprocess.run(
            cmd,
            shell=True,
            capture_output=True,
            text=True,
            timeout=SHELL_COMMAND_TIMEOUT,
            check=False,
        )
        output = result.stdout.strip()
        if result.returncode != 0:
            stderr = result.stderr.strip()
            return _clamp_injected(
                cmd,
                f"[error] {cmd}: {stderr or '(exit code ' + str(result.returncode) + ')'}",
            )
        return _clamp_injected(cmd, output)
    except subprocess.TimeoutExpired:
        return f"[error] {cmd}: timed out (30s)"
    except Exception as e:
        return f"[error] {cmd}: {e}"


def substitute_arguments(
    template: str,
    arguments: str,
    skill_dir: str = "",
    session_id: str = "",
) -> str:
    """Replace $ARGUMENTS, $N, ${SKILL_DIR}, ${SESSION_ID}, !`cmd` in template.

    ${CLAUDE_SKILL_DIR} is supported as an alias for Claude Code compatibility.

    When a skill's prompt must document the template syntax itself (e.g.
    create-skill showing what ${SKILL_DIR} looks like), put the
    placeholder-bearing docs in a `references/` file and instruct the
    LLM to read_file it at runtime. Tool results do not pass through
    this substitution, so the literal placeholder survives to the LLM.
    """
    # Dynamic context injection: !`command` → command output
    result = _SHELL_INJECT_PATTERN.sub(_execute_shell_inject, template)

    # Built-in variables (${SKILL_DIR} is the primary form; ${CLAUDE_SKILL_DIR} for compat)
    result = result.replace("${SKILL_DIR}", skill_dir)
    result = result.replace("${CLAUDE_SKILL_DIR}", skill_dir)
    result = result.replace("${SESSION_ID}", session_id)

    # $ARGUMENTS[N] bracket notation (before $ARGUMENTS replacement)
    args_list = arguments.split()

    def _replace_bracket(m: re.Match) -> str:
        idx = int(m.group(1))
        return args_list[idx] if idx < len(args_list) else ""

    result = re.sub(r"\$ARGUMENTS\[(\d+)\]", _replace_bracket, result)

    # $ARGUMENTS (full string)
    result = result.replace("$ARGUMENTS", arguments)

    # $N shorthand — 단일 패스 정규식 치환. 숫자 전체를 한 토큰으로 잡으므로
    # 인자가 10개 이상이어도 ``$10`` 이 ``$1``+"0" 으로 쪼개지지 않고(종전
    # 순차 replace 루프의 버그 — 리뷰 §4.5), 인자 범위 밖 $N 은 빈 문자열로
    # 정리된다(종전 후처리 sub 와 동일 의미).
    def _replace_n(m: re.Match) -> str:
        idx = int(m.group(1))
        return args_list[idx] if idx < len(args_list) else ""

    result = re.sub(r"\$(\d+)", _replace_n, result)

    return result


def execute_skill(
    skill: Skill,
    arguments: str,
    provider: LLMProvider,
    capabilities: ModelCapabilities,
    model: str,
    provider_name: str = "openai",
    base_url: str = "",
    api_key: str = "",
    max_turns: int = 0,
    verbose: bool = False,
    max_depth: int = 2,
    agent_timeout: int = AGENT_DEFAULT_TIMEOUT,
    ctx: ContextManager | None = None,
    session=None,
    skill_stack: list[str] | None = None,
    graceful_interrupt: bool = False,
    stop_event=None,
    parent_tools: list[str] | None = None,
    parent_role: str = "",
    parent_hooks_config: dict | None = None,
    parent_depth: int = 0,
    compaction_enabled: bool = True,
    agent_registry=_INHERIT,
    session_subdir: str = "",
    scope_id: str = "",
):
    """Execute a skill by substituting arguments and calling run_loop.

    Tool intersection: skill's allowed_tools ∩ parent_tools.
    If intersection is empty, execution is rejected.
    Parent role is inherited for system prompt.

    Hooks: parent_hooks_config is merged with the skill's own frontmatter
    hooks (skill.hooks) so skill-local matchers layer on top of the
    caller's hooks for the duration of this skill's execution.
    """
    from pathlib import Path

    from agent_cli.hooks import merge_hooks_configs

    # registry 해석 (배선 통일, v7.17.0): 미지정(_INHERIT)이면 main 슬롯을
    # 자동 상속 — 사용자 /skill dispatch 등 main 의 모든 skill 실행 경로가
    # 별도 배선 없이 spawn 가능해진다. 명시 전달(None 포함)은 그대로 존중
    # — 서브에이전트 루프의 dispatch 가 명시 None 을 넘겨 run-only 경계 유지.
    if agent_registry is _INHERIT:
        from agent_cli.subagent.agents_live import main_registry

        agent_registry = main_registry()

    # Tool intersection: skill tools ∩ parent tools
    effective_tools = skill.allowed_tools
    if effective_tools and parent_tools:
        effective_tools = [t for t in effective_tools if t in parent_tools]
        if not effective_tools:
            skill_tools = ", ".join(skill.allowed_tools)
            parent_str = ", ".join(parent_tools)
            return ToolResult(
                False,
                error=f"Skill '{skill.name}' cannot run: "
                f"no tools in common between skill ({skill_tools}) "
                f"and parent ({parent_str})",
            )
    elif not effective_tools and parent_tools:
        # Skill has no tool restriction → use parent's tools
        effective_tools = parent_tools

    skill_dir = str(Path(skill.source_path).parent) if skill.source_path else ""
    session_id = ""
    prompt = substitute_arguments(
        skill.prompt_template, arguments, skill_dir=skill_dir, session_id=session_id
    )

    effective_max_turns = skill.max_turns if skill.max_turns > 0 else max_turns
    effective_model = skill.model if skill.model else model

    # Skill creates its own subdir with history.jsonl
    skill_ctx = None
    skill_dir_name = ""
    if ctx:
        import os
        import time as _time

        # ``session_subdir`` — pre-named by the scope-opening caller so the
        # resume sidecar's ``ctx_dir`` and this ctx's actual directory agree
        # (the card's inner turns replay from it). Self-naming stays as the
        # fallback for direct callers/tests that never open a scope.
        if session_subdir:
            skill_dir_name = session_subdir
        else:
            name = skill.name or "skill"
            hash_part = os.urandom(3).hex()
            ts = _time.strftime("%Y%m%dT%H%M%S", _time.gmtime())
            ms = f"{int(_time.time() * 1000) % 1000:03d}"
            skill_dir_name = f"skill_{name}_{hash_part}_{ts}{ms}"
        skill_session_dir = ctx.session_dir / skill_dir_name
        skill_ctx = ContextManager(
            session_dir=skill_session_dir,
            max_context_tokens=ctx.max_context_tokens,
            wire_format=ctx.wire_format,
        )

    effective_hooks_config = merge_hooks_configs(parent_hooks_config, skill.hooks)

    # 프롬프트 인스펙터 스코프. skill 은 호출자 스레드에서 중첩 실행이라
    # 시스템 스냅샷/동적 ctx 를 이 스코프에 귀속시킨다.
    #
    # ★ scope_id 통일 (인스펙터 재설계): 스코프를 여는 호출자(main.py /
    # loop.skill_invoke)는 `render_begin_scope(scope_id, "skill", …)` 로 이미
    # 이 스레드의 prompt-scope 스택에 scope_id 를 push 하고, 종료 시 end_scope 가
    # pop + 동적 ctx 고정까지 한다. 그 scope_id 가 곧 타임라인 카드의
    # data-task-id 이자 `GET /api/debug/prompt?task_id=` 의 키다. 따라서 여기서
    # 별도 id 로 begin_prompt_scope 를 또 push 하면 스냅샷이 카드 id 와 다른
    # id 에 저장돼 카드 🔍 인스펙션이 ok:false 가 됐다. 호출자가 scope_id 를
    # 넘겨주면(=스코프를 이미 열었으면) 그 스코프에 note_scope_ctx 로만 등록하고
    # push/pop 은 호출자에게 맡긴다. 직접 호출/테스트(scope_id 없음)만 자체
    # 스코프를 열어 폴백한다. CLI(minimal 렌더러)는 전부 no-op.
    import uuid as _uuid

    from agent_cli.render import get_renderer as _get_renderer

    _renderer = _get_renderer()
    _owns_scope = not scope_id  # 호출자가 스코프를 안 열었을 때만 자체 관리
    if _owns_scope:
        _scope_id = f"skill-{skill.name or 'skill'}-{_uuid.uuid4().hex[:8]}"
        _renderer.begin_prompt_scope(_scope_id, label=f"skill:{skill.name or 'skill'}")
    if skill_ctx is not None:
        # 현재 스택 top 스코프에 등록 — scope_id 전달 시 카드 id, 아니면 위 폴백 id.
        _renderer.note_scope_ctx(skill_ctx)
    try:
        loop_result = run_loop(
            query=prompt,
            provider=provider,
            capabilities=capabilities,
            model=effective_model,
            provider_name=provider_name,
            base_url=base_url,
            api_key=api_key,
            max_turns=effective_max_turns,
            verbose=verbose,
            depth=parent_depth + 1,
            max_depth=max_depth,
            # A skill is the MAIN agent's own workflow (unlike a sub-agent, which
            # is an independent one-shot), so it inherits the main registry and
            # can spawn/manage persistent workers. depth still bounds skill→skill
            # recursion; spawn permission rides on the registry, not depth — so a
            # skill (registry present) gets the full agent tool while an ordinary
            # sub-agent (no registry) stays run-only. Workers it spawns register
            # on the main registry and outlive the skill (main takes them over).
            agent_registry=agent_registry,
            agent_timeout=agent_timeout,
            active_tools=effective_tools,
            ctx=skill_ctx or ctx,
            session=session,
            hooks_config=effective_hooks_config,
            skill_name=skill.name,
            skill_stack=skill_stack,
            skill_args=arguments,
            graceful_interrupt=graceful_interrupt,
            stop_event=stop_event,
            agent_role=parent_role,
            compaction_enabled=compaction_enabled,
        )
    finally:
        if _owns_scope:
            _renderer.end_prompt_scope(_scope_id)

    result = loop_result.output if loop_result.success else None

    # Save result.md in skill subdir
    if skill_ctx and skill_dir_name and result:
        try:
            result_path = skill_ctx.session_dir / "result.md"
            result_path.write_text(result, encoding="utf-8")
        except Exception:
            pass

    artifact = f"{skill_dir_name}/" if skill_dir_name else ""
    if result:
        return ToolResult(True, output=result, artifact=artifact)
    else:
        return ToolResult(False, error="Skill returned no result", artifact=artifact)
