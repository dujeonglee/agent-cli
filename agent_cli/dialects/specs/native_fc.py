"""native_fc 스펙 — 서버 네이티브 함수 호출 (docs/dialects/NATIVE.md, v10.2.0).

텍스트 블록이 아니라 API 의 함수 호출로 도구를 부른다: 요청에 `tools`(함수 스키마)를
싣고, 호출은 서버가 파싱한 `tool_calls` 로 받는다. 서버의 tool-call 파서가 `<tool_call>`
블록을 삼키는 게이트웨이(실측: LiteLLM + vLLM 류)에서도 돌고, 모델이 학습한 네이티브
포맷을 그대로 쓴다. 파싱 주체가 서버이므로 문법·구제·등가성 코퍼스·원문 기록이 없다.

내부 표현은 json_fc 와 같다(서버 `tool_calls` 를 루프가 flat op 배열 텍스트로 렌더해
같은 파서로 읽는다) — 스펙은 json_fc 를 복제하고 산문만 바꾼다.
"""

from __future__ import annotations

from dataclasses import replace

from agent_cli.dialects.base import NO_ACTION_FRAMING
from agent_cli.dialects.spec import Prose
from agent_cli.dialects.specs.json_fc import JSON_FC

_REMINDER_CALL = (
    "Call tools through the API's native function calling (the `tools` you were "
    "given) — never as text. To finish, call the `complete` function with your "
    "final answer as `result`."
)
_REMINDER_ACTION_REQUIRED = (
    "Every turn must call at least one function. If the task is DONE, call "
    "`complete` with the final answer as `result`. If your last message was the "
    "final answer in plain prose, call `complete` with that answer. Never stop "
    "without an explicit `complete` call."
)
_FRAMING_PARSE_FAIL = (
    "Your response contained no function call — tools must be called through the "
    "API's function calling, not written as text."
)
_NO_ACTION_DETAIL = "no function call reached the harness"

_FORMAT_RULES = """\

Tools are called through the API's native function calling — the `tools` list
attached to every request. Write brief reasoning as plain prose, then CALL the
functions you need; never write a tool call as text (no JSON arrays, no
<tool_call> tags — the server will not deliver them).

Batch independent work into ONE turn: every call that does NOT need another's
result goes in THIS turn as a separate function call (reading three files is
ONE turn with three calls). Split into separate turns only when a later step
needs an earlier step's result.

Rules:
1. Every turn must END with at least one function call (work, or `complete`
   to finish). Do NOT just stop after prose.
2. Each call acts on ONE target — to read N files, make N `read_file` calls.
3. When the task is DONE, call `complete` with your final answer as `result`.
4. If a result shows an error, fix the arguments and call again.
5. Respond in the user's language."""

NATIVE_FC = replace(
    JSON_FC,
    name="native_fc",
    server_parsed=True,
    prose=Prose(
        rules=_FORMAT_RULES,
        reminder_call=_REMINDER_CALL,
        reminder_action_required=_REMINDER_ACTION_REQUIRED,
        framing_parse_fail=_FRAMING_PARSE_FAIL,
        no_action_detail=_NO_ACTION_DETAIL,
        retry_no_json=f"{_FRAMING_PARSE_FAIL} {_REMINDER_CALL}",
        retry_no_action=(
            f"{NO_ACTION_FRAMING} ({_NO_ACTION_DETAIL}). {_REMINDER_ACTION_REQUIRED}"
        ),
        user_prefixes=(
            "Your response contained no function call",
            NO_ACTION_FRAMING,
        ),
    ),
)
