"""Tests for shell output artifact saving + LRU + read-touch.

Covers the loop-level post-process design (A2/A3 = a''): shell tool
itself is unchanged; the loop owns the "oversized observation →
artifact + preview" branch, using shell_artifact helpers.
"""

from __future__ import annotations

import time
from unittest.mock import MagicMock, patch


from agent_cli.tools import shell_artifact as sa


# ── exceeds_limit ─────────────────────────────────────────────────


class TestExceedsLimit:
    def test_small_output_not_over(self, monkeypatch):
        # Keep thresholds at defaults; 10 lines × 20 bytes is tiny.
        monkeypatch.delenv("AGENT_CLI_SHELL_OUTPUT_LIMIT_LINES", raising=False)
        monkeypatch.delenv("AGENT_CLI_SHELL_OUTPUT_LIMIT_BYTES", raising=False)
        output = "\n".join(f"line {i}" for i in range(10))
        assert not sa.exceeds_limit(output)

    def test_line_count_trips_limit(self, monkeypatch):
        # Cut line limit to 50 — a 100-line output must trip.
        monkeypatch.setenv("AGENT_CLI_SHELL_OUTPUT_LIMIT_LINES", "50")
        output = "\n".join("x" for _ in range(100))
        assert sa.exceeds_limit(output)

    def test_byte_count_trips_limit(self, monkeypatch):
        """A few lines but one of them is huge → byte limit catches it."""
        monkeypatch.setenv("AGENT_CLI_SHELL_OUTPUT_LIMIT_LINES", "10000")
        monkeypatch.setenv("AGENT_CLI_SHELL_OUTPUT_LIMIT_BYTES", "1024")
        output = "a" * 5000  # single massive line
        assert sa.exceeds_limit(output)

    def test_zero_disables_line_axis(self, monkeypatch):
        monkeypatch.setenv("AGENT_CLI_SHELL_OUTPUT_LIMIT_LINES", "0")
        monkeypatch.setenv("AGENT_CLI_SHELL_OUTPUT_LIMIT_BYTES", "1024")
        # 10k lines but small bytes → line axis off, byte still under limit.
        output = "x\n" * 10_000
        # byte count > 1024 → trips byte axis
        assert sa.exceeds_limit(output)
        # But short output with 10k lines-disabled and tiny bytes:
        short = "a\nb\nc"
        assert not sa.exceeds_limit(short)

    def test_zero_disables_both_axes(self, monkeypatch):
        monkeypatch.setenv("AGENT_CLI_SHELL_OUTPUT_LIMIT_LINES", "0")
        monkeypatch.setenv("AGENT_CLI_SHELL_OUTPUT_LIMIT_BYTES", "0")
        output = "x\n" * 1_000_000
        assert not sa.exceeds_limit(output)


# ── save_artifact + LRU ───────────────────────────────────────────


class TestSaveArtifact:
    def test_writes_file_and_returns_path(self, tmp_path):
        path = sa.save_artifact(tmp_path, "ls -la", "a\nb\nc\n")
        assert path is not None
        assert path.is_file()
        assert path.read_text() == "a\nb\nc\n"

    def test_filename_encodes_command_hash(self, tmp_path):
        """Different commands hash to different filenames so concurrent
        writes don't collide."""
        p1 = sa.save_artifact(tmp_path, "find /", "x")
        p2 = sa.save_artifact(tmp_path, "grep foo", "y")
        assert p1 is not None and p2 is not None
        assert p1.name != p2.name
        # Both land in session_dir/shell/
        assert p1.parent.name == "shell"
        assert p2.parent.name == "shell"

    def test_artifact_size_cap_truncates(self, tmp_path, monkeypatch):
        """Per-file size cap protects against any single pathological
        command filling the disk."""
        monkeypatch.setenv("AGENT_CLI_SHELL_ARTIFACT_MAX_SIZE", "100")
        huge = "x" * 10_000
        path = sa.save_artifact(tmp_path, "dd if=/dev/urandom", huge)
        assert path is not None
        body = path.read_text()
        # Body is at most cap + truncation note (~80 chars)
        assert len(body) < 500
        assert "[truncated:" in body

    def test_lru_evicts_oldest(self, tmp_path, monkeypatch):
        """21st write with keep=20 must evict the mtime-oldest entry."""
        monkeypatch.setenv("AGENT_CLI_SHELL_ARTIFACT_KEEP", "20")

        # Write 20 artifacts with distinct commands (→ distinct filenames).
        paths = []
        for i in range(20):
            p = sa.save_artifact(tmp_path, f"cmd-{i}", f"content {i}")
            assert p is not None
            paths.append(p)
            # Stagger mtime so ordering is deterministic even at
            # second granularity.
            past = time.time() - (20 - i)
            import os as _os

            _os.utime(p, (past, past))

        shell_dir = tmp_path / "shell"
        assert len(list(shell_dir.glob("*.log"))) == 20

        # 21st write → oldest should be gone.
        newest = sa.save_artifact(tmp_path, "cmd-20", "new")
        assert newest is not None
        survivors = sorted(p.name for p in shell_dir.glob("*.log"))
        assert len(survivors) == 20
        # The first one we wrote (mtime-oldest) should not survive.
        assert paths[0].name not in survivors
        # Newest and second-oldest should still be around.
        assert newest.name in survivors

    def test_lru_zero_disables(self, tmp_path, monkeypatch):
        """keep=0 means unbounded: write 25, see 25."""
        monkeypatch.setenv("AGENT_CLI_SHELL_ARTIFACT_KEEP", "0")
        for i in range(25):
            p = sa.save_artifact(tmp_path, f"cmd-{i}", str(i))
            assert p is not None
        assert len(list((tmp_path / "shell").glob("*.log"))) == 25

    def test_atomic_write_leaves_no_tmp(self, tmp_path):
        """.tmp file from the write-rename sequence must not linger."""
        sa.save_artifact(tmp_path, "anything", "data")
        leftovers = list((tmp_path / "shell").glob("*.tmp"))
        assert leftovers == []

    def test_failed_write_returns_none(self, tmp_path, monkeypatch):
        """When the artifact dir cannot be created, return None so the
        caller falls back to inline output (information > optimisation)."""

        # Point session_dir at a path that mkdir can't create (parent is
        # a regular file, not a dir).
        blocker = tmp_path / "blocker"
        blocker.write_text("I am a file, not a directory")
        bad_session = blocker / "would-be-session"
        assert sa.save_artifact(bad_session, "x", "y") is None


# ── build_preview ─────────────────────────────────────────────────


class TestBuildPreview:
    def test_contains_head_tail_path_options(self, tmp_path):
        path = tmp_path / "shell" / "test.log"
        path.parent.mkdir()
        lines = [f"line {i}" for i in range(100)]
        output = "\n".join(lines)
        preview = sa.build_preview("grep x .", output, path)

        # Head 20 lines + tail 20 lines visible.
        assert "line 0" in preview and "line 19" in preview
        assert "line 80" in preview and "line 99" in preview
        # Middle omitted marker.
        assert "omitted" in preview
        # Only the NARROW recovery options are named up front.
        assert 'search="<keyword>"' in preview
        assert "line_start=N" in preview
        # Artifact path must be present so the LLM can dereference it.
        assert str(path) in preview
        # Command echoed for context.
        assert "grep x ." in preview

    def test_preview_hides_full_true_escape_hatch(self, tmp_path):
        """Just-in-time disclosure invariant: the shell preview must
        NOT advertise ``full=true``. Surfacing it here would let the
        LLM skip straight to reloading the whole artifact, defeating
        the entire savings story. If the LLM actually needs the full
        log it must call ``read_file(path)`` bare on the artifact —
        read_file's own guard then refuses with a message that *does*
        disclose ``full=true``. That extra roundtrip is the conscious-
        choice gate we want.
        """
        path = tmp_path / "shell" / "big.log"
        path.parent.mkdir()
        output = "\n".join(f"line {i}" for i in range(200))
        preview = sa.build_preview("find /", output, path)

        assert "full=true" not in preview
        assert "full=True" not in preview  # guard against casing drift
        assert '"full"' not in preview

    def test_failure_biases_tail(self, tmp_path):
        """On a failed command, preview should show more tail lines —
        error output clusters near the end."""
        path = tmp_path / "shell" / "fail.log"
        path.parent.mkdir()
        lines = [f"line {i}" for i in range(200)]
        preview = sa.build_preview("make", "\n".join(lines), path, succeeded=False)
        # Default tail is 20; failure mode bumps to 30. So line 170
        # (i.e. 200 - 30) should appear, but line 170 wouldn't appear
        # in the 20-line tail case.
        assert "line 170" in preview
        assert "line 180" in preview
        assert "line 199" in preview

    def test_short_output_no_omitted_marker(self, tmp_path):
        """If head + tail covers the whole output, no "omitted" note.

        Match the exact marker "lines omitted" — the word "omitted" on
        its own matches tmp_path names like `test_..._omitted_m0` that
        pytest generates from the test function name.
        """
        path = tmp_path / "shell" / "short.log"
        path.parent.mkdir()
        preview = sa.build_preview("echo hi", "a\nb\nc", path)
        assert "lines omitted" not in preview


# ── read-touch (LRU read awareness) ───────────────────────────────


class TestIsSessionShellArtifact:
    def test_path_inside_session_shell_dir(self, tmp_path):
        (tmp_path / "shell").mkdir()
        target = tmp_path / "shell" / "1776-abc.log"
        target.write_text("x")
        assert sa.is_session_shell_artifact(str(target), tmp_path) is True

    def test_path_outside_session_shell_dir(self, tmp_path):
        """A file with the same /shell/ substring but outside our
        session must not match — prevents false positives on user
        project files named like project/shell/build.log."""
        stranger = tmp_path / "project" / "shell" / "build.log"
        stranger.parent.mkdir(parents=True)
        stranger.write_text("user log")
        # Different session_dir → different shell root.
        assert (
            sa.is_session_shell_artifact(str(stranger), tmp_path / "sessionX") is False
        )

    def test_other_sessions_shell_dir_rejected(self, tmp_path):
        """Read path belongs to session B; we're in session A. Must not
        match — foreign session's LRU is not our concern."""
        a = tmp_path / "sessionA"
        b = tmp_path / "sessionB"
        (b / "shell").mkdir(parents=True)
        foreign = b / "shell" / "foo.log"
        foreign.write_text("x")
        assert sa.is_session_shell_artifact(str(foreign), a) is False

    def test_symlink_into_artifact_dir_resolves(self, tmp_path):
        """A symlink whose target is inside session/shell/ counts —
        resolve() follows the link before the prefix check."""
        (tmp_path / "shell").mkdir()
        real = tmp_path / "shell" / "real.log"
        real.write_text("x")
        link = tmp_path / "link.log"
        link.symlink_to(real)
        assert sa.is_session_shell_artifact(str(link), tmp_path) is True

    def test_none_session_dir_returns_false(self):
        assert sa.is_session_shell_artifact("/any/path", None) is False

    def test_non_string_path_returns_false(self, tmp_path):
        # Defensive against callers passing through raw action_input
        # where 'path' might be a dict / number due to LLM error.
        assert sa.is_session_shell_artifact(None, tmp_path) is False  # type: ignore
        assert sa.is_session_shell_artifact(123, tmp_path) is False  # type: ignore
        assert sa.is_session_shell_artifact("", tmp_path) is False


class TestTouchIfArtifact:
    def test_touch_bumps_mtime_on_artifact(self, tmp_path):
        (tmp_path / "shell").mkdir()
        artifact = tmp_path / "shell" / "old.log"
        artifact.write_text("x")
        # Backdate mtime
        past = time.time() - 10_000
        import os as _os

        _os.utime(artifact, (past, past))
        old_mtime = artifact.stat().st_mtime

        sa.touch_if_artifact(str(artifact), tmp_path)

        assert artifact.stat().st_mtime > old_mtime

    def test_touch_silent_on_non_artifact(self, tmp_path):
        """Calling on a path that isn't in our shell/ dir must be a
        no-op — especially must not raise."""
        other = tmp_path / "some" / "other.txt"
        other.parent.mkdir()
        other.write_text("x")
        # Should not raise, should not touch.
        sa.touch_if_artifact(str(other), tmp_path)


# ── Loop integration (A'') ────────────────────────────────────────


class TestLoopPostProcess:
    """The loop's _dispatch_tool_with_hooks must wrap oversized shell output
    into an artifact-backed preview, and must bump mtime on a read_file
    that targets a session shell artifact.
    """

    def _make_context(self, session_dir):
        """Minimal ctx stand-in exposing session_dir."""
        ctx = MagicMock()
        ctx.session_dir = session_dir
        return ctx

    def test_large_shell_output_replaced_with_preview(self, tmp_path, monkeypatch):
        """Trigger the guard at ~50 lines to keep the test fast."""
        monkeypatch.setenv("AGENT_CLI_SHELL_OUTPUT_LIMIT_LINES", "50")
        monkeypatch.setenv("AGENT_CLI_SHELL_OUTPUT_LIMIT_BYTES", "0")

        from agent_cli.loop import _dispatch_tool_with_hooks

        big = "\n".join(f"line {i}" for i in range(200))

        # Patch the underlying shell tool so we don't actually spawn a
        # subprocess; we only care about the loop's wrapping behaviour.
        from agent_cli.tools.result import ToolResult

        with patch(
            "agent_cli.loop.execute_tool",
            return_value=ToolResult(True, output=big),
        ):
            result = _dispatch_tool_with_hooks(
                tool_name="shell",
                tool_input={"command": "grep -rn TODO ."},
                tools_list=["shell"],
                capabilities=None,
                provider_name="",
                model="",
                base_url="",
                api_key="",
                delegate_timeout=30,
                tools_called=[],
                recent_tool_history=[],
                turn=1,
                session_dir=tmp_path,
            )

        assert result.success
        assert "[shell-output-saved]" in result.output
        assert "grep -rn TODO ." in result.output  # command echoed
        assert "200 lines" in result.output
        # An artifact file should exist in tmp_path/shell/
        logs = list((tmp_path / "shell").glob("*.log"))
        assert len(logs) == 1
        assert logs[0].read_text().startswith("line 0\n")

    def test_small_shell_output_not_touched(self, tmp_path):
        """A small output must pass through unmodified — guard silent."""
        from agent_cli.loop import _dispatch_tool_with_hooks
        from agent_cli.tools.result import ToolResult

        with patch(
            "agent_cli.loop.execute_tool",
            return_value=ToolResult(True, output="small output\n2 lines"),
        ):
            result = _dispatch_tool_with_hooks(
                tool_name="shell",
                tool_input={"command": "echo hi"},
                tools_list=["shell"],
                capabilities=None,
                provider_name="",
                model="",
                base_url="",
                api_key="",
                delegate_timeout=30,
                tools_called=[],
                recent_tool_history=[],
                turn=1,
                session_dir=tmp_path,
            )

        assert result.output == "small output\n2 lines"
        # No artifact written.
        assert not (tmp_path / "shell").exists() or not list(
            (tmp_path / "shell").glob("*.log")
        )

    def test_no_session_dir_disables_guard(self, tmp_path, monkeypatch):
        """With session_dir=None (headless / no ctx) we can't write
        artifacts anywhere, so the guard stays out of the way and the
        full output flows through — information > optimisation."""
        monkeypatch.setenv("AGENT_CLI_SHELL_OUTPUT_LIMIT_LINES", "10")

        from agent_cli.loop import _dispatch_tool_with_hooks
        from agent_cli.tools.result import ToolResult

        big = "\n".join(f"line {i}" for i in range(100))
        with patch(
            "agent_cli.loop.execute_tool",
            return_value=ToolResult(True, output=big),
        ):
            result = _dispatch_tool_with_hooks(
                tool_name="shell",
                tool_input={"command": "x"},
                tools_list=["shell"],
                capabilities=None,
                provider_name="",
                model="",
                base_url="",
                api_key="",
                delegate_timeout=30,
                tools_called=[],
                recent_tool_history=[],
                turn=1,
                session_dir=None,
            )

        assert result.output == big
        assert "[shell-output-saved]" not in result.output

    def test_read_file_of_artifact_touches_mtime(self, tmp_path, monkeypatch):
        """After a successful read_file on a path inside session/shell/,
        the loop post-process must bump the file's mtime so the next
        LRU pass treats it as recently-used."""
        from agent_cli.loop import _dispatch_tool_with_hooks
        from agent_cli.tools.result import ToolResult

        (tmp_path / "shell").mkdir()
        artifact = tmp_path / "shell" / "old.log"
        artifact.write_text("some log content\n")
        past = time.time() - 10_000
        import os as _os

        _os.utime(artifact, (past, past))
        old_mtime = artifact.stat().st_mtime

        with patch(
            "agent_cli.loop.execute_tool",
            return_value=ToolResult(True, output="(some file contents)"),
        ):
            _dispatch_tool_with_hooks(
                tool_name="read_file",
                tool_input={"path": str(artifact)},
                tools_list=["read_file"],
                capabilities=None,
                provider_name="",
                model="",
                base_url="",
                api_key="",
                delegate_timeout=30,
                tools_called=[],
                recent_tool_history=[],
                turn=1,
                session_dir=tmp_path,
            )

        new_mtime = artifact.stat().st_mtime
        assert new_mtime > old_mtime, (
            "LRU read-awareness: artifact mtime must be bumped after read"
        )
