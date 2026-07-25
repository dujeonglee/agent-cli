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

  var PAD = { l: 132, r: 22, t: 46, b: 22 },
    LH = 46,
    BARH = 8;

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

    /** Build the swimlane SVG inside host and take ownership. */
    mount: function (host) {
      this._host = host;
      host.innerHTML = "";
      var self = this;
      this._svg = svg("svg", { class: "tv-svg", role: "img", "aria-label": "Agent team swimlane" });
      host.appendChild(this._svg);
      this._empty = elh("div", "tv-empty", "No team activity yet — spawn agents or run /orchestrate.");
      host.appendChild(this._empty);
      // Custom fast tooltip (native SVG <title> has a ~0.5s+ delay that can't
      // be tuned). Delegated on the SVG once — survives re-renders. Any element
      // with a data-tip attribute shows it ~100ms after hover, near the cursor.
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
      this._svg.addEventListener("mousemove", function (e) {
        mx = e.clientX;
        my = e.clientY;
        if (!self._tip.hidden) place();
      });
      this._svg.addEventListener("mouseover", function (e) {
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
      this._svg.addEventListener("mouseout", function (e) {
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
      if (!this._svg || !this._host || !global.TeamModel) return;
      var m = global.TeamModel.build(this._events, Date.now() / 1000);
      var agOf = function (k) {
        return m.agents.get ? m.agents.get(k) : m.agents[k];
      };
      var hasActivity =
        m.lanes.length > 1 || m.oneshots.length || m.skillBands.length || m.mainSpans.length;
      this._empty.hidden = !!hasActivity;
      this._svg.style.display = hasActivity ? "block" : "none";
      if (!hasActivity) {
        this._svg.innerHTML = "";
        return;
      }

      var W = Math.max(560, this._host.clientWidth - 4);
      var plotW = W - PAD.l - PAD.r;
      var T0 = m.t0,
        T1 = Math.max(m.t1, m.t0 + 1);
      var span = T1 - T0;
      var lanes = m.lanes;
      var H = PAD.t + lanes.length * LH + PAD.b;
      var s = this._svg;
      s.innerHTML = "";
      s.setAttribute("viewBox", "0 0 " + W + " " + H);
      s.setAttribute("width", W);
      s.setAttribute("height", H);
      var X = function (t) {
        return PAD.l + ((t - T0) / span) * plotW;
      };
      var clampX = function (t) {
        return Math.max(PAD.l, Math.min(PAD.l + plotW, X(t)));
      };
      var inDom = function (a, b) {
        return b >= T0 && a <= T1;
      };
      var xEnd = PAD.l + plotW;
      var laneY = function (i) {
        return PAD.t + i * LH + LH / 2;
      };
      var laneIndex = {};
      lanes.forEach(function (k, i) {
        laneIndex[k] = i;
      });

      // axis ticks (relative seconds from run start)
      // adaptive step: ~7 ticks, snapped to a "nice" value so the axis stays
      // legible whether the run is 10s or 10min (the base unit grows with span).
      var raw = span / 7;
      var mags = [1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 900, 1800, 3600];
      var step = mags[mags.length - 1];
      for (var mi = 0; mi < mags.length; mi++) {
        if (mags[mi] >= raw) {
          step = mags[mi];
          break;
        }
      }
      for (var tk = Math.ceil(T0 / step) * step; tk <= T1 + 0.01; tk += step) {
        var x = X(tk);
        svg("line", { class: "tv-axis", x1: x, y1: PAD.t - 6, x2: x, y2: H - PAD.b + 3 }, s);
        var tx = svg("text", { class: "tv-tick", x: x + 3, y: PAD.t - 10 }, s);
        tx.textContent = fmtClock(tk - m.t0);
      }

      // lanes: label chip + baseline + state glyph
      lanes.forEach(function (k, i) {
        var y = laneY(i);
        var ag = agOf(k);
        var profile = k === "main" ? "" : ag ? ag.profile : "";
        var label = k === "main" ? "main" : ag ? ag.label : k;
        var role = k === "main" ? "session" : ag ? ag.role || "" : "";
        var hv = k === "main" ? "var(--h-main)" : hueVar(profile);
        svg("line", { class: "tv-base", x1: PAD.l, y1: y, x2: xEnd, y2: y }, s);
        var g = svg("g", { class: "tv-chip" }, s);
        var chip = svg("rect", { x: 12, y: y - 13, width: PAD.l - 24, height: 26, rx: 6, class: "tv-chip-bg" }, g);
        var gb = svg("rect", { x: 18, y: y - 9, width: 18, height: 18, rx: 5 }, g);
        gb.style.fill = hv;
        gb.style.opacity = "0.18";
        var gl = svg("text", { x: 27, y: y + 4, "text-anchor": "middle", "font-size": "11" }, g);
        gl.textContent = k === "main" ? "◈" : glyphFor(profile);
        var nm = svg("text", { class: "tv-lane-nm", x: 42, y: y - 1 }, g);
        nm.textContent = label;
        var rl = svg("text", { class: "tv-lane-role", x: 42, y: y + 10 }, g);
        rl.textContent = role;
        // state glyph on the right edge
        var st = ag ? ag.state : "";
        var glyph = st === "busy" ? "⣾" : st === "waiting_ask" ? "?" : "·";
        var sx = svg("text", { class: "tv-state", x: xEnd + 4, y: y + 4 }, s);
        sx.textContent = glyph;
        sx.style.fill = st === "busy" ? "var(--cyan)" : "var(--muted)";
      });

      // A skill or one-shot run BLOCKS its caller for its whole duration, so it
      // renders as a distinctly-colored span INSIDE the caller's lane (not a
      // separate band/track). main's own turns get the neutral main hue.
      function scopeSpan(item, cls, prefix) {
        if (!inDom(item.t0, item.t1)) return;
        var ci = laneIndex[item.caller];
        if (ci == null) ci = 0;
        var y = laneY(ci);
        var x0 = clampX(item.t0),
          x1 = clampX(item.t1),
          w = Math.max(x1 - x0, 3);
        var r = svg("rect", { class: cls, x: x0, y: y - BARH / 2, width: w, height: BARH, rx: BARH / 2 }, s);
        // Label shows on HOVER (custom fast tooltip) — no always-drawn text,
        // which overlapped badly when many scopes stacked on one lane.
        r.setAttribute("data-tip", prefix + " " + item.label);
      }
      m.mainSpans.forEach(function (sp) {
        if (!inDom(sp.t0, sp.t1)) return;
        var y = laneY(0);
        var x0 = clampX(sp.t0),
          x1 = clampX(sp.t1);
        var r = svg("rect", { class: "tv-span", x: x0, y: y - BARH / 2, width: Math.max(x1 - x0, 3), height: BARH, rx: BARH / 2 }, s);
        r.style.fill = "var(--h-main)";
        r.setAttribute("data-tip", "main: turn");
      });
      m.skillBands.forEach(function (b) {
        scopeSpan(b, "tv-scope-skill", "🪄");
      });
      m.oneshots.forEach(function (o) {
        scopeSpan(o, "tv-scope-run", "⟲");
      });

      // work spans (persistent lanes)
      lanes.forEach(function (k) {
        var ag = agOf(k);
        if (!ag || !ag.spans) return;
        var y = laneY(laneIndex[k]);
        var hv = hueVar(ag.profile);
        ag.spans.forEach(function (sp) {
          if (!inDom(sp.t0, sp.t1)) return;
          var x0 = clampX(sp.t0),
            x1 = clampX(sp.t1),
            w = Math.max(x1 - x0, 3);
          var r = svg("rect", { class: "tv-span", x: x0, y: y - BARH / 2, width: w, height: BARH, rx: BARH / 2 }, s);
          r.style.fill = hv;
          r.setAttribute("data-tip", ag.label + ": " + (sp.title || "working"));
        });
      });

      // messages between lanes
      m.messages.forEach(function (msg) {
        if (msg.t < T0 || msg.t > T1) return;
        var fi = laneIndex[msg.from],
          ti = laneIndex[msg.to];
        if (fi == null || ti == null) return;
        var yf = laneY(fi),
          yt = laneY(ti),
          x = X(msg.t);
        var fromProfile = msg.from === "main" ? "" : (agOf(msg.from) || {}).profile;
        var hv = msg.from === "main" ? "var(--h-main)" : hueVar(fromProfile);
        var dir = yt > yf ? 1 : -1;
        var bow = Math.min(22, 8 + Math.abs(yt - yf) * 0.14);
        var d =
          "M " + x + " " + (yf + dir * (BARH / 2 + 2)) +
          " C " + (x + bow) + " " + (yf + yt) / 2 + ", " + (x + bow) + " " + (yf + yt) / 2 + ", " +
          x + " " + (yt - dir * 6);
        // group the visible path + arrowhead + origin + wide hit so hovering
        // ANY of them highlights THIS message (an adjacent-sibling selector was
        // highlighting the NEXT arrow instead).
        var g = svg("g", { class: "tv-msg-g" }, s);
        var p = svg("path", { class: "tv-msg", d: d, fill: "none" }, g);
        p.style.stroke = hv;
        var ah = svg("path", { class: "tv-msg-ah", d: "M " + (x - 3) + " " + (yt - dir * 6) + " L " + (x + 3) + " " + (yt - dir * 6) + " L " + x + " " + (yt - dir * 1) + " Z" }, g);
        ah.style.fill = hv;
        svg("circle", { cx: x, cy: yf + dir * (BARH / 2 + 2), r: 1.8 }, g).style.fill = hv;
        var hit = svg("path", { class: "tv-msg-hit", d: d, fill: "none" }, g);
        hit.setAttribute("data-tip", msg.from + " → " + msg.to + " · " + msg.type + (msg.text ? ": " + msg.text : ""));
      });

      // "earlier" marker when a window hides the run's head
      if (T0 > m.t0) {
        var em = svg("text", { class: "tv-tick", x: PAD.l + 4, y: H - PAD.b + 14 }, s);
        em.style.fill = "var(--muted)";
        em.textContent = "◄ 00:00–" + fmtClock(T0 - m.t0) + " earlier";
      }
    },
  };

  global.TeamView = TeamView;
})(typeof window !== "undefined" ? window : globalThis);
