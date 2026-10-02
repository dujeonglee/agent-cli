"""Content-agnostic runaway detector for the streaming path (v10.10.0).

Measured (room 67qcmb, 2026-10-02): three generations hit the 32,768-token
output cap after degenerating into tabs, spaces and filler right after a
``node -e "`` inside a shell command — 27 to 40 minutes each, 106 minutes of
GPU time in one room, every result discarded. The dialect-level early stop
(``dialect.is_degenerate``, v8.41.0) never looked, because it runs only on
chunks carrying the dialect's trigger character (``#``/``<``) and matches
repeated wire-shape headers; whitespace has neither.

This detector looks at the text itself, one chunk at a time, O(1) per chunk
plus one bounded scan of the tail window:

- ``whitespace_run``: the output's trailing run of whitespace-only characters
  reaches ``WHITESPACE_RUN_CHARS``. Real output never pauses for two thousand
  blank characters — indentation is tens of characters, blank lines are two.
- ``no_words``: the last ``NO_WORDS_WINDOW`` characters hold fewer than
  ``NO_WORDS_MAX_ALNUM_RATIO`` alphanumerics (``str.isalnum`` — CJK and
  Hangul count as words). Tabs, spaces and punctuation soup trip it; a
  numeric map grid (``0,0,0,…``) is half alphanumeric and never does.

The stream loop stops reading on the first hit; the loop then treats the
turn like an output-cap cut (not executed, retried smaller) and records it.
"""

from __future__ import annotations

#: trailing whitespace-only run that counts as a runaway (characters)
WHITESPACE_RUN_CHARS = 2048
#: tail window inspected for the no-words rule (characters)
NO_WORDS_WINDOW = 4096
#: below this share of alphanumerics in a full window the tail has no words
NO_WORDS_MAX_ALNUM_RATIO = 0.02

#: what each reason means to the model (``RUNAWAY_NOTICE`` fills it in)
REASON_TEXT = {
    "whitespace_run": "a long run of whitespace",
    "no_words": "filler characters with no words",
}


class RunawayDetector:
    """Feed every content chunk; the first non-None return is the reason."""

    def __init__(
        self,
        *,
        whitespace_run: int = WHITESPACE_RUN_CHARS,
        window: int = NO_WORDS_WINDOW,
        max_alnum_ratio: float = NO_WORDS_MAX_ALNUM_RATIO,
    ) -> None:
        self.whitespace_run = whitespace_run
        self.window = window
        self.max_alnum_ratio = max_alnum_ratio
        self._ws_run = 0
        self._tail = ""

    def feed(self, text: str) -> str | None:
        if not text:
            return None
        stripped = text.rstrip()
        if stripped:
            # a non-whitespace character ends the run; what follows it starts a new one
            self._ws_run = len(text) - len(stripped)
        else:
            self._ws_run += len(text)
        if self._ws_run >= self.whitespace_run:
            return "whitespace_run"
        self._tail = (self._tail + text)[-self.window :]
        if len(self._tail) >= self.window:
            alnum = sum(1 for c in self._tail if c.isalnum())
            if alnum < self.window * self.max_alnum_ratio:
                return "no_words"
        return None
