// channels.js — 채널·트레이 상태 + 채널 필터 + 점프. 에이전트 아이콘·별명 해시.
// 순환 묶음의 진입점이다: app.js 가 순환 멤버 중 이것을 먼저 import 하므로
// 순환의 다른 모듈이 모두 평가된 뒤 마지막으로 평가된다 — top-level 에서
// 다른 모듈의 함수를 부르는 곳(채널 바 IIFE)이 이 모듈에만 있다(DESIGN §8.4).
// ovActiveChannel·ovBack 은 이 모듈만 대입한다 — 바깥은 setter 를 부른다.
// (v10.117.0, docs/web-modules/DESIGN.md §8 — 옛 main.js 의 절 [28]·[30]·[31])

import { $messages, el, elHtml } from "./dom.js";
import { session } from "./state.js";
import { escapeHtml } from "./markdown.js";
import { pin, setScrollTop, unpin } from "./scroll.js";
import { scopeAncestors, taskGroups } from "./timeline.js";
import { ctxReveal, frameShowChannel } from "./frames.js";
import { attachMessage } from "./attach.js";
import {
  ALREADY_CLOSED_NOTICE,
  buildPromptMetaEl,
  clearAnswerNote,
  highlightDangerHtml,
  noteOnAnswer,
} from "./input.js";
import { sendOnce } from "./send.js";
import {
  ovApplyChannelInput,
  ovCloseDoneMenu,
  ovShowAllDone,
  ovSyncChannels,
  ovToggleDoneMenu,
} from "./runs.js";

// ── 채널·트레이 상태 (구 개요의 잔존분) ─────────────────
// ② 가 개요 렌더를 지웠을 때 그 **데이터**(ovEntries/ovTopScopes/ovScopeSrc/
// ovSkills)는 선언만 남아 아무도 읽지 않았다 — ⑥ 에서 함께 걷어낸다. 남는
// 것은 실제로 읽히는 둘: roster(채널 칩·점프 가능 판정의 진실원)와 활성 채널.
export var ovRoster = []; // agent_roster
// 사람에게 온 열린 질문 — main·에이전트·스킬 런의 것이 한 목록(6b, sticky ``open_questions``). ❓ 트레이의 출처다.
var ovQuestions = [];
// 사람에게 건 열린 확인 — 도구 호출 하나가 그 자리에서 기다린다(6d, sticky ``open_confirms``). 트레이 맨 위에 선다.
var ovConfirms = [];
// 일하는 에이전트인가 — 런 중이거나 일을 끝내지 않은 run (서버 `AgentRegistry.working_agents` 와 같은 판정).
// 입력창 옆 Stop ▾ 와 채널 줄 끝 ■ all 이 이것을 본다 (v10.142.0).
export function ovAgentWorking(t) {
  return t.state === "busy" || (!!t.ends_after_task && t.state !== "dead");
}
// 대화 채널. "main" = 메인, 그 외 = 상주 agent key. 채널은 **타임라인의
// 필터**다(v9.4.0 ⑥) — 별도 스트림을 들고 있지 않으므로 리로드하면
// replay 버퍼가 그대로 복원한다.
export var ovActiveChannel = "main";
// 3단계: 글로벌 ask 트레이 — **주소가 사람인 열린 질문**을 채널 무관하게
// 노출한다. 진실원은 열린 질문 목록(`ovQuestions`) 하나다(질문 단위, 6b).
// 에이전트별 결정적 아이콘 — key 해시로 고정. 서버(agent_cli/agent_icon.py)와
// 반드시 동일한 풀·순서·해시(test_app_markdown 이 대조). key 는 ASCII 라
// charCodeAt == Python ord.
export var OV_AGENT_ICONS = [
  "🦊", "🐙", "🦉", "🦄", "🐳", "🦋", "🐢", "🐝",
  "🦁", "🐧", "🦩", "🐬", "🦇", "🐡", "🦕", "🐌",
  "🦔", "🦦", "🐨", "🐼", "🦭", "🦡", "🐺", "🐸",
];
// 아이콘과 **같은 인덱스**의 동물 + 독립 해시의 수식어 → "날쌘 여우".
// 별명은 **표시 전용**이다 — 주소는 언제나 key(`@agt-<key>`)라, 화면은 늘
// key 를 흐리게 곁에 단다(`ovAgentLabelHtml`). 별명을 주소로 받기 시작하면
// 유일성·경로·rename 이 따라오는데 key 가 uuid 라 그 셋이 이미 해결돼 있다.
export var OV_AGENT_ANIMALS = [
  "Fox", "Octopus", "Owl", "Unicorn", "Whale", "Butterfly", "Turtle", "Bee",
  "Lion", "Penguin", "Flamingo", "Dolphin", "Bat", "Pufferfish", "Dinosaur", "Snail",
  "Hedgehog", "Otter", "Koala", "Panda", "Seal", "Badger", "Wolf", "Frog",
];
var OV_AGENT_ADJECTIVES = [
  "Swift", "Easygoing", "Meticulous", "Brave", "Quiet", "Quirky", "Diligent", "Clever",
  "Steady", "Nimble", "Careful", "Kind", "Reliable", "Busy", "Calm", "Cheerful",
];
function ovAgentHash(key) {
  var s = 0;
  for (var i = 0; i < key.length; i++) s += key.charCodeAt(i);
  return s;
}
export function ovAgentIcon(key) {
  if (!key) return OV_AGENT_ICONS[0];
  return OV_AGENT_ICONS[ovAgentHash(key) % OV_AGENT_ICONS.length];
}
export function ovAgentNickname(key) {
  var h = ovAgentHash(key);
  return (
    OV_AGENT_ADJECTIVES[
      Math.floor(h / OV_AGENT_ANIMALS.length) % OV_AGENT_ADJECTIVES.length
    ] + " " + OV_AGENT_ANIMALS[h % OV_AGENT_ANIMALS.length]
  );
}
// 화면에 쓸 **이름** — 구체적인 것부터: 인스턴스 이름 > 프로파일 > 별명.
// (서버 `agent_icon.agent_display_name` 과 같은 우선순위 — 한 에이전트가
// CLI 와 웹에서 다른 이름으로 불리면 안 된다.)
function ovAgentDisplayName(key) {
  var tm = ovRoster.filter(function (t) { return t.key === key; })[0];
  var profile = (tm && tm.profile) || "";
  var name = (tm && tm.name) || "";
  if (name && profile) return profile + " · " + name;
  return name || profile || ovAgentNickname(key);
}
// 채널 표시명(**평문**) — title/placeholder 등 마크업을 못 쓰는 자리용.
export function ovAgentLabel(key) {
  return ovAgentIcon(key) + " " + ovAgentDisplayName(key);
}
// 이름 + **흐린 key** — 자리가 있는 곳(채널 칩·ask 트레이)에서 쓴다.
// 사람은 이름으로 기억하고, `@agt-…` 로 부를 때 필요한 주소가 바로 옆에 있다.
export function ovAgentLabelHtml(key) {
  return (
    escapeHtml(ovAgentLabel(key)) +
    ' <span class="ag-key">' + escapeHtml(key) + "</span>"
  );
}

// 사용자 입력 = 독립된 평문 줄(그룹핑 없음).
// 응답 = 독립된 블록(항상 done — complete 시 한 번에 append). 짝짓기·귀속 없음.
// 활동 스트립: 실행 중 도구 호출 축약 한 줄(왼쪽=누적 카운트, 오른쪽=현재 배치 칩).
export function ovOnRoster(d) {
  ovRoster = (d && d.roster) || [];
  ovSyncChannels(); // 채널 바(상태 dot 포함) 갱신(죽으면 main 복귀)
  ovRenderAskTray(); // 이름표가 명단을 따른다
}
// 열린 질문 목록이 바뀌었다(6b) — 질문이 열리고, 답·닫힘·묻는 쪽의 끝으로 닫힐 때마다.
export function ovOnQuestions(d) {
  ovQuestions = (d && d.questions) || [];
  ovRenderAskTray();
}
// 열린 확인 목록이 바뀌었다(6d) — 확인이 열리고, 답·기다리던 호출의 정지로 닫힐 때마다.
export function ovOnConfirms(d) {
  ovConfirms = (d && d.confirms) || [];
  ovRenderAskTray();
}
// 글로벌 ask 트레이: 열린 확인·질문 목록을 채널 무관하게 렌더. 답·닫기는 그 번호의 길(api/confirms/<c-N>/…,
// api/questions/<q-N>/…) — 서버 공유 상태라 누가 먼저 답하면 항목이 닫혀 → 모든 뷰어에서 사라짐(선착순).
// 확인 항목 하나(6d) — 확인은 id 로 불변이라 한 번 만든다. 이름표만 명단을 따라 바뀐다(`ovLabelQuestionEl`).
// 답은 버튼뿐이다 — 기본값이 없고 말 칸의 Enter 는 아무것도 보내지 않는다(명시적 UI 로만).
function ovBuildConfirmEl(c) {
  var item = el("div", ["ask-item", "ask-confirm"]);
  item.setAttribute("data-key", c.asker || "");
  item.setAttribute("data-cid", c.id);
  item.sendSlot = { sending: false };
  var head = el("div", ["ask-q"]);
  head.appendChild(document.createTextNode("⚠ "));
  head.appendChild(el("b"));
  head.appendChild(document.createTextNode(" — confirmation " + c.id));
  item.appendChild(head);
  var meta = buildPromptMetaEl(c);
  if (meta) item.appendChild(meta);
  // **무엇을 승인하는지**를 먼저 보인다 — 머리 한 줄과 자세히(경로 목록·반복 호출). 명령은 따로 강조해 그린다.
  var why = [c.title || "", c.detail || ""].filter(Boolean).join("\n");
  if (why) item.appendChild(el("div", ["confirm-why"], why));
  if (typeof c.command === "string" && c.command) {
    item.appendChild(
      elHtml("pre", ["action-shell", "confirm-cmd"],
        highlightDangerHtml(c.command, c.danger_spans))
    );
  }
  var btns = el("div", ["ask-confirm-btns"]);
  (c.choices || []).forEach(function (ch) {
    var b = el("button", ["confirm-btn"], ch.label);
    b.type = "button";
    b.setAttribute("data-action", ch.action);
    btns.appendChild(b);
  });
  item.appendChild(btns);
  var cw = el("div", ["ask-in"]);
  var cmt = el("input", ["ask-answer"]);
  cmt.type = "text";
  cmt.placeholder = "Optional comment";
  cw.appendChild(cmt);
  item.appendChild(cw);
  return item;
}
// 에이전트 질문 항목 하나 — 질문은 id 로 불변이라 한 번 만든다. 이름표만 명단을 따라 바뀐다
// (`ovLabelQuestionEl`). 보내는 중 상태는 항목마다 하나.
function ovBuildQuestionEl(key, q) {
  var item = el("div", ["ask-item"]);
  item.setAttribute("data-key", key);
  item.setAttribute("data-qid", q.id);
  item.sendSlot = { sending: false };
  var head = el("div", ["ask-q"]);
  head.appendChild(document.createTextNode("❓ "));
  head.appendChild(el("b"));
  head.appendChild(document.createTextNode(" asked"));
  item.appendChild(head);
  item.appendChild(el("div", ["ask-qt"], q.text || ""));
  var iw = el("div", ["ask-in"]);
  var inp = el("input", ["ask-answer"]);
  inp.type = "text";
  inp.placeholder = "Answer…";
  inp.setAttribute("aria-label", "Answer");
  var send = el("button", ["ask-send", "btn-primary"], "Send");
  send.type = "button";
  // 답 없이 닫기(6b) — 묻는 쪽은 "닫혔다" 는 사실의 봉투를 받는다
  var close = el("button", ["ask-close"], "Close without answer");
  close.type = "button";
  iw.appendChild(inp);
  iw.appendChild(send);
  iw.appendChild(close);
  item.appendChild(iw);
  return item;
}
// 질문 항목의 이름표 — 바뀐 때만 쓴다(입력은 건드리지 않는다). 에이전트는 지금 명단의 이름(이름이 바뀌면 따라간다),
// 명단에 없는 묻는 쪽(main·스킬 런)은 서버의 이름표(`asker_label` — "main"·"skill <이름>", 6b)를 그대로 쓴다.
function ovLabelQuestionEl(item, key, q) {
  var b = item.querySelector(".ask-q > b");
  var agent = ovRoster.some(function (t) { return t.key === key; });
  var html = agent ? ovAgentLabelHtml(key) : escapeHtml(q.label || key);
  if (b.innerHTML !== html) b.innerHTML = html;
}
// 트레이를 **항목 단위로** 맞춘다 (v10.147.4): 여전히 열린 질문의 항목은 그대로 두고(쓰던 답·포커스·
// 커서·사유 줄이 산다), 닫힌 질문의 항목은 빼고, 새 질문만 만든다. 순서는 main 의 물음이 맨 위, 그 뒤
// 명단 순서. 자리가 맞는 항목은 옮기지 않는다(옮기면 포커스가 풀린다). 종전엔 명단이 올 때마다
// innerHTML 로 통째로 다시 그려 쓰던 답이 지워졌다.
export function ovRenderAskTray() {
  var tray = document.getElementById("ask-tray");
  if (!tray) return;
  // 비동기 질문(docs/agent-ask/DESIGN.md §3.6)과 확인(6d): 주소는 언제나 사람이다. 상대의 state 로는 알 수
  // 없고 목록 자체가 진실이다 — 항목은 번호 단위.
  var haveC = {};
  var haveQ = {};
  Array.prototype.forEach.call(tray.children, function (c) {
    if (c.hasAttribute("data-cid")) haveC[c.getAttribute("data-cid")] = c;
    else if (c.hasAttribute("data-qid")) haveQ[c.getAttribute("data-qid")] = c;
  });
  // 확인이 맨 위 — 도구 호출이 그 자리에서 기다린다. 그 뒤 질문.
  var want = [];
  ovConfirms.forEach(function (c) {
    var item = haveC[c.id] || ovBuildConfirmEl(c);
    ovLabelQuestionEl(item, c.asker || "", c);
    want.push(item);
  });
  ovQuestions.forEach(function (q) {
    var item = haveQ[q.id] || ovBuildQuestionEl(q.asker, q);
    ovLabelQuestionEl(item, q.asker, q);
    want.push(item);
  });
  Array.prototype.slice.call(tray.children).forEach(function (c) {
    if (want.indexOf(c) < 0) c.remove();
  });
  want.forEach(function (item, i) {
    if (tray.children[i] !== item) tray.insertBefore(item, tray.children[i] || null);
  });
  tray.hidden = !want.length;
}
// 확인의 답(6d) — 확인 번호의 길(``api/confirms/<c-N>/<action>``)로 **그 확인의 답**으로 짝짓는다. 질문 항목과 같은
// 보내기: 받으면 목록이 항목을 거두고, 409(이미 답했거나 닫힌 확인)는 항목의 입력을 닫고 알린다.
function ovSubmitConfirm(item, action) {
  if (item.sendSlot.sending) {
    noteOnAnswer(item, "Still sending the previous answer");
    return;
  }
  clearAnswerNote(item);
  var cid = item.getAttribute("data-cid");
  var note = item.querySelector(".ask-answer").value.trim();
  sendOnce(
    item.sendSlot,
    function (signal) {
      return fetch("api/confirms/" + encodeURIComponent(cid) + "/" + action, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ conn_id: session.connId, note: note }),
        signal: signal,
      });
    },
    function (failure, status) {
      if (!failure) return;
      if (status === 409) {
        var inp = item.querySelector(".ask-answer");
        inp.value = "";
        inp.disabled = true;
        item.querySelectorAll(".confirm-btn").forEach(function (b) { b.disabled = true; });
        attachMessage(ALREADY_CLOSED_NOTICE);
        return;
      }
      if (!item.isConnected) return attachMessage(ALREADY_CLOSED_NOTICE);
      noteOnAnswer(item, "⚠ Not sent — " + failure);
    }
  );
}
// 질문의 답 — 질문 번호의 길(``api/questions/<q-N>/answer``, 6b)로 **그 질문의 답**으로 짝짓는다(묻는 쪽은 번호가 정한다).
// 입력창과 같은 규칙: 받았을 때만 비우고(항목은 목록이 닫는다), 실패면 항목 안에 사유·글 남김. 409 는
// 이미 답했거나 닫힌 질문(상태 코드로만 읽는다) — 글을 버리고 입력창 아래 줄에 알리며,
// 목록이 항목을 거둘 때까지 그 항목의 입력을 닫아 둔다.
function ovSubmitAsk(item) {
  var inp = item.querySelector(".ask-answer");
  var text = inp.value;
  if (!text.trim()) return;
  if (item.sendSlot.sending) {
    noteOnAnswer(item, "Still sending the previous answer");
    return;
  }
  clearAnswerNote(item);
  ovPostQuestion(item, "answer", { content: text }, function () {
    if (inp.value === text) inp.value = "";
  });
}
// 답 없이 닫기(6b) — 같은 길의 ``close``. 받으면 목록이 항목을 거둔다.
function ovCloseAsk(item) {
  if (item.sendSlot.sending) {
    noteOnAnswer(item, "Still sending the previous answer");
    return;
  }
  clearAnswerNote(item);
  ovPostQuestion(item, "close", {}, function () {});
}
// 질문 하나에 보내기 — 답·닫기 공용. 409(이미 답했거나 닫힌 질문)는 항목의 입력을 닫고 알린다.
function ovPostQuestion(item, action, extra, onDone) {
  var qid = item.getAttribute("data-qid");
  var body = Object.assign({ conn_id: session.connId }, extra);
  sendOnce(
    item.sendSlot,
    function (signal) {
      return fetch("api/questions/" + encodeURIComponent(qid) + "/" + action, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
        signal: signal,
      });
    },
    function (failure, status) {
      if (!failure) return onDone();
      var inp = item.querySelector(".ask-answer");
      if (status === 409) {
        inp.value = "";
        inp.placeholder = "Already answered or closed";
        inp.disabled = true;
        item.querySelector(".ask-send").disabled = true;
        item.querySelector(".ask-close").disabled = true;
        attachMessage(ALREADY_CLOSED_NOTICE);
        return;
      }
      // 보내는 사이 항목이 빠졌다(목록이 질문을 닫았다) — 보이지 않는 곳에 사유를 세우지 않는다
      if (!item.isConnected) return attachMessage(ALREADY_CLOSED_NOTICE);
      noteOnAnswer(item, "⚠ Not sent — " + failure);
    }
  );
}
(function () {
  var tray = document.getElementById("ask-tray");
  if (!tray) return;
  // 질문 항목(``data-qid``)의 보내기·닫기·Enter, 확인 항목(``data-cid``)의 버튼.
  function questionItemOf(target) {
    var item = target.closest(".ask-item");
    return item && item.hasAttribute("data-qid") ? item : null;
  }
  tray.addEventListener("click", function (e) {
    var btn = e.target.closest(".confirm-btn");
    var citem = btn && btn.closest(".ask-item[data-cid]");
    if (citem) return ovSubmitConfirm(citem, btn.getAttribute("data-action"));
    var item = questionItemOf(e.target);
    if (!item) return;
    if (e.target.classList.contains("ask-send")) ovSubmitAsk(item);
    else if (e.target.classList.contains("ask-close")) ovCloseAsk(item);
  });
  tray.addEventListener("keydown", function (e) {
    if (e.key !== "Enter" || e.isComposing || !e.target.classList.contains("ask-answer")) return;
    var item = questionItemOf(e.target);
    if (!item) return;
    e.preventDefault();
    ovSubmitAsk(item);
  });
})();
// ── 채널 = 타임라인 필터 (v9.4.0 ⑥) ──────────────────────
// `#messages` 의 **직계 자식**만 걸러 보여준다. `data-ch` 가 없는 노드는
// 어느 채널에서나 보인다(생성 중 한 줄 — 채널과 무관한 표시).
// 인자를 주면 그 노드 하나만(새로 append 된 카드), 없으면 전부 훑는다.
export function applyChannelFilter(one) {
  var nodes = one ? [one] : Array.prototype.slice.call($messages.children);
  nodes.forEach(function (n) {
    var ch = n.dataset ? n.dataset.ch : null;
    if (ch) n.hidden = ch !== ovActiveChannel;
  });
}

// 지금 채널에 맞춰 화면을 맞춘다 — 타임라인 필터와 대화 밖 고정 자리(맨 위 카드·
// 꼬리 띠·예산 줄). 채널 전환(ovSetChannel)과 보던 채널이 사라져 main 으로 되돌릴 때
// (runs.js ovSyncChannels) 둘 다 이것을 부른다.
export function syncChannelView() {
  applyChannelFilter();
  frameShowChannel(ovActiveChannel);
}

// 채널 전환. 표시(필터) · 입력 라우팅 · 칩 활성이 한 지점에서 움직인다.
//
// **전환하면 그 대화의 맨 아래로 간다.** 필터가 노드를 감추고 드러내면
// 문서 높이가 확 바뀌는데, 스크롤 위치는 그대로라 아무 데나 떨어져 있었다
// — 칩을 누를 때마다 "어딘지 모를 곳"으로 튀어 규칙을 읽을 수 없었다
// (사용자 보고). 대화를 열면 최신부터 보는 게 채팅의 규칙이고, 자동 따라가기도
// 함께 되살려 그 채널에 새 줄이 오면 이어서 따라간다.
// (점프는 이 뒤에 앵커로 다시 스크롤하므로 영향받지 않는다 — ovJump 참조.)
function ovSetChannel(key) {
  ovActiveChannel = key || "main";
  ovSyncChannels();
  syncChannelView();
  ovApplyChannelInput();
  pin();
}

// ── 점프 — 엿보기, 네비게이션이 아니다 (docs/chat-ui §5) ────
// 상태는 값 하나: 직전에 어디서 왔는지. **스택이 아니다** — 새 점프가 이전
// 것을 덮고, 한 번 누르면 사라진다. 스택이면 누를 때마다 라벨이 바뀌어
// 어디로 갈지 예측이 안 되는데, 채널 칩이 항상 보이므로 그 복잡도를 살
// 이유가 없다(사용자 지적으로 스택→단일 변경).
export var ovBack = null; // {ch, id} | null
export function ovRenderBack() {
  var btn = document.getElementById("ch-back");
  if (!btn) return;
  if (!ovBack) {
    btn.hidden = true;
    return;
  }
  btn.hidden = false;
  btn.textContent =
    "↩ Back to " + (ovBack.ch === "main" ? "💬 main" : ovAgentLabel(ovBack.ch));
}
/** 채널의 앵커 카드 = 그 채널의 마지막 카드 — 도착은 했음을 보이는 게
 * 아무것도 안 하는 것보다 낫다. (왕래 줄 앵커는 v9.23.0 에서 줄과 함께
 * 사라졌다 — 점프는 main 의 `⚡ agent` 도구 줄에서만 출발한다.) */
function ovChannelAnchor(key) {
  var cards = $messages.querySelectorAll(
    ':scope > [data-ch="' + cssEsc(key) + '"]'
  );
  return cards.length ? cards[cards.length - 1] : null;
}
export function cssEsc(s) {
  return String(s).replace(/["\\]/g, "\\$&");
}
export function ovJump(key) {
  if (!key || key === ovActiveChannel) return;
  ovBack = { ch: ovActiveChannel };
  ovSetChannel(key);
  ovRenderBack();
  var anchor = ovChannelAnchor(key);
  if (anchor) {
    expandAncestors(anchor.dataset.taskId || "");
    scrollTimelineTo(anchor);
  }
}
(function () {
  var btn = document.getElementById("ch-back");
  if (!btn) return;
  btn.addEventListener("click", function () {
    if (!ovBack) return;
    var to = ovBack.ch;
    ovBack = null; // 한 단계 — 돌아가면 사라진다
    ovSetChannel(to);
    ovRenderBack();
  });
})();
(function () {
  var bar = document.getElementById("ov-channels");
  if (!bar) return;
  bar.addEventListener("click", function (e) {
    if (e.target.closest && e.target.closest("#ov-stop-all")) {
      postStop("agents"); // ■ all — 모든 에이전트의 런 (main 은 계속)
      return;
    }
    if (e.target.closest && e.target.closest("#ov-done")) {
      e.stopPropagation(); // 바깥 클릭 닫기가 같은 클릭으로 다시 닫지 않게
      ovToggleDoneMenu(); // 끝난 run 목록 (v10.143.0)
      return;
    }
    var chip = e.target.closest ? e.target.closest(".ov-ch") : null;
    if (!chip) return;
    var key = chip.getAttribute("data-key");
    var ctl = e.target.closest(".ov-ch-ctl");
    if (ctl) {
      // 칩의 조작은 죽은 상주의 ↻ 하나다. 상한 없이 — 늦게라도 닿으면 되살린다. 거절(한도·이미 살아 있음)이나
      // 네트워크 오류면 입력창 아래에 사유 (v10.155.0; 종전엔 아무 표시 없이 끝났다)
      e.stopPropagation();
      if (ctl.disabled) return;
      ctl.disabled = true;
      var path = "api/agent/" + encodeURIComponent(key) + "/" + ctl.getAttribute("data-act");
      sendOnce(
        { sending: false },
        function (signal) { return fetch(path, { method: "POST", signal: signal }); },
        function (failure) {
          if (!failure) return; // 명단이 칩을 다시 그린다
          ctl.disabled = false;
          attachMessage("Not revived — " + failure);
        },
        { limit: false }
      );
      return;
    }
    ovSetChannel(key);
  });
  // 끝난 run 목록(`Done N ▾`, v10.143.0) — 항목은 칩처럼 그 채널을 연다. 바깥을 누르거나 Esc 면 닫힌다.
  var menu = document.getElementById("ov-done-menu");
  if (menu) {
    menu.addEventListener("click", function (e) {
      e.stopPropagation();
      if (e.target.closest(".ov-done-more")) return ovShowAllDone();
      var row = e.target.closest(".ov-done-row");
      if (!row) return;
      ovCloseDoneMenu();
      ovSetChannel(row.getAttribute("data-key"));
    });
    document.addEventListener("click", function () { ovCloseDoneMenu(); });
    document.addEventListener("keydown", function (e) {
      if (e.key === "Escape") ovCloseDoneMenu();
    });
  }
  ovSyncChannels(); // 로드 시 최소 main 칩 렌더(roster 오면 갱신)
  frameShowChannel(ovActiveChannel); // main 의 맨 위 카드·꼬리 띠
})();

// 정지 요청 하나 (v10.142.0) — ``/api/stop`` 의 범위: "main" · "agents" · "all". 결과는 SSE(worker_state·
// 명단)가 그린다. 상한이 없다 — 막힌 연결에서도 늦게라도 닿으면 멈춘다(사용자 결정 v10.155.0). 닿지 못했거나 서버가
// 거절하면 ``failed(사유)`` 를 한 번 부르고 입력창 아래에 ``Not stopped — <사유>`` 를 세운다. ``stopped: false``
// ("돌던 턴 없음")는 실패가 아니다.
export function postStop(scope, failed) {
  sendOnce(
    { sending: false },
    function (signal) {
      return fetch("api/stop", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ scope: scope }),
        signal: signal,
      });
    },
    function (failure) {
      if (!failure) return;
      if (failed) failed(failure);
      attachMessage("Not stopped — " + failure);
    },
    { limit: false }
  );
}

/** Scroll the TIMELINE CONTAINER to a card — never scrollIntoView, which
 * also scrolls every scrollable ancestor (incl. horizontally) and shoved
 * the whole team view sideways in the mockup. The container is the one
 * surface that should move. */
export function scrollTimelineTo(card) {
  ctxReveal(card); // 접힌 "컨텍스트 밖 대화" 안이면 먼저 편다
  // Off auto-follow so a live scrollToBottom() can't yank us back down.
  // (프로그램 스크롤로 표시 — 도착지가 바닥 근처여도 규칙 2 가 되붙이지 않는다.)
  unpin();
  const top =
    card.getBoundingClientRect().top -
    $messages.getBoundingClientRect().top +
    $messages.scrollTop -
    10;
  setScrollTop(Math.max(0, top));
  flashOnce(card);
}

/** 점프 도착지를 한 번 번쩍이고 **클래스를 뗀다**.
 *
 * 떼는 것이 핵심이다. `.tv-nav-hl` 은 CSS 애니메이션인데, 채널 전환이
 * `n.hidden` 으로 카드를 숨겼다 드러낸다(`applyChannelFilter`). 숨겨졌던
 * 요소가 다시 표시되면 **CSS 애니메이션은 처음부터 다시 재생된다** — 그래서
 * 클래스를 남겨두면 그 탭을 열 때마다 엉뚱하게 또 번쩍였다(사용자 보고).
 *
 * 전환이 빨라 `animationend` 가 안 오는 경우(애니메이션 중 숨겨지면 이벤트가
 * 안 뜬다)를 위해 타이머도 함께 건다. 같은 카드로 다시 점프하면 이전 타이머는
 * 취소한다 — 안 그러면 옛 타이머가 **새 번쩍임을 중간에 꺼버린다**.
 */
function flashOnce(card) {
  if (card.__flashTimer) clearTimeout(card.__flashTimer);
  if (card.__flashEnd) {
    card.removeEventListener("animationend", card.__flashEnd);
  }
  card.classList.remove("tv-nav-hl");
  void card.offsetWidth; // restart the animation
  card.classList.add("tv-nav-hl");

  const clear = function () {
    if (card.__flashTimer) clearTimeout(card.__flashTimer);
    if (card.__flashEnd) {
      card.removeEventListener("animationend", card.__flashEnd);
    }
    card.__flashTimer = null;
    card.__flashEnd = null;
    card.classList.remove("tv-nav-hl");
  };
  card.__flashEnd = function (e) {
    // 다른 애니메이션의 종료로 꺼지면 안 된다.
    if (e.animationName === "tv-nav-flash") clear();
  };
  card.addEventListener("animationend", card.__flashEnd);
  card.__flashTimer = setTimeout(clear, 2500);
}

/** Expand a scope card's collapsed ancestor chain, OUTERMOST first (jumping
 * before expanding would move the target away from the jump). */
function expandAncestors(tid) {
  scopeAncestors(tid)
    .slice()
    .reverse()
    .forEach(function (a) {
      const g = taskGroups[a];
      if (g && g.body.hidden) g.toggle();
      else if (!g) {
        // Group entry already released (scope finished) — the card's own
        // DOM is still there, so toggle it through the element.
        const anc = $messages.querySelector(
          '.card-task-group[data-task-id="' +
            (window.CSS && CSS.escape ? CSS.escape(a) : a.replace(/"/g, '\\"')) +
            '"]',
        );
        const b = anc && anc.querySelector(":scope > .task-body");
        if (b && b.hidden) {
          b.hidden = false;
          const ch = anc.querySelector(":scope > .task-header > .task-chevron");
          if (ch) ch.textContent = "▼";
        }
      }
    });
}

// ── 원문 레코드로 점프 (v10.119.0, 장부 P4) ─────────────────
// 📋 띠(ledger.js — 순환 밖 잎이라 이 모듈을 import 하지 않는다)가 행 클릭에
// `agentcli:jump` {hidx} 를 보낸다. 장부는 main 의 것이므로 main 채널의 카드만
// 찾는다 — 에이전트 채널·delegate 그룹 안 카드는 자기 history 의 서수라 같은 숫자가
// 다른 레코드다. 다른 채널에 있었으면 main 으로 옮기고 돌아가기 한 단계를 남긴다
// (ovJump 와 같은 엿보기).
function ovRecordCard(hidx) {
  var hits = $messages.querySelectorAll('[data-hidx="' + cssEsc(hidx) + '"]');
  for (var i = 0; i < hits.length; i++) {
    var top = hits[i];
    while (top.parentNode && top.parentNode !== $messages) top = top.parentNode;
    var ch = top.dataset ? top.dataset.ch : null;
    if ((!ch || ch === "main") && !hits[i].closest(".task-body")) return hits[i];
  }
  return null;
}
document.addEventListener("agentcli:jump", function (e) {
  var hidx = e.detail ? e.detail.hidx : null;
  if (hidx == null) return;
  var card = ovRecordCard(hidx);
  if (!card) return;
  if (ovActiveChannel !== "main") {
    ovBack = { ch: ovActiveChannel };
    ovSetChannel("main");
    ovRenderBack();
  }
  scrollTimelineTo(card);
});

// 모듈 경계의 setter — 이 모듈의 let/var 는 이 모듈만 대입한다(DESIGN §8.3).
export function setOvActiveChannel(v) { ovActiveChannel = v; }
export function setOvBack(v) { ovBack = v; }
