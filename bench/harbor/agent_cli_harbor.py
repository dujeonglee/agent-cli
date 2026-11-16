"""Harbor(https://github.com/harbor-framework/harbor) 어댑터 — agent-cli 를
Terminal-Bench / SWE-bench 등 Harbor 데이터셋에 태운다.

제품 패키지 밖(``bench/harbor``)에 있고 **harbor + stdlib 만 import** 한다
(agent-cli 는 컨테이너 안에 wheel 로 설치되므로 호스트 harbor 프로세스는
agent-cli 를 import 하지 않는다). 사용법은 README.md.

    PYTHONPATH=bench/harbor harbor run -p <task> \\
        --agent agent_cli_harbor:AgentCli -m openai/<model>

trial 흐름 (Harbor BaseInstalledAgent 계약, harbor 0.22.0 배포판 기준):
  setup → install(): wheel 업로드 + uv 로 격리 파이썬에 설치, 서버 하나짜리
          config.json·models.json 주입(agent-cli ≥ 10.144.0 의 서버별 모양)
  run():  ``agent-cli run "$AGENT_CLI_TASK"`` 를 컨테이너 안에서 비대화형 실행
          (세션은 AGENT_CLI_SESSIONS_DIR=/logs/agent/sessions — 워크스페이스 무오염;
          종료 코드는 /logs/agent/exit-code.txt, 0·1 은 검증기로 — agent_command.py)
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

import agent_command
import atif
import wheels
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
_PATH_PREFIX = agent_command.PATH_PREFIX


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


def _host_endpoint(cfg: dict, name: str) -> tuple[str, dict]:
    """호스트 config.json(agent-cli ≥ 10.144.0 모양)에서 서버 하나 — ``(이름, 값)``.

    ``name`` 이 비면 그 파일의 선택된 서버. 옛 모양(최상위 ``endpoints`` 없음)·파일 없음은
    ``("", {})`` — agent-cli 와 같이 옛 모양은 읽지 않는다. 이름을 댔는데 없으면 ValueError
    (다른 서버로 넘어가지 않는다)."""
    endpoints = cfg.get("endpoints")
    endpoints = endpoints if isinstance(endpoints, dict) else {}
    chosen = name or (
        cfg.get("endpoint") if isinstance(cfg.get("endpoint"), str) else ""
    )
    if not chosen:
        return "", {}
    entry = endpoints.get(chosen)
    if not isinstance(entry, dict):
        raise ValueError(
            f"agent-cli server {chosen!r} is not in the host config "
            f"(saved: {', '.join(sorted(endpoints)) or 'none'})"
        )
    return chosen, entry


def _endpoint_models(models: dict, name: str) -> dict:
    """호스트 models.json 에서 서버 ``name`` 의 모델 칸 — 없거나 옛 모양이면 {}."""
    section = models.get("endpoints")
    section = section.get(name) if isinstance(section, dict) else None
    found = section.get("models") if isinstance(section, dict) else None
    return found if isinstance(found, dict) else {}


class AgentCli(BaseInstalledAgent):
    """``--ak`` kwargs: ``wheel``(기본 dist/ 최신), ``endpoint``(호스트의 agent-cli 서버
    이름, 기본 호스트 config 의 선택된 서버), ``models_json``(기본
    ``~/.agent-cli/models.json`` — 그 서버의 모델 칸을 주입해 컨테이너 안 런타임
    프로빙을 생략하고 방언 바인딩을 준다; 빈 문자열이면 미주입), ``host_config``(기본
    ``~/.agent-cli/config.json`` — 서버의 접속 정보), ``python``(uv 가 확보할 파이썬,
    기본 3.12), ``extras``(wheel 의 extras, 기본 ``image`` — Pillow 축소; ``""`` 면
    순정), 그리고 CLI_FLAGS 의 ``max_turns``·``response_format``·``max_depth``·
    ``verbose``(``--ak verbose=true`` → ``--verbose``).

    컨테이너에는 서버 하나짜리 config.json·models.json 을 올린다(이름은 호스트 서버의
    이름) — 방언 바인딩이 models.json 의 그 서버 칸에서 오므로, 서버 없는(env 만) 실행으로는
    벤치 모델이 뜨지 않는다. 접속 정보 우선순위: ``--ae AGENT_CLI_BASE_URL/API_KEY``
    (또는 호스트 env) > 호스트 서버의 값(같은 provider 일 때; loopback 은
    host.docker.internal 로 치환). 키·헤더 값은 파일(권한 600)로만 가고 env·명령줄·
    예외 문구에는 싣지 않는다."""

    SUPPORTS_ATIF = (
        True  # populate_context_post_run 이 /logs/agent/trajectory.json 생성
    )

    # ``max_turns`` 에 기본값을 두지 않는다(v10.130.1) — 안 주면 플래그가 나가지 않아
    # agent-cli 의 기본(상한 없음)으로 돈다. 제품에 없는 상한을 벤치만 걸고 있었고,
    # 끝은 과제의 시간 제한이 정한다. 종전 기록과 맞출 때는 ``--ak max_turns=60``.
    CLI_FLAGS: ClassVar[list[CliFlag]] = [
        CliFlag("max_turns", cli="--max-turns", type="int"),
        CliFlag("response_format", cli="--response-format", type="str"),
        CliFlag("max_depth", cli="--max-depth", type="int"),
        CliFlag("verbose", cli="--verbose", type="bool"),
    ]

    def __init__(
        self,
        logs_dir: Path,
        *args,
        wheel: str = "",
        endpoint: str = "",
        models_json: str = "~/.agent-cli/models.json",
        host_config: str = "~/.agent-cli/config.json",
        python: str = "3.12",
        extras: str = "image",
        **kwargs,
    ):
        super().__init__(logs_dir, *args, **kwargs)
        self._wheel = (
            Path(wheel).expanduser()
            if wheel
            else wheels.newest_wheel(_REPO_ROOT / "dist")
        )
        self._models_json = Path(models_json).expanduser() if models_json else None
        self._endpoint, self._host_cfg = _host_endpoint(
            _read_json(Path(host_config).expanduser()) if host_config else {},
            endpoint,
        )
        self._python = python
        self._extras = extras
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
                f"{shlex.quote(wheels.install_requirement(remote_wheel, self._extras))}"
                " && agent-cli --version"
            ),
            timeout_sec=330,
        )

        result = await self.exec_as_agent(environment, command='printf "%s" "$HOME"')
        home = (result.stdout or "").strip() or "/root"
        await self.exec_as_agent(environment, command=f"mkdir -p {home}/.agent-cli")
        for filename, content in self._container_files().items():
            await self._upload_config_text(
                environment,
                content=content,
                remote_path=f"{home}/.agent-cli/{filename}",
                filename=filename,
            )

    # ----- run -------------------------------------------------------------

    def _container_server(self) -> dict:
        """컨테이너에 올릴 서버 하나의 값 — 주소·키는 ``--ae``(또는 호스트 env)가 호스트 서버의
        값보다 앞선다. 호스트 서버의 값은 같은 provider 일 때만 쓴다. 주소가 없으면 ValueError
        (문구에 값은 싣지 않는다)."""
        cfg = self._host_cfg
        same = (cfg.get("provider") or "openai") == self._provider
        url = self._get_env("AGENT_CLI_BASE_URL") or (
            _container_url(str(cfg["base_url"])) if same and cfg.get("base_url") else ""
        )
        if not url:
            raise ValueError(
                "AGENT_CLI_BASE_URL required (e.g. --ae "
                "AGENT_CLI_BASE_URL=http://host.docker.internal:8000/v1), or pick an "
                "agent-cli server with a base_url (--ak endpoint=<name>)"
            )
        server = {
            "provider": self._provider,
            "base_url": url,
            "default_model": self._model,
        }
        key = self._get_env("AGENT_CLI_API_KEY") or (
            str(cfg["api_key"]) if same and cfg.get("api_key") else ""
        )
        if key:
            server["api_key"] = key
        if same and isinstance(cfg.get("extra_headers"), list):
            server["extra_headers"] = cfg["extra_headers"]
        return server

    def _container_files(self) -> dict[str, str]:
        """컨테이너 ``~/.agent-cli/`` 에 올릴 파일 ``{이름: 내용}`` — 서버 하나짜리 config.json 과
        (주입하면) 그 서버 칸만 든 models.json. 서버 이름은 호스트 서버의 이름, 없으면 ``bench``."""
        name = self._endpoint or "bench"
        files = {
            "config.json": json.dumps(
                {"endpoint": name, "endpoints": {name: self._container_server()}},
                indent=2,
            )
        }
        if self._models_json and self._models_json.is_file():
            models = _endpoint_models(_read_json(self._models_json), self._endpoint)
            files["models.json"] = json.dumps(
                {"endpoints": {name: {"models": models}}}, ensure_ascii=False, indent=2
            )
        return files

    def _runtime_env(self, instruction: str) -> dict[str, str]:
        # 접속 정보(주소·키)는 env 로 넘기지 않는다 — 컨테이너의 config.json 이 서버다
        # (agent-cli 는 서버가 골라지면 AGENT_CLI_BASE_URL·API_KEY env 를 쓰지 않는다).
        return {
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

    @with_prompt_template
    async def run(
        self, instruction: str, environment: BaseEnvironment, context: AgentContext
    ) -> None:
        # 종료 코드 0·1 은 흡수(검증기로), 2 이상은 harbor 예외 — agent_command.py
        command = agent_command.build_run_command(
            provider=self._provider,
            model=self._model,
            flags=self.build_cli_flags(),
            agent_dir=_AGENT_DIR,
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
