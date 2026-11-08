// stuck.js — 입력창 위 🔁 띠: 모델이 새 결과 없이 같은 자리를 돌고 있다(StaleGuard).
// 순환 밖 잎이다 — dom·state·sse 만 import 한다.
// (v10.120.0, docs/stale-guard/DESIGN.md §5)
//
// 띠는 스티키 `stuck` {turns, since_turn, fires, asking} 를 그린다. turns 0 이면 숨는다
// (새 결과가 나왔거나 런이 끝났다). Stop 은 보통 /api/stop(Stop 버튼과 같은 길)이고,
// 런이 멈춰 확인을 기다리는 중(asking)이면 그 확인에 "s" 로 답한다 — 기다리는 워커는
// /api/stop 의 턴 경계에 닿지 못한다.

import { el } from "./dom.js";
import { session } from "./state.js";
import { es } from "./sse.js";

const $stuck = document.getElementById("stuck-band");
let stStopping = false;

function stStop(asking) {
  if (stStopping) return;
  stStopping = true;
  render(lastStuck);
  const req = asking
    ? fetch("api/input", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ kind: "confirm", key: "s", comment: "", conn_id: session.connId }),
      })
    : fetch("api/stop", { method: "POST" });
  req.catch(function () {
    /* network blip — 런은 사람이 다시 누르거나 스스로 끝난다 */
  });
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
  stop.title = d.asking ? "Stop the run (answers the question above)" : "Stop the current run";
  stop.addEventListener("click", function () {
    stStop(!!d.asking);
  });
  $stuck.appendChild(stop);
}

es.addEventListener("stuck", function (e) {
  const d = JSON.parse(e.data);
  if (!d || !d.turns) stStopping = false; // 띠가 내려갔다 — 다음 띠는 새로 누를 수 있다
  lastStuck = d;
  render(d);
});
