"""Context compression prompts for structured summarization.

Designed to complement the scratchpad (which tracks Progress / Decisions / Open Questions).
The summary focuses on context that the scratchpad does NOT capture:
goal, working state, conversation direction, and files touched.
"""

SUMMARIZATION_PROMPT = """\
Summarize the following conversation into these sections.
Be concise. Preserve file paths, error messages, and command outputs exactly.

NOTE: Progress and Decisions are tracked separately in a scratchpad.
Do NOT duplicate them here. Focus on context the scratchpad cannot capture.

## Goal
What the user is trying to accomplish (1-2 sentences).

## Working State
Current errors, blockers, intermediate results, or variable values
that the next assistant turn needs to continue work.
Preserve exact error messages but summarize long tracebacks.

## Conversation Direction
What was agreed on next, the user's intent, and any stated preferences
for how to proceed.

## Files Touched
- Read: [list of file paths read]
- Modified: [list of file paths written/edited]

Reply with ONLY the summary in the format above. Do not continue the conversation."""

INCREMENTAL_UPDATE_PROMPT = """\
Update the existing summary below with new information from the recent conversation.
Do NOT re-summarize from scratch. Only ADD or REPLACE information in the relevant sections.
If a section has no new information, keep it unchanged.

## Existing Summary
{existing_summary}

## New Conversation to Incorporate
{new_messages}

Reply with the FULL updated summary in the same format. Do not continue the conversation."""
