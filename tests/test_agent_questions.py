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

import pytest

import agent_cli.render as render_mod
from agent_cli.subagent.agents_live import (
    _MAX_QUESTION_NAGS,
    AgentRegistry,
    Question,
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
    """``submit`` 호출 인자를 기록 — 답 배달의 라우팅 인자를 고정한다."""

    def __init__(self, reg):
        self.reg = reg
        self.calls = []
        self._orig = reg.submit

    def __enter__(self):
        def spy(key, message, **kw):
            self.calls.append({"key": key, "message": message, **kw})
            return self._orig(key, message, **kw)

        self.reg.submit = spy
        return self

    def __exit__(self, *a):
        self.reg.submit = self._orig


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
        # 앞으로 온 것: asker 가 풀려야 한다 — 사유를 배달.
        assert wait_until(lambda: reg.questions_asked_in(a, 0) == [])
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
    def test_saved_question_survives_and_is_counted_not_revived(
        self, mkreg, tmp_path, renderer
    ):
        """§3.9: 되살리지 않고 **N건만 알린다** — 재시작으로 asker 의 런이
        사라져 답을 받을 주체가 없다. 저장 없이 N 을 알릴 수 없으므로,
        알릴 거면 저장한다."""
        reg = mkreg()
        a = spawn_idle(reg)
        reg.register_question(a, "user:bob", "열린 채 종료")
        reg.shutdown_all()

        fresh = AgentRegistry(tmp_path, runtime={"model": "m"}, runner=make_runner())
        assert fresh.stale_questions == 0  # restore 전
        fresh.restore()
        assert fresh.stale_questions == 1
        assert fresh.open_human_questions() == []  # 되살리지 않는다
        fresh.shutdown_all()

    def test_fresh_registry_reports_zero(self, mkreg, tmp_path, renderer):
        reg = AgentRegistry(tmp_path, runtime={"model": "m"}, runner=make_runner())
        reg.restore()
        assert reg.stale_questions == 0


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
        """모델이 끝내 안 답해도 asker 가 영원히 기다리지는 않는다.

        ``Question`` 을 직접 넣어 배달 경로를 건너뛴다 — 여기서 재는 것은
        독촉 정책이지 배달이 아니고, 라이브 워커가 질문 항목을 꺼내면
        ``delivered_seq`` 가 실제 seq 로 먼저 찍혀 런 스코프가 흐려진다.
        """
        reg = mkreg()
        b = spawn_idle(reg)
        q = Question(
            id="q-cap01",
            asker="main",
            target=f"agent:{b}",
            text="질문",
            delivered_seq=7,
        )
        reg._questions[q.id] = q
        for _ in range(_MAX_QUESTION_NAGS):
            assert reg.remind_owed(f"agent:{b}") == 1
        assert reg.remind_owed(f"agent:{b}") == 0  # 상한 — 닫힌다
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
        b = spawn_idle(reg)
        q = Question(id="q-scp01", asker="main", target=f"agent:{b}", text="질문")
        reg._questions[q.id] = q
        assert reg.remind_owed(f"agent:{b}") == 0  # 아직 안 꺼냈다
        reg.mark_question_delivered(q.id, 4)
        assert reg.remind_owed(f"agent:{b}") == 1

    def test_cascade_terminates_at_the_cap(self, mkreg, tmp_path, renderer):
        """독촉 런 끝에서도 독촉하므로 답 않는 모델에겐 연쇄한다 — 그건
        낭비지 버그가 아니고, **상한이 반드시 걸려 끝난다**.

        한때 ``item["reminder"]`` 로 연쇄를 막았는데 그게 더 나빴다:
        독촉 1회 뒤 그 에이전트에게 일이 안 오면 ``nags`` 가 1에 멈춰
        상한이 영영 안 걸리고 질문이 영원히 열린다(정지).
        """
        reg = mkreg()
        a, b = spawn_idle(reg), spawn_idle(reg)
        qid, _ = reg.register_question(a, f"agent:{b}", "끝내 답 안 할 질문")
        # 아무도 답하지 않아도 스스로 끝난다.
        assert wait_until(lambda: qid not in reg._questions, timeout=10.0)
        assert wait_until(lambda: reg.get(b).state == "idle", timeout=10.0)
        # asker 는 침묵이 아니라 사유를 받는다.
        assert wait_until(
            lambda: any(
                "repeated reminders" in str(c[1].get("text"))
                for c in renderer.named("agent_message")
                if c[1].get("key") == a
            )
        )

    def test_reminder_repeats_until_answered(self, mkreg, tmp_path, renderer):
        """한 번 읽은 빚은 **답할 때까지 매 런 끝에** 다시 온다 — 사용자의
        'post turn prompt 로 계속 넣어준다'가 이것이다."""
        reg = mkreg()
        b = spawn_idle(reg)
        q = Question(
            id="q-rep01",
            asker="main",
            target=f"agent:{b}",
            text="질문",
            delivered_seq=4,
        )
        reg._questions[q.id] = q
        assert reg.remind_owed(f"agent:{b}") == 1  # 런 A 끝
        assert reg.remind_owed(f"agent:{b}") == 1  # 런 B 끝 — seq 가 달라도 계속
        assert reg.answer_question(q.id, "답", by=f"agent:{b}") == ""
        assert reg.remind_owed(f"agent:{b}") == 0  # 답했으면 그친다


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
        reg.submit(b, "일감", author="user:bob")
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
