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
import os
import re
import threading
import time
import uuid
from pathlib import Path
from queue import SimpleQueue
from typing import TYPE_CHECKING, Callable

from agent_cli.tools.result import ToolResult

if TYPE_CHECKING:
    from agent_cli.context.manager import ContextManager

# worker 를 inbox 블록에서 깨워 종료시키는 sentinel (identity 비교).
_SHUTDOWN = object()

_DEFAULT_MAX_AGENTS = 4

# agents.json 스키마 버전 — 비호환 변경 시 bump (구버전 파일은 무시=fresh).
AGENTS_STATE_VERSION = 1

# ── Live Teammates 시스템 프롬프트 리로드 플래그 ──
# 멤버십(spawn/kill/died/restore/auto_spawn) 변화 시 set — 루프의
# _execute_turn 이 directives/memory 플래그와 같은 자리에서 consume 해
# 다음 턴 시스템 프롬프트를 재조립한다 (상태 전이 busy/idle 은 안 건드림
# — KV 캐시 프리픽스를 의미 있는 사건에만 버스트).
_membership_changed = threading.Event()


def notify_agents_changed(registry=None) -> None:
    """멤버십 변화 신호. 플래그(다음 턴 실제 재조립)에 더해, ``registry``
    가 주어지면 인스펙터의 main 스냅샷 Live Teammates 섹션을 **즉시**
    갱신한다 — idle 중 대화 창 kill 도 인스펙터에 바로 반영 (v4.61.0)."""
    _membership_changed.set()
    if registry is None:
        return
    try:
        from agent_cli.prompts.system_prompt import build_live_agents_section
        from agent_cli.render import get_renderer

        get_renderer().update_prompt_section(
            "", "Live Agents", build_live_agents_section(registry)
        )
    except Exception:
        pass  # 표시용 — 실행 경로를 막지 않는다


def consume_agents_reload() -> bool:
    if _membership_changed.is_set():
        _membership_changed.clear()
        return True
    return False


def compose_role_prompt(profile_body: str, instructions: str) -> str:
    """instant-agent 합성 규칙 (U4): 파일(일반) → 인라인(구체) 순.

    인라인이 뒤(recency)라 세션-특정 지시가 우선 효과를 갖는다. 둘 중
    하나만 있으면 그것만, 둘 다 없으면 빈 문자열(익명 generalist).
    """
    parts = [p for p in (profile_body.strip(), instructions.strip()) if p]
    if len(parts) == 2:
        return f"{parts[0]}\n\n## Additional instructions\n{parts[1]}"
    return parts[0] if parts else ""


def format_agent_label(key: str, profile: str = "", name: str = "") -> str:
    """표시 라벨 — 다중 인스턴스 구분: "agt-x (coder · ui)"."""
    parts = [p for p in (profile, name) if p]
    return f"{key} ({' · '.join(parts)})" if parts else key


def _max_agents() -> int:
    try:
        return int(os.environ.get("AGENT_CLI_MAX_AGENTS", _DEFAULT_MAX_AGENTS))
    except ValueError:
        return _DEFAULT_MAX_AGENTS


def build_reply_record(reply: dict, *, cap: int = 0) -> dict:
    """mailbox 아이템 1건(회신 또는 질문) → main ctx 에 넣을 관찰 레코드.

    ``kind:"question"`` (P2, ask→main 라우팅): teammate 가 ask 로 물은
    질문 — teammate 는 답변까지 블록되므로 request 로 답하라는 안내를
    붙인다. 그 외(kind:"reply"/부재)는 회신.

    ``cap``(loop 의 ``_oversized_cap``, 0=무제한) 초과 회신은 전문 대신
    디스크 포인터 + head 발췌로 치환 — 전문은 worker 가 이미
    ``teammates/<key>/replies/reply-<seq>.md`` 에 영속했다(배달과 무관하게
    항상 저장 — P3 resume 미배달 보존의 토대).
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
        question = reply.get("output") or "(empty question)"
        if reply.get("stale"):
            # P3: 세션 재시작으로 teammate 는 더 이상 답변 대기가 아님 —
            # 그대로 답하면 새 request 로 오인되므로 정직하게 알린다.
            tail = (
                "(STALE — the session was resumed; the agent is NO LONGER "
                "waiting for this answer. Re-send the original request with "
                f'{{"mode":"request","key":"{key}","message":"..."}} if still needed.)'
            )
        else:
            tail = (
                "(The agent is BLOCKED until answered. Answer via agent op: "
                f'{{"mode":"request","key":"{key}","message":"<your answer>"}}.)'
            )
        content = f"── agent {label} QUESTION ──\n{question}\n{tail}"
        return {
            "role": "user",
            "tool": "agent",
            "success": True,
            "content": content,
            "source": "agent_question",
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
        subscriptions: list[str] | None = None,
    ):
        self.key = key
        self.profile_name = profile_name
        self.role_prompt = role_prompt
        # 다중 인스턴스 구분 라벨 (예: 같은 coder 역할의 "ui"/"api") — 주소는
        # 항상 key, name 은 표시·광고용.
        self.instance_name = instance_name
        # 역할 md 의 description — Live Teammates 광고의 전문영역 요약.
        self.description = description
        # instant-agent (U4): spawn 시 인라인으로 받은 추가 지시 원본 —
        # 합성 결과는 role_prompt 에 이미 들어있고, 이 필드는 manifest
        # 영속·인스펙터 가시성용.
        self.instructions = instructions
        # 도구 이벤트 구독 (5.8.0): main 루프의 해당 도구 실행이 턴 경계에
        # 배치 1건으로 inbox 에 들어온다. "*" = 전체 (학습/기록 모듈용),
        # "complete" 같은 가상 도구도 구독 가능 (task reviewer).
        self.subscriptions = list(subscriptions or [])
        self.allowed_tools = allowed_tools
        self.model = model
        self.hooks_config = hooks_config
        self.context_mode = context_mode
        self.home_dir = home_dir

        self.inbox: SimpleQueue = SimpleQueue()
        self.stop_event = threading.Event()
        self.worker: threading.Thread | None = None
        self.ctx: ContextManager | None = None

        self.state = "starting"  # starting | idle | busy | waiting_ask | dead
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
            "subscriptions": list(self.subscriptions),
            "est_tokens": est_tokens,
            "error": self.error,
        }


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
    ):
        # 회신을 처리할 provider/모델 등 실행 배선 — spawn 시점이 아니라
        # 레지스트리 생성 시점(부트스트랩)에 고정할 수도 있으나, provider
        # 는 tool_bridge 인터셉트에서만 완전하므로 spawn 마다 갱신 수용.
        self.runtime = runtime or {}
        self._runner = runner
        self.session_dir = Path(session_dir) if session_dir else None

        self._agents: dict[str, AgentInstance] = {}
        self._cv = threading.Condition()
        self._pending: list[dict] = []  # 미배달 회신 (도착 순서)
        # 회신 도착 알림 (CLI 📨 라인 / web transient status) — 부트스트랩 주입.
        self.on_reply: Callable[[dict], None] | None = None

    # ── 조회 ────────────────────────────────────

    def get(self, key: str) -> AgentInstance | None:
        return self._agents.get(key)

    def roster_snapshot(self) -> list[dict]:
        return [tm.snapshot() for tm in self._agents.values()]

    def _notify_roster(self) -> None:
        """P4: 상태 변화를 대화 창 목록에 반영 — web sticky, CLI no-op."""
        from agent_cli.render import get_renderer

        try:
            get_renderer().agent_roster(self.roster_snapshot())
        except Exception:
            pass  # 표시용 — 실행 경로를 막지 않는다

    def alive_count(self) -> int:
        return sum(1 for t in self._agents.values() if t.state != "dead")

    def has_pending_replies(self) -> bool:
        with self._cv:
            return bool(self._pending)

    def has_active_work(self) -> bool:
        """P5: CLI run 큐 펌프의 정지 판정 — 미배달 회신·처리 중(busy)·
        큐잉된 요청이 하나라도 있으면 True. **waiting_ask 는 제외**:
        main 이 답하지 않기로 한 질문을 기다리는 건 영원히 안 끝나는
        교착이라, 펌프는 경고 후 종료(shutdown 이 대기를 푼다)를 택한다."""
        if self.has_pending_replies():
            return True
        for tm in self._agents.values():
            if tm.state == "busy" or tm.inbox.qsize() > 0:
                return True
        return False

    def waiting_ask_keys(self) -> list[str]:
        """답변 대기 중인 teammate — 펌프 종료 시 경고 표시용."""
        return [tm.key for tm in self._agents.values() if tm.state == "waiting_ask"]

    # ── spawn ───────────────────────────────────

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
        subscribe: list[str] | None = None,
    ) -> tuple[str, str]:
        """teammate 생성 — ``(key, error)``. 성공 시 error 는 빈 문자열.

        역할 로드·fork 전제 검사는 여기서 동기로 (즉시 거부), ctx 생성은
        worker 스레드에서 (인스펙터 스코프가 worker 스레드 키라서 — D9).
        """
        if runtime:
            self.runtime = runtime

        if self.alive_count() >= _max_agents():
            return "", (
                f"agent limit reached ({_max_agents()} alive). "
                f'Kill one first (mode:"kill") or raise AGENT_CLI_MAX_AGENTS.'
            )

        if name and not re.match(r"^[a-zA-Z0-9_-]{1,24}$", name):
            return "", (f"invalid instance name '{name}': [a-zA-Z0-9_-], max 24 chars")

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
            allowed_tools, model, hooks_config = apply_role_overrides(
                config,
                allowed_tools=allowed_tools,
                model=model,
                hooks_config=hooks_config,
            )

        # 도구 이벤트 구독 (5.8.0): 명시 파라미터 > 프로파일 frontmatter
        # `subscribes` > 없음. 문자열 리스트만 수용 (도구명 or "*").
        profile_subs = config.get("subscribes") if profile else None
        subscriptions = subscribe if subscribe is not None else profile_subs
        if subscriptions is not None and (
            not isinstance(subscriptions, list)
            or not all(isinstance(s, str) and s for s in subscriptions)
        ):
            return "", "invalid subscribe: expected a list of tool names"

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
            subscriptions=subscriptions,
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
        notify_agents_changed(self)  # Live Teammates 재조립+인스펙터 즉시 반영
        return key, ""

    # ── request / 회신 ──────────────────────────

    def request(self, key: str, message: str, *, author: str = "main") -> str:
        """request 큐잉 — 성공 시 빈 문자열, 실패 시 에러 메시지."""
        tm = self._agents.get(key)
        if tm is None:
            return f"unknown agent '{key}' (see mode:\"status\" for live keys)"
        if tm.state == "dead":
            reason = f" ({tm.error})" if tm.error else ""
            return f"agent '{key}' is dead{reason} — spawn a new one"
        if not message.strip():
            return "empty message"
        tm.queued += 1
        tm.inbox.put({"seq": tm.queued, "text": message, "author": author})
        from agent_cli.render import get_renderer

        get_renderer().agent_message(
            key=key, direction="in", author=author, text=message, seq=tm.queued
        )
        self._notify_roster()
        return ""

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

    def drain_replies(self) -> list[dict]:
        """미배달 회신 전량 회수 (턴 경계 배달 — D2). 도착 순서 유지."""
        with self._cv:
            out = list(self._pending)
            self._pending.clear()
        if out:
            self._save_state()  # 배달 완료 → 디스크 미러도 소비
        return out

    # ── status / kill / 종료 ────────────────────

    def wants_tool_events(self) -> bool:
        """구독자가 하나라도 살아 있나 — 루프 탭의 저비용 게이트."""
        return any(
            tm.subscriptions and tm.state != "dead" for tm in self._agents.values()
        )

    def publish_tool_events(self, events: list[dict], *, turn: int = 0) -> int:
        """main 턴의 도구 이벤트를 구독자별 **배치 1건**으로 팬아웃 (5.8.0).

        events 항목: ``{tool, summary, body, success}``. 구독 매칭 =
        도구명 일치 or ``"*"``. 메시지는 author="watch" 로 큐잉 —
        회신 라우팅은 main 과 같되 **LGTM 회신은 main 비배달**(창 전용,
        무발견 노이즈가 컨텍스트를 먹지 않게). 발행 건수 반환.
        """
        if not events:
            return 0
        published = 0
        for tm in list(self._agents.values()):
            if tm.state == "dead" or not tm.subscriptions:
                continue
            subs = set(tm.subscriptions)
            hits = [e for e in events if "*" in subs or e.get("tool") in subs]
            if not hits:
                continue
            lines = [f"[tool-events] main 턴 {turn} 의 구독 도구 실행 {len(hits)}건:"]
            for i, e in enumerate(hits, 1):
                mark = "✓" if e.get("success") else "✗"
                head = f"{i}. {mark} {e.get('tool')}"
                if e.get("summary"):
                    head += f" {e['summary']}"
                lines.append(head)
                body = (e.get("body") or "").strip()
                if body:
                    lines.append(body)
            lines.append(
                '— 조치/발견이 없으면 정확히 "LGTM" 한 줄로만 회신하세요 '
                "(LGTM 회신은 main 에 배달되지 않습니다). 발견이 있으면 "
                "구체적으로(예: 심각도+file:line) 회신하세요. 전문이 필요하면 "
                "파일을 직접 읽으세요."
            )
            error = self.request(tm.key, "\n".join(lines), author="watch")
            if not error:
                published += 1
        return published

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
        lines = [f"agents ({self.alive_count()}/{_max_agents()} alive):"]
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
            if s.get("subscriptions"):
                line += f" | watching: {', '.join(s['subscriptions'])}"
            if s["error"]:
                line += f" | error: {s['error']}"
            if s["state"] == "dead":
                line += ' | resumable via {"mode":"resume","key":"%s"}' % s["key"]
            lines.append(line)
        with self._cv:
            if self._pending:
                lines.append(f"(undelivered replies: {len(self._pending)})")
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
        self._save_state()
        self._notify_roster()
        notify_agents_changed(self)
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
        if self.alive_count() >= _max_agents():
            return (
                f"agent limit reached ({_max_agents()} alive). "
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
        self._save_state()
        self._notify_roster()
        notify_agents_changed(self)
        return ""

    def auto_spawn(self, parent_ctx=None) -> int:
        """frontmatter ``auto-spawn: true`` 역할을 세션 시작 시 자동 상주
        (전문가 팀 확장). restore() **이후에** 호출 — 같은 역할의 살아있는
        teammate(재생성분)가 이미 있으면 중복 스폰하지 않는다. 스폰 수 반환."""
        from agent_cli.subagent.profiles import available_profiles

        live_roles = {
            tm.profile_name for tm in self._agents.values() if tm.state != "dead"
        }
        spawned = 0
        for name, meta in available_profiles(include_meta=True):
            if not meta.get("auto-spawn"):
                continue
            if name in live_roles:
                continue
            key, error = self.spawn(profile=name, parent_ctx=parent_ctx)
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
        for tm in self._agents.values():
            entries.append(
                {
                    "key": tm.key,
                    "profile": tm.profile_name,
                    "name": tm.instance_name,
                    "description": tm.description,
                    "instructions": tm.instructions,
                    "subscriptions": list(tm.subscriptions),
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
        try:
            from agent_cli.fsio import atomic_write_json

            atomic_write_json(
                path,
                {
                    "version": AGENTS_STATE_VERSION,
                    "agents": entries,
                    "pending": pending,
                },
            )
        except OSError:
            pass

    def restore(self, parent_ctx=None) -> int:
        """세션 resume 시 agents.json 에서 재생성 — 되살린 수 반환.

        - 살아있던(revivable) teammate: 자기 history 를 resume 한 ctx 로
          worker 재기동 (이전 문답 전부 기억). kill 된 것은 dead 툼스톤으로
          만 복원 (status 가시성).
        - 미배달 pending 은 그대로 복원 → 첫 턴 경계에 정상 배달. 단
          질문(kind:"question")은 **stale 마킹** — 재시작으로 teammate 가
          더 이상 답변 대기가 아니므로 배달 문구가 재요청을 안내.
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
                if item.get("kind") == "question":
                    item["stale"] = True
                self._pending.append(item)
            if self._pending:
                self._cv.notify_all()

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
                subscriptions=e.get("subscriptions") or [],
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
            revived += 1
        self._save_state()
        self._notify_roster()
        if revived:
            notify_agents_changed(self)
        return revived

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
            ctx, err = create_subagent_ctx(mode, parent_ctx, tm.home_dir)
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

            while not tm.stop_event.is_set():
                item = tm.inbox.get()
                if item is _SHUTDOWN or tm.stop_event.is_set():
                    break
                tm.state = "busy"
                seq = item["seq"]
                text = item["text"]
                # 화자 attribution — teammate ctx 에 누가 말했는지 남긴다
                # (P2 양방향·P4 인간 개입에서 두 화자를 구분하는 기반).
                author = item.get("author", "main")
                tm.current_author = author  # 회신/질문 라우팅 기준 (D8)
                self._notify_roster()
                query = f"[{author}]: {text}" if author != "main" else text

                renderer.begin_agent_work(
                    key=tm.key, seq=seq, profile=_disp, message=text
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
                reply_path = self._persist_reply(tm, seq, output)
                tm.handled += 1
                tm.state = "idle"
                # 대화 창에는 화자 불문 항상 표시 (P4).
                renderer.agent_message(
                    key=tm.key,
                    direction="out",
                    author=tm.key,
                    text=output,
                    seq=seq,
                    success=success,
                    to=author,  # 수신자 — @agt 명령/창 개입이면 user:* (D8)
                )
                deliver = author == "main" or (
                    # watch(구독 이벤트): 발견 회신만 main 으로 — "LGTM" 은
                    # 창 전용 (무발견 노이즈가 매 턴 컨텍스트를 먹지 않게).
                    author == "watch"
                    and not (output or "").strip().upper().startswith("LGTM")
                )
                if deliver:
                    # main 발신 요청의 회신만 main mailbox 로 (D8 — 인간
                    # 개입 문답은 창에만, main 컨텍스트 비오염).
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
                        }
                    )
                else:
                    self._save_state()  # push 를 건너뛰어도 상태는 미러
                tm.current_author = "main"
                self._notify_roster()
        except BaseException as e:  # noqa: BLE001 — 루프 기계 자체의 사망 포착
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
            renderer.end_prompt_scope(tm.key)  # 스코프 고정 (사후 검사 가능)
            self._save_state()  # ctx 실패(error→dead)·종료 상태 반영
            self._notify_roster()
            notify_agents_changed(self)  # 사망도 멤버십 변화 — 광고에서 제거

    def _make_ask_handler(self, tm: AgentInstance):
        """teammate 서브루프의 ask 라우팅 훅 (P2, D4).

        teammate 가 ask 를 부르면: 질문을 main mailbox 에 올리고
        (턴 경계 배달 — main 은 관찰로 받는다), 이 teammate 의 inbox 에
        **다음으로 도착하는 메시지를 답변으로 소비**한다 — "도착 순서가
        답" (main 의 request 든, P4 의 인간 개입이든 먼저 온 쪽).
        kill/세션 종료(_SHUTDOWN)는 답변 대기를 즉시 풀고 sentinel 을
        재게시해 바깥 worker 루프도 종료되게 한다.
        """

        def handler(question: str) -> str:
            from agent_cli.render import get_renderer

            # 대화 창에는 항상 (P4) — 인간이 먼저 답할 수 있는 표면.
            get_renderer().agent_message(
                key=tm.key,
                direction="question",
                author=tm.key,
                text=question,
                to=tm.current_author,  # 인간 발신 작업의 질문은 그 사람에게
            )
            if tm.current_author in ("main", "watch"):
                # main/구독 발신 작업의 질문만 main mailbox 로 (D8 대칭).
                self._push_reply(
                    {
                        "kind": "question",
                        "key": tm.key,
                        "profile": tm.profile_name,
                        "name": tm.instance_name,
                        "success": True,
                        "output": question,
                    }
                )
            tm.state = "waiting_ask"
            self._notify_roster()
            try:
                item = tm.inbox.get()
                if item is _SHUTDOWN:
                    tm.inbox.put(_SHUTDOWN)  # 바깥 루프의 몫으로 재게시
                    return "(no response — agent is being terminated)"
                author = item.get("author", "main")
                text = item.get("text", "")
                return f"[{author}]: {text}" if author != "main" else text
            finally:
                tm.state = "busy"
                self._notify_roster()

        return handler

    def _run_message(self, tm: AgentInstance, query: str):
        """request 1건 실행 — 실제 러너 또는 테스트 주입 러너."""
        runner = self._runner
        if runner is None:
            from agent_cli.subagent.runner import run_subagent_message

            runner = run_subagent_message
        rt = self.runtime
        return runner(
            query,
            tm.ctx,
            ask_handler=self._make_ask_handler(tm),
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
        self.idle = threading.Event()  # worker 가 dequeue 대기 중인 구간

    def _arm(self) -> None:
        if not self._armed:
            self._armed = True
            self._enqueue(None, self.WAKE_TEXT)

    def on_mail(self) -> None:
        """registry.on_reply 에서 — idle 일 때만 wake."""
        if self.idle.is_set():
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
        self._armed = False
        return "run" if self._has_pending() else "skip"


# ── LLM 도구 진입점 (tool_bridge 인터셉트) ──────


def tool_agent(
    args: dict,
    *,
    registry: AgentRegistry | None,
    parent_ctx=None,
    runtime: dict | None = None,
) -> ToolResult:
    """teammate 도구 mode 디스패치. delegate 처럼 루프가 인터셉트해
    provider/ctx 배선(runtime)을 주입한다."""
    if registry is None:
        return ToolResult(
            False,
            error=(
                "persistent agent modes (spawn/request/status/resume/kill) are "
                'main-session only — in this loop use {"mode":"run","task":...} '
                "for a one-shot sub-agent instead"
            ),
        )

    # P3: 매 호출 runtime 갱신 — restore 로 되살아난 teammate(스폰 없음)도
    # 첫 request 부터 현재 세션의 provider 배선으로 돈다.
    if runtime:
        registry.runtime = runtime

    mode = args.get("mode", "")

    if mode == "spawn":
        key, error = registry.spawn(
            profile=args.get("profile", ""),
            name=args.get("name", ""),
            instructions=args.get("instructions", ""),
            allowed_tools=args.get("tools"),
            context_mode=args.get("context", "none"),
            parent_ctx=parent_ctx,
            runtime=runtime,
            subscribe=args.get("subscribe"),
        )
        if error:
            return ToolResult(False, error=f"spawn rejected: {error}")
        _parts = [p for p in (args.get("profile", ""), args.get("name", "")) if p]
        lines = [
            f"spawned agent '{key}'" + (f" ({' · '.join(_parts)})" if _parts else "")
        ]
        # 같은 역할의 dead 가 있으면 알려준다 — "다시 시작" 의도였다면
        # 기억을 보존하는 길은 resume 이었음을 다음 선택부터 반영하도록.
        profile_arg = args.get("profile", "")
        if profile_arg:
            dead_same_role = [
                tm.key
                for tm in registry._agents.values()
                if tm.profile_name == profile_arg
                and tm.state == "dead"
                and tm.key != key
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
                    "initial task queued — the reply will be delivered "
                    "automatically as an observation when ready."
                )
        else:
            lines.append(
                'send work with {"mode":"request","key":"' + key + '","message":"..."}.'
            )
        return ToolResult(True, output="\n".join(lines))

    if mode == "request":
        key = args.get("key", "")
        tm = registry.get(key)
        was_waiting = tm is not None and tm.state == "waiting_ask"
        err = registry.request(key, args.get("message", ""))
        if err:
            return ToolResult(False, error=f"request rejected: {err}")
        if was_waiting:
            # P2: 질문 대기 중이던 teammate — 이 메시지가 답변으로 소비되고
            # 원래 작업이 재개된다 (표시용 힌트 — 도착 순서가 진실).
            return ToolResult(
                True,
                output=(
                    f"delivered to {key} as the answer to its pending question — "
                    f"it resumes the original request now."
                ),
            )
        return ToolResult(
            True,
            output=(
                f"queued to {key} — the reply will be delivered to you "
                f"automatically at a later turn (even while you are idle). "
                f"Keep working on other things, or complete if nothing else "
                f"remains — you will be woken when it arrives."
            ),
        )

    if mode == "resume":
        key = args.get("key", "")
        err = registry.resume_teammate(key, parent_ctx=parent_ctx)
        if err:
            return ToolResult(False, error=f"resume rejected: {err}")
        lines = [
            f"agent '{key}' resumed — it remembers ALL previous exchanges "
            f"(its context was preserved across death)."
        ]
        task = args.get("task", "")
        if task:
            qerr = registry.request(key, task)
            if qerr:
                lines.append(f"task NOT queued: {qerr}")
            else:
                lines.append("task queued — reply will be delivered automatically.")
        return ToolResult(True, output="\n".join(lines))

    if mode == "status":
        return ToolResult(True, output=registry.format_status(args.get("key", "")))

    if mode == "kill":
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

    return ToolResult(
        False,
        error=(f"unknown mode '{mode}' — use spawn / request / status / resume / kill"),
    )
