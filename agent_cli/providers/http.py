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

Retryable HTTP statuses
-----------------------
Transient gateway errors — ``502``/``503``/``504`` — ARE retried (bounded, on a
budget SEPARATE from the network-error one). A reverse proxy (nginx/caddy) in
front of an on-prem LLM returns these while the upstream is restarting or
overloaded, so a short re-send usually recovers — exactly like
``ConnectionError`` does when connecting directly. The error response is closed
and the request re-sent from scratch (no partial to resume).

Other HTTP error responses (4xx, a bare 500) are NOT retried — they are raised
via ``raise_for_status_with_body`` by the caller after this function returns and
represent a server decision that retrying won't change. When the 5xx retries are
exhausted the last error response is returned unchanged, so the caller still
surfaces it (with body) exactly as before.

Budgets (fixed)
---------------
- Network errors (Timeout / ConnectionError): ``_DEFAULT_ATTEMPTS`` (10)
  total attempts including the first.
- Transient gateway 5xx (502/503/504): ``_DEFAULT_STATUS_ATTEMPTS`` (3),
  a budget separate from the network one.
- Backoff: fixed ``_DEFAULT_DELAY`` (1s) between attempts, not exponential —
  single-user on-prem, so thundering-herd / rate-limit concerns don't apply;
  the 1s just gives a restarting server headroom for ConnectionError.
"""

from __future__ import annotations

import json
import queue
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field

import requests

from agent_cli.context.overflow import (
    ContextOverflowError,
    is_context_overflow,
    parse_overflow_amounts,
)
from agent_cli.verbose import debug_log

_DEFAULT_ATTEMPTS = 10
_DEFAULT_STATUS_ATTEMPTS = 3
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
        # Provider boundary: this is the ONE place that knows both the HTTP
        # status and the body. Promote a 400 overflow to a typed error so the
        # loop never has to string-match arbitrary exceptions — a false match
        # there destroyed history via force_fit (AUDIT-2026-08-09.md L-1).
        if r.status_code == 400 and is_context_overflow(body):
            actual, limit = parse_overflow_amounts(body)
            raise ContextOverflowError(actual, limit, body[:max_body]) from e
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

# Transient gateway errors from a reverse proxy (nginx/caddy) in front of an
# on-prem LLM server: the upstream is momentarily down/restarting/overloaded, so
# a bounded re-send usually recovers — unlike 4xx (bad request) or a bare 500.
_RETRYABLE_STATUS: frozenset[int] = frozenset({502, 503, 504})


def post_with_retry(
    post_fn: Callable[..., requests.Response],
    url: str,
    *,
    retry_statuses: frozenset[int] = _RETRYABLE_STATUS,
    **kwargs,
) -> requests.Response:
    """Invoke ``post_fn(url, **kwargs)`` with bounded retry on network errors
    and on transient gateway ``5xx`` responses.

    Two independent budgets: ``_DEFAULT_ATTEMPTS`` (10) for
    ``Timeout``/``ConnectionError`` raised *before* any status arrives, and
    ``_DEFAULT_STATUS_ATTEMPTS`` (3) for a response whose status is in
    ``retry_statuses`` (default 502/503/504). Both share ``_DEFAULT_DELAY``
    (1s) for the pause between attempts.

    When the 5xx budget is exhausted the last error response is returned
    unchanged — the caller's ``raise_for_status_with_body`` then surfaces it
    (with body). Any other status (2xx, 4xx, bare 500) returns immediately.

    ``post_fn`` is passed in explicitly (not imported from ``requests``
    here) so that existing test patches of
    ``agent_cli.providers.{name}.requests.post`` continue to intercept
    the call — the provider module still owns the ``requests`` name and
    passes its own ``requests.post`` attribute to this helper.
    """
    # Lazy import: keeps this module loadable during test collection
    # even if the render subsystem isn't fully wired.
    from agent_cli.render import render_status

    attempts = _DEFAULT_ATTEMPTS
    status_attempts = _DEFAULT_STATUS_ATTEMPTS
    delay = _DEFAULT_DELAY
    last_exc: BaseException | None = None
    net_failures = 0
    status_failures = 0

    while True:
        try:
            r = post_fn(url, **kwargs)
        except _RETRYABLE as e:
            last_exc = e
            net_failures += 1
            if net_failures >= attempts:
                break
            render_status(
                "running",
                f"LLM request failed ({type(e).__name__}) — "
                f"retrying ({net_failures + 1}/{attempts})",
            )
            debug_log(
                f"[retry] {type(e).__name__} on {url}: "
                f"attempt {net_failures}/{attempts} failed; sleeping {delay}s"
            )
            time.sleep(delay)
            continue

        # A response arrived. Transient gateway 5xx (502/503/504) is re-sent on
        # its own budget; every other status returns to the caller.
        if retry_statuses and r.status_code in retry_statuses:
            status_failures += 1
            if status_failures >= status_attempts:
                debug_log(
                    f"[retry] HTTP {r.status_code} on {url}: exhausted "
                    f"{status_attempts} attempts; surfacing to caller"
                )
                return r
            r.close()  # discard the error response before re-sending
            render_status(
                "running",
                f"LLM request failed (HTTP {r.status_code}) — "
                f"retrying ({status_failures + 1}/{status_attempts})",
            )
            debug_log(
                f"[retry] HTTP {r.status_code} on {url}: "
                f"attempt {status_failures}/{status_attempts} failed; "
                f"sleeping {delay}s"
            )
            time.sleep(delay)
            continue

        return r

    # Network-error retries exhausted — surface the last exception to the caller.
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


# ── 공용 SSE 스트림 러너 (C6, v4.48.0) ─────────────────────────────
# openai/anthropic 두 provider 가 ~70% 동일한 스트림 골격(라인 순회·idle
# notice/타임아웃·"data:" 파싱·누산·타이밍·degeneration 조기종료·interrupt
# 라벨링)을 복제하고 있었고, idle 처리·JSONDecodeError 관용은 openai/
# anthropic 한쪽에만 있는 비대칭까지 있었다. 골격을 여기(이미
# post_with_retry 를 공유하는 인프라 계층)로 수렴 — provider 는 자기
# 이벤트 shape 해석(map_payload)만 소유한다. wire-format self-contained
# 규율과 같은 정신: 독립 진화가 필요한 부분(이벤트 shape)만 provider 에.


@dataclass
class StreamEvent:
    """``map_payload`` 가 payload dict 하나를 정규화해 돌려주는 이벤트.

    text/thinking 은 델타(누산은 골격 소유), usage_fields 는 provider 별
    usage 키 dict(골격이 누적 merge — 나중 값이 이김), stop_reason 은
    발견 시 설정, done=True 는 명시 종료.
    """

    text: str = ""
    thinking: str = ""
    stop_reason: str | None = None
    usage_fields: dict | None = None
    done: bool = False


@dataclass
class StreamAccum:
    """골격의 누산 결과 — provider 가 usage/LLMResponse 로 조립한다."""

    content: str = ""
    thinking: str = ""
    stop_reason: str | None = None
    usage_fields: dict = field(default_factory=dict)
    ttft_ns: int = 0
    decode_ns: int = 0


def run_sse_stream(
    r,
    on_chunk,
    *,
    map_payload,
    degeneration_check=None,
    degeneration_trigger="#",
    interrupt_check=None,
) -> StreamAccum:
    """SSE 스트림 공용 골격 — 양 provider 동형 보장 지점.

    소유: ``interruptible_lines`` 순회(**idle notice + StreamIdleTimeout
    을 양 provider 에 동일 적용** — 이전엔 openai 만), ``data: `` 프리픽스
    파싱, ``[DONE]`` 관용(SSE 관례), **JSONDecodeError 관용(스킵)** —
    이전엔 anthropic 만, content/thinking 누산, t_first/ttft 타이밍,
    degeneration ``'#'`` 게이트 조기종료(``r.close()``), 루프 후
    interrupt 재확인 라벨링.

    ``map_payload(data: dict) -> StreamEvent | None`` 이 provider 몫 —
    None 은 "이 payload 무시".
    """
    import time as _time

    from agent_cli.constants import STREAM_IDLE_MAX_TICKS, STREAM_IDLE_THRESHOLD

    acc = StreamAccum()
    t0 = _time.perf_counter_ns()
    t_first = 0

    def _on_idle(tick: int, seconds: float) -> None:
        from agent_cli.render import render_status

        render_status(
            "running",
            f"응답 대기 중 — 토큰 없음 {int(seconds)}s "
            f"({tick}/{STREAM_IDLE_MAX_TICKS}, "
            f"{STREAM_IDLE_MAX_TICKS * STREAM_IDLE_THRESHOLD // 60}분 후 재연결)",
        )

    for line in interruptible_lines(
        r,
        interrupt_check,
        idle_threshold=STREAM_IDLE_THRESHOLD,
        max_idle_ticks=STREAM_IDLE_MAX_TICKS,
        on_idle=_on_idle,
    ):
        if not line:
            continue
        line_str = line.decode("utf-8") if isinstance(line, bytes) else line
        if not line_str.startswith("data: "):
            continue
        payload = line_str[6:]
        if payload == "[DONE]":  # SSE 관례 종료 (openai 계열)
            break
        try:
            data = json.loads(payload)
        except json.JSONDecodeError:
            continue  # 깨진 라인 관용 — 스트림 전체를 죽이지 않는다

        ev = map_payload(data)
        if ev is None:
            continue
        if ev.usage_fields:
            acc.usage_fields.update(ev.usage_fields)
        if ev.thinking:
            acc.thinking += ev.thinking
        if ev.text:
            if not t_first:
                t_first = _time.perf_counter_ns()
            acc.content += ev.text
            on_chunk(ev.text)
            # Early-stop format runaway. 게이트 문자는 wire shape 소유
            # (P0-4 — json_fc "#" 헤더 / xml_fc "<" 태그): 러너웨이 시그니처가
            # 가능해진 시점에만 predicate(regex) 실행 — O(트리거) not O(chunks).
            if (
                degeneration_check is not None
                and degeneration_trigger in ev.text
                and degeneration_check(acc.content)
            ):
                acc.stop_reason = "degenerate_runaway"
                r.close()
                break
        if ev.stop_reason:
            acc.stop_reason = ev.stop_reason
        if ev.done:
            break

    # Reader stopped early because the user interrupted; flag still set →
    # (폐기될) partial 에 라벨.
    if interrupt_check is not None and interrupt_check():
        acc.stop_reason = "interrupted"

    t_end = _time.perf_counter_ns()
    acc.ttft_ns = (t_first - t0) if t_first else 0
    acc.decode_ns = (t_end - t_first) if t_first else 0
    return acc
