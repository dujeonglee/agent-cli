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
   **스레드 안전** (v7.29.0) — 아래 :data:`_APPEND_LOCKS` 참조.

3. **직접 쓰기** (이 모듈 비사용, 의도) — 도구가 만드는 사용자/산출물
   파일 (write_file/edit_file 의 대상, shell/fetch over-cap 저장,
   delegate result.md). 도구의 의미론이 "그 경로에 그대로 쓴다"이므로
   원자 교체가 오히려 어긋난다 (하드링크/권한/감시자 의미 변화).
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
from pathlib import Path

# ── append 직렬화 (v7.29.0) ──────────────────────────────
#
# 왜 필요한가 — **파일시스템이 O_APPEND 원자성을 보장하지 않으면 레코드가
# 통째로 사라진다.** 로컬 ext4 에서는 문제가 없다(실측: append 1회 = write(2)
# 1회, 8MB 페이로드도 마찬가지 — BufferedWriter 는 버퍼보다 큰 데이터를 쪼개지
# 않고 raw 로 그대로 흘린다. 따라서 O_APPEND 원자성이 성립해 16스레드×8회
# 동시 append 가 무손상). 그러나 **WSL 의 Windows 드라이브 마운트(/mnt/*,
# drvfs)** 에서는 같은 탐침이 처참하게 깨진다 — 페이로드 4KB 에서도
# 128줄 중 45줄만 남고 그중 15줄이 깨진 JSON이었다(1MB 에서는 28/128).
# 즉 손상의 원인은 "버퍼 분할"이 아니라 **마운트가 O_APPEND 를 원자적으로
# 구현하지 않는 것**이고, 그래서 크기와 무관하게 발생한다. 같은 조건에서
# 이 락을 걸면 128/128·corrupt 0 으로 완전히 사라진다.
#
# 왜 치명적인가: ``store.load_records`` 는 "깨진 줄은 건너뜀" 정책이라 오염된
# 레코드를 **조용히 버린다**. resume 시 원인 표시 없이 기록이 사라지는, 진단이
# 가장 어려운 종류의 손상이다.
#
# 이게 가정이 아닌 이유: 세션 디렉토리는 cwd 를 따라가므로 사용자가 Windows
# 드라이브(/mnt/d/...)에서 작업하면 history.jsonl 이 바로 그 마운트에 놓인다.
# NFS 등 네트워크 파일시스템도 같은 부류다(O_APPEND 원자성 미보장).
#
# 왜 지금인가: 오늘까지 이 레이스는 발현하지 않았다 — 병렬 서브에이전트는
# 각자 자기 ContextManager·자기 history.jsonl 을 갖기 때문이다
# (``subagent/runner.py`` ``create_subagent_ctx``). 공유 세션 파일에 대한 동시
# append 는 **다중 사용자 병렬 턴(A1)** 이 처음 만든다. 그 전에 닫아둔다.
# (참고: 렌더러는 같은 문제를 자기 사이드카에서 이미 배웠다 —
#  ``render/web.py`` 의 ``_scope_log_lock``. ctx 쪽만 무방비였다.)
#
# 왜 스트라이핑인가: 경로별 dict 는 세션이 길어질수록 Lock 이 무한 누적된다
# (서브에이전트 run 디렉토리마다 history.jsonl 하나). 고정 개수 스트라이프는
# 메모리가 유계이고 등록용 락도 필요 없다. 해시 충돌은 서로 무관한 두 파일이
# 잠깐 줄을 서는 것뿐 — **정확성에는 영향이 없고**, append 한 건은 수십 μs 라
# LLM 콜(초 단위) 옆에서 측정되지 않는다.
#
# 프로세스 로컬로 충분한 이유: 한 세션 = 한 프로세스이고(web 은 spawn-or-attach
# 계약), 서브에이전트는 프로세스가 아니라 스레드다. 프로세스 간 공유가 생기면
# 그때는 파일 락(fcntl)이 필요하며 이 주석이 그 경계다.
#
# 데드락 무관: 락 구간 안에서 부르는 것은 open/write/close 뿐 — 사용자 코드도
# 다른 락도 타지 않는다. 따라서 효과 락(M4)과의 획득 순서를 따질 필요가 없다.
_APPEND_LOCK_STRIPES = 64
_APPEND_LOCKS: tuple[threading.Lock, ...] = tuple(
    threading.Lock() for _ in range(_APPEND_LOCK_STRIPES)
)


def _append_lock(path: Path) -> threading.Lock:
    """``path`` 를 담당하는 스트라이프 락.

    키는 ``os.path.abspath`` — 파일시스템을 건드리지 않는 순수 문자열 정규화라
    상대/절대 표기가 같은 락으로 모인다. symlink 로 같은 파일을 가리키는 서로
    다른 경로는 다른 키가 되지만, 호출부는 전부 session_dir 파생 경로를 쓰므로
    실제로 발생하지 않는다 (경로 탈출 검사는 ``_confine`` 책임).
    """
    return _APPEND_LOCKS[hash(os.path.abspath(path)) % _APPEND_LOCK_STRIPES]


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
    """``line``(개행 미포함) 을 JSONL 로그에 append. **스레드 안전**.

    mkdir 는 실패 시에만 — history.jsonl 의 외부-정리 복구 가드를
    일반화한 것 (원 위치: context.manager v4.39.0).

    같은 파일로 향하는 동시 호출은 :func:`_append_lock` 이 직렬화한다 —
    버퍼 초과 레코드가 여러 write(2) 로 쪼개져 서로의 줄 중간에 끼어드는
    것을 막는다(근거는 :data:`_APPEND_LOCKS` 주석). 재시도 경로까지 한
    임계영역에 두는 이유: mkdir 와 재-open 사이에 다른 스레드가 끼면 같은
    쪼개짐이 재현된다. 단일 스레드에서는 무경쟁 락이라 비용이 사실상 0.
    """
    path = Path(path)
    data = line + "\n"
    with _append_lock(path):
        try:
            with open(path, "a", encoding="utf-8") as f:
                f.write(data)
        except FileNotFoundError:
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "a", encoding="utf-8") as f:
                f.write(data)
