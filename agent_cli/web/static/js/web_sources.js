// ── 🌐 정보원 서랍 (v10.57.0, agent_cli/web_sources.py) ─────────────
// 꼬리 ## Web Sources 의 체크박스 목록. 체크·추가·삭제마다 바로 저장하고, 서버는
// 바꾼 것만 .agent-cli/web-sources.json 에 남긴다. 꼬리는 턴마다 파일을 읽는다.

const $btn = document.getElementById("websrc-btn");
const $drawer = document.getElementById("websrc-drawer");
const $backdrop = document.getElementById("websrc-backdrop");
const $list = document.getElementById("websrc-list");
const $total = document.getElementById("websrc-total");
const $path = document.getElementById("websrc-path");
const $form = document.getElementById("websrc-form");
const $group = document.getElementById("websrc-group");
const $groups = document.getElementById("websrc-groups");
const $text = document.getElementById("websrc-text");
const $reset = document.getElementById("websrc-reset");
const $status = document.getElementById("websrc-status");
if ($btn && $drawer) {

  let rows = [];

  function el(tag, cls, text) {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined) n.textContent = text;
    return n;
  }
  function state() {
    return {
      disabled: rows.filter(function (r) { return !r.enabled; }).map(function (r) { return r.id; }),
      added: rows.filter(function (r) { return !r.builtin; }).map(function (r) {
        return { id: r.id, group: r.group, text: r.text };
      }),
    };
  }
  function render() {
    $list.textContent = "";
    const on = rows.filter(function (r) { return r.enabled; });
    const tok = on.reduce(function (a, r) { return a + (r.tokens || 0); }, 0);
    $total.textContent = on.length + "/" + rows.length + " enabled · ~" + tok + " tokens/turn";
    const order = [];
    rows.forEach(function (r) { if (order.indexOf(r.group) < 0) order.push(r.group); });
    $groups.textContent = "";
    order.forEach(function (g) {
      const o = document.createElement("option");
      o.value = g;
      $groups.appendChild(o);
      const box = el("div", "websrc-group");
      box.setAttribute("data-group", g);
      box.appendChild(el("div", "websrc-group-name", g));
      rows.filter(function (r) { return r.group === g; }).forEach(function (r) {
        const item = el("label", "websrc-item" + (r.enabled ? "" : " off"));
        item.setAttribute("data-id", r.id);
        const cb = document.createElement("input");
        cb.type = "checkbox";
        cb.checked = r.enabled;
        cb.addEventListener("change", function () {
          r.enabled = cb.checked;
          save();
        });
        item.appendChild(cb);
        const t = el("span", "websrc-text", r.text);
        if (!r.builtin) t.appendChild(el("span", "websrc-user", "added"));
        item.appendChild(t);
        item.appendChild(el("span", "websrc-tok", "~" + r.tokens));
        if (!r.builtin) {
          const del = el("button", "btn-ghost websrc-del", "✕");
          del.type = "button";
          del.title = "Delete this entry";
          del.addEventListener("click", function (e) {
            e.preventDefault();
            rows = rows.filter(function (x) { return x !== r; });
            save();
          });
          item.appendChild(del);
        }
        box.appendChild(item);
      });
      $list.appendChild(box);
    });
  }
  async function call(method, body) {
    const r = await fetch("api/web-sources", {
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
  function load() {
    return call("GET").then(function (d) {
      rows = d.entries || [];
      $path.textContent = d.path || "";
      render();
    }).catch(function (e) { $status.textContent = "✗ " + e.message; });
  }
  function save(body) {
    return call("POST", body || state()).then(function (d) {
      rows = d.entries || [];
      render();
      $status.textContent = "Saved — from the next turn";
    }).catch(function (e) {
      $status.textContent = "✗ " + e.message;
      load();
    });
  }
  $form.addEventListener("submit", function (e) {
    e.preventDefault();
    const g = $group.value.trim();
    const t = $text.value.trim();
    if (!g || !t) { $status.textContent = "Enter both a group and a URL"; return; }
    let n = 1;
    const ids = rows.map(function (r) { return r.id; });
    while (ids.indexOf("user-" + n) >= 0) n += 1;
    rows.push({ id: "user-" + n, group: g, text: t, enabled: true, builtin: false });
    save().then(function () { $text.value = ""; });
  });
  $reset.addEventListener("click", function () {
    save({ disabled: [], added: [] });
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
  document.getElementById("websrc-close").addEventListener("click", close);
  $backdrop.addEventListener("click", close);
  document.addEventListener("keydown", function (e) {
    if (e.key === "Escape" && $drawer.classList.contains("open")) close();
  });
}
