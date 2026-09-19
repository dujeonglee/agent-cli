"""채널 필터 · 왕래 줄 · 점프 · 중첩 블록 — v9.4.0 ⑥ (docs/chat-ui §3·§5).

⑥ 이전까지 채널 칩은 **입력 라우팅만** 바꿨다(보이는 것은 그대로). ⑥ 에서
채널이 타임라인의 필터가 되고, `agent_msg` 가 왕래 줄로 그려지며, 왕래 줄의
상대 이름이 점프 버튼을 겸한다. 여기서 고정하는 계약:

1. **채널 = 필터** — `#messages` 직계 자식의 `data-ch` 로 걸러진다. `data-ch`
   가 없는 노드(생성 중 한 줄)는 어느 채널에서나 보인다.
2. **점프는 한 방향** — main→agent(⚡ agent 도구 호출), peer↔peer(왕래 줄)는
   가능. **agent→main 은 불가** — main 쪽 대응 줄이 도구 호출이라 매칭 키가
   달라 이번 범위 밖이고, 눌리는 것처럼 보이면 고장으로 읽힌다.
3. **돌아가기는 한 단계** — 두 번 점프해도 스택이 쌓이지 않는다(사용자 지적으로
   스택→단일 변경). 누르면 사라진다.
4. **중첩 블록** — skill/inline agent 는 좌측 레일, 상주 agent 는 레일 없음
   (채널 칩이 이미 있어 레일을 주면 중첩처럼 보여 거짓말이 된다).

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

    def test_agent_work_lands_flat_in_its_channel_without_a_wrapper_card(
        self, stack, page
    ):
        """상주 에이전트의 작업은 **카드로 감싸지 않고** 그 채널에 평평하게
        흐른다 (v9.7.0, 사용자 지적).

        ⑥ 이전엔 🦀 스코프 카드가 에이전트 작업을 묶는 유일한 수단이었다.
        채널이 필터가 된 뒤로는 채널이 그 일을 하므로, 자기 채널 안에서 자기를
        또 묶으면 아무 정보도 더하지 않으면서 **기본 접힘 뒤로 투명성을 숨기고**
        (⑥의 목적과 정반대) main 창은 평평한데 agent 창만 한 겹 들어가
        "같은 컨셉으로 통일"이 깨진다. 요청 경계는 왕래 줄이 이미 긋는다."""
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

        # 래퍼 카드는 생기지 않는다.
        assert _wait(lambda: page.locator("#messages > .card-assistant").count() > 0)
        assert page.locator(f'[data-task-id="{AGT}#1"]').count() == 0

        # 내부 턴이 채널 귀속을 그대로 물려받아 main 에서는 숨는다.
        card = page.locator("#messages > .card-assistant").first
        assert card.get_attribute("data-ch") == AGT
        assert not card.is_visible()
        _chip(page, AGT).click()
        assert _wait(lambda: card.is_visible())

    def test_nested_scope_inside_an_agent_keeps_its_own_card(self, stack, page):
        """에이전트 안에서 열린 skill/inline 은 **여전히 자기 카드**를 갖는다 —
        래퍼를 없앤 것이 중첩까지 없앤 것은 아니다. 부모 그룹이 없으니
        `channelOf(parent)` 로 그 에이전트 채널의 루트에 붙는다."""
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
        sk = page.locator('[data-task-id="sk-in"]')
        assert _wait(lambda: sk.count() == 1)
        assert "scope-skill" in (sk.get_attribute("class") or "")
        assert sk.get_attribute("data-ch") == AGT
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

    def test_node_without_data_ch_stays_visible_in_every_channel(self, stack, page):
        """생성 중 한 줄처럼 **채널과 무관한** 표시는 `data-ch` 를 달지 않고,
        필터는 그런 노드를 손대지 않는다. (필터가 무조건 숨기면 에이전트
        채널에서 생성 중 표시가 사라져 '멈춘 것처럼' 보인다.)"""
        stack.emit_ready()
        _roster(stack, AGT)
        page.goto(stack.url)
        assert _wait(lambda: _chip(page, AGT).count() > 0)

        stack.renderer.stream_chunk("본문 " * 60)  # → stream_tick → 생성 중 한 줄
        gen = page.locator("#messages > .gen")
        assert _wait(lambda: gen.count() > 0 and gen.is_visible())
        assert gen.get_attribute("data-ch") is None

        _chip(page, AGT).click()
        assert gen.is_visible(), "생성 중 표시가 채널 전환에 숨었다"

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

    def test_rejected_emission_stays_in_its_own_channel(self, stack, page):
        """거부된 응답(`⚠ no action`)은 **그걸 낸 주체**의 것이다.

        실사고(사용자 보고): 에이전트에게 물은 답이 거부됐는데 그 빨간 박스가
        **main 대화**에 떴다. `failed_turn` 은 `_emit` 이 스코프 task_id 를 이미
        실어 보내는데 리스너가 그걸 흘리고 `appendToTimeline(card)` 로 붙여,
        채널 없는 루트 카드가 된 탓. `renderUserMessage` 누수와 같은 부류라
        **루트에 붙는 경로마다** 이 가드가 필요하다."""
        stack.emit_ready()
        _roster(stack, AGT)
        page.goto(stack.url)
        assert _wait(lambda: _chip(page, AGT).count() > 0)

        # 에이전트 스코프 안에서 난 실패 — 렌더러가 스레드→task_id 를 붙인다.
        stack.renderer.begin_scope(
            task_id=f"{AGT}#1",
            kind="run",
            label="3+3",
            agent=AGT,
            parent="",
            ctx_dir=f"agents/{AGT}",
        )
        stack.renderer.recovery("6입니다.", "형식을 지켜 다시", "no action", 1)
        card = page.locator("#messages .card-failed")
        assert _wait(lambda: card.count() > 0)

        # 에이전트 작업 카드 **안**(중첩)이거나, 최소한 main 채널은 아니어야 한다.
        inside = page.locator(f'[data-task-id="{AGT}#1"] .card-failed').count()
        assert inside == 1 or card.first.get_attribute("data-ch") == AGT, (
            "거부된 응답이 낸 주체를 잃었다 — main 대화에 뜬다"
        )
        _chip(page, "main").click()
        assert _wait(lambda: not card.first.is_visible()), "main 에서 보인다"

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


class TestTrafficRow:
    """왕래(가로로 건너는 것)는 방향과 상대를 싣는다 — 내부 작업과 같은 리듬."""

    def test_incoming_and_outgoing_rows_carry_direction_and_peer(self, stack, page):
        stack.emit_ready()
        _roster(stack, AGT)
        page.goto(stack.url)
        assert _wait(lambda: _chip(page, AGT).count() > 0)
        _chip(page, AGT).click()

        stack.renderer.agent_message(
            key=AGT, direction="in", author="main", text="방금 커밋 리뷰해줘", to=AGT
        )
        stack.renderer.agent_message(
            key=AGT,
            direction="out",
            author=AGT,
            text="폴백에 테스트가 없습니다",
            to="main",
        )
        rows = page.locator("#messages > .card-msg .row.msg")
        assert _wait(lambda: rows.count() == 2)

        assert rows.nth(0).locator(".ic").inner_text().strip() == "←"
        assert rows.nth(0).locator(".k").inner_text().strip() == "받음"
        assert "방금 커밋 리뷰해줘" in rows.nth(0).locator(".s").inner_text()
        assert rows.nth(1).locator(".ic").inner_text().strip() == "→"
        assert rows.nth(1).locator(".k").inner_text().strip() == "보냄"
        # 상대는 양쪽 다 main — 마크업이 문자로 새지 않는다(③ 실사고와 동형).
        for i in (0, 1):
            peer = rows.nth(i).locator(".peer")
            assert "main" in peer.inner_text()
            assert "<span" not in peer.inner_text()

    def test_timestamp_does_not_overlap_the_peer_chip(self, stack, page):
        """시각과 상대 이름이 **겹쳐 그려지지 않는다** (사용자 지적).

        카드 우상단 시각 배지는 absolute 라 그 아래 줄의 오른쪽 끝(상대 칩)을
        덮었다. 여백을 상수로 비워두는 첫 수정은 배지 폭 추정에 기대 위태로웠고,
        지금은 꼬리 칸이 있는 줄이면 **시각을 같은 그리드 안**에 넣는다 —
        겹칠 자리가 원천적으로 없다. 기하로 봐야 잡히는 부류라 여기서 고정한다."""
        stack.emit_ready()
        _roster(stack, AGT)
        page.goto(stack.url)
        assert _wait(lambda: _chip(page, AGT).count() > 0)
        _chip(page, AGT).click()

        stack.renderer.agent_message(
            key=AGT,
            direction="out",
            author=AGT,
            text="폴백에 테스트가 없습니다",
            to="main",
        )
        row = page.locator("#messages > .card-msg .row.msg")
        assert _wait(lambda: row.count() == 1)

        # 시각은 코너 배지가 아니라 줄 안에 있다.
        assert page.locator("#messages > .card-msg .card-time").count() == 0
        peer = row.locator(".peer").bounding_box()
        tm = row.locator(".row-time").bounding_box()
        assert peer and tm
        assert peer["x"] + peer["width"] <= tm["x"] + 0.5, (
            f"상대 칩과 시각이 겹친다: peer={peer}, time={tm}"
        )

    def test_reply_repeating_the_final_becomes_a_receipt(self, stack, page):
        """`→ 보냄` 이 바로 위 최종답을 그대로 반복하면 **영수증 한 줄**로만
        (v9.7.0, 사용자 지적: 같은 내용이 화면에 두 번)."""
        stack.emit_ready()
        _roster(stack, AGT)
        page.goto(stack.url)
        assert _wait(lambda: _chip(page, AGT).count() > 0)
        _chip(page, AGT).click()

        answer = "기차. 어릴 때 기차 여행하던 기억이 나네요!"
        stack.renderer.begin_scope(
            task_id=f"{AGT}#1",
            kind="run",
            label="한 수",
            agent=AGT,
            parent="",
            ctx_dir=f"agents/{AGT}",
        )
        stack.renderer.final(answer, turn=1)
        stack.renderer.agent_message(
            key=AGT, direction="out", author=AGT, text=answer, to="main"
        )
        row = page.locator("#messages .card-msg .row")
        assert _wait(lambda: row.count() == 1)
        assert "receipt" in (row.get_attribute("class") or "")
        assert row.locator(".s").inner_text().strip() == "회신했습니다"
        assert row.locator(".row-body").count() == 0
        # 화면 전체에서 답은 **한 번만** 나온다.
        body = page.locator("#messages").inner_text()
        assert body.count(answer) == 1, "같은 답이 두 번 보인다"

    def test_reply_without_a_preceding_final_keeps_its_content(self, stack, page):
        """kill→resume 재생은 `agent_message` 만 다시 내보낸다(최종답은 안
        온다) — 그때 이 줄은 그 답의 **유일한 기록**이므로 내용을 그대로
        싣는다. 무조건 영수증으로 접으면 대화 기록이 사라진다."""
        stack.emit_ready()
        _roster(stack, AGT)
        page.goto(stack.url)
        assert _wait(lambda: _chip(page, AGT).count() > 0)
        _chip(page, AGT).click()

        answer = "리플레이로만 남은 회신 본문입니다."
        stack.renderer.agent_message(
            key=AGT, direction="out", author=AGT, text=answer, to="main"
        )
        row = page.locator("#messages .card-msg .row")
        assert _wait(lambda: row.count() == 1)
        assert "receipt" not in (row.get_attribute("class") or "")
        assert answer in row.locator(".s").inner_text()

    def test_question_row_also_lands_in_the_global_ask_tray(self, stack, page):
        """질문은 대화에도 남고 트레이에도 뜬다 — 어느 채널을 보고 있든 놓치지
        않는 것이 트레이의 존재 이유다(docs/chat-ui §6)."""
        stack.emit_ready()
        stack.renderer.agent_roster(
            [{"key": AGT, "name": AGT, "profile": "rev", "state": "waiting_ask"}]
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
        # main 채널을 보고 있는데도 트레이에 뜬다.
        tray = page.locator("#ask-tray .ask-item")
        assert _wait(lambda: tray.count() > 0 and tray.first.is_visible())
        assert "제가 추가할까요" in tray.first.inner_text()

        # 대화 쪽 줄은 그 에이전트 채널에 있다.
        _chip(page, AGT).click()
        row = page.locator("#messages > .card-msg .row.msg")
        assert _wait(lambda: row.count() == 1 and row.is_visible())
        assert row.locator(".ic").inner_text().strip() == "❓"
        assert row.locator(".k").inner_text().strip() == "질문"

    def test_kill_clears_that_channels_traffic_rows(self, stack, page):
        """kill=정리 / resume=재생 대칭 — 안 지우면 부활 시 같은 대화를 두 번
        그린다. 왕래가 DOM 카드가 됐으므로 비우는 곳도 DOM 이다."""
        stack.emit_ready()
        _roster(stack, AGT)
        page.goto(stack.url)
        assert _wait(lambda: _chip(page, AGT).count() > 0)
        _chip(page, AGT).click()

        stack.renderer.agent_message(
            key=AGT, direction="in", author="main", text="리뷰해줘", to=AGT
        )
        rows = page.locator("#messages > .card-msg")
        assert _wait(lambda: rows.count() == 1)

        stack.renderer.clear_agent_conversation(AGT)
        assert _wait(lambda: rows.count() == 0)


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

    def test_peer_to_peer_jump_switches_channel_and_highlights(self, stack, page):
        """peer↔peer 는 양쪽에 대응 줄이 있어 점프가 성립한다. 도착지에서는
        **그 상대와 주고받은 줄**을 하이라이트한다 — 채널만 바뀌고 어디를 봐야
        할지 모르면 점프가 아니라 그냥 탭 전환이다."""
        stack.emit_ready()
        _roster(stack, AGT, PEER)
        page.goto(stack.url)
        assert _wait(lambda: _chip(page, PEER).count() > 0)

        # AGT 채널: PEER 가 보낸 요청. PEER 채널: AGT 가 보낸 요청.
        stack.renderer.agent_message(
            key=AGT,
            direction="in",
            author=f"agent:{PEER}",
            text="전송 세대 설명도 넣어주세요",
            to=AGT,
        )
        stack.renderer.agent_message(
            key=PEER,
            direction="in",
            author=f"agent:{AGT}",
            text="문서 초안 부탁해요",
            to=PEER,
        )
        assert _wait(lambda: page.locator("#messages > .card-msg").count() == 2)

        _chip(page, AGT).click()
        chip = _visible_rows(page, "#messages > .card-msg").locator(".peer.can-jump")
        assert _wait(lambda: chip.count() == 1)
        chip.click()

        assert _wait(lambda: _chip(page, PEER).get_attribute("aria-selected") == "true")
        target = page.locator(f'#messages > .card-msg[data-peer="{AGT}"]')
        assert _wait(lambda: target.is_visible())
        assert "tv-nav-hl" in (target.get_attribute("class") or ""), (
            "도착지 하이라이트 없음"
        )

    def test_agent_to_main_is_not_clickable(self, stack, page):
        """agent→main 은 매칭 키가 달라 이번 범위 밖 — 상대 칩은 **보이되
        눌리지 않는다**. 돌아가기 버튼이 그 자리를 채운다."""
        stack.emit_ready()
        _roster(stack, AGT)
        page.goto(stack.url)
        assert _wait(lambda: _chip(page, AGT).count() > 0)
        _chip(page, AGT).click()

        stack.renderer.agent_message(
            key=AGT, direction="out", author=AGT, text="끝냈습니다", to="main"
        )
        peer = page.locator("#messages > .card-msg .peer")
        assert _wait(lambda: peer.count() == 1)
        assert "main" in peer.inner_text()
        assert "can-jump" not in (peer.get_attribute("class") or "")

    def test_user_sender_is_labelled_and_not_a_jump_target(self, stack, page):
        """사람은 채널이 아니다 — `(사람)` 꼬리표로 종류를 밝히되 점프는 없다."""
        stack.emit_ready()
        _roster(stack, AGT)
        page.goto(stack.url)
        assert _wait(lambda: _chip(page, AGT).count() > 0)
        _chip(page, AGT).click()

        stack.renderer.agent_message(
            key=AGT, direction="in", author="user:두정", text="README 갱신해줘", to=AGT
        )
        peer = page.locator("#messages > .card-msg .peer")
        assert _wait(lambda: peer.count() == 1)
        assert peer.inner_text().strip() == "두정 (사람)"
        assert "can-jump" not in (peer.get_attribute("class") or "")


class TestBackIsSingleLevel:
    """돌아가기 스택은 누를 때마다 라벨이 바뀌어 어디로 갈지 예측이 안 된다 —
    채널 칩이 항상 보이므로 그 복잡도를 살 이유가 없다(사용자 지적)."""

    def _two_hops(self, stack, page):
        stack.emit_ready()
        _roster(stack, AGT, PEER)
        page.goto(stack.url)
        assert _wait(lambda: _chip(page, PEER).count() > 0)
        stack.renderer.agent_message(
            key=AGT, direction="in", author=f"agent:{PEER}", text="A", to=AGT
        )
        stack.renderer.agent_message(
            key=PEER, direction="in", author=f"agent:{AGT}", text="B", to=PEER
        )
        assert _wait(lambda: page.locator("#messages > .card-msg").count() == 2)

    def test_back_button_hidden_until_a_jump(self, stack, page):
        self._two_hops(stack, page)
        back = page.locator("#ch-back")
        assert not back.is_visible()
        _chip(page, AGT).click()  # 칩 클릭은 점프가 아니다 — 돌아갈 곳이 없다
        assert not back.is_visible()

    def test_second_jump_replaces_the_first_instead_of_stacking(self, stack, page):
        """main → AGT → PEER 로 두 번 점프해도 돌아가기는 **한 번**이고, 그
        목적지는 **직전**(AGT)이다. 스택이면 두 번 눌러야 사라진다."""
        self._two_hops(stack, page)
        back = page.locator("#ch-back")

        _chip(page, AGT).click()
        page.locator("#messages > .card-msg:not([hidden]) .peer.can-jump").click()
        assert _wait(lambda: _chip(page, PEER).get_attribute("aria-selected") == "true")
        assert back.is_visible()
        assert AGT in back.inner_text(), (
            f"돌아갈 곳이 직전이 아님: {back.inner_text()!r}"
        )

        back.click()
        assert _wait(lambda: _chip(page, AGT).get_attribute("aria-selected") == "true")
        # 한 번 누르면 사라진다 — 스택이면 여기서 또 보인다.
        assert _wait(lambda: not back.is_visible()), "돌아가기가 스택으로 쌓였다"


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
        # v9.7.0: 상주 에이전트는 **카드 자체가 없다** — 채널이 그 역할이다.
        assert page.locator(f'[data-task-id="{AGT}#1"]').count() == 0

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
