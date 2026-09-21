"""도구 왕복 계약 — 광고한 모양을 하네스가 받는가 (TOOLS 전수).

살아 있는 wire format 둘이 모두 ``multi_op`` 이라, 인터셉트되지 않는 도구는
프로덕션에서 **항상** 이 경로를 탄다:

    프롬프트가 광고한 flat op → wrap_single_op → 중앙 검증 → 실행

v9.11.0 의 ``monitor`` 는 ``wrap_single_op`` 을 오버라이드하지 않아 기본
``add_prefix`` 가 키에 ``monitor_`` 를 붙였고, 검증이 *"Missing required
field(s): mode"* 로 **모든 호출을 거절**했다. 도구가 릴리스 내내 한 번도
동작하지 않았다.

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

- 표준 입력: 래핑 **전후의 판정이 같아야** 한다. 의미 오류는 양쪽에 똑같이
  나오므로 건너뛸 이유가 없다 — 모양이 깨지면 판정이 달라진다.
- 광고 flat 입력: 래핑 뒤에도 **필수 키가 전부 남아야** 한다.

그리고 마지막 테스트가 **가드의 가드**다: 전수 판정이 조용히 줄어들면 잡는다.
"""

from __future__ import annotations

from typing import ClassVar

import pytest

from agent_cli.tools import TOOLS
from agent_cli.tools.base import Tool
from agent_cli.tools.registry import (
    TOOL_SCHEMAS,
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

    `registry._multi_op_flat_params` 와 같은 규칙이다: 배열 파라미터는 item
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


class TestMultiOpRoundTrip:
    @pytest.mark.parametrize("name", NAMES)
    def test_wrapping_canonical_input_never_changes_the_verdict(self, name):
        """래핑은 **판정을 바꾸면 안 된다**.

        건너뛰기가 없다. 의미 오류(enum 불일치)는 래핑 전후에 똑같이 나므로
        비교가 성립하고, 모양이 깨지면(키가 사라지거나 이름이 바뀌면) 판정이
        달라져 여기서 잡힌다.
        """
        tool = TOOLS[name]
        canonical = _canonical(tool.parameters or {})
        before = validate_tool_input(name, dict(canonical))[:2]
        after = validate_tool_input(name, tool.wrap_single_op(dict(canonical)))[:2]
        assert before == after, (
            f"{name}: wrap_single_op 이 판정을 바꿨다 — multi_op 포맷에서 이 "
            f"도구는 호출이 거절될 수 있다.\n  전: {before}\n  후: {after}"
        )

    @pytest.mark.parametrize("name", NAMES)
    def test_advertised_flat_shape_keeps_every_required_key(self, name):
        """프롬프트가 광고한 모양을 래핑해도 **필수 키가 남아야** 한다.

        monitor 가 깨진 방식이 정확히 이것이다: `mode` → `monitor_mode` 로
        이름이 바뀌어 required 가 사라졌고, 검증이 전부 거절했다.
        """
        tool = TOOLS[name]
        params = tool.parameters or {}
        wrapped = tool.wrap_single_op(_advertised_flat(name, params))
        lost = [k for k in params.get("required", []) if k not in wrapped]
        assert not lost, (
            f"{name}: 래핑이 필수 키를 잃었다 {lost} — 모델이 광고대로 내보내도 "
            f"거절된다.\n  wrapped={wrapped}"
        )

    @pytest.mark.parametrize("name", NAMES)
    def test_wrap_is_idempotent_on_both_shapes(self, name):
        """표준형이 한 번 더 들어와도 같아야 한다 — 배치 도구가 이미 캐노니컬인
        입력을 또 감싸면 이중 래핑으로 거절된다."""
        tool = TOOLS[name]
        for label, shape in (
            ("canonical", _canonical(tool.parameters or {})),
            ("flat", _advertised_flat(name, tool.parameters or {})),
        ):
            once = tool.wrap_single_op(dict(shape))
            twice = tool.wrap_single_op(dict(once))
            assert twice == once, f"{name}({label}): 멱등하지 않다\n  {once}\n  {twice}"


class TestDefaultIsIdentity:
    """기본값이 **접두사를 붙이면** 오버라이드를 잊은 모든 도구가 죽는다.

    그게 v9.11.0 의 고장이었고, 고침은 "각 도구가 기억하기" 가 아니라
    **기본값을 identity 로** 뒤집는 것이었다 — 잊어도 안 깨지게.
    """

    def test_base_wrap_does_not_rename_keys(self):
        class _Probe(Tool):
            name = "probe"
            description = "d"
            key_prefix = "probe_"
            parameters: ClassVar[dict] = {
                "type": "object",
                "properties": {"mode": {"type": "string"}},
            }

            def _run(self, args, *, ctx=None):  # pragma: no cover - 미사용
                raise AssertionError

        flat = {"mode": "list"}
        assert _Probe().wrap_single_op(dict(flat)) == flat, (
            "기본 wrap 이 키를 바꾼다 — 오버라이드를 잊은 도구가 전부 죽는다"
        )


class TestNoToolIsSkipped:
    """**가드의 가드.**

    앞선 회귀 가드는 판정 불가한 도구를 조용히 `continue` 했고, 그 결과
    15개 중 5개가 미판정이었다 — 하필 막으려던 monitor 가 거기 있었다.
    전수를 세는 이 테스트가 그 재발을 잡는다.
    """

    def test_every_registered_tool_is_covered(self):
        assert NAMES, "도구 목록이 비었다"
        assert set(NAMES) == set(TOOL_SCHEMAS), (
            "TOOLS 와 TOOL_SCHEMAS 가 어긋난다 — 한쪽에만 있는 도구는 "
            "계약 검사에서 샌다"
        )

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
            tool = TOOLS[n]
            canonical = _canonical(tool.parameters or {})
            before = validate_tool_input(n, dict(canonical))[:2]
            after = validate_tool_input(n, tool.wrap_single_op(dict(canonical)))[:2]
            assert before == after, f"{n}: 의미 실패 도구가 모양 판정을 못 받았다"
