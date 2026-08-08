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
 *   scope_start        {task_id,kind:"skill"|"run",label,agent,index,parent,depth}
 *                          kind="skill" → a skill band; kind="run" → one-shot
 *                          parent/depth → containment (see SLOTS below)
 *   scope_end          {task_id}                                 → closes it
 *   assistant_turn     {turn,...} with NO task_id                → main activity
 *                          …with ``final`` + ``answers`` → a complete answering
 *                          the listed users (reply arrow main → user lane)
 *   user_message       {content,author?}                          → user lane
 *                          author = sending user's nickname; absent for a 🤝
 *                          agent-report starter (no user to attribute)
 *
 * Output model:
 *   { lanes:[key...],   // "user" first (when any user activity), then "main",
 *                       // then agents by first-seen. ONE multiplexed user
 *                       // lane: every web user shares it, told apart by name.
 *     agents: Map(key → {key,label,role,profile,state,firstTs,lastTs,spans}),
 *     messages:[{from,to,t,type,text,who?,reply?}], // deduped canonical sends;
 *                       // who = user nickname(s) on user-lane arrows,
 *                       // reply:true = main→user complete (dashed in view)
 *     userMarks:[{t,who,text}],             // 👤 points on the user lane
 *     finals:[{t,answers:[who…],text}],     // main-timeline completes (dock/nav)
 *     oneshots:[{caller,label,role,t0,t1,task_id,parent,depth,slot}], skillBands:[…],
 *     mainSpans:[{t0,t1,title}],            // main's own turns
 *     forks:[{parent,t0,members:[task_id],slots:[…]}],  // batch fan-outs
 *     maxSlot,                              // widest slot in the caller column
 *     t0, t1 }                              // time domain (epoch seconds)
 *
 * SLOTS — skill bands and one-shot runs all live in their CALLER's column (they
 * block it), so a nested scope used to be painted at the same x and width as its
 * parent, i.e. invisible. Each gets a slot instead, drawn as a horizontal offset:
 *
 *   slot = the smallest k with k >= depth AND k > parent's slot, such that no
 *          already-placed scope in slot k overlaps this one in time.
 *
 * ``k >= depth`` indents by nesting level; ``k > parent's slot`` keeps a child
 * strictly right of its parent even when the parent was itself pushed sideways;
 * the overlap test only ever fires for CONCURRENT siblings, which is the rare
 * parallel-batch case (skill / single agent run block their caller, so ordinary
 * siblings are back-to-back and simply reuse the same slot). Placement order is
 * (t0, index) so a batch's slots follow worker index — stable across replays.
 *
 * FORKS — a parallel batch shares ONE start timestamp (the spawning code stamps
 * it once, see Renderer.begin_scope), so "same parent + same t0 + 2 or more
 * members" identifies a fan-out without any extra field on the wire. The view
 * draws one fork from the parent instead of N unrelated connector stubs.
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

  // Peer messages stamp the sender as ``agent:<key>`` (the message handler's
  // author), but lane keys are the BARE agent key. Strip the prefix so a
  // reply addressed to "agent:orch" maps to the "orch" lane instead of being
  // dropped (the "request shows, reply doesn't" bug).
  function laneKey(x) {
    return typeof x === "string" && x.indexOf("agent:") === 0 ? x.slice(6) : x;
  }

  // user→* = request, *→user = reply, main→agent = assign,
  // agent→main = report, agent→agent = message.
  function msgType(from, to) {
    if (from === "user") return "request";
    if (to === "user") return "reply";
    if (to === "main") return "report";
    if (from === "main") return "assign";
    return "message";
  }

  function build(events, now) {
    var agents = new Map(); // key → lane record
    var messages = [];
    var userMarks = []; // 👤 points on the single multiplexed user lane
    var finals = []; // main-timeline completes (reply arrows + dock nav)
    var oneshots = [];
    var skillBands = [];
    var mainSpans = [];
    var openRun = new Map(); // task_id → open scope {kind, t0, label, key|caller}
    var seenMsg = new Set(); // dedupe (author,to,seq,ts)
    function closeScope(o) {
      if (o.kind === "skill") skillBands.push(o);
      else if (o.kind === "work") {
        var ag = agents.get(o.key);
        // Keep task_id so the view can link the bar to its timeline card
        // (``.card-task-group[data-task-id]``) for click-to-navigate.
        if (ag) ag.spans.push({ t0: o.t0, t1: o.t1, title: o.label, task_id: o.task_id });
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
      // A roster is a STATE snapshot, not an activity time-point, and it is
      // stamped with wall-clock-now on every emit. Bumping the time domain with
      // it would drag the axis (and any open scope, which closes at tMax) out to
      // "now" — reintroducing the moving-now behavior the event-ordinal view
      // drops. Only real activity (messages, scopes, turns) defines the domain.
      if (e.type !== "agent_roster") bump(ts);

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
          var from = laneKey(e.author || e.key || "main");
          var to = laneKey(e.to || "main");
          var sig = from + " " + to + " " + (e.seq || 0) + " " + (ts || 0);
          if (!seenMsg.has(sig)) {
            seenMsg.add(sig);
            messages.push({ from: from, to: to, t: ts, type: msgType(from, to), text: e.text || "" });
          }
        } else if (e.direction === "in") {
          // A request ARRIVING at a teammate. Draw the request leg of the
          // round-trip ONLY when it comes from main / a web user ("user:*"):
          // those senders emit no "out" of their own, so this is the only
          // record of that arrow. A PEER-originated inbound is skipped — the
          // sender's own "out" already draws it, so drawing the "in" too would
          // double the arrow.
          var inAuthor = laneKey(e.author || "main");
          if (inAuthor === "main" || inAuthor.indexOf("user") === 0) {
            var inTo = laneKey(e.key || "main");
            var inSig = inAuthor + " " + inTo + " " + (e.seq || 0) + " in";
            // A "user:<nick>" author maps onto the ONE multiplexed user lane,
            // keeping the nickname as the arrow label. (These arrows used to
            // be dropped silently — "user:…" was no lane key.)
            var inFrom = inAuthor;
            var inWho = "";
            if (inAuthor.indexOf("user") === 0) {
              inFrom = "user";
              inWho = inAuthor.indexOf("user:") === 0 ? inAuthor.slice(5) : "";
              if (inWho) userMarks.push({ t: ts, who: inWho, text: e.text || "" });
            }
            if (!seenMsg.has(inSig)) {
              seenMsg.add(inSig);
              messages.push({
                from: inFrom,
                to: inTo,
                t: ts,
                type: msgType(inFrom, inTo),
                who: inWho,
                text: e.text || "",
              });
            }
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
          openRun.set(tid, {
            kind: "skill",
            t0: ts,
            label: e.label || "skill",
            caller: "main",
            task_id: tid,
            parent: e.parent || "",
            depth: e.depth || 0,
            index: e.index || 0,
          });
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
            parent: e.parent || "",
            depth: e.depth || 0,
            index: e.index || 0,
          });
        }
      } else if (e.type === "scope_end") {
        var o = openRun.get(e.task_id);
        if (o) {
          o.t1 = ts;
          closeScope(o);
          openRun.delete(e.task_id);
        }
      } else if (e.type === "user_message") {
        // Author present = a real USER message (starter / steering injection /
        // ask answer) → a 👤 mark on the multiplexed user lane + a request
        // arrow into main. Author absent (🤝 agent-report starter, CLI replay)
        // = a timeline card only, nothing on the user lane.
        if (e.author) {
          userMarks.push({ t: ts, who: e.author, text: e.content || "" });
          messages.push({
            from: "user",
            to: "main",
            t: ts,
            type: "request",
            who: e.author,
            text: e.content || "",
          });
        }
      } else if (e.type === "assistant_turn" && !e.task_id) {
        if (e.final !== undefined) {
          // A main-timeline complete. ``answers`` = the users whose asks this
          // run serviced (starter + steering injections, loop-owned). Non-empty
          // → ONE dashed reply arrow into the shared user lane, labeled with
          // every recipient. Empty/missing → no arrow (agent-report run or a
          // pre-attribution session).
          var ans = Array.isArray(e.answers) ? e.answers : [];
          finals.push({ t: ts, answers: ans, text: e.final || "" });
          if (ans.length) {
            messages.push({
              from: "main",
              to: "user",
              t: ts,
              type: "reply",
              reply: true,
              who: ans.join("·"),
              text: e.final || "",
            });
          }
        } else if (mainBusyFrom == null) {
          // main's own turn — a short activity span (start..next event bump).
          mainBusyFrom = ts;
        } else {
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

    // ── slots: lay the caller-column scopes out sideways (see SLOTS above) ──
    var column = skillBands.concat(oneshots).sort(function (x, y) {
      return (x.t0 - y.t0) || ((x.index || 0) - (y.index || 0));
    });
    var placed = {}; // slot → [{t0,t1}]
    var slotOf = {}; // task_id → slot
    var maxSlot = 0;
    column.forEach(function (it) {
      var floor = it.depth || 0;
      // A parent already placed further right (it was itself in a batch) pushes
      // its child past it, so containment always reads left-to-right.
      var ps = it.parent ? slotOf[it.parent] : null;
      if (ps != null && ps + 1 > floor) floor = ps + 1;
      var k = floor;
      for (;;) {
        var taken = (placed[k] || []).some(function (p) {
          // Same START instant ⇒ concurrent, full stop: one scope cannot have
          // finished before the other began. This is not just a shortcut — a
          // batch whose members are ALL still open closes at tMax, which IS
          // their shared start when nothing has happened since, making every
          // span zero-length. A strict interval test then finds no overlap and
          // the whole live batch collapses into one slot (one visible bar).
          return p.t0 === it.t0 || (p.t0 < it.t1 && it.t0 < p.t1);
        });
        if (!taken) break;
        k++;
      }
      it.slot = k;
      slotOf[it.task_id] = k;
      (placed[k] = placed[k] || []).push({ t0: it.t0, t1: it.t1 });
      if (k > maxSlot) maxSlot = k;
    });

    // ── forks: same parent + same shared start = one parallel batch ──
    // A batch straight from main (parent "") counts too: the SHARED timestamp is
    // what identifies a fan-out — the spawning thread stamps it once for all of
    // its workers — and two independent scopes cannot collide on a microsecond
    // clock. The view anchors a parentless fork on the caller's slot-0 baseline.
    var forkBy = {};
    column.forEach(function (it) {
      var key = it.parent + " " + it.t0;
      (forkBy[key] = forkBy[key] || []).push(it);
    });
    var forks = [];
    Object.keys(forkBy).forEach(function (key) {
      var g = forkBy[key];
      if (g.length < 2) return;
      forks.push({
        parent: g[0].parent,
        t0: g[0].t0,
        members: g.map(function (x) { return x.task_id; }),
        slots: g.map(function (x) { return x.slot; }),
      });
    });
    forks.sort(function (a, b) { return a.t0 - b.t0; });

    var ordered = Array.from(agents.values()).sort(function (x, y) {
      return (x.firstTs || 0) - (y.firstTs || 0);
    });
    var lanes = ["main"].concat(ordered.map(function (a) {
      return a.key;
    }));
    // The ONE multiplexed user lane, leftmost — only when some user activity
    // exists, so an author-less session (CLI recording, pre-author history)
    // keeps its old lane set instead of gaining an empty column.
    if (userMarks.length) lanes.unshift("user");

    return {
      lanes: lanes,
      agents: agents,
      messages: messages,
      userMarks: userMarks,
      finals: finals,
      oneshots: oneshots,
      skillBands: skillBands,
      mainSpans: mainSpans,
      forks: forks,
      maxSlot: maxSlot,
      t0: isFinite(tMin) ? tMin : 0,
      t1: tEnd,
    };
  }

  var api = { build: build, hueFor: hueFor, _msgType: msgType, _clip: clip, _toEpoch: toEpoch };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  global.TeamModel = api;
})(typeof window !== "undefined" ? window : globalThis);
