"""Auto-review verdict parsing (PR2).

The reviewer agent is spawned as a normal delegate after the main agent
completes (web-only, when the auto-review toggle is on). It ends its run with
``complete`` whose result carries a verdict signature:

    VERDICT: ACCEPT
  or
    VERDICT: REJECT
    <specific issues to fix>

The worker parses that result string (no loop changes — the reviewer is just a
delegate). Parsing is LENIENT (small models drift): case-insensitive, the LAST
``VERDICT:`` line wins, and an unparseable verdict defaults to REJECT carrying
the raw output as feedback (quality-first — the user controls termination via
the toggle, decision 2).
"""

from agent_cli.review import parse_review_verdict


class TestParseReviewVerdict:
    def test_accept(self):
        accept, feedback = parse_review_verdict("Looks good.\nVERDICT: ACCEPT")
        assert accept is True

    def test_reject_with_feedback(self):
        out = "VERDICT: REJECT\n- main.c line 5 missing null check\n- add tests"
        accept, feedback = parse_review_verdict(out)
        assert accept is False
        assert "null check" in feedback
        assert "VERDICT" not in feedback  # verdict line stripped from feedback

    def test_case_insensitive(self):
        assert parse_review_verdict("verdict: accept")[0] is True
        assert parse_review_verdict("Verdict:  Reject\nfix it")[0] is False

    def test_last_verdict_wins(self):
        # reviewer may discuss "accept" criteria, then give its real verdict
        out = "I considered VERDICT: ACCEPT but found issues.\nVERDICT: REJECT\nfix"
        accept, _ = parse_review_verdict(out)
        assert accept is False

    def test_body_mentioning_accept_does_not_false_accept(self):
        # the word 'accept' in prose must NOT trigger accept — only VERDICT: line
        out = "I would accept this normally, but:\nVERDICT: REJECT\nmissing error handling"
        assert parse_review_verdict(out)[0] is False

    def test_unparseable_defaults_to_reject_with_raw(self):
        out = "This work has some problems with the parser logic."
        accept, feedback = parse_review_verdict(out)
        assert accept is False  # decision 2: quality-first default
        assert feedback == out  # raw output is the feedback

    def test_empty_defaults_to_reject(self):
        accept, feedback = parse_review_verdict("")
        assert accept is False

    def test_accept_feedback_empty(self):
        _, feedback = parse_review_verdict("all requirements met\nVERDICT: ACCEPT")
        assert feedback == ""  # accept carries no actionable feedback


class TestRunAutoReview:
    """The orchestration loop (deps injected for unit testing): spawn reviewer →
    parse verdict → accept stops, reject resumes the main agent with feedback →
    repeat until accept OR the toggle goes off. No safety cap (toggle controls)."""

    def _run(self, *, enabled, reviews, resumes):
        """enabled: list of bools consumed per is_enabled() call.
        reviews: list of reviewer outputs consumed per spawn.
        resumes: list of new final answers consumed per resume."""
        from agent_cli.review import run_auto_review

        en = iter(enabled)
        rv = iter(reviews)
        rs = iter(resumes)
        spawned, resumed, rendered = [], [], []

        def is_enabled():
            return next(en, False)

        def spawn_reviewer(task):
            spawned.append(task)
            return next(rv)

        def resume_main(feedback):
            resumed.append(feedback)
            return next(rs)

        def render(event, detail=""):
            rendered.append((event, detail))

        run_auto_review(
            "task",
            "final answer",
            ctx=None,
            is_enabled=is_enabled,
            spawn_reviewer=spawn_reviewer,
            resume_main=resume_main,
            render=render,
        )
        self._rendered = rendered
        return spawned, resumed

    def test_accept_first_round_stops(self):
        spawned, resumed = self._run(
            enabled=[True], reviews=["VERDICT: ACCEPT"], resumes=[]
        )
        assert len(spawned) == 1  # reviewed once
        assert resumed == []  # never resumed the main agent

    def test_reject_then_accept(self):
        spawned, resumed = self._run(
            enabled=[True, True],
            reviews=["VERDICT: REJECT\nfix the null check", "VERDICT: ACCEPT"],
            resumes=["fixed answer"],
        )
        assert len(spawned) == 2  # reviewed twice
        assert resumed == ["fix the null check"]  # resumed once with feedback

    def test_toggle_off_stops_immediately(self):
        spawned, resumed = self._run(enabled=[False], reviews=[], resumes=[])
        assert spawned == []  # toggle off → no review at all

    def test_toggle_off_mid_loop_stops(self):
        # round 1: enabled, reject → resume; round 2: toggle now off → stop
        spawned, resumed = self._run(
            enabled=[True, False],
            reviews=["VERDICT: REJECT\nmore work"],
            resumes=["second answer"],
        )
        assert len(spawned) == 1
        assert resumed == ["more work"]  # resumed once, then toggle stopped it

    def test_render_surfaces_accept_to_main(self):
        """The verdict must be surfaced to the main UI (it was invisible — the
        reviewer's ACCEPT lived only inside the delegate group card)."""
        self._run(enabled=[True], reviews=["VERDICT: ACCEPT good"], resumes=[])
        events = [e for e, _ in self._rendered]
        assert "review_start" in events
        assert "accept" in events

    def test_render_surfaces_reject_with_feedback(self):
        self._run(
            enabled=[True, True],
            reviews=["VERDICT: REJECT\nfix the parser", "VERDICT: ACCEPT"],
            resumes=["fixed"],
        )
        # the reject event carries the feedback detail so the main card shows it
        reject = [d for e, d in self._rendered if e == "reject"]
        assert reject and "fix the parser" in reject[0]
        assert "accept" in [e for e, _ in self._rendered]  # final round accepted


class TestRecordReviewObservation:
    """Auto-review result cards must also land in ctx (history.jsonl) so they
    survive resume — not just live SSE. The record mirrors loop's observation
    shape ({role:user, tool, success, content:"Observation: …"}) so
    replay_from_history renders it as an observation card on resume."""

    class _FakeCtx:
        def __init__(self):
            self.added = []

        def add(self, msg):
            self.added.append(msg)
            return msg

    def test_records_to_ctx(self):
        from agent_cli.review import record_review_observation

        ctx = self._FakeCtx()
        record_review_observation(ctx, "Auto-review passed.", success=True)
        assert len(ctx.added) == 1
        rec = ctx.added[0]
        assert rec["role"] == "user"
        assert rec["tool"] == "auto-review"
        assert rec["success"] is True
        # "Observation: " prefix so replay strips it like any tool result
        assert rec["content"].startswith("Observation: ")
        assert "Auto-review passed." in rec["content"]

    def test_records_failure(self):
        from agent_cli.review import record_review_observation

        ctx = self._FakeCtx()
        record_review_observation(ctx, "changes requested\nfix X", success=False)
        assert ctx.added[0]["success"] is False
        assert "fix X" in ctx.added[0]["content"]

    def test_none_ctx_is_noop(self):
        from agent_cli.review import record_review_observation

        # must not crash when there's no ctx (CLI / pre-session)
        record_review_observation(None, "x", success=True)
