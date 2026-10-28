"""`python` 도구 (v10.66.0, docs/python-tool/DESIGN.md) — 상태가 남는 파이썬 실행.

고정하는 계약: 상태가 호출 사이에 남는다 · 마지막 식의 값 · 출력 순서(print 와
자식 프로세스가 한 흐름) · 상태 요약 줄(쓸모 순·핸들 제외·재사용 힌트) · 예외는
실패 관찰이고 상태는 산다 · 타임아웃은 SIGINT 로 멈추고 상태를 지킨다, 시그널을
무시하는 코드만 kill 되고 소실을 말한다 · reset · 소유자 분리 · 봉쇄 · 레지스트리
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
from agent_cli.tools.python_tool import (
    STATE_HINT,
    PythonTool,
    literal_paths,
    state_line,
)
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
    def test_timeout_interrupts_and_keeps_state(self, reg):
        """노트북의 Ctrl-C: 루프가 모으던 결과가 남고 다음 호출이 이어 간다 (벤치
        실측 2026-10-22: 95프레임 OCR 루프가 120s 에 죽어 결과를 잃고 다시 돌렸다)."""
        assert _run("1").success
        pid = reg.get("main").pid
        r = _run(
            "import time\nresults = {}\nfor i in range(100):\n"
            "    results[i] = i\n    time.sleep(0.05)\n",
            timeout=1,
        )
        assert not r.success
        assert "interrupted after 1s" in r.error and "state is kept" in r.error
        assert "KeyboardInterrupt" in r.error
        assert "results (dict," in r.error
        assert reg.get("main").alive and reg.get("main").pid == pid
        r2 = _run("len(results) > 5")
        assert r2.success and "=> True" in r2.output

    def test_interrupt_reaches_subprocesses_too(self, reg):
        r = _run(
            "import subprocess\nsubprocess.run(['sleep', '30'])\nprint('REACHED')",
            timeout=1,
        )
        assert not r.success and "interrupted after 1s" in r.error
        assert "REACHED" not in r.error and reg.get("main").alive

    def test_code_ignoring_the_interrupt_is_killed_and_lost_names_reported(self, reg):
        assert _run("held = [1, 2, 3]").success
        pid = reg.get("main").pid
        r = _run(
            "import signal, time\nsignal.signal(signal.SIGINT, signal.SIG_IGN)\n"
            "time.sleep(10)",
            timeout=1,
        )
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
        assert not r.success and "use `shell` instead" in r.error

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

        # v10.73.0: 경로 선택 한 문장 — 상태가 남는다는 전제는 그대로 (tests/test_python_guide.py)
        assert "`python`" in TASK_GUIDELINES and "state persists" in TASK_GUIDELINES

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
        rows = [[f"v{i}", "int", "scalar"] for i in range(25)]
        s = state_line(rows)
        assert s.count("(int)") == 20 and s.endswith("+5 more")

    def test_state_line_hints_reuse_only_when_data_is_held(self):
        assert STATE_HINT not in state_line([["n", "3", "scalar"]])
        assert state_line([["texts", "dict, 2 keys", "data"]]).endswith(STATE_HINT)

    def test_state_is_ordered_by_usefulness_and_drops_handles(self, reg, tmp_path):
        (tmp_path / "t.txt").write_text("x" * 300)
        r = _run(
            "i = 3\nf = open('t.txt')\ntxt = f.read()\ndef parse(x): return x\n"
            "rows = [1, 2]\nname = 'short'\n"
        )
        assert r.success
        line = next(ln for ln in r.output.splitlines() if ln.startswith("state:"))
        names = [p.split(" (")[0] for p in line[len("state: ") :].split(" · ")]
        names[-1] = names[-1].split(" — ")[0]
        assert names == ["txt", "rows", "parse", "i", "name"]  # 데이터→코드→스칼라
        assert "f (" not in line and line.endswith(STATE_HINT)


class TestWorkerUnit:
    def test_worker_can_be_killed_before_start(self, tmp_path):
        w = PyWorker(tmp_path)
        w.kill()
        assert not w.alive and w.pid is None


class TestMissingModuleHint:
    """ModuleNotFoundError 관찰은 **이 인터프리터** 경로와 거기에 설치하는 명령을
    같이 준다 (v10.73.0) — pyguide r2 financial 은 PATH 의 pip·다른 cpython 에 깔고
    도구에서 import 가 안 되자 shell 로 돌아갔다."""

    def test_hint_names_the_interpreter_and_the_package(self):
        from agent_cli.tools.python_tool import missing_module_hint

        h = missing_module_hint(
            "Traceback…\nModuleNotFoundError: No module named 'PIL'", "/v/bin/python"
        )
        assert "`/v/bin/python`" in h
        assert "`/v/bin/python -m pip install Pillow`" in h
        assert "ensurepip" in h
        h2 = missing_module_hint(
            "ModuleNotFoundError: No module named 'pdfminer.high_level'", "/p"
        )
        assert "-m pip install pdfminer`" in h2
        assert missing_module_hint("ZeroDivisionError: division by zero", "/p") == ""
        assert missing_module_hint(None, "/p") == ""

    def test_live_observation_carries_the_hint(self, reg):
        from agent_cli.pyworker import interpreter

        r = _run("import no_such_module_4711")
        assert not r.success
        assert "ModuleNotFoundError" in r.error
        assert f"`{interpreter()} -m pip install no_such_module_4711`" in r.error
        assert "state:" in r.error  # 상태줄은 그대로 맨 끝
        assert r.error.rstrip().endswith("state: (empty)")
