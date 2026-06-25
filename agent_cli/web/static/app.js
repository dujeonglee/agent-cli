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
        el("span", ["sys-text"], "컨텍스트 압축 중… (" + fmtTok(d.old_tokens) + " tok)")
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
        "컨텍스트 압축됨 " + fmtTok(d.old_tokens) + " → " + fmtTok(d.new_tokens) + " tok";
    } else if (d.phase === "warning") {
      line.classList.add("warn");
      textEl.textContent = "컨텍스트 압축 실패 (" + (d.reason || "") + ") — FIFO 사용";
    }
    delete compactionLines[scope];
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
    // A `delegate` observation is a subagent's prose answer (the
    // STATUS/RESULT/[Task N]/[Duration] wrapper around markdown text), so
    // render it through the markdown pipeline like an assistant turn. Every
    // other tool's output (read_file hashlines, shell text, write/edit diffs)
    // is monospace/structured → keep the <pre> + diff colouring.
    if ((d.tool_name || "") === "delegate") {
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

  function renderConfirmButtons(options, defaultKey, data) {
    clearConfirmButtons();
    const container = el("div");
    container.id = "confirm-buttons";
    const meta = buildPromptMetaEl(data, true);
    if (meta) container.appendChild(meta);
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

  es.addEventListener("auto_review", function (e) {
    // Sticky toggle state from the server (shared across all browsers).
    // Bridge to the toggle button's own IIFE via a document CustomEvent so
    // every viewer's button reflects the latest value (live + reconnect).
    const d = JSON.parse(e.data);
    document.dispatchEvent(
      new CustomEvent("agentcli:auto_review", { detail: !!d.enabled }),
    );
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
    // Nudge the Prompt Inspector (separate IIFE) to refresh its scope chips
    // if it's open, so a new sub-agent's chip appears live.
    window.dispatchEvent(new CustomEvent("agent-cli:scopes-changed"));
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
    fetch("/api/nickname?token=" + encodeURIComponent(token), {
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
          fetch("/api/queue/cancel?token=" + encodeURIComponent(token), {
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
            '<div class="insp-divider">── 동적 컨텍스트 (대화 · 관찰) ──</div>';
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
    return fetch("/api/debug/prompt/scopes?" + qtoken())
      .then(function (r) { return r.json(); })
      .then(function (d) { renderChips((d && d.scopes) || []); })
      .catch(function () { renderChips([]); });
  }

  function loadPrompt() {
    const q = activeScope
      ? "?" + qtoken() + "&task_id=" + encodeURIComponent(activeScope)
      : "?" + qtoken();
    return fetch("/api/debug/prompt" + q)
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
      "/api/debug/prompt?" + qtoken() + "&task_id=" + encodeURIComponent(id),
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

  function open() {
    $backdrop.hidden = false;
    requestAnimationFrame(function () {
      $backdrop.classList.add("open");
      $drawer.classList.add("open");
    });
    $drawer.setAttribute("aria-hidden", "false");
    loadScopes().then(loadPrompt);
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
        kind: "delegate",
        label: t ? t.innerText.trim() : "delegate",
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
      const resp = await fetch("/api/export/html?" + qtoken(), {
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
      const r = await fetch("/api/export/jira/targets?" + qtoken());
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
      const r = await fetch("/api/export/jira?" + qtoken(), {
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
  const $all = document.getElementById("dl-all");
  const $tree = document.getElementById("dl-tree");
  const $count = document.getElementById("dl-count");
  const $go = document.getElementById("dl-download");
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

  function setUploadDir(rel, rowEl) {
    uploadDir = rel || "";
    if ($targetRow) $targetRow.classList.remove("target");
    $targetRow = rowEl || null;
    if ($targetRow) $targetRow.classList.add("target");
    $target.innerHTML = "⬆ 업로드 대상: <b>" + (uploadDir ? esc(uploadDir) : "(루트)") + "</b>";
  }
  const esc = (s) =>
    s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  const fmtSize = (n) =>
    n == null ? "" : n < 1024 ? n + "B" : n < 1048576
      ? (n / 1024).toFixed(0) + "KB" : (n / 1048576).toFixed(1) + "MB";

  function updateCount() {
    if ($all.checked) {
      $count.textContent = "whole workspace";
    } else {
      $count.textContent = selected.size + " selected";
    }
  }

  async function fetchTree(path) {
    const r = await fetch("/api/workspace/tree?" + qt() + "&path=" + encodeURIComponent(path));
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
            $msg.textContent = "로드 실패: " + e.message;
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
      // ("this folder") — drops then land here.
      label.addEventListener("click", () => {
        setUploadDir(entry.rel, row);
        onToggle();
      });
      wrap.appendChild(kids);
    }
    return wrap;
  }

  async function open() {
    $backdrop.hidden = false;
    $backdrop.classList.add("open");
    $drawer.classList.add("open");
    $drawer.setAttribute("aria-hidden", "false");
    selected.clear();
    $all.checked = false;
    setUploadDir("", null); // default upload target = root
    // reset the dim/disable that the All checkbox applies — otherwise a
    // prior All-download leaves the tree greyed out + unclickable on reopen
    $tree.style.opacity = "";
    $tree.style.pointerEvents = "";
    $msg.textContent = "";
    $tree.innerHTML = "<div class='dl-loading'>loading…</div>";
    updateCount();
    try {
      const entries = await fetchTree("");
      $tree.innerHTML = "";
      entries.forEach((e) => $tree.appendChild(makeRow(e, 0)));
      if (!entries.length) $tree.innerHTML = "<div class='dl-loading'>(empty)</div>";
    } catch (e) {
      $tree.innerHTML = "<div class='dl-loading'>로드 실패: " + esc(e.message) + "</div>";
    }
  }

  function close() {
    $backdrop.classList.remove("open");
    $drawer.classList.remove("open");
    $drawer.setAttribute("aria-hidden", "true");
    setTimeout(() => { $backdrop.hidden = true; }, 200);
  }

  $all.addEventListener("change", () => {
    $tree.style.opacity = $all.checked ? "0.4" : "";
    $tree.style.pointerEvents = $all.checked ? "none" : "";
    updateCount();
  });

  async function download() {
    const payload = $all.checked
      ? { all: true }
      : { paths: Array.from(selected) };
    if (!$all.checked && selected.size === 0) {
      $msg.textContent = "선택된 항목이 없습니다";
      return;
    }
    $go.disabled = true;
    $msg.textContent = "압축 중…";
    try {
      const r = await fetch("/api/workspace/download?" + qt(), {
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
      $msg.textContent = "다운로드 시작: " + fname;
    } catch (e) {
      $msg.textContent = "실패: " + e.message;
    } finally {
      $go.disabled = false;
    }
  }

  // ── Upload — items are {file, name} where name is the file's path relative
  // to the target dir ("a.txt" for a single file, "mydir/sub/a.c" for a
  // directory upload; the server creates the nested dirs). ─────────────
  function uploadOne(item) {
    const q =
      "/api/workspace/upload?" +
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
      entries.forEach((e) => $tree.appendChild(makeRow(e, 0)));
      setUploadDir("", null);
    } catch (e) {
      /* leave the tree as-is on refresh failure */
    }
  }

  function uploadItems(items) {
    if (!items || !items.length) return;
    const where = uploadDir ? uploadDir + "/" : "(루트)";
    $msg.textContent = "업로드 중 → " + where + " (" + items.length + ")";
    const out = [];
    let done = 0;
    items.forEach((it) => {
      uploadOne(it)
        .then((res) => {
          out.push(
            res.ok
              ? "✓ " + res.d.rel + (res.d.overwritten ? " (덮어씀)" : "")
              : "✗ " + esc(it.name) + " — " + (res.d.detail || res.status)
          );
        })
        .catch(() => out.push("✗ " + esc(it.name) + " — 네트워크 오류"))
        .then(() => {
          done += 1;
          if (done === items.length) {
            $msg.innerHTML = out.join("<br>");
            if (out.some((s) => s[0] === "✓")) refreshTree();
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
// Toggles the server's auto-review state (POST /api/auto_review). When on,
// the worker runs a reviewer agent after each completion and keeps reviewing
// until it accepts (or the toggle goes off). The state is SHARED across all
// browsers: the server broadcasts ``auto_review`` (sticky) and the main SSE
// handler relays it here via a document CustomEvent, so every viewer's button
// stays in sync (live + on reconnect via snapshot), not just the clicker's.
(function () {
  "use strict";

  const token = new URLSearchParams(window.location.search).get("token");
  const $btn = document.getElementById("auto-review-btn");
  if (!$btn) return;
  let enabled = false;

  function paint() {
    $btn.setAttribute("aria-pressed", enabled ? "true" : "false");
    $btn.classList.toggle("active", enabled);
    $btn.textContent = enabled ? "🔍 Review: on" : "🔍 Review: off";
  }

  // Sync from the server broadcast (any browser toggling, or our own
  // reconnect snapshot). Server is the source of truth.
  document.addEventListener("agentcli:auto_review", function (e) {
    enabled = !!e.detail;
    paint();
  });

  $btn.addEventListener("click", function () {
    // POST the intended next state; the button repaints from the resulting
    // ``auto_review`` broadcast (so all viewers converge on the server value).
    fetch("/api/auto_review?token=" + encodeURIComponent(token), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ enabled: !enabled }),
    }).catch(function () {
      /* leave the toggle as-is on failure */
    });
  });

  paint();
})();
