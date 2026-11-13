"""사용자 표면은 영어 하나 (v10.62.0).

웹 정적 파일의 **코드 안** 문자열(라벨·툴팁·placeholder·상태 줄)에 한글이 다시
섞이는 것을 막는다. 주석은 작업 언어라 한글 그대로 둔다 — 검사는 주석을
벗긴 뒤의 본문에만 건다. 검사기는 거칠다(문자열 안의 ``//`` 를 주석으로
오인할 수 있다) — 그 경우 실제 코드가 아니라 문구를 고쳐 피한다.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parents[1] / "agent_cli" / "web" / "static"
# v10.116.0: 프런트가 js/ 모듈로 나뉘었다 — 정적 파일 전부를 본다(새 모듈이 생겨도 빠지지 않게).
_STATIC_FILES = ["index.html", "style.css", "app.js"] + sorted(
    f"js/{p.name}" for p in (STATIC / "js").glob("*.js")
)
HANGUL = re.compile(r"[가-힣]")


def strip_js_comments(text: str) -> list[str]:
    """줄마다 ``//``·``/* */`` 주석을 벗긴 본문."""
    out: list[str] = []
    in_block = False
    for line in text.splitlines():
        code = ""
        j = 0
        while j < len(line):
            if in_block:
                k = line.find("*/", j)
                if k < 0:
                    j = len(line)
                else:
                    in_block = False
                    j = k + 2
                continue
            if line.startswith("/*", j):
                in_block = True
                j += 2
                continue
            if line.startswith("//", j):
                break
            code += line[j]
            j += 1
        out.append(code)
    return out


def strip_html_comments(text: str) -> list[str]:
    stripped = re.sub(
        r"<!--.*?-->", lambda m: "\n" * m.group(0).count("\n"), text, flags=re.DOTALL
    )
    return stripped.splitlines()


def _hangul_lines(lines: list[str]) -> list[str]:
    return [
        f"{i}: {ln.strip()[:120]}" for i, ln in enumerate(lines, 1) if HANGUL.search(ln)
    ]


def test_static_file_list_covers_modules():
    assert len(_STATIC_FILES) > 4 and "js/channels.js" in _STATIC_FILES


@pytest.mark.parametrize("name", _STATIC_FILES)
def test_web_static_has_no_korean_outside_comments(name):
    stripper = strip_html_comments if name.endswith(".html") else strip_js_comments
    hits = _hangul_lines(stripper((STATIC / name).read_text(encoding="utf-8")))
    assert not hits, f"{name}: Korean in user-facing code — " + "; ".join(hits[:5])


def test_strippers_keep_comments_out_and_code_in():
    js = 'a = "ok"; // 주석\n/* 블록\n 주석 */ b = "본문";\n'
    assert _hangul_lines(strip_js_comments(js)) == ['3: b = "본문";']
    html = "<!-- 주석 -->\n<p>본문</p>\n<!--\n 여러 줄\n-->\n"
    assert _hangul_lines(strip_html_comments(html)) == ["2: <p>본문</p>"]


# ── CLI 도움말 (v10.127.5) ────────────────────────────────────────────
# `agent-cli mcp --help` 에 한글 docstring 이 그대로 나왔다. 정적으로 docstring 을 긁지
# 않고 typer 가 실제로 보여 주는 `--help` 출력을 명령 트리 전부에서 뽑아 본다 —
# 숨긴 명령·하위 그룹·옵션 help 까지(typer 가 무엇을 고르는지가 기준이다).


def _cli_command_paths() -> list[list[str]]:
    from typer.main import get_command

    from agent_cli.main import app

    def walk(cmd, path):
        yield path
        for name, sub in sorted((getattr(cmd, "commands", None) or {}).items()):
            yield from walk(sub, [*path, name])

    return list(walk(get_command(app), []))


def test_cli_command_tree_is_walked():
    paths = {" ".join(p) for p in _cli_command_paths()}
    assert {"", "run", "web", "sessions", "mcp", "mcp add", "mcp remove"} <= paths


@pytest.mark.parametrize(
    "path", _cli_command_paths(), ids=lambda p: " ".join(p) or "(root)"
)
def test_cli_help_has_no_korean(path, tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from agent_cli.main import app

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    r = CliRunner().invoke(app, [*path, "--help"])
    assert r.exit_code == 0, r.output
    # typer 는 GITHUB_ACTIONS 가 있으면 도움말에 색을 강제한다 — `--help` 가
    # `-` 와 `-help` 로 따로 칠해져 평문 비교가 CI 에서만 빗나간다.
    plain = re.sub(r"\x1b\[[0-9;]*m", "", r.output)
    assert "--help" in plain
    hits = [ln.strip() for ln in plain.splitlines() if HANGUL.search(ln)]
    assert not hits, f"agent-cli {' '.join(path)} --help: " + "; ".join(hits[:5])
    # 프런트엔드가 없던 때의 문단("Phase A scope … Phase B")이 web --help 에 남아 있었다.
    flat = " ".join(plain.split())
    assert "Phase A" not in flat and "Phase B" not in flat, flat
