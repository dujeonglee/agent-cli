"""A6: 응답↔질문 1:1 귀속 (``id`` / ``reply_to``, 병합 계획 M1).

포크(Coagora)의 ``Message.replyToId`` 를 본류 레코드 단위로 옮긴 것.
직렬 모드에서는 유발 질의가 항상 직전 사용자 메시지라 귀속이 암묵적이지만,
턴이 복수가 되는 순간 그 암묵성이 소실되므로 지금 데이터로 남긴다.

핵심 불변식 2개를 고정한다:
  1. 귀속은 **history.jsonl 에만** 존재한다 — 캐시/LLM 경로는 무변.
  2. resume 가 카운터를 이어받아 id 가 재사용되지 않는다.
"""

import json

import pytest

from agent_cli.context.manager import ContextManager


@pytest.fixture
def session_dir(tmp_path):
    return tmp_path / "sessions" / "attribution"


def _records(ctx) -> list[dict]:
    with open(ctx.history_path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


class TestAttribution:
    def test_user_query_gets_an_id(self, session_dir):
        ctx = ContextManager(session_dir)
        ctx.add({"role": "user", "content": "hello"})
        assert _records(ctx)[0]["id"] == "u1"

    def test_follow_up_records_point_at_the_query(self, session_dir):
        ctx = ContextManager(session_dir)
        ctx.add({"role": "user", "content": "build it"})
        ctx.add({"role": "assistant", "ops": [{"action": "shell", "action_input": {}}]})
        ctx.add({"role": "user", "tool": "shell", "success": True, "content": "ok"})

        query, action, observation = _records(ctx)
        assert query["id"] == "u1"
        assert "reply_to" not in query  # 질의 자신은 무엇에 대한 응답도 아니다
        assert action["reply_to"] == "u1"
        assert observation["reply_to"] == "u1"

    def test_a_new_query_takes_over_attribution(self, session_dir):
        ctx = ContextManager(session_dir)
        ctx.add({"role": "user", "content": "first"})
        ctx.add({"role": "assistant", "content": "a"})
        ctx.add({"role": "user", "content": "second"})
        ctx.add({"role": "assistant", "content": "b"})

        q1, a1, q2, a2 = _records(ctx)
        assert (q1["id"], a1["reply_to"]) == ("u1", "u1")
        assert (q2["id"], a2["reply_to"]) == ("u2", "u2")

    def test_records_before_any_query_carry_no_reply_to(self, session_dir):
        """system anchor 처럼 어떤 질의보다 앞선 레코드는 무귀속."""
        ctx = ContextManager(session_dir)
        ctx.add({"role": "system", "content": "you are an agent"})
        assert "reply_to" not in _records(ctx)[0]

    def test_multi_user_queries_are_distinguishable(self, session_dir):
        """다중 사용자 세션: 누가 물었는지(author)와 어느 질의였는지(id)가
        각각 남아 병렬화 시 1:1 귀속의 근거가 된다."""
        ctx = ContextManager(session_dir)
        ctx.add({"role": "user", "content": "[a]: x", "author": "a"})
        ctx.add({"role": "assistant", "content": "for a"})
        ctx.add({"role": "user", "content": "[b]: y", "author": "b"})
        ctx.add({"role": "assistant", "content": "for b"})

        recs = _records(ctx)
        assert (recs[0]["author"], recs[0]["id"]) == ("a", "u1")
        assert recs[1]["reply_to"] == "u1"
        assert (recs[2]["author"], recs[2]["id"]) == ("b", "u2")
        assert recs[3]["reply_to"] == "u2"


class TestSerialPathUnchanged:
    """기존 직렬 경로 보존 — 귀속은 디스크 레코드에만 붙는다."""

    def test_cache_and_llm_messages_are_untouched(self, session_dir):
        ctx = ContextManager(session_dir)
        user = {"role": "user", "content": "hello"}
        ctx.add(user)
        ctx.add({"role": "assistant", "content": "hi"})

        # 호출자가 넘긴 dict 자체가 오염되지 않는다(enrich 는 복사본 작업).
        assert user == {"role": "user", "content": "hello"}
        for msg in ctx.get_raw_messages():
            assert "id" not in msg
            assert "reply_to" not in msg
        for msg in ctx.get_messages():
            assert "reply_to" not in msg

    def test_add_returns_the_unenriched_message(self, session_dir):
        ctx = ContextManager(session_dir)
        returned = ctx.add({"role": "user", "content": "hello"})
        assert "id" not in returned


class TestResume:
    def test_counter_continues_across_resume(self, session_dir):
        ctx = ContextManager(session_dir)
        ctx.add({"role": "user", "content": "first"})
        ctx.add({"role": "user", "content": "second"})

        resumed = ContextManager(session_dir, resume=True)
        resumed.add({"role": "user", "content": "third"})

        ids = [r["id"] for r in _records(resumed) if "id" in r]
        assert ids == ["u1", "u2", "u3"]  # 재사용 없음

    def test_resume_keeps_attributing_to_the_last_query(self, session_dir):
        ctx = ContextManager(session_dir)
        ctx.add({"role": "user", "content": "first"})

        resumed = ContextManager(session_dir, resume=True)
        resumed.add({"role": "assistant", "content": "late answer"})
        assert _records(resumed)[-1]["reply_to"] == "u1"

    def test_counter_survives_a_compacted_prefix(self, session_dir):
        """캐시에서 밀려난 앞부분의 id 도 카운터에 반영돼야 한다 —
        전체 history 를 훑는 이유."""
        ctx = ContextManager(session_dir)
        for i in range(3):
            ctx.add({"role": "user", "content": f"q{i}"})

        resumed = ContextManager(session_dir, resume=True)
        # 캐시는 잘려도(예산/압축) 카운터는 디스크 전량 기준.
        resumed._cache = resumed._cache[-1:]
        resumed.add({"role": "user", "content": "next"})
        assert _records(resumed)[-1]["id"] == "u4"

    def test_legacy_session_without_ids_starts_at_u1(self, session_dir):
        """이 필드 도입 전 세션: 기존 레코드는 무귀속인 채 남고 새 질의만
        번호를 받는다(additive 필드의 정상적 부재)."""
        session_dir.mkdir(parents=True, exist_ok=True)
        with open(session_dir / "history.jsonl", "w", encoding="utf-8") as f:
            f.write(json.dumps({"role": "user", "content": "old", "kind": "query"}))
            f.write("\n")

        resumed = ContextManager(session_dir, resume=True)
        resumed.add({"role": "user", "content": "new"})
        recs = _records(resumed)
        assert "id" not in recs[0]
        assert recs[1]["id"] == "u1"

    def test_malformed_id_is_ignored_by_the_counter(self, session_dir):
        session_dir.mkdir(parents=True, exist_ok=True)
        with open(session_dir / "history.jsonl", "w", encoding="utf-8") as f:
            for bad in ("uXY", "abc", 7):
                f.write(json.dumps({"role": "user", "content": "q", "id": bad}))
                f.write("\n")

        resumed = ContextManager(session_dir, resume=True)
        resumed.add({"role": "user", "content": "new"})
        assert _records(resumed)[-1]["id"] == "u1"


class TestParallelAttribution:
    """A1: 동시 턴에서 귀속이 섞이지 않는가.

    실측으로 발견한 회귀의 가드다 — 세션 전역 ``_reply_to`` 하나만 쓰던 최초
    구현은 동시 3턴에서 **세 응답이 전부 마지막 질의를 가리켰다**. A6 가
    존재하는 이유(병렬화 시 1:1 귀속 보존) 자체가 무너지는 버그였다.
    턴 하나 = 스레드 하나이므로 귀속은 스레드별이어야 한다.
    """

    def test_concurrent_turns_keep_their_own_attribution(self, session_dir):
        import threading

        ctx = ContextManager(session_dir, max_context_tokens=1_000_000)
        n = 6
        start = threading.Barrier(n)
        mid = threading.Barrier(n)

        def turn(i):
            start.wait(timeout=5)
            ctx.add({"role": "user", "content": f"[u{i}]: q{i}", "author": f"u{i}"})
            # 전원이 질의를 넣은 뒤에 응답을 넣는다 — 전역 필드 하나면
            # 여기서 모두 마지막 질의를 가리키게 된다(원래 버그의 재현 조건).
            mid.wait(timeout=5)
            ctx.add({"role": "assistant", "content": f"answer-{i}"})

        ts = [threading.Thread(target=turn, args=(i,)) for i in range(n)]
        [t.start() for t in ts]
        [t.join(timeout=10) for t in ts]

        recs = _records(ctx)
        queries = {r["id"]: r["author"] for r in recs if r.get("kind") == "query"}
        assert len(queries) == n

        answers = [r for r in recs if r.get("kind") == "raw"]
        assert len(answers) == n
        # 각 응답의 reply_to 는 **자기 스레드의 질의**를 가리켜야 한다:
        # answer-i 의 작성자는 u{i} 였다.
        for a in answers:
            i = a["content"].removeprefix("answer-")
            assert queries[a["reply_to"]] == f"u{i}", (
                f"answer-{i} 가 {queries[a['reply_to']]} 의 질의에 귀속됨 — 귀속 혼선"
            )
        # 그리고 모두 서로 다른 질의를 가리킨다(한 곳으로 몰리지 않았다).
        assert len({a["reply_to"] for a in answers}) == n

    def test_thread_without_its_own_query_falls_back_to_latest(self, session_dir):
        """질의를 스스로 넣지 않은 스레드(백그라운드 배달 등)는 전역 폴백."""
        import threading

        ctx = ContextManager(session_dir, max_context_tokens=1_000_000)
        ctx.add({"role": "user", "content": "q"})

        seen = []

        def bg():
            ctx.add({"role": "assistant", "content": "from-other-thread"})
            seen.append(True)

        t = threading.Thread(target=bg)
        t.start()
        t.join(timeout=5)
        assert seen
        assert _records(ctx)[-1]["reply_to"] == "u1"
