// send.js — 보내기 한 번 (v10.147.3 입력창 → v10.147.4 트레이 답 → vNEXT 확인·글 없는 버튼): 응답·오류·상한 중
// 먼저 온 것으로 한 번만 끝내고, 거절의 사유를 한 가지로 읽는다. 순환 밖 잎 — import 없음(``fetch`` 는 호출자가
// ``request`` 로 준다)이라 잎 모듈(stuck 등)도 쓴다.
//
// 규칙 하나: 쓴 글은 서버가 받았을 때만 비운다 — 거절(죽은 에이전트·사라진 첨부 등)·네트워크 오류·무응답이면
// 사유를 알리고 글은 남긴다. 보내는 자리(입력창 하나, 트레이 항목마다 하나)마다 "보내는 중" 이 따로 서고,
// 같은 자리의 두 번째 보내기는 알리고 받지 않는다(중복·순서 뒤바뀜 없음) — 다른 자리는 막지 않는다.
// 응답이 ``SEND_LIMIT_MS`` 안에 오지 않으면(막힌 연결) 요청을 끊고 실패로 본다 — 끊긴 요청의 늦은 결과는
// 화면을 건드리지 않는다. 요청 본문은 글·경로뿐이라 정상 응답은 로컬에서 수십 ms 다(큰 이미지 셋이 든
// 요청도 서버의 검증·보관까지 70 ms) — 상한은 원격·프록시 너머의 지연까지 넉넉히 덮는다.
const SEND_LIMIT_MS = 20000;

// 거절 응답의 사유 — 서버의 ``detail`` 글, 없으면 ``HTTP <code>``. 읽기만 한다.
function refusalReason(r) {
  return r.json().then(
    function (d) { return d && typeof d.detail === "string" ? d.detail : "HTTP " + r.status; },
    function () { return "HTTP " + r.status; }
  );
}

// 보내기 하나를 시작해 응답·오류·상한 중 먼저 온 것으로 한 번만 끝낸다. ``slot`` 은 그 자리의 상태
// ``{sending}`` — 보내는 동안 true(호출자가 넘긴 것만 바꾼다). ``request(signal)`` 은 fetch 의 약속,
// ``settled(failure, status)`` 는 끝에 한 번 불린다: ``failure`` 는 받았으면 "", 아니면 사유(서버의
// detail · ``HTTP <code>`` · ``could not reach the server`` · ``no response from the server``).
//
// ``limit`` (vNEXT): false 면 상한이 없다 — 늦게라도 닿으면 적용되는 것이 맞는 누름(확인 클릭·정지·취소·되살리기)은
// 막힌 연결에서 끊지 않고 기다린다. 그 길의 실패는 서버의 거절과 네트워크 오류뿐이다.
export function sendOnce(slot, request, settled, { limit = true } = {}) {
  const ctl = new AbortController();
  let done = false;
  slot.sending = true;
  function finish(failure, status) {
    if (done) return;
    done = true;
    clearTimeout(timer);
    slot.sending = false;
    settled(failure, status);
  }
  const timer = limit
    ? setTimeout(function () {
        if (done) return;
        finish("no response from the server", 0);
        ctl.abort();
      }, SEND_LIMIT_MS)
    : null;
  request(ctl.signal)
    .then(function (r) {
      if (r.ok) return finish("", r.status);
      return refusalReason(r).then(function (why) { finish(why, r.status); });
    })
    .catch(function () {
      finish("could not reach the server", 0);
    });
}
