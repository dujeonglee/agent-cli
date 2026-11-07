// cards.js — 한 줄 리듬(makeRow) + 발신 도구 칩 + 스텝 카드(사고·행동·관찰).
// (v10.117.0, docs/web-modules/DESIGN.md §8 — 옛 main.js 의 절 [15]·[16]·[17])

import { el, elHtml } from "./dom.js";
import { colorizeDiffBody, escapeAndFormat, escapeHtml } from "./markdown.js";
import { scheduleScroll } from "./scroll.js";
import { fence } from "./copy.js";
import { ovAgentLabel, ovJump, ovRoster } from "./channels.js";
import { finishCard } from "./timeline.js";
import { thumbStrip } from "./thumbs.js";

// ── 한 줄 리듬 (v9.4.0 ③, docs/chat-ui §2) ──────────────────
// 모든 줄이 `아이콘 · 종류 · 한 줄 요약 · 펼침표시` 4칸 그리드다. 기본은
// 한 줄이고 누르면 전문이 펼쳐진다 — **투명성은 깊이로, 간결함은 기본
// 상태로**. 펼칠 게 있는 줄에만 ▸ 가 뜬다(없는 줄에 뜨면 눌러도 아무 일이
// 없어 고장으로 읽힌다).
export function makeRow(icon, kind, summary, bodyNode, extraCls, tailNode) {
  const row = el("div", ["row"].concat(extraCls || []));
  row.appendChild(el("span", ["ic"], icon));
  row.appendChild(el("span", ["k"], kind));
  row.appendChild(el("span", ["s"], summary));
  // 꼬리 칸(선택): 왕래 줄의 **상대 이름**처럼 항상 보여야 하는 것. 펼침
  // 표시(`.x`)는 hover 전까지 투명하므로 거기에 섞을 수 없다 — 별도 칸으로
  // 두고 그리드를 5칸으로 늘린다(`.row.has-tail`).
  if (tailNode) {
    row.classList.add("has-tail");
    row.appendChild(tailNode);
  }
  const mark = el("span", ["x"]);
  row.appendChild(mark);
  if (bodyNode) {
    row.classList.add("can");
    mark.textContent = "▸";
    bodyNode.classList.add("row-body", "hide");
    row.appendChild(bodyNode);
    row.addEventListener("click", function (e) {
      // 본문 안의 선택·클릭(복사 등)은 접기를 유발하지 않는다.
      if (e.target.closest(".row-body")) return;
      const open = !bodyNode.classList.toggle("hide");
      row.classList.toggle("open", open);
      mark.textContent = open ? "▾" : "▸";
    });
  }
  return row;
}

/** 도구 호출 → 한 줄 요약. 길이는 CSS ellipsis 가 맡고, 여기선 **무엇을
 *  했는지 알아볼 수 있는 가장 짧은 문자열**을 고른다. */
function actionSummary(tool, inputStr) {
  let p = {};
  try {
    p = JSON.parse(inputStr || "{}");
  } catch (_e) {
    p = {};
  }
  if (typeof p !== "object" || p === null) p = {};
  if (tool === "shell" || tool === "sh") return String(p.command || "");
  if (tool === "wait") return String(p.reason || "waiting");
  if (tool === "read_file") return String(p.path || "");
  if (tool === "write_file") return String(p.path || "");
  if (tool === "edit_file") {
    const n = Array.isArray(p.edits) ? p.edits.length : 0;
    return String(p.path || "") + (n ? " · " + n + " places" : "");
  }
  if (tool === "run_skill") return String(p.name || "");
  if (tool === "agent") {
    const who = p.key || p.agent || p.profile || p.mode || "";
    const task = String(p.task || p.message || "");
    return (who ? who + (task ? " · " : "") : "") + task;
  }
  if (tool === "ask") {
    if (typeof p.question === "string" && p.question) return p.question;
    const qs = Array.isArray(p.questions) ? p.questions : [];
    return qs.length ? String(qs[0]) + (qs.length > 1 ? " +" + (qs.length - 1) + " more" : "") : "";
  }
  // 발신 도구 — 본문이 곧 요약. 상대는 꼬리 칩(`sendPeerChip`)이 맡는다.
  if (SEND_TOOLS[tool]) return String(p.text || "");
  if (tool === "complete") return String(p.result || "");
  // 알 수 없는 도구: 첫 문자열 값이 대개 가장 설명적이다
  for (const k of Object.keys(p)) {
    if (typeof p[k] === "string" && p[k]) return p[k];
  }
  return "";
}

/** 관찰 출력 → 한 줄 요약. **첫** 유의미한 줄 (v10.74.1). 접힌 줄은 사람이
 *  보는 것이고, 사람은 "펼치기 전엔 첫 줄" 에 익숙하다. 종전(v9.4.0)엔 성공이면
 *  **마지막** 줄이었다 — 테스트·린트의 결론(3826 passed)이 끝에 있어서. 그러나
 *  하니스의 파일 도구는 머리가 요약이고(write_file "Wrote …", 그 뒤 해시라인 에코),
 *  read_file·fetch·`/sh ls` 의 끝 줄은 임의의 한 줄이라 접힌 줄이 파일의 `}` 나
 *  목록의 마지막 항목을 보였다(사용자 보고). 결론이 끝에 있는 출력은 펼쳐 본다. */
// 실패는 **원인** 줄 (v9.24.2): 오류 원인은 앞에 오고, 끝은 안내 문구거나 긴
// 덤프의 꼬리다 — 실측 `✗ memory×2  } This op did NOT run…` (스키마 JSON 의 닫는
// 괄호). 배치면 첫 FAILED op 의 다음 줄이 그 op 의 원인이다.
const BATCH_HEAD = /^\[\d+\/\d+\]\s/;
// 런 도중 흡수된 요청(v9.25.0 `absorb_pending`) — 머리 `── request [n]
// from X (arrived while you were working) ──`, 본문, 꼬리 `(You now owe …)`.
// 종전 "성공은 마지막 줄" 규칙이 꼬리 상용구만 보여줘 33건이 전부 같은 한 줄로
// 보였다(2an3cv). 첫 줄 규칙에서도 머리 `── request … ──` 는 장식이라 요청 본문의
// 첫 줄과 보낸 이를 낸다(v9.26.1).
const ABSORBED_HEAD = /^── request \[(\d+)\] from (.+?) \(arrived while you were working\) ──$/;
function absorbedSummary(lines) {
  const m = ABSORBED_HEAD.exec(lines[0].replace(/^Observation:\s*/, "").trim());
  if (!m) return null;
  const body = lines.slice(1).find((l) => !/^\(You now owe /.test(l.trim()));
  return "[" + m[1] + "] " + m[2] + ": " + (body ? body.trim() : "");
}

export function obsSummary(content, failed) {
  const lines = String(content || "").split("\n").filter((l) => l.trim());
  if (!lines.length) return "(no output)";
  if (!failed) return absorbedSummary(lines) || lines[0].replace(/^Observation:\s*/, "").trim();
  for (let i = 0; i < lines.length - 1; i++) {
    if (BATCH_HEAD.test(lines[i]) && /FAILED\s*$/.test(lines[i])) {
      return lines[i + 1].trim();
    }
  }
  const first = lines.find((l) => !BATCH_HEAD.test(l)) || lines[0];
  return first.replace(/^Observation:\s*/, "").trim();
}

/** `⚡ agent` 도구 호출의 대상 칩 — roster 에 있는 **상주** 에이전트일 때만.
 * 일회성 위임(`run`)은 대상이 채널이 아니라 이 카드 안의 중첩 블록이라
 * 갈 곳이 없다(docs/chat-ui §3). 없으면 null → 꼬리 칸 자체가 안 생긴다. */
// ── 발신 도구 (v9.23.0) ──────────────────────────────────
// `message`/`reply`/`ask` 는 왕래다 — 아이콘 `→`, 꼬리에 상대.
// 종전엔 같은 발신이 `⚡ message {…}` 도구 줄 **과** `→ 보냄` 왕래 줄로
// 두 번 그려졌다. 도구 줄 하나가 그 자리를 맡고, 배달 결과는 스텝 배지
// (✓/✗) 가 이미 말한다 — 거부만 펼쳐서 사유가 보인다.
const SEND_TOOLS = { message: 1, reply: 1, ask: 1 };
function actIcon(tool) {
  return SEND_TOOLS[tool] ? "→" : "⚡";
}
/** 상대(또는 요청 id)를 글자로만 — 점프 없음. 반쪽만 눌리는 버튼(peer 만
 * 되고 main·사람은 안 되던)은 고장으로 읽혀 걷어냈다. */
function sendPeerChip(tool, inputStr) {
  if (!SEND_TOOLS[tool]) return null;
  let p = {};
  try {
    p = JSON.parse(inputStr || "{}");
  } catch (_e) {
    return null;
  }
  let label = "";
  if (tool === "message") label = peerLabel(String(p.to || ""));
  else if (tool === "ask") label = "user (human)"; // 질문은 사람에게만 (v10.77.0)
  else if (tool === "reply") label = String(p.id || "") || "requester";
  return label ? el("span", ["peer"], label) : null;
}
/** 주소 → 사람이 읽을 이름. `author` 네임스페이스가 이미 셋을 구분한다
 * (`main` / `user:<닉>` / `agent:<key>` 또는 bare key). 렌더 시점에
 * 로스터로 해소한다 — replay 에서 agent_msg 가 roster 보다 먼저 와도 된다. */
export function peerLabel(who) {
  var w = String(who || "");
  if (!w || w === "main") return "main";
  if (w === "user") return "user (human)";
  if (w.indexOf("user:") === 0) return w.slice(5) + " (human)";
  var k = w.indexOf("agent:") === 0 ? w.slice(6) : w;
  return ovAgentLabel(k);
}

function agentJumpChip(inputStr) {
  let p = {};
  try {
    p = JSON.parse(inputStr || "{}");
  } catch (_e) {
    return null;
  }
  const key = p && typeof p.key === "string" ? p.key : "";
  if (!key || !ovRoster.some((t) => t.key === key)) return null;
  const chip = el("span", ["peer", "can-jump"], ovAgentLabel(key));
  chip.title = "Go to " + ovAgentLabel(key);
  chip.addEventListener("click", function (e) {
    e.stopPropagation();
    ovJump(key);
  });
  return chip;
}

// ── 스텝 카드 (docs/chat-ui — 사고·행동·관찰을 한 장으로) ──────
//
// 종전엔 한 스텝이 카드 **둘**로 흩어졌다(assistant: 💭+⚡ / observation).
// 타임라인이 길어지면 어느 관찰이 어느 행동의 것인지 눈으로 다시 묶어야
// 했다. 이제 한 장으로 접는다:
//
//   접힘:  머리 한 줄 + 오른쪽 배지( ⚡도구 · ✓/✗ )
//   펼침:  행동 상세 + 관찰
//
// **머리 = 그 스텝이 가진 것 중 가장 요약적인 것** — 생각이 있으면 생각,
// 없으면 행동. 살아 있는 두 wire format 모두 생각이 선택이라(행동 앞 산문)
// 생각 없는 턴이 실측 1/3이고, 빈 자리에 "(생각 없음)" 을 그리면 줄만
// 쓰고 아무것도 안 알려준다.
//
// 배지를 남기는 이유: 접힘의 목적은 "안 읽고 지나가기" 가 아니라 **"읽을
// 곳을 고르기"** 다. 도구 이름과 성패는 그 선택에 쓰이는 정보라, 같은 줄
// 오른쪽 끝에 두면 줄 수를 안 늘리고 실패한 스텝이 목록에서 튀어나온다.

// **스코프별** 관찰을 기다리는 스텝 카드 한 장. DOM 을 뒤지지 않는 이유는
// 카드가 루트가 아니라 task group 본문에 들어갈 수 있어서다(appendToTimeline).
//
// ★ 열쇠는 채널이 아니라 **스코프**다(`stepKey`). 턴 번호는 루프마다 1 부터라
// main 의 턴 1(`⚡ agent`)이 열려 있는 동안 그 서브에이전트의 턴 1 이 오면
// 채널 열쇠로는 "같은 턴의 두 번째 op" 로 보인다 — 서브에이전트의 스텝이 main
// 의 카드 안으로 들어가고, 자기 카드에는 최종답만 남고, main 의 `agent` 관찰은
// 짝을 잃어 따로 떨어졌다(라이브만 — resume 재생은 순서가 달라 멀쩡했다).
//
// ★ 병합 단위는 **턴**이지 "직전 행동" 이 아니다. 서버는 `action()` 을
// op 마다 부르므로 다중 op 턴은 `assistant_turn` 을 **N개** 내고 관찰은
// 합쳐서 **하나** 낸다(`[1/2] shell — OK …`). "직전 행동" 으로 붙이면 마지막
// op 만 관찰을 받고 **앞 카드들은 성패 배지 없이 영영 남는다** — 실제로 그
// 회귀를 냈다(사용자 제보, 한 세션의 다중 op 턴 30건). 같은 턴의 두 번째
// 행동은 새 카드가 아니라 **그 카드의 본문**으로 들어간다.
const pendingStep = {};
function stepKey(d) {
  return d.task_id ? "scope:" + d.task_id : "main";
}

/** 스텝 카드의 머리 행 — 통째로 눌러 본문을 여닫는다. */
function makeStepHead(icon, kind, summary, badges, cls, tailNode) {
  const row = makeRow(icon, kind, summary, null, cls, tailNode || badges);
  row.classList.add("can", "step-head");
  const mark = row.querySelector(".x");
  if (mark) mark.textContent = "▸";
  return row;
}

export function renderAssistantTurn(d) {
  const card = el("div", ["card", "card-assistant"]);
  if (d.hidx != null) card.dataset.hidx = String(d.hidx);
  const t = d.thought ? String(d.thought).trim() : "";
  const first = t ? t.split("\n").find((l) => l.trim()) || t : "";
  const thoughtBody = t && t !== first.trim()
    ? elHtml("div", ["md"], escapeAndFormat(t))
    : null;

  if (d.final !== undefined) {
    // 최종 답변은 **접지 않는다** — 읽히려고 있는 것이고, 접으면 대화가
    // 아니라 로그가 된다. 묶음 카드도 아니다(뒤따를 관찰이 없다).
    if (t) {
      // 생각은 **펼친 채로** 둔다(v10.19.0) — 모델이 할 말을 생각 칸에 쓰고
      // 최종답에는 한 단어만 내는 일이 있고(실측 `out`), 접혀 있으면 빈 답을
      // 받은 것으로 보인다. 답보다 흐리게 그려 답이 먼저 읽히게 한다.
      // 한 줄짜리 생각은 머리 줄이 전부다(긴 요약은 줄바꿈된다) — 펼칠 게 없다.
      const think = makeRow("💭", "Thinking", first.trim(), thoughtBody, ["think", "final-think"]);
      if (thoughtBody) {
        thoughtBody.classList.remove("hide");
        think.classList.add("open");
        think.querySelector(".x").textContent = "▾";
      }
      card.appendChild(think);
    }
    // 이 카드가 `complete` 임을 **표식**으로 (v10.47.1): 종전엔 본문과 각주만
    // 있어 바로 위 액션 카드(monitor 등)에 이어진 산문처럼 읽혔다(보드 방 실측).
    card.appendChild(el("div", ["final-tag"], "✔ complete"));
    card.appendChild(elHtml("div", ["final"], escapeAndFormat(d.final)));
    card._md = function () { return (t ? "💭 " + t + "\n\n" : "") + String(d.final); };
    // 이 답이 **무엇에 대한 답인지** (v9.19.0). 합쳐진 런에서는 카드 위치가
    // 알려주지 않는다 — 요청 둘이 한 턴에 들어오고 최종답은 하나다.
    // `d.requests` 는 모델이 `complete` 의 `answers` 로 주장한 요청들이고,
    // 라이브·resume 양쪽에서 같은 모양으로 온다.
    if (Array.isArray(d.requests) && d.requests.length) {
      const box = el("div", ["final-answers"]);
      box.appendChild(el("span", ["fa-label"], "Answered request"));
      d.requests.forEach(function (r) {
        const chip = el("span", ["fa-chip"]);
        chip.appendChild(el("span", ["fa-id"], "[" + (r.id || "?") + "]"));
        if (r.author) chip.appendChild(el("span", ["fa-who"], r.author));
        chip.appendChild(el("span", ["fa-text"], r.text || ""));
        chip.title = (r.author ? r.author + ": " : "") + (r.text || "");
        box.appendChild(chip);
      });
      card.appendChild(box);
    }
    delete pendingStep[stepKey(d)];
    finishCard(card, d);
    return;
  }

  if (!d.action) {
    // 생각만 있는 턴 — 묶을 것이 없다.
    if (t) {
      card.appendChild(
        makeRow("💭", "Thinking", first.trim(), thoughtBody, ["think"])
      );
    }
    card._md = function () { return t ? "💭 " + t : ""; };
    delete pendingStep[stepKey(d)];
    finishCard(card, d);
    return;
  }

  const tool = d.action.tool_name || "";
  const input = d.action.tool_input || "";
  // main → agent 점프의 출발점. main 쪽에서 상주 에이전트에게 거는 일은
  // **왕래 줄이 아니라 `⚡ agent` 도구 호출**로 나타나므로(docs/chat-ui §5),
  // 상대 칩을 여기 붙인다.
  const jump = tool === "agent" ? agentJumpChip(input) : sendPeerChip(tool, input);
  const actCls = SEND_TOOLS[tool] ? ["act", "send"] : ["act"];

  // 같은 턴의 두 번째 op — 새 카드가 아니라 앞 카드에 행을 더한다.
  const openStep = pendingStep[stepKey(d)];
  if (openStep && openStep.turn === d.turn) {
    openStep.body.appendChild(
      makeRow(actIcon(tool), tool, actionSummary(tool, input),
              renderActionInput(tool, input), actCls, jump)
    );
    openStep.acts += 1;
    openStep.md.actions.push({ tool: tool, input: input });
    // 배지는 `⚡ 첫도구 +N` 으로 접는다 — 도구 이름을 전부 늘어놓으면
    // 머리 한 줄이 배지로 밀린다.
    if (openStep.toolBadge) {
      openStep.toolBadge.textContent =
        "⚡ " + openStep.firstTool + " +" + (openStep.acts - 1);
    }
    scheduleScroll();
    return;
  }

  card.classList.add("step");
  const badges = el("span", ["badges"]);
  const body = el("div", ["step-body"]);
  body.hidden = true;

  let head;
  if (t) {
    head = makeStepHead("💭", "Thinking", first.trim(), badges, ["think"]);
    // 도구 이름은 머리의 `k` 칸에 없으므로 배지로 알린다.
    badges.appendChild(el("span", ["badge", "tool"], "⚡ " + (tool || "?")));
    if (thoughtBody) body.appendChild(thoughtBody);
    const actRow = makeRow(actIcon(tool), tool, actionSummary(tool, input),
                           renderActionInput(tool, input), actCls, jump);
    body.appendChild(actRow);
  } else {
    // 생각이 없으면 행동이 머리로 올라온다 — 도구 이름이 이미 `k` 칸에
    // 있으므로 배지에서는 빼고 성패만 남긴다(같은 사실을 두 번 말하지 않는다).
    head = makeStepHead(actIcon(tool), tool, actionSummary(tool, input), badges,
                        actCls, jump || undefined);
    if (jump) head.appendChild(badges);
    const detail = renderActionInput(tool, input);
    if (detail) {
      detail.classList.add("row-body");
      body.appendChild(detail);
    }
  }
  head.addEventListener("click", function (e) {
    if (e.target.closest(".step-body") || e.target.closest("a, button")) return;
    const open = body.hidden;
    body.hidden = !open;
    head.classList.toggle("open", open);
    card.classList.toggle("open", open);
    const mark = head.querySelector(".x");
    if (mark) mark.textContent = open ? "▾" : "▸";
  });
  card.appendChild(head);
  card.appendChild(body);
  // 관찰이 이 카드를 찾아 붙는다. 못 찾으면 종전대로 단독 카드로 떨어진다
  // (안전망 — 구조적으로는 관찰이 행동 없이 오지 않는다).
  // 복사용 원문 — 행동은 같은 턴의 op 마다, 관찰은 붙을 때 쌓인다.
  const md = { thought: t, actions: [{ tool: tool, input: input }], obs: [] };
  card._md = function () { return stepMarkdown(md); };
  pendingStep[stepKey(d)] = {
    card: card, head: head, body: body, badges: badges,
    turn: d.turn, acts: 1, firstTool: tool || "?",
    toolBadge: badges.querySelector(".badge.tool"),
    md: md,
  };
  finishCard(card, d);
}

/** 스텝 카드의 마크다운: 💭 생각 → ⚡ 도구 + 입력(JSON) → ✓/✗ 도구 + 관찰 원문. */
function stepMarkdown(md) {
  const parts = [];
  if (md.thought) parts.push("💭 " + md.thought);
  md.actions.forEach(function (a) {
    let input = a.input;
    try { input = JSON.stringify(JSON.parse(a.input), null, 2); } catch (_e) {}
    parts.push("⚡ `" + (a.tool || "?") + "`" + (input ? "\n" + fence(input, "json") : ""));
  });
  md.obs.forEach(function (o) {
    parts.push((o.success ? "✓" : "✗") + " `" + (o.tool || "result") + "`" + (o.content ? "\n" + fence(o.content) : ""));
  });
  return parts.join("\n\n");
}

/** Render the action_input portion of an assistant_turn card.
 *
 * Known tool names get a custom layout (ask → numbered question list,
 * shell → ``$ <cmd>``, read_file → path + flags, edit_file →
 * path + edit count, agent → mode/task 요약). Unknown tools fall
 * back to pretty-printed JSON. Always escapes user-supplied text.
 */
function renderActionInput(toolName, toolInputStr) {
  let parsed;
  try {
    parsed = JSON.parse(toolInputStr);
  } catch (_e) {
    // tool_input wasn't valid JSON (e.g. parser returned a string).
    // Show it verbatim so the user can still inspect what happened.
    return el("pre", ["args"], toolInputStr);
  }

  if (toolName === "ask" && Array.isArray(parsed.questions)) {
    const ol = el("ol", ["action-ask"]);
    parsed.questions.forEach(function (q) {
      const li = document.createElement("li");
      li.textContent = String(q);
      ol.appendChild(li);
    });
    return ol;
  }

  if (toolName === "shell" && typeof parsed.command === "string") {
    return el(
      "pre",
      ["action-shell"],
      "$ " + parsed.command
    );
  }

  if (toolName === "read_file" && typeof parsed.path === "string") {
    const parts = [escapeHtml(parsed.path)];
    if (parsed.stat) parts.push('<span class="muted">(stat)</span>');
    if (parsed.search) {
      parts.push(
        '<span class="muted">search:</span> ' + escapeHtml(parsed.search)
      );
    }
    if (parsed.line_start) {
      parts.push(
        '<span class="muted">lines:</span> ' +
          parsed.line_start +
          "-" +
          (parsed.line_end || "?")
      );
    }
    return elHtml("div", ["action-detail"], parts.join(" "));
  }

  if (toolName === "edit_file" && typeof parsed.path === "string") {
    // edit_file is flat-native: one op = {path, op, pos, end?, lines?} — no
    // `edits` array (that always read 0 → "(0 edits)"). Show the op + target
    // ref instead, e.g. "app.py (replace 2#KT)". Legacy/batch `edits[]` (if it
    // ever returns) still falls back to a count.
    let detail;
    if (Array.isArray(parsed.edits)) {
      const n = parsed.edits.length;
      detail = "(" + n + " edit" + (n === 1 ? "" : "s") + ")";
    } else if (parsed.op) {
      const ref = parsed.end
        ? parsed.pos + ".." + parsed.end
        : parsed.pos || "";
      detail = "(" + parsed.op + (ref ? " " + ref : "") + ")";
    } else {
      detail = "";
    }
    return elHtml(
      "div",
      ["action-detail"],
      escapeHtml(parsed.path) +
        (detail ? ' <span class="muted">' + escapeHtml(detail) + "</span>" : "")
    );
  }

  if (toolName === "agent") {
    // 배치형 {tasks:[...]} — run fan-out 을 task 목록으로.
    if (Array.isArray(parsed.tasks)) {
      const ul = el("ul", ["action-delegate"]);
      parsed.tasks.forEach(function (t) {
        const li = document.createElement("li");
        li.textContent = String(t.task || "");
        if (t.agent || t.profile) {
          const prof = el(
            "span",
            ["muted"],
            " → " + String(t.agent || t.profile)
          );
          li.appendChild(prof);
        }
        ul.appendChild(li);
      });
      return ul;
    }
    // 플랫 op — "run: <task>" / "request agt-x: <message>" 한 줄 요약.
    if (typeof parsed.mode === "string") {
      const target = parsed.key || parsed.profile || "";
      const body = parsed.task || parsed.message || "";
      return elHtml(
        "div",
        ["action-detail"],
        escapeHtml(parsed.mode + (target ? " " + target : "")) +
          (body
            ? ' <span class="muted">' + escapeHtml(String(body)) + "</span>"
            : "")
      );
    }
  }

  if (toolName === "complete" && typeof parsed.result === "string") {
    // Should not normally hit (complete renders as ``final``) but
    // act gracefully if the model emits an explicit complete action.
    return elHtml("div", ["final"], escapeAndFormat(parsed.result));
  }

  // Fallback: pretty JSON. Two-space indent keeps wide objects readable
  // without burning horizontal real estate.
  return el(
    "pre",
    ["args"],
    JSON.stringify(parsed, null, 2)
  );
}

export function renderObservation(d) {
  const card = el("div", ["card", "card-observation"]);
  card.classList.add(d.success ? "ok" : "fail");
  if (d.hidx != null) card.dataset.hidx = String(d.hidx);
  const content = d.content || "";
  const tool = d.tool_name || "";
  // An `agent` observation is a subagent's prose answer (run 결과의
  // STATUS/RESULT/[Task N]/[Duration] wrapper, 상주 회신 배달), so
  // render it through the markdown pipeline like an assistant turn. Every
  // other tool's output (read_file hashlines, shell text, write/edit diffs)
  // is monospace/structured → keep the <pre> + diff colouring.
  let body =
    tool === "agent"
      ? elHtml("div", ["obs-body", "obs-md"], escapeAndFormat(content))
      : elHtml("pre", ["obs-body"], colorizeDiffBody(escapeHtml(content)));
  // 이미지가 딸린 관찰 (v10.60.0, read_file 의 PNG 등): 본문 아래 썸네일.
  // `makeRow` 가 접힘 클래스를 바깥 상자에 달므로 본문과 썸네일을 한 상자에.
  if (Array.isArray(d.images) && d.images.length) {
    const wrap = el("div", ["obs-images"]);
    wrap.appendChild(body);
    wrap.appendChild(thumbStrip(d.images));
    body = wrap;
  }
  // 실패도 **접어 둔다**(v10.19.0) — 펼쳐진 실패가 화면을 차지해 바로 옆의
  // 성공한 스텝을 가렸다(실측: 파일을 만든 카드를 못 찾음). 접힌 줄이 원인을
  // 말한다: 요약 칸이 원인 줄이고(`obsSummary`), 스텝 카드는 머리 아래에
  // `✗ 도구 · 원인` 한 줄을 단다.
  // 종류 칸은 **도구 이름** — "결과"라고만 쓰면 어느 도구의 결과인지 알 수
  // 없다(브라우저 TC 가 잡은 실수). 바로 위 ⚡ 행과 같은 이름이 서로를
  // 가리키므로 호출↔결과 짝이 눈으로 붙는다.
  const row = makeRow(
    d.success ? "✓" : "✗", tool || "result", obsSummary(content, !d.success), body,
    [d.success ? "ok" : "bad"]
  );
  // 자기 행동이 만든 **스텝 카드**에 붙는다 — 한 스텝은 한 장이다.
  const step = pendingStep[stepKey(d)];
  // 턴이 다르면 남의 카드다 — 개입 턴이 `state.turn` 을 되돌려 번호가
  // 재사용될 수 있고, 블로킹 `ask` 는 관찰 없이 카드를 열어 둔 채 남는다.
  if (step && step.turn === d.turn) {
    delete pendingStep[stepKey(d)];
    step.md.obs.push({ tool: tool, content: content, success: !!d.success });
    step.body.appendChild(row);
    step.badges.appendChild(
      el("span", ["badge", d.success ? "ok" : "bad"], d.success ? "✓" : "✗")
    );
    if (!d.success) {
      // 카드는 접힌 채 — 빨간 표시와 원인 한 줄만 낸다. 펼치면 관찰 본문이
      // 바로 보이게 그 행은 열어 둔다(실패를 보러 연 것이므로).
      step.card.classList.add("failed");
      body.classList.remove("hide");
      row.classList.add("open");
      const mark = row.querySelector(".x");
      if (mark) mark.textContent = "▾";
      const why = el("div", ["step-why"]);
      why.appendChild(el("span", ["why-ic"], "✗"));
      why.appendChild(el("span", ["why-tool"], tool || "result"));
      why.appendChild(el("span", ["why-text"], obsSummary(content, true)));
      why.addEventListener("click", function () { step.head.click(); });
      step.card.insertBefore(why, step.body);
    }
    scheduleScroll();
    return;
  }

  // 안전망: 붙일 스텝을 못 찾았다. 조용히 버리면 결과가 **사라지므로**
  // 종전대로 단독 카드로 떨어뜨린다 — 최악이 "안 묶임" 이지 "유실" 이 아니다.
  card.appendChild(row);
  card._md = function () {
    return (d.success ? "✓" : "✗") + " `" + (tool || "result") + "`" + (content ? "\n" + fence(content) : "");
  };
  finishCard(card, d);
}

export function renderError(d) {
  const card = el("div", ["card", "card-error"]);
  card.textContent = d.content;
  finishCard(card, d);
}
