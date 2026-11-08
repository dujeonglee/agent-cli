"""Failure detectors for the recovery layer.

Two flavors:
- **Stateful** detectors are classes owned by the AgentLoop instance,
  carrying state across turns (e.g. ``ActionLoopDetector``, ``StaleGuard``).
- **Stateless** detectors are pure functions called per-decision; they
  inspect a single response/dispatch attempt without remembering
  anything (e.g. ``detect_unknown_tool``, ``detect_schema_mismatch``).

The split mirrors what each failure category actually needs — adding
ceremony (a class) for a stateless one-line check would be cargo-cult
symmetry without value. New stateful failures get classes; new
stateless ones get functions.

See ``docs/robust-harness/DESIGN.md`` §1 (Layer A & B failures) and §3.1.
"""

from __future__ import annotations

import hashlib
import json
from collections import deque
from typing import Any


class ActionLoopDetector:
    """Detect consecutive identical (action, args) emissions (failure B1).

    Fires escalation level 1+ when the same (action, normalized_args)
    has been observed ``threshold`` times in a row. Subsequent identical
    observations bump the level (1, 2, 3, ...) so the playbook can
    escalate primitives. A different action or an explicit error-retry
    resets the counter and the fire count.

    The detector looks only at the model's *intent* (action + args) — it
    does not consult the model's response text or the tool result. That
    keeps it purely about "the model wants to do X with Y again". Whether
    the earlier identical calls *made progress* is the caller's judgment
    (v10.107.0: ``TurnDispatcher._progressed`` compares the last two
    results of the same call) and arrives as ``progressed=True`` — the same
    reset as an error retry.

    Threshold default is 2 (early nudge): after one repeat the next
    observation fires. The 2-fire principle mirrors human metacognition
    — repeating the same action twice should already prompt "wait, did
    I just do this?". The harness gives the model the same nudge.
    """

    def __init__(self, threshold: int = 2):
        if threshold < 2:
            raise ValueError("threshold must be ≥ 2 (one repeat is not a loop)")
        self.threshold = threshold
        self._last_signature: str | None = None
        self._consecutive_count = 0
        self._run_length = 0
        self._fire_count = 0

    @property
    def fire_count(self) -> int:
        """Number of times the detector has fired for the current loop."""
        return self._fire_count

    @property
    def consecutive_count(self) -> int:
        """Consecutive emissions of the current signature counted toward the
        threshold — an error retry or a progress reset starts it again at 1."""
        return self._consecutive_count

    @property
    def run_length(self) -> int:
        """How many times in a row the current signature was emitted, resets
        included (v10.107.0) — the number the nudge quotes to the model. Only a
        different signature restarts it."""
        return self._run_length

    def observe(
        self,
        action: str,
        args: Any,
        *,
        prev_was_error: bool = False,
        progressed: bool = False,
    ) -> int:
        """Record an action emission. Returns escalation level.

        Returns:
            0 — no fire (below threshold, or reset by an error retry)
            1 — first fire (just hit threshold)
            2 — second fire (kept emitting after the first intervention)
            3+ — subsequent fires (caller should hard-fail)

        Args:
            action: Tool name the model is about to invoke.
            args: Tool input. Normalized to a canonical JSON string for
                comparison; dict key ordering is ignored.
            prev_was_error: ``True`` when the previous tool call (with
                the same signature) returned an error. Error retries
                are legitimate — counter resets, no fire.
            progressed: ``True`` when the earlier identical calls are still
                making progress (v10.107.0 — their last two results differ, or
                only one has run so there is nothing to compare). Same reset
                as ``prev_was_error``.
        """
        sig = self._canonical(action, args)
        self._run_length = self._run_length + 1 if sig == self._last_signature else 1

        if prev_was_error or progressed:
            # Legitimate retry after a failure, or a repeat whose results are
            # still changing — reset counter so the current emission is
            # treated as a fresh attempt. The next
            # *same-sig* emission after a successful run can still fire.
            self._last_signature = sig
            self._consecutive_count = 1
            self._fire_count = 0
            return 0

        if sig == self._last_signature:
            self._consecutive_count += 1
        else:
            self._last_signature = sig
            self._consecutive_count = 1
            self._fire_count = 0

        if self._consecutive_count >= self.threshold:
            self._fire_count += 1
            return self._fire_count
        return 0

    @staticmethod
    def _canonical(action: str, args: Any) -> str:
        """Build a hashable signature from ``action`` + ``args``.

        ``json.dumps(sort_keys=True)`` makes dict key ordering
        irrelevant. Non-JSON-serializable values fall back to ``repr``
        — the canonicalization is best-effort, not a security boundary.
        """
        try:
            args_str = json.dumps(args, sort_keys=True, default=str)
        except (TypeError, ValueError):
            args_str = repr(args)
        return f"{action}({args_str})"


#: 제자리 고리 감지 창 — 최근 이만큼의 턴에 나온 (도구, 결과) 쌍을 "본 것" 으로 친다.
STALE_WINDOW = 8
#: 제자리 턴이 이만큼 연달아면 발화한다.
STALE_RUN = 3
#: 든 턴은 제자리가 아니다 — 기다리는 도구는 같은 결과가 정상이다.
STALE_RESET_TOOLS = frozenset({"wait", "monitor"})
#: 결과가 같아도 진척일 수 있는 쓰기 도구 — 그 쌍은 제자리로 세지 않는다.
STALE_WRITE_TOOLS = frozenset({"write_file", "edit_file", "snapshot", "memory"})
#: 정보가 없는 결과 — 같아도 "이미 본 결과" 가 아니다.
_STALE_GENERIC = frozenset({"", "(no output)"})
_OBSERVATION_PREFIX = "Observation: "


def stale_turn_pairs(records: list[dict]) -> list[tuple[str, str]] | None:
    """한 턴이 기록한 레코드들 → 그 턴의 (도구 이름, 관찰 본문) 쌍 목록 (v10.120.0).

    ``records`` 는 그 턴의 디스패치가 ``ctx.add`` 한 것 그대로(history.jsonl 의 그 구간과
    같은 내용)다. 정의는 ATIF 변환(`bench/harbor/atif.py`)이 trajectory 의 호출과 결과를
    짝짓는 방식과 같다 — assistant 레코드의 op(``complete`` 제외)마다, 개입이 아닌 관찰
    레코드의 본문을 순서대로 붙이고, 모자라면 빈 결과. 다른 점 하나: 묶음 관찰은 op 별
    조각(``parts``)으로 나눠 붙인다 — trajectory 는 합친 본문을 첫 호출에만 붙인다.

    assistant 레코드가 없으면 ``None`` — 저장되지 않은 시도(형식 재시도·B1 이 막은 반복)는
    턴이 아니다(턴 미계수 규칙과 같다). 감지기를 부르지 않는다."""
    assistant = next((r for r in records if r.get("role") == "assistant"), None)
    if assistant is None:
        return None
    names = [
        str(op["action"])
        for op in assistant.get("ops") or []
        if isinstance(op, dict) and op.get("action") and op.get("action") != "complete"
    ]
    contents: list[str] = []
    for r in records:
        if r.get("role") != "user" or not r.get("tool") or r.get("recovery"):
            continue
        parts = r.get("parts")
        if parts:
            contents.extend(str(p.get("content") or "") for p in parts)
        else:
            contents.append(
                str(r.get("content") or "").removeprefix(_OBSERVATION_PREFIX)
            )
    return [
        (name, contents[i] if i < len(contents) else "") for i, name in enumerate(names)
    ]


class StaleGuard:
    """제자리 고리 감지 — 새 결과 없는 턴이 연달아 이어진다 (v10.120.0, docs/stale-guard).

    B1(`ActionLoopDetector`)은 **바로 연달은 같은 호출**만 본다. 실측 고리는 그 모양이
    아니었다: 글자를 조금씩 바꾼 명령이 같은 결과를 내고, 사이에 되돌리기가 끼었다
    (2f2mRbp — `ls … | wc -l` → 17 을 되풀이). 이 감지기는 호출 글자를 보지 않고 **결과**를
    본다.

    턴 하나는 그 턴에 실행된 호출의 (도구, 관찰 본문) 쌍 목록이다. 모든 쌍이 비어 있지
    않은 결과이고, 쓰기 도구가 아니고, ``(no output)`` 이 아니고, 최근 ``window`` 턴 안에
    같은 (도구, 결과) 쌍이 이미 있었으면 "제자리 턴" 이다. 쌍이 없거나, `wait`·`monitor` 가
    들었거나, 하니스 작업(그 런의 백그라운드 작업·모니터)이 도는 중이면 제자리가 아니다.
    제자리 턴이 ``run`` 번 연달아면 발화하고 연속 수는 0 으로 돌아간다.

    순수 객체다 — 입력은 턴별 쌍과 "작업 진행 중" 불리언뿐이다. 오프라인 대입 스크립트
    (규칙 C, N3W8)와 같은 정의이고 등가성은 기록 trajectory 1,153개로 확인했다."""

    def __init__(self, window: int = STALE_WINDOW, run: int = STALE_RUN):
        if window < 1 or run < 1:
            raise ValueError("window and run must be ≥ 1")
        self._target = run
        # 지난 턴마다 "본 것" 으로 칠 쌍의 키 (도구, 본문 지문) — 창 밖은 저절로 빠진다
        self._window: deque[frozenset[tuple[str, str]]] = deque(maxlen=window)
        self._run = 0
        self._streak = 0
        self._since_turn = 0
        self._fires = 0

    @property
    def fires(self) -> int:
        """이 런의 발화 수 — 사람이 Continue 를 고르면 1 로 돌아간다."""
        return self._fires

    @property
    def run(self) -> int:
        """다음 발화까지 센 연속 제자리 턴 — 발화하면 0 으로."""
        return self._run

    @property
    def turns_without_new(self) -> int:
        """지금까지 이어진 제자리 턴 수 — 발화로 리셋되지 않는다. 화면의 N."""
        return self._streak

    @property
    def since_turn(self) -> int:
        """지금 이어지는 제자리 구간이 시작된 턴 (구간이 없으면 0)."""
        return self._since_turn if self._streak else 0

    def observe(
        self, pairs: list[tuple[str, str]], *, turn: int, work_running: bool = False
    ) -> bool:
        """한 턴을 먹인다. 발화했으면 ``True``."""
        keyed = [
            (
                tool,
                hashlib.sha256(body.encode("utf-8", "replace")).hexdigest(),
                bool(body.strip())
                and tool not in STALE_WRITE_TOOLS
                and body not in _STALE_GENERIC,
            )
            for tool, body in pairs
        ]
        seen: set[tuple[str, str]] = set().union(*self._window)
        stale = (
            bool(keyed)
            and not work_running
            and not any(tool in STALE_RESET_TOOLS for tool, _, _ in keyed)
            and all(ok and (tool, digest) in seen for tool, digest, ok in keyed)
        )
        self._window.append(
            frozenset((tool, digest) for tool, digest, ok in keyed if ok)
        )
        if not stale:
            self._run = 0
            self._streak = 0
            return False
        if self._streak == 0:
            self._since_turn = turn
        self._streak += 1
        self._run += 1
        if self._run < self._target:
            return False
        self._run = 0
        self._fires += 1
        return True

    def acknowledge(self) -> None:
        """사람이 Continue 를 골랐다 — 연속 수 0, 발화 수 1. 다음 발화에서 다시 묻는다."""
        self._run = 0
        self._fires = 1


def detect_unknown_tool(action: str, tools_list: list[str]) -> bool:
    """Stateless A4 detector: is ``action`` outside the active tool registry?

    Returns ``True`` when the action references a tool not in
    ``tools_list``. The recovery layer uses this to label the failure
    and skip dispatch — the model sees the resulting observation
    (currently the same "Unknown tool `X`. Available: [...]" message
    produced by the leaf-level dispatch).
    """
    return bool(action) and action not in tools_list


def detect_schema_mismatch(
    action: str, action_input: Any
) -> tuple[bool, str | None, Any]:
    """Stateless A5 detector: does ``action_input`` match the tool schema?

    Returns ``(mismatched, error_message, normalized_input)``:
    - ``mismatched``: True when the input violates the schema.
    - ``error_message``: human-readable description (None on success).
    - ``normalized_input``: the (possibly auto-converted) input value
      when valid — string-to-dict promotion happens inside
      ``validate_tool_input``. Caller should use this normalized value
      for downstream dispatch.

    Wraps ``tools.registry.validate_tool_input`` so the recovery layer
    has its own vocabulary entry without owning the schema knowledge.
    Imported lazily to avoid a registry → tool-modules → recovery import
    cycle at module-load time.
    """
    from agent_cli.tools.registry import validate_tool_input

    valid, err, normalized = validate_tool_input(action, action_input)
    if valid:
        return (False, None, normalized)
    return (True, err, normalized)


def detect_nested_envelope(result_value: Any) -> bool:
    """Stateless A6 detector: does the complete result wrap another envelope?

    Some models (notably qwen3.5/3.6 family) double-wrap the complete
    action's payload — they emit
    ``{"action":"complete","action_input":{"result":"<JSON of {result:...}>"}}``
    instead of the intended single-level envelope. The user-facing
    answer ends up prefixed with a literal ``{"result": "..."}`` artifact.

    Returns True when ``result_value`` is a string that successfully
    parses as a JSON object containing a top-level ``result`` key.
    Strings that merely *start* with ``{"result"`` but fail to parse
    are not flagged — false positives on legitimate text that happens
    to look like JSON would corrupt observability data more than
    missed cases.

    Uses ``strict=False`` for the parse: the same models that
    double-wrap also routinely emit the inner JSON body with
    literal newlines / tabs inside its string fields (markdown
    bodies, line-wrapped prose) instead of properly escaped
    ``\\n`` / ``\\t``. Strict mode rejects those as "invalid
    control character", which previously slipped 5 / 8 cases on
    a real parallel-delegate run through the detector unchanged.
    ``strict=False`` keeps the structural guard (must be a JSON
    object with ``result`` key) while accepting the messy bodies
    these models actually produce.

    ``FAILURE_NESTED_ENVELOPE`` is still recorded by the caller for
    measurement; the auto-unwrap that ``unwrap_nested_envelope``
    performs only fixes the user-facing artifact.
    """
    if not isinstance(result_value, str):
        return False
    stripped = result_value.lstrip()
    if not stripped.startswith('{"result"'):
        return False
    try:
        parsed = json.loads(stripped, strict=False)
    except (json.JSONDecodeError, ValueError):
        return False
    return isinstance(parsed, dict) and "result" in parsed


def unwrap_nested_envelope(result_value: str) -> str:
    """One-level unwrap for the pattern ``detect_nested_envelope``
    matches. Returns the inner ``result`` value when it's a string,
    or ``result_value`` unchanged when:
      - the input doesn't match the nested-envelope pattern,
      - parsing fails,
      - the inner ``result`` is not a string (so the unwrap would
        change the answer's shape — better to surface the LLM's
        actual output than silently coerce).

    Uses ``strict=False`` for parity with ``detect_nested_envelope``
    — see its docstring for why the lenient mode matters.

    Single-level only by design: recursive nesting (``{"result":
    "{\\"result\\": ...}"}``) is rare and almost always indicates a
    different bug that benefits from staying visible.
    """
    if not isinstance(result_value, str):
        return result_value
    stripped = result_value.lstrip()
    if not stripped.startswith('{"result"'):
        return result_value
    try:
        parsed = json.loads(stripped, strict=False)
    except (json.JSONDecodeError, ValueError):
        return result_value
    if not (isinstance(parsed, dict) and "result" in parsed):
        return result_value
    inner = parsed["result"]
    return inner if isinstance(inner, str) else result_value
