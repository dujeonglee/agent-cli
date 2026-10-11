"""내장 훅 — 워크스페이스 git 스냅샷 (docs/snapshot/DESIGN.md, v10.36.0).

- ``OnSessionStart``: 부트스트랩(`runtime.build_snapshot_store`)이 세운 저장소가 있을
  때만 모델에게 안내(`snapshot` 도구로 복구)를 주입한다(없는 사본을 약속하지 않는다
  — 실험 2 에서 안내만 있고 사본이 없자 모델이 없는 것을 찾다 기준선보다 빨리
  실패했다). 저장소를 부트스트랩에서 세우는 이유: `snapshot` 도구의 광고 여부
  (`requires_handler="snapshot"`)가 LoopConfig 조립 때 정해지기 때문. 끔·git 없음·
  실패의 사람용 한 줄도 거기서 난다.
- ``PreToolUse``: shell·edit_file·write_file 직전 체크포인트(최소 간격 안이면 건너뜀).
- ``OnSessionEnd``: 마지막 체크포인트 — 세션 끝 상태도 복원 가능하게.

여러 루프(main·상주·스킬·일회성)가 같은 세션 러너를 쓰므로 이 훅은 루프마다 불린다
— `get_snapshot` 이 워크스페이스당 하나를 돌려주고 `init` 은 한 번만 일어난다.
함수 이름은 ``EVENT_TO_FUNC`` 대로(``on_session_start`` / ``pre_tool_use`` /
``on_session_end``).
"""

from __future__ import annotations

from pathlib import Path

EVENTS = ["OnSessionStart", "PreToolUse", "OnSessionEnd"]

MUTATING_TOOLS = ("shell", "edit_file", "write_file")
_SECTION = "Workspace snapshot"


def _enabled() -> bool:
    from agent_cli.constants import snapshot_knobs

    return snapshot_knobs()["enabled"]


def on_session_start(ctx):
    """저장소는 부트스트랩(`runtime.build_snapshot_store`)이 세웠다 — 여기선 안내만.
    루프마다 불리지만(서브에이전트도) 저장소는 워크스페이스당 하나다."""
    if not _enabled():
        return
    from agent_cli.snapshot import get_snapshot

    store = get_snapshot(Path.cwd())
    if store is None or not store.enabled:
        return  # 없는 사본을 약속하지 않는다 — 도구도 같은 이유로 벗겨져 있다
    ctx.inject_system_section(_SECTION, store.notice())


def pre_tool_use(ctx):
    if ctx.tool_name not in MUTATING_TOOLS or not _enabled():
        return
    from agent_cli.snapshot import get_snapshot

    store = get_snapshot(Path.cwd())
    if store is not None and store.enabled:
        store.checkpoint(f"before {ctx.tool_name} (turn {ctx.turn})")


def on_session_end(ctx):
    if not _enabled():
        return
    from agent_cli.snapshot import get_snapshot

    store = get_snapshot(Path.cwd())
    if store is not None and store.enabled:
        store.checkpoint(f"session end (turn {ctx.turn})", force=True)
