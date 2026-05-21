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
        el(
          "div",
          ["tool"],
          "⚡ " + escapeHtml(d.action.tool_name || "")
        )
      );
      a.appendChild(el("pre", ["args"], escapeHtml(d.action.tool_input || "")));
      card.appendChild(a);
    }
    $messages.appendChild(card);
    scrollToBottom();
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
      $modeBadge.textContent = kind === "prompt" ? "ANSWERING" : "";
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
    if (e.key === "Enter" && !e.shiftKey) {
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

  es.addEventListener("prune", function (e) {
    const d = JSON.parse(e.data);
    pruneOldest(d.drop);
  });

  es.addEventListener("input_required", function (e) {
    const d = JSON.parse(e.data);
    setInputMode(d.kind, d);
  });

  es.addEventListener("input_resolved", function () {
    setInputMode("chat", null);
  });

  es.addEventListener("takeover", function () {
    showTakeoverNotice();
    es.close();
  });
})();
