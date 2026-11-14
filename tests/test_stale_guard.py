"""제자리 고리 감지기 StaleGuard (v10.120.0, docs/stale-guard/DESIGN.md).

- 규칙(규칙 C, N3W8 — 오프라인 대입 스크립트와 같은 정의): 턴의 모든 (도구, 결과) 쌍이 비어
  있지 않고·쓰기 도구가 아니고·``(no output)`` 이 아니고·최근 8턴 안에 이미 있었으면 제자리.
  3번 연달아면 발화, 발화 뒤 연속 수 0. `wait`·`monitor`·하니스 작업 중·쌍 없음은 리셋.
- 쌍: 턴이 ctx 에 남긴 레코드 → (op 이름, 관찰 본문). 묶음은 op 별 조각. 저장 안 된 시도는 턴이
  아니다(None).
- 개입: 모든 발화 = failures.jsonl ``stale_loop`` + 렌더러 ``stale_loop(info)``. 웹 main 은 두
  번째 발화부터 기존 confirm 으로 멈추고 묻는다(c+코멘트 = Hint 가 입력 큐로, s = Stop).
  run(Minimal)은 묻지 않는다. 모델 입력은 Hint 의 사용자 메시지 외에 그대로다.
"""

from __future__ import annotations

import io
import json
import re
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient
from rich.console import Console

from agent_cli import render
from agent_cli.context.manager import ContextManager
from agent_cli.input_queue import InputQueue
from agent_cli.instance_file import read_status_file
from agent_cli.providers.base import LLMResponse
from agent_cli.providers.capabilities import ModelCapabilities
from agent_cli.recovery.detectors import (
    STALE_RUN,
    STALE_WINDOW,
    StaleGuard,
    stale_turn_pairs,
)
from agent_cli.render.base import interactive_lock
from agent_cli.render.minimal import MinimalRenderer
from agent_cli.render.web import WebRenderer
from agent_cli.render.web_hub import WebConnection
from agent_cli.tools.result import ToolResult
from agent_cli.web.server import WebServer, create_app
from tests.loop_ports import TEST_PORTS, make_ports
from tests.loopkit import run_loop


def _feed(guard: StaleGuard, turns, *, work=None) -> list[int]:
    """턴 목록을 먹여 발화한 턴 번호(1부터)를 돌려준다."""
    hits = []
    for i, pairs in enumerate(turns, start=1):
        if guard.observe(pairs, turn=i, work_running=bool(work and i in work)):
            hits.append(i)
    return hits


# ── 규칙 ─────────────────────────────────────────────────


class TestRule:
    def test_constants_are_module_constants(self):
        assert (STALE_WINDOW, STALE_RUN) == (8, 3)

    def test_abab_loop_fires_on_the_third_stale_turn(self):
        """A·B 가 번갈아도 결과가 창 안에 있으면 제자리 — 첫 A, 첫 B 는 새것."""
        a, b = [("shell", "17")], [("read_file", "x")]
        assert _feed(StaleGuard(), [a, b, a, b, a]) == [5]

    def test_same_result_with_different_call_text_counts(self):
        """호출 글자는 보지 않는다 — `ls` 와 `ls 2>/dev/null` 이 같은 17 이면 같다."""
        g = StaleGuard()
        assert _feed(g, [[("shell", "17")]] * 4) == [4]

    def test_a_new_result_resets(self):
        turns = [[("shell", "17")]] * 3 + [[("shell", "18")]] + [[("shell", "17")]] * 2
        g = StaleGuard()
        assert _feed(g, turns) == []
        assert g.turns_without_new == 2  # 18 뒤로 다시 셌다

    def test_one_new_pair_in_a_batch_makes_the_turn_new(self):
        turns = [[("shell", "17")]] * 3 + [[("shell", "17"), ("read_file", "new")]]
        assert _feed(StaleGuard(), turns) == []

    @pytest.mark.parametrize(
        "pair",
        [
            ("write_file", "File saved"),
            ("edit_file", "ok"),
            ("snapshot", "restored"),
            ("memory", "saved"),
            ("shell", ""),
            ("shell", "   \n"),
            ("shell", "(no output)"),
        ],
    )
    def test_writes_empty_and_no_output_are_never_stale(self, pair):
        turns = [[pair]] * 6
        g = StaleGuard()
        assert _feed(g, turns) == [] and g.turns_without_new == 0

    def test_a_write_in_the_turn_breaks_it(self):
        turns = [[("shell", "17")]] * 3 + [[("shell", "17"), ("write_file", "ok")]]
        assert _feed(StaleGuard(), turns) == []

    @pytest.mark.parametrize("tool", ["wait", "monitor"])
    def test_wait_and_monitor_reset(self, tool):
        turns = [[("shell", "17")]] * 3 + [[(tool, "17")], [("shell", "17")]]
        g = StaleGuard()
        assert _feed(g, turns) == []
        assert g.turns_without_new == 1

    def test_running_harness_work_resets(self):
        """백그라운드 작업이 도는 동안 같은 로그를 다시 보는 것은 고리가 아니다(폴링)."""
        turns = [[("shell", "tail")]] * 6
        g = StaleGuard()
        assert _feed(g, turns, work={2, 3, 4, 5, 6}) == []
        assert _feed(StaleGuard(), turns, work={3}) == [6]

    def test_turn_without_pairs_resets(self):
        turns = [[("shell", "17")]] * 3 + [[]] + [[("shell", "17")]] * 2
        assert _feed(StaleGuard(), turns) == []

    def test_pair_pushed_out_of_the_window_is_new_again(self):
        """창은 직전 8턴 — 9턴 전에 본 결과는 새것이다."""
        filler = [[("shell", f"f{i}")] for i in range(STALE_WINDOW)]
        turns = [[("shell", "17")]] + filler + [[("shell", "17")]] * 3
        g = StaleGuard()
        assert _feed(g, turns) == [] and g.turns_without_new == 2

    def test_after_a_fire_the_run_restarts_and_fires_count(self):
        turns = [[("shell", "17")]] * 10
        g = StaleGuard()
        assert _feed(g, turns) == [4, 7, 10]
        assert g.fires == 3 and g.run == 0
        assert g.turns_without_new == 9 and g.since_turn == 2

    def test_acknowledge_resets_run_and_sets_fires_to_one(self):
        g = StaleGuard()
        _feed(g, [[("shell", "17")]] * 7)
        assert g.fires == 2
        g.acknowledge()
        assert g.fires == 1 and g.run == 0
        assert g.observe([("shell", "17")], turn=8) is False

    def test_since_turn_is_zero_without_a_streak(self):
        assert StaleGuard().since_turn == 0


# ── 쌍: 턴의 레코드 → (op, 본문) ───────────────────────────


def _asst(*actions):
    return {
        "role": "assistant",
        "ops": [{"action": a, "action_input": {}} for a in actions],
    }


class TestTurnPairs:
    def test_single_op_strips_the_observation_prefix(self):
        recs = [
            _asst("shell"),
            {"role": "user", "tool": "shell", "content": "Observation: 17"},
        ]
        assert stale_turn_pairs(recs) == [("shell", "17")]

    def test_batch_uses_parts(self):
        recs = [
            _asst("shell", "read_file"),
            {
                "role": "user",
                "tool": "shell+read_file",
                "content": "Observation: [1/2] …",
                "parts": [
                    {"tool": "shell", "content": "17"},
                    {"tool": "read_file", "content": "body"},
                ],
            },
        ]
        assert stale_turn_pairs(recs) == [("shell", "17"), ("read_file", "body")]

    def test_complete_is_not_a_call_and_missing_results_are_empty(self):
        recs = [_asst("shell", "complete")]
        assert stale_turn_pairs(recs) == [("shell", "")]

    def test_interventions_are_not_results(self):
        recs = [
            _asst("shell"),
            {"role": "user", "tool": "shell", "recovery": "loop", "content": "nudge"},
            {"role": "user", "tool": "", "content": "format"},
        ]
        assert stale_turn_pairs(recs) == [("shell", "")]

    def test_unstored_attempt_is_not_a_turn(self):
        """형식 재시도·B1 이 막은 반복은 assistant 레코드를 남기지 않는다 — 턴이 아니다."""
        assert (
            stale_turn_pairs([{"role": "user", "tool": "shell", "recovery": "loop"}])
            is None
        )
        assert stale_turn_pairs([]) is None

    def test_records_since_returns_this_turns_slice(self, tmp_path):
        ctx = ContextManager(Path(tmp_path) / "s", max_context_tokens=30_000)
        ctx.add({"role": "user", "content": "q"})
        start = ctx.next_ordinal
        ctx.add(_asst("shell"))
        ctx.add({"role": "user", "tool": "shell", "content": "Observation: 17"})
        got = ctx.records_since(start)
        assert [r["role"] for r in got] == ["assistant", "user"]
        assert ctx.records_since(ctx.next_ordinal) == []


class TestHarnessWork:
    def test_running_job_or_live_monitor_of_the_owner(self, monkeypatch):
        from agent_cli.session_slots import set_job_registry, set_monitor_registry
        from agent_cli.wake import harness_work_running

        running = {"main": [object()]}
        set_job_registry(MagicMock(running=lambda owner: running.get(owner, [])))
        set_monitor_registry(None)
        assert harness_work_running("main") is True
        assert harness_work_running("agent:k") is False
        m = MagicMock(owner="agent:k")
        set_monitor_registry(MagicMock(list_all=lambda: [m]))
        assert harness_work_running("agent:k") is True


# ── 렌더러 ─────────────────────────────────────────────────


def _info(n, *, since=2, fires=1, asking=False):
    return {
        "turns_without_new": n,
        "since_turn": since,
        "fires": fires,
        "asking": asking,
    }


def _events(conn) -> list[tuple[str, dict]]:
    out = []
    while not conn.queue.empty():
        out.append(conn.queue.get_nowait())
    return out


class TestWebRenderer:
    def test_sticky_status_and_clear(self, tmp_path):
        r = WebRenderer(session_dir=str(tmp_path))
        conn = WebConnection(id="c1")
        r.hub.register(conn)
        r.stale_loop(_info(3))
        assert r.hub.sticky_payload("stuck") == {
            "turns": 3,
            "since_turn": 2,
            "fires": 1,
            "asking": False,
        }
        assert read_status_file(tmp_path)["stuck"] == {
            "turns": 3,
            "since_turn": 2,
            "asking": False,
        }
        r.stale_loop(_info(3))  # 같은 payload 는 다시 보내지 않는다
        r.stale_loop(_info(0))
        assert r.hub.sticky_payload("stuck") is None
        assert "stuck" not in read_status_file(tmp_path)
        live = [d for ev, d in _events(conn) if ev == "stuck"]
        assert [d["turns"] for d in live] == [3, 0]

    def test_health_carries_the_same_summary(self):
        r = WebRenderer()
        client = TestClient(create_app(WebServer(r, token="t", ctx=None)))
        assert client.get("/api/health").json()["stuck"] is None
        r.stale_loop(_info(4, asking=True))
        assert client.get("/api/health").json()["stuck"] == {
            "turns": 4,
            "since_turn": 2,
            "asking": True,
        }

    def test_nested_loops_do_not_draw_the_main_band(self):
        r = WebRenderer()
        r.begin_prompt_scope("skill-1", "skill")
        try:
            r.stale_loop(_info(3))
        finally:
            r.end_prompt_scope("skill-1")
        assert r.hub.sticky_payload("stuck") is None

    def test_capabilities(self):
        assert WebRenderer().can_pause_for_human() is True
        assert (
            MinimalRenderer(Console(file=io.StringIO())).can_pause_for_human() is False
        )

    def test_confirm_author_is_the_answering_viewer(self):
        r = WebRenderer()
        conn = WebConnection(id="c1")
        r.hub.register(conn)
        r.hub.set_nickname("c1", "dj")
        threading.Timer(
            0.2,
            lambda: r.push_user_input(
                "confirm", {"key": "c", "comment": "hint", "conn_id": "c1"}
            ),
        ).start()
        from agent_cli.render.base import ConfirmOption

        got = r.confirm("q", [ConfirmOption("c", "Continue")], default_key="c")
        assert got == ("c", "hint") and r.confirm_author() == "dj"
        assert r.confirm_author() == ""  # 한 번 읽으면 비운다


class TestMinimalRenderer:
    def test_one_dim_line_per_fire(self):
        buf = io.StringIO()
        r = MinimalRenderer(Console(file=buf, force_terminal=False, width=120))
        r.stale_loop(_info(3, fires=1))
        r.stale_loop(_info(4, fires=1))  # N 갱신은 찍지 않는다
        r.stale_loop(_info(6, fires=2))
        r.stale_loop(_info(0, fires=0))
        r.stale_loop(_info(3, fires=1))  # 지운 뒤 새 구간
        assert buf.getvalue().splitlines() == [
            "  🔁 no new results for 3 turns",
            "  🔁 no new results for 6 turns",
            "  🔁 no new results for 3 turns",
        ]


# ── 통합: 가짜 공급자로 같은 결과를 되풀이 ──────────────────────


@pytest.fixture
def caps():
    return ModelCapabilities(
        context_window=32768, max_output_tokens=4096, supports_thinking=False
    )


@pytest.fixture(autouse=True)
def _fixed_shell(monkeypatch):
    """shell 은 `fresh` 가 든 명령만 새 결과 — 나머지는 언제나 17."""
    from agent_cli.tools import TOOLS

    monkeypatch.setattr(
        TOOLS["shell"],
        "_run",
        lambda args, **kw: ToolResult(
            True, output=args["command"] if "fresh" in args["command"] else "17"
        ),
    )


def _shell(i: int, cmd: str = "") -> LLMResponse:
    # 호출 글자는 매번 다르다 — B1(바로 연달은 같은 호출)에는 안 걸린다
    return LLMResponse(
        content=json.dumps(
            {"action": "shell", "command": cmd or f"ls /app | wc -l #{i}"}
        )
    )


def _complete(result="done", answers=None) -> LLMResponse:
    body = {"action": "complete", "result": result}
    if answers:
        body["answers"] = answers
    return LLMResponse(content=json.dumps(body))


@pytest.fixture
def web(tmp_path):
    r = WebRenderer(session_dir=str(tmp_path / "status"))
    (tmp_path / "status").mkdir()
    conn = WebConnection(id="c1")
    r.hub.register(conn)
    r.hub.set_nickname("c1", "dj")
    prev = render.get_renderer()
    render.set_renderer(r)
    yield r, conn
    render.set_renderer(prev)


def _failures(ctx) -> list[dict]:
    path = Path(ctx.session_dir) / "failures.jsonl"
    if not path.exists():
        return []
    return [json.loads(ln) for ln in path.read_text().splitlines() if ln.strip()]


def _main_ports(q: InputQueue):
    return make_ports(
        dequeue_user_message=q.dequeue_nowait,
        enqueue_user_message=lambda text, nickname: q.enqueue(
            None, text, nickname=nickname
        ),
    )


def _status_once_awaiting(status_dir, deadline: float) -> dict | None:
    """``awaiting_input`` 이 선 status.json — 스티키가 먼저 서고 파일은 그 직후에 쓰이므로
    (``set_sticky`` → ``_publish_status``) 스티키를 본 순간의 파일은 아직 옛 것일 수 있다.
    마감까지 안 서면 마지막으로 읽은 것을 돌려줘 단언이 그 값으로 실패한다."""
    if not status_dir:
        return None
    status = read_status_file(status_dir)
    while not (status or {}).get("awaiting_input") and time.time() < deadline:
        time.sleep(0.02)
        status = read_status_file(status_dir)
    return status


def _answer_when_asked(r: WebRenderer, key: str, comment: str, seen: dict):
    """뷰어 대역 — confirm 이 뜨면 그때의 상태를 적고 답한다."""

    def run():
        deadline = time.time() + 20
        while time.time() < deadline:
            if r.awaiting_input_kind() == "confirm":
                seen["stuck"] = r.hub.sticky_payload("stuck")
                seen["prompt"] = r.hub.sticky_payload("input_required")
                seen["status"] = _status_once_awaiting(r._status_dir, deadline)
                r.push_user_input(
                    "confirm", {"key": key, "comment": comment, "conn_id": "c1"}
                )
                return
            time.sleep(0.02)

    t = threading.Thread(target=run, daemon=True)
    t.start()
    return t


class TestWebMain:
    def test_first_fire_shows_the_band_and_a_new_result_clears_it(
        self, web, caps, tmp_path
    ):
        r, conn = web
        ctx = ContextManager(session_dir=tmp_path / "s")
        provider = MagicMock()
        provider.call.side_effect = [
            _shell(1),
            _shell(2),
            _shell(3),
            _shell(4),  # 발화 1 — 띠
            _shell(5),  # N 갱신
            _shell(6, "echo fresh"),  # 새 결과 — 띠가 내려간다
            _complete(),
        ]
        q = InputQueue()
        result = run_loop(
            ports=_main_ports(q),
            query="count",
            provider=provider,
            capabilities=caps,
            model="m",
            ctx=ctx,
            max_turns=20,
        )
        assert result.success
        stuck = [d for ev, d in _events(conn) if ev == "stuck"]
        assert [d["turns"] for d in stuck] == [3, 4, 0]
        assert {k: stuck[0][k] for k in ("turns", "since_turn", "fires", "asking")} == {
            "turns": 3,
            "since_turn": 2,
            "fires": 1,
            "asking": False,
        }
        assert r.hub.sticky_payload("stuck") is None
        assert r.awaiting_input_kind() is None  # 첫 발화는 묻지 않는다
        recs = _failures(ctx)
        assert [x["failure_signal"] for x in recs] == ["stale_loop"]
        assert recs[0]["turn"] == 4
        assert recs[0]["detail"]["turns_without_new"] == 3
        assert recs[0]["detail"]["fires"] == 1
        assert "→ 17" in recs[0]["text"]

    def test_second_fire_pauses_and_continue_hint_goes_through_the_queue(
        self, web, caps, tmp_path
    ):
        r, _conn = web
        ctx = ContextManager(session_dir=tmp_path / "s")
        calls: list[list[dict]] = []

        def respond(*args, **kwargs):
            msgs = kwargs.get("messages") or args[0]
            calls.append(msgs)
            n = len(calls)
            if n <= 7:
                return _shell(n)
            # Hint 가 흡수된 뒤 — 꼬리의 장부 id 로 답한다
            text = json.dumps(msgs, ensure_ascii=False)
            ids = re.findall(r"\bu-\d+\b", text)
            return _complete(answers=sorted(set(ids)))

        provider = MagicMock()
        provider.call.side_effect = respond
        q = InputQueue()
        seen: dict = {}
        viewer = _answer_when_asked(r, "c", "read the WAL file instead", seen)
        result = run_loop(
            ports=_main_ports(q),
            query="count",
            provider=provider,
            capabilities=caps,
            model="m",
            ctx=ctx,
            max_turns=20,
        )
        viewer.join(5)
        assert result.success, result.error
        # 두 번째 발화(7턴)에서 기존 confirm 으로 멈췄다
        prompt = seen["prompt"]
        assert prompt["kind"] == "confirm" and prompt["default_key"] == "c"
        assert [o["key"] for o in prompt["options"]] == ["c", "s"]
        assert "No new results for 6 turns" in prompt["prompt"]
        assert "→ 17" in prompt["prompt"]
        assert seen["stuck"]["asking"] is True
        assert seen["status"]["awaiting_input"] is True
        assert seen["status"]["stuck"]["asking"] is True
        # Hint = 대답한 뷰어의 메시지로, 다음 턴 경계에 흡수(u-N 장부 항목)
        hist = [
            json.loads(ln)
            for ln in (Path(ctx.session_dir) / "history.jsonl").read_text().splitlines()
        ]
        hints = [h for h in hist if h.get("author") == "dj"]
        assert len(hints) == 1
        assert hints[0]["content"] == "[dj]: read the WAL file instead"
        assert re.fullmatch(r"u-\d+", hints[0]["request_id"])
        # 흡수는 8번째 모델 호출 전 — 그 호출이 Hint 를 본다
        assert "read the WAL file instead" in json.dumps(calls[7], ensure_ascii=False)
        assert "read the WAL file instead" not in json.dumps(
            calls[6], ensure_ascii=False
        )
        assert [x["detail"]["fires"] for x in _failures(ctx)] == [1, 2]
        # StaleGuard 확인의 코멘트는 Hint 로만 — 도구 결과 끝의 사람 말 줄이 아니다
        # (v10.120.2 `human_note` 는 안전 확인 두 곳만 기록한다).
        assert "User note when" not in json.dumps(calls, ensure_ascii=False)
        assert "User note when" not in json.dumps(hist, ensure_ascii=False)

    def test_stop_ends_the_run_with_the_fact(self, web, caps, tmp_path):
        r, _conn = web
        ctx = ContextManager(session_dir=tmp_path / "s")
        provider = MagicMock()
        provider.call.side_effect = [_shell(i) for i in range(1, 8)] + [_complete()]
        seen: dict = {}
        viewer = _answer_when_asked(r, "s", "", seen)
        result = run_loop(
            ports=_main_ports(InputQueue()),
            query="count",
            provider=provider,
            capabilities=caps,
            model="m",
            ctx=ctx,
            max_turns=20,
            graceful_interrupt=True,
        )
        viewer.join(5)
        assert not result.success
        assert "Stopped by the user after 6 turns without new results." in result.error
        assert provider.call.call_count == 7  # 멈춘 뒤 모델을 다시 부르지 않았다
        assert r.hub.sticky_payload("stuck") is None  # 런 끝에 띠가 내려간다
        ends = [e for e in r.hub.events if e[0] == "turn_error"]  # 런 종료 사유 줄
        assert any(
            "Stopped by the user after 6 turns" in str(e[1].get("content", ""))
            for e in ends
        )

    def test_confirm_is_called_without_holding_the_interactive_lock(
        self, caps, tmp_path, monkeypatch
    ):
        """락 순서: 디스패처의 이 자리는 락을 잡지 않은 채 confirm 을 부른다 —
        confirm 이 `interactive_lock` 을 처음 잡는다(ask·위험 셸 확인과 같은 순서)."""
        r = WebRenderer()
        held: list[bool] = []

        def confirm(prompt, options, *, default_key, **kw):
            held.append(interactive_lock._is_owned())
            return ("c", "")

        monkeypatch.setattr(r, "confirm", confirm)
        prev = render.get_renderer()
        render.set_renderer(r)
        try:
            provider = MagicMock()
            provider.call.side_effect = [_shell(i) for i in range(1, 8)] + [_complete()]
            run_loop(
                ports=_main_ports(InputQueue()),
                query="count",
                provider=provider,
                capabilities=caps,
                model="m",
                ctx=ContextManager(session_dir=tmp_path / "s"),
                max_turns=20,
            )
        finally:
            render.set_renderer(prev)
        assert held == [False]


class TestNotAsked:
    def _run(self, caps, tmp_path, *, ports, n=8):
        ctx = ContextManager(session_dir=tmp_path / "s")
        provider = MagicMock()
        provider.call.side_effect = [_shell(i) for i in range(1, n)] + [_complete()]
        result = run_loop(
            ports=ports,
            query="count",
            provider=provider,
            capabilities=caps,
            model="m",
            ctx=ctx,
            max_turns=20,
        )
        return result, ctx

    def test_run_minimal_prints_lines_and_records_but_never_asks(self, caps, tmp_path):
        buf = io.StringIO()
        r = MinimalRenderer(Console(file=buf, force_terminal=False, width=120))
        r.confirm = MagicMock(side_effect=AssertionError("run 은 묻지 않는다"))
        prev = render.get_renderer()
        render.set_renderer(r)
        try:
            result, ctx = self._run(caps, tmp_path, ports=_main_ports(InputQueue()))
        finally:
            render.set_renderer(prev)
        assert result.success
        lines = [ln for ln in buf.getvalue().splitlines() if "🔁" in ln]
        assert lines == [
            "  🔁 no new results for 3 turns",
            "  🔁 no new results for 6 turns",
        ]
        assert [x["failure_signal"] for x in _failures(ctx)] == ["stale_loop"] * 2

    def test_non_main_web_loop_records_but_does_not_ask(self, web, caps, tmp_path):
        r, _conn = web
        r.confirm = MagicMock(side_effect=AssertionError("서브루프는 묻지 않는다"))
        result, ctx = self._run(caps, tmp_path, ports=TEST_PORTS)
        assert result.success
        assert [x["detail"]["fires"] for x in _failures(ctx)] == [1, 2]


class TestWebSource:
    def test_band_module(self):
        from tests.web_static import js_text, module_order

        js = js_text("stuck")
        assert 'es.addEventListener("stuck"' in js
        assert '"🔁 No new results for "' in js
        assert '"api/stop"' in js and 'kind: "confirm", key: "s"' in js
        order = module_order()
        assert order.index("stuck") < order.index("channels")


# ── ⚡ 자동 승인은 안전 확인의 정책 — 멈춤 질문은 언제나 묻는다 ─────────


class TestAutoApproveDoesNotSkipThePause:
    def test_second_fire_still_pauses_with_auto_approve_on(self, web, caps, tmp_path):
        r, _conn = web
        r.set_auto_approve(True)
        ctx = ContextManager(session_dir=tmp_path / "s")
        provider = MagicMock()
        provider.call.side_effect = [_shell(i) for i in range(1, 8)] + [_complete()]
        seen: dict = {}
        viewer = _answer_when_asked(r, "s", "", seen)
        result = run_loop(
            ports=_main_ports(InputQueue()),
            query="count",
            provider=provider,
            capabilities=caps,
            model="m",
            ctx=ctx,
            max_turns=20,
            graceful_interrupt=True,
        )
        viewer.join(5)
        # input_required 가 떴고 워커가 사람의 답(s)까지 멈췄다 — 자동 Continue 가 아니다
        assert seen["prompt"]["kind"] == "confirm"
        assert seen["stuck"]["asking"] is True
        assert "Stopped by the user after 6 turns" in (result.error or "")
        assert not any(
            "Auto-approved" in str(d.get("content", ""))
            for ev, d in r.hub.events
            if ev == "observation"
        )

    def test_dangerous_shell_confirm_is_still_auto_approved(self, monkeypatch):
        from agent_cli.tools.shell import _ask_confirmation

        r = WebRenderer()
        r.set_auto_approve(True)
        recorded = []
        r.observation = lambda content, turn, tool_name=None, success=True: (
            recorded.append((content, tool_name))
        )
        r.confirm = MagicMock(side_effect=AssertionError("⚡ 에서는 묻지 않는다"))
        monkeypatch.setattr(render, "get_renderer", lambda: r)
        assert _ask_confirmation("rm -rf build", "rm") == ("y", "")
        assert recorded == [
            ("⚡ Auto-approved (no confirmation): rm -rf build", "confirm")
        ]

    def test_path_confirm_is_still_auto_approved(self, tmp_path, monkeypatch):
        from agent_cli.tools import _confine

        ws, outside, systmp = tmp_path / "ws", tmp_path / "outside", tmp_path / "t"
        for d in (ws, outside, systmp):
            d.mkdir()
        monkeypatch.setenv("AGENT_CLI_WORKSPACE_CONFINE", "1")
        monkeypatch.setenv("AGENT_CLI_WORKSPACE_ROOT", str(ws))
        monkeypatch.setattr(_confine, "temp_roots", lambda: frozenset({systmp}))
        monkeypatch.setattr(_confine, "_session_root_allowlist", set())
        r = WebRenderer()
        r.set_auto_approve(True)
        recorded = []
        r.observation = lambda content, turn, tool_name=None, success=True: (
            recorded.append(content)
        )
        r.confirm = MagicMock(side_effect=AssertionError("⚡ 에서는 묻지 않는다"))
        monkeypatch.setattr(render, "get_renderer", lambda: r)
        assert _confine.guard([str(outside / "x")], "write_file") is None
        want = "⚡ Auto-approved (no confirmation): ⚠ write_file touches path(s)"
        assert recorded == [f"{want} outside the workspace"]

    def test_confirm_itself_never_auto_answers(self):
        """정책은 호출자의 것 — confirm 은 ⚡ 와 무관하게 input_required 를 띄운다."""
        from agent_cli.render.base import ConfirmOption

        r = WebRenderer()
        r.set_auto_approve(True)
        threading.Timer(
            0.3, lambda: r.push_user_input("confirm", {"key": "s", "comment": ""})
        ).start()
        asked: list = []
        orig = r.set_sticky

        def spy(name, event, payload, **kw):
            if name == "input_required":
                asked.append(payload["kind"])
            return orig(name, event, payload, **kw)

        r.set_sticky = spy
        got = r.confirm(
            "pause?",
            [ConfirmOption("c", "Continue"), ConfirmOption("s", "Stop")],
            default_key="c",
        )
        assert got == ("s", "") and asked == ["confirm"]
