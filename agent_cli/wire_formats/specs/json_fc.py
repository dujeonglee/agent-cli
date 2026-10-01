"""json_fc 스펙 — 산문 thought + flat op JSON 배열 (기본 포맷).

PHASE4 의 손 코딩 모듈을 데이터로 다시 쓴 것 (Phase 5 S3). 산문 조각은 옛 모듈의
텍스트를 **바이트 그대로** 옮겼다. md_array 헤더 관용은 여기서 끝났다(결정 1) —
헤더 반복 러너웨이 감지·센티널 라인 제거는 누출 위생이라 엔진에 남는다.
json_fc 는 우리 고유 모양이라 어느 모델의 네이티브 프라이어와도 바이트 일치하지
않는다(JSON 프라이어 모델에 "구제 가능한 거리").
"""

from __future__ import annotations

from agent_cli.wire_formats.base import NO_ACTION_FRAMING
from agent_cli.wire_formats.spec import ArgStyle, DialectSpec, NameSlot, Prose

_REMINDER_CALL = 'Respond with plain-prose reasoning followed by ONE JSON array of {"action": ..., params} ops. To finish, use a `complete` op: {"action": "complete", "result": "<final answer>"}.'
_REMINDER_ACTION_REQUIRED = 'Each array element must include an "action" field naming one tool from Available Tools. If the task is DONE, emit a `complete` op: {"action": "complete", "result": "<final answer>"}. If your last message already was the final answer in plain prose, re-emit that answer as the `result`. If you were about to do something, emit that tool call now. Never stop without an explicit `complete`.'
_FRAMING_PARSE_FAIL = "Your response did not match the expected format — it must end with a valid JSON array of tool calls."
_NO_ACTION_DETAIL = (
    'no JSON array, or no op whose "action" names a tool from Available Tools'
)
_RETRY_NO_JSON_TAIL = (
    "Plain prose then ONE JSON array — no markdown headers (## ...), no HTML/XML tags."
)

_FORMAT_RULES = """\
## Response Format

Write brief reasoning as plain prose, then end your turn with ONE JSON
array of tool calls:

Your reasoning goes here, as plain prose.

[{"action": "<tool name>", <its parameters>}]

Each array element is one tool call: {"action": ..., params}. Use the
parameter names shown in each tool's guide above (plain, no prefix).

Batch independent work into ONE turn. Before you emit, look at everything
you intend to do: every operation that does NOT need another's output goes
in THIS turn as a separate array element. Reading three files, or a read
plus an unrelated search, is ONE turn — not three; batching saves turns and
context budget. Split into separate turns ONLY when a later step needs an
earlier step's result (then emit just the first now — its observation
arrives next turn).

Rules:
1. Reasoning (optional) is plain prose BEFORE the array — never after it.
2. Every turn must END with one JSON array containing at least one op
   (work, or `complete` to finish). Do NOT just stop after prose.
3. Each op must have an "action" naming one tool.
4. Each op acts on ONE target. To read N files, emit N separate
   {"action": "read_file", "path": ...} ops in the SAME array. NEVER put a
   list of items inside a single op (no nested arrays).
5. When the task is DONE, end with a `complete` op carrying your final
   answer: {"action": "complete", "result": "<your final answer>"}.
6. NEVER use HTML/XML tags of ANY kind — no <tool_call>, <function_call>,
   <div>, <answer>, or anything tag-shaped — and NO markdown headers
   (## ...). This protocol is plain prose + ONE JSON array, NOTHING else.
   A turn containing tags or headers is UNPARSEABLE and completely wasted —
   if you feel the urge to open a tag, write the JSON array instead.
7. If an observation shows an error, fix parameters and retry.
8. Respond in the user's language.

Several independent operations in one turn (read three files at once —
they don't depend on each other):
To see how auth, session, and the login route fit together I need all
three files; none depends on another's output, so read them together.

[{"action": "read_file", "path": "src/auth.py"}, {"action": "read_file", "path": "src/session.py"}, {"action": "read_file", "path": "src/routes/login.py"}]

Finishing the task:
The login() function is implemented and the tests pass.

[{"action": "complete", "result": "Implemented login() in src/auth.py; all tests pass."}]"""

JSON_FC = DialectSpec(
    name="json_fc",
    call=None,
    name_slot=NameSlot.JSON_KEY,
    args=ArgStyle.JSON_NATIVE,
    value_mode="json",
    op_shape="flat_action",
    prose_opener="[",
    prose_after_blank_line=True,
    # 러너웨이 시그니처는 옛 md_array 헤더 반복(`## Thought`/`## Action`) — 문법이
    # 꺼진 서버에서 옛 프라이어 누출은 여전히 가능하므로 감지는 남긴다(위생).
    degeneration_trigger="#",
    prose=Prose(
        rules=_FORMAT_RULES,
        reminder_call=_REMINDER_CALL,
        reminder_action_required=_REMINDER_ACTION_REQUIRED,
        framing_parse_fail=_FRAMING_PARSE_FAIL,
        no_action_detail=_NO_ACTION_DETAIL,
        retry_no_json=f"{_FRAMING_PARSE_FAIL} {_REMINDER_CALL} {_RETRY_NO_JSON_TAIL}",
        retry_no_action=(
            f"{NO_ACTION_FRAMING} ({_NO_ACTION_DETAIL}). {_REMINDER_ACTION_REQUIRED}"
        ),
        user_prefixes=(
            "Your response did not match the expected format",
            NO_ACTION_FRAMING,
            "Your JSON array had no usable tool call",  # ≤ v9.24.2 문구 — 옛 세션 resume
        ),
    ),
)
