"""사람이 에이전트 채널에 붙인 이미지 — 실제 레지스트리·실제 엔진(가짜 provider).

글 없이 이미지만 보낸 말도 일감이다(main 과 같다). 그 말이 에이전트의 런을 열든, 도는 런의 턴
경계에 흡수되든 모델이 받는 user 레코드는 main 의 것과 같은 모양이다 — 글 끝의 자리표시 줄과
``images`` 참조.
"""

from __future__ import annotations

import threading

import pytest

from agent_cli.session_slots import set_agent_registry
from agent_cli.subagent.agents_live import AgentRegistry
from tests.binding_support import make_binding
from tests.test_engine_p2 import CAPS, Provider, call, wait_until

REF = {
    "path": "a.png",
    "mime": "image/png",
    "width": 8,
    "height": 4,
    "bytes": 1,
    "sha256": "",
}


@pytest.fixture
def registry(tmp_path):
    made: list = []

    def make(provider):
        binding = make_binding(provider=provider, capabilities=CAPS, model="m")
        reg = AgentRegistry(
            tmp_path / "s",
            runtime={"binding": binding, "max_turns": 6, "depth": 0, "max_depth": 2},
        )
        set_agent_registry(reg)
        made.append(reg)
        key, err = reg.spawn(name="alpha")
        assert err is None or err == "", err
        assert wait_until(lambda: reg.get(key).state == "idle")
        return reg, key

    yield make
    for reg in made:
        reg.shutdown_all()
    set_agent_registry(None)


def _image_records(messages) -> list[dict]:
    """모델에게 간 user 레코드 중 이미지가 딸린 것 — 본문만(하니스가 마지막 레코드에 붙이는 꼬리는 뗀다)."""
    return [
        {**m, "content": str(m["content"]).split("\n\n── end of message", 1)[0]}
        for m in messages
        if m.get("role") == "user" and m.get("images")
    ]


def test_image_only_request_opens_a_run_with_the_image(registry):
    p = Provider({"MAIN": []})
    reg, key = registry(p)
    assert reg.request(key, "", author="user:dj", images=[REF]) == ""
    assert wait_until(lambda: p.calls["MAIN"])
    recs = _image_records(p.calls["MAIN"][0])
    assert [r["content"] for r in recs] == ["[user:dj]: \n[image: a.png (8×4)]"]
    assert recs[0]["images"] == [REF]


def test_neither_text_nor_image_is_refused(registry):
    reg, key = registry(Provider({"MAIN": []}))
    assert reg.request(key, "", author="user:dj") == "empty message"
    assert reg.request(key, "  ", author="user:dj", images=[]) == "empty message"


@pytest.mark.parametrize("text", ["", "and this"])
def test_image_absorbed_mid_run_reaches_the_injected_record(registry, text):
    """도는 런에 들어온 사람 말 — 다음 턴 경계의 흡수가 이미지를 싣고 주입한다."""
    gate, inside = threading.Event(), threading.Event()

    def held(_messages):
        inside.set()
        assert gate.wait(10)
        return call("shell", command="true")

    p = Provider({"MAIN": [held]})
    reg, key = registry(p)
    assert reg.request(key, "start", author="user:dj") == ""
    assert inside.wait(10)
    assert reg.request(key, text, author="user:dj", images=[REF]) == ""
    gate.set()
    assert wait_until(lambda: len(p.calls["MAIN"]) >= 2)
    recs = _image_records(p.calls["MAIN"][1])
    assert [r["content"] for r in recs] == [f"[user:dj]: {text}\n[image: a.png (8×4)]"]
    assert recs[0]["images"] == [REF]
