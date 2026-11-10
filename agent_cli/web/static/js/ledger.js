// ledger.js — 입력창 위 📋 띠: 모델이 갚아야 하는 열린 항목(장부)을 사람에게 보인다.
// 순환 밖 잎이다 — dom·sse 만 import 한다. 원문 카드로의 점프는 순환 안(channels)의
// 일이라 직접 부르지 않고 `agentcli:jump` {hidx} 를 document 로 보낸다(기능 모듈 사이
// 계약 `agentcli:*` 와 같은 방식).
// (v10.119.0, docs/ledger/DESIGN.md §15 · docs/web-modules/DESIGN.md §8)
//
// 접기(v10.122.0, §15.7): 행은 OI_FOLD_ROWS(3)까지만 보이고 넘치면 끝에 "+N more ▾" 토글
// 행 하나. 열린 항목이 먼저 자리를 차지하고 ✓ closed 영수증은 남는 자리에만 선다 — 접혀서
// 안 보이는 영수증은 N 에 세지 않는다(곧 사라질 행이라 "더 있다" 는 약속이 거짓이 된다).
// 펼친 목록은 CSS 상한(.oi-list) 안에서 스크롤하고 머리(제목·budget)와 토글은 늘 보인다.
//
// 띠는 셋이고 방향으로 나뉜다: ❓ 트레이(모델→사람, 답해야 한다) → 📋 장부(사람→모델,
// 들어가서 갚는 중 — 보고 점프만 한다) → ⏳ 대기열(아직 안 들어감, 취소할 수 있다).
// 같은 요청이 대기열에서 장부로 같은 id(`u-N`)로 옮겨 온다.

import { el } from "./dom.js";
import { es } from "./sse.js";

const $openItems = document.getElementById("open-items");
// 닫힌 행의 영수증 시간 — 대기열의 ✓ 영수증과 같은 리듬
const OI_CLOSED_MS = 1500;
let oiItems = []; // 지금 열린 항목 (스냅샷 순서 = 꼬리의 닫는 순서)
let oiBudget = null; // {left, total}
const oiLeaving = {}; // id → 직전 스냅샷의 항목 (✓ closed 로 잠깐 남는다)
const oiTimers = {}; // id → 영수증을 거둘 타이머
// 접힌 띠가 보이는 행 수(영수증 포함)
const OI_FOLD_ROWS = 3;
// 펼침은 페이지 메모리에만 — 스냅샷이 다시 와도 사람이 고른 것을 지킨다. 열린 항목이
// 한도 이하로 줄어 토글이 사라지면 접힘으로 돌아간다(다음에 넘칠 때 띠가 갑자기 길게
// 서지 않도록).
let oiExpanded = false;

// 닫는 법 힌트 — 꼬리가 모델에게 말하는 것과 같은 도구 이름
function oiCloserHint(it) {
  if (it.closer === "complete") {
    return it.source === "mail" ? "→ complete, answers" : "→ complete";
  }
  if (!it.closer) return "";
  return "→ " + it.closer + ' "' + it.id + '"';
}

function oiRow(it, closed) {
  const row = el("div", ["oi-item"]);
  row.dataset.id = it.id;
  row.appendChild(el("span", ["oi-id"], it.id));
  if (it.who) row.appendChild(el("span", ["oi-who"], it.who));
  const head = el("span", ["oi-head"], it.head || "");
  head.title = it.head || "";
  row.appendChild(head);
  if (closed) {
    row.classList.add("oi-closed");
    row.appendChild(el("span", ["oi-state"], "✓ closed"));
    return row;
  }
  const hint = oiCloserHint(it);
  if (hint) row.appendChild(el("span", ["oi-closer"], hint));
  if (it.turns != null) {
    row.appendChild(
      el("span", ["oi-turns"], it.turns + (it.turns === 1 ? " turn" : " turns"))
    );
  }
  if (it.hidx != null) {
    // 원문 카드로 — 엿보기라 돌아가기는 한 단계(channels 의 점프와 같다)
    row.classList.add("oi-jump");
    row.tabIndex = 0;
    row.setAttribute("role", "button");
    row.title = "Jump to the original message";
    const jump = function () {
      document.dispatchEvent(
        new CustomEvent("agentcli:jump", { detail: { hidx: it.hidx } })
      );
    };
    row.addEventListener("click", jump);
    row.addEventListener("keydown", function (e) {
      if (e.key === "Enter" || e.key === " ") {
        e.preventDefault();
        jump();
      }
    });
  }
  return row;
}

// 접기 토글 — 진짜 <button> 이라 Enter·Space 가 기본으로 누른다
function oiToggle(hidden) {
  const b = el("button", ["oi-more"], oiExpanded ? "Show less ▴" : "+" + hidden + " more ▾");
  b.type = "button";
  b.setAttribute("aria-expanded", oiExpanded ? "true" : "false");
  b.setAttribute("aria-controls", "oi-list");
  b.title = oiExpanded ? "Show only the first " + OI_FOLD_ROWS : "Show all open items";
  b.addEventListener("click", function () {
    oiExpanded = !oiExpanded;
    oiRender();
  });
  return b;
}

function oiRender() {
  if (!$openItems) return;
  const leaving = Object.keys(oiLeaving);
  // 다시 그려도 키보드 초점·목록 스크롤을 잃지 않게 — 스냅샷은 턴마다 온다
  const act = document.activeElement;
  let focusKey = null;
  if (act && $openItems.contains(act)) {
    if (act.classList.contains("oi-more")) focusKey = "more";
    else if (act.dataset && act.dataset.id) focusKey = act.dataset.id;
  }
  const oldList = $openItems.querySelector(".oi-list");
  const scrollTop = oldList ? oldList.scrollTop : 0;

  $openItems.innerHTML = "";
  $openItems.hidden = oiItems.length + leaving.length === 0;
  if (oiItems.length <= OI_FOLD_ROWS) oiExpanded = false;
  if ($openItems.hidden) return;
  const hd = el("div", ["oi-hd"]);
  hd.appendChild(el("span", ["oi-title"], "📋 open items · the model owes these"));
  if (oiBudget && oiBudget.total != null) {
    const b = el("span", ["oi-budget"], "budget " + oiBudget.left + "/" + oiBudget.total);
    b.title = "Reminders left before the harness closes what is still open";
    if (oiBudget.left === 0) b.classList.add("oi-budget-out");
    hd.appendChild(b);
  }
  $openItems.appendChild(hd);

  const folds = oiItems.length > OI_FOLD_ROWS;
  const list = el("div", ["oi-list"]);
  list.id = "oi-list";
  if (folds && oiExpanded) list.classList.add("oi-list-open");
  // 펼침이면 전부. 아니면 열린 항목 앞 3개, 영수증은 그 한도에 남는 자리에만.
  const open = folds && oiExpanded;
  const shown = open ? oiItems : oiItems.slice(0, OI_FOLD_ROWS);
  const room = open ? leaving.length : Math.max(0, OI_FOLD_ROWS - shown.length);
  shown.forEach(function (it) {
    list.appendChild(oiRow(it, false));
  });
  leaving.slice(0, room).forEach(function (id) {
    list.appendChild(oiRow(oiLeaving[id], true));
  });
  $openItems.appendChild(list);
  list.scrollTop = scrollTop;
  if (folds) $openItems.appendChild(oiToggle(oiItems.length - OI_FOLD_ROWS));

  if (focusKey === "more") {
    const m = $openItems.querySelector(".oi-more");
    if (m) m.focus();
  } else if (focusKey) {
    const r = list.querySelector('.oi-item[data-id="' + CSS.escape(focusKey) + '"]');
    if (r && r.tabIndex >= 0) r.focus();
  }
}

es.addEventListener("open_items", function (e) {
  const d = JSON.parse(e.data);
  const next = Array.isArray(d.items) ? d.items : [];
  const nextIds = {};
  next.forEach(function (it) {
    nextIds[it.id] = true;
    if (oiLeaving[it.id]) {
      // 다시 열렸다 — 영수증과 그 타이머를 거둔다
      clearTimeout(oiTimers[it.id]);
      delete oiTimers[it.id];
      delete oiLeaving[it.id];
    }
  });
  // 직전 스냅샷에 있다가 사라진 id = 닫혔다 → ✓ closed 로 잠깐 남는다
  oiItems.forEach(function (it) {
    if (nextIds[it.id] || oiLeaving[it.id]) return;
    oiLeaving[it.id] = it;
    oiTimers[it.id] = setTimeout(function () {
      delete oiTimers[it.id];
      delete oiLeaving[it.id];
      oiRender();
    }, OI_CLOSED_MS);
  });
  oiItems = next;
  oiBudget = d.budget || null;
  oiRender();
});
