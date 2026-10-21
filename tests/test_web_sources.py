"""정보원 목록 (v10.57.0) — 기본 목록 + 바꾼 것만 저장, 꼬리 ``## Web Sources``."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from agent_cli import web_sources as ws
from agent_cli.prompts.session_state import build_session_state


@pytest.fixture
def f(tmp_path) -> Path:
    return tmp_path / ".agent-cli" / ws.FILE_NAME


def _ids(rows, enabled=None):
    return [r["id"] for r in rows if enabled is None or r["enabled"] == enabled]


class TestDefaults:
    def test_no_file_means_every_default_is_on(self, f):
        rows = ws.entries(f)
        assert rows and all(r["enabled"] and r["builtin"] for r in rows)
        assert {"search.engines", "vulns.nvd", "wifi.hostap"} <= set(_ids(rows))

    def test_block_opens_with_the_header_and_names_each_group_once(self, f):
        block = ws.render(f)
        assert block.startswith("## Web Sources\nWeb sources that answer fetch")
        assert block.count("\n  wifi    ") == 1 and block.count("\n  vulns   ") == 1
        assert "\n          services.nvd.nist.gov" in block  # 같은 그룹 둘째 줄

    def test_multi_line_entry_keeps_the_column(self, f):
        block = ws.render(f)
        assert "\n          — engines often answer automated requests" in block
        assert "\n            /+log/refs/heads/" in block  # 항목 자체의 들여쓰기 보존

    def test_each_row_reports_tokens(self, f):
        assert all(r["tokens"] > 0 for r in ws.entries(f))


class TestOverrides:
    def test_disabled_default_drops_out(self, f):
        ws.save(["vulns.nvd"], [], f)
        assert "nvd.nist.gov" not in ws.render(f)
        assert "vulns.nvd" in _ids(ws.entries(f), enabled=False)

    def test_file_keeps_only_what_changed(self, f):
        ws.save(
            ["vulns.nvd"], [{"id": "user-1", "group": "wifi", "text": "x.corp/g"}], f
        )
        assert json.loads(f.read_text()) == {
            "disabled": ["vulns.nvd"],
            "added": [{"id": "user-1", "group": "wifi", "text": "x.corp/g"}],
        }

    def test_back_to_defaults_removes_the_file(self, f):
        ws.save(["vulns.nvd"], [], f)
        ws.save([], [], f)
        assert not f.exists()

    def test_added_entry_joins_its_group(self, f):
        ws.save([], [{"id": "user-1", "group": "wifi", "text": "gerrit.corp/x"}], f)
        rows = ws.entries(f)
        wifi = [r["id"] for r in rows if r["group"] == "wifi"]
        assert wifi[-1] == "user-1" and not rows[_ids(rows).index("user-1")]["builtin"]
        assert "\n          gerrit.corp/x" in ws.render(f)

    def test_new_group_goes_last(self, f):
        ws.save([], [{"id": "user-1", "group": "infra", "text": "status.corp/api"}], f)
        assert ws.entries(f)[-1]["group"] == "infra"
        assert ws.render(f).endswith("\n  infra   status.corp/api")

    def test_added_entry_can_be_disabled(self, f):
        ws.save(["user-1"], [{"id": "user-1", "group": "g", "text": "t"}], f)
        assert "user-1" in _ids(ws.entries(f), enabled=False)

    def test_new_default_in_a_later_version_shows_up(self, f, monkeypatch):
        ws.save(["vulns.nvd"], [], f)
        d = json.loads(json.dumps(ws._defaults()))
        d["sources"].append({"id": "new.one", "group": "data", "text": "new.api/x"})
        monkeypatch.setattr(ws, "_defaults", lambda: d)
        assert "new.one" in _ids(ws.entries(f), enabled=True)

    def test_unknown_disabled_ids_are_not_stored(self, f):
        ws.save(["gone.id", "vulns.nvd"], [], f)
        assert json.loads(f.read_text())["disabled"] == ["vulns.nvd"]

    @pytest.mark.parametrize(
        "bad",
        [
            {"id": "", "group": "g", "text": "t"},
            {"id": "a", "group": "", "text": "t"},
            {"id": "a", "group": "g", "text": ""},
            {"id": "a", "group": "g" * 13, "text": "t"},
            {"id": "a", "group": "g", "text": "x" * 501},
            {"id": "wiki.summary", "group": "g", "text": "t"},
        ],
    )
    def test_bad_added_entry_is_refused(self, f, bad):
        with pytest.raises(ValueError):
            ws.save([], [bad], f)

    def test_broken_file_falls_back_to_defaults(self, f):
        f.parent.mkdir(parents=True)
        f.write_text("{not json")
        assert all(r["enabled"] for r in ws.entries(f))

    def test_everything_off_renders_nothing(self, f):
        ws.save([s["id"] for s in ws._defaults()["sources"]], [], f)
        assert ws.render(f) == ""


class TestTail:
    def test_block_sits_in_session_state(self):
        out = build_session_state(turn=1, max_turns=9, web_sources="## Web Sources\nx")
        assert out.index("── session state") < out.index("## Web Sources")

    def _loop(self, tools):
        from agent_cli.loop import AgentLoop
        from agent_cli.providers.capabilities import ModelCapabilities
        from tests.loop_ports import make_ports

        return AgentLoop(
            query="q",
            provider=MagicMock(),
            capabilities=ModelCapabilities(
                context_window=32768, max_output_tokens=4096, supports_thinking=False
            ),
            model="m",
            active_tools=tools,
            ports=make_ports(),
        )

    def test_loop_with_fetch_carries_the_block(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        tail = self._loop(["read_file", "fetch"])._llm._build_session_state(30_000)
        assert "## Web Sources" in tail

    def test_loop_without_fetch_has_no_block(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        tail = self._loop(["read_file", "shell"])._llm._build_session_state(30_000)
        assert "## Web Sources" not in tail

    def test_change_on_disk_applies_next_turn(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        lp = self._loop(["fetch"])
        assert "nvd.nist.gov" in lp._llm._build_session_state(30_000)
        ws.save(["vulns.nvd"], [])
        assert "nvd.nist.gov" not in lp._llm._build_session_state(30_000)


def test_save_replaces_the_file_atomically_and_leaves_no_temp(tmp_path):
    """v10.61.2: 임시 파일 + os.replace — 쓰는 도중 빈 파일이 보이지 않는다."""
    from agent_cli.web_sources import save

    f = tmp_path / "web_sources.json"
    f.write_text("{garbage")  # 종전 내용이 무엇이든 교체 뒤엔 완전한 JSON
    data = save(["vulns.nvd"], [], path=f)
    assert json.loads(f.read_text()) == data
    assert [p.name for p in tmp_path.iterdir()] == ["web_sources.json"]
