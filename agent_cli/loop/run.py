"""Agent loop: ReAct pattern with M1/M2 module integration."""

from __future__ import annotations

from agent_cli.constants import (
    AGENT_DEFAULT_TIMEOUT,
)
from agent_cli.context.manager import ContextManager

# Max shrink-and-retry attempts per turn when the server rejects the
# prompt as too long (flow 2 reactive recovery). Each attempt sheds more
# history via ``ContextManager.force_fit``; the bound stops a runaway
# loop when the cache cannot shrink enough or the server keeps rejecting.
from agent_cli.loop.core import AgentLoop
from agent_cli.providers.base import LLMProvider
from agent_cli.providers.capabilities import ModelCapabilities
from agent_cli.wire_formats import get as _get_wire_format


def run_loop(
    query: str,
    provider: LLMProvider,
    capabilities: ModelCapabilities,
    model: str,
    provider_name: str = "openai",
    base_url: str = "",
    api_key: str = "",
    max_turns: int = 0,
    verbose: bool = False,
    ctx: ContextManager | None = None,
    depth: int = 0,
    max_depth: int = 2,
    agent_timeout: int = AGENT_DEFAULT_TIMEOUT,
    active_tools: list[str] | None = None,
    session=None,  # SessionMeta — avoid circular import
    hooks_config: dict | None = None,
    skill_name: str = "",
    skill_stack: list[str] | None = None,
    agent_stack: list[str] | None = None,
    skill_args: str = "",
    graceful_interrupt: bool = False,
    stop_event=None,
    dequeue_user_message=None,
    route_message=None,
    query_author: str | None = None,
    agent_role: str = "",
    agent_name: str = "",
    mcp_manager=None,
    hook_runner=None,
    record_turns: bool = True,
    wire_format=None,
    compaction_enabled: bool = True,
    agent_registry=None,
    ask_handler=None,
    message_handler=None,
    peer_agents_section: str = "",
    origin_turn: str = "",
):
    """Run the agent loop with the given wire-format plugin. Returns ToolResult.

    ``wire_format`` accepts a registered plugin name (str) or a
    ``WireFormat`` instance directly. ``None`` falls back to the
    default wire format so existing callers don't need to change.
    """
    if isinstance(wire_format, str):
        wire_format = _get_wire_format(wire_format)
    return AgentLoop(
        query=query,
        provider=provider,
        capabilities=capabilities,
        model=model,
        provider_name=provider_name,
        base_url=base_url,
        api_key=api_key,
        max_turns=max_turns,
        verbose=verbose,
        ctx=ctx,
        depth=depth,
        max_depth=max_depth,
        agent_timeout=agent_timeout,
        active_tools=active_tools,
        session=session,
        hooks_config=hooks_config,
        skill_name=skill_name,
        skill_stack=skill_stack,
        agent_stack=agent_stack,
        skill_args=skill_args,
        graceful_interrupt=graceful_interrupt,
        dequeue_user_message=dequeue_user_message,
        route_message=route_message,
        query_author=query_author,
        stop_event=stop_event,
        agent_role=agent_role,
        agent_name=agent_name,
        mcp_manager=mcp_manager,
        hook_runner=hook_runner,
        record_turns=record_turns,
        wire_format=wire_format,
        compaction_enabled=compaction_enabled,
        agent_registry=agent_registry,
        ask_handler=ask_handler,
        message_handler=message_handler,
        peer_agents_section=peer_agents_section,
        origin_turn=origin_turn,
    ).run()
