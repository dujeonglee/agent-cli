"""python 사용 가이드의 자리 (v10.73.0, docs/tail/DESIGN.md).

도구 설명(머리)은 **성질**만, 사용 가이드는 **꼬리**에 — 상시 한 줄(경로 선택)
+ 상태 조건부 둘(쓰는 동안 `## Python session` 블록, shell 로 파이썬을 돌린
직후 교정 한 줄). 2026-10-25 A/B: 꼬리 줄을 지우고 설명만 남기자 35B 가
`python3 -c`·heredoc·스크립트 재실행으로 갔다(extract-elf 0/4 호출, financial
.py 재실행으로 결과 0 덮어쓰기).
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from agent_cli.loop import run_loop
from agent_cli.prompts.session_state import (
    RULES_HEADER,
    SESSION_STATE_HEADER,
    SHELL_PYTHON_NOTICE,
    build_session_state,
)
from agent_cli.prompts.system_prompt import TASK_GUIDELINES
from agent_cli.providers.base import LLMResponse
from agent_cli.providers.capabilities import ModelCapabilities
from agent_cli.pyworker import PyWorkerRegistry, set_py_registry
from agent_cli.tools.python_tool import (
    PYTHON_SESSION_HEADER,
    PYTHON_SESSION_RULES,
    PythonTool,
    shell_runs_python,
    tail_block,
)
from tests.loop_ports import TEST_PORTS


def _complete(result: str) -> str:
    return json.dumps([{"action": "complete", "result": result}])


class TestDescriptionIsPropertiesOnly:
    def test_no_usage_rules_in_the_head(self):
        d = PythonTool.description
        assert "keeps state between calls" in d  # 성질 — 꼬리 규칙의 전제
        assert "`state:` line" in d  # 관찰 해석에 필요
        for usage in (
            "Use it for data work",
            "one step",
            "accumulate",
            "Print only",
            "deliverables",
            "coordinates",
        ):
            assert usage not in d, usage

    def test_routing_line_is_one_sentence_in_the_tail(self):
        lines = [ln for ln in TASK_GUIDELINES.splitlines() if "`python`" in ln]
        assert len(lines) == 1
        (ln,) = lines
        # 실행 파일 이름(`python3`)은 모델이 읽는 글에 없다 — 환경마다 다르다
        assert (
            "not Python run from `shell` (an inline `-c`, a heredoc or a script file)"
            in ln
        )
        assert "python3" not in ln
        # 이득이 나는 일의 **성질**로 말한다 — 작업 종류(OCR·parsing…)를 나열하지 않는다
        assert "costly step" in ln and "repeated attempts" in ln
        for task in ("OCR", "parsing", "extraction", "transforming"):
            assert task not in ln and task not in SHELL_PYTHON_NOTICE, task
        # 쓰는 법은 상태 블록으로 갔다
        assert "One call = one step" not in TASK_GUIDELINES
        assert "Plot coordinates" not in TASK_GUIDELINES
        assert "One call = one step" in PYTHON_SESSION_RULES


class TestShellRunsPython:
    @pytest.mark.parametrize(
        "cmd",
        [
            'python3 -c "import struct; print(1)"',
            "python -c 'print(1)'",
            "cd /app && python3 -c 'x'",
            "python3 - <<'EOF'\nprint(1)\nEOF",
            "python3 <<EOF\nprint(1)\nEOF",
            "python3 parse.py",
            "python3 -u /app/scripts/run.py --fast",
            "python3.11 extract.py",
            "ls; python3 a.py | head",
            # 경로가 붙은 인터프리터 — pyguide r2 financial 의 10건 전부 (판정 빈틈이었다)
            '/usr/bin/python3 -c "print(1)"',
            "/root/.local/share/uv/python/cpython-3.12/bin/python -c 'import pdfminer'",
            ".venv/bin/python extract.py",
            "~/py/bin/python3.12 - <<EOF\nprint(1)\nEOF",
        ],
    )
    def test_python_work_through_shell(self, cmd):
        assert shell_runs_python(cmd) is True

    @pytest.mark.parametrize(
        "cmd",
        [
            "python3 -m pip install pytesseract",
            "python3 -m pytest tests/",
            "python3 -m venv .venv",
            "python3 --version",
            "python3 -V",
            "pip install numpy",
            "ls -la",
            "which python3",
            "python3 -m json.tool data.json",
            "/usr/bin/python3 -m pip install x",
            "ls /usr/bin/python* /usr/local/bin/python*",
            "",
        ],
    )
    def test_not_python_work(self, cmd):
        assert shell_runs_python(cmd) is False


class TestTailBlock:
    def test_empty_state_renders_nothing(self):
        assert tail_block([]) == ""

    def test_holding_line_then_rules(self):
        out = tail_block([["texts", "dict, 12 keys", "data"], ["parse", "function"]])
        first, rest = out.split("\n", 1)
        assert first == (
            f"{PYTHON_SESSION_HEADER} — holding: texts (dict, 12 keys) · parse (function)"
        )
        assert rest == PYTHON_SESSION_RULES

    def test_caps_the_list(self):
        state = [[f"v{i}", "int"] for i in range(15)]
        assert "+3 more" in tail_block(state).split("\n", 1)[0]


class _Worker:
    def __init__(self, state, alive=True):
        self._last_state = state
        self.alive = alive


class TestRegistryStateOf:
    def test_no_worker_no_state_and_none_created(self, tmp_path):
        reg = PyWorkerRegistry(tmp_path)
        assert reg.state_of("main") == []
        assert reg._workers == {}

    def test_alive_worker_reports_its_last_state(self, tmp_path):
        reg = PyWorkerRegistry(tmp_path)
        reg._workers["main"] = _Worker([["df", "DataFrame, 3 rows", "data"]])
        assert reg.state_of("main") == [["df", "DataFrame, 3 rows", "data"]]

    def test_dead_worker_is_empty(self, tmp_path):
        reg = PyWorkerRegistry(tmp_path)
        reg._workers["main"] = _Worker([["x", "int"]], alive=False)
        assert reg.state_of("main") == []


class TestSessionStateRendering:
    def test_notice_rides_under_the_turn_line_before_open_items(self):
        out = build_session_state(
            turn=3,
            max_turns=10,
            items="## Open Items\n- u-1 x",
            shell_python_notice=True,
            guidelines="## Task Guidelines\n- r",
        )
        lines = out.split("\n")
        i = lines.index(SESSION_STATE_HEADER)
        assert lines[i + 1].startswith("turn 3/10")
        assert lines[i + 2] == SHELL_PYTHON_NOTICE
        assert out.index(RULES_HEADER) < out.index(SHELL_PYTHON_NOTICE)
        assert out.index(SHELL_PYTHON_NOTICE) < out.index("## Open Items")

    def test_python_block_after_jobs_before_agents(self):
        out = build_session_state(
            turn=1,
            max_turns=5,
            jobs="## Background jobs\n- j",
            python=tail_block([["a", "int"]]),
            agents="## Live Agents\n- none",
        )
        assert (
            out.index("## Background jobs")
            < out.index(PYTHON_SESSION_HEADER)
            < out.index("## Live Agents")
        )

    def test_absent_by_default(self):
        out = build_session_state(turn=3, max_turns=10)
        assert SHELL_PYTHON_NOTICE not in out and PYTHON_SESSION_HEADER not in out
        assert build_session_state() == ""
        assert build_session_state(shell_python_notice=True) != ""


class TestLoopEndToEnd:
    @pytest.fixture
    def caps(self):
        return ModelCapabilities(
            context_window=32768, max_output_tokens=4096, supports_thinking=False
        )

    @pytest.fixture
    def reg(self, tmp_path):
        r = PyWorkerRegistry(tmp_path)
        set_py_registry(r)
        yield r
        set_py_registry(None)

    @staticmethod
    def _recording_provider(*contents: str):
        """호출 **시점**의 마지막 user 메시지를 복사해 둔다 — MagicMock 의
        call_args_list 는 같은 리스트 객체를 가리켜 나중에 보면 뒤 턴의 내용이다."""
        provider = MagicMock()
        tails: list[str] = []
        replies = [LLMResponse(content=c) for c in contents]

        def call(messages=None, **kwargs):
            msgs = messages if messages is not None else kwargs["messages"]
            users = [m for m in msgs if m.get("role") == "user"]
            tails.append(str(users[-1].get("content", "")) if users else "")
            return replies.pop(0)

        provider.call.side_effect = call
        return provider, tails

    def _run(self, provider, caps, tmp_path, monkeypatch):
        from agent_cli.context.manager import ContextManager
        from agent_cli.tools.base import ToolResult
        from agent_cli.tools.registry import TOOLS

        monkeypatch.setattr(
            TOOLS["shell"], "_run", lambda args, **_k: ToolResult(True, output="ok")
        )
        run_loop(
            ports=TEST_PORTS,
            query="q",
            provider=provider,
            capabilities=caps,
            model="m",
            ctx=ContextManager(session_dir=tmp_path),
            max_turns=6,
        )

    def test_shell_python_then_correction_then_gone(
        self, caps, reg, tmp_path, monkeypatch
    ):
        """턴 1 shell `python3 -c` → 턴 2 꼬리에 교정 줄 → 턴 2 는 `ls` → 턴 3
        꼬리엔 없다 → complete."""
        provider, tails = self._recording_provider(
            json.dumps([{"action": "shell", "command": "python3 -c 'print(1)'"}]),
            json.dumps([{"action": "shell", "command": "ls"}]),
            _complete("done"),
        )
        self._run(provider, caps, tmp_path, monkeypatch)
        assert len(tails) == 3
        assert SHELL_PYTHON_NOTICE not in tails[0]
        assert SHELL_PYTHON_NOTICE in tails[1]
        assert SHELL_PYTHON_NOTICE not in tails[2]

    def test_pip_install_is_not_corrected(self, caps, reg, tmp_path, monkeypatch):
        provider, tails = self._recording_provider(
            json.dumps([{"action": "shell", "command": "python3 -m pip install x"}]),
            _complete("done"),
        )
        self._run(provider, caps, tmp_path, monkeypatch)
        assert SHELL_PYTHON_NOTICE not in tails[1]

    def test_no_python_tool_no_notice(self, caps, tmp_path, monkeypatch):
        """레지스트리가 없는 세션엔 `python` 이 광고되지 않으니 교정도 없다."""
        provider, tails = self._recording_provider(
            json.dumps([{"action": "shell", "command": "python3 -c 'print(1)'"}]),
            _complete("done"),
        )
        self._run(provider, caps, tmp_path, monkeypatch)
        assert SHELL_PYTHON_NOTICE not in tails[1]

    def test_session_block_while_state_is_held(self, caps, reg, tmp_path, monkeypatch):
        reg._workers["main"] = _Worker([["texts", "dict, 12 keys", "data"]])
        provider, tails = self._recording_provider(_complete("done"))
        self._run(provider, caps, tmp_path, monkeypatch)
        assert f"{PYTHON_SESSION_HEADER} — holding: texts (dict, 12 keys)" in tails[0]
        assert PYTHON_SESSION_RULES in tails[0]
