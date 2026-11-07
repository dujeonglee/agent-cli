// roster.js — identity·뷰어 로스터 + 별명 + 대기열(전달 상태).
// (v10.117.0, docs/web-modules/DESIGN.md §8 — 옛 main.js 의 절 [33]·[34]·[35])

import { el } from "./dom.js";
import { session } from "./state.js";
import { revealAtBottom } from "./scroll.js";
import { es } from "./sse.js";
import { miniThumbs } from "./thumbs.js";

// ── Identity + viewer roster ───────
// Every connection is equal (all may send input / queue). conn_id is needed
// only to mark "(you)" in the roster and to own queued messages.
es.addEventListener("snapshot_end", function () {
  revealAtBottom();
});
es.addEventListener("identity", function (e) {
  session.connId = JSON.parse(e.data).conn_id;
  // 상주 에이전트 대화 창 IIFE(별도 클로저)가 닉네임 attribution 에 쓰도록 노출.
  window.AGENTCLI_CONN_ID = session.connId;
});

const $viewers = document.getElementById("viewers");
const $renameBtn = document.getElementById("rename-btn");
es.addEventListener("viewers", function (e) {
  if (!$viewers) return;
  const d = JSON.parse(e.data);
  const labels = (d.viewers || []).map(function (v) {
    return v.id === session.connId ? v.name + " (you)" : v.name;
  });
  $viewers.textContent =
    "👁 " + d.count + (labels.length ? " · " + labels.join(", ") : "");
  $viewers.title = labels.join(", ");
  // ✎ rename: visible once we know who we are and we're in the roster.
  const me = (d.viewers || []).find(function (v) {
    return v.id === session.connId;
  });
  // 2인 이상일 때만 말풍선에 보낸 사람 이름을 보인다(위 renderUserMessage).
  document.body.classList.toggle("multi-viewer", (d.count || 0) > 1);
  if (me) myNickname = me.name; // latest name for rename prefill
  if ($renameBtn) $renameBtn.hidden = !me;
  maybeNamePrompt(d.viewers || []);
});

// ── Nickname (input on first connect; fun default pre-filled) ───────
// Once per page load: if a name was saved before, re-apply it silently;
// otherwise show a bar pre-filled with the assigned fun default so the
// user can edit/confirm (or ✕ to keep the default).
const NICK_KEY = "agentcli_nickname";
const $nameBar = document.getElementById("name-bar");
const $nbInput = document.getElementById("nb-input");
const $nbSet = document.getElementById("nb-set");
const $nbSkip = document.getElementById("nb-skip");
let namePrompted = false;
let myNickname = ""; // latest roster name, for prefill on rename

function postNickname(name) {
  fetch("api/nickname", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ conn_id: session.connId, name: name }),
  }).catch(function () {});
}

// Show the name-bar pre-filled with `current`, focused. Shared by the
// first-connect prompt and the ✎ rename entry point.
function openNameBar(current) {
  if (!$nameBar) return;
  $nbInput.value = current || "";
  $nameBar.hidden = false;
  $nbInput.focus();
  $nbInput.select();
}

function maybeNamePrompt(viewers) {
  if (namePrompted || !session.connId || !$nameBar) return;
  namePrompted = true;
  const saved = (localStorage.getItem(NICK_KEY) || "").trim();
  if (saved) {
    postNickname(saved); // remembered from a previous session
    return;
  }
  const me = viewers.find(function (v) {
    return v.id === session.connId;
  });
  openNameBar(me ? me.name : ""); // pre-fill the fun default
}

function applyNickname() {
  const name = $nbInput.value.trim();
  if (name) {
    postNickname(name);
    localStorage.setItem(NICK_KEY, name);
  }
  $nameBar.hidden = true;
}
if ($nbSet) $nbSet.addEventListener("click", applyNickname);
if ($nbSkip) {
  $nbSkip.addEventListener("click", function () {
    $nameBar.hidden = true;
  });
}
if ($nbInput) {
  $nbInput.addEventListener("keydown", function (e) {
    if (e.key === "Enter") {
      e.preventDefault();
      applyNickname();
    }
  });
}
if ($renameBtn) {
  $renameBtn.addEventListener("click", function () {
    if (!session.connId) return; // identity not yet known
    openNameBar(myNickname); // prefill with current nickname
  });
}

// ── Pending message queue (live) + 전달 상태 ───────
// Messages queued while the worker is busy; injected one-per-turn-boundary.
// Each viewer can cancel their OWN still-pending items.
//
// 조용히 사라지는 대신 "전달됨" 을 보여준다(단계 3): 큐를 떠난 항목은
// ⏳ → ✓ 주입됨(내 것=✓ 내 요청 반영됨) 또는 ✕ 취소됨 영수증으로 잠깐 남았다
// 사라진다. 주입/취소 판정은 기존 이벤트로 추론(로직 불변): 큐에서 빠진 항목의
// 텍스트가 곧 user_message 로 재방출되면 주입(그리고 개요의 다음 응답 블록
// 요청으로 자동 승격), ~1.4s 내 매칭이 없으면 취소. 내가 ✕ 누른 건 즉시 취소.
const $queueList = document.getElementById("queue-list");
let qPrevById = {}; // 직전 pending: id → item
const qLeaving = {}; // id → {item, state:'wait'|'injected'|'cancelled', _timer}
const qCancelledByMe = {}; // id → true (내가 ✕)
let qPending = [];
// main 앞 미배달 메일도 같은 대기열에 선다 (v10.51.0, `mail: true`) — 사용자
// 요청이 바쁠 때 서는 그 자리, 같은 ⏳ → ✓ 주입됨. 다른 점 둘: 아무도 취소할
// 수 없고, **유예 뒤에야** 그린다 — main 이 놀면 1초 안에 깨우기 런이 흡수해
// 종전처럼 떴다 사라지는 줄(사용자 지적, `InputQueue.snapshot`)이 되기 때문.
// 큐에서 빠진 메일은 곧 주입된 것이다(메일박스 drain = 관찰 주입).
const Q_MAIL_GRACE_MS = 1500;
const qMailSince = {}; // id → 처음 본 시각 (유예 판정)
function qVisible(it) {
  return !it.mail || Date.now() - qMailSince[it.id] >= Q_MAIL_GRACE_MS;
}

function qScheduleRemove(id) {
  setTimeout(function () {
    delete qLeaving[id];
    delete qCancelledByMe[id];
    qRender();
  }, 1800);
}
function qRender() {
  if (!$queueList) return;
  const leaving = Object.keys(qLeaving);
  $queueList.innerHTML = "";
  const shown = qPending.filter(qVisible);
  $queueList.hidden = shown.length + leaving.length === 0;
  shown.forEach(function (it) {
    const row = el("div", ["queue-item"]);
    // id 칩 (v10.119.0) — 디큐되면 같은 id 로 📋 띠에 오른다. 두 띠를 잇는다.
    if (it.id) row.appendChild(el("span", ["oi-id"], it.id));
    const txt = el("span", ["queue-text"]);
    txt.textContent = "⏳ [" + it.nickname + "] " + it.text;
    row.appendChild(txt);
    if (Array.isArray(it.images) && it.images.length) row.appendChild(miniThumbs(it.images));
    if (!it.mail && it.conn_id === session.connId) {
      const x = el("button", ["queue-cancel"]);
      x.type = "button";
      x.textContent = "✕";
      x.title = "Cancel this queued message";
      x.addEventListener("click", function () {
        qCancelledByMe[it.id] = true; // 로컬 확정 — 다음 queue 이벤트서 취소 영수증
        fetch("api/queue/cancel", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ conn_id: session.connId, id: it.id }),
        });
      });
      row.appendChild(x);
    }
    $queueList.appendChild(row);
  });
  leaving.forEach(function (id) {
    const lv = qLeaving[id];
    const cancelled = lv.state === "cancelled";
    const mine = lv.item.conn_id === session.connId;
    const row = el("div", ["queue-item", cancelled ? "q-cancelled" : "q-injected"]);
    if (lv.item.id) row.appendChild(el("span", ["oi-id"], lv.item.id));
    const txt = el("span", ["queue-text"]);
    txt.textContent = (cancelled ? "✕" : "✓") + " [" + lv.item.nickname + "] " + lv.item.text;
    row.appendChild(txt);
    if (Array.isArray(lv.item.images) && lv.item.images.length) row.appendChild(miniThumbs(lv.item.images));
    const st = el("span", ["queue-state"]);
    st.textContent = cancelled
      ? "Cancelled"
      : mine && !lv.item.mail
        ? "✓ My request applied"
        : "Injected · applies to the next answer";
    row.appendChild(st);
    $queueList.appendChild(row);
  });
}
export function qOnUserMsg(d) {
  // 큐를 떠나 대기 중인 항목이 user_message 로 재방출 → 주입 확정.
  const c = d.content || "";
  Object.keys(qLeaving).forEach(function (id) {
    const lv = qLeaving[id];
    if (
      lv.state === "wait" &&
      (c === lv.item.text || c.indexOf(lv.item.text) >= 0 || lv.item.text.indexOf(c) >= 0)
    ) {
      if (lv._timer) clearTimeout(lv._timer);
      lv.state = "injected";
      qRender();
      qScheduleRemove(id);
    }
  });
}
es.addEventListener("queue", function (e) {
  if (!$queueList) return;
  const pending = JSON.parse(e.data).pending || [];
  const newIds = {};
  pending.forEach(function (it) {
    newIds[it.id] = true;
  });
  // pending 에서 빠진 항목 → 영수증(즉시 취소 or 판정 대기)
  Object.keys(qPrevById).forEach(function (id) {
    if (newIds[id] || qLeaving[id]) return;
    const item = qPrevById[id];
    if (item.mail) {
      // 메일: 빠짐 = 주입. 유예 안에 빠졌으면(main 이 놀았다) 영수증도 없다 —
      // 타임라인의 관찰 줄이 전부다.
      const wasShown = qVisible(item);
      delete qMailSince[id];
      if (!wasShown) return;
      qLeaving[id] = { item: item, state: "injected" };
      qScheduleRemove(id);
      return;
    }
    if (qCancelledByMe[id]) {
      qLeaving[id] = { item: item, state: "cancelled" };
      qScheduleRemove(id);
    } else {
      qLeaving[id] = { item: item, state: "wait" };
      qLeaving[id]._timer = setTimeout(function () {
        if (qLeaving[id] && qLeaving[id].state === "wait") {
          qLeaving[id].state = "cancelled";
          qRender();
          qScheduleRemove(id);
        }
      }, 1400);
    }
  });
  qPending = pending;
  qPrevById = {};
  let graceTimer = false;
  pending.forEach(function (it) {
    qPrevById[it.id] = it;
    if (it.mail && qMailSince[it.id] === undefined) {
      qMailSince[it.id] = Date.now();
      graceTimer = true;
    }
  });
  // 유예가 끝나도 아직 서 있으면 그때 그린다(새 queue 이벤트가 없어도).
  if (graceTimer) setTimeout(qRender, Q_MAIL_GRACE_MS + 20);
  qRender();
});
