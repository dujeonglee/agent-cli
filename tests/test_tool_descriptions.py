"""도구 설명은 **성질**만 (v10.74.0, docs/tail/DESIGN.md §9).

"언제 이 도구를 고르나" 는 꼬리(상시 한 줄·상태 블록·관찰 교정)가 맡는다 — 머리의 설명은
도구를 고른 뒤 읽는 글이라 사용 규칙을 둬도 지켜지지 않았다(python, 2026-10-25 A/B).
작업 종류 나열("a test suite, a build…", "OCR")과 실행 파일 이름도 과제·환경에 묶이므로
모델이 읽는 글에 두지 않는다.
"""

from __future__ import annotations

import pytest

from agent_cli.tools.registry import TOOLS

#: 사용 규칙을 드러내는 어구 — 이 넷의 설명·인자 설명에 나타나면 실패.
USAGE_PHRASES = (
    "Use it for",
    "Use for ",
    "prefer `",
    "Do not raise",
    "keep working meanwhile",
)
#: 작업 종류 나열·낡은 요리법.
TASK_LISTS = (
    "a test suite, a build",
    "OCR",
    "nohup",
    "/tmp/x",
    "PYTHONUNBUFFERED",
    "EXIT:",
)


def _all_text(tool) -> str:
    parts = [tool.description or ""]
    for p in (getattr(tool, "parameters", {}) or {}).get("properties", {}).values():
        parts.append(p.get("description") or "")
    return "\n".join(parts)


@pytest.mark.parametrize("name", ["shell", "monitor", "write_file", "python"])
def test_description_carries_properties_only(name):
    text = _all_text(TOOLS[name])
    for phrase in USAGE_PHRASES + TASK_LISTS:
        assert phrase not in text, f"{name}: {phrase!r}"


def test_shell_describes_the_background_shape_not_when_to_use_it():
    d = TOOLS["shell"].description
    assert "job id, pid and log path" in d
    assert "longer than about a minute" not in d  # 경로 선택은 꼬리 작업 줄
    from agent_cli.prompts.system_prompt import TASK_GUIDELINES

    assert "longer than about a minute" in TASK_GUIDELINES  # 한 곳에만


def test_monitor_lost_the_shell_recipe_but_kept_its_semantics():
    d = TOOLS["monitor"].description
    assert "command + every" in d and "file + pattern" in d and "file + idle" in d
    assert "deadline defaults to 2h" in d
    assert "not saved with the session" in d and "`schedule`" in d


def test_write_file_states_cost_and_the_way_out():
    d = TOOLS["write_file"].description
    assert "passes through your context twice" in d
    assert "If the same edit keeps failing on a small file, rewriting it is fine" in d
    assert "LINE#HASH:content" in d
