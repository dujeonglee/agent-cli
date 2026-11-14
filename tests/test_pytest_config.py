"""pytest 설정 가드 — 멈춘 테스트가 흔적 없이 끝나지 않게 한다.

실브라우저 테스트 첫 실행이 600초 멈췄는데 출력이 남지 않아 원인을 못 찾았다.
``faulthandler_timeout`` 은 한 테스트가 그 초를 넘기면 모든 스레드의 스택을
캡처 밖(터미널)에 남긴다 — 이 값이 설정에서 빠지지 않았는지 본다.
"""

from __future__ import annotations

from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # py3.10
    import tomli as tomllib

_PYPROJECT = Path(__file__).resolve().parent.parent / "pyproject.toml"


def test_faulthandler_timeout_is_set():
    ini = tomllib.loads(_PYPROJECT.read_text(encoding="utf-8"))["tool"]["pytest"][
        "ini_options"
    ]
    assert 0 < float(ini["faulthandler_timeout"]) <= 300
