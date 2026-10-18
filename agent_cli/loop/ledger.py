"""열린 항목 장부 — 이 런이 끝나기 전에 닫아야 하는 것 (docs/ledger/DESIGN.md).

P1 (v10.54.0): 흩어져 있던 회계를 한 객체로 모은다. 종전엔 `LoopState` 의 필드 넷
(`run_requests`·`answers_prompted`·`requests_nagged`·`debt_nags`)과 dispatch 의
정산 함수, 꼬리 조립(llm), 압축 핀(core 의 람다)이 각자 같은 목록을 만졌다.

- **사용자 요청**은 이 런이 소유한다 — ``{"id", "author", "text"}``. 런 스타터 1건 +
  턴 경계 drain 으로 들어온 N건(웹 큐가 발급한 id 를 그대로 쓴다; 상주는 inbox seq).
- **빚**(회신·질문 답)은 레지스트리가 소유하고, 여기서는 질문 포트로 읽는 뷰다
  (`constants.open_debts`). 포트가 없으면 빚도 없다.

모델이 보는 글은 종전과 바이트 동일하다(`tests/test_ledger_golden.py`) — 꼬리
블록은 `constants` 의 렌더러를 그대로 부른다. 장부가 바꾸는 것은 **누가 그 사실을
쥐는가** 하나다.
"""

from __future__ import annotations


class Ledger:
    """런 수명 장부. 이월은 없다 — 다음 런은 새 장부로 시작한다."""

    def __init__(self) -> None:
        #: 이 런이 응답해야 할 사용자 요청 — 도착 순서.
        self.requests: list[dict] = []
        #: `answers` 를 이미 한 번 되물었나 — 되묻기는 런당 **한 번**. 끝내 안
        #: 실으면 받아주고 "미신고" 로 적는다(무한 되묻기는 런을 태운다).
        self.answers_prompted = False
        #: 이미 독촉한 요청 id — 요청당 **한 번**. 결과는 이미 나갔고 남은
        #: 요청만 가리킨다(붙잡기가 아니다).
        self.requests_nagged: set[str] = set()
        #: 빚이 남은 `complete` 를 독촉·거부한 횟수 — 런 공유, 상한은
        #: ``dispatch.MAX_DEBT_NAGS``. 상한 뒤엔 레지스트리가 런 요약을 라벨 붙여
        #: 폴백 배달하고 질문은 닫는다 — 요청자가 침묵을 받진 않는다.
        self.debt_nags = 0
        self._port = None

    # ── 출처 ──────────────────────────────────────────────

    def bind_questions(self, port) -> None:
        """빚을 읽을 질문 포트(`LoopPorts.questions`). None = 빚 없음."""
        self._port = port

    @property
    def resident(self) -> bool:
        """상주 루프인가 — 빚을 갚는 도구가 다르다(`reply` 대 `agent request`)."""
        return bool(getattr(self._port, "nonblocking", False))

    def add_request(self, rid: str, author: str, text: str) -> None:
        self.requests.append(
            {"id": str(rid), "author": author or "", "text": text or ""}
        )

    # ── 읽기 ──────────────────────────────────────────────

    def request_ids(self) -> list[str]:
        """열린 요청 id — 압축 핀(`ContextManager.set_open_requests`)이 라이브로 읽는다."""
        return [str(r.get("id")) for r in self.requests]

    def pin_ids(self) -> list[str]:
        """압축 핀을 걸 id — 열린 요청(사용자·메일)과 빚(질문 답·회신) 전부
        (P2, v10.55.0). 그 원문 레코드가 같은 id 를 ``request_id`` 로 싣는다.
        종전엔 사용자 요청만 핀했다 — 미답 질문·회신 빚의 원문은 압축에 요약될
        수 있었고 꼬리에는 120자만 남았다(설계 §3.6-4)."""
        return self.request_ids() + [str(d["id"]) for d in self.debts() if d.get("id")]

    def has_request(self, rid: str) -> bool:
        return any(str(r.get("id")) == rid for r in self.requests)

    def claimed_requests(self, claimed: set[str] | None) -> list[dict]:
        """`complete` 이 주장한 요청들 — **정산 전에** 뽑는다(정산이 지운다)."""
        if not claimed:
            return []
        return [r for r in self.requests if str(r.get("id")) in claimed]

    def debts(self) -> list[dict]:
        """아직 갚지 않은 message/ask 빚 — 포트가 없거나 깨지면 빈 목록."""
        from agent_cli.constants import open_debts

        return open_debts(self._port)

    # ── 정산 ──────────────────────────────────────────────

    def settle(self, claimed: set[str] | None) -> list[dict]:
        """주장된 요청을 **지우고** 아직 열린 것을 돌려준다.

        주장 = 닫힘이다. 남은 것이 곧 outstanding 이고, 그게 있으면 루프는
        끝나지 않는다. ``claimed is None`` (필드 생략)은 **모름**이다 — 1건이어도
        같다(v9.22.0: "결과가 곧 그 답" 은 사용자 요청과 에이전트 질문이 한 런에
        섞이면 틀리는 추측). 되묻기가 이미 한 번 요구했으니 여기서 회계를 닫고
        남은 것 전부를 미신고로 돌려준다."""
        if not self.requests:
            return []
        if claimed is None:
            open_now = list(self.requests)
            self.requests.clear()
            return open_now
        remaining = [r for r in self.requests if str(r.get("id")) not in claimed]
        self.requests[:] = remaining
        return list(remaining)

    # ── 꼬리 ──────────────────────────────────────────────

    def requests_block(self) -> str:
        """``## Open Requests`` — **1건에도 싣는다**(합쳐진 런에서만 보이면 모델이
        id 어휘를 배울 기회가 없다)."""
        if not self.requests:
            return ""
        from agent_cli.constants import outstanding_requests_block

        return outstanding_requests_block(self.requests)

    def debts_block(self) -> str:
        """``## Owed Replies`` — 갚는 도구와 함께."""
        from agent_cli.constants import owed_replies_block

        return owed_replies_block(self.debts(), resident=self.resident)

    def debt_lines(self, debts: list[dict]) -> str:
        """빚 목록을 갚는 수단과 함께 — 꼬리와 거부·독촉 관찰이 같은 줄을 쓴다."""
        from agent_cli.constants import debt_lines

        return debt_lines(debts, resident=self.resident)
