"""`wait` 와 런의 경계 (5c-1) — 실제 조립(오라클 `Session`, web), 모델 없이.

5c-1 은 런의 경계만 바꾼다: `wait` 는 런을 끝내지 않고 깨울 것이 오면 같은 런이 잇는다. 요청·회신의 짝과 꼬리 Open Items
는 글자까지 같아야 한다 — 깰 때마다의 Open Items 는 `tests/fixtures/wait_keeps_the_run.json` 이 고정한다(에이전트 키는
``agt-*`` 로). 대본의 동기 지점은 제품이 판정에 쓰는 값이다: 기다림 표시(`PostOffice.is_waiting`), 호출 수, 로스터의 상태.

``[5c-1 에서 바뀜]`` 표지가 붙은 단정은 그 커밋에서 뒤집힌다.
"""

from __future__ import annotations

import json
import re
import threading
from pathlib import Path

import pytest

from agent_cli.render import get_renderer, set_renderer
from tests.oracle.driver import (
    Provider,
    Session,
    _agent_key,
    call,
    complete_all,
    owed_id,
    owed_ids,
    runs_ended,
    until,
)

FIXTURE = Path(__file__).parent / "fixtures" / "wait_keeps_the_run.json"
_AGT = re.compile(r"agt-[0-9a-f]{8}")


@pytest.fixture(autouse=True)
def _keep_renderer():
    before = get_renderer()
    yield
    set_renderer(before)


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("AGENT_CLI_SNAPSHOT", "0")
    return tmp_path


def pinned(name: str):
    return json.loads(FIXTURE.read_text(encoding="utf-8"))[name]


def open_items(text: str) -> str:
    """마지막 메시지 꼬리의 ``## Open Items`` 덩어리 — 에이전트 키는 ``agt-*``."""
    i = text.rfind("## Open Items — ")
    if i < 0:
        return ""
    rest = text[i:]
    j = rest.find("\n## ", 5)
    return _AGT.sub("agt-*", (rest if j < 0 else rest[:j]).rstrip())


def message_head(text: str) -> str:
    """하니스가 붙이는 꼬리 앞 — 모델이 읽는 그 메시지의 본문."""
    return _AGT.sub("agt-*", text.split("── end of message", 1)[0]).rstrip()


def is_waiting(s, addr: str = "main") -> bool:
    return s.main_input.post.is_waiting(addr)


def monitor_notice(s, n: int) -> None:
    assert (
        s.reg.deliver(
            "main",
            mail={
                "kind": "monitor",
                "success": True,
                "output": f"🔔 monitor m-1 fired ({n}): tick {n}",
            },
            text="",
            author="",
            expects_reply=False,
        )
        == ""
    )


def history(s, owner: str = "main") -> list[dict]:
    path = (
        s.sdir / "history.jsonl"
        if owner == "main"
        else next(p for p in s.sdir.rglob("history.jsonl") if owner in str(p))
    )
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def work_cards(s, key: str) -> list[str]:
    """그 에이전트의 런 카드(화면의 블록) — 연 항목 id 들."""
    return [
        e[2].get("item")
        for e in s.recorder.lanes.get(f"agent-{key}", [])
        if e[0] == "begin_agent_work"
    ]


def replies_main_read(s) -> list[str]:
    return [
        _AGT.sub("agt-*", str(r.get("content")))
        for r in history(s)
        if "── reply" in str(r.get("content") or "")
    ]


# ── A. main 이 작업을 기다리는 동안 모니터 통지가 세 번 온다 (r1) ─────────────────


def _main_watches_three_ticks(home):
    tails: list[str] = []

    def tail_then(step):
        def go(m):
            tails.append(open_items(m[-1]["content"]))
            return step(m) if callable(step) else step

        return go

    p = Provider(
        {
            "MAIN": [
                call("shell", command="sleep 30", background=True),
                call("wait"),
                tail_then(call("wait")),
                tail_then(call("wait")),
                tail_then(complete_all("all ticks read")),
            ]
        }
    )
    s = Session(home, p, path="web")
    s.say("MAIN watch the ticks", nickname="dj")
    pump = s.pump_in_thread()
    for n in (1, 2, 3):
        until(lambda n=n: is_waiting(s) and len(p.calls["MAIN"]) == 1 + n, f"waits {n}")
        monitor_notice(s, n)
    until(lambda: len(p.calls["MAIN"]) == 5 and s.main_idle(), "main done")
    return s, p, pump, tails


def test_main_waiting_on_a_job_reads_each_notice_with_the_same_open_items(home):
    s, p, pump, tails = _main_watches_three_ticks(home)
    try:
        assert tails == pinned(
            "main_three_ticks"
        )  # 깰 때마다의 Open Items — 글자까지 그대로
        assert message_head(p.calls["MAIN"][-1]["last"]) == (
            "[monitor]\n🔔 monitor m-1 fired (3): tick 3\n\n"
            "(If nothing remains to do, call `complete`.)"
        )
        # [5c-1 에서 바뀜] `wait` 는 런을 끝내지 않는다 — 통지 셋을 한 런이 읽는다(종전: 통지마다 새 런, 넷).
        # 수락 문장에서 "Open items stay open for the next run: …" 이 빠졌다(다음 런이 없다)
        assert runs_ended(s) == 1
        accepted = (
            "Observation: waiting — will wake on: job #1 (`sleep 30`), or in 5m if nothing has "
            "reported."
        )
        assert [r["content"] for r in history(s) if r.get("tool") == "wait"] == [
            accepted
        ] * 3
    finally:
        s.close()
        pump.join(10)


# ── B. 상주 aa 가 bb 에게 묻고 기다린다 (r2) ──────────────────────────────────


def _aa_asks_bb(home, *, bb_answers: threading.Event | None = None):
    """main → aa(TASK-A: bb 에게 묻고 wait) → bb 가 답한다(``bb_answers`` 가 서면). aa 는 받은 답으로 main 에게 회신."""
    box: dict = {}

    def bb_turn(m):
        s = box["s"]
        aa = _agent_key(s, "aa")
        until(lambda: is_waiting(s, f"agent:{aa}"), "aa waits")
        if bb_answers is not None:
            until(bb_answers.is_set, "let bb answer")
        return call("reply", id=owed_id(m), text="42")

    p = Provider(
        {
            "MAIN": [
                call("agent", mode="spawn", name="aa"),
                call("agent", mode="spawn", name="bb"),
                call(
                    "message", to="aa", text="TASK-A ask bb what 6*7 is, then tell me"
                ),
                call("wait"),
            ]
            + [complete_all("main done")] * 6,
            "TASK-A": [
                lambda m: call("message", to="bb", text="TASK-B compute 6*7"),
                call("wait"),
                lambda m: call("reply", id=owed_id(m), text="bb says 42"),
                lambda m: call("complete", result="told main", answers=[]),
            ]
            + [complete_all("aa more")] * 4,
            "TASK-B": [bb_turn] + [complete_all("bb more")] * 4,
        }
    )
    s = box["s"] = Session(home, p, path="web")
    s.say("MAIN go", nickname="dj")
    pump = s.pump_in_thread()
    return s, p, pump


def _settled(s) -> bool:
    return s.main_idle() and all(
        r["state"] in ("idle", "dead") for r in s.reg.roster_snapshot()
    )


def test_a_resident_waiting_on_a_peer_reads_its_reply(home):
    s, p, pump = _aa_asks_bb(home)
    try:
        until(lambda: len(p.calls.get("TASK-A", [])) >= 4 and _settled(s), "settled")
        aa = _agent_key(s, "aa")
        woke = p.calls["TASK-A"][2]["last"]
        assert open_items(woke) == pinned("aa_woken_by_bb")  # 그대로
        assert replies_main_read(s) == [
            "── reply [n-5] from agt-* (aa) re [r-2] ──\nbb says 42"
        ]  # 그대로 — 무엇에 답하는지는 번호가 정한다
        # [5c-1 에서 바뀜] aa 의 `wait` 는 런을 끝내지 않는다 — bb 의 회신은 같은 런의 관찰이다(종전: 새 런 n-4 의 첫 글)
        assert work_cards(s, aa) == ["r-2"]
        assert message_head(woke) == (
            "[agent]\n── reply [n-4] from agt-* (bb) re [r-3] ──\n42\n\n"
            "(If nothing remains to do, call `complete`.)"
        )
    finally:
        s.close()
        pump.join(10)


def test_stop_and_stop_all_on_a_resident_that_waits(home):
    """■ 와 Stop all — 기다리는 상주는 런 안이다(일하는 중). ■ 가 그 런을 멈추고 main 에게 정지 회신이 간다."""
    release = threading.Event()
    s, _p, pump = _aa_asks_bb(home, bb_answers=release)
    try:
        until(lambda: len(s.agent_keys()) == 2, "two agents")
        aa = _agent_key(s, "aa")
        # [5c-1 에서 바뀜] 기다리는 aa 는 busy 다 — Stop all 이 그를 세고 ■ 가 받는다(종전: 런이 끝나 idle, ■ 는
        # 'is not working on a request' 로 거절, Stop all 은 그를 세지 않았다)
        until(lambda: is_waiting(s, f"agent:{aa}"), "aa waits")
        assert s.agent_state(aa) == "busy"
        assert aa in s.reg.working_agents()
        assert s.reg.stop_run(aa) == ""
        until(
            lambda: any("stopped by user" in r for r in replies_main_read(s)),
            "main read the stop reply",
        )
        (stop_reply,) = [r for r in replies_main_read(s) if "stopped by user" in r]
        assert "re [r-2] · stopped by user" in stop_reply
    finally:
        release.set()
        s.close()
        pump.join(10)


# ── E. 기다리는 aa 에게 둘째 요청 (r6) ──────────────────────────────────────


def test_a_second_request_to_a_waiting_agent_is_answered_by_its_number(home):
    box: dict = {}

    def aa_key():
        return _agent_key(box["s"], "aa")

    def main_sends_c(m):
        until(lambda: is_waiting(box["s"], f"agent:{aa_key()}"), "aa waits on bb")
        return call("message", to="aa", text="TASK-C also list three primes")

    def bb_replies(m):
        until(
            lambda: (
                len(box["s"].provider.calls.get("TASK-A", [])) >= 3
                and is_waiting(box["s"], f"agent:{aa_key()}")
            ),
            "aa read TASK-C and waits again",
        )
        return call("reply", id=owed_id(m), text="42")

    p = Provider(
        {
            "MAIN": [
                call("agent", mode="spawn", name="aa"),
                call("agent", mode="spawn", name="bb"),
                call(
                    "message", to="aa", text="TASK-A ask bb what 6*7 is, then tell me"
                ),
                main_sends_c,
                call("wait"),
            ]
            + [
                lambda m: (
                    call("wait")
                    if box["s"].reg.awaited_replies("main")
                    else complete_all("main done")(m)
                )
            ]
            * 8,
            "TASK-A": [
                lambda m: call("message", to="bb", text="TASK-B compute 6*7"),
                call("wait"),
                call("wait"),
                lambda m: call("reply", id=owed_ids(m)[0], text="answer one"),
                lambda m: call("reply", id=owed_ids(m)[0], text="answer two"),
                lambda m: call("complete", result="aa done", answers=[]),
            ]
            + [complete_all("aa more")] * 4,
            "TASK-B": [bb_replies] + [complete_all("bb more")] * 4,
        }
    )
    s = box["s"] = Session(home, p, path="web")
    s.say("MAIN go", nickname="dj")
    pump = s.pump_in_thread()
    try:
        until(
            lambda: (
                len(p.calls.get("TASK-A", [])) >= 6
                and _settled(s)
                and not s.reg.awaited_replies("main")
            ),
            "settled",
        )
        assert [open_items(c["last"]) for c in p.calls["TASK-A"]] == pinned(
            "aa_two_requests"
        )  # 그대로
        assert replies_main_read(s) == [
            "── reply [n-6] from agt-* (aa) re [r-2] ──\nanswer one",
            "── reply [n-7] from agt-* (aa) re [r-4] ──\nanswer two",
        ]  # 그대로
        # [5c-1 에서 바뀜] 런 카드 하나 — 둘째 요청과 bb 의 회신이 같은 런에 든다(종전: r-2·r-4·n-5 셋)
        assert work_cards(s, aa_key()) == ["r-2"]
    finally:
        s.close()
        pump.join(10)


# ── C·D. 깰 때마다 새로 세는 것 (r3·r4) ──────────────────────────────────────


def _main_looks_then_waits(home, *, max_turns: int, wakes: int):
    """main: 작업을 띄우고 기다린다 → 통지마다 같은 호출(`shell` "true") 하나를 하고 다시 기다린다 → 마지막 통지 뒤에는
    끝낸다. 호출은 모두 ``2 + 2·wakes``."""
    script = [call("shell", command="sleep 30", background=True), call("wait")]
    for n in range(1, wakes + 1):
        script += [
            call("shell", command="true"),
            call("wait") if n < wakes else complete_all("done"),
        ]
    p = Provider({"MAIN": script})
    s = Session(home, p, path="web")
    s.wired.pump.deps.main.max_turns = max_turns
    s.say("MAIN watch", nickname="dj")
    pump = s.pump_in_thread()
    for n in range(1, wakes + 1):
        until(lambda n=n: is_waiting(s) and len(p.calls["MAIN"]) == 2 * n, f"waits {n}")
        monitor_notice(s, n)
    until(lambda: len(p.calls["MAIN"]) == 2 + 2 * wakes and s.main_idle(), "done")
    return s, pump


def test_the_turn_limit_counts_from_each_wake(home):
    """턴 상한 4 — 깰 때마다 두 턴(살피기·다시 기다림)을 쓰며 세 번 깬다. 어느 깸에서도 상한에 닿지 않는다."""
    s, pump = _main_looks_then_waits(home, max_turns=4, wakes=3)
    try:
        ends = [r for r in s.recorder.lanes.get("pump", []) if r[0] == "run_ended"]
        assert not any("Max turns" in str(r) for r in ends)
        assert ends[-1][2] == "done"
    finally:
        s.close()
        pump.join(10)


def test_the_same_call_on_each_wake_is_not_a_loop(home):
    """같은 호출이 `wait` 를 사이에 두고 되풀이된다 — 반복 감지기는 발화하지 않는다."""
    s, pump = _main_looks_then_waits(home, max_turns=0, wakes=3)
    try:
        signals = [
            json.loads(line).get("failure_signal")
            for line in (s.sdir / "turns.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
            if line.strip()
        ]
        assert "ACTION_LOOP" not in signals
    finally:
        s.close()
        pump.join(10)


# ── F. 기다리는 동안 남이 바꾼 파일 — 되돌릴 지점 (r9) ─────────────────────────


def test_the_point_before_the_wait_can_be_restored(tmp_path, monkeypatch):
    """main 이 a.txt 를 쓰고(v1), 작업이 그것을 바꾸는 동안 기다리고, 깨어 b.txt 를 쓰고 끝낸다. 되돌림 목록에 "a.txt =
    v1" 지점이 있어야 한다 — 내가 고친 뒤, 기다리는 동안 남이 바꾸기 전."""
    from agent_cli.runtime import build_snapshot_store
    from agent_cli.snapshot import get_snapshot

    ws, home = tmp_path / "ws", tmp_path / "home"
    ws.mkdir()
    home.mkdir()
    monkeypatch.chdir(ws)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("AGENT_CLI_SNAPSHOT_MIN_INTERVAL_S", "0")
    build_snapshot_store("wait-point")
    job = "while [ ! -f go ]; do sleep 0.05; done; echo 'line by job' >> a.txt"
    p = Provider(
        {
            "MAIN": [
                call("shell", command=job, background=True),
                call("write_file", path="a.txt", content="v1 by main\n"),
                call("wait"),
                call("write_file", path="b.txt", content="b by main\n"),
                complete_all("done"),
            ]
        }
    )
    s = Session(tmp_path / "sess", p, path="web")
    s.say("MAIN edit then wait for the job", nickname="dj")
    pump = s.pump_in_thread()
    try:
        until(lambda: is_waiting(s) and len(p.calls["MAIN"]) == 3, "main waits")
        # 기다리는 동안 작업이 a.txt 를 바꾼다
        (ws / "go").write_text("", encoding="utf-8")
        until(lambda: len(p.calls["MAIN"]) == 5 and s.main_idle(), "main done")
        store = get_snapshot(Path.cwd())
        points = {
            label: store._git("show", f"{c}:a.txt").stdout
            for c, label in store.log(limit=30)
        }
        # [5c-1 에서 바뀜] `wait` 가 런을 끝내지 않으니 런 끝 체크포인트가 그 자리에 없다 — "a.txt = v1" 지점이 사라졌다
        # (r9). 다음 커밋의 `PreWait` 체크포인트가 되살린다
        assert "v1 by main\n" not in points.values()
    finally:
        s.close()
        pump.join(10)


# ── 구간 — 제자리 `wait` 에서 깰 때마다 새로 세는 것 (main 이 회신을 기다리는 길 — 지금도 제자리) ──────────


def _main_asks_aa_twice(home, *, max_turns: int, between):
    """main: aa 를 띄우고 요청 → 제자리에서 기다림 → 회신에 깨어 ``between`` 한 번 → 둘째 요청 → 기다림 → 끝.
    돌려주는 것 ``(s, p, pump)`` — 끝까지 기다린 뒤."""
    p = Provider(
        {
            "MAIN": [
                call("agent", mode="spawn", name="aa"),
                call("message", to="aa", text="TASK-Q1 first"),
                between,
                call("wait"),
                between,
                call("message", to="aa", text="TASK-Q2 second"),
                call("wait"),
                complete_all("main done"),
            ],
            "TASK-Q1": [lambda m: call("reply", id=owed_id(m), text="one")],
            "TASK-Q2": [lambda m: call("reply", id=owed_id(m), text="two")],
        }
    )
    s = Session(home, p, path="web")
    s.wired.pump.deps.main.max_turns = max_turns
    s.say("MAIN ask twice", nickname="dj")
    pump = s.pump_in_thread()
    until(lambda: len(p.calls["MAIN"]) == 8 and _settled(s), "main done")
    return s, p, pump


def test_the_turn_limit_counts_each_segment_of_an_in_place_wait(home):
    """턴 상한 4 — 런 전체는 여덟 턴이지만 깨어 있는 토막마다 넷 이하다. 상한에 닿지 않고 끝난다."""
    s, p, pump = _main_asks_aa_twice(
        home, max_turns=4, between=call("shell", command="true")
    )
    try:
        ends = [r for r in s.recorder.lanes.get("pump", []) if r[0] == "run_ended"]
        assert [r[2] for r in ends] == ["main done"]
        # 꼬리의 턴 줄도 토막의 턴이다 — 깬 뒤 첫 호출은 "turn 1/4"
        tails = [str(c["last"]) for c in p.calls["MAIN"]]
        assert "turn 4/4 (0 left after this one)" in tails[3]  # 첫 토막의 넷째(wait)
        assert "turn 1/4 (3 left after this one)" in tails[4]  # 깬 뒤 첫 호출
    finally:
        s.close()
        pump.join(10)


def test_begin_segment_resets_what_counts_per_segment():
    from unittest.mock import MagicMock

    from agent_cli.loop.ledger import NAG_BUDGET
    from agent_cli.render import set_renderer
    from tests.loop_ports import TEST_PORTS
    from tests.loopkit import make_loop

    loop = make_loop(query="Q", provider=MagicMock(), ports=TEST_PORTS)
    d = loop._dispatch
    d.state.turn = 7
    d.state.ledger.spend()
    old_loop, old_stale = d.loop_detector, d.stale_guard
    renderer = MagicMock()
    set_renderer(renderer)
    d._stale_shown = True  # 🔁 표시가 떠 있었다
    d._begin_segment()
    assert d.state.segment_turn0 == 7
    assert d.state.ledger.budget == NAG_BUDGET
    assert d.loop_detector is not old_loop and d.stale_guard is not old_stale
    renderer.stale_loop.assert_called_once()  # 🔁 표시를 지웠다


# ── run 에이전트(일회용) — 기다린 뒤에도 같은 런, `complete` 에서 내려온다 (§2.4) ─────────────


def _provider_fails(_messages):
    raise RuntimeError("oracle: provider down")


def test_a_run_agent_that_waited_comes_down_when_its_run_fails(home):
    """§2.4: run 에이전트는 런이 어떻게 끝나든 내려온다 — 기다린 뒤 깨어 그 런이 오류로 끝나도(`wait` 를 낀 런에 내려옴의
    예외가 없다). main 은 그 런의 회신을 받는다."""
    job = "while [ ! -f go ]; do sleep 0.05; done; echo done"
    p = Provider(
        {
            "MAIN": [call("agent", mode="run", task="TASK-R build it"), call("wait")]
            + [complete_all("main done")] * 3,
            "TASK-R": [
                call("shell", command=job, background=True),
                call("wait"),
                _provider_fails,
            ],
        }
    )
    s = Session(home, p, path="web")
    s.say("MAIN go", nickname="dj")
    pump = s.pump_in_thread()
    try:
        until(lambda: len(s.agent_keys()) == 1, "kid up")
        kid = s.agent_keys()[0]
        until(lambda: is_waiting(s, f"agent:{kid}"), "kid waits")
        (home / "go").write_text("", encoding="utf-8")
        until(lambda: s.agent_state(kid) == "dead" and _settled(s), "kid came down")
        assert work_cards(s, kid) == [s.reg.get(kid).task_id]  # 런 하나
        (read,) = replies_main_read(s)
        assert read.startswith("── reply [n-4] from agt-* re [r-2] ──")
    finally:
        s.close()
        pump.join(10)


def test_a_run_agent_waits_in_its_run_and_comes_down_at_complete(home):
    """run 에이전트가 자기 작업을 띄우고 `wait` — 런은 끝나지 않고(살아 있다, busy), 작업 통지에 깨어 같은 런에서
    `complete` 한다. 그때 내려온다. main 은 그 회신을 받는다."""
    job = "while [ ! -f go ]; do sleep 0.05; done; echo done"
    p = Provider(
        {
            "MAIN": [call("agent", mode="run", task="TASK-R build it"), call("wait")]
            + [complete_all("main done")] * 3,
            "TASK-R": [
                call("shell", command=job, background=True),
                call("wait"),
                lambda m: call("reply", id=owed_id(m), text="built"),
                lambda m: call("complete", result="built", answers=[]),
            ],
        }
    )
    s = Session(home, p, path="web")
    s.say("MAIN go", nickname="dj")
    pump = s.pump_in_thread()
    try:
        until(lambda: len(s.agent_keys()) == 1, "kid up")
        kid = s.agent_keys()[0]
        until(lambda: is_waiting(s, f"agent:{kid}"), "kid waits")
        assert s.agent_state(kid) == "busy"  # 기다리는 동안도 런 안 — 내려오지 않았다
        (home / "go").write_text("", encoding="utf-8")
        until(lambda: s.agent_state(kid) == "dead" and _settled(s), "kid came down")
        assert work_cards(s, kid) == [s.reg.get(kid).task_id]  # 런 하나
        assert replies_main_read(s) == ["── reply [n-4] from agt-* re [r-2] ──\nbuilt"]
    finally:
        s.close()
        pump.join(10)
