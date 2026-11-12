"""컨테이너 안에서 돌릴 ``agent-cli run`` 명령 한 줄 — harbor 없이 import 되는 stdlib 전용.

종료 코드 (agent-cli v10.124.0 부터 0 답 · 1 실패/무답 · 2 설정/사용법 · 130 중단):
harbor 0.22.0 의 ``BaseInstalledAgent._exec`` 는 ``set -o pipefail; <명령>`` 으로 돌리고
0 이 아니면 예외를 던진다 — 그러면 ``Trial`` 이 검증을 건너뛴다. 종전 ``run`` 은 늘 0 이라
max_turns·LLM 실패 런도 검증기로 갔고, 벤치 점수가 그 전제 위에 있다. 그 의미를 지키려고
**0·1 (런이 끝났다 — 답이 있든 없든)은 흡수**해 검증기로 보내고, 2 이상(설정/사용법 오류,
127 명령 없음, 130 중단, 신호)만 종전처럼 harbor 예외로 둔다. 코드는 ``exit-code.txt`` 에
남겨 사후 분석이 로그를 다시 해석하지 않게 한다. 셸은 harbor docker 의 main 서비스가
``bash -c`` 다(``{ …; }``·``$?``·pipefail 조합은 bash 기준). ``test_agent_command.py`` 가
실제 ``bash -c`` 로 확인한다.
"""

from __future__ import annotations

import shlex

PATH_PREFIX = 'export PATH="$HOME/.local/bin:$PATH"; '
#: 이 코드 이하는 "런이 끝났다" — 검증기로 간다
ABSORBED_MAX = 1


def build_run_command(*, provider: str, model: str, flags: str, agent_dir: str) -> str:
    run = (
        'agent-cli run "$AGENT_CLI_TASK" '
        f"-p {provider} -m {shlex.quote(model)} "
        f"{flags + ' ' if flags else ''}"
        f"--result-file {agent_dir}/result.txt </dev/null"
    )
    return (
        PATH_PREFIX + f"{{ {run}; rc=$?; "
        f'echo "$rc" > {agent_dir}/exit-code.txt; '
        f'if [ "$rc" -gt {ABSORBED_MAX} ]; then exit "$rc"; fi; }} '
        f"2>&1 | tee {agent_dir}/agent-cli.txt"
    )
