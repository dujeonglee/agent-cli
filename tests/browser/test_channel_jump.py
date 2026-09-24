"""채널 필터 · 런 블록 · 점프 · 중첩 블록 — v9.4.0 ⑥ → v9.23.0 §12.

⑥ 에서 채널이 타임라인의 필터가 됐고, v9.23.0 에서 에이전트 채널의 단위가
**런 블록**이 됐다(docs/chat-ui §12). 여기서 고정하는 계약:

1. **채널 = 필터** — `#messages` 직계 자식의 `data-ch` 로 걸러진다. `data-ch`
   가 없는 노드(생성 중 한 줄)는 어느 채널에서나 보인다.
2. **런 블록** — 상주 에이전트의 런(`scope_start`, `key#seq`)마다 블록 하나:
   받은 항목이 머리, 단계가 몸통, 결과가 꼬리. 수신 줄은 큐에 넣을 때 나와
   런보다 앞서므로 블록이 없으면 `⏳ 대기` 로 두었다가 `seqs` 로 끌어온다.
   보냄 줄은 없다 — `message`/`reply`/`answer` **도구 줄**이 그 발신이고,
   도구 호출 없는 하네스 폴백만 ⚠ 줄로 남는다.
3. **점프는 main → agent 만** — `⚡ agent` 도구 줄의 칩. 왕래 줄의 상대 칩은
   줄과 함께 사라졌다(peer 만 눌리고 main·사람은 안 눌리던 반쪽 버튼).
4. **돌아가기는 한 단계** — 누르면 사라진다.
5. **중첩 블록** — skill/inline agent 는 좌측 레일의 접히는 카드, 상주 agent
   의 런은 접히지 않는 블록(자기 채널 안에서 자기를 또 접지 않는다).

전부 **렌더된 DOM/가시성**으로 본다 — 소스 핀(tests/test_web_server.py)이
"호출이 있다"까지만 보는 층이라 필터가 실제로 숨기는지는 여기서만 잡힌다.
"""

from __future__ import annotations

import time

AGT = "agt-rev"
PEER = "agt-doc"


def _wait(cond, timeout=8.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if cond():
            return True
        time.sleep(0.05)
    return False


def _roster(stack, *keys):
    """두 상주 에이전트를 올린다 — 점프 가능 판정이 roster 를 진실로 쓴다
    (모르는 key 로는 점프 칩이 생기지 않는다)."""
    stack.renderer.agent_roster(
        [{"key": k, "name": k, "profile": "reviewer", "state": "idle"} for k in keys]
    )


def _chip(page, key):
    return page.locator(f'.ov-ch[data-key="{key}"]')


def _visible_rows(page, sel):
    """`hidden` 이 아닌 것만 — 필터는 `el.hidden` 으로 숨긴다."""
    return page.locator(sel + ":not([hidden])")


class TestChannelFilter:
    """채널 전환이 **표시**를 바꾼다 (⑥ 이전엔 입력 라우팅만 바뀌었다)."""

    def test_agent_work_lands_in_an_open_run_block_in_its_channel(self, stack, page):
        """상주 에이전트의 런은 **접히지 않는 런 블록** 안에 흐른다 (v9.23.0).

        v9.7.0 은 🦀 스코프 카드(기본 접힘)를 걷어내 채널에 평평하게 흘렸다 —
        접힘이 투명성을 숨겨서였다. 블록은 그 자리를 잇되 접히지 않고, 종전
        왕래 줄이 긋던 "요청 경계" 를 대신 긋는다."""
        stack.emit_ready()
        _roster(stack, AGT)
        page.goto(stack.url)
        assert _wait(lambda: _chip(page, AGT).count() > 0)

        stack.renderer.begin_scope(
            task_id=f"{AGT}#1",
            kind="run",
            label="리뷰해줘",
            agent=AGT,
            parent="",
            ctx_dir=f"agents/{AGT}",
        )
        stack.renderer.thought("먼저 확인한다", 1)
        stack.renderer.final("검토 끝", turn=1)  # thought 는 다음 턴 이벤트에 실린다

        blk = page.locator(f'#messages > .card-run[data-task-id="{AGT}#1"]')
        assert _wait(lambda: blk.count() == 1)
        # 접히는 스코프 카드가 아니다.
        assert page.locator(f'.card-task-group[data-task-id="{AGT}#1"]').count() == 0
        # 내부 턴은 블록 **몸통** 안에 있고, 블록이 채널 귀속을 진다.
        card = blk.locator(".run-body > .card-assistant").first
        assert _wait(lambda: card.count() == 1)
        assert blk.get_attribute("data-ch") == AGT
        assert not blk.is_visible()
        _chip(page, AGT).click()
        assert _wait(lambda: card.is_visible())  # 클릭 없이 바로 보인다

    def test_nested_scope_inside_an_agent_keeps_its_own_card(self, stack, page):
        """에이전트 안에서 열린 skill/inline 은 **여전히 자기 카드**를 갖는다 —
        런 블록의 몸통 안에 담긴다(부모 런이 블록이므로)."""
        stack.emit_ready()
        _roster(stack, AGT)
        page.goto(stack.url)
        assert _wait(lambda: _chip(page, AGT).count() > 0)
        _chip(page, AGT).click()

        stack.renderer.begin_scope(
            task_id=f"{AGT}#1",
            kind="run",
            label="리뷰",
            agent=AGT,
            parent="",
            ctx_dir=f"agents/{AGT}",
        )
        stack.renderer.begin_scope(
            task_id="sk-in", kind="skill", label="plan", parent=f"{AGT}#1"
        )
        sk = page.locator(
            f'.card-run[data-task-id="{AGT}#1"] .run-body [data-task-id="sk-in"]'
        )
        assert _wait(lambda: sk.count() == 1)
        assert "scope-skill" in (sk.get_attribute("class") or "")
        assert sk.is_visible()

    def test_main_cards_hide_when_viewing_an_agent_channel(self, stack, page):
        """반대 방향도 성립해야 필터다 — 한쪽만 걸리면 두 대화가 섞인다."""
        stack.emit_ready()
        _roster(stack, AGT)
        page.goto(stack.url)
        assert _wait(lambda: _chip(page, AGT).count() > 0)

        stack.renderer.final("메인 최종답", turn=1)
        main_card = page.locator("#messages > .card-assistant")
        assert _wait(lambda: main_card.count() > 0)
        assert main_card.first.is_visible()

        _chip(page, AGT).click()
        assert _wait(lambda: not main_card.first.is_visible())
        _chip(page, "main").click()
        assert _wait(lambda: main_card.first.is_visible())

    def test_generating_line_belongs_to_its_own_channel(self, stack, page):
        """생성 중 줄은 **그 스코프의 채널에만** 보인다 (v9.9.0, 사용자 제보).

        v9.4.0 은 이 줄을 `data-ch` 없이 두어 모든 채널에 보이게 했다 — *"필터가
        무조건 숨기면 에이전트 채널에서 생성 중 표시가 사라져 '멈춘 것처럼'
        보인다"*. 에이전트가 하나일 때를 전제한 판단이라, 넷을 띄우자 정반대로
        거짓말이 됐다: **멈춰 있는 세 창에 옆 에이전트의 사고량이 비쳤다.**
        컨텍스트가 에이전트마다 분리돼 있으므로 진행 표시도 그래야 한다.
        """
        stack.emit_ready()
        _roster(stack, AGT)
        page.goto(stack.url)
        assert _wait(lambda: _chip(page, AGT).count() > 0)

        # main 스코프의 생성 중 줄 — main 에서만 보인다.
        stack.renderer.stream_chunk("본문 " * 60)
        gen = page.locator("#messages > .gen")
        assert _wait(lambda: gen.count() > 0 and gen.is_visible())
        assert gen.get_attribute("data-ch") == "main"

        _chip(page, AGT).click()
        assert _wait(lambda: not gen.is_visible()), (
            "main 의 생성 중 표시가 에이전트 채널에 비친다"
        )

    def test_each_agent_gets_its_own_generating_line_and_numbers(self, stack, page):
        """동시에 도는 둘은 **각자의 줄·각자의 숫자**를 갖는다.

        서버 카운터도 같은 릴리스에서 스코프별로 갈랐다 — 화면만 나눠 그리면
        숫자 자체가 이미 섞인 값이다(`_tick_state`). 여기서는 그 둘이 **함께**
        동작해 실제 화면에 다른 숫자가 나오는지를 본다."""
        import threading

        stack.emit_ready()
        _roster(stack, AGT, PEER)
        page.goto(stack.url)
        assert _wait(lambda: _chip(page, PEER).count() > 0)

        def stream_as(key, chunk):
            """에이전트 워커처럼 **자기 스레드**에서 자기 스코프를 연다."""
            r = stack.renderer
            r.begin_scope(
                task_id=f"{key}#1",
                kind="run",
                label="작업",
                agent=key,
                parent="",
                ctx_dir=f"agents/{key}",
            )
            r.thinking_chunk(chunk)

        for key, n in ((AGT, 400), (PEER, 1200)):
            t = threading.Thread(target=stream_as, args=(key, "x" * n))
            t.start()
            t.join()

        assert _wait(lambda: page.locator("#messages > .gen").count() == 2)
        by_ch = {
            g.get_attribute("data-ch"): g.inner_text()
            for g in page.locator("#messages > .gen").all()
        }
        assert set(by_ch) == {AGT, PEER}, f"채널 귀속이 어긋남: {list(by_ch)}"
        # 400/4=100, 1200/4=300 — 섞였다면 둘 다 400 이 된다.
        assert "100" in by_ch[AGT] and "300" in by_ch[PEER], by_ch

        # 각 채널에서 자기 것만 보인다.
        _chip(page, AGT).click()
        assert _wait(
            lambda: (
                page.locator(f'.gen[data-ch="{AGT}"]').first.is_visible()
                and not page.locator(f'.gen[data-ch="{PEER}"]').first.is_visible()
            )
        )

    def test_one_agent_finishing_leaves_the_others_line_alone(self, stack, page):
        """한 쪽의 `stream_end` 가 전원의 줄을 거두면 안 된다."""
        import threading

        stack.emit_ready()
        _roster(stack, AGT, PEER)
        page.goto(stack.url)
        assert _wait(lambda: _chip(page, PEER).count() > 0)

        def run(key, fn):
            r = stack.renderer
            r.begin_scope(
                task_id=f"{key}#1",
                kind="run",
                label="작업",
                agent=key,
                parent="",
                ctx_dir=f"agents/{key}",
            )
            fn(r)

        for key in (AGT, PEER):
            t = threading.Thread(
                target=run, args=(key, lambda r: r.thinking_chunk("x" * 400))
            )
            t.start()
            t.join()
        assert _wait(lambda: page.locator("#messages > .gen").count() == 2)

        t = threading.Thread(target=run, args=(AGT, lambda r: r.stream_end()))
        t.start()
        t.join()

        assert _wait(lambda: page.locator(f'.gen[data-ch="{AGT}"]').count() == 0)
        assert page.locator(f'.gen[data-ch="{PEER}"]').count() == 1, (
            "한 에이전트의 종료가 다른 에이전트의 생성 중 줄을 거뒀다"
        )

    def test_main_user_message_does_not_leak_into_agent_channels(self, stack, page):
        """`data-ch` 없음 = 모든 채널에 보임 이므로, 루트에 카드를 붙이는 경로는
        **빠짐없이** appendToTimeline 을 거쳐야 한다.

        실장에서 잡은 회귀: ``renderUserMessage`` 만 `$messages.appendChild` 로
        직결이라 main 의 사용자 입력이 에이전트 채널에도 떠 있었다. 필터 자체는
        멀쩡했으므로(다른 TC 전부 통과) **이 누락은 이 단언으로만 잡힌다**."""
        stack.emit_ready()
        _roster(stack, AGT)
        page.goto(stack.url)
        assert _wait(lambda: _chip(page, AGT).count() > 0)

        stack.renderer.push_user_message("[두정]: 리뷰 돌려줘", author="두정")
        card = page.locator("#messages > .card-user")
        assert _wait(lambda: card.count() > 0)
        assert card.get_attribute("data-ch") == "main"

        _chip(page, AGT).click()
        assert _wait(lambda: not card.is_visible()), (
            "main 사용자 입력이 에이전트 채널에 샜다"
        )

    def test_retry_tick_draws_nothing_in_any_channel(self, stack, page):
        """형식 거부는 v9.8.0 부터 카드를 만들지 않는다 — 따라서 채널 누수도
        구조적으로 불가능하다.

        종전엔 `failed_turn` 이 `task_id` 를 잃고 main 에 빨간 박스로 떴고
        (사용자 보고), v9.8.0 에서 그 카드 자체를 없앴다. 귀속을 검사하던
        자리에 **아무것도 안 그린다**는 계약을 대신 박는다."""
        stack.emit_ready()
        _roster(stack, AGT)
        page.goto(stack.url)
        assert _wait(lambda: _chip(page, AGT).count() > 0)

        stack.renderer.begin_scope(
            task_id=f"{AGT}#1",
            kind="run",
            label="3+3",
            agent=AGT,
            parent="",
            ctx_dir=f"agents/{AGT}",
        )
        stack.renderer.recovery("6입니다.", "형식을 지켜 다시", "no action", 1)
        assert _wait(lambda: "재시도" in page.locator("#messages .gen").inner_text())
        for ch in ("main", AGT):
            _chip(page, ch).click()
            body = page.locator("#messages").inner_text()
            assert "6입니다." not in body, f"{ch} 채널에 거부된 원문이 있다"
            assert "형식을 지켜 다시" not in body, f"{ch} 채널에 개입이 있다"

    def test_switching_channel_lands_at_the_bottom(self, stack, page):
        """채널을 열면 **그 대화의 맨 아래**로 간다 (사용자 보고).

        필터가 노드를 감추고 드러내면 문서 높이가 확 바뀌는데 스크롤 위치는
        그대로라, 칩을 누를 때마다 아무 데나 떨어져 규칙을 읽을 수 없었다.
        대화를 열면 최신부터 보는 게 채팅의 규칙이다."""
        stack.emit_ready()
        _roster(stack, AGT)
        page.goto(stack.url)
        assert _wait(lambda: _chip(page, AGT).count() > 0)

        for i in range(40):  # main 을 길게 만들어 스크롤이 생기게
            stack.renderer.final(f"메인 응답 {i}\n" + ("본문 " * 30), turn=i)
        for i in range(12):
            stack.renderer.agent_message(
                key=AGT,
                direction="in",
                author="main",
                text=f"에이전트 요청 {i}\n" + ("본문 " * 30),
                to=AGT,
            )
        assert _wait(lambda: page.locator("#messages > *").count() >= 52)

        def at_bottom():
            return page.evaluate(
                "() => { const m = document.getElementById('messages');"
                " return m.scrollHeight - m.scrollTop - m.clientHeight < 40; }"
            )

        page.evaluate("document.getElementById('messages').scrollTop = 0")
        _chip(page, AGT).click()
        assert _wait(at_bottom), "에이전트 채널을 열었는데 맨 아래가 아니다"
        page.evaluate("document.getElementById('messages').scrollTop = 0")
        _chip(page, "main").click()
        assert _wait(at_bottom), "main 으로 돌아왔는데 맨 아래가 아니다"


class TestRunBlock:
    """에이전트 채널의 단위는 런이다 (v9.23.0, docs/chat-ui §12)."""

    def _open(self, stack, page, *keys):
        stack.emit_ready()
        _roster(stack, *(keys or (AGT,)))
        page.goto(stack.url)
        assert _wait(lambda: _chip(page, AGT).count() > 0)
        _chip(page, AGT).click()

    def _begin(self, stack, seq=1, seqs=None, key=AGT):
        stack.renderer.begin_agent_work(
            key=key, seq=seq, profile="reviewer", message="일감", seqs=seqs
        )

    def test_incoming_row_waits_then_becomes_the_block_head(self, stack, page):
        """수신 줄은 큐에 넣을 때 나와 런보다 앞선다 — 블록이 없으면 `⏳ 대기`,
        그 seq 를 품은 런이 열리면 블록 머리로 옮겨 간다."""
        self._open(stack, page)
        stack.renderer.agent_message(
            key=AGT,
            direction="in",
            author="main",
            text="방금 커밋 리뷰해줘",
            to=AGT,
            seq=1,
        )
        queued = page.locator("#messages > .card-queued .row.queued")
        assert _wait(lambda: queued.count() == 1)
        assert queued.locator(".ic").inner_text().strip() == "⏳"
        assert queued.locator(".k").inner_text().strip() == "대기"
        assert "main" in queued.locator(".peer").inner_text()

        self._begin(stack, seq=1)
        item = page.locator(f'.card-run[data-task-id="{AGT}#1"] .run-head .run-item')
        assert _wait(lambda: item.count() == 1)
        assert page.locator("#messages > .card-queued").count() == 0
        assert item.locator(".ic").inner_text().strip() == "←"
        assert "방금 커밋 리뷰해줘" in item.locator(".s").inner_text()
        assert item.locator(".who").inner_text().strip() == "main"

    def test_incoming_row_after_the_scope_lands_in_the_head(self, stack, page):
        """resume 재생은 순서를 보장하지 않는다(스코프 재생과 대화 재생이 다른
        경로) — 블록이 먼저 있으면 수신 줄이 곧장 머리로 간다."""
        self._open(stack, page)
        self._begin(stack, seq=1)
        assert _wait(
            lambda: page.locator(f'.card-run[data-task-id="{AGT}#1"]').count() == 1
        )
        stack.renderer.agent_message(
            key=AGT, direction="in", author="main", text="늦게 온 수신", to=AGT, seq=1
        )
        item = page.locator(f'.card-run[data-task-id="{AGT}#1"] .run-head .run-item')
        assert _wait(lambda: item.count() == 1)
        assert page.locator("#messages > .card-queued").count() == 0

    def test_batch_claims_all_its_items(self, stack, page):
        """사람 창 배치: N건이 런 1개 — `scope_start.seqs` 가 머리를 N줄로."""
        self._open(stack, page)
        stack.renderer.agent_message(
            key=AGT, direction="in", author="user:dj", text="요약해 줘", to=AGT, seq=2
        )
        stack.renderer.agent_message(
            key=AGT,
            direction="in",
            author="user:ann",
            text="끝나면 알려줘",
            to=AGT,
            seq=3,
        )
        assert _wait(lambda: page.locator("#messages > .card-queued").count() == 2)
        self._begin(stack, seq=2, seqs=[2, 3])
        items = page.locator(f'.card-run[data-task-id="{AGT}#2"] .run-head .run-item')
        assert _wait(lambda: items.count() == 2)
        assert page.locator("#messages > .card-queued").count() == 0
        assert items.nth(0).locator(".who").inner_text().strip() == "dj (사람)"
        assert items.nth(1).locator(".who").inner_text().strip() == "ann (사람)"

    def test_plain_out_row_draws_nothing_and_fallback_draws_a_row(self, stack, page):
        """`→ 보냄` 은 없다 — 도구 줄이 그 발신이다. 도구 호출 없는 하네스
        폴백만 ⚠ 줄로 블록 몸통에 남는다."""
        self._open(stack, page)
        self._begin(stack, seq=1)
        blk = page.locator(f'.card-run[data-task-id="{AGT}#1"]')
        assert _wait(lambda: blk.count() == 1)
        stack.renderer.agent_message(
            key=AGT, direction="out", author=AGT, text="과일", to="main", seq=1
        )
        stack.renderer.agent_message(
            key=AGT,
            direction="out",
            author=AGT,
            text="(no explicit reply — this is the agent's run summary)\n리뷰 결과 요약",
            to="main",
            seq=1,
            fallback=True,
        )
        fb = blk.locator(".run-body .card-fallback .row.fallback")
        assert _wait(lambda: fb.count() == 1)
        assert fb.locator(".k").inner_text().strip() == "폴백"
        assert "main" in fb.locator(".peer").inner_text()
        # 평범한 out 은 아무 줄도 만들지 않았다.
        assert page.locator("#messages").inner_text().count("과일") == 0

    def test_final_becomes_the_foot_with_duration(self, stack, page):
        """최종답이 있으면 **그 카드가 꼬리**다 — 같은 말을 두 번 하지 않는다."""
        self._open(stack, page)
        self._begin(stack, seq=1)
        stack.renderer.final("검토 끝", turn=1)
        stack.renderer.end_agent_work(key=AGT, seq=1, success=True, duration_s=2.1)
        blk = page.locator(f'.card-run[data-task-id="{AGT}#1"]')
        assert _wait(lambda: "run-ok" in (blk.get_attribute("class") or ""))
        meta = blk.locator(".run-final .run-meta")
        assert meta.count() == 1 and meta.inner_text().strip() == "✓ (2.1s)"
        assert blk.locator(".row.run-foot").count() == 0
        assert blk.inner_text().count("검토 끝") == 1

    def test_failed_run_gets_a_status_foot(self, stack, page):
        self._open(stack, page)
        self._begin(stack, seq=1)
        stack.renderer.end_agent_work(
            key=AGT, seq=1, success=False, duration_s=0.8, error="killed"
        )
        blk = page.locator(f'.card-run[data-task-id="{AGT}#1"]')
        assert _wait(lambda: "run-fail" in (blk.get_attribute("class") or ""))
        foot = blk.locator(".row.run-foot.bad")
        assert foot.count() == 1
        assert "killed" in foot.locator(".s").inner_text()

    def test_send_tool_row_has_arrow_peer_and_no_jump(self, stack, page):
        """`message` 도구 줄이 발신이다: 아이콘 →, 본문이 요약, 꼬리에 상대.
        상대는 글자다 — 눌리지 않는다."""
        self._open(stack, page, AGT, PEER)
        self._begin(stack, seq=1)
        stack.renderer.action("message", f'{{"to":"{PEER}","text":"과일"}}', turn=1)
        stack.renderer.observation(
            f"[message → {PEER}] delivered", turn=1, tool_name="message", success=True
        )
        row = page.locator(f'.card-run[data-task-id="{AGT}#1"] .row.act.send')
        assert _wait(lambda: row.count() == 1)
        assert row.locator(".ic").inner_text().strip() == "→"
        assert row.locator(".k").inner_text().strip() == "message"
        assert row.locator(".s").inner_text().strip() == "과일"
        peer = row.locator(".peer")
        assert PEER in peer.inner_text()
        assert "can-jump" not in (peer.get_attribute("class") or "")
        # 배달 결과는 스텝 배지가 말한다.
        assert _wait(
            lambda: (
                page.locator(f'.card-run[data-task-id="{AGT}#1"] .badge.ok').count()
                == 1
            )
        )

    def test_timestamp_does_not_overlap_the_peer_chip(self, stack, page):
        """시각과 상대 이름이 **겹쳐 그려지지 않는다** — 꼬리 칸이 있는 줄은
        시각을 같은 그리드 안에 넣는다(코너 배지가 칩을 덮던 사고)."""
        self._open(stack, page, AGT, PEER)
        self._begin(stack, seq=1)
        stack.renderer.action("message", f'{{"to":"{PEER}","text":"과일"}}', turn=1)
        row = page.locator(f'.card-run[data-task-id="{AGT}#1"] .row.act.send')
        assert _wait(lambda: row.count() == 1)
        peer = row.locator(".peer").bounding_box()
        tm = row.locator(".row-time").bounding_box()
        assert peer and tm
        assert peer["x"] + peer["width"] <= tm["x"] + 0.5, (
            f"상대 칩과 시각이 겹친다: peer={peer}, time={tm}"
        )

    def test_question_row_only_updates_the_tray(self, stack, page):
        """질문 줄은 그리지 않는다(`→ ask` 도구 줄이 있다). 트레이는 갱신된다 —
        어느 채널을 보고 있든 놓치지 않는 것이 트레이의 존재 이유다(§6)."""
        stack.emit_ready()
        stack.renderer.agent_roster(
            [
                {
                    "key": AGT,
                    "name": AGT,
                    "profile": "rev",
                    "state": "idle",
                    "open_questions": [
                        {
                            "id": "q-ab12",
                            "text": "제가 추가할까요, 지적만 할까요?",
                            "to": "user:bob",
                            "ts": 0,
                        },
                        {
                            "id": "q-cd34",
                            "text": "main 에게 묻는 것",
                            "to": "main",
                            "ts": 0,
                        },
                    ],
                }
            ]
        )
        page.goto(stack.url)
        assert _wait(lambda: _chip(page, AGT).count() > 0)

        stack.renderer.agent_message(
            key=AGT,
            direction="question",
            author=AGT,
            text="제가 추가할까요, 지적만 할까요?",
            to="main",
        )
        tray = page.locator("#ask-tray .ask-item")
        assert _wait(lambda: tray.count() > 0 and tray.first.is_visible())
        assert "제가 추가할까요" in tray.first.inner_text()
        assert tray.count() == 1  # 사람 주소인 것만
        assert "main 에게 묻는 것" not in page.locator("#ask-tray").inner_text()
        # 채널에는 아무 줄도 생기지 않았다.
        _chip(page, AGT).click()
        assert (
            page.locator("#messages > .card-queued, #messages > .card-run").count() == 0
        )

    def test_incoming_row_ignores_the_senders_task_id(self, stack, page):
        """수신 줄의 `task_id` 는 **보낸 쪽**의 런이다 — peer 워커 스레드의
        `_emit` 이 붙인다. 그걸로 배치하면 AGT 앞으로 온 줄이 PEER 의 블록
        몸통에 들어간다(패치 중 실제로 났던 버그). 자리는 언제나 자기 채널 루트."""
        self._open(stack, page, AGT, PEER)
        # PEER 의 런이 이 스레드에 열려 있다 → 이후 emit 은 PEER#1 을 단다.
        stack.renderer.begin_agent_work(key=PEER, seq=1, profile="doc", message="x")
        stack.renderer.agent_message(
            key=AGT, direction="in", author=f"agent:{PEER}", text="부탁", to=AGT, seq=5
        )
        queued = page.locator(f'#messages > .card-queued[data-ch="{AGT}"]')
        assert _wait(lambda: queued.count() == 1)
        assert (
            page.locator(f'.card-run[data-task-id="{PEER}#1"] .card-queued').count()
            == 0
        )
        stack.renderer.end_agent_work(key=PEER, seq=1, success=True, duration_s=0.1)

    def test_kill_then_resume_puts_heads_back_into_their_blocks(self, stack, page):
        """kill=정리 / resume=재생 — 정리는 **`agent_msg` 에서 나온 것만**.

        서버는 kill 때 버퍼에서 `agent_msg` 만 지우고 스코프·턴 이벤트는 남기며,
        resume 은 `conversation.jsonl`(수신·폴백 줄)만 재생한다. 초판은 kill 에
        블록 전체를 지워, 재생된 수신 줄이 붙을 블록이 없어 전부 `⏳ 대기` 로
        떨어졌다(사용자 보고 — 새로고침해야 나왔다)."""
        self._open(stack, page)
        stack.renderer.agent_message(
            key=AGT, direction="in", author="main", text="리뷰해줘", to=AGT, seq=1
        )
        self._begin(stack, seq=1)
        stack.renderer.final("검토 끝", turn=1)
        stack.renderer.end_agent_work(key=AGT, seq=1, success=True, duration_s=1.0)
        stack.renderer.agent_message(
            key=AGT, direction="in", author="main", text="다음 것", to=AGT, seq=2
        )
        blk = page.locator(f'#messages > .card-run[data-task-id="{AGT}#1"]')
        queued = page.locator(f'#messages > .card-queued[data-ch="{AGT}"]')
        assert _wait(lambda: blk.count() == 1 and queued.count() == 1)

        # kill: 수신 항목·대기 줄은 지워지고, 블록과 그 안의 작업은 남는다.
        stack.renderer.clear_agent_conversation(AGT)
        assert _wait(lambda: queued.count() == 0)
        assert _wait(lambda: blk.locator(".run-item").count() == 0)
        assert blk.count() == 1
        assert "검토 끝" in blk.inner_text()

        # resume: conversation.jsonl 재생 — 같은 seq 가 제 블록 머리로 돌아온다.
        stack.renderer.agent_message(
            key=AGT, direction="in", author="main", text="리뷰해줘", to=AGT, seq=1
        )
        stack.renderer.agent_message(
            key=AGT, direction="in", author="main", text="다음 것", to=AGT, seq=2
        )
        assert _wait(lambda: blk.locator(".run-item").count() == 1)
        assert "리뷰해줘" in blk.locator(".run-item").inner_text()
        # 런이 안 열린 seq=2 만 대기 — 이미 돈 seq=1 은 대기로 떨어지지 않는다.
        assert _wait(lambda: queued.count() == 1)
        assert "다음 것" in queued.inner_text()

    def test_duration_mark_does_not_overlap_the_final_text(self, stack, page):
        """꼬리의 `✓ (58.8s)` 는 최종답의 왼쪽 여백 안에 있다 — 본문 첫 글자를
        덮지 않는다(사용자 보고: 초판은 여백을 줄여 겹쳤다). 생각 줄이 위에
        있어도 마찬가지."""
        self._open(stack, page)
        self._begin(stack, seq=1)
        stack.renderer.thought("짧게 확인하고 답한다", 1)
        stack.renderer.final("B입니다. 준비 완료. 끝말잇기 시작 대기 중.", turn=1)
        stack.renderer.end_agent_work(key=AGT, seq=1, success=True, duration_s=58.8)
        fin = page.locator(f'.card-run[data-task-id="{AGT}#1"] .run-final .final')
        meta = fin.locator(".run-meta")
        assert _wait(lambda: meta.count() == 1)
        assert meta.inner_text().strip() == "✓ (58.8s)"
        mb = meta.bounding_box()
        # 본문 첫 글자의 위치 = .final 의 콘텐츠 시작(왼쪽 padding 뒤).
        text_left = page.evaluate(
            """(el) => {
                const r = document.createRange();
                const t = [...el.childNodes].find(n => n.nodeType === 3 && n.textContent.trim());
                r.setStart(t, 0); r.setEnd(t, 1);
                return r.getBoundingClientRect().left;
            }""",
            fin.element_handle(),
        )
        assert mb["x"] + mb["width"] <= text_left + 0.5, (
            f"✓ 표시가 본문을 덮는다: meta={mb}, text_left={text_left}"
        )
        # 생각 줄과도 겹치지 않는다(.final 기준 배치).
        think = page.locator(f'.card-run[data-task-id="{AGT}#1"] .run-final .row.think')
        tb = think.bounding_box()
        assert tb and mb["y"] >= tb["y"] + tb["height"] - 0.5


class TestAgentWake:
    """에이전트 메일이 깨운 런은 **사람 발화가 아니다** (v9.7.0, 사용자 제보).

    종전엔 `push_user_message` 로 흘러 오른쪽 파란 말풍선(`.card-user`)으로
    그려졌다 — `[🤝 agent]: New agent mail has arrived…` 가 사용자가 친 말과
    글자 하나 차이 없이 보였다. 서버는 이미 `author=""` 로 "귀속할 사용자가
    없다"를 표시하고 있었지만, 그 구분을 프론트로 나르는 길이 v9.4.0 ① 에서
    스윔레인과 함께 사라져 있었다."""

    WAKE = (
        "New agent mail has arrived. It is delivered as observation(s) at "
        "the start of this turn — review it and continue accordingly."
    )

    def test_wake_is_a_row_not_a_user_bubble(self, stack, page):
        stack.emit_ready()
        page.goto(stack.url)
        stack.renderer.agent_wake(self.WAKE)

        row = page.locator("#messages .row.wake")
        assert _wait(lambda: row.count() == 1)
        # ★ 회귀 가드: 말풍선으로 되돌아가면 즉시 실패한다.
        assert page.locator("#messages .card-user").count() == 0
        assert row.locator(".ic").inner_text().strip() == "🤝"
        assert row.locator(".k").inner_text().strip() == "메일"

    def test_summary_is_for_humans_body_is_what_the_model_got(self, stack, page):
        """요약은 사람용 한 줄, 펼치면 **모델이 실제로 받은 지시문**.

        그 영문 두 문장은 모델을 움직이는 장치지 사람이 읽을 문장이 아니다 —
        숨기지는 않되(투명성) 기본 화면을 먹지도 않게(간결) 한 줄 안에서
        분리한다."""
        stack.emit_ready()
        page.goto(stack.url)
        stack.renderer.agent_wake(self.WAKE)

        row = page.locator("#messages .row.wake")
        assert _wait(lambda: row.count() == 1)
        summary = row.locator(".s").inner_text()
        assert "에이전트 회신 도착" in summary
        assert "New agent mail" not in summary  # 원문은 요약 칸에 없다

        body = row.locator(".row-body")
        assert body.count() == 1
        assert not body.is_visible()  # 기본 접힘
        row.locator(".s").click()
        assert _wait(lambda: body.is_visible())
        assert "New agent mail has arrived" in body.inner_text()

    def test_wake_belongs_to_main_channel(self, stack, page):
        """깨우기는 main 의 런이 시작됐다는 기록이다 — 에이전트 채널이 아니다."""
        stack.emit_ready()
        _roster(stack, AGT)
        page.goto(stack.url)
        assert _wait(lambda: _chip(page, AGT).count() > 0)

        stack.renderer.agent_wake(self.WAKE)
        card = page.locator("#messages > .card-assistant")
        assert _wait(lambda: card.count() == 1)
        assert card.get_attribute("data-ch") == "main"
        _chip(page, AGT).click()
        assert _wait(lambda: not card.is_visible())


class TestJump:
    """점프는 **엿보기**다 — 한 방향만, 돌아가기는 한 단계(docs/chat-ui §5)."""

    def _delegate_call(self, stack, key):
        stack.renderer.action(
            "agent", f'{{"mode":"request","key":"{key}","message":"리뷰해줘"}}', turn=1
        )

    def test_main_to_agent_jumps_from_the_agent_tool_call(self, stack, page):
        """main 쪽에서 상주 에이전트에게 거는 일은 왕래 줄이 아니라 `⚡ agent`
        도구 호출로 나타난다 — 점프의 출발점이 거기여야 하는 이유."""
        stack.emit_ready()
        _roster(stack, AGT)
        page.goto(stack.url)
        assert _wait(lambda: _chip(page, AGT).count() > 0)

        self._delegate_call(stack, AGT)
        chip = page.locator("#messages > .card-assistant .row.act .peer.can-jump")
        assert _wait(lambda: chip.count() > 0)

        chip.click()
        assert _wait(lambda: _chip(page, AGT).get_attribute("aria-selected") == "true")

    def test_no_jump_chip_for_a_key_that_is_not_a_resident_agent(self, stack, page):
        """일회성 위임(`run`)의 대상은 채널이 아니라 이 카드 안의 중첩 블록이라
        갈 곳이 없다 — 칩 자체가 생기면 안 된다(눌러도 아무 일이 없는 버튼)."""
        stack.emit_ready()
        _roster(stack, AGT)
        page.goto(stack.url)
        assert _wait(lambda: _chip(page, AGT).count() > 0)

        self._delegate_call(stack, "agt-unknown")
        row = page.locator("#messages > .card-assistant .row.act")
        assert _wait(lambda: row.count() > 0)
        assert row.locator(".peer").count() == 0

    def test_highlight_does_not_replay_when_the_tab_is_reopened(self, stack, page):
        """번쩍임은 **한 번**이다.

        `.tv-nav-hl` 은 CSS 애니메이션이고, 채널 전환은 카드를 `hidden` 으로
        숨겼다 드러낸다. 숨겨졌던 요소가 다시 표시되면 CSS 애니메이션은
        **처음부터 다시 재생**된다 — 그래서 클래스를 안 떼면 그 탭을 열
        때마다 엉뚱하게 또 번쩍였다(사용자 보고)."""
        stack.emit_ready()
        _roster(stack, AGT)
        page.goto(stack.url)
        assert _wait(lambda: _chip(page, AGT).count() > 0)
        # 도착 채널에 카드 하나 — 앵커.
        stack.renderer.agent_message(
            key=AGT, direction="in", author="main", text="요청", to=AGT, seq=1
        )
        assert _wait(lambda: page.locator("#messages > .card-queued").count() == 1)

        self._delegate_call(stack, AGT)
        chip = page.locator("#messages > .card-assistant .row.act .peer.can-jump")
        assert _wait(lambda: chip.count() > 0)
        chip.click()

        target = page.locator(f'#messages > .card-queued[data-ch="{AGT}"]')
        assert _wait(lambda: "tv-nav-hl" in (target.get_attribute("class") or "")), (
            "사전 조건: 점프가 한 번은 번쩍여야 한다"
        )
        assert _wait(
            lambda: "tv-nav-hl" not in (target.get_attribute("class") or ""),
            timeout=6.0,
        ), "애니메이션이 끝나도 클래스가 남았다"

        _chip(page, "main").click()
        assert _wait(lambda: not target.is_visible())
        _chip(page, AGT).click()
        assert _wait(lambda: target.is_visible())
        assert "tv-nav-hl" not in (target.get_attribute("class") or ""), (
            "탭을 다시 열었더니 하이라이트가 되살아났다"
        )

    def test_user_sender_is_labelled_as_a_person(self, stack, page):
        """사람은 채널이 아니다 — 머리의 `(사람)` 꼬리표로 종류를 밝힌다."""
        stack.emit_ready()
        _roster(stack, AGT)
        page.goto(stack.url)
        assert _wait(lambda: _chip(page, AGT).count() > 0)
        _chip(page, AGT).click()

        stack.renderer.begin_agent_work(key=AGT, seq=1, profile="rev", message="x")
        stack.renderer.agent_message(
            key=AGT,
            direction="in",
            author="user:두정",
            text="README 갱신해줘",
            to=AGT,
            seq=1,
        )
        who = page.locator(f'.card-run[data-task-id="{AGT}#1"] .run-item .who')
        assert _wait(lambda: who.count() == 1)
        assert who.inner_text().strip() == "두정 (사람)"
        assert page.locator(f'.card-run[data-task-id="{AGT}#1"] .can-jump').count() == 0


class TestBackIsSingleLevel:
    """돌아가기 스택은 누를 때마다 라벨이 바뀌어 어디로 갈지 예측이 안 된다 —
    채널 칩이 항상 보이므로 그 복잡도를 살 이유가 없다(사용자 지적). 점프의
    출발점은 main 의 `⚡ agent` 도구 줄뿐이다(v9.23.0)."""

    def _setup(self, stack, page):
        stack.emit_ready()
        _roster(stack, AGT, PEER)
        page.goto(stack.url)
        assert _wait(lambda: _chip(page, PEER).count() > 0)

    def _jump_to(self, stack, page, key):
        # 턴 번호를 매번 올린다 — 같은 턴의 두 번째 op 는 앞 스텝 카드의 (접힌)
        # 본문에 행으로 들어가 칩이 보이지 않는다.
        self._turn = getattr(self, "_turn", 0) + 1
        chips = page.locator("#messages > .card-assistant .row.act .peer.can-jump")
        n = chips.count()  # 발신 **전에** 센다 — 뒤에 세면 이미 그려진 칩을 또 기다린다
        stack.renderer.action(
            "agent",
            f'{{"mode":"request","key":"{key}","message":"리뷰해줘"}}',
            turn=self._turn,
        )
        assert _wait(lambda: chips.count() == n + 1)
        chips.nth(n).click()
        assert _wait(lambda: _chip(page, key).get_attribute("aria-selected") == "true")

    def test_back_button_hidden_until_a_jump(self, stack, page):
        self._setup(stack, page)
        back = page.locator("#ch-back")
        assert not back.is_visible()
        _chip(page, AGT).click()  # 칩 클릭은 점프가 아니다 — 돌아갈 곳이 없다
        assert not back.is_visible()

    def test_back_is_one_level_and_vanishes_when_pressed(self, stack, page):
        """main → AGT 점프 → 돌아가기 → main → PEER 점프: 돌아갈 곳은 언제나
        **직전**이고, 한 번 누르면 사라진다."""
        self._setup(stack, page)
        back = page.locator("#ch-back")

        self._jump_to(stack, page, AGT)
        assert back.is_visible() and "main" in back.inner_text()
        back.click()
        assert _wait(
            lambda: _chip(page, "main").get_attribute("aria-selected") == "true"
        )
        assert _wait(lambda: not back.is_visible()), "돌아가기가 스택으로 쌓였다"

        self._jump_to(stack, page, PEER)
        assert back.is_visible() and "main" in back.inner_text()
        back.click()
        assert _wait(lambda: not back.is_visible())


class TestNestedBlocks:
    """skill·inline agent 는 끝나면 사라지므로 칩이 아니라 **중첩 블록**.
    상주 agent 는 채널 칩이 있으니 레일을 주면 중첩처럼 보여 거짓말이 된다."""

    def test_rail_class_per_scope_kind(self, stack, page):
        stack.emit_ready()
        _roster(stack, AGT)
        page.goto(stack.url)
        assert _wait(lambda: _chip(page, AGT).count() > 0)

        stack.renderer.begin_scope(task_id="sk1", kind="skill", label="plan", parent="")
        stack.renderer.begin_scope(task_id="in1", kind="run", label="조사", parent="")
        stack.renderer.begin_scope(
            task_id=f"{AGT}#1",
            kind="run",
            label="리뷰",
            agent=AGT,
            parent="",
            ctx_dir=f"agents/{AGT}",
        )
        assert _wait(lambda: page.locator("#messages > .card-task-group").count() == 2)

        def cls(tid):
            return page.locator(f'[data-task-id="{tid}"]').get_attribute("class")

        assert "scope-skill" in cls("sk1")
        assert "scope-inline" in cls("in1")
        # 상주 에이전트의 런은 접히는 스코프 카드가 아니라 **런 블록**이다
        # (v9.23.0) — 레일은 있되 카드 종류가 다르다.
        run = page.locator(f'[data-task-id="{AGT}#1"]')
        assert run.count() == 1
        assert "card-run" in (run.get_attribute("class") or "")
        assert "card-task-group" not in (run.get_attribute("class") or "")

    def test_nested_scope_collapses_inside_its_parent(self, stack, page):
        """중첩 접기: 부모를 접으면 자식 블록도 함께 사라진다 — 중첩이 표시만이
        아니라 **담김**이어야 한 번의 접기로 정리된다."""
        stack.emit_ready()
        page.goto(stack.url)
        stack.renderer.begin_scope(task_id="sk1", kind="skill", label="plan", parent="")
        assert _wait(lambda: page.locator('[data-task-id="sk1"]').count() > 0)
        stack.renderer.begin_scope(
            task_id="in1", kind="run", label="조사", parent="sk1"
        )
        child = page.locator('[data-task-id="in1"]')
        assert _wait(lambda: child.count() > 0)

        # 자식은 부모 body 안에 있다(형제가 아니라).
        assert (
            page.locator('[data-task-id="sk1"] .task-body [data-task-id="in1"]').count()
            == 1
        )

        parent_head = page.locator('[data-task-id="sk1"] > .task-header')
        parent_head.click()  # 펼침
        assert _wait(lambda: child.is_visible())
        parent_head.click()  # 접음
        assert _wait(lambda: not child.is_visible())


class TestAgentNameAndKey:
    """이름은 사람이 읽고, key 는 부를 때 쓴다 — 둘 다 보인다 (v9.9.0).

    종전엔 프로파일 없는 즉석 에이전트가 화면에 `agt-882cc142` 로만 나왔다.
    별명으로 폴백하되 **key 를 흐리게 곁에 둔다**: 보이는 것과 칠 수 있는 것이
    갈라지면 안 되기 때문이다(`@agt-<key>` 가 유일한 주소 — 별명을 주소로
    받기 시작하면 유일성·경로·rename 이 전부 따라온다. 자세한 근거는
    `agent_cli/agent_icon.py` 모듈 docstring).
    """

    def test_instant_agent_falls_back_to_a_nickname_beside_its_key(self, stack, page):
        stack.emit_ready()
        page.goto(stack.url)
        # 프로파일도 이름도 없는 즉석 에이전트 — 종전 raw key 가 나오던 경우.
        stack.renderer.agent_roster(
            [{"key": "agt-882cc142", "profile": "", "name": "", "state": "idle"}]
        )
        assert _wait(lambda: _chip(page, "agt-882cc142").count() > 0)

        chip = _chip(page, "agt-882cc142")
        assert "조용한 사자" in chip.inner_text()  # agent_nickname 과 동일
        key_el = chip.locator(".ag-key")
        assert key_el.inner_text().strip() == "agt-882cc142"

    def test_profile_wins_over_the_nickname(self, stack, page):
        """프로파일이 있으면 그게 별명보다 많은 것을 말한다 — 별명은 폴백."""
        stack.emit_ready()
        page.goto(stack.url)
        stack.renderer.agent_roster(
            [
                {
                    "key": "agt-c83d4f82",
                    "profile": "code-writer",
                    "name": "ui",
                    "state": "idle",
                }
            ]
        )
        assert _wait(lambda: _chip(page, "agt-c83d4f82").count() > 0)

        text = _chip(page, "agt-c83d4f82").inner_text()
        assert "code-writer · ui" in text
        assert "여우" not in text and "사자" not in text

    def test_the_key_is_dimmed_but_present(self, stack, page):
        """흐리게 — 이름을 가리면 안 되고, 안 보이면 주소로 못 쓴다."""
        stack.emit_ready()
        page.goto(stack.url)
        stack.renderer.agent_roster(
            [{"key": "agt-882cc142", "profile": "", "name": "", "state": "idle"}]
        )
        assert _wait(lambda: page.locator(".ag-key").count() > 0)

        key_el = page.locator(".ag-key").first
        assert key_el.is_visible()
        opacity = float(
            page.evaluate("getComputedStyle(document.querySelector('.ag-key')).opacity")
        )
        assert 0.2 < opacity < 0.8, f"흐림 정도가 의도 밖: {opacity}"

    def test_channel_bar_does_not_overflow_at_phone_width(self, stack, browser):
        """이름+key 는 칩을 길게 만든다 — 좁은 폭에서 접혀야 한다."""
        ctx = browser.new_context(viewport={"width": 400, "height": 720})
        page = ctx.new_page()
        try:
            stack.emit_ready()
            page.goto(stack.url)
            stack.renderer.agent_roster(
                [
                    {"key": "agt-882cc142", "profile": "", "name": "", "state": "idle"},
                    {
                        "key": "agt-c83d4f82",
                        "profile": "code-writer",
                        "name": "ui",
                        "state": "idle",
                    },
                    {
                        "key": "agt-9859a1e1",
                        "profile": "reviewer",
                        "name": "",
                        "state": "idle",
                    },
                ]
            )
            assert _wait(lambda: page.locator(".ov-ch").count() == 4)
            sw = page.evaluate("document.body.scrollWidth")
            cw = page.evaluate("document.body.clientWidth")
            assert sw <= cw, f"채널 바가 화면을 가로로 넓힘: {sw} > {cw}"
        finally:
            ctx.close()
