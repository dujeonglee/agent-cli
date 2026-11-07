// ledger.js — 입력창 위 📋 띠: 모델이 갚아야 하는 열린 항목(장부)을 사람에게 보인다.
// 순환 밖 잎이다 — dom·sse 만 import 한다. 원문 카드로의 점프는 순환 안(channels)의
// 일이라 직접 부르지 않고 `agentcli:jump` {hidx} 를 document 로 보낸다(기능 모듈 사이
// 계약 `agentcli:*` 와 같은 방식).
// (v10.119.0, docs/ledger/DESIGN.md §15 · docs/web-modules/DESIGN.md §8)
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

function oiRender() {
  if (!$openItems) return;
  const leaving = Object.keys(oiLeaving);
  $openItems.innerHTML = "";
  $openItems.hidden = oiItems.length + leaving.length === 0;
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
  oiItems.forEach(function (it) {
    $openItems.appendChild(oiRow(it, false));
  });
  leaving.forEach(function (id) {
    $openItems.appendChild(oiRow(oiLeaving[id], true));
  });
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
