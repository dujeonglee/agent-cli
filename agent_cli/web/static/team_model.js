/* team_model.js — pure derivation of the "Team" swimlane model from the SSE
 * event stream. NO DOM, NO globals beyond the single export — so it can be
 * unit-driven with synthetic events and asserted on observable output.
 *
 * Inputs are the same event objects app.js receives from the server, each an
 * object with a ``type`` plus its payload and a ``ts`` (epoch seconds, or an
 * ISO string). The events the swimlane consumes:
 *   agent_roster       {roster:[{key,profile,name,state,...}]}   → lanes + state
 *   agent_msg          {key,direction:"in"|"out",author,to,text,ts,seq}
 *                          direction "out" = the canonical send (from=author→to);
 *                          direction "in"  = a request arriving at ``key`` (opens
 *                          that agent's busy span).
 *   scope_start        {task_id,kind:"skill"|"run",label,agent,index}
 *                          kind="skill" → a skill band; kind="run" → one-shot
 *   scope_end          {task_id}                                 → closes it
 *   assistant_turn     {turn,...} with NO task_id                → main activity
 *
 * Output model:
 *   { lanes:[key...],                       // "main" first, agents by first-seen
 *     agents: Map(key → {key,label,role,profile,state,firstTs,lastTs,spans}),
 *     messages:[{from,to,t,type,text}],     // deduped canonical sends
 *     oneshots:[{caller,label,role,t0,t1,task_id}], skillBands:[…],
 *     mainSpans:[{t0,t1,title}],            // main's own turns
 *     t0, t1 }                              // time domain (epoch seconds)
 *
 * Reconnect note: agent_msg is persistent (replayed), so messages and the
 * request→reply spans they bound survive a reconnect. agent_roster is a sticky
 * (latest only), so lane identity/current state is restored.
 *
 * Resume note: scope_start/scope_end (the activity bars) have no counterpart in
 * the LLM message history, so the server logs them to a scopes.jsonl sidecar and
 * replay_scopes() re-emits them on resume (tagged replay:true) — the bars come
 * back on a resumed session, not just a reconnect. Dedup in team_view.js keeps
 * the replay idempotent.
 */
(function (global) {
  "use strict";

  // profile → CSS custom-property name for the lane hue. Unknown profiles fall
  // back to a neutral worker hue so a custom agent still gets a stable color.
  var HUE = {
    orchestrator: "--h-orch",
    "code-writer": "--h-writer",
    "code-reviewer": "--h-reviewer",
    "unittest-writer": "--h-tester",
    "code-analyst": "--h-reviewer",
    "log-analyst": "--h-rose",
  };
  function hueFor(profile) {
    return HUE[profile] || "--h-worker";
  }

  function toEpoch(ts) {
    if (ts == null) return null;
    if (typeof ts === "number") return ts;
    var n = Date.parse(ts);
    return isNaN(n) ? null : n / 1000;
  }

  function clip(s, n) {
    s = (s || "").replace(/\s+/g, " ").trim();
    n = n || 48;
    return s.length > n ? s.slice(0, n - 1) + "…" : s;
  }

  // main→agent = assign, agent→main = report, agent→agent = message.
  function msgType(from, to) {
    if (to === "main") return "report";
    if (from === "main") return "assign";
    return "message";
  }

  function build(events, now) {
    var agents = new Map(); // key → lane record
    var messages = [];
    var oneshots = [];
    var skillBands = [];
    var mainSpans = [];
    var openRun = new Map(); // task_id → open scope {kind, t0, label, key|caller}
    var seenMsg = new Set(); // dedupe (author,to,seq,ts)
    function closeScope(o) {
      if (o.kind === "skill") skillBands.push(o);
      else if (o.kind === "work") {
        var ag = agents.get(o.key);
        if (ag) ag.spans.push({ t0: o.t0, t1: o.t1, title: o.label });
      } else oneshots.push(o);
    }
    var mainBusyFrom = null;
    var tMin = Infinity,
      tMax = -Infinity;

    function bump(ts) {
      if (ts == null) return;
      if (ts < tMin) tMin = ts;
      if (ts > tMax) tMax = ts;
    }
    function ensure(key, profile, name) {
      if (!key || key === "main") return null;
      var a = agents.get(key);
      if (!a) {
        a = {
          key: key,
          profile: profile || "",
          name: name || "",
          label: name || key,
          role: profile || "",
          state: "idle",
          firstTs: null,
          lastTs: null,
          spans: [],
        };
        agents.set(key, a);
      }
      if (profile && !a.profile) {
        a.profile = profile;
        a.role = profile;
      }
      if (name && !a.name) {
        a.name = name;
        a.label = name;
      }
      return a;
    }

    for (var i = 0; i < events.length; i++) {
      var e = events[i];
      if (!e || !e.type) continue;
      var ts = toEpoch(e.ts);
      bump(ts);

      if (e.type === "agent_roster") {
        var roster = e.roster || (e.data && e.data.roster) || [];
        for (var j = 0; j < roster.length; j++) {
          var r = roster[j];
          var a = ensure(r.key, r.profile, r.name);
          if (a) {
            if (r.state) a.state = r.state;
            if (a.firstTs == null) a.firstTs = ts;
            a.lastTs = ts;
          }
        }
      } else if (e.type === "agent_msg") {
        // Messages come from the canonical "out" send. WORK SPANS come from the
        // scope events (begin_agent_work), NOT from messages — so a busy agent
        // shows as a bar in ITS own lane instead of flooding main's lane.
        if (e.direction === "out") {
          var from = e.author || e.key || "main";
          var to = e.to || "main";
          var sig = from + " " + to + " " + (e.seq || 0) + " " + (ts || 0);
          if (!seenMsg.has(sig)) {
            seenMsg.add(sig);
            messages.push({ from: from, to: to, t: ts, type: msgType(from, to), text: e.text || "" });
          }
        }
      } else if (e.type === "scope_start") {
        // Route scopes by identity:
        //  - kind="skill"          → skill band in main's lane
        //  - task_id "{key}#{seq}" → begin_agent_work: a resident agent
        //      processing ONE request → a WORK SPAN in THAT agent's lane
        //  - else (kind="run")     → a true one-shot (main → agent run /
        //      delegate) → sub-work under main
        var tid = e.task_id || "";
        var hash = tid.indexOf("#");
        if (e.kind === "skill") {
          openRun.set(tid, { kind: "skill", t0: ts, label: e.label || "skill", caller: "main", task_id: tid });
        } else if (hash >= 0) {
          var akey = tid.slice(0, hash);
          ensure(akey);
          openRun.set(tid, { kind: "work", t0: ts, label: clip(e.label) || "work", key: akey, task_id: tid });
        } else {
          openRun.set(tid, {
            kind: "run",
            caller: "main",
            label: e.agent || e.label || "run",
            role: "one-shot run",
            t0: ts,
            task_id: tid,
          });
        }
      } else if (e.type === "scope_end") {
        var o = openRun.get(e.task_id);
        if (o) {
          o.t1 = ts;
          closeScope(o);
          openRun.delete(e.task_id);
        }
      } else if (e.type === "assistant_turn" && !e.task_id) {
        // main's own turn — a short activity span (start..next event bump).
        if (mainBusyFrom == null) mainBusyFrom = ts;
        else {
          mainSpans.push({ t0: mainBusyFrom, t1: ts, title: "turn" });
          mainBusyFrom = ts;
        }
      }
    }

    // close anything still open at the end of the trace
    if (!isFinite(tMax)) tMax = isFinite(tMin) ? tMin : 1;
    // If there is ONGOING work (open scopes / a live main turn) and a live
    // ``now`` is supplied, extend the domain + those open items to now so
    // in-progress bars grow and the axis advances between events. A finished
    // run (nothing open) keeps its last-event end — no dead space on the right.
    var hasOpen = openRun.size > 0 || mainBusyFrom != null;
    var tEnd = now != null && hasOpen ? Math.max(tMax, now) : tMax;
    for (var oo of openRun.values()) {
      oo.t1 = tEnd;
      closeScope(oo);
    }
    if (mainBusyFrom != null) mainSpans.push({ t0: mainBusyFrom, t1: tEnd, title: "turn" });

    var ordered = Array.from(agents.values()).sort(function (x, y) {
      return (x.firstTs || 0) - (y.firstTs || 0);
    });
    var lanes = ["main"].concat(ordered.map(function (a) {
      return a.key;
    }));

    return {
      lanes: lanes,
      agents: agents,
      messages: messages,
      oneshots: oneshots,
      skillBands: skillBands,
      mainSpans: mainSpans,
      t0: isFinite(tMin) ? tMin : 0,
      t1: tEnd,
    };
  }

  var api = { build: build, hueFor: hueFor, _msgType: msgType, _clip: clip, _toEpoch: toEpoch };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  global.TeamModel = api;
})(typeof window !== "undefined" ? window : globalThis);
