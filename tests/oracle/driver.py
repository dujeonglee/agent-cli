"""메일함·요청 장부 오라클 드라이버 (docs: 메일함 시안 v4.1 §8 1c).

f44cf539(v10.143.0) 에서 만든 기대값이 그 뒤의 구조 교체(우편함·장부)를 재는 자다. 그래서 이 드라이버는
**공개 표면만** 쓴다 — 실제 세션 조립(`runtime.build_session_stores`·`build_session_owners`·`wire_session`),
펌프, 대본 응답의 가짜 provider, 레지스트리의 공개 메서드(`request`·`deliver`·`stop_run`·`kill`·
`stop_all_runs`·`answer_question`·`restore`·`shutdown_all`), main 앞 사람의 말(`MainInput` — v10.146.6 까지는
입력 큐 `InputQueue`, 드라이버는 `Session` 의 메서드 다섯만 바뀌었다). 레지스트리의 저장소를 읽지 않고,
조회도 쓰지 않는다(f44cf539 에 없는 표면). 가드 테스트(`test_oracle.py`)가 import 목록을 고정한다.

담는 것: 배우(main·에이전트)마다 provider 호출의 마지막 메시지(꼬리의 `## Open Items` 가 여기 있다),
세션 폴더의 `history.jsonl`(모델 레코드 — content·source·tool·request_id·answers)과 `conversation.jsonl`,
렌더러 이벤트(스레드별 열), 시나리오가 직접 본 결과(도구 결과·재시작 뒤 사실). `agents.json` 바이트는
담지 않는다.

결정성: 시나리오마다 한 번에 한 배우만 움직이도록 대본이 공개 상태(렌더러 이벤트·에이전트 상태)를 보고
기다린다. 이벤트는 스레드별 열로 담는다 — 스레드 사이의 끼어듦은 담지 않는다. 비결정 값은 `normalize`
가 지운다.

재생성: ``python3 -m tests.oracle.driver --out tests/oracle/expected`` — 언제나 **전체**로 돌린다(``--only`` 로 일부만
돌리면 앞 시나리오가 쓴 id 가 없어 원본 id 의 자릿수가 달라지고, 머리 자르기가 한 글자 달라지는 칸이 생긴다).
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
from agent_cli.main import web_instance_is_active
from agent_cli.main_input import MainInput
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
#: 갚지 않은 빚이 있는 `complete` 를 하니스가 되돌리는 횟수 — 제품의 되묻기 예산(`loop.ledger.NAG_BUDGET`)과 같다.
#: 대본이 그 호출 수만큼 줄을 둔다(공개 표면만 import 하는 규칙이라 값을 적는다 — 어긋나면 기대값 diff 로 드러난다).
NAG_BUDGET = 8


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
        #: 에이전트 키 → 그 키의 런이 시작될 때(``begin_agent_work``) 워커를 죽일 신호 — 세워지면 터진다.
        #: 원인 미상의 사망을 흉내 낸다(요청 처리기 밖의 기계 사망 — `test_machinery_crash_notifies_main` 과 같은
        #: 수단). 기록은 터지기 전에 남는다.
        self.crash_on_start: dict[str, threading.Event] = {}
        #: ``(사건 이름, 에이전트 키 또는 "*", 조건)`` — 그 키의 스레드가 그 사건을 적은 뒤 ``조건`` 이 참이 될 때까지 그
        #: 스레드를 잡는다. 그 스레드가 다음에 짓는 꼬리(로스터의 남의 상태)보다 다른 배우의 사건을 앞세운다.
        #: 한 번 쓰면 빠진다(`hold`).
        self.holds: list[list] = []
        #: 렌더러 메서드 이름 → ``(args, kwargs)`` 를 받아 그 스레드를 잡는 규칙 — 세션이 건다(`Session.settle_rules`).
        #: 기록되는 사건이든(적은 뒤) 아니든(관찰 카드 등) 같다. 한 번 쓰고 빠지지 않는다 — 모든 시나리오의 규칙이다.
        self.rules: dict = {}

    def hold(self, event: str, key: str, cond) -> None:
        self.holds.append([event, key, cond])

    def lane(self) -> str:
        t = threading.current_thread()
        if t is self._main or t.name == "oracle-pump":
            return "main"
        if t.name.startswith("agent-"):
            return t.name
        return "other"

    def __getattr__(self, name):
        target = getattr(self._inner, name)
        if not callable(target):
            return target
        if name not in EVENTS:
            rule = self.rules.get(name)
            if rule is None:
                return target

            def ruled(*args, **kwargs):
                rule(args, kwargs)
                return target(*args, **kwargs)

            return ruled

        def recorded(*args, **kwargs):
            with self._lock:
                self.lanes.setdefault(self.lane(), []).append(
                    [name, _plain(list(args)), _plain(kwargs)]
                )
                self.counts[name] = self.counts.get(name, 0) + 1
            rule = self.rules.get(name)
            if rule is not None:
                rule(args, kwargs)
            for h in list(self.holds):
                if h[0] == name and h[1] in ("*", kwargs.get("key")):
                    self.holds.remove(h)
                    until(h[2], f"held {name}")
                    break
            if name == "begin_agent_work" and kwargs.get("key") in self.crash_on_start:
                gate = self.crash_on_start.pop(kwargs["key"])
                if not gate.wait(_PATIENCE_S):
                    raise TimeoutError("oracle: crash gate never opened")
                raise MemoryError("oracle: simulated crash in worker machinery")
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
        #: 런을 열기 직전(`run_starting` 을 적은 뒤) 부를 것 — ``(start)``. 펌프 스레드를 잡아 그 런의 첫 꼬리보다
        #: 다른 배우의 사건을 앞세운다.
        self.before_run = None

    def _note(self, *row):
        with self.rec._lock:
            self.rec.lanes.setdefault("pump", []).append(list(row))

    def idle(self):
        pass

    def busy(self):
        pass

    def echo(self, item):
        # 사람의 말로 연 차례만 불린다(메일 런은 부르지 않는다) — 셋째 값은 옛 기록과 같은 모양을 지키려는 False
        self._note("echo", str(item.get("text") or ""), False)

    def bind_stop(self, event):
        self.stop = event

    def is_command(self, text):
        return text in self.commands

    def route(self, text, start=None, *, item=None):
        fn = self.commands.get(text)
        if fn is None:
            return False
        self._note("routed", text)
        fn()
        return True

    def run_starting(self, start):
        self._note(
            "run_starting", start.query, not start.author_is_user, start.author_is_user
        )
        if self.before_run is not None:
            self.before_run(start)

    def run_ended(self, result, start):
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
        holds=(),
    ):
        """``holds``: 조립 **전에** 거는 `Recorder.hold` 들 — 되살린 워커처럼 조립 도중에 시작하는 것을 잡는다."""
        self.root = root
        _LIVE.append(self)
        self.provider = provider
        self.path = path
        self.recorder = Recorder(threading.current_thread())
        for h in holds:
            self.recorder.hold(*h)
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
        self.main_input = MainInput()
        if query is not None:
            self.say(query)  # argv 질의 — 큐가 조립보다 먼저 선다(`run` 과 같다)
        self.stores = build_session_stores(self.spec, main_input=self.main_input)
        self.owners = build_session_owners(
            self.spec, self.stores, renderer=self.recorder
        )
        self.reg = self.owners.agent_registry
        self.recorder.rules.update(self.settle_rules())
        self.surface = Surface(self.recorder)
        if path == "run":
            policy = QuietPolicy(
                main_input=self.main_input,
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
            surface=self.surface,
            policy=policy,
            on_mail_notice=(
                (lambda reply, busy: self.hints.append(f"busy={busy}"))
                if path == "run"
                else None
            ),
        )

    def settle_rules(self) -> dict:
        """모든 시나리오의 순서 규칙 — 띄운 결과 카드(`observation`, 도구 ``agent`` 의 "spawned/started agent")를
        그린 스레드를 띄운 에이전트가 모두 시작을 마칠 때까지 잡는다. 기대값의 꼬리는 그 뒤의 로스터다 — 막 띄운
        것은 ``starting``, 과제가 있으면 그것을 집기 전 ``idle`` 인데 구현은 꼬리보다 먼저 끝남을 보장하지 않는다."""

        def agents_settled() -> bool:
            return all(
                r["state"] != "starting"
                and not (r["state"] == "idle" and r.get("pending_requests"))
                for r in self.reg.roster_snapshot()
            )

        def after_spawn(args, kwargs):
            content = str(args[0] if args else kwargs.get("content") or "")
            tool = args[2] if len(args) > 2 else kwargs.get("tool_name")
            if tool == "agent" and content.startswith(
                ("spawned agent", "started agent")
            ):
                until(agents_settled, "spawned agents settle")

        return {"observation": after_spawn}

    def settle_repliers_before_main_reads(self) -> None:
        """main 이 제자리 대기에서 깰 때(``wait_in_place(False)``) 바쁜 에이전트가 없어질 때까지 main 을 잡는다 —
        회신·정지 회신을 배달한 쪽은 배달(`end_run`·`answer_debts_as_stopped`) **뒤** 런을 정리(`_close_run`, idle)
        한다. 그 틈에서 main 이 꼬리를 지으면 [busy], 뒤면 [idle] — 제품의 비결정이라 정착된 쪽을 강제한다."""

        def after_wake(args, kwargs):
            if args and args[0] is False:
                until(
                    lambda: all(
                        r["state"] != "busy" for r in self.reg.roster_snapshot()
                    ),
                    "repliers settle",
                )

        self.recorder.rules["wait_in_place"] = after_wake

    def dead_stopped_runs_before_main_reads(self) -> None:
        """main 이 정지 회신("stopped by user")에 깰 때 멈춘 run 이 내려올 때까지 main 을 잡는다 — 정지 회신을 보낸
        뒤 run 이 런을 정리하고(idle) 내려오기(dead)까지의 틈을 꼬리가 보지 않게."""

        def stopped_mail() -> bool:
            return any(
                "stopped by user" in (m.get("text") or "")
                for m in self.reg.pending_mail()
            )

        def after_wake(args, kwargs):
            if args and args[0] is False and stopped_mail():
                until(
                    lambda: all(
                        r["state"] == "dead" for r in self.reg.roster_snapshot()
                    ),
                    "stopped run came down",
                )

        self.recorder.rules["wait_in_place"] = after_wake

    def close_answered_awaits_before_main_reads(self) -> None:
        """main 이 제자리 대기에서 깰 때(``wait_in_place(False)``) 도착한 회신이 답하는 "기다리는 회신" 이 닫힐
        때까지 main 을 잡는다 — 회신한 쪽은 배달한 **뒤** 보낸 쪽 기록을 지운다(`AgentRegistry.awaited_replies`).
        기대값의 띠(`open_items`)는 그 뒤의 것이다. 회신한 쪽이 그다음 곧바로 런을 끝내는 시나리오에서는 쓰지
        않는다 — main 의 꼬리가 회신한 쪽의 "바쁨" 을 보는 순서와 함께 강제할 손잡이가 없다."""

        def answered_awaits_closed() -> bool:
            heads = " ".join(m.get("text") or "" for m in self.reg.pending_mail())
            return not any(
                f"re [{r['id']}]" in heads for r in self.reg.awaited_replies("main")
            )

        def after_wake(args, kwargs):
            if args and args[0] is False:
                until(answered_awaits_closed, "answered awaits close")

        self.recorder.rules["wait_in_place"] = after_wake

    def say(self, text: str, nickname=None) -> dict:
        """사람의 입력 한 줄 — 입력 큐로 (CLI 의 argv 질의는 ``nickname=None``)."""
        return self.main_input.enqueue(None, text, nickname=nickname)

    def pending_count(self) -> int:
        """main 앞에 줄 선 사람의 입력 수 — 웹 idle 리퍼가 보는 값."""
        return self.main_input.pending_count()

    def main_idle(self) -> bool:
        """펌프가 런을 열 것을 기다리는 중인가(한가함) — CLI 메일 대기 줄의 ``busy`` 가 이 값이다."""
        return self.main_input.idle()

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

    def stop_pump(self) -> None:
        """펌프를 끝낸다(웹 서버 종료와 같은 길) — 줄 선 사람의 입력은 먼저 꺼낸다. 세션은 닫지 않는다."""
        self.main_input.close()

    def close(self) -> None:
        self.stop_pump()
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
#: 키 앞의 경계 — 단어 경계, 또는 직렬화된 줄바꿈(``\\n``) 바로 뒤(died 통지는 줄 머리가 키다: ``…──\\nagt-…``
#: 에서 ``n`` 과 ``a`` 사이는 단어 경계가 아니다). 기존 기대값 20개에는 그 모양이 없어 바이트가 그대로다.
_AGENT = re.compile(r"(?:\b|(?<=\\n))agt-[0-9a-f]{8}\b")
_TIME_KEYS = frozenset(
    {"ts", "created_at", "asked_at", "duration_s", "elapsed", "send_ts", "req_ts"}
)
_DURATION = re.compile(r"\b\d+(?:\.\d+)?s\b|\b\d+m\d{2}s\b|\b\d+h\d{2}m\b")
#: 작업의 프로세스 번호 — 꼬리의 ``## Background jobs`` 줄(``pid 123 · … `kill -- -123```). 기존 기대값 20개에는
#: 숫자 pid 가 없어(작업을 띄우는 시나리오가 없다) 바이트가 그대로다.
_PID = re.compile(r"\bpid \d+\b|kill -- -\d+\b")


def _drop_times(v):
    if isinstance(v, dict):
        return {k: ("<t>" if k in _TIME_KEYS else _drop_times(x)) for k, x in v.items()}
    if isinstance(v, list):
        return [_drop_times(x) for x in v]
    return v


def normalize(doc: dict, *, root: str, agents: list[str] = ()) -> dict:
    """비결정 값을 지운 판 — 경로(``root`` → ``<S>``), 에이전트 키(``agents`` 의 순서 = 띄운 순서로 ``agt-A``…,
    그 밖은 첫 등장 순), 시각 키(``<t>``), 글 속 소요 시간(``<d>``), 작업 pid(``<p>``), 항목 id(접두 u-/n-/r-/q- 별로 첫 등장 순
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
    text = _PID.sub(
        lambda m: "pid <p>" if m.group(0).startswith("pid") else "kill -- -<p>", text
    )
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


def _after_main_calls(box, n: int, step):
    """main 의 provider 호출이 ``n`` 번 시작된 **뒤** ``step(msgs)`` — 회신한 배우가 main 의 다음 꼬리(로스터의
    ``[busy]``)가 지어지기 전에 내려오지 않게 한다. 호출 기록은 호출이 시작될 때 쌓인다(꼬리를 지은 뒤)."""

    def go(msgs):
        until(
            lambda: len(box["s"].provider.calls.get("MAIN", [])) >= n,
            "main's next call",
        )
        return step(msgs)

    return go


def _after_runs_began(box, n: int, step):
    """에이전트 런이 모두 ``n`` 번 시작된(``begin_agent_work``) **뒤** ``step`` — 시작은 빚을 적은 뒤다
    (`ResidentLoop.start` → `open_scope`). 먼저 `wait` 하면 근거가 "working" 하나로 줄어든다(빚 줄이 아직 없다)."""

    def go(msgs):
        rec = box["s"].recorder
        until(lambda: rec.counts.get("begin_agent_work", 0) >= n, "agent runs began")
        return step if isinstance(step, str) else step(msgs)

    return go


def _held_peer_message(box, sender: str, to: str, text: str) -> str:
    """``sender`` 가 ``to`` 에게 회신 기대 `message` — 받는 쪽 런의 첫 꼬리는 보낸 쪽이 `wait` 로 런을 끝낸 뒤
    (한가함)에, 보낸 쪽의 다음 꼬리는 받는 쪽이 일을 집은 뒤(바쁨)에 짓게 잡는다."""
    s = box["s"]
    a, b = _agent_key(s, sender), _agent_key(s, to)
    s.recorder.hold("begin_agent_work", b, lambda: s.agent_state(a) == "idle")
    # 요청의 창 줄(`agent_message` — 받는 쪽 키, 보낸 쪽 스레드, 받은편지함에 넣은 뒤)에서 보낸 쪽을 잡는다
    s.recorder.hold("agent_message", b, lambda: s.agent_state(b) == "busy")
    return call("message", to=to, text=text)


def _held_request_to(box, to: str, text: str, *, expects_reply: bool = True) -> str:
    """보내는 쪽이 ``to`` 에게 `message`(회신 기대, ``expects_reply`` 가 거짓이면 통지) — 보낸 쪽의 다음 꼬리는 받는
    쪽이 일을 집은 뒤(바쁨)에 짓게 잡는다(요청의 창 줄 `agent_message` 에서). 안 잡으면 부하 아래 그 꼬리가
    "idle · 1 queued" 로 찍힌다(s22 가, 컨테이너 cpus 0.5·부하 1회, 2026-11-17; s17 의 통지도, 750회 중 1회)."""
    s = box["s"]
    b = _agent_key(s, to)
    s.recorder.hold("agent_message", b, lambda: s.agent_state(b) == "busy")
    return call(
        "message",
        to=to,
        text=text,
        **({} if expects_reply else {"expects_reply": False}),
    )


def _wake_runs_after_idle(s: Session, name: str) -> None:
    """메일이 연 main 의 런(``wake``)은 ``name`` 이 런을 끝낸(한가한) 뒤에 연다 — 그 상주의 폴백이 연 런의 첫 글이
    상주의 상태(바쁨↔한가함)를 보는 순서가 갈리지 않게(s17 과 같은 규칙, v10.145.1)."""
    s.surface.before_run = lambda start: (
        until(lambda: s.agent_state(_agent_key(s, name)) == "idle", f"{name} done")
        if not start.author_is_user
        else None
    )


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
                _after_runs_began(box, 1, call("wait", reason="agent")),
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
                # 회신을 읽은 main 의 꼬리(셋째 호출)가 run 의 "바쁨" 을 본 뒤에 내려온다
                _after_main_calls(box, 3, lambda m: call("complete", result="replied")),
            ],
        }
    )
    s = box["s"] = Session(root, p, path=path)
    s.close_answered_awaits_before_main_reads()
    s.say("MAIN go")
    drive(s, path=path, until_done=lambda s: until(lambda: runs_ended(s) >= 1))
    return s.collect()


def s02_fallback_reply(root: Path) -> dict:
    """§4 요청 r-: 도구 없이 닫힘 — 런 끝 폴백(라벨 붙은 런 요약). `reply` 없이 `complete` 하면 빚 독촉이
    되돌리고, 예산이 다하면 요약이 요청자에게 간다.

    폴백을 읽은 main 꼬리의 로스터는 지금 구현에서 `end_run`↔`_close_run` 틈 때문에 [busy]/[idle] 둘 다 나올 수
    있다 — 기대값은 정착된 쪽(회신한 run 이 idle)을 강제해 기록한다(v10.144.1 에서 재기록). 회신 레코드의 상태
    꼬리줄("(agent status: …)")도 회신 시점이 아니라 main 이 흡수할 때의 상태를 읽어 함께 움직인다."""
    box: dict = {}
    p = Provider(
        {
            "MAIN": [
                call("agent", mode="run", task="TASK-B summarize"),
                _after_runs_began(box, 1, call("wait")),
                _after_agents_settle(box, complete_all("ok"), states=("dead",)),
            ],
            "TASK-B": [
                _main_waits_then(box, lambda m: call("complete", result="the summary"))
            ]
            + [call("complete", result="the summary")] * 9,
        }
    )
    s = box["s"] = Session(root, p)
    # 폴백을 보낸 run 이 런을 정리(idle)한 뒤 main 이 읽는다 — run 은 main 의 셋째 호출까지 내려오지 않는다
    s.settle_repliers_before_main_reads()
    s.recorder.hold("end_agent_work", "*", lambda: len(p.calls.get("MAIN", [])) >= 3)
    s.say("MAIN go")
    drive(s, path="run", until_done=None)
    return s.collect()


def s03_agent_request_to_main(root: Path, *, main_replies: bool) -> dict:
    """§4 요청 r-(→main): 상주가 main 에게 `message`(회신 기대) → main 앞 메일 → main 이 `reply`(또는 하지 않고
    런을 끝내 폴백). 상주는 `wait` 로 런을 끝냈다가 회신에 새 런으로 깬다. `[v10.145.1 에서 재기록 — 그 `message` 는
    상주의 과제 빚(TASK-C)을 갚지 않는다: 깬 런의 `complete` 가 되묻기 예산을 다 쓰고 런 끝 폴백이 main 에게 간다]`"""
    box: dict = {}

    def agent_asks(msgs):
        assert box["s"].recorder.main_waiting.wait(_PATIENCE_S)
        # `message` 는 상주의 빚을 갚지 않는다(v10.145.1) — main 의 기다림은 회신(또는 폴백)까지 열린 채다
        return call("message", to="main", text="which file should I use?")

    def main_answers(msgs):
        s = box["s"]
        key = s.agent_keys()[0]
        until(lambda: s.agent_state(key) == "idle", "asker parked")
        # 회신의 창 줄(`agent_message` — 받는 쪽 키, main 스레드, 받은편지함에 넣은 뒤)에서 main 을 잡는다 — main 의
        # 다음 꼬리는 상주가 그 회신을 집은 뒤(바쁨)에 짓는다(`_held_request_to` 와 같은 규칙, v10.145.1)
        s.recorder.hold("agent_message", key, lambda: s.agent_state(key) == "busy")
        return call("reply", id=owed_id(msgs), text="TASK-C use a.py")

    def main_skips(msgs):
        s = box["s"]
        key = s.agent_keys()[0]
        until(lambda: s.agent_state(key) == "idle", "asker parked")
        # 폴백(main 의 런 요약)이 요청자에게 간다 — 그 런의 표지가 요약에 실린다
        return call(
            "complete", result="TASK-C no answer from main", answers=closing_ids(msgs)
        )

    main_script = [
        call("agent", mode="spawn", name="helper", task="TASK-C do the work"),
        _after_runs_began(box, 1, call("wait")),
    ]
    if main_replies:
        main_script += [
            main_answers,
            _after_agents_settle(box, complete_all("main finished")),
            # 상주의 과제 폴백이 연 main 의 둘째 런
            complete_all("read the fallback"),
        ]
    else:
        # `reply` 없는 `complete` 는 빚 독촉이 되돌린다 — 예산이 다할 때까지 같은 답(첫 `complete` + 되묻기 여덟),
        # 그리고 상주의 폴백이 연 main 의 둘째 런에서 한 번
        main_script += [main_skips] * (1 + NAG_BUDGET + 1)
    # 깬 런 — main 의 회신(또는 폴백)을 읽고 끝낸다. 과제 빚이 열린 채라 되묻기가 예산만큼 돌아오고 폴백이 닫는다.
    # main 이 회신하지 않는 길에선 main 의 런이 끝난 뒤에 끝낸다 — 상주의 폴백이 main 의 그 런에 흡수될지 새 런을
    # 열지가 갈리지 않게(늘 새 런)
    read_the_answer = (
        # main 의 회신으로 깬 런 — main 의 다음 꼬리(넷째 호출)가 이 런의 "바쁨" 을 본 뒤에 끝낸다
        _after_main_calls(box, 4, complete_all("read the answer"))
        if main_replies
        else lambda m: (
            until(lambda: runs_ended(box["s"]) >= 1, "main's run ended"),
            complete_all("read the answer")(m),
        )[1]
    )
    p = Provider(
        {
            "MAIN": main_script,
            "TASK-C": [
                agent_asks,
                # main 이 그 요청을 읽어 빚을 적은 뒤(셋째 호출) — 먼저 기다리면 근거가 없어 거절된다
                _after_main_calls(box, 3, lambda m: call("wait")),
                read_the_answer,
                *[complete_all("read the answer")] * NAG_BUDGET,
            ],
        }
    )
    s = box["s"] = Session(root, p, path="web")
    _wake_runs_after_idle(s, "helper")

    def done(s):
        until(lambda: runs_ended(s) >= 2, "main runs")
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
    """§4 회신·통지 — 줄 없음: 상주 → main 통지(`message` 회신 기대 없음)는 main 을 깨우되 장부 항목이 아니다, main
    → 상주 통지는 상주의 새 런을 열되 빚이 아니다. `[v10.145.1 에서 재기록 — 통지는 상주의 빚을 갚지 않는다: 상주의
    `complete` 는 되묻기 예산(8)을 다 쓰고 런 끝 폴백이 그 빚을 갚는다]`"""
    box: dict = {}
    p = Provider(
        {
            "MAIN": [
                call("agent", mode="spawn", name="helper", task="TASK-D build it"),
                _after_runs_began(box, 1, call("wait")),
                _after_agents_settle(box, lambda m: _notice_to_helper(box)),
                _after_agents_settle(box, complete_all("main finished")),
            ],
            "TASK-D": [
                _main_waits_then(
                    box,
                    lambda m: call(
                        "message", to="main", text="built it", expects_reply=False
                    ),
                ),
                # 통지로 깬 main 의 꼬리(셋째 호출)가 이 런의 "바쁨" 을 본 뒤에 끝낸다 — 빚이 열린 채라 되묻기가
                # 여덟 번 더 돌아오고(예산), 그 뒤 하니스가 폴백으로 닫는다
                _after_main_calls(box, 3, complete_all("ok")),
                *[complete_all("ok")] * NAG_BUDGET,
                # 통지를 보낸 main 의 꼬리(넷째 호출)가 그 런의 "바쁨" 을 본 뒤에 끝낸다
                _after_main_calls(box, 4, complete_all("noted")),
            ],
        }
    )
    s = box["s"] = Session(root, p)
    s.close_answered_awaits_before_main_reads()
    s.say("MAIN go")
    drive(s, path="run", until_done=None)
    return s.collect()


def _reply_then_settle(box, me: str, to: str, rid: str, text: str) -> str:
    """``me`` 가 ``to`` 에게 `reply` — 회신이 깨운 ``to`` 의 새 런은 ``me`` 가 런을 끝낸(한가한) 뒤에 열고, ``me`` 의
    다음 꼬리는 ``to`` 가 그 회신을 집은 뒤(바쁨)에 짓는다(`_held_request_to` 와 같은 규칙 — 안 잡으면 느린 러너에서
    그 꼬리가 "idle · 1 queued" 로 찍힌다: 컨테이너 cpus 0.5, 750회 중 2회)."""
    s = box["s"]
    a, b = _agent_key(s, me), _agent_key(s, to)
    s.recorder.hold("begin_agent_work", b, lambda: s.agent_state(a) == "idle")
    # 회신의 창 줄(`agent_message` — 받는 쪽 키, 보낸 쪽 스레드, 받은편지함에 넣은 뒤)에서 보낸 쪽을 잡는다
    s.recorder.hold("agent_message", b, lambda: s.agent_state(b) == "busy")
    return call("reply", id=rid, text=text)


def _notice_to_helper(box) -> str:
    """main → helper 통지 — 통지의 창 줄(`agent_message`)에서 main 을 helper 가 그것을 집을 때까지 잡는다."""
    s = box["s"]
    key = _agent_key(s, "helper")
    s.recorder.hold("agent_message", key, lambda: s.agent_state(key) == "busy")
    return call(
        "message", to="helper", text="TASK-D fyi: the build passed", expects_reply=False
    )


def s05_agent_to_agent(root: Path) -> dict:
    """§4 에이전트↔에이전트: 상주 X 가 상주 Y 에게 요청 → Y 가 `reply` → X 가 새 런으로 깨어 main 에게 회신."""
    box: dict = {}
    p = Provider(
        {
            "MAIN": [
                call("agent", mode="spawn", name="yy"),
                call("agent", mode="spawn", name="xx", task="TASK-E1 ask yy"),
                _after_runs_began(box, 1, call("wait")),
                _after_agents_settle(box, complete_all("main finished")),
            ],
            "TASK-E1": [
                _main_waits_then(
                    box,
                    lambda m: _held_peer_message(
                        box, "xx", "yy", "TASK-E2 compute 6*7"
                    ),
                ),
                _after_runs_began(box, 2, call("wait")),
                lambda m: call("reply", id=owed_ids(m)[0], text="yy says 42"),
                # 회신으로 깬 main 의 다음 호출(넷째)이 시작될 때까지 내려오지 않는다
                _after_main_calls(box, 4, lambda m: call("complete", result="ok")),
            ],
            "TASK-E2": [
                lambda m: _reply_then_settle(box, "yy", "xx", owed_id(m), "TASK-E1 42"),
                call("complete", result="ok"),
            ],
        }
    )
    s = box["s"] = Session(root, p)
    s.close_answered_awaits_before_main_reads()
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
                _after_runs_began(box, 1, call("wait")),
                _after_agents_settle(box, complete_all("main finished")),
            ],
            "TASK-G": [
                _main_waits_then(
                    box, lambda m: call("ask", question="TASK-G v1 or v2?")
                ),
                lambda m: call("reply", id=owed_id(m), text="asked the user"),
                # 회신으로 깬 main 의 다음 호출(셋째)이 시작될 때까지 내려오지 않는다
                _after_main_calls(box, 3, lambda m: call("complete", result="ok")),
                complete_all("went with v2"),
            ],
        }
    )
    s = box["s"] = Session(root, p, path="web")
    s.close_answered_awaits_before_main_reads()
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
        return _after_runs_began(box, 1, call("wait"))(msgs)

    def agent_first(msgs):
        assert second_sent.wait(_PATIENCE_S)
        assert box["s"].recorder.main_waiting.wait(_PATIENCE_S)
        return call("shell", command="true")

    def agent_reply_second(msgs):
        # main 의 **둘째** 제자리 대기(wait_in_place 셋째 호출 = 둘째 True) — 첫 대기의 낡은 표지를 보지 않는다
        until(
            lambda: (
                box["s"].recorder.counts.get("wait_in_place", 0) >= 3
                and box["s"].recorder.main_waiting.is_set()
            ),
            "main waits again",
        )
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
                # 둘째 회신을 읽은 main 의 꼬리(다섯째 호출)가 상주의 "바쁨" 을 본 뒤에 끝낸다
                _after_main_calls(box, 5, lambda m: call("complete", result="ok")),
            ],
        }
    )
    s = box["s"] = Session(root, p)
    s.close_answered_awaits_before_main_reads()
    # 상주의 첫 런은 main 의 셋째 호출이 시작된 뒤 — 그 꼬리가 줄 선 둘째 요청을 보고, 첫 턴 경계가 그것을 흡수한다
    s.recorder.hold("begin_agent_work", "*", lambda: len(p.calls.get("MAIN", [])) >= 3)
    s.say("MAIN go")
    drive(s, path="run", until_done=None)
    return s.collect()


def s09_stop_square(root: Path) -> dict:
    """§4 정지 ■: 런만 멈춘다 — 지금 일의 요청자에게 stopped 회신, 기다리던 main 이 깬다.

    정지 회신을 읽은 main 꼬리의 로스터는 지금 구현에서 `answer_debts_as_stopped`↔`_close_run` 틈 때문에
    [busy]/[idle] 둘 다 나올 수 있다 — 기대값은 정착된 쪽(멈춘 상주가 idle)을 강제해 기록한다(v10.144.1 에서 재기록)."""
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
                _after_runs_began(box, 1, call("wait")),
                _after_agents_settle(box, complete_all("main finished")),
            ],
            "TASK-J": [stop_me],
        }
    )
    s = box["s"] = Session(root, p)
    # 정지 회신을 보낸 상주가 런을 정리(idle)한 뒤 main 이 읽는다
    s.settle_repliers_before_main_reads()
    s.say("MAIN go")
    drive(s, path="run", until_done=None)
    return s.collect({"stop_run": box.get("stop")})


def s10_kill_x(root: Path) -> dict:
    """§4 정지 ✕: 지금 일은 stopped, 시작 안 한 대기 요청은 removed — 둘 다 main 메일함에 든 뒤 main 이 본다."""
    box: dict = {}

    def kill_me(msgs):
        s = box["s"]
        # main 이 둘째 요청을 보내고 다음 호출을 시작한 뒤(그 꼬리가 줄 선 요청을 본다)
        until(
            lambda: (
                len(s.reg.pending_mail()) == 0
                and len(s.provider.calls.get("MAIN", [])) >= 3
            ),
            "queued",
        )
        box["kill"] = s.reg.kill(s.agent_keys()[0])
        return call("shell", command="true")

    def main_queue_second(msgs):
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
                _after_runs_began(box, 1, call("wait")),
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
            SimpleNamespace(pending_count=s.pending_count),
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
                _after_runs_began(box, 1, call("wait")),
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
    s.dead_stopped_runs_before_main_reads()
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
        # run 에이전트의 첫 provider 호출이 시작된 뒤에 멈춘다 — 사람 말이 제자리 대기를 곧바로 깨우면(창구 하나)
        # main 이 둘째 대기에 먼저 들어, 멈춤이 그 호출보다 앞설 수 있다(컨테이너 cpus 0.5 50회 중 3회)
        until(lambda: len(p.calls.get("TASK-N", [])) >= 1, "agent's first call")
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
    않는다. ``query`` 쪽은 사람의 말(질의)이 메일보다 먼저 런을 열고, 그 첫 턴 경계가 메일을 배달한다.

    `[v10.146.6 뒤 재기록 — 메일 깨우기 항목이 없어져, 질의 런의 첫 경계에서 깨우기 문장이 사람 말풍선
    (`push_user_message`)으로 그려지던 한 줄이 사라졌다(s28 도 같다). 모델이 읽는 글은 같다]`

    `[v10.145.0 에서 재기록 — 2단계 예외 D1: 재시작 뒤 main 메일함의 요청을 보낸 쪽이 기다리는 것으로 선다. 번호는
    정규화의 첫 등장 순서 때문에 r-1↔r-2 가 뒤바뀐다]`"""
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
    s.stop_pump()
    t.join(10)
    worker, asker = s.agent_keys()
    box["worker"] = worker
    until(lambda: s.agent_state(worker) == "idle" and s.agent_state(asker) == "idle")
    # 펌프가 없는 동안: asker 가 main 에게 묻고(미배달 요청) 기다린다, worker 는 첫 일 도중 · 둘째는 줄에
    s.reg.request(asker, "TASK-P ask main about the branch")
    # `message` 는 asker 의 TASK-P 빚을 갚지 않는다(v10.145.1) — 펌프가 없어 main 이 집지 않은 요청은 `wait` 근거가
    # 아니므로 asker 의 런은 되묻기 끝에 폴백으로 끝난다. 그 폴백까지(메일 둘) 잡는다 — 하나에서 멈추면 폴백과 경주한다.
    until(
        lambda: len(s.reg.pending_mail()) == 2, "asker's request and its run's fallback"
    )
    until(lambda: s.agent_state(asker) == "idle")
    # worker 의 첫 일은 세션 종료가 멈춘다 — 그 런의 시작을 종료(로스터의 dead)까지 잡아, 모델 호출 전에 멈추게 한다
    s.recorder.hold("begin_agent_work", worker, lambda: s.agent_state(worker) == "dead")
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
        # 회신의 창 줄에서 main 을 asker 가 그것을 집을 때까지 잡는다 — main 의 다음 꼬리가 asker 의 "바쁨" 을 본다
        s2 = box2["s"]
        s2.recorder.hold(
            "agent_message", asker, lambda: s2.agent_state(asker) == "busy"
        )
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
            "TASK-P": [
                _after_main_calls(
                    box2,
                    2,
                    lambda m: call("complete", result="got the branch", answers=[]),
                )
            ],
        }
    )
    # 재시작 직후에는 main 과 되살린 에이전트가 아직 ctx 를 여는 에이전트를 `[starting]` 으로 볼 수 있다 — 사실이고
    # (`tests/test_agent_worker_end.py` 의 `TestStartingIsVisible`), 이 시나리오는 되살림이 가라앉은 뒤를 고정한다:
    # 되살린 worker 는 조립 도중에 끊긴 런을 다시 집어 곧바로 모델을 부르므로 그 런의 시작을 asker 가 idle 이 될 때까지
    # 잡고(`holds` — 조립 전에 건다), 사실은 asker 도 idle 이 된 뒤 뜬다(펌프는 그 뒤에 돈다).
    s2 = box2["s"] = Session(
        root,
        p2,
        path="run" if query else "web",
        resume=True,
        query="MAIN resumed query" if query else None,
        holds=[
            (
                "begin_agent_work",
                worker,
                lambda: "s" in box2 and box2["s"].agent_state(asker) == "idle",
            )
        ],
    )
    # 되살린 worker 는 끊긴 런을 다시 집어 대본에서 main 의 대기를 기다린다 — 그 상태에서 사실을 뜬다
    until(
        lambda: s2.agent_state(worker) == "busy" and s2.agent_state(asker) == "idle",
        "worker resumed, asker settled",
    )
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
        # 런 끝의 기록(`surface.run_ended`)은 펌프가 다시 기다림에 들어 한가함을 세우기 **전**이다 — 그 사이에
        # 통지가 오면 busy=True 로 찍힌다(컨테이너 cpus 0.5·부하 아래 1회, 2026-11-17). 한가함까지 잡는다.
        until(
            lambda: runs_ended(box["s"]) >= 1 and box["s"].main_idle(),
            "main run ended and idle",
        )
        return call("message", to="main", text="all done", expects_reply=False)

    p = Provider(
        {
            "MAIN": [
                call("agent", mode="spawn", name="helper"),
                lambda m: _held_request_to(
                    box, "helper", "TASK-R do it and tell me", expects_reply=False
                ),
                complete_all("handed off"),
                _after_agents_settle(box, complete_all("heard back")),
            ],
            "TASK-R": [notify_after_main_ended, complete_all("ok")],
        }
    )
    s = box["s"] = Session(root, p)
    # 통지가 연 메일 런의 꼬리는 통지를 보낸 상주가 런을 끝낸(한가한) 뒤의 것이다
    s.surface.before_run = lambda start: (
        until(lambda: s.agent_state(_agent_key(s, "helper")) == "idle", "helper done")
        if not start.author_is_user
        else None
    )
    s.say("MAIN go")
    drive(s, path="run", until_done=None)
    return s.collect()


# ── 1단계 기준 보강 (v10.144.1): 기대값 20개에 없던 지금 동작 ──────────────────
# 표지 ``[단계 N 에서 바뀜 — …]`` 은 그 단계의 "바뀌는 기대값 목록" 이다(one-actor-plan §2). 표지 없는
# 시나리오는 단일 구조의 끝까지 같아야 한다.


def _agent_key(s: Session, name: str) -> str:
    rows = [r for r in s.reg.roster_snapshot() if r.get("name") == name]
    if not rows:
        raise AssertionError(f"oracle: no agent named {name}")
    return rows[0]["key"]


def _roster(s: Session) -> list[dict]:
    return [
        {k: r.get(k) for k in ("key", "name", "state", "pending_requests", "handled")}
        for r in s.reg.roster_snapshot()
    ]


def s21_main_stop_owes_agent(root: Path) -> dict:
    """1단계 ① main 정지 회신 `[단계 4c 에서 바뀜 — 결정 2]`: 상주가 main 에게 회신 기대 `message` → main 이 그
    메일로 깬 런에서 사람이 ■ — 지금은 펌프의 런 끝(`main_run_ended`)이 stopped 를 넘기지 않아 빚진 상대(상주)에게
    라벨 붙은 런 요약 폴백이 간다. 상주는 그것을 회신으로 읽고 새 런을 연다. `[v10.145.1 에서 재기록 — 그 `message` 는
    상주의 과제 빚(TASK-S1)을 갚지 않는다: 깬 런의 `complete` 가 되묻기 예산을 다 쓰고 런 끝 폴백이 main 의 새 런을 연다]`"""
    box: dict = {}

    def agent_asks(msgs):
        assert box["s"].recorder.main_waiting.wait(_PATIENCE_S)
        # `message` 는 상주의 빚을 갚지 않는다(v10.145.1) — main 의 기다림은 회신(또는 폴백)까지 열린 채다
        return call("message", to="main", text="which file should I use?")

    def main_is_stopped(msgs):
        s = box["s"]
        until(lambda: s.agent_state(_agent_key(s, "helper")) == "idle", "asker parked")
        s.surface.stop.set()  # 사람 ■ — 이 호출이 돌아온 뒤 런이 멈춘다
        return call("shell", command="true")

    p = Provider(
        {
            "MAIN": [
                call("agent", mode="spawn", name="helper", task="TASK-S1 do the work"),
                _after_runs_began(box, 1, call("wait")),
                main_is_stopped,
                # 상주의 과제 폴백이 연 main 의 둘째 런
                complete_all("read the fallback"),
            ],
            "TASK-S1": [
                agent_asks,
                _after_main_calls(box, 3, lambda m: call("wait")),
                complete_all("read the answer"),
                *[complete_all("read the answer")] * NAG_BUDGET,
            ],
        }
    )
    s = box["s"] = Session(root, p, path="web")
    _wake_runs_after_idle(s, "helper")
    facts: dict = {}

    def done(s):
        until(
            lambda: runs_ended(s) >= 2, "main stopped, then read the asker's fallback"
        )
        key = _agent_key(s, "helper")
        until(
            lambda: (
                s.reg.roster_snapshot()[0]["handled"] >= 2
                and s.agent_state(key) == "idle"
            ),
            "asker read the fallback",
        )
        facts["awaited_helper"] = s.reg.awaited_replies(f"agent:{key}")
        facts["awaited_main"] = s.reg.awaited_replies("main")
        facts["queue_lines"] = s.reg.pending_mail()

    s.say("MAIN go", nickname="dj")
    drive(s, path="web", until_done=done)
    return s.collect(facts)


def _bb_dead_then(box, crash, step):
    """bb 를 죽이고(신호) 죽음이 확정된 **뒤** ``step`` — 그다음 판정이 죽은 bb 를 본다."""

    def go(msgs):
        s = box["s"]
        crash.set()
        until(lambda: s.agent_state(_agent_key(s, "bb")) == "dead", "bb died")
        return step if isinstance(step, str) else step(msgs)

    return go


def s22_agent_dies(root: Path, *, who: str) -> dict:
    """1단계 ② 사망 통지 — 워커가 런 시작에서 원인 미상으로 죽는다(`Recorder.crash_on_start`).

    - ``who="requested"`` (가) `[단계 4c 에서 바뀜 — 변화 5]`: 상주 aa 가 bb 에게 요청하고 `wait` → bb 사망. main
      은 died 통지를 받고, aa 는 bb 가 집어 든 그 요청의 died 회신(`· died` — "… died while it was working on your
      request …")을 받아 그 런에서 읽는다 `[v10.146.3 에서 바뀜 — 종전엔 aa 가 아무것도 받지 않았다]`.
    - ``who="requester"`` (나) `[단계 4c 에서 바뀜 — 변화 10]`: aa 가 bb 에게 요청한 뒤 aa 사망. bb 는 죽은 쪽에
      빚진 회신을 Open Items 에 든 채 일한다. 죽은 aa 가 main 에게 빚진 과제 요청에는 main 의 died 통지와 함께 died
      회신이 간다 `[v10.146.3 에서 바뀜]`.
      `[v10.145.2 에서 바뀜]` bb 의 `reply` 는 "not delivered — … was already gone" 을 실패로 듣고, 배달되지 않은 회신은
      발신 줄(conversation)에도 남지 않는다(종전엔 "delivered to whoever was waiting on it").
    - ``who="main_waits"`` (다) `[단계 4c 에서 바뀔 수 있음 — died 가 회신 모양이 되면]`: main 이 bb 에게 요청하고
      제자리 대기 중 bb 사망 — main 이 같은 런에서 깨어 died 통지를 읽고, 기다리던 회신이 닫히는가. `[v10.146.3 에서 바뀜]`
      bb 가 집어 든 그 요청에 died 회신이 와 main 의 기다리던 회신을 회신으로 닫는다. 죽음은 통지와 회신 두 통을
      잇달아 보낸다 — 둘 다 닿은 뒤에 main 이 기다리기 시작하게 강제한다(하나만 보고 깨면 회신이 런을 하나 더 연다)."""
    box: dict = {}
    facts: dict = {}
    crash = threading.Event()

    def arm(name: str):
        s = box["s"]
        s.recorder.crash_on_start[_agent_key(s, name)] = crash

    def snapshot(tag: str):
        s = box["s"]
        facts[tag] = {
            "roster": _roster(s),
            "queue_lines": s.reg.pending_mail(),
            "awaited_main": s.reg.awaited_replies("main"),
            **{
                f"awaited_{n}": s.reg.awaited_replies(f"agent:{_agent_key(s, n)}")
                for n in ("aa", "bb")
                if any(r.get("name") == n for r in s.reg.roster_snapshot())
            },
        }

    if who == "requested":
        scripts = {
            "MAIN": [
                call("agent", mode="spawn", name="bb"),
                lambda m: (
                    arm("bb"),
                    call("agent", mode="spawn", name="aa", task="TASK-D1 ask bb"),
                )[1],
                _after_runs_began(box, 1, call("wait")),
            ]
            + [complete_all("main read it")] * 10,
            "TASK-D1": [
                _main_waits_then(
                    box, lambda m: _held_request_to(box, "bb", "TASK-D2 compute")
                ),
                # main 이 died 통지를 읽고 되묻기를 다 받은 뒤(열두째 호출) — aa 는 그동안 바쁘다
                _bb_dead_then(
                    box, crash, _after_main_calls(box, 12, lambda m: call("wait"))
                ),
                # main 의 첫 런(died 통지로 깬 런)이 끝난 뒤 — 먼저 갚으면 그 런의 마지막 띠(`open_items`)에서 기다리는
                # 회신 r-2 가 빠진다(컨테이너 cpus 0.5, 40회 중 1회)
                lambda m: (
                    until(lambda: runs_ended(box["s"]) >= 1, "main's first run ends"),
                    call("reply", id=owed_id(m), text="aa: bb did not answer"),
                )[1],
                call("complete", result="ok"),
            ],
        }
    elif who == "requester":
        bb_go = threading.Event()
        box["bb_go"] = bb_go
        scripts = {
            "MAIN": [
                call("agent", mode="spawn", name="bb"),
                call("agent", mode="spawn", name="aa", task="TASK-E1 ask bb"),
                _after_runs_began(box, 1, call("wait")),
            ]
            + [complete_all("main read it")] * 10,
            "TASK-E1": [
                _main_waits_then(
                    box,
                    lambda m: _held_peer_message(box, "aa", "bb", "TASK-E2 slow job"),
                ),
                _after_runs_began(box, 2, call("wait")),
            ],
            "TASK-E2": [
                lambda m: (
                    bb_go.wait(_PATIENCE_S),
                    call("reply", id=owed_id(m), text="E2 done"),
                )[1],
                call("complete", result="ok"),
            ],
        }
    else:
        scripts = {
            "MAIN": [
                call("agent", mode="spawn", name="bb"),
                # 다음 꼬리는 bb 가 일을 집은 뒤(바쁨)에 짓는다 — 안 잡으면 느린 러너에서 "idle · 1 queued" (CI 1회)
                lambda m: (arm("bb"), _held_request_to(box, "bb", "TASK-F1 compute"))[
                    1
                ],
                _after_runs_began(box, 1, call("wait")),
            ]
            + [complete_all("main read it")] * 10,
        }
    p = Provider(scripts)
    s = box["s"] = Session(root, p, path="web")
    s.say("MAIN go", nickname="dj")
    if who == "main_waits":

        def die_as_main_waits(args, kwargs):
            # 사망은 main 이 제자리 대기를 시작할 때(그 `wait` 가 받아들여진 뒤 — 먼저 죽으면 "이미 도착" 으로 돌아간다).
            # 죽음의 두 통(died 통지·died 회신)이 다 닿을 때까지 main 을 잡는다 — main 이 둘 중 하나만 보고 깨는지가
            # 제품의 비결정이라, 둘 다 와 있는 쪽을 강제한다
            if args and args[0] is True:
                crash.set()
                until(lambda: not s.reg.awaited_replies("main"), "the died reply lands")

        s.recorder.rules["wait_in_place"] = die_as_main_waits

    def done(s):
        if who == "requester":
            until(lambda: len(s.agent_keys()) == 2, "spawned")
            aa = _agent_key(s, "aa")
            until(
                lambda: (
                    s.agent_state(aa) == "idle"
                    and s.agent_state(_agent_key(s, "bb")) == "busy"
                ),
                "aa parked",
            )
            # bb 의 첫 호출이 시작된 뒤(한가한 aa 를 본 꼬리) — 먼저 죽이면 bb 가 aa 의 한가함을 못 본다
            until(lambda: len(p.calls.get("TASK-E2", [])) >= 1, "bb's first call")
            s.recorder.crash_on_start[aa] = crash
            # 통지를 적은(`request` 의 대화 기록) 뒤에 죽는다 — 죽음의 died 회신 기록이 그 앞에 끼지 않게
            s.recorder.hold("begin_agent_work", aa, lambda: "notice" in facts)
            crash.set()
            facts["notice"] = s.reg.request(
                aa, "TASK-E1 fyi", author="main", expects_reply=False
            )
            until(lambda: s.agent_state(aa) == "dead", "aa died")
            # 제자리에서 기다리던 main 이 died 통지를 읽은 뒤
            until(lambda: not s.reg.pending_mail(), "main read the death")
            snapshot("after_death")
            box["bb_go"].set()
            until(lambda: s.agent_state(_agent_key(s, "bb")) == "idle", "bb done")
            until(lambda: runs_ended(s) >= 1, "main read the death")
        else:
            # requested 는 main 의 런 둘 — died 통지로 깬 런, aa 의 회신이 연 런 [v10.146.3 에서 바뀜 — 종전엔 하나]
            runs = 2 if who == "requested" else 1
            until(lambda: runs_ended(s) >= runs, "main runs")
        until(
            lambda: all(s.agent_state(k) in ("idle", "dead") for k in s.agent_keys()),
            "settle",
        )
        snapshot("end")

    drive(s, path="web", until_done=done)
    return s.collect(facts)


class _HeldTimer:
    """체크인 타이머 대역 — 걸리기만 하고 스스로 발화하지 않는다. 대본이 `fire_checkin` 을 직접 부른다
    (`threading.Timer` 는 가짜 시계로 돌릴 수 없다 — 레지스트리의 `checkin_timer_factory` 자리)."""

    def __init__(self, interval, fn, args=()):
        self.interval = interval
        self.daemon = False

    def start(self):
        pass

    def cancel(self):
        pass


def _agents_file_kinds(s: Session) -> list:
    """``agents.json`` 의 미배달 메일 종류만 — 바이트는 담지 않는다."""
    files = sorted(s.sdir.rglob("agents.json"))
    if not files:
        return []
    doc = json.loads(files[0].read_text(encoding="utf-8"))
    return [str(m.get("kind") or "") for m in doc.get("pending") or []]


def s23_checkin_main_in_place(root: Path) -> dict:
    """1단계 ③ 체크인: main 이 run 에이전트의 회신을 기다리는 제자리 대기 중 체크인이 깨운다 — 모델이 읽는 글,
    Open Items 꼬리(체크인은 항목이 아니다), 저장 안 됨. main 은 다시 `wait` 하고 회신에 깬다."""
    box: dict = {}
    go = threading.Event()
    facts: dict = {}
    p = Provider(
        {
            "MAIN": [
                call("agent", mode="run", task="TASK-G slow work"),
                _after_runs_began(box, 1, call("wait")),
                call("wait"),
                _after_agents_settle(
                    box, complete_all("main finished"), states=("dead",)
                ),
            ],
            "TASK-G": [
                lambda m: (
                    go.wait(_PATIENCE_S),
                    call("reply", id=owed_id(m), text="G done"),
                )[1],
                # 회신을 읽은 main 의 꼬리(넷째 호출)가 run 의 "바쁨" 을 본 뒤에 내려온다
                _after_main_calls(box, 4, lambda m: call("complete", result="ok")),
            ],
        }
    )
    s = box["s"] = Session(root, p, path="web")
    s.close_answered_awaits_before_main_reads()
    s.reg.checkin_timer_factory = _HeldTimer
    # run 의 런은 체크인이 나간 뒤에 첫 기록을 남긴다 — 그 앞이면 체크인 글에 "last step … ago" 가 붙는다
    fired = threading.Event()
    s.recorder.hold("begin_agent_work", "*", fired.is_set)

    def done(s):
        assert s.recorder.main_waiting.wait(_PATIENCE_S)
        # 대기 표시(`wait_in_place(True)`)는 체크인을 걸기(`arm_checkin`) 전이다 — 걸린 뒤에 쏜다(걸기 전이면
        # `fire_checkin` 이 빈 채로 돌아오고 체크인은 영영 오지 않는다, 컨테이너 cpus 0.5 300회 중 4회)
        until(lambda: "main" in s.reg._checkin_armed, "checkin armed")
        facts["checkin"] = s.reg.fire_checkin("main")
        assert facts["checkin"], "checkin fired before it was armed"
        fired.set()
        facts["persisted_kinds"] = _agents_file_kinds(s)
        until(
            lambda: (
                len(p.calls.get("MAIN", [])) >= 3 and s.recorder.main_waiting.is_set()
            ),
            "waits again",
        )
        go.set()
        until(lambda: runs_ended(s) >= 1, "main finished")

    s.say("MAIN go", nickname="dj")
    drive(s, path="web", until_done=done)
    return s.collect(facts)


def s24_commands_while_waiting(root: Path) -> dict:
    """1단계 ⑥ 기다리는 중 명령 (web).

    - 다리 1 `[단계 4b 에서 바뀜 — 명령 함수 하나]`: main 이 run 에이전트의 회신을 제자리에서 기다리는 중 ``@…``·
      ``/…`` 명령은 그 자리에서 라우팅되고 깨우지 않으며 말풍선 에코(`push_user_message`)가 없다. 일반 입력은
      깨운다(에코는 다음 턴 경계의 흡수가 낸다). 다시 기다리다 ■.
    - 다리 2 `[단계 5 에서 바뀜 — 결정 1]`: main 이 작업만 기다려 런이 끝난 상태(`wait` 가 런을 끝냄)에서 명령은
      런을 열지 않고, 일반 입력은 새 런을 연다."""
    box: dict = {}
    go = threading.Event()
    facts: dict = {}
    p = Provider(
        {
            "MAIN": [
                call("agent", mode="run", task="TASK-H slow work"),
                _after_runs_began(box, 1, call("wait")),
                call("wait"),
                # 메일 런(■ 뒤 run 에이전트의 회신)
                _after_agents_settle(
                    box, complete_all("read the late reply"), states=("dead",)
                ),
                # 다리 2: 작업만 기다린다 — 런이 끝난다(작업은 대본이 띄웠다: 도구 결과의 pid 는 비결정이다)
                call("wait"),
                # 다리 2 의 일반 입력 런
                complete_all("plain input read"),
            ],
            "TASK-H": [
                lambda m: (
                    go.wait(_PATIENCE_S),
                    call("reply", id=owed_id(m), text="H done"),
                )[1],
                call("complete", result="ok"),
            ],
        }
    )
    s = box["s"] = Session(root, p, path="web")
    s.reg.checkin_timer_factory = _HeldTimer
    # ■ 뒤 회신이 연 메일 런의 꼬리는 회신한 run 이 내려온 뒤의 것이다
    s.surface.before_run = lambda start: (
        until(
            lambda: all(s.agent_state(k) == "dead" for k in s.agent_keys()),
            "run came down",
        )
        if not start.author_is_user
        else None
    )
    s.surface.commands["@helper note"] = lambda: None
    s.surface.commands["/noop"] = lambda: None

    def routed(s) -> int:
        return sum(1 for r in s.recorder.lanes.get("pump", []) if r[0] == "routed")

    def done(s):
        assert s.recorder.main_waiting.wait(_PATIENCE_S)
        s.say("@helper note", nickname="dj")
        until(lambda: routed(s) == 1, "first command")
        s.say("/noop", nickname="dj")
        until(lambda: routed(s) == 2, "second command")
        facts["leg1_calls_after_commands"] = len(p.calls.get("MAIN", []))
        s.say("MAIN also check x", nickname="dj")
        until(
            lambda: (
                len(p.calls.get("MAIN", [])) >= 3 and s.recorder.main_waiting.is_set()
            ),
            "waits again",
        )
        s.surface.stop.set()  # ■ — 제자리 대기 중
        until(lambda: runs_ended(s) >= 1, "main stopped")
        go.set()
        until(lambda: runs_ended(s) >= 2, "mail run")
        # 다리 2
        facts["job"] = s.stores.jobs.start("sleep 30", owner="main").id
        s.say("MAIN wait for the job", nickname="dj")
        until(lambda: runs_ended(s) >= 3, "job wait ended the run")
        s.say("@helper note", nickname="dj")
        until(lambda: routed(s) == 3, "idle command")
        s.say("/noop", nickname="dj")
        until(lambda: routed(s) == 4, "idle command 2")
        facts["leg2_runs_after_commands"] = runs_ended(s)
        s.say("MAIN plain words", nickname="dj")
        until(lambda: runs_ended(s) >= 4, "plain input run")

    s.say("MAIN go", nickname="dj")
    drive(s, path="web", until_done=done)
    return s.collect(facts)


def s25_run_agent_leaves_queue(root: Path) -> dict:
    """1단계 ⑦ `[단계 4c 에서 바뀜 — 변화 9]`: run 에이전트가 마지막 응답(`complete`)을 만드는 동안 main 이 그에게
    회신 기대 요청을 보낸다 — 줄 선 채로 run 이 내려온다. 보낸 쪽(main)의 기다리는 회신과 `wait` 판정(받아들여
    막히면 ■ 로 끝낸다 — 막힘은 대기 표시로 판정). v10.146.1 부터 내려오는 run 이 그 요청에 `· ended` 회신을
    보내고 줄을 닫는다 — main 은 그 회신으로 깨어 읽고 끝난다(종전엔 `wait` 가 "has your request queued" 를 근거로
    받아들였다)."""
    box: dict = {}
    facts: dict = {}

    def main_sends_second(msgs):
        until(lambda: len(p.calls.get("TASK-Q1", [])) >= 2, "run agent's last call")
        return call("message", to=box["s"].agent_keys()[0], text="TASK-Q2 also this")

    def main_waits_after_come_down(msgs):
        s = box["s"]
        until(lambda: s.agent_state(s.agent_keys()[0]) == "dead", "run came down")
        facts["awaited_main_before_wait"] = s.reg.awaited_replies("main")
        facts["roster_before_wait"] = _roster(s)
        return call("wait")

    p = Provider(
        {
            "MAIN": [
                call("agent", mode="run", task="TASK-Q1 work"),
                main_sends_second,
                main_waits_after_come_down,
            ]
            + [complete_all("main finished")] * 10,
            "TASK-Q1": [
                # main 의 둘째 호출이 시작된(그 꼬리를 지은) 뒤에 회신한다 — 먼저 오면 그 턴 경계가 회신을 흡수한다
                # (컨테이너 cpus 0.25 부하 아래 1회)
                _after_main_calls(
                    box, 2, lambda m: call("reply", id=owed_id(m), text="Q1 done")
                ),
                _after_main_calls(box, 3, lambda m: call("complete", result="ok")),
            ],
        }
    )
    s = box["s"] = Session(root, p, path="web")
    s.reg.checkin_timer_factory = _HeldTimer

    def done(s):
        until(lambda: len(p.calls.get("MAIN", [])) >= 3, "main's wait call")
        until(
            lambda: (
                s.recorder.main_waiting.is_set()
                or runs_ended(s) >= 1
                or len(p.calls.get("MAIN", [])) >= 4
            ),
            "wait judged",
        )
        facts["wait_blocked"] = s.recorder.main_waiting.is_set()
        if facts["wait_blocked"]:
            s.surface.stop.set()
        until(lambda: runs_ended(s) >= 1, "main run ended")
        facts["awaited_main_end"] = s.reg.awaited_replies("main")
        facts["queue_lines"] = s.reg.pending_mail()

    s.say("MAIN go", nickname="dj")
    drive(s, path="web", until_done=done)
    return s.collect(facts)


def s26_auditor_range(root: Path) -> dict:
    """1단계 ⑧ 감사 구간: 사람 런이 `complete` 로 끝나면 감사 작업 하나 → 그 결과는 main 앞 통지 → main 의 메일 런.
    감사가 도는 동안 main 의 `wait` 는 감사를 근거로 삼지 않는다(거절)."""
    box: dict = {}
    go = threading.Event()
    facts: dict = {}
    p = Provider(
        {
            "MAIN": [
                complete_all("answer one"),
                call("wait"),
                complete_all("answer two"),
                complete_all("read the audit"),
            ],
            "Audit main's run": [
                lambda m: (
                    go.wait(_PATIENCE_S),
                    call("complete", result="audit: run one is fine"),
                )[1],
            ],
        }
    )
    s = box["s"] = Session(root, p, path="web")
    auditor = s.owners.auditor
    auditor.enabled = True
    # run 의 수명 판정 — 큐·에이전트가 조용할 때 끝나지 않는 이유는 진행 중인 감사뿐이다
    quiet = QuietPolicy(
        main_input=s.main_input,
        agent_registry=s.reg,
        monitors=s.stores.monitors,
        schedules=s.stores.schedules,
        harness=auditor,
        jobs=s.stores.jobs,
    )

    def done(s):
        until(lambda: runs_ended(s) >= 1, "first run")
        until(lambda: len(p.calls.get("Audit main's run", [])) >= 1, "audit started")
        facts["run_would_stop_while_auditing"] = quiet.should_stop()
        auditor.enabled = False  # 둘째 런은 감사하지 않는다 — 결과 한 통만
        s.say("MAIN second", nickname="dj")
        until(lambda: runs_ended(s) >= 2, "second run")
        go.set()
        until(lambda: runs_ended(s) >= 3, "audit mail run")
        until(quiet.should_stop, "audit finished")

    s.say("MAIN first", nickname="dj")
    drive(s, path="web", until_done=done)
    return s.collect(facts)


def s27_restart_after_wait(root: Path) -> dict:
    """1단계 추론 A `[단계 5 에서 바뀜 — 결정 1·집어 든 봉투 저장]`: 상주 aa 가 main 의 요청을 받아 bb 에게 묻고
    `wait` 로 런을 끝낸 채(빚은 메모리) 세션이 끝난다 → 새 조립이 복원 — aa 의 빚·main 의 기다리는 회신·bb 의 끊긴
    런이 어떻게 되살아나는가. 2~4b 는 저장 모양이 바뀌어도 이 기대값이 같아야 한다."""
    facts: dict = {}
    box: dict = {}
    p1 = Provider(
        {
            "MAIN": [
                call("agent", mode="spawn", name="aa"),
                call("agent", mode="spawn", name="bb"),
                complete_all("spawned"),
            ],
            "TASK-W1": [
                lambda m: _held_peer_message(box, "aa", "bb", "TASK-W2 compute 6*7"),
                _after_runs_began(box, 2, call("wait")),
            ],
            "TASK-W2": [
                lambda m: (
                    until(
                        lambda: box["s"].agent_state(box["bb"]) == "dead", "session end"
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
    s.stop_pump()
    t.join(10)
    aa, bb = _agent_key(s, "aa"), _agent_key(s, "bb")
    box["bb"] = bb
    until(lambda: s.agent_state(aa) == "idle" and s.agent_state(bb) == "idle")
    s.reg.request(aa, "TASK-W1 ask bb, then tell me")
    until(lambda: s.agent_state(bb) == "busy", "bb picked aa's request")
    # bb 의 모델 호출이 시작된 뒤 — 호출 전에 세션이 끝나면 대본의 호출 기록이 흔들린다
    until(lambda: len(p1.calls.get("TASK-W2", [])) >= 1, "bb's call started")
    until(lambda: s.agent_state(aa) == "idle", "aa's wait ended its run")
    facts["before"] = {
        "roster": _roster(s),
        "queue_lines": s.reg.pending_mail(),
        "awaited_aa": s.reg.awaited_replies(f"agent:{aa}"),
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

    box2: dict = {}
    # 둘째 세션의 대본 — 되살린 쪽이 다시 일하면 이 대본을 읽는다(지금은 아무도 다시 일하지 않는다)
    p2 = Provider(
        {
            "MAIN": [complete_all("main mail run")] * 3,
            "TASK-W2": [lambda m: call("reply", id=owed_id(m), text="42")],
            "TASK-W1": [complete_all("aa woke after restart")] * 3,
        }
    )
    s2 = box2["s"] = Session(root, p2, path="web", resume=True)
    # 되살림이 가라앉은 뒤 — 끊긴 런을 다시 집는 에이전트가 없으면 아무도 움직이지 않는다
    until(
        lambda: all(s2.agent_state(k) in ("idle", "dead") for k in s2.agent_keys()),
        "restore settles",
    )
    facts["after_restore"] = {
        "revived": s2.wired.revived,
        "roster": _roster(s2),
        "queue_lines": s2.reg.pending_mail(),
        "awaited_aa": s2.reg.awaited_replies(f"agent:{aa}"),
        "awaited_bb": s2.reg.awaited_replies(f"agent:{bb}"),
        "awaited_main": s2.reg.awaited_replies("main"),
        "main_runs": runs_ended(s2),
    }

    def done(s):
        pass

    drive(s2, path="web", until_done=done)
    second = s2.collect()
    return {"first": first, "second": second, "facts": facts}


# ── 사람의 입력과 메일이 함께 main 앞에 설 때 (입력 큐를 main 의 상자로 옮기기 전의 기준) ─────────────


def s28_words_and_mail_after_the_last_boundary(root: Path, path: str) -> dict:
    """main 이 마지막 응답(`complete`)을 만드는 동안 작업 통지 한 통과 사람의 입력 한 줄이 함께 온다 — 그 런은
    끝나고, 다음 런은 **사람의 입력**이 연다. 그 런의 첫 턴 경계가 사람 말 뒤에 메일을 배달한다.

    `[v10.146.6 뒤 재기록 — 그 경계에서 메일 깨우기 문장이 사람 말풍선(`push_user_message`)으로 그려지던 한 줄이
    사라졌다]`"""
    box: dict = {}

    def mail_and_words_then_complete(msgs):
        s = box["s"]
        box["job"] = _job_mail(s, 1)
        s.say("MAIN one more thing", nickname="dj")
        return complete_all("first answer")(msgs)

    p = Provider(
        {
            "MAIN": [mail_and_words_then_complete, complete_all("saw both")],
        }
    )
    s = box["s"] = Session(root, p, path=path)
    s.say("MAIN go", nickname="dj")
    drive(s, path=path, until_done=lambda s: until(lambda: runs_ended(s) >= 2))
    return s.collect({"job": box.get("job")})


def _waiting_on_a_run_agent(box, tag: str, before_wait, *, then_waits: bool = True):
    """main 이 run 에이전트(``tag`` 과제)를 띄우고 그 회신을 제자리에서 기다리는 대본 — ``before_wait(s)`` 는 첫
    `wait` 를 내기 **직전**(main 의 스레드, 응답을 만드는 자리)에 부른다. 깬 뒤 main 은 다시 `wait` 하고, 회신에
    깨어 끝낸다. 회신은 ``box["go"]`` 가 세워진 뒤에 나간다."""
    go = box["go"] = threading.Event()

    def first_wait(msgs):
        before_wait(box["s"])
        return call("wait")

    main = [
        call("agent", mode="run", task=f"{tag} slow work"),
        _after_runs_began(box, 1, first_wait),
        call("wait"),
        _after_agents_settle(box, complete_all("main finished"), states=("dead",)),
    ]
    agent = [
        lambda m: (go.wait(_PATIENCE_S), call("reply", id=owed_id(m), text="done"))[1],
        # 회신을 읽은 main 의 꼬리(넷째 호출)가 run 의 "바쁨" 을 본 뒤에 내려온다
        _after_main_calls(box, 4, lambda m: call("complete", result="ok")),
    ]
    return main, agent


def _second_wait_then_reply(box, p, s) -> None:
    """main 이 다시 제자리 대기에 든 뒤(셋째 호출이 `wait` 를 냈고 대기 표시가 섰다) 회신을 내보내고 끝을 기다린다."""
    until(
        lambda: len(p.calls.get("MAIN", [])) >= 3 and s.recorder.main_waiting.is_set(),
        "waits again",
    )
    box["go"].set()
    until(lambda: runs_ended(s) >= 1, "main finished")


def s29_words_already_there_when_main_waits(root: Path) -> dict:
    """사람의 입력이 이미 줄 서 있는데 main 이 `wait` 한다 — 사람 말은 "이미 도착한 통지" 로 세지 않으므로 `wait`
    는 받아들여지고, 곧바로 "the user wrote to you" 로 깬다. 그 말은 다음 턴 경계에 주입된다 (web)."""
    box: dict = {}
    main, agent = _waiting_on_a_run_agent(
        box, "TASK-S1", lambda s: s.say("MAIN also look at y", nickname="dj")
    )
    p = Provider({"MAIN": main, "TASK-S1": agent})
    s = box["s"] = Session(root, p, path="web")
    s.close_answered_awaits_before_main_reads()
    s.reg.checkin_timer_factory = _HeldTimer  # 5분 체크인이 저절로 나가지 않게
    s.say("MAIN go", nickname="dj")
    drive(s, path="web", until_done=lambda s: _second_wait_then_reply(box, p, s))
    return s.collect()


def s30_slash_words_that_are_not_a_command_while_waiting(root: Path) -> dict:
    """제자리 대기 중 맨 앞의 사람 입력이 ``/`` 로 시작하지만 명령이 아니다(라우팅이 받지 않는다) — 그 말은 맨
    앞으로 되돌아가고 기다림이 깬다. 뒤에 선 말과 함께, 선 순서 그대로 다음 턴 경계에 주입된다 (web)."""
    box: dict = {}

    def two_lines(s):
        s.say("/tmp/notes.txt please read it", nickname="dj")
        s.say("MAIN and then summarize", nickname="dj")

    main, agent = _waiting_on_a_run_agent(box, "TASK-S2", two_lines)
    p = Provider({"MAIN": main, "TASK-S2": agent})
    s = box["s"] = Session(root, p, path="web")
    s.close_answered_awaits_before_main_reads()
    s.reg.checkin_timer_factory = _HeldTimer  # 5분 체크인이 저절로 나가지 않게
    s.say("MAIN go", nickname="dj")
    drive(s, path="web", until_done=lambda s: _second_wait_then_reply(box, p, s))
    return s.collect()


def s31_schedule_fires_while_main_waits(root: Path) -> dict:
    """main 이 제자리에서 기다리는 동안 예약이 발화한다 — 발화는 사람 요청과 같은 길이라 기다림이 "the user wrote to
    you" 로 깨고, 그 프롬프트는 예약의 이름으로 다음 턴 경계에 주입된다 (web). 지금 동작 그대로 — 예약의 발화도
    "사용자가 썼다" 고 말한다."""
    box: dict = {}
    facts: dict = {}
    main, agent = _waiting_on_a_run_agent(box, "TASK-S3", lambda s: None)
    p = Provider({"MAIN": main, "TASK-S3": agent})
    s = box["s"] = Session(root, p, path="web")
    s.close_answered_awaits_before_main_reads()
    s.reg.checkin_timer_factory = _HeldTimer  # 5분 체크인이 저절로 나가지 않게
    # 1년에 한 번 — 시나리오 동안 스스로 발화하지 않는다. 이름이 발화의 작성자(`Scheduler-nightly`)가 된다
    sched = s.stores.schedules.add(
        "0 0 1 1 *", "MAIN scheduled check", label="nightly", source="user"
    )

    def done(s):
        assert s.recorder.main_waiting.wait(_PATIENCE_S)
        facts["fired"] = s.stores.schedules.run_now(sched.id)
        _second_wait_then_reply(box, p, s)

    s.say("MAIN go", nickname="dj")
    drive(s, path="web", until_done=done)
    return s.collect(facts)


# ── 메일 런이 열리는 모양 (런을 봉투로 열기 전의 기준) ─────────────


def s32_quiet_reply_then_waking_mail(root: Path) -> dict:
    """ "Stop everything" 의 깨우지 않는 정지 회신이 한가한 main 앞에 먼저 서고, 그 뒤 작업 통지(깨움)가 온다 — 메일
    런이 열리고 그 첫 턴 경계가 둘을 **도착 순서**로 읽는다 (web). s11 과 같은 정지, 다음 런을 사람 말 대신 메일이 연다."""
    box: dict = {}
    facts: dict = {}

    def stop_everything(msgs):
        s = box["s"]
        assert s.recorder.main_waiting.wait(_PATIENCE_S)
        s.surface.stop.set()  # main 먼저 (서버·Ctrl+C 와 같은 순서)
        box["stopped"] = s.reg.stop_all_runs(wakes_requesters=False)
        return call("shell", command="true")

    p = Provider(
        {
            "MAIN": [
                call("agent", mode="run", task="TASK-T slow work"),
                _after_runs_began(box, 1, call("wait")),
                complete_all("read both"),
            ],
            "TASK-T": [stop_everything],
        }
    )
    s = box["s"] = Session(root, p, path="web")
    s.reg.checkin_timer_factory = _HeldTimer  # 5분 체크인이 저절로 나가지 않게

    def done(s):
        until(lambda: runs_ended(s) >= 1)
        key = s.agent_keys()[0]
        until(lambda: s.agent_state(key) == "dead", "run came down")
        until(lambda: len(s.reg.pending_mail()) == 1, "quiet reply")
        facts["job"] = _job_mail(s, 1)
        until(lambda: runs_ended(s) >= 2, "mail run")

    s.say("MAIN go", nickname="dj")
    drive(s, path="web", until_done=done)
    facts["stopped"] = len(box.get("stopped") or [])
    return s.collect(facts)


def s33_words_right_after_a_mail_run_opens(root: Path) -> dict:
    """메일 런이 열린 직후(런을 열기 직전의 표면 알림에서), 그 런의 첫 턴 경계 전에 사람 말이 온다 — 지금은 그 경계가
    사람 말 → 메일 순서로 넣는다 (web)."""
    box: dict = {}
    facts: dict = {}
    p = Provider({"MAIN": [complete_all("first answer"), complete_all("saw both")]})
    s = box["s"] = Session(root, p, path="web")
    s.surface.before_run = lambda start: (
        s.say("MAIN while it opens", nickname="dj")
        if not start.author_is_user
        else None
    )

    def done(s):
        until(lambda: runs_ended(s) >= 1 and s.main_idle(), "main idle")
        facts["job"] = _job_mail(s, 1)
        until(lambda: runs_ended(s) >= 2, "mail run")

    s.say("MAIN go", nickname="dj")
    drive(s, path="web", until_done=done)
    return s.collect(facts)


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
    # 1단계 기준 보강 (v10.144.1)
    "s21_main_stop_owes_agent": s21_main_stop_owes_agent,
    "s22_dies_requested": lambda root: s22_agent_dies(root, who="requested"),
    "s22_dies_requester": lambda root: s22_agent_dies(root, who="requester"),
    "s22_dies_main_waits": lambda root: s22_agent_dies(root, who="main_waits"),
    "s23_checkin_main_in_place": s23_checkin_main_in_place,
    "s24_commands_while_waiting": s24_commands_while_waiting,
    "s25_run_agent_leaves_queue": s25_run_agent_leaves_queue,
    "s26_auditor_range": s26_auditor_range,
    "s27_restart_after_wait": s27_restart_after_wait,
    # 사람의 입력과 메일이 함께 main 앞에 설 때
    "s28_words_and_mail_late_run": lambda root: (
        s28_words_and_mail_after_the_last_boundary(root, "run")
    ),
    "s28_words_and_mail_late_web": lambda root: (
        s28_words_and_mail_after_the_last_boundary(root, "web")
    ),
    "s29_words_before_wait": s29_words_already_there_when_main_waits,
    "s30_slash_words_while_waiting": s30_slash_words_that_are_not_a_command_while_waiting,
    "s31_schedule_while_waiting": s31_schedule_fires_while_main_waits,
    # 메일 런이 열리는 모양
    "s32_quiet_reply_then_waking_mail": s32_quiet_reply_then_waking_mail,
    "s33_words_right_after_mail_run_opens": s33_words_right_after_a_mail_run_opens,
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
