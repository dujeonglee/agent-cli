"""스펙 구동 방언 vs 옛 손 코딩 모듈의 고정 출력 — 등가성 합격선 (PHASE5.md §7).

옛 모듈은 S5 에서 삭제됐다. 삭제 직전에 그 출력을 ``tests/equivalence/expected/``
에 고정했고(S2·S3 에서 라이브 비교로 바이트 동일을 확인한 뒤), 이 테스트는 그
고정본과 현재 엔진을 비교한다 — 코퍼스(``corpus/<name>.jsonl``) 전건의 parse·
투영·history·재렌더·러너웨이·sanitize·진단, 그리고 산문·문법·플래그 표면 전체.
큰 필드는 sha 로 비교하고 ops 는 그대로 둬 차이가 나면 어디서 났는지 보인다.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from agent_cli.dialects import get
from agent_cli.tools.registry import (
    TOOL_SCHEMAS,
    allows_extra_keys,
    effective_tool_names,
    flat_param_schemas,
)
from agent_cli.tools.virtual import AskTool

HERE = Path(__file__).parent
CORPUS = HERE / "corpus"
EXPECTED = HERE / "expected"
PARITY_FORMATS = ["xml_fc", "json_fc"]


def _sha(x) -> str:
    return hashlib.sha256(
        json.dumps(x, ensure_ascii=False, sort_keys=True).encode()
    ).hexdigest()[:16]


def _corpus(name: str) -> list[str]:
    return [
        json.loads(l)["text"]
        for l in (CORPUS / f"{name}.jsonl").open(encoding="utf-8")
        if l.strip()
    ]


def _expected(name: str) -> list[dict]:
    return [
        json.loads(l)
        for l in (EXPECTED / f"{name}.jsonl").open(encoding="utf-8")
        if l.strip()
    ]


def _surface(name: str) -> dict:
    return json.loads((EXPECTED / f"{name}.surface.json").read_text(encoding="utf-8"))


def _tools(wf, surface: dict | None = None):
    """문법 입력 도구 목록. ``surface["tools"]`` (v10.1.0) 가 있으면 **고정 시점의
    도구 스키마**를 쓴다 — 이 테스트는 방언 엔진의 등가성을 재는 것이지 도구
    스키마의 불변을 재는 것이 아니다(`message` 에 인자 하나 더한 것이 xml_fc
    패리티를 깨뜨렸다). 없으면 현재 레지스트리."""
    frozen = (surface or {}).get("tools")
    out = []
    if frozen:
        for n, p in frozen.items():
            out.append((n, flat_param_schemas(n, p), allows_extra_keys(p)))
        return out
    for n in effective_tool_names(None, wf):
        p = AskTool.RESIDENT_PARAMETERS if n == "ask" else TOOL_SCHEMAS[n].parameters
        out.append((n, flat_param_schemas(n, p), allows_extra_keys(p)))
    return out


@pytest.mark.parametrize("name", PARITY_FORMATS)
class TestParity:
    def test_corpus_and_expected_line_up(self, name):
        corpus, exp = _corpus(name), _expected(name)
        assert len(corpus) > 100 and len(corpus) == len(exp)
        assert all(_sha(t) == e["text_sha"] for t, e in zip(corpus, exp))

    def test_parse_turn_matches_frozen_legacy(self, name):
        wf = get(name)
        diffs = []
        for text, e in zip(_corpus(name), _expected(name)):
            t = wf.parse_turn(text)
            got = {
                "thought": t.thought,
                "ops": [[o.action, o.action_input, o.truncated] for o in t.ops],
                "terminal": t.terminal,
                "raw_sha": _sha(t.raw),
                "parse_stage": t.parse_stage,
                "thinking": t.thinking,
            }
            if got != e["turn"]:
                diffs.append((text[:120], e["turn"]["ops"][:1], got["ops"][:1]))
        assert not diffs, f"{len(diffs)} differing rows; first: {diffs[0]}"

    def test_projection_history_rerender_match(self, name):
        wf = get(name)
        for text, e in zip(_corpus(name), _expected(name)):
            a = wf.parse(text)
            assert (
                _sha(
                    [
                        a.thought,
                        a.action,
                        a.action_input,
                        a.raw,
                        a.parse_stage,
                        a.thinking,
                        a.truncated,
                    ]
                )
                == e["action_sha"]
            ), text[:120]
            rec = wf.serialize_assistant_for_history(text)
            assert _sha(rec) == e["history_sha"], text[:120]
            assert _sha(wf.render_assistant_from_history(rec)) == e["rerender_sha"], (
                text[:120]
            )
            assert wf.is_degenerate(text) == e["degenerate"], text[:120]
            assert _sha(wf.sanitize_thought(text)) == e["sanitized_sha"], text[:120]
            assert _sha(wf.diagnose_syntax_error(text)) == e["diag_sha"], text[:120]

    def test_prose_flags_and_terminal_record(self, name):
        wf, s = get(name), _surface(name)
        assert wf.format_rules() == s["format_rules"]
        for m, v in s["prose"].items():
            assert getattr(wf, m)() == v, m
        assert list(wf.system_user_prefixes()) == s["prefixes"]
        assert [
            wf.name,
            wf.multi_op,
            wf.action_required,
            wf.exposes_complete,
            wf.degeneration_trigger,
            wf.thinking_stop.pattern,
        ] == s["flags"]
        assert (
            wf.serialize_terminal_for_history("done", "result", ["r1"]) == s["terminal"]
        )

    def test_grammar_byte_identical(self, name):
        wf, s = get(name), _surface(name)
        tl = _tools(wf, s)
        assert wf.grammar(tl) == s["grammar"]["off"]
        assert wf.grammar(tl, thinking_open=True) == s["grammar"]["on"]
        extra = [("mcp_x", {"q": ({"type": "string", "minLength": 1}, True)}, True)]
        enum = [
            (
                "pick",
                {
                    "mode": ({"type": "string", "enum": ["a", "b"]}, True),
                    "n": ({"type": "integer"}, False),
                },
                False,
            )
        ]
        assert wf.grammar(tl + extra + enum) == s["grammar"]["extra_enum"]
