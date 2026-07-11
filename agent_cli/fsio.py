"""파일 저장 패턴의 단일 소유자 (C5 부산물, v4.47.0).

repo 전역에 제각각이던 저장 코드를 3패턴으로 수렴한다:

1. **원자 교체** (:func:`atomic_write_text` / :func:`atomic_write_json`) —
   다른 프로세스/스레드/다음 실행이 *읽는* 상태 파일용
   (compaction.json, web.json, status.json, models.json, memory.jsonl
   rewrite, DIRECTIVE.md, 세션 meta 생성). ``mkstemp`` 로 **writer 마다
   유니크한** 같은-디렉토리 tmp 에 쓰고 ``os.replace`` 로 스왑 — 독자는
   all-or-nothing 만 본다. 고정 tmp 이름은 동시 writer 레이스로 실측
   크래시를 냈던 패턴이라 금지 (status.json, v4.27.1).

2. **가드 append** (:func:`append_line`) — JSONL 로그용 (history.jsonl,
   turns.jsonl). 부모 디렉토리가 외부 정리(`rm -rf` 등)로 사라져도
   FileNotFoundError 실패 경로에서만 mkdir+재시도 — 정상 경로는 open
   1회 (v4.39.0 B6 결정 보존). 핸들 유지는 의도적 비채택: unlink 된
   inode 에 계속 쓰는 소리 없는 유실이 가드 목적을 훼손한다.

3. **직접 쓰기** (이 모듈 비사용, 의도) — 도구가 만드는 사용자/산출물
   파일 (write_file/edit_file 의 대상, shell/fetch over-cap 저장,
   delegate result.md). 도구의 의미론이 "그 경로에 그대로 쓴다"이므로
   원자 교체가 오히려 어긋난다 (하드링크/권한/감시자 의미 변화).
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path


def atomic_write_text(path: Path, text: str) -> None:
    """``text`` 를 ``path`` 에 원자적으로 저장 (유니크 tmp → replace).

    부모 디렉토리 소실은 실패 경로에서만 mkdir+재시도 (가드 append 와
    동일한 규율 — 정상 경로 syscall 최소).
    """
    path = Path(path)

    def _write() -> None:
        fd, tmp = tempfile.mkstemp(
            dir=str(path.parent), prefix=f".{path.name}-", suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(text)
            os.replace(tmp, path)  # atomic swap — 독자는 all-or-nothing
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    try:
        _write()
    except FileNotFoundError:
        path.parent.mkdir(parents=True, exist_ok=True)
        _write()


def atomic_write_json(path: Path, obj, *, indent: int | None = None) -> None:
    """JSON 직렬화 + :func:`atomic_write_text`."""
    atomic_write_text(path, json.dumps(obj, ensure_ascii=False, indent=indent))


def append_line(path: Path, line: str) -> None:
    """``line``(개행 미포함) 을 JSONL 로그에 append.

    mkdir 는 실패 시에만 — history.jsonl 의 외부-정리 복구 가드를
    일반화한 것 (원 위치: context.manager v4.39.0).
    """
    path = Path(path)
    data = line + "\n"
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(data)
    except FileNotFoundError:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(data)
