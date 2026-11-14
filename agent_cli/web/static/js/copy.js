// copy.js — 복사: 카드·타임라인 마크다운과 복사 버튼. 테스트 훅 window.__cardMarkdown·
// window.__timelineMarkdown 을 단다.
// (v10.117.0, docs/web-modules/DESIGN.md §8 — 옛 main.js 의 절 [9])

import { $messages, el } from "./dom.js";
import { copyToClipboard } from "./sse.js";

// ── 복사 (v10.8.0) — export 를 대신한다 ──────────────────────────
// 카드마다 ⧉ 하나: 그 카드를 **마크다운**으로 클립보드에. 헤더의 ⧉ 는 보이는
// 대화 전체(현재 채널)를 같은 포맷으로 이어 붙인다. 카드는 자기 마크다운을
// `_md`(함수)로 가진다 — 렌더 결과(HTML)를 되긁지 않고 원문(최종답의 마크다운
// 소스, 도구 입력 JSON, 관찰 원문)에서 만든다. 인라인 카드는 자식 카드를 재귀.
export function fence(text, lang) {
  const body = String(text == null ? "" : text);
  // 본문에 ``` 가 있으면 더 긴 울타리로
  let f = "```";
  while (body.indexOf(f) !== -1) f += "`";
  return f + (lang || "") + "\n" + body.replace(/\n$/, "") + "\n" + f;
}
function cardMarkdown(card) {
  if (!card || !card.classList) return "";
  if (typeof card._md === "function") return card._md() || "";
  if (card.classList.contains("card-task-group")) {
    const title = card.querySelector(":scope > .task-header > .task-title");
    const meta = card.querySelector(":scope > .task-header > .task-meta");
    const body = card.querySelector(":scope > .task-body");
    const kids = body ? Array.prototype.slice.call(body.children) : [];
    const topEl = card.querySelector(":scope > .ctx-top");
    const inner = (topEl ? [topEl] : []).concat(kids).map(cardMarkdown).filter(Boolean).join("\n\n");
    return (
      "#### " + ((title && title.textContent) || "") +
      (meta && meta.textContent ? " — " + meta.textContent : "") +
      (inner ? "\n\n" + inner : "")
    );
  }
  if (card.classList.contains("card-sys")) {
    const t = card.querySelector(".sys-text");
    return t && t.textContent ? "_" + t.textContent + "_" : "";
  }
  return "";
}
// 채널의 맨 위 카드(Head)는 대화 밖 고정 자리(#ctx-head)에 선다 — 압축 요약을 싣는
// 그 카드(`_md`)를 대화 앞에 둬, 대화 복사가 Head 가 대화 맨 앞에 있던 때와 같다.
const $ctxHead = document.getElementById("ctx-head");
function timelineMarkdown() {
  const slice = Array.prototype.slice;
  return ($ctxHead ? slice.call($ctxHead.children) : [])
    .concat(slice.call($messages.children))
    .filter(function (n) { return !n.hidden; }) // 현재 채널에 보이는 것만
    .map(cardMarkdown)
    .filter(Boolean)
    .join("\n\n");
}
export function flashCopied(btn) {
  const old = btn.textContent;
  btn.textContent = "✓";
  btn.classList.add("done");
  setTimeout(function () { btn.textContent = old; btn.classList.remove("done"); }, 1000);
}
export function attachCopy(card) {
  if (!card.classList || card.querySelector(":scope > .card-copy")) return;
  if (typeof card._md !== "function" && !card.classList.contains("card-task-group")) return;
  const btn = el("button", ["card-copy"], "⧉");
  btn.type = "button";
  btn.title = "Copy this card (Markdown)";
  btn.addEventListener("click", function (e) {
    e.stopPropagation();
    copyToClipboard(cardMarkdown(card)).then(function () { flashCopied(btn); });
  });
  card.appendChild(btn);
}
window.__cardMarkdown = cardMarkdown; // 브라우저 테스트가 읽는다
window.__timelineMarkdown = timelineMarkdown;
(function () {
  const btn = document.getElementById("copy-all-btn");
  if (!btn) return;
  btn.addEventListener("click", function () {
    copyToClipboard(timelineMarkdown()).then(function () { flashCopied(btn); });
  });
})();
