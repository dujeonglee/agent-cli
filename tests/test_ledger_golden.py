"""열린 항목 장부의 회귀 가드 — 모델이 보는 글이 바이트 단위로 같다.

P1(v10.54.0, docs/ledger/DESIGN.md)은 흩어져 있던 회계(`run_requests`·되묻기·
독촉·빚 카운터)를 장부 하나로 모으되 **모델이 보는 글은 바꾸지 않는다**. 그래서
리팩터 **전** 코드로 시나리오마다 꼬리·되묻기·독촉·거부·최종 통지 문구를 뽑아
얼려 두었다(`tests/fixtures/ledger_golden.json`). 이 파일의 `SCENARIOS` 가 그
생성기이기도 하다 — ``python -m tests.test_ledger_golden <out.json>``.

- P1(v10.54.0): 리팩터 **전** 코드로 만들었다.
- P2(v10.55.0): 메일이 장부 항목이 되며 "user request" 가 "request" 로 바뀌었다
  (꼬리 머리말·되묻기·독촉). 다시 만들 때 달라진 문자열 4개가 정확히 그 치환으로
  설명됨을 확인했다(나머지 바이트 동일).
- P3(v10.56.0): 꼬리(`## Open Items`)·정책(계속형 붙잡기 + 예산 8)·id(`u-`·`r-`)가
  설계대로 바뀌어 기계적 대조가 불가능하다 — 시나리오마다 새 출력을 사람이 읽고
  다시 얼렸다(PR 에 대조표). 이후 변경은 다시 바이트 가드를 받는다.
- v10.77.0: 표기 규칙(인자 백틱·값 따옴표) — 6개 시나리오가 꼬리 Task Guidelines 의
  "`background: true`" → "`background` true" 하나로만 달라졌다(나머지 바이트 동일).

빈 `answers: []` 를 되돌리는 정책은 `test_request_accounting` 이 따로 고정한다.
"""

from __future__ import annotations

import json
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from agent_cli.loop import AgentLoop
from agent_cli.providers.capabilities import ModelCapabilities
from tests.loop_ports import make_ports

_FIXTURE = Path(__file__).parent / "fixtures" / "ledger_golden.json"


class _Port:
    """질문 포트 흉내 — 빚 목록만. ``nonblocking`` 이 상주/main 을 가른다."""

    def __init__(self, debts, *, resident):
        self._debts = list(debts)
        self.nonblocking = resident

    def debts(self):
        return list(self._debts)


_REPLY = {"kind": "reply", "to": "agent:agt-a", "id": "r-1", "text": "리뷰 결과 보내줘"}
_QUESTION = {"kind": "answer", "to": "agt-b", "id": "q-7a3f", "text": "할인 상한은?"}


def _loop(items=(), *, debts=(), resident=False, user_run=None):
    q = [dict(it) for it in items]
    loop = AgentLoop(
        query="Q",
        provider=MagicMock(),
        capabilities=ModelCapabilities(
            context_window=32768, max_output_tokens=4096, supports_thinking=False
        ),
        model="m",
        ports=make_ports(
            dequeue_user_message=lambda: q.pop(0) if q else None,
            questions=_Port(debts, resident=resident) if debts else None,
        ),
    )
    loop._inject_queued_messages()
    if user_run is not None:
        loop._state.user_run = user_run
    return loop


def _complete(loop, **ai) -> list[str]:
    """`complete` 하나를 dispatch 에 넣고 — 그 결과로 모델 쪽에 생긴 글을 돌려준다."""
    before = len(loop.messages)
    turn = types.SimpleNamespace(
        thought="", record={"role": "assistant", "content": "raw"}
    )
    op = types.SimpleNamespace(action="complete", action_input=dict(ai))
    res = loop._dispatch._op_complete("raw", turn, op, {})
    out = [
        json.dumps(m, ensure_ascii=False, sort_keys=True)
        for m in loop.messages[before:]
    ]
    if hasattr(res, "output"):
        out.append("RESULT:" + str(res.output))
    else:
        out.append("CONTINUE")
    return out


def _tail(loop) -> str:
    return loop._llm._build_session_state(10_000)


def _req(i, text, nick="dj"):
    return {"id": f"u-{i}", "nickname": nick, "text": text}


def s_single_answered():
    lp = _loop([_req(1, "README 정리해 줘")])
    return [_tail(lp), *_complete(lp, result="정리했다", answers=["u-1"])]


def s_single_omitted_twice():
    lp = _loop([_req(1, "README 정리해 줘")])
    a = _complete(lp, result="정리했다")
    b = _complete(lp, result="정리했다")
    return [_tail(lp), *a, *b]


def s_two_partial_then_rest():
    lp = _loop([_req(1, "A 해줘"), _req(2, "B 해줘", nick="ann")])
    a = _complete(lp, result="A 했다", answers=["u-1"])
    t = _tail(lp)
    b = _complete(lp, result="B 했다", answers=["u-2"])
    return [*a, t, *b]


def s_two_partial_twice():
    lp = _loop([_req(1, "A 해줘"), _req(2, "B 해줘", nick="ann")])
    a = _complete(lp, result="A 했다", answers=["u-1"])
    b = _complete(lp, result="A 했다", answers=["u-1"])
    return [*a, *b]


def s_debts_not_user_run():
    lp = _loop(debts=[_REPLY, _QUESTION], resident=True, user_run=False)
    outs = [_tail(lp)]
    for _ in range(4):
        outs += _complete(lp, result="끝")
    return outs


def s_debts_user_run_main():
    lp = _loop([_req(1, "A 해줘")], debts=[_REPLY], resident=False)
    outs = [_tail(lp)]
    for _ in range(4):
        outs += _complete(lp, result="끝", answers=["u-1"])
    return outs


def s_request_and_debts_mixed():
    lp = _loop([_req(1, "A 해줘"), _req(2, "B 해줘")], debts=[_QUESTION], resident=True)
    a = _complete(lp, result="A", answers=["u-1"])
    b = _complete(lp, result="B", answers=["u-2"])
    return [_tail(lp), *a, *b]


def s_answer_tool_on_request_id():
    lp = _loop([_req(1, "A 해줘")])
    return [
        str(lp._state.ledger.has_request("u-1")),
        str(lp._state.ledger.has_request("u-9")),
    ]


SCENARIOS = {
    f.__name__[2:]: f
    for f in (
        s_single_answered,
        s_single_omitted_twice,
        s_two_partial_then_rest,
        s_two_partial_twice,
        s_debts_not_user_run,
        s_debts_user_run_main,
        s_request_and_debts_mixed,
        s_answer_tool_on_request_id,
    )
}


@pytest.fixture(autouse=True)
def _no_render(monkeypatch):
    # 렌더러 부작용(카드)은 이 검사의 대상이 아니다 — 모델 쪽 글만.
    import agent_cli.loop.dispatch as d

    monkeypatch.setattr(d, "render_step", lambda *a, **k: None)
    # 정보원 목록(v10.57.0)은 장부와 무관한 꼬리 블록 — 얼린 글은 장부만 본다.
    monkeypatch.setattr("agent_cli.web_sources.render", lambda *a, **k: "")


@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_matches_the_frozen_accounting(name):
    want = json.loads(_FIXTURE.read_text(encoding="utf-8"))[name]
    assert SCENARIOS[name]() == want


if __name__ == "__main__":
    import agent_cli.loop.dispatch as _d

    _d.render_step = lambda *a, **k: None
    import agent_cli.web_sources as _w

    _w.render = lambda *a, **k: ""
    out = {n: f() for n, f in SCENARIOS.items()}
    Path(sys.argv[1]).write_text(
        json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    print("scenarios", len(out))
