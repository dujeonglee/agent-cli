// agent-cli web — vanilla JS client.
//
// Responsibilities:
//   1. Open SSE to /api/stream with the token-from-URL.
//   2. Render incoming events as cards in #messages.
//   3. Handle three input modes (chat / prompt / confirm) driven by
//      ``input_required`` events from the server.
//   4. POST the user's response back to /api/input.
//   5. Keep the visible chat in sync with the server-side FIFO via
//      ``prune`` events (drop the oldest N persistent cards).
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
  const $abort = document.getElementById("abort");
  const $info = document.getElementById("info");
  const $tokenUsage = document.getElementById("token-usage");
  const $status = document.getElementById("conn-status");
  const $modeBadge = document.getElementById("input-mode-badge");
  const $inputArea = document.getElementById("input-area");

  // ── State ──────────────────────────────────
  let currentMode = "chat"; // "chat" | "prompt" | "confirm"
  let confirmDefaultKey = null;
  let streamingCard = null;
  let streamingText = "";
  // ``workerBusy`` mirrors the server's ``worker_state`` event: true
  // means the chat worker is between popping a user message and
  // returning to the next ``pop_chat`` blocking call. While busy,
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
  function isStopMode() {
    return currentMode === "chat" && workerBusy;
  }

  function updateSendEnabled() {
    // chat mode: idle → "Send" (ready for next message); busy → "Stop"
    //   (interrupt the in-flight turn) rather than a dead disabled button.
    // prompt mode: always enabled (answering IS what unblocks the worker).
    // confirm mode: send button isn't the primary control —
    //   ``renderConfirmButtons`` owns the input affordance there.
    const stopMode = isStopMode();
    // chat: idle → "Send" (enabled); busy → "Stop" (enabled) until the
    // user clicks it, then "Stopping…" (disabled) until the turn ends.
    const stopping = stopMode && stopRequested;
    if (currentMode === "prompt") {
      $send.disabled = false;
    } else if (currentMode === "confirm") {
      $send.disabled = false; // Falls back to default option
    } else {
      $send.disabled = stopping; // "Stopping…" → disabled; else enabled
    }
    $send.textContent = stopMode ? (stopping ? "Stopping…" : "Stop") : "Send";
    // Red only while actionable ("Stop"); the disabled "Stopping…" uses
    // the default disabled grey.
    $send.classList.toggle("send-stop", stopMode && !stopRequested);
    $input.placeholder =
      currentMode === "prompt"
        ? "Type your answer — Enter to send"
        : currentMode === "confirm"
          ? "Optional comment (empty = no comment)"
          : stopping
            ? "Stopping… waiting for the current step to finish"
            : stopMode
              ? "Worker is processing… click Stop to interrupt"
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

  function ensureTaskGroup(taskId, index, agent, taskText) {
    if (taskGroups[taskId]) return taskGroups[taskId];

    const card = el("div", ["card", "card-task-group"]);
    card.dataset.taskId = taskId;

    const header = el("div", ["task-header"]);
    const chevron = el("span", ["task-chevron"], "▶");
    const title = el("span", ["task-title"]);
    const label = agent ? agent + ": " + taskText : taskText;
    title.textContent = "🦀 [" + (index + 1) + "] " + label;
    const statusEl = el("span", ["task-status"], "starting…");
    const meta = el("span", ["task-meta"]);
    header.appendChild(chevron);
    header.appendChild(title);
    header.appendChild(statusEl);
    header.appendChild(meta);

    const body = el("div", ["task-body"]);
    body.hidden = true; // default collapsed

    header.addEventListener("click", function () {
      const wasCollapsed = body.hidden;
      body.hidden = !body.hidden;
      chevron.textContent = body.hidden ? "▶" : "▼";
      if (wasCollapsed) {
        // After expand, scroll the header back into the top of the
        // viewport so the long body that just appeared doesn't
        // push the header off-screen — otherwise users lose their
        // anchor and scroll feels stuck.
        header.scrollIntoView({ behavior: "smooth", block: "start" });
      }
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

  // ── Card renderers ─────────────────────────
  function renderUserMessage(content) {
    const card = el("div", ["card", "card-user"]);
    card.appendChild(el("div", ["bubble"], escapeAndFormat(content)));
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
    appendToTimeline(card, d.task_id);
    scrollToBottom();
  }

  /** Render the action_input portion of an assistant_turn card.
   *
   * Known tool names get a custom layout (ask → numbered question list,
   * shell → ``$ <cmd>``, read_file → path + flags, edit_file →
   * path + edit count, delegate → task list). Unknown tools fall
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
      const editCount = Array.isArray(parsed.edits) ? parsed.edits.length : 0;
      return el(
        "div",
        ["action-detail"],
        escapeHtml(parsed.path) +
          ' <span class="muted">(' +
          editCount +
          " edit" +
          (editCount === 1 ? "" : "s") +
          ")</span>"
      );
    }

    if (toolName === "delegate" && Array.isArray(parsed.tasks)) {
      const ul = el("ul", ["action-delegate"]);
      parsed.tasks.forEach(function (t) {
        const li = document.createElement("li");
        li.textContent = String(t.task || "");
        if (t.agent) {
          const agent = el(
            "span",
            ["muted"],
            " → " + escapeHtml(String(t.agent))
          );
          li.appendChild(agent);
        }
        ul.appendChild(li);
      });
      return ul;
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
    card.appendChild(
      el("pre", ["obs-body"], colorizeDiffBody(escapeHtml(d.content || "")))
    );
    appendToTimeline(card, d.task_id);
    scrollToBottom();
  }

  function renderError(d) {
    const card = el("div", ["card", "card-error"]);
    card.textContent = d.content;
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

  // ── FIFO prune ─────────────────────────────
  function pruneOldest(drop) {
    // Drop the N oldest persistent cards. Streaming / transient cards
    // (card-streaming) are excluded — they re-form anyway. Matches
    // the server's persistent_count semantics so the visible chat
    // mirrors ContextManager's live cache.
    const selector =
      ".card-user, .card-assistant, .card-observation, .card-error";
    let removed = 0;
    while (removed < drop) {
      const first = $messages.querySelector(selector);
      if (!first) break;
      first.remove();
      removed++;
    }
  }

  // ── Takeover ───────────────────────────────
  function showTakeoverNotice() {
    const banner = el("div", ["banner-takeover"]);
    banner.textContent =
      "Another client took over this session. Refresh after closing the other tab to reconnect.";
    document.body.appendChild(banner);
    $input.disabled = true;
    $send.disabled = true;
    $status.classList.remove("up");
    $status.classList.add("down");
  }

  // ── Input mode switching ───────────────────
  function clearConfirmButtons() {
    const btns = document.getElementById("confirm-buttons");
    if (btns) btns.remove();
  }

  function renderConfirmButtons(options, defaultKey) {
    clearConfirmButtons();
    const container = el("div");
    container.id = "confirm-buttons";
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
    currentMode = kind;
    if (kind === "confirm") {
      $modeBadge.textContent = "CONFIRM";
      $modeBadge.classList.add("visible");
      confirmDefaultKey = data.default_key;
      renderConfirmButtons(data.options || [], data.default_key);
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
    return fetch(
      "/api/input?token=" + encodeURIComponent(token),
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
    postInput({ kind: kind, content: text });
    $input.value = "";
  }

  function submitConfirm(key) {
    const comment = $input.value.trim();
    postInput({ kind: "confirm", key: key, comment: comment });
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
    fetch("/api/stop?token=" + encodeURIComponent(token), {
      method: "POST",
    }).catch(function () {
      /* network blip — ignore; the turn will end on its own anyway */
    });
  }

  $send.addEventListener("click", function () {
    if ($send.disabled) return;
    if (isStopMode()) {
      requestStop();
      return;
    }
    if (currentMode === "confirm") {
      // No textarea-only path in confirm — buttons are the contract.
      // Pressing Send falls back to the default option.
      submitConfirm(confirmDefaultKey);
    } else if (currentMode === "prompt") {
      // Answering an ``ask`` — server flips to worker_busy on its
      // own when the answer arrives. No optimistic flip here.
      submitChatOrPrompt();
    } else {
      // Chat send: optimistically flip to busy so the button
      // disables instantly. The server's ``worker_busy`` event
      // will arrive a moment later and confirm the state (and any
      // future refresh will see the latest one via snapshot).
      submitChatOrPrompt();
      workerBusy = true;
      updateSendEnabled();
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
      // Mirror the click handler — refuse to submit when send is
      // gated. Without this Enter would still queue a chat message
      // into a busy worker even though the button looks disabled,
      // defeating the whole point of the gating.
      if ($send.disabled) return;
      // In stop mode the button says "Stop"; Enter must NOT trigger it
      // (and must not queue a message). Stop is click-only by design.
      if (isStopMode()) return;
      if (currentMode === "confirm") {
        submitConfirm(confirmDefaultKey);
      } else if (currentMode === "prompt") {
        submitChatOrPrompt();
      } else {
        submitChatOrPrompt();
        workerBusy = true;
        updateSendEnabled();
      }
    }
  });

  // ── SSE connection ─────────────────────────
  const es = new EventSource(
    "/api/stream?token=" + encodeURIComponent(token)
  );

  es.onopen = function () {
    $status.classList.remove("down");
    $status.classList.add("up");
  };
  es.onerror = function () {
    $status.classList.remove("up");
    $status.classList.add("down");
  };

  es.addEventListener("ready", function (e) {
    const d = JSON.parse(e.data);
    // ``workspace`` is the agent's working directory at session
    // creation time. Showing it in the top bar disambiguates which
    // checkout you're talking to when several LAN sessions are open
    // side-by-side. Field is omitted (rather than empty-string) when
    // unavailable so we never render a dangling " · " separator.
    let label = d.provider + " · " + d.model;
    if (d.workspace) {
      label += " · " + d.workspace;
    }
    $info.textContent = label;
    // Full path in the tooltip — the header truncates with ellipsis
    // for long paths but the user can still hover to see the whole.
    $info.title = d.workspace || "";
  });

  function fmtTok(n) {
    n = n || 0;
    return n >= 1000 ? (n / 1000).toFixed(1) + "K" : String(n);
  }

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
  });

  es.addEventListener("user_message", function (e) {
    const d = JSON.parse(e.data);
    renderUserMessage(d.content);
  });

  es.addEventListener("assistant_turn", function (e) {
    const d = JSON.parse(e.data);
    clearStreamingCard(d.task_id);
    renderAssistantTurn(d);
  });

  es.addEventListener("observation", function (e) {
    const d = JSON.parse(e.data);
    renderObservation(d);
  });

  es.addEventListener("error", function (e) {
    const d = JSON.parse(e.data);
    renderError(d);
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
  //   delegate_task_start  → open card (default collapsed)
  //   delegate_task_status → update live status line (transient)
  //   delegate_task_end    → close card with ✓/✗ + duration
  es.addEventListener("delegate_task_start", function (e) {
    const d = JSON.parse(e.data);
    ensureTaskGroup(d.task_id, d.index, d.agent || "", d.task_text || "");
  });

  es.addEventListener("delegate_task_status", function (e) {
    const d = JSON.parse(e.data);
    updateTaskStatus(d.task_id, d.status || "");
  });

  es.addEventListener("delegate_task_end", function (e) {
    const d = JSON.parse(e.data);
    closeTaskGroup(d.task_id, !!d.success, d.duration_s, d.error || "");
  });

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
    fetch("/api/abort?token=" + encodeURIComponent(token), {
      method: "POST",
    });
  });

  es.addEventListener("prune", function (e) {
    const d = JSON.parse(e.data);
    pruneOldest(d.drop);
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
    // user message, worker_idle right before the next pop_chat
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

  es.addEventListener("takeover", function () {
    showTakeoverNotice();
    es.close();
  });
})();
