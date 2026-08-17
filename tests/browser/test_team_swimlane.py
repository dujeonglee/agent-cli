"""Team swimlane (Phase 3) — real-browser e2e.

The swimlane is SVG driven by the live SSE stream (agent_roster → lanes,
agent_msg in→out → work spans + message connectors, scope_start kind=skill →
skill band). jsdom can't render/measure SVG, so a real browser is the only
guard. Events are injected through the REAL WebRenderer (same path production
uses); the page derives the model (team_model.js) and paints (team_view.js).
"""

from __future__ import annotations

import time


def _wait(cond, timeout=8.0, step=0.05):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if cond():
            return True
        time.sleep(step)
    return False


def _show_flow(page):
    """v8.12.0 부터 기본 뷰가 **개요**라 스윔레인(`#team-view`)과 응답 독(`#dock`)은
    숨겨진다. 이 파일의 테스트는 모두 스윔레인/독을 검증하므로, 로드 직후 **흐름** 탭으로
    전환해 이들을 노출시킨다(레벨 컨트롤은 항상 헤더에 있어 활동 전에도 클릭 가능)."""
    page.wait_for_selector("#vt-flow", timeout=8000)
    page.click("#vt-flow")


def _drive_team(stack):
    """Emit a minimal team run through the real renderer: an enclosing skill
    band, a two-agent roster, and one request→reply that becomes w1's work
    span + a peer-message connector."""
    r = stack.renderer
    r.begin_scope(task_id="orch-sk", kind="skill", label="orchestrate")
    r.agent_roster(
        [
            {"key": "orch", "profile": "orchestrator", "name": "orch", "state": "busy"},
            {"key": "w1", "profile": "code-writer", "name": "w1", "state": "idle"},
        ]
    )
    # w1 processes a request → begin_agent_work scope = a work span in w1's
    # OWN lane (not a one-shot under main).
    r.begin_agent_work(key="w1", seq=0, profile="code-writer", message="implement")
    time.sleep(0.05)
    r.end_agent_work(key="w1", seq=0, success=True, duration_s=0.05)
    # a peer message orch→w1 draws a connector.
    r.agent_message(key="w1", direction="out", author="orch", text="go", seq=1, to="w1")


class TestTeamSwimlane:
    def test_toggle_reveals_and_swimlane_renders(self, stack, page):
        page.goto(stack.url)
        stack.emit_ready()
        _show_flow(page)
        _drive_team(stack)

        # The swimlane pane + collapse toggle appear once team activity arrives
        # (side-by-side with the timeline — no view switch needed).
        page.wait_for_selector("#view-toggle:not([hidden])", timeout=8000)
        page.wait_for_selector("#team-view .tv-svg", timeout=8000)

        # Lanes: main + orch + w1 → at least 3 lane chips, and w1/orch labelled.
        assert _wait(lambda: page.locator(".tv-chip").count() >= 3)
        assert page.locator(".tv-lane-nm", has_text="orch").count() >= 1
        assert page.locator(".tv-lane-nm", has_text="w1").count() >= 1

        # The enclosing skill blocks main → a colored span in main's lane.
        assert _wait(lambda: page.locator(".tv-scope-skill").count() >= 1)
        # w1's request→reply becomes a work span.
        assert _wait(lambda: page.locator(".tv-span").count() >= 1)
        # The reply is a message connector between lanes.
        assert _wait(lambda: page.locator(".tv-msg").count() >= 1)

    def test_flow_surface_and_drawer_toggles(self, stack, page):
        """흐름(팀 스윔레인)은 full-width base 표면(v8.2.0 반전; v8.12.0 부터 기본은
        개요라 _show_flow 로 전환)이고, 타임라인은 전문 드로어(▤ / #vt-detail-toggle 로
        열고 ✕ 로 닫음)로 그 위에 오버레이된다."""
        page.goto(stack.url)
        stack.emit_ready()
        _show_flow(page)
        _drive_team(stack)

        team = page.locator("#team-view")
        drawer = page.locator("#timeline-drawer")
        assert _wait(lambda: team.is_visible())
        # Drawer starts closed (slid off-screen; aria mirrors it).
        assert drawer.get_attribute("aria-hidden") == "true"
        assert "open" not in (drawer.get_attribute("class") or "")
        # ▤ opens it…
        page.click("#vt-detail-toggle")
        assert _wait(lambda: "open" in (drawer.get_attribute("class") or ""))
        assert drawer.get_attribute("aria-hidden") == "false"
        # …the team view stays visible behind (overlay, not a swap)…
        assert team.is_visible()
        # …and ✕ closes it again.
        page.click("#td-close")
        assert _wait(lambda: "open" not in (drawer.get_attribute("class") or ""))

    def test_reconnect_replay_no_flash_empty_or_duplicate(self, stack, page):
        """Reconnect replays the persistent buffer (roster sticky + scope_* +
        agent_msg). ingest dedups replayed events, so the view neither flashes
        the empty state nor draws duplicate spans — the fix for 'resets to No
        team activity on every event' (the old reset()-on-ready flush)."""
        page.goto(stack.url)
        stack.emit_ready()
        _show_flow(page)
        _drive_team(stack)
        page.wait_for_selector("#view-toggle:not([hidden])", timeout=8000)
        page.wait_for_selector(".tv-scope-skill", timeout=8000)
        n_skill = page.locator(".tv-scope-skill").count()

        # Simulate the reconnect replay re-delivering the same persistent events.
        page.evaluate(
            "() => { window.TeamView.ingest('scope_start',"
            "{task_id:'orch-sk', kind:'skill', label:'orchestrate'});"
            "window.TeamView.ingest('agent_msg',"
            "{key:'w1', direction:'out', author:'w1', to:'orch', text:'done', seq:2}); }"
        )
        # Never empty, and no duplicate skill span.
        assert not page.locator(".tv-empty").is_visible()
        assert page.locator(".tv-scope-skill").count() == n_skill

    def test_hover_shows_custom_tooltip(self, stack, page):
        """Bars/connectors show their label via a custom fast tooltip (native
        <title> was too slow). Hovering the skill span reveals its text."""
        page.goto(stack.url)
        stack.emit_ready()
        _show_flow(page)
        _drive_team(stack)
        page.wait_for_selector("#view-toggle:not([hidden])", timeout=8000)
        page.wait_for_selector(".tv-scope-skill", timeout=8000)
        page.locator(".tv-scope-skill").first.hover()
        page.wait_for_selector(".tv-tip:not([hidden])", timeout=3000)
        assert "orchestrate" in page.locator(".tv-tip").inner_text()

    def test_click_bar_pins_panel_then_escalates_to_timeline(self, stack, page):
        """v8.13.0: the swimlane is a navigator, but clicking a work bar now PINS
        the Tier-2 detail panel (focused view of that one span) instead of opening
        the full timeline directly. The panel's [▤ 전체 타임라인] button escalates
        to Tier-3: opens the drawer and flashes the matching card (shared task_id
        "w1#0")."""
        page.goto(stack.url)
        stack.emit_ready()
        _show_flow(page)
        _drive_team(stack)
        page.wait_for_selector("#view-toggle:not([hidden])", timeout=8000)
        bar = page.locator('#team-view .tv-span[data-task-id="w1#0"]')
        assert _wait(lambda: bar.count() >= 1)
        card = page.locator('#messages .card-task-group[data-task-id="w1#0"]')
        assert _wait(lambda: card.count() == 1)
        # Click bar → Tier-2 detail panel pins; the drawer stays closed.
        bar.first.click()
        panel = page.locator("#detail-panel")
        assert _wait(lambda: "open" in (panel.get_attribute("class") or ""))
        drawer = page.locator("#timeline-drawer")
        assert "open" not in (drawer.get_attribute("class") or "")
        # [▤ 전체 타임라인] → Tier-3: drawer opens and the matching card flashes.
        page.click("#dp-timeline")
        assert _wait(lambda: "open" in (drawer.get_attribute("class") or ""))
        assert _wait(lambda: "tv-nav-hl" in (card.first.get_attribute("class") or ""))

    def test_click_bar_jumps_instantly_and_survives_live_events(self, stack, page):
        """From the bottom of a tall timeline, clicking a bar must jump to the
        card INSTANTLY (synchronously) — a smooth scroll gets cancelled by the
        constant card-appends + scrollToBottom() of an active run, which is the
        'sometimes doesn't scroll up, click 2-3 times' bug. Then a burst of live
        events must not yank it back down (auto-follow turned off)."""
        page.set_viewport_size({"width": 1100, "height": 460})
        page.goto(stack.url)
        stack.emit_ready()
        _show_flow(page)
        r = stack.renderer
        r.agent_roster(
            [{"key": "w1", "profile": "code-writer", "name": "w1", "state": "idle"}]
        )
        for i in range(20):
            r.begin_agent_work(key="w1", seq=i, profile="code-writer", message=f"t{i}")
            r.end_agent_work(key="w1", seq=i, success=True, duration_s=0.01)
        page.wait_for_selector("#team-view .tv-svg", timeout=8000)
        target = "w1#3"  # near the top → far off-screen once scrolled to the bottom
        assert _wait(
            lambda: (
                page.locator(
                    f'#messages .card-task-group[data-task-id="{target}"]'
                ).count()
                == 1
            )
        )
        in_view = (
            "() => { const m=document.getElementById('messages');"
            f" const c=m.querySelector('.card-task-group[data-task-id=\"{target}\"]');"
            " if(!c) return false; const mr=m.getBoundingClientRect(),"
            " cr=c.getBoundingClientRect();"
            " return cr.top >= mr.top-2 && cr.top <= mr.bottom; }"
        )
        moved_up = (
            "() => { const m=document.getElementById('messages');"
            " return m.scrollTop < m.scrollHeight - m.clientHeight - 5; }"
        )
        page.evaluate(
            "() => { const m=document.getElementById('messages'); m.scrollTop=m.scrollHeight; }"
        )
        assert not page.evaluate(in_view)  # target is off the top
        # v8.13.0: bar click pins the Tier-2 panel; [▤ 전체 타임라인] escalates and
        # performs the instant scroll. Both are synchronous.
        page.locator(f'#team-view .tv-span[data-task-id="{target}"]').first.click()
        page.click("#dp-timeline")
        # INSTANT: scrollTop has already moved up synchronously (a smooth scroll
        # would still be at the bottom here, then get cancelled by the appends).
        assert page.evaluate(moved_up)
        # A burst of live events (append cards + scrollToBottom) must not yank.
        for _ in range(10):
            r.final("live turn", turn=1)
            time.sleep(0.02)
        assert _wait(lambda: page.evaluate(in_view))
        card = page.locator(f'#messages .card-task-group[data-task-id="{target}"]')
        assert _wait(lambda: "tv-nav-hl" in (card.first.get_attribute("class") or ""))

    def test_click_navigates_to_top_of_expanded_card(self, stack, page):
        """A tall EXPANDED card must land with its header at the top (block:start)
        — center-aligning it would push the header off-screen above, so the user
        'lands in the middle' and can't tell which card it is."""
        page.set_viewport_size({"width": 1100, "height": 480})
        page.goto(stack.url)
        stack.emit_ready()
        _show_flow(page)
        r = stack.renderer
        r.agent_roster(
            [{"key": "w1", "profile": "code-writer", "name": "w1", "state": "idle"}]
        )
        for i in range(8):
            r.begin_agent_work(key="w1", seq=i, profile="code-writer", message=f"t{i}")
            r.end_agent_work(key="w1", seq=i, success=True, duration_s=0.01)
        page.wait_for_selector("#team-view .tv-svg", timeout=8000)
        assert _wait(
            lambda: (
                page.locator('#messages .card-task-group[data-task-id="w1#5"]').count()
                == 1
            )
        )
        # Make w1#5's card tall + expanded (a big body), then park it off-screen.
        page.evaluate(
            "() => { const c=document.querySelector("
            "'#messages .card-task-group[data-task-id=\"w1#5\"]');"
            " const b=c.querySelector('.task-body'); if(b){ b.hidden=false;"
            " const d=document.createElement('div'); d.style.height='800px';"
            " b.appendChild(d); } const m=document.getElementById('messages');"
            " m.scrollTop=m.scrollHeight; }"
        )
        # v8.13.0: bar click pins the panel; [▤ 전체 타임라인] escalates + scrolls.
        page.locator('#team-view .tv-span[data-task-id="w1#5"]').first.click()
        page.click("#dp-timeline")
        # The card's HEADER (top) must sit at/near the top of the viewport — not
        # scrolled past it (which a center-align of an 800px card would do).
        header_at_top = (
            "() => { const m=document.getElementById('messages');"
            " const c=m.querySelector('.card-task-group[data-task-id=\"w1#5\"]');"
            " if(!c) return false; const mr=m.getBoundingClientRect(),"
            " cr=c.getBoundingClientRect();"
            " return cr.top >= mr.top - 2 && cr.top <= mr.top + 60; }"
        )
        assert _wait(lambda: page.evaluate(header_at_top))


class TestResumeScopeReplay:
    """On resume, the scope sidecar's events come back tagged ``replay:true``
    and restore BOTH the swimlane bar and the timeline card.

    v7.21~7.27 drew only the bar ("inner turns replay flat → an empty shell").
    That left a resumed session with zero skill/agent cards while the swimlane
    still offered click-navigation to them. Since v7.28.0 resume is one
    time-ordered stream (``replay_session``) and turns carry their scope, so the
    card is the right thing to build — and when a scope's turns genuinely are not
    in this history (old session / sub-agent context) the card says so."""

    def test_replay_scope_draws_bar_and_card(self, stack, page):
        page.goto(stack.url)
        stack.emit_ready()
        _show_flow(page)
        # As replay_session would emit on resume: replay-tagged skill scope.
        r = stack.renderer
        r._emit(
            "scope_start",
            {"task_id": "sk-r", "kind": "skill", "label": "plan", "replay": True},
            persistent=True,
        )
        r._emit(
            "scope_end",
            {"task_id": "sk-r", "kind": "skill", "success": True, "replay": True},
            persistent=True,
        )
        page.wait_for_selector("#view-toggle:not([hidden])", timeout=8000)
        # Swimlane bar restored…
        assert _wait(lambda: page.locator(".tv-scope-skill").count() >= 1)
        # …and the collapsible card too, so the bar has somewhere to navigate.
        assert _wait(
            lambda: page.locator('.card-task-group[data-task-id="sk-r"]').count() == 1
        )
        # No turns were replayed into it → the card explains why it is empty.
        assert (
            page.locator(
                '.card-task-group[data-task-id="sk-r"] .task-empty-note'
            ).count()
            == 1
        )

    def test_live_scope_still_builds_timeline_card(self, stack, page):
        """Contrast: a normal (non-replay) scope DOES build the timeline card —
        the guard is scoped to replays only, no regression for live runs."""
        page.goto(stack.url)
        stack.emit_ready()
        _show_flow(page)
        stack.renderer.begin_scope(task_id="sk-live", kind="skill", label="plan")
        assert _wait(
            lambda: (
                page.locator('.card-task-group[data-task-id="sk-live"]').count() == 1
            )
        )


class TestVerticalLayout:
    """Vertical sequence-diagram layout: agent COLUMNS with a sticky header,
    time flows DOWN, long runs scroll vertically. Round-trip message arrows
    (request + reply) both draw."""

    def test_sticky_header_has_agent_columns(self, stack, page):
        page.goto(stack.url)
        stack.emit_ready()
        _show_flow(page)
        _drive_team(stack)
        page.wait_for_selector("#view-toggle:not([hidden])", timeout=8000)
        # Column chips live in the sticky header (.tv-head), not the plot.
        page.wait_for_selector("#team-view .tv-head .tv-chip", timeout=8000)
        assert _wait(lambda: page.locator("#team-view .tv-head .tv-chip").count() >= 3)
        # The header is position:sticky so it pins while the plot scrolls.
        pos = page.evaluate(
            "() => getComputedStyle(document.querySelector('.tv-head')).position"
        )
        assert pos == "sticky"

    def test_request_and_reply_arrows_both_draw(self, stack, page):
        """Round-trip: a main->agent request (direction=in, author=main) AND the
        agent->main reply (direction=out) each draw an arrow."""
        page.goto(stack.url)
        stack.emit_ready()
        _show_flow(page)
        r = stack.renderer
        r.agent_roster(
            [
                {"key": "w1", "profile": "code-writer", "name": "w1", "state": "busy"},
            ]
        )
        # request main -> w1 (previously invisible: "in" was ignored)
        r.agent_message(
            key="w1", direction="in", author="main", to="w1", text="go", seq=1
        )
        # reply w1 -> main
        r.agent_message(
            key="w1", direction="out", author="w1", to="main", text="done", seq=1
        )
        page.wait_for_selector("#view-toggle:not([hidden])", timeout=8000)
        assert _wait(lambda: page.locator("#team-view .tv-msg").count() == 2)

    def test_many_events_scroll_vertically(self, stack, page):
        """The axis is EVENT-ordinal: each event is a fixed-height row, so a run
        with many events grows past the viewport and scrolls (regardless of how
        much wall-clock time it spanned)."""
        page.set_viewport_size({"width": 900, "height": 460})
        page.goto(stack.url)
        stack.emit_ready()
        _show_flow(page)
        r = stack.renderer
        r.agent_roster(
            [{"key": "w1", "profile": "code-writer", "name": "w1", "state": "idle"}]
        )
        # 30 message events → 30 rows → taller than the viewport.
        for i in range(30):
            r.agent_message(
                key="w1", direction="out", author="w1", to="main", text=f"m{i}", seq=i
            )
        page.wait_for_selector("#view-toggle:not([hidden])", timeout=8000)
        page.wait_for_selector("#team-view .tv-msg", timeout=8000)
        assert _wait(
            lambda: page.evaluate(
                "() => { const h = document.getElementById('team-view');"
                " return h.scrollHeight > h.clientHeight + 20; }"
            )
        )

    def test_event_rows_are_uniformly_spaced(self, stack, page):
        """Event-ordinal axis: three events at 0s, 1s, 1000s (wildly uneven real
        gaps) land on evenly-spaced rows — consecutive row labels are the SAME
        pixel distance apart. Mutation guard against a proportional-time axis."""
        page.goto(stack.url)
        stack.emit_ready()
        _show_flow(page)
        r = stack.renderer
        r.agent_roster(
            [{"key": "w1", "profile": "code-writer", "name": "w1", "state": "idle"}]
        )
        r._emit(
            "agent_msg",
            {
                "key": "w1",
                "direction": "out",
                "author": "w1",
                "to": "main",
                "text": "a",
                "seq": 1,
                "ts": 1000.0,
            },
            persistent=True,
        )
        r._emit(
            "agent_msg",
            {
                "key": "w1",
                "direction": "out",
                "author": "w1",
                "to": "main",
                "text": "b",
                "seq": 2,
                "ts": 1001.0,
            },
            persistent=True,
        )
        r._emit(
            "agent_msg",
            {
                "key": "w1",
                "direction": "out",
                "author": "w1",
                "to": "main",
                "text": "c",
                "seq": 3,
                "ts": 2000.0,
            },
            persistent=True,
        )
        page.wait_for_selector("#view-toggle:not([hidden])", timeout=8000)
        page.wait_for_selector("#team-view .tv-tick", timeout=8000)
        ys = page.evaluate(
            "() => Array.from(document.querySelectorAll('#team-view .tv-tick'))"
            ".map(e => parseFloat(e.getAttribute('y'))).sort((a,b)=>a-b)"
        )
        assert len(ys) >= 3
        d1 = round(ys[1] - ys[0], 1)
        d2 = round(ys[2] - ys[1], 1)
        assert d1 == d2  # uniform spacing despite 1s vs 999s real gaps

    def test_busy_agent_shows_spinner_idle_shows_check(self, stack, page):
        """Tail status: a working agent gets a spinner, a resting one a check."""
        page.goto(stack.url)
        stack.emit_ready()
        _show_flow(page)
        r = stack.renderer
        r.agent_roster(
            [
                {"key": "w1", "profile": "code-writer", "name": "w1", "state": "busy"},
                {
                    "key": "w2",
                    "profile": "code-reviewer",
                    "name": "w2",
                    "state": "idle",
                },
            ]
        )
        r.agent_message(
            key="w1", direction="out", author="w1", to="main", text="hi", seq=1
        )
        page.wait_for_selector("#view-toggle:not([hidden])", timeout=8000)
        assert _wait(lambda: page.locator("#team-view .tv-busy").count() == 1)  # w1
        assert _wait(lambda: page.locator("#team-view .tv-idle").count() == 1)  # w2
        # Status lives ONLY at the tail now — the header chip no longer draws a
        # duplicate state glyph.
        assert page.locator("#team-view .tv-head .tv-state").count() == 0

    def test_hover_bar_shows_real_duration(self, stack, page):
        """The bar length is ordinal, so hover reports the REAL elapsed time."""
        page.goto(stack.url)
        stack.emit_ready()
        _show_flow(page)
        r = stack.renderer
        # skill scope from 1000s..1030s → 30s real duration
        r._emit(
            "scope_start",
            {"task_id": "sk", "kind": "skill", "label": "plan", "ts": 1000.0},
            persistent=True,
        )
        r._emit(
            "scope_end",
            {"task_id": "sk", "kind": "skill", "success": True, "ts": 1030.0},
            persistent=True,
        )
        page.wait_for_selector("#view-toggle:not([hidden])", timeout=8000)
        page.wait_for_selector("#team-view .tv-scope-skill", timeout=8000)
        tip = page.locator("#team-view .tv-scope-skill").first.get_attribute("data-tip")
        assert "30s" in tip

    def test_peer_reply_arrow_draws_despite_agent_prefix(self, stack, page):
        """orchestrator<->worker round-trip: the worker's reply is addressed
        to="agent:orch" but the lane is the bare "orch" — the prefix must be
        normalized so the REPLY arrow draws, not just the request (the reported
        'orchestrator request shows, reply doesn't' bug)."""
        page.goto(stack.url)
        stack.emit_ready()
        _show_flow(page)
        r = stack.renderer
        r.agent_roster(
            [
                {
                    "key": "orch",
                    "profile": "orchestrator",
                    "name": "orch",
                    "state": "busy",
                },
                {"key": "w1", "profile": "code-writer", "name": "w1", "state": "busy"},
            ]
        )
        # request orch -> w1 (orchestrator's canonical "out" uses bare keys)
        r.agent_message(
            key="orch", direction="out", author="orch", to="w1", text="impl", seq=1
        )
        # reply w1 -> orch, addressed with the "agent:" prefix
        r.agent_message(
            key="w1", direction="out", author="w1", to="agent:orch", text="done", seq=1
        )
        page.wait_for_selector("#view-toggle:not([hidden])", timeout=8000)
        assert _wait(lambda: page.locator("#team-view .tv-msg").count() == 2)


class TestScopeNestingRender:
    """Nested scopes — the visual fix, only checkable in a real browser: slots
    are horizontal offsets in an SVG and card nesting is DOM containment.

    Before this, every skill band / one-shot run was painted at the SAME x with
    the same width in main's column, so an outer skill covered its children
    completely, and each child's timeline card was appended to the ROOT as a
    sibling of its parent.
    """

    @staticmethod
    def _xs(page, selector):
        return sorted(float(h.get_attribute("x")) for h in page.locator(selector).all())

    def test_nested_scopes_get_distinct_x_positions(self, stack, page):
        page.goto(stack.url)
        stack.emit_ready()
        _show_flow(page)
        r = stack.renderer
        # skill → run → skill, each nested in the previous (same thread, so the
        # parent link is derived from the scope stack).
        r.begin_scope(task_id="sk-outer", kind="skill", label="orchestrate")
        r.begin_scope(task_id="run-1", kind="run", label="explorer", agent="explorer")
        r.end_scope(task_id="run-1", kind="run", success=True, duration_s=0.1)
        r.begin_scope(task_id="sk-inner", kind="skill", label="create-team")
        page.wait_for_selector("#team-view .tv-scope-skill", timeout=8000)
        assert _wait(lambda: page.locator("#team-view .tv-scope-skill").count() == 2)
        assert _wait(lambda: page.locator("#team-view .tv-scope-run").count() == 1)
        skills = self._xs(page, "#team-view .tv-scope-skill")
        runs = self._xs(page, "#team-view .tv-scope-run")
        # The nested skill and the run share slot 1 (sequential siblings), the
        # outer skill owns slot 0 → exactly two distinct columns of bars.
        assert len(set(skills)) == 2, f"nested skill hidden under its parent: {skills}"
        assert skills[0] < skills[1]
        assert runs[0] == skills[1]  # same slot reused by the later sibling

    def test_sequential_siblings_do_not_widen_main_lane(self, stack, page):
        """Slot reuse is what keeps the column narrow when a skill runs many
        children one after another."""
        page.goto(stack.url)
        stack.emit_ready()
        _show_flow(page)
        r = stack.renderer
        r.begin_scope(task_id="sk", kind="skill", label="orchestrate")
        for i in range(4):
            r.begin_scope(task_id=f"r{i}", kind="run", label=f"w{i}", index=i)
            r.end_scope(task_id=f"r{i}", kind="run", success=True, duration_s=0.01)
        page.wait_for_selector("#team-view .tv-scope-run", timeout=8000)
        assert _wait(lambda: page.locator("#team-view .tv-scope-run").count() == 4)
        assert len(set(self._xs(page, "#team-view .tv-scope-run"))) == 1

    def test_batch_shares_one_row_and_draws_a_fork(self, stack, page):
        """A parallel batch shares one start timestamp → one row (equal y), one
        fork, and a ⋔N badge; the members still get their own slots (distinct x)."""
        page.goto(stack.url)
        stack.emit_ready()
        _show_flow(page)
        r = stack.renderer
        r.begin_scope(task_id="sk", kind="skill", label="orchestrate")
        batch_ts = time.time()
        for i in range(3):
            r.begin_scope(
                task_id=f"w{i}",
                kind="run",
                label=f"worker {i}",
                index=i,
                parent="sk",
                ts=batch_ts,
            )
        # attached, not visible: a fork spine is a zero-height SVG path.
        page.wait_for_selector("#team-view .tv-fork", state="attached", timeout=8000)
        assert _wait(lambda: page.locator("#team-view .tv-scope-run").count() == 3)
        bars = page.locator("#team-view .tv-scope-run").all()
        ys = {b.get_attribute("y") for b in bars}
        xs = {b.get_attribute("x") for b in bars}
        assert len(ys) == 1, f"batch spread over {len(ys)} rows — reads as sequential"
        assert len(xs) == 3, "batch members overlap each other"
        assert page.locator("#team-view .tv-batch", has_text="⋔3").count() == 1
        tip = bars[0].get_attribute("data-tip")
        assert "배치 3개 동시 시작" in tip and "depth 1" in tip

    def test_nested_card_lives_inside_its_parents_body(self, stack, page):
        page.goto(stack.url)
        stack.emit_ready()
        _show_flow(page)
        r = stack.renderer
        r.begin_scope(task_id="sk-outer", kind="skill", label="orchestrate")
        r.begin_scope(task_id="run-1", kind="run", label="explorer", agent="explorer")
        # A nested card sits inside its parent's collapsed body, so wait for it
        # to be ATTACHED — "visible" is exactly what it must not be yet.
        page.wait_for_selector(
            '.card-task-group[data-task-id="run-1"]', state="attached", timeout=8000
        )
        nested = page.locator(
            '.card-task-group[data-task-id="sk-outer"] .task-body '
            '.card-task-group[data-task-id="run-1"]'
        )
        assert nested.count() == 1, "child card is not nested in its parent"
        # Root-level siblings: only the outer scope hangs off #messages directly.
        assert page.locator("#messages > .card-task-group").count() == 1

    def test_collapsed_parent_shows_live_child_hint(self, stack, page):
        page.goto(stack.url)
        stack.emit_ready()
        _show_flow(page)
        r = stack.renderer
        r.begin_scope(task_id="sk-outer", kind="skill", label="orchestrate")
        r.begin_scope(task_id="run-1", kind="run", label="find X", agent="explorer")
        page.wait_for_selector(
            '.card-task-group[data-task-id="run-1"]', state="attached", timeout=8000
        )
        sub = page.locator(
            '.card-task-group[data-task-id="sk-outer"] > .task-header > .task-sub'
        )
        assert _wait(lambda: "explorer" in (sub.inner_text() or ""))
        # …and it clears when the child finishes.
        r.end_scope(task_id="run-1", kind="run", success=True, duration_s=0.1)
        assert _wait(lambda: (sub.inner_text() or "").strip() == "")

    def test_clicking_nested_bar_expands_ancestors_and_reveals_card(self, stack, page):
        page.goto(stack.url)
        stack.emit_ready()
        _show_flow(page)
        r = stack.renderer
        r.begin_scope(task_id="sk-outer", kind="skill", label="orchestrate")
        r.begin_scope(task_id="run-1", kind="run", label="find X", agent="explorer")
        page.wait_for_selector("#team-view .tv-scope-run", timeout=8000)
        nested_card = page.locator('.card-task-group[data-task-id="run-1"]')
        # Parent collapsed by default → the nested card is not visible yet.
        assert not nested_card.is_visible()
        # v8.13.0: nested bar click pins the panel; [▤ 전체 타임라인] escalates,
        # expands the ancestor chain and reveals+flashes the nested card.
        page.locator("#team-view .tv-scope-run").first.click()
        page.click("#dp-timeline")
        assert _wait(lambda: nested_card.is_visible()), "ancestor chain not expanded"
        assert _wait(lambda: "tv-nav-hl" in (nested_card.get_attribute("class") or ""))


class TestUserLaneAndDock:
    """v8.2.0 — 팀뷰 기본 전환의 3요소: 사용자 레인(multiplex), complete 회신
    화살표(answers 귀속), 응답 독. 모두 실 렌더러 경로로 주입해 검증한다.

    (이 자리에 있던 TestSplitPaneWidthClamp 는 페인 폭 저장/클램프 기계장치와
    함께 제거 — 드로어 오버레이 레이아웃에는 저장되는 폭 자체가 없다.)
    """

    def test_user_message_draws_lane_mark_and_request_arrow(self, stack, page):
        page.goto(stack.url)
        stack.emit_ready()
        _show_flow(page)
        r = stack.renderer
        r.push_user_message("[Bob]: fix the bug", author="Bob")
        page.wait_for_selector("#team-view .tv-user-mark", timeout=8000)
        # The multiplexed user lane is the FIRST column ("users" chip).
        chips = page.locator("#team-view .tv-head .tv-lane-nm")
        assert _wait(lambda: chips.count() >= 2)
        assert chips.first.text_content() == "users"  # SVG text → not inner_text
        # Request arrow user→main with the sender's name as its label.
        assert _wait(lambda: page.locator("#team-view .tv-msg").count() >= 1)
        assert page.locator("#team-view .tv-msg-label", has_text="Bob").count() >= 1
        # The mark itself is labeled with the nickname (how multiple users
        # share one lane).
        assert page.locator("#team-view .tv-user-nm", has_text="Bob").count() >= 1

    def test_authorless_user_message_stays_off_the_lane(self, stack, page):
        # 🤝 agent-report starter: card only — no user lane, no arrow.
        page.goto(stack.url)
        stack.emit_ready()
        _show_flow(page)
        stack.renderer.push_user_message("[🤝 agent]: reply arrived")
        page.wait_for_selector("#messages .card-user", timeout=8000)
        assert page.locator("#team-view .tv-user-mark").count() == 0

    def test_final_with_answers_draws_dashed_reply_arrow(self, stack, page):
        page.goto(stack.url)
        stack.emit_ready()
        _show_flow(page)
        r = stack.renderer
        r.push_user_message("[Bob]: q1", author="Bob")
        r.push_user_message("[두정]: q2", author="두정")
        r.set_run_authors(["Bob", "두정"])
        r.final("the combined answer", turn=1)
        # ONE dashed reply arrow into the shared lane, labeled with everyone.
        assert _wait(lambda: page.locator("#team-view .tv-msg.tv-reply").count() == 1)
        assert (
            page.locator("#team-view .tv-msg-label", has_text="✓ Bob·두정").count() == 1
        )

    def test_reply_arrow_click_pins_panel_then_opens_drawer_at_final_card(
        self, stack, page
    ):
        page.goto(stack.url)
        stack.emit_ready()
        _show_flow(page)
        r = stack.renderer
        r.push_user_message("[Bob]: q", author="Bob")
        r.set_run_authors(["Bob"])
        r.final("the answer", turn=1)
        page.wait_for_selector("#team-view .tv-msg.tv-reply", timeout=8000)
        # The hit path is a transparent wide stroke — click via dispatchEvent
        # (its bbox centre may sit off the curve).
        page.eval_on_selector(
            "#team-view .tv-msg-g:has(.tv-reply) .tv-msg-hit[data-nav-ts]",
            "el => el.dispatchEvent(new MouseEvent('click', {bubbles: true}))",
        )
        # v8.13.0: reply-arrow click pins the Tier-2 panel first; [▤ 전체 타임라인]
        # escalates to the drawer + flashes the final card.
        panel = page.locator("#detail-panel")
        assert _wait(lambda: "open" in (panel.get_attribute("class") or ""))
        page.click("#dp-timeline")
        drawer = page.locator("#timeline-drawer")
        assert _wait(lambda: "open" in (drawer.get_attribute("class") or ""))
        card = page.locator("#messages .card-assistant[data-nav-ts]")
        assert _wait(lambda: "tv-nav-hl" in (card.first.get_attribute("class") or ""))

    def test_agent_report_run_is_unattributed(self, stack, page):
        # answers=[] (🤝 run): no reply arrow; the dock says so explicitly.
        page.goto(stack.url)
        stack.emit_ready()
        _show_flow(page)
        r = stack.renderer
        r.set_run_authors([])
        r.final("agent report folded in", turn=1)
        page.wait_for_selector("#dock:not([hidden])", timeout=8000)
        assert page.locator("#team-view .tv-msg.tv-reply").count() == 0
        assert "🤝" in page.locator("#dock .d-meta").inner_text()

    def test_dock_shows_attribution_and_navigates(self, stack, page):
        page.goto(stack.url)
        stack.emit_ready()
        _show_flow(page)
        r = stack.renderer
        r.push_user_message("[Bob]: q", author="Bob")
        r.set_run_authors(["Bob"])
        r.final("short answer", turn=1)
        page.wait_for_selector("#dock:not([hidden])", timeout=8000)
        assert _wait(lambda: "→ [Bob]" in page.locator("#dock .d-who").inner_text())
        assert "short answer" in page.locator("#d-text").inner_text()
        # Short answer → no 펼치기 (button only appears when clamped).
        assert page.locator("#d-expand").is_hidden()
        # Body click = drawer at that answer's card.
        page.click("#d-text")
        drawer = page.locator("#timeline-drawer")
        assert _wait(lambda: "open" in (drawer.get_attribute("class") or ""))
        card = page.locator("#messages .card-assistant[data-nav-ts]")
        assert _wait(lambda: "tv-nav-hl" in (card.first.get_attribute("class") or ""))

    def test_dock_expand_appears_only_when_clamped(self, stack, page):
        page.goto(stack.url)
        stack.emit_ready()
        _show_flow(page)
        r = stack.renderer
        r.set_run_authors(["Bob"])
        long_answer = "긴 답변 문장입니다. " * 120
        r.final(long_answer, turn=1)
        page.wait_for_selector("#dock:not([hidden])", timeout=8000)
        assert _wait(lambda: page.locator("#d-expand").is_visible())
        h0 = page.evaluate("() => document.getElementById('d-text').clientHeight")
        page.click("#d-expand")
        assert _wait(
            lambda: (
                page.evaluate("() => document.getElementById('d-text').clientHeight")
                > h0
            )
        )
        assert page.locator("#d-expand").inner_text() == "접기"
        # A NEW final resets to the collapsed 3-line preview.
        r.final("next short", turn=2)
        assert _wait(
            lambda: (
                "expanded" not in (page.locator("#dock").get_attribute("class") or "")
            )
        )

    def test_resume_replay_restores_lane_and_reply_arrow(self, stack, page):
        """Replay path parity: replayed user_message(author) + final(answers)
        rebuild the user lane and the reply arrow after reconnect."""
        r = stack.renderer
        r.push_user_message("[Bob]: q", author="Bob")
        r.set_run_authors(["Bob"])
        r.final("answered", turn=1)
        # Connect AFTER the events exist — the SSE replay must rebuild both.
        page.goto(stack.url)
        stack.emit_ready()
        _show_flow(page)
        page.wait_for_selector("#team-view .tv-user-mark", timeout=8000)
        assert _wait(lambda: page.locator("#team-view .tv-msg.tv-reply").count() == 1)


class TestMainSpinnerAndLabelClip:
    """v8.3.0 — 팀뷰 기본 표면의 두 마감: main 응답-중 스피너(worker_state
    구동)와 첫 행 화살표 who-라벨의 viewBox 클리핑 수리."""

    def test_main_spinner_follows_worker_state(self, stack, page):
        # 타임라인이 드로어 안이라, main 이 응답 중이라는 단서는 팀 표면의
        # main 컬럼 tail 스피너가 유일 — worker_busy 로 켜지고 idle 로 꺼진다.
        page.goto(stack.url)
        stack.emit_ready()
        _show_flow(page)
        r = stack.renderer
        r.push_user_message("[Bob]: go", author="Bob")
        page.wait_for_selector("#team-view .tv-svg", timeout=8000)
        assert page.locator("#team-view .tv-main-busy").count() == 0
        r.worker_busy()
        assert _wait(lambda: page.locator("#team-view .tv-main-busy").count() == 1)
        r.worker_idle()
        assert _wait(lambda: page.locator("#team-view .tv-main-busy").count() == 0)

    def test_reconnect_lands_in_busy_state(self, stack, page):
        # worker_state 는 sticky — 실행 중에 새로 접속해도 스피너가 보인다.
        r = stack.renderer
        r.push_user_message("[Bob]: go", author="Bob")
        r.worker_busy()
        page.goto(stack.url)
        stack.emit_ready()
        _show_flow(page)
        assert _wait(lambda: page.locator("#team-view .tv-main-busy").count() == 1)

    def test_first_row_arrow_label_is_not_clipped(self, stack, page):
        # 첫 이벤트가 사용자 메시지면 그 화살표가 첫 행(y=PAD_T)에 그려진다.
        # 라벨은 화살표 활보다 위에 있으므로 PAD_T 가 활+라벨 높이를 못
        # 이기면 viewBox 밖으로 잘린다 ("맨 위 화살표 이름 철수 안 보임").
        page.goto(stack.url)
        stack.emit_ready()
        _show_flow(page)
        stack.renderer.push_user_message("[철수]: 첫 요청", author="철수")
        page.wait_for_selector("#team-view .tv-msg-label", timeout=8000)
        box = page.evaluate(
            "() => document.querySelector('#team-view .tv-msg-label').getBBox().y"
        )
        assert box > 0, f"label bbox top {box} — clipped above the viewBox"
        assert page.locator("#team-view .tv-msg-label", has_text="철수").count() == 1


class TestSpawnTaskArrows:
    """v8.5.1 — 서로 다른 에이전트에 대한 같은-seq 요청(연속 spawn+task 의
    실제 모양)이 각각 화살표를 그린다. to 누락 시절엔 ingest 중복제거 키가
    충돌해 두 번째가 드롭됐다 (실세션 ae730c35 재현)."""

    def test_same_seq_requests_to_two_agents_both_draw(self, stack, page):
        page.goto(stack.url)
        stack.emit_ready()
        _show_flow(page)
        r = stack.renderer
        r.agent_roster(
            [
                {
                    "key": "w1",
                    "profile": "code-reviewer",
                    "name": "w1",
                    "state": "busy",
                },
                {"key": "w2", "profile": "code-writer", "name": "w2", "state": "busy"},
            ]
        )
        # spawn+task 두 번의 실제 이벤트 모양: 각자 자기 seq 카운터의 1번.
        r.agent_message(
            key="w1", direction="in", author="main", to="w1", text="review it", seq=1
        )
        r.agent_message(
            key="w2", direction="in", author="main", to="w2", text="write tests", seq=1
        )
        page.wait_for_selector("#team-view .tv-msg", timeout=8000)
        assert _wait(lambda: page.locator("#team-view .tv-msg").count() == 2), (
            "두 번째 spawn+task 요청 화살표가 dedup 에 삼켜짐"
        )
