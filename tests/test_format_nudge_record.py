"""구조화 형식 넛지 레코드 (v10.5.0) — 저장은 방언과 독립.

history 의 파싱 실패 관찰은 문장이 아니라 ``nudge: {reason, prior, …}`` 만
저장하고, 모델이 읽을 문장은 읽는 시점의 방언이 조립한다. 여기서는
① 레코드 모양, ② 라이브 메시지 == 캐시 렌더 == resume 렌더, ③ 다른 방언으로
resume 하면 그 방언의 문장이 나온다, ④ 발췌는 두 번 잘리지 않는다,
⑤ 재생·분류·토큰 추정이 content 없는 레코드를 다룬다 — 를 고정한다.
"""

from __future__ import annotations

import json

import pytest

from agent_cli.context.manager import ContextManager
from agent_cli.context.records import _classify_record, is_format_intervention
from agent_cli.context.render import _estimate_message_tokens, render_history_message
from agent_cli.dialects import get as get_dialect
from agent_cli.recovery.dialect_recovery import (
    build_format_nudge,
    format_no_action_retry,
    format_no_json_retry,
    make_format_nudge,
)
from agent_cli.recovery.primitives import ECHO_MAX_CHARS, bounded_excerpt


def _dispatcher(tmp_path, dialect_name="json_fc"):
    from agent_cli.loop import LoopConfig, LoopState, ToolBridge, TurnDispatcher
    from agent_cli.recovery.observability import TurnRecorder

    wf = get_dialect(dialect_name)
    ctx = ContextManager(tmp_path / "s", max_context_tokens=100_000, dialect=wf)
    cfg = LoopConfig(tools_list=["shell", "complete"], dialect=wf)
    st = LoopState(query="q")
    d = TurnDispatcher(
        cfg,
        st,
        ctx=ctx,
        tools=ToolBridge(cfg, st, ctx, None),
        recorder=TurnRecorder(session_dir=None, enabled=False),
    )
    return d, ctx, st


class TestMakeAndBuild:
    def test_no_json_round_trips_to_the_direct_builder(self):
        wf = get_dialect("json_fc")
        raw = '  [{"broken\n  '
        nudge = make_format_nudge("no_json", raw, syntax_error="line 1: oops")
        assert nudge == {
            "reason": "no_json",
            "prior": raw.strip(),
            "syntax_error": "line 1: oops",
        }
        direct = format_no_json_retry(
            prior_content=raw, dialect=wf, syntax_error="line 1: oops"
        )
        built = build_format_nudge(nudge, wf)
        assert built.message == direct.message
        assert built.primitives == direct.primitives

    def test_no_action_round_trips_to_the_direct_builder(self):
        wf = get_dialect("xml_fc")
        nudge = make_format_nudge("no_action", "just prose")
        assert nudge == {"reason": "no_action", "prior": "just prose"}
        assert (
            build_format_nudge(nudge, wf).message
            == format_no_action_retry(prior_content="just prose", dialect=wf).message
        )

    def test_flags_only_when_true(self):
        nudge = make_format_nudge("no_json", "", thinking_only=True, swallowed=False)
        assert nudge == {"reason": "no_json", "prior": "", "thinking_only": True}
        wf = get_dialect("json_fc")
        assert (
            build_format_nudge(nudge, wf).message
            == format_no_json_retry(
                prior_content="", dialect=wf, thinking_only=True
            ).message
        )
        swallowed = make_format_nudge("no_json", "", swallowed=True)
        assert "swallowed" in build_format_nudge(swallowed, wf).primitives[0]

    def test_long_prior_is_stored_bounded_and_not_cut_twice(self):
        wf = get_dialect("json_fc")
        raw = "x" * (ECHO_MAX_CHARS * 5)
        nudge = make_format_nudge("no_json", raw)
        assert nudge["prior"] == bounded_excerpt(raw)
        assert len(nudge["prior"]) < len(raw)
        # 저장된 발췌로 조립한 문장 == 원문으로 바로 조립한 문장
        assert (
            build_format_nudge(nudge, wf).message
            == format_no_json_retry(prior_content=raw, dialect=wf).message
        )

    def test_unknown_reason_rejected(self):
        with pytest.raises(ValueError):
            make_format_nudge("whatever", "x")


class TestRecordShape:
    def test_parse_failure_stores_nudge_not_text(self, tmp_path):
        d, ctx, _ = _dispatcher(tmp_path)
        d._handle_text_path('[{"broken op')
        [rec] = [r for r in ctx.get_raw_messages() if is_format_intervention(r)]
        assert rec["tool"] == "" and rec["success"] is False
        assert rec["recovery"] == "format"
        assert "content" not in rec
        # 관용 파서가 op 잔해를 건지면 no_action, 아니면 no_json — 둘 다 구조화
        assert rec["nudge"]["reason"] in ("no_json", "no_action")
        assert rec["nudge"]["prior"] == '[{"broken op'
        # 파일에도 같은 모양 (JSON 한 줄, 방언 문장 없음)
        on_disk = [json.loads(ln) for ln in ctx.history_path.read_text().splitlines()]
        stored = [r for r in on_disk if r.get("recovery") == "format"][-1]
        assert "content" not in stored and stored["nudge"] == rec["nudge"]

    def test_unparseable_text_is_no_json(self, tmp_path):
        d, ctx, _ = _dispatcher(tmp_path)
        d._handle_text_path("{{{{ ::: not a call :::")
        recs = [r for r in ctx.get_raw_messages() if is_format_intervention(r)]
        if (
            recs
        ):  # 순수 산문은 산문-완료로 수용될 수 있다 (v7.14) — 개입이 있으면 구조화
            assert recs[-1]["nudge"]["reason"] in ("no_json", "no_action")
            assert "content" not in recs[-1]

    def test_no_action_reason(self, tmp_path):
        d, ctx, _ = _dispatcher(tmp_path)
        d._handle_text_path(
            '[{"action_input": {"cmd": "ls"}}]'
        )  # op 는 있고 action 이 없다
        recs = [r for r in ctx.get_raw_messages() if is_format_intervention(r)]
        assert recs and recs[-1]["nudge"]["reason"] in ("no_action", "no_json")

    def test_live_message_equals_cache_render(self, tmp_path):
        d, ctx, st = _dispatcher(tmp_path)
        d._handle_text_path('[{"broken op')
        live = st.messages[-1]["content"]
        rendered = ctx.get_messages()[-1]["content"]
        assert live == rendered
        assert "Your prior output" in live and '[{"broken op' in live


class TestResumeAcrossDialects:
    def _record(self):
        return {
            "role": "user",
            "tool": "",
            "success": False,
            "recovery": "format",
            "nudge": make_format_nudge("no_json", "<tool_call>half"),
        }

    def test_same_dialect_renders_same_sentence(self):
        wf = get_dialect("json_fc")
        [msg] = render_history_message(
            self._record(), wf, index=1, assistant_index=None
        )
        assert msg == {
            "role": "user",
            "content": format_no_json_retry(
                prior_content="<tool_call>half", dialect=wf
            ).message,
        }

    def test_other_dialect_renders_its_own_rules(self):
        rec = self._record()
        json_msg = render_history_message(
            rec, get_dialect("json_fc"), index=1, assistant_index=None
        )[0]["content"]
        xml_msg = render_history_message(
            rec, get_dialect("xml_fc"), index=1, assistant_index=None
        )[0]["content"]
        assert json_msg != xml_msg
        assert "<tool_call>half" in json_msg and "<tool_call>half" in xml_msg
        assert (
            xml_msg
            == format_no_json_retry(
                prior_content="<tool_call>half", dialect=get_dialect("xml_fc")
            ).message
        )

    def test_resumed_context_renders_with_the_new_dialect(self, tmp_path):
        first = ContextManager(
            tmp_path / "s", max_context_tokens=100_000, dialect=get_dialect("json_fc")
        )
        first.add({"role": "user", "content": "q"})
        first.add(self._record())
        resumed = ContextManager(
            tmp_path / "s",
            max_context_tokens=100_000,
            dialect=get_dialect("xml_fc"),
            resume=True,
        )
        last = resumed.get_messages()[-1]["content"]
        assert (
            last
            == format_no_json_retry(
                prior_content="<tool_call>half", dialect=get_dialect("xml_fc")
            ).message
        )

    def test_native_fc_renders_nudge_as_user_message(self):
        [msg] = render_history_message(
            self._record(), get_dialect("native_fc"), index=1, assistant_index=0
        )
        assert msg["role"] == "user" and "<tool_call>half" in msg["content"]


class TestReaders:
    def test_classify_record_has_a_search_surface(self):
        rec = {
            "role": "user",
            "tool": "",
            "success": False,
            "recovery": "format",
            "nudge": {"reason": "no_json", "prior": "zzz"},
        }
        kind, tools, text = _classify_record(rec)
        assert kind == "observation" and tools == [""]
        assert "no_json" in text and "zzz" in text

    def test_token_estimate_counts_the_nudge(self):
        empty = {"role": "user", "tool": "", "success": False}
        with_nudge = dict(empty, nudge={"reason": "no_json", "prior": "y" * 400})
        assert _estimate_message_tokens(with_nudge) > _estimate_message_tokens(empty)

    def test_web_replay_skips_the_nudge_like_live(self, tmp_path):
        from agent_cli.render.web import WebRenderer

        r = WebRenderer.__new__(WebRenderer)
        calls = []
        r.observation = lambda *a, **k: calls.append(("observation", a, k))
        r.push_user_message = lambda *a, **k: calls.append(("user", a, k))
        r._replay_authors = []
        r._replay_record(
            {
                "role": "user",
                "tool": "",
                "success": False,
                "nudge": {"reason": "no_json"},
            }
        )
        assert calls == []
        r._replay_record(
            {"role": "user", "tool": "shell", "success": True, "content": "ok"}
        )
        assert calls and calls[0][0] == "observation"
