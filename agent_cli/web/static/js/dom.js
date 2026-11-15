// dom.js — 메인 화면이 함께 쓰는 DOM 참조와 요소 생성 helper(el·elHtml).
// 다른 모듈이 top-level 에서 읽는 DOM 참조는 여기에만 둔다 — import 없는 잎. 접히는 요소의 클릭 규칙(foldOnClick)도.
// (v10.117.0, docs/web-modules/DESIGN.md §8 — 옛 main.js 의 절 [2]·[5])

// ── DOM refs ───────────────────────────────
export const $messages = document.getElementById("messages");
export const $input = document.getElementById("input");
export const $send = document.getElementById("send");
export const $chatStop = document.getElementById("chat-stop");
export const $chatStopMore = document.getElementById("chat-stop-more");
export const $stopMenu = document.getElementById("stop-menu");
export const $abort = document.getElementById("abort");
export const $tokenUsage = document.getElementById("token-usage");
export const $status = document.getElementById("conn-status");
export const $inputArea = document.getElementById("input-area");

// ── DOM helpers ────────────────────────────
// P0-6②: el() 의 3번째 인자는 **textContent** — 기본이 안전(모델/서버 원문을
// 그대로 넘겨도 마크업 실행 불가). HTML 이 필요한 곳만 elHtml() 로 **명시**한다.
// 종전엔 el() 이 무조건 innerHTML 이라 콜사이트마다 escapeHtml 규율이 갈렸고
// (606 은 이스케이프, 실패 카드는 누락 → self-XSS), 누락 = 취약이었다.
export function el(tag, classes, text) {
  const e = document.createElement(tag);
  if (classes && classes.length) e.classList.add.apply(e.classList, classes);
  if (text !== undefined && text !== null) e.textContent = text;
  return e;
}
// 이미 이스케이프/조립된 HTML 전용 — 새 콜사이트는 "왜 HTML 인가"가 자명할
// 때만 사용(마크다운 렌더·diff 색상·danger 하이라이트 류).
export function elHtml(tag, classes, html) {
  const e = document.createElement(tag);
  if (classes && classes.length) e.classList.add.apply(e.classList, classes);
  if (html !== undefined && html !== null) e.innerHTML = html;
  return e;
}

// ── 접히는 채팅 요소의 클릭 (v10.143.0) ─────────────────
// 줄·스텝 카드·런 머리 항목은 머리든 본문이든 어디를 눌러도 펴고 접는다(펼칠 때와 접을 때가 같은 규칙).
// 토글하지 않는 클릭: 상호작용 요소(링크·버튼·입력·점프 칩 — 복사 ⧉ 도 버튼이다), 그 요소 안에서 사람이
// 글을 고르고 있을 때(비어 있지 않은 선택 — 복사하려던 것), 더 안쪽의 접히는 요소를 누른 것(그 요소의 몫).
const FOLD_SKIP = "a, button, input, textarea, select, label, [contenteditable], .can-jump";
function selectingIn(node) {
  const sel = window.getSelection ? window.getSelection() : null;
  if (!sel || sel.isCollapsed || !String(sel).trim()) return false;
  return node.contains(sel.anchorNode) || node.contains(sel.focusNode);
}
/** ``node`` 를 누르면 ``toggle()`` — 위의 세 경우는 빼고. ``node`` 는 접히는 요소로 표시된다(``data-fold``). */
export function foldOnClick(node, toggle) {
  node.dataset.fold = "1";
  node.addEventListener("click", function (e) {
    const t = e.target;
    if (!t || !t.closest) return;
    if (t.closest("[data-fold]") !== node) return;
    const hit = t.closest(FOLD_SKIP);
    if (hit && node.contains(hit)) return;
    if (selectingIn(node)) return;
    toggle();
  });
}
