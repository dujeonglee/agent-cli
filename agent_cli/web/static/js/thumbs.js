// thumbs.js — 사용자 말풍선(renderUserMessage) + 이미지 썸네일 줄.
// (v10.117.0, docs/web-modules/DESIGN.md §8 — 옛 main.js 의 절 [13]·[14])

import { el, elHtml } from "./dom.js";
import { escapeAndFormat, imageUrl } from "./markdown.js";
import { scheduleScroll } from "./scroll.js";
import { finishCard, MAIN } from "./timeline.js";

// ── Card renderers ─────────────────────────

/** 사용자 말풍선. ``author`` 가 있으면 내용 앞의 `[닉]: ` 접두를 떼어
 * **별도 라벨**로 돌린다 — 단독 세션에선 오른쪽 파란 말풍선이 이미 "나"라
 * 접두가 순수 소음이고, 여러 명일 때만 누가 보냈는지가 정보다.
 *
 * 표시 여부는 CSS(`body.multi-viewer`)가 정한다. 렌더 시점에 뷰어 수로
 * 분기하면 **스냅샷 재생 순서에 종속**된다(`viewers` 가 늦게 오면 이미 그린
 * 카드들이 영영 안 바뀐다). 클래스 토글이면 나중에 온 뷰어 정보가 과거
 * 카드에도 소급된다. */
// ── 이미지 썸네일 (v10.60.0, docs/vision/DESIGN.md §7) ─────────────
// 사용자 말풍선·관찰 본문 아래 같은 줄. 원본은 `api/workspace/file?path=` —
// 워크스페이스 안 이미지만 서빙된다(서버가 매직 바이트로 거른다).
const IMAGE_PLACEHOLDER_RE = /^\[image: .+ \(\d+×\d+\)\]$/;
function refVersion(ref) {
  return ref && /^[0-9a-f]{64}$/.test(ref.sha256 || "") ? ref.sha256 : "";
}
export function thumbStrip(images) {
  const strip = el("div", ["thumbs"]);
  images.forEach(function (ref) {
    if (!ref || !ref.path) return;
    const url = imageUrl(ref.path, refVersion(ref)); // 상대 URL — <base href> 아래로 풀린다
    const a = el("a");
    a.href = url;
    a.target = "_blank";
    a.rel = "noopener";
    const img = el("img");
    img.src = url;
    img.alt = ref.path;
    img.loading = "lazy";
    a.appendChild(img);
    a.appendChild(el("span", ["cap"], ref.path + " (" + ref.width + "×" + ref.height + ")"));
    strip.appendChild(a);
  });
  return strip;
}
// 작은 썸네일 줄 (v10.61.3): 대기 줄·런 머리처럼 **접힌 채로도** 이미지가 있다는
// 사실이 보여야 하는 자리. 28px, 캡션 없음(제목에 경로). 펼치면 `thumbStrip`.
export function miniThumbs(images) {
  const strip = el("span", ["mini-thumbs"]);
  (Array.isArray(images) ? images : []).forEach(function (ref) {
    if (!ref || !ref.path) return;
    const img = el("img");
    img.src = imageUrl(ref.path, refVersion(ref));
    img.alt = ref.path;
    img.title = ref.path + " (" + ref.width + "×" + ref.height + ")";
    strip.appendChild(img);
  });
  return strip;
}
// 레코드의 자리표시 줄(`[image: path (W×H)]`)은 썸네일이 대신 말한다 — 재생
// 카드의 본문에서 뗀다(라이브 에코는 애초에 없다).
function stripImagePlaceholders(text) {
  return text
    .split("\n")
    .filter(function (line) { return !IMAGE_PLACEHOLDER_RE.test(line); })
    .join("\n")
    .replace(/\n+$/, "");
}

export function renderUserMessage(content, ts, author, hidx, images) {
  const card = el("div", ["card", "card-user"]);
  if (hidx != null) card.dataset.hidx = String(hidx);
  let body = content;
  if (author) {
    const tag = "[" + author + "]: ";
    if (body.startsWith(tag)) body = body.slice(tag.length);
  }
  const imgs = Array.isArray(images) ? images : [];
  if (imgs.length) body = stripImagePlaceholders(body);
  const bubble = elHtml("div", ["bubble"], escapeAndFormat(body));
  if (author) bubble.insertBefore(el("span", ["who"], author), bubble.firstChild);
  if (imgs.length) bubble.appendChild(thumbStrip(imgs));
  card.appendChild(bubble);
  card._md = function () {
    const lines = imgs.map(function (r) { return "[image: " + r.path + " (" + r.width + "×" + r.height + ")]"; });
    return "**👤 " + (author || "user") + "**\n\n" + [body].concat(lines).filter(Boolean).join("\n");
  };
  // main 에 두는 것을 **명시**한다 — 에이전트 채널의 사용자 입력은 여기
  // 오지 않는다 — `api/agent/<key>/input` 을 거쳐 `agent_msg` 왕래 줄로 온다.
  finishCard(card, { ts: ts, task_id: MAIN.task_id });
  scheduleScroll();
}
