"""확인에서 사람이 남긴 말은 한 곳에서 — 그 호출의 결과 끝에 한 줄 (v10.120.2).

모으기는 안전 확인 두 곳(셸 `confirm_dangerous`, `_confine.guard`)이 ``human_note`` 에,
붙이기는 실행 지점 두 곳(``ToolBridge._dispatch_tool_with_hooks``·
``dispatch_edit_batch``)이 결과 끝에 — 성공이면 output, 실패면 error. 그래서 이
스위트는 도구를 **bridge 를 통해** 부른다(도구를 바로 부르면 줄이 없다).

v10.120.1 의 ``test_approval_note.py`` 기대(경로마다 승인+코멘트 → 줄, 코멘트 없음 →
줄 없음)를 그대로 옮기고, 그때 못 하던 것을 더한다: 승인 뒤 도구 실패, 거절 줄,
앞 호출의 남은 기록, 스레드 격리, ⚡ 자동 승인.
"""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

from agent_cli import render as render_mod
from agent_cli.loop.state import LoopState
from agent_cli.loop.tool_bridge import ToolBridge
from agent_cli.tools import TOOLS, _confine, human_note
from agent_cli.tools import shell as shell_mod
from tests.loopkit import make_config

APPROVE = "[User note when approving: {}]"
DENY = "[User note when denying: {}]"


class _Scripted:
    """confirm 에 대본대로 답하는 렌더러 — 묻는 순서대로 ``(key, comment)`` 를 꺼낸다.
    ``answers`` 가 dict 면 스레드 이름으로 고른다."""

    auto_approve = False

    def __init__(self, answers, *, auto=False):
        self.answers = answers if isinstance(answers, dict) else list(answers)
        self.auto = auto
        self.asked: list[str] = []
        self.notes: list[str] = []
        self.answered = threading.Event()

    def can_prompt(self) -> bool:
        return True

    def auto_approved(self, label: str) -> bool:
        return self.auto

    def confirm(self, prompt, options, *, default_key, command=None, danger_spans=None):
        self.asked.append(prompt)
        if isinstance(self.answers, dict):
            ans = self.answers[threading.current_thread().name]
        else:
            ans = self.answers.pop(0)
        self.answered.set()
        return ans

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
    human_note.clear()
    yield root, outside
    human_note.clear()


def _answer(monkeypatch, *answers, auto=False) -> _Scripted:
    """대본 렌더러를 끼운다 — 답 하나가 dict 면 스레드 이름별 답."""
    if len(answers) == 1 and isinstance(answers[0], dict):
        answers = answers[0]
    r = _Scripted(answers, auto=auto)
    monkeypatch.setattr(render_mod, "get_renderer", lambda: r)
    return r


def _bridge() -> ToolBridge:
    return ToolBridge(
        make_config(tools_list=list(TOOLS.keys())), LoopState(), ctx=None, provider=None
    )


def _call(name: str, args: dict):
    return _bridge()._dispatch_tool_with_hooks(name, args)


# ── 셸 전경 ───────────────────────────────────────────────


class TestShellForeground:
    def test_approve_with_comment_ends_the_output(self, ws, monkeypatch):
        _answer(monkeypatch, ("y", "check the log"))
        r = _call("shell", {"command": "echo done; rm -f nothing.txt"})
        assert r.success and r.output == "done\n" + APPROVE.format("check the log")

    def test_approve_without_comment_adds_nothing(self, ws, monkeypatch):
        _answer(monkeypatch, ("y", ""))
        r = _call("shell", {"command": "echo done; rm -f nothing.txt"})
        assert r.success and r.output == "done"

    def test_nonzero_exit_keeps_the_line_after_the_exit_code(self, ws, monkeypatch):
        _answer(monkeypatch, ("y", "fine if it fails"))
        r = _call("shell", {"command": "rm nothing.txt"})
        assert r.success  # 비영 종료도 셸에선 결과다
        assert r.output.endswith(
            "[exit code: 1]\n" + APPROVE.format("fine if it fails")
        )

    def test_failure_after_approval_ends_the_error(self, ws, monkeypatch):
        """승인 뒤 도구가 실패해도(시간 초과) 말은 오류 결과 끝에 — v10.120.1 은 버렸다."""
        _answer(monkeypatch, ("y", "kill it if slow"))
        r = _call("shell", {"command": "rm -f nothing.txt; sleep 5", "timeout": 1})
        assert not r.success
        assert r.error.endswith("\n" + APPROVE.format("kill it if slow"))

    def test_deny_with_comment_ends_the_denial(self, ws, monkeypatch):
        _answer(monkeypatch, ("n", "not now"))
        cmd = "rm -f nothing.txt"
        r = _call("shell", {"command": cmd})
        assert not r.success
        assert r.error == (
            f"User denied command containing `rm`: {cmd}\n" + DENY.format("not now")
        )

    def test_deny_without_comment_is_the_bare_denial(self, ws, monkeypatch):
        _answer(monkeypatch, ("n", ""))
        cmd = "rm -f nothing.txt"
        r = _call("shell", {"command": cmd})
        assert r.error == f"User denied command containing `rm`: {cmd}"

    def test_always_with_comment(self, ws, monkeypatch):
        rr = _answer(monkeypatch, ("a", "only under build/"))
        r = _call("shell", {"command": "echo ok; rm -f nothing.txt"})
        assert r.output == "ok\n" + APPROVE.format("only under build/")
        assert rr.notes and "rm" in shell_mod._session_allowlist

    def test_auto_approve_adds_nothing(self, ws, monkeypatch):
        """⚡ 자동 승인은 묻지 않는다 — 코멘트도 줄도 없다."""
        rr = _answer(monkeypatch, auto=True)
        _, outside = ws
        r = _call("shell", {"command": f"echo ok; rm -f {outside / 'x'}"})
        assert rr.asked == [] and r.success and r.output == "ok"


# ── 셸의 경로 확인 ─────────────────────────────────────────


class TestShellPathCheck:
    def test_outside_path_without_keyword(self, ws, monkeypatch):
        """위험 키워드 없이 경로 확인만 묻는다 — 그 승인 코멘트가 전경 결과 끝에."""
        _, outside = ws
        r = _answer(monkeypatch, ("y", "use ws next time"))
        res = _call("shell", {"command": f"echo done; echo hi > {outside / 'z.txt'}"})
        assert len(r.asked) == 1 and "outside the workspace" in r.asked[0]
        assert res.success and res.output == "done\n" + APPROVE.format(
            "use ws next time"
        )

    def test_outside_path_approve_without_comment(self, ws, monkeypatch):
        _, outside = ws
        _answer(monkeypatch, ("y", ""))
        res = _call("shell", {"command": f"echo done; echo hi > {outside / 'z.txt'}"})
        assert res.success and res.output == "done"

    def test_outside_path_deny(self, ws, monkeypatch):
        _, outside = ws
        _answer(monkeypatch, ("n", "stay inside"))
        target = outside / "z.txt"
        res = _call("shell", {"command": f"echo hi > {target}"})
        assert not res.success and not target.exists()
        assert res.error == (
            f"User denied shell command — outside workspace: {target.resolve()}\n"
            + DENY.format("stay inside")
        )

    def test_keyword_and_path_both_asked_both_lines(self, ws, monkeypatch):
        """위험 키워드와 밖 경로가 둘 다 확인되면 줄 둘 — 기록 순서(키워드 먼저)."""
        _, outside = ws
        r = _answer(monkeypatch, ("y", "first"), ("y", "second"))
        res = _call("shell", {"command": f"echo ok; rm -f {outside / 'gone.txt'}"})
        assert len(r.asked) == 2
        assert "Dangerous command" in r.asked[0]
        assert "outside the workspace" in r.asked[1]
        assert res.output == (
            "ok\n" + APPROVE.format("first") + "\n" + APPROVE.format("second")
        )

    def test_keyword_approved_then_path_denied_both_lines(self, ws, monkeypatch):
        _, outside = ws
        _answer(monkeypatch, ("y", "ok to delete"), ("n", "but not there"))
        target = outside / "gone.txt"
        res = _call("shell", {"command": f"rm -f {target}"})
        assert not res.success
        assert res.error.endswith(
            "\n" + APPROVE.format("ok to delete") + "\n" + DENY.format("but not there")
        )


# ── 셸 백그라운드 ─────────────────────────────────────────


@pytest.fixture
def jobs(tmp_path):
    from agent_cli.jobs import JobRegistry
    from agent_cli.session_slots import set_job_registry
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
        return _call("shell", {"command": self.CMD, "background": True})

    def test_approve_with_comment_ends_the_start_result(self, ws, jobs, monkeypatch):
        _answer(monkeypatch, ("y", "then check the log"))
        r = self._run()
        assert r.success, r.error
        assert r.output.startswith("job #1 started (pid ")
        assert r.output.endswith(".\n" + APPROVE.format("then check the log"))

    def test_approve_without_comment_adds_nothing(self, ws, jobs, monkeypatch):
        _answer(monkeypatch, ("y", ""))
        r = self._run()
        assert r.success and "User note" not in r.output
        assert r.output.endswith(".")  # 종전 결과의 끝 그대로

    def test_deny_with_comment(self, ws, jobs, monkeypatch):
        _answer(monkeypatch, ("n", "not now"))
        r = self._run()
        assert not r.success and jobs.running() == []
        assert r.error == (
            f"User denied command containing `rm`: {self.CMD}\n"
            + DENY.format("not now")
        )


# ── monitor 등록 ─────────────────────────────────────────


@pytest.fixture
def monitors():
    from agent_cli.monitor import MonitorRegistry
    from agent_cli.session_slots import set_monitor_registry
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
        return _call("monitor", {"mode": "add", "command": self.CMD, "every": "60s"})

    def test_approve_with_comment_ends_the_registration(
        self, ws, monitors, monkeypatch
    ):
        _answer(monkeypatch, ("y", "stop it after the build"))
        r = self._add()
        assert r.success, r.error
        assert r.output.endswith("\n" + APPROVE.format("stop it after the build"))
        assert r.output.count("User note") == 1

    def test_approve_without_comment_adds_nothing(self, ws, monitors, monkeypatch):
        _answer(monkeypatch, ("y", ""))
        r = self._add()
        assert r.success and "User note" not in r.output

    def test_deny_with_comment(self, ws, monitors, monkeypatch):
        _answer(monkeypatch, ("n", "no polling"))
        r = self._add()
        assert not r.success
        assert r.error == (
            f"User denied command containing `rm`: {self.CMD}\n"
            + DENY.format("no polling")
        )


# ── 파일 도구 (워크스페이스 밖·민감 경로) ─────────────────────


class TestWriteFileOutside:
    def test_approve_with_comment_ends_the_result(self, ws, monkeypatch):
        _, outside = ws
        _answer(monkeypatch, ("y", "keep a copy in ws too"))
        r = _call("write_file", {"path": str(outside / "a.txt"), "content": "hi\n"})
        assert r.success, r.error
        assert r.output.startswith("File saved: ")
        assert r.output.endswith("\n" + APPROVE.format("keep a copy in ws too"))

    def test_approve_without_comment_adds_nothing(self, ws, monkeypatch):
        _, outside = ws
        _answer(monkeypatch, ("y", ""))
        r = _call("write_file", {"path": str(outside / "a.txt"), "content": "hi\n"})
        assert r.success and "User note" not in r.output

    def test_write_failure_after_approval_ends_the_error(self, ws, monkeypatch):
        _, outside = ws
        (outside / "afile").write_text("x")
        _answer(monkeypatch, ("y", "put it under outside/"))
        r = _call(
            "write_file", {"path": str(outside / "afile" / "a.txt"), "content": "hi"}
        )
        assert not r.success
        assert r.error.startswith("write_file failed: ")
        assert r.error.endswith("\n" + APPROVE.format("put it under outside/"))

    def test_deny_with_comment(self, ws, monkeypatch):
        _, outside = ws
        _answer(monkeypatch, ("n", "wrong place"))
        target = outside / "a.txt"
        r = _call("write_file", {"path": str(target), "content": "hi\n"})
        assert not r.success and not target.exists()
        assert r.error == (
            f"User denied write_file — outside workspace: {target.resolve()}\n"
            + DENY.format("wrong place")
        )


def _ref(n: int, line: str) -> str:
    from agent_cli.tools.read_file import compute_line_hash

    return f"{n}#{compute_line_hash(n - 1, line)}"


class TestEditFileOutside:
    def test_single_edit_approve_with_comment(self, ws, monkeypatch):
        _, outside = ws
        target = outside / "e.txt"
        target.write_text("a\nb\n")
        _answer(monkeypatch, ("y", "also update the docs"))
        r = _call(
            "edit_file",
            {"path": str(target), "op": "replace", "pos": _ref(1, "a"), "lines": ["A"]},
        )
        assert r.success, r.error
        assert r.output.endswith("\n" + APPROVE.format("also update the docs"))

    def test_batch_ends_the_result_and_the_last_part(self, ws, monkeypatch):
        _, outside = ws
        target = outside / "e.txt"
        target.write_text("a\nb\n")
        _answer(monkeypatch, ("y", "also update the docs"))
        r, parts = _bridge().dispatch_edit_batch(
            str(target),
            [
                {"op": "replace", "pos": _ref(1, "a"), "lines": ["A"]},
                {"op": "replace", "pos": _ref(2, "b"), "lines": ["B"]},
            ],
        )
        assert r.success, r.error
        assert target.read_text() == "A\nB\n"
        line = APPROVE.format("also update the docs")
        assert r.output.endswith("\n" + line)
        assert parts[-1]["content"].endswith("\n" + line)
        assert "User note" not in parts[0]["content"]

    def test_batch_failure_after_approval(self, ws, monkeypatch):
        _, outside = ws
        target = outside / "e.txt"
        target.write_text("a\nb\n")
        _answer(monkeypatch, ("y", "careful"))
        r, parts = _bridge().dispatch_edit_batch(
            str(target), [{"op": "replace", "pos": "1#ZZ", "lines": ["A"]}]
        )
        assert not r.success
        assert r.error.endswith("\n" + APPROVE.format("careful"))
        assert parts[-1]["content"].endswith("\n" + APPROVE.format("careful"))


class TestSensitiveReads:
    def test_read_file(self, ws, monkeypatch):
        _, outside = ws
        key = outside / "id_rsa"
        key.write_text("SECRET\n")
        _answer(monkeypatch, ("y", "do not print it again"))
        r = _call("read_file", {"path": str(key)})
        assert r.success, r.error
        assert "SECRET" in r.output
        assert r.output.endswith("\n" + APPROVE.format("do not print it again"))

    def test_view_image_failure_after_approval(self, ws, monkeypatch):
        """승인했지만 이미지가 아니다 — 실패 결과 끝에 줄 (v10.120.1 은 성공에만 붙였다)."""
        _, outside = ws
        key = outside / "id_rsa"
        key.write_text("not an image")
        _answer(monkeypatch, ("y", "it is text"))
        r = _call("view_image", {"path": str(key)})
        assert not r.success and "is not a PNG" in r.error
        assert r.error.endswith("\n" + APPROVE.format("it is text"))


class TestFetchDownload:
    def test_approve_success_and_failure(self, ws, monkeypatch):
        from agent_cli.tools import fetch as fetch_mod

        _, outside = ws
        dest = str(outside / "f.bin")
        monkeypatch.setattr(
            fetch_mod, "_download", lambda url, d: (f"Saved {d} (3 bytes)", None)
        )
        _answer(monkeypatch, ("y", "unzip it next"))
        r = _call("fetch", {"url": "https://x.test/f", "download_to": dest})
        assert r.output == f"Saved {dest} (3 bytes)\n" + APPROVE.format("unzip it next")

        monkeypatch.setattr(fetch_mod, "_download", lambda url, d: ("", "HTTP 404"))
        monkeypatch.setattr(_confine, "_session_root_allowlist", set())
        _answer(monkeypatch, ("y", "try the mirror"))
        r = _call("fetch", {"url": "https://x.test/f", "download_to": dest})
        assert not r.success
        assert r.error == "HTTP 404\n" + APPROVE.format("try the mirror")


class TestBrowserLocalFile:
    def test_open_sensitive_local_file(self, ws, monkeypatch):
        import agent_cli.browser as B
        from agent_cli.tools.browser_tool import BrowserTool

        _, outside = ws
        key = outside / "id_rsa"
        key.write_text("<p>x</p>")
        monkeypatch.setattr(B, "get_browser", lambda create=False: object())
        monkeypatch.setattr(
            BrowserTool, "_dispatch", lambda self, s, mode, args, budget: "[page] ok"
        )
        _answer(monkeypatch, ("y", "close it after"))
        r = _call("browser", {"mode": "open", "url": str(key)})
        assert r.success, r.error
        assert r.output == "[page] ok\n" + APPROVE.format("close it after")
        _answer(monkeypatch, ("n", "never"))
        r = _call("browser", {"mode": "open", "url": str(key)})
        assert r.error.endswith("\n" + DENY.format("never"))


# ── 보관함의 수명·격리 ─────────────────────────────────────


class TestStore:
    def test_take_empties_and_keeps_order(self):
        human_note.clear()
        human_note.record("approving", "a")
        human_note.record("approving", "")
        human_note.record("denying", "b")
        assert human_note.take() == [APPROVE.format("a"), DENY.format("b")]
        assert human_note.take() == []

    def test_stale_note_does_not_leak_into_the_next_call(self, ws, monkeypatch):
        """앞 호출이 기록만 남기고 꺼내지 못했다(중단·예외) — 다음 호출 결과에 새지 않는다."""
        human_note.record("approving", "from an aborted call")
        r = _call("shell", {"command": "echo clean"})
        assert r.output == "clean"
        human_note.record("denying", "also stale")
        _, outside = ws
        target = outside / "e.txt"
        target.write_text("a\n")
        _answer(monkeypatch, ("y", ""))
        r, parts = _bridge().dispatch_edit_batch(
            str(target), [{"op": "replace", "pos": _ref(1, "a"), "lines": ["A"]}]
        )
        assert r.success and "User note" not in r.output + parts[-1]["content"]

    def test_threads_do_not_see_each_others_notes(self):
        barrier = threading.Barrier(2)
        got: dict[str, list[str]] = {}

        def run(name):
            human_note.clear()
            human_note.record("approving", name)
            barrier.wait()  # 둘 다 기록한 뒤에 꺼낸다
            got[name] = human_note.take()

        ts = [threading.Thread(target=run, args=(n,), name=n) for n in ("A", "B")]
        for t in ts:
            t.start()
        for t in ts:
            t.join(5)
        assert got == {"A": [APPROVE.format("A")], "B": [APPROVE.format("B")]}

    def test_parallel_bridges_keep_their_own_lines(self, ws, monkeypatch):
        """병렬 위임처럼 워커 스레드 둘이 각자 bridge 로 확인 있는 호출을 돌린다 — B 의
        확인·기록은 A 가 명령을 도는 사이에 일어나지만 A 는 자기 줄만 받는다."""
        rr = _answer(monkeypatch, {"A": ("y", "note for A"), "B": ("y", "note for B")})
        out: dict = {}

        def run(name, cmd):
            out[name] = _bridge()._dispatch_tool_with_hooks("shell", {"command": cmd})

        a = threading.Thread(
            target=run, args=("A", "rm -f a.txt; sleep 0.6; echo A"), name="A"
        )
        a.start()
        assert rr.answered.wait(5)
        b = threading.Thread(target=run, args=("B", "rm -f b.txt; echo B"), name="B")
        b.start()
        b.join(5)
        a.join(5)
        assert out["A"].output == "A\n" + APPROVE.format("note for A")
        assert out["B"].output == "B\n" + APPROVE.format("note for B")


class TestSourceGuard:
    def test_tools_do_not_attach_notes(self):
        """붙이기는 tool_bridge 한 곳 — 도구 패키지에 옛 붙이기·거절 문구 안 코멘트가 없다."""
        tools_dir = Path(human_note.__file__).parent
        for p in tools_dir.rglob("*.py"):
            src = p.read_text(encoding="utf-8")
            for banned in ("approval_note", "with_approval_note", "User said:"):
                assert banned not in src, f"{p.name}: {banned}"

    def test_bridge_attaches_in_both_execution_paths(self):
        import inspect

        src = inspect.getsource(ToolBridge._dispatch_tool_with_hooks)
        assert "human_note.clear()" in src and "human_note.take()" in src
        src = inspect.getsource(ToolBridge.dispatch_edit_batch)
        assert "human_note.clear()" in src and "human_note.take()" in src
