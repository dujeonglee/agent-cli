// attach.js — 이미지 첨부: 📎 버튼·붙여넣기·끌어놓기, 업로드 칩, vision 가능 여부.
// (v10.117.0, docs/web-modules/DESIGN.md §8 — 옛 main.js 의 절 [23])

import { $input, $inputArea, el } from "./dom.js";

// ── 이미지 첨부 (v10.60.0, docs/vision/DESIGN.md §5) ───────────────
// 세 입구(📎 · 붙여넣기 · 끌어놓기)가 한 함수(`attachFiles`)로 모인다. 파일은
// 고르는 즉시 `api/images/upload` 로 올라가 `.agent-cli/tmp/uploads/` 에
// 들어가고, 칩은 서버가 돌려준 참조(`path`·크기)를 든다. 보내기는 그 경로만
// `api/input` 의 `images` 에 싣는다 — 서버가 다시 검증해 참조를 만든다.
// 모델이 이미지를 못 보면(`api/images/support` = false) 📎 를 숨기고
// 붙여넣기·끌어놓기에 한 줄로 알린다. null(미확인)이면 보통처럼 받는다 —
// 첫 이미지 런이 값을 정하고, 런이 끝날 때마다 다시 묻는다.
const $attach = document.getElementById("attach");
const $attachInput = document.getElementById("attach-input");
const $attachStrip = document.getElementById("attach-strip");
const attachChips = []; // {chip, path, uploading}
let attachVision = null; // true | false | null(미확인)
let attachMsgTimer = null;

export function attachRefreshVision() {
  fetch("api/images/support")
    .then(function (r) { return r.ok ? r.json() : null; })
    .then(function (d) {
      if (!d) return;
      attachVision = d.supported;
      $attach.hidden = attachVision === false;
    })
    .catch(function () {});
}

export function attachMessage(text) {
  let m = $attachStrip.querySelector(".attach-msg");
  if (!m) {
    m = el("span", ["attach-msg"]);
    $attachStrip.appendChild(m);
  }
  m.textContent = text;
  $attachStrip.hidden = false;
  clearTimeout(attachMsgTimer);
  attachMsgTimer = setTimeout(function () {
    m.remove();
    attachSyncStrip();
  }, 4000);
}

function attachSyncStrip() {
  $attachStrip.hidden = !attachChips.length && !$attachStrip.querySelector(".attach-msg");
}

export function attachReadyPaths() {
  return attachChips.filter(function (c) { return !c.uploading && c.path; })
    .map(function (c) { return c.path; });
}
export function attachBusy() {
  return attachChips.some(function (c) { return c.uploading; });
}
export function attachClear() {
  attachChips.splice(0).forEach(function (c) { c.chip.remove(); });
  attachSyncStrip();
}

function attachFiles(files) {
  const list = Array.prototype.filter.call(files || [], function (f) {
    return f && /^image\/(png|jpeg|gif|webp)$/.test(f.type);
  });
  if (!list.length) return false;
  if (attachVision === false) {
    attachMessage("This model cannot view images — describe it in words");
    return true;
  }
  list.forEach(attachOne);
  return true;
}

function attachOne(file) {
  const chip = el("span", ["attach-chip", "uploading"]);
  const img = el("img");
  img.src = URL.createObjectURL(file);
  img.alt = file.name;
  chip.appendChild(img);
  const label = el("span", [], file.name || "image");
  chip.appendChild(label);
  const rm = el("span", ["rm"], "✕");
  rm.title = "Remove";
  chip.appendChild(rm);
  $attachStrip.appendChild(chip);
  const entry = { chip: chip, path: "", uploading: true };
  attachChips.push(entry);
  attachSyncStrip();
  rm.addEventListener("click", function () {
    const i = attachChips.indexOf(entry);
    if (i >= 0) attachChips.splice(i, 1);
    chip.remove();
    attachSyncStrip();
  });
  const name = (file.name || "image").replace(/[\\/]/g, "_") || "image";
  fetch("api/images/upload?name=" + encodeURIComponent(name), {
    method: "POST",
    body: file,
  })
    .then(function (r) {
      return r.json().then(function (d) { return { ok: r.ok, d: d }; });
    })
    .then(function (res) {
      if (!res.ok) throw new Error((res.d && res.d.detail) || "upload failed");
      entry.path = res.d.path;
      entry.uploading = false;
      chip.classList.remove("uploading");
      label.textContent = res.d.path + " (" + res.d.width + "×" + res.d.height + ")";
    })
    .catch(function (err) {
      const i = attachChips.indexOf(entry);
      if (i >= 0) attachChips.splice(i, 1);
      chip.remove();
      attachMessage(String(err.message || err));
    });
}

$attach.addEventListener("click", function () { $attachInput.click(); });
$attachInput.addEventListener("change", function () {
  attachFiles($attachInput.files);
  $attachInput.value = "";
});
// 붙여넣기: 클립보드에 이미지 파일이 있으면 그것만 받고 글 붙여넣기는 막는다
// (스크린샷 붙여넣기는 글이 비어 있다). 글만 있으면 기본 동작 그대로.
$input.addEventListener("paste", function (e) {
  const files = e.clipboardData && e.clipboardData.files;
  if (files && files.length && attachFiles(files)) e.preventDefault();
});
["dragenter", "dragover"].forEach(function (ev) {
  $inputArea.addEventListener(ev, function (e) {
    if (!e.dataTransfer || !Array.prototype.some.call(e.dataTransfer.types || [], function (t) { return t === "Files"; })) return;
    e.preventDefault();
    $inputArea.classList.add("drop-over");
  });
});
$inputArea.addEventListener("dragleave", function () {
  $inputArea.classList.remove("drop-over");
});
$inputArea.addEventListener("drop", function (e) {
  $inputArea.classList.remove("drop-over");
  if (!e.dataTransfer || !e.dataTransfer.files || !e.dataTransfer.files.length) return;
  e.preventDefault();
  if (!attachFiles(e.dataTransfer.files)) attachMessage("Only PNG, JPEG, GIF or WebP can be attached");
});
attachRefreshVision();
