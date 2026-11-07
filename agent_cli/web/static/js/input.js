// input.js — 입력: Send/Stop 가시성, 프롬프트 메타·위험 강조, POST, confirm stall,
// 입력 바인딩, abort 버튼과 input_required·worker_state 배선.
// (v10.117.0, docs/web-modules/DESIGN.md §8 — 옛 main.js 의 절 [19]·[20]·[21]·[22]·[32])

import { $abort, $chatStop, $input, $send, el } from "./dom.js";
import { session } from "./state.js";
import { escapeHtml } from "./markdown.js";
import { es } from "./sse.js";
import {
  attachBusy,
  attachClear,
  attachMessage,
  attachReadyPaths,
  attachRefreshVision,
} from "./attach.js";
import { ovActiveChannel, ovRenderAskTray } from "./channels.js";
import { ovApplyChannelInput } from "./runs.js";

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
  // main ask(prompt/confirm) 대기 중엔 #chat-stop 을 숨긴다 — 그땐 런을
  // 중단(#chat-stop)하는 게 아니라 입력 대기를 취소(#abort)하는 상황.
  // (예전 currentMode 게이트를 ovMainAsk 로 대체 — 두 Stop 동시노출 방지.)
  return session.workerBusy && !session.mainAsk;
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
  // placeholder 는 채널(+busy) 인지형 — ovApplyChannelInput 단일 소유.
  ovApplyChannelInput();
}

// ── Input mode switching ───────────────────
// Provenance block (who/why/what) shown with a confirm or ask prompt so
// the user can tell which delegate agent is asking and about what.
// Returns null when there's nothing to show (e.g. main-agent prompt).
export function buildPromptMetaEl(data, includeAction) {
  if (!data) return null;
  const agent = typeof data.agent === "string" ? data.agent : "";
  const reasoning = typeof data.reasoning === "string" ? data.reasoning : "";
  const action = typeof data.action === "string" ? data.action : "";
  // **agent 없으면 안 그린다** — CLI 가 이미 내린 판단을 옮긴 것
  // (`base._format_prompt_meta`: *"the main agent already prints its
  // thought/action inline right above the prompt, so a header would just
  // duplicate it"*). 웹에만 이 게이트가 없어서 main confirm 이 바로 위
  // 💭/⚡ 두 줄을 그대로 반복했다. 서브에이전트 confirm 의 `↳ from` 은
  // agent 가 있으므로 그대로 산다 — 거기선 출처를 달리 알 방법이 없다.
  if (!agent) return null;
  if (!reasoning && !(includeAction && action)) return null;
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
  if (includeAction && action) {
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
function postInput(body) {
  body.conn_id = session.connId; // identifies the sender (queued-message ownership)
  return fetch(
    "api/input",
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }
  );
}

// 입력창은 항상 chat(3b): 활성 채널로 라우팅(main → /api/input, agent →
// /api/agent/<key>/input). main 의 prompt/confirm 답변은 글로벌 트레이가 담당.
function submitChatOrPrompt() {
  const text = $input.value.trim();
  const images = attachReadyPaths();
  if (!text && !images.length) return;
  if (attachBusy()) {
    attachMessage("Still uploading");
    return;
  }
  if (ovActiveChannel !== "main") {
    // 에이전트 채널도 이미지를 받는다 (v10.61.0): inbox 항목 → 그 런의 시작
    // user 레코드 `images`. 같은 업로드 경로, 같은 `images:[path]` 모양.
    const agentBody = { content: text, conn_id: session.connId };
    if (images.length) agentBody.images = images;
    fetch("api/agent/" + encodeURIComponent(ovActiveChannel) + "/input", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(agentBody),
    });
    $input.value = "";
    attachClear();
    return;
  }
  const body = { kind: "chat", content: text };
  if (images.length) body.images = images;
  postInput(body);
  $input.value = "";
  attachClear();
}

// main ask 를 접는다 — 409(선착순 소비) / input_resolved 공용.
function ovFoldMainAsk() {
  session.mainAsk = null;
  clearConfirmStall();
  ovRenderAskTray();
  updateSendEnabled(); // ovMainAsk 해제 → busy 면 #chat-stop 다시 노출
  setAbortVisible(false);
}

// ── Confirm stall visibility ───────────────
// A confirm click normally resolves in well under a second (POST →
// worker unblocks → input_resolved). When nothing comes back — e.g.
// the browser's 6-connections-per-origin pool is starved by SSE tabs
// and the POST is silently queued — the button just looks broken.
// Surface that state instead of staying silent.
const CONFIRM_STALL_MS = 3000;
let confirmStallTimer = null;

function clearConfirmStall() {
  if (confirmStallTimer) {
    clearTimeout(confirmStallTimer);
    confirmStallTimer = null;
  }
  const w = document.getElementById("confirm-stall");
  if (w) w.remove();
}

// main prompt(ask) 답변 — 트레이 항목의 입력에서 호출.
export function ovSubmitMainPrompt(text) {
  if (!text.trim()) return;
  postInput({ kind: "prompt", content: text }).then(function (res) {
    if (res && res.status === 409) ovFoldMainAsk(); // 이미 응답됨(다른 뷰어)
  });
}

// main confirm 결정 — 트레이 항목의 옵션 버튼에서 호출(key + 선택 코멘트).
export function ovSubmitMainConfirm(key, comment) {
  clearConfirmStall();
  confirmStallTimer = setTimeout(function () {
    var item = document.querySelector(".ask-main");
    if (!session.mainAsk || session.mainAsk.kind !== "confirm" || !item) return;
    if (item.querySelector(".confirm-stall")) return;
    const warn = el("div", ["confirm-stall"]);
    warn.textContent =
      "⚠ No response from the server yet — the connection may be " +
      "stalled (e.g. too many open tabs holding connections to this " +
      "host). The click applies as soon as it gets through; closing " +
      "unused tabs can help.";
    item.appendChild(warn);
  }, CONFIRM_STALL_MS);
  postInput({ kind: "confirm", key: key, comment: comment || "" })
    .then(function (res) {
      if (res && res.status === 409) ovFoldMainAsk(); // 이미 응답됨
    })
    .catch(function () {
      /* network error — the stall warning covers the visible feedback */
    });
}

// ── Input bindings ─────────────────────────
function requestStop() {
  // Halt the in-flight chat turn at the next turn boundary. Fire and
  // forget — the worker's _on_interrupt path emits the observation and
  // flips back to worker_idle, which the SSE stream reflects.
  // Flip to "Stopping…" (disabled) immediately so the user gets
  // feedback and can't double-fire /api/stop.
  session.stopRequested = true;
  updateSendEnabled();
  fetch("api/stop", {
    method: "POST",
  }).catch(function () {
    /* network blip — ignore; the turn will end on its own anyway */
  });
}

if ($chatStop) {
  $chatStop.addEventListener("click", function () {
    if (!$chatStop.disabled) requestStop();
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

// ── Abort button visibility ────────────────
// Shown only during ``input_required`` waits (ask answer / confirm
// decision) — POST /api/abort releases the worker thread's blocking
// input wait via an EOF sentinel. NOT shown during LLM streaming:
// the abort endpoint can't cancel a streaming provider call, and
// a button that doesn't do what it says undermines trust. True
// streaming cancellation is a Phase D concern (provider-level).
function setAbortVisible(visible) {
  $abort.hidden = !visible;
}
$abort.addEventListener("click", function () {
  fetch("api/abort", {
    method: "POST",
  });
});

es.addEventListener("input_required", function (e) {
  const d = JSON.parse(e.data);
  // 3b: main 의 prompt/confirm 을 글로벌 ask 트레이 항목으로(입력창은 chat 유지).
  clearConfirmStall();
  session.mainAsk = { kind: d.kind, data: d };
  ovRenderAskTray();
  updateSendEnabled(); // ovMainAsk 반영 → #chat-stop 숨김(두 Stop 방지)
  // Allow aborting a stuck prompt / confirm wait. Worker side
  // surfaces this as EOFError → ``(no response)`` (ask) or
  // ``(default_key, "")`` (confirm).
  setAbortVisible(true);
});

es.addEventListener("input_resolved", function () {
  ovFoldMainAsk();
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
