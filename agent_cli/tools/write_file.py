"""Write file tool."""

from __future__ import annotations

import difflib
from pathlib import Path
from typing import ClassVar

from agent_cli.tools._change_echo import render_change_echo
from agent_cli.tools.base import Tool
from agent_cli.tools.read_file import format_hashlines, format_hashlines_range
from agent_cli.tools.result import ToolResult

# A write_file that OVERWRITES an existing file but changes only a small
# fraction of its lines is almost always a case edit_file would do far cheaper
# — re-writing re-sends the whole file into context each turn. Below this
# changed-line fraction the write counts as a "small overwrite", which drives
# BOTH effects in one decision: (1) a one-line steering nudge, and (2) the
# observation echoes a DIFF + fresh hashline refs for the changed region
# (render_change_echo, shared with edit_file) instead of the whole file's
# hashlines, so the churn-case echo shrinks to ~diff+region size while still
# handing the model the refs to edit_file next. Both are observation-side (no
# mimicry); the write itself is unchanged. Tuned from a real session: small
# edits sat at 2-3% changed, full rewrites at 100%+.
_REWRITE_NUDGE_RATIO = 0.30
# 새 파일·큰 변경의 echo (v10.77.0): 이 줄 수 이하면 파일 전체를 해시라인으로,
# 넘으면 처음·끝 ``_ECHO_EDGE`` 줄만 — 9과제 16런에서 쓰자마자 edit_file 로 고친
# 경우는 적었고(GLM 8·35B 5), 35B 는 15K자 파일을 쓰고 그대로 돌려받았다. 40줄은
# GLM 중앙값(181자)은 통째로, 35B 중앙값(2,256자)은 머리·꼬리만 되는 경계.
_ECHO_FULL_LINES = 40
_ECHO_EDGE = 10


def _small_overwrite_analysis(path: str, new_content: str):
    """Judge whether writing ``new_content`` to ``path`` is a SMALL OVERWRITE,
    BEFORE the write (the old content is gone after). Returns
    ``(is_small, old_content, nudge_text)``:

    - ``is_small`` True iff ``path`` exists, is non-empty/readable, and <
      ``_REWRITE_NUDGE_RATIO`` of the new lines differ from the old.
    - ``old_content`` the prior file text (for the diff echo), or "".
    - ``nudge_text`` the steering note, or "".

    New file / empty / unreadable / genuine rewrite → ``(False, "", "")``: the
    single decision that gates both the nudge and the diff-vs-hashline echo."""
    p = Path(path)
    if not p.is_file():
        return (False, "", "")
    try:
        old = p.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return (False, "", "")
    if not old.strip():
        return (False, "", "")
    old_lines = old.splitlines()
    new_lines = new_content.splitlines()
    sm = difflib.SequenceMatcher(None, old_lines, new_lines)
    changed = sum(
        max(i2 - i1, j2 - j1)
        for tag, i1, i2, j1, j2 in sm.get_opcodes()
        if tag != "equal"
    )
    total = max(len(new_lines), 1)
    if changed / total >= _REWRITE_NUDGE_RATIO:
        return (False, "", "")
    pct = round(changed / total * 100)
    # 명령("Use edit_file next time.")이 아니라 사실 — 넛지 뒤 edit_file 로 간
    # 경우는 GLM 3/6·35B 0/2 였다 (v10.77.0).
    nudge = (
        f"[note] Only ~{pct}% of lines changed ({changed}/{total}). `edit_file` "
        f"would have sent just those lines; this rewrite sent the whole file, "
        f"and it stays in your context."
    )
    return (True, old, nudge)


def tool_write_file(args: dict) -> ToolResult:
    """Create or overwrite a file with raw content.

    Returns the written content in hashline format (LINE#HASH:content) so
    the model can ``edit_file`` the file it just wrote WITHOUT a separate
    ``read_file`` round-trip. This removes the friction that pushes models
    to re-``write_file`` the whole file on every small change (observed:
    same file rewritten in full 4× in one session, edit_file used 0×).

    A SMALL overwrite (< 30% lines changed) is the exception: instead of the
    full hashline echo it shows a diff plus fresh hashline refs for the changed
    region (``render_change_echo``, shared with edit_file), paired with the
    steering nudge — the churn-case echo shrinks to ~diff+region size while
    still handing the model the refs to edit_file next. A file longer than
    ``_ECHO_FULL_LINES`` echoes only its first and last ``_ECHO_EDGE`` lines
    (v10.77.0) — the rest is a read_file away.
    """

    path = args.get("path", "")
    content = args.get("content", "")
    from agent_cli.tools import _confine

    denial = _confine.guard([path], "write_file")
    if denial:
        return ToolResult(False, error=denial)
    try:
        p = Path(path)
        # Judge small-overwrite BEFORE writing — the old content is gone after.
        is_small, old_content, nudge = _small_overwrite_analysis(path, content)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        all_lines = content.split("\n")
        total = len(all_lines)
        header = f"File saved: {path} ({total} lines, {len(content):,} bytes)"
        if nudge:
            header += f"\n{nudge}"
        if is_small:
            # Small overwrite: echo a diff of the changed lines PLUS fresh
            # hashline refs for the changed region (shared with edit_file via
            # render_change_echo) — shows the model exactly what it should have
            # edit_file'd AND gives it the refs to do so next without a
            # read_file. Far cheaper than dumping every line as a hashline.
            body = render_change_echo(old_content, content, path)
            msg = f"{header}\n{body}" if body else header
        elif total <= _ECHO_FULL_LINES:
            # New file or genuine rewrite of a short file: full hashline echo —
            # the same format read_file emits, so it doubles as the edit_file
            # ref source.
            msg = (
                f"{header}\n"
                f"To modify, call `edit_file` with the hashline refs below "
                f"(no need to `read_file` first):\n"
                f"{format_hashlines(content)}"
            )
        else:
            # 긴 파일: 처음·끝만 — 나머지는 read_file 이 태그를 붙여 보여 준다.
            tail_start = total - _ECHO_EDGE
            msg = (
                f"{header}\n"
                f"Lines 1–{_ECHO_EDGE} and {tail_start + 1}–{total} are tagged below "
                f"for `edit_file`; `read_file` shows the rest tagged.\n"
                f"{format_hashlines_range(all_lines, 0, _ECHO_EDGE)}\n"
                f"…\n"
                f"{format_hashlines_range(all_lines, tail_start, total)}"
            )
        # Refresh code_index after a successful write. Best-effort —
        # post_hook swallows its own exceptions so an indexing hiccup
        # never poisons the user-facing write.
        from agent_cli.tools.code_index import post_hook

        post_hook(path)
        return ToolResult(True, output=msg)
    except Exception as e:
        return ToolResult(False, error=f"write_file failed: {e}")


class WriteFileTool(Tool):
    name = "write_file"
    # The edit-vs-write rule lives HERE and only here (v10.13.0): this is where
    # the choice is made, and ``edit_file`` sits right above it in the prompt.
    # It used to be told four times — twice contradicting itself ("a NEW file
    # or a genuine FULL rewrite" here and atop the edit guide, "only for
    # creating new files" at the guide's end). A full rewrite is allowed for a
    # small file, and named as the way out when an edit keeps failing: a model
    # that kept failing edit_file had been told rewriting was off limits.
    description = (
        "Create a file, or overwrite one, with exactly the content you give. The "
        "content is its own field, so it needs no shell quoting. Use it for a new file "
        "or to rewrite a small file in full; to change part of an existing file, "
        "`edit_file` sends only the changed lines, while a rewrite sends the whole file"
        " again and keeps it in your context. If the same edit keeps failing on a small"
        " file, rewriting it is fine. Returns the written file tagged LINE#HASH:content"
        " — the whole file when it is short, otherwise its first and last lines; "
        "`read_file` shows the rest tagged."
    )
    # edit_file 이 없는 루프용 설명(``DESCRIPTION_WITHOUT_EDIT``)은 v10.77.0 에서
    # 지웠다 — write_file 이 있으면 edit_file 도 붙는다(``EditFileTool.mounted_with``).
    # Flat-native (consolidation roadmap Step 3): the schema is the plain
    # single-target shape — no `write_file_` wire-key prefix. `wrap_single_op`
    # is identity (no canonical re-wrap), so the flat op dispatches straight
    # through. `key_prefix` is left at its default; strip_prefix is then a
    # no-op on these unprefixed keys, and `claims` correctly returns False for
    # a flat `{path}` (no `write_file_` key) so it stays out of infer_action.
    parameters: ClassVar[dict] = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "minLength": 1,
                "description": "File path to write; missing parent directories are created.",
            },
            "content": {
                "type": "string",
                "description": "The whole file, exactly as it should be saved.",
            },
        },
        "required": ["path", "content"],
    }

    def wrap_single_op(self, flat: dict) -> dict:
        return flat

    def touched_paths(self, action_input: dict) -> list[str]:
        p = self.strip_prefix(action_input).get("path")
        return [p] if isinstance(p, str) and p else []

    def summary_arg(self, action_input: dict) -> str:
        return self.strip_prefix(action_input).get("path", "")

    # NOTE: no ``render_action_input_for_context`` override — eliding the
    # written ``content`` on re-feed taught mimicry-prone models to emit the
    # marker AS the file body (real corruption; the model only recovered by
    # routing around write_file via ``shell`` heredocs). Reverted in v3.16.1;
    # the body stays verbatim on re-feed. See docs/ARCHITECTURE.md §5.4.

    def _run(self, args: dict, *, ctx=None) -> ToolResult:
        return tool_write_file(args)
