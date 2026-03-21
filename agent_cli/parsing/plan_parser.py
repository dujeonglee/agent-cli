"""Parse plan steps from LLM output."""
from __future__ import annotations

import json
import re

from agent_cli.planning.models import PlanStep

# Matches: >>>PLAN (with optional whitespace)
_PLAN_MARKER = re.compile(r">>>PLAN\s*\n", re.I)

# Matches numbered lines: "1. desc", "1) desc", "1: desc", "- desc"
_STEP_PATTERN = re.compile(
    r"^\s*(?:(\d+)\s*[.):\-]\s*|[-*]\s+)(.+)$", re.MULTILINE
)


def parse_plan_steps(text: str) -> list[PlanStep]:
    """Extract plan steps from LLM response text.

    Tries multiple strategies:
    1. >>>PLAN marker + numbered list
    2. JSON with "steps" or "plan" array
    3. Bare numbered list in text
    """
    # Strategy 1: >>>PLAN marker
    marker_match = _PLAN_MARKER.search(text)
    if marker_match:
        steps = _extract_numbered_steps(text[marker_match.end():])
        if steps:
            return steps

    # Strategy 2: JSON response with steps array
    steps = _extract_from_json(text)
    if steps:
        return steps

    # Strategy 3: Bare numbered list
    steps = _extract_numbered_steps(text)
    return steps


def _extract_numbered_steps(text: str) -> list[PlanStep]:
    """Extract numbered/bulleted steps from text."""
    steps: list[PlanStep] = []

    for match in _STEP_PATTERN.finditer(text):
        description = match.group(2).strip()
        if not description:
            continue
        steps.append(PlanStep(id=len(steps) + 1, description=description))

    return steps


def _extract_from_json(text: str) -> list[PlanStep]:
    """Try to parse plan from JSON response."""
    # Strip markdown fences
    cleaned = re.sub(r"^```(?:json)?\s*", "", text.strip(), flags=re.I)
    cleaned = re.sub(r"\s*```\s*$", "", cleaned)

    try:
        data = json.loads(cleaned)
    except (json.JSONDecodeError, ValueError):
        # Try extracting first { ... } block
        m = re.search(r"\{[\s\S]*\}", cleaned)
        if not m:
            return []
        try:
            data = json.loads(m.group())
        except (json.JSONDecodeError, ValueError):
            return []

    if not isinstance(data, dict):
        return []

    # Look for steps in various keys
    step_list = (
        data.get("steps")
        or data.get("plan")
        or data.get("plan_steps")
        or data.get("tasks")
    )

    if isinstance(step_list, list):
        steps = []
        for i, item in enumerate(step_list, 1):
            if isinstance(item, str):
                steps.append(PlanStep(id=i, description=item))
            elif isinstance(item, dict):
                desc = (
                    item.get("description")
                    or item.get("step")
                    or item.get("task")
                    or item.get("action")
                    or str(item)
                )
                steps.append(PlanStep(id=i, description=str(desc)))
        return steps

    # Maybe the JSON has a "plan" field that's a string with numbered list
    plan_text = data.get("plan", "")
    if isinstance(plan_text, str) and plan_text:
        return _extract_numbered_steps(plan_text)

    return []
