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
from agent_cli.subagent.agents_live import AgentRegistry, build_reply_record
from tests.test_agents_live import (
    RecordingRenderer,
    make_registry,
    make_runner,
    wait_until,
)


@pytest.fixture
def renderer(monkeypatch):
    r = RecordingRenderer()
    monkeypatch.setattr(render_mod, "get_renderer", lambda: r)
    return r


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
    def test_peer_question_lands_in_inbox_with_id(self, tmp_path, renderer):
        gate = threading.Event()  # B 를 붙잡아 inbox 를 들여다본다
        reg = make_registry(tmp_path, runner=make_runner(block=gate))
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

    def test_duplicate_question_reuses_id(self, tmp_path, renderer):
        gate = threading.Event()
        reg = make_registry(tmp_path, runner=make_runner(block=gate))
        a, b = spawn_idle(reg), spawn_idle(reg)
        first, _ = reg.register_question(a, f"agent:{b}", "같은 질문")
        with SubmitSpy(reg) as spy:
            second, err = reg.register_question(a, f"agent:{b}", "같은 질문")
        assert not err
        assert second == first
        assert spy.calls == []  # 두 번째는 배달하지 않는다
        gate.set()

    def test_empty_question_rejected(self, tmp_path, renderer):
        reg = make_registry(tmp_path)
        a = spawn_idle(reg)
        qid, err = reg.register_question(a, "main", "   ")
        assert not qid
        assert "empty" in err

    def test_dead_target_is_not_registered(self, tmp_path, renderer):
        """배달 실패면 등록도 취소 — 남기면 아무도 못 답할 빚이 된다."""
        reg = make_registry(tmp_path)
        a, b = spawn_idle(reg), spawn_idle(reg)
        reg.kill(b)
        qid, err = reg.register_question(a, f"agent:{b}", "살아있나요?")
        assert not qid
        assert "dead" in err
        assert reg.questions_owed_by(f"agent:{b}") == []
        assert reg.questions_asked_in(a, 0) == []

    def test_main_question_lands_in_mailbox_carrying_id(self, tmp_path, renderer):
        """main 에는 inbox 가 없다 — 메일박스가 유일한 흡수 지점이고,
        ``answer(id)`` 를 하려면 레코드에 id 가 실려야 한다."""
        reg = make_registry(tmp_path)
        a = spawn_idle(reg)
        qid, err = reg.register_question(a, "main", "이 파일 덮어쓸까요?")
        assert not err
        (reply,) = reg.drain_replies()
        assert reply["kind"] == "question"
        assert reply["id"] == qid
        assert reply["output"] == "이 파일 덮어쓸까요?"

    @pytest.mark.parametrize("addr", ["user", "user:bob"])
    def test_human_question_is_not_delivered_only_listed(
        self, tmp_path, renderer, addr
    ):
        """사람에겐 배달할 inbox 가 없다 — ❓ 트레이가 표면이다(§3.6)."""
        reg = make_registry(tmp_path)
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
    def test_queued_question_is_not_owed_until_dequeued(self, tmp_path, renderer):
        """**바쁜 peer 에게 묻기.** 질문이 B 의 큐 뒤에 서 있는 동안은
        B 가 진 빚이 아니다 — 여기서 owed 로 세면 B 의 *앞* 런이 읽지도
        않은 질문으로 nag 를 받고, sweep 은 그걸 "(답변 없음)" 으로 닫아
        asker 에게 거짓 무응답을 보낸다."""
        gate = threading.Event()
        reg = make_registry(tmp_path, runner=make_runner(block=gate))
        a, b = spawn_idle(reg), spawn_idle(reg)
        reg.request(b, "앞선 일감")  # B 를 붙잡는다
        assert wait_until(lambda: reg.get(b).state == "busy")

        qid, err = reg.register_question(a, f"agent:{b}", "이거 맞나요?")
        assert not err
        assert reg.get(b).inbox.qsize() == 1  # 큐에 들어는 갔다
        assert reg.questions_owed_by(f"agent:{b}") == []  # 그러나 빚은 아니다

        gate.set()  # B 가 앞 일감을 끝내고 질문 항목을 꺼낸다
        assert wait_until(lambda: reg.questions_owed_by(f"agent:{b}") != [])
        (owed,) = reg.questions_owed_by(f"agent:{b}")
        assert owed.id == qid
        assert owed.delivered_seq is not None

    def test_asked_in_is_scoped_to_the_run(self, tmp_path, renderer):
        """G3: 다른 런에서 걸어 둔(영영 열릴 수 있는) 사람 질문이 이후
        모든 런의 회신을 막으면 안 된다."""
        reg = make_registry(tmp_path)
        a = spawn_idle(reg)
        reg.get(a).current_seq = 3
        reg.register_question(a, "user:bob", "run 3 의 질문")
        reg.get(a).current_seq = 5
        assert len(reg.questions_asked_in(a, 3)) == 1
        assert reg.questions_asked_in(a, 5) == []


# ── 짝짓기 ──────────────────────────────────────


class TestAnswer:
    def test_only_the_addressee_may_answer(self, tmp_path, renderer):
        reg = make_registry(tmp_path)
        a = spawn_idle(reg)
        qid, _ = reg.register_question(a, "main", "질문")
        assert "addressed to main" in reg.answer_question(qid, "답", by="agent:zz")
        assert "addressed to main" in reg.answer_question(qid, "답", by="user:bob")
        assert reg.answer_question(qid, "답", by="main") == ""

    @pytest.mark.parametrize("answerer", ["user", "user:bob", "user:carol"])
    def test_any_human_may_answer_a_human_question(self, tmp_path, renderer, answerer):
        """G5: CLI 는 ``user``, 웹은 ``user:{nick}``, 뷰어마다 닉이 다르다.
        문자열 동치로 검사하면 두 번째 뷰어의 트레이 답이 거부된다."""
        reg = make_registry(tmp_path)
        a = spawn_idle(reg)
        qid, _ = reg.register_question(a, "user:bob", "배포할까요?")
        assert reg.answer_question(qid, "네", by=answerer) == ""
        assert reg.open_human_questions() == []

    def test_agent_cannot_answer_a_human_question(self, tmp_path, renderer):
        reg = make_registry(tmp_path)
        a = spawn_idle(reg)
        qid, _ = reg.register_question(a, "user:bob", "배포할까요?")
        assert "operator" in reg.answer_question(qid, "네", by="agent:x")

    def test_claim_is_atomic(self, tmp_path, renderer):
        reg = make_registry(tmp_path)
        a = spawn_idle(reg)
        qid, _ = reg.register_question(a, "main", "질문")
        assert reg.answer_question(qid, "첫 답", by="main") == ""
        err = reg.answer_question(qid, "둘째 답", by="main")
        assert "already-answered" in err

    def test_empty_answer_rejected(self, tmp_path, renderer):
        reg = make_registry(tmp_path)
        a = spawn_idle(reg)
        qid, _ = reg.register_question(a, "main", "질문")
        assert "empty" in reg.answer_question(qid, "  ", by="main")
        assert reg.questions_owed_by("main") == []  # 미배달이라 owed 는 아님
        assert len(reg.questions_asked_in(a, 0)) == 1  # 그러나 열려 있다

    def test_answer_routes_back_to_the_original_requester(self, tmp_path, renderer):
        """불변식(§0): 질문의 주소가 곧 원 요청자다. ``author=q.target`` +
        ``expects_reply=True`` 면 기존 회신 라우팅이 제자리로 보낸다."""
        gate = threading.Event()
        reg = make_registry(tmp_path, runner=make_runner(block=gate))
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

    def test_answer_to_main_asker_uses_the_mailbox(self, tmp_path, renderer):
        """설계 3판 §3.3 이 빠뜨린 경우 — ``submit`` 의 대상은 상주
        에이전트뿐이라 main 이 asker 면 메일박스로 가야 한다."""
        reg = make_registry(tmp_path)
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
    def test_close_delivers_the_reason_to_the_asker(self, tmp_path, renderer):
        reg = make_registry(tmp_path)
        b = spawn_idle(reg)
        qid, _ = reg.register_question("main", f"agent:{b}", "질문")
        reg.drain_replies()
        assert reg.close_question(qid, "상한 초과") is not None
        (reply,) = reg.drain_replies()
        assert "상한 초과" in reply["output"]
        assert reg.close_question(qid, "다시") is None  # 멱등

    def test_nag_counter(self, tmp_path, renderer):
        reg = make_registry(tmp_path)
        a = spawn_idle(reg)
        qid, _ = reg.register_question(a, "main", "질문")
        assert reg.bump_question_nag(qid) == 1
        assert reg.bump_question_nag(qid) == 2
        assert reg.bump_question_nag("q-nope") == 0


# ── G1: 사망 정리 ───────────────────────────────


class TestDeath:
    def test_kill_purges_both_directions(self, tmp_path, renderer):
        gate = threading.Event()
        gate.set()
        reg = make_registry(tmp_path, runner=make_runner(block=gate))
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

    def test_shutdown_all_keeps_questions(self, tmp_path, renderer):
        """G1: ``_worker`` finally 는 세션 종료에서도 돈다. 거기서 지우면
        직후의 ``_save_state`` 가 빈 목록을 저장해 resume 이 알릴 열린
        질문이 **항상 0건**이 된다."""
        reg = make_registry(tmp_path)
        a = spawn_idle(reg)
        qid, _ = reg.register_question(a, "user:bob", "살아남아야 한다")
        reg.shutdown_all()
        assert [q.id for q in reg.open_human_questions()] == [qid]
        saved = json.loads((tmp_path / "agents.json").read_text(encoding="utf-8"))
        assert [q["id"] for q in saved["questions"]] == [qid]


# ── 영속 ────────────────────────────────────────


class TestPersistence:
    def test_saved_question_survives_and_is_counted_not_revived(
        self, tmp_path, renderer
    ):
        """§3.9: 되살리지 않고 **N건만 알린다** — 재시작으로 asker 의 런이
        사라져 답을 받을 주체가 없다. 저장 없이 N 을 알릴 수 없으므로,
        알릴 거면 저장한다."""
        reg = make_registry(tmp_path)
        a = spawn_idle(reg)
        reg.register_question(a, "user:bob", "열린 채 종료")
        reg.shutdown_all()

        fresh = AgentRegistry(tmp_path, runtime={"model": "m"}, runner=make_runner())
        assert fresh.stale_questions == 0  # restore 전
        fresh.restore()
        assert fresh.stale_questions == 1
        assert fresh.open_human_questions() == []  # 되살리지 않는다
        fresh.shutdown_all()

    def test_fresh_registry_reports_zero(self, tmp_path, renderer):
        reg = AgentRegistry(tmp_path, runtime={"model": "m"}, runner=make_runner())
        reg.restore()
        assert reg.stale_questions == 0
