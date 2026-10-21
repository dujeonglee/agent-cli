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


@pytest.mark.parametrize(
    ("name", "stripper"),
    [
        ("index.html", strip_html_comments),
        ("app.js", strip_js_comments),
        ("style.css", strip_js_comments),
    ],
)
def test_web_static_has_no_korean_outside_comments(name, stripper):
    hits = _hangul_lines(stripper((STATIC / name).read_text(encoding="utf-8")))
    assert not hits, f"{name}: Korean in user-facing code — " + "; ".join(hits[:5])


def test_strippers_keep_comments_out_and_code_in():
    js = 'a = "ok"; // 주석\n/* 블록\n 주석 */ b = "본문";\n'
    assert _hangul_lines(strip_js_comments(js)) == ['3: b = "본문";']
    html = "<!-- 주석 -->\n<p>본문</p>\n<!--\n 여러 줄\n-->\n"
    assert _hangul_lines(strip_html_comments(html)) == ["2: <p>본문</p>"]
