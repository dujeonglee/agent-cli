// ── 세션 노브 클라이언트 (v10.43.0, docs/knobs/DESIGN.md §5.1) ─────────────
// 한 번의 GET api/knobs (상태 한 장씩, `knobs.web_state` 모양) 와 POST
// api/knobs/<name> {value}. 다른 뷰어의 변경(과 서버가 다시 계산한 상태)은
// SSE `knob` → `agentcli:knob` 으로 온다. 종전 노브별 엔드포인트 일곱(api/
// compaction·stream-idle·max-agents·confirm-mode·thinking·grammar·auditor)과
// 이벤트 일곱을 대체.
import { sendOnce } from "./send.js";

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
// 바꾸기 — 언제나 ``{ok: true, …상태}`` 또는 ``{ok: false, error}`` 로 끝난다 (vNEXT: 종전엔 실패를 null 로 뭉개
// 서버가 준 사유를 버렸다). 거부(미등록 모델 등)는 서버의 200 ``{ok: false, error}``, 그 밖(없는 노브·네트워크)은
// 보내기의 사유. 상한 없음 — 늦게라도 닿으면 적용된다(다른 누름과 같다).
function set(name, value) {
  return new Promise((resolve) => {
    sendOnce(
      { sending: false },
      (signal) =>
        fetch("api/knobs/" + name, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ value: value }),
          signal: signal,
        }),
      (failure, status, r) => {
        if (failure) return resolve({ ok: false, error: failure });
        r.json().then(
          (d) => resolve(d && d.ok ? d : { ok: false, error: (d && d.error) || "HTTP " + status }),
          () => resolve({ ok: false, error: "HTTP " + status })
        );
      },
      { limit: false }
    );
  });
}
export const KNOBS = { all, set };
