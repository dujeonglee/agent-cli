"""세션 영속화의 디스크 I/O primitive (C5, v4.47.0).

정책 0 — 모든 함수가 ``path`` 를 인자로 받는 순수 I/O 다. 압축 정책
상태(summary/file_list/offset …)와 캐시 재구성은 ContextManager 소유
그대로: store 경계 비교(A vs B)에서 ``_dynamic_start_index`` 소유권
역전 때문에 상태-소유 클래스(B)가 성립 불가함을 확인하고 primitive
함수(A)로 확정했다. 두 번째 스토리지 백엔드가 실재하거나 fake-store
수요가 monkeypatch 로 감당 안 될 때 클래스로 승격한다 (호출부가 이미
이 모듈로 수렴돼 있어 저비용).

저장 패턴은 :mod:`agent_cli.fsio` 규율을 따른다 — compaction.json 은
원자 교체(이전의 고정-tmp 는 v4.27.1 status.json 레이스와 같은 패턴이라
유니크 tmp 로 교정), history.jsonl 은 가드 append.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from agent_cli.fsio import append_line, atomic_write_json

# Bump when the compaction.json schema changes incompatibly — loader
# ignores files with a different version (falls back to fresh state).
COMPACTION_JSON_VERSION = 1


def append_record(path: Path, record: dict) -> None:
    """history.jsonl 에 레코드 1건 append (가드 append 패턴)."""
    append_line(path, json.dumps(record, ensure_ascii=False))


def load_records(path: Path) -> list[dict]:
    """history.jsonl 전체를 관용 파싱 (깨진 줄은 건너뜀)."""
    with open(path, "r", encoding="utf-8") as f:
        raw_lines = [line.strip() for line in f.readlines() if line.strip()]
    records: list[dict] = []
    for line in raw_lines:
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return records


def save_compaction(path: Path, state: dict) -> None:
    """compaction.json 원자 저장. ``state`` 는 manager 가 소유한 압축
    상태의 스냅샷 dict — version 스탬프는 여기서 찍는다."""
    atomic_write_json(path, {"version": COMPACTION_JSON_VERSION, **state})


def load_compaction(path: Path) -> dict | None:
    """compaction.json 로드 — 부재/파손/버전 불일치는 None (fresh 시작)."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict) or data.get("version") != COMPACTION_JSON_VERSION:
        return None
    return data


def fork_history(src: Path, dst_dir: Path) -> Path:
    """history.jsonl 을 fork 대상 디렉토리로 복사 (delegate fork 모드)."""
    dst_dir.mkdir(parents=True, exist_ok=True)
    target = dst_dir / "history.jsonl"
    if src.is_file():
        shutil.copy2(src, target)
    return target
