"""승인하면서 남긴 말이 모든 확인 경로에서 모델에게 간다 (v10.120.1).

종전엔 셸 전경의 위험 키워드 확인만 ``[User note when approving: …]`` 를 붙였고,
셸 백그라운드·monitor 등록은 코멘트를 버렸으며 경로 확인(`_confine.guard`)은
거절 문자열만 돌려줘 승인 코멘트가 나갈 길이 없었다. 경로마다 세 경우를 본다:
승인 + 코멘트 → 결과 끝에 그 줄, 승인 + 코멘트 없음 → 줄 없음(모델 입력 불변),
거절 + 코멘트 → 종전 문구.
"""

from __future__ import annotations

import pytest

from agent_cli import render as render_mod
from agent_cli.tools import _confine
from agent_cli.tools import shell as shell_mod


class _Scripted:
    """confirm 에 대본대로 답하는 렌더러 — 묻는 순서대로 ``(key, comment)`` 를 꺼낸다."""

    auto_approve = False

    def __init__(self, answers):
        self.answers = list(answers)
        self.asked: list[str] = []
        self.notes: list[str] = []

    def can_prompt(self) -> bool:
        return True

    def auto_approved(self, label: str) -> bool:
        return False

    def confirm(self, prompt, options, *, default_key, command=None, danger_spans=None):
        self.asked.append(prompt)
        return self.answers.pop(0)

    def note_next(self, text: str) -> None:
        self.notes.append(text)


@pytest.fixture
def ws(tmp_path, monkeypatch):
    """워크스페이스 ``<tmp>/ws`` (cwd), 밖 ``<tmp>/outside``. 임시 루트는 ``<tmp>/systmp``
    하나로 바꿔 끼운다 — 실제 루트면 ``outside`` 가 임시 통과로 묻지 않는다."""
    root = tmp_path / "ws"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    systmp = tmp_path / "systmp"
    systmp.mkdir()
    monkeypatch.chdir(root)
    monkeypatch.setenv("AGENT_CLI_WORKSPACE_CONFINE", "1")
    monkeypatch.setenv("AGENT_CLI_WORKSPACE_ROOT", str(root))
    monkeypatch.setenv("AGENT_CLI_DANGEROUS_SHELL_CONFIRM", "1")
    monkeypatch.setattr(_confine, "temp_roots", lambda: frozenset({systmp.resolve()}))
    monkeypatch.setattr(_confine, "_session_root_allowlist", set())
    monkeypatch.setattr(shell_mod, "_session_allowlist", set())
    return root, outside


def _answer(monkeypatch, *answers) -> _Scripted:
    r = _Scripted(answers)
    monkeypatch.setattr(render_mod, "get_renderer", lambda: r)
    return r


NOTE = "[User note when approving: {}]"


# ── 셸 백그라운드 ─────────────────────────────────────────


@pytest.fixture
def jobs(tmp_path):
    from agent_cli.jobs import JobRegistry, set_job_registry
    from tests.monitor_delivery import RecordingDelivery

    reg = JobRegistry(tmp_path)
    reg.deliver = RecordingDelivery()
    set_job_registry(reg)
    yield reg
    reg.stop_all()
    set_job_registry(None)


class TestShellBackground:
    CMD = "rm -f nothing.txt"

    def _run(self):
        from agent_cli.tools.shell import ShellTool

        return ShellTool()._run({"command": self.CMD, "background": True})

    def test_approve_with_comment_ends_the_start_result(self, ws, jobs, monkeypatch):
        _answer(monkeypatch, ("y", "then check the log"))
        r = self._run()
        assert r.success, r.error
        assert r.output.startswith("job #1 started (pid ")
        assert r.output.endswith("\n" + NOTE.format("then check the log"))

    def test_approve_without_comment_adds_nothing(self, ws, jobs, monkeypatch):
        _answer(monkeypatch, ("y", ""))
        r = self._run()
        assert r.success and "User note" not in r.output
        assert r.output.endswith(".")  # 종전 결과의 끝 그대로

    def test_deny_with_comment_keeps_the_old_wording(self, ws, jobs, monkeypatch):
        _answer(monkeypatch, ("n", "not now"))
        r = self._run()
        assert not r.success and jobs.running() == []
        assert (
            r.error
            == f"User denied command containing `rm`: {self.CMD}. User said: not now"
        )


# ── monitor 등록 ─────────────────────────────────────────


@pytest.fixture
def monitors():
    from agent_cli.monitor import MonitorRegistry
    from agent_cli.monitor.runtime import set_monitor_registry
    from tests.monitor_delivery import RecordingDelivery

    reg = MonitorRegistry()
    reg.deliver = RecordingDelivery()
    reg.stop()
    set_monitor_registry(reg)
    yield reg
    set_monitor_registry(None)
    reg.stop()


class TestMonitorRegistration:
    CMD = "rm -f nothing.txt"

    def _add(self):
        from agent_cli.tools.registry import TOOLS

        return TOOLS["monitor"].run(
            {"mode": "add", "command": self.CMD, "every": "60s"}
        )

    def test_approve_with_comment_ends_the_registration(
        self, ws, monitors, monkeypatch
    ):
        _answer(monkeypatch, ("y", "stop it after the build"))
        r = self._add()
        assert r.success, r.error
        assert r.output.endswith("\n" + NOTE.format("stop it after the build"))
        assert r.output.count("User note") == 1

    def test_approve_without_comment_adds_nothing(self, ws, monitors, monkeypatch):
        _answer(monkeypatch, ("y", ""))
        r = self._add()
        assert r.success and "User note" not in r.output

    def test_deny_with_comment_keeps_the_old_wording(self, ws, monitors, monkeypatch):
        _answer(monkeypatch, ("n", "no polling"))
        r = self._add()
        assert not r.success
        assert (
            r.error
            == f"User denied command containing `rm`: {self.CMD}. User said: no polling"
        )


# ── 파일 도구 (워크스페이스 밖) ───────────────────────────


class TestWriteFileOutside:
    def test_approve_with_comment_ends_the_result(self, ws, monkeypatch):
        from agent_cli.tools.write_file import tool_write_file

        _, outside = ws
        _answer(monkeypatch, ("y", "keep a copy in ws too"))
        r = tool_write_file({"path": str(outside / "a.txt"), "content": "hi\n"})
        assert r.success, r.error
        assert r.output.startswith("File saved: ")
        assert r.output.endswith("\n" + NOTE.format("keep a copy in ws too"))

    def test_approve_without_comment_adds_nothing(self, ws, monkeypatch):
        from agent_cli.tools.write_file import tool_write_file

        _, outside = ws
        _answer(monkeypatch, ("y", ""))
        r = tool_write_file({"path": str(outside / "a.txt"), "content": "hi\n"})
        assert r.success and "User note" not in r.output

    def test_deny_with_comment_keeps_the_old_wording(self, ws, monkeypatch):
        from agent_cli.tools.write_file import tool_write_file

        _, outside = ws
        _answer(monkeypatch, ("n", "wrong place"))
        target = outside / "a.txt"
        r = tool_write_file({"path": str(target), "content": "hi\n"})
        assert not r.success and not target.exists()
        assert r.error == (
            f"User denied write_file — outside workspace: {target.resolve()}. "
            "User said: wrong place"
        )


class TestEditFileOutside:
    def test_approve_with_comment_ends_the_last_part(self, ws, monkeypatch):
        from agent_cli.tools.edit_file import apply_edits_batch_parts
        from agent_cli.tools.read_file import compute_line_hash

        _, outside = ws
        target = outside / "e.txt"
        target.write_text("a\nb\n")
        ref = f"1#{compute_line_hash(0, 'a')}"
        _answer(monkeypatch, ("y", "also update the docs"))
        r, parts = apply_edits_batch_parts(
            str(target), [{"op": "replace", "pos": ref, "lines": ["A"]}]
        )
        assert r.success, r.error
        assert r.output.endswith("\n" + NOTE.format("also update the docs"))
        assert parts[-1]["content"].endswith(NOTE.format("also update the docs"))


# ── 셸의 경로 확인 ─────────────────────────────────────────


class TestShellPathCheck:
    def test_outside_path_without_keyword(self, ws, monkeypatch):
        """위험 키워드 없이 경로 확인만 묻는다 — 그 승인 코멘트가 전경 결과 끝에."""
        _, outside = ws
        r = _answer(monkeypatch, ("y", "use ws next time"))
        res = shell_mod.tool_shell(
            {"command": f"echo done; echo hi > {outside / 'z.txt'}"}
        )
        assert len(r.asked) == 1 and "outside the workspace" in r.asked[0]
        assert res.success and res.output == "done\n" + NOTE.format("use ws next time")

    def test_outside_path_approve_without_comment(self, ws, monkeypatch):
        _, outside = ws
        _answer(monkeypatch, ("y", ""))
        res = shell_mod.tool_shell(
            {"command": f"echo done; echo hi > {outside / 'z.txt'}"}
        )
        assert res.success and res.output == "done"

    def test_outside_path_deny_keeps_the_old_wording(self, ws, monkeypatch):
        _, outside = ws
        _answer(monkeypatch, ("n", "stay inside"))
        target = outside / "z.txt"
        res = shell_mod.tool_shell({"command": f"echo hi > {target}"})
        assert not res.success and not target.exists()
        assert res.error == (
            f"User denied shell command — outside workspace: {target.resolve()}. "
            "User said: stay inside"
        )

    def test_keyword_and_path_both_asked_both_comments(self, ws, monkeypatch):
        """위험 키워드와 밖 경로가 둘 다 확인되면 코멘트 둘 — 키워드 쪽이 먼저."""
        _, outside = ws
        r = _answer(monkeypatch, ("y", "first"), ("y", "second"))
        res = shell_mod.tool_shell({"command": f"rm -f {outside / 'gone.txt'}"})
        assert len(r.asked) == 2
        assert "Dangerous command" in r.asked[0]
        assert "outside the workspace" in r.asked[1]
        assert res.success, res.error
        assert res.output == NOTE.format("first") + "\n" + NOTE.format("second")


# ── 도우미 ────────────────────────────────────────────────


class TestWithApprovalNote:
    def test_no_comment_is_identity(self):
        assert _confine.with_approval_note("out") == "out"
        assert _confine.with_approval_note("out", "", "") == "out"

    def test_one_line_per_comment(self):
        assert _confine.with_approval_note("out", "a", "", "b") == (
            "out\n[User note when approving: a]\n[User note when approving: b]"
        )
