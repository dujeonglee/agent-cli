"""`monitor` 문구 정리와 `run` 삭제 (v10.79.0, docs/monitor/DESIGN.md §13,
docs/tool-review/TRACKER.md).

고정하는 계약: 설명의 첫머리가 "작업 종료는 이미 통지된다" 를 말한다 · 대문자 강조와
`run` 문장이 없다 · 모델이 보는 문구(설명·인자·보고 머리줄·목록·오류)는 전부 인자
짝(`file` + `pattern`)이고 내부 이름(`match`·`silence`·`command ·`)과 중괄호 모양이
없다 · 초는 "2h"·"1h49m" · `run` 을 주면 한 릴리스 동안 명시 거절 · 옛 monitors.json
의 `run` 키는 읽지 않는다.
"""

from __future__ import annotations

import json
import re

from agent_cli.monitor import MonitorRegistry, build
from agent_cli.monitor.conditions import SHAPES
from agent_cli.monitor.registry import describe_previous
from agent_cli.monitor.runtime import set_monitor_registry
from agent_cli.tools.base import RunContext
from agent_cli.tools.monitor_tool import RUN_REMOVED, MonitorTool
from tests.monitor_delivery import RecordingDelivery

_INTERNAL = re.compile(r"\bmatch ·|\bsilence ·|\bcommand ·|\{command, every\}|\{file, ")


class TestDescription:
    def test_first_sentence_says_jobs_already_report(self):
        d = MonitorTool.description
        assert d.startswith(
            "Watch a condition in the background and report to you when it fires. A "
            "`shell` job started with `background` true already reports when it "
            "ends; a monitor is for other conditions"
        )
        assert "for long-running work you started with `shell`" not in d

    def test_no_caps_emphasis_no_run_no_internal_names(self):
        d = MonitorTool.description
        for bad in (
            "ONE",
            "NEW",
            "STOPS",
            "never mix",
            "`run`",
            "do NOT",
            "silent action",
        ):
            assert bad not in d, bad
        assert not _INTERNAL.search(d)
        assert '`deadline` defaults to "2h" (from "60s" to "24h")' in d
        assert "`schedule` is and does" in d

    def test_parameters_have_no_run_and_speak_in_pairs(self):
        props = MonitorTool.parameters["properties"]
        assert "run" not in props
        for name, p in props.items():
            desc = p["description"]
            assert not desc.startswith("condition"), name
            assert not _INTERNAL.search(desc), name
        assert props["every"]["description"] == (
            'Interval for `command`, such as "60s" or "5m" (at least "60s").'
        )
        assert props["silent"]["description"].startswith("With `pattern`:")

    def test_shapes_are_argument_pairs(self):
        assert SHAPES == "`command` + `every`, `file` + `pattern`, or `file` + `idle`"


class TestRunRefusal:
    def test_run_is_refused_explicitly(self):
        tool = MonitorTool()
        err = tool.validate({"mode": "add", "file": "x", "pattern": "y", "run": "true"})
        assert err == RUN_REMOVED
        assert err == (
            "monitor: `run` is no longer an argument — act on the report with "
            "`shell` instead."
        )
        # list/delete 에 붙여도 거절 — 어디서든 실행되지 않는다는 것을 알려야 한다
        assert tool.validate({"mode": "list", "run": "kill 1"}) == RUN_REMOVED
        assert (
            tool.validate({"mode": "add", "file": "x", "pattern": "y", "run": ""})
            is None
        )

    def test_old_monitors_json_run_key_is_ignored(self, tmp_path):
        (tmp_path / "monitors.json").write_text(
            json.dumps(
                {
                    "monitors": [
                        {
                            "id": "mon-1",
                            "desc": "`file` + `pattern` on a",
                            "run": "kill 9",
                        }
                    ]
                }
            )
        )
        assert describe_previous(tmp_path) == ["[mon-1] `file` + `pattern` on a"]

    def test_shell_less_loop_message_names_only_command(self, tmp_path):
        reg = MonitorRegistry()
        reg.stop()
        reg.deliver = lambda *a, **k: ""
        set_monitor_registry(reg)
        try:
            r = MonitorTool()._run(
                {"mode": "add", "command": "true", "every": "60s"},
                ctx=RunContext(tools_available=frozenset({"monitor"})),
            )
            assert r.error == (
                "monitor: `command` conditions need the `shell` tool, which this agent "
                "does not have — use a `file` + `pattern` or `file` + `idle` condition"
            )
        finally:
            set_monitor_registry(None)


class TestReportsListsAndErrors:
    def _reg(self):
        reg = MonitorRegistry()
        reg.deliver = RecordingDelivery()
        reg.stop()
        set_monitor_registry(reg)
        return reg

    def test_describe_uses_argument_pairs(self, tmp_path):
        log = tmp_path / "a.log"
        assert build({"file": str(log), "pattern": "E"}).describe() == (
            f"`file` + `pattern` on {log}"
        )
        assert build({"file": str(log), "idle": "5m"}).describe() == (
            f"`file` + `idle` on {log} for 5m"
        )
        assert build({"command": "test -f out/done", "every": "90s"}).describe() == (
            "`command` + `every` running `test -f out/done` every 1m30s"
        )

    def test_add_and_list_say_durations_not_seconds(self, tmp_path):
        reg = self._reg()
        try:
            log = tmp_path / "a.log"
            log.write_text("")
            tool = MonitorTool()
            r = tool.run({"mode": "add", "file": str(log), "pattern": "E"})
            assert r.success
            assert (
                f"watching `file` + `pattern` on {log} for 2h. It reports to you when it fires; there is nothing to check in the meantime."
                in r.output
            )
            assert "7200s" not in r.output and "do NOT poll" not in r.output
            mon = reg.list_all()[0]
            mon.deadline_at = __import__("time").time() + 6540
            lst = tool.run({"mode": "list"}).output
            assert f"- [{mon.id}] `file` + `pattern` on {log} · 1h49m left" in lst
            assert "run=" not in lst and "s left" not in lst
        finally:
            set_monitor_registry(None)

    def test_errors_name_the_argument_pair(self, tmp_path):
        self._reg()
        try:
            tool = MonitorTool()
            cases = {
                ("file", "x", "idle", "0"): "monitor: `idle` must be longer than zero",
                (
                    "file",
                    "x",
                    "pattern",
                    "([",
                ): "monitor: `pattern` '([' is not a valid regex",
                ("file", "x", "every", "60s", "pattern", "a"): (
                    "monitor: give one condition — `command` + `every`, `file` + "
                    "`pattern`, or `file` + `idle`; yours mixes `every`, `file`, `pattern`"
                ),
                ("file", "x"): (
                    "monitor: the condition must be one of `command` + `every`, `file` + "
                    "`pattern`, or `file` + `idle` — yours has only `file`"
                ),
                (
                    "file",
                    "x",
                    "pattern",
                    "a",
                    "deadline",
                    "soon",
                ): "monitor: `deadline` — ",
            }
            for flat, want in cases.items():
                args = {"mode": "add", **dict(zip(flat[::2], flat[1::2]))}
                r = tool._run(args)
                assert not r.success and r.error.startswith(want), (args, r.error)
                assert not _INTERNAL.search(r.error) and "match:" not in r.error
        finally:
            set_monitor_registry(None)

    def test_report_head_uses_the_pair(self, tmp_path):
        reg = self._reg()
        try:
            import time

            log = tmp_path / "w.log"
            log.write_text("")
            reg.add(
                build({"file": str(log), "pattern": "X"}), owner="main", deadline_s=60
            )
            reg.tick(time.time())
            log.write_text("X\n")
            reg.tick(time.time())
            (rep,) = reg.deliver.take()
            assert (
                rep.startswith("🔔 monitor mon-")
                and f"(`file` + `pattern` on {log})" in rep
            )
        finally:
            set_monitor_registry(None)
