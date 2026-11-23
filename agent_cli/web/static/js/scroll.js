// scroll.js — 따라가기: #messages 의 자동 스크롤·'새 줄' 점프 버튼·스냅샷 재생 중 숨김.
// (v10.117.0, docs/web-modules/DESIGN.md §8 — 옛 main.js 의 절 [7])

import { $messages } from "./dom.js";

// ── 따라가기 (v9.26.0, 사용자가 시안으로 확인한 세 규칙) ──────────
// 상태는 값 하나: ``pinned``.
//   1. 처음 열면 맨 아래 — 스냅샷 재생·늦게 자라는 레이아웃(폰트·이미지·
//      펼친 카드)까지 바닥을 유지한다.
//   2. 바닥에서 SCROLL_BOTTOM_THRESHOLD 안으로 오면 **그 자리에서** 바닥에
//      붙이고 pinned — 이후 새 내용·스트리밍·창 크기 변화에 바닥을 따라간다.
//   3. 그보다 멀어지면 unpinned — 보던 줄이 움직이지 않고, 그 사이 도착한
//      카드 수가 "↓ 새 메시지 n" 버튼에 쌓인다. 누르면 규칙 2.
// 규칙 2·3 의 주어는 **사람**이다: 사람이 타임라인을 굴렸을 때만 붙고 풀린다. 사람의 입력 없이 바뀐
// 위치 — 브라우저가 옮긴 것(위쪽에 줄이 끼면 스크롤 앵커링, 아래가 빠지면 잘림)과 프로그램이 쓴 것 — 은 판정하지
// 않는다: 따라가는 중이면 바닥으로 다시 간다.
// 종전(~v9.25)은 근처에서 플래그만 바꿔 **다음 이벤트가 와야** 따라갔고,
// 로드 뒤 늦게 자란 내용은 바닥을 놓쳤다(사용자 보고: "기본이 맨 아래여야").
let pinned = true;
// 열 때(v10.8.1): 스냅샷이 다 재생될 때까지 타임라인을 숨기고 스크롤도 하지
// 않는다 — 카드가 붙을 때마다 내려가는 모습 대신, `snapshot_end` 에서 한 번에
// 맨 아래로 점프한 뒤 보여 준다. 재접속은 전체 리로드(es.onerror)라 같은 길.
let loading = true;
document.body.classList.add("loading");
export function revealAtBottom() {
  if (!loading) return;
  loading = false;
  setScrollTop($messages.scrollHeight);
  document.body.classList.remove("loading");
}
// 옛 서버(snapshot_end 없음)와의 창: 2초면 어차피 다 왔다. 브라우저 전역이
// 있을 때만 — 노드 마크다운 하네스(test_app_markdown)는 이 IIFE 를 그대로 돈다.
if (typeof requestAnimationFrame === "function") setTimeout(revealAtBottom, 2000);
const SCROLL_BOTTOM_THRESHOLD = 48; // px — 메시지 두 줄쯤 (시안에서 확인)
let _unseen = 0; // 위치 고정 중 도착한 카드 수

function isAtBottom() {
  const dist =
    $messages.scrollHeight - $messages.scrollTop - $messages.clientHeight;
  return dist <= SCROLL_BOTTOM_THRESHOLD;
}

export function setScrollTop(v) {
  const max = Math.max(0, $messages.scrollHeight - $messages.clientHeight);
  v = Math.min(v, max);
  if (Math.abs($messages.scrollTop - v) < 1) return; // 이미 거기 — 쓰기 없음
  $messages.scrollTop = v;
}

function scrollToBottom() {
  if (!pinned) return;
  setScrollTop($messages.scrollHeight);
}

// "↓ 새 메시지 n" — #messages 밖(body)에 fixed 로 띄운다. #messages 안에
// 두면 `#messages > *` 를 세는 곳(성능 테스트·내보내기)에 섞이고, 카드가
// 뒤에 붙으면 마지막 자식이 아니게 된다.
const $jump = document.createElement("button");
$jump.id = "jump-new";
$jump.type = "button";
$jump.hidden = true;
$jump.title = "Jump to bottom";
document.body.appendChild($jump);
function placeJump() {
  const r = $messages.getBoundingClientRect();
  $jump.style.left = r.left + r.width / 2 + "px";
  $jump.style.bottom = window.innerHeight - r.bottom + 14 + "px";
}
function renderJump() {
  $jump.hidden = pinned || _unseen === 0;
  if (!$jump.hidden) {
    $jump.textContent = "↓ " + _unseen + " new";
    placeJump();
  }
}
export function pin() {
  pinned = true;
  _unseen = 0;
  $messages.dataset.pinned = "1"; // 상태 노출 — 실브라우저 테스트가 읽는다
  renderJump();
  scrollToBottom();
}
export function unpin() {
  if (!pinned) return;
  pinned = false;
  $messages.dataset.pinned = "0";
  renderJump();
}
$messages.dataset.pinned = "1";
$jump.addEventListener("click", pin);

// 렌더 병합 (v8.42.0 — 리뷰 §4.6 효율): 스냅샷 재생·스트리밍처럼 이벤트가
// 동기로 폭주하는 구간에서 scrollTop=scrollHeight 쓰기가 이벤트마다 전체
// 레이아웃을 강제해 O(N²) 였다 (888이벤트 스냅샷에서 ~400ms 블로킹 실측).
// requestAnimationFrame 당 1회로 병합 — 폭주 구간에선 사실상 마지막 1회만
// 실행되고, 라이브에서도 프레임당 1회로 준다. ~16ms 지연은 인지 불가.
var _scrollQueued = false;
export function scheduleScroll() {
  if (_scrollQueued) return;
  _scrollQueued = true;
  requestAnimationFrame(function () {
    _scrollQueued = false;
    scrollToBottom();
  });
}

// 사람의 굴림인가 — 값 둘. ``held``: 타임라인 위에서 포인터를 누르고 있다(스크롤바 끌기·글자 끌어 고르기 —
// 놓을 때까지). ``rolling``: 휠·터치 밀기·굴리는 키가 시작한 굴림이 아직 끝나지 않았다(`scrollend` 가 내린다 —
// 관성까지 덮는다). 클릭은 포인터를 놓은 뒤에 처리되므로 단추가 부른 스크롤(점프·"↓ n new"·채널 전환)은 어느
// 쪽도 아니다. `scrollend` 가 없는 브라우저에서는 ``rolling`` 이 내려가지 않는다 — 첫 굴림 뒤로는 모든 scroll
// 사건을 사람의 것으로 본다(종전과 같은 판정).
let held = false;
let rolling = false;
const SCROLL_KEYS = ["PageUp", "PageDown", "Home", "End", "ArrowUp", "ArrowDown", " "];
$messages.addEventListener("pointerdown", function () { held = true; });
["pointerup", "pointercancel"].forEach(function (t) {
  document.addEventListener(t, function () { held = false; });
});
["wheel", "touchmove"].forEach(function (t) {
  $messages.addEventListener(t, function () { rolling = true; }, { passive: true });
});
document.addEventListener("keydown", function (e) {
  // 글 쓰는 자리에서는 그 키가 타임라인을 굴리지 않는다
  const typing = e.target.closest && e.target.closest("textarea, input, [contenteditable]");
  if (SCROLL_KEYS.indexOf(e.key) >= 0 && !typing) rolling = true;
});
$messages.addEventListener("scrollend", function () { rolling = false; });

// 규칙 2·3 — 사람이 굴릴 때마다 바닥과의 거리로 판정. 근처에 들어오면
// 굴림이 멎은 뒤(80ms) 붙인다 — 굴리는 손에서 낚아채지 않는다.
let _settle = null;
$messages.addEventListener("scroll", function () {
  if (!held && !rolling) {
    if (pinned) scheduleScroll(); // 사람이 굴린 것이 아니다 — 따라가는 중이면 바닥으로
    return;
  }
  if (isAtBottom()) {
    clearTimeout(_settle);
    _settle = setTimeout(function () {
      if (isAtBottom()) pin();
    }, 80);
  } else {
    clearTimeout(_settle);
    unpin();
  }
});

// 규칙 1·3 — 내용이 붙을 때: 고정이면 따라가고(늦은 추가까지), 아니면 **보고 있는 채널에 새로 붙은, 보이는
// 카드**를 센다 — 루트에 붙든 런 블록·스코프 카드 안에 붙든(에이전트 채널의 카드는 블록 몸통 안에 붙는다).
// 채널 필터가 감춘 것은 `offsetParent` 가 없다(필터는 추가와 같은 동기 구간에서 끝나고 이 콜백은 그 뒤
// 마이크로태스크다).
new MutationObserver(function (records) {
  let added = 0;
  let cards = 0;
  for (const rec of records)
    for (const n of rec.addedNodes) {
      if (n.nodeType !== 1) continue;
      added++;
      if (n.classList.contains("card") && n.offsetParent !== null) cards++;
    }
  if (loading) return; // 재생 중엔 숨겨져 있다 — snapshot_end 가 한 번에 내린다
  if (pinned) {
    if (added) scheduleScroll();
  } else if (cards) {
    _unseen += cards;
    renderJump();
  }
}).observe($messages, { childList: true, subtree: true });
// 규칙 1·2 끝부분 — 창 크기·늦은 레이아웃(폰트·이미지)에도 바닥 유지.
new ResizeObserver(function () {
  if (pinned) scheduleScroll();
  if (!$jump.hidden) placeJump();
}).observe($messages);
$messages.addEventListener("load", function () { if (pinned) scheduleScroll(); }, true);
if (document.fonts && document.fonts.ready) document.fonts.ready.then(scheduleScroll);
