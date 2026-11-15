"""`agent` ``mode`` "run" 결과 코퍼스 — 엔진 통합 P1(docs/agent-engine/DESIGN.md)의 바이트 등가 기준.

run 의 도구 결과(`ToolResult` 의 success·output·error·artifact)와 그 앞뒤로 모델이 받는
요청(부모·자식 루프의 system·messages), 저장된 ``result.md`` 를 가짜 provider 로 기록한다.
엔진을 바꾸기 **전에** 이 코퍼스를 ``tests/equivalence/expected/run_results.json`` 로
고정했고, `test_run_result_parity` 가 지금 엔진의 출력을 그것과 바이트로 비교한다.

입구는 엔진과 무관한 표면만 쓴다 — 부모 루프(`tests.loopkit.run_loop`)가 모델이 낸
`agent` 호출을 실제로 디스패치하는 길, 루프 없이 거부 판정에 닿는 `ToolBridge._invoke_agent`,
``@agent`` 명령의 `main._dispatch_agent`. 무작위·시각 부분(런 디렉터리의 hex·시각,
Duration 숫자, 임시 경로)만 정규화한다.

다시 만들기(기준 엔진에서만)::

    AGENT_CLI_WRITE_RUN_CORPUS=1 pytest tests/equivalence/test_run_result_parity.py
"""

from __future__ import annotations

import json
import re
import threading
from pathlib import Path

from agent_cli.providers.base import LLMResponse
from agent_cli.providers.capabilities import ModelCapabilities

EXPECTED = Path(__file__).parent / "expected" / "run_results.json"

#: 작은 창 — 큰 결과 사례가 관찰 상한(창/10)을 넘게
CAPS = ModelCapabilities(
    context_window=16384, max_output_tokens=2048, supports_thinking=False
)
#: 보통 사례의 창
BIG_CAPS = ModelCapabilities(
    context_window=131072, max_output_tokens=2048, supports_thinking=False
)


def complete(result: str) -> str:
    return json.dumps({"action": "complete", "result": result})


def call(action: str, **args) -> str:
    return json.dumps({"action": action, **args})


def ops(*items: dict) -> str:
    """한 턴의 호출 여럿 — json_fc 의 배열 모양."""
    return json.dumps(list(items))


class ScriptedProvider:
    """호출마다 대화의 **첫 사용자 메시지**로 누가 부르는지(부모·자식) 가린다 — 병렬 자식은
    순서가 없으므로 배우별 대본과 배우별 기록을 둔다. 대본 항목이 콜러블이면 부른 결과가 답."""

    def __init__(self, scripts: dict[str, list]):
        self._scripts = {k: list(v) for k, v in scripts.items()}
        self._last: dict[str, object] = {}
        self.calls: dict[str, list[dict]] = {}
        self._lock = threading.Lock()

    @staticmethod
    def actor_of(messages) -> str:
        # 과제 메시지는 표식으로 **시작**한다(관찰 속 인용은 아니다) — fork 한 자식은 부모의
        # 대화를 앞에 달고 오므로 마지막 것이 그 루프의 과제다.
        actor = "?"
        for m in messages:
            if m.get("role") != "user":
                continue
            content = m.get("content")
            if isinstance(content, list):
                content = " ".join(
                    p.get("text", "") for p in content if isinstance(p, dict)
                )
            found = re.match(r"\s*((PARENT|CHILD)-[A-Za-z0-9]+)", str(content or ""))
            if found:
                actor = found.group(1)
        return actor

    def call(self, messages, system, model, capabilities, **kwargs):
        actor = self.actor_of(messages)
        with self._lock:
            self.calls.setdefault(actor, []).append(
                {"system": system, "messages": json.loads(json.dumps(messages))}
            )
            script = self._scripts.get(actor, [])
            step = script.pop(0) if script else self._last.get(actor)
            self._last[actor] = step
        if step is None:
            step = complete(f"(no script for {actor})")
        if callable(step):
            step = step()
        return LLMResponse(content=step)


# ── 정규화 ──────────────────────────────────────────────────────

_RUN_DIR = re.compile(r"(run_[A-Za-z0-9_.-]+?)_[0-9a-f]{6}_\d{8}T\d{9}")
_DURATION = re.compile(r"\[Duration: \d+\.\ds\]")
_ISO = re.compile(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(:\d{2}(\.\d+)?)?")
_DIGEST = re.compile(r"(oversized/[A-Za-z0-9_.-]*?)[0-9a-f]{8,}")
#: `## Environment` 의 OS 줄 — 코퍼스를 만든 기계(macOS)와 CI(Linux)가 다르다
_PLATFORM = re.compile(r"^- Platform: .*$", re.MULTILINE)


#: 저장소 뿌리 — 프로파일 탐색 경로가 거부 문구에 실린다(검증하는 작업 트리마다 다르다)
_REPO = str(Path(__file__).resolve().parents[2])


def normalize(text, root: str):
    if not isinstance(text, str):
        return text
    text = text.replace(root, "<TMP>").replace(_REPO, "<REPO>")
    text = _RUN_DIR.sub(r"\1_<hex>_<ts>", text)
    text = _DURATION.sub("[Duration: <s>]", text)
    text = _ISO.sub("<time>", text)
    text = _DIGEST.sub(r"\1<digest>", text)
    text = _PLATFORM.sub("- Platform: <platform>", text)
    return text


def normalize_tree(obj, root: str):
    if isinstance(obj, dict):
        return {normalize(k, root): normalize_tree(v, root) for k, v in obj.items()}
    if isinstance(obj, list):
        return [normalize_tree(v, root) for v in obj]
    return normalize(obj, root)


def tool_result_view(result) -> dict:
    return {
        "success": bool(result.success),
        "output": result.output,
        "error": result.error,
        "artifact": getattr(result, "artifact", "") or "",
    }


def result_files(session_dir: Path) -> list[str]:
    """``run_*/result.md`` 본문들 — 병렬은 순서가 없으므로 정렬한다(이름은 무작위라 뺀다)."""
    return sorted(
        p.read_text(encoding="utf-8") for p in session_dir.glob("run_*/result.md")
    )
