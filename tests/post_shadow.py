"""2단계 그림자 비교기 — 옛 저장소(진실)와 우편(`AgentRegistry._post`)이 같은 것을 말하는가 (발판, 조각 1c 에서 지운다).

conftest 의 자동 픽스처가 부른다: 저장 지점(`_save_state`)마다 ``final=False``, 테스트 정리 때 ``final=True``. 제품
코드에는 비교 호출이 없다. 어긋나면 `ShadowMismatch` 를 던진다 — 삼키지 않는다.

비교 칸
- 상자: 에이전트 받은편지함 거울(``inbox_items``) ↔ 그 주소의 queued(순서까지), 지금 런이 든 것(``in_flight``) ↔
  taken(집합), main 메일함(``_pending``) ↔ main 의 queued(순서까지), private 메일함(``_mailboxes``) ↔ 그 주소의 queued
- 빚: 갚지 않은 사람 아닌 빚(``_reply_debts``) ↔ `owed_by`(순서까지), 사람 주소의 빚 ↔ 그 주소의 열린 u 줄,
  갚은 빚 ↔ 런 안에서 닫혀 남겨 둔 r 줄
- 보낸 쪽 기록(``_sent``) ↔ `awaited_by`
- 질문(``_questions``) ↔ q 줄(그 ``meta`` 가 같은 `Question`)
- 저장 파일: 옛 저장소에서 지은 ``agents.json`` 문서 == 우편에서 지은 문서(`agents_state.document_from_post`)
- 우편 자신의 불변식(`PostOffice.check_consistency`)

빼는 칸(이유)
- 명단에 없는 에이전트가 보낸 요청 메일(테스트가 main 메일함에 손으로 넣은 것) — 옛 쪽에 보낸 쪽 기록이 없다.
- 버그 4(추정 버그 재현 테스트 `TestReplyInTheMessageToMainGap`): 요청을 넣은 뒤 보낸 쪽 기록을 적는 틈에 회신이
  오면 옛 쪽에는 끝난 기다림이 영영 남는다. 우편이 옳다 — 그 테스트는 xfail 이라 어긋남이 xfail 로 보고된다.
- 런이 집었지만 아직 빚으로 적히지 않은 요청 — 옛 구현의 틈(`_begin_items` 와 런 시작 사이).
- main 이 꺼낸(taken) 요청 메일의 빚 — 우편은 꺼낼 때 줄을 집고(빚), 옛 구현은 루프가 그 메일을 관찰로 넣을 때 같은
  id 로 빚을 적는다(`_inject_mail`). 꺼내기만 하는 테스트 흐름에서는 옛 쪽에 끝까지 빚이 없다.
- 봉투 없이 적힌 빚(테스트가 `note_owes` 로 바로 세운 것) — 옛 구현은 보낸 쪽 기록을 적지 않고, 우편의 `owe` 줄은
  기다림으로 센다. 제품 경로의 빚은 언제나 봉투의 줄이다(런 시작·흡수·main 의 메일 흡수).
- D1: 재시작이 main 메일함에 되넣은 요청 — 옛 구현은 보낸 쪽 기록을 다시 적지 않는다(추정 버그 2). 우편은 상자에
  든 요청마다 줄이 있다. 사용자 결정을 기다리는 동안 그 id 는 보낸 쪽 기록 비교에서 뺀다.
- D2: 지금 런이 든 것의 **순서** — 옛 워커는 깨우지 않는 항목을 들고 있다가 받은편지함 끝으로 되돌린다
  (`_release_held`), 우편은 도착 순서를 지킨다. 집합만 비교한다.
- 저장 지점에서(``final=False``) 보낸 쪽 기록이 우편보다 늦는 칸 — 옛 구현은 회신을 배달한 **뒤** 다른 락 구간에서
  보낸 쪽 기록을 지운다(추정 버그 4·5 의 자리). 그 사이의 저장 지점에서는 "옛 쪽에만 있고, 우편에서는 런 안에서
  닫혀 남은 줄" 인 id 를 허용한다. 정리 때는 허용하지 않는다.
"""

from __future__ import annotations

from agent_cli.subagent import agents_state
from agent_cli.subagent.agent_mail import is_human_addr


class ShadowMismatch(AssertionError):
    pass


def _ids(envs) -> list[str]:
    return [e.id for e in envs]


def _taken_ids(reg, addr: str) -> set[str]:
    tm = reg._agents.get(addr.split(":", 1)[1]) if addr.startswith("agent:") else None
    return set() if tm is None else {str(i.get("id") or "") for i in tm.in_flight}


def compare(
    reg,
    *,
    final: bool,
    restored_main_requests: frozenset = frozenset(),
    bare_debts: frozenset = frozenset(),
) -> None:
    post = getattr(reg, "_post", None)
    if post is None:
        return
    problems: list[str] = []
    with reg._cv:
        if reg._shadow_busy and not final:
            return  # 의미 연산(런 끝 폴백·정지 회신) 도중 — 옛 쪽이 하나씩 배달하는 중이다
        post.check_consistency()
        tms = [t for t in reg._agents.values() if hasattr(t, "inbox_items")]
        if len(tms) != len(reg._agents):
            return  # 테스트가 명단을 대역으로 바꿨다
        retained = {
            r.rid: r for r in post.rows() if r.kind == "r" and r.state == "closed"
        }
        # ── 상자 ──
        for tm in tms:
            addr = f"agent:{tm.key}"
            old_q = [str(i.get("id") or "") for i in tm.inbox_items]
            if old_q != _ids(post.queued(addr)):
                problems.append(f"{addr} queued {old_q} != {_ids(post.queued(addr))}")
            old_t = {str(i.get("id") or "") for i in tm.in_flight}
            new_t = set(_ids(post.taken(addr)))
            # 저장 지점에서: 런 끝 정리(`run_ended` — 우편은 여기서 든 것을 비운다)와 런 정리(`_close_run` — 옛 쪽은
            # 여기서 비운다) 사이
            if old_t != new_t and (final or not new_t <= old_t):
                problems.append(
                    f"{addr} taken {sorted(old_t)} != {sorted(_ids(post.taken(addr)))}"
                )
        old_main = [str(m.get("item_id") or "") for m in reg._pending]
        if old_main != _ids(post.queued("main")):
            problems.append(f"main queued {old_main} != {_ids(post.queued('main'))}")
        for addr, mails in reg._mailboxes.items():
            old_p = [str(m.get("item_id") or "") for m in mails]
            if old_p != _ids(post.queued(addr)):
                problems.append(f"{addr} queued {old_p} != {_ids(post.queued(addr))}")
        # ── 빚 ──
        debtors = set(reg._reply_debts) | {
            r.dst for r in post.rows() if r.kind in ("r", "u")
        }
        for debtor in sorted(debtors):
            debts = reg._reply_debts.get(debtor, [])
            noted = {d.id for d in debts}
            # 저장 지점에서: 런이 집었지만(`_begin_items`·흡수) 런 시작이 아직 빚으로 적지 않은 것 — 옛 구현의 틈
            # 런이 집었지만(`_begin_items`·흡수) 런 시작이 아직 빚으로 적지 않은 것 — 옛 구현의 틈. 테스트가 그 틈에서
            # 멈추면(일꾼 없는 대역) 정리 때까지 남는다
            unnoted = _taken_ids(reg, debtor) - noted
            if debtor == "main":
                # main 이 꺼낸 요청 메일 — 옛 구현은 루프가 관찰로 넣을 때(loop/core.py `_inject_mail`) 같은 id 로
                # 빚을 적는다. 꺼내기만 하고 넣지 않는 테스트 흐름에서는 끝까지 적히지 않는다
                unnoted |= {e.id for e in post.taken("main")} - noted
            open_old = [d.id for d in debts if not d.settled and not d.human]
            open_new = [r.rid for r in post.owed_by(debtor) if r.rid not in unnoted]
            if open_old != open_new:
                problems.append(f"{debtor} owes {open_old} != {open_new}")
            human_old = sorted(d.id for d in debts if d.human)
            human_new = sorted(
                r.rid for r in post.open_requests(debtor) if r.rid not in unnoted
            )
            if debtor != "main" and human_old != human_new:
                problems.append(f"{debtor} human {human_old} != {human_new}")
            paid_old = sorted(d.id for d in debts if d.settled and not d.human)
            paid_new = sorted(rid for rid, r in retained.items() if r.dst == debtor)
            if paid_old != paid_new:
                problems.append(f"{debtor} paid {paid_old} != {paid_new}")
        # ── 보낸 쪽 기록 ──
        requesters = set(reg._sent) | {
            r.src for r in post.rows() if r.kind == "r" and not is_human_addr(r.src)
        }
        for req in sorted(requesters):
            if req.startswith("agent:") and req.split(":", 1)[1] not in reg._agents:
                continue  # 명단에 없는 보낸 쪽 — 테스트가 메일을 손으로 넣었다(제품의 요청은 살아 있는 에이전트가 보낸다)
            old_s = set(reg._sent.get(req, {}))
            new_s = (
                {r.rid for r in post.awaited_by(req)}
                - set(restored_main_requests)
                - set(bare_debts)
            )
            extra_old, extra_new = old_s - new_s, new_s - old_s
            # 버그 4 — 옛 쪽에만 남은, 우편에서는 이미 닫혔거나 사라진 기다림(옛 쪽이 회신이 온 뒤에 기록을 적었다)
            extra_old = {
                i for i in extra_old if (row := post.row(i)) and row.state != "closed"
            }
            if not final:
                # 옛 두 락 구간 사이: 회신·폴백·정지·removed 를 배달한 뒤 보낸 쪽 기록을 지우기 전(우편의 줄은 이미
                # 닫혔다), 요청을 넣은 뒤 보낸 쪽 기록을 적기 전(우편의 줄은 이미 queued — 추정 버그 4 의 자리)
                extra_old = {
                    i
                    for i in extra_old
                    if (row := post.row(i)) and row.state != "closed"
                }
                extra_new = {
                    i
                    for i in extra_new
                    if (row := post.row(i)) and row.state not in ("queued", "picked")
                }
            if extra_old or extra_new:
                problems.append(f"{req} awaits {sorted(old_s)} != {sorted(new_s)}")
        # ── 질문 ──
        qs = {r.rid: r for r in post.open_questions()}
        if list(reg._questions) != list(qs):
            problems.append(f"questions {list(reg._questions)} != {list(qs)}")
        for qid, q in reg._questions.items():
            if qid in qs and qs[qid].meta is not q:
                problems.append(f"question {qid} meta is not the registry's Question")
        # ── 저장 파일 ──
        old_doc = agents_state.state_document(
            [
                agents_state.agent_entry(
                    tm, inbox=tm.inbox_items, in_flight=tm.in_flight
                )
                for tm in tms
            ],
            [dict(r) for r in reg._pending if agents_state.persists(r)],
            [q.as_dict() for q in reg._questions.values()],
        )
        new_doc = agents_state.document_from_post(tms, post)
        if old_doc != new_doc and not _same_but_held_order(old_doc, new_doc):
            problems.append("agents.json built from the post office differs")
    if problems:
        raise ShadowMismatch("; ".join(problems))


def _same_but_held_order(old: dict, new: dict) -> bool:
    """D2 — 지금 런이 든 항목의 순서만 다르면 같은 것으로 본다."""
    if {k: v for k, v in old.items() if k != "agents"} != {
        k: v for k, v in new.items() if k != "agents"
    }:
        return False
    for a, b in zip(old["agents"], new["agents"], strict=True):
        key = lambda i: str(i.get("id") or "")
        if {k: v for k, v in a.items() if k != "in_flight"} != {
            k: v for k, v in b.items() if k != "in_flight"
        }:
            return False
        if sorted(a["in_flight"], key=key) != sorted(b["in_flight"], key=key):
            return False
    return True
