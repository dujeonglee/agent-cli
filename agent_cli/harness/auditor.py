"""`SessionAuditor` — main 의 사용자 런마다 감사 작업 하나 (v10.42.0, 4판).

하니스가 하는 일은 둘뿐이다: 런의 history 구간(``seq a..b``)을 재고, 그 구간을
적은 요청으로 하니스 작업 ``auditor`` 를 돌린다(`HarnessJobs.run`). 판정은
모델의 일이고(프로파일 `agents/harness/auditor.md`), 결과 전달은 작업 실행기의
일이다(`complete` 결과 → main 메일함 → `MailWaker` 가 main 을 깨움). 감사
회신으로 깨어난 런(``req.wake``)은 감사하지 않는다 — 고리 상한은 코드다.
"""

from __future__ import annotations

from agent_cli.constants import AUDIT_REQUEST, AUDITOR_NAME, auditor_enabled
from agent_cli.harness.jobs import HarnessJobs


class SessionAuditor:
    """세션당 하나. ``enabled`` 가 노브다 — 부팅값은 `AGENT_CLI_AUDITOR`, web 의
    🔎 칩이 세션 도중 바꾼다(`/api/auditor`). 꺼져 있어도 커서는 같이 움직여
    다시 켰을 때 그 사이 런이 다음 구간에 섞이지 않고, 진행 중이던 감사는
    끝까지 돈다(결과는 배달된다). ``jobs`` 가 None(세션 없음)이면 비활성."""

    def __init__(self, jobs: HarnessJobs | None, ctx, *, enabled: bool) -> None:
        self.jobs = jobs
        self.ctx = ctx
        self.enabled = bool(enabled)
        self._cursor: int = ctx.next_ordinal

    @property
    def active(self) -> bool:
        """지금 감사를 띄울 수 있는가 — 켜져 있고 실행기가 살아 있다."""
        return self.enabled and self.jobs is not None and not self.jobs.stopped

    @property
    def in_flight(self) -> int:
        return self.jobs.in_flight if self.jobs is not None else 0

    def state(self) -> dict:
        """🔎 칩·`/api/auditor`·sticky 가 같이 읽는 한 모양."""
        return {
            "enabled": self.enabled,
            "available": self.jobs is not None,
            "in_flight": self.in_flight,
        }

    def run_ended(self, req, *, ok: bool) -> str:
        """main 의 런이 끝났다. 사용자 런이고 `complete` 로 끝났으면 그 런의
        구간을 감사 작업으로 돌린다. 커서는 **모든** 런이 앞당긴다 — 실패한
        런·깨우기 런의 레코드가 다음 감사 구간에 섞이지 않게.

        돌려주는 값: 띄운 요청 본문(빈 문자열 = 안 띄웠다)."""
        a, b = self._cursor + 1, self.ctx.next_ordinal  # seq 는 1 기준 줄 번호
        self._cursor = b
        if not self.active or b < a:
            return ""
        if not ok or req.wake or not req.author_is_user:
            return ""
        text = AUDIT_REQUEST.format(
            a=a,
            b=b,
            session=self.ctx.session_dir.name,
            history=str(self.ctx.history_path),
        )
        error = self.jobs.run(AUDITOR_NAME, f"{a}-{b}", text)
        return "" if error else text

    def stop(self) -> None:
        if self.jobs is not None:
            self.jobs.stop()


def build_auditor(registry, *, parent_ctx, on_change=None) -> SessionAuditor | None:
    """세션의 감사자 핸들 — 세션이 없으면(``parent_ctx`` None) 감사할 history 가
    없으니 None. 실행기는 늘 조립하고 노브(`AGENT_CLI_AUDITOR`)는 ``enabled``
    초기값일 뿐이라 세션 도중 켜고 끌 수 있다. 결과는
    ``registry.deliver("main", …)`` 로 main 메일함에 — 모니터 보고와 같은 길이다."""
    if parent_ctx is None:
        return None

    def deliver(job: str, success: bool, output: str) -> str:
        return registry.deliver(
            "main",
            mail={"kind": "harness", "job": job, "success": success, "output": output},
            text=output,
            author=job,
            expects_reply=False,
        )

    jobs = HarnessJobs(
        session_ctx=parent_ctx,
        runtime=registry.runtime,
        deliver=deliver,
        on_change=on_change,
    )
    return SessionAuditor(jobs, parent_ctx, enabled=auditor_enabled())
