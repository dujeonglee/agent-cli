"""변화 없는 편집 (v10.77.1) — 새 `lines` 가 그 자리의 줄과 같으면 쓰지 않고 그렇다고 말한다.

라이브 GLM(v10.77.0 S4): 중복된 두 줄(`5#QK`·`6#QK`) 중 하나를 같은 내용으로 replace 하고
diff 없는 "Edit complete" 를 받아, 바뀐 줄 알고 다시 읽고 같은 편집을 한 번 더 보냈다.
"""

from __future__ import annotations

from agent_cli.tools.edit_file import apply_edits_batch_parts, tool_edit_file
from agent_cli.tools.read_file import compute_line_hash

DUP = [
    "def fib(n):",
    '    """doc"""',
    "    if n < 0:",
    '        raise ValueError("neg")',
    "    a, b = 0, 1",
    "    a, b = 0, 1",
    "    for _ in range(n):",
    "        a, b = b, a + b",
    "    return a",
]


def _ref(n, lines):
    return f"{n}#{compute_line_hash(n, lines[n - 1])}"


def _file(tmp_path, lines):
    p = tmp_path / "fib.py"
    p.write_text("\n".join(lines))
    return p


def test_same_content_replace_writes_nothing_and_says_so(tmp_path):
    p = _file(tmp_path, DUP)
    before = p.stat().st_mtime_ns
    r = tool_edit_file(
        {
            "path": str(p),
            "op": "replace",
            "pos": _ref(5, DUP),
            "lines": ["    a, b = 0, 1"],
        }
    )
    assert r.success
    assert r.output.startswith(f"No change: '{p}' is unchanged")
    assert "the same as line 5 already there. Nothing was written." in r.output
    # 옆의 같은 줄 — 태그가 같다는 사실과 번호가 고른다는 것
    assert "Lines 5 and 6 have the same content, so they share a tag" in r.output
    assert '`op` "delete"' in r.output and "give `end`" in r.output
    assert "Edit complete" not in r.output
    assert p.read_text() == "\n".join(DUP) and p.stat().st_mtime_ns == before


def test_unique_line_no_change_has_no_twin_sentence(tmp_path):
    lines = ["a = 1", "b = 2", "c = 3"]
    p = _file(tmp_path, lines)
    r = tool_edit_file(
        {"path": str(p), "op": "replace", "pos": _ref(2, lines), "lines": ["b = 2"]}
    )
    assert r.success and r.output.startswith("No change:")
    assert "share a tag" not in r.output


def test_a_real_change_still_reports_edit_complete(tmp_path):
    p = _file(tmp_path, DUP)
    r = tool_edit_file(
        {
            "path": str(p),
            "op": "replace",
            "pos": _ref(5, DUP),
            "end": _ref(6, DUP),
            "lines": ["    a, b = 0, 1"],
        }
    )
    assert r.success and r.output.startswith("Edit complete:")
    assert p.read_text().count("a, b = 0, 1") == 1


def test_batch_of_only_no_change_edits_reports_each_span(tmp_path):
    lines = ["a = 1", "b = 2", "c = 3"]
    p = _file(tmp_path, lines)
    combined, parts = apply_edits_batch_parts(
        str(p),
        [
            {"op": "replace", "pos": _ref(1, lines), "lines": ["a = 1"]},
            {"op": "replace", "pos": _ref(3, lines), "lines": ["c = 3"]},
        ],
    )
    assert combined.success
    assert "the same as line 1, line 3 already there" in combined.output
    assert all("No change:" in pt["content"] for pt in parts)
