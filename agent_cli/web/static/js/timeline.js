// timeline.js — 카드 시각 + delegate 그룹(접히는 카드)·타임라인 추가 + scope_* 수명 배선.
// (v10.117.0, docs/web-modules/DESIGN.md §8 — 옛 main.js 의 절 [6]·[8]·[26]·[27])

import { $messages, el } from "./dom.js";
import { scheduleScroll } from "./scroll.js";
import { es } from "./sse.js";
import { attachCopy } from "./copy.js";
import { applyChannelFilter } from "./channels.js";
import { ctxApplyToCard, frameForCard, frameOfScope, frames, frameTouch } from "./frames.js";
import { closeRunBlock, ensureRunBlock, runBlocks } from "./runs.js";

// ── Card timestamps ────────────────────────
// Server stamps every event with `ts` (epoch seconds) at emit time; the
// browser formats to its own local time. Short form on the card
// (YYMMDD HH:MM:SS), full form (with ms) in the hover tooltip.
function pad2(n) {
  return String(n).padStart(2, "0");
}
// `ts` is epoch seconds (live `_emit`) or an ISO string (resume replay,
// from the history record). Normalise both to a Date.
function tsToDate(ts) {
  return typeof ts === "number" ? new Date(ts * 1000) : new Date(ts);
}
function fmtCardTime(ts) {
  const d = tsToDate(ts);
  return (
    pad2(d.getFullYear() % 100) + pad2(d.getMonth() + 1) + pad2(d.getDate()) +
    " " + pad2(d.getHours()) + ":" + pad2(d.getMinutes()) + ":" + pad2(d.getSeconds())
  );
}
function fmtCardTimeFull(ts) {
  const d = tsToDate(ts);
  return (
    d.getFullYear() + "-" + pad2(d.getMonth() + 1) + "-" + pad2(d.getDate()) +
    " " + pad2(d.getHours()) + ":" + pad2(d.getMinutes()) + ":" + pad2(d.getSeconds()) +
    "." + String(d.getMilliseconds()).padStart(3, "0")
  );
}
// Attach a muted timestamp to any `.card`. No-op when `ts` is absent
// (e.g. legacy buffered events) so nothing breaks if the field is missing.
//
// 자리는 **둘 중 하나**다 (v9.4.0 ⑥):
//   · 행(.row)이 있는 카드 → **첫 행 안**의 한 칸 (.row-time)
//   · 그 외(사용자 말풍선·시스템 줄·최종답) → 우상단 코너 배지 (.card-time)
// 코너 배지는 absolute 라 그 아래 놓이는 줄의 글자를 덮어 **겹쳐 보였다**
// (사용자 지적 2회 — 왕래 줄의 상대 칩, 그리고 생각/도구/관찰의 요약).
// 여백을 상수로 비워두는 방식은 배지 폭 추정에 기대 깨지기 쉬우므로, 행이
// 있으면 같은 그리드 안에 넣는다 — 겹칠 자리가 원천적으로 없다. 행이 없는
// 산문 블록만 `--time-w` 로 자리를 비운다.
export function stampCard(cardEl, ts) {
  if (ts == null) return cardEl;
  const row = cardEl.querySelector(".row");
  const t = el("span", [row ? "row-time" : "card-time"], fmtCardTime(ts));
  t.title = fmtCardTimeFull(ts);
  if (row) {
    row.classList.add("has-time");
    row.insertBefore(t, row.querySelector(".x"));
  } else {
    cardEl.appendChild(t);
  }
  return cardEl;
}

// ── Delegate task groups (collapsible cards) ──
//
// Parallel delegate workers (one per ``delegate({tasks:[...]})``
// entry) get their own collapsible card. Every event the worker
// emits — assistant_turn, observation, stream_chunk, error —
// carries ``task_id`` (auto-attached by ``WebRenderer._emit``),
// which routes the card into the matching group's body instead of
// the main timeline. Without this routing the parallel work would
// interleave and the user couldn't tell which task is doing what.
//
// Group state per task_id: { card, header, body, statusEl, closed }.
export const taskGroups = {};
// task_id → enclosing scope's task_id ("" = top level). Outlives the group
// entry (which ``closeTaskGroup`` drops) because the CARDS stay in the
// timeline: swimlane click-navigation still has to expand a finished
// ancestor chain to reveal a nested card.
const scopeParent = {};
// ancestor task_id → [{id, label}] of its currently-running descendants.
// A collapsed parent would otherwise hide live nested work completely.
const liveKids = {};
// task_id → **대화 채널**("main" 또는 상주 에이전트 key). 채널은 타임라인의
// 필터다(v9.4.0 ⑥): `#messages` 의 **직계** 자식만 `data-ch` 로 걸러 보여준다.
// 상주 에이전트의 작업 스코프는 `ctx_dir="agents/<key>"` 로 자기를 밝히므로
// (render/web.py begin_agent_work) 서버에 새 필드를 더할 필요가 없다. 중첩
// 스코프는 부모의 채널을 물려받는다 — 카드가 부모 body 안에 있어 필터가
// 닿지 않지만, 그 안에서 append 되는 왕래 줄의 귀속에는 쓰인다.
export const scopeChannel = {};
export function channelOf(taskId) {
  return (taskId && scopeChannel[taskId]) || "main";
}

export function scopeAncestors(taskId) {
  const chain = [];
  let cur = scopeParent[taskId];
  while (cur && chain.indexOf(cur) < 0) {
    chain.push(cur);
    cur = scopeParent[cur];
  }
  return chain;
}

/** Paint (or clear) the "▸ child running" hint on one ancestor header. This
 * is a DEDICATED element, not the status line: ``scope_status`` targets the
 * innermost scope of its own thread, so a parallel child and its parent can
 * both be writing status at the same time. */
function refreshLiveKids(taskId) {
  const g = taskGroups[taskId];
  if (!g) return;
  const kids = liveKids[taskId] || [];
  const last = kids.length ? kids[kids.length - 1].label : "";
  g.subEl.textContent = kids.length
    ? "▸ " + last + (kids.length > 1 ? " +" + (kids.length - 1) : "") + " running"
    : "";
}

function noteScopeStart(taskId, parent, label) {
  scopeParent[taskId] = parent || "";
  scopeAncestors(taskId).forEach(function (a) {
    (liveKids[a] = liveKids[a] || []).push({ id: taskId, label: label });
    refreshLiveKids(a);
  });
}

function noteScopeEnd(taskId) {
  scopeAncestors(taskId).forEach(function (a) {
    const list = liveKids[a];
    if (!list) return;
    liveKids[a] = list.filter(function (x) {
      return x.id !== taskId;
    });
    refreshLiveKids(a);
  });
}

/** 스코프의 **채널 귀속**을 등록하고, 이 스코프가 곧 채널인지 돌려준다.
 *
 * 상주 에이전트 작업은 `ctx_dir="agents/<key>"` 로 자기를 밝힌다(서버의
 * `begin_agent_work`). 그런 스코프는 **채널 자체**라 카드를 만들지 않는다
 * (v9.7.0) — 채널이 이미 묶어 주는데 그 안에서 또 묶으면 아무 정보도 더하지
 * 않으면서 기본 접힘 뒤로 투명성을 숨기고, main 창은 평평한데 agent 창만 한
 * 겹 들어가 "같은 컨셉으로 통일"이 깨진다. 요청의 경계는 왕래 줄(`← 받음` /
 * `→ 보냄`)이 이미 긋고 있다.
 *
 * 카드를 안 만들어도 배관은 그대로다: `appendToTimeline` 은 그룹이 없으면
 * `channelOf(taskId)` 로 도장을 찍어 루트에 붙이는데, 그게 정확히 원하는
 * 동작이다. 그 스코프 **안에서** 열리는 skill/inline 은 자기 카드를 그대로
 * 갖고, 부모 그룹이 없으니 `channelOf(parent)` = 그 에이전트 채널의 루트에
 * 붙는다 — 중첩 블록은 살아 있다. */
function noteScopeChannel(taskId, parent, ctxDir) {
  const m = /^agents\/(.+)$/.exec(ctxDir || "");
  scopeChannel[taskId] = m ? m[1] : channelOf(parent);
  return !!m;
}

/** 스코프 카드를 연다. 중첩 블록의 종류는 `kind` 가 가른다 (docs/chat-ui §3):
 * `skill` → 🪄 보라 레일, 그 외 → 🦀 inline agent · amber 레일.
 * 상주 에이전트는 여기 오지 않는다 (``noteScopeChannel`` 참조). */
function ensureTaskGroup(taskId, index, agent, taskText, kind, parent, depth, hidx) {
  if (taskGroups[taskId]) return taskGroups[taskId];

  const card = el("div", ["card", "card-task-group"]);
  card.dataset.taskId = taskId;
  // 이 카드를 연 레코드의 서수(부모 컨텍스트 기준) — 그 레코드가 부모의
  // 컨텍스트에서 빠지면 카드째 "컨텍스트 밖 대화" 로 접힌다 (v10.24.1).
  if (hidx != null) card.dataset.hidx = String(hidx);
  card.dataset.kind = kind || "run";
  card.dataset.depth = String(depth || 0);
  card.classList.add(
    kind === "skill" ? "scope-skill" : kind === "harness" ? "scope-harness" : "scope-inline"
  );

  const header = el("div", ["task-header"]);
  const chevron = el("span", ["task-chevron"], "▶");
  const title = el("span", ["task-title"]);
  // Scope card title adapts to kind: a skill subloop (🪄 label) vs a
  // delegate/one-shot worker (🦀 [n] agent: task).
  if (kind === "skill") {
    title.textContent = "🪄 " + taskText;
  } else if (kind === "harness") {
    // 하니스 작업(감사, v10.53.0) — main 이 한 일이 아니다. 접힌 채로 한 줄,
    // 펼치면 그 런이 실제로 한 일. 종전엔 스코프가 없어 main 타임라인에 섞였다.
    title.textContent = "🔎 " + (agent || "harness") + " · seq " + taskText;
  } else {
    const label = agent ? agent + ": " + taskText : taskText;
    title.textContent = "🦀 [" + (index + 1) + "] " + label;
  }
  const statusEl = el("span", ["task-status"], "starting…");
  const subEl = el("span", ["task-sub"]);
  const meta = el("span", ["task-meta"]);
  header.appendChild(chevron);
  header.appendChild(title);
  header.appendChild(subEl);
  header.appendChild(statusEl);
  header.appendChild(meta);

  const body = el("div", ["task-body"]);
  body.hidden = true; // default collapsed

  // Collapse from ANY position (not just the top arrow):
  //   1. the whole header row toggles (chevron is decorative),
  //   2. the header is CSS-sticky, so on a long expanded card it
  //      stays pinned at the top of the viewport — one click to
  //      collapse no matter how far you've scrolled into the body,
  //   3. clicking the body's own padding/gutter also collapses
  //      (``e.target === body`` only — nested cards, links, and text
  //      selection inside the body are untouched).
  function toggleTaskGroup() {
    body.hidden = !body.hidden;
    chevron.textContent = body.hidden ? "▶" : "▼";
    // NOTE: no scrollIntoView here. The header is CSS-sticky (top:0), so
    // it already stays reachable when a long body expands — the earlier
    // `scrollIntoView({behavior:"smooth"})` became both REDUNDANT and
    // HARMFUL once sticky was added (v7.13.0): smooth-scrolling toward a
    // sticky element re-computes a moving target and thrashes against the
    // streaming `scrollToBottom`, freezing the UI on a live delegate card.
  }

  header.addEventListener("click", toggleTaskGroup);
  body.addEventListener("click", function (e) {
    // Only the body's own surface (padding/gutter) collapses — never a
    // click that lands on nested content the user is reading/selecting.
    if (e.target === body) toggleTaskGroup();
  });

  card.appendChild(header);
  card.appendChild(body);
  // 모델 시점 틀: 머리 아래 "맨 처음 받는 것", 몸통 아래 "매 턴 끝에 붙는 것".
  // 몸통이 접혀 있으면 CSS 가 둘 다 감춘다.
  frameForCard(taskId, header, body);
  // 중첩 위치는 **부모**가 정하고(`task_id: parent`), 채널 귀속은 **자기
  // 것**이다(세 번째 인자) — 부모가 카드 없는 상주 스코프면 루트로 떨어지는데
  // 그때 부모 채널로 찍히면 출처가 어긋난다. 종전엔 append 뒤에 `data-ch` 를
  // 다시 칠했고, 이제 인자 하나로 끝난다.
  //
  // `ts` 를 주지 않는 이유: 헤더 우상단은 이미 `✓ (2.1s)` 가 쓰고 있어
  // 코너 배지가 겹친다(어제 왕래 줄에서 겪은 그 부류). 스코프 카드는 소요
  // 시간을 `meta` 에 따로 보이므로 시각이 없어도 잃는 게 없다.
  finishCard(card, { task_id: parent }, scopeChannel[taskId]);

  const group = {
    card: card,
    header: header,
    body: body,
    chevron: chevron,
    statusEl: statusEl,
    subEl: subEl,
    meta: meta,
    closed: false,
    toggle: toggleTaskGroup,
  };
  taskGroups[taskId] = group;
  scheduleScroll();
  return group;
}

function updateTaskStatus(taskId, status) {
  const g = taskGroups[taskId];
  if (!g || g.closed) return;
  g.statusEl.textContent = status;
}

function closeTaskGroup(taskId, success, durationS, error) {
  const g = taskGroups[taskId];
  if (!g) return;
  g.closed = true;
  g.statusEl.textContent = ""; // live status no longer relevant
  g.subEl.textContent = ""; // nor a nested-child hint
  delete liveKids[taskId];
  g.card.classList.add(success ? "task-ok" : "task-fail");
  const icon = success ? "✓" : "✗";
  const dur = durationS != null ? " (" + durationS.toFixed(1) + "s)" : "";
  g.meta.textContent = icon + dur;
  if (!success && error) {
    const errEl = el("div", ["task-error"], error);
    g.body.appendChild(errEl);
  }
  // Release the global entry now that the task is done. The card's
  // DOM stays in the timeline (still visible + expandable via its own
  // header listener); only this bookkeeping reference is dropped so
  // ``taskGroups`` doesn't grow unbounded over a long session and no
  // stale entry lingers for a future task_id to collide with. No more
  // worker events arrive for this task_id after ``delegate_task_end``.
  delete taskGroups[taskId];
}

/** Append ``cardEl`` to either the main timeline or a task group's
 * body, based on ``taskId``. If the task group hasn't been
 * registered yet (event raced before ``delegate_task_start``), the
 * card falls back to the main timeline so it isn't dropped. */
export function appendToTimeline(cardEl, taskId, channel) {
  if (taskId && runBlocks[taskId]) {
    runBlocks[taskId].body.appendChild(cardEl);
    return;
  }
  if (taskId && taskGroups[taskId]) {
    taskGroups[taskId].body.appendChild(cardEl);
    return;
  }
  // 루트 append 만 채널 필터의 대상이다. `data-ch` 가 **없는** 노드는 어느
  // 채널에서나 보인다(생성 중 한 줄처럼 채널과 무관한 표시).
  // ``channel`` 은 귀속을 **직접** 지정한다 — 왕래 줄은 자기 에이전트의
  // 채널에 속하지 `channelOf(task_id)` 가 아니다. 종전엔 호출부가
  // `$messages.appendChild` + `dataset.ch` + `applyChannelFilter` 를 손으로
  // 조립했고, 그 세 단계가 누수의 단골 자리였다.
  cardEl.dataset.ch = channel || channelOf(taskId);
  $messages.appendChild(cardEl);
  applyChannelFilter(cardEl);
}

/** 타임라인 카드를 **마무리**한다 — 시각 도장 · 배치(중첩 body 또는 채널
 * 필터가 걸린 루트) · 스크롤 1회 병합.
 *
 * 13개 렌더러가 진짜로 공유하는 건 본문 구성이 아니라 이 **꼬리 배관**이고,
 * 최근 누수가 전부 여기서 났다(사용자 입력=도장 누락, 거부된 응답=task_id
 * 흘림, 왕래=세 단계 수동 조립, 무진전 줄=스코프 누락). 조사에서 하나 더
 * 나왔다 — 거부된 응답 카드는 아예 `stampCard` 를 안 불러 **시각 없는
 * 카드**였다(그 카드 자체가 v9.8.0 에서 사라졌다).
 *
 * 인자는 **이벤트 페이로드 통째**다. 추출한 id 를 받으면 `undefined` 가
 * 조용히 통과하지만(실제로 `transcript_truncated` 가 그랬다), 페이로드를
 * 받으면 귀속 누락이 "필드를 잊었다"가 아니라 **"인자를 안 넘겼다"** 가 된다.
 * main 에 일부러 두려면 `MAIN` 을 명시한다. */
export const MAIN = { task_id: "" };
export function finishCard(cardEl, ev, channel) {
  stampCard(cardEl, ev && ev.ts);
  // 표시 전용 주석 — 서버의 `note_next()` 가 `_emit` 에서 실어 보낸다.
  // **여기 한 곳**이라 어떤 타임라인 객체든 공짜로 주석을 받는다(관찰·최종답·
  // 왕래·시스템 줄 전부 이 함수를 지난다). 모델 컨텍스트와는 무관하다.
  if (ev && ev.note) {
    const n = el("span", ["card-note"], ev.note);
    n.title = ev.note;
    cardEl.appendChild(n);
  }
  appendToTimeline(cardEl, (ev && ev.task_id) || "", channel);
  ctxApplyToCard(cardEl, (ev && ev.task_id) || "");
  attachCopy(cardEl);
  scheduleScroll();
}

// ── Delegate task lifecycle ────────────────
//
// Three event types frame each parallel-delegate worker's
// collapsible card:
//   scope_start   → open card (default collapsed); kind = skill | run
//   scope_status  → update live status line (transient)
//   scope_end     → close card with ✓/✗ + duration
// Unified path: a skill subloop (e.g. /orchestrate) and a delegate/one-shot
// worker now BOTH arrive as scope_start — previously skills emitted an
// un-handled group_start and drew no card.
es.addEventListener("scope_start", function (e) {
  const d = JSON.parse(e.data);
  // NOTE: a resume-replayed scope (``d.replay``) builds its card too. It used
  // to return here — the reasoning being that the scope's turns replayed flat,
  // so the card would be an empty shell. Since v7.28.0 the resume replay is
  // ONE time-ordered stream (``replay_session``) and each turn carries the
  // scope it belongs to, so the card opens BEFORE its turns arrive and they
  // land inside it. Skipping it now would be the bug: no card at all, and a
  // swimlane bar offering navigation to something that does not exist.
  // 채널 귀속은 카드와 무관한 등록이라 먼저, 그리고 **채널 자체인 스코프는
  // 카드를 만들지 않는다**(v9.7.0 — 자기 채널 안에서 자기를 또 묶지 않는다).
  // 채널 자체인 스코프(상주 에이전트의 런)는 접히는 카드가 아니라 **런
  // 블록**이다 (v9.23.0, docs/chat-ui §12): 받은 항목이 머리, 단계가 몸통,
  // 결과가 꼬리. 왕래 줄(보냄/받음)이 하던 "요청 경계" 를 이 블록이 긋는다.
  const grp = noteScopeChannel(d.task_id, d.parent || "", d.ctx_dir || "")
    ? (ensureRunBlock(d), null)
    : ensureTaskGroup(
        d.task_id,
        d.index || 0,
        d.agent || "",
        d.label || "",
        d.kind || "run",
        d.parent || "",
        d.depth || 0,
        d.hidx,
      );
  // Register the parent link + light up the ancestors' "child running" hint.
  // AFTER ensureTaskGroup so the new card is already nested inside its parent.
  noteScopeStart(d.task_id, d.parent || "", d.agent || d.label || "scope");
});

es.addEventListener("scope_status", function (e) {
  const d = JSON.parse(e.data);
  updateTaskStatus(d.task_id, d.status || "");
});

es.addEventListener("scope_end", function (e) {
  const d = JSON.parse(e.data);
  noteScopeEnd(d.task_id);
  if (runBlocks[d.task_id]) {
    closeRunBlock(d);
    frameTouch(frameOfScope(d.task_id));
    return;
  }
  // A replayed scope whose turns are not in this session's history closes
  // EMPTY (an old session recorded before turns carried their scope, or a
  // sub-agent whose turns live in its own context). Say why, rather than
  // leaving a card that looks like it lost its content.
  if (d.replay) {
    const g = taskGroups[d.task_id];
    if (g && !g.body.querySelector(".card")) {
      const note = el("div", ["card", "card-sys", "task-empty-note"]);
      note.appendChild(el("span", ["sys-icon"], "⊘"));
      note.appendChild(
        el(
          "span",
          ["sys-text"],
          "This run's turn records are not in this session's history — a subagent context, or a record from before resume.",
        ),
      );
      g.body.appendChild(note);
    }
  }
  closeTaskGroup(d.task_id, !!d.success, d.duration_s, d.error || "");
  frameTouch(frames[d.task_id]); // 종료 시점 상태로 고정된다
});

// ── 타임라인 = 유일한 표면 (v9.4.0 ②, docs/chat-ui) ──
// 종전엔 개요(요약) / 전문(드로어) / 흐름(스윔레인) 셋이 같은 대화를 각자
// 그렸고, 투명성(도구 호출·reasoning)이 **드로어 안에만** 있었다. 기본 화면인
// 개요엔 도구가 아예 안 나와(슬래시만 화이트리스트 예외) "간단"과 "투명"이
// 둘 다 손해였다. 이제 `#messages` 하나다 — 뷰 전환·드로어·탭이 전부 없다.
//
// 에이전트 작업이 사라지지 않는 이유: `begin_agent_work` 가 task_id 를 붙여
// `scope_start` 를 내고 `_emit` 이 후속 이벤트에 자동 부착하므로, 에이전트의
// 생각·도구·결과는 **이미** 여기 중첩 카드로 들어와 있었다(§7.5). 개요를
// 지우는 것은 잃는 게 아니라 드러내는 것이다.
