"""read_file flat-native 테스트 (consolidation 로드맵 Step 3).

read_file 은 flat single-file 인터페이스다 — batch `read_file_reads` 배열·
`read_file_` wire-key prefix 둘 다 없음. 한 op 가 한 파일을 읽고, 여러 파일은
multi-op 포맷이 read_file op 를 여러 개 내서 읽는다.

- `_read_one(spec)`: 파일 하나를 읽는 per-file 단위 (모드 분기).
- 스키마: flat `{path, line_start, line_end, search, context, stat}`, required `["path"]`.
- `wrap_single_op` = identity (canonical 재포장 없음). `key_prefix` 유지 →
  flat 키엔 strip no-op, `claims` 는 flat `{path}` 에 False.
"""

from __future__ import annotations

import pytest

from agent_cli.tools.read_file import ReadFileTool, _read_one


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


# ── 스키마: flat single-file (batch 배열·prefix 제거) ─────────


def test_schema_is_flat_single_file():
    props = ReadFileTool.parameters["properties"]
    assert ReadFileTool.parameters["required"] == ["path"]
    # flat 필드가 top-level 에 직접 산다 (배열 wrapper 없음)
    for k in ("path", "line_start", "line_end", "search", "context", "stat"):
        assert k in props
    # 옛 batch 배열 키·prefix 키는 전부 사라짐
    assert "read_file_reads" not in props
    assert "read_file_path" not in props


# ── wrap_single_op = identity, dispatch 는 flat 직결 ──────────


def test_wrap_single_op_is_identity():
    flat = {"path": "a.py", "stat": True}
    assert ReadFileTool().wrap_single_op(flat) == flat


def test_run_dispatches_flat_input(sample):
    r = ReadFileTool().run({"path": str(sample)})
    assert r.success
    assert "line1" in r.output and "line5" in r.output


def test_run_flat_with_mode(sample):
    r = ReadFileTool().run({"path": str(sample), "line_start": 2, "line_end": 3})
    assert r.success
    assert "line2" in r.output and "line4" not in r.output


def test_touched_paths_single(sample):
    assert ReadFileTool().touched_paths({"path": str(sample)}) == [str(sample)]


def test_summary_arg_single(sample):
    assert ReadFileTool().summary_arg({"path": str(sample)}) == str(sample)


def test_claims_false_on_flat_keys():
    # flat `{path}` 엔 `read_file_` prefix 가 없으므로 claims 는 False —
    # infer_action 오염 없음 (write_file 와 동일한 latent-prefix 보장).
    assert ReadFileTool().claims({"path": "x", "stat": True}) is False
