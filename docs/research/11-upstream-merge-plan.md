# Coagora → agent-cli 본류 병합 계획 (Upstream Merge Plan)

> ## 종결 (2026-08-07)
>
> **이 문서는 완료·동결됐다.** 역병합은 끝났고, v0.8 프로그램의 1·2단계
> (seq 기반 증분 재생, 관전자 읽기 전용 모드)로 기능 이식까지 마쳐 시스템은
> **하나**로 단일화됐다. 실험 분기였던 외부 리포는 **동결**한다 — 추가 개발도,
> 논문·문서에서의 참조도 없다. 논문 v0.8 은 단일 시스템 서사로 재구성됐고
> 모든 수치를 이 리포의 `bench/multiuser/out/` 에서 재도출했다.
>
> 아래 내용은 그 이행 과정의 기록으로 보존한다. 이 문서에 남은 외부 프로젝트
> 명칭·구조 서술은 **히스토리**이며, 공개 아티팩트인 `bench/multiuser/` 와
> 논문 두 파일에는 그 흔적이 남아 있지 않다(5단계에서 중립 표현으로 정리).
>
> ---
>
> **진행 상태 (2026-08-06 갱신): M1–M5 + M2 전부 본류 커밋 완료.**
> `e1d5e011`(M1) → `91da8d3f`(fsio append 직렬화) → `a453a03c`(ctx 원자 커밋+낙관적 압축) → `c61552f7`(M3/A1 병렬 턴 코어) → `64d86d7e`(M4 효과 락) → `cee1381c`(M5 공정 큐+턴 인터럽트) → `2c725a85`(M2 계측+거부 게이트).
> 아래 §5 의 "M3 2–3주 임계 경로" 추정은 이행 전 계획이며 실제로는 완료됨 — **P1–P5 전부 본류에서 실행 가능한 상태**다. 남은 것: M0 문서화(REQUIREMENTS.md), 벤치 하네스(`bench/multiuser/`), M6(선택). §2 M3-(ii) 의 "동시 턴 중 압축 금지" 게이트는 실제 구현에서 **낙관적 3단계 압축**(무락 요약 + 세대 재검증 커밋)으로 대체·개선되었다 (`a453a03c`, `agent_cli/context/manager.py:_compact`).

> 작성일: 2026-08-05. 전제: **agent-cli = 본류(mainline, Python)**, **Coagora(구 Aidit-Code) = 포크(TypeScript/Node)**.
> Coagora는 본류에서 갈라져 나와 "다중 사용자 병렬 턴" 기능군을 먼저 개발·실측 검증한 실험 분기다. 본 문서는 그 기능군을 본류로 **역병합(merge back)** 하는 절차를 정의한다.
> 격차 사실관계는 [`10-agent-cli-gap-analysis.md`](10-agent-cli-gap-analysis.md) §1–2, 실험 요구는 [`08-usecase-performance-experiment-plan.md`](08-usecase-performance-experiment-plan.md) 참조.

## 0. 병합의 성격 — 코드 이식이 아니라 "계약 이식"

두 코드베이스는 언어(TS ↔ Python)와 런타임 구조(Fastify 서버+child worker ↔ 단일 프로세스 CLI+스레드)가 달라 **텍스트 수준 머지가 불가능**하다. 병합 단위는 다음 세 가지다.

1. **동시성 계약과 불변식** — 병렬 추론+직렬 부수효과, 스냅샷 읽기/원자 커밋, 툴콜 짝 정합, per-user 1활성턴, 엄격 FIFO 무추월. 포크에서 문서·주석·테스트로 검증된 규칙을 본류의 관용구로 재구현한다.
2. **검증 자산** — 포크의 계약 테스트(arMux, xcCap, sandboxLock, piWorkerConcurrency 등 8종)와 벤치 하네스(e2-hol, e1-ablation)를 **인수 기준(acceptance criteria)** 으로 삼는다. `mockLlm.mjs`는 OpenAI 호환 서버라 본류가 언어 무관하게 그대로 사용한다.
3. **설계 근거(design rationale)** — 포크 코드 주석에 보존된 결정 이유(FILE_DELETE 배타의 ENOENT 레이스, 디스패치 시점 turnId 부여, 409 게이트의 stale-click 방어 등)를 본류 docs/ 설계 문서로 옮긴다 — 본류의 DESIGN.md 문화(03 §6)와 합치.

**본류 우선 원칙**: 충돌 시 본류의 기존 계약이 이긴다. 특히 (a) 본류의 선착순 409 응답 게이트·소유자 큐 취소는 유지하고 포크 메커니즘을 그 위에 얹는다, (b) 상주 서브에이전트·MailWaker·공용 InputQueue 계약(03 §3.3)은 병렬 턴 도입 후에도 깨지지 않아야 한다, (c) KV 캐시 안정성 규율(도구 순서 고정, 멤버십 변화에만 프롬프트 재조립)을 위반하지 않는다.

## 1. 컴포넌트 대응표 (포크 원본 → 본류 병합 지점)

| 포크(Coagora) 원본 | 병합할 것 | 본류(agent-cli) 대상 | 신규/수정 |
|---|---|---|---|
| `pi.ts` RuntimeHandle(activeTurns Map, turnSeq, pumpConcurrent) | 턴 레지스트리·디스패치 계약 | `agent_cli/loop/turns.py` (신규) + `loop/core.py` 수정 | 신규+수정 |
| `piWorker.mjs` convo 스냅샷/`commitToConvo` 짝 정합 | 스냅샷 읽기·원자 커밋 불변식 | `agent_cli/context/manager.py`·`records.py` 확장 | 수정 |
| `sandboxLock.ts` 호환성 행렬·엄격 FIFO·`LOCK_SCOPE` | 부수효과 계층 락 | `agent_cli/tools/effect_lock.py` (신규) + `loop/tool_bridge.py` 훅 | 신규 |
| `turn.ts` `lockScopeFor()` 인텐트 분류 | 도구 호출 → 효과 인텐트 매핑 | 각 Tool에 `effect_intent()` 메서드 (기존 `touched_paths()` seam 옆) | 수정 |
| `Message.replyToId` | 응답↔질문 1:1 귀속 | `history.jsonl` 레코드에 `reply_to` additive 키 (`context/records.py`) | 수정 |
| XC-CAP(`hasActiveUser`, 공정 큐 splice) | cap + per-user 1활성턴 + 라운드로빈 | `agent_cli/input_queue.py` 확장 (eligible-scan 디큐) | 수정 |
| `interrupt(turnId)` + stale done 이중 억제 | 턴 단위 인터럽트 | `loop/state.py` per-turn stop flag + `web/server.py` `/api/turn/{id}/interrupt` | 신규+수정 |
| activeTurns 카운트 세션 상태 | RUNNING/IDLE 판정 | `status.json` 스키마 additive 확장 | 수정 |
| `mockLlm.mjs`, `e2-hol.mjs`, `e1-ablation.mjs` | 벤치 하네스 | `bench/multiuser/` (신규 디렉토리, mockLlm은 원본 재사용) | 신규 |
| `pathGuard.realResolve`, ENV allowlist | 격리 강화(선택) | `tools/_confine.py`·shell 도구 env 처리 | 수정 |

병합하지 **않는** 것: 게시판/피드/북마크(제품 계층 — 포크 고유), Prisma/SQLite 메시지 DB(본류는 history.jsonl 유지), JWT 계정(본류는 토큰+닉네임 유지, 관전 role만 선택 병합), Fastify/SSE 서버(본류 FastAPI/SSE 유지).

## 2. 병합 단계 M0–M6

각 단계는 본류 `CLAUDE.md` 커밋 규약을 따른다: **유닛 테스트 + README.md + docs/ARCHITECTURE.md 갱신 + ruff 통과를 하나의 커밋으로**. 각 단계는 독립적으로 릴리스 가능해야 하며(additive, 기본 off), 포크의 opt-in 게이트 철학(메타 손상 시 직렬 폴백)을 그대로 가져온다.

### M0 — 인수 기준 확립 (선행, 코드 변경 없음)
- 포크의 계약 테스트 8종을 본류 관점의 **계약 명세서**(`docs/multiuser-turns/REQUIREMENTS.md`)로 번역. 각 항목에 "포크에서의 검증 근거(파일:라인)" 명기.
- `mockLlm.mjs`를 본류 config(base_url)로 연결해 스모크 확인 — P6 즉시 해금.
- 완료 기준: REQUIREMENTS 문서 + mockLlm 연결 테스트 1건.

### M1 — 귀속·인텐트 기반 정비 (additive, 위험 낮음)
- `reply_to` 키를 history 레코드에 추가(디큐된 사용자 메시지 id를 응답 레코드에 기록). 직렬 모드에서는 항상 직전 사용자 메시지 — 기존 동작 불변.
- 각 Tool에 `effect_intent()` 추가: `FILE_WRITE(path)` / `FILE_READ(path)` / `FILE_DELETE(path)` / `SHELL` / `PACKAGE` / `UNKNOWN`. 기존 `touched_paths()`와 동일한 소유권 원칙(도구가 자기 shape를 안다).
- 해금: A6 해소. M4의 전제.

### M2 — 거부 게이트 + 측정 훅 (P1 부분 해금)
- opt-in 플래그 `--concurrency-contract {serial|reject}`: reject 모드에서 워커 busy 중 입력에 409 반환(기존 ask/confirm 409 게이트 패턴 재사용).
- 턴 타임스탬프(enqueue/dispatch/first-token/complete)를 `turns.jsonl`에 additive 기록 — TTFT 측정 기반.
- 해금: **P1의 직렬·거부 2계약 실측을 본류에서 실행 가능.**

### M3 — 병렬 턴 코어 (임계 경로, 최대 공수)
- `loop/turns.py`: 턴 레지스트리(turnId 단조 발급 — 디스패치 시점 부여 규칙 준수), 턴별 워커 스레드 또는 스레드 풀, per-turn 렌더 스트림 태깅.
- 컨텍스트: 턴 시작 시 `ctx.get_messages()` 스냅샷, 완료 시 assistant+tool 블록을 **단일 락 하에 원자 append** — 짝 정합 불변식을 유닛 테스트로 고정(포크 `piWorkerConcurrency.test.ts` 상당).
- **본류 고유 위험 3건을 명시 처리**: (i) 상주 서브에이전트 회신 배달(`_deliver_agent_mail`)은 "턴 경계"가 복수가 되므로 배달 시점 재정의, (ii) compaction(`ensure_within`)은 동시 턴 중 실행 금지 — 활성 턴 0일 때만 트리거하도록 게이트(포크에는 compaction이 없어 선례 없음, 본류가 최초로 푸는 문제), (iii) 세션 파일(history.jsonl 등) append 직렬화.
- opt-in: `--concurrency-contract parallel` (기본 serial 유지).
- 해금: P1 병렬 셀. 완료 기준: 계약 테스트(M0 명세) 통과 + 기존 전체 테스트 무회귀.

### M4 — 부수효과 계층 락
- `tools/effect_lock.py`: 호환성 행렬 + 샌드박스(=워크스페이스)별 엄격 FIFO + 경로 정규화(win32 소문자화 포함 — 본류가 Windows 지원이므로 포크 규칙 그대로) + `--lock-scope {workspace|conflict}` 스위치.
- `tool_bridge.py`의 invoke 경로에서 M1 인텐트로 락 획득 후 실행.
- 해금: **P2, P3.** 완료 기준: 포크 `sandboxLock`·`sandboxLockScope` 테스트 상당의 Python 테스트 + e1-ablation 재현(74%→0% 상당 수치).

### M5 — 공정성·제어
- `input_queue.py`: cap(기본 4), per-user(conn_id/닉네임 단위) 1활성턴, eligible-scan 디큐(첫 적격 항목 splice — 포크 `pumpConcurrent` 규칙).
- 턴 단위 인터럽트: per-turn stop flag + `/api/turn/{id}/interrupt` + stale 완료 억제. 기존 `/api/stop`(세션 전역)은 유지.
- activeTurns 카운트를 `status.json`·SSE 이벤트에 additive 노출.
- 해금: **P4, P5** — 이 시점에 P1–P8 전체가 본류에서 실행 가능.

### M6 — 선택 병합 (실험 비필수)
- 관전 전용 role(뷰어 토큰), ENV allowlist(deny-by-default) + 키 유출 회귀 테스트, `_confine` symlink-realpath 강화, seq 영속 재생(이벤트 버퍼 → history 기반 무제한 replay).
- 판단 기준: 논문 보안 주장 포함 여부·공개 배포 계획에 따라.

### 단계-실험 해금 맵

| 단계 | 해금 실험 | 의존 |
|---|---|---|
| M0 | P6, P7, P8 (기존 기능으로 즉시) | — |
| M2 | P1(직렬·거부) | M1 권장 |
| M3 | P1(병렬) | M1 |
| M4 | P2, P3 | M1, M3 |
| M5 | P4, P5 | M3 |

## 3. 검증 게이트 (전 단계 공통)

1. `pytest tests/` 전체 무회귀 + 신규 계약 테스트 (M0 명세 기준).
2. `ruff check` / `ruff format --check` 통과.
3. **포크 실측과의 교차 검증**: M3 완료 시 e2-hol 상당 시나리오를 본류에서 실행해 기울기 0.000 재현, M4 완료 시 ablation 74%→0% 상당 재현. 수치가 재현되면 논문 §6에 "**동일 계약의 이종 구현 2건(TS 서버형/Python CLI형)에서 결과 재현**"을 추가 — 일반화 주장 확보(포크→본류 병합의 학술적 보너스).
4. 기존 직렬 경로 바이트 수준 보존: `--concurrency-contract` 미지정 시 현재 동작과 동일(포크의 "레거시 경로 불가침" 원칙 준수).
5. 상주 서브에이전트 회귀: orchestrate 스킬 E2E + MailWaker 대기 시나리오.

## 4. 리스크 대장

| 리스크 | 완화 |
|---|---|
| M3가 `loop/core.py`·`dispatch.py`(합 2,163 LOC)의 단일 스레드 가정을 광범위하게 건드림 | 포크의 "이중 경로 완전 분리" 전략 복제 — 병렬 자료구조를 별도 모듈(turns.py)에 두고 직렬 경로는 무수정 유지 |
| compaction × 동시 턴 상호작용(선례 없음) | M3에서 활성 턴 >0 시 compaction 금지로 시작; 완화 해제는 별도 설계 문서로 |
| 상주 에이전트 스레드와 턴 스레드의 락 경합 | 효과 락 획득 순서 단일화(effect_lock → 세션 파일 락), 데드락 테스트 추가 |
| Python GIL로 인한 "병렬" 의미 | 추론은 I/O 대기(HTTP 스트리밍)라 GIL 영향 미미 — 스레드로 충분. 벤치로 확인 |
| 포크·본류 기능 발산 지속 | 병합 완료 후 Coagora는 UI/커뮤니티 계층 전용 분기로 역할 재정의(계약 코드는 본류가 단일 출처) |

## 5. 일정 감각 (근사)

M0 2–3일 → M1 2–3일 → M2 2일 → M3 2–3주 → M4 1주 → M5 1주 → (M6 별도). 논문 데드라인이 빠듯하면 M0–M2까지만 본류에서 확보하고 P1 병렬 이후 실험은 포크 실측을 인용하는 절충이 가능하나, §3-3의 이종 구현 재현 주장은 포기된다.
