"""수트 가드(``tests/cwd_guard.py``)가 새 항목을 실제로 가려내는지."""

from __future__ import annotations

from tests import cwd_guard


def _tree(root, *rels):
    for rel in rels:
        path = root / rel
        if rel.endswith("/"):
            path.mkdir(parents=True, exist_ok=True)
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("x", encoding="utf-8")


def test_new_root_file_is_reported(tmp_path):
    _tree(tmp_path, "README.md", ".agent-cli/skills/a/SKILL.md")
    before = cwd_guard.snapshot(tmp_path)
    _tree(tmp_path, "out.txt")
    after = cwd_guard.snapshot(tmp_path)
    assert cwd_guard.new_entries(before, after) == ["out.txt"]


def test_agent_cli_two_levels_are_reported(tmp_path):
    _tree(tmp_path, ".agent-cli/skills/")
    before = cwd_guard.snapshot(tmp_path)
    _tree(
        tmp_path, ".agent-cli/sessions/sess-1/history.jsonl", ".agent-cli/snapshot.git/"
    )
    after = cwd_guard.snapshot(tmp_path)
    assert cwd_guard.new_entries(before, after) == [
        ".agent-cli/sessions",
        ".agent-cli/sessions/sess-1",
        ".agent-cli/snapshot.git",
    ]


def test_new_child_of_existing_session_dir_is_reported(tmp_path):
    # 이미 있던 ``sessions/`` 아래에 새 세션이 생겨도 잡는다(두 번째 단계).
    _tree(tmp_path, ".agent-cli/sessions/old/")
    before = cwd_guard.snapshot(tmp_path)
    _tree(tmp_path, ".agent-cli/sessions/sess-1/")
    after = cwd_guard.snapshot(tmp_path)
    assert cwd_guard.new_entries(before, after) == [".agent-cli/sessions/sess-1"]


def test_hypothesis_db_is_not_ignored(tmp_path):
    before = cwd_guard.snapshot(tmp_path)
    _tree(tmp_path, ".hypothesis/examples/")
    after = cwd_guard.snapshot(tmp_path)
    assert cwd_guard.new_entries(before, after) == [".hypothesis"]


def test_caches_and_tracked_skills_are_ignored(tmp_path):
    _tree(tmp_path, ".agent-cli/skills/")
    before = cwd_guard.snapshot(tmp_path)
    _tree(
        tmp_path,
        "__pycache__/",
        ".pytest_cache/",
        ".ruff_cache/",
        ".agent-cli/__pycache__/",
        ".agent-cli/skills/new-skill/SKILL.md",
    )
    after = cwd_guard.snapshot(tmp_path)
    assert cwd_guard.new_entries(before, after) == []


def test_removed_entries_are_not_reported(tmp_path):
    _tree(tmp_path, "gone.txt")
    before = cwd_guard.snapshot(tmp_path)
    (tmp_path / "gone.txt").unlink()
    after = cwd_guard.snapshot(tmp_path)
    assert cwd_guard.new_entries(before, after) == []


def test_missing_root_snapshot_is_empty(tmp_path):
    assert cwd_guard.snapshot(tmp_path / "nope") == frozenset()
