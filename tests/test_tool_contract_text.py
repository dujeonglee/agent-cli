"""모델이 읽는 도구 계약 문장 — 코드와 맞춘 것들 (도구 계약 감사 묶음 1).

문장마다 감사 번호를 단다. 바꾸면 이 파일의 단언이 바뀌어야 한다 — 코드의 동작이 문장과
같은지는 그 도구의 테스트가 본다.
"""

from __future__ import annotations

import pytest

from agent_cli.dialects import get as get_dialect
from agent_cli.recovery.recursion import format_depth_limit_error
from agent_cli.tools import history
from agent_cli.tools.registry import TOOLS


def test_history_description_oversized():
    """1-9: 큰 결과는 공통 과대 출력 규칙(파일 + 발췌)을 탄다 — "refused" 라고 하지 않는다."""
    d = TOOLS["history"].description
    assert (
        "Rows come back whole, so project and limit (substr of `text`, a LIMIT) and "
        "fetch the one row you need afterwards. Omit `query`" in d
    )
    assert "refused" not in d


def test_history_help_table():
    """3-4: 무인자 도움말의 표 설명 — 도구 설명과 같게(행은 레코드마다, kind 에 `system`)."""
    out = history._help(None).output
    assert "Table `history` (one row per record):" in out
    assert (
        "  kind: query=user ask · action=tool-call turn · observation=tool result"
        " · final=complete answer · raw=unparsed · system=system prompt\n" in out
    )


def test_write_file_description_result():
    """3-5: 결과 모양."""
    assert TOOLS["write_file"].description.endswith(
        "Returns the written file tagged LINE#HASH:content — the whole file when it "
        "is short, otherwise its first and last lines; `read_file` shows the rest tagged."
    )


@pytest.mark.parametrize("name", ["json_fc", "xml_fc", "glm_argkey", "hermes_json"])
def test_format_rules_parameter_names(name):
    """3-e: 인자 이름을 어디서 보는가."""
    flat = " ".join(get_dialect(name).format_rules().split())
    assert (
        "Use the parameter names shown in each tool's guide above (plain, no prefix)."
        in flat
    )


def test_depth_refusal_names_the_hops():
    """깊이 거절문의 도구 이름."""
    assert "Skill and delegate hops share this counter." in format_depth_limit_error(
        "skill", "x", 3, 3
    )
