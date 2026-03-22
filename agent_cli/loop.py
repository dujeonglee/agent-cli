"""Agent loop: ReAct pattern with M1/M2 module integration."""

from __future__ import annotations

import json
import re

from agent_cli.constants import OBS_SUCCESS, OBS_ERROR, OBS_ERROR_HINT

from agent_cli.context.manager import ContextManager
from agent_cli.context.overflow import check_preemptive_overflow, is_context_overflow
from agent_cli.parsing.react_parser import parse_react
from agent_cli.prompts.system_prompt import build_system_prompt
from agent_cli.providers.base import LLMProvider
from agent_cli.providers.compat import ModelCapabilities, needs_tool_action
from agent_cli.render import (
    render_context_dump,
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

# Checkpoint: first check at N iterations, then repeat every M iterations
# Shows last M tool calls at each checkpoint for LLM self-assessment
_CHECKPOINT_FIRST = 50
_CHECKPOINT_INTERVAL = 20
assert _CHECKPOINT_FIRST >= _CHECKPOINT_INTERVAL, (
    f"CHECKPOINT_FIRST ({_CHECKPOINT_FIRST}) must be >= "
    f"CHECKPOINT_INTERVAL ({_CHECKPOINT_INTERVAL})"
)


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
    recent_tool_history: list[dict] = []  # [{tool, input_summary, status}]

    while max_iter <= 0 or iteration < max_iter:
        iteration += 1
        if not quiet:
            render_iter_sep(iteration)

        # 1. Checkpoint: nudge LLM if running too long
        if iteration >= _CHECKPOINT_FIRST and (
            (iteration - _CHECKPOINT_FIRST) % _CHECKPOINT_INTERVAL == 0
        ):
            recent = recent_tool_history[-_CHECKPOINT_INTERVAL:]
            history_summary = "\n".join(
                f"  iter {h['iter']}: {h['tool']} → {h['result'][:100]}" for h in recent
            )
            checkpoint_msg = (
                f"[SYSTEM] CHECKPOINT — {iteration} iterations used.\n"
                f"Recent tool calls:\n{history_summary}\n\n"
                f"You MUST now do ONE of:\n"
                f'1. Return final answer: {{"thought": "...", "final_answer": "your result"}}\n'
                f"2. If genuinely incomplete, explain what SPECIFIC step remains and do it.\n\n"
                f"Do NOT call echo, cat, or any tool just to confirm completion.\n"
                f"Do NOT repeat previous tool calls.\n"
                f"If you already completed the task, provide final_answer IMMEDIATELY."
            )
            messages.append({"role": "user", "content": checkpoint_msg})
            if ctx:
                ctx.add("user", checkpoint_msg)
            if not quiet:
                render_status("running", f"Checkpoint at iteration {iteration}")

        # 2. Preemptive overflow check
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

        # 3. Context dump (verbose only)
        if verbose and not quiet:
            render_context_dump(messages, iteration)

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
            # Check if the only tool call is an echo (final answer pattern)
            if len(response.tool_calls) == 1:
                tc0 = response.tool_calls[0]
                echo_answer = _try_echo_as_final(tc0["name"], tc0["input"])
                if echo_answer:
                    if not quiet:
                        render_step("final", echo_answer, iteration)
                    return echo_answer

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

                # Execute tool (shared logic — tracks tools_called + history)
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
                    tools_called,
                    recent_tool_history,
                    iteration,
                )

                if not quiet:
                    render_step("observation", obs, iteration)
                observations.append({"tool_call": tc, "output": obs})

            # Repeated call detection
            if _detect_repeated_calls(recent_tool_history):
                last = recent_tool_history[-1]
                if not quiet:
                    render_status(
                        "error",
                        f"Repeated call detected: {last['tool']} called "
                        f"{_REPEAT_THRESHOLD} times with same input. Stopping.",
                    )
                return None

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

        # 9. Detect echo-as-final-answer (common small model pattern)
        echo_answer = _try_echo_as_final(parsed.action, parsed.action_input)
        if echo_answer:
            if not quiet:
                render_step("final", echo_answer, iteration)
            return echo_answer

        # 10. Tool execution (text parsing path)
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

            # Execute tool (shared logic — tracks tools_called + history)
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
                tools_called,
                recent_tool_history,
                iteration,
            )

            if not quiet:
                render_step("observation", observation, iteration)

            # Repeated call detection
            if _detect_repeated_calls(recent_tool_history):
                last = recent_tool_history[-1]
                if not quiet:
                    render_status(
                        "error",
                        f"Repeated call detected: {last['tool']} called "
                        f"{_REPEAT_THRESHOLD} times with same input. Stopping.",
                    )
                return None

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
    tools_called: list[str] | None = None,
    recent_tool_history: list[dict] | None = None,
    iteration: int = 0,
) -> str:
    """Execute a single tool, track history, and return observation string."""
    obs = _do_execute_tool(
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

    # Track tool usage
    if tools_called is not None:
        tools_called.append(tool_name)
    if recent_tool_history is not None:
        recent_tool_history.append(
            {
                "tool": tool_name,
                "input": _normalize_input(tool_input),
                "result": obs[:200],
                "iter": iteration,
            }
        )

    return obs


_REPEAT_THRESHOLD = 3  # Same tool+input N times → force exit

# Regex: simple echo with no pipes, redirects, subshells, or chaining
_ECHO_FINAL_RE = re.compile(
    r'^echo\s+["\']?(.+?)["\']?\s*$',
    re.DOTALL,
)


def _try_echo_as_final(tool_name: str, tool_input) -> str | None:
    """Detect 'echo ...' shell calls that are actually final answers.

    Small models often use shell echo instead of final_answer.
    Only matches simple echo commands with no pipes, redirects, or chaining.
    """
    if tool_name != "shell" or not isinstance(tool_input, dict):
        return None
    cmd = tool_input.get("command", "").strip()
    # Reject if command has pipes, redirects, semicolons, &&, || etc.
    if any(c in cmd for c in ["|", ">", "<", ";", "&&", "||", "`", "$("]):
        return None
    m = _ECHO_FINAL_RE.match(cmd)
    if m:
        return m.group(1).strip().strip("'\"")
    return None


def _normalize_input(tool_input) -> str:
    """Normalize tool input to a comparable string."""
    if isinstance(tool_input, dict):
        return json.dumps(tool_input, sort_keys=True, ensure_ascii=False)
    return str(tool_input)


def _detect_repeated_calls(history: list[dict]) -> bool:
    """Return True if last N calls are identical (same tool + same input)."""
    if len(history) < _REPEAT_THRESHOLD:
        return False
    recent = history[-_REPEAT_THRESHOLD:]
    first = (recent[0]["tool"], recent[0]["input"])
    return all((h["tool"], h["input"]) == first for h in recent)


def _do_execute_tool(
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
    """Core tool execution logic (no tracking)."""
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
