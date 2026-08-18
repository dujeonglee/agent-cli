// agent-cli web — vanilla JS client.
//
// Responsibilities:
//   1. Open SSE to /api/stream with the token-from-URL.
//   2. Render incoming events as cards in #messages.
//   3. Handle three input modes (chat / prompt / confirm) driven by
//      ``input_required`` events from the server.
//   4. POST the user's response back to /api/input.
//
// No build step, no framework — single file, ~300 LOC. Polish (markdown
// rendering, syntax highlighting, abort button) is Phase D.

(function () {
  "use strict";

  // ── Bootstrap token → cookie ───────────────
  // The index response set an HttpOnly ``act`` cookie from the one-time
  // ``?token=`` URL, so every request (and the SSE stream, which cannot send
  // headers) authenticates via that cookie — the token never rides in a
  // per-request URL. Strip it from the address bar. If we hold no valid cookie,
  // the SSE handshake fails and ``showSetupHelp`` (below) explains what to do.
  const params = new URLSearchParams(window.location.search);
  if (params.has("token")) {
    params.delete("token");
    const q = params.toString();
    history.replaceState(
      null,
      "",
      window.location.pathname + (q ? "?" + q : "") + window.location.hash
    );
  }

  function showSetupHelp() {
    document.body.innerHTML =
      '<div class="setup-message">' +
      "<h1>agent-cli web</h1>" +
      "<p>Not authenticated. Open the URL printed by " +
      "<code>agent-cli web</code> — it carries a one-time " +
      "<code>?token=</code> that installs your session cookie.</p>" +
      "</div>";
  }

  // ── DOM refs ───────────────────────────────
  const $messages = document.getElementById("messages");
  const $input = document.getElementById("input");
  const $send = document.getElementById("send");
  const $chatStop = document.getElementById("chat-stop");
  const $abort = document.getElementById("abort");
  const $info = document.getElementById("info");
  const $tokenUsage = document.getElementById("token-usage");
  const $status = document.getElementById("conn-status");
  const $modeBadge = document.getElementById("input-mode-badge");
  const $inputArea = document.getElementById("input-area");

  // ── State ──────────────────────────────────
  let currentMode = "chat"; // "chat" | "prompt" | "confirm"
  let confirmDefaultKey = null;
  // Every connection is equal (all may send input / queue). ``myConnId`` (from
  // the ``identity`` event) is used to mark "(you)" in the viewer roster and
  // to own queued messages.
  let myConnId = null;
  let streamingCard = null;
  let streamingText = "";
  // ``workerBusy`` mirrors the server's ``worker_state`` event: true
  // means the chat worker is between popping a user message and
  // returning to the next ``dequeue_blocking`` call. While busy,
  // the chat ``Send`` button stays disabled so a second message
  // can't be queued into an in-flight turn. The prompt-mode answer
  // path is not gated by this flag — answering an ``ask`` is the
  // expected way to *unblock* the worker, not an additional message.
  // Refresh / reconnect uses the server's ``_latest_worker_state``
  // snapshot prepend, so this flag is set from the very first event
  // a fresh client receives.
  let workerBusy = false;
  // True between clicking "Stop" and the worker actually returning to
  // idle. While set, the button shows "Stopping…" and is disabled so a
  // second click can't fire a redundant /api/stop. Reset on the next
  // worker_state event (idle = the turn ended; busy = a fresh turn).
  let stopRequested = false;

  // True when the Send button is acting as a Stop button: chat mode +
  // worker busy. In that state a click POSTs /api/stop instead of
  // sending, halting the in-flight turn at the next turn boundary
  // (server.trigger_stop → run_loop stop_event). Enter is NOT wired to
  // stop — the button is the deliberate affordance, so a stray Enter
  // can't abort a run by accident.
  // Whether a chat run is in flight (Stop button shown). Send is ALWAYS
  // available in chat mode now — typing while busy QUEUES the message
  // (injected at the next turn boundary), so Stop is a separate button.
  function isBusyChat() {
    return currentMode === "chat" && workerBusy;
  }

  function updateSendEnabled() {
    // Send is always enabled (chat idle → starts a run; chat busy → queues;
    // prompt/confirm → answers). Stop is a SEPARATE button shown only while a
    // chat run is in flight.
    const busy = isBusyChat();
    const stopping = busy && stopRequested;
    $send.disabled = false;
    $send.textContent = "Send";
    if ($chatStop) {
      $chatStop.hidden = !busy;
      $chatStop.disabled = stopping;
      $chatStop.textContent = stopping ? "Stopping…" : "Stop";
    }
    $input.placeholder =
      currentMode === "prompt"
        ? "Type your answer — Enter to send"
        : currentMode === "confirm"
          ? "Optional comment (empty = no comment)"
          : busy
            ? "Worker is processing… your message will be queued (injected next turn)"
            : "Type a message — Enter to send, Shift+Enter for newline";
  }

  // ── HTML escaping + minimal markdown ───────
  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, function (c) {
      return {
        "&": "&amp;",
        "<": "&lt;",
        ">": "&gt;",
        '"': "&quot;",
        "'": "&#39;",
      }[c];
    });
  }

  /** Colour a write_file/edit_file observation body. ``format_diff``
   * now emits a PLAIN standard unified diff (no Rich markup — the LLM
   * observation stays clean), so the colour is applied here by reading
   * each line's leading character, mirroring the CLI's
   * ``_colorize_diff_line``. Input MUST already be ``escapeHtml``-ed.
   * Only the diff block (from the ``--- a/`` header onward) is coloured;
   * preceding lines like "File saved: …" pass through untouched. */
  function colorizeDiffBody(escaped) {
    let inDiff = false;
    return escaped
      .split("\n")
      .map(function (line) {
        if (line.startsWith("--- ") || line.startsWith("+++ ")) {
          inDiff = true;
          return '<span class="rich-bold">' + line + "</span>";
        }
        if (line.startsWith("@@")) {
          inDiff = true;
          return '<span class="rich-cyan">' + line + "</span>";
        }
        if (inDiff && line.startsWith("+")) {
          return '<span class="rich-green">' + line + "</span>";
        }
        if (inDiff && line.startsWith("-")) {
          return '<span class="rich-red">' + line + "</span>";
        }
        return line; // context / blank / non-diff line — plain
      })
      .join("\n");
  }

  /** Extract fenced code blocks (``` … ```), replacing each with a
   * placeholder comment so subsequent inline/block markdown passes
   * can't munge the content. Returns ``{ stripped, blocks }`` where
   * ``stripped`` contains the placeholders and ``blocks[i].html`` is
   * the pre-rendered ``<pre><code>`` to splice back in.
   *
   * Pre-rendering at extraction time means the placeholder is a
   * sealed leaf — restore is a literal string replace. Input must be
   * already-escaped HTML; the code body inside fences IS the escaped
   * text, so no further escaping is needed when we wrap it. */
  function extractCodeFences(s) {
    const blocks = [];
    const stripped = s.replace(
      /```([\w-]*)\n([\s\S]*?)```/g,
      function (_m, _lang, code) {
        const token = "<!--cf:" + blocks.length + "-->";
        blocks.push({
          token: token,
          html: '<pre class="code"><code>' + code + "</code></pre>",
        });
        return token;
      }
    );
    return { stripped: stripped, blocks: blocks };
  }

  function restoreCodeFences(s, blocks) {
    let html = s;
    for (const b of blocks) {
      html = html.split(b.token).join(b.html);
    }
    return html;
  }

  /** Scan the input line-by-line and replace contiguous GFM pipe-table
   * runs (header row + ``---`` separator row + body rows) with a
   * single ``<table>`` block. Lines that don't fit the pattern pass
   * through untouched.
   *
   * Alignment specifiers (``:--``, ``:--:``, ``--:``) are out of
   * scope for v1 — the separator row just has to look like a
   * separator. */
  function renderTables(s) {
    const lines = s.split("\n");
    const out = [];
    let i = 0;
    while (i < lines.length) {
      const headerLine = lines[i];
      if (i + 1 < lines.length && /^\s*\|.*\|\s*$/.test(headerLine)) {
        const sepLine = lines[i + 1];
        if (/^\s*\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)+\|?\s*$/.test(sepLine)) {
          const headerCells = splitTableRow(headerLine);
          const bodyRows = [];
          let j = i + 2;
          while (j < lines.length && /^\s*\|.*\|\s*$/.test(lines[j])) {
            bodyRows.push(splitTableRow(lines[j]));
            j++;
          }
          let table = "<table><thead><tr>";
          for (const c of headerCells) {
            table += "<th>" + c + "</th>";
          }
          table += "</tr></thead><tbody>";
          for (const row of bodyRows) {
            table += "<tr>";
            for (const c of row) {
              table += "<td>" + c + "</td>";
            }
            table += "</tr>";
          }
          table += "</tbody></table>";
          out.push(table);
          i = j;
          continue;
        }
      }
      out.push(headerLine);
      i++;
    }
    return out.join("\n");
  }

  function splitTableRow(line) {
    // Strip leading/trailing pipe then split on remaining pipes. Cells
    // are trimmed to avoid leading-space artefacts but their content
    // stays as-is (already HTML-escaped upstream).
    let trimmed = line.trim();
    if (trimmed.startsWith("|")) trimmed = trimmed.slice(1);
    if (trimmed.endsWith("|")) trimmed = trimmed.slice(0, -1);
    return trimmed.split("|").map(function (c) {
      return c.trim();
    });
  }

  /** ATX headings: ``# H1`` / ``## H2`` / ``### H3``. ``####`` and
   * deeper are left as literal text (FR-MD-1). The regex is anchored
   * to line start with the ``m`` flag so headers inside paragraphs
   * don't accidentally match. */
  function renderHeadings(s) {
    return s.replace(/^(#{1,3})\s+(.+?)\s*$/gm, function (_m, hashes, body) {
      const level = hashes.length;
      return "<h" + level + ">" + body + "</h" + level + ">";
    });
  }

  /** Group consecutive ``-`` / ``*`` / ``\d+.`` lines into ``<ul>`` /
   * ``<ol>``. A blank line ends the group. Unordered and ordered
   * markers are not mixed mid-group — switching markers starts a
   * fresh list. Nested lists are out of scope (FR-MD-4). */
  function renderLists(s) {
    const lines = s.split("\n");
    const out = [];
    let i = 0;
    while (i < lines.length) {
      const line = lines[i];
      const ulMatch = /^\s*[-*]\s+(.*)$/.exec(line);
      const olMatch = /^\s*\d+\.\s+(.*)$/.exec(line);
      if (ulMatch) {
        const items = [ulMatch[1]];
        let j = i + 1;
        while (j < lines.length) {
          const m = /^\s*[-*]\s+(.*)$/.exec(lines[j]);
          if (!m) break;
          items.push(m[1]);
          j++;
        }
        out.push("<ul>" + items.map(function (x) {
          return "<li>" + x + "</li>";
        }).join("") + "</ul>");
        i = j;
      } else if (olMatch) {
        const items = [olMatch[1]];
        let j = i + 1;
        while (j < lines.length) {
          const m = /^\s*\d+\.\s+(.*)$/.exec(lines[j]);
          if (!m) break;
          items.push(m[1]);
          j++;
        }
        out.push("<ol>" + items.map(function (x) {
          return "<li>" + x + "</li>";
        }).join("") + "</ol>");
        i = j;
      } else {
        out.push(line);
        i++;
      }
    }
    return out.join("\n");
  }

  /** Bold (``**…**``) then italic (``*…*``). Bold first so the
   * leftover single ``*`` characters that bracket italics can't
   * eat the inner ``*`` of a bold pair. The italic regex requires a
   * non-``*`` prefix character (or start-of-string) so it doesn't
   * fire on the middle ``*`` of ``***``. */
  function renderEmphasis(s) {
    let html = s.replace(/\*\*([^*\n]+?)\*\*/g, "<strong>$1</strong>");
    html = html.replace(
      /(^|[^*])\*([^*\n]+?)\*(?!\*)/g,
      "$1<em>$2</em>"
    );
    return html;
  }

  /** Pipeline orchestrator — runs block-level transforms (table,
   * headings, lists) before inline ones (emphasis, inline code) so
   * inline regexes never see header / list markers. */
  function markdownInline(s) {
    let html = renderTables(s);
    html = renderHeadings(html);
    html = renderLists(html);
    html = renderEmphasis(html);
    html = html.replace(/`([^`\n]+)`/g, "<code>$1</code>");
    return html;
  }

  /** Apply a tiny subset of markdown — fenced code blocks, headings
   * (h1-h3), GFM tables, lists, bold/italic, and inline code.
   * Everything else stays as escaped text. No external library;
   * variants beyond this set are intentionally out of scope
   * (NFR-MD-1: zero new JS deps).
   *
   * Order is load-bearing for XSS safety (NFR-MD-2): escapeHtml runs
   * first so every ``<`` becomes ``&lt;``, then fences are extracted
   * to placeholders (so markdown passes don't fire inside code), then
   * block + inline transforms run on the stripped body, and finally
   * fences are restored as pre-rendered ``<pre><code>`` blocks. */
  function escapeAndFormat(s) {
    const escaped = escapeHtml(s);
    const { stripped, blocks } = extractCodeFences(escaped);
    const transformed = markdownInline(stripped);
    return restoreCodeFences(transformed, blocks);
  }

  // ── DOM helpers ────────────────────────────
  function el(tag, classes, html) {
    const e = document.createElement(tag);
    if (classes && classes.length) e.classList.add.apply(e.classList, classes);
    if (html !== undefined && html !== null) e.innerHTML = html;
    return e;
  }

  // ── Card timestamps ────────────────────────
  // Server stamps every event with `ts` (epoch seconds) at emit time; the
  // browser formats to its own local time. Short form on the card
  // (YYMMDD HH:MM:SS), full form (with ms) in the hover tooltip.
  function pad2(n) {
    return String(n).padStart(2, "0");
  }
  // `ts` is epoch seconds (live `_emit`) or an ISO string (resume replay,
  // from the history record). Normalise both to a Date.
  function tsToDate(ts) {
    return typeof ts === "number" ? new Date(ts * 1000) : new Date(ts);
  }
  function fmtCardTime(ts) {
    const d = tsToDate(ts);
    return (
      pad2(d.getFullYear() % 100) + pad2(d.getMonth() + 1) + pad2(d.getDate()) +
      " " + pad2(d.getHours()) + ":" + pad2(d.getMinutes()) + ":" + pad2(d.getSeconds())
    );
  }
  function fmtCardTimeFull(ts) {
    const d = tsToDate(ts);
    return (
      d.getFullYear() + "-" + pad2(d.getMonth() + 1) + "-" + pad2(d.getDate()) +
      " " + pad2(d.getHours()) + ":" + pad2(d.getMinutes()) + ":" + pad2(d.getSeconds()) +
      "." + String(d.getMilliseconds()).padStart(3, "0")
    );
  }
  // Attach a muted corner timestamp to any `.card`. No-op when `ts` is absent
  // (e.g. legacy buffered events) so nothing breaks if the field is missing.
  function stampCard(cardEl, ts) {
    if (ts == null) return cardEl;
    const t = el("span", ["card-time"], escapeHtml(fmtCardTime(ts)));
    t.title = fmtCardTimeFull(ts);
    cardEl.appendChild(t);
    return cardEl;
  }

  // Auto-scroll follows the bottom while the user is parked there,
  // but yields the moment they scroll up to read something —
  // standard chat behaviour. Re-enables itself when the user returns
  // to within ``SCROLL_BOTTOM_THRESHOLD`` of the bottom edge.
  let autoScrollEnabled = true;
  const SCROLL_BOTTOM_THRESHOLD = 50; // px tolerance

  function isAtBottom() {
    const dist =
      $messages.scrollHeight - $messages.scrollTop - $messages.clientHeight;
    return dist <= SCROLL_BOTTOM_THRESHOLD;
  }

  function scrollToBottom() {
    if (!autoScrollEnabled) return;
    $messages.scrollTop = $messages.scrollHeight;
  }

  $messages.addEventListener("scroll", function () {
    // Updating the flag from the scroll handler covers both user
    // wheel/touch input AND our own programmatic scrollTop write —
    // either way the new position is what determines whether the
    // next emit should keep following.
    autoScrollEnabled = isAtBottom();
  });

  // ── Delegate task groups (collapsible cards) ──
  //
  // Parallel delegate workers (one per ``delegate({tasks:[...]})``
  // entry) get their own collapsible card. Every event the worker
  // emits — assistant_turn, observation, stream_chunk, error —
  // carries ``task_id`` (auto-attached by ``WebRenderer._emit``),
  // which routes the card into the matching group's body instead of
  // the main timeline. Without this routing the parallel work would
  // interleave and the user couldn't tell which task is doing what.
  //
  // Group state per task_id: { card, header, body, statusEl,
  // streamingCard, streamingText, closed }.
  const taskGroups = {};
  // task_id → enclosing scope's task_id ("" = top level). Outlives the group
  // entry (which ``closeTaskGroup`` drops) because the CARDS stay in the
  // timeline: swimlane click-navigation still has to expand a finished
  // ancestor chain to reveal a nested card.
  const scopeParent = {};
  // ancestor task_id → [{id, label}] of its currently-running descendants.
  // A collapsed parent would otherwise hide live nested work completely.
  const liveKids = {};

  function scopeAncestors(taskId) {
    const chain = [];
    let cur = scopeParent[taskId];
    while (cur && chain.indexOf(cur) < 0) {
      chain.push(cur);
      cur = scopeParent[cur];
    }
    return chain;
  }

  /** Paint (or clear) the "▸ child running" hint on one ancestor header. This
   * is a DEDICATED element, not the status line: ``scope_status`` targets the
   * innermost scope of its own thread, so a parallel child and its parent can
   * both be writing status at the same time. */
  function refreshLiveKids(taskId) {
    const g = taskGroups[taskId];
    if (!g) return;
    const kids = liveKids[taskId] || [];
    const last = kids.length ? kids[kids.length - 1].label : "";
    g.subEl.textContent = kids.length
      ? "▸ " + last + (kids.length > 1 ? " +" + (kids.length - 1) : "") + " 실행 중"
      : "";
  }

  function noteScopeStart(taskId, parent, label) {
    scopeParent[taskId] = parent || "";
    scopeAncestors(taskId).forEach(function (a) {
      (liveKids[a] = liveKids[a] || []).push({ id: taskId, label: label });
      refreshLiveKids(a);
    });
  }

  function noteScopeEnd(taskId) {
    scopeAncestors(taskId).forEach(function (a) {
      const list = liveKids[a];
      if (!list) return;
      liveKids[a] = list.filter(function (x) {
        return x.id !== taskId;
      });
      refreshLiveKids(a);
    });
  }

  function ensureTaskGroup(taskId, index, agent, taskText, kind, parent) {
    if (taskGroups[taskId]) return taskGroups[taskId];

    const card = el("div", ["card", "card-task-group"]);
    card.dataset.taskId = taskId;
    card.dataset.kind = kind || "run";

    const header = el("div", ["task-header"]);
    const chevron = el("span", ["task-chevron"], "▶");
    const title = el("span", ["task-title"]);
    // Scope card title adapts to kind: a skill subloop (🪄 label) vs a
    // delegate/one-shot worker (🦀 [n] agent: task).
    if (kind === "skill") {
      title.textContent = "🪄 " + taskText;
    } else {
      const label = agent ? agent + ": " + taskText : taskText;
      title.textContent = "🦀 [" + (index + 1) + "] " + label;
    }
    const statusEl = el("span", ["task-status"], "starting…");
    const subEl = el("span", ["task-sub"]);
    const meta = el("span", ["task-meta"]);
    // 🔍 프롬프트 인스펙션 (재설계): 이 agent/skill 스코프의 프롬프트를 연다.
    // 카드가 이미 data-task-id 를 들고 있어(카드=스냅샷 scope_id 통일) 바로
    // 조회 가능. 클릭이 카드 접기 토글로 번지지 않게 stopPropagation.
    const inspectBtn = el("button", ["task-inspect"], "🔍");
    inspectBtn.type = "button";
    inspectBtn.title = "프롬프트 인스펙션";
    inspectBtn.addEventListener("click", function (e) {
      e.stopPropagation();
      if (window.__openInspector) {
        const label =
          kind === "skill"
            ? taskText
            : agent
              ? agent + ": " + taskText
              : taskText;
        window.__openInspector(taskId, label, kind || "run");
      }
    });
    header.appendChild(chevron);
    header.appendChild(title);
    header.appendChild(subEl);
    header.appendChild(statusEl);
    header.appendChild(meta);
    header.appendChild(inspectBtn);

    const body = el("div", ["task-body"]);
    body.hidden = true; // default collapsed

    // Collapse from ANY position (not just the top arrow):
    //   1. the whole header row toggles (chevron is decorative),
    //   2. the header is CSS-sticky, so on a long expanded card it
    //      stays pinned at the top of the viewport — one click to
    //      collapse no matter how far you've scrolled into the body,
    //   3. clicking the body's own padding/gutter also collapses
    //      (``e.target === body`` only — nested cards, links, and text
    //      selection inside the body are untouched).
    function toggleTaskGroup() {
      body.hidden = !body.hidden;
      chevron.textContent = body.hidden ? "▶" : "▼";
      // NOTE: no scrollIntoView here. The header is CSS-sticky (top:0), so
      // it already stays reachable when a long body expands — the earlier
      // `scrollIntoView({behavior:"smooth"})` became both REDUNDANT and
      // HARMFUL once sticky was added (v7.13.0): smooth-scrolling toward a
      // sticky element re-computes a moving target and thrashes against the
      // streaming `scrollToBottom`, freezing the UI on a live delegate card.
    }

    header.addEventListener("click", toggleTaskGroup);
    body.addEventListener("click", function (e) {
      // Only the body's own surface (padding/gutter) collapses — never a
      // click that lands on nested content the user is reading/selecting.
      if (e.target === body) toggleTaskGroup();
    });

    card.appendChild(header);
    card.appendChild(body);
    // NEST the card in its parent scope's body (a skill that runs another skill
    // or an agent). Falls back to the timeline root when the parent is unknown
    // (top-level scope, or its group already closed) — same as any other event.
    appendToTimeline(card, parent);

    const group = {
      card: card,
      header: header,
      body: body,
      chevron: chevron,
      statusEl: statusEl,
      subEl: subEl,
      meta: meta,
      streamingCard: null,
      streamingText: "",
      closed: false,
      toggle: toggleTaskGroup,
    };
    taskGroups[taskId] = group;
    scrollToBottom();
    return group;
  }

  function updateTaskStatus(taskId, status) {
    const g = taskGroups[taskId];
    if (!g || g.closed) return;
    g.statusEl.textContent = status;
  }

  function closeTaskGroup(taskId, success, durationS, error) {
    const g = taskGroups[taskId];
    if (!g) return;
    g.closed = true;
    g.statusEl.textContent = ""; // live status no longer relevant
    g.subEl.textContent = ""; // nor a nested-child hint
    delete liveKids[taskId];
    g.card.classList.add(success ? "task-ok" : "task-fail");
    const icon = success ? "✓" : "✗";
    const dur = durationS != null ? " (" + durationS.toFixed(1) + "s)" : "";
    g.meta.textContent = icon + dur;
    if (!success && error) {
      const errEl = el("div", ["task-error"], escapeHtml(error));
      g.body.appendChild(errEl);
    }
    // Drop the streaming card if the task ended mid-stream — the
    // structured event(s) for the final turn have already replaced
    // it on the body, or won't arrive at all.
    if (g.streamingCard) {
      g.streamingCard.remove();
      g.streamingCard = null;
      g.streamingText = "";
    }
    // Release the global entry now that the task is done. The card's
    // DOM stays in the timeline (still visible + expandable via its own
    // header listener); only this bookkeeping reference is dropped so
    // ``taskGroups`` doesn't grow unbounded over a long session and no
    // stale entry lingers for a future task_id to collide with. No more
    // worker events arrive for this task_id after ``delegate_task_end``.
    delete taskGroups[taskId];
  }

  /** Append ``cardEl`` to either the main timeline or a task group's
   * body, based on ``taskId``. If the task group hasn't been
   * registered yet (event raced before ``delegate_task_start``), the
   * card falls back to the main timeline so it isn't dropped. */
  function appendToTimeline(cardEl, taskId) {
    if (taskId && taskGroups[taskId]) {
      taskGroups[taskId].body.appendChild(cardEl);
    } else {
      $messages.appendChild(cardEl);
    }
  }

  // Inline context-compaction marker. `start` drops a "압축 중…" system line;
  // `done`/`warning` update that same line in place (tracked per scope so a
  // delegate subagent's compaction updates its own line, not main's). The
  // marker is transient — not replayed on reconnect (see WebRenderer).
  const compactionLines = {};
  function renderCompaction(d) {
    const scope = d.task_id || "main";
    let line = compactionLines[scope];
    if (d.phase === "start") {
      line = el("div", ["card", "card-sys"]);
      line.appendChild(el("span", ["sys-icon"], "⊙"));
      line.appendChild(
        el("span", ["sys-text"], "Compacting context… (" + fmtTok(d.old_tokens) + " tok)")
      );
      compactionLines[scope] = line;
      appendToTimeline(line, d.task_id);
      scrollToBottom();
      return;
    }
    // done / warning: update the pending line, or append a fresh one if the
    // start event was missed (reconnect mid-compaction).
    if (!line) {
      line = el("div", ["card", "card-sys"]);
      line.appendChild(el("span", ["sys-icon"], "⊙"));
      line.appendChild(el("span", ["sys-text"], ""));
      appendToTimeline(line, d.task_id);
    }
    const textEl = line.querySelector(".sys-text");
    if (d.phase === "done") {
      textEl.textContent =
        "Context compacted " + fmtTok(d.old_tokens) + " → " + fmtTok(d.new_tokens) + " tok";
    } else if (d.phase === "warning") {
      line.classList.add("warn");
      textEl.textContent = "Context compaction failed (" + (d.reason || "") + ") — using FIFO";
    }
    delete compactionLines[scope];
    scrollToBottom();
  }

  // Agent mail-arrival hint: a live "📨 reply arrived" system line (❓ for a
  // question). Transient — not replayed on reconnect (mirrors the compaction
  // marker: it only means something at the instant it arrives). The reply
  // itself is delivered as main's next observation; this is just the cue.
  // The web UI phrases its own English label from key/kind (the backend's
  // `text` is the CLI's Korean status line — not shown here).
  function renderAgentMail(d) {
    const line = el("div", ["card", "card-sys"]);
    line.appendChild(el("span", ["sys-icon"], d.kind === "question" ? "❓" : "📨"));
    const who = d.key ? "Agent " + d.key : "Agent";
    const label =
      d.kind === "question"
        ? who + " asked a question (awaiting reply)"
        : who + " replied";
    line.appendChild(el("span", ["sys-text"], label));
    appendToTimeline(line, d.task_id);
    scrollToBottom();
  }

  // ── Card renderers ─────────────────────────

  // Timeline-card navigation anchor. The swimlane's user marks / reply arrows
  // and the dock carry the EVENT's epoch-seconds ts; cards stamp the same
  // normalized value so click-nav resolves `[data-nav-ts="…"]` directly.
  // Mirrors TeamModel._toEpoch (numbers pass through, ISO strings parse).
  function navTs(ts) {
    if (ts == null) return null;
    if (typeof ts === "number") return ts;
    const n = Date.parse(ts);
    return isNaN(n) ? null : n / 1000;
  }
  function stampNavTs(card, ts) {
    const t = navTs(ts);
    if (t != null) card.setAttribute("data-nav-ts", String(t));
  }

  function renderUserMessage(content, ts) {
    const card = el("div", ["card", "card-user"]);
    card.appendChild(el("div", ["bubble"], escapeAndFormat(content)));
    stampCard(card, ts);
    stampNavTs(card, ts);
    $messages.appendChild(card);
    scrollToBottom();
  }

  function renderAssistantTurn(d) {
    const card = el("div", ["card", "card-assistant"]);
    if (d.thought) {
      card.appendChild(el("div", ["thought"], escapeAndFormat(d.thought)));
    }
    if (d.final !== undefined) {
      card.appendChild(el("div", ["final"], escapeAndFormat(d.final)));
      if (!d.task_id) stampNavTs(card, d.ts);
    } else if (d.action) {
      const a = el("div", ["action"]);
      a.appendChild(
        el("div", ["tool"], "⚡ " + escapeHtml(d.action.tool_name || ""))
      );
      const detail = renderActionInput(
        d.action.tool_name || "",
        d.action.tool_input || ""
      );
      a.appendChild(detail);
      card.appendChild(a);
    }
    stampCard(card, d.ts);
    appendToTimeline(card, d.task_id);
    scrollToBottom();
  }

  /** Render the action_input portion of an assistant_turn card.
   *
   * Known tool names get a custom layout (ask → numbered question list,
   * shell → ``$ <cmd>``, read_file → path + flags, edit_file →
   * path + edit count, agent → mode/task 요약). Unknown tools fall
   * back to pretty-printed JSON. Always escapes user-supplied text.
   */
  function renderActionInput(toolName, toolInputStr) {
    let parsed;
    try {
      parsed = JSON.parse(toolInputStr);
    } catch (_e) {
      // tool_input wasn't valid JSON (e.g. parser returned a string).
      // Show it verbatim so the user can still inspect what happened.
      return el("pre", ["args"], escapeHtml(toolInputStr));
    }

    if (toolName === "ask" && Array.isArray(parsed.questions)) {
      const ol = el("ol", ["action-ask"]);
      parsed.questions.forEach(function (q) {
        const li = document.createElement("li");
        li.textContent = String(q);
        ol.appendChild(li);
      });
      return ol;
    }

    if (toolName === "shell" && typeof parsed.command === "string") {
      return el(
        "pre",
        ["action-shell"],
        "$ " + escapeHtml(parsed.command)
      );
    }

    if (toolName === "read_file" && typeof parsed.path === "string") {
      const parts = [escapeHtml(parsed.path)];
      if (parsed.stat) parts.push('<span class="muted">(stat)</span>');
      if (parsed.search) {
        parts.push(
          '<span class="muted">search:</span> ' + escapeHtml(parsed.search)
        );
      }
      if (parsed.line_start) {
        parts.push(
          '<span class="muted">lines:</span> ' +
            parsed.line_start +
            "-" +
            (parsed.line_end || "?")
        );
      }
      return el("div", ["action-detail"], parts.join(" "));
    }

    if (toolName === "edit_file" && typeof parsed.path === "string") {
      // edit_file is flat-native: one op = {path, op, pos, end?, lines?} — no
      // `edits` array (that always read 0 → "(0 edits)"). Show the op + target
      // ref instead, e.g. "app.py (replace 2#KT)". Legacy/batch `edits[]` (if it
      // ever returns) still falls back to a count.
      let detail;
      if (Array.isArray(parsed.edits)) {
        const n = parsed.edits.length;
        detail = "(" + n + " edit" + (n === 1 ? "" : "s") + ")";
      } else if (parsed.op) {
        const ref = parsed.end
          ? parsed.pos + ".." + parsed.end
          : parsed.pos || "";
        detail = "(" + parsed.op + (ref ? " " + ref : "") + ")";
      } else {
        detail = "";
      }
      return el(
        "div",
        ["action-detail"],
        escapeHtml(parsed.path) +
          (detail ? ' <span class="muted">' + escapeHtml(detail) + "</span>" : "")
      );
    }

    if (toolName === "agent") {
      // 배치형 {tasks:[...]} — run fan-out 을 task 목록으로.
      if (Array.isArray(parsed.tasks)) {
        const ul = el("ul", ["action-delegate"]);
        parsed.tasks.forEach(function (t) {
          const li = document.createElement("li");
          li.textContent = String(t.task || "");
          if (t.agent || t.profile) {
            const prof = el(
              "span",
              ["muted"],
              " → " + escapeHtml(String(t.agent || t.profile))
            );
            li.appendChild(prof);
          }
          ul.appendChild(li);
        });
        return ul;
      }
      // 플랫 op — "run: <task>" / "request agt-x: <message>" 한 줄 요약.
      if (typeof parsed.mode === "string") {
        const target = parsed.key || parsed.profile || "";
        const body = parsed.task || parsed.message || "";
        return el(
          "div",
          ["action-detail"],
          escapeHtml(parsed.mode + (target ? " " + target : "")) +
            (body
              ? ' <span class="muted">' + escapeHtml(String(body)) + "</span>"
              : "")
        );
      }
    }

    if (toolName === "complete" && typeof parsed.result === "string") {
      // Should not normally hit (complete renders as ``final``) but
      // act gracefully if the model emits an explicit complete action.
      return el("div", ["final"], escapeAndFormat(parsed.result));
    }

    // Fallback: pretty JSON. Two-space indent keeps wide objects readable
    // without burning horizontal real estate.
    return el(
      "pre",
      ["args"],
      escapeHtml(JSON.stringify(parsed, null, 2))
    );
  }

  function renderObservation(d) {
    const card = el("div", ["card", "card-observation"]);
    card.classList.add(d.success ? "ok" : "fail");
    card.appendChild(
      el(
        "div",
        ["obs-head"],
        '<span class="icon">' +
          (d.success ? "✓" : "✗") +
          "</span> " +
          escapeHtml(d.tool_name || "")
      )
    );
    // An `agent` observation is a subagent's prose answer (run 결과의
    // STATUS/RESULT/[Task N]/[Duration] wrapper, 상주 회신 배달), so
    // render it through the markdown pipeline like an assistant turn. Every
    // other tool's output (read_file hashlines, shell text, write/edit diffs)
    // is monospace/structured → keep the <pre> + diff colouring.
    if ((d.tool_name || "") === "agent") {
      card.appendChild(
        el("div", ["obs-body", "obs-md"], escapeAndFormat(d.content || ""))
      );
    } else {
      card.appendChild(
        el("pre", ["obs-body"], colorizeDiffBody(escapeHtml(d.content || "")))
      );
    }
    stampCard(card, d.ts);
    appendToTimeline(card, d.task_id);
    scrollToBottom();
  }

  function renderError(d) {
    const card = el("div", ["card", "card-error"]);
    card.textContent = d.content;
    stampCard(card, d.ts);
    appendToTimeline(card, d.task_id);
    scrollToBottom();
  }

  // ── Streaming card (transient) ─────────────
  //
  // Streaming chunks belong to whoever last fired ``begin_delegate_
  // task`` on the emitting thread — main thread (no task_id) writes
  // to the global ``streamingCard`` slot; delegate worker threads
  // write to their group's per-task streaming slot. This keeps two
  // parallel workers' raw streams from colliding inside the same
  // pre element.
  function ensureStreamingCard(taskId) {
    if (taskId && taskGroups[taskId]) {
      const g = taskGroups[taskId];
      if (g.streamingCard) return;
      g.streamingCard = el("div", ["card", "card-streaming"]);
      g.streamingCard.appendChild(el("pre", ["streaming"], ""));
      g.body.appendChild(g.streamingCard);
      return;
    }
    if (streamingCard) return;
    streamingCard = el("div", ["card", "card-streaming"]);
    streamingCard.appendChild(el("pre", ["streaming"], ""));
    $messages.appendChild(streamingCard);
  }
  function updateStreamingCard(taskId) {
    if (taskId && taskGroups[taskId]) {
      const g = taskGroups[taskId];
      if (!g.streamingCard) return;
      g.streamingCard.querySelector(".streaming").textContent = g.streamingText;
      scrollToBottom();
      return;
    }
    if (!streamingCard) return;
    streamingCard.querySelector(".streaming").textContent = streamingText;
    scrollToBottom();
  }
  function clearStreamingCard(taskId) {
    if (taskId && taskGroups[taskId]) {
      const g = taskGroups[taskId];
      if (g.streamingCard) {
        g.streamingCard.remove();
        g.streamingCard = null;
        g.streamingText = "";
      }
      return;
    }
    if (streamingCard) {
      streamingCard.remove();
      streamingCard = null;
      streamingText = "";
    }
  }
  // Finalize the live streaming card in place as a *failed* emission and
  // reset the streaming slot. Unlike clearStreamingCard (which removes the
  // card for the structured assistant_turn to replace), this keeps the
  // rejected raw text visible and closes the card so the next turn's
  // stream opens a fresh one — instead of appending to the failed card.
  function finalizeStreamingAsFailed(taskId, reason, raw) {
    function mark(card) {
      card.classList.remove("card-streaming");
      card.classList.add("card-failed");
      if (reason) card.appendChild(el("div", ["fail-reason"], "⚠ " + reason));
    }
    if (taskId && taskGroups[taskId]) {
      const g = taskGroups[taskId];
      if (g.streamingCard) {
        mark(g.streamingCard);
        g.streamingCard = null;
        g.streamingText = "";
      }
      return;
    }
    if (streamingCard) {
      mark(streamingCard);
      streamingCard = null;
      streamingText = "";
    } else if (raw) {
      // Replay (event_buffer): no live stream card to close — render the
      // rejected emission as a standalone failed card.
      const card = el("div", ["card", "card-failed"]);
      card.appendChild(el("pre", ["streaming"], raw));
      if (reason) card.appendChild(el("div", ["fail-reason"], "⚠ " + reason));
      $messages.appendChild(card);
    }
  }

  // ── Input mode switching ───────────────────
  function clearConfirmButtons() {
    const btns = document.getElementById("confirm-buttons");
    if (btns) btns.remove();
  }

  // Provenance block (who/why/what) shown with a confirm or ask prompt so
  // the user can tell which delegate agent is asking and about what.
  // Returns null when there's nothing to show (e.g. main-agent prompt).
  function buildPromptMetaEl(data, includeAction) {
    if (!data) return null;
    const agent = typeof data.agent === "string" ? data.agent : "";
    const reasoning = typeof data.reasoning === "string" ? data.reasoning : "";
    const action = typeof data.action === "string" ? data.action : "";
    if (!agent && !reasoning && !(includeAction && action)) return null;
    const box = el("div", ["prompt-meta"]);
    if (agent) {
      const a = el("div", ["prompt-meta-agent"]);
      a.textContent = "↳ from " + agent;
      box.appendChild(a);
    }
    if (reasoning) {
      const r = el("div", ["prompt-meta-reasoning"]);
      r.textContent = "💭 " + reasoning.split("\n")[0];
      box.appendChild(r);
    }
    if (includeAction && action) {
      const ac = el("div", ["prompt-meta-action"]);
      ac.textContent = "⚡ " + action;
      box.appendChild(ac);
    }
    return box;
  }

  // Build the "$ <command>" HTML for a confirm dialog, wrapping each
  // dangerous (start,end) char range in a <span class="danger">. Ranges are
  // pre-computed server-side (single source of truth); we only paint them.
  // Every segment is escaped, so a command with < > & stays inert.
  function highlightDangerHtml(command, spans) {
    const cmd = String(command);
    if (!Array.isArray(spans) || !spans.length) {
      return "$ " + escapeHtml(cmd);
    }
    let out = "$ ";
    let pos = 0;
    spans.forEach(function (sp) {
      const s = sp[0];
      const e = sp[1];
      if (typeof s !== "number" || typeof e !== "number") return;
      if (s < pos || s >= e || e > cmd.length) return; // skip bad/overlapping
      out += escapeHtml(cmd.slice(pos, s));
      out += '<span class="danger">' + escapeHtml(cmd.slice(s, e)) + "</span>";
      pos = e;
    });
    out += escapeHtml(cmd.slice(pos));
    return out;
  }

  function renderConfirmButtons(options, defaultKey, data) {
    clearConfirmButtons();
    const container = el("div");
    container.id = "confirm-buttons";
    const meta = buildPromptMetaEl(data, true);
    if (meta) container.appendChild(meta);
    // The command under review, with its dangerous tokens highlighted. The
    // dialog otherwise never shows the command (it lives only in the action
    // card above), so this anchors the decision to what will actually run.
    if (data && typeof data.command === "string" && data.command) {
      const cmdEl = el(
        "pre",
        ["action-shell", "confirm-cmd"],
        highlightDangerHtml(data.command, data.danger_spans)
      );
      container.appendChild(cmdEl);
    }
    options.forEach(function (opt) {
      const btn = el("button", ["confirm-btn"]);
      if (opt.key === defaultKey) btn.classList.add("default");
      btn.textContent = opt.key + " — " + opt.label;
      btn.addEventListener("click", function () {
        submitConfirm(opt.key);
      });
      container.appendChild(btn);
    });
    $inputArea.parentNode.insertBefore(container, $inputArea);
  }

  function setInputMode(kind, data) {
    // Any mode transition ends a pending stall watch: resolved → the
    // warning is moot; a NEW confirm → the old timer must not fire into it.
    clearConfirmStall();
    currentMode = kind;
    if (kind === "confirm") {
      $modeBadge.textContent = "CONFIRM";
      $modeBadge.classList.add("visible");
      confirmDefaultKey = data.default_key;
      renderConfirmButtons(data.options || [], data.default_key, data);
      $input.placeholder = "Optional comment (empty = no comment)";
    } else {
      // chat or prompt
      // ``data.context`` is the ``ask`` tool's question block (a
      // plain-text mirror of the CLI's "Agent asks:" announcement).
      // Surfacing it next to the badge means the user doesn't have
      // to scroll the chat back to see what they're answering —
      // the question stays anchored to the input affordance until
      // they reply.
      $modeBadge.innerHTML = "";
      if (kind === "prompt") {
        const tag = document.createElement("span");
        tag.className = "mode-tag";
        tag.textContent = "ANSWERING";
        $modeBadge.appendChild(tag);
        // Who/why (delegate agent + reasoning), so an ask from a subagent
        // is attributable. No-op for a main-agent ask.
        const metaEl = buildPromptMetaEl(data, false);
        if (metaEl) $modeBadge.appendChild(metaEl);
        const ctx = data && typeof data.context === "string" ? data.context : "";
        if (ctx) {
          const ctxEl = document.createElement("span");
          ctxEl.className = "mode-context";
          ctxEl.textContent = ctx;
          $modeBadge.appendChild(ctxEl);
        }
      }
      $modeBadge.classList.toggle("visible", kind === "prompt");
      clearConfirmButtons();
    }
    // Mode just changed — recompute the send button + placeholder.
    // ``updateSendEnabled`` reads currentMode + workerBusy and
    // owns the placeholder text now, so we don't set it here.
    updateSendEnabled();
  }

  // ── POST helpers ───────────────────────────
  function postInput(body) {
    body.conn_id = myConnId; // identifies the sender (queued-message ownership)
    return fetch(
      "api/input",
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      }
    );
  }

  function submitChatOrPrompt() {
    const text = $input.value.trim();
    if (!text) return;
    const kind = currentMode === "prompt" ? "prompt" : "chat";
    postInput({ kind: kind, content: text }).then(function (res) {
      if (kind === "prompt" && res && res.status === 409) {
        // The ask was already answered (another viewer) or aborted —
        // fold the stale ANSWERING affordance.
        setInputMode("chat", null);
        setAbortVisible(false);
      }
    });
    $input.value = "";
  }

  // ── Confirm stall visibility ───────────────
  // A confirm click normally resolves in well under a second (POST →
  // worker unblocks → input_resolved). When nothing comes back — e.g.
  // the browser's 6-connections-per-origin pool is starved by SSE tabs
  // and the POST is silently queued — the button just looks broken.
  // Surface that state instead of staying silent.
  const CONFIRM_STALL_MS = 3000;
  let confirmStallTimer = null;

  function clearConfirmStall() {
    if (confirmStallTimer) {
      clearTimeout(confirmStallTimer);
      confirmStallTimer = null;
    }
    const w = document.getElementById("confirm-stall");
    if (w) w.remove();
  }

  function submitConfirm(key) {
    const comment = $input.value.trim();
    clearConfirmStall();
    confirmStallTimer = setTimeout(function () {
      const box = document.getElementById("confirm-buttons");
      if (currentMode !== "confirm" || !box) return;
      const warn = el("div", ["confirm-stall"]);
      warn.id = "confirm-stall";
      warn.textContent =
        "⚠ No response from the server yet — the connection may be " +
        "stalled (e.g. too many open tabs holding connections to this " +
        "host). The click applies as soon as it gets through; closing " +
        "unused tabs can help.";
      box.appendChild(warn);
    }, CONFIRM_STALL_MS);
    postInput({ kind: "confirm", key: key, comment: comment })
      .then(function (res) {
        if (res && res.status === 409) {
          // Already answered (another viewer / earlier click) or aborted —
          // this dialog is stale. input_resolved normally folds it; this
          // covers a client that missed that event.
          clearConfirmStall();
          setInputMode("chat", null);
          setAbortVisible(false);
        }
      })
      .catch(function () {
        /* network error — the stall warning covers the visible feedback */
      });
    $input.value = "";
  }

  // ── Input bindings ─────────────────────────
  function requestStop() {
    // Halt the in-flight chat turn at the next turn boundary. Fire and
    // forget — the worker's _on_interrupt path emits the observation and
    // flips back to worker_idle, which the SSE stream reflects.
    // Flip to "Stopping…" (disabled) immediately so the user gets
    // feedback and can't double-fire /api/stop.
    stopRequested = true;
    updateSendEnabled();
    fetch("api/stop", {
      method: "POST",
    }).catch(function () {
      /* network blip — ignore; the turn will end on its own anyway */
    });
  }

  if ($chatStop) {
    $chatStop.addEventListener("click", function () {
      if (!$chatStop.disabled) requestStop();
    });
  }

  $send.addEventListener("click", function () {
    if ($send.disabled) return;
    if (currentMode === "confirm") {
      // No textarea-only path in confirm — buttons are the contract.
      // Pressing Send falls back to the default option.
      submitConfirm(confirmDefaultKey);
    } else {
      // chat (idle → starts a run; busy → queues for injection) / prompt
      // (answers an ask). The server decides; no optimistic busy flip —
      // a queued message doesn't change worker state.
      submitChatOrPrompt();
    }
  });
  $input.addEventListener("keydown", function (e) {
    // ``e.isComposing`` / ``keyCode === 229`` guard the IME commit
    // step: when typing Korean / Japanese / Chinese, the Enter that
    // finalises the in-flight syllable arrives as a keydown with
    // ``isComposing: true``. Submitting on that Enter races the IME
    // commit — the typed-but-not-yet-committed character lands in
    // the textarea after we already sent the (incomplete) value,
    // leaving an orphan glyph + newline behind. Only treat Enter as
    // submit when no composition is active.
    if (
      e.key === "Enter" &&
      !e.shiftKey &&
      !e.isComposing &&
      e.keyCode !== 229
    ) {
      e.preventDefault();
      if ($send.disabled) return;
      if (currentMode === "confirm") {
        submitConfirm(confirmDefaultKey);
      } else {
        // chat (queues if busy) / prompt — Stop is click-only (separate btn).
        submitChatOrPrompt();
      }
    }
  });

  // ── SSE connection ─────────────────────────
  const es = new EventSource(
    "api/stream"
  );

  let sseConnected = false;
  es.onopen = function () {
    sseConnected = true;
    $status.classList.remove("down");
    $status.classList.add("up");
  };
  es.onerror = function () {
    $status.classList.remove("up");
    $status.classList.add("down");
    // EventSource does NOT retry a failed handshake (401 → readyState CLOSED).
    // A hard failure before we ever connected means the browser holds no valid
    // auth cookie — show the setup help instead of a silently dead UI.
    if (!sseConnected && es.readyState === EventSource.CLOSED) showSetupHelp();
  };

  // Release the SSE when the page is hidden — navigation, tab close, or
  // bfcache (back/forward cache). Browsers keep a bfcached page's connections
  // open, so without this the server keeps counting a viewer that has left
  // (roster grows on every revisit; idle-reap never fires). On bfcache restore
  // reload to get a fresh connection rather than a frozen, already-closed one.
  window.addEventListener("pagehide", function () {
    es.close();
  });
  window.addEventListener("pageshow", function (e) {
    if (e.persisted) {
      location.reload();
    }
  });

  /** Copy ``text`` to the clipboard. navigator.clipboard needs a secure
   * context (https / localhost) — board-proxied LAN sessions are plain
   * http, so fall back to the legacy textarea + execCommand path there. */
  function copyToClipboard(text) {
    if (navigator.clipboard && window.isSecureContext) {
      return navigator.clipboard.writeText(text);
    }
    const ta = document.createElement("textarea");
    ta.value = text;
    ta.style.position = "fixed";
    ta.style.opacity = "0";
    document.body.appendChild(ta);
    ta.select();
    try {
      document.execCommand("copy");
    } finally {
      document.body.removeChild(ta);
    }
    return Promise.resolve();
  }

  es.addEventListener("ready", function (e) {
    const d = JSON.parse(e.data);
    // NOTE: no TeamView.reset() here — reconnect replays the buffer and
    // TeamView.ingest dedups replayed events (idempotent). Clearing on every
    // reconnect used to flash "no team activity yet" mid-run.
    // ``workspace`` is the agent's working directory at session
    // creation time. Showing it in the top bar disambiguates which
    // checkout you're talking to when several LAN sessions are open
    // side-by-side. Field is omitted (rather than empty-string) when
    // unavailable so we never render a dangling " · " separator.
    // 칩 헤더 (v7.1.0): 모델 칩 = 모델명만 (provider 는 hover), 워크스
    // 페이스는 별도 칩에 꼬리(마지막 세그먼트)만 — hover 로 전체 경로.
    $info.textContent = d.model;
    $info.title = d.provider + " · " + d.model;
    if (d.workspace) {
      // ws 칩 = 클릭-복사 버튼 (별도 내부 버튼은 칩 높이보다 커서 세로
      // 클리핑됐던 실사용 피드백의 수리). 표시는 마지막 2세그먼트,
      // hover = 전체 경로, 클릭 = 복사 + ✓ 플래시.
      const ws = document.getElementById("chip-ws");
      const ic = document.getElementById("ws-copy-ic");
      const segs = d.workspace.replace(/\/+$/, "").split("/").filter(Boolean);
      const tail =
        segs.length > 2 ? "…/" + segs.slice(-2).join("/") : d.workspace;
      document.getElementById("ws-tail").textContent = tail;
      ws.title = "Copy workspace path — " + d.workspace;
      ws.hidden = false;
      ws.addEventListener("click", function () {
        copyToClipboard(d.workspace).then(function () {
          ic.textContent = "✓";
          setTimeout(function () {
            ic.textContent = "📋";
          }, 1000);
        });
      });
    }
  });

  // ── Share link (🔗) — copy a token-bearing URL others can open ──
  // The token lives in an HttpOnly cookie (JS can't read it), so fetch it from
  // the server and build the shareable URL from <base href> (which already
  // carries origin + base-path, so it is correct for direct / LAN / board /
  // caddy alike). This deliberately re-exposes the shared access token to the
  // authenticated session so the operator can hand it to a teammate.
  const $shareBtn = document.getElementById("share-btn");
  if ($shareBtn) {
    $shareBtn.addEventListener("click", async function () {
      const orig = $shareBtn.textContent;
      try {
        const r = await fetch("api/share-url");
        if (!r.ok) throw new Error("HTTP " + r.status);
        const d = await r.json();
        const base = document.querySelector("base");
        const baseHref = base ? base.href : window.location.origin + "/";
        const url = baseHref + "?token=" + encodeURIComponent(d.token);
        await copyToClipboard(url);
        $shareBtn.textContent = "✓";
        $shareBtn.title = "복사됨: " + url;
      } catch (e) {
        $shareBtn.textContent = "✗";
        $shareBtn.title = "링크 복사 실패: " + e.message;
      }
      setTimeout(function () {
        $shareBtn.textContent = orig;
      }, 1200);
    });
  }

  function fmtTok(n) {
    n = n || 0;
    return n >= 1000 ? (n / 1000).toFixed(1) + "K" : String(n);
  }


  es.addEventListener("agent_roster", function (e) {
    // 상주 에이전트 목록/상태 sticky (P4) — 대화 창 IIFE 로 중계 + Team 스윔레인.
    const d = JSON.parse(e.data);
    if (window.TeamView) TeamView.ingest("agent_roster", d);
    document.dispatchEvent(new CustomEvent("agentcli:tm-roster", { detail: d }));
    ovOnRoster(d); // 개요: 에이전트 상태 dot
  });

  es.addEventListener("agent_msg", function (e) {
    // 상주 에이전트 대화 메시지 (persistent — 재접속 replay 포함).
    const d = JSON.parse(e.data);
    if (window.TeamView) TeamView.ingest("agent_msg", d);
    document.dispatchEvent(new CustomEvent("agentcli:tm-msg", { detail: d }));
  });

  es.addEventListener("agent_cleared", function (e) {
    // 5.13: kill 시 그 에이전트 대화창 비움 (resume 재생과 대칭) — 대화
    // 창 IIFE 로 중계해 열려 있는 탭의 msgs[key] 도 지우게 한다.
    document.dispatchEvent(
      new CustomEvent("agentcli:tm-cleared", { detail: JSON.parse(e.data) }),
    );
  });

  es.addEventListener("compaction_ratio", function (e) {
    // 5.13: 다른 뷰어가 압축 슬라이더를 바꾸면 sticky 로 전파 — 슬라이더
    // IIFE 로 중계해 이 탭의 슬라이더도 동기화한다.
    document.dispatchEvent(
      new CustomEvent("agentcli:compaction", { detail: JSON.parse(e.data) }),
    );
  });

  es.addEventListener("max_agents", function (e) {
    // 5.16: 다른 뷰어가 에이전트 상한을 바꾸면 sticky 로 전파 — maxagents
    // IIFE 로 중계해 이 탭의 입력/체크박스도 동기화한다.
    document.dispatchEvent(
      new CustomEvent("agentcli:maxagents", { detail: JSON.parse(e.data) }),
    );
  });

  es.addEventListener("directives_changed", function () {
    // Someone saved DIRECTIVE.md via the Prompt Inspector → tell the inspector
    // IIFE to re-fetch the editor so concurrent editors don't show stale text.
    window.dispatchEvent(new CustomEvent("agentcli:directives-changed"));
  });

  es.addEventListener("prompt_changed", function () {
    // 시스템 프롬프트 스냅샷의 외과적 갱신 (상주 에이전트 멤버십 즉시 반영) —
    // 열린 인스펙터가 프롬프트 뷰를 재조회하게 중계.
    window.dispatchEvent(new CustomEvent("agentcli:prompt-changed"));
  });

  es.addEventListener("memory_changed", function () {
    // A `memory` op updated the ## Session Memory index → refresh the prompt
    // view (memory has no editor, so prompt-only).
    window.dispatchEvent(new CustomEvent("agentcli:memory-changed"));
  });

  es.addEventListener("token_usage", function (e) {
    // Top-bar readout: context occupancy %, this turn's in/out, and the
    // cumulative session output. Server sends raw counts; we format here.
    const d = JSON.parse(e.data);
    const parts = [];
    const inTok = d.in || 0;
    const win = d.context_window || 0;
    if (inTok && win) {
      const pct = Math.round((inTok / win) * 100);
      parts.push("ctx " + fmtTok(inTok) + "/" + fmtTok(win) + " (" + pct + "%)");
    }
    if (inTok || d.out) {
      parts.push("↑" + fmtTok(inTok) + " ↓" + fmtTok(d.out));
    }
    if (d.total_out) {
      parts.push("Σ↓" + fmtTok(d.total_out));
    }
    $tokenUsage.textContent = parts.join(" · ");
    $tokenUsage.title =
      "context " +
      fmtTok(inTok) +
      " / " +
      fmtTok(win) +
      " · turn in " +
      fmtTok(inTok) +
      " out " +
      fmtTok(d.out) +
      " · session out " +
      fmtTok(d.total_out);
    // ctx 칩 요약 (v7.1.0) — 게이지 + %, 상세는 팝오버(#token-usage)로.
    if (inTok && win) {
      const pct = Math.min(100, Math.round((inTok / win) * 100));
      const chip = document.getElementById("chip-ctx");
      document.getElementById("ctx-pct").textContent = "ctx " + pct + "%";
      document.getElementById("ctx-gauge-fill").style.width = pct + "%";
      chip.hidden = false;
      ovOnCtx(pct); // 개요: ambient ctx%
    }
  });

  es.addEventListener("user_message", function (e) {
    const d = JSON.parse(e.data);
    renderUserMessage(d.content, d.ts);
    // ``author`` (a user's nickname) puts the message on the swimlane's
    // multiplexed user lane; author-less messages (🤝 starter) are card-only.
    if (window.TeamView) TeamView.ingest("user_message", d);
    ovOnUserMsg(d); // 개요: 누적 쿼리
    qOnUserMsg(d); // 큐: 이 메시지가 큐서 주입된 것이면 ✓ 영수증
  });

  es.addEventListener("assistant_turn", function (e) {
    const d = JSON.parse(e.data);
    clearStreamingCard(d.task_id);
    renderAssistantTurn(d);
    // MAIN-timeline finals only: the swimlane draws the main→user reply
    // arrow (``answers``), the dock pins the answer text. Action turns and
    // scoped (sub-agent) finals stay out of the team buffer.
    if (!d.task_id && d.final !== undefined) {
      if (window.TeamView) TeamView.ingest("assistant_turn", d);
      updateDock(d);
      ovOnFinal(d); // 개요: 메인 응답 기록
    } else if (d.task_id && d.final !== undefined) {
      ovOnScopedFinal(d); // 개요: top-level /skill·@agent 결과 기록(모델 B)
    }
  });

  es.addEventListener("failed_turn", function (e) {
    const d = JSON.parse(e.data);
    finalizeStreamingAsFailed(d.task_id, d.reason, d.raw);
    ovOnFailed(d); // 개요: 진행 중 hero 가 "생성 중" 에 갇히지 않게 확정
  });

  es.addEventListener("observation", function (e) {
    const d = JSON.parse(e.data);
    renderObservation(d);
  });

  es.addEventListener("compaction", function (e) {
    renderCompaction(JSON.parse(e.data));
  });

  es.addEventListener("agent_mail", function (e) {
    renderAgentMail(JSON.parse(e.data));
  });

  // Application-level turn/tool errors arrive as ``turn_error`` — NOT
  // ``error`` — precisely so they don't collide with the native EventSource
  // "error" event type (which drives the connection dot via ``es.onerror``
  // above). Listening for ``error`` here would also latch the dot red on a
  // healthy stream and try to ``JSON.parse`` data-less transport errors.
  es.addEventListener("turn_error", function (e) {
    const d = JSON.parse(e.data);
    renderError(d);
  });

  // Bounded replay buffer: on reconnect to a very long session the server
  // replays only the most recent window and says how many events fell off.
  // Full history is still on disk (history.jsonl / --resume).
  es.addEventListener("transcript_truncated", function (e) {
    const d = JSON.parse(e.data);
    const line = el("div", ["card", "card-sys"]);
    line.appendChild(el("span", ["sys-icon"], "⋯"));
    line.appendChild(
      el(
        "span",
        ["sys-text"],
        d.omitted + " earlier events omitted (reconnect replay limit — full record kept in session history)"
      )
    );
    appendToTimeline(line);
    scrollToBottom();
  });

  es.addEventListener("stream_chunk", function (e) {
    const d = JSON.parse(e.data);
    if (d.task_id && taskGroups[d.task_id]) {
      taskGroups[d.task_id].streamingText += d.text;
    } else {
      streamingText += d.text;
    }
    ensureStreamingCard(d.task_id);
    updateStreamingCard(d.task_id);
    ovOnStream(d); // 개요: hero 라이브 스트리밍(메인 스코프)
  });

  es.addEventListener("stream_end", function () {
    // assistant_turn will replace the streaming card with the
    // structured version; nothing to do here.
  });

  // ── Delegate task lifecycle ────────────────
  //
  // Three event types frame each parallel-delegate worker's
  // collapsible card:
  //   scope_start   → open card (default collapsed); kind = skill | run
  //   scope_status  → update live status line (transient)
  //   scope_end     → close card with ✓/✗ + duration
  // Unified path: a skill subloop (e.g. /orchestrate) and a delegate/one-shot
  // worker now BOTH arrive as scope_start — previously skills emitted an
  // un-handled group_start and drew no card.
  es.addEventListener("scope_start", function (e) {
    const d = JSON.parse(e.data);
    if (window.TeamView) TeamView.ingest("scope_start", d);
    // NOTE: a resume-replayed scope (``d.replay``) builds its card too. It used
    // to return here — the reasoning being that the scope's turns replayed flat,
    // so the card would be an empty shell. Since v7.28.0 the resume replay is
    // ONE time-ordered stream (``replay_session``) and each turn carries the
    // scope it belongs to, so the card opens BEFORE its turns arrive and they
    // land inside it. Skipping it now would be the bug: no card at all, and a
    // swimlane bar offering navigation to something that does not exist.
    const grp = ensureTaskGroup(
      d.task_id,
      d.index || 0,
      d.agent || "",
      d.label || "",
      d.kind || "run",
      d.parent || "",
    );
    // Resident-agent request cards carry ``nav_ts`` = the originating request's
    // timestamp (server-provided, matching the swimlane user-mark/request arrow),
    // so clicking that arrow navigates to this card. Only resident-agent work
    // spans (begin_agent_work) set it; delegates/skills have no request anchor.
    if (d.nav_ts != null && grp && grp.card && !grp.card.hasAttribute("data-nav-ts"))
      stampNavTs(grp.card, d.nav_ts);
    // Register the parent link + light up the ancestors' "child running" hint.
    // AFTER ensureTaskGroup so the new card is already nested inside its parent.
    noteScopeStart(d.task_id, d.parent || "", d.agent || d.label || "scope");
    ovOnScopeStart(d); // 개요: ambient 스킬 상태
    // Nudge the Prompt Inspector (separate IIFE) to refresh its scope chips
    // if it's open, so a new sub-agent's chip appears live.
    window.dispatchEvent(new CustomEvent("agent-cli:scopes-changed"));
  });

  es.addEventListener("scope_status", function (e) {
    const d = JSON.parse(e.data);
    updateTaskStatus(d.task_id, d.status || "");
  });

  es.addEventListener("scope_end", function (e) {
    const d = JSON.parse(e.data);
    if (window.TeamView) TeamView.ingest("scope_end", d);
    noteScopeEnd(d.task_id);
    ovOnScopeEnd(d); // 개요: 스킬 종료
    // A replayed scope whose turns are not in this session's history closes
    // EMPTY (an old session recorded before turns carried their scope, or a
    // sub-agent whose turns live in its own context). Say why, rather than
    // leaving a card that looks like it lost its content.
    if (d.replay) {
      const g = taskGroups[d.task_id];
      if (g && !g.body.querySelector(".card")) {
        const note = el("div", ["card", "card-sys", "task-empty-note"]);
        note.appendChild(el("span", ["sys-icon"], "⊘"));
        note.appendChild(
          el(
            "span",
            ["sys-text"],
            "이 실행의 턴 기록이 이 세션 히스토리에 없습니다 — 서브에이전트 컨텍스트이거나 resume 이전 기록입니다.",
          ),
        );
        g.body.appendChild(note);
      }
    }
    closeTaskGroup(d.task_id, !!d.success, d.duration_s, d.error || "");
  });

  // ── Team swimlane: side-by-side with the timeline ──
  // The swimlane is a compact overview + navigator on the LEFT, the timeline the
  // detail on the RIGHT (see #content-split). Both stay visible; the ◧ Team
  // button just collapses the pane so the timeline can reclaim the width. The
  // pane + button are hidden until team activity arrives, so a plain
  // single-agent chat never sees them. Clicking a swimlane bar scrolls the
  // timeline to the matching collapsible card (shared task_id). Named function
  // (not a nested IIFE) so the markdown test harness's "first })(); = main
  // closer" extraction stays valid.
  /** Transient "no card for this bar" notice at the cursor (auto-hides). */
  let navMissEl = null;
  let navMissTimer = 0;
  function showNavMiss(x, y) {
    if (!navMissEl) {
      navMissEl = el("div", ["tv-nav-miss"]);
      document.body.appendChild(navMissEl);
    }
    navMissEl.textContent =
      "이 실행의 카드가 없습니다 — resume 이전 기록(스윔레인 막대만 복구)";
    navMissEl.style.left = Math.max(4, Math.min(x + 12, window.innerWidth - 340)) + "px";
    navMissEl.style.top = Math.max(4, y + 14) + "px";
    navMissEl.hidden = false;
    clearTimeout(navMissTimer);
    navMissTimer = setTimeout(function () {
      if (navMissEl) navMissEl.hidden = true;
    }, 2600);
  }

  // ── v8.2.0 layout inversion: team view = primary surface, timeline =
  // on-demand drawer, response dock = latest answer. ──
  const $drawer = document.getElementById("timeline-drawer");
  const $detailBtn = document.getElementById("vt-detail-toggle");
  const $overviewBtn = document.getElementById("vt-overview");
  const $flowBtn = document.getElementById("vt-flow");
  const $overview = document.getElementById("overview");
  const $teamView = document.getElementById("team-view");
  // Level control (progressive disclosure). 흐름=swimlane, 전문=timeline drawer,
  // 개요=summary view. 개요 뷰 완성(단계 2) 전까지 기본은 흐름 — 회귀 안전.
  let viewMode = "flow"; // base 뷰: overview | flow (전문 타임라인은 이 위에 뜨는 오버레이)
  let drawerOpen = false; // 전문 드로어 오버레이 상태 — base 와 독립(개요/흐름 어디 위에도)

  // 레벨 컨트롤 탭 상태 반영. 개요/흐름은 배타적 base, 전문은 그 위에 함께 뜨는
  // 오버레이 토글이라 base 와 동시에 활성일 수 있다(개요 보면서 전문 사이드 열기).
  function syncTabs() {
    if ($overviewBtn)
      $overviewBtn.setAttribute("aria-selected", viewMode === "overview" ? "true" : "false");
    if ($flowBtn) $flowBtn.setAttribute("aria-selected", viewMode === "flow" ? "true" : "false");
    if ($detailBtn) {
      $detailBtn.setAttribute("aria-selected", drawerOpen ? "true" : "false");
      $detailBtn.setAttribute("aria-pressed", drawerOpen ? "true" : "false");
    }
  }

  function setDrawer(open) {
    drawerOpen = open;
    $drawer.classList.toggle("open", open);
    $drawer.setAttribute("aria-hidden", open ? "false" : "true");
    // base(개요/흐름) 옆으로 붙는 사이드 패널 — 개요는 이 클래스로 폭을 내줘 겹침 방지.
    document.body.classList.toggle("drawer-open", open);
    syncTabs();
    // Re-pin the bottom on open: scrollTop writes while the drawer was shut
    // may have landed on stale geometry.
    if (open && autoScrollEnabled) scrollToBottom();
  }

  // base 뷰 전환(개요 ↔ 흐름). 전문 드로어는 base 위에 뜨는 독립 오버레이라 여기서
  // 닫지 않는다 — 개요를 보면서 전문 사이드를 함께 열어둘 수 있다.
  function setBaseView(mode) {
    viewMode = mode;
    if (typeof dpClose === "function") dpClose(); // Tier-2 패널은 맥락 종속 — base 전환 시 닫기
    if ($overview) $overview.hidden = mode !== "overview";
    if ($teamView) $teamView.style.display = mode === "overview" ? "none" : "";
    // 개요 모드에서는 dock(기존 최신응답 스트립)을 CSS로 숨긴다 — 개요의 응답
    // 블록 hero 가 대체하므로 중복 방지(dock 의 hidden 로직과 충돌 없이).
    document.body.classList.toggle("mode-overview", mode === "overview");
    syncTabs();
    if (mode === "overview") ovRender();
  }

  // 하위호환 단일 진입점. 'detail' = 현재 base 위에 타임라인 오버레이를 연다(더 이상
  // base 를 바꾸지 않음 → 개요에서 열어도 개요가 유지되고 전문이 옆에 뜬다).
  function setViewMode(mode) {
    if (mode === "detail") {
      setDrawer(true);
      return;
    }
    setBaseView(mode);
  }
  // 다른 IIFE(예: Export)에서 타임라인을 확실히 보이게 할 때 쓰는 훅 — base 유지, 오버레이만 연다.
  window.__showTimeline = function () {
    setDrawer(true);
  };

  // ── 개요(GLANCE) 뷰 렌더 ──────────────────────────
  // presentation-only: 기존 SSE 이벤트를 재사용. **플랫 로그 모델** — 사용자 입력과
  // "complete"(메인 final + top-level 스코프 final)를 도착 순서대로 append 만 한다.
  // 짝짓기(pairing)를 상태로 저장하지 않으므로 "대기" 정체·쿼리↔응답 오귀속이 원천적
  // 으로 없다. 블록 그룹핑(연속 user + 뒤따르는 resp)은 렌더 시점에 순서로만 도출.
  var ovEntries = []; // 플랫: {kind:'user',who,text,tm} | {kind:'resp',text,reasoning,answers,status,navTs,scopeId}
  var ovGen = null; // 현재 스트리밍 중인 resp 엔트리 참조(없으면 null)
  var ovTopScopes = {}; // depth-0 스코프 task_id → true (직접 dispatch 결과 수용용)
  var ovRoster = []; // agent_roster
  var ovSkills = {}; // 열린 skill scope: task_id → label
  var ovCtxPct = null;
  function ovCap() {
    if (ovEntries.length > 80) ovEntries = ovEntries.slice(-80);
  }
  // 스크롤 고정: 기본은 bottom 고정(스트리밍 따라감). 사용자가 위로 스크롤하면
  // 해제하고, 다시 바닥까지 내리면 재고정 — 타임라인의 _stick 과 동형 동작.
  var ovStick = true;
  if ($overview) {
    $overview.addEventListener("scroll", function () {
      ovStick =
        $overview.scrollTop + $overview.clientHeight >= $overview.scrollHeight - 24;
    });
  }

  function ovClock(ts) {
    var ms = typeof ts === "number" ? ts * 1000 : Date.parse(ts);
    if (!ms || isNaN(ms)) return "";
    var d = new Date(ms);
    return d.getHours() + ":" + String(d.getMinutes()).padStart(2, "0");
  }
  function ovAmbient() {
    var dots = ovRoster
      .map(function (a) {
        var s = a.state;
        var c = s === "busy" ? "b" : s === "waiting_ask" ? "w" : s === "dead" ? "x" : "i";
        return '<span class="ov-dot ' + c + '" title="' +
          escapeHtml((a.profile || a.name || a.key || "") + " · " + s) + '"></span>';
      })
      .join("");
    var sk = Object.keys(ovSkills);
    var skHtml = sk.length
      ? '<span class="ov-a sk">🪄 ' + escapeHtml(ovSkills[sk[sk.length - 1]]) + " 실행 중</span>"
      : "";
    var ctxHtml = ovCtxPct != null ? '<span class="ov-a">🧮 ctx ' + ovCtxPct + "%</span>" : "";
    var model = ((document.getElementById("info") || {}).textContent || "").trim();
    var mdHtml = model ? '<span class="ov-a">◈ ' + escapeHtml(model) + "</span>" : "";
    var viewers = ((document.getElementById("viewers") || {}).textContent || "").trim();
    var vwHtml = viewers ? '<span class="ov-a">' + escapeHtml(viewers) + "</span>" : "";
    return (
      '<div class="ov-ambient">' + skHtml +
      (dots ? '<span class="ov-a">' + dots + "</span>" : "") +
      ctxHtml + vwHtml + mdHtml + "</div>"
    );
  }
  function ovBlockHtml(b, isHero) {
    // 사용자 입력 = 채팅 로그 스타일 평문 줄(박스·"N건" 없음).
    var q = (b.queries || [])
      .map(function (x) {
        return '<div class="ov-q"><span class="w">👤 ' + escapeHtml(x.who) + "</span> " +
          escapeHtml(x.t) + (x.tm ? '<span class="tm">' + x.tm + "</span>" : "") + "</div>";
      })
      .join("");
    // 응답 아직 없음(트레일링 입력) = 입력 줄 + 은은한 대기 표시만.
    if (b.status === "wait") {
      return '<div class="ov-block wait">' + q + '<div class="ov-wait">⚪ 응답 대기…</div></div>';
    }
    var st =
      b.status === "gen"
        ? '<span class="ov-st gen"><span class="ov-pulse"></span> 생성 중</span>'
        : '<span class="ov-st done">✓</span>';
    // 귀속(누구의 요청에 답했나) — "응답" 라벨은 빼고 은은한 ↳ 칩만.
    var who =
      b.answers && b.answers.length
        ? '<span class="who">↳ ' + b.answers.map(escapeHtml).join(", ") + "</span>"
        : "";
    // 완료 = 앱 마크다운 렌더러(타임라인 .final 과 동일). 스트리밍 중엔 평문 + caret.
    var txt =
      b.status === "gen"
        ? escapeHtml(b.text || "") + '<span class="ov-caret"></span>'
        : escapeAndFormat(b.text || "");
    var acts =
      b.status === "done"
        ? '<div class="ov-acts"><button type="button" class="ov-act ov-copy">⧉ 복사</button>' +
          '<button type="button" class="ov-act ov-open">▤ 전체 대화</button></div>'
        : "";
    // 💭 reasoning — final 이벤트의 thought. 기본 접힘(<details>).
    var re =
      b.status === "done" && b.reasoning && b.reasoning.trim()
        ? '<details class="ov-re"><summary>💭 reasoning</summary>' +
          '<div class="ov-re-body">' + escapeAndFormat(b.reasoning) + "</div></details>"
        : "";
    // 점프 앵커: 메인 응답=data-nav-ts, 직접 dispatch 응답=data-scope-id.
    var navAttr = b.scopeId
      ? ' data-scope-id="' + escapeHtml(String(b.scopeId)) + '"'
      : b.navTs != null
        ? ' data-nav-ts="' + escapeHtml(String(b.navTs)) + '"'
        : "";
    return (
      '<div class="ov-block ' + (isHero ? "hero" : "past") + '"' + navAttr + ">" + q +
      '<div class="ov-hero"><div class="ov-he">' + who + st + "</div>" + re +
      '<div class="ov-tx">' + txt + "</div>" + acts + "</div></div>"
    );
  }
  // 플랫 로그 → 블록 도출(렌더 시점, 무상태): 연속된 user 엔트리 + 뒤따르는 resp 를
  // 한 블록으로 묶는다. 마지막에 남은 user 들(응답 아직 없음)은 wait 블록.
  function ovBuildBlocks() {
    var blocks = [];
    var q = [];
    for (var i = 0; i < ovEntries.length; i++) {
      var e = ovEntries[i];
      if (e.kind === "user") {
        q.push({ who: e.who, t: e.text, tm: e.tm });
      } else {
        blocks.push({
          queries: q,
          text: e.text,
          reasoning: e.reasoning,
          answers: e.answers,
          status: e.status,
          navTs: e.navTs,
          scopeId: e.scopeId,
        });
        q = [];
      }
    }
    if (q.length) blocks.push({ queries: q, text: "", status: "wait", answers: [] });
    return blocks;
  }
  function ovRender() {
    if (!$overview || viewMode !== "overview") return; // 활성일 때만 DOM 갱신
    var all = ovBuildBlocks().slice(-6);
    var html = ovAmbient();
    if (!all.length) {
      html += '<div class="ov-placeholder">아직 응답이 없습니다 — 아래에 메시지를 입력하세요.</div>';
    } else {
      // hero = 마지막 "응답 있는" 블록(wait 블록은 hero 아님).
      var heroIdx = -1;
      for (var k = all.length - 1; k >= 0; k--) {
        if (all[k].status !== "wait") {
          heroIdx = k;
          break;
        }
      }
      all.forEach(function (b, i) {
        html += ovBlockHtml(b, i === heroIdx);
      });
    }
    $overview.innerHTML = html;
    // 사용자가 위로 스크롤해 둔 상태(ovStick=false)면 강제로 바닥으로 끌어내리지
    // 않는다 — 읽던 위치 유지. 바닥에 붙어 있을 때만 스트리밍을 따라 자동 스크롤.
    if (ovStick) $overview.scrollTop = $overview.scrollHeight;
  }
  // 이벤트 훅 (아래 es 핸들러에서 호출) — 전부 ovEntries 에 append 만(무상태 페어링).
  function ovOnUserMsg(d) {
    // 본문에 이미 박힌 "[닉네임]: " 접두는 제거(who 라벨과 중복 방지).
    var t = (d.content || "").replace(/^\[[^\]]+\]:\s*/, "");
    ovEntries.push({ kind: "user", who: d.author || "사용자", text: t, tm: ovClock(d.ts) });
    ovCap();
    ovRender();
  }
  function ovOnStream(d) {
    if (d.task_id) return; // 메인 스코프만 hero 스트리밍
    if (!ovGen) {
      ovGen = { kind: "resp", text: "", reasoning: "", answers: [], status: "gen", navTs: null };
      ovEntries.push(ovGen);
    }
    ovGen.text += d.text || "";
    ovRender();
  }
  // navTs = d.ts: 메인 스코프 final 카드는 stampNavTs(card, d.ts) 로 같은 값을 달고
  // 있어(≈733행) 개요 블록에서 그 카드로 정확히 점프할 수 있다.
  function ovOnFinal(d) {
    if (ovGen) {
      ovGen.text = d.final;
      ovGen.reasoning = d.thought || ""; // 💭 reasoning (final 이벤트가 실어옴)
      ovGen.answers = d.answers || [];
      ovGen.status = "done";
      ovGen.navTs = d.ts;
      ovGen = null;
    } else {
      ovEntries.push({
        kind: "resp",
        text: d.final,
        reasoning: d.thought || "",
        answers: d.answers || [],
        status: "done",
        navTs: d.ts,
      });
    }
    ovCap();
    ovRender();
  }
  // 직접 실행한 top-level(depth0) /skill·@agent 의 complete 결과를 기록(모델 B).
  // 중첩(depth>0) 스코프·nav_ts 없는 메인 도구 스코프는 요약에서 제외하지 않지만,
  // 여기선 top-level 만 수용(ovTopScopes) → 요약이 과하게 시끄러워지지 않게.
  function ovOnScopedFinal(d) {
    if (!d || !d.task_id || d.final === undefined) return;
    if (!ovTopScopes[d.task_id]) return; // 중첩 스코프는 제외(전문에만)
    ovEntries.push({
      kind: "resp",
      text: d.final,
      reasoning: d.thought || "",
      answers: [],
      status: "done",
      navTs: d.ts,
      scopeId: d.task_id, // [전체 대화] 는 스코프 카드로 점프
    });
    ovCap();
    ovRender();
  }
  // 스트리밍만 하고 complete 을 못 낸 채 끝난 경우(포맷 실패·실패 턴·런 종료)의
  // gen 엔트리를 마감한다 — 그냥 두면 "생성 중" caret 정체. 본문 있으면 done, 없으면 제거.
  function ovFinalizeGen() {
    if (!ovGen) return;
    if ((ovGen.text || "").trim()) {
      ovGen.status = "done";
    } else {
      var i = ovEntries.indexOf(ovGen);
      if (i >= 0) ovEntries.splice(i, 1);
    }
    ovGen = null;
    ovRender();
  }
  function ovOnFailed(d) {
    if (d && d.task_id) return; // 메인 스코프만 (서브루프 실패는 hero 와 무관)
    ovFinalizeGen();
  }
  function ovOnRoster(d) {
    ovRoster = (d && d.roster) || [];
    ovRender();
  }
  function ovOnCtx(pct) {
    ovCtxPct = pct;
    ovRender();
  }
  function ovOnScopeStart(d) {
    if (!d || !d.task_id) return;
    if (d.depth === 0) ovTopScopes[d.task_id] = true; // top-level → complete 수용 대상
    if (d.kind === "skill") ovSkills[d.task_id] = String(d.label || "skill").replace(/^skill:/, "");
    ovRender();
  }
  function ovOnScopeEnd(d) {
    if (!d || !d.task_id) return;
    delete ovTopScopes[d.task_id];
    if (ovSkills[d.task_id]) delete ovSkills[d.task_id];
    ovRender();
  }

  /** Scroll the TIMELINE CONTAINER to a card — never scrollIntoView, which
   * also scrolls every scrollable ancestor (incl. horizontally) and shoved
   * the whole team view sideways in the mockup. The container is the one
   * surface that should move. */
  function scrollTimelineTo(card) {
    // Off auto-follow so a live scrollToBottom() can't yank us back down.
    autoScrollEnabled = false;
    const top =
      card.getBoundingClientRect().top -
      $messages.getBoundingClientRect().top +
      $messages.scrollTop -
      10;
    $messages.scrollTop = Math.max(0, top);
    card.classList.remove("tv-nav-hl");
    void card.offsetWidth; // restart the animation
    card.classList.add("tv-nav-hl");
  }

  /** Expand a scope card's collapsed ancestor chain, OUTERMOST first (jumping
   * before expanding would move the target away from the jump). */
  function expandAncestors(tid) {
    scopeAncestors(tid)
      .slice()
      .reverse()
      .forEach(function (a) {
        const g = taskGroups[a];
        if (g && g.body.hidden) g.toggle();
        else if (!g) {
          // Group entry already released (scope finished) — the card's own
          // DOM is still there, so toggle it through the element.
          const anc = $messages.querySelector(
            '.card-task-group[data-task-id="' +
              (window.CSS && CSS.escape ? CSS.escape(a) : a.replace(/"/g, '\\"')) +
              '"]',
          );
          const b = anc && anc.querySelector(":scope > .task-body");
          if (b && b.hidden) {
            b.hidden = false;
            const ch = anc.querySelector(":scope > .task-header > .task-chevron");
            if (ch) ch.textContent = "▼";
          }
        }
      });
  }

  // ── Response dock — latest MAIN answer, pinned above the input bar ──
  const $dock = document.getElementById("dock");
  let dockNavTs = null;
  function updateDock(d) {
    if (!$dock) return;
    const who = document.getElementById("d-who");
    // answers present+non-empty → attributed; [] → a 🤝 agent-report run;
    // missing → pre-attribution session (no claim either way).
    if (Array.isArray(d.answers) && d.answers.length) {
      who.textContent = "→ [" + d.answers.join(", ") + "]";
    } else if (Array.isArray(d.answers)) {
      who.textContent = "(🤝 보고 처리 — 특정 사용자 답 아님)";
    } else {
      who.textContent = "";
    }
    const t = navTs(d.ts);
    document.getElementById("d-time").textContent = t
      ? new Date(t * 1000).toLocaleTimeString()
      : "";
    const dt = document.getElementById("d-text");
    dt.textContent = d.final || "";
    dockNavTs = t != null ? String(t) : null;
    $dock.hidden = false;
    $dock.classList.remove("expanded");
    const xb = document.getElementById("d-expand");
    xb.textContent = "펼치기";
    // 펼치기 only when the text actually exceeds the 3-line clamp. Modern
    // Chromium treats line-clamp as REAL truncation (scrollHeight reports the
    // clamped height), so measure by lifting the clamp for one layout pass.
    const clamped = dt.clientHeight;
    dt.style.webkitLineClamp = "unset";
    const full = dt.scrollHeight;
    dt.style.webkitLineClamp = "";
    xb.hidden = !(full > clamped + 1);
    $dock.classList.remove("pulse");
    void $dock.offsetWidth;
    $dock.classList.add("pulse");
  }
  if ($dock) {
    document.getElementById("d-expand").addEventListener("click", function () {
      $dock.classList.toggle("expanded");
      this.textContent = $dock.classList.contains("expanded") ? "접기" : "펼치기";
    });
    // Dock body click = jump to this answer's card in the drawer (same
    // gesture as clicking the reply arrow in the swimlane).
    document.getElementById("d-text").addEventListener("click", function () {
      setViewMode("detail");
      if (!dockNavTs) return;
      const card = $messages.querySelector('[data-nav-ts="' + dockNavTs + '"]');
      if (card) scrollTimelineTo(card);
    });
  }

  // ── 선택→고정 상세 패널 (Tier 2, v8.13.0) ──────────────────────────
  // 흐름/개요 요소 클릭 → 그 요소만 집중해 우측 패널에 고정. 기존 카드/모델의
  // 이미-렌더된(escapeAndFormat) HTML 을 추출해 재사용 — presentation-only,
  // 서버·데이터·SSE 무변경. hover=툴팁(순간)·click=이 패널(고정)·버튼=Tier-3 승격.
  const $dp = document.getElementById("detail-panel");
  const $dpTag = document.getElementById("dp-tag");
  const $dpTitle = document.getElementById("dp-title");
  const $dpMeta = document.getElementById("dp-meta");
  const $dpBody = document.getElementById("dp-body");
  const $dpTimeline = document.getElementById("dp-timeline");
  const $dpInspect = document.getElementById("dp-inspect");
  const DP_TAGS = {
    user: "사용자",
    main: "응답",
    skill: "스킬",
    agent: "에이전트",
    obs: "관찰",
    error: "오류",
  };
  let dpNav = null; // Tier-3 승격 타겟 {card, taskId, label, kind, overview}

  // 카드 → {tag, label, status?, srcSel?} (export IIFE 의 classify 미러 · 메인 스코프판).
  function dpClassifyCard(card) {
    const cl = card.classList;
    if (cl.contains("card-user")) return { tag: "user", label: "사용자", srcSel: ".bubble" };
    if (cl.contains("card-assistant")) return { tag: "main", label: "응답", srcSel: ".final" };
    if (cl.contains("card-observation")) {
      const h = card.querySelector(".obs-head");
      return { tag: "obs", label: h ? h.innerText.trim() : "관찰", srcSel: ".obs-body" };
    }
    if (cl.contains("card-error")) return { tag: "error", label: "오류", srcSel: null };
    if (cl.contains("card-task-group")) {
      const t = card.querySelector(".task-title");
      const st = card.querySelector(".task-status");
      return {
        tag: card.dataset.kind === "skill" ? "skill" : "agent",
        label: t ? t.innerText.trim() : "스코프",
        status: st ? st.innerText.trim() : "",
        srcSel: null,
      };
    }
    return { tag: "main", label: "카드", srcSel: null };
  }

  function dpSetOpen(open) {
    $dp.hidden = !open;
    $dp.classList.toggle("open", open);
    $dp.setAttribute("aria-hidden", open ? "false" : "true");
    if (!open) dpNav = null;
  }
  function dpClose() {
    dpSetOpen(false);
  }

  // 공통 채움: tag/label/meta/bodyHTML + Tier-3 승격 타겟(nav).
  function dpFill(tag, label, meta, bodyHtml, nav) {
    $dpTag.textContent = DP_TAGS[tag] || tag;
    $dpTag.className = "dp-tag tag-" + tag;
    $dpTitle.textContent = label || "";
    $dpMeta.textContent = meta || "";
    $dpMeta.hidden = !meta;
    $dpBody.innerHTML = bodyHtml || '<div class="dp-empty">표시할 내용이 없습니다.</div>';
    dpNav = nav || null;
    // 🔍 는 프롬프트 스냅샷이 있는 스코프(task_id)에서만.
    $dpInspect.hidden = !(dpNav && dpNav.taskId);
    dpSetOpen(true);
    $dpBody.scrollTop = 0;
  }

  // 흐름 스윔레인 요소(카드 해석 완료) → 패널. meta = data-tip(label · 소요).
  function dpPinCard(card, meta, taskId) {
    const info = dpClassifyCard(card);
    let bodyHtml;
    if (info.tag === "skill" || info.tag === "agent") {
      // 스코프: 상태 + task-body 안 첫 .final/.obs-body(주 결과)만 초점 복제
      // (전체 중첩 트리는 [전체 타임라인] 로 승격).
      const res = card.querySelector(".task-body .final, .task-body .obs-body");
      const statusLine = info.status
        ? '<div class="dp-status">' + escapeHtml(info.status) + "</div>"
        : "";
      bodyHtml =
        statusLine +
        (res
          ? '<div class="dp-result">' + res.innerHTML + "</div>"
          : '<div class="dp-empty">아직 결과가 없습니다 (실행 중이거나 관찰 미도착).</div>');
    } else if (info.srcSel) {
      const src = card.querySelector(info.srcSel);
      bodyHtml = src ? src.innerHTML : "";
    } else {
      bodyHtml = escapeAndFormat(card.innerText || "");
    }
    dpFill(info.tag, info.label, meta, bodyHtml, {
      card: card,
      taskId: taskId || null,
      label: info.label,
      kind: card.dataset.kind || "run",
    });
  }

  // 개요 응답 블록 → 패널(이미 렌더된 hero HTML 재사용). data-nav-ts 로 타임라인
  // 카드를 해석해 두면 [▤ 전체 타임라인] 이 그 카드로 정확히 점프한다.
  // 개요 블록 → 대응하는 타임라인 카드. data-scope-id(직접 dispatch)면 스코프 task-group
  // 카드로, 아니면 data-nav-ts(메인 응답)로 해석.
  function ovResolveCard(block) {
    if (!block) return { card: null, taskId: null };
    const sid = block.getAttribute("data-scope-id");
    if (sid) {
      const sel = window.CSS && CSS.escape ? CSS.escape(sid) : sid.replace(/"/g, '\\"');
      return {
        card: $messages.querySelector('.card-task-group[data-task-id="' + sel + '"]'),
        taskId: sid,
      };
    }
    const nts = block.getAttribute("data-nav-ts");
    return {
      card: nts ? $messages.querySelector('[data-nav-ts="' + nts + '"]') : null,
      taskId: null,
    };
  }
  function dpPinOverview(block) {
    const tx = block.querySelector(".ov-tx");
    const who = block.querySelector(".ov-he .who");
    const label = who && who.textContent.trim() ? "응답 " + who.textContent.trim() : "응답";
    const r = ovResolveCard(block);
    dpFill("main", label, "", tx ? tx.innerHTML : "", { card: r.card, taskId: r.taskId, kind: "main", overview: true });
  }

  // 개요 응답 블록 액션: ⧉ 복사(본문 텍스트) · ▤ 전체 대화(타임라인으로 승격).
  // navigator.clipboard 는 secure context 전용 → LAN http 에서 미정의라 execCommand 폴백 필수.
  function ovCopyBlock(block) {
    if (!block) return;
    const tx = block.querySelector(".ov-tx");
    const text = tx ? tx.innerText : "";
    const btn = block.querySelector(".ov-copy");
    function flash() {
      if (!btn) return;
      const orig = btn.textContent;
      btn.textContent = "✓ 복사됨";
      setTimeout(function () {
        btn.textContent = orig;
      }, 1200);
    }
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(flash, function () {});
    } else {
      const ta = document.createElement("textarea");
      ta.value = text;
      ta.style.position = "fixed";
      ta.style.opacity = "0";
      document.body.appendChild(ta);
      ta.select();
      try {
        document.execCommand("copy");
        flash();
      } catch (e) {
        /* no-op */
      }
      document.body.removeChild(ta);
    }
  }
  function ovOpenTimeline(block) {
    setViewMode("detail");
    const r = ovResolveCard(block);
    if (r.card) {
      if (r.taskId && typeof expandAncestors === "function") expandAncestors(r.taskId);
      scrollTimelineTo(r.card);
    } else {
      scrollToBottom();
    }
  }

  // Tier-3 승격: [▤ 전체 타임라인] → 드로어 열고 카드로 이동(개요/카드없음은 최신 하단).
  $dpTimeline.addEventListener("click", function () {
    const nav = dpNav;
    dpClose();
    setViewMode("detail");
    if (nav && nav.card) {
      if (nav.taskId) expandAncestors(nav.taskId);
      scrollTimelineTo(nav.card);
    } else {
      scrollToBottom();
    }
  });
  // [🔍 프롬프트] → 기존 인스펙터(스코프 프롬프트+동적 컨텍스트).
  $dpInspect.addEventListener("click", function () {
    const nav = dpNav;
    if (!nav || !nav.taskId || !window.__openInspector) return;
    dpClose();
    window.__openInspector(nav.taskId, nav.label, nav.kind);
  });
  document.getElementById("dp-close").addEventListener("click", dpClose);
  document.addEventListener("keydown", function (e) {
    if (e.key === "Escape" && $dp.classList.contains("open")) dpClose();
  });
  // 개요 클릭 위임: ⧉ 복사 / ▤ 전체 대화 버튼 우선, 그 외 헤더(.ov-he) 클릭은
  // 상세 패널 고정(본문 텍스트 선택 방해 없이).
  if ($overview) {
    $overview.addEventListener("click", function (e) {
      const copyBtn = e.target.closest(".ov-copy");
      if (copyBtn) {
        ovCopyBlock(copyBtn.closest(".ov-block"));
        return;
      }
      const openBtn = e.target.closest(".ov-open");
      if (openBtn) {
        ovOpenTimeline(openBtn.closest(".ov-block"));
        return;
      }
      const he = e.target.closest(".ov-he");
      if (!he) return;
      const block = he.closest(".ov-block");
      if (block) dpPinOverview(block);
    });
  }

  function _setupTeamView() {
    const teamHost = document.getElementById("team-view");
    if (!teamHost || !window.TeamView) return;
    TeamView.mount(teamHost);
    // The team view IS the default surface now — always active, no reveal
    // gate, no resizable side pane. (The old pane width in localStorage is
    // obsolete; drop it so it can't confuse a future layout.)
    try {
      localStorage.removeItem("agentcli_team_w");
    } catch (_e) {}
    TeamView.setActive(true);

    // 기본 뷰 = 개요(GLANCE) — progressive disclosure 기본값(단계 2). 흐름/전문은
    // 레벨 컨트롤로 전환. (스윔레인·타임라인 동작은 그대로 보존.)
    setViewMode("overview");

    if ($overviewBtn)
      $overviewBtn.addEventListener("click", function () { setViewMode("overview"); });
    if ($flowBtn)
      $flowBtn.addEventListener("click", function () { setViewMode("flow"); });
    if ($detailBtn)
      $detailBtn.addEventListener("click", function () {
        setDrawer(!drawerOpen); // 전문 = base 위 오버레이 토글(개요/흐름 유지)
      });
    const tdClose = document.getElementById("td-close");
    if (tdClose)
      tdClose.addEventListener("click", function () {
        setDrawer(false); // 오버레이만 닫고 현재 base(개요/흐름) 유지
      });

    // Click a swimlane bar / arrow / user mark → open the drawer and scroll
    // the timeline to the matching card. Two anchor kinds: scope bars carry
    // data-task-id (scope cards), user marks + reply arrows carry data-nav-ts
    // (user/final cards stamped with the same event ts).
    teamHost.addEventListener("click", (e) => {
      const t = e.target;
      const tid = t && t.getAttribute && t.getAttribute("data-task-id");
      const nts = t && t.getAttribute && t.getAttribute("data-nav-ts");
      if (!tid && !nts) return;
      let card = null;
      if (tid) {
        const sel =
          window.CSS && CSS.escape ? CSS.escape(tid) : tid.replace(/"/g, '\\"');
        card = $messages.querySelector(
          '.card-task-group[data-task-id="' + sel + '"]',
        );
      } else {
        card = $messages.querySelector('[data-nav-ts="' + nts + '"]');
      }
      if (!card) {
        // ⓒ No card to jump to — say so instead of doing nothing (a session
        // recorded before the anchor existed replays bars/arrows only). A
        // silent no-op reads as a broken button.
        showNavMiss(e.clientX, e.clientY);
        return;
      }
      // v8.13.0: 바로 전문 드로어를 여는 대신 Tier-2 고정 패널에 이 요소를 집중
      // 표시(점진적 공개). 패널의 [▤ 전체 타임라인] 이 종전 드로어+스크롤 동작을 승계.
      const tip = (t.getAttribute && t.getAttribute("data-tip")) || "";
      dpPinCard(card, tip, tid || null);
    });
  }
  _setupTeamView();

  // ── Abort button visibility ────────────────
  // Shown only during ``input_required`` waits (ask answer / confirm
  // decision) — POST /api/abort releases the worker thread's blocking
  // input wait via an EOF sentinel. NOT shown during LLM streaming:
  // the abort endpoint can't cancel a streaming provider call, and
  // a button that doesn't do what it says undermines trust. True
  // streaming cancellation is a Phase D concern (provider-level).
  function setAbortVisible(visible) {
    $abort.hidden = !visible;
  }
  $abort.addEventListener("click", function () {
    fetch("api/abort", {
      method: "POST",
    });
  });

  es.addEventListener("input_required", function (e) {
    const d = JSON.parse(e.data);
    setInputMode(d.kind, d);
    // Allow aborting a stuck prompt / confirm wait. Worker side
    // surfaces this as EOFError → ``(no response)`` (ask) or
    // ``(default_key, "")`` (confirm).
    setAbortVisible(true);
  });

  es.addEventListener("input_resolved", function () {
    setInputMode("chat", null);
    setAbortVisible(false);
  });

  es.addEventListener("worker_state", function (e) {
    // Server-side flip: worker_busy fires right after popping a
    // user message, worker_idle right before the next dequeue
    // wait. Refresh / reconnect lands here too — the server
    // prepends the latest worker_state to the snapshot replay so
    // a freshly-connected client sees the correct send-button
    // state on the very first event, without having to wait for
    // the worker to actually transition.
    const d = JSON.parse(e.data);
    workerBusy = !!d.busy;
    // Any worker_state transition ends a pending stop: idle = the turn
    // we were stopping has finished; busy = a fresh turn started.
    stopRequested = false;
    updateSendEnabled();
    // Main-lane tail spinner: with the timeline in a drawer this is the
    // team surface's "main is responding" cue (agents get theirs from
    // roster state; main's truth is the worker state).
    if (window.TeamView && TeamView.setMainBusy) TeamView.setMainBusy(d.busy);
    // 런 종료(idle)인데 hero 가 아직 'gen' 이면(메인이 complete final 없이 끝남)
    // 더는 스트리밍이 없으므로 확정 — "응답 끝났는데 대기 중" 정체 방지(안전망).
    if (!d.busy) ovFinalizeGen();
  });

  // ── Identity + viewer roster ───────
  // Every connection is equal (all may send input / queue). conn_id is needed
  // only to mark "(you)" in the roster and to own queued messages.
  es.addEventListener("identity", function (e) {
    myConnId = JSON.parse(e.data).conn_id;
    // 상주 에이전트 대화 창 IIFE(별도 클로저)가 닉네임 attribution 에 쓰도록 노출.
    window.AGENTCLI_CONN_ID = myConnId;
  });

  const $viewers = document.getElementById("viewers");
  const $renameBtn = document.getElementById("rename-btn");
  es.addEventListener("viewers", function (e) {
    if (!$viewers) return;
    const d = JSON.parse(e.data);
    const labels = (d.viewers || []).map(function (v) {
      return v.id === myConnId ? v.name + " (you)" : v.name;
    });
    $viewers.textContent =
      "👁 " + d.count + (labels.length ? " · " + labels.join(", ") : "");
    $viewers.title = labels.join(", ");
    // ✎ rename: visible once we know who we are and we're in the roster.
    const me = (d.viewers || []).find(function (v) {
      return v.id === myConnId;
    });
    if (me) myNickname = me.name; // latest name for rename prefill
    if ($renameBtn) $renameBtn.hidden = !me;
    maybeNamePrompt(d.viewers || []);
  });

  // ── Nickname (input on first connect; fun default pre-filled) ───────
  // Once per page load: if a name was saved before, re-apply it silently;
  // otherwise show a bar pre-filled with the assigned fun default so the
  // user can edit/confirm (or ✕ to keep the default).
  const NICK_KEY = "agentcli_nickname";
  const $nameBar = document.getElementById("name-bar");
  const $nbInput = document.getElementById("nb-input");
  const $nbSet = document.getElementById("nb-set");
  const $nbSkip = document.getElementById("nb-skip");
  let namePrompted = false;
  let myNickname = ""; // latest roster name, for prefill on rename

  function postNickname(name) {
    fetch("api/nickname", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ conn_id: myConnId, name: name }),
    }).catch(function () {});
  }

  // Show the name-bar pre-filled with `current`, focused. Shared by the
  // first-connect prompt and the ✎ rename entry point.
  function openNameBar(current) {
    if (!$nameBar) return;
    $nbInput.value = current || "";
    $nameBar.hidden = false;
    $nbInput.focus();
    $nbInput.select();
  }

  function maybeNamePrompt(viewers) {
    if (namePrompted || !myConnId || !$nameBar) return;
    namePrompted = true;
    const saved = (localStorage.getItem(NICK_KEY) || "").trim();
    if (saved) {
      postNickname(saved); // remembered from a previous session
      return;
    }
    const me = viewers.find(function (v) {
      return v.id === myConnId;
    });
    openNameBar(me ? me.name : ""); // pre-fill the fun default
  }

  function applyNickname() {
    const name = $nbInput.value.trim();
    if (name) {
      postNickname(name);
      localStorage.setItem(NICK_KEY, name);
    }
    $nameBar.hidden = true;
  }
  if ($nbSet) $nbSet.addEventListener("click", applyNickname);
  if ($nbSkip) {
    $nbSkip.addEventListener("click", function () {
      $nameBar.hidden = true;
    });
  }
  if ($nbInput) {
    $nbInput.addEventListener("keydown", function (e) {
      if (e.key === "Enter") {
        e.preventDefault();
        applyNickname();
      }
    });
  }
  if ($renameBtn) {
    $renameBtn.addEventListener("click", function () {
      if (!myConnId) return; // identity not yet known
      openNameBar(myNickname); // prefill with current nickname
    });
  }

  // ── Pending message queue (live) + 전달 상태 ───────
  // Messages queued while the worker is busy; injected one-per-turn-boundary.
  // Each viewer can cancel their OWN still-pending items.
  //
  // 조용히 사라지는 대신 "전달됨" 을 보여준다(단계 3): 큐를 떠난 항목은
  // ⏳ → ✓ 주입됨(내 것=✓ 내 요청 반영됨) 또는 ✕ 취소됨 영수증으로 잠깐 남았다
  // 사라진다. 주입/취소 판정은 기존 이벤트로 추론(로직 불변): 큐에서 빠진 항목의
  // 텍스트가 곧 user_message 로 재방출되면 주입(그리고 개요의 다음 응답 블록
  // 요청으로 자동 승격), ~1.4s 내 매칭이 없으면 취소. 내가 ✕ 누른 건 즉시 취소.
  const $queueList = document.getElementById("queue-list");
  let qPrevById = {}; // 직전 pending: id → item
  const qLeaving = {}; // id → {item, state:'wait'|'injected'|'cancelled', _timer}
  const qCancelledByMe = {}; // id → true (내가 ✕)
  let qPending = [];

  function qScheduleRemove(id) {
    setTimeout(function () {
      delete qLeaving[id];
      delete qCancelledByMe[id];
      qRender();
    }, 1800);
  }
  function qRender() {
    if (!$queueList) return;
    const leaving = Object.keys(qLeaving);
    $queueList.innerHTML = "";
    $queueList.hidden = qPending.length + leaving.length === 0;
    qPending.forEach(function (it) {
      const row = el("div", ["queue-item"]);
      const txt = el("span", ["queue-text"]);
      txt.textContent = "⏳ [" + it.nickname + "] " + it.text;
      row.appendChild(txt);
      if (it.conn_id === myConnId) {
        const x = el("button", ["queue-cancel"]);
        x.type = "button";
        x.textContent = "✕";
        x.title = "Cancel this queued message";
        x.addEventListener("click", function () {
          qCancelledByMe[it.id] = true; // 로컬 확정 — 다음 queue 이벤트서 취소 영수증
          fetch("api/queue/cancel", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ conn_id: myConnId, id: it.id }),
          });
        });
        row.appendChild(x);
      }
      $queueList.appendChild(row);
    });
    leaving.forEach(function (id) {
      const lv = qLeaving[id];
      const cancelled = lv.state === "cancelled";
      const mine = lv.item.conn_id === myConnId;
      const row = el("div", ["queue-item", cancelled ? "q-cancelled" : "q-injected"]);
      const txt = el("span", ["queue-text"]);
      txt.textContent = (cancelled ? "✕" : "✓") + " [" + lv.item.nickname + "] " + lv.item.text;
      row.appendChild(txt);
      const st = el("span", ["queue-state"]);
      st.textContent = cancelled
        ? "취소됨"
        : mine
          ? "✓ 내 요청 반영됨"
          : "주입됨 · 다음 답변에 반영";
      row.appendChild(st);
      $queueList.appendChild(row);
    });
  }
  function qOnUserMsg(d) {
    // 큐를 떠나 대기 중인 항목이 user_message 로 재방출 → 주입 확정.
    const c = d.content || "";
    Object.keys(qLeaving).forEach(function (id) {
      const lv = qLeaving[id];
      if (
        lv.state === "wait" &&
        (c === lv.item.text || c.indexOf(lv.item.text) >= 0 || lv.item.text.indexOf(c) >= 0)
      ) {
        if (lv._timer) clearTimeout(lv._timer);
        lv.state = "injected";
        qRender();
        qScheduleRemove(id);
      }
    });
  }
  es.addEventListener("queue", function (e) {
    if (!$queueList) return;
    const pending = JSON.parse(e.data).pending || [];
    const newIds = {};
    pending.forEach(function (it) {
      newIds[it.id] = true;
    });
    // pending 에서 빠진 항목 → 영수증(즉시 취소 or 판정 대기)
    Object.keys(qPrevById).forEach(function (id) {
      if (newIds[id] || qLeaving[id]) return;
      const item = qPrevById[id];
      if (qCancelledByMe[id]) {
        qLeaving[id] = { item: item, state: "cancelled" };
        qScheduleRemove(id);
      } else {
        qLeaving[id] = { item: item, state: "wait" };
        qLeaving[id]._timer = setTimeout(function () {
          if (qLeaving[id] && qLeaving[id].state === "wait") {
            qLeaving[id].state = "cancelled";
            qRender();
            qScheduleRemove(id);
          }
        }, 1400);
      }
    });
    qPending = pending;
    qPrevById = {};
    pending.forEach(function (it) {
      qPrevById[it.id] = it;
    });
    qRender();
  });
})();

// ── Directive Editor (📝 toolbar) ─────────────────────────
// Split out of the old Prompt Inspector: this drawer ONLY edits the agent
// directives (three audience buffers). Prompt VIEWING now lives in the
// contextual Prompt Inspection drawer, opened from conversation cards.
(function () {
  "use strict";

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
  if (!$btn || !$drawer) return;

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
})();

// ── Prompt Inspection (contextual) ─────────────────────────
// Opened from a conversation agent/skill card (🔍) or the main-prompt footer
// button, SCOPED to what was clicked (no scope chips). Fetches
// /api/debug/prompt?task_id= on open; renders system + dynamic sections with
// semantic colors, group headers, per-section share bar + copy.
(function () {
  "use strict";

  const $drawer = document.getElementById("inspector");
  const $backdrop = document.getElementById("inspector-backdrop");
  const $tag = document.getElementById("insp-scope-tag");
  const $name = document.getElementById("insp-scope-name");
  const $meta = document.getElementById("insp-meta");
  const $search = document.getElementById("insp-search");
  const $sections = document.getElementById("insp-sections");
  const $collapse = document.getElementById("insp-collapse");
  const $copyall = document.getElementById("insp-copyall");
  const $mainBtn = document.getElementById("insp-main-btn");
  if (!$drawer) return;

  let activeScope = ""; // "" = main loop; a task_id = an agent/skill scope
  let lastData = null;

  function esc(s) {
    return String(s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }
  function fmtTok(n) {
    return n >= 1000 ? (n / 1000).toFixed(1) + "K" : String(n);
  }

  function copyText(text, btn) {
    const flash = function () {
      if (!btn) return;
      const old = btn.textContent;
      btn.textContent = "✓";
      setTimeout(function () { btn.textContent = old; }, 1000);
    };
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(flash).catch(function () {});
    } else {
      const ta = document.createElement("textarea");
      ta.value = text;
      document.body.appendChild(ta);
      ta.select();
      try { document.execCommand("copy"); flash(); } catch (e) {}
      document.body.removeChild(ta);
    }
  }

  function render(data) {
    lastData = data;
    if (!data || !data.ok) {
      $meta.textContent = "";
      $sections.innerHTML =
        '<div class="insp-empty">아직 LLM 호출이 없습니다 — 먼저 메시지를 보내세요.</div>';
      return;
    }
    $meta.textContent =
      "turn " + data.turn + " · " + fmtTok(data.est_tokens) + " tok · " +
      data.sections.length + " 섹션";
    const total = Math.max(1, data.est_tokens);
    let html = "";
    let lastKind = null;
    data.sections.forEach(function (s) {
      const kind = s.kind === "dynamic" ? "dynamic" : "system";
      if (kind !== lastKind) {
        html +=
          '<div class="insp-group">' +
          (kind === "dynamic" ? "Conversation · observations" : "System prompt") +
          "</div>";
        lastKind = kind;
      }
      const pct = (100 * s.est_tokens) / total;
      html +=
        '<details class="insp-sec insp-' + kind +
        '" data-name="' + esc(s.name.toLowerCase()) + '">' +
        "<summary>" +
        '<span class="insp-name">' + esc(s.name) + "</span>" +
        '<span class="insp-share"><i style="width:' +
        Math.max(3, pct).toFixed(0) + '%"></i></span>' +
        '<span class="insp-tok">' + fmtTok(s.est_tokens) + "</span>" +
        '<button class="insp-cp" type="button" title="이 섹션 복사">⧉</button>' +
        "</summary>" +
        '<pre class="insp-body">' + esc(s.text) + "</pre>" +
        "</details>";
    });
    $sections.innerHTML = html;
    $collapse.textContent = "⤢ 접기";
    applyFilter();
  }

  function applyFilter() {
    const q = $search.value.trim().toLowerCase();
    $sections.querySelectorAll(".insp-sec").forEach(function (el) {
      if (!q) {
        el.hidden = false;
        return;
      }
      const name = el.getAttribute("data-name") || "";
      const body = el.querySelector(".insp-body").textContent.toLowerCase();
      const hit = name.includes(q) || body.includes(q);
      el.hidden = !hit;
      if (hit && body.includes(q) && q.length >= 2) el.open = true;
    });
  }

  // Per-section copy — stop the click from toggling the <details>.
  $sections.addEventListener("click", function (e) {
    const cp = e.target.closest(".insp-cp");
    if (!cp) return;
    e.preventDefault();
    e.stopPropagation();
    const det = cp.closest(".insp-sec");
    const body = det && det.querySelector(".insp-body");
    if (body) copyText(body.textContent, cp);
  });

  $collapse.addEventListener("click", function () {
    const secs = $sections.querySelectorAll(".insp-sec");
    const anyOpen = Array.prototype.some.call(secs, function (d) { return d.open; });
    secs.forEach(function (d) { d.open = !anyOpen; });
    $collapse.textContent = anyOpen ? "⤢ 펼치기" : "⤢ 접기";
  });

  $copyall.addEventListener("click", function () {
    if (!lastData || !lastData.ok) return;
    const all = lastData.sections
      .map(function (s) { return "### " + s.name + "\n" + s.text; })
      .join("\n\n");
    copyText(all, $copyall);
  });

  function loadPrompt() {
    const q = activeScope ? "?task_id=" + encodeURIComponent(activeScope) : "";
    return fetch("api/debug/prompt" + q)
      .then(function (r) { return r.json(); })
      .then(render)
      .catch(function () {
        $sections.innerHTML =
          '<div class="insp-empty">프롬프트 스냅샷을 불러오지 못했습니다.</div>';
      });
  }

  function open() {
    $backdrop.hidden = false;
    requestAnimationFrame(function () {
      $backdrop.classList.add("open");
      $drawer.classList.add("open");
    });
    $drawer.setAttribute("aria-hidden", "false");
  }
  function close() {
    $backdrop.classList.remove("open");
    $drawer.classList.remove("open");
    $drawer.setAttribute("aria-hidden", "true");
    setTimeout(function () { $backdrop.hidden = true; }, 260);
  }

  // Public entry — open scoped to a conversation agent/skill (or the main loop).
  // scope: "" = main; a task_id = that card's scope. kind: "main"|"run"|"skill".
  window.__openInspector = function (scope, name, kind) {
    // Resident-agent work-span cards carry a per-invocation id `<agentId>#<n>`,
    // but the prompt snapshot is keyed by the BASE agent id (`<agentId>`). One-
    // shot delegates (`delegate-…`) and skills (`skill-…`) have no `#` suffix, so
    // stripping a trailing `#<n>` normalizes the resident case and is a no-op for
    // the rest. (Verified live: `agt-x#1` card → snapshot under `agt-x`.)
    activeScope = (scope || "").replace(/#\d+$/, "");
    const tag = kind === "skill" ? "skill" : !scope || kind === "main" ? "main" : "agent";
    $tag.textContent = tag;
    $tag.className = "insp-scope-tag tag-" + tag;
    $name.textContent = name || (tag === "main" ? "Main" : scope);
    $search.value = "";
    $sections.innerHTML = '<div class="insp-empty">불러오는 중…</div>';
    open();
    loadPrompt();
  };

  if ($mainBtn)
    $mainBtn.addEventListener("click", function () {
      window.__openInspector("", "Main", "main");
    });
  document.getElementById("insp-close").addEventListener("click", close);
  $backdrop.addEventListener("click", close);
  document.addEventListener("keydown", function (e) {
    if (e.key === "Escape" && $drawer.classList.contains("open")) close();
  });
  $search.addEventListener("input", applyFilter);

  // Live refresh of the currently-open scope (loop rebuilt the prompt / memory).
  window.addEventListener("agentcli:prompt-changed", function () {
    if ($drawer.classList.contains("open")) loadPrompt();
  });
  window.addEventListener("agentcli:memory-changed", function () {
    if ($drawer.classList.contains("open")) loadPrompt();
  });
})();

// ── Export feature (self-contained IIFE) ───────────────────────────────
//
// Decoupled from the main render loop: it reads top-level cards straight
// from #messages (classifying by card class, body from innerText), so it
// needs no hook into the card renderers. Selection happens in place via
// per-card checkboxes shown only in export mode; the bottom action bar
// exports the selected entries as a downloaded HTML file or a Jira comment.
(function () {
  "use strict";

  const $btn = document.getElementById("export-btn");
  const $bar = document.getElementById("export-bar");
  const $messages = document.getElementById("messages");
  if (!$btn || !$bar || !$messages) return;

  const $all = document.getElementById("export-all");
  const $count = document.getElementById("export-count");
  const $html = document.getElementById("export-html");
  const $jiraBtn = document.getElementById("export-jira-btn");
  const $cancel = document.getElementById("export-cancel");
  const $jiraForm = document.getElementById("export-jira-form");
  const $jiraTarget = document.getElementById("export-jira-target");
  const $jiraUrl = document.getElementById("export-jira-url");
  const $jiraDeployment = document.getElementById("export-jira-deployment");
  const $jiraUser = document.getElementById("export-jira-user");
  const $jiraSecret = document.getElementById("export-jira-secret");
  const $jiraIssue = document.getElementById("export-jira-issue");
  const $jiraSend = document.getElementById("export-jira-send");
  const $jiraHttpWarn = document.getElementById("export-jira-http-warn");
  const $msg = document.getElementById("export-msg");

  let exportMode = false;
  const selected = new Set(); // selected card elements

  // Classify a top-level card → {kind, label, mono, body?(selector)} or null
  // to skip (transient streaming / rejected raw cards).
  function classify(card) {
    const cl = card.classList;
    if (!cl || !cl.contains("card")) return null;
    if (cl.contains("card-user"))
      return { kind: "user", label: "User", mono: false, body: ".bubble" };
    if (cl.contains("card-assistant"))
      return { kind: "assistant", label: "Assistant", mono: false };
    if (cl.contains("card-observation")) {
      const head = card.querySelector(".obs-head");
      return {
        kind: "observation",
        label: head ? head.innerText.trim() : "Observation",
        mono: true,
        body: ".obs-body",
      };
    }
    if (cl.contains("card-error"))
      return { kind: "error", label: "Error", mono: true };
    if (cl.contains("card-task-group")) {
      const t = card.querySelector(".task-title");
      return {
        kind: "agent",
        label: t ? t.innerText.trim() : "agent",
        mono: false,
        body: ".task-body",
      };
    }
    return null; // card-streaming / card-failed / unknown
  }

  function topCards() {
    return Array.from($messages.children).filter(function (c) {
      return classify(c) !== null;
    });
  }

  function attachCheckbox(card) {
    if (card.querySelector(":scope > .export-check")) return;
    const cb = document.createElement("input");
    cb.type = "checkbox";
    cb.className = "export-check";
    cb.checked = selected.has(card);
    // Don't let a checkbox click bubble to card-collapse handlers.
    cb.addEventListener("click", function (e) {
      e.stopPropagation();
    });
    cb.addEventListener("change", function () {
      if (cb.checked) selected.add(card);
      else selected.delete(card);
      updateBar();
    });
    card.insertBefore(cb, card.firstChild);
  }

  function detachCheckboxes() {
    $messages.querySelectorAll(".export-check").forEach(function (c) {
      c.remove();
    });
  }

  function updateBar() {
    const cards = topCards();
    $count.textContent = selected.size + " selected";
    $all.checked = cards.length > 0 && selected.size === cards.length;
    $all.indeterminate = selected.size > 0 && selected.size < cards.length;
    const has = selected.size > 0;
    $html.disabled = !has;
    $jiraBtn.disabled = !has;
  }

  // Checkbox cards that arrive while export mode is active (e.g. a still-
  // running agent appends more turns).
  const observer = new MutationObserver(function (muts) {
    if (!exportMode) return;
    muts.forEach(function (m) {
      m.addedNodes.forEach(function (n) {
        if (n.nodeType === 1 && classify(n)) attachCheckbox(n);
      });
    });
    updateBar();
  });

  function enter() {
    exportMode = true;
    selected.clear();
    // 선택 체크박스는 #messages 카드에 붙는다 — 개요/흐름 모드에선 #messages 가
    // 닫힌 전문 드로어 안이라 보이지 않으므로 타임라인을 띄운다(없으면 no-op).
    if (window.__showTimeline) window.__showTimeline();
    document.body.classList.add("export-mode");
    $bar.hidden = false;
    hideJiraForm();
    $msg.textContent = "";
    topCards().forEach(attachCheckbox);
    observer.observe($messages, { childList: true });
    updateBar();
  }

  function exit() {
    exportMode = false;
    observer.disconnect();
    detachCheckboxes();
    selected.clear();
    document.body.classList.remove("export-mode");
    $bar.hidden = true;
  }

  function collectEntries() {
    return topCards()
      .filter(function (c) {
        return selected.has(c);
      })
      .map(function (card) {
        const c = classify(card);
        const bodyEl = c.body ? card.querySelector(c.body) : card;
        const body = (bodyEl ? bodyEl.innerText : card.innerText) || "";
        return { kind: c.kind, label: c.label, body: body.trim(), mono: c.mono };
      });
  }

  async function exportHtml() {
    $msg.textContent = "Exporting…";
    try {
      const resp = await fetch("api/export/html", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ title: document.title, entries: collectEntries() }),
      });
      if (!resp.ok) throw new Error("HTTP " + resp.status);
      const blob = await resp.blob();
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = "agent-cli-export-" + Date.now() + ".html";
      document.body.appendChild(a);
      a.click();
      a.remove();
      URL.revokeObjectURL(url);
      $msg.textContent = "";
      exit();
    } catch (e) {
      $msg.textContent = "Export failed: " + e.message;
    }
  }

  async function loadJiraTargets() {
    try {
      const r = await fetch("api/export/jira/targets");
      const d = await r.json();
      return (d && d.targets) || [];
    } catch (_e) {
      return [];
    }
  }

  // Credentials live ONLY in this browser's localStorage — never stored
  // server-side; the comment is posted as the front-end user. They are keyed by
  // base_url (the real scope of where the credentials are sent), so a typed /
  // edited URL carries its own saved login. LAST_URL remembers the URL to
  // prefill when there is no configured default (zero-config use).
  var JIRA_LAST_URL = "agentcli_jira_url";
  function credKey(url) {
    return "agentcli_jira_cred_" + (url || "").replace(/\/+$/, "");
  }
  function loadCreds(url) {
    try {
      return JSON.parse(localStorage.getItem(credKey(url)) || "{}") || {};
    } catch (_e) {
      return {};
    }
  }
  function saveCreds(url, user, secret) {
    try {
      localStorage.setItem(credKey(url), JSON.stringify({ user: user, secret: secret }));
    } catch (_e) {}
  }

  // deployment → placeholder labels for the credential fields. Cloud uses
  // email + API token; Server/DC uses username + password (or PAT).
  function applyDeploymentLabels(dep) {
    const server = dep === "server";
    $jiraUser.placeholder = server ? "username" : "email";
    $jiraSecret.placeholder = server ? "password / PAT" : "API token";
  }

  // Known config targets keyed by name → {base_url, deployment} so picking a
  // target fills the URL + toggle; the URL field is still freely editable.
  let jiraTargetsByName = {};

  // Show a plaintext-credential warning when the (user-typed) URL is http://.
  // https / config URLs are TLS-protected; empty hides it.
  function updateJiraHttpWarn() {
    if (!$jiraHttpWarn) return;
    const url = $jiraUrl.value.trim().toLowerCase();
    $jiraHttpWarn.hidden = !url.startsWith("http://");
  }

  // Reload the saved login + toggle for whatever URL is currently in the field.
  function onJiraUrlChange() {
    const c = loadCreds($jiraUrl.value.trim());
    $jiraUser.value = c.user || "";
    $jiraSecret.value = c.secret || "";
    updateJiraHttpWarn();
  }

  function onJiraTargetChange() {
    const t = jiraTargetsByName[$jiraTarget.value];
    if (t) {
      $jiraUrl.value = t.base_url || "";
      const dep = t.deployment || "cloud";
      $jiraDeployment.value = dep;
      applyDeploymentLabels(dep);
    }
    onJiraUrlChange();
  }

  async function showJiraForm() {
    const targets = await loadJiraTargets();
    jiraTargetsByName = {};
    $jiraTarget.innerHTML = "";
    targets.forEach(function (t) {
      const o = document.createElement("option");
      o.value = t.name;
      o.textContent = t.name;
      if (t.default) o.selected = true;
      $jiraTarget.appendChild(o);
      jiraTargetsByName[t.name] = t;
    });
    // Hide the selector when there are 0 or 1 instances; the URL field is the
    // entry point either way (config targets prefill it; otherwise type it).
    $jiraTarget.style.display = targets.length > 1 ? "" : "none";
    $jiraForm.hidden = false;
    $msg.textContent = "";
    if (targets.length) {
      onJiraTargetChange();
    } else {
      // Zero-config: prefill the last-used URL (if any) + its saved login.
      $jiraUrl.value = localStorage.getItem(JIRA_LAST_URL) || "";
      applyDeploymentLabels($jiraDeployment.value);
      onJiraUrlChange();
    }
    if (!$jiraUrl.value) $jiraUrl.focus();
    else if ($jiraUser.value && $jiraSecret.value) $jiraIssue.focus();
    else $jiraUser.focus();
  }

  function hideJiraForm() {
    $jiraForm.hidden = true;
  }

  async function sendJira() {
    const url = $jiraUrl.value.trim().replace(/\/+$/, "");
    if (!url) {
      $msg.textContent = "Enter your Jira base URL (e.g. https://your.atlassian.net).";
      return;
    }
    const issue = $jiraIssue.value.trim();
    if (!issue) {
      $msg.textContent = "Enter an issue key (e.g. PROJ-123).";
      return;
    }
    const user = $jiraUser.value.trim();
    const secret = $jiraSecret.value;
    if (!user || !secret) {
      $msg.textContent = "Enter your Jira account and token/password.";
      return;
    }
    $jiraSend.disabled = true;
    $msg.textContent = "Posting to Jira…";
    try {
      const r = await fetch("api/export/jira", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          target: $jiraTarget.value,
          base_url: url,
          issue_key: issue,
          deployment: $jiraDeployment.value,
          entries: collectEntries(),
          auth: { user: user, secret: secret },
        }),
      });
      const d = await r.json();
      if (!r.ok || !d.ok) throw new Error((d && d.detail) || "HTTP " + r.status);
      saveCreds(url, user, secret);
      try { localStorage.setItem(JIRA_LAST_URL, url); } catch (_e) {}
      $msg.innerHTML =
        'Posted → <a href="' +
        d.url +
        '" target="_blank" rel="noopener">' +
        issue +
        "</a>";
      setTimeout(exit, 2500);
    } catch (e) {
      $msg.textContent = "Jira failed: " + e.message;
    } finally {
      $jiraSend.disabled = false;
    }
  }

  // ── Wiring ──
  $btn.addEventListener("click", function () {
    if (exportMode) exit();
    else enter();
  });
  $cancel.addEventListener("click", exit);
  $all.addEventListener("change", function () {
    const cards = topCards();
    if ($all.checked) cards.forEach(function (c) { selected.add(c); });
    else selected.clear();
    $messages.querySelectorAll(".export-check").forEach(function (cb) {
      cb.checked = selected.has(cb.parentNode);
    });
    updateBar();
  });
  $html.addEventListener("click", exportHtml);
  $jiraBtn.addEventListener("click", function () {
    if ($jiraForm.hidden) showJiraForm();
    else hideJiraForm();
  });
  $jiraSend.addEventListener("click", sendJira);
  $jiraTarget.addEventListener("change", onJiraTargetChange);
  $jiraUrl.addEventListener("change", onJiraUrlChange);
  // Re-evaluate the plaintext warning live as the URL is typed.
  $jiraUrl.addEventListener("input", onJiraUrlChange);
  $jiraDeployment.addEventListener("change", function () {
    applyDeploymentLabels($jiraDeployment.value);
  });
  $jiraIssue.addEventListener("keydown", function (e) {
    if (e.key === "Enter") {
      e.preventDefault();
      sendJira();
    }
  });
})();

// ─── Workspace files (📁) — one drawer: download (select → zip) + upload
// (drag-drop into the drawer → uploads to the directory clicked in the tree,
// or the workspace root). Drag-OUT download isn't done (browser-restricted to
// Chromium single-files); the select + zip button is the universal path. ───
(function () {
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
  if (!$btn || !$drawer) return;

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
})();

// ── Auto-review toggle (header button → separate IIFE) ──────────────


// ── Theme picker (🎨) ───────────────────────────────────────────────
// Self-contained dropdown: the <head> inline script already applied the saved
// (or default) theme to <html data-theme>; this builds the menu, applies a
// pick, and persists it. One source of truth for the theme list + swatches.
(function () {
  var btn = document.getElementById("theme-btn");
  var menu = document.getElementById("theme-menu");
  if (!btn || !menu) return;
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
})();

// ── 상주 에이전트 대화 창 (🤝, P4) ─────────────────────────────────────────
// 상주 에이전트 roster + 대화 스트림 + 인간 개입. 데이터는 메인 SSE 가
// document CustomEvent 로 중계(agentcli:tm-roster / agentcli:tm-msg —
// 메인 SSE 가 CustomEvent 로 중계하는 브리지 패턴). 메시지는 persistent 이벤트라
// 재접속 replay 로 창 내용이 복원된다.
(function () {
  "use strict";

  const $btn = document.getElementById("agent-btn");
  const $badge = document.getElementById("tm-badge");
  const $drawer = document.getElementById("tm-drawer");
  const $backdrop = document.getElementById("tm-backdrop");
  const $roster = document.getElementById("tm-roster");
  const $conv = document.getElementById("tm-conv");
  const $input = document.getElementById("tm-input");
  const $send = document.getElementById("tm-send");
  if (!$btn || !$drawer) return;

  let roster = []; // [{key, role, state, handled, ...}]
  const msgs = Object.create(null); // key → [{direction, author, text, seq, success}]
  let selected = null;


  function esc(s) {
    const d = document.createElement("div");
    d.textContent = s == null ? "" : String(s);
    return d.innerHTML;
  }

  function renderRoster() {
    const alive = roster.filter((t) => t.state !== "dead").length;
    $badge.hidden = alive === 0;
    $badge.textContent = String(alive);
    if (!roster.length) {
      $roster.innerHTML =
        '<div class="tm-empty">No resident agents yet — spawn one and it appears here.</div>';
      return;
    }
    $roster.innerHTML = "";
    roster.forEach(function (tm) {
      const chip = document.createElement("span");
      chip.className = "tm-chip" + (tm.key === selected ? " active" : "");
      const who = [tm.profile, tm.name].filter(Boolean).join(" · ");
      chip.innerHTML =
        esc(tm.key) +
        (who ? " <b>" + esc(who) + "</b>" : "") +
        ' <span class="tm-state ' + esc(tm.state) + '">' + esc(tm.state) + "</span>";
      chip.addEventListener("click", function () {
        select(tm.key);
      });
      if (tm.state !== "dead") {
        const kill = document.createElement("button");
        kill.className = "tm-kill";
        kill.title = "Kill";
        kill.textContent = "✕";
        kill.addEventListener("click", function (ev) {
          ev.stopPropagation();
          fetch("api/agent/" + encodeURIComponent(tm.key) + "/kill", {
            method: "POST",
          });
        });
        chip.appendChild(kill);
      } else {
        // 죽은 에이전트는 이전 컨텍스트 그대로 부활 가능 (mode:"resume")
        const rev = document.createElement("button");
        rev.className = "tm-kill";
        rev.title = "Revive with previous context";
        rev.textContent = "↻";
        rev.addEventListener("click", function (ev) {
          ev.stopPropagation();
          fetch(
            "api/agent/" + encodeURIComponent(tm.key) + "/resume",
            { method: "POST" },
          );
        });
        chip.appendChild(rev);
      }
      $roster.appendChild(chip);
    });
  }

  function renderConv() {
    const list = (selected && msgs[selected]) || [];
    $conv.innerHTML = "";
    if (!selected) {
      $conv.innerHTML = '<div class="tm-empty">Select an agent.</div>';
      return;
    }
    if (!list.length) {
      $conv.innerHTML = '<div class="tm-empty">No conversation yet.</div>';
      return;
    }
    list.forEach(function (m) {
      const el = document.createElement("div");
      el.className =
        "tm-msg " + m.direction + (m.direction === "out" && !m.success ? " fail" : "");
      const who =
        m.direction === "in"
          ? m.author
          : m.direction === "question"
            ? m.author + " ❓"
            : m.author;
      el.innerHTML = '<div class="tm-author">' + esc(who) + "</div>" + esc(m.text);
      $conv.appendChild(el);
    });
    $conv.scrollTop = $conv.scrollHeight;
  }

  function select(key) {
    selected = key;
    const tm = roster.find((t) => t.key === key);
    const alive = tm && tm.state !== "dead";
    $input.disabled = !alive;
    $send.disabled = !alive;
    $input.placeholder = alive
      ? key + " — type a message… (consumed as the answer if it's awaiting one)"
      : "This agent has been terminated.";
    renderRoster();
    renderConv();
  }

  document.addEventListener("agentcli:tm-roster", function (e) {
    roster = (e.detail && e.detail.roster) || [];
    if (!selected && roster.length) {
      select(roster[0].key);
      return;
    }
    if (selected && !roster.find((t) => t.key === selected)) selected = null;
    renderRoster();
    if (selected) select(selected);
  });

  document.addEventListener("agentcli:tm-msg", function (e) {
    const m = e.detail || {};
    if (!m.key) return;
    (msgs[m.key] = msgs[m.key] || []).push(m);
    if ($drawer.classList.contains("open") && m.key === selected) renderConv();
  });

  document.addEventListener("agentcli:tm-cleared", function (e) {
    // 5.13: kill → 그 에이전트 대화창 비움. resume 시 conversation.jsonl
    // 재생(agent_msg)이 다시 채운다. 열려 있고 그 에이전트를 보고 있으면
    // 즉시 다시 그린다.
    const m = e.detail || {};
    if (!m.key) return;
    delete msgs[m.key];
    if ($drawer.classList.contains("open") && m.key === selected) renderConv();
  });

  function sendInput() {
    const text = ($input.value || "").trim();
    if (!text || !selected) return;
    fetch("api/agent/" + encodeURIComponent(selected) + "/input", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ content: text, conn_id: window.AGENTCLI_CONN_ID || null }),
    }).then(function (r) {
      if (r.ok) $input.value = "";
    });
  }
  $send.addEventListener("click", sendInput);
  $input.addEventListener("keydown", function (e) {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      sendInput();
    }
  });

  function open() {
    $backdrop.hidden = false;
    requestAnimationFrame(function () {
      $backdrop.classList.add("open");
    });
    $drawer.classList.add("open");
    $drawer.setAttribute("aria-hidden", "false");
    renderRoster();
    renderConv();
  }
  function close() {
    $drawer.classList.remove("open");
    $backdrop.classList.remove("open");
    $drawer.setAttribute("aria-hidden", "true");
    setTimeout(function () {
      $backdrop.hidden = true;
    }, 260);
  }
  $btn.addEventListener("click", function () {
    if ($drawer.classList.contains("open")) close();
    else open();
  });
  document.getElementById("tm-close").addEventListener("click", close);
  $backdrop.addEventListener("click", close);
  document.addEventListener("keydown", function (e) {
    if (e.key === "Escape" && $drawer.classList.contains("open")) close();
  });
})();

// ── 컨텍스트 압축 임계 슬라이더 (5.13) ──────────────────────────────────
// 헤더의 토큰 사용량 옆 슬라이더로 compaction 목표 비율을 세션 한정 변경.
// web·loop 이 같은 ctx 를 공유하므로 저장 즉시 다음 LLM 콜에 반영. 다른
// 뷰어는 sticky(compaction_ratio) 브로드캐스트로 동기화. 별도 IIFE — 메인
// 렌더 루프 무수정(인스펙터·테마 등과 동일 패턴).
(function () {
  "use strict";
  const $wrap = document.getElementById("compaction-wrap");
  const $range = document.getElementById("compaction-range");
  const $label = document.getElementById("compaction-label");
  if (!$wrap || !$range || !$label) return;

  const pctOf = (ratio) => Math.round(ratio * 100);
  function setLabel(pct) {
    $label.textContent = "Compact " + pct + "%";
  }
  function applyRatio(ratio) {
    const pct = pctOf(ratio);
    $range.value = pct;
    setLabel(pct);
  }

  // 초기 로드: 현재 비율 + 슬라이더 범위(min/max/step). 성공 시 노출.
  fetch("api/compaction")
    .then((r) => (r.ok ? r.json() : null))
    .then((d) => {
      if (!d) return;
      if (typeof d.min === "number") $range.min = pctOf(d.min);
      if (typeof d.max === "number") $range.max = pctOf(d.max);
      if (typeof d.step === "number") $range.step = Math.max(1, pctOf(d.step));
      if (typeof d.ratio === "number") applyRatio(d.ratio);
      $wrap.hidden = false;
    })
    .catch(() => {});

  // 드래그 중엔 라벨만 실시간, 놓을 때 저장(POST). clamp 결과를 되반영.
  $range.addEventListener("input", () => setLabel($range.value));
  $range.addEventListener("change", () => {
    const ratio = Number($range.value) / 100;
    fetch("api/compaction", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ratio: ratio }),
    })
      .then((r) => (r.ok ? r.json() : null))
      .then((d) => {
        if (d && typeof d.ratio === "number") applyRatio(d.ratio);
      })
      .catch(() => {});
  });

  // 다른 뷰어가 바꾸면 동기화.
  document.addEventListener("agentcli:compaction", (e) => {
    const d = e.detail || {};
    if (typeof d.ratio === "number") applyRatio(d.ratio);
  });
})();

// ── 에이전트 상한 제어 (5.16) ────────────────────────────────
// 헤더의 압축 슬라이더 옆에서 동시 생존 에이전트 수를 세션 한정 변경.
// 숫자 입력 + 무제한 체크박스(체크 시 입력 비활성화, value=0 전송). 레지스트리
// 가 다음 spawn/resume 게이트에서 즉시 새 값을 읽고, 다른 뷰어는
// sticky(max_agents) 로 동기화. 별도 IIFE — 메인 렌더 루프 무수정.
(function () {
  "use strict";
  const $wrap = document.getElementById("maxagents-wrap");
  const $input = document.getElementById("maxagents-input");
  const $unlim = document.getElementById("maxagents-unlimited");
  if (!$wrap || !$input || !$unlim) return;

  let lastValue = 10; // 무제한 해제 시 되돌릴 마지막 유한값

  // value=0 → 무제한(체크+입력 비활성). >0 → 유한(체크 해제+입력 활성).
  function applyValue(value) {
    if (value === 0) {
      $unlim.checked = true;
      $input.disabled = true;
    } else {
      $unlim.checked = false;
      $input.disabled = false;
      $input.value = value;
      lastValue = value;
    }
  }

  function post(value) {
    fetch("api/max-agents", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ value: value }),
    })
      .then((r) => (r.ok ? r.json() : null))
      .then((d) => {
        if (d && typeof d.value === "number") applyValue(d.value);
      })
      .catch(() => {});
  }

  // 초기 로드: 현재 값 + 최소값. 성공 시 노출.
  fetch("api/max-agents")
    .then((r) => (r.ok ? r.json() : null))
    .then((d) => {
      if (!d) return;
      if (typeof d.min === "number") $input.min = d.min;
      if (typeof d.value === "number") applyValue(d.value);
      $wrap.hidden = false;
    })
    .catch(() => {});

  // 숫자 입력 변경 → 저장 (min 미만/빈값은 min 으로 바닥).
  $input.addEventListener("change", () => {
    let v = parseInt($input.value, 10);
    const min = parseInt($input.min, 10) || 1;
    if (!Number.isFinite(v) || v < min) v = min;
    post(v);
  });
  // 무제한 토글 → 체크면 0(무제한), 해제면 마지막 유한값 복원.
  $unlim.addEventListener("change", () => {
    post($unlim.checked ? 0 : lastValue);
  });

  // 다른 뷰어가 바꾸면 동기화.
  document.addEventListener("agentcli:maxagents", (e) => {
    const d = e.detail || {};
    if (typeof d.value === "number") applyValue(d.value);
  });
})();

// ── ctx 칩 팝오버 (v7.1.0) ──────────────────────────────────────
// 저빈도 컨트롤(토큰 상세·컴팩션 슬라이더·Agents 상한)을 헤더에서 팝오버로
// 승격 — 헤더가 어떤 창 폭에서도 한 줄. 열림/닫힘은 테마 메뉴와 동형
// (클릭 토글, 바깥 클릭·Escape 닫기). 별도 IIFE — 메인 렌더 루프 무수정.
(function () {
  var chip = document.getElementById("chip-ctx");
  var pop = document.getElementById("ctx-popover");
  if (!chip || !pop) return;
  function setOpen(open) {
    pop.hidden = !open;
    chip.setAttribute("aria-expanded", String(open));
  }
  chip.addEventListener("click", function (e) {
    e.stopPropagation();
    setOpen(pop.hidden);
  });
  pop.addEventListener("click", function (e) {
    e.stopPropagation(); // 팝오버 내부 조작(슬라이더 등)이 닫힘을 유발하지 않게
  });
  document.addEventListener("click", function () {
    if (!pop.hidden) setOpen(false);
  });
  document.addEventListener("keydown", function (e) {
    if (e.key === "Escape" && !pop.hidden) setOpen(false);
  });
})();

// ── Tab-presence beacon ─────────────────────
// Every live web-UI tab holds one SSE connection out of the browser's
// 6-connections-per-origin (HTTP/1.1) pool. Under agent-board's
// board-proxy gateway ALL rooms share one origin, so the board's
// dashboard gates "open a new room" on how many tabs are already
// holding connections — it asks over a same-origin BroadcastChannel
// and each tab answers here (the v7.2.0 confirm-starvation incident).
// `path` lets the board spot "a tab for this room already exists"
// (named-window reuse → no new connection → its gate is waived).
// Operating premise (v7.7.0): rooms are ALWAYS opened through the
// board, so the board-side gate is the only admission control — the
// per-tab parking gate (v7.5/7.6) was dropped (Web Locks needs a
// secure context the plain-http LAN deployment doesn't have, and
// direct-URL entry is out of scope by policy). Direct per-port use:
// each instance is its own origin, so the channel has no other
// members and this stays inert.
(function () {
  if (typeof BroadcastChannel === "undefined") return;
  const ch = new BroadcastChannel("agentcli_tab_presence");
  ch.addEventListener("message", function (e) {
    const d = e.data || {};
    if (d.type === "ping") {
      ch.postMessage({
        type: "pong",
        nonce: d.nonce,
        path: location.pathname,
      });
    }
  });
})();
