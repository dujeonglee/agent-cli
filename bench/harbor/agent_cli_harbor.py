"""Harbor(https://github.com/harbor-framework/harbor) 어댑터 — agent-cli 를
Terminal-Bench / SWE-bench 등 Harbor 데이터셋에 태운다.

제품 패키지 밖(``bench/harbor``)에 있고 **harbor + stdlib 만 import** 한다
(agent-cli 는 컨테이너 안에 wheel 로 설치되므로 호스트 harbor 프로세스는
agent-cli 를 import 하지 않는다). 사용법은 README.md.

    PYTHONPATH=bench/harbor harbor run -p <task> \\
        --agent agent_cli_harbor:AgentCli -m openai/<model>

trial 흐름 (Harbor BaseInstalledAgent 계약, harbor 0.22.0 배포판 기준):
  setup → install(): wheel 업로드 + uv 로 격리 파이썬에 설치, models.json 주입
  run():  ``agent-cli run "$AGENT_CLI_TASK"`` 를 컨테이너 안에서 비대화형 실행
          (세션은 AGENT_CLI_SESSIONS_DIR=/logs/agent/sessions — 워크스페이스 무오염)
  populate_context_post_run(): 호스트로 동기화된 세션에서 토큰 합계·헬스 집계,
          ATIF trajectory.json 생성
"""

from __future__ import annotations

import json
import logging
import re
import shlex
from pathlib import Path
from typing import ClassVar

import atif
from harbor.agents.installed.base import (
    BaseInstalledAgent,
    CliFlag,
    with_prompt_template,
)
from harbor.environments.base import BaseEnvironment
from harbor.models.agent.context import AgentContext
from harbor.models.trial.paths import EnvironmentPaths

_REPO_ROOT = Path(__file__).resolve().parents[2]
_AGENT_DIR = str(EnvironmentPaths.agent_dir)  # /logs/agent
_INSTALL_DIR = "/installed-agent"  # setup() 이 root 로 만들어 둔다
_PATH_PREFIX = 'export PATH="$HOME/.local/bin:$PATH"; '


def _read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _container_url(url: str) -> str:
    """호스트 loopback → 컨테이너에서 보이는 호스트 주소 (Docker Desktop /
    최신 Docker Engine 의 host.docker.internal)."""
    return re.sub(
        r"//(127\.0\.0\.1|localhost)(?=[:/]|$)", "//host.docker.internal", url
    )


def _newest_wheel() -> Path:
    wheels = sorted((_REPO_ROOT / "dist").glob("agent_cli-*.whl"))
    if not wheels:
        raise FileNotFoundError(
            f"no agent-cli wheel under {_REPO_ROOT / 'dist'} — run "
            "`python3 -m build --wheel` first, or pass --ak wheel=<path>"
        )
    return wheels[-1]


class AgentCli(BaseInstalledAgent):
    """``--ak`` kwargs: ``wheel``(기본 dist/ 최신), ``models_json``(기본
    ``~/.agent-cli/models.json`` — 컨테이너 안 런타임 프로빙 생략용; 빈 문자열
    이면 미주입), ``host_config``(기본 ``~/.agent-cli/config.json`` — 접속
    정보 폴백), ``python``(uv 가 확보할 파이썬, 기본 3.12), 그리고 CLI_FLAGS 의
    ``max_turns``·``response_format``·``max_depth``.

    접속 정보 우선순위: ``--ae AGENT_CLI_BASE_URL/API_KEY`` (또는 호스트 env)
    > 호스트 config.json(같은 provider 일 때; loopback 은 host.docker.internal
    로 치환)."""

    SUPPORTS_ATIF = (
        True  # populate_context_post_run 이 /logs/agent/trajectory.json 생성
    )

    CLI_FLAGS: ClassVar[list[CliFlag]] = [
        CliFlag("max_turns", cli="--max-turns", type="int", default=60),
        CliFlag("response_format", cli="--response-format", type="str"),
        CliFlag("max_depth", cli="--max-depth", type="int"),
    ]

    def __init__(
        self,
        logs_dir: Path,
        *args,
        wheel: str = "",
        models_json: str = "~/.agent-cli/models.json",
        host_config: str = "~/.agent-cli/config.json",
        python: str = "3.12",
        **kwargs,
    ):
        super().__init__(logs_dir, *args, **kwargs)
        self._wheel = Path(wheel).expanduser() if wheel else _newest_wheel()
        self._models_json = Path(models_json).expanduser() if models_json else None
        self._host_cfg = (
            _read_json(Path(host_config).expanduser()) if host_config else {}
        )
        self._python = python
        self._provider, self._model = self._split_model()

    # ----- identity -----------------------------------------------------

    @staticmethod
    def name() -> str:
        return "agent-cli"

    def get_version_command(self) -> str | None:
        return _PATH_PREFIX + "agent-cli --version"

    def parse_version(self, stdout: str) -> str:
        text = stdout.strip().splitlines()
        return (text[-1] if text else "").removeprefix("agent-cli").strip()

    def _split_model(self) -> tuple[str, str]:
        """``-m provider/model`` (Harbor 관례) → agent-cli 의 ``-p``/``-m``.
        ``-m`` 이 없으면 ``--ae AGENT_CLI_PROVIDER/AGENT_CLI_MODEL`` → 호스트
        config.json 순으로 폴백."""
        cfg = self._host_cfg
        if self.model_name:
            if "/" in self.model_name:
                provider, model = self.model_name.split("/", 1)
            else:
                provider, model = "openai", self.model_name
        else:
            provider = (
                self._get_env("AGENT_CLI_PROVIDER") or cfg.get("provider") or "openai"
            )
            model = self._get_env("AGENT_CLI_MODEL") or cfg.get("default_model") or ""
        if provider not in {"openai", "anthropic"}:
            raise ValueError(
                f"agent-cli provider must be openai|anthropic, got {provider!r} "
                "(use -m openai/<model> for any OpenAI-compatible server)"
            )
        if not model:
            raise ValueError(
                "model required: -m openai/<model> or --ae AGENT_CLI_MODEL=<model>"
            )
        return provider, model

    # ----- install -------------------------------------------------------

    async def install(self, environment: BaseEnvironment) -> None:
        await self.ensure_system_dependencies(
            environment, ("curl", "ca_certificates", "git")
        )
        remote_wheel = f"{_INSTALL_DIR}/{self._wheel.name}"
        await environment.upload_file(self._wheel, remote_wheel)
        # 에이전트 사용자(root 가 아닐 수 있음)가 읽을 수 있게
        await self.exec_as_root(environment, command=f"chmod 644 {remote_wheel}")

        await self.exec_as_agent(
            environment,
            command=(
                "set -euo pipefail; "
                + _PATH_PREFIX
                + "command -v uv >/dev/null 2>&1 || "
                "curl -LsSf https://astral.sh/uv/install.sh | sh; "
                + _PATH_PREFIX
                + f"uv tool install --python {shlex.quote(self._python)} --force "
                f"{shlex.quote(remote_wheel)} && agent-cli --version"
            ),
            timeout_sec=330,
        )

        if self._models_json and self._models_json.is_file():
            result = await self.exec_as_agent(
                environment, command='printf "%s" "$HOME"'
            )
            home = (result.stdout or "").strip() or "/root"
            await self.exec_as_agent(environment, command=f"mkdir -p {home}/.agent-cli")
            await self._upload_config_text(
                environment,
                content=self._models_json.read_text(encoding="utf-8"),
                remote_path=f"{home}/.agent-cli/models.json",
                filename="models.json",
            )

    # ----- run -------------------------------------------------------------

    def _runtime_env(self, instruction: str) -> dict[str, str]:
        env = {
            "AGENT_CLI_PROVIDER": self._provider,
            "AGENT_CLI_MODEL": self._model,
            # 세션을 워크스페이스 밖(/logs/agent — 호스트로 동기화)으로 (v8.50.0)
            "AGENT_CLI_SESSIONS_DIR": f"{_AGENT_DIR}/sessions",
            # 비대화형: 확인 프롬프트를 띄울 수 없으면 rm/mv 가 거부되므로 해제
            "AGENT_CLI_DANGEROUS_SHELL_CONFIRM": "0",
            # 태스크가 /tmp·/etc 등 워크스페이스 밖을 만질 수 있음
            "AGENT_CLI_WORKSPACE_CONFINE": "0",
            "NO_COLOR": "1",
            "TERM": "dumb",
            # 지시문은 env 로 (멀티라인·따옴표 안전; kimi_code 어댑터와 동형)
            "AGENT_CLI_TASK": instruction,
        }
        for key in ("AGENT_CLI_BASE_URL", "AGENT_CLI_API_KEY"):
            val = self._get_env(key)
            if val:
                env[key] = val
        # 폴백: 호스트 config.json (같은 provider 일 때만; bench/swebench 컨테이너
        # 스크립트의 host_provider_config 와 동형)
        cfg = self._host_cfg
        if cfg.get("provider", "openai") == self._provider:
            if not env.get("AGENT_CLI_BASE_URL") and cfg.get("base_url"):
                env["AGENT_CLI_BASE_URL"] = _container_url(str(cfg["base_url"]))
            if not env.get("AGENT_CLI_API_KEY") and cfg.get("api_key"):
                env["AGENT_CLI_API_KEY"] = str(cfg["api_key"])
        if not env.get("AGENT_CLI_BASE_URL"):
            raise ValueError(
                "AGENT_CLI_BASE_URL required (e.g. --ae "
                "AGENT_CLI_BASE_URL=http://host.docker.internal:8000/v1)"
            )
        return env

    @with_prompt_template
    async def run(
        self, instruction: str, environment: BaseEnvironment, context: AgentContext
    ) -> None:
        flags = self.build_cli_flags()
        command = (
            _PATH_PREFIX + 'agent-cli run "$AGENT_CLI_TASK" '
            f"-p {self._provider} -m {shlex.quote(self._model)} "
            f"{flags + ' ' if flags else ''}"
            f"--result-file {_AGENT_DIR}/result.txt "
            f"</dev/null 2>&1 | tee {_AGENT_DIR}/agent-cli.txt"
        )
        await self.exec_as_agent(
            environment, command=command, env=self._runtime_env(instruction)
        )

    # ----- post-run (host) --------------------------------------------------

    def populate_context_post_run(self, context: AgentContext) -> None:
        session = atif.newest_session_dir(self.logs_dir / "sessions")
        if session is None:
            self.logger.warning("agent-cli: no session dir under %s", self.logs_dir)
            return
        turns = atif.read_jsonl(session / "turns.jsonl")
        totals = atif.usage_totals(turns)
        context.n_input_tokens = totals["input"]
        context.n_cache_tokens = totals["cache"]
        context.n_output_tokens = totals["output"]
        context.cost_usd = None  # on-prem — 단가 없음
        meta = atif.health(turns)
        meta["session_id"] = session.name
        meta["result_file"] = (self.logs_dir / "result.txt").is_file()
        context.metadata = meta

        try:
            trajectory = atif.build_trajectory(
                atif.read_jsonl(session / "history.jsonl"),
                turns,
                agent_name=self.name(),
                agent_version=self.version() or "unknown",
                model_name=self._model,
                session_id=session.name,
            )
            (self.logs_dir / "trajectory.json").write_text(
                json.dumps(trajectory, ensure_ascii=False, indent=1), encoding="utf-8"
            )
        except Exception as exc:  # 궤적 변환 실패가 trial 을 깨면 안 됨
            logging.getLogger(__name__).warning("ATIF conversion failed: %s", exc)
