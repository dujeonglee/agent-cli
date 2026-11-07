// ── 🔌 MCP 상태 칩 (v9.2.0, docs/mcp-ui §D) ──────────────────────
// 읽기 전용. 부팅 시 붙인 MCP 서버의 연결/전체·도구 수·실패 이유 — 지금까지
// 이 정보는 stderr 에만 있어 웹 사용자는 볼 방법이 아예 없었다. 등록·수정은
// `agent-cli mcp` 로만(§E: 등록은 곧 로컬 프로세스 실행). 부팅 시점 상태라
// 세션 중 바뀌지 않으므로 sticky/SSE 없이 로드 시 GET 한 번. 등록 서버가
// 없으면 칩을 숨긴다 — `0/0` 은 정보가 아니라 소음.
const $wrap = document.getElementById("mcp-wrap");
const $badge = document.getElementById("mcp-badge");
const $tools = document.getElementById("mcp-tools");
const $list = document.getElementById("mcp-list");
if ($wrap && $list) {

  // 메인 IIFE 의 el() 은 이 스코프에 없다 — 같은 시그니처의 지역 헬퍼
  function el(tag, classes, text) {
    const e = document.createElement(tag);
    if (classes && classes.length) e.classList.add.apply(e.classList, classes);
    if (text !== undefined && text !== null) e.textContent = text;
    return e;
  }

  function render(d) {
    if (!d || !d.total) {
      $wrap.hidden = true;
      return;
    }
    // 배지는 붙은/전체 — 하나라도 실패하면 숫자만 보고 안다
    $badge.textContent = d.connected + "/" + d.total;
    $wrap.classList.toggle("has-fail", d.connected < d.total);
    $tools.textContent = d.tool_count + " tools";
    $list.textContent = "";
    d.servers.forEach(function (s) {
      const row = el("div", ["mcp-srv", s.connected ? "ok" : "fail"]);
      row.appendChild(el("span", ["dot"]));
      row.appendChild(el("span", ["nm"], s.name));
      row.appendChild(el("span", ["tr"], s.transport));
      let detail;
      if (s.connected) {
        const names = s.tools.slice(0, 4).join(", ") + (s.tools.length > 4 ? ", …" : "");
        detail = s.tools.length + " tools" + (names ? " · " + names : "");
      } else {
        detail = s.error || "connection failed";
      }
      row.appendChild(el("span", ["detail"], detail));
      $list.appendChild(row);
    });
    $wrap.hidden = false;
  }

  fetch("api/mcp")
    .then((r) => (r.ok ? r.json() : null))
    .then(render)
    .catch(() => {});
}
