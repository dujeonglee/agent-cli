"""열린 항목 장부 — 이 런이 끝나기 전에 닫아야 하는 것 (docs/ledger/DESIGN.md).

- P1 (v10.54.0): 흩어져 있던 회계를 한 객체로.
- P2 (v10.55.0): main 앞 메일 7종이 요청으로 오르고, 열린 항목 전부가 압축 핀.
- P3 (v10.56.0): **장부 하나 · 정책 하나 · 순서 하나.**

  - 꼬리 블록 하나 ``## Open Items`` — 행동으로 닫는 것(회신 빚 ``reply``, 질문
    답 빚 ``answer``) 먼저, ``complete`` 의 ``answers`` 로 닫는 것(사람 요청·메일)
    마지막. 종전의 ``## Open Requests``·``## Owed Replies`` 두 블록을 합쳤다.
  - 정책은 **계속형 붙잡기 + 런 공유 예산** — ``complete`` 결과는 언제나 배달하고,
    열린 항목이 남으면 예산(:data:`NAG_BUDGET`) 안에서 되돌린다. 종전엔 되묻기
    런당 1회(결과 미배달)·요청 독촉 요청당 1회·빚 독촉 런 공유 3회(비사용자 런은
    결과 미배달 거부)로 셋이 달랐다.

출처: 사람 요청과 메일은 이 런이 소유한다(``{"id", "author", "text"}``). 빚은
레지스트리가 소유하고 여기서는 질문 포트로 읽는 뷰다(`constants.open_debts`).
id 는 전부 ``<접두>-<토큰>``(`item_ids`) — 접두가 닫는 법을 말한다.
"""

from __future__ import annotations

from agent_cli import item_ids

#: 한 런이 쓰는 되돌림 예산 — 되묻기·독촉이 전부 나눠 쓴다(사용자 결정 8, 2026-10-18).
#: 다 쓰면 남은 항목을 하니스가 닫는다: 요청은 최종 답에 미답으로, 빚은 런 끝의
#: 폴백(회신 빚은 런 요약 배달, 질문은 닫힘).
NAG_BUDGET = 8

_HEAD = "## Open Items — close them in this order; `complete` is always the last step"
_TAIL = (
    "  The run stays open until every item is closed; your result is delivered "
    "either way."
)


def complete_line(ids: list[str]) -> str:
    """``complete`` 절의 마지막 줄 — 닫을 id 를 담은 호출 모양 (§11.1 변형 A).

    재생 측정(§11.6)이 변형 B(문장)와 고른다 — 이 함수 하나를 바꾼다."""
    quoted = ", ".join(f'"{i}"' for i in ids)
    return f'     → complete(result="...", answers=[{quoted}])'


def _debt_line(d: dict) -> str:
    """빚 한 줄 — 닫는 도구를 같이 적는다."""
    who = str(d.get("to") or "").split(":", 1)[-1]
    text = str(d.get("text") or "").strip()[:120]
    did = d.get("id", "")
    if d.get("kind") == "answer":
        return (
            f'     [{did}] ({who}) question: "{text}"  → answer(id="{did}", text="...")'
        )
    return f'     [{did}] ({who}) reply owed: "{text}"  → reply(id="{did}", text="...")'


def _request_line(r: dict) -> str:
    who = f" ({r['author']})" if r.get("author") else ""
    return f'     [{r.get("id")}]{who} "{(r.get("text") or "").strip()[:120]}"'


class Ledger:
    """런 수명 장부. 이월은 없다 — 다음 런은 새 장부로 시작한다."""

    def __init__(self) -> None:
        #: `complete` 의 `answers` 로 닫는 항목 — 사람 요청·메일, 도착 순서.
        self.requests: list[dict] = []
        #: 남은 되돌림 예산 (:data:`NAG_BUDGET`).
        self.budget = NAG_BUDGET
        self._port = None

    # ── 출처 ──────────────────────────────────────────────

    def bind_questions(self, port) -> None:
        """빚을 읽을 질문 포트(`LoopPorts.questions`). None = 빚 없음."""
        self._port = port

    def add_request(self, rid: str, author: str, text: str) -> None:
        self.requests.append(
            {"id": str(rid), "author": author or "", "text": text or ""}
        )

    # ── 읽기 ──────────────────────────────────────────────

    def request_ids(self) -> list[str]:
        return [str(r.get("id")) for r in self.requests]

    def pin_ids(self) -> list[str]:
        """압축 핀을 걸 id — 열린 항목 전부. 그 원문 레코드가 같은 id 를
        ``request_id`` 로 싣는다(P2)."""
        return self.request_ids() + [str(d["id"]) for d in self.debts() if d.get("id")]

    def has_request(self, rid: str) -> bool:
        return any(str(r.get("id")) == rid for r in self.requests)

    def claimed_requests(self, claimed: set[str] | None) -> list[dict]:
        """`complete` 이 주장한 요청들 — **정산 전에** 뽑는다(정산이 지운다)."""
        if not claimed:
            return []
        return [r for r in self.requests if str(r.get("id")) in claimed]

    def debts(self) -> list[dict]:
        """아직 갚지 않은 회신·질문 빚 — 포트가 없거나 깨지면 빈 목록."""
        from agent_cli.constants import open_debts

        return open_debts(self._port)

    # ── 정산 ──────────────────────────────────────────────

    def settle(self, claimed: set[str] | None) -> list[dict]:
        """주장된 요청을 지우고 아직 열린 것을 돌려준다.

        ``None``(``answers`` 생략)은 **모름** — 아무것도 닫지 않는다(P3: 종전엔
        모름을 "전부 닫음" 으로 처리해 진짜 미답이 꼬리·핀에서 사라졌다). 빈
        집합(``answers: []``)은 "아무것도 안 닫음" 이라는 진술 — 역시 아무것도
        닫지 않는다. 둘의 차이는 되돌림 문구와 최종 통지에서만 난다."""
        if claimed:
            self.requests[:] = [
                r for r in self.requests if str(r.get("id")) not in claimed
            ]
        return list(self.requests)

    def spend(self) -> bool:
        """되돌림 한 번을 쓴다 — 남았으면 True."""
        if self.budget <= 0:
            return False
        self.budget -= 1
        return True

    # ── 꼬리 ──────────────────────────────────────────────

    def items_lines(self, requests: list[dict], debts: list[dict]) -> str:
        """열린 항목을 닫는 순서대로 — 꼬리와 되돌림 관찰이 같은 줄을 쓴다."""
        out: list[str] = []
        if debts:
            out.append("  1. by action")
            out += [_debt_line(d) for d in debts]
        if requests:
            out.append("  2. by `complete` (last)")
            out += [_request_line(r) for r in requests]
            out.append(complete_line([str(r.get("id")) for r in requests]))
        return "\n".join(out)

    def block(self) -> str:
        """``## Open Items`` — 열린 것이 없으면 빈 문자열. **1건에도 싣는다**(모델이
        id 어휘를 배울 기회 — 합쳐진 런에서만 보이면 처음 본 필드를 곧바로 채우라는
        요구가 된다)."""
        debts = self.debts()
        if not self.requests and not debts:
            return ""
        return f"{_HEAD}\n{self.items_lines(self.requests, debts)}\n{_TAIL}"

    def lines_for_notice(self, requests: list[dict]) -> str:
        """최종 답 말미의 미답 목록 — 사람이 읽는 줄."""
        return "\n".join(
            f"   [{r.get('id')}]"
            + (f" ({r['author']})" if r.get("author") else "")
            + f' "{(r.get("text") or "").strip()[:120]}"'
            for r in requests
        )


def wrong_closer_hint(item_id: str, used: str) -> str:
    """``used`` 도구로 닫을 수 없는 id 에 — 그 항목을 닫는 법 (§11.4).

    접두가 닫는 법을 말하므로 판정은 첫 글자 하나다. 모르는 형식이면 ``""``."""
    closer = item_ids.closer_of(item_id)
    if not closer or closer == used:
        return ""
    if closer == "complete":
        return (
            f"'{item_id}' is an open item closed by `complete` — put your answer in "
            f'`complete`\'s `result` and list it: answers=["{item_id}"]'
        )
    return f'\'{item_id}\' is closed with {closer}(id="{item_id}", text="...")'
