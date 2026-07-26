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
    oneshots:m.oneshots, skillBands:m.skillBands, mainSpans:m.mainSpans, t0:m.t0, t1:m.t1}));
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
        assert m["messages"][0]["from"] == "user:alice"
        assert m["messages"][0]["to"] == "w1"

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
