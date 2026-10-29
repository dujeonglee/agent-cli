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
        assert not r.success
        assert "exited while running this code — its state is gone" in r.error
        assert "=> 1" in _run("1").output

    def test_death_with_nothing_held_does_not_claim_lost_state(self, reg):
        # 첫 셀이 프로세스를 끝냄 — 인터프리터 문제(시작 실패)가 아니고, 잃은 것도 없다.
        r = _run("import os\nos._exit(3)")
        assert not r.success
        assert "exited while running this code" in r.error
        assert "state is gone" not in r.error
        assert "3.8" not in r.error


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


class TestImportPath:
    """v10.77.1 — 워커의 import 경로는 대화형 python3 와 같다: 지금 폴더가 맨 앞이고,
    하니스의 도구 폴더는 경로에 없다(라이브 GLM S4: 방금 쓴 fib.py 를 못 불렀다)."""

    def test_a_module_in_the_working_folder_imports(self, reg, tmp_path):
        (tmp_path / "fibmod_4711.py").write_text("def fib(n):\n    return n\n")
        r = _run("import fibmod_4711\nfibmod_4711.fib(7)")
        assert r.success, r.error
        assert "7" in r.output

    def test_harness_tool_modules_are_not_importable_by_bare_name(self, reg):
        r = _run("import sys\nany(p.endswith('tools') for p in sys.path)")
        assert r.success and "False" in r.output
        r = _run("import shell")
        assert not r.success and "ModuleNotFoundError" in r.error

    def test_the_path_follows_chdir_like_the_repl(self, reg, tmp_path):
        sub = tmp_path / "sub"
        sub.mkdir()
        (sub / "inner_4711.py").write_text("X = 3\n")
        r = _run("import os\nos.chdir('sub')\nimport inner_4711\ninner_4711.X")
        assert r.success, r.error
        assert "3" in r.output


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
        assert "-m pip install pdfminer.six`" in h2
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


class TestMainModule:
    """셀은 진짜 ``__main__`` 모듈에서 돈다 (v10.78.0) — ``sys.modules["__main__"]``
    로 이름을 찾는 것들(pickle·fork Pool·unittest·doctest·dataclass)이 셀의 이름을
    본다. r2b video: 남은 OCR 을 Pool 로 나누다 PicklingError → shell 로 이탈."""

    def test_cell_functions_pickle(self, reg):
        _run("def double(x):\n    return 2 * x")
        r = _run("import pickle\npickle.loads(pickle.dumps(double))(21)")
        assert r.success, r.error
        assert "=> 42" in r.output

    @pytest.mark.skipif(
        not hasattr(__import__("os"), "fork"), reason="fork start method only"
    )
    def test_fork_pool_runs_cell_functions(self, reg):
        _run("def double(x):\n    return 2 * x")
        r = _run(
            "import multiprocessing as mp\n"
            "with mp.get_context('fork').Pool(2) as p:\n"
            "    out = p.map(double, [1, 2, 3])\n"
            "out",
            timeout=60,
        )
        assert r.success, r.error
        assert "=> [2, 4, 6]" in r.output

    def test_spawn_pool_is_cut_by_the_timeout_and_state_survives(self, reg):
        # 알려진 한계(대화형 python 과 같다): spawn 자식은 셀의 ``__main__`` 을 다시
        # import 하지 못해 Pool 이 멈춘다 — 타임아웃이 끊고 상태는 산다.
        _run("v = 1\ndef double(x):\n    return 2 * x")
        r = _run(
            "import multiprocessing as mp\n"
            "with mp.get_context('spawn').Pool(1) as p:\n"
            "    p.map(double, [1])",
            timeout=3,
        )
        assert not r.success
        assert "interrupted after 3s" in r.error
        assert "=> 1" in _run("v").output

    def test_unittest_main_finds_cell_tests(self, reg):
        r = _run(
            "import unittest\n"
            "class T(unittest.TestCase):\n"
            "    def test_one(self):\n"
            "        self.assertEqual(1, 1)\n"
            "unittest.main(argv=['x'])"
        )
        assert "Ran 1 test" in r.error
        assert "SystemExit(0)" in r.error

    def test_state_line_holds_only_cell_names(self, reg):
        r = _run("v = 1")
        assert r.output.rstrip().endswith("state: v (1)")

    def test_name_is_main(self, reg):
        assert "=> '__main__'" in _run("__name__").output


class TestSyntaxError:
    """실행 전의 SyntaxError 는 하니스의 ``ast.py`` 프레임 없이 (v10.78.0) — 벤치
    SyntaxError 27건 중 26건이 그 프레임으로 시작했다."""

    def test_parse_error_shows_only_the_cell_lines(self, reg):
        r = _run("x = (1,\ny = 2")
        assert not r.success
        assert "ast.py" not in r.error
        assert "Traceback" not in r.error
        assert 'File "<python>", line' in r.error
        assert "SyntaxError" in r.error

    def test_compile_stage_error_is_clean_too(self, reg):
        r = _run("return 1")
        assert not r.success
        assert "ast.py" not in r.error
        assert "SyntaxError" in r.error and "outside function" in r.error

    def test_syntax_error_raised_while_running_keeps_the_cell_frame(
        self, reg, tmp_path
    ):
        (tmp_path / "broken_mod.py").write_text("def f(:\n", encoding="utf-8")
        r = _run("import broken_mod")
        assert not r.success
        assert 'File "<python>", line 1' in r.error
        assert "broken_mod.py" in r.error


class TestSystemExit:
    @pytest.mark.parametrize(
        "code, head",
        [
            ("exit()", "SystemExit —"),
            ("import sys\nsys.exit(2)", "SystemExit(2) —"),
            ("import sys\nsys.exit('bad args')", "SystemExit —"),
        ],
    )
    def test_exit_keeps_the_interpreter(self, reg, code, head):
        _run("v = 1")
        r = _run(code)
        assert not r.success
        assert head in r.error
        assert "the interpreter is still running and its state is kept" in r.error
        assert "=> 1" in _run("v").output


class TestInterpreterChoice:
    """인터프리터는 PATH 의 ``python3``, 없으면 ``python`` — 그것뿐 (v10.78.0). 예전엔
    agent-cli 자신의 인터프리터로 떨어졌고, 벤치에서 그게 pip 없는 uv venv 라 워커가
    거기 고정돼 나중에 깔린 시스템 python3 의 패키지를 못 봤다."""

    @staticmethod
    def _bin(tmp_path, monkeypatch, **links):
        import os
        import sys

        d = tmp_path / "bin"
        d.mkdir(exist_ok=True)
        for name, target in links.items():
            p = d / name
            if target == "real":
                p.symlink_to(sys.executable)
            else:  # 시작 인사 전에 죽는 가짜
                p.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
                p.chmod(0o755)
        monkeypatch.setenv("PATH", str(d))
        assert os.environ["PATH"] == str(d)
        return d

    def test_python3_first(self, tmp_path, monkeypatch):
        from agent_cli.pyworker import interpreter

        d = self._bin(tmp_path, monkeypatch, python3="real", python="real")
        assert interpreter() == str(d / "python3")

    def test_python_when_no_python3(self, tmp_path, monkeypatch):
        from agent_cli.pyworker import interpreter

        d = self._bin(tmp_path, monkeypatch, python="real")
        assert interpreter() == str(d / "python")

    def test_none_when_neither(self, tmp_path, monkeypatch):
        from agent_cli.pyworker import interpreter

        self._bin(tmp_path, monkeypatch)
        assert interpreter() is None

    def test_no_interpreter_is_reported_then_found_on_the_next_call(
        self, reg, tmp_path, monkeypatch
    ):
        d = self._bin(tmp_path, monkeypatch)
        r = _run("1")
        assert not r.success
        assert "neither `python3` nor `python` is on PATH" in r.error
        assert "the next `python` call starts on it" in r.error
        import sys

        (d / "python3").symlink_to(sys.executable)
        r = _run("1")
        assert r.success, r.error
        assert "=> 1" in r.output

    def test_interpreter_that_cannot_start_names_its_path(
        self, reg, tmp_path, monkeypatch
    ):
        d = self._bin(tmp_path, monkeypatch, python3="dead")
        r = _run("1")
        assert not r.success
        assert f"`{d / 'python3'}` exited before answering" in r.error
        assert "Python 3.8 or newer" in r.error
        assert "state is gone" not in r.error
