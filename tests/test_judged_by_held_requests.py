"""감사와 `run` 의 결과 파일은 그 작업 구간이 든 사람의 요청 번호(`u-`)로 판정한다 (5c-2 A1).

"무엇이 작업 구간을 열었나" 가 아니다. 감사: 성공했고 `u-` 를 하나라도 든 작업 구간. 결과 파일·종료 코드:
argv 질의의 번호를 든 작업 구간의 답. "닫은 번호(`answers`)" 도 아니다 — 모델이 `answers` 를 빠뜨린 성공이 실패로
찍히지 않는다.

장면은 엔진을 대본 공급자로 실제로 돌려 만든다 — 그 결과를 감사자와 콘솔 표면(`run` 의 결과 파일)에 넘긴다.
"""

from __future__ import annotations

import json

from agent_cli.context.manager import ContextManager
from agent_cli.harness.auditor import SessionAuditor
from agent_cli.loop.kind import Start
from agent_cli.loop.ledger import NAG_BUDGET
from agent_cli.main import _ConsoleSurface
from agent_cli.providers.base import LLMResponse
from agent_cli.providers.capabilities import ModelCapabilities
from tests.loop_ports import make_ports, queue_inbox
from tests.loopkit import run_loop

CAPS = ModelCapabilities(
    context_window=32768, max_output_tokens=4096, supports_thinking=False
)
#: argv 질의의 번호
QUERY = "u-1"


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

    def segment(self, script: Script, *, opened_by_query=True, words=(), **attrs):
        """작업 구간 하나 — ``opened_by_query`` 면 argv 질의가 열고, 아니면 메일이 연다(시작 메시지 없음).
        ``words`` 는 첫 턴 경계에 서 있는 사람의 말(번호, 글)."""
        queue = [{"id": rid, "text": text} for rid, text in words]
        start = {
            "query": "what is 2+2" if opened_by_query else "",
            "query_author_is_user": opened_by_query,
            "query_request_id": QUERY if opened_by_query else "",
        }
        result = run_loop(
            provider=script,
            capabilities=CAPS,
            model="m",
            ctx=self.ctx,
            ports=make_ports(
                inbox=queue_inbox(lambda: queue.pop(0) if queue else None)
            ),
            **start,
            **attrs,
        )
        self.ended(
            result,
            Start(
                query=start["query"],
                author=None,
                author_is_user=opened_by_query,
                request_id=start["query_request_id"],
            ),
        )
        return result

    def ended(self, result, start) -> None:
        self.auditor.run_ended(start, ok=result.success)
        self.surface.run_ended(result, start)

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
        assert not result.success
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
        """메일이 연 작업 구간이 도중에 사람의 말을 받아 답했다 — 지금은 감사하지 않는다."""
        s = Session(tmp_path)
        script = Script(call("complete", result="six", answers=["u-2"]))
        result = s.segment(script, opened_by_query=False, words=[("u-2", "3+3?")])
        assert result.success and result.output == "six"
        assert s.audited == 0
        assert s.file is None and s.surface.exit_code == 1  # argv 질의의 답이 아니다
