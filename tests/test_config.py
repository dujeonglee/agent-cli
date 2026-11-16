"""Tests for agent_cli.config — 서버(endpoint) 표·서버별 모델 표 (v10.144.0 모양)."""

import json
import os

import pytest

import agent_cli.config as _config
from agent_cli.config import (
    ConfigError,
    EndpointError,
    endpoint_config,
    endpoint_host,
    env_endpoint_config,
    get_model_entry,
    get_provider_defaults,
    is_legacy_shape,
    legacy_backup_path,
    list_endpoints,
    model_names,
    pick_endpoint_name,
    retire_legacy_files,
    save_endpoint,
    save_model_entry,
    set_default_endpoint,
    set_endpoint_value,
    use_endpoint,
)
from tests.endpoint_files import point_config


def _models(path, name="home"):
    return json.loads(path.read_text())["endpoints"][name]["models"]


class TestGetModelEntry:
    def test_known_model(self):
        """Models from default_models.json should load."""
        entry = get_model_entry("gpt-4o")
        assert entry is not None
        assert entry["provider"] == "openai"
        assert entry["context_window"] == 128000

    def test_unknown_model(self):
        assert get_model_entry("nonexistent-model") is None

    def test_anthropic_model(self):
        entry = get_model_entry("claude-sonnet-4-20250514")
        assert entry is not None
        assert entry["provider"] == "anthropic"
        assert entry["supports_thinking"] is True


class TestGetProviderDefaults:
    """주소는 준다, 모델 이름은 **추측하지 않는다** (v9.5.0).

    종전엔 openai→``gpt-4o`` / anthropic→``claude-sonnet-4-…`` 를 최후 폴백으로
    끼워 넣었다. 로컬 서버(omlx·vLLM·LM Studio)에는 그런 모델이 있을 리 없으니
    이 추측은 **절대 맞을 수 없고**, "모델을 안 골랐다"를 "없는 모델 404"로
    바꿔 원인을 가렸다(사용자 제보). 주소는 사정이 다르다 — 프로바이더의 공개
    엔드포인트는 실제로 그 주소가 맞다."""

    def test_anthropic_gives_url_but_no_model_guess(self):
        defaults = get_provider_defaults("anthropic")
        assert "api.anthropic.com" in defaults.base_url
        assert defaults.default_model == ""

    def test_openai_gives_url_but_no_model_guess(self):
        defaults = get_provider_defaults("openai")
        assert "api.openai.com" in defaults.base_url
        assert defaults.default_model == ""

    def test_unknown_provider_fallback(self):
        defaults = get_provider_defaults("unknown_provider")
        assert defaults.base_url == "http://127.0.0.1:8000/v1"
        assert defaults.default_model == ""

    def test_no_provider_default_ships_a_model_name(self):
        """패키지 기본 레지스트리에 모델 이름이 **되살아나지 않도록** 하는 가드.
        누가 편의로 다시 넣으면 같은 고장이 그대로 돌아온다."""
        from pathlib import Path

        import agent_cli

        raw = json.loads(
            (Path(agent_cli.__file__).parent / "default_models.json").read_text()
        )
        for provider, entry in raw.get("provider_defaults", {}).items():
            assert "default_model" not in entry, (
                f"{provider} 에 모델 추측이 다시 들어왔다: {entry.get('default_model')!r}"
            )


class TestSaveModelEntry:
    """탐지 결과 저장은 이 프로세스 서버의 칸으로 (v10.144.0)."""

    def test_save_new_model(self, tmp_path, monkeypatch):
        _, mdl = point_config(monkeypatch, tmp_path, use="home")
        assert save_model_entry(
            "new-model:7b", {"context_window": 8192, "max_output_tokens": 2048}
        )
        assert _models(mdl)["new-model:7b"]["context_window"] == 8192

    def test_fill_adds_only_missing_keys_even_to_manual_entries(
        self, tmp_path, monkeypatch
    ):
        """프로브 결과 영속 (v10.68.0): 손으로 등록한 엔트리라도 **없는 키** 는
        채운다, 있는 키는 사용자 값이 이긴다, 모르는 모델은 만들지 않는다."""
        from agent_cli.config import fill_model_entry

        _, mdl = point_config(
            monkeypatch,
            tmp_path,
            models={"home": {"m": {"context_window": 4096, "supports_grammar": True}}},
            use="home",
        )
        assert fill_model_entry("m", {"supports_vision": False}) is True
        data = _models(mdl)["m"]
        assert data["supports_vision"] is False and data["context_window"] == 4096
        assert fill_model_entry("m", {"supports_vision": True}) is False
        assert _models(mdl)["m"]["supports_vision"] is False
        assert fill_model_entry("unknown", {"supports_vision": True}) is False
        assert "unknown" not in _models(mdl)

    def test_no_overwrite_existing(self, tmp_path, monkeypatch):
        _, mdl = point_config(
            monkeypatch,
            tmp_path,
            models={"home": {"existing:8b": {"context_window": 4096}}},
            use="home",
        )
        assert save_model_entry("existing:8b", {"context_window": 99999}) is False
        assert _models(mdl)["existing:8b"]["context_window"] == 4096

    def test_overwrite_auto_detected(self, tmp_path, monkeypatch):
        _, mdl = point_config(
            monkeypatch,
            tmp_path,
            models={
                "home": {
                    "gemma4:31b": {
                        "context_window": 4096,
                        "supports_thinking": False,
                        "_auto_detected": True,
                    }
                }
            },
            use="home",
        )
        assert save_model_entry(
            "gemma4:31b",
            {
                "context_window": 131072,
                "supports_thinking": True,
                "_auto_detected": True,
            },
        )
        assert _models(mdl)["gemma4:31b"]["context_window"] == 131072
        assert _models(mdl)["gemma4:31b"]["supports_thinking"] is True

    def test_refresh_preserves_hand_added_keys(self, tmp_path, monkeypatch):
        """auto-detected refresh 는 프로브 필드만 갱신 — 손으로 추가한 dialect 는 보존."""
        _, mdl = point_config(
            monkeypatch,
            tmp_path,
            models={
                "home": {
                    "qwen5:35b": {
                        "context_window": 4096,
                        "dialect": "json_fc",
                        "_auto_detected": True,
                    }
                }
            },
            use="home",
        )
        assert save_model_entry(
            "qwen5:35b", {"context_window": 131072, "_auto_detected": True}
        )
        assert _models(mdl)["qwen5:35b"]["context_window"] == 131072
        assert _models(mdl)["qwen5:35b"]["dialect"] == "json_fc"

    def test_no_overwrite_manual_entry(self, tmp_path, monkeypatch):
        _, mdl = point_config(
            monkeypatch,
            tmp_path,
            models={"home": {"custom:7b": {"context_window": 8192}}},
            use="home",
        )
        assert (
            save_model_entry(
                "custom:7b", {"context_window": 99999, "_auto_detected": True}
            )
            is False
        )
        assert _models(mdl)["custom:7b"]["context_window"] == 8192

    def test_creates_directory(self, tmp_path, monkeypatch):
        point_config(monkeypatch, tmp_path, use="home")
        target = tmp_path / "subdir" / "models.json"
        monkeypatch.setattr(_config, "_GLOBAL_MODELS_PATH", target)
        save_model_entry("test:1b", {"context_window": 2048})
        assert target.exists()

    def test_other_servers_are_untouched(self, tmp_path, monkeypatch):
        """저장은 이 서버 칸만 — 다른 서버의 같은 이름 엔트리는 그대로."""
        _, mdl = point_config(
            monkeypatch,
            tmp_path,
            models={"work": {"m": {"context_window": 1}}},
            use="home",
        )
        assert save_model_entry("m", {"context_window": 2})
        assert _models(mdl, "work")["m"]["context_window"] == 1
        assert _models(mdl, "home")["m"]["context_window"] == 2

    def test_nothing_is_saved_without_a_server(self, tmp_path, monkeypatch):
        """서버가 골라지지 않은 실행(env 만)은 탐지 결과를 저장하지 않는다."""
        _, mdl = point_config(monkeypatch, tmp_path)
        use_endpoint(None)
        assert save_model_entry("m", {"context_window": 2}) is False
        assert not mdl.exists()

    def test_legacy_models_file_is_neither_read_nor_written(
        self, tmp_path, monkeypatch, capsys
    ):
        _, mdl = point_config(monkeypatch, tmp_path, use="home")
        old = json.dumps({"models": {"m": {"dialect": "json_fc"}}})
        mdl.write_text(old)
        assert get_model_entry("m") is None
        assert save_model_entry("m2", {"context_window": 2}) is False
        assert mdl.read_text() == old
        assert "pre-10.144.0 shape" in capsys.readouterr().err


class TestModelsPerServer:
    def test_the_server_section_overlays_the_package_floor(self, tmp_path, monkeypatch):
        point_config(
            monkeypatch,
            tmp_path,
            models={"home": {"gpt-4o": {"context_window": 7}, "mine": {}}},
            use="home",
        )
        assert get_model_entry("gpt-4o")["context_window"] == 7
        assert "mine" in model_names() and "claude-sonnet-4-20250514" in model_names()

    def test_another_server_does_not_see_the_entries(self, tmp_path, monkeypatch):
        point_config(monkeypatch, tmp_path, models={"home": {"mine": {}}}, use="work")
        assert get_model_entry("mine") is None
        assert get_model_entry("gpt-4o") is not None  # 바닥은 늘 깔린다

    def test_no_server_reads_only_the_package_floor(self, tmp_path, monkeypatch):
        point_config(monkeypatch, tmp_path, models={"home": {"mine": {}}})
        use_endpoint(None)
        assert get_model_entry("mine") is None
        assert get_model_entry("gpt-4o") is not None


class TestUseEndpoint:
    def test_once_per_process(self):
        use_endpoint("home")
        use_endpoint("home")  # 같은 이름은 그대로
        with pytest.raises(RuntimeError, match="already uses endpoint 'home'"):
            use_endpoint("work")
        assert _config.current_endpoint() == "home"

    def test_none_also_counts(self):
        use_endpoint(None)
        with pytest.raises(RuntimeError):
            use_endpoint("home")

    def test_there_is_no_reset_path_in_the_module(self):
        """재설정은 conftest 픽스처 한 곳 — 제품 코드에 `_current` 를 되돌리는 함수가 없다."""
        import inspect

        src = inspect.getsource(_config)
        assert src.count("global _current") == 1  # use_endpoint 하나만 대입한다


class TestPickEndpointName:
    @pytest.mark.parametrize(
        ("flag", "env", "recorded", "default", "expected"),
        [
            ("a", "b", "c", "d", "a"),
            (None, "b", "c", "d", "b"),
            (None, "", "c", "d", "c"),
            (None, "", "", "d", "d"),
            (None, "", "", None, None),
            ("  ", " ", None, "d", "d"),
        ],
    )
    def test_order(self, flag, env, recorded, default, expected):
        environ = {"AGENT_CLI_ENDPOINT": env} if env is not None else {}
        assert pick_endpoint_name(flag, environ, recorded, default) == expected


class TestEndpointTable:
    def test_values_of_a_server(self, tmp_path, monkeypatch):
        hdrs = [{"name": "X-Team", "value": "v"}]
        point_config(
            monkeypatch,
            tmp_path,
            endpoints={
                "home": {
                    "provider": "openai",
                    "base_url": "http://h/v1",
                    "api_key": "",
                    "extra_headers": hdrs,
                    "context": 3,
                }
            },
        )
        assert endpoint_config("home") == {
            "provider": "openai",
            "base_url": "http://h/v1",
            "extra_headers": hdrs,
            "context": "3",
        }

    def test_unknown_name_is_an_error_naming_the_saved_ones(
        self, tmp_path, monkeypatch
    ):
        point_config(monkeypatch, tmp_path, endpoints={"home": {}, "work": {}})
        with pytest.raises(EndpointError, match=r"'lab'.*home, work"):
            endpoint_config("lab")

    def test_legacy_shape_is_no_server_and_is_not_read(self, tmp_path, monkeypatch):
        cfg, _ = point_config(monkeypatch, tmp_path)
        cfg.write_text(json.dumps({"provider": "openai", "base_url": "http://x/v1"}))
        assert list_endpoints() == {}
        assert _config.default_endpoint_name() is None
        assert _config.legacy_config_present() is True
        with pytest.raises(EndpointError):
            endpoint_config("home")

    def test_unreadable_file_is_a_config_error(self, tmp_path, monkeypatch):
        cfg, _ = point_config(monkeypatch, tmp_path)
        cfg.write_text("{broken")
        with pytest.raises(ConfigError):
            list_endpoints()

    def test_endpoints_must_be_objects(self, tmp_path, monkeypatch):
        cfg, _ = point_config(monkeypatch, tmp_path)
        cfg.write_text(json.dumps({"endpoints": {"home": "x"}}))
        with pytest.raises(ConfigError, match="server objects"):
            list_endpoints()

    def test_is_legacy_shape(self):
        assert is_legacy_shape({"provider": "openai"})
        assert is_legacy_shape({"models": {}})
        assert not is_legacy_shape({})
        assert not is_legacy_shape({"endpoints": {}})


class TestEnvEndpoint:
    """이름 없는 서버 = 서버가 골라지지 않은 실행의 env 세 개 (종전 "env 는 파일보다 낮다" 의 자리)."""

    def test_env_values(self):
        env = {
            "AGENT_CLI_PROVIDER": "anthropic",
            "AGENT_CLI_API_KEY": "k",
            "AGENT_CLI_BASE_URL": "",
        }
        assert env_endpoint_config(env) == {"provider": "anthropic", "api_key": "k"}

    def test_model_env_is_not_a_server_field(self):
        assert env_endpoint_config({"AGENT_CLI_MODEL": "m"}) == {}


class TestSaveEndpoint:
    def test_creates_and_keeps_other_keys(self, tmp_path, monkeypatch):
        cfg, _ = point_config(
            monkeypatch,
            tmp_path,
            endpoints={
                "home": {"provider": "openai", "extra_headers": [{"name": "A"}]}
            },
        )
        save_endpoint("home", {"base_url": "http://h/v1"})
        save_endpoint("work", {"provider": "anthropic"})
        data = json.loads(cfg.read_text())
        assert data["endpoint"] == "home"
        assert data["endpoints"]["home"] == {
            "provider": "openai",
            "extra_headers": [{"name": "A"}],
            "base_url": "http://h/v1",
        }
        assert data["endpoints"]["work"] == {"provider": "anthropic"}

    def test_creates_the_file(self, tmp_path, monkeypatch):
        point_config(monkeypatch, tmp_path)
        target = tmp_path / "sub" / "config.json"
        monkeypatch.setattr(_config, "_CONFIG_PATH", target)
        save_endpoint("home", {"provider": "openai"})
        assert list_endpoints() == {"home": {"provider": "openai"}}

    @pytest.mark.parametrize("bad", ["Home", "a b", "", "a_b", "é"])
    def test_name_rule(self, tmp_path, monkeypatch, bad):
        point_config(monkeypatch, tmp_path)
        with pytest.raises(EndpointError, match="lowercase"):
            save_endpoint(bad, {})

    def test_refuses_to_mix_into_a_legacy_file(self, tmp_path, monkeypatch):
        cfg, _ = point_config(monkeypatch, tmp_path)
        cfg.write_text(json.dumps({"provider": "openai"}))
        with pytest.raises(ConfigError, match="pre-10.144.0"):
            save_endpoint("home", {})
        assert json.loads(cfg.read_text()) == {"provider": "openai"}

    def test_default_must_exist(self, tmp_path, monkeypatch):
        cfg, _ = point_config(monkeypatch, tmp_path, endpoints={"home": {}})
        with pytest.raises(EndpointError):
            set_default_endpoint("work")
        save_endpoint("work", {})
        set_default_endpoint("work")
        assert json.loads(cfg.read_text())["endpoint"] == "work"

    def test_set_value(self, tmp_path, monkeypatch):
        cfg, _ = point_config(monkeypatch, tmp_path, endpoints={"home": {"a": 1}})
        assert set_endpoint_value("home", "default_model", "m") is True
        assert json.loads(cfg.read_text())["endpoints"]["home"] == {
            "a": 1,
            "default_model": "m",
        }
        assert set_endpoint_value("work", "default_model", "m") is False


class TestRetireLegacyFiles:
    def test_renames_legacy_files_without_reading_them_into_servers(
        self, tmp_path, monkeypatch
    ):
        cfg, mdl = point_config(monkeypatch, tmp_path)
        cfg.write_text(json.dumps({"provider": "openai", "api_key": "k"}))
        mdl.write_text(json.dumps({"models": {"m": {}}}))
        moved = retire_legacy_files()
        assert moved == [tmp_path / "config.json.v1", tmp_path / "models.json.v1"]
        assert not cfg.exists() and not mdl.exists()
        assert json.loads((tmp_path / "config.json.v1").read_text())["api_key"] == "k"
        assert list_endpoints() == {}

    def test_never_overwrites_a_backup(self, tmp_path, monkeypatch):
        cfg, _ = point_config(monkeypatch, tmp_path)
        (tmp_path / "config.json.v1").write_text("first")
        (tmp_path / "config.json.v1-2").write_text("second")
        cfg.write_text(json.dumps({"provider": "openai"}))
        assert retire_legacy_files([cfg]) == [tmp_path / "config.json.v1-3"]
        assert (tmp_path / "config.json.v1").read_text() == "first"
        assert (tmp_path / "config.json.v1-2").read_text() == "second"

    def test_backup_path_rule(self, tmp_path):
        p = tmp_path / "models.json"
        assert legacy_backup_path(p).name == "models.json.v1"
        (tmp_path / "models.json.v1").write_text("")
        assert legacy_backup_path(p).name == "models.json.v1-2"

    def test_new_shape_and_missing_files_stay(self, tmp_path, monkeypatch):
        cfg, mdl = point_config(monkeypatch, tmp_path, endpoints={"home": {}})
        assert retire_legacy_files() == []
        assert cfg.exists() and not mdl.exists()

    def test_unreadable_file_is_kept_aside_too(self, tmp_path, monkeypatch):
        cfg, _ = point_config(monkeypatch, tmp_path)
        cfg.write_text("{broken")
        assert retire_legacy_files([cfg]) == [tmp_path / "config.json.v1"]


class TestEndpointHost:
    @pytest.mark.parametrize(
        ("url", "host"),
        [
            ("http://127.0.0.1:8000/v1", "127.0.0.1:8000"),
            ("https://gw.example.com/v1?token=secret", "gw.example.com"),
            ("https://user:pw@gw.example.com:8443/x", "gw.example.com:8443"),
            ("http://[::1]:9/v1", "[::1]:9"),
            ("", ""),
            ("not a url", ""),
        ],
    )
    def test_only_host_and_port(self, url, host):
        assert endpoint_host(url) == host


class TestRegistryReloadOnChange:
    """v10.3.0: models.json 이 바뀌면 재시작 없이 다음 조회가 다시 읽는다
    (보드 어드민의 바인딩 편집 → 다음 spawn 부터 반영)."""

    def test_edit_is_seen_without_reload(self, tmp_path, monkeypatch):
        _, mdl = point_config(
            monkeypatch,
            tmp_path,
            models={"home": {"m": {"dialect": "json_fc"}}},
            use="home",
        )
        assert get_model_entry("m")["dialect"] == "json_fc"
        mdl.write_text(
            json.dumps(
                {"endpoints": {"home": {"models": {"m": {"dialect": "xml_fc"}}}}}
            )
        )
        os.utime(mdl, ns=(0, mdl.stat().st_mtime_ns + 1_000_000))
        assert get_model_entry("m")["dialect"] == "xml_fc"

    def test_unchanged_file_is_not_reread(self, tmp_path, monkeypatch):
        point_config(
            monkeypatch,
            tmp_path,
            models={"home": {"m": {"dialect": "json_fc"}}},
            use="home",
        )
        first = _config._load_registry()
        assert _config._load_registry() is first
