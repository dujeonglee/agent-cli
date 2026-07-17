// agent-cli web — connection admission gate (runs BEFORE the app).
//
// Browsers allow only 6 concurrent HTTP/1.1 connections per origin
// (profile-wide, across every window and tab), and each live web-UI tab
// holds one SSE stream. An unchecked "6th tab" therefore saturates the
// pool and silently freezes EVERY tab on the origin — page loads spin
// forever, confirm clicks queue invisibly (the v7.2.0 confirm-starvation
// incident). agent-board's open-button gate can't see tabs that arrive
// directly (typed URL, session restore, tab duplication), so the page
// polices itself at the CONNECTION, not the URL.
//
// v7.6.0: admission is arbitrated with the Web Locks API instead of
// BroadcastChannel ping/pong sampling. Sampling was inherently racy —
// a 150ms collection window undercounts under load (a "6th" tab slips
// in and saturates the pool) and a just-closed tab's dying context can
// still pong (overcount → retry seems stuck). Named slot locks fix all
// of it structurally:
//   - acquisition is ATOMIC (two tabs can never share a slot),
//   - the browser releases a tab's lock the INSTANT it closes/navigates,
//   - a parked tab queues lock requests and is woken by the browser the
//     moment a slot frees — no polling, no timers, no retry button.
//
// SLOTS = 5 of the 6: one connection is deliberately kept free so page
// loads and API calls (confirm clicks!) always have a slot to run on.
// "agentcli-conn-slot-<i>" is a cross-repo protocol constant — the
// agent-board dashboard holds one for its own SSE and counts the same
// names for its open-button gate.
//
// BroadcastChannel stays only as a presence beacon (pong {path, held}):
// the board uses `path` to spot "a tab for this room already exists"
// (named-window reuse), and pre-Web-Locks peers still count pongs.
(function () {
  "use strict";
  var SLOTS = 5; // of the browser's 6 per-origin connections, keep 1 free
  var LOCK_PREFIX = "agentcli-conn-slot-";
  var held = false;

  function loadApp() {
    var s = document.createElement("script");
    s.src = "static/app.js";
    document.body.appendChild(s);
  }

  var hasLocks =
    typeof navigator !== "undefined" &&
    navigator.locks &&
    typeof navigator.locks.request === "function";

  // ── Presence beacon (kept from v7.3.0) ──
  if (typeof BroadcastChannel !== "undefined") {
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
  }

  // Exact held-slot count via lock introspection (no sampling window).
  function countHeld() {
    if (!hasLocks) return Promise.resolve(0);
    return navigator.locks.query().then(
      function (state) {
        var n = 0;
        (state.held || []).forEach(function (l) {
          if (l.name && l.name.indexOf(LOCK_PREFIX) === 0) n++;
        });
        return n;
      },
      function () {
        return 0;
      }
    );
  }

  // Shared surface for app.js (crowd banner) and anyone else.
  window.AgentCliPresence = {
    countHeld: countHeld,
    setHeld: function (v) {
      held = !!v;
    },
  };

  if (!hasLocks) {
    loadApp(); // can't arbitrate — behave like before the gate existed
    return;
  }

  var parked = null;

  function admit() {
    held = true; // beacon now answers "holding"
    if (parked) {
      parked.remove();
      parked = null;
    }
    loadApp();
  }

  function park(count) {
    if (!parked) {
      parked = document.createElement("div");
      parked.id = "conn-parked";
      var box = document.createElement("div");
      box.className = "parked-box";
      var msg = document.createElement("p");
      msg.id = "conn-parked-msg";
      box.appendChild(msg);
      parked.appendChild(box);
      document.body.appendChild(parked);
    }
    parked.querySelector("#conn-parked-msg").textContent =
      "⚠ Connection slots full — " + count + " tabs on this host " +
      "(rooms and the board dashboard) already hold live connections. " +
      "Browsers cap HTTP/1.1 at 6 per host and one slot is kept free " +
      "so pages and clicks keep working, leaving " + SLOTS + " for " +
      "tabs. This tab connects AUTOMATICALLY the instant any other " +
      "tab closes — no reload needed.";
  }

  // Try each slot without waiting. The callback holds the lock forever
  // (never-settling promise) — the browser releases it when the tab
  // closes or navigates away.
  function acquireAny() {
    return new Promise(function (resolve) {
      var i = 0;
      function tryNext() {
        if (i >= SLOTS) {
          resolve(false);
          return;
        }
        var name = LOCK_PREFIX + i;
        i += 1;
        navigator.locks
          .request(name, { ifAvailable: true }, function (lock) {
            if (lock) {
              resolve(true);
              return new Promise(function () {}); // hold forever
            }
            tryNext();
          })
          .catch(function () {
            tryNext();
          });
      }
      tryNext();
    });
  }

  // Parked: queue a request on EVERY slot; the browser grants the first
  // one that frees. One controller per slot so cancelling the losers can
  // never touch the winner's granted lock.
  function waitForSlot() {
    var ctrls = [];
    var won = -1;
    var request = function (i) {
      var ac = new AbortController();
      ctrls[i] = ac;
      navigator.locks
        .request(LOCK_PREFIX + i, { signal: ac.signal }, function () {
          if (won !== -1) {
            // Another slot already admitted us — release this one by
            // returning immediately (undefined → lock dropped).
            return;
          }
          won = i;
          ctrls.forEach(function (c, j) {
            if (j !== i) c.abort();
          });
          admit();
          return new Promise(function () {}); // hold forever
        })
        .catch(function () {
          /* aborted — we won on another slot */
        });
    };
    for (var i = 0; i < SLOTS; i++) request(i);
  }

  acquireAny().then(function (got) {
    if (got) {
      admit();
    } else {
      countHeld().then(park);
      waitForSlot();
    }
  });
})();
