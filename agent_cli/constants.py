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

# 샘플링 노브의 값 영역 (v10.69.0, docs/knobs/DESIGN.md §7) — ctx.set_sampling 이
# 이 범위로 clamp 하고 🎲 칩이 min/max 로 그린다. OpenAI·Anthropic 이 받는 범위의
# 교집합이 아니라 합집합(Anthropic temperature 는 0~1 이지만 서버가 거절할 뿐,
# 하니스가 미리 좁히지 않는다 — 어느 서버를 대하는지는 세션이 안다).
SAMPLING_RANGES: dict[str, tuple[float, float]] = {
    "temperature": (0.0, 2.0),
    "top_p": (0.0, 1.0),
}
# P3 (v8.55.0): 스트림 무진전(no-token) 한도의 기본/상한. 유휴는 "마지막
# **진전**(실제 토큰/종결 이벤트) 이후"로 잰다 — keep-alive 프레임은 진전이
# 아니다(omlx hang 실측 2회: keep-alive 가 종전 줄-기준 감지를 무력화).
# 사용자 값은 ctx.stream_idle_timeout_s(web "Stall" / env
# AGENT_CLI_STREAM_IDLE_TIMEOUT_S) → CallSettings 로 매 콜 전달, 0=감지 끔.
DEFAULT_STREAM_IDLE_TIMEOUT_S = STREAM_IDLE_THRESHOLD * STREAM_IDLE_MAX_TICKS
STREAM_IDLE_TIMEOUT_MAX_S = 3600
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
# Per-format retry hints (parse-fail, no-action) live on the dialect
# plugin: ``ReActFormat.static_retry_hint_no_*()``. The unified prefix
# list for filtering system messages out of resume previews lives at
# ``agent_cli.dialects.all_system_user_prefixes()``.
INTERRUPT_NOTICE = "⚡ User interrupted. Waiting for new instructions."


#: Prefixed to the request a resident agent was working on when the session
#: restarted (v9.24.5). The run died without a ``complete``; its history is
#: intact (the ctx resumes), but nothing told the agent to go on — board
#: session 1zfgc2's engine sat idle after a restart with its task half-done
#: and ``floorCeil.js`` left broken. Checking state first matters: the last
#: edit before the cut may have been applied or not.
RESUMED_RUN_NOTICE = (
    "[session restarted] Your previous run was interrupted by a session "
    "restart before you called `complete`. The request you were working on is "
    "quoted below; your history above shows how far you got. First check the "
    "current state of anything you changed — the last edit before the restart "
    "may be half-done. Then finish the request and reply as you normally "
    "would.\n\nOriginal request:\n"
)


def open_debts(port) -> list[dict]:
    """The run's unpaid message/ask debts through its question port — ``[]``
    without a port (v9.22.0 ledger; one reader for the ``complete`` refusal
    and the per-turn tail)."""
    fn = getattr(port, "debts", None) if port is not None else None
    try:
        return list(fn()) if callable(fn) else []
    except Exception:
        return []


# Shown as an observation when a response hits the model's output-token
# limit (stop_reason == "length"). The truncated action is NOT executed
# — the loop records this so the model retries with a smaller unit.
OUTPUT_TRUNCATED_NOTICE = (
    "⚠️ Your previous response was cut off at the output-token limit, so "
    "its action was incomplete and was NOT executed. Retry with a smaller "
    "unit — e.g. build a large file incrementally with `edit_file` instead "
    "of one big `write_file`."
)

# v10.11.0: the cut was the loop's window-fit clamp (the context was full),
# and the loop has just compacted — so the model is told the room is back and
# to re-emit as it was, not to shrink its work (OUTPUT_TRUNCATED_NOTICE's
# "smaller unit" advice would be the wrong lesson here).
CONTEXT_CLAMP_NOTICE = (
    "⚠️ Your previous response was cut off because the context window was "
    "full. Older turns have now been compacted into a summary and the room "
    "is back. The cut action was NOT executed — re-emit it as it was."
)

# v10.10.0: the stream-side runaway detector (providers/runaway.py) stopped
# the generation. Same contract as the cap cut — not executed, retry smaller
# — but the model is told what it did, so it does not blame the context.
RUNAWAY_NOTICE = (
    "⚠️ Your previous response ran away: after its real content it "
    "degenerated into {what}, so the stream was stopped. Its action was "
    "incomplete and was NOT executed. Re-emit the action from the start as "
    "a smaller unit — a shorter command, or build a large file incrementally "
    "with `edit_file`."
)

# v10.19.1: the response ended in the middle of a native tool call. omlx sends
# an in-stream ``incomplete_tool_call`` error instead of a ``length`` finish
# (the unfinished call is withheld, so there is nothing to quote). Same
# contract as the cap cut — not executed, retry smaller — worded for what the
# model can act on: the call was too large for one response.
STOP_INCOMPLETE_TOOL_CALL = "incomplete_tool_call"
INCOMPLETE_TOOL_CALL_NOTICE = (
    "⚠️ Your previous response ended in the middle of a tool call, so the "
    "call was incomplete and was NOT executed. One call was too large for a "
    "single response. Retry with a smaller unit — e.g. write the first part "
    "of a large file with `write_file`, then add the rest in further calls "
    "with `edit_file`."
)

# v10.20.0: the server sent an error inside the stream (anything but the
# incomplete-call code above). Nothing was delivered, so nothing ran; the
# model is told what the server said instead of "your response was empty".
STOP_SERVER_ERROR = "server_error"
SERVER_ERROR_NOTICE = (
    "⚠️ The server reported an error while generating your previous response "
    "({detail}), so nothing was delivered and nothing was executed. Try again."
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
        raise ValueError(
            f"{raw!r} is not a duration — use seconds (600), minutes (10m) or "
            "hours (2h)"
        )
    if value < 0:
        raise ValueError("a duration cannot be negative")
    return value * mult


def format_duration(seconds: float) -> str:
    """초 → 모델이 읽는 기간 — ``"2h"`` · ``"1h49m"`` · ``"5m"`` · ``"45s"`` (v10.79.0).

    `parse_duration` 의 역방향이되 사람이 쓰는 모양으로: 한 시간 이상이면 초를, 자투리
    없는 분이면 "00s" 를 버린다(모니터의 남은 시간 "6540s left" 는 모델이 환산해야
    했다). `Job.elapsed` 와 같은 감각이되 반올림한다 — 남은 시간 1799.9s 는 "30m" 이다."""
    s = max(0, int(seconds + 0.5))
    if s < 60:
        return f"{s}s"
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    if h:
        return f"{h}h{m:02d}m" if m else f"{h}h"
    return f"{m}m{sec:02d}s" if sec else f"{m}m"


#: `wait` 중간 깨우기 간격 (v10.79.0, docs/wait/DESIGN.md §6) — 같은 대상을 다시
#: 기다린 횟수마다 5·10·20·40·60분, 그 뒤로는 60분. 하니스가 고정한다 — 모델이
#: 정하는 인자는 없다.
CHECKIN_STEPS_S: tuple[int, ...] = (300, 600, 1200, 2400, 3600)


# monitor clamp 상수 (docs/monitor/DESIGN.md §10.1·10.2)
# `context/manager.py` 의 STREAM_IDLE_TIMEOUT_{MIN,MAX}_S 와 같은 모양 —
# 거부가 아니라 clamp 다. 값이 크다고 실패시키면 모델이 "얼마가 맞는지"를
# 탐색하느라 턴을 태운다.
MONITOR_DEADLINE_DEFAULT_S = 7200  # 2h — 빠뜨려도 등록이 성공한다
MONITOR_DEADLINE_MIN_S = 60  # 1m
MONITOR_DEADLINE_MAX_S = 86400  # 24h — 그 이상은 배치 작업이고 board 소관
MONITOR_INTERVAL_MIN_S = 60  # 주기 `command` 의 `every` 하한
#: `match` 의 조용함 한도 기본 (v10.47.0): 파일이 이만큼 안 바뀌면 물러난다 (0 = 끔)
MONITOR_SILENT_DEFAULT_S = 600
#: `match` 등록 때 파일이 이미 있고 **모델의 마지막 관찰 이후**에 수정됐으면 이
#: 크기까지는 내용 전부를 새 줄로 본다(커서 0 → 다음 틱 발화). 결과·상태 파일
#: (`done.txt`)과 누적 로그를 가르는 대용 지표 — 로그는 mtime 이 방금이어도 앞쪽
#: 수만 줄이 어제 것이라 0 부터 읽으면 옛 사건이 새 사건으로 보고된다. 0 = 자동
#: 발화 없음(항상 정보만), -1 = 무제한. env `AGENT_CLI_MONITOR_REPLAY_MAX_BYTES`.
MONITOR_REPLAY_MAX_BYTES = 65536
#: 등록 정보에 실을 "마지막 매칭 줄" 을 찾을 때 읽는 꼬리 — 큰 로그도 비용이 고정.
MONITOR_PREVIEW_TAIL_BYTES = 4096


# ── 워크스페이스 스냅샷 (docs/snapshot/DESIGN.md, v10.36.0) ──────────────
#: 체크포인트 최소 간격 — 여러 루프가 턴마다 부르므로 stat 한 바퀴 비용을 묶는다.
SNAPSHOT_MIN_INTERVAL_S = 10.0
#: 상한 장치 — 기본 **무제한**(-1, 사용자 결정). 파일 크기를 넘으면 그 파일만 제외,
#: 파일 수를 넘으면 시작 스냅샷만 찍고 턴 단위 체크포인트를 끈다.
SNAPSHOT_FILE_MAX_BYTES = -1
SNAPSHOT_FILE_COUNT_MAX = -1


def snapshot_knobs() -> dict:
    """env 노브 — `AGENT_CLI_SNAPSHOT=0` 끔, `AGENT_CLI_SNAPSHOT_MIN_INTERVAL_S`,
    `AGENT_CLI_SNAPSHOT_FILE_MAX_BYTES`, `AGENT_CLI_SNAPSHOT_FILE_COUNT_MAX`.
    잘못된 값은 기본값(헤드리스에서 조용한 실패 대신 안전한 기본)."""
    import os

    def _int(name: str, default: int) -> int:
        raw = os.environ.get(name, "").strip()
        if not raw:
            return default
        try:
            return max(-1, int(raw))
        except ValueError:
            return default

    def _float(name: str, default: float) -> float:
        raw = os.environ.get(name, "").strip()
        if not raw:
            return default
        try:
            return max(0.0, float(raw))
        except ValueError:
            return default

    return {
        "enabled": os.environ.get("AGENT_CLI_SNAPSHOT", "1").strip().lower()
        not in ("0", "false", "no", "off"),
        "min_interval_s": _float(
            "AGENT_CLI_SNAPSHOT_MIN_INTERVAL_S", SNAPSHOT_MIN_INTERVAL_S
        ),
        "file_max_bytes": _int(
            "AGENT_CLI_SNAPSHOT_FILE_MAX_BYTES", SNAPSHOT_FILE_MAX_BYTES
        ),
        "file_count_max": _int(
            "AGENT_CLI_SNAPSHOT_FILE_COUNT_MAX", SNAPSHOT_FILE_COUNT_MAX
        ),
    }


def monitor_replay_max_bytes() -> int:
    """운영자 노브 — 잘못된 값은 기본값으로(헤드리스에서 조용한 실패 대신 안전한 기본)."""
    import os

    raw = os.environ.get("AGENT_CLI_MONITOR_REPLAY_MAX_BYTES", "").strip()
    if not raw:
        return MONITOR_REPLAY_MAX_BYTES
    try:
        return max(-1, int(raw))
    except ValueError:
        return MONITOR_REPLAY_MAX_BYTES


_WORD_CHAR = __import__("re").compile(r"[^\W_]", __import__("re").UNICODE)


def has_words(text) -> bool:
    """글자나 숫자가 하나라도 있는가 (v10.1.2) — 구두점·공백뿐인 값을 거른다.

    실측(board nnq141·재현 5런): xml_fc+문법 강제에서 Qwen3.8 Flash 가
    `<parameter=task>\n>\n</parameter>` 처럼 **구두점 하나짜리** task/text 를
    267건 중 8건 냈다. 받은 에이전트는 빈 지시의 뜻을 찾느라 5턴을 썼다.
    언어 무관(유니코드 문자 클래스)."""
    return bool(_WORD_CHAR.search(str(text or "")))


def browser_knobs() -> dict:
    """env 노브 — `AGENT_CLI_BROWSER_TOOL=0` 끔, `AGENT_CLI_BROWSER_HOSTS`(쉼표 목록,
    비면 전부 허용), `AGENT_CLI_BROWSER_EXECUTABLE`(브라우저 실행 파일),
    `AGENT_CLI_BROWSER_HEADLESS` = `auto`(기본: 헤드리스로 시작, 사람 확인에
    막히면 보이는 창으로 전환) | `1`(항상 헤드리스) | `0`(처음부터 보이는 창)."""
    import os

    off = ("0", "false", "no", "off")
    hosts = [
        h.strip().lower().lstrip(".")
        for h in os.environ.get("AGENT_CLI_BROWSER_HOSTS", "").split(",")
    ]
    headless = os.environ.get("AGENT_CLI_BROWSER_HEADLESS", "auto").strip().lower()
    return {
        "enabled": os.environ.get("AGENT_CLI_BROWSER_TOOL", "1").strip().lower()
        not in off,
        "hosts": tuple(h for h in hosts if h),
        "headless": "0"
        if headless in off
        else "1"
        if headless in ("1", "true", "yes", "on")
        else "auto",
        "executable": os.environ.get("AGENT_CLI_BROWSER_EXECUTABLE", "").strip(),
    }


# ── 감사자 (docs/auditor/DESIGN.md, v10.42.0) ──────────────────────────────

#: 하니스 작업 `auditor` 의 이름 — 프로파일 파일(`agents/harness/auditor.md`),
#: 런 디렉토리(`harness/auditor/<a>-<b>/`), 메일의 `job`, main 채널 칩이 전부 이
#: 하나다.
AUDITOR_NAME = "auditor"

# ── 소유자 주소 (v10.84.0, docs/wait/DESIGN.md §7) ─────────────────────────
# 잡·모니터·체크인·깨우기 판정은 전부 소유자 주소로 돈다. "main" 은 메일함(펌프가
# 읽음), "agent:<key>" 는 상주의 받은편지함(워커가 읽음). 그 밖의 둘은 **private
# 주소** — 프로세스 수명의 메일함을 그 루프 자신이 턴 경계와 `wait` 에서 읽는다:
# one-shot 서브에이전트(종전엔 부모 주소를 빌려 써서 자기 잡의 통지가 부모에게
# 갔다)와 하니스 작업(주소만 있고 메일함이 없어 통지가 버려졌다).
RUN_OWNER_PREFIX = "run:"
HARNESS_OWNER_PREFIX = "harness:"
_PRIVATE_OWNER_PREFIXES = (RUN_OWNER_PREFIX, HARNESS_OWNER_PREFIX)


#: 하니스 통지(잡·모니터·체크인)의 레코드 계약 — 한 표 (v10.84.0, 사용자 결정 C):
#: 어느 루프(main·상주·one-shot)에 가든 봉투 없이 보고 그대로, ``tool`` 은 이 표.
#: "도구가 만든 보고는 봉투 밖" (`build_reply_record`) — 보낸이가 하니스 하나라 ``from``
#: 이 정보가 아니다. 봉투는 에이전트끼리의 요청·회신·죽음 통지의 것. 체크인은 `wait`
#: 의 뒤따름이라 그 도구 이름.
HARNESS_MAIL_TOOL = {"job": "job", "monitor": "monitor", "checkin": "wait"}


def harness_mail_tool(kind: str) -> str | None:
    """하니스 통지면 레코드의 ``tool``, 아니면 None."""
    return HARNESS_MAIL_TOOL.get(kind or "")


def is_private_owner(addr: str) -> bool:
    """main 도 상주도 아닌 소유자 — 메일함을 루프 자신이 읽는 주소."""
    return addr.startswith(_PRIVATE_OWNER_PREFIXES)


#: main 의 사용자 런이 끝날 때마다 하니스가 감사 작업에 주는 과제
#: (`harness.auditor.SessionAuditor.run_ended`) — 감사자가 알아야 할 하니스 사실은
#: 셋뿐이다: 세션 id(history 도구의 범위·파일 필터), 감사할 서수 구간, 그리고
#: 보고는 `complete` 의 result 라는 것(하니스가 그걸 main 에게 전한다 — 4판).
#: 켜고 끄기는 노브 `auditor` (`knobs.KNOBS`: env AGENT_CLI_AUDITOR · --auditor · 🔎 칩).
AUDIT_REQUEST = (
    "Audit main's run that just ended: history seq {a}..{b} of session {session} "
    "(main's history file: {history}).\n"
    'Every history call must pass `sessions` "{session}" and the filter '
    "`loc LIKE '{session}/history.jsonl:%'` (that table also holds the session's "
    "other agents' files), and read only rows with `seq BETWEEN {a} AND {b}`.\n"
    "Judge every request in that range as your profile says. Main's final answer "
    "ends with `Checked:` / `Not checked:` lines — your checklist, not evidence: "
    "each Checked item needs a record in the range. Your `complete` result is the "
    "report — the harness delivers it to main; there is nobody else to tell."
)
