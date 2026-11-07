// frames.js — 모델 시점(ctx_view) + 틀(맨 위 카드·맨 아래 띠). 테스트 훅 window.__ctxFrames.
// (v10.117.0, docs/web-modules/DESIGN.md §8 — 옛 main.js 의 절 [10]·[11])

import { $messages, el } from "./dom.js";
import { escapeHtml } from "./markdown.js";
import { scheduleScroll } from "./scroll.js";
import { copyToClipboard } from "./sse.js";
import { flashCopied } from "./copy.js";
import { applyChannelFilter, ovActiveChannel } from "./channels.js";
import { channelOf, finishCard, scopeChannel } from "./timeline.js";
import { fmtTok } from "./share.js";
import { runBlocks } from "./runs.js";

// ── 모델 시점 (v10.17.0, docs/inspector-model-view) ─────────────
// 화면은 위에서 아래로 **모델이 받는 순서**다. main · 상주 에이전트 채널 ·
// 인라인 카드(agent/skill)가 같은 세 부분을 가진다:
//   맨 위  "맨 처음 받는 것"   — 시스템 프롬프트·함수 스키마·문법·압축 요약
//   가운데 대화               — 지금 컨텍스트에 있는 카드
//   맨 아래 "매 턴 끝에 붙는 것" — 매턴 꼬리(직전 턴과의 diff)
// 컨텍스트에서 빠진 카드는 한 묶음으로 접힌다(기본 접힘, 펼치면 흐린 카드).
// 종전의 🔍 드로어가 보여 주던 것이 전부 이 틀 안에 있다.
//
// 서버의 sticky `ctx_view` 가 스코프별로 "캐시에서 빠진 접두사의 경계" 와 압축
// 요약을 보낸다. 뷰의 열쇠는 **컨테이너**다: 인라인 카드면 그 카드(task_id),
// 아니면 채널(main / 상주 에이전트 key) — 상주 에이전트의 런 스코프 id 는
// 런마다 다르므로 채널로 모은다(그 에이전트의 컨텍스트는 런을 넘어 이어진다).
export const ctxViews = {}; // viewKey → ctx_view payload
const ctxFoldOpen = {}; // viewKey → "컨텍스트 밖 대화" 묶음을 펼쳤나
const ctxFoldRows = {}; // viewKey → 묶음 머리 줄
const ctxApplyTimers = {};
export const frames = {}; // viewKey → 틀 {key, scope, channel, top, tail, body, data, …}
const $ctxTail = document.getElementById("ctx-tail");
const $ctxBudget = document.getElementById("ctx-budget");

export function ctxViewKey(taskId) {
  // 인라인 카드의 틀은 카드가 닫힌 뒤에도 남는다(`taskGroups` 항목은 종료 때
  // 풀린다) — 재접속 스냅샷이 끝난 스코프의 뷰를 채널에 잘못 덮지 않게 한다.
  if (!taskId) return "main";
  if (frames[taskId] && !frames[taskId].channel) return taskId;
  // 상주 에이전트의 뷰는 런 스코프(`key#<item id>`)가 아니라 그 에이전트의 프롬프트
  // 스코프 = **채널 key 그대로** 온다. 모르는 id 를 main 으로 돌리면 그 뷰가
  // main 의 뷰를 덮어 "컨텍스트 밖 대화" 가 통째로 사라진다(v10.18.1) — 모르는
  // id 는 자기 열쇠로 둔다.
  return scopeChannel[taskId] || taskId;
}
// 경계는 history 레코드 서수 하나(`gone.hidx` = 캐시에 남은 첫 레코드) —
// 카드는 자기 레코드의 서수(`data-hidx`, 이벤트의 `hidx`)가 그보다 작으면 빠진 것.
// 턴 번호는 런마다 1 부터 다시 시작해 경계가 될 수 없다(실측).
// 예외는 `gone.kept` — 경계 앞인데 압축이 원문으로 남긴 미답 요청(v10.41.0).
function ctxCardGone(card, gone) {
  if (!gone || card.dataset.hidx === undefined) return false;
  const h = Number(card.dataset.hidx);
  if (gone.kept && gone.kept.includes(h)) return false;
  return h < gone.hidx;
}
export function ctxApplyToCard(card, taskId) {
  if (!card.dataset || card.dataset.hidx === undefined) return;
  const key = ctxViewKey(taskId);
  const v = ctxViews[key];
  if (!v) return;
  // 빠진 카드가 뷰보다 늦게 오면(재생 순서) 묶음을 다시 센다 — 한 번으로 합쳐서.
  if (ctxCardGone(card, v.gone)) ctxScheduleApply(key);
}
function ctxScheduleApply(key) {
  if (ctxApplyTimers[key]) return;
  ctxApplyTimers[key] = setTimeout(function () {
    delete ctxApplyTimers[key];
    ctxApplyView(key);
  }, 0);
}
/** 뷰의 카드들. `root` 는 그 카드가 놓인 컨테이너 직계 노드 — 채널에서는
 * 런 블록 안의 카드도 그 채널의 대화라 블록을 풀어 넣는다. */
function ctxCards(key) {
  const f = frames[key];
  const out = [];
  if (f && !f.channel) {
    Array.prototype.forEach.call(f.body.children, function (n) {
      if (n.classList.contains("card")) out.push({ card: n, root: n });
    });
    return { parent: f.body, cards: out };
  }
  Array.prototype.forEach.call($messages.children, function (n) {
    if (!n.dataset || n.dataset.ch !== key || !n.classList.contains("card")) return;
    if (n.classList.contains("card-run")) {
      const body = n.querySelector(":scope > .run-body");
      Array.prototype.forEach.call(body ? body.children : [], function (c) {
        if (c.classList.contains("card")) out.push({ card: c, root: n });
      });
    } else {
      out.push({ card: n, root: n });
    }
  });
  return { parent: $messages, cards: out };
}
function ctxFoldRow(key) {
  const row = el("button", ["ctx-fold"]);
  row.type = "button";
  row.appendChild(el("span", ["ctx-chev"], "▶"));
  row.appendChild(el("span", ["ctx-title"], "Evicted"));
  row.appendChild(el("span", ["ctx-count"], ""));
  row.appendChild(el("span", ["ctx-note"], "The model no longer sees this part"));
  row.addEventListener("click", function () {
    ctxFoldOpen[key] = !ctxFoldOpen[key];
    ctxApplyView(key);
  });
  return row;
}
export function ctxApplyView(key) {
  const v = ctxViews[key];
  if (!v) return;
  const found = ctxCards(key);
  const open = !!ctxFoldOpen[key];
  let first = null;
  let n = 0;
  const blocks = []; // 런 블록 → {el, all, gone}
  found.cards.forEach(function (it) {
    const c = it.card;
    const gone = ctxCardGone(c, v.gone);
    c.classList.toggle("ctx-gone", gone);
    c.classList.toggle("ctx-folded", gone && !open);
    if (gone) {
      c.dataset.ctxKey = key;
      n += 1;
      if (!first) first = it.root;
    }
    if (it.root !== c && c.dataset.hidx !== undefined) {
      let b = blocks.length && blocks[blocks.length - 1].el === it.root ? blocks[blocks.length - 1] : null;
      if (!b) { b = { el: it.root, all: 0, gone: 0 }; blocks.push(b); }
      b.all += 1;
      if (gone) b.gone += 1;
    }
  });
  // 기록이 전부 빠진 런 블록은 블록째 접는다(머리만 남아 떠 있지 않게).
  blocks.forEach(function (b) {
    const whole = b.all > 0 && b.gone === b.all;
    b.el.classList.toggle("ctx-gone", whole);
    b.el.classList.toggle("ctx-folded", whole && !open);
    if (whole) b.el.dataset.ctxKey = key;
  });
  let row = ctxFoldRows[key];
  if (!n) {
    if (row) { row.remove(); delete ctxFoldRows[key]; }
  } else {
    if (!row) {
      row = ctxFoldRows[key] = ctxFoldRow(key);
      if (found.parent === $messages) row.dataset.ch = key;
    }
    if (row.nextElementSibling !== first) first.insertAdjacentElement("beforebegin", row);
    if (found.parent === $messages) applyChannelFilter(row);
    row.classList.toggle("open", open);
    row.setAttribute("aria-expanded", open ? "true" : "false");
    row.querySelector(".ctx-chev").textContent = open ? "▼" : "▶";
    row.querySelector(".ctx-count").textContent = n + " cards";
  }
  if (frames[key]) frameRender(frames[key]); // 압축 요약은 맨 위 카드에 산다
}
/** 접힌 묶음 안의 카드로 점프하려면 먼저 묶음을 편다. */
export function ctxReveal(node) {
  const folded = node && node.closest ? node.closest(".ctx-folded") : null;
  if (!folded || !folded.dataset.ctxKey) return;
  ctxFoldOpen[folded.dataset.ctxKey] = true;
  ctxApplyView(folded.dataset.ctxKey);
}

// ── 틀: 맨 위 카드 + 맨 아래 띠 ──
// 데이터는 `GET api/debug/prompt?task_id=` (그 스코프의 마지막 LLM 호출
// 스냅샷). 보일 때만 가져온다 — 재생이 인라인 카드를 수백 개 만들어도 접힌
// 카드는 요청을 내지 않는다(IntersectionObserver: 안 보이는 요소는 교차하지
// 않는다). 턴이 끝나면(`token_usage`) 보이는 틀만 다시 가져온다.
const frameSeen = window.IntersectionObserver
  ? new IntersectionObserver(function (entries) {
      entries.forEach(function (en) {
        if (en.isIntersecting && en.target.__frame) frameLoad(en.target.__frame);
      });
    })
  : null;

function frameMake(key, scope, channel) {
  const f = {
    key: key, scope: scope, channel: channel,
    top: el("div", ["ctx-top"]), tail: el("div", ["ctx-tail"]),
    body: null, data: null, stale: true, loading: false, again: false,
    open: { top: false, tail: false }, sig: { top: "", tail: "" },
  };
  frames[key] = f;
  ["top", "tail"].forEach(function (which) {
    const part = f[which];
    part.__frame = f;
    part.addEventListener("click", function (e) { frameClick(f, which, e); });
    if (frameSeen) frameSeen.observe(part);
  });
  // 복사: 맨 위 카드가 압축 요약을 싣는다(대화 복사에 요약이 빠지지 않게).
  f.top._md = function () {
    const v = ctxViews[key];
    const s = v && v.summary;
    if (!s) return "";
    let body = s.text || "";
    if (s.files && s.files.length) body += "\nFiles: " + s.files.join(", ");
    return "> ⊙ **Compaction summary** · " + frameSummaryMeta(v) + "\n>\n> " + body.split("\n").join("\n> ");
  };
  frameRender(f);
  if (!frameSeen) frameLoad(f);
  return f;
}
/** 채널(main / 상주 에이전트)의 틀 — 맨 위 카드는 타임라인 맨 앞, 띠는 입력창 위. */
function frameForChannel(key) {
  if (frames[key]) return frames[key];
  const f = frameMake(key, key === "main" ? "" : key, true);
  f.top.dataset.ch = key;
  $messages.insertBefore(f.top, $messages.firstChild);
  applyChannelFilter(f.top);
  if ($ctxTail) $ctxTail.appendChild(f.tail);
  f.tail.hidden = key !== ovActiveChannel;
  return f;
}
/** 인라인 카드의 틀 — 맨 위 카드는 머리 바로 아래, 띠는 몸통 바로 아래. */
export function frameForCard(taskId, header, body) {
  if (frames[taskId]) return frames[taskId];
  const f = frameMake(taskId, taskId, false);
  f.body = body;
  header.insertAdjacentElement("afterend", f.top);
  body.insertAdjacentElement("afterend", f.tail);
  return f;
}
/** 채널 전환 — 띠와 예산 줄은 지금 채널의 것만 보인다. */
export function frameShowChannel(key) {
  frameForChannel(key);
  Object.keys(frames).forEach(function (k) {
    if (frames[k].channel) frames[k].tail.hidden = k !== key;
  });
  frameRenderBudget(frames[key]);
}
/** 이벤트의 task_id → 틀. 상주 에이전트의 런(`<key>#<n>`)은 그 채널의 틀. */
export function frameOfScope(taskId) {
  if (!taskId) return frames.main || null;
  return frames[taskId] || frames[taskId.replace(/#\d+$/, "")] ||
    (runBlocks[taskId] ? frames[channelOf(taskId)] : null) || null;
}
function frameVisible(f) {
  return f.top.offsetParent !== null || f.tail.offsetParent !== null;
}
/** 그 스코프의 스냅샷이 바뀌었다 — 보이면 지금, 아니면 다음에 보일 때 가져온다. */
export function frameTouch(f) {
  if (!f) return;
  f.stale = true;
  if (frameVisible(f)) frameLoad(f);
}
function frameLoad(f) {
  if (!f.stale) return;
  if (f.loading) { f.again = true; return; }
  f.loading = true;
  f.stale = false;
  const q = f.scope ? "?task_id=" + encodeURIComponent(f.scope) : "";
  fetch("api/debug/prompt" + q)
    .then(function (r) { return r.json(); })
    .then(function (data) { f.data = data; })
    .catch(function () { f.stale = true; })
    .then(function () {
      f.loading = false;
      frameRender(f);
      if (f.again) { f.again = false; f.stale = true; frameLoad(f); }
    });
}

// 줄 단위 LCS diff — 꼬리는 수십 줄이라 O(n·m) 으로 충분하다.
function lineDiff(prev, cur) {
  const a = prev.split("\n"), b = cur.split("\n");
  const n = a.length, m = b.length;
  const dp = [];
  for (let i = 0; i <= n; i++) { dp.push(new Array(m + 1).fill(0)); }
  for (let i = n - 1; i >= 0; i--) {
    for (let j = m - 1; j >= 0; j--) {
      dp[i][j] = a[i] === b[j] ? dp[i + 1][j + 1] + 1 : Math.max(dp[i + 1][j], dp[i][j + 1]);
    }
  }
  const out = [];
  let i = 0, j = 0, ins = 0, del = 0;
  while (i < n && j < m) {
    if (a[i] === b[j]) { out.push(["=", b[j]]); i++; j++; }
    else if (dp[i + 1][j] >= dp[i][j + 1]) { out.push(["-", a[i]]); i++; del++; }
    else { out.push(["+", b[j]]); j++; ins++; }
  }
  while (i < n) { out.push(["-", a[i++]]); del++; }
  while (j < m) { out.push(["+", b[j++]]); ins++; }
  return { lines: out, ins: ins, del: del };
}
function frameSecHtml(s, cls, extraTag) {
  return (
    '<details class="insp-sec ' + cls + '" data-sec="' + escapeHtml(cls + ":" + s.name) + '">' +
    "<summary>" +
    '<span class="insp-name">' + escapeHtml(s.name) + (extraTag || "") + "</span>" +
    '<span class="insp-tok">' + fmtTok(s.est_tokens) + "</span>" +
    '<button class="insp-cp" type="button" title="Copy this section">⧉</button>' +
    "</summary>" +
    '<pre class="insp-body">' + escapeHtml(s.text) + "</pre>" +
    "</details>"
  );
}
/** Open Items 의 `[u-1] …` 줄 — 그 호출 뒤에 닫혔으면(`closed_after`) 긋고
 * 표시한다. 꼬리 글은 그대로다: 모델이 받은 것을 고쳐 쓰지 않는다. */
function markClosed(raw, html, closed) {
  const m = /^\s*\[([A-Za-z]+-\d+)\]/.exec(raw);
  if (!m || !closed[m[1]]) return html;
  return '<span class="ctx-closed">' + html + '</span> <span class="insp-tag ok">✓ closed after this call</span>';
}
function frameTailSecHtml(s, d, prevTurn, closed) {
  let tags = "";
  const lines = s.text.split("\n");
  const hasClosed = lines.some(function (l) { return markClosed(l, "", closed) !== ""; });
  let body = '<pre class="insp-body">' +
    lines.map(function (l) { return markClosed(l, escapeHtml(l), closed); }).join("\n") + "</pre>";
  let open = true; // 첫 스냅샷 — 비교 대상이 없으면 펼친다
  if (d) {
    // 바뀐 섹션만 펼친다 — Standing Rules 는 늘 접힌다. 닫힌 항목이 있으면 그 섹션도.
    open = !!(d.ins || d.del) || hasClosed;
    if (open) {
      if (d.ins) tags += '<span class="insp-tag ins">+' + d.ins + "</span>";
      if (d.del) tags += '<span class="insp-tag del">−' + d.del + "</span>";
      if (prevTurn != null) tags += '<span class="insp-tag">vs turn ' + escapeHtml(prevTurn) + "</span>";
      body =
        '<pre class="insp-diff">' +
        d.lines
          .map(function (l) {
            if (l[0] === "+") return '<span class="ins">+ ' + markClosed(l[1], escapeHtml(l[1]), closed) + "</span>";
            if (l[0] === "-") return '<span class="del">- ' + escapeHtml(l[1]) + "</span>";
            return markClosed(l[1], escapeHtml(l[1]), closed);
          })
          .join("\n") +
        "</pre>";
    } else if (!hasClosed) {
      tags += '<span class="insp-tag">no change</span>';
    }
  }
  return (
    '<details class="insp-sec insp-tail"' + (open ? " open" : "") + ">" +
    "<summary>" +
    '<span class="insp-name">' + escapeHtml(s.name) + tags + "</span>" +
    '<span class="insp-tok">' + fmtTok(s.est_tokens) + "</span>" +
    '<button class="insp-cp" type="button" title="Copy this section">⧉</button>' +
    "</summary>" +
    body +
    "</details>"
  );
}
/** 모델이 지금 보는 양 — **서버가 센 값만** 그린다. 실측이 없으면(첫 호출 전 ·
 * usage 를 안 주는 서버 · 옛 세션) 추정으로 채우지 않고 N/A 로 둔다; 다음
 * 호출이 끝나면 채워진다. 칸은 실측 합계를 바이트 비율로 나눈 것이라 합이
 * 합계와 같다. 막대는 창 대비(빈 자리 = 남은 창). */
function frameBudgetHtml(b, ended) {
  const label = "Context" + (ended ? " (at end)" : "");
  if (!b.measured) {
    return (
      '<div class="bl"><span>' + label + '</span><span class="bn">N/A</span></div>' +
      '<div class="bleg"><span>' +
      (ended ? "No server-measured count was recorded." : "No server-measured count yet — filled after the next model call.") +
      "</span></div>"
    );
  }
  const base = Math.max(1, b.window || b.total);
  const seg = function (v, css) {
    return '<i style="width:' + Math.min(100, (100 * (v || 0)) / base).toFixed(1) + "%;background:var(" + css + ')"></i>';
  };
  const head =
    (b.window
      ? fmtTok(b.total) + " / " + fmtTok(b.window) + " (" + Math.round((100 * b.total) / b.window) + "%)"
      : fmtTok(b.total)) +
    (b.turn != null ? " · turn " + escapeHtml(b.turn) + " measured" : " · measured");
  return (
    '<div class="bl"><span>' + label + '</span><span class="bn">' + head + "</span></div>" +
    '<div class="bbar">' + seg(b.system, "--insp-system") + seg(b.tools, "--insp-tools") + seg(b.convo, "--insp-convo") + seg(b.tail, "--insp-tail") + "</div>" +
    '<div class="bleg">' +
    '<span><b style="background:var(--insp-system)"></b>system <span class="n">' + fmtTok(b.system) + "</span></span>" +
    (b.tools ? '<span><b style="background:var(--insp-tools)"></b>tool schemas <span class="n">' + fmtTok(b.tools) + "</span></span>" : "") +
    '<span><b style="background:var(--insp-convo)"></b>conversation <span class="n">' + fmtTok(b.convo) + "</span></span>" +
    '<span><b style="background:var(--insp-tail)"></b>tail <span class="n">' + fmtTok(b.tail) + "</span></span>" +
    (b.compactions ? '<span class="insp-tag">⊙ compactions ' + b.compactions + "</span>" : "") +
    "</div>"
  );
}
function frameRenderBudget(f) {
  if (!$ctxBudget || !f || !f.channel || f.key !== ovActiveChannel) return;
  const b = f.data && f.data.ok ? f.data.budget : null;
  $ctxBudget.hidden = !b;
  if (b) $ctxBudget.innerHTML = frameBudgetHtml(b, false);
}
function frameSummaryMeta(v) {
  const s = v.summary;
  let meta = "";
  if (s.turns && s.turns.length === 2) meta += "turns " + s.turns[0] + "–" + s.turns[1];
  if (s.before_tokens != null && s.after_tokens != null) {
    meta += (meta ? " · " : "") + fmtTok(s.before_tokens) + " → " + fmtTok(s.after_tokens) + " tok";
  }
  return meta || "compactions " + (v.compactions || 1);
}
function frameBarHtml(title, open, chips, right, tip) {
  return (
    '<button type="button" class="ctx-bar" aria-expanded="' + (open ? "true" : "false") + '"' +
    (tip ? ' title="' + escapeHtml(tip) + '"' : "") + ">" +
    '<span class="ctx-chev">' + (open ? "▼" : "▶") + "</span>" +
    '<span class="ctx-title">' + title + "</span>" +
    '<span class="ctx-chips">' + chips.join('<span class="ctx-dot"> · </span>') + "</span>" +
    '<span class="ctx-right">' + right + "</span>" +
    "</button>"
  );
}
/** 다시 그려도 사용자가 펼쳐 둔 섹션은 그대로 둔다. */
function frameSetHtml(part, html) {
  const openSecs = {};
  part.querySelectorAll("details[open][data-sec]").forEach(function (d) {
    openSecs[d.dataset.sec] = true;
  });
  part.innerHTML = html;
  part.querySelectorAll("details[data-sec]").forEach(function (d) {
    if (openSecs[d.dataset.sec]) d.open = true;
  });
}
function frameRender(f) {
  const d = f.data;
  const ok = !!(d && d.ok);
  const v = ctxViews[f.key];
  const sum = v && v.summary;
  const ended = !f.channel && ok && d.scope && d.scope.ended;
  const sys = [], tools = [], tail = [];
  let grammar = null;
  if (ok) {
    d.sections.forEach(function (s) {
      if (s.kind === "tools") tools.push(s);
      else if (s.kind === "tail") tail.push(s);
      else if (s.kind === "grammar") grammar = s;
      else if (s.kind === "system" || !s.kind) sys.push(s);
    });
  }
  const pending = !ok && (f.loading || f.stale);

  // ── 맨 위: 맨 처음 받는 것 ──
  const chips = [];
  if (ok) {
    const hooks = sys.filter(function (s) { return /^Hook: /.test(s.name); }).length;
    chips.push(sys.length + " system prompt sections");
    if (hooks) chips.push(hooks + " hook sections");
    if (tools.length) chips.push(tools.length + " tool schemas");
    if (grammar) chips.push("Decoding grammar");
  } else {
    chips.push(pending ? "…" : "no model call yet");
  }
  if (sum) {
    const t = sum.turns && sum.turns.length === 2 ? " turns " + sum.turns[0] + "–" + sum.turns[1] : "";
    chips.push('<span class="ctx-sumchip">Compaction summary' + t + "</span>");
  }
  const b = ok ? d.budget : null;
  let top = frameBarHtml(
    "Head", f.open.top, chips,
    // 섹션 배지와 같은 척도(추정) — 실측 합계는 예산 줄이 보여 준다.
    ok ? "~" + fmtTok(sys.concat(tools).reduce(function (n, x) { return n + (x.est_tokens || 0); }, 0)) + " tok" : "",
    "What the model receives first: system prompt · function schemas · decoding grammar · compaction summary"
  );
  if (f.open.top) {
    top += '<div class="ctx-body">';
    if (ok) {
      top += '<div class="ctx-actions"><button type="button" class="insp-mini ctx-copyall" title="Copy all system prompt and tool schema sections">⧉ Copy all</button></div>';
      if (!f.channel && b) top += '<div class="insp-budget">' + frameBudgetHtml(b, ended) + "</div>";
      top += '<div class="insp-group">System prompt</div>';
      top += sys.map(function (s) {
        return frameSecHtml(s, "insp-system", /^Hook: /.test(s.name) ? '<span class="insp-tag">hook · may change every turn</span>' : "");
      }).join("");
      if (tools.length) {
        top += '<div class="insp-group">Tool schemas — request tools[]</div>';
        top += tools.map(function (s) { return frameSecHtml(s, "insp-tools", ""); }).join("");
      }
      top += '<div class="insp-group">Request settings</div>';
      top += grammar
        ? frameSecHtml(
            { name: "Decoding grammar", text: grammar.text, est_tokens: grammar.est_tokens },
            "insp-grammar",
            '<span class="insp-tag">' + (grammar.thinking_open ? "thinking open" : "thinking closed") + " · enforced by the server</span>"
          )
        : '<div class="insp-sub">No decoding grammar — the server parses.</div>';
    } else {
      top += '<div class="insp-sub">' + (pending ? "Loading…" : "This scope has not called the model yet.") + "</div>";
    }
    if (sum) {
      let body = sum.text || "";
      if (sum.files && sum.files.length) body += "\nFiles: " + sum.files.join(", ");
      top +=
        '<div class="insp-group">Compaction summary — what the model sees instead of the evicted conversation</div>' +
        '<div class="ctx-summary"><div class="ctx-summary-meta">' + escapeHtml(frameSummaryMeta(v)) + "</div>" +
        '<div class="ctx-summary-body">' + escapeHtml(body) + "</div></div>";
    }
    top += "</div>";
  }
  if (top !== f.sig.top) { f.sig.top = top; frameSetHtml(f.top, top); }
  f.top.classList.toggle("open", f.open.top);

  // ── 맨 아래: 매 턴 끝에 붙는 것 ──
  const prevByName = {};
  ((ok && d.tail_prev) || []).forEach(function (s) { prevByName[s.name] = s.text; });
  let ins = 0, del = 0, tailTok = 0;
  const diffs = tail.map(function (s) {
    tailTok += s.est_tokens || 0;
    if (prevByName[s.name] === undefined) return null;
    const df = lineDiff(prevByName[s.name], s.text);
    ins += df.ins;
    del += df.del;
    return df;
  });
  // 이 호출 뒤에 닫힌 항목 (v10.63.0) — 런이 끝나면 서버가 단다.
  const closedIds = (ok && d.closed_after) || [];
  const closed = {};
  closedIds.forEach(function (id) { closed[id] = true; });
  const tchips = [];
  if (!ok) tchips.push(pending ? "…" : "no model call yet");
  else if (!tail.length) tchips.push(d.turn ? "this call had no tail" : "filled on the first model call");
  else {
    if (ended) tchips.push("state at end");
    // 꼬리 본문에서 읽는다: `turn 3/30 …` 한 줄과 `## ` 제목들.
    const text = tail.map(function (s) { return s.text; }).join("\n");
    const tm = /^turn (\d+)\/(\d+)/m.exec(text);
    tchips.push(tm ? "turn " + tm[1] + "/" + tm[2] : "turn " + d.turn);
    text.split("\n").forEach(function (line) {
      const hm = /^## (.+)$/.exec(line);
      if (hm) tchips.push(escapeHtml(hm[1]));
    });
  }
  let right = "";
  if (closedIds.length) right += '<span class="insp-tag ok">✓ ' + closedIds.length + " closed after</span>";
  if (ins) right += '<span class="insp-tag ins">+' + ins + "</span>";
  if (del) right += '<span class="insp-tag del">−' + del + "</span>";
  if ((ins || del) && d.prev_turn != null) right += '<span class="ctx-vs">vs turn ' + escapeHtml(d.prev_turn) + "</span>";
  if (tailTok) right += fmtTok(tailTok) + " tok";
  let tl = frameBarHtml("Tail", f.open.tail, tchips, right,
    "Appended to the last message on every model call · diff vs the previous call");
  if (f.open.tail) {
    tl += '<div class="ctx-body">';
    if (tail.length) {
      tl += '<div class="insp-sub">' + (ended ? "Snapshot at end — no further changes." : "As of the last model call (turn " + escapeHtml(d.turn) + ").") +
        (closedIds.length ? " Items marked ✓ were closed after this call." : "") + "</div>";
      tl += tail.map(function (s, i) { return frameTailSecHtml(s, diffs[i], d.prev_turn, closed); }).join("");
    } else {
      tl += '<div class="insp-sub">' + tchips[0] + "</div>";
    }
    tl += "</div>";
  }
  if (tl !== f.sig.tail) { f.sig.tail = tl; f.tail.innerHTML = tl; }
  f.tail.classList.toggle("open", f.open.tail);
  frameRenderBudget(f);
}
function frameClick(f, which, e) {
  const cp = e.target.closest(".insp-cp");
  if (cp) {
    // 섹션 복사 — <details> 토글로 번지지 않게.
    e.preventDefault();
    e.stopPropagation();
    const det = cp.closest(".insp-sec");
    const body = det && det.querySelector(".insp-body, .insp-diff");
    if (body) copyToClipboard(body.textContent).then(function () { flashCopied(cp); });
    return;
  }
  const all = e.target.closest(".ctx-copyall");
  if (all) {
    e.stopPropagation();
    if (!f.data || !f.data.ok) return;
    const text = f.data.sections
      .filter(function (s) { return s.kind !== "grammar" && s.kind !== "tail"; })
      .map(function (s) { return "### " + s.name + "\n" + s.text; })
      .join("\n\n");
    copyToClipboard(text).then(function () { flashCopied(all); });
    return;
  }
  if (e.target.closest(".ctx-bar")) {
    e.stopPropagation(); // 인라인 카드의 몸통 클릭(접기)으로 번지지 않게
    f.open[which] = !f.open[which];
    frameRender(f);
    frameLoad(f);
  }
}
window.__ctxFrames = frames; // 브라우저 테스트가 읽는다

// Inline context-compaction marker. `start` drops a "압축 중…" system line;
// `done`/`warning` update that same line in place (tracked per scope so a
// delegate subagent's compaction updates its own line, not main's). The
// marker is transient — not replayed on reconnect (see WebRenderer).
const compactionLines = {};
export function renderCompaction(d) {
  const scope = d.task_id || "main";
  let line = compactionLines[scope];
  if (d.phase === "start") {
    line = el("div", ["card", "card-sys"]);
    line.appendChild(el("span", ["sys-icon"], "⊙"));
    line.appendChild(
      el("span", ["sys-text"], "Compacting context… (" + fmtTok(d.old_tokens) + " tok)")
    );
    compactionLines[scope] = line;
    finishCard(line, d);
    return;
  }
  // done / warning: update the pending line, or append a fresh one if the
  // start event was missed (reconnect mid-compaction).
  if (!line) {
    line = el("div", ["card", "card-sys"]);
    line.appendChild(el("span", ["sys-icon"], "⊙"));
    line.appendChild(el("span", ["sys-text"], ""));
    finishCard(line, d);
  }
  const textEl = line.querySelector(".sys-text");
  if (d.phase === "done") {
    textEl.textContent =
      "Context compacted " + fmtTok(d.old_tokens) + " → " + fmtTok(d.new_tokens) + " tok";
  } else if (d.phase === "warning") {
    line.classList.add("warn");
    textEl.textContent = "Context compaction failed (" + (d.reason || "") + ") — using FIFO";
  }
  delete compactionLines[scope];
  scheduleScroll();
}

/** `card-sys` 한 줄 — 아이콘 + 텍스트. 타임라인 카드와 달리 4칸 리듬이
 * 아니라 가운데 정렬된 **구분선**에 가깝다(압축·무진전·시스템 고지).
 * 손으로 일곱 번 쓰이던 것을 한 곳으로. 제자리 갱신하는 호출부는 반환된
 * 노드의 `.sys-text` 를 그대로 잡아 쓴다. */
export function sysLine(icon, text, extraCls) {
  const line = el("div", ["card", "card-sys"].concat(extraCls || []));
  line.appendChild(el("span", ["sys-icon"], icon));
  line.appendChild(el("span", ["sys-text"], text || ""));
  return line;
}
