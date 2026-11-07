// ── Theme picker (🎨) ───────────────────────────────────────────────
// Self-contained dropdown: the <head> inline script already applied the saved
// (or default) theme to <html data-theme>; this builds the menu, applies a
// pick, and persists it. One source of truth for the theme list + swatches.
var btn = document.getElementById("theme-btn");
var menu = document.getElementById("theme-menu");
if (btn && menu) {
  var root = document.documentElement;
  // swatch = [surface bg, accent] so each row previews the theme at a glance
  var THEMES = [
    { id: "amber", name: "Amber", bg: "#18140f", accent: "#e0a458" },
    { id: "slate", name: "Slate", bg: "#15171c", accent: "#7e8db0" },
    { id: "midnight", name: "Midnight", bg: "#111725", accent: "#4d8eff" },
    { id: "terminal", name: "Terminal", bg: "#101413", accent: "#2dd4bf" },
    { id: "light", name: "Light", bg: "#ffffff", accent: "#6366f1" },
  ];
  function current() {
    var t = root.getAttribute("data-theme");
    return THEMES.some(function (x) { return x.id === t; }) ? t : "amber";
  }
  function apply(id) {
    root.setAttribute("data-theme", id);
    try {
      localStorage.setItem("agentcli_theme", id);
    } catch (e) {
      /* private mode — theme just won't persist */
    }
    render();
  }
  function render() {
    var cur = current();
    menu.innerHTML = "";
    THEMES.forEach(function (t) {
      var item = document.createElement("button");
      item.type = "button";
      item.className = "theme-item" + (t.id === cur ? " active" : "");
      item.setAttribute("role", "menuitem");
      var sw = document.createElement("span");
      sw.className = "theme-swatch";
      // diagonal split: surface → accent
      sw.style.background =
        "linear-gradient(135deg, " + t.bg + " 0 55%, " + t.accent + " 55% 100%)";
      var label = document.createElement("span");
      label.textContent = t.name;
      item.appendChild(sw);
      item.appendChild(label);
      if (t.id === cur) {
        var chk = document.createElement("span");
        chk.className = "theme-check";
        chk.textContent = "✓";
        item.appendChild(chk);
      }
      item.addEventListener("click", function () {
        apply(t.id);
        close();
      });
      menu.appendChild(item);
    });
  }
  function open() {
    render();
    menu.hidden = false;
    btn.setAttribute("aria-expanded", "true");
  }
  function close() {
    menu.hidden = true;
    btn.setAttribute("aria-expanded", "false");
  }
  btn.addEventListener("click", function (e) {
    e.stopPropagation();
    if (menu.hidden) open();
    else close();
  });
  // dismiss on outside click / Escape
  document.addEventListener("click", function (e) {
    if (!menu.hidden && !menu.contains(e.target) && e.target !== btn) close();
  });
  document.addEventListener("keydown", function (e) {
    if (e.key === "Escape" && !menu.hidden) close();
  });
}
