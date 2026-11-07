// ── ⏰ 예약 (v10.12.0, docs/schedule/DESIGN.md §6.4) ────────────────────
// 이 세션의 예약 목록·추가·이력 서랍 + 놓친 예약 카드. 예약은 이 프로세스가
// 발화하므로 화면도 여기 하나다. 상태는 서버가 정본이고(GET /api/schedules),
// 변경은 SSE `schedules_changed` 로 온다 — 서랍이 닫혀 있어도 배지와 놓친
// 예약 카드는 갱신한다.

const $btn = document.getElementById("schedule-btn");
const $badge = document.getElementById("schedule-badge");
const $drawer = document.getElementById("schedule-drawer");
const $backdrop = document.getElementById("schedule-backdrop");
const $list = document.getElementById("sched-list");
const $log = document.getElementById("sched-log");
const $form = document.getElementById("sched-form");
const $cron = document.getElementById("sched-cron");
const $label = document.getElementById("sched-label");
const $prompt = document.getElementById("sched-prompt");
const $nick = document.getElementById("sched-nickname");
const $status = document.getElementById("sched-status");
const $missed = document.getElementById("sched-missed");
if ($btn && $drawer) {

  function el(tag, cls, text) {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined) n.textContent = text;
    return n;
  }
  function when(iso) {
    // "2026-10-05T09:00" → "10-05 09:00" (서버 로컬 시각 그대로)
    return iso ? iso.slice(5, 16).replace("T", " ") : "";
  }
  function name(s) {
    return s.label || s.human || s.cron;
  }

  async function call(method, path, body) {
    const r = await fetch(path, {
      method: method,
      headers: body ? { "Content-Type": "application/json" } : undefined,
      body: body ? JSON.stringify(body) : undefined,
    });
    if (!r.ok) {
      let detail = "";
      try { detail = (await r.json()).detail || ""; } catch (e) { /* 본문 없음 */ }
      throw new Error(detail || "HTTP " + r.status);
    }
    return r.json();
  }
  function act(method, path, body) {
    return call(method, path, body).then(render).catch(function (e) {
      $status.textContent = "✗ " + e.message;
    });
  }

  function button(text, cls, fn) {
    const b = el("button", cls, text);
    b.type = "button";
    b.addEventListener("click", fn);
    return b;
  }

  function renderItem(s) {
    const item = el("div", "sched-item" + (s.enabled ? "" : " off") + (s.missed_at ? " missed" : ""));
    item.dataset.id = s.id;
    const head = el("div", "sched-item-head");
    head.appendChild(el("span", "", s.source === "agent" ? "🤖" : "👤"));
    head.appendChild(el("span", "sched-item-name", name(s)));
    if (s.label && s.human) head.appendChild(el("span", "sched-item-when", s.human));
    head.appendChild(el("span", "sched-item-cron", s.cron));
    item.appendChild(head);
    item.appendChild(el("div", "sched-item-prompt", s.prompt));
    const meta = [];
    if (s.enabled && s.next_fire) meta.push("next " + when(s.next_fire));
    if (!s.enabled) meta.push("off");
    if (s.last_fired_at) meta.push("last run " + when(s.last_fired_at));
    meta.push("display name " + s.effective_nickname);
    item.appendChild(el("div", "sched-item-meta", meta.join(" · ")));
    if (s.missed_at) {
      item.appendChild(el("div", "sched-item-missed", "Missed — did not run at " + when(s.missed_at)));
    }
    const actions = el("div", "sched-item-actions");
    const base = "api/schedules/" + encodeURIComponent(s.id);
    actions.appendChild(button(s.enabled ? "Disable" : "Enable", "btn-ghost", function () {
      act("POST", base + "/toggle", { enabled: !s.enabled });
    }));
    actions.appendChild(button("Run now", "btn-ghost", function () {
      act("POST", base + "/run-now");
    }));
    if (s.missed_at) {
      actions.appendChild(button("Skip", "btn-ghost", function () {
        act("POST", base + "/dismiss");
      }));
    }
    actions.appendChild(button("Delete", "btn-danger", function () {
      act("DELETE", base);
    }));
    item.appendChild(actions);
    return item;
  }

  function renderMissed(rows) {
    $missed.textContent = "";
    const missed = rows.filter(function (s) { return s.missed_at; });
    $missed.hidden = missed.length === 0;
    missed.forEach(function (s) {
      const row = el("div", "sched-missed-row");
      row.dataset.id = s.id;
      row.appendChild(el("span", "sched-missed-text",
        "⏰ Missed schedule: " + name(s) + " (" + when(s.missed_at) + ")"));
      const base = "api/schedules/" + encodeURIComponent(s.id);
      row.appendChild(button("Run now", "btn-primary", function () {
        act("POST", base + "/run-now");
      }));
      row.appendChild(button("Skip", "btn-ghost", function () {
        act("POST", base + "/dismiss");
      }));
      $missed.appendChild(row);
    });
  }

  function render(view) {
    const rows = view.schedules || [];
    $list.textContent = "";
    if (!rows.length) {
      $list.appendChild(el("div", "sched-empty",
        "No schedules. Add one below, or just tell the agent something like \"write the weekly report every Monday at 9\"."));
    }
    rows.forEach(function (s) { $list.appendChild(renderItem(s)); });

    const on = rows.filter(function (s) { return s.enabled; }).length;
    const missed = rows.filter(function (s) { return s.missed_at; }).length;
    $badge.hidden = rows.length === 0;
    $badge.textContent = missed ? on + "!" : String(on);
    $badge.classList.toggle("missed", missed > 0);
    renderMissed(rows);

    $log.textContent = "";
    const EVENT = { fired: "fired", missed: "missed", skipped: "skipped", failed: "failed" };
    (view.log || []).slice().reverse().forEach(function (r) {
      $log.appendChild(el("div", "",
        when(r.ts) + "  " + (EVENT[r.event] || r.event) + "  " + (r.label || r.id) +
        (r.error ? "  — " + r.error : "")));
    });
    if (!$log.childNodes.length) $log.appendChild(el("div", "", "(no history)"));
    return view;
  }

  function load() {
    return call("GET", "api/schedules").then(render).catch(function () {
      /* 레지스트리 없음(headless) — 배지 없이 둔다 */
    });
  }

  $form.addEventListener("submit", function (e) {
    e.preventDefault();
    $status.textContent = "";
    call("POST", "api/schedules", {
      cron: $cron.value,
      prompt: $prompt.value,
      label: $label.value,
      nickname: $nick.value,
    }).then(function (view) {
      render(view);
      $cron.value = $label.value = $prompt.value = $nick.value = "";
      $status.textContent = "✓ Added";
    }).catch(function (err) {
      $status.textContent = "✗ " + err.message;
    });
  });
  document.getElementById("sched-presets").addEventListener("click", function (e) {
    const c = e.target && e.target.getAttribute("data-cron");
    if (c) $cron.value = c;
  });

  function open() {
    $backdrop.hidden = false;
    requestAnimationFrame(function () {
      $backdrop.classList.add("open");
      $drawer.classList.add("open");
    });
    $drawer.setAttribute("aria-hidden", "false");
    $status.textContent = "";
    load();
  }
  function close() {
    $backdrop.classList.remove("open");
    $drawer.classList.remove("open");
    $drawer.setAttribute("aria-hidden", "true");
    setTimeout(function () { $backdrop.hidden = true; }, 260);
  }
  $btn.addEventListener("click", function () {
    if ($drawer.classList.contains("open")) close();
    else open();
  });
  document.getElementById("sched-close").addEventListener("click", close);
  $backdrop.addEventListener("click", close);
  document.addEventListener("keydown", function (e) {
    if (e.key === "Escape" && $drawer.classList.contains("open")) close();
  });

  window.addEventListener("agentcli:schedules-changed", load);
  load();
}
