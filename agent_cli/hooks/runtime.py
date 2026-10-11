"""세션 단위 Python 훅 러너 — 네 루프(main·상주·스킬·일회성)가 같은 인스턴스를 본다.

`monitor.runtime` 과 같은 모양이다. 종전엔 `HookRunner` 가 어느 루프에도 꽂히지
않았다(`ports_for_*` 의 `hook_runner=None`, 사유 "배선만 준비 — 응용이 생기면
연결한다", 2026-09). 첫 응용이 워크스페이스 스냅샷(docs/snapshot/DESIGN.md)이다 —
서브에이전트의 shell 도 입력을 망치므로 main 만으론 부족해 **네 루프 전부**가 같은
러너를 쓴다(v10.35.0).

셸 훅(`hooks.json`)은 종전대로 `LoopConfig.hooks_config` 경로로 돌며 여기와 무관하다
— 러너에 `shell_hooks_config` 를 주지 않아 두 번 돌지 않는다.
"""

from __future__ import annotations

from agent_cli.hooks.runner import HookRunner

_RUNNER: HookRunner | None = None


def set_hook_runner(runner: HookRunner | None) -> None:
    global _RUNNER
    _RUNNER = runner


def get_hook_runner() -> HookRunner | None:
    return _RUNNER
