"""Minimal indent renderer — no boxes, resize-safe, nested depth support."""

from __future__ import annotations

import json
import sys
import threading
from contextlib import contextmanager
from io import StringIO

from rich.console import Console
from rich.live import Live
from rich.markdown import Markdown
from rich.markup import escape
from rich.text import Text

from agent_cli.images import is_placeholder_line
from agent_cli.render.base import ConfirmOption, Renderer
from agent_cli.token_estimator import estimate_tokens

_MUTED = "grey46"


def _fmt_tok(n: int) -> str:
    """Compact token count: 1_430_000 → '1.4M', 5234 → '5.2K', 980 → '980'."""
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    return f"{n / 1000:.1f}K" if n >= 1000 else str(int(n))


def _fmt_mmss(seconds: float) -> str:
    """경과/한도 표시용 m:ss — 초 단위 누적(600s)보다 한도 대비 위치가
    즉시 읽힌다. 음수는 0 으로 접는다."""
    s = max(0, int(seconds))
    return f"{s // 60}:{s % 60:02d}"


def _format_token_stats(stats: dict) -> str:
    """Render the token-usage dict into the CLI's single-line summary.

    Order: ttft, in (+speed), out (+speed), context %, cumulative in/out,
    cache. Each part is omitted when its value is zero/absent so other
    providers' lines stay lean.
    """
    parts: list[str] = []
    if stats.get("ttft_ms"):
        parts.append(f"ttft: {stats['ttft_ms']:.0f}ms")
    in_tok = stats.get("in", 0)
    if in_tok:
        sp = stats.get("in_speed", 0)
        parts.append(f"in: {_fmt_tok(in_tok)}" + (f" ({sp:.0f} tok/s)" if sp else ""))
    out_tok = stats.get("out", 0)
    if out_tok:
        sp = stats.get("out_speed", 0)
        parts.append(f"out: {_fmt_tok(out_tok)}" + (f" ({sp:.0f} tok/s)" if sp else ""))
    win = stats.get("context_window", 0)
    if in_tok and win:
        parts.append(
            f"ctx: {_fmt_tok(in_tok)}/{_fmt_tok(win)} ({in_tok / win * 100:.0f}%)"
        )
    if stats.get("total_in"):
        parts.append(f"Σin: {_fmt_tok(stats['total_in'])}")
    if stats.get("total_out"):
        parts.append(f"Σout: {_fmt_tok(stats['total_out'])}")
    if stats.get("cache_read"):
        parts.append(f"cache hit: {_fmt_tok(stats['cache_read'])}")
    if stats.get("cache_write"):
        parts.append(f"cache write: {_fmt_tok(stats['cache_write'])}")
    return " | ".join(parts)


def _fmt_duration(seconds: float) -> str:
    """합계 줄의 시간 — ``38s`` · ``4m12s`` · ``1h02m``."""
    s = max(0, round(seconds))
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m{s % 60:02d}s"
    return f"{s // 3600}h{s % 3600 // 60:02d}m"


def format_usage_totals(totals) -> str:
    """런 끝 합계 한 줄 — ``Total: in 1.4M (cached 1.1M) · out 9.1K · 23 calls · LLM 4m12s``.

    캐시 적중·LLM 시간은 0 이면 뺀다(서버가 안 준 경로). 호출 수는 늘 있다 —
    호출이 없던 런은 이 줄을 부르지 않는다.
    """
    head = f"in {_fmt_tok(totals.input)}"
    if totals.cached:
        head += f" (cached {_fmt_tok(totals.cached)})"
    parts = [head, f"out {_fmt_tok(totals.output)}"]
    parts.append(f"{totals.calls} call" + ("" if totals.calls == 1 else "s"))
    if totals.llm_ns:
        parts.append(f"LLM {_fmt_duration(totals.llm_ns / 1e9)}")
    return "Total: " + " · ".join(parts)


def _colorize_diff_line(line: str) -> str:
    """Rich-markup one plain unified-diff line by its leading character.

    ``format_diff`` now emits a plain standard diff (no colour markup) so
    the LLM observation stays clean; the colour is applied here, at the
    terminal. Content is escaped first so a literal ``[`` in source code
    isn't parsed as a Rich tag. Context / blank / truncation lines fall
    through to plain (escaped) text.
    """
    esc = line.replace("[", "\\[")
    if line.startswith(("--- ", "+++ ")):
        return f"[bold]{esc}[/bold]"
    if line.startswith("@@"):
        return f"[cyan]{esc}[/cyan]"
    if line.startswith("+"):
        return f"[green]{esc}[/green]"
    if line.startswith("-"):
        return f"[red]{esc}[/red]"
    return esc


# East Asian "Ambiguous" (A) chars — `…` `—` `─` `※` `→` `《》` etc. — are
# rendered as 2 columns in CJK-locale terminals (macOS Terminal.app and
# iTerm2 with Korean/Japanese/Chinese locale). Counting them as 1 caused
# the marquee to underestimate paint width, overflow the terminal, and
# wrap onto new lines instead of overwriting in place. We assume
# ambiguous = wide so the calculation is correct in CJK terminals and
# only mildly conservative (a column or two of unused tail) elsewhere.
_WIDE_EAW = ("W", "F", "A")

# Streaming animation: a face that "speaks" the response — silent dots,
# then a partial word, then the whole word, then closed-mouth with the
# completed word as a delivery beat. Cycles per chunk but throttled to
# `_FRAME_INTERVAL` so fast streams don't blur it. `•` (U+2022) is East
# Asian Ambiguous; we treat it as 2 cols throughout so width calc agrees
# with how CJK-locale terminals render it.
#
# Each frame is padded with trailing spaces so all frames have the same
# rendered display width — this keeps the `~N tokens` counter that
# follows the frame anchored at a fixed column. Recomputing per-frame
# every chunk would be wasteful, so we pad once at module load.
_TALK_FRAMES_RAW = (
    "(•_•) < ...",
    "(•o•) < blah",
    "(•O•) < blah-blah",
    "(•_•) < blah-blah",
)

# Thinking animation: face + accumulating "hmmm" murmur → "?"
# realization → "!" eureka. Loops back to a fresh "hmm". Used by
# `spinner_start` via Rich Live at 10 fps; the frames are
# self-describing so the old "thinking..." text prefix becomes
# redundant.
_THINK_FRAMES_RAW = (
    "(•_•) hmm",
    "(•_•) hmmm",
    "(•_•) hmmm...",
    "(•_•) hmmm...?",
    "(•_•) hmmm...!",
)

# Cap streaming frame advancement at ~7 fps so multi-character frames
# (the talking face grows letters across frames) stay readable even
# when chunks arrive faster than the eye can track. Counter still
# updates on every chunk — only the visual frame is throttled.
_FRAME_INTERVAL = 0.15


def _display_width(text: str) -> int:
    """Calculate display width accounting for CJK double-width characters."""
    import unicodedata

    w = 0
    for ch in text:
        eaw = unicodedata.east_asian_width(ch)
        w += 2 if eaw in _WIDE_EAW else 1
    return w


def _pad_to_width(frames: tuple[str, ...]) -> tuple[str, ...]:
    """Right-pad each frame with spaces so all share the widest frame's
    display width. Keeps following content (token counter, thought
    bubble) anchored at a fixed column instead of jiggling as the face
    cycles through different mouth shapes."""
    target = max(_display_width(f) for f in frames)
    return tuple(f + " " * (target - _display_width(f)) for f in frames)


# Width-aligned frame tuples — the public constants used by the
# renderer. Padding happens once at import; the raw tuples remain
# available for tests that want to verify the underlying glyphs.
_TALK_FRAMES = _pad_to_width(_TALK_FRAMES_RAW)
_THINK_FRAMES = _pad_to_width(_THINK_FRAMES_RAW)


class FrameClock:
    """Time-throttled cyclic frame advancer.

    Single source of truth for the animation cadence of
    `spinner_start()` (the thinking spinner). `current()` is meant to be called from a
    repaint callback — it returns the frame to draw *now*, advancing
    only if `_FRAME_INTERVAL` has elapsed since the last advance.
    """

    def __init__(self, frames: tuple[str, ...]):
        self._frames = frames
        self._idx = 0
        self._last = 0.0

    def current(self) -> str:
        import time

        now = time.monotonic()
        if now - self._last >= _FRAME_INTERVAL:
            self._idx += 1
            self._last = now
        return self._frames[self._idx % len(self._frames)]


def _truncate_to_width(text: str, max_width: int) -> str:
    """Truncate text from the left to fit within max_width display columns."""
    import unicodedata

    total = _display_width(text)
    if total <= max_width:
        return text
    # Reserve `…`'s actual rendered width (Ambiguous → 2 cols on CJK
    # terminals). Reserving only 1 used to put the truncated string 1
    # col over budget once we started counting Ambiguous as wide.
    target = max_width - _display_width("…")
    target = max(target, 0)
    w = 0
    for i in range(len(text) - 1, -1, -1):
        eaw = unicodedata.east_asian_width(text[i])
        cw = 2 if eaw in _WIDE_EAW else 1
        if w + cw > target:
            return "…" + text[i + 1 :]
        w += cw
    return "…" + text


_AGENT_LINE_WIDTH = 72  # 에이전트 줄 한 칸의 글자 상한 (요약·과제 첫 줄)


def _one_line(text: str) -> str:
    """여러 줄 글 → 첫 줄(공백 정리) 하나, 상한에서 자른다."""
    first = next((ln.strip() for ln in str(text or "").splitlines() if ln.strip()), "")
    first = " ".join(first.split())
    return (
        first
        if len(first) <= _AGENT_LINE_WIDTH
        else first[: _AGENT_LINE_WIDTH - 1] + "…"
    )


def stop_summary(how: str, notified: list[tuple[str, str]], kept: int) -> str:
    """정지 한 줄의 본문 — 회신을 보낸 상대를 받는 이별로 묶는다:
    ``stopped by user — reply sent to main (r-21, r-22), agt-6 (r-31); 1 queued request kept``."""
    by_to: dict[str, list[str]] = {}
    for to, rid in notified:
        by_to.setdefault(to, []).append(rid)
    if by_to:
        sent = ", ".join(f"{to} ({', '.join(ids)})" for to, ids in by_to.items())
        n = len(notified)
        text = f"{how} — repl{'ies' if n != 1 else 'y'} sent to {sent}"
    else:
        text = f"{how} — nobody was waiting on it"
    if kept:
        text += f"; {kept} queued request{'s' if kept != 1 else ''} kept"
    return text


def _step_summary(tool_input: str) -> str:
    """도구 호출 입력(JSON 글) → 한 줄 요약. 사람이 고른 값 하나(명령·경로·URL·과제…)가
    있으면 그것, 없으면 JSON 그대로를 한 줄로."""
    try:
        args = json.loads(tool_input) if tool_input else {}
    except (TypeError, ValueError):
        return _one_line(tool_input)
    if isinstance(args, dict):
        for k in (
            "command",
            "path",
            "url",
            "query",
            "pattern",
            "task",
            "message",
            "to",
        ):
            v = args.get(k)
            if isinstance(v, str) and v.strip():
                return _one_line(v)
        return _one_line(json.dumps(args, ensure_ascii=False)) if args else ""
    return _one_line(str(args))


def _message_line(tool_input: str) -> str:
    """`message` 호출 → ``✉ message → <to> (request|notice) <첫 줄>``. 모양이 아니면 ""."""
    try:
        args = json.loads(tool_input) if tool_input else {}
    except (TypeError, ValueError):
        return ""
    if not isinstance(args, dict) or not args.get("to"):
        return ""
    notice = str(args.get("expects_reply", True)).lower() in ("false", "0", "no")
    kind = "notice" if notice else "request"
    return f"✉ message → {args['to']} ({kind}) {_one_line(str(args.get('text') or ''))}"


class MinimalRenderer(Renderer):
    """Clean indented output with icons, no boxes or color-dependent structure.

    Supports nested rendering for skills/delegates via push_depth/pop_depth.
    Each depth level adds a "│ " prefix to all output.
    """

    def __init__(self, console: Console):
        super().__init__()
        self.con = console
        self._live: Live | None = None
        # Marquee resize-recovery state: terminal width and total cols
        # written by the last `stream_chunk`. When the terminal is
        # resized smaller mid-stream, the previous paint reflows onto
        # multiple lines that `\r` alone can't reach. Tracking the prior
        # paint lets us erase exactly those reflowed lines on the next
        # chunk. Reset to 0 in `stream_end`.
        self._last_term_w: int = 0
        self._last_painted_w: int = 0
        # Marquee counters (~N tokens · 🧠 N · 🔧 N · talking frame). Eager, not
        # lazy: ``stream_end`` resets the buffers even when no chunk ever
        # arrived (a tool-call-only response), and a lazy ``hasattr``
        # guard keyed on ``_stream_buf`` then skipped ``_last_frame_time``
        # — the next ``stream_chunk`` raised AttributeError and killed
        # the LLM call (v10.25.1).
        self._stream_buf = ""
        self._stream_chunks = 0
        self._think_buf = ""
        self._tool_buf = ""  # v10.70.0: 호출 조각(native_fc) 채널
        self._last_frame_time = 0.0
        # 에이전트 줄 (docs/agent-engine/DESIGN.md §6) — 에이전트 스레드 → 그 키와 지금
        # 실행 중인 도구 호출 하나. `begin_agent_work`/`end_agent_work` 가 쌍으로 쓴다.
        self._agent_lines: dict[int, dict] = {}
        self._agent_lock = threading.Lock()
        # 직전 장부 스냅샷 (v10.119.0) — 같은 것이 연달아 오면 줄을 다시 찍지 않는다.
        self._last_open_items: dict | None = None
        # 마지막으로 찍은 제자리 고리 발화 수 (v10.120.0) — 발화마다 한 줄, N 갱신엔 안 찍는다.
        self._last_stale_fires = 0

    @property
    def _prefix(self) -> str:
        """Depth-based prefix for nested rendering.

        Depth 0: no prefix (content has its own 2-space indent).
        Depth 1+: │ at column 0, aligned with ┌─/└─ group brackets.
        """
        if self._depth == 0:
            return ""
        return "│ " * self._depth

    def _p(self, text: str, **kwargs) -> None:
        """Print with depth prefix. Captures to buffer if in capture mode."""
        line = f"{self._prefix}{text}"
        # 캡처본은 화면과 같은 글자 — markup=False 줄은 그대로, 마크업 줄은 rich 가
        # 그리는 평문(이스케이프한 `\[u-1]` 은 `[u-1]` 로 남는다). 종전 정규식은
        # 대괄호 덩어리를 전부 지워 JSON 목록·항목 id 까지 날렸다.
        if kwargs.get("markup") is False:
            clean = line
        else:
            clean = Text.from_markup(line).plain
        if self._capture_line(clean):
            return
        self.con.print(line, **kwargs)

    def header(
        self,
        provider: str,
        model: str,
        max_turns: int,
        skill_name: str = "",
        skill_args: str = "",
    ) -> None:
        # Skip header for nested calls (depth>0) or parallel delegates (capture mode).
        # Each AgentLoop calls render_header in _setup(), but only the main loop
        # should show the banner.
        if self.in_nested_render():
            return
        self.con.print()
        if skill_name:
            args_label = f"({skill_args})" if skill_args else ""
            self.con.print(
                f"  ● skill:{skill_name}{args_label}  "
                f"[{_MUTED}]{provider} · {model}[/]",
                highlight=False,
            )
        else:
            iter_label = str(max_turns) if max_turns > 0 else "∞"
            self.con.print(
                f"  ● agent-cli  "
                f"[{_MUTED}]{provider} · {model} · max_turns={iter_label}[/]",
                highlight=False,
            )
        self.con.print()

    def turn_sep(self, turn: int) -> None:
        # No-op: turn number is already shown in token stats line
        # (e.g. "● ttft: 200ms | in: 1024 | out: 156  turn 1").
        pass

    def _render_markdown(self, icon: str, content: str) -> None:
        """Render markdown content with icon, respecting depth prefix."""
        buf = StringIO()
        temp = Console(file=buf, width=max(self.con.width - len(self._prefix) - 4, 40))
        temp.print(Markdown(content), highlight=False)
        rendered = buf.getvalue().rstrip("\n")
        lines = rendered.split("\n")
        self._p(f"  {icon} {lines[0]}", highlight=False)
        for line in lines[1:]:
            self._p(f"     {line}", highlight=False)

    def thought(self, content: str, turn: int) -> None:
        # Record for an interactive prompt header (who/why behind a confirm).
        self.note_thought(content)
        # Update live status (first line of thought, shown in parallel progress panel)
        first_line = content.strip().split("\n", 1)[0]
        self.set_thread_status(f"💭 {first_line}")
        self._p("")
        self._render_markdown("💭", content)
        # No trailing blank — let the action/observation pair visually below

    def action(self, tool_name: str, tool_input: str, turn: int) -> None:
        self.note_action(tool_name, tool_input)
        self._note_agent_step(tool_name, tool_input)
        if tool_name == "message" and not self.is_capturing:
            # ``✉ message → agt-3 (notice) Scope change: …`` (v10.141.0, 설계 §6)
            line = _message_line(tool_input)
            if line:
                self._p(f"  {line}", highlight=False, markup=False)
                return
        display = tool_input[:200] + "..." if len(tool_input) > 200 else tool_input
        self._p(f"  ⚡ {tool_name} → {display}", highlight=False, markup=False)

    def observation(
        self,
        content: str,
        turn: int,
        tool_name: str | None = None,
        success: bool = True,
        images: list[dict] | None = None,
        item: str = "",
    ) -> None:
        # ``images`` 는 쓰지 않는다 — 콘솔은 content 의 자리표시 줄을 🖼 로 찍는다. ``item`` 도(웹의 카드 찾기용).
        self._print_agent_step(tool_name, success)
        status = "success" if success else "error"
        icon = "✓" if success else "✗"
        tool_label = f" {tool_name}" if tool_name else ""

        detail = ""
        if not success:
            for line in content.split("\n"):
                if line.startswith("ERROR:"):
                    detail = f"  {line}"
                    break

        self._p(f"  {icon}{tool_label}  {status}{detail}", highlight=False)

        # 이미지 관찰 (v10.59.0): 자리표시 줄 `[image: path (W×H)]` 을 한 줄로.
        for line in content.split("\n"):
            if is_placeholder_line(line):
                self._p(f"  🖼 {line[len('[image: ') : -1]}", highlight=False)

        # `write_file` / `edit_file` append a plain unified diff to the
        # observation (see agent_cli.tools._diff.format_diff). The summary
        # above only conveys "what tool ran"; the diff is what tells the
        # user *what changed*. It's preceded by a blank line and starts
        # with a `--- a/...` header — colour is applied per line here
        # (the diff text itself stays plain in the LLM observation).
        diff_idx = content.find("--- a/")
        if diff_idx != -1:
            diff_block = content[diff_idx:].rstrip("\n")
            for line in diff_block.split("\n"):
                self._p(f"     {_colorize_diff_line(line)}", highlight=False)

    def final(self, content: str, turn: int, requests: list | None = None) -> None:
        self._p("")
        self._render_markdown("✅", content)
        for r in requests or []:
            who = f" ({r['author']})" if r.get("author") else ""
            text = (r.get("text") or "").strip().replace("\n", " ")
            if len(text) > 72:
                text = text[:71] + "…"
            # id 는 `u-1`·`n-1` 형식(v10.56.0) — 이스케이프하지 않으면 rich 가
            # `[u-1]` 을 스타일 태그로 읽어 지운다(옛 `[1]` 은 태그가 아니라 살았다).
            body = escape(f"[{r.get('id')}]{who} {text}")
            self._p(f"  [{_MUTED}]↳ answers {body}[/]")
        self._p("")

    def error(self, content: str, turn: int, *, failed: bool = True) -> None:
        self._p(f"  {'✗' if failed else '●'} {content}", highlight=False)

    def open_items(self, snapshot: dict) -> None:
        """열린 항목 한 줄 ``open: u-2 n-1 r-1`` (v10.119.0, docs/ledger/DESIGN.md
        §15) — 턴 경계마다 사용량 줄 뒤에 온다. 항목이 없거나 직전과 같은 스냅샷이면
        찍지 않는다. id 는 대괄호 없이 공백으로 잇고 이스케이프한다 — rich 가
        ``[u-1]`` 을 스타일 태그로 삼킨 사고(v10.56.1)의 재발 방지."""
        if snapshot == self._last_open_items:
            return
        self._last_open_items = snapshot
        ids = [str(it.get("id")) for it in snapshot.get("items") or []]
        if ids:
            self._p(f"  [{_MUTED}]{escape('open: ' + ' '.join(ids))}[/]")

    def _note_auto_approved(self, text: str) -> None:
        self.con.print(text, highlight=False)  # ⚡ — 터미널에도 물을 사람이 없다

    def stale_loop(self, info: dict) -> None:
        """제자리 고리 한 줄 ``🔁 no new results for N turns`` (v10.120.0) — 발화마다 흐리게.
        묻지 않는다(``run`` 은 사람 없이 끝난다). 발화 뒤 N 이 느는 갱신과 지움(0)은 찍지
        않고 발화 수만 따라간다. escape 는 `open_items` 줄과 같은 이유."""
        n = int(info.get("turns_without_new") or 0)
        fires = int(info.get("fires") or 0)
        if n <= 0:
            self._last_stale_fires = 0
            return
        if fires == self._last_stale_fires:
            return
        self._last_stale_fires = fires
        self._p(f"  [{_MUTED}]{escape(f'🔁 no new results for {n} turns')}[/]")

    def agent_mail_hint(self, *, key: str, kind: str, text: str) -> None:
        """main 앞 메일 도착 한 줄 — ``↩ reply r-21 ← agt-3 …``·``⏳ Queued …``. 알림은 보낸
        에이전트의 스레드에서 오고 그 스레드의 출력은 캡처해 버리므로(에이전트 줄), 캡처를 거치지
        않고 바로 찍는다 — 거치면 회신 줄이 통째로 사라진다."""
        self.con.print(f"  {text}", markup=False, highlight=False, soft_wrap=True)

    def agent_stopped(
        self, *, key: str, how: str, notified: list[tuple[str, str]], kept: int
    ) -> None:
        """``■ agt-3 │ stopped by user — reply sent to main (r-21), agt-6 (r-31); 1 queued request kept``
        (v10.142.0, 설계 §6). 정지는 에이전트 스레드에서도 오므로 캡처를 거치지 않는다(`agent_mail_hint`)."""
        self.con.print(
            f"  ■ {key} │ {stop_summary(how, notified, kept)}",
            markup=False,
            highlight=False,
            soft_wrap=True,
        )

    def wait_in_place(
        self, waiting: bool, awaited: list[dict], others: list[str] | None = None
    ) -> None:
        """``⏳ waiting for N reply (r-21), job #1 — type to steer…`` 한 줄 (v10.141.0, 설계 §6; 작업·모니터·예약은
        5c-1 결정 C) — main 이 제자리 대기에 들어갈 때만. 깰 때는 찍지 않는다(다음 줄이 그 이유를 말한다)."""
        parts = list(others or [])
        if awaited:
            n = len(awaited)
            ids = ", ".join(str(r.get("id")) for r in awaited)
            parts.insert(0, f"{n} repl{'ies' if n != 1 else 'y'} ({ids})")
        if not waiting or not parts:
            return
        text = (
            f"⏳ waiting for {', '.join(parts)} — type to steer "
            "or give new work, Enter to send"
        )
        self._p(f"  [{_MUTED}]{escape(text)}[/]")

    def push_user_message(
        self,
        content: str,
        author: str = "",
        hidx: int | None = None,
        images: list[dict] | None = None,
    ) -> None:
        """런 도중 합류한 요청(예약 발화 등)의 에코 (v10.33.0) — run 도 web
        처럼 턴 경계에서 큐를 주입하므로, 합류한 사실이 콘솔에도 보여야 한다.
        런 시작 질의는 호출자가 이미 보여 주므로 여기 오지 않는다. 딸린
        이미지는 `🖼` 줄로 (v10.60.0 — 주입 메시지도 이미지를 실을 수 있다)."""
        self._p(f"  📨 {content}", highlight=False, markup=False)
        for ref in images or []:
            self._p(
                f"  🖼 {ref.get('path')} ({ref.get('width')}×{ref.get('height')})",
                highlight=False,
                markup=False,
            )

    def status(self, state: str, message: str, turn: int = 0) -> None:
        it = f"  turn {turn}" if turn else ""
        self._p(f"  ● {message}{it}", highlight=False)

    def stream_reset(self) -> None:
        """마르퀴 누적(~N tokens · 🧠 N · 🔧 N)을 0 으로 되돌리고 줄을 지운다 —
        재전송 후 카운터가 옛 시도분을 이어 세면 실제보다 부풀어 보인다."""
        self._stream_buf = ""
        self._stream_chunks = 0
        self._think_buf = ""
        self._tool_buf = ""
        if self.is_capturing or not self.con.file:
            return
        self._erase_reflowed_marquee()
        self.con.file.write(f"\r{' ' * self.con.width}\r")
        self.con.file.flush()
        self._last_painted_w = 0

    def stream_stall(
        self,
        *,
        kind: str,
        elapsed_s: float = 0.0,
        limit_s: float = 0.0,
        attempt: int = 1,
        attempts: int = 1,
    ) -> None:
        """무진전 대기는 마르퀴와 **같은 한 줄 슬롯**에서 제자리 갱신하고,
        재전송(전이)만 기록으로 남긴다 (v8.60.0). 종전엔 대기 알림이
        시도당 20줄씩 쌓여 재전송·실패가 그 사이에 묻혔다.

        비-TTY(harbor·파이프·instance.log)에선 ``\\r`` 갱신이 무의미하고
        줄이 남는 편이 사후 진단에 유리하므로 **기본 구현(줄 출력)** 그대로
        — 사람이 보는 표면에서만 제자리로 바꾼다."""
        if self.is_capturing or not self.con.is_terminal or not self.con.file:
            super().stream_stall(
                kind=kind,
                elapsed_s=elapsed_s,
                limit_s=limit_s,
                attempt=attempt,
                attempts=attempts,
            )
            return
        if kind == "wait":
            left = max(0, int(limit_s) - int(elapsed_s))
            prefix = f"{self._prefix}  " if self._depth > 0 else "  "
            line = (
                f"{prefix}⏳ Waiting for a response {_fmt_mmss(elapsed_s)}"
                f" / {_fmt_mmss(limit_s)} · attempt {attempt}/{attempts}"
            )
            if left <= 0:
                line += " · reconnecting"
            avail = self.con.width - 1
            if _display_width(line) > avail:
                line = _truncate_to_width(line, avail)
            self._erase_reflowed_marquee()
            self.con.file.write(f"\r{line}")
            pad = max(0, self.con.width - _display_width(line) - 1)
            self.con.file.write(" " * pad)
            self.con.file.flush()
            self._last_term_w = self.con.width
            self._last_painted_w = _display_width(line) + pad
        else:
            # 전이·정리 — 제자리 줄을 먼저 지운 뒤에 기록을 쓴다. 지우지
            # 않으면 잔여 문자가 다음 줄 앞에 남는다(폭이 더 긴 경우).
            self._erase_reflowed_marquee()
            self.con.file.write(f"\r{' ' * self.con.width}\r")
            self.con.file.flush()
            self._last_painted_w = 0
            if kind == "resend":
                self._p(
                    f"  ↻ Stream stalled — reconnected and resent (attempt {attempt}/{attempts})",
                    highlight=False,
                )

    def token_usage(self, stats: dict, turn: int, verbose: bool = False) -> None:
        msg = _format_token_stats(stats)
        if not msg:
            return
        if not verbose:
            msg += "  (--verbose records raw responses to verbose.jsonl)"
        self.status("running", msg, turn)

    def usage_totals(self, totals) -> None:
        self._p(f"  ● {format_usage_totals(totals)}", highlight=False)

    def model_detected(
        self, model: str, capabilities, provider: str, saved_path: str
    ) -> None:
        yes, no = "✓", "✗"
        thinking_info = yes if capabilities.supports_thinking else no
        self.con.print()
        self.con.print("  ● Model Detected", highlight=False)
        self.con.print(f"    {model} ({provider})", highlight=False)
        self.con.print(
            f"    context={capabilities.context_window:,}  "
            f"output={capabilities.max_output_tokens:,}  "
            f"thinking={thinking_info}",
            highlight=False,
        )
        self.con.print(f"    [{_MUTED}]saved to {saved_path}[/]")
        self.con.print()

    def model_loaded(self, model: str, capabilities) -> None:
        yes, no = "✓", "✗"
        thinking = (
            f"thinking={yes}" if capabilities.supports_thinking else f"thinking={no}"
        )
        self.con.print(
            f"  ● {model} (ctx={capabilities.context_window:,}, {thinking})",
            highlight=False,
        )

    def spinner_start(self, message: str = "") -> None:
        if self.is_capturing:
            return  # No spinner in capture mode
        if self._live is not None:
            return  # Already spinning
        try:
            prefix = self._prefix
            # `_THINK_FRAMES` are self-describing (face + thought bubble
            # progression), so a `message` is optional — only prepended
            # when callers want to add context (e.g. "loading model").
            # `FrameClock` enforces the shared `_FRAME_INTERVAL` cadence.
            clock = FrameClock(_THINK_FRAMES)

            def get_renderable():
                msg = f"{message} " if message else ""
                return Text(f"{prefix}  {msg}{clock.current()}", style=_MUTED)

            self._live = Live(
                get_renderable(),
                console=self.con,
                refresh_per_second=10,
                transient=True,
                get_renderable=get_renderable,
            )
            self._live.start()
        except Exception:
            self._live = None  # Graceful fallback in non-TTY environments

    def spinner_stop(self) -> None:
        if self._live is not None:
            try:
                self._live.stop()
            except Exception:
                pass
            self._live = None

    def begin_scope(
        self,
        *,
        task_id: str,
        kind: str = "run",
        label: str = "",
        agent: str = "",
        index: int = 0,
        parent: str | None = None,
        ts: float | None = None,
        ctx_dir: str | None = None,
    ) -> None:
        """Enter a nested scope. Same abstraction surface as web; CLI display
        adapts to ``kind`` — a skill draws a ``┌─ 🪄`` bracket at the current
        depth (caller then push_depth); a harness job captures its thread and
        prints one line at the end. Agents (run·resident) do not come here —
        they print agent lines (`begin_agent_work`).

        ``parent``/``ts``/``ctx_dir`` are accepted and ignored: the CLI already encodes
        nesting as depth indentation (``_prefix``) and prints in arrival order,
        so it needs neither the parent link nor a shared batch timestamp. They
        exist for the web surface's slot layout (see ``Renderer.begin_scope``)."""
        if not hasattr(self, "_scope_labels"):
            self._scope_labels: dict[str, str] = {}
        self._scope_labels[task_id] = label
        if kind == "skill":
            self._p(f"┌─ 🪄 {label}", highlight=False)
        elif kind == "harness":
            # 하니스 작업(감사, v10.53.0): main 과 따로 도는 스레드라 흘리면 main
            # 의 출력처럼 섞인다 — 상주 에이전트처럼 모아 버리고 끝에 한 줄.
            self._scope_labels[task_id] = f"{agent} · {label}" if agent else label
            self.start_capture()
            self.set_thread_agent(agent or label)

    def end_scope(
        self,
        *,
        task_id: str,
        kind: str = "run",
        success: bool = True,
        duration_s: float = 0.0,
        error: str = "",
    ) -> None:
        """Leave a scope. Skill prints ``└─ ✓`` (call AFTER pop_depth); a harness
        job prints its one line."""
        label = getattr(self, "_scope_labels", {}).pop(task_id, "")
        if kind == "skill":
            status = "✓" if success else "✗"
            dur = f" ({duration_s:.1f}s)" if duration_s > 0 else ""
            self._p(f"└─ {status} {label}{dur}", highlight=False)
        elif kind == "harness":
            self.stop_capture()  # 전문은 <session>/harness/<job>/<label>/ 에
            self.set_thread_agent("")
            status = "✓" if success else "✗"
            dur = f" ({duration_s:.1f}s)" if duration_s > 0 else ""
            self._p(f"🔎 {label} {status}{dur}", highlight=False)

    def _erase_reflowed_marquee(self) -> None:
        """If the terminal shrank since the last paint, the previous
        paint's content has been retroactively wrapped across multiple
        lines and a bare `\\r` only reaches the bottom one. Compute how
        many lines that paint now occupies at the new width and erase
        them all (current line + N-1 lines above) so the next paint
        starts on a clean line where the original paint began.

        Safe to call when no resize happened — falls through.
        """
        if not (self._last_term_w and self._last_painted_w):
            return
        new_w = self.con.width
        if new_w <= 0 or new_w == self._last_term_w:
            return
        # Ceil division: how many `new_w`-wide rows the prior paint now
        # occupies after the terminal's reflow.
        wrap_count = max(1, (self._last_painted_w + new_w - 1) // new_w)
        if wrap_count <= 1:
            return  # widened (or no change) — `\r` + pad still cleans up
        f = self.con.file
        for _ in range(wrap_count - 1):
            f.write("\r\x1b[K\x1b[1A")
        f.write("\r\x1b[K")

    def stream_chunk(self, text: str) -> None:
        if self.is_capturing:
            # Skip streaming in capture mode (parallel delegates).
            # The talking-face progress indicator is for live TTY only.
            return
        self._stream_buf += text
        self._paint_marquee()

    def thinking_chunk(self, text: str) -> None:
        # P5 (v8.56.0): 사고 델타 — 같은 마르퀴 줄의 🧠 카운터로만 표시.
        # content 없이 사고만 이어지는 러너웨이가 무음이 되지 않게 한다.
        if self.is_capturing:
            return
        self._think_buf += text
        self._paint_marquee()

    def tool_call_chunk(self, text: str) -> None:
        # v10.70.0: 호출 조각(native_fc) — 같은 줄의 🔧 카운터. 긴 호출을 쓰는
        # 동안 ~0 tokens 로 멈춘 듯 보이던 것을 메운다.
        if self.is_capturing:
            return
        self._tool_buf += text
        self._paint_marquee()

    def _paint_marquee(self) -> None:
        import time

        # Frame advancement is time-throttled so multi-char talking
        # frames stay readable. The counter (below) still updates on
        # every chunk, so the user still sees activity even between
        # frame ticks.
        now = time.monotonic()
        if now - self._last_frame_time >= _FRAME_INTERVAL:
            self._stream_chunks += 1
            self._last_frame_time = now
        if self.con.file:
            self._erase_reflowed_marquee()
            prefix = f"{self._prefix}  " if self._depth > 0 else "  "
            frame = _TALK_FRAMES[self._stream_chunks % len(_TALK_FRAMES)]
            # v10.70.0: 앞 숫자는 세 채널(본문·사고·호출)의 합 = "생성 중" 의 크기,
            # 괄호는 0 이 아닌 사고·호출만. 전부 본문이면 괄호가 없다(종전 모양).
            think = estimate_tokens(self._think_buf) if self._think_buf else 0
            tool = estimate_tokens(self._tool_buf) if self._tool_buf else 0
            tokens = estimate_tokens(self._stream_buf) + think + tool
            line = f"{prefix}{frame} ~{tokens} tokens"
            parts = ([f"🧠 {think}"] if think else []) + (
                [f"🔧 {tool}"] if tool else []
            )
            if parts:
                line += f" ({' · '.join(parts)})"
            # Narrow-terminal safety net: if frame+counter wouldn't fit,
            # drop the counter; if even the face wouldn't fit, truncate.
            # Keeps the indicator from ever wrapping onto a new line.
            avail = self.con.width - 1
            if _display_width(line) > avail:
                line = f"{prefix}{frame}"
                if _display_width(line) > avail:
                    line = _truncate_to_width(line, avail)
            self.con.file.write(f"\r{line}")
            pad = max(0, self.con.width - _display_width(line) - 1)
            self.con.file.write(" " * pad)
            self.con.file.flush()
            self._last_term_w = self.con.width
            self._last_painted_w = _display_width(line) + pad

    def stream_end(self) -> None:
        self._stream_buf = ""
        self._stream_chunks = 0
        self._think_buf = ""
        self._tool_buf = ""
        if self.is_capturing:
            return
        if self.con.file:
            # Resize may have left reflowed remnants — clean those up
            # before the single-line clear below.
            self._erase_reflowed_marquee()
            self.con.file.write(f"\r{' ' * self.con.width}\r")
            self.con.file.flush()
            self._last_term_w = 0
            self._last_painted_w = 0

    # ── User input ──────────────────────────────────

    def prompt_user(
        self,
        prompt: str,
        *,
        default: str = "",
        multiline: bool = True,
        continuation: str = "... ",
        context: str = "",
    ) -> str:
        """CLI implementation — readline + paste detection for multi-line,
        single ``input()`` call for one-line prompts.

        ``EOFError`` / ``KeyboardInterrupt`` propagate to the caller —
        different consumers want different policy (the setup wizard
        aborts back to caller, the ask tool substitutes
        ``"(no response)"``), so the renderer doesn't impose one. Empty
        input → ``default`` substitution.

        Decode errors get swallowed by ``read_rich_input`` (one-shot
        warning + empty) so the user can retype without aborting.

        ``context`` is ignored — callers that announce pre-input text
        (e.g. ``_handle_ask``'s coloured question block) print it
        directly via ``console.print`` so Rich markup survives.
        """
        del context  # CLI prints announcements separately for color.
        # Reads go through ``_guarded_read`` → serialized against other
        # prompts (confirm / concurrent delegate asks) and wrapped in
        # ``_prompt_display_guard`` so an active Live panel doesn't paint
        # over the prompt. EOF / KbInt propagate (see docstring). The
        # provenance header (who/why) prints inside the guard so it lands
        # after the Live panel is paused.
        if multiline:
            from agent_cli.input_history import read_rich_input

            def _read():
                self._emit_prompt_meta_header(include_action=False)
                return read_rich_input(prompt, continuation=continuation)

            value = self._guarded_read(_read).strip()
        else:

            def _read():
                self._emit_prompt_meta_header(include_action=False)
                return input(prompt)

            try:
                value = self._guarded_read(_read).strip()
            except UnicodeDecodeError:
                # Mirror read_rich_input's behaviour: drop the broken
                # paste and let the caller treat it as no input. EOF /
                # KbInt deliberately not caught — see method docstring.
                value = ""
        return value if value else default

    def _emit_prompt_meta_header(self, *, include_action: bool) -> None:
        """Print the who/why header for a delegate-originated prompt (no-op
        for the main agent or when there's nothing to show). ``markup=False``
        so a bracketed agent label like ``[code-analyst]`` isn't parsed as Rich
        markup."""
        header = self._format_prompt_meta(include_action=include_action)
        if header:
            self.con.print(header, markup=False, highlight=False)

    def can_prompt(self) -> bool:
        """A CLI prompt needs a real terminal on both ends. The active
        Live region is paused by ``_prompt_display_guard`` and reads are
        serialized by the shared lock, so a TTY is the only precondition —
        Live state and worker-thread origin are handled at read time, not
        gated here."""
        try:
            return bool(sys.stdin.isatty() and sys.stdout.isatty())
        except Exception:
            return False

    @contextmanager
    def _prompt_display_guard(self):
        """Pause an active Live region (the spinner)
        for the duration of a user read, then resume — otherwise Rich's
        repaint loop overwrites the prompt and the user sees a frozen panel
        with no question. Used by both ``confirm`` and ``prompt_user`` via
        ``_guarded_read``."""
        live = self._live
        paused = False
        if live is not None:
            try:
                live.stop()
                paused = True
            except Exception:
                paused = False
        try:
            yield
        finally:
            if paused:
                try:
                    live.start()
                except Exception:
                    pass

    @staticmethod
    def _danger_command_text(command: str, danger_spans) -> Text:
        """Build the ``  $ <command>`` line with each dangerous ``(start, end)``
        range styled bold-red. Offsets in ``danger_spans`` index into
        ``command``; the leading ``  $ `` prefix shifts them by its length. Out-
        of-range / overlapping ranges are skipped defensively so a bad span
        never corrupts the display."""
        prefix = "  $ "
        text = Text(prefix + command, style=_MUTED)
        base = len(prefix)
        pos = 0
        for start, end in danger_spans or ():
            if start < pos or start >= end or end > len(command):
                continue  # out of order / out of range — skip, don't corrupt
            text.stylize("bold red", base + start, base + end)
            pos = end
        return text

    def confirm(
        self,
        prompt: str,
        options: list[ConfirmOption],
        *,
        default_key: str,
        command: str | None = None,
        danger_spans: list[tuple[int, int]] | None = None,
    ) -> tuple[str, str]:
        """CLI implementation — read one line and match the first token
        case-insensitively against each option's ``key`` and aliases.

        Empty / EOF / Ctrl+C → ``(default_key, "")``. Unrecognized first
        token → ``(default_key, full_raw_line)`` so the user's typed
        intent is preserved as a comment (e.g. shell danger prompt
        surfaces "I don't trust this" as a comment alongside the
        implicit deny).

        The read goes through ``_guarded_read`` → serialized against other
        prompts and wrapped in ``_prompt_display_guard`` so an active Live
        panel (parallel-delegate worker context) doesn't paint over it. A
        provenance header (agent + reasoning + action) prints first for
        delegate-originated confirms, and when ``command`` is supplied it is
        printed on its own line with the dangerous tokens highlighted.
        """

        def _read():
            self._emit_prompt_meta_header(include_action=True)
            if command is not None:
                self.con.print(
                    self._danger_command_text(command, danger_spans),
                    highlight=False,
                )
            return input(prompt)

        try:
            raw = self._guarded_read(_read).strip()
        except (EOFError, KeyboardInterrupt):
            return (default_key, "")
        if not raw:
            return (default_key, "")

        parts = raw.split(maxsplit=1)
        first_lower = parts[0].lower()
        comment = parts[1].strip() if len(parts) > 1 else ""

        for opt in options:
            candidates = (opt.key.lower(), *(a.lower() for a in opt.aliases))
            if first_lower in candidates:
                return (opt.key, comment)

        # Unrecognized — preserve the full raw input as comment so the
        # user's intent surfaces upstream (e.g. tool result observation).
        return (default_key, raw)

    # ── 에이전트 줄 (docs/agent-engine/DESIGN.md §6) ─────────────
    # 에이전트(run·상주)는 자기 스레드에서 main 과 동시에 돈다. 그 스레드의 평소 출력은
    # 캡처해 버리고(전문은 그 에이전트의 세션 디렉터리에), 런의 시작·끝과 끝난 도구 호출마다
    # 한 줄씩 **오는 순서대로** 섞어 찍는다 — 모았다가 통째로 찍지 않는다:
    #   ``  agt-3a1b2c4d │ shell  rg -n parse_tokens src/  ✓``

    def begin_agent_work(
        self,
        *,
        key: str,
        item: str,
        profile: str,
        message: str,
        req_ts: float | str | None = None,
        items: list[str] | None = None,
        caller_task: str | None = None,
    ) -> None:
        self.start_capture()
        self.set_thread_agent(profile or key)
        with self._agent_lock:
            self._agent_lines[threading.get_ident()] = {"key": key, "step": None}
        who = f"{profile} · " if profile else ""
        self._print_agent_line(key, f"▶ {who}{_one_line(message)}")

    def end_agent_work(
        self,
        *,
        key: str,
        item: str,
        success: bool,
        duration_s: float,
        error: str = "",
        stopped: bool = False,
    ) -> None:
        self.stop_capture()  # 캡처 폐기 — 전문은 에이전트 세션 디렉토리에
        self.set_thread_agent("")
        with self._agent_lock:
            self._agent_lines.pop(threading.get_ident(), None)
        dur = f" ({duration_s:.1f}s)" if duration_s > 0 else ""
        if stopped:
            self._print_agent_line(key, f"■ stopped{dur}")
            return
        tail = "done" if success else (_one_line(error) or "failed")
        self._print_agent_line(key, f"{'✓' if success else '✗'} {tail}{dur}")

    def _note_agent_step(self, tool_name: str, tool_input: str) -> None:
        """에이전트 스레드면 지금 시작한 도구 호출을 적어 둔다 — 결과가 오면 한 줄이 된다."""
        with self._agent_lock:
            line = self._agent_lines.get(threading.get_ident())
            if line is not None:
                line["step"] = (tool_name, _step_summary(tool_input))

    def _print_agent_step(self, tool_name: str | None, success: bool) -> None:
        """에이전트 스레드의 도구 결과 → ``key │ 도구  요약  ✓`` 한 줄(캡처를 거치지 않는다)."""
        with self._agent_lock:
            line = self._agent_lines.get(threading.get_ident())
            if line is None:
                return
            name, summary = line["step"] or (tool_name or "", "")
            line["step"] = None
            key = line["key"]
        if not name:
            return
        body = f"{name}  {summary}" if summary else name
        self._print_agent_line(key, f"{body}  {'✓' if success else '✗'}")

    def _print_agent_line(self, key: str, text: str) -> None:
        self.con.print(
            f"  {key} │ {text}", markup=False, highlight=False, soft_wrap=True
        )

    def agent_message(
        self,
        *,
        key: str,
        direction: str,
        author: str,
        text: str,
        item: str = "",
        success: bool = True,
        to: str = "main",
        ts: float | str | None = None,
        profile: str = "",
        instance_name: str = "",
        fallback: bool = False,
        absorbed_into: str | None = None,
    ) -> None:
        """CLI 의 teammate 문답 수신 창 — @agt-<key> 명령(사용자 발신)의
        회신/질문은 main 관찰로 배달되지 않으므로(D8) 콘솔 라인이 유일한
        수신 표면이다. main 발신 문답은 관찰 배달과 이중 표시되지 않게
        건너뛴다."""
        # "absorbed"(v10.8.2) 는 웹의 대기 줄 정리 신호 — 터미널은 흡수된 본문을
        # 관찰로 이미 찍는다. "restarted"(v10.165.0) 는 웹 채널의 재시작 줄 — 터미널은 그리지 않는다.
        if to == "main" or direction in ("in", "absorbed", "restarted"):
            return
        from agent_cli.agent_icon import agent_display_name, agent_icon

        # 이름을 앞에, 주소(key)를 뒤에 — 웹 채널 칩과 같은 배치. 터미널엔
        # 흐리게 할 자리가 없으니 괄호로 눌러 둔다. v9.9.0.
        label = f"{agent_display_name(key, profile, instance_name)} ({key})"
        if direction == "question":
            self.question_line(label, item, text, to=to)
            return
        base_icon = agent_icon(key)
        icon = base_icon if success else base_icon + "✗"
        self.con.print(f"\n{icon} [{label} → {to}]", style="bold cyan", markup=False)
        self.con.print(text, highlight=False, markup=False)

    def question_line(
        self, asker: str, qid: str, text: str, *, to: str = "user"
    ) -> None:
        """사람에게 건 질문 한 줄 — 번호와 답·닫는 법을 단다(6c·6b). 터미널의 답은 그 번호를 적은 `/answer`,
        닫기는 `/close` 하나다. 에이전트의 질문(대화 창 줄)과 main 의 질문이 같은 모양이다."""
        self.con.print(f"\n❓ [{asker} → {to}] {qid}", style="bold cyan", markup=False)
        self.con.print(text, highlight=False, markup=False)
        self.con.print(
            f"  answer with: /answer {qid} <text> · close with: /close {qid}",
            style=_MUTED,
            markup=False,
        )

    # ── Ask-tool announcement ────────────────────────

    def announce_ask(self, questions: list[str], *, prefix: str = "") -> None:
        """Print the ``Agent asks:`` header + each question in colour.

        Mirrors the plain-text version that ``loop.py`` passes to
        ``prompt_user(context=...)`` for web surfaces; the CLI needs
        the spelled-out version because terminals don't echo the
        prompt context back to the user.
        """
        # Local import to keep `C` palette resolution lazy — the
        # render/__init__ module sets up `_renderer` at import time
        # and we don't want a hard circular dependency.
        from agent_cli.render import C

        accent = C["accent"]
        self.con.print(f"{prefix}\n{prefix}[{accent}]Agent asks:[/]")
        if len(questions) > 1:
            for i, q in enumerate(questions, 1):
                self.con.print(f"{prefix}  {i}. {q}")
        else:
            self.con.print(f"{prefix}  {questions[0]}")
