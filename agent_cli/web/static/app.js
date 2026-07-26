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

  // ── Token from URL ─────────────────────────
  const params = new URLSearchParams(window.location.search);
  const token = params.get("token");

  if (!token) {
    document.body.innerHTML =
      '<div class="setup-message">' +
      "<h1>agent-cli web</h1>" +
      "<p>Add <code>?token=&lt;your-token&gt;</code> to the URL.</p>" +
      "<p>The token was printed to stdout when you started " +
      "<code>agent-cli web</code>.</p>" +
      "</div>";
    return;
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

  function ensureTaskGroup(taskId, index, agent, taskText, kind) {
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
    const meta = el("span", ["task-meta"]);
    header.appendChild(chevron);
    header.appendChild(title);
    header.appendChild(statusEl);
    header.appendChild(meta);

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
    $messages.appendChild(card);

    const group = {
      card: card,
      header: header,
      body: body,
      chevron: chevron,
      statusEl: statusEl,
      meta: meta,
      streamingCard: null,
      streamingText: "",
      closed: false,
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
  function renderUserMessage(content, ts) {
    const card = el("div", ["card", "card-user"]);
    card.appendChild(el("div", ["bubble"], escapeAndFormat(content)));
    stampCard(card, ts);
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
      "api/input?token=" + encodeURIComponent(token),
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
    fetch("api/stop?token=" + encodeURIComponent(token), {
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
    "api/stream?token=" + encodeURIComponent(token)
  );

  es.onopen = function () {
    $status.classList.remove("down");
    $status.classList.add("up");
  };
  es.onerror = function () {
    $status.classList.remove("up");
    $status.classList.add("down");
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

  function fmtTok(n) {
    n = n || 0;
    return n >= 1000 ? (n / 1000).toFixed(1) + "K" : String(n);
  }


  es.addEventListener("agent_roster", function (e) {
    // 상주 에이전트 목록/상태 sticky (P4) — 대화 창 IIFE 로 중계 + Team 스윔레인.
    const d = JSON.parse(e.data);
    if (window.TeamView) TeamView.ingest("agent_roster", d);
    document.dispatchEvent(new CustomEvent("agentcli:tm-roster", { detail: d }));
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
    }
  });

  es.addEventListener("user_message", function (e) {
    const d = JSON.parse(e.data);
    renderUserMessage(d.content, d.ts);
  });

  es.addEventListener("assistant_turn", function (e) {
    const d = JSON.parse(e.data);
    clearStreamingCard(d.task_id);
    renderAssistantTurn(d);
  });

  es.addEventListener("failed_turn", function (e) {
    const d = JSON.parse(e.data);
    finalizeStreamingAsFailed(d.task_id, d.reason, d.raw);
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
    // Resume replay (``replay_scopes``): the swimlane wants the bar, but the
    // timeline's collapsible card must NOT be rebuilt — the scope's inner turns
    // replay flat (ungrouped) via replay_from_history, so a re-created card
    // would be an empty shell.
    if (d.replay) return;
    ensureTaskGroup(
      d.task_id,
      d.index || 0,
      d.agent || "",
      d.label || "",
      d.kind || "run",
    );
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
    if (d.replay) return; // replayed scope: swimlane only (see scope_start)
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
  function _setupTeamView() {
    const teamHost = document.getElementById("team-view");
    const toggle = document.getElementById("view-toggle");
    const btn = document.getElementById("vt-team-toggle");
    const split = document.getElementById("content-split");
    const handle = document.getElementById("split-handle");
    if (!teamHost || !toggle || !btn || !window.TeamView) return;
    TeamView.mount(teamHost);

    // Restore a previously dragged pane width (session-persistent).
    const WKEY = "agentcli_team_w";
    const savedW = parseInt(localStorage.getItem(WKEY) || "", 10);
    if (savedW > 0) teamHost.style.flexBasis = savedW + "px";

    // aria-pressed="true" = pane shown; setActive toggles teamHost.hidden. The
    // drag handle shows/hides with the pane.
    function setCollapsed(collapsed) {
      btn.setAttribute("aria-pressed", collapsed ? "false" : "true");
      TeamView.setActive(!collapsed);
      if (handle) handle.hidden = collapsed;
    }
    btn.addEventListener("click", () =>
      setCollapsed(btn.getAttribute("aria-pressed") === "true"),
    );

    // Draggable divider: resize the swimlane pane (min 260px; keep the timeline
    // at least 360px). Pointer events → mouse + touch + pen; capture keeps the
    // drag alive when the cursor leaves the thin handle.
    if (handle && split) {
      let dragging = false;
      handle.addEventListener("pointerdown", (e) => {
        dragging = true;
        handle.setPointerCapture(e.pointerId);
        handle.classList.add("dragging");
        e.preventDefault();
      });
      handle.addEventListener("pointermove", (e) => {
        if (!dragging) return;
        const rect = split.getBoundingClientRect();
        let w = e.clientX - rect.left;
        const max = Math.max(260, rect.width - 360);
        w = Math.max(260, Math.min(max, w));
        teamHost.style.flexBasis = w + "px";
        // The host's ResizeObserver (mount) reschedules a render on width change.
      });
      function endDrag(e) {
        if (!dragging) return;
        dragging = false;
        try {
          handle.releasePointerCapture(e.pointerId);
        } catch (_) {}
        handle.classList.remove("dragging");
        localStorage.setItem(WKEY, String(parseInt(teamHost.style.flexBasis, 10) || 400));
      }
      handle.addEventListener("pointerup", endDrag);
      handle.addEventListener("pointercancel", endDrag);
    }

    let revealed = false;
    const origIngest = TeamView.ingest.bind(TeamView);
    TeamView.ingest = function (type, data) {
      origIngest(type, data);
      const isTeam =
        type === "scope_start" ||
        (type === "agent_roster" && data.roster && data.roster.length);
      if (isTeam && !revealed) {
        revealed = true;
        toggle.hidden = false;
        setCollapsed(false); // reveal the pane beside the timeline
      }
    };

    // Click a swimlane bar → scroll the timeline to its card FIRST, then flash
    // it once the scroll settles. Flashing before/during the scroll means an
    // off-screen card lights up where the user isn't looking; waiting for
    // ``scrollend`` (with a timeout fallback for no-scroll / unsupported) makes
    // the highlight land after the card is actually in view.
    teamHost.addEventListener("click", (e) => {
      const tid =
        e.target && e.target.getAttribute && e.target.getAttribute("data-task-id");
      if (!tid) return;
      const sel =
        window.CSS && CSS.escape ? CSS.escape(tid) : tid.replace(/"/g, '\\"');
      const card = $messages.querySelector(
        '.card-task-group[data-task-id="' + sel + '"]',
      );
      if (!card) return;
      // Turn OFF auto-follow so a live event's scrollToBottom() can't pull us
      // back down after the jump. Then jump INSTANTLY (not smooth): during an
      // active run the timeline is constantly appending cards + calling
      // scrollToBottom, and both cancel an in-flight smooth scroll — that was
      // the "sometimes doesn't scroll up, click 2-3 times" bug. An instant jump
      // has no animation window to interrupt, so it lands every time. ``block:
      // "start"`` puts the card's header at the top (a tall/expanded card
      // center-aligned would hide the header); ``scroll-margin-top`` adds a gap.
      autoScrollEnabled = false;
      card.scrollIntoView({ block: "start" });
      // Highlight now that the card is in view (the jump already happened).
      card.classList.remove("tv-nav-hl");
      void card.offsetWidth; // restart the animation
      card.classList.add("tv-nav-hl");
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
    fetch("api/abort?token=" + encodeURIComponent(token), {
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
    fetch("api/nickname?token=" + encodeURIComponent(token), {
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

  // ── Pending message queue (live) ───────
  // Messages queued while the worker is busy; injected one-per-turn-boundary.
  // Each viewer can cancel their OWN still-pending items.
  const $queueList = document.getElementById("queue-list");
  es.addEventListener("queue", function (e) {
    if (!$queueList) return;
    const pending = JSON.parse(e.data).pending || [];
    $queueList.innerHTML = "";
    $queueList.hidden = pending.length === 0;
    pending.forEach(function (it) {
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
          fetch("api/queue/cancel?token=" + encodeURIComponent(token), {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ conn_id: myConnId, id: it.id }),
          });
        });
        row.appendChild(x);
      }
      $queueList.appendChild(row);
    });
  });
})();

// ── Prompt Inspector ─────────────────────────
// Independent IIFE: fetches /api/debug/prompt on open, renders the system
// prompt as a token-budget bar + per-section accordions. Store-only on the
// server side, so opening the drawer is the only thing that costs a request.
(function () {
  "use strict";

  const token = new URLSearchParams(window.location.search).get("token");
  const $btn = document.getElementById("inspector-btn");
  const $drawer = document.getElementById("inspector");
  const $backdrop = document.getElementById("inspector-backdrop");
  const $meta = document.getElementById("insp-meta");
  const $scopes = document.getElementById("insp-scopes");
  const $budget = document.getElementById("insp-budget");
  const $search = document.getElementById("insp-search");
  const $sections = document.getElementById("insp-sections");
  const $dirText = document.getElementById("insp-dir-text");
  const $dirPath = document.getElementById("insp-dir-path");
  const $dirSave = document.getElementById("insp-dir-save");
  const $dirCancel = document.getElementById("insp-dir-cancel");
  const $dirStatus = document.getElementById("insp-dir-status");
  const $dirTabs = document.getElementById("insp-dir-tabs");
  const $dirBrief = document.getElementById("insp-dir-brief");
  const $dirGen = document.getElementById("insp-dir-gen");
  // 💾 save modal
  if (!$btn || !$drawer || !token) return;

  // Which system-prompt scope the drawer is showing: "" = main loop, a
  // task_id = a delegate sub-agent. Clicking a chip switches scope; the ⚡
  // button always re-opens on whatever was last selected.
  let activeScope = "";

  function qtoken() {
    return "token=" + encodeURIComponent(token);
  }

  // Distinct, stable hues per section index (works on the light theme).
  const PALETTE = [
    "#6366f1", "#0ea5e9", "#10b981", "#f59e0b", "#ef4444",
    "#8b5cf6", "#14b8a6", "#f97316", "#ec4899", "#64748b",
    "#84cc16", "#06b6d4",
  ];

  function esc(s) {
    return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  }

  function fmtTok(n) {
    return n >= 1000 ? (n / 1000).toFixed(1) + "K" : String(n);
  }

  let lastData = null;

  function render(data) {
    lastData = data;
    if (!data.ok) {
      $meta.textContent = "";
      $budget.innerHTML = "";
      $sections.innerHTML =
        '<div class="insp-empty">No LLM call yet — send a message first.</div>';
      return;
    }
    $meta.textContent =
      "turn " + data.turn + " · " + fmtTok(data.est_tokens) + " tok · " +
      (data.total_chars / 1024).toFixed(1) + " KB · " +
      data.sections.length + " sections";

    const total = Math.max(1, data.est_tokens);
    $budget.innerHTML = data.sections
      .map(function (s, i) {
        const pct = (100 * s.est_tokens) / total;
        return (
          '<span style="width:' + Math.max(0.6, pct) + "%;background:" +
          PALETTE[i % PALETTE.length] + '" title="' + esc(s.name) + " — " +
          fmtTok(s.est_tokens) + " tok (" + pct.toFixed(1) + '%)"></span>'
        );
      })
      .join("");

    $sections.innerHTML = data.sections
      .map(function (s, i) {
        const pct = ((100 * s.est_tokens) / total).toFixed(1);
        const kind = s.kind || "system";
        // Divider above the first dynamic section: the static system prompt
        // ends, the live conversation/observations begin.
        const prev = i > 0 ? data.sections[i - 1] : null;
        let divider = "";
        if (kind === "dynamic" && (!prev || (prev.kind || "system") !== "dynamic")) {
          divider =
            '<div class="insp-divider">── Dynamic context (conversation · observations) ──</div>';
        }
        return (
          divider +
          '<details class="insp-sec insp-' + kind +
          '" data-name="' + esc(s.name.toLowerCase()) + '">' +
          "<summary>" +
          '<span class="insp-dot" style="background:' +
          PALETTE[i % PALETTE.length] + '"></span>' +
          '<span class="insp-name">' + esc(s.name) + "</span>" +
          '<span class="insp-tok">' + fmtTok(s.est_tokens) + " tok</span>" +
          '<span class="insp-pct">' + pct + "%</span>" +
          "</summary>" +
          '<pre class="insp-body">' + esc(s.text) + "</pre>" +
          "</details>"
        );
      })
      .join("");
    applyFilter();
  }

  function applyFilter() {
    const q = $search.value.trim().toLowerCase();
    $drawer.querySelectorAll(".insp-sec").forEach(function (el) {
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

  // ── Scope chip row (Main + delegate sub-agents) ──
  function renderChips(scopes) {
    // Always offer Main even if it has no snapshot yet, so the user has a
    // stable home; agent chips only appear once that agent has a captured
    // prompt (the server omits scope-less agents).
    let hasMain = false;
    const chips = scopes.map(function (s) {
      if (s.id === "") hasMain = true;
      const active = s.id === activeScope ? " active" : "";
      const del = s.main
        ? ""
        : '<button class="insp-chip-del" type="button" title="Remove this agent\'s snapshot" data-del="' +
          esc(s.id) + '">✕</button>';
      return (
        '<span class="insp-chip' + active + '" data-scope="' + esc(s.id) + '">' +
        '<span class="insp-chip-label">' + esc(s.label) + "</span>" +
        (s.est_tokens
          ? '<span class="insp-chip-tok">' + fmtTok(s.est_tokens) + "</span>"
          : "") +
        del + "</span>"
      );
    });
    if (!hasMain) {
      const active = activeScope === "" ? " active" : "";
      chips.unshift(
        '<span class="insp-chip' + active + '" data-scope=""><span class="insp-chip-label">Main</span></span>'
      );
    }
    $scopes.innerHTML = chips.join("");
    // If the active scope vanished (e.g. deleted elsewhere), fall back to Main.
    if (
      activeScope !== "" &&
      !scopes.some(function (s) { return s.id === activeScope; })
    ) {
      activeScope = "";
    }
  }

  function loadScopes() {
    return fetch("api/debug/prompt/scopes?" + qtoken())
      .then(function (r) { return r.json(); })
      .then(function (d) { renderChips((d && d.scopes) || []); })
      .catch(function () { renderChips([]); });
  }

  function loadPrompt() {
    const q = activeScope
      ? "?" + qtoken() + "&task_id=" + encodeURIComponent(activeScope)
      : "?" + qtoken();
    return fetch("api/debug/prompt" + q)
      .then(function (r) { return r.json(); })
      .then(render)
      .catch(function () {
        $sections.innerHTML =
          '<div class="insp-empty">Failed to load prompt snapshot.</div>';
      });
  }

  function selectScope(id) {
    if (id === activeScope) return;
    activeScope = id;
    // Re-paint active state immediately for snappy feedback, then refetch.
    $scopes.querySelectorAll(".insp-chip").forEach(function (el) {
      el.classList.toggle("active", el.getAttribute("data-scope") === id);
    });
    loadPrompt();
  }

  function deleteScope(id) {
    fetch(
      "api/debug/prompt?" + qtoken() + "&task_id=" + encodeURIComponent(id),
      { method: "DELETE" }
    )
      .then(function () {
        if (id === activeScope) activeScope = "";
        return loadScopes();
      })
      .then(function () {
        if (id === activeScope || activeScope === "") loadPrompt();
      })
      .catch(function () {});
  }

  $scopes.addEventListener("click", function (e) {
    const del = e.target.closest(".insp-chip-del");
    if (del) {
      e.stopPropagation();
      deleteScope(del.getAttribute("data-del"));
      return;
    }
    const chip = e.target.closest(".insp-chip");
    if (chip) selectScope(chip.getAttribute("data-scope"));
  });

  // ── Directives editor — 청중 스코프 탭 (5.4.0) ──
  // 에디터 구조 = 파일 구조: 공통 / ## @main / ## @agents 세 버퍼.
  // 분해(GET scopes)·조립(POST scopes)은 서버(Python 파서 단일 출처).
  // ✨ 생성은 run 엔진 경유 초안 — 활성 탭에 미저장 반영, 검토 후 저장.
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
      b.classList.toggle("active", aud === dirAudience);
      // ● 뱃지 — 내용 있는 탭 표시 (라벨 뒤에 부착/제거)
      const base = b.textContent.replace(/ ●$/, "");
      b.textContent = (aud === dirAudience ? base : base) +
        ((aud === dirAudience ? $dirText.value : dirBuffers[aud]).trim() ? " ●" : "");
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
    return fetch("api/directives?" + qtoken())
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
    fetch("api/directives?" + qtoken(), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ scopes: dirBuffers }),
    })
      .then(function (r) {
        if (!r.ok) throw new Error(r.status);
        dirDirty = false;
        // Update-when-applied: don't optimistically refresh the prompt view —
        // it shows the CURRENTLY-applied directive and refreshes via broadcast
        // when the loop actually rebuilds (next LLM call).
        $dirStatus.textContent = "✓ Saved — applies on the next LLM call";
      })
      .catch(function () { $dirStatus.textContent = "✗ Save failed"; })
      .finally(function () { $dirSave.disabled = false; });
  }
  // Cancel: discard unsaved edits by re-loading the file's current content back
  // into the buffers. Clear dirDirty FIRST so loadDirectives (which bails while
  // dirty to protect in-progress typing) actually overwrites, then note it.
  function cancelDirectives() {
    dirDirty = false;
    loadDirectives().then(function () {
      $dirStatus.textContent = "↩ Canceled — original restored";
    });
  }
  if ($dirSave) $dirSave.addEventListener("click", saveDirectives);
  if ($dirCancel) $dirCancel.addEventListener("click", cancelDirectives);

  // ✨ 생성 — brief → 요청 시점 탭의 directive 초안 (기존 내용은 병합/개정).
  // 별도 agent-cli run 프로세스라 **탭별 동시 생성** 가능 (5.6.0): 결과는
  // 요청한 탭의 버퍼로 들어가고, 생성 중 다른 탭에서 또 ✨ 를 눌러도 된다.
  // 같은 탭의 이중 생성만 막는다 (버퍼 레이스).
  const dirGenPending = { common: false, main: false, agents: false };

  function dirGenLabel() {
    if (!$dirGen) return;
    const busy = Object.keys(dirGenPending).filter(function (k) { return dirGenPending[k]; });
    $dirGen.disabled = dirGenPending[dirAudience];
    $dirGen.textContent = busy.length ? "✨ Generate (" + busy.length + " running…)" : "✨ Generate";
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
    fetch("api/directives/generate?" + qtoken(), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        audience: aud,
        brief: brief,
        current: dirBuffers[aud],
      }),
    })
      .then(function (r) {
        if (!r.ok) return r.json().then(function (e) { throw new Error((e && e.detail) || r.status); });
        return r.json();
      })
      .then(function (d) {
        if (d && d.content) {
          dirBuffers[aud] = d.content;
          if (dirAudience === aud) $dirText.value = d.content; // 보고 있는 탭이면 즉시
          dirDirty = true; // unsaved — review, then 저장 or 취소
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
  // Directives changed on disk (a save) OR were just applied by the loop:
  // re-sync the editor and refresh the prompt view so its Directives section
  // reflects the currently-applied directive (updates when applied, not on save).
  window.addEventListener("agentcli:directives-changed", function () {
    if ($drawer.classList.contains("open")) {
      loadDirectives();
      loadPrompt();
    }
  });
  window.addEventListener("agentcli:prompt-changed", function () {
    if ($drawer.classList.contains("open")) {
      loadPrompt();
    }
  });

  window.addEventListener("agentcli:memory-changed", function () {
    // Memory index changed → refresh the prompt view only (no editor).
    if ($drawer.classList.contains("open")) loadPrompt();
  });

  function open() {
    $backdrop.hidden = false;
    requestAnimationFrame(function () {
      $backdrop.classList.add("open");
      $drawer.classList.add("open");
    });
    $drawer.setAttribute("aria-hidden", "false");
    loadScopes().then(loadPrompt);
    loadDirectives();
    loadAllAxes();
  }

  // Live chip refresh: when a delegate sub-agent spins up while the drawer is
  // open, surface its chip without forcing a reopen (the main timeline IIFE
  // dispatches this on ``delegate_task_start``).
  window.addEventListener("agent-cli:scopes-changed", function () {
    if ($drawer.classList.contains("open")) loadScopes();
  });

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
  document.getElementById("insp-close").addEventListener("click", close);
  $backdrop.addEventListener("click", close);
  document.addEventListener("keydown", function (e) {
    if (e.key === "Escape" && $drawer.classList.contains("open")) close();
  });
  $search.addEventListener("input", applyFilter);
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

  const token = new URLSearchParams(window.location.search).get("token");
  const $btn = document.getElementById("export-btn");
  const $bar = document.getElementById("export-bar");
  const $messages = document.getElementById("messages");
  if (!$btn || !$bar || !$messages || !token) return;

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

  function qtoken() {
    return "token=" + encodeURIComponent(token);
  }

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
      const resp = await fetch("api/export/html?" + qtoken(), {
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
      const r = await fetch("api/export/jira/targets?" + qtoken());
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
      const r = await fetch("api/export/jira?" + qtoken(), {
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
  const token = new URLSearchParams(window.location.search).get("token");
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
  if (!$btn || !$drawer || !token) return;

  const qt = () => "token=" + encodeURIComponent(token);
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
    const r = await fetch("api/workspace/tree?" + qt() + "&path=" + encodeURIComponent(path));
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
      const r = await fetch("api/workspace/download?" + qt(), {
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
      const r = await fetch("api/workspace/delete?" + qt(), {
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
      "api/workspace/upload?" +
      qt() +
      "&name=" +
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

  const token = new URLSearchParams(window.location.search).get("token");
  const $btn = document.getElementById("agent-btn");
  const $badge = document.getElementById("tm-badge");
  const $drawer = document.getElementById("tm-drawer");
  const $backdrop = document.getElementById("tm-backdrop");
  const $roster = document.getElementById("tm-roster");
  const $conv = document.getElementById("tm-conv");
  const $input = document.getElementById("tm-input");
  const $send = document.getElementById("tm-send");
  if (!$btn || !$drawer || !token) return;

  let roster = []; // [{key, role, state, handled, ...}]
  const msgs = Object.create(null); // key → [{direction, author, text, seq, success}]
  let selected = null;

  const qt = () => "token=" + encodeURIComponent(token);

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
          fetch("api/agent/" + encodeURIComponent(tm.key) + "/kill?" + qt(), {
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
            "api/agent/" + encodeURIComponent(tm.key) + "/resume?" + qt(),
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
    fetch("api/agent/" + encodeURIComponent(selected) + "/input?" + qt(), {
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
  const token = new URLSearchParams(window.location.search).get("token");
  const qt = () => "token=" + encodeURIComponent(token);
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
  fetch("api/compaction?" + qt())
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
    fetch("api/compaction?" + qt(), {
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
  const token = new URLSearchParams(window.location.search).get("token");
  const qt = () => "token=" + encodeURIComponent(token);
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
    fetch("api/max-agents?" + qt(), {
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
  fetch("api/max-agents?" + qt())
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
