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
  const $status = document.getElementById("conn-status");
  const $modeBadge = document.getElementById("input-mode-badge");
  const $inputArea = document.getElementById("input-area");

  // ── State ──────────────────────────────────
  let currentMode = "chat"; // "chat" | "prompt" | "confirm"
  let confirmDefaultKey = null;
  let streamingCard = null;
  let streamingText = "";

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

  /** Apply a tiny subset of markdown — fenced code blocks and inline
   * code. Everything else stays as escaped text. Phase D will swap
   * this for a real markdown renderer if/when the cost is justified. */
  function escapeAndFormat(s) {
    let html = escapeHtml(s);
    html = html.replace(
      /```(\w*)\n([\s\S]*?)```/g,
      function (_m, _lang, code) {
        return '<pre class="code"><code>' + code + "</code></pre>";
      }
    );
    html = html.replace(/`([^`\n]+)`/g, "<code>$1</code>");
    return html;
  }

  // ── DOM helpers ────────────────────────────
  function el(tag, classes, html) {
    const e = document.createElement(tag);
    if (classes && classes.length) e.classList.add.apply(e.classList, classes);
    if (html !== undefined && html !== null) e.innerHTML = html;
    return e;
  }

  function scrollToBottom() {
    $messages.scrollTop = $messages.scrollHeight;
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
    $messages.appendChild(card);
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
    card.appendChild(el("pre", ["obs-body"], escapeHtml(d.content || "")));
    $messages.appendChild(card);
    scrollToBottom();
  }

  function renderError(d) {
    const card = el("div", ["card", "card-error"]);
    card.textContent = d.content;
    $messages.appendChild(card);
    scrollToBottom();
  }

  // ── Streaming card (transient) ─────────────
  function ensureStreamingCard() {
    if (streamingCard) return;
    streamingCard = el("div", ["card", "card-streaming"]);
    streamingCard.appendChild(el("pre", ["streaming"], ""));
    $messages.appendChild(streamingCard);
  }
  function updateStreamingCard() {
    if (!streamingCard) return;
    streamingCard.querySelector(".streaming").textContent = streamingText;
    scrollToBottom();
  }
  function clearStreamingCard() {
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
      $input.placeholder =
        kind === "prompt"
          ? "Type your answer — Enter to send"
          : "Type a message — Enter to send, Shift+Enter for newline";
    }
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
  $send.addEventListener("click", function () {
    if (currentMode === "confirm") {
      // No textarea-only path in confirm — buttons are the contract.
      // Pressing Send falls back to the default option.
      submitConfirm(confirmDefaultKey);
    } else {
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
      if (currentMode === "confirm") {
        submitConfirm(confirmDefaultKey);
      } else {
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
    $info.textContent = d.provider + " · " + d.model;
  });

  es.addEventListener("user_message", function (e) {
    const d = JSON.parse(e.data);
    renderUserMessage(d.content);
  });

  es.addEventListener("assistant_turn", function (e) {
    const d = JSON.parse(e.data);
    clearStreamingCard();
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
    streamingText += d.text;
    ensureStreamingCard();
    updateStreamingCard();
  });

  es.addEventListener("stream_end", function () {
    // assistant_turn will replace the streaming card with the
    // structured version; nothing to do here.
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

  es.addEventListener("takeover", function () {
    showTakeoverNotice();
    es.close();
  });
})();
