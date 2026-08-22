"""Hook system — PreToolUse, PostToolUse, PostToolUseFailure lifecycle hooks.

Hooks are shell commands that execute at specific lifecycle points.
They receive JSON on stdin and communicate via exit codes:
  - exit 0: allow (proceed)
  - exit 2: block (PreToolUse only)
  - other: non-blocking error (proceed with warning)

Configuration: .agent-cli/hooks.json (project) + ~/.agent-cli/hooks.json
(global) — both fire, merged per event, project matchers first (v8.40.0)
"""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class HookEntry:
    """A single hook command to execute."""

    command: str
    timeout: int = 30


@dataclass
class HookMatcher:
    """A matcher that filters which tools trigger the hooks."""

    matcher: str  # regex pattern; empty = match all
    hooks: list[HookEntry] = field(default_factory=list)


@dataclass
class HookResult:
    """Result of running hooks for an event."""

    allowed: bool = True
    updated_input: dict | None = None
    stderr: str | None = None


# Search paths for hooks config (경로쌍 단일 소스 — v8.40.0, 리뷰 §4.5)
from agent_cli.paths import scoped_paths

_HOOKS_PATHS = scoped_paths("hooks.json")

_cached_hooks: dict[str, list[HookMatcher]] | None = None


def _parse_hooks_file(hooks_path: Path) -> dict[str, list[HookMatcher]]:
    """hooks.json 한 파일 → {event: [HookMatcher]} (파싱 관용은 종전 동일)."""
    result: dict[str, list[HookMatcher]] = {}
    try:
        data = json.loads(hooks_path.read_text(encoding="utf-8"))
    except Exception:
        return result
    if not isinstance(data, dict):
        return result

    for event_name, matchers_raw in data.items():
        if not isinstance(matchers_raw, list):
            continue
        matchers = []
        for m in matchers_raw:
            if not isinstance(m, dict):
                continue
            hooks_list = [
                HookEntry(
                    command=h.get("command", ""),
                    timeout=int(h.get("timeout", 30)),
                )
                for h in m.get("hooks", [])
                if isinstance(h, dict) and h.get("command")
            ]
            if hooks_list:
                matchers.append(
                    HookMatcher(
                        matcher=m.get("matcher", ""),
                        hooks=hooks_list,
                    )
                )
        if matchers:
            result[event_name] = matchers
    return result


def load_hooks(use_cache: bool = True) -> dict[str, list[HookMatcher]]:
    """Load hook configuration from disk — 프로젝트+사용자 **둘 다 발화**.

    v8.40.0 병합 규칙 통일(사용자 결정): 종전엔 첫 발견 파일만 읽어
    프로젝트 hooks.json 이 존재하는 순간 사용자 전역 파일(안전 훅 포함)이
    통째로 무시됐다. 이제 이벤트별로 두 파일의 matcher 를 **연결 병합** —
    프로젝트 훅이 먼저, 사용자 전역 훅이 뒤에 발화한다 (Python 훅
    디렉토리의 '프로젝트 → 사용자 둘 다 실행'·스킬 frontmatter 훅의
    ``merge_hooks_configs`` 연결 병합과 동형 철학). 한쪽만 있으면 종전과
    동일하게 그 파일만 쓰인다.
    """
    global _cached_hooks
    if use_cache and _cached_hooks is not None:
        return _cached_hooks

    result: dict[str, list[HookMatcher]] = {}
    for hooks_path in _HOOKS_PATHS:  # [프로젝트, 사용자] — 발화 순서
        if not hooks_path.is_file():
            continue
        for event_name, matchers in _parse_hooks_file(hooks_path).items():
            result.setdefault(event_name, []).extend(matchers)

    _cached_hooks = result
    return result


def merge_hooks_configs(
    *configs: dict[str, list[HookMatcher]] | None,
) -> dict[str, list[HookMatcher]] | None:
    """Combine multiple hooks_config dicts by concatenating matcher lists.

    Later configs append their matchers after earlier ones, so calling
    `merge_hooks_configs(parent, skill)` yields parent's matchers first,
    then skill-local ones — both fire for the same event. Returns None
    when every input is empty so callers can stay on the `if cfg:` path.
    """
    merged: dict[str, list[HookMatcher]] = {}
    for cfg in configs:
        if not cfg:
            continue
        for event, matchers in cfg.items():
            if not matchers:
                continue
            merged.setdefault(event, []).extend(matchers)
    return merged or None


def parse_hooks_config(
    raw: dict,
) -> dict[str, list[HookMatcher]]:
    """Parse hooks from a raw dict (e.g. skill frontmatter hooks field)."""
    result: dict[str, list[HookMatcher]] = {}
    if not isinstance(raw, dict):
        return result

    for event_name, matchers_raw in raw.items():
        if not isinstance(matchers_raw, list):
            continue
        matchers = []
        for m in matchers_raw:
            if not isinstance(m, dict):
                continue
            hooks_list = [
                HookEntry(
                    command=h.get("command", ""),
                    timeout=int(h.get("timeout", 30)),
                )
                for h in m.get("hooks", [])
                if isinstance(h, dict) and h.get("command")
            ]
            if hooks_list:
                matchers.append(
                    HookMatcher(
                        matcher=m.get("matcher", ""),
                        hooks=hooks_list,
                    )
                )
        if matchers:
            result[event_name] = matchers

    return result


def run_hooks(
    event: str,
    tool_name: str,
    tool_input: dict,
    hooks_config: dict[str, list[HookMatcher]] | None = None,
    tool_result: str | None = None,
) -> HookResult:
    """Run all matching hooks for an event. Returns aggregated result."""
    if hooks_config is None:
        hooks_config = load_hooks()

    matchers = hooks_config.get(event, [])
    if not matchers:
        return HookResult(allowed=True)

    for matcher in matchers:
        # Check if tool matches
        if matcher.matcher and not re.search(matcher.matcher, tool_name):
            continue

        for hook_entry in matcher.hooks:
            stdin_data = json.dumps(
                {
                    "hook_event_name": event,
                    "tool_name": tool_name,
                    "tool_input": tool_input,
                    **({"tool_result": tool_result} if tool_result else {}),
                },
                ensure_ascii=False,
            )

            result = _execute_hook_command(hook_entry, stdin_data)

            if not result.allowed:
                return result

            if result.updated_input is not None:
                return result

    return HookResult(allowed=True)


def _execute_hook_command(hook: HookEntry, stdin_data: str) -> HookResult:
    """Execute a single hook command and interpret result."""
    try:
        proc = subprocess.run(
            hook.command,
            shell=True,
            input=stdin_data,
            capture_output=True,
            text=True,
            timeout=hook.timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return HookResult(allowed=True, stderr="Hook timed out")
    except Exception as e:
        return HookResult(allowed=True, stderr=str(e))

    if proc.returncode == 2:
        return HookResult(allowed=False, stderr=proc.stderr.strip())

    # Parse stdout for JSON response (updatedInput, etc.)
    if proc.stdout.strip():
        try:
            output = json.loads(proc.stdout.strip())
            updated = output.get("updatedInput")
            if isinstance(updated, dict):
                return HookResult(allowed=True, updated_input=updated)
        except (json.JSONDecodeError, AttributeError):
            pass

    return HookResult(allowed=True)
