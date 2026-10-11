"""내장 훅 — 워크스페이스 git 스냅샷 (docs/snapshot/DESIGN.md, v10.36.0).

- ``OnSessionStart``: 저장소 준비 + 시작 커밋. **성공했을 때만** 모델에게 안내(`snapshot` 도구로 복구)를
  주입한다(없는 사본을 약속하지 않는다 — 실험 2 에서 안내만 있고 사본이 없자 모델이
  없는 것을 찾다 기준선보다 빨리 실패했다). 끔(`AGENT_CLI_SNAPSHOT=0`)·git 없음·
  실패는 사람에게만 한 줄.
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
_announced: set[Path] = set()


def _enabled() -> bool:
    from agent_cli.constants import snapshot_knobs

    return snapshot_knobs()["enabled"]


def _say(msg: str) -> None:
    """사람에게만 — 콘솔/웹 상태줄. 모델 컨텍스트엔 안 들어간다."""
    try:
        from agent_cli.render import get_renderer

        get_renderer().status("running", msg)
    except Exception:
        pass


def on_session_start(ctx):
    if not _enabled():
        return
    from agent_cli.snapshot import get_snapshot

    ws = Path.cwd().resolve()
    store = get_snapshot(ws, create=True)
    if store is None or not store.enabled:
        if ws not in _announced:
            _announced.add(ws)
            _say(f"📸 스냅샷 꺼짐 — {store.reason if store else 'unavailable'}")
        return
    if ws not in _announced:
        _announced.add(ws)
        _say(
            f"📸 스냅샷 준비 — {store.git_dir.relative_to(ws)} "
            f"({store.stats.seconds:.1f}s, 하위 저장소 {len(store.nested)}개)"
        )
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
