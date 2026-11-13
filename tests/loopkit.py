"""테스트가 엔진을 직접 세우는 길 하나 (v10.131.2) — `tests/loop_ports.py`·`tests/loop_kinds.py` 의 짝.

프로덕션에서 엔진에 들어가는 길은 종류(`LoopKind.run`) 하나뿐이다. 테스트는 종류 없이
부품(``provider``·``capabilities``·``model``·``ports``·``ctx``·``dialect``)과 설정 몇 개로
엔진을 돌리거나 세운다 — 274곳이 같은 키워드 집합으로 부르므로 여기 도우미 둘이 받는다.

- `run_loop(...)` — 루프를 돌려 `ToolResult` 를 돌려준다.
- `make_loop(...)` — `AgentLoop` 를 세우기만 한다(메서드를 직접 부르는 테스트).
- `make_config(...)` — 협력자(`ToolBridge`·`TurnDispatcher`·`LLMCaller`·…)를 루프 없이 세우는
  테스트의 `LoopConfig` (v10.133.0 — 설정은 맨 종류에, 설정 객체엔 계산한 것만).

v10.132.0 부터 엔진은 ``(kind, start, ctx)`` 만 받는다. 여기서 **정적 바인딩**(부품 →
`ModelBinding`)과 **맨 종류**(`LoopKind` 기본 구현 — 포트·깊이·이름에서 정책을 유도)를
만들고, 런마다 다른 값은 `Start` 에 싣는다. 종전에 `AgentLoop.__init__` 의 ``binding is
None`` 분기와 `_bare_kind` 가 하던 일이다 — 제품은 그 길을 쓰지 않아 여기로 왔다.
"""

from __future__ import annotations

from agent_cli.loop.core import AgentLoop
from agent_cli.loop.kind import LoopKind, Start
from agent_cli.loop.run import run_loop as _run_loop
from agent_cli.loop.state import LoopConfig
from agent_cli.tools.result import ToolResult
from tests.loop_ports import make_ports

#: 맨 종류에 그대로 싣는 설정 — 이름이 곧 `LoopKind` 의 속성이다.
_KIND_ATTRS = frozenset(
    {
        "max_turns",
        "verbose",
        "depth",
        "max_depth",
        "session",
        "hooks_config",
        "skill_name",
        "skill_args",
        "skill_stack",
        "agent_stack",
        "agent_name",
        "agent_role",
        "peer_agents_section",
        "graceful_interrupt",
        "compaction_enabled",
        "record_turns",
        "stop_event",
    }
)


def static_binding(
    *,
    provider=None,
    capabilities=None,
    model: str = "",
    provider_name: str = "openai",
    base_url: str = "",
    api_key: str = "",
    dialect=None,
    ctx=None,
):
    """부품 → 정적 `ModelBinding`. 방언 폴백은 명시 > ctx > 기본 (G2 — ctx 와 루프의
    포맷이 갈리는 split-brain 방지: ctx 를 만든 방언을 루프도 쓴다)."""
    from agent_cli.dialects import get as get_dialect
    from agent_cli.providers.binding import ModelBinding

    if isinstance(dialect, str):
        dialect = get_dialect(dialect)
    if dialect is None:
        dialect = ctx.dialect if ctx is not None else get_dialect()
    return ModelBinding(
        provider=provider,
        provider_name=provider_name,
        base_url=base_url,
        api_key=api_key,
        model=model,
        capabilities=capabilities,
        dialect=dialect,
    )


def _split(
    *,
    query: str,
    provider,
    capabilities,
    model: str,
    ports,
    ctx,
    dialect,
    provider_name: str = "openai",
    base_url: str = "",
    api_key: str = "",
    binding=None,
    active_tools: list[str] | None = None,
    query_author: str | None = None,
    query_author_is_user: bool = True,
    query_request_id: str = "",
    user_requests: list | None = None,
    query_images: list[dict] | None = None,
    **attrs,
) -> tuple[LoopKind, Start]:
    unknown = set(attrs) - _KIND_ATTRS
    if unknown:
        raise TypeError(f"loopkit: unknown loop argument(s) {sorted(unknown)}")
    kind = LoopKind()
    kind.owner = ports.owner
    kind.ports = ports
    kind.tools = active_tools
    kind.binding = (
        binding
        if binding is not None
        else static_binding(
            provider=provider,
            capabilities=capabilities,
            model=model,
            provider_name=provider_name,
            base_url=base_url,
            api_key=api_key,
            dialect=dialect,
            ctx=ctx,
        )
    )
    for name, value in attrs.items():
        if value is not None:
            setattr(kind, name, value)
    start = Start(
        query=query,
        images=list(query_images or []),
        author=query_author,
        author_is_user=query_author_is_user,
        request_id=query_request_id,
        user_requests=list(user_requests or []),
    )
    return kind, start


def run_loop(
    *,
    query: str,
    provider=None,
    capabilities=None,
    model: str = "",
    ports=None,
    ctx=None,
    dialect=None,
    **attrs,
) -> ToolResult:
    """엔진을 한 번 돌린다 — ``attrs`` 는 설정(``max_turns``·``depth``·…)과 시작의 귀속
    (``query_author``·``query_request_id``·…)."""
    kind, start = _split(
        query=query,
        provider=provider,
        capabilities=capabilities,
        model=model,
        ports=ports,
        ctx=ctx,
        dialect=dialect,
        **attrs,
    )
    return _run_loop(kind, start, ctx)


def make_loop(
    *,
    query: str,
    provider=None,
    capabilities=None,
    model: str = "",
    ports=None,
    ctx=None,
    dialect=None,
    **attrs,
) -> AgentLoop:
    """`AgentLoop` 를 세운다(돌리지 않는다) — 인자는 `run_loop` 와 같다."""
    kind, start = _split(
        query=query,
        provider=provider,
        capabilities=capabilities,
        model=model,
        ports=ports,
        ctx=ctx,
        dialect=dialect,
        **attrs,
    )
    return AgentLoop(kind, start, ctx)


#: `make_config` 의 설정 이름과 기본값 — 종전 `LoopConfig` 22필드의 것(포트·도구·스택 제외).
_CONFIG_DEFAULTS = {
    "binding": None,
    "depth": 0,
    "max_depth": 2,
    "max_turns": 0,
    "skill_name": "",
    "skill_args": "",
    "hooks_config": None,
    "session": None,
    "agent_role": "",
    "graceful_interrupt": False,
    "compaction_enabled": True,
    "verbose": False,
}
_PORT_ARGS = (
    "mcp_manager",
    "hook_runner",
    "agent_registry",
    "message_handler",
    "questions",
)


def make_config(
    *,
    kind: LoopKind | None = None,
    owner: str = "main",
    tools_list: list | None = None,
    skill_stack: list | None = None,
    agent_stack: list | None = None,
    **attrs,
) -> LoopConfig:
    """협력자를 단독으로 세우는 테스트의 설정 — 이름과 기본값은 종전 `LoopConfig` 22필드의 것.

    설정·포트는 종류에 싣고(엔진이 읽는 자리), 설정 객체엔 도구 목록·스택만 둔다 — 루프가
    계산했을 값을 테스트가 그대로 준다(게이트·자기 이름 push 없음). 종전엔
    `LoopConfig.__post_init__` 이 같은 일을 했다.

    ``kind`` 를 주면 그 종류에 **명시한 설정만** 싣는다(포트는 그 종류의 것 — 포트 인자와 함께
    주면 `TypeError`). 없으면 맨 종류를 만든다: 포트는 준 것만 연결, 설정은 기본값."""
    unknown = set(attrs) - set(_CONFIG_DEFAULTS) - set(_PORT_ARGS)
    if unknown:
        raise TypeError(f"make_config: unknown argument(s) {sorted(unknown)}")
    tools = list(tools_list or [])
    if kind is None:
        kind = LoopKind()
        kind.owner = owner
        kind.ports = make_ports(
            owner=owner, **{n: attrs[n] for n in _PORT_ARGS if n in attrs}
        )
        kind.tools = tools or None
        kind.skill_stack = list(skill_stack or [])
        kind.agent_stack = list(agent_stack or [])
        settings = {
            **_CONFIG_DEFAULTS,
            **{n: attrs[n] for n in _CONFIG_DEFAULTS if n in attrs},
        }
    else:
        if owner != kind.owner:
            raise TypeError(f"make_config: owner comes from the kind ({kind.owner!r})")
        given = sorted(n for n in _PORT_ARGS if n in attrs)
        if given:
            raise TypeError(f"make_config: ports come from the kind, not {given}")
        settings = {n: attrs[n] for n in _CONFIG_DEFAULTS if n in attrs}
    for name, value in settings.items():
        setattr(kind, name, value)
    return LoopConfig(
        kind=kind,
        tools_list=tools,
        skill_stack=list(skill_stack or []),
        agent_stack=list(agent_stack or []),
    )
