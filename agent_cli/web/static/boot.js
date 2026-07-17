// agent-cli web — connection admission gate (runs BEFORE the app).
//
// Browsers allow only 6 concurrent HTTP/1.1 connections per origin
// (profile-wide, across every window and tab), and each live web-UI tab
// holds one SSE stream. An unchecked "6th tab" therefore saturates the
// pool and silently freezes EVERY tab on the origin — page loads spin
// forever, confirm clicks queue invisibly (the v7.2.0 confirm-starvation
// incident). agent-board's open-button gate can't see tabs that arrive
// directly (typed URL, session restore, tab duplication), so the page
// polices itself:
//
//   1. Count the origin's connection-holding tabs over the same
//      BroadcastChannel the board uses (parked tabs answer held:false
//      and are excluded — they hold nothing).
//   2. Under the cap → load app.js (which opens the SSE) and claim the
//      slot. At the cap → PARK: render a notice instead of connecting,
//      and retry automatically until a slot frees up.
//
// The invariant "held connections ≤ 5" becomes self-enforcing, so one
// slot always stays free and a parked tab's own page load never starves.
(function () {
  "use strict";
  var MAX_HELD_TABS = 5; // 6th held connection = saturated pool
  var RETRY_MS = 5000;
  var held = false;

  function loadApp() {
    var s = document.createElement("script");
    s.src = "static/app.js";
    document.body.appendChild(s);
  }

  if (typeof BroadcastChannel === "undefined") {
    loadApp(); // can't count — behave like before the gate existed
    return;
  }

  // Presence responder — single owner of the channel protocol. Other
  // tabs' counters (and the board dashboard) ping; we answer with
  // whether THIS tab is actually holding a connection. `path` lets the
  // board recognise "a tab for this room already exists" (named-window
  // reuse → no new connection → its gate is waived).
  var ch = new BroadcastChannel("agentcli_tab_presence");
  ch.addEventListener("message", function (e) {
    var d = e.data || {};
    if (d.type === "ping") {
      ch.postMessage({
        type: "pong",
        nonce: d.nonce,
        path: location.pathname,
        held: held,
      });
    }
  });

  function countHeld() {
    return new Promise(function (resolve) {
      var counter = new BroadcastChannel("agentcli_tab_presence");
      var nonce = String(Date.now()) + Math.random();
      var n = 0;
      counter.addEventListener("message", function (e) {
        var d = e.data || {};
        // No `held` field → an older tab or the board dashboard; both
        // always hold a connection, so count them.
        if (d.type === "pong" && d.nonce === nonce && d.held !== false) n++;
      });
      counter.postMessage({ type: "ping", nonce: nonce });
      setTimeout(function () {
        counter.close();
        resolve(n);
      }, 150);
    });
  }

  // Shared surface for app.js (crowd banner) — the channel protocol
  // lives here, consumers count through this.
  window.AgentCliPresence = {
    countHeld: countHeld,
    setHeld: function (v) {
      held = !!v;
    },
  };

  var parked = null;

  function park(count) {
    if (!parked) {
      parked = document.createElement("div");
      parked.id = "conn-parked";
      var box = document.createElement("div");
      box.className = "parked-box";
      var msg = document.createElement("p");
      msg.id = "conn-parked-msg";
      box.appendChild(msg);
      var btn = document.createElement("button");
      btn.type = "button";
      btn.className = "btn-primary";
      btn.textContent = "Retry now";
      btn.addEventListener("click", attempt);
      box.appendChild(btn);
      parked.appendChild(box);
      document.body.appendChild(parked);
    }
    parked.querySelector("#conn-parked-msg").textContent =
      "⚠ Connection limit — " + count + " tabs on this host already " +
      "hold live connections (browser cap: 6 per host). This tab is " +
      "parked so the others keep working; it connects automatically " +
      "when a slot frees up. Closing an unused tab admits it instantly.";
  }

  function attempt() {
    countHeld().then(function (n) {
      if (n < MAX_HELD_TABS) {
        held = true; // claim the slot before the SSE actually opens
        if (parked) {
          parked.remove();
          parked = null;
        }
        loadApp();
      } else {
        park(n);
        // jitter so two parked tabs don't re-check in lockstep and
        // admit into the same freed slot together
        setTimeout(attempt, RETRY_MS + Math.random() * 2000);
      }
    });
  }

  attempt();
})();
