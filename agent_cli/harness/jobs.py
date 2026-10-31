"""`HarnessJobs` — 하니스 작업의 실행기 (v10.42.0, docs/auditor/DESIGN.md 4판).

하니스 작업은 **팀원이 아니다**. `AgentRegistry` 에 들지 않으므로 로스터·주소
해석(`message`·`agent` request/kill)·`agents.json`·생존 상한·보드 칩 어디에도
없다 — 격리는 필터가 아니라 부재다. 팀원과 공유하는 것은 런 기계뿐이다:
`HarnessLoop` 는 one-shot 과 같은 private 종류(`loop.kind.PrivateLoop` — 질문
포트·레지스트리·메시지 없음 — `ask`·`message`·`reply` 가 도구 목록에 없다).

- **프로파일** `agent_cli/agents/harness/<job>.md` — 사용자 카탈로그
  (`subagent.profiles` 의 검색 경로) 밖이라 `@agents`·spawn·delegate 가 이름을
  모른다. 프로젝트의 `.agent-cli/agents/` 도 덮어쓰지 못한다.
- **기록** `<session>/harness/<job>/<label>/` (`context.session.harness_run_dir`) —
  세션과 함께 지워지고 이전되지만 `history` 테이블 밖이다.
- **결과** 런의 `complete` 결과(못 끝냈으면 그 사유)를 하니스가 main 메일함에
  넣는다(`deliver`). 전달은 모델의 선택이 아니다 — 3판까지의 회신 빚·독촉·
  이중 회신이 통째로 사라진 자리.
- **수명** `in_flight` 가 세션 수명 술어(`runtime.session_has_live_work`)의 한
  항이다: run 은 진행 중인 작업이 끝나야 꺼지고, `stop()` 이 세우고 join 한다.
- **화면** (v10.53.0) 런 하나 = 스코프 하나(``kind="harness"``). 종전엔 스코프를
  열지 않아 렌더러가 이 스레드의 카드를 전부 main 의 것으로 그렸다 — main 이
  쉬는 동안 스스로 기록을 뒤지는 것처럼 보였다(웹·CLI 둘 다). web 은 접힌
  ``🔎`` 카드 안에, CLI 는 출력을 모아 버리고 끝에 한 줄. 레지스트리·주소의
  "부재" 와 같은 원칙이 렌더 경로에도 선다.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from pathlib import Path

from agent_cli.context.session import HARNESS_DIR, harness_run_dir
from agent_cli.loop.kind import LoopKindError, PrivateLoop, Start
from agent_cli.resource_loader import ResourceLoader

#: 하니스 프로파일의 자리 — `agents/builtin`(사용자 카탈로그)과 형제, 검색 경로는
#: 이것 하나뿐이다.
HARNESS_PROFILES_DIR = Path(__file__).parent.parent / "agents" / "harness"
_profile_loader = ResourceLoader([HARNESS_PROFILES_DIR])

#: `AgentRuntime.as_dict()` 의 키 — `HarnessLoop` 가 읽는 것만 (`runtime` 인자의 계약).
RUNTIME_KEYS = frozenset(
    {
        "binding",
        "max_turns",
        "depth",
        "max_depth",
        "session",
        "hooks_config",
        "compaction_enabled",
    }
)


def load_harness_profile(job: str) -> tuple[str, dict, str]:
    """``(본문, frontmatter, 오류)`` — 하니스 프로파일만 본다."""
    res = _profile_loader.load_one(job)
    if res is None:
        return "", {}, f"harness profile '{job}' not found in {HARNESS_PROFILES_DIR}"
    if not res.body:
        return "", {}, f"harness profile '{job}.md' has no content"
    return res.body, dict(res.meta), ""


def harness_profile_names() -> list[str]:
    return _profile_loader.list_names()


class HarnessJobs:
    """세션당 하나. ``run(job, label, task)`` 이 스레드 하나로 런을 돌리고 결과를
    ``deliver(job, success, output)`` 로 넘긴다 — 배달 뒤에야 ``in_flight`` 가
    줄어들어, 수명 술어가 결과 전달 전에 세션을 끝내지 못한다.

    ``runtime`` 은 `AgentRuntime.as_dict()` 모양(``RUNTIME_KEYS``); 세션 셸 훅은
    싣지 않는다(하니스 런에 사용자 훅을 돌리지 않는다). ``runner`` 는 테스트
    주입(종류의 엔진 — 기본 `run_loop`). ``on_change(n)`` 은 진행 중 수가 바뀔 때마다
    — web 이 status.json 의 ``auditing`` 을 이것으로 쓴다.
    """

    def __init__(
        self,
        *,
        session_ctx,
        runtime: dict,
        deliver: Callable[[str, bool, str], str],
        runner: Callable | None = None,
        on_change: Callable[[int], None] | None = None,
    ) -> None:
        self.ctx = session_ctx
        self.runtime = {k: v for k, v in dict(runtime).items() if k in RUNTIME_KEYS}
        self._deliver = deliver
        self._runner = runner
        self.on_change = on_change
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._threads: list[threading.Thread] = []
        self._in_flight = 0

    @property
    def in_flight(self) -> int:
        with self._lock:
            return self._in_flight

    @property
    def stopped(self) -> bool:
        return self._stop.is_set()

    def run(self, job: str, label: str, task: str) -> str:
        """작업 하나를 띄운다 — 오류 문자열 또는 ``""``. 프로파일이 없거나 이미
        ``stop()`` 됐으면 띄우지 않는다."""
        if self._stop.is_set():
            return "harness jobs are stopped"
        body, meta, error = load_harness_profile(job)
        if error:
            return error
        with self._lock:
            self._in_flight += 1
            n = self._in_flight
            t = threading.Thread(
                target=self._run,
                args=(job, label, task, body, meta),
                daemon=True,
                name=f"harness-{job}-{label}",
            )
            self._threads.append(t)
        self._notify(n)
        t.start()
        return ""

    def _run(self, job: str, label: str, task: str, body: str, meta: dict) -> None:
        success, output = HarnessLoop(
            job,
            label,
            task,
            body,
            meta,
            runtime=self.runtime,
            session_ctx=self.ctx,
            stop_event=self._stop,
            engine=self._runner,
        ).run()
        try:
            self._deliver(job, success, output)
        finally:
            with self._lock:
                self._in_flight -= 1
                n = self._in_flight
            self._notify(n)

    def _notify(self, n: int) -> None:
        cb = self.on_change
        if cb is None:
            return
        try:
            cb(n)
        except Exception:
            pass  # 표면 알림은 best-effort

    def stop(self, timeout: float = 5.0) -> None:
        """진행 중인 런을 세우고 기다린다 — 세션 종료 시퀀스의 한 단계."""
        self._stop.set()
        with self._lock:
            threads = list(self._threads)
        for t in threads:
            t.join(timeout)


class HarnessLoop(PrivateLoop):
    """하니스 작업 한 런 — 작업마다 하나 (`LoopKind`, v10.87.0). one-shot 과 같은 private
    종류(메일함·제자리 `wait`·끝나면 전부 거둠)이고 다른 것만 여기 있다: 주소
    ``harness:<job>:<label>`` (v10.87.0 D2 — 종전엔 라벨이 없어 겹치는 감사 둘이 한 주소였다),
    도구·역할·모델은 `agents/harness/<job>.md` 프로파일, 훅 없음, 깊이 1, 중단은 모든 하니스
    런이 공유하는 `HarnessJobs._stop`. 결과 ``(ok, output)`` 은 호스트(`HarnessJobs._run`)가
    main 메일함에 넣는다 — 전달은 모델의 선택이 아니다."""

    verbose = False
    graceful_interrupt = False

    def __init__(
        self,
        job: str,
        label: str,
        task: str,
        body: str,
        meta: dict,
        *,
        runtime: dict,
        session_ctx,
        stop_event,
        engine=None,
    ) -> None:
        from agent_cli.constants import HARNESS_OWNER_PREFIX
        from agent_cli.subagent.runner import apply_role_overrides

        super().__init__(owner=f"{HARNESS_OWNER_PREFIX}{job}:{label}")
        self.job, self.label, self.task = job, label, task
        self.session_ctx = session_ctx
        binding = runtime.get("binding")
        allowed_tools, model, _hooks = apply_role_overrides(
            meta,
            allowed_tools=None,
            model=binding.model if binding is not None else "",
            hooks_config=None,
        )
        if binding is not None and model and model != binding.model:
            binding = binding.pinned(model)  # 프로파일이 고정한 모델
        self.binding = binding
        self.tools = allowed_tools
        self.depth = runtime.get("depth", 0) + 1
        self.max_depth = runtime.get("max_depth", 2)
        self.max_turns = runtime.get("max_turns", 0)
        self.session = runtime.get("session")
        self.compaction_enabled = runtime.get("compaction_enabled", True)
        self.hooks_config = None  # 하니스 런에 사용자 훅을 돌리지 않는다
        self.agent_name = job
        self.agent_role = body
        self.stop_event = stop_event
        self.engine = engine
        self._task_id = f"harness-{job}-{label}"

    def start(self) -> Start:
        return Start(query=self.task)

    def make_ctx(self, start: Start):
        from agent_cli.subagent.runner import create_subagent_ctx

        run_dir = harness_run_dir(self.session_ctx.session_dir, self.job, self.label)
        ctx, err = create_subagent_ctx(
            "none", self.session_ctx, run_dir, binding=self.binding
        )
        if ctx is None:
            raise LoopKindError(f"{self.job} could not start: {err}")
        return ctx

    def rejected(self, reason: str) -> tuple[bool, str]:
        return False, reason

    def open_scope(self, start: Start):
        """이 스레드의 카드를 이 런의 스코프에 가둔다. ``parent=""`` — main 의 어떤 스코프
        안도 아니다(감사는 main 런이 끝난 뒤 따로 돈다). 렌더러 오류는 런을 막지 않는다."""
        from agent_cli.render import get_renderer

        try:
            get_renderer().begin_scope(
                task_id=self._task_id,
                kind="harness",
                label=self.label,
                agent=self.job,
                parent="",
                ctx_dir=f"{HARNESS_DIR}/{self.job}/{self.label}",
            )
        except Exception:
            return False
        return True

    def close_scope(self, scoped, finished, secs: float) -> None:
        if not scoped:
            return
        from agent_cli.render import get_renderer

        ok, output = finished if finished is not None else (False, "")
        try:
            get_renderer().end_scope(
                task_id=self._task_id,
                kind="harness",
                success=ok,
                duration_s=secs,
                error="" if ok else output[:300],
            )
        except Exception:
            pass

    def finish(self, result, secs: float, released: str) -> tuple[bool, str]:
        if result.success:
            return True, result.output or ""
        return False, f"{self.job} did not complete: {result.error or 'no result'}"

    def crashed(self, exc: BaseException, secs: float) -> tuple[bool, str]:
        # 어떤 실패도 main 에게는 결과 한 장으로 간다
        return False, f"{self.job} crashed: {type(exc).__name__}: {exc}"
