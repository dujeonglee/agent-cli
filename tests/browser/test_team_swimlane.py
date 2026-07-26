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
        r = stack.renderer
        r.begin_scope(task_id="sk-outer", kind="skill", label="orchestrate")
        r.begin_scope(task_id="run-1", kind="run", label="find X", agent="explorer")
        page.wait_for_selector("#team-view .tv-scope-run", timeout=8000)
        nested_card = page.locator('.card-task-group[data-task-id="run-1"]')
        # Parent collapsed by default → the nested card is not visible yet.
        assert not nested_card.is_visible()
        page.locator("#team-view .tv-scope-run").first.click()
        assert _wait(lambda: nested_card.is_visible()), "ancestor chain not expanded"
        assert _wait(lambda: "tv-nav-hl" in (nested_card.get_attribute("class") or ""))


class TestSplitPaneWidthClamp:
    """저장된 패널 폭이 타임라인을 지워버리지 못하게 (v7.27.2 회귀 수리).

    v7.26.0 이 드래그 폭을 localStorage 에 저장하면서, **복원 경로에만 클램프가
    없었다**(드래그는 `rect.width - 360` 으로 클램프). 넓은 창에서 저장한 폭을
    좁은 창에서 복원하면 타임라인이 ~30px 로 짜부라져 **카드가 폭 0 으로
    렌더**됐다 — 카드는 DOM 에 다 있는데 보이지 않는다. localStorage 라서 서버·
    세션을 몇 번 재시작해도 안 고쳐졌고, 실제로 "resume 하면 카드가 하나도 안
    보인다"로 제보됐다(원인 오진 1회: 저장값 없는 새 브라우저로만 확인해서
    '회귀 아님' 으로 결론냈다가 라이브 재현으로 뒤집힘).
    """

    @staticmethod
    def _seed_and_measure(stack, browser, *, viewport, saved):
        ctx = browser.new_context(viewport=viewport)
        page = ctx.new_page()
        page.goto(stack.url)
        page.evaluate(f"() => localStorage.setItem('agentcli_team_w', '{saved}')")
        # 팀 활동이 있어야 패널이 드러난다 (평범한 단일-에이전트 대화엔 미표시).
        stack.renderer.begin_scope(task_id="sk", kind="skill", label="orchestrate")
        stack.renderer.observation("done", turn=1, tool_name="read_file", success=True)
        page.reload()
        page.wait_for_selector("#team-view:not([hidden])", timeout=8000)
        page.wait_for_selector("#messages .card", timeout=8000)
        out = page.evaluate(
            """() => {
                const m = document.getElementById('messages');
                const tv = document.getElementById('team-view');
                const card = m.querySelector(':scope > .card');
                const w = e => Math.round(e.getBoundingClientRect().width);
                return {msg: w(m), pane: w(tv), card: card ? w(card) : 0,
                        stored: localStorage.getItem('agentcli_team_w')};
            }"""
        )
        ctx.close()
        return out

    def test_oversized_stored_width_cannot_squeeze_the_timeline(self, stack, browser):
        stack.emit_ready()
        for viewport in ({"width": 1400, "height": 900}, {"width": 1000, "height": 800}):
            got = self._seed_and_measure(stack, browser, viewport=viewport, saved=3000)
            assert got["msg"] >= 300, (viewport, got)  # 타임라인 최소폭 확보
            assert got["card"] >= 200, (viewport, got)  # 카드가 실제로 읽히는 폭
            assert got["pane"] <= viewport["width"] - 300, (viewport, got)

    def test_small_stored_width_is_floored(self, stack, browser):
        stack.emit_ready()
        got = self._seed_and_measure(
            stack, browser, viewport={"width": 1200, "height": 800}, saved=10
        )
        assert got["pane"] >= 260, got  # 스윔레인도 사용 가능한 최소폭 유지

    def test_shrinking_the_window_reclamps(self, stack, browser):
        """창을 좁혀도 타임라인이 사라지지 않아야 한다 (resize 재클램프)."""
        stack.emit_ready()
        ctx = browser.new_context(viewport={"width": 1600, "height": 900})
        page = ctx.new_page()
        page.goto(stack.url)
        page.evaluate("() => localStorage.setItem('agentcli_team_w', '1100')")
        stack.renderer.begin_scope(task_id="sk2", kind="skill", label="orchestrate")
        stack.renderer.observation("done", turn=1, tool_name="read_file", success=True)
        page.reload()
        page.wait_for_selector("#messages .card", timeout=8000)
        page.set_viewport_size({"width": 900, "height": 800})
        assert _wait(
            lambda: page.evaluate(
                "() => Math.round(document.getElementById('messages').getBoundingClientRect().width)"
            )
            >= 300
        ), page.evaluate(
            "() => Math.round(document.getElementById('messages').getBoundingClientRect().width)"
        )
        ctx.close()
