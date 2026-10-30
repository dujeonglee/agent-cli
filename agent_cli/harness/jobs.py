"""`HarnessJobs` — 하니스 작업의 실행기 (v10.42.0, docs/auditor/DESIGN.md 4판).

하니스 작업은 **팀원이 아니다**. `AgentRegistry` 에 들지 않으므로 로스터·주소
해석(`message`·`agent` request/kill)·`agents.json`·생존 상한·보드 칩 어디에도
없다 — 격리는 필터가 아니라 부재다. 팀원과 공유하는 것은 런 기계뿐이다:
`create_subagent_ctx` + `run_subagent_message` + `ports_for_oneshot`(질문
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
import time
from collections.abc import Callable
from pathlib import Path

from agent_cli.context.session import HARNESS_DIR, harness_run_dir
from agent_cli.resource_loader import ResourceLoader

#: 하니스 프로파일의 자리 — `agents/builtin`(사용자 카탈로그)과 형제, 검색 경로는
#: 이것 하나뿐이다.
HARNESS_PROFILES_DIR = Path(__file__).parent.parent / "agents" / "harness"
_profile_loader = ResourceLoader([HARNESS_PROFILES_DIR])

#: `run_subagent_message` 의 kwargs 와 같은 키 — `AgentRuntime.as_dict()` 가 이
#: 모양이다 (`runtime` 인자의 계약).
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
    주입(기본 `run_subagent_message`). ``on_change(n)`` 은 진행 중 수가 바뀔 때마다
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
        task_id = f"harness-{job}-{label}"
        scoped = self._open_scope(task_id, job, label)
        t0 = time.monotonic()
        try:
            success, output = self._execute(job, label, task, body, meta)
        except Exception as exc:  # 어떤 실패도 main 에게는 결과 한 장으로 간다
            success, output = False, f"{job} crashed: {type(exc).__name__}: {exc}"
        if scoped:
            self._close_scope(task_id, success, time.monotonic() - t0, output)
        try:
            self._deliver(job, success, output)
        finally:
            with self._lock:
                self._in_flight -= 1
                n = self._in_flight
            self._notify(n)

    def _execute(
        self, job: str, label: str, task: str, body: str, meta: dict
    ) -> tuple[bool, str]:
        from agent_cli.constants import HARNESS_OWNER_PREFIX
        from agent_cli.runtime import ports_for_oneshot, release_private_owner
        from agent_cli.subagent.runner import (
            apply_role_overrides,
            create_subagent_ctx,
            run_subagent_message,
        )

        binding = self.runtime.get("binding")
        allowed_tools, model, _hooks = apply_role_overrides(
            meta,
            allowed_tools=None,
            model=binding.model if binding is not None else "",
            hooks_config=None,
        )
        if binding is not None and model and model != binding.model:
            binding = binding.pinned(model)  # 프로파일이 고정한 모델
        run_dir = harness_run_dir(self.ctx.session_dir, job, label)
        ctx, err = create_subagent_ctx("none", self.ctx, run_dir, binding=binding)
        if ctx is None:
            return False, f"{job} could not start: {err}"
        runner = self._runner or run_subagent_message
        owner = f"{HARNESS_OWNER_PREFIX}{job}"
        try:
            result, _secs = runner(
                task,
                ctx,
                ports=ports_for_oneshot(owner=owner),
                active_tools=allowed_tools,
                agent_name=job,
                agent_role=body,
                stop_event=self._stop,
                **{**self.runtime, "binding": binding, "hooks_config": None},
            )
        finally:
            release_private_owner(owner)  # v10.84.0: 자기 주소의 메일함·체크인 정리
        if result.success:
            return True, result.output or ""
        return False, f"{job} did not complete: {result.error or 'no result'}"

    @staticmethod
    def _open_scope(task_id: str, job: str, label: str) -> bool:
        """이 스레드의 카드를 이 런의 스코프에 가둔다. ``parent=""`` — main 의
        어떤 스코프 안도 아니다(감사는 main 런이 끝난 뒤 따로 돈다). ``ctx_dir``
        은 resume 이 카드 안을 다시 채우는 자리다. 렌더러 오류는 런을 막지
        않는다 — 카드는 표시일 뿐이다."""
        from agent_cli.render import get_renderer

        try:
            get_renderer().begin_scope(
                task_id=task_id,
                kind="harness",
                label=label,
                agent=job,
                parent="",
                ctx_dir=f"{HARNESS_DIR}/{job}/{label}",
            )
        except Exception:
            return False
        return True

    @staticmethod
    def _close_scope(task_id: str, success: bool, secs: float, output: str) -> None:
        from agent_cli.render import get_renderer

        try:
            get_renderer().end_scope(
                task_id=task_id,
                kind="harness",
                success=success,
                duration_s=secs,
                error="" if success else output[:300],
            )
        except Exception:
            pass

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
