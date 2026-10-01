"""옵트인 원문 기록기 (Phase 5 — PHASE5.md §7 코퍼스 재료).

history 는 파싱 결과만, turns 는 지표만 담아 모델이 낸 원문은 어디에도 남지
않았다. ``AGENT_CLI_RECORD_EMISSIONS=1`` 일 때만 파싱 전에 한 줄씩 남긴다."""

from __future__ import annotations

import json
from types import SimpleNamespace

from agent_cli.loop.dispatch import TurnDispatcher


def _fake(session_dir):
    return SimpleNamespace(
        ctx=SimpleNamespace(session_dir=session_dir),
        cfg=SimpleNamespace(wire_format=SimpleNamespace(name="xml_fc")),
    )


def test_off_by_default_writes_nothing(tmp_path, monkeypatch):
    monkeypatch.delenv("AGENT_CLI_RECORD_EMISSIONS", raising=False)
    TurnDispatcher._record_emission(_fake(tmp_path), "<tool_call>…</tool_call>")
    assert not (tmp_path / "emissions.jsonl").exists()


def test_records_raw_text_with_format_when_opted_in(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_CLI_RECORD_EMISSIONS", "1")
    raw = "thinking…\n\n<tool_call>\n<function=shell>\n<parameter=command>ls</parameter>\n</function>\n</tool_call>"
    TurnDispatcher._record_emission(_fake(tmp_path), raw)
    TurnDispatcher._record_emission(_fake(tmp_path), "second")
    rows = [
        json.loads(l) for l in (tmp_path / "emissions.jsonl").read_text().splitlines()
    ]
    assert [r["text"] for r in rows] == [raw, "second"]
    assert rows[0]["format"] == "xml_fc" and "ts" in rows[0]


def test_no_session_dir_is_a_noop(monkeypatch):
    monkeypatch.setenv("AGENT_CLI_RECORD_EMISSIONS", "1")
    TurnDispatcher._record_emission(
        SimpleNamespace(ctx=None, cfg=None), "x"
    )  # no raise
