"""agent-cli 세션 파일 → ATIF(Agent Trajectory Interchange Format) 변환.

순수 stdlib — harbor 를 import 하지 않으므로 어댑터 없이도 단독 테스트
가능하다(``python3 -m pytest bench/harbor``). 어댑터
(``agent_cli_harbor.py``)가 trial 종료 후 호스트에서 호출한다.

입력은 세션 디렉토리의 두 파일:

* ``history.jsonl`` — 대화 레코드. 각 줄의 shape 은 ``agent_cli/context/
  records.py::_classify_record`` 가 정의하는 ``kind`` 로 구분된다:
  ``query``(사용자 요청) / ``action``·``final``·``raw``(assistant 턴 —
  ``thought`` + ``ops[{action, action_input}]``, final 은 ``complete`` op
  포함, raw 는 파싱 실패 원문) / ``observation``(도구 결과 — ``tool``·
  ``success``·``content``) / 그 외 user 레코드(개입 넛지·ask 답변).
* ``turns.jsonl`` — LLM 호출 1회당 1행(``parse_stage``·``failure_signal``·
  토큰 4필드, v8.49.0). assistant 레코드와 파일 순서로 1:1 대응한다.

출력은 ATIF-v1.7 dict — ``harbor.models.trajectories.Trajectory`` 로
검증 가능(``python -m harbor.utils.trajectory_validator``).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

_OBSERVATION_PREFIX = "Observation: "


def read_jsonl(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    rows: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue  # 크래시로 잘린 마지막 줄 관용 (observability 계약)
        if isinstance(row, dict):
            rows.append(row)
    return rows


# ----- turns.jsonl 집계 -----------------------------------------------------


def turn_rows(turns: list[dict]) -> list[dict]:
    """``event`` 행(compaction)을 뺀 LLM-턴 행만."""
    return [r for r in turns if "event" not in r]


def usage_totals(turns: list[dict]) -> dict[str, int]:
    """세션 합계. ``input`` 은 캐시 포함 총 프롬프트 토큰(Harbor 의
    ``n_input_tokens`` 정의: "including cache"), ``cache`` 는 캐시 read."""
    rows = turn_rows(turns)
    inp = sum(
        int(r.get("input_tokens", 0) or 0)
        + int(r.get("cache_read_input_tokens", 0) or 0)
        + int(r.get("cache_creation_input_tokens", 0) or 0)
        for r in rows
    )
    return {
        "input": inp,
        "output": sum(int(r.get("output_tokens", 0) or 0) for r in rows),
        "cache": sum(int(r.get("cache_read_input_tokens", 0) or 0) for r in rows),
        "turns": len(rows),
    }


def health(turns: list[dict]) -> dict[str, Any]:
    """종전 SWE-bench 하니스의 헬스 표와 같은 집계 — 형식 실패·parse_stage
    분포. Harbor ``AgentContext.metadata`` 에 실린다."""
    rows = turn_rows(turns)
    fails: dict[str, int] = {}
    stages: dict[str, int] = {}
    for r in rows:
        stages[str(r.get("parse_stage"))] = stages.get(str(r.get("parse_stage")), 0) + 1
        s = r.get("failure_signal")
        if s:
            fails[s] = fails.get(s, 0) + 1
    return {"turns": len(rows), "failures": fails, "parse_stage": stages}


# ----- history.jsonl → ATIF steps ------------------------------------------


def _metrics(row: dict | None) -> dict | None:
    if not row:
        return None
    cache = int(row.get("cache_read_input_tokens", 0) or 0)
    prompt = (
        int(row.get("input_tokens", 0) or 0)
        + cache
        + int(row.get("cache_creation_input_tokens", 0) or 0)
    )
    return {
        "prompt_tokens": prompt,
        "completion_tokens": int(row.get("output_tokens", 0) or 0),
        "cached_tokens": cache,
    }


def _arguments(action_input: Any) -> dict:
    if isinstance(action_input, dict):
        return action_input
    if action_input is None:
        return {}
    return {"input": action_input}


def _strip_author(record: dict) -> str:
    content = str(record.get("content") or "")
    author = record.get("author")
    if author and content.startswith(f"[{author}]: "):
        return content[len(f"[{author}]: ") :]
    return content


def _agent_step(record: dict, turn_row: dict | None, call_prefix: str) -> dict:
    """assistant 레코드 → agent step (observation 은 나중에 채움)."""
    ops = record.get("ops")
    thought = str(record.get("thought") or "")
    step: dict[str, Any] = {"source": "agent"}
    if not isinstance(ops, list) or not ops:
        # raw: 파싱 실패 원문 그대로
        step["message"] = str(record.get("content") or "")
    else:
        tool_calls = []
        final_text = None
        for i, op in enumerate(ops):
            if not isinstance(op, dict) or not op.get("action"):
                continue
            action = str(op["action"])
            args = _arguments(op.get("action_input"))
            if action == "complete":
                final_text = str(args.get("result", "") or "")
                continue
            tool_calls.append(
                {
                    "tool_call_id": f"{call_prefix}_{i}",
                    "function_name": action,
                    "arguments": args,
                }
            )
        step["message"] = final_text if final_text is not None else thought
        if thought and final_text is not None:
            step["reasoning_content"] = thought
        if tool_calls:
            step["tool_calls"] = tool_calls
    m = _metrics(turn_row)
    if m:
        step["metrics"] = m
    if record.get("ts"):
        step["timestamp"] = record["ts"]
    return step


def build_trajectory(
    history: list[dict],
    turns: list[dict],
    *,
    agent_name: str,
    agent_version: str,
    model_name: str | None,
    session_id: str | None,
) -> dict:
    """ATIF-v1.7 dict. 관찰(observation) 레코드는 직전 agent step 의
    tool_calls 에 순서대로 붙는다(agent-cli 는 op 당 관찰 1건을 같은 순서로
    기록). 남는 관찰(개입·ask 답변 등 tool 없는 user 레코드)은 user step."""
    llm_rows = turn_rows(turns)
    steps: list[dict] = []
    assistant_idx = 0
    pending_calls: list[str] = []

    for record in history:
        role = record.get("role")
        if role == "system":
            continue
        if role == "assistant":
            row = llm_rows[assistant_idx] if assistant_idx < len(llm_rows) else None
            step = _agent_step(record, row, call_prefix=f"call_{assistant_idx + 1}")
            assistant_idx += 1
            steps.append(step)
            pending_calls = [c["tool_call_id"] for c in step.get("tool_calls", [])]
            continue
        # 하니스 개입 넛지(형식 복구: recovery 마킹 또는 빈 tool) 는 도구
        # 관찰이 아니라 시스템 발화 — records.is_format_intervention 계약
        is_intervention = bool(record.get("recovery")) or (
            "tool" in record and not record.get("tool")
        )
        if (
            role == "user"
            and record.get("tool")
            and not is_intervention
            and steps
            and steps[-1]["source"] == "agent"
        ):
            content = str(record.get("content") or "").removeprefix(_OBSERVATION_PREFIX)
            result: dict[str, Any] = {"content": content}
            if pending_calls:
                result["source_call_id"] = pending_calls.pop(0)
            obs = steps[-1].setdefault("observation", {"results": []})
            obs["results"].append(result)
            continue
        # 사용자 요청 / ask 답변 → user, 개입 → system
        step = {
            "source": "system" if is_intervention else "user",
            "message": _strip_author(record),
        }
        if record.get("ts"):
            step["timestamp"] = record["ts"]
        steps.append(step)

    for i, step in enumerate(steps, start=1):
        step["step_id"] = i

    totals = usage_totals(turns)
    return {
        "schema_version": "ATIF-v1.7",  # harbor 0.22.0 배포판 검증기 상한
        "session_id": session_id,
        "agent": {
            "name": agent_name,
            "version": agent_version,
            "model_name": model_name,
        },
        "steps": steps,
        "final_metrics": {
            "total_prompt_tokens": totals["input"],
            "total_completion_tokens": totals["output"],
            "total_cached_tokens": totals["cache"],
            "total_steps": len(steps),
        },
    }


def newest_session_dir(sessions_root: Path) -> Path | None:
    if not sessions_root.is_dir():
        return None
    dirs = [d for d in sessions_root.iterdir() if d.is_dir()]
    return max(dirs, key=lambda d: d.stat().st_mtime, default=None)
