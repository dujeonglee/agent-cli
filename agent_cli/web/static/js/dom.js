// dom.js — 메인 화면이 함께 쓰는 DOM 참조와 요소 생성 helper(el·elHtml).
// 다른 모듈이 top-level 에서 읽는 DOM 참조는 여기에만 둔다 — import 없는 잎.
// (v10.117.0, docs/web-modules/DESIGN.md §8 — 옛 main.js 의 절 [2]·[5])

// ── DOM refs ───────────────────────────────
export const $messages = document.getElementById("messages");
export const $input = document.getElementById("input");
export const $send = document.getElementById("send");
export const $chatStop = document.getElementById("chat-stop");
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
