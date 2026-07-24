"""In-process subagent delegation tool.

Supports context modes: none (independent), fork (copy conversation history).
Uses tasks array API: single item = sync, multiple items = parallel (threading).
"""

from __future__ import annotations

import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

from agent_cli.tools.result import ToolResult

# ── Agent file loading ──────────────────────────


@dataclass
class DelegateResult:
    """Structured result from delegate execution."""

    output: str | None = None
    iterations: int = 0
    duration_secs: float = 0.0
    activity_log: list[str] = field(default_factory=list)
    last_actions: list[str] = field(default_factory=list)


# Caps for the per-iteration summaries handed back to the parent
# loop in ``DelegateResult``. The parent splices these into its
# observation card, so they're token cost in the parent's context —
# a runaway sub-agent (50+ iterations) would otherwise drown out
# the actual answer. Module-level so the values are discoverable
# without spelunking through the extractor body.
_ACTIVITY_LOG_MAX_ENTRIES = 20
_LAST_ACTIONS_KEEP = 5


def _extract_activity_log(messages: list[dict]) -> list[str]:
    """Extract per-iteration action summaries from raw history records.

    Reads each assistant record's structured ops via
    :func:`~agent_cli.context.manager.iter_record_ops` (multi-op ``ops``
    records and the base singular ``action`` shape both) and formats one
    line per TURN — a multi-op turn joins its op summaries with ``"; "``.
    Capped at ``_ACTIVITY_LOG_MAX_ENTRIES``; overflow surfaces as a single
    ``... and N more`` footer so the parent knows the log was trimmed.

    Returns list of strings like:
      ["iter 1: read_file auth.py", "iter 2: shell pytest"]
    """
    from agent_cli.context.records import iter_record_ops

    log: list[str] = []
    iter_num = 0

    for msg in messages:
        pairs = iter_record_ops(msg)
        if not pairs:
            continue
        iter_num += 1
        summary = "; ".join(_summarize_action(a, ai) for a, ai in pairs)
        log.append(f"iter {iter_num}: {summary}")

    if len(log) > _ACTIVITY_LOG_MAX_ENTRIES:
        trimmed = log[:_ACTIVITY_LOG_MAX_ENTRIES]
        trimmed.append(f"... and {len(log) - _ACTIVITY_LOG_MAX_ENTRIES} more")
        return trimmed
    return log


def _summarize_action(action: str, action_input: dict) -> str:
    """Format a single action into a one-line summary."""
    if not isinstance(action_input, dict):
        return action

    path = action_input.get("path", "")
    if action == "read_file" and path:
        return f"read_file {Path(path).name}"
    elif action in ("write_file", "edit_file") and path:
        return f"{action} {Path(path).name}"
    elif action == "shell":
        cmd = action_input.get("command", "")
        return f"shell {cmd[:60]}" if cmd else "shell"
    elif action == "agent":
        task = action_input.get("task", "") or action_input.get("message", "")
        return f'agent "{task[:40]}"' if task else "agent"
    else:
        return action


def _extract_last_actions(messages: list[dict]) -> list[str]:
    """Extract the last ``_LAST_ACTIONS_KEEP`` actions with their
    observation results.

    Distinct from ``_extract_activity_log`` in two ways: (1) it's
    intentionally a tail-slice, not a head-with-overflow, since the
    parent uses these lines to spot the most recent failures, and
    (2) it scrapes the immediately-following user-role message for
    error keywords (``ERROR``/``FAIL``/``EXCEPTION``/``TRACEBACK``)
    and appends a one-line hint when found.

    Returns list of strings like:
      ["iter 4: shell pytest -> ERROR: 3 tests failed",
       "iter 5: edit_file test_auth.py -> hash mismatch"]
    """
    from agent_cli.context.records import iter_record_ops

    actions: list[tuple[int, int, str]] = []
    iter_num = 0
    for i, msg in enumerate(messages):
        pairs = iter_record_ops(msg)
        if not pairs:
            continue
        iter_num += 1
        summary = "; ".join(_summarize_action(a, ai) for a, ai in pairs)
        actions.append((i, iter_num, summary))

    last_n = actions[-_LAST_ACTIONS_KEEP:]

    result: list[str] = []
    for msg_idx, it, summary in last_n:
        obs_hint = ""
        if msg_idx + 1 < len(messages) and messages[msg_idx + 1].get("role") == "user":
            obs = messages[msg_idx + 1].get("content", "")
            if not isinstance(obs, str):
                obs = ""
            for line in obs.split("\n")[:5]:
                if any(
                    kw in line.upper()
                    for kw in ["ERROR", "FAIL", "EXCEPTION", "TRACEBACK"]
                ):
                    obs_hint = f" → {line.strip()[:80]}"
                    break
        result.append(f"iter {it}: {summary}{obs_hint}")

    return result


def _generate_run_dir_name(agent_name: str) -> str:
    """Generate a unique run directory name: run_{name}_{hash}_{ts}"""
    import os

    name = agent_name or "task"
    hash_part = os.urandom(3).hex()  # 6-char hex
    ts = time.strftime("%Y%m%dT%H%M%S", time.gmtime())
    ms = f"{int(time.time() * 1000) % 1000:03d}"
    return f"run_{name}_{hash_part}_{ts}{ms}"


def _resolve_session_dir(session, parent_ctx) -> Path:
    """Determine session directory from session or parent context."""
    if session and hasattr(session, "session_dir"):
        return Path(session.session_dir)
    if parent_ctx and hasattr(parent_ctx, "session_dir"):
        return parent_ctx.session_dir
    return Path(tempfile.mkdtemp(prefix="delegate_"))


def _persist_run_result(
    formatted: str,
    run_dir: Path,
) -> None:
    """Save delegate result as result.md in delegate directory."""
    try:
        run_dir.mkdir(parents=True, exist_ok=True)
        result_path = run_dir / "result.md"
        result_path.write_text(formatted, encoding="utf-8")
    except Exception:
        pass


def extract_result_body(formatted: str) -> str:
    """run 관찰 문자열에서 서브에이전트의 **원문 답**만 역추출.

    ``STATUS: success\nRESULT:\n<본문>\n\n[Subagent activity]…`` 포맷의
    역 — 포맷터(:func:`_format_delegate_output` + oneshot 의 STATUS 랩)와
    같은 모듈이 소유해 드리프트를 막는다. directive 생성(웹 에디터)이
    run 결과에서 초안 본문을 꺼낼 때 소비. 포맷이 아니면 그대로 반환.
    """
    body = formatted
    if "RESULT:\n" in body:
        body = body.split("RESULT:\n", 1)[1]
    for section in (
        "\n\n[Subagent activity]",
        "\n\n[Last actions before failure]",
        "\n\n[Files touched]",
        "\n\n[Duration",
    ):
        idx = body.find(section)
        if idx != -1:
            body = body[:idx]
    return body.strip()


def _format_delegate_output(result: DelegateResult) -> str:
    """Format DelegateResult into observation string."""
    parts = []

    # 1. Output
    if result.output:
        parts.append(result.output)
    else:
        parts.append("(subagent returned no result)")

    # 2. Activity log
    if result.activity_log:
        parts.append("")
        parts.append("[Subagent activity]")
        for entry in result.activity_log:
            parts.append(f"- {entry}")

    # 3. Last actions on failure
    if result.last_actions:
        parts.append("")
        parts.append("[Last actions before failure]")
        for entry in result.last_actions:
            parts.append(f"- {entry}")

    # 4. Duration + Iterations
    footer = []
    if result.duration_secs > 0:
        footer.append(f"[Duration: {result.duration_secs:.1f}s]")
    if result.iterations > 0:
        footer.append(f"[Subagent used {result.iterations} iterations]")
    if footer:
        parts.append("")
        parts.append(" ".join(footer))

    return "\n".join(parts)


def _format_parallel_results(
    specs: list[dict], results: list[ToolResult | None]
) -> ToolResult:
    """Combine multiple delegate results into a single observation."""
    parts: list[str] = []
    succeeded = 0
    failed = 0

    for i, (spec, result) in enumerate(zip(specs, results)):
        label = spec["task"][:80]
        parts.append(f"[Task {i + 1}] {label}")
        if result and result.success:
            parts.append(result.output or "(no output)")
            succeeded += 1
        else:
            error = result.error if result else "Thread timed out or crashed"
            parts.append(f"ERROR: {error}")
            failed += 1
        parts.append("")

    summary = f"[Parallel execution: {len(specs)} tasks"
    if failed == 0:
        summary += ", all succeeded]"
    else:
        summary += f", {succeeded} succeeded, {failed} failed]"
    parts.append(summary)

    combined = "\n".join(parts)
    if failed == 0:
        return ToolResult(True, output=f"STATUS: success\nRESULT:\n{combined}")
    else:
        return ToolResult(False, error=f"STATUS: error\n{combined}")
