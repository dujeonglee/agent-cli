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
import {
  buildPromptMetaEl,
  highlightDangerHtml,
  ovSubmitMainConfirm,
  ovSubmitMainPrompt,
} from "./input.js";
import { ovApplyChannelInput, ovSyncChannels } from "./runs.js";

// ── 채널·트레이 상태 (구 개요의 잔존분) ─────────────────
// ② 가 개요 렌더를 지웠을 때 그 **데이터**(ovEntries/ovTopScopes/ovScopeSrc/
// ovSkills)는 선언만 남아 아무도 읽지 않았다 — ⑥ 에서 함께 걷어낸다. 남는
// 것은 실제로 읽히는 둘: roster(채널 칩·점프 가능 판정의 진실원)와 활성 채널.
export var ovRoster = []; // agent_roster
// 대화 채널. "main" = 메인, 그 외 = 상주 agent key. 채널은 **타임라인의
// 필터**다(v9.4.0 ⑥) — 별도 스트림을 들고 있지 않으므로 리로드하면
// replay 버퍼가 그대로 복원한다.
export var ovActiveChannel = "main";
// 3단계: 글로벌 ask 트레이 — **주소가 사람인 열린 질문**을 채널 무관하게
// 노출한다. 진실원은 roster 행의 `open_questions` 다(질문 단위).
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
  ovRenderAskTray(); // 진실원은 roster 의 open_questions
}
// 글로벌 ask 트레이: roster 행의 `open_questions` 중 사람 주소인 것을
// 채널 무관하게 렌더. 답변은 그 agent 로 고정(api/agent/<key>/input), 서버
// 공유 상태라 누가 먼저 답하면 질문이 닫혀 → 모든 뷰어에서 사라짐(선착순).
// main 의 prompt/confirm 을 트레이 항목(DOM)으로 — 검증된 조각 재사용
// (buildPromptMetaEl·highlightDangerHtml·confirm-btn·mode-context). 3b.
function ovBuildMainAskEl() {
  var kind = session.mainAsk.kind;
  var data = session.mainAsk.data || {};
  var item = el("div", ["ask-item", "ask-main"]);
  var head = el("div", ["ask-q"]);
  head.textContent =
    kind === "confirm" ? "⚠ main — confirmation needed" : "❓ main asked";
  item.appendChild(head);
  var meta = buildPromptMetaEl(data, kind === "confirm");
  if (meta) item.appendChild(meta);
  if (kind === "confirm") {
    // **무엇을 승인하는지**를 먼저 보인다. 종전엔 이 줄이 없어서, 워크스페이스
    // 밖 경로에 쓰려는 것을 승인할 때 화면에 남는 게 헤더+버튼뿐이었다 —
    // 이게 이탈 가드라는 사실도, 어느 경로가 밖인지도, 루트가 뭔지도 없이
    // `a` 버튼만 "always allow these locations" 라고 말했다(감사 발견).
    // `shell` 은 `command` 를 따로 실어 강조 렌더가 되지만 edit/write_file 은
    // 안 싣는다. `prompt` 는 그 사유의 유일한 전달자다.
    if (typeof data.prompt === "string" && data.prompt.trim()) {
      item.appendChild(el("div", ["confirm-why"], data.prompt.trim()));
    }
    if (typeof data.command === "string" && data.command) {
      item.appendChild(
        elHtml("pre", ["action-shell", "confirm-cmd"],
          highlightDangerHtml(data.command, data.danger_spans))
      );
    }
    var cmt = el("input", ["ask-answer"]);
    cmt.type = "text";
    cmt.placeholder = "Optional comment";
    var btns = el("div", ["ask-confirm-btns"]);
    (data.options || []).forEach(function (opt) {
      var b = el("button", ["confirm-btn"]);
      if (opt.key === data.default_key) b.classList.add("default");
      b.textContent = opt.key + " — " + opt.label;
      b.addEventListener("click", function () {
        ovSubmitMainConfirm(opt.key, cmt.value.trim());
      });
      btns.appendChild(b);
    });
    cmt.addEventListener("keydown", function (e) {
      if (e.key === "Enter" && !e.isComposing) {
        e.preventDefault();
        ovSubmitMainConfirm(data.default_key, cmt.value.trim());
      }
    });
    item.appendChild(btns);
    var cw = el("div", ["ask-in"]);
    cw.appendChild(cmt);
    item.appendChild(cw);
  } else {
    var ctx = typeof data.context === "string" ? data.context : "";
    if (ctx) {
      var ce = el("div", ["mode-context"]);
      ce.textContent = ctx;
      item.appendChild(ce);
    }
    var ans = el("input", ["ask-answer"]);
    ans.type = "text";
    ans.placeholder = "Answer… (Enter to send)";
    var send = el("button", ["ask-send", "btn-primary"]);
    send.textContent = "Send";
    send.addEventListener("click", function () {
      ovSubmitMainPrompt(ans.value);
      ans.value = "";
    });
    ans.addEventListener("keydown", function (e) {
      if (e.key === "Enter" && !e.isComposing) {
        e.preventDefault();
        ovSubmitMainPrompt(ans.value);
        ans.value = "";
      }
    });
    var iw = el("div", ["ask-in"]);
    iw.appendChild(ans);
    iw.appendChild(send);
    item.appendChild(iw);
  }
  return item;
}
export function ovRenderAskTray() {
  var tray = document.getElementById("ask-tray");
  if (!tray) return;
  // 비동기 질문(docs/agent-ask/DESIGN.md §3.6): 열린 질문이 뜬다 — 주소는
  // 언제나 사람이다(v10.77.0). 상대가 막혀 있지 않으므로 state 로는 알 수
  // 없고 질문 자체가 진실이다 — 한 에이전트가 동시에 여럿 물을 수 있어 항목도
  // 질문 단위.
  var open = [];
  ovRoster.forEach(function (t) {
    (t.open_questions || []).forEach(function (q) {
      open.push({ key: t.key, q: q });
    });
  });
  if (!session.mainAsk && !open.length) {
    tray.hidden = true;
    tray.innerHTML = "";
    return;
  }
  var html = "";
  open.forEach(function (it) {
    html +=
      '<div class="ask-item" data-key="' + escapeHtml(it.key) +
      '" data-qid="' + escapeHtml(it.q.id) + '">' +
      '<div class="ask-q">❓ <b>' + ovAgentLabelHtml(it.key) +
      "</b> asked</div>" +
      '<div class="ask-qt">' + escapeHtml(it.q.text || "") + "</div>" +
      '<div class="ask-in"><input class="ask-answer" type="text" ' +
      'placeholder="Answer…" aria-label="Answer"><button type="button" ' +
      'class="ask-send btn-primary">Send</button></div></div>';
  });
  tray.innerHTML = html;
  // main ask 항목(DOM)은 맨 위에 — 검증된 confirm/prompt 조각 재사용.
  if (session.mainAsk) tray.insertBefore(ovBuildMainAskEl(), tray.firstChild);
  tray.hidden = false;
}
// 트레이 답변 전송(그 asker 로 고정 — 드롭박스 채널과 무관).
function ovSubmitAsk(key, text, qid) {
  if (!key || !text.trim()) return;
  // `answer_id` 가 있으면 새 일감이 아니라 **그 질문의 답**으로 짝짓는다
  // — 없으면 서버가 종전대로 inbox 로 넣는다(블로킹 경로).
  var body = { content: text, conn_id: session.connId };
  if (qid) body.answer_id = qid;
  fetch("api/agent/" + encodeURIComponent(key) + "/input", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}
(function () {
  var tray = document.getElementById("ask-tray");
  if (!tray) return;
  function submitFrom(el) {
    var item = el.closest(".ask-item");
    if (!item) return;
    var inp = item.querySelector(".ask-answer");
    ovSubmitAsk(
      item.getAttribute("data-key"),
      inp ? inp.value : "",
      item.getAttribute("data-qid")
    );
    if (inp) inp.value = ""; // 낙관적 클리어 — roster 갱신이 항목 제거
  }
  tray.addEventListener("click", function (e) {
    if (e.target.classList.contains("ask-send")) submitFrom(e.target);
  });
  tray.addEventListener("keydown", function (e) {
    if (e.key === "Enter" && !e.isComposing && e.target.classList.contains("ask-answer")) {
      e.preventDefault();
      submitFrom(e.target);
    }
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
  applyChannelFilter();
  frameShowChannel(ovActiveChannel);
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
    var chip = e.target.closest ? e.target.closest(".ov-ch") : null;
    if (!chip) return;
    var key = chip.getAttribute("data-key");
    var ctl = e.target.closest(".ov-ch-ctl");
    if (ctl) {
      e.stopPropagation();
      fetch("api/agent/" + encodeURIComponent(key) + "/" +
        ctl.getAttribute("data-act"), { method: "POST" });
      return;
    }
    ovSetChannel(key);
  });
  ovSyncChannels(); // 로드 시 최소 main 칩 렌더(roster 오면 갱신)
  frameShowChannel(ovActiveChannel); // main 의 맨 위 카드·꼬리 띠
})();

/** Scroll the TIMELINE CONTAINER to a card — never scrollIntoView, which
 * also scrolls every scrollable ancestor (incl. horizontally) and shoved
 * the whole team view sideways in the mockup. The container is the one
 * surface that should move. */
function scrollTimelineTo(card) {
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

// 모듈 경계의 setter — 이 모듈의 let/var 는 이 모듈만 대입한다(DESIGN §8.3).
export function setOvActiveChannel(v) { ovActiveChannel = v; }
export function setOvBack(v) { ovBack = v; }
