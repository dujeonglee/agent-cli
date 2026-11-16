"""`agent_cli_harbor.AgentCli` — 플래그 표(제품에 없는 조건을 몰래 걸지 않는다)와 컨테이너에
올리는 서버 하나짜리 설정(agent-cli ≥ 10.144.0 모양, 키는 파일로만).

harbor 는 벤치 전용 의존성이라(uvx 격리) 없으면 건너뛴다.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

pytest.importorskip("harbor.agents.installed.base")

sys.path.insert(0, str(Path(__file__).parent))

import agent_cli_harbor


def _agent(tmp_path: Path, **kwargs):
    wheel = tmp_path / "agent_cli-0.0.0-py3-none-any.whl"
    wheel.write_bytes(b"")
    kwargs.setdefault("models_json", "")
    kwargs.setdefault("host_config", str(tmp_path / "no-config.json"))
    kwargs.setdefault("model_name", "openai/m")
    return agent_cli_harbor.AgentCli(logs_dir=tmp_path, wheel=str(wheel), **kwargs)


_KEY = "SENTINEL-harbor-key-7731"
_HDR = "SENTINEL-harbor-header-9902"


def _host(tmp_path: Path, config: dict, models: dict | None = None) -> dict:
    cfg = tmp_path / "host-config.json"
    cfg.write_text(json.dumps(config), encoding="utf-8")
    out = {"host_config": str(cfg)}
    if models is not None:
        mdl = tmp_path / "host-models.json"
        mdl.write_text(json.dumps(models), encoding="utf-8")
        out["models_json"] = str(mdl)
    return out


_HOST = {
    "endpoint": "home",
    "endpoints": {
        "home": {
            "provider": "openai",
            "base_url": "http://127.0.0.1:8000/v1",
            "api_key": _KEY,
            "default_model": "m",
            "extra_headers": [{"name": "X-Team", "value": _HDR}],
        },
        "work": {"provider": "openai", "base_url": "https://gw.example.com/v1"},
    },
}
_MODELS = {
    "endpoints": {
        "home": {"models": {"m": {"dialect": "xml_fc"}}},
        "work": {"models": {"w": {"dialect": "json_fc"}}},
    }
}


def test_no_turn_cap_unless_asked(tmp_path):
    """기본으로는 ``--max-turns`` 가 나가지 않는다 — agent-cli 의 기본(상한 없음)으로 돈다."""
    assert "--max-turns" not in _agent(tmp_path).build_cli_flags()


def test_turn_cap_is_passed_when_given(tmp_path):
    assert "--max-turns 60" in _agent(tmp_path, max_turns=60).build_cli_flags()


def test_container_gets_the_selected_server_only(tmp_path):
    agent = _agent(tmp_path, **_host(tmp_path, _HOST, _MODELS))
    files = agent._container_files()
    cfg = json.loads(files["config.json"])
    assert cfg["endpoint"] == "home" and list(cfg["endpoints"]) == ["home"]
    server = cfg["endpoints"]["home"]
    assert server["base_url"] == "http://host.docker.internal:8000/v1"
    assert server["api_key"] == _KEY
    assert server["extra_headers"] == [{"name": "X-Team", "value": _HDR}]
    assert json.loads(files["models.json"]) == {
        "endpoints": {"home": {"models": {"m": {"dialect": "xml_fc"}}}}
    }


def test_named_server_and_ae_overrides(tmp_path):
    agent = _agent(
        tmp_path,
        endpoint="work",
        extra_env={
            "AGENT_CLI_BASE_URL": "http://other:1/v1",
            "AGENT_CLI_API_KEY": "k2",
        },
        **_host(tmp_path, _HOST, _MODELS),
    )
    files = agent._container_files()
    server = json.loads(files["config.json"])["endpoints"]["work"]
    assert server["base_url"] == "http://other:1/v1" and server["api_key"] == "k2"
    assert "w" in json.loads(files["models.json"])["endpoints"]["work"]["models"]


def test_unknown_server_is_an_error_without_values(tmp_path):
    import pytest as _pytest

    with _pytest.raises(ValueError) as ei:
        _agent(tmp_path, endpoint="lab", **_host(tmp_path, _HOST))
    assert "'lab'" in str(ei.value) and _KEY not in str(ei.value)


def test_legacy_host_config_is_not_read(tmp_path):
    """옛 모양 호스트 설정은 접속 정보 폴백이 되지 않는다 — agent-cli 와 같은 규칙."""
    import pytest as _pytest

    legacy = {
        "provider": "openai",
        "base_url": "http://127.0.0.1:8000/v1",
        "api_key": _KEY,
    }
    agent = _agent(tmp_path, **_host(tmp_path, legacy))
    with _pytest.raises(ValueError) as ei:
        agent._container_files()
    assert "AGENT_CLI_BASE_URL required" in str(ei.value)
    assert _KEY not in str(ei.value)


def test_key_is_not_in_env_or_command(tmp_path):
    agent = _agent(tmp_path, **_host(tmp_path, _HOST, _MODELS))
    env = agent._runtime_env("do the task")
    assert _KEY not in json.dumps(env) and _HDR not in json.dumps(env)
    assert "AGENT_CLI_API_KEY" not in env and "AGENT_CLI_BASE_URL" not in env
    import agent_command

    command = agent_command.build_run_command(
        provider=agent._provider,
        model=agent._model,
        flags=agent.build_cli_flags(),
        agent_dir="/logs/agent",
    )
    assert _KEY not in command and _HDR not in command


def test_install_uploads_files_without_logging_the_key(tmp_path, caplog):
    """install() 이 올리는 것은 파일 둘(권한 600 — harbor `_upload_config_text`) 뿐이고, 실행한
    명령과 로그에 키·헤더 값이 없다."""
    import asyncio
    import logging

    agent = _agent(tmp_path, **_host(tmp_path, _HOST, _MODELS))
    commands, uploads = [], {}

    class _Result:
        stdout = "/root"

    async def _exec(env, command, **kw):
        commands.append(command)
        return _Result()

    async def _upload(env, *, content, remote_path, filename):
        uploads[remote_path] = content

    async def _nothing(*a, **k):
        return _Result()

    agent.exec_as_agent = _exec
    agent.exec_as_root = _exec
    agent.ensure_system_dependencies = _nothing
    agent._upload_config_text = _upload

    class _Env:
        async def upload_file(self, *a, **k):
            return None

    with caplog.at_level(logging.DEBUG):
        asyncio.run(agent.install(_Env()))
    assert sorted(uploads) == [
        "/root/.agent-cli/config.json",
        "/root/.agent-cli/models.json",
    ]
    blob = "\n".join(commands) + caplog.text
    assert _KEY not in blob and _HDR not in blob
