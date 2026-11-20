"""봉투 — 만드는 함수가 닫는 법을 정하고, 봉투는 불변이다."""

from __future__ import annotations

import dataclasses

import pytest

from agent_cli.post import envelope as E


@pytest.mark.parametrize(
    ("make", "kind", "closes_by"),
    [
        (lambda: E.request("r-1", "main", "agent:a", "do"), "request", "reply"),
        (lambda: E.human("u-1", "user:dj", "agent:a", "hi"), "request", "complete"),
        (lambda: E.notice("n-1", "agent:a", "main", "fyi"), "notice", None),
        (lambda: E.reply("n-2", "agent:a", "main", "ok", re="r-1"), "reply", None),
        (
            lambda: E.system("n-3", "job", "main", "done", opens_row=True),
            "system",
            "complete",
        ),
        (
            lambda: E.system("n-4", "job", "agent:a", "done", opens_row=False),
            "system",
            None,
        ),
    ],
)
def test_each_maker_says_how_it_closes(make, kind, closes_by):
    env = make()
    assert (env.kind, env.closes_by) == (kind, closes_by)
    assert env.expects_reply is (closes_by == "reply")


def test_a_checkin_is_a_system_notice_that_opens_no_row():
    env = E.system("n-5", "harness", "agent:a", "⏰", opens_row=False)
    assert env.closes_by is None and env.wakes is True


def test_a_quiet_reply_does_not_wake():
    assert E.reply("n-6", "agent:a", "main", "x", re="r-1", wakes=False).wakes is False


def test_frozen_with_tuple_answers_and_a_read_only_payload_copy():
    src = {"run": "r-1"}
    answers = ["dj"]
    env = E.request("r-2", "main", "agent:a", "do", answers=answers, payload=src)
    with pytest.raises(dataclasses.FrozenInstanceError):
        env.text = "changed"
    assert env.answers == ("dj",)
    answers.append("other")
    src["run"] = "r-9"
    assert env.answers == ("dj",) and env.payload["run"] == "r-1"  # 사본
    with pytest.raises(TypeError):
        env.payload["run"] = "x"  # 읽기 전용


def test_human_words_are_told_by_kind_and_closes_by_only():
    assert E.is_human_words(E.human("u-1", "user:dj", "agent:a", "hi"))
    # 주소가 사람처럼 보여도 머리가 사람의 말이 아니면 아니다 — 주소를 읽지 않는다
    assert not E.is_human_words(E.notice("n-1", "user:dj", "agent:a", "hi"))
    assert not E.is_human_words(E.request("r-1", "user:dj", "agent:a", "hi"))


def test_hashable_and_equal_by_value():
    a = E.notice("n-1", "agent:a", "main", "fyi", payload={"k": 1})
    b = E.notice("n-1", "agent:a", "main", "fyi", payload={"k": 1})
    assert a == b and hash(a) == hash(b)


def test_a_persons_answer_is_human_words_about_the_question():
    env = E.human("u-7", "user:dj", "agent:a", "v2", re="q-3")
    assert E.is_human_words(env) and env.re == "q-3" and env.closes_by == "complete"
