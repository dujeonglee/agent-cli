"""Tests for setup wizard helpers."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

from agent_cli.providers.credentials import Credentials
from agent_cli.setup import (
    SetupWizard,
    _list_models,
)


class TestListOpenAIModels:
    def test_returns_ids(self):
        """omlx/vLLM /v1/models → list of model ids."""
        with patch("agent_cli.setup.requests.get") as mock_get:
            mock_get.return_value = MagicMock(
                status_code=200,
                json=lambda: {
                    "data": [
                        {"id": "Qwen3.6-27B-MLX-8bit", "object": "model"},
                        {"id": "Qwen3.6-35B-A3B-MLX-8bit", "object": "model"},
                    ]
                },
            )
            models = _list_models("http://127.0.0.1:8000/v1", Credentials())
            assert models == ["Qwen3.6-27B-MLX-8bit", "Qwen3.6-35B-A3B-MLX-8bit"]

    def test_passes_api_key(self):
        with patch("agent_cli.setup.requests.get") as mock_get:
            mock_get.return_value = MagicMock(
                status_code=200, json=lambda: {"data": []}
            )
            _list_models("http://x/v1", Credentials(api_key="secret"))
            headers = mock_get.call_args.kwargs.get("headers", {})
            assert headers.get("Authorization") == "Bearer secret"

    def test_anthropic_uses_xapikey_and_version_headers(self):
        """Anthropic /v1/models needs x-api-key + anthropic-version, not Bearer."""
        with patch("agent_cli.setup.requests.get") as mock_get:
            mock_get.return_value = MagicMock(
                status_code=200,
                json=lambda: {"data": [{"id": "claude-sonnet-4-20250514"}]},
            )
            models = _list_models(
                "http://x/v1", Credentials(api_key="sk"), provider="anthropic"
            )
            headers = mock_get.call_args.kwargs.get("headers", {})
            assert headers.get("x-api-key") == "sk"
            assert headers.get("anthropic-version") == "2023-06-01"
            assert "Authorization" not in headers
            assert models == ["claude-sonnet-4-20250514"]

    def test_anthropic_version_header_sent_even_without_key(self):
        with patch("agent_cli.setup.requests.get") as mock_get:
            mock_get.return_value = MagicMock(
                status_code=200, json=lambda: {"data": []}
            )
            _list_models("http://x/v1", Credentials(), provider="anthropic")
            headers = mock_get.call_args.kwargs.get("headers", {})
            assert headers.get("anthropic-version") == "2023-06-01"
            assert "x-api-key" not in headers  # no key → omit

    def test_empty_on_failure(self):
        with patch("agent_cli.setup.requests.get", side_effect=Exception("fail")):
            assert _list_models("http://x/v1", Credentials()) == []

    def test_empty_on_non_200(self):
        with patch("agent_cli.setup.requests.get") as mock_get:
            mock_get.return_value = MagicMock(status_code=404, json=dict)
            assert _list_models("http://x/v1", Credentials()) == []

    def test_skips_entries_without_id(self):
        with patch("agent_cli.setup.requests.get") as mock_get:
            mock_get.return_value = MagicMock(
                status_code=200,
                json=lambda: {"data": [{"object": "model"}, {"id": "good"}]},
            )
            assert _list_models("http://x/v1", Credentials()) == ["good"]


class TestSelectModelRouting:
    def test_openai_provider_uses_v1_models(self):
        """provider=openai must route to the /v1/models listing path."""
        wiz = SetupWizard()
        with (
            patch(
                "agent_cli.setup._list_models",
                return_value=["m1", "m2"],
            ) as mock_list,
            patch("agent_cli.setup.IntPrompt.ask", return_value=2),
        ):
            selected = wiz._select_model("openai", "http://x/v1", Credentials())
        mock_list.assert_called_once()
        assert selected == "m2"

    def test_openai_falls_back_to_manual_when_empty(self):
        wiz = SetupWizard()
        with (
            patch("agent_cli.setup._list_models", return_value=[]),
            patch("agent_cli.setup.Prompt.ask", return_value="typed-model"),
        ):
            selected = wiz._select_model("openai", "http://x/v1", Credentials())
        assert selected == "typed-model"

    def test_anthropic_provider_lists_models(self):
        """provider=anthropic now also lists models (not just a manual prompt)."""
        wiz = SetupWizard()
        with (
            patch(
                "agent_cli.setup._list_models",
                return_value=["claude-a", "claude-b"],
            ) as mock_list,
            patch("agent_cli.setup.IntPrompt.ask", return_value=2),
        ):
            selected = wiz._select_model(
                "anthropic", "http://x/v1", Credentials(api_key="k")
            )
        mock_list.assert_called_once()
        # listing was queried with the anthropic provider
        assert (
            mock_list.call_args.args[2] == "anthropic"
            or mock_list.call_args.kwargs.get("provider") == "anthropic"
        )
        assert selected == "claude-b"

    def test_anthropic_falls_back_to_manual_when_empty(self):
        wiz = SetupWizard()
        with (
            patch("agent_cli.setup._list_models", return_value=[]),
            patch("agent_cli.setup.Prompt.ask", return_value="claude-typed"),
        ):
            selected = wiz._select_model("anthropic", "http://x/v1", Credentials())
        assert selected == "claude-typed"


class TestSetupWizardConfig:
    def test_build_config(self):
        """Wizard builds correct config dict."""
        wizard = SetupWizard()
        config = wizard._build_config(
            provider="openai",
            base_url="http://127.0.0.1:8000/v1",
            api_key="",
            default_model="gpt-4o",
        )
        assert config["provider"] == "openai"
        assert config["base_url"] == "http://127.0.0.1:8000/v1"
        assert config["api_key"] == ""
        assert config["default_model"] == "gpt-4o"


class TestShowExisting:
    """The wizard shows the saved servers at startup so the user can see what they
    are about to edit. Silent when nothing exists. 옛 모양 파일은 표가 아니라 한 줄 안내."""

    def _show(self, monkeypatch, tmp_path, *, width=120, **kwargs):
        from io import StringIO

        from rich.console import Console

        from agent_cli.setup import _saved_endpoints
        from tests.endpoint_files import point_config

        cfg, _ = point_config(monkeypatch, tmp_path, **kwargs)
        wizard = SetupWizard()
        buf = StringIO()
        wizard.console = Console(file=buf, force_terminal=False, width=width)
        wizard._show_existing(*_saved_endpoints())
        return buf.getvalue(), cfg

    def test_silent_when_no_configs(self, tmp_path, monkeypatch):
        out, _ = self._show(monkeypatch, tmp_path)
        assert out == ""

    def test_project_config_is_ignored(self, tmp_path, monkeypatch):
        """v9.0.0 (docs/config-scopes): config.json 은 유저 스코프만 — cwd 의 것은 보이지 않는다."""
        project = tmp_path / "project"
        (project / ".agent-cli").mkdir(parents=True)
        (project / ".agent-cli" / "config.json").write_text(
            json.dumps({"endpoint": "p", "endpoints": {"p": {"provider": "openai"}}})
        )
        monkeypatch.chdir(project)
        out, _ = self._show(monkeypatch, tmp_path / "home")
        assert out == ""

    def test_shows_saved_servers_and_default(self, tmp_path, monkeypatch):
        out, _ = self._show(
            monkeypatch,
            tmp_path,
            endpoints={
                "home": {"provider": "openai", "default_model": "m1"},
                "work": {"provider": "anthropic", "base_url": "https://w/v1"},
            },
        )
        assert "Saved servers" in out
        assert "home (default)" in out and "work" in out and "https://w/v1" in out

    def test_api_key_masked_in_display(self, tmp_path, monkeypatch):
        out, _ = self._show(
            monkeypatch,
            tmp_path,
            endpoints={"home": {"api_key": "sk-supersecret-do-not-leak"}},
        )
        assert "sk-supersecret-do-not-leak" not in out
        assert "***" in out

    def test_legacy_file_is_a_note_not_a_table(self, tmp_path, monkeypatch):
        from tests.endpoint_files import point_config

        cfg, _ = point_config(monkeypatch, tmp_path)
        cfg.write_text(json.dumps({"provider": "openai", "api_key": "sk-old"}))
        out, _ = self._show(monkeypatch, tmp_path)
        assert "before 10.144.0 are not read" in out and "config.json.v1" in out
        assert "sk-old" not in out and "Saved servers" not in out

    def test_malformed_config_is_reported_not_fatal(self, tmp_path, monkeypatch):
        """Unparseable config file doesn't crash the wizard — setup is the way to
        fix it — but it says so instead of acting as if absent (v10.123.4)."""
        from tests.endpoint_files import point_config

        # 경로는 80칸을 넘게, 폭은 80 — 경로가 한 덩어리로 나와야 복사해 쓸 수 있다
        home = tmp_path / ("d" * 40) / ("e" * 40) / "home"
        home.mkdir(parents=True)
        cfg, _ = point_config(monkeypatch, home)
        cfg.write_text('{"provider": "openai",}', encoding="utf-8")
        assert len(str(cfg)) > 80
        out, _ = self._show(monkeypatch, home, width=80)
        assert "Could not read the existing config" in out
        assert "will not be in the new file" in " ".join(out.split())
        assert str(cfg) in out
        assert "line 1" in out
        assert "Saved servers" not in out


class TestWizardServer:
    """마법사는 서버 하나를 만들거나 고친다 (v10.144.0)."""

    def _run(self, monkeypatch, tmp_path, *, name, url, key, confirm=(True,), **kw):
        from unittest.mock import patch

        from tests.endpoint_files import point_config

        cfg, mdl = point_config(monkeypatch, tmp_path, **kw)
        answers = iter(confirm)
        wiz = SetupWizard()
        wiz.console = MagicMock()
        with (
            patch("agent_cli.setup._list_models", return_value=["m1", "m2"]),
            patch.object(wiz, "_select_name", return_value=name),
            patch.object(wiz, "_select_provider", return_value="openai"),
            patch.object(wiz, "_configure_connection", return_value=(url, key)),
            patch("agent_cli.setup.IntPrompt.ask", return_value=2),
            patch(
                "agent_cli.setup.Confirm.ask", side_effect=lambda *a, **k: next(answers)
            ),
        ):
            result = wiz.run()
        return result, cfg, mdl

    def test_first_server_becomes_the_default(self, tmp_path, monkeypatch):
        result, cfg, _ = self._run(
            monkeypatch, tmp_path, name="home", url="http://h/v1", key=""
        )
        data = json.loads(cfg.read_text())
        assert data == {
            "endpoints": {
                "home": {
                    "provider": "openai",
                    "base_url": "http://h/v1",
                    "api_key": "",
                    "default_model": "m2",
                }
            },
            "endpoint": "home",
        }
        assert result["name"] == "home"

    def test_second_server_asks_before_taking_the_default(self, tmp_path, monkeypatch):
        _, cfg, _ = self._run(
            monkeypatch,
            tmp_path,
            name="work",
            url="http://w/v1",
            key="k",
            confirm=(False, True),  # 기본으로? 아니오 · 저장? 예
            endpoints={"home": {"provider": "openai"}},
        )
        data = json.loads(cfg.read_text())
        assert data["endpoint"] == "home"
        assert data["endpoints"]["work"]["api_key"] == "k"

    def test_editing_with_an_empty_key_keeps_the_saved_one(self, tmp_path, monkeypatch):
        hdrs = [{"name": "X-Team", "value": "v"}]
        _, cfg, _ = self._run(
            monkeypatch,
            tmp_path,
            name="home",
            url="http://new/v1",
            key="",
            endpoints={"home": {"api_key": "old-key", "extra_headers": hdrs}},
        )
        saved = json.loads(cfg.read_text())["endpoints"]["home"]
        assert saved["api_key"] == "old-key"
        assert saved["extra_headers"] == hdrs
        assert saved["base_url"] == "http://new/v1"

    def test_legacy_files_are_kept_as_v1_on_save(self, tmp_path, monkeypatch):
        from tests.endpoint_files import point_config

        cfg, mdl = point_config(monkeypatch, tmp_path)
        cfg.write_text(json.dumps({"provider": "openai", "default_model": "old"}))
        mdl.write_text(json.dumps({"models": {"old": {}}}))
        _, cfg, mdl = self._run(
            monkeypatch, tmp_path, name="home", url="http://h/v1", key=""
        )
        assert (
            json.loads((tmp_path / "config.json.v1").read_text())["default_model"]
            == "old"
        )
        assert (tmp_path / "models.json.v1").is_file() and not mdl.exists()
        assert list(json.loads(cfg.read_text())["endpoints"]) == ["home"]

    def test_cancel_writes_nothing(self, tmp_path, monkeypatch):
        _, cfg, _ = self._run(
            monkeypatch,
            tmp_path,
            name="home",
            url="http://h/v1",
            key="",
            confirm=(False,),
        )
        assert not cfg.exists()

    def test_name_prompt_repeats_until_valid(self, monkeypatch):
        answers = iter(["Home", "my server", "lab-2"])
        monkeypatch.setattr("agent_cli.setup.Prompt.ask", lambda *a, **k: next(answers))
        wiz = SetupWizard()
        wiz.console = MagicMock()
        assert wiz._select_name({}) == "lab-2"
