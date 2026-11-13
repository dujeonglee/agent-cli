"""`docs/ARCHITECTURE.md` 는 지금의 구조를 그린 지도다 — 지도와 저장소가 어긋나지 않게 지킨다.

§2 디렉토리 트리:
1. 트리가 가리키는 경로가 전부 있다.
2. `agent_cli/` 아래 `.py` 와 `web/static/` 의 `app.js`·`js/*.js` 가 전부 트리에 있다.
3. §2 의 어느 줄도 200자를 넘지 않고, `(123)`·`(~30)`·`(282, …` 꼴의 줄 수 표기가 없다.

문서 전체:
4. `v<숫자>.<숫자>` 꼴 버전 표기가 없다(`Python 3.10`·`>=0.25` 는 걸리지 않는다).
5. 백틱 안의 경로 꼴 토큰과 `docs/….md` 언급이 저장소에 있다(범위는 `_path_candidates`).

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


# ── 문서 전체 ────────────────────────────────────────────────

_VERSION_MARK = re.compile(r"(?<![\w.])v\d+\.\d+")
_FENCE = re.compile(r"^```.*?^```", re.MULTILINE | re.DOTALL)
_INLINE_CODE = re.compile(r"`([^`\n]+)`")
_DOC_MENTION = re.compile(r"(?<![\w/.-])(docs/[\w./-]+\.md)")
_PATH_ROOTS = ("agent_cli/", "docs/", "tests/", "bench/", ".github/", "scripts/")
_SOURCE_SUFFIXES = (".py", ".js")
_PLACEHOLDER = set("<>{}*…$")


def _path_candidates(text: str) -> set[str]:
    """백틱 안의 경로 꼴 토큰(코드 블록 밖) + 문서 어디서든 ``docs/….md`` 언급.

    경로 꼴: ``….py``·``….js`` 로 끝나거나, `_PATH_ROOTS` 로 시작하거나, ``/`` 가 든 채
    ``/`` 로 끝나는 디렉터리(확장자만인 ``.py`` 는 아니다). ``::이름`` 꼬리는 뗀다. 자리표시(``<name>``·``{id}``·``*``·``…``)가
    든 것, 실행 시 만들어지는 프로젝트·유저 경로(``.agent-cli/``·``~/``)는 대상이 아니다.
    """
    out: set[str] = set(_DOC_MENTION.findall(text))  # 설계 문서 언급은 트리 블록 안까지
    body = _FENCE.sub("", text)
    for span in _INLINE_CODE.findall(body):
        for tok in span.split():
            tok = tok.split("::")[0].strip("()[],;:'\"")
            if not tok or tok in _SOURCE_SUFFIXES or _PLACEHOLDER & set(tok):
                continue
            if tok.startswith((".agent-cli", "~", "/", "http")):
                continue
            if (
                tok.endswith(_SOURCE_SUFFIXES)
                or tok.startswith(_PATH_ROOTS)
                or ("/" in tok.rstrip("/") and tok.endswith("/"))
            ):
                out.add(tok)
    return out


def _path_exists(tok: str) -> bool:
    if (REPO / tok).exists() or (PKG / tok).exists():
        return True
    if "/" not in tok:  # 파일 이름만 — 저장소 어딘가에 그 이름이 있으면
        return any(
            "__pycache__" not in p.parts for p in REPO.glob(f"agent_cli/**/{tok}")
        ) or any(REPO.glob(f"tests/**/{tok}"))
    return False


def test_no_version_marks_anywhere():
    bad = [
        f"  줄 {no}: {line.strip()[:90]}"
        for no, line in enumerate(_doc_text().split("\n"), 1)
        if _VERSION_MARK.search(line)
    ]
    assert not bad, (
        "docs/ARCHITECTURE.md 에 버전 표기(vX.Y)가 있다 — 지도는 지금의 구조만 적는다. "
        "언제 바뀌었는지는 git log·릴리스 노트에 맡기고 표기를 지운다:\n"
        + "\n".join(bad)
    )


def test_backticked_paths_exist():
    missing = sorted(t for t in _path_candidates(_doc_text()) if not _path_exists(t))
    assert not missing, (
        "docs/ARCHITECTURE.md 가 없는 경로를 가리킨다(저장소 루트 또는 agent_cli/ 기준으로 찾았다) — "
        "옮겨졌으면 고치고, 없어졌으면 그 문장을 지운다:\n"
        + "\n".join(f"  {m}" for m in missing)
    )


def test_path_candidates_scope():
    text = (
        "`loop/core.py` `agent_cli/tools/registry.py::TOOLS` `python -m agent_cli.prompts.x` "
        "`dialects/specs/<name>.py` `.agent-cli/hooks/` `run_*/` `history.jsonl` "
        "`tests/browser/` `kdump.py` `.py` 그리고 docs/pump/DESIGN.md\n"
        "```\nghost/inside_fence.py  docs/tree/DESIGN.md\n```\n"
    )
    assert _path_candidates(text) == {
        "loop/core.py",
        "agent_cli/tools/registry.py",
        "tests/browser/",
        "kdump.py",
        "docs/pump/DESIGN.md",
        "docs/tree/DESIGN.md",
    }
