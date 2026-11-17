"""``agents.json`` 의 모양 — 고정 표본 (v10.145.0, 단일 구조 2단계).

받은편지함·메일함·빚이 우편(`agent_cli.post`)으로 옮겨 가도 저장 파일의 모양은 그대로다(사용자 결정: 2단계는 모양
불변). 표본 `fixtures/agents_state_sample.json` 은 옛 구현(c39747ac)이 같은 상태에서 쓴 파일이다 — 줄 선 요청·
처리 중인 요청·main 앞 미배달 메일(요청 하나·통지 하나)·열린 질문. 시각·무작위 키만 지우고 바이트로 견준다.
"""

from __future__ import annotations

import json
import re
import threading
from pathlib import Path

from tests.test_agents_live import make_registry, make_runner, wait_until

SAMPLE = Path(__file__).parent / "fixtures" / "agents_state_sample.json"
_TIME_KEYS = ("ts", "created_at", "asked_at", "send_ts")


def build_state(tmp_path) -> str:
    """같은 상태를 세우고 저장 파일을 시각·키를 지운 글로 — 표본을 만들 때도 이 함수를 쓴다."""
    gate = threading.Event()
    reg = make_registry(tmp_path, runner=make_runner(block=gate))
    try:
        key, _ = reg.spawn(name="worker")
        asker, _ = reg.spawn(name="asker")
        assert wait_until(lambda: reg.get(key).state == "idle")
        assert wait_until(lambda: reg.get(asker).state == "idle")
        assert reg.request(key, "first job") == ""  # 처리 중(in_flight)
        assert wait_until(lambda: reg.get(key).state == "busy")
        assert reg.request(key, "second job") == ""  # 줄 선 것(inbox)
        reg.message_to_main(asker, "which branch?", expects_reply=True)
        reg.message_to_main(asker, "fyi: started", expects_reply=False)
        _qid, err = reg.register_question(asker, "v1 or v2?")
        assert err == ""
        doc = json.loads((tmp_path / "agents.json").read_text(encoding="utf-8"))
    finally:
        gate.set()
        reg.shutdown_all()
    text = json.dumps(doc, ensure_ascii=False, indent=1, sort_keys=True)
    for k in _TIME_KEYS:
        text = re.sub(rf'"{k}": [0-9.e+-]+', f'"{k}": "<t>"', text)
    for i, k in enumerate((key, asker)):
        text = text.replace(k, f"agt-{'AB'[i]}")
    return text + "\n"


def test_the_saved_file_has_the_old_shape(tmp_path):
    assert build_state(tmp_path) == SAMPLE.read_text(encoding="utf-8")
