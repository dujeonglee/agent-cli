"""사람이 백그라운드 작업을 멈춘다 — 🧵 칩 ■ (v10.121.0,
docs/complete-while-running/REVIEW-fable.md 후속 리뷰).

고정하는 것:
- `kill(job, by=)` 는 소유자에게 보통 종료 보고와 같은 모양의 메일을 보낸다 — 머리줄
  ``stopped by user <name> after T`` (신호 rc 없음 — docs/complete-while-running/MEASURE.md;
  멈춤 전에 스스로 끝났으면 ``· it had already exited, rc=R``), 로그 끝 줄도 같은 사실,
  ``success`` False, 지시 없음.
  ``by`` 없는 kill(`drop_owner`·`stop_all`)은 종전대로 조용하다.
- 자연 종료·폴링과의 경합에서 통지는 **정확히 하나**다. TERM 뒤 폴링이 먼저 rc=-15 를
  보아도 "finished rc=-15" 가 나가지 않는다(멈춘 사실이 지워지면 모델은 되살린다).
- `on_change` → 렌더러 스티키 ``jobs`` → status.json·``/api/health`` 의 ``jobs``.
- ``POST /api/jobs/stop`` — 닉네임, 404·400·409, 동기 핸들러.
- 에이전트 소유 작업의 멈춤 메일은 그 에이전트의 inbox 로 가고 main 은 모른다.
"""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agent_cli.jobs import Job, JobRegistry, set_job_registry
from tests.monitor_delivery import RecordingDelivery


def _wait(cond, timeout=8.0, step=0.05):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if cond():
            return True
        time.sleep(step)
    return False


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


@pytest.fixture
def reg(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    r = JobRegistry(tmp_path)
    r.deliver = RecordingDelivery()
    set_job_registry(r)
    yield r
    r.stop_all()
    set_job_registry(None)


class FakeHandle:
    """프로세스 없는 핸들 — 경합 순서를 테스트가 정한다."""

    def __init__(self, rc=None, on_stop=None):
        self.rc = rc
        self.on_stop = on_stop
        self.stops = 0

    pid = 4242

    def poll(self):
        return self.rc

    def stop(self):
        self.stops += 1
        if self.rc is None:
            self.rc = -15
        if self.on_stop is not None:
            self.on_stop()

    def force(self):
        self.rc = -9

    stop_hint = "`kill -- -4242`"


def _fake_job(reg, handle, *, owner="main", command="bash build.sh") -> Job:
    job_id = reg._next_id()
    job = Job(
        id=job_id,
        pid=handle.pid,
        command=command,
        log=Path(f".agent-cli/tmp/jobs/{job_id}.log"),
        owner=owner,
        handle=handle,
    )
    (reg._cwd / job.log).parent.mkdir(parents=True, exist_ok=True)
    (reg._cwd / job.log).write_text(f"$ {command}\nstep 1\nstep 2\n")
    with reg._lock:
        reg._jobs[job.id] = job
    return job


class TestKillBy:
    def test_a_stop_by_name_tells_the_owner_like_an_exit_report(self, reg, tmp_path):
        job = reg.start("echo started; sleep 60", owner="main")
        # 머리줄(`$ echo started…`)이 아니라 명령의 출력 줄을 기다린다
        assert _wait(lambda: "\nstarted\n" in (tmp_path / job.log).read_text())
        pid = job.pid
        assert reg.kill(job, by="alice") is True
        assert not job.running and job.stopped_by == "alice"
        assert _wait(lambda: not _alive(pid))
        d = reg.deliver
        assert d.addrs == ["main"]  # 정확히 하나
        _addr, kw = d.calls[0]
        assert kw["mail"]["kind"] == "job" and kw["mail"]["success"] is False
        assert kw["author"] == "main" and kw["expects_reply"] is False
        text = kw["text"]
        head = text.splitlines()[0]
        assert head.startswith("🧵 job #1 stopped by user alice after ")
        assert job.rc in (-15, -9) and not job.exited_before_stop
        tail = " — `echo started; sleep 60`"
        assert head.endswith(f"s{tail}")  # "after 0s — `cmd`" — 신호 rc 없음
        assert "rc=" not in head and "already exited" not in head
        assert "log: .agent-cli/tmp/jobs/1.log (last 1 line(s))" in text
        assert "  started" in text
        # 사실만 — 다음 행동을 지시하지 않는다(리뷰 (e))
        low = text.lower()
        assert "restart" not in low and "do not" not in low and "don't" not in low
        log = (tmp_path / job.log).read_text()
        assert log.rstrip().endswith(
            f"[job #1 stopped by user alice after {job.elapsed()}]"
        )
        assert "exited rc=" not in log
        assert not reg.has_active_work()

    def test_kill_without_by_stays_silent(self, reg, tmp_path):
        job = reg.start("sleep 60", owner="main")
        assert reg.kill(job) is True
        assert reg.deliver.calls == []
        assert "[job #" not in (tmp_path / job.log).read_text()

    def test_drop_owner_and_stop_all_stay_silent(self, reg):
        reg.start("sleep 60", owner="agent:x")
        reg.start("sleep 60", owner="main")
        assert reg.drop_owner("agent:x") == 1
        assert len(reg.stop_all()) == 1
        assert reg.deliver.calls == []

    def test_a_second_kill_is_a_no_op(self, reg):
        job = _fake_job(reg, FakeHandle())
        assert reg.kill(job, by="alice") is True
        assert reg.kill(job, by="bob") is False
        assert len(reg.deliver.calls) == 1
        assert "stopped by user alice" in reg.deliver.reports[0]

    def test_an_unnamed_stopper_is_a_user(self, reg, tmp_path):
        job = _fake_job(reg, FakeHandle())
        assert reg.kill(job, by="") is True
        head = reg.deliver.reports[0].splitlines()[0]
        assert head.startswith("🧵 job #1 stopped by a user after ")
        assert "[job #1 stopped by a user after " in (tmp_path / job.log).read_text()
        assert reg.deliver.calls[0][1]["mail"]["success"] is False

    def test_a_process_that_exits_on_its_own_after_term_is_not_called_stopped(
        self, reg
    ):
        """TERM 을 받고 스스로 종료 코드(0 이상)를 냈다 — 신호로 죽은 게 아니다."""

        class ExitsCleanly(FakeHandle):
            def stop(self):
                self.rc = 0

        job = _fake_job(reg, ExitsCleanly())
        assert reg.kill(job, by="alice") is True
        head = reg.deliver.reports[0].splitlines()[0]
        assert job.exited_before_stop
        assert " · it had already exited, rc=0 — `bash build.sh`" in head


class TestRace:
    def test_poll_after_term_does_not_turn_the_stop_into_a_finish(self, reg):
        """TERM 직후 폴링 틱이 끼어든다 — 폴링이 rc=-15 를 보고 "finished" 를 보내면
        사람의 멈춤이 지워진다. `stopping` 이 그 틱을 비켜 가게 한다."""
        handle = FakeHandle(on_stop=lambda: reg.tick())
        job = _fake_job(reg, handle)
        assert reg.kill(job, by="alice") is True
        assert len(reg.deliver.calls) == 1
        assert reg.deliver.reports[0].startswith("🧵 job #1 stopped by user alice")
        assert "finished" not in reg.deliver.reports[0]

    def test_natural_exit_seen_first_wins_and_the_stop_is_a_no_op(self, reg):
        job = _fake_job(reg, FakeHandle(rc=0))
        reg.tick()  # 폴링이 먼저 — 자연 종료 통지
        assert reg.kill(job, by="alice") is False
        assert reg.deliver.addrs == ["main"]
        assert reg.deliver.reports[0].startswith("🧵 job #1 finished rc=0")

    def test_stop_first_on_an_already_exited_process_says_so(self, reg):
        """TERM 이 닿기 전에 스스로 끝난 창(≤ POLL_SECS) — 사람이 먼저여도 "멈췄다" 만
        쓰면 거짓이다: ``· it had already exited, rc=0`` (두 변형 모두). TERM 은 안
        보낸다. 실패(``success`` False)다 — 일을 맡긴 쪽이 멈췄다."""
        handle = FakeHandle(rc=0)
        job = _fake_job(reg, handle)
        assert reg.kill(job, by="alice") is True
        reg.tick()
        assert len(reg.deliver.calls) == 1 and handle.stops == 0
        head = reg.deliver.reports[0].splitlines()[0]
        assert head.startswith("🧵 job #1 stopped by user alice after ")
        assert head.endswith(" · it had already exited, rc=0 — `bash build.sh`")
        assert head.count("rc=") == 1
        assert reg.deliver.calls[0][1]["mail"]["success"] is False

    def test_threads_racing_a_real_exit_send_exactly_one_notice(self, reg):
        """진짜 프로세스·진짜 폴링 스레드와 사람의 kill 을 겹친다 — 몇 번을 해도 하나."""
        for _ in range(15):
            reg.deliver.calls.clear()
            job = reg.start("sleep 0.05", owner="main")
            time.sleep(0.04)
            t = threading.Thread(target=reg.kill, args=(job,), kwargs={"by": "alice"})
            t.start()
            reg.tick()
            t.join()
            assert _wait(lambda: not reg.has_active_work())
            assert len(reg.deliver.calls) == 1, reg.deliver.reports


class TestView:
    def test_view_lists_running_jobs_with_epoch_start(self, reg):
        a = reg.start("sleep 60", owner="main")
        b = reg.start("sleep 60   &&   echo   done", owner="agent:k")
        v = reg.view()
        assert v["count"] == 2
        assert v["list"][0] == {
            "id": a.id,
            "pid": a.pid,
            "owner": "main",
            "command": "sleep 60",
            "started_at": a.started_at,
            "stopping": False,
        }
        assert v["list"][1]["owner"] == "agent:k"
        assert v["list"][1]["command"] == "sleep 60 && echo done"  # 축약(_short)
        assert isinstance(v["list"][1]["started_at"], float)
        reg.kill(b)
        assert [j["id"] for j in reg.view()["list"]] == [a.id]

    def test_on_change_fires_on_start_stop_and_natural_end(self, reg):
        seen: list[dict] = []
        reg.on_change = seen.append
        job = reg.start("sleep 60", owner="main")
        assert seen[-1]["count"] == 1
        reg.kill(job, by="alice")
        # 멈춤 시작(stopping True) → 끝남(빈 목록)
        assert any(v["list"] and v["list"][0]["stopping"] for v in seen)
        assert seen[-1] == {"count": 0, "list": []}
        n = len(seen)
        reg.start("true", owner="main")
        assert _wait(lambda: len(seen) >= n + 2 and seen[-1]["count"] == 0)

    def test_a_failing_observer_does_not_break_jobs(self, reg):
        def boom(_view):
            raise RuntimeError("renderer gone")

        reg.on_change = boom
        job = reg.start("true", owner="main")
        assert _wait(lambda: reg.deliver.calls)
        assert job.rc == 0


class TestStatusFile:
    def test_jobs_reach_status_json_and_health(self, reg, tmp_path):
        from agent_cli.instance_file import read_status_file
        from agent_cli.render.web import WebRenderer
        from agent_cli.web.server import WebServer, create_app

        sess = tmp_path / "sess"
        sess.mkdir()
        renderer = WebRenderer(session_dir=str(sess))
        reg.on_change = renderer.jobs_changed
        client = TestClient(create_app(WebServer(renderer, token="t")))
        assert client.get("/api/health").json()["jobs"] is None
        job = reg.start("sleep 60", owner="main")
        status = read_status_file(sess)
        assert status["jobs"] == {
            "count": 1,
            "list": [
                {
                    "id": job.id,
                    "pid": job.pid,
                    "owner": "main",
                    "command": "sleep 60",
                    "started_at": job.started_at,
                }
            ],
        }
        assert client.get("/api/health").json()["jobs"] == status["jobs"]
        reg.kill(job, by="alice")
        assert "jobs" not in read_status_file(sess)
        assert client.get("/api/health").json()["jobs"] is None
        # 칩이 내려가도록 빈 목록도 스티키에 남는다
        assert renderer.hub.sticky_payload("jobs") == {"count": 0, "list": []}

    def test_nothing_is_sent_before_the_first_job(self, tmp_path):
        from agent_cli.render.web import WebRenderer

        renderer = WebRenderer()
        renderer.jobs_changed({"count": 0, "list": []})
        assert renderer.hub.sticky_payload("jobs") is None

    def test_jobs_is_a_status_sticky(self):
        from agent_cli.render.web import _STATUS_STICKY_KEYS

        assert "jobs" in _STATUS_STICKY_KEYS


class TestStopEndpoint:
    @pytest.fixture
    def stack(self, reg):
        from agent_cli.render.web import WebRenderer
        from agent_cli.render.web_hub import WebConnection
        from agent_cli.web.server import WebServer, create_app

        renderer = WebRenderer()
        renderer.hub.register(WebConnection(id="c1"))
        renderer.hub.set_nickname("c1", "alice")
        reg.on_change = renderer.jobs_changed
        client = TestClient(create_app(WebServer(renderer, token="t")))
        return renderer, client

    def test_stop_names_the_viewer_and_returns_the_view(self, reg, stack):
        _renderer, client = stack
        keep = reg.start("sleep 60", owner="main")
        job = reg.start("sleep 60", owner="main")
        r = client.post("/api/jobs/stop?token=t", json={"id": job.id, "conn_id": "c1"})
        assert r.status_code == 200
        body = r.json()
        assert body["stopped"] is True and body["id"] == job.id
        assert body["rc"] == job.rc
        assert [j["id"] for j in body["jobs"]["list"]] == [keep.id]
        assert reg.deliver.reports[0].startswith(
            f"🧵 job #{job.id} stopped by user alice"
        )

    def test_an_unknown_viewer_is_a_user(self, reg, stack):
        _renderer, client = stack
        job = reg.start("sleep 60", owner="main")
        r = client.post("/api/jobs/stop?token=t", json={"id": job.id})
        assert r.status_code == 200
        assert "stopped by a user after" in reg.deliver.reports[0]

    def test_unknown_or_finished_id_is_404(self, reg, stack):
        _renderer, client = stack
        assert client.post("/api/jobs/stop?token=t", json={"id": 99}).status_code == 404
        job = reg.start("true", owner="main")
        assert _wait(lambda: not job.running)
        assert (
            client.post("/api/jobs/stop?token=t", json={"id": job.id}).status_code
            == 404
        )

    def test_bad_id_is_400(self, stack):
        _renderer, client = stack
        for bad in ({}, {"id": "x"}, {"id": None}):
            assert client.post("/api/jobs/stop?token=t", json=bad).status_code == 400

    def test_a_stop_already_in_progress_is_409(self, reg, stack):
        _renderer, client = stack
        job = _fake_job(reg, FakeHandle())
        job.stopping = True  # 다른 뷰어의 멈춤이 진행 중
        r = client.post("/api/jobs/stop?token=t", json={"id": job.id})
        assert r.status_code == 409

    def test_requires_auth(self, reg, stack):
        _renderer, client = stack
        job = reg.start("sleep 60", owner="main")
        assert client.post("/api/jobs/stop", json={"id": job.id}).status_code == 401
        assert job.running

    def test_handler_is_sync_so_kill_does_not_block_the_event_loop(self):
        """`kill` 은 최대 6초를 막는다 — async 핸들러면 SSE 가 그만큼 선다."""
        import inspect

        from agent_cli.render.web import WebRenderer
        from agent_cli.web.server import WebServer, create_app

        app = create_app(WebServer(WebRenderer(), token="t"))
        (route,) = [r for r in app.routes if getattr(r, "path", "") == "/api/jobs/stop"]
        assert not inspect.iscoroutinefunction(route.endpoint)

    def test_no_registry_is_503(self, stack):
        _renderer, client = stack
        set_job_registry(None)
        assert client.post("/api/jobs/stop?token=t", json={"id": 1}).status_code == 503


class TestOwnerRouting:
    """멈춤 메일은 보통 종료 통지와 같은 `deliver` 길 — 에이전트 소유면 그 inbox, main 은 모른다."""

    @pytest.fixture
    def agents(self, tmp_path, monkeypatch):
        import agent_cli.render as render_mod
        from tests.test_agents_live import RecordingRenderer, make_registry

        monkeypatch.setattr(render_mod, "get_renderer", lambda: RecordingRenderer())
        registry = make_registry(tmp_path)
        mail: list[dict] = []
        requests: list[tuple] = []
        registry._push_reply = mail.append
        registry.request = lambda key, text, **kw: (
            requests.append((key, text, kw)) or ""
        )
        yield registry, mail, requests
        registry.shutdown_all()

    def test_agent_owned_stop_goes_to_that_agent_only(self, tmp_path, agents):
        from agent_cli.runtime import wire_job_delivery

        registry, mail, requests = agents
        jobs = JobRegistry(tmp_path)
        wire_job_delivery(registry, jobs)
        try:
            job = jobs.start("sleep 60", owner="agent:k")
            assert jobs.kill(job, by="alice") is True
            assert mail == []  # main 은 모른다 (소유자 의미론)
            ((key, text, kw),) = requests
            assert key == "k" and kw["kind"] == "job"
            assert kw["author"] == "main" and kw["expects_reply"] is False
            assert text.startswith(f"🧵 job #{job.id} stopped by user alice after ")
        finally:
            jobs.stop_all()

    def test_main_owned_stop_lands_in_the_main_mailbox(self, tmp_path, agents):
        from agent_cli.runtime import wire_job_delivery
        from agent_cli.subagent.agents_live import build_reply_record

        registry, mail, requests = agents
        jobs = JobRegistry(tmp_path)
        wire_job_delivery(registry, jobs)
        try:
            job = jobs.start("sleep 60", owner="main")
            jobs.kill(job, by="alice")
            assert requests == []
            (m,) = mail
            assert m["kind"] == "job" and m["success"] is False
            rec = build_reply_record(m)
            assert rec["tool"] == "job" and rec["success"] is False
            assert rec["content"].startswith(f"🧵 job #{job.id} stopped by user alice")
        finally:
            jobs.stop_all()
