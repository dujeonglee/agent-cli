"""메일함·요청 장부 오라클 드라이버 (docs: 메일함 시안 v4.1 §8 1c).

f44cf539(v10.143.0) 에서 만든 기대값이 그 뒤의 구조 교체(우편함·장부)를 재는 자다. 그래서 이 드라이버는
**공개 표면만** 쓴다 — 실제 세션 조립(`runtime.build_session_stores`·`build_session_owners`·`wire_session`),
펌프, 대본 응답의 가짜 provider, 레지스트리의 공개 메서드(`request`·`deliver`·`stop_run`·`kill`·
`stop_all_runs`·`answer_question`·`restore`·`shutdown_all`), 입력 큐. 레지스트리의 저장소를 읽지 않고,
조회도 쓰지 않는다(f44cf539 에 없는 표면). 가드 테스트(`test_oracle.py`)가 import 목록을 고정한다.

담는 것: 배우(main·에이전트)마다 provider 호출의 마지막 메시지(꼬리의 `## Open Items` 가 여기 있다),
세션 폴더의 `history.jsonl`(모델 레코드 — content·source·tool·request_id·answers)과 `conversation.jsonl`,
렌더러 이벤트(스레드별 열), 시나리오가 직접 본 결과(도구 결과·재시작 뒤 사실). `agents.json` 바이트는
담지 않는다.

결정성: 시나리오마다 한 번에 한 배우만 움직이도록 대본이 공개 상태(렌더러 이벤트·에이전트 상태)를 보고
기다린다. 이벤트는 스레드별 열로 담는다 — 스레드 사이의 끼어듦은 담지 않는다. 비결정 값은 `normalize`
가 지운다.

재생성: ``python3 -m tests.oracle.driver --out tests/oracle/expected``
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import tempfile
import threading
import time
from pathlib import Path
from types import SimpleNamespace

from rich.console import Console

from agent_cli.context.manager import ContextManager
from agent_cli.input_queue import InputQueue
from agent_cli.main import web_instance_is_active
from agent_cli.providers.base import LLMResponse
from agent_cli.providers.capabilities import ModelCapabilities
from agent_cli.pump import ForeverPolicy, QuietPolicy
from agent_cli.render import get_renderer, set_renderer
from agent_cli.render.minimal import MinimalRenderer
from agent_cli.runtime import (
    SessionSpec,
    build_session_owners,
    build_session_stores,
    teardown_session,
    wire_session,
)
from tests.binding_support import make_binding

CAPS = ModelCapabilities(
    context_window=65536, max_output_tokens=4096, supports_thinking=False
)

#: 렌더러 이벤트 중 메일·요청 장부에 닿는 것만 — 나머지(스트림·스피너·로스터 토큰 수)는 잡음이다
EVENTS = frozenset(
    {
        "agent_message",
        "agent_stopped",
        "agent_mail_hint",
        "begin_agent_work",
        "end_agent_work",
        "open_items",
        "wait_in_place",
        "note_request",
        "note_items_closed_after",
        "announce_ask",
        "push_user_message",
        "set_run_authors",
    }
)

#: 한 번 기다리는 상한 — 넘으면 시나리오가 실패한다(결과를 바꾸는 값이 아니다)
_PATIENCE_S = 20.0


def call(action: str, **kw) -> str:
    return json.dumps({"action": action, **kw})


def until(pred, what: str = "") -> None:
    """``pred`` 가 참이 될 때까지 기다린다 — 넘으면 시나리오 실패."""
    end = time.monotonic() + _PATIENCE_S
    while time.monotonic() < end:
        if pred():
            return
        time.sleep(0.01)
    raise TimeoutError(f"oracle: timed out waiting for {what or pred}")


# ── 렌더러 기록 ──────────────────────────────────────────────


def _plain(v):
    """JSON 으로 옮길 수 있는 값으로."""
    if isinstance(v, dict):
        return {str(k): _plain(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_plain(x) for x in v]
    if isinstance(v, (str, int, float, bool)) or v is None:
        return v
    return f"<{type(v).__name__}>"


class Recorder:
    """렌더러 대리 — 진짜 `MinimalRenderer`(출력은 버림)에 넘기면서 :data:`EVENTS` 를 스레드별로 적는다.
    ``wait_in_place(True, …)`` 가 오면 ``main_waiting`` 을 세운다(대본이 그 뒤에 회신한다)."""

    def __init__(self, main_thread: threading.Thread):
        self._inner = MinimalRenderer(Console(file=io.StringIO(), width=200))
        self._main = main_thread
        self._lock = threading.Lock()
        self.lanes: dict[str, list] = {}
        self.main_waiting = threading.Event()
        self.counts: dict[str, int] = {}

    def lane(self) -> str:
        t = threading.current_thread()
        if t is self._main or t.name == "oracle-pump":
            return "main"
        if t.name.startswith("agent-"):
            return t.name
        return "other"

    def __getattr__(self, name):
        target = getattr(self._inner, name)
        if name not in EVENTS or not callable(target):
            return target

        def recorded(*args, **kwargs):
            with self._lock:
                self.lanes.setdefault(self.lane(), []).append(
                    [name, _plain(list(args)), _plain(kwargs)]
                )
                self.counts[name] = self.counts.get(name, 0) + 1
            if name == "wait_in_place" and args and args[0]:
                self.main_waiting.set()
            if name == "wait_in_place" and args and not args[0]:
                self.main_waiting.clear()
            return target(*args, **kwargs)

        return recorded


# ── 가짜 provider ────────────────────────────────────────────


class Provider:
    """배우별 대본 — 배우는 런의 첫 사용자 메시지에 든 표지(``TASK-…``)로 가른다, 없으면 ``MAIN``.
    대본의 한 줄은 글(그대로 응답)이거나 함수(그 호출의 메시지를 보고 응답을 만든다). 다 떨어지면 `complete`."""

    def __init__(self, scripts: dict[str, list]):
        self.scripts = {k: list(v) for k, v in scripts.items()}
        self.calls: dict[str, list] = {k: [] for k in scripts}
        self._lock = threading.Lock()

    def actor(self, messages) -> str:
        users = [m for m in messages if m.get("role") == "user"]
        first = str(users[0].get("content") or "") if users else ""
        first = first.split("── end of message", 1)[0]
        for tag in self.scripts:
            if tag != "MAIN" and tag in first:
                return tag
        return "MAIN"

    def call(self, messages, system, model, capabilities, **kw):
        a = self.actor(messages)
        with self._lock:
            self.calls.setdefault(a, []).append(
                {
                    "n_messages": len(messages),
                    "last": str(messages[-1].get("content") or "") if messages else "",
                }
            )
            script = self.scripts.setdefault(a, [])
            step = script.pop(0) if script else None
        if step is None:
            step = call("complete", result=f"{a} done", answers=[])
        if callable(step):
            step = step(messages)
        return LLMResponse(content=step)


def tail_of(messages) -> str:
    return "\n".join(str(m.get("content") or "") for m in messages[-2:])


def owed_id(messages) -> str:
    """꼬리의 `## Open Items` 에서 갚을 요청 id."""
    m = re.search(r'`reply` with `id` "(r-\d+)"', tail_of(messages))
    if not m:
        raise AssertionError("oracle: no owed id in " + tail_of(messages)[-600:])
    return m.group(1)


def closing_ids(messages) -> list[str]:
    m = re.search(
        r"listing in `answers` the ids it covers \(([^)]*)\)",
        str(messages[-1].get("content") or ""),
    )
    return re.findall(r'"([a-z]-\d+)"', m.group(1)) if m else []


def complete_all(result: str):
    """꼬리가 말하는 닫을 id 전부로 `complete`."""
    return lambda msgs: call("complete", result=result, answers=closing_ids(msgs))


# ── 세션 ─────────────────────────────────────────────────────


class Surface:
    """펌프 표면 — 부른 순서를 main 열에 적는다(런 시작·끝·말풍선)."""

    graceful_interrupt = False

    def __init__(self, recorder: Recorder):
        self.rec = recorder
        self.results: list = []
        #: 지금 런의 중단 핸들 — "Stop everything"·■ 을 흉내 내는 대본이 세운다(서버·Ctrl+C 와 같은 자리)
        self.stop: threading.Event | None = None
        #: 명령으로 받을 글 → 그때 할 일 — 런을 열지 않는 차례(`/`·`@` 명령)를 흉내 낸다
        self.commands: dict = {}

    def _note(self, *row):
        with self.rec._lock:
            self.rec.lanes.setdefault("pump", []).append(list(row))

    def idle(self):
        pass

    def busy(self):
        pass

    def echo(self, item, *, wake):
        self._note("echo", str(item.get("text") or ""), bool(wake))

    def bind_stop(self, event):
        self.stop = event

    def route(self, text):
        fn = self.commands.get(text)
        if fn is None:
            return False
        self._note("routed", text)
        fn()
        return True

    def run_starting(self, req):
        self._note("run_starting", req.text, req.wake, req.author_is_user)

    def run_ended(self, result, req):
        self.results.append(result)
        self._note(
            "run_ended",
            bool(getattr(result, "success", False)),
            str(getattr(result, "output", "") or ""),
            bool(getattr(result, "waiting", False)),
        )

    def run_failed(self, exc):
        self._note("run_failed", type(exc).__name__, str(exc))
        return False


#: 이 시나리오가 세운 세션들 — 실패하면 그때까지 담긴 것을 남긴다(진단용, 기대값에는 들지 않는다)
_LIVE: list = []


class Session:
    """실제 조립 하나 — ``path`` 가 ``"run"`` 이면 QuietPolicy(조용해지면 끝), ``"web"`` 이면 ForeverPolicy
    (``close`` 의 SHUTDOWN 까지). HTTP 는 없다: 웹 호스트의 펌프·큐·정책과 같은 조립이다."""

    def __init__(
        self,
        root: Path,
        provider: Provider,
        *,
        path: str = "run",
        resume: bool = False,
        query: str | None = None,
    ):
        self.root = root
        _LIVE.append(self)
        self.provider = provider
        self.path = path
        self.recorder = Recorder(threading.current_thread())
        set_renderer(self.recorder)
        self.binding = make_binding(provider=provider, capabilities=CAPS, model="m")
        self.sdir = root / "s"
        self.ctx = ContextManager(
            self.sdir, max_context_tokens=60000, resume=resume, binding=self.binding
        )
        self.spec = SessionSpec(
            binding=self.binding,
            session=SimpleNamespace(session_id="oracle"),
            ctx=self.ctx,
            mcp_manager=None,
            knob_args=None,
            hooks_config=None,
            max_turns=30,
            max_depth=2,
            verbose=False,
            record_turns=True,
        )
        self.queue = InputQueue()
        if query is not None:
            self.queue.enqueue(
                None, query
            )  # argv 질의 — 큐가 조립보다 먼저 선다(`run` 과 같다)
        self.stores = build_session_stores(self.spec, queue=self.queue)
        self.owners = build_session_owners(
            self.spec, self.stores, renderer=self.recorder
        )
        self.reg = self.owners.agent_registry
        self.surface = Surface(self.recorder)
        if path == "run":
            policy = QuietPolicy(
                queue=self.queue,
                agent_registry=self.reg,
                monitors=self.stores.monitors,
                schedules=self.stores.schedules,
                harness=self.owners.auditor,
                jobs=self.stores.jobs,
                poll_secs=0.05,
            )
        else:
            policy = ForeverPolicy()
        self.hints: list[str] = []
        self.wired = wire_session(
            self.spec,
            self.stores,
            self.owners,
            queue=self.queue,
            surface=self.surface,
            policy=policy,
            on_mail_notice=(
                (lambda reply, busy: self.hints.append(f"busy={busy}"))
                if path == "run"
                else None
            ),
        )

    def say(self, text: str, nickname=None) -> dict:
        """사람의 입력 한 줄 — 입력 큐로 (CLI 의 argv 질의는 ``nickname=None``)."""
        return self.queue.enqueue(None, text, nickname=nickname)

    def pump(self) -> None:
        """펌프를 돈다 — run 은 조용해지면, web 은 :meth:`close` 의 SHUTDOWN 에서 끝난다."""
        self.wired.pump.run()

    def pump_in_thread(self) -> threading.Thread:
        t = threading.Thread(target=self.pump, daemon=True, name="oracle-pump")
        t.start()
        return t

    def agent_keys(self) -> list[str]:
        return [row["key"] for row in self.reg.roster_snapshot()]

    def agent_state(self, key: str) -> str:
        rows = [r for r in self.reg.roster_snapshot() if r["key"] == key]
        return rows[0]["state"] if rows else ""

    def close(self) -> None:
        self.queue.shutdown()
        teardown_session(
            None,
            self.ctx,
            agent_registry=self.reg,
            monitors=self.stores.monitors,
            schedules=self.stores.schedules,
            harness=self.owners.auditor,
            jobs=self.stores.jobs,
        )

    # ── 담기 ──
    def collect(self, extra: dict | None = None) -> dict:
        records = {}
        for p in sorted(self.sdir.rglob("history.jsonl")):
            rows = []
            for line in p.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                r = json.loads(line)
                rows.append(
                    {
                        k: r.get(k)
                        for k in (
                            "role",
                            "content",
                            "source",
                            "tool",
                            "request_id",
                            "answers",
                            "success",
                        )
                        if k in r
                    }
                )
            records[str(p.relative_to(self.sdir))] = rows
        conversation = {}
        for p in sorted(self.sdir.rglob("conversation.jsonl")):
            conversation[str(p.relative_to(self.sdir))] = [
                json.loads(line)
                for line in p.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
        return {
            "provider_calls": self.provider.calls,
            "records": records,
            "conversation": conversation,
            "events": self.recorder.lanes,
            "mail_hints": self.hints,
            "facts": extra or {},
        }


# ── 정규화 ───────────────────────────────────────────────────

_ID = re.compile(r"\b([unrq])-(\d+)\b")
_AGENT = re.compile(r"\bagt-[0-9a-f]{8}\b")
_TIME_KEYS = frozenset(
    {"ts", "created_at", "asked_at", "duration_s", "elapsed", "send_ts", "req_ts"}
)
_DURATION = re.compile(r"\b\d+(?:\.\d+)?s\b|\b\d+m\d{2}s\b|\b\d+h\d{2}m\b")


def _drop_times(v):
    if isinstance(v, dict):
        return {k: ("<t>" if k in _TIME_KEYS else _drop_times(x)) for k, x in v.items()}
    if isinstance(v, list):
        return [_drop_times(x) for x in v]
    return v


def normalize(doc: dict, *, root: str, agents: list[str] = ()) -> dict:
    """비결정 값을 지운 판 — 경로(``root`` → ``<S>``), 에이전트 키(``agents`` 의 순서 = 띄운 순서로 ``agt-A``…,
    그 밖은 첫 등장 순), 시각 키(``<t>``), 글 속 소요 시간(``<d>``), 항목 id(접두 u-/n-/r-/q- 별로 첫 등장 순
    재번호). 키 이름을 먼저 바꾸고 정렬해 직렬화한 뒤 id 를 번호 매긴다 — 무작위 키의 사전순이 순서를 흔들지
    않게."""
    keys: dict[str, str] = {}
    for k in agents:
        keys.setdefault(k, f"agt-{chr(ord('A') + len(keys))}")

    def agent(m):
        k = m.group(0)
        if k not in keys:
            keys[k] = f"agt-{chr(ord('A') + len(keys))}"
        return keys[k]

    text = json.dumps(_drop_times(doc), ensure_ascii=False)
    text = text.replace(root, "<S>")
    text = _AGENT.sub(agent, text)
    text = _DURATION.sub("<d>", text)
    text = json.dumps(json.loads(text), ensure_ascii=False, sort_keys=True)
    return json.loads(renumber_ids(text))


def renumber_ids(text: str) -> str:
    """``u-/n-/r-/q-`` 접두별로 첫 등장 순 1, 2, … 로 다시 번호를 붙인다(전역 할당기의 번호가 밀려도 같은 판)."""
    seen: dict[tuple[str, str], int] = {}
    per: dict[str, int] = {}

    def sub(m):
        key = (m.group(1), m.group(2))
        if key not in seen:
            per[m.group(1)] = per.get(m.group(1), 0) + 1
            seen[key] = per[m.group(1)]
        return f"{m.group(1)}-{seen[key]}"

    return _ID.sub(sub, text)


# ── 시나리오 ─────────────────────────────────────────────────


def owed_ids(messages) -> list[str]:
    """꼬리의 `## Open Items` 에서 갚을 요청 id 전부(번호 순)."""
    return sorted(
        set(re.findall(r'`reply` with `id` "(r-\d+)"', tail_of(messages))),
        key=lambda i: int(i.split("-")[1]),
    )


def runs_ended(s: Session) -> int:
    return sum(1 for r in s.recorder.lanes.get("pump", []) if r[0] == "run_ended")


def drive(s: Session, *, path: str, until_done) -> None:
    """``path`` 가 run 이면 펌프가 조용해질 때까지, web 이면 펌프를 스레드로 돌리고 ``until_done(s)`` 뒤에
    SHUTDOWN. 어느 쪽이든 끝에 세션을 닫는다."""
    if path == "run":
        s.pump()
        s.close()
        return
    t = s.pump_in_thread()
    until_done(s)
    s.close()
    t.join(10)


def _main_waits_then(box, step):
    """main 이 제자리 대기에 든 **뒤** ``step(msgs)`` — 먼저 오면 `wait` 가 "이미 도착" 으로 돌아간다."""

    def go(msgs):
        if not box["s"].recorder.main_waiting.wait(_PATIENCE_S):
            raise TimeoutError("oracle: main never waited in place")
        return step(msgs)

    return go


def _after_agents_settle(box, step, *, states=("idle", "dead")):
    """에이전트 모두가 한가해지거나 내려온 뒤 ``step(msgs)`` — 그 런의 끝 정리가 main 의 다음 턴보다 먼저."""

    def go(msgs):
        s = box["s"]
        until(
            lambda: all(s.agent_state(k) in states for k in s.agent_keys()),
            "agents settle",
        )
        return step(msgs)

    return go


def s01_request_to_run_agent_explicit_reply(root: Path, path: str) -> dict:
    """§4 요청 r-(→에이전트): put → 런 시작에 picked → `reply` 로 닫힘. main 은 제자리 `wait` 가 받아들여진 채
    기다리다 회신에 깬다."""
    box: dict = {}
    p = Provider(
        {
            "MAIN": [
                call("agent", mode="run", task="TASK-A find callers"),
                call("wait", reason="agent"),
                _after_agents_settle(
                    box, complete_all("main finished"), states=("dead",)
                ),
            ],
            "TASK-A": [
                _main_waits_then(
                    box,
                    lambda m: call(
                        "reply", id=owed_id(m), text="3 callers: a.py, b.py"
                    ),
                ),
                call("complete", result="replied"),
            ],
        }
    )
    s = box["s"] = Session(root, p, path=path)
    s.say("MAIN go")
    drive(s, path=path, until_done=lambda s: until(lambda: runs_ended(s) >= 1))
    return s.collect()


def s02_fallback_reply(root: Path) -> dict:
    """§4 요청 r-: 도구 없이 닫힘 — 런 끝 폴백(라벨 붙은 런 요약). `reply` 없이 `complete` 하면 빚 독촉이
    되돌리고, 예산이 다하면 요약이 요청자에게 간다."""
    box: dict = {}
    p = Provider(
        {
            "MAIN": [
                call("agent", mode="run", task="TASK-B summarize"),
                call("wait"),
                _after_agents_settle(box, complete_all("ok"), states=("dead",)),
            ],
            "TASK-B": [
                _main_waits_then(box, lambda m: call("complete", result="the summary"))
            ]
            + [call("complete", result="the summary")] * 9,
        }
    )
    s = box["s"] = Session(root, p)
    s.say("MAIN go")
    drive(s, path="run", until_done=None)
    return s.collect()


def s03_agent_request_to_main(root: Path, *, main_replies: bool) -> dict:
    """§4 요청 r-(→main): 상주가 main 에게 `message`(회신 기대) → main 앞 메일 → main 이 `reply`(또는 하지 않고
    런을 끝내 폴백). 상주는 `wait` 로 런을 끝냈다가 회신에 새 런으로 깬다."""
    box: dict = {}

    def agent_asks(msgs):
        assert box["s"].recorder.main_waiting.wait(_PATIENCE_S)
        return call("message", to="main", text="which file should I use?")

    def main_answers(msgs):
        s = box["s"]
        key = s.agent_keys()[0]
        until(lambda: s.agent_state(key) == "idle", "asker parked")
        return call("reply", id=owed_id(msgs), text="TASK-C use a.py")

    def main_skips(msgs):
        s = box["s"]
        key = s.agent_keys()[0]
        until(lambda: s.agent_state(key) == "idle", "asker parked")
        # 폴백(main 의 런 요약)이 요청자에게 간다 — 그 런의 표지가 요약에 실린다
        return call(
            "complete", result="TASK-C no answer from main", answers=closing_ids(msgs)
        )

    # 요청자(main)에게 보낸 `message` 는 상주 자신의 빚도 갚는다 — main 의 기다림은 이 메일로 닫힌다
    main_script = [
        call("agent", mode="spawn", name="helper", task="TASK-C do the work"),
        call("wait"),
    ]
    if main_replies:
        main_script += [
            main_answers,
            _after_agents_settle(box, complete_all("main finished")),
        ]
    else:
        # `reply` 없는 `complete` 는 빚 독촉이 되돌린다 — 예산이 다할 때까지 같은 답
        main_script += [main_skips] * 10
    p = Provider(
        {
            "MAIN": main_script,
            "TASK-C": [
                agent_asks,
                call("wait"),
                # 깬 런 — main 의 회신(또는 폴백)을 읽고 끝낸다
                complete_all("read the answer"),
            ],
        }
    )
    s = box["s"] = Session(root, p, path="web")

    def done(s):
        until(lambda: runs_ended(s) >= 1, "main runs")
        key = s.agent_keys()[0]
        until(
            lambda: (
                s.reg.roster_snapshot()[0]["handled"] >= 2
                and s.agent_state(key) == "idle"
            ),
            "asker read the answer",
        )

    s.say("MAIN go", nickname="dj")
    drive(s, path="web", until_done=done)
    return s.collect()


def s04_notices_both_ways(root: Path) -> dict:
    """§4 회신·통지 — 줄 없음: 상주 → main 통지(`message` 회신 기대 없음)는 main 을 깨우고 상주의 빚을 갚되 장부
    항목이 아니다, main → 상주 통지는 상주의 새 런을 열되 빚이 아니다."""
    box: dict = {}
    p = Provider(
        {
            "MAIN": [
                call("agent", mode="spawn", name="helper", task="TASK-D build it"),
                call("wait"),
                _after_agents_settle(
                    box,
                    lambda m: call(
                        "message",
                        to="helper",
                        text="TASK-D fyi: the build passed",
                        expects_reply=False,
                    ),
                ),
                _after_agents_settle(box, complete_all("main finished")),
            ],
            "TASK-D": [
                _main_waits_then(
                    box,
                    lambda m: call(
                        "message", to="main", text="built it", expects_reply=False
                    ),
                ),
                complete_all("ok"),
                complete_all("noted"),
            ],
        }
    )
    s = box["s"] = Session(root, p)
    s.say("MAIN go")
    drive(s, path="run", until_done=None)
    return s.collect()


def s05_agent_to_agent(root: Path) -> dict:
    """§4 에이전트↔에이전트: 상주 X 가 상주 Y 에게 요청 → Y 가 `reply` → X 가 새 런으로 깨어 main 에게 회신."""
    box: dict = {}
    p = Provider(
        {
            "MAIN": [
                call("agent", mode="spawn", name="yy"),
                call("agent", mode="spawn", name="xx", task="TASK-E1 ask yy"),
                call("wait"),
                _after_agents_settle(box, complete_all("main finished")),
            ],
            "TASK-E1": [
                _main_waits_then(
                    box,
                    lambda m: call("message", to="yy", text="TASK-E2 compute 6*7"),
                ),
                call("wait"),
                lambda m: call("reply", id=owed_ids(m)[0], text="yy says 42"),
                call("complete", result="ok"),
            ],
            "TASK-E2": [
                lambda m: call("reply", id=owed_id(m), text="TASK-E1 42"),
                call("complete", result="ok"),
            ],
        }
    )
    s = box["s"] = Session(root, p)
    s.say("MAIN go")
    drive(s, path="run", until_done=None)
    return s.collect()


def s06_human_to_main_and_agent(root: Path) -> dict:
    """§4 사람 u-(→main)·u-(→에이전트): 큐 주입에 줄이 생기고 `complete` 의 answers 로 닫힌다. 에이전트 앞 사람
    봉투는 웹 채널 입력(`request` author=user:…)."""
    box: dict = {}
    p = Provider(
        {
            "MAIN": [
                call("agent", mode="spawn", name="helper"),
                complete_all("spawned"),
                complete_all("hello back"),
            ],
            "TASK-F": [complete_all("hi dj")],
        }
    )
    s = box["s"] = Session(root, p, path="web")
    s.say("MAIN spawn a helper", nickname="dj")

    def done(s):
        until(lambda: runs_ended(s) >= 1)
        key = s.agent_keys()[0]
        until(lambda: s.agent_state(key) == "idle")
        box["agent_input"] = s.reg.request(key, "TASK-F hi there", author="user:dj")
        until(lambda: s.agent_state(key) == "busy" or True)
        until(lambda: s.reg.roster_snapshot()[0]["handled"] >= 1, "human run")
        until(lambda: s.agent_state(key) == "idle")
        s.say("MAIN hello", nickname="dj")
        until(lambda: runs_ended(s) >= 2)

    drive(s, path="web", until_done=done)
    return s.collect({"agent_input": box.get("agent_input")})


def s07_question_to_human(root: Path) -> dict:
    """§4 q-(에이전트→사람): `ask` 로 열린 질문 → 사람의 답(`answer_question`)이 asker 의 새 런이 된다."""
    box: dict = {}
    p = Provider(
        {
            "MAIN": [
                call(
                    "agent", mode="spawn", name="helper", task="TASK-G pick a version"
                ),
                call("wait"),
                _after_agents_settle(box, complete_all("main finished")),
            ],
            "TASK-G": [
                _main_waits_then(
                    box, lambda m: call("ask", question="TASK-G v1 or v2?")
                ),
                lambda m: call("reply", id=owed_id(m), text="asked the user"),
                call("complete", result="ok"),
                complete_all("went with v2"),
            ],
        }
    )
    s = box["s"] = Session(root, p, path="web")
    s.say("MAIN go", nickname="dj")
    facts: dict = {}

    def done(s):
        until(lambda: runs_ended(s) >= 1)
        key = s.agent_keys()[0]
        until(lambda: s.agent_state(key) == "idle")
        (row,) = s.reg.roster_snapshot()
        facts["open_questions"] = [
            {"id": q["id"], "text": q["text"], "to": q["to"]}
            for q in row["open_questions"]
        ]
        qid = row["open_questions"][0]["id"]
        facts["answer"] = s.reg.answer_question(qid, "v2", by="user:dj")
        until(lambda: s.reg.roster_snapshot()[0]["handled"] >= 2, "answer run")
        until(lambda: s.agent_state(key) == "idle")
        facts["open_after"] = s.reg.roster_snapshot()[0]["open_questions"]

    drive(s, path="web", until_done=done)
    return s.collect(facts)


def s08_mid_run_absorption(root: Path) -> dict:
    """§4 런 도중 흡수: 바쁜 상주에게 온 둘째 요청은 그 런의 턴 경계가 흡수해 picked 가 되고, 같은 런이 둘 다
    갚는다. main 쪽은 기다리는 회신 둘."""
    box: dict = {}
    second_sent = threading.Event()

    def main_second(msgs):
        return call("message", to="helper", text="TASK-H also check b.py")

    def main_wait(msgs):
        second_sent.set()
        return call("wait")

    def agent_first(msgs):
        assert second_sent.wait(_PATIENCE_S)
        assert box["s"].recorder.main_waiting.wait(_PATIENCE_S)
        return call("shell", command="true")

    def agent_reply_second(msgs):
        until(lambda: box["s"].recorder.main_waiting.is_set(), "main waits again")
        return call("reply", id=owed_ids(msgs)[0], text="b.py ok")

    p = Provider(
        {
            "MAIN": [
                call("agent", mode="spawn", name="helper", task="TASK-H check a.py"),
                main_second,
                main_wait,
                call("wait"),
                _after_agents_settle(box, complete_all("main finished")),
            ],
            "TASK-H": [
                agent_first,
                lambda m: call("reply", id=owed_ids(m)[0], text="a.py ok"),
                agent_reply_second,
                call("complete", result="ok"),
            ],
        }
    )
    s = box["s"] = Session(root, p)
    s.say("MAIN go")
    drive(s, path="run", until_done=None)
    return s.collect()


def s09_stop_square(root: Path) -> dict:
    """§4 정지 ■: 런만 멈춘다 — 지금 일의 요청자에게 stopped 회신, 기다리던 main 이 깬다."""
    box: dict = {}

    def stop_me(msgs):
        assert box["s"].recorder.main_waiting.wait(_PATIENCE_S)
        s = box["s"]
        box["stop"] = s.reg.stop_run(s.agent_keys()[0])
        return call("shell", command="true")

    p = Provider(
        {
            "MAIN": [
                call("agent", mode="spawn", name="helper", task="TASK-J slow work"),
                call("wait"),
                _after_agents_settle(box, complete_all("main finished")),
            ],
            "TASK-J": [stop_me],
        }
    )
    s = box["s"] = Session(root, p)
    s.say("MAIN go")
    drive(s, path="run", until_done=None)
    return s.collect({"stop_run": box.get("stop")})


def s10_kill_x(root: Path) -> dict:
    """§4 정지 ✕: 지금 일은 stopped, 시작 안 한 대기 요청은 removed — 둘 다 main 메일함에 든 뒤 main 이 본다."""
    box: dict = {}

    def kill_me(msgs):
        s = box["s"]
        until(lambda: len(s.reg.pending_mail()) == 0 and box.get("queued"), "queued")
        box["kill"] = s.reg.kill(s.agent_keys()[0])
        return call("shell", command="true")

    def main_queue_second(msgs):
        box["queued"] = True
        return call("message", to="helper", text="TASK-K second job")

    def main_wait_after_both(msgs):
        s = box["s"]
        until(lambda: s.agent_state(s.agent_keys()[0]) == "dead", "killed")
        until(lambda: len(s.reg.pending_mail()) == 2, "two replies")
        return call("wait")

    first_turn = threading.Event()

    def agent_blocks(msgs):
        first_turn.set()
        return kill_me(msgs)

    p = Provider(
        {
            "MAIN": [
                call("agent", mode="spawn", name="helper", task="TASK-K first job"),
                lambda m: (first_turn.wait(_PATIENCE_S), main_queue_second(m))[1],
                main_wait_after_both,
                complete_all("main finished"),
            ],
            "TASK-K": [agent_blocks],
        }
    )
    s = box["s"] = Session(root, p)
    s.say("MAIN go")
    drive(s, path="run", until_done=None)
    return s.collect({"kill": box.get("kill")})


def s11_stop_everything(root: Path) -> dict:
    """§4 Stop everything: main 의 런과 에이전트의 런을 멈추고, 정지 회신은 main 을 깨우지 않은 채 쌓였다가 사람의
    다음 입력 런이 함께 읽는다."""
    box: dict = {}

    def stop_everything(msgs):
        s = box["s"]
        assert s.recorder.main_waiting.wait(_PATIENCE_S)
        s.surface.stop.set()  # main 먼저 (서버·Ctrl+C 와 같은 순서)
        box["stopped"] = s.reg.stop_all_runs(wakes_requesters=False)
        return call("shell", command="true")

    p = Provider(
        {
            "MAIN": [
                call("agent", mode="run", task="TASK-L slow work"),
                call("wait"),
                complete_all("read the stopped reply"),
            ],
            "TASK-L": [stop_everything],
        }
    )
    s = box["s"] = Session(root, p, path="web")
    s.say("MAIN go", nickname="dj")
    facts: dict = {}

    def done(s):
        until(lambda: runs_ended(s) >= 1)
        key = s.agent_keys()[0]
        until(lambda: s.agent_state(key) == "dead")
        until(lambda: len(s.reg.pending_mail()) == 1, "quiet reply")
        facts["queue_lines"] = s.reg.pending_mail()
        facts["wakes_main"] = s.reg.has_mail_that_wakes_main()
        # 깨우지 않는 회신만 남았다 — 워커가 다 내려오면 `run` 이 끝날 수 있다(조용해짐)
        try:
            until(lambda: not s.reg.has_active_work(), "quiet")
            facts["becomes_quiet"] = True
        except TimeoutError:
            facts["becomes_quiet"] = False
        # 웹 idle 리퍼의 술어 — 깨우지 않는 메일만 남은 방은 걷힐 수 있다(뷰어 없음·main 한가)
        quiet_renderer = SimpleNamespace(
            hub=SimpleNamespace(has_live_connections=lambda: False),
            worker_is_busy=lambda: False,
        )
        facts["web_instance_active"] = web_instance_is_active(
            quiet_renderer,
            SimpleNamespace(pending_count=s.queue.pending_count),
            s.reg,
            monitors=s.stores.monitors,
            schedules=s.stores.schedules,
            harness=s.owners.auditor,
            jobs=s.stores.jobs,
        )
        s.say("MAIN what happened?", nickname="dj")
        until(lambda: runs_ended(s) >= 2)

    drive(s, path="web", until_done=done)
    facts["stopped"] = len(box.get("stopped") or [])
    return s.collect(facts)


def s12_hop_limit(root: Path) -> dict:
    """§4 dropped(hop 상한): hop 6 으로 받은 요청의 회신(폴백)은 요청자에게 가지 않는다."""
    box: dict = {}
    p = Provider(
        {
            "MAIN": [
                call("agent", mode="spawn", name="xx"),
                call("agent", mode="spawn", name="yy"),
                complete_all("spawned"),
            ],
            "TASK-M": [call("complete", result="hop done")] * 10,
        }
    )
    s = box["s"] = Session(root, p, path="web")
    s.say("MAIN go", nickname="dj")
    facts: dict = {}

    def done(s):
        until(lambda: runs_ended(s) >= 1)
        xx, yy = s.agent_keys()
        until(lambda: s.agent_state(xx) == "idle" and s.agent_state(yy) == "idle")
        facts["request"] = s.reg.request(yy, "TASK-M deep", author=f"agent:{xx}", hop=6)
        until(
            lambda: (
                [r["handled"] for r in s.reg.roster_snapshot() if r["key"] == yy] == [1]
            ),
            "yy ran",
        )
        until(lambda: s.agent_state(yy) == "idle")
        facts["xx_handled"] = [
            r["handled"] for r in s.reg.roster_snapshot() if r["key"] == xx
        ]

    drive(s, path="web", until_done=done)
    return s.collect(facts)


def s13_wait_refusals_and_wakes(root: Path) -> dict:
    """§4 제자리 wait 거절·수락·깬 이유: 기다릴 것 없음 → 거절 / 메일이 이미 도착 → 거절(다음 턴에 배달) / 사람
    말에 깸(기다림은 그대로) / 회신에 깸 / 한가한 main 을 메일이 깨운다(메일 런)."""
    box: dict = {}

    def job_mail(n: int) -> str:
        return box["s"].reg.deliver(
            "main",
            mail={
                "kind": "job",
                "success": True,
                "output": f"🧵 job #{n} finished rc=0 after 3m00s",
            },
            text="",
            author="",
            expects_reply=False,
        )

    def job_mail_then_wait(msgs):
        box["job1"] = job_mail(1)
        return call("wait")

    p = Provider(
        {
            "MAIN": [
                call("wait"),  # 기다릴 것 없음
                job_mail_then_wait,  # 이미 도착
                complete_all("job seen"),
                # 둘째 런(사람): 느린 run 에이전트를 기다리다 사람 말에 깨고, 다시 기다리다 회신에 깬다
                call("agent", mode="run", task="TASK-N slow work"),
                call("wait"),
                call("wait"),
                complete_all("read the stopped reply"),
                # 셋째 런(메일 런): 한가한 main 을 작업 통지가 깨웠다
                complete_all("second job seen"),
            ],
            "TASK-N": [
                lambda m: (
                    box["agent_go"].wait(_PATIENCE_S),
                    call("shell", command="true"),
                )[1]
            ],
        }
    )
    box["agent_go"] = threading.Event()
    s = box["s"] = Session(root, p, path="web")
    s.say("MAIN first", nickname="dj")
    facts: dict = {}

    def waits(s) -> int:
        return s.recorder.counts.get("wait_in_place", 0)

    def done(s):
        until(lambda: runs_ended(s) >= 1)
        s.say("MAIN second", nickname="dj")
        until(lambda: waits(s) == 1, "first wait")
        s.say("MAIN keep waiting", nickname="dj")
        until(lambda: waits(s) == 3, "second wait")
        key = s.agent_keys()[0]
        facts["stop"] = s.reg.stop_run(key)
        box["agent_go"].set()
        until(lambda: runs_ended(s) >= 2)
        until(lambda: s.agent_state(key) == "dead")
        facts["job2"] = job_mail(2)
        until(lambda: runs_ended(s) >= 3, "mail run")

    drive(s, path="web", until_done=done)
    facts["job1"] = box.get("job1")
    return s.collect(facts)


def s14_restart_resume(root: Path, *, query: bool) -> dict:
    """§4 재시작: 세션 끝(런 도중)·미배달 메일·에이전트→main 미배달 요청 → 저장 → 새 조립이 복원 → 로스터·
    다음 런의 모델 글·기다리는 회신. ``query`` 면 `run --resume` 처럼 첫 질의가 펌프보다 먼저 큐에 있다(질의가 먼저
    연다), 아니면 웹 resume 처럼 질의 없이 — 복원된 메일이 한가한 펌프를 깨운다. ``agents.json`` 바이트는 담지
    않는다."""
    facts: dict = {}
    box: dict = {}
    # ── 첫 세션: 상주 둘을 띄우고 펌프를 닫는다(웹 서버가 내려간 것과 같다) ──
    p1 = Provider(
        {
            "MAIN": [
                call("agent", mode="spawn", name="worker"),
                call("agent", mode="spawn", name="asker"),
                complete_all("spawned"),
            ],
            "TASK-P": [
                lambda m: call("message", to="main", text="TASK-Q which branch?"),
                call("wait"),
            ],
            "TASK-O1": [
                lambda m: (
                    until(
                        lambda: box["s"].agent_state(box["worker"]) == "dead",
                        "session end",
                    ),
                    call("shell", command="true"),
                )[1]
            ],
        }
    )
    s = box["s"] = Session(root, p1, path="web")
    s.say("MAIN go", nickname="dj")
    t = s.pump_in_thread()
    until(lambda: runs_ended(s) >= 1)
    s.queue.shutdown()
    t.join(10)
    worker, asker = s.agent_keys()
    box["worker"] = worker
    until(lambda: s.agent_state(worker) == "idle" and s.agent_state(asker) == "idle")
    # 펌프가 없는 동안: asker 가 main 에게 묻고(미배달 요청) 기다린다, worker 는 첫 일 도중 · 둘째는 줄에
    s.reg.request(asker, "TASK-P ask main about the branch")
    until(lambda: len(s.reg.pending_mail()) == 1, "asker's request")
    until(lambda: s.agent_state(asker) == "idle")
    s.reg.request(worker, "TASK-O1 first job")
    until(lambda: s.agent_state(worker) == "busy")
    s.reg.request(worker, "TASK-O2 second job")
    facts["before"] = {
        "queue_lines": s.reg.pending_mail(),
        "awaited_asker": s.reg.awaited_replies(f"agent:{asker}"),
        "awaited_main": s.reg.awaited_replies("main"),
    }
    teardown_session(
        None,
        s.ctx,
        agent_registry=s.reg,
        monitors=s.stores.monitors,
        schedules=s.stores.schedules,
        harness=s.owners.auditor,
        jobs=s.stores.jobs,
    )
    first = s.collect()

    # ── 둘째 세션: 같은 폴더로 다시 조립(`--resume`) ──
    # 한 번에 한 배우만: main 의 첫 응답이 시작되면 되살린 worker 가 회신하고, main 은 worker 가 한가해진 뒤
    # asker 에게 회신한다(asker 의 런이 보는 로스터가 흔들리지 않게). worker 의 회신은 main 의 다음 턴 경계에 온다.
    box2: dict = {}
    main_started = threading.Event()

    def main_replies_to_asker(msgs):
        main_started.set()
        until(lambda: box2["s"].agent_state(worker) == "idle", "worker replied")
        return call("reply", id=owed_id(msgs), text="TASK-P use main")

    def worker_replies(msgs):
        assert main_started.wait(_PATIENCE_S)
        return call("reply", id=owed_id(msgs), text="resumed run done")

    p2 = Provider(
        {
            "MAIN": [
                main_replies_to_asker,
                _after_agents_settle(box2, complete_all("resumed and done")),
            ],
            # 배우는 컨텍스트의 첫 사용자 메시지로 가른다 — 되살린 worker 의 런은 TASK-O1 의 대본이다
            "TASK-O1": [worker_replies],
            "TASK-P": [lambda m: call("complete", result="got the branch", answers=[])],
        }
    )
    s2 = box2["s"] = Session(
        root,
        p2,
        path="run" if query else "web",
        resume=True,
        query="MAIN resumed query" if query else None,
    )
    # 되살린 worker 는 끊긴 런을 다시 집어 대본에서 main 의 대기를 기다린다 — 그 상태에서 사실을 뜬다
    until(lambda: s2.agent_state(worker) == "busy", "worker resumed")
    facts["after_restore"] = {
        "revived": s2.wired.revived,
        "roster": [
            {k: r[k] for k in ("key", "name", "state", "pending_requests", "handled")}
            for r in s2.reg.roster_snapshot()
        ],
        "queue_lines": s2.reg.pending_mail(),
        "awaited_asker": s2.reg.awaited_replies(f"agent:{asker}"),
        "awaited_main": s2.reg.awaited_replies("main"),
    }

    def done(s):
        until(lambda: runs_ended(s) >= 1, "mail run")
        until(lambda: all(s.agent_state(k) == "idle" for k in s.agent_keys()))

    drive(s2, path="run" if query else "web", until_done=done)
    second = s2.collect()
    return {"first": first, "second": second, "facts": facts}


def _job_mail(s: Session, n: int) -> str:
    """main 앞 작업 통지 한 통 — 작업 레지스트리가 쓰는 공개 길(`deliver`)."""
    return s.reg.deliver(
        "main",
        mail={
            "kind": "job",
            "success": True,
            "output": f"🧵 job #{n} finished rc=0 after 3m00s",
        },
        text="",
        author="",
        expects_reply=False,
    )


def s15_mail_after_the_last_boundary(root: Path) -> dict:
    """§5 마지막 턴 경계 뒤 도착: main 이 마지막 응답(`complete`)을 만드는 동안 온 메일 — 그 런은 끝나고, 메일이
    한가해진 main 을 새 런으로 깨운다(run 경로 — 조용해지기 전에 배달된다)."""
    box: dict = {}

    def complete_after_mail(msgs):
        box["job"] = _job_mail(box["s"], 1)
        return complete_all("done before the mail")(msgs)

    p = Provider(
        {
            "MAIN": [complete_after_mail, complete_all("saw the late mail")],
        }
    )
    s = box["s"] = Session(root, p)
    s.say("MAIN go")
    drive(s, path="run", until_done=None)
    return s.collect({"job": box.get("job")})


def s16_mail_during_a_command(root: Path) -> dict:
    """§5 명령 뒤 회신: 런을 열지 않는 명령을 처리하는 동안 온 메일도 놓치지 않는다 — 명령 차례가 끝나면 메일
    런이 열린다."""
    box: dict = {}
    p = Provider({"MAIN": [complete_all("saw the mail")]})
    s = box["s"] = Session(root, p, path="web")
    s.surface.commands["/noop"] = lambda: box.setdefault("job", _job_mail(s, 1))
    s.say("/noop", nickname="dj")
    drive(s, path="web", until_done=lambda s: until(lambda: runs_ended(s) >= 1))
    return s.collect({"job": box.get("job")})


def s17_mail_to_an_idle_main_on_the_run_path(root: Path) -> dict:
    """§5 run 경로의 메일 런: main 의 런이 끝난 뒤(한가함) 상주의 통지가 오면 CLI 대기 줄 없이(``busy=False``)
    메일 런이 열리고, 조용해지면 `run` 이 끝난다."""
    box: dict = {}

    def notify_after_main_ended(msgs):
        until(lambda: runs_ended(box["s"]) >= 1, "main run ended")
        return call("message", to="main", text="all done", expects_reply=False)

    p = Provider(
        {
            "MAIN": [
                call("agent", mode="spawn", name="helper"),
                call(
                    "message",
                    to="helper",
                    text="TASK-R do it and tell me",
                    expects_reply=False,
                ),
                complete_all("handed off"),
                _after_agents_settle(box, complete_all("heard back")),
            ],
            "TASK-R": [notify_after_main_ended, complete_all("ok")],
        }
    )
    s = box["s"] = Session(root, p)
    s.say("MAIN go")
    drive(s, path="run", until_done=None)
    return s.collect()


SCENARIOS = {
    "s01_run": lambda root: s01_request_to_run_agent_explicit_reply(root, "run"),
    "s01_web": lambda root: s01_request_to_run_agent_explicit_reply(root, "web"),
    "s02_fallback": s02_fallback_reply,
    "s03_agent_to_main_reply": lambda root: s03_agent_request_to_main(
        root, main_replies=True
    ),
    "s03_agent_to_main_fallback": lambda root: s03_agent_request_to_main(
        root, main_replies=False
    ),
    "s04_notices": s04_notices_both_ways,
    "s05_agent_to_agent": s05_agent_to_agent,
    "s06_human": s06_human_to_main_and_agent,
    "s07_question": s07_question_to_human,
    "s08_absorb": s08_mid_run_absorption,
    "s09_stop_square": s09_stop_square,
    "s10_kill_x": s10_kill_x,
    "s11_stop_everything": s11_stop_everything,
    "s12_hop_limit": s12_hop_limit,
    "s13_wait": s13_wait_refusals_and_wakes,
    "s14_restart_query": lambda root: s14_restart_resume(root, query=True),
    "s14_restart_mail_run": lambda root: s14_restart_resume(root, query=False),
    "s15_late_mail": s15_mail_after_the_last_boundary,
    "s16_mail_during_command": s16_mail_during_a_command,
    "s17_idle_mail_run_path": s17_mail_to_an_idle_main_on_the_run_path,
}


def run_all(
    out: Path, only: list[str] | None = None, work: Path | None = None
) -> dict[str, str]:
    """시나리오를 돌려 ``out/raw/<이름>.json``(원본)·``out/<이름>.json``(정규화)을 쓰고 정규화 판의 sha256 을
    돌려준다. 작업 폴더는 임시 폴더 — cwd·HOME 을 거기로 옮겨 사용자 설정을 건드리지 않는다."""
    out = out.resolve()
    work = work.resolve() if work is not None else None
    out.mkdir(parents=True, exist_ok=True)
    (out / "raw").mkdir(exist_ok=True)
    sums: dict[str, str] = {}
    old_cwd, old_env, old_renderer = os.getcwd(), dict(os.environ), get_renderer()
    try:
        for name, fn in SCENARIOS.items():
            if only and name not in only:
                continue
            with tempfile.TemporaryDirectory(prefix="oracle-", dir=work) as tmp:
                root = Path(tmp).resolve()
                (root / "home").mkdir()
                os.chdir(root)
                os.environ["HOME"] = str(root / "home")
                os.environ["AGENT_CLI_SNAPSHOT"] = "0"
                _LIVE.clear()
                try:
                    doc = fn(root)
                except BaseException:
                    for i, live in enumerate(_LIVE):
                        (out / "raw" / f"{name}.FAILED-{i}.json").write_text(
                            json.dumps(live.collect(), ensure_ascii=False, indent=1),
                            encoding="utf-8",
                        )
                    raise
                finally:
                    set_renderer(old_renderer)
                    os.chdir(old_cwd)
                agents: list[str] = []
                for live in _LIVE:
                    agents += [k for k in live.agent_keys() if k not in agents]
                # 원본도 작업 폴더 경로만은 지운다 — 사용자 경로가 저장소에 들어가지 않게
                raw = json.dumps(
                    doc, ensure_ascii=False, sort_keys=True, indent=1
                ).replace(str(root), "<S>")
                norm = json.dumps(
                    normalize(doc, root=str(root), agents=agents),
                    ensure_ascii=False,
                    sort_keys=True,
                    indent=1,
                )
            (out / "raw" / f"{name}.json").write_text(raw + "\n", encoding="utf-8")
            (out / f"{name}.json").write_text(norm + "\n", encoding="utf-8")
            sums[name] = hashlib.sha256((norm + "\n").encode("utf-8")).hexdigest()
    finally:
        os.environ.clear()
        os.environ.update(old_env)
        os.chdir(old_cwd)
    return sums


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--only", nargs="*")
    ap.add_argument(
        "--work", type=Path, help="작업 폴더를 만들 곳(기본: 시스템 임시 폴더)"
    )
    args = ap.parse_args()
    sums = run_all(args.out, args.only, args.work)
    lines = [f"{h}  {n}.json" for n, h in sorted(sums.items())]
    if not args.only:
        (args.out / "SHA256SUMS").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))


if __name__ == "__main__":
    main()
