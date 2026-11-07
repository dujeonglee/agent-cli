// sse.js — SSE 연결: EventSource 하나(es)와 연결 상태 표시, 클립보드 helper.
// 이벤트 핸들러는 각 소유 모듈이 es 를 import 해 건다(DESIGN §8.5).
// (v10.117.0, docs/web-modules/DESIGN.md §8 — 옛 main.js 의 절 [24])

import { showSetupHelp } from "./boot.js";
import { $status } from "./dom.js";

// ── SSE connection ─────────────────────────
export const es = new EventSource(
  "api/stream"
);

let sseConnected = false;
es.onopen = function () {
  sseConnected = true;
  $status.classList.remove("down");
  $status.classList.add("up");
};
es.onerror = function () {
  $status.classList.remove("up");
  $status.classList.add("down");
  // EventSource does NOT retry a failed handshake (401 → readyState CLOSED).
  // A hard failure before we ever connected means the browser holds no valid
  // auth cookie — show the setup help instead of a silently dead UI.
  if (!sseConnected && es.readyState === EventSource.CLOSED) showSetupHelp();
};

// Release the SSE when the page is hidden — navigation, tab close, or
// bfcache (back/forward cache). Browsers keep a bfcached page's connections
// open, so without this the server keeps counting a viewer that has left
// (roster grows on every revisit; idle-reap never fires). On bfcache restore
// reload to get a fresh connection rather than a frozen, already-closed one.
window.addEventListener("pagehide", function () {
  es.close();
});
window.addEventListener("pageshow", function (e) {
  if (e.persisted) {
    location.reload();
  }
});

/** Copy ``text`` to the clipboard. navigator.clipboard needs a secure
 * context (https / localhost) — board-proxied LAN sessions are plain
 * http, so fall back to the legacy textarea + execCommand path there. */
export function copyToClipboard(text) {
  if (navigator.clipboard && window.isSecureContext) {
    return navigator.clipboard.writeText(text);
  }
  const ta = document.createElement("textarea");
  ta.value = text;
  ta.style.position = "fixed";
  ta.style.opacity = "0";
  document.body.appendChild(ta);
  ta.select();
  try {
    document.execCommand("copy");
  } finally {
    document.body.removeChild(ta);
  }
  return Promise.resolve();
}

es.addEventListener("ready", function (e) {
  const d = JSON.parse(e.data);
  // 방 제목 (v10.71.0): 보드 글의 topic 이 --title 로 온다. 헤더 첫 자리와 브라우저
  // 탭 제목이 된다 — 방을 여럿 열어도 탭만 보고 고를 수 있게. 없으면 둘 다 종전대로.
  const $title = document.getElementById("room-title");
  if ($title) {
    if (d.title) {
      $title.textContent = d.title;
      $title.title = "Room title — " + d.title;
      $title.hidden = false;
      document.title = d.title + " · agent-cli";
    } else {
      $title.hidden = true;
      document.title = "agent-cli";
    }
  }
  // ``workspace`` is the agent's working directory at session
  // creation time. Showing it in the top bar disambiguates which
  // checkout you're talking to when several LAN sessions are open
  // side-by-side. Field is omitted (rather than empty-string) when
  // unavailable so we never render a dangling " · " separator.
  // 칩 헤더 (v7.1.0): 모델 칩 = 모델명만 (provider 는 hover), 워크스
  // 페이스는 별도 칩에 꼬리(마지막 세그먼트)만 — hover 로 전체 경로.
  // 모델은 🤖 노브 칩이 보여 준다 (v10.47.1 — 종전 #info 모델 박스 삭제)
  if (d.workspace) {
    // ws 칩 = 클릭-복사 버튼 (별도 내부 버튼은 칩 높이보다 커서 세로
    // 클리핑됐던 실사용 피드백의 수리). 표시는 마지막 2세그먼트,
    // hover = 전체 경로, 클릭 = 복사 + ✓ 플래시.
    const ws = document.getElementById("chip-ws");
    const ic = document.getElementById("ws-copy-ic");
    const segs = d.workspace.replace(/\/+$/, "").split("/").filter(Boolean);
    const tail =
      segs.length > 2 ? "…/" + segs.slice(-2).join("/") : d.workspace;
    document.getElementById("ws-tail").textContent = tail;
    ws.title = "Copy workspace path — " + d.workspace;
    ws.hidden = false;
    ws.addEventListener("click", function () {
      copyToClipboard(d.workspace).then(function () {
        ic.textContent = "✓";
        setTimeout(function () {
          ic.textContent = "📋";
        }, 1000);
      });
    });
  }
});
