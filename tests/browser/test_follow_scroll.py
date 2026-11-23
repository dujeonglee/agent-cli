"""대화창 따라가기 — 세 규칙 (v9.26.0, 사용자가 시안으로 확인).

1. 처음 열면 맨 아래.
2. 바닥에서 48px 안으로 오면 그 자리에서 바닥에 붙고, 이후 새 내용을 따라간다.
3. 그보다 멀어지면 보던 줄이 움직이지 않고 "↓ 새 메시지 n" 이 쌓인다.

종전 코드는 근처에서 플래그만 바꿔 다음 이벤트가 와야 따라갔고, 버튼이
없었다. jsdom 으로는 스크롤 기하가 없어 실브라우저에서만 검증된다.
"""

from __future__ import annotations

import time

import pytest

pytestmark = pytest.mark.browser

DIST = (
    "() => { const m = document.getElementById('messages');"
    " return m.scrollHeight - m.scrollTop - m.clientHeight; }"
)


def _wait(cond, timeout=8.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if cond():
            return True
        time.sleep(0.05)
    return False


def _fill(stack, n, start=0):
    for i in range(start, start + n):
        stack.renderer.final(f"메인 응답 {i}\n" + ("본문 " * 40), turn=i)


def _dist(page):
    return page.evaluate(DIST)


def _cards(page):
    return page.locator("#messages > .card").count()


def _wheel(page, dy):
    """타임라인 위에서 진짜 휠 입력 — 음수는 위로. 따라가기는 사람이 굴렸을 때만 풀리고 붙는다: ``scrollTop`` 을
    대입하는 것은 프로그램이 쓴 위치라 사람의 굴림을 흉내 내지 못한다."""
    box = page.locator("#messages").bounding_box()
    page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
    page.mouse.wheel(0, dy)


class TestFollowScroll:
    def test_opens_at_the_bottom(self, stack, page):
        """규칙 1 — 스냅샷을 재생한 뒤 바닥에 있다. v10.8.1: 재생 중엔 타임라인이
        숨겨져 있고(`body.loading`), 공개되는 순간 이미 바닥이다 — 내려가는
        스크롤이 보이지 않는다."""
        stack.emit_ready()
        _fill(stack, 40)
        page.goto(stack.url)
        page.wait_for_function(
            "!document.body.classList.contains('loading')", timeout=8000
        )
        # 공개 직후 — 기다리지 않고 바로 — 바닥이고 카드는 다 있다
        assert _dist(page) < 2, f"공개 순간 바닥이 아니다: {_dist(page)}"
        assert _cards(page) >= 40
        assert (
            page.evaluate(
                "getComputedStyle(document.getElementById('messages')).visibility"
            )
            == "visible"
        )
        assert page.locator("#jump-new").is_hidden()

    def test_scrolled_up_holds_and_counts(self, stack, page):
        """규칙 3 — 위로 올린 상태에서 새 카드가 와도 위치는 그대로, 버튼에 개수."""
        stack.emit_ready()
        _fill(stack, 40)
        page.goto(stack.url)
        assert _wait(lambda: _cards(page) >= 40 and _dist(page) < 2)

        _wheel(page, -2000)
        # 규칙 3 판정은 scroll 이벤트(다음 렌더 단계)에서 — 그때까지 기다린다
        assert _wait(
            lambda: page.locator("#messages").get_attribute("data-pinned") == "0"
        )
        before = page.evaluate("document.getElementById('messages').scrollTop")

        _fill(stack, 3, start=40)
        assert _wait(lambda: _cards(page) >= 43)
        assert _wait(
            lambda: (
                page.locator("#jump-new").is_visible()
                and page.locator("#jump-new").inner_text() == "↓ 3 new"
            )
        ), (
            page.locator("#jump-new").inner_text(),
            page.locator("#messages").get_attribute("data-pinned"),
            _dist(page),
        )
        after = page.evaluate("document.getElementById('messages').scrollTop")
        assert after == before, f"위치 고정인데 움직였다: {before} → {after}"

        # 버튼 → 맨 아래, 버튼 사라짐, 다시 따라간다
        page.locator("#jump-new").click()
        assert _wait(lambda: _dist(page) < 2)
        assert _wait(lambda: page.locator("#jump-new").is_hidden())
        _fill(stack, 1, start=43)
        assert _wait(lambda: _cards(page) >= 44)
        assert _wait(lambda: _dist(page) < 2), "버튼으로 붙인 뒤 새 카드를 안 따라간다"

    def test_near_bottom_snaps_and_follows(self, stack, page):
        """규칙 2 — 48px 안으로 오면 그 자리에서 바닥에 붙고, 이후를 따라간다."""
        stack.emit_ready()
        _fill(stack, 40)
        page.goto(stack.url)
        assert _wait(lambda: _cards(page) >= 40 and _dist(page) < 2)

        _wheel(page, -2000)
        assert _wait(lambda: _dist(page) > 48)
        assert page.evaluate(
            "document.getElementById('messages').scrollHeight"
        ) > page.evaluate("document.getElementById('messages').clientHeight")

        # 바닥에서 20px 위 — "근처". 이벤트 없이도 80ms 뒤 바닥에 붙는다.
        _wheel(page, _dist(page) - 20)
        assert _wait(lambda: _dist(page) < 2), f"근처인데 안 붙는다: {_dist(page)}"
        assert page.locator("#jump-new").is_hidden()

        _fill(stack, 2, start=40)
        assert _wait(lambda: _cards(page) >= 42)
        assert _wait(lambda: _dist(page) < 2), "붙은 뒤 새 카드를 안 따라간다"

    def test_far_scroll_does_not_snap(self, stack, page):
        """규칙 3 의 경계 — 48px 보다 멀면 붙이지 않는다."""
        stack.emit_ready()
        _fill(stack, 40)
        page.goto(stack.url)
        assert _wait(lambda: _cards(page) >= 40 and _dist(page) < 2)
        _wheel(page, -120)
        time.sleep(0.4)
        d = _dist(page)
        assert 100 < d < 140, f"멀리 있는데 움직였다: {d}"


# ── 사람이 굴리지 않았는데 위치가 바뀔 때 ────────────────────

PINNED = "document.getElementById('messages').dataset.pinned"

# 프레임 순서를 페이지 안에서 세운다(시간에 기대지 않는다):
#   프레임 N   : 루트에 카드 하나 → 제품이 따라가기를 예약한다(다음 프레임의 rAF)
#   프레임 N+1 : 제품이 바닥으로 쓴 **뒤**(같은 프레임, 뒤에 등록한 rAF) 내용을 바꾼다
#   프레임 N+2 : 그 scroll 사건이 판정된다
# ``above`` px 을 화면 위쪽에 끼우면 브라우저의 스크롤 앵커링이 보던 줄을 붙잡으려고 scrollTop 을 옮긴다 —
# 런 도중 흡수된 요청이 블록 머리에 줄로 서는 것과 같다. ``below`` px 은 그 사이 아래에 붙은 새 내용이다.
MOVE_CONTENT_RIGHT_AFTER_THE_FOLLOW_WRITE = """([above, below]) => new Promise((resolve) => {
  const m = document.getElementById("messages");
  const block = (h) => {
    const d = document.createElement("div");
    d.className = "card";
    d.style.cssText = "flex: 0 0 auto; height:" + h + "px; min-height:" + h + "px;";
    return d;
  };
  requestAnimationFrame(() => {
    m.appendChild(block(30));
    Promise.resolve().then(() => requestAnimationFrame(() => {
      if (above) m.insertBefore(block(above), m.firstChild);
      if (below) m.appendChild(block(below));
      requestAnimationFrame(() => requestAnimationFrame(resolve));
    }));
  });
})"""


class TestContentMovesUnderAFollow:
    def _open_following(self, stack, page):
        stack.emit_ready()
        _fill(stack, 40)
        page.goto(stack.url)
        assert _wait(lambda: _cards(page) >= 40 and _dist(page) < 2)
        assert page.evaluate(PINNED) == "1"

    @pytest.mark.parametrize("above,below", [(0, 2000), (60, 0)])
    def test_growth_below_or_a_row_above_keeps_following(
        self, stack, page, above, below
    ):
        self._open_following(stack, page)
        page.evaluate(MOVE_CONTENT_RIGHT_AFTER_THE_FOLLOW_WRITE, [above, below])
        assert _wait(lambda: _dist(page) < 2 and page.evaluate(PINNED) == "1")

    def test_a_row_above_with_growth_below_keeps_following(self, stack, page):
        """화면 위쪽에 줄이 끼는 것과 아래의 새 내용이 겹쳐도 따라간다 — 브라우저가 옮긴 scrollTop 은 사람의 굴림이
        아니다. 종전엔 그 scroll 사건이 사람의 굴림으로 판정돼, 바닥까지 48px 넘게 남은 그 자리에서 풀렸다."""
        self._open_following(stack, page)
        page.evaluate(MOVE_CONTENT_RIGHT_AFTER_THE_FOLLOW_WRITE, [60, 2000])
        assert _wait(lambda: _dist(page) < 2 and page.evaluate(PINNED) == "1")

    def test_following_writes_once_per_change_and_then_rests(self, stack, page):
        """판정하지 않는 scroll 사건은 바닥으로 다시 쓰게 한다 — 그 쓰기가 낸 scroll 사건이 또 쓰기를 부르지는 않는다
        (이미 바닥이면 쓰지 않는다). 내용이 한 번 움직인 뒤의 scrollTop 쓰기 수를 센다."""
        self._open_following(stack, page)
        page.evaluate(
            """() => {
              const d = Object.getOwnPropertyDescriptor(Element.prototype, "scrollTop");
              window.__writes = 0;
              Object.defineProperty(document.getElementById("messages"), "scrollTop", {
                get() { return d.get.call(this); },
                set(v) { window.__writes++; d.set.call(this, v); },
                configurable: true,
              });
            }"""
        )
        page.evaluate(MOVE_CONTENT_RIGHT_AFTER_THE_FOLLOW_WRITE, [60, 2000])
        assert _wait(lambda: _dist(page) < 2 and page.evaluate(PINNED) == "1")
        settled = page.evaluate(
            """() => new Promise((resolve) => {
              const frames = (n, f) => (n ? requestAnimationFrame(() => frames(n - 1, f)) : f());
              const before = window.__writes;
              frames(10, () => resolve([before, window.__writes]));
            })"""
        )
        assert settled[0] == settled[1], settled  # 열 프레임 동안 더 쓰지 않는다
        assert settled[0] <= 4, settled  # 카드 하나 + 움직임 한 번에 쓰기 몇 번


class TestNewCountInsideARunBlock:
    AGT = "agt-rev"
    OTHER = "agt-doc"

    def _run_block(self, stack, key, cards):
        """그 에이전트 채널에 런 블록 하나를 열고 몸통에 카드를 붙인다 — 스코프는 열어 둔다."""
        stack.renderer.begin_scope(
            task_id=f"{key}#r-1",
            kind="run",
            label="task",
            agent=key,
            parent="",
            ctx_dir=f"agents/{key}",
        )
        for n in range(cards):
            stack.renderer.observation(f"step {n}", n, "shell")

    def test_cards_arriving_inside_the_block_are_counted(self, stack, page):
        """위로 올려 읽는 동안 보고 있는 채널에 붙은 카드를 센다 — 런 블록 몸통 안에 붙어도. 다른 채널의 블록에 붙은
        것은 세지 않는다. 종전엔 `#messages` 의 직계 자식만 세어, 에이전트 채널에서는 단추가 뜨지 않았다."""
        stack.emit_ready()
        stack.renderer.agent_roster(
            [
                {"key": k, "name": k, "profile": "reviewer", "state": "busy"}
                for k in (self.AGT, self.OTHER)
            ]
        )
        self._run_block(stack, self.AGT, 60)
        page.goto(stack.url)
        chip = page.locator(f'.ov-ch[data-key="{self.AGT}"]')
        assert _wait(lambda: chip.count() > 0)
        chip.click()
        body = page.locator(f'.card-run[data-task-id="{self.AGT}#r-1"] .run-body')
        assert _wait(lambda: body.locator("> .card").count() == 60 and _dist(page) < 2)

        _wheel(page, -400)
        assert _wait(lambda: page.evaluate(PINNED) == "0")
        for n in range(3):
            stack.renderer.observation(f"late {n}", 100 + n, "shell")
        assert _wait(lambda: body.locator("> .card").count() == 63)
        jump = page.locator("#jump-new")
        assert _wait(lambda: jump.is_visible() and jump.inner_text() == "↓ 3 new")
        # 다른 채널의 블록에 붙는 카드 — 지금 화면에 없다
        self._run_block(stack, self.OTHER, 2)
        other = page.locator(f'.card-run[data-task-id="{self.OTHER}#r-1"] .run-body')
        assert _wait(lambda: other.locator("> .card").count() == 2)
        assert jump.inner_text() == "↓ 3 new"
        jump.click()
        assert _wait(lambda: _dist(page) < 2 and jump.is_hidden())


class TestWhatCountsAsThePersonScrolling:
    """휠 말고 사람의 굴림으로 보는 것 둘 — 굴리는 키, 타임라인 위에서 포인터를 누른 채 옮겨진 위치(스크롤바 끌기·
    끌어 고르기). 프로그램이 쓴 위치는 사람의 굴림이 아니다."""

    def _open_following(self, stack, page):
        stack.emit_ready()
        _fill(stack, 40)
        page.goto(stack.url)
        assert _wait(lambda: _cards(page) >= 40 and _dist(page) < 2)

    def test_a_scroll_key_unpins_but_not_while_typing(self, stack, page):
        self._open_following(stack, page)
        page.locator("#input").click()
        page.keyboard.press(
            "PageUp"
        )  # 입력창 안 — 타임라인을 굴리는 키가 아니다(굴림을 시작하지 않는다)
        page.evaluate(MOVE_CONTENT_RIGHT_AFTER_THE_FOLLOW_WRITE, [60, 2000])
        assert _wait(lambda: _dist(page) < 2 and page.evaluate(PINNED) == "1")

        page.locator("#messages").click(
            position={"x": 5, "y": 5}
        )  # 초점을 타임라인 쪽으로
        page.keyboard.press("PageUp")
        assert _wait(lambda: page.evaluate(PINNED) == "0")
        assert _dist(page) > 48

    def test_a_position_moved_while_the_pointer_is_held_unpins(self, stack, page):
        """스크롤바 끌기가 페이지에 보이는 모양 — 타임라인 위 `pointerdown` 뒤 놓기 전에 위치가 옮겨진다. 헤드리스
        크롬은 스크롤바를 그리지 않아(폭 0) 엄지를 직접 끌 수 없다: 진짜 포인터를 누른 채 위치를 옮긴다."""
        self._open_following(stack, page)
        box = page.locator("#messages").bounding_box()
        page.mouse.move(box["x"] + 3, box["y"] + box["height"] - 3)
        page.mouse.down()
        page.evaluate("document.getElementById('messages').scrollTop -= 900")
        assert _wait(lambda: page.evaluate(PINNED) == "0")
        _fill(stack, 1, start=40)  # 누르고 있는 동안 온 카드 — 끌려가지 않는다
        assert _wait(lambda: _cards(page) >= 41)
        assert _dist(page) > 900
        page.mouse.up()

    def test_a_position_written_by_a_program_does_not_unpin(self, stack, page):
        """따라가는 중이면 그 scroll 사건이 바닥으로 다시 쓰게 한다 — 다른 내용이 오기를 기다리지 않는다: 사건이
        판정되는 프레임과 그다음 프레임(쓰기) 안에 바닥이다."""
        self._open_following(stack, page)
        frames = page.evaluate(
            """() => new Promise((resolve) => {
              const m = document.getElementById("messages");
              // 열 때 예약된 따라가기가 다 지나간 뒤에 옮긴다 — 그것이 대신 되돌려 주면 이 길을 보지 못한다
              const rest = (k, f) => (k ? requestAnimationFrame(() => rest(k - 1, f)) : f());
              rest(20, () => {
              m.scrollTop -= 900;
              let n = 0;
              const step = () => {
                n++;
                const back = m.scrollHeight - m.scrollTop - m.clientHeight < 2;
                if (back || n >= 30) resolve(back ? n : -1);
                else requestAnimationFrame(step);
              };
              requestAnimationFrame(step);
              });
            })"""
        )
        assert 0 < frames <= 3, frames
        assert page.evaluate(PINNED) == "1"

    def test_a_roll_ends_and_later_moves_are_not_the_persons(self, stack, page):
        """사람의 굴림은 끝난다(`scrollend`) — 바닥 근처에서 휠을 굴려 다시 붙은 뒤, 내용이 움직여 바뀐 위치는 사람의
        것이 아니다. 굴림이 끝나지 않은 것으로 남으면 그 위치가 사람의 굴림으로 판정돼 풀린다."""
        self._open_following(stack, page)
        _wheel(page, -20)  # 48px 안 — 다시 붙는다
        assert _wait(lambda: _dist(page) < 2 and page.evaluate(PINNED) == "1")
        page.evaluate(MOVE_CONTENT_RIGHT_AFTER_THE_FOLLOW_WRITE, [60, 2000])
        assert _wait(lambda: _dist(page) < 2 and page.evaluate(PINNED) == "1")


# 사람의 입력 뒤 굴림이 따라오지 않은 경우를 본다: 여섯 프레임을 쉰 뒤(입력을 기다리던 것이 지나가게), 앞선 따라가기
# 쓰기 없이 곧바로 브라우저가 위치를 옮기게 한다 — 화면 위쪽에 60px, 아래에 2,000px 을 한 프레임에.
MOVE_CONTENT_WITH_NO_WRITE_BEFORE = """() => new Promise((resolve) => {
  const m = document.getElementById("messages");
  const block = (h) => {
    const d = document.createElement("div");
    d.className = "card";
    d.style.cssText = "flex: 0 0 auto; height:" + h + "px; min-height:" + h + "px;";
    return d;
  };
  const frames = (n, f) => (n ? requestAnimationFrame(() => frames(n - 1, f)) : f());
  frames(6, () => {
    m.insertBefore(block(60), m.firstChild);
    m.appendChild(block(2000));
    frames(3, resolve);
  });
})"""


class TestAnInputThatScrollsNothing:
    """사람의 입력이 타임라인을 굴리지 않았으면 "굴리는 중" 이 아니다 — 그 뒤 브라우저가 옮긴 위치는 사람의 굴림으로
    판정되지 않는다. 입력은 그 굴림의 첫 scroll 사건을 몇 프레임 기다릴 뿐이다(굴림이 오지 않으면 사라진다)."""

    def _open_following(self, stack, page):
        stack.emit_ready()
        _fill(stack, 40)
        page.goto(stack.url)
        assert _wait(lambda: _cards(page) >= 40 and _dist(page) < 2)

    def _content_moves(self, page):
        page.evaluate(MOVE_CONTENT_WITH_NO_WRITE_BEFORE)

    def test_no_input_is_the_control(self, stack, page):
        self._open_following(stack, page)
        self._content_moves(page)
        assert _wait(lambda: _dist(page) < 2 and page.evaluate(PINNED) == "1")

    def test_a_wheel_over_an_inner_scroller(self, stack, page):
        """카드 안의 따로 굴러가는 상자(코드 블록의 모양) 위에서 휠 — `wheel` 은 타임라인까지 올라오지만 타임라인은
        움직이지 않는다."""
        self._open_following(stack, page)
        page.evaluate(
            """() => {
              const box = document.createElement("pre");
              box.id = "inner";
              box.style.cssText = "height:80px; overflow:auto;";
              box.textContent = Array(60).fill("line").join("\\n");
              document.getElementById("messages").lastElementChild.appendChild(box);
            }"""
        )
        assert _wait(lambda: _dist(page) < 2)
        r = page.locator("#inner").bounding_box()
        page.mouse.move(r["x"] + 20, r["y"] + 40)
        page.mouse.wheel(0, 40)
        assert _wait(
            lambda: page.evaluate("document.getElementById('inner').scrollTop") == 40
        )
        self._content_moves(page)
        assert _wait(lambda: _dist(page) < 2 and page.evaluate(PINNED) == "1")

    def test_a_scroll_key_on_a_focused_button(self, stack, page):
        self._open_following(stack, page)
        page.evaluate(
            """() => {
              const all = document.querySelectorAll("#messages .card-copy");
              all[all.length - 1].focus({ preventScroll: true });
            }"""
        )
        page.keyboard.press("ArrowDown")
        self._content_moves(page)
        assert _wait(lambda: _dist(page) < 2 and page.evaluate(PINNED) == "1")

    def test_end_at_the_bottom(self, stack, page):
        self._open_following(stack, page)
        page.locator("#messages").click(position={"x": 5, "y": 5})
        page.keyboard.press("End")
        self._content_moves(page)
        assert _wait(lambda: _dist(page) < 2 and page.evaluate(PINNED) == "1")


class TestTabFocus:
    """Tab(·Shift+Tab)으로 초점을 옮기면 브라우저가 그 요소를 화면에 들인다 — 사람이 옮긴 것이다."""

    def _open_following(self, stack, page):
        stack.emit_ready()
        _fill(stack, 40)
        page.goto(stack.url)
        assert _wait(lambda: _cards(page) >= 40 and _dist(page) < 2)

    def test_tab_to_an_element_far_above_is_pulled_back(self, stack, page):
        """[Tab 에서 바뀜] 따라가는 중 Tab 으로 맨 위 카드의 단추에 초점이 가면, 초점은 가는데 화면은 바닥으로
        되돌아온다 — Tab 이 굴리는 키로 세어지지 않아서다."""
        self._open_following(stack, page)
        page.evaluate(
            """() => {
              const m = document.getElementById("messages");
              m.setAttribute("tabindex", "-1");
              m.focus({ preventScroll: true });
            }"""
        )
        page.keyboard.press("Tab")
        focused = "document.activeElement.closest('#messages > .card') === document.querySelector('#messages > .card')"
        assert _wait(lambda: page.evaluate(focused))
        _fill(stack, 1, start=40)
        assert _wait(lambda: _cards(page) >= 41 and _dist(page) < 2)
        assert page.evaluate(PINNED) == "1"
