"""xml_fc 스펙 — 태그-파라미터 function call (Qwen3-Coder XML 과 같은 문법).

PHASE2 의 손 코딩 모듈을 데이터로 다시 쓴 것 (Phase 5 S2). 산문 조각은 옛 모듈의
텍스트를 **바이트 그대로** 옮겼다 — 시스템 프롬프트가 같으면 모델 동작의 상한도
같다(PHASE5.md §4.6·§7.3). 네이티브 프라이어: Qwen3-Coder, Qwen3.5/3.6/3.8,
Nemotron 3/3.5, Granite 4.2.
"""

from __future__ import annotations

from agent_cli.wire_formats.base import NO_ACTION_FRAMING
from agent_cli.wire_formats.spec import ArgStyle, DialectSpec, Lenient, NameSlot, Prose

_REMINDER_CALL = "Respond with one or more <tool_call> blocks, each containing <function=TOOL> with <parameter=NAME>value</parameter> lines. To finish, call <function=complete> with a result parameter."
_REMINDER_ACTION_REQUIRED = "Each <function=...> must name one tool from Available Tools. If the task is DONE, finish with <function=complete> and a <parameter=result>your final answer</parameter>. If your last message already was the final answer in plain prose, re-emit that answer as the result parameter. If you were about to do something, emit that tool call now. Never stop without an explicit complete."
_FRAMING_PARSE_FAIL = "Your response contained no parseable <tool_call> block — tool calls must use <function=...> / <parameter=...> tags."
_NO_ACTION_DETAIL = "no <function=TOOL> naming a tool from Available Tools"

_FORMAT_RULES = """\
## Response Format

Write brief reasoning as plain prose, then emit your tool calls:

Your reasoning goes here, as plain prose. No tags around it.

<tool_call>
<function=TOOL_NAME>
<parameter=PARAM_NAME>value</parameter>
</function>
</tool_call>

Parameter values are RAW text: write file contents, code, and multi-line
text directly between the tags with NO escaping — no \\n, no quote
escaping, no JSON. For a multi-line value put it on its own lines:
<parameter=content>
line one
line two
</parameter>
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
2. Each <function=...> names exactly one tool from Available Tools.
3. Each call acts on ONE target. To read N files, emit N separate
   <tool_call> blocks in the SAME turn — never a list inside one call.
4. Close every tag: </parameter>, </function>, </tool_call>.
5. When the task is DONE, end with a `complete` call carrying your final
   answer:
<tool_call>
<function=complete>
<parameter=result>
your final answer
</parameter>
</function>
</tool_call>
   Always finish this way — do NOT just stop.
6. If an observation shows an error, fix parameters and retry.
7. Respond in the user's language.

Several independent operations in one turn (read two files at once — they
don't depend on each other):
To see how auth and session fit together I need both files; neither
depends on the other, so read them together.

<tool_call>
<function=read_file>
<parameter=path>src/auth.py</parameter>
</function>
</tool_call>
<tool_call>
<function=read_file>
<parameter=path>src/session.py</parameter>
</function>
</tool_call>

Finishing the task:
The login() function is implemented and the tests pass.

<tool_call>
<function=complete>
<parameter=result>
Implemented login() in src/auth.py; all tests pass.
</parameter>
</function>
</tool_call>"""


XML_FC = DialectSpec(
    name="xml_fc",
    call=("<tool_call>", "</tool_call>"),
    name_slot=NameSlot.OPEN_TAG,
    name_wrap=("<function=", ">"),
    name_close="</function>",
    args=ArgStyle.TAGGED,
    param=("<parameter={k}>", "</parameter>"),
    value_mode="raw",
    prose_opener="<tool_call>",
    degeneration_trigger="<",
    forbid_in_think=("<tool_call>",),
    lenient=Lenient(tag_name_variants=True, key_named_closer=True),
    prose=Prose(
        rules=_FORMAT_RULES,
        reminder_call=_REMINDER_CALL,
        reminder_action_required=_REMINDER_ACTION_REQUIRED,
        framing_parse_fail=_FRAMING_PARSE_FAIL,
        no_action_detail=_NO_ACTION_DETAIL,
        # 옛 모듈의 조합 그대로: framing + reminder / NO_ACTION framing + reminder
        retry_no_json=f"{_FRAMING_PARSE_FAIL} {_REMINDER_CALL}",
        retry_no_action=(
            f"{NO_ACTION_FRAMING} ({_NO_ACTION_DETAIL}). {_REMINDER_ACTION_REQUIRED}"
        ),
        user_prefixes=(
            "Your response contained no parseable <tool_call> block",
            NO_ACTION_FRAMING,
            "Your tool call names no tool",  # ≤ v9.24.2 문구 — 옛 세션 resume
        ),
    ),
)
