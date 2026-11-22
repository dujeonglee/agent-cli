"""흡수된 관찰 레코드의 ``item`` 칸은 화면 전용이다 (v10.165.0) — 모델이 읽는 어느 길에도 실리지 않는다.

`AgentRegistry.absorb_pending` 이 그 배달의 id 를 레코드의 ``item`` 에 싣는다(웹이 관찰 카드의 ``data-item`` 으로 받아
작업 블록 머리 줄에서 찾아간다). 값이 id(``r-4``)라 본문에도 같은 글자가 있을 수 있다 — 그래서 여기서는 본문 어디에도
없는 표지 값을 칸에 넣고, 모델 호출 본문·압축 요약 호출 본문(경계 모양·전사 모양)·`history` 도구 출력에서 찾는다.
"""

from __future__ import annotations

import json

import agent_cli.tools.history as history_mod
from agent_cli.tools import TOOLS, RunContext
from tests.test_compaction_boundary import (
    _ERR_STREAM,
    SUMMARY,
    _compact,
    _fill,
    _loop,
    _main_body,
    _sse,
    _text_stream,
)

SENTINEL = "ZZ-DISPLAY-ONLY-ITEM"


def _absorbed(text: str, **extra) -> dict:
    return {
        "role": "user",
        "tool": "agent",
        "success": True,
        "content": f"── request from main ──\n{text}",
        "source": "agent_request",
        "item": SENTINEL,
        **extra,
    }


def _ctx_with_absorbed_records(tmp_path, dialect: str = "json_fc"):
    loop, ctx = _loop(tmp_path, dialect)
    _fill(ctx, n=2)
    ctx.add(_absorbed("early request"))
    _fill(ctx, n=6)
    ctx.add(_absorbed("late request", request_id="r-9"))
    return loop, ctx


def test_the_model_call_does_not_carry_it(tmp_path):
    loop, _ctx = _ctx_with_absorbed_records(tmp_path)
    body = json.dumps(_main_body(loop), ensure_ascii=False)
    assert "early request" in body and "late request" in body
    assert SENTINEL not in body


def test_the_boundary_summary_call_does_not_carry_it(tmp_path):
    ctx = _ctx_with_absorbed_records(tmp_path)[1]
    result, bodies = _compact(ctx, _text_stream(SUMMARY))
    assert result.compacted and len(bodies) == 1
    sent = json.dumps(bodies, ensure_ascii=False)
    assert "early request" in sent and SENTINEL not in sent


def test_the_transcript_summary_call_does_not_carry_it(tmp_path):
    ctx = _ctx_with_absorbed_records(tmp_path)[1]
    result, bodies = _compact(ctx, _sse(list(_ERR_STREAM)), _text_stream(SUMMARY))
    assert result.compacted and len(bodies) == 2  # 경계 모양 실패 → 전사 모양
    sent = json.dumps(bodies, ensure_ascii=False)
    assert "early request" in sent and SENTINEL not in sent


def test_the_history_tool_does_not_show_it(tmp_path, monkeypatch):
    _ctx_with_absorbed_records(tmp_path)
    session_dir = tmp_path / "session"
    assert SENTINEL in (session_dir / "history.jsonl").read_text(encoding="utf-8")
    monkeypatch.setattr(history_mod, "_SESSIONS_DIR", tmp_path)
    for query in (
        "SELECT * FROM history",
        "SELECT * FROM history WHERE text LIKE '%early request%'",
    ):
        out = TOOLS["history"].run(
            {"query": query}, ctx=RunContext(session_dir=session_dir)
        )
        assert out.success and "early request" in out.output
        assert SENTINEL not in out.output
