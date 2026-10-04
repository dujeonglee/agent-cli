"""모델 시점 틀의 on-disk 사본 (v10.17.0) — resume 회귀 가드.

맨 위 "맨 처음 받는 것" 과 "매 턴 끝에 붙는 것" 은 그 스코프의 마지막 LLM 호출
스냅샷에서 나온다. 스냅샷이 메모리에만 있으면 resume 뒤에 main 은 꼬리가 비고
인라인 카드(agent/skill)는 아예 빈다 — 스코프당 파일 하나로 남기고 되살린다.
"""

from __future__ import annotations

import inspect
import json

from fastapi.testclient import TestClient

from agent_cli.render.web import WebConnection, WebRenderer
from agent_cli.web.server import WebServer, create_app


class _Ctx:
    """스코프의 라이브 컨텍스트 — 종료 때 대화 크기를 고정하는 데 쓰인다."""

    compaction_count = 2

    def get_messages(self):
        return [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "do the task " * 20},
        ]

    def get_estimated_tokens(self):
        return 123


_SECTIONS = [("Role", "You are an agent."), ("Hook: lint", "## lint\nrun ruff")]


def _tail(turn: int) -> list[tuple[str, str]]:
    return [("Session State (per-turn tail)", f"turn {turn}/30\n## Session Memory")]


def _prompt(renderer, task_id: str = "") -> dict:
    server = WebServer(renderer, token="t", ctx=None)
    q = f"&task_id={task_id}" if task_id else ""
    return TestClient(create_app(server)).get(f"/api/debug/prompt?token=t{q}").json()


def _run_scope(renderer, sid: str, *, view: dict | None = None) -> None:
    renderer.begin_scope(task_id=sid, kind="skill", label="review", agent="reviewer")
    try:
        renderer.note_scope_ctx(_Ctx())
        renderer.note_system_prompt(
            [("Role", "reviewer")], turn=3, tools=None, tail=_tail(3)
        )
        if view is not None:
            renderer.context_view(view)
    finally:
        renderer.end_scope(task_id=sid)


class TestPersist:
    def test_each_llm_call_leaves_one_file_per_scope(self, tmp_path):
        r = WebRenderer(session_dir=str(tmp_path))
        r.note_system_prompt(_SECTIONS, turn=1, tail=_tail(1))
        r.note_system_prompt(_SECTIONS, turn=2, tail=_tail(2))
        _run_scope(r, "t-1")
        files = sorted(p.name for p in (tmp_path / "prompt_views").glob("*.json"))
        assert len(files) == 2 and "main.json" in files
        main = json.loads((tmp_path / "prompt_views" / "main.json").read_text())
        assert main["scope"] == "" and main["snapshot"]["turn"] == 2
        # 직전 턴의 꼬리도 같이 남는다 — 되살린 뒤에도 diff 가 그려진다
        assert main["snapshot"]["tail_prev"][0]["text"].startswith("turn 1/30")

    def test_no_session_dir_writes_nothing(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        r = WebRenderer()
        r.note_system_prompt(_SECTIONS, turn=1, tail=_tail(1))
        assert list(tmp_path.iterdir()) == []
        assert r.restore_prompt_views() is False

    def test_scope_id_never_becomes_a_path(self, tmp_path):
        r = WebRenderer(session_dir=str(tmp_path))
        _run_scope(r, "../agt-1#3/x")
        names = [p.name for p in (tmp_path / "prompt_views").iterdir()]
        assert len(names) == 1 and "/" not in names[0] and ".." not in names[0]
        assert not (tmp_path.parent / "agt-1#3").exists()

    def test_deleting_a_scope_removes_its_file(self, tmp_path):
        r = WebRenderer(session_dir=str(tmp_path))
        _run_scope(r, "t-1")
        assert r.delete_prompt_scope("t-1") is True
        assert list((tmp_path / "prompt_views").glob("*.json")) == []


class TestRestore:
    def _resumed(self, tmp_path) -> WebRenderer:
        """같은 세션 폴더로 새 프로세스가 뜬 것처럼."""
        r = WebRenderer(session_dir=str(tmp_path))
        self.main_restored = r.restore_prompt_views()
        return r

    def test_main_frame_comes_back_as_it_was(self, tmp_path):
        r = WebRenderer(session_dir=str(tmp_path))
        r.note_system_prompt(_SECTIONS, turn=1, grammar=(False, "root ::= x"))
        r.note_system_prompt(
            _SECTIONS, turn=2, grammar=(False, "root ::= x"), tail=_tail(2)
        )
        before = _prompt(r)
        after = _prompt(self._resumed(tmp_path))
        assert self.main_restored is True
        assert after["ok"] and after["turn"] == 2
        assert after["sections"] == before["sections"]  # 시스템·훅·꼬리·문법
        assert after["tail_prev"] == before["tail_prev"]
        assert after["prev_turn"] == before["prev_turn"]

    def test_inline_scope_comes_back_ended_with_its_size(self, tmp_path):
        r = WebRenderer(session_dir=str(tmp_path))
        _run_scope(r, "t-1")
        before = _prompt(r, "t-1")
        after = _prompt(self._resumed(tmp_path), "t-1")
        assert after["ok"] and after["scope"] == before["scope"]
        assert after["scope"]["kind"] == "skill" and after["scope"]["ended"] is True
        assert after["budget"]["convo"] == before["budget"]["convo"] > 0
        assert after["budget"]["compactions"] == 2
        assert [s["name"] for s in after["sections"]] == [
            s["name"] for s in before["sections"]
        ]

    def test_scope_that_died_mid_run_is_ended_after_resume(self, tmp_path):
        """프로세스가 런 도중 죽으면 종료 기록이 없다 — 되살아나지 않으므로 종료."""
        r = WebRenderer(session_dir=str(tmp_path))
        r.begin_scope(task_id="t-9", kind="run", label="x", agent="worker")
        r.note_scope_ctx(_Ctx())
        r.note_system_prompt([("Role", "worker")], turn=1, tail=_tail(1))
        # end_scope 없이 프로세스가 사라졌다
        after = _prompt(self._resumed(tmp_path), "t-9")
        assert after["ok"] and after["scope"]["ended"] is True

    def test_sub_scope_view_is_replayed_to_new_viewers(self, tmp_path):
        view = {
            "gone": {"hidx": 4},
            "summary": {"text": "요약", "files": [], "turns": [1, 2]},
            "compactions": 1,
        }
        r = WebRenderer(session_dir=str(tmp_path))
        _run_scope(r, "t-1", view=view)
        resumed = self._resumed(tmp_path)
        snapshot = resumed.register_connection(WebConnection(id="c"))
        views = [d for e, d in snapshot if e == "ctx_view"]
        assert views == [{**view, "task_id": "t-1"}]

    def test_main_view_is_not_restored_from_disk(self, tmp_path):
        """main 의 경계·요약은 복원된 ctx 가 진실이다 — 파일에서 되살리지 않는다."""
        r = WebRenderer(session_dir=str(tmp_path))
        r.note_system_prompt(_SECTIONS, turn=1)
        r.context_view({"gone": {"hidx": 7}, "summary": None})
        resumed = self._resumed(tmp_path)
        snapshot = resumed.register_connection(WebConnection(id="c"))
        assert not [d for e, d in snapshot if e == "ctx_view"]

    def test_torn_file_does_not_block_the_rest(self, tmp_path):
        r = WebRenderer(session_dir=str(tmp_path))
        r.note_system_prompt(_SECTIONS, turn=1)
        _run_scope(r, "t-1")
        (tmp_path / "prompt_views" / "0000.json").write_text("{not json")
        (tmp_path / "prompt_views" / "0001.json").write_text('["no scope"]')
        resumed = self._resumed(tmp_path)
        assert self.main_restored is True
        assert _prompt(resumed, "t-1")["ok"]

    def test_first_real_call_after_resume_diffs_against_the_restored_tail(
        self, tmp_path
    ):
        r = WebRenderer(session_dir=str(tmp_path))
        r.note_system_prompt(_SECTIONS, turn=4, tail=_tail(4))
        resumed = self._resumed(tmp_path)
        resumed.note_system_prompt(_SECTIONS, turn=1, tail=_tail(1))
        data = _prompt(resumed)
        assert data["turn"] == 1 and data["prev_turn"] == 4
        assert data["tail_prev"][0]["text"].startswith("turn 4/30")


class TestResumeWiring:
    def test_resume_restores_before_falling_back_to_startup_capture(self):
        """main 을 되살렸으면 시작 시점 캡처로 덮지 않는다 — 덮으면 마지막 호출의
        꼬리가 사라져 띠가 빈다."""
        import agent_cli.main as main_mod

        src = inspect.getsource(main_mod)
        assert "if not (is_resume and renderer.restore_prompt_views()):" in src
        guard = src.index("renderer.restore_prompt_views()")
        capture = src.index("capture_startup_system_prompt(\n", guard)
        replay = src.index("renderer.replay_session(ctx)")
        assert replay < guard < capture
