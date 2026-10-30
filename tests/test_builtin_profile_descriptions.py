"""내장 프로파일 설명은 짧은 한 문장 (v10.86.0, agent 검토 PR C — 사용자 결정 "축약하자").

`## Agent Profiles` 절은 고르는 데 필요한 만큼만 싣는다 — 역할·읽기/쓰기·상주 여부를
한 문장(≈25단어)으로; 상세는 본문(띄운 에이전트의 시스템 프롬프트)이 가진다. 꼬리
로스터는 설명의 **첫 문장**만 싣으므로(``build_live_agents_section``) 한 문장이면
절과 로스터가 같은 말을 한다. 머리글의 ``": "`` 는 YAML 매핑 표시라 설명이 통째로
비므로(실측) 역할과 설명은 ``" — "`` 로 잇는다. 실측(GLM 토크나이저): 절 620 → 427 토큰.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

import agent_cli
from agent_cli.subagent.profiles import available_profiles

_BUILTIN = sorted((Path(agent_cli.__file__).parent / "agents" / "builtin").glob("*.md"))
_NAMES = [p.stem for p in _BUILTIN]
_SENTENCE_END = re.compile(r"[.!?](\s|$)")


@pytest.mark.parametrize("name", _NAMES)
def test_builtin_description_first_sentence_is_short(name):
    """첫 문장(로스터에 실리는 것)은 ≤35 단어, 전체 ≤60 단어. `general` 만 둘째 문장이
    있다 — 전문가 라우팅(v10.21.0 결정, tests/test_builtin_agents.py 가 고정)."""
    desc = dict(available_profiles())[name]
    assert desc, name
    ends = list(_SENTENCE_END.finditer(desc))
    assert ends and ends[-1].end() == len(desc), (name, desc)
    first = desc[: ends[0].end()].strip()
    assert len(first.split()) <= 35, (name, len(first.split()))
    assert len(desc.split()) <= 60, (name, len(desc.split()))
    assert len(ends) == (2 if name == "general" else 1), (name, desc)


@pytest.mark.parametrize("name", _NAMES)
def test_builtin_description_names_role_and_persistence(name):
    """로스터·절 모두에서 고르는 데 쓰는 두 사실 — 무엇을 하는가, 상주인가."""
    desc = dict(available_profiles())[name].lower()
    assert "persistent" in desc
