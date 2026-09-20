"""비동기 ask/answer — 질문 목록 코어 (docs/agent-ask/DESIGN.md §3, 1단계).

이 단계는 **호출자가 없다**: 도구도 강제도 아직 안 붙었고, 여기 있는 것은
레지스트리가 소유하는 질문 목록과 그 배달/짝짓기/정리뿐이다. 그래서 동작
변화가 0이고, 검증도 전부 레지스트리 표면에서 한다.

설계 리뷰가 지목한 결함이 전부 이 층에 있다:

- **B1 런 스코프** — 질문은 등록 즉시 목록에 오르지만 상대 inbox 에서는
  줄을 선다. 그 비대칭을 안 보면 상대가 꺼내지도 않은 질문에 강제·sweep 이
  걸린다 (:class:`TestRunScope`).
- **G1 사망 정리** — 세션 종료에서 지우면 resume 이 알릴 게 항상 0건이 된다
  (:class:`TestDeath`).
- **G3 회신 억제 범위** — ``asked_seq`` 없이는 다른 런의 질문까지 센다.
- **G5 사람 주소** — CLI ``user`` / 웹 ``user:{nick}`` / 뷰어마다 다른 닉.
"""

from __future__ import annotations

import json
import threading
import time

import pytest

import agent_cli.render as render_mod
from agent_cli.subagent.agents_live import (
    _MAX_QUESTION_NAGS,
    AgentRegistry,
    Question,
    QuestionPort,
    build_reply_record,
)
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
    def test_peer_question_lands_in_inbox_with_id(self, mkreg, tmp_path, renderer):
        gate = threading.Event()  # B 를 붙잡아 inbox 를 들여다본다
        reg = mkreg(runner=make_runner(block=gate))
        a, b = spawn_idle(reg), spawn_idle(reg)
        with SubmitSpy(reg) as spy:
            qid, err = reg.register_question(a, f"agent:{b}", "빌드 깨졌나요?")
        assert not err
        assert qid.startswith("q-")
        (call,) = spy.calls
        assert call["key"] == b
        assert call["question_id"] == qid
        # 산출물이 asker 에게 되돌아가면 안 된다 — 답은 answer 도구로만.
        assert call["expects_reply"] is False
        assert call["author"] == f"agent:{a}"
        assert qid in call["message"]
        gate.set()

    def test_duplicate_question_reuses_id(self, mkreg, tmp_path, renderer):
        gate = threading.Event()
        reg = mkreg(runner=make_runner(block=gate))
        a, b = spawn_idle(reg), spawn_idle(reg)
        first, _ = reg.register_question(a, f"agent:{b}", "같은 질문")
        with SubmitSpy(reg) as spy:
            second, err = reg.register_question(a, f"agent:{b}", "같은 질문")
        assert not err
        assert second == first
        assert spy.calls == []  # 두 번째는 배달하지 않는다
        gate.set()

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
        qid, err = reg.register_question(a, "main", "   ")
        assert not qid
        assert "empty" in err

    def test_dead_target_is_not_registered(self, mkreg, tmp_path, renderer):
        """배달 실패면 등록도 취소 — 남기면 아무도 못 답할 빚이 된다."""
        reg = mkreg()
        a, b = spawn_idle(reg), spawn_idle(reg)
        reg.kill(b)
        qid, err = reg.register_question(a, f"agent:{b}", "살아있나요?")
        assert not qid
        assert "dead" in err
        assert reg.questions_owed_by(f"agent:{b}") == []
        assert reg.questions_asked_in(a, 0) == []

    def test_main_question_lands_in_mailbox_carrying_id(
        self, mkreg, tmp_path, renderer
    ):
        """main 에는 inbox 가 없다 — 메일박스가 유일한 흡수 지점이고,
        ``answer(id)`` 를 하려면 레코드에 id 가 실려야 한다."""
        reg = mkreg()
        a = spawn_idle(reg)
        qid, err = reg.register_question(a, "main", "이 파일 덮어쓸까요?")
        assert not err
        (reply,) = reg.drain_replies()
        assert reply["kind"] == "question"
        assert reply["id"] == qid
        assert reply["output"] == "이 파일 덮어쓸까요?"

    @pytest.mark.parametrize("addr", ["user", "user:bob"])
    def test_human_question_is_not_delivered_only_listed(
        self, mkreg, tmp_path, renderer, addr
    ):
        """사람에겐 배달할 inbox 가 없다 — ❓ 트레이가 표면이다(§3.6)."""
        reg = mkreg()
        a = spawn_idle(reg)
        with SubmitSpy(reg) as spy:
            qid, err = reg.register_question(a, addr, "배포해도 되나요?")
        assert not err
        assert spy.calls == []
        assert reg.drain_replies() == []
        assert [q.id for q in reg.open_human_questions()] == [qid]
        # 강제 대상이 아니다 — 배달이 없으니 owed 에 영영 안 뜬다.
        assert reg.questions_owed_by(addr) == []


# ── B1: 런 스코프 ───────────────────────────────


class TestRunScope:
    def test_queued_question_is_not_owed_until_dequeued(
        self, mkreg, tmp_path, renderer
    ):
        """**바쁜 peer 에게 묻기.** 질문이 B 의 큐 뒤에 서 있는 동안은
        B 가 진 빚이 아니다 — 여기서 owed 로 세면 B 의 *앞* 런이 읽지도
        않은 질문으로 독촉을 받고, 상한이 그걸로 타 asker 에게 거짓
        무응답이 간다."""
        job, qg = threading.Event(), threading.Event()

        def runner(query, ctx, **kw):
            (qg if "question q-" in query else job).wait(5)
            return _FakeLoopResult(output="ok"), 0.01

        reg = mkreg()
        reg._runner = runner
        a, b = spawn_idle(reg), spawn_idle(reg)
        reg.request(b, "앞선 일감")  # B 를 붙잡는다
        assert wait_until(lambda: reg.get(b).state == "busy")

        qid, err = reg.register_question(a, f"agent:{b}", "이거 맞나요?")
        assert not err
        assert reg.get(b).inbox.qsize() == 1  # 큐에 들어는 갔다
        assert reg.questions_owed_by(f"agent:{b}") == []  # 그러나 빚은 아니다

        job.set()  # B 가 앞 일감을 끝내고 질문 항목을 꺼내 **그 런 안에서** 멈춘다
        assert wait_until(lambda: reg.questions_owed_by(f"agent:{b}") != [])
        (owed,) = reg.questions_owed_by(f"agent:{b}")
        assert owed.id == qid
        assert owed.delivered_seq is not None
        qg.set()

    def test_asked_in_is_scoped_to_the_run(self, mkreg, tmp_path, renderer):
        """G3: 다른 런에서 걸어 둔(영영 열릴 수 있는) 사람 질문이 이후
        모든 런의 회신을 막으면 안 된다."""
        reg = mkreg()
        a = spawn_idle(reg)
        reg.get(a).current_seq = 3
        reg.register_question(a, "user:bob", "run 3 의 질문")
        reg.get(a).current_seq = 5
        assert len(reg.questions_asked_in(a, 3)) == 1
        assert reg.questions_asked_in(a, 5) == []


# ── 짝짓기 ──────────────────────────────────────


class TestAnswer:
    def test_only_the_addressee_may_answer(self, mkreg, tmp_path, renderer):
        reg = mkreg()
        a = spawn_idle(reg)
        qid, _ = reg.register_question(a, "main", "질문")
        assert "addressed to main" in reg.answer_question(qid, "답", by="agent:zz")
        assert "addressed to main" in reg.answer_question(qid, "답", by="user:bob")
        assert reg.answer_question(qid, "답", by="main") == ""

    @pytest.mark.parametrize("answerer", ["user", "user:bob", "user:carol"])
    def test_any_human_may_answer_a_human_question(
        self, mkreg, tmp_path, renderer, answerer
    ):
        """G5: CLI 는 ``user``, 웹은 ``user:{nick}``, 뷰어마다 닉이 다르다.
        문자열 동치로 검사하면 두 번째 뷰어의 트레이 답이 거부된다."""
        reg = mkreg()
        a = spawn_idle(reg)
        qid, _ = reg.register_question(a, "user:bob", "배포할까요?")
        assert reg.answer_question(qid, "네", by=answerer) == ""
        assert reg.open_human_questions() == []

    def test_agent_cannot_answer_a_human_question(self, mkreg, tmp_path, renderer):
        reg = mkreg()
        a = spawn_idle(reg)
        qid, _ = reg.register_question(a, "user:bob", "배포할까요?")
        assert "operator" in reg.answer_question(qid, "네", by="agent:x")

    def test_claim_is_atomic(self, mkreg, tmp_path, renderer):
        reg = mkreg()
        a = spawn_idle(reg)
        qid, _ = reg.register_question(a, "main", "질문")
        assert reg.answer_question(qid, "첫 답", by="main") == ""
        err = reg.answer_question(qid, "둘째 답", by="main")
        assert "already-answered" in err

    def test_empty_answer_rejected(self, mkreg, tmp_path, renderer):
        reg = mkreg()
        a = spawn_idle(reg)
        qid, _ = reg.register_question(a, "main", "질문")
        assert "empty" in reg.answer_question(qid, "  ", by="main")
        assert reg.questions_owed_by("main") == []  # 미배달이라 owed 는 아님
        assert len(reg.questions_asked_in(a, 0)) == 1  # 그러나 열려 있다

    def test_answer_routes_back_to_the_original_requester(
        self, mkreg, tmp_path, renderer
    ):
        """불변식(§0): 질문의 주소가 곧 원 요청자다. ``author=q.target`` +
        ``expects_reply=True`` 면 기존 회신 라우팅이 제자리로 보낸다."""
        gate = threading.Event()
        reg = mkreg(runner=make_runner(block=gate))
        a, b = spawn_idle(reg), spawn_idle(reg)
        reg.get(a).current_author = f"agent:{b}"  # B 가 시킨 일을 하는 중
        qid, err = reg.register_question(a, f"agent:{b}", "어느 쪽인가요?")
        assert not err
        with SubmitSpy(reg) as spy:
            assert reg.answer_question(qid, "왼쪽", by=f"agent:{b}") == ""
        (call,) = spy.calls
        assert call["key"] == a  # 답은 물어본 쪽으로
        assert call["author"] == f"agent:{b}"  # 주소 = 원 요청자
        assert call["expects_reply"] is True  # 답 런의 결과가 되돌아간다
        assert "왼쪽" in call["message"]
        assert "어느 쪽인가요?" in call["message"]  # 원 질문이 딸려간다
        gate.set()

    def test_answer_to_main_asker_uses_the_mailbox(self, mkreg, tmp_path, renderer):
        """설계 3판 §3.3 이 빠뜨린 경우 — ``submit`` 의 대상은 상주
        에이전트뿐이라 main 이 asker 면 메일박스로 가야 한다."""
        reg = mkreg()
        b = spawn_idle(reg)
        qid, err = reg.register_question("main", f"agent:{b}", "상태 어때요?")
        assert not err
        reg.drain_replies()
        assert reg.answer_question(qid, "초록", by=f"agent:{b}") == ""
        (reply,) = reg.drain_replies()
        assert reply["kind"] == "answer"
        assert reply["id"] == qid
        assert "초록" in reply["output"]
        rec = build_reply_record(reply, registry=reg)
        assert rec["source"] == "agent_answer"
        assert "초록" in rec["content"]


class TestClose:
    def test_close_delivers_the_reason_to_the_asker(self, mkreg, tmp_path, renderer):
        reg = mkreg()
        b = spawn_idle(reg)
        qid, _ = reg.register_question("main", f"agent:{b}", "질문")
        reg.drain_replies()
        assert reg.close_question(qid, "상한 초과") is not None
        (reply,) = reg.drain_replies()
        assert "상한 초과" in reply["output"]
        assert reg.close_question(qid, "다시") is None  # 멱등

    def test_nag_counter(self, mkreg, tmp_path, renderer):
        reg = mkreg()
        a = spawn_idle(reg)
        qid, _ = reg.register_question(a, "main", "질문")
        assert reg.bump_question_nag(qid) == 1
        assert reg.bump_question_nag(qid) == 2
        assert reg.bump_question_nag("q-nope") == 0


# ── G1: 사망 정리 ───────────────────────────────


class TestDeath:
    def test_kill_purges_both_directions(self, mkreg, tmp_path, renderer):
        gate = threading.Event()
        gate.set()
        reg = mkreg(runner=make_runner(block=gate))
        a, b = spawn_idle(reg), spawn_idle(reg)
        # B 앞으로 온 질문 하나, B 가 건 질문 하나.
        incoming, _ = reg.register_question(a, f"agent:{b}", "B 에게 묻는다")
        reg.get(b).current_author = "main"
        outgoing, _ = reg.register_question(b, "main", "B 가 묻는다")
        reg.drain_replies()

        reg.kill(b)
        # 앞으로 온 것: asker 가 풀려야 한다 — **사유가 실제로 배달**된다.
        # 질문만 지우면 A 는 아무것도 못 받고 영원히 기다린다.
        assert wait_until(lambda: reg.questions_asked_in(a, 0) == [])
        assert wait_until(
            lambda: any(
                "terminated before answering" in str(c[1].get("text"))
                and c[1].get("key") == a
                for c in renderer.named("agent_message")
            )
        )
        # 그가 건 것: 폐기 — 남기면 답하려는 쪽이 dead 에러를 받고 재시도한다.
        assert reg.answer_question(outgoing, "답", by="main").startswith("unknown")
        assert reg.answer_question(incoming, "답", by=f"agent:{b}").startswith(
            "unknown"
        )

    def test_shutdown_all_keeps_questions(self, mkreg, tmp_path, renderer):
        """G1: ``_worker`` finally 는 세션 종료에서도 돈다. 거기서 지우면
        직후의 ``_save_state`` 가 빈 목록을 저장해 resume 이 알릴 열린
        질문이 **항상 0건**이 된다."""
        reg = mkreg()
        a = spawn_idle(reg)
        qid, _ = reg.register_question(a, "user:bob", "살아남아야 한다")
        reg.shutdown_all()
        assert [q.id for q in reg.open_human_questions()] == [qid]
        saved = json.loads((tmp_path / "agents.json").read_text(encoding="utf-8"))
        assert [q["id"] for q in saved["questions"]] == [qid]


# ── 영속 ────────────────────────────────────────


class TestPersistence:
    """§3.9 — resume 은 질문을 **되살린다**. ctx 가 통째로 복원되므로
    나중에 도착한 답도 평소처럼 새 런으로 처리된다. 되살리지 못하는 것은
    asker 나 target 이 돌아오지 않은 것뿐이다."""

    def test_human_question_comes_back_to_the_tray(self, mkreg, tmp_path, renderer):
        reg = mkreg()
        a = spawn_idle(reg)
        qid, _ = reg.register_question(a, "user:bob", "열린 채 종료")
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

    def test_asked_seq_is_reset_so_it_cannot_gag_a_new_run(
        self, mkreg, tmp_path, renderer
    ):
        """``asked_seq`` 는 세션마다 의미가 다르다. 그대로 두면 새 세션의
        어떤 런과 우연히 같아져 §3.7 억제가 엉뚱하게 걸릴 수 있다."""
        reg = mkreg()
        a = spawn_idle(reg)
        reg.get(a).current_seq = 4
        qid, _ = reg.register_question(a, "user:bob", "질문")
        assert reg._questions[qid].asked_seq == 4
        reg.shutdown_all()

        fresh = AgentRegistry(tmp_path, runtime={"model": "m"}, runner=make_runner())
        fresh.restore()
        assert fresh._questions[qid].asked_seq == 0  # seq 는 1부터 — 안 겹친다
        fresh.shutdown_all()

    def test_delivered_debt_survives_and_is_kicked(self, mkreg, tmp_path, renderer):
        """**kick.** 독촉은 런이 끝나는 자리에 걸린다 — resume 직후 새
        일감이 안 오면 끝나는 런이 없어 빚이 영원히 잠든다. 한 번 깨워
        주면 그 독촉 항목이 런을 만들고 이후는 평소 흐름이다."""
        reg = mkreg()
        a, b = spawn_idle(reg), spawn_idle(reg)
        q = Question(
            id="q-debt1",
            asker=a,
            target=f"agent:{b}",
            text="배달됐던 빚",
            delivered_seq=2,
        )
        reg._questions[q.id] = q
        reg._save_state()
        reg.shutdown_all()

        seen = []
        hold = threading.Event()

        def runner(query, ctx, **kw):
            seen.append(query)
            # 독촉 런을 붙잡아 둔다. 놓아 두면 그 런이 끝나면서 **자기도**
            # `remind_owed` 를 불러(설계대로의 연쇄) nags 가 2가 되고,
            # "두 번 깨우지 않는다" 단언이 CI 에서 깨진다.
            if "(reminder)" in query:
                hold.wait(5)
            return _FakeLoopResult(output="ok"), 0.01

        fresh = AgentRegistry(tmp_path, runtime={"model": "m"}, runner=runner)
        fresh.restore()
        assert q.id in fresh._questions
        assert fresh._questions[q.id].delivered_seq is not None  # 빚이 유지된다
        assert wait_until(
            lambda: fresh.get(b).handled >= 0 and any("(reminder)" in x for x in seen)
        )  # 깨웠다 — 독촉 항목이 런을 만들었다
        assert fresh._questions[q.id].nags == 1  # 두 번 깨우지 않는다
        # 이미 읽은 질문을 **다시 배달하지는 않는다** — 상대 ctx 에 남아
        # 있고, 재배달하면 같은 질문을 두 번 묻는 꼴이다.
        assert not [x for x in seen if "[question q-" in x]
        assert [x for x in seen if "(reminder)" in x]
        hold.set()
        fresh.shutdown_all()

    def test_main_targeted_debt_is_kicked_too(self, mkreg, tmp_path, renderer):
        """kick 을 ``agent:`` 로만 한정하면 main 의 빚은 resume 후 영영
        잠든다 — main 의 독촉은 런 끝에서만 걸리는데, main 이 그 질문을
        모르고 있으면 끝날 런도 없다."""
        reg = mkreg()
        a = spawn_idle(reg)
        q = Question(
            id="q-main1",
            asker=a,
            target="main",
            text="main 앞 배달된 빚",
            delivered_seq=0,
        )
        reg._questions[q.id] = q
        reg._save_state()
        reg.shutdown_all()

        fresh = AgentRegistry(tmp_path, runtime={"model": "m"}, runner=make_runner())
        fresh.restore()
        assert fresh._questions[q.id].nags == 1
        rec = next(r for r in fresh.drain_replies() if r.get("kind") == "reminder")
        assert q.id in rec["output"]
        fresh.shutdown_all()

    def test_undelivered_peer_question_is_redelivered(self, mkreg, tmp_path, renderer):
        """inbox 는 ``SimpleQueue`` 라 영속 대상이 아니다 — 아직 안 꺼낸
        질문은 항목으로만 존재했으므로 통째로 증발한다. 되살려 놓기만
        하면 영영 안 꺼내지고 독촉도 안 가 영구 미결이 된다."""
        reg = mkreg()
        a, b = spawn_idle(reg), spawn_idle(reg)
        q = Question(id="q-undel", asker=a, target=f"agent:{b}", text="못 꺼낸 질문")
        reg._questions[q.id] = q
        reg._save_state()
        reg.shutdown_all()

        seen = []

        def runner(query, ctx, **kw):
            seen.append(query)
            return _FakeLoopResult(output="ok"), 0.01

        fresh = AgentRegistry(tmp_path, runtime={"model": "m"}, runner=runner)
        fresh.restore()
        # **질문 항목**으로 다시 배달돼야 한다 — id 만 찾으면 독촉 문구에도
        # 들어 있어, 재배달을 지우고 kick 만 남겨도 통과한다(실제로 그랬다).
        assert wait_until(
            lambda: any("[question q-undel" in x for x in seen), timeout=5.0
        )
        assert wait_until(lambda: fresh._questions["q-undel"].delivered_seq is not None)
        # 미배달이었으므로 kick 대상이 아니다 — 배달과 독촉을 겹쳐 받으면
        # 런 하나가 낭비되고 상한이 일찍 닳는다.
        assert fresh._questions["q-undel"].nags == 0
        assert not [x for x in seen if "(reminder)" in x]
        fresh.shutdown_all()

    def test_question_is_dropped_when_its_target_did_not_come_back(
        self, mkreg, tmp_path, renderer
    ):
        """asker 나 target 이 안 돌아오면 아무도 답할 수 없거나 답을 받을
        데가 없다 — 되살리면 영구 미결로 남는다.

        정상 경로에서는 ``kill``/crash 가 **즉시** 양방향 정리를 하므로
        (§3.8) 이 상태가 잘 안 만들어진다. 그래서 파일을 직접 손봐
        안전망 자체를 잰다 — 손상·수동 편집·버전 엇갈림에서 나올 수 있다.
        """
        reg = mkreg()
        a = spawn_idle(reg)
        reg._save_state()
        reg.shutdown_all()

        path = tmp_path / "agents.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        data["questions"] = [
            {  # target 이 매니페스트에 없다
                "id": "q-orphan",
                "asker": a,
                "target": "agent:agt-gone",
                "text": "돌아오지 않은 상대에게",
                "asked_at": 1.0,
                "asked_seq": 1,
                "delivered_seq": None,
                "nags": 0,
            },
            {  # asker 가 매니페스트에 없다
                "id": "q-orphan2",
                "asker": "agt-gone2",
                "target": "main",
                "text": "돌아오지 않은 쪽이 물었다",
                "asked_at": 1.0,
                "asked_seq": 1,
                "delivered_seq": 3,
                "nags": 0,
            },
        ]
        path.write_text(json.dumps(data), encoding="utf-8")

        fresh = AgentRegistry(tmp_path, runtime={"model": "m"}, runner=make_runner())
        fresh.restore()
        assert fresh._questions == {}
        assert fresh.stale_questions == 2
        fresh.shutdown_all()

    def test_delivered_seq_zero_survives_round_trip(self, mkreg, tmp_path, renderer):
        """main 의 배달 마킹은 ``0`` 이다(런 seq 가 없다). ``or``/truthy 로
        읽으면 0 이 None 이 되어 **main 의 빚이 resume 마다 사라진다** —
        ``is None`` 관용구가 그걸 막는 유일한 장치다."""
        q = Question.from_dict(
            {
                "id": "q-zero",
                "asker": "agt-a",
                "target": "main",
                "text": "t",
                "delivered_seq": 0,
            }
        )
        assert q is not None
        assert q.delivered_seq == 0  # None 이 아니다 → 여전히 빚이다

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

    def test_async_question_record_is_not_marked_stale(self, mkreg, tmp_path, renderer):
        """``stale`` 은 블로킹 경로 전용이다 — 거기선 재시작으로 대기
        슬롯이 사라져 "BLOCKED" 가 거짓이 된다. 비동기 질문은 목록째
        되살아나 답이 정상 처리되므로, 마킹하면 그게 거짓말이 된다."""
        reg = mkreg()
        a = spawn_idle(reg)
        qid, _ = reg.register_question(a, "main", "main 에게")  # pending 에 실린다
        reg.shutdown_all()

        fresh = AgentRegistry(tmp_path, runtime={"model": "m"}, runner=make_runner())
        fresh.restore()
        rec = next(r for r in fresh.drain_replies() if r.get("kind") == "question")
        assert rec["id"] == qid
        assert rec.get("stale") is not True
        assert "STALE" not in build_reply_record(rec)["content"]
        assert qid in fresh._questions  # 되살아났다 — 답할 수 있다
        fresh.shutdown_all()

    def test_fresh_registry_reports_zero(self, mkreg, tmp_path, renderer):
        reg = AgentRegistry(tmp_path, runtime={"model": "m"}, runner=make_runner())
        reg.restore()
        assert reg.stale_questions == 0
        assert reg._questions == {}


# ── 독촉 런 (§3.4) ──────────────────────────────


class TestReminder:
    def test_result_goes_out_immediately_and_reminder_follows(
        self, mkreg, tmp_path, renderer
    ):
        """**complete 을 붙잡지 않는다.** 붙잡으면 그 런에 일을 시킨 쪽이
        자기와 무관한 질문이 풀릴 때까지 결과를 못 받는다 — 없애려던 결합이
        그대로 돌아온다. 결과는 그대로 나가고, 남은 빚은 새 런으로 온다.

        세 게이트로 런 경계를 고정한다 — 독촉 런까지 멈춰 세워야 "질문이
        아직 열려 있는 시점"의 상태를 경합 없이 관찰할 수 있다.
        """
        job, qg, rg = threading.Event(), threading.Event(), threading.Event()

        def runner(query, ctx, **kw):
            if "(reminder)" in query:
                rg.wait(5)
            elif "question q-" in query:
                qg.wait(5)
            else:
                job.wait(5)
            return _FakeLoopResult(output="ok"), 0.01

        reg = mkreg()
        reg._runner = runner
        a, b = spawn_idle(reg), spawn_idle(reg)
        reg.request(b, "main 이 시킨 일")  # seq 1
        assert wait_until(lambda: reg.get(b).state == "busy")
        qid, _ = reg.register_question(a, f"agent:{b}", "이거 맞나요?")  # seq 2

        job.set()
        # seq 1 의 결과는 질문과 **무관하게** 곧바로 main 에게 간다.
        assert wait_until(
            lambda: any(
                r.get("kind") == "reply" and r.get("seq") == 1
                for r in reg.drain_replies()
            )
        )

        qg.set()  # 질문 런이 답 없이 끝나면 독촉이 새 항목으로 온다

        def reminders():
            return [
                c[1]
                for c in renderer.named("agent_message")
                if c[1].get("key") == b
                and c[1].get("direction") == "in"
                and "(reminder)" in str(c[1].get("text"))
            ]

        assert wait_until(lambda: bool(reminders()))
        note = reminders()[0]
        # 발신자는 **기다리는 쪽** — addr 을 쓰면 "자기가 자기에게" 가 된다.
        assert note["author"] == f"agent:{a}"
        assert qid in note["text"]  # 어느 질문인지 지목한다
        assert f"from {a}" in note["text"]  # 누가 물었는지도

        # 독촉 런이 도는 동안(= 질문이 아직 열린 동안) A 는 아무것도 못 받는다.
        # 독촉이 expects_reply 면 그 산출물이 A 의 inbox 로 재주입돼 A 가
        # 답을 받은 줄 안다 — 청탁받은 일이 아니므로 어디로도 가면 안 된다.
        assert qid in reg._questions
        assert reg.get(a).handled == 0
        assert reg.get(a).inbox.qsize() == 0
        assert [r for r in reg.drain_replies() if r.get("kind") == "reply"] == []
        rg.set()

    def test_answered_in_run_gets_no_reminder(self, mkreg, tmp_path, renderer):
        answered = threading.Event()

        def runner(query, ctx, **kw):
            if "question q-" in query:
                qid = query.split("question ")[1].split(" ")[0]
                assert reg.answer_question(qid, "네", by=f"agent:{b}") == ""
                answered.set()
            return _FakeLoopResult(output="ok"), 0.01

        reg = mkreg()
        reg._runner = runner
        a, b = spawn_idle(reg), spawn_idle(reg)
        reg.register_question(a, f"agent:{b}", "답할게요")
        assert answered.wait(5)
        assert wait_until(lambda: reg.get(b).state == "idle")
        assert reg._questions == {}
        assert not [
            c
            for c in renderer.named("agent_message")
            if "reminder" in str(c[1].get("text"))
        ]

    def test_reminder_cap_closes_and_tells_the_asker(self, mkreg, tmp_path, renderer):
        """모델이 끝내 안 답해도 asker 가 영원히 기다리지는 않는다."""
        reg = mkreg()
        # 대상을 **워커 없는 키**로 둔다. 살아 있는 상대를 쓰면 독촉 항목을
        # 그 워커가 곧바로 처리하고, 그 런의 끝에서 **자기도 remind_owed 를
        # 부른다**(설계대로의 연쇄). 그러면 이 동기 호출들과 경합해 nags 가
        # 앞서 나간다 — CI(Linux)에서 실제로 깨졌다. 여기서 재는 것은 배달이
        # 아니라 bump/상한/닫기 정책이므로 대상은 없어도 된다
        # (``request`` 는 unknown 으로 실패하고 ``remind_owed`` 는 무시한다).
        ghost = "agent:agt-noworker"
        q = Question(
            id="q-cap01", asker="main", target=ghost, text="질문", delivered_seq=7
        )
        reg._questions[q.id] = q
        for _ in range(_MAX_QUESTION_NAGS):
            assert reg.remind_owed(ghost) == 1
        assert reg.remind_owed(ghost) == 0  # 상한 — 닫힌다
        assert q.id not in reg._questions
        assert any(
            "repeated reminders" in (r.get("output") or "") for r in reg.drain_replies()
        )

    def test_reminder_needs_delivery_not_just_registration(
        self, mkreg, tmp_path, renderer
    ):
        """B1: 큐 뒤에 서 있는(아직 안 꺼낸) 질문으로는 독촉하지 않는다.

        스코프 축은 **배달 여부**이지 seq 동치가 아니다 — seq 로 좁히면
        독촉 런의 seq 가 달라 두 번째 독촉이 영영 안 나가고 상한조차
        안 걸린다(아래 ``test_reminder_repeats_until_answered`` 가 반대쪽).
        """
        reg = mkreg()
        # 대상을 **워커 없는 키**로 둔다. 살아 있는 상대를 쓰면 독촉 항목을
        # 그 워커가 곧바로 처리하고, 그 런의 끝에서 **자기도 remind_owed 를
        # 부른다**(설계대로의 연쇄). 그러면 이 동기 호출들과 경합해 nags 가
        # 앞서 나간다 — CI(Linux)에서 실제로 깨졌다. 여기서 재는 것은 배달이
        # 아니라 bump/상한/닫기 정책이므로 대상은 없어도 된다
        # (``request`` 는 unknown 으로 실패하고 ``remind_owed`` 는 무시한다).
        ghost = "agent:agt-noworker"
        q = Question(id="q-scp01", asker="main", target=ghost, text="질문")
        reg._questions[q.id] = q
        assert reg.remind_owed(ghost) == 0  # 아직 안 꺼냈다
        reg.mark_question_delivered(q.id, 4)
        assert reg.remind_owed(ghost) == 1

    def test_reminder_repeats_until_answered(self, mkreg, tmp_path, renderer):
        """한 번 읽은 빚은 **답할 때까지 매 런 끝에** 다시 온다 — 사용자의
        'post turn prompt 로 계속 넣어준다'가 이것이다."""
        reg = mkreg()
        # 대상을 **워커 없는 키**로 둔다. 살아 있는 상대를 쓰면 독촉 항목을
        # 그 워커가 곧바로 처리하고, 그 런의 끝에서 **자기도 remind_owed 를
        # 부른다**(설계대로의 연쇄). 그러면 이 동기 호출들과 경합해 nags 가
        # 앞서 나간다 — CI(Linux)에서 실제로 깨졌다. 여기서 재는 것은 배달이
        # 아니라 bump/상한/닫기 정책이므로 대상은 없어도 된다
        # (``request`` 는 unknown 으로 실패하고 ``remind_owed`` 는 무시한다).
        ghost = "agent:agt-noworker"
        q = Question(
            id="q-rep01", asker="main", target=ghost, text="질문", delivered_seq=4
        )
        reg._questions[q.id] = q
        assert reg.remind_owed(ghost) == 1  # 런 A 끝
        assert reg.remind_owed(ghost) == 1  # 런 B 끝 — seq 가 달라도 계속
        assert reg.answer_question(q.id, "답", by=ghost) == ""
        assert reg.remind_owed(ghost) == 0  # 답했으면 그친다


# ── 사람 알림 · 회신 억제 · 표면 ────────────────


class TestSurfaces:
    def test_open_human_question_is_reported_in_the_result(
        self, mkreg, tmp_path, renderer
    ):
        """사람에겐 강제를 못 건다(우리 루프가 아니다) — 대신 결과에 실어
        "내가 답을 안 해서 끝났구나"를 알린다."""
        reg = mkreg()

        def runner(query, ctx, **kw):
            reg.register_question(b, "user:bob", "덮어쓸까요?")
            return _FakeLoopResult(output="정리 완료"), 0.01

        reg._runner = runner
        b = spawn_idle(reg)
        reg.request(b, "일감", author="user:bob")
        assert wait_until(lambda: reg.get(b).state == "idle")
        out = [
            c for c in renderer.named("agent_message") if c[1].get("direction") == "out"
        ][-1][1]["text"]
        assert "정리 완료" in out
        assert "답을 받지 못한 질문 1건" in out
        assert "덮어쓸까요?" in out

    def test_reply_is_withheld_while_this_runs_question_is_open(
        self, mkreg, tmp_path, renderer
    ):
        """§3.7: 부분 결과로 회신하면 나중 답 런이 같은 요청에 두 번째
        회신을 만든다. 요청자는 질문을 이미 받았으므로 깜깜하지 않다."""
        reg = mkreg()

        def runner(query, ctx, **kw):
            if "일감" in query:
                reg.register_question(b, "main", "어느 쪽인가요?")
            return _FakeLoopResult(output="부분 결과"), 0.01

        reg._runner = runner
        b = spawn_idle(reg)
        reg.request(b, "일감")
        assert wait_until(lambda: reg.get(b).state == "idle")
        kinds = [r["kind"] for r in reg.drain_replies()]
        assert "question" in kinds
        assert "reply" not in kinds  # 답 런의 회신이 진짜 회신이다
        # 억제는 **재주입만** 막는다 — 창·로그·persist 는 그대로 돌아야
        # 사용자가 이 에이전트가 한 일을 본다.
        outs = [
            c[1]
            for c in renderer.named("agent_message")
            if c[1].get("direction") == "out" and c[1].get("key") == b
        ]
        assert len(outs) == 1
        assert "부분 결과" in outs[0]["text"]
        assert (tmp_path / "agents" / b / "replies" / "reply-1.md").is_file()

    def test_roster_carries_open_questions(self, mkreg, tmp_path, renderer):
        reg = mkreg()
        a = spawn_idle(reg)
        qid, _ = reg.register_question(a, "user:bob", "배포?")
        row = next(r for r in reg.roster_snapshot() if r["key"] == a)
        assert [q["id"] for q in row["open_questions"]] == [qid]
        assert row["open_questions"][0]["to"] == "user:bob"

    def test_registering_notifies_the_roster(self, mkreg, tmp_path, renderer):
        """트레이는 로스터의 ``open_questions`` 를 읽는다 — 알리지 않으면
        사람 주소 질문이 다음 브로드캐스트까지 화면에 안 뜬다."""
        reg = mkreg()
        a = spawn_idle(reg)
        before = len(renderer.named("agent_roster"))
        qid, _ = reg.register_question(a, "user:bob", "배포?")
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
        reg.register_question(a, "user:bob", "배포?")
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
        qid, _ = reg.register_question(a, "user:bob", "덮어쓸까요?")
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


# ── main 독촉 (§3.4 — 에이전트와 같은 정책, 다른 배달) ──


class TestMainReminder:
    """main 앞 질문의 출처는 **사람이 아니라 에이전트**다: 질문의 주소는
    ask 시점의 ``current_author`` 이고, 그게 ``main`` 이라는 건 main 이
    시킨 일을 하던 에이전트가 되묻는 경우뿐이다."""

    def test_drain_marks_delivered_so_main_can_be_reminded(self, mkreg, renderer):
        """main 에게는 ``drain_replies`` 가 배달 시점이다. 안 찍으면
        ``questions_owed_by(\"main\")`` 이 영영 비어 독촉도 상한도 없고,
        §3.7 로 보류된 회신이 영구 정지한다."""
        reg = mkreg()
        a = spawn_idle(reg)
        qid, _ = reg.register_question(a, "main", "API v1/v2 중 어느 쪽?")
        assert reg.questions_owed_by("main") == []  # 아직 메일박스에 있다
        assert reg.remind_owed("main") == 0

        (rec,) = reg.drain_replies()
        assert rec["kind"] == "question" and rec["id"] == qid
        owed = reg.questions_owed_by("main")
        assert [q.id for q in owed] == [qid]

    def test_main_reminder_goes_to_the_mailbox_as_an_observation(self, mkreg, renderer):
        reg = mkreg()
        a = spawn_idle(reg)
        qid, _ = reg.register_question(a, "main", "어느 쪽?")
        reg.drain_replies()  # 배달 마킹

        assert reg.remind_owed("main") == 1
        (rec,) = reg.drain_replies()
        assert rec["kind"] == "reminder"
        assert qid in rec["output"]
        assert f"from {a}" in rec["output"]
        obs = build_reply_record(rec, registry=reg)
        assert obs["source"] == "agent_reminder"
        assert qid in obs["content"]

    def test_main_answering_stops_the_reminder(self, mkreg, renderer):
        reg = mkreg()
        a = spawn_idle(reg)
        qid, _ = reg.register_question(a, "main", "어느 쪽?")
        reg.drain_replies()
        assert reg.remind_owed("main") == 1
        assert reg.answer_question(qid, "v2", by="main") == ""
        assert reg.remind_owed("main") == 0

    def test_main_cap_closes_and_tells_the_asker(self, mkreg, renderer):
        """정책은 에이전트와 **같은 함수**다 — 상한도 같이 걸린다."""
        reg = mkreg()
        a = spawn_idle(reg)
        qid, _ = reg.register_question(a, "main", "어느 쪽?")
        reg.drain_replies()
        for _ in range(_MAX_QUESTION_NAGS):
            assert reg.remind_owed("main") == 1
        assert reg.remind_owed("main") == 0
        assert qid not in reg._questions
        assert wait_until(
            lambda: any(
                "repeated reminders" in str(c[1].get("text"))
                for c in renderer.named("agent_message")
                if c[1].get("key") == a
            )
        )


class TestReminderRace:
    def test_answered_between_snapshot_and_bump_is_not_reminded(self, mkreg, renderer):
        """``remind_owed`` 는 락 밖에서 스냅샷을 잡고 bump 한다. 그 사이
        답이 들어오면 ``bump_question_nag`` 가 0 을 돌려주는데, 0 을
        안 걸러내면 ``0 > 6`` 이 거짓이라 **이미 답한 질문으로 독촉**한다.
        살아 있는 질문의 nags 는 언제나 ≥1 이라 0 은 모호하지 않다."""
        reg = mkreg()
        a = spawn_idle(reg)
        ghost = Question(
            id="q-gone1",
            asker=a,
            target="main",
            text="이미 답한 질문",
            delivered_seq=0,
        )
        reg.questions_owed_by = lambda addr: [ghost]  # 스냅샷만 낡았다
        assert reg.remind_owed("main") == 0
        assert reg.drain_replies() == []


# ── §3.7 회신 신선도 ────────────────────────────


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
    """질문을 건 런은 요청자 채널로 **재주입하지 않는다**.

    보장하는 성질은 "회신이 정확히 한 번" 이 아니라 **"회신이 낡지
    않는다"** 다. 부분 회신은 *답이 존재하기 전에* 만들어졌는데 *답을 보낸
    뒤에* 도착해서, 요청자가 자기 답이 반영된 상태로 오독한다 — peer 면
    inbox 항목 1개 = 런 1개라 잘못된 하위 작업이 실제로 돌아간다.
    """

    def test_answer_arriving_before_run_end_still_yields_one_reply(
        self, mkreg, renderer
    ):
        """**이 판정이 바뀐 이유.** 비동기라 답은 보통 묻던 런이 끝나기
        *전에* 온다. "아직 열려 있나" 로 판정하면 그때 질문이 이미 닫혀
        있어 억제가 안 걸리고 낡은 부분 회신이 나간다 — 막으려던 상황이
        정상 경로다."""
        hold = threading.Event()
        reg = mkreg()
        box = {}

        def runner(query, ctx, **kw):
            if "일감" in query:
                box["qid"], err = reg.register_question(b, "main", "어느 쪽?")
                assert not err
                hold.wait(5)  # 이 런이 끝나기 전에 답이 들어온다
                return _FakeLoopResult(output="부분 결과"), 0.01
            return _FakeLoopResult(output="완성"), 0.01

        reg._runner = runner
        b = spawn_idle(reg)
        rep = Replies(reg)
        reg.request(b, "일감")  # seq 1
        assert wait_until(lambda: "qid" in box)
        assert wait_until(lambda: any(r.get("kind") == "question" for r in rep.all()))

        assert reg.answer_question(box["qid"], "왼쪽", by="main") == ""
        hold.set()  # 이제 런 1 이 끝난다 — 질문은 이미 닫혀 있다

        assert wait_until(lambda: len(rep.kinds()) == 1, timeout=5.0)
        (only,) = rep.kinds()
        assert only["output"] == "완성"  # 낡은 "부분 결과" 가 아니다
        assert only["seq"] == 2  # 답 런의 회신
        time.sleep(0.15)
        assert len(rep.kinds()) == 1  # 뒤늦게 하나 더 오지 않는다

    def test_answer_arriving_after_run_end_still_yields_one_reply(
        self, mkreg, renderer
    ):
        """반대 순서(종전 조건이 잡던 경우)도 그대로여야 한다."""
        reg = mkreg()
        box = {}

        def runner(query, ctx, **kw):
            if "일감" in query:
                box["qid"], _ = reg.register_question(b, "main", "어느 쪽?")
                return _FakeLoopResult(output="부분 결과"), 0.01
            return _FakeLoopResult(output="완성"), 0.01

        reg._runner = runner
        b = spawn_idle(reg)
        rep = Replies(reg)
        reg.request(b, "일감")
        assert wait_until(lambda: reg.get(b).state == "idle")
        assert rep.kinds() == []  # 런 1 은 회신을 안 밀었다

        assert reg.answer_question(box["qid"], "왼쪽", by="main") == ""
        assert wait_until(lambda: len(rep.kinds()) == 1)
        assert rep.kinds()[0]["output"] == "완성"

    def test_answer_run_reply_reaches_a_peer_requester(self, mkreg, renderer):
        """억제의 대가는 **답 런이 원 요청자에게 도달한다**는 것이다 —
        여기가 무너지면 요청자는 아무것도 못 받는다.

        A 가 자기 질문 런 안에서 바로 답한다. 늦게 답하면 가짜 러너가
        즉시 반환해 독촉 상한이 밀리초에 타 버린다(실제 모델은 런 하나가
        초 단위라 생기지 않는 경합).
        """
        reg = mkreg()
        from_b, asked = [], []

        def runner(query, ctx, **kw):
            if "[question q-" in query:  # A 가 질문을 받았다 → 즉답
                qid = query.split("[question ")[1].split(" ")[0]
                assert reg.answer_question(qid, "v2", by=f"agent:{a}") == ""
                return _FakeLoopResult(output="A 가 답함"), 0.01
            if "[answer to your question" in query:  # B 의 답 런
                return _FakeLoopResult(output="B 완성"), 0.01
            if query.startswith(f"[agent:{b}]"):  # A 가 받은 B 의 최종 회신
                from_b.append(query)
                return _FakeLoopResult(output="A 수신"), 0.01
            asked.append(reg.register_question(b, f"agent:{a}", "정책?"))
            return _FakeLoopResult(output="부분"), 0.01

        reg._runner = runner
        a, b = spawn_idle(reg), spawn_idle(reg)
        reg.request(b, "리팩터", author=f"agent:{a}", expects_reply=True)

        assert wait_until(lambda: len(from_b) == 1, timeout=5.0)
        assert "B 완성" in from_b[0]
        assert "부분" not in from_b[0]  # 낡은 부분 결과는 A 를 깨우지 않았다
        time.sleep(0.15)
        assert len(from_b) == 1  # 두 번 깨우지도 않는다

    def test_run_without_a_question_replies_normally(self, mkreg, renderer):
        reg = mkreg()
        b = spawn_idle(reg)
        rep = Replies(reg)
        reg.request(b, "평범한 일감")
        assert wait_until(lambda: len(rep.kinds()) == 1)

    def test_failed_registration_does_not_withhold(self, mkreg, renderer):
        """등록이 실패하면 답 런이 안 생긴다 — 거기에 억제를 걸면 그 런의
        회신이 **영영 사라진다**. 그래서 플래그는 배달 성공 뒤에 세운다."""
        reg = mkreg()

        def runner(query, ctx, **kw):
            qid, err = reg.register_question(b, f"agent:{dead}", "죽은 상대에게")
            assert not qid and "dead" in err
            return _FakeLoopResult(output="그래도 끝냈다"), 0.01

        reg._runner = runner
        b, dead = spawn_idle(reg), spawn_idle(reg)
        reg.kill(dead)
        rep = Replies(reg)
        reg.request(b, "일감")
        assert wait_until(lambda: len(rep.kinds()) == 1)
        assert rep.kinds()[0]["output"] == "그래도 끝냈다"

    def test_flag_is_per_run(self, mkreg, renderer):
        """플래그가 런 경계에서 안 지워지면 이후 모든 회신이 사라진다."""
        reg = mkreg()
        calls = []

        def runner(query, ctx, **kw):
            calls.append(query)
            if len(calls) == 1:
                reg.register_question(b, "main", "첫 런의 질문")
            return _FakeLoopResult(output=f"out{len(calls)}"), 0.01

        reg._runner = runner
        b = spawn_idle(reg)
        rep = Replies(reg)
        reg.request(b, "일감 1")
        assert wait_until(lambda: len(calls) == 1)
        reg.request(b, "일감 2")
        assert wait_until(lambda: len(calls) == 2)
        assert wait_until(lambda: len(rep.kinds()) == 1)
        assert rep.kinds()[0]["output"] == "out2"  # 두 번째 런은 밀었다

    def test_capped_question_still_produces_a_reply(self, mkreg, renderer):
        """아무도 답하지 않아도 상한이 질문을 닫고, 그 닫힘이 답 런을
        만들어 회신을 보낸다 — 억제가 **영구 보류**가 되지 않는다.

        주소를 peer 로 둔다: 주소가 main 이면 독촉을 거는 주체가 main 펌프
        뿐이라 테스트에는 없고, 상한이 구조적으로 안 걸린다.
        """
        reg = mkreg()
        from_b, asked = [], []

        def runner(query, ctx, **kw):
            if "[question q-" in query:
                return _FakeLoopResult(output="A 는 답하지 않는다"), 0.01
            if "(reminder)" in query:
                return _FakeLoopResult(output="A 는 여전히 답하지 않는다"), 0.01
            if "[answer to your question" in query:
                return _FakeLoopResult(output="B 마무리"), 0.01
            if query.startswith(f"[agent:{b}]"):
                from_b.append(query)
                return _FakeLoopResult(output="A 수신"), 0.01
            asked.append(reg.register_question(b, f"agent:{a}", "영영 무응답"))
            return _FakeLoopResult(output="부분"), 0.01

        reg._runner = runner
        a, b = spawn_idle(reg), spawn_idle(reg)
        reg.request(b, "리팩터", author=f"agent:{a}", expects_reply=True)

        assert wait_until(lambda: reg._questions == {}, timeout=10.0)  # 상한이 닫음
        assert wait_until(lambda: len(from_b) == 1, timeout=10.0)
        # A 가 받는 것은 닫힘 사유가 아니라 **그 사유를 보고 B 가 마무리한
        # 결과**다 — 사유는 B 에게 답으로 들어가고, B 의 답 런이 회신한다.
        assert "B 마무리" in from_b[0]
        assert "부분" not in from_b[0]
        time.sleep(0.15)
        assert len(from_b) == 1

    def test_two_questions_in_one_run_give_two_fresh_replies(self, mkreg, renderer):
        """한 런이 둘을 물으면 답 런도 둘, 회신도 둘 — 보장하는 것은
        "정확히 한 번" 이 아니라 **"낡지 않음"** 이다. 둘 다 각자의 답이
        반영된 상태다."""
        reg = mkreg()
        ids, n = [], []

        def runner(query, ctx, **kw):
            n.append(query)
            if len(n) == 1:
                for t in ("질문 A", "질문 B"):
                    qid, err = reg.register_question(b, "main", t)
                    assert not err
                    ids.append(qid)
            return _FakeLoopResult(output=f"out{len(n)}"), 0.01

        reg._runner = runner
        b = spawn_idle(reg)
        rep = Replies(reg)
        reg.request(b, "일감")
        assert wait_until(lambda: len(ids) == 2)
        assert wait_until(lambda: reg.get(b).state == "idle")
        assert rep.kinds() == []

        for qid in ids:
            assert reg.answer_question(qid, "답", by="main") == ""
        assert wait_until(lambda: len(rep.kinds()) == 2, timeout=5.0)

    def test_same_question_across_runs_is_not_folded(self, mkreg, renderer):
        """중복 접기는 **런 안에서만**. 런 경계를 넘어 접으면 요청 둘에
        답 런 하나 → 회신 하나가 되어, 낡은 회신보다 나쁜 **누락**이 된다."""
        reg = mkreg()
        ids, n = [], []

        def runner(query, ctx, **kw):
            n.append(query)
            if "일감" in query:  # 답 런에서는 다시 묻지 않는다
                qid, err = reg.register_question(b, "main", "똑같은 질문")
                assert not err
                ids.append(qid)
            return _FakeLoopResult(output=f"out{len(n)}"), 0.01

        reg._runner = runner
        b = spawn_idle(reg)
        rep = Replies(reg)
        reg.request(b, "일감 1")
        assert wait_until(lambda: len(ids) == 1)
        reg.request(b, "일감 2")
        assert wait_until(lambda: len(ids) == 2)
        assert ids[0] != ids[1]  # 런이 다르면 별개의 질문

        for qid in dict.fromkeys(ids):
            assert reg.answer_question(qid, "답", by="main") == ""
        assert wait_until(lambda: len(rep.kinds()) == 2, timeout=5.0)

    def test_same_question_within_one_run_is_folded(self, mkreg, renderer):
        """런 안에서는 접는다 — ``_op_ask`` 가 루프 탐지기 앞에서 반환해
        같은 질문의 반복이 구조적으로 안 잡히기 때문이다."""
        reg = mkreg()
        ids = []

        def runner(query, ctx, **kw):
            for _ in range(3):
                qid, err = reg.register_question(b, "main", "반복 질문")
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
        """사람 발신 런은 애초에 밀 회신이 없다(창만) — 억제가 창까지
        막으면 사용자가 에이전트가 한 일을 못 본다."""
        reg = mkreg()

        def runner(query, ctx, **kw):
            reg.register_question(b, "user:bob", "덮어쓸까요?")
            return _FakeLoopResult(output="정리 완료"), 0.01

        reg._runner = runner
        b = spawn_idle(reg)
        reg.request(b, "일감", author="user:bob")
        assert wait_until(lambda: reg.get(b).state == "idle")
        outs = [
            c[1]
            for c in renderer.named("agent_message")
            if c[1].get("direction") == "out" and c[1].get("key") == b
        ]
        assert len(outs) == 1
        assert "정리 완료" in outs[0]["text"]
        assert (tmp_path_of(reg) / "agents" / b / "replies" / "reply-1.md").is_file()


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

    def test_ask_addresses_the_current_requester(self, mkreg, renderer):
        """주소 = ask 시점의 ``current_author`` (§0). 그게 원 요청자다."""
        reg = mkreg()
        a = spawn_idle(reg)
        reg.get(a).current_author = "user:bob"
        qid, err = port_ask(reg, a, "사람에게")
        assert not err
        assert reg._questions[qid].target == "user:bob"

        reg.get(a).current_author = "main"
        qid2, _ = port_ask(reg, a, "main 에게")
        assert reg._questions[qid2].target == "main"

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

    def test_answer_goes_through_as_this_address(self, mkreg, renderer):
        """포트의 ``answer`` 는 자기 ``me`` 로 답한다 — 남의 질문에 답할 수
        없어야 한다(§0)."""
        reg = mkreg()
        a, b = spawn_idle(reg), spawn_idle(reg)
        qid, _ = reg.register_question(a, "main", "누가 답하나")
        assert "addressed to main" in reg.question_port(b).answer(qid, "내가")
        assert reg.question_port(None).answer(qid, "main 이") == ""

    def test_port_never_exposes_the_registry(self, mkreg, renderer):
        """레지스트리가 서브루프에 닿으면 '팀원 안 팀원 금지' 의 단일
        가드(loop/state.py)가 깨진다 — 포트의 공개 표면은 둘뿐이다."""
        reg = mkreg()
        port = reg.question_port(spawn_idle(reg))
        public = {n for n in dir(port) if not n.startswith("_")}
        assert public == {"ask", "answer", "key", "me", "asker", "nonblocking"}
        # 상주는 비블로킹, main 은 기존 블로킹 경로 (§8-④)
        assert port.nonblocking is True
        assert reg.question_port(None).nonblocking is False
        assert isinstance(port, QuestionPort)


def port_ask(reg, key, text):
    return reg.question_port(key).ask(text)


# ── (가) 가 기댈 표면 ───────────────────────────


class TestPersistShape:
    def test_as_dict_round_trips_every_field(self, mkreg, renderer):
        """resume 복원이 이 모양에 기댄다 — 필드가 빠지면 조용히 유실된다."""
        q = Question(
            id="q-shape",
            asker="agt-x",
            target="agent:agt-y",
            text="본문",
            asked_seq=4,
            delivered_seq=9,
            nags=2,
        )
        d = q.as_dict()
        assert d == {
            "id": "q-shape",
            "asker": "agt-x",
            "target": "agent:agt-y",
            "text": "본문",
            "asked_at": q.asked_at,
            "asked_seq": 4,
            "delivered_seq": 9,
            "nags": 2,
        }

    def test_mark_delivered_records_the_first_read_only(self, mkreg, renderer):
        """'언제 처음 읽었나' 다 — 뒤 런이 덮어쓰면 그 의미가 사라진다."""
        reg = mkreg()
        q = Question(id="q-mk", asker="main", target="agent:zz", text="t")
        reg._questions[q.id] = q
        reg.mark_question_delivered(q.id, 3)
        reg.mark_question_delivered(q.id, 7)
        assert q.delivered_seq == 3
        reg.mark_question_delivered("q-nope", 1)  # 없는 id 는 조용히 무시

    def test_unknown_asker_is_rejected(self, mkreg, renderer):
        reg = mkreg()
        qid, err = reg.register_question("agt-ghost", "main", "질문")
        assert not qid
        assert "unknown asker" in err

    def test_unroutable_target_is_rejected(self, mkreg, renderer):
        reg = mkreg()
        a = spawn_idle(reg)
        qid, err = reg.register_question(a, "nobody", "질문")
        assert not qid
        assert "unroutable" in err
        assert reg._questions == {}  # 등록도 취소된다

    def test_roster_row_carries_text_and_time(self, mkreg, renderer):
        """트레이가 이 세 필드로 그려진다."""
        reg = mkreg()
        a = spawn_idle(reg)
        qid, _ = reg.register_question(a, "user:bob", "덮어쓸까요?")
        (row,) = [r for r in reg.roster_snapshot() if r["key"] == a]
        (item,) = row["open_questions"]
        assert item["id"] == qid
        assert item["text"] == "덮어쓸까요?"
        assert item["to"] == "user:bob"
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
                    "seq": i,
                    "text": t,
                    "author": "user:bob",
                    "expects_reply": True,
                    "ts": time.time(),
                }
            )
        gate.set()

    def test_batch_run_end_also_reminds(self, mkreg, renderer):
        """독촉은 '런이 끝났다' 는 사실에 붙는다. 핸들러마다 두면 배치
        경로에서 빠진다 — 실제로 빠졌었고, 그래서 워커 루프로 옮겼다."""
        gate = threading.Event()
        reg = mkreg(runner=make_runner(block=gate))
        b = spawn_idle(reg)
        q = Question(
            id="q-batch",
            asker="main",
            target=f"agent:{b}",
            text="빚",
            delivered_seq=99,
        )
        reg._questions[q.id] = q
        self._human_batch(reg, b, gate, ("하나", "둘"))
        assert wait_until(
            lambda: any(
                c[1].get("key") == b
                and c[1].get("direction") == "in"
                and "(reminder)" in str(c[1].get("text"))
                for c in renderer.named("agent_message")
            ),
            timeout=5.0,
        )

    def test_batch_run_reports_open_human_questions(self, mkreg, renderer):
        """사람 발신 배치야말로 사람 주소 질문이 나오는 경로다(주소 =
        current_author) — 알림이 가장 필요한 곳이 비어 있었다."""
        gate = threading.Event()
        reg = mkreg()

        def runner(query, ctx, **kw):
            if "선행" in query:
                gate.wait(5)
                return _FakeLoopResult(output="선행 끝"), 0.01
            reg.register_question(b, "user:bob", "덮어쓸까요?")
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
        assert "답을 받지 못한 질문 1건" in out["text"]
        assert "덮어쓸까요?" in out["text"]

    def test_redelivery_does_not_duplicate_the_window_entry(
        self, mkreg, tmp_path, renderer
    ):
        """재배달은 배관만 다시 태운다 — 질문은 첫 세션에서 이미 창에
        그려졌고 `_replay_conversation` 이 방금 재생했다. 다시 그리면
        사용자 창에 같은 질문이 두 번, 로그에 두 줄이 된다."""
        reg = mkreg()
        a, b = spawn_idle(reg), spawn_idle(reg)
        q = Question(id="q-nodup", asker=a, target=f"agent:{b}", text="못 꺼낸 질문")
        reg._questions[q.id] = q
        reg._save_state()
        reg.shutdown_all()

        fresh = AgentRegistry(tmp_path, runtime={"model": "m"}, runner=make_runner())
        fresh.restore()
        assert wait_until(
            lambda: fresh._questions["q-nodup"].delivered_seq is not None, timeout=5.0
        )
        drawn = [
            c
            for c in renderer.named("agent_message")
            if c[1].get("direction") == "question" and c[1].get("key") == a
        ]
        assert drawn == []  # 재배달이 새로 그리지 않았다
        fresh.shutdown_all()

    def test_main_run_ended_is_a_single_definition(self, mkreg, renderer):
        """main 은 펌프가 둘(run/web)이라 호출부가 둘이다 — 정의가 하나여야
        한쪽만 고치는 사고가 안 난다(`runtime.py` 가 있는 이유)."""
        from agent_cli.runtime import main_run_ended

        assert main_run_ended(None) == 0  # 레지스트리 없는 부팅에서 안전
        reg = mkreg()
        a = spawn_idle(reg)
        qid, _ = reg.register_question(a, "main", "어느 쪽?")
        reg.drain_replies()  # 배달 마킹
        assert main_run_ended(reg) == 1
        rec = next(r for r in reg.drain_replies() if r.get("kind") == "reminder")
        assert qid in rec["output"]
        assert main_run_ended(reg) == 1  # 답할 때까지 계속

    def test_mail_notice_labels_reminder_and_answer(self, mkreg, renderer):
        """독촉을 '회신 도착' 으로 적으면 사용자가 뭔가 끝난 줄 안다."""
        from agent_cli.main import _agent_mail_notice

        seen = []
        renderer.agent_mail_hint = lambda **kw: seen.append(kw["text"])
        for kind, want in (
            ("reminder", "독촉"),
            ("answer", "답변 도착"),
            ("question", "질문 도착"),
            ("reply", "회신 도착"),
        ):
            _agent_mail_notice({"kind": kind, "key": "agt-x"})
            assert want in seen[-1], (kind, seen[-1])

    def test_async_question_record_points_at_the_answer_tool(self, mkreg, renderer):
        """main 이 새 request 를 보내면 그건 답이 아니라 일감이다 — 질문은
        열린 채 남고 독촉이 상한까지 돌다 닫힌다. 런만 태운다."""
        rec = build_reply_record(
            {"kind": "question", "id": "q-abc", "key": "agt-x", "output": "어느 쪽?"}
        )
        assert "`answer` tool" in rec["content"]
        assert "q-abc" in rec["content"]
        assert "BLOCKED" not in rec["content"]
        # 블로킹 경로는 ④에서 사라졌다 — 이제 질문 레코드는 하나뿐이다.
        assert "mode" not in rec["content"]


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
        **kw,
    ), provider


class TestFlipToolMount:
    def test_answer_mounts_only_with_a_port_and_is_forced(self, mkreg, renderer):
        """``MessageTool`` 과 같은 선언이라 정책이 파생된다 — 포트 없는
        루프에서는 목록에서 빠지고, 있으면 프로파일 allowed-tools 와 무관
        하게 커널이 강제 탑재한다."""
        from agent_cli.loop import AgentLoop

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
                questions=questions,
            )
            return list(loop.tools_list)

        assert "answer" not in tools_for(None)
        assert "answer" in tools_for(reg.question_port(a))  # force_mount
        assert "answer" in tools_for(reg.question_port(None))  # main 도


class TestFlipDispatch:
    def test_resident_ask_returns_immediately_and_answer_pairs(
        self, mkreg, tmp_path, renderer
    ):
        """실제 루프 관통: ``ask`` 가 막지 않고, 관찰이 '막히지 않았다' 를
        말하며, ``answer`` 가 id 로 짝지어 배달한다."""
        from agent_cli.context.manager import ContextManager

        reg = mkreg()
        a = spawn_idle(reg)
        reg.get(a).current_author = "main"
        port = reg.question_port(a)
        ctx = ContextManager(tmp_path / "s1", max_context_tokens=30_000)

        result, provider = _run_scripted(
            ctx,
            [
                json.dumps({"action": "ask", "question": "v1 or v2?"}),
                json.dumps({"action": "complete", "result": "부분 결과"}),
            ],
            questions=port,
        )
        assert result.success and result.output == "부분 결과"
        # 막지 않았다 — 두 번째 턴이 실제로 돌았다.
        assert provider.call.call_count == 2
        (q,) = list(reg._questions.values())
        assert q.text == "v1 or v2?" and q.target == "main"
        obs = "\n".join(str(m) for m in ctx.get_raw_messages())
        assert "NOT blocked" in obs
        assert q.id in obs  # 모델이 id 를 본다 → 되물을 때 지목 가능

        # main 이 answer 도구로 답한다
        ctx2 = ContextManager(tmp_path / "s2", max_context_tokens=30_000)
        result2, _ = _run_scripted(
            ctx2,
            [json.dumps({"action": "answer", "id": q.id, "text": "v2"})],
            questions=reg.question_port(None),
        )
        assert result2.success
        assert reg._questions == {}
        assert "delivered to the asker" in "\n".join(
            str(m) for m in ctx2.get_raw_messages()
        )

    def test_answer_with_unknown_id_is_a_failed_observation(
        self, mkreg, tmp_path, renderer
    ):
        """모델이 틀린 id 로 답하면 **조용히 성공하면 안 된다** — 질문은
        열린 채 남고 독촉이 계속되는데 모델은 답했다고 믿는다."""
        from agent_cli.context.manager import ContextManager

        reg = mkreg()
        ctx = ContextManager(tmp_path / "s", max_context_tokens=30_000)
        _run_scripted(
            ctx,
            [json.dumps({"action": "answer", "id": "q-nope", "text": "x"})],
            questions=reg.question_port(None),
        )
        raw = ctx.get_raw_messages()
        rec = next(m for m in raw if m.get("tool") == "answer")
        assert rec["success"] is False
        assert "unknown or already-answered" in rec["content"]

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
                questions=questions,
            )
            loop._prompt.rebuild()  # __init__ 은 조립만 — 빌드는 런 시작에
            assert "ask" in loop.tools_list
            return loop.system

        resident = system_for(reg.question_port(a), "r")
        main = system_for(reg.question_port(None), "m")
        # 도구 설명 — "NOT blocked" 만 보면 AnswerTool 설명으로도 통과한다
        assert "KEEP WORKING" in resident and "KEEP WORKING" not in main
        assert "WAIT for their reply" in main and "WAIT for their reply" not in resident
        # 인라인 가이드도 함께 — 둘이 엇갈리면 모델이 헷갈린다
        assert "does NOT block you" in resident
        assert "does NOT block you" not in main
        assert "pick by intent" in main and "pick by intent" not in resident

    def test_resident_ask_description_says_it_does_not_block(self):
        """설명이 'WAIT for their reply' 라고 거짓말하면 모델이 그걸 믿고
        추측으로 메운다."""
        from agent_cli.prompts.system_prompt import _build_tools_section
        from agent_cli.wire_formats import get as get_wf

        wf = get_wf("json_fc")
        blocking = _build_tools_section(["ask"], wf)
        resident = _build_tools_section(["ask"], wf, nonblocking_ask=True)
        assert "WAIT for their reply" in blocking
        assert "WAIT for their reply" not in resident
        assert "NOT blocked" in resident
        # 인라인 가이드도 함께 바뀐다 — 둘이 엇갈리면 모델이 헷갈린다
        assert "does NOT block you" in resident
        assert "does NOT block you" not in blocking
        assert "pick by intent" in blocking
