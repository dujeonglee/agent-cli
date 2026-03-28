"""Conditional system prompt builder adapted to model capabilities."""

from __future__ import annotations


from agent_cli.constants import SMALL_MODEL_CONTEXT

from agent_cli.providers.compat import ModelCapabilities
from agent_cli.tools.registry import get_tool_descriptions

BASE_ROLE_PROMPT = """\
You are an AI assistant that solves tasks step-by-step using available tools.

## Response Format (STRICT)
You MUST respond with a single JSON object and nothing else.
No markdown fences, no extra text — ONLY the JSON object.

{"thought": "your reasoning", "action": "tool_name", "action_input": {...}}

When the task is complete, use the "complete" tool:
{"thought": "summary", "action": "complete", "action_input": {"result": "your answer"}}"""

HASHLINE_GUIDE = """\
## Hashline Editing
read_file returns lines tagged as LINE#HASH:content, e.g.:
  1#VR:def hello():
  2#KT:    return "world"
  3#ZZ:

To edit, use edit_file with hashline refs copied EXACTLY from read_file output.
- replace single line:  {"op": "replace", "pos": "2#KT", "lines": ["    return \\"hello\\""]}
- replace range:        {"op": "replace", "pos": "1#VR", "end": "3#ZZ", "lines": ["def greet():", "    pass"]}
- delete lines:         {"op": "replace", "pos": "2#KT", "lines": []}
- insert after:         {"op": "append", "pos": "1#VR", "lines": ["    # new comment"]}
- insert before:        {"op": "prepend", "pos": "1#VR", "lines": ["# header"]}
- append to EOF:        {"op": "append", "lines": ["# end of file"]}

IMPORTANT: Always read the file first to get current hashline tags.
If a hash mismatch error occurs, re-read the file and retry with fresh tags.
Use write_file only for creating NEW files, not for editing existing ones."""

DELEGATE_GUIDE = """\
## Delegation Rules
- Only delegate tasks that are fully independent and self-contained
- The subagent has NO memory of this conversation
- Include ALL details: file paths, content, specific instructions
- NEVER use pronouns or references to prior context in the task
- Good: "Read /tmp/data.csv and count the number of rows"
- Bad: "Analyze the file we discussed earlier"
"""

DELEGATE_DESC = (
    "Delegate a self-contained subtask to an independent subagent. "
    "The subagent has NO context from this conversation — the task "
    "description must include ALL necessary details."
)
DELEGATE_SCHEMA = '{"task": "fully self-contained task description"}'

ARTIFACT_GUIDE = """\
## Scratchpad & Artifacts
A scratchpad is maintained with your task progress and decisions.
Each tool result is saved as an artifact file on disk.
The scratchpad Progress section shows what was done and where the artifact is stored.
If you need detailed results from a previous step, use read_artifact tool:
  - Read artifact: {"action": "read_artifact", "action_input": {"path": "artifacts/turn_0003.md"}}
  - List artifacts: {"action": "read_artifact", "action_input": {"mode": "list"}}
  - Search by tag: {"action": "read_artifact", "action_input": {"mode": "search", "tag": "filename.py"}}
Do NOT read all artifacts — only load what is needed for the current step."""

RULES = """\
## Rules
1. Always include "thought" in your JSON
2. "action_input" must match the tool's input schema
3. If observation shows error, fix parameters and retry
4. Respond in the same language as the user
5. Do NOT include "observation" — that is injected by the system
6. Output ONLY valid JSON, nothing else
7. NEVER invoke yourself recursively — do NOT run agent-cli, python agent-cli.py, or any command that starts this tool again via shell"""

SMALL_MODEL_HINTS = """\
## Important
Keep responses concise. Prefer short thoughts.
When reading files, request specific line ranges if possible.
Avoid reading entire large files."""

THINKING_MODEL_HINTS = """\
## Thinking Budget
Keep your internal reasoning brief and focused.
Prioritize outputting the JSON response over extended reasoning."""


def _format_tool_block(
    active_tools: list[str],
    include_delegate: bool = False,
) -> str:
    """Generate tool descriptions for the system prompt."""
    return get_tool_descriptions(active_tools, include_delegate)


def build_system_prompt(
    capabilities: ModelCapabilities,
    active_tools: list[str],
    include_delegate: bool = False,
    skill_stack: list[str] | None = None,
) -> str:
    """Build a system prompt adapted to model capabilities and active tools."""
    sections = [BASE_ROLE_PROMPT]

    # Tool descriptions (only active tools)
    tool_block = _format_tool_block(active_tools, include_delegate)
    sections.append(f"## Available Tools\n{tool_block}")

    # Conditional guidelines
    if "edit_file" in active_tools:
        sections.append(HASHLINE_GUIDE)

    if include_delegate:
        sections.append(DELEGATE_GUIDE)

    sections.append(ARTIFACT_GUIDE)
    sections.append(RULES)

    # Small model hints
    if capabilities.context_window <= SMALL_MODEL_CONTEXT:
        sections.append(SMALL_MODEL_HINTS)

    # Thinking model hints for small context + thinking
    if (
        capabilities.thinking_budget > 0
        and capabilities.context_window <= SMALL_MODEL_CONTEXT
    ):
        sections.append(THINKING_MODEL_HINTS)

    # Inject available skills for LLM auto-invocation
    skill_desc = build_skill_descriptions(exclude_names=skill_stack)
    if skill_desc:
        sections.append(skill_desc)

    return "\n\n".join(sections)


def build_skill_descriptions(
    skills: dict | None = None,
    exclude_names: list[str] | None = None,
) -> str:
    """Build skill descriptions for system prompt injection.

    Excludes skills with disable_model_invocation=True and
    skills in exclude_names (e.g. current skill_stack for recursion prevention).
    If skills is None, loads from disk.
    """
    if skills is None:
        try:
            from agent_cli.skills import load_skills

            skills = load_skills()
        except Exception:
            return ""

    if not skills:
        return ""

    excluded = set(exclude_names or [])

    lines = [
        "## Available Skills",
        "Use the run_skill tool to invoke these skills:",
        '  Example: {"action": "run_skill", "action_input": {"name": "skill-name", "arguments": "..."}}',
    ]
    for skill in skills.values():
        if skill.disable_model_invocation:
            continue
        if skill.name in excluded:
            continue
        hint = f" {skill.argument_hint}" if skill.argument_hint else ""
        lines.append(f"- `{skill.name}{hint}` — {skill.description}")

    # If all skills are disabled, return empty
    if len(lines) <= 2:
        return ""

    return "\n".join(lines)
