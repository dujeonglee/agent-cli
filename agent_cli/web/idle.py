"""Self-reap decision logic for ``agent-cli web --idle-timeout``.

A web instance launched by an external orchestrator (the "board" service)
should run only while someone is using it and exit on its own once idle, so the
orchestrator never has to track or kill processes — it just spawns
``agent-cli web --resume <id> --idle-timeout N`` and the instance reaps itself.

:class:`IdleMonitor` is the pure decision: given an ``is_active`` predicate and
a timeout, it fires ``on_idle`` once after the instance has been continuously
inactive for ``timeout_s``. Clock-injected so it is fully unit-testable without
real waiting. The wiring (a poll thread + ``uvicorn should_exit``) lives in
``main.web``; this module owns only the timing rule.
"""

from __future__ import annotations

import time
from collections.abc import Callable


class IdleMonitor:
    """Fire ``on_idle`` once after ``timeout_s`` of continuous inactivity.

    ``is_active()`` returns True whenever the instance is in use (a live viewer,
    a busy worker, or queued work). Any active tick resets the idle clock.
    ``timeout_s <= 0`` (or ``None``) disables the monitor entirely (the default
    ``agent-cli web`` behaviour — run until killed). ``now`` is injectable for
    tests; production passes ``time.monotonic``.
    """

    def __init__(
        self,
        *,
        is_active: Callable[[], bool],
        timeout_s: float | None,
        on_idle: Callable[[], None],
        now: Callable[[], float] = time.monotonic,
    ) -> None:
        self._is_active = is_active
        self._timeout = timeout_s
        self._on_idle = on_idle
        self._now = now
        self._last_active = now()
        self._fired = False

    @property
    def enabled(self) -> bool:
        return bool(self._timeout and self._timeout > 0)

    def tick(self) -> None:
        """Poll once. Resets the clock if active; fires ``on_idle`` (once) if
        inactive for ``>= timeout_s``."""
        if not self.enabled or self._fired:
            return
        if self._is_active():
            self._last_active = self._now()
            return
        if self._now() - self._last_active >= self._timeout:
            self._fired = True
            self._on_idle()
