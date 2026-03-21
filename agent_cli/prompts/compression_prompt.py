"""Context compression prompts for structured summarization."""

SUMMARIZATION_PROMPT = """\
Summarize the following conversation into these sections.
Be concise. Preserve file paths, command outputs, and error messages exactly.

## Goal
What the user is trying to accomplish.

## Progress
What has been completed so far (specific files, commands, results).

## Key Decisions
Important choices made and their rationale.

## Current State
Where things stand right now (errors, blockers, next action needed).

## Files Touched
- Read: [list of files read]
- Modified: [list of files written/edited]

Reply with ONLY the summary in the format above. Do not continue the conversation."""

INCREMENTAL_UPDATE_PROMPT = """\
Update the existing summary below with new information from the recent conversation.
Do NOT re-summarize from scratch. Only ADD new information to the relevant sections.
If a section has no new information, keep it unchanged.

## Existing Summary
{existing_summary}

## New Conversation to Incorporate
{new_messages}

Reply with the FULL updated summary in the same format. Do not continue the conversation."""
