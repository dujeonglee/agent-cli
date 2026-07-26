"""Workspace path confinement (agent_cli/tools/_confine.py).

Gate write_file / edit_file / shell to the launch workspace: a path resolving
OUTSIDE the workspace root prompts for confirmation, reusing the same
confirm / allowlist infra as the dangerous-command guard. read_file is NOT
gated (driver/kernel work reads outside by the dozen — a prompt storm). Shell
path extraction is best-effort and has a documented blind spot ($(...),
python -c, variables).
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from agent_cli.tools import _confine
from agent_cli.tools.edit_file import tool_edit_file
from agent_cli.tools.shell import tool_shell
from agent_cli.tools.write_file import tool_write_file


@pytest.fixture(autouse=True)
def _reset_allowlist():
    """Session root allowlist is module-level — clear between tests so one
    test's `a` answer doesn't bleed into the next."""
    _confine._session_root_allowlist.clear()
    yield
    _confine._session_root_allowlist.clear()


@pytest.fixture
def confined(tmp_path, monkeypatch):
    """Enable confinement with the workspace root pinned to ``<tmp>/ws``.
    Returns ``(ws, outside)`` — an inside dir and a sibling outside dir."""
    ws = tmp_path / "ws"
    ws.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    monkeypatch.setenv("AGENT_CLI_WORKSPACE_CONFINE", "1")
    monkeypatch.setenv("AGENT_CLI_WORKSPACE_ROOT", str(ws))
    return ws, outside


def _allow_prompt(monkeypatch):
    """Under pytest there is no TTY, so the renderer refuses early. Force the
    active renderer to report it can prompt so the confirm flow runs."""
    from agent_cli.render import get_renderer

    monkeypatch.setattr(type(get_renderer()), "can_prompt", lambda self: True)


# ── resolve_within ─────────────────────────────────────────


class TestResolveWithin:
    def test_absolute_inside(self, tmp_path):
        root = tmp_path.resolve()
        _resolved, inside = _confine.resolve_within(
            str(tmp_path / "a/b.txt"), root=root
        )
        assert inside is True

    def test_absolute_outside(self, tmp_path):
        root = (tmp_path / "ws").resolve()
        (tmp_path / "ws").mkdir()
        _, inside = _confine.resolve_within("/etc/passwd", root=root)
        assert inside is False

    def test_relative_resolves_inside(self, tmp_path):
        root = tmp_path.resolve()
        _, inside = _confine.resolve_within("sub/file.py", root=root)
        assert inside is True  # relative → joined to root

    def test_dotdot_escape_is_outside(self, tmp_path):
        root = (tmp_path / "ws").resolve()
        (tmp_path / "ws").mkdir()
        _, inside = _confine.resolve_within("../outside/x", root=root)
        assert inside is False

    def test_root_itself_is_inside(self, tmp_path):
        root = tmp_path.resolve()
        _, inside = _confine.resolve_within(str(tmp_path), root=root)
        assert inside is True

    def test_symlink_escape_is_outside(self, tmp_path):
        root = (tmp_path / "ws").resolve()
        root.mkdir()
        target = tmp_path / "outside"
        target.mkdir()
        link = root / "link"
        link.symlink_to(target)  # inside-looking path → resolves outside
        _, inside = _confine.resolve_within(str(link / "f.txt"), root=root)
        assert inside is False


# ── guard: enable / inside / no-prompt ─────────────────────


class TestGuardGating:
    def test_disabled_returns_none(self, confined, monkeypatch):
        _, outside = confined
        monkeypatch.setenv("AGENT_CLI_WORKSPACE_CONFINE", "0")
        # Even an outside path passes untouched when the gate is off.
        assert _confine.guard([str(outside / "x")], "write_file") is None

    def test_inside_path_no_prompt(self, confined):
        ws, _ = confined
        # No renderer forced / input patched → if it prompted, it'd refuse or
        # hang. None means it passed without prompting.
        assert _confine.guard([str(ws / "deep/x.txt")], "write_file") is None

    def test_outside_cannot_prompt_refused(self, confined):
        _, outside = confined
        # pytest has no TTY → can_prompt False → refuse (not hang).
        denial = _confine.guard([str(outside / "x")], "write_file")
        assert denial is not None
        assert "outside the workspace" in denial
        assert "AGENT_CLI_WORKSPACE_CONFINE=0" in denial


# ── guard: prompt decisions ────────────────────────────────


class TestGuardPromptFlow:
    def test_outside_yes_allows(self, confined, monkeypatch):
        _, outside = confined
        _allow_prompt(monkeypatch)
        with patch("builtins.input", return_value="y"):
            assert _confine.guard([str(outside / "x")], "write_file") is None
        # `y` does NOT allowlist — a second op prompts again.
        assert not _confine._session_root_allowlist

    def test_outside_no_denies(self, confined, monkeypatch):
        _, outside = confined
        _allow_prompt(monkeypatch)
        with patch("builtins.input", return_value="n wrong place"):
            denial = _confine.guard([str(outside / "x")], "write_file")
        assert denial is not None
        assert "User denied" in denial
        assert "wrong place" in denial  # free-text comment surfaced

    def test_outside_always_allowlists_subtree(self, confined, monkeypatch):
        _, outside = confined
        _allow_prompt(monkeypatch)
        with patch("builtins.input", return_value="a"):
            assert _confine.guard([str(outside / "hdr" / "a.h")], "shell") is None
        # Sibling under the same dir passes WITHOUT a second prompt (input
        # patched to raise → would fire if it prompted).
        with patch("builtins.input", side_effect=AssertionError("should not prompt")):
            assert _confine.guard([str(outside / "hdr" / "b.h")], "shell") is None


# ── shell path extraction (best-effort) ────────────────────


class TestExtractShellPaths:
    def test_absolute_path(self):
        assert _confine.extract_shell_paths("cat /etc/passwd") == ["/etc/passwd"]

    def test_flag_attached_path(self):
        # -C/usr/src and -I/usr/include: the path rides on the flag.
        assert _confine.extract_shell_paths("make -C /usr/src/linux") == [
            "/usr/src/linux"
        ]
        assert _confine.extract_shell_paths("gcc -I/usr/include a.c") == [
            "/usr/include"
        ]

    def test_long_flag_equals_path(self):
        assert _confine.extract_shell_paths("prog --dir=/opt/tool") == ["/opt/tool"]

    def test_home_path(self):
        assert _confine.extract_shell_paths("cat ~/secrets") == ["~/secrets"]

    def test_dotdot_escape(self):
        assert _confine.extract_shell_paths("cat ../../etc/x") == ["../../etc/x"]

    def test_bare_relative_not_extracted(self):
        # Resolves inside the workspace → never gates → not a candidate.
        assert _confine.extract_shell_paths("cat foo.txt src/main.c") == []

    def test_no_paths(self):
        assert _confine.extract_shell_paths("echo hello && ls -la") == []

    def test_redirect_target_extracted(self):
        assert _confine.extract_shell_paths("echo x > /etc/motd") == ["/etc/motd"]

    def test_blind_spot_python_c_not_caught(self):
        # DOCUMENTED LIMITATION: a path inside an interpreter string is opaque
        # to token extraction. Not caught — needs an OS sandbox, not a matcher.
        assert _confine.extract_shell_paths("python -c \"open('/etc/passwd')\"") == []

    def test_blind_spot_variable_not_caught(self):
        # DOCUMENTED LIMITATION: a path hidden behind a shell variable is
        # unknown at parse time. Not caught — needs an OS sandbox.
        assert _confine.extract_shell_paths("cat $SECRET_FILE") == []

    def test_command_substitution_leaks_token_safely(self):
        # $(...) is NOT a blind spot in the simple case: shlex spills the inner
        # absolute path as a token (with a trailing ')'), so it still gates.
        # Over-triggering is the safe side — we'd rather prompt than miss.
        assert _confine.extract_shell_paths("echo $(cat /etc/passwd)") == [
            "/etc/passwd)"
        ]


# ── tool integration ───────────────────────────────────────


class TestToolIntegration:
    def test_write_inside_ok(self, confined):
        ws, _ = confined
        r = tool_write_file({"path": str(ws / "a.txt"), "content": "hi"})
        assert r.success
        assert (ws / "a.txt").read_text() == "hi"

    def test_write_outside_refused_no_tty(self, confined):
        _, outside = confined
        target = outside / "evil.txt"
        r = tool_write_file({"path": str(target), "content": "x"})
        assert not r.success
        assert "outside the workspace" in (r.error or "")
        assert not target.exists()  # nothing written

    def test_write_outside_denied_by_user(self, confined, monkeypatch):
        _, outside = confined
        _allow_prompt(monkeypatch)
        target = outside / "evil.txt"
        with patch("builtins.input", return_value="n"):
            r = tool_write_file({"path": str(target), "content": "x"})
        assert not r.success
        assert not target.exists()

    def test_edit_outside_refused_no_tty(self, confined):
        _, outside = confined
        # Create the file directly (bypassing the gate) so the refusal is about
        # the edit path, not a missing file.
        target = outside / "f.txt"
        target.write_text("line1\nline2\n")
        r = tool_edit_file(
            {"path": str(target), "op": "append", "pos": None, "lines": ["x"]}
        )
        assert not r.success
        assert "outside the workspace" in (r.error or "")

    def test_shell_outside_path_refused_no_tty(self, confined):
        r = tool_shell({"command": "cat /etc/passwd"})
        assert not r.success
        assert "outside the workspace" in (r.error or "")

    def test_shell_inside_path_runs(self, confined):
        ws, _ = confined
        (ws / "note.txt").write_text("hello-inside")
        r = tool_shell({"command": f"cat {ws / 'note.txt'}"})
        assert r.success
        assert "hello-inside" in (r.output or "")


class TestConfineWaitsForLateViewer:
    """v7.8.0 can_prompt=True(web) 의 confine 측 계약 — shell 과 같은
    게이트인데 v7.8.0 당시 shell 만 검증했다(v7.11.1 보강). 뷰어가 없어도
    워크스페이스 밖 접근 승인은 대기하고, 늦게 접속한 사용자의 답으로
    해제된다."""

    def test_confine_confirm_waits_and_accepts_late_answer(self, confined, monkeypatch):
        import threading
        import time

        import agent_cli.render as render_mod
        from agent_cli.render.web import WebConnection, WebRenderer

        _ws, outside = confined
        r = WebRenderer()
        monkeypatch.setattr(render_mod, "get_renderer", lambda: r)
        results = []

        def worker():
            results.append(_confine.guard([str(outside / "x.txt")], "read_file"))

        t = threading.Thread(target=worker, daemon=True)
        t.start()
        time.sleep(0.3)
        assert t.is_alive(), "뷰어 0명이어도 confine 승인은 대기해야 한다"

        conn = WebConnection(id="late")
        snapshot = r.register_connection(conn)
        kinds = [d.get("kind") for ev, d in snapshot if ev == "input_required"]
        assert "confirm" in kinds

        r.push_user_input("confirm", {"key": "n", "comment": ""})
        t.join(timeout=3.0)
        assert results and results[0] is not None
        assert "denied" in results[0]


class TestOutsideSpans:
    """``_outside_spans`` — char ranges of the offending path literals inside a
    shell command, for the confine confirm's highlight (same contract as the
    dangerous-keyword guard)."""

    def test_absolute_path_span(self):
        assert _confine._outside_spans("cat /tmp/a.c", ["/tmp/a.c"]) == [(4, 12)]

    def test_trailing_boundary_avoids_substring(self):
        # "/tmp/a" must NOT match inside "/tmp/a.c" (followed by ".").
        cmd = "rm /tmp/a && cat /tmp/a.c"
        assert _confine._outside_spans(cmd, ["/tmp/a"]) == [(3, 9)]

    def test_flag_attached_path(self):
        cmd = "gcc -I/usr/inc x.c"
        i = cmd.index("/usr/inc")
        assert _confine._outside_spans(cmd, ["/usr/inc"]) == [(i, i + len("/usr/inc"))]

    def test_duplicate_literal_spans_merged(self):
        # Same literal extracted twice → deduped/merged, not doubled.
        assert _confine._outside_spans("/tmp/a /tmp/a", ["/tmp/a", "/tmp/a"]) == [
            (0, 6),
            (7, 13),
        ]

    def test_empty_command(self):
        assert _confine._outside_spans("", ["/x"]) == []


class TestConfineHighlight:
    """The shell confine confirm passes the raw command + the offending path
    spans so the dialog highlights them; write/edit_file (no command) stay
    prompt-only."""

    def _spy(self, monkeypatch, decision="y"):
        captured = {}

        class Spy:
            def can_prompt(self):
                return True

            def confirm(
                self, prompt, options, *, default_key, command=None, danger_spans=None
            ):
                captured["prompt"] = prompt
                captured["command"] = command
                captured["danger_spans"] = danger_spans
                return (decision, "")

        monkeypatch.setattr("agent_cli.render.get_renderer", lambda: Spy())
        return captured

    def test_shell_command_highlights_outside_path(self, confined, monkeypatch):
        _, outside = confined
        captured = self._spy(monkeypatch)
        p = str(outside / "a.c")
        cmd = f"cat > {p}"
        assert _confine.guard([p], "shell command", command=cmd) is None
        assert captured["command"] == cmd
        i = cmd.index(p)
        assert captured["danger_spans"] == [(i, i + len(p))]

    def test_write_file_prompt_only_no_command(self, confined, monkeypatch):
        _, outside = confined
        captured = self._spy(monkeypatch)
        assert _confine.guard([str(outside / "x")], "write_file") is None
        assert captured["command"] is None
        assert captured["danger_spans"] is None

    def test_tool_shell_passes_command_to_guard(self, confined, monkeypatch):
        # shell wires the raw command through so the confine confirm can
        # highlight the offending path. Deny to avoid running the command.
        from agent_cli.tools import shell as shell_mod

        seen = {}

        def fake_guard(paths, action, *, command=None):
            seen["command"] = command
            return "denied for test"

        monkeypatch.setattr(_confine, "guard", fake_guard)
        _, outside = confined
        p = str(outside / "z.txt")
        res = shell_mod.tool_shell({"command": f"echo hi > {p}"})
        assert res.success is False
        assert seen["command"] == f"echo hi > {p}"
