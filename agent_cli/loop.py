"""Agent loop: ReAct pattern with M1/M2 module integration."""

from __future__ import annotations

import json

from agent_cli.constants import OBS_SUCCESS, OBS_ERROR, OBS_ERROR_HINT

from agent_cli.context.manager import ContextManager
from agent_cli.context.overflow import check_preemptive_overflow, is_context_overflow
from agent_cli.parsing.react_parser import parse_react
from agent_cli.prompts.system_prompt import build_system_prompt
from agent_cli.providers.base import LLMProvider
from agent_cli.providers.compat import ModelCapabilities, needs_tool_action
from agent_cli.render import (
    render_header,
    render_iter_sep,
    render_raw,
    render_status,
    render_step,
)
from agent_cli.tools import TOOLS, execute_tool, validate_tool_input
from agent_cli.tools.delegate import tool_delegate
from agent_cli.tools.registry import convert_to_anthropic_tools, convert_to_openai_tools
from agent_cli.tools.truncation import get_truncation_config, truncate_output


def run_loop(
    query: str,
    provider: LLMProvider,
    capabilities: ModelCapabilities,
    model: str,
    provider_name: str = "ollama",
    base_url: str = "",
    api_key: str = "",
    max_iter: int = 0,
    verbose: bool = False,
    ctx: ContextManager | None = None,
    quiet: bool = False,
    depth: int = 0,
    max_depth: int = 2,
    delegate_timeout: int = 300,
    active_tools: list[str] | None = None,
    plan_context: str | None = None,
) -> str | None:
    """Run the ReAct agent loop.

    Returns the final answer string, or None if max iterations reached.
    """
    include_delegate = depth < max_depth
    tools_list = active_tools or list(TOOLS.keys())

    system = build_system_prompt(
        capabilities=capabilities,
        active_tools=tools_list,
        include_delegate=include_delegate,
        plan_context=plan_context,
    )

    if not quiet:
        render_header(provider_name, model, max_iter)

    # Message setup
    if ctx:
        ctx.add("user", query)
        messages = ctx.get_messages()
    else:
        messages = [{"role": "user", "content": query}]

    iteration = 0
    tools_called: list[str] = []
    overflow_retried = False

    while max_iter <= 0 or iteration < max_iter:
        iteration += 1
        if not quiet:
            render_iter_sep(iteration)

        # 1. Preemptive overflow check
        if check_preemptive_overflow(messages, capabilities):
            if ctx:
                render_status("running", "Compressing context (preemptive)...")
                ctx.force_compress()
                messages = ctx.get_messages()
            else:
                # Single-shot: trim oldest observations
                messages = _trim_old_observations(messages, capabilities)

        # 2. Prepare tools for native tool calling
        call_kwargs: dict = {}
        if capabilities.supports_tool_calling:
            if provider_name == "anthropic":
                call_kwargs["tools"] = convert_to_anthropic_tools(
                    tools_list, include_delegate=include_delegate
                )
            elif provider_name == "openai":
                call_kwargs["tools"] = convert_to_openai_tools(
                    tools_list, include_delegate=include_delegate
                )

        # 3. LLM call
        try:
            response = provider.call(
                messages=messages,
                system=system,
                model=model,
                capabilities=capabilities,
                **call_kwargs,
            )
            llm_text = response.content
        except Exception as e:
            if is_context_overflow(str(e)):
                if ctx and not overflow_retried:
                    render_status("running", "Context overflow — compressing...")
                    ctx.force_compress()
                    messages = ctx.get_messages()
                    overflow_retried = True
                    iteration -= 1
                    continue
            if not quiet:
                render_step("error", f"LLM call failed: {e}", iteration)
            return None

        if not quiet:
            render_raw(llm_text, iteration, verbose)

        # 4. Native tool calling path (Anthropic/OpenAI)
        if response.tool_calls:
            observations = []
            for tc in response.tool_calls:
                tool_name = tc["name"]
                tool_input = tc["input"]

                if not quiet:
                    render_step(
                        "action",
                        "",
                        iteration,
                        tool_name=tool_name,
                        tool_input=json.dumps(tool_input, ensure_ascii=False)
                        if isinstance(tool_input, dict)
                        else str(tool_input),
                    )

                # Execute tool (shared logic)
                obs = _execute_single_tool(
                    tool_name,
                    tool_input,
                    tools_list,
                    include_delegate,
                    capabilities,
                    provider_name,
                    model,
                    base_url,
                    api_key,
                    delegate_timeout,
                )

                if not quiet:
                    render_step("observation", obs, iteration)
                observations.append({"tool_call": tc, "output": obs})
                tools_called.append(tool_name)

            # Format messages based on provider
            new_msgs = _format_tool_call_messages(provider_name, response, observations)
            messages.extend(new_msgs)
            if ctx:
                for m in new_msgs:
                    ctx.add(
                        m["role"],
                        m.get("content", "")
                        if isinstance(m.get("content"), str)
                        else json.dumps(m.get("content", "")),
                    )
            continue

        # 5. Text parsing path (Ollama, fallback)
        parsed = parse_react(llm_text)

        # 6. Thought
        if parsed.thought and not quiet:
            render_step("thought", parsed.thought, iteration)

        # 7. Final answer
        if parsed.final_answer:
            if not quiet:
                render_step("final", parsed.final_answer, iteration)

            # Fulfillment guard
            if not tools_called and needs_tool_action(query):
                nudge = (
                    "You provided a final_answer, but the task likely requires "
                    "tool actions (file operations, shell commands, etc.). "
                    "Please use the appropriate tools first, then provide final_answer."
                )
                messages.append({"role": "assistant", "content": llm_text})
                messages.append({"role": "user", "content": nudge})
                if ctx:
                    ctx.add("assistant", llm_text)
                    ctx.add("user", nudge)
                continue

            return parsed.final_answer

        # 8. Handle LLM sending action="final_answer" (common mistake)
        if parsed.action == "final_answer":
            answer = ""
            if isinstance(parsed.action_input, dict):
                answer = parsed.action_input.get(
                    "final_answer",
                    parsed.action_input.get("answer", str(parsed.action_input)),
                )
            elif isinstance(parsed.action_input, str):
                answer = parsed.action_input
            if answer:
                if not quiet:
                    render_step("final", answer, iteration)
                return answer

        # 9. Tool execution (text parsing path)
        if parsed.action:
            tool_name = parsed.action
            tool_input = parsed.action_input or {}

            if not quiet:
                render_step(
                    "action",
                    "",
                    iteration,
                    tool_name=tool_name,
                    tool_input=json.dumps(tool_input, ensure_ascii=False)
                    if isinstance(tool_input, dict)
                    else str(tool_input),
                )

            # Execute tool (shared logic)
            observation = _execute_single_tool(
                tool_name,
                tool_input,
                tools_list,
                include_delegate,
                capabilities,
                provider_name,
                model,
                base_url,
                api_key,
                delegate_timeout,
            )
            tools_called.append(tool_name)

            if not quiet:
                render_step("observation", observation, iteration)

            # Inject observation
            obs_msg = f"Observation: {observation}\n\nContinue with the next step. Respond with JSON only."
            messages.append({"role": "assistant", "content": llm_text})
            messages.append({"role": "user", "content": obs_msg})
            if ctx:
                ctx.add("assistant", llm_text)
                ctx.add("user", obs_msg)
            continue

        # 9. Parse failure — retry with format reminder
        if not quiet:
            render_status(
                "error",
                f"Invalid response (parse_stage={parsed.parse_stage}). Retrying...",
                iteration,
            )
        retry_msg = (
            "Your response was not valid JSON. "
            "Output ONLY a JSON object with either "
            '{"thought": "...", "action": "...", "action_input": {...}} '
            'or {"thought": "...", "final_answer": "..."}. '
            "No markdown fences, no extra text."
        )
        messages.append({"role": "assistant", "content": llm_text})
        messages.append({"role": "user", "content": retry_msg})
        if ctx:
            ctx.add("assistant", llm_text)
            ctx.add("user", retry_msg)
        iteration -= 1  # Don't count format retries

    if not quiet:
        render_status("error", f"Max iterations ({max_iter}) reached.")
    return None


def _execute_single_tool(
    tool_name: str,
    tool_input,
    tools_list: list[str],
    include_delegate: bool,
    capabilities: ModelCapabilities,
    provider_name: str = "",
    model: str = "",
    base_url: str = "",
    api_key: str = "",
    delegate_timeout: int = 300,
) -> str:
    """Execute a single tool and return observation string. Shared by both paths."""
    if tool_name == "delegate" and include_delegate:
        try:
            return tool_delegate(
                args=tool_input
                if isinstance(tool_input, dict)
                else {"task": str(tool_input)},
                provider=provider_name,
                model=model,
                base_url=base_url,
                api_key=api_key,
                timeout=delegate_timeout,
            )
        except Exception as e:
            return OBS_ERROR_HINT.format(
                error=e, hint="Check task description and try again."
            )

    if tool_name in tools_list:
        valid, err = validate_tool_input(tool_name, tool_input)
        if not valid:
            return OBS_ERROR_HINT.format(error=err, hint="Fix action_input and retry.")
        try:
            raw = execute_tool(tool_name, tool_input)
            cfg = get_truncation_config(capabilities, tool_name)
            return OBS_SUCCESS.format(result=truncate_output(raw, cfg))
        except Exception as e:
            return OBS_ERROR_HINT.format(
                error=e, hint="Check parameters and try again."
            )

    avail = ", ".join(TOOLS) + (", delegate" if include_delegate else "")
    return OBS_ERROR.format(error=f"Unknown tool '{tool_name}'. Available: {avail}")


def _format_tool_call_messages(
    provider_name: str,
    response,
    observations: list[dict],
) -> list[dict]:
    """Format tool call results as provider-specific messages."""
    if provider_name == "anthropic":
        return _format_anthropic_tool_messages(response, observations)
    elif provider_name == "openai":
        return _format_openai_tool_messages(response, observations)
    else:
        # Fallback: generic observation format
        obs_text = "\n\n".join(o["output"] for o in observations)
        return [
            {"role": "assistant", "content": response.content or ""},
            {
                "role": "user",
                "content": f"Observation: {obs_text}\n\nContinue. Respond with JSON only.",
            },
        ]


def _format_anthropic_tool_messages(response, observations: list[dict]) -> list[dict]:
    """Format for Anthropic: assistant content blocks + tool_result user message."""
    # Build assistant message with text + tool_use blocks
    assistant_content = []
    if response.content:
        assistant_content.append({"type": "text", "text": response.content})
    for tc in response.tool_calls or []:
        assistant_content.append(
            {
                "type": "tool_use",
                "id": tc["id"],
                "name": tc["name"],
                "input": tc["input"],
            }
        )

    # Build tool result user message
    tool_results = []
    for obs in observations:
        tool_results.append(
            {
                "type": "tool_result",
                "tool_use_id": obs["tool_call"]["id"],
                "content": obs["output"],
            }
        )

    return [
        {"role": "assistant", "content": assistant_content},
        {"role": "user", "content": tool_results},
    ]


def _format_openai_tool_messages(response, observations: list[dict]) -> list[dict]:
    """Format for OpenAI: assistant with tool_calls + tool role messages."""
    # Build assistant message with tool_calls
    assistant_msg = {
        "role": "assistant",
        "content": response.content or None,
        "tool_calls": [
            {
                "id": tc["id"],
                "type": "function",
                "function": {
                    "name": tc["name"],
                    "arguments": json.dumps(tc["input"]),
                },
            }
            for tc in response.tool_calls or []
        ],
    }

    # Build tool result messages
    result_msgs = [
        {
            "role": "tool",
            "tool_call_id": obs["tool_call"]["id"],
            "content": obs["output"],
        }
        for obs in observations
    ]

    return [assistant_msg] + result_msgs


def _trim_old_observations(
    messages: list[dict], capabilities: ModelCapabilities
) -> list[dict]:
    """Trim oldest observation messages in single-shot mode to fit context."""
    if len(messages) <= 3:
        return messages
    # Keep first (query) and last 4 messages, trim middle
    keep_last = min(4, len(messages) - 1)
    return [messages[0]] + messages[-keep_last:]
