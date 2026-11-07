// state.js — 세션 전역 상태. 여러 모듈이 대입하는 넷을 객체 하나(session)의 필드로 둔다.
// ES 모듈의 live binding 은 정의 모듈만 대입할 수 있어서다(DESIGN §8.3).
// 옛 이름: ovMainAsk → session.mainAsk, myConnId → session.connId,
// workerBusy·stopRequested → session.workerBusy·session.stopRequested.
// 아래 주석은 옛 이름 그대로다(코드 불변 — 주석도 옮기기만 했다).
// (v10.117.0, docs/web-modules/DESIGN.md §8 — 옛 main.js 의 절 [3])

// ── State ──────────────────────────────────
// 3b: 입력창은 항상 chat(활성 채널로 라우팅). main 의 prompt/confirm 은 글로벌
// ask 트레이의 항목으로 렌더된다(ovMainAsk) — 입력창 mode 전환 폐지.
// Every connection is equal (all may send input / queue). ``myConnId`` (from
// the ``identity`` event) is used to mark "(you)" in the viewer roster and
// to own queued messages.
// ``workerBusy`` mirrors the server's ``worker_state`` event: true
// means the chat worker is between popping a user message and
// returning to the next ``dequeue_blocking`` call. While busy,
// the chat ``Send`` button stays disabled so a second message
// can't be queued into an in-flight turn. The prompt-mode answer
// path is not gated by this flag — answering an ``ask`` is the
// expected way to *unblock* the worker, not an additional message.
// Refresh / reconnect uses the server's ``_latest_worker_state``
// snapshot prepend, so this flag is set from the very first event
// a fresh client receives.
// True between clicking "Stop" and the worker actually returning to
// idle. While set, the button shows "Stopping…" and is disabled so a
// second click can't fire a redundant /api/stop. Reset on the next
// worker_state event (idle = the turn ended; busy = a fresh turn).
export const session = {
  mainAsk: null, // null | {kind:"prompt"|"confirm", data}
  connId: null,
  workerBusy: false,
  stopRequested: false,
};
