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
    # request (in) opens w1's span; reply (out) closes it AND is the message.
    r.agent_message(
        key="w1", direction="in", author="orch", text="implement", seq=1, to="w1"
    )
    time.sleep(0.05)
    r.agent_message(
        key="w1", direction="out", author="w1", text="done", seq=2, to="orch"
    )


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

        # The enclosing skill renders as a band.
        assert _wait(lambda: page.locator(".tv-band").count() >= 1)
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
