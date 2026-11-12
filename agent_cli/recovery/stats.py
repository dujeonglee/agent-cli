"""``agent-cli stats`` — 본문 없는 집계 내보내기 (v10.130.0).

``turns.jsonl`` (``observability.TurnRecorder`` 가 쓰는 것)만 읽어 숫자로 접는다.
용도: 다른 머신(회사)의 실사용 기록에서 **숫자만** 들고 나오기. 그래서 계약이 이 모듈의
존재 이유다 —

- 여는 파일은 이름이 정확히 ``turns.jsonl`` 인 파일뿐이다. 심볼릭 링크는 따라가지
  않는다(디렉터리도 파일도). ``history.jsonl``·``failures.jsonl``·verbose 는 열지 않는다.
- 출력의 문자열은 닫힌 어휘뿐: 하니스 버전(숫자 모양), 방언·내장 도구 이름(코드의
  레지스트리), ``parse_stage``·``failure_signal``·``stop_reason``·``stop_detail``·
  ``summary_shape``·``shape_fallback`` 의 알려진 값(정의 자리의 상수), 그리고 모델 이름
  (모델별 비교가 목적이라 그대로 — 단 경로 모양이면 마지막 요소만, 한 줄·120자).
  어휘 밖 값은 전부 ``"other"``.
- 경로·작업 디렉터리·세션 id·제목은 내지 않는다. 날짜는 일 단위(첫날·끝날)만.

그룹 키는 ``harness_version × model × dialect`` — 키가 없는 옛 줄은 ``"unknown"``.
압축 줄에는 모델이 없어 같은 파일에서 직전 턴 줄의 모델을 쓴다(없으면 ``"unknown"``).
못 잰 값(키 없음)은 그 항목의 분모에서 빠지고, 항목마다 ``n`` 이 함께 나간다.
"""

from __future__ import annotations

import json
import math
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from agent_cli import __version__
from agent_cli.constants import STOP_INCOMPLETE_TOOL_CALL, STOP_SERVER_ERROR
from agent_cli.context.manager import SHAPE_FALLBACK_KINDS, SUMMARY_SHAPES
from agent_cli.dialects import list_names as _dialect_names
from agent_cli.providers.runaway import REASON_TEXT as _RUNAWAY_REASONS
from agent_cli.recovery import observability as _obs
from agent_cli.tools.registry import _BUILTIN_ORDER

SCHEMA = 1
FILE_NAME = "turns.jsonl"
OTHER = "other"
UNKNOWN = "unknown"
NONE = "none"

#: 출력 머리의 "이 파일에 든 것" 한 줄 — 들고 나가기 전에 사용자가 확인한다.
CONTAINS = (
    "Counts only: harness versions, model names, dialect names, built-in tool "
    "names and fixed status labels. No paths, prompts, responses, tool arguments, "
    "session ids or titles; dates are days only."
)

_VERSION_RE = re.compile(r"^\d{1,4}\.\d{1,4}\.\d{1,4}$")
_DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

#: ``failure_signal`` 의 정의 자리는 observability 의 ``FAILURE_*`` 상수다.
_SIGNALS = frozenset(
    v for k, v in vars(_obs).items() if k.startswith("FAILURE_") and isinstance(v, str)
)
#: 루프가 잘린 턴에 적는 ``stop_reason`` — ``LLMResponse.stop_reason`` 의 루프 어휘
#: (providers/base.py) 중 기록되는 넷. 이 넷이 곧 "잘린 턴".
_CUT_STOPS = frozenset(
    {"length", "runaway", STOP_INCOMPLETE_TOOL_CALL, STOP_SERVER_ERROR}
)
#: 고정 어휘로 쓰이는 ``stop_detail`` — 상한 종류(classify_output_cap)·도구 호출 중
#: 끊김·폭주 감지 규칙. 서버 오류의 detail 은 서버 문구(자유 문장)라 ``other``.
_STOP_DETAILS = frozenset(
    {*_obs.OUTPUT_CAP_KINDS, STOP_INCOMPLETE_TOOL_CALL, *_RUNAWAY_REASONS, UNKNOWN}
)
_PARSE_STAGES = frozenset({0, 1, 2, 3})
#: 경로 모양의 모델 이름 — 절대(``/``)·홈(``~``)·상대(``.``)·Windows 드라이브(``X:\\``·``X:/``).
_PATH_LIKE_RE = re.compile(r"^(?:[/~.\\]|[A-Za-z]:[\\/])")
_MODEL_MAX = 120


def _is_count(v) -> bool:
    # json.loads 는 Infinity·NaN 을 받는다 — 유한한 수만 센다(합이 Infinity 가 되지 않게).
    return (
        isinstance(v, (int, float))
        and not isinstance(v, bool)
        and math.isfinite(v)
        and v >= 0
    )


def _pct(values: list[float], q: float) -> float | None:
    """가장 가까운 순위 백분위 — 값이 없으면 None."""
    if not values:
        return None
    s = sorted(values)
    return s[max(0, math.ceil(q * len(s)) - 1)]


def _dist(values: list[float]) -> dict:
    return {
        "n": len(values),
        "median": _pct(values, 0.5),
        "p90": _pct(values, 0.9),
        "sum": sum(values) if values else 0,
    }


def _bump(counter: dict, key: str) -> None:
    counter[key] = counter.get(key, 0) + 1


@dataclass
class _Tool:
    calls: int = 0
    failures: int = 0
    ms: list[float] = field(default_factory=list)


@dataclass
class _Group:
    sessions: set = field(default_factory=set)
    turns_by_file: dict = field(default_factory=dict)
    days: set = field(default_factory=set)
    turns: int = 0
    # 토큰
    tok_n: int = 0
    tok_in: int = 0
    tok_out: int = 0
    tok_cache: int = 0
    reason_n: int = 0
    reason_sum: int = 0
    reason_out: int = 0
    # 시간
    ttft: list = field(default_factory=list)
    llm: list = field(default_factory=list)
    tool: list = field(default_factory=list)
    # 결과
    parse_stage: dict = field(default_factory=dict)
    failure_signal: dict = field(default_factory=dict)
    stop_reason: dict = field(default_factory=dict)
    stop_detail: dict = field(default_factory=dict)
    cut: int = 0
    tools: dict = field(default_factory=dict)
    # 압축
    comp: int = 0
    comp_before: list = field(default_factory=list)
    comp_after: list = field(default_factory=list)
    comp_ms: list = field(default_factory=list)
    comp_shape: dict = field(default_factory=dict)
    comp_fallback: dict = field(default_factory=dict)
    comp_fifo: int = 0


class Stats:
    """한 번 훑으며 접는다 — 파일을 통째로 들지 않는다(백분위용 값 목록만)."""

    def __init__(self, since: str | None = None) -> None:
        self.since = since
        self.files = 0
        self.lines = 0
        self.skipped = 0
        self.filtered = 0
        self.groups: dict[tuple[str, str, str], _Group] = {}
        self._dialects = frozenset(_dialect_names())
        self._tools = frozenset(_BUILTIN_ORDER)

    # ── 닫힌 어휘 ─────────────────────────────────────
    @staticmethod
    def _version(v) -> str:
        if v is None:
            return UNKNOWN
        return v if isinstance(v, str) and _VERSION_RE.match(v) else OTHER

    def _dialect(self, v) -> str:
        if v is None:
            return UNKNOWN
        return v if v in self._dialects else OTHER

    @staticmethod
    def _model(v) -> str:
        """모델 이름 — 경로 모양이면 마지막 요소만, 한 줄·``_MODEL_MAX`` 자.

        vLLM 은 ``--model /data/models/foo`` 의 경로를 서빙 이름으로 그대로 쓴다. 절대·홈·
        상대 경로(``/``·``~``·``.`` 시작, ``X:\\``·``X:/``)는 경로를 내지 않는 계약에 걸리므로
        마지막 요소만 남긴다. ``org/name``(HF 식)은 경로가 아니라 이름이라 그대로 둔다."""
        if not isinstance(v, str):
            return UNKNOWN
        name = " ".join(v.split())
        if _PATH_LIKE_RE.match(name):
            name = re.split(r"[\\/]", name.rstrip("\\/"))[-1].strip()
        name = name[:_MODEL_MAX]
        return name or UNKNOWN

    def _tool(self, v) -> str:
        return v if isinstance(v, str) and v in self._tools else OTHER

    @staticmethod
    def _label(v, vocab) -> str:
        if v is None:
            return NONE
        return v if isinstance(v, str) and v in vocab else OTHER

    # ── 훑기 ──────────────────────────────────────────
    def add_file(self, path: Path) -> None:
        self.files += 1
        last_model = UNKNOWN
        with open(path, encoding="utf-8", errors="replace") as fh:
            for raw in fh:
                if not raw.strip():
                    continue
                self.lines += 1
                try:
                    row = json.loads(raw)
                except ValueError:
                    self.skipped += 1
                    continue
                if not isinstance(row, dict):
                    self.skipped += 1
                    continue
                day = row.get("timestamp")
                day = day[:10] if isinstance(day, str) else ""
                day = day if _DAY_RE.match(day) else ""
                is_turn = "event" not in row
                if is_turn:
                    # 압축 줄의 모델은 직전 턴 줄의 것 — 그 턴이 --since 로 걸러져도 기억한다.
                    last_model = self._model(row.get("model"))
                if self.since and (not day or day < self.since):
                    self.filtered += 1
                    continue
                if is_turn:
                    self._add_turn(path, row, last_model, day)
                elif row.get("event") == "compaction":
                    self._add_compaction(path, row, last_model, day)
                else:
                    self.skipped += 1

    def _group(self, row: dict, model: str) -> _Group:
        key = (
            self._version(row.get("harness_version")),
            model,
            self._dialect(row.get("dialect")),
        )
        g = self.groups.get(key)
        if g is None:
            g = self.groups[key] = _Group()
        return g

    def _add_turn(self, path: Path, row: dict, model: str, day: str) -> None:
        g = self._group(row, model)
        g.sessions.add(path)
        g.turns_by_file[path] = g.turns_by_file.get(path, 0) + 1
        g.turns += 1
        if day:
            g.days.add(day)
        fields = (
            "input_tokens",
            "output_tokens",
            "cache_read_input_tokens",
            "cache_creation_input_tokens",
        )
        if all(_is_count(row.get(k)) for k in fields):
            g.tok_n += 1
            g.tok_in += (
                row["input_tokens"]
                + row["cache_read_input_tokens"]
                + row["cache_creation_input_tokens"]
            )
            g.tok_out += row["output_tokens"]
            g.tok_cache += row["cache_read_input_tokens"]
            if _is_count(row.get("reasoning_tokens")):
                g.reason_n += 1
                g.reason_sum += row["reasoning_tokens"]
                g.reason_out += row["output_tokens"]
        for key, bucket in (
            ("ttft_ms", g.ttft),
            ("llm_ms", g.llm),
            ("tool_ms", g.tool),
        ):
            if _is_count(row.get(key)):
                bucket.append(row[key])
        stage = row.get("parse_stage")
        _bump(
            g.parse_stage,
            str(stage)
            if stage in _PARSE_STAGES and not isinstance(stage, bool)
            else OTHER,
        )
        _bump(g.failure_signal, self._label(row.get("failure_signal"), _SIGNALS))
        stop = self._label(row.get("stop_reason"), _CUT_STOPS)
        _bump(g.stop_reason, stop)
        if stop != NONE:
            g.cut += 1
        if row.get("stop_detail") is not None:
            _bump(g.stop_detail, self._label(row.get("stop_detail"), _STOP_DETAILS))
        tools = row.get("tools")
        if isinstance(tools, list):
            for entry in tools:
                if not (isinstance(entry, list) and len(entry) == 3):
                    continue
                name, ok, ms = entry
                t = g.tools.setdefault(self._tool(name), _Tool())
                t.calls += 1
                if ok is not True:
                    t.failures += 1
                if _is_count(ms):
                    t.ms.append(ms)

    def _add_compaction(self, path: Path, row: dict, model: str, day: str) -> None:
        g = self._group(row, model)
        g.sessions.add(path)
        if day:
            g.days.add(day)
        g.comp += 1
        for key, bucket in (
            ("tokens_before", g.comp_before),
            ("tokens_after", g.comp_after),
            ("duration_ms", g.comp_ms),
        ):
            if _is_count(row.get(key)):
                bucket.append(row[key])
        _bump(g.comp_shape, self._label(row.get("summary_shape"), SUMMARY_SHAPES))
        fallback = row.get("shape_fallback")
        if fallback:
            # 사유는 "too_large: ~N > M tokens"·"error: …" 처럼 머리말 뒤에 자유 글이
            # 붙는다 — 머리말만 어휘로 본다.
            kind = fallback.split(":", 1)[0] if isinstance(fallback, str) else None
            _bump(g.comp_fallback, self._label(kind, SHAPE_FALLBACK_KINDS))
        if row.get("fallback_used") is True:
            g.comp_fifo += 1

    # ── 결과 ──────────────────────────────────────────
    def result(self) -> dict:
        groups = []
        for (version, model, dialect), g in sorted(self.groups.items()):
            per_session = list(g.turns_by_file.values())
            llm_sum = sum(g.llm)
            tool_sum = sum(g.tool)
            groups.append(
                {
                    "harness_version": version,
                    "model": model,
                    "dialect": dialect,
                    "sessions": len(g.sessions),
                    "turns": g.turns,
                    "turns_per_session": {
                        "median": _pct(per_session, 0.5),
                        "p90": _pct(per_session, 0.9),
                        "max": max(per_session) if per_session else None,
                    },
                    "first_day": min(g.days) if g.days else None,
                    "last_day": max(g.days) if g.days else None,
                    "tokens": {
                        "n": g.tok_n,
                        "input": g.tok_in,
                        "output": g.tok_out,
                        "cache_read": g.tok_cache,
                        "cache_hit_rate": (g.tok_cache / g.tok_in)
                        if g.tok_in
                        else None,
                        "reasoning": {
                            "n": g.reason_n,
                            "sum": g.reason_sum,
                            "ratio": (
                                g.reason_sum / g.reason_out if g.reason_out else None
                            ),
                        },
                    },
                    "time_ms": {
                        "ttft": _dist(g.ttft),
                        "llm": _dist(g.llm),
                        "tool": _dist(g.tool),
                        "llm_to_tool": (llm_sum / tool_sum) if tool_sum else None,
                    },
                    "results": {
                        "parse_stage": dict(sorted(g.parse_stage.items())),
                        "failure_signal": dict(sorted(g.failure_signal.items())),
                        "stop_reason": dict(sorted(g.stop_reason.items())),
                        "stop_detail": dict(sorted(g.stop_detail.items())),
                        "cut_turns": g.cut,
                    },
                    "tools": {
                        name: {
                            "calls": t.calls,
                            "failures": t.failures,
                            "ms_sum": sum(t.ms),
                            "ms_median": _pct(t.ms, 0.5),
                        }
                        for name, t in sorted(g.tools.items())
                    },
                    "compaction": {
                        "count": g.comp,
                        "tokens_before_median": _pct(g.comp_before, 0.5),
                        "tokens_after_median": _pct(g.comp_after, 0.5),
                        "duration_ms": {
                            "n": len(g.comp_ms),
                            "median": _pct(g.comp_ms, 0.5),
                            "p90": _pct(g.comp_ms, 0.9),
                        },
                        "summary_shape": dict(sorted(g.comp_shape.items())),
                        "shape_fallback": dict(sorted(g.comp_fallback.items())),
                        "fallback_used": g.comp_fifo,
                    },
                }
            )
        return {
            "schema": SCHEMA,
            "harness_version": __version__,
            "contains": CONTAINS,
            "since": self.since,
            "files_read": self.files,
            "lines_read": self.lines,
            "lines_skipped": self.skipped,
            "lines_filtered": self.filtered,
            "groups": groups,
        }


def find_turn_files(root: Path):
    """``root`` 아래(또는 ``root`` 자체)의 ``turns.jsonl`` — 링크는 따라가지 않는다."""
    if root.is_symlink():
        return
    if root.is_file():
        if root.name == FILE_NAME:
            yield root
        return
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames.sort()
        if FILE_NAME in filenames:
            path = Path(dirpath) / FILE_NAME
            if not path.is_symlink() and path.is_file():
                yield path


def collect(roots: list[Path], since: str | None = None) -> dict:
    stats = Stats(since)
    seen: set[Path] = set()
    for root in roots:
        for path in find_turn_files(root):
            real = path.resolve()
            if real in seen:  # 겹친 PATH 는 한 번만
                continue
            seen.add(real)
            stats.add_file(path)
    return stats.result()


# ── 사람이 읽는 표 ─────────────────────────────────────


def _num(v, digits: int = 0) -> str:
    if v is None:
        return "-"
    if isinstance(v, float) and digits:
        return f"{v:.{digits}f}"
    return f"{round(v):,}"


def _rate(v) -> str:
    return "-" if v is None else f"{v * 100:.1f}%"


def _counts(d: dict) -> str:
    return ", ".join(f"{k}={v}" for k, v in d.items()) or "-"


def format_table(data: dict) -> str:
    lines = [
        f"agent-cli stats (schema {data['schema']}, agent-cli {data['harness_version']})",
        data["contains"],
        f"files {data['files_read']} · lines {data['lines_read']} · "
        f"skipped {data['lines_skipped']} · filtered {data['lines_filtered']}"
        + (f" · since {data['since']}" if data["since"] else ""),
    ]
    if not data["groups"]:
        lines.append("No turn records found.")
    for g in data["groups"]:
        tok = g["tokens"]
        t = g["time_ms"]
        r = g["results"]
        c = g["compaction"]
        tps = g["turns_per_session"]
        lines += [
            "",
            f"== {g['harness_version']} · {g['model']} · {g['dialect']}",
            (
                f"  sessions {g['sessions']} · turns {g['turns']} "
                f"(per session median {_num(tps['median'])}, p90 {_num(tps['p90'])}, "
                f"max {_num(tps['max'])}) · days {g['first_day'] or '-'}"
                f" .. {g['last_day'] or '-'}"
            ),
            (
                f"  tokens (n {tok['n']}): in {_num(tok['input'])} · out "
                f"{_num(tok['output'])} · cache read {_num(tok['cache_read'])} "
                f"({_rate(tok['cache_hit_rate'])}) · reasoning "
                f"{_num(tok['reasoning']['sum'])} ({_rate(tok['reasoning']['ratio'])}, "
                f"n {tok['reasoning']['n']})"
            ),
        ]
        for label in ("ttft", "llm", "tool"):
            d = t[label]
            lines.append(
                f"  {label + ' ms':<8} n {d['n']} · median {_num(d['median'])} · "
                f"p90 {_num(d['p90'])} · sum {_num(d['sum'])}"
            )
        lines += [
            f"  llm:tool time {_num(t['llm_to_tool'], 2)}",
            f"  parse_stage: {_counts(r['parse_stage'])}",
            f"  failure_signal: {_counts(r['failure_signal'])}",
            f"  stop_reason: {_counts(r['stop_reason'])} · cut turns {r['cut_turns']}",
            f"  stop_detail: {_counts(r['stop_detail'])}",
        ]
        if g["tools"]:
            lines.append("  tools (calls / failures / ms sum / ms median):")
            for name, s in g["tools"].items():
                lines.append(
                    f"    {name:<12} {s['calls']:>6} {s['failures']:>6} "
                    f"{_num(s['ms_sum']):>10} {_num(s['ms_median']):>8}"
                )
        if c["count"]:
            d = c["duration_ms"]
            lines.append(
                f"  compaction {c['count']}: tokens before/after median "
                f"{_num(c['tokens_before_median'])} / {_num(c['tokens_after_median'])}"
                f" · ms median {_num(d['median'])}, p90 {_num(d['p90'])}"
                f" · shape {_counts(c['summary_shape'])}"
                f" · boundary skipped {_counts(c['shape_fallback'])}"
                f" · fifo fallback {c['fallback_used']}"
            )
    return "\n".join(lines) + "\n"
