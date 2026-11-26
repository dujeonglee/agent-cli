// stuck.js — 입력창 위 🔁 띠: 모델이 새 결과 없이 같은 자리를 돌고 있다(StaleGuard).
// 순환 밖 잎이다 — dom·state·sse 만 import 한다.
// (v10.120.0, docs/stale-guard/DESIGN.md §5)
//
// 띠는 스티키 `stuck` {turns, since_turn, fires, asking} 를 그린다. turns 0 이면 숨는다
// (새 결과가 나왔거나 런이 끝났다). Stop 은 언제나 /api/stop(Stop 버튼과 같은 길) — 런이 멈춰 확인을
// 기다리는 중(asking)이어도 그 정지가 기다림을 깨운다(6d).

import { el } from "./dom.js";
import { es } from "./sse.js";
import { attachMessage } from "./attach.js";
import { sendOnce } from "./send.js";

const $stuck = document.getElementById("stuck-band");
let stStopping = false;

function stStop() {
  if (stStopping) return;
  stStopping = true;
  render(lastStuck);
  // 상한 없이 — 늦게라도 닿으면 멈춘다. 닿지 못했거나 거절이면 "Stopping…" 을 풀고 사유 (v10.155.0)
  sendOnce(
    { sending: false },
    function (signal) {
      return fetch("api/stop", { method: "POST", signal: signal });
    },
    function (failure) {
      if (!failure) return;
      stStopping = false;
      render(lastStuck);
      attachMessage("Not stopped — " + failure);
    },
    { limit: false }
  );
}

let lastStuck = null;

function render(d) {
  if (!$stuck) return;
  $stuck.innerHTML = "";
  const n = d && d.turns ? d.turns : 0;
  $stuck.hidden = n === 0;
  if (!n) return;
  const msg =
    "🔁 No new results for " + n + (n === 1 ? " turn" : " turns") +
    " — the model may be going in circles.";
  $stuck.appendChild(el("span", ["stuck-msg"], msg));
  if (d.asking) {
    $stuck.appendChild(el("span", ["stuck-asking"], "The run is paused — answer above."));
  }
  const stop = el("button", ["btn-danger", "stuck-stop"], stStopping ? "Stopping…" : "Stop");
  stop.type = "button";
  stop.disabled = stStopping;
  stop.title = "Stop the current run";
  stop.addEventListener("click", stStop);
  $stuck.appendChild(stop);
}

es.addEventListener("stuck", function (e) {
  const d = JSON.parse(e.data);
  if (!d || !d.turns) stStopping = false; // 띠가 내려갔다 — 다음 띠는 새로 누를 수 있다
  lastStuck = d;
  render(d);
});
