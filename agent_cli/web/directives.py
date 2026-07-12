"""Directive 스코프 에디터 도메인 로직 — ✨ 생성 (5.4.0 개편, 5.6.0 프로세스 분리).

구 3축(성격/업무/지침) zone 외과수술·프리셋 라이브러리는 폐지됐다 —
에디터의 구조 = 파일의 구조(U-C 청중 스코프: 공통/``## @main``/``## @agents``)
하나뿐이고, 분해/조립은 :mod:`agent_cli.prompts.system_prompt` 의
``split_directive_scopes``/``join_directive_scopes`` 가 단일 출처다.

이 모듈이 소유하는 것은 ✨ 생성 하나: 사용자의 대략적 의도(brief)를
**별도 ``agent-cli run`` 서브프로세스**(5.6.0 — 사용자 결정 "완전 분리")
로 directive 초안으로 만든다:

- ``@directive-writer``(내장 프로파일 — 도구 0, 작성 규율) 디스패치 +
  ``--result-file`` 로 원문 수확. 렌더러/세션/레지스트리가 메인 프로세스와
  완전히 분리 — 메인 타임라인에 카드가 생기지 않고, 탭별 동시 생성 가능.
- 자격(base_url/api_key)은 argv 가 아니라 **env 로** 전달 (ps 누출 방지).
- 구 🪄 자동생성의 CoT-leak(산문 메타-콜)와 무관 — run 은 JSON wire
  경로라 CoT 가 격리된다.

FastAPI import 0 (전송은 server.py) — 테스트 가드 유지.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

VALID_AUDIENCES = ("common", "main", "agents")

# 청중별 프레이밍 — 생성 태스크에 삽입되는 요약. DIRECTIVE 스코프 의미론
# (U-C, docs/agent-unification/DESIGN.md §3.7)과 일치해야 한다.
_AUDIENCE_FRAMING = {
    "common": (
        "AUDIENCE: every LLM in the session — the main conversation loop AND "
        "all subagents. Write rules that hold everywhere (coding conventions, "
        "verification discipline, project constraints)."
    ),
    "main": (
        "AUDIENCE: the MAIN conversation LLM only (subagents never see this). "
        "Good fits: user-facing reporting style/voice/language, when to ask "
        "vs. proceed, how to summarize results for the user."
    ),
    "agents": (
        "AUDIENCE: subagents only (one-shot runs and persistent agents — the "
        "main LLM never sees this). Good fits: result format returned to the "
        "caller, scope discipline, citation/verification requirements."
    ),
}


def build_generation_task(audience: str, brief: str, current: str) -> str:
    """✨ 생성 서브프로세스(@directive-writer)에 넘길 task 텍스트."""
    parts = [
        _AUDIENCE_FRAMING[audience],
        f"USER INTENT (rough — turn this into directive rules):\n{brief.strip()}",
    ]
    current = (current or "").strip()
    if current:
        parts.append(
            "EXISTING directive content for this audience (revise/merge — "
            f"your output replaces it):\n{current}"
        )
    return "\n\n".join(parts)


def generate_directive_section(
    audience: str, brief: str, current: str, *, runtime: dict
) -> str:
    """brief → directive 초안 (활성 스코프용, 미저장 반환).

    별도 ``agent-cli run`` 프로세스 1회 — 완전 격리(메인 워커/렌더러/
    세션 무접촉), POST 마다 자기 프로세스라 동시 생성이 자연 지원.
    ``runtime`` 은 문자열 배선만: model/provider_name/base_url/api_key/
    timeout. 입력 오류=ValueError(→400) / 실행 실패=RuntimeError(→502).
    """
    if audience not in VALID_AUDIENCES:
        raise ValueError(f"unknown audience: {audience}")
    if not brief.strip():
        raise ValueError("brief 가 비어 있습니다")
    if not runtime or not runtime.get("model"):
        raise ValueError("LLM 이 배선되지 않았습니다")

    task = build_generation_task(audience, brief, current)
    timeout = int(runtime.get("timeout", 120))

    # 자격은 env 로 (argv 는 ps 에 노출). 서브프로세스는 부모와 같은
    # 인터프리터의 agent_cli 를 실행 — PATH 의 다른 설치본에 안 흔들린다.
    env = dict(os.environ)
    for env_key, rt_key in (
        ("AGENT_CLI_PROVIDER", "provider_name"),
        ("AGENT_CLI_BASE_URL", "base_url"),
        ("AGENT_CLI_API_KEY", "api_key"),
    ):
        val = runtime.get(rt_key)
        if val:
            env[env_key] = str(val)

    with tempfile.TemporaryDirectory(prefix="agentcli-dirgen-") as workdir:
        result_path = Path(workdir) / "result.md"
        cmd = [
            sys.executable,
            "-m",
            "agent_cli",
            "run",
            f"@directive-writer {task}",
            "--model",
            str(runtime["model"]),
            "--max-turns",
            "4",
            "--result-file",
            str(result_path),
        ]
        try:
            proc = subprocess.run(
                cmd,
                cwd=workdir,  # 세션/스크래치가 임시 디렉토리에 격리
                env=env,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired as e:
            raise RuntimeError(f"생성 시간 초과 ({timeout}s)") from e
        if not result_path.is_file():
            tail = (proc.stderr or proc.stdout or "").strip()[-400:]
            raise RuntimeError(
                f"생성 실패 (exit {proc.returncode}): {tail or '(no output)'}"
            )
        # @ 경로의 result-file 은 run 관찰 포맷(STATUS/RESULT/[activity])
        # 그대로다 — 포맷터의 역(extract_result_body)으로 원문만 수확.
        from agent_cli.subagent.report import extract_result_body

        body = extract_result_body(result_path.read_text(encoding="utf-8")).strip()
    if not body:
        raise RuntimeError("생성 결과가 비어 있습니다")
    return body
