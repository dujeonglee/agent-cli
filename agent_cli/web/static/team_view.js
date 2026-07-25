/* team_view.js — the live "Team" swimlane surface. Renders TeamModel (from
 * team_model.js) as an SVG timeline: one lane per agent, work spans as bars,
 * peer messages as connectors, the enclosing skill as a top band, one-shot
 * runs as transient sub-tracks under main.
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
    BARH = 8,
    BANDY = 22;

  var TeamView = {
    _events: [],
    _host: null,
    _svg: null,
    _note: null,
    _active: false,
    _scale: "fit", // "fit" | "win"
    _win: 30, // window seconds
    _raf: 0,

    /** Feed one SSE event into the model buffer. */
    ingest: function (type, data) {
      var e = { type: type };
      for (var k in data) e[k] = data[k];
      if (e.ts == null) e.ts = Date.now() / 1000;
      this._events.push(e);
      if (this._active) this._schedule();
    },

    reset: function () {
      this._events = [];
      if (this._active) this._schedule();
    },

    /** Build the skeleton (controls + svg) inside host and take ownership. */
    mount: function (host) {
      this._host = host;
      host.innerHTML = "";
      var ctrl = elh("div", "tv-ctrl");
      var lbl = elh("span", "tv-cl", "Scale");
      var fit = elh("button", "tv-seg on", "Fit run");
      fit.dataset.scale = "fit";
      var win = elh("button", "tv-seg", "Live window");
      win.dataset.scale = "win";
      this._note = elh("span", "tv-note", "whole run scaled to width");
      var self = this;
      [fit, win].forEach(function (b) {
        b.addEventListener("click", function () {
          ctrl.querySelectorAll(".tv-seg").forEach(function (s) {
            s.classList.toggle("on", s === b);
          });
          self._scale = b.dataset.scale;
          self._note.textContent =
            self._scale === "win"
              ? "last " + self._win + "s — older scrolls off (follows the live edge)"
              : "whole run scaled to width";
          self.render();
        });
      });
      ctrl.appendChild(lbl);
      ctrl.appendChild(fit);
      ctrl.appendChild(win);
      ctrl.appendChild(this._note);
      host.appendChild(ctrl);
      this._svg = svg("svg", { class: "tv-svg", role: "img", "aria-label": "Agent team swimlane" });
      host.appendChild(this._svg);
      this._empty = elh("div", "tv-empty", "No team activity yet — spawn agents or run /orchestrate.");
      host.appendChild(this._empty);
      this._ro = new ResizeObserver(function () {
        if (self._active) self._schedule();
      });
      this._ro.observe(host);
    },

    setActive: function (on) {
      this._active = on;
      if (this._host) this._host.hidden = !on;
      if (on) this.render();
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
      var m = global.TeamModel.build(this._events);
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
      if (this._scale === "win" && T1 - T0 > this._win) T0 = T1 - this._win;
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
      var step = span <= 20 ? 5 : span <= 120 ? 15 : 60;
      for (var tk = Math.ceil(T0 / step) * step; tk <= T1 + 0.01; tk += step) {
        var x = X(tk);
        svg("line", { class: "tv-axis", x1: x, y1: PAD.t - 6, x2: x, y2: H - PAD.b + 3 }, s);
        var tx = svg("text", { class: "tv-tick", x: x + 3, y: PAD.t - 10 }, s);
        tx.textContent = fmtClock(tk - m.t0);
      }

      // skill band(s) at top
      m.skillBands.forEach(function (b) {
        if (!inDom(b.t0, b.t1)) return;
        var x0 = clampX(b.t0),
          x1 = clampX(b.t1);
        var g = svg("g", {}, s);
        var r = svg("rect", { class: "tv-band", x: x0, y: BANDY - 8, width: Math.max(x1 - x0, 4), height: 16, rx: 8 }, g);
        var t = svg("text", { class: "tv-band-lbl", x: x0 + 8, y: BANDY + 3 }, g);
        t.textContent = "🪄 " + b.label;
        var tt = svg("title", {}, r);
        tt.textContent = "skill: " + b.label;
      });
      var sl = svg("text", { class: "tv-tick", x: 14, y: BANDY + 3 }, s);
      sl.textContent = "skill";

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

      // one-shot runs — transient sub-track under caller (main) + return arrow
      m.oneshots.forEach(function (o) {
        if (!inDom(o.t0, o.t1)) return;
        var ci = laneIndex[o.caller];
        if (ci == null) ci = 0;
        var my = laneY(ci),
          sy = my + 17;
        var hv = "var(--h-reviewer)";
        var x0 = clampX(o.t0),
          x1 = clampX(o.t1);
        var base = svg("line", { x1: x0, y1: sy, x2: x1, y2: sy, "stroke-dasharray": "2 3" }, s);
        base.style.stroke = hv;
        base.style.opacity = "0.5";
        var bar = svg("rect", { x: x0, y: sy - 3, width: Math.max(x1 - x0, 3), height: 6, rx: 3 }, s);
        bar.style.fill = hv;
        var lg = svg("text", { class: "tv-tick", x: x1 + 5, y: sy + 3 }, s);
        lg.style.fill = hv;
        lg.textContent = "⟲ " + o.label + " ↩";
        var arr = svg("path", { d: "M " + x1 + " " + (sy - 3) + " L " + x1 + " " + (my + 4), fill: "none" }, s);
        arr.style.stroke = hv;
        arr.style.opacity = "0.7";
        var tt = svg("title", {}, bar);
        tt.textContent = "one-shot run: " + o.label;
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
          var tt = svg("title", {}, r);
          tt.textContent = ag.label + ": " + (sp.title || "working");
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
        var p = svg("path", { class: "tv-msg", d: d, fill: "none" }, s);
        p.style.stroke = hv;
        var ah = svg("path", { d: "M " + (x - 3) + " " + (yt - dir * 6) + " L " + (x + 3) + " " + (yt - dir * 6) + " L " + x + " " + (yt - dir * 1) + " Z" }, s);
        ah.style.fill = hv;
        svg("circle", { cx: x, cy: yf + dir * (BARH / 2 + 2), r: 1.8 }, s).style.fill = hv;
        var hit = svg("path", { class: "tv-msg-hit", d: d, fill: "none" }, s);
        var tt = svg("title", {}, hit);
        tt.textContent = msg.from + " → " + msg.to + " · " + msg.type + (msg.text ? ": " + msg.text : "");
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
