"""``browser`` tool — a headless browser the model drives by ref (docs/browser/DESIGN.md).

The engine lives in ``agent_cli/browser.py`` (one session per process, owned
by a dedicated thread). This module is the tool surface only: schema, per-mode
validation, dispatch, and the observation wording.

Advertised only when ``playwright`` is importable and the knob is on
(``requires_handler="browser"`` → ``runtime.browser_resource``), the same
seam as ``snapshot``: a tool that cannot work is not offered.
"""

from __future__ import annotations

from typing import ClassVar

from agent_cli.tools.base import Tool
from agent_cli.tools.result import ToolResult

MODES = (
    "open",
    "snapshot",
    "find",
    "text",
    "click",
    "type",
    "select",
    "press",
    "back",
    "close",
)
#: mode → required fields (``find`` needs one of text/regex, checked apart).
_REQUIRED = {
    "open": ("url",),
    "click": ("ref",),
    "type": ("ref", "text"),
    "select": ("ref", "value"),
    "press": ("key",),
}


class BrowserTool(Tool):
    name = "browser"
    #: `runtime.browser_resource` 가 None 이면(playwright 없음·`AGENT_CLI_BROWSER_TOOL=0`)
    #: tools_list 에서 벗겨져 프롬프트에 나오지 않는다.
    requires_handler = "browser"
    description = (
        "Headless browser for pages that need JavaScript or interaction (for "
        "plain HTML use fetch). mode 'open' {url} loads a page and returns its "
        "snapshot: an accessibility tree where buttons, links, inputs and "
        "landmarks carry [ref=eN]. Act with a ref: 'click' {ref}; 'type' {ref, "
        "text, submit?} (submit presses Enter); 'select' {ref, value}; 'press' "
        "{key}. 'snapshot' {ref?, depth?} re-reads the page or one landmark; "
        "'find' {text | regex} returns only the matching nodes with their path; "
        "'text' {ref?} returns the readable text of the page or an element — use "
        "it for articles and long content, snapshots cut long text. 'back', "
        "'close'. Refs stay valid until the next page load; when an action "
        "changes the page the result already includes the new snapshot (cut to "
        "the depth that fits when the page is big — the hidden part is still "
        "reachable by find/text/snapshot)."
    )
    oversized_retry_hint = (
        "snapshot one landmark (snapshot with ref=<its ref>) or a shallower depth, "
        "find(text=…) the element you need, or text(ref=…) for the article body "
        "— not the whole page."
    )
    parameters: ClassVar[dict] = {
        "type": "object",
        "properties": {
            "mode": {
                "type": "string",
                "enum": list(MODES),
                "description": " | ".join(MODES),
            },
            "url": {
                "type": "string",
                "minLength": 1,
                "description": "open: http(s) URL",
            },
            "ref": {
                "type": "string",
                "description": "click/type/select: the element's [ref=eN]; snapshot/text: optional landmark ref to scope to",
            },
            "text": {
                "type": "string",
                "description": "type: the text to enter; find: substring to look for (case-insensitive)",
            },
            "regex": {
                "type": "string",
                "description": "find: regular expression instead of text",
            },
            "submit": {
                "type": "boolean",
                "description": "type: press Enter after typing (default false)",
            },
            "value": {"type": "string", "description": "select: option value or label"},
            "key": {
                "type": "string",
                "description": "press: key name, e.g. Enter, Escape, Tab",
            },
            "depth": {
                "type": "integer",
                "description": "snapshot: keep only nodes up to this depth below the root",
            },
        },
        "required": ["mode"],
    }

    def validate(self, args: dict) -> str | None:
        mode = str(args.get("mode", "")).strip()
        if mode not in MODES:
            return f"browser: mode must be one of {', '.join(MODES)}"
        for f in _REQUIRED.get(mode, ()):
            if not str(args.get(f, "") or "").strip():
                return f"browser: {mode} requires `{f}`"
        if mode == "find" and not (
            str(args.get("text", "") or "").strip()
            or str(args.get("regex", "") or "").strip()
        ):
            return "browser: find requires `text` or `regex`"
        return None

    def summary_arg(self, action_input: dict) -> str:
        a = self.strip_prefix(action_input)
        mode = str(a.get("mode", ""))
        rest = next(
            (str(a[k]) for k in ("url", "ref", "text", "regex", "key") if a.get(k)),
            "",
        )
        return f"{mode} {rest}".strip()

    def _run(self, args: dict, *, ctx=None) -> ToolResult:
        from agent_cli.browser import AUTO_SNAPSHOT_SHARE, BrowserError, get_browser

        mode = str(args.get("mode", "")).strip()
        session = get_browser(create=mode == "open")
        if session is None:
            return ToolResult(
                False,
                error="browser: no page is open — call mode 'open' with a url first",
            )
        # 이동 뒤 자동 스냅샷 예산 — 과대 출력 캡(창/10)의 일부. 캡이 꺼져
        # 있으면(0) 예산도 없다.
        budget = int((ctx.oversized_cap if ctx else 0) * AUTO_SNAPSHOT_SHARE)
        try:
            out = self._dispatch(session, mode, args, budget)
        except BrowserError as e:
            return ToolResult(False, error=f"browser: {e}")
        except Exception as e:
            first = (str(e).strip().splitlines() or [type(e).__name__])[0]
            return ToolResult(False, error=f"browser: {type(e).__name__}: {first}")
        return ToolResult(True, output=out)

    @staticmethod
    def _dispatch(session, mode: str, args: dict, budget: int = 0) -> str:
        ref = str(args.get("ref", "") or "").strip()
        if mode == "open":
            return session.open(str(args["url"]).strip(), budget=budget)
        if mode == "snapshot":
            depth = args.get("depth")
            return session.snapshot(ref, int(depth) if depth is not None else None)
        if mode == "find":
            return session.find(
                text=str(args.get("text", "") or ""),
                regex=str(args.get("regex", "") or ""),
            )
        if mode == "text":
            return session.text(ref)
        if mode == "click":
            return session.click(ref, budget=budget)
        if mode == "type":
            return session.type(
                ref, str(args["text"]), bool(args.get("submit", False)), budget=budget
            )
        if mode == "select":
            return session.select(ref, str(args["value"]), budget=budget)
        if mode == "press":
            return session.press(str(args["key"]).strip(), budget=budget)
        if mode == "back":
            return session.back(budget=budget)
        return session.close()
