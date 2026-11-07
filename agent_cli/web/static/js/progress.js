// progress.js — 스트림 무진전(stall) 표시 + 생성 중 표시.
// (v10.117.0, docs/web-modules/DESIGN.md §8 — 옛 main.js 의 절 [12]·[18])

import { el } from "./dom.js";
import { scheduleScroll } from "./scroll.js";
import { appendToTimeline, channelOf, finishCard } from "./timeline.js";
import { fmtTok } from "./share.js";

// ── 스트림 무진전(stall) 표시 (v8.60.0) ──────────────────────
// 종전엔 이 알림이 전부 ``status`` 이벤트로 왔고 여기 리스너가 없어
// **통째로 드롭**됐다 — 사용자는 40분을 아무 표시 없이 기다린 뒤
// "LLM call failed" 만 봤다. 두 부류로 갈라 표시한다:
//   wait   = 30초마다 반복 → 카드 하나를 **제자리 갱신** (compaction
//            마커가 scope 별 줄을 재사용하는 것과 동형)
//   resend = 드문 전이 → 기록으로 남긴다
//   clear  = 대기 종료 → 제자리 카드 제거
let stallLine = null;
function mmss(s) {
  const n = Math.max(0, Math.floor(Number(s) || 0));
  return Math.floor(n / 60) + ":" + String(n % 60).padStart(2, "0");
}
export function renderStreamStall(d) {
  if (d.kind === "wait") {
    if (!stallLine || !stallLine.isConnected) {
      stallLine = el("div", ["card", "card-sys", "stall-line"]);
      stallLine.appendChild(el("span", ["sys-icon"], "⏳"));
      stallLine.appendChild(el("span", ["sys-text"]));
      const bar = el("div", ["stall-bar"]);
      bar.appendChild(el("i"));
      stallLine.appendChild(bar);
      // 생성 시 한 번만 찍힌다 → **대기 시작 시각**. 이 줄은 30초마다
      // 제자리 갱신되므로, 마지막 갱신 시각이 아니라 "언제부터 기다렸나"가
      // 남는 게 맞다(본문의 경과 시간과 합쳐 읽힌다).
      finishCard(stallLine, d);
    }
    const txt = stallLine.querySelector(".sys-text");
    const fill = stallLine.querySelector(".stall-bar > i");
    txt.textContent =
      "Waiting for a response " +
      mmss(d.elapsed_s) +
      " / " +
      mmss(d.limit_s) +
      " · attempt " +
      d.attempt +
      "/" +
      d.attempts;
    if (fill) {
      const pct = d.limit_s > 0 ? (d.elapsed_s / d.limit_s) * 100 : 0;
      fill.style.width = Math.max(0, Math.min(100, pct)) + "%";
    }
    return;
  }
  // 전이·정리 — 제자리 줄을 먼저 걷는다(낡은 경과 시간을 남기지 않는다).
  if (stallLine && stallLine.isConnected) stallLine.remove();
  stallLine = null;
  if (d.kind === "resend") {
    const line = el("div", ["card", "card-sys", "stall-resend"]);
    line.appendChild(el("span", ["sys-icon"], "↻"));
    line.appendChild(
      el(
        "span",
        ["sys-text"],
        "Stream stalled — reconnected and resent (attempt " +
          d.attempt +
          "/" +
          d.attempts +
          ")"
      )
    );
    finishCard(line, d);
  }
}

// ── 생성 중 표시 (v9.4.0 ④, docs/chat-ui §2) ────────────────────
// 스트리밍(라이브 타이핑)을 버리고 **한 줄**만 남긴다:
//     ● 생성 중 · 1.2K tokens · 💭 사고 2.1K
// 숫자가 오르면 살아 있다는 뜻이고, 사고 토큰이 따로 잡히면 러너웨이가
// 바로 보인다. 본문이 흐르지 않으므로 **카드가 자라며 화면이 튀지 않는다**.
// 서버는 0.5s 스로틀 tick 만 보내므로 트래픽도 토큰 수와 무관하다.
// **채널당 하나**다 (v9.9.0). 종전엔 전역 엘리먼트 하나를 `data-ch` 없이
// 붙여 모든 채널에 같은 줄이 보였다 — 에이전트가 하나일 때를 전제한 판단이라
// 넷이 되자 거짓말이 됐다(멈춰 있는 에이전트 창에 옆 에이전트의 사고량이
// 비쳤다, 사용자 제보). 서버도 같은 릴리스에서 누적을 스코프별로 갈랐다 —
// 화면만 나눠 그리면 숫자 자체가 이미 섞인 값이다.
//
// `channel -> {el, body, think, retry}`. 귀속은 `channelOf(task_id)` —
// 카드가 쓰는 그 함수라 필터와 어긋날 수 없다.
const genByCh = {};
// 📐 문법 제약이 실리는 세션인가 — 생성 중 줄이 읽는다(sticky knob:grammar).
export const GRAMMAR = { active: false };
// fmtTok 은 헤더 토큰바가 이미 가진 것을 쓴다 (같은 IIFE — 중복 선언 금지).

export function genSlot(channel) {
  let g = genByCh[channel];
  if (!g) {
    const node = el("div", ["gen"]);
    node.appendChild(el("span", ["gen-pulse"]));
    node.appendChild(el("span", ["gen-label"], "Generating"));
    node.appendChild(el("span", ["gen-tok"]));
    g = genByCh[channel] = { el: node, body: 0, think: 0, tool: 0, retry: 0 };
  }
  return g;
}

/** `taskId` 가 속한 채널의 생성 중 줄을 갱신한다(없으면 만든다). */
export function showGenerating(taskId) {
  const channel = channelOf(taskId || "");
  const g = genSlot(channel);
  const parts = [];
  // v10.70.0: 앞 숫자는 세 채널(본문·🧠 사고·🔧 호출 조각)의 합 — "생성 중" 의
  // 크기. 내역은 0 이 아닌 사고·호출만. 아이콘은 노브 칩(🧠)과 같다.
  const total = (g.body || 0) + (g.think || 0) + (g.tool || 0);
  if (total) parts.push("· ~" + fmtTok(total) + " tokens");
  if (g.think) parts.push("· 🧠 " + fmtTok(g.think));
  if (g.tool) parts.push("· 🔧 " + fmtTok(g.tool));
  // 재시도는 **왜 오래 걸리는지**의 유일한 단서다. 거부된 응답 자체는 안
  // 보여주지만(하네스의 재시도 기계는 모델의 작업이 아니다), "그냥 느린 것"과
  // "형식 거부로 맴도는 것"은 구별돼야 한다 — 형식 붕괴가 잦은 로컬 모델에선
  // 실제로 다른 상황이고, 후자면 `--verbose` 로 넘어갈 신호다.
  if (g.retry) parts.push("· ↻ retries " + g.retry);
  // 문법 제약 중이면 표시 — 속도가 왜 다른지가 이 줄에 있어야 한다.
  if (GRAMMAR.active) parts.push("· 📐");
  g.el.querySelector(".gen-tok").textContent = parts.join(" ");
  // 매번 다시 붙인다 = **맨 아래로 이동**(appendChild 는 기존 노드를 옮긴다).
  // 진행 표시는 그 채널의 마지막 줄이어야 한다. taskId 를 비워 넘기는 이유:
  // delegate/skill 스코프의 접히는 카드 **안**으로 들어가면 접었을 때 사라져
  // "멈춘 것처럼" 보인다 — 귀속은 채널로 직접 지정한다.
  appendToTimeline(g.el, "", channel);
  scheduleScroll();
}

/** 그 채널의 생성 중 줄을 거둔다. 다른 채널은 건드리지 않는다. */
export function hideGenerating(taskId) {
  const channel = channelOf(taskId || "");
  const g = genByCh[channel];
  if (!g) return;
  g.el.remove();
  delete genByCh[channel];
}
