// runs.js — 런 블록: 에이전트 런의 접힌 블록, 채널 칩 동기화·채널 입력 라우팅.
// (v10.117.0, docs/web-modules/DESIGN.md §8 — 옛 main.js 의 절 [29])

import { $input, $messages, $send, el, elHtml } from "./dom.js";
import { session } from "./state.js";
import { escapeAndFormat, escapeHtml } from "./markdown.js";
import { scheduleScroll } from "./scroll.js";
import {
  applyChannelFilter,
  cssEsc,
  ovActiveChannel,
  ovAgentLabel,
  ovAgentLabelHtml,
  ovBack,
  ovRenderAskTray,
  ovRenderBack,
  ovRoster,
  setOvActiveChannel,
  setOvBack,
} from "./channels.js";
import { finishCard, scopeChannel, stampCard } from "./timeline.js";
import { miniThumbs, thumbStrip } from "./thumbs.js";
import { makeRow, peerLabel } from "./cards.js";

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
    it.addEventListener("click", function () {
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
}

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

/** 꼬리 — 최종답이 있으면 **그 카드가 꼬리**(같은 말을 두 번 하지 않는다),
 * 없으면(중단·오류·complete 없이 끝남) 상태 한 줄. */
export function closeRunBlock(d) {
  var blk = runBlocks[d.task_id];
  if (!blk || blk.closed) return;
  blk.closed = true;
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
// 채널 바(칩) 렌더 — main + roster 전 agent(dead 포함). 칩마다 **상태 dot**
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
    applyChannelFilter(); // 사라진 채널을 보고 있었다면 표시도 되돌린다
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
  ovRoster.forEach(function (t) {
    html += ovChanChip(
      t.key, ovAgentLabelHtml(t.key), t.state, ovActiveChannel === t.key
    );
  });
  bar.innerHTML = html;
  // 에이전트가 없으면 바 전체를 숨긴다 — "탭 1개짜리 탭바"는 고를 것이
  // 없다. `#ov-channels:empty` 규칙은 main 칩을 항상 그리므로 **절대
  // 발동하지 않았다**(감사 발견). 🔌 칩의 판례와 같은 판단:
  // "0/0 은 정보가 아니라 소음".
  bar.hidden = ovRoster.length === 0;
  ovApplyChannelInput();
}
// `labelHtml` 은 **이스케이프된 HTML** — 흐린 key 병기(`ovAgentLabelHtml`)를
// 담기 위해 평문이 아니다. 호출부가 이스케이프 책임을 진다.
function ovChanChip(key, labelHtml, state, active) {
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
    ctrl = dead
      ? '<button type="button" class="ov-ch-ctl" data-act="resume" title="Revive">↻</button>'
      : '<button type="button" class="ov-ch-ctl" data-act="kill" title="Kill">✕</button>';
  }
  return (
    '<span class="ov-ch' + (active ? " on" : "") + (dead ? " dead" : "") +
    '" data-key="' + escapeHtml(key) + '" role="tab" aria-selected="' +
    (active ? "true" : "false") + '">' + dot + '<span class="ov-ch-lb">' +
    labelHtml + "</span>" + badges + ctrl + "</span>"
  );
}
function ovActiveDead() {
  if (ovActiveChannel === "main") return false;
  var t = ovRoster.filter(function (x) { return x.key === ovActiveChannel; })[0];
  return !!(t && t.state === "dead");
}
// 입력창 placeholder/disabled 를 현재 채널(+busy·dead)에 맞게. 입력창은 chat 전용.
export function ovApplyChannelInput() {
  if (!$input) return;
  var dead = ovActiveDead();
  $input.disabled = dead;
  if ($send) $send.disabled = dead;
  if (dead) {
    $input.placeholder = "✕ This agent has exited — pick another channel or revive it with ↻";
  } else if (ovActiveChannel !== "main") {
    $input.placeholder = "To " + ovAgentLabel(ovActiveChannel) + "… (Enter to send)";
  } else {
    $input.placeholder = session.workerBusy
      ? "Worker is processing… your message will be queued (injected next turn)"
      : "Type a message — Enter to send, Shift+Enter for newline";
  }
}
