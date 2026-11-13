"""`docs/ARCHITECTURE.md` 는 지금의 구조를 그린 지도다 — 지도와 저장소가 어긋나지 않게 지킨다.

§2 디렉토리 트리:
1. 트리가 가리키는 경로가 전부 있다.
2. `agent_cli/` 아래 `.py` 와 `web/static/` 의 `app.js`·`js/*.js` 가 전부 트리에 있다.
3. §2 의 어느 줄도 200자를 넘지 않고, `(123)`·`(~30)`·`(282, …` 꼴의 줄 수 표기가 없다.

트리 파서는 이 파일 안에 있다: 들여쓰기 깊이(4칸 단위 + `├── `/`└── `) → 경로.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DOC = REPO / "docs" / "ARCHITECTURE.md"
PKG = REPO / "agent_cli"

MAX_LINE = 200
_CONNECTORS = ("├── ", "└── ")
_LOC_MARK = re.compile(r"\(~?\d+[\s,)]")


def _section(text: str, number: int) -> list[str]:
    """`## <number>.` 제목부터 다음 `## ` 제목 전까지의 줄."""
    lines = text.split("\n")
    start = next(i for i, ln in enumerate(lines) if ln.startswith(f"## {number}. "))
    end = next(
        (i for i in range(start + 1, len(lines)) if lines[i].startswith("## ")),
        len(lines),
    )
    return lines[start:end]


def _tree_block(section: list[str]) -> list[str]:
    """절 안의 첫 코드 블록(트리)의 줄."""
    fences = [i for i, ln in enumerate(section) if ln.startswith("```")]
    assert len(fences) >= 2, "§2 에 트리 코드 블록(```)이 없다"
    return section[fences[0] + 1 : fences[1]]


def parse_tree(block: list[str]) -> list[tuple[int, str]]:
    """트리 줄 → ``[(줄 번호, 저장소 루트 기준 경로)]``.

    연결자 없는 줄은 저장소 루트의 항목(깊이 0), ``├── ``/``└── `` 앞의 4칸 단위 수가
    n 이면 깊이 n+1. 이름은 연결자 뒤 첫 공백까지, 디렉터리는 ``/`` 로 끝난다.
    """
    out: list[tuple[int, str]] = []
    stack: list[str] = []
    for no, line in enumerate(block):
        if not line.strip():
            continue
        pos = -1
        for c in _CONNECTORS:
            p = line.find(c)
            if p != -1 and (pos == -1 or p < pos):
                pos = p
        if pos == -1:
            level, rest = 0, line
        else:
            prefix = line[:pos]
            assert len(prefix) % 4 == 0 and set(prefix) <= {"│", " "}, (
                f"§2 트리 줄 {no + 1} 의 들여쓰기가 4칸 단위가 아니다: {line!r}"
            )
            level, rest = len(prefix) // 4 + 1, line[pos + len(_CONNECTORS[0]) :]
        name = rest.split()[0]
        assert level <= len(stack), (
            f"§2 트리 줄 {no + 1} 이 부모 디렉터리 없이 깊어진다: {line!r}"
        )
        del stack[level:]
        parent = "".join(stack)
        out.append((no + 1, parent + name))
        if name.endswith("/"):
            stack.append(name)
    return out


def _doc_text() -> str:
    return DOC.read_text(encoding="utf-8")


def _tree_paths() -> list[tuple[int, str]]:
    return parse_tree(_tree_block(_section(_doc_text(), 2)))


def test_tree_parser_on_sample():
    block = [
        "agent_cli/",
        "├── a.py                 설명",
        "├── sub/                 디렉터리",
        "│   ├── b.py             설명",
        "│   └── deep/",
        "│       └── c.js         설명",
        "└── d.py                 설명",
        "",
        "pyproject.toml           설명",
    ]
    assert [p for _, p in parse_tree(block)] == [
        "agent_cli/",
        "agent_cli/a.py",
        "agent_cli/sub/",
        "agent_cli/sub/b.py",
        "agent_cli/sub/deep/",
        "agent_cli/sub/deep/c.js",
        "agent_cli/d.py",
        "pyproject.toml",
    ]


def test_tree_paths_exist():
    missing = [
        f"  트리 줄 {no}: {path}"
        for no, path in _tree_paths()
        if not (REPO / path).exists()
    ]
    assert not missing, (
        "docs/ARCHITECTURE.md §2 트리가 없는 경로를 가리킨다 — 파일이 없어졌거나 옮겨졌으면 "
        "그 줄을 지우거나 고친다:\n" + "\n".join(missing)
    )


def _required_files() -> set[str]:
    req = {
        p.relative_to(REPO).as_posix()
        for p in PKG.rglob("*.py")
        if "__pycache__" not in p.parts
    }
    static = PKG / "web" / "static"
    req.add((static / "app.js").relative_to(REPO).as_posix())
    req |= {p.relative_to(REPO).as_posix() for p in (static / "js").glob("*.js")}
    return req


def test_every_source_file_is_on_the_map():
    listed = {path for _, path in _tree_paths()}
    missing = sorted(_required_files() - listed)
    assert not missing, (
        "docs/ARCHITECTURE.md §2 트리에 없는 파일 — 디렉터리 자리에 '파일 하나 = 한 줄 = "
        "지금 하는 일' 로 올린다:\n" + "\n".join(f"  {m}" for m in missing)
    )


def test_section2_lines_short_and_without_line_counts():
    bad_len, bad_loc = [], []
    for no, line in enumerate(_section(_doc_text(), 2), 1):
        if len(line) > MAX_LINE:
            bad_len.append(f"  §2 줄 {no} ({len(line)}자): {line[:60]}…")
        if _LOC_MARK.search(line):
            bad_loc.append(f"  §2 줄 {no}: {line[:80]}")
    assert not bad_len, (
        f"docs/ARCHITECTURE.md §2 의 줄이 {MAX_LINE}자를 넘는다 — 지금의 역할 한 줄로 줄이고, "
        "이력은 git log 에 맡긴다:\n" + "\n".join(bad_len)
    )
    assert not bad_loc, (
        "docs/ARCHITECTURE.md §2 에 줄 수 표기 `(123)` 가 있다 — 지도는 줄 수를 적지 않는다:\n"
        + "\n".join(bad_loc)
    )
