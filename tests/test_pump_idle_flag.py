"""펌프의 한가함은 빈손으로 돌아와도 꺼지지 않는다 (v10.145.0).

한가함이 시간 초과마다 꺼지면 그 깜박임 창에 온 메일이 main 이 바쁜 것으로 보인다(CLI 대기 줄 판정 ``busy``, 오라클
s17 이 컨테이너 cpus 0.5 에서 흔들린 원인). 한가함은 창구가 들고(`MainInput.idle`), 런을 열 것을 꺼낼 때만 내린다.
정지 판정 자리에서 대본을 한 스레드로 돈다 — 시간을 재지 않는다(시간 초과는 0).
"""

from __future__ import annotations

from types import SimpleNamespace

from agent_cli.main_input import MainInput
from agent_cli.post import envelope as post_envelope
from tests.pump_support import make_pump


def _policy(mi: MainInput, seen: list, script: list):
    """바퀴마다 한가함을 적고, 대본의 다음 일을 한다(없으면 닫는다)."""

    class Policy:
        poll_secs = 0

        def should_stop(self):
            seen.append(("loop top", mi.idle()))
            (script.pop(0) if script else mi.close)()
            return False

    return Policy()


def test_idle_stays_on_through_an_empty_poll_and_goes_off_for_an_item():
    mi = MainInput()
    seen: list[tuple[str, bool]] = []

    def run_main(req):
        seen.append(("run", mi.idle()))
        return SimpleNamespace(output="ok", success=True, waiting=False)

    script = [lambda: None, lambda: mi.enqueue(None, "hello")]
    make_pump(main_input=mi, run_main=run_main, policy=_policy(mi, seen, script)).run()
    assert seen == [
        ("loop top", False),  # 첫 바퀴 — 아직 기다림에 들기 전
        ("loop top", True),  # 빈손으로 돌아온 뒤 — 꺼지지 않았다
        ("run", False),  # 항목을 꺼내면 꺼진다
        ("loop top", False),  # 런 뒤 다음 바퀴 — 다시 기다리기 전
    ]


def test_a_mail_that_does_not_wake_leaves_idle_on():
    """깨우지 않는 메일("Stop everything" 의 정지 회신)은 런을 열지 않는다 — 한가함도 꺼지지 않는다."""
    mi = MainInput()
    seen: list[tuple[str, bool]] = []

    def quiet_reply():
        mi.post.deliver(
            post_envelope.reply("n-1", "agent:a", "main", "x", re="r-1", wakes=False)
        )

    def run_main(req):  # pragma: no cover — 열리면 안 된다
        seen.append(("run", mi.idle()))

    make_pump(
        main_input=mi, run_main=run_main, policy=_policy(mi, seen, [quiet_reply])
    ).run()
    assert seen == [("loop top", False), ("loop top", True)]
