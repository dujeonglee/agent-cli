"""glm_argkey 스펙 — ``<tool_call>NAME<arg_key>k</arg_key><arg_value>v</arg_value></tool_call>`` (Phase 5 S4).

가족 ⑤ 변형, GLM-4.5 ~ 5.3(Zhipu/Z.ai)의 네이티브 모양: 이름은 여는 태그 바로 뒤
첫 토큰(``NameSlot.BODY_HEAD``), 인자는 키·값 **쌍 태그**(``ArgStyle.TAGGED_PAIR``),
값은 raw 텍스트. 4.5/4.6 은 여러 줄, 4.7 부터 한 줄 — 파서·문법 모두 태그 경계
공백을 허용해 둘 다 읽는다. 결과 쪽 ``<|observation|>`` 는 모델 템플릿 토큰이라
우리와 무관(관찰은 user 메시지). 산문 조각은 초안이며 **실모델 미검증**(PHASE5 D3).
"""

from __future__ import annotations

from agent_cli.wire_formats.base import NO_ACTION_FRAMING
from agent_cli.wire_formats.spec import ArgStyle, DialectSpec, Lenient, NameSlot, Prose

_REMINDER_CALL = (
    "Respond with one or more <tool_call> blocks: the tool name right after "
    "<tool_call>, then <arg_key>NAME</arg_key><arg_value>value</arg_value> "
    "pairs. To finish, call complete with a result argument."
)
_REMINDER_ACTION_REQUIRED = (
    "The word right after <tool_call> must name one tool from Available "
    "Tools. If the task is DONE, finish with <tool_call>complete and "
    "<arg_key>result</arg_key><arg_value>your final answer</arg_value>. If "
    "your last message already was the final answer in plain prose, re-emit "
    "that answer as the result argument. If you were about to do something, "
    "emit that tool call now. Never stop without an explicit complete."
)
_FRAMING_PARSE_FAIL = (
    "Your response contained no parseable <tool_call> block — tool calls must "
    "use <tool_call>NAME with <arg_key>/<arg_value> pairs."
)
_NO_ACTION_DETAIL = "no <tool_call> naming a tool from Available Tools"

_FORMAT_RULES = """\
## Response Format

Write brief reasoning as plain prose, then emit your tool calls:

Your reasoning goes here, as plain prose. No tags around it.

<tool_call>TOOL_NAME
<arg_key>PARAM_NAME</arg_key>
<arg_value>value</arg_value>
</tool_call>

The tool name comes right after <tool_call>. Each parameter is one
<arg_key>…</arg_key> followed by its <arg_value>…</arg_value>. Values are
RAW text: write file contents, code, and multi-line text directly between
the tags with NO escaping — no \\n, no quote escaping, no JSON. For a
multi-line value put it on its own lines:
<arg_key>content</arg_key>
<arg_value>
line one
line two
</arg_value>
Use the parameter names shown in each tool's guide above (plain, no
prefix).

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
2. The name after <tool_call> is exactly one tool from Available Tools.
3. Each call acts on ONE target. To read N files, emit N separate
   <tool_call> blocks in the SAME turn — never a list inside one call.
4. Close every tag: </arg_key>, </arg_value>, </tool_call>.
5. When the task is DONE, end with a `complete` call carrying your final
   answer:
<tool_call>complete
<arg_key>result</arg_key>
<arg_value>
your final answer
</arg_value>
</tool_call>
   Always finish this way — do NOT just stop.
6. If an observation shows an error, fix parameters and retry.
7. Respond in the user's language.

Several independent operations in one turn (read two files at once — they
don't depend on each other):
To see how auth and session fit together I need both files; neither
depends on the other, so read them together.

<tool_call>read_file
<arg_key>path</arg_key>
<arg_value>src/auth.py</arg_value>
</tool_call>
<tool_call>read_file
<arg_key>path</arg_key>
<arg_value>src/session.py</arg_value>
</tool_call>

Finishing the task:
The login() function is implemented and the tests pass.

<tool_call>complete
<arg_key>result</arg_key>
<arg_value>
Implemented login() in src/auth.py; all tests pass.
</arg_value>
</tool_call>"""

GLM_ARGKEY = DialectSpec(
    name="glm_argkey",
    call=("<tool_call>", "</tool_call>"),
    name_slot=NameSlot.BODY_HEAD,
    args=ArgStyle.TAGGED_PAIR,
    param=("<arg_key>{k}</arg_key><arg_value>", "</arg_value>"),
    value_mode="raw",
    prose_opener="<tool_call>",
    degeneration_trigger="<",
    forbid_in_think=("<tool_call>",),
    lenient=Lenient(tag_name_variants=False, key_named_closer=False),
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
