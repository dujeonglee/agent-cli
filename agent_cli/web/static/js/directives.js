// ── Directive Editor (📝 toolbar) ─────────────────────────
// Split out of the old Prompt Inspector: this drawer ONLY edits the agent
// directives (three audience buffers). Prompt VIEWING now lives in the
// contextual Prompt Inspection drawer, opened from conversation cards.

const $btn = document.getElementById("directive-btn");
const $drawer = document.getElementById("directive-editor");
const $backdrop = document.getElementById("directive-backdrop");
const $dirText = document.getElementById("insp-dir-text");
const $dirPath = document.getElementById("insp-dir-path");
const $dirSave = document.getElementById("insp-dir-save");
const $dirCancel = document.getElementById("insp-dir-cancel");
const $dirStatus = document.getElementById("insp-dir-status");
const $dirTabs = document.getElementById("insp-dir-tabs");
const $dirBrief = document.getElementById("insp-dir-brief");
const $dirGen = document.getElementById("insp-dir-gen");
if ($btn && $drawer) {

  // 청중 스코프 탭 (5.4.0): 에디터 구조 = 파일 구조(공통 / ## @main / ## @agents).
  // 분해(GET scopes)·조립(POST scopes)은 서버(Python 파서 단일 출처).
  let dirDirty = false; // user typed since last load → don't clobber on refetch
  let dirAudience = "common";
  const dirBuffers = { common: "", main: "", agents: "" };

  function dirSyncActive() {
    dirBuffers[dirAudience] = $dirText.value;
  }
  function dirUpdateTabs() {
    if (!$dirTabs) return;
    $dirTabs.querySelectorAll("button").forEach(function (b) {
      const aud = b.getAttribute("data-aud");
      const on = aud === dirAudience;
      b.classList.toggle("active", on);
      b.setAttribute("aria-selected", on ? "true" : "false");
      // ● 뱃지 — 내용 있는 탭 표시 (라벨 뒤에 부착/제거)
      const base = b.textContent.replace(/ ●$/, "");
      b.textContent =
        base + ((on ? $dirText.value : dirBuffers[aud]).trim() ? " ●" : "");
    });
  }
  function selectDirTab(aud) {
    if (!aud || aud === dirAudience) return;
    dirSyncActive();
    dirAudience = aud;
    $dirText.value = dirBuffers[aud];
    dirUpdateTabs();
    dirGenLabel();
  }
  if ($dirTabs)
    $dirTabs.addEventListener("click", function (e) {
      const b = e.target.closest("button[data-aud]");
      if (b) selectDirTab(b.getAttribute("data-aud"));
    });

  function loadDirectives() {
    return fetch("api/directives")
      .then(function (r) { return r.json(); })
      .then(function (d) {
        if (dirDirty) return; // a concurrent edit shouldn't overwrite my typing
        const s = (d && d.scopes) || {};
        dirBuffers.common = s.common || "";
        dirBuffers.main = s.main || "";
        dirBuffers.agents = s.agents || "";
        $dirText.value = dirBuffers[dirAudience];
        if ($dirPath) $dirPath.textContent = (d && d.path) || "";
        $dirStatus.textContent = "";
        dirUpdateTabs();
      })
      .catch(function () {});
  }
  function saveDirectives() {
    dirSyncActive();
    $dirSave.disabled = true;
    $dirStatus.textContent = "Saving…";
    fetch("api/directives", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ scopes: dirBuffers }),
    })
      .then(function (r) {
        if (!r.ok) throw new Error(r.status);
        dirDirty = false;
        $dirStatus.textContent = "✓ Saved — applies on the next LLM call";
      })
      .catch(function () { $dirStatus.textContent = "✗ Save failed"; })
      .finally(function () { $dirSave.disabled = false; });
  }
  // Cancel: discard unsaved edits by re-loading the file's current content back
  // into the buffers. Clear dirDirty FIRST so loadDirectives overwrites.
  function cancelDirectives() {
    dirDirty = false;
    loadDirectives().then(function () {
      $dirStatus.textContent = "↩ Canceled — original restored";
    });
  }
  if ($dirSave) $dirSave.addEventListener("click", saveDirectives);
  if ($dirCancel) $dirCancel.addEventListener("click", cancelDirectives);

  // ✨ 생성 — brief → 요청 시점 탭의 directive 초안 (기존 내용 병합/개정).
  // 별도 run 프로세스라 탭별 동시 생성 가능; 같은 탭 이중 생성만 막는다.
  const dirGenPending = { common: false, main: false, agents: false };
  function dirGenLabel() {
    if (!$dirGen) return;
    const busy = Object.keys(dirGenPending).filter(function (k) { return dirGenPending[k]; });
    $dirGen.disabled = dirGenPending[dirAudience];
    $dirGen.textContent = busy.length
      ? "✨ Generate (" + busy.length + " running…)"
      : "✨ Generate";
  }
  function generateDirective() {
    const brief = ($dirBrief.value || "").trim();
    if (!brief) {
      $dirStatus.textContent = "· Describe what you want to include first";
      return;
    }
    dirSyncActive();
    const aud = dirAudience; // 요청 시점 탭 고정 — 완료 시 이 버퍼에만 반영
    if (dirGenPending[aud]) return;
    dirGenPending[aud] = true;
    $dirBrief.value = "";
    dirGenLabel();
    $dirStatus.textContent = "✨ [" + aud + "] drafting — this can take tens of seconds";
    fetch("api/directives/generate", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ audience: aud, brief: brief, current: dirBuffers[aud] }),
    })
      .then(function (r) {
        if (!r.ok) return r.json().then(function (e) { throw new Error((e && e.detail) || r.status); });
        return r.json();
      })
      .then(function (d) {
        if (d && d.content) {
          dirBuffers[aud] = d.content;
          if (dirAudience === aud) $dirText.value = d.content;
          dirDirty = true; // unsaved — review, then save/cancel
          $dirStatus.textContent = "✨ [" + aud + "] draft applied — review then save";
          dirUpdateTabs();
        }
      })
      .catch(function (e) { $dirStatus.textContent = "✗ [" + aud + "] generation failed: " + e.message; })
      .finally(function () {
        dirGenPending[aud] = false;
        dirGenLabel();
      });
  }
  if ($dirGen) $dirGen.addEventListener("click", generateDirective);
  if ($dirBrief)
    $dirBrief.addEventListener("keydown", function (e) {
      if (e.key === "Enter") generateDirective();
    });
  if ($dirText)
    $dirText.addEventListener("input", function () {
      dirDirty = true;
      $dirStatus.textContent = "● Unsaved";
      dirUpdateTabs();
    });

  // Directives changed on disk (a save elsewhere) → re-sync the editor.
  window.addEventListener("agentcli:directives-changed", function () {
    if ($drawer.classList.contains("open")) loadDirectives();
  });

  function open() {
    $backdrop.hidden = false;
    requestAnimationFrame(function () {
      $backdrop.classList.add("open");
      $drawer.classList.add("open");
    });
    $drawer.setAttribute("aria-hidden", "false");
    loadDirectives();
  }
  function close() {
    $backdrop.classList.remove("open");
    $drawer.classList.remove("open");
    $drawer.setAttribute("aria-hidden", "true");
    setTimeout(function () { $backdrop.hidden = true; }, 260);
  }
  $btn.addEventListener("click", function () {
    if ($drawer.classList.contains("open")) close();
    else open();
  });
  const $deClose = document.getElementById("de-close");
  if ($deClose) $deClose.addEventListener("click", close);
  $backdrop.addEventListener("click", close);
  document.addEventListener("keydown", function (e) {
    if (e.key === "Escape" && $drawer.classList.contains("open")) close();
  });
}
