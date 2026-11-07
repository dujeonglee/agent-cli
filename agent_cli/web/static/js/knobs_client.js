// ── 세션 노브 클라이언트 (v10.43.0, docs/knobs/DESIGN.md §5.1) ─────────────
// 한 번의 GET api/knobs (상태 한 장씩, `knobs.web_state` 모양) 와 POST
// api/knobs/<name> {value}. 다른 뷰어의 변경(과 서버가 다시 계산한 상태)은
// SSE `knob` → `agentcli:knob` 으로 온다. 종전 노브별 엔드포인트 일곱(api/
// compaction·stream-idle·max-agents·confirm-mode·thinking·grammar·auditor)과
// 이벤트 일곱을 대체.
let loaded = null;
function all() {
  if (!loaded) {
    loaded = fetch("api/knobs")
      .then((r) => (r.ok ? r.json() : null))
      .then((d) => (d && d.knobs) || [])
      .catch(() => []);
  }
  return loaded;
}
function set(name, value) {
  return fetch("api/knobs/" + name, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ value: value }),
  })
    .then((r) => (r.ok ? r.json() : null))
    .then((d) => (d && d.ok ? d : null))
    .catch(() => null);
}
export const KNOBS = { all, set };
