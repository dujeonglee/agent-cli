"""``agent-cli mcp`` 마법사 (agent_cli/mcp/wizard.py, v9.1.0 — docs/mcp-ui 시안).

두 층: 순수 헬퍼(파일 I/O·프로브·env 검사)는 프롬프트 없이, ``McpWizard`` 는
rich 프롬프트를 스크립트로 몽키패치해 대화 흐름을 검사한다. 실제 MCP 서버는
띄우지 않는다 — ``probe_server`` 를 패치하고, 프로브 자체는 매니저를 가짜로
바꿔 검사한다.
"""

from __future__ import annotations

import json
from collections import deque
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from agent_cli.mcp import wizard as W

# ── 순수 헬퍼 ──────────────────────────────────────────


class TestFileIO:
    def test_read_missing_is_empty(self, tmp_path):
        assert W.read_servers(tmp_path / "nope.json") == {}

    def test_read_broken_json_is_empty(self, tmp_path):
        f = tmp_path / "mcp.json"
        f.write_text("{not json")
        assert W.read_servers(f) == {}

    def test_read_skips_non_dict_entries(self, tmp_path):
        f = tmp_path / "mcp.json"
        f.write_text(json.dumps({"mcpServers": {"a": {"command": "x"}, "b": "junk"}}))
        assert W.read_servers(f) == {"a": {"command": "x"}}

    def test_save_creates_file_and_parent(self, tmp_path):
        f = tmp_path / ".agent-cli" / "mcp.json"
        W.save_server("gh", {"command": "npx", "args": ["-y", "pkg"]}, f)
        assert json.loads(f.read_text())["mcpServers"]["gh"]["args"] == ["-y", "pkg"]

    def test_save_preserves_other_servers_and_top_level_keys(self, tmp_path):
        """다른 서버·다른 최상위 키를 날리면 사용자가 손으로 넣은 설정이 사라진다."""
        f = tmp_path / "mcp.json"
        f.write_text(
            json.dumps({"mcpServers": {"a": {"command": "x"}}, "note": "keep"})
        )
        W.save_server("b", {"url": "http://h"}, f)
        data = json.loads(f.read_text())
        assert set(data["mcpServers"]) == {"a", "b"}
        assert data["note"] == "keep"

    def test_save_overwrites_same_name(self, tmp_path):
        f = tmp_path / "mcp.json"
        W.save_server("a", {"command": "old"}, f)
        W.save_server("a", {"command": "new"}, f)
        assert W.read_servers(f)["a"]["command"] == "new"

    def test_remove(self, tmp_path):
        f = tmp_path / "mcp.json"
        W.save_server("a", {"command": "x"}, f)
        W.save_server("b", {"command": "y"}, f)
        assert W.remove_server("a", f) is True
        assert W.read_servers(f) == {"b": {"command": "y"}}

    def test_remove_missing_is_false_and_no_write(self, tmp_path):
        f = tmp_path / "mcp.json"
        assert W.remove_server("ghost", f) is False
        assert not f.exists()

    def test_paths_follow_scope_rules(self, tmp_path, monkeypatch):
        """v9.0.0: 저장은 프로젝트, 레거시 조회만 유저."""
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
        assert W.mcp_json_path() == tmp_path / ".agent-cli" / "mcp.json"
        assert W.legacy_user_mcp_path() == tmp_path / "home" / ".agent-cli" / "mcp.json"


class TestEnvAndMasking:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("", ""),
            ("abc", "•••"),
            ("12345678", "••••••••"),
            ("ghp_abcdefghij4f2a", "ghp_••••••••4f2a"),
        ],
    )
    def test_mask(self, raw, expected):
        assert W.mask_secret(raw) == expected

    def test_env_ref_present(self, monkeypatch):
        monkeypatch.setenv("GH", "ghp_abcdefghij4f2a")
        assert W.env_ref_status("${GH}") == ("GH", True, "ghp_••••••••4f2a")

    def test_env_ref_missing_is_flagged(self, monkeypatch):
        """A 시안의 오타 케이스 — 미정의는 빈 문자열이 되어 인증만 실패하므로
        여기서 잡아야 한다."""
        monkeypatch.delenv("GITHUB_TOKN", raising=False)
        var, present, _ = W.env_ref_status("${GITHUB_TOKN}")
        assert var == "GITHUB_TOKN" and present is False

    def test_literal_value_is_not_a_ref(self):
        assert W.env_ref_status("literal-token") == (None, True, "")

    def test_resolve_command(self):
        assert W.resolve_command("python3") is not None
        assert W.resolve_command("definitely-not-a-cmd-xyz") is None
        assert W.resolve_command("") is None


class TestProbe:
    def _fake_manager(self, status: str, tools=()):
        m = MagicMock()
        m.connect_all.return_value = {"s": status}
        m.list_tools.return_value = [MagicMock(name=t) for t in tools]
        for i, t in enumerate(tools):
            m.list_tools.return_value[i].name = t
        return m

    def test_invalid_entry_fails_fast(self):
        ok, msg, tools, _ = W.probe_server("s", {})
        assert ok is False and "command" in msg and tools == []

    def test_missing_executable_fails_before_connect(self):
        """PATH 에 없으면 서버를 띄우려 들지도 않는다."""
        with patch("agent_cli.mcp.client.McpClientManager") as M:
            ok, msg, _, _ = W.probe_server("s", {"command": "no-such-cmd-xyz"})
        assert ok is False and "PATH" in msg
        M.assert_not_called()

    def test_connected_reports_tools_and_disconnects(self):
        m = self._fake_manager("connected", ["list_issues", "create_pr"])
        with patch("agent_cli.mcp.client.McpClientManager", return_value=m):
            ok, _msg, tools, secs = W.probe_server("s", {"command": "python3"})
        assert ok is True and tools == ["list_issues", "create_pr"]
        assert secs >= 0
        m.disconnect_all.assert_called_once()  # 마법사가 띄운 프로세스를 남기지 않는다

    @pytest.mark.parametrize(
        "status,needle",
        [
            ("error: [Errno 2] No such file or directory: 'x'", "찾을 수 없습니다"),
            ("error: Connection refused", "연결 거부"),
            ("error: No module named 'mcp'", "pip install mcp"),
            ("error: something odd", "something odd"),
        ],
    )
    def test_error_messages_say_what_to_do(self, status, needle):
        m = self._fake_manager(status)
        with patch("agent_cli.mcp.client.McpClientManager", return_value=m):
            ok, msg, _, _ = W.probe_server("s", {"url": "http://h:1"})
        assert ok is False and needle in msg
        m.disconnect_all.assert_called_once()

    def test_timeout_is_bounded(self):
        """initialize() 에 타임아웃이 없어 응답 없는 서버가 마법사를 영원히
        세운다 — 프로브가 상한을 건다."""
        import threading

        m = MagicMock()
        m.connect_all.side_effect = lambda c: threading.Event().wait()  # never returns
        with patch("agent_cli.mcp.client.McpClientManager", return_value=m):
            ok, msg, _, secs = W.probe_server("s", {"url": "http://h:1"}, timeout=0.2)
        assert ok is False and "응답이 없습니다" in msg
        assert 0.15 <= secs < 2.0


# ── 대화형 ──────────────────────────────────────────────


class _Script:
    """rich 프롬프트에 순서대로 답한다. 남거나 모자라면 실패 — 흐름이 바뀐 걸
    바로 드러내기 위해."""

    def __init__(self, answers):
        self.q = deque(answers)
        self.asked: list[str] = []

    def __call__(self, prompt, **kw):
        self.asked.append(str(prompt))
        assert self.q, f"answers exhausted at prompt: {prompt!r}"
        return self.q.popleft()


@pytest.fixture
def proj(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
    (tmp_path / "home" / ".agent-cli").mkdir(parents=True)
    return tmp_path


def _wizard():
    from io import StringIO

    from rich.console import Console

    buf = StringIO()
    # 폭을 크게 — 긴 경로·메시지가 줄바꿈되면 문자열 매칭이 깨진다
    return W.McpWizard(Console(file=buf, force_terminal=False, width=400)), buf


def _script_prompts(monkeypatch, *, prompt=(), intp=(), confirm=()):
    p, i, c = _Script(prompt), _Script(intp), _Script(confirm)
    monkeypatch.setattr(W.Prompt, "ask", p)
    monkeypatch.setattr(W.IntPrompt, "ask", i)
    monkeypatch.setattr(W.Confirm, "ask", c)
    return p, i, c


class TestAddFlow:
    def test_stdio_happy_path_saves_after_probe(self, proj, monkeypatch):
        """이름 → stdio → 명령/인자 → env(GH 있음) → 프로브 OK → 저장."""
        monkeypatch.setenv("GH", "ghp_abcdefghij4f2a")
        _p, _i, _c = _script_prompts(
            monkeypatch,
            prompt=["github", "python3", "-y pkg", "GH", "${GH}", ""],
            intp=[1],
        )
        with patch.object(
            W, "probe_server", return_value=(True, "연결됨", ["a", "b"], 0.5)
        ) as pr:
            w, buf = _wizard()
            assert w.add() is True
        saved = W.read_servers(proj / ".agent-cli" / "mcp.json")
        assert saved == {
            "github": {
                "command": "python3",
                "args": ["-y", "pkg"],
                "env": {"GH": "${GH}"},
            }
        }
        pr.assert_called_once()
        out = buf.getvalue()
        assert "GH 있음" in out and "ghp_••••••••4f2a" in out
        assert "도구 2개" in out and "다음 실행부터" in out

    def test_sse_sets_transport_explicitly(self, proj, monkeypatch):
        _script_prompts(
            monkeypatch, prompt=["figma", "http://localhost:3845", ""], intp=[2]
        )
        with patch.object(W, "probe_server", return_value=(True, "", [], 0.1)):
            w, _ = _wizard()
            assert w.add()
        saved = W.read_servers(proj / ".agent-cli" / "mcp.json")["figma"]
        assert saved == {"url": "http://localhost:3845", "transport": "sse"}

    def test_missing_env_is_flagged_but_allowed(self, proj, monkeypatch):
        """오타 변수 — 경고는 하되 막지는 않는다(값이 나중에 생길 수 있다)."""
        monkeypatch.delenv("GITHUB_TOKN", raising=False)
        _script_prompts(
            monkeypatch,
            prompt=["gh", "python3", "", "GITHUB_TOKN", "${GITHUB_TOKN}", ""],
            intp=[1],
        )
        with patch.object(W, "probe_server", return_value=(True, "", [], 0.1)):
            w, buf = _wizard()
            assert w.add()
        assert "GITHUB_TOKN 가 없습니다" in buf.getvalue()
        assert "빈 문자열" in buf.getvalue()

    def test_probe_failure_then_cancel_writes_nothing(self, proj, monkeypatch):
        """붙지 않는 설정은 파일에 남지 않는다 — 시안 6단계."""
        _script_prompts(monkeypatch, prompt=["bad", "python3", "", ""], intp=[1, 3])
        with patch.object(
            W,
            "probe_server",
            return_value=(False, "서버가 응답하지 않습니다", [], 10.0),
        ):
            w, buf = _wizard()
            assert w.add() is False
        assert not (proj / ".agent-cli" / "mcp.json").exists()
        assert "취소" in buf.getvalue()

    def test_probe_failure_then_save_anyway(self, proj, monkeypatch):
        _script_prompts(monkeypatch, prompt=["bad", "python3", "", ""], intp=[1, 2])
        with patch.object(W, "probe_server", return_value=(False, "x", [], 1.0)):
            w, _ = _wizard()
            assert w.add() is True
        assert "bad" in W.read_servers(proj / ".agent-cli" / "mcp.json")

    def test_probe_failure_then_fix_reruns_from_transport(self, proj, monkeypatch):
        """[1] 설정 고치기 — 이름은 유지하고 전송 방식부터 다시."""
        p, _i, _ = _script_prompts(
            monkeypatch,
            prompt=["gh", "wrong-cmd", "", "", "python3", "", ""],
            intp=[1, 1, 1],  # stdio · 고치기 · stdio
        )
        with patch.object(
            W,
            "probe_server",
            side_effect=[(False, "no", [], 1.0), (True, "", ["t"], 0.2)],
        ) as pr:
            w, _ = _wizard()
            assert w.add() is True
        assert pr.call_count == 2
        assert (
            W.read_servers(proj / ".agent-cli" / "mcp.json")["gh"]["command"]
            == "python3"
        )
        assert p.asked.count("   서버 이름") == 1  # 이름은 다시 묻지 않는다

    def test_name_with_dot_rejected(self, proj, monkeypatch):
        """도구 이름이 {서버}.{도구} 라 점은 파싱을 깨뜨린다."""
        # name(거부) · name · command · args · env(빈 줄)
        _script_prompts(
            monkeypatch, prompt=["bad.name", "ok", "python3", "", ""], intp=[1]
        )
        with patch.object(W, "probe_server", return_value=(True, "", [], 0.1)):
            w, buf = _wizard()
            assert w.add()
        assert "점(.)" in buf.getvalue()
        assert "ok" in W.read_servers(proj / ".agent-cli" / "mcp.json")

    def test_existing_name_asks_before_overwrite(self, proj, monkeypatch):
        W.save_server("gh", {"command": "old"}, proj / ".agent-cli" / "mcp.json")
        _script_prompts(monkeypatch, prompt=["gh"], confirm=[False])
        w, _ = _wizard()
        assert w.add() is False
        assert (
            W.read_servers(proj / ".agent-cli" / "mcp.json")["gh"]["command"] == "old"
        )


class TestLegacyImport:
    """v9.0.0 스코프 정리는 경고 없이 갔다 — 마법사가 ~/.agent-cli/mcp.json 을
    발견하면 가져올지 묻는 것이 유일한 마이그레이션 경로."""

    def test_offers_import_and_copies(self, proj, monkeypatch):
        legacy = proj / "home" / ".agent-cli" / "mcp.json"
        legacy.write_text(json.dumps({"mcpServers": {"old": {"command": "x"}}}))
        monkeypatch.setattr(
            W.sys.stdin, "isatty", lambda: True
        )  # 목록은 TTY 에서만 제안
        _script_prompts(monkeypatch, confirm=[True])
        w, buf = _wizard()
        with patch.object(W, "probe_server", return_value=(True, "", [], 0.1)):
            w.list()
        assert W.read_servers(proj / ".agent-cli" / "mcp.json") == {
            "old": {"command": "x"}
        }
        assert legacy.exists()  # 원본은 지우지 않는다
        assert "읽지 않습니다" in buf.getvalue()

    def test_declined_import_leaves_project_untouched(self, proj, monkeypatch):
        legacy = proj / "home" / ".agent-cli" / "mcp.json"
        legacy.write_text(json.dumps({"mcpServers": {"old": {"command": "x"}}}))
        monkeypatch.setattr(
            W.sys.stdin, "isatty", lambda: True
        )  # 목록은 TTY 에서만 제안
        _script_prompts(monkeypatch, confirm=[False])
        w, _ = _wizard()
        w.list(test=False)
        assert not (proj / ".agent-cli" / "mcp.json").exists()

    def test_only_offers_servers_not_already_in_project(self, proj, monkeypatch):
        """이미 프로젝트에 있는 이름은 제안하지 않는다 — 덮어쓰기 사고 방지."""
        legacy = proj / "home" / ".agent-cli" / "mcp.json"
        legacy.write_text(json.dumps({"mcpServers": {"gh": {"command": "OLD"}}}))
        monkeypatch.setattr(W.sys.stdin, "isatty", lambda: True)
        W.save_server("gh", {"command": "NEW"}, proj / ".agent-cli" / "mcp.json")
        _, _, c = _script_prompts(monkeypatch, confirm=[])
        w, _ = _wizard()
        w.list(test=False)
        assert c.asked == []  # 묻지도 않았다
        assert (
            W.read_servers(proj / ".agent-cli" / "mcp.json")["gh"]["command"] == "NEW"
        )


class TestListTestRemove:
    def test_list_reports_failures_with_reason_and_path(self, proj, monkeypatch):
        f = proj / ".agent-cli" / "mcp.json"
        W.save_server("ok", {"command": "python3"}, f)
        W.save_server("bad", {"url": "http://h:1"}, f)
        results = {
            "ok": (True, "", ["a"] * 26, 1.8),
            "bad": (False, "연결 거부 — http://h:1", [], 0.1),
        }
        with patch.object(W, "probe_server", side_effect=lambda n, e, **k: results[n]):
            w, buf = _wizard()
            failures = w.list()
        out = buf.getvalue()
        assert failures == 1
        assert "26" in out and "연결 거부" in out and str(f) in out

    def test_list_no_test_skips_probe(self, proj, monkeypatch):
        W.save_server("a", {"command": "python3"}, proj / ".agent-cli" / "mcp.json")
        with patch.object(W, "probe_server") as pr:
            w, buf = _wizard()
            assert w.list(test=False) == 0
        pr.assert_not_called()
        assert "미확인" in buf.getvalue()

    def test_list_empty_points_to_add(self, proj):
        w, buf = _wizard()
        assert w.list() == 0
        assert "agent-cli mcp add" in buf.getvalue()

    def test_test_unknown_name_hints_registered(self, proj):
        W.save_server("real", {"command": "python3"}, proj / ".agent-cli" / "mcp.json")
        w, buf = _wizard()
        assert w.test("ghost") is False
        assert "등록됨: real" in buf.getvalue()

    def test_remove_and_remove_unknown(self, proj):
        W.save_server("a", {"command": "python3"}, proj / ".agent-cli" / "mcp.json")
        w, _buf = _wizard()
        assert w.remove("a") is True
        assert w.remove("a") is False
        assert W.read_servers(proj / ".agent-cli" / "mcp.json") == {}


class TestCliWiring:
    def _opts(self, *path):
        from typer.main import get_command

        from agent_cli.main import app

        cmd = get_command(app)
        for p in path:
            cmd = cmd.commands[p]
        return cmd

    def test_subcommands_registered(self):
        mcp = self._opts("mcp")
        assert set(mcp.commands) == {"add", "test", "remove"}

    def test_root_has_no_test_flag(self):
        mcp = self._opts("mcp")
        assert "--no-test" in {o for p in mcp.params for o in getattr(p, "opts", [])}

    def test_root_exit_code_reflects_failures(self, proj, monkeypatch):
        """스크립트에서 `agent-cli mcp && ...` 로 쓸 수 있게."""
        from typer.testing import CliRunner

        from agent_cli.main import app

        W.save_server("bad", {"url": "http://h:1"}, proj / ".agent-cli" / "mcp.json")
        with patch.object(W, "probe_server", return_value=(False, "x", [], 0.1)):
            r = CliRunner().invoke(app, ["mcp"])
        assert r.exit_code == 1
        with patch.object(W, "probe_server", return_value=(True, "", [], 0.1)):
            r = CliRunner().invoke(app, ["mcp"])
        assert r.exit_code == 0


class TestListIsScriptSafe:
    def test_list_does_not_prompt_without_tty(self, proj, monkeypatch):
        """실장에서 잡힌 것: `agent-cli mcp` 가 파이프 뒤에서 가져오기 프롬프트에
        걸려 영원히 멈췄다. 진단 명령은 stdin 이 TTY 가 아니면 묻지 않는다."""
        legacy = proj / "home" / ".agent-cli" / "mcp.json"
        legacy.write_text(json.dumps({"mcpServers": {"old": {"command": "x"}}}))
        monkeypatch.setattr(W.sys.stdin, "isatty", lambda: False)
        _, _, c = _script_prompts(monkeypatch, confirm=[])
        w, _ = _wizard()
        w.list(test=False)
        assert c.asked == []
        assert not (proj / ".agent-cli" / "mcp.json").exists()

    def test_add_still_offers_import_without_tty(self, proj, monkeypatch):
        """add 는 대화형이라 파이프 입력으로도 가져오기를 제안한다 — E2E 파이프
        테스트가 그 경로에 의존한다."""
        legacy = proj / "home" / ".agent-cli" / "mcp.json"
        legacy.write_text(json.dumps({"mcpServers": {"old": {"command": "x"}}}))
        monkeypatch.setattr(W.sys.stdin, "isatty", lambda: False)
        _script_prompts(
            monkeypatch, prompt=["n2", "python3", "", ""], intp=[1], confirm=[True]
        )
        with patch.object(W, "probe_server", return_value=(True, "", [], 0.1)):
            w, _ = _wizard()
            assert w.add()
        assert set(W.read_servers(proj / ".agent-cli" / "mcp.json")) == {"old", "n2"}
