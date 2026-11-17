"""2단계 그림자 비교기 — 아직 남은 옛 저장소(private 메일함)와 우편(`AgentRegistry._post`)이 같은 것을 말하는가
(발판, 조각 3 에서 지운다).

conftest 의 자동 픽스처가 부른다: 저장 지점(`_save_state`)마다, 그리고 테스트 정리 때. 제품 코드에는 비교 호출이
없다. 어긋나면 `ShadowMismatch` 를 던진다 — 삼키지 않는다.

조각 1(받은편지함·거울·든 것·main 메일함·빚·보낸 쪽 기록)·조각 2(질문)의 칸은 옛 저장소와 함께 지웠다 — 저장 파일의
모양은 고정 표본(`tests/test_agents_state_sample.py`)이 지킨다.

비교 칸
- private 메일함(``_mailboxes``) ↔ 그 주소의 queued
- 우편 자신의 불변식(`PostOffice.check_consistency`)
"""

from __future__ import annotations


class ShadowMismatch(AssertionError):
    pass


def _ids(envs) -> list[str]:
    return [e.id for e in envs]


def compare(reg, *, final: bool) -> None:
    post = getattr(reg, "_post", None)
    if post is None:
        return
    problems: list[str] = []
    with reg._cv:
        post.check_consistency()
        for addr, mails in reg._mailboxes.items():
            old_p = [str(m.get("item_id") or "") for m in mails]
            if old_p != _ids(post.queued(addr)):
                problems.append(f"{addr} queued {old_p} != {_ids(post.queued(addr))}")
    if problems:
        raise ShadowMismatch("; ".join(problems))
