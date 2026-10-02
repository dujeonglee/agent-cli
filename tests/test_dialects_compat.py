"""v10.4.0 — 옛 이름 호환층 제거의 회귀 가드.

v10.0.0 의 ``wire_formats`` → ``dialects`` 개명이 남겼던 세 호환 계약
(패키지 shim · CLI ``--response-format`` 별칭 · models.json ``wire_format``
키)은 v10.4.0 에서 사라졌다. 여기서는 그것들이 **되살아나지 않음**을
고정한다 — 옛 키만 있는 엔트리는 미설정이고, 옛 플래그·옛 import 는
즉시 실패한다 (조용한 수용 금지).
"""

from __future__ import annotations

import importlib
import json
import re
import sys

import pytest
import typer
from typer.testing import CliRunner

import agent_cli.config as _config
import agent_cli.dialects as _dialects
from agent_cli.dialects import DialectUnbound, dialect_for_model, resolve_dialect


class TestNoPackageShim:
    def test_old_package_does_not_import(self):
        sys.modules.pop("agent_cli.wire_formats", None)
        with pytest.raises(ModuleNotFoundError):
            importlib.import_module("agent_cli.wire_formats")

    def test_old_aliases_absent_from_dialects(self):
        for name in (
            "WireFormat",
            "DEFAULT_WIRE_FORMAT",
            "wire_format_for_model",
            "resolve_wire_format",
        ):
            assert not hasattr(_dialects, name), name


class TestNoCliAlias:
    @staticmethod
    def _opts(command_name: str) -> list[str]:
        from agent_cli.main import app

        group = typer.main.get_command(app)
        cmd = group.commands[command_name]
        return [o for p in cmd.params for o in getattr(p, "opts", [])]

    @pytest.mark.parametrize("command", ["run", "web"])
    def test_only_dialect_flag_is_registered(self, command):
        opts = self._opts(command)
        assert "--dialect" in opts
        assert "--response-format" not in opts

    def test_old_flag_is_rejected_before_boot(self, monkeypatch):
        # 옛 플래그를 받아 조용히 무시하면 "바인딩 됐다고 믿는" 오진이 된다
        # — click 의 unknown-option 으로 즉시 실패(Exit 2)해야 한다.
        import agent_cli.main as main_mod

        booted = []
        monkeypatch.setattr(
            main_mod, "_bootstrap_provider", lambda *a, **k: booted.append(1)
        )
        result = CliRunner().invoke(
            main_mod.app, ["run", "--response-format", "json_fc", "hi"]
        )
        assert result.exit_code == 2
        # 리눅스 CI 는 typer 의 rich 출력에 색상 코드가 섞여 옵션 문자열이
        # 쪼개진다 — ANSI 를 벗긴 뒤 검사 (macOS 로컬만 초록이던 함정).
        plain = re.sub(r"\x1b\[[0-9;]*m", "", result.output)
        assert "No such option: --response-format" in plain
        assert booted == []


class TestModelsJsonOldKeyIgnored:
    def _models(self, tmp_path, monkeypatch, models: dict):
        target = tmp_path / "models.json"
        target.write_text(json.dumps({"models": models}), encoding="utf-8")
        monkeypatch.setattr(_config, "_SEARCH_PATHS", [target])
        monkeypatch.setattr(_config, "_cached_registry", None)
        monkeypatch.setattr(_dialects, "_override", None)

    def test_old_key_alone_is_unbound(self, tmp_path, monkeypatch):
        self._models(tmp_path, monkeypatch, {"m": {"wire_format": "xml_fc"}})
        assert dialect_for_model("m") is None
        with pytest.raises(DialectUnbound):
            resolve_dialect("m")

    def test_new_key_used_regardless_of_old(self, tmp_path, monkeypatch):
        self._models(
            tmp_path,
            monkeypatch,
            {"m": {"dialect": "json_fc", "wire_format": "xml_fc"}},
        )
        assert dialect_for_model("m") == "json_fc"
        assert resolve_dialect("m").name == "json_fc"

    def test_empty_new_key_does_not_fall_back_to_old(self, tmp_path, monkeypatch):
        self._models(
            tmp_path, monkeypatch, {"m": {"dialect": "", "wire_format": "xml_fc"}}
        )
        assert dialect_for_model("m") is None

    def test_subagent_spawn_refused_on_old_key_only(self, tmp_path, monkeypatch):
        # 보드가 옛 키만 가진 엔트리를 "미설정" 으로 보여 주는 것과 같은 판정
        from agent_cli.subagent.runner import create_subagent_ctx

        self._models(tmp_path, monkeypatch, {"m": {"wire_format": "xml_fc"}})
        ctx, error = create_subagent_ctx("none", None, tmp_path / "sub", model="m")
        assert ctx is None
        assert "No dialect for model 'm'" in error
