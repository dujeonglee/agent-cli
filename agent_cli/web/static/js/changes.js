// changes.js — 상단 Δ 칩과 Changes 패널: 기준점 이후 디스크에서 바뀐 파일 목록.
// 순환 밖 잎이다 — dom·sse·markdown 만 import 한다.
// (v10.137.0, docs/snapshot/DESIGN.md §7 · docs/web-modules/DESIGN.md §8)
//
// 목록은 "스냅샷 저장소의 기준점 커밋 ↔ 지금 디스크" 다(api/changes). 누가 바꿨든 —
// 모델·상주 에이전트·사람의 IDE — 디스크가 바뀌면 오른다. 그래서 칩은 "모델이 바꾼
// 것" 이라고 말하지 않는다. Clear 는 지금 디스크를 새 기준점으로 삼을 뿐 파일을 건드리지
// 않고, 몇 초간 Undo 로 이전 기준점을 되살린다(한 단계).
//
// 다시 세는 때: 페이지 로드(스냅샷 재생 끝), 패널을 열 때, ↻, 런 끝. 런 끝은 기존 SSE 로
// 안다 — main 런은 `worker_state` 가 바쁨 → 한가로 바뀔 때(main 런은 `scope_end` 를 내지
// 않는다), 에이전트·스킬·상주 런은 `scope_end`. 도구 호출마다 세지 않는다. 재생이
// `scope_end` 를 몰아 보내도 한 번으로 합친다(RECOUNT_MS).

import { el, elHtml } from "./dom.js";
import { es } from "./sse.js";
import { colorizeDiffBody, escapeHtml } from "./markdown.js";

const $chip = document.getElementById("changes-chip");
const $drawer = document.getElementById("changes-drawer");
const $backdrop = document.getElementById("changes-backdrop");
const $since = document.getElementById("changes-since");
const $list = document.getElementById("changes-list");
const $diff = document.getElementById("changes-diff");
const $toast = document.getElementById("changes-toast");
const $clear = document.getElementById("changes-clear");
// 런 끝 신호를 한 번으로 합치는 간격 — 재생이 끝난 스코프를 몰아 보낸다
const RECOUNT_MS = 400;
// Clear 뒤 Undo 토스트가 서 있는 시간
const UNDO_MS = 6000;

if ($chip && $drawer) {
  let data = null; // 마지막 api/changes 응답
  let selected = null; // 고른 파일 경로
  let recountTimer = null;
  let toastTimer = null;
  let wasBusy = false;

  function hhmm(epoch) {
    const d = new Date(epoch * 1000);
    const p = function (n) { return String(n).padStart(2, "0"); };
    const t = p(d.getHours()) + ":" + p(d.getMinutes());
    if (d.toDateString() === new Date().toDateString()) return t;
    return d.getFullYear() + "-" + p(d.getMonth() + 1) + "-" + p(d.getDate()) + " " + t;
  }
  function sinceText(b) {
    if (b.kind === "session") return "since session start · " + hhmm(b.when);
    if (b.kind === "cleared") return "since " + hhmm(b.when) + " · cleared";
    return "since " + hhmm(b.when);
  }

  function renderChip() {
    const on = !!(data && data.enabled);
    $chip.hidden = !on;
    if (!on) return;
    const n = data.files.length;
    $chip.textContent = n ? "Δ " + n + (n === 1 ? " file" : " files") : "Δ 0";
    $chip.classList.toggle("chg-zero", !n);
    $chip.title = "Files changed on disk " + sinceText(data.baseline) + " — open the list";
  }

  function countsHtml(f) {
    if (f.added == null && f.deleted == null) return '<span class="chg-bin">binary</span>';
    let s = "";
    if (f.added) s += '<span class="chg-add">+' + f.added + "</span>";
    if (f.deleted) s += (s ? " " : "") + '<span class="chg-del">−' + f.deleted + "</span>";
    return s;
  }

  function renderPanel() {
    $list.textContent = "";
    const on = !!(data && data.enabled);
    $clear.disabled = !on;
    $drawer.querySelector("#changes-refresh").disabled = !on;
    if (!on) {
      $since.textContent = "";
      $list.appendChild(el("div", ["chg-empty"], "Snapshots are off in this room"));
      showDiff(null);
      return;
    }
    $since.textContent = sinceText(data.baseline);
    if (!data.files.length) {
      const box = el("div", ["chg-empty"]);
      box.appendChild(el("b", [], "No changes " + sinceText(data.baseline).replace(/ · cleared$/, "")));
      if (data.baseline.kind === "cleared") {
        box.appendChild(el("span", [], "Files on disk were not touched — only this list was reset."));
      }
      $list.appendChild(box);
      selected = null;
      showDiff(null);
      return;
    }
    if (selected && !data.files.some(function (f) { return f.path === selected; })) selected = null;
    data.files.forEach(function (f) {
      const row = el("button", ["chg-row"]);
      row.type = "button";
      row.dataset.path = f.path;
      if (f.path === selected) row.classList.add("sel");
      row.appendChild(el("span", ["chg-st", "chg-st-" + f.status], f.status));
      const p = el("span", ["chg-path"], f.path);
      p.title = f.path;
      row.appendChild(p);
      row.appendChild(elHtml("span", ["chg-n"], countsHtml(f)));
      row.addEventListener("click", function () { pick(f.path); });
      $list.appendChild(row);
    });
    if (selected) loadDiff(selected);
    else showDiff(null);
  }

  function showDiff(text) {
    $diff.hidden = text === null;
    if (text === null) { $diff.textContent = ""; return; }
    $diff.innerHTML = text ? colorizeDiffBody(escapeHtml(text)) : "No difference on disk now.";
  }
  function pick(path) {
    selected = path;
    $list.querySelectorAll(".chg-row").forEach(function (r) {
      r.classList.toggle("sel", r.dataset.path === path);
    });
    loadDiff(path);
  }
  function loadDiff(path) {
    fetch("api/changes/diff?path=" + encodeURIComponent(path))
      .then(function (r) { return r.json().then(function (d) { return { ok: r.ok, d: d }; }); })
      .then(function (x) {
        if (selected !== path) return; // 그 사이 다른 파일을 골랐다
        showDiff(x.ok ? x.d.diff : "✗ " + (x.d.detail || "diff failed"));
      })
      .catch(function () { if (selected === path) showDiff("✗ diff failed"); });
  }

  function apply(d) {
    data = d;
    renderChip();
    if ($drawer.classList.contains("open")) renderPanel();
  }
  function recount() {
    return fetch("api/changes")
      .then(function (r) { return r.json(); })
      .then(apply)
      .catch(function () { /* 다음 신호에 다시 센다 */ });
  }
  function recountSoon() {
    clearTimeout(recountTimer);
    recountTimer = setTimeout(recount, RECOUNT_MS);
  }

  function toast(n) {
    clearTimeout(toastTimer);
    $toast.textContent = "";
    $toast.appendChild(el("span", [], "List cleared (" + n + (n === 1 ? " file" : " files") + ")."));
    const undo = el("button", ["btn-ghost", "chg-undo"], "Undo");
    undo.type = "button";
    undo.addEventListener("click", function () {
      $toast.hidden = true;
      fetch("api/changes/undo", { method: "POST" })
        .then(function (r) { return r.json(); })
        .then(apply)
        .catch(recount);
    });
    $toast.appendChild(undo);
    $toast.hidden = false;
    toastTimer = setTimeout(function () { $toast.hidden = true; }, UNDO_MS);
  }
  $clear.addEventListener("click", function () {
    const n = data && data.files ? data.files.length : 0;
    $clear.disabled = true;
    fetch("api/changes/clear", { method: "POST" })
      .then(function (r) { if (!r.ok) throw new Error(); return r.json(); })
      .then(function (d) { selected = null; apply(d); toast(n); })
      .catch(recount)
      .then(function () { $clear.disabled = !(data && data.enabled); });
  });
  document.getElementById("changes-refresh").addEventListener("click", recount);

  function open() {
    $backdrop.hidden = false;
    requestAnimationFrame(function () {
      $backdrop.classList.add("open");
      $drawer.classList.add("open");
    });
    $drawer.setAttribute("aria-hidden", "false");
    $chip.setAttribute("aria-expanded", "true");
    renderPanel();
    recount();
  }
  function close() {
    $backdrop.classList.remove("open");
    $drawer.classList.remove("open");
    $drawer.setAttribute("aria-hidden", "true");
    $chip.setAttribute("aria-expanded", "false");
    setTimeout(function () { $backdrop.hidden = true; }, 260);
  }
  $chip.addEventListener("click", function () {
    if ($drawer.classList.contains("open")) close();
    else open();
  });
  document.getElementById("changes-close").addEventListener("click", close);
  $backdrop.addEventListener("click", close);
  document.addEventListener("keydown", function (e) {
    if (e.key === "Escape" && $drawer.classList.contains("open")) close();
  });

  // 런 끝 — 새 이벤트 없이 기존 신호로
  es.addEventListener("snapshot_end", recountSoon);
  es.addEventListener("scope_end", recountSoon);
  es.addEventListener("worker_state", function (e) {
    const busy = !!JSON.parse(e.data).busy;
    if (wasBusy && !busy) recountSoon();
    wasBusy = busy;
  });
  recount();
}
