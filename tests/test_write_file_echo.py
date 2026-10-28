"""write_file v10.77.0 — 결과 echo 크기 · 넛지 문장 · write 가 있으면 edit 도.

- 40줄 이하 새 파일은 전체를 해시라인으로, 넘으면 처음·끝 10줄만(나머지는 read_file).
- 30% 미만 덮어쓰기 넛지는 명령이 아니라 사실.
- write_file 만 허용한 루프에도 edit_file 이 붙는다(더 작은 권한, ``mounted_with``).
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from agent_cli.tools.write_file import tool_write_file


def test_short_file_echoes_every_line(tmp_path):
    p = tmp_path / "s.py"
    out = tool_write_file({"path": str(p), "content": "a\nb\nc"}).output
    head, rest = out.split("\n", 1)
    assert head == f"File saved: {p} (3 lines, 5 bytes)"
    assert rest.startswith("To modify, call edit_file with the hashline refs below")
    assert "\n1#" in out and "\n3#" in out


def test_long_file_echoes_head_and_tail_only(tmp_path):
    p = tmp_path / "big.py"
    content = "\n".join(f"line {i}" for i in range(1, 101))
    out = tool_write_file({"path": str(p), "content": content}).output
    lines = out.split("\n")
    assert lines[0] == f"File saved: {p} (100 lines, {len(content):,} bytes)"
    assert lines[1] == (
        "Lines 1–10 and 91–100 are tagged below for edit_file; read_file shows "
        "the rest tagged."
    )
    tagged = [ln.split("#", 1)[0] for ln in lines[2:] if "#" in ln]
    assert tagged == [str(i) for i in (*range(1, 11), *range(91, 101))]
    assert "…" in lines


def test_small_overwrite_note_states_a_fact(tmp_path):
    p = tmp_path / "f.txt"
    body = "\n".join(f"l{i}" for i in range(40))
    p.write_text(body)
    out = tool_write_file({"path": str(p), "content": body + "\nx"}).output
    assert "[note] Only ~2% of lines changed (1/41)." in out
    assert "next time" not in out.lower()


def test_write_only_loop_also_gets_edit_file():
    from agent_cli.dialects import get as get_dialect
    from agent_cli.loop import AgentLoop
    from agent_cli.providers.capabilities import ModelCapabilities
    from tests.loop_ports import TEST_PORTS

    loop = AgentLoop(
        ports=TEST_PORTS,
        query="Q",
        provider=MagicMock(),
        capabilities=ModelCapabilities(
            context_window=32768, max_output_tokens=4096, supports_thinking=False
        ),
        model="m",
        dialect=get_dialect("json_fc"),
        active_tools=["read_file", "write_file", "shell", "complete"],
    )
    assert "edit_file" in loop._llm.cfg.tools_list
    loop = AgentLoop(
        ports=TEST_PORTS,
        query="Q",
        provider=MagicMock(),
        capabilities=ModelCapabilities(
            context_window=32768, max_output_tokens=4096, supports_thinking=False
        ),
        model="m",
        dialect=get_dialect("json_fc"),
        active_tools=["read_file", "edit_file", "shell", "complete"],
    )
    # 거꾸로(edit 만)는 그대로 — 더 작은 권한만 준 것
    assert "write_file" not in loop._llm.cfg.tools_list


def test_builtin_skills_name_edit_file_next_to_write_file():
    root = Path(__file__).resolve().parent.parent / "agent_cli" / "skills" / "builtin"
    for name in (
        "plan.md",
        "orchestrate.md",
        "create-agent.md",
        "create-skill/SKILL.md",
    ):
        head = (root / name).read_text().split("---", 2)[1]
        line = next(ln for ln in head.splitlines() if ln.startswith("allowed-tools"))
        assert "write_file" in line and "edit_file" in line, name
