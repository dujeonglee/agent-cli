// ── 노브 칩 렌더러 (v10.45.0, docs/knobs/DESIGN.md §5.2) ───────────────────
// 칩은 서버의 노브 표에서 나온다: 상태 한 장(kind·ui·badge·note·locked·on·
// group)이 곧 칩이다. 종류(kind)와 컨트롤 변형(ui.control)으로 입력을 고르고,
// 배지·설명·잠김 사유는 서버가 준 글자를 보여줄 뿐 여기서 계산하지 않는다.
// 묶음(ui.group)은 한 칩·한 팝업에 멤버 컨트롤을 나란히 — 배지·설명은 묶음 것.
// 새 노브는 표에 선언 한 줄이면 칩이 생긴다. 종전 칩별 IIFE 일곱(🗜️ 👥 ⏳ 🧠
// 📐 🔎 ⚡)을 대체. id 는 규칙적이다: 칩 `knob-<chip>-{wrap,chip,pop,badge,note}`
// (chip = 묶음 이름 또는 노브 이름), 컨트롤 `knob-<노브 이름>`.
import { KNOBS } from "./knobs_client.js";

const $chips = document.getElementById("knobs");
const $header = document.getElementById("knobs-header");
if ($chips && $header) {

  const chips = {}; // chip name → {wrap, btn, badge, pop, note, members: {knob → control}}
  const states = {}; // knob name → 마지막 상태

  function el(tag, attrs, text) {
    const n = document.createElement(tag);
    Object.keys(attrs || {}).forEach((k) => {
      if (k === "className") n.className = attrs[k];
      else if (attrs[k] !== null && attrs[k] !== undefined) n.setAttribute(k, attrs[k]);
    });
    if (text !== undefined) n.textContent = text;
    return n;
  }
  function chipNameOf(d) {
    return d.group ? d.group.name : d.name;
  }
  function scaleOf(d) {
    return (d.ui && d.ui.scale) || 1;
  }
  // 배율 곱·나눗셈의 부동소수 찌꺼기(600.0000001, 5.000000000000001)를 걷는다.
  function tidy(x) {
    return Math.round(x * 1e6) / 1e6;
  }
  function unscale(d, shown) {
    return tidy(Number(shown) / scaleOf(d)); // 화면 값 → 저장 값
  }
  function post(d, value) {
    KNOBS.set(d.name, value).then((r) => {
      // 거부(미등록 모델 등)·실패면 마지막 상태로 되돌린다 — 서버는 아무것도 안 바꿨다.
      if (!r) apply(states[d.name], true);
    });
  }
  // 조작 중인 입력은 서버 상태로 덮지 않는다 — 한 노브를 바꾸면 서버가 열 장을
  // 다 다시 보내므로, 옆 칸에 막 적은 값이 change 전에 옛 값으로 되돌아가던
  // 경합(브라우저 테스트가 잡음). 거부 되돌리기(force)만 예외.
  function editing($x, force) {
    return !force && document.activeElement === $x;
  }
  function setNumberBounds($in, d) {
    const sc = scaleOf(d);
    if (typeof d.min === "number") $in.min = tidy(d.min * sc);
    if (typeof d.max === "number") $in.max = tidy(d.max * sc);
    if (typeof d.step === "number") $in.step = tidy(d.step * sc);
  }
  function labelOf(d, v) {
    const m = (d.ui && d.ui.choice_labels) || {};
    return Object.prototype.hasOwnProperty.call(m, v) ? m[v] : v;
  }

  // 컨트롤 — {nodes: [라벨, 입력, 단위…], write(d), disable(bool)}
  function makeControl(d) {
    const id = "knob-" + d.name;
    const control = (d.ui && d.ui.control) || "";
    const $label = el("label", { for: id }, d.label);
    const unit = d.ui && d.ui.unit ? el("span", { className: "knob-unit" }, d.ui.unit) : null;
    let nodes, write, disable;

    if (d.kind === "choice" || d.kind === "bool" || control === "options") {
      const $sel = el("select", { id: id, "aria-label": d.label });
      let keys = [];
      function fill(st, force) {
        if (editing($sel, force)) return;
        const current = d.kind === "bool" ? (st.value ? "on" : "off") : String(st.value);
        let opts = d.kind === "bool" ? ["off", "on"] : (st.choices || st.options || []).slice();
        if (opts.indexOf(current) < 0) opts.push(current);
        if (opts.join("\n") !== keys.join("\n")) {
          keys = opts;
          $sel.textContent = "";
          opts.forEach((v) => $sel.appendChild(el("option", { value: v }, labelOf(st, v))));
        }
        $sel.value = current;
      }
      $sel.addEventListener("change", () => {
        if ($sel.disabled) return;
        post(d, d.kind === "bool" ? $sel.value === "on" : $sel.value);
      });
      nodes = [$label, $sel, unit];
      write = fill;
      disable = (b) => ($sel.disabled = b);
    } else if (control === "range") {
      const $in = el("input", { id: id, type: "range", "aria-label": d.label });
      $in.addEventListener("change", () => post(d, unscale(d, $in.value)));
      nodes = [$label, $in, unit];
      write = (st, force) => {
        $label.textContent = st.label + " " + st.badge; // 슬라이더 옆에 지금 값
        if (editing($in, force)) return;
        setNumberBounds($in, st);
        $in.value = tidy(st.value * scaleOf(st));
      };
      disable = (b) => ($in.disabled = b);
    } else if (control === "unlimited") {
      const $in = el("input", { id: id, type: "number", "aria-label": d.label });
      const $chk = el("input", { id: id + "-unlimited", type: "checkbox" });
      const $chkLabel = el("label", { className: "knob-check" });
      $chkLabel.appendChild($chk);
      $chkLabel.appendChild(document.createTextNode("Unlimited"));
      let last = 1; // 무제한 해제 시 되돌릴 마지막 유한값
      $in.addEventListener("change", () => {
        let v = Number($in.value);
        const min = Number($in.min) || 1;
        if (!Number.isFinite(v) || v < min) v = min;
        post(d, unscale(d, v));
      });
      $chk.addEventListener("change", () => post(d, $chk.checked ? 0 : last));
      nodes = [$label, $in, $chkLabel, unit];
      write = (st, force) => {
        const unlimited = st.value === 0;
        $chk.checked = unlimited;
        $in.disabled = unlimited || !!st.locked;
        if (!unlimited) last = st.value;
        if (editing($in, force)) return;
        setNumberBounds($in, st);
        if (!unlimited) $in.value = tidy(st.value * scaleOf(st));
      };
      disable = (b) => ($chk.disabled = b);
    } else if (control === "optional") {
      // 🎲 (v10.69.0): 값이 없음(auto)이 뜻을 가지는 숫자 — 숫자 입력 + auto 체크박스.
      // auto 를 풀면 서버가 준 `pick`(지금 실효값, 없으면 1.0)을 보내 동작이 그 순간
      // 바뀌지 않는다; 다시 켜면 "auto" 를 보내 키를 지운다. 배지·설명(출처)은 서버 것.
      const $in = el("input", { id: id, type: "number", "aria-label": d.label });
      const $chk = el("input", { id: id + "-auto", type: "checkbox" });
      const $chkLabel = el("label", { className: "knob-check" });
      $chkLabel.appendChild($chk);
      $chkLabel.appendChild(document.createTextNode("auto"));
      let pick = 1;
      $in.addEventListener("change", () => {
        if ($in.disabled || $in.value === "") return;
        post(d, unscale(d, $in.value));
      });
      $chk.addEventListener("change", () => post(d, $chk.checked ? "auto" : pick));
      nodes = [$label, $in, $chkLabel, unit];
      write = (st, force) => {
        const auto = st.value === null || st.value === undefined;
        if (typeof st.pick === "number") pick = st.pick;
        $chk.checked = auto;
        $in.disabled = auto || !!st.locked;
        if (editing($in, force)) return;
        setNumberBounds($in, st);
        const shown = auto ? st.effective : st.value;
        $in.value = typeof shown === "number" ? tidy(shown * scaleOf(st)) : "";
        $in.placeholder = auto && typeof shown !== "number" ? "server default" : "";
      };
      disable = (b) => ($chk.disabled = b);
    } else if (d.kind === "number" || d.kind === "duration") {
      const $in = el("input", { id: id, type: "number", "aria-label": d.label });
      $in.addEventListener("change", () => {
        if ($in.disabled) return;
        post(d, unscale(d, $in.value === "" ? 0 : $in.value));
      });
      nodes = [$label, $in, unit];
      write = (st, force) => {
        if (editing($in, force)) return;
        setNumberBounds($in, st);
        $in.value = tidy(st.value * scaleOf(st));
      };
      disable = (b) => ($in.disabled = b);
    } else {
      const $in = el("input", { id: id, type: "text", "aria-label": d.label });
      $in.addEventListener("change", () => post(d, $in.value));
      nodes = [$label, $in, unit];
      write = (st, force) => {
        if (!editing($in, force)) $in.value = st.value === null ? "" : String(st.value);
      };
      disable = (b) => ($in.disabled = b);
    }
    return { nodes: nodes.filter(Boolean), write: write, disable: disable };
  }

  // 헤더 자리(ui.placement="header") — 팝업 없는 토글 버튼 하나 (⚡ 자동 승인).
  function buildToggle(d) {
    const $btn = el("button", {
      id: "knob-" + d.name,
      className: "hd-chip hd-chip-btn knob-toggle",
      type: "button",
      "aria-pressed": "false",
      hidden: "",
    });
    $btn.addEventListener("click", () => post(d, !states[d.name].value));
    $header.appendChild($btn);
    return { wrap: $btn, toggle: true };
  }

  function buildChip(d) {
    const name = chipNameOf(d);
    const g = d.group;
    const $wrap = el("span", { id: "knob-" + name + "-wrap", className: "knob", hidden: "" });
    const $badge = el("span", { id: "knob-" + name + "-badge" });
    const $btn = el("button", {
      id: "knob-" + name + "-chip",
      className: "knob-btn",
      type: "button",
      "data-pop": "knob-" + name + "-pop",
      "aria-haspopup": "true",
      "aria-expanded": "false",
    });
    $btn.appendChild(document.createTextNode(((g ? g.icon : d.ui.icon) || "") + " "));
    $btn.appendChild($badge);
    const $pop = el("div", {
      id: "knob-" + name + "-pop",
      className: "knob-pop" + (g ? " is-group" : ""),
      hidden: "",
    });
    const $note = el("span", { id: "knob-" + name + "-note", className: "knob-note" });
    $wrap.appendChild($btn);
    $wrap.appendChild($pop);
    $chips.appendChild($wrap);
    return { wrap: $wrap, btn: $btn, badge: $badge, pop: $pop, note: $note, members: {} };
  }

  function apply(d, force) {
    if (!d) return;
    states[d.name] = d;
    const name = d.ui && d.ui.placement === "header" ? d.name : chipNameOf(d);
    let chip = chips[name];
    if (!chip) {
      chip = chips[name] = d.ui && d.ui.placement === "header" ? buildToggle(d) : buildChip(d);
    }
    if (chip.toggle) {
      chip.wrap.hidden = !d.available;
      chip.wrap.textContent = (d.ui.icon || "") + " " + (d.badge || d.label);
      chip.wrap.title = d.locked || d.help;
      chip.wrap.classList.toggle("on", !!d.on);
      chip.wrap.setAttribute("aria-pressed", String(!!d.value));
      chip.wrap.disabled = !!d.locked;
      return;
    }
    let ctl = chip.members[d.name];
    if (!ctl) {
      ctl = chip.members[d.name] = makeControl(d);
      ctl.nodes.forEach((n) => chip.pop.appendChild(n));
      chip.pop.appendChild(chip.note); // 설명 줄은 항상 맨 아래
    }
    ctl.write(d, force);
    ctl.disable(!!d.locked);
    // 묶음이면 배지·설명·가시성은 묶음 것(서버가 멤버 전부를 보고 계산), 아니면 제 것.
    const members = d.group ? d.group.members.map((m) => states[m]).filter(Boolean) : [d];
    const badge = d.group ? d.group.badge : d.badge;
    const note = d.group ? d.group.note : d.note;
    const available = d.group ? d.group.badge !== null : d.available;
    const locked = members.every((m) => m.locked);
    chip.wrap.hidden = !available;
    chip.badge.textContent = badge === null || badge === undefined ? "" : String(badge);
    chip.note.textContent = note || "";
    chip.note.hidden = !note;
    chip.wrap.title = locked
      ? Array.from(new Set(members.map((m) => m.locked))).join(" · ") // 같은 사유는 한 번
      : d.group
        ? d.group.help
        : d.help;
    chip.wrap.classList.toggle("is-disabled", locked);
    chip.wrap.classList.toggle("is-on", members.some((m) => m.on));
  }

  KNOBS.all().then((ks) => ks.forEach(apply));
  document.addEventListener("agentcli:knob", (e) => apply(e.detail));
}
