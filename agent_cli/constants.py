"""Shared constants for agent-cli."""

# ── Timeout values (seconds) ──────────────────
SHELL_COMMAND_TIMEOUT = 30
LLM_API_TIMEOUT = 1200
DELEGATE_DEFAULT_TIMEOUT = 300

# ── Token estimation ─────────────────────────
CHARS_PER_TOKEN = 4
OVERFLOW_RESERVE_TOKENS = 2048

# ── Observation message templates ──────────────
OBS_SUCCESS = "STATUS: success\nRESULT:\n{result}"
OBS_ERROR = "STATUS: error\nERROR: {error}"
OBS_ERROR_HINT = "STATUS: error\nERROR: {error}\nHINT: {hint}"

# ── System-injected user messages ───────────────
# These get persisted as role=user in history.jsonl but are NOT actual
# user queries — they're loop-emitted notifications/hints. `recent_exchanges`
# uses these prefixes to skip them when surfacing the resume preview.
RETRY_HINT_NO_JSON = (
    "Your response was not valid JSON. "
    "Output ONLY a JSON object: "
    '{"thought": "...", "action": "tool_name", "action_input": {...}}. '
    "No markdown fences, no extra text."
)
RETRY_HINT_NO_ACTION = (
    "Your JSON was parsed but has no action. "
    "You MUST include an action. Either use a tool: "
    '{"thought": "...", "action": "tool_name", "action_input": {...}} '
    "or complete the task: "
    '{"thought": "...", "action": "complete", "action_input": {"result": "..."}}'
)
INTERRUPT_NOTICE = "⚡ User interrupted. Waiting for new instructions."
SYSTEM_USER_PREFIXES: tuple[str, ...] = (
    "Your response was not valid JSON.",
    "Your JSON was parsed but has no action.",
    "⚡ User interrupted.",
)

# Cap echoed segment length. Asymmetric truncation: content keeps the
# head (structural drift markers like `thought:` / `shell(...)` appear
# at the start), thinking keeps the tail (self-correction beats like
# "let me try X this time" appear at the end).
_ECHO_HEAD = 400
_ECHO_TAIL = 400


def _truncate_head(text: str, n: int = _ECHO_HEAD) -> str:
    """Strip and head-truncate (keep first n chars).

    Used for content echo so the model sees its own structural drift
    markers (YAML-style `thought:` / `action:`, function-call
    `tool(args)` syntax, etc.) which typically lead the failing
    output.
    """
    cleaned = text.strip() if text else ""
    if not cleaned:
        return ""
    if len(cleaned) > n:
        cleaned = cleaned[:n] + "..."
    return cleaned


def _truncate_tail(text: str, n: int = _ECHO_TAIL) -> str:
    """Strip and tail-truncate (keep last n chars).

    Used for thinking echo so the self-correction beat ("I keep
    failing to provide valid JSON. Let me try X this time.") at the
    tail is preserved.
    """
    cleaned = text.strip() if text else ""
    if not cleaned:
        return ""
    if len(cleaned) > n:
        cleaned = "..." + cleaned[-n:]
    return cleaned


def format_no_json_retry(
    *,
    prior_content: str = "",
    prior_thinking: str = "",
) -> str:
    """Build the retry hint shown when an LLM response failed to parse as JSON.

    Echoes the model's prior output back at it under "failure
    grounding" — abstract "your response was invalid" gets concretized
    as "here is what you wrote, that was invalid". Two channels
    available:

    - ``prior_content``: the actual emitted text (from
      ``LLMResponse.content``). Head-truncated. This is the
      *primary* grounding signal — the model can see its own
      structural drift (YAML-style keys, function-call syntax, bare
      prose, etc.) and self-diagnose.
    - ``prior_thinking``: provider-side reasoning channel
      (Ollama ``message.thinking``, Anthropic thinking blocks, vLLM
      ``reasoning_content``, or ``<think>`` tags inside content,
      extracted by parse_react). Tail-truncated. Captures
      self-correction beats when present.

    Falls back to the static RETRY_HINT_NO_JSON when both channels
    are empty — preserves the existing retry path for providers that
    expose neither (plain OpenAI Chat Completions, etc.).

    Keyword-only to avoid positional ambiguity between the two
    similar string args.
    """
    content_quote = _truncate_head(prior_content)
    thinking_quote = _truncate_tail(prior_thinking)
    if not content_quote and not thinking_quote:
        return RETRY_HINT_NO_JSON

    parts: list[str] = ["Your response was not valid JSON.", ""]
    if content_quote:
        parts.extend(["Your prior output:", "---", content_quote, "---", ""])
    if thinking_quote:
        parts.extend(["Your prior reasoning:", "---", thinking_quote, "---", ""])
    parts.append(
        "Honor that. Output ONLY a JSON object: "
        '{"thought": "...", "action": "tool_name", "action_input": {...}}. '
        "No markdown fences, no extra text."
    )
    return "\n".join(parts)


def format_no_action_retry(
    *,
    prior_content: str = "",
    prior_thinking: str = "",
) -> str:
    """Build the retry hint shown when JSON parsed but no action was provided.

    Same failure-grounding rationale as ``format_no_json_retry``.
    """
    content_quote = _truncate_head(prior_content)
    thinking_quote = _truncate_tail(prior_thinking)
    if not content_quote and not thinking_quote:
        return RETRY_HINT_NO_ACTION

    parts: list[str] = ["Your JSON was parsed but has no action.", ""]
    if content_quote:
        parts.extend(["Your prior output:", "---", content_quote, "---", ""])
    if thinking_quote:
        parts.extend(["Your prior reasoning:", "---", thinking_quote, "---", ""])
    parts.append(
        "Honor that. You MUST include an action. Either use a tool: "
        '{"thought": "...", "action": "tool_name", "action_input": {...}} '
        "or complete the task: "
        '{"thought": "...", "action": "complete", "action_input": {"result": "..."}}'
    )
    return "\n".join(parts)
