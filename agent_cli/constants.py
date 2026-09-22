"""Shared constants for agent-cli."""

# ── Timeout values (seconds) ──────────────────
SHELL_COMMAND_TIMEOUT = 30
# LLM request timeout as a requests ``(connect, read)`` tuple.
#   - connect (30s): TCP/TLS connection establishment. Short so a DOWN /
#     unreachable server fails fast — ConnectTimeout is retried up to
#     AGENT_CLI_LLM_RETRY_ATTEMPTS times (default 10).
#   - read: time between bytes once connected.
# Two profiles:
#   - LLM_API_TIMEOUT (non-streaming, read=1200s): the post() reads the whole
#     body, so the read timeout is the full-generation idle bound — generous so
#     a slow cold 27B isn't killed mid-generation.
#   - LLM_STREAM_TIMEOUT (streaming, read=30s): the post() only reads the
#     response HEADERS, so 30s bounds the header wait (a broken server that
#     never responds fails fast + retries, instead of the old ~20min hang) AND
#     interrupt during the header wait. After post() returns, the provider
#     RESETS the socket read timeout to patient (LLM_READ_TIMEOUT) so body reads
#     don't inherit the 30s — the poll-loop idle detector owns body stalls. (A
#     single socket timeout can't be both short-for-header and patient-for-body;
#     the reset is how we get both. Empirically verified; best-effort with a
#     fallback to the configured read timeout if the urllib3 socket is
#     unreachable.)
LLM_CONNECT_TIMEOUT = 30
LLM_READ_TIMEOUT = 1200
LLM_STREAM_READ_TIMEOUT = 30
LLM_API_TIMEOUT = (LLM_CONNECT_TIMEOUT, LLM_READ_TIMEOUT)
LLM_STREAM_TIMEOUT = (LLM_CONNECT_TIMEOUT, LLM_STREAM_READ_TIMEOUT)
# Streaming idle/stall handling (poll-loop on the patient body socket):
#   - every STREAM_IDLE_THRESHOLD seconds with no token, render a notice
#     (visible "still waiting" feedback) — resets when a token arrives.
#   - after STREAM_IDLE_MAX_TICKS consecutive idle intervals (20*30s = 10min of
#     total silence) the connection is closed and the request re-sent
#     (StreamIdleTimeout), for STREAM_MAX_ATTEMPTS attempts before hard-fail.
#     A re-send RESTARTS generation (no server-side resume); a 10-min-silent
#     stream is dead anyway. Interrupt is independent (polled every 0.2s).
STREAM_IDLE_THRESHOLD = 30
STREAM_IDLE_MAX_TICKS = 20
# v8.60.0: **총 시도 횟수** (첫 전송 포함) — 종전 STREAM_MAX_RECONNECTS=3
# ("재전송 횟수")의 재정의로, 동작은 바이트 동일(4회)하고 의미만 바꾼다.
# 이유: 같은 파일의 post_with_retry 는 _DEFAULT_ATTEMPTS=10 을 "총 시도"로
# 쓰는데 여기만 "재전송"이라 `range(N+1)` 변환과 `(1/3)` 표시가 필요했고,
# 사용자에게 "3 설정인데 왜 40분?"으로 읽혔다. 이제 값=표시=총 시간 나눗수.
# 사용자 값은 ctx.stream_max_attempts(web ⏳ 노브 2번째 입력 /
# env AGENT_CLI_STREAM_MAX_ATTEMPTS / CLI --stall-attempts).
STREAM_MAX_ATTEMPTS = 4
STREAM_MAX_ATTEMPTS_MAX = 10
# P3 (v8.55.0): 스트림 무진전(no-token) 한도의 기본/상한. 유휴는 "마지막
# **진전**(실제 토큰/종결 이벤트) 이후"로 잰다 — keep-alive 프레임은 진전이
# 아니다(omlx hang 실측 2회: keep-alive 가 종전 줄-기준 감지를 무력화).
# 사용자 값은 ctx.stream_idle_timeout_s(web "Stall" / env
# AGENT_CLI_STREAM_IDLE_TIMEOUT_S) → CallSettings 로 매 콜 전달, 0=감지 끔.
DEFAULT_STREAM_IDLE_TIMEOUT_S = STREAM_IDLE_THRESHOLD * STREAM_IDLE_MAX_TICKS
STREAM_IDLE_TIMEOUT_MAX_S = 3600
AGENT_DEFAULT_TIMEOUT = 300
# First-run capability detection probes (thinking support, JSON-format
# tolerance, context-window overflow). All run once per model and may
# incur a cold model load, so they share a generous allowance distinct
# from SHELL_COMMAND_TIMEOUT (which is for user shell commands, not
# probes).
DETECTION_PROBE_TIMEOUT = 60

# ── Observation message templates ──────────────
OBS_SUCCESS = "STATUS: success\nRESULT:\n{result}"

# ── System-injected user messages ───────────────
# These get persisted as role=user in history.jsonl but are NOT actual
# user queries — they're loop-emitted notifications/hints.
#
# Per-format retry hints (parse-fail, no-action) live on the wire-format
# plugin: ``ReActFormat.static_retry_hint_no_*()``. The unified prefix
# list for filtering system messages out of resume previews lives at
# ``agent_cli.wire_formats.all_system_user_prefixes()``.
INTERRUPT_NOTICE = "⚡ User interrupted. Waiting for new instructions."


def outstanding_requests_block(requests: list) -> str:
    """이 런이 답해야 할 사용자 요청 — **매 턴 꼬리**에 실린다.

    종전엔 드레인 시점에 딱 한 번 대화에 주입했다. 두 가지가 나빴다:

    - **읽히지 않는다.** 턴이 길어지면 그 줄은 뒤로 밀리고, 모델이
      `complete` 을 쓰는 순간엔 이미 멀다.
    - **영구히 남는다.** `ctx.add` 라 history 에 박히고 resume 프리뷰까지
      따라다닌다.

    꼬리는 재현성 주의가 가장 센 자리이고, KV 프리픽스 비용도 사실상 없으며
    (`prompts/session_state.py` — 어차피 매 턴 재계산되는 구간), history 에
    안 남는다.

    **요청이 하나여도 싣는다.** 그리고 (v9.22.0) 하나여도 `answers` 를 요구한다
    — "결과가 곧 그 답" 은 사용자 요청과 에이전트 질문이 한 런에 섞이면
    틀리는 추측이라 건수 특례를 없앴다.
    """
    lines = "\n".join(
        f"  [{r.get('id')}]"
        + (f" ({r['author']})" if r.get("author") else "")
        + f' "{(r.get("text") or "").strip()[:120]}"'
        for r in requests
    )
    n = len(requests)
    head = (
        f"{n} user requests are open in this run:"
        if n > 1
        else "This run is answering:"
    )
    ids = ", ".join(f'"{r.get("id")}"' for r in requests)
    return (
        f"## Open Requests\n{head}\n{lines}\n"
        f"Set `answers` on `complete` to the ids you actually answered — here "
        f"that would be `answers: [{ids}]` if you addressed them all. Ids you "
        "leave out are reported to the user as unanswered."
    )


# Shown as an observation when a response hits the model's output-token
# limit (stop_reason == "length"). The truncated action is NOT executed
# — the loop records this so the model retries with a smaller unit.
OUTPUT_TRUNCATED_NOTICE = (
    "⚠️ Your previous response was cut off at the output-token limit, so "
    "its action was incomplete and was NOT executed. Retry with a smaller "
    "unit — e.g. build a large file incrementally with edit_file instead "
    "of one big write_file."
)


# ── duration 파서 — 표면 둘이 공유 (v9.11.0) ────────────────
#
# 같은 문법을 두 번 구현하면 둘이 갈라지고, 갈라진 걸 아무도 모른다(감사에서
# 이스케이퍼 3종·`el()` 2종을 그 이유로 정리했다). ``--stall`` 과 monitor 의
# ``deadline``/``every`` 가 같은 문법을 쓰므로 **순수 파서를 여기 두고 양쪽이
# 자기 표면의 예외로 변환**한다:
#
#   main._parse_stall  → typer.BadParameter  (CLI 표면 유지)
#   monitor_tool       → ToolResult(False, …) (도구는 예외를 못 던진다)
def parse_duration(raw: str) -> int:
    """``"600"``(초) · ``"10m"``(분) · ``"2h"``(시) → 초.

    ``"45s"`` 처럼 명시적 초 접미사도 받는다. 형식 오류·음수는 ``ValueError``
    — 호출자가 자기 표면의 예외로 바꾼다. 빈 값 처리는 **호출자 몫**이다
    (``--stall`` 은 빈 값 = "미지정"이라 0 과 구분해야 하는데, monitor 는
    그런 구분이 없다 — 파서가 둘 중 하나를 고르면 다른 쪽이 틀린다).
    """
    t = str(raw).strip().lower()
    mult = 1
    if t.endswith("h"):
        mult, t = 3600, t[:-1]
    elif t.endswith("m"):
        mult, t = 60, t[:-1]
    elif t.endswith("s"):
        t = t[:-1]
    try:
        value = int(t)
    except ValueError:
        raise ValueError(f"{raw!r} — 초(600) · 분(10m) · 시(2h) 형식이어야 합니다")
    if value < 0:
        raise ValueError("음수는 쓸 수 없습니다")
    return value * mult


# monitor clamp 상수 (docs/monitor/DESIGN.md §10.1·10.2)
# `context/manager.py` 의 STREAM_IDLE_TIMEOUT_{MIN,MAX}_S 와 같은 모양 —
# 거부가 아니라 clamp 다. 값이 크다고 실패시키면 모델이 "얼마가 맞는지"를
# 탐색하느라 턴을 태운다.
MONITOR_DEADLINE_DEFAULT_S = 7200  # 2h — 빠뜨려도 등록이 성공한다
MONITOR_DEADLINE_MIN_S = 60  # 1m
MONITOR_DEADLINE_MAX_S = 86400  # 24h — 그 이상은 배치 작업이고 board 소관
MONITOR_INTERVAL_MIN_S = 60  # 주기 `command` 의 `every` 하한
