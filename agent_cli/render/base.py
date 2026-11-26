"""Base renderer interface — override methods to customize output.

렌더러는 루프가 사람을 보는 창이다: 사건(프롬프트·관찰·상태)을 내고, 사람을 기다리는 것(열린 질문·열린 확인)을
그린다. 답은 렌더러가 아니라 명령과 웹 트레이로 돌아온다(6b·6d). 렌더러에 남은 읽기는 부팅 마법사의
``prompt_user``(CLI) 하나다.
"""

from __future__ import annotations

import os
import select
import sys
import threading
from abc import ABC, abstractmethod
from contextlib import contextmanager

# 터미널을 읽는 이는 한 번에 하나 (6d): 세션의 터미널 읽기(`main.read_terminal_line` — main 의 대기들과 확인을
# 기다리는 호출, 여러 스레드가 부른다)는 막지 않고 잡아 보고 못 잡으면 이번엔 읽지 않는다 — 친 줄은 한 스레드가 한
# 번만 읽는다. 부팅 마법사의 ``prompt_user`` 는 ``input()`` 동안 쥔다. 재진입 가능.
interactive_lock = threading.RLock()

try:
    import termios
except (
    ImportError
):  # 터미널 제어가 없는 곳(Windows) — 먼저 친 입력을 거르지 않는다(종전 동작)
    termios = None


class Renderer(ABC):
    """Abstract base for all renderers.

    Implement a subclass and call set_renderer() to swap output style.
    Each method corresponds to a distinct UI event in the agent loop.

    Built-in support for:
    - Nested rendering via push_depth/pop_depth for skills/delegates
    - Thread-local output capture for parallel delegate buffering
    """

    #: `agent_message` 가 막히지 않는가 — 디스크 I/O·콜백·외부 출력 없이 끝나는가(v10.146.5). 규칙 한 문장: **막히지 않는
    #: 표면만 배달 구간 안에서 낸다.** 참이면 레지스트리가 대화 줄을 기록(conversation.jsonl)과 같은 ``_cv`` 구간에서
    #: 내 화면 순서가 기록 순서를 따른다. 거짓(터미널 — stdout 이 막힐 수 있다)이면 구간 밖에서 낸다.
    agent_message_never_blocks: bool = False

    def __init__(self) -> None:
        self._depth: int = 0
        self._captures: dict[int, list[str]] = {}  # thread_id → captured lines
        self._capture_lock = threading.Lock()
        # Per-thread prompt provenance, so an interactive confirm/ask can
        # show WHO is asking and WHY. The loop calls thought()/action() on
        # the same thread right before a tool triggers confirm/ask, and
        # delegate workers register their agent label — all keyed by
        # ``threading.get_ident()``.
        self._thread_agent: dict[int, str] = {}  # tid → delegate label
        self._thread_thought: dict[int, str] = {}  # tid → last reasoning
        self._thread_action: dict[int, str] = {}  # tid → last action line
        # ⚡ 자동 승인 (노브 `auto_approve`, v10.43.0 — 종전 web 전용): 안전 확인
        # (위험 셸·경로)을 묻지 않고 허용한다 — `auto_approved` 가 판정한다. 세션 한정, 기본 off.
        self.auto_approve: bool = False

    def set_auto_approve(self, on: bool) -> None:
        """⚡ 자동 승인 토글 — web 은 sticky 전파를 덧붙인다."""
        self.auto_approve = bool(on)

    def auto_approved(self, label: str) -> bool:
        """⚡ 자동 승인이 켜져 있으면 무엇을 묻지 않고 허용했는지 남기고 ``True`` (v10.120.0).

        안전 확인(위험 셸 `shell.confirm_dangerous`·경로 `_confine.guard`)이 확인을 열기 **전에**
        부른다. 종전엔 이 검사가 확인 안에 있어 모든 확인이 자동으로 답해졌다 —
        StaleGuard 의 멈춤 질문(런을 세우고 사람의 결정을 받는 확인)까지. 자동 승인의 뜻은
        "위험 확인을 묻지 마라" 라 정책은 그 호출자의 것이고, 열린 확인은 언제나 사람의 답을 기다린다.
        기록 모양은 렌더러가 정한다(`_note_auto_approved`)."""
        if not self.auto_approve:
            return False
        self._note_auto_approved(f"⚡ Auto-approved (no confirmation): {label}")
        return True

    def _note_auto_approved(self, text: str) -> None:
        """자동 승인 기록 한 줄 — Default no-op; CLI 는 콘솔 줄, web 은 관찰 카드."""

    # ── Depth (nesting) ──────────────────────────────

    @property
    def depth(self) -> int:
        return self._depth

    def push_depth(self) -> None:
        """Increase nesting depth (enter skill/delegate)."""
        self._depth += 1

    def pop_depth(self) -> None:
        """Decrease nesting depth (exit skill/delegate)."""
        if self._depth > 0:
            self._depth -= 1

    def in_nested_render(self) -> bool:
        """이 호출이 세션 최상위가 아닌 루프(스킬·delegate·one-shot 워커 등)
        에서 왔는가. 기본 규칙은 깊이 > 0 또는 이 스레드가 캡처 중 — 병렬
        delegate 는 캡처로 돈다. 렌더러마다 중첩을 다르게 추적하면 오버라이드한다
        (web 은 스레드 스코프 기준)."""
        return self._depth > 0 or self.is_capturing

    # ── Capture (parallel buffering) ─────────────────

    def start_capture(self) -> None:
        """Start capturing output for the current thread."""
        with self._capture_lock:
            self._captures[threading.get_ident()] = []

    def stop_capture(self) -> list[str]:
        """Stop capturing and return collected lines for the current thread."""
        with self._capture_lock:
            tid = threading.get_ident()
            return self._captures.pop(tid, [])

    @property
    def is_capturing(self) -> bool:
        """True if the current thread is in capture mode."""
        return threading.get_ident() in self._captures

    def _capture_line(self, line: str) -> bool:
        """Append line to capture buffer if capturing. Returns True if captured."""
        tid = threading.get_ident()
        with self._capture_lock:
            buf = self._captures.get(tid)
            if buf is None:
                return False
            buf.append(line)
        return True

    def set_thread_status(self, status: str) -> None:
        """The live status line of the current thread — the web renderer shows it on the scope's card; others ignore it."""

    # ── Prompt provenance (who/why behind a confirm or ask) ──

    def set_thread_agent(self, label: str) -> None:
        """Tag the current thread with a delegate label (or clear it with
        ``""``). Surfaced by ``confirm`` / ``prompt_user`` so the user can
        tell which agent is asking."""
        tid = threading.get_ident()
        if label:
            self._thread_agent[tid] = label
        else:
            self._thread_agent.pop(tid, None)

    def note_thought(self, content: str) -> None:
        """Record the current thread's latest reasoning (first line kept
        for the compact prompt header)."""
        self._thread_thought[threading.get_ident()] = content.strip()

    def note_action(self, tool_name: str, tool_input: str) -> None:
        """Record the current thread's latest action."""
        line = f"{tool_name} → {tool_input}" if tool_input else tool_name
        self._thread_action[threading.get_ident()] = line

    def begin_prompt_scope(self, scope_id: str, label: str = "") -> None:
        """프롬프트 인스펙터 스코프 push (v4.52.0) — 중첩 루프(skill 등)가
        호출자 스레드에서 돌 때 자기 스코프를 선언. Default no-op (C8 규율 —
        인스펙터 없는 렌더러는 무관)."""

    def end_prompt_scope(self, scope_id: str) -> None:
        """스코프 pop + 동적 컨텍스트 고정. Default no-op."""

    def note_scope_ctx(self, ctx) -> None:
        """현재 스코프의 ContextManager 를 인스펙터에 등록 — 동적 컨텍스트
        on-demand 조회용. Default no-op."""

    def note_system_prompt(
        self,
        sections: list[tuple[str, str]],
        turn: int,
        *,
        grammar: tuple[bool, str] | None = None,
        tools: list[dict] | None = None,
        tail: list[tuple[str, str]] | None = None,
        open_items: list[str] | None = None,
    ) -> None:
        """Record the system prompt (as named sections) sent to the LLM this
        turn, plus the decoding grammar the server enforces on the call
        (``(thinking_open, ebnf)``, None when unconstrained) and the function
        schemas sent as request ``tools[]`` (native_fc; None otherwise). Concrete no-op
        — the CLI renderer has no use for it; the web renderer overrides to
        keep the latest snapshot for the Prompt Inspector
        (`GET /api/debug/prompt`). Called once per LLM call, so an override
        must be cheap (store, don't render)."""

    def note_items_closed_after(self, open_now: list[str]) -> None:
        """런 끝의 아직 열린 항목 id (v10.63.0) — 마지막 스냅샷의 ``open_items``
        와 비교해 그 호출 뒤에 닫힌 것을 표시하는 데 쓴다. Default no-op."""

    def open_items(self, snapshot: dict) -> None:
        """사람에게 보이는 장부 (v10.119.0, docs/ledger/DESIGN.md §15) — main 런의
        `Ledger.snapshot` 이 런 시작·턴 경계·`complete` 정산·런 끝에 온다. 모델
        입력과 무관한 표시 전용이다. Default no-op — web 은 스티키, CLI 는 한 줄."""

    def wait_in_place(
        self, waiting: bool, awaited: list[dict], others: list[str] | None = None
    ) -> None:
        """main 이 제자리에서 `wait` 한다 / 깼다 (v10.141.0, docs/agent-engine/DESIGN.md §5·§6). ``awaited`` 는
        기다리는 회신 ``[{"id", "from", "text"}]``(깰 때는 빈 목록), ``others`` 는 회신 말고 깨울 것의 짧은 이름
        ("job #1"·"monitor m-2"·"a schedule" — 5c-1 결정 C, 없으면 None). 표시 전용 — 사람에게 "입력하면
        깨운다" 를 알린다. Default no-op — web 은 입력창 위 줄(스티키), CLI 는 ⏳ 한 줄."""

    def note_request(self, request_id: str, hidx: int) -> None:
        """장부 항목 ``request_id`` 의 원문 레코드 history 서수 (v10.119.0) —
        `open_items` 의 행이 그 카드로 점프하는 데 쓴다. Default no-op."""

    def stale_loop(self, info: dict) -> None:
        """제자리 고리 (v10.120.0, docs/stale-guard/DESIGN.md) — ``info`` 는
        ``{turns_without_new, since_turn, fires, asking}``. 발화할 때, 발화 뒤 제자리 턴이
        이어질 때(N 이 는다), 새 결과가 나오거나 런이 끝날 때(``turns_without_new`` 0 =
        지운다) 온다. 모델 입력과 무관한 표시 전용이다. Default no-op — web 은 입력창 위
        띠(스티키 ``stuck``), CLI 는 발화마다 흐린 한 줄."""

    def can_pause_for_human(self) -> bool:
        """런을 멈추고 사람에게 물어 결정을 받을 수 있는 표면인가 (v10.120.0, StaleGuard 의
        두 번째 발화). ``can_prompt`` 와 다른 질문이다 — CLI 는 TTY 가 있어도 묻지 않는다
        (``run`` 은 사람 없이 끝나는 것이 결정이다). Default ``False``; web 만 ``True``."""
        return False

    def prompt_meta(self) -> dict[str, str]:
        """Provenance for an interactive prompt on the current thread:
        ``agent`` (delegate label, empty for the main agent), ``reasoning``
        (last thought), ``action`` (last action line)."""
        tid = threading.get_ident()
        return {
            "agent": self._thread_agent.get(tid, ""),
            "reasoning": self._thread_thought.get(tid, ""),
            "action": self._thread_action.get(tid, ""),
        }

    # ── Nested scope lifecycle ───────────────────────
    #
    # Out-of-band surfaces (web) need to know when a thread enters /
    # leaves a nested scope (skill, harness job) so they can route the
    # thread's subsequent emits into a dedicated UI group instead of
    # interleaving them on the main timeline. The base implementations
    # are concrete no-ops. WebRenderer overrides to map
    # ``thread_id → task_id`` and emit SSE markers. Agents (run·resident)
    # use ``begin_agent_work`` instead — their runs are channels.

    def begin_scope(
        self,
        *,
        task_id: str,
        kind: str = "run",
        label: str = "",
        agent: str = "",
        parent: str | None = None,
        ts: float | None = None,
        ctx_dir: str | None = None,
    ) -> None:
        """Enter a nested execution scope — a skill subloop (``kind="skill"``)
        or a harness job (``kind="harness"``). On web this opens ONE ``scope_start`` card + routes
        the scope's inner events to it (fixes: ``/orchestrate`` used to emit a
        different, un-handled event and drew no card). CLI branches on ``kind``
        (skill bracket vs harness one-liner). No-op default.

        ``parent`` — the ENCLOSING scope's ``task_id``, or ``""`` for a
        top-level scope under main. Leave it ``None`` (the default) when the
        scope opens on the same thread as its caller: the renderer then reads
        the parent off the thread's scope stack. Pass it explicitly only when
        that inference is impossible — a parallel worker calls this from its
        OWN thread, whose stack is empty, so the spawning code captures the
        parent (``current_scope()``) and hands it over.

        ``ctx_dir`` — the scope's OWN context directory, relative to the
        session dir (``skill_…``, ``run_…``, ``agents/<key>``). Sub-agents keep
        their turns in their own ``history.jsonl`` there — main's history holds
        only the final observation — so without this link a resumed session can
        rebuild the scope's card but not the turns inside it. Callers that name
        the directory AFTER opening the scope must name it first and pass it to
        both sides.

        ``ts`` — the scope's canonical start time (epoch seconds). Leave it
        ``None`` to have the emit point stamp "now". A parallel BATCH passes one
        shared value for all its workers."""

    def end_scope(
        self,
        *,
        task_id: str,
        kind: str = "run",
        success: bool = True,
        duration_s: float = 0.0,
        error: str = "",
    ) -> None:
        """Leave a scope opened by :meth:`begin_scope`. No-op default."""

    def current_scope(self) -> str:
        """The calling thread's innermost open scope ``task_id`` (``""`` = main).

        Read it BEFORE spawning worker threads to capture the parent a worker
        can't infer from its own (empty) stack. Default ``""`` — only WebRenderer
        tracks scope identity, and a CLI caller passing that "" through to
        ``begin_scope(parent=...)`` is indistinguishable from top-level, which
        is what the CLI renders anyway (depth comes from push_depth)."""
        return ""

    def current_task_id(self) -> str:
        """호출 스레드가 지금 그리는 자리의 ``task_id`` (``""`` = main 의 맨 위).

        run 에이전트를 띄우는 쪽이 **자기 스레드에서** 읽어 넘긴다 — 에이전트는 자기 스레드에서
        돌므로 부른 자리(부모의 카드·런 블록)를 스스로 알 수 없다. 기본 ``""`` — 자리를 추적하는
        것은 WebRenderer 뿐이다."""
        return ""

    def begin_agent_work(
        self,
        *,
        key: str,
        item: str,
        profile: str,
        message: str,
        req_ts: float | str | None = None,
        items: list[str] | None = None,
        caller_task: str | None = None,
    ) -> None:
        """teammate worker 가 request 1건 처리를 시작 (teammate P1).

        ``item`` (v10.77.0) — 이 런을 연 배달의 id(``u-3``·``r-7``…), 곧 런의
        이름. ``items`` — 이 런이 처리하는 inbox 항목들의 id. 보통 ``[item]``
        이고 사람-직접 배치는 N개. 웹의 런 블록이 이걸로 큐에서 먼저 도착한
        수신 줄(``agent_message(in)``)을 자기 머리로 끌어온다 — 수신 줄은
        넣기와 한 구간에서 나가는 렌더러(``agent_message_never_blocks`` — 웹)에선
        언제나 런보다 앞서고, 터미널처럼 구간 뒤에 내는 렌더러에선 보장이 아니다
        (v10.146.5 — 웹은 늦게 온 in 도 블록 머리로 끌어온다).
        이 목록 없이는 어느 항목이 이 런의 것인지 알 수 없다(배치가 특히 그렇다).

        ``caller_task`` — run 에이전트의 과제 런만: 부른 자리(`current_task_id`, main 이면
        ``""``). 웹은 그 자리에 한 줄 카드를 놓는다. 그 밖은 ``None``. 대화가 사는 곳은
        에이전트마다 ``agents/<key>`` 하나다(v10.141.0 — run 도).

        **프롬프트 스코프를 건드리지 않는다** — 스코프는 에이전트 스레드가
        begin_prompt_scope(key) 로 상시 보유. 이 표면은 런별 표시(웹 SSE 채널
        블록 라우팅 / CLI 에이전트 줄)만 담당. 기본 no-op."""

    def end_agent_work(
        self,
        *,
        key: str,
        item: str,
        success: bool,
        duration_s: float,
        error: str = "",
        stopped: bool = False,
    ) -> None:
        """teammate request 1건 처리 종료 — begin_agent_work 과 짝. ``stopped`` (v10.142.0): 사람(또는 모델)이
        멈춘 런 — 실패가 아니라 정지로 그린다. 기본 no-op."""

    def open_questions(self, questions: list) -> None:
        """사람에게 온 열린 질문 목록 (6b) — 질문이 열리고 닫힐 때마다 ``[{id, asker, text, ts}]``. web 은 sticky(❓
        트레이의 출처), CLI 는 no-op."""

    def question_line(
        self, asker: str, qid: str, text: str, *, to: str = "user"
    ) -> None:
        """사람에게 건 질문 한 줄 (6b) — 터미널이 그린다(번호·답하는 법). web 은 no-op: 트레이와 `ask` 카드가 보인다."""

    def open_confirms(self, confirms: list) -> None:
        """열린 확인 목록 (6d) — 확인이 열리고 닫힐 때마다(`ConfirmsMixin._notify_confirms` 의 모양). web 은 sticky(❓
        트레이의 출처), CLI 는 no-op."""

    def confirm_line(self, asker: str, item) -> None:
        """사람에게 건 확인 한 줄 (6d) — 터미널이 그린다(번호·무엇·답하는 법). ``item`` 은 `agent_cli.confirm.Confirm`.
        web 은 no-op: 트레이가 보인다."""

    def agent_roster(self, roster: list) -> None:
        """teammate 목록/상태 스냅샷 (P4) — spawn/kill/상태 전이마다 호출.
        web 은 sticky 브로드캐스트(대화 창 목록), CLI 는 no-op."""

    def agent_message(
        self,
        *,
        key: str,
        direction: str,
        author: str,
        text: str,
        item: str = "",
        success: bool = True,
        to: str = "main",
        ts: float | str | None = None,
        profile: str = "",
        instance_name: str = "",
        fallback: bool = False,
        absorbed_into: str | None = None,
    ) -> None:
        """teammate 대화 창의 메시지 1건 (P4). direction: "in"(요청/답변
        수신) | "out"(회신) | "question"(ask). ``to`` = 수신자("main" 또는
        "user:닉네임" 등 — @agt 명령/창 개입 문답의 라우팅 표시). ``ts``
        (5.13) = resume 재생 시 원래 발생 시각. ``fallback`` (v9.23.0) =
        하네스가 대신 보낸 런 요약(회신 없이 끝난 런) — 도구 호출이 없는
        유일한 발신이라 웹이 이것만 따로 그린다. ``item`` (v10.77.0) = 그 배달의
        id(수신 줄), 그 발신을 낸 런의 id(사람에게 가는 결과 줄), 폴백 줄은 그 폴백이 갚는
        빚의 id(v10.165.0 — 빚마다 한 줄). ``direction="absorbed"`` +
        ``absorbed_into`` (v10.8.2) = 받은편지함의 이 항목(``item``)이 **도는 런**
        (``absorbed_into`` = 그 런의 id)에 턴 경계에서 흡수됐다 — 웹이 ⏳ 대기
        줄을 그 런 블록 머리로 옮긴다. ``direction="restarted"`` (v10.165.0) = 세션 재시작이
        이 에이전트를 되살렸다 — 웹이 채널에 한 줄 그린다(``author``·``text`` 는 빈 값). 기본 no-op."""

    def clear_agent_conversation(self, key: str) -> None:
        """한 teammate 의 대화 기록을 표면에서 정리 (5.13, kill 시). web 은
        replay 버퍼 정리 + 라이브 창 비움, CLI 는 no-op."""

    def agent_stopped(
        self, *, key: str, how: str, notified: list[tuple[str, str]], kept: int
    ) -> None:
        """사람(또는 모델)이 에이전트를 멈췄다 (v10.142.0, docs/agent-engine/DESIGN.md §2.3) — ``how`` 는
        ``"stopped by user"``·``"removed by main"``, ``notified`` 는 회신을 보낸 상대 ``[(받는 이, 요청 id)]``,
        ``kept`` 는 남긴 대기 요청 수. 표시 전용. Default no-op — CLI 는 ``■ agt-3 │ …`` 한 줄, 웹은 채널
        칩·회신 카드가 말한다."""

    def agent_mail_hint(self, *, key: str, kind: str, text: str) -> None:
        """main 앞 메일 도착 한 줄 — CLI 전용 (v10.51.0). ``text`` 는 이미
        조립된 사람용 한 줄(바쁠 때의 대기 줄, 질문 도착). 기본 구현은
        ``status`` 로 위임한다. web 은 이 신호를 쓰지 않는다 — 미배달 메일이
        입력창 위 대기열에 줄을 선다(``WebServer.queue_snapshot``)."""
        self.status("running", text)

    def note_next(self, text: str) -> None:
        """다음 카드 하나에 **표시 전용 주석**을 붙인다 (기본 no-op).

        모델 컨텍스트에는 안 들어간다 — 화면에만 붙는다. 터미널은 이미 확인
        프롬프트를 인라인으로 찍어 승인 사실이 스크롤백에 남으므로 기본 no-op
        으로 두고, 웹만 override 한다(웹은 트레이가 답하는 순간 사라져 흔적이
        없다). 필수 구현 아님."""

    def stream_stall(
        self,
        *,
        kind: str,
        elapsed_s: float = 0.0,
        limit_s: float = 0.0,
        attempt: int = 1,
        attempts: int = 1,
    ) -> None:
        """스트림 무진전(no-token) 상태 (v8.60.0). ``agent_mail_hint`` 와 같은
        형태 — **기본 구현은 ``status`` 위임**이라 커스텀 렌더러는 무수정.

        이벤트를 두 부류로 가르는 게 이 훅의 존재 이유다:

        - ``kind="wait"``: 30초마다 **반복**되는 대기. 한 시도에 20번까지
          울리므로 기록으로 남기면 표면을 덮는다 → 제자리 갱신 대상.
        - ``kind="resend"``: 한도 소진 후 재전송이라는 **전이**. 드물고
          되돌릴 수 없으므로 기록으로 남긴다.
        - ``kind="clear"``: 대기 종료(토큰 도착·에러·중단) — 제자리 줄 정리.
          기본 구현에선 할 일이 없다(남긴 게 없으므로).

        ``attempt``/``attempts`` 는 **총 시도 기준 1-오리진** (n/N, N=총
        횟수). 최대 대기 = ``limit_s`` × ``attempts``."""
        if kind == "wait":
            left = max(0, int(limit_s) - int(elapsed_s))
            self.status(
                "running",
                f"Waiting for a response — no tokens for {int(elapsed_s)}s "
                f"(attempt {attempt}/{attempts}, reconnect in {left}s)",
            )
        elif kind == "resend":
            self.status(
                "running",
                f"Stream stalled — reconnected and resent (attempt {attempt}/{attempts})",
            )
        elif kind == "broken":
            self.status(
                "running",
                f"Stream connection broken — resending ({attempt}/{attempts})",
            )

    def stream_reset(self) -> None:
        """진행 중 스트림의 **부분 출력을 버린다** (v8.61.0).

        재전송은 생성을 처음부터 다시 한다(서버측 resume 없음). 이미 표면에
        나간 부분 출력을 걷지 않으면 새 시도의 토큰이 그 뒤에 이어붙어
        중복으로 보인다 — 최종 응답 내용은 시도마다 새 ``StreamAccum`` 이라
        멀쩡하고, **표시만** 어긋난다.

        기본 no-op: 부분 출력을 화면에 들고 있지 않은 렌더러는 할 일이 없다.
        (종전 무진전 재전송에도 이 갭이 있었으나 그쪽은 거의 항상 TTFT —
        토큰이 하나도 안 나온 상태 — 라 드러나지 않았다.)"""

    # ── Abstract render methods ──────────────────────

    @abstractmethod
    def header(
        self,
        provider: str,
        model: str,
        max_turns: int,
        skill_name: str = "",
        skill_args: str = "",
    ) -> None:
        """Session or skill start banner."""

    def turn_sep(self, turn: int) -> None:
        """Separator between turns. Default no-op (C8) — web 이 실증한
        정답(턴 정보가 이벤트에 실림); 시각 구분이 필요한 렌더러만 override."""

    @abstractmethod
    def thought(self, content: str, turn: int) -> None:
        """LLM reasoning/thought."""

    @abstractmethod
    def action(self, tool_name: str, tool_input: str, turn: int) -> None:
        """Tool call (action)."""

    @abstractmethod
    def observation(
        self,
        content: str,
        turn: int,
        tool_name: str | None = None,
        success: bool = True,
        images: list[dict] | None = None,
        item: str = "",
    ) -> None:
        """Tool result (observation).

        `success` is the observation record's ``success`` and drives the ✓/✗ icon and `success`/`error`
        label — except ``tool_name == "interrupt"`` (the stop/restart fact record, not a tool call's
        result), drawn neutral ■ with no label (IMPL-mark-icons).

        ``images`` (v10.60.0): the observation record's ``ImageRef`` dicts —
        web draws thumbnails from them; CLI reads the placeholder lines in
        ``content`` instead and ignores this.

        ``item`` (v10.165.0): 작업 구간 도중 흡수된 배달의 id(그 관찰 레코드의 ``item``) — web 이 카드에 달아
        작업 블록 머리 줄에서 그 카드로 간다. CLI 는 쓰지 않는다."""

    @abstractmethod
    def final(self, content: str, turn: int, requests: list | None = None) -> None:
        """Final answer.

        ``requests`` (v9.19.0, additive) — the user requests this result
        **claims to answer**, resolved from ``complete``'s ``answers`` ids to
        ``{id, author, text}``. Lets a reader see WHICH ask a final belongs to
        instead of inferring it from position; in a merged run the position
        tells you nothing. ``None``/empty = nothing claimed (single-user CLI,
        a sub-loop's final, or a model that did not declare).
        """

    @abstractmethod
    def error(self, content: str, turn: int, *, failed: bool = True) -> None:
        """오류 한 줄, 또는 런이 끝난 까닭 한 줄(`render_run_ended`) — ``failed`` 가 참(기본)이면 오류로, 거짓이면
        중립 줄로 그린다(기다릴 것 없이 끝난 런, 5d)."""

    def recovery(
        self,
        raw_emission: str,
        intervention_message: str,
        reason: str,
        turn: int,
    ) -> None:
        """Recovery lifecycle: the model's emission failed to parse or
        validate, so the loop feeds an ``intervention`` back and retries.

        Default — mark the format error and surface the intervention (what
        was fed back to the model) as its own observation, so the failed
        emission, the intervention, and the retry each read as distinct
        steps rather than one growing blob. The web renderer overrides this
        to first *finalize* the live streaming card as a failed emission
        (otherwise the next turn's stream appends to the same card).
        ``raw_emission`` is the rejected text — already shown via
        :meth:`raw` in verbose CLI; web uses it to close the stream card.
        """
        self.status("error", f"format error ({reason}) — retrying", turn)
        self.observation(intervention_message, turn, None, success=False)

    def compaction(
        self,
        *,
        phase: str,
        old_tokens: int = 0,
        new_tokens: int = 0,
        evicted_count: int = 0,
        reason: str = "",
    ) -> None:
        """Context-compaction lifecycle (``start`` / ``done`` / ``warning``).

        Default — route through :meth:`status` so CLI/minimal renderers
        print a one-line notice. The web renderer overrides this to emit a
        dedicated structured ``compaction`` SSE event the frontend renders
        as an inline conversation line (the generic ``status`` event has no
        frontend listener). Unknown ``phase`` is silent rather than raising
        — this is a UX path and a typo must not break the agent loop.
        """
        if phase == "start":
            self.status(
                "info",
                f"Compacting context ({old_tokens:,} tokens, "
                f"{evicted_count} messages → summary)",
                0,
            )
        elif phase == "done":
            self.status(
                "info",
                f"Compaction done ({old_tokens:,} → {new_tokens:,} tokens)",
                0,
            )
        elif phase == "warning":
            self.status(
                "warning",
                f"Context compaction failed ({reason}); using FIFO drop instead.",
                0,
            )

    def note_record(self, hidx: int) -> None:
        """다음 카드 이벤트가 가리킬 history 레코드 서수 (v10.6.0). 기본 no-op —
        web 이 스레드 로컬로 받아 ``assistant_turn``/``observation``/``user_message``
        페이로드에 ``hidx`` 로 싣는다."""

    def context_view(self, view: dict) -> None:
        """컨텍스트 캐시의 "모델 시점" 사실 (v10.6.0, docs/inspector-model-view §3.1):
        ``{"gone": {"turn", "kind"} | None, "summary": {...} | None, "compactions", "folded_nudges"}``.
        기본 no-op — CLI 는 타임라인을 다시 그리지 않는다. web 이 스코프별 sticky
        ``ctx_view`` 로 override."""

    def push_user_message(
        self,
        content: str,
        author: str = "",
        hidx: int | None = None,
        images: list[dict] | None = None,
    ) -> None:
        """Echo a user message into the conversation surface (v8.6.0 ABC
        승격 — 종전 web 전용+hasattr 게이트).

        ``hidx`` (v10.6.0): 이 메시지의 history 레코드 서수 — 챗의 모델 시점
        (압축 경계) 판정에 쓴다. 레코드가 없는 UI 전용 에코는 None.
        ``images`` (v10.60.0): 메시지에 딸린 ``ImageRef`` dict — web 은 말풍선
        아래 썸네일, CLI 는 무시(런 시작 질의는 `🖼` 줄을 호출자가 찍는다).

        Web 은 persistent ``user_message`` 이벤트로 override; CLI 는 no-op
        기본 — 사용자가 방금 타이핑한 내용을 터미널에 다시 찍을 이유가
        없다(다중 사용자 큐/주입도 web 전용). 코어(loop)는 이 ABC 표면만
        알고 무조건 호출한다. ``author`` = 보낸 USER 의 닉네임(팀뷰 사용자
        레인 라우팅), 비-사용자 발신(🤝 스타터)·CLI 는 빈값.
        """

    @abstractmethod
    def status(self, state: str, message: str, turn: int = 0) -> None:
        """Status update (running/done/error)."""

    def token_usage(self, stats: dict, turn: int, verbose: bool = False) -> None:
        """Per-turn token usage: in/out tokens (+speed), context-window
        occupancy %, and cumulative session output. ``stats`` is the
        render-agnostic dict from ``loop._build_token_stats``.

        Non-abstract with a no-op default so custom ``render/<name>.py``
        renderers keep working without implementing it; MinimalRenderer
        (CLI line) and WebRenderer (top-bar SSE) override.
        """

    def usage_totals(self, totals) -> None:
        """A main run ended: its summed calls (``loop.llm.UsageTotals`` —
        input·cached·output·calls·llm_ns). Called only when the run made at
        least one call. No-op default: WebRenderer's top bar already carries
        the running sums (Σ↑·Σ↓, calls in the tooltip); MinimalRenderer prints
        one ``Total:`` line (``format_usage_totals``).
        """

    def model_detected(
        self, model: str, capabilities, provider: str, saved_path: str
    ) -> None:
        """Newly detected model info. Default (C8): 코어 ``status`` 위임 —
        최소 구현(코어 7)만으로도 정보가 표출된다. 리치 표(스펙 카드)가
        필요한 렌더러만 override."""
        self.status(
            "done",
            f"model detected: {model} (provider={provider}, saved={saved_path})",
        )

    def model_loaded(self, model: str, capabilities) -> None:
        """Loaded model one-liner. Default (C8): 코어 ``status`` 위임."""
        self.status("done", f"model loaded: {model}")

    def consume_directives_dirty(self) -> bool:
        """Atomically read+clear the 'DIRECTIVE.md was edited via the web
        Prompt Inspector' flag. The loop checks it each LLM call to rebuild its
        system prompt. Only the web renderer tracks it; others never edit."""
        return False

    def broadcast_directives_changed(self) -> None:
        """Tell open Prompt Inspectors to re-fetch (editor + prompt view). Fired
        on save (editor sync) and when the edit is actually applied (prompt
        view reflects the now-live directive). No-op off the web renderer."""

    def broadcast_memory_changed(self) -> None:
        """Tell open Prompt Inspectors to re-fetch the prompt view after a
        `memory` op changed the `## Session Memory` index. Memory has no editor
        (unlike directives), so this refreshes the prompt only. No-op off web."""

    def spinner_start(self, message: str = "") -> None:
        """Start a spinner animation (e.g. during LLM call). Default
        no-op (C8) — 장식; 애니메이션 가능한 매체만 override."""

    def spinner_stop(self) -> None:
        """Stop the spinner animation. Default no-op (C8)."""

    def stream_chunk(self, text: str) -> None:
        """Render a streaming chunk from LLM response. Default: no-op."""

    def thinking_chunk(self, text: str) -> None:
        """Streaming reasoning delta (P5, v8.56.0). Default: no-op —
        선택 구현(필수 9개 계약 불변). 진행 카운터용이며 transcript 에
        싣지 않는 것이 계약이다."""

    def tool_call_chunk(self, text: str) -> None:
        """Streaming tool-call delta (v10.70.0, native_fc). Default: no-op —
        진행 카운터용 셋째 채널; transcript 에 싣지 않는다."""

    def stream_end(self) -> None:
        """Signal end of streaming. Default: no-op."""

    # ── User input ──────────────────────────────────

    def can_prompt(self) -> bool:
        """사람에게 물은 것(``ask`` 의 질문·확인 `c-N`)에 답이 올 수 있는 표면인가 — 거짓이면 묻는 쪽이 묻지 않고
        거절한다(오지 않을 답을 기다리지 않게). Default ``True``; 터미널에 달린 렌더러(CLI)는 터미널 여부로 답한다."""
        return True

    @contextmanager
    def _prompt_display_guard(self):
        """Context wrapping a blocking user read so the renderer can quiet
        any output that would clash with it.

        Default no-op. ``MinimalRenderer`` overrides it to pause an active
        Rich ``Live`` region (the spinner) that would
        otherwise repaint over the prompt; ``WebRenderer`` needs nothing
        (its prompt is an out-of-band SSE event).
        """
        yield

    def _guarded_read(self, read):
        """막는 읽기 하나를 터미널을 읽는 잠금(`interactive_lock`)과 화면 가드 아래서 — 부팅 마법사의 ``prompt_user``
        가 쓴다(세션의 터미널 읽기와 겹치지 않는다). ``read`` 는 인자 없는 막는 읽기, 그 값을 그대로 돌려주고 예외
        (EOF·Ctrl+C)는 부르는 쪽으로 올린다."""
        with interactive_lock, self._prompt_display_guard():
            self._drop_typeahead()
            return read()

    def _drop_typeahead(self) -> None:
        """프롬프트는 뜬 뒤에 그 프롬프트에 친 것만 받는다(사람의 답은 명시적 UI 로만 — 사용자 결정). 프롬프트가 터미널을
        쥔 순간(`_guarded_read` 의 잠금 안, 프롬프트 글을 찍기 전) 그 전에 친 입력을 버린다 — 부팅 마법사뿐이라 넘길 main
        이 아직 없다. 표준입력이 터미널이 아니거나 터미널 제어가 없으면 할 일이 없다."""
        if termios is None or not sys.stdin.isatty():
            return
        try:
            fd = sys.stdin.fileno()
            while select.select([fd], [], [], 0)[0]:
                if not os.read(fd, 65536):
                    break
            termios.tcflush(fd, termios.TCIFLUSH)
        except (OSError, ValueError):
            return
