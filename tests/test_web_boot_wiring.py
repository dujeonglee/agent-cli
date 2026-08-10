"""``agent-cli web`` 부트 배선 — 옵션이 실제 객체에 가 닿는가 (A1/M2/관전).

``test_web_command.py`` 는 옵션 **게이트**(거부·기본값 해석)를 무거운 배선
**이전**에서 끊어 잰다. 그 방식으로는 게이트를 통과한 값이 그 뒤에 무엇에
연결되는지를 볼 수 없고, 실제로 부품 테스트가 전부 green 인 채 조립만
어긋나는 사고가 이 프로젝트에서 이미 한 층 아래(효과 락 ↔ ``_invoke_regular``,
``test_effect_lock.py::TestLoopIntegration``)에서 확인됐다.

여기서는 ``web()`` 을 **끝까지 부팅**시킨 뒤(uvicorn 이 서브만 하지 않도록
가로챈다) 살아 있는 ``WebServer``/``WebRenderer``/``TurnRegistry`` 를 그대로
들여다본다. 잡는 것:

  - 병렬 계약의 디스패처 배선 — 레지스트리 생성 여부·cap·게이트·``/api/stop``
    훅·``server.turn_registry``·계측 프로바이더·on_change → 렌더러
  - 큐에서 꺼낸 메시지가 **직렬 실행이 아니라 턴 제출**로 가는가
  - 실 ``run_loop`` 이 그 턴의 ``origin_turn`` 으로 도는가
  - 종료 시 레지스트리가 수거되는가
  - ``--turn-metrics`` 가 세션 디렉토리에 붙는가
  - 관전 토큰 해석(``--spectators`` / ``--view-token``)과 동일 토큰 거부

직렬 계약은 **반대 방향으로도** 고정한다: 레지스트리가 생성조차 되지 않고
관전 자격도 존재하지 않는 것이 "기존 동작 보존"의 관측 가능한 정의다.
"""

from __future__ import annotations

import json
import threading
import time
from contextlib import contextmanager

import pytest

from agent_cli import turn_metrics
from agent_cli.providers.base import LLMResponse
from agent_cli.tools import effect_lock

#: ``turn_metrics`` 가 쓰는 ``event`` 값들 — ``turns.jsonl`` 을 함께 쓰는
#: 복구 관측(TurnRecorder) 행과 구별하는 키다(그 행에는 ``event`` 가 없다).
_METRIC_EVENTS = frozenset({"turn", "lock", "compact", "ctx", "reject", "llm_call"})


class _Caps:
    """ModelCapabilities 대역 — 부트가 읽는 필드만."""

    context_window = 32768
    max_output_tokens = 4096
    supports_thinking = False
    thinking_budget = 0


class _FakeProvider:
    """항상 ``complete`` 로 답하는 provider — 턴이 한 번의 콜로 끝난다.

    ``system`` 을 기록하는 이유: 턴 스코핑 섹션이 프롬프트까지 갔는지는
    렌더러 내부가 아니라 **모델이 실제로 받은 것**으로 재는 것이 맞다.
    ``gate`` 를 걸면 콜 안에서 멈춰 턴을 inflight 로 붙들어 둘 수 있다.
    """

    def __init__(self):
        self.threads: list[str] = []
        self.systems: list[str] = []
        self.gate: threading.Event | None = None
        self.entered = threading.Event()
        self._lock = threading.Lock()

    def call(self, messages=None, **kwargs):
        with self._lock:
            self.threads.append(threading.current_thread().name)
            self.systems.append(str(kwargs.get("system", "")))
        self.entered.set()
        if self.gate is not None:
            self.gate.wait(timeout=15)
        return LLMResponse(content=json.dumps({"action": "complete", "result": "ok"}))


class _Booted:
    """부팅된 세션의 살아 있는 객체들."""

    def __init__(self):
        self.server = None
        self.provider = _FakeProvider()
        self.registries: list = []  # 생성된 TurnRegistry (직렬이면 비어 있다)
        self.kwargs: list[dict] = []  # 그 생성 kwargs
        self.result = None  # 조기 종료 시의 CliRunner 결과
        self.exc: BaseException | None = None

    @property
    def registry(self):
        return self.registries[0] if self.registries else None

    @property
    def renderer(self):
        return self.server.renderer if self.server else None

    def wait_for(self, predicate, timeout=10.0, what="condition"):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.01)
        raise AssertionError(f"시간 내에 성립하지 않음: {what}")


@pytest.fixture(autouse=True)
def _isolate_process_globals():
    """계측·효과 락·전역 렌더러는 프로세스 전역 — 부팅이 건드리므로 격리."""
    from agent_cli.render import get_renderer, set_renderer

    previous = get_renderer()
    turn_metrics.disable()
    effect_lock.reset()
    yield
    turn_metrics.disable()
    effect_lock.reset()
    set_renderer(previous)


@contextmanager
def _boot(tmp_path, monkeypatch, *args):
    """``agent-cli web <args>`` 를 uvicorn 서브 직전까지 부팅한다.

    ``uvicorn.Server.run`` 을 가로채 거기서 멈춰 두므로, 워커 스레드는 이미
    자기 배선을 마치고 큐에서 대기 중이다 — 그 상태의 객체를 그대로 넘긴다.
    블록을 빠져나가면 **실제 종료 경로**(``server.shutdown()`` → SHUTDOWN
    센티널 → 워커 정리)를 그대로 태운다: 종료 배선도 같은 하네스로 잰다.

    옵션 게이트에서 조기 종료한 경우 ``run`` 이 불리지 않으므로, 그때는
    ``booted.result`` 만 채워진 채(서버 None) 넘어온다.
    """
    import uvicorn
    from typer.testing import CliRunner

    import agent_cli.loop.turns as turns_mod
    import agent_cli.main as main_mod
    import agent_cli.web.server as server_mod

    booted = _Booted()
    serving = threading.Event()
    release = threading.Event()

    monkeypatch.setattr(
        main_mod,
        "_setup_provider",
        lambda *a, **k: (booted.provider, _Caps(), "m1", "http://u", "k", "openai"),
    )

    real_create_app = server_mod.create_app

    def spy_create_app(server):
        booted.server = server
        return real_create_app(server)

    monkeypatch.setattr(server_mod, "create_app", spy_create_app)

    real_registry_cls = turns_mod.TurnRegistry

    class _RecordingRegistry(real_registry_cls):
        """생성 인자·submit·shutdown 을 기록하되 동작은 진짜 그대로."""

        def __init__(self, runner, **kw):
            booted.kwargs.append(dict(kw))
            super().__init__(runner, **kw)
            self.submitted: list[dict] = []
            self.shutdown_calls = 0
            booted.registries.append(self)

        def submit(self, text, **kw):
            self.submitted.append({"text": text, **kw})
            return super().submit(text, **kw)

        def shutdown(self, **kw):
            self.shutdown_calls += 1
            return super().shutdown(**kw)

    monkeypatch.setattr(turns_mod, "TurnRegistry", _RecordingRegistry)

    def fake_run(_self):
        serving.set()
        release.wait(timeout=30)

    monkeypatch.setattr(uvicorn.Server, "run", fake_run)
    monkeypatch.chdir(tmp_path)

    def _invoke():
        try:
            booted.result = CliRunner().invoke(
                main_mod.app,
                ["web", "--no-browser", "--host", "127.0.0.1", "--port", "0", *args],
                catch_exceptions=False,
            )
        except BaseException as exc:  # pragma: no cover - 진단용
            booted.exc = exc
        finally:
            serving.set()  # 조기 종료도 대기자를 풀어 준다

    cli = threading.Thread(target=_invoke, name="web-boot", daemon=True)
    cli.start()
    assert serving.wait(timeout=30), "부팅이 uvicorn 지점까지 오지 않았다"
    try:
        yield booted
    finally:
        release.set()
        cli.join(timeout=30)
        if booted.exc is not None:  # pragma: no cover - 진단용
            raise booted.exc


def _boot_serial_ready(booted):
    """직렬 계약에서 워커가 큐 대기까지 갔음을 확인 (레지스트리가 없으므로
    ``server.agent_registry`` 배선이 워커 진입의 관측 지점이다)."""
    booted.wait_for(
        lambda: booted.server.agent_registry is not None,
        what="워커 스레드 부팅",
    )


class TestSerialContractBuildsNothing:
    """직렬은 오늘 동작 그대로 — 병렬 기계가 **생성조차 되지 않는다**."""

    def test_no_registry_is_built(self, tmp_path, monkeypatch):
        with _boot(tmp_path, monkeypatch) as booted:
            _boot_serial_ready(booted)
            assert booted.registries == [], "직렬인데 TurnRegistry 가 생겼다"
            assert booted.server.turn_registry is None

    def test_stop_all_hook_is_not_installed(self, tmp_path, monkeypatch):
        """``/api/stop`` 이 종전 단일 핸들 경로로 남아야 한다."""
        with _boot(tmp_path, monkeypatch) as booted:
            _boot_serial_ready(booted)
            assert booted.server._stop_all is None

    def test_metrics_provider_is_not_registered(self, tmp_path, monkeypatch):
        """직렬에서는 '동시 턴 수'가 존재하지 않는다 — None 이어야 압축
        이벤트에서 그 필드가 생략된다."""
        with _boot(tmp_path, monkeypatch) as booted:
            _boot_serial_ready(booted)
            assert turn_metrics.active_turns() is None


class TestParallelDispatcherWiring:
    """병렬 계약 — 옵션이 레지스트리와 서버에 실제로 가 닿는가."""

    def test_registry_is_built_and_published_to_the_server(self, tmp_path, monkeypatch):
        with _boot(tmp_path, monkeypatch, "--concurrency-contract", "parallel") as b:
            b.wait_for(lambda: b.registry is not None, what="레지스트리 생성")
            assert b.server.turn_registry is b.registry, (
                "/api/turns·/api/turn/{id}/interrupt 가 409 로 응답한다"
            )

    def test_stop_all_is_wired_to_interrupt_all(self, tmp_path, monkeypatch):
        """세션 전역 ``/api/stop`` 이 활성 턴 **전부**를 중단시켜야 한다 —
        단일 슬롯 핸들로는 동시 N턴을 표현할 수 없다."""
        with _boot(tmp_path, monkeypatch, "--concurrency-contract", "parallel") as b:
            b.wait_for(lambda: b.registry is not None, what="레지스트리 생성")
            assert b.server._stop_all is not None
            assert b.server._stop_all.__self__ is b.registry
            assert b.server._stop_all.__func__.__name__ == "interrupt_all"

    def test_cap_and_gate_defaults(self, tmp_path, monkeypatch):
        from agent_cli.loop.turns import DEFAULT_MAX_CONCURRENT_TURNS

        with _boot(tmp_path, monkeypatch, "--concurrency-contract", "parallel") as b:
            b.wait_for(lambda: b.kwargs, what="레지스트리 생성")
            assert b.kwargs[0]["max_concurrent"] == DEFAULT_MAX_CONCURRENT_TURNS
            assert b.kwargs[0]["per_user_gate"] is True

    def test_cap_and_gate_flags_reach_the_registry(self, tmp_path, monkeypatch):
        """P4 ablation 이 두 축을 독립적으로 조작한다 — 플래그가 CLI 에서
        멈추면 대조군과 실험군이 같은 것을 재게 된다."""
        with _boot(
            tmp_path,
            monkeypatch,
            "--concurrency-contract",
            "parallel",
            "--max-concurrent-turns",
            "2",
            "--no-per-user-gate",
        ) as b:
            b.wait_for(lambda: b.kwargs, what="레지스트리 생성")
            assert b.kwargs[0]["max_concurrent"] == 2
            assert b.kwargs[0]["per_user_gate"] is False

    def test_metrics_provider_reports_live_turn_count(self, tmp_path, monkeypatch):
        """N1: 압축 이벤트가 '압축 중 동시 턴 수'를 실으려면 레지스트리가
        프로바이더로 걸려 있어야 한다."""
        with _boot(tmp_path, monkeypatch, "--concurrency-contract", "parallel") as b:
            b.wait_for(lambda: b.registry is not None, what="레지스트리 생성")
            assert turn_metrics.active_turns() == 0  # 걸려 있고, 지금은 0

    def test_on_change_publishes_counts_to_the_renderer(self, tmp_path, monkeypatch):
        """레지스트리 → 렌더러 배선. 끊기면 상태 표시가 굳고 idle self-reap
        (``--idle-timeout``)이 영영 발동하지 않는다."""
        with _boot(tmp_path, monkeypatch, "--concurrency-contract", "parallel") as b:
            b.wait_for(lambda: b.registry is not None, what="레지스트리 생성")
            assert b.renderer.worker_is_busy() is False
            b.provider.gate = threading.Event()  # 턴을 inflight 로 붙든다
            try:
                b.registry.submit("hold", author="alice", conn_id="c1")
                b.wait_for(
                    lambda: b.renderer.worker_is_busy(), what="렌더러가 활성 턴을 반영"
                )
            finally:
                b.provider.gate.set()
            b.registry.wait_idle(timeout=15)
            b.wait_for(lambda: not b.renderer.worker_is_busy(), what="렌더러 idle 복귀")


class TestQueuedMessageBecomesATurn:
    """큐 → 턴 제출 경로. 여기가 끊기면 병렬 계약이 조용히 직렬로 되돌아간다."""

    def _boot_parallel(self, tmp_path, monkeypatch, *extra):
        return _boot(
            tmp_path, monkeypatch, "--concurrency-contract", "parallel", *extra
        )

    def test_enqueued_message_is_submitted_with_its_identity(
        self, tmp_path, monkeypatch
    ):
        """작성자·연결·큐 id 가 함께 넘어가야 공정성 게이트(conn_id)와
        계측 상관(queue_id)이 성립한다."""
        with self._boot_parallel(tmp_path, monkeypatch) as b:
            b.wait_for(lambda: b.registry is not None, what="레지스트리 생성")
            b.renderer.set_nickname("c-alice", "alice")
            item = b.server.enqueue("c-alice", "hello there")

            b.wait_for(lambda: b.registry.submitted, what="턴 제출")
            sub = b.registry.submitted[0]
            assert sub["text"] == "hello there"
            assert sub["author"] == "alice"
            assert sub["conn_id"] == "c-alice"
            assert sub["queue_id"] == item["id"]

    def test_worker_stays_free_for_the_next_message(self, tmp_path, monkeypatch):
        """직렬 워커와 달리 제출 후 **즉시** 다음 메시지를 받는다 — 이게 안
        되면 두 번째 사용자가 첫 턴 뒤에 줄을 서서 병렬이 무의미해진다."""
        with self._boot_parallel(tmp_path, monkeypatch) as b:
            b.wait_for(lambda: b.registry is not None, what="레지스트리 생성")
            for i in range(3):
                b.renderer.set_nickname(f"c{i}", f"user{i}")
                b.server.enqueue(f"c{i}", f"question {i}")
            b.wait_for(
                lambda: len(b.registry.submitted) == 3, what="세 건 모두 턴으로 제출"
            )
            assert [s["author"] for s in b.registry.submitted] == [
                "user0",
                "user1",
                "user2",
            ]

    def test_turn_actually_runs_run_loop_under_its_own_turn_id(
        self, tmp_path, monkeypatch
    ):
        """러너 클로저가 실제로 돈다 — 공유 ctx 에 질의가 남고, 응답은 그
        질의에 귀속되며(A6), 턴은 자기 워커 스레드에서 실행된다."""
        with self._boot_parallel(tmp_path, monkeypatch) as b:
            b.wait_for(lambda: b.registry is not None, what="레지스트리 생성")
            b.renderer.set_nickname("c-amy", "amy")
            b.server.enqueue("c-amy", "do the thing")
            b.wait_for(lambda: b.provider.threads, what="LLM 콜")
            b.registry.wait_idle(timeout=15)

            # 턴 전용 스레드에서 돌았다 = TurnRegistry 경로를 지났다.
            assert b.provider.threads[0].startswith("agent-turn-t"), (
                f"턴 스레드가 아니라 {b.provider.threads[0]} 에서 돌았다"
            )
            ctx = b.server.ctx
            with open(ctx.history_path, encoding="utf-8") as f:
                recs = [json.loads(line) for line in f if line.strip()]
            queries = [r for r in recs if r.get("kind") == "query"]
            assert len(queries) == 1
            assert "do the thing" in queries[0]["content"]
            assert queries[0]["author"] == "amy"
            replies = [r for r in recs if "reply_to" in r]
            assert replies and all(r["reply_to"] == queries[0]["id"] for r in replies)

    def test_turn_scope_section_is_applied_by_default(self, tmp_path, monkeypatch):
        """``--turn-scoping`` 기본 on 이 실제 프롬프트까지 도달하는가 —
        병렬 계약에서만 붙는 두 겹 게이트의 바깥쪽 배선."""
        with self._boot_parallel(tmp_path, monkeypatch) as b:
            b.wait_for(lambda: b.registry is not None, what="레지스트리 생성")
            b.renderer.set_nickname("c-amy", "amy")
            b.server.enqueue("c-amy", "rename the parser")
            b.wait_for(lambda: b.provider.systems, what="LLM 콜")
            b.registry.wait_idle(timeout=15)
            prompt = b.provider.systems[0]
            assert "You are serving turn t1" in prompt
            assert "rename the parser" in prompt

    def test_turn_scoping_can_be_disabled(self, tmp_path, monkeypatch):
        """절제 팔 — 플래그가 CLI 에서 러너까지 내려가는지."""
        with self._boot_parallel(tmp_path, monkeypatch, "--no-turn-scoping") as b:
            b.wait_for(lambda: b.registry is not None, what="레지스트리 생성")
            b.renderer.set_nickname("c-amy", "amy")
            b.server.enqueue("c-amy", "rename the parser")
            b.wait_for(lambda: b.provider.systems, what="LLM 콜")
            b.registry.wait_idle(timeout=15)
            assert "You are serving turn" not in b.provider.systems[0]


class TestShutdownDrainsTheRegistry:
    def test_registry_is_shut_down_on_server_teardown(self, tmp_path, monkeypatch):
        """daemon 스레드째 죽이면 진행 중 턴의 history append 가 반쯤 쓰인
        채 남는다 — SHUTDOWN 센티널에서 명시적으로 수거해야 한다."""
        with _boot(tmp_path, monkeypatch, "--concurrency-contract", "parallel") as b:
            b.wait_for(lambda: b.registry is not None, what="레지스트리 생성")
            registry = b.registry
        # 블록을 빠져나오며 실제 종료 경로를 탔다.
        assert registry.shutdown_calls == 1, "종료 시 레지스트리를 수거하지 않았다"
        assert registry.is_busy() is False


class TestTurnMetricsWiring:
    """``--turn-metrics`` → ``{session_dir}/turns.jsonl``.

    파일 존재로는 판정할 수 없다 — 그 파일은 복구 관측(``TurnRecorder``)이
    이미 소유하고 있고 계측은 **행을 얹어 쓴다**. 구별 키는 ``event``:
    TurnRecord 행에는 그 키가 없다.
    """

    def _metric_rows(self, ctx) -> list[dict]:
        path = ctx.session_dir / "turns.jsonl"
        if not path.is_file():
            return []
        rows = [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        return [r for r in rows if r.get("event") in _METRIC_EVENTS]

    def test_flag_enables_recording_into_the_session_dir(self, tmp_path, monkeypatch):
        with _boot(tmp_path, monkeypatch, "--turn-metrics") as b:
            _boot_serial_ready(b)
            assert turn_metrics.is_enabled()
            b.renderer.set_nickname("c1", "amy")
            b.server.enqueue("c1", "hi")
            b.wait_for(
                lambda: any(
                    r.get("phase") == "enqueue" for r in self._metric_rows(b.server.ctx)
                ),
                what="enqueue 계측 기록",
            )

    def test_default_records_nothing(self, tmp_path, monkeypatch):
        """opt-in 계약 — 켜지 않으면 계측 행이 한 줄도 생기지 않는다."""
        with _boot(tmp_path, monkeypatch) as b:
            _boot_serial_ready(b)
            assert not turn_metrics.is_enabled()
            b.renderer.set_nickname("c1", "amy")
            b.server.enqueue("c1", "hi")
            b.wait_for(lambda: b.provider.threads, what="턴 실행")
            time.sleep(0.2)  # 뒤늦은 기록까지 흘려보낸다
            assert self._metric_rows(b.server.ctx) == []


class TestSpectatorTokenResolution:
    """관전 자격의 발급 — ``--spectators`` / ``--view-token`` 해석."""

    def test_off_by_default_no_credential_exists(self, tmp_path, monkeypatch):
        with _boot(tmp_path, monkeypatch) as b:
            assert b.server.view_token is None

    def test_spectators_flag_generates_a_distinct_token(self, tmp_path, monkeypatch):
        with _boot(tmp_path, monkeypatch, "--spectators") as b:
            assert b.server.view_token
            assert b.server.view_token != b.server.token
            assert len(b.server.view_token) >= 32  # token_urlsafe(32)

    def test_explicit_view_token_is_used_verbatim_and_implies_spectators(
        self, tmp_path, monkeypatch
    ):
        """오케스트레이터가 재시작 너머로 안정적인 관전 URL 을 원하는 경우 —
        ``--spectators`` 를 따로 주지 않아도 켜져야 한다."""
        with _boot(tmp_path, monkeypatch, "--view-token", "watch-me") as b:
            assert b.server.view_token == "watch-me"

    def test_identical_tokens_are_refused_before_the_server_starts(
        self, tmp_path, monkeypatch
    ):
        """``_token_role`` 은 full 을 먼저 보므로 같은 문자열을 두 역할에 주면
        관전 링크가 조용히 전권을 나눠준다 (서버 쪽 확인:
        ``test_web_server.py::test_identical_tokens_resolve_as_full_control``).
        읽기 전용이 아닌 관전 URL 은 아예 없는 것보다 나쁘므로 여기서 막는다.
        """
        with _boot(
            tmp_path, monkeypatch, "--token", "same-tok", "--view-token", "same-tok"
        ) as b:
            pass
        assert b.server is None, "거부됐어야 할 기동이 서버를 세웠다"
        assert b.result is not None and b.result.exit_code == 2
        assert "--view-token must differ from --token" in b.result.stdout
