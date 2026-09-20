"""감시 조건 3종 — `type` 키로 찾는 작은 레지스트리 (docs/monitor/DESIGN.md §4.2).

조건 추가 = 클래스 하나 + `@register`, 소비 지점 0. `WireFormat`·`Tool`·
`render/<name>.py` 와 같은 방식이다.

세 종류로 좁힌 경위는 §4.2 에 있다. 요약하면:

- `exit` **삭제** — `shell` 이 `start_new_session` 없이 `subprocess.run(shell=True)`
  라 `&` 로 띄운 자식은 재부모화된다. 부모가 아닌 우리 스레드는 `waitpid` 를 못
  하므로 "종료 코드 포함"이 명세부터 불가능했다. 관용구
  ``( cmd; echo "EXIT:$?" ) > log 2>&1 &`` + `match` 가 대신한다.
- `interval` **삭제** — 주기형 `command` 가 상위집합이다(`interval` 은 "시간이
  됐다"만 알려 모델이 로그를 다시 읽어야 하는데, 주기 `command` 는 내용을 보고문에
  실어 온다 → 턴 하나 절약).
"""

from __future__ import annotations

import os
import re
import subprocess
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import ClassVar

# 주기 `command` 한 번의 실행 상한 — 폴링 스레드를 오래 잡으면 다른 모니터가 밀린다.
COMMAND_TIMEOUT_S = 30


@dataclass
class Match:
    """조건 발화 1건. ``lines`` 가 보고문 본문이 된다."""

    lines: list[str] = field(default_factory=list)


class Condition(ABC):
    """``st`` 는 이 모니터의 가변 상태(바이트 커서·inode·마지막 실행 시각).

    레지스트리가 모니터당 dict 하나를 들고 매 틱 넘긴다 — 조건 객체 자신은
    **상태를 갖지 않는다**(영속·재시작이 dict 하나만 다루면 되도록).
    """

    type: ClassVar[str]

    @abstractmethod
    def check(self, st: dict, *, now: float) -> Match | None: ...

    def describe(self) -> str:
        """보고문 머리줄에 쓸 한 줄 — 모니터가 여럿일 때 어느 것인지 알아야 한다."""
        return self.type


_REGISTRY: dict[str, type[Condition]] = {}


def register(cls: type[Condition]) -> type[Condition]:
    _REGISTRY[cls.type] = cls
    return cls


def build(spec: dict) -> Condition:
    """``when`` dict → Condition. 미지의 타입·잘못된 파라미터는 ValueError
    (도구가 ToolResult 로 변환한다 — `constants.parse_duration` 과 같은 분업)."""
    if not isinstance(spec, dict):
        # ValueError 로 통일한다 — 호출자(도구)가 ToolResult 로 변환하는데
        # 예외 종류가 갈리면 변환부가 둘이 된다.
        raise ValueError("`when` must be an object")  # noqa: TRY004
    kind = spec.get("type", "")
    cls = _REGISTRY.get(kind)
    if cls is None:
        known = ", ".join(sorted(_REGISTRY))
        raise ValueError(f"unknown condition type {kind!r} — known: {known}")
    return cls.from_spec(spec)  # type: ignore[attr-defined]


def known_types() -> list[str]:
    return sorted(_REGISTRY)


# ── 파일 커서 공용 ──────────────────────────────────────────


def _stat(path: Path) -> os.stat_result | None:
    try:
        return path.stat()
    except OSError:
        return None


def _cursor_reset_needed(st: dict, stt: os.stat_result) -> bool:
    """파일이 **갈렸는가** — 축소(truncate)와 **inode 변경**(logrotate 의 rename)
    둘 다 본다.

    크기만 보면 rename 로테이션을 놓친다: 새 파일이 한 틱 안에 옛 오프셋을
    넘어서면 크기는 커진 채라 "정상 전진"으로 읽히고, **그 사이 줄을 통째로
    건너뛴다.** `st_ino` 는 필드 하나다.
    """
    if st.get("ino") not in (None, stt.st_ino):
        return True
    return stt.st_size < st.get("offset", 0)


@register
class MatchCondition(Condition):
    """파일의 **새 줄**에서 정규식을 찾는다.

    커서는 **등록 시점의 EOF 에서 시작**한다(0 이 아니다) — 이미 수십 MB 쌓인
    로그에 모니터를 걸면 과거 전체가 한꺼번에 매치된다.

    **전제: append-only 로그.** 같은 크기로 제자리 덮어쓰기(rewrite-in-place)는
    보이지 않는다 — 크기도 inode 도 안 바뀌기 때문이다. 감시 대상은 리다이렉션된
    스크립트 출력이라 append 가 정상이고, 이걸 잡으려면 매 틱 전문을 다시 읽어야
    해서 비용이 뒤집힌다. 알고 두는 한계다.
    """

    type = "match"

    def __init__(self, file: str, pattern: str):
        self.file = file
        self.pattern = pattern
        self._re = re.compile(pattern)

    @classmethod
    def from_spec(cls, spec: dict) -> MatchCondition:
        file = str(spec.get("file") or "").strip()
        pattern = str(spec.get("pattern") or "")
        if not file:
            raise ValueError("match: `file` is required")
        if not pattern:
            raise ValueError("match: `pattern` is required")
        try:
            re.compile(pattern)
        except re.error as exc:
            raise ValueError(f"match: bad regex {pattern!r} — {exc}") from None
        return cls(file, pattern)

    def describe(self) -> str:
        return f"match · {self.file}"

    def check(self, st: dict, *, now: float) -> Match | None:
        path = Path(self.file).expanduser()
        stt = _stat(path)
        if stt is None:
            # 아직 없는 파일 = **대기**, 죽음이 아니다. 스크립트가 나중에
            # 만드는 로그가 정상 경로다(§9 테스트 계획).
            return None
        if "offset" not in st:
            # 등록 후 첫 관측 — 지금의 EOF 부터 본다.
            st["offset"] = stt.st_size
            st["ino"] = stt.st_ino
            return None
        if _cursor_reset_needed(st, stt):
            st["offset"] = 0
        st["ino"] = stt.st_ino
        if stt.st_size <= st["offset"]:
            return None
        try:
            with path.open("rb") as f:
                f.seek(st["offset"])
                chunk = f.read()
                st["offset"] = f.tell()
        except OSError:
            return None
        text = chunk.decode("utf-8", errors="replace")
        hits = [ln for ln in text.splitlines() if self._re.search(ln)]
        return Match(hits) if hits else None


@register
class SilenceCondition(Condition):
    """파일이 ``seconds`` 동안 조용하면 발화 — **침묵은 성공이 아니다.**

    기준 시각은 ``max(mtime, registered_at)`` 이다. mtime 만 쓰면 아직 쓰기
    시작 전인 로그(또는 오래된 로그)에 모니터를 건 순간 **즉시 발화**한다.
    """

    type = "silence"

    def __init__(self, file: str, seconds: int):
        self.file = file
        self.seconds = seconds

    @classmethod
    def from_spec(cls, spec: dict) -> SilenceCondition:
        file = str(spec.get("file") or "").strip()
        if not file:
            raise ValueError("silence: `file` is required")
        raw = spec.get("seconds")
        try:
            seconds = int(raw)
        except (TypeError, ValueError):
            raise ValueError("silence: `seconds` must be an integer") from None
        if seconds <= 0:
            raise ValueError("silence: `seconds` must be positive")
        return cls(file, seconds)

    def describe(self) -> str:
        return f"silence · {self.file} · {self.seconds}s"

    def check(self, st: dict, *, now: float) -> Match | None:
        path = Path(self.file).expanduser()
        stt = _stat(path)
        base = st.get("registered_at", now)
        last = max(stt.st_mtime, base) if stt is not None else base
        if now - last < self.seconds:
            return None
        quiet = int(now - last)
        where = self.file if stt is not None else f"{self.file} (아직 없음)"
        return Match([f"{where} — {quiet}s 동안 변화 없음"])


@register
class CommandCondition(Condition):
    """``every`` 초마다 명령을 돌리고 **exit 0 이면** 발화. stdout 이 보고 본문.

    **주기 실행이지 스트리밍이 아니다.** 스트리밍이면
    ``grep --line-buffered``/``awk fflush()`` 를 정확히 써야 하고, 한 단이라도
    버퍼링하면 매치가 갇혀 **조용히 아무 알림도 안 온다** — 선언형을 고른 바로
    그 실패 모드를 탈출구가 다시 들여오는 꼴이다. 주기 실행은 프로세스가
    끝나므로 **구조적으로** 플러시된다(지시가 아니라 구조로 해결).
    """

    type = "command"

    def __init__(self, command: str, every: int):
        self.command = command
        self.every = every

    @classmethod
    def from_spec(cls, spec: dict) -> CommandCondition:
        from agent_cli.constants import MONITOR_INTERVAL_MIN_S, parse_duration

        command = str(spec.get("command") or "").strip()
        if not command:
            raise ValueError("command: `command` is required")
        raw = spec.get("every", MONITOR_INTERVAL_MIN_S)
        try:
            every = parse_duration(str(raw))
        except ValueError as exc:
            raise ValueError(f"command: `every` — {exc}") from None
        # clamp (거부 아님) — §10.2
        every = max(MONITOR_INTERVAL_MIN_S, every)
        return cls(command, every)

    def describe(self) -> str:
        return f"command · {self.command[:60]} · {self.every}s"

    def check(self, st: dict, *, now: float) -> Match | None:
        last = st.get("last_run")
        if last is not None and now - last < self.every:
            return None
        st["last_run"] = now
        try:
            proc = subprocess.run(
                self.command,
                shell=True,
                capture_output=True,
                timeout=COMMAND_TIMEOUT_S,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return Match([f"(명령이 {COMMAND_TIMEOUT_S}s 안에 끝나지 않음)"])
        if proc.returncode != 0:
            return None
        out = proc.stdout.decode("utf-8", errors="replace").strip()
        return Match(out.splitlines() if out else ["(exit 0, 출력 없음)"])
