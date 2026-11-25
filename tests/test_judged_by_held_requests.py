"""감사와 `run` 의 결과 파일은 그 작업 구간이 든 사람의 요청 번호(`u-`)로 판정한다 (5c-2 A1).

"무엇이 작업 구간을 열었나" 가 아니다. 감사: 성공했고 `u-` 를 하나라도 든 작업 구간. 결과 파일·종료 코드:
argv 질의의 번호를 든 작업 구간의 답. "닫은 번호(`answers`)" 도 아니다 — 모델이 `answers` 를 빠뜨린 성공이 실패로
찍히지 않는다.

장면은 엔진을 대본 공급자로 실제로 돌려 만든다 — 그 결과를 감사자와 콘솔 표면(`run` 의 결과 파일)에 넘긴다.
"""

from __future__ import annotations

import json
import threading

from agent_cli.context.manager import ContextManager
from agent_cli.harness.auditor import SessionAuditor
from agent_cli.loop.ledger import NAG_BUDGET, Ledger
from agent_cli.main import _ConsoleDispatchOutput, _ConsoleSurface
from agent_cli.providers.base import LLMResponse
from agent_cli.providers.capabilities import ModelCapabilities
from agent_cli.tools.result import ToolResult
from tests.loop_ports import make_ports
from tests.loopkit import run_loop

CAPS = ModelCapabilities(
    context_window=32768, max_output_tokens=4096, supports_thinking=False
)
#: argv 질의의 번호
QUERY = "u-1"
#: 예약 발화의 번호 — 사람의 말이지만 argv 질의가 아니다
SCHEDULED = "u-5"
#: 작업이 끝났다는 통지 — 장부에 ``n-`` 항목으로 오른다(사람의 요청이 아니다)
JOB_DONE = {"kind": "job", "success": True, "output": "job #1 finished"}


def call(action: str, **kw) -> str:
    return json.dumps({"action": action, **kw})


class Script:
    """대본 — 다 떨어지면 마지막 줄을 되풀이한다."""

    def __init__(self, *lines: str) -> None:
        self._lines = list(lines)
        self.calls = 0

    def call(self, messages, system, model, capabilities, **kwargs):
        self.calls += 1
        line = self._lines.pop(0) if len(self._lines) > 1 else self._lines[0]
        return LLMResponse(content=line)


class Jobs:
    """`HarnessJobs` 의 표면만 — 띄운 감사를 적는다."""

    stopped = False
    in_flight = 0

    def __init__(self) -> None:
        self.runs: list[tuple] = []

    def run(self, job, label, task):
        self.runs.append((job, label, task))
        return ""


class Session:
    """main 의 작업 구간 하나를 돌리고 그 결과를 감사자·`run` 표면에 넘긴다(펌프의 끝 정리가 하는 두 호출)."""

    def __init__(self, tmp_path) -> None:
        self.ctx = ContextManager(tmp_path / "s", max_context_tokens=20_000)
        self.jobs = Jobs()
        self.auditor = SessionAuditor(self.jobs, self.ctx, enabled=True)
        self.out = tmp_path / "result.txt"
        self.surface = _ConsoleSurface(
            result_file=str(self.out), dispatch=lambda text, stop, item=None: False
        )
        self.surface.query_id = QUERY

    def segment(self, script: Script, *, opener="query", words=(), mail=(), **attrs):
        """작업 구간 하나. ``opener``: ``"query"``(argv 질의가 연다) · ``"schedule"``(예약 발화가 연다 — 이름 있는
        사람의 말) · ``"mail"``(메일이 연다 — 시작 메시지 없음). ``words`` 는 첫 턴 경계에 서 있는 사람의 말
        (번호, 글), ``mail`` 은 그 경계에 서 있는 통지."""
        queue = [{"message": {"id": rid, "text": text}} for rid, text in words]
        mail = list(mail)
        # 메일이 연 작업 구간은 그 메일을 시작에 싣는다(`Start.opening`) — 없으면 장부에 오르지 않는 통지 하나
        opening = (
            {"mail": dict(mail.pop(0) if mail else {**JOB_DONE, "ledger": "none"})}
            if opener == "mail"
            else None
        )
        queue += [{"mail": dict(m)} for m in mail]

        def take() -> list:
            items = list(queue)
            queue.clear()
            return items

        start = {
            "query": "" if opener == "mail" else "what is 2+2",
            "query_author": "⏰ nightly" if opener == "schedule" else None,
            "query_author_is_user": opener != "mail",
            "query_request_id": {"query": QUERY, "schedule": SCHEDULED, "mail": ""}[
                opener
            ],
        }
        result = run_loop(
            provider=script,
            capabilities=CAPS,
            model="m",
            ctx=self.ctx,
            ports=make_ports(inbox=take),
            opening=opening,
            **start,
            **attrs,
        )
        self.ended(result)
        return result

    def ended(self, result) -> None:
        self.auditor.run_ended(result)
        self.surface.run_ended(result)

    @property
    def audited(self) -> int:
        return len(self.jobs.runs)

    @property
    def file(self) -> str | None:
        return self.out.read_text(encoding="utf-8") if self.out.exists() else None


class TestScenes:
    def test_query_segment_that_never_lists_answers_still_counts(self, tmp_path):
        """되묻기 예산을 다 쓰고 `answers` 없이 끝난 성공 — 결과 파일·종료 0·감사."""
        s = Session(tmp_path)
        script = Script(call("complete", result="four"))
        result = s.segment(script)
        assert result.success and script.calls == NAG_BUDGET + 1
        assert s.file.startswith("four") and s.surface.exit_code == 0
        assert s.audited == 1

    def test_failed_query_segment_is_exit_1_without_a_file_or_an_audit(self, tmp_path):
        s = Session(tmp_path)
        result = s.segment(Script(call("read_file", path="x.txt")), max_turns=1)
        assert not result.success and result.human_requests == (QUERY,)
        assert s.file is None and s.surface.exit_code == 1
        assert s.audited == 0

    def test_query_segment_that_lists_only_another_item_still_counts(self, tmp_path):
        """모델이 질의 말고 도중에 온 다른 말만 `answers` 에 적었다 — 질의의 답으로 친다."""
        s = Session(tmp_path)
        script = Script(call("complete", result="four", answers=["u-2"]))
        result = s.segment(script, words=[("u-2", "and 3+3?")])
        assert result.success
        assert s.file.startswith("four") and s.surface.exit_code == 0
        assert s.audited == 1

    def test_mail_opened_segment_that_takes_a_persons_words(self, tmp_path):
        """메일이 연 작업 구간이 도중에 사람의 말을 받아 답했다 — 사람의 요청을 들었으니 감사한다."""
        s = Session(tmp_path)
        script = Script(call("complete", result="six", answers=["u-2"]))
        result = s.segment(script, opener="mail", words=[("u-2", "3+3?")])
        assert result.success and result.output == "six"
        assert result.human_requests == ("u-2",)
        assert s.audited == 1
        assert s.file is None and s.surface.exit_code == 1  # argv 질의의 답이 아니다

    def test_segment_that_holds_only_mail_is_not_audited(self, tmp_path):
        """통지만 든 작업 구간 — 장부에는 ``n-`` 항목이 오르지만 사람의 요청은 없다."""
        s = Session(tmp_path)
        script = Script(call("complete", result="noted"))
        result = s.segment(script, opener="mail", mail=[JOB_DONE])
        assert result.success and result.human_requests == ()
        assert s.audited == 0 and s.file is None

    def test_mail_opens_first_and_the_query_read_at_its_boundary_is_still_the_query(
        self, tmp_path
    ):
        """도착 순서 — 메일이 argv 질의보다 먼저 서 있어 메일이 연다. 질의는 그 작업 구간의 첫 턴 경계가 읽고, 그 답이
        결과 파일·종료 0 이다(무엇이 열었나로 판정하던 때는 파일 없음·종료 1 이었다)."""
        s = Session(tmp_path)
        script = Script(call("complete", result="four", answers=[QUERY]))
        result = s.segment(
            script, opener="mail", mail=[JOB_DONE], words=[(QUERY, "what is 2+2")]
        )
        assert result.success and result.human_requests == (QUERY,)
        assert s.file.startswith("four") and s.surface.exit_code == 0
        assert s.audited == 1

    def test_a_command_that_was_the_query_is_judged_when_mail_opened_first(
        self, tmp_path
    ):
        """argv 질의가 명령인데 메일이 먼저 서 있었다 — 명령은 메일이 연 작업 구간의 첫 턴 경계에서 실행되고, 그
        답이 결과 파일·종료 0 이다(그 말의 번호로 안다). 그 작업 구간 자체는 통지만 들어 판정을 덮지 않는다."""
        s = Session(tmp_path)

        def dispatch(text, stop, item=None):
            _ConsoleDispatchOutput(report=s.surface.routed).skill_result(
                "review", f"did {text}"
            )
            return True

        s.surface._dispatch = dispatch
        result = run_loop(
            provider=Script(call("complete", result="noted", answers=[])),
            capabilities=CAPS,
            model="m",
            ctx=s.ctx,
            ports=make_ports(
                inbox=iter(
                    [
                        [
                            {
                                "message": {
                                    "id": QUERY,
                                    "text": "/review",
                                    "command": True,
                                }
                            }
                        ],
                    ]
                    + [[]] * 20
                ).__next__,
                route_message=lambda text, item: s.surface.route(text, item=item),
            ),
            query="",
            query_author_is_user=False,
            opening={"mail": {**JOB_DONE, "ledger": "none"}},
        )
        assert s.file == "did /review" and s.surface.exit_code == 0
        s.ended(result)
        assert result.success and result.human_requests == ()
        assert s.file == "did /review" and s.surface.exit_code == 0
        assert s.audited == 0

    def test_scheduled_words_are_audited_but_are_not_the_query(self, tmp_path):
        s = Session(tmp_path)
        script = Script(call("complete", result="nightly done", answers=[SCHEDULED]))
        result = s.segment(script, opener="schedule")
        assert result.success and result.human_requests == (SCHEDULED,)
        assert s.audited == 1
        assert s.file is None and s.surface.exit_code == 1

    def test_query_segment_cannot_end_on_wait_before_it_answers(self, tmp_path):
        """질의(u-1)가 열린 구간의 깨울 것 없는 `wait` 는 거절이다(5d) — 질의 구간이 답 없이 `wait` 로 끝나는 길이
        없다. 거절 뒤 답하면 결과 파일·종료 0·감사 그대로."""
        s = Session(tmp_path)
        script = Script(call("wait"), call("complete", result="four", answers=[QUERY]))
        result = s.segment(script)
        assert (result.success, result.output) == (True, "four")
        assert result.human_requests == (QUERY,) and script.calls == 2
        assert s.audited == 1
        assert s.file == "four" and s.surface.exit_code == 0

    def test_mail_segment_that_ends_on_nothing_to_wait_for_is_not_judged(
        self, tmp_path
    ):
        s = Session(tmp_path)
        # 둘째 줄은 쓰이지 않는다 — 대본은 마지막 줄을 되풀이하므로 거절로 되돌아가면 `wait` 되풀이 대신 이 줄로 끝난다
        result = s.segment(
            Script(call("wait"), call("complete", result="x")), opener="mail"
        )
        assert (result.success, result.output, result.human_requests) == (True, "", ())
        assert s.audited == 0 and s.file is None and s.surface.exit_code == 1

    def test_a_stopped_segment_still_says_what_it_held(self, tmp_path):
        """어떻게 끝났든 칸은 실린다 — 정지로 끝난 작업 구간."""
        stop = threading.Event()
        stop.set()
        s = Session(tmp_path)
        result = s.segment(Script(call("complete", result="x")), stop_event=stop)
        assert not result.success and result.human_requests == (QUERY,)
        assert s.audited == 0 and s.file is None


def _result(output: str, *held: str, ok: bool = True) -> ToolResult:
    """작업 구간 하나의 결과 — ``held`` 는 그 구간이 든 사람의 요청 번호."""
    return ToolResult(ok, output=output, human_requests=tuple(held))


class LeftAJob:
    """`JobRegistry` 의 표면만 — 그 작업 구간이 작업을 남겼는가를 답한다."""

    def __init__(self) -> None:
        self.left = False

    def running(self):
        return ["job"] if self.left else []

    def started_since(self, ts):
        return []


class TestRunSurface:
    """`run` 의 결과 파일·종료 코드 — 표면이 결과의 번호만 보고 판정한다."""

    def _surface(self, tmp_path):
        out = tmp_path / "result.txt"
        jobs = LeftAJob()
        s = _ConsoleSurface(
            result_file=str(out),
            dispatch=lambda text, stop, item=None: False,
            jobs=jobs,
        )
        s.query_id = QUERY
        return s, out, jobs

    def test_words_that_are_not_the_query_do_not_touch_the_file(self, tmp_path):
        """`run` 도중 친 줄이 **연** 작업 구간(친 줄이 읽히기 전에 앞 작업 구간이 멈춘 경우)의 답은
        argv 질의의 답이 아니다 — 파일도 종료 코드도 그대로."""
        s, out, _ = self._surface(tmp_path)
        s.run_ended(_result("the answer", QUERY))
        s.run_ended(_result("answer to a typed line", "u-7"))
        assert out.read_text(encoding="utf-8") == "the answer" and s.exit_code == 0
        s.run_ended(_result("typed line failed", "u-8", ok=False))
        assert s.exit_code == 0

    def test_a_wake_run_continues_the_query_that_left_a_job(self, tmp_path):
        s, out, jobs = self._surface(tmp_path)
        jobs.left = True
        s.run_ended(_result("still waiting on job #1", QUERY))
        jobs.left = False
        s.run_ended(_result("14 passed"))  # 사람의 요청이 없는 깨우기 런
        assert out.read_text(encoding="utf-8") == "14 passed" and s.exit_code == 0
        s.run_ended(_result("later chatter"))  # 사슬이 닫혔다
        assert out.read_text(encoding="utf-8") == "14 passed"

    def test_a_wake_run_that_takes_a_persons_words_is_not_a_wake_run(self, tmp_path):
        """질의가 작업을 남겨 사슬이 열려 있어도, 깨어난 작업 구간이 도중에 사람의 말(예약 발화·친 줄)을
        받았으면 그 답은 질의의 나머지 답이 아니다 — 판정하지 않고 사슬도 그대로 둔다."""
        s, out, jobs = self._surface(tmp_path)
        jobs.left = True
        s.run_ended(_result("still waiting on job #1", QUERY))
        jobs.left = False
        s.run_ended(_result("answered the nightly prompt too", SCHEDULED))
        assert out.read_text(encoding="utf-8") == "still waiting on job #1"
        s.run_ended(_result("answered and failed", SCHEDULED, ok=False))
        assert s.exit_code == 0
        s.run_ended(_result("14 passed"))  # 사슬은 아직 열려 있다
        assert out.read_text(encoding="utf-8") == "14 passed"

    def test_the_query_held_with_other_words_is_still_the_query(self, tmp_path):
        s, out, _ = self._surface(tmp_path)
        s.run_ended(_result("both answered", "u-0", QUERY))
        assert out.read_text(encoding="utf-8") == "both answered" and s.exit_code == 0

    def test_route_judges_the_command_that_was_the_query_by_its_id(self, tmp_path):
        """argv 질의가 명령(`run "/skill"`)이면 라우팅이 답한다 — 그 말의 번호가 질의의 번호일 때만."""
        out = tmp_path / "result.txt"

        def dispatch(text, stop, item=None):
            _ConsoleDispatchOutput(report=s.routed).skill_result("x", f"did {text}")
            return True

        s = _ConsoleSurface(result_file=str(out), dispatch=dispatch)
        s.query_id = QUERY
        s.route("/other", item={"id": "u-7", "text": "/other"})
        s.route("/bare")
        assert not out.exists() and s.exit_code == 1
        s.route("/x", item={"id": QUERY, "text": "/x"})
        assert out.read_text(encoding="utf-8") == "did /x" and s.exit_code == 0

    def test_a_command_routed_while_the_query_skill_waits_does_not_take_its_verdict(
        self, tmp_path
    ):
        """질의였던 스킬이 묻고 기다리는 동안 친 `/answer` 는 그 안에서 라우팅된다(6b) — 그 결과는 질의의 답이 아니고,
        스킬이 끝나며 낸 결과가 질의의 판정이다."""
        out = tmp_path / "result.txt"

        def dispatch(text, stop, item=None):
            report = _ConsoleDispatchOutput(report=s.routed)
            if text == "/demo":
                s.route("/answer q-1 x", item={"id": "u-9", "text": "/answer q-1 x"})
                report.skill_result("demo", "GOT x")
            else:
                report.agent_dispatch_result("answered q-1", True)
            return True

        s = _ConsoleSurface(result_file=str(out), dispatch=dispatch)
        s.query_id = QUERY
        s.route("/demo", item={"id": QUERY, "text": "/demo"})
        assert out.read_text(encoding="utf-8") == "GOT x" and s.exit_code == 0


class TestLedger:
    def test_human_request_ids_are_the_u_ids_in_arrival_order(self):
        lg = Ledger()
        lg.add_request("u-3", "dj", "A")
        lg.add_request("n-4", "mail", "job done")
        lg.add_request("u-5", "", "B")
        assert lg.human_request_ids() == ("u-3", "u-5")
        lg.settle({"u-3", "n-4", "u-5"})  # 닫아도 "들었다" 는 남는다
        assert lg.open_requests() == () and lg.human_request_ids() == ("u-3", "u-5")
