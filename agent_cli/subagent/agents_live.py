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

from agent_cli import item_ids
from agent_cli.constants import (
    CHECKIN_STEPS_S,
    harness_mail_tool,
    has_words,
    is_private_owner,
)
from agent_cli.context.session import AGENTS_DIR
from agent_cli.loop.kind import LoopKindError
from agent_cli.tools.result import ToolResult

if TYPE_CHECKING:
    from agent_cli.context.manager import ContextManager

# worker 를 inbox 블록에서 깨워 종료시키는 sentinel (identity 비교).
_SHUTDOWN = object()


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


def _persists(mail: dict) -> bool:
    """이 메일이 agents.json 미러에 남을 것인가 — 장부 밖(``ledger: "none"``) 메일은
    "지금" 에만 참인 재알림이라 남기지 않는다. 다음 세션의 첫 턴에 낡은 채 배달되면
    존재하지 않는 것에 대해 말하게 된다 (docs/ledger/DESIGN.md §14)."""
    return mail.get("ledger") != "none"


#: 배달된 peer 회신의 꼬리표 (v10.1.0 문구). 종전 "`complete` alone reports to no
#: one" 은 받은 쪽이 작별 인사를 `message` 로 되보내게 만들었고(빚 생성 → 핑퐁),
#: 실측(cnr60g) 게임 종료 뒤 작별 인사만 다섯 왕복이 돌았다. 되받아칠 것이
#: 없고 ack 도 원치 않는다는 사실을 말한다.


def envelope(
    kind: str, item_id: str, who: str, body: str, *, re: str = "", todo: str = ""
) -> str:
    """들어오는 배달의 봉투 — 모양 하나 (v10.77.0, D).

    ``── <kind> [<id>] from <who>[ re [<id>]] ──`` 다음 줄부터 본문, 할 일이
    있을 때만 끝에 괄호 한 줄. 종전엔 배달마다 머리줄이 제각각이었다(`agent X
    message`·`agent X reply (success)`·`[question q- from …]`·`[answer to your
    question: …]`). 회신·답은 무엇에 대한 것인지(``re``) 적는다 — 보낸 쪽과
    받은 쪽이 같은 id 를 본다. ``who`` 가 비면 ``from`` 을 뺀다(죽음 통지)."""
    src = f" from {who}" if who else ""
    ref = f" re [{re}]" if re else ""
    out = f"── {kind} [{item_id}]{src}{ref} ──\n{body}"
    return f"{out}\n({todo})" if todo else out


def owe_line(who: str, rid: str) -> str:
    """요청 봉투의 괄호 줄 — 누구에게 무엇으로 갚는가."""
    return f'You owe {who} a `reply` with `id` "{rid}".'


# ── 비동기 ask (docs/agent-ask/DESIGN.md) ──────────────
#
# 질문은 **아무것도 블록하지 않는다**: ``ask`` 가 여기에 등록하고 즉시
# 반환한다. v10.77.0 (B): 질문은 **사람에게만** 간다 — ❓ 트레이가 표면이고
# 사람이 답하면 asker 에게 새 배달로 온다. 에이전트·main 에게 묻는 길은
# `message`(요청 → 상대가 `reply` 를 빚진다) 하나다. 그래서 `answer` 도구도,
# 에이전트가 빚지는 질문(q-)도 없다. ``user*`` 는 하나의 주체로 본다 — CLI 는
# ``user``, 웹은 ``user:{nickname}`` 이고 뷰어가 여럿이면 닉이 다르다.

#: 빚진 `message` 없이 런이 끝났을 때 요청자에게 가는 폴백 요약의 머리말
#: (v9.21.0). 라벨이 있어야 받는 쪽이 "명시 회신" 과 구분한다.
_NO_REPLY_LABEL = "(no explicit reply — this is the agent's run summary)"


def _is_human_addr(addr: str) -> bool:
    """``user`` / ``user:<nick>`` — 사람 주소인가 (§0: 하나의 주체)."""
    return addr == "user" or addr.startswith("user:")


@dataclass
class Question:
    """열린 질문 하나 — ``AgentRegistry._questions`` 의 값. 주소는 언제나 사람
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
    """회신 빚 하나 — ``AgentRegistry._reply_debts[debtor]`` 목록의 원소.

    런이 받은 **요청**(``expects_reply=True`` 항목·main 에 배달된 `message`)
    마다 하나(v9.25.0 — 종전엔 상대별 하나라 같은 상대의 새 요청이 앞 것을
    덮었다; 런 도중 흡수된 요청이 여럿이면 각각 회신을 받아야 한다는 사용자
    결정). ``id`` 가 그 요청을 가리킨다 — 그 요청 배달의 id(`item_ids`,
    전역에서 유일). 갚으면(`reply(id)`·요청자에게 `message`·main 의 `agent request`)
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

    @property
    def human(self) -> bool:
        return _is_human_addr(self.to)


def _new_question_id() -> str:
    return item_ids.next_id(item_ids.QUESTION)


# agents.json 스키마 버전 — 비호환 변경 시 bump (구버전 파일은 무시=fresh).
#: v2 (v10.77.0): 순번(`seq`) 대신 id 가 키 — 옛 파일은 무시한다(호환층 없음).
AGENTS_STATE_VERSION = 2

# ── Live Teammates 시스템 프롬프트 리로드 플래그 ──
# 멤버십(spawn/kill/died/restore/auto_spawn) 변화 시 set — 루프의
# _execute_turn 이 directives/memory 플래그와 같은 자리에서 consume 해
# 다음 턴 시스템 프롬프트를 재조립한다 (상태 전이 busy/idle 은 안 건드림
# — KV 캐시 프리픽스를 의미 있는 사건에만 버스트).

# ── main registry 프로세스 슬롯 (v7.17.0 배선 통일) ──
# agent-cli 프로세스 = 세션 1개(web 인스턴스는 post 당 1 프로세스, run 은
# 단명) 전제의 "이 세션의 main AgentRegistry" 슬롯. run/web 이 생성 직후
# 등록하고, skill 실행(`SkillLoop`)이 registry 를 명시받지 못한 경로
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


def mail_queue_item(reply: dict) -> dict:
    """미배달 메일 1건 → 입력창 위 대기열의 한 줄 (v10.51.0).

    사용자 요청이 main 이 바쁠 때 서는 그 대기열에 같은 모양으로 선다 —
    ``{id, conn_id, nickname, text}`` 에 ``mail: True``. 글은 **곧 들어갈
    관찰의 머리줄**이라 대기열 줄과 타임라인의 관찰 줄이 같은 말을 한다.
    ``conn_id`` 가 비어 아무도 취소할 수 없다(메일은 취소 대상이 아니다)."""
    rec = build_reply_record(reply)
    lines = [ln.strip() for ln in str(rec.get("content") or "").splitlines()]
    lines = [ln for ln in lines if ln]
    tool = rec.get("tool") or "agent"
    head = lines[0].strip("─ ").strip() if lines else ""
    if head == tool and len(lines) > 1:
        head = lines[1]  # 하니스 작업: 머리줄이 `── auditor ──` 뿐이다
    return {
        "id": str(reply.get("item_id") or ""),
        "conn_id": "",
        "nickname": tool,
        "text": head,
        "mail": True,
    }


def build_reply_record(reply: dict, *, cap: int = 0, registry=None) -> dict:
    """mailbox 아이템 1건 → main ctx 에 넣을 관찰 레코드.

    에이전트에게서 온 것은 봉투 하나(:func:`envelope`, v10.77.0 D) — 요청·통지·
    회신·죽음. 머리의 id 는 그 메일의 id(`item_id`)이고, 요청이면 main 이 그
    id 로 빚진다. 작업·모니터·하니스 보고는 도구가 만든 보고라 봉투 밖이다.

    ``cap``(loop 의 ``_oversized_cap``, 0=무제한) 초과 회신은 전문 대신
    디스크 포인터 + head 발췌로 치환 — 전문은 worker 가 이미
    ``teammates/<key>/replies/reply-<id>.md`` 에 영속했다(배달과 무관하게
    항상 저장 — P3 resume 미배달 보존의 토대).

    ``registry`` (v7.11.0, 배달 시점에만 전달): 회신 레코드 말미에 그
    에이전트의 **배달-시점** 잔여 상태 한 줄을 동봉 — main 이 "얼마나
    밀렸는지" 보고 다음 요청/대기를 판단한다. 잔여가 있으면 남은 회신도
    자동 배달됨을 같이 안내(status 폴링 넛지와 정합 — 이 줄이 폴링을
    유발하면 안 됨). died/peer 레코드에는 붙이지 않는다.
    """
    from agent_cli.context.token_estimator import estimate_tokens

    key = reply.get("key", "")
    label = format_agent_label(key, reply.get("profile", ""), reply.get("name", ""))
    mid = str(reply.get("item_id") or "")

    if reply.get("kind") == "harness":
        # 하니스 작업의 결과 (v10.42.0, harness/jobs) — 팀원 회신이 아니다:
        # 돌려줄 주소도, 빚도 없다. 감사 보고가 여기로 온다. `tool=<job>` 은
        # monitor 와 같은 이유로 등록이 필요 없다(아래 주석).
        job = reply.get("job") or "harness"
        body = reply.get("output") or "(empty result)"
        return {
            "role": "user",
            "tool": job,
            "success": bool(reply.get("success")),
            "content": (
                f"── {job} ──\n{body}\n"
                "(A harness job's result — nothing to send back. Act on findings "
                "if there are any; otherwise there is nothing to do.)"
            ),
            "source": "harness",
        }

    from agent_cli.runtime import harness_record

    harness = harness_record(reply)
    if harness is not None:
        # 잡·모니터·체크인 — 도구가 만든 보고라 봉투 밖. 상주의 흡수·one-shot 의 메일함과
        # **같은 함수**(v10.89.0). 모니터의 `tool="monitor"` 는 빈 문자열이 아니어야 한다:
        # `is_format_intervention` 이 `tool == ""` 를 형식 개입으로 읽는다.
        return harness

    if reply.get("kind") == "died":
        # worker 사망 통지 (Q4): kill/세션종료가 아닌 비정상 종료 — main 이
        # status 를 조회하기 전에 능동적으로 알린다.
        reason = reply.get("output") or "worker terminated unexpectedly"
        content = envelope(
            "died",
            mid,
            "",
            f"{label}: {reason}",
            todo=(
                "Its queued requests were lost, but its context is kept — bring "
                f'it back with `agent` `mode` "resume" and `key` "{key}" to '
                "continue with full memory, or spawn a new one. If it died from "
                "a crash, the same cause may recur."
            ),
        )
        return {
            "role": "user",
            "tool": "agent",
            "success": False,
            "content": content,
            "source": "agent_died",
        }

    if reply.get("kind") == "peer_message":
        # 상주 에이전트가 main 에게 먼저 보낸 메시지 (v5.11) — 회신 대기
        # 아님. main 은 필요하면 agent request 로 답한다.
        msg = reply.get("output") or "(empty message)"
        if reply.get("notice"):
            # v10.1.0: 통지 — 빚도 회신도 아니다. 할 일이 없으니 괄호 줄도 없다.
            content = envelope("notice", mid, label, msg)
        elif reply.get("expects_reply"):
            # v9.22.0: `message` — 회신을 빚진다(빚 id = 이 메일의 id).
            rid = str(reply.get("debt_id") or mid)
            content = envelope("request", rid, label, msg, todo=owe_line("it", rid))
        else:
            # `reply` — main 이 보낸 요청의 답. 되받아칠 것 없음.
            content = envelope("reply", mid, label, msg, re=str(reply.get("re") or ""))
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

    if not reply.get("success"):
        body = f"(the run ended with an error)\n{body}"
    # 런의 회신(라벨 붙은 런 요약 폴백 포함) — 그 런을 연 요청이 ``run``
    content = envelope("reply", mid, label, body, re=str(reply.get("run") or ""))
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
        # 받은편지함 미러 (v9.24.5) — ``SimpleQueue`` 는 영속이 안 돼 재시작이
        # 줄 선 요청을 통째로 증발시켰다(보드 1zfgc2: 엔진 앞 8건). 들어올 때
        # 추가, 처리를 시작할 때 제거 — ``_save_state`` 가 이 목록을 쓴다.
        self.inbox_items: list[dict] = []
        # 지금 처리 중인 항목 — 런이 정상 종료하면 비운다. 재시작 때 남아
        # 있으면 complete 전에 끊긴 런이다(계속하기 안내로 다시 넣는다).
        self.in_flight: list[dict] = []
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
        self.handled = 0  # 처리 완료한 request 수 (화면용 숫자)
        # 지금 도는 런의 이름 = 그 런을 연 배달의 id — 질문의 런 스코프
        # (Question.asked_in)가 이 값을 찍는다. 유휴는 "".
        self.current_item = ""
        # 이 런의 회신 빚은 여기 없다 — 레지스트리의 주소별 장부
        # (`AgentRegistry._reply_debts`, v9.22.1) 가 main 과 같은 자리에서 든다.

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
    ``ResidentLoop`` 에 꿰면 15군데가 된다 —
    객체 하나로 묶는다. **레지스트리 자체는 절대 넘기지 않는다**:
    ``LoopConfig.agent_registry`` 가 "teammate 안 teammate 금지"의 단일
    가드(``loop/state.py``)이므로 서브루프에 닿으면 안 된다.

    표면은 ``ask``·``reply``·``debts`` 다. main 도 같은 포트를 받는다
    (``key=None``) — 빚을 갚는 `reply` 가 루프와 무관하게 하나이기 때문이다.
    단 ``ask`` 는 거부한다(아래).
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
        """사람에게 질문 등록 — ``(id, err)``, err 가 비면 성공 (v10.77.0, B).

        주소는 언제나 사람(❓ 트레이)이다. 에이전트·main 에게 무언가 필요하면
        `message` 다 — 그건 요청이고 상대가 `reply` 를 빚진다. 종전의 ``to``
        (원 요청자 기본·에이전트 주소)는 에이전트가 빚지는 질문(q-)과 `answer`
        도구를 만들었는데, 실제 기록에서 `answer` 11회가 전부 실패했다.

        **main 은 거부한다.** main 의 ``ask`` 는 사람에게 묻는 기존 블로킹
        경로(`renderer.prompt_user`) 그대로다. 여기로 오면 로스터에도 창에도 안
        뜨는(둘 다 asker 를 에이전트 키로 찾는다) 질문이 생겨 ``any_activity()``
        를 영구 True 로 만든다.
        """
        if self.key is None:
            return "", (
                "main asks the operator through the blocking `ask` prompt, "
                "not through the question list"
            )
        return self._reg.register_question(self.asker, text)

    def debts(self) -> list[dict]:
        """이 런이 아직 갚지 않은 회신 빚 (v9.22.0) — main·상주 **같은 코드**.

        ``{"kind": "reply", "to": addr, "id": rid, "text": …}`` — 이 런이 받은
        요청 중 안 갚은 것(`_reply_debts[me]`). 상주는 시작·흡수 항목, main 은
        런에 배달된 `message`; 적는 쪽만 다르고 장부는 하나다. `complete`
        직전에 루프가 물어 독촉한다.
        """
        return [
            {"kind": "reply", "to": d.to, "id": d.id, "text": d.text}
            for d in self._reg.reply_debts(self.me)
        ]

    @staticmethod
    def _owed_listing(owed) -> str:
        return "; ".join(
            f'[{d.id}] {d.to.split(":", 1)[-1]}: "{d.text[:60]}"' for d in owed
        )

    def _main_reply(self, text: str, *, id: str) -> str:
        """main 이 에이전트에게 빚진 회신을 갚는다 (v10.56.0, 장부 P3 §11.3).

        종전엔 main 의 `agent(mode="request")` 가 그 에이전트 앞 **가장 오래된**
        빚을 갚은 것으로 쳤다 — id 가 없어 어떤 빚이 닫혔는지 추적되지 않았고,
        "일을 시킨다" 와 "답한다" 가 한 도구에 섞였다. 이제 갚는 도구는 루프와
        무관하게 `reply(id)` 하나다. 배달은 그 에이전트의 inbox 로, 되받을 것
        없이(`expects_reply=False` — 핑퐁 방지)."""
        owed = self._reg.reply_debts(self.me)
        if not owed:
            return (
                "nothing to reply to — no agent is waiting on a reply from you. "
                'To give an agent new work, use `agent` with mode "request".'
            )
        listing = self._owed_listing(owed)
        id = str(id or "").strip()
        if not id:
            return (
                "reply needs `id` — which request is this the reply to? You owe: "
                f"{listing}. Send `reply` with that `id`."
            )
        d = next((x for x in owed if x.id == id), None)
        if d is None:
            return f'no unpaid request "{id}" — you owe: {listing}.'
        self._reg.settle_id(self.me, id)
        return self._reg._deliver_peer_reply(
            d.to.split(":", 1)[1], "main", text, 0, re=id
        )

    def reply(self, text: str, *, id: str = "") -> str:
        """빚진 회신을 갚는다 (v9.21.0) — 에러 메시지 또는 빈 문자열.

        ``id`` (v9.25.0) 는 **필수** — 어느 요청에 대한 답인지 하네스가 알아야
        한다. 런 도중 요청이 흡수되면 빚이 여럿이고, 그때 "요청자" 는 하나가
        아니다. 없으면 항상 거부하고 번호 목록을 보여 준다(`complete` 의
        `answers` 와 같은 원칙, 사용자 결정). id 가 전역에서 유일하므로 그것만으로
        상대가 정해진다 — v10.77.0 에서 ``to`` 를 지웠다(C).
        돌려받을 것이 없으므로 `expects_reply=False` 로 간다.
        **빚진 게 없으면 거부한다**(사용자 결정): 회신·질문·독촉으로 시작한
        런엔 답할 상대가 없고, "확인했습니다" 류의 ack 가 정확히 여기서
        새어 나온다. 거부 문구가 `message` 로 안내한다.
        """
        text = (text or "").strip()
        if not text:
            return "empty reply — nothing sent"
        if not has_words(text):
            return f"reply has no words ({text[:20]!r}) — nothing sent; say it in words"
        if self.key is None:
            return self._main_reply(text, id=id)
        tm = self._reg.get(self.key)
        if tm is None:
            return "unknown agent"
        owed = self._reg.reply_debts(self.me)
        if not owed:
            a = tm.current_author
            d = self._reg.reply_debt(self.me, a)
            if d is not None and d.human:
                # 사람이 창에서 직접 시킨 런 — 창이 곧 배달이라 빚이 없다(D8).
                # 종전 문구("요청으로 시작하지 않은 런 … message 를 써라")는
                # 거짓이었고, 그대로 따르면 message(to="user:…") 가 에이전트가
                # 아니라 실패한다.
                return (
                    "nothing to send — your requester is a person watching this "
                    "window, and they see your `complete` here. Just `complete`."
                )
            if d is not None and d.settled:
                # 이미 갚았다 — 실측(a209hq): player 가 "칙령" 을 보낸 뒤 "칙명"
                # 으로 정정하려다 "요청으로 시작되지 않은 런" 이라는 거짓
                # 사유를 받았다. 사유는 정직해야 다음 행동이 맞아진다.
                return (
                    f"already replied to {a} in this run — a second reply is not "
                    "delivered. If you must correct it, send the correction with "
                    f'`message` (`to` "{a.split(":", 1)[-1]}", `text` your correction).'
                )
            return (
                "nothing to reply to — this run was not started by a request "
                "that expects one (it was a delivered reply, a notice, or an "
                "answer). Do not acknowledge those. If you need something from "
                "someone, use `message` (`to` the recipient, `text` your message)."
            )
        listing = self._owed_listing(owed)
        id = str(id or "").strip()
        if not id:
            return (
                "reply needs `id` — which request is this the reply to? You owe: "
                f"{listing}. Send `reply` with that `id`."
            )
        d = next((x for x in owed if x.id == id), None)
        if d is None:
            prior = self._reg.debt_by_id(self.me, id)
            if prior is not None and prior.settled:
                return (
                    f"already replied to [{id}] ({prior.to}) in this run — a second "
                    "reply is not delivered. If you must correct it, send the "
                    f'correction with `message` (`to` "{prior.to.split(":", 1)[-1]}", '
                    "`text` your correction)."
                )
            return f'no unpaid request "{id}" — you owe: {listing}.'
        self._reg.settle_id(self.me, id)
        if d.to == "main":
            self._reg.message_to_main(
                self.key,
                text,
                profile=tm.profile_name,
                name=tm.instance_name,
                answers=d.answers,
                re=id,
            )
            return ""
        requester = d.to.split(":", 1)[1]
        self._reg._deliver_peer_reply(requester, self.key, text, 0, re=id)
        self._reg._log_outbound(self.key, text, to=requester)
        return ""


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
    ):
        # 회신을 처리할 provider/모델 등 실행 배선 — spawn 시점이 아니라
        # 레지스트리 생성 시점(부트스트랩)에 고정할 수도 있으나, provider
        # 는 tool_bridge 인터셉트에서만 완전하므로 spawn 마다 갱신 수용.
        self.runtime = runtime or {}
        self._runner = runner
        self.session_dir = Path(session_dir) if session_dir else None
        # 동시 생존 상한 (세션 한정, web UI 조절). 0 = 무제한. None = 기본값 —
        # env·CLI 는 부트가 `knobs.apply_all` 로 적용한다 (v10.43.0).
        self.max_agents = clamp_max_agents(
            _DEFAULT_MAX_AGENTS if max_agents is None else max_agents
        )

        self._agents: dict[str, AgentInstance] = {}
        self._cv = threading.Condition()
        self._pending: list[dict] = []  # 미배달 회신 (도착 순서)
        #: private 주소(run:·harness:)의 메일함 (v10.84.0) — 그 루프가 턴 경계와
        #: `wait` 에서 읽는다. UI·영속 밖(프로세스 수명). 같은 `_cv` 로 깨운다.
        self._mailboxes: dict[str, list[dict]] = {}
        # 회신 도착 알림 (CLI 대기 줄) — 부트스트랩 주입.
        self.on_reply: Callable[[dict], None] | None = None
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
        # 열린 질문 (비동기 ask/answer, DESIGN.md §3) — id → Question.
        # ``_pending`` 과 같은 규율: 모든 접근은 ``_cv`` 아래. 답 claim 이
        # 원자적이어야 동시 답변자 둘 중 하나만 답이 된다.
        self._questions: dict[str, Question] = {}
        # 회신 빚 장부 (v9.22.1) — debtor 주소("main"|"agent:<key>") →
        # {상대 주소 → ReplyDebt}. main 과 상주가 **같은 장부**를 쓴다: 적는
        # 자리(main = 메일 배달, 상주 = 항목 시작)와 폴백 배관만 다르다.
        self._reply_debts: dict[str, list[ReplyDebt]] = {}
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
                k
                for k, t in list(self._agents.items())
                if t.instance_name and t.instance_name.lower() == low
            ]
            alive = [k for k in hits if self._agents[k].state != "dead"]
        hits = alive or hits
        return hits[0] if len(hits) == 1 else None

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
        메일함(``_pending``)에서 작업·모니터 통지는 ``to`` 가 없어 한 건도
        안 잡혔고(벤치 실측: 모니터가 먼저 발화한 뒤의 wait 가 "nothing
        pending" 으로 거부), 잡혔더라도 **곧 배달될 것** 을 깨울 것으로 쳐
        런을 끝내고 바로 다시 깨우는 낭비였다."""
        return [text for _, text in self.pending_agents_for(addr, exclude)]

    def pending_agents_for(
        self, addr: str, exclude: frozenset = frozenset()
    ) -> list[tuple[str, str]]:
        """`pending_for` 의 재료 — ``(에이전트 주소, 사람이 읽을 줄)`` 쌍 (v10.79.0).
        주소는 `wait` 중간 깨우기의 대상 id(`runtime.wake_source_ids`)가 된다."""
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
            for debtor, debts in self._reply_debts.items():
                if debtor == addr or debtor in exclude:
                    continue
                owes = any(
                    not d.settled and not d.human and d.to == addr for d in debts
                )
                if owes and not self._debtor_is_parked(debtor):
                    out.append(("owes", debtor))
        for tm in list(self._agents.values()):
            me = f"agent:{tm.key}"
            if me == addr or me in exclude:
                continue
            if self.state_is_active(tm.state) and tm.current_author == addr:
                out.append(("working", me))
            elif any(str(i.get("author") or "") == addr for i in self._queued(tm)):
                # 받은편지함 미러(`inbox_items`)로 본다 — `SimpleQueue` 엔 `queue`
                # 속성이 없어 종전 `getattr(tm.inbox, "queue", [])` 는 늘 비었다.
                out.append(("queued", me))
        return out

    def checkin_agent_lines(
        self, addr: str, step_marks: dict[str, int]
    ) -> tuple[list[str], bool]:
        """`wait` 중간 깨우기 알림의 에이전트 줄 (v10.79.0, docs/wait/DESIGN.md §6.3)
        — ``(줄 목록, 사람 질문에 막힌 줄이 있는가)``.

        작업·monitor 와 달리 에이전트는 로그 파일이 없다. 레지스트리가 **이미 아는
        것**만으로 그린다 — 상태·지금 처리 중인 요청자·받은편지함 미러·회신 빚과
        스스로 잠듦·그 에이전트가 기다리는 것(`runtime.wake_sources`)·열린 사람
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
        from agent_cli.runtime import wake_sources

        for debtor in self.parked_debtors_for(addr):
            key = debtor.split(":", 1)[1]
            on = wake_sources(debtor, frozenset({addr}))
            if not on:
                continue  # 깨울 것이 없는 채무자는 깨울 근거가 아니다 (v10.77.0)
            line = (
                f"{_label(key)} · owes you a reply · itself waiting on {', '.join(on)}"
            )
            with self._cv:
                open_qs = [q for q in self._questions.values() if q.asker == key]
            if open_qs:
                q = min(open_qs, key=lambda q: q.asked_at)
                line += (
                    f" · blocked on question {q.id} to the user, unanswered for "
                    f"{format_duration(now - q.asked_at)}"
                )
                question = True
            lines.append(line)
        return lines, question

    def _debtor_is_parked(self, debtor: str) -> bool:
        """빚진 쪽이 **스스로 잠들어 있다** — 런이 `wait` 로 끝나 빚만 남은
        유휴 에이전트(보통의 런 끝은 빚을 비운다). 그런 채무자는 그 자체로는
        아무도 깨우지 못한다: 그를 깨울 것이 있어야 회신이 온다
        (:meth:`parked_debtors_for` 와 `runtime.wake_sources` 가 거슬러 본다)."""
        if debtor == "main" or not debtor.startswith("agent:"):
            return False
        tm = self._agents.get(debtor.split(":", 1)[1])
        if tm is None or tm.state == "dead":
            return False
        return not self.state_is_active(tm.state) and not tm.inbox_items

    def parked_debtors_for(self, addr: str) -> list[str]:
        """``addr`` 에게 회신을 빚진 채 잠든 에이전트 주소들 (v10.77.0) — 그들을
        깨울 것이 있는지는 호출자(`runtime.wake_sources`)가 거슬러 본다. 서로
        `message` 하고 둘 다 `wait` 하면 아무도 깨지 않는 교착을 여기서 끊는다."""
        out: list[str] = []
        with self._cv:
            for debtor, debts in self._reply_debts.items():
                if debtor == addr:
                    continue
                owes = any(
                    not d.settled and not d.human and d.to == addr for d in debts
                )
                if owes and self._debtor_is_parked(debtor):
                    out.append(debtor)
        return out

    def _queued(self, tm: AgentInstance) -> list[dict]:
        """``tm`` 의 받은편지함에 들어 있고 아직 런이 집지 않은 항목(미러 스냅샷)."""
        with self._cv:
            return list(tm.inbox_items)

    def arrived_for(self, addr: str) -> int:
        """``addr`` 앞에 **이미 도착해 다음 턴 경계에 배달될** 메일 수 (v10.66.2,
        `wait` 의 둘째 판정 재료). main 은 메일함(``_pending`` — 작업·모니터
        통지, 에이전트 회신·질문 전부 main 행), 상주 에이전트는 자기 받은편지함.
        이게 0 이 아니면 `wait` 는 받지 않는다: 런을 끝내 봐야 펌프가 바로 다시
        깨우므로, 거부하고 다음 관찰에 배달되게 두는 쪽이 싸고 맥락도 남는다."""
        if addr == "main":
            with self._cv:
                return len(self._pending)
        if addr.startswith("agent:"):
            tm = self._agents.get(addr.split(":", 1)[1])
            return 0 if tm is None else len(self._queued(tm))
        if is_private_owner(addr):
            with self._cv:
                return len(self._mailboxes.get(addr, ()))
        return 0

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
            return "", f"unknown asker '{asker}'"
        asked_in = tm.current_item
        with self._cv:
            # 중복 접기 — **같은 런 안에서만**. ``_op_ask`` 는 루프 탐지기
            # 앞에서 반환하므로(dispatch.py) 한 런이 같은 질문을 반복해도
            # 구조적으로 안 잡힌다. 런 경계를 넘으면 접지 않는다.
            for q in self._questions.values():
                if q.asker == asker and q.text == text and q.asked_in == asked_in:
                    return q.id, ""
            q = Question(
                id=_new_question_id(),
                asker=asker,
                target="user",
                text=text,
                asked_in=asked_in,
            )
            self._questions[q.id] = q
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
        with self._cv:
            return [
                q
                for q in self._questions.values()
                if q.asker == asker and q.asked_in == run_item
            ]

    def open_human_questions(self) -> list[Question]:
        """열린 질문 — ❓ 트레이와 idle-reap 가드가 읽는다(주소는 전부 사람)."""
        with self._cv:
            return list(self._questions.values())

    def answer_question(self, qid: str, text: str, *, by: str) -> str:
        """사람이 트레이에서 ``qid`` 에 답한다 — 에러 또는 빈 문자열.

        ``user*`` 는 하나의 주체로 본다 — CLI 는 ``user``, 웹은 ``user:{nick}``
        이고 뷰어마다 닉이 달라, 문자열 동치로 검사하면 두 번째 뷰어의 트레이
        답이 거부된다.
        """
        text = (text or "").strip()
        if not text:
            return "empty answer"
        with self._cv:
            q = self._questions.get(qid)
            if q is None:
                return f"unknown or already-answered question '{qid}'"
            if not _is_human_addr(by):
                return f"question '{qid}' is addressed to the person, not {by}"
            del self._questions[qid]  # 원자적 claim — 동시 답변자 중 하나만
        self._deliver_answer(q, text)
        self._save_state()
        return ""

    # ── 회신 빚 장부 (v9.22.1 — main·상주 공통) ──────
    # 요청(`expects_reply=True` 항목 / main 에 배달된 `message`)은 빚이다.
    # 갚는 길: 상주는 `reply`·요청자에게 `message`, main 은 `agent request`.
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
            d = ReplyDebt(
                to=to, text=text, answers=list(answers or []), hop=hop, id=str(id)
            )
            self._reply_debts.setdefault(debtor, []).append(d)
            return d

    def settle(self, debtor: str, to: str) -> ReplyDebt | None:
        """``to`` 에게 진 **가장 오래된** 미납 빚을 갚는다 — 상대만 고르는 갚기
        (`message(to)`·main 의 `agent request`). 없으면 None."""
        with self._cv:
            for d in self._reply_debts.get(debtor, []):
                if d.to == to and not d.settled:
                    d.settled = True
                    return d
            return None

    def settle_id(self, debtor: str, id: str) -> ReplyDebt | None:
        """번호로 갚는다(`reply(id)`) — 그 번호의 미납 빚, 없으면 None."""
        with self._cv:
            for d in self._reply_debts.get(debtor, []):
                if d.id == str(id) and not d.settled:
                    d.settled = True
                    return d
            return None

    def reply_debt(self, debtor: str, to: str) -> ReplyDebt | None:
        """``to`` 에게 진 빚 중 **가장 최근** 것 — 갚았든 아니든 (거부 사유용)."""
        with self._cv:
            for d in reversed(self._reply_debts.get(debtor, [])):
                if d.to == to:
                    return d
            return None

    def debt_by_id(self, debtor: str, id: str) -> ReplyDebt | None:
        with self._cv:
            for d in self._reply_debts.get(debtor, []):
                if d.id == str(id):
                    return d
            return None

    def reply_debts(self, debtor: str) -> list[ReplyDebt]:
        """아직 안 갚은 빚 — 사람 주소 제외(창이 곧 배달).

        v10.77.0: 종전 §3.7 억제(이 런이 질문을 건 주소의 빚을 감춤)는 지웠다
        — 에이전트는 이제 질문 대신 `message` 요청을 보내고, 그 회신을 `wait`
        로 기다린 뒤 자기 빚을 `reply` 로 갚는다. 감추던 빚이 열린 채 남으니
        오히려 더 엄밀하다."""
        with self._cv:
            return [
                d
                for d in self._reply_debts.get(debtor, [])
                if not d.settled and not d.human
            ]

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

        - ``waiting`` (`wait` 로 끝남): 아무것도 하지 않는다 — 갚을 차례가 아니다.
          빚은 열린 채 다음 런으로 가고 깨어난 런이 `reply` 로 갚는다. 폴백을
          지금 보내면 요청자가 "⏸ waiting" 을 답으로 받는다.
        - ``stopped`` (kill·종료로 끊김): 빚을 버린다. 끊긴 요청은 재시작이 새
          id 로 다시 넣으며 다시 적는다 — 남기면 같은 요청에 빚이 둘이 되고, 죽은
          채무자의 빚이 요청자의 `wait` 를 영원히 붙잡는다.
        - 그 밖(보통의 끝): :meth:`end_run` — 남은 빚을 라벨 붙은 런 요약으로.

        종전엔 이 판정(``not waiting``·``not stop_event``)이 호출부 셋에 흩어져
        있었고, v10.65.0 의 상주 판정이 런 시작(`begin_run` 의 비우기)을 빠뜨려
        wait 뒤의 빚이 사라졌다.

        ``waiting`` 이면 중간 깨우기 타이머를 건다 (v10.79.0, :meth:`arm_checkin`) —
        런 끝의 유일한 자리라 main·상주·run·web 이 한 곳이다. ``meta`` 의 ``run``
        키는 그 런을 연 배달 id 이고 `run_started`(런 **시작**)와는 무관하다."""
        if waiting:
            self.arm_checkin(debtor)
            return 0
        self.run_started(debtor)  # 끝났으니 걸린 것이 있어도 의미가 없다
        if stopped:
            with self._cv:
                self._reply_debts.pop(debtor, None)
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
        from agent_cli.runtime import wake_source_ids

        ids = wake_source_ids(owner)
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
        from agent_cli.jobs import get_job_registry
        from agent_cli.runtime import wake_source_ids

        self.run_started(owner)  # 이미 걸린 것이 있으면 푼다
        ids = wake_source_ids(owner)
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
        않고 영속되지 않는다(`_persists`)."""
        from agent_cli.runtime import wait_checkin

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
        run(그 런을 연 배달 id)/success/duration/reply_path 를 싣는다).
        """
        n = 0
        summary = f"{_NO_REPLY_LABEL}\n{output}"
        for d in self.reply_debts(debtor):
            if debtor == "main":
                if not self._deliver_peer_reply(
                    d.to.split(":", 1)[1], "main", summary, 0, re=d.id
                ):
                    n += 1
                continue
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
                    }
                )
            else:
                self._deliver_peer_reply(
                    d.to.split(":", 1)[1], key, summary, d.hop, re=d.id
                )
            n += 1
        with self._cv:
            self._reply_debts.pop(debtor, None)
        return n

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
        )

    def _drop_monitors(self, key: str) -> int:
        """이 에이전트가 건 감시·띄운 백그라운드 작업·python 워커를 폐기 — private 종류와 같은
        함수(`runtime.release_owner`, v10.89.0). 메일함·체크인은 부르는 쪽이 따로(kill·종료는
        자기 순서로 취소하고, 죽음의 정본 자리는 레지스트리까지 넘긴다)."""
        from agent_cli.runtime import release_owner

        return release_owner(
            f"agent:{key}", jobs=self.jobs, monitors=self.monitors
        ).count

    def _purge_questions_for(self, key: str) -> None:
        """에이전트 사망 정리 — 그가 건 질문은 폐기한다(답할 사람이 트레이에서
        답해도 받을 곳이 없다). 호출자가 ``not revivable`` 아래에서만 부른다 —
        세션 종료(``shutdown_all``)에서 지우면 직후의 ``_save_state`` 가 빈 목록을
        저장해 resume 이 질문을 되살리지 못한다.
        """
        with self._cv:
            outgoing = [q.id for q in self._questions.values() if q.asker == key]
            for qid in outgoing:
                self._questions.pop(qid, None)
        if outgoing:
            self._save_state()
            self._notify_roster()

    # ── spawn ───────────────────────────────────

    def _model_unavailable(self, model: str, profile: str) -> str:
        """역할이 요구한 모델이 서버에 없으면 사람이 읽을 이유를, 아니면 ""."""
        from agent_cli.model_check import ModelNotFound, NoModelSelected, verify_model

        binding = (self.runtime or {}).get("binding")
        if binding is None or not binding.base_url:
            return ""  # 확인할 수단이 없으면 막지 않는다 (부팅 정책과 동일)
        try:
            verify_model(
                model,
                binding.base_url,
                binding.api_key,
                binding.provider_name or "openai",
            )
        except ModelNotFound as e:
            near = e.listing.suggest(model)
            # 모델이 읽는 거절 관찰이다(spawn 의 결과) — 영어로, 다음 행동과 함께.
            hint = f" (closest name: {near})" if near else ""
            avail = ", ".join(e.listing.models[:6])
            where = f"profile '{profile}'" if profile else "the role settings"
            return (
                f"the model '{model}' required by {where} is not on the server"
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
                f'Kill one first (`mode` "kill") or raise the limit in the web UI.'
            )

        if name and not re.match(r"^[a-zA-Z0-9_-]{1,24}$", name):
            return "", (f"invalid instance name '{name}': [a-zA-Z0-9_-], max 24 chars")

        dup = self._duplicate_label(profile, name)
        if dup:
            return "", dup

        role_prompt = ""
        description = ""
        config: dict = {}
        binding = self.runtime.get("binding")
        model = binding.model if binding is not None else ""
        hooks_config = self.runtime.get("hooks_config")
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
            home_dir=self.session_dir / AGENTS_DIR / key,
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
        answers: list | None = None,
        resumed: bool = False,
        images: list[dict] | None = None,
        item_id: str = "",
        kind: str = "",
        re: str = "",
        todo: str = "",
        ledger: str = "",
    ) -> str:
        """request 큐잉 — 에러 메시지 또는 빈 문자열.

        ``ledger`` (v10.79.0): ``"none"`` 이면 장부 밖 메일 — agents.json 미러에
        남지 않는다(`_persists`). 첫 사용자는 `wait` 의 중간 깨우기(``checkin``):
        "지금" 에만 참인 알림이 다음 세션 첫 턴에 낡은 채 배달되면 안 된다.

        ``item_id`` (v10.77.0): 이 배달의 id — 보낸 쪽이 미리 받아 두면(그
        id 를 결과에 실어 회신과 짝짓는다) 그대로 쓴다. 없으면 종류대로 새로
        받는다: 사람 발신은 ``u-``, 회신을 기다리는 요청은 ``r-``, 그 밖(통지·
        회신 도착)은 ``n-``. 이 id 가 배달의 유일한 키다.

        ``kind``·``re``·``todo`` (v10.77.0, D): 봉투의 재료 — ``"notice"``·
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
                f"unknown agent '{key}' — use a key or name from `## Live Agents` "
                '(or "main")'
            )
        key = resolved
        # `state` 만 보면 창이 뚫린다: `kill`/`shutdown_all` 은
        # `stop_event` 만 세우고 `state="dead"` 는 워커의 `finally` 에서야
        # 찍히므로, busy 런이면 몇 분 뒤다. 그 사이 큐에 넣은 항목은
        # `_SHUTDOWN` 뒤에 줄 서서 **영영 안 읽힌다**. `tm.stop_event` 를
        # 세우는 곳은 그 둘뿐이라(SIGINT 핸들러는 메인 스레드 전용, 웹
        # `/api/stop` 은 main 의 이벤트) 죽지 않는데 서는 경로가 없다.
        if tm.state == "dead" or tm.stop_event.is_set():
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
        # id 가 키다 (v10.77.0) — 전역 할당기라 동시 request() 에도 겹치지 않는다
        # (종전 에이전트별 순번은 P0-9a 의 락과 v8.5.1 의 키 덧붙이기가 필요했다).
        if not item_id:
            if _is_human_addr(author):
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
        if ledger:
            item["ledger"] = ledger
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
        self._enqueue(tm, item)
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

    def _enqueue(self, tm: AgentInstance, item: dict) -> None:
        """받은편지함에 넣고 미러·``agents.json`` 에 남긴다 (v9.24.5)."""
        with self._cv:
            tm.inbox_items.append(item)
        tm.inbox.put(item)
        self._save_state()

    def _begin_items(self, tm: AgentInstance, items: list[dict]) -> None:
        """이 항목들로 런을 시작한다 — 미러에서 빼고 처리 중으로 적는다.
        꺼냈지만 아직 시작 안 한 항목(배치 수집의 stash)은 미러에 남는다."""
        ids = {i.get("id") for i in items}
        with self._cv:
            tm.inbox_items = [i for i in tm.inbox_items if i.get("id") not in ids]
            tm.in_flight = [dict(i) for i in items]
        self._save_state()

    def _who(self, author: str) -> str:
        """봉투의 ``from`` — 에이전트는 이름표(`format_agent_label`), 사람은
        "the user", main 은 "main"."""
        if author == "main":
            return "main"
        if _is_human_addr(author):
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
            )
        if _is_human_addr(author):
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

        루프의 ``absorb_inbox`` 포트가 이걸 부른다. 여기서 하는 일은 런 시작
        (`ResidentLoop.start`)이 항목 하나에 하는 것과 같다 — 처리 중 목록으로
        옮기고(재시작 복원 대상), 요청이면 빚을 적고, 질문이면 배달로 찍는다.
        돌려주는 것은 루프가 그대로 넣을 수 있는 모양이다:
        ``{"user_request": {id, author, text}}`` (사람 창 입력 — main 의 큐
        주입과 같은 회계) 또는 ``{"record": 관찰 레코드}``.

        ``_SHUTDOWN`` 표식은 되넣고 멈춘다(사람 배치 수집기와 같은 방식).
        ``tool=""`` 은 형식 개입 표식이라 레코드에 절대 쓰지 않는다.
        """
        if tm.stop_event.is_set():
            return []
        items: list[dict] = []
        while tm.inbox.qsize() > 0:
            try:
                nxt = tm.inbox.get_nowait()
            except Empty:
                break
            if nxt is _SHUTDOWN:
                tm.inbox.put(_SHUTDOWN)
                break
            items.append(nxt)
        if not items:
            return []
        me = f"agent:{tm.key}"
        with self._cv:
            ids = {i.get("id") for i in items}
            tm.inbox_items = [i for i in tm.inbox_items if i.get("id") not in ids]
            tm.in_flight.extend(dict(i) for i in items)
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
            if _is_human_addr(author):
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
                # main·one-shot 과 같은 함수(`runtime.harness_record`, v10.89.0).
                from agent_cli.runtime import harness_record

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
            tm.handled += max(handled, len(tm.in_flight))
            tm.in_flight = []
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
        return f"unroutable address '{addr}'"

    def _push_reply(self, reply: dict) -> None:
        with self._cv:
            self._stamp_mail(reply)
            self._pending.append(reply)
            self._cv.notify_all()
        self._save_state()  # P3: 미배달분 디스크 미러 — resume 시 유실 없음
        cb = self.on_reply
        if cb is not None:
            try:
                cb(reply)
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
        with self._cv:
            items = [dict(r) for r in self._pending]
        return [mail_queue_item(r) for r in items]

    def _deliver_peer_reply(
        self, requester_key: str, from_key: str, output: str, hop: int, *, re: str = ""
    ) -> str:
        """요청의 회신을 요청자 inbox 로 terminal 재주입 (v5.11) — 에러 또는 "".

        ``from_key`` 는 에이전트 키 또는 ``"main"``(main 이 `reply` 로 갚을 때).
        ``expects_reply=False`` 로 넣어 요청자가 소비만 하고 되받아치지 않게
        한다(핑퐁 방지). 봉투(``reply``)가 무엇에 대한 회신인지(``re``) 적는다
        (v10.77.0). 상한(_MAX_PEER_HOPS) 초과나 요청자 부재/사망 시 드롭 —
        best-effort 배관(데드락은 비동기라 불가).
        """
        if hop >= _MAX_PEER_HOPS:
            return f"reply dropped — peer hop limit ({_MAX_PEER_HOPS}) reached"
        tgt = self._agents.get(requester_key)
        if tgt is None or tgt.state == "dead":
            return f"agent '{requester_key}' is gone — the reply was not delivered"
        return self.request(
            requester_key,
            output,
            author="main" if from_key == "main" else f"agent:{from_key}",
            hop=hop + 1,
            expects_reply=False,
            kind="reply",
            re=re,
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
    ) -> str:
        """상주 에이전트 → main 메시지 (v5.11). main 은 inbox 대신 mailbox
        (_pending)로 받아 턴 경계 관찰로 본다 (peer↔main 대칭).

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
        self._push_reply(rec)
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

    def _make_message_handler(self, tm: AgentInstance):
        """상주 에이전트 서브루프의 ``message`` 도구 라우팅 훅 (v5.11).

        ``message(to, text)`` → 대상 inbox(또는 main mailbox)로 비동기
        전송하고 즉시 반환한다. 대상의 회신은 이 에이전트의 inbox 로 새
        메시지처럼 도착한다(발신자는 블록하지 않음)."""

        def handler(to: str, text: str, expects_reply: bool = True) -> str:
            to = (to or "").strip()
            text = (text or "").strip()
            if not to:
                return 'message needs a `to` agent key or name (or "main")'
            if not text:
                return "empty message — nothing sent"
            if not has_words(text):
                # v10.1.2: 구두점뿐인 메시지(실측 `>`)는 보내지 않는다 — 받은
                # 쪽이 뜻을 찾느라 턴을 쓴다.
                return (
                    f"message has no words ({text[:20]!r}) — nothing sent; say it "
                    "in words"
                )
            if to != "main":
                # v10.1.0: 키·`agent:` 접두·이름 전부 받는다 (resolve_key).
                resolved = self.resolve_key(to)
                if resolved is None:
                    return (
                        f"unknown agent '{to}' — use a key or name from "
                        '`## Live Agents` (or "main")'
                    )
                to = resolved
            if to == tm.key:
                return "cannot message yourself"
            # `message` 는 기본으로 **요청**이다 — 상대에게 회신을 빚지운다
            # (v9.21.0). 내 요청자에게 보내는 것이면 내 빚도 같이 갚는다:
            # "여기 내 수, 이제 네 차례" — 항목 하나로 양쪽이 빚진다(끝말잇기가
            # 성립하는 규칙). 돌려받을 것이 없는 답은 `reply` 다.
            # ``expects_reply=False`` (v10.1.0) 는 **통지**: 상대가 빚지지 않고,
            # 내 요청자에게 보냈다면 내 빚은 갚는다(fire-and-forget). 상태
            # 공유·인수인계·작별 인사가 여기 간다 — 종전엔 이 말들이 전부 요청이
            # 되어 답이 답을 부르는 핑퐁을 만들었다(실측 cnr60g).
            addr = "main" if to == "main" else f"agent:{to}"
            settled = self.settle(f"agent:{tm.key}", addr)
            # 결과가 그 배달의 id 를 말한다 (v10.77.0) — 회신이 같은 id 를 달고
            # 돌아오므로(`re [r-17]`) 보낸 쪽이 무엇에 대한 회신인지 안다.
            if to == "main":
                rec_id = self.message_to_main(
                    tm.key,
                    text,
                    profile=tm.profile_name,
                    name=tm.instance_name,
                    answers=settled.answers if settled is not None else None,
                    expects_reply=bool(expects_reply),
                    notice=not expects_reply,
                )
            else:
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
                    author=f"agent:{tm.key}",
                    expects_reply=bool(expects_reply),
                    item_id=rec_id,
                    kind="" if expects_reply else "notice",
                )
                if err:
                    return err
                self._log_outbound(tm.key, text, to=to, ts=send_ts)  # 발신자 창 out
            if not expects_reply:
                return f"delivered as notice {rec_id} — nothing is owed back"
            return (
                f"delivered as request {rec_id} — its reply will come marked [{rec_id}]"
            )

        return handler

    def drain_replies(self) -> list[dict]:
        """미배달 회신 전량 회수 (턴 경계 배달 — D2). 도착 순서 유지."""
        with self._cv:
            out = list(self._pending)
            self._pending.clear()
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
            self._mailboxes.setdefault(addr, []).append(mail)
            self._cv.notify_all()

    def take_mail(self, addr: str) -> list[dict]:
        """private 메일함을 비우며 전부 돌려준다 — 루프의 ``absorb_inbox`` 포트가 턴
        경계마다 부른다. 도착 순서."""
        with self._cv:
            return list(self._mailboxes.pop(addr, []))

    def wait_mail(
        self, addr: str, *, stop_event, still_waiting, poll_s: float = 0.5
    ) -> str:
        """메일이 올 때까지 막는다 — one-shot 의 `wait` (`dispatch._op_wait`). 돌려주는
        값은 깬 이유: ``"mail"``(도착), ``"stopped"``(``stop_event``), ``"nothing"``
        (``still_waiting()`` 이 거짓 — 기다리던 것이 메일 없이 사라졌다: 잡을 밖에서
        죽임, 모니터 삭제). 상한은 없다 — 중간 깨우기가 메일로 와서 모델이 판단한다."""
        while True:
            with self._cv:
                if self._mailboxes.get(addr):
                    return "mail"
                self._cv.wait(timeout=poll_s)
                if self._mailboxes.get(addr):
                    return "mail"
            if stop_event is not None and stop_event.is_set():
                return "stopped"
            if not still_waiting():
                return "nothing"

    def drop_mailbox(self, addr: str) -> int:
        """private 주소의 루프가 끝났다 — 체크인을 풀고 메일함을 버린다(읽을 사람이
        없다). 버린 메일 수를 돌려준다."""
        self._cancel_checkins(addr)
        with self._cv:
            self._checkin_last.pop(addr, None)
            return len(self._mailboxes.pop(addr, []))

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
            return 'no live agents. Spawn one with `mode` "spawn".'
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
                line += STATUS_DEAD_SUFFIX
            lines.append(line)
        with self._cv:
            if self._pending:
                lines.append(f"(undelivered replies: {len(self._pending)})")
        # 폴링 차단 넛지: working 이거나 미처리 inbox 가 있으면 모델이
        # status 를 돌려 기다리는 상황일 가능성이 높다 — 회신은 mail 로
        # 자동 배달되고 도착 시 harness 가 깨우므로(폴링 금지 설계),
        # `wait` 로 턴을 마치는 게 올바른 대기다(v10.65.0). 전부 idle 인 로스터
        # 확인용 status 에는 붙이지 않는다(노이즈).
        waiting = any(
            self.state_is_active(tm.state) or tm.inbox.qsize() > 0 for tm in items
        )
        if waiting:
            lines.append(STATUS_WAIT_NUDGE)
        return "\n".join(lines)

    def kill(self, key: str) -> str:
        """종료 요청 — 성공 시 빈 문자열. 멱등 (이미 dead 여도 성공)."""
        tm = self._agents.get(key)
        if tm is None:
            return f"unknown agent '{key}'"
        tm.revivable = False  # P3: 명시 kill 은 영구 — resume 이 되살리지 않음
        self._cancel_checkins(f"agent:{key}")  # 죽은 에이전트에게 알림이 가지 않게
        # 감시 폐기는 `stop_event.set()` **앞**이다. busy 에이전트는 stop 뒤에도
        # 현재 턴을 마저 돌고(join 은 best-effort), 그 창에 발화하면 항목이
        # `_SHUTDOWN` 뒤에 줄 서서 영영 안 읽힌다. 그 턴에서 새로 건 감시는
        # 워커의 `finally` 가 잡는다 — 죽음이 확정되는 유일한 지점이다.
        self._drop_monitors(tm.key)
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
        self._cancel_checkins()  # main 포함 — 끝나는 세션에 알림이 쌓이지 않게
        for tm in list(self._agents.values()):
            self._drop_monitors(tm.key)
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
                    # v9.24.5: 줄 선 요청과 처리 중이던 요청 — 재시작이 되살린다.
                    # 장부 밖 메일(``ledger: "none"``)은 "지금" 에만 참이라 남기지
                    # 않는다 (docs/ledger/DESIGN.md §14).
                    "inbox": [dict(i) for i in list(tm.inbox_items) if _persists(i)],
                    "in_flight": [dict(i) for i in list(tm.in_flight)],
                }
            )
        with self._cv:
            pending = [dict(r) for r in self._pending if _persists(r)]
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
                self._stamp_mail(item)
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
            # 번호 잇기 (v10.77.0) — 할당기는 저장하지 않고 남은 id 를 본다
            item_ids.observe(
                *(i.get("item_id") for i in self._pending),
                *(q.id for q in restored),
            )

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
                home_dir=self.session_dir / AGENTS_DIR / key,
                instance_name=e.get("name", ""),
                description=e.get("description", ""),
                instructions=e.get("instructions", ""),
            )
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
            self._requeue(tm, in_flight, queued)
            revived += 1
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
            if first.get("resumed"):
                rest.insert(0, first)
            else:
                self.request(
                    tm.key,
                    RESUMED_RUN_NOTICE + str(first.get("text") or ""),
                    author=str(first.get("author") or "main"),
                    hop=int(first.get("hop") or 0),
                    expects_reply=bool(first.get("expects_reply", True)),
                    answers=first.get("answers"),
                    resumed=True,
                    kind=str(first.get("kind") or ""),
                    re=str(first.get("re") or ""),
                    todo=str(first.get("todo") or ""),
                )
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
                self._questions[q.id] = q
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
                    self._begin_items(tm, batch)
                    kind.run(kind.start(batch))
                else:
                    self._begin_items(tm, [item])
                    kind.run(kind.start([item]))
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
            # 감시 폐기의 **정본 위치** — kill·크래시·세션 종료가 모두
            # 여기로 수렴하고, 여기가 죽음이 확정되는 곳이다. kill/
            # shutdown_all 의 조기 드롭은 그 앞 창을 좁힐 뿐이다.
            from agent_cli.runtime import release_owner

            release_owner(
                f"agent:{tm.key}", jobs=self.jobs, monitors=self.monitors, registry=self
            )
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


# ── `agent` 결과·거절 문구 (v10.85.0, agent 검토 PR B) ──────────────────────────
# 모듈 상수인 이유: ``prompts.inventory`` 가 표본으로 읽어 표기 가드(도구·인자 백틱,
# 값 큰따옴표, 호출 모양 없음, 대문자 강조 없음)가 여기까지 본다 — v10.77/10.81 규칙
# 위반 7곳·11낱말이 가드 밖이라 남아 있었다. 결과는 **사실만** 말한다(키·요청 id·
# "회신이 깨운다"); "기다리는 법"은 꼬리 `## Live Agents` 머리글이 상시로 맡고,
# 폴링 금지는 `status` 의 넛지가 맡는다(세 겹 → 한 겹, v10.73.0 자리 원칙).
SPAWN_DEAD_SAME_PROFILE = (
    "note: dead agent(s) with the same profile exist ({keys}) — this new spawn "
    "starts with no memory of them; to continue one of them instead, kill this "
    'and bring that one back with `mode` "resume".'
)
SPAWN_TASK_QUEUED = (
    "initial task queued as request {rid} — its `reply` comes back marked [{rid}] "
    "and wakes you (if it completes without one you get its run summary instead)."
)
SPAWN_NO_TASK = 'send work with `mode` "request", `key` "{key}" and the `task` text.'
SPAWN_PEERS = (
    "Peers reach it with their `message` tool as {who}; they see the live roster "
    "in their own per-turn tail, so you need not relay keys."
)
REQUEST_QUEUED = (
    "queued to {key} as request {rid} — its `reply` comes back marked [{rid}] at a "
    "later turn (even while you are idle) and wakes you; if it completes without "
    "one you get its run summary."
)
REQUEST_STACKED = (
    "\n⚠ it now has {n} queued requests — if these are progress checks or re-sends "
    "of the same ask, that is interference: each one queues more work and delays "
    "the answer. Its `reply` reaches you on its own; stop re-sending."
)
RESUME_DONE = (
    "agent '{key}' resumed — it remembers all previous exchanges (its context was "
    "kept across death)."
)
RESUME_TASK_QUEUED = (
    "task queued as request {rid} — its `reply` comes back marked [{rid}] and "
    "wakes you."
)
KILL_DONE = (
    "agent '{key}' terminated. Its context is kept — `mode` \"resume\" brings it "
    "back with full memory; a new spawn would start without it."
)
KILL_ALREADY = (
    "agent '{key}' was already terminated — nothing to do. Its context is kept; "
    '`mode` "resume" brings it back with full memory.'
)
STATUS_DEAD_SUFFIX = ' | resumable with `mode` "resume"'
STATUS_WAIT_NUDGE = (
    "⏳ Replies arrive as agent mail and wake you — finish this turn with `wait` "
    "instead of polling `status`."
)
DUPLICATE_DEAD = (
    "'{label}' already exists (dead) as {key} — bring it back with `mode` "
    '"resume" instead of spawning a duplicate, or spawn with a distinct `name`.'
)
DUPLICATE_ALIVE = (
    "'{label}' is already running as {key} — send it work with `mode` "
    '"request", or spawn with a distinct `name` if you really need a second '
    "instance."
)
NO_REGISTRY = (
    "persistent agent modes ({modes}) are main-session only — in this loop use "
    '`mode` "run" for a one-shot sub-agent instead.{hint}'
)
NO_REGISTRY_PEER_HINT = (
    ' To reach a peer agent or main, use `message` (`to` = its key or name, or "main").'
)


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
            lines.append(SPAWN_DEAD_SAME_PROFILE.format(keys=", ".join(dead_same_role)))
    task = args.get("task", "")
    if task:
        rid = item_ids.next_id(item_ids.REPLY)
        err = registry.request(key, task, item_id=rid)
        if err:
            lines.append(f"initial task not queued: {err}")
        else:
            lines.append(SPAWN_TASK_QUEUED.format(rid=rid))
    else:
        lines.append(SPAWN_NO_TASK.format(key=key))
    # v10.1.0: peer 채널 안내 — 상주 에이전트끼리는 `message` 로 부르고(키나
    # 이름), 명단은 그들의 매 턴 꼬리에 있다. 실측(mgv4en): main 이 모르면
    # 플레이어들에게 `agent(mode="request")` 를 가르쳐 한 턴씩 낭비됐다.
    _name = str(args.get("name", "") or "")
    who = f"'{_name}' or key {key}" if _name else f"key {key}"
    lines.append(SPAWN_PEERS.format(who=who))
    return ToolResult(True, output="\n".join(lines))


def _agent_request(registry, args: dict, *, parent_ctx, runtime) -> ToolResult:
    key = args.get("key", "")
    tm = registry.get(key)
    # request 는 언제나 **일감**이다. id 를 먼저 받아 결과에 싣는다(v10.77.0) —
    # 회신이 같은 id 를 달고 온다(`re [r-7]`).
    rid = item_ids.next_id(item_ids.REPLY)
    err = registry.request(key, args.get("task", ""), item_id=rid)
    if err:
        return ToolResult(False, error=f"request rejected: {err}")
    backlog = tm.inbox.qsize() if tm is not None else 0
    stacked = REQUEST_STACKED.format(n=backlog) if backlog >= 2 else ""
    return ToolResult(True, output=REQUEST_QUEUED.format(key=key, rid=rid) + stacked)


def _agent_resume(registry, args: dict, *, parent_ctx, runtime) -> ToolResult:
    key = args.get("key", "")
    err = registry.resume_teammate(key, parent_ctx=parent_ctx)
    if err:
        return ToolResult(False, error=f"resume rejected: {err}")
    lines = [RESUME_DONE.format(key=key)]
    task = args.get("task", "")
    if task:
        # spawn·request 처럼 요청 id 를 먼저 받아 결과에 싣는다 — 회신이 같은 id 로 온다.
        rid = item_ids.next_id(item_ids.REPLY)
        qerr = registry.request(key, task, item_id=rid)
        if qerr:
            lines.append(f"task not queued: {qerr}")
        else:
            lines.append(RESUME_TASK_QUEUED.format(rid=rid))
    return ToolResult(True, output="\n".join(lines))


def _agent_status(registry, args: dict, *, parent_ctx, runtime) -> ToolResult:
    return ToolResult(True, output=registry.format_status(args.get("key", "")))


def _agent_kill(registry, args: dict, *, parent_ctx, runtime) -> ToolResult:
    key = args.get("key", "")
    tm = registry.get(key)
    if tm is not None and tm.state == "dead":
        # 멱등은 그대로(v10.84.0) — 다만 문장이 반복을 알려 준다: 벤치에서 35B 가
        # 같은 키에 kill 을 일곱 번 보내며 매번 같은 "terminated" 를 받았다.
        registry.kill(key)
        return ToolResult(True, output=KILL_ALREADY.format(key=key))
    err = registry.kill(key)
    if err:
        return ToolResult(False, error=f"kill rejected: {err}")
    return ToolResult(True, output=KILL_DONE.format(key=key))


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
    resident: bool = False,
) -> ToolResult:
    """teammate 도구 mode 디스패치 — ``_MODE_HANDLERS`` 테이블 경유.
    delegate 처럼 루프가 인터셉트해 provider/ctx 배선(runtime)을 주입한다.

    ``resident`` (v10.1.0): 호출한 루프가 상주 에이전트인가 — 거절 문구가
    peer 채널(`message`)을 가리킨다. 실측(mgv4en): main 이 플레이어들에게
    `agent(mode="request")` 로 서로 부르라고 지시했고, 둘이 각각 한 턴을
    "main-session only" 에 썼다 — 종전 문구는 `run` 만 가리켜 길을 안 알려줬다.
    """
    from agent_cli.tools.agent_tool import REGISTRY_MODES

    if registry is None:
        hint = NO_REGISTRY_PEER_HINT if resident else ""
        return ToolResult(
            False,
            error=NO_REGISTRY.format(modes="/".join(REGISTRY_MODES), hint=hint),
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
            error=(f'unknown `mode` "{mode}" — use ' + " / ".join(_MODE_HANDLERS)),
        )
    return handler(registry, args, parent_ctx=parent_ctx, runtime=runtime)
