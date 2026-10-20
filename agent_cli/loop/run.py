"""Agent loop: ReAct pattern with M1/M2 module integration."""

from __future__ import annotations

from agent_cli.context.manager import ContextManager

# Max shrink-and-retry attempts per turn when the server rejects the
# prompt as too long (flow 2 reactive recovery). Each attempt sheds more
# history via ``ContextManager.force_fit``; the bound stops a runaway
# loop when the cache cannot shrink enough or the server keeps rejecting.
from agent_cli.loop.core import AgentLoop
from agent_cli.loop.ports import LoopPorts
from agent_cli.providers.base import LLMProvider
from agent_cli.providers.capabilities import ModelCapabilities


def run_loop(
    query: str,
    provider: LLMProvider | None = None,
    capabilities: ModelCapabilities | None = None,
    model: str = "",
    ports: LoopPorts | None = None,
    provider_name: str = "openai",
    base_url: str = "",
    api_key: str = "",
    *,
    binding=None,
    max_turns: int = 0,
    verbose: bool = False,
    ctx: ContextManager | None = None,
    depth: int = 0,
    max_depth: int = 2,
    active_tools: list[str] | None = None,
    session=None,  # SessionMeta — avoid circular import
    hooks_config: dict | None = None,
    skill_name: str = "",
    skill_stack: list[str] | None = None,
    agent_stack: list[str] | None = None,
    skill_args: str = "",
    graceful_interrupt: bool = False,
    stop_event=None,
    query_author: str | None = None,
    query_author_is_user: bool = True,
    query_request_id: str = "",
    user_requests: list | None = None,
    agent_role: str = "",
    agent_name: str = "",
    record_turns: bool = True,
    dialect=None,
    compaction_enabled: bool = True,
    peer_agents_section: str = "",
    query_images: list[dict] | None = None,
):
    """Run the agent loop. Returns ToolResult — `AgentLoop(...).run()` 의 함수 꼴.

    세션 코드는 ``binding``(`providers.binding.ModelBinding`)을 넘긴다; 부품
    (``provider``/``capabilities``/``model``/…/``dialect``)은 바인딩 없이 부르는
    쪽을 위한 것으로, 정적 바인딩 조립은 `AgentLoop.__init__` 한 곳이 한다.
    """
    return AgentLoop(
        query=query,
        provider=provider,
        capabilities=capabilities,
        model=model,
        ports=ports,
        provider_name=provider_name,
        base_url=base_url,
        api_key=api_key,
        binding=binding,
        max_turns=max_turns,
        verbose=verbose,
        ctx=ctx,
        depth=depth,
        max_depth=max_depth,
        active_tools=active_tools,
        session=session,
        hooks_config=hooks_config,
        skill_name=skill_name,
        skill_stack=skill_stack,
        agent_stack=agent_stack,
        skill_args=skill_args,
        graceful_interrupt=graceful_interrupt,
        query_author=query_author,
        query_author_is_user=query_author_is_user,
        query_request_id=query_request_id,
        user_requests=user_requests,
        stop_event=stop_event,
        agent_role=agent_role,
        agent_name=agent_name,
        record_turns=record_turns,
        dialect=dialect,
        compaction_enabled=compaction_enabled,
        peer_agents_section=peer_agents_section,
        query_images=query_images,
    ).run()
