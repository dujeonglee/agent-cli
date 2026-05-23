# Context Compaction — Requirements

> Status: Draft
> Date: 2026-05-22
> Owner: claude (RFC)
> Companion: [DESIGN.md](DESIGN.md), [TEST_PLAN.md](TEST_PLAN.md)

## 0. 배경

현재 `ContextManager._evict` (manager.py:108-112)는 **단순 FIFO 드롭**:

```python
def _evict(self) -> None:
    while self._cache_tokens > self.max_context_tokens and len(self._cache) > 1:
        removed = self._cache.pop(0)
        self._cache_tokens -= _estimate_message_tokens(removed)
```

오래된 메시지를 그냥 버린다. 문제:
- 사용자/LLM이 초반에 쌓은 정보(설정, 도구 결과, 발견 사항) **영구 손실**
- 긴 세션에서 LLM은 자기 작업 도메인을 모름 — "처음 본 파일"처럼 행동
- evict된 후 사용자가 옛 내용 언급하면 LLM이 모르는 척

## 1. 목표

긴 세션에서도 LLM이 **작업 도메인 + 진행 history** 인식 유지하면서, context window 안에 들어가도록 자동 압축.

## 2. 기능 요구사항 (FR)

### FR-CC-1: 단계적 compaction 트리거

- 초기: 일반 FIFO 없이 — context window 토큰 budget 다 채울 때까지 누적
- Budget 초과 시점: **compaction 1회 실행** (단순 FIFO drop 대체)

### FR-CC-2: Compaction 동작

Trigger 시:
1. **오래된 절반 evict** — 토큰 기준 절반
2. **요약 생성** — evict된 메시지에 대해 LLM 호출, 자연어 요약 생성
3. **파일 액션 추출** — evict된 메시지를 스크립트로 스캔, touched 파일 리스트 추출
4. **새 context 레이아웃**: `[anchor: system + 첫 query] [요약] [파일 리스트] [남은 절반 dynamic]`

### FR-CC-3: 재귀적 compaction

Dynamic 절반이 다시 꽉 차면:
1. **현재 dynamic의 오래된 절반 evict**
2. **새 요약 생성**: "이전 요약 + 방금 evict된 절반" → 한 요약으로 통합
3. **파일 리스트 갱신**: (정책 TBD — DESIGN §3 참조)
4. **레이아웃 동일**: `[anchor] [새 요약] [새 파일 리스트] [남은 dynamic]`

### FR-CC-4: Anchor 보존

다음만 evict 대상 X (영구 보존):
- **System prompt**

**첫 user query는 evict 대상에 포함**. 이유:
- 세션이 길어지면 작업이 여러 단계로 진화. 첫 query는 의미 약해짐
- 현재 turn의 anchor는 "마지막 user query" — LLM이 답해야 하는 것
- 마지막 user query는 dynamic 영역의 가장 최근이라 evict 1순위 아님 → 자연 보존
- 첫 query의 원래 의도는 요약 안에 포함되어 보존됨 (LLM 요약이 "user started with X, then evolved to Y")

### FR-CC-5: 파일 액션 추출

evict된 메시지에서 다음 액션 추출:
- `write_file` / `edit_file` — `path` 필드
- `read_file` — `path` 필드
- `delegate` subagent의 액션 — parent에서 추적 가능한 범위

shell 명령(`mv`, `rm`, `cp`)의 파일 추출은 정규식 시도 (실패 시 skip — false positive 방지).

### FR-CC-6: Resume 호환

`agent-cli {chat,web} --resume <id>`로 세션 재개 시:
- 저장된 요약 + 파일 리스트 복원
- dynamic 메시지는 history.jsonl에서 토큰 budget까지 복원

### FR-CC-7: 사용자 가시화

Compaction 진행 중 사용자에게 표시:
- CLI: "Compacting context (1234 → 567 tokens)..."
- Web: SSE 이벤트로 progress 표시

## 3. 비기능 요구사항 (NFR)

### NFR-CC-1: LLM 호출 비용 통제

- Compaction 당 LLM 호출 1회 (요약 생성)
- 요약 모델: 메인 모델과 동일 (별도 small model 안 씀)
- 요약 길이 cap: 2000 토큰 (DESIGN §5 참조)

### NFR-CC-2: 실패 안전성

- LLM 요약 실패 → fallback: 단순 FIFO drop (현재 동작과 동일)
- 파일 액션 추출 실패 → skip (빈 리스트)
- compaction 자체 실패 → 사용자에게 경고 + FIFO fallback

### NFR-CC-3: 토큰 budget 안전 마진

- Trigger는 100%가 아닌 **90%** — 다음 turn 들어갈 자리 보장
- Compaction 후 결과 cache는 50% 이하로 줄도록 절반 evict

### NFR-CC-4: 회귀 금지

- 기존 turn flow / wire format / tool dispatch에 영향 0
- ContextManager의 add / get_messages / get_raw_messages 인터페이스 보존
- CLI / web 양쪽 동일 동작

### NFR-CC-5: 사용자 통제 (비활성 옵션)

- `--no-compaction` CLI flag (`run` / `chat` / `web` 세 명령 공통)
- `AGENT_CLI_COMPACTION=off` 환경변수 (동일 효과, deployment 친화적)
- off 시 동작: 기존 `_evict_fifo` 만 호출 (compaction 발동 0)
- 측정 baseline 확보 + 사용자 디버그 / A-B 비교 가능
- README 에 옵션 설명 + "언제 off가 유용한가" 가이드

### NFR-CC-6: 측정 인프라

- `TurnRecorder.record_compaction(...)` — 각 compaction event 를
  `turns.jsonl` 에 기록:
  - `event: "compaction"`
  - `tokens_before`, `tokens_after`, `evicted_count`
  - `fallback_used: bool` — belt-and-braces FIFO 가 추가 발동했는지
  - `failure_signal: str | null` — LLM 실패 시 ``"summary_failed"`` 등
  - `duration_ms` — LLM 호출 + 후처리 총 시간
- v1 의 RFC §8 위험 사항(LLM 비용, 빈도, 재귀 drift) 측정 → 향후
  threshold / cap / prompt 조정 근거

## 4. 범위 밖

- 의미 기반(semantic similarity) evict (FIFO 절반 정책으로 충분)
- 별도 summary model (메인 모델 사용)
- 사용자 수동 compaction 명령 (`/compact`는 이미 chat REPL에 있으나 본 PR 미변경)
- 다중 요약 레벨 (multi-level hierarchy) — 단일 레벨 + 재귀로 충분
- offline session annealing (별도 작업 — 메모리 idea만 있음)

## 5. 결정 사항 (사용자 confirmed 2026-05-22)

1. **파일 리스트 정책: 누적** — 모든 evict 단계의 파일 path 합집합. compaction 마다 새 path 추가, 기존 유지. 컨텍스트 부담 작음 (~50 bytes/path).
2. **Compaction 트리거: 90% threshold** — `cache_tokens > max_context_tokens * 0.9` 시점. 100% 도달 전에 발동해 다음 turn 들어갈 자리 보장.
3. **"절반" 정의: 토큰 기준** — anchor(system prompt only) 제외한 남은 cache의 토큰 절반. 메시지 count는 토큰 분포 불균등 시 부적합. **마지막 user query는 dynamic 끝에 있어 evict 대상 아님** (FR-CC-4 참조).
4. **요약 길이 cap: 2000 토큰** — `max_tokens=2000` LLM 요청. 초과 시 truncate. 측정 후 조정 가능.
5. **요약 실패 fallback: FIFO drop + 자연 재시도** — 한 번 LLM 요약 실패 시 단순 FIFO drop으로 fallback, `render_status` 경고. 다음 turn에 다시 90% 초과하면 자연스럽게 compaction 재시도 (별도 retry counter 없음).
6. **파일 액션 추출 범위: tool path 필드만** — `write_file`/`edit_file`/`read_file`/`read_symbols`의 `path` 필드, `delegate` subagent 액션 (parent에서 추적). shell 명령은 skip (false positive 회피). pre-hook redirect 아이디어는 별도 PR.
7. **시스템 프롬프트 섹션 위치: anchor 직후** — `[system prompt][summary section][file list section][anchor user query][dynamic messages]`. Wire format 무관 위치.
8. **Resume 저장 형식: `session_dir/compaction.json`** — 별도 JSON 파일. schema는 DESIGN.md §4. history.jsonl 원본 보존.
9. **사용자 가시화: progress text 한 줄** — `render_status("info", f"Compacting context ({old_tokens:,} → {new_tokens:,} tokens, summarizing {N} messages)…")`. CLI는 console.print, web은 SSE status 이벤트로 자동 전달.

## 6. 성공 기준

- 실측: ~50K 토큰 세션에서 LLM이 초기 작업 내용 (예: "처음에 만든 X 파일") 인식 유지
- 회귀: 기존 1475개 테스트 + 신규 통합 테스트 통과
- 사용자 체감: 긴 세션도 자연스럽게 이어짐 (compaction 트리거 시점에 짧은 progress 표시 외 끊김 없음)
