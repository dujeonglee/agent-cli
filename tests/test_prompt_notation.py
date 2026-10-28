"""모델이 보는 글의 표기 규칙 (v10.77.0) — 재유입 가드.

- 도구 이름·인자 이름은 백틱(`read_file`, `line_start`), 값은 큰따옴표("replace").
- 호출 모양(`read_file(path=…)`, `mode='list'`, `mode:"run"`)은 쓰지 않는다 — 문장으로.

검사 대상은 ``prompts.inventory`` 가 모으는 글 전부(도구 설명·인자·안내, 시스템
프롬프트 절, 실행 중 문구)와 형식 오류 넛지의 "You sent:" 줄이다. 형식이
보여 주는 호출 예시(JSON 배열, ``<tool_call>`` 블록, `` `tool` {...} ``)와 줄 머리
이름표(``read_file failed:`` 같은 오류 머리)는 규칙 밖이다.
"""

from __future__ import annotations

import re

import pytest

from agent_cli.prompts.inventory import build_inventory
from agent_cli.recovery.primitives import summarize_call
from agent_cli.tools.registry import _BUILTIN_ORDER

_UNDERSCORED = [n for n in _BUILTIN_ORDER if "_" in n]
# 백틱·따옴표·태그·경로 안이 아닌, 밑줄 있는 도구 이름 (단어형 이름은 보통 영어와
# 구별되지 않아 기계 검사 밖이다)
_BARE = re.compile(
    r"(?<![`\w\"'#/.\-\[<=>{])(" + "|".join(_UNDERSCORED) + r")(?![`\w\"'(\]>}])"
)
_CALL = re.compile(
    r"\b(?:" + "|".join(_BUILTIN_ORDER) + r")\([a-z_]+\s*="  # tool(k=…)
    r"|\bmode\s*[:=]\s*['\"]"  # mode='x' / mode:"x"
    r"|\b(?:op|pos|end|search|stat|background|query|sessions|run|ref|to)='"
)
# 형식이 그리는 호출 예시 줄 (glm_argkey 예시는 도구 이름만 있는 줄로 시작한다)
_EXAMPLE = re.compile(
    r'"action"|"name":|<function=|<tool_call>|<arg_key>|^\s*`\w+` \{|^\s*[a-z_]+\s*$'
)
_LABEL = re.compile(r"^\s*(?:\[)?(?:" + "|".join(_UNDERSCORED) + r")[ :\]]")


def _texts():
    inv = build_inventory()
    for dn, d in inv["dialects"].items():
        for t in d["tools"]:
            yield f"{dn} tool {t['name']}", t["description"]
            if t.get("guide"):
                yield f"{dn} guide {t['name']}", t["guide"]
            for pn, pv in (t["parameters"].get("properties") or {}).items():
                yield f"{dn} param {t['name']}.{pn}", pv.get("description") or ""
        for r in d["runtime"]:
            yield f"{dn} runtime {r['id']}", r["text"]
        for scn, sc in d["scenarios"].items():
            for sec in sc.get("sections", []):
                yield f"{dn} {scn} section {sec.get('name')}", sec.get("text") or ""


_ALL = list(_texts())


def _lines(text):
    for line in (text or "").split("\n"):
        if _EXAMPLE.search(line) or _LABEL.match(line) or "SELECT" in line:
            continue
        # Available Tools 의 "- read_file: …" 머리 (텍스트 방언 도구 목록)
        line = re.sub(r"^\s*- [a-z_]+: ", "", line)
        yield line


@pytest.mark.parametrize(("where", "text"), _ALL, ids=[w for w, _ in _ALL])
def test_tool_names_are_backticked(where, text):
    bad = [ln.strip() for ln in _lines(text) if _BARE.search(ln)]
    assert not bad, f"{where}: {bad[:3]}"


@pytest.mark.parametrize(("where", "text"), _ALL, ids=[w for w, _ in _ALL])
def test_no_call_shapes(where, text):
    bad = [ln.strip() for ln in _lines(text) if _CALL.search(ln)]
    assert not bad, f"{where}: {bad[:3]}"


def test_you_sent_line_is_a_sentence():
    out = summarize_call(
        "edit_file", {"op": "replace", "pos": "2#KT"}, missing=["path"]
    )
    assert out.startswith('You sent: `edit_file` with `op` "replace", `pos` "2#KT"')
    assert "edit_file(" not in out and "missing: `path`" in out
