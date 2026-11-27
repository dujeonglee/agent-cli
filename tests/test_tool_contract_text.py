"""모델이 읽는 도구 계약 문장 — 코드와 맞춘 것들 (도구 계약 감사 묶음 1).

문장마다 감사 번호를 단다. 바꾸면 이 파일의 단언이 바뀌어야 한다 — 코드의 동작이 문장과
같은지는 그 도구의 테스트가 본다.
"""

from __future__ import annotations

import pytest

from agent_cli.dialects import get as get_dialect
from agent_cli.loop import ledger
from agent_cli.prompts.session_state import final_turn_notice
from agent_cli.prompts.system_prompt import _REPLY_DISCIPLINE_SECTION
from agent_cli.recovery.recursion import format_depth_limit_error
from agent_cli.subagent.agent_mail import build_reply_record
from agent_cli.subagent.agent_modes import RESUME_TASK_QUEUED, SPAWN_TASK_QUEUED
from agent_cli.subagent.run_agent import RUN_STARTED
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
    """3-5: 결과 모양 — 작은 덮어쓰기는 diff + 바뀐 줄(`write_file.py` 의 `is_small` 갈래)."""
    assert TOOLS["write_file"].description.endswith(
        "Returns the written file tagged LINE#HASH:content — the whole file when it "
        "is short, otherwise its first and last lines; a small change to an existing "
        "file returns the diff and the changed lines tagged instead. `read_file` "
        "shows the rest tagged."
    )


@pytest.mark.parametrize("name", ["json_fc", "xml_fc", "glm_argkey", "hermes_json"])
def test_format_rules_parameter_names(name):
    """3-e: 인자 이름은 Available Tools 에 있다 — 그 절은 형식 규칙 **아래**이고 지연 도구엔
    안내(guide)가 없다(뼈대만)."""
    flat = " ".join(get_dialect(name).format_rules().split())
    assert (
        "Use the parameter names listed in Available Tools (plain, no prefix)." in flat
    )
    assert "guide above" not in flat


def test_depth_refusal_names_the_hops():
    """깊이 거절문의 도구 이름 — `delegate` 는 옛 이름이다(v5 부터 `agent`)."""
    assert "Skill and agent hops share this counter." in format_depth_limit_error(
        "skill", "x", 3, 3
    )


# ── 답은 `reply` 하나 (감사 1-1·2-6·3-2·1-6, 제안서 PROP-no-result-fallback 3절) ──


def test_complete_says_where_result_goes():
    """#1 `complete` 설명 — `result` 가 어디로 가나."""
    assert (
        "`result` is the only text delivered as your answer — to the user, or to the "
        "agent that called you; what you wrote outside it is not."
        in TOOLS["complete"].description
    )


def test_message_points_answers_to_reply():
    """#2 `message` 설명 끝."""
    assert (
        "To answer a request you received, use `reply`. `complete` alone reports to "
        "no one." in TOOLS["message"].description
    )


def test_reply_description():
    """#3·1-6 `reply` 요약·설명."""
    assert TOOLS["reply"].summary == (
        "Answer a request someone is waiting on — another agent's `message` or the "
        "request you were given."
    )
    d = TOOLS["reply"].description
    assert d.startswith(
        "Reply to a request someone is waiting on — an agent's `message` that expects "
        "an answer, or the request you were given. `id` is the request's `r-…` from "
        "`## Open Items`; it alone says where the reply goes"
    )
    assert d.endswith(
        "`complete` alone reports to no one: reply before you complete. Refused when "
        "nothing is owed."
    )


def test_reply_discipline_first_rule():
    """#4 Reply Discipline 1."""
    flat = " ".join(_REPLY_DISCIPLINE_SECTION.split())
    assert (
        "1. `complete` reports to no one — it only ends your run. The answer for a "
        "requester goes out with `reply` (that request's id + your text), one reply "
        "per request, BEFORE you complete. A request from the user in your chat window "
        "is answered by `complete` itself (they see it there). 2." in flat
    )


def test_last_turn_says_who_receives_the_result():
    """#5 마지막 턴 안내의 끝 문장."""
    assert final_turn_notice(reports_to_caller=True).endswith(
        "The agent that called you receives only what you put in complete — "
        "anything you do not write there is lost."
    )
    assert final_turn_notice(reports_to_caller=False).endswith(
        "The user receives what you put in complete."
    )


def test_run_and_spawn_say_what_comes_back():
    """#6·#7 `agent` 결과 — 회신이 없으면 무엇이 오나."""
    assert (
        "(if it completes without one you get its run summary instead)" in RUN_STARTED
    )
    assert SPAWN_TASK_QUEUED.endswith(
        "(if it completes without one you get its run summary instead)."
    )
    assert RESUME_TASK_QUEUED.endswith("and wakes you.")


def test_open_items_tail():
    """#10 `## Open Items` 꼬리말."""
    assert ledger._TAIL == (
        "  The run stays open until every item is closed; your result is delivered "
        "either way."
    )


def test_harness_mail_says_what_to_do():
    """#11 감사 메일(2-6)."""
    rec = build_reply_record({"kind": "harness", "job": "auditor", "output": "x"})
    assert rec["content"].endswith(
        "(A harness job's result — nothing to send back. Act on findings if there are "
        "any; otherwise there is nothing to do.)"
    )
