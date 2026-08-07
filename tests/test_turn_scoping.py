"""턴 스코핑(`--turn-scoping`) — 공유 세션에서 이 턴이 수행 중인 요청을
시스템 프롬프트에 못 박는 섹션.

병렬 계약에서 여러 사용자가 하나의 트랜스크립트를 공유하면, 구조적 귀속
(``reply_to``)이 정확해도 모델이 *내용상* 남의 동시 질문에 답할 수 있다.
이 섹션은 그 의미론적 혼선의 완화이고, 여기서는 두 가지를 고정한다:

1. 섹션이 **언제 붙는가** — 병렬(``origin_turn`` 있음) + 플래그 on 일 때만.
   직렬 모드에 붙으면 실행 중 주입된 질문(그건 수행 대상이다)을 모델이
   무시하게 만들 수 있으므로 게이트가 두 겹인 것이 요점이다.
2. 섹션이 **무엇을 담는가** — 턴 id, 작성자, 그리고 잘린 요청 인용.
"""

from unittest.mock import MagicMock

import pytest

from agent_cli.loop import AgentLoop, LoopConfig, SystemPromptSvc
from agent_cli.loop.prompt import _SCOPE_QUOTE_LIMIT
from agent_cli.providers.capabilities import ModelCapabilities

_SCOPE_TITLE = "Turn Scope"


@pytest.fixture
def caps():
    return ModelCapabilities(
        context_window=32768,
        max_output_tokens=4096,
        supports_thinking=False,
        thinking_budget=0,
    )


def _scope_text(svc: SystemPromptSvc) -> str | None:
    for name, text in svc.sections:
        if name == _SCOPE_TITLE:
            return text
    return None


class TestSetTurnScope:
    """SystemPromptSvc 단독 — 섹션 내용과 rebuild 생존."""

    def test_section_names_turn_author_and_request(self):
        svc = SystemPromptSvc(LoopConfig(tools_list=["shell"]), ctx=None)
        svc.set_turn_scope("t3", "bob", "rename the parser module")
        svc.rebuild()
        text = _scope_text(svc)
        assert text is not None
        assert "t3" in text
        assert "from bob" in text
        assert "rename the parser module" in text
        # 섹션은 join 된 system 문자열에도 반드시 실려야 한다 — 프롬프트에
        # 안 실리면 이 기능은 존재하지 않는 것과 같다.
        assert text in svc.system

    def test_anonymous_author_omits_attribution(self):
        svc = SystemPromptSvc(LoopConfig(), ctx=None)
        svc.set_turn_scope("t1", None, "hello")
        svc.rebuild()
        text = _scope_text(svc)
        assert "from" not in text.split("is:")[0].split("whose request")[1]

    def test_long_request_is_truncated(self):
        svc = SystemPromptSvc(LoopConfig(), ctx=None)
        svc.set_turn_scope("t1", "amy", "x" * (_SCOPE_QUOTE_LIMIT + 500))
        svc.rebuild()
        text = _scope_text(svc)
        assert "…" in text
        # 인용은 상한 + 말줄임표를 넘지 않는다 (컨텍스트 예산 보호).
        assert "x" * (_SCOPE_QUOTE_LIMIT + 1) not in text

    def test_whitespace_is_normalised(self):
        """줄바꿈이 살아 있으면 인용 블록(`> `)이 한 줄만 인용하고 나머지가
        평범한 지시문으로 새어 나간다."""
        svc = SystemPromptSvc(LoopConfig(), ctx=None)
        svc.set_turn_scope("t1", "amy", "line one\nline two\n\n  line three")
        svc.rebuild()
        text = _scope_text(svc)
        assert "> line one line two line three" in text

    def test_scope_survives_rebuild(self):
        """DIRECTIVE.md 편집 등으로 rebuild 가 다시 돌아도 스코프는 남는다 —
        rebuild 는 섹션을 처음부터 다시 만들기 때문에 명시적 재부착이 없으면
        조용히 사라진다."""
        svc = SystemPromptSvc(LoopConfig(), ctx=None)
        svc.set_turn_scope("t9", "amy", "do the thing")
        svc.rebuild()
        svc.rebuild()
        assert _scope_text(svc) is not None
        assert svc.system.count("You are serving turn t9") == 1

    def test_scope_survives_hook_sections(self):
        """훅 섹션 적용은 'Hook: ' 접두 섹션만 갈아끼운다 — 스코프는 정적
        섹션이므로 보존돼야 한다."""
        svc = SystemPromptSvc(LoopConfig(), ctx=None)
        svc.set_turn_scope("t2", "amy", "do the thing")
        svc.rebuild()
        hook_ctx = MagicMock()
        hook_ctx.system_sections = {"Extra": "extra content"}
        svc.apply_hook_sections(hook_ctx)
        assert _scope_text(svc) is not None
        assert "extra content" in svc.system

    def test_absent_by_default(self):
        svc = SystemPromptSvc(LoopConfig(), ctx=None)
        svc.rebuild()
        assert _scope_text(svc) is None


class TestAgentLoopGating:
    """AgentLoop — 두 겹 게이트(플래그 + origin_turn)."""

    def _loop(self, caps, **kw):
        loop = AgentLoop(
            query="my own request",
            provider=MagicMock(),
            capabilities=caps,
            model="m",
            query_author="amy",
            **kw,
        )
        loop._setup()
        return loop

    def test_applied_when_parallel_and_enabled(self, caps):
        loop = self._loop(caps, origin_turn="t7", turn_scoping=True)
        assert "You are serving turn t7" in loop.system
        assert "my own request" in loop.system

    def test_absent_without_origin_turn(self, caps):
        """직렬 계약(origin_turn 없음)에는 걸지 않는다 — 거기서는 실행 중
        주입된 질문이 오히려 이 턴이 수행할 대상이다."""
        loop = self._loop(caps, origin_turn="", turn_scoping=True)
        assert "You are serving turn" not in loop.system

    def test_absent_when_flag_off(self, caps):
        """기본 off — 효과가 측정되기 전까지 출하 동작을 바꾸지 않는다."""
        loop = self._loop(caps, origin_turn="t7", turn_scoping=False)
        assert "You are serving turn" not in loop.system

    def test_default_is_off(self, caps):
        loop = self._loop(caps, origin_turn="t7")
        assert "You are serving turn" not in loop.system
