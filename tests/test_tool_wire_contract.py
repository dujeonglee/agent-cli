"""도구 왕복 계약 — 광고한 모양을 하네스가 받는가 (TOOLS 전수).

인터셉트되지 않는 도구는 프로덕션에서 **항상** 이 경로를 탄다:

    프롬프트가 광고한 flat op → 중앙 검증 → 실행

v9.11.0 의 ``monitor`` 는 그 사이의 재포장(도구 훅의 기본 ``add_prefix``)이 키에
``monitor_`` 를 붙여, 검증이 *"Missing required field(s): mode"* 로 **모든 호출을 거절**했다. 도구가
릴리스 내내 한 번도 동작하지 않았다. 재포장은 v10.177.5 에서 없앴다(모든 도구가 항등이었다).

## 이 파일이 생긴 이유 — 가드에 구멍이 있었다

그때 쓴 회귀 가드(`test_loop.TestWrapSingleOpIdempotence`)는 합성 예시가
**의미 검증**을 통과 못 하면 그 도구를 건너뛰었다::

    ok_before, _, _ = validate_tool_input(name, canonical)
    if not ok_before:
        continue   # ← 판정 보류

그런데 `"x"` 같은 더미는 enum 을 가진 도구에서 언제나 거절된다. 실측: 15개
중 **5개**(edit_file · code_index · memory · agent · **monitor**)가 통째로
미판정이었다 — **monitor 버그를 막으려고 쓴 가드가 monitor 를 건너뛰고
있었다.**

원인은 축이 섞인 것이다. 막으려던 고장은 **모양**(키가 사라짐)인데 판정을
**의미**(enum 값이 맞나)에 걸었다. 그래서 여기서는 의미를 판정에서 빼고,
모양만 본다:

- 광고 flat 입력: **필수 키가 전부 있어야** 한다.

그리고 마지막 테스트가 **가드의 가드**다: 전수 판정이 조용히 줄어들면 잡는다.
"""

from __future__ import annotations

import pytest

from agent_cli.tools import TOOLS
from agent_cli.tools.registry import (
    _strip_own_prefix,
    validate_tool_input,
)

NAMES = sorted(TOOLS)


def _dummy(spec: dict):
    """타입만 맞는 더미. **의미는 맞추지 않는다** — 맞출 수 없고(enum 이
    산문 설명에 있다), 이 파일의 판정은 의미와 무관해야 한다."""
    t = (spec or {}).get("type")
    if t == "integer" or t == "number":
        return 1
    if t == "boolean":
        return True
    if t == "array":
        items = (spec or {}).get("items") or {}
        return [_dummy(items)] if items else []
    if t == "object":
        props = (spec or {}).get("properties") or {}
        req = set((spec or {}).get("required", []))
        return {k: _dummy(v) for k, v in props.items() if k in req}
    return "x"


def _canonical(params: dict) -> dict:
    """스키마가 선언한 **표준(배치) 입력** — required 만 채운 최소형."""
    props = (params or {}).get("properties", {}) or {}
    return {k: _dummy(props.get(k) or {}) for k in (params or {}).get("required", [])}


def _advertised_flat(name: str, params: dict) -> dict:
    """멀티-op 프롬프트가 **광고하는** flat 모양.

    `registry._flat_params` 와 같은 규칙이다: 배열 파라미터는 item
    속성으로 펼치고, 스칼라는 도구 자신의 접두사를 뗀다. 모델이 실제로
    내보내는 것이 이 모양이라, 계약은 여기서 성립해야 한다.
    """
    props = (params or {}).get("properties", {}) or {}
    req = set((params or {}).get("required", []))
    out: dict = {}
    for k, v in props.items():
        items = v.get("items") if v.get("type") == "array" else None
        if isinstance(items, dict) and items.get("properties"):
            ireq = set(items.get("required", []))
            for ik, iv in items["properties"].items():
                if ik in ireq:
                    out[ik] = _dummy(iv)
        elif k in req:
            out[_strip_own_prefix(name, k)] = _dummy(v)
    return out


class TestAdvertisedShape:
    @pytest.mark.parametrize("name", NAMES)
    def test_advertised_flat_shape_carries_every_required_key(self, name):
        """프롬프트가 광고한 모양에 **필수 키가 다 있어야** 한다 — 검증은 모델이 쓴 그대로를 본다.

        monitor 가 깨진 방식이 정확히 이것이다: `mode` → `monitor_mode` 로
        이름이 바뀌어 required 가 사라졌고, 검증이 전부 거절했다.
        """
        params = TOOLS[name].parameters or {}
        flat = _advertised_flat(name, params)
        lost = [k for k in params.get("required", []) if k not in flat]
        assert not lost, (
            f"{name}: 광고한 모양에 필수 키가 없다 {lost} — 모델이 광고대로 내보내도 "
            f"거절된다.\n  flat={flat}"
        )


class TestNoToolIsSkipped:
    """**가드의 가드.**

    앞선 회귀 가드는 판정 불가한 도구를 조용히 `continue` 했고, 그 결과
    15개 중 5개가 미판정이었다 — 하필 막으려던 monitor 가 거기 있었다.
    전수를 세는 이 테스트가 그 재발을 잡는다.
    """

    def test_every_registered_tool_is_covered(self):
        assert NAMES, "도구 목록이 비었다"

    def test_no_tool_is_excluded_by_a_synthetic_example(self):
        """더미로 표준형을 못 만드는 도구가 있어도 **판정은 계속된다**.

        의미 검증에 걸리는 도구가 실제로 존재해야 이 계약이 의미가 있다 —
        하나도 없으면 위 테스트들이 '쉬운 경우만' 보고 있는 것이다.
        """
        semantic_fail = [
            n
            for n in NAMES
            if not validate_tool_input(n, _canonical(TOOLS[n].parameters or {}))[0]
        ]
        assert semantic_fail, (
            "더미가 전부 의미 검증을 통과한다 — 이 파일의 '의미를 판정에서 "
            "뺐다'는 전제가 더 이상 검증되지 않는다"
        )
        # 그 도구들도 위 계약 테스트에서 **건너뛰지 않고** 판정된다.
        for n in semantic_fail:
            params = TOOLS[n].parameters or {}
            flat = _advertised_flat(n, params)
            lost = [k for k in params.get("required", []) if k not in flat]
            assert not lost, f"{n}: 의미 실패 도구가 모양 판정을 못 받았다"
