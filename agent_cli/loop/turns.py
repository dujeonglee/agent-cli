"""동시 병렬 턴 레지스트리 — 다중 사용자 병렬 턴(A1)의 코어.

포크(Coagora)가 먼저 구현·실측 검증한 "병렬 추론 + 직렬 부수효과" 계약의
디스패치 계층을 본류로 역병합한 것이다 (docs/research/11-upstream-merge-plan.md
§2 M3, 원본 ``backend/src/agent/pi.ts`` 의 ``RuntimeHandle``·``pumpConcurrent``).

**여기서 "턴"이란 사용자 메시지 1건을 완료까지 처리하는 단위**다 — 본류에서는
``run_loop()`` 한 번(내부의 ReAct 반복 전체)이 그 단위이며, 포크의
``runAgentTurn`` 과 같은 입자다. LLM 추론은 HTTP 스트리밍 대기라 GIL 영향이
작아 스레드로 충분하다(계획 §4).

설계 규칙 3가지 — 전부 포크에서 근거가 확인된 것:

1. **turnId 는 디스패치 시점에 발급한다** (``pi.ts:240-244``). enqueue 시점이
   아니다. 그래야 ``turn_seq`` 가 실제 디스패치 순서와 일치하고,
   :meth:`interrupt` 가 "실제로 시작된 턴"만 가리킨다. 아직 시작 안 된 대기
   항목은 id 가 없으므로 취소가 아니라 큐에서 빼는 문제다(호출자 소관).
2. **cap 초과분은 FIFO 로 대기하고, 슬롯이 비면 펌프가 전진시킨다**
   (``pi.ts:226-237``). 완료/실패 **양쪽**에서 슬롯을 회수해야 한다 — 한쪽만
   회수하면 실패한 턴이 슬롯을 영구 점유해 세션이 굳는다.
3. **레거시 경로와 완전히 분리한다** (``pi.ts:559-568`` 의 조기 분기). 이
   모듈은 직렬 모드에서 **생성조차 되지 않는다** — 직렬 경로가 이 코드를 한
   줄도 지나가지 않는 것이 "기존 동작 보존"의 유일하게 확실한 방법이다.

이 모듈이 **하지 않는** 것: per-user 1활성턴 게이트와 라운드로빈 공정 큐는
M5(A5) 소관이라 넣지 않았다. 지금은 순수 FIFO + cap 이다. 부수효과 직렬화도
여기가 아니라 M4 의 효과 락이 담당한다 — 여기서 병렬화되는 것은 **추론**이다.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass, field

from agent_cli import turn_metrics

#: 한 세션의 동시 inflight 턴 상한 기본값. 포크 ``config.maxConcurrentTurns``
#: 대응. 4 는 포크가 쓴 값이며, 본류에서 다시 실측할 대상이다(P4).
DEFAULT_MAX_CONCURRENT_TURNS = 4


@dataclass
class Turn:
    """디스패치된 턴 하나. ``id`` 는 디스패치 시점에 부여된다."""

    id: str
    text: str
    #: 발화자 닉네임 (CLI/단일 사용자는 None).
    author: str | None = None
    #: 웹 연결 식별자 — 소유권 판정용(취소 권한 등). 없으면 "".
    conn_id: str = ""
    #: 이 턴만 중단시키는 플래그. 세션 전역 ``/api/stop`` 과 별개로,
    #: ``AgentLoop`` 가 턴 경계·스트리밍 도중 양쪽에서 관측한다.
    stop_event: threading.Event = field(default_factory=threading.Event)
    #: 입력 큐(InputQueue) 항목 id — 계측(M2)이 enqueue 이벤트(큐 id 만
    #: 있음)와 dispatch 이벤트(턴 id 발급 후)를 잇는 상관 키. 계측 off 나
    #: 큐를 거치지 않은 제출은 "".
    queue_id: str = ""
    #: P1 requester-supplied exact file capability and optional oracle.
    write_paths: list[str] = field(default_factory=list)
    expected_contents: dict[str, str] = field(default_factory=dict)


class TurnRegistry:
    """활성 턴 레지스트리 + cap 기반 디스패처.

    ``runner(turn)`` 은 턴 하나를 **완료까지** 실행하는 콜러블이다(본류에서는
    ``run_loop`` 을 감싼 클로저). 레지스트리는 그것을 전용 스레드에서 돌리고,
    끝나면 성공/실패와 무관하게 슬롯을 회수한 뒤 대기열을 전진시킨다.

    스레드 안전: 하나의 락이 활성 맵·대기열·시퀀스를 지킨다. 락 구간에서는
    자료구조 조작과 ``Thread.start()`` 만 하고 ``runner`` 는 절대 부르지
    않는다 — 사용자 코드를 락 아래서 실행하면 그 코드가 잡는 다른 락(ctx·
    renderer·효과 락)과 순환이 생길 수 있다.
    """

    def __init__(
        self,
        runner: Callable[[Turn], None],
        *,
        max_concurrent: int = DEFAULT_MAX_CONCURRENT_TURNS,
        on_change: Callable[[], None] | None = None,
        per_user_gate: bool = True,
    ):
        if max_concurrent < 1:
            raise ValueError("max_concurrent must be >= 1")
        self._runner = runner
        self._max_concurrent = max_concurrent
        #: per-user 1활성턴 게이트 (M5/A5). False 는 **실험 전용**(P4 ablation
        #: 대조군 — 순수 FIFO+cap): 한 사용자의 연속 제출이 cap 을 독식해
        #: 다른 사용자가 그 백로그 뒤에 줄을 서는 동작을 재현한다.
        self._per_user_gate = per_user_gate
        #: 활성 턴 수가 바뀔 때마다 호출 — 상태 브로드캐스트(SSE/status.json)를
        #: 얹는 자리. **락 밖에서** 부른다(renderer 락과의 중첩 회피 —
        #: ``InputQueue.on_change`` 와 같은 규율).
        self.on_change = on_change

        self._lock = threading.Lock()
        #: ``on_change`` 배달 직렬화 전용 락 (:meth:`_notify` 참조). ``_lock``
        #: 과 분리한 이유: 콜백은 락 밖에서 불러야 하는데(renderer 락 중첩
        #: 회피), 그러면 배달 **순서**가 보장되지 않아 낡은 값이 최종값으로
        #: 남을 수 있다. 배달만 따로 줄 세운다.
        self._notify_lock = threading.Lock()
        self._active: dict[str, Turn] = {}
        self._pending: list[Turn] = []
        self._threads: dict[str, threading.Thread] = {}
        self._turn_seq = 0
        self._shutdown = False
        #: 활성·대기가 모두 0 이 되면 set. 정지 판정/테스트가 기다린다.
        self._idle = threading.Event()
        self._idle.set()

    # ── 조회 ────────────────────────────────────────────

    def active_count(self) -> int:
        """현재 inflight 턴 수. 포크 ``activeTurnCount`` 대응.

        대기 중(아직 디스패치 안 된) 턴은 **세지 않는다** — "지금 추론 중인
        턴이 몇 개인가"와 "일이 남았는가"는 다른 질문이라, 후자는
        :meth:`is_busy` 가 답한다.
        """
        with self._lock:
            return len(self._active)

    def pending_count(self) -> int:
        """cap 에 막혀 대기 중인 턴 수."""
        with self._lock:
            return len(self._pending)

    def is_busy(self) -> bool:
        """활성이든 대기든 처리할 일이 남았는가.

        idle self-reap(``web_instance_is_active``)과 CLI 펌프의 정지 판정이
        읽는다 — 대기분을 빼먹으면 마지막 턴이 시작되기 전에 세션이 죽는다.
        """
        with self._lock:
            return bool(self._active or self._pending)

    def active_ids(self) -> list[str]:
        with self._lock:
            return list(self._active)

    def snapshot(self) -> list[dict]:
        """활성 턴 요약 — ``/api/turns`` 의 페이로드.

        싣는 것은 턴 id·작성자·연결 id 뿐이고 **메시지 본문(``text``)은 넣지
        않는다**. 이 표면은 관전 토큰으로도 읽히므로(라우트 표의 ``read``),
        필드를 더할 때는 "스트림이 이미 보여주는 것 이상을 노출하지 않는다"는
        관전 경계를 함께 확인해야 한다 — 본문은 렌더러가 정한 표시 규칙을
        거쳐 나가는데 여기로 새면 그 규칙을 우회한다.
        """
        with self._lock:
            return [
                {"id": t.id, "author": t.author, "conn_id": t.conn_id}
                for t in self._active.values()
            ]

    # ── 디스패치 ────────────────────────────────────────

    def submit(
        self,
        text: str,
        *,
        author: str | None = None,
        conn_id: str = "",
        queue_id: str = "",
        write_paths: list[str] | None = None,
        expected_contents: dict[str, str] | None = None,
    ) -> None:
        """턴 하나를 제출한다. cap 에 여유가 있으면 **같은 호출 안에서** 즉시
        디스패치되고, 아니면 FIFO 로 대기한다.

        turnId 를 반환하지 않는 이유: 대기로 떨어지면 아직 id 가 없다(규칙 1).
        시작된 턴의 id 가 필요하면 :meth:`active_ids` 나 ``on_change`` 로 관측한다.
        """
        with self._lock:
            if self._shutdown:
                return
            self._pending.append(
                Turn(
                    id="",
                    text=text,
                    author=author,
                    conn_id=conn_id,
                    queue_id=queue_id,
                    write_paths=list(write_paths or []),
                    expected_contents=dict(expected_contents or {}),
                )
            )
            self._idle.clear()
            started = self._pump_locked()
        if started:
            self._notify()

    def _has_active_user(self, conn_id: str) -> bool:
        """``conn_id`` 의 활성 턴이 하나라도 있는가 (락 보유 중 호출).

        빈 conn_id 는 **게이트 면제**이며 서로를 막지 않는다 — 포크
        ``hasActiveUser``(``pi.ts:212-218``)의 ``userId == null`` 규칙 그대로.
        CLI·MailWaker wake 아이템처럼 사람에게 귀속되지 않는 턴이 여기 해당하고,
        빈 값끼리 매칭시키면 그것들이 서로를 막아 병렬이 통째로 무너진다.

        ``activeTurns`` 스캔이 per-user 활성의 **단일 진실원천**이다 — 별도
        카운터 맵을 두면 취소·실패 경로에서 어긋나 영구 차단이 생긴다.
        """
        if not conn_id:
            return False
        return any(t.conn_id == conn_id for t in self._active.values())

    def _next_eligible_locked(self) -> int:
        """디스패치할 다음 대기 항목의 인덱스, 없으면 -1 (락 보유 중 호출).

        **FIFO 스캔 + 활성 사용자 스킵** — 큐를 머리→꼬리로 보며 "conn_id 가
        비었거나 그 conn_id 의 활성 턴이 없는" 첫 항목을 고른다. 한 사용자가 큐에
        N건을 쌓아도 per-user=1 이라 1건만 활성이 되고, 나머지는 스킵되어 **다른
        사용자가 슬롯을 얻는다**(기아 방지). 포크 ``pumpConcurrent``
        (``pi.ts:226-237``)의 규칙 그대로다.

        효과 락의 엄격 FIFO(추월 금지)와 방향이 반대인 것은 의도다: 저기서는
        **같은** 자원을 두고 다투므로 추월이 기아를 만들지만, 여기서는 **다른**
        사용자에게 기회를 주는 것이 공정성이다.
        """
        if not self._per_user_gate:
            return 0 if self._pending else -1  # ablation: 순수 FIFO+cap
        for i, cand in enumerate(self._pending):
            if not self._has_active_user(cand.conn_id):
                return i
        return -1

    def _pump_locked(self) -> bool:
        """cap 여유만큼 적격 대기 항목을 디스패치 (락 보유 중 호출).

        포크 ``pumpConcurrent``. 반환값은 "무언가 시작했는가" — 호출자가 락 밖에서
        ``on_change`` 를 부를지 판단한다.
        """
        started = False
        while not self._shutdown and len(self._active) < self._max_concurrent:
            idx = self._next_eligible_locked()
            if idx < 0:
                break  # 적격 후보 없음 — 완료 시 재펌프된다
            turn = self._pending.pop(idx)
            self._turn_seq += 1
            turn.id = f"t{self._turn_seq}"  # 규칙 1: 디스패치 시점 발급
            self._active[turn.id] = turn
            thread = threading.Thread(
                target=self._run_turn,
                args=(turn,),
                daemon=True,
                name=f"agent-turn-{turn.id}",
            )
            self._threads[turn.id] = thread
            thread.start()
            started = True
        return started

    def _run_turn(self, turn: Turn) -> None:
        """턴 워커 스레드 본체 — 성공/실패 무관하게 슬롯을 회수한다.

        ``runner`` 예외를 삼키는 이유: 한 턴의 실패가 다른 턴이나 세션 전체를
        죽이면 안 된다(레거시 ``_worker_loop`` 이 turn 예외를 렌더러 에러로
        흡수하는 것과 같은 규율). 실제 보고는 ``runner`` 안에서 한다.
        """
        # M2 계측: dispatch 는 여기(워커 스레드 진입)서 찍는다 — turnId 발급
        # 직후이고 runner 시작 직전이라 "추론이 시작될 수 있게 된 시각"이다.
        # _pump_locked 안에서 찍지 않는 이유는 락 아래서 콜백 금지 규율.
        turn_metrics.emit(
            "turn",
            phase="dispatch",
            turn_id=turn.id,
            queue_id=turn.queue_id or None,
            author=turn.author,
            conn_id=turn.conn_id or None,
        )
        try:
            self._runner(turn)
        except BaseException:
            pass
        finally:
            turn_metrics.emit(
                "turn",
                phase="complete",
                turn_id=turn.id,
                interrupted=turn.stop_event.is_set() or None,
            )
            with self._lock:
                self._active.pop(turn.id, None)
                self._threads.pop(turn.id, None)
                started = self._pump_locked()
                if not self._active and not self._pending:
                    self._idle.set()
            self._notify()
            if started:
                self._notify()

    def _notify(self) -> None:
        """``on_change`` 를 **한 번에 하나씩** 배달한다.

        직렬화가 필요한 이유 (낡은 값 고착): 콜백은 인자가 없고 구독자가
        :meth:`active_count` 로 **현재 값을 스스로 읽는다**. 두 턴이 동시에
        끝나면 A 가 값을 읽은 뒤 선점되고, 그 사이 B 가 읽고 쓰기까지 마친
        다음 A 가 자기 낡은 값을 덮어쓸 수 있다 — 레지스트리는 비었는데
        표시는 "1개 실행 중"으로 굳는다. 그 상태에서
        ``WebRenderer.worker_is_busy`` 가 영원히 True 라 idle self-reap
        (``--idle-timeout``)이 끝내 발동하지 않고 status.json 도 계속
        거짓말을 한다.

        이 락 아래서 콜백이 값을 **다시 읽기** 때문에 마지막 배달이 항상
        최신 상태를 본다 — 읽기와 쓰기가 같은 뮤텍스로 순서지어지므로.
        ``_lock`` 이 아니라 전용 락인 것은 규율 유지 때문이다: 콜백은
        여전히 ``_lock`` 밖에서 돌아 renderer 락과 중첩되지 않는다
        (획득 순서 ``_notify_lock`` → renderer ``_lock``, 역방향 없음).
        """
        cb = self.on_change
        if cb is None:
            return
        with self._notify_lock:
            try:
                cb()
            except Exception:
                pass  # 상태 표시 실패가 턴 진행을 막지 않는다

    # ── 제어 ────────────────────────────────────────────

    def interrupt(self, turn_id: str, *, conn_id: str | None = None) -> bool:
        """지정 턴만 중단한다 — 다른 동시 턴은 불간섭 (포크 ``interrupt(turnId)``).

        ``conn_id`` 를 주면 소유자만 취소할 수 있다(``InputQueue.cancel`` 과 같은
        소유권 규율). 알 수 없는/이미 끝난 id 는 False — 멱등하다.
        """
        with self._lock:
            turn = self._active.get(turn_id)
            if turn is None:
                return False
            if conn_id is not None and turn.conn_id != conn_id:
                return False
            turn.stop_event.set()
        turn_metrics.emit("turn", phase="interrupt", turn_id=turn_id)
        return True

    def interrupt_all(self) -> int:
        """모든 활성 턴 중단 — 세션 전역 ``/api/stop`` 의 병렬판."""
        with self._lock:
            turns = list(self._active.values())
            for t in turns:
                t.stop_event.set()
        return len(turns)

    def wait_idle(self, timeout: float | None = None) -> bool:
        """활성·대기가 모두 비워질 때까지 대기. 시간 내 조용해지면 True."""
        return self._idle.wait(timeout)

    def shutdown(self, *, timeout: float = 30.0) -> None:
        """새 디스패치를 막고, 활성 턴을 중단시킨 뒤 스레드를 수거한다.

        대기 항목은 시작된 적이 없으므로 그냥 버린다(취소 통보는 호출자 몫 —
        레거시 ``InputQueue`` 가 큐 표시를 소유하는 것과 같다).
        """
        with self._lock:
            self._shutdown = True
            self._pending.clear()
            threads = list(self._threads.values())
            for t in self._active.values():
                t.stop_event.set()
        for th in threads:
            th.join(timeout=timeout)
        with self._lock:
            if not self._active and not self._pending:
                self._idle.set()
        self._notify()
