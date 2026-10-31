"""Skill executor — 인자 치환(`substitute_arguments`)과 `SkillLoop` (`LoopKind`, v10.87.0)."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

from agent_cli.constants import SHELL_COMMAND_TIMEOUT
from agent_cli.loop.kind import Caller, LoopKind, LoopKindError, Start
from agent_cli.skills.models import Skill
from agent_cli.tools.result import ToolResult

if TYPE_CHECKING:
    from agent_cli.loop.ports import LoopPorts

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


class SkillLoop(LoopKind):
    """스킬 실행 — 호출마다 하나, ``SkillLoop(skill, arguments, caller).run()``.

    스킬은 **호출자의 일**이다(독립 one-shot 과 달리): 주소·레지스트리·역할·도구 범위·
    바인딩·스택을 호출자(`Caller`)에게서 받는다. main 아래면 main 레지스트리를 상속해
    spawn 할 수 있고(orchestrate 류의 존재 이유), 서브에이전트 아래면 run-only 다. 깊이는
    호출자 +1 — 스킬→스킬 재귀도 깊이가 막는다.

    - ⑤ 도구: ``skill.allowed_tools ∩ caller.tools`` (공통 없음이면 거절; 스킬에 제한이
      없으면 호출자 범위). v10.87.0 D3: 종전엔 ``parent_tools`` 를 아무도 넘기지 않아 교집합이
      죽어 있었다.
    - ⑨ 역할: ``caller.agent_role`` (D3 — 상주 안의 스킬도 그 에이전트의 역할로 돈다).
    - ⑬ ctx: 호출자 세션 아래 ``skill_<name>_…`` 서브디렉터리에 `create_subagent_ctx("none")`
      — 종전엔 `ContextManager` 를 직접 만들어 사고·문법·샘플링·타임아웃·압축 비율을 빠뜨렸다.
      호출자에 ctx 가 없으면(헤드리스) ctx 없이 돈다.
    - ⑭ 스코프: 카드 하나(``kind="skill"``) + CLI 들여쓰기 + OnSkillStart/OnSkillEnd 훅 — 도구
      길(`run_skill`)과 명령 길(`/skill`)이 같다(종전엔 `/skill` 에 훅이 없었다).
    - ⑰ 결과: ``result.md`` 저장 → `ToolResult(True, output, artifact)` 또는
      ``"Skill returned no result"``; 예외는 ``run_skill(<name>) failed: …``.
    - ⑱ 정리: 없음 — 주소가 호출자 것이라 거둘 것이 없다.
    """

    def __init__(
        self,
        skill: Skill,
        arguments: str,
        caller: Caller,
        *,
        scope_id: str = "",
        session_subdir: str = "",
        tool_input: dict | None = None,
    ) -> None:
        import uuid as _uuid

        from agent_cli.context.session import SKILL_PREFIX
        from agent_cli.hooks import merge_hooks_configs
        from agent_cli.subagent.report import generate_subdir_name

        self.skill = skill
        self.arguments = arguments
        self.caller = caller
        name = skill.name or "skill"
        self.owner = caller.owner
        self.depth = caller.depth + 1
        self.max_depth = caller.max_depth
        self.max_turns = skill.max_turns if skill.max_turns > 0 else caller.max_turns
        # 스킬이 모델을 고정했으면 그 모델의 바인딩(능력·방언) — 세션 바인딩은 그대로.
        self.binding = (
            caller.binding.pinned(skill.model) if skill.model else caller.binding
        )
        self.hooks_config = merge_hooks_configs(caller.hooks_config, skill.hooks)
        self.session = caller.session
        self.compaction_enabled = caller.compaction_enabled
        self.graceful_interrupt = caller.graceful_interrupt
        self.verbose = caller.verbose
        self.stop_event = caller.stop_event
        self.skill_name = skill.name
        self.skill_args = arguments
        self.skill_stack = list(caller.skill_stack)
        self.agent_stack = list(caller.agent_stack)
        self.agent_role = caller.agent_role
        self.ports = self.make_ports(
            agent_registry=caller.agent_registry, owner=self.owner
        )
        self.scope_id = scope_id or f"skill-{name}-{_uuid.uuid4().hex[:8]}"
        # 디렉토리 이름은 스코프를 열 때 정해져 사이드카의 ``ctx_dir`` 과 같아야 한다 —
        # resume 이 카드 안의 턴을 거기서 다시 그린다.
        self.dir_name = session_subdir or generate_subdir_name(SKILL_PREFIX, name)
        self.tool_input = (
            tool_input
            if tool_input is not None
            else {
                "name": skill.name,
                "arguments": arguments,
            }
        )
        self.skill_ctx = None

    @staticmethod
    def make_ports(*, agent_registry, owner: str) -> LoopPorts:
        """스킬 루프의 포트. ``owner`` 는 호출자 것 — 상주 안에서 돈 스킬이 건 모니터는 그
        에이전트에게 보고해야 한다(기본값 없음: 상속이 빠지면 조용히 main 으로 가므로)."""
        from agent_cli.loop.ports import LoopPorts
        from agent_cli.runtime import (
            _AS_BEFORE,
            _NO_INBOX,
            _RESIDENT_ROSTER,
            _WEB_ONLY,
            _hook_runner_port,
            _hook_unwired,
        )

        return LoopPorts(
            owner=owner,
            agent_registry=agent_registry,
            questions=None,
            mcp_manager=None,
            message_handler=None,
            hook_runner=_hook_runner_port(),
            route_message=None,
            dequeue_user_message=None,
            absorb_inbox=None,
            peer_roster=None,
            unwired={
                "questions": _AS_BEFORE,
                "mcp_manager": _AS_BEFORE,
                "message_handler": _AS_BEFORE,
                **_hook_unwired(),
                "route_message": _WEB_ONLY,
                "dequeue_user_message": _WEB_ONLY,
                "absorb_inbox": _NO_INBOX,
                "peer_roster": _RESIDENT_ROSTER,
            },
        )

    # ── ⑫ 시작 — 템플릿 치환 ──
    def start(self) -> Start:
        skill = self.skill
        skill_dir = str(Path(skill.source_path).parent) if skill.source_path else ""
        return Start(
            query=substitute_arguments(
                skill.prompt_template,
                self.arguments,
                skill_dir=skill_dir,
                session_id="",
            )
        )

    # ── ⑤·⑬ 도구 범위와 ctx ──
    def make_ctx(self, start: Start):
        from agent_cli.subagent.runner import create_subagent_ctx

        skill, caller = self.skill, self.caller
        effective = skill.allowed_tools
        parent = caller.tools
        if effective and parent:
            effective = [t for t in effective if t in parent]
            if not effective:
                raise LoopKindError(
                    f"Skill '{skill.name}' cannot run: no tools in common between "
                    f"skill ({', '.join(skill.allowed_tools)}) and parent "
                    f"({', '.join(parent)})"
                )
        elif not effective and parent:
            effective = parent  # 스킬에 제한이 없으면 호출자 범위
        self.tools = effective or None
        if caller.ctx is None:
            return None
        ctx, err = create_subagent_ctx(
            "none",
            caller.ctx,
            caller.ctx.session_dir / self.dir_name,
            binding=self.binding,
        )
        if ctx is None:
            raise LoopKindError(f"Skill '{skill.name}' cannot run: {err}")
        self.skill_ctx = ctx
        return ctx

    def rejected(self, reason: str) -> ToolResult:
        return ToolResult(False, error=reason)

    # ── ⑭ 스코프 — 카드 하나 + 들여쓰기 + 훅 ──
    def open_scope(self, start: Start):
        from agent_cli.render import render_begin_scope, render_push_depth

        hooks = self.caller.hook_runner
        if hooks:
            hooks.fire(
                "OnSkillStart",
                tool_name="run_skill",
                tool_input=self.tool_input,
                mcp_manager=self.caller.mcp_manager,
            )
        render_begin_scope(
            self.scope_id, "skill", f"skill:{self.skill.name}", ctx_dir=self.dir_name
        )
        render_push_depth()
        return self.scope_id

    def close_scope(self, scope_id, finished, secs: float) -> None:
        from agent_cli.render import render_end_scope, render_pop_depth

        render_pop_depth()
        render_end_scope(
            scope_id,
            "skill",
            success=bool(finished is not None and finished.success),
            duration_s=secs,
        )
        hooks = self.caller.hook_runner
        if hooks:
            hooks.fire(
                "OnSkillEnd",
                tool_name="run_skill",
                tool_input=self.tool_input,
                skill_result=finished,
                mcp_manager=self.caller.mcp_manager,
            )

    # ── ⑰ 결과 ──
    def finish(self, result, secs: float, released: str) -> ToolResult:
        output = result.output if result.success else None
        if self.skill_ctx is not None and output:
            try:
                (self.skill_ctx.session_dir / "result.md").write_text(
                    output, encoding="utf-8"
                )
            except Exception:
                pass
        artifact = f"{self.dir_name}/" if self.skill_ctx is not None else ""
        if output:
            return ToolResult(True, output=output, artifact=artifact)
        return ToolResult(False, error="Skill returned no result", artifact=artifact)

    def crashed(self, exc: BaseException, secs: float) -> ToolResult:
        from agent_cli.verbose import debug_log

        debug_log(f"run_skill({self.skill.name}) exception: {exc}")
        return ToolResult(False, error=f"run_skill({self.skill.name}) failed: {exc}")
