"""Context compression prompts for structured summarization.

Designed to complement the scratchpad (which tracks Progress / Decisions / Open Questions).
The summary focuses on context that the scratchpad does NOT capture:
goal, working state, and conversation direction.

Files Touched is extracted by rule-based parsing (not LLM) for accuracy.
"""

SUMMARIZATION_PROMPT = """\
Summarize the following conversation into these sections.
Be concise. Preserve error messages and command outputs exactly.

Constraints:
- Progress and Decisions are tracked separately in a scratchpad. Do not duplicate them here.
- Files Touched is extracted automatically — do not include it.
Focus on context the scratchpad cannot capture.

## Goal
What the user is trying to accomplish (1-2 sentences).

## Working State
Current errors, blockers, intermediate results, or variable values
that the next assistant turn needs to continue work.
Preserve exact error messages but summarize long tracebacks.

## Conversation Direction
What was agreed on next, the user's intent, and any stated preferences
for how to proceed.

Reply with ONLY the summary in the format above. Do not continue the conversation."""

INCREMENTAL_UPDATE_PROMPT = """\
Update the existing summary below with new information from the recent conversation.
Do not re-summarize from scratch. Only add or replace information in the relevant sections.
If a section has no new information, keep it unchanged.
Do not include a "Files Touched" section — it is handled automatically.

## Existing Summary
{existing_summary}

## New Conversation to Incorporate
{new_messages}

Reply with the FULL updated summary (Goal, Working State, Conversation Direction only).
Do not continue the conversation."""
