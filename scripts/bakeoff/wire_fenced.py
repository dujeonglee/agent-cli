"""json_fc_fenced — A/B 실험 변형: op 배열을 ``<tool_call>`` 펜스로 감싼다.

사용자 제안(2026-07-26)의 측정용 구현. ``<tool_call>{json}</tool_call>`` 은
Qwen/Hermes 계열의 **네이티브 tool-call 템플릿**이라 bare 배열보다 모델
프라이어에 가깝다는 가설을 검증한다. D7 이 기각한 ``{"tool_call": [...]}`` 는
JSON **중첩 레이어**(배치-중첩 27B 90% 파괴 전례)였고, 이 변형은 JSON 밖의
태그 펜스라 그 기각과 별개다.

의도적으로 **bakeoff 전용**(agent_cli 패키지 밖): 실험이 이기기 전에는
사용자-대면 포맷으로 출하하지 않는다(문서·바인딩·resume 표면 전부 따라오므로).
phase2.py 가 import 하면 registry 에 등록되어 ``BAKEOFF_PLUGINS=json_fc_fenced``
로 선택 가능해진다.

A/B 격리 원칙: JsonFcFormat 서브클래스로 **펜스만** 변수로 만든다 — body(flat
op 배열)·JSON 수리 기계·recovery 의미론은 전부 상속. 규칙 텍스트도 펜스 관련
문장만 다르고 배칭 지시는 자구까지 동일하게 유지한다(compact A/B 의 교훈:
프롬프트의 다른 차이가 배칭 행동을 오염시킨다).

파서 관용: 펜스 없는 bare 배열도 수용하되 **stage 2(drift)** 로 계수 —
xml_fc 가 bare ``<function=`` 를 drift 로 받는 것과 동형. prior 재렌더는 항상
캐노니컬(펜스 포함)이라 B→C 자기 교정이 걸린다.
"""

from __future__ import annotations

import json
import re

from agent_cli.wire_formats import register
from agent_cli.wire_formats.base import ParsedTurn
from agent_cli.wire_formats.json_fc import JsonFcFormat

_TC_BLOCK = re.compile(r"<tool_call>\s*(.*?)\s*</tool_call>", re.DOTALL | re.IGNORECASE)
_TC_OPEN_ONLY = re.compile(r"<tool_call>\s*", re.IGNORECASE)

_FORMAT_RULES = """\
## Response Format

Write brief reasoning as plain prose, then end your turn with ONE
<tool_call> block containing a JSON array of tool calls:

Your reasoning goes here, as plain prose.

<tool_call>
[{"action": "<tool name>", <its parameters>}]
</tool_call>

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
1. Reasoning (optional) is plain prose BEFORE the <tool_call> block — never
   after it.
2. Every turn must END with one <tool_call> block whose content is one JSON
   array containing at least one op (work, or `complete` to finish). Do NOT
   just stop after prose.
3. Each op must have an "action" naming one tool.
4. Each op acts on ONE target. To read N files, emit N separate
   {"action": "read_file", "path": ...} ops in the SAME array. NEVER put a
   list of items inside a single op (no nested arrays).
5. When the task is DONE, end with a `complete` op carrying your final
   answer: {"action": "complete", "result": "<your final answer>"}.
6. The ONLY tag in your output is the <tool_call> wrapper — exactly one
   block per turn. No other HTML/XML tags (<function_call>, <div>,
   <answer>, ...) and NO markdown headers (## ...).
7. If an observation shows an error, fix parameters and retry.
8. Respond in the user's language.

Several independent operations in one turn (read three files at once —
they don't depend on each other):
To see how auth, session, and the login route fit together I need all
three files; none depends on another's output, so read them together.

<tool_call>
[{"action": "read_file", "path": "src/auth.py"}, {"action": "read_file", "path": "src/session.py"}, {"action": "read_file", "path": "src/routes/login.py"}]
</tool_call>

Finishing the task:
The login() function is implemented and the tests pass.

<tool_call>
[{"action": "complete", "result": "Implemented login() in src/auth.py; all tests pass."}]
</tool_call>"""


class JsonFcFencedFormat(JsonFcFormat):
    name = "json_fc_fenced"

    def format_rules(self) -> str:
        return _FORMAT_RULES

    def format_rules_anchor(self) -> str:
        return (
            "Write brief reasoning as plain prose, then end the turn with "
            "ONE <tool_call> block containing a JSON array of ops (finish "
            "with a `complete` op)."
        )

    def format_rules_field_specific(self) -> str:
        return (
            "1. Optional reasoning is plain prose BEFORE the <tool_call> block.\n"
            "2. The turn ends with a <tool_call> block containing a JSON array "
            'of {"action": ..., params} ops.'
        )

    # ── parse: 펜스를 벗기고 부모의 검증된 경로로 ─────────────────
    def parse_turn(self, llm_text: str) -> ParsedTurn:
        text, thinking = self.strip_thinking(llm_text)
        m = _TC_BLOCK.search(text)
        if m is not None:
            # 캐노니컬: thought = 펜스 앞 산문, body = 펜스 내용. 부모의
            # stripped-parser 에 "산문\n\nJSON" 동형으로 재조립해 넘긴다 —
            # 수리 기계·op-서명 앵커 전부 그대로.
            rebuilt = f"{text[: m.start()].strip()}\n\n{m.group(1)}"
            turn = self._parse_turn_stripped(rebuilt)
            turn.raw = text
            turn.thinking = thinking
            return turn
        opened = _TC_OPEN_ONLY.search(text)
        if opened is not None:
            # 열림만 있고 닫힘 누락(트렁케이션/드리프트) — 내용은 그대로
            # 수리 기계로, drift 로 계수.
            rebuilt = f"{text[: opened.start()].strip()}\n\n{text[opened.end() :]}"
            turn = self._parse_turn_stripped(rebuilt)
            turn.raw = text
            turn.thinking = thinking
            if turn.parse_stage == 1:
                turn.parse_stage = 2
            return turn
        # 펜스 자체가 없음 — bare 배열 관용(부모 경로), 성공 시 drift 계수.
        turn = self._parse_turn_stripped(text)
        turn.thinking = thinking
        if turn.ops and turn.parse_stage == 1:
            turn.parse_stage = 2
        return turn

    # ── prior 재렌더: 항상 캐노니컬(펜스 포함) — B→C 자기 교정 ────
    def render_assistant_from_history(self, record: dict) -> dict:
        ops = record.get("ops")
        if isinstance(ops, list) and ops:
            rendered = json.dumps(
                [
                    {"action": o.get("action"), **(o.get("action_input") or {})}
                    for o in ops
                    if isinstance(o, dict)
                ],
                ensure_ascii=False,
            )
            fenced = f"<tool_call>\n{rendered}\n</tool_call>"
            thought = record.get("thought", "")
            content = f"{thought}\n\n{fenced}" if thought else fenced
            return {"role": "assistant", "content": content}
        return super().render_assistant_from_history(record)

    def render_full_example(self, *, thought, action: str, action_input: str) -> str:
        bare = super().render_full_example(
            thought=thought, action=action, action_input=action_input
        )
        # 부모 예시의 말미 배열을 펜스로 감싼다 (배열은 항상 마지막 줄).
        head, _, arr = bare.rpartition("\n\n")
        if not arr.startswith("["):
            return bare
        return f"{head}\n\n<tool_call>\n{arr}\n</tool_call>"

    # ── recovery 문구: 펜스 언급으로 치환 ─────────────────────────
    def constraint_reminder_call(self) -> str:
        return (
            "Respond with plain-prose reasoning followed by ONE <tool_call> "
            'block containing a JSON array of {"action": ..., params} ops. '
            "To finish, use a `complete` op: "
            '{"action": "complete", "result": "<final answer>"}.'
        )

    def static_retry_hint_no_json(self) -> str:
        return (
            "Your last message had no parseable tool-call block. End the turn "
            "with ONE <tool_call> block containing a JSON array of ops, e.g. "
            '<tool_call>[{"action": "read_file", "path": "..."}]</tool_call>.'
        )


register(JsonFcFencedFormat())
