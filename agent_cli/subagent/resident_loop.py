"""상주 에이전트의 루프 종류 — `ResidentLoop` (`LoopKind`, v10.87.0).

에이전트에 하나(워커 스레드가 만든다), ``run(start)`` 은 받은편지함 묶음마다. 주소
``agent:<key>``, 깊이는 spawn 시점 호출자 +1(종전엔 런마다 "가장 최근 spawner 의 깊이 +1"
을 다시 읽어 흔들렸다), 바인딩은 프로파일이 모델을 고정하면 그 복사본(한 번만 계산),
도구는 프로파일 allowlist(엔진이 `message`·`reply` 를 강제 탑재), 포트는 `make_ports`
(종전 `runtime.ports_for_resident`). ctx 는 워커 스레드에서 `open()` 으로 한 번 만든다 —
`create_subagent_ctx` 의 `note_scope_ctx` 가 그 스레드의 스코프(에이전트 키)에 등록되게.

런의 단계: ⑫ `start(items)` 가 받은편지함 항목(단건 또는 사람-직접 묶음)을 시작 메시지·
회계(`user_requests`·`request_id`)로 바꾸고, ⑭ 스코프는 작업 카드(`begin_agent_work`/
`end_agent_work`), ⑰ 결과는 ``(ToolResult, secs)`` — 회신·빚·`run_ended` 후처리는 아직
레지스트리(`_handle_request`·`_handle_human_batch`)에 있다(PR 3). 런마다 거둘 것은 없다
(상주는 잡·모니터를 런 너머로 가진다) — 에이전트가 죽을 때 워커가 `_drop_monitors` 로 거둔다.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from agent_cli.loop.kind import LoopKind, LoopKindError, Start
from agent_cli.tools.result import ToolResult

if TYPE_CHECKING:
    from agent_cli.loop.ports import LoopPorts


def _is_human_addr(author: str) -> bool:
    return author == "user" or str(author).startswith("user:")


class ResidentLoop(LoopKind):
    verbose = False
    graceful_interrupt = False

    def __init__(self, tm, registry, *, disp: str) -> None:
        from agent_cli.prompts.system_prompt import build_peer_roster_intro

        self.tm = tm
        self.registry = registry
        self.disp = disp
        rt = registry.runtime
        self.owner = f"agent:{tm.key}"
        self.depth = rt.get("depth", 0) + 1
        self.max_depth = rt.get("max_depth", 2)
        self.max_turns = rt.get("max_turns", 0)
        self.session = rt.get("session")
        self.compaction_enabled = rt.get("compaction_enabled", True)
        self.binding = rt.get(
            "binding"
        )  # 프로파일 고정은 `open()` 에서 (실패 = 사망 사유)
        self.tools = tm.allowed_tools
        self.hooks_config = tm.hooks_config
        self.stop_event = tm.stop_event
        self.agent_name = tm.profile_name
        self.agent_role = tm.role_prompt
        # 시스템 프롬프트에는 **안내만**(정적, 캐시 친화); 실제 로스터는 매 턴 꼬리(`peer_roster`).
        self.peer_agents_section = build_peer_roster_intro()
        self.ports = self.make_ports(
            key=tm.key,
            message_handler=registry._make_message_handler(tm),
            questions=registry.question_port(tm.key),
            absorb_inbox=lambda: registry.absorb_pending(tm),
            peer_roster=self.peer_roster,
        )
        self._items: list[dict] = []

    @staticmethod
    def make_ports(
        *, key: str, message_handler, questions, absorb_inbox, peer_roster
    ) -> LoopPorts:
        """상주 런의 포트. ``absorb_inbox`` (v9.25.0) 가 턴 경계마다 받은편지함을 이 런으로
        흡수하고, ``peer_roster`` (v10.1.0) 가 매 턴 꼬리에 실을 동료 로스터를 돌려준다."""
        from agent_cli.loop.ports import LoopPorts
        from agent_cli.runtime import (
            _AS_BEFORE,
            _NO_REGISTRY_IN_SUBLOOP,
            _WEB_ONLY,
            _hook_runner_port,
            _hook_unwired,
        )

        return LoopPorts(
            owner=f"agent:{key}",
            message_handler=message_handler,
            questions=questions,
            absorb_inbox=absorb_inbox,
            peer_roster=peer_roster,
            agent_registry=None,
            mcp_manager=None,
            hook_runner=_hook_runner_port(),
            route_message=None,
            dequeue_user_message=None,
            unwired={
                "agent_registry": _NO_REGISTRY_IN_SUBLOOP,
                "mcp_manager": _AS_BEFORE,
                **_hook_unwired(),
                "route_message": _WEB_ONLY,
                "dequeue_user_message": _WEB_ONLY,
            },
        )

    def _engine(self):
        # 레지스트리의 러너(테스트 DI — 가짜 러너)는 **호출 시점**에 읽는다: spawn 뒤에 바꿔
        # 끼우는 테스트가 있고, None 이면 `run_loop`.
        runner = self.registry._runner
        return runner if runner is not None else self._run_loop

    def peer_roster(self) -> str:
        from agent_cli.prompts.system_prompt import build_live_agents_section

        return build_live_agents_section(
            self.registry,
            exclude_key=self.tm.key,
            via_message_tool=True,
            include_state=True,
        )

    # ── 에이전트 수명: ctx 한 번 ──
    def open(self, parent_ctx) -> None:
        """워커 스레드에서 — 바인딩 고정·ctx 생성. 실패는 `LoopKindError`(= 사망 사유)."""
        from agent_cli.subagent.runner import create_subagent_ctx

        tm = self.tm
        # resume 모드: 자기 history 를 그대로 이어받아 이전 세션의 문답을 전부 기억한 채 살아난다
        mode = "resume" if tm.revive else tm.context_mode
        binding = self.binding
        if binding is not None and tm.model and tm.model != binding.model:
            try:
                binding = binding.pinned(tm.model)
            except KeyError as exc:
                raise LoopKindError(str(exc.args[0] if exc.args else exc)) from exc
        ctx, err = create_subagent_ctx(mode, parent_ctx, tm.home_dir, binding=binding)
        if ctx is None:
            raise LoopKindError(err)
        self.binding = binding
        self.ctx = ctx
        tm.ctx = ctx

    # ── ⑫ 시작 — 받은편지함 항목 → 시작 메시지 + 회계 ──
    def start(self, items: list[dict]) -> Start:
        """단건이면 그 항목, 사람-직접 묶음(2건+)이면 전부 ``user:*`` 라 한 질의로 합친다 —
        항목마다 회계 id 가 올라 꼬리의 `## Open Requests` 와 `answers` 강제가 그 일을 한다."""
        reg = self.registry
        self._items = list(items)
        if len(items) == 1:
            item = items[0]
            author = item.get("author", "main")
            human = _is_human_addr(author)
            expects = item.get("expects_reply", True)
            return Start(
                query=reg._model_text(item),
                images=list(item.get("images") or []),
                author_is_user=human,
                # 시작 항목의 장부 id — 그 레코드가 열려 있는 동안 핀된다
                request_id=item["id"] if human or expects else "",
                user_requests=(
                    [
                        {
                            "id": item["id"],
                            "author": author,
                            "text": reg._model_text(item, bare=True),
                        }
                    ]
                    if human
                    else []
                ),
            )
        user_requests = [
            {
                "id": it["id"],
                "author": it.get("author", ""),
                "text": reg._model_text(it, bare=True),
            }
            for it in items
        ]
        return Start(
            query="\n\n".join(reg._model_text(it) for it in items),
            # 배치의 이미지는 항목 순서대로 한 레코드에 (v10.61.0)
            images=[r for it in items for r in (it.get("images") or [])],
            author_is_user=True,
            # 한 레코드에 여럿이 합쳐진다 — 첫 요청 id 로 찍는다(핀)
            request_id=user_requests[0]["id"],
            user_requests=user_requests,
        )

    # ── ⑭ 스코프 — 작업 카드 ──
    def open_scope(self, start: Start):
        from agent_cli.render import get_renderer

        items = self._items
        first = items[0]
        renderer = get_renderer()
        renderer.begin_agent_work(
            key=self.tm.key,
            item=first["id"],
            profile=self.disp,
            message=(
                first["text"]
                if len(items) == 1
                else "\n".join(self.registry._model_text(it) for it in items)
            ),
            # 요청 발신 시각을 scope_start(nav_ts)로 실어 스윔레인 화살표가 이 카드로 오게
            req_ts=first.get("ts"),
            items=[it["id"] for it in items] if len(items) > 1 else None,
        )
        return renderer

    def close_scope(self, renderer, finished, secs: float) -> None:
        result, duration = finished if finished is not None else (None, 0.0)
        success = bool(result is not None and result.success)
        output = (
            result.output
            if result is not None and result.output is not None
            else "(agent did not complete the request)"
        )
        renderer.end_agent_work(
            key=self.tm.key,
            item=self._items[0]["id"],
            success=success,
            duration_s=duration,
            error="" if success else output[:200],
        )

    # ── ⑰ 결과 — (ToolResult, secs); 회신·빚·run_ended 는 레지스트리 (PR 3) ──
    def finish(self, result, secs: float, released: str) -> tuple[Any, float]:
        return result, secs

    def crashed(self, exc: BaseException, secs: float) -> tuple[Any, float]:
        # worker 는 죽지 않는다 — 회신으로 보고 (종전 `_handle_request` 의 except)
        return (
            ToolResult(
                False, output=f"agent internal error: {type(exc).__name__}: {exc}"
            ),
            0.0,
        )
