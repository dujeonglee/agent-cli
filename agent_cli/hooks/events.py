"""Hook lifecycle event constants."""

# Session lifecycle
ON_SESSION_START = "OnSessionStart"
ON_SESSION_END = "OnSessionEnd"

# Turn lifecycle
PRE_LLM_CALL = "PreLLMCall"
POST_LLM_CALL = "PostLLMCall"
ON_TURN_END = "OnTurnEnd"

# Tool lifecycle
PRE_TOOL_USE = "PreToolUse"
POST_TOOL_USE = "PostToolUse"

# Delegate lifecycle
ON_DELEGATE_START = "OnDelegateStart"
ON_DELEGATE_END = "OnDelegateEnd"

# Skill lifecycle
ON_SKILL_START = "OnSkillStart"
ON_SKILL_END = "OnSkillEnd"

ALL_EVENTS = frozenset(
    {
        ON_SESSION_START,
        ON_SESSION_END,
        PRE_LLM_CALL,
        POST_LLM_CALL,
        ON_TURN_END,
        PRE_TOOL_USE,
        POST_TOOL_USE,
        ON_DELEGATE_START,
        ON_DELEGATE_END,
        ON_SKILL_START,
        ON_SKILL_END,
    }
)

# Event name → function name mapping
EVENT_TO_FUNC = {
    ON_SESSION_START: "on_session_start",
    ON_SESSION_END: "on_session_end",
    PRE_LLM_CALL: "pre_llm_call",
    POST_LLM_CALL: "post_llm_call",
    ON_TURN_END: "on_turn_end",
    PRE_TOOL_USE: "pre_tool_use",
    POST_TOOL_USE: "post_tool_use",
    ON_DELEGATE_START: "on_delegate_start",
    ON_DELEGATE_END: "on_delegate_end",
    ON_SKILL_START: "on_skill_start",
    ON_SKILL_END: "on_skill_end",
}
