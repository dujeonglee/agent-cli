// runs.js — 런 블록: 에이전트 런의 접힌 블록, 채널 칩 동기화·채널 입력 라우팅.
// (v10.117.0, docs/web-modules/DESIGN.md §8 — 옛 main.js 의 절 [29])

import {
  $chatStop,
  $chatStopMore,
  $input,
  $messages,
  $send,
  $stopMenu,
  el,
  elHtml,
  foldOnClick,
} from "./dom.js";
import { session } from "./state.js";
import { escapeAndFormat, escapeHtml } from "./markdown.js";
import { scheduleScroll } from "./scroll.js";
import {
  cssEsc,
  ovActiveChannel,
  ovJump,
  ovAgentLabel,
  ovAgentLabelHtml,
  ovAgentWorking,
  ovBack,
  ovRenderAskTray,
  ovRenderBack,
  ovRoster,
  scrollTimelineTo,
  setOvActiveChannel,
  setOvBack,
  syncChannelView,
} from "./channels.js";
import { channelOf, finishCard, scopeChannel, stampCard } from "./timeline.js";
import { miniThumbs, thumbStrip } from "./thumbs.js";
import { makeRow, peerLabel } from "./cards.js";
import { attachMessage } from "./attach.js";
import { sendOnce } from "./send.js";

// ── 런 블록 (v9.23.0, docs/chat-ui §12) ─────────────────────
// 에이전트 채널의 단위는 **런**이다: 런이 시작될 때 블록이 열리고
// (`scope_start`, 앵커 = `key#<item id>`), 그 런이 받은 항목(들)이 머리, 단계가
// 몸통, 결과가 꼬리. 왕래 줄 셋(← 받음 / → 보냄 / ❓ 질문)은 사라졌다:
//   · 받음 → 블록 머리. 수신 줄은 **큐에 넣을 때** 나가 런보다 앞서므로
//     블록이 없으면 `⏳ 대기` 로 두었다가 그 id 를 품은 런이 열리면 끌어온다
//     (`scope_start.items` — 배치는 N개; v10.77.0 부터 배달 id 가 키). 앵커를 수신 줄로 잡으면 바쁠 때
//     쌓인 3건이 블록 3개를 먼저 연다.
//   · 보냄 → 그리지 않는다. `message`/`reply`/`answer` **도구 줄**이 그 발신
//     이고(라이브·resume 재생 둘 다 history 에서 온다), 유일한 예외는 도구
//     호출 없는 하네스 폴백(`fallback`) — 그것만 ⚠ 줄로 몸통에.
//   · 질문 → `→ ask` 도구 줄. 트레이 갱신만 남는다.
// resume: `scopes.jsonl` 이 같은 scope_start/end 를 재생하고 에이전트
// history 가 도구 줄을 재생하므로 재생 소스는 **새로 늘지 않는다**.
export const runBlocks = {}; // task_id → {card, head, body, key, items, closed}
const runByItem = {}; // key → {item id → task_id}
const queuedIn = {}; // key → {item id → {d, card}}  블록이 아직 없는 수신 줄
const pendingFallback = {}; // key → {item id → [d]}  블록이 아직 없는 폴백 줄

function firstLineOf(text) {
  var t = String(text || "");
  return (t.split("\n").find(function (l) { return l.trim(); }) || t).trim();
}

/** 수신 항목 한 줄 — 블록 머리의 그리드(`← 본문 · 누가`). */
function runItem(d) {
  var it = el("div", ["run-item"]);
  it.appendChild(el("span", ["ic"], "←"));
  var text = String(d.text || "");
  var first = firstLineOf(text);
  var sCell = el("span", ["s"], first);
  it.appendChild(sCell);
  it.appendChild(el("span", ["who"], peerLabel(d.author)));
  var imgs = Array.isArray(d.images) ? d.images : [];
  if (imgs.length) sCell.appendChild(miniThumbs(imgs)); // 접힌 채로도 보인다 (v10.61.3)
  if (text.trim() !== first || imgs.length) {
    var body = elHtml("div", ["run-item-body", "md"], escapeAndFormat(text));
    if (imgs.length) body.appendChild(thumbStrip(imgs)); // 창에서 붙인 이미지 (v10.61.0)
    body.hidden = true;
    it.appendChild(body);
    it.classList.add("can");
    foldOnClick(it, function () {
      body.hidden = !body.hidden;
      it.classList.toggle("open", !body.hidden);
    });
  }
  return it;
}

function claimIn(blk, q) {
  if (q.card) q.card.remove();
  var it = runItem(q.d);
  it.dataset.item = String(q.d.item || "");
  blk.head.appendChild(it);
  linkHeadToCard(blk, it.dataset.item);
}

// ── 머리 줄 → 몸통의 그 카드 (v10.165.0) ─────────────────────
// 런 도중 흡수된 배달은 머리 줄과 몸통의 관찰 카드 둘로 선다. 둘을 잇는 열쇠는 그 배달의 id 다(`data-item` — 블록
// 번호가 아니다). 둘 다 서 있을 때만 머리 줄 끝에 ↓ 를 단다 — 카드가 없는 줄(런을 연 첫 항목, 사람이 창에 쓴 말)에는
// 없다. 머리 줄이 설 때(`claimIn`)와 카드가 설 때(cards.js 의 `agentcli:item-card`) 부른다.
function linkHeadToCard(blk, item) {
  if (!item) return;
  var sel = '[data-item="' + cssEsc(item) + '"]';
  var row = blk.head.querySelector(".run-item" + sel);
  if (!row || row.querySelector(".run-item-jump") || !blk.body.querySelector(sel)) return;
  var btn = el("button", ["run-item-jump"], "↓");
  btn.type = "button";
  btn.title = "Go to this message in the run";
  btn.addEventListener("click", function (e) {
    e.stopPropagation(); // 줄 클릭은 본문 펼침이다
    var card = blk.body.querySelector(sel);
    if (card) scrollTimelineTo(card);
  });
  row.querySelector(".who").appendChild(btn);
}
document.addEventListener("agentcli:item-card", function (e) {
  var card = e.detail.card;
  var run = card.closest(".card-run");
  var blk = run && runBlocks[run.dataset.taskId];
  if (blk) linkHeadToCard(blk, card.dataset.item);
});

export function ensureRunBlock(d) {
  if (runBlocks[d.task_id]) return runBlocks[d.task_id];
  var key = scopeChannel[d.task_id] || "";
  var items = Array.isArray(d.items) ? d.items.map(String) : [];
  var card = el("div", ["card", "card-run"]);
  card.dataset.taskId = d.task_id;
  var head = el("div", ["run-head"]);
  var body = el("div", ["run-body"]);
  card.appendChild(head);
  card.appendChild(body);
  var blk = { card: card, head: head, body: body, key: key, items: items, closed: false };
  runBlocks[d.task_id] = blk;
  // run 에이전트는 받은편지함이 없다 — 머리는 과제 원문과 부른 쪽(부른 자리의 채널)
  if (d.run) head.appendChild(runItem({ text: d.label, author: channelOf(d.caller_task || "") }));
  var byItem = runByItem[key] || (runByItem[key] = {});
  // 큐에서 먼저 도착한 수신 줄 → 머리 (배치는 받은 순서대로).
  var q = queuedIn[key] || {};
  var pf = pendingFallback[key] || {};
  items.forEach(function (id) {
    byItem[id] = d.task_id;
    if (q[id]) {
      claimIn(blk, q[id]);
      delete q[id];
    }
    (pf[id] || []).forEach(function (fd) { renderFallbackRow(blk, fd); });
    delete pf[id];
  });
  // 루트에, 자기 채널로. 시각은 첫 수신 줄이 아니라 런 시작이다.
  finishCard(card, { task_id: "", ts: d.ts }, key);
  return blk;
}

// ── run 에이전트의 한 줄 카드 (docs/agent-engine/DESIGN.md §2.5) ─────────
// run 에이전트의 런은 자기 채널의 런 블록이다. 부른 자리(`caller_task` — main 의
// 맨 위, 또는 부른 에이전트의 런 블록)에는 한 줄만 남는다:
//   → 🦊 explorer (agt-3a1b2c4d) · find every caller…   working · open channel
// 같은 `scope_start`/`scope_end` 가 사이드카에서 재생되므로 resume 도 같은 카드다.
const runLinks = {}; // run 블록의 task_id → {card, state}

// ── 끝난 run 접기 (v10.143.0, 시안 A) ─────────────────────
// 채널 줄의 칩은 main·일하는 에이전트·상주(죽은 상주의 ↻ 포함)뿐이다. 끝난 run 은 줄 끝 `Done N ▾` 하나로
// 접고, 누르면 최근 순 목록(이름·키·과제 앞부분·끝난 시각, 멈췄으면 stopped)에서 그 채널을 연다. 규칙은
// 하나 — 끝나면 접힌다(시간·개수 문턱 없음). 단 지금 보고 있는 채널은 접지 않는다: 보던 화면의 칩이 줄에서
// 사라지지 않게(다른 채널로 가면 접힌다). 목록의 과제·시각·정지는 그 run 의 스코프(과제 런 scope_start 의
// 원문, 마지막 scope_end 의 ts·stopped)에서 온다 — resume 은 사이드카가 같은 스코프를 재생해 같은 목록이 선다.
// 부른 쪽 대화의 한 줄 카드 "open channel" 은 그대로다. 화면 전용 — 모델 바이트와 무관.
const runTask = {}; // run 의 key → 과제 첫 줄
const runEnd = {}; // run 의 key → {ts, stopped}
const DONE_SHOWN = 8; // 목록이 처음 보이는 줄 수 — 나머지는 "+N more…"
const $doneMenu = document.getElementById("ov-done-menu");
var doneShowAll = false;

function doneMenuOpen() {
  return !!$doneMenu && $doneMenu.hidden === false;
}
function ovRunFolded(t) {
  return !!t.ends_after_task && t.state === "dead" && t.key !== ovActiveChannel;
}
function endSecs(ts) {
  if (typeof ts === "number") return ts;
  var ms = Date.parse(ts || "");
  return isNaN(ms) ? 0 : ms / 1000;
}
function agoText(ts) {
  var s = endSecs(ts);
  if (!s) return "";
  var d = Math.max(0, Math.floor(Date.now() / 1000 - s));
  if (d < 60) return "just now";
  if (d < 3600) return Math.floor(d / 60) + "m ago";
  if (d < 86400) return Math.floor(d / 3600) + "h ago";
  return Math.floor(d / 86400) + "d ago";
}
function doneRow(t) {
  var end = runEnd[t.key] || {};
  var task = runTask[t.key] || "";
  if (task.length > 80) task = task.slice(0, 79) + "…";
  return (
    '<button type="button" role="menuitem" class="ov-done-row" data-key="' + escapeHtml(t.key) + '">' +
    '<span class="ov-dot x"></span><span class="ov-done-lb">' + ovAgentLabelHtml(t.key) + "</span>" +
    (task ? '<span class="ov-done-task">· ' + escapeHtml(task) + "</span>" : "") +
    (end.stopped ? '<span class="ov-done-tag">stopped</span>' : "") +
    '<span class="ov-done-when">' + escapeHtml(agoText(end.ts)) + "</span></button>"
  );
}
/** 접힌 run 의 목록을 다시 그린다 — 열려 있을 때만. 최근에 끝난 것부터(끝난 시각을 모르면 명단의 뒤부터). */
function ovRenderDoneMenu(folded) {
  if (!doneMenuOpen()) return;
  var btn = document.getElementById("ov-done");
  if (!folded.length || !btn) {
    ovCloseDoneMenu();
    return;
  }
  var rows = folded
    .map(function (t, i) { return { t: t, at: endSecs((runEnd[t.key] || {}).ts), i: i }; })
    .sort(function (a, b) { return b.at - a.at || b.i - a.i; });
  var shown = doneShowAll ? rows : rows.slice(0, DONE_SHOWN);
  var html = '<div class="ov-done-h">Finished runs — newest first</div>';
  shown.forEach(function (r) { html += doneRow(r.t); });
  if (shown.length < rows.length) {
    html += '<button type="button" class="ov-done-more">+' + (rows.length - shown.length) + " more…</button>";
  }
  $doneMenu.innerHTML = html;
  // 버튼 위로 연다(채널 줄은 입력창 바로 위) — 화면 밖으로 나가지 않게 왼쪽을 당긴다
  var r = btn.getBoundingClientRect();
  $doneMenu.style.bottom = Math.round(window.innerHeight - r.top + 6) + "px";
  var w = $doneMenu.offsetWidth;
  $doneMenu.style.left = Math.round(Math.max(8, Math.min(r.left, window.innerWidth - w - 8))) + "px";
}
/** `Done N ▾` 를 눌렀다 — 목록을 열거나 닫는다. */
export function ovToggleDoneMenu() {
  if (!$doneMenu) return;
  if (doneMenuOpen()) return ovCloseDoneMenu();
  doneShowAll = false;
  $doneMenu.hidden = false;
  var btn = document.getElementById("ov-done");
  if (btn) btn.setAttribute("aria-expanded", "true");
  ovRenderDoneMenu(ovRoster.filter(ovRunFolded));
}
/** 목록의 나머지("+N more…")를 편다. */
export function ovShowAllDone() {
  doneShowAll = true;
  ovRenderDoneMenu(ovRoster.filter(ovRunFolded));
}
export function ovCloseDoneMenu() {
  if (!doneMenuOpen()) return;
  $doneMenu.hidden = true;
  $doneMenu.innerHTML = "";
  var btn = document.getElementById("ov-done");
  if (btn) btn.setAttribute("aria-expanded", "false");
}

export function ensureRunLink(d) {
  if (runLinks[d.task_id]) return runLinks[d.task_id];
  var key = scopeChannel[d.task_id] || "";
  var task = firstLineOf(d.label);
  var summary = (d.agent || key) + " (" + key + ")" + (task ? " · " + task : "");
  var state = el("span", ["run-link-state"], "working");
  var open = el("button", ["run-link-open"], "open channel");
  open.type = "button";
  open.addEventListener("click", function (e) {
    e.stopPropagation();
    ovJump(key);
  });
  var tail = el("span", ["run-link-tail"]);
  tail.appendChild(state);
  tail.appendChild(document.createTextNode(" · "));
  tail.appendChild(open);
  var card = el("div", ["card", "card-run-link"]);
  card.dataset.runKey = key;
  card.appendChild(makeRow("→", "Run", summary, null, null, tail));
  var link = { card: card, state: state };
  runLinks[d.task_id] = link;
  runTask[key] = task; // 끝난 run 목록의 과제 앞부분
  finishCard(card, { task_id: d.caller_task || "", ts: d.ts });
  return link;
}

export function closeRunLink(d) {
  var link = runLinks[d.task_id];
  if (!link) return;
  var ok = !!d.success;
  // 사람이 멈춘 run 은 실패가 아니라 정지다 (v10.142.0)
  link.state.textContent = ok ? "done" : d.stopped ? "stopped" : "failed";
  link.card.classList.add(ok ? "run-ok" : d.stopped ? "run-stopped" : "run-fail");
}

/** 꼬리 — 최종답이 있으면 **그 카드가 꼬리**(같은 말을 두 번 하지 않는다),
 * 없으면(중단·오류·complete 없이 끝남) 상태 한 줄. */
export function closeRunBlock(d) {
  var blk = runBlocks[d.task_id];
  if (!blk || blk.closed) return;
  blk.closed = true;
  runEnd[blk.key] = { ts: d.ts, stopped: !!d.stopped }; // 끝난 run 목록의 시각·정지 — 그 채널의 마지막 런
  var ok = !!d.success;
  blk.card.classList.add(ok ? "run-ok" : "run-fail");
  var dur = d.duration_s != null ? " (" + Number(d.duration_s).toFixed(1) + "s)" : "";
  var finalCard = null;
  var fins = blk.body.querySelectorAll(":scope > .card-assistant > .final");
  if (fins.length) finalCard = fins[fins.length - 1].parentNode;
  if (ok && finalCard) {
    finalCard.classList.add("run-final");
    var meta = el("span", ["run-meta"], "✓" + dur);
    var finEl = finalCard.querySelector(".final");
    finEl.insertBefore(meta, finEl.firstChild); // 여백 안 — CSS 참조
    scheduleScroll();
    return;
  }
  var foot = makeRow(
    ok ? "✓" : "✗",
    ok ? "Done" : "Failed",
    ok ? "ended without a result" + dur : (d.error || "stopped before the run ended") + dur,
    null,
    ["run-foot", ok ? "ok" : "bad"]
  );
  blk.body.appendChild(foot);
  scheduleScroll();
}

/** 하네스 폴백 — 회신 없이 끝난 런의 요약을 하네스가 대신 보냈다. 본문
 * 첫 줄은 그 사정을 말하는 라벨이라 요약에서는 뺀다. */
function renderFallbackRow(blk, d) {
  var text = String(d.text || "");
  var lines = text.split("\n");
  var rest = lines.slice(1).join("\n").trim();
  var body = rest ? elHtml("div", ["md"], escapeAndFormat(text)) : null;
  var tail = el("span", ["peer"], peerLabel(d.to));
  var row = makeRow("⚠", "Fallback", "ended without a reply — the harness sent the run summary instead", body, ["fallback"], tail);
  var card = el("div", ["card", "card-fallback"]);
  card.appendChild(row);
  stampCard(card, d.ts);
  blk.body.appendChild(card);
  scheduleScroll();
}

export function ovOnAgentMsg(d) {
  if (!d || !d.key) return;
  var key = d.key;
  var item = String(d.item || "");
  if (d.direction === "in") {
    var tid = runByItem[key] && runByItem[key][item];
    if (tid && runBlocks[tid]) {
      claimIn(runBlocks[tid], { d: d });
      return;
    }
    var card = el("div", ["card", "card-queued"]);
    card.dataset.item = item;
    var hasImgs = Array.isArray(d.images) && d.images.length > 0;
    var qRow = makeRow("⏳", "Queued", firstLineOf(d.text), hasImgs ? thumbStrip(d.images) : null,
                       ["queued"], el("span", ["peer"], peerLabel(d.author)));
    if (hasImgs) qRow.querySelector(".s").appendChild(miniThumbs(d.images)); // 접힌 채로도 보인다
    card.appendChild(qRow);
    // `d.task_id` 는 **보낸 쪽**의 런이다(peer 워커 스레드의 `_emit` 이
    // 붙인다) — 그걸로 배치하면 이 줄이 남의 블록 몸통에 들어간다. 수신
    // 줄의 자리는 언제나 자기 채널의 루트.
    finishCard(card, { ts: d.ts, note: d.note }, key);
    (queuedIn[key] || (queuedIn[key] = {}))[item] = { d: d, card: card };
    return;
  }
  if (d.direction === "absorbed") {
    // 런 도중 턴 경계에서 흡수됨 (v10.8.2): 그 id 의 ⏳ 대기 줄을 **도는 런**
    // (absorbed_into) 블록 머리로 옮긴다 — 새 런이 열리지 않으니 종전엔 배달
    // 뒤에도 "대기" 로 남았다. 블록이 없으면(재생 순서) 줄만 ✓ 로.
    var into = String(d.absorbed_into || "");
    var q = queuedIn[key] && queuedIn[key][item];
    var tidInto = runByItem[key] && runByItem[key][into];
    if (tidInto && runBlocks[tidInto]) {
      var blkInto = runBlocks[tidInto];
      if (q) {
        claimIn(blkInto, q);
        delete queuedIn[key][item];
      } else if (!blkInto.head.querySelector('.run-item[data-item="' + item + '"]')) {
        claimIn(blkInto, { d: d }); // 대기 줄이 안 왔다(버퍼 창 밖) — 머리에 직접
      }
      if (blkInto.items.indexOf(item) < 0) blkInto.items.push(item);
      (runByItem[key] || (runByItem[key] = {}))[item] = tidInto;
      return;
    }
    if (q) {
      var row = q.card.querySelector(".row");
      row.classList.add("absorbed");
      row.querySelector(".ic").textContent = "✓";
      row.querySelector(".k").textContent = "Absorbed";
      row.title = "absorbed into run " + into + " at a turn boundary";
    }
    return;
  }
  if (d.direction === "out") {
    if (!d.fallback) return; // 도구 줄이 이미 그 발신이다
    var t2 = runByItem[key] && runByItem[key][item];
    if (t2 && runBlocks[t2]) renderFallbackRow(runBlocks[t2], d);
    else {
      var pf = pendingFallback[key] || (pendingFallback[key] = {});
      (pf[item] || (pf[item] = [])).push(d);
    }
    return;
  }
  if (d.direction === "question") {
    // 대화 줄은 `→ ask` 도구 줄이 맡는다. 트레이는 **주소가 사람인 질문에만**
    // 뜨고, 무엇을 띄울지는 roster 의 `open_questions` 가 정한다.
    ovRenderAskTray();
  }
}
// kill(agent_cleared) → **`agent_msg` 에서 나온 것만** 정리한다: 블록 머리의
// 수신 항목 · 폴백 줄 · 대기 줄. 서버도 버퍼에서 `agent_msg` 만 지운다 — 블록
// 자체와 그 안의 턴 카드는 스코프·턴 이벤트에서 왔고 서버 버퍼에 그대로
// 남는다. 블록까지 지우면(v9.23.0 초판) resume 의 `conversation.jsonl` 재생이
// 수신 줄만 다시 보내 붙을 블록이 없어 전부 `⏳ 대기` 로 떨어졌다(사용자 보고
// — 새로고침해야 버퍼의 스코프가 블록을 다시 세웠다). 블록과 `runByItem` 을
// 남겨 두면 재생된 수신 줄이 제자리 머리로 돌아온다.
export function ovOnAgentCleared(key) {
  var ch = cssEsc(key);
  $messages
    .querySelectorAll(
      ':scope > .card-queued[data-ch="' + ch + '"], ' +
      ':scope > .card-run[data-ch="' + ch + '"] .run-head > .run-item, ' +
      ':scope > .card-run[data-ch="' + ch + '"] .run-body > .card-fallback'
    )
    .forEach(function (c) { c.remove(); });
  delete queuedIn[key];
  delete pendingFallback[key];
  ovRenderAskTray();
  ovSyncChannels();
}
// 채널 바(칩) 렌더 — main + roster 의 agent(죽은 상주 포함, 끝난 run 은 `Done N ▾` 로 접힘). 칩마다 **상태 dot**
// (idle/busy/dead) + ✕(kill)/↻(resume). 에이전트 상태를
// 이 "agent 탭"에 직접 표시(v8.33.0 — 구 흐름-탭 dots 통합).
export function ovSyncChannels() {
  var bar = document.getElementById("ov-channels");
  if (!bar) return;
  // 선택 채널이 roster 에서 완전히 사라지면(dead 도 아님) main 복귀. dead 는
  // 유지(⑤: 사망 고지 + ↻ 되살리기 가능).
  if (ovActiveChannel !== "main" &&
      !ovRoster.some(function (t) { return t.key === ovActiveChannel; })) {
    setOvActiveChannel("main");
    syncChannelView(); // 사라진 채널을 보고 있었다면 표시(필터·맨 위 카드·꼬리 띠)도 main 으로
  }
  // 돌아갈 채널이 사라졌으면 버튼도 사라진다(눌러도 갈 곳이 없다).
  if (ovBack && ovBack.ch !== "main" &&
      !ovRoster.some(function (t) { return t.key === ovBack.ch; })) {
    setOvBack(null);
  }
  ovRenderBack(); // 라벨은 roster 가 와야 이름으로 해소된다
  var html = ovChanChip(
    "main", escapeHtml("💬 main"), "", ovActiveChannel === "main"
  );
  var folded = []; // 끝난 run — 칩 대신 `Done N ▾` 로 (보고 있는 채널은 빼고)
  ovRoster.forEach(function (t) {
    if (ovRunFolded(t)) {
      folded.push(t);
      return;
    }
    html += ovChanChip(
      t.key, ovAgentLabelHtml(t.key), t.state, ovActiveChannel === t.key,
      !!t.ends_after_task
    );
  });
  if (folded.length) {
    html += '<button type="button" id="ov-done" aria-haspopup="menu" aria-expanded="' +
      doneMenuOpen() + '" title="Finished runs">Done ' + folded.length + " ▾</button>";
  }
  // ■ all (v10.142.0) — main 이 한가하고(입력창 옆은 Send 그대로) 일하는 에이전트가 있을 때만
  if (!session.workerBusy && ovRoster.some(ovAgentWorking)) {
    html += '<button type="button" id="ov-stop-all" class="btn-danger" title="Stop all agents">■ all</button>';
  }
  bar.innerHTML = html;
  ovRenderDoneMenu(folded); // 열려 있으면 새 명단으로 (방금 끝난 run 이 들어온다)
  // 에이전트가 없으면 바 전체를 숨긴다 — "탭 1개짜리 탭바"는 고를 것이
  // 없다. `#ov-channels:empty` 규칙은 main 칩을 항상 그리므로 **절대
  // 발동하지 않았다**(감사 발견). 🔌 칩의 판례와 같은 판단:
  // "0/0 은 정보가 아니라 소음".
  bar.hidden = ovRoster.length === 0;
  ovApplyChannelInput();
  ovRenderChannelHead();
  ovSyncStopMore();
}
// 입력창 옆 Stop ▾ (v10.142.0) — 정지 버튼은 하나: main 만 바쁘면 Stop(종전 그대로), 에이전트도 일하면 ▾ 가
// 붙어 범위를 고른다. Stop 의 보임·Stopping… 은 input.js 의 updateSendEnabled 가 정하고 여기는 ▾ 만 본다.
function ovSyncStopMore() {
  if (!$chatStopMore) return;
  var show = !$chatStop.hidden && !$chatStop.disabled && ovRoster.some(ovAgentWorking);
  $chatStopMore.hidden = !show;
  if (!show && $stopMenu && $stopMenu.hidden === false) {
    $stopMenu.hidden = true; // ▾ 가 사라지면 열린 메뉴도 닫는다
    $chatStopMore.setAttribute("aria-expanded", "false");
  }
}
// 채널 머리 (v10.142.0, docs/agent-engine/DESIGN.md §2.3) — 고른 에이전트의 이름·종류와 ■ Stop run · ✕ Remove.
// 끝난 에이전트는 머리가 없다(회색 칩 — 둘 다 없음). ■ 는 일하는 동안만 누를 수 있다.
function ovRenderChannelHead() {
  var head = document.getElementById("ov-chhead");
  if (!head) return;
  var t = ovActiveAgent();
  if (!t || t.state === "dead") {
    head.hidden = true;
    head.innerHTML = "";
    return;
  }
  var sub = [t.ends_after_task ? "run" : "spawn"];
  if (t.parent) sub.push("from " + String(t.parent).replace(/^agent:/, ""));
  sub.push(t.state);
  var item = t.ends_after_task ? t.task_id : t.item;
  if (item) sub.push(item);
  var working = ovAgentWorking(t);
  var stopTitle = t.ends_after_task
    ? "Stop this run — the agent ends and whoever was waiting is told"
    : "Stop this agent's current run — it stays and takes the next request";
  head.setAttribute("data-key", t.key);
  head.innerHTML =
    '<span class="chh-ttl">' + ovAgentLabelHtml(t.key) + "</span>" +
    '<span class="chh-sub">' + escapeHtml(sub.join(" · ")) + "</span>" +
    '<span class="chh-sp"></span>' +
    '<button type="button" class="btn-danger chh-btn" data-act="stop"' +
    (working ? "" : " disabled") + ' title="' + stopTitle + '">■ Stop run</button>' +
    '<button type="button" class="btn-ghost chh-btn" data-act="kill" ' +
    'title="Remove this agent — whoever was waiting on it is told">✕ Remove</button>';
  head.hidden = false;
}
(function () {
  var head = document.getElementById("ov-chhead");
  if (!head) return;
  head.addEventListener("click", function (e) {
    var b = e.target.closest ? e.target.closest(".chh-btn") : null;
    if (!b || b.disabled) return;
    b.disabled = true; // 눌렀다 — 명단(SSE)이 다시 그린다
    var act = b.getAttribute("data-act");
    var path = "api/agent/" + encodeURIComponent(head.getAttribute("data-key")) + "/" + act;
    // 상한 없이 — 늦게라도 닿으면 적용. 닿지 못했거나 거절이면 버튼을 풀고 사유 (v10.155.0; 종전엔 잠긴 채 남았다)
    sendOnce(
      { sending: false },
      function (signal) { return fetch(path, { method: "POST", signal: signal }); },
      function (failure) {
        if (!failure) return;
        b.disabled = false;
        attachMessage((act === "kill" ? "Not removed — " : "Not stopped — ") + failure);
      },
      { limit: false }
    );
  });
})();
// `labelHtml` 은 **이스케이프된 HTML** — 흐린 key 병기(`ovAgentLabelHtml`)를
// 담기 위해 평문이 아니다. 호출부가 이스케이프 책임을 진다.
// `run` — run 에이전트(과제 하나): 끝나면 지운 줄이 아니라 회색 칩이고 ↻ 가 없다(끝난 일은
// 되살릴 기억이 아니다). 일하는 동안은 채널에 사람이 직접 쓸 수 있다 — 상주와 같은 길(v10.141.0,
// 설계 §5 시안 C). ■ Stop run · ✕ Remove 는 칩이 아니라 채널 머리에 있다(v10.142.0 — `ovRenderChannelHead`).
// 칩에 남는 조작은 죽은 상주의 ↻ 하나.
function ovChanChip(key, labelHtml, state, active, run) {
  var dead = state === "dead";
  // main 은 상태 dot 없음(항상 LLM). agent 는 상태색 dot.
  var dot = "";
  if (key !== "main") {
    var c = state === "busy" ? "b" : dead ? "x" : "i";
    dot = '<span class="ov-dot ' + c + '"></span>';
  }
  // ❓ 배지는 없다 (v9.8.0) — dot 이 이미 상태를 말하고 있어 한 칩 안에서
  // 같은 사실을 두 기호로 말하게 된다. 질문은 dot 이 아니라 ❓ 트레이가
  // 알린다 (비동기라 질문 중에도 에이전트는 busy 거나 idle 이다).
  var badges = "";
  var ctrl = "";
  if (key !== "main") {
    if (dead && !run) {
      // 끝난 run 은 되살릴 기억이 아니라 끝난 일이다 — ↻ 는 상주에게만
      ctrl = '<button type="button" class="ov-ch-ctl" data-act="resume" title="Revive">↻</button>';
    }
  }
  if (run) badges = '<span class="ov-ch-run">run</span>';
  var cls = run ? (dead ? " run done" : " run") : dead ? " dead" : "";
  return (
    '<span class="ov-ch' + (active ? " on" : "") + cls +
    '" data-key="' + escapeHtml(key) + '" role="tab" aria-selected="' +
    (active ? "true" : "false") + '">' + dot + '<span class="ov-ch-lb">' +
    labelHtml + "</span>" + badges + ctrl + "</span>"
  );
}
function ovActiveAgent() {
  if (ovActiveChannel === "main") return null;
  return ovRoster.filter(function (x) { return x.key === ovActiveChannel; })[0] || null;
}
function ovActiveDead() {
  var t = ovActiveAgent();
  return !!(t && t.state === "dead");
}
// 입력창 placeholder/disabled 를 현재 채널(+busy·dead)에 맞게. 입력창은 chat 전용.
// run 채널도 일하는 동안은 열려 있다 — 사람이 직접 지시를 바꾼다(설계 §5 시안 C).
export function ovApplyChannelInput() {
  if (!$input) return;
  var dead = ovActiveDead();
  $input.disabled = dead;
  if ($send) $send.disabled = dead;
  if (dead && ovActiveAgent().ends_after_task) {
    $input.placeholder = "This run agent finished its task — pick another channel";
  } else if (dead) {
    $input.placeholder = "✕ This agent has exited — pick another channel or revive it with ↻";
  } else if (ovActiveChannel !== "main") {
    $input.placeholder = "To " + ovAgentLabel(ovActiveChannel) + "… (Enter to send)";
  } else {
    $input.placeholder = session.workerBusy
      ? "Worker is processing… your message will be queued (injected next turn)"
      : "Type a message — Enter to send, Shift+Enter for newline";
  }
}
