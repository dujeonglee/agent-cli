"""Input validation for TaskBook.

Spec (docstrings are the contract the tasks are graded against):

- A title must be 1 to 50 characters after stripping whitespace.
  Empty or whitespace-only titles are invalid.
- A priority must be an integer from 1 to 5 inclusive.
"""


class ValidationError(Exception):
    pass


def validate_title(title):
    """Return the cleaned title or raise ValidationError.

    Valid: 1..50 characters after strip. Empty is invalid.
    """
    if title is None:
        raise ValidationError("ERROR!! title is missing")
    cleaned = title.strip()
    # NOTE: boundary handling below is what pair-1 P1 is asked to fix.
    if len(cleaned) > 51:
        raise ValidationError("ERROR!! title too long")
    return cleaned


def validate_priority(priority):
    """Return the priority as int or raise ValidationError.

    Valid: 1 <= priority <= 5.
    """
    try:
        value = int(priority)
    except (TypeError, ValueError):
        raise ValidationError("bad priority (not a number)") from None
    if value < 1 or value > 6:
        raise ValidationError("bad priority (out of range)")
    return value
