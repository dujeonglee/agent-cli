"""태그 구제 — 붕괴·변종 태그를 파라미터로 되살리는 기계 (Phase 5 S1: 이동만).

옛 ``xml_fc`` 가 자기 안에 두던 것들이다 (docs/dialects/PHASE5.md §4.4):
키-이름 closer(``</KEY>``) 수용 정규식, tool-name 태그 변종(``<X>``/``<k>``) 구제,
값-무결성 우선 closer 판정(v7.11.4), 블록 트림. 태그 가족(xml_fc·glm_argkey) 공용이며
어느 스펙의 소유물도 아니다 — 스펙의 ``Lenient`` 옵션이 어느 규칙을 켤지 정한다.

코드는 무수정 이동이다 — 등가성 합격선(PHASE5 §7)이 이 모듈을 기준으로 잰다.
"""

from __future__ import annotations

import re

# 키-이름 closer 수용 — 설명은 옛 xml_fc 의 _PARAM_CLOSED 주석(v7.28.1 strict 대칭).
_PARAM_CLOSED = re.compile(
    r"<parameter=([\w.\-]+)>(.*?)</(?:parameter|\1)>\s*"
    r"(?=<parameter=|</function>|</tool_call>|<function=|<tool_call>|\Z)",
    re.DOTALL | re.IGNORECASE,
)


# ── lenient 구제 (tool-name 태그 변종) ───────────────────────
# 2026-07-17 bakeoff 실측: Qwen3.6-35B-A3B 가 `<function=X>` 를 `<X>` 로,
# `<parameter=k>` 를 `<k>` 로 붕괴시키는 변종이 0-op(NO_ACTION) 마찰의
# 83% (30건 캡처 중 25). strict 경로가 0-op 일 때만 발화하는 최후 폴백 —
# 캐노니컬 emission 은 절대 이 경로에 안 들어온다 (bail-safe). 구제 턴은
# parse_stage=2(drift) 로 계수되고, prior 는 캐노니컬 shape 로 재렌더되어
# (B→C) 다음 턴부터 모델을 교정한다.

# lenient 파라미터 오픈: `<parameter=k>` 또는 plain `<k>`. 닫는 태그는
# 아무 이름이나 수용 (실측: `<parameter=line_start>1</line_start>` 처럼
# 키-이름으로 닫는 혼합 스타일 존재).
_LENIENT_PARAM_OPEN = re.compile(r"<(?:parameter=)?([\w.\-]+)>")
# 값 종료 폴백 (키-closer 부재 시): 라인-선두 다음 오픈 / 구조 닫기 /
# 라인-끝의 임의 닫는 태그. ★값 **속** 임의 `<tag>` 토큰(HTML content,
# `grep "<pat>"`, sed 식)에서 끊지 않는다 — 종전 any-token stop 이
# content 를 빈 값으로 만들고 phantom param 을 만들던 data-loss 의 수리
# (v7.11.4).
_LENIENT_VALUE_FALLBACK_STOP = re.compile(
    r"(?m)^[ \t]*<(?:parameter=)?[\w.\-]+>"  # 다음 param 오픈 (라인 선두)
    r"|</(?:function|tool_call)>"  # 구조 닫기
    r"|</[\w.\-]+>[ \t]*\r?$"  # 라인 끝의 임의 closer (오기명 드리프트)
)


def _lenient_tool_open_re() -> re.Pattern:
    """라인-단독 ``<TOOLNAME>`` 오픈 — 등록 도구명만 (매 파스 재조립: MCP
    도구가 런타임에 등록될 수 있다). 라인-앵커가 산문 속 인라인 언급
    (``use the <shell> tool``)의 오인 구제를 차단한다."""
    from agent_cli.tools.registry import TOOLS

    names = "|".join(re.escape(n) for n in sorted(TOOLS, key=len, reverse=True))
    return re.compile(rf"^[ \t]*<({names})>[ \t]*\r?$", re.MULTILINE)


def _extract_params_lenient(segment: str) -> dict:
    """혼합 스타일 파라미터 추출 — plain ``<k>v</k>``, canonical
    ``<parameter=k>v</parameter>``, 키-이름 closer ``<parameter=k>v</k>``
    전부 수용.

    값의 끝 판정 (v7.11.4 — 값 무결성 우선):
    1. **자기 closer 우선**: ``</KEY>`` 또는 ``</parameter>`` 의 최근접
       매치 — 값 속의 임의 ``<tag>`` 토큰(HTML, ``grep "<pat>"``, sed
       식)은 값의 일부로 보존된다. 같은 줄 다중 param 도 자기 closer
       로만 끊겨 서로를 삼키지 않는다.
    2. 자기 closer 가 없으면(closer 생략/오기명 드리프트) 폴백: 라인-선두
       다음 오픈 / 구조 닫기 / 라인-끝 임의 closer / 세그먼트 끝."""
    params: dict = {}
    pos = 0
    while True:
        m = _LENIENT_PARAM_OPEN.search(segment, pos)
        if m is None:
            break
        key = m.group(1)
        own_closer = re.compile(rf"</(?:parameter|{re.escape(key)})\s*>", re.IGNORECASE)
        cm = own_closer.search(segment, m.end())
        if cm is not None:
            value, pos = segment[m.end() : cm.start()], cm.end()
        else:
            stop = _LENIENT_VALUE_FALLBACK_STOP.search(segment, m.end())
            if stop is None:
                value, pos = segment[m.end() :], len(segment)
            elif stop.group(0).startswith("</"):
                value, pos = segment[m.end() : stop.start()], stop.end()
            else:
                # closer 생략 — 다음 라인-선두 오픈 직전까지가 값
                value, pos = segment[m.end() : stop.start()], stop.start()
        params[key] = _trim_block(value.rstrip())
    return params


def _trim_block(value: str) -> str:
    """블록 스타일 허용: 여는 태그 직후·닫는 태그 직전 개행 1개만 트림.

    내부 공백/개행은 보존 — raw 값 계약. (render 측이 멀티라인 값을
    블록 스타일로 쓰므로 이 트림과 대칭 = round-trip 보존.)
    """
    value = value.removeprefix("\n")
    value = value.removesuffix("\n")
    return value
