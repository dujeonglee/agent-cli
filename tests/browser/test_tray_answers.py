"""❓ 트레이의 답 — 실브라우저 (v10.147.4).

트레이는 명단(roster)이 올 때마다 갱신된다. 항목 단위로 맞추므로 열린 질문의 항목은 그대로 남는다 —
쓰던 답·포커스·커서·사유 줄이 산다. 답 보내기는 입력창과 같은 규칙 하나다: 서버가 받았을 때만
비우고, 실패하면 그 항목 안에 사유가 서고 글이 남는다. 409(이미 답함·닫힘)는 글을 버리고 입력창
아래 줄에 알린다.
"""

from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

CLOSED = "Not sent — already answered or closed"  # 409·떨어져 나간 항목의 알림
AGT = "agt-0000aaaa"
PEER = "agt-0000bbbb"


def _wait(cond, timeout=8.0, step=0.05):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if cond():
            return True
        time.sleep(step)
    return False


def _row(key, questions, *, name="", state="idle"):
    return {
        "key": key,
        "name": name or key,
        "profile": "rev",
        "state": state,
        "open_questions": [
            {"id": qid, "text": text, "to": "user", "ts": 0} for qid, text in questions
        ],
    }


def _item(page, qid):
    return page.locator(f'#ask-tray .ask-item[data-qid="{qid}"]')


def _typing_state(page, selector):
    """그 입력의 글·포커스·커서 — 읽기만 한다."""
    return page.evaluate(
        """(sel) => { const i = document.querySelector(sel);
            return i ? { value: i.value, focused: document.activeElement === i,
                         caret: i.selectionStart } : null; }""",
        selector,
    )


class TestTypingSurvivesRosterUpdates:
    def test_agent_question_answer(self, stack, page):
        stack.emit_ready()
        stack.renderer.agent_roster([_row(AGT, [("q-1", "which file?")])])
        page.goto(stack.url)
        sel = '#ask-tray .ask-item[data-qid="q-1"] .ask-answer'
        page.wait_for_selector(sel, timeout=8000)
        page.click(sel)
        page.keyboard.type("src/ma")
        page.keyboard.press("ArrowLeft")
        # 같은 질문이 열린 채 명단이 다시 온다(상태가 바뀌었다)
        stack.renderer.agent_roster([_row(AGT, [("q-1", "which file?")], state="busy")])
        assert _wait(
            lambda: (
                page.locator(".ov-ch[data-key='" + AGT + "'] .ov-dot.b").count() == 1
            )
        )
        assert _typing_state(page, sel) == {
            "value": "src/ma",
            "focused": True,
            "caret": 5,
        }

    def test_main_prompt_answer(self, stack, page):
        results: list = []
        stack.start_ask(results)
        stack.emit_ready()
        page.goto(stack.url)
        sel = ".ask-main .ask-answer"
        page.wait_for_selector(sel, timeout=8000)
        page.click(sel)
        page.keyboard.type("Pyth")
        stack.renderer.agent_roster([_row(AGT, [])])
        assert _wait(lambda: page.locator(f".ov-ch[data-key='{AGT}']").count() == 1)
        assert _typing_state(page, sel) == {
            "value": "Pyth",
            "focused": True,
            "caret": 4,
        }

    def test_confirm_comment(self, stack, page):
        results: list = []
        stack.start_confirm_loop(results)
        stack.emit_ready()
        page.goto(stack.url)
        sel = ".ask-main .ask-answer"
        page.wait_for_selector(sel, timeout=8000)
        page.click(sel)
        page.keyboard.type("only /tmp")
        stack.renderer.agent_roster([_row(AGT, [])])
        assert _wait(lambda: page.locator(f".ov-ch[data-key='{AGT}']").count() == 1)
        assert _typing_state(page, sel) == {
            "value": "only /tmp",
            "focused": True,
            "caret": 9,
        }


class TestItemsAreKeptByQuestion:
    def _texts(self, page):
        return page.evaluate(
            """() => Array.from(document.querySelectorAll('#ask-tray > .ask-item'))
                 .map(i => i.classList.contains('ask-main') ? 'main' : i.dataset.qid)"""
        )

    def test_closing_or_adding_a_question_keeps_the_other_answer(self, stack, page):
        stack.emit_ready()
        stack.renderer.agent_roster(
            [_row(AGT, [("q-1", "first?")]), _row(PEER, [("q-2", "second?")])]
        )
        page.goto(stack.url)
        sel = '#ask-tray .ask-item[data-qid="q-2"] .ask-answer'
        page.wait_for_selector(sel, timeout=8000)
        assert self._texts(page) == ["q-1", "q-2"]
        page.click(sel)
        page.keyboard.type("half")
        # q-1 이 닫히고(다른 뷰어가 답했다) 새 질문 q-3 이 AGT 에 열린다
        stack.renderer.agent_roster(
            [_row(AGT, [("q-3", "third?")]), _row(PEER, [("q-2", "second?")])]
        )
        assert _wait(lambda: self._texts(page) == ["q-3", "q-2"])
        assert _typing_state(page, sel) == {
            "value": "half",
            "focused": True,
            "caret": 4,
        }

    def test_main_ask_stays_on_top_and_a_rename_updates_only_the_label(
        self, stack, page
    ):
        results: list = []
        stack.emit_ready()
        stack.renderer.agent_roster([_row(AGT, [("q-1", "first?")], name="old")])
        page.goto(stack.url)
        sel = '#ask-tray .ask-item[data-qid="q-1"] .ask-answer'
        page.wait_for_selector(sel, timeout=8000)
        page.click(sel)
        page.keyboard.type("abc")
        stack.start_ask(results)  # main 의 물음이 뒤에 온다 — 맨 위에 선다
        assert _wait(lambda: self._texts(page) == ["main", "q-1"])
        assert _typing_state(page, sel)["value"] == "abc"
        stack.renderer.agent_roster([_row(AGT, [("q-1", "first?")], name="new")])
        label = _item(page, "q-1").locator(".ask-q")
        assert _wait(lambda: "new" in label.inner_text())
        assert "❓" in label.inner_text() and "asked" in label.inner_text()
        assert _typing_state(page, sel)["value"] == "abc"
        page.fill(".ask-main .ask-answer", "Python")
        page.press(".ask-main .ask-answer", "Enter")
        assert _wait(lambda: results == ["Python"])
        assert _wait(lambda: self._texts(page) == ["q-1"])
        assert _typing_state(page, sel)["value"] == "abc"


@pytest.fixture
def live_registry(stack, monkeypatch, tmp_path):
    """실제 레지스트리 — 그 명단이 이 스택의 화면으로 간다. 에이전트 하나(질문을 걸 묻는 쪽)."""
    from agent_cli import render as render_mod
    from agent_cli.subagent.agents_live import AgentRegistry

    monkeypatch.setattr(render_mod, "_renderer", stack.renderer)
    monkeypatch.setattr(render_mod, "get_renderer", lambda: stack.renderer)
    reg = AgentRegistry(
        tmp_path / "agents",
        runtime={"model": "m"},
        runner=lambda kind, start, ctx: (
            SimpleNamespace(success=True, output="ok"),
            0.01,
        ),
    )
    stack.server.agent_registry = reg
    key, _ = reg.spawn(name="alpha")
    assert _wait(lambda: reg.get(key).state == "idle")
    yield reg, key
    reg.shutdown_all()


def _open_question(stack, page, reg, key, text="which file?"):
    qid, err = reg.register_question(key, text)
    assert not err
    stack.emit_ready()
    page.goto(stack.url)
    sel = f'#ask-tray .ask-item[data-qid="{qid}"] .ask-answer'
    page.wait_for_selector(sel, timeout=8000)
    return qid, sel


def _note(page, item_sel):
    page.wait_for_selector(f"{item_sel} > .ask-failed", timeout=8000)
    return page.inner_text(f"{item_sel} > .ask-failed")


def _bar_notice(page) -> str:
    page.wait_for_selector("#attach-strip .attach-msg", timeout=8000)
    return page.inner_text("#attach-strip .attach-msg")


class TestAgentAnswer:
    def test_accepted_answer_clears_and_the_roster_closes_the_item(
        self, stack, page, live_registry
    ):
        reg, key = live_registry
        qid, sel = _open_question(stack, page, reg, key)
        page.fill(sel, "src/main.py")
        page.press(sel, "Enter")
        assert _wait(lambda: _item(page, qid).count() == 0)
        assert reg.open_human_questions() == []
        assert page.locator("#attach-strip .attach-msg").count() == 0

    def test_network_error_keeps_the_answer_with_a_reason(
        self, stack, page, live_registry
    ):
        reg, key = live_registry
        qid, sel = _open_question(stack, page, reg, key)
        page.route("**/api/agent/*/input", lambda route: route.abort())
        page.fill(sel, "src/main.py")
        page.press(sel, "Enter")
        item = f'#ask-tray .ask-item[data-qid="{qid}"]'
        assert _note(page, item) == "⚠ Not sent — could not reach the server"
        assert page.input_value(sel) == "src/main.py"
        # 다시 보내기를 시작하면 사유 줄이 지워지고, 받으면 항목이 닫힌다
        page.unroute("**/api/agent/*/input")
        page.press(sel, "Enter")
        assert _wait(lambda: _item(page, qid).count() == 0)

    def test_no_response_times_out_and_a_late_answer_is_ignored(
        self, stack, page, live_registry
    ):
        reg, key = live_registry
        page.clock.install()
        qid, sel = _open_question(stack, page, reg, key)
        held: list = []
        page.route("**/api/agent/*/input", lambda route: held.append(route))
        page.fill(sel, "src/main.py")
        page.press(sel, "Enter")
        assert _wait(lambda: len(held) == 1)
        page.press(sel, "Enter")  # 보내는 중 — 받지 않는다
        item = f'#ask-tray .ask-item[data-qid="{qid}"]'
        assert _note(page, item) == "Still sending the previous answer"
        assert len(held) == 1
        page.clock.run_for(20_500)
        assert _wait(
            lambda: (
                page.inner_text(f"{item} > .ask-failed")
                == "⚠ Not sent — no response from the server"
            )
        )
        assert page.input_value(sel) == "src/main.py"
        try:
            held[0].fulfill(status=200, json={"accepted": True})
        except Exception:
            pass  # 끊긴 요청 — 페이지는 못 받는다
        page.unroute("**/api/agent/*/input")
        page.press(sel, "Enter")
        assert _wait(lambda: _item(page, qid).count() == 0)

    def test_registry_not_ready_is_a_reason_in_the_item(
        self, stack, page, live_registry
    ):
        reg, key = live_registry
        qid, sel = _open_question(stack, page, reg, key)
        stack.server.agent_registry = None  # 503 "agent registry not ready"
        page.fill(sel, "src/main.py")
        page.press(sel, "Enter")
        item = f'#ask-tray .ask-item[data-qid="{qid}"]'
        assert _note(page, item) == "⚠ Not sent — agent registry not ready"
        assert page.input_value(sel) == "src/main.py"
        stack.server.agent_registry = reg

    def test_already_answered_discards_and_closes_the_input_until_the_roster(
        self, stack, page, live_registry
    ):
        reg, key = live_registry
        qid, sel = _open_question(stack, page, reg, key)
        # 다른 뷰어가 먼저 답했다 — 명단은 아직 이 화면에 오지 않았다
        reg._notify_roster = lambda: None  # 인스턴스에만 — 아래에서 지운다
        assert reg.answer_question(qid, "other", by="user:other") == ""
        look = """(q) => { const b = document.querySelector(q + ' .ask-send');
                     const i = document.querySelector(q + ' .ask-answer');
                     return { send: parseFloat(getComputedStyle(b).opacity),
                              bg: getComputedStyle(i).backgroundColor,
                              ph: i.placeholder }; }"""
        q = f'#ask-tray .ask-item[data-qid="{qid}"]'
        open_look = page.evaluate(look, q)
        page.fill(sel, "mine")
        page.press(sel, "Enter")
        assert _bar_notice(page) == CLOSED
        assert page.input_value(sel) == ""
        assert page.is_disabled(sel)
        assert page.is_disabled(f"{q} .ask-send")
        # 꺼져 보인다 — Send 는 disabled 의 흐림(전환이 끝난 값), 입력은 꺼진 입력의 바탕·안내
        assert _wait(lambda: page.evaluate(look, q)["send"] < 1)
        closed = page.evaluate(look, q)
        assert closed["bg"] != open_look["bg"]
        assert closed["ph"] == "Already answered or closed"
        del reg._notify_roster  # 클래스의 것으로 돌아온다
        reg._notify_roster()
        assert _wait(lambda: _item(page, qid).count() == 0)

    def test_one_item_sending_does_not_block_another_or_the_input(
        self, stack, page, live_registry
    ):
        reg, key = live_registry
        q1, sel1 = _open_question(stack, page, reg, key, "first?")
        q2, _ = reg.register_question(key, "second?")
        sel2 = f'#ask-tray .ask-item[data-qid="{q2}"] .ask-answer'
        page.wait_for_selector(sel2, timeout=8000)
        held: list = []

        def hold_first(route):
            if q1 in (route.request.post_data or ""):
                held.append(route)
            else:
                route.continue_()

        page.route("**/api/agent/*/input", hold_first)
        page.fill(sel1, "one")
        page.press(sel1, "Enter")
        assert _wait(lambda: len(held) == 1)
        page.fill(sel2, "two")
        page.press(sel2, "Enter")
        assert _wait(lambda: _item(page, q2).count() == 0)  # 받았다 — 명단이 닫았다
        page.fill("#input", "hello main")
        page.press("#input", "Enter")
        assert _wait(lambda: stack.server.main_input.pending_count() == 1)
        assert page.input_value(sel1) == "one"
        held[0].continue_()
        assert _wait(lambda: _item(page, q1).count() == 0)


class TestMainAnswer:
    def test_prompt_network_error_keeps_the_answer(self, stack, page):
        results: list = []
        stack.start_ask(results)
        stack.emit_ready()
        page.goto(stack.url)
        sel = ".ask-main .ask-answer"
        page.wait_for_selector(sel, timeout=8000)
        page.route("**/api/input", lambda route: route.abort())
        page.fill(sel, "Python")
        page.press(sel, "Enter")
        assert _note(page, "#ask-tray .ask-main") == (
            "⚠ Not sent — could not reach the server"
        )
        assert page.input_value(sel) == "Python"
        page.unroute("**/api/input")
        page.click(".ask-main .ask-send")
        assert _wait(lambda: results == ["Python"])
        assert _wait(lambda: page.locator(".ask-main").count() == 0)

    def test_prompt_already_answered_folds_with_a_notice(self, stack, page):
        import requests

        results: list = []
        stack.start_ask(results)
        stack.emit_ready()
        page.goto(stack.url)
        sel = ".ask-main .ask-answer"
        page.wait_for_selector(sel, timeout=8000)

        def other_viewer_first(route):
            # 이 요청이 서버에 닿기 전에 다른 뷰어가 답한다 — 서버의 진짜 409
            r = requests.post(
                f"{stack.base}/api/input?token={stack.token}",
                json={"kind": "prompt", "content": "Rust"},
            )
            assert r.status_code == 200
            assert _wait(lambda: results == ["Rust"])
            route.continue_()

        page.route("**/api/input", other_viewer_first)
        page.fill(sel, "Python")
        page.press(sel, "Enter")
        assert _bar_notice(page) == CLOSED
        assert _wait(lambda: page.locator(".ask-main").count() == 0)
        assert results == ["Rust"]

    def test_confirm_refusal_keeps_the_comment_with_a_reason(self, stack, page):
        results: list = []
        stack.start_confirm_loop(results)
        stack.emit_ready()
        page.goto(stack.url)
        page.wait_for_selector(".ask-main .confirm-btn", timeout=8000)
        page.route(
            "**/api/input",
            lambda route: route.fulfill(status=500, body="Internal Server Error"),
        )
        page.fill(".ask-main .ask-answer", "only /tmp")
        page.click(".ask-main .confirm-btn >> nth=0")
        assert _note(page, "#ask-tray .ask-main") == "⚠ Not sent — HTTP 500"
        assert page.input_value(".ask-main .ask-answer") == "only /tmp"
        assert results == []
        page.unroute("**/api/input")
        page.click(".ask-main .confirm-btn >> nth=0")
        assert _wait(lambda: results and results[0] == ("y", "only /tmp"))

    def test_confirm_already_answered_folds_with_a_notice(self, stack, page):
        import threading

        import requests

        from agent_cli.render.base import ConfirmOption

        results: list = []
        opts = [
            ConfirmOption(key="y", label="once"),
            ConfirmOption(key="n", label="deny"),
        ]

        def once():
            if stack.wait_connected():
                results.append(stack.renderer.confirm("Allow?", opts, default_key="n"))

        t = threading.Thread(target=once, daemon=True)
        t.start()
        stack.emit_ready()
        page.goto(stack.url)
        page.wait_for_selector(".ask-main .confirm-btn", timeout=8000)

        def other_viewer_first(route):
            r = requests.post(
                f"{stack.base}/api/input?token={stack.token}",
                json={"kind": "confirm", "key": "n", "comment": ""},
            )
            assert r.status_code == 200
            assert _wait(lambda: bool(results))
            route.continue_()

        page.route("**/api/input", other_viewer_first)
        page.click(".ask-main .confirm-btn >> nth=0")
        assert _bar_notice(page) == CLOSED
        assert _wait(lambda: page.locator(".ask-main").count() == 0)
        assert results == [("n", "")]
        t.join(5)


def _drop_sse(stack, page) -> str:
    """SSE 를 서버 쪽에서 끊는다 — EventSource 가 약 3초 뒤 다시 붙어 스냅샷(스티키 포함)을 다시 받는다.
    끊기 전 연결의 id 를 돌려준다."""
    before = page.evaluate("() => window.AGENTCLI_CONN_ID")
    hub = stack.renderer.hub
    for c in list(hub._connections):
        hub.unregister(c)
    return before


def _wait_reconnected(page, before):
    """새 연결의 ``identity`` 가 왔다(스냅샷이 시작됐다)."""
    assert _wait(
        lambda: page.evaluate("() => window.AGENTCLI_CONN_ID") not in (None, before),
        timeout=15,
    )


def _reconnect(stack, page):
    _wait_reconnected(page, _drop_sse(stack, page))
    page.wait_for_function("() => !!document.querySelector('.ask-main')")


def _pending_prompt(stack, results, question):
    """main 의 물음 하나를 띄운다 — 화면 연결을 기다리지 않는다(끊긴 사이·첫 접속 전에 떠 있게)."""
    import threading

    def ask():
        try:
            results.append(
                stack.renderer.prompt_user("Your answer: ", context=question)
            )
        except EOFError:
            pass

    t = threading.Thread(target=ask, daemon=True)
    t.start()
    assert _wait(lambda: stack.renderer.is_awaiting_input())
    return t


class TestSameAskAcrossReconnect:
    def test_typing_survives_a_reconnect_replay_of_the_same_ask(self, stack, page):
        results: list = []
        stack.start_ask(results)
        stack.emit_ready()
        page.goto(stack.url)
        sel = ".ask-main .ask-answer"
        page.wait_for_selector(sel, timeout=8000)
        page.click(sel)
        page.keyboard.type("Pyth")
        _reconnect(stack, page)
        assert _typing_state(page, sel) == {
            "value": "Pyth",
            "focused": True,
            "caret": 4,
        }

    def test_ask_resolved_while_disconnected_folds_after_the_snapshot(
        self, stack, page
    ):
        """끊긴 사이 다른 뷰어가 답했다 — 재접속 스냅샷에 물음이 없으면 남은 항목을 접는다(알림 없음)."""
        import requests

        results: list = []
        stack.start_ask(results)
        stack.emit_ready()
        page.goto(stack.url)
        page.wait_for_selector(".ask-main .ask-answer", timeout=8000)
        page.fill(".ask-main .ask-answer", "Pyth")
        before = _drop_sse(stack, page)
        r = requests.post(
            f"{stack.base}/api/input?token={stack.token}",
            json={"kind": "prompt", "content": "Rust"},
        )
        assert r.status_code == 200 and _wait(lambda: results == ["Rust"])
        # 풀림 알림(input_resolved)은 아무에게도 가지 않았다
        assert not stack.renderer.hub.has_live_connections()
        _wait_reconnected(page, before)
        assert _wait(lambda: page.locator(".ask-main").count() == 0)
        assert page.locator("#attach-strip .attach-msg").count() == 0

    def test_a_different_ask_in_the_snapshot_replaces_the_item(self, stack, page):
        """끊긴 사이 물음이 풀리고 새 물음이 떴다 — 다른 ``ask_id`` 라 새 항목이다(쓰던 글은 버려진다)."""
        import requests

        results: list = []
        first = _pending_prompt(stack, results, "first?")
        stack.emit_ready()
        page.goto(stack.url)
        page.wait_for_selector(".ask-main .ask-answer", timeout=8000)
        assert "first?" in page.inner_text(".ask-main")
        page.fill(".ask-main .ask-answer", "Pyth")
        page.evaluate(
            "() => { window.__oldAsk = document.querySelector('.ask-main'); }"
        )
        before = _drop_sse(stack, page)
        r = requests.post(
            f"{stack.base}/api/input?token={stack.token}",
            json={"kind": "prompt", "content": "Rust"},
        )
        assert r.status_code == 200 and _wait(lambda: results == ["Rust"])
        first.join(5)
        second = _pending_prompt(stack, results, "second?")
        _wait_reconnected(page, before)
        assert _wait(lambda: "second?" in page.inner_text("#ask-tray"))
        assert page.locator(".ask-main").count() == 1
        assert page.evaluate(
            "() => document.querySelector('.ask-main') !== window.__oldAsk"
        )
        assert page.input_value(".ask-main .ask-answer") == ""
        stack.renderer.push_abort()
        second.join(5)

    def test_an_ask_pending_at_first_connect_stays(self, stack, page):
        """첫 접속에 물음이 이미 떠 있다 — 스냅샷 안에 있으니 접지 않는다."""
        results: list = []
        t = _pending_prompt(stack, results, "which language?")
        stack.emit_ready()
        page.goto(stack.url)
        page.wait_for_selector(".ask-main .ask-answer", timeout=8000)
        # 라이브 이벤트 하나가 그려지면 그 앞의 snapshot_end 는 이미 처리됐다
        stack.renderer.agent_roster([_row(AGT, [])])
        assert _wait(lambda: page.locator(f".ov-ch[data-key='{AGT}']").count() == 1)
        assert page.locator(".ask-main").count() == 1
        stack.renderer.push_abort()
        t.join(5)


class TestConfirmStall:
    def test_click_again_clears_the_stall_and_a_refusal_leaves_only_the_reason(
        self, stack, page
    ):
        results: list = []
        page.clock.install()
        stack.start_confirm_loop(results)
        stack.emit_ready()
        page.goto(stack.url)
        page.wait_for_selector(".ask-main .confirm-btn", timeout=8000)
        held: list = []
        page.route("**/api/input", lambda route: held.append(route))
        stall = page.locator(".ask-main > .confirm-stall")
        page.click(".ask-main .confirm-btn >> nth=0")
        assert _wait(lambda: len(held) == 1)
        page.clock.run_for(3_100)
        assert _wait(lambda: stall.count() == 1)
        page.click(".ask-main .confirm-btn >> nth=0")  # 다시 누른다 — 경고는 지워진다
        assert _wait(lambda: len(held) == 2)
        assert stall.count() == 0
        page.clock.run_for(3_100)  # 다시 3초 뒤에만 선다
        assert _wait(lambda: stall.count() == 1)
        for r in held:
            r.fulfill(status=500, body="Internal Server Error")
        assert _note(page, "#ask-tray .ask-main") == "⚠ Not sent — HTTP 500"
        assert stall.count() == 0
        page.clock.run_for(3_100)
        assert stall.count() == 0  # 답이 왔다 — 경고가 다시 서지 않는다
        assert results == []


class TestItemLeftWhileSending:
    def test_agent_answer_failure_after_the_item_left_notices_below_the_input(
        self, stack, page, live_registry
    ):
        reg, key = live_registry
        qid, sel = _open_question(stack, page, reg, key)
        held: list = []
        page.route("**/api/agent/*/input", lambda route: held.append(route))
        page.fill(sel, "mine")
        page.press(sel, "Enter")
        assert _wait(lambda: len(held) == 1)
        assert (
            reg.answer_question(qid, "other", by="user:other") == ""
        )  # 명단이 항목을 닫는다
        assert _wait(lambda: _item(page, qid).count() == 0)
        held[0].abort()
        assert _bar_notice(page) == CLOSED

    def test_main_prompt_failure_after_the_item_left_notices_below_the_input(
        self, stack, page
    ):
        import requests

        results: list = []
        stack.start_ask(results)
        stack.emit_ready()
        page.goto(stack.url)
        sel = ".ask-main .ask-answer"
        page.wait_for_selector(sel, timeout=8000)
        held: list = []
        page.route("**/api/input", lambda route: held.append(route))
        page.fill(sel, "Python")
        page.press(sel, "Enter")
        assert _wait(lambda: len(held) == 1)
        r = requests.post(
            f"{stack.base}/api/input?token={stack.token}",
            json={"kind": "prompt", "content": "Rust"},
        )
        assert r.status_code == 200
        assert _wait(lambda: page.locator(".ask-main").count() == 0)
        held[0].abort()
        assert _bar_notice(page) == CLOSED
        assert results == ["Rust"]


@pytest.fixture
def busy_agent(stack, monkeypatch, tmp_path):
    """일하는 중인 에이전트 하나(런이 붙잡혀 있다)와 그가 건 질문 — 실제 레지스트리, 명단은 이 화면으로."""
    import threading

    from agent_cli import render as render_mod
    from agent_cli.subagent.agents_live import AgentRegistry

    monkeypatch.setattr(render_mod, "_renderer", stack.renderer)
    monkeypatch.setattr(render_mod, "get_renderer", lambda: stack.renderer)
    gate, inside = threading.Event(), threading.Event()

    def runner(kind, start, ctx):
        inside.set()
        gate.wait(20)
        return SimpleNamespace(success=True, output="ok"), 0.01

    reg = AgentRegistry(tmp_path / "busy", runtime={"model": "m"}, runner=runner)
    stack.server.agent_registry = reg
    key, _ = reg.spawn(name="alpha")
    assert _wait(lambda: reg.get(key).state == "idle")
    assert reg.request(key, "work", author="user:dj") == ""
    assert inside.wait(5)
    yield reg, key
    gate.set()
    reg.shutdown_all()


class TestKillClosesQuestions:
    def test_kill_while_working_removes_the_item(self, stack, page, busy_agent):
        reg, key = busy_agent
        qid, sel = _open_question(stack, page, reg, key)
        page.fill(sel, "half")
        r = page.request.post(f"{stack.base}/api/agent/{key}/kill?token={stack.token}")
        assert r.ok
        assert reg.get(key).state == "busy"  # 워커는 아직 런 안
        assert _wait(lambda: _item(page, qid).count() == 0)

    def test_answer_that_reaches_after_the_kill_is_refused_with_a_notice(
        self, stack, page, busy_agent
    ):
        reg, key = busy_agent
        qid, sel = _open_question(stack, page, reg, key)
        held: list = []
        page.route("**/api/agent/*/input", lambda route: held.append(route))
        page.fill(sel, "src/main.py")
        page.press(sel, "Enter")
        assert _wait(lambda: len(held) == 1)
        reg.kill(key)  # 답이 서버에 닿기 전에 ✕
        assert _wait(lambda: _item(page, qid).count() == 0)
        with page.expect_response(lambda r: "/input" in r.url) as info:
            held[0].continue_()
        assert info.value.status == 409
        assert _bar_notice(page) == CLOSED
        assert reg.queued_for(f"agent:{key}") == ()
