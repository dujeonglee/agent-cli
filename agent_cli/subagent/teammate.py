"""teammate — 상주 세션 에이전트 (P1 코어, docs/teammate/DESIGN.md).

delegate 가 "파견"(스폰→완주→ctx 폐기)이라면 teammate 는 "상주 팀원"이다:
spawn 이 key 를 반환하고, 이후 request 가 같은 ctx 위에서 반복 처리된다
(D1 비동기 mailbox). 회신은 LLM 폴링이 아니라 **harness 가 배달**한다 —
main 루프가 턴 경계에서 :meth:`TeammateRegistry.drain_replies` 를 비워
관찰 레코드로 주입한다 (D2, ``AgentLoop._deliver_teammate_replies``).

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
  요청별 SSE 라우팅은 별도 표면 ``begin/end_teammate_work`` (renderer) —
  스코프(상시)와 카드(요청별)를 분리한 이유는 web 렌더러의
  ``begin_delegate_task`` 가 스코프 push 와 결합돼 있어서다.
- **레코드 계약**: 배달 레코드는 ``tool:"teammate"`` + additive
  ``source:"teammate_reply"``. ``tool:""`` 는 v4.51.0 형식-개입 레거시
  마커라 금지 (``records.is_format_intervention`` 오인 방지 — 테스트 고정).

P1 경계: teammate 안 teammate 금지(레지스트리 미전파로 도구가 서브루프
에서 자동 strip), manifest/resume 재생성은 P3, WebUI 대화 창은 P4.
"""

from __future__ import annotations

import json
import os
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

_DEFAULT_MAX_TEAMMATES = 4

# teammates.json 스키마 버전 — 비호환 변경 시 bump (구버전 파일은 무시=fresh).
TEAMMATES_STATE_VERSION = 1


def _max_teammates() -> int:
    try:
        return int(os.environ.get("AGENT_CLI_MAX_TEAMMATES", _DEFAULT_MAX_TEAMMATES))
    except ValueError:
        return _DEFAULT_MAX_TEAMMATES


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
    role = reply.get("role", "")
    label = f"{key} ({role})" if role else key

    if reply.get("kind") == "died":
        # worker 사망 통지 (Q4): kill/세션종료가 아닌 비정상 종료 — main 이
        # status 를 조회하기 전에 능동적으로 알린다.
        reason = reply.get("output") or "worker terminated unexpectedly"
        content = (
            f"── teammate {label} DIED ──\n{reason}\n"
            f"(This teammate is gone — its queued requests were lost. Spawn a "
            f"new one if the work is still needed; its context dir is kept on "
            f"disk for post-mortem.)"
        )
        return {
            "role": "user",
            "tool": "teammate",
            "success": False,
            "content": content,
            "source": "teammate_died",
        }

    if reply.get("kind") == "question":
        question = reply.get("output") or "(empty question)"
        if reply.get("stale"):
            # P3: 세션 재시작으로 teammate 는 더 이상 답변 대기가 아님 —
            # 그대로 답하면 새 request 로 오인되므로 정직하게 알린다.
            tail = (
                "(STALE — the session was resumed; the teammate is NO LONGER "
                "waiting for this answer. Re-send the original request with "
                f'{{"mode":"request","key":"{key}","message":"..."}} if still needed.)'
            )
        else:
            tail = (
                "(The teammate is BLOCKED until answered. Answer via teammate op: "
                f'{{"mode":"request","key":"{key}","message":"<your answer>"}}.)'
            )
        content = f"── teammate {label} QUESTION ──\n{question}\n{tail}"
        return {
            "role": "user",
            "tool": "teammate",
            "success": True,
            "content": content,
            "source": "teammate_question",
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
    content = f"── teammate {label} reply ({status}) ──\n{body}"
    return {
        "role": "user",
        "tool": "teammate",
        "success": bool(reply.get("success")),
        "content": content,
        # additive 마킹 — tool="" (형식-개입 레거시) 오인 금지 계약과 짝.
        "source": "teammate_reply",
    }


class Teammate:
    """상주 teammate 1명 — key·역할·영속 ctx·inbox·worker 스레드."""

    def __init__(
        self,
        key: str,
        *,
        role_name: str,
        role_prompt: str,
        allowed_tools: list[str] | None,
        model: str,
        hooks_config: dict | None,
        context_mode: str,
        home_dir: Path,
    ):
        self.key = key
        self.role_name = role_name
        self.role_prompt = role_prompt
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
            "role": self.role_name,
            "state": self.state,
            "handled": self.handled,
            "pending_requests": self.inbox.qsize(),
            "est_tokens": est_tokens,
            "error": self.error,
        }


class TeammateRegistry:
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

        self._teammates: dict[str, Teammate] = {}
        self._cv = threading.Condition()
        self._pending: list[dict] = []  # 미배달 회신 (도착 순서)
        # 회신 도착 알림 (CLI 📨 라인 / web transient status) — 부트스트랩 주입.
        self.on_reply: Callable[[dict], None] | None = None

    # ── 조회 ────────────────────────────────────

    def get(self, key: str) -> Teammate | None:
        return self._teammates.get(key)

    def roster_snapshot(self) -> list[dict]:
        return [tm.snapshot() for tm in self._teammates.values()]

    def _notify_roster(self) -> None:
        """P4: 상태 변화를 대화 창 목록에 반영 — web sticky, CLI no-op."""
        from agent_cli.render import get_renderer

        try:
            get_renderer().teammate_roster(self.roster_snapshot())
        except Exception:
            pass  # 표시용 — 실행 경로를 막지 않는다

    def alive_count(self) -> int:
        return sum(1 for t in self._teammates.values() if t.state != "dead")

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
        for tm in self._teammates.values():
            if tm.state == "busy" or tm.inbox.qsize() > 0:
                return True
        return False

    def waiting_ask_keys(self) -> list[str]:
        """답변 대기 중인 teammate — 펌프 종료 시 경고 표시용."""
        return [tm.key for tm in self._teammates.values() if tm.state == "waiting_ask"]

    # ── spawn ───────────────────────────────────

    def spawn(
        self,
        *,
        role: str = "",
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

        if self.alive_count() >= _max_teammates():
            return "", (
                f"teammate limit reached ({_max_teammates()} alive). "
                f'Kill one first (mode:"kill") or raise AGENT_CLI_MAX_TEAMMATES.'
            )

        role_prompt = ""
        model = self.runtime.get("model", "")
        hooks_config = self.runtime.get("hooks_config")
        if role:
            from agent_cli.subagent.roles import load_teammate_role
            from agent_cli.subagent.runner import apply_role_overrides

            body, config, error = load_teammate_role(role)
            if error:
                return "", error
            role_prompt = body or ""
            allowed_tools, model, hooks_config = apply_role_overrides(
                config,
                allowed_tools=allowed_tools,
                model=model,
                hooks_config=hooks_config,
            )

        if context_mode == "fork" and parent_ctx is None:
            return "", "fork requires parent context"

        if self.session_dir is None:
            return "", "teammate requires a session dir (headless run not supported)"

        key = f"agt-{uuid.uuid4().hex[:8]}"
        tm = Teammate(
            key,
            role_name=role,
            role_prompt=role_prompt,
            allowed_tools=allowed_tools,
            model=model,
            hooks_config=hooks_config,
            context_mode=context_mode,
            home_dir=self.session_dir / "teammates" / key,
        )
        self._teammates[key] = tm
        tm.worker = threading.Thread(
            target=self._worker,
            args=(tm, parent_ctx),
            daemon=True,
            name=f"teammate-{key}",
        )
        tm.worker.start()
        self._save_state()
        self._notify_roster()
        return key, ""

    # ── request / 회신 ──────────────────────────

    def request(self, key: str, message: str, *, author: str = "main") -> str:
        """request 큐잉 — 성공 시 빈 문자열, 실패 시 에러 메시지."""
        tm = self._teammates.get(key)
        if tm is None:
            return f"unknown teammate '{key}' (see mode:\"status\" for live keys)"
        if tm.state == "dead":
            reason = f" ({tm.error})" if tm.error else ""
            return f"teammate '{key}' is dead{reason} — spawn a new one"
        if not message.strip():
            return "empty message"
        tm.queued += 1
        tm.inbox.put({"seq": tm.queued, "text": message, "author": author})
        from agent_cli.render import get_renderer

        get_renderer().teammate_message(
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

    def wait_reply(self, key: str, timeout: float) -> dict | None:
        """``key`` 의 mailbox 아이템 1건을 블록 대기 (mode:"wait").

        해당 key 의 것만 꺼내고 다른 teammate 의 것은 pending 에 남긴다
        (다음 턴 경계에 정상 배달). **kind 불문** — 질문(P2)이 먼저
        도착하면 그걸 반환한다: wait 가 질문을 건너뛰면 main(회신 대기)과
        teammate(답변 대기)가 서로를 기다리는 교착이 된다.
        """
        deadline = time.monotonic() + timeout
        with self._cv:
            while True:
                for i, r in enumerate(self._pending):
                    if r.get("key") == key:
                        item = self._pending.pop(i)
                        self._save_state()  # 소비 반영 (_cv 는 RLock — 재진입 안전)
                        return item
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self._cv.wait(remaining)

    # ── status / kill / 종료 ────────────────────

    def format_status(self, key: str = "") -> str:
        if key:
            tm = self._teammates.get(key)
            if tm is None:
                return f"unknown teammate '{key}'"
            items = [tm]
        else:
            items = list(self._teammates.values())
        if not items:
            return 'no teammates. Spawn one with mode:"spawn".'
        lines = [f"teammates ({self.alive_count()}/{_max_teammates()} alive):"]
        for tm in items:
            s = tm.snapshot()
            role = s["role"] or "anon"
            line = (
                f"- {s['key']} [{role}] {s['state']}"
                f" | handled {s['handled']}"
                f" | inbox {s['pending_requests']}"
                f" | ctx ~{s['est_tokens']} tokens"
            )
            if s["error"]:
                line += f" | error: {s['error']}"
            lines.append(line)
        with self._cv:
            if self._pending:
                lines.append(f"(undelivered replies: {len(self._pending)})")
        return "\n".join(lines)

    def kill(self, key: str) -> str:
        """종료 요청 — 성공 시 빈 문자열. 멱등 (이미 dead 여도 성공)."""
        tm = self._teammates.get(key)
        if tm is None:
            return f"unknown teammate '{key}'"
        tm.revivable = False  # P3: 명시 kill 은 영구 — resume 이 되살리지 않음
        tm.stop_event.set()
        tm.inbox.put(_SHUTDOWN)
        if tm.worker is not None:
            tm.worker.join(timeout=2.0)  # busy 면 다음 턴 경계에서 멈춤
        self._save_state()
        self._notify_roster()
        return ""

    def shutdown_all(self) -> None:
        """세션 종료 — 전원 kill. main 부트스트랩의 finally 에서 호출."""
        for tm in list(self._teammates.values()):
            tm.stop_event.set()
            tm.inbox.put(_SHUTDOWN)
        for tm in list(self._teammates.values()):
            if tm.worker is not None:
                tm.worker.join(timeout=5.0)
        # 최종 스냅샷 — revivable 유지 상태로 기록돼 resume 이 되살린다 (D7).
        self._save_state()

    def auto_spawn(self, parent_ctx=None) -> int:
        """frontmatter ``auto-spawn: true`` 역할을 세션 시작 시 자동 상주
        (전문가 팀 확장). restore() **이후에** 호출 — 같은 역할의 살아있는
        teammate(재생성분)가 이미 있으면 중복 스폰하지 않는다. 스폰 수 반환."""
        from agent_cli.subagent.roles import available_roles

        live_roles = {
            tm.role_name for tm in self._teammates.values() if tm.state != "dead"
        }
        spawned = 0
        for name, meta in available_roles(include_meta=True):
            if not meta.get("auto-spawn"):
                continue
            if name in live_roles:
                continue
            key, error = self.spawn(role=name, parent_ctx=parent_ctx)
            if not error:
                spawned += 1
        return spawned

    # ── P3: 상태 영속 + resume 재생성 (D7) ──────

    def _state_path(self) -> Path | None:
        return self.session_dir / "teammates.json" if self.session_dir else None

    def _save_state(self) -> None:
        """teammates.json 원자 저장 (fsio) — manifest + 미배달 pending 미러.

        갱신 시점: spawn/kill/worker 종료(상태) + push/drain/wait(pending).
        상태 파일은 best-effort — 디스크 문제로 세션 진행을 막지 않는다.
        """
        path = self._state_path()
        if path is None:
            return
        entries = []
        for tm in self._teammates.values():
            entries.append(
                {
                    "key": tm.key,
                    "role": tm.role_name,
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
                    "version": TEAMMATES_STATE_VERSION,
                    "teammates": entries,
                    "pending": pending,
                },
            )
        except OSError:
            pass

    def restore(self, parent_ctx=None) -> int:
        """세션 resume 시 teammates.json 에서 재생성 — 되살린 수 반환.

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
        if not isinstance(data, dict) or data.get("version") != TEAMMATES_STATE_VERSION:
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
        for e in data.get("teammates", []):
            key = e.get("key")
            if not key or key in self._teammates or self.session_dir is None:
                continue
            tm = Teammate(
                key,
                role_name=e.get("role", ""),
                role_prompt=e.get("role_prompt", ""),
                allowed_tools=e.get("allowed_tools"),
                model=e.get("model", ""),
                hooks_config=e.get("hooks_config"),
                context_mode=e.get("context_mode", "none"),
                home_dir=self.session_dir / "teammates" / key,
            )
            tm.handled = int(e.get("handled", 0) or 0)
            tm.queued = tm.handled  # seq 이어가기 (reply-N.md 충돌 방지)
            if isinstance(e.get("created_at"), (int, float)):
                tm.created_at = e["created_at"]
            self._teammates[key] = tm
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
                name=f"teammate-{key}",
            )
            tm.worker.start()
            revived += 1
        self._save_state()
        self._notify_roster()
        return revived

    # ── worker ──────────────────────────────────

    def _persist_reply(self, tm: Teammate, seq: int, body: str) -> str:
        """회신 전문을 항상 디스크에 (over-cap 포인터 + P3 보존 토대)."""
        try:
            replies_dir = tm.home_dir / "replies"
            replies_dir.mkdir(parents=True, exist_ok=True)
            path = replies_dir / f"reply-{seq}.md"
            path.write_text(body, encoding="utf-8")
            return str(path)
        except OSError:
            return ""

    def _worker(self, tm: Teammate, parent_ctx) -> None:
        """teammate 의 전 생애: 스코프 열기 → ctx 생성 → inbox 루프 → 정리.

        상태 전이는 전부 이 함수 안이다.
        """
        from agent_cli.render import get_renderer
        from agent_cli.subagent.runner import create_subagent_ctx

        renderer = get_renderer()
        renderer.begin_prompt_scope(tm.key, label=f"teammate:{tm.role_name or 'anon'}")
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

                renderer.begin_teammate_work(
                    key=tm.key, seq=seq, role=tm.role_name, message=text
                )
                success, output, duration = False, "", 0.0
                try:
                    loop_result, duration = self._run_message(tm, query)
                    success = bool(loop_result.success)
                    output = (
                        loop_result.output
                        if loop_result.output is not None
                        else "(teammate did not complete the request)"
                    )
                except Exception as e:  # worker 는 죽지 않는다 — 회신으로 보고
                    output = f"teammate internal error: {type(e).__name__}: {e}"
                finally:
                    renderer.end_teammate_work(
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
                renderer.teammate_message(
                    key=tm.key,
                    direction="out",
                    author=tm.key,
                    text=output,
                    seq=seq,
                    success=success,
                )
                if author == "main":
                    # main 발신 요청의 회신만 main mailbox 로 (D8 — 인간
                    # 개입 문답은 창에만, main 컨텍스트 비오염).
                    self._push_reply(
                        {
                            "kind": "reply",
                            "key": tm.key,
                            "role": tm.role_name,
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
                        "role": tm.role_name,
                        "success": False,
                        "output": crash,
                    }
                )
            renderer.end_prompt_scope(tm.key)  # 스코프 고정 (사후 검사 가능)
            self._save_state()  # ctx 실패(error→dead)·종료 상태 반영
            self._notify_roster()

    def _make_ask_handler(self, tm: Teammate):
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
            get_renderer().teammate_message(
                key=tm.key,
                direction="question",
                author=tm.key,
                text=question,
            )
            if tm.current_author == "main":
                # main 발신 작업의 질문만 main mailbox 로 (D8 대칭).
                self._push_reply(
                    {
                        "kind": "question",
                        "key": tm.key,
                        "role": tm.role_name,
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
                    return "(no response — teammate is being terminated)"
                author = item.get("author", "main")
                text = item.get("text", "")
                return f"[{author}]: {text}" if author != "main" else text
            finally:
                tm.state = "busy"
                self._notify_roster()

        return handler

    def _run_message(self, tm: Teammate, query: str):
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
            agent_name=tm.role_name,
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
        "New teammate mail has arrived. It is delivered as observation(s) at "
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


def tool_teammate(
    args: dict,
    *,
    registry: TeammateRegistry | None,
    parent_ctx=None,
    runtime: dict | None = None,
) -> ToolResult:
    """teammate 도구 mode 디스패치. delegate 처럼 루프가 인터셉트해
    provider/ctx 배선(runtime)을 주입한다."""
    if registry is None:
        return ToolResult(
            False,
            error=(
                "teammate is unavailable in this loop (main session only — "
                "teammates cannot spawn teammates)"
            ),
        )

    # P3: 매 호출 runtime 갱신 — restore 로 되살아난 teammate(스폰 없음)도
    # 첫 request 부터 현재 세션의 provider 배선으로 돈다.
    if runtime:
        registry.runtime = runtime

    mode = args.get("mode", "")

    if mode == "spawn":
        key, error = registry.spawn(
            role=args.get("role", ""),
            allowed_tools=args.get("tools"),
            context_mode=args.get("context", "none"),
            parent_ctx=parent_ctx,
            runtime=runtime,
        )
        if error:
            return ToolResult(False, error=f"spawn rejected: {error}")
        lines = [
            f"spawned teammate '{key}'"
            + (f" (role: {args['role']})" if args.get("role") else "")
        ]
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
                f"queued to {key} — the reply will be delivered automatically "
                f"at a later turn. Keep working on other things, or block for "
                f'it with {{"mode":"wait","key":"{key}"}}.'
            ),
        )

    if mode == "wait":
        key = args.get("key", "")
        tm = registry.get(key)
        if tm is None:
            return ToolResult(False, error=f"unknown teammate '{key}'")
        if tm.state == "dead" and not registry.has_pending_replies():
            return ToolResult(
                False, error=f"teammate '{key}' is dead — nothing to wait for"
            )
        timeout = float(registry.runtime.get("timeout", 300))
        reply = registry.wait_reply(key, timeout)
        if reply is None:
            return ToolResult(
                False,
                error=(
                    f"no reply from {key} within {int(timeout)}s — it is still "
                    f"working. Wait again, keep doing other work (the reply "
                    f"will be delivered automatically), or kill it."
                ),
            )
        if reply.get("kind") == "question":
            # P2: 회신 대신 질문이 먼저 도착 — wait 가 이걸 삼키지 않으면
            # main(대기)과 teammate(답변 대기)가 서로를 기다리는 교착.
            return ToolResult(
                True,
                output=(
                    f"STATUS: question\nQUESTION from {key}:\n"
                    f"{reply.get('output', '')}\n"
                    f"(The teammate is BLOCKED until answered. Answer via "
                    f'{{"mode":"request","key":"{key}","message":"<answer>"}}, '
                    f"then wait again for the actual reply.)"
                ),
            )
        status = "success" if reply.get("success") else "error"
        return ToolResult(
            bool(reply.get("success")),
            output=(
                f"STATUS: {status}\nREPLY from {key} (seq {reply.get('seq')}):\n"
                f"{reply.get('output', '')}"
            ),
            artifact=reply.get("reply_path", ""),
        )

    if mode == "status":
        return ToolResult(True, output=registry.format_status(args.get("key", "")))

    if mode == "kill":
        key = args.get("key", "")
        err = registry.kill(key)
        if err:
            return ToolResult(False, error=f"kill rejected: {err}")
        return ToolResult(True, output=f"teammate '{key}' terminated.")

    return ToolResult(
        False,
        error=f"unknown mode '{mode}' — use spawn / request / wait / status / kill",
    )
