"""code_index 배치(code_index_queries 배열-only) 테스트 — TDD.

설계 (read_file 배치와 동형):
- `_dispatch_one(query)`: 단일 query 를 mode handler 로 dispatch (기존 tool_code_index 본문).
- `tool_code_index(args)`: args["queries"] 배열을 순회. 1개면 단일 출력, 여러 개면
  mode별 헤더로 합성. 전부 실패할 때만 success=False.
- 배열 item 내부는 bare 키(mode/path/name/...), top-level code_index_queries 만 prefix.
- read-only 전체 모드가 배치 대상 (모드 섞기 허용).
"""

from __future__ import annotations

import pytest

from agent_cli.tools.code_index import (
    CodeIndexTool,
    _dispatch_one,
    tool_code_index,
)


def _write(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


@pytest.fixture
def project(tmp_path, monkeypatch):
    _write(
        tmp_path / "alpha.py",
        "def alpha():\n    return helper()\n\ndef helper():\n    return 1\n",
    )
    _write(
        tmp_path / "sub" / "beta.py",
        "class Beta:\n    def run(self):\n        alpha()\n",
    )
    monkeypatch.chdir(tmp_path)
    return tmp_path


# ── _dispatch_one: 단일 query 단위 ──────────────────────────


def test_dispatch_one_list(project):
    r = _dispatch_one({"mode": "list", "path": "alpha.py"})
    assert r.success
    assert "alpha" in r.output and "helper" in r.output


def test_dispatch_one_fetch(project):
    r = _dispatch_one({"mode": "fetch", "path": "alpha.py", "name": "alpha"})
    assert r.success
    assert "alpha" in r.output


def test_dispatch_one_missing_mode(project):
    r = _dispatch_one({"path": "alpha.py"})
    assert not r.success
    assert "mode" in r.error


def test_dispatch_one_unknown_mode(project):
    r = _dispatch_one({"mode": "explode"})
    assert not r.success
    assert "unknown mode" in r.error


# ── tool_code_index: queries 배열 순회 ──────────────────────


def test_batch_single_element_matches_dispatch_one(project):
    batch = tool_code_index({"queries": [{"mode": "list", "path": "alpha.py"}]})
    direct = _dispatch_one({"mode": "list", "path": "alpha.py"})
    assert batch.success
    assert batch.output == direct.output  # 1개 배열 = 단일 출력 (헤더 합성 없음)


def test_batch_multiple_queries(project):
    r = tool_code_index(
        {
            "queries": [
                {"mode": "list", "path": "alpha.py"},
                {"mode": "fetch", "path": "alpha.py", "name": "helper"},
            ]
        }
    )
    assert r.success
    assert "alpha" in r.output and "helper" in r.output


def test_batch_mixed_modes(project):
    # 다른 mode 를 한 배치에 섞을 수 있다 (검증 스파이크에서 모델이 실제로 그렇게 냄)
    r = tool_code_index(
        {
            "queries": [
                {"mode": "lookup", "name": "alpha"},
                {"mode": "list", "path": "alpha.py"},
            ]
        }
    )
    assert r.success
    assert "alpha" in r.output


def test_batch_partial_failure_is_success(project):
    # 하나라도 성공하면 success=True (전부 실패할 때만 False)
    r = tool_code_index(
        {
            "queries": [
                {"mode": "list", "path": "alpha.py"},
                {"mode": "list"},  # path 없음 → 이 query 만 실패
            ]
        }
    )
    assert r.success
    assert "alpha" in r.output  # 성공분 포함
    assert "path" in r.output  # 실패 사유 관찰 가능


def test_batch_all_failure_is_failure(project):
    r = tool_code_index({"queries": [{"mode": "list"}, {"mode": "fetch"}]})
    assert not r.success


def test_batch_empty_queries_is_failure(project):
    r = tool_code_index({"queries": []})
    assert not r.success


# ── 스키마: code_index_queries 배열 only (단일 키 제거) ──────


def test_schema_is_queries_array_only():
    props = CodeIndexTool.parameters["properties"]
    assert "code_index_queries" in props
    assert CodeIndexTool.parameters["required"] == ["code_index_queries"]
    for k in (
        "code_index_mode",
        "code_index_path",
        "code_index_name",
        "code_index_symbol_kind",
        "code_index_ref_kind",
        "code_index_search",
        "code_index_depth",
    ):
        assert k not in props


def test_schema_item_shape():
    queries = CodeIndexTool.parameters["properties"]["code_index_queries"]
    assert queries["type"] == "array"
    item = queries["items"]
    assert item["required"] == ["mode"]
    for k in ("mode", "path", "name", "symbol_kind", "ref_kind", "search", "depth"):
        assert k in item["properties"]
    # mode 는 enum (10 모드)
    assert "fetch" in item["properties"]["mode"]["enum"]


# ── wire-key prefix 정합 ────────────────────────────────────


def test_run_strips_top_level_prefix_only(project):
    r = CodeIndexTool().run(
        {"code_index_queries": [{"mode": "list", "path": "alpha.py"}]}
    )
    assert r.success
    assert "alpha" in r.output


def test_run_multiple_via_prefix(project):
    r = CodeIndexTool().run(
        {
            "code_index_queries": [
                {"mode": "list", "path": "alpha.py"},
                {"mode": "fetch", "path": "alpha.py", "name": "alpha"},
            ]
        }
    )
    assert r.success
    assert "alpha" in r.output
