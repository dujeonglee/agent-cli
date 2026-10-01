"""DialectSpec — 와이어 포맷(방언)을 코드가 아니라 데이터로 (Phase 5, PHASE5.md §4.1).

생태계(vLLM structural-tag · llama.cpp autoparser · HF response_template)가 쓰는
네 축 — **시작 트리거 · 호출 단위 구분자 · 이름 위치 · 인자 포맷** — 에 우리 축
(산문 thought, 종결 op, 문법 opener, 러너웨이 시그니처, 구제 옵션, 산문 조각)을
더한 것이 스펙이다. :mod:`agent_cli.dialects.engine` 의 ``Dialect`` 가 스펙
하나로 렌더·파서·문법·산문·history 왕복을 전부 만든다.

스펙은 데이터다 — 메서드가 없다. 모양이 다른 포맷은 스펙 필드가 다르고, 같은
엔진 코드가 그 차이를 읽는다. 산문(``Prose``)만은 생성하지 않는다: 벤치로 다듬은
말이라 포맷마다 조각을 그대로 들고 다닌다(§4.6).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum


class ArgStyle(Enum):
    """인자 포맷 — 호출 하나 안에서 파라미터가 어떻게 적히나."""

    TAGGED = "tagged"  # <parameter=k>v</parameter>              (xml_fc, Nemotron 3, Granite 4.2)
    TAGGED_PAIR = (
        "tagged_pair"  # <arg_key>k</arg_key><arg_value>v</arg_value>  (GLM-4.5+)
    )
    JSON_IN_TAG = (
        "json_in_tag"  # <tool_call>{"name":…,"arguments":{…}}</tool_call>  (Hermes)
    )
    JSON_NATIVE = "json_native"  # [{"action":…, …}] — 태그 없음               (json_fc)


class NameSlot(Enum):
    """이름 위치 — 호출된 도구 이름이 어디에 적히나."""

    OPEN_TAG = "open_tag"  # 여는 태그 안: <function=NAME> … </function>
    BODY_HEAD = "body_head"  # 호출 본문 첫 토큰: <tool_call>NAME …
    JSON_KEY = "json_key"  # JSON 객체의 키: "name" / "action"


@dataclass(frozen=True)
class Lenient:
    """구제 옵션 — ``recovery.tagged`` 의 어느 규칙을 켤지 (PHASE5 D4).

    켠 스펙에서만 해당 코드 경로가 실행된다. 캐노니컬 출력은 어느 옵션에도
    닿지 않는다(strict 0-op 뒤의 최후 폴백)."""

    tag_name_variants: bool = False
    """``<function=X>``→``<X>``, ``<parameter=k>``→``<k>`` 붕괴 변종 구제 — 등록
    도구명이 줄에 혼자 있을 때만 호출로 인정(xml_fc 실측: 0-op 마찰의 83%)."""
    key_named_closer: bool = False
    """``<parameter=path>…</path>`` 처럼 키 이름으로 닫는 혼합 스타일 수용."""
    inline_quotes_exclude: bool = True
    """균형 ``` 쌍·인라인 `…` 안의 후보는 인용(예시)이다 — 자격 있는 비인용
    후보가 있을 때만 제외 (v7.28.1 / v9.24.8)."""


@dataclass(frozen=True)
class Prose:
    """모델에게 가르치고 되돌려 주는 말 — 생성하지 않고 조각으로 든다 (§4.6)."""

    rules: str
    """``## Response Format`` 섹션 전문 (``format_rules``)."""
    reminder_call: str
    reminder_action_required: str
    framing_parse_fail: str
    no_action_detail: str
    retry_no_json: str
    retry_no_action: str
    user_prefixes: tuple[str, ...]
    """이 방언의 구제 문구가 시작하는 접두 — resume 미리보기가 시스템 주입을 걸러낸다."""


@dataclass(frozen=True)
class DialectSpec:
    """한 방언의 전부 — ``Dialect(spec)`` 가 이것만 읽는다."""

    name: str
    """레지스트리 이름 (= ``--dialect`` 값, 세션 메타, models.json 바인딩)."""

    # ── 네 축 ──
    call: tuple[str, str] | None
    """호출 단위 여닫기 (``("<tool_call>", "</tool_call>")``). ``None`` = 태그 없음
    (json_native — 배열 원소가 곧 호출)."""
    name_slot: NameSlot
    args: ArgStyle
    name_wrap: tuple[str, str] = ("", "")
    """OPEN_TAG: 이름 여는 태그의 앞뒤 (``("<function=", ">")``)."""
    name_close: str = ""
    """OPEN_TAG: 이름 블록 닫기 (``"</function>"``)."""
    param: tuple[str, str] = ("", "")
    """TAGGED: ``("<parameter={k}>", "</parameter>")`` — ``{k}`` 자리에 키.
    TAGGED_PAIR: ``("<arg_key>{k}</arg_key><arg_value>", "</arg_value>")``."""
    value_mode: str = "raw"
    """``raw``: 태그 안 원문, 스키마가 string 이 아닌 param 만 JSON 으로 복원.
    ``json``: 값 자체가 JSON."""
    op_shape: str = "name_arguments"
    """JSON 계열의 op 객체 모양: ``name_arguments`` (``{"name","arguments"}``) |
    ``flat_action`` (``{"action", …params}``)."""
    section: tuple[str, str] | None = None
    """섹션 래퍼(MiniMax 류 — 여러 호출을 한 번 더 감싸는 태그). 평문 태그 가족
    넷에는 없다; 예약."""

    # ── 우리 축 ──
    terminal_op: str = "complete"
    prose_opener: str = ""
    """문법의 산문 규칙이 줄 머리에 두지 못하는 시퀀스 (``"<tool_call>"`` / ``"["``)."""
    prose_after_blank_line: bool = False
    """json_fc 규약: 빈 줄 다음 줄(또는 첫 줄)만 opener 로 시작할 수 없다."""
    degeneration_trigger: str = "<"
    """스트림 조기종료 게이트 문자 (P0-4) — 러너웨이 시그니처의 첫 글자."""
    forbid_in_think: tuple[str, ...] = ()
    """사고 구간에서 금지할 리터럴 (v9.24.8 — 태그 가족은 호출 여는 태그)."""
    lenient: Lenient = field(default_factory=Lenient)
    prose: Prose | None = None
    action_required: bool = False
    multi_op: bool = True
    exposes_complete: bool = True

    # ── 파생 (엔진이 쓰는 정규식 — 스펙에서 결정적으로 나온다) ──
    @property
    def call_open(self) -> str:
        return self.call[0] if self.call else ""

    @property
    def call_close(self) -> str:
        return self.call[1] if self.call else ""

    @property
    def param_open_prefix(self) -> str:
        """``<parameter=`` — 파라미터 여는 태그에서 키 앞까지."""
        return self.param[0].split("{k}", 1)[0]

    @property
    def param_open_suffix(self) -> str:
        """``>`` — 키 뒤부터 값 앞까지."""
        parts = self.param[0].split("{k}", 1)
        return parts[1] if len(parts) == 2 else ""

    @property
    def param_close(self) -> str:
        return self.param[1]

    @property
    def closer_tag_name(self) -> str:
        """``</parameter>`` → ``parameter`` (키-이름 closer 대안의 짝)."""
        m = re.match(r"</([\w:.\-]+)>", self.param_close)
        return m.group(1) if m else ""

    def tag_name(self, token: str) -> str:
        """``<tool_call>``/``<function=`` → ``tool_call``/``function``."""
        m = re.match(r"</?([\w:.\-]+)", token)
        return m.group(1) if m else ""
