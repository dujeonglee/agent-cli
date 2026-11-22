"""작업 구간 도중 흡수한 요청의 원문은 갚기 전 압축에도 남는다 — 실제 조립(오라클 `Session`, web), 모델 없이.

기다리는 상주 aa 가 둘째 요청(TASK-C, 회신 기대)을 같은 작업 구간에 흡수한 뒤 큰 셸 출력으로 압축이 걸린다. 압축은 열린
항목 id(`Ledger.pin_ids`)를 ``request_id`` 로 단 user 레코드를 원문 그대로 남긴다(`ContextManager._pinned_indices`). 작업
구간의 첫 항목(TASK-A)은 그 칸이 있다. 흡수된 요청의 레코드(`AgentRegistry.absorb_pending`, source "agent_request")에도
있어야 한다 — 없으면 원문이 요약으로 바뀌고 꼬리 Open Items 의 앞 120자만 남는다.

순서는 사건(aa 의 기다림 표시·호출 수)으로만 잡는다. 요약 호출에는 고정 글로 답한다. 원문의 120자 뒤에 둔 표지
(SECRET-…)가 모델 입력에 있으면 원문이 남은 것이다.
"""

from __future__ import annotations

import json
import threading

import pytest

from agent_cli.prompts.compaction import BOUNDARY_LINE, SUMMARY_HEADINGS
from agent_cli.providers.base import LLMResponse
from agent_cli.providers.capabilities import ModelCapabilities
from agent_cli.render import get_renderer, set_renderer
from tests.oracle import driver
from tests.oracle.driver import Provider, Session, _agent_key, call, owed_id, until

PAD = "Please be careful and thorough with this; the details below matter. " * 3
TASK_A = f"TASK-A ask bb for the build id. {PAD} SECRET-A-TAIL: mention blue-falcon."
TASK_C = f"TASK-C also check the release notes. {PAD} SECRET-C-TAIL: mention red-otter."
BIG = "python3 -c \"print('lorem ipsum dolor ' * {n})\""
SUMMARY = "\n".join(
    f"{ln.split(' ', 1)[0]} — (fake summary line)"
    for ln in SUMMARY_HEADINGS.splitlines()
)


class _SummarizingProvider(Provider):
    """대본 공급자 + 요약 호출에는 고정 글 — 배우는 처음 본 첫 사용자 메시지로 가르고 그 뒤로는 **스레드**로 기억한다(압축
    뒤에는 첫 메시지가 요약이라 표지로 가를 수 없다). 배우별 모델 입력 전체를 ``full`` 에 남긴다."""

    def __init__(self, scripts):
        super().__init__(scripts)
        self.full: dict[str, list] = {}
        self.summaries = 0
        self._by_thread: dict[str, str] = {}

    def call(self, messages, system, model, capabilities, **kw):
        blob = "".join(str(m.get("content") or "") for m in messages)
        if (
            BOUNDARY_LINE in blob
            or "Harness compaction" in blob
            or "You are compacting" in (system or "")
        ):
            self.summaries += 1
            return LLMResponse(content=SUMMARY)
        name = threading.current_thread().name
        actor = self._by_thread.setdefault(name, self.actor(messages))
        self.full.setdefault(actor, []).append([dict(m) for m in messages])
        with self._lock:
            self.calls.setdefault(actor, []).append({"n_messages": len(messages)})
            script = self.scripts.setdefault(actor, [])
            step = script.pop(0) if script else None
        if step is None:
            step = call("complete", result=f"{actor} done", answers=[])
        if callable(step):
            step = step(messages)
        return LLMResponse(content=step)


@pytest.fixture(autouse=True)
def _keep_renderer():
    before = get_renderer()
    yield
    set_renderer(before)


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("AGENT_CLI_SNAPSHOT", "0")
    # 작은 창 — 큰 셸 출력 몇 번이 압축을 일으킨다
    monkeypatch.setattr(
        driver,
        "CAPS",
        ModelCapabilities(
            context_window=12000, max_output_tokens=1024, supports_thinking=False
        ),
    )
    return tmp_path


def test_a_request_absorbed_mid_run_keeps_its_text_through_compaction(home):
    box: dict = {}
    released = threading.Event()

    def aa_waits():
        s = box["s"]
        return s.main_input.post.is_waiting(f"agent:{_agent_key(s, 'aa')}")

    def main_sends_c(m):
        until(aa_waits, "aa waits on bb")
        return call("message", to="aa", text=TASK_C)

    def bb_holds(m):
        until(released.is_set, "released at the end")
        return call("reply", id=owed_id(m), text="build id 42")

    aa_script = [
        lambda m: call("message", to="bb", text="TASK-B what is the build id?"),
        call("wait"),
    ]
    # 같은 호출 반복 감지를 피하려고 조금씩 다르게. 큰 출력은 상한 안내(발췌)로 바뀌어 한 번에 ~1.6K 토큰씩 쌓인다 —
    # 열 번이면 창 12K 를 넉넉히 넘는다(여섯 번은 경계라 경로 길이 따위로 갈렸다)
    aa_script += [call("shell", command=BIG.format(n=600 + k)) for k in range(10)]
    aa_script += [lambda m: (released.set(), call("wait"))[1]]
    aa_script += [lambda m: call("complete", result="done", answers=[])] * 6
    p = _SummarizingProvider(
        {
            "MAIN": [
                call("agent", mode="spawn", name="aa"),
                call("agent", mode="spawn", name="bb"),
                lambda m: call("message", to="aa", text=TASK_A),
                main_sends_c,
                call("wait"),
            ]
            + [
                lambda m: (
                    call("wait")
                    if box["s"].reg.awaited_replies("main")
                    else call("complete", result="ok", answers=[])
                )
            ]
            * 12,
            "TASK-A": aa_script,
            "TASK-B": [bb_holds]
            + [lambda m: call("complete", result="bb", answers=[])] * 4,
        }
    )
    s = box["s"] = Session(home, p, path="web")
    s.say("MAIN go", nickname="dj")
    pump = s.pump_in_thread()
    try:

        def after_compaction() -> list:
            return [
                msgs
                for msgs in list(p.full.get("TASK-A", []))
                if any("(fake summary line)" in str(m.get("content")) for m in msgs)
            ]

        # 압축 뒤 aa 의 첫 모델 호출 — 그 입력이 모델이 읽는 것이다
        until(lambda: bool(after_compaction()), "aa called after a compaction")
        aa = _agent_key(s, "aa")
        after = after_compaction()
        body = "\n".join(str(m.get("content") or "") for m in after[0])
        assert "SECRET-A-TAIL" in body  # 첫 항목 — 핀으로 남는다
        assert "SECRET-C-TAIL" in body  # 흡수된 요청 — 갚기 전이라 같은 대접
        path = next(f for f in (s.sdir).rglob("history.jsonl") if aa in str(f))
        rec = next(
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if "TASK-C also check" in line
        )
        assert rec.get("source") == "agent_request"
        # 그 레코드의 id 는 그 요청의 빚 id(r-…) — 압축이 읽는 핀 목록(`Ledger.pin_ids`)에 든다
        assert rec.get("request_id") in {d.id for d in s.reg.reply_debts(f"agent:{aa}")}
    finally:
        s.close()
        pump.join(20)
