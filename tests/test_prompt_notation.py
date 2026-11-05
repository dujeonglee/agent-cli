"""모델이 보는 글의 표기 규칙 (v10.77.0) — 재유입 가드.

- 도구 이름·인자 이름은 백틱(`read_file`, `line_start`), 값은 큰따옴표("replace").
- 호출 모양(`read_file(path=…)`, `mode='list'`, `mode:"run"`)은 쓰지 않는다 — 문장으로.
- 대문자 강조(``ONE``, ``NOT``, ``VERBATIM``)는 쓰지 않는다 (v10.81.0, 사용자 결정) —
  머리글자(``JSON``·``URL``), SQL 예시 줄, ``LINE#HASH`` 같은 자리표시, 밑줄 있는
  자리표시(``TOOL_NAME``)만 예외.

검사 대상은 ``prompts.inventory`` 가 모으는 글 전부(도구 설명·인자·안내, 시스템
프롬프트 절, 실행 중 문구)와 형식 오류 넛지의 "You sent:" 줄, 그리고 내장 에이전트·스킬
파일(``agents/builtin/*.md``·``skills/builtin/**/*.md`` — 띄운 에이전트의 시스템 프롬프트와
스킬 지시문이 되지만 inventory 를 거치지 않는다; 대문자 강조만 본다, 사용자 결정 2026-10-30). 형식이
보여 주는 호출 예시(JSON 배열, ``<tool_call>`` 블록, `` `tool` {...} ``)와 줄 머리
이름표(``read_file failed:`` 같은 오류 머리)는 규칙 밖이다.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

import agent_cli
from agent_cli.prompts.inventory import build_inventory
from agent_cli.recovery.primitives import summarize_call
from agent_cli.tools.registry import _BUILTIN_ORDER

_UNDERSCORED = [n for n in _BUILTIN_ORDER if "_" in n]
# 백틱·따옴표·태그·경로 안이 아닌, 밑줄 있는 도구 이름 (단어형 이름은 보통 영어와
# 구별되지 않아 기계 검사 밖이다)
_BARE = re.compile(
    r"(?<![`\w\"'#/.\-\[<=>{])(" + "|".join(_UNDERSCORED) + r")(?![`\w\"'(\]>}])"
)
_CALL = re.compile(
    r"\b(?:" + "|".join(_BUILTIN_ORDER) + r")\([a-z_]+\s*="  # tool(k=…)
    r"|\bmode\s*[:=]\s*['\"]"  # mode='x' / mode:"x"
    r"|\b(?:op|pos|end|search|stat|background|query|sessions|run|ref|to)='"
)
# 형식이 그리는 호출 예시 줄 (glm_argkey 예시는 도구 이름만 있는 줄로 시작한다)
_EXAMPLE = re.compile(
    r'"action"|"name":|<function=|<tool_call>|<arg_key>|^\s*`\w+` \{|^\s*[a-z_]+\s*$'
)
# 따옴표로 감싼 도구 이름 — 도구 이름은 백틱이다 (v10.100.0: 깊이 상한 줄의
# `'run_skill'` 이 `_BARE` 의 따옴표 제외에 걸려 빠져나갔다). 단어형 이름도 따옴표 안이면
# 영어가 아니라 이름이니 함께 본다.
_QUOTED = re.compile(r"'(?:" + "|".join(_BUILTIN_ORDER) + r")'")
# 작은따옴표로 감싼 id 모양 값 — 에이전트 키(`agt-3`·`agt-1a2b3c4d`)와 장부 id(`r-5`·`q-2`).
# 값은 큰따옴표다 (v10.100.0: `agent` 결과가 `'agt-3'` 으로 썼다). 값 일반은 목록이 없어
# 기계로 못 가리니 모양이 정해진 id 만 본다.
_QUOTED_ID = re.compile(r"'(?:agt-[0-9a-z]+|[unrq]-\d+)'")
# 작은따옴표로 감싼 경로·ref 모양 값 (`'src/a.py'`·`'out.txt'`·`'12#KT'`) — 값은 큰따옴표
# (v10.100.0). 큰따옴표 값 안에 다시 든 것(`task "read_file 'a.c' lines 1-40"`)은 겹쳐
# 쓴 따옴표, 낱말 바로 뒤의 `'` 는 소유격(`agent's/…`), 백틱 구간(SQL 예시의 `LIKE 'a/%'`)은 코드라 규칙 밖 — 둘을 먼저 걷어 낸다.
_QUOTED_PATH = re.compile(
    r"(?<![A-Za-z])'[^'\s]*(?:/|\.[A-Za-z]{1,5}\b|\d+#[A-Z]{2})[^'\s]*'"
)
_DQ_SPAN = re.compile(r'"[^"\n]*"')
_LABEL = re.compile(r"^\s*(?:\[)?(?:" + "|".join(_UNDERSCORED) + r")[ :\]]")
# 머리글자·형식 이름 — 뜻이 대문자 그 자체인 것만. 강조로 쓴 보통 단어는 여기 없다.
_ACRONYMS = frozenset(
    [
        *("JSON", "HTML", "XML", "URL", "URLS", "HTTP", "HTTPS", "PDF", "PNG"),
        *("JPEG", "GIF", "SVG", "CSV", "LLM", "SQL", "API", "CLI", "MCP", "SSE"),
        *("EOS", "PID", "CPU", "GPU", "RAM", "UTF", "ASCII", "OCR", "ISO", "UUID"),
        *("ZIP", "YAML", "TOML", "WAL", "TTY", "SIGINT", "SIGTERM", "SIGKILL"),
        *("RAII", "OOM", "ASGI", "README"),
        # windows-dump 스킬 (v10.105.0): 덤프·심볼 형식 이름
        *("PDB", "GUID", "OMAP", "DBI", "MDMP", "IRQL"),
    ]
)
# 세 글자 이상의 대문자 낱말 — 백틱·따옴표·경로·`#`(LINE#HASH)·`$`(변수) 옆과
# 파일 이름(`SKILL.md`)은 뺀다
_CAPS = re.compile(r"(?<![`\w/.\-\$#])([A-Z][A-Z]{2,})(?![`\w#]|\.\w)")
_CODE_SPAN = re.compile(r"`[^`\n]*`")


def _texts():
    inv = build_inventory()
    for dn, d in inv["dialects"].items():
        for t in d["tools"]:
            yield f"{dn} tool {t['name']}", t["description"]
            if t.get("guide"):
                yield f"{dn} guide {t['name']}", t["guide"]
            for pn, pv in (t["parameters"].get("properties") or {}).items():
                yield f"{dn} param {t['name']}.{pn}", pv.get("description") or ""
        for r in d["runtime"]:
            yield f"{dn} runtime {r['id']}", r["text"]
        for scn, sc in d["scenarios"].items():
            for sec in sc.get("sections", []):
                yield f"{dn} {scn} section {sec.get('name')}", sec.get("text") or ""


_ALL = list(_texts())


def _lines(text):
    for line in (text or "").split("\n"):
        if _EXAMPLE.search(line) or _LABEL.match(line) or "SELECT" in line:
            continue
        # Available Tools 의 "- read_file: …" 머리 (텍스트 방언 도구 목록)
        line = re.sub(r"^\s*- [a-z_]+: ", "", line)
        yield line


@pytest.mark.parametrize(("where", "text"), _ALL, ids=[w for w, _ in _ALL])
def test_tool_names_are_backticked(where, text):
    bad = [ln.strip() for ln in _lines(text) if _BARE.search(ln)]
    assert not bad, f"{where}: {bad[:3]}"


@pytest.mark.parametrize(("where", "text"), _ALL, ids=[w for w, _ in _ALL])
def test_tool_names_are_not_single_quoted(where, text):
    bad = [ln.strip() for ln in _lines(text) if _QUOTED.search(ln)]
    assert not bad, f"{where}: {bad[:3]}"


@pytest.mark.parametrize(("where", "text"), _ALL, ids=[w for w, _ in _ALL])
def test_ids_are_not_single_quoted(where, text):
    bad = [ln.strip() for ln in _lines(text) if _QUOTED_ID.search(ln)]
    assert not bad, f"{where}: {bad[:3]}"


@pytest.mark.parametrize(("where", "text"), _ALL, ids=[w for w, _ in _ALL])
def test_paths_and_refs_are_not_single_quoted(where, text):
    bad = [
        ln.strip()
        for ln in _lines(text)
        if _QUOTED_PATH.search(_DQ_SPAN.sub("", _CODE_SPAN.sub("", ln)))
    ]
    assert not bad, f"{where}: {bad[:3]}"


def test_value_samples_are_in_the_inventory():
    """경로·ref 를 싣는 문구가 표본에 있어야 위 가드가 본다."""
    ids = {w.split(" runtime ")[-1] for w, _ in _ALL if " runtime " in w}
    assert {
        "oversized_saved",
        "oversized_source",
        "edit_bare_line_ref",
        "edit_hash_mismatch",
        "edit_lines_not_array",
    } <= ids


def test_browser_result_samples_are_in_the_inventory():
    """`browser` 결과·거절 문구(v10.106.0)는 도구 결과라 표본으로만 가드에 닿는다."""
    ids = {w.split(" runtime ")[-1] for w, _ in _ALL if " runtime " in w}
    assert {
        "browser_same_page_changed",
        "browser_same_page_unchanged",
        "browser_snapshot_cut",
        "browser_ref_errors",
        "browser_page_errors",
        "browser_header_lines",
        "browser_no_match",
        "browser_challenge",
    } <= ids
    text = "\n".join(t for w, t in _ALL if " runtime browser_" in w)
    assert "'" not in text.replace("site's", "")  # repr 작은따옴표 값이 돌아오지 않는다


def test_schedule_result_samples_are_in_the_inventory():
    """`schedule` 결과·거절 문구(v10.108.0)도 도구 결과라 표본으로만 가드에 닿는다."""
    ids = {w.split(" runtime ")[-1] for w, _ in _ALL if " runtime " in w}
    assert {
        "schedule_added",
        "schedule_deleted_empty",
        "schedule_add_rejected",
        "schedule_delete_rejected",
        "schedule_unavailable",
    } <= ids
    text = "\n".join(t for w, t in _ALL if " runtime schedule_" in w)
    assert "'" not in text.replace("user's", "")  # repr 작은따옴표 값이 돌아오지 않는다
    assert "missed 2026-11-03T09:00:00" in text  # 놓친 발화 줄도 표본에 있다


def test_tool_help_samples_are_in_the_inventory():
    """`tool_help` 결과(v10.109.0)도 도구 결과라 표본으로만 가드에 닿는다 — 모르는
    이름의 도구 목록은 백틱(가드가 처음에 잡았다)."""
    ids = {w.split(" runtime ")[-1] for w, _ in _ALL if " runtime " in w}
    assert {"tool_help_result", "tool_help_unknown"} <= ids
    unknown = next(t for w, t in _ALL if w.endswith(" runtime tool_help_unknown"))
    assert "Tools: `read_file`, `edit_file`" in unknown


def test_monitor_result_samples_are_in_the_inventory():
    """`monitor` 거절 문구(v10.110.0)도 표본으로 가드에 닿는다 — 값은 큰따옴표."""
    ids = {w.split(" runtime ")[-1] for w, _ in _ALL if " runtime " in w}
    assert {
        "monitor_validation",
        "monitor_condition_errors",
        "monitor_unknown_id",
    } <= ids
    text = "\n".join(t for w, t in _ALL if " runtime monitor_" in w)
    assert '`pattern` "([" is not a valid regex' in text
    assert 'unknown id "err-watch"' in text and "'" not in text


def test_monitor_report_samples_are_in_the_inventory():
    """`monitor` 의 나머지 결과 문구(v10.111.0) — 목록·등록 안내·발화 보고·삭제가 표본으로
    가드에 닿는다. 등록 안내·보고는 임시 파일로 실제 함수를 불러 만든 글이다."""
    ids = {w.split(" runtime ")[-1] for w, _ in _ALL if " runtime " in w}
    assert {
        "monitor_list_empty",
        "monitor_list",
        "monitor_added",
        "monitor_reports",
        "monitor_deleted",
    } <= ids
    text = "\n".join(t for w, t in _ALL if " runtime monitor_" in w)
    assert "'" not in text  # repr 작은따옴표 값이 돌아오지 않는다
    for phrase in (
        "lines written before now are not reported",
        "Its last line matching the pattern:",
        "is small, so its current contents count as new lines",
        "no change for 400s",
        "the file never appeared",
        "(deadline expired) 3 match(es) total",
        "· 0 hit(s)",  # v10.112.0: 적중 없이 물러난 보고
        "fired once — monitor retired",
        "· repeating",
    ):
        assert phrase in text, phrase
    assert "/work/app.log" in text and "/var/folders" not in text  # 임시 경로는 고정


def test_depth_limit_line_is_in_the_inventory():
    """깊이 상한 줄은 상한에 닿은 루프에만 실린다 — 시나리오가 없으면 가드가 못 본다."""
    texts = [t for w, t in _ALL if " limit section " in w]
    assert any("Depth limit reached" in t for t in texts)


@pytest.mark.parametrize(("where", "text"), _ALL, ids=[w for w, _ in _ALL])
def test_no_call_shapes(where, text):
    bad = [ln.strip() for ln in _lines(text) if _CALL.search(ln)]
    assert not bad, f"{where}: {bad[:3]}"


def caps_emphasis(text) -> list[str]:
    """대문자 강조로 쓰인 낱말들 (머리글자·자리표시 제외) — 가드와 범위 조사가 같은 판정."""
    out = []
    for ln in _lines(text):
        ln = _CODE_SPAN.sub(
            "", ln
        )  # 명령 안의 grep 패턴(`"ERROR\|FATAL"`)은 글이 아니다
        for w in _CAPS.findall(ln):
            if w not in _ACRONYMS:
                out.append(w)
    return out


@pytest.mark.parametrize(("where", "text"), _ALL, ids=[w for w, _ in _ALL])
def test_no_caps_emphasis(where, text):
    """v10.81.0: monitor(v10.79.0)·memory·history 에서 지운 대문자 강조가 다른 글에
    남아 있거나 돌아오면 실패. 모델 문구의 강조는 문장 구조로 — 사용자 결정(2026-10-30)."""
    bad = caps_emphasis(text)
    assert not bad, f"{where}: {sorted(set(bad))}"


_PKG = Path(agent_cli.__file__).parent
_BODIES = sorted(
    [
        *(_PKG / "agents" / "builtin").glob("*.md"),
        *(_PKG / "agents" / "harness").glob("*.md"),
        *(_PKG / "skills" / "builtin").rglob("*.md"),
    ]
)


@pytest.mark.parametrize(
    "path", _BODIES, ids=[str(p.relative_to(_PKG)) for p in _BODIES]
)
def test_no_caps_emphasis_in_builtin_bodies(path):
    """내장 에이전트·스킬 파일은 inventory 밖이지만 모델이 읽는 글이다(에이전트 본문은
    띄운 에이전트의 시스템 프롬프트, 스킬은 호출 시 지시문, references/ 는 스킬이 읽게
    한다). v10.81.0 이 설명(머리)만 고쳐 한 파일 안에 두 표기가 섞였다 — 본문도 같은
    규칙(사용자 결정)."""
    bad = caps_emphasis(path.read_text(encoding="utf-8"))
    assert not bad, f"{path.name}: {sorted(set(bad))}"


def test_you_sent_line_is_a_sentence():
    out = summarize_call(
        "edit_file", {"op": "replace", "pos": "2#KT"}, missing=["path"]
    )
    assert out.startswith('You sent: `edit_file` with `op` "replace", `pos` "2#KT"')
    assert "edit_file(" not in out and "missing: `path`" in out
