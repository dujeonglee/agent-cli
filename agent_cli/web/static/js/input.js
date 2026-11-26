// input.js — 입력: Send/Stop 가시성, 프롬프트 메타·위험 강조, POST, 입력 바인딩, worker_state 배선.
// (v10.117.0, docs/web-modules/DESIGN.md §8 — 옛 main.js 의 절 [19]·[20]·[21]·[22]·[32])

import { $chatStop, $chatStopMore, $input, $send, $stopMenu, el } from "./dom.js";
import { session } from "./state.js";
import { escapeHtml } from "./markdown.js";
import { es } from "./sse.js";
import {
  attachBusy,
  attachMessage,
  attachReadyPaths,
  attachRefreshVision,
  attachRemove,
} from "./attach.js";
import { ovActiveChannel, postStop } from "./channels.js";
import { sendOnce } from "./send.js";
import { ovSyncChannels } from "./runs.js";

// True when the Send button is acting as a Stop button: chat mode +
// worker busy. In that state a click POSTs /api/stop instead of
// sending, halting the in-flight turn at the next turn boundary
// (server.trigger_stop → run_loop stop_event). Enter is NOT wired to
// stop — the button is the deliberate affordance, so a stray Enter
// can't abort a run by accident.
// Whether a chat run is in flight (Stop button shown). Send is ALWAYS
// available in chat mode now — typing while busy QUEUES the message
// (injected at the next turn boundary), so Stop is a separate button.
function isBusyChat() {
  // 확인을 기다리는 동안에도 Stop 이 보인다(6d) — 그 정지가 기다리는 호출을 깨운다.
  return session.workerBusy;
}

function updateSendEnabled() {
  // Send is always enabled (chat idle → starts a run; chat busy → queues).
  // Stop is a SEPARATE button shown only while a chat run is in flight.
  const busy = isBusyChat();
  const stopping = busy && session.stopRequested;
  $send.disabled = false;
  $send.textContent = "Send";
  if ($chatStop) {
    $chatStop.hidden = !busy;
    $chatStop.disabled = stopping;
    $chatStop.textContent = stopping ? "Stopping…" : "Stop";
  }
  // placeholder 는 채널(+busy) 인지형 — ovApplyChannelInput 단일 소유. 채널 줄 끝 ■ all·Stop ▾ 도
  // main 의 바쁨을 본다(v10.142.0) — 채널 동기화가 셋을 함께 다시 그린다.
  ovSyncChannels();
}

// ── Input mode switching ───────────────────
// Provenance block (who/why/what) shown with a confirm so the user can
// tell which delegate agent is asking and about what.
// Returns null when there's nothing to show (e.g. main-agent confirm).
export function buildPromptMetaEl(data) {
  if (!data) return null;
  const agent = typeof data.agent === "string" ? data.agent : "";
  const reasoning = typeof data.reasoning === "string" ? data.reasoning : "";
  const action = typeof data.action === "string" ? data.action : "";
  // **agent 없으면 안 그린다** — CLI 가 이미 내린 판단을 옮긴 것
  // (옛 CLI 머리 블록의 판단: *"the main agent already prints its
  // thought/action inline right above the prompt, so a header would just
  // duplicate it"*). 웹에만 이 게이트가 없어서 main confirm 이 바로 위
  // 💭/⚡ 두 줄을 그대로 반복했다. 서브에이전트 confirm 의 `↳ from` 은
  // agent 가 있으므로 그대로 산다 — 거기선 출처를 달리 알 방법이 없다.
  if (!agent) return null;
  if (!reasoning && !action) return null;
  const box = el("div", ["prompt-meta"]);
  if (agent) {
    const a = el("div", ["prompt-meta-agent"]);
    a.textContent = "↳ from " + agent;
    box.appendChild(a);
  }
  if (reasoning) {
    const r = el("div", ["prompt-meta-reasoning"]);
    r.textContent = "💭 " + reasoning.split("\n")[0];
    box.appendChild(r);
  }
  if (action) {
    const ac = el("div", ["prompt-meta-action"]);
    ac.textContent = "⚡ " + action;
    box.appendChild(ac);
  }
  return box;
}

// Build the "$ <command>" HTML for a confirm dialog, wrapping each
// dangerous (start,end) char range in a <span class="danger">. Ranges are
// pre-computed server-side (single source of truth); we only paint them.
// Every segment is escaped, so a command with < > & stays inert.
export function highlightDangerHtml(command, spans) {
  const cmd = String(command);
  if (!Array.isArray(spans) || !spans.length) {
    return "$ " + escapeHtml(cmd);
  }
  let out = "$ ";
  let pos = 0;
  spans.forEach(function (sp) {
    const s = sp[0];
    const e = sp[1];
    if (typeof s !== "number" || typeof e !== "number") return;
    if (s < pos || s >= e || e > cmd.length) return; // skip bad/overlapping
    out += escapeHtml(cmd.slice(pos, s));
    out += '<span class="danger">' + escapeHtml(cmd.slice(s, e)) + "</span>";
    pos = e;
  });
  out += escapeHtml(cmd.slice(pos));
  return out;
}

// ── POST helpers ───────────────────────────
function postInput(body, signal) {
  body.conn_id = session.connId; // identifies the sender (queued-message ownership)
  return fetch(
    "api/input",
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
      signal: signal,
    }
  );
}

// ── 보내기 한 번 ─────────────────────────────
// 규칙과 함수는 `send.js`(`sendOnce`) — 입력창·트레이 답·확인·글 없는 버튼이 같이 쓴다.

// 트레이 항목 안 맨 아래의 알림 줄 하나(`.ask-failed`) — 있으면 글만 바꾼다.
export function noteOnAnswer(item, text) {
  let n = item.querySelector(":scope > .ask-failed");
  if (!n) n = el("div", ["ask-failed"]);
  n.textContent = text;
  item.appendChild(n); // 늘 맨 아래 — stall 경고가 먼저 섰어도 그 뒤
}
export function clearAnswerNote(item) {
  const n = item.querySelector(":scope > .ask-failed");
  if (n) n.remove();
}

// 입력창은 항상 chat(3b): 활성 채널로 라우팅(main → /api/input, agent →
// /api/agent/<key>/input). main 의 확인 대화 답은 글로벌 트레이가 담당.
const chatSlot = { sending: false };
function submitChatOrPrompt() {
  const text = $input.value.trim();
  const images = attachReadyPaths();
  if (!text && !images.length) return;
  if (chatSlot.sending) {
    attachMessage("Still sending the previous message");
    return;
  }
  if (attachBusy()) {
    attachMessage("Still uploading");
    return;
  }
  sendOnce(
    chatSlot,
    function (signal) {
      // 바로 불린다(`sendOnce` 가 시작하며) — 지금의 채널로 간다
      if (ovActiveChannel !== "main") {
        // 에이전트 채널도 이미지를 받는다 (v10.61.0): inbox 항목 → 그 런의 시작
        // user 레코드 `images`. 같은 업로드 경로, 같은 `images:[path]` 모양.
        const agentBody = { content: text, conn_id: session.connId };
        if (images.length) agentBody.images = images;
        return fetch("api/agent/" + encodeURIComponent(ovActiveChannel) + "/input", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(agentBody),
          signal: signal,
        });
      }
      const body = { kind: "chat", content: text };
      if (images.length) body.images = images;
      return postInput(body, signal);
    },
    function (failure) {
      if (failure) {
        attachMessage("Not sent — " + failure);
        return;
      }
      // 보내는 사이 고쳐 쓴 글·새로 붙인 칩은 남긴다 — 보낸 것만 비운다
      if ($input.value.trim() === text) $input.value = "";
      attachRemove(images);
    }
  );
}

// 답할 물음이 이미 닫혔다는 알림 — 트레이 답의 409(다른 뷰어가 먼저 답함·묻기가 끝남), 그리고 보내는 사이
// 항목이 트레이에서 빠진 뒤의 실패. 입력창 아래 줄에 선다. 409 는 상태 코드로만 읽는다 — 웹에서 나오는
// 트레이 답의 409 는 "닫힌 물음" 뿐이고, 서버 문구(모델도 읽는 글)에 화면을 묶지 않는다.
export const ALREADY_CLOSED_NOTICE = "Not sent — already answered or closed";

// ── Input bindings ─────────────────────────
function requestStop(scope) {
  // Halt the in-flight chat turn at the next turn boundary. Fire and
  // forget — the worker's _on_interrupt path emits the observation and
  // flips back to worker_idle, which the SSE stream reflects.
  // Flip to "Stopping…" (disabled) immediately so the user gets
  // feedback and can't double-fire /api/stop.
  // ``scope`` (v10.142.0): "main"(본체) · "agents"(▾ Stop all agents — main 은 계속이라 Stopping… 없음) ·
  // "all"(▾ Stop everything).
  closeStopMenu();
  if (scope !== "agents") {
    session.stopRequested = true;
    updateSendEnabled();
  }
  // 닿지 못했으면 "Stopping…" 을 푼다 — 다시 누를 수 있게 (v10.155.0; 종전엔 그 런이 끝날 때까지 잠겼다)
  postStop(scope, function () {
    if (scope === "agents") return;
    session.stopRequested = false;
    updateSendEnabled();
  });
}

function closeStopMenu() {
  if (!$stopMenu) return;
  $stopMenu.hidden = true;
  if ($chatStopMore) $chatStopMore.setAttribute("aria-expanded", "false");
}

if ($chatStop) {
  $chatStop.addEventListener("click", function () {
    if (!$chatStop.disabled) requestStop("main");
  });
}
if ($chatStopMore && $stopMenu) {
  $chatStopMore.addEventListener("click", function (e) {
    e.stopPropagation();
    var open = $stopMenu.hidden;
    $stopMenu.hidden = !open;
    $chatStopMore.setAttribute("aria-expanded", open ? "true" : "false");
  });
  $stopMenu.addEventListener("click", function (e) {
    var item = e.target.closest ? e.target.closest("[data-scope]") : null;
    if (item) requestStop(item.getAttribute("data-scope"));
  });
  document.addEventListener("click", function (e) {
    if (!$stopMenu.hidden && !$stopMenu.contains(e.target) && e.target !== $chatStopMore) {
      closeStopMenu();
    }
  });
}

$send.addEventListener("click", function () {
  if ($send.disabled) return;
  // 입력창은 항상 chat(idle→런 시작·busy→큐). main ask 답변은 트레이가 담당.
  submitChatOrPrompt();
});
$input.addEventListener("keydown", function (e) {
  // ``e.isComposing`` / ``keyCode === 229`` guard the IME commit
  // step: when typing Korean / Japanese / Chinese, the Enter that
  // finalises the in-flight syllable arrives as a keydown with
  // ``isComposing: true``. Submitting on that Enter races the IME
  // commit — the typed-but-not-yet-committed character lands in
  // the textarea after we already sent the (incomplete) value,
  // leaving an orphan glyph + newline behind. Only treat Enter as
  // submit when no composition is active.
  if (
    e.key === "Enter" &&
    !e.shiftKey &&
    !e.isComposing &&
    e.keyCode !== 229
  ) {
    e.preventDefault();
    if ($send.disabled) return;
    submitChatOrPrompt(); // chat (queues if busy). Stop 은 별도 버튼.
  }
});

es.addEventListener("worker_state", function (e) {
  // Server-side flip: worker_busy fires right after popping a
  // user message, worker_idle right before the next dequeue
  // wait. Refresh / reconnect lands here too — the server
  // prepends the latest worker_state to the snapshot replay so
  // a freshly-connected client sees the correct send-button
  // state on the very first event, without having to wait for
  // the worker to actually transition.
  const d = JSON.parse(e.data);
  session.workerBusy = !!d.busy;
  // Any worker_state transition ends a pending stop: idle = the turn
  // we were stopping has finished; busy = a fresh turn started.
  session.stopRequested = false;
  updateSendEnabled();
  // 런이 끝났다 — 첫 이미지 런이 모델의 vision 여부를 정했을 수 있다 (v10.60.0).
  if (!session.workerBusy) attachRefreshVision();
});
