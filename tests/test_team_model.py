"""Unit tests for the Team-swimlane derivation (web/static/team_model.js).

team_model.js is a pure, DOM-free module (dual Node/browser export), so we drive
it through Node with synthetic SSE event streams and assert the derived model —
observable output, no logic re-implementation. Skips when Node is absent; GitHub
ubuntu runners ship Node so this runs in CI.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess

import pytest

_NODE = shutil.which("node")
_MODULE = os.path.abspath(
    os.path.join(
        os.path.dirname(__file__), "..", "agent_cli", "web", "static", "team_model.js"
    )
)

pytestmark = pytest.mark.skipif(
    _NODE is None, reason="node not available for JS unit test"
)


def run_model(events: list[dict]) -> dict:
    """Build the team model in Node and return it as plain JSON (agents Map →
    object, private _busyFrom stripped)."""
    script = (
        f"const TM = require({json.dumps(_MODULE)});"
        + """
let raw=""; process.stdin.on("data",d=>raw+=d); process.stdin.on("end",()=>{
  const m = TM.build(JSON.parse(raw));
  const agents = {};
  m.agents.forEach((v,k)=>{ const c=Object.assign({},v); delete c._busyFrom; agents[k]=c; });
  process.stdout.write(JSON.stringify({lanes:m.lanes, agents, messages:m.messages,
    userMarks:m.userMarks, finals:m.finals,
    oneshots:m.oneshots, skillBands:m.skillBands, mainSpans:m.mainSpans,
    forks:m.forks, maxSlot:m.maxSlot, t0:m.t0, t1:m.t1}));
});
"""
    )
    p = subprocess.run(
        [_NODE, "-e", script],
        input=json.dumps(events),
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert p.returncode == 0, p.stderr
    return json.loads(p.stdout)


def node_eval(expr: str):
    """Evaluate a small expression against the module and return parsed JSON."""
    script = f"const TM = require({json.dumps(_MODULE)}); process.stdout.write(JSON.stringify({expr}));"
    p = subprocess.run(
        [_NODE, "-e", script],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert p.returncode == 0, p.stderr
    return json.loads(p.stdout)


ROSTER = {
    "type": "agent_roster",
    "ts": 2.0,
    "roster": [
        {"key": "orch", "profile": "orchestrator", "name": "orch", "state": "busy"},
        {"key": "w1", "profile": "code-writer", "name": "w1", "state": "idle"},
    ],
}


class TestLanes:
    def test_main_is_first_then_agents_by_first_seen(self):
        # w2 appears (in roster) after w1 → lane order main, w1, w2
        events = [
            {
                "type": "agent_roster",
                "ts": 1.0,
                "roster": [{"key": "w1", "profile": "code-writer"}],
            },
            {
                "type": "agent_roster",
                "ts": 2.0,
                "roster": [
                    {"key": "w1", "profile": "code-writer"},
                    {"key": "w2", "profile": "code-reviewer"},
                ],
            },
        ]
        m = run_model(events)
        assert m["lanes"] == ["main", "w1", "w2"]

    def test_agent_identity_from_roster(self):
        m = run_model([ROSTER])
        assert m["agents"]["orch"]["role"] == "orchestrator"
        assert m["agents"]["w1"]["label"] == "w1"
        assert m["lanes"] == ["main", "orch", "w1"]


class TestSpans:
    def test_agent_work_scope_becomes_span_in_that_lane(self):
        # begin_agent_work uses a "{key}#{seq}" task_id → a work span in that
        # agent's OWN lane (NOT a one-shot under main — that flooded main's lane).
        events = [
            ROSTER,
            {
                "type": "scope_start",
                "ts": 3.0,
                "task_id": "w1#0",
                "kind": "run",
                "agent": "🤝 code-writer",
                "label": "impl",
            },
            {"type": "scope_end", "ts": 9.0, "task_id": "w1#0"},
        ]
        m = run_model(events)
        spans = m["agents"]["w1"]["spans"]
        assert len(spans) == 1
        assert spans[0]["t0"] == 3.0 and spans[0]["t1"] == 9.0
        assert spans[0]["task_id"] == "w1#0"  # kept for click→timeline-card link
        assert m["oneshots"] == []  # NOT a main one-shot

    def test_open_agent_work_closed_at_trace_end(self):
        events = [
            ROSTER,
            {
                "type": "scope_start",
                "ts": 3.0,
                "task_id": "w1#0",
                "kind": "run",
                "agent": "🤝 code-writer",
                "label": "impl",
            },
            {
                "type": "agent_msg",
                "ts": 12.0,
                "direction": "out",
                "key": "orch",
                "author": "orch",
                "to": "main",
                "seq": 9,
            },
        ]
        m = run_model(events)
        spans = m["agents"]["w1"]["spans"]
        assert len(spans) == 1
        assert spans[0]["t0"] == 3.0 and spans[0]["t1"] == 12.0  # tMax

    def test_true_oneshot_stays_under_main(self):
        # a delegate/agent-run (task_id WITHOUT "#") is a real one-shot → main.
        events = [
            {
                "type": "scope_start",
                "ts": 0.5,
                "task_id": "delegate-single-abc",
                "kind": "run",
                "agent": "code-analyst",
                "label": "map",
            },
            {"type": "scope_end", "ts": 1.4, "task_id": "delegate-single-abc"},
        ]
        m = run_model(events)
        assert len(m["oneshots"]) == 1
        assert m["oneshots"][0]["label"] == "code-analyst"


class TestMessages:
    def test_lone_inbound_is_not_a_message(self):
        # Only the canonical "out" (the sender's own send) becomes a message.
        # A lone inbound with no matching out yields ZERO messages. Mutation
        # guard: if the direction=="out" filter is dropped, this inbound would
        # wrongly produce a message.
        events = [
            ROSTER,
            {
                "type": "agent_msg",
                "ts": 3.0,
                "direction": "in",
                "key": "w1",
                "author": "orch",
                "to": "w1",
                "text": "impl",
                "seq": 1,
            },
        ]
        m = run_model(events)
        assert m["messages"] == []

    def test_duplicate_out_is_deduped(self):
        # Reconnect replay can re-deliver an identical send. Same (from,to,seq,ts)
        # collapses to one message. Mutation guard: removing the seenMsg dedup
        # makes this two.
        dup = {
            "type": "agent_msg",
            "ts": 9.0,
            "direction": "out",
            "key": "w1",
            "author": "w1",
            "to": "orch",
            "text": "done",
            "seq": 2,
        }
        m = run_model([ROSTER, dict(dup), dict(dup)])
        assert len(m["messages"]) == 1

    def test_inbound_out_pair_is_one_message(self):
        # The same logical message (its in at receiver + out at sender) counts once.
        events = [
            ROSTER,
            {
                "type": "agent_msg",
                "ts": 3.0,
                "direction": "in",
                "key": "w1",
                "author": "orch",
                "to": "w1",
                "text": "impl",
                "seq": 1,
            },
            {
                "type": "agent_msg",
                "ts": 3.0,
                "direction": "out",
                "key": "orch",
                "author": "orch",
                "to": "w1",
                "text": "impl",
                "seq": 1,
            },
        ]
        m = run_model(events)
        assert len(m["messages"]) == 1
        assert m["messages"][0]["from"] == "orch" and m["messages"][0]["to"] == "w1"

    def test_main_request_inbound_is_drawn(self):
        # A request FROM main arrives at a teammate as direction="in",
        # author="main". main emits no "out" of its own, so without drawing this
        # inbound the request leg of the round-trip is invisible — it must render
        # as the assign arrow. Mutation guard: dropping the main/user "in" branch
        # makes this empty.
        events = [
            ROSTER,
            {
                "type": "agent_msg",
                "ts": 3.0,
                "direction": "in",
                "key": "w1",
                "author": "main",
                "to": "w1",
                "text": "go",
                "seq": 1,
            },
        ]
        m = run_model(events)
        assert len(m["messages"]) == 1
        assert m["messages"][0]["from"] == "main" and m["messages"][0]["to"] == "w1"
        assert m["messages"][0]["type"] == "assign"

    def test_user_intervention_inbound_is_drawn(self):
        # A web human intervention arrives as author="user:*" — also external
        # (no "out" counterpart), so its request arrow must show too.
        events = [
            ROSTER,
            {
                "type": "agent_msg",
                "ts": 3.0,
                "direction": "in",
                "key": "w1",
                "author": "user:alice",
                "to": "w1",
                "text": "hi",
                "seq": 1,
            },
        ]
        m = run_model(events)
        assert len(m["messages"]) == 1
        # v8.2.0: "user:*" authors map onto the ONE multiplexed user lane
        # (leftmost), nickname kept as the arrow label — previously the
        # "user:alice" from-key had no lane so the view dropped the arrow.
        assert m["messages"][0]["from"] == "user"
        assert m["messages"][0]["who"] == "alice"
        assert m["messages"][0]["to"] == "w1"
        assert m["lanes"][0] == "user"
        assert m["userMarks"] == [{"t": 3.0, "who": "alice", "text": "hi"}]

    def test_peer_inbound_not_doubled_by_roundtrip(self):
        # A peer request has BOTH an "in" (at receiver) and an "out" (at sender).
        # Only the "out" is canonical — the peer "in" must STAY skipped so the
        # round-trip fix (drawing main/user inbounds) doesn't double peer arrows.
        events = [
            ROSTER,
            {
                "type": "agent_msg",
                "ts": 3.0,
                "direction": "in",
                "key": "w1",
                "author": "orch",
                "to": "w1",
                "text": "impl",
                "seq": 5,
            },
            {
                "type": "agent_msg",
                "ts": 3.1,
                "direction": "out",
                "key": "orch",
                "author": "orch",
                "to": "w1",
                "text": "impl",
                "seq": 1,
            },
        ]
        m = run_model(events)
        assert len(m["messages"]) == 1  # canonical out only, not doubled

    def test_peer_reply_agent_prefix_normalized(self):
        # A peer reply carries to="agent:orch" (the request author was stamped
        # "agent:<key>" by the message handler), but the lane key is the BARE
        # "orch". Without stripping the "agent:" prefix the reply's target lane
        # isn't found and the VIEW drops the arrow — the reported "orchestrator
        # request shows but the reply doesn't" bug. Mutation guard: remove the
        # normalization and to stays "agent:orch" (no matching lane).
        events = [
            {
                "type": "agent_roster",
                "ts": 0.0,
                "roster": [
                    {"key": "orch", "profile": "orchestrator", "state": "busy"},
                    {"key": "w1", "profile": "code-writer", "state": "busy"},
                ],
            },
            {
                "type": "agent_msg",
                "ts": 5.0,
                "direction": "out",
                "key": "w1",
                "author": "w1",
                "to": "agent:orch",
                "text": "done",
                "seq": 1,
            },
        ]
        m = run_model(events)
        assert len(m["messages"]) == 1
        assert m["messages"][0]["from"] == "w1"
        assert m["messages"][0]["to"] == "orch"  # "agent:" stripped → lane match

    def test_message_type_assign_report_message(self):
        events = [
            ROSTER,
            {
                "type": "agent_msg",
                "ts": 3.0,
                "direction": "out",
                "key": "main",
                "author": "main",
                "to": "orch",
                "seq": 1,
            },
            {
                "type": "agent_msg",
                "ts": 9.0,
                "direction": "out",
                "key": "w1",
                "author": "w1",
                "to": "orch",
                "seq": 2,
            },
            {
                "type": "agent_msg",
                "ts": 30.0,
                "direction": "out",
                "key": "orch",
                "author": "orch",
                "to": "main",
                "seq": 9,
            },
        ]
        m = run_model(events)
        types = {(x["from"], x["to"]): x["type"] for x in m["messages"]}
        assert types[("main", "orch")] == "assign"
        assert types[("orch", "main")] == "report"
        assert types[("w1", "orch")] == "message"


class TestOneshots:
    def test_delegate_task_lifecycle(self):
        events = [
            {
                "type": "scope_start",
                "ts": 0.5,
                "task_id": "t1",
                "kind": "run",
                "index": 0,
                "agent": "code-analyst",
                "label": "map rbtree",
            },
            {"type": "scope_end", "ts": 1.4, "task_id": "t1"},
        ]
        m = run_model(events)
        assert len(m["oneshots"]) == 1
        o = m["oneshots"][0]
        assert o["caller"] == "main" and o["label"] == "code-analyst"
        assert o["t0"] == 0.5 and o["t1"] == 1.4

    def test_unfinished_run_closes_at_end(self):
        # An unfinished scope closes at tMax — the last ACTIVITY event (a later
        # message here; a roster ts would not count, see the domain test).
        events = [
            {
                "type": "scope_start",
                "ts": 0.5,
                "task_id": "t1",
                "kind": "run",
                "agent": "x",
            },
            {
                "type": "agent_msg",
                "ts": 5.0,
                "direction": "out",
                "author": "w1",
                "to": "main",
                "seq": 1,
            },
        ]
        m = run_model(events)
        assert m["oneshots"][0]["t1"] == 5.0  # tMax (last activity)


class TestDomainAndMisc:
    def test_time_domain_spans_all_events(self):
        m = run_model(
            [
                {
                    "type": "scope_start",
                    "ts": 0.5,
                    "task_id": "t",
                    "kind": "run",
                    "agent": "a",
                },
                {"type": "scope_end", "ts": 1.0, "task_id": "t"},
                {
                    "type": "agent_msg",
                    "ts": 30.0,
                    "direction": "out",
                    "author": "orch",
                    "to": "main",
                    "seq": 1,
                },
            ]
        )
        assert m["t0"] == 0.5 and m["t1"] == 30.0

    def test_roster_only_no_spans(self):
        # reconnect: only the roster sticky replays → lanes restored, no spans
        m = run_model([ROSTER])
        assert m["lanes"] == ["main", "orch", "w1"]
        assert m["agents"]["w1"]["spans"] == []

    def test_roster_ts_does_not_extend_time_domain(self):
        # A roster is a state snapshot stamped with wall-clock-now; it must NOT
        # bump the time domain (which would drag an open scope out to "now" and
        # spawn a phantom far-future row in the event-ordinal view). The domain
        # is defined by activity (messages/scopes) only. Mutation guard: bump on
        # roster and t1 jumps to 9999.
        m = run_model(
            [
                {
                    "type": "agent_msg",
                    "ts": 10.0,
                    "direction": "out",
                    "author": "orch",
                    "to": "main",
                    "seq": 1,
                },
                {
                    "type": "agent_msg",
                    "ts": 20.0,
                    "direction": "out",
                    "author": "w1",
                    "to": "main",
                    "seq": 2,
                },
                {"type": "agent_roster", "ts": 9999.0, "roster": [{"key": "w1"}]},
            ]
        )
        assert m["t1"] == 20.0  # roster's 9999 ts ignored for the domain

    def test_hue_fallback_for_unknown_profile(self):
        assert node_eval("TM.hueFor('code-writer')") == "--h-writer"
        assert node_eval("TM.hueFor('orchestrator')") == "--h-orch"
        assert node_eval("TM.hueFor('some-custom')") == "--h-worker"

    def test_iso_timestamp_accepted(self):
        m = run_model(
            [
                {
                    "type": "agent_msg",
                    "ts": "2026-07-25T00:00:10Z",
                    "direction": "out",
                    "author": "orch",
                    "to": "main",
                    "seq": 1,
                },
            ]
        )
        assert m["messages"][0]["from"] == "orch"
        assert m["t1"] > 0  # ISO parsed to epoch, not dropped


class TestSkillBands:
    def test_scope_skill_becomes_band_run_becomes_oneshot(self):
        # A skill scope (e.g. /orchestrate) → skill band; a run scope → one-shot.
        events = [
            {
                "type": "scope_start",
                "ts": 0.0,
                "task_id": "sk",
                "kind": "skill",
                "label": "orchestrate",
            },
            {
                "type": "scope_start",
                "ts": 0.5,
                "task_id": "r1",
                "kind": "run",
                "label": "code-analyst",
                "agent": "code-analyst",
            },
            {"type": "scope_end", "ts": 1.4, "task_id": "r1"},
            {"type": "scope_end", "ts": 31.0, "task_id": "sk"},
        ]
        m = run_model(events)
        assert len(m["skillBands"]) == 1
        assert m["skillBands"][0]["label"] == "orchestrate"
        assert m["skillBands"][0]["t0"] == 0.0 and m["skillBands"][0]["t1"] == 31.0
        assert len(m["oneshots"]) == 1
        assert m["oneshots"][0]["label"] == "code-analyst"

    def test_scope_default_kind_is_run(self):
        # kind omitted → treated as a run (one-shot), not a skill band.
        m = run_model(
            [
                {"type": "scope_start", "ts": 0.0, "task_id": "x", "label": "t"},
                {"type": "scope_end", "ts": 1.0, "task_id": "x"},
            ]
        )
        assert m["skillBands"] == []
        assert len(m["oneshots"]) == 1


def run_model_now(events: list[dict], now: float) -> dict:
    """Build with a live ``now`` (second arg) and return the model JSON."""
    script = f"const TM = require({json.dumps(_MODULE)});" + (
        "\nlet raw=''; process.stdin.on('data',d=>raw+=d); process.stdin.on('end',()=>{\n"
        "  const p = JSON.parse(raw);\n"
        "  const m = TM.build(p.events, p.now);\n"
        "  const agents = {}; m.agents.forEach((v,k)=>{ agents[k]=Object.assign({},v); });\n"
        "  process.stdout.write(JSON.stringify({agents, oneshots:m.oneshots,"
        " skillBands:m.skillBands, mainSpans:m.mainSpans, t0:m.t0, t1:m.t1}));\n"
        "});\n"
    )
    p = subprocess.run(
        [_NODE, "-e", script],
        input=json.dumps({"events": events, "now": now}),
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert p.returncode == 0, p.stderr
    return json.loads(p.stdout)


_LIVE_ROSTER = {
    "type": "agent_roster",
    "ts": 1.0,
    "roster": [{"key": "w1", "profile": "code-writer", "name": "w1"}],
}
_LIVE_OPEN = {
    "type": "scope_start",
    "ts": 2.0,
    "task_id": "w1#0",
    "kind": "run",
    "label": "build",
}


class TestLiveNow:
    def test_ongoing_scope_extends_to_now(self):
        # open scope + live now → the bar (and domain) grow to now.
        m = run_model_now([_LIVE_ROSTER, _LIVE_OPEN], 100.0)
        assert m["agents"]["w1"]["spans"][0]["t1"] == 100.0
        assert m["t1"] == 100.0

    def test_finished_run_has_no_dead_space(self):
        # everything closed → domain stays at the last event, NOT now.
        m = run_model_now(
            [
                _LIVE_ROSTER,
                _LIVE_OPEN,
                {"type": "scope_end", "ts": 5.0, "task_id": "w1#0"},
            ],
            100.0,
        )
        assert m["agents"]["w1"]["spans"][0]["t1"] == 5.0
        assert m["t1"] == 5.0

    def test_no_now_is_deterministic(self):
        # no now (tests / non-live) → tMax from events, never wall-clock.
        m = run_model([_LIVE_ROSTER, _LIVE_OPEN])
        assert m["t1"] == 2.0


def _scope(task_id, t0, t1, *, kind="run", parent="", depth=0, index=0, label=None):
    """One scope's start/end event pair. ``t1=None`` leaves it open."""
    start = {
        "type": "scope_start",
        "ts": t0,
        "task_id": task_id,
        "kind": kind,
        "label": label or task_id,
        "parent": parent,
        "depth": depth,
        "index": index,
    }
    if t1 is None:
        return [start]
    return [start, {"type": "scope_end", "ts": t1, "task_id": task_id}]


def _slots(m):
    """task_id → slot for every scope in the caller column."""
    return {it["task_id"]: it["slot"] for it in m["skillBands"] + m["oneshots"]}


class TestScopeSlots:
    """Skill bands and one-shot runs all block their caller, so they share ONE
    column. Without a slot each, a nested scope was painted at the same x and
    width as its parent — the parent (drawn later) covered it completely. The
    slot is derived from parent + depth, never from event order.
    """

    def test_nested_scopes_get_increasing_slots(self):
        events = (
            _scope("sk-outer", 0.0, None, kind="skill", depth=0)
            + _scope("run-1", 1.0, 5.0, parent="sk-outer", depth=1)
            + _scope("sk-inner", 6.0, None, kind="skill", parent="sk-outer", depth=1)
            + _scope("run-2", 7.0, 9.0, parent="sk-inner", depth=2)
        )
        m = run_model(events)
        slots = _slots(m)
        assert slots["sk-outer"] == 0
        assert slots["run-1"] == 1
        assert slots["sk-inner"] == 1  # sequential sibling REUSES the slot
        assert slots["run-2"] == 2
        assert m["maxSlot"] == 2

    def test_sequential_siblings_do_not_widen_the_column(self):
        """skill/run block the caller, so back-to-back children never overlap:
        a long chain of them must not push the column wider and wider."""
        events = _scope("sk", 0.0, None, kind="skill")
        for i in range(6):
            events += _scope(
                f"r{i}", 1.0 + i * 2, 2.0 + i * 2, parent="sk", depth=1, index=i
            )
        m = run_model(events)
        assert m["maxSlot"] == 1
        assert set(_slots(m).values()) == {0, 1}

    def test_concurrent_siblings_fan_out_sideways(self):
        """The rare parallel batch: three runs alive at once must each get
        their own slot, or they collapse into one visible bar."""
        events = (
            _scope("sk", 0.0, None, kind="skill")
            + _scope("w0", 2.0, 9.0, parent="sk", depth=1, index=0)
            + _scope("w1", 2.0, 7.0, parent="sk", depth=1, index=1)
            + _scope("w2", 2.0, None, parent="sk", depth=1, index=2)
        )
        m = run_model(events)
        assert _slots(m) == {"sk": 0, "w0": 1, "w1": 2, "w2": 3}

    def test_live_batch_with_no_later_event_still_fans_out(self):
        """Degenerate but REAL: the batch has just started and nothing has
        happened since, so every member is open and closes at tMax — which is
        their own shared start. Zero-length spans "don't overlap" under a strict
        interval test, and the whole live batch collapsed into one slot (one
        visible bar) until the next event arrived. Same start ⇒ concurrent."""
        events = (
            _scope("sk", 0.0, None, kind="skill")
            + _scope("w0", 2.0, None, parent="sk", depth=1, index=0)
            + _scope("w1", 2.0, None, parent="sk", depth=1, index=1)
            + _scope("w2", 2.0, None, parent="sk", depth=1, index=2)
        )
        m = run_model(events)
        assert _slots(m) == {"sk": 0, "w0": 1, "w1": 2, "w2": 3}
        assert m["maxSlot"] == 3

    def test_back_to_back_touching_spans_reuse_the_slot(self):
        """The same-start rule must NOT bleed into adjacency: a child that
        starts exactly when the previous one ended is still sequential and
        should reuse the slot (otherwise the column creeps wider)."""
        events = (
            _scope("sk", 0.0, None, kind="skill")
            + _scope("a", 2.0, 5.0, parent="sk", depth=1, index=0)
            + _scope("b", 5.0, 9.0, parent="sk", depth=1, index=1)
        )
        assert _slots(run_model(events)) == {"sk": 0, "a": 1, "b": 1}

    def test_batch_slot_order_follows_worker_index(self):
        """A batch shares one t0, so ordering by time alone is ambiguous —
        index breaks the tie, keeping the picture stable across replays."""
        events = _scope("sk", 0.0, None, kind="skill")
        # events deliberately arrive out of index order
        for idx in (2, 0, 1):
            events += _scope(f"w{idx}", 2.0, 8.0 + idx, parent="sk", depth=1, index=idx)
        assert _slots(run_model(events)) == {"sk": 0, "w0": 1, "w1": 2, "w2": 3}

    def test_child_is_pushed_right_of_a_displaced_parent(self):
        """A parent bumped sideways by a batch must still visually CONTAIN its
        child. The overlap test alone does not guarantee that: a slot left of
        the parent can be free by the time the child starts (its earlier
        occupant already finished), and the child would then be drawn to the
        LEFT of its own parent — nesting reading backwards. The parent-slot
        floor is what prevents it, so the batch here has short-lived siblings.
        """
        events = (
            _scope("sk", 0.0, None, kind="skill")
            # batch of three: slots 1, 2, 3 by index
            + _scope("w0", 2.0, 5.0, parent="sk", depth=1, index=0)
            + _scope("w1", 2.0, 5.0, parent="sk", depth=1, index=1)
            + _scope("w2", 2.0, 40.0, parent="sk", depth=1, index=2)
            # w0/w1 are long gone by now, so slots 1 and 2 are free again
            + _scope("grand", 10.0, 12.0, parent="w2", depth=2)
        )
        slots = _slots(run_model(events))
        assert (slots["w0"], slots["w1"], slots["w2"]) == (1, 2, 3)
        assert slots["grand"] > slots["w2"], "child drawn left of its own parent"

    def test_missing_parent_and_depth_defaults_to_flat(self):
        """Resume from a pre-nesting sidecar (no parent/depth on the wire) must
        still render: everything lands in slot 0, i.e. the old picture."""
        m = run_model(
            [
                {"type": "scope_start", "ts": 0.0, "task_id": "a", "kind": "skill"},
                {"type": "scope_end", "ts": 4.0, "task_id": "a"},
                {"type": "scope_start", "ts": 5.0, "task_id": "b", "kind": "run"},
                {"type": "scope_end", "ts": 6.0, "task_id": "b"},
            ]
        )
        assert set(_slots(m).values()) == {0}
        assert m["maxSlot"] == 0

    def test_teammate_work_span_has_no_slot_effect(self):
        """A resident agent's work lives in its OWN lane, so it must not widen
        main's column."""
        m = run_model(
            [
                {
                    "type": "scope_start",
                    "ts": 1.0,
                    "task_id": "w1#0",
                    "kind": "run",
                    "label": "do",
                },
                {"type": "scope_end", "ts": 3.0, "task_id": "w1#0"},
            ]
        )
        assert m["maxSlot"] == 0
        assert m["skillBands"] == [] and m["oneshots"] == []


class TestScopeForks:
    """A parallel batch shares ONE start timestamp (stamped by the spawning
    thread), so "same parent + same t0 + ≥2 members" identifies a fan-out with
    no extra field on the wire. The view draws one fork instead of N stubs.
    """

    def test_shared_start_becomes_one_fork(self):
        events = (
            _scope("sk", 0.0, None, kind="skill")
            + _scope("w0", 2.0, 9.0, parent="sk", depth=1, index=0)
            + _scope("w1", 2.0, 7.0, parent="sk", depth=1, index=1)
            + _scope("w2", 2.0, 8.0, parent="sk", depth=1, index=2)
        )
        m = run_model(events)
        assert len(m["forks"]) == 1
        f = m["forks"][0]
        assert f["parent"] == "sk" and f["t0"] == 2.0
        assert sorted(f["members"]) == ["w0", "w1", "w2"]
        assert sorted(f["slots"]) == [1, 2, 3]

    def test_sequential_children_are_not_a_fork(self):
        events = (
            _scope("sk", 0.0, None, kind="skill")
            + _scope("a", 2.0, 4.0, parent="sk", depth=1)
            + _scope("b", 5.0, 7.0, parent="sk", depth=1)
        )
        assert run_model(events)["forks"] == []

    def test_same_time_different_parents_are_separate(self):
        """Coincidence must not be read as a batch: only a SHARED parent plus
        the shared stamp means one fan-out."""
        events = (
            _scope("p1", 0.0, None, kind="skill")
            + _scope("p2", 0.0, None, kind="skill", parent="p1", depth=1)
            + _scope("a", 2.0, 4.0, parent="p1", depth=1)
            + _scope("b", 2.0, 4.0, parent="p2", depth=2)
        )
        assert run_model(events)["forks"] == []

    def test_top_level_batch_forks_from_main(self):
        """A batch fired straight from main (parent "") is the COMMON batch
        shape — it forks as well, anchored on main's own baseline by the view."""
        events = _scope("w0", 1.0, 3.0, depth=0, index=0) + _scope(
            "w1", 1.0, 4.0, depth=0, index=1
        )
        m = run_model(events)
        assert len(m["forks"]) == 1
        assert m["forks"][0]["parent"] == "" and m["forks"][0]["t0"] == 1.0
        assert sorted(m["forks"][0]["members"]) == ["w0", "w1"]
        assert sorted(_slots(m).values()) == [0, 1]  # both visible

    def test_lone_top_level_scope_is_not_a_fork(self):
        """One scope under main must not become a one-member fork."""
        m = run_model(_scope("w0", 1.0, 3.0))
        assert m["forks"] == []

    def test_two_batches_are_two_forks(self):
        events = _scope("sk", 0.0, None, kind="skill")
        for i in range(2):
            events += _scope(f"a{i}", 2.0, 5.0, parent="sk", depth=1, index=i)
        for i in range(2):
            events += _scope(f"b{i}", 8.0, 11.0, parent="sk", depth=1, index=i)
        forks = run_model(events)["forks"]
        assert [f["t0"] for f in forks] == [2.0, 8.0]
        assert all(len(f["members"]) == 2 for f in forks)


class TestUserLane:
    """v8.2.0 — the ONE multiplexed user lane + complete attribution."""

    def _um(self, ts, author, content="hello"):
        e = {"type": "user_message", "ts": ts, "content": content}
        if author:
            e["author"] = author
        return e

    def _final(self, ts, answers, text="done"):
        e = {"type": "assistant_turn", "ts": ts, "turn": 3, "final": text}
        if answers is not None:
            e["answers"] = answers
        return e

    def test_user_message_with_author_makes_lane_mark_and_request_arrow(self):
        m = run_model([self._um(1.0, "Bob", "fix the bug")])
        assert m["lanes"][0] == "user"
        assert m["userMarks"] == [{"t": 1.0, "who": "Bob", "text": "fix the bug"}]
        [msg] = m["messages"]
        assert msg["from"] == "user" and msg["to"] == "main"
        assert msg["type"] == "request" and msg["who"] == "Bob"

    def test_authorless_user_message_stays_off_the_lane(self):
        # 🤝 agent-report starter / CLI history: card only, no user lane.
        m = run_model([self._um(1.0, None)])
        assert m["userMarks"] == [] and m["messages"] == []
        assert "user" not in m["lanes"]

    def test_multiple_users_multiplex_into_one_lane(self):
        m = run_model([self._um(1.0, "Bob"), self._um(2.0, "두정")])
        assert m["lanes"].count("user") == 1
        assert [u["who"] for u in m["userMarks"]] == ["Bob", "두정"]

    def test_final_with_answers_draws_one_reply_arrow_with_all_names(self):
        # A run Bob started and 두정 steered mid-run: ONE dashed arrow into the
        # shared lane, labeled with every recipient — not an arrow per user.
        m = run_model(
            [
                self._um(1.0, "Bob"),
                self._um(2.0, "두정"),
                self._final(3.0, ["Bob", "두정"], "answer text"),
            ]
        )
        replies = [x for x in m["messages"] if x.get("reply")]
        assert len(replies) == 1
        assert replies[0]["from"] == "main" and replies[0]["to"] == "user"
        assert replies[0]["who"] == "Bob·두정"
        assert replies[0]["text"] == "answer text"
        assert m["finals"] == [
            {"t": 3.0, "answers": ["Bob", "두정"], "text": "answer text"}
        ]

    def test_final_with_empty_answers_has_no_reply_arrow(self):
        # 🤝 agent-report run: the final is recorded (dock shows it,
        # unattributed) but no arrow points at any user.
        m = run_model([self._um(1.0, "Bob"), self._final(2.0, [])])
        assert [x for x in m["messages"] if x.get("reply")] == []
        assert m["finals"][0]["answers"] == []

    def test_final_without_answers_field_is_pre_attribution(self):
        # Sessions recorded before v8.2.0 replay finals with NO answers key —
        # same rendering as empty (no arrow), never a crash.
        m = run_model([self._final(2.0, None)])
        assert m["finals"] == [{"t": 2.0, "answers": [], "text": "done"}]
        assert [x for x in m["messages"] if x.get("reply")] == []

    def test_scoped_final_is_not_a_main_reply(self):
        # A sub-agent's final carries task_id → not main's answer to anyone.
        e = self._final(2.0, ["Bob"])
        e["task_id"] = "t1"
        m = run_model([self._um(1.0, "Bob"), e])
        assert m["finals"] == []
        assert [x for x in m["messages"] if x.get("reply")] == []

    def test_final_does_not_pollute_main_turn_spans(self):
        # finals divert from the mainBusyFrom span tracker — a lone final must
        # not open a phantom main-activity span.
        m = run_model([self._um(1.0, "Bob"), self._final(2.0, ["Bob"])])
        assert m["mainSpans"] == []

    def test_user_lane_is_leftmost_with_agents_present(self):
        events = [
            {
                "type": "agent_roster",
                "ts": 0.5,
                "roster": [{"key": "w1", "profile": "code-writer"}],
            },
            self._um(1.0, "Bob"),
        ]
        m = run_model(events)
        assert m["lanes"] == ["user", "main", "w1"]
