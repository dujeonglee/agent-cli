// jobs.js — 입력창 위 🧵 칩: 돌고 있는 백그라운드 작업과 사람이 멈추는 ■.
// 순환 밖 잎이다 — dom·state·sse 만 import 한다.
// (v10.121.0, docs/complete-while-running/REVIEW-fable.md 후속 리뷰)
//
// 칩은 스티키 `jobs` {count, list: [{id, pid, owner, command, started_at, stopping}]} 를
// 그린다. 경과는 started_at(epoch 초)에서 브라우저가 1초마다 계산한다 — 서버는 시각만
// 보낸다. ■ 는 칩 안에서 한 번 더 묻고(브라우저가 confirm() 대화상자를 막는다) Stop 을
// 누르면 POST api/jobs/stop {id, conn_id}. 멈춘 사람의 닉네임이 소유자에게 가는 작업
// 메일에 실린다("🧵 job #N stopped by user <name> …"). 멈춤은 몇 초 걸린다(TERM → 3초 →
// KILL) — 그동안 칩은 "stopping…" 이고, 끝나면 스티키가 칩을 거둔다.

import { el } from "./dom.js";
import { session } from "./state.js";
import { es } from "./sse.js";

const $jobs = document.getElementById("job-chips");
let jbList = []; // 지금 돌고 있는 작업 (스티키 순서 = 번호 순)
const jbAsking = {}; // id → true: 칩이 "Stop job #N?" 을 묻는 중
const jbErrors = {}; // id → 마지막 실패 문구 (네트워크·409)
let jbTimer = null;

function jbElapsed(startedAt) {
  // 작업 꼬리·통지(jobs.elapsed_text)와 같은 모양: 5s · 2m31s · 1h05m
  let s = Math.max(0, Math.floor(Date.now() / 1000 - Number(startedAt || 0)));
  const h = Math.floor(s / 3600);
  s -= h * 3600;
  const m = Math.floor(s / 60);
  const sec = s - m * 60;
  if (h) return h + "h" + String(m).padStart(2, "0") + "m";
  return m ? m + "m" + String(sec).padStart(2, "0") + "s" : sec + "s";
}

function jbStop(id) {
  delete jbAsking[id];
  delete jbErrors[id];
  const job = jbList.find(function (j) { return j.id === id; });
  if (job) job.stopping = true; // 스티키가 오기 전에도 바로 "stopping…"
  render();
  fetch("api/jobs/stop", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ id: id, conn_id: session.connId }),
  })
    .then(function (r) {
      if (r.ok || r.status === 404) return; // 404 = 이미 끝났다 — 스티키가 칩을 거둔다
      if (job) job.stopping = false; // 멈추지 못했다 — ■ 를 다시 보인다
      return r.json().then(
        function (d) { jbErrors[id] = (d && d.detail) || "Could not stop the job"; },
        function () { jbErrors[id] = "Could not stop the job"; }
      );
    })
    .catch(function () {
      if (job) job.stopping = false;
      jbErrors[id] = "Could not reach the server";
    })
    .then(render);
}

function jbChip(j) {
  const chip = el("div", ["job-chip"]);
  chip.dataset.id = String(j.id);
  chip.appendChild(el("span", ["job-id"], "🧵 #" + j.id));
  const cmd = el("code", ["job-cmd"], j.command || "");
  cmd.title = (j.command || "") + " · pid " + j.pid;
  chip.appendChild(cmd);
  if (j.owner && j.owner !== "main") chip.appendChild(el("span", ["job-owner"], j.owner));
  chip.appendChild(el("span", ["job-elapsed"], jbElapsed(j.started_at)));
  if (j.stopping) {
    chip.classList.add("job-stopping");
    chip.appendChild(el("span", ["job-state"], "stopping…"));
    return chip;
  }
  if (jbErrors[j.id]) chip.appendChild(el("span", ["job-error"], jbErrors[j.id]));
  if (jbAsking[j.id]) {
    chip.classList.add("job-asking");
    chip.appendChild(el("span", ["job-ask"], "Stop job #" + j.id + "?"));
    const yes = el("button", ["btn-danger", "job-confirm"], "Stop");
    yes.type = "button";
    yes.title = "Stop this job — its owner is told who stopped it";
    yes.addEventListener("click", function () { jbStop(j.id); });
    const no = el("button", ["btn-ghost", "job-cancel"], "Cancel");
    no.type = "button";
    no.addEventListener("click", function () {
      delete jbAsking[j.id];
      render();
    });
    chip.appendChild(yes);
    chip.appendChild(no);
    return chip;
  }
  const stop = el("button", ["btn-danger", "job-stop"], "■");
  stop.type = "button";
  stop.title = "Stop job #" + j.id;
  stop.setAttribute("aria-label", "Stop job #" + j.id);
  stop.addEventListener("click", function () {
    jbAsking[j.id] = true;
    render();
  });
  chip.appendChild(stop);
  return chip;
}

function render() {
  if (!$jobs) return;
  $jobs.innerHTML = "";
  $jobs.hidden = jbList.length === 0;
  for (const j of jbList) $jobs.appendChild(jbChip(j));
}

function tickElapsed() {
  // 칩 전체를 다시 그리지 않는다 — 묻는 중인 버튼의 포커스가 날아간다
  if (!$jobs) return;
  for (const j of jbList) {
    const chip = $jobs.querySelector('.job-chip[data-id="' + j.id + '"] .job-elapsed');
    if (chip) chip.textContent = jbElapsed(j.started_at);
  }
}

es.addEventListener("jobs", function (e) {
  const d = JSON.parse(e.data) || {};
  jbList = (d.list || []).map(function (j) { return Object.assign({}, j); });
  const live = {};
  for (const j of jbList) live[j.id] = true;
  for (const id of Object.keys(jbAsking)) if (!live[id]) delete jbAsking[id];
  for (const id of Object.keys(jbErrors)) if (!live[id]) delete jbErrors[id];
  render();
  if (jbList.length && jbTimer === null) {
    jbTimer = setInterval(tickElapsed, 1000);
  } else if (!jbList.length && jbTimer !== null) {
    clearInterval(jbTimer);
    jbTimer = null;
  }
});
