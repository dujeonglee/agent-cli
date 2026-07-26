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

    def test_side_by_side_and_collapse_toggle(self, stack, page):
        page.goto(stack.url)
        stack.emit_ready()
        _drive_team(stack)
        page.wait_for_selector("#view-toggle:not([hidden])", timeout=8000)

        messages = page.locator("#messages")
        team = page.locator("#team-view")
        # Both visible at once — the swimlane sits BESIDE the timeline.
        assert _wait(lambda: team.is_visible() and messages.is_visible())
        # The drag handle is shown alongside the pane.
        assert _wait(lambda: page.locator("#split-handle").is_visible())
        # ◧ Team collapses the pane; the timeline stays visible, handle hidden.
        page.click("#vt-team-toggle")
        assert _wait(lambda: not team.is_visible() and messages.is_visible())
        assert not page.locator("#split-handle").is_visible()
        # Toggling again re-shows the pane + handle.
        page.click("#vt-team-toggle")
        assert _wait(lambda: team.is_visible() and messages.is_visible())
        assert _wait(lambda: page.locator("#split-handle").is_visible())

    def test_split_handle_drag_resizes_pane(self, stack, page):
        """Dragging the divider widens the swimlane pane (and the width sticks)."""
        page.set_viewport_size({"width": 1100, "height": 640})
        page.goto(stack.url)
        stack.emit_ready()
        _drive_team(stack)
        page.wait_for_selector("#split-handle:not([hidden])", timeout=8000)
        team = page.locator("#team-view")
        w0 = team.bounding_box()["width"]
        box = page.locator("#split-handle").bounding_box()
        cx = box["x"] + box["width"] / 2
        cy = box["y"] + box["height"] / 2
        page.mouse.move(cx, cy)
        page.mouse.down()
        page.mouse.move(cx + 130, cy, steps=6)
        page.mouse.up()
        w1 = team.bounding_box()["width"]
        assert w1 > w0 + 70  # pane got wider by ~the drag distance

    def test_reconnect_replay_no_flash_empty_or_duplicate(self, stack, page):
        """Reconnect replays the persistent buffer (roster sticky + scope_* +
        agent_msg). ingest dedups replayed events, so the view neither flashes
        the empty state nor draws duplicate spans — the fix for 'resets to No
        team activity on every event' (the old reset()-on-ready flush)."""
        page.goto(stack.url)
        stack.emit_ready()
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
        _drive_team(stack)
        page.wait_for_selector("#view-toggle:not([hidden])", timeout=8000)
        page.wait_for_selector(".tv-scope-skill", timeout=8000)
        page.locator(".tv-scope-skill").first.hover()
        page.wait_for_selector(".tv-tip:not([hidden])", timeout=3000)
        assert "orchestrate" in page.locator(".tv-tip").inner_text()

    def test_click_bar_navigates_timeline(self, stack, page):
        """The swimlane is a navigator: clicking a work bar flashes the matching
        timeline card (shared task_id). The w1 work bar carries data-task-id
        "w1#0", and the timeline has a .card-task-group with the same id."""
        page.goto(stack.url)
        stack.emit_ready()
        _drive_team(stack)
        page.wait_for_selector("#view-toggle:not([hidden])", timeout=8000)
        bar = page.locator('#team-view .tv-span[data-task-id="w1#0"]')
        assert _wait(lambda: bar.count() >= 1)
        card = page.locator('#messages .card-task-group[data-task-id="w1#0"]')
        assert _wait(lambda: card.count() == 1)
        bar.first.click()
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
        r = stack.renderer
        r.agent_roster(
            [{"key": "w1", "profile": "code-writer", "name": "w1", "state": "idle"}]
        )
        for i in range(20):
            r.begin_agent_work(key="w1", seq=i, profile="code-writer", message=f"t{i}")
            r.end_agent_work(key="w1", seq=i, success=True, duration_s=0.01)
        page.wait_for_selector("#split-handle:not([hidden])", timeout=8000)
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
        page.locator(f'#team-view .tv-span[data-task-id="{target}"]').first.click()
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
        r = stack.renderer
        r.agent_roster(
            [{"key": "w1", "profile": "code-writer", "name": "w1", "state": "idle"}]
        )
        for i in range(8):
            r.begin_agent_work(key="w1", seq=i, profile="code-writer", message=f"t{i}")
            r.end_agent_work(key="w1", seq=i, success=True, duration_s=0.01)
        page.wait_for_selector("#split-handle:not([hidden])", timeout=8000)
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
        page.locator('#team-view .tv-span[data-task-id="w1#5"]').first.click()
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
    """On resume, ``replay_scopes`` re-emits scope events tagged ``replay:true``
    so the swimlane bars come back. The frontend must draw the bar but NOT
    rebuild the timeline's collapsible card (inner turns replay flat → an
    empty shell)."""

    def test_replay_scope_draws_bar_without_timeline_card(self, stack, page):
        page.goto(stack.url)
        stack.emit_ready()
        # As replay_scopes would emit on resume: replay-tagged skill scope.
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
        # …but no collapsible timeline card for the replayed scope.
        assert page.locator('.card-task-group[data-task-id="sk-r"]').count() == 0

    def test_live_scope_still_builds_timeline_card(self, stack, page):
        """Contrast: a normal (non-replay) scope DOES build the timeline card —
        the guard is scoped to replays only, no regression for live runs."""
        page.goto(stack.url)
        stack.emit_ready()
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
