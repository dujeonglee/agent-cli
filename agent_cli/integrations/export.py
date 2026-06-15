"""Conversation export rendering — selected transcript entries → HTML / ADF / wiki.

The web UI's Export feature sends a list of selected transcript entries to the
server, which renders them into a self-contained HTML document (download), an
Atlassian Document Format (ADF) doc (Jira **Cloud** comment body), or a
wiki-markup string (Jira **Server/DC** comment body). All renderers are pure
functions of the entry list so they unit-test without a browser or a live Jira.

Entry contract (one per selected transcript card, built by the frontend):

    {
        "kind":  str,   # "user" | "assistant" | "observation" | "error" | ...
        "label": str,   # short header, e.g. "User", "read_file", "Observation"
        "body":  str,   # the textual content
        "mono":  bool,  # monospace body (tool I/O, observation, shell) → <pre> / codeBlock
    }
"""

from __future__ import annotations

import html
from typing import Any

# Entry kinds whose body is monospace by default even if the frontend omits the
# flag (defensive — the flag is authoritative when present).
_DEFAULT_MONO_KINDS = {"observation"}


def _entry_fields(entry: dict[str, Any]) -> tuple[str, str, str, bool]:
    """Normalize one entry to ``(kind, label, body, mono)`` with safe defaults."""
    kind = str(entry.get("kind") or "entry")
    label = str(entry.get("label") or kind.capitalize())
    body = entry.get("body")
    body = "" if body is None else str(body)
    mono = entry.get("mono")
    mono = (kind in _DEFAULT_MONO_KINDS) if mono is None else bool(mono)
    return kind, label, body, mono


# ── HTML ─────────────────────────────────────────────────────────────────────

_HTML_STYLE = """\
  :root { color-scheme: light dark; }
  body { font: 14px/1.5 -apple-system, system-ui, sans-serif; max-width: 860px;
         margin: 2rem auto; padding: 0 1rem; }
  h1 { font-size: 1.3rem; border-bottom: 1px solid #8884; padding-bottom: .4rem; }
  .exp-meta { color: #8889; font-size: .85rem; margin-bottom: 1.5rem; }
  .exp-entry { margin: 0 0 1.1rem; }
  .exp-label { font-weight: 600; font-size: .8rem; text-transform: uppercase;
               letter-spacing: .03em; color: #6679; margin-bottom: .25rem; }
  .exp-user .exp-label { color: #2a7; }
  .exp-assistant .exp-label { color: #58f; }
  .exp-observation .exp-label, .exp-error .exp-label { color: #c74; }
  .exp-body { white-space: pre-wrap; word-break: break-word; margin: 0; }
  pre.exp-body { background: #8881; padding: .6rem .8rem; border-radius: 6px;
                 overflow-x: auto; font: 12px/1.45 ui-monospace, monospace; }
"""


def entries_to_html(entries: list[dict[str, Any]], *, title: str = "") -> str:
    """Render *entries* into a standalone, self-contained HTML document string.

    No external assets — styles are inlined so the file opens anywhere. Bodies
    are escaped and ``white-space: pre-wrap`` preserves their layout; monospace
    bodies (tool I/O / observations) render in a ``<pre>`` block.
    """
    heading = html.escape(title) if title else "agent-cli conversation export"
    parts: list[str] = [
        "<!doctype html>",
        '<html lang="en"><head><meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        f"<title>{heading}</title>",
        f"<style>\n{_HTML_STYLE}</style>",
        "</head><body>",
        f"<h1>{heading}</h1>",
        f'<div class="exp-meta">{len(entries)} entries</div>',
    ]
    for entry in entries:
        kind, label, body, mono = _entry_fields(entry)
        kind_cls = "".join(c if c.isalnum() else "-" for c in kind.lower())
        body_el = (
            f'<pre class="exp-body">{html.escape(body)}</pre>'
            if mono
            else f'<p class="exp-body">{html.escape(body)}</p>'
        )
        parts.append(
            f'<section class="exp-entry exp-{kind_cls}">'
            f'<div class="exp-label">{html.escape(label)}</div>'
            f"{body_el}</section>"
        )
    parts.append("</body></html>")
    return "\n".join(parts)


# ── ADF (Atlassian Document Format — Jira Cloud comment body) ─────────────────


def _adf_text_paragraph(text: str, *, strong: bool = False) -> dict[str, Any]:
    node: dict[str, Any] = {"type": "text", "text": text}
    if strong:
        node["marks"] = [{"type": "strong"}]
    return {"type": "paragraph", "content": [node]}


def _adf_code_block(text: str) -> dict[str, Any]:
    return {"type": "codeBlock", "content": [{"type": "text", "text": text}]}


def entries_to_adf(entries: list[dict[str, Any]]) -> dict[str, Any]:
    """Render *entries* into an ADF document for a Jira **Cloud** comment body.

    Each entry becomes a bold-label paragraph followed by its body — a
    ``codeBlock`` for monospace bodies (tool I/O / observation), a plain
    ``paragraph`` otherwise. ADF text nodes reject empty strings, so empty
    bodies are skipped (label only) and an entry-less export still yields a
    valid (placeholder) doc.
    """
    content: list[dict[str, Any]] = []
    for entry in entries:
        _kind, label, body, mono = _entry_fields(entry)
        content.append(_adf_text_paragraph(label, strong=True))
        if body:
            content.append(_adf_code_block(body) if mono else _adf_text_paragraph(body))
    if not content:
        content.append(_adf_text_paragraph("(no content)"))
    return {"type": "doc", "version": 1, "content": content}


# ── Jira wiki markup (Server / Data Center) ───────────────────────────────────


def entries_to_wiki(entries: list[dict[str, Any]]) -> str:
    """Render *entries* into a Jira **wiki-markup** string for a Server/DC
    comment body (``/rest/api/2`` takes a string, not ADF).

    Mirrors :func:`entries_to_adf`: a bold ``*label*`` line, then the body — a
    ``{code}`` block for monospace bodies (literal, no markup interpretation), a
    plain line otherwise. Empty bodies are skipped (label only); an entry-less
    export yields a placeholder so the comment is never empty.
    """
    parts: list[str] = []
    for entry in entries:
        _kind, label, body, mono = _entry_fields(entry)
        parts.append(f"*{label}*")
        if body:
            parts.append(f"{{code}}\n{body}\n{{code}}" if mono else body)
    if not parts:
        return "(no content)"
    return "\n\n".join(parts)
