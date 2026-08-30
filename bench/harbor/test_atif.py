"""bench/harbor/atif.py 단위 테스트 — ``python3 -m pytest bench/harbor``
(제품 스위트 ``tests/`` 밖; harbor 불필요)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import atif


def _turn(**kw):
    base = {
        "model": "m",
        "timestamp": "t",
        "parse_stage": 1,
        "failure_signal": None,
        "primitives_applied": [],
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": 0,
    }
    base.update(kw)
    return base


HISTORY = [
    {"role": "system", "content": "sys"},
    {"role": "user", "content": "[u]: make hello.txt", "author": "u", "ts": "t0"},
    {
        "role": "assistant",
        "thought": "write it",
        "ops": [
            {
                "action": "write_file",
                "action_input": {"path": "hello.txt", "content": "hi"},
            },
            {"action": "shell", "action_input": {"command": "cat hello.txt"}},
        ],
        "ts": "t1",
    },
    {
        "role": "user",
        "tool": "write_file",
        "success": True,
        "content": "Observation: saved",
    },
    {"role": "user", "tool": "shell", "success": True, "content": "Observation: hi"},
    {"role": "assistant", "content": "garbled", "ts": "t2"},  # raw (NO_JSON)
    {
        "role": "user",
        "tool": "",
        "recovery": "format",
        "content": "Observation: emit JSON",
    },
    {
        "role": "assistant",
        "thought": "done",
        "ops": [
            {"action": "complete", "action_input": {"result": "created hello.txt"}}
        ],
        "ts": "t3",
    },
]
TURNS = [
    _turn(input_tokens=100, output_tokens=10, cache_read_input_tokens=50),
    _turn(parse_stage=0, failure_signal="NO_JSON", input_tokens=120, output_tokens=3),
    _turn(input_tokens=130, output_tokens=8, cache_creation_input_tokens=7),
    {"event": "compaction", "tokens_before": 1, "tokens_after": 1},
]


def test_usage_totals_include_cache_in_input():
    t = atif.usage_totals(TURNS)
    assert t == {
        "input": 100 + 50 + 120 + 130 + 7,
        "output": 21,
        "cache": 50,
        "turns": 3,
    }


def test_health_counts_failures_and_stages():
    h = atif.health(TURNS)
    assert h == {
        "turns": 3,
        "failures": {"NO_JSON": 1},
        "parse_stage": {"1": 2, "0": 1},
    }


def test_trajectory_shape():
    tr = atif.build_trajectory(
        HISTORY,
        TURNS,
        agent_name="agent-cli",
        agent_version="8.50.0",
        model_name="qwen",
        session_id="123",
    )
    assert tr["schema_version"] == "ATIF-v1.7"
    assert tr["agent"] == {
        "name": "agent-cli",
        "version": "8.50.0",
        "model_name": "qwen",
    }
    steps = tr["steps"]
    assert [s["step_id"] for s in steps] == [1, 2, 3, 4, 5]
    assert [s["source"] for s in steps] == ["user", "agent", "agent", "system", "agent"]
    # user query: author prefix stripped, system record dropped
    assert steps[0]["message"] == "make hello.txt"
    # multi-op action: tool calls + observations matched in order
    a = steps[1]
    assert [c["function_name"] for c in a["tool_calls"]] == ["write_file", "shell"]
    assert a["tool_calls"][0]["arguments"] == {"path": "hello.txt", "content": "hi"}
    assert [r["content"] for r in a["observation"]["results"]] == ["saved", "hi"]
    assert (
        a["observation"]["results"][1]["source_call_id"]
        == a["tool_calls"][1]["tool_call_id"]
    )
    assert a["metrics"] == {
        "prompt_tokens": 150,
        "completion_tokens": 10,
        "cached_tokens": 50,
    }
    # raw turn keeps the verbatim text; harness intervention nudge is a system step
    assert steps[2]["message"] == "garbled" and "tool_calls" not in steps[2]
    assert steps[3]["message"] == "Observation: emit JSON"
    # final: complete result is the message, thought → reasoning_content
    assert steps[4]["message"] == "created hello.txt"
    assert steps[4]["reasoning_content"] == "done"
    assert "tool_calls" not in steps[4]
    assert tr["final_metrics"]["total_prompt_tokens"] == 407
    assert tr["final_metrics"]["total_steps"] == 5


def test_read_jsonl_tolerates_truncated_tail(tmp_path):
    p = tmp_path / "x.jsonl"
    p.write_text('{"a": 1}\n{"a": 2\n')
    assert atif.read_jsonl(p) == [{"a": 1}]


def test_newest_session_dir(tmp_path):
    assert atif.newest_session_dir(tmp_path / "none") is None
    (tmp_path / "1").mkdir()
    (tmp_path / "2").mkdir()
    assert atif.newest_session_dir(tmp_path).name in {"1", "2"}
    json.dumps(
        atif.build_trajectory(
            [], [], agent_name="a", agent_version="v", model_name=None, session_id=None
        )
    )
