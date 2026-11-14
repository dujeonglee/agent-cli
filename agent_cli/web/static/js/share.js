// share.js — 🔗 공유 링크 + 토큰 사용량(fmtTok) + 대화 카드 SSE 핸들러 등록.
// (v10.117.0, docs/web-modules/DESIGN.md §8 — 옛 main.js 의 절 [25])

import { $tokenUsage, el } from "./dom.js";
import { copyToClipboard, es } from "./sse.js";
import { ovOnRoster } from "./channels.js";
import { channelOf, finishCard, MAIN } from "./timeline.js";
import {
  ctxApplyView,
  ctxViewKey,
  ctxViews,
  frameOfScope,
  frames,
  frameTouch,
  renderCompaction,
  sysLine,
} from "./frames.js";
import { genSlot, GRAMMAR, hideGenerating, renderStreamStall, showGenerating } from "./progress.js";
import { renderUserMessage } from "./thumbs.js";
import { renderAssistantTurn, renderError, renderObservation } from "./cards.js";
import { ovOnAgentCleared, ovOnAgentMsg } from "./runs.js";
import { qOnUserMsg } from "./roster.js";

// ── Share link (🔗) — copy a token-bearing URL others can open ──
// The token lives in an HttpOnly cookie (JS can't read it), so fetch it from
// the server and build the shareable URL from <base href> (which already
// carries origin + base-path, so it is correct for direct / LAN / board /
// caddy alike). This deliberately re-exposes the shared access token to the
// authenticated session so the operator can hand it to a teammate.
const $shareBtn = document.getElementById("share-btn");
if ($shareBtn) {
  $shareBtn.addEventListener("click", async function () {
    const orig = $shareBtn.textContent;
    try {
      const r = await fetch("api/share-url");
      if (!r.ok) throw new Error("HTTP " + r.status);
      const d = await r.json();
      const base = document.querySelector("base");
      const baseHref = base ? base.href : window.location.origin + "/";
      const url = baseHref + "?token=" + encodeURIComponent(d.token);
      await copyToClipboard(url);
      $shareBtn.textContent = "✓";
      $shareBtn.title = "Copied: " + url;
    } catch (e) {
      $shareBtn.textContent = "✗";
      $shareBtn.title = "Copy failed: " + e.message;
    }
    setTimeout(function () {
      $shareBtn.textContent = orig;
    }, 1200);
  });
}

export function fmtTok(n) {
  n = n || 0;
  if (n >= 1000000) return (n / 1000000).toFixed(1) + "M";
  return n >= 1000 ? (n / 1000).toFixed(1) + "K" : String(n);
}

// 상단 사용량 칩의 글 — ctx 점유·이 턴의 출력(+디코드 속도)·이 루프의 입력/출력 합.
// v8.57.0: ↑(턴 입력)은 ctx 분자와 동일 값이라 생략. 0/없는 조각은 뺀다.
export function usageLine(d) {
  const parts = [];
  const inTok = d.in || 0;
  const win = d.context_window || 0;
  if (inTok && win) {
    const pct = Math.round((inTok / win) * 100);
    parts.push("ctx " + fmtTok(inTok) + "/" + fmtTok(win) + " (" + pct + "%)");
  }
  if (inTok || d.out) parts.push("↓" + fmtTok(d.out));
  if (d.out_speed) parts.push(Math.round(d.out_speed) + " tok/s");
  if (d.total_in) parts.push("Σ↑" + fmtTok(d.total_in));
  if (d.total_out) parts.push("Σ↓" + fmtTok(d.total_out));
  if (d.grammar) parts.push("📐"); // 이 턴은 문법 제약 하에 생성됐다
  return parts.join(" · ");
}

// 상단 사용량 칩의 툴팁 — 줄에 없는 것(ttft·prefill 속도·캐시 적중 합·호출 수)까지.
// 0/없는 조각은 뺀다(시간을 안 주는 경로는 속도·ttft 가 자연히 빠진다).
export function usageTooltip(d) {
  const lines = ["context " + fmtTok(d.in) + " / " + fmtTok(d.context_window)];
  let turn = "turn out " + fmtTok(d.out);
  if (d.out_speed) turn += " (" + Math.round(d.out_speed) + " tok/s)";
  lines.push(turn);
  if (d.ttft_ms) lines.push("ttft " + Math.round(d.ttft_ms) + " ms");
  if (d.in_speed) lines.push("prefill " + Math.round(d.in_speed) + " tok/s");
  if (d.total_in || d.total_out) {
    let sum = "run in " + fmtTok(d.total_in);
    if (d.total_cached) sum += " (cached " + fmtTok(d.total_cached) + ")";
    sum += " · out " + fmtTok(d.total_out);
    if (d.calls) sum += " · " + d.calls + (d.calls === 1 ? " call" : " calls");
    lines.push(sum);
  }
  return lines.join("\n");
}


es.addEventListener("agent_roster", function (e) {
  // 상주 에이전트 목록/상태 sticky (P4) — Team 스윔레인 + 개요 채널 바.
  const d = JSON.parse(e.data);
  ovOnRoster(d); // 개요: 채널 바 + ask 트레이 + 상태
});

es.addEventListener("agent_msg", function (e) {
  // 상주 에이전트 대화 메시지 (persistent — 재접속 replay 포함).
  const d = JSON.parse(e.data);
  ovOnAgentMsg(d); // 개요: agent 대화 채널 스트림
});

es.addEventListener("agent_cleared", function (e) {
  // kill 시 그 에이전트 대화 비움 (resume 재생과 대칭 — 안 지우면 부활 시
  // conversation.jsonl 재생이 채널 스트림에 중복 append). 5단계: 드로어
  // 대신 개요 채널을 정리.
  const d = JSON.parse(e.data);
  if (d && d.key) ovOnAgentCleared(d.key);
});

es.addEventListener("knob", function (e) {
  // 세션 노브 상태 한 장 (v10.43.0, `/api/knobs` 와 같은 모양) — 다른 뷰어가
  // 바꾸면(또는 부팅 초기값) sticky `knob:<name>` 으로. 칩 IIFE 들이 name 으로
  // 거른다(KNOBS.on). 종전 노브별 SSE 이벤트 일곱 개를 대체.
  const d = JSON.parse(e.data);
  if (d && d.name === "grammar") GRAMMAR.active = !!d.active;
  document.dispatchEvent(new CustomEvent("agentcli:knob", { detail: d }));
});

es.addEventListener("thinking_tick", function (e) {
  // P5/v8.57.0: 사고 러너웨이 가시화. v9.8.0 부터 **대화 쪽 한 곳**만 —
  // 헤더 배지는 같은 숫자를 두 번 보여 제거했다.
  const d = JSON.parse(e.data);
  if (typeof d.tokens !== "number") return;
  // 헤더 배지는 제거했다 (v9.8.0) — 아래 생성 중 줄이 같은 숫자를 이미
  // 보여 화면에 문자 그대로 두 번 있었다. 그 줄은 `data-ch` 가 없어 **모든
  // 채널에서 항상 맨 아래**이고 자동 스크롤이 따라가므로, 헤더를 빼도
  // "지금 얼마나 생각 중인가"는 계속 보인다.
  // v9.4.0 ④: 생성 중 줄에도 싣는다. 사고만 하고 본문이 아직 없는 구간
  // (러너웨이가 정확히 그 모양이다)에서도 살아있음이 보여야 한다 —
  // 헤더 배지만 갱신하면 대화 쪽은 조용해 멎은 것처럼 읽힌다.
  genSlot(channelOf(d.task_id || "")).think = d.tokens;
  showGenerating(d.task_id);
});

es.addEventListener("directives_changed", function () {
  // Someone saved DIRECTIVE.md via the Prompt Inspector → tell the inspector
  // IIFE to re-fetch the editor so concurrent editors don't show stale text.
  window.dispatchEvent(new CustomEvent("agentcli:directives-changed"));
});

es.addEventListener("schedules_changed", function () {
  // 예약이 바뀌었다(추가·삭제·발화·놓침) → ⏰ IIFE 가 다시 가져온다.
  window.dispatchEvent(new CustomEvent("agentcli:schedules-changed"));
});

es.addEventListener("memory_changed", function () {
  // A `memory` op updated the ## Session Memory index.
  // 메모리 인덱스는 꼬리에 실린다 — 채널 틀을 다시 가져온다.
  Object.keys(frames).forEach(function (k) {
    if (frames[k].channel) frameTouch(frames[k]);
  });
});

es.addEventListener("prompt_snapshot", function (e) {
  // 모델 호출이 막 나갔다(또는 런이 끝나 닫힌 항목이 붙었다) — 그 스코프의 틀을
  // 지금 다시 가져온다. 사용량 줄은 호출이 끝난 뒤(token_usage) 채워진다.
  frameTouch(frameOfScope(JSON.parse(e.data).scope || ""));
});

es.addEventListener("token_usage", function (e) {
  // Top-bar readout: context occupancy %, this turn's out (+decode speed), and
  // the loop's running input/output sums. Server sends raw counts; we format here.
  const d = JSON.parse(e.data);
  // 모델 시점 틀: 이 호출을 낸 스코프의 스냅샷이 바뀌었다.
  frameTouch(frameOfScope(d.task_id || ""));
  const inTok = d.in || 0;
  const win = d.context_window || 0;
  var $base = document.getElementById("tok-base");
  if ($base) $base.textContent = usageLine(d);
  $tokenUsage.title = usageTooltip(d);
  // v8.57.0: 토큰 상세는 헤더에 상시 노출 (구 ctx 게이지 칩 폐기).
  if (inTok && win) $tokenUsage.hidden = false;
});

es.addEventListener("user_message", function (e) {
  const d = JSON.parse(e.data);
  renderUserMessage(d.content, d.ts, d.author || "", d.hidx, d.images);
  // ``author`` (a user's nickname) puts the message on the swimlane's
  // multiplexed user lane; author-less messages (🤝 starter) are card-only.
  qOnUserMsg(d); // 큐: 이 메시지가 큐서 주입된 것이면 ✓ 영수증
});

es.addEventListener("assistant_turn", function (e) {
  const d = JSON.parse(e.data);
  hideGenerating(d.task_id);
  renderAssistantTurn(d);
});

// 형식 거부 → 재시도. **카드를 그리지 않는다** (v9.8.0) — 거부된 원문은
// 하네스의 재시도 기계지 모델의 작업이 아니고, 사용자가 할 수 있는 일도 없다.
// 대신 생성 중 줄의 카운터만 올린다: 런은 계속되므로 `hideGenerating` 도
// 부르지 않는다(종전엔 불렀다가 다음 tick 에 다시 만들어 깜빡였다).
// 재시도를 다 쓰면 하드페일이 `turn_error` 로 **영속** 표면에 남는다.
es.addEventListener("retry_tick", function (e) {
  const d = JSON.parse(e.data || "{}");
  genSlot(channelOf(d.task_id || "")).retry += 1;
  showGenerating(d.task_id);
});

es.addEventListener("observation", function (e) {
  const d = JSON.parse(e.data);
  renderObservation(d);
});

es.addEventListener("compaction", function (e) {
  renderCompaction(JSON.parse(e.data));
});
// 모델 시점 (v10.6.0): 스코프별 sticky — 재접속 스냅샷에도 실린다.
es.addEventListener("ctx_view", function (e) {
  const d = JSON.parse(e.data || "{}");
  const key = ctxViewKey(d.task_id || "");
  ctxViews[key] = d;
  ctxApplyView(key);
});

// 턴 사이의 **진행 해설** — 재시도·열화·부팅 고지. 종전엔 이 이벤트에
// 리스너가 없어 10곳의 호출이 웹에서 통째로 버려졌다(HTTP 재시도 전부가
// 암전이었다 — `stream_stall` 이 고친 것과 같은 병인데 그쪽은 스트림
// 레벨만 덮었다). 휘발 한 줄이 이 부류의 올바른 기본값이다: 대화 기록에
// 남아야 하는 것(런 종료 사유)은 `turn_error` 로 따로 간다.
es.addEventListener("status", function (e) {
  const d = JSON.parse(e.data);
  if (!d || !d.message) return;
  const line = sysLine(d.state === "error" ? "✗" : "●", d.message,
                       d.state === "error" ? ["warn"] : null);
  finishCard(line, d);
});

es.addEventListener("stream_stall", function (e) {
  renderStreamStall(JSON.parse(e.data));
});

// v9.4.0 ④: stream_reset 리스너 제거 — 버릴 **부분 출력이 없다**.
// (v8.61.0 이 재전송 시 이어붙기를 고치려 넣은 것인데, 라이브 타이핑을
// 버리면서 그 문제 자체가 사라졌다. 서버 방출은 CLI 마르퀴가 쓰므로 유지.)

// Application-level turn/tool errors arrive as ``turn_error`` — NOT
// ``error`` — precisely so they don't collide with the native EventSource
// "error" event type (which drives the connection dot via ``es.onerror``
// above). Listening for ``error`` here would also latch the dot red on a
// healthy stream and try to ``JSON.parse`` data-less transport errors.
es.addEventListener("turn_error", function (e) {
  const d = JSON.parse(e.data);
  renderError(d);
});

// Bounded replay buffer: on reconnect to a very long session the server
// replays only the most recent window and says how many events fell off.
// Full history is still on disk (history.jsonl / --resume).
es.addEventListener("transcript_truncated", function (e) {
  const d = JSON.parse(e.data);
  const line = el("div", ["card", "card-sys"]);
  line.appendChild(el("span", ["sys-icon"], "⋯"));
  line.appendChild(
    el(
      "span",
      ["sys-text"],
      d.omitted + " earlier events omitted (reconnect replay limit — full record kept in session history)"
    )
  );
  // 세션 전체에 대한 고지라 main 에 둔다 — 인자를 **생략하지 않고 명시**한다
  // (생략하면 "귀속을 잊었다"와 구별이 안 된다; 소스 핀이 이걸 강제한다).
  // 이 이벤트엔 `ts` 가 없어(스냅샷에 직접 삽입) 시각은 안 찍힌다 —
  // `stampCard` 가 `ts == null` 이면 그냥 넘어간다.
  finishCard(line, { ts: d.ts, task_id: MAIN.task_id });
});

// 본문·사고 tick — 토큰 수만 온다(텍스트 없음).
es.addEventListener("stream_tick", function (e) {
  const d = JSON.parse(e.data) || {};
  genSlot(channelOf(d.task_id || "")).body = d.tokens || 0;
  showGenerating(d.task_id);
});

es.addEventListener("tool_tick", function (e) {
  // v10.70.0: 서버가 구조로 보내는 호출 조각(native_fc) — 긴 python 호출을 쓰는
  // 동안 본문·사고가 0 이라 멈춘 듯 보이던 구간을 메운다.
  const d = JSON.parse(e.data) || {};
  genSlot(channelOf(d.task_id || "")).tool = d.tokens || 0;
  showGenerating(d.task_id);
});

es.addEventListener("stream_end", function (e) {
  hideGenerating((JSON.parse(e.data || "{}") || {}).task_id);
});
