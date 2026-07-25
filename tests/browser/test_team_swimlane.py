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

        # The Timeline/Team toggle appears once team activity arrives.
        page.wait_for_selector("#view-toggle:not([hidden])", timeout=8000)
        page.click('.vt-tab[data-view="team"]')
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

    def test_timeline_and_team_are_mutually_exclusive(self, stack, page):
        page.goto(stack.url)
        stack.emit_ready()
        _drive_team(stack)
        page.wait_for_selector("#view-toggle:not([hidden])", timeout=8000)

        messages = page.locator("#messages")
        team = page.locator("#team-view")
        # Default: Timeline shown, Team hidden.
        assert messages.is_visible()
        assert not team.is_visible()
        # Switch to Team → swimlane shown, timeline hidden.
        page.click('.vt-tab[data-view="team"]')
        assert _wait(lambda: team.is_visible() and not messages.is_visible())
        # Back to Timeline.
        page.click('.vt-tab[data-view="timeline"]')
        assert _wait(lambda: messages.is_visible() and not team.is_visible())

    def test_reconnect_replay_no_flash_empty_or_duplicate(self, stack, page):
        """Reconnect replays the persistent buffer (roster sticky + scope_* +
        agent_msg). ingest dedups replayed events, so the view neither flashes
        the empty state nor draws duplicate spans — the fix for 'resets to No
        team activity on every event' (the old reset()-on-ready flush)."""
        page.goto(stack.url)
        stack.emit_ready()
        _drive_team(stack)
        page.wait_for_selector("#view-toggle:not([hidden])", timeout=8000)
        page.click('.vt-tab[data-view="team"]')
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
        page.click('.vt-tab[data-view="team"]')
        page.wait_for_selector(".tv-scope-skill", timeout=8000)
        page.locator(".tv-scope-skill").first.hover()
        page.wait_for_selector(".tv-tip:not([hidden])", timeout=3000)
        assert "orchestrate" in page.locator(".tv-tip").inner_text()


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
        page.click('.vt-tab[data-view="team"]')
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
        page.click('.vt-tab[data-view="team"]')
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
        page.click('.vt-tab[data-view="team"]')
        assert _wait(lambda: page.locator("#team-view .tv-msg").count() == 2)

    def test_long_run_scrolls_vertically(self, stack, page):
        """A run longer than the 1h cap stops compressing and grows downward —
        the container becomes vertically scrollable."""
        page.goto(stack.url)
        stack.emit_ready()
        r = stack.renderer
        # A 2-hour skill scope (explicit ts) — span 7200s > CAP 3600s.
        r._emit(
            "scope_start",
            {"task_id": "long", "kind": "skill", "label": "big", "ts": 1000.0},
            persistent=True,
        )
        r._emit(
            "scope_end",
            {"task_id": "long", "kind": "skill", "success": True, "ts": 1000.0 + 7200},
            persistent=True,
        )
        page.wait_for_selector("#view-toggle:not([hidden])", timeout=8000)
        page.click('.vt-tab[data-view="team"]')
        page.wait_for_selector("#team-view .tv-scope-skill", timeout=8000)
        assert _wait(
            lambda: page.evaluate(
                "() => { const h = document.getElementById('team-view');"
                " return h.scrollHeight > h.clientHeight + 20; }"
            )
        )

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
        page.click('.vt-tab[data-view="team"]')
        assert _wait(lambda: page.locator("#team-view .tv-msg").count() == 2)
