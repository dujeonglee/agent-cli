"""Context overflow detection across providers."""

from __future__ import annotations

import re

# Provider-specific overflow error patterns (pi-mono reference)
OVERFLOW_PATTERNS = [
    # Anthropic
    r"prompt is too long",
    r"maximum context length",
    # OpenAI
    r"maximum context length.*exceeded",
    r"This model's maximum context length is",
    r"reduce the length of the messages",
    # Other OpenAI-compatible servers (vLLM, llama.cpp, LM Studio)
    r"context length exceeded",
    r"token limit",
    r"exceeds the model's context",
    # mlx-lm / omlx (verified against a live omlx server, 2026-05-30):
    #   "Prompt too long: 360012 tokens exceeds max context window of
    #    262144 tokens"
    # The generic ``context window`` rule below already matches this, but
    # an explicit pattern keeps detection robust if omlx rewords the
    # phrasing (and documents the real shape for the next reader).
    r"prompt too long",
    r"exceeds max context window",
    # Generic
    r"too many tokens",
    r"input.*too long",
    r"context window",
]

_COMPILED_PATTERNS = [re.compile(p, re.IGNORECASE) for p in OVERFLOW_PATTERNS]


def is_context_overflow(error_message: str) -> bool:
    """Check if an error message indicates context overflow."""
    return any(p.search(error_message) for p in _COMPILED_PATTERNS)


class ContextOverflowError(Exception):
    """Raised at the provider boundary when the server rejects a request as
    context-overflow (HTTP 400 + overflow body).

    Carries the server-reported amounts so the loop's reactive recovery needs
    no string re-parsing. ``actual``/``limit`` are best-effort — ``None`` when
    the body did not name them."""

    def __init__(
        self, actual: int | None, limit: int | None, message: str = ""
    ) -> None:
        super().__init__(message or "context overflow")
        self.actual = actual
        self.limit = limit


# The destructive ``force_fit`` recovery must fire ONLY for a genuine
# server-side context rejection. HTTP 400/413 is the ground truth; gating on
# it stops a non-overflow exception whose text merely *mentions* "context
# window" (a config error, a proxy 500 page, a bare transport error) from
# shredding history via a false-positive shrink-and-retry (see AUDIT L-1).
_OVERFLOW_STATUS = frozenset({400, 413})


def classify_overflow(exc: Exception) -> tuple[int | None, int | None] | None:
    """Classify an exception as context-overflow, returning ``(actual, limit)``
    or ``None`` when it is NOT an overflow the loop should recover from.

    - :class:`ContextOverflowError` → its carried amounts (the boundary already
      knew it was a 400 overflow and parsed the body).
    - An HTTP 400/413 error whose body matches the overflow patterns → amounts
      parsed from the message. This is the fallback for a provider path that
      does not route through :func:`raise_for_status_with_body`; the status gate
      is what makes it safe.
    - Anything else → ``None``. A matching phrase alone never triggers
      recovery, so a non-overflow error can never destroy context."""
    if isinstance(exc, ContextOverflowError):
        return exc.actual, exc.limit
    status = getattr(getattr(exc, "response", None), "status_code", None)
    if status in _OVERFLOW_STATUS and is_context_overflow(str(exc)):
        return parse_overflow_amounts(str(exc))
    return None


# Combined-form patterns: the actual count and the limit appear bound
# together in a single comparison clause, so one regex captures both.
# Each tuple: (compiled_regex, actual_group, limit_group).
_COMBINED_AMOUNT_PATTERNS = [
    # mlx-lm / omlx: "<actual> tokens exceeds max context window of <limit> tokens"
    (
        re.compile(
            r"(\d+)\s+tokens?\s+exceeds\s+max context window of\s+(\d+)", re.IGNORECASE
        ),
        1,
        2,
    ),
    # Anthropic: "prompt is too long: <actual> tokens > <limit> maximum"
    (re.compile(r"(\d+)\s+tokens?\s*>\s*(\d+)\s+maximum", re.IGNORECASE), 1, 2),
]

# OpenAI / vLLM phrase the limit and the actual count SEPARATELY, and the
# actual lead-in varies by version:
#   "...maximum context length is <limit> tokens. However, your messages
#    resulted in <actual> tokens"               (OpenAI / newer vLLM)
#   "...However, you requested <actual> tokens (...)"  (OpenAI classic / older vLLM)
#   "...your prompt contains at least <actual> input tokens."  (vLLM)
# Extract the two INDEPENDENTLY so an unrecognised (or absent) actual
# phrasing never costs us the limit: the probe needs only the limit, and
# recovery uses it as the shrink target. The limit is always exact;
# ``contains at least`` is a lower bound but recovery treats actual as
# best-effort (None → fall back to the local estimate).
_LIMIT_PATTERN = re.compile(r"maximum context length is\s+(\d+)", re.IGNORECASE)
_ACTUAL_PATTERN = re.compile(
    r"(?:resulted in|contains at least|requested)\s+(\d+)", re.IGNORECASE
)


def parse_overflow_amounts(error_message: str) -> tuple[int | None, int | None]:
    """Extract ``(actual_tokens, limit_tokens)`` from an overflow error.

    Returns the server-reported actual prompt size and the model's
    context limit when the message carries them, else ``None`` for any
    field that could not be parsed. omlx/Anthropic bind both numbers in
    one clause (combined patterns); OpenAI/vLLM phrase them separately, so
    the limit and the actual count are extracted independently — the limit
    survives even when the actual lead-in is an unrecognised wording.

    The recovery layer uses ``actual_tokens`` to override its local
    char-based estimate (which under-counts CJK badly) and
    ``limit_tokens`` to set a precise shrink target, turning a blind
    "drop oldest until it works" loop into a calculated one. Both are
    None-safe at the call site.
    """
    if not error_message:
        return (None, None)
    # Combined forms (actual & limit bound together) — omlx, Anthropic.
    for regex, actual_grp, limit_grp in _COMBINED_AMOUNT_PATTERNS:
        m = regex.search(error_message)
        if m:
            try:
                return (int(m.group(actual_grp)), int(m.group(limit_grp)))
            except (ValueError, IndexError):
                continue
    # OpenAI / vLLM — independent extraction. The limit is recovered even
    # when the actual phrasing is unknown; actual is best-effort.
    limit_m = _LIMIT_PATTERN.search(error_message)
    actual_m = _ACTUAL_PATTERN.search(error_message)
    limit = int(limit_m.group(1)) if limit_m else None
    actual = int(actual_m.group(1)) if actual_m else None
    return (actual, limit)
