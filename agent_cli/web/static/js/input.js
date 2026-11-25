// input.js — 입력: Send/Stop 가시성, 프롬프트 메타·위험 강조, POST, confirm stall,
// 입력 바인딩, abort 버튼과 input_required·worker_state 배선.
// (v10.117.0, docs/web-modules/DESIGN.md §8 — 옛 main.js 의 절 [19]·[20]·[21]·[22]·[32])

import { $abort, $chatStop, $chatStopMore, $input, $send, $stopMenu, el } from "./dom.js";
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
import { ovActiveChannel, ovRenderAskTray, postStop } from "./channels.js";
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
  // main 의 확인 대화 대기 중엔 #chat-stop 을 숨긴다 — 그땐 런을
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
  // (`base._format_prompt_meta`: *"the main agent already prints its
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

// 트레이 항목 안 맨 아래의 알림 줄 하나(`.ask-failed`, `.confirm-stall` 과 같은 모양) — 있으면 글만 바꾼다.
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

// main ask 를 접는다 — 409(선착순 소비) / input_resolved 공용.
function ovFoldMainAsk() {
  session.mainAsk = null;
  clearConfirmStall();
  ovRenderAskTray();
  updateSendEnabled(); // ovMainAsk 해제 → busy 면 #chat-stop 다시 노출
  setAbortVisible(false);
}

// 답할 물음이 이미 닫혔다는 알림 — 트레이 답의 409(다른 뷰어가 먼저 답함·묻기가 끝남), 그리고 보내는 사이
// 항목이 트레이에서 빠진 뒤의 실패. 입력창 아래 줄에 선다. 409 는 상태 코드로만 읽는다 — 웹에서 나오는
// 트레이 답의 409 는 "닫힌 물음" 뿐이고, 서버 문구(모델도 읽는 글)에 화면을 묶지 않는다.
export const ALREADY_CLOSED_NOTICE = "Not sent — already answered or closed";

// main 의 답이 409 — 항목을 접고 알린다. 쓴 글은 버린다(물음이 없다). 그 사이 새 물음이 떴으면
// (``item.askOf`` 가 지금의 것이 아니면) 새 물음은 접지 않는다.
function mainAskAlreadyClosed(item) {
  if (session.mainAsk === item.askOf) ovFoldMainAsk();
  attachMessage(ALREADY_CLOSED_NOTICE);
}

// ── Confirm stall visibility ───────────────
// A confirm click normally resolves in well under a second (POST →
// worker unblocks → input_resolved). When nothing comes back — e.g.
// the browser's 6-connections-per-origin pool is starved by SSE tabs
// and the POST is silently queued — the button just looks broken.
// Surface that state instead of staying silent.
const CONFIRM_STALL_MS = 3000;
let confirmStallTimer = null;

// stall 타이머를 멈추고, ``item`` 을 주면 그 항목의 경고 줄도 지운다(항목이 접힐 때는 항목째 사라진다).
function clearConfirmStall(item) {
  if (confirmStallTimer) {
    clearTimeout(confirmStallTimer);
    confirmStallTimer = null;
  }
  const w = item && item.querySelector(":scope > .confirm-stall");
  if (w) w.remove();
}

// main confirm 결정 — 트레이 항목의 옵션 버튼에서 호출(key + 선택 코멘트). 지연·네트워크 오류는 상한도
// 끊기도 없이 둔다 — 늦게라도 닿은 확인 클릭은 적용되는 것이 맞다(3초 stall 경고가 보인다). 서버가
// 거절하면(409 밖) 항목 안에 사유가 서고 코멘트는 남는다.
export function ovSubmitMainConfirm(item, key, comment) {
  clearConfirmStall(item);
  clearAnswerNote(item);
  confirmStallTimer = setTimeout(function () {
    if (!session.mainAsk || session.mainAsk.kind !== "confirm" || !item.isConnected) return;
    if (item.querySelector(".confirm-stall")) return;
    const warn = el("div", ["confirm-stall"]);
    warn.textContent =
      "⚠ No response from the server yet — the connection may be " +
      "stalled (e.g. too many open tabs holding connections to this " +
      "host). The click applies as soon as it gets through; closing " +
      "unused tabs can help.";
    item.appendChild(warn);
  }, CONFIRM_STALL_MS);
  // 상한 없는 보내기 — 누를 때마다 새 자리(여러 번 눌러도 막지 않는다, 종전과 같다)
  sendOnce(
    { sending: false },
    function (signal) {
      return postInput({ kind: "confirm", key: key, comment: comment || "" }, signal);
    },
    function (failure, status) {
      if (status === 0) return; // 네트워크 오류 — 지연 경고가 보이는 사실이다(종전과 같다)
      clearConfirmStall(item); // 답이 왔다 — 지연 경고는 끝났다
      if (!failure) return;
      if (status === 409) return mainAskAlreadyClosed(item);
      noteOnAnswer(item, "⚠ Not sent — " + failure);
    },
    { limit: false }
  );
}

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

// 재접속 스냅샷 안에 main 의 물음이 있었나 (v10.149.2). 스냅샷은 늘 ``identity`` 로 시작해
// ``snapshot_end`` 로 끝나고(서버 `EventHub.register`, 라이브 이벤트는 그 뒤), 떠 있는 물음은 스티키라 반드시
// 그 사이에 온다. 끊긴 사이 물음이 풀렸으면 ``input_resolved`` 는 아무에게도 가지 않았다 — 스냅샷에 물음이
// 없으면 남은 항목을 접는다(라이브 ``input_resolved`` 와 같게, 알림 없음).
let askSeenInSnapshot = false;
es.addEventListener("identity", function () {
  askSeenInSnapshot = false;
});
es.addEventListener("snapshot_end", function () {
  if (!askSeenInSnapshot && session.mainAsk) ovFoldMainAsk();
});

es.addEventListener("input_required", function (e) {
  const d = JSON.parse(e.data);
  askSeenInSnapshot = true;
  // 같은 물음이 다시 왔다(재접속 스냅샷의 재생) — 항목을 그대로 둔다(쓰던 답·지연 경고가 산다). 물음마다
  // 서버가 새 ``ask_id`` 를 싣는다(v10.147.4).
  if (session.mainAsk && session.mainAsk.data.ask_id === d.ask_id) return;
  // 3b: main 의 확인 대화를 글로벌 ask 트레이 항목으로(입력창은 chat 유지).
  clearConfirmStall();
  session.mainAsk = { kind: d.kind, data: d };
  ovRenderAskTray();
  updateSendEnabled(); // ovMainAsk 반영 → #chat-stop 숨김(두 Stop 방지)
  // Allow aborting a stuck confirm wait. Worker side surfaces this as
  // ``(default_key, "")``.
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
