/* team_view.js — the live "Team" swimlane surface. Renders TeamModel (from
 * team_model.js) as an SVG timeline: one lane per agent, work spans as bars,
 * peer messages as connectors; a skill or one-shot run BLOCKS its caller
 * for its whole duration, so it renders as a distinctly-colored span INSIDE
 * the caller's lane (not a separate band/track).
 *
 * Theme-reactive: every color is a CSS custom property (var(--…)) — the app's
 * data-theme tokens plus role hues (--h-orch/writer/…) — so a theme switch
 * recolors the swimlane without a re-render.
 *
 * app.js feeds it: TeamView.ingest(type, payload) for each relevant SSE event
 * (agent_roster, agent_msg, scope_start, scope_end, assistant_turn). The view
 * re-derives the whole model and repaints (rAF-debounced). Full rebuild is
 * cheap for realistic team sizes and keeps state in ONE place (team_model.js).
 */
(function (global) {
  "use strict";
  var SVGNS = "http://www.w3.org/2000/svg";

  // profile → a small glyph for the lane chip (falls back to a gear).
  var GLYPH = {
    orchestrator: "🧭",
    "code-writer": "✍",
    "code-reviewer": "🔎",
    "code-analyst": "🗺",
    "unittest-writer": "🧪",
    "log-analyst": "📄",
  };
  function glyphFor(profile) {
    return GLYPH[profile] || "⚙";
  }
  function hueVar(profile) {
    return "var(" + (global.TeamModel ? global.TeamModel.hueFor(profile) : "--h-worker") + ")";
  }
  function fmtClock(sec) {
    sec = Math.max(0, Math.floor(sec));
    var m = Math.floor(sec / 60),
      s = sec % 60;
    return (m < 10 ? "0" : "") + m + ":" + (s < 10 ? "0" : "") + s;
  }

  function svg(tag, attrs, parent) {
    var n = document.createElementNS(SVGNS, tag);
    if (attrs) for (var k in attrs) n.setAttribute(k, attrs[k]);
    if (parent) parent.appendChild(n);
    return n;
  }
  function elh(tag, cls, txt) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (txt != null) n.textContent = txt;
    return n;
  }

  // Vertical (sequence-diagram) layout: time flows DOWN, one COLUMN per agent.
  //   HEAD_H  sticky column-header height (agent chips pin at the top)
  //   GUT_L   left gutter for time-axis (MM:SS) labels
  //   COL_MIN minimum column width (columns share the remaining width)
  //   BARW    work-bar / scope-span thickness
  //   CAP     compression cap: fit up to CAP seconds into the visible height,
  //           then freeze px/sec and let content grow → vertical scroll.
  var HEAD_H = 54,
    GUT_L = 50,
    PAD_T = 12,
    PAD_B = 20,
    PAD_R = 14,
    COL_MIN = 96,
    BARW = 10,
    CAP = 3600;

  var TeamView = {
    _events: [],
    _host: null,
    _svg: null,
    _active: false,
    _raf: 0,
    _poll: 0,
    _seen: null,

    /** Feed one SSE event into the model buffer. */
    ingest: function (type, data) {
      if (!this._seen) this._seen = {};
      // Reconnect replays the persistent buffer (roster sticky + scope_* +
      // agent_msg). Dedup by a stable key so a replayed event is NOT added
      // twice — this is why the view no longer needs to clear its buffer on
      // ``ready`` (that clear used to flash "no team activity yet" on every
      // reconnect). Roster is a full state snapshot → re-applying is already
      // idempotent, so it needs no key.
      var key = null;
      if (type === "scope_start" || type === "scope_end") {
        key = type + ":" + data.task_id;
      } else if (type === "agent_msg") {
        key =
          "msg:" + (data.author || "") + ":" + (data.to || "") +
          ":" + (data.seq || 0) + ":" + (data.direction || "");
      }
      if (key) {
        if (this._seen[key]) return;
        this._seen[key] = 1;
      }
      var e = { type: type };
      for (var k in data) e[k] = data[k];
      if (e.ts == null) e.ts = Date.now() / 1000;
      this._events.push(e);
      if (this._active) this._schedule();
    },

    reset: function () {
      this._events = [];
      this._seen = {};
      if (this._active) this._schedule();
    },

    /** Build the swimlane inside host and take ownership. ``host`` (#team-view)
     * is the vertical scroll container; a sticky header SVG (agent columns) pins
     * at the top while the tall plot SVG scrolls under it. */
    mount: function (host) {
      this._host = host;
      host.innerHTML = "";
      var self = this;
      // Sticky agent-column header (first child so position:sticky pins it).
      this._head = svg("svg", { class: "tv-head", role: "presentation" });
      host.appendChild(this._head);
      this._svg = svg("svg", { class: "tv-svg", role: "img", "aria-label": "Agent team swimlane" });
      host.appendChild(this._svg);
      this._empty = elh("div", "tv-empty", "No team activity yet — spawn agents or run /orchestrate.");
      host.appendChild(this._empty);
      // Stick to "now" (the bottom, since time flows down) unless the user has
      // scrolled up to read history. The scroll listener flips the flag; render
      // re-pins the bottom only while stuck.
      this._stick = true;
      host.addEventListener("scroll", function () {
        self._stick = host.scrollTop + host.clientHeight >= host.scrollHeight - 24;
      });
      // Custom fast tooltip (native SVG <title> has a ~0.5s+ delay that can't
      // be tuned). Delegated on the host once — survives re-renders and covers
      // both the header and plot SVGs. Any element with a data-tip attribute
      // shows it ~100ms after hover, near the cursor.
      this._tip = elh("div", "tv-tip");
      this._tip.hidden = true;
      document.body.appendChild(this._tip);
      var timer = 0,
        mx = 0,
        my = 0;
      function place() {
        // Default to the RIGHT of the cursor; flip to the LEFT when it would
        // overflow the viewport's right edge (and clamp so it never goes
        // off-screen either way, incl. the bottom edge).
        var w = self._tip.offsetWidth || 0;
        var h = self._tip.offsetHeight || 0;
        var left = mx + 12;
        if (left + w > window.innerWidth - 6) left = mx - 12 - w;
        if (left < 4) left = 4;
        var top = my + 14;
        if (top + h > window.innerHeight - 6) top = my - 12 - h;
        if (top < 4) top = 4;
        self._tip.style.left = left + "px";
        self._tip.style.top = top + "px";
      }
      host.addEventListener("mousemove", function (e) {
        mx = e.clientX;
        my = e.clientY;
        if (!self._tip.hidden) place();
      });
      host.addEventListener("mouseover", function (e) {
        var txt = e.target && e.target.getAttribute && e.target.getAttribute("data-tip");
        if (!txt) return;
        mx = e.clientX;
        my = e.clientY;
        clearTimeout(timer);
        timer = setTimeout(function () {
          self._tip.textContent = txt;
          self._tip.hidden = false;
          place();
        }, 100);
      });
      host.addEventListener("mouseout", function (e) {
        if (e.target && e.target.getAttribute && e.target.getAttribute("data-tip")) {
          clearTimeout(timer);
          self._tip.hidden = true;
        }
      });
      this._ro = new ResizeObserver(function () {
        if (self._active) self._schedule();
      });
      this._ro.observe(host);
    },

    setActive: function (on) {
      // Event-driven (ingest → rAF) PLUS a 5s tick while the Team view is open,
      // so in-progress bars grow to "now" and the time axis advances between
      // events. Load is negligible — a model rebuild + SVG redraw at 0.2 Hz,
      // and only while the view is visible.
      this._active = on;
      if (this._host) this._host.hidden = !on;
      if (on) {
        this.render();
        this._startPoll();
      } else {
        this._stopPoll();
      }
    },

    _startPoll: function () {
      var self = this;
      if (self._poll) return;
      self._poll = setInterval(function () {
        self.render();
      }, 5000);
    },

    _stopPoll: function () {
      if (this._poll) {
        clearInterval(this._poll);
        this._poll = 0;
      }
    },

    _schedule: function () {
      var self = this;
      if (self._raf) return;
      self._raf = requestAnimationFrame(function () {
        self._raf = 0;
        self.render();
      });
    },

    render: function () {
      if (!this._svg || !this._head || !this._host || !global.TeamModel) return;
      var host = this._host;
      var m = global.TeamModel.build(this._events, Date.now() / 1000);
      var agOf = function (k) {
        return m.agents.get ? m.agents.get(k) : m.agents[k];
      };
      var hasActivity =
        m.lanes.length > 1 || m.oneshots.length || m.skillBands.length || m.mainSpans.length;
      this._empty.hidden = !!hasActivity;
      this._head.style.display = hasActivity ? "block" : "none";
      this._svg.style.display = hasActivity ? "block" : "none";
      if (!hasActivity) {
        this._head.innerHTML = "";
        this._svg.innerHTML = "";
        return;
      }

      var lanes = m.lanes;
      var N = lanes.length;
      // ── horizontal: one column per agent ──
      var W = Math.max(GUT_L + N * COL_MIN + PAD_R, host.clientWidth - 4);
      var colW = (W - GUT_L - PAD_R) / N;
      var colX = function (i) {
        return GUT_L + i * colW + colW / 2;
      };
      var laneIndex = {};
      lanes.forEach(function (k, i) {
        laneIndex[k] = i;
      });

      // ── vertical: time flows down. Fit up to CAP seconds into the visible
      // height; beyond CAP freeze px/sec and let content grow → scroll (stick
      // to now at the bottom). Short runs zoom to fill the height. ──
      var T0 = m.t0,
        T1 = Math.max(m.t1, m.t0 + 1);
      var span = T1 - T0;
      var availH = Math.max(200, host.clientHeight - HEAD_H - 8);
      var pxPerSec = availH / Math.min(span, CAP);
      var Y = function (t) {
        return PAD_T + (t - T0) * pxPerSec;
      };
      var contentH = PAD_T + span * pxPerSec + PAD_B;
      var inDom = function (a, b) {
        return b >= T0 && a <= T1;
      };

      // ── sticky header: one column chip per lane ──
      var hd = this._head;
      hd.innerHTML = "";
      hd.setAttribute("viewBox", "0 0 " + W + " " + HEAD_H);
      hd.setAttribute("width", W);
      hd.setAttribute("height", HEAD_H);
      lanes.forEach(function (k, i) {
        var cx = colX(i);
        var ag = agOf(k);
        var profile = k === "main" ? "" : ag ? ag.profile : "";
        var label = k === "main" ? "main" : ag ? ag.label : k;
        var role = k === "main" ? "session" : ag ? ag.role || "" : "";
        var hv = k === "main" ? "var(--h-main)" : hueVar(profile);
        var cw = Math.min(colW - 10, 150);
        var x0 = cx - cw / 2;
        var g = svg("g", { class: "tv-chip" }, hd);
        svg("rect", { x: x0, y: 8, width: cw, height: HEAD_H - 16, rx: 7, class: "tv-chip-bg" }, g);
        var gb = svg("rect", { x: x0 + 6, y: 14, width: 18, height: 18, rx: 5 }, g);
        gb.style.fill = hv;
        gb.style.opacity = "0.18";
        var gl = svg("text", { x: x0 + 15, y: 27, "text-anchor": "middle", "font-size": "11" }, g);
        gl.textContent = k === "main" ? "◈" : glyphFor(profile);
        var nm = svg("text", { class: "tv-lane-nm", x: x0 + 30, y: 22 }, g);
        nm.textContent = label;
        var rl = svg("text", { class: "tv-lane-role", x: x0 + 30, y: 34 }, g);
        rl.textContent = role;
        var st = ag ? ag.state : "";
        var glyph = st === "busy" ? "⣾" : st === "waiting_ask" ? "?" : "·";
        var sx = svg("text", { class: "tv-state", x: x0 + cw - 10, y: 27 }, g);
        sx.textContent = glyph;
        sx.style.fill = st === "busy" ? "var(--cyan)" : "var(--muted)";
      });

      // ── plot ──
      var s = this._svg;
      s.innerHTML = "";
      s.setAttribute("viewBox", "0 0 " + W + " " + contentH);
      s.setAttribute("width", W);
      s.setAttribute("height", contentH);

      // column guide lines (vertical)
      lanes.forEach(function (k, i) {
        var cx = colX(i);
        svg("line", { class: "tv-base", x1: cx, y1: PAD_T, x2: cx, y2: contentH - PAD_B }, s);
      });

      // time axis: horizontal grid lines + left-gutter MM:SS labels.
      // adaptive step snapped to a "nice" value so labels stay legible whether
      // the run is 10s or hours (~1 tick per 64px).
      var targetTicks = Math.max(4, Math.round((contentH - PAD_T - PAD_B) / 64));
      var raw = span / targetTicks;
      var mags = [1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 900, 1800, 3600];
      var step = mags[mags.length - 1];
      for (var mi = 0; mi < mags.length; mi++) {
        if (mags[mi] >= raw) {
          step = mags[mi];
          break;
        }
      }
      for (var tk = Math.ceil(T0 / step) * step; tk <= T1 + 0.01; tk += step) {
        var y = Y(tk);
        svg("line", { class: "tv-axis", x1: GUT_L - 4, y1: y, x2: W - PAD_R, y2: y }, s);
        var tx = svg("text", { class: "tv-tick", x: 6, y: y + 3 }, s);
        tx.textContent = fmtClock(tk - m.t0);
      }

      // A skill or one-shot run BLOCKS its caller for its whole duration, so it
      // renders as a distinctly-colored span INSIDE the caller's column (not a
      // separate track). main's own turns get the neutral main hue.
      function scopeSpan(item, cls, prefix) {
        if (!inDom(item.t0, item.t1)) return;
        var ci = laneIndex[item.caller];
        if (ci == null) ci = 0;
        var cx = colX(ci);
        var y0 = Y(item.t0),
          h = Math.max(Y(item.t1) - y0, 3);
        var r = svg("rect", { class: cls, x: cx - BARW / 2, y: y0, width: BARW, height: h, rx: BARW / 2 }, s);
        // Label shows on HOVER (custom fast tooltip) — no always-drawn text.
        r.setAttribute("data-tip", prefix + " " + item.label);
      }
      m.mainSpans.forEach(function (sp) {
        if (!inDom(sp.t0, sp.t1)) return;
        var cx = colX(0);
        var y0 = Y(sp.t0),
          h = Math.max(Y(sp.t1) - y0, 3);
        var r = svg("rect", { class: "tv-span", x: cx - BARW / 2, y: y0, width: BARW, height: h, rx: BARW / 2 }, s);
        r.style.fill = "var(--h-main)";
        r.setAttribute("data-tip", "main: turn");
      });
      m.skillBands.forEach(function (b) {
        scopeSpan(b, "tv-scope-skill", "🪄");
      });
      m.oneshots.forEach(function (o) {
        scopeSpan(o, "tv-scope-run", "⟲");
      });

      // work spans in each agent column
      lanes.forEach(function (k) {
        var ag = agOf(k);
        if (!ag || !ag.spans) return;
        var cx = colX(laneIndex[k]);
        var hv = hueVar(ag.profile);
        ag.spans.forEach(function (sp) {
          if (!inDom(sp.t0, sp.t1)) return;
          var y0 = Y(sp.t0),
            h = Math.max(Y(sp.t1) - y0, 3);
          var r = svg("rect", { class: "tv-span", x: cx - BARW / 2, y: y0, width: BARW, height: h, rx: BARW / 2 }, s);
          r.style.fill = hv;
          r.setAttribute("data-tip", ag.label + ": " + (sp.title || "working"));
        });
      });

      // messages: a horizontal arrow between two columns at the message's y(time)
      // — request (main/peer → agent) and reply (agent → requester) both draw,
      // reading like a sequence diagram.
      m.messages.forEach(function (msg) {
        if (msg.t < T0 || msg.t > T1) return;
        var fi = laneIndex[msg.from],
          ti = laneIndex[msg.to];
        if (fi == null || ti == null) return;
        var xf = colX(fi),
          xt = colX(ti),
          y = Y(msg.t);
        var fromProfile = msg.from === "main" ? "" : (agOf(msg.from) || {}).profile;
        var hv = msg.from === "main" ? "var(--h-main)" : hueVar(fromProfile);
        var dir = xt > xf ? 1 : -1;
        var x0 = xf + dir * (BARW / 2 + 2),
          x1 = xt - dir * 7;
        var bow = Math.min(14, 6 + Math.abs(xt - xf) * 0.03);
        var d =
          "M " + x0 + " " + y +
          " C " + (x0 + dir * bow) + " " + (y - bow) + ", " +
          (x1 - dir * bow) + " " + (y - bow) + ", " + x1 + " " + y;
        // group the visible path + arrowhead + origin + wide hit so hovering
        // ANY of them highlights THIS message.
        var g = svg("g", { class: "tv-msg-g" }, s);
        var p = svg("path", { class: "tv-msg", d: d, fill: "none" }, g);
        p.style.stroke = hv;
        var ah = svg("path", { class: "tv-msg-ah", d: "M " + x1 + " " + (y - 3) + " L " + x1 + " " + (y + 3) + " L " + (x1 + dir * 6) + " " + y + " Z" }, g);
        ah.style.fill = hv;
        svg("circle", { cx: x0, cy: y, r: 1.8 }, g).style.fill = hv;
        var hit = svg("path", { class: "tv-msg-hit", d: d, fill: "none" }, g);
        hit.setAttribute("data-tip", msg.from + " → " + msg.to + " · " + msg.type + (msg.text ? ": " + msg.text : ""));
      });

      // Stick to "now" (bottom) unless the user scrolled up to read history.
      if (this._stick) host.scrollTop = host.scrollHeight;
    },
  };

  global.TeamView = TeamView;
})(typeof window !== "undefined" ? window : globalThis);
