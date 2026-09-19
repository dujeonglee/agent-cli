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
  const $inputArea = document.getElementById("input-area");

  // ── State ──────────────────────────────────
  // 3b: 입력창은 항상 chat(활성 채널로 라우팅). main 의 prompt/confirm 은 글로벌
  // ask 트레이의 항목으로 렌더된다(ovMainAsk) — 입력창 mode 전환 폐지.
  var ovMainAsk = null; // null | {kind:"prompt"|"confirm", data}
  // Every connection is equal (all may send input / queue). ``myConnId`` (from
  // the ``identity`` event) is used to mark "(you)" in the viewer roster and
  // to own queued messages.
  let myConnId = null;
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
    // main ask(prompt/confirm) 대기 중엔 #chat-stop 을 숨긴다 — 그땐 런을
    // 중단(#chat-stop)하는 게 아니라 입력 대기를 취소(#abort)하는 상황.
    // (예전 currentMode 게이트를 ovMainAsk 로 대체 — 두 Stop 동시노출 방지.)
    return workerBusy && !ovMainAsk;
  }

  function updateSendEnabled() {
    // Send is always enabled (chat idle → starts a run; chat busy → queues).
    // Stop is a SEPARATE button shown only while a chat run is in flight.
    const busy = isBusyChat();
    const stopping = busy && stopRequested;
    $send.disabled = false;
    $send.textContent = "Send";
    if ($chatStop) {
      $chatStop.hidden = !busy;
      $chatStop.disabled = stopping;
      $chatStop.textContent = stopping ? "Stopping…" : "Stop";
    }
    // placeholder 는 채널(+busy) 인지형 — ovApplyChannelInput 단일 소유.
    ovApplyChannelInput();
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
  // P0-6②: el() 의 3번째 인자는 **textContent** — 기본이 안전(모델/서버 원문을
  // 그대로 넘겨도 마크업 실행 불가). HTML 이 필요한 곳만 elHtml() 로 **명시**한다.
  // 종전엔 el() 이 무조건 innerHTML 이라 콜사이트마다 escapeHtml 규율이 갈렸고
  // (606 은 이스케이프, 실패 카드는 누락 → self-XSS), 누락 = 취약이었다.
  function el(tag, classes, text) {
    const e = document.createElement(tag);
    if (classes && classes.length) e.classList.add.apply(e.classList, classes);
    if (text !== undefined && text !== null) e.textContent = text;
    return e;
  }
  // 이미 이스케이프/조립된 HTML 전용 — 새 콜사이트는 "왜 HTML 인가"가 자명할
  // 때만 사용(마크다운 렌더·diff 색상·danger 하이라이트 류).
  function elHtml(tag, classes, html) {
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
  // Attach a muted timestamp to any `.card`. No-op when `ts` is absent
  // (e.g. legacy buffered events) so nothing breaks if the field is missing.
  //
  // 자리는 **둘 중 하나**다 (v9.4.0 ⑥):
  //   · 행(.row)이 있는 카드 → **첫 행 안**의 한 칸 (.row-time)
  //   · 그 외(사용자 말풍선·시스템 줄·최종답) → 우상단 코너 배지 (.card-time)
  // 코너 배지는 absolute 라 그 아래 놓이는 줄의 글자를 덮어 **겹쳐 보였다**
  // (사용자 지적 2회 — 왕래 줄의 상대 칩, 그리고 생각/도구/관찰의 요약).
  // 여백을 상수로 비워두는 방식은 배지 폭 추정에 기대 깨지기 쉬우므로, 행이
  // 있으면 같은 그리드 안에 넣는다 — 겹칠 자리가 원천적으로 없다. 행이 없는
  // 산문 블록만 `--time-w` 로 자리를 비운다.
  function stampCard(cardEl, ts) {
    if (ts == null) return cardEl;
    const row = cardEl.querySelector(".row");
    const t = el("span", [row ? "row-time" : "card-time"], fmtCardTime(ts));
    t.title = fmtCardTimeFull(ts);
    if (row) {
      row.classList.add("has-time");
      row.insertBefore(t, row.querySelector(".x"));
    } else {
      cardEl.appendChild(t);
    }
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

  // 렌더 병합 (v8.42.0 — 리뷰 §4.6 효율): 스냅샷 재생·스트리밍처럼 이벤트가
  // 동기로 폭주하는 구간에서 scrollTop=scrollHeight 쓰기가 이벤트마다 전체
  // 레이아웃을 강제해 O(N²) 였다 (888이벤트 스냅샷에서 ~400ms 블로킹 실측).
  // requestAnimationFrame 당 1회로 병합 — 폭주 구간에선 사실상 마지막 1회만
  // 실행되고, 라이브에서도 프레임당 1회로 준다. ~16ms 지연은 인지 불가.
  var _scrollQueued = false;
  function scheduleScroll() {
    if (_scrollQueued) return;
    _scrollQueued = true;
    requestAnimationFrame(function () {
      _scrollQueued = false;
      scrollToBottom();
    });
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
  // Group state per task_id: { card, header, body, statusEl, closed }.
  const taskGroups = {};
  // task_id → enclosing scope's task_id ("" = top level). Outlives the group
  // entry (which ``closeTaskGroup`` drops) because the CARDS stay in the
  // timeline: swimlane click-navigation still has to expand a finished
  // ancestor chain to reveal a nested card.
  const scopeParent = {};
  // ancestor task_id → [{id, label}] of its currently-running descendants.
  // A collapsed parent would otherwise hide live nested work completely.
  const liveKids = {};
  // task_id → **대화 채널**("main" 또는 상주 에이전트 key). 채널은 타임라인의
  // 필터다(v9.4.0 ⑥): `#messages` 의 **직계** 자식만 `data-ch` 로 걸러 보여준다.
  // 상주 에이전트의 작업 스코프는 `ctx_dir="agents/<key>"` 로 자기를 밝히므로
  // (render/web.py begin_agent_work) 서버에 새 필드를 더할 필요가 없다. 중첩
  // 스코프는 부모의 채널을 물려받는다 — 카드가 부모 body 안에 있어 필터가
  // 닿지 않지만, 그 안에서 append 되는 왕래 줄의 귀속에는 쓰인다.
  const scopeChannel = {};
  function channelOf(taskId) {
    return (taskId && scopeChannel[taskId]) || "main";
  }

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

  /** 스코프의 **채널 귀속**을 등록하고, 이 스코프가 곧 채널인지 돌려준다.
   *
   * 상주 에이전트 작업은 `ctx_dir="agents/<key>"` 로 자기를 밝힌다(서버의
   * `begin_agent_work`). 그런 스코프는 **채널 자체**라 카드를 만들지 않는다
   * (v9.7.0) — 채널이 이미 묶어 주는데 그 안에서 또 묶으면 아무 정보도 더하지
   * 않으면서 기본 접힘 뒤로 투명성을 숨기고, main 창은 평평한데 agent 창만 한
   * 겹 들어가 "같은 컨셉으로 통일"이 깨진다. 요청의 경계는 왕래 줄(`← 받음` /
   * `→ 보냄`)이 이미 긋고 있다.
   *
   * 카드를 안 만들어도 배관은 그대로다: `appendToTimeline` 은 그룹이 없으면
   * `channelOf(taskId)` 로 도장을 찍어 루트에 붙이는데, 그게 정확히 원하는
   * 동작이다. 그 스코프 **안에서** 열리는 skill/inline 은 자기 카드를 그대로
   * 갖고, 부모 그룹이 없으니 `channelOf(parent)` = 그 에이전트 채널의 루트에
   * 붙는다 — 중첩 블록은 살아 있다. */
  function noteScopeChannel(taskId, parent, ctxDir) {
    const m = /^agents\/(.+)$/.exec(ctxDir || "");
    scopeChannel[taskId] = m ? m[1] : channelOf(parent);
    return !!m;
  }

  /** 스코프 카드를 연다. 중첩 블록의 종류는 `kind` 가 가른다 (docs/chat-ui §3):
   * `skill` → 🪄 보라 레일, 그 외 → 🦀 inline agent · amber 레일.
   * 상주 에이전트는 여기 오지 않는다 (``noteScopeChannel`` 참조). */
  function ensureTaskGroup(taskId, index, agent, taskText, kind, parent, depth) {
    if (taskGroups[taskId]) return taskGroups[taskId];

    const card = el("div", ["card", "card-task-group"]);
    card.dataset.taskId = taskId;
    card.dataset.kind = kind || "run";
    card.dataset.depth = String(depth || 0);
    card.classList.add(kind === "skill" ? "scope-skill" : "scope-inline");

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
    // 채널 귀속은 **이 스코프 자신의** 것이다. appendToTimeline 은 중첩 위치를
    // 정하려고 `parent` 를 받으므로 부모 채널로 도장을 찍는데, 부모가 카드 없는
    // 상주 스코프면 루트로 떨어지면서 그 채널이 아니라 부모의 채널로 찍힌다
    // — 값은 같지만(자식은 부모 채널을 물려받는다) 출처를 자기 것으로 맞춘다.
    if (card.parentNode === $messages) {
      card.dataset.ch = scopeChannel[taskId];
      applyChannelFilter(card);
    }

    const group = {
      card: card,
      header: header,
      body: body,
      chevron: chevron,
      statusEl: statusEl,
      subEl: subEl,
      meta: meta,
      closed: false,
      toggle: toggleTaskGroup,
    };
    taskGroups[taskId] = group;
    scheduleScroll();
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
      const errEl = el("div", ["task-error"], error);
      g.body.appendChild(errEl);
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
      return;
    }
    // 루트 append 만 채널 필터의 대상이다. `data-ch` 가 **없는** 노드는 어느
    // 채널에서나 보인다(생성 중 한 줄처럼 채널과 무관한 표시).
    cardEl.dataset.ch = channelOf(taskId);
    $messages.appendChild(cardEl);
    applyChannelFilter(cardEl);
  }

  /** 에이전트 메일이 idle 한 main 을 깨워 런이 시작됐다 (v9.7.0).
   *
   * 종전엔 이게 `push_user_message` 로 흘러 **오른쪽 파란 말풍선**으로
   * 그려졌다 — 기계가 만든 깨우기 신호가 사람이 친 말과 구별되지 않았다
   * (사용자 제보). `card-sys`(압축 마커 같은 **휘발** 표시)로 보내지 않는
   * 이유는, 이건 그 런이 왜 시작됐는지를 설명하는 **기록**이고 모델
   * 컨텍스트에도 들어간 실제 턴 입력이라서다. 그래서 다른 내부 작업과 같은
   * 한 줄 리듬으로 그리고, 펼치면 **모델이 받은 원문**이 나온다 — 사람이 읽을
   * 요약과 모델을 움직인 지시문을 한 줄 안에서 분리한다.
   */
  // 채널별 **직전 최종답** — `→ 보냄` 줄이 그걸 그대로 반복하는지 판정한다.
  // 라이브에서는 최종답 바로 뒤에 회신 줄이 오므로 텍스트가 같으면 중복이고,
  // kill→resume 뒤에는 최종답이 재생되지 않아(`_replay_conversation` 은
  // `agent_message` 만 재발행) 같지 않다 → 그때는 줄이 내용을 싣는다.
  // 채널당 한 칸이고 최종답마다 덮어써서 자라지 않는다.
  const lastFinalByChannel = {};

  function renderAgentWake(d) {
    const card = el("div", ["card", "card-assistant"]);
    const raw = String((d && d.text) || "");
    card.appendChild(
      makeRow(
        "🤝",
        "메일",
        "에이전트 회신 도착 — 이어서 진행합니다",
        raw ? el("pre", ["args"], raw) : null,
        ["wake"]
      )
    );
    stampCard(card, d.ts);
    appendToTimeline(card, d.task_id);
    scheduleScroll();
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
      scheduleScroll();
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
    scheduleScroll();
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
    scheduleScroll();
  }

  // ── 스트림 무진전(stall) 표시 (v8.60.0) ──────────────────────
  // 종전엔 이 알림이 전부 ``status`` 이벤트로 왔고 여기 리스너가 없어
  // **통째로 드롭**됐다 — 사용자는 40분을 아무 표시 없이 기다린 뒤
  // "LLM call failed" 만 봤다. 두 부류로 갈라 표시한다:
  //   wait   = 30초마다 반복 → 카드 하나를 **제자리 갱신** (compaction
  //            마커가 scope 별 줄을 재사용하는 것과 동형)
  //   resend = 드문 전이 → 기록으로 남긴다
  //   clear  = 대기 종료 → 제자리 카드 제거
  let stallLine = null;
  function mmss(s) {
    const n = Math.max(0, Math.floor(Number(s) || 0));
    return Math.floor(n / 60) + ":" + String(n % 60).padStart(2, "0");
  }
  function renderStreamStall(d) {
    if (d.kind === "wait") {
      if (!stallLine || !stallLine.isConnected) {
        stallLine = el("div", ["card", "card-sys", "stall-line"]);
        stallLine.appendChild(el("span", ["sys-icon"], "⏳"));
        stallLine.appendChild(el("span", ["sys-text"]));
        const bar = el("div", ["stall-bar"]);
        bar.appendChild(el("i"));
        stallLine.appendChild(bar);
        appendToTimeline(stallLine, d.task_id);
        scheduleScroll();
      }
      const txt = stallLine.querySelector(".sys-text");
      const fill = stallLine.querySelector(".stall-bar > i");
      txt.textContent =
        "응답 대기 중 " +
        mmss(d.elapsed_s) +
        " / " +
        mmss(d.limit_s) +
        " · 시도 " +
        d.attempt +
        "/" +
        d.attempts;
      if (fill) {
        const pct = d.limit_s > 0 ? (d.elapsed_s / d.limit_s) * 100 : 0;
        fill.style.width = Math.max(0, Math.min(100, pct)) + "%";
      }
      return;
    }
    // 전이·정리 — 제자리 줄을 먼저 걷는다(낡은 경과 시간을 남기지 않는다).
    if (stallLine && stallLine.isConnected) stallLine.remove();
    stallLine = null;
    if (d.kind === "resend") {
      const line = el("div", ["card", "card-sys", "stall-resend"]);
      line.appendChild(el("span", ["sys-icon"], "↻"));
      line.appendChild(
        el(
          "span",
          ["sys-text"],
          "스트림 무응답 — 재연결 후 재전송 (시도 " +
            d.attempt +
            "/" +
            d.attempts +
            ")"
        )
      );
      appendToTimeline(line, d.task_id);
      scheduleScroll();
    }
  }

  // ── Card renderers ─────────────────────────

  function renderUserMessage(content, ts) {
    const card = el("div", ["card", "card-user"]);
    card.appendChild(elHtml("div", ["bubble"], escapeAndFormat(content)));
    stampCard(card, ts);
    // 루트 직결이 아니라 appendToTimeline 경유 — 채널 도장(`data-ch="main"`)을
    // 받아야 한다. 직결이면 도장이 없어 **모든 채널에서 보였다**(v9.4.0 ⑥
    // 실장에서 발견). 에이전트 채널의 사용자 입력은 여기 오지 않는다 —
    // /api/agent/<key>/input 을 거쳐 `agent_msg` 왕래 줄로 온다.
    appendToTimeline(card, "");
    scheduleScroll();
  }

  // ── 한 줄 리듬 (v9.4.0 ③, docs/chat-ui §2) ──────────────────
  // 모든 줄이 `아이콘 · 종류 · 한 줄 요약 · 펼침표시` 4칸 그리드다. 기본은
  // 한 줄이고 누르면 전문이 펼쳐진다 — **투명성은 깊이로, 간결함은 기본
  // 상태로**. 펼칠 게 있는 줄에만 ▸ 가 뜬다(없는 줄에 뜨면 눌러도 아무 일이
  // 없어 고장으로 읽힌다).
  function makeRow(icon, kind, summary, bodyNode, extraCls, tailNode) {
    const row = el("div", ["row"].concat(extraCls || []));
    row.appendChild(el("span", ["ic"], icon));
    row.appendChild(el("span", ["k"], kind));
    row.appendChild(el("span", ["s"], summary));
    // 꼬리 칸(선택): 왕래 줄의 **상대 이름**처럼 항상 보여야 하는 것. 펼침
    // 표시(`.x`)는 hover 전까지 투명하므로 거기에 섞을 수 없다 — 별도 칸으로
    // 두고 그리드를 5칸으로 늘린다(`.row.has-tail`).
    if (tailNode) {
      row.classList.add("has-tail");
      row.appendChild(tailNode);
    }
    const mark = el("span", ["x"]);
    row.appendChild(mark);
    if (bodyNode) {
      row.classList.add("can");
      mark.textContent = "▸";
      bodyNode.classList.add("row-body", "hide");
      row.appendChild(bodyNode);
      row.addEventListener("click", function (e) {
        // 본문 안의 선택·클릭(복사 등)은 접기를 유발하지 않는다.
        if (e.target.closest(".row-body")) return;
        const open = !bodyNode.classList.toggle("hide");
        row.classList.toggle("open", open);
        mark.textContent = open ? "▾" : "▸";
      });
    }
    return row;
  }

  /** 도구 호출 → 한 줄 요약. 길이는 CSS ellipsis 가 맡고, 여기선 **무엇을
   *  했는지 알아볼 수 있는 가장 짧은 문자열**을 고른다. */
  function actionSummary(tool, inputStr) {
    let p = {};
    try {
      p = JSON.parse(inputStr || "{}");
    } catch (_e) {
      p = {};
    }
    if (typeof p !== "object" || p === null) p = {};
    if (tool === "shell" || tool === "sh") return String(p.command || "");
    if (tool === "read_file") return String(p.path || "");
    if (tool === "write_file") return String(p.path || "");
    if (tool === "edit_file") {
      const n = Array.isArray(p.edits) ? p.edits.length : 0;
      return String(p.path || "") + (n ? " · " + n + "곳" : "");
    }
    if (tool === "run_skill") return String(p.name || "");
    if (tool === "agent") {
      const who = p.key || p.agent || p.profile || p.mode || "";
      const task = String(p.task || p.message || "");
      return (who ? who + (task ? " · " : "") : "") + task;
    }
    if (tool === "ask") {
      const qs = Array.isArray(p.questions) ? p.questions : [];
      return qs.length ? String(qs[0]) + (qs.length > 1 ? " 외 " + (qs.length - 1) : "") : "";
    }
    if (tool === "complete") return String(p.result || "");
    // 알 수 없는 도구: 첫 문자열 값이 대개 가장 설명적이다
    for (const k of Object.keys(p)) {
      if (typeof p[k] === "string" && p[k]) return p[k];
    }
    return "";
  }

  /** 관찰 출력 → 한 줄 요약. **마지막** 유의미한 줄을 쓴다 — 셸·테스트·린트
   *  출력의 결론이 대개 끝에 있고(3826 passed / All checks passed), 실패도
   *  끝에서 드러난다. 첫 줄은 명령 에코나 헤더라 정보가 적다. */
  function obsSummary(content) {
    const lines = String(content || "").split("\n").filter((l) => l.trim());
    if (!lines.length) return "(출력 없음)";
    return lines[lines.length - 1].trim();
  }

  /** `⚡ agent` 도구 호출의 대상 칩 — roster 에 있는 **상주** 에이전트일 때만.
   * 일회성 위임(`run`)은 대상이 채널이 아니라 이 카드 안의 중첩 블록이라
   * 갈 곳이 없다(docs/chat-ui §3). 없으면 null → 꼬리 칸 자체가 안 생긴다. */
  function agentJumpChip(inputStr) {
    let p = {};
    try {
      p = JSON.parse(inputStr || "{}");
    } catch (_e) {
      return null;
    }
    const key = p && typeof p.key === "string" ? p.key : "";
    if (!key || !ovRoster.some((t) => t.key === key)) return null;
    const chip = el("span", ["peer", "can-jump"], ovAgentLabel(key));
    chip.title = ovAgentLabel(key) + " 채널로 이동";
    chip.addEventListener("click", function (e) {
      e.stopPropagation();
      ovJump(key, "");
    });
    return chip;
  }

  function renderAssistantTurn(d) {
    const card = el("div", ["card", "card-assistant"]);
    // 💭 생각 — 한 줄 요약(첫 줄), 전문은 펼쳐서. reasoning 이 길면 대화가
    // 통째로 밀리므로 기본은 접는다.
    if (d.thought) {
      const t = String(d.thought).trim();
      const first = t.split("\n").find((l) => l.trim()) || t;
      const multi = t !== first.trim();
      card.appendChild(
        makeRow(
          "💭",
          "생각",
          first.trim(),
          multi ? elHtml("div", ["md"], escapeAndFormat(t)) : null,
          ["think"]
        )
      );
    }
    if (d.final !== undefined) {
      // 최종 답변은 **접지 않는다** — 읽히려고 있는 것이고, 접으면 대화가
      // 아니라 로그가 된다.
      card.appendChild(elHtml("div", ["final"], escapeAndFormat(d.final)));
      const fch = channelOf(d.task_id);
      if (fch !== "main") lastFinalByChannel[fch] = String(d.final).trim();
    } else if (d.action) {
      const tool = d.action.tool_name || "";
      const input = d.action.tool_input || "";
      // main → agent 점프의 출발점. main 쪽에서 상주 에이전트에게 거는 일은
      // **왕래 줄이 아니라 `⚡ agent` 도구 호출**로 나타나므로(docs/chat-ui §5),
      // 상대 칩을 여기 붙인다. 반대 방향(agent → main)은 매칭 키가 달라
      // 이번 범위 밖 — 돌아가기 버튼이 그 자리를 채운다.
      card.appendChild(
        makeRow("⚡", tool, actionSummary(tool, input),
                renderActionInput(tool, input), ["act"],
                tool === "agent" ? agentJumpChip(input) : null)
      );
    }
    stampCard(card, d.ts);
    appendToTimeline(card, d.task_id);
    scheduleScroll();
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
      return el("pre", ["args"], toolInputStr);
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
        "$ " + parsed.command
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
      return elHtml("div", ["action-detail"], parts.join(" "));
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
      return elHtml(
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
              " → " + String(t.agent || t.profile)
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
        return elHtml(
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
      return elHtml("div", ["final"], escapeAndFormat(parsed.result));
    }

    // Fallback: pretty JSON. Two-space indent keeps wide objects readable
    // without burning horizontal real estate.
    return el(
      "pre",
      ["args"],
      JSON.stringify(parsed, null, 2)
    );
  }

  function renderObservation(d) {
    const card = el("div", ["card", "card-observation"]);
    card.classList.add(d.success ? "ok" : "fail");
    const content = d.content || "";
    const tool = d.tool_name || "";
    // An `agent` observation is a subagent's prose answer (run 결과의
    // STATUS/RESULT/[Task N]/[Duration] wrapper, 상주 회신 배달), so
    // render it through the markdown pipeline like an assistant turn. Every
    // other tool's output (read_file hashlines, shell text, write/edit diffs)
    // is monospace/structured → keep the <pre> + diff colouring.
    const body =
      tool === "agent"
        ? elHtml("div", ["obs-body", "obs-md"], escapeAndFormat(content))
        : elHtml("pre", ["obs-body"], colorizeDiffBody(escapeHtml(content)));
    // 실패는 **펼친 채로** 둔다 — 접힌 한 줄로는 왜 실패했는지 알 수 없고,
    // 사용자가 지금 봐야 하는 유일한 줄이다.
    // 종류 칸은 **도구 이름** — "결과"라고만 쓰면 어느 도구의 결과인지 알 수
    // 없다(브라우저 TC 가 잡은 실수). 바로 위 ⚡ 행과 같은 이름이 서로를
    // 가리키므로 호출↔결과 짝이 눈으로 붙는다.
    const row = makeRow(
      d.success ? "✓" : "✗", tool || "결과", obsSummary(content), body,
      [d.success ? "ok" : "bad"]
    );
    if (!d.success) {
      body.classList.remove("hide");
      row.classList.add("open");
      const mark = row.querySelector(".x");
      if (mark) mark.textContent = "▾";
    }
    card.appendChild(row);
    stampCard(card, d.ts);
    appendToTimeline(card, d.task_id);
    scheduleScroll();
  }

  function renderError(d) {
    const card = el("div", ["card", "card-error"]);
    card.textContent = d.content;
    stampCard(card, d.ts);
    appendToTimeline(card, d.task_id);
    scheduleScroll();
  }

  // ── 생성 중 표시 (v9.4.0 ④, docs/chat-ui §2) ────────────────────
  // 스트리밍(라이브 타이핑)을 버리고 **한 줄**만 남긴다:
  //     ● 생성 중 · 1.2K tokens · 💭 사고 2.1K
  // 숫자가 오르면 살아 있다는 뜻이고, 사고 토큰이 따로 잡히면 러너웨이가
  // 바로 보인다. 본문이 흐르지 않으므로 **카드가 자라며 화면이 튀지 않는다**.
  // 서버는 0.5s 스로틀 tick 만 보내므로 트래픽도 토큰 수와 무관하다.
  let genEl = null;
  let genBody = 0;    // 본문 누적 토큰
  let genThink = 0;   // 사고 누적 토큰
  // fmtTok 은 헤더 토큰바가 이미 가진 것을 쓴다 (같은 IIFE — 중복 선언 금지).

  function showGenerating() {
    if (!genEl) {
      genEl = el("div", ["gen"]);
      genEl.appendChild(el("span", ["gen-pulse"]));
      genEl.appendChild(el("span", ["gen-label"], "생성 중"));
      genEl.appendChild(el("span", ["gen-tok"]));
      $messages.appendChild(genEl);
    }
    const parts = [];
    if (genBody) parts.push("· " + fmtTok(genBody) + " tokens");
    if (genThink) parts.push("· 💭 사고 " + fmtTok(genThink));
    genEl.querySelector(".gen-tok").textContent = parts.join(" ");
    scheduleScroll();
  }

  function hideGenerating() {
    if (genEl) {
      genEl.remove();
      genEl = null;
    }
    genBody = 0;
    genThink = 0;
  }

  // 거부된 원문(raw)을 실패 카드로. 종전엔 라이브 스트리밍 카드를 마감하는
  // 경로가 주였으나, 스트리밍이 없어져 **원문 표시**만 남는다 — 모델이 무엇을
  // 뱉어 거부됐는지는 여전히 봐야 한다.
  // ``taskId`` 를 **반드시** 넘긴다. 거부된 응답은 그걸 낸 주체의 것이라, 빠뜨리면
  // 에이전트가 낸 실패가 main 대화에 빨간 박스로 뜬다(사용자 보고). `_emit` 이
  // `failed_turn` 에 스코프 task_id 를 이미 실어 보내므로 리스너가 흘리기만 하면 됐다.
  function renderFailedEmission(reason, raw, taskId) {
    if (!raw && !reason) return;
    const card = el("div", ["card", "card-failed"]);
    // P0-6②: reason/raw 는 모델·서버 원문 — el() 이 textContent 기반이라
    // 원문 그대로 안전(과거 innerHTML 시절 미이스케이프 self-XSS 의 수리 지점).
    if (raw) card.appendChild(el("pre", ["streaming"], raw));
    if (reason) card.appendChild(el("div", ["fail-reason"], "⚠ " + reason));
    appendToTimeline(card, taskId);
    scheduleScroll();
  }

  // ── Input mode switching ───────────────────
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

  // 입력창은 항상 chat(3b): 활성 채널로 라우팅(main → /api/input, agent →
  // /api/agent/<key>/input). main 의 prompt/confirm 답변은 글로벌 트레이가 담당.
  function submitChatOrPrompt() {
    const text = $input.value.trim();
    if (!text) return;
    if (ovActiveChannel !== "main") {
      fetch("api/agent/" + encodeURIComponent(ovActiveChannel) + "/input", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ content: text, conn_id: myConnId }),
      });
      $input.value = "";
      return;
    }
    postInput({ kind: "chat", content: text });
    $input.value = "";
  }

  // main ask 를 접는다 — 409(선착순 소비) / input_resolved 공용.
  function ovFoldMainAsk() {
    ovMainAsk = null;
    clearConfirmStall();
    ovRenderAskTray();
    updateSendEnabled(); // ovMainAsk 해제 → busy 면 #chat-stop 다시 노출
    setAbortVisible(false);
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

  // main prompt(ask) 답변 — 트레이 항목의 입력에서 호출.
  function ovSubmitMainPrompt(text) {
    if (!text.trim()) return;
    postInput({ kind: "prompt", content: text }).then(function (res) {
      if (res && res.status === 409) ovFoldMainAsk(); // 이미 응답됨(다른 뷰어)
    });
  }

  // main confirm 결정 — 트레이 항목의 옵션 버튼에서 호출(key + 선택 코멘트).
  function ovSubmitMainConfirm(key, comment) {
    clearConfirmStall();
    confirmStallTimer = setTimeout(function () {
      var item = document.querySelector(".ask-main");
      if (!ovMainAsk || ovMainAsk.kind !== "confirm" || !item) return;
      if (item.querySelector(".confirm-stall")) return;
      const warn = el("div", ["confirm-stall"]);
      warn.textContent =
        "⚠ No response from the server yet — the connection may be " +
        "stalled (e.g. too many open tabs holding connections to this " +
        "host). The click applies as soon as it gets through; closing " +
        "unused tabs can help.";
      item.appendChild(warn);
    }, CONFIRM_STALL_MS);
    postInput({ kind: "confirm", key: key, comment: comment || "" })
      .then(function (res) {
        if (res && res.status === 409) ovFoldMainAsk(); // 이미 응답됨
      })
      .catch(function () {
        /* network error — the stall warning covers the visible feedback */
      });
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
    // 입력창은 항상 chat(idle→런 시작·busy→큐). main ask 답변은 트레이가 담당.
    submitChatOrPrompt();
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
      submitChatOrPrompt(); // chat (queues if busy). Stop 은 별도 버튼.
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
    // 상주 에이전트 목록/상태 sticky (P4) — Team 스윔레인 + 개요 채널 바.
    const d = JSON.parse(e.data);
    ovOnRoster(d); // 개요: 채널 바 + ask 트레이 + 상태
  });

  es.addEventListener("agent_msg", function (e) {
    // 상주 에이전트 대화 메시지 (persistent — 재접속 replay 포함).
    const d = JSON.parse(e.data);
    ovOnAgentMsg(d); // 개요: agent 대화 채널 스트림
  });

  es.addEventListener("agent_cleared", function (e) {
    // kill 시 그 에이전트 대화 비움 (resume 재생과 대칭 — 안 지우면 부활 시
    // conversation.jsonl 재생이 채널 스트림에 중복 append). 5단계: 드로어
    // 대신 개요 채널을 정리.
    const d = JSON.parse(e.data);
    if (d && d.key) ovOnAgentCleared(d.key);
  });

  es.addEventListener("compaction_ratio", function (e) {
    // 5.13: 다른 뷰어가 압축 슬라이더를 바꾸면 sticky 로 전파 — 슬라이더
    // IIFE 로 중계해 이 탭의 슬라이더도 동기화한다.
    document.dispatchEvent(
      new CustomEvent("agentcli:compaction", { detail: JSON.parse(e.data) }),
    );
  });

  es.addEventListener("stream_idle", function (e) {
    // P3: 다른 뷰어의 Stall(무진전 한도) 변경을 IIFE 로 중계.
    document.dispatchEvent(
      new CustomEvent("agentcli:streamidle", { detail: JSON.parse(e.data) }),
    );
  });

  es.addEventListener("thinking_tick", function (e) {
    // P5/v8.57.0: 사고 러너웨이 가시화 — 헤더 토큰바의 #tok-think 세그먼트에
    // 💭 카운트를 부착. token_usage(턴 경계)가 #tok-think 를 비워 자연 소멸.
    const d = JSON.parse(e.data);
    if (typeof d.tokens !== "number") return;
    const $think = document.getElementById("tok-think");
    if ($think) $think.textContent = " · 💭 " + fmtTok(d.tokens);
    // v9.4.0 ④: 생성 중 줄에도 싣는다. 사고만 하고 본문이 아직 없는 구간
    // (러너웨이가 정확히 그 모양이다)에서도 살아있음이 보여야 한다 —
    // 헤더 배지만 갱신하면 대화 쪽은 조용해 멎은 것처럼 읽힌다.
    genThink = d.tokens;
    showGenerating();
  });

  es.addEventListener("max_agents", function (e) {
    // 5.16: 다른 뷰어가 에이전트 상한을 바꾸면 sticky 로 전파 — maxagents
    // IIFE 로 중계해 이 탭의 입력/체크박스도 동기화한다.
    document.dispatchEvent(
      new CustomEvent("agentcli:maxagents", { detail: JSON.parse(e.data) }),
    );
  });

  es.addEventListener("confirm_mode", function (e) {
    // ⚡ 자동 승인 체크박스 동기화 — 다른 뷰어가 바꾸면 sticky 로 전파.
    document.dispatchEvent(
      new CustomEvent("agentcli:confirmmode", { detail: JSON.parse(e.data) }),
    );
  });

  es.addEventListener("thinking_mode", function (e) {
    // 🧠 사고/노력 컨트롤 동기화 — 다른 뷰어가 바꾸면 sticky 로 전파.
    document.dispatchEvent(
      new CustomEvent("agentcli:thinkingmode", { detail: JSON.parse(e.data) }),
    );
  });

  es.addEventListener("directives_changed", function () {
    // Someone saved DIRECTIVE.md via the Prompt Inspector → tell the inspector
    // IIFE to re-fetch the editor so concurrent editors don't show stale text.
    window.dispatchEvent(new CustomEvent("agentcli:directives-changed"));
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
    // v8.57.0: ↑(턴 입력)은 ctx 분자와 동일 값이라 생략 — ↓(출력)·Σ↓(누적)만.
    if (inTok || d.out) {
      parts.push("↓" + fmtTok(d.out));
    }
    if (d.total_out) {
      parts.push("Σ↓" + fmtTok(d.total_out));
    }
    // base 세그먼트는 #tok-base, 💭 세그먼트는 thinking_tick 가 #tok-think 에
    // 부착 — 이 갱신(턴 경계)이 오면 💭 는 클리어돼 자연 소멸한다.
    var $base = document.getElementById("tok-base");
    var $think = document.getElementById("tok-think");
    if ($base) $base.textContent = parts.join(" · ");
    if ($think) $think.textContent = "";
    $tokenUsage.title =
      "context " +
      fmtTok(inTok) +
      " / " +
      fmtTok(win) +
      " · turn out " +
      fmtTok(d.out) +
      " · session out " +
      fmtTok(d.total_out);
    // v8.57.0: 토큰 상세는 헤더에 상시 노출 (구 ctx 게이지 칩 폐기).
    if (inTok && win) $tokenUsage.hidden = false;
  });

  es.addEventListener("user_message", function (e) {
    const d = JSON.parse(e.data);
    renderUserMessage(d.content, d.ts);
    // ``author`` (a user's nickname) puts the message on the swimlane's
    // multiplexed user lane; author-less messages (🤝 starter) are card-only.
    qOnUserMsg(d); // 큐: 이 메시지가 큐서 주입된 것이면 ✓ 영수증
  });

  es.addEventListener("assistant_turn", function (e) {
    const d = JSON.parse(e.data);
    hideGenerating();
    renderAssistantTurn(d);
  });

  es.addEventListener("failed_turn", function (e) {
    const d = JSON.parse(e.data);
    hideGenerating();
    renderFailedEmission(d.reason, d.raw, d.task_id);
    // failed_turn 은 서버의 ``recovery()`` 에서만 나온다 — **포맷 복구 후
    // 같은 런이 재시도**한다는 뜻이지 런 종료가 아니다. 런 종료 정리는
    // worker_state idle 이 소유한다.
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

  es.addEventListener("agent_wake", function (e) {
    renderAgentWake(JSON.parse(e.data));
  });

  es.addEventListener("stream_stall", function (e) {
    renderStreamStall(JSON.parse(e.data));
  });

  // v9.4.0 ④: stream_reset 리스너 제거 — 버릴 **부분 출력이 없다**.
  // (v8.61.0 이 재전송 시 이어붙기를 고치려 넣은 것인데, 라이브 타이핑을
  // 버리면서 그 문제 자체가 사라졌다. 서버 방출은 CLI 마르퀴가 쓰므로 유지.)

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
    scheduleScroll();
  });

  // 본문·사고 tick — 토큰 수만 온다(텍스트 없음).
  es.addEventListener("stream_tick", function (e) {
    genBody = (JSON.parse(e.data) || {}).tokens || 0;
    showGenerating();
  });

  es.addEventListener("stream_end", function () {
    hideGenerating();
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
    // NOTE: a resume-replayed scope (``d.replay``) builds its card too. It used
    // to return here — the reasoning being that the scope's turns replayed flat,
    // so the card would be an empty shell. Since v7.28.0 the resume replay is
    // ONE time-ordered stream (``replay_session``) and each turn carries the
    // scope it belongs to, so the card opens BEFORE its turns arrive and they
    // land inside it. Skipping it now would be the bug: no card at all, and a
    // swimlane bar offering navigation to something that does not exist.
    // 채널 귀속은 카드와 무관한 등록이라 먼저, 그리고 **채널 자체인 스코프는
    // 카드를 만들지 않는다**(v9.7.0 — 자기 채널 안에서 자기를 또 묶지 않는다).
    const grp = noteScopeChannel(d.task_id, d.parent || "", d.ctx_dir || "")
      ? null
      : ensureTaskGroup(
          d.task_id,
          d.index || 0,
          d.agent || "",
          d.label || "",
          d.kind || "run",
          d.parent || "",
          d.depth || 0,
        );
    // Register the parent link + light up the ancestors' "child running" hint.
    // AFTER ensureTaskGroup so the new card is already nested inside its parent.
    noteScopeStart(d.task_id, d.parent || "", d.agent || d.label || "scope");
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
    noteScopeEnd(d.task_id);
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

  // ── 타임라인 = 유일한 표면 (v9.4.0 ②, docs/chat-ui) ──
  // 종전엔 개요(요약) / 전문(드로어) / 흐름(스윔레인) 셋이 같은 대화를 각자
  // 그렸고, 투명성(도구 호출·reasoning)이 **드로어 안에만** 있었다. 기본 화면인
  // 개요엔 도구가 아예 안 나와(슬래시만 화이트리스트 예외) "간단"과 "투명"이
  // 둘 다 손해였다. 이제 `#messages` 하나다 — 뷰 전환·드로어·탭이 전부 없다.
  //
  // 에이전트 작업이 사라지지 않는 이유: `begin_agent_work` 가 task_id 를 붙여
  // `scope_start` 를 내고 `_emit` 이 후속 이벤트에 자동 부착하므로, 에이전트의
  // 생각·도구·결과는 **이미** 여기 중첩 카드로 들어와 있었다(§7.5). 개요를
  // 지우는 것은 잃는 게 아니라 드러내는 것이다.

  // ── 채널·트레이 상태 (구 개요의 잔존분) ─────────────────
  // ② 가 개요 렌더를 지웠을 때 그 **데이터**(ovEntries/ovTopScopes/ovScopeSrc/
  // ovSkills)는 선언만 남아 아무도 읽지 않았다 — ⑥ 에서 함께 걷어낸다. 남는
  // 것은 실제로 읽히는 둘: roster(채널 칩·점프 가능 판정의 진실원)와 활성 채널.
  var ovRoster = []; // agent_roster
  // 대화 채널. "main" = 메인, 그 외 = 상주 agent key. 채널은 **타임라인의
  // 필터**다(v9.4.0 ⑥) — 별도 스트림을 들고 있지 않으므로 리로드하면
  // replay 버퍼가 그대로 복원한다.
  var ovActiveChannel = "main";
  // 3단계: 글로벌 ask 트레이 — agent 질문(waiting_ask)을 채널 무관하게 노출.
  // key → {text, ts}. 실제 표시 여부는 roster 의 state==="waiting_ask" 가 진실.
  var ovAskTray = {};
  // 에이전트별 결정적 아이콘 — key 해시로 고정. 서버(agent_cli/agent_icon.py)와
  // 반드시 동일한 풀·순서·해시(test_app_markdown 이 대조). key 는 ASCII 라
  // charCodeAt == Python ord.
  var OV_AGENT_ICONS = [
    "🦊", "🐙", "🦉", "🦄", "🐳", "🦋", "🐢", "🐝",
    "🦁", "🐧", "🦩", "🐬", "🦇", "🐡", "🦕", "🐌",
    "🦔", "🦦", "🐨", "🐼", "🦭", "🦡", "🐺", "🐸",
  ];
  function ovAgentIcon(key) {
    if (!key) return OV_AGENT_ICONS[0];
    var s = 0;
    for (var i = 0; i < key.length; i++) s += key.charCodeAt(i);
    return OV_AGENT_ICONS[s % OV_AGENT_ICONS.length];
  }
  // 채널 표시명: agent 는 "<key별 아이콘> <name>"(스윔레인/주체 배지와 동형).
  function ovAgentLabel(key) {
    var tm = ovRoster.filter(function (t) { return t.key === key; })[0];
    var nm = tm && (tm.name || [tm.profile, tm.name].filter(Boolean).join(" · "));
    return ovAgentIcon(key) + " " + (nm || key);
  }

  // 사용자 입력 = 독립된 평문 줄(그룹핑 없음).
  // 응답 = 독립된 블록(항상 done — complete 시 한 번에 append). 짝짓기·귀속 없음.
  // 활동 스트립: 실행 중 도구 호출 축약 한 줄(왼쪽=누적 카운트, 오른쪽=현재 배치 칩).
  function ovOnRoster(d) {
    ovRoster = (d && d.roster) || [];
    ovSyncChannels(); // 채널 바(상태 dot 포함) 갱신(죽으면 main 복귀)
    ovRenderAskTray(); // waiting_ask 진실원(선착순 답변→소비 시 자동 사라짐)
  }
  // 글로벌 ask 트레이: roster 에서 waiting_ask 인 agent + 그 질문(ovAskTray)을
  // 채널 무관하게 렌더. 답변은 그 agent 로 고정(api/agent/<key>/input), 서버
  // 공유 상태라 누가 먼저 답하면 waiting_ask 해제 → 모든 뷰어에서 사라짐(선착순).
  // main 의 prompt/confirm 을 트레이 항목(DOM)으로 — 검증된 조각 재사용
  // (buildPromptMetaEl·highlightDangerHtml·confirm-btn·mode-context). 3b.
  function ovBuildMainAskEl() {
    var kind = ovMainAsk.kind;
    var data = ovMainAsk.data || {};
    var item = el("div", ["ask-item", "ask-main"]);
    var head = el("div", ["ask-q"]);
    head.textContent =
      kind === "confirm" ? "⚠ main — 확인 필요" : "❓ main 이(가) 물었습니다";
    item.appendChild(head);
    var meta = buildPromptMetaEl(data, kind === "confirm");
    if (meta) item.appendChild(meta);
    if (kind === "confirm") {
      if (typeof data.command === "string" && data.command) {
        item.appendChild(
          elHtml("pre", ["action-shell", "confirm-cmd"],
            highlightDangerHtml(data.command, data.danger_spans))
        );
      }
      var cmt = el("input", ["ask-answer"]);
      cmt.type = "text";
      cmt.placeholder = "선택 코멘트(비우면 없음)";
      var btns = el("div", ["ask-confirm-btns"]);
      (data.options || []).forEach(function (opt) {
        var b = el("button", ["confirm-btn"]);
        if (opt.key === data.default_key) b.classList.add("default");
        b.textContent = opt.key + " — " + opt.label;
        b.addEventListener("click", function () {
          ovSubmitMainConfirm(opt.key, cmt.value.trim());
        });
        btns.appendChild(b);
      });
      cmt.addEventListener("keydown", function (e) {
        if (e.key === "Enter" && !e.isComposing) {
          e.preventDefault();
          ovSubmitMainConfirm(data.default_key, cmt.value.trim());
        }
      });
      item.appendChild(btns);
      var cw = el("div", ["ask-in"]);
      cw.appendChild(cmt);
      item.appendChild(cw);
    } else {
      var ctx = typeof data.context === "string" ? data.context : "";
      if (ctx) {
        var ce = el("div", ["mode-context"]);
        ce.textContent = ctx;
        item.appendChild(ce);
      }
      var ans = el("input", ["ask-answer"]);
      ans.type = "text";
      ans.placeholder = "답변… (Enter 전송)";
      var send = el("button", ["ask-send", "btn-primary"]);
      send.textContent = "전송";
      send.addEventListener("click", function () {
        ovSubmitMainPrompt(ans.value);
        ans.value = "";
      });
      ans.addEventListener("keydown", function (e) {
        if (e.key === "Enter" && !e.isComposing) {
          e.preventDefault();
          ovSubmitMainPrompt(ans.value);
          ans.value = "";
        }
      });
      var iw = el("div", ["ask-in"]);
      iw.appendChild(ans);
      iw.appendChild(send);
      item.appendChild(iw);
    }
    return item;
  }
  function ovRenderAskTray() {
    var tray = document.getElementById("ask-tray");
    if (!tray) return;
    var waiting = ovRoster.filter(function (t) { return t.state === "waiting_ask"; });
    if (!ovMainAsk && !waiting.length) {
      tray.hidden = true;
      tray.innerHTML = "";
      return;
    }
    var html = "";
    waiting.forEach(function (t) {
      var q = ovAskTray[t.key];
      var qt = q && q.text ? escapeHtml(q.text) : "(질문 대기)";
      html +=
        '<div class="ask-item" data-key="' + escapeHtml(t.key) + '">' +
        '<div class="ask-q">❓ <b>' + escapeHtml(ovAgentLabel(t.key)) +
        "</b> 이(가) 물었습니다</div>" +
        '<div class="ask-qt">' + qt + "</div>" +
        '<div class="ask-in"><input class="ask-answer" type="text" ' +
        'placeholder="답변…" aria-label="답변"><button type="button" ' +
        'class="ask-send btn-primary">전송</button></div></div>';
    });
    tray.innerHTML = html;
    // main ask 항목(DOM)은 맨 위에 — 검증된 confirm/prompt 조각 재사용.
    if (ovMainAsk) tray.insertBefore(ovBuildMainAskEl(), tray.firstChild);
    tray.hidden = false;
  }
  // 트레이 답변 전송(그 asker 로 고정 — 드롭박스 채널과 무관).
  function ovSubmitAsk(key, text) {
    if (!key || !text.trim()) return;
    fetch("api/agent/" + encodeURIComponent(key) + "/input", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ content: text, conn_id: myConnId }),
    });
  }
  (function () {
    var tray = document.getElementById("ask-tray");
    if (!tray) return;
    function submitFrom(el) {
      var item = el.closest(".ask-item");
      if (!item) return;
      var inp = item.querySelector(".ask-answer");
      ovSubmitAsk(item.getAttribute("data-key"), inp ? inp.value : "");
      if (inp) inp.value = ""; // 낙관적 클리어 — roster 갱신이 항목 제거
    }
    tray.addEventListener("click", function (e) {
      if (e.target.classList.contains("ask-send")) submitFrom(e.target);
    });
    tray.addEventListener("keydown", function (e) {
      if (e.key === "Enter" && !e.isComposing && e.target.classList.contains("ask-answer")) {
        e.preventDefault();
        submitFrom(e.target);
      }
    });
  })();
  // agent 대화 메시지(agent_msg) → 왕래 줄. 주체/대상 라벨은 **렌더 시점에
  // 해소**한다 — replay 시 agent_msg 가 roster 보다 먼저 와도 이름이 키로
  // 고정되지 않게.
  // 왕래 상대 해소 — `author` 네임스페이스가 이미 셋을 구분한다
  // (`main` / `user:<닉>` / `agent:<key>`). 반환 `key` 가 있으면 **점프 가능**.
  // main 은 일부러 비운다: main 쪽 대응 줄이 `⚡ agent` 도구 호출이라 매칭 키가
  // 달라 agent→main 점프는 이번 범위 밖이다(docs/chat-ui §5).
  function ovPeerInfo(who) {
    var w = String(who || "");
    if (!w || w === "main") return { label: "💬 main", key: "" };
    if (w.indexOf("user:") === 0) return { label: w.slice(5) + " (사람)", key: "" };
    var k = w.indexOf("agent:") === 0 ? w.slice(6) : w;
    var known = ovRoster.some(function (t) { return t.key === k; });
    return { label: ovAgentLabel(k) + " (peer)", key: known ? k : "" };
  }
  // 왕래 한 줄 — 세로로 흐르는 내부 작업과 달리 **방향과 상대**를 싣는다
  // (docs/chat-ui §4). 상대 칩이 점프 버튼을 겸한다.
  function ovRenderAgentMsg(d) {
    var out = d.direction === "out" || d.direction === "question";
    var peer = ovPeerInfo(out ? d.to : d.author);
    var text = String(d.text || "");
    var first = text.split("\n").find(function (l) { return l.trim(); }) || text;
    var tail = el("span", ["peer"], peer.label);
    if (peer.key) {
      tail.classList.add("can-jump");
      tail.title = peer.label + " 채널로 이동";
      tail.addEventListener("click", function (e) {
        e.stopPropagation(); // 줄 펼침 토글과 분리
        ovJump(peer.key, d.key);
      });
    }
    var card = el("div", ["card", "card-msg"]);
    card.dataset.ch = d.key;
    if (peer.key) card.dataset.peer = peer.key;
    // 바로 위 최종답과 같은 답이면 **영수증 한 줄**로만 (사용자 지적: 같은
    // 내용이 화면에 두 번). 같지 않으면(= resume 재생이라 최종답이 없다)
    // 이 줄이 유일한 기록이므로 내용을 그대로 싣는다.
    var dup =
      d.direction === "out" && lastFinalByChannel[d.key] === text.trim();
    var body =
      !dup && text !== first.trim()
        ? elHtml("div", ["md"], escapeAndFormat(text))
        : null;
    card.appendChild(
      makeRow(
        d.direction === "question" ? "❓" : out ? "→" : "←",
        d.direction === "question" ? "질문" : out ? "보냄" : "받음",
        dup ? "회신했습니다" : first.trim(),
        body,
        dup ? ["msg", "receipt"] : ["msg"],
        tail
      )
    );
    stampCard(card, d.ts);
    $messages.appendChild(card);
    applyChannelFilter(card);
    scheduleScroll();
  }

  function ovOnAgentMsg(d) {
    if (!d || !d.key) return;
    ovRenderAgentMsg(d);
    if (d.direction === "question") {
      ovAskTray[d.key] = { text: d.text || "", ts: d.ts }; // 글로벌 트레이용
      ovRenderAskTray();
    }
  }
  // kill(agent_cleared) → 그 채널 대화·트레이 정리 (resume 재생 중복 방지).
  // 왕래 줄은 이제 타임라인의 카드라, 비우는 것도 DOM 에서 한다.
  function ovOnAgentCleared(key) {
    $messages
      .querySelectorAll(':scope > .card-msg[data-ch="' + cssEsc(key) + '"]')
      .forEach(function (c) { c.remove(); });
    delete ovAskTray[key];
    ovRenderAskTray();
    ovSyncChannels();
  }
  // 채널 바(칩) 렌더 — main + roster 전 agent(dead 포함). 칩마다 **상태 dot**
  // (idle/busy/waiting/dead) + ❓(waiting_ask) + ✕(kill)/↻(resume). 에이전트 상태를
  // 이 "agent 탭"에 직접 표시(v8.33.0 — 구 흐름-탭 dots 통합).
  function ovSyncChannels() {
    var bar = document.getElementById("ov-channels");
    if (!bar) return;
    // 선택 채널이 roster 에서 완전히 사라지면(dead 도 아님) main 복귀. dead 는
    // 유지(⑤: 사망 고지 + ↻ 되살리기 가능).
    if (ovActiveChannel !== "main" &&
        !ovRoster.some(function (t) { return t.key === ovActiveChannel; })) {
      ovActiveChannel = "main";
      applyChannelFilter(); // 사라진 채널을 보고 있었다면 표시도 되돌린다
    }
    // 돌아갈 채널이 사라졌으면 버튼도 사라진다(눌러도 갈 곳이 없다).
    if (ovBack && ovBack.ch !== "main" &&
        !ovRoster.some(function (t) { return t.key === ovBack.ch; })) {
      ovBack = null;
    }
    ovRenderBack(); // 라벨은 roster 가 와야 이름으로 해소된다
    var html = ovChanChip("main", "💬 main", "", ovActiveChannel === "main");
    ovRoster.forEach(function (t) {
      html += ovChanChip(
        t.key, ovAgentLabel(t.key), t.state, ovActiveChannel === t.key
      );
    });
    bar.innerHTML = html;
    ovApplyChannelInput();
  }
  function ovChanChip(key, label, state, active) {
    var dead = state === "dead";
    // main 은 상태 dot 없음(항상 LLM). agent 는 상태색 dot.
    var dot = "";
    if (key !== "main") {
      var c = state === "busy" ? "b" : state === "waiting_ask" ? "w" : dead ? "x" : "i";
      dot = '<span class="ov-dot ' + c + '"></span>';
    }
    var badges = state === "waiting_ask" ? '<span class="ov-ch-q">❓</span>' : "";
    var ctrl = "";
    if (key !== "main") {
      ctrl = dead
        ? '<button type="button" class="ov-ch-ctl" data-act="resume" title="되살리기">↻</button>'
        : '<button type="button" class="ov-ch-ctl" data-act="kill" title="종료">✕</button>';
    }
    return (
      '<span class="ov-ch' + (active ? " on" : "") + (dead ? " dead" : "") +
      '" data-key="' + escapeHtml(key) + '" role="tab" aria-selected="' +
      (active ? "true" : "false") + '">' + dot + '<span class="ov-ch-lb">' +
      escapeHtml(label) + "</span>" + badges + ctrl + "</span>"
    );
  }
  function ovActiveDead() {
    if (ovActiveChannel === "main") return false;
    var t = ovRoster.filter(function (x) { return x.key === ovActiveChannel; })[0];
    return !!(t && t.state === "dead");
  }
  // 입력창 placeholder/disabled 를 현재 채널(+busy·dead)에 맞게. 입력창은 chat 전용.
  function ovApplyChannelInput() {
    if (!$input) return;
    var dead = ovActiveDead();
    $input.disabled = dead;
    if ($send) $send.disabled = dead;
    if (dead) {
      $input.placeholder = "✕ 이 에이전트는 종료됨 — 다른 대상 선택 또는 ↻ 로 되살리기";
    } else if (ovActiveChannel !== "main") {
      $input.placeholder = ovAgentLabel(ovActiveChannel) + " 에게… (Enter 전송)";
    } else {
      $input.placeholder = workerBusy
        ? "Worker is processing… your message will be queued (injected next turn)"
        : "Type a message — Enter to send, Shift+Enter for newline";
    }
  }
  // ── 채널 = 타임라인 필터 (v9.4.0 ⑥) ──────────────────────
  // `#messages` 의 **직계 자식**만 걸러 보여준다. `data-ch` 가 없는 노드는
  // 어느 채널에서나 보인다(생성 중 한 줄 — 채널과 무관한 표시).
  // 인자를 주면 그 노드 하나만(새로 append 된 카드), 없으면 전부 훑는다.
  function applyChannelFilter(one) {
    var nodes = one ? [one] : Array.prototype.slice.call($messages.children);
    nodes.forEach(function (n) {
      var ch = n.dataset ? n.dataset.ch : null;
      if (ch) n.hidden = ch !== ovActiveChannel;
    });
  }

  // 채널 전환. 표시(필터) · 입력 라우팅 · 칩 활성이 한 지점에서 움직인다.
  //
  // **전환하면 그 대화의 맨 아래로 간다.** 필터가 노드를 감추고 드러내면
  // 문서 높이가 확 바뀌는데, 스크롤 위치는 그대로라 아무 데나 떨어져 있었다
  // — 칩을 누를 때마다 "어딘지 모를 곳"으로 튀어 규칙을 읽을 수 없었다
  // (사용자 보고). 대화를 열면 최신부터 보는 게 채팅의 규칙이고, 자동 따라가기도
  // 함께 되살려 그 채널에 새 줄이 오면 이어서 따라간다.
  // (점프는 이 뒤에 앵커로 다시 스크롤하므로 영향받지 않는다 — ovJump 참조.)
  function ovSetChannel(key) {
    ovActiveChannel = key || "main";
    ovSyncChannels();
    applyChannelFilter();
    ovApplyChannelInput();
    autoScrollEnabled = true;
    scrollToBottom();
  }

  // ── 점프 — 엿보기, 네비게이션이 아니다 (docs/chat-ui §5) ────
  // 상태는 값 하나: 직전에 어디서 왔는지. **스택이 아니다** — 새 점프가 이전
  // 것을 덮고, 한 번 누르면 사라진다. 스택이면 누를 때마다 라벨이 바뀌어
  // 어디로 갈지 예측이 안 되는데, 채널 칩이 항상 보이므로 그 복잡도를 살
  // 이유가 없다(사용자 지적으로 스택→단일 변경).
  var ovBack = null; // {ch, id} | null
  function ovRenderBack() {
    var btn = document.getElementById("ch-back");
    if (!btn) return;
    if (!ovBack) {
      btn.hidden = true;
      return;
    }
    btn.hidden = false;
    btn.textContent =
      "↩ " + (ovBack.ch === "main" ? "💬 main" : ovAgentLabel(ovBack.ch)) + " 으로";
  }
  /** 채널의 앵커 카드로 이동 + 1.6s 하이라이트. ``peer`` 가 주어지면 그 상대와
   * 주고받은 **가장 최근 왕래 줄**을 앵커로 삼는다(정확히 그 대화를 가리킴).
   * 없으면 그 채널의 마지막 카드 — 도착은 했음을 보이는 게 아무것도 안 하는
   * 것보다 낫다. */
  function ovChannelAnchor(key, peer) {
    var cards = $messages.querySelectorAll(
      ':scope > [data-ch="' + cssEsc(key) + '"]'
    );
    if (peer) {
      var hit = null;
      cards.forEach(function (c) {
        if (c.dataset.peer === peer) hit = c;
      });
      if (hit) return hit;
    }
    return cards.length ? cards[cards.length - 1] : null;
  }
  function cssEsc(s) {
    return String(s).replace(/["\\]/g, "\\$&");
  }
  function ovJump(key, peer) {
    if (!key || key === ovActiveChannel) return;
    ovBack = { ch: ovActiveChannel };
    ovSetChannel(key);
    ovRenderBack();
    var anchor = ovChannelAnchor(key, peer);
    if (anchor) {
      expandAncestors(anchor.dataset.taskId || "");
      scrollTimelineTo(anchor);
    }
  }
  (function () {
    var btn = document.getElementById("ch-back");
    if (!btn) return;
    btn.addEventListener("click", function () {
      if (!ovBack) return;
      var to = ovBack.ch;
      ovBack = null; // 한 단계 — 돌아가면 사라진다
      ovSetChannel(to);
      ovRenderBack();
    });
  })();
  // 🔍 인스펙터가 열 스코프 = 현재 대화 채널. main → main 스코프, agent → 그
  // agent 의 프롬프트 스냅샷(task_id=agent key). 인스펙터 IIFE 가 읽는다.
  window.__inspectorScope = function () {
    if (ovActiveChannel === "main") return { scope: "", name: "Main", kind: "main" };
    return { scope: ovActiveChannel, name: ovAgentLabel(ovActiveChannel), kind: "agent" };
  };
  (function () {
    var bar = document.getElementById("ov-channels");
    if (!bar) return;
    bar.addEventListener("click", function (e) {
      var chip = e.target.closest ? e.target.closest(".ov-ch") : null;
      if (!chip) return;
      var key = chip.getAttribute("data-key");
      var ctl = e.target.closest(".ov-ch-ctl");
      if (ctl) {
        e.stopPropagation();
        fetch("api/agent/" + encodeURIComponent(key) + "/" +
          ctl.getAttribute("data-act"), { method: "POST" });
        return;
      }
      ovSetChannel(key);
    });
    ovSyncChannels(); // 로드 시 최소 main 칩 렌더(roster 오면 갱신)
  })();

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
    // 3b: main 의 prompt/confirm 을 글로벌 ask 트레이 항목으로(입력창은 chat 유지).
    clearConfirmStall();
    ovMainAsk = { kind: d.kind, data: d };
    ovRenderAskTray();
    updateSendEnabled(); // ovMainAsk 반영 → #chat-stop 숨김(두 Stop 방지)
    // Allow aborting a stuck prompt / confirm wait. Worker side
    // surfaces this as EOFError → ``(no response)`` (ask) or
    // ``(default_key, "")`` (confirm).
    setAbortVisible(true);
  });

  es.addEventListener("input_resolved", function () {
    ovFoldMainAsk();
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
      const kind =
        s.kind === "dynamic" ? "dynamic" : s.kind === "tail" ? "tail" : "system";
      if (kind !== lastKind) {
        html +=
          '<div class="insp-group">' +
          (kind === "dynamic"
            ? "Conversation · observations"
            : kind === "tail"
              ? "Per-turn tail — appended to the LAST message every turn (not system prompt)"
              : "System prompt") +
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
      // 현재 대화 채널의 프롬프트/컨텍스트를 연다(main 또는 활성 agent).
      const s =
        (window.__inspectorScope && window.__inspectorScope()) ||
        { scope: "", name: "Main", kind: "main" };
      window.__openInspector(s.scope, s.name, s.kind);
    });
  document.getElementById("insp-close").addEventListener("click", close);
  $backdrop.addEventListener("click", close);
  document.addEventListener("keydown", function (e) {
    if (e.key === "Escape" && $drawer.classList.contains("open")) close();
  });
  $search.addEventListener("input", applyFilter);

  // Live refresh of the currently-open scope. The "prompt-changed" relay is
  // gone with the server event that fed it (v8.46.0 — the agent roster left the
  // system prompt for the per-turn tail block, so there is nothing to patch
  // between turns); memory still notifies, and the reload picks the new note up
  // from the dynamic half.
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
      // v9.4.0 ③: obs-head 가 행(.row)으로 바뀌었다 — 라벨은 도구 이름
      // 칸(.k)에서 뽑는다(종전 "✓ shell" 대신 "shell").
      const kind = card.querySelector(".row .k");
      return {
        kind: "observation",
        label: kind ? kind.innerText.trim() : "Observation",
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
  var $badge = document.getElementById("compaction-badge");
  function setLabel(pct) {
    $label.textContent = "Compact " + pct + "%";
    if ($badge) $badge.textContent = pct + "%";
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

// ── 스트림 무진전(Stall) 한도 (P3, v8.55.0) ────────────────────────────
// ctx 팝오버에서 "마지막 토큰 이후 N분 무진전 시 재접속·재전송" 한도를 세션
// 한정 변경. keep-alive 는 진전이 아님. 0 = 감지 끔. compaction IIFE 동형.
(function () {
  "use strict";
  const $wrap = document.getElementById("stall-wrap");
  const $input = document.getElementById("stall-input");
  if (!$wrap || !$input) return;

  const toMin = (s) => (s <= 0 ? 0 : Math.round(s / 60));
  const $badge = document.getElementById("stall-badge");
  const $att = document.getElementById("stall-attempts");
  const $derived = document.getElementById("stall-derived");

  // 배지가 "10m×4" 인 이유: 곱(40m)만 보이면 어느 축을 고칠지 알 수 없고,
  // 한 축만 보이면 최대 대기를 머릿속에서 계산해야 한다. 곱은 파생 줄이 진다.
  function apply(seconds, attempts) {
    const m = toMin(seconds);
    const n =
      typeof attempts === "number"
        ? attempts
        : $att
          ? Math.max(1, Number($att.value) || 1)
          : 1;
    // 편집 중인 입력은 덮어쓰지 않는다. 한 축을 바꾸면 응답이 **두 축 모두**
    // 실어 오므로, 그대로 쓰면 다른 칸에 타이핑하던 값이 서버 값으로 되돌아가
    // 사용자가 방금 친 숫자를 잃는다(두 입력이 한 팝업에 생기며 드러난 경합).
    if (document.activeElement !== $input) $input.value = m;
    if ($att && document.activeElement !== $att) $att.value = n;
    if ($badge) $badge.textContent = m === 0 ? "off" : m + "m×" + n;
    // 한도 0 = 감지 끔 → 시도 횟수라는 개념 자체가 성립하지 않는다.
    if ($att) $att.disabled = m === 0;
    if ($derived) {
      $derived.classList.toggle("is-off", m === 0);
      $derived.textContent =
        m === 0
          ? "무진전 감지가 꺼져 있어 재전송하지 않습니다"
          : m + "분 × " + n + "회 = 최대 " + m * n + "분 후 실패";
    }
  }

  fetch("api/stream-idle")
    .then((r) => (r.ok ? r.json() : null))
    .then((d) => {
      if (!d) return;
      if (typeof d.seconds === "number") apply(d.seconds, d.attempts);
      $wrap.hidden = false;
    })
    .catch(() => {});

  function push(payload) {
    fetch("api/stream-idle", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    })
      .then((r) => (r.ok ? r.json() : null))
      .then((d) => {
        if (d && typeof d.seconds === "number") apply(d.seconds, d.attempts);
      })
      .catch(() => {});
  }

  $input.addEventListener("change", () => {
    const minutes = Math.max(0, Math.round(Number($input.value) || 0));
    push({ seconds: minutes * 60 });
  });
  if ($att) {
    $att.addEventListener("change", () => {
      push({ attempts: Math.max(1, Math.round(Number($att.value) || 1)) });
    });
  }

  document.addEventListener("agentcli:streamidle", (e) => {
    const d = e.detail || {};
    if (typeof d.seconds === "number") apply(d.seconds, d.attempts);
  });
})();

// ── 🔌 MCP 상태 칩 (v9.2.0, docs/mcp-ui §D) ──────────────────────
// 읽기 전용. 부팅 시 붙인 MCP 서버의 연결/전체·도구 수·실패 이유 — 지금까지
// 이 정보는 stderr 에만 있어 웹 사용자는 볼 방법이 아예 없었다. 등록·수정은
// `agent-cli mcp` 로만(§E: 등록은 곧 로컬 프로세스 실행). 부팅 시점 상태라
// 세션 중 바뀌지 않으므로 sticky/SSE 없이 로드 시 GET 한 번. 등록 서버가
// 없으면 칩을 숨긴다 — `0/0` 은 정보가 아니라 소음.
(function () {
  "use strict";
  const $wrap = document.getElementById("mcp-wrap");
  const $badge = document.getElementById("mcp-badge");
  const $tools = document.getElementById("mcp-tools");
  const $list = document.getElementById("mcp-list");
  if (!$wrap || !$list) return;

  // 메인 IIFE 의 el() 은 이 스코프에 없다 — 같은 시그니처의 지역 헬퍼
  function el(tag, classes, text) {
    const e = document.createElement(tag);
    if (classes && classes.length) e.classList.add.apply(e.classList, classes);
    if (text !== undefined && text !== null) e.textContent = text;
    return e;
  }

  function render(d) {
    if (!d || !d.total) {
      $wrap.hidden = true;
      return;
    }
    // 배지는 붙은/전체 — 하나라도 실패하면 숫자만 보고 안다
    $badge.textContent = d.connected + "/" + d.total;
    $wrap.classList.toggle("has-fail", d.connected < d.total);
    $tools.textContent = d.tool_count + " tools";
    $list.textContent = "";
    d.servers.forEach(function (s) {
      const row = el("div", ["mcp-srv", s.connected ? "ok" : "fail"]);
      row.appendChild(el("span", ["dot"]));
      row.appendChild(el("span", ["nm"], s.name));
      row.appendChild(el("span", ["tr"], s.transport));
      let detail;
      if (s.connected) {
        const names = s.tools.slice(0, 4).join(", ") + (s.tools.length > 4 ? ", …" : "");
        detail = s.tools.length + " tools" + (names ? " · " + names : "");
      } else {
        detail = s.error || "연결 실패";
      }
      row.appendChild(el("span", ["detail"], detail));
      $list.appendChild(row);
    });
    $wrap.hidden = false;
  }

  fetch("api/mcp")
    .then((r) => (r.ok ? r.json() : null))
    .then(render)
    .catch(() => {});
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
  var $badge = document.getElementById("maxagents-badge");
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
    if ($badge) $badge.textContent = value === 0 ? "∞" : String(value);
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

// ── ⚡ 자동 승인 헤더 토글 (confirm-mode, 별도 IIFE) ───────────────────
// 헤더 버튼으로 노출(팝오버 밖). 켜면 위험 명령/경로 확인 프롬프트를 건너뛰고 자동
// 허용(세션 한정·런타임). GET 초기화, 클릭 토글→POST, sticky(confirm_mode) 동기화.
(function () {
  "use strict";
  const $btn = document.getElementById("confirm-mode-btn");
  if (!$btn) return;
  let on = false;

  function apply(v) {
    on = !!v;
    $btn.setAttribute("aria-pressed", on ? "true" : "false");
    $btn.classList.toggle("on", on); // 활성 시 amber 경고색
  }

  fetch("api/confirm-mode")
    .then((r) => (r.ok ? r.json() : null))
    .then((d) => {
      if (!d) return;
      apply(!!d.auto_approve);
      $btn.hidden = false;
    })
    .catch(() => {});

  $btn.addEventListener("click", () => {
    apply(!on);
    fetch("api/confirm-mode", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ auto_approve: on }),
    })
      .then((r) => (r.ok ? r.json() : null))
      .then((d) => {
        if (d && typeof d.auto_approve === "boolean") apply(d.auto_approve);
      })
      .catch(() => {});
  });

  document.addEventListener("agentcli:confirmmode", (e) => {
    const d = e.detail || {};
    if (typeof d.auto_approve === "boolean") apply(d.auto_approve);
  });
})();

// ── 🧠 사고/추론 노력 컨트롤 (thinking, 별도 IIFE) ─────────────────────
// ctx 팝오버의 두 셀렉트(사고 on/off·reasoning effort)를 /api/thinking 으로 저장 →
// 다음 LLM 요청부터 반영(공유 ctx). sticky(thinking_mode) 로 뷰어 동기화.
(function () {
  "use strict";
  const $wrap = document.getElementById("thinking-wrap");
  const $en = document.getElementById("think-enable");
  const $eff = document.getElementById("think-effort");
  if (!$wrap || !$en || !$eff) return;

  // 서버 상태(null|bool / null|str) → 셀렉트 값(auto/on/off, auto/low/…).
  const $badge = document.getElementById("thinking-badge");
  function apply(d) {
    const et = d ? d.enable_thinking : undefined;
    $en.value = et === true ? "on" : et === false ? "off" : "auto";
    const eff = (d && d.reasoning_effort) || "auto";
    $eff.value = ["low", "medium", "high"].includes(eff) ? eff : "auto";
    // 배지: off → "off", effort 지정 → 그 값, 그 외 → "auto"
    if ($badge) {
      $badge.textContent =
        $en.value === "off" ? "off" : $eff.value !== "auto" ? $eff.value : "auto";
    }
  }

  // 모델이 사고를 지원 안 하면(supports_thinking=false) provider 가 사고
  // 파라미터를 아예 안 넣으므로 컨트롤을 잠근다. null/undefined(unknown)면 잠그지 않음.
  function setSupported(supported) {
    const locked = supported === false;
    $en.disabled = locked;
    $eff.disabled = locked;
    $wrap.classList.toggle("is-disabled", locked);
    if (locked) {
      $wrap.title = "이 모델은 사고(thinking)를 지원하지 않습니다 — 사고/노력 조정 불가.";
    }
  }

  function post() {
    if ($en.disabled) return; // 미지원 모델 — 잠긴 상태에선 저장 안 함
    const et = $en.value === "on" ? true : $en.value === "off" ? false : null;
    const eff = $eff.value === "auto" ? "auto" : $eff.value;
    fetch("api/thinking", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ enable_thinking: et, reasoning_effort: eff }),
    })
      .then((r) => (r.ok ? r.json() : null))
      .then((d) => {
        if (d && d.ok) apply(d);
      })
      .catch(() => {});
  }

  fetch("api/thinking")
    .then((r) => (r.ok ? r.json() : null))
    .then((d) => {
      apply(d);
      setSupported(d ? d.supports_thinking : null);
      $wrap.hidden = false;
    })
    .catch(() => {});

  $en.addEventListener("change", post);
  $eff.addEventListener("change", post);
  document.addEventListener("agentcli:thinkingmode", (e) => apply(e.detail || {}));
})();

// ── 노브 칩 팝업 토글 (v8.57.0) ─────────────────────────────────
// 각 노브 칩(🗜️/👥/⏳/🧠·보따리)이 data-pop 으로 자기 팝업을 가리킨다.
// 한 번에 하나만 열리고, 바깥 클릭·Escape 로 닫힘. 팝업 내부 조작(슬라이더·
// 셀렉트)은 닫힘을 유발하지 않는다. 구 ctx-popover 단일 토글의 계승.
(function () {
  function chips() {
    return Array.prototype.slice.call(document.querySelectorAll(".knob-btn"));
  }
  function popOf(btn) {
    return document.getElementById(btn.dataset.pop || "");
  }
  function closeAll(except) {
    chips().forEach(function (b) {
      var pop = popOf(b);
      if (pop && b !== except) { pop.hidden = true; b.setAttribute("aria-expanded", "false"); }
    });
  }
  document.addEventListener("click", function (e) {
    var btn = e.target.closest && e.target.closest(".knob-btn");
    if (btn) {
      e.stopPropagation();
      var pop = popOf(btn);
      if (!pop) return;
      var willOpen = pop.hidden;
      closeAll(willOpen ? btn : null);
      pop.hidden = !willOpen;
      btn.setAttribute("aria-expanded", String(willOpen));
      if (willOpen) {
        // 기본(오른쪽 펼침)으로 열고, 뷰포트 오른쪽을 넘으면 왼쪽 펼침으로 뒤집는다.
        pop.classList.remove("align-right");
        var r = pop.getBoundingClientRect();
        if (r.right > window.innerWidth - 4) pop.classList.add("align-right");
      }
      return;
    }
    if (e.target.closest && e.target.closest(".knob-pop")) return; // 내부 조작
    closeAll(null);
  });
  document.addEventListener("keydown", function (e) {
    if (e.key === "Escape") closeAll(null);
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
