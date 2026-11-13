"""docs/REFERENCE.md 환경변수 표 ⊇ 코드가 읽는 ``AGENT_CLI_*`` 이름 (v10.127.3).

표가 코드와 어긋나 있었다 — 코드가 읽는 8개(`AGENT_CLI_NO_INPUT`·스냅샷 넷·monitor 재생 상한·
스트림 덤프·원문 기록)가 표에 없었다. "코드가 읽는 이름" = 패키지 소스의 **문자열 리터럴 전체가**
``AGENT_CLI_…`` 인 것(``os.environ.get("AGENT_CLI_X")``·노브 표의 env 칸). 주석·docstring 속
언급(옛 이름의 이력 서술)은 리터럴 전체가 아니므로 세지 않는다.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_NAME = re.compile(r"^AGENT_CLI_[A-Z0-9_]+$")
_ROW = re.compile(r"^\| `(AGENT_CLI_[A-Z0-9_]+)` \|", re.MULTILINE)


def _code_names() -> dict[str, str]:
    found: dict[str, str] = {}
    for path in sorted((_ROOT / "agent_cli").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and _NAME.match(node.value)
            ):
                found.setdefault(node.value, str(path.relative_to(_ROOT)))
    return found


def _reference_env_section() -> str:
    text = (_ROOT / "docs" / "REFERENCE.md").read_text(encoding="utf-8")
    start = text.index("### 환경변수")
    end = text.index("\n#", start + 1)
    return text[start:end]


def test_env_table_covers_every_name_the_code_reads():
    table = set(_ROW.findall(_reference_env_section()))
    missing = {n: where for n, where in _code_names().items() if n not in table}
    assert not missing, f"docs/REFERENCE.md 환경변수 표에 없는 이름: {missing}"


def test_scan_sees_the_known_readers():
    # 스캔이 비어 가드가 늘 통과하는 일이 없게 — 잘 알려진 것 몇 개
    names = _code_names()
    for n in ("AGENT_CLI_BASE_URL", "AGENT_CLI_NO_INPUT", "AGENT_CLI_SESSIONS_DIR"):
        assert n in names
