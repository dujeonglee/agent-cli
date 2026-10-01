"""hermes_json 스펙 — ``<tool_call>{"name": …, "arguments": {…}}</tool_call>`` (Phase 5 S4).

가족 ① Hermes JSON: NousResearch Hermes 2 Pro/3/4 가 정하고 Qwen2.5·Qwen3·Qwen3-Next,
Granite 4.0/4.1, SmolLM3 가 그대로 쓴 모양 — 2026 년 현재 가장 넓게 지원되는 네이티브
프라이어(조사 리포트 §2 가족 ①). 산문 조각은 xml_fc 의 것을 이 모양으로 옮긴 초안이며
**실모델 미검증**(PHASE5 D3 — 로컬 omlx 는 Qwen3.8 XML 프라이어뿐). 파서·문법·foreign
구제 테스트로만 들어간다.
"""

from __future__ import annotations

from agent_cli.dialects.base import NO_ACTION_FRAMING
from agent_cli.dialects.spec import ArgStyle, DialectSpec, Lenient, NameSlot, Prose

_REMINDER_CALL = (
    "Respond with one or more <tool_call> blocks, each containing ONE JSON "
    'object {"name": "<tool>", "arguments": {...}}. To finish, call '
    '{"name": "complete", "arguments": {"result": "<final answer>"}}.'
)
_REMINDER_ACTION_REQUIRED = (
    'Each <tool_call> object must have a "name" naming one tool from '
    "Available Tools. If the task is DONE, finish with "
    '{"name": "complete", "arguments": {"result": "<final answer>"}}. If your '
    "last message already was the final answer in plain prose, re-emit that "
    "answer as the result argument. If you were about to do something, emit "
    "that tool call now. Never stop without an explicit complete."
)
_FRAMING_PARSE_FAIL = (
    "Your response contained no parseable <tool_call> block — tool calls must "
    'be a JSON object {"name": ..., "arguments": {...}} inside <tool_call> tags.'
)
_NO_ACTION_DETAIL = 'no <tool_call> whose "name" is a tool from Available Tools'

_FORMAT_RULES = """\
## Response Format

Write brief reasoning as plain prose, then emit your tool calls:

Your reasoning goes here, as plain prose. No tags around it.

<tool_call>
{"name": "TOOL_NAME", "arguments": {"PARAM_NAME": "value"}}
</tool_call>

Each <tool_call> block holds exactly ONE JSON object with "name" (the tool)
and "arguments" (an object of its parameters). Use the parameter names shown
in each tool's guide above (plain, no prefix). Values are JSON: strings are
quoted and escaped (\\n for newlines, \\" for quotes), numbers and booleans
are bare.

Batch independent work into ONE turn. Before you emit, look at everything
you intend to do: every operation that does NOT need another's output goes
in THIS turn as a separate <tool_call> block. Reading three files, or a
read plus an unrelated search, is ONE turn — not three; batching saves
turns and context budget. Split into separate turns ONLY when a later step
needs an earlier step's result (then emit just the first now — its
observation arrives next turn).

Rules:
1. Every turn must contain at least one <tool_call> block (work, or
   `complete` to finish).
2. Each "name" is exactly one tool from Available Tools.
3. Each call acts on ONE target. To read N files, emit N separate
   <tool_call> blocks in the SAME turn — never a list inside one call.
4. Close every block: </tool_call>. One JSON object per block, nothing else
   inside the tags.
5. When the task is DONE, end with a `complete` call carrying your final
   answer:
<tool_call>
{"name": "complete", "arguments": {"result": "your final answer"}}
</tool_call>
   Always finish this way — do NOT just stop.
6. If an observation shows an error, fix arguments and retry.
7. Respond in the user's language.

Several independent operations in one turn (read two files at once — they
don't depend on each other):
To see how auth and session fit together I need both files; neither
depends on the other, so read them together.

<tool_call>
{"name": "read_file", "arguments": {"path": "src/auth.py"}}
</tool_call>
<tool_call>
{"name": "read_file", "arguments": {"path": "src/session.py"}}
</tool_call>

Finishing the task:
The login() function is implemented and the tests pass.

<tool_call>
{"name": "complete", "arguments": {"result": "Implemented login() in src/auth.py; all tests pass."}}
</tool_call>"""

HERMES_JSON = DialectSpec(
    name="hermes_json",
    call=("<tool_call>", "</tool_call>"),
    name_slot=NameSlot.JSON_KEY,
    args=ArgStyle.JSON_IN_TAG,
    value_mode="json",
    op_shape="name_arguments",
    prose_opener="<tool_call>",
    degeneration_trigger="<",
    forbid_in_think=("<tool_call>",),
    lenient=Lenient(),
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
            "Your response contained no parseable <tool_call> block",
            NO_ACTION_FRAMING,
        ),
    ),
)
