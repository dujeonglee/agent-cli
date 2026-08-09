# 구현 계획 — L-1 / L-25: overflow 오탐지 → provider 경계 typed error

> **상태**: ✅ **완료 (v8.6.1, 2026-08-09)** — 범위 A 구현·전체 3338 테스트 무회귀·뮤테이션 검증·wheel release 완료.
> **핸드오프**: 이 문서만 읽으면 다른 세션(Opus)이 이어서 완결 가능하도록 작성.
> **관련 감사 항목**: `docs/AUDIT-2026-08-09.md` L-1(P1, 히스토리 영구 파쇄) · L-25(P2, 근본 시임)
> **semver**: wire/resume 포맷 무변경(런타임 분류만) → **PATCH (8.6.0 → 8.6.1)**. resume 하위호환 유지.
> **작성**: 2026-08-09

---

## 1. 문제 (근본 원인)

`loop/llm.py` 의 LLM 호출 예외 핸들러가 **임의 예외의 문자열**을 overflow 로 오분류한다:

```python
# agent_cli/loop/llm.py:174-176 (현재)
except Exception as e:
    if (
        is_context_overflow(str(e))          # ← bare 문자열 매칭
        and self.ctx
        and self.overflow_retries < _MAX_OVERFLOW_RETRIES
    ):
        actual, limit = parse_overflow_amounts(str(e))
        ...
        if self.ctx.force_fit(target, actual_tokens=actual):   # ← 파괴적
```

`is_context_overflow` 패턴에는 bare `r"context window"`, `r"input.*too long"`, `r"token limit"` 등이 있어(`context/overflow.py:8-32`), **HTTP 상태와 무관하게** 그 문구를 담은 비-overflow 예외(설정 오류, 프록시 500 페이지, 네트워크 예외 메시지)가 `force_fit` 을 유발한다.

`force_fit`(`context/manager.py:699-716`)은 FIFO evict + `_dynamic_start_index` 전진 + `compaction.json` 영속화를 최대 5회 수행 → **resume 가 전진된 오프셋을 읽어 손실이 영구화**된다.

경계 지점(`http.raise_for_status_with_body`, `providers/http.py:99-105`)은 status+body 를 모두 아는 **유일한 곳**인데 지금은 body 포함 generic `requests.HTTPError` 만 던진다:

```python
# agent_cli/providers/http.py:99-105 (현재)
try:
    r.raise_for_status()
except requests.HTTPError as e:
    body = (r.text or "").strip()
    if not body:
        raise
    raise requests.HTTPError(f"{e}: {body[:max_body]}", response=r) from e
```

---

## 2. 설계 (typed error 를 경계에서 발생 + 상태 게이트)

**핵심 불변식**: `force_fit`(파괴적)은 **오직 HTTP 400/413 + overflow 본문**일 때만 발화한다. 비-HTTP 예외·다른 상태코드·overflow 아닌 400 은 절대 force_fit 하지 않고 그대로 전파한다.

두 계층:
1. **경계(선호 경로)**: `raise_for_status_with_body` 가 `status==400 && is_context_overflow(body)` 이면 typed `ContextOverflowError(actual, limit)` 를 raise. body 에서 이미 깨끗이 파싱.
2. **루프(견고 폴백)**: `loop/llm.py` 는 `classify_overflow(e)` 로 판정 — typed error 면 그 amounts, 아니면 **HTTPError 이고 response.status_code ∈ {400,413} 이고 is_context_overflow(str(e))** 일 때만 amounts, 그 외 `None`. `None` 이면 force_fit 안 하고 예외 재전파.

폴백을 두는 이유: 모든 provider 가 반드시 `raise_for_status_with_body` 를 거친다고 단정할 수 없으므로(anthropic/openai 경로 편차), 상태-게이트 폴백이 recovery 를 보존하면서도 오탐지는 막는다.

### 왜 이게 "가장 어려운" 항목인가
단일 파일 버그가 아니라 **추상화 시임 재설계**다. 분류 지식이 3곳(loop, capabilities×2)에 흩어져 각자 정규식을 돌리는 것을, 타입이 소유하는 경계로 수렴시킨다. 스트리밍 200 경로(`r.text` 안 읽음)·omlx 400 본문 파싱·provider 편차를 안 깨야 한다.

---

## 3. 파일별 변경 (정확한 스케치)

### 3.1 `agent_cli/context/overflow.py` — 예외 + 분류기 추가
`is_context_overflow` 정의 아래에 추가:

```python
class ContextOverflowError(Exception):
    """provider 가 요청을 context-overflow(HTTP 400/413 + overflow 본문)로
    거부했을 때 경계에서 raise. 서버 보고 수치를 실어 루프가 문자열 재파싱을
    할 필요가 없게 한다. actual/limit 는 best-effort(본문에 없으면 None)."""

    def __init__(
        self, actual: int | None, limit: int | None, message: str = ""
    ) -> None:
        super().__init__(message or "context overflow")
        self.actual = actual
        self.limit = limit


# HTTP 상태를 게이트로 쓰는 단일 판정점. 파괴적 force_fit 은 이 함수가
# non-None 을 줄 때만 발화 → 오탐지(비-HTTP·비-400·overflow아닌 400) 소멸.
_OVERFLOW_STATUS = frozenset({400, 413})


def classify_overflow(exc: Exception) -> tuple[int | None, int | None] | None:
    """overflow 로 취급할 예외면 (actual, limit), 아니면 None.

    - ContextOverflowError → 실린 수치 그대로.
    - HTTP 상태 400/413 + overflow 본문 → 본문에서 파싱(경계를 안 거친
      provider 를 위한 폴백).
    - 그 외 전부 None(문구가 우연히 겹쳐도 force_fit 안 함).
    """
    if isinstance(exc, ContextOverflowError):
        return exc.actual, exc.limit
    status = getattr(getattr(exc, "response", None), "status_code", None)
    if status in _OVERFLOW_STATUS and is_context_overflow(str(exc)):
        return parse_overflow_amounts(str(exc))
    return None
```

> 주의: `overflow.py` 는 내부 의존 0(`import re` 만) → `providers/http` 가 이를 import 해도 순환 없음(acyclic). 이 파일이 overflow 의미론의 origin 이므로 여기 둔다.

### 3.2 `agent_cli/providers/http.py` — 경계에서 typed raise
파일 상단 import 에 추가:
```python
from agent_cli.context.overflow import (
    ContextOverflowError,
    is_context_overflow,
    parse_overflow_amounts,
)
```
`raise_for_status_with_body` 의 except 분기 교체:
```python
    try:
        r.raise_for_status()
    except requests.HTTPError as e:
        body = (r.text or "").strip()
        if not body:
            raise
        # 경계: status+body 를 아는 유일 지점. 400 overflow 를 typed error 로
        # 승격해 루프가 임의 예외를 문자열-매칭하지 않게 한다(오탐지 시
        # 히스토리 파괴 — docs/AUDIT-2026-08-09.md L-1).
        if r.status_code == 400 and is_context_overflow(body):
            actual, limit = parse_overflow_amounts(body)
            raise ContextOverflowError(actual, limit, body[:max_body]) from e
        raise requests.HTTPError(f"{e}: {body[:max_body]}", response=r) from e
```

### 3.3 `agent_cli/loop/llm.py` — typed catch, 오탐지 제거
`from agent_cli.context.overflow import classify_overflow` (또는 기존 import 라인 확장). 예외 핸들러(현 174-200 부근)를:
```python
    except Exception as e:
        amounts = classify_overflow(e)
        if amounts is None:
            raise                      # 비-overflow → force_fit 절대 금지
        if not (self.ctx and self.overflow_retries < _MAX_OVERFLOW_RETRIES):
            raise                      # ctx 없거나 재시도 소진 → 깔끔히 실패
        actual, limit = amounts
        budget = self.ctx.max_context_tokens
        target = int((limit or budget) * self.ctx.compaction_ratio)
        if self.ctx.force_fit(target, actual_tokens=actual):
            self.overflow_retries += 1
            render_status("running", f"Context overflow — shrinking and retrying ...")
            self.state.messages = self.ctx.get_messages()
            self.state.turn -= 1
            return _RETRY
        raise                          # 더 못 줄이면 실패(anchor-only)
```
> 현재 로직의 `is_context_overflow(str(e))` / `parse_overflow_amounts(str(e))` 직접 호출을 `classify_overflow` 로 대체. `overflow_retries=0` 리셋(성공 경로, 현 ~171행)은 **그대로 유지**.

### 3.4 (범위 밖, 옵션) `providers/capabilities.py:391,500`
프로브 층의 `is_context_overflow` 문자열 사용. **이 계획의 기본 범위(A)에서는 무변경** — 프로브는 임의 엔드포인트 대상이라 파괴 경로가 아니고, `raise_for_status_with_body` 미경유라 별도 배선 필요. L-25 완전체는 follow-up 으로 분리(아래 §7).

---

## 4. 테스트 계획 (뮤테이션으로 무는지 증명)

`tests/` 에 추가/갱신 (기존 overflow 테스트 파일 위치 먼저 확인: `grep -rl overflow tests/`):

1. **`ContextOverflowError`**: actual/limit 보존.
2. **`classify_overflow`** (핵심 회귀):
   - `ContextOverflowError(360012, 262144)` → `(360012, 262144)`.
   - `HTTPError` (response.status_code=400, 본문 omlx 문구) → 파싱된 amounts.
   - **`HTTPError` (status=500, 본문에 "context window") → `None`** ← 오탐지 킬.
   - **비-HTTP `Exception("... context window ...")` → `None`** ← 오탐지 킬.
   - `HTTPError` (status=400, overflow 아닌 본문) → `None`.
3. **`raise_for_status_with_body`**: 가짜 400+omlx 본문 → `ContextOverflowError` (amounts 정확); 400+비-overflow → `requests.HTTPError`; 500+overflow-ish → `HTTPError`(typed 아님).
4. **loop/llm 회귀** (L-1 증명): `provider.call` 이
   - `Exception("...context window...")`(response 없음) raise → **force_fit 미호출**, 예외 전파, `ctx._dynamic_start_index` 불변. (뮤테이션: 옛 `except Exception + is_context_overflow(str(e))` 로 되돌리면 이 테스트 실패)
   - `ContextOverflowError(...)` raise → `force_fit` 호출 + `_RETRY`.

검증 명령:
```
python -m pytest tests/ -k "overflow or llm or capabilit or context" -q
python -m pytest tests/ -q          # 전체 무회귀
ruff check agent_cli/ tests/
ruff format --check agent_cli/ tests/
```

---

## 5. 문서 동기 (CLAUDE.md 규칙 3·6)

- **`docs/ARCHITECTURE.md §3.2`**: 의존 관계에 `providers/http → context/overflow (ContextOverflowError, is_context_overflow, parse_overflow_amounts)` edge 추가.
- **`docs/ARCHITECTURE.md §5.4 flow 2`**: "`is_context_overflow(str(err))` 확인" 서술을 "경계가 `ContextOverflowError` 를 raise, 루프는 `classify_overflow(e)` 로 상태-게이트 판정"으로 갱신.
- **`CHANGELOG.md`**: `[8.6.1]` Fixed — overflow 오탐지로 인한 히스토리 파괴 수정(typed `ContextOverflowError` + 상태 게이트).
- **`docs/AUDIT-2026-08-09.md`**: L-1/L-25(loop 경로) 항목에 "✅ resolved (8.6.1)" 표기(선택).
- README: 사용자 대면 변화 없음 → 무변경.

---

## 6. 커밋 (CLAUDE.md 규칙 6 — 한 커밋)
`agent_cli/context/overflow.py` · `agent_cli/providers/http.py` · `agent_cli/loop/llm.py` · `tests/…` · `docs/ARCHITECTURE.md` · `CHANGELOG.md` · `agent_cli/__init__.py`(버전 8.6.1) 를 **하나의 커밋**으로. 명시적 staging(`git add <각 파일>`, `git add -A` 금지). main 직접 커밋은 문서 커밋과 달리 코드이므로 — **push 전 사용자 확인**. Co-Authored-By 푸터 포함.

---

## 7. 범위 결정 (승인 항목)
- **범위 A (추천, 기본)**: §3.1~3.3 + 테스트 + 문서. 파괴적 오탐지(L-1) 완전 제거. capabilities(L-25 잔여)는 follow-up.
- **범위 B (전체 L-25)**: A + §3.4 capabilities 2곳을 typed 경로로. 프로브가 `raise_for_status_with_body` 미경유라 추가 배선 필요 → 예산 초과 위험.
- 미결정 시 **A 로 진행**.

---

## 8. 핸드오프 상태 — ✅ 전부 완료
- [x] §3.1 overflow.py 예외+분류기 (`ContextOverflowError`, `classify_overflow`, `_OVERFLOW_STATUS`)
- [x] §3.2 http.py 경계 raise (400+overflow → `ContextOverflowError`)
- [x] §3.3 llm.py catch 교체 — **정정**: 비-overflow 는 `raise` 가 아니라 **기존 fall-through**(로그+`render_step("error")`+`return ToolResult(False)`) 를 그대로 보존. `classify_overflow(e) is None` 이면 그 경로로 감. `overflow_retries=0` 리셋·`_RETRY`·`render_status` 문구 보존됨.
- [x] §4 테스트 — `test_overflow.py`(ContextOverflowError·classify_overflow 게이트), `test_providers_retry.py`(경계 typed raise/generic 유지), `test_loop.py::TestContextOverflowRecovery`(typed/400-폴백 recovery + **오탐지 회귀** `test_bare_overflow_text_does_not_destroy_history`)
- [x] §5 문서 동기 (ARCHITECTURE §3.2·§5.4, CHANGELOG 8.6.1, __init__ 8.6.1)
- [x] 검증 — 전체 **3338 passed, 27 skipped**; ruff check/format pass; **뮤테이션**(상태 게이트 제거 시 오탐지 테스트 3건 실패 확인)
- [x] §6 단일 커밋 + wheel release

**주의/함정**:
- `llm.py` 성공 경로의 `self.overflow_retries = 0` 리셋을 지우지 말 것.
- `http.py` import 는 top-level 로(순환 없음 확인됨). lazy 불필요.
- provider 별 400 overflow 가 `raise_for_status_with_body` 를 경유하는지 실확인 어려우면 §3.3 의 `classify_overflow` 폴백이 안전망이므로 그대로 둘 것(경계 raise 가 안 걸려도 상태-게이트로 recovery 유지).
- 뮤테이션 테스트로 "옛 catch-all 로 되돌리면 오탐지 테스트가 실패"함을 반드시 확인(가드가 실제로 무는지 증명 — 프로젝트 규율).
