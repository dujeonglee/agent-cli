"""턴 레코드는 한 번 만들어지고 모든 기록 경로가 그걸 쓴다 (v10.42.0).

감사자 3차 실측에서 발견: native_fc 는 호출을 서버가 구조로 주므로 ``llm_text``
는 산문뿐인데, 관찰을 기록하는 ``_append_observation`` 은 원문을 다시 파싱하는
것이 기본이고 구조화 기록은 선택 인자였다. 일반 도구 경로만 그 인자를 넘겼고
가상 op(`reply`·`message`·`answer`·`ask`) 경로는 ``{"content": ""}`` 를 남겼다 —
history 에 `kind: raw` 빈 줄, 다음 호출의 prior 에 호출 없음, 모델은 "아직 회신
안 했다" 고 보고 **같은 reply 를 다시 보냈다**(여섯 감사 런 전부).

수리는 인터페이스다: ``_handle_text_path`` 가 ``ParsedTurn.record`` 를 한 번
만들고, ``_append_observation`` 은 원문 대신 그 레코드를 **필수**로 받는다.
아래는 그 계약을 디스패처가 특별 취급하는 op 전부에 대해 고정한다 — 목록은
손으로 적지 않고 ``_dispatch_op`` 의 소스에서 뽑는다(새 가상 op 가 생기면
여기 입력 표가 없어 바로 실패한다).
"""

from __future__ import annotations

import inspect
import json
import re
from unittest.mock import MagicMock

import pytest

from agent_cli.dialects import get as get_dialect
from agent_cli.dialects.base import Op
from agent_cli.loop import _append_observation
from agent_cli.loop.dispatch import TurnDispatcher
from tests.test_format_nudge_record import _dispatcher

#: 디스패처가 ``op.action == "<name>"`` 으로 갈라 다루는 op 마다 실행 가능한 입력.
SPECIAL_OP_INPUTS: dict[str, dict] = {
    "complete": {"result": "done"},
    "wait": {"reason": "job running"},  # 깨울 것이 없는 테스트 루프 → 거부 관찰
    "ask": {"questions": ["which one?"]},
    "run_skill": {"skill": "no-such-skill"},
    "message": {"to": "main", "text": "hi", "expects_reply": False},
    "reply": {"id": "r1", "text": "AUDIT run seq 1..8: pass"},
}


def _special_ops() -> set[str]:
    src = inspect.getsource(TurnDispatcher._dispatch_op)
    return set(re.findall(r'op\.action == "(\w+)"', src))


def _ports():
    port = MagicMock()
    port.nonblocking = True
    port.reply.return_value = ""
    port.ask.return_value = ("q1", "")
    return {"questions": port, "message_handler": lambda to, text, **kw: ""}


def _records(ctx) -> list[dict]:
    return [json.loads(ln) for ln in ctx.history_path.read_text().splitlines()]


def test_the_input_table_covers_every_special_cased_op():
    assert set(SPECIAL_OP_INPUTS) == _special_ops()


@pytest.mark.parametrize("action", sorted(SPECIAL_OP_INPUTS))
def test_server_parsed_special_op_is_recorded_with_its_ops(tmp_path, action):
    action_input = SPECIAL_OP_INPUTS[action]
    d, ctx, _st = _dispatcher(tmp_path, "native_fc", **_ports())
    d._handle_text_path("", server_ops=[Op(action=action, action_input=action_input)])
    recs = _records(ctx)
    assistant = [r for r in recs if r.get("role") == "assistant"][-1]
    ops = assistant.get("ops")
    assert ops and ops[0]["action"] == action, assistant
    if action != "complete":  # 종결 레코드는 answers 등을 더 싣는다
        assert ops == [{"action": action, "action_input": action_input}]
        # 관찰은 그 호출의 결과로 짝지어진다
        assert recs[-1].get("role") == "user" and recs[-1].get("tool") == action
    # 다음 호출의 prior: native 는 tool_calls 로 재렌더된다 — 모델이 자기 호출을 본다
    prior = ctx.dialect.render_assistant_from_history(assistant)
    assert prior["tool_calls"][0]["function"]["name"] == action


def test_text_dialect_path_is_unchanged(tmp_path):
    """json_fc(텍스트 파싱)는 종전처럼 llm_text 에서 ops 를 읽는다."""
    d, ctx, _st = _dispatcher(tmp_path, "json_fc", **_ports())
    d._handle_text_path('[{"action":"reply","id":"r1","text":"done"}]')
    assistant = [r for r in _records(ctx) if r.get("role") == "assistant"][-1]
    assert assistant.get("ops") == [
        {"action": "reply", "action_input": {"id": "r1", "text": "done"}}
    ]


def test_storing_an_emission_without_its_record_is_refused():
    """원문 재파싱 폴백은 없다 — 레코드 없이 저장하려 하면 조용히 빈 레코드를
    남기는 대신 실패한다. 저장하지 않는 개입(store_emission=False)만 None."""
    wf = get_dialect("json_fc")
    with pytest.raises(ValueError):
        _append_observation([], None, wf, None, "obs", tool_name="x", success=True)
    msgs: list[dict] = []
    _append_observation(
        msgs, None, wf, None, "obs", tool_name="", success=False, store_emission=False
    )
    assert msgs == [{"role": "user", "content": "obs"}]
