"""사람에게 건 확인 — 열기·제자리에서 기다리기·답·목록 (6d-1, 질문 믹스인과 같은 모양).

`AgentRegistry` 가 이 믹스인을 받는다 — 메서드의 ``self`` 는 레지스트리다(우편 `_post`·명단 `_agents`·조건변수
`_cv` 를 레지스트리의 것으로 쓴다). 확인은 장부의 c 줄이고(`agent_cli.confirm.Confirm` 이 ``meta``), 저장하지 않는다 —
프로세스가 끝나면 기다리던 호출도 함께 끝난다.
"""

from __future__ import annotations

from agent_cli import render as render_surface
from agent_cli.agent_icon import agent_display_name
from agent_cli.confirm import (
    NO_ALWAYS,
    NOT_A_PERSON,
    STOP_POLL_S,
    STOPPED,
    UNKNOWN,
    Confirm,
    Decision,
)
from agent_cli.subagent.agent_mail import is_human_addr


class ConfirmsMixin:
    """`AgentRegistry` 의 확인 메서드 묶음."""

    def open_human_confirms(self) -> list[Confirm]:
        """열린 확인 — 연 순서."""
        return [r.meta for r in self._post.open_confirms()]

    def ask_confirm(self, c: Confirm, *, stopped, wait_input) -> Decision:
        """확인 ``c`` 를 열고 답이나 정지가 올 때까지 이 스레드에서 기다린다 — 답(`Decision`) 또는 ``STOPPED``.

        표면은 둘이다: 터미널의 확인 줄(`Renderer.confirm_line`)과 웹 트레이의 목록(`open_confirms`). 깨우는 것은
        답(`answer_confirm` 이 조건변수를 깨운다)과 ``stopped()`` — 그 런의 정지이고, 세션 종료도 정지를 세운다.
        ``wait_input(timeout)`` 은 기다리는 동안 터미널에서 친 한 줄을 읽는 법(`run` — 확인 명령은 읽는 그 자리에서
        답이 된다), 없으면(웹) 조건변수만 본다. 판정은 한 락 아래 "답 → 정지" 순서다."""
        renderer = render_surface.get_renderer()
        c.provenance = renderer.prompt_meta()
        self._post.open_confirm(c.id, c.asker, c.title, meta=c)
        renderer.confirm_line(self.confirm_label(c), c)
        self._notify_confirms()
        try:
            while True:
                with self._cv:
                    if c.decision is not None:
                        return c.decision
                    if stopped is not None and stopped():
                        self._post.close_confirm(c.id, "undeliverable")
                        return Decision(STOPPED)
                    self._cv.wait(timeout=STOP_POLL_S)
                if wait_input is not None:
                    wait_input(STOP_POLL_S)
        finally:
            # 예외로 떠나도(SIGTERM 의 풀기 등) 줄을 남기지 않는다 — 이미 닫혔으면 아무것도 안 한다
            self._post.close_confirm(c.id, "undeliverable")
            self._notify_confirms()

    def answer_confirm(self, cid: str, action: str, note: str, *, by: str) -> str:
        """사람이 ``cid`` 에 답한다(명령·트레이) — 에러 또는 빈 문자열. 답을 채우는 것과 줄을 닫는 것이 한 락
        구간이라 두 답 중 하나만 답이 된다. ``user*`` 는 하나의 주체다(질문과 같다)."""
        with self._cv:
            row = self._post.row(cid)
            c = row.meta if row is not None and row.kind == "c" else None
            if c is None:
                return UNKNOWN.format(cid=cid)
            if not is_human_addr(by):
                return NOT_A_PERSON.format(cid=cid, by=by)
            if action not in {ch.action for ch in c.choices}:
                return NO_ALWAYS.format(cid=cid)
            c.decision = Decision(action, (note or "").strip(), by)
            self._post.close_confirm(cid, "user_answered")
        self._notify_confirms()
        return ""

    def confirm_label(self, c: Confirm) -> str:
        """묻는 쪽 이름표 — 에이전트는 질문 줄과 같은 ``이름 (키)``, 그 밖(main·하니스)은 소유자 그대로."""
        tm = self._agents.get(c.asker)
        if tm is not None:
            return f"{agent_display_name(tm.key, tm.profile_name, tm.instance_name)} ({tm.key})"
        return c.asker

    def _notify_confirms(self) -> None:
        """열린 확인 목록을 표면에 — 웹 트레이의 출처 하나(sticky), 터미널은 아무것도 안 한다. 표시용 — 실행 경로를
        막지 않는다."""
        try:
            render_surface.get_renderer().open_confirms(
                [
                    {
                        "id": c.id,
                        "asker": c.asker,
                        "label": self.confirm_label(c),
                        "title": c.title,
                        "detail": c.detail,
                        "command": c.command,
                        "danger_spans": [[s, e] for s, e in c.danger_spans],
                        "choices": [
                            {"action": ch.action, "label": ch.label} for ch in c.choices
                        ],
                        **c.provenance,
                        "ts": c.asked_at,
                    }
                    for c in self.open_human_confirms()
                ]
            )
        except Exception:
            pass
