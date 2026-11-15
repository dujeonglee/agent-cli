"""run 결과 바이트 등가 — 엔진 통합 P1 의 합격선 (docs/agent-engine/DESIGN.md §P1).

코퍼스(`tests/equivalence/run_corpus.py` 설명)를 사례마다 다시 만들어 고정본
``expected/run_results.json`` 과 비교한다: run 의 `ToolResult`, 부모·자식 루프가 모델에게
보낸 요청 전부(system·messages), ``result.md``. 고정본은 옛 엔진(`OneShotLoop`)에서 만들었다.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import os
import threading

import pytest

from agent_cli.context.manager import ContextManager
from agent_cli.loop.tool_bridge import ToolBridge
from agent_cli.session_slots import get_agent_registry, set_agent_registry
from agent_cli.subagent.agents_live import AgentRegistry
from tests.binding_support import make_binding
from tests.equivalence.run_corpus import (
    BIG_CAPS,
    CAPS,
    EXPECTED,
    ScriptedProvider,
    call,
    complete,
    normalize_tree,
    ops,
    result_files,
    tool_result_view,
)
from tests.loop_ports import make_ports
from tests.loopkit import make_config, run_loop

WRITE = os.environ.get("AGENT_CLI_WRITE_RUN_CORPUS") == "1"


@pytest.fixture
def world(tmp_path, monkeypatch):
    """임시 HOME·cwd·세션 디렉터리 + 세션 슬롯의 에이전트 레지스트리(실제 세션처럼)."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.chdir(work)
    # 도구 목록이 기계에 따라 달라지지 않게 — `browser` 는 playwright 가 깔린 곳에서만 광고된다
    # (CI 의 min-deps 에는 없다). 코퍼스를 만든 실제 세션(playwright 있음)으로 고정한다.
    monkeypatch.setattr("agent_cli.browser.browser_available", lambda: True)
    sdir = tmp_path / "session"
    sdir.mkdir()
    prev = get_agent_registry()
    registry = AgentRegistry(sdir)
    set_agent_registry(registry)
    recorded: list = []
    real = ToolBridge._invoke_agent

    def recording(self, tool_input, input_dict):
        result = real(self, tool_input, input_dict)
        recorded.append(tool_result_view(result))
        return result

    monkeypatch.setattr(ToolBridge, "_invoke_agent", recording)
    yield {"root": str(tmp_path), "session": sdir, "work": work, "results": recorded}
    set_agent_registry(prev)


def _ctx(world, caps=BIG_CAPS, provider=None):
    binding = make_binding(provider=provider, capabilities=caps)
    return ContextManager(session_dir=world["session"], binding=binding), binding


def _parent(world, provider, *, caps=BIG_CAPS, stop_event=None, **attrs):
    ctx, binding = _ctx(world, caps, provider)
    out = run_loop(
        query="PARENT-main do the thing",
        binding=binding,
        ports=make_ports(),
        ctx=ctx,
        stop_event=stop_event,
        **attrs,
    )
    return tool_result_view(out)


def _record(world, name: str, provider=None, **extra) -> dict:
    case = {
        "results": world["results"],
        "calls": provider.calls if provider is not None else {},
        "files": result_files(world["session"]),
        **extra,
    }
    return json.loads(json.dumps(normalize_tree(case, world["root"])))


def _split_systems(case: dict) -> tuple[dict, dict[str, str]]:
    """시스템 프롬프트는 사례들 사이에 몇 벌뿐이다 — 고정본엔 한 번씩만(sha 로 참조)."""
    systems: dict[str, str] = {}
    for calls in case["calls"].values():
        for c in calls:
            sha = hashlib.sha256(c["system"].encode("utf-8")).hexdigest()[:16]
            systems[sha] = c["system"]
            c["system"] = sha
    return case, systems


def _check(name: str, case: dict) -> None:
    case, systems = _split_systems(case)
    if WRITE:
        data = json.loads(EXPECTED.read_text()) if EXPECTED.exists() else {}
        data.setdefault("_systems", {}).update(systems)
        data[name] = case
        EXPECTED.parent.mkdir(parents=True, exist_ok=True)
        EXPECTED.write_text(
            json.dumps(data, ensure_ascii=False, indent=1, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        return
    expected = json.loads(EXPECTED.read_text(encoding="utf-8"))
    assert name in expected, f"no frozen corpus entry for {name!r}"
    # 어느 줄이 달라졌는지 보이게 — **같은 배우·같은 호출 순번**의 고정본과 비교한다(사례의
    # 첫 시스템 프롬프트와 비교하면 부모·자식이 섞여 엉뚱한 diff 가 나온다).
    for actor, calls in case["calls"].items():
        old_calls = expected[name]["calls"].get(actor, [])
        for i, c in enumerate(calls):
            if c["system"] in expected["_systems"]:
                continue
            old = (
                expected["_systems"].get(old_calls[i]["system"], "")
                if i < len(old_calls)
                else ""
            )
            diff = "\n".join(
                difflib.unified_diff(
                    old.splitlines(), systems[c["system"]].splitlines(), lineterm=""
                )
            )
            raise AssertionError(
                f"system prompt changed in {name} ({actor} call {i + 1}):\n{diff}"
            )
    assert case == expected[name]


def _bridge(world, *, binding=None, **cfg_attrs):
    """루프 없이 run 을 디스패치하는 부모 자리 — 거부 판정은 엔진에 닿기 전이다."""
    from agent_cli.loop.state import LoopState

    ctx, b = _ctx(world)
    cfg = make_config(binding=binding if binding is not None else b, **cfg_attrs)
    return ToolBridge(cfg, LoopState(), ctx, None)


# ── 단건 ──────────────────────────────────────────────────────


def test_single_success(world):
    p = ScriptedProvider(
        {
            "PARENT-main": [
                call("agent", mode="run", task="CHILD-ok find the answer"),
                complete("parent done"),
            ],
            "CHILD-ok": [complete("the child answer")],
        }
    )
    final = _parent(world, p)
    _check("single_success", _record(world, "single_success", p, final=final))


def test_single_profile_success(world):
    p = ScriptedProvider(
        {
            "PARENT-main": [
                call(
                    "agent",
                    mode="run",
                    task="CHILD-prof review this",
                    profile="code-reviewer",
                    instructions="Be brief.",
                ),
                complete("parent done"),
            ],
            "CHILD-prof": [
                call("read_file", path="nope.txt"),
                complete("reviewed"),
            ],
        }
    )
    final = _parent(world, p)
    _check("single_profile", _record(world, "single_profile", p, final=final))


def test_single_max_turns(world):
    p = ScriptedProvider(
        {
            "PARENT-main": [
                call("agent", mode="run", task="CHILD-loop keep reading"),
                complete("parent done"),
            ],
            "CHILD-loop": [call("read_file", path="missing.txt")],
        }
    )
    final = _parent(world, p, max_turns=2)
    _check("single_max_turns", _record(world, "single_max_turns", p, final=final))


def test_single_stopped(world):
    stop = threading.Event()

    def child_turn():
        stop.set()  # 방 ■ — 호출자 런의 정지가 자식 런을 멈춘다
        return call("read_file", path="a.txt")

    p = ScriptedProvider(
        {
            "PARENT-main": [
                call("agent", mode="run", task="CHILD-stop work slowly"),
                complete("parent done"),
            ],
            "CHILD-stop": [child_turn, complete("never")],
        }
    )
    final = _parent(world, p, stop_event=stop)
    _check("single_stopped", _record(world, "single_stopped", p, final=final))


def test_context_fork(world):
    p = ScriptedProvider(
        {
            "PARENT-main": [
                call("read_file", path="nothing.txt"),
                call(
                    "agent", mode="run", task="CHILD-fork continue it", context="fork"
                ),
                complete("parent done"),
            ],
            "CHILD-fork": [complete("forked answer")],
        }
    )
    final = _parent(world, p)
    _check("context_fork", _record(world, "context_fork", p, final=final))


def test_big_result_pointer(world):
    big = "\n".join(f"line {i} " + "x" * 60 for i in range(900))
    p = ScriptedProvider(
        {
            "PARENT-main": [
                call("agent", mode="run", task="CHILD-big dump everything"),
                complete("parent done"),
            ],
            "CHILD-big": [complete(big)],
        }
    )
    final = _parent(world, p, caps=CAPS)
    _check("big_result", _record(world, "big_result", p, final=final))


def test_main_tail_with_registry(world):
    """부모가 main 처럼 레지스트리 포트를 가진다 — 전 스키마와 꼬리의 `## Live Agents`
    (0/N) 가 run 앞뒤로 같아야 한다(끝난 run 이 명단에 끼면 바이트가 바뀐다)."""
    p = ScriptedProvider(
        {
            "PARENT-main": [
                call(
                    "agent", mode="run", task="CHILD-ok find it", profile="code-analyst"
                ),
                ops(
                    {"action": "agent", "mode": "run", "task": "CHILD-a one"},
                    {"action": "agent", "mode": "run", "task": "CHILD-b two"},
                ),
                call("agent", mode="status"),
                complete("parent done"),
            ],
            "CHILD-ok": [complete("found")],
            "CHILD-a": [complete("a")],
            "CHILD-b": [complete("b")],
        }
    )
    ctx, binding = _ctx(world, BIG_CAPS, p)
    final = run_loop(
        query="PARENT-main do the thing",
        binding=binding,
        ports=make_ports(agent_registry=get_agent_registry()),
        ctx=ctx,
    )
    _check(
        "main_tail_registry",
        _record(world, "main_tail_registry", p, final=tool_result_view(final)),
    )


# ── 거부 ──────────────────────────────────────────────────────


def test_reject_empty_task(world):
    bridge = _bridge(world)
    bridge._invoke_agent({"mode": "run", "task": "   "}, {"mode": "run"})
    _check("reject_empty_task", _record(world, "reject_empty_task"))


def test_reject_no_provider(world):
    bridge = _bridge(world, binding=make_binding(provider=None, capabilities=None))
    bridge._invoke_agent({"mode": "run", "task": "CHILD-x go"}, {"mode": "run"})
    _check("reject_no_provider", _record(world, "reject_no_provider"))


def test_reject_recursion(world):
    p = ScriptedProvider({})
    bridge = _bridge(
        world,
        binding=make_binding(provider=p, capabilities=BIG_CAPS),
        agent_stack=["general"],
    )
    bridge._invoke_agent(
        {"mode": "run", "task": "CHILD-x go", "profile": "general"}, {"mode": "run"}
    )
    _check("reject_recursion", _record(world, "reject_recursion"))


def test_reject_depth(world):
    p = ScriptedProvider({})
    bridge = _bridge(
        world, binding=make_binding(provider=p, capabilities=BIG_CAPS), depth=2
    )
    bridge._invoke_agent({"mode": "run", "task": "CHILD-x go"}, {"mode": "run"})
    _check("reject_depth", _record(world, "reject_depth"))


def test_reject_unknown_profile(world):
    p = ScriptedProvider(
        {
            "PARENT-main": [
                call("agent", mode="run", task="CHILD-x go", profile="no-such-profile"),
                complete("parent done"),
            ],
        }
    )
    final = _parent(world, p)
    _check("reject_profile", _record(world, "reject_profile", p, final=final))


def test_reject_tools(world):
    p = ScriptedProvider(
        {
            "PARENT-main": [
                call(
                    "agent",
                    mode="run",
                    task="CHILD-x go",
                    profile="code-reviewer",
                    tools=["write_file"],
                ),
                complete("parent done"),
            ],
        }
    )
    final = _parent(world, p)
    _check("reject_tools", _record(world, "reject_tools", p, final=final))


# ── 병렬 ──────────────────────────────────────────────────────


def test_parallel_all_success(world):
    p = ScriptedProvider(
        {
            "PARENT-main": [
                ops(
                    {"action": "agent", "mode": "run", "task": "CHILD-a part one"},
                    {"action": "agent", "mode": "run", "task": "CHILD-b part two"},
                ),
                complete("parent done"),
            ],
            "CHILD-a": [complete("answer a")],
            "CHILD-b": [call("read_file", path="b.txt"), complete("answer b")],
        }
    )
    final = _parent(world, p)
    _check("parallel_all", _record(world, "parallel_all", p, final=final))


def test_parallel_partial_failure(world):
    p = ScriptedProvider(
        {
            "PARENT-main": [
                ops(
                    {"action": "agent", "mode": "run", "task": "CHILD-ok part one"},
                    {
                        "action": "agent",
                        "mode": "run",
                        "task": "CHILD-bad part two",
                        "profile": "no-such-profile",
                    },
                    {"action": "agent", "mode": "run", "task": "CHILD-loop part three"},
                ),
                complete("parent done"),
            ],
            "CHILD-ok": [complete("answer ok")],
            "CHILD-loop": [call("read_file", path="missing.txt")],
        }
    )
    final = _parent(world, p, max_turns=3)
    _check("parallel_partial", _record(world, "parallel_partial", p, final=final))


# ── @agent 명령 ───────────────────────────────────────────────


def test_at_agent_command(world):
    from agent_cli.main import _dispatch_agent

    p = ScriptedProvider({"CHILD-cmd": [complete("command answer")]})
    ctx, binding = _ctx(world, BIG_CAPS, p)
    ctx.add({"role": "user", "content": "earlier talk"})
    answer, ok = _dispatch_agent("@general CHILD-cmd look around", binding, ctx=ctx)
    records = [m for m in ctx.get_raw_messages() if m.get("role") == "user"]
    for m in records:
        m.pop("ts", None)
    _check(
        "at_agent",
        _record(world, "at_agent", p, answer=answer, ok=ok, records=records),
    )
