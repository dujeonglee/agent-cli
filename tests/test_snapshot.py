"""워크스페이스 git 스냅샷 (v10.36.0, docs/snapshot/DESIGN.md) — 실제 git 으로.

고정하는 계약: 워크스페이스에 흔적 없음 · `.gitignore` 무시 · 우리 제외 목록은 pathspec
(`--force` 가 info/exclude 를 무시하는 git 의 성질) · 변경 없으면 커밋 없음 · 삭제·훼손
뒤 복원(`restore` 는 "지금과 다른 가장 최근 판") · 하위 저장소는 gitlink + 기록 ·
사용자 전역 설정 차단 · git 없음 처리 · 노브 · 내장 훅의 안내는 성공 시에만.
"""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from agent_cli.snapshot import GitSnapshot, get_snapshot, git_available, reset_snapshot

pytestmark = pytest.mark.skipif(not git_available()[0], reason="git not installed")


@pytest.fixture
def ws(tmp_path):
    w = tmp_path / "ws"
    w.mkdir()
    (w / "main.db").write_bytes(b"base")
    (w / "main.db-wal").write_bytes(b"wal-bytes")
    (w / "src").mkdir()
    (w / "src" / "a.py").write_text("print(1)\n")
    return w


@pytest.fixture(autouse=True)
def _reset():
    reset_snapshot()
    yield
    reset_snapshot()


def _store(ws, **kw) -> GitSnapshot:
    s = GitSnapshot(ws, min_interval_s=0, **kw)
    assert s.init(), s.reason
    return s


def _files(ws: Path) -> set[str]:
    return {
        str(p.relative_to(ws))
        for p in ws.rglob("*")
        if p.is_file() and ".agent-cli" not in p.parts
    }


class TestShadowRepo:
    def test_leaves_no_trace_in_the_workspace(self, ws):
        before = _files(ws)
        s = _store(ws)
        assert s.head() is not None
        assert not (ws / ".git").exists()
        assert _files(ws) == before
        assert (ws / ".agent-cli" / "snapshot.git" / "HEAD").exists()

    def test_gitignore_in_the_workspace_is_ignored(self, ws):
        """보호해야 할 `*.db` 가 `.gitignore` 에 적혀 있기 일쑤다."""
        (ws / ".gitignore").write_text("*.db\n*.db-wal\nsrc/\n")
        s = _store(ws)
        listed = s._git("ls-tree", "-r", "--name-only", "HEAD").stdout.split()
        assert "main.db" in listed and "main.db-wal" in listed and "src/a.py" in listed

    def test_our_excludes_hold_even_with_force(self, ws):
        """`add --force` 는 info/exclude 까지 무시한다 — 제외는 pathspec 으로."""
        (ws / "node_modules" / "x").mkdir(parents=True)
        (ws / "node_modules" / "x" / "big.js").write_text("x")
        (ws / "src" / "__pycache__").mkdir()
        (ws / "src" / "__pycache__" / "a.pyc").write_bytes(b"\x00")
        s = _store(ws)
        listed = s._git("ls-tree", "-r", "--name-only", "HEAD").stdout.split()
        assert not any(p.startswith(".agent-cli") for p in listed), listed
        assert not any("node_modules" in p for p in listed), listed
        assert not any("__pycache__" in p for p in listed), listed
        assert "src/a.py" in listed

    def test_no_commit_when_nothing_changed(self, ws):
        s = _store(ws)
        h = s.head()
        assert s.checkpoint("again") is None
        assert s.head() == h
        (ws / "src" / "a.py").write_text("print(2)\n")
        assert s.checkpoint("changed") not in (None, h)

    def test_min_interval_gates_turn_checkpoints_but_not_forced_ones(self, ws):
        s = GitSnapshot(ws, min_interval_s=3600)
        assert s.init()
        (ws / "new.txt").write_text("n")
        assert s.checkpoint("too soon") is None
        assert s.checkpoint("forced", force=True) is not None

    def test_user_global_git_config_is_blocked(self, ws, tmp_path, monkeypatch):
        """gpgsign 이 켜진 전역 설정이 있어도 커밋된다 — 하니스 내부 커밋은 사용자
        설정을 보지 않는다."""
        cfg = tmp_path / "gitconfig"
        cfg.write_text("[commit]\n\tgpgsign = true\n[user]\n\tname = nobody\n")
        monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(cfg))
        monkeypatch.setenv("HOME", str(tmp_path))
        s = _store(ws)
        author = s._git("log", "-1", "--format=%an <%ae>").stdout.strip()
        assert author == "agent-cli snapshot <snapshot@agent-cli>"


class TestRestore:
    def test_deleted_file_comes_back(self, ws):
        s = _store(ws)
        (ws / "main.db-wal").unlink()
        s.checkpoint("after damage", force=True)  # 망가진 상태가 HEAD 가 돼도
        ok, msg = s.restore("main.db-wal")
        assert ok, msg
        assert (ws / "main.db-wal").read_bytes() == b"wal-bytes"

    def test_damaged_in_place_file_gets_the_previous_version(self, ws):
        """HEAD 가 망가진 판이면 그 전 판 — "지금 디스크와 다른 가장 최근 스냅샷"."""
        s = _store(ws)
        (ws / "main.db-wal").write_bytes(b"corrupted")
        s.checkpoint("after damage", force=True)
        ok, _ = s.restore("main.db-wal")
        assert ok
        assert (ws / "main.db-wal").read_bytes() == b"wal-bytes"

    def test_explicit_commit(self, ws):
        s = _store(ws)
        first = s.head()
        (ws / "src" / "a.py").write_text("v2\n")
        s.checkpoint("v2", force=True)
        (ws / "src" / "a.py").write_text("v3\n")
        s.checkpoint("v3", force=True)
        ok, _ = s.restore("src/a.py", at=first)
        assert ok and (ws / "src" / "a.py").read_text() == "print(1)\n"

    def test_unknown_path_is_a_clean_failure(self, ws):
        s = _store(ws)
        ok, msg = s.restore("never-existed.txt")
        assert not ok and "no snapshot" in msg

    def test_changed_files_and_log(self, ws):
        s = _store(ws)
        (ws / "src" / "a.py").write_text("v2\n")
        (ws / "gone.txt").write_text("x")
        s.checkpoint("edit", force=True)
        (ws / "gone.txt").unlink()
        s.checkpoint("delete", force=True)
        assert ("D", "gone.txt") in s.changed_files()
        assert [m for _, m in s.log()][:2] == ["delete", "edit"]


class TestNestedRepos:
    def test_nested_repo_is_recorded_not_stored(self, ws):
        sub = ws / "lib"
        sub.mkdir()
        env = {
            **os.environ,
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@t",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@t",
        }
        subprocess.run(["git", "init", "-q"], cwd=sub, env=env, check=True)
        (sub / "tracked.txt").write_text("t")
        subprocess.run(["git", "add", "."], cwd=sub, env=env, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "i"], cwd=sub, env=env, check=True)
        (sub / "tracked.txt").write_text("dirty")
        (sub / "untracked.txt").write_text("u")
        s = _store(ws)
        assert [n.path for n in s.nested] == ["lib"]
        assert set(s.nested[0].dirty) == {"tracked.txt", "untracked.txt"}
        assert s.nested[0].head != "(no commits)"
        modes = s._git("ls-tree", "HEAD").stdout
        assert "160000" in modes and "lib" in modes  # gitlink
        assert "nested" in s.notice().lower() and "`lib`" in s.notice()


class TestAvailability:
    def test_git_missing_disables_with_a_reason(self, ws, monkeypatch):
        monkeypatch.setattr("agent_cli.snapshot.shutil.which", lambda _n: None)
        s = GitSnapshot(ws)
        assert not s.init()
        assert not s.enabled and "git not found" in s.reason
        assert s.checkpoint("x") is None
        assert not (ws / ".agent-cli").exists()

    def test_session_singleton_per_workspace(self, ws, tmp_path):
        a = get_snapshot(ws, create=True)
        assert a is get_snapshot(ws)
        assert get_snapshot(tmp_path / "other") is None


class TestCaps:
    def test_file_size_cap_excludes_only_the_big_file(self, ws):
        (ws / "huge.bin").write_bytes(b"x" * 2048)
        s = _store(ws, file_max_bytes=1024)
        listed = s._git("ls-tree", "-r", "--name-only", "HEAD").stdout.split()
        assert "huge.bin" not in listed and "main.db" in listed

    def test_file_count_cap_turns_off_per_turn_checkpoints(self, ws):
        s = _store(ws, file_count_max=1)
        assert s.head() is not None  # 시작 스냅샷은 찍는다
        assert s.per_turn is False
        (ws / "n.txt").write_text("n")
        assert s.checkpoint("turn") is None
        assert s.checkpoint("end", force=True) is not None


class TestKnobs:
    def test_defaults_and_env(self, monkeypatch):
        from agent_cli.constants import snapshot_knobs

        for k in (
            "AGENT_CLI_SNAPSHOT",
            "AGENT_CLI_SNAPSHOT_MIN_INTERVAL_S",
            "AGENT_CLI_SNAPSHOT_FILE_MAX_BYTES",
            "AGENT_CLI_SNAPSHOT_FILE_COUNT_MAX",
        ):
            monkeypatch.delenv(k, raising=False)
        k = snapshot_knobs()
        assert k == {
            "enabled": True,
            "min_interval_s": 10.0,
            "file_max_bytes": -1,
            "file_count_max": -1,
        }
        monkeypatch.setenv("AGENT_CLI_SNAPSHOT", "0")
        monkeypatch.setenv("AGENT_CLI_SNAPSHOT_FILE_MAX_BYTES", "1024")
        monkeypatch.setenv("AGENT_CLI_SNAPSHOT_MIN_INTERVAL_S", "oops")
        k = snapshot_knobs()
        assert k["enabled"] is False and k["file_max_bytes"] == 1024
        assert k["min_interval_s"] == 10.0  # 오타는 기본값


class TestBuiltinHook:
    """저장소는 `runtime.build_snapshot_store` 가 세우고, 훅은 안내·체크포인트만."""

    def _hook(self):
        from agent_cli.hooks.loader import BUILTIN_HOOKS_DIR, _load_module

        mod = _load_module(BUILTIN_HOOKS_DIR / "10_snapshot.py")
        assert mod is not None
        return mod

    def test_notice_only_when_the_bootstrap_built_a_store(self, ws, monkeypatch):
        from agent_cli.runtime import build_snapshot_store

        monkeypatch.chdir(ws)
        monkeypatch.delenv("AGENT_CLI_SNAPSHOT", raising=False)
        hook = self._hook()
        ctx = MagicMock()
        hook.on_session_start(ctx)  # 부트스트랩 전 — 저장소 없음
        ctx.inject_system_section.assert_not_called()
        assert build_snapshot_store() is not None
        hook.on_session_start(ctx)
        ctx.inject_system_section.assert_called_once()
        title, body = ctx.inject_system_section.call_args.args
        assert title == "Workspace snapshot" and "`snapshot` tool" in body
        assert get_snapshot(ws).head() is not None

    def test_disabled_by_knob_means_no_store_no_notice(self, ws, monkeypatch):
        from agent_cli.runtime import build_snapshot_store

        monkeypatch.chdir(ws)
        monkeypatch.setenv("AGENT_CLI_SNAPSHOT", "0")
        assert build_snapshot_store() is None
        ctx = MagicMock()
        self._hook().on_session_start(ctx)
        ctx.inject_system_section.assert_not_called()
        assert not (ws / ".agent-cli").exists()

    def test_git_missing_means_no_store_and_a_human_line(self, ws, monkeypatch):
        from unittest.mock import patch

        from agent_cli.runtime import build_snapshot_store

        monkeypatch.chdir(ws)
        monkeypatch.delenv("AGENT_CLI_SNAPSHOT", raising=False)
        monkeypatch.setattr("agent_cli.snapshot.shutil.which", lambda _n: None)
        renderer = MagicMock()
        with patch("agent_cli.render.get_renderer", return_value=renderer):
            assert build_snapshot_store() is None
        assert "Snapshots off" in renderer.status.call_args.args[1]
        ctx = MagicMock()
        self._hook().on_session_start(ctx)
        ctx.inject_system_section.assert_not_called()

    def test_pre_tool_use_checkpoints_mutating_tools_only(self, ws, monkeypatch):
        from agent_cli.runtime import build_snapshot_store

        monkeypatch.chdir(ws)
        monkeypatch.delenv("AGENT_CLI_SNAPSHOT", raising=False)
        monkeypatch.setenv("AGENT_CLI_SNAPSHOT_MIN_INTERVAL_S", "0")
        store = build_snapshot_store()
        hook = self._hook()
        h0 = store.head()
        (ws / "src" / "a.py").write_text("v2\n")
        hook.pre_tool_use(MagicMock(tool_name="read_file", turn=3))
        assert store.head() == h0, "읽기 도구는 체크포인트를 안 찍는다"
        hook.pre_tool_use(MagicMock(tool_name="shell", turn=3))
        assert store.head() != h0
        (ws / "src" / "a.py").write_text("v3\n")
        hook.on_session_end(MagicMock(turn=9))
        assert next(m for _, m in store.log()).startswith("session end")

    @pytest.mark.parametrize("tool", ["edit_file", "write_file", "shell"])
    def test_each_mutating_tool_checkpoints(self, ws, monkeypatch, tool):
        from agent_cli.runtime import build_snapshot_store

        monkeypatch.chdir(ws)
        monkeypatch.delenv("AGENT_CLI_SNAPSHOT", raising=False)
        monkeypatch.setenv("AGENT_CLI_SNAPSHOT_MIN_INTERVAL_S", "0")
        store = build_snapshot_store()
        h0 = store.head()
        (ws / "src" / "a.py").write_text("changed\n")
        self._hook().pre_tool_use(MagicMock(tool_name=tool, turn=2))
        assert store.head() != h0, f"{tool} 직전에 체크포인트가 없다"
        assert next(m for _, m in store.log()) == f"before {tool} (turn 2)"

    @pytest.mark.parametrize(
        "tool", ["read_file", "code_index", "monitor", "agent", "fetch"]
    )
    def test_non_mutating_tools_do_not(self, ws, monkeypatch, tool):
        from agent_cli.runtime import build_snapshot_store

        monkeypatch.chdir(ws)
        monkeypatch.delenv("AGENT_CLI_SNAPSHOT", raising=False)
        monkeypatch.setenv("AGENT_CLI_SNAPSHOT_MIN_INTERVAL_S", "0")
        store = build_snapshot_store()
        h0 = store.head()
        (ws / "src" / "a.py").write_text("changed\n")
        self._hook().pre_tool_use(MagicMock(tool_name=tool, turn=2))
        assert store.head() == h0

    def test_a_batch_of_three_ops_within_the_interval_commits_once(
        self, ws, monkeypatch
    ):
        """한 턴의 shell·edit·write 배치 — 훅은 op 마다 불리지만 최소 간격 안이라 첫
        op 앞의 체크포인트 하나만 남는다(배치 직전 상태)."""
        from agent_cli.runtime import build_snapshot_store

        monkeypatch.chdir(ws)
        monkeypatch.delenv("AGENT_CLI_SNAPSHOT", raising=False)
        monkeypatch.setenv("AGENT_CLI_SNAPSHOT_MIN_INTERVAL_S", "3600")
        store = build_snapshot_store()
        # 시작 커밋 뒤 간격이 지났다고 친다 — monotonic 은 부팅 기준이라 0.0 으로
        # 두면 갓 켜진 CI 러너(uptime < 3600s)에서 "아직 간격 안" 이 된다.
        store._last_checkpoint = time.monotonic() - 3601
        n0 = store.stats.commits
        hook = self._hook()
        for tool in ("shell", "edit_file", "write_file"):
            (ws / "src" / "a.py").write_text(f"by {tool}\n")
            hook.pre_tool_use(MagicMock(tool_name=tool, turn=4))
        assert store.stats.commits == n0 + 1
        assert next(m for _, m in store.log()) == "before shell (turn 4)"

    def test_an_op_after_the_interval_commits_again(self, ws, monkeypatch):
        """간격이 지나면(긴 shell 뒤의 edit) 중간 상태도 남는다 — 간격은 비용 제한이지
        "턴당 하나" 가 아니다."""
        from agent_cli.runtime import build_snapshot_store

        monkeypatch.chdir(ws)
        monkeypatch.delenv("AGENT_CLI_SNAPSHOT", raising=False)
        monkeypatch.setenv("AGENT_CLI_SNAPSHOT_MIN_INTERVAL_S", "0.2")
        store = build_snapshot_store()
        store._last_checkpoint = (
            time.monotonic() - 1
        )  # 시작 커밋 뒤 간격이 지났다고 친다
        n0 = store.stats.commits
        hook = self._hook()
        (ws / "src" / "a.py").write_text("v2\n")
        hook.pre_tool_use(MagicMock(tool_name="shell", turn=4))
        (ws / "src" / "a.py").write_text("v3\n")
        hook.pre_tool_use(MagicMock(tool_name="edit_file", turn=4))  # 간격 안 — 건너뜀
        assert store.stats.commits == n0 + 1
        time.sleep(0.25)
        hook.pre_tool_use(MagicMock(tool_name="write_file", turn=4))  # 간격 지남
        assert store.stats.commits == n0 + 2
        assert [m for _, m in store.log()][:2] == [
            "before write_file (turn 4)",
            "before shell (turn 4)",
        ]

    def test_second_loop_reuses_the_store(self, ws, monkeypatch):
        """서브에이전트 루프의 OnSessionStart 가 또 와도 저장소는 하나, init 도 한 번."""
        from agent_cli.runtime import build_snapshot_store

        monkeypatch.chdir(ws)
        monkeypatch.delenv("AGENT_CLI_SNAPSHOT", raising=False)
        store = build_snapshot_store()
        n = store.stats.commits
        hook = self._hook()
        hook.on_session_start(MagicMock())
        hook.on_session_start(MagicMock())
        assert get_snapshot(ws) is store and store.stats.commits == n


class TestToolAdvertising:
    """`snapshot` 도구는 저장소가 섰을 때만 tools_list 에 남는다 (requires_handler)."""

    def _tools(self):
        from unittest.mock import MagicMock

        from agent_cli.loop import AgentLoop
        from agent_cli.providers.capabilities import ModelCapabilities
        from tests.loop_ports import split_ports

        caps = ModelCapabilities(
            context_window=4096, max_output_tokens=2048, supports_thinking=False
        )
        loop = AgentLoop(
            query="q",
            provider=MagicMock(),
            capabilities=caps,
            model="m",
            **split_ports({}),
        )
        return list(loop._config.tools_list)

    def test_absent_without_a_store(self, ws, monkeypatch):
        monkeypatch.chdir(ws)
        assert "snapshot" not in self._tools()

    def test_present_with_a_store(self, ws, monkeypatch):
        from agent_cli.runtime import build_snapshot_store

        monkeypatch.chdir(ws)
        monkeypatch.delenv("AGENT_CLI_SNAPSHOT", raising=False)
        assert build_snapshot_store() is not None
        tools = self._tools()
        assert "snapshot" in tools
        assert tools.index("snapshot") == tools.index("schedule") + 1

    def test_both_bootstraps_build_the_store(self):
        import ast
        from pathlib import Path

        src = Path(__import__("agent_cli.main", fromlist=["x"]).__file__).read_text()
        calls = [
            n
            for n in ast.walk(ast.parse(src))
            if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Name)
            and n.func.id == "build_snapshot_store"
        ]
        assert len(calls) == 2, f"호출부가 {len(calls)}개 — run/web 둘 다여야 한다"


def test_scan_cost_is_bounded_when_unchanged(ws):
    """변화 없는 체크포인트는 git 의 index(size·mtime) 비교만 — 수백 파일에 수십 ms."""
    for i in range(300):
        (ws / "src" / f"f{i}.py").write_text(f"# {i}\n")
    s = _store(ws)
    t0 = time.monotonic()
    for _ in range(3):
        assert s.checkpoint("idle", force=True) is None
    assert (time.monotonic() - t0) / 3 < 1.0
