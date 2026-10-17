"""Edit file tool — hashline-verified edits, one apply core (v10.48.0).

A single ``edit_file`` op and a run of same-file ops go through the SAME core
(:func:`apply_edits_batch_parts`): resolve every ref against one read, reject
overlaps, apply bottom-up, write once. A lone op is a batch of one — there is
no second implementation of the op semantics to drift. Fuzzy ref matching was
removed in v10.48.0: its acceptance condition (the NORMALIZED current line
hashing to the ORIGINAL line's tag) almost never holds, and it fired 0 times in
460 recorded edits; a stale ref is a hash mismatch and the model re-reads.
"""

from __future__ import annotations

from pathlib import Path
from typing import ClassVar

from agent_cli.tools._change_echo import render_change_echo
from agent_cli.tools.base import Tool
from agent_cli.tools.read_file import _verify_ref
from agent_cli.tools.result import ToolResult

#: Edit operations — single source for the schema ``enum`` and the checks.
EDIT_OPS = ("replace", "append", "prepend", "delete")


def _resolve_ref(lines: list[str], ref: str) -> int:
    """Exact ref → 0-based index. A hash mismatch keeps the edit tool's own
    wording (the file moved since the read — re-read and retry); parse and
    range errors pass through from ``_verify_ref`` unchanged."""
    try:
        return _verify_ref(lines, ref)
    except RuntimeError as exc:
        if not str(exc).startswith("Hash mismatch"):
            raise
        line_num = str(exc).split("line ", 1)[1].split(":", 1)[0]
        raise RuntimeError(
            f"Hash mismatch at line {line_num}: ref '{ref}' does not match "
            f"current content.{_RETRY_HINT}"
        ) from None


def _op_label(ed: dict) -> str:
    """``op 3 (replace 8#ZZ..9#QQ)`` — what the model sent, so it can find the op."""
    parts = [str(ed.get("op", "?"))]
    if ed.get("pos"):
        parts.append(str(ed["pos"]) + (f"..{ed['end']}" if ed.get("end") else ""))
    return " ".join(parts)


_RETRY_HINT = " Re-read the file with read_file to get fresh hashline tags, then retry."


def _strip_retry_hint(msg: str) -> str:
    """The per-ref message ends with a re-read hint; the batch summary says it
    once for all rejected ops."""
    return msg.removesuffix(_RETRY_HINT)


def _op_to_span(ed: dict, file_lines: list[str]) -> tuple[int, int, list]:
    """Map one edit op to the half-open ORIGINAL-line span it replaces:
    ``(lo, hi, repl)`` where ``file_lines[lo:hi] = repl``. ``lo == hi`` is a pure
    insertion. Refs are resolved against ``file_lines`` (the single original
    read) with ``_verify_ref``. Raises ``RuntimeError`` on a bad op / ref (the
    caller turns it into a clean Observation)."""
    op = ed.get("op", "")
    pos = ed.get("pos")
    end = ed.get("end")
    lines = ed.get("lines")
    if isinstance(lines, str):
        lines = lines.split("\n")
    if lines is None:
        lines = []

    def resolve(ref):
        return _resolve_ref(file_lines, ref)

    if op in ("replace", "delete"):
        if not pos:
            raise RuntimeError(f"{op} requires 'pos'.")
        repl = [] if op == "delete" else lines
        lo = resolve(pos)
        hi = (resolve(end) + 1) if end else lo + 1
        return lo, hi, repl
    if op == "append":
        if pos:
            i = resolve(pos)
            return i + 1, i + 1, lines
        return len(file_lines), len(file_lines), lines
    if op == "prepend":
        if pos:
            i = resolve(pos)
            return i, i, lines
        return 0, 0, lines
    raise RuntimeError(f"Unknown edit op: '{op}'. Use {'|'.join(EDIT_OPS)}.")


def _find_overlap(spans: list[tuple]) -> str | None:
    """Return a human description of the first overlapping pair, or None.

    Each span is ``(lo, hi, repl, emit_idx)``; ``[lo, hi)`` is half-open so
    ADJACENT ranges (``[2,4)`` and ``[4,5)``) do NOT overlap (decision: reject
    only on true overlap). A pure insert (``lo == hi``) conflicts only when it
    lands STRICTLY inside a replace/delete range (its anchor line is removed);
    inserts sharing a position with each other are fine."""
    for a in range(len(spans)):
        lo1, hi1, _, e1 = spans[a]
        for b in range(a + 1, len(spans)):
            lo2, hi2, _, e2 = spans[b]
            ins1, ins2 = lo1 == hi1, lo2 == hi2
            conflict = False
            if not ins1 and not ins2:  # range vs range
                conflict = lo1 < hi2 and lo2 < hi1
            elif ins1 and not ins2:  # insert inside range?
                conflict = lo2 < lo1 < hi2
            elif ins2 and not ins1:
                conflict = lo1 < lo2 < hi1
            # insert vs insert: never a conflict
            if conflict:
                return f"edit #{e1 + 1} (lines {lo1 + 1}-{hi1}) and edit #{e2 + 1} (lines {lo2 + 1}-{hi2})"
    return None


def apply_edits_batch(path: str, edits: list[dict]) -> ToolResult:
    """``apply_edits_batch_parts`` 의 합친 결과만 — 단일 ToolResult 소비자용."""
    result, _parts = apply_edits_batch_parts(path, edits)
    return result


_EDIT_PART_DIFF_LINES = 6  # op 별 조각에 보이는 -/+ 줄 수 상한 (한쪽당)


def _edit_part(ed: dict, lo: int, hi: int, repl: list[str], original: list[str]) -> str:
    """편집 하나의 결과 조각 (v10.23.0) — 원본 기준 줄 범위와 그 자리의 -/+."""
    label = _op_label(ed)
    if lo == hi:
        head = f"{label}: inserted {len(repl)} line(s) after original line {lo}"
    elif not repl:
        head = f"{label}: deleted original lines {lo + 1}–{hi} ({hi - lo} line(s))"
    else:
        head = (
            f"{label}: replaced original lines {lo + 1}–{hi} "
            f"({hi - lo} → {len(repl)} line(s))"
        )
    body: list[str] = []
    for sign, lines in (("-", original[lo:hi]), ("+", repl)):
        shown = lines[:_EDIT_PART_DIFF_LINES]
        body += [f"{sign} {line}" for line in shown]
        if len(lines) > len(shown):
            body.append(f"  … {len(lines) - len(shown)} more {sign} line(s)")
    return head + ("\n" + "\n".join(body) if body else "")


def apply_edits_batch_parts(
    path: str, edits: list[dict]
) -> tuple[ToolResult, list[dict]]:
    """Apply SEVERAL edits to ONE file against a single original read.

    Returns the combined ``ToolResult`` **and** one part per edit, in emit
    order — ``{"success", "content"}`` (v10.23.0). The harness knows every
    edit's span, so each call gets its own result instead of one merged
    message: the per-edit parts carry the op's original-line range and its
    -/+ lines; the LAST part also carries the file-level summary and the
    fresh-hashline echo (the state after all edits, nearest to the next
    turn). On rejection every part says what happened to ITS op — the bad
    ref's reason, or "valid as sent, not applied" — and the last part carries
    the re-send instruction. Text dialects render the parts with ``[i/N]``
    headers; native_fc sends each as the ``tool`` message of its call.

    All refs resolve against the same original content, so a later edit's
    hashline ref stays valid even if an earlier edit shifts lines; bottom-up
    application (highest line first) keeps indices from drifting. ALL-OR-NOTHING:
    any bad ref or overlapping pair → nothing is written and the model re-emits
    the whole group (a partial write would re-introduce the drift it avoids).

    Reuses ``_verify_ref`` / ``format_diff`` / ``post_hook``; pure function
    (path + edits → ToolResult), no loop/ctx/renderer coupling."""
    from agent_cli.tools import _confine

    def _all(err: str) -> tuple[ToolResult, list[dict]]:
        return ToolResult(False, error=err), [
            {"success": False, "content": err} for _ in edits
        ]

    denial = _confine.guard([path], "edit_file")
    if denial:
        return _all(denial)
    try:
        original_text = Path(path).read_text(encoding="utf-8")
    except Exception as e:
        return _all(f"edit_file: cannot read '{path}': {e}")
    file_lines = original_text.split("\n")

    # 1. Resolve EVERY op against the original (all-or-nothing on a bad ref).
    #    Keep going past a bad one (v9.26.3): the model used to learn about
    #    rejected refs ONE per retry, and the message never said that the other
    #    refs were fine and the file untouched — so it re-read and re-emitted
    #    the whole batch each time.
    spans: list[tuple] = []
    bad: dict[int, str] = {}
    raw: dict[int, str] = {}
    for i, ed in enumerate(edits):
        try:
            lo, hi, repl = _op_to_span(ed, file_lines)
            spans.append((lo, hi, repl, i))
        except RuntimeError as e:
            raw[i] = str(e)
            bad[i] = f"op {i + 1} ({_op_label(ed)}): {_strip_retry_hint(str(e))}"
    if bad and len(edits) == 1:
        # A lone op keeps the single-edit wording it always had (v10.48.0 —
        # same core, same model-facing text): op errors bare, ref errors
        # prefixed. No "batch" vocabulary for a model that sent one edit.
        err = raw[0]
        if not err.endswith("requires 'pos'."):
            err = f"edit_file apply failed: {err}"
        return ToolResult(False, error=err), [{"success": False, "content": err}]
    if bad:
        ok = len(edits) - len(bad)
        resend = (
            "Re-read the file for fresh tags of the rejected lines only, then "
            "resend ALL the ops in one turn — the valid ones unchanged, the "
            "rejected ones with corrected refs."
        )
        combined = ToolResult(
            False,
            error=(
                f"edit_file batch: {len(bad)} of {len(edits)} edits rejected — nothing "
                f"written, '{path}' is unchanged.\n"
                + "\n".join(f"  {line}" for line in bad.values())
                + "\n"
                + (
                    f"The other {ok} ref{'s are' if ok != 1 else ' is'} valid as sent. "
                    if ok
                    else ""
                )
                + resend
            ),
        )
        parts = []
        for i, ed in enumerate(edits):
            if i in bad:
                text = f"{bad[i]} — nothing written, '{path}' is unchanged."
            else:
                text = (
                    f"{_op_label(ed)}: ref valid as sent — not applied, the batch "
                    f"is all-or-nothing and '{path}' is unchanged."
                )
            parts.append({"success": False, "content": text})
        parts[-1]["content"] += "\n" + resend
        return combined, parts

    # 2. Reject overlaps before mutating anything.
    overlap = _find_overlap(spans)
    if overlap:
        text = (
            f"edit_file batch: overlapping edits — {overlap}. Split them across "
            f"turns (re-read between). No changes written."
        )
        return ToolResult(False, error=text), [
            {"success": False, "content": f"{_op_label(ed)}: not applied — {text}"}
            for ed in edits
        ]

    # op 별 조각은 원본 기준 — 적용(아래)으로 줄이 밀리기 전에 만든다.
    parts = [
        {"success": True, "content": _edit_part(edits[i], lo, hi, repl, file_lines)}
        for lo, hi, repl, i in sorted(spans, key=lambda s: s[3])
    ]

    # 3. Bottom-up apply: highest line first so lower indices don't shift.
    #    Tie-break by emit index (descending) so same-position inserts keep
    #    their emitted order in the result.
    for lo, hi, repl, _ in sorted(spans, key=lambda s: (s[0], s[3]), reverse=True):
        file_lines[lo:hi] = repl

    # 4. One write.
    result_text = "\n".join(file_lines)
    try:
        Path(path).write_text(result_text, encoding="utf-8")
    except Exception as e:
        return _all(f"edit_file: cannot write '{path}': {e}")

    n = len(edits)
    msg = (
        f"Edit complete: {path} ({len(file_lines)} lines)"
        if n == 1
        else f"Edit complete: {path} ({n} edits, {len(file_lines)} lines)"
    )
    echo = render_change_echo(original_text, result_text, path)
    if echo:
        msg += "\n\n" + echo
    from agent_cli.tools.code_index import post_hook

    post_hook(path)
    parts[-1]["content"] += "\n\n" + msg
    return ToolResult(True, output=msg), parts


def _validate_semantics(args: dict) -> str | None:
    """op enum + pos/end 하시라인-문자열 형식 (C7) — None=통과, str=오류.

    ``pos``/``end`` 가 bare int/null 로 오면 예전엔 ``_parse_ref`` 의
    ``re.match`` 에서 raw TypeError 가 워커 스레드를 죽였다 — 실행 전에
    잡아 재시도 가능한 문구로. A5 경로(EditFileTool.validate)와 직접
    호출자(tool_edit_file)가 같은 구현을 소비. ``op`` 기본값 ""(누락=
    오류)은 기존 tool_edit_file 과 바이트 동일 의미론."""
    op = args.get("op", "")
    if op not in EDIT_OPS:
        return f"Unknown edit op: '{op}'. Use {'|'.join(EDIT_OPS)}."
    for field in ("pos", "end"):
        v = args.get(field)
        if v is not None and not isinstance(v, str):
            return (
                f"'{field}' must be a hashline string like '5#VR', got "
                f"{type(v).__name__} ({v!r}). Re-read the file with "
                f"read_file to get fresh hashline tags, then retry."
            )
    return None


def tool_edit_file(args: dict) -> ToolResult:
    """Apply ONE hashline edit — a batch of one through the shared core.

    Flat-native (consolidation roadmap Step 3): one op = one edit. Several
    edits to one file are several edit_file ops in a turn; the loop groups a
    consecutive same-path run into one :func:`apply_edits_batch_parts` call.
    """
    semantic_err = _validate_semantics(args)
    if semantic_err:
        return ToolResult(False, error=semantic_err)
    result, _parts = apply_edits_batch_parts(args.get("path", ""), [args])
    return result


class EditFileTool(Tool):
    name = "edit_file"
    description = (
        "Change part of a file — the default way to modify an existing file. "
        "Addresses lines by the hashline refs that read_file returns. "
        "Ops: replace, append, prepend, delete. "
        "delete removes the pos..end range and takes no lines; "
        "replace with lines=[] also deletes (legacy form)."
    )
    # Flat-native (consolidation roadmap Step 3): the schema is the plain
    # single-edit shape — no `edit_file_` wire-key prefix and no `edits` batch
    # array. One op applies one edit; several edits to one file are several
    # edit_file ops in a turn (hashline refs are content-addressed, so they
    # survive line shifts from earlier ops). `wrap_single_op` is identity;
    # `key_prefix` is left at its default so strip_prefix is a no-op on these
    # flat keys and `claims` returns False for a flat `{path}`.
    parameters: ClassVar[dict] = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "minLength": 1, "description": "File path"},
            "op": {
                "type": "string",
                "enum": list(EDIT_OPS),
                "description": "replace | append | prepend | delete",
            },
            # pos 에는 minLength 를 두지 않는다 — append/prepend 가 빈 pos 를
            # 파일 끝/처음으로 받는 분기가 실제로 돈다(문서화되진 않았지만).
            "pos": {
                "type": "string",
                "description": "Hashline ref (e.g. '5#VR') of the target line",
            },
            "end": {
                "type": "string",
                "description": "Hashline ref ending an inclusive range (replace/delete)",
            },
            "lines": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Replacement / inserted lines",
            },
        },
        "required": ["path", "op", "pos"],
    }

    def wrap_single_op(self, flat: dict) -> dict:
        return flat

    def touched_paths(self, action_input: dict) -> list[str]:
        p = self.strip_prefix(action_input).get("path")
        return [p] if isinstance(p, str) and p else []

    def summary_arg(self, action_input: dict) -> str:
        return self.strip_prefix(action_input).get("path", "")

    # NOTE: no ``render_action_input_for_context`` override — see WriteFileTool
    # (action-body re-feed elision caused marker mimicry; reverted in v3.16.1).

    def validate(self, args: dict) -> str | None:
        """C7: op enum·pos/end 형식 — _validate_semantics 단일 구현 위임."""
        return _validate_semantics(args)

    def _run(self, args: dict, *, ctx=None) -> ToolResult:
        return tool_edit_file(args)
