// ─── Workspace files (📁) — one drawer: download (select → zip) + upload
// (drag-drop into the drawer → uploads to the directory clicked in the tree,
// or the workspace root). Drag-OUT download isn't done (browser-restricted to
// Chromium single-files); the select + zip button is the universal path. ───
const $btn = document.getElementById("files-btn");
const $drawer = document.getElementById("download-drawer");
const $backdrop = document.getElementById("download-backdrop");
const $close = document.getElementById("dl-close");
const $tree = document.getElementById("dl-tree");
const $count = document.getElementById("dl-count");
const $go = document.getElementById("dl-download");
const $del = document.getElementById("dl-delete");
const $msg = document.getElementById("dl-msg");
const $drop = document.getElementById("ul-drop");
const $pick = document.getElementById("ul-pick");
const $pickDir = document.getElementById("ul-pick-dir");
const $fileInput = document.getElementById("ul-input");
const $dirInput = document.getElementById("ul-dir-input");
const $target = document.getElementById("ul-target");
if ($btn && $drawer) {

  const selected = new Set();
  // Upload target directory (rel path; "" = workspace root). Set by clicking a
  // directory label in the tree; shown in the dropzone.
  let uploadDir = "";
  let $targetRow = null; // the highlighted dir row
  // The root row's checkbox = "whole workspace" for download (replaces the old
  // separate "All" checkbox). Re-created on each tree (re)render.
  let $rootCb = null;
  const allChecked = () => !!($rootCb && $rootCb.checked);

  function setUploadDir(rel, rowEl) {
    // Target is any tree row, INCLUDING the synthetic root row — so "go back to
    // root" is just clicking root (no ✕, no re-click toggle needed).
    uploadDir = rel || "";
    if ($targetRow) $targetRow.classList.remove("target");
    $targetRow = rowEl || null;
    if ($targetRow) $targetRow.classList.add("target");
    $target.innerHTML =
      "⬆ Upload to: <b>" + (uploadDir ? esc(uploadDir) : "/ (root)") + "</b>";
  }
  const esc = (s) =>
    s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  const fmtSize = (n) =>
    n == null ? "" : n < 1024 ? n + "B" : n < 1048576
      ? (n / 1024).toFixed(0) + "KB" : (n / 1048576).toFixed(1) + "MB";

  function updateCount() {
    if (allChecked()) {
      $count.textContent = "whole workspace";
    } else {
      $count.textContent = selected.size + " selected";
    }
  }

  async function fetchTree(path) {
    const r = await fetch("api/workspace/tree?path=" + encodeURIComponent(path));
    if (!r.ok) throw new Error("tree " + r.status);
    return (await r.json()).entries;
  }

  function makeRow(entry, depth) {
    const row = document.createElement("div");
    row.className = "dl-row";
    row.style.paddingLeft = depth * 16 + "px";
    // changes.js 가 이 두 속성으로 트리 행에 변경 표시를 단다(DOM 으로만 만난다, v10.138.0)
    Object.assign(row.dataset, { rel: entry.rel, type: entry.type });

    const toggle = document.createElement("span");
    toggle.className = "dl-toggle";
    toggle.textContent = entry.type === "dir" ? "▶" : "";

    const cb = document.createElement("input");
    cb.type = "checkbox";
    cb.checked = selected.has(entry.rel);
    cb.addEventListener("change", () => {
      if (cb.checked) selected.add(entry.rel);
      else selected.delete(entry.rel);
      updateCount();
    });

    const label = document.createElement("span");
    label.className = "dl-label";
    // 🔗 밖을 가리키거나 깨진 심링크 (v10.113.1) — 보이되 들어갈 수 없다: 펼침 없음,
    // 선택 안 됨. 판정(밖/깨짐)은 서버가 한다 — 프런트는 파일시스템을 모른다.
    if (entry.type === "link") {
      cb.disabled = true;
      label.innerHTML = `🔗 ${esc(entry.name)}`;
      label.title =
        `symlink → ${entry.target} — ` +
        (entry.broken ? "target does not exist" : "outside the workspace, not listed");
      row.appendChild(toggle);
      row.appendChild(cb);
      row.appendChild(label);
      const linkWrap = document.createElement("div");
      linkWrap.appendChild(row);
      return linkWrap;
    }
    const icon = entry.type === "dir" ? "📁" : "📄";
    const size = ` <span class="dl-size">${fmtSize(entry.size)}</span>`;
    label.innerHTML = `${icon} ${esc(entry.name)}${size}`;

    row.appendChild(toggle);
    row.appendChild(cb);
    row.appendChild(label);

    const wrap = document.createElement("div");
    wrap.appendChild(row);

    if (entry.type === "dir") {
      const kids = document.createElement("div");
      kids.className = "dl-kids";
      let loaded = false;
      const expand = async () => {
        if (kids.childElementCount === 0) {
          try {
            const entries = await fetchTree(entry.rel);
            entries.forEach((e) => kids.appendChild(makeRow(e, depth + 1)));
          } catch (e) {
            $msg.textContent = "Load failed: " + e.message;
          }
        }
      };
      const onToggle = async () => {
        loaded = !loaded;
        toggle.textContent = loaded ? "▼" : "▶";
        kids.style.display = loaded ? "" : "none";
        if (loaded) await expand();
      };
      toggle.style.cursor = "pointer";
      toggle.addEventListener("click", onToggle);
      label.style.cursor = "pointer";
      // Clicking a directory both expands it AND makes it the upload target
      // ("this folder"). To go back to root, click the root row.
      label.addEventListener("click", () => {
        setUploadDir(entry.rel, row);
        onToggle();
      });
      wrap.appendChild(kids);
    }
    return wrap;
  }

  // Synthetic root row at the top of the tree. Its CHECKBOX = "whole
  // workspace" for download (replaces the old separate "All" checkbox); its
  // LABEL click = set the upload target back to root. Both are just the
  // root-level versions of what every dir row already does.
  function makeRootRow(totalSize) {
    const row = document.createElement("div");
    row.className = "dl-row";
    Object.assign(row.dataset, { rel: "", type: "dir" });
    const spacer = document.createElement("span");
    spacer.className = "dl-toggle";
    const cb = document.createElement("input");
    cb.type = "checkbox";
    cb.title = "Download the whole workspace";
    cb.addEventListener("change", () => {
      // checking root subsumes individual selection — dim the rest of the tree
      $tree.classList.toggle("all-selected", cb.checked);
      updateCount();
    });
    $rootCb = cb;
    const label = document.createElement("span");
    label.className = "dl-label";
    label.style.cursor = "pointer";
    // total workspace size = sum of top-level entries (each already recursive)
    const total =
      totalSize != null
        ? ` <span class="dl-size">${fmtSize(totalSize)}</span>`
        : "";
    label.innerHTML =
      "📁 / <span class='dl-size'>(workspace root)</span>" + total;
    label.addEventListener("click", () => setUploadDir("", row));
    row.appendChild(spacer);
    row.appendChild(cb);
    row.appendChild(label);
    return row;
  }

  async function open() {
    $backdrop.hidden = false;
    $backdrop.classList.add("open");
    $drawer.classList.add("open");
    $drawer.setAttribute("aria-hidden", "false");
    selected.clear();
    $tree.classList.remove("all-selected"); // clear a prior whole-workspace dim
    $msg.textContent = "";
    $tree.innerHTML = "<div class='dl-loading'>loading…</div>";
    updateCount();
    try {
      const entries = await fetchTree("");
      $tree.innerHTML = "";
      const rootSize = entries.reduce((s, e) => s + (e.size || 0), 0);
      const rootRow = makeRootRow(rootSize);
      $tree.appendChild(rootRow);
      // top-level entries render at depth 1 so they nest visually under root
      entries.forEach((e) => $tree.appendChild(makeRow(e, 1)));
      setUploadDir("", rootRow); // root selected by default (highlighted)
    } catch (e) {
      $tree.innerHTML = "<div class='dl-loading'>Load failed: " + esc(e.message) + "</div>";
    }
  }

  function close() {
    $backdrop.classList.remove("open");
    $drawer.classList.remove("open");
    $drawer.setAttribute("aria-hidden", "true");
    setTimeout(() => { $backdrop.hidden = true; }, 200);
  }


  async function download() {
    const payload = allChecked()
      ? { all: true }
      : { paths: Array.from(selected) };
    if (!allChecked() && selected.size === 0) {
      $msg.textContent = "No items selected";
      return;
    }
    $go.disabled = true;
    $msg.textContent = "Zipping…";
    try {
      const r = await fetch("api/workspace/download", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      if (!r.ok) {
        let d = "";
        try { d = (await r.json()).detail || ""; } catch (e) {}
        throw new Error(d || ("HTTP " + r.status));
      }
      const blob = await r.blob();
      const cd = r.headers.get("Content-Disposition") || "";
      const m = cd.match(/filename="?([^"]+)"?/);
      const fname = (m && m[1]) || "workspace.zip";
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = fname;
      document.body.appendChild(a);
      a.click();
      a.remove();
      URL.revokeObjectURL(url);
      $msg.textContent = "Download started: " + fname;
    } catch (e) {
      $msg.textContent = "Failed: " + e.message;
    } finally {
      $go.disabled = false;
    }
  }

  async function deleteSelected() {
    const paths = Array.from(selected);
    if (!paths.length) {
      $msg.textContent = "No items selected";
      return;
    }
    // destructive + permanent → always confirm
    const preview =
      paths.length <= 3 ? paths.join(", ") : paths.length + " items";
    if (!confirm(`Delete these? (permanent, cannot be undone)\n${preview}`)) return;
    $del.disabled = true;
    $msg.textContent = "Deleting…";
    try {
      const r = await fetch("api/workspace/delete", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ paths: paths }),
      });
      if (!r.ok) {
        let d = "";
        try { d = (await r.json()).detail || ""; } catch (e) {}
        throw new Error(d || "HTTP " + r.status);
      }
      const res = await r.json();
      selected.clear();
      const errs = (res.errors || []).length;
      $msg.textContent =
        "✓ " + res.deleted.length + " deleted" + (errs ? ` (${errs} failed)` : "");
      refreshTree();
    } catch (e) {
      $msg.textContent = "Failed: " + e.message;
    } finally {
      $del.disabled = false;
    }
  }

  // ── Upload — items are {file, name} where name is the file's path relative
  // to the target dir ("a.txt" for a single file, "mydir/sub/a.c" for a
  // directory upload; the server creates the nested dirs). ─────────────
  function uploadOne(item) {
    const q =
      "api/workspace/upload?name=" +
      encodeURIComponent(item.name) +
      (uploadDir ? "&path=" + encodeURIComponent(uploadDir) : "");
    return fetch(q, { method: "POST", body: item.file }).then((r) =>
      r.json().then((d) => ({ ok: r.ok, status: r.status, d: d }))
    );
  }

  // Recursively walk a dropped FileSystemEntry (dir → its files, keeping the
  // relative path). Entries must be captured synchronously in the drop event;
  // the walk itself is async.
  function readEntries(reader) {
    return new Promise((res, rej) => reader.readEntries(res, rej));
  }
  async function walkEntry(entry, prefix, out) {
    if (entry.isFile) {
      const file = await new Promise((res, rej) => entry.file(res, rej));
      out.push({ file: file, name: prefix + entry.name });
    } else if (entry.isDirectory) {
      const reader = entry.createReader();
      let batch;
      // readEntries returns in chunks; loop until it yields none.
      do {
        batch = await readEntries(reader);
        for (const e of batch) await walkEntry(e, prefix + entry.name + "/", out);
      } while (batch.length);
    }
  }
  async function collectEntries(entries) {
    const out = [];
    for (const ent of entries) await walkEntry(ent, "", out);
    return out;
  }

  async function refreshTree() {
    try {
      const entries = await fetchTree("");
      $tree.innerHTML = "";
      const rootSize = entries.reduce((s, e) => s + (e.size || 0), 0);
      const rootRow = makeRootRow(rootSize);
      $tree.appendChild(rootRow);
      entries.forEach((e) => $tree.appendChild(makeRow(e, 1)));
      setUploadDir("", rootRow);
    } catch (e) {
      /* leave the tree as-is on refresh failure */
    }
  }

  function uploadItems(items) {
    if (!items || !items.length) return;
    const where = uploadDir ? uploadDir + "/" : "(root)";
    $msg.textContent = "Uploading → " + where + " (" + items.length + ")";
    const out = [];
    let done = 0;
    items.forEach((it) => {
      uploadOne(it)
        .then((res) => {
          out.push(
            res.ok
              ? "✓ " + res.d.rel + (res.d.overwritten ? " (overwritten)" : "")
              : "✗ " + esc(it.name) + " — " + (res.d.detail || res.status)
          );
        })
        .catch(() => out.push("✗ " + esc(it.name) + " — network error"))
        .then(() => {
          done += 1;
          if (done === items.length) {
            const failed = out.filter((s) => s[0] === "✗");
            const okCount = out.length - failed.length;
            if (failed.length) {
              // Keep failures visible — the user needs to see what didn't land.
              $msg.innerHTML = out.join("<br>");
            } else {
              // All good: a brief confirmation that auto-clears, so the drawer
              // doesn't keep a stale file list around.
              $msg.textContent = "✓ " + okCount + " uploaded";
              setTimeout(() => {
                $msg.textContent = "";
              }, 2500);
            }
            if (okCount) refreshTree();
          }
        });
    });
  }

  // <input> files → items. Folder picks carry webkitRelativePath ("dir/a.c").
  function itemsFromInput(files) {
    return Array.prototype.slice.call(files || []).map((f) => ({
      file: f,
      name: f.webkitRelativePath || f.name,
    }));
  }

  $btn.addEventListener("click", open);
  $close.addEventListener("click", close);
  $backdrop.addEventListener("click", close);
  $go.addEventListener("click", download);
  if ($del) $del.addEventListener("click", deleteSelected);
  $pick.addEventListener("click", () => $fileInput.click());
  $pickDir.addEventListener("click", () => $dirInput.click());
  $fileInput.addEventListener("change", () => {
    uploadItems(itemsFromInput($fileInput.files));
    $fileInput.value = "";
  });
  $dirInput.addEventListener("change", () => {
    uploadItems(itemsFromInput($dirInput.files)); // webkitRelativePath = dir/...
    $dirInput.value = "";
  });
  // The whole drawer is a drop target; the dropzone shows the active state.
  ["dragenter", "dragover"].forEach((ev) =>
    $drawer.addEventListener(ev, (e) => {
      if (e.dataTransfer && Array.prototype.indexOf.call(e.dataTransfer.types, "Files") >= 0) {
        e.preventDefault();
        $drop.classList.add("over");
      }
    })
  );
  ["dragleave", "drop"].forEach((ev) =>
    $drawer.addEventListener(ev, (e) => {
      if (ev === "drop") e.preventDefault();
      if (ev === "dragleave" && $drawer.contains(e.relatedTarget)) return;
      $drop.classList.remove("over");
    })
  );
  $drawer.addEventListener("drop", (e) => {
    e.preventDefault();
    const dt = e.dataTransfer;
    if (!dt) return;
    // Capture FileSystemEntry objects SYNCHRONOUSLY (only valid during the
    // event) so directories can be walked. Fall back to flat files if the
    // entries API is unavailable.
    let entries = [];
    if (dt.items) {
      entries = Array.prototype.slice
        .call(dt.items)
        .map((it) => (it.webkitGetAsEntry ? it.webkitGetAsEntry() : null))
        .filter(Boolean);
    }
    if (entries.length) {
      collectEntries(entries).then(uploadItems);
    } else {
      uploadItems(
        Array.prototype.slice.call(dt.files || []).map((f) => ({ file: f, name: f.name }))
      );
    }
  });
}
