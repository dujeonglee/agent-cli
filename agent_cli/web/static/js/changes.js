// changes.js — Changes 패널: 기준점 이후 디스크에서 바뀐 파일 목록과 그 입구 둘.
// 순환 밖 잎이다 — dom·sse·markdown 만 import 한다.
// (v10.137.0, docs/snapshot/DESIGN.md §7 · docs/web-modules/DESIGN.md §8)
//
// 입구 셋(v10.137.1·v10.144.2): ① 📁(`#files-btn`) 안의 숫자 배지 — 배지를 누르면 이 패널, 아이콘을
// 누르면 workspace.js 의 Workspace files 드로어 그대로(배지 클릭은 버블링을 막는다).
// ② main 채널의 가장 최근 `complete` 카드 안의 한 줄 `Δ N files changed · View changes`.
// 둘 다 다른 모듈의 화면이라 import 없이 **DOM 으로만** 붙는다(선택자). 줄은 하나뿐이다 —
// 누적 목록이라 옛 카드의 숫자는 의미가 없어, 새 카드가 생기면 그리로 옮긴다. 카드가
// 셈보다 늦게 그려질 수 있어(재생·라이브 둘 다) `#messages` 의 직계 자식 추가만 보는
// MutationObserver 하나가 새 main 최종답 카드를 보면 다시 붙인다. 줄은 화면 전용이다 —
// 대화 복사는 카드의 `_md`(원문)에서 만들어 이 줄이 들어가지 않는다.
// ③ 📁 Workspace files 서랍 머리 아래 늘 서 있는 한 줄(`#changes-entry`, v10.144.2): 바뀐
// 파일 N(강조, `View changes ›`)·0(흐림, `Open ›` — 빈 패널)·추적 꺼짐(점선, 누를 수 없음 —
// 서버가 준 까닭 `reason`, 원문 `detail` 은 title 에만). 배지·런 끝 줄과 달리 0 에도 선다.
//
// 목록은 "스냅샷 저장소의 기준점 커밋 ↔ 지금 디스크" 다(api/changes). 누가 바꿨든 —
// 모델·상주 에이전트·사람의 IDE — 디스크가 바뀌면 오른다. 그래서 입구는 "모델이 바꾼
// 것" 이라고 말하지 않는다. Clear 는 지금 디스크를 새 기준점으로 삼을 뿐 파일을 건드리지
// 않고, 몇 초간 Undo 로 이전 기준점을 되살린다(한 단계).
//
// 패널의 행은 접고 펴진다(v10.138.0): 고른 행을 다시 누르면 diff 가 닫히고 선택이 풀린다.
// 행 앞 ▸(닫힘)/▾(열림). 따로 닫기 버튼은 두지 않는다 — 패널의 ✕ 와 헷갈린다.
//
// 📁 Workspace files 트리의 변경 표시(v10.138.0): 같은 목록으로 트리 행을 꾸민다 — 파일은
// 이름 오른쪽에 상태 글자(M·A, 패널과 같은 색), 폴더(루트 포함)는 안 어디든 바뀐 경로가
// 있으면 점 ●. 삭제된 파일은 트리에 없어 상위 폴더 점으로만 드러난다. 트리는
// workspace.js 의 화면이다 — 그쪽은 행(`.dl-row`)에 `data-rel`·`data-type` 만 달고, 여기는
// import 없이 그 속성으로 찾는다. 폴더를 펼칠 때 늦게 그려지는 행은 `#dl-tree` 의
// MutationObserver 하나가 새 행만 꾸미고, 목록이 바뀌면(갱신·Clear·Undo) 트리 전체를 지우고
// 다시 꾸민다. 표시는 목록에서만 나오므로 Clear 로 0 이 되면 배지·런 끝 줄과 함께 사라진다.
// 파일 글자를 누르면 그 파일 diff 를 연 채로 패널이 열린다 — Workspace 드로어는 그대로 둔다
// (패널이 그 위를 덮고, 패널을 닫으면 보던 트리로 돌아온다). 폴더 점은 패널만 연다.
// v10.138.2: 🔗 행(`data-type="link"` — 밖을 가리키거나 깨진 링크)도 파일처럼 글자 — 목록의
// 변경은 링크 자체(대상 문자열)이고 diff 도 그것이다. 목록의 경로가 트리에서 폴더 행이면
// 하위 git 저장소(gitlink — 커밋이 바뀌면 그 폴더 한 줄로 오른다)거나 안을 가리키는 폴더
// 링크라 그 행에도 점 ●, 누르면 그 경로 diff.
//
// 하위 git 저장소(v10.139.0, docs/snapshot/DESIGN.md §8): 서버가 그 저장소의 git 으로 센
// 항목은 `repo`(저장소 경로)를 싣고 경로는 워크스페이스 기준이라(`git-proj/src/a.py`) 행·diff·
// 트리 표시는 그대로다. 보이는 하위 저장소 그룹이 하나라도 있으면 목록을 그룹으로 나눈다 —
// `Workspace` 와 `P/`(머리 태그: HEAD 이동·기준 다시 잡음·새 저장소·지워짐). 없으면 지금처럼
// 한 목록이다. 요약 줄(`summary` — 지워진 저장소 `D P/`·기준점 뒤에 생긴 저장소 `A P/`)은
// 줄 수 없이 서고 diff 자리에 서버의 요약 문장이 온다. 기준을 잃은 저장소의 안내 줄
// (`notices`)은 목록 맨 위에 ⚠ 문장 ✕ — ✕ 는 서버 기록에서 닫는다(재접속에도 닫힌 채).
// 서버가 셀 때마다 정하는 안내(v10.139.1 — 커밋 없는 저장소·스냅샷 실패)는 `dismissable`
// false 라 ✕ 가 없고, 스냅샷 실패(`cause` `snapshot_failing`)는 색만 빨갛다.
// 📁 트리의 하위 저장소 폴더 행에는 작은 `git` 배지를 단다(`data-rel` 로, 표시와 같은 길).
//
// 다시 세는 때: 페이지 로드(스냅샷 재생 끝), 패널을 열 때, ↻, 런 끝. 런 끝은 기존 SSE 로
// 안다 — main 런은 `worker_state` 가 바쁨 → 한가로 바뀔 때(main 런은 `scope_end` 를 내지
// 않는다), 에이전트·스킬·상주 런은 `scope_end`. 도구 호출마다 세지 않는다. 재생이
// `scope_end` 를 몰아 보내도 한 번으로 합친다(RECOUNT_MS).

import { $messages, el, elHtml } from "./dom.js";
import { es } from "./sse.js";
import { colorizeDiffBody, escapeHtml } from "./markdown.js";

const $badge = document.getElementById("changes-badge");
const $drawer = document.getElementById("changes-drawer");
const $backdrop = document.getElementById("changes-backdrop");
const $since = document.getElementById("changes-since");
const $list = document.getElementById("changes-list");
const $diff = document.getElementById("changes-diff");
const $toast = document.getElementById("changes-toast");
const $clear = document.getElementById("changes-clear");
const $entry = document.getElementById("changes-entry"); // 📁 서랍 머리 아래 한 줄
const $tree = document.getElementById("dl-tree"); // workspace.js 의 트리 — 행을 꾸미기만 한다
// 런 끝 신호를 한 번으로 합치는 간격 — 재생이 끝난 스코프를 몰아 보낸다
const RECOUNT_MS = 400;
// Clear 뒤 Undo 토스트가 서 있는 시간
const UNDO_MS = 6000;

if ($badge && $drawer) {
  let data = null; // 마지막 api/changes 응답
  let selected = null; // 고른 파일 경로
  let recountTimer = null;
  let toastTimer = null;
  let wasBusy = false;
  let byPath = new Map(); // 경로 → 목록 항목 (트리 표시용)
  let dirty = new Set(); // 안에 바뀐 경로가 있는 폴더 rel ("" = 루트)
  // 런 끝 줄 — 하나를 만들어 두고 마지막 main 최종답 카드로 옮긴다
  const $line = el("button", ["final-changes"]);
  $line.type = "button";

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

  function count() {
    return data && data.enabled ? data.files.length : 0;
  }
  function tip(n) {
    return n + (n === 1 ? " file" : " files") + " changed on disk " +
      sinceText(data.baseline) + " — open Changes";
  }
  function renderBadge() {
    const n = count();
    $badge.hidden = !n;
    $badge.textContent = n ? String(n) : "";
    $badge.title = n ? tip(n) : "";
  }
  // main 채널의 가장 최근 최종답 카드(cards.js 가 그린 것, 루트 직계 — 에이전트 채널의
  // 런 블록이나 서브에이전트 묶음 안의 것은 아니다)
  function lastMainFinal() {
    const fins = $messages.querySelectorAll(':scope > .card-assistant[data-ch="main"] > .final');
    return fins.length ? fins[fins.length - 1].parentNode : null;
  }
  function placeLine() {
    const n = count();
    const card = n ? lastMainFinal() : null;
    if (!card) { $line.remove(); return; }
    $line.textContent = "Δ " + n + (n === 1 ? " file" : " files") + " changed · View changes";
    $line.title = tip(n);
    // `.final` 다음, 답한 요청 줄이 있으면 그 뒤
    const anchor = card.querySelector(":scope > .final-answers") || card.querySelector(":scope > .final");
    if ($line.parentNode !== card || $line.previousSibling !== anchor) {
      card.insertBefore($line, anchor.nextSibling);
    }
  }
  // 📁 서랍의 한 줄 — 세 상태. 서버에서 온 글은 textContent 로만
  function renderEntryLine() {
    if (!$entry) return;
    $entry.hidden = !data;
    if (!data) return;
    const on = !!data.enabled;
    const n = count();
    const state = !on ? "off" : n ? "has" : "zero";
    $entry.classList.remove("has", "zero", "off");
    $entry.classList.add(state);
    $entry.setAttribute("aria-disabled", on ? "false" : "true");
    $entry.tabIndex = on ? 0 : -1;
    const part = function (cls, text) { $entry.querySelector(".chg-entry-" + cls).textContent = text; };
    if (!on) {
      part("what", "Change tracking is off");
      part("since", "· " + (data.reason || ""));
      part("go", "");
      const detail = data.detail && data.detail !== data.reason ? data.detail : "";
      $entry.title = (data.reason || "") + (detail ? " — " + detail : "");
      return;
    }
    const since = sinceText(data.baseline);
    part("what", n ? n + (n === 1 ? " file" : " files") + " changed" : "No changes");
    part("since", "· " + since);
    part("go", n ? "View changes ›" : "Open ›");
    $entry.title = n ? tip(n) : "No files changed on disk " + since + " — open Changes";
  }
  function renderEntries() {
    renderBadge();
    placeLine();
    renderEntryLine();
    decorateTree();
  }

  // ── 📁 트리 표시 — 행은 workspace.js 가 그리고 여기는 data-rel·data-type 으로 꾸미기만
  function indexEntries() {
    byPath = new Map();
    dirty = new Set();
    if (!count()) return;
    data.files.forEach(function (f) {
      byPath.set(f.path, f);
      // 모든 상위 폴더(루트 "" 포함)에 점 — 삭제된 파일도 여기서만 드러난다
      let i = f.path.lastIndexOf("/");
      while (i > 0) {
        dirty.add(f.path.slice(0, i));
        i = f.path.lastIndexOf("/", i - 1);
      }
      dirty.add("");
    });
  }
  function treeMark(row) {
    const rel = row.dataset.rel;
    if (rel === undefined) return null;
    const since = sinceText(data.baseline);
    const type = row.dataset.type;
    const f = byPath.get(rel);
    // 파일·symlink — 목록의 글자 그대로(삭제된 것은 트리에 없다)
    if ((type === "file" || type === "link") && f && f.status !== "D") {
      const b = el("button", ["chg-mark", "chg-st", "chg-st-" + f.status], f.status);
      b.title = (f.status === "A" ? "Added " : "Modified ") + since;
      b.addEventListener("click", function (e) {
        e.stopPropagation(); // 체크박스·폴더 펼침·업로드 대상으로 번지지 않게
        open(rel);
      });
      return b;
    }
    if (type !== "dir") return null;
    // 기준점 뒤에 생긴 하위 저장소의 요약 줄 — 누르면 그 요약
    if (f && f.summary) {
      const b = el("button", ["chg-mark", "chg-dot"], "●");
      b.title = "New repository " + since;
      b.addEventListener("click", function (e) {
        e.stopPropagation();
        open(rel);
      });
      return b;
    }
    // 폴더 행 자체가 목록에 있으면 하위 git 저장소(gitlink)거나 안을 가리키는 폴더 링크의
    // 대상이 바뀐 것(서버는 그런 링크를 대상 종류 dir 로 보인다 — 프런트는 둘을 못 가른다).
    // 그 경로 diff 를 연다
    if (f) {
      const b = el("button", ["chg-mark", "chg-dot"], "●");
      b.title = "Nested repository or symlink changed " + since;
      b.addEventListener("click", function (e) {
        e.stopPropagation();
        open(rel);
      });
      return b;
    }
    if (dirty.has(rel)) {
      const b = el("button", ["chg-mark", "chg-dot"], "●");
      b.title = "Contains changed files";
      b.addEventListener("click", function (e) {
        e.stopPropagation();
        open();
      });
      return b;
    }
    return null;
  }
  // 지금 디스크에 있는 하위 저장소 폴더(지워진 것은 트리에 없다)
  function isRepoDir(row) {
    if (row.dataset.type !== "dir" || !data || !data.enabled) return false;
    return (data.repos || []).some(function (r) {
      return r.path === row.dataset.rel && r.state !== "removed";
    });
  }
  function decorateRow(row) {
    row.querySelectorAll(":scope > .chg-mark, :scope > .chg-git").forEach(function (n) { n.remove(); });
    if (isRepoDir(row)) {
      const g = el("span", ["chg-git"], "git");
      g.title = "Nested git repository — changes inside are counted with its own git";
      row.appendChild(g);
    }
    const m = count() ? treeMark(row) : null;
    if (m) {
      m.type = "button";
      row.appendChild(m);
    }
  }
  function decorateTree() {
    if ($tree) $tree.querySelectorAll(".dl-row").forEach(decorateRow);
  }

  function countsHtml(f) {
    if (f.summary) return "";
    if (f.added == null && f.deleted == null) return '<span class="chg-bin">binary</span>';
    let s = "";
    if (f.added) s += '<span class="chg-add">+' + f.added + "</span>";
    if (f.deleted) s += (s ? " " : "") + '<span class="chg-del">−' + f.deleted + "</span>";
    return s;
  }

  // Clear 는 비울 것이 있을 때만 — 목록 0 이고 보이는 하위 저장소 그룹도 0 이면 끈다
  // (0 에서 누르면 빈 기준점 커밋과 "List cleared (0 files)" 가 생겼다). Undo 토스트는 따로 산다.
  const clearTitle = $clear.title;
  function syncClear() {
    const on = !!(data && data.enabled);
    const some = on && (data.files.length > 0 || shownRepos().length > 0);
    $clear.disabled = !some;
    $clear.title = !on || some ? clearTitle : "Nothing to clear";
  }

  function renderPanel() {
    $list.textContent = "";
    const on = !!(data && data.enabled);
    syncClear();
    $drawer.querySelector("#changes-refresh").disabled = !on;
    if (!on) {
      $since.textContent = "";
      $list.appendChild(el("div", ["chg-empty"], "Snapshots are off in this room"));
      showDiff(null);
      return;
    }
    $since.textContent = sinceText(data.baseline);
    renderNotices();
    const groups = shownRepos();
    if (!data.files.length && !groups.length) {
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
    if (!groups.length) {
      data.files.forEach(addRow);
    } else {
      const ws = data.files.filter(function (f) { return !f.repo; });
      if (ws.length) {
        $list.appendChild(el("div", ["chg-grp"], "Workspace"));
        ws.forEach(addRow);
      }
      groups.forEach(function (r) {
        const head = el("div", ["chg-grp"], r.path + "/");
        head.dataset.repo = r.path;
        head.appendChild(el("span", ["chg-grp-tag"], repoTag(r)));
        $list.appendChild(head);
        const rows = data.files.filter(function (f) { return f.repo === r.path; });
        if (!rows.length) {
          $list.appendChild(el("div", ["chg-grp-empty"], "No changes in " + r.path + " since " + hhmm(r.since) + "."));
        }
        rows.forEach(addRow);
      });
    }
    if (selected) loadDiff(selected);
    else showDiff(null);
  }

  function addRow(f) {
    const row = el("button", ["chg-row"]);
    row.type = "button";
    row.dataset.path = f.path;
    if (f.summary) row.classList.add("chg-summary");
    row.appendChild(el("span", ["chg-caret"]));
    markSel(row, f.path === selected);
    row.appendChild(el("span", ["chg-st", "chg-st-" + f.status], f.status));
    const shown = f.summary ? f.path + "/" : f.path;
    const p = el("span", ["chg-path"], shown);
    p.title = shown;
    row.appendChild(p);
    row.appendChild(elHtml("span", ["chg-n"], countsHtml(f)));
    row.addEventListener("click", function () { pick(f.path); });
    $list.appendChild(row);
  }

  // 그룹으로 보일 하위 저장소 — 항목이 있거나, 기준을 다시 잡아 "지금부터" 를 말해야 하는 것
  function shownRepos() {
    return (data.repos || []).filter(function (r) {
      return r.state === "reset" || data.files.some(function (f) { return f.repo === r.path; });
    });
  }
  function repoTag(r) {
    if (r.state === "removed") return "repository removed";
    if (r.state === "new") return "new repository · since " + hhmm(r.since);
    if (r.state === "reset") return "since " + hhmm(r.since) + " · baseline reset";
    const from = r.head_from || "none";
    if (!r.head_to || r.head_to === r.head_from) return "HEAD " + from;
    let t = "HEAD " + from + " → " + r.head_to;
    if (r.commits) t += " (+" + r.commits + (r.commits === 1 ? " commit)" : " commits)");
    return t;
  }
  // 안내 줄 — 기준을 잃은·커밋 없는 하위 저장소, 스냅샷 실패. 문장이 저장소 이름으로 시작하면 그 이름을 굵게
  function renderNotices() {
    (data.notices || []).forEach(function (n) {
      const box = el("div", ["chg-notice"]);
      box.setAttribute("role", "status");
      if (n.cause === "snapshot_failing") box.classList.add("chg-notice-err");
      if (n.repo) box.dataset.repo = n.repo;
      box.appendChild(el("span", [], "⚠"));
      const text = el("span", []);
      if (n.repo && n.text.indexOf(n.repo) === 0) {
        text.appendChild(el("b", [], n.repo));
        text.appendChild(document.createTextNode(n.text.slice(n.repo.length)));
      } else {
        text.textContent = n.text;
      }
      box.appendChild(text);
      if (n.dismissable !== false) {
        const x = el("button", ["chg-notice-x"], "✕");
        x.type = "button";
        x.title = "Dismiss";
        x.setAttribute("aria-label", "Dismiss");
        x.addEventListener("click", function () { dismiss(n.repo); });
        box.appendChild(x);
      }
      $list.appendChild(box);
    });
  }
  function dismiss(repo) {
    fetch("api/changes/notices/dismiss", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ repo: repo }),
    })
      .then(function (r) { if (!r.ok) throw new Error(); return r.json(); })
      .then(apply)
      .catch(recount);
  }

  function showDiff(text) {
    $diff.hidden = text === null;
    if (text === null) { $diff.textContent = ""; return; }
    $diff.innerHTML = text ? colorizeDiffBody(escapeHtml(text)) : "No difference on disk now.";
  }
  function markSel(row, on) {
    row.classList.toggle("sel", on);
    row.setAttribute("aria-expanded", on ? "true" : "false");
    row.querySelector(".chg-caret").textContent = on ? "▾" : "▸";
  }
  // 고른 행을 다시 누르면 접는다 — diff 를 닫고 선택을 푼다
  function pick(path) {
    selected = selected === path ? null : path;
    $list.querySelectorAll(".chg-row").forEach(function (r) {
      markSel(r, r.dataset.path === selected);
    });
    if (selected) loadDiff(selected);
    else showDiff(null);
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
    indexEntries();
    renderEntries();
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
      .then(syncClear);
  });
  document.getElementById("changes-refresh").addEventListener("click", recount);

  // path 를 주면 그 파일 diff 를 연 채로(📁 트리의 파일 글자)
  function open(path) {
    if (typeof path === "string") selected = path;
    $backdrop.hidden = false;
    requestAnimationFrame(function () {
      $backdrop.classList.add("open");
      $drawer.classList.add("open");
    });
    $drawer.setAttribute("aria-hidden", "false");
    renderPanel();
    recount();
  }
  function close() {
    $backdrop.classList.remove("open");
    $drawer.classList.remove("open");
    $drawer.setAttribute("aria-hidden", "true");
    setTimeout(function () { $backdrop.hidden = true; }, 260);
  }
  // 배지는 📁 버튼 안에 있다 — 버블링을 막아야 Workspace files 드로어가 같이 열리지 않는다
  $badge.addEventListener("click", function (e) {
    e.stopPropagation();
    open();
  });
  $line.addEventListener("click", function () { open(); });
  if ($entry) {
    $entry.addEventListener("click", function () {
      if ($entry.getAttribute("aria-disabled") !== "true") open();
    });
  }
  // 📁 트리에 새로 그려진 행만 꾸민다(폴더 펼침의 늦은 fetch 포함). 트리가 처음 그려질 때
  // (루트 행) 목록이 아직 없으면 한 번 센다. 표시 버튼 자체의 추가는 행이 아니라 지나친다.
  if ($tree) {
    new MutationObserver(function (records) {
      let fresh = false;
      records.forEach(function (r) {
        Array.prototype.forEach.call(r.addedNodes, function (n) {
          if (n.nodeType !== 1 || n.classList.contains("chg-mark")) return;
          const rows = n.classList.contains("dl-row") ? [n] : n.querySelectorAll(".dl-row");
          Array.prototype.forEach.call(rows, function (row) {
            if (row.dataset.rel === "") fresh = true;
            if (data) decorateRow(row);
          });
        });
      });
      if (fresh && !data) recount();
    }).observe($tree, { childList: true, subtree: true });
  }
  // 새 main 최종답 카드가 그려지면 줄을 그리로 — 직계 자식 추가만 본다(subtree 아님)
  new MutationObserver(function (records) {
    if (!count()) return;
    const hit = records.some(function (r) {
      return Array.prototype.some.call(r.addedNodes, function (n) {
        return n.nodeType === 1 && n.dataset.ch === "main" &&
          n.classList.contains("card-assistant") && !!n.querySelector(":scope > .final");
      });
    });
    if (hit) placeLine();
  }).observe($messages, { childList: true });
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
