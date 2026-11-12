"""선택 의존성(extra) 설치 안내 — 실제로 되는 명령을 보여 준다 (v10.123.3).

agent-cli 는 PyPI 에 없다(RELEASING.md) — ``pip install agent-cli[web]`` 는 패키지를
못 찾아 실패하고, rich 마크업으로 찍으면 ``[web]`` 이 태그로 삼켜져 ``pip install
agent-cli`` 만 보였다. 그래서 안내는 **빠진 패키지 이름을 직접** 댄다. 목록은 설치
메타데이터에서 읽는다 — 문자열에 베끼면 pyproject 와 어긋난다.

안내 문자열에는 대괄호(``uvicorn[standard]``)가 들어가므로 찍는 쪽은 ``markup=False``.
"""

from __future__ import annotations

import re
import shlex

_DIST = "agent-cli"


def extra_requirements(extra: str) -> list[str]:
    """``extra`` 에 딸린 요구 사항(``fastapi>=0.110`` 꼴) — 설치 메타데이터 순서 그대로.

    메타데이터를 못 읽으면(설치 없이 소스로 실행) 빈 목록."""
    from importlib import metadata

    try:
        requires = metadata.requires(_DIST) or []
    except metadata.PackageNotFoundError:
        return []
    marker = re.compile(r"""\bextra\s*==\s*['"]""" + re.escape(extra) + r"""['"]""")
    out = []
    for line in requires:
        req, _, cond = line.partition(";")
        if marker.search(cond):
            out.append(req.strip())
    return out


def install_hint(extra: str) -> str:
    """사용자에게 보여 줄 한 줄 — ``pip install 'fastapi>=0.110' …`` 또는 폴백 문장."""
    reqs = extra_requirements(extra)
    if not reqs:
        return f'Install the dependencies of the "{extra}" extra (see pyproject.toml).'
    return "Install with: pip install " + " ".join(shlex.quote(r) for r in reqs)
