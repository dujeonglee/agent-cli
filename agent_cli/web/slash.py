"""웹 slash 명령 + 디스패치 어댑터 — /help·/sh·/compact·@agent·/skill.

C3: web 전송 계층(server.py)에서 분리. main.py 의 web worker 와
create_app 이 소비한다.
"""

from __future__ import annotations

import subprocess


from agent_cli.constants import SHELL_COMMAND_TIMEOUT
from agent_cli.render.web import WebRenderer

_WEB_HELP_TEXT = (
    "Web mode commands:\n"
    "  /help                    Show this help\n"
    "  /compact                 Compact context now (summarise oldest half)\n"
    "  /sh <command>            Run a shell command directly (LLM bypass)\n"
    "  /skills                  List available skills\n"
    "  /<skill> <args>          Invoke a skill directly\n"
    "  @agents                  List available agents\n"
    "  @<agent> <task>          Delegate a task to an agent\n"
    "\n"
    "Any other input goes to the LLM as a chat turn."
)


def handle_slash_command(message: str, renderer: WebRenderer, ctx=None) -> bool:
    """Intercept web-specific commands.

    Returns ``True`` if the message was handled here (caller skips
    further dispatch / LLM); ``False`` otherwise. Output surfaces as
    an ``observation`` event so the frontend renders it as a tool-
    result card alongside whatever else is in the session.

    Handled:
      - ``/help`` — list supported web commands
      - ``/sh <cmd>`` — direct shell execution (no CLI parity yet)
      - ``/compact`` — manual context compaction (needs ``ctx``)

    ``@<agent>`` / ``/<skill>`` (including the ``@agents`` /
    ``/skills`` listings and not-found errors) are routed through
    :func:`agent_cli.main.try_dispatch_agent_or_skill` so the web worker
    and ``run`` share one prefix-dispatcher with a thin output adapter
    per surface — see ``WebDispatchOutput`` below.
    """
    if message == "/help":
        renderer.observation(
            _WEB_HELP_TEXT,
            turn=0,
            tool_name="help",
            success=True,
        )
        return True

    if message == "/compact" or message.startswith("/compact "):
        if ctx is None:
            renderer.observation(
                "Compaction unavailable in this session.",
                turn=0,
                tool_name="compact",
                success=False,
            )
            return True
        before, after = ctx.compact_now()
        if after < before:
            msg = f"Compacted: {before:,} → {after:,} tokens."
        else:
            msg = (
                f"Nothing to compact ({before:,} / {ctx.max_context_tokens:,} tokens)."
            )
        renderer.observation(msg, turn=0, tool_name="compact", success=True)
        return True

    if message.startswith("/sh"):
        return _handle_sh(message, renderer)

    return False


class WebDispatchOutput:
    """Web-flavoured ``DispatchOutput`` — every branch maps to a single
    ``observation`` event so the frontend renders consistent tool-result
    cards for listings, errors, and agent results.

    Lives in this module (next to ``handle_slash_command``) because
    it's the only place that needs ``WebRenderer`` knowledge; keeping
    ``agent_cli.main`` free of web-renderer imports preserves the
    optional-extra boundary (``pip install agent-cli`` without ``[web]``
    must still work).
    """

    def __init__(self, renderer: WebRenderer) -> None:
        self.renderer = renderer

    def agent_dispatch_result(self, text: str, success: bool) -> None:
        self.renderer.observation(text, turn=0, tool_name="agent", success=success)

    def list_agents(self, agents: list[tuple[str, str]], live_status: str = "") -> None:
        lines = ["Agent profiles:"]
        if not agents:
            lines.append("  (none)")
        for name, desc in agents:
            suffix = f" — {desc}" if desc else ""
            lines.append(f"  @{name}{suffix}")
        if live_status:
            lines.append("")
            lines.append("Live agents:")
            lines.append(live_status)
        lines.append("")
        lines.append(
            "``@<profile> <task>`` (일회성 run) · ``@<profile>-spawn <task>`` "
            "(상주) · ``@agt-<key> <메시지>`` (직접 전송 — 회신은 🤝 창으로)"
        )
        self.renderer.observation(
            "\n".join(lines),
            turn=0,
            tool_name="agents",
            success=True,
        )

    def list_skills(self, skills: dict) -> None:
        user_skills = {k: v for k, v in skills.items() if v.user_invocable}
        if not user_skills:
            self.renderer.observation(
                "No skills available.",
                turn=0,
                tool_name="skills",
                success=True,
            )
            return
        lines = ["Available skills:"]
        for s in user_skills.values():
            hint = f" {s.argument_hint}" if s.argument_hint else ""
            lines.append(f"  /{s.name}{hint} — {s.description}")
        lines.append("")
        lines.append(
            "Invoke directly with ``/<skill> <args>`` or let the LLM call ``run_skill``."
        )
        self.renderer.observation(
            "\n".join(lines),
            turn=0,
            tool_name="skills",
            success=True,
        )

    def agent_not_found(self, name: str) -> None:
        self.renderer.observation(
            f"Agent not found: @{name}. Type ``@agents`` to list available agents.",
            turn=0,
            tool_name=f"@{name}",
            success=False,
        )

    def agent_result(self, result) -> None:
        # No-op. The delegate path (``_dispatch_agent`` →
        # ``tool_delegate``) already emits the final answer through
        # the renderer's observation channel — re-emitting here would
        # surface the same body twice in the chat thread.
        del result

    def skill_not_found(self, name: str) -> None:
        self.renderer.observation(
            f"Unknown command: /{name}. Type /help for available commands.",
            turn=0,
            tool_name=f"/{name}",
            success=False,
        )

    def skill_result(self, name: str, result) -> None:
        # Same rationale as ``agent_result``: ``_dispatch_skill`` calls
        # ``render_group_end`` which the frontend uses to close the
        # nested skill panel. Re-emitting the answer here would
        # duplicate. The ``None`` (stopped without final) case is
        # already visible via the unsuccessful group_end.
        del name, result


def _handle_sh(message: str, renderer: WebRenderer) -> bool:
    """``/sh <command>`` — run a shell command, render output as a
    tool-result observation card."""
    cmd = message[3:].lstrip()
    if not cmd:
        renderer.observation(
            "Usage: /sh <command>",
            turn=0,
            tool_name="sh",
            success=False,
        )
        return True
    try:
        # Bytes + replace, not ``text=True``: strict UTF-8 decode raises
        # UnicodeDecodeError mid-run on non-UTF-8 output (e.g. ``git show``,
        # binary diffs), which TimeoutExpired wouldn't catch.
        result = subprocess.run(
            cmd,
            shell=True,
            capture_output=True,
            timeout=SHELL_COMMAND_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        renderer.observation(
            f"Command timed out ({SHELL_COMMAND_TIMEOUT}s)",
            turn=0,
            tool_name="sh",
            success=False,
        )
        return True
    stdout = result.stdout.decode("utf-8", errors="replace")
    stderr = result.stderr.decode("utf-8", errors="replace")
    parts: list[str] = []
    if stdout:
        parts.append(stdout)
    if stderr:
        parts.append(stderr)
    if result.returncode != 0:
        parts.append(f"[exit code: {result.returncode}]")
    output = "".join(parts) or "(no output)"
    renderer.observation(
        output,
        turn=0,
        tool_name="sh",
        success=result.returncode == 0,
    )
    return True
