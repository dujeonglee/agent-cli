"""서버를 고르는 부트 (v10.144.0) — `--endpoint` > ``AGENT_CLI_ENDPOINT`` > 세션 기록 > config 기본,
없는 이름은 오류(rc 2), 세션에 이름 기록, 서버가 없으면 env 가 이름 없는 서버.

실제 ``run``·``web`` 을 CliRunner 로 태운다 — 모델 서버는 닿지 않는 주소(``127.0.0.1:1``)와
가짜 ``/models`` 거절이고, LLM 호출은 가짜 provider 다(네트워크 없음).
"""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest
import requests
import typer
from rich.console import Console
from typer.testing import CliRunner

import agent_cli.config as _config
import agent_cli.dialects as _dialects
import agent_cli.main as m
import agent_cli.session.store as session_mod
from agent_cli import model_check
from agent_cli.providers.base import LLMResponse
from tests.endpoint_files import point_config

URL_HOME = "http://127.0.0.1:1/v1"
URL_WORK = "http://127.0.0.1:2/v1"
_KEY = "SENTINEL-endpoint-key-5521"
_SERVERS = {
    "home": {"provider": "openai", "base_url": URL_HOME, "default_model": "m-home"},
    "work": {
        "provider": "openai",
        "base_url": URL_WORK,
        "api_key": _KEY,
        "default_model": "m-work",
    },
}
_MODELS = {
    "home": {"m-home": {"context_window": 32768, "dialect": "json_fc"}},
    "work": {"m-work": {"context_window": 32768, "dialect": "json_fc"}},
}


@pytest.fixture
def servers(tmp_path, monkeypatch):
    for key in (
        "AGENT_CLI_ENDPOINT",
        "AGENT_CLI_PROVIDER",
        "AGENT_CLI_BASE_URL",
        "AGENT_CLI_API_KEY",
        "AGENT_CLI_MODEL",
        "OPENAI_API_KEY",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("AGENT_CLI_NO_INPUT", "1")
    point_config(
        monkeypatch,
        tmp_path / "home",
        endpoints=_SERVERS,
        default="home",
        models=_MODELS,
        package=False,
    )
    monkeypatch.setattr(_dialects, "_override", None)
    monkeypatch.chdir(tmp_path)
    model_check._cache.clear()

    def _refused(*a, **k):
        raise requests.exceptions.ConnectionError("refused")

    monkeypatch.setattr(requests, "get", _refused)
    yield tmp_path
    model_check._cache.clear()


@pytest.fixture
def out(monkeypatch):
    con = Console(width=200, record=True, color_system=None)
    monkeypatch.setattr(m, "console", con)
    return con


class _Provider:
    """늘 답하는 가짜 provider — 어느 주소로 만들어졌는지 남긴다."""

    made: list = []  # noqa: RUF012 — 테스트마다 `_run` 이 새 목록으로 바꾼다

    def __init__(self, name, url, credentials):
        _Provider.made.append((name, url, credentials))

    def call(self, messages, system, model, capabilities, **kwargs):
        return LLMResponse(
            content=json.dumps(
                {"action": "complete", "result": "ok", "answers": ["u-1"]}
            )
        )


def _run(*args):
    _Provider.made = []
    with (
        patch("agent_cli.main.create_provider", _Provider),
        patch("agent_cli.main._setup_mcp", return_value=(None, {})),
    ):
        return CliRunner().invoke(m.app, ["run", *args])


def _sessions(root):
    d = root / session_mod._SESSIONS_DIR
    return sorted(p.name for p in d.iterdir()) if d.is_dir() else []


def _meta(root, sid):
    line = (root / session_mod._SESSIONS_DIR / sid / "session.jsonl").read_text()
    return json.loads(line.splitlines()[0])["_meta"]


# ── 고르기 (`_select_endpoint`) ─────────────────────────────────


class TestSelectEndpoint:
    def test_default_server(self, servers):
        name, values, source = m._select_endpoint(None, None)
        assert name == "home" and values["base_url"] == URL_HOME
        assert _config.current_endpoint() == "home"
        assert "server 'home'" in source

    @pytest.mark.parametrize(
        ("flag", "env", "recorded", "expected"),
        [
            ("work", "home", "home", "work"),
            (None, "work", "home", "work"),
            (None, None, "work", "work"),
        ],
    )
    def test_order(self, servers, monkeypatch, flag, env, recorded, expected):
        if env:
            monkeypatch.setenv("AGENT_CLI_ENDPOINT", env)
        assert m._select_endpoint(flag, recorded)[0] == expected

    @pytest.mark.parametrize("where", ["flag", "env", "recorded"])
    def test_unknown_name_exits_2_without_fallback(
        self, servers, monkeypatch, out, where
    ):
        kwargs = {"flag": None, "recorded": None}
        if where == "env":
            monkeypatch.setenv("AGENT_CLI_ENDPOINT", "gone")
        else:
            kwargs[where] = "gone"
        with pytest.raises(typer.Exit) as ei:
            m._select_endpoint(**kwargs)
        assert ei.value.exit_code == 2
        text = out.export_text()
        assert "Unknown endpoint 'gone'" in text and "home, work" in text
        assert _config.current_endpoint() is None  # 아무 서버도 세우지 않았다

    def test_env_fields_are_ignored_once_a_server_is_chosen(self, servers, monkeypatch):
        monkeypatch.setenv("AGENT_CLI_BASE_URL", "http://elsewhere:9/v1")
        monkeypatch.setenv("AGENT_CLI_API_KEY", "env-key")
        _name, values, _ = m._select_endpoint(None, None)
        assert values["base_url"] == URL_HOME and "api_key" not in values

    def test_no_servers_means_the_env_is_an_unnamed_server(self, tmp_path, monkeypatch):
        point_config(monkeypatch, tmp_path)
        monkeypatch.delenv("AGENT_CLI_ENDPOINT", raising=False)
        monkeypatch.setenv("AGENT_CLI_BASE_URL", URL_HOME)
        monkeypatch.delenv("AGENT_CLI_PROVIDER", raising=False)
        monkeypatch.delenv("AGENT_CLI_API_KEY", raising=False)
        name, values, source = m._select_endpoint(None, None)
        assert name is None and values == {"base_url": URL_HOME}
        assert "environment" in source

    def test_unreadable_config_exits_2(self, tmp_path, monkeypatch, out):
        cfg, _ = point_config(monkeypatch, tmp_path)
        cfg.write_text("{oops", encoding="utf-8")
        with pytest.raises(typer.Exit) as ei:
            m._select_endpoint(None, None)
        assert ei.value.exit_code == 2
        assert "Could not read config" in out.export_text()


class TestApplyEndpoint:
    def _meta(self, endpoint=""):
        return session_mod.SessionMeta("1", "/ws", "", endpoint=endpoint)

    def test_new_session_records_quietly(self, out):
        s = self._meta()
        m._apply_endpoint(s, "home")
        assert s.endpoint == "home" and out.export_text() == ""

    def test_same_name_is_quiet(self, out):
        s = self._meta("home")
        m._apply_endpoint(s, "home")
        assert out.export_text() == ""

    def test_change_is_one_line(self, out):
        s = self._meta("home")
        m._apply_endpoint(s, "work")
        assert s.endpoint == "work"
        assert out.export_text().strip() == "Session server changed: home → work"

    def test_unnamed_run_keeps_the_record(self, out):
        s = self._meta("home")
        m._apply_endpoint(s, None)
        assert s.endpoint == "home"


def test_meta_round_trip_writes_endpoint_only_when_set(tmp_path, monkeypatch):
    monkeypatch.setattr(session_mod, "_SESSIONS_DIR", tmp_path / "s")
    a = session_mod.create_session("/ws")
    session_mod.save_meta(a)
    raw = json.loads((tmp_path / "s" / a.session_id / "session.jsonl").read_text())
    assert "endpoint" not in raw["_meta"]
    a.endpoint = "work"
    session_mod.save_meta(a)
    assert session_mod.load_session(a.session_id).endpoint == "work"


# ── 실제 run ─────────────────────────────────────────────────


class TestRunCommand:
    def test_records_the_default_server_and_uses_its_values(self, servers):
        r = _run("hi")
        assert r.exit_code == 0, r.output
        (sid,) = _sessions(servers)
        assert _meta(servers, sid)["endpoint"] == "home"
        assert _Provider.made[0][1] == URL_HOME

    def test_endpoint_flag_uses_that_servers_key_and_models(self, servers):
        r = _run("hi", "--endpoint", "work")
        assert r.exit_code == 0, r.output
        (sid,) = _sessions(servers)
        assert _meta(servers, sid)["endpoint"] == "work"
        _name, url, cred = _Provider.made[0]
        assert url == URL_WORK and cred.api_key == _KEY
        assert _KEY not in r.output

    def test_unknown_endpoint_exits_2_and_leaves_no_session(self, servers):
        r = _run("hi", "--endpoint", "nope")
        assert r.exit_code == 2, r.output
        assert "Unknown endpoint 'nope'" in r.output
        assert _sessions(servers) == []

    def test_resume_uses_the_recorded_server(self, servers, next_process):
        assert _run("hi", "--endpoint", "work").exit_code == 0
        (sid,) = _sessions(servers)
        next_process()
        r = _run("again", "--resume", sid)
        assert r.exit_code == 0, r.output
        assert _Provider.made[0][1] == URL_WORK  # 기본(home)이 아니라 기록
        assert "Session server changed" not in r.output

    def test_resume_with_another_endpoint_says_so_and_records_it(
        self, servers, next_process
    ):
        assert _run("hi").exit_code == 0
        (sid,) = _sessions(servers)
        next_process()
        r = _run("again", "--resume", sid, "--endpoint", "work")
        assert r.exit_code == 0, r.output
        assert "Session server changed: home → work" in r.output
        assert _meta(servers, sid)["endpoint"] == "work"

    def test_old_session_without_a_record_gets_the_default(self, servers):
        monkey_meta = session_mod.create_session(str(servers))
        session_mod.save_meta(monkey_meta)
        sid = monkey_meta.session_id
        assert "endpoint" not in _meta(servers, sid)
        r = _run("again", "--resume", sid)
        assert r.exit_code == 0, r.output
        assert _meta(servers, sid)["endpoint"] == "home"

    def test_deleted_server_of_a_session_exits_2(self, servers, next_process):
        assert _run("hi", "--endpoint", "work").exit_code == 0
        (sid,) = _sessions(servers)
        data = json.loads(_config._CONFIG_PATH.read_text())
        del data["endpoints"]["work"]
        _config._CONFIG_PATH.write_text(json.dumps(data))
        next_process()
        r = _run("again", "--resume", sid)
        assert r.exit_code == 2, r.output
        assert "Unknown endpoint 'work'" in r.output

    def test_env_only_run_without_any_config_still_works(self, tmp_path, monkeypatch):
        """설정 파일 없는 컨테이너식 실행 — env 와 ``-m`` 만으로 돈다(서버 기록 없음)."""
        for key in ("AGENT_CLI_ENDPOINT", "AGENT_CLI_PROVIDER", "AGENT_CLI_API_KEY"):
            monkeypatch.delenv(key, raising=False)
        monkeypatch.setenv("AGENT_CLI_NO_INPUT", "1")
        monkeypatch.setenv("AGENT_CLI_BASE_URL", URL_HOME)
        point_config(monkeypatch, tmp_path / "home")
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(
            requests,
            "get",
            lambda *a, **k: (_ for _ in ()).throw(
                requests.exceptions.ConnectionError()
            ),
        )
        r = _run("hi", "-m", "gpt-4o", "--dialect", "json_fc")
        assert r.exit_code == 0, r.output
        (sid,) = _sessions(tmp_path)
        assert "endpoint" not in _meta(tmp_path, sid)
        assert _Provider.made[0][1] == URL_HOME


# ── web: 이어가기 질문은 부트 전 ───────────────────────────────


class TestWebResumePromptBeforeBoot:
    def test_order_and_no_session_on_boot_failure(self, servers, monkeypatch):
        """질문(최근 세션) → 부트 순서이고, 부트가 실패하면 새 세션 디렉터리가 없다."""
        calls = []
        monkeypatch.setattr(
            m,
            "_pick_recent_session",
            lambda ws, fn: calls.append("ask") or None,
        )

        def _boot(*a, **k):
            calls.append(("boot", k.get("recorded_endpoint")))
            raise typer.Exit(2)

        monkeypatch.setattr(m, "_bootstrap_provider", _boot)
        r = CliRunner().invoke(m.app, ["web", "--no-browser"])
        assert r.exit_code == 2, r.output
        assert calls == ["ask", ("boot", None)]
        assert _sessions(servers) == []

    def test_resumed_session_boots_with_its_server(self, servers, monkeypatch):
        prior = session_mod.create_session(str(servers))
        prior.endpoint = "work"
        session_mod.save_meta(prior)
        monkeypatch.setattr(m, "_pick_recent_session", lambda ws, fn: prior)
        seen = []

        def _boot(*a, **k):
            seen.append(k.get("recorded_endpoint"))
            raise typer.Exit(2)

        monkeypatch.setattr(m, "_bootstrap_provider", _boot)
        CliRunner().invoke(m.app, ["web", "--no-browser"])
        assert seen == ["work"]


# ── endpoints 명령 ───────────────────────────────────────────


class TestEndpointsCommand:
    def test_lists_names_hosts_and_default_without_keys(self, servers):
        r = CliRunner().invoke(m.app, ["endpoints"])
        assert r.exit_code == 0, r.output
        lines = [ln.split() for ln in r.output.strip().splitlines()]
        assert lines == [
            ["home", "openai", "127.0.0.1:1", "(default)"],
            ["work", "openai", "127.0.0.1:2"],
        ]
        assert _KEY not in r.output

    def test_no_servers_says_how_to_add_one(self, tmp_path, monkeypatch):
        cfg, _ = point_config(monkeypatch, tmp_path)
        cfg.write_text(json.dumps({"provider": "openai", "api_key": _KEY}))
        r = CliRunner().invoke(m.app, ["endpoints"])
        assert r.exit_code == 0
        assert "No model server is saved" in r.output
        assert "agent-cli setup" in r.output and "board admin" in r.output
        assert "before 10.144.0 are not read" in r.output
        assert _KEY not in r.output


def test_profile_model_missing_names_the_server():
    """프로파일이 고정한 모델이 방의 서버에 없을 때의 거절 문구에 서버 이름."""
    from agent_cli.model_check import ModelListing, ModelNotFound
    from agent_cli.subagent.agents_live import AgentRegistry
    from tests.binding_support import make_binding

    binding = make_binding(base_url=URL_HOME)
    binding.endpoint = "work"
    listing = ModelListing(models=("a", "b"), available=True)

    def _missing(*a, **k):
        raise ModelNotFound("pinned", listing, URL_HOME, "profile")

    with patch("agent_cli.model_check.verify_model", _missing):
        msg = AgentRegistry(None)._model_unavailable("pinned", "code-writer", binding)
    assert 'is not on server "work"' in msg
