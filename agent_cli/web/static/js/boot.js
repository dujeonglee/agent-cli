// boot.js — 부트스트랩: 주소창의 ?token= 을 걷고, 인증이 없을 때의 안내(showSetupHelp).
// app.js 의 첫 import 다: 다른 모듈이 fetch 하기 전에 주소가 정리된다.
// (v10.117.0, docs/web-modules/DESIGN.md §8 — 옛 main.js 의 절 [1])

// ── Bootstrap token → cookie ───────────────
// The index response set an HttpOnly ``act`` cookie from the one-time
// ``?token=`` URL, so every request (and the SSE stream, which cannot send
// headers) authenticates via that cookie — the token never rides in a
// per-request URL. Strip it from the address bar. If we hold no valid cookie,
// the SSE handshake fails and ``showSetupHelp`` (below) explains what to do.
const params = new URLSearchParams(window.location.search);
if (params.has("token")) {
  params.delete("token");
  const q = params.toString();
  history.replaceState(
    null,
    "",
    window.location.pathname + (q ? "?" + q : "") + window.location.hash
  );
}

export function showSetupHelp() {
  document.body.innerHTML =
    '<div class="setup-message">' +
    "<h1>agent-cli web</h1>" +
    "<p>Not authenticated. Open the URL printed by " +
    "<code>agent-cli web</code> — it carries a one-time " +
    "<code>?token=</code> that installs your session cookie.</p>" +
    "</div>";
}
