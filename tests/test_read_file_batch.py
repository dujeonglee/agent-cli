"""read_file 배치(배열-only) 테스트 — TDD.

설계: read_file 은 `read_file_reads` 배열 only (단일 키 제거).
- `_read_one(spec)`: 파일 하나를 읽는다 (기존 tool_read_file 본문).
- `tool_read_file(args)`: args["reads"] 배열을 순회. 1개면 단일 출력(헤더 없음),
  여러 개면 파일별 헤더로 합성. 전부 실패할 때만 success=False.
- 배열 item 내부는 bare 키(path/line_start/...), top-level read_file_reads 만 prefix.
"""

from __future__ import annotations

import pytest

from agent_cli.tools.read_file import ReadFileTool, _read_one, tool_read_file


@pytest.fixture
def sample(tmp_path):
    f = tmp_path / "a.py"
    f.write_text("line1\nline2\nline3\nline4\nline5\n")
    return f


# ── _read_one: 단일 파일 읽기 단위 ──────────────────────────


def test_read_one_full(sample):
    r = _read_one({"path": str(sample)})
    assert r.success
    assert "line1" in r.output and "line5" in r.output


def test_read_one_range(sample):
    r = _read_one({"path": str(sample), "line_start": 2, "line_end": 3})
    assert r.success
    assert "line2" in r.output and "line3" in r.output
    assert "line4" not in r.output


def test_read_one_search(sample):
    r = _read_one({"path": str(sample), "search": "line3", "context": 0})
    assert r.success
    assert "line3" in r.output


def test_read_one_stat(sample):
    r = _read_one({"path": str(sample), "stat": True})
    assert r.success
    assert "[stat]" in r.output


def test_read_one_missing_file(tmp_path):
    r = _read_one({"path": str(tmp_path / "nope.py")})
    assert not r.success


# ── tool_read_file: 배열 순회 ───────────────────────────────


def test_batch_single_element_matches_read_one(sample):
    # 1개짜리 배열은 _read_one 과 바이트 동일 (파일 헤더 합성 없음)
    batch = tool_read_file({"reads": [{"path": str(sample)}]})
    direct = _read_one({"path": str(sample)})
    assert batch.success
    assert batch.output == direct.output


def test_batch_multiple_files(tmp_path):
    f1 = tmp_path / "a.py"
    f1.write_text("aaa\n")
    f2 = tmp_path / "b.py"
    f2.write_text("bbb\n")
    r = tool_read_file({"reads": [{"path": str(f1)}, {"path": str(f2)}]})
    assert r.success
    assert "aaa" in r.output and "bbb" in r.output
    # 파일별로 어느 파일인지 구분되어야 한다
    assert str(f1) in r.output and str(f2) in r.output


def test_batch_same_file_multiple_ranges(sample):
    r = tool_read_file(
        {
            "reads": [
                {"path": str(sample), "line_start": 1, "line_end": 2},
                {"path": str(sample), "line_start": 4, "line_end": 5},
            ]
        }
    )
    assert r.success
    assert "line1" in r.output and "line5" in r.output


def test_batch_partial_failure_is_success(tmp_path):
    # 일부 실패해도 하나라도 성공하면 success=True (전부 실패할 때만 False)
    f1 = tmp_path / "a.py"
    f1.write_text("aaa\n")
    r = tool_read_file(
        {"reads": [{"path": str(f1)}, {"path": str(tmp_path / "missing.py")}]}
    )
    assert r.success  # 성공분이 있으므로 True
    assert "aaa" in r.output  # 성공분 포함
    assert "missing.py" in r.output  # 실패도 관찰 가능하게 표기


def test_batch_all_failure_is_failure(tmp_path):
    r = tool_read_file(
        {"reads": [{"path": str(tmp_path / "x.py")}, {"path": str(tmp_path / "y.py")}]}
    )
    assert not r.success


def test_batch_empty_reads_is_failure():
    # reads 가 비었거나 없으면 명확한 실패 (읽을 게 없음)
    r = tool_read_file({"reads": []})
    assert not r.success


# ── 스키마: read_file_reads 배열 only (단일 키 제거) ─────────


def test_schema_is_reads_array_only():
    props = ReadFileTool.parameters["properties"]
    assert "read_file_reads" in props
    assert ReadFileTool.parameters["required"] == ["read_file_reads"]
    # 단일 키는 전부 제거됨
    for k in (
        "read_file_path",
        "read_file_line_start",
        "read_file_line_end",
        "read_file_search",
        "read_file_context",
        "read_file_stat",
    ):
        assert k not in props


def test_schema_item_shape():
    reads = ReadFileTool.parameters["properties"]["read_file_reads"]
    assert reads["type"] == "array"
    item = reads["items"]
    assert item["required"] == ["path"]
    for k in ("path", "line_start", "line_end", "search", "context", "stat"):
        assert k in item["properties"]


# ── wire-key prefix 정합: run() 이 top-level prefix 만 벗긴다 ──


def test_run_strips_top_level_prefix_only(sample):
    # read_file_reads(top-level)는 prefix 벗겨 'reads' 로, item 내부 path 는 그대로
    r = ReadFileTool().run({"read_file_reads": [{"path": str(sample)}]})
    assert r.success
    assert "line1" in r.output


def test_run_multiple_via_prefix(tmp_path):
    f1 = tmp_path / "a.py"
    f1.write_text("aaa\n")
    f2 = tmp_path / "b.py"
    f2.write_text("bbb\n")
    r = ReadFileTool().run({"read_file_reads": [{"path": str(f1)}, {"path": str(f2)}]})
    assert r.success
    assert "aaa" in r.output and "bbb" in r.output
