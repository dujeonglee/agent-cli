// ── 노브 칩 팝업 토글 (v8.57.0) ─────────────────────────────────
// 각 노브 칩(🗜️/👥/⏳/🧠·보따리)이 data-pop 으로 자기 팝업을 가리킨다.
// 한 번에 하나만 열리고, 바깥 클릭·Escape 로 닫힘. 팝업 내부 조작(슬라이더·
// 셀렉트)은 닫힘을 유발하지 않는다. 구 ctx-popover 단일 토글의 계승.
function chips() {
  return Array.prototype.slice.call(document.querySelectorAll(".knob-btn"));
}
function popOf(btn) {
  return document.getElementById(btn.dataset.pop || "");
}
function closeAll(except) {
  chips().forEach(function (b) {
    var pop = popOf(b);
    if (pop && b !== except) { pop.hidden = true; b.setAttribute("aria-expanded", "false"); }
  });
}
document.addEventListener("click", function (e) {
  var btn = e.target.closest && e.target.closest(".knob-btn");
  if (btn) {
    e.stopPropagation();
    var pop = popOf(btn);
    if (!pop) return;
    var willOpen = pop.hidden;
    closeAll(willOpen ? btn : null);
    pop.hidden = !willOpen;
    btn.setAttribute("aria-expanded", String(willOpen));
    if (willOpen) {
      // 기본(오른쪽 펼침)으로 열고, 뷰포트 오른쪽을 넘으면 왼쪽 펼침으로 뒤집는다.
      pop.classList.remove("align-right");
      var r = pop.getBoundingClientRect();
      if (r.right > window.innerWidth - 4) pop.classList.add("align-right");
    }
    return;
  }
  if (e.target.closest && e.target.closest(".knob-pop")) return; // 내부 조작
  closeAll(null);
});
document.addEventListener("keydown", function (e) {
  if (e.key === "Escape") closeAll(null);
});
