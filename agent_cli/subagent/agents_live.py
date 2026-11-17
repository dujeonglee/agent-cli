"""teammate — 상주 세션 에이전트 (P1 코어, docs/teammate/DESIGN.md).

delegate 가 "파견"(스폰→완주→ctx 폐기)이라면 teammate 는 "상주 팀원"이다:
spawn 이 key 를 반환하고, 이후 request 가 같은 ctx 위에서 반복 처리된다
(D1 비동기 mailbox). 회신은 LLM 폴링이 아니라 **harness 가 배달**한다 —
main 루프가 턴 경계에서 :meth:`AgentRegistry.drain_replies` 를 비워
관찰 레코드로 주입한다 (D2, ``AgentLoop._deliver_agent_mail``).

스레딩 모델
-----------
teammate 하나 = 데몬 worker 스레드 하나. worker 는 자기 inbox 를 블록해
메시지당 `ResidentLoop.run` (엔진 `run_loop`) 1회를
돌리고 회신을 registry 의 공용 pending 리스트에 push 한다. 상태 전이
(idle→busy→idle→…→dead)는 worker 루프 한 곳에만 있다.

- **인터럽트 분리**: worker 는 자기 ``stop_event`` 만 본다 — main 의
  Ctrl+C / /api/stop 은 teammate 를 죽이지 않는다(백그라운드 팀원).
  종료는 명시 ``kill`` 또는 세션 종료(:meth:`shutdown_all`)뿐.
- **인스펙터 (D9)**: worker 시작 시 ``begin_prompt_scope(key)`` 로 상시
  스코프를 열고(ctx 도 등록), **종료 시에만** ``end_prompt_scope`` —
  delegate 와 달리 요청 사이에도 칩이 살아 있고 동적 컨텍스트가 자란다.
  요청별 SSE 라우팅은 별도 표면 ``begin/end_agent_work`` (renderer) —
  스코프(상시)와 카드(요청별)를 분리한 이유는 web 렌더러의
  ``begin_delegate_task`` 가 스코프 push 와 결합돼 있어서다.
- **레코드 계약**: 배달 레코드는 ``tool:"agent"`` + additive
  ``source:"agent_reply"``. ``tool:""`` 는 v4.51.0 형식-개입 레거시
  마커라 금지 (``records.is_format_intervention`` 오인 방지 — 테스트 고정).

P1 경계: teammate 안 teammate 금지(레지스트리 미전파로 도구가 서브루프
에서 자동 strip), manifest/resume 재생성은 P3, WebUI 대화 창은 P4.

모듈 나눔
---------
레지스트리는 명단·배달·저장·스레드의 틀이다. 규칙과 모양은 레지스트리를 import 하지 않는 잎에 있고
레지스트리가 부른다: 메일의 글(`agent_mail` — 봉투·관찰 레코드), 회신 갚기의 규칙(`question_port`),
`agents.json` 직렬화(`agents_state`), main 깨우기(`mail_waker`), 사람의 정지(`agent_stop`), `agent` 도구의
모드 처리(`agent_modes`, run 은 `run_agent`).
"""

from __future__ import annotations

import json
import re
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from agent_cli import item_ids
from agent_cli.constants import (
    CHECKIN_STEPS_S,
    harness_mail_tool,
    has_words,
    is_private_owner,
)
from agent_cli.loop.kind import LoopKindError
from agent_cli.post.office import PostOffice
from agent_cli.post.request_book import RowError
from agent_cli.session.store import AGENTS_DIR
from agent_cli.session_slots import get_job_registry
from agent_cli.subagent import agent_stop, agents_state
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
from agent_cli.subagent.delegation_checks import depth_or_recursion_refusal
from agent_cli.subagent.question_port import REPLY_REQUESTER_GONE, QuestionPort
from agent_cli.terminate import wait_left
from agent_cli.tools.result import ToolResult
from agent_cli.wake import (
    harness_record,
    release_owner,
    wait_checkin,
    wake_source_ids,
    wake_sources,
)

if TYPE_CHECKING:
    from agent_cli.context.manager import ContextManager

# worker 를 inbox 블록에서 깨워 종료시키는 sentinel (identity 비교).


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

    @classmethod
    def from_dict(cls, d: dict) -> Question | None:
        """``agents.json`` 의 한 항목 → Question. 모양이 깨졌으면 None."""
        try:
            return cls(
                id=str(d["id"]),
                asker=str(d["asker"]),
                target=str(d["target"]),
                text=str(d["text"]),
                asked_at=float(d.get("asked_at") or time.time()),
                # 물어본 런은 사라졌다 — "" 는 상주 런 이름과 안 겹친다.
                asked_in="",
            )
        except (KeyError, TypeError, ValueError):
            return None

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "asker": self.asker,
            "target": self.target,
            "text": self.text,
            "asked_at": self.asked_at,
            "asked_in": self.asked_in,
        }


@dataclass
class ReplyDebt:
    """회신 빚 하나의 공개 모양 — 우편 장부의 빚 줄(`_debt_view`)을 이 모양으로 보여 준다(v10.145.0 — 장부는
    `agent_cli.post`).

    런이 받은 **요청**(``expects_reply=True`` 항목·main 에 배달된 `message`)
    마다 하나(v9.25.0 — 종전엔 상대별 하나라 같은 상대의 새 요청이 앞 것을
    덮었다; 런 도중 흡수된 요청이 여럿이면 각각 회신을 받아야 한다는 사용자
    결정). ``id`` 가 그 요청을 가리킨다 — 그 요청 배달의 id(`item_ids`,
    전역에서 유일). 갚으면(`reply(id)`·요청자에게 `message`)
    ``settled`` — 지우지 않는다: "이미 갚았다" 는 거부 사유가 사실이어야
    한다. 사람 주소(``user:*``)의 빚은 창이 곧 배달이라 갚을 것이 없지만
    **적어는 둔다** — 그 런의 `reply` 를 거부할 때 사유가 "요청이 없었다" 가
    아니라 "요청자가 창을 보는 사람" 이어야 하기 때문이다.
    """

    to: str  # "main" | "agent:<key>" | "user" | "user:<nick>"
    text: str  # 무엇에 대한 회신인지 (빚 목록의 설명)
    answers: list = field(default_factory=list)  # 요청 항목의 귀속 스냅샷
    hop: int = 0  # peer 폴백 배달의 hop
    settled: bool = False
    id: str = ""  # 요청 번호 — `reply(id=…)` 가 가리키는 값
    #: 닫힌 줄이면 닫힌 사유(장부의 ``Row.how`` — "reply"·"unsent"·…), 열린 줄이면 "". "unsent" 는 회신이
    #: 요청자에게 가지 못한 채 닫힌 것이다 — 다시 `reply` 할 때 "이미 회신했다" 가 아니라 그 사실을 말한다.
    closed_by: str = ""

    @property
    def human(self) -> bool:
        return is_human_addr(self.to)


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
        #: 런이 `complete` 로 끝나면 내려온다(`wait` 로 끝난 런은 아니다 — `_run_ended_by_wait`).
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
        # (v10.145.0). 재시작이 되살릴 것은 저장 파일의 ``inbox``·``in_flight`` 로 간다(모양 그대로).
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
        # P4: 지금 처리 중인 request 의 화자 — "main" 이 아니면(웹 인간 개입)
        # 그 회신/질문을 main mailbox 에 넣지 않는다 (D8: 컨텍스트 비오염).
        self.current_author = "main"
        self.created_at = time.time()
        self.handled = 0  # 처리 완료한 request 수 (화면용 숫자)
        # 지금 도는 런의 이름 = 그 런을 연 배달의 id — 질문의 런 스코프
        # (Question.asked_in)가 이 값을 찍는다. 유휴는 "".
        self.current_item = ""
        # 이 런의 회신 빚은 여기 없다 — 레지스트리의 주소별 장부
        # (`AgentRegistry._post` — 약속 장부, v10.145.0) 가 main 과 같은 자리에서 든다.

    def snapshot(self, *, pending_requests: int) -> dict:
        """status 표시용 스냅샷 (락 없는 근사값 — 표시 용도). ``pending_requests`` 는 레지스트리가 센 앞에 쌓인
        할 일(`AgentRegistry.backlog_of` — 받은편지함은 레지스트리의 우편에 있다)."""
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
        없으면 레지스트리가 만든다. 4a 에서 입력 큐와 같은 조건변수를 쓰게 된다. 레지스트리의 조건변수 ``_cv`` 는
        언제나 ``post.cv`` 다."""
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
        #: ``_cv`` 를 쥔 채로는 저장을 부르지 않는다(순서: ``_save_lock`` → ``_cv``).
        self._save_lock = threading.Lock()
        # 동시 생존 상한 (세션 한정, web UI 조절). 0 = 무제한. None = 기본값 —
        # env·CLI 는 부트가 `knobs.apply_all` 로 적용한다 (v10.43.0).
        self.max_agents = clamp_max_agents(
            _DEFAULT_MAX_AGENTS if max_agents is None else max_agents
        )

        self._agents: dict[str, AgentInstance] = {}
        self._post = post if post is not None else PostOffice()
        self._cv = self._post.cv
        # private 주소(run:·harness:)의 메일함(v10.84.0)도 우편의 상자다 — 그 루프가 턴 경계와 `wait` 에서 읽는다.
        # UI·영속 밖(프로세스 수명 — 저장 파일은 에이전트와 main 의 상자만 쓴다).
        # 회신 도착 알림(CLI 대기 줄)과 main 이 한가한가(읽기만, 락 없음) — 부트스트랩(`wire_agent_mail`) 주입.
        self.on_reply: Callable[..., None] | None = None
        self.main_is_idle: Callable[[], bool] | None = None
        #: 미배달 메일이 늘거나 비워졌다 (v10.51.0) — web 이 입력창 위 대기열을
        #: 다시 그린다(`WebServer.broadcast_queue`). 락 밖에서 부른다.
        self.on_pending_change: Callable[[], None] | None = None
        #: 모니터 레지스트리 — `wire_monitor_delivery` 가 주입한다. 워커의
        #: `finally` 가 죽은 소유자의 감시를 폐기하려면 참조가 필요하다.
        #: 배선 안 된 레지스트리(테스트 픽스처 다수)는 `None` 이라 모든
        #: 소비 지점이 가드한다.
        self.monitors = None
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
        # resume 이 버린 열린 질문 수 — 부트스트랩이 사람에게 알린다(§3.9).
        self.stale_questions = 0
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
        """P4: 상태 변화를 대화 창 목록에 반영 — web sticky, CLI no-op."""
        from agent_cli.render import get_renderer

        try:
            get_renderer().agent_roster(self.roster_snapshot())
        except Exception:
            pass  # 표시용 — 실행 경로를 막지 않는다

    def alive_count(self) -> int:
        return sum(1 for t in list(self._agents.values()) if t.state != "dead")

    #: "작업 중" 판정 — 어휘의 단일 소유는 `agents_state.state_is_active` (메일의 글도 읽는다).
    state_is_active = staticmethod(agents_state.state_is_active)

    def any_activity(self) -> bool:
        """idle-reap 가드 (v7.10.0): 에이전트가 working 이거나 inbox 에
        미처리 요청이 있으면 True — main 유휴·무접속이어도 인스턴스를
        자가 종료하면 진행 중 작업이 소실되므로 IdleMonitor 의 is_active
        에 합류한다. main 앞 미배달 회신은 게이트하지 않는다 — resume
        시 agents.json pending 미러로 복원·배달되므로 reap 안전."""
        if self.open_human_questions():
            # 사람 주소 질문은 답이 올 때까지 열려 있고 상한도 없다 —
            # 활동으로 세지 않으면 작업 중인 세션을 idle-reap 이 걷는다.
            # (§3.9 가 되살리긴 하지만 걷히는 것 자체가 사용자에겐 사고다.)
            return True
        return any(
            self.state_is_active(t.state)
            or (
                t.state != "dead" and self.backlog_of(t.key) > 0
            )  # 죽은 이의 남은 봉투는 일이 아니다
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

    def pending_for(self, addr: str, exclude: frozenset = frozenset()) -> list[str]:
        """``addr`` 를 **깨울** 에이전트 활동 (v10.65.0, `wait` 의 판정 재료) —
        사람이 읽을 한 줄씩. 세션 전체의 `has_active_work` 가 아니라 **요청자별**:
        그 주소에게 회신을 빚진 에이전트, 그 주소가 보낸 항목을 받은편지함에
        들고 있거나 지금 처리 중인 에이전트. 자기 자신은 빼지 않아도 된다 —
        자기에게 빚지거나 자기 항목을 드는 일은 없다.

        ``exclude`` (v10.77.0): 순환 대기 판정에서 거슬러 온 주소들 — 지금 `wait`
        를 부르는 쪽은 바쁨 상태라 그대로 두면 상대의 깨울 근거로 잡힌다.

        **이미 도착한** 메일은 여기 안 든다(v10.66.2) — 그건 `arrived_for`.
        종전엔 "그 주소로 가는 미배달 회신" 을 ``to`` 키로 세었는데, main 의
        메일함에서 작업·모니터 통지는 ``to`` 가 없어 한 건도
        안 잡혔고(벤치 실측: 모니터가 먼저 발화한 뒤의 wait 가 "nothing
        pending" 으로 거부), 잡혔더라도 **곧 배달될 것** 을 깨울 것으로 쳐
        런을 끝내고 바로 다시 깨우는 낭비였다."""
        return [text for _, text in self.pending_agents_for(addr, exclude)]

    def pending_agents_for(
        self, addr: str, exclude: frozenset = frozenset()
    ) -> list[tuple[str, str]]:
        """`pending_for` 의 재료 — ``(에이전트 주소, 사람이 읽을 줄)`` 쌍 (v10.79.0).
        주소는 `wait` 중간 깨우기의 대상 id(`wake.wake_source_ids`)가 된다."""
        out: list[tuple[str, str]] = []
        for kind, me in self._agent_relations(addr, exclude):
            if kind == "owes":
                out.append((me, f"{me} owes you a reply"))
            elif kind == "working":
                out.append((me, f"{me} working on your request"))
            else:
                out.append((me, f"{me} has your request queued"))
        return out

    def _agent_relations(
        self, addr: str, exclude: frozenset = frozenset()
    ) -> list[tuple[str, str]]:
        """``addr`` 를 깨울 에이전트들과 그 관계 — ``("owes"|"working"|"queued",
        주소)``. 빚진 채 **스스로 잠든** 쪽(`_debtor_is_parked`)은 여기 없다 —
        `parked_debtors_for` 가 따로 들고 호출자가 거슬러 본다."""
        out: list[tuple[str, str]] = []
        with self._cv:
            for debtor in self._debtors_owing(addr):
                if debtor in exclude:
                    continue
                if not self._debtor_is_parked(debtor):
                    out.append(("owes", debtor))
        for tm in list(self._agents.values()):
            me = f"agent:{tm.key}"
            if me == addr or me in exclude:
                continue
            if self.state_is_active(tm.state) and self._holds_unpaid_request_from(
                self._taken(tm), addr
            ):
                out.append(("working", me))
            elif self._holds_unpaid_request_from(self._queued(tm), addr):
                # 받은편지함(우편의 그 주소 상자)에 줄 선 것으로 본다
                out.append(("queued", me))
        return out

    def _holds_unpaid_request_from(self, items: list[dict], addr: str) -> bool:
        """``items``(런이 집은 항목 또는 받은편지함 미러) 가운데 ``addr`` 가 보낸 요청으로 아직 회신이 가지 않은
        것이 있는가 — 읽기만 한다. ``addr`` 의 `wait` 를 깨울 근거는 이것뿐이다(v10.143.0, 사용자 결정): 이미
        회신한 요청·통지로는 그 에이전트가 무엇을 해도 ``addr`` 에게 돌아올 것이 없다.

        ``addr`` 의 기다리는 회신(우편 장부의 열린 r 줄 — 요청이 상자에 들 때 열리고 회신·폴백·버림과 한 락
        구간에 닫힌다)만 센다. 통지는 처음부터 줄이 없다."""
        with self._cv:
            awaited = {r.rid for r in self._post.awaited_by(addr)}
            return any(
                str(it.get("author") or "") == addr
                and str(it.get("id") or "") in awaited
                for it in items
            )

    def checkin_agent_lines(
        self, addr: str, step_marks: dict[str, int]
    ) -> tuple[list[str], bool]:
        """`wait` 중간 깨우기 알림의 에이전트 줄 (v10.79.0, docs/wait/DESIGN.md §6.3)
        — ``(줄 목록, 사람 질문에 막힌 줄이 있는가)``.

        작업·monitor 와 달리 에이전트는 로그 파일이 없다. 레지스트리가 **이미 아는
        것**만으로 그린다 — 상태·지금 처리 중인 요청자·받은편지함 미러·회신 빚과
        스스로 잠듦·그 에이전트가 기다리는 것(`wake.wake_sources`)·열린 사람
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
        question = False
        for kind, me in self._agent_relations(addr):
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
                busy = (
                    f"busy on another request{_last_step(tm)}"
                    if self.state_is_active(tm.state)
                    else "picking it up"
                )
                lines.append(f"{_label(key)} · {where} · {busy}")
            else:
                lines.append(
                    f"{_label(key)} · owes you a reply · busy on another request"
                    f"{_last_step(tm)}"
                )
        for debtor in self.parked_debtors_for(addr):
            key = debtor.split(":", 1)[1]
            on = wake_sources(debtor, frozenset({addr}), agents=self)
            if not on:
                continue  # 깨울 것이 없는 채무자는 깨울 근거가 아니다 (v10.77.0)
            line = (
                f"{_label(key)} · owes you a reply · itself waiting on {', '.join(on)}"
            )
            open_qs = [q for q in self.open_human_questions() if q.asker == key]
            if open_qs:
                q = min(open_qs, key=lambda q: q.asked_at)
                line += (
                    f' · blocked on question "{q.id}" to the user, unanswered for '
                    f"{format_duration(now - q.asked_at)}"
                )
                question = True
            lines.append(line)
        return lines, question

    def _debtor_is_parked(self, debtor: str) -> bool:
        """빚진 쪽이 **스스로 잠들어 있다** — 런이 `wait` 로 끝나 빚만 남은
        유휴 에이전트(보통의 런 끝은 빚을 비운다). 그런 채무자는 그 자체로는
        아무도 깨우지 못한다: 그를 깨울 것이 있어야 회신이 온다
        (:meth:`parked_debtors_for` 와 `wake.wake_sources` 가 거슬러 본다)."""
        if debtor == "main" or not debtor.startswith("agent:"):
            return False
        tm = self._agents.get(debtor.split(":", 1)[1])
        if tm is None or tm.state == "dead":
            return False
        return not self.state_is_active(tm.state) and not self._queued(tm)

    def _debtors_owing(self, addr: str) -> list[str]:
        """``addr`` 에게 아직 갚지 않은 회신을 빚진 쪽들(``addr`` 자신 빼고) — 빚진 쪽의 첫 열린 줄 순서. ``_cv``
        아래에서. 빚진 쪽이 죽었는지는 보지 않는다(발견 1 — 죽은 빚도 근거로 남는다, 4c 에서 바뀐다)."""
        order: list[str] = []
        for row in self._post.rows():
            if row.kind == "r" and row.state == "picked" and row.dst not in order:
                order.append(row.dst)
        return [
            d
            for d in order
            if d != addr and any(r.src == addr for r in self._post.owed_by(d))
        ]

    def parked_debtors_for(self, addr: str) -> list[str]:
        """``addr`` 에게 회신을 빚진 채 잠든 에이전트 주소들 (v10.77.0) — 그들을
        깨울 것이 있는지는 호출자(`wake.wake_sources`)가 거슬러 본다. 서로
        `message` 하고 둘 다 `wait` 하면 아무도 깨지 않는 교착을 여기서 끊는다."""
        with self._cv:
            return [d for d in self._debtors_owing(addr) if self._debtor_is_parked(d)]

    def awaited_replies(self, addr: str) -> list[dict]:
        """``addr`` 가 보낸 요청 중 회신이 아직 오지 않은 것 — 장부의 "기다리는 회신"
        (v10.141.0, docs/agent-engine/DESIGN.md §3). ``[{"id", "from", "text"}]``, 번호 순.

        우편 장부의 열린 r 줄을 읽는다 — 요청이 상자에 들 때 열리고(`_enqueue`·`message_to_main`), 받는 쪽의
        빚이 끝날 때(갚음·폴백·버림 — 배달과 한 락 구간) 닫힌다(v10.145.0).
        갚음·폴백은 회신을 **배달한 뒤** 지운다: 먼저 지우면 그 틈에 요청자의 `wait` 가 기다릴
        회신도 도착한 메일도 없는 순간을 보고 제자리 대신 런을 끝낸다.
        받는 쪽에서 세면(빚 장부 + 받은편지함) 항목이 받은편지함을 떠나 빚으로 적히기까지의
        틈에 회신 기다림이 사라진다. 받는 쪽이 죽었으면 빼다 — 회신을 보낼 이가 없다(그 죽음은
        따로 메일로 온다). 읽기만 한다."""
        if not addr or is_human_addr(addr):
            return []
        with self._cv:
            out = [
                {"id": r.rid, "from": r.dst, "text": r.text}
                for r in self._post.awaited_by(addr)
                if self._agent_alive(r.dst)
            ]
        return sorted(out, key=lambda r: item_ids.number_of(r["id"]))

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
        """에이전트 하나의 표시용 스냅샷 — 앞에 쌓인 할 일을 우편에서 센다."""
        return tm.snapshot(pending_requests=self.backlog_of(tm.key))

    def backlog_of(self, key: str) -> int:
        """에이전트 ``key`` 앞에 쌓인 할 일 — 깨우는 봉투만 센다(런을 열지 않는 정지 회신은 일이 아니다)."""
        return self._post.backlog(f"agent:{key}")

    def queued_for(self, addr: str) -> tuple[MailView, ...]:
        """``addr`` 앞에 줄 서 아직 꺼내 가지 않은 배달 — main 메일함·에이전트 받은편지함(미러)·private 메일함,
        도착 순서. 읽기만 한다 — 락 안에서 사본을 뜬다."""
        if addr == "main" or is_private_owner(addr):
            return tuple(
                view_of_main_mail(dict(e.payload)) for e in self._post.queued(addr)
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

    def arrived_for(self, addr: str) -> int:
        """``addr`` 앞에 **이미 도착해 다음 턴 경계에 배달될** 메일 수 (v10.66.2,
        `wait` 의 둘째 판정 재료). main 은 메일함(작업·모니터
        통지, 에이전트 회신·질문 전부 main 행), 상주 에이전트는 자기 받은편지함.
        이게 0 이 아니면 `wait` 는 받지 않는다: 런을 끝내 봐야 펌프가 바로 다시
        깨우므로, 거부하고 다음 관찰에 배달되게 두는 쪽이 싸고 맥락도 남는다."""
        if addr.startswith("agent:") and addr.split(":", 1)[1] not in self._agents:
            return 0
        if addr == "main" or addr.startswith("agent:") or is_private_owner(addr):
            return self._post.arrived(addr)
        return 0

    def has_pending_replies(self) -> bool:
        return bool(self._post.queued("main"))

    def has_mail_that_wakes_main(self) -> bool:
        """main 메일함에 main 을 깨울 메일이 있는가 — ``wakes_main`` 이 거짓인 메일("Stop everything" 의 정지
        회신, v10.142.0)은 빼고. 깨우기(`MailWaker`)와 `run` 의 정지 판정이 본다. 읽기만 한다."""
        return self._post.has_waking("main")

    def has_active_work(self) -> bool:
        """P5: CLI run 큐 펌프의 정지 판정 — 미배달 회신·처리 중(busy)·
        큐잉된 요청이 하나라도 있으면 True.

        비동기 질문은 아무도 막지 않으므로(§3) 여기서 뺄 상태가 없다 —
        질문을 건 에이전트도 계속 돌고, 답은 새 항목으로 온다. main 을 깨우지 않는 메일("Stop everything"
        의 정지 회신)은 일이 아니다 — 남아 있어도 `run` 은 끝나고 그 메일은 agents.json 미러에 남는다."""
        if self.has_mail_that_wakes_main():
            return True
        # 죽은 에이전트의 받은편지함은 세지 않는다 — 런 도중 끝난(■·✕) 워커는 남은 봉투를 꺼내지 않고 나간다;
        # 세면 `run` 이 끝나지 않는다 (v10.142.0)
        return any(
            tm.state == "busy" or (tm.state != "dead" and self.backlog_of(tm.key) > 0)
            for tm in list(self._agents.values())
        )

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
        self._render_question(q)
        self._save_state()
        # 트레이는 로스터의 ``open_questions`` 를 읽는다 — 알리지 않으면
        # 질문이 다음 로스터 브로드캐스트까지 화면에 안 뜬다.
        self._notify_roster()
        return q.id, ""

    def _render_question(self, q: Question) -> None:
        """대화 창 표면 — 사람이 먼저 보는 자리 (표시 전용, best-effort)."""
        tm = self._agents.get(q.asker)
        if tm is None:
            return
        from agent_cli.render import get_renderer

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
        try:
            get_renderer().agent_message(**payload)
        except Exception:
            pass
        self._log_conversation(tm, payload)

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
        try:
            self._deliver_answer(
                q, text
            )  # 답의 배달이 곧 질문 줄 닫기(``settles`` — 한 락 구간)
        except RowError:
            return unknown  # 다른 답변자가 먼저 답했다 — 줄이 이미 닫혔다
        if (
            self._post.row(qid) is not None
        ):  # 묻는 쪽이 사라져 답이 가지 않았다 — 질문을 버린다
            self._post.forget_questions_of(f"agent:{q.asker}")
        self._save_state()
        return ""

    # ── 회신 빚 장부 (v9.22.1 — main·상주 공통) ──────
    # 요청(`expects_reply=True` 항목 / main 에 배달된 `message`)은 빚이다.
    # 갚는 길: `reply`·요청자에게 `message` — main·에이전트 같다 (v10.141.0).
    # 런의 끝은 `run_ended` 하나로 온다(v10.77.0) — 보통의 끝은 남은 빚을 폴백
    # (라벨 붙은 런 요약)으로 배달하고 비우며, `wait` 면 그대로 두고(다음 런이
    # 갚는다), 중단이면 버린다. 그래서 남아 있는 빚은 언제나 `wait` 가 남긴
    # 것이고, 런 시작이 따로 할 일이 없다(`begin_run` 삭제). 종전엔 상주의 장부가
    # `AgentInstance` 필드 넷(current_expects_reply/current_text/
    # current_answers/replied_this_run)이고 main 의 것이 dict 둘이라 같은
    # 규칙을 두 벌 코드가 들었다.

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
        err = self._deliver_peer_reply(requester, key, text, 0, re=d.id, settles=d.id)
        if err:
            return err
        self._log_outbound(key, text, to=requester)
        return ""

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
        waiting: bool = False,
        stopped: bool = False,
        **meta,
    ) -> int:
        """런 끝의 **유일한** 정리 (v10.77.0) — 상주 단건·사람 배치·main 펌프가
        모두 여기로 온다. 처리한 빚 수를 돌려준다.

        - ``waiting`` (`wait` 로 끝남): 갚을 차례가 아니다 — 열린 빚은 다음 런으로 가고 깨어난
          런이 `reply` 로 갚는다. 폴백을 지금 보내면 요청자가 "⏸ waiting" 을 답으로 받는다. 이 런에서
          닫힌 줄은 다른 끝과 같이 버린다(`PostOffice.end_run` 의 ``wait`` — main 은 여기서, 에이전트는
          `_close_run` 에서).
        - ``stopped`` (정지로 끊김): 남은 빚을 버린다. 사람·모델의 정지(■·✕)는 런의 정리가
          먼저 빚진 상대 모두에게 회신했다(`answer_debts_as_stopped` — 여기 남는 것은 사람 주소의
          빚뿐). 세션 종료의 빚은 재시작이 끊긴 요청을 새 id 로 다시 넣으며 다시 적는다 — 남기면
          같은 요청에 빚이 둘이 되고, 죽은 채무자의 빚이 요청자의 `wait` 를 영원히 붙잡는다.
        - 그 밖(보통의 끝): :meth:`end_run` — 남은 빚을 라벨 붙은 런 요약으로.

        종전엔 이 판정(``not waiting``·``not stop_event``)이 호출부 셋에 흩어져
        있었고, v10.65.0 의 상주 판정이 런 시작(`begin_run` 의 비우기)을 빠뜨려
        wait 뒤의 빚이 사라졌다.

        ``waiting`` 이면 중간 깨우기 타이머를 건다 (v10.79.0, :meth:`arm_checkin`) —
        런 끝의 유일한 자리라 main·상주·run·web 이 한 곳이다. ``meta`` 의 ``run``
        키는 그 런을 연 배달 id 이고 `run_started`(런 **시작**)와는 무관하다."""
        if waiting:
            if debtor == "main":  # 에이전트는 런 정리(`_close_run`)가 든 것을 비운다
                self._post.end_run(debtor, run_id=debtor, outcome="wait")
            self.arm_checkin(debtor)
            return 0
        self.run_started(debtor)  # 끝났으니 걸린 것이 있어도 의미가 없다
        if stopped:
            # 버린 빚 — 요청자는 더 기다리지 않는다(dropped). 세션 종료도 여기다(추정 버그 3 — 5단계에서
            # outcome="session_end" 로 바뀐다)
            self._post.end_run(debtor, run_id=self._run_of(debtor), outcome="stopped")
            return 0
        return self.end_run(debtor, output, **meta)

    # ── `wait` 중간 깨우기 (v10.79.0, docs/wait/DESIGN.md §6) ──────────────
    #
    # 받아들여진 `wait` 가 한참 깨지 않으면 하니스가 깨운다 — 벤치 50번의 wait 중
    # 13번이 깨지 못한 채 과제 시간으로 끝났고, 헤드리스(run·예약) 세션은 작업에
    # 시간 제한이 없어 끝없이 잠들 수 있었다. 알림은 기존 셋째 갈래(장부 밖 메일,
    # `ledger: "none"`)의 첫 사용자다 — 새 층이 아니다.

    def next_checkin_s(self, owner: str) -> int | None:
        """이 소유자가 지금 `wait` 하면 걸릴 간격(초) — 없으면(예약만 기다림) None.
        `dispatch._op_wait` 가 결과 문장에 적고 `arm_checkin` 이 같은 값을 건다."""
        ids = wake_source_ids(owner, agents=self)
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

    def arm_checkin(self, owner: str) -> int | None:
        """`wait` 로 끝난 런 — 타이머를 건다. 걸린 간격(초) 또는 None.

        기준을 함께 적는다: 작업별 로그 크기(P(a) "+N lines since your wait")와
        에이전트별 `last_ordinal`("N steps since your wait")."""
        self.run_started(owner)  # 이미 걸린 것이 있으면 푼다
        ids = wake_source_ids(owner, agents=self)
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

    def run_started(self, owner: str) -> None:
        """이 소유자의 런이 시작됐다(어떤 이유로든 — 보고·메일·사람 입력) —
        걸린 타이머를 푼다. `AgentLoop.run()` 이 배달 뒤 한 번 부른다; 네 경로
        (main·상주·run·web)가 그 한 지점을 지난다. 걸린 것이 없으면 no-op."""
        with self._cv:
            ck = self._checkin_armed.pop(owner, None)
        if ck is not None and ck.timer is not None:
            ck.timer.cancel()

    def _cancel_checkins(self, *owners: str) -> None:
        """정리 — 소유자가 죽거나 세션이 끝난다. 빈 인자는 전부."""
        with self._cv:
            targets = list(owners) if owners else list(self._checkin_armed)
        for o in targets:
            self.run_started(o)

    def fire_checkin(self, owner: str) -> str:
        """타이머가 부른다(테스트는 직접) — 알림을 만들어 배달하고 본문을 돌려준다.
        걸린 것이 없거나(런이 먼저 시작해 풀렸다) 아무것도 안 돌고 있으면 "".

        알림은 사실 진술("still running: …")이라 진짜 보고와 같은 순간에 와도
        거짓이 아니다 — 그래서 "런 진행 중" 상태도, 락 안의 확인+추가도 없다.
        main 이면 메일함(`_push_reply`, `MailWaker` 가 깨운다), 상주면 받은편지함
        (`request`, 봉투 ``checkin``). 둘 다 ``ledger: "none"`` — 장부 항목이 되지
        않고 영속되지 않는다(`agents_state.persists`)."""
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
                d.to.split(":", 1)[1], "main", summary, 0, re=d.id, settles=d.id
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

    def _deliver_answer(self, q: Question, text: str) -> None:
        """사람의 답을 asker 에게 — 새 배달(사람 발신이라 ``u-``)로.

        사람이 보낸 것이므로 그 런은 사람 런이다: 결과는 창이 곧 배달이고, 닫는
        법은 사람 요청과 같다(`complete` 의 `answers`). 봉투가 무엇에 대한
        답인지(``re``) 적는다."""
        self.request(
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
        """에이전트 사망 정리 — 그가 건 질문은 폐기한다(답할 사람이 트레이에서
        답해도 받을 곳이 없다). 호출자가 ``not revivable`` 아래에서만 부른다 —
        세션 종료(``shutdown_all``)에서 지우면 직후의 ``_save_state`` 가 빈 목록을
        저장해 resume 이 질문을 되살리지 못한다.
        """
        outgoing = self._post.forget_questions_of(f"agent:{key}")
        if outgoing:
            self._save_state()
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
        resumed: bool = False,
        images: list[dict] | None = None,
        item_id: str = "",
        kind: str = "",
        re: str = "",
        todo: str = "",
        ledger: str = "",
        mark: str = "",
        opens_run: bool = True,
        settles: str = "",
    ) -> str:
        """request 큐잉 — 에러 메시지 또는 빈 문자열.

        ``settles`` (v10.145.0): 이 배달이 닫는 줄 — 갚는 빚·답하는 질문의 id. 갚는 자리만 준다(`_post_deliver`).
        항목에 실리지 않는다.

        ``ledger`` (v10.79.0): ``"none"`` 이면 장부 밖 메일 — agents.json 미러에
        남지 않는다(`agents_state.persists`). 첫 사용자는 `wait` 의 중간 깨우기(``checkin``):
        "지금" 에만 참인 알림이 다음 세션 첫 턴에 낡은 채 배달되면 안 된다.

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

        v9.12 의 ``submit() -> (error, verdict)`` 은 ask 답변 슬롯 배달을
        구분하려던 것인데, 비동기 전환으로 슬롯이 사라져 판정할 것이 없다 —
        이름과 반환형을 원래대로 되돌렸다.

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
        if tm.state == "dead" or tm.stop_event.is_set():
            reason = f" ({tm.error})" if tm.error else ""
            return f'agent "{key}" is dead{reason} — spawn a new one'
        if not message.strip():
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
            # 발신 시각 — 스윔레인 요청 화살표(agent_msg "in", ts=send_ts)와
            # 작업 카드(begin_agent_work→scope_start)가 같은 앵커를 갖도록
            # worker 로 실어 보낸다(웹 프런트가 카드 data-nav-ts 로 사용).
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
            # 런을 열지 않는 항목 (v10.142.0, "Stop everything" 의 정지 회신) — 워커가 들고 있다가 다음 런의
            # 첫 흡수에 넣는다(`_release_held`). 받은편지함 미러에는 남아 재시작에도 산다.
            item["opens_run"] = False
        if resumed:
            item["resumed"] = True  # 재시작 계속하기 — 다시 끊겨도 또 감싸지 않는다
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
        self._enqueue(tm, item, settles=settles)
        # 답이든 일감이든 **항상** 창·로그·로스터에 남긴다 — 답만 건너뛰면
        # 🤝 창과 conversation.jsonl(=resume 재생 소스)에서 사라진다.
        from agent_cli.render import get_renderer

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
        get_renderer().agent_message(**payload)
        self._log_conversation(tm, payload)
        self._notify_roster()
        return ""

    def _enqueue(self, tm: AgentInstance, item: dict, *, settles: str = "") -> None:
        """받은편지함(우편의 그 주소 상자)에 넣고 ``agents.json`` 에 남긴다 (v9.24.5). 회신을 기다리는 요청이면
        그 줄이 보낸 쪽의 "기다리는 회신" 이다(v10.141.0 — 넣는 것과 한 락 구간)."""
        with self._cv:
            self._post_deliver(
                envelope_of_item(item, f"agent:{tm.key}"), settles=settles
            )
        self._save_state()

    def _begin_items(self, tm: AgentInstance, items: list[dict]) -> None:
        """이 항목들로 런을 시작한다 — 우편은 `_next_run_items` 가 이미 집었다(처리 중). 그 상태를 저장한다."""
        self._save_state()

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
        from agent_cli.render import get_renderer

        renderer = get_renderer()
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
            renderer.agent_message(**absorbed)
            self._log_conversation(tm, absorbed)
            if is_human_addr(author):
                # 빚은 아니지만 `reply` 거부 사유가 이걸 본다 (사람 배치와 동일)
                uid = iid
                self.note_owes(me, author, text, id=uid)
                out.append(
                    {
                        "user_request": {
                            "id": uid,
                            "author": author,
                            "text": self._model_text(item, bare=True),
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
                content = self._model_text(item, mid_run=True)
                out.append(
                    {
                        "record": {
                            "role": "user",
                            "tool": "agent",
                            "success": True,
                            "content": content,
                            "source": "agent_request",
                        }
                    }
                )
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
                out.append({"record": rec})
        self._save_state()
        self._notify_roster()
        return out

    def _close_run(self, tm: AgentInstance, handled: int) -> None:
        """런 끝 — 두 처리 경로(단건·사람 배치)의 공통 마무리.

        ``handled`` 증가와 처리 중 표시 해제(v9.24.5)를 **한 번의 저장**에
        싣는다. 해제를 따로 저장하면 회신이 나간 뒤 다음 항목을 꺼내기까지
        디스크 쓰기가 한 번 더 끼어, 그 사이 idle 로 보이는 창이 넓어진다."""
        with self._cv:
            # 흡수분(v9.25.0)까지 이 런이 처리했다 — 시작 항목 수보다 클 수 있다
            tm.handled += handled + tm.absorbed_in_run
            tm.absorbed_in_run = 0
            # 든 것을 비우고 이 런에서 닫힌 줄을 버린다(열린 줄은 런 끝 정리 `run_ended` 가 이미 닫았거나,
            # `wait` 면 그대로 — 잠든 빚)
            self._post.end_run(
                f"agent:{tm.key}",
                run_id=self._run_of(f"agent:{tm.key}"),
                outcome="wait",
            )
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

        **두 백엔드는 나르는 것이 다르다** — 그래서 인자가 둘이다:

        - main 에는 worker/inbox 가 없다. 메일박스가 유일한 흡수 지점이고
          ``MailWaker`` 가 idle main 도 깨운다. 메일박스 아이템은 **구조**를
          싣는다(``kind``/``id``/``key``/``profile`` — UI 렌더와 ``answer(id)``
          가 그걸 읽는다). 그래서 ``mail``.
        - 에이전트 inbox 는 **평문**을 싣는다. 항목 1개 = 런 1개이므로 상대가
          idle 이어도 깨어난다. 그래서 ``text``.

        둘이 같은 내용인 호출부(답·독촉)는 같은 문자열을 두 번 준다. 질문만
        본문이 갈린다(main 은 질문 원문, 에이전트는 출처를 머리에 단 한 줄).
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

    def _push_reply(self, reply: dict, *, settles: str = "") -> None:
        with self._cv:  # 배달 순간의 한가함은 배달 구간에서 읽는다 — 락을 놓으면 펌프가 이 메일로 런을 열 수 있다
            self._stamp_mail(reply)
            self._post_deliver(envelope_of_main_mail(reply), settles=settles)
            idle_at_delivery = self.main_is_idle() if self.main_is_idle else None
        self._save_state()  # P3: 미배달분 디스크 미러 — resume 시 유실 없음
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
        return [mail_queue_item(dict(e.payload)) for e in self._post.queued("main")]

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
    ) -> str:
        """요청의 회신을 요청자 inbox 로 terminal 재주입 (v5.11) — 에러 또는 "". ``settles`` 는 `request` 와 같다.

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
        self._push_reply(rec, settles=settles)
        # ★v7.11.1 (실사고): mailbox 만 채우면 main 챗 관찰로는 보이는데
        # 발신 에이전트의 🤝 대화창·conversation.jsonl 에는 흔적이 없다
        # (재접속/resume 소실). 발신자 창에 out 방향으로 남긴다.
        self._log_outbound(from_key, text, to="main")
        return str(rec.get("item_id") or "")

    def _log_outbound(
        self, from_key: str, text: str, *, to: str, ts: float | None = None
    ) -> None:
        """발신 에이전트 창에 out 메시지 기록 — 라이브 표면(agent_message)
        + 대화 로그(conversation.jsonl, resume 재생 소스) 동시. 각 창은 그
        에이전트 관점의 완결 대화: 수신측 in 은 request() 가 담당.

        ``ts`` 를 넘기면 그 시각으로 스탬프한다 — peer send 는 이 화살표를
        ``request()`` **뒤에** 기록하므로, 여기서 ``time.time()`` 을 다시 찍으면
        수신 워커가 이미 시작한 작업(scope_start)보다 늦어 팀 뷰에서 요청이
        작업 아래로 밀린다. 호출자가 send 직전 시각을 잡아 넘긴다."""
        tm = self._agents.get(from_key)
        if tm is None:
            return
        from agent_cli.render import get_renderer

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
        try:
            get_renderer().agent_message(**payload)
        except Exception:
            pass  # 표시용 — 전송 경로를 막지 않는다
        self._log_conversation(tm, payload)

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
            err = self.request(
                to,
                text,
                author=sender,
                expects_reply=bool(expects_reply),
                item_id=rec_id,
                kind="" if expects_reply else "notice",
            )
            if err:
                return "", err
            if me_key:
                self._log_outbound(me_key, text, to=to, ts=send_ts)  # 발신자 창 out
            line = delivered_line(rec_id, expects_reply=bool(expects_reply))
            target = self._agents.get(to)
            backlog = self.backlog_of(to) if target is not None else 0
            stacked = REQUEST_STACKED.format(n=backlog) if backlog >= 2 else ""
            return line + stacked, ""

        return handler

    def drain_replies(self) -> list[dict]:
        """미배달 회신 전량 회수 (턴 경계 배달 — D2). 도착 순서 유지."""
        envs = self._post.absorb("main")
        out = [dict(e.payload) for e in envs]
        if out:
            self._save_state()  # 배달 완료 → 디스크 미러도 소비
            self._pending_changed()  # 대기열에서 빠진다 = 주입됐다
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
        self,
        addr: str,
        *,
        stop_event,
        still_waiting,
        wait_input=None,
        poll_s: float = 0.5,
    ) -> str:
        """``addr`` 앞에 메일이 올 때까지 막는다 — 제자리 `wait` (`dispatch._wait_in_place`:
        하니스 작업, 기다리는 회신을 둔 main). 돌려주는 값은 깬 이유: ``"mail"``(도착 —
        `arrived_for`), ``"input"``(``wait_input(timeout)`` 이 참 — 사람 입력이 도착했다,
        main 만), ``"stopped"``(``stop_event``), ``"nothing"``(``still_waiting()`` 이 거짓 —
        기다리던 것이 메일 없이 사라졌다: 잡을 밖에서 죽임, 모니터 삭제, 에이전트 사망).
        상한은 없다 — 중간 깨우기가 메일로 와서 모델이 판단한다."""
        while True:
            with self._cv:
                if self.arrived_for(addr):
                    return "mail"
                self._cv.wait(timeout=poll_s if wait_input is None else poll_s / 2)
                if self.arrived_for(addr):
                    return "mail"
            if wait_input is not None and wait_input(poll_s / 2):
                return "input"
            if stop_event is not None and stop_event.is_set():
                return "stopped"
            if not still_waiting():
                # 회신을 갚는 쪽은 빚을 닫은 **뒤** 배달한다 — 그 틈에 "기다릴 것 없음" 으로 보이는
                # 회신이 곧 도착한다. 한 번 더 보고 판정한다.
                with self._cv:
                    if not self.arrived_for(addr):
                        self._cv.wait(timeout=poll_s)
                    if self.arrived_for(addr):
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
                self._save_state()
                self._pending_changed()
                return mail
            if stop_event is not None and stop_event.is_set():
                return None
            if not any(r["id"] == rid for r in self.awaited_replies("main")):
                with self._cv:  # 사라지기 직전에 회신이 왔을 수 있다
                    if not any(e.re == rid for e in self._post.queued("main")):
                        return None

    def drop_mailbox(self, addr: str) -> int:
        """private 주소의 루프가 끝났다 — 체크인을 풀고 메일함을 버린다(읽을 사람이
        없다). 버린 메일 수를 돌려준다."""
        self._cancel_checkins(addr)
        with self._cv:
            self._checkin_last.pop(addr, None)
            if not is_private_owner(addr):
                return (
                    0  # 에이전트 주소의 받은편지함은 그대로 — private 메일함만 버린다
                )
            envs = self._post.take(addr)
            return len(envs)

    # ── status / kill / 종료 ────────────────────

    def format_status(self, key: str = "") -> str:
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
        undelivered = len(self._post.queued("main"))
        if undelivered:
            lines.append(f"(undelivered replies: {undelivered})")
        # 폴링 차단 넛지: working 이거나 미처리 inbox 가 있으면 모델이
        # status 를 돌려 기다리는 상황일 가능성이 높다 — 회신은 mail 로
        # 자동 배달되고 도착 시 harness 가 깨우므로(폴링 금지 설계),
        # `wait` 로 턴을 마치는 게 올바른 대기다(v10.65.0). 전부 idle 인 로스터
        # 확인용 status 에는 붙이지 않는다(노이즈).
        waiting = any(
            self.state_is_active(tm.state) or self.backlog_of(tm.key) > 0
            for tm in items
        )
        if waiting:
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
        """에이전트의 평생을 끝낸다 — 되살리지 않음·체크인·감시 정리·평생 정지·워커 깨우기(``join`` 이면
        ≤2s 기다림 — ✕ 는 창을 비우기 전에 기다린다, ■ 는 기다리지 않는다). 빚 알림은 워커의 정리
        (`_answer_after_stop`)가 원인(``stop_cause``)을 보고 한다."""
        tm.revivable = False  # P3: 명시 kill 은 영구 — resume 이 되살리지 않음
        self._cancel_checkins(f"agent:{tm.key}")  # 죽은 에이전트에게 알림이 가지 않게
        # 감시 폐기는 `stop_event.set()` **앞**이다. busy 에이전트는 stop 뒤에도
        # 현재 턴을 마저 돌고(join 은 best-effort), 그 창에 발화하면 항목이
        # 워커가 다시 꺼내지 않아 영영 안 읽힌다. 그 턴에서 새로 건 감시는
        # 워커의 `finally` 가 잡는다 — 죽음이 확정되는 유일한 지점이다.
        self._drop_monitors(tm.key)
        tm.stop_event.set()
        self._wake_worker()
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
        """일하는 에이전트의 키 — 런 중인 것과 일을 끝내지 않은 run 에이전트(``wait`` 로 잠든 것 포함).
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
        me = f"agent:{tm.key}"
        by = tm.stop_by or "user"
        mark = agent_stop.STOPPED_MARK.format(by=by)
        for d in self.reply_debts(me):
            text = agent_stop.stopped_reply(
                tm.key, d.id, d.text, activity, notes, by=by
            )
            self._send_stop_reply(  # 정지 회신의 배달이 곧 갚음
                tm, d.to, d.id, text, mark, hop=d.hop, answers=d.answers, settles=True
            )

    def _answer_queued_as_removed(self, tm: AgentInstance) -> None:
        """시작 안 한 대기 요청(받은편지함 미러의 요청 항목)에 removed 회신을 보내고 미러를 비운다."""
        by = tm.stop_by or "user"
        mark = agent_stop.REMOVED_MARK.format(by=by)
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
            text = agent_stop.removed_reply(
                tm.key, str(it.get("id") or ""), str(it.get("text") or ""), by=by
            )
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
        배달이 빚 ``rid`` 를 갚는다(정지 회신 — 한 락 구간). removed 회신은 줄이 이미 닫혀 갚지 않는다."""
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
            )
            self._log_outbound(tm.key, text, to="main")
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
        """사람(또는 모델)이 끝낸 에이전트의 남은 것을 표(`agent_stop.stop_rule`)대로 정리한다 — 워커가 죽을 때
        한 번. 런 없이 남은 빚(스스로 잠든 빚)·대기 요청·띄운 run, 그리고 화면 한 줄."""
        rule = agent_stop.stop_rule(tm.stop_cause, tm.ends_after_task)
        self.answer_debts_as_stopped(tm, activity=[], notes="")
        kept = 0
        if rule.answers_queued:
            self._answer_queued_as_removed(tm)
        else:
            kept = len(self._queued(tm))
        if rule.stops_sub_runs:
            me = f"agent:{tm.key}"
            for sub in list(self._agents.values()):
                if sub.parent == me and sub.ends_after_task:
                    self.stop_run(
                        sub.key,
                        by=tm.stop_by or "user",
                        wakes_requesters=tm.stop_replies_wake,
                    )
        self.report_stop(tm, kept=kept)

    def report_stop(self, tm: AgentInstance, *, kept: int) -> None:
        """정지 한 번의 화면 한 줄(CLI ``■ agt-3 │ stopped by user — reply sent to main (r-21)``) — 보낸 상대를
        비운다."""
        from agent_cli.render import get_renderer

        by = tm.stop_by or "user"
        mark = (
            agent_stop.REMOVED_MARK
            if tm.stop_cause == agent_stop.REMOVED
            else agent_stop.STOPPED_MARK
        )
        notified, tm.stop_notified = tm.stop_notified, []
        try:
            get_renderer().agent_stopped(
                key=tm.key, how=mark.format(by=by), notified=notified, kept=kept
            )
        except Exception:
            pass  # 화면은 best-effort — 정리를 막지 않는다

    def shutdown_all(self, *, deadline: float | None = None) -> None:
        """세션 종료 — 전원 kill. main 부트스트랩의 finally 에서 호출. ``deadline``
        (SIGTERM 경로, v10.134.1)이 있으면 워커를 그 안에서만 기다린다."""
        self._cancel_checkins()  # main 포함 — 끝나는 세션에 알림이 쌓이지 않게
        for tm in list(self._agents.values()):
            self._drop_monitors(tm.key)
            tm.stop_event.set()
        self._wake_worker()
        for tm in list(self._agents.values()):
            if tm.worker is not None:
                tm.worker.join(timeout=wait_left(deadline, 5.0))
        # 최종 스냅샷 — revivable 유지 상태로 기록돼 resume 이 되살린다 (D7).
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
        """agents.json 원자 저장 (fsio) — manifest + 미배달 pending 미러.

        갱신 시점: spawn/kill/worker 종료(상태) + push/drain(pending).
        상태 파일은 best-effort — 디스크 문제로 세션 진행을 막지 않는다.
        """
        path = self._state_path()
        if path is None:
            return
        # 읽지 않은 파일은 덮어쓰지 않는다 (v10.131.1). 복원 전의 명단은 비어 있어서,
        # 그때 쓰면 resume 세션의 상주 에이전트와 미배달 메일이 지워진다 — 조립 도중
        # 죽어 `shutdown_all` 만 불린 길. 건너뛴 저장은
        # 잃지 않는다: `restore` 는 메모리의 pending 에 덧붙이고, 그 뒤 첫 저장이 다 쓴다.
        # 뜨기와 쓰기를 한 락 아래에서 (v10.133.2) — 따로 두면 워커가 뜬 옛 사본이
        # main 의 새 저장 **뒤에** 써져, 이미 꺼낸 회신이 디스크에 되살아난다.
        with self._save_lock:
            if not self._state_owned and path.is_file():
                return
            self._write_state(path)

    def _write_state(self, path: Path) -> None:
        """명단·미배달분·열린 질문을 떠서 쓴다 — ``_save_lock`` 을 쥔 채로만 부른다."""
        # Snapshot under the GIL — ``_save_state`` runs on worker threads while
        # the main thread mutates ``_agents``; iterating the live view raced a
        # spawn and killed an UNRELATED agent via the worker's except-handler
        # (AUDIT S-1). ``list(...)`` can't raise mid-iteration.
        tms = list(self._agents.values())
        # 열린 질문은 resume 이 **되살린다**(§3.9) — 그러려면 남아야 한다. 되살리지 못한 것만 `stale_questions` 로
        # 알린다. 파일 모양은 옛 저장소에서 짓던 것과 같다(2단계 — `agents_state.document_from_post`).
        with self._cv:
            doc = agents_state.document_from_post(tms, self._post)
        try:
            from agent_cli.fsio import atomic_write_json

            atomic_write_json(path, doc)
            self._state_owned = True
        except OSError:
            pass

    def restore(self, parent_ctx=None) -> int:
        """세션 resume 시 agents.json 에서 재생성 — 되살린 수 반환.

        - 살아있던(revivable) teammate: 자기 history 를 resume 한 ctx 로
          worker 재기동 (이전 문답 전부 기억). kill 된 것은 dead 툼스톤으로
          만 복원 (status 가시성).
        - 미배달 pending 은 그대로 복원 → 첫 턴 경계에 정상 배달.
        - 열린 질문도 **되살린다**(§3.9): ctx 가 통째로 resume 되므로 나중에
          도착한 답도 평소처럼 새 런으로 처리된다 — 못 할 기술적 이유가
          없다. 되살리지 못하는 것은 asker 나 target 이 돌아오지 않은 것뿐.
        - 파일 부재/파손/버전 불일치는 조용히 no-op (fresh 세션과 동일).
        """
        self._state_owned = True  # 읽으려 했다 — 이제부터 이 파일은 이 레지스트리의 것
        path = self._state_path()
        data = agents_state.read_state(path) if path is not None else None
        if data is None:
            return 0

        with self._cv:
            for item in data.get("pending", []):
                if not isinstance(item, dict):
                    continue
                self._stamp_mail(item)
                self._post.deliver(envelope_of_main_mail(item))
            # 열린 질문 복원 — 살릴 수 있는 것만. asker/target 판정은 에이전트
            # 복원 뒤에 해야 하므로 여기서는 담아만 둔다.
            restored = [
                q
                for q in (
                    Question.from_dict(d)
                    for d in data.get("questions", [])
                    if isinstance(d, dict)
                )
                if q is not None
            ]
            # 번호 잇기 (v10.77.0) — 할당기는 저장하지 않고 남은 id 를 본다
            item_ids.observe(
                *(e.id for e in self._post.queued("main")),
                *(q.id for q in restored),
            )

        revived = 0
        requeue: list[tuple[AgentInstance, list, list]] = []
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
            if e.get("state") == "dead":
                tm.state = "dead"
                tm.error = e.get("error", "")
                tm.revivable = False
                continue
            tm.revive = True
            in_flight = [i for i in (e.get("in_flight") or []) if isinstance(i, dict)]
            queued = [i for i in (e.get("inbox") or []) if isinstance(i, dict)]
            # 새 id 가 복원분과 겹치지 않게 (v10.77.0 — 할당기는 저장하지 않는다)
            item_ids.observe(*(i.get("id") for i in in_flight + queued))
            tm.worker = threading.Thread(
                target=self._worker,
                args=(tm, parent_ctx),
                daemon=True,
                name=f"agent-{key}",
            )
            tm.worker.start()
            # 5.13: 세션 resume(b) 근본 경로 — 새 프로세스라 replay 버퍼가
            # 비어 🤝 대화창이 텅 비던 증상. conversation.jsonl 을 재생해
            # 복원한다(restore 는 SSE 접속 전이라 buffer 에 쌓였다가 첫
            # 접속 snapshot 으로 배달). dead(kill) 툼스톤은 재생 안 함 —
            # kill=정리 일관(필요하면 mode:"resume" 이 그때 복원).
            self._replay_conversation(tm)
            requeue.append((tm, in_flight, queued))
            revived += 1
        # 되넣기는 명단을 다 세운 뒤 — 끊긴 run 의 요청자가 돌아왔는지 봐야 한다
        for tm, in_flight, queued in requeue:
            self._requeue(tm, in_flight, queued)
        self._adopt_questions(restored)
        self._save_state()
        self._notify_roster()
        return revived

    def _requeue(self, tm: AgentInstance, in_flight: list, queued: list) -> None:
        """재시작이 남긴 항목을 되돌린다 (v9.24.5) — 끊긴 런이 맨 앞.

        끊긴 런(처리 중이던 첫 항목)은 계속하기 안내를 붙여 ``request`` 로
        다시 넣는다: 새 id·대화창 한 줄·같은 발신자(빚 장부가 같은 상대에게
        답을 강제). 이미 한 번 이어 하던 항목(``resumed``)이면 감싸지 않고
        그대로 — 재시작이 거듭돼도 안내가 쌓이지 않는다. 나머지(배치의 다른
        항목, 줄 서 있던 요청)는 이미 대화창·장부에 있으니 조용히 되넣는다."""
        from agent_cli.constants import RESUMED_RUN_NOTICE

        rest = list(in_flight[1:]) + list(queued)
        if in_flight:
            first = in_flight[0]
            author = str(first.get("author") or "main")
            if tm.ends_after_task and not self._agent_alive(author):
                # 끊긴 run 의 요청자가 돌아오지 않았다 — 회신은 main 메일로 (설계 §2 resume)
                author = "main"
            if first.get("resumed"):
                rest.insert(0, {**first, "author": author})
            else:
                expects = bool(first.get("expects_reply", True))
                # id 를 먼저 받는다(종류 규칙은 `request` 와 같다) — run 의 과제 id 를 잇기 위해
                rid = item_ids.next_id(
                    item_ids.USER
                    if is_human_addr(author)
                    else item_ids.REPLY
                    if expects
                    else item_ids.MAIL
                )
                self.request(
                    tm.key,
                    RESUMED_RUN_NOTICE + str(first.get("text") or ""),
                    author=author,
                    hop=int(first.get("hop") or 0),
                    expects_reply=expects,
                    answers=first.get("answers"),
                    resumed=True,
                    item_id=rid,
                    kind=str(first.get("kind") or ""),
                    re=str(first.get("re") or ""),
                    todo=str(first.get("todo") or ""),
                )
                if tm.ends_after_task and first.get("id") == tm.task_id:
                    tm.task_id = rid  # 과제가 새 id 로 다시 들어갔다
        for item in rest:
            self._enqueue(tm, item)

    def _adopt_questions(self, restored: list[Question]) -> None:
        """resume 이 복원한 질문을 받아들인다 — 에이전트 복원 **뒤에**.

        주소는 사람이라 언제나 살아 있다 — asker 가 돌아오지 않았으면(kill
        툼스톤·매니페스트 부재) 답을 받을 데가 없으니 버린다. 버린 수는
        ``stale_questions`` 로 사람에게 알린다. 되살린 질문은 트레이에 다시 뜨고,
        사람이 답하면 평소처럼 새 배달로 간다.
        """
        if not restored:
            return
        dropped = 0
        with self._cv:
            for q in restored:
                tm = self._agents.get(q.asker)
                if tm is None or tm.state == "dead":
                    dropped += 1
                    continue
                self._post.ask(q.id, f"agent:{q.asker}", q.target, q.text, meta=q)
        self.stale_questions = dropped

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

    def _conversation_path(self, tm: AgentInstance) -> Path:
        return tm.home_dir / "conversation.jsonl"

    def _log_conversation(self, tm: AgentInstance, payload: dict) -> None:
        """🤝 대화창 메시지 1건을 ``conversation.jsonl`` 에 append (5.13).

        이 파일이 대화창의 **진짜 소스** — resume 시 :meth:`_replay_conversation`
        이 그대로 재발행해 창을 정확 복원한다(ctx 는 에이전트 내부 작업까지
        담아 대화창과 추상화 레벨이 달라 소스가 못 됨). best-effort."""
        try:
            tm.home_dir.mkdir(parents=True, exist_ok=True)
            with self._conversation_path(tm).open("a", encoding="utf-8") as f:
                f.write(json.dumps(payload, ensure_ascii=False) + "\n")
        except OSError:
            pass

    def _replay_conversation(self, tm: AgentInstance) -> None:
        """``conversation.jsonl`` 을 순회해 🤝 대화창을 재발행 (5.13).

        라이브와 같은 ``agent_message`` 표면을 통과하므로 web 은 persistent
        버퍼에 다시 쌓여 재접속 뷰어까지 복원되고, 저장해 둔 ``ts`` 로 원래
        대화 시각이 유지된다. 파일 없으면 no-op. kill→resume 대칭: kill 이
        표면을 정리(clear_agent_conversation)하고 resume 이 여기서 다시 채움."""
        path = self._conversation_path(tm)
        if not path.exists():
            return
        from agent_cli.render import get_renderer

        renderer = get_renderer()
        # 재생 전에 표면을 비운다 → 멱등: 비정상 사망(kill 아님)으로 옛
        # agent_msg 가 버퍼에 남아 있어도 중복 없이 정확히 한 벌만 남는다.
        renderer.clear_agent_conversation(tm.key)
        try:
            with path.open(encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(rec, dict) and rec.get("key"):
                        renderer.agent_message(**rec)
        except OSError:
            pass

    def _worker(self, tm: AgentInstance, parent_ctx) -> None:
        """teammate 의 전 생애: 스코프 열기 → ctx 생성 → inbox 루프 → 정리.

        상태 전이는 전부 이 함수 안이다.
        """
        from agent_cli.render import get_renderer
        from agent_cli.subagent.resident_loop import ResidentLoop

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

            while not tm.stop_event.is_set():
                items = self._next_run_items(tm)
                if not items:
                    break
                self._begin_items(tm, items)
                finished = kind.run(kind.start(items))
                if tm.ends_after_task and not self._run_ended_by_wait(finished):
                    self._come_down(tm, finished)
                    break
        except BaseException as e:
            tm.error = f"{type(e).__name__}: {e}"
            crash = tm.error
        finally:
            tm.state = "dead"
            if crash and not tm.stop_event.is_set():
                # Q4: kill/세션종료(의도된 종료)가 아닌 사망 → main 에 관찰
                # 통지. MailWaker 가 on_reply 로 idle run 도 깨운다.
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
            if not tm.revivable:
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

    def _next_run_items(self, tm: AgentInstance) -> list[dict]:
        """다음 런을 열 항목 — 우편의 `start_run`(첫 **깨우는** 봉투, 사람의 말이면 이어진 사람의 말까지 묶음 — 깨우지
        않는 봉투는 줄에 남아 그 런의 첫 흡수가 읽는다). 올 때까지 시한 없이 기다린다: 배달이 깨우고, ``stop_event`` 를
        세우는 둘(`_end_agent`·`shutdown_all`)이 세운 뒤 깨운다(`_wake_worker`). 정지가 서면 빈 목록.

        집는 순간 ``busy`` 다(P0-9b) — 그 사이 idle-reap 의 `any_activity`(상태·쌓인 일)가 "활동 없음" 으로 보지
        않게 같은 락 구간에서."""
        addr = f"agent:{tm.key}"
        with self._cv:
            while True:
                if tm.stop_event.is_set():
                    return []
                envs = self._post.start_run(addr)
                if envs:
                    tm.state = "busy"
                    return [dict(e.payload) for e in envs]
                self._cv.wait()

    def _wake_worker(self) -> None:
        """정지를 세운 뒤 기다리는 워커를 깨운다 — 그 워커가 ``stop_event`` 를 보고 나온다."""
        with self._cv:
            self._cv.notify_all()

    @staticmethod
    def _run_ended_by_wait(finished) -> bool:
        """그 런이 `wait` 로 끝났는가 — run 에이전트가 내려오지 않는 유일한 끝이다(깨어 계속한다).
        그 밖의 끝(`complete`, 턴 상한·오류)은 run 에이전트의 수명 끝이다 (사용자 결정 2026-11-15:
        회신은 장부가 보장한다 — 갚을 빚이 남으면 `complete` 가 되돌아오고, 그래도 남으면 런 끝
        폴백이 런 요약으로 회신한다). 런 중간의 `reply` 는 끝이 아니다."""
        result = finished[0] if isinstance(finished, tuple) else finished
        return bool(getattr(result, "waiting", False))

    def _come_down(self, tm: AgentInstance, finished) -> None:
        """run 에이전트가 과제를 마쳤다 — 되살릴 대상이 아니고(채널은 회색으로 남는다),
        부른 쪽이 건 ``on_task_end``(OnAgentEnd 훅)를 마지막 런의 결과로 한 번 부른다.
        상태(dead)·저장·로스터는 워커의 정리(`finally`)가 한다."""
        tm.revivable = False
        tm.handled = max(tm.handled, 1)
        hook, tm.on_task_end = tm.on_task_end, None
        if hook is not None:
            result = finished[0] if isinstance(finished, tuple) else finished
            try:
                hook(result)
            except Exception:
                pass  # 훅은 best-effort — 내려오는 길을 막지 않는다

    @staticmethod
    def _is_human_direct(item) -> bool:
        """사람-직접(웹/@agt 대화창) 요청인가 — ``user:*`` 발신 + 회신 기대.
        이런 요청만 배치 대상(C-1, 회신→대화창 ⑥). main 위임(→mailbox)·peer
        (→requester)·배달된 peer 회신(expects_reply=False)은 제외."""
        return (
            isinstance(item, dict)
            and str(item.get("author", "")).startswith("user:")
            and item.get("expects_reply", True)
        )

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
    "\n⚠ it now has {n} queued requests — if these are progress checks or re-sends "
    "of the same ask, that is interference: each one queues more work and delays "
    "the answer. Its `reply` reaches you on its own; stop re-sending."
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
