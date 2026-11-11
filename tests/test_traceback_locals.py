"""잡히지 않은 예외의 traceback 에 지역 변수를 찍지 않는다 (v10.123.2).

typer 의 pretty exception 은 기본으로 프레임마다 지역 변수를 찍는다 — `run` 의
``api_key``·``resolved_key`` 가 그대로 화면(과 그걸 붙여 넣은 이슈)에 나갔다.
CliRunner 는 그 경로를 안 탄다(typer 가 excepthook 으로 처리) — 서브프로세스로
진짜 진입점을 돌린다.
"""

from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
from pathlib import Path

import agent_cli

_PKG = Path(agent_cli.__file__).parent
_SENTINEL = "SENTINEL-traceback-key-0451"

# `_resolve_provider` 를 터지는 함수로 바꿔 끼우고 진짜 앱을 부른다 — 네트워크 전에
# 터지므로 아무 요청도 나가지 않는다. 터지는 함수도 키를 지역 변수로 든다.
_SCRIPT = """
import sys
import agent_cli.main as m

def _boom(provider, model, base_url, api_key):
    resolved_key = api_key
    raise RuntimeError("forced failure for the traceback test")

m._resolve_provider = _boom
sys.argv = ["agent-cli", "run", "hi", "-p", "openai",
            "--base-url", "http://127.0.0.1:1/v1", "--api-key", sys.argv[1]]
m.app(prog_name="agent-cli")
"""


def test_uncaught_exception_traceback_has_no_locals(tmp_path):
    home = tmp_path / "home"
    (home / ".agent-cli").mkdir(parents=True)
    # 설정이 있어야 setup 마법사가 안 뜬다 — 닫힌 포트
    (home / ".agent-cli" / "config.json").write_text(
        json.dumps({"base_url": "http://127.0.0.1:1/v1"}), encoding="utf-8"
    )
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(("AGENT_CLI_", "OPENAI_", "ANTHROPIC_", "_TYPER"))
    }
    env.update(
        HOME=str(home),
        PYTHONPATH=str(_PKG.parent),
        AGENT_CLI_SESSIONS_DIR=str(tmp_path / "sessions"),
        COLUMNS="200",
    )
    proc = subprocess.run(
        [sys.executable, "-c", _SCRIPT, _SENTINEL],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    out = proc.stdout + proc.stderr
    assert proc.returncode == 1, out
    assert "forced failure for the traceback test" in out  # traceback 은 있다
    assert _SENTINEL not in out


def _typer_calls(tree: ast.AST):
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "Typer"
        ):
            yield node


def test_every_typer_app_disables_locals():
    """세 번째 앱이 생겨도 빠지지 않게 — 패키지 안의 모든 ``typer.Typer(`` 호출."""
    found = []
    for path in sorted(_PKG.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for call in _typer_calls(tree):
            kw = {k.arg: k.value for k in call.keywords}
            value = kw.get("pretty_exceptions_show_locals")
            ok = isinstance(value, ast.Constant) and value.value is False
            found.append((f"{path.relative_to(_PKG)}:{call.lineno}", ok))
    assert len(found) >= 2, found  # app · mcp_app
    assert all(ok for _, ok in found), found
