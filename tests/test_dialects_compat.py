"""v10.0.0 — ``wire_formats`` → ``dialects`` 개명의 호환층 (docs/dialects/PHASE5.md §6).

세 계약이 옛 이름으로도 계속 동작하는지 고정한다:
패키지 shim · CLI ``--response-format`` 별칭 · models.json ``wire_format`` 키.
shim 과 CLI 별칭은 v11 에서 빠진다. (세션 메타의 방언 키는 v10.3.0 에서
사라졌다 — 방언은 모델 바인딩이 정한다.)
"""

from __future__ import annotations

import importlib
import json
import sys
import warnings

import typer

import agent_cli.config as _config
from agent_cli import dialects
from agent_cli.dialects import dialect_for_model


class TestPackageShim:
    def _fresh_import(self):
        sys.modules.pop("agent_cli.wire_formats", None)
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            mod = importlib.import_module("agent_cli.wire_formats")
        return mod, caught

    def test_import_warns_deprecation(self):
        _, caught = self._fresh_import()
        assert any(
            issubclass(w.category, DeprecationWarning)
            and "agent_cli.dialects" in str(w.message)
            for w in caught
        )

    def test_reexports_registry_and_old_names(self):
        mod, _ = self._fresh_import()
        assert mod.get is dialects.get
        assert mod.list_names is dialects.list_names
        assert mod.register is dialects.register
        assert mod.try_foreign_parse is dialects.try_foreign_parse
        assert mod.all_system_user_prefixes is dialects.all_system_user_prefixes
        # 옛 이름 별칭
        assert mod.WireFormat is dialects.DialectBase
        assert mod.DEFAULT_WIRE_FORMAT == dialects.DEFAULT_DIALECT
        assert mod.wire_format_for_model is dialects.dialect_for_model
        assert mod.resolve_wire_format is dialects.resolve_dialect
        assert mod.get("json_fc") is dialects.get("json_fc")


class TestCliAlias:
    @staticmethod
    def _opts(command_name: str) -> list[str]:
        from agent_cli.main import app

        group = typer.main.get_command(app)
        cmd = group.commands[command_name]
        return [o for p in cmd.params for o in getattr(p, "opts", [])]

    def test_run_accepts_both_flags(self):
        opts = self._opts("run")
        assert "--dialect" in opts
        assert "--response-format" in opts

    def test_web_accepts_both_flags(self):
        opts = self._opts("web")
        assert "--dialect" in opts
        assert "--response-format" in opts

    def test_both_flags_feed_one_parameter(self):
        from agent_cli.main import app

        cmd = typer.main.get_command(app).commands["run"]
        owners = {o: p.name for p in cmd.params for o in getattr(p, "opts", [])}
        assert owners["--dialect"] == owners["--response-format"] == "dialect"


class TestModelsJsonOldKey:
    def _models(self, tmp_path, monkeypatch, models: dict):
        target = tmp_path / "models.json"
        target.write_text(json.dumps({"models": models}), encoding="utf-8")
        monkeypatch.setattr(_config, "_SEARCH_PATHS", [target])
        monkeypatch.setattr(_config, "_cached_registry", None)

    def test_old_key_still_read(self, tmp_path, monkeypatch):
        self._models(tmp_path, monkeypatch, {"m": {"wire_format": "xml_fc"}})
        assert dialect_for_model("m") == "xml_fc"

    def test_new_key_wins_over_old(self, tmp_path, monkeypatch):
        self._models(
            tmp_path,
            monkeypatch,
            {"m": {"dialect": "json_fc", "wire_format": "xml_fc"}},
        )
        assert dialect_for_model("m") == "json_fc"

    def test_empty_new_key_falls_back_to_old(self, tmp_path, monkeypatch):
        self._models(
            tmp_path, monkeypatch, {"m": {"dialect": "", "wire_format": "xml_fc"}}
        )
        assert dialect_for_model("m") == "xml_fc"
