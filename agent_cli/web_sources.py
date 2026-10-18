"""정보원 목록 — 꼬리 ``## Web Sources`` (v10.57.0, docs/web-sources/DESIGN.md).

기본 목록은 패키지(``default_web_sources.json``)에 있고 전부 켜져 있다. 사용자가
바꾼 것만 프로젝트의 ``.agent-cli/web-sources.json`` 에 둔다::

    {"disabled": ["vulns.nvd"],
     "added": [{"id": "user-1", "group": "wifi", "text": "gerrit.mycorp.com/…"}]}

바꾼 것만 저장하므로 다음 버전에서 기본 정보원이 늘거나 주소가 바뀌어도 그대로
따라간다(파일이 기본 목록의 사본이면 옛 목록에 묶인다). 웹은 🌐 드로어의
체크박스로, CLI 는 이 파일을 직접 고쳐 바꾼다.

자리가 꼬리인 이유 — A/B 96런(8과제 × 3회 × 4변형, Qwen3.6-35B): 꼬리 24/24·
첫 호출이 목록 22/24, ``fetch`` 설명에 둔 판 22/24·8/24(같은 주소를 ``/v1`` 을
빠뜨린 채 지어내 51번 되풀이). 일하는 방식의 안내라서 도구 설명보다 꼬리에
맞는다(Task Guidelines 를 꼬리로 옮긴 v8.52.0 과 같은 결).
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

FILE_NAME = "web-sources.json"
BLOCK_HEADER = "## Web Sources"
_INDENT = " " * 10  # "  " + 그룹 칸 7 + " "
MAX_TEXT = 500
MAX_GROUP = 12


@lru_cache(maxsize=1)
def _defaults() -> dict:
    path = Path(__file__).with_name("default_web_sources.json")
    return json.loads(path.read_text(encoding="utf-8"))


def overrides_file() -> Path:
    """``cwd/.agent-cli/web-sources.json``."""
    from agent_cli.paths import project_dir

    return project_dir() / FILE_NAME


def read_overrides(path: Path | None = None) -> dict:
    """``{"disabled": [...], "added": [...]}`` — 파일이 없거나 깨졌으면 빈 변경
    (기본 목록 전부 켜짐). 깨진 파일 때문에 런이 멈추지 않게."""
    f = path or overrides_file()
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"disabled": [], "added": []}
    if not isinstance(data, dict):
        return {"disabled": [], "added": []}
    disabled = [str(x) for x in data.get("disabled") or [] if isinstance(x, str)]
    added = [a for a in (data.get("added") or []) if _valid_added(a)]
    return {"disabled": disabled, "added": added}


def _valid_added(a) -> bool:
    return (
        isinstance(a, dict)
        and isinstance(a.get("id"), str)
        and a["id"].strip() != ""
        and isinstance(a.get("text"), str)
        and 0 < len(a["text"].strip()) <= MAX_TEXT
        and isinstance(a.get("group"), str)
        and 0 < len(a["group"].strip()) <= MAX_GROUP
    )


def entries(path: Path | None = None) -> list[dict]:
    """기본 + 추가 항목, 그룹 순서대로 — ``{id, group, text, enabled, builtin, tokens}``.

    그룹 순서는 기본 목록의 순서, 새 그룹은 처음 나온 순서로 뒤에."""
    from agent_cli.context.token_estimator import estimate_tokens

    ov = read_overrides(path)
    off = set(ov["disabled"])
    builtin = {s["id"] for s in _defaults()["sources"]}
    rows = [
        {**s, "enabled": s["id"] not in off, "builtin": True}
        for s in _defaults()["sources"]
    ] + [
        {
            "id": a["id"],
            "group": a["group"].strip(),
            "text": a["text"].strip(),
            "enabled": a["id"] not in off,
            "builtin": False,
        }
        for a in ov["added"]
        if a["id"] not in builtin
    ]
    order: list[str] = []
    for r in rows:
        if r["group"] not in order:
            order.append(r["group"])
    rows.sort(key=lambda r: order.index(r["group"]))  # 안정 정렬 — 그룹 안 순서 유지
    for r in rows:
        r["tokens"] = estimate_tokens(_lines(r["group"], r["text"], first=True))
    return rows


def _lines(group: str, text: str, *, first: bool) -> str:
    """한 항목의 줄들 — 그룹의 첫 항목만 그룹 이름을 단다."""
    head, *rest = text.split("\n")
    lead = f"  {group:<7} " if first else _INDENT
    return "\n".join([lead + head, *(_INDENT + line for line in rest)])


def render(path: Path | None = None) -> str:
    """꼬리 블록 — 켜진 항목이 없으면 ``""``."""
    on = [r for r in entries(path) if r["enabled"]]
    if not on:
        return ""
    out = [BLOCK_HEADER, _defaults()["header"]]
    prev = None
    for r in on:
        out.append(_lines(r["group"], r["text"], first=r["group"] != prev))
        prev = r["group"]
    return "\n".join(out)


def save(disabled: list[str], added: list[dict], path: Path | None = None) -> dict:
    """바꾼 것만 저장한다. 아무것도 안 바꿨으면 파일을 지운다(기본 그대로).

    ``ValueError`` — 잘못된 추가 항목·중복 id."""
    known = {s["id"] for s in _defaults()["sources"]}
    clean: list[dict] = []
    seen = set(known)
    for a in added:
        if not _valid_added(a):
            raise ValueError(f"invalid source: {a!r}")
        if a["id"] in seen:
            raise ValueError(f"duplicate id: {a['id']}")
        seen.add(a["id"])
        clean.append(
            {"id": a["id"], "group": a["group"].strip(), "text": a["text"].strip()}
        )
    off = sorted({str(d) for d in disabled} & seen)
    f = path or overrides_file()
    if not off and not clean:
        f.unlink(missing_ok=True)
        return {"disabled": [], "added": []}
    f.parent.mkdir(parents=True, exist_ok=True)
    data = {"disabled": off, "added": clean}
    f.write_text(json.dumps(data, ensure_ascii=False, indent=1) + "\n", "utf-8")
    return data
