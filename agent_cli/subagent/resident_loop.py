"""에이전트의 루프 종류 — `ResidentLoop` (`LoopKind`, v10.87.0). run·spawn 에이전트 전부의 것이다
(v10.141.0, docs/agent-engine/DESIGN.md — 차이는 에이전트 값 ``ends_after_task`` 하나이고 그건
레지스트리의 워커가 본다).

에이전트에 하나(워커 스레드가 만든다), ``run(start)`` 은 받은편지함 묶음마다. 주소
``agent:<key>``, 깊이는 띄운 호출자 +1(`AgentInstance.caller_runtime`; 종전엔 런마다 "가장 최근
spawner 의 깊이 +1" 을 다시 읽어 흔들렸다), 스택은 호출자의 것(엔진이 자기 프로파일을 얹는다),
바인딩은 프로파일이 모델을 고정하면 그 복사본(한 번만 계산),
도구는 프로파일 allowlist(엔진이 `message`·`reply` 를 강제 탑재), 포트는 `make_ports`
(종전 `runtime.ports_for_resident`). ctx 는 워커 스레드에서 `open()` 으로 한 번 만든다 —
`create_subagent_ctx` 의 `note_scope_ctx` 가 그 스레드의 스코프(에이전트 키)에 등록되게.

런의 단계: ⑫ `start(items)` 가 받은편지함 항목(단건 또는 사람-직접 묶음)을 시작 메시지·
회계(`user_requests`·`request_id`)로 바꾸고, ⑭ 스코프는 작업 카드(`begin_agent_work`/
`end_agent_work`), ⑰ `finish` 가 회신 계약·빚·`run_ended`·`_close_run` 후처리까지 한 벌로 한다(v10.89.0). 런마다 거둘 것은 없다
(상주는 잡·모니터를 런 너머로 가진다) — 에이전트가 죽을 때 워커가 `_drop_monitors` 로 거둔다.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

from agent_cli.actor import RunEnd
from agent_cli.loop.kind import (
    WAIT_NEXT_KEEP_WORKING,
    WAIT_NEXT_RESIDENT,
    LoopKind,
    LoopKindError,
    PromptProfile,
    Start,
)
from agent_cli.main_input import CLOSED
from agent_cli.session.records import record_restart
from agent_cli.session_slots import get_agent_registry
from agent_cli.subagent import agent_stop
from agent_cli.tools.result import ToolResult
from agent_cli.wake import Mailbox

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
        rt = tm.caller_runtime or registry.runtime
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
        self.agent_stack = list(tm.agent_stack)
        self.agent_name = tm.profile_name
        self.agent_role = tm.role_prompt
        # 시스템 프롬프트에는 **안내만**(정적, 캐시 친화); 실제 로스터는 매 턴 꼬리(`peer_roster`).
        self.peer_agents_section = build_peer_roster_intro()
        self.ports = self.make_ports(
            key=tm.key,
            message_handler=registry.message_handler(f"agent:{tm.key}"),
            questions=registry.question_port(tm.key),
            inbox=lambda: registry.absorb_pending(tm),
            peer_roster=self.peer_roster,
            agent_registry=registry,
        )
        self._items: list[dict] = []
        #: 지금 런의 정지(■ — `AgentRegistry.stop_run`)와 그 런이 기록을 시작한 서수(멈췄을 때 회신에 실을
        #: 마지막 호출·생각을 읽는다). 런마다 `start` 가 새로 세운다.
        self._run_stop: agent_stop.LinkedStop | None = None
        self._run_from = 0

    @staticmethod
    def make_ports(
        *, key: str, message_handler, questions, inbox, peer_roster, agent_registry
    ) -> LoopPorts:
        """상주 런의 포트. ``inbox`` (v9.25.0, 메일함 포트의 ``take``) 가 턴 경계마다 받은편지함을 이 런으로
        흡수하고, ``peer_roster`` (v10.1.0) 가 매 턴 꼬리에 실을 동료 로스터를 돌려준다."""
        from agent_cli.loop.ports import (
            _AS_BEFORE,
            _WEB_ONLY,
            LoopPorts,
            _hook_runner_port,
            _hook_unwired,
        )

        return LoopPorts(
            owner=f"agent:{key}",
            message_handler=message_handler,
            questions=questions,
            peer_roster=peer_roster,
            mailbox=Mailbox(get_agent_registry(), f"agent:{key}", take=inbox),
            # `agent` 도구의 권한 — run·spawn 이 모든 에이전트의 표면이다 (v10.141.0, 설계 §1).
            # 메일함·꼬리 로스터는 이 종류가 따로 정한다(`intake`·`tail_roster`).
            agent_registry=agent_registry,
            mcp_manager=None,
            hook_runner=_hook_runner_port(),
            route_message=None,
            enqueue_user_message=None,
            unwired={
                "mcp_manager": _AS_BEFORE,
                **_hook_unwired(),
                "route_message": _WEB_ONLY,
                "enqueue_user_message": _WEB_ONLY,
            },
        )

    def _engine(self):
        # 레지스트리의 러너(테스트 DI — 가짜 러너)는 **호출 시점**에 읽는다: spawn 뒤에 바꿔
        # 끼우는 테스트가 있고, None 이면 `run_loop`.
        runner = self.registry._runner
        return runner if runner is not None else self._run_loop

    # ── 엔진이 묻는 것 (v10.88.0) ──
    def wait_refused_next(self) -> str:
        """run 에이전트는 런이 끝나면 내려간다; 상주는 다음 메시지가 새 런을 연다(ctx 는 수명에 하나)."""
        return WAIT_NEXT_KEEP_WORKING if self.tm.ends_after_task else WAIT_NEXT_RESIDENT

    def session_ended(self) -> bool:
        """세션이 끝나는 중인가 — main 상자가 닫혔다(`shutdown_all` 이 맨 먼저 닫는다, 5e1). main 과 같은 사실이다."""
        return self.registry._post.is_closed("main")

    def intake(self) -> list[dict]:
        """런 도중 받은편지함에 도착한 항목을 지금 흡수한다 (v9.25.0) — 11시간 런 동안
        오케스트레이터 메시지 8건이 안 읽히던 것."""
        return list(self.ports.mailbox.take() or [])

    def prompt_profile(self) -> PromptProfile:
        return PromptProfile(
            role=self.tm.role_prompt,
            peer_section=self.peer_agents_section,
            ask_blocking=False,  # 질문 포트로 비차단
            reports_to_caller=True,
            peer_channel=True,  # 상주 모드 거절은 `message` 동료 채널을 가리킨다
        )

    def tail_roster(self, tools: list[str]) -> str:
        return self.peer_roster()

    def renders(self) -> bool:
        return True

    @property
    def tracks_run_authors(self) -> bool:
        return False

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
        if tm.restarted:  # 재시작이 되살렸다 — 컨텍스트 끝에 사실 기록 한 줄(5c-0)
            record_restart(ctx)

    # ── ⑫ 시작 — 받은편지함 항목 → 시작 메시지 + 회계 ──
    def start(self, items: list[dict]) -> Start:
        """단건이면 그 항목, 사람-직접 묶음(2건+)이면 전부 ``user:*`` 라 한 질의로 합친다 —
        항목마다 회계 id 가 올라 꼬리의 `## Open Requests` 와 `answers` 강제가 그 일을 한다."""
        reg, tm = self.registry, self.tm
        self._items = list(items)
        me = self.owner
        first = items[0]
        # 이 런의 정지 — ■ 는 이것만, ✕·세션 종료는 평생 정지로 (`agent_stop.LinkedStop`)
        self._run_stop = agent_stop.LinkedStop(tm.stop_event)
        tm.run_stop = self._run_stop
        if tm.stop_cause == agent_stop.STOPPED:
            self._run_stop.set()  # ■ 가 꺼낸 뒤·시작 전에 왔다 — 이 런이 그 런이다
        self._run_from = tm.ctx.next_ordinal if tm.ctx is not None else 0
        tm.state = "busy"
        # 화자 attribution — 회신/질문 라우팅 기준(D8); 묶음은 첫 발신자. 이 런의 이름은
        # 첫 항목의 id — 거는 질문의 asked_in (§3.2).
        tm.current_author = first.get("author", "main")
        tm.current_item = first["id"]
        if len(items) == 1:
            item = first
            author = item.get("author", "main")
            human = _is_human_addr(author)
            expects = item.get("expects_reply", True)
            if expects:
                # 요청 항목은 빚이다 (v9.21.0 — `complete` 는 국소, 배달은 `reply`/`message` 뿐).
                reg.note_owes(
                    me,
                    author,
                    item["text"],
                    answers=item.get("answers") or [],
                    hop=item.get("hop", 0),
                    id=item["id"],
                )
            reg._notify_roster()
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
                stop_event=self._run_stop,
            )
        for it in items:  # 전부 user:* — 빚은 아니지만 `reply` 거부 사유가 이걸 본다
            reg.note_owes(me, it.get("author", ""), it["text"], id=it["id"])
        reg._notify_roster()
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
            stop_event=self._run_stop,
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
            # run 의 과제 런: 부른 자리에 한 줄 카드 (docs/agent-engine/DESIGN.md §2.5)
            caller_task=(
                self.tm.caller_task
                if self.tm.ends_after_task and first["id"] == self.tm.task_id
                else None
            ),
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
        run_stop = self._run_stop
        renderer.end_agent_work(
            key=self.tm.key,
            item=self._items[0]["id"],
            success=success,
            duration_s=duration,
            error="" if success else output[:200],
            # 정지로 끝난 런(■·✕·세션 종료) — 실패가 아니라 정지로 그린다 (v10.142.0)
            stopped=bool(run_stop is not None and run_stop.is_set() and not success),
        )

    # ── ⑰ 결과 — 회신 계약·빚·run_ended (v10.89.0: 단건·사람 묶음 한 벌) ──
    def finish(self, result, secs: float, released: str) -> tuple[Any, float]:
        """런 끝의 후처리 — 종전 `_handle_request`·`_handle_human_batch` 두 벌.

        - `complete` 는 국소다: 산출물은 자동으로 아무에게도 가지 않는다. 요청 항목이면 이
          런은 요청자에게 회신을 **빚졌고**, 갚았으면 끝; 안 갚았으면 `run_ended` →
          `end_run` 이 런 요약을 라벨 붙여 폴백 배달한다(독촉 3회를 지나고도 안 보낸 경우).
          중단된 런은 빚을 버린다(`wait` 는 런을 끝내지 않는다 — 깬 뒤 같은 런이 갚는다, 5c-1).
        - out 레코드(대화창의 발신 줄)는 **실제 발신** 둘뿐: 하니스 폴백(라벨 붙음)과 사람
          발신(창이 곧 배달 — 묶음은 전부 사람이고 `reply_debts` 가 사람 주소를 빼므로
          단건과 같은 판정으로 떨어진다).
        - ``handled``/``state`` 는 **정리 뒤**(`_close_run`) — idle 은 '이 런의 일이 다
          끝났다' 여야 한다(v9.22.4, Linux CI 레이스).
        - 세션 종료로 끝난 런(``session_ended`` — 5e1)은 아무것도 하지 않는다: 회신·폴백·우편 정리·처리 수 없이
          그대로 돌려준다. 우편은 프로세스와 함께 사라지고(5c-0), 마지막 저장은 `shutdown_all` 이 한다.
        """
        if result is not None and getattr(result, "session_ended", False):
            return result, secs

        from agent_cli.subagent.agent_mail import _NO_REPLY_LABEL

        reg, tm, items = self.registry, self.tm, self._items
        me = self.owner
        iid = items[0]["id"]
        author = items[0].get("author", "main")
        success = bool(result.success)
        output = (
            result.output
            if result.output is not None
            else "(agent did not complete the request)"
        )
        # 이 런에서 **건** 사람 질문 중 아직 열린 것을 결과에 실어 알린다 (§3.6·§3.7)
        output = reg._with_human_notice(tm, iid, output)
        reply_path = reg._persist_reply(tm, iid, output)
        human = _is_human_addr(author)
        run_stop = self._run_stop
        stopped = run_stop.is_set() if run_stop is not None else tm.stop_event.is_set()
        if stopped and tm.stop_cause:
            # 사람(또는 모델)이 멈췄다 — 빚진 상대 모두에게 stopped 회신 (P3, `agent_stop`)
            self._answer_stopped_run()
        # 🤝 창의 out 줄 — 하니스 폴백은 도구 호출 없는 유일한 발신이라(v9.23.0) 웹이 ⚠ 줄로 그린다: 갚지 못한 빚마다
        # 한 줄, 그 빚의 id 와 상대로(`end_run` 이 보내는 폴백과 한 줄씩 맞는다). 사람이 창에서 연 구간은 창이 곧
        # 배달이다 — 그 사람에게 결과 한 줄(폴백이 아니다).
        line = {
            "key": tm.key,
            "direction": "out",
            "author": tm.key,
            "success": success,
            "ts": time.time(),
            "profile": tm.profile_name,
            "instance_name": tm.instance_name,
        }
        for d in () if stopped else reg.reply_debts(me):
            text = f"{_NO_REPLY_LABEL}\n{output}"
            reg._record(
                tm, {**line, "text": text, "item": d.id, "to": d.to, "fallback": True}
            )
        if human:
            reg._record(
                tm,
                {**line, "text": output, "item": iid, "to": author, "fallback": False},
            )
        # 런 끝 정리 — 판정은 `run_ended` 한 곳에 (wait·중단·보통)
        reg.run_ended(
            me,
            output,
            stopped=stopped,
            run=iid,
            success=success,
            duration_s=secs,
            reply_path=reply_path,
            # 실패한 런의 사유(턴 상한·provider 오류) — 산출물이 비어 회신 머리 한 줄에만 남는다
            **({"error": str(result.error)} if not success and result.error else {}),
        )
        reg._close_run(tm, len(items))
        if tm.stop_cause == agent_stop.STOPPED and not tm.stop_event.is_set():
            if stopped:
                # ■ — 에이전트는 남는다: 한 줄 알린다(대기 요청은 다음 차례)
                reg.report_stop(tm, kept=len(reg._queued(tm)))
            # 다음 런은 원인 없이 시작한다 — 멈추지 않고 끝난 런에 늦게 온 ■ 도 여기서 사라진다. 정지 상태 세 값을
            # 한자리에서 지운다("Stop everything" 의 조용함은 그 정지 회신만의 것이다)
            tm.stop_cause = tm.stop_by = ""
            tm.stop_replies_wake = True
        return result, secs

    def _answer_stopped_run(self) -> None:
        """멈춘 런의 빚에 회신한다 — 그 런의 마지막 호출과 생각을 실어(`agent_stop.run_activity`·
        `partial_notes`). 회신은 레지스트리가 보낸다(`answer_debts_as_stopped`)."""
        tm = self.tm
        records = tm.ctx.records_since(self._run_from) if tm.ctx is not None else []
        self.registry.answer_debts_as_stopped(
            tm,
            activity=agent_stop.run_activity(records),
            notes=agent_stop.partial_notes(records),
        )

    def crashed(self, exc: BaseException, secs: float) -> tuple[Any, float]:
        # worker 는 죽지 않는다 — 회신으로 보고하고 같은 후처리를 거친다
        return self.finish(
            ToolResult(
                False, output=f"agent internal error: {type(exc).__name__}: {exc}"
            ),
            0.0,
            "",
        )


class AgentActor:
    """에이전트 워커의 액터 (`actor.run_actor`) — 자기 받은편지함에서 런을 열 것(첫 깨우는 봉투, 사람의 말은 묶음)을
    시한 없이 기다려 그 종류(`ResidentLoop`)로 돌린다. 수명: 상주는 끝내기(✕·세션 종료 — 상자가 닫힌다)까지, run
    에이전트(``ends_after_task``)는 과제 하나 — 런이 끝나면 내려온다(`wait` 는 런을 끝내지 않는다).

    런의 예외는 종류가 회신으로 바꾼다(`ResidentLoop.crashed`) — 워커의 사망·정지 뒤처리는 레지스트리의 `_worker` 가
    든다."""

    poll_secs = None

    def __init__(self, registry, tm, kind: ResidentLoop) -> None:
        self.registry = registry
        self.tm = tm
        self.kind = kind

    def should_stop(self) -> bool:
        return False  # 시한이 없다 — 닫힘이 끝낸다

    def idle(self) -> None:
        """한가함은 꺼낼 때 창구가 세우고, 로스터의 ``idle`` 은 런 끝 정리가 세운다 — 따로 할 일이 없다."""

    def take(self, timeout):
        if self.tm.stop_event.is_set():
            return CLOSED  # 평생 정지 — 정지가 선 뒤 상자가 닫히기 전에 와도 새 런을 열지 않는다
        items = self.registry._next_run_items(self.tm)
        return items if items else CLOSED

    def open(self, items: list[dict]) -> Start:
        return self.kind.start(items)

    def run_start(self, start: Start) -> RunEnd:
        """종류가 런을 돌리고 그 끝 정리(`ResidentLoop.finish` — 빚 정리·`_close_run`)까지 한다: 런 카드를 닫기
        전이어야 화면의 순서가 같다. 여기서는 그 사실만 짓는다."""
        finished = self.kind.run(start)
        stop = self.kind._run_stop
        return RunEnd.of(finished, stopped=bool(stop is not None and stop.is_set()))

    def ended(self, start: Start, end: RunEnd) -> bool:
        """run 에이전트는 런이 끝나면 내려온다 — `complete`·정지·턴 상한·오류 어느 끝이든(사용자 결정 2026-11-15:
        회신은 장부가 보장한다 — 갚을 빚이 남으면 `complete` 가 되돌아오고, 그래도 남으면 런 끝 폴백이 런 요약으로
        회신한다). `wait` 는 런을 끝내지 않는다(5c-1). 런 중간의 `reply` 는 끝이 아니다. 세션 종료(``session_end``)는
        과제의 끝이 아니다 — 내려옴(`OnAgentEnd` 훅)도 없이 워커를 끝낸다(5e1 결정 E)."""
        if end.outcome == "session_end":
            return True
        if self.tm.ends_after_task:
            self._come_down(end.result)
            return True
        return False

    def _come_down(self, finished) -> None:
        """run 에이전트가 과제를 마쳤다 — 되살릴 대상이 아니고(채널은 회색으로 남는다),
        부른 쪽이 건 ``on_task_end``(OnAgentEnd 훅)를 마지막 런의 결과로 한 번 부른다.
        상태(dead)·저장·로스터는 워커의 정리(`finally`)가 한다."""
        tm = self.tm
        tm.revivable = False
        tm.handled = max(tm.handled, 1)
        hook, tm.on_task_end = tm.on_task_end, None
        if hook is not None:
            result = finished[0] if isinstance(finished, tuple) else finished
            try:
                hook(result)
            except Exception:
                pass  # 훅은 best-effort — 내려오는 길을 막지 않는다
