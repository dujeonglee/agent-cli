// markdown.js — HTML 이스케이프 + 최소 마크다운 파이프라인 + diff 색칠 + 이미지 주소(imageUrl).
// 순수 모듈: import 가 없고 top-level 에서 DOM·window 를 건드리지 않는다 —
// node 테스트(tests/test_app_markdown.py)가 이 파일을 그대로 import 한다.
// (v10.117.0, docs/web-modules/DESIGN.md §8 — 옛 main.js 의 절 [4])

// ── HTML escaping + minimal markdown ───────
export function escapeHtml(s) {
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
export function colorizeDiffBody(escaped) {
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
export function extractCodeFences(s) {
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
export function renderHeadings(s) {
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
 * fire on the middle ``*`` of ``***``.
 *
 * CommonMark flanking (v9.22.3): a delimiter followed by whitespace
 * cannot open, one preceded by whitespace cannot close. Without it a
 * cron line ``0 9 * * 1-5`` or ``2 * 3 * 4`` lost its stars to an
 * italic space — in user bubbles and assistant text alike. */
function renderEmphasis(s) {
  let html = s.replace(/\*\*(?!\s)([^*\n]+?)(?<!\s)\*\*/g, "<strong>$1</strong>");
  html = html.replace(
    /(^|[^*])\*(?!\s)([^*\n]+?)(?<!\s)\*(?!\*)/g,
    "$1<em>$2</em>"
  );
  return html;
}

/** Pipeline orchestrator — runs block-level transforms (table,
 * headings, lists) before inline ones (emphasis, inline code) so
 * inline regexes never see header / list markers. */
function unescapeHtml(s) {
  return s.replace(/&(amp|lt|gt|quot|#39);/g, function (_m, e) {
    return { amp: "&", lt: "<", gt: ">", quot: '"', "#39": "'" }[e];
  });
}

/** 그림 ``![alt](src)`` (v10.64.0). ``<img>`` 는 그려지는 순간 브라우저가 스스로
 * 요청을 내므로 출처를 좁힌다: 작업 폴더 경로(`api/workspace/file` — 서버가
 * 워크스페이스 밖·이미지 아닌 파일을 거절한다)와 래스터 ``data:`` URI 만 그림으로.
 * 외부 http(s) 는 그리지 않고 링크로 둔다 — 인젝션된 페이지가 모델에게
 * ``![](https://x/?d=<비밀>)`` 을 쓰게 하면 보는 것만으로 새어 나간다(클릭은
 * 사람의 의도). SVG ``data:`` 는 스크립트를 품을 수 있어 뺀다. 그 밖의 스킴
 * (``javascript:`` 등)은 글 그대로. 인라인 코드 안은 건드리지 않는다.
 * 입력은 이미 이스케이프된 글이라 alt·src 를 그대로 속성에 넣어도 따옴표를
 * 깨지 못한다. 강조보다 먼저 — alt 에 ``<strong>`` 이 끼지 않게. */
const MD_IMAGE = /!\[([^\]\n]*)\]\(([^()\s]+)\)/g;
const MD_DATA_IMAGE = /^data:image\/(?:png|jpeg|gif|webp);base64,[A-Za-z0-9+/=]+$/;
function renderImages(html) {
  // 참조(sha256)가 없는 그림 — 그린 시각이 판이다(같은 카드의 그림들은 한 판).
  const drawn = Date.now().toString(36);
  return html
    .split(/(`[^`\n]+`)/)
    .map(function (part, i) {
      if (i % 2) return part; // 인라인 코드
      return part.replace(MD_IMAGE, function (all, alt, src) {
        if (MD_DATA_IMAGE.test(src)) {
          return '<img class="md-img" src="' + src + '" alt="' + alt + '">';
        }
        if (/^https?:\/\//i.test(src)) {
          return (
            '<a class="md-img-link" href="' + src + '" target="_blank" rel="noopener noreferrer" ' +
            'title="External image — not loaded; click to open">🖼 ' + (alt || src) + "</a>"
          );
        }
        if (/^[a-z][a-z0-9+.-]*:/i.test(src) || src.indexOf("//") === 0) return all;
        const url = escapeHtml(imageUrl(unescapeHtml(src), "", drawn));
        return (
          '<a class="md-img-wrap" href="' + url + '" target="_blank" rel="noopener">' +
          '<img class="md-img" src="' + url + '" alt="' + alt + '" loading="lazy"></a>'
        );
      });
    })
    .join("");
}

function markdownInline(s) {
  let html = renderTables(s);
  html = renderHeadings(html);
  html = renderLists(html);
  html = renderImages(html);
  html = renderEmphasis(html);
  html = html.replace(/`([^`\n]+)`/g, "<code>$1</code>");
  return html;
}

/** Apply a tiny subset of markdown — fenced code blocks, headings
 * (h1-h3), GFM tables, lists, images (workspace paths and raster
 * ``data:`` only — see renderImages), bold/italic, and inline code.
 * Everything else stays as escaped text. No external library;
 * variants beyond this set are intentionally out of scope
 * (NFR-MD-1: zero new JS deps).
 *
 * Order is load-bearing for XSS safety (NFR-MD-2): escapeHtml runs
 * first so every ``<`` becomes ``&lt;``, then fences are extracted
 * to placeholders (so markdown passes don't fire inside code), then
 * block + inline transforms run on the stripped body, and finally
 * fences are restored as pre-rendered ``<pre><code>`` blocks. */
export function escapeAndFormat(s) {
  const escaped = escapeHtml(s);
  const { stripped, blocks } = extractCodeFences(escaped);
  const transformed = markdownInline(stripped);
  return restoreCodeFences(transformed, blocks);
}

// ``ver`` (v10.100.1): 한 문서 안에서 브라우저는 **같은 URL** 의 이미지를
// 메모리에서 다시 쓴다 — ``no-cache`` 와 무관하다(HTML 의 available images
// 목록). 경로만 실으면 덮어쓴 스크린샷의 새 카드가 옛 그림을 그린다.
// 참조가 있으면 ``v=<sha256>`` — 서버가 그 내용의 보관본을 준다
// (v10.101.0: 옛 카드는 새로고침 뒤에도 그때의 그림). 참조가 없는 마크다운
// 그림은 ``t=<그린 시각>`` 으로 주소만 가른다 — 서버는 ``t`` 를 보지 않고
// 지금 파일을 준다.
export function imageUrl(path, sha, drawn) {
  let url = "api/workspace/file?path=" + encodeURIComponent(path);
  if (sha) url += "&v=" + sha;
  else if (drawn) url += "&t=" + drawn;
  return url;
}
