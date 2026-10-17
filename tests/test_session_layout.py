"""세션 디렉토리 배치는 `context.session` 이 한 번 선언한다 (v10.42.0).

종전엔 만드는 쪽 셋(main ctx·팀원·delegate/스킬)이 각자 이름을 박았고, 읽는 쪽
(`history` 도구)은 배치를 모른 채 `rglob` 로 "세션 밑은 전부 대화" 라고 추정했다.
하니스 작업(`harness/`)이 생기며 그 추정이 틀렸다 — 하니스가 돌린 런은 모델의
대화가 아니다. 그래서 이름은 상수로, "무엇이 대화인가" 는 함수로, 모두 한 모듈에
둔다. 여기서 고정하는 것: 만드는 쪽이 그 상수를 쓴다, 읽는 쪽이 그 함수를 쓴다,
그리고 세션 밑의 모든 history 파일은 대화이거나 하니스 밑이다(조용한 누락 없음).
"""

from __future__ import annotations

import json
from pathlib import Path

from agent_cli.context.manager import ContextManager
from agent_cli.context.session import (
    AGENTS_DIR,
    HARNESS_DIR,
    HISTORY_NAME,
    RUN_PREFIX,
    SKILL_PREFIX,
    conversation_history_files,
    harness_run_dir,
)


def _history(path: Path, text: str) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    hp = path / HISTORY_NAME
    hp.write_text(
        json.dumps({"role": "user", "content": text, "kind": "query"}) + "\n",
        encoding="utf-8",
    )
    return hp


def _session(tmp_path: Path) -> tuple[Path, dict[str, Path]]:
    """실제 모양의 세션: main, 팀원, delegate(중첩 포함), 스킬, 하니스 작업."""
    root = tmp_path / "1700000000"
    files = {
        "main": _history(root, "main q"),
        "agent": _history(root / AGENTS_DIR / "agt-1", "agent q"),
        "run": _history(root / f"{RUN_PREFIX}_task_ab12_20260101T000000000", "run q"),
        "nested": _history(
            root
            / f"{RUN_PREFIX}_task_ab12_20260101T000000000"
            / f"{RUN_PREFIX}_inner_cd34_20260101T000001000",
            "nested q",
        ),
        "skill": _history(root / f"{SKILL_PREFIX}_plan_ef56_20260101T000002000", "s"),
        "harness": _history(harness_run_dir(root, "auditor", "1-9"), "Audit main"),
    }
    return root, files


class TestLayoutIsDeclaredOnce:
    def test_makers_use_the_declared_names(self, tmp_path):
        """팀원 자리·delegate/스킬 접두·하니스 자리 — 만드는 쪽 넷이 같은 상수를 쓴다."""
        from agent_cli.loop import skill_invoke
        from agent_cli.subagent import agents_live, report

        assert agents_live.AGENTS_DIR is AGENTS_DIR
        assert report._generate_run_dir_name("x").startswith(f"{RUN_PREFIX}_x_")
        assert "SKILL_PREFIX" in Path(skill_invoke.__file__).read_text(encoding="utf-8")
        assert harness_run_dir(tmp_path, "auditor", "3-7") == (
            tmp_path / HARNESS_DIR / "auditor" / "3-7"
        )

    def test_every_history_file_is_conversation_or_harness(self, tmp_path):
        """조용한 누락 방지: 세션 밑 history 파일은 둘 중 하나다."""
        root, _files = _session(tmp_path)
        conv = set(conversation_history_files(root))
        harness = {hp for hp in root.rglob(HISTORY_NAME) if HARNESS_DIR in hp.parts}
        assert conv | harness == set(root.rglob(HISTORY_NAME))
        assert conv & harness == set()

    def test_conversation_excludes_only_the_harness_subtree(self, tmp_path):
        root, files = _session(tmp_path)
        conv = conversation_history_files(root)
        assert files["harness"] not in conv
        for key in ("main", "agent", "run", "nested", "skill"):
            assert files[key] in conv, key
        assert conv == sorted(conv)  # 결정적 순서


class TestHistoryToolReadsTheDeclaredConversation:
    def test_harness_rows_never_enter_the_table(self, tmp_path):
        """실측(4차 A-1 세션): 포함하면 감사 과제가 사용자 요청 모양(`kind='query'`)
        으로 main 의 무필터 질의에 섞였다. 감사 결과 자체는 main 파일의 메일
        관찰에 있으니 잃는 것이 없다."""
        from agent_cli.tools.history import tool_history

        ctx = ContextManager(tmp_path / "s", max_context_tokens=10_000)
        ctx.add({"role": "user", "content": "main q"})
        agent_ctx = ContextManager(
            ctx.session_dir / AGENTS_DIR / "agt-1", max_context_tokens=10_000
        )
        agent_ctx.add({"role": "user", "content": "agent q"})
        audit_ctx = ContextManager(
            harness_run_dir(ctx.session_dir, "auditor", "1-1"),
            max_context_tokens=10_000,
        )
        audit_ctx.add({"role": "user", "content": "Audit main's run"})
        res = tool_history(
            {"query": "SELECT loc, substr(text,1,12) AS t FROM history ORDER BY loc"},
            session_dir=ctx.session_dir,
        )
        assert res.success, res.error
        assert "agents/agt-1/history.jsonl" in res.output  # 팀원의 일은 세션의 일
        assert "history.jsonl:1 | t=main q" in res.output
        assert "harness" not in res.output and "Audit" not in res.output
