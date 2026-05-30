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
We retry only exceptions raised by ``requests.post()`` itself — that is,
errors that fire *before* the server starts streaming. Once
``requests.post(stream=True)`` returns a Response, any error while
consuming chunks is out of scope: the caller already has partial output
and retransmitting the whole request would duplicate chunks the LLM
has already spoken.

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
  (default 3). Values below 1 are clamped to 1 so the call isn't
  silently dropped.
- ``AGENT_CLI_LLM_RETRY_DELAY``: seconds between attempts (default 1.0).
"""

from __future__ import annotations

import os
import time
from typing import Callable

import requests

from agent_cli.verbose import debug_log


_DEFAULT_ATTEMPTS = 3
_DEFAULT_DELAY = 1.0

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
