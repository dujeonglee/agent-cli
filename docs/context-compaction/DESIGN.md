# Context Compaction — Design

> Status: Draft
> Date: 2026-05-22
> Owner: claude (RFC)
> Companion: [REQUIREMENTS.md](REQUIREMENTS.md), [TEST_PLAN.md](TEST_PLAN.md)

## 0. 아키텍처 개요

```
ContextManager.add(message)
   │
   │ cache.append(message); _cache_tokens += ...
   ↓
ContextManager._maybe_compact()           ← 신규 (replaces _evict)
   │
   ├─ if _cache_tokens > 0.9 * max_context_tokens:
   │     try:
   │         summary, file_list = _compact()         ← 신규
   │         (LLM 호출 + script 추출)
   │     except CompactionError:
   │         render_status("warning", "Compaction failed, falling back to FIFO")
   │         _evict_fifo()                            ← 기존 동작 그대로
   │
   ├─ if success:
   │     _summary = summary
   │     _file_list.update(new_paths)                ← 누적
   │     _cache = anchor + new dynamic               ← 절반 evict 결과
   │     _save_compaction_json()
   ↓
ContextManager.get_messages()              ← 변경
   │
   │ if _summary: yield [system, summary_msg, file_list_msg, ...dynamic]
   │ else:        yield _cache (기존 동작)
   ↓
LLM 호출
```

## 1. 새 상태 (ContextManager 인스턴스 변수)

```python
# 신규 인스턴스 변수
self._summary: str = ""              # 누적 요약. 빈 문자열 = 미발생
self._file_list: list[str] = []      # 누적 파일 path (deduped, sorted)
self._compaction_count: int = 0      # 트리거 횟수. 디버깅 / 측정
self._last_compacted_at: str = ""    # ISO timestamp. compaction.json 동기화 검증

# 기존
self._cache: list[dict] = ...
self._cache_tokens: int = ...
self.max_context_tokens: int = ...
```

## 2. 핵심 메서드

### 2.1 `_maybe_compact() -> None`

`add()`의 마지막에 호출 (기존 `_evict()` 자리). 90% threshold + belt-and-braces fallback.

```python
def _maybe_compact(self) -> None:
    """Trigger compaction when cache exceeds 90% of budget.

    Two-layer safety:
      1. Try ``_compact()`` (LLM summarisation + cache rebuild).
      2. After (1) — success or failure — if cache is *still* over the
         threshold, drop oldest with plain FIFO until in budget.

    Layer 2 catches two distinct cases under one path:
      (a) ``_compact()`` raised CompactionError (LLM failure, etc.).
      (b) ``_compact()`` succeeded but the resulting cache —
          ``anchor + summary (≤2000 tok) + file_list + retained half`` —
          is itself larger than ``threshold``. This is rare but real
          when ``max_context_tokens`` is small (e.g. 4K) and the
          summary cap dominates the dynamic half.
    The single FIFO fallback handles both without per-case branching.
    """
    threshold = int(self.max_context_tokens * _COMPACTION_THRESHOLD_RATIO)
    if self._cache_tokens <= threshold:
        return
    try:
        self._compact()
    except CompactionError as e:
        render_compaction_progress(phase="warning", reason=str(e))
        # falls through to the belt-and-braces FIFO below
    # Belt-and-braces: drop oldest until in budget. Idempotent — no-op
    # when ``_compact()`` already brought the cache below the threshold.
    if self._cache_tokens > threshold:
        self._evict_fifo()
```

### 2.2 `_compact() -> None`

핵심 흐름. evict 대상 선정 → 요약 → 파일 추출 → cache 재구성 → 영속화.

```python
def _compact(self) -> None:
    """Evict ~half of dynamic messages, summarise them, accumulate
    file list, persist to compaction.json. Raises CompactionError on
    LLM / I/O failure."""
    anchor, evict_set, retained = self._split_for_compaction()

    if not evict_set:
        return  # nothing to compact (cache too small)

    # LLM 호출 — 실패 시 CompactionError raise
    new_summary = self._summarize_messages(evict_set)

    # 스크립트 추출 — 실패 시 빈 리스트 (compaction 자체는 성공)
    new_paths = self._extract_file_paths(evict_set)

    # 누적 — 이전 요약과 evict 절반을 합쳐 새 요약 생성 (재귀 단계)
    if self._summary:
        # 두 번째 이후 compaction: 이전 요약을 prefix로 LLM에게 주고
        # 새 evict 메시지와 함께 더 큰 요약 생성. 길이 cap 적용.
        new_summary = self._merge_summaries(self._summary, new_summary)

    self._summary = new_summary
    self._file_list = self._merge_file_lists(self._file_list, new_paths)
    self._cache = anchor + retained
    self._cache_tokens = self._recompute_tokens()
    self._compaction_count += 1
    self._last_compacted_at = _now_iso()
    self._save_compaction_json()
```

### 2.3 `_split_for_compaction() -> tuple[anchor, evict, retained]`

```python
def _split_for_compaction(
    self,
) -> tuple[list[dict], list[dict], list[dict]]:
    """Partition cache into three regions:
       anchor    = [system prompt only]   (영구 보존)
       evict     = oldest dynamic, ~50% tokens of remainder
       retained  = newer dynamic, the rest
    
    Last user query is naturally in ``retained`` (newest end). No
    explicit safeguard needed.
    """
    anchor: list[dict] = []
    dynamic_start = 0
    if self._cache and self._cache[0].get("role") == "system":
        anchor = [self._cache[0]]
        dynamic_start = 1
    dynamic = self._cache[dynamic_start:]

    dynamic_tokens = sum(_estimate_message_tokens(m) for m in dynamic)
    target_evict = dynamic_tokens // 2

    evict: list[dict] = []
    evicted_tokens = 0
    for msg in dynamic:
        if evicted_tokens >= target_evict:
            break
        evict.append(msg)
        evicted_tokens += _estimate_message_tokens(msg)

    retained = dynamic[len(evict):]
    return anchor, evict, retained
```

### 2.4 `_summarize_messages(messages) -> str`

LLM 호출 한 번 — 재귀 단계도 같은 호출 path.

```python
def _summarize_messages(self, messages: list[dict]) -> str:
    """Call the main LLM to summarise evicted messages. Raises
    CompactionError on provider error / parse failure."""
    if not messages:
        return ""
    if self._compactor_callback is None:
        raise CompactionError("no compactor callback registered")
    try:
        summary = self._compactor_callback(messages)
    except Exception as e:  # noqa: BLE001 — provider boundary
        raise CompactionError(f"summariser raised: {e}") from e
    if not isinstance(summary, str) or not summary.strip():
        raise CompactionError("summariser returned empty/non-string")
    return summary
```

### 2.5 재귀 단계 — 단일 호출

이전엔 두 단계 LLM 호출 (evict → new summary, merge(prev, new) → final). 두 호출은:
- 비용 2배 (요약 LLM 호출 두 번)
- 첫 호출이 prev 컨텍스트 없이 만들어져 정보 손실 가능 (예: "사용자가 원래 X 요청했다"는 사실 모르고 evict만 봄)

**개선: prev summary를 evict messages 앞에 prior context로 prepend → 한 번의 호출**:

```python
# _compact() 안
if self._summary:
    prior_context_msg = {
        "role": "user",
        "content": (
            "## Running summary of earlier conversation\n\n"
            f"{self._summary}\n\n"
            "Below are NEW messages to fold into this running summary. "
            "Produce one updated summary."
        ),
    }
    summarize_input = [prior_context_msg] + list(evict_set)
else:
    summarize_input = list(evict_set)

new_summary = self._summarize_messages(summarize_input)
```

- 첫 compaction: prev 없음 → evict만 input
- 재귀 단계: prev가 context로 들어가 LLM이 작업 도메인 전체 인식 + 일관된 통합 summary 생성
- LLM 호출 1번 — 비용 절반

### 2.6 `_extract_file_paths(messages) -> list[str]`

스크립트 추출 (LLM 호출 없음). tool record 기반.

```python
_PATH_TOOLS = {"write_file", "edit_file", "read_file", "code_index"}

def _extract_file_paths(self, messages: list[dict]) -> list[str]:
    """Walk evicted messages, extract path field from known tool
    invocations. Shell commands skipped (FR-CC-5 decision)."""
    paths: list[str] = []
    for msg in messages:
        # Tool result entry shape: {role:user, tool:<name>, args:{...}, content:...}
        tool = msg.get("tool")
        if tool in _PATH_TOOLS:
            args = msg.get("args", {})
            path = args.get("path", "")
            if path and path not in paths:
                paths.append(path)
        # Assistant action entry shape: {role:assistant, action:<name>, action_input:{...}}
        action = msg.get("action")
        if action in _PATH_TOOLS:
            ai = msg.get("action_input", {})
            if isinstance(ai, dict):
                path = ai.get("path", "")
                if path and path not in paths:
                    paths.append(path)
        # Delegate: subagent별 결과는 별도 디렉토리 — 그 안의 액션은
        # 부모 ctx에서 접근 불가. v1는 delegate 자체만 path 추가.
        if action == "delegate":
            ai = msg.get("action_input", {})
            if isinstance(ai, dict):
                tasks = ai.get("tasks", [])
                for t in tasks if isinstance(tasks, list) else []:
                    if isinstance(t, dict) and "agent" in t:
                        paths.append(f"<delegate:{t['agent']}>")
    return paths
```

### 2.7 `get_messages() -> list[dict]`

기존 시그니처 유지. 요약/파일리스트 있을 때 합성 메시지 prepend.

```python
def get_messages(self) -> list[dict]:
    """Return cached messages converted to natural language for LLM.
    
    When a summary is present, prepend two synthesised messages right
    after the system prompt: one with the recursive summary, one
    with the accumulated file list. Wire format plugins handle them
    as plain user-role messages (no format-specific conversion)."""
    result: list[dict] = []
    cache = self._cache

    if cache and cache[0].get("role") == "system":
        result.append(_to_natural_language(cache[0], self.wire_format))
        cache_rest = cache[1:]
    else:
        cache_rest = cache

    if self._summary:
        result.append({
            "role": "user",
            "content": f"## Summary of earlier conversation\n\n{self._summary}",
        })
    if self._file_list:
        listing = "\n".join(f"- {p}" for p in self._file_list)
        result.append({
            "role": "user",
            "content": f"## Files touched in earlier turns\n\n{listing}",
        })

    result.extend(
        _to_natural_language(msg, self.wire_format) for msg in cache_rest
    )
    return result
```

## 3. `compaction.json` 스키마

`session_dir/compaction.json`:

```json
{
  "version": 1,
  "summary": "user requested X, agent read A.py and B.py, found...",
  "file_list": [
    "agent_cli/main.py",
    "agent_cli/render/web.py",
    "<delegate:explorer>"
  ],
  "compaction_count": 3,
  "last_compacted_at": "2026-05-22T15:30:00Z",
  "dynamic_start_index": 47
}
```

### 필드

- ``version`` — 1. Forward-compat: 미지의 버전은 무시 + 빈 상태로 초기화.
- ``summary`` — 누적 요약 텍스트.
- ``file_list`` — 누적 path (sorted, deduped).
- ``compaction_count`` — 트리거 횟수. 측정 / 디버그.
- ``last_compacted_at`` — ISO 8601 timestamp.
- **``dynamic_start_index``** — ``history.jsonl`` 에서 dynamic 영역이
  시작하는 0-based offset. 즉 ``history[0:dynamic_start_index]`` 가
  evict + 요약된 영역. Resume 시 정확히 ``history[dynamic_start_index:]``
  를 cache로 로드해 summary 와 시간적으로 어긋나지 않게 보장.

### Resume 흐름 (변경)

기존: ``history.jsonl`` 끝부터 budget까지 reverse 로드.

신규:
1. ``compaction.json`` 있고 ``dynamic_start_index >= 0`` 이면:
   - ``history[dynamic_start_index:]`` 만 forward 로드 (시간순)
   - budget 초과 시점에 oldest drop으로 trim — 단 일반적으로
     초과 안 함 (compaction 후 약 50% 였으니까)
2. ``compaction.json`` 없거나 invalid:
   - 기존 동작 (history 끝부터 reverse 로드 + budget)

이렇게 하면 budget 변경 (예: 10K → 20K) 후 resume 해도 summary 가
이미 요약한 메시지가 dynamic 에 *다시* 들어오지 않음.

### Edge case

- ``dynamic_start_index`` 가 ``len(history)`` 초과면 (history 가
  외부에서 잘렸거나 손상) → invalid, 기존 동작 fallback.
- ``compaction.json`` 의 ``summary`` 와 ``history.jsonl`` 끝 부분 사이
  consistency 는 ``dynamic_start_index`` 한 필드로 강제 — 외부 도구가
  history 만 수정하고 compaction.json 안 건드리면 invariant 깨짐
  (사용자가 직접 편집한 경우만). 일반 사용 경로 (run/chat/web) 는
  ContextManager 가 둘을 atomic 하게 같이 update.

## 4. 사용자 가시화 — render 모듈에 helper 집중

**원칙**: ContextManager는 UI 출력 직접 호출 X. `render_status` 같은 일반 메서드도 ContextManager가 직접 부르면 동일 텍스트 fragment가 호출처마다 흩어짐 — 향후 progress bar, spinner, web-specific 이벤트 추가 시 grep으로 찾아 다니는 패치. **모든 compaction 관련 UI 렌더링은 `agent_cli/render/__init__.py`의 helper 한 곳에 모은다.**

### 4.1 신규 helper (render 모듈)

`agent_cli/render/__init__.py`:

```python
from typing import Literal

CompactionPhase = Literal["start", "done", "warning"]

def render_compaction_progress(
    *,
    phase: CompactionPhase,
    old_tokens: int = 0,
    new_tokens: int = 0,
    evicted_count: int = 0,
    reason: str = "",
) -> None:
    """Surface compaction lifecycle to the user. Single entry point —
    ContextManager calls this helper rather than ``render_status``
    directly so the text shape (and any future presentation upgrade
    such as a progress bar, dedicated SSE event, or web toast) stays
    in one place.

    Phase semantics:
      - ``start``: just before LLM summarisation begins.
      - ``done``: after cache rebuilt with new summary + retained.
      - ``warning``: LLM summarisation failed; falling back to FIFO drop.
    """
    if phase == "start":
        _renderer.status(
            "info",
            f"Compacting context ({old_tokens:,} tokens, "
            f"{evicted_count} messages → summary)",
            0,
        )
    elif phase == "done":
        _renderer.status(
            "info",
            f"Compaction done ({old_tokens:,} → {new_tokens:,} tokens)",
            0,
        )
    elif phase == "warning":
        _renderer.status(
            "warning",
            f"Context compaction failed ({reason}); using FIFO drop instead.",
            0,
        )
```

### 4.2 ContextManager 호출 패턴

```python
# manager.py — _compact() 안에서
from agent_cli.render import render_compaction_progress

old_tokens = self._cache_tokens
render_compaction_progress(
    phase="start",
    old_tokens=old_tokens,
    evicted_count=len(evict_set),
)
# ... 본 작업 ...
render_compaction_progress(
    phase="done",
    old_tokens=old_tokens,
    new_tokens=self._cache_tokens,
)

# _maybe_compact() fallback path
render_compaction_progress(phase="warning", reason=str(e))
```

### 4.3 Surface 별 동작 (helper 내부에서 분기 X — `_renderer.status`가 처리)

- **CLI (MinimalRenderer.status)**: console.print 한 줄
- **Web (WebRenderer.status)**: SSE `status` 이벤트 → 프론트가 자동 표시 (기존 메커니즘)

`_renderer.status` 인터페이스 그대로 사용 — 새 abstract method 추가 안 함. 미래에 compaction 전용 SSE 이벤트 (progress bar 등)가 필요해질 때, 그 변경은 `render_compaction_progress` 본문에 모임. ContextManager / loop.py 변경 없음.

### 4.4 진짜 새 abstraction 필요 시점

다음 중 하나가 발생하면 helper를 넘어 abstract method 추가 검토:
- Compaction이 long-running (수초~수십초)으로 진행 progress bar 필요
- Web 프론트엔드에 dedicated UI (예: 헤더에 compaction count badge) 추가
- 사용자가 compaction을 명시적으로 trigger / cancel하는 인터랙션

v1은 helper로 충분. abstract method 추가는 위 조건 충족 시 별도 작업.

## 5. 자명한 default + 상수

```python
_COMPACTION_THRESHOLD_RATIO = 0.9       # FR-CC-2: 90%
_SUMMARY_CHAR_CAP = 8000                 # ~2000 tokens (4 chars/token 추정)
_PATH_TOOLS = {"write_file", "edit_file", "read_file", "code_index"}
```

## 6. AgentLoop 변경

### 6.1 Compactor callback 주입

`ContextManager`는 provider 직접 핸들 없음. compactor callback 주입:

```python
# loop.py AgentLoop._setup() 안
if self.ctx is not None and self._compaction_enabled():
    self.ctx.set_compactor(
        lambda msgs: self._llm_compact_summarize(msgs)
    )

def _compaction_enabled(self) -> bool:
    """NFR-CC-5: disabled via CLI flag or env var."""
    import os
    env = os.environ.get("AGENT_CLI_COMPACTION", "").strip().lower()
    if env in ("off", "false", "0", "disabled"):
        return False
    return getattr(self, "compaction_enabled", True)

def _llm_compact_summarize(self, messages: list[dict]) -> str:
    """Internal: call provider with a summarisation prompt + the
    evicted messages, return the summary text."""
    summarisation_prompt = (
        "Summarise the conversation below concisely. Preserve "
        "(a) the user's original intent, (b) key actions taken "
        "(tools used, files touched), (c) decisions made, (d) "
        "outcomes / discoveries. Stay under 2000 tokens. Plain text."
    )
    request_messages = [
        {"role": "system", "content": summarisation_prompt},
        *messages,
    ]
    response = self.provider.call(
        model=self.model,
        messages=request_messages,
        max_tokens=2000,
        # No streaming, no tool calling — pure text completion.
    )
    return response.content
```

### 6.2 CLI flag — `--no-compaction`

`main.py` 의 `run` / `chat` / `web` 세 명령 모두 동일 옵션:

```python
no_compaction: bool = typer.Option(
    False,
    "--no-compaction",
    help=(
        "Disable context compaction (LLM summarisation when cache "
        "exceeds 90% of budget). Falls back to plain FIFO drop. "
        "Use for measurement baseline / debugging."
    ),
)
```

`AgentLoop` 생성자에 `compaction_enabled=not no_compaction` 전달.
환경변수 ``AGENT_CLI_COMPACTION=off`` 가 CLI flag 보다 우선.

### 6.3 TurnRecorder — compaction event 기록

`agent_cli/recovery/observability.py` 의 `TurnRecorder` 에 메서드 추가:

```python
def record_compaction(
    self,
    *,
    tokens_before: int,
    tokens_after: int,
    evicted_count: int,
    fallback_used: bool,
    failure_signal: str | None,
    duration_ms: float,
) -> None:
    """Append a compaction event to turns.jsonl. Separate record
    type ('event': 'compaction') from per-turn records so analysis
    scripts can filter cleanly. No-op when recorder is disabled."""
    if not self.enabled or not self._fh:
        return
    row = {
        "event": "compaction",
        "tokens_before": tokens_before,
        "tokens_after": tokens_after,
        "evicted_count": evicted_count,
        "fallback_used": fallback_used,
        "failure_signal": failure_signal,
        "duration_ms": duration_ms,
        "timestamp": _now_iso(),
    }
    self._fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    self._fh.flush()
```

ContextManager 측에서 호출:

```python
# manager.py _compact() 안 (시작/종료)
t0 = time.monotonic()
fallback_used = False
failure_signal: str | None = None
old_tokens = self._cache_tokens
try:
    # ... 본 작업 ...
except CompactionError as e:
    failure_signal = "summary_failed"
    raise  # _maybe_compact 가 belt-and-braces 처리
finally:
    duration_ms = (time.monotonic() - t0) * 1000
    if self._recorder is not None:
        self._recorder.record_compaction(
            tokens_before=old_tokens,
            tokens_after=self._cache_tokens,
            evicted_count=len(evict_set),
            fallback_used=fallback_used,
            failure_signal=failure_signal,
            duration_ms=duration_ms,
        )
```

`AgentLoop._setup()` 에서 `self.ctx.set_recorder(self.recorder)` 호출
(`set_compactor` 와 같은 패턴, 단순 setter).

## 7. 변경 파일 목록

| 파일 | 변경 LOC (추정) | 종류 |
|---|---|---|
| `agent_cli/context/manager.py` | +200 / -10 | compaction logic, state, get_messages, dynamic_start_index |
| `agent_cli/context/_file_extract.py` (신규) | +60 | 스크립트 추출 헬퍼 (test 가능하도록 분리) |
| `agent_cli/loop.py` | +40 | compactor callback + recorder 주입, `_compaction_enabled` |
| `agent_cli/main.py` | +15 | `--no-compaction` flag (run/chat/web 세 명령) |
| `agent_cli/recovery/observability.py` | +25 | `TurnRecorder.record_compaction` 메서드 |
| `agent_cli/render/__init__.py` | +35 | `render_compaction_progress` helper (compaction 관련 모든 UI 렌더링 단일 진입점) |
| `README.md` | +20 | `--no-compaction` 옵션 설명 + 비활성 시 동작 + 사용 가이드 |
| `tests/test_context_compaction.py` (신규) | +350 | 단위 + 통합 + 회귀 |
| `docs/ARCHITECTURE.md` | +30 | compaction flow + dynamic_start_index 섹션 |
| `docs/context-compaction/{REQUIREMENTS,DESIGN,TEST_PLAN}.md` | +1100 (신규) | RFC |

**총 ~775 LOC** + RFC 1100줄.

## 8. 위험 / 결정 보류

### 8.1 LLM 비용

- Compaction 1회 = LLM 호출 1회 (요약 ~2000 토큰 입력 + ~1000 토큰 출력)
- 긴 세션에서 trigger 빈도가 너무 잦으면? 매 N turn마다 compaction = 응답 시간 ↑
- 측정: turns.jsonl에 compaction event 기록, 빈도 분석

### 8.2 요약 품질

- LLM이 핵심 누락 가능 (작은 모델 우려)
- v1 검증: 사용자 직접 long session run + 결과 평가
- 개선 옵션 (future): few-shot prompt, 도구 호출 빈도 별 가중치

### 8.3 재귀 요약 drift

- "이전 요약 + 새 evict 요약 → 통합 요약" 반복 시 의미 drift
- N회 compaction 후 원래 의도와 어긋날 수 있음
- 측정: 사용자가 세션 후반에 초기 작업 명시적으로 언급해 LLM 인식 확인

### 8.4 anchor 변경의 영향

- 첫 user query를 anchor에서 제거 — 첫 요약에 첫 query 본문 포함되어야 LLM이 그 의도 알 수 있음
- _summarize_messages가 첫 evict 시 첫 query를 evict에 포함하므로 자연 처리. 단 LLM 요약 prompt가 "preserve user's original intent" 같이 명시되어야 효과적

### 8.5 ContextManager의 LLM 의존

- 기존 ContextManager는 provider 무관 (token estimation만)
- compactor callback 주입으로 inversion of control — ContextManager가 LLM 직접 호출 안 함, AgentLoop이 주입
- 테스트 시 callback을 stub로 대체 가능 — 단위 테스트에서 실제 LLM 호출 안 함

### 8.6 Delegate sub-agent의 파일 액션

- subagent는 별도 ContextManager 인스턴스 (`session_dir/delegate_*`)
- parent의 _file_list에 subagent 내부 파일 액션 누락
- v1는 `<delegate:agent_name>` placeholder만. v2에서 delegate 결과 metadata 통해 통합 검토
