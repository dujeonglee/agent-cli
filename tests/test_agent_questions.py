"""비동기 ask · 회신 빚 — 질문 목록과 요청·회신 회계 (docs/agent-ask/DESIGN.md §3).

v10.77.0 (B): 질문은 **사람에게만** 간다 — ❓ 트레이가 표면이고, 사람이 답하면
asker 에게 새 배달(``── answer [u-N] from the user re [q-M] ──``)로 온다. 에이전트·
main 에게 묻는 길은 `message`(요청 → 상대가 `reply` 를 빚진다) 하나라 `answer`
도구도, 에이전트가 빚지는 질문도 없다. 그래서 종전의 런 스코프(delivered_in)·
독촉(kick)·런 끝 닫기·§3.7 억제 테스트는 지웠다.

남은 결함 지점:

- **G1 사망 정리** — 세션 종료에서 지우면 resume 이 알릴 게 항상 0건이 된다
  (:class:`TestDeath`).
- **G3 런 스코프** — ``asked_in`` 없이는 다른 런의 질문까지 센다.
- **G5 사람 주소** — CLI ``user`` / 웹 ``user:{nick}`` / 뷰어마다 다른 닉.
- **회신 엄밀함** — 요청(r-)은 그 id 의 `reply` 로만 한 번, 갚을 것이 없으면 거부,
  끝내 안 갚으면 라벨 붙은 런 요약이 요청자에게 간다.
"""

from __future__ import annotations

import json
import re
import threading
import time
from unittest.mock import MagicMock

import pytest

import agent_cli.render as render_mod
from agent_cli.subagent.agents_live import (
    AgentRegistry,
    Question,
    QuestionPort,
    build_reply_record,
)
from tests.loop_ports import make_ports, split_ports
from tests.test_agents_live import (
    RecordingRenderer,
    _FakeLoopResult,
    make_registry,
    make_runner,
    wait_until,
)


@pytest.fixture
def renderer(monkeypatch):
    r = RecordingRenderer()
    monkeypatch.setattr(render_mod, "get_renderer", lambda: r)
    return r


@pytest.fixture
def mkreg(tmp_path):
    """레지스트리 팩토리 + **테어다운**.

    worker 는 데몬 스레드라 테스트가 끝나도 살아 있고, `get_renderer` 는
    다음 테스트의 monkeypatch 를 가리킨다 — 정리하지 않으면 앞 테스트의
    독촉 런이 **뒤 테스트의 렌더러 기록에 섞인다**(실제로 그랬다).
    """
    regs = []

    def _make(**kw):
        reg = make_registry(tmp_path, **kw)
        regs.append(reg)
        return reg

    yield _make
    for reg in regs:
        reg.shutdown_all()


def spawn_idle(reg):
    key, err = reg.spawn()
    assert not err, err
    assert wait_until(lambda: reg.get(key).state == "idle")
    return key


def wait_run_done(reg, key, *, timeout: float = 3.0) -> bool:
    """런 하나가 **정리까지** 끝나길 기다린다.

    `state == "idle"` 은 `_handle_request` 가 런 끝 정리(out 왕래 줄 렌더 ·
    `end_run` 의 폴백 배달·질문 닫기 · `_save_state`) **앞**에 세운다 —
    그걸 기다리면 아직 안 그려진 out 줄·안 나간 폴백을 볼 수 있다(Linux
    CI 에서 실제로 졌다, run 35930241864). `current_item` 는 정리 뒤에 0 으로
    돌아온다. 호출 전에 러너가 돌았음(`ran`)을 먼저 확인할 것 — 시작 전의
    0 과 구분이 안 된다.
    """
    return wait_until(lambda: reg.get(key).current_item == "", timeout=timeout)


class SubmitSpy:
    """``request`` 호출 인자를 기록 — 답 배달의 라우팅 인자를 고정한다."""

    def __init__(self, reg):
        self.reg = reg
        self.calls = []
        self._orig = reg.request

    def __enter__(self):
        def spy(key, message, **kw):
            self.calls.append({"key": key, "message": message, **kw})
            return self._orig(key, message, **kw)

        self.reg.request = spy
        return self

    def __exit__(self, *a):
        self.reg.request = self._orig


# ── 등록과 배달 ─────────────────────────────────


class TestRegisterAndDeliver:
    def test_duplicate_question_reuses_id(self, mkreg, tmp_path, renderer):
        reg = mkreg()
        a = spawn_idle(reg)
        first, _ = reg.register_question(a, "같은 질문")
        second, err = reg.register_question(a, "같은 질문")
        assert not err
        assert second == first
        assert len(reg._questions) == 1  # 두 번째는 새로 올리지 않는다

    def test_request_returns_a_plain_error_string(self, mkreg, tmp_path, renderer):
        """``submit() -> (error, verdict)`` 는 ask 답변 슬롯을 구분하려던
        것인데 슬롯이 사라져 되접혔다. 튜플로 돌아가면 **빈 튜플이 아닌
        모든 반환이 참**이라 호출자들의 ``if err:`` 가 성공을 에러로 읽는다."""
        reg = mkreg()
        a = spawn_idle(reg)
        assert reg.request(a, "일감") == ""
        assert isinstance(reg.request("agt-nope", "x"), str)
        assert isinstance(reg.request(a, "   "), str)
        assert not hasattr(reg, "submit")

    def test_empty_question_rejected(self, mkreg, tmp_path, renderer):
        reg = mkreg()
        a = spawn_idle(reg)
        qid, err = reg.register_question(a, "   ")
        assert not qid
        assert "empty" in err

    def test_question_is_not_delivered_only_listed(self, mkreg, tmp_path, renderer):
        """사람에겐 배달할 inbox 가 없다 — ❓ 트레이가 표면이다(§3.6). 주소는
        언제나 사람이다(v10.77.0) — 누가 시킨 일이든."""
        reg = mkreg()
        a = spawn_idle(reg)
        reg.get(a).current_author = "main"  # main 이 시킨 일이어도
        with SubmitSpy(reg) as spy:
            qid, err = reg.register_question(a, "배포해도 되나요?")
        assert not err and qid.startswith("q-")
        assert spy.calls == []
        assert reg.drain_replies() == []  # main 의 메일박스로도 안 간다
        assert [q.id for q in reg.open_human_questions()] == [qid]
        assert reg._questions[qid].target == "user"


# ── G3: 런 스코프 ───────────────────────────────


class TestRunScope:
    def test_asked_in_is_scoped_to_the_run(self, mkreg, tmp_path, renderer):
        """G3: 다른 런에서 걸어 둔(영영 열릴 수 있는) 사람 질문이 이후
        모든 런의 결과 알림에 섞이면 안 된다."""
        reg = mkreg()
        a = spawn_idle(reg)
        reg.get(a).current_item = "r-3"
        reg.register_question(a, "run 3 의 질문")
        reg.get(a).current_item = "r-5"
        assert len(reg.questions_asked_in(a, "r-3")) == 1
        assert reg.questions_asked_in(a, "r-5") == []


# ── 짝짓기 ──────────────────────────────────────


class TestAnswer:
    def test_only_a_person_may_answer(self, mkreg, tmp_path, renderer):
        """주소는 사람이다 — main 도 에이전트도 대신 답할 수 없다(v9.20.0 실측:
        main 이 사용자의 답을 지어냈다)."""
        reg = mkreg()
        a = spawn_idle(reg)
        qid, _ = reg.register_question(a, "질문")
        assert "addressed to the person" in reg.answer_question(qid, "답", by="main")
        assert "addressed to the person" in reg.answer_question(
            qid, "답", by="agent:zz"
        )
        assert reg.answer_question(qid, "답", by="user") == ""

    @pytest.mark.parametrize("answerer", ["user", "user:bob", "user:carol"])
    def test_any_human_may_answer(self, mkreg, tmp_path, renderer, answerer):
        """G5: CLI 는 ``user``, 웹은 ``user:{nick}``, 뷰어마다 닉이 다르다.
        문자열 동치로 검사하면 두 번째 뷰어의 트레이 답이 거부된다."""
        reg = mkreg()
        a = spawn_idle(reg)
        qid, _ = reg.register_question(a, "배포할까요?")
        assert reg.answer_question(qid, "네", by=answerer) == ""
        assert reg.open_human_questions() == []

    def test_claim_is_atomic(self, mkreg, tmp_path, renderer):
        reg = mkreg()
        a = spawn_idle(reg)
        qid, _ = reg.register_question(a, "질문")
        assert reg.answer_question(qid, "첫 답", by="user:bob") == ""
        err = reg.answer_question(qid, "둘째 답", by="user:carol")
        assert "already-answered" in err

    def test_empty_answer_rejected(self, mkreg, tmp_path, renderer):
        reg = mkreg()
        a = spawn_idle(reg)
        qid, _ = reg.register_question(a, "질문")
        assert "empty" in reg.answer_question(qid, "  ", by="user")
        assert len(reg.questions_asked_in(a, "")) == 1  # 열려 있다(런 밖)

    def test_answer_is_a_new_delivery_to_the_asker(self, mkreg, tmp_path, renderer):
        """답은 asker 에게 **새 배달**로 — 사람이 보낸 것이므로 사람 런이고(``u-``),
        봉투가 무엇에 대한 답인지(``re [q-…]``) 적는다 (v10.77.0, D)."""
        gate = threading.Event()
        reg = mkreg(runner=make_runner(block=gate))
        a = spawn_idle(reg)
        qid, err = reg.register_question(a, "어느 쪽인가요?")
        assert not err
        with SubmitSpy(reg) as spy:
            assert reg.answer_question(qid, "왼쪽", by="user:bob") == ""
        (call,) = spy.calls
        assert call["key"] == a  # 답은 물어본 쪽으로
        assert call["author"] == "user"  # 주소 = 사람
        assert call["expects_reply"] is True
        assert call["kind"] == "answer" and call["re"] == qid
        assert "왼쪽" in call["message"]
        assert "어느 쪽인가요?" in call["message"]  # 원 질문이 딸려간다
        gate.set()

    def test_answer_run_reads_the_answer_envelope(self, mkreg, tmp_path, renderer):
        """런을 여는 글은 봉투 한 모양 — 창·로그에는 본문만, 모델은 봉투를 본다.
        사람 런이라 그 id 는 ``u-`` 이고 `complete` 의 `answers` 로 닫는다."""
        seen = []

        def runner(query, ctx, **kw):
            seen.append((query, kw.get("user_requests")))
            return _FakeLoopResult(output="ok"), 0.01

        reg = mkreg()
        reg._runner = runner
        a = spawn_idle(reg)
        qid, _ = reg.register_question(a, "어느 쪽?")
        assert reg.answer_question(qid, "왼쪽", by="user:bob") == ""
        assert wait_until(lambda: seen)
        query, user_requests = seen[0]
        head = query.split("\n")[0]
        assert head.startswith("── answer [u-") and head.endswith(f"re [{qid}] ──")
        assert "from the user" in head
        assert "Continue the work it was blocking." in query
        (req,) = user_requests
        assert req["id"].startswith("u-") and "왼쪽" in req["text"]
        assert wait_run_done(reg, a)
        assert reg.drain_replies() == []  # 사람 런 — main 에게 가는 것은 없다


# ── G1: 사망 정리 ───────────────────────────────


class TestDeath:
    def test_kill_purges_its_questions(self, mkreg, tmp_path, renderer):
        """그가 건 질문은 폐기 — 남기면 사람이 답해도 받을 곳이 없다."""
        reg = mkreg()
        a, b = spawn_idle(reg), spawn_idle(reg)
        mine, _ = reg.register_question(b, "B 가 묻는다")
        other, _ = reg.register_question(a, "A 가 묻는다")
        reg.kill(b)
        assert wait_until(lambda: mine not in reg._questions)
        assert reg.answer_question(mine, "답", by="user").startswith("unknown")
        assert other in reg._questions  # 남의 질문은 그대로

    def test_shutdown_all_keeps_questions(self, mkreg, tmp_path, renderer):
        """G1: ``_worker`` finally 는 세션 종료에서도 돈다. 거기서 지우면
        직후의 ``_save_state`` 가 빈 목록을 저장해 resume 이 알릴 열린
        질문이 **항상 0건**이 된다."""
        reg = mkreg()
        a = spawn_idle(reg)
        qid, _ = reg.register_question(a, "살아남아야 한다")
        reg.shutdown_all()
        assert [q.id for q in reg.open_human_questions()] == [qid]
        saved = json.loads((tmp_path / "agents.json").read_text(encoding="utf-8"))
        assert [q["id"] for q in saved["questions"]] == [qid]


# ── 영속 ────────────────────────────────────────


class TestPersistence:
    """§3.9 — resume 은 질문을 **되살린다**. ctx 가 통째로 복원되므로
    나중에 도착한 답도 평소처럼 새 런으로 처리된다. 되살리지 못하는 것은
    asker 가 돌아오지 않은 것뿐이다(주소는 사람이라 언제나 있다)."""

    def test_human_question_comes_back_to_the_tray(self, mkreg, tmp_path, renderer):
        reg = mkreg()
        a = spawn_idle(reg)
        qid, _ = reg.register_question(a, "열린 채 종료")
        reg.shutdown_all()

        fresh = AgentRegistry(tmp_path, runtime={"model": "m"}, runner=make_runner())
        assert fresh.restore() == 1
        assert [q.id for q in fresh.open_human_questions()] == [qid]
        assert fresh.stale_questions == 0
        row = next(r for r in fresh.roster_snapshot() if r["key"] == a)
        assert [q["id"] for q in row["open_questions"]] == [qid]
        # 답할 수 있다 — 되살리는 이유 자체다.
        assert fresh.answer_question(qid, "네", by="user:carol") == ""
        fresh.shutdown_all()

    def test_asked_in_is_reset_on_resume(self, mkreg, tmp_path, renderer):
        """``asked_in`` 는 그 생의 런 이름이다 — 물어본 런은 사라졌으므로 ""
        로 되살린다(새 생의 런 결과 알림에 섞이지 않게)."""
        reg = mkreg()
        a = spawn_idle(reg)
        reg.get(a).current_item = "r-4"
        qid, _ = reg.register_question(a, "질문")
        assert reg._questions[qid].asked_in == "r-4"
        reg.shutdown_all()

        fresh = AgentRegistry(tmp_path, runtime={"model": "m"}, runner=make_runner())
        fresh.restore()
        assert fresh._questions[qid].asked_in == ""
        fresh.shutdown_all()

    def test_question_is_dropped_when_its_asker_did_not_come_back(
        self, mkreg, tmp_path, renderer
    ):
        """asker 가 안 돌아오면 답을 받을 데가 없다 — 되살리면 영구 미결로
        남는다.

        정상 경로에서는 ``kill``/crash 가 **즉시** 정리하므로(§3.8) 이 상태가
        잘 안 만들어진다. 그래서 파일을 직접 손봐 안전망 자체를 잰다 — 손상·
        수동 편집·버전 엇갈림에서 나올 수 있다.
        """
        reg = mkreg()
        a = spawn_idle(reg)
        reg._save_state()
        reg.shutdown_all()

        path = tmp_path / "agents.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        data["questions"] = [
            {  # asker 가 매니페스트에 없다
                "id": "q-orphan",
                "asker": "agt-gone",
                "target": "user",
                "text": "돌아오지 않은 쪽이 물었다",
                "asked_at": 1.0,
                "asked_in": "r-1",
            },
            {  # 살아 돌아온 asker — 되살린다
                "id": "q-kept",
                "asker": a,
                "target": "user",
                "text": "남는다",
                "asked_at": 1.0,
                "asked_in": "r-1",
            },
        ]
        path.write_text(json.dumps(data), encoding="utf-8")

        fresh = AgentRegistry(tmp_path, runtime={"model": "m"}, runner=make_runner())
        fresh.restore()
        assert list(fresh._questions) == ["q-kept"]
        assert fresh.stale_questions == 1
        fresh.shutdown_all()

    def test_malformed_question_entry_is_skipped(self, mkreg, tmp_path, renderer):
        reg = mkreg()
        spawn_idle(reg)
        reg._save_state()
        reg.shutdown_all()
        path = tmp_path / "agents.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        data["questions"] = [
            {"id": "q-bad"},  # 키 부족 (KeyError)
            "not a dict",
            {},
            {  # 타입이 깨졌다 (ValueError)
                "id": "q-bad2",
                "asker": "a",
                "target": "main",
                "text": "t",
                "nags": "여섯",
            },
        ]
        path.write_text(json.dumps(data), encoding="utf-8")

        fresh = AgentRegistry(tmp_path, runtime={"model": "m"}, runner=make_runner())
        fresh.restore()  # 조용히 무시 — 부팅을 막지 않는다
        assert fresh._questions == {}
        fresh.shutdown_all()

    def test_fresh_registry_reports_zero(self, mkreg, tmp_path, renderer):
        reg = AgentRegistry(tmp_path, runtime={"model": "m"}, runner=make_runner())
        reg.restore()
        assert reg.stale_questions == 0
        assert reg._questions == {}


# ── 사람 알림 · 표면 ────────────────────────────


class TestSurfaces:
    def test_open_human_question_is_reported_in_the_result(
        self, mkreg, tmp_path, renderer
    ):
        """사람에겐 강제를 못 건다(우리 루프가 아니다) — 대신 결과에 실어
        "내가 답을 안 해서 끝났구나"를 알린다."""
        reg = mkreg()

        ran = []

        def runner(query, ctx, **kw):
            reg.register_question(b, "덮어쓸까요?")
            ran.append(query)
            return _FakeLoopResult(output="정리 완료"), 0.01

        reg._runner = runner
        b = spawn_idle(reg)
        reg.request(b, "일감", author="user:bob")
        # 러너가 **돌았는지**를 먼저 기다린다 — `state == "idle"` 만 보면
        # 워커가 요청을 집어 들기 전의 idle 을 보고 즉시 통과한다(CI 부하
        # 시 실제로 깨졌다). 이 파일의 공통 함정.
        assert wait_until(lambda: ran)
        assert wait_run_done(reg, b)
        out = [
            c for c in renderer.named("agent_message") if c[1].get("direction") == "out"
        ][-1][1]["text"]
        assert "정리 완료" in out
        assert "1 question(s) still unanswered" in out
        assert "덮어쓸까요?" in out

    def test_asking_the_person_does_not_withhold_the_reply(
        self, mkreg, tmp_path, renderer
    ):
        """v10.77.0: §3.7 억제는 없다 — 사람에게 물은 런도 요청자(main)에게
        결과가 간다(회신이 없으면 라벨 붙은 런 요약). 사람의 답은 사람 런으로
        따로 오고 main 에게 두 번째 회신을 만들지 않는다."""
        from agent_cli.subagent.agents_live import _NO_REPLY_LABEL

        reg = mkreg()
        ran = []

        def runner(query, ctx, **kw):
            if "일감" in query:
                reg.register_question(b, "어느 쪽인가요?")
            ran.append(query)
            return _FakeLoopResult(output="부분 결과"), 0.01

        reg._runner = runner
        b = spawn_idle(reg)
        reg.request(b, "일감")
        assert wait_until(lambda: ran)
        assert wait_run_done(reg, b)
        (rep,) = reg.drain_replies()
        assert rep["kind"] == "reply" and rep["output"].startswith(_NO_REPLY_LABEL)
        assert rep["re"] == "r-1"
        assert (tmp_path / "agents" / b / "replies" / "reply-r-1.md").is_file()

    def test_roster_carries_open_questions(self, mkreg, tmp_path, renderer):
        reg = mkreg()
        a = spawn_idle(reg)
        qid, _ = reg.register_question(a, "배포?")
        row = next(r for r in reg.roster_snapshot() if r["key"] == a)
        assert [q["id"] for q in row["open_questions"]] == [qid]
        assert row["open_questions"][0]["to"] == "user"

    def test_registering_notifies_the_roster(self, mkreg, tmp_path, renderer):
        """트레이는 로스터의 ``open_questions`` 를 읽는다 — 알리지 않으면
        사람 주소 질문이 다음 브로드캐스트까지 화면에 안 뜬다."""
        reg = mkreg()
        a = spawn_idle(reg)
        before = len(renderer.named("agent_roster"))
        qid, _ = reg.register_question(a, "배포?")
        rosters = renderer.named("agent_roster")
        assert len(rosters) > before
        (row,) = [r for r in rosters[-1][1]["roster"] if r["key"] == a]
        assert [q["id"] for q in row["open_questions"]] == [qid]

    def test_open_human_question_counts_as_activity(self, mkreg, tmp_path, renderer):
        """idle-reap 이 세션을 걷으면 resume 은 되살리지 않으므로(§3.9)
        질문이 묘비명이 된다."""
        reg = mkreg()
        a = spawn_idle(reg)
        assert reg.any_activity() is False
        reg.register_question(a, "배포?")
        assert reg.any_activity() is True


# ── 웹 표면: 트레이 답 (§3.6) ───────────────────


class TestWebAnswerEndpoint:
    def _client(self):
        from fastapi.testclient import TestClient

        from agent_cli.render.web import WebRenderer
        from agent_cli.web.server import WebServer, create_app

        renderer = WebRenderer()
        server = WebServer(renderer, token="t")
        return server, TestClient(create_app(server))

    def test_answer_id_pairs_instead_of_queueing_work(self, mkreg, tmp_path, renderer):
        """트레이 답은 **새 일감이 아니다** — 질문에 짝지어야 한다.
        ``answer_id`` 없이 보내면 종전대로 inbox 로 들어간다."""
        reg = mkreg()
        a = spawn_idle(reg)
        qid, _ = reg.register_question(a, "덮어쓸까요?")
        server, client = self._client()
        server.agent_registry = reg

        r = client.post(
            f"/api/agent/{a}/input?token=t",
            json={"content": "네 덮어쓰세요", "answer_id": qid},
        )
        assert r.status_code == 200
        assert r.json()["answered"] == qid
        assert reg._questions == {}
        assert reg.get(a).inbox.qsize() == 0  # 일감으로 들어가지 않았다

    def test_stale_answer_id_is_rejected_not_queued(self, mkreg, tmp_path, renderer):
        reg = mkreg()
        a = spawn_idle(reg)
        server, client = self._client()
        server.agent_registry = reg
        r = client.post(
            f"/api/agent/{a}/input?token=t",
            json={"content": "답", "answer_id": "q-gone"},
        )
        assert r.status_code == 409
        assert reg.get(a).inbox.qsize() == 0


class Replies:
    """``drain_replies`` 는 비우므로 누적해서 본다."""

    def __init__(self, reg):
        self.reg = reg
        self.seen = []

    def all(self):
        self.seen.extend(self.reg.drain_replies())
        return self.seen

    def kinds(self, kind="reply"):
        return [r for r in self.all() if r.get("kind") == kind]


class TestReplyFreshness:
    """질문과 회신 (v10.77.0). 종전엔 질문을 건 런의 회신을 억제했다(§3.7 —
    답 런이 같은 요청에 회신하므로). 질문이 사람에게만 가면서 답 런은 사람
    런이 되었고 요청자에게 회신하지 않는다 — 억제할 이유가 없어 지웠다.
    남는 성질: 질문이 없는 런은 평소대로 회신하고, 같은 질문은 같은 런
    안에서만 접고, 사람 런은 창에 결과를 남긴다.
    """

    def test_run_without_a_question_replies_normally(self, mkreg, renderer):
        reg = mkreg()
        b = spawn_idle(reg)
        rep = Replies(reg)
        reg.request(b, "평범한 일감")
        assert wait_until(lambda: len(rep.kinds()) == 1)

    def test_same_question_across_runs_is_not_folded(self, mkreg, renderer):
        """중복 접기는 **런 안에서만**. 런마다 자기 질문이 있어야 그 런의 결과
        알림(`_with_human_notice`)이 맞는다."""
        reg = mkreg()
        ids, n = [], []

        def runner(query, ctx, **kw):
            n.append(query)
            if "일감" in query:
                qid, err = reg.register_question(b, "똑같은 질문")
                assert not err
                ids.append(qid)
            return _FakeLoopResult(output=f"out{len(n)}"), 0.01

        reg._runner = runner
        b = spawn_idle(reg)
        reg.request(b, "일감 1")
        assert wait_until(lambda: len(ids) == 1)
        reg.request(b, "일감 2")
        assert wait_until(lambda: len(ids) == 2)
        assert ids[0] != ids[1]  # 런이 다르면 별개의 질문

    def test_same_question_within_one_run_is_folded(self, mkreg, renderer):
        """런 안에서는 접는다 — ``_op_ask`` 가 루프 탐지기 앞에서 반환해
        같은 질문의 반복이 구조적으로 안 잡히기 때문이다."""
        reg = mkreg()
        ids = []

        def runner(query, ctx, **kw):
            for _ in range(3):
                qid, err = reg.register_question(b, "반복 질문")
                assert not err
                ids.append(qid)
            return _FakeLoopResult(output="ok"), 0.01

        reg._runner = runner
        b = spawn_idle(reg)
        reg.request(b, "일감")
        assert wait_until(lambda: len(ids) == 3)
        assert len(set(ids)) == 1
        assert len(reg._questions) == 1

    def test_human_run_still_renders_the_window(self, mkreg, renderer):
        """사람 발신 런은 애초에 밀 회신이 없다(창만) — 질문을 걸었어도 창에
        결과가 남아야 사용자가 에이전트가 한 일을 본다."""
        reg = mkreg()

        ran = []

        def runner(query, ctx, **kw):
            reg.register_question(b, "덮어쓸까요?")
            ran.append(query)
            return _FakeLoopResult(output="정리 완료"), 0.01

        reg._runner = runner
        b = spawn_idle(reg)
        reg.request(b, "일감", author="user:bob")
        # 러너가 **돌았는지**를 먼저 기다린다 — `state == "idle"` 만 보면
        # 워커가 요청을 집어 들기 전의 idle 을 보고 즉시 통과한다(CI 부하
        # 시 실제로 깨졌다). 이 파일의 공통 함정.
        assert wait_until(lambda: ran)
        assert wait_run_done(reg, b)
        outs = [
            c[1]
            for c in renderer.named("agent_message")
            if c[1].get("direction") == "out" and c[1].get("key") == b
        ]
        assert len(outs) == 1
        assert "정리 완료" in outs[0]["text"]
        assert (tmp_path_of(reg) / "agents" / b / "replies" / "reply-u-1.md").is_file()


def tmp_path_of(reg):
    return reg.session_dir


# ── QuestionPort — 루프가 닿는 유일한 seam ──────


class TestQuestionPort:
    """③ flip 이 통째로 기대는 클래스인데 가드가 하나도 없었다."""

    def test_labels_distinguish_address_from_sender(self, mkreg, renderer):
        """``me``(질문의 주소와 비교) 와 ``asker``(답이 돌아갈 곳)는 다른
        어휘다. 섞으면 답변자 검증과 배달이 동시에 깨진다."""
        reg = mkreg()
        a = spawn_idle(reg)
        port = reg.question_port(a)
        assert port.me == f"agent:{a}"
        assert port.asker == a
        main = reg.question_port(None)
        assert main.me == "main"
        assert main.asker == "main"

    def test_ask_always_addresses_the_person(self, mkreg, renderer):
        """주소는 누가 시켰든 사람이다 (v10.77.0, B) — 종전엔 원 요청자가
        기본이라 main 이 시킨 일의 질문이 main 에게 갔고 main 이 사람의 답을
        지어냈다(v9.20.0 실측)."""
        reg = mkreg()
        a = spawn_idle(reg)
        for author in ("user:bob", "main", "agent:agt-x"):
            reg.get(a).current_author = author
            qid, err = port_ask(reg, a, f"{author} 의 일에서")
            assert not err
            assert reg._questions[qid].target == "user"

    def test_main_ask_is_refused(self, mkreg, renderer):
        """main 이 포트로 물으면 **보이지도 답할 수도 사라지지도 않는**
        질문이 생긴다 — 로스터/창은 asker 를 에이전트 키로 찾아 못 찾고,
        ``open_human_questions`` 에만 남아 idle-reap 을 영구 차단한다.
        resume 이 되살리면 매 세션 부활한다."""
        reg = mkreg()
        qid, err = reg.question_port(None).ask("이거 해도 되나요?")
        assert not qid
        assert "blocking" in err
        assert reg._questions == {}
        assert reg.open_human_questions() == []
        assert reg.any_activity() is False

    def test_port_never_exposes_the_registry(self, mkreg, renderer):
        """레지스트리가 서브루프에 닿으면 '팀원 안 팀원 금지' 의 단일
        가드(loop/state.py)가 깨진다 — 포트의 공개 표면은 이것뿐이다."""
        reg = mkreg()
        port = reg.question_port(spawn_idle(reg))
        public = {n for n in dir(port) if not n.startswith("_")}
        assert public == {
            "ask",
            "key",
            "me",
            "asker",
            "nonblocking",
            "reply",  # v9.21.0 — 빚진 회신을 갚는 표면. 레지스트리 노출은 아니다.
            "debts",  # v9.22.0 — main·상주 공통 회신 빚 목록 (v10.77.0: answer 빚 없음)
        }
        # 상주는 비블로킹, main 은 기존 블로킹 경로 (§8-④)
        assert port.nonblocking is True
        assert reg.question_port(None).nonblocking is False
        assert isinstance(port, QuestionPort)


HANGUL = re.compile(r"[가-힣]")


def port_ask(reg, key, text):
    return reg.question_port(key).ask(text)


# ── (가) 가 기댈 표면 ───────────────────────────


class TestPersistShape:
    def test_as_dict_round_trips_every_field(self, mkreg, renderer):
        """resume 복원이 이 모양에 기댄다 — 필드가 빠지면 조용히 유실된다."""
        q = Question(
            id="q-shape",
            asker="agt-x",
            target="user",
            text="본문",
            asked_in="r-4",
        )
        d = q.as_dict()
        assert d == {
            "id": "q-shape",
            "asker": "agt-x",
            "target": "user",
            "text": "본문",
            "asked_at": q.asked_at,
            "asked_in": "r-4",
        }
        back = Question.from_dict(d)
        assert back is not None and (back.id, back.asker, back.text) == (
            "q-shape",
            "agt-x",
            "본문",
        )

    def test_unknown_asker_is_rejected(self, mkreg, renderer):
        reg = mkreg()
        qid, err = reg.register_question("agt-ghost", "질문")
        assert not qid
        assert "unknown asker" in err

    def test_roster_row_carries_text_and_time(self, mkreg, renderer):
        """트레이가 이 세 필드로 그려진다."""
        reg = mkreg()
        a = spawn_idle(reg)
        qid, _ = reg.register_question(a, "덮어쓸까요?")
        (row,) = [r for r in reg.roster_snapshot() if r["key"] == a]
        (item,) = row["open_questions"]
        assert item["id"] == qid
        assert item["text"] == "덮어쓸까요?"
        assert item["to"] == "user"
        assert isinstance(item["ts"], float)


# ── 리뷰가 짚은 무가드 지점 ────────────────────


class TestUncoveredSurfaces:
    @staticmethod
    def _human_batch(reg, key, gate, texts):
        """사람-직접 2건이 **확실히 한 배치로** 묶이게 한다.

        그냥 넣으면 유휴 워커가 첫 건을 즉시 집어 단건 경로로 가는 경합이
        있다. 먼저 일감 하나로 워커를 붙잡아 두고 넣은 뒤 풀어 준다.
        """
        reg.request(key, "선행 일감")
        assert wait_until(lambda: reg.get(key).state == "busy")
        for i, t in enumerate(texts, start=2):
            reg.get(key).inbox.put(
                {
                    "id": f"u-{i + 10}",
                    "text": t,
                    "author": "user:bob",
                    "expects_reply": True,
                    "ts": time.time(),
                }
            )
        gate.set()

    def test_batch_run_reports_open_human_questions(self, mkreg, renderer):
        """사람 발신 배치야말로 사람 주소 질문이 나오는 경로다(주소 =
        current_author) — 알림이 가장 필요한 곳이 비어 있었다."""
        gate = threading.Event()
        reg = mkreg()

        def runner(query, ctx, **kw):
            if "선행" in query:
                gate.wait(5)
                return _FakeLoopResult(output="선행 끝"), 0.01
            reg.register_question(b, "덮어쓸까요?")
            return _FakeLoopResult(output="둘 다 처리"), 0.01

        reg._runner = runner
        b = spawn_idle(reg)
        self._human_batch(reg, b, gate, ("하나", "둘"))
        assert wait_until(
            lambda: any(
                "둘 다 처리" in str(c[1].get("text"))
                for c in renderer.named("agent_message")
                if c[1].get("direction") == "out"
            ),
            timeout=5.0,
        )
        out = [
            c[1]
            for c in renderer.named("agent_message")
            if c[1].get("direction") == "out" and "둘 다 처리" in str(c[1].get("text"))
        ][-1]
        assert "1 question(s) still unanswered" in out["text"]
        assert "덮어쓸까요?" in out["text"]

    def test_main_run_ended_is_a_single_definition(self, mkreg, renderer):
        """main 은 펌프가 둘(run/web)이라 호출부가 둘이다 — 정의가 하나여야
        한쪽만 고치는 사고가 안 난다(`runtime.py` 가 있는 이유)."""
        from agent_cli.runtime import main_run_ended

        assert main_run_ended(None) == 0  # 레지스트리 없는 부팅에서 안전
        reg = mkreg()
        a = spawn_idle(reg)
        reg.note_owes("main", f"agent:{a}", "결정해 줘")  # main 이 받은 message 요청
        # 런 끝: 안 갚은 빚은 라벨 붙은 런 요약으로 정리된다
        assert main_run_ended(reg, "main 요약") == 1
        assert reg.reply_debts("main") == []
        assert main_run_ended(reg, "main 요약") == 0  # 두 번 보내지 않는다

    def test_cli_mail_notice_waits_only_when_main_is_busy(self, mkreg, renderer):
        """CLI 대기 줄 (v10.51.0): main 이 바쁠 때만 — 유휴면 곧 깨우기 런의 관찰
        줄이 나오므로 따로 알리지 않는다. 글은 곧 들어갈 관찰의 머리줄(봉투,
        v10.77.0)이라 종류마다 맞다(종전엔 사망 통지가 기본 문구로 떨어졌다)."""
        from agent_cli.main import _agent_mail_notice

        seen = []
        renderer.agent_mail_hint = lambda **kw: seen.append(kw["text"])
        _agent_mail_notice({"kind": "reply", "key": "agt-x", "output": "done"}, False)
        assert seen == []  # 유휴 — 관찰 줄이 전부다
        for reply, want in (
            (
                {"kind": "reply", "key": "agt-x", "output": "done", "item_id": "n-4"},
                "reply [n-4] from agt-x",
            ),
            (
                {"kind": "died", "key": "agt-x", "output": "boom", "item_id": "n-5"},
                "died [n-5]",
            ),
            (
                {
                    "kind": "peer_message",
                    "key": "agt-x",
                    "output": "결정해 줘",
                    "expects_reply": True,
                    "item_id": "r-6",
                },
                "request [r-6] from agt-x",
            ),
            (
                {"kind": "monitor", "output": "🔔 monitor tests: 3 failed"},
                "[monitor] 🔔",
            ),
            ({"kind": "job", "output": "🧵 job #1 finished rc=0"}, "[job] 🧵 job #1"),
        ):
            _agent_mail_notice(reply, True)
            line = seen[-1]
            assert line.startswith("⏳ Queued · [") and want in line, (reply, line)
            assert line.endswith("delivered to main when this turn ends")
            assert "?" not in line  # key 없는 모니터·작업 — "에이전트 ?" 금지


# ── ③ flip: 도구·디스패치·프롬프트 ─────────────


def _caps():
    from agent_cli.providers.capabilities import ModelCapabilities

    return ModelCapabilities(
        context_window=32768, max_output_tokens=4096, supports_thinking=False
    )


def _run_scripted(ctx, emissions, **kw):
    """스크립트된 LLM 응답으로 실제 ``run_loop`` 를 한 번 돌린다."""
    from unittest.mock import MagicMock

    from agent_cli.loop import run_loop
    from agent_cli.providers.base import LLMResponse

    provider = MagicMock()
    seq = list(emissions)
    provider.call = MagicMock(
        side_effect=lambda *a, **k: LLMResponse(
            content=seq.pop(0)
            if seq
            else json.dumps({"action": "complete", "result": "done"})
        )
    )
    return run_loop(
        query="Q",
        provider=provider,
        capabilities=_caps(),
        model="test-model",
        ctx=ctx,
        **split_ports(dict(kw)),
    ), provider


class TestFlipToolMount:
    def test_answer_is_not_a_tool_anywhere(self, mkreg, renderer):
        """v10.77.0 (B): 에이전트는 질문을 받지 않으므로 `answer` 도 없다 —
        실제 기록에서 11번 불려 11번 다 실패했다(사용자 요청에 쓰거나 id 를
        지어냄). 갚는 도구는 `reply` 하나다."""
        from agent_cli.loop import AgentLoop
        from agent_cli.tools.registry import _BUILTIN_ORDER

        reg = mkreg()
        a = spawn_idle(reg)

        def tools_for(questions):
            from unittest.mock import MagicMock

            loop = AgentLoop(
                query="Q",
                provider=MagicMock(),
                capabilities=_caps(),
                model="m",
                active_tools=["shell"],
                ports=make_ports(questions=questions),
            )
            return list(loop.tools_list)

        assert "answer" not in _BUILTIN_ORDER
        for port in (None, reg.question_port(a), reg.question_port(None)):
            assert "answer" not in tools_for(port)
        assert "reply" in tools_for(reg.question_port(a))  # force_mount


class TestFlipDispatch:
    def test_resident_ask_returns_immediately(self, mkreg, tmp_path, renderer):
        """실제 루프 관통: ``ask`` 가 막지 않고, 관찰이 '막히지 않았다' 를
        말하며, 질문은 사람의 트레이에 오른다. 모델이 옛 ``to`` 를 보내도
        주소는 사람이다(v10.77.0)."""
        from agent_cli.context.manager import ContextManager

        reg = mkreg()
        a = spawn_idle(reg)
        reg.get(a).current_author = "main"
        port = reg.question_port(a)
        ctx = ContextManager(tmp_path / "s1", max_context_tokens=30_000)

        result, provider = _run_scripted(
            ctx,
            [
                json.dumps({"action": "ask", "question": "v1 or v2?", "to": "main"}),
                json.dumps({"action": "complete", "result": "부분 결과"}),
            ],
            questions=port,
        )
        assert result.success and result.output == "부분 결과"
        # 막지 않았다 — 두 번째 턴이 실제로 돌았다.
        assert provider.call.call_count == 2
        (q,) = list(reg._questions.values())
        assert q.text == "v1 or v2?" and q.target == "user"
        obs = "\n".join(str(m) for m in ctx.get_raw_messages())
        assert "not blocked" in obs
        assert q.id in obs  # 모델이 id 를 본다 → 같은 것을 되묻지 않는다
        # 사람이 트레이에서 답한다
        assert reg.answer_question(q.id, "v2", by="user:dj") == ""
        assert reg._questions == {}

    def test_reply_with_a_user_request_id_points_to_complete(
        self, mkreg, tmp_path, renderer
    ):
        """v10.30.0 → v10.77.0: 꼬리의 사용자 요청을 갚을 빚으로 읽고 `reply`
        를 부르면 "unpaid" 대신 **그 id 가 무엇인지** 돌려준다 — 번호 공간이
        하나라 접두(`u-`)가 종류를 정확히 말한다. 구제는 하지 않는다."""
        from agent_cli.context.manager import ContextManager

        reg = mkreg()
        ctx = ContextManager(tmp_path / "s", max_context_tokens=30_000)
        result, _ = _run_scripted(
            ctx,
            [
                json.dumps({"action": "reply", "id": "u-1", "text": "안녕하세요!"}),
                json.dumps(
                    {"action": "complete", "result": "안녕!", "answers": ["u-1"]}
                ),
            ],
            questions=reg.question_port(None),
            query_request_id="u-1",
            query_author="Chill Capybara",
        )
        rec = next(m for m in ctx.get_raw_messages() if m.get("tool") == "reply")
        assert rec["success"] is False
        assert '"u-1" is a user request' in rec["content"]
        assert "`complete`'s `answers`" in rec["content"]
        assert "no unpaid request" not in rec["content"]
        assert result.success and result.output == "안녕!"

    def test_main_ask_still_blocks(self, mkreg, tmp_path, renderer):
        """main·delegate 의 ``ask``(사람에게 묻기)는 무변경이다 — 포트를
        받아도 비블로킹 분기를 타면 안 된다."""
        from agent_cli.context.manager import ContextManager

        reg = mkreg()
        ctx = ContextManager(tmp_path / "s", max_context_tokens=30_000)
        _run_scripted(
            ctx,
            [json.dumps({"action": "ask", "question": "괜찮나요?"})],
            questions=reg.question_port(None),
        )
        assert reg._questions == {}  # 목록에 안 들어간다
        obs = "\n".join(str(m) for m in ctx.get_raw_messages())
        assert "User responded" in obs  # 기존 사용자-프롬프트 경로


class TestFlipPrompt:
    def test_resident_loop_actually_gets_the_resident_prompt(
        self, mkreg, tmp_path, renderer
    ):
        """``_build_tools_section`` 을 직접 부르는 테스트는 **배선**을 못
        잰다 — `loop/prompt.py` 가 신호를 안 넘겨도 통과한다. 실제 루프가
        만든 시스템 프롬프트를 본다."""
        from unittest.mock import MagicMock

        from agent_cli.context.manager import ContextManager
        from agent_cli.loop import AgentLoop

        reg = mkreg()
        a = spawn_idle(reg)

        def system_for(questions, tag):
            loop = AgentLoop(
                query="Q",
                provider=MagicMock(),
                capabilities=_caps(),
                model="m",
                # ``ask`` 는 requires_handler="ctx" — ctx 없이는 벗겨진다.
                ctx=ContextManager(tmp_path / tag, max_context_tokens=30_000),
                active_tools=["ask", "shell"],
                ports=make_ports(questions=questions),
            )
            loop._prompt.rebuild()  # __init__ 은 조립만 — 빌드는 런 시작에
            assert "ask" in loop.tools_list
            return loop.system

        resident = system_for(reg.question_port(a), "r")
        main = system_for(reg.question_port(None), "m")
        # 도구 설명 — 상주 ask 만 "keep working" 을 말한다
        assert "one question and keep working" in resident
        assert "one question and keep working" not in main
        assert "wait for their reply" in main and "wait for their reply" not in resident
        # 인라인 가이드도 함께 — 둘이 엇갈리면 모델이 헷갈린다
        assert "does not block you" in resident
        assert "does not block you" not in main
        assert "pick by intent" in main and "pick by intent" not in resident

    def test_resident_ask_description_says_it_does_not_block(self):
        """설명이 'WAIT for their reply' 라고 거짓말하면 모델이 그걸 믿고
        추측으로 메운다."""
        from agent_cli.dialects import get as get_wf
        from agent_cli.prompts.system_prompt import _build_tools_section

        wf = get_wf("json_fc")
        blocking = _build_tools_section(["ask"], wf)
        resident = _build_tools_section(["ask"], wf, nonblocking_ask=True)
        assert "wait for their reply" in blocking
        assert "wait for their reply" not in resident
        assert "not blocked" in resident
        # 인라인 가이드도 함께 바뀐다 — 둘이 엇갈리면 모델이 헷갈린다
        assert "does not block you" in resident
        assert "does not block you" not in blocking
        assert "pick by intent" in blocking


# ── ⑧ 질문은 사람에게만 (v9.20.0 `to="user"` → v10.77.0 B) ────────


class TestAskToUser:
    """실측(프로브 1790070684): main 이 "사용자한테 질문해 봐" 라고 시킨
    에이전트의 ``ask`` 가 **main 에게** 갔다 — 주소 = 원 요청자 = main.
    트레이는 사람 주소 질문만 보이므로 사람은 아무것도 못 봤고, 그 질문을
    받은 main 은 사용자의 답을 **지어냈다**. v9.20.0 의 ``to="user"`` 를
    거쳐 v10.77.0 에서 주소를 사람 하나로 고정했다 — `ask` 는 사람에게만,
    에이전트·main 에게는 `message`.
    """

    def test_lands_in_the_tray_even_when_main_asked(self, mkreg, renderer):
        reg = mkreg()
        a = spawn_idle(reg)
        reg.get(a).current_author = "main"  # main 이 시킨 일
        qid, err = reg.question_port(a).ask("사람이 정할 일")
        assert not err
        q = reg._questions[qid]
        assert q.target == "user" and q.to_human
        assert [x.id for x in reg.open_human_questions()] == [qid], "트레이에 없다"
        # main 의 메일박스로는 **안 간다** — 그랬다면 main 이 또 대신 답한다.
        assert reg.drain_replies() == []

    def test_any_human_answers_and_the_reply_reaches_the_asker(self, mkreg, renderer):
        reg = mkreg()
        a = spawn_idle(reg)
        reg.get(a).current_author = "main"
        qid, _ = reg.question_port(a).ask("버릴까 남길까?")
        # main 은 답할 수 없다 — 사람에게 간 질문이다 (§0).
        assert "addressed to the person" in reg.answer_question(qid, "버려", by="main")
        # 아무 뷰어나 답한다 — 답은 asker(에이전트)에게, 주소는 user.
        with SubmitSpy(reg) as spy:
            assert reg.answer_question(qid, "남겨", by="user:dj") == ""
        assert qid not in reg._questions
        (call,) = spy.calls
        assert call["key"] == a and "남겨" in call["message"], "답이 asker 에게 안 갔다"
        assert call["author"] == "user"  # 주소 = user (§0 그대로)

    def test_neither_schema_offers_to(self, tmp_path):
        """스키마 배선 — `ask` 에 `to` 가 없다(v10.77.0). 상주 설명은 트레이를
        말하고 `message` 로 안내한다."""
        from agent_cli.context.manager import ContextManager
        from agent_cli.loop import AgentLoop
        from tests.loop_ports import make_ports

        class _Port:
            nonblocking = True

        def system_for(questions, tag):
            loop = AgentLoop(
                query="Q",
                provider=MagicMock(),
                capabilities=_caps(),
                model="m",
                ctx=ContextManager(tmp_path / tag, max_context_tokens=30_000),
                active_tools=["ask", "shell"],
                ports=make_ports(questions=questions),
            )
            loop._prompt.rebuild()
            return loop.system

        resident = system_for(_Port(), "r")
        main = system_for(None, "m")
        assert "question tray" in resident and "send it a `message`" in resident
        assert '"to"' not in resident and '"to"' not in main

    def test_guide_no_longer_promises_the_asker_a_reminder(self, tmp_path):
        from agent_cli.prompts.system_prompt import _ASK_INLINE_RESIDENT as g

        assert "you will be reminded" not in g, "asker 는 독촉을 못 받는다"
        assert "nobody chases them" in g
        assert "cannot reach a run that is still going" in g
        assert "`ask` reaches only the person" in g and "`message`" in g
        assert "`to`" not in g
        assert not HANGUL.findall(g)

    def test_dispatch_ignores_a_stale_to(self):
        """모델이 옛 ``to`` 를 보내도 포트는 질문만 받는다 — 주소는 포트가
        정한다(사람). 구제가 아니라 무시다: 스키마에 없는 키다."""
        import types

        from agent_cli.loop import AgentLoop
        from tests.loop_ports import make_ports

        seen = []

        class _Port:
            nonblocking = True

            def ask(self, text):
                seen.append(text)
                return "q-1", ""

        loop = AgentLoop(
            query="Q",
            provider=MagicMock(),
            capabilities=_caps(),
            model="m",
            ports=make_ports(questions=_Port()),
        )
        op = types.SimpleNamespace(
            action="ask", action_input={"question": "정할까요?", "to": "main"}
        )
        turn = types.SimpleNamespace(
            thought="", record={"role": "assistant", "content": ""}
        )
        loop._dispatch._op_ask("raw", turn, op, None)
        assert seen == ["정할까요?"]


# ── ⑨ 창의 "→ 보냄" 은 실제 배달만 (v9.20.1) ────────


class TestTrafficRowsAreRealSendsOnly:
    """창의 왕래 줄(`agent_message` out)은 **실제 발신**에만 생긴다 (v9.21.0).

    v9.20.1 은 배달 없는 산출물에 `to=""` 을 찍어 "배달 없음" 으로 그렸다.
    그 뒤 `complete` 이 국소가 되면서(어떤 경우에도 배달 없음) 그 줄은 존재
    이유를 잃었다 — 에이전트 채널의 final 카드가 이미 그것이고, "완료·
    배달 없음 / 폴백 / 보냄" 셋으로 가를 이유가 없다(사용자 지적). 남는
    out 은 둘: 하네스 폴백(라벨 달린 런 요약)과 user:* (창이 곧 배달).
    reply/message 는 보낼 때 스스로 out 을 남긴다.
    """

    def _run_one(self, mkreg, renderer, **req):
        reg = mkreg()
        ran = []

        def runner(query, ctx, **kw):
            ran.append(query)
            return _FakeLoopResult(output="산출물"), 0.01

        reg._runner = runner
        b = spawn_idle(reg)
        assert reg.request(b, "일감", **req) == ""
        assert wait_until(lambda: ran)
        assert wait_run_done(reg, b)
        outs = [
            c[1]
            for c in renderer.named("agent_message")
            if c[1].get("direction") == "out" and c[1].get("key") == b
        ]
        return reg, b, outs

    def test_unpaid_request_yields_one_labelled_fallback_row(self, mkreg, renderer):
        from agent_cli.subagent.agents_live import _NO_REPLY_LABEL

        _, _, outs = self._run_one(mkreg, renderer, author="agent:agt-x")
        assert len(outs) == 1
        assert outs[0]["to"] == "agent:agt-x"
        assert outs[0]["text"].startswith(_NO_REPLY_LABEL)

    def test_delivered_item_run_yields_no_row(self, mkreg, renderer):
        """받은 회신으로 시작한 런 — 산출물은 아무에게도 안 가고 줄도 없다."""
        _, _, outs = self._run_one(
            mkreg, renderer, author="agent:agt-x", expects_reply=False
        )
        assert outs == []

    def test_human_window_request_keeps_its_row(self, mkreg, renderer):
        _, _, outs = self._run_one(mkreg, renderer, author="user:bob")
        assert len(outs) == 1 and outs[0]["to"] == "user:bob"

    def test_conversation_log_matches_the_window(self, mkreg, renderer, tmp_path):
        import json

        _, b, _ = self._run_one(
            mkreg, renderer, author="agent:agt-x", expects_reply=False
        )
        log = tmp_path / "agents" / b / "conversation.jsonl"
        recs = [json.loads(line) for line in log.read_text().splitlines()]
        assert [r for r in recs if r.get("direction") == "out"] == []


# ── ⑪ 독촉 상한 3회 (v9.21.0) ──────────────────────


class TestReplyNagCap:
    """빚진 회신 없이 `complete` 하면 결과는 배달하고 독촉하되 **런 공유 예산**
    안에서 (v10.56.0 장부 P3 — 종전엔 빚 3회, 비사용자 런은 결과 미배달 거부).

    무제한이면 아무것도 런을 못 멈춘다 — 개입은 max_turns 를 소모하지 않고,
    B1 액션-루프 감지는 도구 경로에만 있어 반복 complete 을 안 본다. 예산
    뒤엔 레지스트리가 런 요약을 라벨 붙여 폴백 배달한다 — 침묵이 아니다.
    """

    class _OwedPort:
        nonblocking = True

        def __init__(self):
            self.replies = []

        def debts(self):
            if self.replies:
                return []
            return [{"kind": "reply", "to": "agent:agt-a", "id": "r-1", "text": "일감"}]

        def reply(self, text, *, id=""):
            if not id:  # v9.25.0: the real port refuses without an id
                return "reply needs `id`"
            self.replies.append(text)
            return ""

    @staticmethod
    def _env(ops):
        import json

        return "t\n\n" + json.dumps(ops)

    def _loop(self, port, *contents):
        import tempfile
        from pathlib import Path

        from agent_cli.context.manager import ContextManager
        from agent_cli.loop import run_loop
        from agent_cli.providers.base import LLMResponse

        p = MagicMock()
        p.call.side_effect = [LLMResponse(content=c) for c in contents]
        ctx = ContextManager(Path(tempfile.mkdtemp()) / "s", max_context_tokens=30000)
        res = run_loop(
            query="[agent:agt-a]: 일감",
            query_author_is_user=False,  # 에이전트 항목으로 시작한 런 (러너가 넘기는 값)
            provider=p,
            capabilities=_caps(),
            model="m",
            ctx=ctx,
            max_turns=10,
            dialect="json_fc",
            ports=make_ports(owner="agent:agt-b", questions=port),
        )
        return p, ctx, res

    def test_reminders_within_the_budget_then_the_run_ends(self):
        from agent_cli.loop.ledger import NAG_BUDGET

        port = self._OwedPort()
        done = self._env([{"action": "complete", "result": "끝"}])
        p, ctx, res = self._loop(port, *[done] * (NAG_BUDGET + 1))
        nags = [
            m
            for m in ctx.get_raw_messages()
            if m.get("role") == "user"
            and "your result was delivered, but" in m.get("content", "")
        ]
        assert len(nags) == NAG_BUDGET, f"독촉 {len(nags)}회 — 예산은 {NAG_BUDGET}"
        assert p.call.call_count == NAG_BUDGET + 1, "예산 뒤 complete 은 통과해야 한다"
        assert res.output == "끝"
        assert f"{NAG_BUDGET - 1} more reminders" in nags[0]["content"]
        assert "Last reminder" in nags[-1]["content"]
        assert '`reply` with `id` "r-1"' in nags[0]["content"]

    def test_replying_after_a_reminder_stops_them(self):
        port = self._OwedPort()
        p, _ctx, res = self._loop(
            port,
            self._env([{"action": "complete", "result": "끝"}]),
            self._env(
                [
                    {"action": "reply", "id": "r-1", "text": "답"},
                    {"action": "complete", "result": "끝"},
                ]
            ),
        )
        assert p.call.call_count == 2 and port.replies == ["답"]
        assert res.output == "끝"

    def test_complete_with_a_debt_is_delivered_and_the_nag_follows(self):
        """v10.56.0: 빚이 남은 complete 도 결과는 배달된다(final) — 독촉은 그 뒤의
        관찰이다. 종전엔 결과 미배달 거부(✗ 관찰)였다(실측 a209hq 의 "통과한 것처럼
        보임" 은 이제 실제로 배달된 것이고, 남은 빚은 꼬리·독촉이 가리킨다)."""
        import agent_cli.loop.dispatch as D

        seen = []
        real = D.render_step
        D.render_step = lambda kind, text, *a, **k: seen.append(
            (kind, k.get("success"))
        )
        try:
            port = self._OwedPort()
            self._loop(
                port,
                self._env([{"action": "complete", "result": "끝"}]),
                self._env(
                    [
                        {"action": "reply", "id": "r-1", "text": "답"},
                        {"action": "complete", "result": "끝"},
                    ]
                ),
            )
        finally:
            D.render_step = real
        finals = [k for k, _ in seen if k == "final"]
        assert len(finals) == 2, f"두 complete 모두 배달돼야 한다: {seen}"


# ── ⑩ complete 는 국소, 배달은 message/reply 뿐 (v9.21.0) ──


class TestCompleteIsLocal:
    """실측(257zmx 끝말잇기): 피어 `message` 로 시작한 런의 `complete` 출력이
    요청자에게 회신으로 **자동 배달**됐다. 대화에서는 상대가 이미 명시
    `message` 로 답하므로 그 에코가 같은 턴의 두 번째 항목이 되고, 받은 쪽이
    거기에 또 반응해 한 턴에 세 번 답했다.

    사용자 결정: `complete` 는 국소. 요청 항목(`expects_reply=True`)은 빚이고,
    `reply` 가 그 id 로 갚는다(돌려받을 것 없음, v10.77.0 부터 `to` 없음). `message` 는 언제나 요청
    (상대에게 빚을 지움)이되 내 요청자에게 보내면 내 빚도 갚는다 — "내 수,
    이제 네 차례". 빚진 게 없는 런의 `reply` 는 거부한다.
    """

    @staticmethod
    def _handler(reg, key):
        return reg._make_message_handler(reg.get(key))

    def _run(self, reg, b, requester, body, **req):
        ran = []

        def runner(query, ctx, **kw):
            # 레지스트리 전체가 이 러너를 쓴다 — 상대 에이전트의 런(회신을
            # 받은 런)에서도 몸체가 돌면 서로 message 를 주고받으며 무한히
            # 돈다. 원 요청("일감") 런에서만 몸체를 돌린다.
            ran.append(query)
            if "일감" in query:
                body()
            return _FakeLoopResult(output="런 요약"), 0.01

        reg._runner = runner
        with SubmitSpy(reg) as spy:
            assert reg.request(b, "일감", author=requester, **req) == ""
            assert wait_until(lambda: ran)
            assert wait_run_done(reg, b)
        return spy.calls

    def test_reply_settles_and_nothing_else_is_delivered(self, mkreg, renderer):
        reg = mkreg()
        a, b = spawn_idle(reg), spawn_idle(reg)
        calls = self._run(
            reg,
            b,
            f"agent:{a}",
            lambda: reg.question_port(b).reply("답:완료", id="r-1"),
        )
        to_a = [c for c in calls if c["key"] == a]
        assert len(to_a) == 1, f"에코가 섞였다: {to_a}"
        assert to_a[0]["message"].startswith("답:완료")
        assert to_a[0]["expects_reply"] is False, "reply 가 새 빚을 지웠다"

    def test_message_to_requester_settles_and_opens_their_debt(self, mkreg, renderer):
        """끝말잇기 규칙 — 항목 하나로 양쪽이 빚진다."""
        reg = mkreg()
        a, b = spawn_idle(reg), spawn_idle(reg)
        calls = self._run(
            reg, b, f"agent:{a}", lambda: self._handler(reg, b)(a, "늘그막, 네 차례")
        )
        to_a = [c for c in calls if c["key"] == a]
        assert len(to_a) == 1, f"폴백까지 갔다(빚이 안 갚혔다): {to_a}"
        assert to_a[0]["expects_reply"] is True

    def test_message_to_a_third_party_leaves_the_debt(self, mkreg, renderer):
        from agent_cli.subagent.agents_live import _NO_REPLY_LABEL

        reg = mkreg()
        a, b, c = spawn_idle(reg), spawn_idle(reg), spawn_idle(reg)
        calls = self._run(
            reg, b, f"agent:{a}", lambda: self._handler(reg, b)(c, "부탁")
        )
        assert [x["expects_reply"] for x in calls if x["key"] == c] == [True]
        (to_a,) = [x for x in calls if x["key"] == a]
        assert to_a["message"].startswith(_NO_REPLY_LABEL), (
            "빚이 안 갚혔는데 폴백이 없다"
        )

    def test_unpaid_debt_falls_back_to_a_labelled_summary(self, mkreg, renderer):
        from agent_cli.subagent.agents_live import _NO_REPLY_LABEL

        reg = mkreg()
        a, b = spawn_idle(reg), spawn_idle(reg)
        calls = self._run(reg, b, f"agent:{a}", lambda: None)
        (to_a,) = [x for x in calls if x["key"] == a]
        assert to_a["message"].startswith(_NO_REPLY_LABEL)
        assert "런 요약" in to_a["message"]
        assert to_a["expects_reply"] is False
        assert to_a["kind"] == "reply" and to_a["re"] == "r-1"  # 무엇에 대한 것인가

    def test_unanswered_message_request_reaches_the_asker_as_a_reply_envelope(
        self, mkreg, renderer
    ):
        """묻는 쪽은 반드시 무언가를 받는다 (v10.77.0 ①) — 종전엔 답 없는
        에이전트 질문을 런 끝에 사유와 함께 닫았다. 이제 에이전트에게 묻는
        길은 `message` 요청이고, 상대가 끝내 `reply` 하지 않으면 그 r- 빚이
        라벨 붙은 런 요약으로 묻는 쪽에 돌아온다 — 봉투가 그 요청을 가리킨다."""
        from agent_cli.subagent.agents_live import _NO_REPLY_LABEL

        reg = mkreg()
        got, box = [], {}

        def runner(query, ctx, **kw):
            if query == "원 일감":  # a 의 런 — b 에게 묻는다
                box["res"] = reg._make_message_handler(reg.get(a))(b, "정책?")
            elif query.startswith("── request ["):  # b 의 런 — 답하지 않는다
                pass
            elif query.startswith("── reply ["):  # a 가 받은 것
                got.append(query)
            return _FakeLoopResult(output="b 의 런 요약"), 0.01

        reg._runner = runner
        a, b = spawn_idle(reg), spawn_idle(reg)
        reg.request(a, "원 일감")
        assert wait_until(lambda: got, timeout=5.0)
        rid = box["res"].split("delivered as request ")[1].split(" ")[0]
        assert rid.startswith("r-")
        head, _, body = got[0].partition("\n")
        assert head.startswith("── reply [n-") and head.endswith(f"re [{rid}] ──")
        assert f"from {b}" in head
        assert body.startswith(_NO_REPLY_LABEL) and "b 의 런 요약" in body

    def test_reply_to_main_carries_attribution(self, mkreg, renderer):
        reg = mkreg()
        b = spawn_idle(reg)
        reg.set_current_run_authors(["dj"])
        self._run(reg, b, "main", lambda: reg.question_port(b).reply("보고", id="r-1"))
        mail = reg.drain_replies()
        assert [m["output"] for m in mail] == ["보고"], f"에코가 섞였다: {mail}"
        assert mail[0].get("answers") == ["dj"]

    def test_reply_is_refused_when_nothing_is_owed(self, mkreg, renderer):
        """회신·통지·답으로 시작한 런 — ack 가 새는 자리. 거부하고 message 로 안내."""
        reg = mkreg()
        a, b = spawn_idle(reg), spawn_idle(reg)
        seen = []
        calls = self._run(
            reg,
            b,
            f"agent:{a}",
            lambda: seen.append(reg.question_port(b).reply("확인했습니다")),
            expects_reply=False,  # 배달 항목으로 시작한 런
        )
        (err,) = seen
        assert "nothing to reply to" in err and "use `message` (`to`" in err
        assert not [x for x in calls if x["key"] == a], "거부됐는데 배달됐다"

    def test_second_reply_is_refused_with_the_true_reason(self, mkreg, renderer):
        """실측(a209hq): 정정하려는 두 번째 reply 가 "요청으로 시작되지 않은
        런" 이라는 거짓 사유로 거부됐다. 사유는 "이미 답했다" 여야 한다."""
        reg = mkreg()
        a, b = spawn_idle(reg), spawn_idle(reg)
        seen = []

        def body():
            port = reg.question_port(b)
            seen.append(port.reply("칙령", id="r-1"))
            seen.append(port.reply("칙명", id="r-1"))

        calls = self._run(reg, b, f"agent:{a}", body)
        assert seen[0] == ""
        assert "already replied" in seen[1] and "`message` (`to`" in seen[1]
        assert "not started by a request" not in seen[1]
        assert next(c["message"] for c in calls if c["key"] == a).startswith("칙령")
        assert len([c for c in calls if c["key"] == a]) == 1, "두 번째가 배달됐다"

    def test_reply_in_a_human_window_run_is_refused_truthfully(self, mkreg, renderer):
        """사람이 창에서 시킨 런 — 빚이 없어 거부되는 건 맞지만, 사유가
        "요청으로 시작하지 않은 런" 이면 거짓이고 `message(to=user:…)` 로
        유인해 실패시킨다. 창이 곧 배달임을 말한다."""
        reg = mkreg()
        b = spawn_idle(reg)
        tm = reg.get(b)
        tm.current_author = "user:dj"
        reg.note_owes(f"agent:{b}", "user:dj", "리뷰해줘")  # 사람 창 요청 — 빚 아님
        err = reg.question_port(b).reply("리뷰 결과")
        assert "person watching this window" in err and "Just `complete`" in err
        assert "message(to=" not in err

    def test_main_with_nothing_owed_is_refused_and_pointed_to_agent_request(
        self, mkreg, renderer
    ):
        """v10.56.0: main 도 `reply` 를 갖는다(빚을 갚는 도구는 하나) — 빚이 없으면
        거부하고, 일을 시키는 길(`agent request`)을 알려 준다."""
        reg = mkreg()
        err = reg.question_port(None).reply("x")
        assert (
            err.startswith("nothing to reply to")
            and '`agent` with mode "request"' in err
        )

    def test_port_reports_the_debt_and_its_repayment(self, mkreg, renderer):
        reg = mkreg()
        a, b = spawn_idle(reg), spawn_idle(reg)
        me = f"agent:{b}"
        tm = reg.get(b)
        tm.current_author = f"agent:{a}"
        port = reg.question_port(b)
        reg.note_owes(me, f"agent:{a}", "일감")
        assert [d["to"] for d in port.debts()] == [f"agent:{a}"]
        reg.settle(me, f"agent:{a}")
        assert port.debts() == []
        assert reg.reply_debt(me, f"agent:{a}").settled  # "이미 갚았다" 의 근거
        assert port.debts() == []
        reg.note_owes(me, "user:dj", "리뷰해줘")  # 사람은 창이 곧 배달
        assert port.debts() == []
        assert reg.reply_debt(me, "user:dj").human


# ── 런 끝의 빚 정리 (v9.22.0) ────────────────────────


class TestMainRunEnd:
    """main 의 런 끝 — `end_run("main", output)`: 남은 회신 빚은 라벨 붙은 런
    요약을 그 에이전트 inbox 로. main 의 회신 빚은 에이전트가 main 에게 보낸
    `message`(expects_reply=True)만 만든다 — `reply` 는 답이라 빚이 아니다."""

    def test_message_to_main_creates_a_debt_and_reply_does_not(self, mkreg, renderer):
        reg = mkreg()
        a = spawn_idle(reg)
        reg.message_to_main(a, "결정해 줘", expects_reply=True)
        reg.message_to_main(a, "보고", expects_reply=False)
        recs = reg.drain_replies()
        assert [r["expects_reply"] for r in recs] == [True, False]
        # 배달을 흉내 낸다 — core 의 _deliver_agent_mail 이 하는 일
        for r in recs:
            if r["expects_reply"]:
                reg.note_owes("main", f"agent:{r['key']}", r["output"])
        port = reg.question_port(None)
        assert [d["kind"] for d in port.debts()] == ["reply"]
        assert port.debts()[0]["to"] == f"agent:{a}"

    def test_reply_settles_the_debt_and_agent_request_does_not(self, mkreg, renderer):
        """v10.56.0 (장부 P3 §11.3): 빚은 `reply(id)` 가 갚는다. 종전엔 main 의 아무
        `agent request` 가 그 에이전트 앞 가장 오래된 빚을 갚은 것으로 쳤다."""
        reg = mkreg()
        a = spawn_idle(reg)
        d = reg.note_owes("main", f"agent:{a}", "결정해 줘")
        assert reg.request(a, "새 일") == ""  # main 발신 — 일을 시킨다
        assert [x.id for x in reg.reply_debts("main")] == [d.id], (
            "request 가 빚을 갚았다"
        )
        assert reg.question_port(None).reply("여기 답", id=d.id) == ""
        assert reg.reply_debts("main") == []
        assert reg.question_port(None).debts() == []

    def test_unsettled_debt_falls_back_to_a_labelled_summary(self, mkreg, renderer):
        from agent_cli.subagent.agents_live import _NO_REPLY_LABEL

        reg = mkreg()
        a = spawn_idle(reg)
        reg.note_owes("main", f"agent:{a}", "결정해 줘")
        with SubmitSpy(reg) as spy:
            assert reg.end_run("main", "main 은 이렇게 끝냈다") == 1
        (call,) = [c for c in spy.calls if c["key"] == a]
        assert call["message"].startswith(_NO_REPLY_LABEL)
        assert "main 은 이렇게 끝냈다" in call["message"]
        assert call["expects_reply"] is False
        assert call["author"] == "main" and call["kind"] == "reply"
        assert reg.reply_debts("main") == []

    def test_mail_envelope_distinguishes_request_from_reply(self, mkreg, renderer):
        """봉투 한 모양 (v10.77.0, D) — 요청은 그 id 로 갚으라고 말하고(빚 id =
        메일 id), 회신은 무엇에 대한 것인지(``re``)만 적는다(할 일이 없다)."""
        reg = mkreg()
        a = spawn_idle(reg)
        reg.message_to_main(a, "결정해 줘", expects_reply=True)
        reg.message_to_main(a, "보고", expects_reply=False, re="r-9")
        reg.message_to_main(a, "알림", notice=True)
        m, r, n = reg.drain_replies()
        mc = build_reply_record(m, registry=reg)["content"]
        rid = m["item_id"]
        assert rid.startswith("r-")
        assert mc.startswith(f"── request [{rid}] from {a} ──\n결정해 줘")
        assert mc.endswith(f'(You owe it a `reply` with `id` "{rid}".)')
        rc = build_reply_record(r, registry=reg)["content"]
        assert rc == f"── reply [{r['item_id']}] from {a} re [r-9] ──\n보고"
        nc = build_reply_record(n, registry=reg)["content"]
        assert nc == f"── notice [{n['item_id']}] from {a} ──\n알림"


# ── 유형별 통일 규칙 (v9.22.0) ────────────────────────


class TestUnifiedDebtRules:
    """수신 **유형**으로 규칙이 정해진다 — 루프(main/상주)가 아니라.

    | 유형 | complete 이 오면 |
    |---|---|
    | 사용자 요청 | 수락 · answers 의 키 제거 · 남은 요청 독촉 |
    | message 요청 | 수락 후 독촉(main·상주 공통) · 예산 8 · 런 끝 폴백 |
    | 섞임 | 수락 → 요청 정산 → message 빚 독촉 |
    """

    @staticmethod
    def _env(ops):
        import json

        return "t\\n\\n" + json.dumps(ops)

    def _run(
        self,
        *,
        ports,
        query,
        contents,
        query_request_id="",
        user_requests=None,
        is_user=True,
    ):
        import tempfile
        from pathlib import Path

        from agent_cli.context.manager import ContextManager
        from agent_cli.loop import run_loop
        from agent_cli.providers.base import LLMResponse

        p = MagicMock()
        p.call.side_effect = [LLMResponse(content=c) for c in contents]
        ctx = ContextManager(Path(tempfile.mkdtemp()) / "s", max_context_tokens=30000)
        res = run_loop(
            query=query,
            query_author="Bob",
            query_author_is_user=is_user,
            query_request_id=query_request_id,
            user_requests=user_requests,
            provider=p,
            capabilities=_caps(),
            model="m",
            ctx=ctx,
            max_turns=10,
            dialect="json_fc",
            ports=ports,
        )
        return p, ctx, res

    # ── 상주: 사람 창 요청은 회계에 오른다 ──────────

    def test_human_window_request_gets_accounting_in_the_agent_loop(
        self, mkreg, renderer
    ):
        """에이전트 창의 사용자 요청도 main 과 같다 — answers 필수, 없으면 되돌림."""
        from agent_cli.subagent.resident_loop import ResidentLoop

        reg = mkreg()
        b = spawn_idle(reg)
        ports = ResidentLoop.make_ports(
            key=b,
            message_handler=lambda to, text, **kw: "",
            questions=reg.question_port(b),
            absorb_inbox=lambda: reg.absorb_pending(reg.get(b)),
            peer_roster=None,
        )
        tm = reg.get(b)
        tm.current_author = "user:dj"
        reg.note_owes(f"agent:{b}", "user:dj", "리뷰해줘")
        p, _ctx, res = self._run(
            ports=ports,
            query="[user:dj]: 리뷰해줘",
            user_requests=[{"id": "7", "author": "user:dj", "text": "리뷰해줘"}],
            contents=[
                self._env([{"action": "complete", "result": "리뷰 결과"}]),
                self._env(
                    [{"action": "complete", "result": "리뷰 결과", "answers": ["7"]}]
                ),
            ],
        )
        assert p.call.call_count == 2, (
            "사람 창 요청인데 answers 없는 complete 이 통과했다"
        )
        assert res.output == "리뷰 결과"

    def test_registry_passes_human_requests_to_the_runner(self, mkreg, renderer):
        reg = mkreg()
        seen = {}

        def runner(query, ctx, **kw):
            seen["user_requests"] = kw.get("user_requests")
            return _FakeLoopResult(output="ok"), 0.01

        reg._runner = runner
        b = spawn_idle(reg)
        reg.request(b, "리뷰해줘", author="user:dj")
        assert wait_until(lambda: "user_requests" in seen)
        (req,) = seen["user_requests"]
        assert req["author"] == "user:dj" and req["text"] == "리뷰해줘" and req["id"]

    def test_agent_originated_request_carries_no_user_accounting(self, mkreg, renderer):
        reg = mkreg()
        seen = {}

        def runner(query, ctx, **kw):
            seen["user_requests"] = kw.get("user_requests")
            return _FakeLoopResult(output="ok"), 0.01

        reg._runner = runner
        a, b = spawn_idle(reg), spawn_idle(reg)
        reg.request(b, "일감", author=f"agent:{a}")
        assert wait_until(lambda: "user_requests" in seen)
        assert seen["user_requests"] == []

    # ── 상주: 질문(ask) 빚도 거부 대상 ─────────────

    def test_resident_completing_with_an_unpaid_request_is_nagged(
        self, mkreg, renderer
    ):
        """상주가 받은 `message` 요청(r-)을 안 갚고 `complete` 하면 결과는 배달되고
        독촉이 그 id 로 `reply` 를 가리킨다. 갚으면 런이 끝나고 빚이 없다."""
        from agent_cli.subagent.resident_loop import ResidentLoop

        reg = mkreg()
        a, b = spawn_idle(reg), spawn_idle(reg)
        ports = ResidentLoop.make_ports(
            key=b,
            message_handler=lambda to, text, **kw: "",
            questions=reg.question_port(b),
            absorb_inbox=lambda: reg.absorb_pending(reg.get(b)),
            peer_roster=None,
        )
        tm = reg.get(b)
        tm.current_author = f"agent:{a}"
        reg.note_owes(f"agent:{b}", f"agent:{a}", "어느 쪽?", id="r-1")
        p, ctx, _res = self._run(
            ports=ports,
            query=f"── request [r-1] from {a} ──\n어느 쪽?",
            is_user=False,  # 에이전트 항목으로 시작한 런
            contents=[
                self._env([{"action": "complete", "result": "끝"}]),
                self._env(
                    [
                        {"action": "reply", "id": "r-1", "text": "왼쪽"},
                        {"action": "complete", "result": "끝"},
                    ]
                ),
            ],
        )
        assert p.call.call_count == 2, "요청을 안 갚은 complete 이 그대로 끝났다"
        nags = [
            m
            for m in ctx.get_raw_messages()
            if m.get("role") == "user"
            and "your result was delivered, but" in m.get("content", "")
        ]
        assert nags and '`reply` with `id` "r-1"' in nags[0]["content"]
        assert reg.reply_debts(f"agent:{b}") == []  # 갚았다

    def test_main_completing_with_an_unreplied_message_is_nagged_not_refused(
        self, mkreg, renderer
    ):
        from tests.loop_ports import make_ports

        reg = mkreg()
        a = spawn_idle(reg)
        # 런 시작 시 배달되는 회신 기대 message → main 이 빚진다
        reg.message_to_main(a, "결정해 줘", expects_reply=True)
        ports = make_ports(
            owner="main", agent_registry=reg, questions=reg.question_port(None)
        )
        import agent_cli.loop.dispatch as D

        finals = []
        real = D.render_step
        D.render_step = lambda kind, text, *x, **k: (
            finals.append(text) if kind == "final" else None
        )
        try:
            p, ctx, _res = self._run(
                ports=ports,
                query="사용자 요청",
                query_request_id="1",
                contents=[
                    self._env(
                        [{"action": "complete", "result": "답", "answers": ["1"]}]
                    ),
                    self._env([{"action": "complete", "result": "답", "answers": []}]),
                    self._env(
                        [
                            {"action": "reply", "id": "r-1", "text": "결정했다"},
                            {"action": "complete", "result": "끝", "answers": []},
                        ]
                    ),
                ],
            )
        finally:
            D.render_step = real
        # 첫 complete: 사용자 요청은 정산됐고(수락, final 렌더), message 빚이 남아 독촉
        assert finals and finals[0] == "답", (
            "main 의 complete 이 거부됐다 — 수락돼야 한다"
        )
        nags = [
            m
            for m in ctx.get_raw_messages()
            if m.get("role") == "user" and "still open" in m.get("content", "")
        ]
        # v10.56.0: main 도 `reply(id)` 로 갚는다 — 독촉이 그 도구를 가리킨다
        assert nags and '`reply` with `id` "r-' in nags[0]["content"]
        # 결과는 매번 배달됐고(독촉은 예산 8 안), `reply(id)` 로 갚자 런이 끝났다
        assert p.call.call_count == 3 and finals == ["답", "답", "끝"]
        assert len(nags) == len(finals) - 1  # 마지막 complete 만 독촉 없이 끝난다
        assert reg.reply_debts("main") == []

    def test_mixed_run_settles_requests_then_nags_debts(self, mkreg, renderer):
        """섞임: 수락 → answers 의 요청 제거 → message 빚 독촉."""
        from tests.loop_ports import make_ports

        reg = mkreg()
        a = spawn_idle(reg)
        d = reg.note_owes("main", f"agent:{a}", "어느 쪽?")  # main 에 배달된 요청
        ports = make_ports(
            owner="main", agent_registry=reg, questions=reg.question_port(None)
        )
        p, ctx, _res = self._run(
            ports=ports,
            query="사용자 요청",
            query_request_id="1",
            contents=[
                self._env([{"action": "complete", "result": "답", "answers": ["1"]}]),
                self._env(
                    [
                        {"action": "reply", "id": d.id, "text": "v2"},
                        {"action": "complete", "result": "끝", "answers": []},
                    ]
                ),
            ],
        )
        obs = [
            m.get("content", "")
            for m in ctx.get_raw_messages()
            if m.get("role") == "user"
        ]
        blob = "\n".join(obs)
        assert "still open" in blob and f'`reply` with `id` "{d.id}"' in blob
        assert reg.reply_debts("main") == []
        assert p.call.call_count == 2


class TestAskKeepsTheRequesterDebt:
    """v10.77.0: §3.7 의 회신 억제는 없다 — 사람에게 물어도 요청자에게 진 빚은
    열린 채 남고, 런 끝에 갚지 않았으면 라벨 붙은 런 요약이 간다. 종전엔
    요청자에게 물으면 그 빚을 감췄다(답 런이 같은 빚을 진다는 근거) — 질문이
    사람에게만 가면서 그 근거가 사라졌다."""

    def test_asking_the_person_keeps_the_requester_debt(self, mkreg, renderer):
        from agent_cli.subagent.agents_live import _NO_REPLY_LABEL

        reg = mkreg()
        b = spawn_idle(reg)
        me = f"agent:{b}"
        tm = reg.get(b)
        tm.current_author = "main"
        reg.note_owes(me, "main", "일감", id="r-1")
        qid, err = reg.question_port(b).ask("결정해 주세요")
        assert not err and qid
        assert [d.to for d in reg.reply_debts(me)] == ["main"], (
            "사람에게 물었다고 main 빚이 사라졌다"
        )
        # 런 끝: main 은 라벨 붙은 런 요약을 폴백으로 받는다 — 침묵이 아니다
        assert reg.end_run(me, "사용자 답을 기다립니다", run="r-1", success=True) == 1
        (rec,) = [r for r in reg.drain_replies() if r.get("kind") == "reply"]
        assert rec["output"].startswith(_NO_REPLY_LABEL) and rec["re"] == "r-1"

    def test_debt_left_by_a_waiting_run_carries_into_the_next_run(
        self, mkreg, renderer
    ):
        """`message` 로 묻고 `wait` 로 기다린 런의 빚은 다음 런(상대의 회신이
        연 런)으로 넘어가 거기서 `reply` 로 갚는다 (v10.77.0). 종전엔
        런 시작(`begin_run`)이 장부를 비워 요청자가 회신도 런 요약도 못 받았다."""
        from types import SimpleNamespace

        reg = mkreg()
        a, b = spawn_idle(reg), spawn_idle(reg)
        me = f"agent:{b}"
        seen = []

        def runner(query, ctx, **kw):
            seen.append(query)
            if query == "일감":  # 첫 런 — 다른 데 묻고 기다린다
                return SimpleNamespace(
                    success=True, output="⏸ waiting", waiting=True
                ), 0.01
            # 회신이 연 런 — 넘어온 빚을 갚는다
            assert [d.id for d in reg.reply_debts(me)] == ["r-1"]
            assert reg.question_port(b).reply("최종 답", id="r-1") == ""
            return _FakeLoopResult(output="끝"), 0.01

        reg._runner = runner
        assert reg.request(b, "일감", author="main") == ""
        assert wait_until(lambda: seen == ["일감"])
        assert wait_run_done(reg, b)
        assert [d.id for d in reg.reply_debts(me)] == ["r-1"]  # wait — 빚은 그대로
        assert reg.drain_replies() == []  # 폴백도 없다
        reg._deliver_peer_reply(b, a, "정책은 A", 0, re="r-9")
        assert wait_until(lambda: len(seen) == 2)
        assert wait_run_done(reg, b)
        (mail,) = reg.drain_replies()
        assert mail["output"] == "최종 답" and mail["re"] == "r-1"
        assert reg.reply_debts(me) == []


class TestIdleMeansTheRunIsFullyDone:
    """v9.22.4 — `state == "idle"`/`handled` 는 런 끝 정리(out 왕래 줄 · `end_run`
    폴백/닫기 · `_save_state`) **뒤**에 세운다. 종전엔 정리 앞이라 idle 을
    기다린 관찰자(테스트·로스터)가 아직 안 그려진 순간을 봤다(Linux CI 낙방)."""

    def test_state_is_still_busy_during_run_end_cleanup(self, mkreg, renderer):
        reg = mkreg()
        a, b = spawn_idle(reg), spawn_idle(reg)
        seen = {}
        real_end_run = reg.end_run

        def spying_end_run(debtor, output, **meta):
            if debtor != f"agent:{b}" or seen:  # a 의 폴백 런이 부르는 것은 제외
                return real_end_run(debtor, output, **meta)
            tm = reg.get(b)
            seen["state"] = tm.state
            seen["handled"] = tm.handled
            seen["out_drawn"] = any(
                c[1].get("direction") == "out" and c[1].get("key") == b
                for c in renderer.named("agent_message")
            )
            return real_end_run(debtor, output, **meta)

        reg.end_run = spying_end_run
        assert reg.request(b, "일감", author=f"agent:{a}", expects_reply=True) == ""
        assert wait_until(lambda: "state" in seen)
        assert wait_run_done(reg, b)
        assert seen == {"state": "busy", "handled": 0, "out_drawn": True}
        assert reg.get(b).state == "idle" and reg.get(b).handled == 1


# ── 요청별 빚 + reply(id) (v9.25.0) ─────────────────────────────


class TestPerRequestDebts:
    """빚은 상대별 하나가 아니라 **요청별 하나**다(사용자 결정). 런 도중 요청이
    흡수되면 같은 상대의 빚이 여럿이고 각각 회신을 받아야 한다. `reply` 는
    `id` 로 어느 요청인지 밝힌다 — `complete` 의 `answers` 와 같은 원칙: 없으면
    항상 거부. id 가 전역에서 유일하므로 그것만으로 상대가 정해진다(`to` 없음)."""

    def _two(self, reg, b, to):
        me = f"agent:{b}"
        reg.note_owes(me, to, "첫 요청", id="3")
        reg.note_owes(me, to, "둘째 요청", id="5")
        return me

    def test_each_request_is_its_own_debt(self, mkreg, renderer):
        reg = mkreg()
        a, b = spawn_idle(reg), spawn_idle(reg)
        me = self._two(reg, b, f"agent:{a}")
        assert [(d.id, d.text) for d in reg.reply_debts(me)] == [
            ("3", "첫 요청"),
            ("5", "둘째 요청"),
        ]
        assert [d["id"] for d in reg.question_port(b).debts()] == ["3", "5"]

    def test_reply_without_id_is_always_refused(self, mkreg, renderer):
        reg = mkreg()
        a, b = spawn_idle(reg), spawn_idle(reg)
        self._two(reg, b, f"agent:{a}")
        with SubmitSpy(reg) as spy:
            err = reg.question_port(b).reply("답")
        assert "needs `id`" in err and "[3]" in err and "[5]" in err
        assert "Send `reply` with that `id`" in err
        assert not spy.calls, "거부됐는데 배달됐다"
        # 빚이 하나뿐이어도 같다 — 원칙이지 모호함의 해소가 아니다
        reg.note_owes(f"agent:{b}", f"agent:{a}", "하나", id="9")
        assert "needs `id`" in reg.question_port(b).reply("답")

    def test_reply_with_id_settles_only_that_request(self, mkreg, renderer):
        reg = mkreg()
        a, b = spawn_idle(reg), spawn_idle(reg)
        me = self._two(reg, b, f"agent:{a}")
        with SubmitSpy(reg) as spy:
            assert reg.question_port(b).reply("둘째 답", id="5") == ""
        assert [c["message"].split("\n")[0] for c in spy.calls] == ["둘째 답"]
        assert spy.calls[0]["expects_reply"] is False
        assert [d.id for d in reg.reply_debts(me)] == ["3"]  # 첫 요청은 아직

    def test_unknown_and_repeated_ids_are_refused(self, mkreg, renderer):
        reg = mkreg()
        a, b = spawn_idle(reg), spawn_idle(reg)
        self._two(reg, b, f"agent:{a}")
        port = reg.question_port(b)
        err = port.reply("답", id="8")
        assert 'no unpaid request "8"' in err and f'[3] {a}: "첫 요청"' in err
        assert port.reply("답", id="3") == ""
        err = port.reply("정정", id="3")
        assert "already replied to [3]" in err and "`message` (`to`" in err

    def test_id_alone_routes_the_reply(self, mkreg, renderer):
        """main 과 동료에게 진 빚이 섞여 있어도 id 하나로 제자리에 간다
        (v10.77.0, C — `to` 를 지웠다)."""
        reg = mkreg()
        a, b = spawn_idle(reg), spawn_idle(reg)
        me = f"agent:{b}"
        reg.note_owes(me, "main", "main 의 요청", id="3")
        reg.note_owes(me, f"agent:{a}", "동료의 요청", id="5")
        port = reg.question_port(b)
        with SubmitSpy(reg) as spy:
            assert port.reply("동료에게", id="5") == ""
        assert [c["key"] for c in spy.calls] == [a]
        assert port.reply("main 에게", id="3") == ""
        (mail,) = reg.drain_replies()
        assert mail["output"] == "main 에게" and mail["re"] == "3"
        assert reg.reply_debts(me) == []

    def test_message_settles_the_oldest_but_main_request_settles_nothing(
        self, mkreg, renderer
    ):
        reg = mkreg()
        a, b = spawn_idle(reg), spawn_idle(reg)
        me = self._two(reg, b, f"agent:{a}")
        assert reg.settle(me, f"agent:{a}").id == "3"
        assert reg.settle(me, f"agent:{a}").id == "5"
        assert reg.settle(me, f"agent:{a}") is None
        # main 쪽 (v10.56.0): 메일마다 빚(`r-…`), `agent request` 는 갚지 않는다 —
        # 갚는 도구는 `reply(id)` 하나다
        d1 = reg.note_owes("main", f"agent:{a}", "결정 1")
        d2 = reg.note_owes("main", f"agent:{a}", "결정 2")
        assert d1.id != d2.id and d1.id.startswith("r-")
        assert reg.request(a, "새 일") == ""
        assert [d.id for d in reg.reply_debts("main")] == [d1.id, d2.id]

    def test_run_end_falls_back_once_per_unpaid_request(self, mkreg, renderer):
        from agent_cli.subagent.agents_live import _NO_REPLY_LABEL

        reg = mkreg()
        a, b = spawn_idle(reg), spawn_idle(reg)
        me = self._two(reg, b, f"agent:{a}")
        with SubmitSpy(reg) as spy:
            assert reg.end_run(me, "런 요약", run="r-1", success=True) == 2
        assert len(spy.calls) == 2
        assert all(c["message"].startswith(_NO_REPLY_LABEL) for c in spy.calls)
        assert reg.reply_debts(me) == []

    def test_tail_and_nag_show_the_ids(self, mkreg, renderer):
        """꼬리와 독촉이 같은 줄 — 빚마다 id 와 갚는 도구(`reply(id)`, main·상주 공통)."""
        from agent_cli.loop.ledger import Ledger

        reg = mkreg()
        a, b = spawn_idle(reg), spawn_idle(reg)
        self._two(reg, b, f"agent:{a}")
        led = Ledger()
        led.bind_questions(reg.question_port(b))
        block = led.block()
        assert f'[3] ({a}) reply owed: "첫 요청"  → `reply` with `id` "3"' in block
        assert '`reply` with `id` "5"' in block and "## Open Items" in block
        assert "agent(mode=" not in block


# ── 런 도중 흡수 (v9.25.0) ───────────────────────────────────


def _history(ctx) -> list[dict]:
    path = ctx.session_dir / "history.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


class TestMidRunAbsorption:
    """상주 런은 턴 경계마다 받은편지함을 비운다 — 종전엔 런이 끝나야 꺼내
    11시간 런(1zfgc2) 동안 8건이, 50분 런(2an3cv) 동안 main 의 지시 2건이
    안 읽혔다. 레지스트리 쪽(``absorb_pending``)이 장부·영속을 맡고
    루프 쪽(``_absorb_inbox``)이 기록·회계를 맡는다."""

    def _busy(self, mkreg):
        """b 가 런 하나를 잡고 있는 동안(러너가 막힘) 받은편지함에 쌓인다."""
        gate = threading.Event()
        reg = mkreg(runner=make_runner(block=gate))
        a, b = spawn_idle(reg), spawn_idle(reg)
        assert reg.request(b, "첫 일감", author="main") == ""
        # busy 는 런 시작 정리(current_item·장부) **앞**에 세워진다 — 그것까지 기다린다
        assert wait_until(lambda: reg.get(b).current_item == "r-1")
        return reg, a, b, gate

    def test_registry_absorbs_requests_and_mail_into_the_running_run(
        self, mkreg, renderer
    ):
        reg, a, b, gate = self._busy(mkreg)
        tm = reg.get(b)
        me = f"agent:{b}"
        assert reg.request(b, "추가 지시", author="main") == ""
        assert (
            reg.request(b, "회신 본문", author=f"agent:{a}", expects_reply=False) == ""
        )
        assert tm.inbox.qsize() == 2

        items = reg.absorb_pending(tm)
        assert [next(iter(i)) for i in items] == ["record", "record"]
        req, mail = items[0]["record"], items[1]["record"]
        assert req["source"] == "agent_request" and req["tool"] == "agent"
        assert req["content"].startswith("── request [r-2] from main ──\n추가 지시")
        assert '(You owe main a `reply` with `id` "r-2".' in req["content"]
        assert "Your current task stays open" in req["content"]
        assert mail["source"] == "agent_mail"
        assert mail["content"] == f"[agent:{a}]: 회신 본문"
        # 장부: 시작 항목 [1] + 흡수된 요청 [2]; 회신은 빚이 아니다
        assert [d.id for d in reg.reply_debts(me)] == ["r-1", "r-2"]
        # 영속: 받은편지함 미러는 비고, 처리 중 목록에 셋 다
        assert tm.inbox.qsize() == 0 and tm.inbox_items == []
        assert [i["id"] for i in tm.in_flight] == ["r-1", "r-2", "n-3"]
        saved = json.loads((reg.session_dir / "agents.json").read_text())
        (entry,) = [e for e in saved["agents"] if e["key"] == b]
        assert [i["id"] for i in entry["in_flight"]] == ["r-1", "r-2", "n-3"]
        assert entry["inbox"] == []
        # 두 번째 흡수는 빈손 — 저장도 없다
        assert reg.absorb_pending(tm) == []
        gate.set()
        assert wait_run_done(reg, b)
        assert tm.handled == 3 and tm.in_flight == []

    def test_absorption_is_announced_and_logged(self, mkreg, renderer):
        """v10.8.2: 흡수한 항목마다 ``agent_msg direction="absorbed"`` 를 내고
        (absorbed_into = 도는 런의 id) conversation.jsonl 에도 남긴다 — 웹의
        ⏳ 대기 줄이 도는 런 블록으로 옮겨가고, resume 재생도 같은 그림이 된다.
        종전엔 배달 뒤에도 "대기" 로 남았다(67qcmb/qa 실측)."""
        reg, a, b, gate = self._busy(mkreg)
        tm = reg.get(b)
        assert reg.request(b, "추가 지시", author="main") == ""
        assert (
            reg.request(b, "회신 본문", author=f"agent:{a}", expects_reply=False) == ""
        )
        renderer.calls.clear()
        reg.absorb_pending(tm)
        absorbed = [
            kw
            for name, kw in renderer.calls
            if name == "agent_message" and kw.get("direction") == "absorbed"
        ]
        assert [(m["item"], m["absorbed_into"], m["to"]) for m in absorbed] == [
            ("r-2", "r-1", b),
            ("n-3", "r-1", b),
        ]
        assert absorbed[0]["author"] == "main" and absorbed[0]["text"] == "추가 지시"
        assert absorbed[1]["author"] == f"agent:{a}"
        logged = [
            json.loads(ln)
            for ln in (tm.home_dir / "conversation.jsonl").read_text().splitlines()
            if ln.strip()
        ]
        assert [
            (r["item"], r["absorbed_into"])
            for r in logged
            if r.get("direction") == "absorbed"
        ] == [("r-2", "r-1"), ("n-3", "r-1")]
        gate.set()

    def test_human_item_becomes_a_user_request(self, mkreg, renderer):
        reg, _a, b, gate = self._busy(mkreg)
        tm = reg.get(b)
        assert reg.request(b, "리뷰해줘", author="user:dj") == ""
        (item,) = reg.absorb_pending(tm)
        assert item == {
            "user_request": {"id": "u-2", "author": "user:dj", "text": "리뷰해줘"}
        }
        assert reg.reply_debt(f"agent:{b}", "user:dj").human  # `reply` 거부 사유
        gate.set()

    def test_shutdown_sentinel_is_left_in_place(self, mkreg, renderer):
        from agent_cli.subagent.agents_live import _SHUTDOWN

        reg, _a, b, gate = self._busy(mkreg)
        tm = reg.get(b)
        tm.inbox.put(_SHUTDOWN)
        assert reg.request(b, "뒤에 온 것", author="main") == ""
        assert reg.absorb_pending(tm) == []  # 표식에서 멈춘다 — 뒤 항목은 안 꺼낸다
        assert tm.inbox_items and tm.inbox_items[0]["text"] == "뒤에 온 것"
        left = [tm.inbox.get_nowait() for _ in range(tm.inbox.qsize())]
        assert _SHUTDOWN in left  # 되넣어져 남아 있다 (SimpleQueue 는 뒤로 간다)
        assert [i["text"] for i in left if i is not _SHUTDOWN] == ["뒤에 온 것"]
        gate.set()

    def test_absorbed_items_survive_a_restart(self, mkreg, renderer, tmp_path):
        from agent_cli.constants import RESUMED_RUN_NOTICE
        from tests.test_agents_live import _RecordingRunner

        reg, _a, b, gate = self._busy(mkreg)
        tm = reg.get(b)
        assert reg.request(b, "추가 지시", author="main") == ""
        reg.absorb_pending(tm)
        # 프로세스가 죽은 것과 같다 — 같은 폴더로 복원
        rec = _RecordingRunner()
        reg2 = make_registry(tmp_path, runner=rec)
        assert reg2.restore() == 2
        assert wait_until(lambda: len(rec.seen) >= 2)
        assert rec.seen[0] == RESUMED_RUN_NOTICE + "첫 일감"
        assert rec.seen[1] == "추가 지시"  # 흡수됐던 것도 남는다 — 별도 런으로
        gate.set()
        reg2.shutdown_all()

    # ── 실제 루프 ──

    def _loop(self, reg, b, turns):
        """상주 포트로 진짜 루프를 돈다. ``turns(n)`` 가 n 번째 호출의 본문.

        ``b`` 의 워커는 seq 1 런 안에서 막혀 있어야 한다(``_busy``) — 받은편지함을
        기다리는 워커가 있으면 흡수될 항목을 먼저 집어 가 버린다. 실제 배관에선
        워커가 바로 이 루프 안에 있다."""
        import tempfile
        from pathlib import Path

        from agent_cli.context.manager import ContextManager
        from agent_cli.loop import run_loop
        from agent_cli.providers.base import LLMResponse
        from agent_cli.subagent.resident_loop import ResidentLoop

        tm = reg.get(b)
        assert tm.state == "busy" and tm.current_item == "r-1"  # `_busy` 가 세운 런
        p = MagicMock()
        calls = []

        def call(**kw):
            calls.append(kw["messages"])
            return LLMResponse(content=turns(len(calls)))

        p.call.side_effect = call
        ctx = ContextManager(Path(tempfile.mkdtemp()) / "s", max_context_tokens=30000)
        res = run_loop(
            query="원래 일감",
            query_author_is_user=False,
            provider=p,
            capabilities=_caps(),
            model="m",
            ctx=ctx,
            max_turns=10,
            dialect="json_fc",
            ports=ResidentLoop.make_ports(
                key=b,
                message_handler=reg._make_message_handler(tm),
                questions=reg.question_port(b),
                absorb_inbox=lambda: reg.absorb_pending(tm),
                peer_roster=None,
            ),
        )
        return calls, ctx, res

    def test_request_arriving_mid_run_is_seen_next_turn_and_replied(
        self, mkreg, renderer, tmp_path
    ):
        reg, _a, b, gate = self._busy(mkreg)
        me = f"agent:{b}"

        def turns(n):
            if n == 1:
                # 이 턴이 도는 동안 main 이 하나 더 보낸다
                assert reg.request(b, "추가 지시", author="main") == ""
                return json.dumps([{"action": "read_file", "path": str(tmp_path)}])
            if n == 2:
                return json.dumps(
                    [
                        {"action": "reply", "id": "r-2", "text": "추가 답"},
                        {"action": "reply", "id": "r-1", "text": "원래 답"},
                        {"action": "complete", "result": "끝"},
                    ]
                )
            raise AssertionError(n)

        calls, ctx, res = self._loop(reg, b, turns)
        gate.set()
        assert res.output == "끝" and len(calls) == 2
        # 2턴의 프롬프트에 흡수된 요청이 관찰로 들어 있고, 꼬리가 둘 다 빚으로 든다
        second = "\n".join(str(m.get("content")) for m in calls[1])
        assert "── request [r-2] from main" in second and "추가 지시" in second
        assert "## Open Items" in second
        assert '`reply` with `id` "r-1"' in second
        assert '`reply` with `id` "r-2"' in second
        # 기록에도 남는다 — 재시작·재생이 같은 것을 본다
        recs = [r for r in _history(ctx) if r.get("source") == "agent_request"]
        assert len(recs) == 1 and recs[0]["tool"] == "agent"
        # 둘 다 main 에게 배달됐고 빚이 없다
        mail = reg.drain_replies()
        assert [m["output"] for m in mail] == ["추가 답", "원래 답"]
        assert reg.reply_debts(me) == []

    def test_human_arriving_mid_run_turns_it_into_a_user_run(
        self, mkreg, renderer, tmp_path
    ):
        """사람이 끼어들면 사람의 답(`complete`)을 먼저 보여 주고 남은 빚은
        독촉한다 — 거부가 아니다(main 과 같은 규칙, 사용자 결정)."""
        reg, _a, b, gate = self._busy(mkreg)

        def turns(n):
            if n == 1:
                assert reg.request(b, "사람 요청", author="user:dj") == ""
                return json.dumps([{"action": "read_file", "path": str(tmp_path)}])
            if n == 2:
                return json.dumps(
                    [
                        {
                            "action": "complete",
                            "result": "사람에게 답",
                            "answers": ["u-2"],
                        }
                    ]
                )
            if n == 3:
                return json.dumps(
                    [
                        {"action": "reply", "id": "r-1", "text": "main 에게 답"},
                        {"action": "complete", "result": "끝", "answers": []},
                    ]
                )
            raise AssertionError(n)

        calls, ctx, _res = self._loop(reg, b, turns)
        gate.set()
        second = "\n".join(str(m.get("content")) for m in calls[1])
        assert "## Open Items" in second and "[u-2]" in second  # 사람 요청 회계
        third = "\n".join(str(m.get("content")) for m in calls[2])
        assert (
            "your result was delivered, but 1 item is still open" in third
        )  # 배달 후 독촉
        assert "was refused" not in third
        user_recs = [r for r in _history(ctx) if r.get("request_id") == "u-2"]
        assert user_recs and user_recs[0]["content"].startswith("[user:dj]: 사람 요청")
        assert [m["output"] for m in reg.drain_replies()] == ["main 에게 답"]

    def test_nothing_pending_changes_nothing(self, mkreg, renderer, tmp_path):
        reg, _a, b, gate = self._busy(mkreg)
        calls, ctx, _res = self._loop(
            reg,
            b,
            lambda n: json.dumps(
                [
                    {"action": "reply", "id": "r-1", "text": "답"},
                    {"action": "complete", "result": "끝"},
                ]
            ),
        )
        gate.set()
        assert len(calls) == 1
        assert not [r for r in _history(ctx) if r.get("source") == "agent_request"]
