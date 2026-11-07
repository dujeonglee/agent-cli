// ── Tab-presence beacon ─────────────────────
// Every live web-UI tab holds one SSE connection out of the browser's
// 6-connections-per-origin (HTTP/1.1) pool. Under agent-board's
// board-proxy gateway ALL rooms share one origin, so the board's
// dashboard gates "open a new room" on how many tabs are already
// holding connections — it asks over a same-origin BroadcastChannel
// and each tab answers here (the v7.2.0 confirm-starvation incident).
// `path` lets the board spot "a tab for this room already exists"
// (named-window reuse → no new connection → its gate is waived).
// Operating premise (v7.7.0): rooms are ALWAYS opened through the
// board, so the board-side gate is the only admission control — the
// per-tab parking gate (v7.5/7.6) was dropped (Web Locks needs a
// secure context the plain-http LAN deployment doesn't have, and
// direct-URL entry is out of scope by policy). Direct per-port use:
// each instance is its own origin, so the channel has no other
// members and this stays inert.
if (typeof BroadcastChannel !== "undefined") {
  const ch = new BroadcastChannel("agentcli_tab_presence");
  ch.addEventListener("message", function (e) {
    const d = e.data || {};
    if (d.type === "ping") {
      ch.postMessage({
        type: "pong",
        nonce: d.nonce,
        path: location.pathname,
      });
    }
  });
}
