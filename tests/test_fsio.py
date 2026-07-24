"""fsio — 파일 저장 패턴의 단일 소유자 (C5 부산물, v4.47.0).

원자 교체(유니크 tmp + replace)와 가드 append 의 규율을 고정한다.
"""

from __future__ import annotations

import json
import shutil
import threading

from agent_cli.fsio import append_line, atomic_write_json, atomic_write_text


class TestAtomicWrite:
    def test_roundtrip(self, tmp_path):
        p = tmp_path / "state.json"
        atomic_write_json(p, {"a": 1, "한글": "값"})
        assert json.loads(p.read_text()) == {"a": 1, "한글": "값"}

    def test_no_tmp_leftover(self, tmp_path):
        p = tmp_path / "s.txt"
        atomic_write_text(p, "v1")
        atomic_write_text(p, "v2")
        assert p.read_text() == "v2"
        # tmp 잔재 0 (성공 경로에서 replace 로 소비)
        assert [f.name for f in tmp_path.iterdir()] == ["s.txt"]

    def test_unique_tmp_concurrent_writers_no_crash(self, tmp_path):
        # v4.27.1 실측 교훈의 회귀 가드: 고정 tmp 였으면 replace 경합으로
        # FileNotFoundError 크래시. 유니크 tmp 는 writer 독립 — last wins.
        p = tmp_path / "status.json"
        errors: list[BaseException] = []

        def w(i):
            try:
                for _ in range(50):
                    atomic_write_json(p, {"writer": i})
            except BaseException as e:
                errors.append(e)

        ts = [threading.Thread(target=w, args=(i,)) for i in range(4)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        assert not errors
        assert json.loads(p.read_text())["writer"] in range(4)

    def test_parent_dir_wiped_recovers(self, tmp_path):
        d = tmp_path / "sub"
        d.mkdir()
        p = d / "s.txt"
        atomic_write_text(p, "v1")
        shutil.rmtree(d)  # 외부 정리 시뮬레이션
        atomic_write_text(p, "v2")  # 실패 경로 mkdir+재시도
        assert p.read_text() == "v2"


class TestAppendLine:
    def test_appends_lines(self, tmp_path):
        p = tmp_path / "log.jsonl"
        append_line(p, '{"a":1}')
        append_line(p, '{"a":2}')
        assert p.read_text().splitlines() == ['{"a":1}', '{"a":2}']

    def test_parent_dir_wiped_recovers(self, tmp_path):
        d = tmp_path / "sess"
        d.mkdir()
        p = d / "h.jsonl"
        append_line(p, "one")
        shutil.rmtree(d)
        append_line(p, "two")  # 가드 append — mkdir 는 실패 시에만
        assert p.read_text().strip() == "two"


class TestC5Separation:
    def test_records_render_store_standalone(self):
        # 세 모듈이 manager 없이 단독 임포트 + manager 역참조 0 (단방향)
        from agent_cli.context import records, render, store

        for mod in (records, render, store):
            with open(mod.__file__) as fh:
                src = fh.read()
            imports = [
                ln
                for ln in src.splitlines()
                if ln.strip().startswith(("import ", "from "))
            ]
            assert not any("context.manager" in ln for ln in imports), mod.__name__

    def test_store_compaction_roundtrip(self, tmp_path):
        from agent_cli.context.store import (
            COMPACTION_JSON_VERSION,
            load_compaction,
            save_compaction,
        )

        p = tmp_path / "compaction.json"
        save_compaction(p, {"summary": "s", "dynamic_start_index": 7})
        data = load_compaction(p)
        assert data["summary"] == "s" and data["dynamic_start_index"] == 7
        assert data["version"] == COMPACTION_JSON_VERSION
        # 버전 불일치 → None (fresh)
        p.write_text('{"version": 999}')
        assert load_compaction(p) is None

    def test_store_records_roundtrip_tolerant(self, tmp_path):
        from agent_cli.context.store import append_record, load_records

        p = tmp_path / "history.jsonl"
        append_record(p, {"role": "user", "content": "hi"})
        p.open("a").write("NOT JSON\n")  # 깨진 줄 관용
        append_record(p, {"role": "assistant", "thought": "t"})
        recs = load_records(p)
        assert len(recs) == 2 and recs[1]["thought"] == "t"
