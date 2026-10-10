"""Harbor 0.24.0 의 내장 Hermes 에이전트를 현재 hermes-agent main 에 맞춘 얇은 서브클래스.

harbor 0.24.0 은 설치 끝과 버전 조회에 ``hermes version`` 서브커맨드를 쓰는데
hermes-agent main(2026-10) 은 ``hermes --version`` 플래그만 받아 설치 단계가
exit 2 로 끝난다. 그 두 자리만 바꾼다(``--version`` 은 지연 의존성 준비를
건너뛰므로 설치 끝에선 준비를 유발하는 서브커맨드를 먼저 한 번 부른다) —
나머지(프로바이더 라우팅·config.yaml·ATIF 변환)는 상위 클래스 그대로.

사용: ``PYTHONPATH=bench/harbor uvx --from harbor==0.24.0 harbor run …
--agent-import-path hermes_local:HermesLocal -m openai/<model>`` 에 호스트 env
``OPENAI_API_KEY``·``OPENAI_BASE_URL=http://host.docker.internal:8000/v1``.
"""

from __future__ import annotations

from typing import override

from harbor.agents.installed.hermes import Hermes
from harbor.environments.base import BaseEnvironment


class HermesLocal(Hermes):
    @override
    def get_version_command(self) -> str | None:
        return 'export PATH="$HOME/.local/bin:$PATH"; hermes --version'

    @override
    async def install(self, environment: BaseEnvironment) -> None:
        await self.ensure_system_dependencies(
            environment, ("curl", "git", "ripgrep", "xz")
        )
        branch_flag = f" --branch {self._version}" if self._version else ""
        await self.exec_as_agent(
            environment,
            command=(
                "set -euo pipefail; "
                "curl -fsSL https://raw.githubusercontent.com/NousResearch/hermes-agent/main/scripts/install.sh"
                f" | bash -s -- --skip-setup{branch_flag} && "
                'export PATH="$HOME/.local/bin:$PATH" && '
                'export HERMES_HOME="${HERMES_HOME:-/tmp/hermes}" && '
                'mkdir -p "$HERMES_HOME" "$HERMES_HOME/sessions" "$HERMES_HOME/skills" "$HERMES_HOME/memories" && '
                # --skip-setup 은 의존성 환경을 커밋하지 않고, -h/--help/-V/--version
                # 과 `pm` 은 그 지연 준비를 건너뛴다(venv_sync._METADATA_FLAGS).
                # 플래그 없는 실제 서브커맨드 하나로 준비를 유발한 뒤 버전을 찍는다.
                "hermes sessions && hermes --version"
            ),
        )
