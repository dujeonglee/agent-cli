"""teammate — 상주 세션 에이전트 (P1 코어, docs/teammate/DESIGN.md).

delegate 가 "파견"(스폰→완주→ctx 폐기)이라면 teammate 는 "상주 팀원"이다:
spawn 이 key 를 반환하고, 이후 request 가 같은 ctx 위에서 반복 처리된다
(D1 비동기 mailbox). 회신은 LLM 폴링이 아니라 **harness 가 배달**한다 —
main 루프가 턴 경계에서 :meth:`AgentRegistry.drain_replies` 를 비워
관찰 레코드로 주입한다 (D2, ``AgentLoop._deliver_agent_mail``).

스레딩 모델
-----------
teammate 하나 = 데몬 worker 스레드 하나. worker 는 자기 inbox 를 블록해
메시지당 :func:`~agent_cli.subagent.runner.run_subagent_message` 1회를
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
from queue import Empty, SimpleQueue
from typing import TYPE_CHECKING

from agent_cli.tools.result import ToolResult

if TYPE_CHECKING:
    from agent_cli.context.manager import ContextManager

# worker 를 inbox 블록에서 깨워 종료시키는 sentinel (identity 비교).
_SHUTDOWN = object()

# 사람-직접 요청이 한 턴에 여러 건 배치될 때(C-1) 앞에 붙는 안내 — main 의
# QUEUED_REQUEST_NOTICE 대응(에이전트가 최신 것만 답하고 나머지를 흘리지 않게).
_AGENT_BATCH_NOTICE = (
    "(여러 메시지가 함께 도착했습니다 — 아래 요청/메시지 전부에 응답하세요. "
    "최신 것만 답하고 이전 것을 건너뛰지 마세요.)"
)

_DEFAULT_MAX_AGENTS = 10
MAX_AGENTS_MIN = 1  # smallest positive cap; 0 (or less) means unlimited


def default_max_agents() -> int:
    """부팅 기본 — env AGENT_CLI_MAX_AGENTS 오버라이드 (v8.61.0).
    ``0`` = 무제한(clamp 의 sentinel 그대로). 종전엔 웹 노브 전용이라
    headless 에서 동시 에이전트 수를 지정할 수단이 없었다."""
    import os

    raw = os.environ.get("AGENT_CLI_MAX_AGENTS", "")
    if not raw:
        return _DEFAULT_MAX_AGENTS
    return clamp_max_agents(raw)


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


# ── 비동기 ask/answer (docs/agent-ask/DESIGN.md 3판) ──────────────
#
# 질문은 **아무것도 블록하지 않는다**: ``ask`` 가 여기에 등록하고 즉시
# 반환하며, ``answer(id, text)`` 가 id 로 짝지어 배달한다. 강제는 "빚이
# 남으면 런이 안 끝난다"로 하되 루프를 도는 주체에게만 건다.
#
# 불변식(§0): 질문의 주소 = ask 시점의 current_author = 원 요청자.
# 답할 수 있는 주체는 그 주소, 오직 그것. ∴ 답한 주체는 항상 원 요청자라
# 답 배달에 ``author=q.target`` 만 쓰면 기존 회신 라우팅이 제자리로 보낸다.
# 단 ``user*`` 는 하나의 주체로 본다 — CLI 는 ``user``, 웹은
# ``user:{nickname}`` 이고 뷰어가 여럿이면 닉이 다르다.

# 빚을 진 채 complete 을 시도할 수 있는 횟수 — 초과하면 "(답변 없음)" 으로
# 닫고 런을 정상 종료시킨다. 사람 주소 질문에는 적용하지 않는다(§3.5).
_MAX_QUESTION_NAGS = 6

# 런이 끝났는데 답 안 한 질문이 있을 때 다시 거는 항목의 머리말.
_OWED_REMINDER = (
    "(reminder) These question(s) are still waiting for your answer. Answer "
    "each with the `answer` tool (id, text). Nothing is blocked on you — the "
    "asker kept working — but they cannot finish the part that depends on it. "
    "If you genuinely cannot answer, say so with `answer` rather than ignoring."
)


def _is_human_addr(addr: str) -> bool:
    """``user`` / ``user:<nick>`` — 사람 주소인가 (§0: 하나의 주체)."""
    return addr == "user" or addr.startswith("user:")


@dataclass
class Question:
    """열린 질문 하나 — ``AgentRegistry._questions`` 의 값.

    **두 seq 가 런 스코프를 만든다.** 워커는 inbox 항목 1개 = 런 1개이고,
    질문은 등록 즉시 목록에 오르지만 상대 inbox 에서는 **줄을 선다**. 이
    비대칭을 안 보면 (a) 상대가 아직 꺼내지도 않은 질문에 강제·sweep 이
    걸리고 (b) 다른 런에서 걸어 둔(영영 열릴 수 있는) 사람 질문이 이후 모든
    회신을 막는다. 그래서 강제·sweep 은 ``delivered_seq``, 회신 억제는
    ``asked_seq`` 를 본다 (DESIGN.md §3.2).
    """

    id: str
    asker: str  # "main" | "<agent key>"  — 답이 돌아갈 곳
    target: str  # "main" | "agent:<key>" | "user" | "user:<nick>"
    text: str
    asked_at: float = field(default_factory=time.time)
    asked_seq: int = 0  # asker 가 이 질문을 건 런의 inbox seq (main 은 0)
    delivered_seq: int | None = None  # target 이 꺼낸 런의 seq (미배달 None)
    nags: int = 0

    @property
    def to_human(self) -> bool:
        return _is_human_addr(self.target)

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
                # 물어본 런은 사라졌다 — 0 은 어떤 런과도 안 겹친다(seq 는
                # 1부터). §3.7 억제는 이 세션의 런에만 적용된다.
                asked_seq=0,
                # 배달 여부는 **유지**한다 — 상대의 ctx 에 질문이 남아 있고
                # (배달 = 런 1회 = 그 텍스트가 history 에 있다), 그 빚이
                # 이어져야 독촉도 상한도 계속 돈다.
                delivered_seq=(
                    None if d.get("delivered_seq") is None else int(d["delivered_seq"])
                ),
                nags=int(d.get("nags") or 0),
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
            "asked_seq": self.asked_seq,
            "delivered_seq": self.delivered_seq,
            "nags": self.nags,
        }


def _new_question_id() -> str:
    return f"q-{uuid.uuid4().hex[:6]}"


# agents.json 스키마 버전 — 비호환 변경 시 bump (구버전 파일은 무시=fresh).
AGENTS_STATE_VERSION = 1

# ── Live Teammates 시스템 프롬프트 리로드 플래그 ──
# 멤버십(spawn/kill/died/restore/auto_spawn) 변화 시 set — 루프의
# _execute_turn 이 directives/memory 플래그와 같은 자리에서 consume 해
# 다음 턴 시스템 프롬프트를 재조립한다 (상태 전이 busy/idle 은 안 건드림
# — KV 캐시 프리픽스를 의미 있는 사건에만 버스트).

# ── main registry 프로세스 슬롯 (v7.17.0 배선 통일) ──
# agent-cli 프로세스 = 세션 1개(web 인스턴스는 post 당 1 프로세스, run 은
# 단명) 전제의 "이 세션의 main AgentRegistry" 슬롯. run/web 이 생성 직후
# 등록하고, skill 실행(execute_skill)이 registry 를 명시받지 못한 경로
# (사용자 /skill dispatch 등 — main 의 워크플로우)에서 자동 상속한다.
# 종전엔 registry 가 호출자 파라미터로 손에서 손으로 릴레이돼, 경로가
# 늘 때마다 배선이 누락됐다(loop 내부 run_skill op 만 배선되고 사용자
# 슬래시 경로 2곳이 빠져 spawn 이 "main-session only" 거부되던 사고).
# 권한 경계는 "미지정 vs 명시 None" 구분이 지킨다 — 서브에이전트 루프의
# dispatch 는 cfg.agent_registry(=None)를 **명시** 전달하므로 슬롯을
# 조회하지 않고 run-only 가 유지된다.
_MAIN_REGISTRY: AgentRegistry | None = None


def set_main_registry(registry: AgentRegistry | None) -> None:
    """run/web 이 main 세션의 registry 생성 직후 등록."""
    global _MAIN_REGISTRY
    _MAIN_REGISTRY = registry


def main_registry() -> AgentRegistry | None:
    """이 프로세스의 main registry (미등록이면 None)."""
    return _MAIN_REGISTRY


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


def format_agent_label(key: str, profile: str = "", name: str = "") -> str:
    """표시 라벨 — 다중 인스턴스 구분: "agt-x (code-writer · ui)"."""
    parts = [p for p in (profile, name) if p]
    return f"{key} ({' · '.join(parts)})" if parts else key


def build_reply_record(reply: dict, *, cap: int = 0, registry=None) -> dict:
    """mailbox 아이템 1건(회신 또는 질문) → main ctx 에 넣을 관찰 레코드.

    ``kind:"question"`` (P2, ask→main 라우팅): teammate 가 ask 로 물은
    질문 — teammate 는 답변까지 블록되므로 request 로 답하라는 안내를
    붙인다. 그 외(kind:"reply"/부재)는 회신.

    ``cap``(loop 의 ``_oversized_cap``, 0=무제한) 초과 회신은 전문 대신
    디스크 포인터 + head 발췌로 치환 — 전문은 worker 가 이미
    ``teammates/<key>/replies/reply-<seq>.md`` 에 영속했다(배달과 무관하게
    항상 저장 — P3 resume 미배달 보존의 토대).

    ``registry`` (v7.11.0, 배달 시점에만 전달): 회신 레코드 말미에 그
    에이전트의 **배달-시점** 잔여 상태 한 줄을 동봉 — main 이 "얼마나
    밀렸는지" 보고 다음 요청/대기를 판단한다. 잔여가 있으면 남은 회신도
    자동 배달됨을 같이 안내(status 폴링 넛지와 정합 — 이 줄이 폴링을
    유발하면 안 됨). question/died/peer 레코드에는 붙이지 않는다.
    """
    from agent_cli.context.token_estimator import estimate_tokens

    key = reply.get("key", "")
    label = format_agent_label(key, reply.get("profile", ""), reply.get("name", ""))

    if reply.get("kind") == "died":
        # worker 사망 통지 (Q4): kill/세션종료가 아닌 비정상 종료 — main 이
        # status 를 조회하기 전에 능동적으로 알린다.
        reason = reply.get("output") or "worker terminated unexpectedly"
        content = (
            f"── agent {label} DIED ──\n{reason}\n"
            f"(Its queued requests were lost, but its context is kept — bring "
            f'it back with {{"mode":"resume","key":"{key}"}} to continue with '
            f"full memory, or spawn a new one. If it died from a crash, the "
            f"same cause may recur.)"
        )
        return {
            "role": "user",
            "tool": "agent",
            "success": False,
            "content": content,
            "source": "agent_died",
        }

    if reply.get("kind") == "question":
        # 비동기 질문(§3): 상대는 **막혀 있지 않다**. 새 request 를 보내면
        # 그건 답이 아니라 일감이라 질문은 열린 채 남고, 독촉이 상한까지
        # 돌다 닫힌다 — 런만 태운다.
        question = reply.get("output") or "(empty question)"
        tail = (
            "(The agent is NOT blocked and kept working. Answer it with the "
            f'`answer` tool: answer(id="{reply.get("id", "")}", text="..."). '
            "A new request is not an answer.)"
        )
        content = f"── agent {label} QUESTION ──\n{question}\n{tail}"
        return {
            "role": "user",
            "tool": "agent",
            "success": True,
            "content": content,
            "source": "agent_question",
        }

    if reply.get("kind") == "reminder":
        # 미답 질문 독촉 (§3.4) — 상주 에이전트가 inbox 항목으로 받는 것을
        # main 은 메일박스로 받는다. 새 일감이 아니라 빚 통지다.
        return {
            "role": "user",
            "tool": "agent",
            "success": True,
            "content": reply.get("output") or "(empty reminder)",
            "source": "agent_reminder",
        }

    if reply.get("kind") == "answer":
        # main 이 건 질문의 답 — main 에는 inbox 가 없어 메일박스가 유일한
        # 흡수 지점이다(``submit`` 의 대상은 상주 에이전트뿐).
        body = reply.get("output") or "(empty answer)"
        content = (
            f"── answer from {label} ──\n{body}\n"
            "(This answers a question you asked. Continue the work it was "
            "blocking.)"
        )
        return {
            "role": "user",
            "tool": "agent",
            "success": True,
            "content": content,
            "source": "agent_answer",
        }

    if reply.get("kind") == "peer_message":
        # 상주 에이전트가 main 에게 먼저 보낸 메시지 (v5.11) — 회신 대기
        # 아님. main 은 필요하면 agent request 로 답한다.
        msg = reply.get("output") or "(empty message)"
        content = (
            f"── agent {label} message ──\n{msg}\n"
            f"(This agent messaged you directly. Reply if useful with "
            f'{{"mode":"request","key":"{key}","task":"..."}} — otherwise '
            f"just continue.)"
        )
        return {
            "role": "user",
            "tool": "agent",
            "success": True,
            "content": content,
            "source": "agent_message",
        }

    body = reply.get("output") or "(empty reply)"

    tokens = estimate_tokens(body)
    if cap and tokens > cap:
        path = reply.get("reply_path", "")
        head = body[: cap * 2]  # ~cap/2 tokens 어치만 발췌 (chars≈tokens*4)
        body = (
            f"(reply is ~{tokens} tokens — over the {cap}-token cap; "
            f"full text saved to '{path}'. Read a specific range or search "
            f"it, or send a narrower request.)\n"
            f"--- head excerpt ---\n{head}"
        )

    status = "success" if reply.get("success") else "error"
    content = f"── agent {label} reply ({status}) ──\n{body}"
    tm = registry.get(key) if registry is not None else None
    if tm is not None and getattr(tm, "state", "dead") != "dead":
        queued = tm.inbox.qsize()
        if queued > 0 or AgentRegistry.state_is_active(tm.state):
            tail = (
                f"(agent status: {tm.state} · {queued} queued — remaining "
                "replies arrive automatically; no need to poll.)"
            )
        else:
            tail = "(agent status: idle — ready for new requests.)"
        content = f"{content}\n{tail}"
    return {
        "role": "user",
        "tool": "agent",
        "success": bool(reply.get("success")),
        "content": content,
        # additive 마킹 — tool="" (형식-개입 레거시) 오인 금지 계약과 짝.
        "source": "agent_reply",
    }


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
    ):
        self.key = key
        self.profile_name = profile_name
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

        self.inbox: SimpleQueue = SimpleQueue()
        self.stop_event = threading.Event()
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
        self.handled = 0  # 처리 완료한 request 수
        self.queued = 0  # inbox 에 넣은 request 수 (seq 발급)
        # 지금 처리 중인 항목의 seq — 질문의 런 스코프(Question.asked_seq)가
        # 이 값을 찍는다. 유휴/main 은 0.
        self.current_seq = 0
        # 이 런이 질문을 **걸었는가** (§3.7). 걸었다면 그 질문은 반드시 답
        # 런을 하나 만들고(답·상한·상대 사망 셋 다 `_deliver_answer` 를
        # 지난다) 그 런의 회신이 진짜 회신이므로, 이 런의 회신은 요청자에게
        # 재주입하지 않는다. 워커가 런 경계마다 리셋한다.
        self.asked_this_run = False

    def snapshot(self) -> dict:
        """status 표시용 스냅샷 (락 없는 근사값 — 표시 용도)."""
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
            "state": self.state,
            "handled": self.handled,
            "pending_requests": self.inbox.qsize(),
            "est_tokens": est_tokens,
            "error": self.error,
        }


class QuestionPort:
    """루프가 질문 목록에 닿는 **유일한 seam** (DESIGN.md §4).

    콜러블 셋을 ``LoopConfig``·``AgentLoop.__init__``·``run_loop``·
    ``run_subagent_message``·``_run_message`` 에 각각 꿰면 15군데가 된다 —
    객체 하나로 묶는다. **레지스트리 자체는 절대 넘기지 않는다**:
    ``LoopConfig.agent_registry`` 가 "teammate 안 teammate 금지"의 단일
    가드(``loop/state.py``)이므로 서브루프에 닿으면 안 된다.

    표면은 ``ask``/``answer`` **둘뿐**이다. 독촉은 하네스가 런 경계에서
    걸므로(§3.4) 루프가 미답 목록을 조회할 일이 없다 — 조회 메서드를 두면
    호출자 없는 표면이 된다.

    main 도 같은 포트를 받는다(``key=None``) — 안 그러면 main 에 답변
    수단이 없다. 단 ``ask`` 는 거부한다(아래).
    """

    def __init__(self, registry: AgentRegistry, key: str | None = None):
        self._reg = registry
        self.key = key
        # 이 주체의 **주소**(질문의 target 과 비교) / **발신 라벨**(asker).
        self.me = "main" if key is None else f"agent:{key}"
        self.asker = "main" if key is None else key

    @property
    def nonblocking(self) -> bool:
        """이 주체의 ``ask`` 가 막지 않는가 — 상주 에이전트만 True.

        main 의 ``ask`` 는 사람에게 묻는 기존 블로킹 경로 그대로다(§8-④).
        디스패치와 시스템 프롬프트가 같은 이 값을 본다.
        """
        return self.key is not None

    def ask(self, text: str) -> tuple[str, str]:
        """질문 등록 + 배달. ``(id, err)`` — err 가 비면 성공.

        **main 은 거부한다.** main 의 ``ask`` 는 사람에게 묻는 기존 블로킹
        경로(`renderer.prompt_user`) 그대로다. 여기로 오면 주소가 `user` 인
        질문이 생기는데, 그것은 로스터에도 창에도 안 뜨고(둘 다 asker 를
        에이전트 키로 찾는다) `open_human_questions()` 에만 남아
        ``any_activity()`` 를 영구 True 로 만든다 — **보이지도, 답할 수도,
        사라지지도 않는 질문**이다. resume 이 질문을 되살리면 asker(main)도
        target(user)도 '항상 살아있음' 이라 매 세션 부활한다.
        """
        if self.key is None:
            return "", (
                "main asks the operator through the blocking `ask` prompt, "
                "not through the question list"
            )
        tm = self._reg.get(self.key)
        # ask 시점의 ``current_author`` = 원 요청자 = 질문의 주소 (§0).
        target = tm.current_author if tm is not None else "main"
        return self._reg.register_question(self.asker, target, text)

    def answer(self, qid: str, text: str) -> str:
        """``qid`` 에 답한다. 에러 메시지 또는 빈 문자열."""
        return self._reg.answer_question(qid, text, by=self.me)


class AgentRegistry:
    """main 루프 수명의 teammate 소유자 + 회신 mailbox.

    ``runner`` 는 :func:`run_subagent_message` 기본값 — 테스트가 가짜
    러너를 주입하는 DI seam (스레딩·상태 전이를 LLM 없이 검증).
    """

    def __init__(
        self,
        session_dir: Path | None,
        *,
        runtime: dict | None = None,
        runner: Callable | None = None,
        max_agents: int | None = None,
    ):
        # 회신을 처리할 provider/모델 등 실행 배선 — spawn 시점이 아니라
        # 레지스트리 생성 시점(부트스트랩)에 고정할 수도 있으나, provider
        # 는 tool_bridge 인터셉트에서만 완전하므로 spawn 마다 갱신 수용.
        self.runtime = runtime or {}
        self._runner = runner
        self.session_dir = Path(session_dir) if session_dir else None
        # 동시 생존 상한 (세션 한정, web UI 조절). 0 = 무제한.
        # None = 미지정 → env(AGENT_CLI_MAX_AGENTS) 또는 기본값 (v8.61.0).
        self.max_agents = (
            default_max_agents() if max_agents is None else clamp_max_agents(max_agents)
        )

        self._agents: dict[str, AgentInstance] = {}
        self._cv = threading.Condition()
        self._pending: list[dict] = []  # 미배달 회신 (도착 순서)
        # 회신 도착 알림 (CLI 📨 라인 / web transient status) — 부트스트랩 주입.
        self.on_reply: Callable[[dict], None] | None = None
        # 귀속 승계 (v8.5.0): 현재 런이 서비스 중인 USER 들 — worker 가 런
        # 시작에, main 루프가 조향 주입마다 갱신. main 발신 request 가 이
        # 스냅샷을 아이템에 실어 보내고, 그 회신이 그대로 되가져와 회신을
        # 소비한 런의 ``answers`` 에 합류한다(🤝 웨이크 런 포함). 시간이
        # 아니라 요청↔회신 쌍에 묶이므로 인터리빙에 안전.
        self._current_run_authors: list[str] = []
        # 열린 질문 (비동기 ask/answer, DESIGN.md §3) — id → Question.
        # ``_pending`` 과 같은 규율: 모든 접근은 ``_cv`` 아래. 답 claim 이
        # 원자적이어야 동시 답변자 둘 중 하나만 답이 된다.
        self._questions: dict[str, Question] = {}
        # resume 이 버린 열린 질문 수 — 부트스트랩이 사람에게 알린다(§3.9).
        self.stale_questions = 0

    # ── 조회 ────────────────────────────────────

    def set_current_run_authors(self, authors: list[str]) -> None:
        """현재 런의 요청자 목록 갱신 (귀속 승계 — ``_current_run_authors``
        주석 참조). worker(런 시작)와 main 루프(조향 주입)가 호출한다."""
        self._current_run_authors = list(authors)

    def get(self, key: str) -> AgentInstance | None:
        return self._agents.get(key)

    def roster_snapshot(self) -> list[dict]:
        # ``AgentInstance.snapshot`` 은 인스턴스 메서드라 레지스트리의
        # ``_questions``/``_cv`` 에 닿지 못한다 — 열린 질문은 여기서 합류.

        # Snapshot the values first: this runs on worker/web threads while the
        # main thread may spawn/resume/restore into ``_agents``. Iterating the
        # live view directly raises "dictionary changed size during iteration"
        # (AUDIT S-1); ``list(...)`` materialises atomically under the GIL.
        with self._cv:
            by_asker: dict[str, list[dict]] = {}
            for q in self._questions.values():
                by_asker.setdefault(q.asker, []).append(
                    {"id": q.id, "text": q.text, "to": q.target, "ts": q.asked_at}
                )
        out = []
        for tm in list(self._agents.values()):
            snap = tm.snapshot()
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

    @staticmethod
    def state_is_active(state: str) -> bool:
        """ "작업 중" 판정의 단일 소유 — worker 상태 어휘는
        starting|idle|busy|dead (AgentInstance.state). idle/dead 가 아니면
        mid-task 다(starting=곧 첫 요청 처리). ★v7.11.1: 표시/넛지/reap
        코드가 존재하지 않는 "working" 문자열을 비교하던 버그의 수리 —
        어휘는 여기서만."""
        return state not in ("idle", "dead")

    def any_activity(self) -> bool:
        """idle-reap 가드 (v7.10.0): 에이전트가 working 이거나 inbox 에
        미처리 요청이 있으면 True — main 유휴·무접속이어도 인스턴스를
        자가 종료하면 진행 중 작업이 소실되므로 IdleMonitor 의 is_active
        에 합류한다. 미배달 회신(_pending)은 게이트하지 않는다 — resume
        시 agents.json pending 미러로 복원·배달되므로 reap 안전."""
        if self.open_human_questions():
            # 사람 주소 질문은 답이 올 때까지 열려 있고 상한도 없다 —
            # 활동으로 세지 않으면 작업 중인 세션을 idle-reap 이 걷는다.
            # (§3.9 가 되살리긴 하지만 걷히는 것 자체가 사용자에겐 사고다.)
            return True
        return any(
            self.state_is_active(t.state) or t.inbox.qsize() > 0
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

    def has_pending_replies(self) -> bool:
        with self._cv:
            return bool(self._pending)

    def has_active_work(self) -> bool:
        """P5: CLI run 큐 펌프의 정지 판정 — 미배달 회신·처리 중(busy)·
        큐잉된 요청이 하나라도 있으면 True.

        비동기 질문은 아무도 막지 않으므로(§3) 여기서 뺄 상태가 없다 —
        질문을 건 에이전트도 계속 돌고, 답은 새 항목으로 온다."""
        if self.has_pending_replies():
            return True
        return any(
            tm.state == "busy" or tm.inbox.qsize() > 0
            for tm in list(self._agents.values())
        )

    def open_human_question_keys(self) -> list[str]:
        """사람 답을 기다리는 열린 질문의 asker — 종료 시 경고 표시용."""
        return sorted({q.asker for q in self.open_human_questions()})

    # ── 질문 목록 (비동기 ask/answer, DESIGN.md §3) ──────────────

    def question_port(self, key: str | None = None) -> QuestionPort:
        """루프에 넘길 seam. ``key=None`` 이면 main 용."""
        return QuestionPort(self, key)

    def register_question(self, asker: str, target: str, text: str) -> tuple[str, str]:
        """질문 등록 + 배달 — ``(id, err)``. 아무것도 블록하지 않는다."""
        text = (text or "").strip()
        if not text:
            return "", "empty question"
        asked_seq = 0
        if asker != "main":
            tm = self._agents.get(asker)
            if tm is None:
                return "", f"unknown asker '{asker}'"
            asked_seq = tm.current_seq
        with self._cv:
            # 중복 접기 — **같은 런 안에서만**. ``_op_ask`` 는 루프 탐지기
            # 앞에서 반환하므로(dispatch.py) 한 런이 같은 질문을 반복해도
            # 구조적으로 안 잡힌다. 그게 이 장치가 있는 이유고, 런 경계를
            # 넘으면 접으면 안 된다: 억제된 런마다 자기 답 런이 하나씩
            # 있어야 회신이 안 사라진다(§3.7). 접어 버리면 요청 둘에
            # 답 런 하나 → 회신 하나 — 낡은 회신보다 나쁜 **누락**이다.
            for q in self._questions.values():
                if (
                    q.asker == asker
                    and q.target == target
                    and q.text == text
                    and q.asked_seq == asked_seq
                ):
                    self._mark_asked(asker)
                    return q.id, ""
            q = Question(
                id=_new_question_id(),
                asker=asker,
                target=target,
                text=text,
                asked_seq=asked_seq,
            )
            self._questions[q.id] = q
        err = self._deliver_question(q)
        if err:
            # 배달 실패(상대 dead/unknown)면 **등록도 취소**한다 — 남기면
            # 아무도 답할 수 없는 빚이 asker 를 영원히 붙잡는다.
            with self._cv:
                self._questions.pop(q.id, None)
            return "", err
        # 배달까지 성공한 **뒤에** 세운다 — 실패한 질문에 억제를 걸면
        # 답 런이 안 생기므로 그 런의 회신이 영영 사라진다.
        self._mark_asked(asker)
        self._save_state()
        # 트레이는 로스터의 ``open_questions`` 를 읽는다 — 알리지 않으면
        # 사람 주소 질문이 다음 로스터 브로드캐스트까지 화면에 안 뜬다.
        self._notify_roster()
        return q.id, ""

    def _mark_asked(self, asker: str) -> None:
        """이 런이 질문을 걸었다고 표시 (§3.7 회신 억제의 판정값)."""
        tm = self._agents.get(asker)
        if tm is not None:
            tm.asked_this_run = True

    def _deliver_question(self, q: Question, *, render: bool = True) -> str:
        """질문을 **기존 배관**으로 상대에게. 에러 또는 빈 문자열.

        상대가 idle 이어도 깨어난다 — inbox 항목 1개 = 런 1개이므로.
        그래서 ``complete`` 강제(§3.4)는 배달 수단이 아니라 "꺼내 읽고도
        안 답함" 백스톱이다.
        """
        if render:
            self._render_question(q)
        if q.to_human:
            return ""  # ❓ 트레이가 표면 — 배달할 inbox 가 없다 (§3.6)
        if q.target != "main" and not q.target.startswith("agent:"):
            return f"unroutable question target '{q.target}'"
        asker_tm = self._agents.get(q.asker)
        # ``expects_reply=False`` — 상대가 이 항목을 처리한 **산출물**이
        # asker 에게 되돌아가면 안 된다. 답은 ``answer`` 도구로만.
        return self.deliver(
            q.target,
            mail={
                "kind": "question",
                "id": q.id,  # main 이 answer(id) 하려면 실려야 한다
                "key": q.asker,
                "profile": asker_tm.profile_name if asker_tm else "",
                "name": asker_tm.instance_name if asker_tm else "",
                "success": True,
                "output": q.text,
            },
            text=f"[question {q.id} from {q.asker}]: {q.text}",
            author="main" if q.asker == "main" else f"agent:{q.asker}",
            expects_reply=False,
            question_id=q.id,
        )

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

    def mark_question_delivered(self, qid: str, seq: int) -> None:
        """상대가 이 질문을 **꺼냈다** — 강제·sweep 은 여기부터 유효하다."""
        with self._cv:
            q = self._questions.get(qid)
            if q is not None and q.delivered_seq is None:
                q.delivered_seq = seq

    def questions_owed_by(self, addr: str) -> list[Question]:
        """``addr`` 이 답해야 하는 질문 — **배달된 것만** (§3.2 런 스코프)."""
        with self._cv:
            return [
                q
                for q in self._questions.values()
                if q.target == addr and q.delivered_seq is not None
            ]

    def questions_asked_in(self, asker: str, seq: int) -> list[Question]:
        """``asker`` 가 ``seq`` 런에서 건 열린 질문 (§3.7 회신 억제)."""
        with self._cv:
            return [
                q
                for q in self._questions.values()
                if q.asker == asker and q.asked_seq == seq
            ]

    def open_human_questions(self) -> list[Question]:
        """주소가 사람인 열린 질문 — ❓ 트레이와 idle-reap 가드가 읽는다."""
        with self._cv:
            return [q for q in self._questions.values() if q.to_human]

    def answer_question(self, qid: str, text: str, *, by: str) -> str:
        """``qid`` 에 답한다 — 에러 또는 빈 문자열.

        **답할 수 있는 주체는 질문의 주소, 오직 그것**(§0). 단 ``user*`` 는
        하나의 주체로 본다 — CLI 는 ``user``, 웹은 ``user:{nick}`` 이고
        뷰어마다 닉이 달라, 문자열 동치로 검사하면 두 번째 뷰어의 트레이
        답이 거부된다.
        """
        text = (text or "").strip()
        if not text:
            return "empty answer"
        with self._cv:
            q = self._questions.get(qid)
            if q is None:
                return f"unknown or already-answered question '{qid}'"
            if q.to_human:
                if not _is_human_addr(by):
                    return f"question '{qid}' is addressed to the operator, not {by}"
            elif by != q.target:
                return f"question '{qid}' is addressed to {q.target}, not {by}"
            del self._questions[qid]  # 원자적 claim — 동시 답변자 중 하나만
        self._deliver_answer(q, text)
        self._save_state()
        return ""

    def close_question(self, qid: str, reason: str) -> Question | None:
        """답 없이 닫는다 (sweep·사망·상한) — asker 에게 사유를 배달."""
        with self._cv:
            q = self._questions.pop(qid, None)
        if q is None:
            return None
        self._deliver_answer(q, f"({reason})")
        self._save_state()
        return q

    def bump_question_nag(self, qid: str) -> int:
        """빚을 진 채 complete 시도 — 누적 횟수 반환."""
        with self._cv:
            q = self._questions.get(qid)
            if q is None:
                return 0
            q.nags += 1
            return q.nags

    def remind_owed(self, addr: str) -> int:
        """런이 끝났는데 답 안 한 질문이 있으면 **독촉을 하나 건다**.

        ``addr`` 은 질문의 주소 어휘 그대로 — ``"main"`` 또는
        ``"agent:<key>"``. 계산·상한·닫기는 둘이 완전히 같고, 다른 것은
        배달 한 줄뿐이다(에이전트는 inbox 항목, main 은 메일박스 —
        ``_deliver_question`` 과 같은 비대칭).

        ``complete`` 을 붙잡지 않는다 (DESIGN.md §3.4). 붙잡으면 그 런에
        일을 시킨 쪽이 **자기와 무관한 질문이 풀릴 때까지** 결과를 못 받는다
        — 없애려던 결합이 그대로 돌아온다. 대신 결과는 그대로 나가고,
        남은 빚은 **새 항목 = 새 런**으로 다시 온다. 답이 오는 경로와
        정확히 같은 기계라 ``dispatch.py`` 는 한 줄도 안 바뀐다.

        스코프 축은 **배달 여부**(``delivered_seq is not None``)지 seq 동치가
        아니다. 아직 큐에 서 있는 질문은 제외되지만(그 런이 읽지도 않은
        것으로 독촉하면 안 된다), 한 번 읽은 빚은 **답할 때까지 매 런 끝에**
        다시 온다 — seq 동치로 좁히면 독촉이 딱 한 번 나가고 끝나 상한조차
        영영 안 걸린다.

        **독촉 런 끝에서도 독촉한다.** 한때 ``item["reminder"]`` 로 그걸
        막았는데, 그 차단이 만든 정지가 훨씬 나빴다: 독촉 1회 뒤 그 에이전트
        에게 일이 안 오면 ``nags`` 가 1에 멈춰 상한이 영영 안 걸리고 질문이
        영원히 열린다. 차단의 근거였던 "수 밀리초에 상한 6이 탄다"는 **가짜
        러너가 즉시 반환하기 때문**이고, 실제로는 독촉 런 하나가 질문을
        컨텍스트에 놓고 도는 진짜 LLM 턴이다. 게다가 inbox 는 FIFO 라 독촉이
        큐 뒤에 붙어 실제 일감을 굶기지 않는다.

        상한(``_MAX_QUESTION_NAGS``)을 넘으면 사유와 함께 닫는다 — 모델이
        끝내 안 답해도 asker 가 영원히 기다리지는 않는다. 반환값은 건 독촉
        수(0 이면 빚 없음).
        """
        owed = self.questions_owed_by(addr)
        if not owed:
            return 0
        live = []
        for q in owed:
            n = self.bump_question_nag(q.id)
            if n == 0:
                # 스냅샷과 bump 사이에 답이 들어왔다 — 0 은 "그런 질문 없음"
                # 이고 살아 있는 질문은 언제나 ≥1 이라 모호하지 않다.
                continue
            if n > _MAX_QUESTION_NAGS:
                self.close_question(q.id, "no answer after repeated reminders")
            else:
                live.append(q)
        if not live:
            return 0
        body = (
            _OWED_REMINDER
            + "\n"
            + "\n".join(f"  [{q.id}] (from {q.asker}) {q.text}" for q in live)
        )
        self.deliver(
            addr,
            mail={
                "kind": "reminder",
                "key": live[0].asker,
                "success": True,
                "output": body,
            },
            text=body,
            # 발신자는 **기다리는 쪽**(asker)으로 — ``addr`` 은 이 런의
            # 주인 자신이라 창에서 "자기가 자기에게" 로 읽힌다. 여럿이
            # 기다리면 대표로 첫 asker 를 쓰되, 줄마다 누가 물었는지 적는다.
            author="main" if live[0].asker == "main" else f"agent:{live[0].asker}",
            expects_reply=False,  # 독촉의 산출물은 어디로도 가지 않는다
        )
        return len(live)

    def _deliver_answer(self, q: Question, text: str) -> None:
        """답을 **원 요청자**에게. 주소가 곧 원 요청자라 분기가 필요 없다.

        ``author=q.target`` + ``expects_reply=True`` 면 기존 회신 라우팅
        (``_handle_request``)이 답 런의 결과를 제자리로 보낸다 — peer 면
        ``_deliver_peer_reply``, main 이면 메일박스.
        """
        body = f"[answer to your question: {q.text}]\n{text}"
        # main 에는 inbox 가 없다 — 메일박스로. (설계 3판 §3.3 은 이
        # 경우를 빠뜨렸다: ``submit`` 의 대상은 상주 에이전트뿐이다.)
        label = q.target.split(":", 1)[1] if q.target.startswith("agent:") else q.target
        self.deliver(
            q.asker if q.asker == "main" else f"agent:{q.asker}",
            mail={
                "kind": "answer",
                "id": q.id,
                "key": label,
                "success": True,
                "output": body,
            },
            text=body,
            author=q.target,
            expects_reply=True,
        )

    def _purge_questions_for(self, key: str) -> None:
        """에이전트 사망 정리 — **양방향** (§3.8).

        앞으로 온 질문은 "(종료됨)" 으로 닫아 asker 를 풀어주고, 그가 건
        질문은 폐기한다(남기면 답하려는 쪽이 dead 에러를 받고 재시도하며
        상한만 태운다). 호출자가 ``not revivable`` 아래에서만 부른다 —
        세션 종료(``shutdown_all``)에서 지우면 직후의 ``_save_state`` 가
        빈 목록을 저장해 resume 알림이 항상 0건이 된다.
        """
        addr = f"agent:{key}"
        with self._cv:
            incoming = [q.id for q in self._questions.values() if q.target == addr]
            outgoing = [q.id for q in self._questions.values() if q.asker == key]
            for qid in outgoing:
                self._questions.pop(qid, None)
        for qid in incoming:
            self.close_question(qid, f"agent {key} terminated before answering")
        if outgoing:
            self._save_state()

    # ── spawn ───────────────────────────────────

    def _model_unavailable(self, model: str, profile: str) -> str:
        """역할이 요구한 모델이 서버에 없으면 사람이 읽을 이유를, 아니면 ""."""
        from agent_cli.model_check import ModelNotFound, NoModelSelected, verify_model

        rt = self.runtime or {}
        base_url = rt.get("base_url", "")
        if not base_url:
            return ""  # 확인할 수단이 없으면 막지 않는다 (부팅 정책과 동일)
        try:
            verify_model(
                model,
                base_url,
                rt.get("api_key", ""),
                rt.get("provider_name", "openai") or "openai",
            )
        except ModelNotFound as e:
            near = e.listing.suggest(model)
            hint = f" (비슷한 이름: {near})" if near else ""
            avail = ", ".join(e.listing.models[:6])
            where = f"프로파일 '{profile}'" if profile else "역할 설정"
            return (
                f"{where} 이(가) 요구한 모델 '{model}' 이(가) 서버에 없습니다"
                f"{hint}. 사용 가능: {avail}"
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
            if (tm.profile_name, tm.instance_name) != (profile, name):
                continue
            label = " · ".join(p for p in (profile, name) if p)
            if tm.state == "dead":
                return (
                    f"'{label}' already exists (dead) as {tm.key} — resume it with "
                    f'{{"mode":"resume","key":"{tm.key}"}} instead of spawning a '
                    f"duplicate, or spawn with a distinct `name`."
                )
            return (
                f"'{label}' is already running as {tm.key} — send it work with "
                f'{{"mode":"request","key":"{tm.key}","task":"..."}}, or spawn with '
                f"a distinct `name` if you really need a second instance."
            )
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
    ) -> tuple[str, str]:
        """teammate 생성 — ``(key, error)``. 성공 시 error 는 빈 문자열.

        역할 로드·fork 전제 검사는 여기서 동기로 (즉시 거부), ctx 생성은
        worker 스레드에서 (인스펙터 스코프가 worker 스레드 키라서 — D9).
        """
        if runtime:
            self.runtime = runtime

        if self._at_agent_limit():
            return "", (
                f"agent limit reached ({self.max_agents} alive). "
                f'Kill one first (mode:"kill") or raise the limit in the web UI.'
            )

        if name and not re.match(r"^[a-zA-Z0-9_-]{1,24}$", name):
            return "", (f"invalid instance name '{name}': [a-zA-Z0-9_-], max 24 chars")

        dup = self._duplicate_label(profile, name)
        if dup:
            return "", dup

        role_prompt = ""
        description = ""
        config: dict = {}
        model = self.runtime.get("model", "")
        hooks_config = self.runtime.get("hooks_config")
        if profile:
            from agent_cli.subagent.profiles import load_profile
            from agent_cli.subagent.runner import apply_role_overrides

            body, config, error = load_profile(profile)
            if error:
                return "", error
            role_prompt = body or ""
            description = str(config.get("description", "") or "")
            inherited = model
            allowed_tools, model, hooks_config = apply_role_overrides(
                config,
                allowed_tools=allowed_tools,
                model=model,
                hooks_config=hooks_config,
            )
            # 역할 md 가 상속 모델을 덮어썼다면 그 이름도 확인한다 (v9.5.0).
            # 상속분은 부팅 때 이미 검증됐지만 역할이 지정한 이름은 처음 보는
            # 값이고, 안 보면 **그 에이전트의 첫 턴에 가서야** 404 로 드러난다
            # (사용자 제보: 대화 한복판의 빨간 거부 카드). spawn 을 거절하는
            # 편이 훨씬 싸고, 고칠 파일까지 지목할 수 있다.
            if model and model != inherited:
                bad = self._model_unavailable(model, profile)
                if bad:
                    return "", bad

        # instant-agent 역할 캡처 (v5.12): 프로파일 description 이 없고
        # 인라인 instructions 만 있으면 그 첫 문장을 로스터 역할 요약으로.
        # → 프로파일 없는 즉석 에이전트도 동료들에게 역할이 보인다.
        if not description and instructions:
            description = first_sentence(instructions)

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
            home_dir=self.session_dir / "agents" / key,
            instance_name=name,
            description=description,
            instructions=instructions,
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
        question_id: str = "",
    ) -> str:
        """request 큐잉 — 에러 메시지 또는 빈 문자열.

        v9.12 의 ``submit() -> (error, verdict)`` 은 ask 답변 슬롯 배달을
        구분하려던 것인데, 비동기 전환으로 슬롯이 사라져 판정할 것이 없다 —
        이름과 반환형을 원래대로 되돌렸다(답은 ``answer`` 도구로만 온다).

        ``question_id``: 이 아이템이 질문이면 그 id — 런 스코프 마킹용.

        ``expects_reply`` (v5.11): 이 아이템 처리 후 산출물을 발신자에게
        되돌릴지. main/watch/user·peer 요청=True(회신 라우팅), 배달된 peer
        회신=False(terminal — 수신자는 소비만, 재라우팅 없음 → 핑퐁 방지).
        ``hop`` 은 peer 재주입 깊이(_MAX_PEER_HOPS 안전망).
        """
        tm = self._agents.get(key)
        if tm is None:
            return f"unknown agent '{key}' (see mode:\"status\" for live keys)"
        if tm.state == "dead":
            reason = f" ({tm.error})" if tm.error else ""
            return f"agent '{key}' is dead{reason} — spawn a new one"
        if not message.strip():
            return "empty message"
        # Stamp the request's time BEFORE enqueuing. The worker runs on its own
        # thread and can dequeue + emit ``begin_agent_work`` (scope_start) the
        # instant the item lands in the inbox — i.e. before this method reaches a
        # post-``put`` ``time.time()``. Capturing here guarantees the request's
        # timestamp precedes any work it triggers, so the team view shows the
        # request ABOVE (before) the work bar, not after it.
        send_ts = time.time()
        # P0-9a: seq 발급 원자화 — main/peer/웹 스레드가 동시에 request() 하면
        # 같은 seq 가 나와 replies/reply-<seq>.md 상호 덮어쓰기 + UI dedup 키
        # 충돌(요청 화살표 드롭)이 가능했다. _cv(RLock 기반) 아래서 증가+캡처.
        with self._cv:
            tm.queued += 1
            seq = tm.queued
        item = {
            "seq": seq,
            "text": message,
            "author": author,
            "hop": hop,
            "expects_reply": expects_reply,
            # 이 항목이 **질문**이면 그 id (DESIGN.md §3.3). 상대가 항목을
            # 꺼낼 때 ``mark_question_delivered`` 가 이걸로 런 스코프를
            # 찍는다 — 표시 문자열 ``[question q-xxx …]`` 를 파싱하지
            # 않는다(문구가 계약이 되면 못 고친다).
            "question_id": question_id,
            # 발신 시각 — 스윔레인 요청 화살표(agent_msg "in", ts=send_ts)와
            # 작업 카드(begin_agent_work→scope_start)가 같은 앵커를 갖도록
            # worker 로 실어 보낸다(웹 프런트가 카드 data-nav-ts 로 사용).
            "ts": send_ts,
        }
        if author == "main":
            # 귀속 승계: 이 요청을 만든 런의 USER 들을 요청에 스냅샷 —
            # 회신이 그대로 되가져와, 회신을 접는 런(🤝 웨이크 포함)의
            # 최종답이 원 요청자에게 귀속된다. peer/user 발신은 회신이
            # main mailbox 로 안 가므로 스냅샷 불필요.
            item["answers"] = list(self._current_run_authors)
        tm.inbox.put(item)
        # 답이든 일감이든 **항상** 창·로그·로스터에 남긴다 — 답만 건너뛰면
        # 🤝 창과 conversation.jsonl(=resume 재생 소스)에서 사라진다.
        from agent_cli.render import get_renderer

        payload = {
            "key": key,
            "direction": "in",
            "author": author,
            "text": message,
            "seq": seq,  # P0-9a: 락 하에서 캡처한 값 — tm.queued 재독은 레이스
            # 수신자 = 이 에이전트. 누락 시 렌더러 기본값 "main" 이 실려
            # 의미가 뒤집히고, TeamView ingest 중복제거 키(author:to:seq:
            # direction)가 **다른 에이전트의 같은 seq 요청**과 충돌해 두
            # 번째 spawn+task 의 요청 화살표가 드롭됐다 (v8.5.1).
            "to": key,
            "ts": send_ts,
            # 표시용 신원 — CLI 는 로스터를 못 보므로 발신자가 실어 보낸다
            # (웹은 늦게-해소가 replay 에 강해 로스터를 계속 쓴다). v9.9.0
            "profile": tm.profile_name,
            "instance_name": tm.instance_name,
        }
        get_renderer().agent_message(**payload)
        self._log_conversation(tm, payload)
        self._notify_roster()
        return ""

    def deliver(
        self,
        addr: str,
        *,
        mail: dict,
        text: str,
        author: str,
        expects_reply: bool,
        question_id: str = "",
        hop: int = 0,
    ) -> str:
        """주소 하나로 배달 — 백엔드 둘. 에러 문자열 또는 "".

        ``addr`` 은 질문·답·독촉이 이미 쓰던 어휘 그대로: ``"main"`` 또는
        ``"agent:<key>"``. 종전엔 이 분기가 세 곳에 손으로 복제돼 있었다
        (``_deliver_question``/``_deliver_answer``/``remind_owed``).

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
        if addr == "main":
            self._push_reply(mail)
            return ""
        if addr.startswith("agent:"):
            return self.request(
                addr.split(":", 1)[1],
                text,
                author=author,
                expects_reply=expects_reply,
                question_id=question_id,
                hop=hop,
            )
        return f"unroutable address '{addr}'"

    def _push_reply(self, reply: dict) -> None:
        with self._cv:
            self._pending.append(reply)
            self._cv.notify_all()
        self._save_state()  # P3: 미배달분 디스크 미러 — resume 시 유실 없음
        cb = self.on_reply
        if cb is not None:
            try:
                cb(reply)
            except Exception:
                pass  # 알림은 best-effort — 배달 경로를 막지 않는다

    def _deliver_peer_reply(
        self, requester_key: str, from_key: str, output: str, hop: int
    ) -> None:
        """peer 요청의 회신을 요청자 inbox 로 terminal 재주입 (v5.11).

        ``expects_reply=False`` 로 넣어 요청자가 소비만 하고 되받아치지
        않게 한다(핑퐁 방지). 상한(_MAX_PEER_HOPS) 초과나 요청자 부재/사망
        시 조용히 드롭 — best-effort 배관(데드락은 비동기라 불가).
        """
        if hop >= _MAX_PEER_HOPS:
            return
        tgt = self._agents.get(requester_key)
        if tgt is None or tgt.state == "dead":
            return
        # 가이던스 꼬리표: 이 회신으로 작업을 이어가고, 마무리되면 요청자
        # (예: main)에 보고할 게 있으면 message, 없으면 complete 하도록 유도
        # (비동기라 "보고 단계"를 명시 안내 — build_reply_record 꼬리표와 동형).
        text = (
            f"{output}\n\n"
            "(Use this reply to continue your task. When done, if there is a "
            "result to report back to whoever requested your work (e.g. main), "
            "send it with the `message` tool; otherwise just `complete`.)"
        )
        self.request(
            requester_key,
            text,
            author=f"agent:{from_key}",
            hop=hop + 1,
            expects_reply=False,
        )

    def message_to_main(
        self, from_key: str, text: str, *, profile: str = "", name: str = ""
    ) -> None:
        """상주 에이전트 → main 메시지 (v5.11). main 은 inbox 대신 mailbox
        (_pending)로 받아 턴 경계 관찰로 본다 (peer↔main 대칭)."""
        self._push_reply(
            {
                "kind": "peer_message",
                "key": from_key,
                "profile": profile,
                "name": name,
                "success": True,
                "output": text,
            }
        )
        # ★v7.11.1 (실사고): mailbox 만 채우면 main 챗 관찰로는 보이는데
        # 발신 에이전트의 🤝 대화창·conversation.jsonl 에는 흔적이 없다
        # (재접속/resume 소실). 발신자 창에 out 방향으로 남긴다.
        self._log_outbound(from_key, text, to="main")

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
            "seq": tm.handled,
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

    def _make_message_handler(self, tm: AgentInstance):
        """상주 에이전트 서브루프의 ``message`` 도구 라우팅 훅 (v5.11).

        ``message(to, text)`` → 대상 inbox(또는 main mailbox)로 비동기
        전송하고 즉시 반환한다. 대상의 회신은 이 에이전트의 inbox 로 새
        메시지처럼 도착한다(발신자는 블록하지 않음)."""

        def handler(to: str, text: str) -> str:
            to = (to or "").strip()
            text = (text or "").strip()
            if not to:
                return "message needs a 'to' agent key (or 'main')"
            if not text:
                return "empty message — nothing sent"
            if to == tm.key:
                return "cannot message yourself"
            if to == "main":
                self.message_to_main(
                    tm.key, text, profile=tm.profile_name, name=tm.instance_name
                )
                return "delivered to main — it will see your message at its next turn."
            # Capture the send time BEFORE request() enqueues — the target's
            # worker can start (scope_start) the instant it's queued, so a later
            # timestamp on this arrow would render the request AFTER the work.
            send_ts = time.time()
            err = self.request(to, text, author=f"agent:{tm.key}", expects_reply=True)
            if err:
                return err
            self._log_outbound(tm.key, text, to=to, ts=send_ts)  # 발신자 창 out
            return (
                f"delivered to {to} — its reply arrives to you as a new message. "
                f"Keep working or complete; you'll be woken when it comes."
            )

        return handler

    def drain_replies(self) -> list[dict]:
        """미배달 회신 전량 회수 (턴 경계 배달 — D2). 도착 순서 유지.

        main 에게는 여기가 **질문의 배달 시점**이다 — 상주 에이전트의
        ``_handle_request`` 가 inbox 항목을 꺼낼 때 하는 일과 같은 자리.
        안 찍으면 ``questions_owed_by("main")`` 이 영영 비어 main 은 독촉도
        상한도 못 받고, §3.7 로 보류된 회신이 영구 정지한다. main 은 런
        seq 가 없으므로 0 으로 찍는다 (판정 축은 None 여부).
        """
        with self._cv:
            out = list(self._pending)
            self._pending.clear()
            for r in out:
                if r.get("kind") == "question" and r.get("id"):
                    q = self._questions.get(r["id"])
                    if q is not None and q.delivered_seq is None:
                        q.delivered_seq = 0
        if out:
            self._save_state()  # 배달 완료 → 디스크 미러도 소비
        return out

    # ── status / kill / 종료 ────────────────────

    def format_status(self, key: str = "") -> str:
        if key:
            tm = self._agents.get(key)
            if tm is None:
                return f"unknown agent '{key}'"
            items = [tm]
        else:
            items = list(self._agents.values())
        if not items:
            return 'no live agents. Spawn one with mode:"spawn".'
        cap = self.max_agents or "∞"
        lines = [f"agents ({self.alive_count()}/{cap} alive):"]
        for tm in items:
            s = tm.snapshot()
            role = s["profile"] or "anon"
            if s.get("name"):
                role = f"{role} · {s['name']}"
            line = (
                f"- {s['key']} [{role}] {s['state']}"
                f" | handled {s['handled']}"
                f" | inbox {s['pending_requests']}"
                f" | ctx ~{s['est_tokens']} tokens"
            )
            if s["error"]:
                line += f" | error: {s['error']}"
            if s["state"] == "dead":
                line += ' | resumable via {{"mode":"resume","key":"{}"}}'.format(
                    s["key"]
                )
            lines.append(line)
        with self._cv:
            if self._pending:
                lines.append(f"(undelivered replies: {len(self._pending)})")
        # 폴링 차단 넛지: working 이거나 미처리 inbox 가 있으면 모델이
        # status 를 돌려 기다리는 상황일 가능성이 높다 — 회신은 mail 로
        # 자동 배달되고 도착 시 harness 가 깨우므로(폴링 금지 설계),
        # complete 로 턴을 마치는 게 올바른 대기다. 전부 idle 인 로스터
        # 확인용 status 에는 붙이지 않는다(노이즈).
        waiting = any(
            self.state_is_active(tm.state) or tm.inbox.qsize() > 0 for tm in items
        )
        if waiting:
            lines.append(
                "⏳ Waiting on an agent? Its reply is delivered to you "
                "automatically as agent mail and you will be woken when it "
                "arrives — do not poll status. Finish this turn with "
                "`complete` and wait."
            )
        return "\n".join(lines)

    def kill(self, key: str) -> str:
        """종료 요청 — 성공 시 빈 문자열. 멱등 (이미 dead 여도 성공)."""
        tm = self._agents.get(key)
        if tm is None:
            return f"unknown agent '{key}'"
        tm.revivable = False  # P3: 명시 kill 은 영구 — resume 이 되살리지 않음
        tm.stop_event.set()
        tm.inbox.put(_SHUTDOWN)
        if tm.worker is not None:
            tm.worker.join(timeout=2.0)  # busy 면 다음 턴 경계에서 멈춤
        # 5.13: kill=창 정리 — 표면(replay 버퍼+라이브 창)만 비우고
        # conversation.jsonl 은 남긴다(mode:"resume" 시 _replay_conversation
        # 소스). shutdown_all(세션 종료)은 정리하지 않음 — 다음 세션 resume
        # 이 restore 재생으로 복원하게 둔다.
        from agent_cli.render import get_renderer

        get_renderer().clear_agent_conversation(key)
        self._save_state()
        self._notify_roster()
        return ""

    def shutdown_all(self) -> None:
        """세션 종료 — 전원 kill. main 부트스트랩의 finally 에서 호출."""
        for tm in list(self._agents.values()):
            tm.stop_event.set()
            tm.inbox.put(_SHUTDOWN)
        for tm in list(self._agents.values()):
            if tm.worker is not None:
                tm.worker.join(timeout=5.0)
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
            return f"unknown agent '{key}'"
        if tm.state != "dead":
            return (
                f"agent '{key}' is still alive ({tm.state}) — send it a request instead"
            )
        if self._at_agent_limit():
            return (
                f"agent limit reached ({self.max_agents} alive). "
                f'Kill one first (mode:"kill").'
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
        )
        # seq 이어가기 — 이전 생의 replies/reply-N.md 를 덮지 않는다.
        fresh.handled = tm.handled
        fresh.queued = max(tm.queued, tm.handled)
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
            tm.profile_name for tm in list(self._agents.values()) if tm.state != "dead"
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
        entries = []
        # Snapshot under the GIL — ``_save_state`` runs on worker threads while
        # the main thread mutates ``_agents``; iterating the live view raced a
        # spawn and killed an UNRELATED agent via the worker's except-handler
        # (AUDIT S-1). ``list(...)`` can't raise mid-iteration.
        for tm in list(self._agents.values()):
            entries.append(
                {
                    "key": tm.key,
                    "profile": tm.profile_name,
                    "name": tm.instance_name,
                    "description": tm.description,
                    "instructions": tm.instructions,
                    # role_prompt 를 통째로 저장 — resume 시 역할 md 파일이
                    # 지워졌어도 teammate 는 갖고 있던 역할 그대로 살아난다.
                    "role_prompt": tm.role_prompt,
                    "allowed_tools": tm.allowed_tools,
                    "model": tm.model,
                    "hooks_config": tm.hooks_config,
                    "context_mode": tm.context_mode,
                    "created_at": tm.created_at,
                    "handled": tm.handled,
                    # 논리 생사: kill(revivable=False)·에러만 영구 dead —
                    # 세션 종료로 멈춘 teammate 는 idle 로 남아 resume 대상.
                    "state": "dead" if (not tm.revivable or tm.error) else "idle",
                    "error": tm.error,
                }
            )
        with self._cv:
            pending = [dict(r) for r in self._pending]
            # 열린 질문은 resume 이 **되살린다**(§3.9) — 그러려면 남아야
            # 한다. 되살리지 못한 것만 `stale_questions` 로 알린다.
            questions = [q.as_dict() for q in self._questions.values()]
        try:
            from agent_cli.fsio import atomic_write_json

            atomic_write_json(
                path,
                {
                    "version": AGENTS_STATE_VERSION,
                    "agents": entries,
                    "pending": pending,
                    "questions": questions,
                },
            )
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
        path = self._state_path()
        if path is None or not path.is_file():
            return 0
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return 0
        if not isinstance(data, dict) or data.get("version") != AGENTS_STATE_VERSION:
            return 0

        with self._cv:
            for item in data.get("pending", []):
                if not isinstance(item, dict):
                    continue
                self._pending.append(item)
            if self._pending:
                self._cv.notify_all()
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

        revived = 0
        for e in data.get("agents", []):
            key = e.get("key")
            if not key or key in self._agents or self.session_dir is None:
                continue
            tm = AgentInstance(
                key,
                profile_name=e.get("profile", ""),
                role_prompt=e.get("role_prompt", ""),
                allowed_tools=e.get("allowed_tools"),
                model=e.get("model", ""),
                hooks_config=e.get("hooks_config"),
                context_mode=e.get("context_mode", "none"),
                home_dir=self.session_dir / "agents" / key,
                instance_name=e.get("name", ""),
                description=e.get("description", ""),
                instructions=e.get("instructions", ""),
            )
            tm.handled = int(e.get("handled", 0) or 0)
            tm.queued = tm.handled  # seq 이어가기 (reply-N.md 충돌 방지)
            if isinstance(e.get("created_at"), (int, float)):
                tm.created_at = e["created_at"]
            self._agents[key] = tm
            if e.get("state") == "dead":
                tm.state = "dead"
                tm.error = e.get("error", "")
                tm.revivable = False
                continue
            tm.revive = True
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
            revived += 1
        self._adopt_questions(restored)
        self._save_state()
        self._notify_roster()
        return revived

    def _adopt_questions(self, restored: list[Question]) -> None:
        """resume 이 복원한 질문을 받아들인다 — 에이전트 복원 **뒤에**.

        세 가지를 한다.

        **① 살릴 수 없는 것은 버린다.** asker 나 target 이 돌아오지 않았으면
        (kill 툼스톤·매니페스트 부재) 아무도 답할 수 없거나 답을 받을 데가
        없다. 버린 수는 ``stale_questions`` 로 사람에게 알린다.

        **② 미배달 peer 질문은 다시 배달한다.** inbox 는 ``SimpleQueue`` 라
        영속 대상이 아니다 — 아직 안 꺼낸 질문은 항목으로만 존재했으므로
        통째로 증발했고, 되살려 놓기만 하면 영영 안 꺼내지고 독촉도 안 간다.
        배달된 것은 상대 ctx 에 남아 있으니 재배달하지 않는다. main 앞
        질문은 ``pending`` 미러로 살아 있어 역시 그대로 둔다.

        **③ 배달됐던 빚은 한 번 깨운다(kick).** 독촉은 *런이 끝나는 자리*에
        걸리는데, resume 직후 그 에이전트에게 새 일감이 안 오면 **끝나는 런이
        없어** 독촉도 상한도 영영 안 돈다 — 연쇄 차단이 만들었던 정지가
        resume 경로로 다시 들어온다. 여기서 한 번 걸어 주면 그 독촉 항목이
        런을 만들고, 이후는 평소 흐름이다.
        """
        if not restored:
            return

        def alive(addr: str) -> bool:
            if addr == "main" or _is_human_addr(addr):
                return True
            key = addr.split(":", 1)[1] if addr.startswith("agent:") else addr
            tm = self._agents.get(key)
            return tm is not None and tm.state != "dead"

        dropped = 0
        kick: set[str] = set()
        with self._cv:
            for q in restored:
                if not (alive(q.asker) and alive(q.target)):
                    dropped += 1
                    continue
                self._questions[q.id] = q
                if q.delivered_seq is not None:
                    # kick 대상은 **재배달 전에** 확정한다. ②가 큐에 넣은
                    # 질문을 상대 워커가 곧바로 꺼내 `delivered_seq` 를 찍으면
                    # (LLM 호출 **전에** 찍힌다) ③의 집합에 섞여, 방금 배달한
                    # 질문에 독촉까지 날아간다 — 런 하나 낭비 + 상한 조기 소모.
                    kick.add(q.target)
        self.stale_questions = dropped

        for q in list(self._questions.values()):
            undelivered_peer = q.delivered_seq is None and q.target.startswith("agent:")
            # ``render=False``: 이 질문은 이미 첫 세션에서 창에 그려졌고
            # ``conversation.jsonl`` 에 남아 `_replay_conversation` 이 방금
            # 재생했다 — 다시 그리면 창에 두 번, 로그에 두 줄이 된다.
            if undelivered_peer and self._deliver_question(q, render=False):  # ②
                with self._cv:  # 재배달 실패 — 답할 데가 없다
                    self._questions.pop(q.id, None)
        for addr in kick:  # ③
            self.remind_owed(addr)

    # ── worker ──────────────────────────────────

    def _persist_reply(self, tm: AgentInstance, seq: int, body: str) -> str:
        """회신 전문을 항상 디스크에 (over-cap 포인터 + P3 보존 토대)."""
        try:
            replies_dir = tm.home_dir / "replies"
            replies_dir.mkdir(parents=True, exist_ok=True)
            path = replies_dir / f"reply-{seq}.md"
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
        from agent_cli.subagent.runner import create_subagent_ctx

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
            mode = "resume" if tm.revive else tm.context_mode
            # model=tm.model (role 오버라이드 적용 후) — models.json 바인딩이
            # 있으면 그 wire format, 없으면 부모 상속 (multi-wire-format P1).
            ctx, err = create_subagent_ctx(
                mode, parent_ctx, tm.home_dir, model=tm.model
            )
            if ctx is None:
                tm.error = err
                crash = f"context creation failed: {err}"
                return
            tm.ctx = ctx
            tm.state = "idle"
            # starting→idle 전환도 roster 에 반영 — 초기 task 없는
            # spawn/재생성분이 UI 에 "starting" 으로 영구 표시되던 버그
            # (busy/dead 전환만 알리고 최초 idle 을 빠뜨렸었음, v4.62.1).
            self._notify_roster()

            # 사람-직접 요청 배치 수집 중 만난 비-사람 항목을 다음 이터레이션으로
            # 이월하는 1칸 stash (SimpleQueue 는 put-front 가 없어 필요).
            stash = None
            while not tm.stop_event.is_set():
                item = stash if stash is not None else tm.inbox.get()
                stash = None
                if item is _SHUTDOWN or tm.stop_event.is_set():
                    break
                # P0-9b: dequeue 직후 즉시 busy — 종전엔 분류/배치 수집 뒤
                # _handle_* 안에서야 busy 로 바뀌어, 그 사이 idle-reap 의
                # any_activity()(state 검사 + inbox.qsize — 방금 비움)가 "활동
                # 없음"으로 오판해 작업 중 세션을 거둘 수 있었다.
                tm.state = "busy"
                tm.asked_this_run = False  # 런 경계 (§3.7 회신 억제)
                # 사람-직접(user:*) 요청은 대기분을 한 턴에 배치 처리(drain-all,
                # C-1) — main 위임/peer/배달회신은 회신 목적지가 달라 개별. inbox 에
                # 실제로 더 쌓여 있을 때(qsize>0)만 수집하므로 단건 경로는 종전과
                # 동일(회귀 안전).
                if self._is_human_direct(item):
                    batch = [item]
                    while tm.inbox.qsize() > 0:
                        try:
                            nxt = tm.inbox.get_nowait()
                        except Empty:
                            break
                        if nxt is _SHUTDOWN:
                            tm.inbox.put(_SHUTDOWN)  # 바깥 루프 종료 몫으로 재게시
                            break
                        if self._is_human_direct(nxt):
                            batch.append(nxt)
                        else:
                            stash = nxt  # 비-사람 항목은 다음 이터레이션으로 이월
                            break
                    if len(batch) == 1:
                        self._handle_request(tm, item, renderer, _disp)
                    else:
                        self._handle_human_batch(tm, batch, renderer, _disp)
                else:
                    self._handle_request(tm, item, renderer, _disp)
                # 미답 질문 독촉은 **런이 끝났다**는 사실에 붙는다 — 세 갈래가
                # 수렴하는 여기 한 곳에 둬야 핸들러가 늘어도 안 빠진다.
                self.remind_owed(f"agent:{tm.key}")
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
            if not tm.revivable:
                # **영구 사망(kill·crash)일 때만** 질문을 정리한다 (§3.8).
                # 세션 종료(shutdown_all)는 revivable 을 유지하므로 여기
                # 들어오지 않는다 — 들어오면 직후의 _save_state 가 빈 목록을
                # 저장해 resume 이 알릴 열린 질문이 항상 0건이 된다.
                self._purge_questions_for(tm.key)
            renderer.end_prompt_scope(tm.key)  # 스코프 고정 (사후 검사 가능)
            self._save_state()  # ctx 실패(error→dead)·종료 상태 반영
            self._notify_roster()

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

    def _with_human_notice(self, tm: AgentInstance, seq: int, output: str) -> str:
        """이 런이 **사람에게** 물어 둔 미답 질문을 결과에 실어 알린다.

        사람에게는 강제를 못 건다 — 우리 루프가 아니다. 대신 결과에 실어
        "내가 답을 안 해서 끝났구나" 를 알린다. 이 문구는 **하네스가** 붙인다:
        dispatch 에서 붙이면 ``serialize_terminal_for_history`` 를 타고 모델
        자신이 쓴 최종답으로 ctx 에 남아, 다음 런에서 모델이 하네스 문구를
        모방한다.
        """
        human_open = [q for q in self.questions_asked_in(tm.key, seq) if q.to_human]
        if not human_open:
            return output
        lines = "\n".join(f'   [{q.id}] "{q.text}"' for q in human_open)
        return (
            f"{output}\n\n⏳ 답을 받지 못한 질문 {len(human_open)}건 — "
            f"답하면 이어서 진행합니다:\n{lines}"
        )

    def _handle_request(self, tm: AgentInstance, item: dict, renderer, disp) -> None:
        """단일 request 처리 — main 위임/peer/배달회신/사람-직접(단건) 공통 경로.
        회신 라우팅은 author 기준(main→mailbox, agent:*→requester, user:*→창)."""
        tm.state = "busy"
        seq = item["seq"]
        text = item["text"]
        # 화자 attribution — teammate ctx 에 누가 말했는지 남긴다
        # (P2 양방향·P4 인간 개입에서 두 화자를 구분하는 기반).
        author = item.get("author", "main")
        tm.current_author = author  # 회신/질문 라우팅 기준 (D8)
        tm.current_seq = seq  # 이 런에서 거는 질문의 asked_seq (§3.2)
        # 이 항목이 질문이면 **꺼낸 지금** 배달로 친다 — 그 전까지는 큐에서
        # 줄만 서 있었고, 강제·sweep 이 걸리면 상대가 읽지도 않은 질문을
        # "(답변 없음)" 으로 닫아 버린다 (§3.2 런 스코프).
        if item.get("question_id"):
            self.mark_question_delivered(item["question_id"], seq)
        self._notify_roster()
        query = f"[{author}]: {text}" if author != "main" else text

        renderer.begin_agent_work(
            key=tm.key,
            seq=seq,
            profile=disp,
            message=text,
            # 요청 발신 시각을 scope_start(nav_ts)로 실어, 스윔레인
            # 요청 화살표가 이 작업 카드로 이동하게 한다.
            req_ts=item.get("ts"),
        )
        success, output, duration = False, "", 0.0
        try:
            loop_result, duration = self._run_message(tm, query)
            success = bool(loop_result.success)
            output = (
                loop_result.output
                if loop_result.output is not None
                else "(agent did not complete the request)"
            )
        except Exception as e:  # worker 는 죽지 않는다 — 회신으로 보고
            output = f"agent internal error: {type(e).__name__}: {e}"
        finally:
            renderer.end_agent_work(
                key=tm.key,
                seq=seq,
                success=success,
                duration_s=duration,
                error="" if success else output[:200],
            )
        # ── 비동기 질문 마무리 (DESIGN.md §3.6·§3.7) ──
        # 독촉은 여기가 아니라 **워커 루프**에 있다 — 런이 끝났다는 사실에
        # 붙는 것이라 핸들러마다 두면 빠진다(실제로 배치 경로에서 빠졌다).
        # 이 런에서 **건** 질문 중 아직 열린 것 (asked_seq 로 좁힌다 —
        # 다른 런의 사람 질문은 영영 열려 있을 수 있어 여기 섞이면 안 된다).
        output = self._with_human_notice(tm, seq, output)
        reply_path = self._persist_reply(tm, seq, output)
        tm.handled += 1
        tm.state = "idle"
        # 대화 창에는 화자 불문 항상 표시 (P4).
        out_payload = {
            "key": tm.key,
            "direction": "out",
            "author": tm.key,
            "text": output,
            "seq": seq,
            "success": success,
            "to": author,  # 수신자 — @agt 명령/창 개입이면 user:* (D8)
            "ts": time.time(),
            "profile": tm.profile_name,
            "instance_name": tm.instance_name,
        }
        renderer.agent_message(**out_payload)
        self._log_conversation(tm, out_payload)
        expects_reply = item.get("expects_reply", True)
        # ③ 이 런이 질문을 **걸었으면** 재주입만 건너뛴다 (§3.7).
        #
        #    판정은 "아직 열려 있나" 가 **아니다**. 비동기라 답은 보통 이
        #    런이 끝나기 **전에** 도착하고, 그러면 그 조건은 안 걸려 부분
        #    결과가 요청자에게 간다 — 막으려던 상황이 오히려 정상 경로다.
        #    게다가 그 부분 결과는 **답이 존재하기 전에** 만들어졌는데
        #    **답을 보낸 뒤에** 도착한다: 요청자는 자기 답이 반영된 최신
        #    상태로 읽고 다음 단계를 시작한다(peer 면 inbox 항목 = 런 1개라
        #    잘못된 하위 작업이 실제로 돌아간다).
        #
        #    질문을 걸었다면 답 런이 **반드시** 하나 생긴다 — 답·상한 초과·
        #    상대 사망 셋 다 ``_deliver_answer`` 를 지난다. 그 런의 회신이
        #    진짜 회신이다. 보장하는 성질은 "정확히 한 번" 이 아니라
        #    **"회신이 낡지 않는다"** 다(질문 둘이면 답 런도 둘, 회신도 둘 —
        #    다만 각각 자기 답이 반영된 최신 상태다).
        #
        #    창·로그·persist 는 위에서 이미 돌았다 — 억제되는 것은 요청자를
        #    한 번 더 깨우는 재주입 한 줄뿐이고, 하네스는 아무것도 기다리지
        #    않는다. 요청자는 질문을 이미 받았으므로 깜깜하지도 않다.
        if tm.asked_this_run:
            self._save_state()
        elif not expects_reply:
            # 배달된 peer 회신(v5.11): 수신자는 소비만 — 산출물을
            # 어디로도 라우팅하지 않는다(terminal, 핑퐁 방지). 결과에
            # 이어 다른 주체에게 보낼 게 있으면 명시적 message 로.
            self._save_state()
        elif author.startswith("agent:"):
            # peer 요청의 회신 → 요청자 inbox 로 terminal 재주입.
            # 청탁된 응답이라 항상 배달(LGTM 억제 없음 — 구독 제거로
            # watch 노이즈 억제가 불필요해짐, v5.12).
            requester = author.split(":", 1)[1]
            self._deliver_peer_reply(requester, tm.key, output, item.get("hop", 0))
        elif author == "main":
            # main 발신 요청의 회신만 main mailbox 로 (D8 — 인간
            # 개입 문답은 창에만, main 컨텍스트 비오염).
            # ``answers`` = 요청 스냅샷 그대로 (귀속 승계) — 회신을
            # 소비하는 런이 run_authors 에 합류시킨다. question/died
            # 는 미승계(사용자 답이 아닌 내부 왕복).
            self._push_reply(
                {
                    "kind": "reply",
                    "key": tm.key,
                    "profile": tm.profile_name,
                    "seq": seq,
                    "success": success,
                    "output": output,
                    "duration_s": duration,
                    "reply_path": reply_path,
                    "answers": item.get("answers") or [],
                }
            )
        else:
            self._save_state()  # user:* — 창만, push 건너뛰어도 상태 미러
        tm.current_author = "main"
        tm.current_seq = 0
        self._notify_roster()

    def _handle_human_batch(
        self, tm: AgentInstance, items: list[dict], renderer, disp
    ) -> None:
        """사람-직접 요청 2건+ 를 한 턴에 배치 처리(C-1) — 모두 ``user:*`` 발신
        이라 회신은 대화창 전용(⑥), main mailbox 미배달. main 과 동일하게 대기분
        전부를 한 응답으로 처리(_AGENT_BATCH_NOTICE 안내). 첫 항목의 seq/ts 로
        작업 카드 1개(스윔레인은 각 메시지의 in 화살표가 이 카드로 수렴)."""
        first = items[0]
        seq = first["seq"]
        author0 = first.get("author", "main")
        tm.state = "busy"
        tm.current_author = author0  # 배치 중 ask 는 첫 발신자에게
        tm.current_seq = seq
        self._notify_roster()

        labeled = [f"[{it.get('author', 'main')}]: {it['text']}" for it in items]
        query = _AGENT_BATCH_NOTICE + "\n\n" + "\n\n".join(labeled)
        preview = "\n".join(labeled)

        renderer.begin_agent_work(
            key=tm.key, seq=seq, profile=disp, message=preview, req_ts=first.get("ts")
        )
        success, output, duration = False, "", 0.0
        try:
            loop_result, duration = self._run_message(tm, query)
            success = bool(loop_result.success)
            output = (
                loop_result.output
                if loop_result.output is not None
                else "(agent did not complete the request)"
            )
        except Exception as e:
            output = f"agent internal error: {type(e).__name__}: {e}"
        finally:
            renderer.end_agent_work(
                key=tm.key,
                seq=seq,
                success=success,
                duration_s=duration,
                error="" if success else output[:200],
            )
        # 사람 발신 배치야말로 사람 주소 질문이 나오는 경로다(주소 =
        # current_author) — 여기 빠뜨리면 알림이 가장 필요한 곳에 없다.
        output = self._with_human_notice(tm, seq, output)
        self._persist_reply(tm, seq, output)
        tm.handled += len(items)  # N 요청을 한 턴에 처리
        tm.state = "idle"
        out_payload = {
            "key": tm.key,
            "direction": "out",
            "author": tm.key,
            "text": output,
            "seq": seq,
            "success": success,
            "to": author0,
            "ts": time.time(),
            "profile": tm.profile_name,
            "instance_name": tm.instance_name,
        }
        renderer.agent_message(**out_payload)
        self._log_conversation(tm, out_payload)
        self._save_state()  # 전부 user:* — 창만(⑥), mailbox push 없음
        tm.current_author = "main"
        tm.current_seq = 0
        self._notify_roster()

    def _run_message(self, tm: AgentInstance, query: str):
        """request 1건 실행 — 실제 러너 또는 테스트 주입 러너."""
        runner = self._runner
        if runner is None:
            from agent_cli.subagent.runner import run_subagent_message

            runner = run_subagent_message
        rt = self.runtime
        # v5.11: 상주 에이전트끼리 서로를 알고(로스터) 부를 수 있게(message)
        # — registry 자체는 서브루프에 넘기지 않는다(agent 상주 모드 차단
        # 유지). 로스터 문자열은 자기 자신 제외.
        from agent_cli.prompts.system_prompt import build_live_agents_section

        peer_section = build_live_agents_section(
            self, exclude_key=tm.key, via_message_tool=True
        )
        from agent_cli.runtime import ports_for_resident

        return runner(
            query,
            tm.ctx,
            # ``ask_handler`` 없음 — 상주 에이전트의 ask 는 질문 포트로
            # 간다(비블로킹). 죽은 블로킹 슬롯 경로는 C3 에서 제거했다.
            ports=ports_for_resident(
                key=tm.key,
                message_handler=self._make_message_handler(tm),
                questions=self.question_port(tm.key),
            ),
            peer_agents_section=peer_section,
            provider=rt.get("provider"),
            capabilities=rt.get("capabilities"),
            model=tm.model or rt.get("model", ""),
            timeout=rt.get("timeout", 300),
            provider_name=rt.get("provider_name", ""),
            base_url=rt.get("base_url", ""),
            api_key=rt.get("api_key", ""),
            max_turns=rt.get("max_turns", 0),
            depth=rt.get("depth", 0),
            max_depth=rt.get("max_depth", 2),
            active_tools=tm.allowed_tools,
            session=rt.get("session"),
            agent_name=tm.profile_name,
            stop_event=tm.stop_event,
            agent_role=tm.role_prompt,
            hooks_config=tm.hooks_config,
            compaction_enabled=rt.get("compaction_enabled", True),
        )


class MailWaker:
    """P4 (D3): main idle 자동 재기동 조율자 — 순수 로직 (web 무의존).

    회신/질문 도착 시 main worker 가 큐 대기(idle) 중이면 합성 wake
    아이템을 입력 큐에 넣어 run 을 깨운다. 배달 자체는 그 run 의 첫 턴
    경계가 수행. ``armed`` 가 중복 wake 를 막고(여러 mail → run 하나),
    wake 아이템을 꺼냈을 때 잔여가 없으면(이미 다른 run 이 배달) skip.
    ``on_run_end`` 는 "run 마지막 턴 경계 이후 도착한 mail" 레이스 봉합.
    """

    WAKE_TEXT = (
        "New agent mail has arrived. It is delivered as observation(s) at "
        "the start of this turn — review it and continue accordingly."
    )

    def __init__(self, enqueue: Callable, has_pending: Callable[[], bool]):
        self._enqueue = enqueue  # (conn_id, text) — web 서버의 입력 큐
        self._has_pending = has_pending
        self._armed = False
        # P0-9c: _armed 는 worker(on_mail)·펌프(mark_idle/handle_dequeued) 두
        # 스레드가 공유 — check-then-set 을 락으로 원자화해 "중복 무장 무해"
        # 가드가 스스로 레이스이던 것을 봉합(비무장 오판 시 wake 유실 → park).
        self._armed_lock = threading.Lock()
        self.idle = threading.Event()  # worker 가 dequeue 대기 중인 구간

    def _arm(self) -> None:
        with self._armed_lock:
            if self._armed:
                return
            self._armed = True
        self._enqueue(None, self.WAKE_TEXT)

    def on_mail(self) -> None:
        """registry.on_reply 에서 — idle 일 때만 wake."""
        if self.idle.is_set():
            self._arm()

    def mark_idle(self) -> None:
        """펌프가 큐 블록 직전에 호출 — idle 를 set 하고, **이미 미배달 회신이
        있으면 즉시 재무장**한다. ``on_run_end()`` 반환 후 ``idle.set()`` 전
        창에서 도착한 회신은 ``on_mail()`` 이 idle 미set 을 보고 wake 를
        드롭하는데(web 은 timeout 없는 무한 블록이라 그대로 영구 park),
        이 재확인이 그 lost-wakeup 을 봉합한다.

        정합성: idle 를 **먼저** set 한 뒤 pending 을 확인하므로, 동시 도착
        회신은 (a) 여기 ``_has_pending`` 에 잡히거나 (b) idle set 을 본
        ``on_mail`` 이 무장한다 — 최소 한쪽은 반드시 무장(append 와
        has_pending 은 registry cv 로 직렬화, idle 는 Event 로 순서 보장).
        중복 무장은 ``_armed`` 가드로 무해(handle_dequeued 가 skip 처리)."""
        self.idle.set()
        if self._has_pending():
            self._arm()

    def on_run_end(self) -> None:
        """run 종료 직후 — 마지막 턴 경계 이후 도착분 잔여 확인."""
        if self._has_pending():
            self._arm()

    def handle_dequeued(self, text: str) -> str | None:
        """큐에서 꺼낸 메시지 판정: wake 아니면 None, wake 면
        "run"(배달할 잔여 있음) 또는 "skip"(이미 배달됨)."""
        if text != self.WAKE_TEXT:
            return None
        with self._armed_lock:
            self._armed = False
        return "run" if self._has_pending() else "skip"


# ── LLM 도구 진입점 (tool_bridge 인터셉트) ──────
#
# 모드 테이블화 (리뷰 §4.4 T3): 상주 모드별 처리는 ``_agent_<mode>`` 핸들러
# 함수 + ``_MODE_HANDLERS`` 테이블 — 종전 tool_agent 안의 if-체인.
# 모드 추가 = agent_tool.AGENT_MODES 한 줄 + 핸들러 함수 하나
# (커버리지는 tests 의 테이블↔핸들러 정합 테스트가 고정).


def _agent_spawn(registry, args: dict, *, parent_ctx, runtime) -> ToolResult:
    key, error = registry.spawn(
        profile=args.get("profile", ""),
        name=args.get("name", ""),
        instructions=args.get("instructions", ""),
        allowed_tools=args.get("tools"),
        context_mode=args.get("context", "none"),
        parent_ctx=parent_ctx,
        runtime=runtime,
    )
    if error:
        return ToolResult(False, error=f"spawn rejected: {error}")
    _parts = [p for p in (args.get("profile", ""), args.get("name", "")) if p]
    lines = [f"spawned agent '{key}'" + (f" ({' · '.join(_parts)})" if _parts else "")]
    # 같은 역할의 dead 가 있으면 알려준다 — "다시 시작" 의도였다면
    # 기억을 보존하는 길은 resume 이었음을 다음 선택부터 반영하도록.
    profile_arg = args.get("profile", "")
    if profile_arg:
        dead_same_role = [
            tm.key
            for tm in registry._agents.values()
            if tm.profile_name == profile_arg and tm.state == "dead" and tm.key != key
        ]
        if dead_same_role:
            lines.append(
                f"note: dead agent(s) with the same profile exist "
                f"({', '.join(dead_same_role)}) — this NEW spawn starts "
                f"with NO memory of them. If you meant to CONTINUE one, "
                f'kill this and use {{"mode":"resume","key":"..."}} instead.'
            )
    task = args.get("task", "")
    if task:
        err = registry.request(key, task)
        if err:
            lines.append(f"initial task NOT queued: {err}")
        else:
            lines.append(
                "initial task queued — the reply arrives automatically as "
                "an observation and you will be woken. Do NOT poll status "
                "or re-send it (re-sends queue duplicate work and slow it "
                "down). FIRST finish the rest of your plan — spawn the "
                "other agents / send the other requests you intended, "
                "ideally batched with a final `complete` in this SAME "
                "turn. Only complete-and-wait once nothing else remains."
            )
    else:
        lines.append(
            'send work with {"mode":"request","key":"' + key + '","task":"..."}.'
        )
    return ToolResult(True, output="\n".join(lines))


def _agent_request(registry, args: dict, *, parent_ctx, runtime) -> ToolResult:
    key = args.get("key", "")
    tm = registry.get(key)
    # request 는 언제나 **일감**이다 — 열린 질문의 답은 ``answer`` 도구로만
    # 들어온다(비동기 전환 전에는 여기서 슬롯 배달 여부를 구분했다).
    err = registry.request(key, args.get("task", ""))
    if err:
        return ToolResult(False, error=f"request rejected: {err}")
    backlog = tm.inbox.qsize() if tm is not None else 0
    stacked = (
        (
            f"\n⚠ it now has {backlog} queued requests — if these are "
            f"progress checks or re-sends of the same ask, that is "
            f"interference: each one queues MORE work and delays the "
            f"answer. Replies arrive automatically; stop re-sending."
        )
        if backlog >= 2
        else ""
    )
    return ToolResult(
        True,
        output=(
            f"queued to {key} — the reply will be delivered to you "
            f"automatically at a later turn (even while you are idle) and "
            f"you will be woken when it arrives. Do NOT poll status or "
            f"re-send this request. If your plan still has other work "
            f"(other agents, other requests), do it now; then finish "
            f"this turn with `complete` and wait.{stacked}"
        ),
    )


def _agent_resume(registry, args: dict, *, parent_ctx, runtime) -> ToolResult:
    key = args.get("key", "")
    err = registry.resume_teammate(key, parent_ctx=parent_ctx)
    if err:
        return ToolResult(False, error=f"resume rejected: {err}")
    lines = [
        (
            f"agent '{key}' resumed — it remembers ALL previous exchanges "
            f"(its context was preserved across death)."
        )
    ]
    task = args.get("task", "")
    if task:
        qerr = registry.request(key, task)
        if qerr:
            lines.append(f"task NOT queued: {qerr}")
        else:
            lines.append(
                "task queued — reply arrives automatically; do not poll, "
                "finish with `complete` and wait."
            )
    return ToolResult(True, output="\n".join(lines))


def _agent_status(registry, args: dict, *, parent_ctx, runtime) -> ToolResult:
    return ToolResult(True, output=registry.format_status(args.get("key", "")))


def _agent_kill(registry, args: dict, *, parent_ctx, runtime) -> ToolResult:
    key = args.get("key", "")
    err = registry.kill(key)
    if err:
        return ToolResult(False, error=f"kill rejected: {err}")
    return ToolResult(
        True,
        output=(
            f"agent '{key}' terminated. Its context is PRESERVED — to "
            f"bring it back later with full memory, use "
            f'{{"mode":"resume","key":"{key}"}} (do NOT spawn a new one '
            f"if you want it to remember)."
        ),
    )


# 상주 모드 → 핸들러. agent_tool.AGENT_MODES 의 engine="registry" 모드와
# 1:1 — 정합은 tests 의 커버리지 테스트가 고정 (run 은 oneshot 엔진이라
# tool_bridge 가 tool_delegate 로 라우팅, 여기 없음).
_MODE_HANDLERS = {
    "spawn": _agent_spawn,
    "request": _agent_request,
    "status": _agent_status,
    "resume": _agent_resume,
    "kill": _agent_kill,
}


def tool_agent(
    args: dict,
    *,
    registry: AgentRegistry | None,
    parent_ctx=None,
    runtime: dict | None = None,
) -> ToolResult:
    """teammate 도구 mode 디스패치 — ``_MODE_HANDLERS`` 테이블 경유.
    delegate 처럼 루프가 인터셉트해 provider/ctx 배선(runtime)을 주입한다."""
    from agent_cli.tools.agent_tool import REGISTRY_MODES

    if registry is None:
        return ToolResult(
            False,
            error=(
                f"persistent agent modes ({'/'.join(REGISTRY_MODES)}) are "
                'main-session only — in this loop use {"mode":"run","task":...} '
                "for a one-shot sub-agent instead"
            ),
        )

    # P3: 매 호출 runtime 갱신 — restore 로 되살아난 teammate(스폰 없음)도
    # 첫 request 부터 현재 세션의 provider 배선으로 돈다.
    if runtime:
        registry.runtime = runtime

    mode = args.get("mode", "")
    handler = _MODE_HANDLERS.get(mode)
    if handler is None:
        return ToolResult(
            False,
            error=(f"unknown mode '{mode}' — use " + " / ".join(_MODE_HANDLERS)),
        )
    return handler(registry, args, parent_ctx=parent_ctx, runtime=runtime)
