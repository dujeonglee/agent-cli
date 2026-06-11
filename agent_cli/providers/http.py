"""HTTP helper with bounded retry for LLM API calls.

Why this exists
---------------
On-prem LLM servers (vLLM, LM Studio, omlx) occasionally fail with
transient ``requests.ConnectionError`` (server just restarting) or
``requests.Timeout`` (model load is slow on the first call). Retrying
the request one or two times recovers from both without bubbling up to
the user.

Scope: pre-stream only
----------------------
``post_with_retry`` retries only exceptions raised by ``requests.post()``
itself — errors that fire *before* the server starts streaming. Once
``requests.post(stream=True)`` returns a Response, an error while consuming
chunks is out of this helper's scope.

The streaming path adds ONE deliberate re-send for a different failure: a
stream that goes SILENT (``StreamIdleTimeout`` from :func:`interruptible_lines`
after ~10min of no tokens). There the partial is DISCARDED and the whole
request re-sent (the generation restarts — there is no server-side resume), so
no chunks are duplicated. That reconnect loop lives in the provider, not here.

Retryable exceptions
--------------------
- ``requests.Timeout`` (covers ``ConnectTimeout`` and ``ReadTimeout``)
- ``requests.ConnectionError``

HTTP error responses (4xx/5xx) are NOT retried — those are raised via
``response.raise_for_status()`` by the caller after this function
returns, and they represent a server decision that retrying won't
change. This helper only wraps the underlying network call.

Backoff
-------
Fixed 1s between attempts, not exponential. Rationale: the target
deployment is single-user on-prem, so thundering-herd and rate-limit
concerns don't apply. The 1s exists only to give a restarting server
a moment of headroom for ConnectionError; Timeout is already the result
of a long wait so the pause has little effect but no harm.

Config (env)
------------
- ``AGENT_CLI_LLM_RETRY_ATTEMPTS``: total attempts including the first
  (default 10). Values below 1 are clamped to 1 so the call isn't
  silently dropped.
- ``AGENT_CLI_LLM_RETRY_DELAY``: seconds between attempts (default 1.0).
"""

from __future__ import annotations

import os
import queue
import threading
import time
from typing import Callable, Iterator

import requests

from agent_cli.verbose import debug_log


_DEFAULT_ATTEMPTS = 10
_DEFAULT_DELAY = 1.0

# Poll cadence for interrupt during a no-data stream gap (TTFT, between-token
# stalls). Sub-second so a user interrupt feels immediate; not so tight it
# busy-loops.
_INTERRUPT_POLL_SECONDS = 0.2

# Sentinel marking the reader thread has finished (normally or via error).
_STREAM_DONE = object()


def raise_for_status_with_body(r: requests.Response, max_body: int = 1000) -> None:
    """Like ``r.raise_for_status()`` but the raised ``HTTPError`` message INCLUDES
    the response body.

    ``requests.raise_for_status()`` produces a body-less message
    (``"400 Client Error: Bad Request for url: ..."``), which drops the very
    text the runtime needs: on a context-overflow 400, omlx/mlx-lm name the
    limit in the BODY (``"...tokens exceeds max context window of N tokens"``).
    The loop's reactive recovery checks ``is_context_overflow(str(error))`` to
    shrink-and-retry — with the body dropped it never recognises the overflow
    and a recoverable 400 hard-fails instead. Including the body restores that
    path.

    Wraps ``raise_for_status()`` (rather than inspecting ``status_code``) so the
    success path is untouched — for a streaming 200 we never read ``r.text``
    (which would consume the stream); ``r.text`` is read ONLY in the error
    branch, where the server has already sent a complete error body, not a
    stream."""
    try:
        r.raise_for_status()
    except requests.HTTPError as e:
        body = (r.text or "").strip()
        if not body:
            raise
        raise requests.HTTPError(f"{e}: {body[:max_body]}", response=r) from e


def make_stream_patient(r: requests.Response, read_timeout: float) -> None:
    """After a streaming ``post()`` returns (headers received), relax the socket
    read timeout to ``read_timeout`` so BODY reads block patiently.

    The streaming post uses a short read timeout (e.g. 30s) only to bound the
    header wait. A single requests timeout governs both the header read AND
    every body read (verified: a 5s post timeout makes ``iter_lines`` raise at
    5s on a body stall), so without this the 30s would also kill a slow-but-alive
    generation. We reach into the urllib3 socket and re-set its timeout once
    headers are in hand; the poll-loop idle detector then owns body stalls.

    Best-effort: the socket is a urllib3 private attribute, so on any drift we
    leave the timeout as-is (the short post timeout becomes the body backstop —
    stalls then fail fast instead of patiently, never a 20min hang)."""
    try:
        r.raw._connection.sock.settimeout(read_timeout)  # type: ignore[attr-defined]
    except Exception as e:  # pragma: no cover - urllib3 internals drift
        debug_log(
            f"[stream] could not relax socket timeout ({type(e).__name__}); "
            "body reads keep the post read timeout"
        )


class StreamIdleTimeout(Exception):
    """A streaming response went silent for too long (no token for
    ``idle_threshold * max_idle_ticks`` seconds). Raised by
    :func:`interruptible_lines` AFTER closing the response, so the caller can
    reconnect + re-send the request (the generation is restarted — there is no
    server-side resume). Distinct from ``requests.Timeout`` so the caller's
    reconnect loop can tell a stall apart from a connect failure."""

    def __init__(self, idle_seconds: float):
        self.idle_seconds = idle_seconds
        super().__init__(f"stream idle for {idle_seconds:.0f}s")


# Timeout subsumes ConnectTimeout and ReadTimeout. ConnectionError covers
# TCP-level failures (refused, reset) before any HTTP status arrives.
_RETRYABLE: tuple[type[BaseException], ...] = (
    requests.Timeout,
    requests.ConnectionError,
)


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _attempts() -> int:
    # Clamp minimum to 1: setting 0 would silently skip the request.
    return max(1, _env_int("AGENT_CLI_LLM_RETRY_ATTEMPTS", _DEFAULT_ATTEMPTS))


def _delay() -> float:
    return max(0.0, _env_float("AGENT_CLI_LLM_RETRY_DELAY", _DEFAULT_DELAY))


def post_with_retry(
    post_fn: Callable[..., requests.Response],
    url: str,
    **kwargs,
) -> requests.Response:
    """Invoke ``post_fn(url, **kwargs)`` with bounded retry on network errors.

    ``post_fn`` is passed in explicitly (not imported from ``requests``
    here) so that existing test patches of
    ``agent_cli.providers.{name}.requests.post`` continue to intercept
    the call — the provider module still owns the ``requests`` name and
    passes its own ``requests.post`` attribute to this helper.
    """
    # Lazy import: keeps this module loadable during test collection
    # even if the render subsystem isn't fully wired.
    from agent_cli.render import render_status

    attempts = _attempts()
    delay = _delay()
    last_exc: BaseException | None = None

    for i in range(attempts):
        try:
            return post_fn(url, **kwargs)
        except _RETRYABLE as e:
            last_exc = e
            remaining = attempts - i - 1
            if remaining <= 0:
                break
            next_attempt = i + 2  # human-friendly: "retrying (2/3)"
            render_status(
                "running",
                f"LLM request failed ({type(e).__name__}) — "
                f"retrying ({next_attempt}/{attempts})",
            )
            debug_log(
                f"[retry] {type(e).__name__} on {url}: "
                f"attempt {i + 1}/{attempts} failed; sleeping {delay}s"
            )
            time.sleep(delay)

    # Exhausted — surface the last exception to the caller.
    render_status(
        "error",
        f"LLM request failed after {attempts} attempts: {type(last_exc).__name__}",
    )
    debug_log(f"[retry] exhausted {attempts} attempts on {url}: {last_exc}")
    assert last_exc is not None
    raise last_exc


def interruptible_lines(
    r: requests.Response,
    interrupt_check: Callable[[], bool] | None = None,
    poll_interval: float = _INTERRUPT_POLL_SECONDS,
    idle_threshold: float | None = None,
    max_idle_ticks: int | None = None,
    on_idle: Callable[[int, float], None] | None = None,
) -> Iterator[bytes]:
    """Yield SSE lines from a streaming response, but stay interruptible even
    when no data is arriving, and surface / bound a stalled stream.

    ``r.iter_lines()`` blocks waiting for the next byte, so polling a flag
    "per chunk" never fires during a no-data gap — most importantly the TTFT
    window before the first generated token (a large prompt can stall here for
    seconds). Setting a socket read timeout doesn't help: ``requests`` treats a
    read timeout as terminal and the stream can't be resumed.

    So the blocking read runs in a daemon reader thread that pushes lines to a
    queue; this generator polls the queue with ``poll_interval`` and, on each
    empty poll, (1) checks ``interrupt_check`` — on interrupt it closes ``r``
    (unblocking the reader's ``recv``) and stops yielding; the caller detects it
    by re-checking ``interrupt_check()`` after the loop — and (2) measures idle
    time. Because the reader stays blocked (no socket read timeout) the
    connection survives across idle gaps, so we can keep waiting.

    Idle handling (when ``idle_threshold`` is set): every ``idle_threshold``
    seconds of NO data fires ``on_idle(tick, seconds)`` (a UI "still waiting"
    notice); the counter resets the moment a line arrives. After
    ``max_idle_ticks`` consecutive idle intervals the response is closed and
    :class:`StreamIdleTimeout` is raised so the caller can reconnect + re-send.
    Interrupt takes precedence over idle (checked first each poll).

    Without ``interrupt_check`` or ``idle_threshold`` this is a plain
    pass-through over ``iter_lines()`` (no thread). Genuine stream errors raised
    by ``iter_lines`` are propagated; the error caused by our own ``r.close()``
    (interrupt or idle-timeout) is not (we return / raise first).
    """
    if interrupt_check is None and idle_threshold is None:
        yield from r.iter_lines()
        return

    q: queue.Queue = queue.Queue()
    err: list[BaseException] = []

    def _reader() -> None:
        try:
            for line in r.iter_lines():
                q.put(line)
        except BaseException as e:  # incl. the error from our own r.close()
            err.append(e)
        finally:
            q.put(_STREAM_DONE)

    threading.Thread(target=_reader, daemon=True).start()

    last_data = time.monotonic()
    idle_ticks = 0
    while True:
        if interrupt_check is not None and interrupt_check():
            r.close()  # abort the reader's blocked recv
            return
        try:
            item = q.get(timeout=poll_interval)
        except queue.Empty:
            if idle_threshold is not None:
                idle = time.monotonic() - last_data
                if idle >= idle_threshold * (idle_ticks + 1):
                    idle_ticks += 1
                    if on_idle is not None:
                        on_idle(idle_ticks, idle)
                    if max_idle_ticks is not None and idle_ticks >= max_idle_ticks:
                        r.close()
                        raise StreamIdleTimeout(idle)
            continue
        # A line arrived → the stream is alive; reset the idle window.
        last_data = time.monotonic()
        idle_ticks = 0
        if item is _STREAM_DONE:
            if err:
                raise err[0]
            return
        yield item
