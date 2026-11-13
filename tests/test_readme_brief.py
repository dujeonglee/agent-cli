"""README 는 짧은 첫 안내로 남는다 — 레퍼런스는 docs/REFERENCE.md.

README 가 다시 불어나거나, 버전 이력이 섞이거나, 그림·링크가 썩는 것을 막는다.
표준 라이브러리만 쓴다.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
README = ROOT / "README.md"
REFERENCE = ROOT / "docs" / "REFERENCE.md"
IMAGES = ROOT / "docs" / "images"

MAX_LINES = 200
MAX_IMAGE_BYTES = 1024 * 1024

_FENCE = re.compile(r"^\s*(```|~~~)")
_INLINE_CODE = re.compile(r"`+[^`]*`+")
_MD_LINK = re.compile(r"!?\[[^\]]*\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
_IMG_TAG = re.compile(r"<img\s[^>]*src=\"([^\"]+)\"", re.IGNORECASE)
_MD_IMAGE = re.compile(r"!\[[^\]]*\]\(([^)\s]+)")
_HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
_VERSION = re.compile(r"\bv\d+\.\d+\.\d+")
_SCHEME = re.compile(r"^[a-z][a-z0-9+.-]*:", re.IGNORECASE)


def _prose_lines(text: str) -> list[str]:
    """코드 블록 밖의 줄만, 인라인 코드는 지운 채로."""
    out: list[str] = []
    in_fence = False
    for line in text.splitlines():
        if _FENCE.match(line):
            in_fence = not in_fence
            continue
        if not in_fence:
            out.append(_INLINE_CODE.sub("", line))
    return out


def slugify(heading: str) -> str:
    """GitHub 의 제목 앵커 규칙: 소문자 → 글자·숫자·`-`·`_`·공백만 → 공백은 `-`."""
    text = heading.replace("`", "")
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)  # 링크는 글만
    text = text.lower()
    text = "".join(ch for ch in text if ch.isalnum() or ch in "-_ ")
    return text.replace(" ", "-")


def anchors(text: str) -> set[str]:
    """문서의 제목 앵커 — 같은 제목이 반복되면 `-1`, `-2` 가 붙는다."""
    seen: dict[str, int] = {}
    out: set[str] = set()
    in_fence = False
    for line in text.splitlines():
        if _FENCE.match(line):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        m = _HEADING.match(line)
        if not m:
            continue
        slug = slugify(m.group(2))
        n = seen.get(slug, 0)
        seen[slug] = n + 1
        out.add(slug if n == 0 else f"{slug}-{n}")
    return out


def _is_relative(target: str) -> bool:
    return not _SCHEME.match(target)


def _relative_links(text: str) -> list[str]:
    links: list[str] = []
    for line in _prose_lines(text):
        links += _MD_LINK.findall(line)
        links += _IMG_TAG.findall(line)
    return [t for t in links if _is_relative(t)]


def _image_paths(text: str) -> list[str]:
    """저장소 안의 그림만 — 배지 같은 절대 URL 그림은 뺀다."""
    paths: list[str] = []
    for line in _prose_lines(text):
        paths += _MD_IMAGE.findall(line)
        paths += _IMG_TAG.findall(line)
    return [p for p in paths if _is_relative(p)]


def _broken_links(doc: Path) -> list[str]:
    text = doc.read_text(encoding="utf-8")
    broken: list[str] = []
    for target in _relative_links(text):
        path_part, _, frag = target.partition("#")
        dest = (doc.parent / path_part).resolve() if path_part else doc
        if not dest.exists():
            broken.append(f"{target} (no file)")
            continue
        if (
            frag
            and dest.suffix == ".md"
            and frag not in anchors(dest.read_text(encoding="utf-8"))
        ):
            broken.append(f"{target} (no heading)")
    return broken


def test_slugify_matches_github():
    assert slugify("첫 실행 (Setup Wizard)") == "첫-실행-setup-wizard"
    assert slugify("`run` — 단발 실행") == "run--단발-실행"
    assert slugify("🔔 monitor — 조건 감시") == "-monitor--조건-감시"
    text = "# A\n```\n# not a heading\n```\n## A\n"
    assert anchors(text) == {"a", "a-1"}


def test_readme_is_short():
    lines = README.read_text(encoding="utf-8").splitlines()
    assert len(lines) <= MAX_LINES, f"README.md is {len(lines)} lines (max {MAX_LINES})"


def test_readme_has_no_version_marks():
    found = _VERSION.findall(README.read_text(encoding="utf-8"))
    assert not found, f"README.md mentions versions: {found}"


def test_readme_images_exist_and_are_small():
    paths = _image_paths(README.read_text(encoding="utf-8"))
    assert paths, "README.md shows no images"
    used: set[Path] = set()
    for p in paths:
        dest = (ROOT / p).resolve()
        assert dest.parent == IMAGES.resolve(), f"{p} is not under docs/images/"
        assert dest.is_file(), f"{p} does not exist"
        size = dest.stat().st_size
        assert size <= MAX_IMAGE_BYTES, f"{p} is {size} bytes"
        used.add(dest)
    unused = sorted(
        f.name for f in IMAGES.iterdir() if f.is_file() and f.resolve() not in used
    )
    assert not unused, f"docs/images/ has images README.md does not use: {unused}"


def test_readme_links_resolve():
    assert _broken_links(README) == []


def test_reference_links_resolve():
    assert _broken_links(REFERENCE) == []
