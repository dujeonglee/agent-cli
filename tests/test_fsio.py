"""fsio — 파일 저장 패턴의 단일 소유자 (C5 부산물, v4.47.0).

원자 교체(유니크 tmp + replace)와 가드 append 의 규율을 고정한다.
"""

from __future__ import annotations

import builtins
import json
import shutil
import threading
import time
from pathlib import Path

from agent_cli.fsio import (
    _append_lock,
    append_line,
    atomic_write_json,
    atomic_write_text,
)


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


class _SplitWriteFile:
    """write 를 두 조각으로 쪼개고 그 사이에 스레드를 양보하는 파일 래퍼.

    **O_APPEND 원자성을 보장하지 않는 파일시스템을 결정적으로 재현한다.**
    실측(v7.29.0): 로컬 ext4 는 append 1회 = write(2) 1회라 락 없이도 안 깨지지만,
    WSL 의 Windows 드라이브 마운트(drvfs)는 4KB 페이로드에서도 128줄 중 45줄만
    남고 그중 15줄이 깨진 JSON 이었다. 호스트 파일시스템에 의존하는 테스트는
    ext4 에서 락을 지워도 통과해 **가드 구실을 못 하므로**, 비원자 쓰기를 여기서
    주입해 어느 환경에서든 계약(직렬화)을 검증한다.
    """

    def __init__(self, f):
        self._f = f

    def write(self, data):
        mid = max(1, len(data) // 2)
        self._f.write(data[:mid])
        self._f.flush()
        time.sleep(0.002)  # 다른 스레드가 끼어들 창
        self._f.write(data[mid:])

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return self._f.__exit__(*exc)


class TestAppendLineConcurrency:
    """동시 append 직렬화 (v7.29.0) — 다중 사용자 병렬 턴(A1)의 전제.

    오염된 줄은 ``store.load_records`` 의 "깨진 줄 건너뜀" 정책에 걸려
    **조용히 사라진다** — resume 시 원인 표시 없이 기록이 증발하는, 진단이 가장
    어려운 종류의 손상이다. 근거·실측은 :data:`agent_cli.fsio._APPEND_LOCKS`.
    """

    PAYLOAD = 4096

    def _run(self, path, writers, per_writer):
        errors: list[BaseException] = []
        barrier = threading.Barrier(writers)  # 실제로 겹치도록 동시 출발

        def w(i):
            try:
                barrier.wait()
                for j in range(per_writer):
                    rec = {"writer": i, "seq": j, "body": "가" * self.PAYLOAD}
                    append_line(path, json.dumps(rec, ensure_ascii=False))
            except BaseException as e:  # 스레드 실패를 본문 스레드로 전달
                errors.append(e)

        ts = [threading.Thread(target=w, args=(i,)) for i in range(writers)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        assert not errors
        return path.read_text(encoding="utf-8").splitlines()

    def test_non_atomic_writes_never_interleave(self, tmp_path, monkeypatch):
        """비원자 쓰기 파일시스템(drvfs 부류)에서도 줄이 섞이지 않는다.

        **락을 제거하면 이 테스트는 실패한다** — 가드가 실제로 하중을 받는지
        확인한 유일한 케이스다(ext4 만으로는 확인 불가).
        """
        real_open = builtins.open

        def splitting_open(*a, **kw):
            f = real_open(*a, **kw)
            # append 모드만 래핑 — 테스트 본문의 read 는 그대로 둔다.
            mode = a[1] if len(a) > 1 else kw.get("mode", "r")
            return _SplitWriteFile(f) if "a" in mode else f

        monkeypatch.setattr(builtins, "open", splitting_open)
        p = tmp_path / "history.jsonl"
        writers, per_writer = 6, 4
        lines = self._run(p, writers, per_writer)
        monkeypatch.undo()

        # ① 줄 수 정확 — 유실도 분열도 없다.
        assert len(lines) == writers * per_writer
        # ② 모든 줄이 온전한 JSON — 한 줄 중간에 다른 줄이 끼지 않았다.
        recs = [json.loads(line) for line in lines]
        # ③ 본문이 절단/혼합되지 않았다.
        assert all(len(r["body"]) == self.PAYLOAD for r in recs)
        # ④ 정확히 기대한 (writer, seq) 집합.
        assert {(r["writer"], r["seq"]) for r in recs} == {
            (i, j) for i in range(writers) for j in range(per_writer)
        }

    def test_same_writer_keeps_its_order(self, tmp_path):
        """한 writer 안에서는 append 순서가 보존된다(턴 내 레코드 순서)."""
        lines = self._run(tmp_path / "history.jsonl", writers=4, per_writer=8)
        recs = [json.loads(line) for line in lines]
        for i in range(4):
            seqs = [r["seq"] for r in recs if r["writer"] == i]
            assert seqs == sorted(seqs)

    def test_relative_and_absolute_paths_share_one_lock(self, tmp_path, monkeypatch):
        """같은 파일을 상대/절대 경로로 부르는 두 호출이 같은 락으로 모인다 —
        키가 ``abspath`` 정규화라서. (다르면 직렬화가 조용히 무력화된다.)"""
        monkeypatch.chdir(tmp_path)
        assert _append_lock(Path("h.jsonl")) is _append_lock(tmp_path / "h.jsonl")

    def test_serial_path_still_recovers_wiped_parent(self, tmp_path):
        """락이 재시도 경로를 감싸도 외부-정리 복구 가드가 살아있다."""
        d = tmp_path / "sess"
        d.mkdir()
        p = d / "h.jsonl"
        append_line(p, "one")
        shutil.rmtree(d)
        append_line(p, "two")
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
