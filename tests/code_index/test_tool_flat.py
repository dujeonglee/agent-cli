"""code_index flat-native 테스트 (consolidation 로드맵 Step 3).

read_file 와 동형: code_index 는 flat single-query 인터페이스다 — batch
`code_index_queries` 배열·`code_index_` wire-key prefix 둘 다 없음. 한 op 가
한 쿼리를 돌리고, 여러 쿼리는 multi-op 포맷이 code_index op 을 여러 개 emit
(읽기전용이라 순서/상태 의존 없음).

- `_dispatch_one(query)`: 단일 query 를 mode handler 로 dispatch (per-query 단위).
- 스키마: flat `{mode, path?, name?, symbol_kind?, ...}`, required `["mode"]`.
- `wrap_single_op` = identity, `key_prefix` 유지(latent).
"""

from __future__ import annotations

import pytest

from agent_cli.tools.code_index import CodeIndexTool, _dispatch_one


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


# ── 스키마: flat single-query (batch 배열·prefix 제거) ────────


def test_schema_is_flat_single_query():
    props = CodeIndexTool.parameters["properties"]
    assert CodeIndexTool.parameters["required"] == ["mode"]
    for k in ("mode", "path", "name", "symbol_kind", "ref_kind", "search", "depth"):
        assert k in props
    # mode 는 enum (10 모드)
    assert "fetch" in props["mode"]["enum"]
    # 옛 batch 배열 키·prefix 키는 사라짐
    assert "code_index_queries" not in props
    assert "code_index_mode" not in props


# ── wrap_single_op = identity, dispatch 는 flat 직결 ──────────


def test_wrap_single_op_is_identity():
    flat = {"mode": "list", "path": "a.py"}
    assert CodeIndexTool().wrap_single_op(flat) == flat


def test_run_dispatches_flat_input(project):
    r = CodeIndexTool().run({"mode": "list", "path": "alpha.py"})
    assert r.success
    assert "alpha" in r.output


def test_touched_paths_single():
    assert CodeIndexTool().touched_paths({"mode": "list", "path": "a.py"}) == ["a.py"]
    # path-less mode → no touched path
    assert CodeIndexTool().touched_paths({"mode": "lookup", "name": "X"}) == []


def test_summary_arg_single():
    assert (
        CodeIndexTool().summary_arg({"mode": "fetch", "path": "a.py"}) == "fetch a.py"
    )


def test_claims_false_on_flat_keys():
    # flat `{mode}` 엔 `code_index_` prefix 가 없으므로 claims 는 False —
    # infer_action 오염 없음 (다른 flat-native 도구와 동일한 latent-prefix 보장).
    assert CodeIndexTool().claims({"mode": "list", "path": "a.py"}) is False
