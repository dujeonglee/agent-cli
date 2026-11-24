"""teammate — 상주 세션 에이전트 (P1 코어, docs/teammate/DESIGN.md).

delegate 가 "파견"(스폰→완주→ctx 폐기)이라면 teammate 는 "상주 팀원"이다:
spawn 이 key 를 반환하고, 이후 request 가 같은 ctx 위에서 반복 처리된다
(D1 비동기 mailbox). 회신은 LLM 폴링이 아니라 **harness 가 배달**한다 —
main 루프가 턴 경계에서 :meth:`AgentRegistry.absorb_main` 으로 비워
관찰 레코드로 주입한다 (D2, ``AgentLoop._inject_mail``).

스레딩 모델
-----------
teammate 하나 = 데몬 worker 스레드 하나. worker 는 자기 inbox 를 블록해
메시지당 `ResidentLoop.run` (엔진 `run_loop`) 1회를
돌리고 회신을 우편의 main 상자에 넣는다. 상태 전이
(idle→busy→idle→…→dead)는 worker 루프 한 곳에만 있다.

- **인터럽트 분리**: worker 는 자기 ``stop_event`` 만 본다 — main 의
  Ctrl+C / /api/stop 은 teammate 를 죽이지 않는다(백그라운드 팀원).
  종료는 명시 ``kill`` 또는 세션 종료(:meth:`shutdown_all`)뿐.
- **인스펙터 (D9)**: worker 시작 시 ``begin_prompt_scope(key)`` 로 상시
  스코프를 열고(ctx 도 등록), **종료 시에만** ``end_prompt_scope`` —
  delegate 와 달리 요청 사이에도 칩이 살아 있고 동적 컨텍스트가 자란다.
  요청별 SSE 라우팅은 별도 표면 ``begin/end_agent_work`` (renderer).
- **레코드 계약**: 배달 레코드는 ``tool:"agent"`` + additive
  ``source:"agent_reply"``. ``tool:""`` 는 v4.51.0 형식-개입 레거시
  마커라 금지 (``records.is_format_intervention`` 오인 방지 — 테스트 고정).

모듈 나눔
---------
레지스트리는 명단·배달·저장·스레드의 틀이다. 규칙과 모양은 레지스트리를 import 하지 않는 잎에 있고
레지스트리가 부른다: 메일의 글(`agent_mail` — 봉투·관찰 레코드), 회신 갚기의 규칙(`question_port`),
`agents.json` 직렬화(`agents_state`), 사람의 정지(`agent_stop`), `agent` 도구의
모드 처리(`agent_modes`, run 은 `run_agent`).
"""

from __future__ import annotations

import re
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING

from agent_cli import item_ids
from agent_cli.constants import (
    CHECKIN_STEPS_S,
    harness_mail_tool,
    has_words,
    is_blank_words,
    is_private_owner,
)
from agent_cli.loop.kind import LoopKindError
from agent_cli.main_input import intake_item
from agent_cli.post.office import CLOSED, PostOffice
from agent_cli.post.request_book import RowError
from agent_cli.session.store import AGENTS_DIR
from agent_cli.session_slots import get_job_registry
from agent_cli.subagent import agent_stop, agents_state, conversation_log
from agent_cli.subagent.agent_mail import (
    _NO_REPLY_LABEL,
    MailView,
    envelope,
    envelope_of_item,
    envelope_of_main_mail,
    format_agent_label,
    is_human_addr,
    mail_queue_item,
    owe_line,
    run_error_line,
    view_of_inbox_item,
    view_of_main_mail,
)
from agent_cli.subagent.agents_state import WAITING
from agent_cli.subagent.delegation_checks import depth_or_recursion_refusal
from agent_cli.subagent.question_port import (
    REPLY_REQUESTER_GONE,
    QuestionPort,
    ReplyDebt,
    ReplyFacts,
)
from agent_cli.terminate import wait_left
from agent_cli.tools.result import ToolResult
from agent_cli.wake import (
    AgentFact,
    WakeFacts,
    agent_relations,
    awaited_of,
    harness_record,
    parked_view,
    release_owner,
    wait_checkin,
    wake_name,
)

if TYPE_CHECKING:
    from agent_cli.context.manager import ContextManager

_DEFAULT_MAX_AGENTS = 10
MAX_AGENTS_MIN = 1  # smallest positive cap; 0 (or less) means unlimited


def clamp_max_agents(value) -> int:
    """Normalise a requested max-agent cap. ``value <= 0`` → 0 (unlimited);
    otherwise floor to ``MAX_AGENTS_MIN``. Non-numeric → default."""
    try:
        n = int(value)
    except (TypeError, ValueError):
        return _DEFAULT_MAX_AGENTS
    if n <= 0:
        return 0  # unlimited sentinel
    return max(MAX_AGENTS_MIN, n)


# 에이전트↔에이전트 메시지 재주입 상한 (v5.11). 배달된 회신은 terminal
# (expects_reply=False)이라 자동으로 늘지 않으므로 이 상한은 에이전트들이
# 명시적으로 서로 request 를 주고받는 사이클의 안전망일 뿐 — 데드락은
# 비동기라 구조적으로 불가.
_MAX_PEER_HOPS = 6


# ── 비동기 ask (docs/agent-ask/DESIGN.md) ──────────────
#
# 질문은 **아무것도 블록하지 않는다**: ``ask`` 가 여기에 등록하고 즉시
# 반환한다. v10.77.0 (B): 질문은 **사람에게만** 간다 — ❓ 트레이가 표면이고
# 사람이 답하면 asker 에게 새 배달로 온다. 에이전트·main 에게 묻는 길은
# `message`(요청 → 상대가 `reply` 를 빚진다) 하나다. 그래서 `answer` 도구도,
# 에이전트가 빚지는 질문(q-)도 없다. ``user*`` 는 하나의 주체로 본다 — CLI 는
# ``user``, 웹은 ``user:{nickname}`` 이고 뷰어가 여럿이면 닉이 다르다.


@dataclass
class Question:
    """열린 질문 하나 — 우편 장부의 질문 줄(q-)의 ``meta``. 주소는 언제나 사람
    (v10.77.0, B) — ❓ 트레이가 보여 주고 사람이 답한다.

    ``asked_in`` = asker 가 이 질문을 건 런(을 연 배달의 id) — 그 런의 결과에
    "사람에게 물은 것이 열려 있다" 알림을 붙이고(`_with_human_notice`), 같은 런
    안의 같은 질문을 접는다. 다른 런에서 걸어 둔(영영 열릴 수 있는) 질문이
    섞이면 안 되므로 런으로 좁힌다.
    """

    id: str
    asker: str  # "<agent key>" — 답이 돌아갈 곳
    target: str  # "user" — 사람(트레이)
    text: str
    asked_at: float = field(default_factory=time.time)
    asked_in: str = ""  # asker 가 이 질문을 건 런(을 연 배달의 id)

    @property
    def to_human(self) -> bool:
        return is_human_addr(self.target)


def _debt_view(row) -> ReplyDebt:
    """장부의 빚 줄 → 옛 공개 모양(`ReplyDebt`). 갚은 줄(런 안에서 닫혀 남은 것)은 ``settled``."""
    return ReplyDebt(
        to=row.src,
        text=row.text,
        answers=list(row.answers),
        hop=row.hop,
        settled=row.state == "closed",
        id=row.rid,
        closed_by=row.how or "",
    )


def _new_question_id() -> str:
    return item_ids.next_id(item_ids.QUESTION)


# ── Live Teammates 시스템 프롬프트 리로드 플래그 ──
# 멤버십(spawn/kill/died/restore/auto_spawn) 변화 시 set — 루프의
# _execute_turn 이 directives/memory 플래그와 같은 자리에서 consume 해
# 다음 턴 시스템 프롬프트를 재조립한다 (상태 전이 busy/idle 은 안 건드림
# — KV 캐시 프리픽스를 의미 있는 사건에만 버스트).

# NOTE (v8.46.0): ``notify_agents_changed`` / ``consume_agents_reload`` /
# ``_membership_changed`` lived here so the SYSTEM prompt's "Live Agents"
# section could catch up after a spawn/kill/revive/death — a dirty flag the
# loop consumed to rebuild the prompt, plus a renderer call that surgically
# patched the section into the Prompt Inspector's system snapshot in between.
# All three are gone: the roster now rides in the tail session-state block,
# which the loop rebuilds from the registry on EVERY turn, so a membership
# change needs no signal at all. Keeping the snapshot patch would have been
# actively wrong — with no "Live Agents" section left in the system snapshot it
# would have INSERTED a phantom one the LLM never actually received.
# ``_notify_roster()`` (the 🤝 conversation-list update) is unrelated and stays.


def compose_role_prompt(profile_body: str, instructions: str) -> str:
    """instant-agent 합성 규칙 (U4): 파일(일반) → 인라인(구체) 순.

    인라인이 뒤(recency)라 세션-특정 지시가 우선 효과를 갖는다. 둘 중
    하나만 있으면 그것만, 둘 다 없으면 빈 문자열(익명 generalist).
    """
    parts = [p for p in (profile_body.strip(), instructions.strip()) if p]
    if len(parts) == 2:
        return f"{parts[0]}\n\n## Additional instructions\n{parts[1]}"
    return parts[0] if parts else ""


def first_sentence(text: str, *, cap: int = 200) -> str:
    """첫 문장만 (Live Agents 로스터·instant-agent 역할 요약용, v5.12).

    문장 종결부호(.!?)+공백/끝에서 자른다. 종결부호가 없으면 ``cap`` 자로
    폴백(단어 중간 절단 방지는 못 하지만 병리적 무-마침표 케이스 안전망).
    """
    text = (text or "").strip()
    m = re.search(r"[.!?](\s|$)", text)
    if m:
        return text[: m.start() + 1]
    return text if len(text) <= cap else text[: cap - 3] + "..."


@dataclass
class _Checkin:
    """소유자 하나에 걸린 `wait` 중간 깨우기 (v10.79.0) — 대상 id 집합, 배증 단계,
    이번 간격, `wait` 때의 기준(작업별 로그 크기·에이전트별 `last_ordinal`), 타이머."""

    ids: frozenset[str]
    step: int
    interval_s: int
    log_marks: dict[int, int] = field(default_factory=dict)
    step_marks: dict[str, int] = field(default_factory=dict)
    timer: threading.Timer | None = None


class AgentInstance:
    """상주 teammate 1명 — key·역할·영속 ctx·inbox·worker 스레드."""

    def __init__(
        self,
        key: str,
        *,
        profile_name: str,
        role_prompt: str,
        allowed_tools: list[str] | None,
        model: str,
        hooks_config: dict | None,
        context_mode: str,
        home_dir: Path,
        instance_name: str = "",
        description: str = "",
        instructions: str = "",
        ends_after_task: bool = False,
        caller_runtime: dict | None = None,
        agent_stack: list[str] | None = None,
        parent: str = "",
    ):
        self.key = key
        self.profile_name = profile_name
        #: run 에이전트(`agent` ``mode`` "run", docs/agent-engine/DESIGN.md §2) — 과제 하나의
        #: 런이 끝나면 내려온다.
        #: 그 밖은 spawn 과 같다:
        #: 명단·주소·상한·agents.json·채널. ``task_id`` 가 그 과제 요청의 id 다.
        self.ends_after_task = ends_after_task
        self.task_id = ""
        #: 과제가 끝나 내려올 때 한 번 — run 의 ``OnAgentEnd`` 훅(부른 쪽이 건다). 인자는
        #: 마지막 런의 결과(`ToolResult`).
        self.on_task_end: Callable[[ToolResult], None] | None = None
        #: 부른 자리(렌더러의 `current_task_id` — main 의 맨 위면 ``""``) — 웹이 거기에 한 줄
        #: 카드 "→ agt-… · working/done · open channel" 을 놓는다. run 의 과제 런에만.
        self.caller_task: str | None = None
        #: 띄운 쪽(호출자 루프)의 실행 배선 — 깊이·바인딩·턴 상한 (`AgentRuntime` 사전).
        #: 없으면(복원분) 레지스트리의 것(main). 레지스트리 하나의 런타임을 매 호출 덮어쓰면
        #: 에이전트가 띄운 에이전트의 깊이가 main 의 것과 섞인다.
        self.caller_runtime = caller_runtime
        #: 호출자의 에이전트 스택 — 순환 검사(A → B → A)가 본다. 엔진이 자기 프로파일을 얹는다.
        self.agent_stack = list(agent_stack or [])
        self.role_prompt = role_prompt
        # 다중 인스턴스 구분 라벨 (예: 같은 code-writer 역할의 "ui"/"api") — 주소는
        # 항상 key, name 은 표시·광고용.
        self.instance_name = instance_name
        # 역할 md 의 description — Live Teammates 광고의 전문영역 요약.
        self.description = description
        # instant-agent (U4): spawn 시 인라인으로 받은 추가 지시 원본 —
        # 합성 결과는 role_prompt 에 이미 들어있고, 이 필드는 manifest
        # 영속·인스펙터 가시성용.
        self.instructions = instructions
        self.allowed_tools = allowed_tools
        self.model = model
        self.hooks_config = hooks_config
        self.context_mode = context_mode
        self.home_dir = home_dir

        # 받은편지함·지금 런이 든 항목은 레지스트리의 우편(`AgentRegistry._post`)에 있다 — 주소 ``agent:<key>``
        # (v10.145.0). 저장하지 않는다 — 재시작은 명단과 컨텍스트만 올린다(5c-0).
        #: 지금 런이 턴 경계에서 흡수한 항목 수 — 런 정리(`_close_run`)가 ``handled`` 에 더하고 비운다(런 끝
        #: 정리가 우편의 든 것을 먼저 비우므로 거기서는 셀 수 없다)
        self.absorbed_in_run = 0
        #: 평생 정지 — ✕(kill)·세션 종료가 세운다. 런마다의 정지(■)는 ``run_stop`` — 이것에 묶여
        #: 있다(`agent_stop.LinkedStop`). 방 ■·Ctrl+C 한 번은 main 만 멈춘다 (P3, 사용자 결정).
        self.stop_event = threading.Event()
        #: 지금 런의 정지 — 런마다 새로(`ResidentLoop.start`). ``stop_run`` 이 세운다.
        self.run_stop: threading.Event | None = None
        #: 사람·모델이 멈췄다면 그 원인(`agent_stop.STOPPED`·`REMOVED`)과 누가(``"user"``·``"main"``·
        #: 키) — 빚진 상대에게 갈 회신을 정한다. 비면 사람의 정지가 아니다(세션 종료 — 빚은 버린다).
        self.stop_cause = ""
        self.stop_by = ""
        #: 이 정지의 회신이 받는 쪽을 깨우는가 — "Stop everything" 만 거짓(사용자 결정, v10.142.0): 그 회신은
        #: main 에겐 깨우지 않는 메일(``wakes_main``), 에이전트에겐 런을 열지 않는 항목(``opens_run``)으로 가
        #: 쌓였다가 다음에 일을 받는 런이 함께 읽는다(`_send_stop_reply`).
        self.stop_replies_wake = True
        #: 이 정지로 회신을 보낸 상대 ``[(받는 이, 요청 id)]`` — 화면 한 줄이 모아 말한다.
        self.stop_notified: list[tuple[str, str]] = []
        #: 띄운 쪽 주소(``"main"``·``"agent:<key>"``) — ✕ 가 그 에이전트가 띄운 run 을 함께 멈춘다.
        self.parent = parent
        self.worker: threading.Thread | None = None
        self.ctx: ContextManager | None = None

        self.state = "starting"  # starting | idle | busy | dead
        self.error = ""  # dead 사유 (ctx 생성 실패 등)
        # P3 resume: kill 은 영구(revivable=False — manifest 에 dead 로 기록),
        # 세션 종료로 죽은 teammate 는 revivable 유지 → resume 시 재생성.
        self.revivable = True
        self.revive = False  # restore 가 세움 — worker 가 ctx 를 resume 모드로
        #: 재시작이 되살렸다(`restore`) — 첫 ctx 에 재시작 기록 한 줄(`ResidentLoop.open`). ✕ 뒤 되살림은 아니다
        self.restarted = False
        # 지금 처리 중인 요청의 화자 — `reply` 거절 사유가 읽는다(`reply_facts`).
        self.current_author = "main"
        self.created_at = time.time()
        self.handled = 0  # 처리 완료한 request 수 (화면용 숫자)
        # 지금 도는 런의 이름 = 그 런을 연 배달의 id — 질문의 런 스코프
        # (Question.asked_in)가 이 값을 찍는다. 유휴는 "".
        self.current_item = ""
        # 이 런의 회신 빚은 여기 없다 — 레지스트리의 주소별 장부
        # (`AgentRegistry._post` — 약속 장부, v10.145.0) 가 main 과 같은 자리에서 든다.

    def snapshot(self, *, pending_requests: int, waiting: bool) -> dict:
        """status 표시용 스냅샷 (락 없는 근사값 — 표시 용도). ``pending_requests`` 는 레지스트리가 센 앞에 쌓인
        할 일(`AgentRegistry.backlog_of` — 받은편지함은 레지스트리의 우편에 있다), ``waiting`` 은 우편의 기다림 표시."""
        est_tokens = 0
        if self.ctx is not None:
            try:
                est_tokens = self.ctx.get_estimated_tokens()
            except Exception:
                est_tokens = 0
        return {
            "key": self.key,
            "profile": self.profile_name,
            "name": self.instance_name,
            # 끝나는 중(✕·run 의 ■·세션 종료 — 평생 정지가 섰다)은 더 받지 않는다: 워커가 지금 도구를 마치기
            # 전에도 끝난 것으로 보인다 — 멈춘 회신을 읽는 요청자가 "busy · 1 queued" 를 보지 않게 (v10.142.0)
            "state": "dead" if self.stop_event.is_set() else self.state,
            "handled": self.handled,
            "pending_requests": pending_requests,
            "waiting": waiting,
            "est_tokens": est_tokens,
            "error": self.error,
            "ends_after_task": self.ends_after_task,
            # 채널 머리(v10.142.0) — "run · from main · r-21"
            "parent": self.parent,
            "task_id": self.task_id,
            "item": self.current_item,
        }


class AgentRegistry:
    """main 루프 수명의 teammate 소유자 + 회신 mailbox.

    ``runner`` 는 종류(`ResidentLoop`)의 엔진 — 기본 `run_loop`; 테스트가 가짜
    러너를 주입하는 DI seam (스레딩·상태 전이를 LLM 없이 검증).
    """

    def __init__(
        self,
        session_dir: Path | None,
        *,
        runtime: dict | None = None,
        runner: Callable | None = None,
        max_agents: int | None = None,
        post: PostOffice | None = None,
    ):
        """``post`` — 우편·장부(`agent_cli.post`): 받은편지함·메일함·빚·보낸 쪽 기록이 여기 있다(v10.145.0).
        세션에서는 main 앞 사람의 말(`MainInput`)과 같은 창구다 — main 상자에 둘이 함께 선다. 레지스트리의 조건변수
        ``_cv`` 는 언제나 ``post.cv`` 다."""
        # 회신을 처리할 provider/모델 등 실행 배선 — spawn 시점이 아니라
        # 레지스트리 생성 시점(부트스트랩)에 고정할 수도 있으나, provider
        # 는 tool_bridge 인터셉트에서만 완전하므로 spawn 마다 갱신 수용.
        self.runtime = runtime or {}
        self._runner = runner
        self.session_dir = Path(session_dir) if session_dir else None
        #: ``agents.json`` 이 이 레지스트리의 것인가 — 읽었거나(`restore`) 처음 만들었을
        #: 때 참. 그 전에는 있는 파일을 덮어쓰지 않는다(`_save_state`).
        self._state_owned = False
        #: 저장 하나가 뜨기부터 쓰기까지를 쥔다 — 겹친 저장이 옛 사본을 나중에 쓰지 않게.
        #: ``_cv`` 를 쥔 채로는 저장을 부르지 않는다(순서: ``_save_lock`` → ``_cv``). ``_cv`` 안에 드는 것은 대화 파일
        #: append 와 막히지 않는 렌더러의 창 줄뿐이다(`_lines_in_section`, v10.146.5).
        self._save_lock = threading.Lock()
        # 동시 생존 상한 (세션 한정, web UI 조절). 0 = 무제한. None = 기본값 —
        # env·CLI 는 부트가 `knobs.apply_all` 로 적용한다 (v10.43.0).
        self.max_agents = clamp_max_agents(
            _DEFAULT_MAX_AGENTS if max_agents is None else max_agents
        )

        self._agents: dict[str, AgentInstance] = {}
        self._post = post if post is not None else PostOffice()
        self._cv = self._post.cv
        self._roster_step = threading.Lock()  # 명단을 뜨고 내는 한 걸음
        # private 주소(run:·harness:)의 메일함(v10.84.0)도 우편의 상자다 — 그 루프가 턴 경계와 `wait` 에서 읽는다.
        # UI·영속 밖(프로세스 수명 — 저장 파일은 에이전트와 main 의 상자만 쓴다).
        # 회신 도착 알림(CLI 대기 줄) — 부트스트랩(`wire_agent_mail`) 주입. main 이 한가한가는 창구가 안다(`idle`).
        self.on_reply: Callable[..., None] | None = None
        #: 미배달 메일이 늘거나 비워졌다 (v10.51.0) — web 이 입력창 위 대기열을
        #: 다시 그린다(`WebServer.broadcast_queue`). 락 밖에서 부른다.
        self.on_pending_change: Callable[[], None] | None = None
        #: 모니터 레지스트리 — `wire_monitor_delivery` 가 주입한다. 워커의
        #: `finally` 가 죽은 소유자의 감시를 폐기하려면 참조가 필요하다.
        #: 배선 안 된 레지스트리(테스트 픽스처 다수)는 `None` 이라 모든
        #: 소비 지점이 가드한다.
        self.monitors = None
        self.main_stop_wakes = True  # 거짓 = Stop everything (`agent_stop`)
        #: 백그라운드 작업 레지스트리 (v10.47.0) — `wire_job_delivery` 가 꽂는다;
        #: 워커 종료가 `_drop_monitors` 와 같은 자리에서 그 에이전트의 작업을 죽인다
        self.jobs = None
        # 귀속 승계 (v8.5.0): 현재 런이 서비스 중인 USER 들 — worker 가 런
        # 시작에, main 루프가 조향 주입마다 갱신. main 발신 request 가 이
        # 스냅샷을 아이템에 실어 보내고, 그 회신이 그대로 되가져와 회신을
        # 소비한 런의 ``answers`` 에 합류한다(🤝 웨이크 런 포함). 시간이
        # 아니라 요청↔회신 쌍에 묶이므로 인터리빙에 안전.
        self._current_run_authors: list[str] = []
        # 열린 질문(비동기 ask/answer, DESIGN.md §3)은 우편 장부의 q 줄이다 — ``meta`` 가 `Question`(v10.145.0).
        # 답은 줄을 닫는 것과 배달이 한 락 구간이라(`PostOffice.answer`) 동시 답변자 둘 중 하나만 답이 된다.
        # 빚·보낸 쪽 기록·받은편지함·main 메일함은 우편 하나다(`self._post` — 약속 장부와 상자, v10.145.0).
        # `wait` 중간 깨우기 (v10.79.0, docs/wait/DESIGN.md §6) — 소유자별 걸린
        # 타이머와, 직전에 **발화한** 깨우기의 (대상 id 집합, 단계). 둘 다 ``_cv``
        # 아래. 배증이 치르는 유일한 상태다. 영속하지 않는다 — 프로세스가 죽으면
        # 기다리던 것도 함께 죽는다.
        self._checkin_armed: dict[str, _Checkin] = {}
        self._checkin_last: dict[str, tuple[frozenset[str], int]] = {}
        #: 타이머 팩토리 — 테스트가 가짜(즉시 발화하지 않는)로 바꾼다.
        #: `threading.Timer` 는 가짜 시계로 돌릴 수 없어 `fire_checkin` 을 직접 부른다.
        self.checkin_timer_factory: Callable[..., threading.Timer] = threading.Timer

    # ── 조회 ────────────────────────────────────

    def set_current_run_authors(self, authors: list[str]) -> None:
        """현재 런의 요청자 목록 갱신 (귀속 승계 — ``_current_run_authors``
        주석 참조). worker(런 시작)와 main 루프(조향 주입)가 호출한다."""
        self._current_run_authors = list(authors)

    def get(self, key: str) -> AgentInstance | None:
        return self._agents.get(key)

    def instances(self) -> list[AgentInstance]:
        """명단의 에이전트 전부(죽은 것 포함) — 스냅샷 목록이라 도는 동안 명단이 바뀌어도 안전하다."""
        return list(self._agents.values())

    def resolve_key(self, addr: str) -> str | None:
        """주소 → 에이전트 키 (v10.1.0). 키 그대로, ``agent:<key>`` 접두, 또는
        인스턴스 이름(대소문자 무시; 산 것 우선, 유일할 때만). 없으면 None.

        들어오는 메시지는 ``[agent:agt-…]`` 로 표기되고 로스터는 이름을 보여
        주는데 ``message.to`` 는 맨 키만 받았다 — 모델이 보이는 대로 베껴 쓰면
        "unknown agent" 였다(실측 cnr60g: `agent:agt-330e6b1a`, `player3`, `2번`).
        """
        a = (addr or "").strip().removeprefix("agent:")
        if not a:
            return None
        if a in self._agents:
            return a
        low = a.lower()
        with self._cv:
            hits = [
                t.key
                for t in list(self._agents.values())
                if t.instance_name and t.instance_name.lower() == low
            ]
            alive = [k for k in hits if self._agents[k].state != "dead"]
        hits = alive or hits
        return hits[0] if len(hits) == 1 else None

    def roster_snapshot(self) -> list[dict]:
        # ``AgentInstance.snapshot`` 은 인스턴스 메서드라 레지스트리의
        # 우편에 닿지 못한다 — 열린 질문은 여기서 합류.

        # Snapshot the values first: this runs on worker/web threads while the
        # main thread may spawn/resume/restore into ``_agents``. Iterating the
        # live view directly raises "dictionary changed size during iteration"
        # (AUDIT S-1); ``list(...)`` materialises atomically under the GIL.
        by_asker: dict[str, list[dict]] = {}
        for q in self.open_human_questions():
            by_asker.setdefault(q.asker, []).append(
                {"id": q.id, "text": q.text, "to": q.target, "ts": q.asked_at}
            )
        out = []
        for tm in list(self._agents.values()):
            snap = self._snapshot_of(tm)
            snap["open_questions"] = by_asker.get(tm.key, [])
            out.append(snap)
        return out

    def _notify_roster(self) -> None:
        """P4: 상태 변화를 대화 창 목록에 반영 — web sticky, CLI no-op. 명단을 뜨고 내는 것이 한 걸음이다
        (``_roster_step``): 여러 스레드가 부르므로, 묶지 않으면 먼저 뜬 명단이 나중에 나가 새 명단을 덮는다."""
        from agent_cli.render import get_renderer

        try:
            with self._roster_step:
                get_renderer().agent_roster(self.roster_snapshot())
        except Exception:
            pass  # 표시용 — 실행 경로를 막지 않는다

    def alive_count(self) -> int:
        return sum(1 for t in list(self._agents.values()) if t.state != "dead")

    #: "작업 중" 판정 — 어휘의 단일 소유는 `agents_state.state_is_active` (메일의 글도 읽는다).
    state_is_active = staticmethod(agents_state.state_is_active)

    def has_agent_work(self) -> bool:
        """에이전트 몫의 살아 있는 일 — 일하는 중(`state_is_active`, ``starting`` 포함)이거나 죽지 않았고 깨우는 봉투가
        쌓였다(세션 판정 `runtime.session_has_live_work` 의 한 항). 죽은 이의 남은 봉투는 일이 아니다 — 런 도중 끝난
        (■·✕) 워커는 꺼내지 않고 나간다(세면 `run` 이 끝나지 않는다, v10.142.0)."""
        return any(
            self.state_is_active(t.state)
            or (t.state != "dead" and self.backlog_of(t.key) > 0)
            for t in list(self._agents.values())
        )

    def set_max_agents(self, value) -> int:
        """Set the live-agent cap (session-only, web UI). ``value <= 0`` →
        unlimited (0). Returns the stored value. Does not retroactively kill
        agents over a lowered cap — it only gates new spawns/resumes."""
        self.max_agents = clamp_max_agents(value)
        return self.max_agents

    def _at_agent_limit(self) -> bool:
        """True when a new spawn/resume would exceed the cap. 0 = unlimited."""
        return bool(self.max_agents) and self.alive_count() >= self.max_agents

    def agent_limit_refusal(self) -> str:
        """상한에 걸린 새 에이전트(spawn·run)의 거부문 — 한 문장을 둘이 쓴다."""
        return (
            f"agent limit reached ({self.max_agents} alive). "
            f'Kill one first (`mode` "kill") or raise the limit in the web UI.'
        )

    def wake_facts(self) -> WakeFacts:
        """ "누가 깨우는가" 의 재료 — 명단과 우편 장부의 사본 하나(`wake.WakeFacts`), ``_cv`` 를 한 번 쥐고 뜬다.
        판정은 `wake` 의 순수 함수가 한다(`agent_relations`·`parked_debtors`). 읽기만 한다."""

        def heads(items: list[dict]) -> tuple[tuple[str, str], ...]:
            return tuple(
                (str(it.get("author") or ""), str(it.get("id") or "")) for it in items
            )

        with self._cv:
            agents = tuple(
                AgentFact(
                    addr=f"agent:{tm.key}",
                    active=self.state_is_active(tm.state),
                    dead=tm.state == "dead",
                    taken=heads(self._taken(tm)),
                )
                for tm in list(self._agents.values())
            )
            rows = [r for r in self._post.rows() if r.kind == "r"]
            return WakeFacts(
                agents=agents,
                owing=tuple((r.dst, r.src) for r in rows if r.state == "picked"),
                queued=tuple((r.dst, r.src) for r in rows if r.state == "queued"),
                awaited=frozenset((r.src, r.rid) for r in rows if r.state != "closed"),
                waiting=self._post.waiting_addresses(),
                # 받는 쪽이 죽었으면 빼다 — 회신을 보낼 이가 없다(그 죽음은 따로 메일로 온다)
                replies=tuple(
                    (r.src, {"id": r.rid, "from": r.dst, "text": r.text})
                    for r in rows
                    if r.state != "closed" and self._agent_alive(r.dst)
                ),
            )

    def checkin_agent_lines(
        self, addr: str, step_marks: dict[str, int]
    ) -> tuple[list[str], bool, bool]:
        """`wait` 중간 깨우기 알림의 에이전트 줄 (v10.79.0, docs/wait/DESIGN.md §6.3)
        — ``(줄 목록, 에이전트 줄이 있는가, 사람 질문에 막힌 줄이 있는가)``. 받는 쪽이 main 이면 그 줄은
        ``main · owes you a reply``(집었다)나 ``main · has your request queued``(아직 줄 서 있다)뿐이다 — 에이전트가
        아니라 걸음 수·일감이 없다.

        작업·monitor 와 달리 에이전트는 로그 파일이 없다. 레지스트리가 **이미 아는
        것**만으로 그린다 — 상태·지금 처리 중인 요청자·받은편지함 미러·회신 빚과
        스스로 잠듦·그 에이전트가 기다리는 것(`wake.parked_view`)·열린 사람
        질문·컨텍스트 기록의 서수와 시각. 새 필드는 없다. "step" 은 컨텍스트 기록
        하나(모델 턴 또는 관찰) — 모델에게 하니스 용어(턴·관찰)를 쓰지 않는다.
        ``step_marks`` 는 `wait` 때 적어 둔 에이전트별 `last_ordinal`.

        걸음 수·시각은 락 없는 근사값이다(`snapshot()` 의 `est_tokens` 와 같은 수준).
        "일하고 있다" 와 "쓸모 있는 일을 하고 있다" 는 구별하지 못한다."""
        from agent_cli.constants import format_duration

        now = time.time()

        def _label(key: str) -> str:
            tm = self._agents.get(key)
            if tm is None:
                return f"agent:{key}"
            return f"agent:{format_agent_label(key, tm.profile_name, tm.instance_name)}"

        def _steps(tm, me: str) -> str:
            ctx = tm.ctx
            ordinal = (ctx.last_ordinal or 0) if ctx is not None else 0
            n = max(0, ordinal - step_marks.get(me, ordinal))
            at = float(getattr(ctx, "last_added_at", 0.0) or 0.0) if ctx else 0.0
            ago = f", last one {format_duration(now - at)} ago" if at else ""
            steps = f"{n} step{'s' if n != 1 else ''}" if n else "no step"
            return f"{steps} since your wait{ago}"

        def _last_step(tm) -> str:
            ctx = tm.ctx
            at = float(getattr(ctx, "last_added_at", 0.0) or 0.0) if ctx else 0.0
            return f", last step {format_duration(now - at)} ago" if at else ""

        lines: list[str] = []
        main_lines: list[str] = []  # 빚진 main — 에이전트만 일하거나 줄 세운다
        question = False
        facts = self.wake_facts()  # 관계와 잠든 채무자를 같은 사본에서
        for kind, me in agent_relations(facts, addr):
            if not me.startswith("agent:"):
                what = (
                    "has your request queued"
                    if kind == "queued"
                    else "owes you a reply"
                )
                main_lines.append(f"{me} · {what}")
                continue
            key = me.split(":", 1)[1]
            tm = self._agents.get(key)
            if tm is None:
                continue
            if kind == "working":
                lines.append(
                    f"{_label(key)} · working on your request · {_steps(tm, me)}"
                )
            elif kind == "queued":
                queued = self._queued(tm)
                ahead = next(
                    (i for i, it in enumerate(queued) if it.get("author") == addr),
                    len(queued),
                )
                where = (
                    f"has your request queued behind {ahead} other"
                    f"{'s' if ahead != 1 else ''}"
                    if ahead
                    else "has your request queued, next in line"
                )
                # 내 요청을 줄 세운 채 무엇을 하나 — 기다리는 중이면(같은 사본의 기다림 표시) 그 낱말로 (v10.166.0)
                doing = WAITING if me in facts.waiting else "busy on another request"
                busy = (
                    f"{doing}{_last_step(tm)}"
                    if self.state_is_active(tm.state)
                    else "picking it up"
                )
                lines.append(f"{_label(key)} · {where} · {busy}")
            else:  # 빚졌지만 내 요청을 들지도 줄 세우지도 않았다 — 일하는 중이면 다른 요청을 하는 중이다
                busy = self.state_is_active(tm.state)
                tail = f"busy on another request{_last_step(tm)}" if busy else "idle"
                lines.append(f"{_label(key)} · owes you a reply · {tail}")
        for debtor, on in parked_view(
            addr, frozenset({addr}), agents=self, facts=facts
        ):
            key = debtor.split(":", 1)[1]
            if not on:
                continue  # 깨울 것이 없는 채무자는 깨울 근거가 아니다 (v10.77.0)
            names = ", ".join(wake_name(wid) for wid, _ in on)
            line = f"{_label(key)} · owes you a reply · itself waiting on {names}"
            open_qs = [q for q in self.open_human_questions() if q.asker == key]
            if open_qs:
                q = min(open_qs, key=lambda q: q.asked_at)
                line += (
                    f' · blocked on question "{q.id}" to the user, unanswered for '
                    f"{format_duration(now - q.asked_at)}"
                )
                question = True
            lines.append(line)
        return [*main_lines, *lines], bool(lines), question

    def awaited_replies(self, addr: str) -> list[dict]:
        """``addr`` 가 보낸 요청 중 회신이 아직 오지 않은 것 — 장부의 "기다리는 회신"
        (v10.141.0, docs/agent-engine/DESIGN.md §3). ``[{"id", "from", "text"}]``, 번호 순.

        우편 장부의 열린 r 줄을 읽는다 — 요청이 상자에 들 때 열리고(`_enqueue`·`message_to_main`), 받는 쪽의
        빚이 끝날 때(갚음·폴백·버림 — 배달과 한 락 구간) 닫힌다(v10.145.0).
        갚음·폴백은 회신을 **배달한 뒤** 지운다: 먼저 지우면 그 틈에 요청자의 `wait` 가 기다릴
        회신도 도착한 메일도 없는 순간을 보고 `wait` 를 거절한다.
        받는 쪽에서 세면(빚 장부 + 받은편지함) 항목이 받은편지함을 떠나 빚으로 적히기까지의
        틈에 회신 기다림이 사라진다. 받는 쪽이 죽었으면 빼다(`wake_facts` 의 ``replies``). 읽기만 한다."""
        if not addr or is_human_addr(addr):
            return []
        return awaited_of(self.wake_facts(), addr)

    # ── 배달과 갚기 — 한 락 구간 (v10.145.0) ────────────────────────────────────────────
    # 범용 배달(`_enqueue`·`_push_reply`·`_push_private`)은 봉투를 `_post_deliver` 로 우편에 넣는다. 갚는 자리
    # (`pay_debt`·`end_run`·`answer_debts_as_stopped`·`answer_question`)만 ``settles`` 로 그 배달이 닫는 줄의 id 를
    # 말한다 — 같은 모양의 봉투라도 위임·`message`·removed 회신·재배달은 갚지 않으므로 호출자가 아는 사실이다(갚는
    # 것은 `reply` 와 그 대신인 폴백·정지 회신, v10.145.1). 배달 지시라 봉투·저장 파일·모델이 읽는 글에 실리지 않는다.

    def _post_deliver(self, env, *, settles: str = "") -> None:
        """봉투를 우편에 넣는다 — ``_cv`` 안에서 부른다. 닫는 연산은 배달과 한 락 구간이다: 없음 → `deliver`, 줄 id →
        q 줄은 `answer`·r 줄은 `pay`. 없는 줄·r/q 가 아닌 줄은 `RowError`(닫힌 줄은 그 연산이 거절 — "already replied")."""
        if not settles:
            self._post.deliver(env)
            return
        row = self._post.row(settles)
        if row is None or row.kind not in ("q", "r"):
            raise RowError(f"{settles}: no request or question to settle")
        if row.kind == "q":
            self._post.answer(settles, env)
        else:
            self._post.pay(env.src, settles, env, run_id=self._run_of(env.src))

    def _run_of(self, addr: str) -> str:
        """닫는 줄에 적을 런 id — 에이전트는 지금 런을 연 배달, main 은 펌프의 런을 레지스트리가 모른다(주소)."""
        if addr.startswith("agent:"):
            tm = self._agents.get(addr.split(":", 1)[1])
            if tm is not None and tm.current_item:
                return tm.current_item
        return addr

    def _agent_alive(self, addr: str) -> bool:
        """이 주소가 아직 말을 주고받을 수 있는가 — main(과 에이전트 아닌 주소)은 언제나, 에이전트는 명단에 있고
        죽지 않았고 멈추는 중이 아니면. 빚진 쪽이면 "회신을 보낼 수 있다", 요청자면 "회신을 받을 수 있다" 는 같은
        조건이다(`request` 의 dead 거절과도 같다). 읽기만 한다."""
        if not addr.startswith("agent:"):
            return True
        tm = self._agents.get(addr.split(":", 1)[1])
        return tm is not None and tm.state != "dead" and not tm.stop_event.is_set()

    def _queued(self, tm: AgentInstance) -> list[dict]:
        """``tm`` 의 받은편지함에 들어 있고 아직 런이 집지 않은 항목(옛 dict 모양 사본, 도착 순서)."""
        return [dict(e.payload) for e in self._post.queued(f"agent:{tm.key}")]

    def _taken(self, tm: AgentInstance) -> list[dict]:
        """``tm`` 의 지금 런이 든 항목(옛 dict 모양 사본)."""
        return [dict(e.payload) for e in self._post.taken(f"agent:{tm.key}")]

    def _snapshot_of(self, tm) -> dict:
        """에이전트 하나의 표시용 스냅샷 — 앞에 쌓인 할 일과 기다림 표시를 우편에서 읽는다."""
        waiting = self._post.is_waiting(f"agent:{tm.key}")
        return tm.snapshot(pending_requests=self.backlog_of(tm.key), waiting=waiting)

    def backlog_of(self, key: str) -> int:
        """에이전트 ``key`` 앞에 쌓인 할 일 — 깨우는 봉투만 센다(런을 열지 않는 정지 회신은 일이 아니다)."""
        return self._post.arrived(f"agent:{key}", waking_only=True, words=True)

    def queued_for(self, addr: str) -> tuple[MailView, ...]:
        """``addr`` 앞에 줄 서 아직 꺼내 가지 않은 배달 — main 메일함·에이전트 받은편지함(미러)·private 메일함,
        도착 순서. 읽기만 한다 — 락 안에서 사본을 뜬다."""
        if addr == "main" or is_private_owner(addr):
            return tuple(
                view_of_main_mail(dict(e.payload)) for e in self._post.mail(addr)
            )
        tm = self._agents.get(addr.removeprefix("agent:"))
        return () if tm is None else tuple(map(view_of_inbox_item, self._queued(tm)))

    def taken_by(self, addr: str) -> tuple[MailView, ...]:
        """``addr`` 의 런이 꺼내 들고 있는 배달 — 에이전트의 지금 런이 시작·흡수한 항목(런이 끝나면 빈다).
        main·private 은 꺼낸 것을 들고 있지 않다. 읽기만 한다."""
        tm = (
            self._agents.get(addr.split(":", 1)[1])
            if addr.startswith("agent:")
            else None
        )
        if tm is None:
            return ()
        return tuple(map(view_of_inbox_item, self._taken(tm)))

    def arrivals_for(self, addr: str, *, waking_only: bool, words: bool) -> tuple:
        """``addr`` 앞에 **이미 도착해 다음 턴 경계에 배달될** 봉투(도착 순서, 창구의 `PostOffice.arrivals`). main·에이전트·
        private 주소만 — 명단에 없는 에이전트와 그 밖 주소는 비었다. 읽기만 한다."""
        if addr.startswith("agent:") and addr.split(":", 1)[1] not in self._agents:
            return ()
        if addr == "main" or addr.startswith("agent:") or is_private_owner(addr):
            return self._post.arrivals(addr, waking_only=waking_only, words=words)
        return ()

    def arrived_for(self, addr: str, *, waking_only: bool, words: bool) -> int:
        return len(self.arrivals_for(addr, waking_only=waking_only, words=words))

    def open_human_question_keys(self) -> list[str]:
        """사람 답을 기다리는 열린 질문의 asker — 종료 시 경고 표시용."""
        return sorted({q.asker for q in self.open_human_questions()})

    # ── 질문 목록 (비동기 ask/answer, DESIGN.md §3) ──────────────

    def question_port(self, key: str | None = None) -> QuestionPort:
        """루프에 넘길 seam. ``key=None`` 이면 main 용."""
        return QuestionPort(self, key)

    def register_question(self, asker: str, text: str) -> tuple[str, str]:
        """사람에게 질문 등록 — ``(id, err)``. 아무것도 블록하지 않는다.

        표면은 ❓ 트레이(로스터의 ``open_questions``)와 asker 의 대화 창이다 —
        배달할 inbox 가 없다. 사람이 답하면 :meth:`answer_question` 이 asker 에게
        새 배달로 보낸다."""
        text = (text or "").strip()
        if not text:
            return "", "empty question"
        tm = self._agents.get(asker)
        if tm is None:
            return "", f'unknown asker "{asker}"'
        asked_in = tm.current_item
        with self._cv:
            # 중복 접기 — **같은 런 안에서만**. ``_op_ask`` 는 루프 탐지기
            # 앞에서 반환하므로(dispatch.py) 한 런이 같은 질문을 반복해도
            # 구조적으로 안 잡힌다. 런 경계를 넘으면 접지 않는다.
            for q in self.open_human_questions():
                if q.asker == asker and q.text == text and q.asked_in == asked_in:
                    return q.id, ""
            q = Question(
                id=_new_question_id(),
                asker=asker,
                target="user",
                text=text,
                asked_in=asked_in,
            )
            self._post.ask(q.id, f"agent:{asker}", q.target, text, meta=q)
            # 질문 줄은 질문을 여는 구간에서 적는다 — 답의 in 이 앞서지 않게 (v10.146.4)
            lines = self._question_line(q)
            self._lines_in_section(lines)
        self._lines_after_section(lines)
        # 트레이는 로스터의 ``open_questions`` 를 읽는다 — 알리지 않으면
        # 질문이 다음 로스터 브로드캐스트까지 화면에 안 뜬다.
        self._notify_roster()
        return q.id, ""

    def _question_line(self, q: Question) -> list[tuple[AgentInstance, dict]]:
        """묻는 쪽 창의 질문 줄 — 사람이 먼저 보는 자리. 만들기만 한다. 묻는 쪽이 없으면 빈 목록."""
        tm = self._agents.get(q.asker)
        if tm is None:
            return []
        payload = {
            "key": tm.key,
            "direction": "question",
            "author": tm.key,
            "text": q.text,
            "to": q.target,
            "ts": q.asked_at,
            "profile": tm.profile_name,
            "instance_name": tm.instance_name,
        }
        return [(tm, payload)]

    def questions_asked_in(self, asker: str, run_item: str) -> list[Question]:
        """``asker`` 가 ``run_item`` 런에서 건 열린 질문 (런 결과의 알림)."""
        return [
            q
            for q in self.open_human_questions()
            if q.asker == asker and q.asked_in == run_item
        ]

    def open_human_questions(self) -> list[Question]:
        """열린 질문 — ❓ 트레이와 idle-reap 가드가 읽는다(주소는 전부 사람). 질문 줄의 ``meta`` 가 `Question` 이다."""
        return [r.meta for r in self._post.open_questions()]

    def answer_question(self, qid: str, text: str, *, by: str) -> str:
        """사람이 트레이에서 ``qid`` 에 답한다 — 에러 또는 빈 문자열.

        ``user*`` 는 하나의 주체로 본다 — CLI 는 ``user``, 웹은 ``user:{nick}``
        이고 뷰어마다 닉이 달라, 문자열 동치로 검사하면 두 번째 뷰어의 트레이
        답이 거부된다.
        """
        text = (text or "").strip()
        if not text:
            return "empty answer"
        unknown = f'unknown or already-answered question "{qid}"'
        row = self._post.row(qid)
        q = row.meta if row is not None and row.kind == "q" else None
        if q is None:
            return unknown
        if not is_human_addr(by):
            return f'question "{qid}" is addressed to the person, not "{by}"'
        try:  # 답의 배달이 곧 질문 줄 닫기(``settles`` — 한 락 구간)
            refused = self._deliver_answer(q, text)
        except RowError:
            return unknown  # 다른 답변자가 먼저 답했다 — 줄이 이미 닫혔다
        if refused:  # 묻는 쪽이 멈췄거나 사라져 받지 못한다 — 그 질문들을 닫고 명단에 알린다 (v10.149.1)
            self._purge_questions_for(q.asker)
            return f'the agent that asked "{qid}" has stopped — the question is closed'
        return ""

    # ── 회신 빚 장부 (v9.22.1 — main·상주 공통) ──────
    # 요청(`expects_reply=True` 항목 / main 에 배달된 `message`)은 빚이다.
    # 갚는 길: `reply` 하나 — main·에이전트 같은 판정(`question_port.judge_reply`, v10.146.0). `message` 는
    # 갚지 않는다(v10.145.1).
    # 런의 끝은 `run_ended` 하나다 — 보통의 끝은 남은 빚을 폴백(라벨 붙은 런 요약)으로
    # 배달하고, 정지는 버린다.

    def note_owes(
        self,
        debtor: str,
        to: str,
        text: str,
        *,
        answers: list | None = None,
        hop: int = 0,
        id: str = "",
    ) -> ReplyDebt:
        """``debtor`` 가 ``to`` 에게 회신을 빚진다 — 요청마다 새 빚(v9.25.0).
        같은 상대의 요청이 여럿이면 각각 갚아야 한다. ``id`` 를 안 주면 새
        `r-` id 를 받는다(main 에 배달된 `message` — 갚는 도구가 상대만 고른다)."""
        with self._cv:
            if not id:
                id = item_ids.next_id(item_ids.REPLY)
            rid = str(id)
            row = self._post.row(rid)
            if (
                row is None
            ):  # 봉투 없이 적는 빚 — 봉투가 이미 줄을 열었으면(런 시작·흡수가 집었다) 그 줄이다
                if is_human_addr(to):
                    row = self._post.open_human(
                        rid, to, debtor, text, answers=list(answers or [])
                    )
                else:
                    row = self._post.owe(
                        rid, debtor, to, text, hop=hop, answers=list(answers or [])
                    )
            return _debt_view(row)

    def pay_debt(self, debtor: str, d: ReplyDebt, text: str) -> str:
        """``debtor`` 가 빚 ``d`` 를 ``text`` 로 갚는다 — `reply(id)` 의 배달(`QuestionPort.reply`). 에러 또는 "".

        갚음으로 적고 → 요청자에게 배달하고(main 이면 메일함, 에이전트면 받은편지함 — 되받을 것 없는 회신) →
        요청자의 기다리는 회신을 닫는다. 닫기는 배달 **뒤**다: 요청자가 그 틈에 기다림을 잃지 않게.
        회신의 배달이 곧 갚음이다(``settles`` — 한 락 구간의 `PostOffice.pay`). 요청자가 사라졌거나 죽었으면
        배달하지 못한 채 빚을 닫고(`pay_unsent` — 기다리는 이가 없으니 다시 갚게 하지 않는다) 그 사실을 돌려준다
        (`REPLY_REQUESTER_GONE`) — main 이 갚든 에이전트가 갚든 같은 문장이다(v10.145.2)."""
        run_id = self._run_of(debtor)
        try:
            return self._pay_debt(debtor, d, text)
        finally:
            row = self._post.row(d.id)
            if row is not None and row.state == "picked":
                # 회신이 가지 못했다 — 갚음이다(닫힌 채 그 런 끝까지: "이미 회신했다")
                self._post.pay_unsent(debtor, d.id, run_id=run_id)

    def _pay_debt(self, debtor: str, d: ReplyDebt, text: str) -> str:
        if debtor == "main":
            return self._deliver_peer_reply(
                d.to.split(":", 1)[1], "main", text, 0, re=d.id, settles=d.id
            )
        key = debtor.split(":", 1)[1]
        if d.to == "main":
            tm = self._agents.get(key)
            self.message_to_main(
                key,
                text,
                profile=tm.profile_name if tm else "",
                name=tm.instance_name if tm else "",
                answers=d.answers,
                re=d.id,
                settles=d.id,
            )
            return ""
        requester = d.to.split(":", 1)[1]
        out = self._outbound_line(key, text, to=requester)
        return self._deliver_peer_reply(
            requester, key, text, 0, re=d.id, settles=d.id, lines=out
        )

    def reply_facts(self, debtor: str, id: str) -> ReplyFacts:
        """`reply` 판정의 재료(`question_port.ReplyFacts`) — ``_cv`` 를 한 번 쥐고 뜬다. 런을 연 주소는 에이전트만
        안다(지금 런의 첫 항목 author); main 의 런은 펌프가 연다 — None. 읽기만 한다."""
        with self._cv:
            known, opener = True, None
            if debtor.startswith("agent:"):
                tm = self._agents.get(debtor.split(":", 1)[1])
                known = tm is not None
                if (
                    tm is not None
                ):  # 빈 author 도 "아는" 것이다 — 그 주소에게 진 줄이 없을 뿐
                    opener = str(tm.current_author or "")
            return ReplyFacts(
                known=known,
                owed=tuple(self.reply_debts(debtor)),
                opener=opener,
                opener_row=None if opener is None else self.reply_debt(debtor, opener),
                by_id=self.debt_by_id(debtor, id) if id else None,
            )

    def reply_debt(self, debtor: str, to: str) -> ReplyDebt | None:
        """``to`` 에게 진 빚 중 **가장 최근** 것 — 갚았든 아니든 (거부 사유용)."""
        with self._cv:
            for row in reversed(self._post.rows()):
                if self._is_debt_of(row, debtor) and row.src == to:
                    return _debt_view(row)
            return None

    def debt_by_id(self, debtor: str, id: str) -> ReplyDebt | None:
        with self._cv:
            row = self._post.row(str(id))
            return _debt_view(row) if row and self._is_debt_of(row, debtor) else None

    @staticmethod
    def _is_debt_of(row, debtor: str) -> bool:
        """``debtor`` 가 진 빚의 줄인가 — 집었거나(빚) 런 안에서 닫힌(갚은) r 줄, 그리고 사람 말(u — 창이 곧
        배달이라 갚을 것이 없지만 거부 사유가 읽는다). 줄 선 것은 아직 빚이 아니다."""
        return row.dst == debtor and row.kind in ("r", "u") and row.state != "queued"

    def reply_debts(self, debtor: str) -> list[ReplyDebt]:
        """아직 안 갚은 빚 — 사람 주소 제외(창이 곧 배달).

        v10.77.0: 종전 §3.7 억제(이 런이 질문을 건 주소의 빚을 감춤)는 지웠다
        — 에이전트는 이제 질문 대신 `message` 요청을 보내고, 그 회신을 `wait`
        로 기다린 뒤 자기 빚을 `reply` 로 갚는다. 감추던 빚이 열린 채 남으니
        오히려 더 엄밀하다."""
        with self._cv:
            return [_debt_view(r) for r in self._post.owed_by(debtor)]

    def run_ended(
        self,
        debtor: str,
        output: str,
        *,
        stopped: bool = False,
        **meta,
    ) -> int:
        """런 끝의 **유일한** 정리 (v10.77.0) — 에이전트의 런과 main 펌프가
        모두 여기로 온다. 처리한 빚 수를 돌려준다. 받아들여진 `wait` 는 런을 끝내지 않는다(5c-1) — 여기 오지 않는다.

        - ``stopped`` (정지로 끊김): 남은 빚을 버린다. 사람·모델의 정지(■·✕)는 런의 정리가
          먼저 빚진 상대 모두에게 회신했다(`answer_debts_as_stopped` — 여기 남는 것은 사람 주소의
          빚뿐).
        - 그 밖(보통의 끝): :meth:`end_run` — 남은 빚을 라벨 붙은 런 요약으로.

        종전엔 이 판정(``not stop_event``)이 호출부 셋에 흩어져 있었다. ``meta`` 의 ``run`` 키는 그 런을 연 배달
        id 이다. 체크인 타이머는 여기서 풀 것이 없다 — 거는 곳은 제자리 대기 하나이고 깨면 그 ``finally`` 가 푼다."""
        if stopped:
            # 버린 빚 — 요청자는 더 기다리지 않는다(dropped).
            self._post.end_run(debtor, run_id=self._run_of(debtor), outcome="stopped")
            return 0
        return self.end_run(debtor, output, **meta)

    # ── `wait` 중간 깨우기 (v10.79.0, docs/wait/DESIGN.md §6) ──────────────
    #
    # 받아들여진 `wait` 가 한참 깨지 않으면 하니스가 깨운다 — 벤치 50번의 wait 중
    # 13번이 깨지 못한 채 과제 시간으로 끝났고, 헤드리스(run·예약) 세션은 작업에
    # 시간 제한이 없어 끝없이 잠들 수 있었다. 알림은 기존 셋째 갈래(장부 밖 메일,
    # `ledger: "none"`)의 첫 사용자다 — 새 층이 아니다.

    def next_checkin_s(self, owner: str, ids: frozenset[str]) -> int | None:
        """이 소유자가 ``ids``(받아들일 때 읽은 체크인 대상 — `WaitingOn.checkin_ids`)를 기다리면 걸릴 간격(초) —
        없으면(예약만 기다림) None. `dispatch._op_wait` 가 결과 문장에 적고 `arm_checkin` 이 같은 값을 건다."""
        if not ids:
            return None
        return CHECKIN_STEPS_S[
            min(self._checkin_step(owner, ids), len(CHECKIN_STEPS_S) - 1)
        ]

    def _checkin_step(self, owner: str, ids: frozenset[str]) -> int:
        """같은 대상 집합을 직전 깨우기 때도 기다렸으면 다음 단계, 아니면 0.
        진짜 보고로 깨면 집합이 거의 항상 바뀌므로(작업 소멸·빚 정산) 별도
        초기화가 없다. 바뀌지 않는 드문 경우(`once` false 모니터의 재발화)에
        배증이 이어지는 것은 해롭지 않다."""
        with self._cv:
            last = self._checkin_last.get(owner)
        return last[1] + 1 if last is not None and last[0] == ids else 0

    def arm_checkin(self, owner: str, ids: frozenset[str]) -> int | None:
        """제자리 `wait` 가 ``ids``(받아들일 때 읽은 체크인 대상)를 기다리기 시작한다(`Mailbox.arm_checkin`) — 타이머를
        건다. 걸린 간격(초) 또는 None.

        기준을 함께 적는다: 작업별 로그 크기(P(a) "+N lines since your wait")와
        에이전트별 `last_ordinal`("N steps since your wait")."""
        if not ids:
            return None
        step = self._checkin_step(owner, ids)
        interval = CHECKIN_STEPS_S[min(step, len(CHECKIN_STEPS_S) - 1)]
        jobs = get_job_registry()
        log_marks = (
            {j.id: jobs.log_size(j) for j in jobs.running(owner)}
            if jobs is not None
            else {}
        )
        step_marks: dict[str, int] = {}
        for addr in ids:
            if not addr.startswith("agent:"):
                continue
            tm = self._agents.get(addr.split(":", 1)[1])
            ctx = tm.ctx if tm is not None else None
            step_marks[addr] = (ctx.last_ordinal or 0) if ctx is not None else 0
        ck = _Checkin(
            ids=ids,
            step=step,
            interval_s=interval,
            log_marks=log_marks,
            step_marks=step_marks,
        )
        timer = self.checkin_timer_factory(interval, self.fire_checkin, args=(owner,))
        timer.daemon = True
        ck.timer = timer
        with self._cv:
            self._checkin_armed[owner] = ck
        timer.start()
        return interval

    def cancel_checkin(self, owner: str) -> None:
        """이 소유자에게 걸린 체크인 타이머를 푼다 — 제자리 대기가 깰 때(`TurnDispatcher._op_wait` 의 ``finally``),
        주인이 사라질 때(`drop_mailbox`·kill·`shutdown_all`). 걸린 것이 없으면 no-op."""
        with self._cv:
            ck = self._checkin_armed.pop(owner, None)
        if ck is not None and ck.timer is not None:
            ck.timer.cancel()

    def fire_checkin(self, owner: str) -> str:
        """타이머가 부른다(테스트는 직접) — 알림을 만들어 배달하고 본문을 돌려준다.
        걸린 것이 없거나(먼저 풀렸다) 아무것도 안 돌고 있으면 "".

        알림은 사실 진술("still running: …")이라 진짜 보고와 같은 순간에 와도
        거짓이 아니다 — 그래서 "런 진행 중" 상태도, 락 안의 확인+추가도 없다.
        main 이면 메일함(`_push_reply` — 한가한 펌프가 런을 연다), 상주면 받은편지함
        (`request`, 봉투 ``checkin``). 둘 다 ``ledger: "none"`` — 장부 항목이 되지 않는다."""
        with self._cv:
            ck = self._checkin_armed.pop(owner, None)
        if ck is None:
            return ""
        next_s = CHECKIN_STEPS_S[min(ck.step + 1, len(CHECKIN_STEPS_S) - 1)]
        text = wait_checkin(
            owner,
            interval_s=ck.interval_s,
            next_s=next_s,
            log_marks=ck.log_marks,
            step_marks=ck.step_marks,
            registry=self,
        )
        if not text:
            return ""
        with self._cv:
            self._checkin_last[owner] = (ck.ids, ck.step)
        if owner == "main":
            self._push_reply(
                {
                    "kind": "checkin",
                    "ledger": "none",
                    "success": True,
                    "output": text,
                    "author": "main",
                    "expects_reply": False,
                }
            )
        elif owner.startswith("agent:"):
            self.request(
                owner.split(":", 1)[1],
                text,
                author="main",
                expects_reply=False,
                kind="checkin",
                ledger="none",
            )
        elif is_private_owner(owner):
            self._push_private(
                owner,
                {
                    "kind": "checkin",
                    "ledger": "none",
                    "success": True,
                    "output": text,
                    "author": "main",
                    "expects_reply": False,
                },
            )
        return text

    def end_run(self, debtor: str, output: str, **meta) -> int:
        """``debtor`` 의 런이 끝났다 — 남은 회신 빚은 라벨 붙은 런 요약을
        폴백 배달한다. 건수를 돌려준다.

        독촉은 런 **안**에서 3회 있었다(dispatch 의 빚 목록). 배관만 갈린다:
        main → peer 는 `request`(inbox), 상주 → peer 는 `_deliver_peer_reply`
        (terminal 재주입), 상주 → main 은 mailbox `kind:"reply"`(``meta`` 가
        run(그 런을 연 배달 id)/success/duration/reply_path 를, 실패면 error(엔진의 사유)를 싣는다).
        실패한 런의 회신은 머리에 원인 한 줄(`agent_mail.run_error_line`) — main 은 관찰 레코드가, 에이전트
        요청자는 여기서 단다. main 의 런 끝(``meta`` 없음)은 성공으로 본다.
        """
        n = 0
        summary = f"{_NO_REPLY_LABEL}\n{output}"
        # 에이전트 요청자의 회신에도 실패 머리 한 줄 — main 의 메일은 관찰 레코드(`build_reply_record`)가 단다
        peer_summary = (
            summary
            if meta.get("success", True)
            else f"{run_error_line(str(meta.get('error') or ''))}\n{summary}"
        )
        for d in self.reply_debts(debtor):  # 폴백의 배달이 곧 갚음(``settles``)
            n += self._fall_back(debtor, d, summary, peer_summary, meta)
        # 남은 줄(배달 못 한 빚 — dropped, 사람 말 — run_ended)을 닫는다
        self._post.end_run(debtor, run_id=self._run_of(debtor), outcome="done")
        return n

    def _fall_back(
        self, debtor: str, d: ReplyDebt, summary: str, peer_summary: str, meta: dict
    ) -> int:
        """빚 하나의 폴백 회신 — 배달했으면 1. main 은 상대에게(받은편지함), 상주는 main(메일함)·요청자에게."""
        if debtor == "main":
            err = self._deliver_peer_reply(
                d.to.split(":", 1)[1], "main", peer_summary, 0, re=d.id, settles=d.id
            )
            return 0 if err else 1
        key = debtor.split(":", 1)[1]
        tm = self._agents.get(key)
        if d.to == "main":
            self._push_reply(
                {
                    "kind": "reply",
                    "key": key,
                    "profile": tm.profile_name if tm else "",
                    "output": summary,
                    "answers": list(d.answers),
                    **meta,
                    "re": d.id,  # 이 요약이 대신하는 회신의 요청
                },
                settles=d.id,
            )
        else:
            self._deliver_peer_reply(
                d.to.split(":", 1)[1], key, peer_summary, d.hop, re=d.id, settles=d.id
            )
        return 1

    def _deliver_answer(self, q: Question, text: str) -> str:
        """사람의 답을 asker 에게 — 새 배달(사람 발신이라 ``u-``)로. `request` 의 거절(묻는 쪽이 받지 못함) 또는 "".

        사람이 보낸 것이므로 그 런은 사람 런이다: 결과는 창이 곧 배달이고, 닫는
        법은 사람 요청과 같다(`complete` 의 `answers`). 봉투가 무엇에 대한
        답인지(``re``) 적는다."""
        return self.request(
            q.asker,
            f"Your question: {q.text}\nAnswer: {text}",
            author=q.target,
            expects_reply=True,
            kind="answer",
            re=q.id,
            todo="Continue the work it was blocking.",
            settles=q.id,
        )

    def _drop_monitors(self, key: str) -> int:
        """이 에이전트가 건 감시·띄운 백그라운드 작업을 폐기 — private 종류와 같은
        함수(`wake.release_owner`, v10.89.0). 메일함·체크인은 부르는 쪽이 따로(kill·종료는
        자기 순서로 취소하고, 죽음의 정본 자리는 레지스트리까지 넘긴다)."""
        return release_owner(
            f"agent:{key}", jobs=self.jobs, monitors=self.monitors
        ).count

    def _purge_questions_for(self, key: str) -> None:
        """그가 건 질문을 닫고(받을 곳이 없다) 저장·명단 알림 — 영구 사망(✕·크래시, ``not revivable``)과 답이
        거절됐을 때(`answer_question`)만 부른다. 세션 종료(``shutdown_all``)에서 지우면 직후의 ``_save_state`` 가
        빈 목록을 저장해 resume 이 질문을 되살리지 못한다.
        """
        outgoing = self._post.forget_questions_of(f"agent:{key}")
        if outgoing:
            self._notify_roster()

    # ── spawn ───────────────────────────────────

    def _model_unavailable(self, model: str, profile: str, binding=None) -> str:
        """역할이 요구한 모델이 서버에 없으면 사람이 읽을 이유를, 아니면 ""."""
        from agent_cli.model_check import ModelNotFound, NoModelSelected, verify_model

        if binding is None:
            binding = (self.runtime or {}).get("binding")
        if binding is None or not binding.base_url:
            return ""  # 확인할 수단이 없으면 막지 않는다 (부팅 정책과 동일)
        try:
            verify_model(
                model,
                binding.base_url,
                binding.credentials,
                binding.provider_name or "openai",
            )
        except ModelNotFound as e:
            near = e.listing.suggest(model)
            # 모델이 읽는 거절 관찰이다(spawn 의 결과) — 영어로, 다음 행동과 함께.
            hint = f" (closest name: {near})" if near else ""
            avail = ", ".join(e.listing.models[:6])
            where = f'profile "{profile}"' if profile else "the role settings"
            server = (
                f'server "{binding.endpoint}"'
                if getattr(binding, "endpoint", "")
                else "the server"
            )
            return (
                f'the model "{model}" required by {where} is not on {server}'
                f"{hint}. Available: {avail}. Spawn with a different profile, "
                "or tell the user the profile's model must be fixed."
            )
        except NoModelSelected:
            return ""
        return ""

    def _duplicate_label(self, profile: str, name: str) -> str:
        """같은 **표시 이름**을 가진 개체가 이미 있으면 거절 사유, 아니면 "".

        표시 이름 = ``(profile, name)`` 쌍이다 — 사람이 채널 칩에서 보는 것이
        그것이고, 겹치면 **글자로 구별할 수 없다**(실사고: `correctness` 칩이
        둘, 아이콘만 🐺/🐸 로 달랐다). 키는 유일하므로 배달이 틀리지는 않지만,
        어느 쪽에 말을 거는지 사람이 알 수 없는 것 자체가 고장이다.

        죽은 개체도 센다. 오히려 그쪽이 흔한 경로다 — 죽어서 로스터에서
        사라진 개체를 모델이 못 보고 새로 띄운 뒤, 사용자가 원본을 resume 하면
        같은 이름 둘이 동시에 살아난다. 그래서 죽었으면 **resume 을 가리킨다**.
        """
        if not profile and not name:
            return ""  # 익명 즉석 에이전트끼리는 겹칠 이름이 없다
        for tm in list(self._agents.values()):
            if tm.ends_after_task:
                continue  # run 은 과제 하나의 일꾼이다 — 같은 프로파일 run 여럿은 정상
            if (tm.profile_name, tm.instance_name) != (profile, name):
                continue
            label = " · ".join(p for p in (profile, name) if p)
            if tm.state == "dead":
                return DUPLICATE_DEAD.format(label=label, key=tm.key)
            return DUPLICATE_ALIVE.format(label=label, key=tm.key)
        return ""

    def spawn(
        self,
        *,
        profile: str = "",
        name: str = "",
        instructions: str = "",
        allowed_tools: list[str] | None = None,
        context_mode: str = "none",
        parent_ctx=None,
        runtime: dict | None = None,
        agent_stack: list[str] | None = None,
        ends_after_task: bool = False,
        task_summary: str = "",
        parent: str = "",
    ) -> tuple[str, str]:
        """에이전트 생성 — ``(key, error)``. 성공 시 error 는 빈 문자열. run·spawn 한 길
        (docs/agent-engine/DESIGN.md §2): ``ends_after_task`` 면 과제 하나의 회신 뒤 내려온다.

        ``runtime`` 은 띄우는 쪽(호출자 루프)의 실행 배선(`AgentRuntime` 사전) — 깊이·순환
        검사와 그 에이전트의 바인딩·턴 상한이 이것을 본다(없으면 레지스트리의 것 = main).
        ``agent_stack`` 은 호출자의 스택, ``parent`` 는 호출자 주소(✕ 가 띄운 run 을 함께 멈춘다),
        ``task_summary`` 는 설명이 없을 때 로스터에 보일 과제 첫 문장.

        역할 로드·fork 전제 검사는 여기서 동기로 (즉시 거부), ctx 생성은
        worker 스레드에서 (인스펙터 스코프가 worker 스레드 키라서 — D9).
        """
        rt = dict(runtime) if runtime else dict(self.runtime)

        if self._at_agent_limit():
            return "", self.agent_limit_refusal()
        # 깊이·순환 — run·spawn 같은 장치(`delegation_checks`), 호출자의 깊이·스택으로.
        refusal = depth_or_recursion_refusal(
            profile,
            list(agent_stack or []),
            rt.get("depth", 0),
            rt.get("max_depth", 2),
        )
        if refusal:
            return "", refusal

        if name and not re.match(r"^[a-zA-Z0-9_-]{1,24}$", name):
            return "", (f'invalid instance name "{name}": [a-zA-Z0-9_-], max 24 chars')

        dup = "" if ends_after_task else self._duplicate_label(profile, name)
        if dup:
            return "", dup

        role_prompt = ""
        description = ""
        config: dict = {}
        binding = rt.get("binding")
        model = binding.model if binding is not None else ""
        hooks_config = rt.get("hooks_config")
        if profile:
            from agent_cli.subagent.profiles import load_profile
            from agent_cli.subagent.runner import (
                ProfileToolsError,
                apply_role_overrides,
            )

            body, config, error = load_profile(profile)
            if error:
                return "", error
            role_prompt = body or ""
            description = str(config.get("description", "") or "")
            inherited = model
            try:
                allowed_tools, model, hooks_config = apply_role_overrides(
                    config,
                    allowed_tools=allowed_tools,
                    model=model,
                    hooks_config=hooks_config,
                )
            except ProfileToolsError as e:
                return "", str(e)
            # 역할 md 가 상속 모델을 덮어썼다면 그 이름도 확인한다 (v9.5.0).
            # 상속분은 부팅 때 이미 검증됐지만 역할이 지정한 이름은 처음 보는
            # 값이고, 안 보면 **그 에이전트의 첫 턴에 가서야** 404 로 드러난다
            # (사용자 제보: 대화 한복판의 빨간 거부 카드). spawn 을 거절하는
            # 편이 훨씬 싸고, 고칠 파일까지 지목할 수 있다.
            if model and model != inherited:
                bad = self._model_unavailable(model, profile, binding)
                if bad:
                    return "", bad

        # instant-agent 역할 캡처 (v5.12): 프로파일 description 이 없고
        # 인라인 instructions 만 있으면 그 첫 문장을 로스터 역할 요약으로.
        # → 프로파일 없는 즉석 에이전트도 동료들에게 역할이 보인다.
        if not description and instructions:
            description = first_sentence(instructions)
        if not description and task_summary:
            description = first_sentence(task_summary)

        # instant-agent (U4): 인라인 지시를 파일 본문 뒤에 합성 —
        # 합성본이 이 개체의 정체성(role_prompt)이 되어 manifest 로 영속.
        role_prompt = compose_role_prompt(role_prompt, instructions)

        if context_mode == "fork" and parent_ctx is None:
            return "", "fork requires parent context"

        if self.session_dir is None:
            return "", "agents require a session dir (headless run not supported)"

        key = f"agt-{uuid.uuid4().hex[:8]}"
        tm = AgentInstance(
            key,
            profile_name=profile,
            role_prompt=role_prompt,
            allowed_tools=allowed_tools,
            model=model,
            hooks_config=hooks_config,
            context_mode=context_mode,
            home_dir=self.session_dir / AGENTS_DIR / key,
            instance_name=name,
            description=description,
            instructions=instructions,
            ends_after_task=ends_after_task,
            caller_runtime=rt,
            agent_stack=agent_stack,
            parent=parent,
        )
        self._agents[key] = tm
        tm.worker = threading.Thread(
            target=self._worker,
            args=(tm, parent_ctx),
            daemon=True,
            name=f"agent-{key}",
        )
        tm.worker.start()
        self._save_state()
        self._notify_roster()
        return key, ""

    # ── request / 회신 ──────────────────────────

    def request(
        self,
        key: str,
        message: str,
        *,
        author: str = "main",
        hop: int = 0,
        expects_reply: bool = True,
        answers: list | None = None,
        images: list[dict] | None = None,
        item_id: str = "",
        kind: str = "",
        re: str = "",
        todo: str = "",
        ledger: str = "",
        mark: str = "",
        opens_run: bool = True,
        settles: str = "",
        lines=(),
    ) -> str:
        """request 큐잉 — 에러 메시지 또는 빈 문자열.

        ``settles`` (v10.145.0): 이 배달이 닫는 줄 — 갚는 빚·답하는 질문의 id. 갚는 자리만 준다(`_post_deliver`).
        항목에 실리지 않는다. ``lines``: 이 배달의 다른 창 줄(보낸 쪽 out, `_outbound_line`) — in 과 함께 기록.

        ``ledger`` (v10.79.0): ``"none"`` 이면 장부 밖 메일 — 장부 항목이 되지 않는다. 첫 사용자는 `wait` 의 중간
        깨우기(``checkin``).

        ``item_id`` (v10.77.0): 이 배달의 id — 보낸 쪽이 미리 받아 두면(그
        id 를 결과에 실어 회신과 짝짓는다) 그대로 쓴다. 없으면 종류대로 새로
        받는다: 사람 발신은 ``u-``, 회신을 기다리는 요청은 ``r-``, 그 밖(통지·
        회신 도착)은 ``n-``. 이 id 가 배달의 유일한 키다.

        ``kind``·``re``·``todo``·``mark`` (v10.77.0, D · ``mark`` v10.142.0): 봉투의 재료 — ``"notice"``·
        ``"reply"``·``"answer"``. 봉투는 받는 런이 항목을 읽을 때 그린다
        (:meth:`_model_text`) — 창과 대화 로그에는 본문만 남는다.

        ``images`` (v10.61.0): 사람이 창에서 붙인 이미지 참조(``ImageRef`` dict,
        웹 `/api/agent/{key}/input` 의 ``images``) — inbox 항목과 🤝 창 메시지에
        실리고 그 런의 시작 user 레코드 ``images`` 가 된다(docs/vision/DESIGN.md §5).

        ``answers`` (v9.24.5): 귀속 스냅샷을 호출자가 준다 — 재시작 뒤 끊긴
        런을 다시 넣을 때 원래 요청의 스냅샷을 잇기 위해. 없으면 종전대로
        main 발신이면 지금 런의 USER 목록.

        ``expects_reply`` (v5.11): 이 아이템 처리 후 산출물을 발신자에게
        되돌릴지. main/watch/user·peer 요청=True(회신 라우팅), 배달된 peer
        회신=False(terminal — 수신자는 소비만, 재라우팅 없음 → 핑퐁 방지).
        ``hop`` 은 peer 재주입 깊이(_MAX_PEER_HOPS 안전망).
        """
        resolved = self.resolve_key(key)
        tm = self._agents.get(resolved) if resolved else None
        if tm is None:
            return (
                f'unknown agent "{key}" — use a key or name from `## Live Agents` '
                '(or "main")'
            )
        key = resolved
        # `state` 만 보면 창이 뚫린다: `kill`/`shutdown_all` 은
        # `stop_event` 만 세우고 `state="dead"` 는 워커의 `finally` 에서야
        # 찍히므로, busy 런이면 몇 분 뒤다. 그 사이 큐에 넣은 항목은
        # 워커가 다시 꺼내지 않아 **영영 안 읽힌다**. `tm.stop_event` 를
        # 세우는 곳은 그 둘뿐이라(SIGINT 핸들러는 메인 스레드 전용, 웹
        # `/api/stop` 은 main 의 이벤트) 죽지 않는데 서는 경로가 없다.
        if not self._agent_alive(f"agent:{key}"):
            reason = f" ({tm.error})" if tm.error else ""
            return f'agent "{key}" is dead{reason} — spawn a new one'
        if is_blank_words(message, images):  # 이미지만 든 말도 일감이다
            return "empty message"
        # Stamp the request's time BEFORE enqueuing. The worker runs on its own
        # thread and can dequeue + emit ``begin_agent_work`` (scope_start) the
        # instant the item lands in the inbox — i.e. before this method reaches a
        # post-``put`` ``time.time()``. Capturing here guarantees the request's
        # timestamp precedes any work it triggers, so the team view shows the
        # request ABOVE (before) the work bar, not after it.
        send_ts = time.time()
        # id 가 키다 (v10.77.0) — 전역 할당기라 동시 request() 에도 겹치지 않는다
        # (종전 에이전트별 순번은 P0-9a 의 락과 v8.5.1 의 키 덧붙이기가 필요했다).
        if not item_id:
            if is_human_addr(author):
                item_id = item_ids.next_id(item_ids.USER)
            elif expects_reply:
                item_id = item_ids.next_id(item_ids.REPLY)
            else:
                item_id = item_ids.next_id(item_ids.MAIL)
        item = {
            "id": item_id,
            "text": message,
            "author": author,
            "hop": hop,
            "expects_reply": expects_reply,
            # 발신 시각 — 🤝 창의 수신 줄(agent_msg "in", ts=send_ts)과
            # 작업 카드(begin_agent_work→scope_start 의 `nav_ts`)가 같은 값을 갖는다.
            "ts": send_ts,
        }
        if kind:
            item["kind"] = kind
            if re:
                item["re"] = re
            if todo:
                item["todo"] = todo
            if mark:
                item["mark"] = mark
        if ledger:
            item["ledger"] = ledger
        if not opens_run:
            # 런을 열지 않는 항목 (v10.142.0, "Stop everything" 의 정지 회신) — 봉투의 `wakes` 가 거짓이라
            # 워커를 깨우지 않고 줄에 남아 다음 런의 첫 흡수가 읽는다(`Postbox.take_next`). 재시작은 버린다(5c-0).
            item["opens_run"] = False
        if images:
            item["images"] = [dict(r) for r in images]
        if answers is not None:
            item["answers"] = list(answers)
        elif author == "main":
            # 귀속 승계: 이 요청을 만든 런의 USER 들을 요청에 스냅샷 —
            # 회신이 그대로 되가져와, 회신을 접는 런(🤝 웨이크 포함)의
            # 최종답이 원 요청자에게 귀속된다. peer/user 발신은 회신이
            # main mailbox 로 안 가므로 스냅샷 불필요.
            item["answers"] = list(self._current_run_authors)
        # 답이든 일감이든 **항상** 창·로그·로스터에 남긴다 — 답만 건너뛰면
        # 🤝 창과 conversation.jsonl(=resume 재생 소스)에서 사라진다. 기록은 넣기와 한 구간(`_enqueue`).
        payload = {
            "key": key,
            "direction": "in",
            "author": author,
            "text": message,
            "item": item_id,
            # 수신자 = 이 에이전트. 누락 시 렌더러 기본값 "main" 이 실려
            # 의미가 뒤집힌다.
            "to": key,
            "ts": send_ts,
            # 표시용 신원 — CLI 는 로스터를 못 보므로 발신자가 실어 보낸다
            # (웹은 늦게-해소가 replay 에 강해 로스터를 계속 쓴다). v9.9.0
            "profile": tm.profile_name,
            "instance_name": tm.instance_name,
        }
        if images:
            payload["images"] = [dict(r) for r in images]  # 창의 썸네일 (v10.61.0)
        self._enqueue(tm, item, settles=settles, lines=[(tm, payload), *lines])
        self._notify_roster()
        return ""

    def _enqueue(
        self, tm: AgentInstance, item: dict, *, settles: str = "", lines=()
    ) -> None:
        """받은편지함(우편의 그 주소 상자)에 넣는다. 회신을 기다리는 요청이면
        그 줄이 보낸 쪽의 "기다리는 회신" 이다(v10.141.0 — 넣는 것과 한 락 구간). ``lines`` 는 이 배달의 대화
        줄(받는 쪽 in·보낸 쪽 out) — 넣기가 성공한 같은 구간에서 `_lines_in_section`, 그 뒤 `_lines_after_section`."""
        with self._cv:
            self._post_deliver(
                envelope_of_item(item, f"agent:{tm.key}"), settles=settles
            )
            self._lines_in_section(lines)
        self._lines_after_section(lines)

    def _who(self, author: str) -> str:
        """봉투의 ``from`` — 에이전트는 이름표(`format_agent_label`), 사람은
        "the user", main 은 "main"."""
        if author == "main":
            return "main"
        if is_human_addr(author):
            return "the user"
        key = author.split(":", 1)[1] if author.startswith("agent:") else author
        tm = self._agents.get(key)
        if tm is None:
            return key
        return format_agent_label(key, tm.profile_name, tm.instance_name)

    def _model_text(
        self, item: dict, *, mid_run: bool = False, bare: bool = False
    ) -> str:
        """받은편지함 항목 하나 → 모델이 읽는 글 (v10.77.0, D — 봉투 한 모양).

        - 봉투 재료(``kind``)가 있으면 그 봉투: 통지·회신·사람의 답.
        - 에이전트(또는 런 도중의 main)가 보낸 요청은 ``request`` 봉투 — 괄호 줄이
          그 id 로 갚으라고 말한다. 런 도중이면 "지금 일도 열려 있다" 를 덧붙인다.
        - 런을 여는 main 의 요청은 일감 그 자체라 본문 그대로, 사람의 창 입력은
          ``[user:<nick>]: `` 라벨(사람 런 회계가 따로 있다).

        ``bare``: 사람 항목을 장부(`user_requests`)로 넘길 때 — 라벨 없이 본문만.
        """
        author = item.get("author", "main")
        text = item["text"]
        iid = str(item["id"])
        kind = item.get("kind") or ""
        if harness_mail_tool(kind) is not None:
            return text  # 하니스 통지 — 봉투 밖 (main 의 `build_reply_record` 와 같다)
        if kind:
            return envelope(
                kind,
                iid,
                self._who(author),
                text,
                re=str(item.get("re") or ""),
                todo=str(item.get("todo") or ""),
                mark=str(item.get("mark") or ""),
            )
        if is_human_addr(author):
            return text if bare else f"[{author}]: {text}"
        if item.get("expects_reply", True) and (mid_run or author != "main"):
            short = "main" if author == "main" else author.split(":", 1)[-1]
            todo = owe_line(short, iid)
            if mid_run:
                todo += " Your current task stays open; see ## Open Items."
            return envelope("request", iid, self._who(author), text, todo=todo)
        return text if author == "main" else f"[{author}]: {text}"

    def absorb_pending(self, tm: AgentInstance) -> list[dict]:
        """턴 경계 흡수 (v9.25.0) — 받은편지함의 대기분을 **지금 도는 런**으로.

        루프의 메일함 포트(``mailbox.take``)가 이걸 부른다. 여기서 하는 일은 런 시작
        (`ResidentLoop.start`)이 항목 하나에 하는 것과 같다 — 처리 중 목록으로
        옮기고(재시작 복원 대상), 요청이면 빚을 적고, 질문이면 배달로 찍는다.
        돌려주는 것은 루프가 그대로 넣을 수 있는 모양이다:
        ``{"user_request": {id, author, text}}`` (사람 창 입력 — main 의 큐
        주입과 같은 회계) 또는 ``{"record": 관찰 레코드}``.

        ``tool=""`` 은 형식 개입 표식이라 레코드에 절대 쓰지 않는다. 정지가 섰으면 꺼내지 않는다.
        """
        if tm.stop_event.is_set():
            return []
        me = f"agent:{tm.key}"
        with self._cv:
            envs = self._post.absorb(me)
            tm.absorbed_in_run += len(envs)
        items = [dict(e.payload) for e in envs]  # 도착 순서(v10.145.0 예외 D2)
        if not items:
            return []
        out: list[dict] = []
        for item in items:
            author = item.get("author", "main")
            text = item["text"]
            iid = str(item["id"])
            # 웹의 ⏳ 대기 줄은 그 id 로 **새 런**이 열릴 때만 블록 머리로 옮겨졌다
            # — 런 도중 흡수는 새 런을 열지 않아 배달 뒤에도 "대기" 로 남았다
            # (실측 67qcmb/qa: 7분 생성 중 도착 → 다음 경계에 배달, 카드는 그대로).
            # 흡수를 알리고 conversation.jsonl 에도 남겨 resume 재생이 같게 한다.
            absorbed = {
                "key": tm.key,
                "direction": "absorbed",
                "author": author,
                "text": text,
                "item": iid,
                "to": tm.key,
                "ts": time.time(),
                "absorbed_into": tm.current_item,
            }
            self._record(tm, absorbed)
            if is_human_addr(author):
                # 빚은 아니지만 `reply` 거부 사유가 이걸 본다
                uid = iid
                self.note_owes(me, author, text, id=uid)
                out.append(
                    {
                        "user_request": {
                            "id": uid,
                            "author": author,
                            "text": self._model_text(item, bare=True),
                            "images": item.get("images") or [],
                        }
                    }
                )
                continue
            if item.get("expects_reply", True):
                rid = iid
                self.note_owes(
                    me,
                    author,
                    text,
                    answers=item.get("answers") or [],
                    hop=item.get("hop", 0),
                    id=rid,
                )
                rec = {
                    "role": "user",
                    "tool": "agent",
                    "success": True,
                    "content": self._model_text(item, mid_run=True),
                    "source": "agent_request",
                    "request_id": rid,  # 빚 id — 갚기 전 압축이 원문을 남긴다(`Ledger.pin_ids`)
                }
            else:
                # 회신·통지 — 런 시작 항목과 같은 봉투. 하니스 통지(잡·모니터·체크인)는
                # main·one-shot 과 같은 함수(`wake.harness_record`, v10.89.0).
                rec = harness_record(item) or {
                    "role": "user",
                    "tool": "agent",
                    "success": True,
                    "content": self._model_text(item),
                    "source": "agent_mail",
                }
            # ``item``: 그 배달의 id — 화면 전용(웹이 관찰 카드에 달아 블록 머리 줄에서 찾아간다). 모델은 읽지 않는다
            out.append({"record": {**rec, "item": iid}})
        self._notify_roster()
        return out

    def _close_run(self, tm: AgentInstance) -> None:
        """런 끝의 마무리.

        ``handled`` 증가와 처리 중 표시 해제(v9.24.5)를 **한 번의 저장**에
        싣는다. 해제를 따로 저장하면 회신이 나간 뒤 다음 항목을 꺼내기까지
        디스크 쓰기가 한 번 더 끼어, 그 사이 idle 로 보이는 창이 넓어진다."""
        with self._cv:
            # 런을 연 항목 하나와 턴 경계의 흡수분(v9.25.0)까지 이 런이 처리했다
            tm.handled += 1 + tm.absorbed_in_run
            tm.absorbed_in_run = 0
            # 든 것과 이 런에서 닫힌 줄은 런 끝 정리(`run_ended` → `end_run(done|stopped)`)가 이미 비웠다
        self._save_state()  # ``handled`` 를 싣는다 — 증가 뒤에
        tm.state = "idle"
        tm.current_author = "main"
        tm.current_item = ""
        self._notify_roster()

    def deliver(
        self,
        addr: str,
        *,
        mail: dict,
        text: str,
        author: str,
        expects_reply: bool,
        hop: int = 0,
    ) -> str:
        """주소 하나로 배달 — 백엔드 둘. 에러 문자열 또는 "".

        ``addr`` 은 ``"main"`` 또는 ``"agent:<key>"``. 모니터·작업 통지가 이
        한 곳으로 배달된다.

        main 은 메일함에 ``mail``(구조)을, 에이전트는 받은편지함에 ``text``(평문)를, private 주소는
        private 메일함에 ``mail`` 을 받는다.
        """
        try:
            if addr == "main":
                self._push_reply(mail)
                return ""
            if addr.startswith("agent:"):
                # 종류·장부 표지를 봉투 재료로 넘긴다 (v10.84.0) — 종전엔 떨어뜨려
                # 잡·모니터 통지가 체크인과 달리 봉투 없이 갔다.
                return self.request(
                    addr.split(":", 1)[1],
                    text,
                    author=author,
                    expects_reply=expects_reply,
                    hop=hop,
                    kind=str(mail.get("kind") or ""),
                    ledger=str(mail.get("ledger") or ""),
                )
            if is_private_owner(addr):
                self._push_private(addr, mail)
                return ""
        except Exception as exc:  # 배달은 감시 폴링 스레드에서도 불린다
            # **던지지 않는다.** `MonitorRegistry._flush` 가 in-flight
            # 카운터를 `finally` 로 내리긴 하지만, 여기서 새면 `_loop` 의
            # `except Exception: pass` 가 삼켜 그 틱의 남은 모니터가 통째로
            # 건너뛰어진다. 에러는 문자열로 돌려 호출부가 판단하게 한다.
            return f"{type(exc).__name__}: {exc}"
        return f'unroutable address "{addr}"'

    def _push_reply(self, reply: dict, *, settles: str = "", lines=()) -> None:
        with self._cv:  # 배달 순간의 한가함은 배달 구간에서 읽는다 — 락을 놓으면 펌프가 이 메일로 런을 열 수 있다
            self._stamp_mail(reply)
            self._post_deliver(envelope_of_main_mail(reply), settles=settles)
            idle_at_delivery = self._post.idle("main")
            self._lines_in_section(lines)
        self._lines_after_section(lines)
        cb = self.on_reply
        if cb is not None:
            try:
                cb(reply, idle_at_delivery=idle_at_delivery)
            except Exception:
                pass  # 알림은 best-effort — 배달 경로를 막지 않는다
        self._pending_changed()

    def _stamp_mail(self, reply: dict) -> None:
        """메일 한 통의 id — ``_cv`` 를 쥔 채로 부른다. 접두는 종류대로(회신을
        기대하면 ``r-``, 그 밖은 ``n-``). 이미 있으면(복원분)
        그대로 둔다 — id 가 그 메일의 키다(v10.77.0)."""
        if reply.get("item_id"):
            return
        if reply.get("expects_reply"):
            # 회신을 기대하는 message — main 이 이 id 로 빚진다(`reply`)
            reply["item_id"] = item_ids.next_id(item_ids.REPLY)
        else:
            reply["item_id"] = item_ids.next_id(item_ids.MAIL)

    def _pending_changed(self) -> None:
        cb = self.on_pending_change
        if cb is not None:
            try:
                cb()
            except Exception:
                pass  # 화면 갱신은 best-effort

    def pending_mail(self) -> list[dict]:
        """main 앞 미배달 메일의 대기열 줄 (:func:`mail_queue_item`)."""
        return [mail_queue_item(dict(e.payload)) for e in self._post.mail("main")]

    def _deliver_peer_reply(
        self,
        requester_key: str,
        from_key: str,
        output: str,
        hop: int,
        *,
        re: str = "",
        mark: str = "",
        opens_run: bool = True,
        settles: str = "",
        lines=(),
    ) -> str:
        """요청의 회신을 요청자 inbox 로 terminal 재주입 (v5.11) — 에러 또는 "". ``settles``·``lines``: `request` 대로.

        ``from_key`` 는 에이전트 키 또는 ``"main"``(main 이 `reply` 로 갚을 때).
        ``expects_reply=False`` 로 넣어 요청자가 소비만 하고 되받아치지 않게
        한다(핑퐁 방지). 봉투(``reply``)가 무엇에 대한 회신인지(``re``) 적는다
        (v10.77.0). 상한(_MAX_PEER_HOPS) 초과나 요청자 부재/사망 시 드롭 —
        best-effort 배관(데드락은 비동기라 불가).
        """
        if hop >= _MAX_PEER_HOPS:
            return f"reply dropped — peer hop limit ({_MAX_PEER_HOPS}) reached"
        if not self._agent_alive(f"agent:{requester_key}"):
            return REPLY_REQUESTER_GONE.format(key=requester_key, rid=re)
        return self.request(
            requester_key,
            output,
            author="main" if from_key == "main" else f"agent:{from_key}",
            hop=hop + 1,
            expects_reply=False,
            kind="reply",
            re=re,
            **({"mark": mark} if mark else {}),  # 정지 회신만 표지를 단다
            **({} if opens_run else {"opens_run": False}),
            settles=settles,
            **({"lines": lines} if lines else {}),  # 보낸 쪽 out 은 `_pay_debt` 만
        )

    def message_to_main(
        self,
        from_key: str,
        text: str,
        *,
        profile: str = "",
        name: str = "",
        answers: list | None = None,
        expects_reply: bool = False,
        notice: bool = False,
        re: str = "",
        settles: str = "",
    ) -> str:
        """상주 에이전트 → main 메시지 (v5.11). main 은 자기 메일함(우편의 ``main`` 상자)으로 받아 턴 경계
        관찰로 본다 (peer↔main 대칭).

        ``notice`` (v10.1.0): 빚 없는 통지(`message` with expects_reply=false) —
        main 의 관찰이 "회신" 도 "요청" 도 아닌 세 번째 모양으로 갈린다.

        ``expects_reply`` (v9.22.0): `message` 는 True(main 이 회신을 빚진다),
        `reply` 는 False(답이다). main 의 메일 관찰이 이 값으로 갈린다.

        ``answers`` (v9.21.0): main 의 요청에 대한 **회신**이면 그 요청의
        귀속 스냅샷을 그대로 싣는다 — 종전엔 자동 회신(`kind:"reply"`)이
        실어 나르던 것. 자동 회신이 없어졌으니 명시 message 가 실어야
        main 의 run_authors 합류(v8.5.0 승계)가 유지된다.

        그 메일의 id 를 돌려준다(v10.77.0 — `message` 의 결과가 말한다)."""
        rec = {
            "kind": "peer_message",
            "key": from_key,
            "profile": profile,
            "name": name,
            "success": True,
            "output": text,
        }
        if answers is not None:
            rec["answers"] = list(answers)
        rec["expects_reply"] = bool(expects_reply)
        if notice:
            rec["notice"] = True
        if re:
            rec["re"] = re  # `reply` — main 의 어느 요청에 대한 답인가
        # 회신을 기대하면 그 줄이 보낸 쪽의 기다리는 회신이다(넣는 것과 한 락 구간). ``settles`` 는 `request` 와 같다.
        # ★v7.11.1 (실사고): 메일함만 채우면 발신 에이전트의 🤝 창·conversation.jsonl 에 흔적이 없다 — out 으로 남긴다.
        self._push_reply(
            rec, settles=settles, lines=self._outbound_line(from_key, text, to="main")
        )
        return str(rec.get("item_id") or "")

    def _outbound_line(
        self, from_key: str, text: str, *, to: str, ts: float | None = None
    ) -> list[tuple[AgentInstance, dict]]:
        """발신 에이전트 창의 out 줄 — 만들기만 한다(적고 내는 것은 배달, `lines`). 없으면 빈 목록. ``ts`` 는 호출자가
        send 직전에 잡은 시각 — 수신 워커의 작업(scope_start)보다 늦게 찍혀 팀 뷰에서 요청이 작업 아래로 밀리지 않게."""
        tm = self._agents.get(from_key)
        if tm is None:
            return []
        payload = {
            "key": from_key,
            "direction": "out",
            "author": from_key,
            "text": text,
            # 이 발신을 낸 런 — 웹이 폴백 줄을 그 런 블록에 단다
            "item": tm.current_item,
            "success": True,
            "to": to,
            "ts": ts if ts is not None else time.time(),
            "profile": tm.profile_name,
            "instance_name": tm.instance_name,
        }
        return [(tm, payload)]

    def message_handler(self, sender: str):
        """``message`` 도구의 라우팅 훅 — 보내는 주소(``"main"`` | ``"agent:<key>"``) 하나에
        하나 (v5.11; v10.141.0 main 에도). ``(to, text, expects_reply) -> (receipt, err)`` — 보냈으면 ``(배달
        영수증 한 줄, "")``, 못 보냈으면 ``("", 사유)`` (v10.145.2 — 종전엔 한 줄 글이라 루프가 성공·실패를
        가르지 못해 실패도 성공으로 기록했다).

        상대의 받은편지함(또는 main 메일함)으로 비동기 전송하고 즉시 반환한다. 상대의
        회신은 보낸 쪽에게 새 메시지로 온다(보낸 쪽은 막히지 않는다)."""
        me_key = sender.split(":", 1)[1] if sender.startswith("agent:") else ""

        def handler(to: str, text: str, expects_reply: bool = True) -> tuple[str, str]:
            to = (to or "").strip()
            text = (text or "").strip()
            if not to:
                return "", 'message needs a `to` agent key or name (or "main")'
            if not text:
                return "", "empty message — nothing sent"
            if not has_words(text):
                # v10.1.2: 구두점뿐인 메시지(실측 `>`)는 보내지 않는다 — 받은
                # 쪽이 뜻을 찾느라 턴을 쓴다.
                return "", (
                    f"message has no words ({text[:20]!r}) — nothing sent; say it "
                    "in words"
                )
            if to != "main":
                # v10.1.0: 키·`agent:` 접두·이름 전부 받는다 (resolve_key).
                resolved = self.resolve_key(to)
                if resolved is None:
                    return "", (
                        f'unknown agent "{to}" — use a key or name from '
                        '`## Live Agents` (or "main")'
                    )
                to = resolved
            addr = "main" if to == "main" else f"agent:{to}"
            if addr == sender:
                return "", "cannot message yourself"
            tm = self._agents.get(me_key) if me_key else None
            # `message` 는 기본으로 **요청**이다 — 상대에게 회신을 빚지운다
            # (v9.21.0). ``expects_reply=False`` (v10.1.0) 는 **통지**: 상대가
            # 빚지지 않는다 — 상태 공유·인수인계·작별 인사가 여기 간다(실측 cnr60g).
            # 어느 쪽이든 **내 빚은 갚지 않는다**(v10.145.1, 사용자 결정): 갚는 것은
            # `reply` 하나다 — 요청자에게 되물은 `message` 가 원 요청을 조용히 닫아
            # 정작 `reply` 가 거절되던 길(종전 v9.21.0 의 끝말잇기 규칙)을 지웠다.

            # 결과가 그 배달의 id 를 말한다 (v10.77.0) — 회신이 같은 id 를 달고
            # 돌아오므로(`re [r-17]`) 보낸 쪽이 무엇에 대한 회신인지 안다.
            if to == "main":
                rec_id = self.message_to_main(
                    me_key,
                    text,
                    profile=tm.profile_name if tm else "",
                    name=tm.instance_name if tm else "",
                    expects_reply=bool(expects_reply),
                    notice=not expects_reply,
                )
                return delivered_line(rec_id, expects_reply=bool(expects_reply)), ""
            # Capture the send time BEFORE request() enqueues — the target's
            # worker can start (scope_start) the instant it's queued, so a
            # later timestamp on this arrow would render the request AFTER
            # the work.
            send_ts = time.time()
            rec_id = item_ids.next_id(
                item_ids.REPLY if expects_reply else item_ids.MAIL
            )
            out = self._outbound_line(me_key, text, to=to, ts=send_ts)  # 발신자 창 out
            err = self.request(
                to,
                text,
                author=sender,
                expects_reply=bool(expects_reply),
                item_id=rec_id,
                kind="" if expects_reply else "notice",
                lines=out,
            )
            if err:
                return "", err
            line = delivered_line(rec_id, expects_reply=bool(expects_reply))
            # 쌓임은 이 보낸 쪽의 시작 안 한 요청만 센다 — 회신·통지·남의 요청은 재촉이 아니다
            mine = self._post.queued_requests_from(addr, sender) if expects_reply else 0
            stacked = REQUEST_STACKED.format(n=mine) if mine >= 2 else ""
            return line + stacked, ""

        return handler

    def absorb_main(self) -> list[dict]:
        """main 의 턴 경계 — 상자의 줄 선 것 전부를 지금 런의 흡수 항목으로(도착 순서, `intake_item`).
        무엇이든 꺼냈으면 대기열을 다시 그린다."""
        out = [intake_item(e) for e in self._post.absorb("main")]
        if out:
            self._pending_changed()
        return out

    # ── private 메일함 (v10.84.0, docs/wait/DESIGN.md §7) ─────────────────

    def _push_private(self, addr: str, mail: dict) -> None:
        """``run:``·``harness:`` 주소로 온 메일 — 그 루프가 읽는 목록에 넣고 깨운다.
        main 메일함과 달리 디스크·UI·대기열 콜백이 없다(프로세스 수명)."""
        with self._cv:
            self._stamp_mail(mail)
            self._post_deliver(envelope_of_main_mail(mail, addr))

    def take_mail(self, addr: str) -> list[dict]:
        """private 메일함을 비우며 전부 돌려준다 — 루프의 메일함 포트(``mailbox.take``)가 턴
        경계마다 부른다. 도착 순서."""
        envs = self._post.take(addr)
        return [dict(e.payload) for e in envs]

    def wait_mail(
        self, *, arrived, stop_event, still_waiting, wait_input=None, poll_s=0.5
    ) -> str:
        """``arrived()`` 가 0 이 아닐 때까지 막는다 — 제자리 `wait`. 돌려주는 값은 깬 이유: ``"mail"``(도착 —
        `Mailbox._mail_arrived_while_waiting`, 창구의 조건변수 아래서 묻는다), ``"input"``(``wait_input(timeout)`` 이
        참 — 사람 입력, main 만), ``"stopped"``(``stop_event``), ``"nothing"``(``still_waiting()`` 이 거짓 — 기다리던
        것이 메일 없이 사라졌다). 상한은 없다 — 중간 깨우기가 메일로 와서 모델이 판단한다."""
        while True:
            with self._cv:
                if arrived():
                    return "mail"
                self._cv.wait(timeout=poll_s if wait_input is None else poll_s / 2)
                if arrived():
                    return "mail"
            if wait_input is not None and wait_input(poll_s / 2):
                return "input"
            if stop_event is not None and stop_event.is_set():
                return "stopped"
            if not still_waiting():
                # 회신을 갚는 쪽은 빚을 닫은 **뒤** 배달한다 — 그 틈에 "기다릴 것 없음" 으로 보이는
                # 회신이 곧 도착한다. 한 번 더 보고 판정한다.
                with self._cv:
                    if not arrived():
                        self._cv.wait(timeout=poll_s)
                    if arrived():
                        return "mail"
                return "nothing"

    def take_reply(self, rid: str, *, stop_event, poll_s: float = 0.5) -> dict | None:
        """main 메일함에서 ``rid`` 요청에 대한 회신 한 통을 **꺼낸다** — 올 때까지 막는다.

        루프 밖의 `@agent` 명령(`main._dispatch_agent`)이 쓴다: 그 회신은 명령의 답이라
        main 의 다음 턴 관찰로 또 배달되면 안 된다. 정지되거나 요청을 받은 쪽이 회신 없이
        사라지면 None."""
        while True:
            with self._cv:
                env = self._post.take_reply("main", rid)
                if env is not None:
                    mail = dict(env.payload)
                else:
                    mail = None
                    self._cv.wait(timeout=poll_s)
            if mail is not None:
                self._pending_changed()
                return mail
            if stop_event is not None and stop_event.is_set():
                return None
            if not any(r["id"] == rid for r in self.awaited_replies("main")):
                with self._cv:  # 사라지기 직전에 회신이 왔을 수 있다
                    if not any(e.re == rid for e in self._post.queued("main")):
                        return None

    # 기다림 표시 — 창구의 것, `Mailbox` 가 엔진 대신 부른다. 에이전트의 표시는 로스터의 ``waiting`` 칸 — 바뀌면 알린다
    def mark_waiting(self, addr: str) -> None:
        self._post.mark_waiting(addr)
        if addr.startswith("agent:"):
            self._notify_roster()

    def clear_waiting(self, addr: str) -> None:
        self._post.clear_waiting(addr)
        if addr.startswith("agent:"):
            self._notify_roster()

    def is_waiting(self, addr: str) -> bool:
        return self._post.is_waiting(addr)

    def drop_mailbox(self, addr: str) -> int:
        """주소의 루프가 끝났다 — 체크인·기다림 표시를 풀고, private 이면 메일함을 버린다(읽을
        사람이 없다). 버린 메일 수를 돌려준다."""
        self.cancel_checkin(addr)
        with self._cv:
            self._checkin_last.pop(addr, None)
            self._post.clear_waiting(addr)
            if not is_private_owner(addr):
                return (
                    0  # 에이전트 주소의 받은편지함은 그대로 — private 메일함만 버린다
                )
            envs = self._post.take(addr)
            return len(envs)

    # ── status / kill / 종료 ────────────────────

    def format_status(self, key: str = "", *, viewer: str = "") -> str:
        """`agent` `status` 의 글 — ``viewer`` 는 묻는 루프의 주소(대기 넛지를 붙일지)."""
        agents = list(self._agents.values())
        if key:
            tm = self._agents.get(key)
            if tm is None:
                return f'unknown agent "{key}"'
            items = [tm]
        else:
            items = agents
        if not items:
            return 'no live agents. Spawn one with `mode` "spawn".'
        cap = self.max_agents or "∞"
        alive = sum(1 for t in agents if t.state != "dead")
        lines = [f"agents ({alive}/{cap} alive):"]
        for tm in items:
            s = self._snapshot_of(tm)
            role = s["profile"] or "anon"
            if s.get("name"):
                role = f"{role} · {s['name']}"
            if s.get("ends_after_task"):
                role = f"{role} · run"
            line = (
                f"- {s['key']} [{role}] {s['state']}"
                f" | handled {s['handled']}"
                f" | inbox {s['pending_requests']}"
                f" | ctx ~{s['est_tokens']} tokens"
            )
            if s["error"]:
                line += f" | error: {s['error']}"
            if s["state"] == "dead" and not s["ends_after_task"]:
                line += STATUS_DEAD_SUFFIX
            lines.append(line)
        undelivered = len(self._post.mail("main"))
        if undelivered:
            lines.append(f"(undelivered replies: {undelivered})")
        # 폴링 차단 넛지: 묻는 쪽(``viewer``)이 회신을 기다릴 때만 — 그 회신은 메일로 와서 깨우므로
        # `wait` 로 턴을 마치는 게 올바른 대기다(v10.65.0). 기다리는 회신이 없으면 "회신이 깨운다" 가 사실이 아니라
        # 붙이지 않는다(사람의 `@agents` 처럼 묻는 쪽이 없어도).
        if viewer and self.awaited_replies(viewer):
            lines.append(STATUS_WAIT_NUDGE)
        return "\n".join(lines)

    def kill(self, key: str, *, by: str = "user") -> str:
        """✕ 제거 — 성공 시 빈 문자열. 멱등 (이미 dead 여도 성공). run 에이전트도 같다.

        살아 있던 에이전트면 그 빚진 상대 모두에게 회신이 간다(`agent_stop` 의 표 — 지금 일·그 밖의 빚은
        stopped, 시작 안 한 대기 요청은 removed, 띄운 run 은 함께 멈춤). ``by`` 는 누가 지웠는가 —
        ``"user"``(사람: 웹 ✕·``/stop`` 아님), ``"main"``·에이전트 키(모델의 `agent` `kill`). 빈 문자열은
        사람의 정지가 아니다(과제를 못 넣은 run 의 뒷정리 — 알릴 빚이 없다)."""
        tm = self._agents.get(key)
        if tm is None:
            return f'unknown agent "{key}"'
        if by and tm.state != "dead" and not tm.stop_event.is_set():
            tm.stop_cause, tm.stop_by = agent_stop.REMOVED, by
            tm.stop_replies_wake = True
        self._end_agent(tm, join=True)
        # 5.13: kill=창 정리 — 표면(replay 버퍼+라이브 창)만 비우고
        # conversation.jsonl 은 남긴다(mode:"resume" 시 _replay_conversation
        # 소스). shutdown_all(세션 종료)은 정리하지 않음 — 다음 세션 resume
        # 이 restore 재생으로 복원하게 둔다.
        from agent_cli.render import get_renderer

        get_renderer().clear_agent_conversation(key)
        self._save_state()
        self._notify_roster()
        return ""

    def _end_agent(self, tm: AgentInstance, *, join: bool) -> None:
        """에이전트의 평생을 끝낸다 — 되살리지 않음·건 질문 닫기·체크인·감시 정리·평생 정지·워커 깨우기(``join`` 이면
        ≤2s 기다림 — ✕ 는 창을 비우기 전에 기다린다, ■ 는 기다리지 않는다). 빚 알림은 워커의 정리
        (`_answer_after_stop`)가 원인(``stop_cause``)을 보고 한다."""
        tm.revivable = False  # P3: 명시 kill 은 영구 — resume 이 되살리지 않음
        # 건 질문은 지금 닫는다 (v10.149.1) — 워커의 정리(다음 턴 경계)까지 미루면 그 사이 트레이에 남아 답이 갈 곳이 없다
        self._purge_questions_for(tm.key)
        self.cancel_checkin(f"agent:{tm.key}")  # 죽은 에이전트에게 알림이 가지 않게
        # 감시 폐기는 `stop_event.set()` **앞**이다. busy 에이전트는 stop 뒤에도
        # 현재 턴을 마저 돌고(join 은 best-effort), 그 창에 발화하면 항목이
        # 워커가 다시 꺼내지 않아 영영 안 읽힌다. 그 턴에서 새로 건 감시는
        # 워커의 `finally` 가 잡는다 — 죽음이 확정되는 유일한 지점이다.
        self._drop_monitors(tm.key)
        self._stop_worker(tm)
        if (
            join
            and tm.worker is not None
            and tm.worker is not threading.current_thread()
        ):
            tm.worker.join(timeout=2.0)  # busy 면 다음 턴 경계에서 멈춤

    # ── 사람의 정지 (v10.142.0, docs/agent-engine/DESIGN.md §2.3 — 규칙은 `agent_stop`) ──

    def stop_run(
        self, key: str, *, by: str = "user", wakes_requesters: bool = True
    ) -> str:
        """■ 런 정지 — 지금 런만 끝내고 상주 에이전트는 남는다(대기 요청은 다음 차례). run 에이전트는
        맡은 일 하나가 수명이라 내려온다(채널은 회색으로 남는다 — ✕ 와 달리 창을 비우지 않는다).
        런은 도구 실행 직전 검사(v10.140.0)에서 멈추고, 런의 정리가 빚진 상대에게 회신한다.
        ``wakes_requesters`` 가 거짓이면("Stop everything") 정지 회신이 받는 쪽(main·에이전트)을 깨우지 않는다.
        성공 시 빈 문자열, 아니면 사유."""
        tm = self._agents.get(key)
        if tm is None:
            return f'unknown agent "{key}"'
        if tm.state == "dead" or tm.stop_event.is_set():
            return f'agent "{key}" has already ended'
        tm.stop_replies_wake = wakes_requesters
        if tm.ends_after_task:
            tm.stop_cause, tm.stop_by = agent_stop.STOPPED, by
            self._end_agent(tm, join=False)
            self._save_state()
            self._notify_roster()
            return ""
        if tm.state != "busy":
            return f'agent "{key}" is not working on a request'
        # 원인을 먼저 — 꺼낸 뒤·시작 전이면 시작(`ResidentLoop.start`)이 원인을 보고 새 런을 멈춘다
        tm.stop_cause, tm.stop_by = agent_stop.STOPPED, by
        run = tm.run_stop
        if run is not None:
            run.set()
        return ""

    def working_agents(self) -> list[str]:
        """일하는 에이전트의 키 — 런 중인 것(제자리에서 기다리는 것 포함)과 일을 끝내지 않은 run 에이전트.
        "모두 정지"·웹 ``■ all``·Ctrl+C 가 본다. 읽기만 한다."""
        return [
            tm.key
            for tm in list(self._agents.values())
            if not tm.stop_event.is_set()
            and tm.state != "dead"
            and (tm.state == "busy" or tm.ends_after_task)
        ]

    def stop_all_runs(
        self, *, by: str = "user", wakes_requesters: bool = True
    ) -> list[str]:
        """모든 에이전트의 지금 런을 멈춘다(`working_agents` 마다 ■) — 멈춘 키 목록. main 은 건드리지
        않는다(웹 "Stop all agents"·``/stop all``·Ctrl+C 두 번의 에이전트 몫). "Stop everything" 은
        ``wakes_requesters=False`` — 그 회신들이 main 도 살아 남은 에이전트도 다시 깨우지 않는다(사용자 결정)."""
        return [
            k
            for k in self.working_agents()
            if not self.stop_run(k, by=by, wakes_requesters=wakes_requesters)
        ]

    def answer_debts_as_stopped(
        self, tm: AgentInstance, *, activity: list[str], notes: str
    ) -> None:
        """멈춘 에이전트의 갚지 않은 빚(지금 일·그 밖의 빚 — 사람 주소 제외) 모두에 stopped 회신을 보내고
        빚을 갚음으로 적는다(요청자의 기다리는 회신이 닫힌다). 보낸 상대는 ``tm.stop_notified`` 에 쌓인다.
        ``activity``·``notes`` 는 그 런의 마지막 호출·생각(`agent_stop.run_activity`·`partial_notes`)."""
        by = tm.stop_by or "user"
        self._answer_debts(
            tm,
            agent_stop.stop_mark(agent_stop.STOPPED, by),
            partial(agent_stop.stopped_reply, activity=activity, notes=notes, by=by),
        )

    def answer_main_debts_as_stopped(self, records: list[dict]) -> int:
        return agent_stop.answer_main_debts(self, records)

    def _answer_debts(
        self, tm: AgentInstance, mark: str, reply_of: Callable[[str, str, str], str]
    ) -> None:
        """갚지 않은 빚(사람 주소 제외)마다 회신(본문 ``reply_of(key, rid, text)``, 머리 표지 ``mark``)을 보내고 그
        배달로 빚을 갚는다 — 정지(■·✕)의 stopped 와 죽은 워커의 died 가 같은 길."""
        for d in self.reply_debts(f"agent:{tm.key}"):
            text = reply_of(tm.key, d.id, d.text)
            self._send_stop_reply(  # 회신의 배달이 곧 갚음
                tm, d.to, d.id, text, mark, hop=d.hop, answers=d.answers, settles=True
            )

    def _answer_queued(
        self, tm: AgentInstance, mark: str, reply_of: Callable[[str, str, str], str]
    ) -> None:
        """시작 안 한 대기 요청(받은편지함 미러의 요청 항목)마다 회신(본문 ``reply_of(key, rid, text)``, 머리
        표지 ``mark``)을 보내고 미러를 비운다 — 사람·모델의 정지(✕ removed · run 의 ■ stopped)와 스스로 내려간 에이전트의
        ended·died 가 같은 길."""
        with self._cv:
            queued = self._queued(tm)
            self._post.remove_queued(
                f"agent:{tm.key}"
            )  # 줄은 버림(dropped), 회신은 아래서 배달
        for it in queued:
            author = str(it.get("author") or "")
            if it.get("kind") or not it.get("expects_reply", True):
                continue  # 회신·통지 — 빚이 아니다
            if is_human_addr(author):
                continue  # 사람의 창 입력 — 창이 곧 배달이다
            text = reply_of(tm.key, str(it.get("id") or ""), str(it.get("text") or ""))
            self._send_stop_reply(
                tm,
                author,
                str(it.get("id") or ""),
                text,
                mark,
                hop=int(it.get("hop", 0) or 0),
            )

    def _send_stop_reply(
        self,
        tm: AgentInstance,
        to: str,
        rid: str,
        text: str,
        mark: str,
        *,
        hop: int = 0,
        answers: list | None = None,
        settles: bool = False,
    ) -> None:
        """정지 회신 한 통 — 요청자가 main 이면 메일함, 에이전트면 그 받은편지함(회신 — 빚 아님). ``settles`` 면 그
        배달이 빚 ``rid`` 를 갚는다(정지 회신 — 한 락 구간). 시작 전 요청의 회신은 줄이 이미 닫혀 갚지 않는다."""
        if to == "main":
            self._push_reply(
                {
                    "kind": "peer_message",
                    "key": tm.key,
                    "profile": tm.profile_name,
                    "name": tm.instance_name,
                    # 배달된 회신이다(실패가 아니다) — 멈춤은 머리 표지가 말한다
                    "success": True,
                    "output": text,
                    "answers": list(answers or []),
                    "expects_reply": False,
                    "re": rid,
                    "mark": mark,
                    # "Stop everything" 의 회신은 main 을 깨우지 않는다 — 다음 사람 입력 런에 함께 배달된다
                    **({} if tm.stop_replies_wake else {"wakes_main": False}),
                },
                settles=rid if settles else "",
                lines=self._outbound_line(tm.key, text, to="main"),
            )
        elif to.startswith("agent:"):
            # "Stop everything" 의 회신은 받는 상주의 런을 열지 않는다 — 다음에 일을 받는 런이 함께 읽는다
            self._deliver_peer_reply(
                to.split(":", 1)[1],
                tm.key,
                text,
                hop,
                re=rid,
                mark=mark,
                opens_run=tm.stop_replies_wake,
                settles=rid if settles else "",
            )
        tm.stop_notified.append((to.removeprefix("agent:"), rid))

    def _answer_after_stop(self, tm: AgentInstance) -> None:
        """사람(또는 모델)이 끝낸 에이전트의 남은 것을 표(`agent_stop.stop_rule`)대로 정리한다 — 워커가 죽을 때 한 번.
        남은 빚·대기 요청과 화면 한 줄. 띄운 run 은 빚을 닫은 뒤에 멈춘다(`agent_stop.close_debts_then_stop_sub_runs`)."""
        rule = agent_stop.stop_rule(tm.stop_cause, tm.ends_after_task)
        self.answer_debts_as_stopped(tm, activity=[], notes="")
        kept = 0
        if rule.answers_queued:
            by, cause = tm.stop_by or "user", tm.stop_cause
            reply_of = partial(agent_stop.unstarted_reply, by=by, cause=cause)
            self._answer_queued(tm, agent_stop.stop_mark(cause, by), reply_of)
        else:
            kept = len(self._queued(tm))
        self.report_stop(tm, kept=kept)

    def report_stop(self, tm: AgentInstance, *, kept: int) -> None:
        """정지 한 번의 화면 한 줄(CLI ``■ agt-3 │ stopped by user — reply sent to main (r-21)``) — 보낸 상대를
        비운다."""
        from agent_cli.render import get_renderer

        how = agent_stop.stop_mark(tm.stop_cause, tm.stop_by or "user")
        notified, tm.stop_notified = tm.stop_notified, []
        try:
            get_renderer().agent_stopped(
                key=tm.key, how=how, notified=notified, kept=kept
            )
        except Exception:
            pass  # 화면은 best-effort — 정리를 막지 않는다

    def shutdown_all(self, *, deadline: float | None = None) -> None:
        """세션 종료 — main 상자를 먼저 닫고(멈추는 런이 그 사실을 읽는다, 5e1) 전원 kill. main 부트스트랩의 finally 에서
        호출. ``deadline``(SIGTERM 경로, v10.134.1)이 있으면 워커를 그 안에서만 기다린다. 마지막 저장은 revivable 그대로(D7)."""
        self._post.close("main")
        with self._cv:
            armed = list(self._checkin_armed)
        for owner in armed:  # main 포함 — 끝나는 세션에 알림이 쌓이지 않게
            self.cancel_checkin(owner)
        for tm in list(self._agents.values()):
            self._drop_monitors(tm.key)
            self._stop_worker(tm)
        for tm in list(self._agents.values()):
            if tm.worker is not None:
                tm.worker.join(timeout=wait_left(deadline, 5.0))
        self._save_state()

    def resume_teammate(self, key: str, *, parent_ctx=None) -> str:
        """죽은 teammate 를 **이전 컨텍스트 그대로** 되살린다 (mode:"resume").

        kill/비정상 사망으로 dead 가 된 teammate 의 history 는 디스크에
        온전히 남아 있다 — 같은 key 로 fresh worker 를 세우고 ctx 를
        resume 모드(자기 history 이어받기, P3 기계 재사용)로 재기동한다.
        성공 시 빈 문자열, 실패 시 에러 메시지. 부활 후에는 세션 resume
        의 자동 재생성 대상으로도 복귀한다 (revivable=True).
        """
        tm = self._agents.get(key)
        if tm is None:
            return f'unknown agent "{key}"'
        if tm.state != "dead":
            return (
                f'agent "{key}" is still alive ({tm.state}) — send it a request instead'
            )
        if self._at_agent_limit():
            return (
                f"agent limit reached ({self.max_agents} alive). "
                f'Kill one first (`mode` "kill").'
            )

        fresh = AgentInstance(
            key,
            profile_name=tm.profile_name,
            role_prompt=tm.role_prompt,
            allowed_tools=tm.allowed_tools,
            model=tm.model,
            hooks_config=tm.hooks_config,
            context_mode=tm.context_mode,
            home_dir=tm.home_dir,
            instance_name=tm.instance_name,
            description=tm.description,
            instructions=tm.instructions,
            ends_after_task=tm.ends_after_task,
            caller_runtime=tm.caller_runtime,
            agent_stack=tm.agent_stack,
            parent=tm.parent,
        )
        fresh.handled = tm.handled
        fresh.created_at = tm.created_at
        fresh.revive = True  # worker 가 ctx 를 resume 모드로 (이력 이어받기)
        self._agents[key] = fresh
        self._post.reopen(f"agent:{key}")  # 끝낼 때 닫은 상자 — 새 워커가 기다린다
        fresh.worker = threading.Thread(
            target=self._worker,
            args=(fresh, parent_ctx),
            daemon=True,
            name=f"agent-{key}",
        )
        fresh.worker.start()
        # 5.13: 부활 즉시 🤝 대화창 복원 (kill=정리와 대칭). 새 요청 처리
        # 전에 재생해 순서가 과거→현재로 자연스럽게 이어진다.
        self._replay_conversation(fresh)
        self._save_state()
        self._notify_roster()
        return ""

    def auto_spawn(self, parent_ctx=None) -> int:
        """frontmatter ``auto-spawn: true`` 역할을 세션 시작 시 자동 상주
        (전문가 팀 확장). restore() **이후에** 호출 — 같은 역할의 살아있는
        teammate(재생성분)가 이미 있으면 중복 스폰하지 않는다. 스폰 수 반환."""
        from agent_cli.subagent.profiles import available_profiles

        live_roles = {
            tm.profile_name
            for tm in list(self._agents.values())
            if tm.state != "dead" and not tm.ends_after_task
        }
        spawned = 0
        for name, meta in available_profiles(include_meta=True):
            if not meta.get("auto-spawn"):
                continue
            if name in live_roles:
                continue
            _key, error = self.spawn(profile=name, parent_ctx=parent_ctx)
            if not error:
                spawned += 1
        return spawned

    # ── P3: 상태 영속 + resume 재생성 (D7) ──────

    def _state_path(self) -> Path | None:
        return self.session_dir / "agents.json" if self.session_dir else None

    def _save_state(self) -> None:
        """agents.json 원자 저장 (fsio) — 명단만(5c-0). 갱신 시점: 명단이 바뀔 때(spawn·kill·런 끝의 ``handled``·
        워커 종료·되살림). 상태 파일은 best-effort — 디스크 문제로 세션 진행을 막지 않는다."""
        path = self._state_path()
        if path is None:
            return
        # 읽지 않은 파일은 덮어쓰지 않는다 (v10.131.1). 복원 전의 명단은 비어 있어서, 그때 쓰면 resume 세션의 상주
        # 에이전트가 지워진다 — 조립 도중 죽어 `shutdown_all` 만 불린 길. 뜨기와 쓰기를 한 락 아래에서 (v10.133.2) —
        # 따로 두면 워커가 뜬 옛 사본이 새 저장 **뒤에** 써진다.
        with self._save_lock:
            if not self._state_owned and path.is_file():
                return
            self._write_state(path)

    def _write_state(self, path: Path) -> None:
        """명단을 떠서 쓴다 — ``_save_lock`` 을 쥔 채로만 부른다."""
        # Snapshot under the GIL — ``_save_state`` runs on worker threads while
        # the main thread mutates ``_agents``; iterating the live view raced a
        # spawn and killed an UNRELATED agent via the worker's except-handler
        # (AUDIT S-1). ``list(...)`` can't raise mid-iteration.
        doc = agents_state.roster_document(list(self._agents.values()))
        try:
            from agent_cli.fsio import atomic_write_json

            atomic_write_json(path, doc)
            self._state_owned = True
        except OSError:
            pass

    def restore(self, parent_ctx=None) -> int:
        """세션 resume 시 agents.json 의 명단으로 재생성 — 되살린 수 반환 (5c-0: 재시작은 컨텍스트와 명단만 올린다).

        - 살아 있던 상주: 자기 history 를 resume 한 ctx 로 워커 재기동(이전 문답 전부 기억), idle 로 선다. 첫 ctx 에
          재시작 기록 한 줄(`restarted`). 아무것도 스스로 이어 하지 않는다 — 다음 요청이 런을 연다.
        - kill 된 것과 과제 도중이던 run 에이전트(``ends_after_task`` — 살아 있는 run 은 늘 과제 도중이다)는 dead
          툼스톤(status 가시성).
        - 받은편지함·하던 일·main 앞 메일·빚·열린 질문은 버린다 — 저장하지 않는다(옛 파일의 그 칸은 읽지 않는다).
        - 파일 부재/파손/버전 불일치는 조용히 no-op (fresh 세션과 동일).
        """
        self._state_owned = True  # 읽으려 했다 — 이제부터 이 파일은 이 레지스트리의 것
        path = self._state_path()
        data = agents_state.read_state(path) if path is not None else None
        if data is None:
            return 0
        revived = 0
        for e in data.get("agents", []):
            key = e.get("key")
            if not key or key in self._agents or self.session_dir is None:
                continue
            tm = AgentInstance(
                key,
                **agents_state.instance_args(
                    e, session_dir=self.session_dir, runtime=self.runtime
                ),
            )
            tm.task_id = str(e.get("task_id") or "")
            tm.handled = int(e.get("handled", 0) or 0)
            if isinstance(e.get("created_at"), (int, float)):
                tm.created_at = e["created_at"]
            self._agents[key] = tm
            if e.get("state") == "dead" or tm.ends_after_task:
                tm.state = "dead"
                tm.error = e.get("error", "")
                tm.revivable = False
                continue
            tm.revive = True
            tm.restarted = True
            conversation_log.note_restart(tm)  # 창의 재시작 줄 — 워커보다 먼저
            tm.worker = threading.Thread(
                target=self._worker,
                args=(tm, parent_ctx),
                daemon=True,
                name=f"agent-{key}",
            )
            tm.worker.start()
            # 5.13: 세션 resume(b) 근본 경로 — 새 프로세스라 replay 버퍼가 비어 🤝 대화창이 텅 비던 증상.
            # conversation.jsonl 을 재생해 복원한다(restore 는 SSE 접속 전이라 buffer 에 쌓였다가 첫 접속 snapshot
            # 으로 배달). dead(kill) 툼스톤은 재생 안 함 — kill=정리 일관(필요하면 mode:"resume" 이 그때 복원).
            self._replay_conversation(tm)
            revived += 1
        self._save_state()
        self._notify_roster()
        return revived

    # ── worker ──────────────────────────────────

    def _persist_reply(self, tm: AgentInstance, run_item: str, body: str) -> str:
        """회신 전문을 항상 디스크에 (over-cap 포인터 + P3 보존 토대). 파일
        이름은 그 런을 연 배달의 id — 전역에서 유일해 덮어쓸 일이 없다."""
        try:
            replies_dir = tm.home_dir / "replies"
            replies_dir.mkdir(parents=True, exist_ok=True)
            path = replies_dir / f"reply-{run_item}.md"
            path.write_text(body, encoding="utf-8")
            return str(path)
        except OSError:
            return ""

    def _lines_in_section(self, lines) -> None:
        """배달 구간(``_cv``) 안의 몫 — 줄을 파일에 적고, 막히지 않는 렌더러면 창에도 낸다. 규칙(`conversation_log`):
        기록이 원본이고 화면은 그 순서를 따른다 — 막히지 않는 표면만 구간 안에서. 받는 쪽은 ``_cv`` 없이 못 꺼내니
        그 배달로 생기는 줄(런의 out·답의 in)이 앞서지 않는다."""
        conversation_log.append(lines)
        if conversation_log.never_blocks():
            conversation_log.show(p for _, p in lines)

    def _lines_after_section(self, lines) -> None:
        """구간 뒤의 몫 — 막힐 수 있는 렌더러(터미널)면 창에 낸다(막힌 stdout 이 ``_cv`` 를 잡지 않게)."""
        if not conversation_log.never_blocks():
            conversation_log.show(p for _, p in lines)

    def _record(self, tm: AgentInstance, payload: dict) -> None:
        """배달이 아닌 대화 줄 하나(흡수·런의 out) — 배달의 줄과 같은 구간 규칙으로."""
        with self._cv:
            self._lines_in_section([(tm, payload)])
        self._lines_after_section([(tm, payload)])

    def _replay_conversation(self, tm: AgentInstance) -> None:
        """``conversation.jsonl`` 로 🤝 창을 다시 채운다 (5.13) — kill 이 표면을 정리하고 resume 이 여기서 채운다. 먼저
        비운다(멱등). 라이브와 같은 표면이라 웹은 재접속 버퍼에 다시 쌓이고 저장된 ``ts`` 가 원래 시각을 지킨다. 읽기와
        내기도 배달과 같은 구간 규칙(v10.146.5) — 부활 도중 든 배달이 재생과 섞이거나 두 번 보이지 않는다."""
        from agent_cli.render import get_renderer

        def refill(recs):
            get_renderer().clear_agent_conversation(tm.key)
            conversation_log.show(recs)

        with self._cv:
            recs = conversation_log.read(tm.home_dir)
            if conversation_log.never_blocks():
                refill(recs)
        if not conversation_log.never_blocks():
            refill(recs)

    def _worker(self, tm: AgentInstance, parent_ctx) -> None:
        """teammate 의 전 생애: 스코프 열기 → ctx 생성 → inbox 루프 → 정리.

        상태 전이는 전부 이 함수 안이다.
        """
        from agent_cli.actor import run_actor
        from agent_cli.render import get_renderer
        from agent_cli.subagent.resident_loop import AgentActor, ResidentLoop

        renderer = get_renderer()
        _disp = (
            " · ".join(p for p in (tm.profile_name, tm.instance_name) if p) or "anon"
        )
        renderer.begin_prompt_scope(tm.key, label=f"agent:{_disp}")
        crash = ""  # 비정상 종료 사유 (의도된 종료 = stop_event set 은 제외)
        try:
            # ctx 는 worker 스레드에서 생성 — create_subagent_ctx 의
            # note_scope_ctx 가 "현재 스레드의 스코프"(방금 연 tm.key)에
            # 등록되게 하기 위함 (spawn 스레드면 main 스코프를 오염).
            # P3 revive: 자기 history 를 그대로 이어받는 resume 모드 —
            # teammate 는 이전 세션의 문답을 전부 기억한 채 살아난다.
            # 종류 객체 (v10.87.0): 주소·포트·바인딩(프로파일 고정)·ctx 를 한 번 세운다.
            kind = ResidentLoop(tm, self, disp=_disp)
            try:
                kind.open(parent_ctx)
            except LoopKindError as exc:
                tm.error = str(exc)
                crash = f"context creation failed: {tm.error}"
                return
            tm.state = "idle"
            # starting→idle 전환도 roster 에 반영 — 초기 task 없는
            # spawn/재생성분이 UI 에 "starting" 으로 영구 표시되던 버그
            # (busy/dead 전환만 알리고 최초 idle 을 빠뜨렸었음, v4.62.1).
            self._notify_roster()

            run_actor(AgentActor(self, tm, kind))
        except BaseException as e:
            tm.error = f"{type(e).__name__}: {e}"
            crash = tm.error
        finally:
            tm.state = "dead"
            if crash and not tm.stop_event.is_set():
                # Q4: kill/세션종료(의도된 종료)가 아닌 사망 → main 에 관찰
                # 통지. 한가한 main 은 펌프가 그 메일로 런을 연다.
                tm.revivable = False  # 원인 미상 사망을 resume 이 되살리지 않게
                self._push_reply(
                    {
                        "kind": "died",
                        "key": tm.key,
                        "profile": tm.profile_name,
                        "name": tm.instance_name,
                        "success": False,
                        "output": crash,
                    }
                )
            if tm.stop_cause:
                # 사람·모델이 끝냈다(✕·run 의 ■) — 빚진 상대 모두에게 알리고 띄운 run 을 멈춘다
                # (`agent_stop` 의 표). 런 도중이었으면 그 런의 정리가 지금 일의 빚을 이미 갚았다.
                self._answer_after_stop(tm)
            elif not tm.stop_event.is_set():
                if crash:
                    # 죽었다 — 집어 든 요청마다 알리고, 못 간 빚(요청자도 죽었다)은 멈춘 런처럼 버린다
                    self._answer_debts(
                        tm, agent_stop.DIED_MARK, agent_stop.died_working_reply
                    )
                    self.run_ended(f"agent:{tm.key}", crash, stopped=True)
                # 스스로 내려갔다(run 의 과제 끝·워커 죽음) — 더는 상자를 읽지 않으니 줄 선 요청에 알린다
                mark = agent_stop.DIED_MARK if crash else agent_stop.ENDED_MARK
                self._answer_queued(
                    tm, mark, partial(agent_stop.gone_reply, died=bool(crash))
                )
            if not tm.revivable:
                died = bool(crash) and not tm.stop_event.is_set()
                agent_stop.close_debts_then_stop_sub_runs(self, tm, died=died)
                # **영구 사망(kill·crash)일 때만** 질문을 정리한다 (§3.8).
                # 세션 종료(shutdown_all)는 revivable 을 유지하므로 여기
                # 들어오지 않는다 — 들어오면 직후의 _save_state 가 빈 목록을
                # 저장해 resume 이 알릴 열린 질문이 항상 0건이 된다.
                self._purge_questions_for(tm.key)
            # 감시 폐기의 **정본 위치** — kill·크래시·세션 종료가 모두
            # 여기로 수렴하고, 여기가 죽음이 확정되는 곳이다. kill/
            # shutdown_all 의 조기 드롭은 그 앞 창을 좁힐 뿐이다.
            release_owner(
                f"agent:{tm.key}", jobs=self.jobs, monitors=self.monitors, registry=self
            )
            renderer.end_prompt_scope(tm.key)  # 스코프 고정 (사후 검사 가능)
            self._save_state()  # ctx 실패(error→dead)·종료 상태 반영
            self._notify_roster()

    def _next_run_item(self, tm: AgentInstance) -> dict | None:
        """다음 런을 열 항목 — 창구의 `next_run`(첫 **깨우는** 봉투 하나 — 나머지는 줄에 남아 그 런의
        첫 흡수가 도착 순서로 읽는다). 올 때까지 시한 없이 기다린다: 배달이 깨우고, 끝내기(`_stop_worker`)가 상자를
        닫아 깨운다. 닫혔으면 None.

        집는 순간 ``busy`` 다(P0-9b) — 그 사이 세션 판정의 `has_agent_work`(상태·쌓인 일)가 "활동 없음" 으로 보지
        않게 같은 락 구간에서(창구 조건변수는 이 레지스트리의 것 — 기다리는 동안 모든 깊이가 풀린다)."""
        with self._cv:
            env = self._post.next_run(f"agent:{tm.key}", timeout=None)
            if env is CLOSED or env is None:
                return None
            tm.state = "busy"
            return dict(env.payload)

    def _stop_worker(self, tm: AgentInstance) -> None:
        """평생 정지 — ``stop_event`` 를 세우고(엔진·흡수가 본다) 그 상자를 닫는다(기다리는 워커가 깨어 나온다; 줄 선
        것이 있어도 런을 열지 않는다). 순서: 정지 먼저 — 깬 워커가 런 도중이면 다음 흡수가 정지를 본다."""
        tm.stop_event.set()
        self._post.close(f"agent:{tm.key}")

    def _with_human_notice(self, tm: AgentInstance, run_item: str, output: str) -> str:
        """이 런이 **사람에게** 물어 둔 미답 질문을 결과에 실어 알린다.

        사람에게는 강제를 못 건다 — 우리 루프가 아니다. 대신 결과에 실어
        "내가 답을 안 해서 끝났구나" 를 알린다. 이 문구는 **하네스가** 붙인다:
        dispatch 에서 붙이면 ``serialize_terminal_for_history`` 를 타고 모델
        자신이 쓴 최종답으로 ctx 에 남아, 다음 런에서 모델이 하네스 문구를
        모방한다.
        """
        human_open = [
            q for q in self.questions_asked_in(tm.key, run_item) if q.to_human
        ]
        if not human_open:
            return output
        lines = "\n".join(f'   [{q.id}] "{q.text}"' for q in human_open)
        return (
            f"{output}\n\n⏳ {len(human_open)} question(s) still unanswered — "
            f"answering lets this continue:\n{lines}"
        )


# ── 레지스트리가 짓는 도구 결과·거절 문구 (v10.85.0, agent 검토 PR B) ──────────────────────
# 모듈 상수인 이유: ``prompts.inventory`` 가 표본으로 읽어 표기 가드(도구·인자 백틱,
# 값 큰따옴표, 호출 모양 없음, 대문자 강조 없음)가 여기까지 본다. `agent` 모드의 결과 문구는
# `agent_modes` 에 있다 — 여기는 레지스트리가 직접 짓는 것(`message` 결과·status·중복 거절)이다.
DELIVERED_NOTICE = 'delivered as notice "{rid}" — nothing is owed back'
DELIVERED_REQUEST = 'delivered as request "{rid}" — its reply will come marked [{rid}]'


def delivered_line(rid: str, *, expects_reply: bool) -> str:
    """`message` 결과 — 통지는 갚을 것 없음, 요청은 회신 표지."""
    return (DELIVERED_REQUEST if expects_reply else DELIVERED_NOTICE).format(rid=rid)


REQUEST_STACKED = (
    "\n⚠ {n} of your requests to it are still queued — if these are progress "
    "checks or re-sends of the same ask, that is interference: each one queues "
    "more work and delays the answer. Its `reply` reaches you on its own; stop "
    "re-sending."
)
STATUS_DEAD_SUFFIX = ' | resumable with `mode` "resume"'
STATUS_WAIT_NUDGE = (
    "⏳ Replies arrive as agent mail and wake you — finish this turn with `wait` "
    "instead of polling `status`."
)
DUPLICATE_DEAD = (
    '"{label}" already exists (dead) as "{key}" — bring it back with `mode` '
    '"resume" instead of spawning a duplicate, or spawn with a distinct `name`.'
)
DUPLICATE_ALIVE = (
    '"{label}" is already running as "{key}" — send it work with `message`, or '
    "spawn with a distinct `name` if you really need a second instance."
)
