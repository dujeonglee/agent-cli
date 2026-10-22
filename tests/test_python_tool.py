"""`python` 도구 (v10.65.0, docs/python-tool/DESIGN.md) — 상태가 남는 파이썬 실행.

고정하는 계약: 상태가 호출 사이에 남는다 · 마지막 식의 값 · 출력 순서(print 와
자식 프로세스가 한 흐름) · 상태 요약 줄 · 예외는 실패 관찰이고 상태는 산다 ·
타임아웃은 워커를 죽이고 소실을 말한다 · reset · 소유자 분리 · 봉쇄 · 레지스트리
없으면 광고 안 됨 · 자리(shell 바로 뒤) · 스냅샷 훅 · 가이드라인.
"""

from __future__ import annotations

import pytest

from agent_cli.pyworker import (
    PyWorker,
    PyWorkerRegistry,
    get_py_registry,
    set_py_registry,
)
from agent_cli.tools.base import RunContext
from agent_cli.tools.python_tool import PythonTool, literal_paths, state_line
from agent_cli.tools.registry import _BUILTIN_ORDER, TOOLS


@pytest.fixture
def reg(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AGENT_CLI_WORKSPACE_CONFINE", "0")
    r = PyWorkerRegistry(tmp_path)
    set_py_registry(r)
    yield r
    r.stop_all()
    set_py_registry(None)


def _run(code: str, **extra):
    return PythonTool().run({"code": code, **extra}, ctx=RunContext(owner="main"))


class TestState:
    def test_variables_survive_between_calls(self, reg):
        assert _run("texts = {'a': 1, 'b': 2}").success
        r = _run("def parse(k):\n    return texts[k] * 10\nparse('b')")
        assert r.success
        assert "=> 20" in r.output
        assert "state: texts (dict, 2 keys) · parse (function)" in r.output

    def test_last_expression_value_and_no_output(self, reg):
        assert "=> 3" in _run("1 + 2").output
        r = _run("x = 5")
        assert "(no output)" in r.output and "state: x (5)" in r.output

    def test_print_and_subprocess_output_keep_their_order(self, reg):
        r = _run(
            "import subprocess\nprint('one')\n"
            "subprocess.run(['echo', 'two'])\nprint('three')"
        )
        assert r.output.split("\n")[:3] == ["one", "two", "three"]

    def test_exception_is_a_failure_but_state_survives(self, reg):
        assert _run("keep = 1").success
        r = _run("1 / 0")
        assert not r.success
        assert "ZeroDivisionError" in r.error
        assert "_pyworker_child" not in r.error  # 우리 프레임은 빠진다
        assert "state: keep (1)" in r.error
        assert "=> 1" in _run("keep").output

    def test_input_does_not_block(self, reg):
        r = _run("input('x')")
        assert not r.success and "EOFError" in r.error

    def test_reset_starts_fresh(self, reg):
        _run("a = 1")
        r = _run("'a' in dir()", reset=True)
        assert "=> False" in r.output and "state: (empty)" in r.output

    def test_long_value_is_cut(self, reg):
        r = _run("'y' * 100000")
        assert "(100,002 chars)" in r.output
        assert len(r.output) < 6000


class TestTimeout:
    def test_timeout_kills_and_reports_lost_names(self, reg):
        assert _run("held = [1, 2, 3]").success
        pid = reg.get("main").pid
        r = _run("import time\ntime.sleep(5)", timeout=1)
        assert not r.success
        assert "timed out after 1s" in r.error and "it held: held" in r.error
        assert not reg.get("main").alive
        # 다음 호출이 새 워커를 띄우고 소실을 말한다
        r2 = _run("2")
        assert r2.success and reg.get("main").pid != pid
        assert "(fresh interpreter — the previous state was lost: held)" in r2.output


class TestOwnersAndLifecycle:
    def test_owners_have_separate_interpreters(self, reg):
        t = PythonTool()
        t.run({"code": "who = 'main'"}, ctx=RunContext(owner="main"))
        r = t.run({"code": "'who' in dir()"}, ctx=RunContext(owner="agent:x"))
        assert "=> False" in r.output
        assert reg.drop_owner("agent:x") == 1
        assert reg.drop_owner("agent:x") == 0
        assert reg.get("main").alive

    def test_stop_all_kills_everything(self, reg):
        _run("1")
        w = reg.get("main")
        assert w.alive
        assert reg.stop_all() == 1
        assert not w.alive and reg.closed
        r = _run("1")
        assert not r.success and "not available" in r.error

    def test_no_registry_means_not_available(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        set_py_registry(None)
        assert get_py_registry() is None
        r = _run("1")
        assert not r.success and "use shell instead" in r.error

    def test_worker_death_is_reported_and_recovered(self, reg):
        _run("v = 1")
        r = _run("import os\nos._exit(3)")
        assert not r.success and "exited unexpectedly" in r.error
        assert "=> 1" in _run("1").output


class TestFigures:
    def test_open_figures_are_saved_and_attached(self, reg):
        pytest.importorskip("matplotlib")
        r = _run(
            "import matplotlib\nmatplotlib.use('Agg')\n"
            "import matplotlib.pyplot as plt\nplt.plot([0, 1], [0, 1])\n'done'"
        )
        assert r.success, r.error
        assert len(r.images) == 1
        assert r.images[0]["path"].startswith(".agent-cli/tmp/python/fig-")
        assert "[image: .agent-cli/tmp/python/fig-" in r.output
        # 저장 뒤 닫힌다 — 다음 호출엔 figure 가 없다
        assert _run("'again'").images == []


class TestGuard:
    def test_literal_paths(self):
        assert literal_paths("open('/etc/passwd'); x = \"../up\"; y = 'rel/p'") == [
            "/etc/passwd",
            "../up",
        ]

    def test_outside_path_is_refused_when_nobody_can_prompt(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("AGENT_CLI_WORKSPACE_CONFINE", raising=False)
        r = PyWorkerRegistry(tmp_path)
        set_py_registry(r)
        try:
            res = _run("open('/etc/hosts').read()")
            assert not res.success and "outside the workspace" in res.error
        finally:
            r.stop_all()
            set_py_registry(None)


class TestRegistration:
    def test_right_after_shell(self):
        order = list(_BUILTIN_ORDER)
        assert order.index("python") == order.index("shell") + 1
        assert isinstance(TOOLS["python"], PythonTool)
        assert TOOLS["python"].requires_handler == "python"

    def test_validate_and_summary(self):
        t = PythonTool()
        assert t.validate({"code": "  "}) is not None
        assert t.validate({"code": "x"}) is None
        assert t.summary_arg({"code": "a\nb"}) == "a\nb"

    def test_snapshot_hook_treats_python_as_mutating(self):
        import importlib.util
        from pathlib import Path

        p = Path("agent_cli/hooks/builtin/10_snapshot.py")
        spec = importlib.util.spec_from_file_location("snap_hook", p)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        assert "python" in mod.MUTATING_TOOLS

    def test_guideline_mentions_python(self):
        from agent_cli.prompts.system_prompt import TASK_GUIDELINES

        assert "`python`" in TASK_GUIDELINES and "keeps state" in TASK_GUIDELINES

    def test_resource_follows_env(self, monkeypatch, tmp_path):
        from agent_cli.runtime import build_py_registry, python_resource

        monkeypatch.setenv("AGENT_CLI_PYTHON_TOOL", "0")
        assert build_py_registry(tmp_path) is None
        assert python_resource() is None
        monkeypatch.delenv("AGENT_CLI_PYTHON_TOOL")
        r = build_py_registry(tmp_path)
        try:
            assert python_resource() is r
        finally:
            r.stop_all()
            set_py_registry(None)
            assert python_resource() is None

    def test_state_line_caps_at_twenty(self):
        rows = [[f"v{i}", "int"] for i in range(25)]
        s = state_line(rows)
        assert s.count("(int)") == 20 and s.endswith("+5 more")


class TestWorkerUnit:
    def test_worker_can_be_killed_before_start(self, tmp_path):
        w = PyWorker(tmp_path)
        w.kill()
        assert not w.alive and w.pid is None
