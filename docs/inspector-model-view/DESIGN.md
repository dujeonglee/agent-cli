# 컨텍스트 인스펙터 — 챗이 곧 모델 시점 (DESIGN)

> 상태: **P1 구현 (v10.6.0) — P2·P3 대기** (2026-10-02). 시안: https://claude.ai/artifact/MZHYxZyskHEjnULGnYwuFG (v4).
> 결정(사용자): ① 챗 창은 **항상** 그 에이전트의 모델 시점 ② 컨텍스트에서 빠진 카드는 **흐린 채로 둔다**(숨기지 않음)
> ③ 인라인 카드의 상속분은 **기본 접힘** ④ 드로어의 대화 요약 줄·섹션 필터 삭제 ⑤ 드로어 제목은 스코프 이름만.
> 선행: [docs/inspector-redesign-plan.md](../inspector-redesign-plan.md) (v8.11.0 — 스코프별 드로어·카드 🔍), [docs/chat-ui/DESIGN.md](../chat-ui/DESIGN.md) (채널·중첩 카드).

## 1. 동기

지금 🔍 드로어는 "시스템 프롬프트 + 함수 스키마 + 대화 메시지 목록 + 꼬리" 를 한 목록으로 보여 준다. 대화 목록은 왼쪽 챗과
같은 내용의 **두 번째 사본**이라 길고, 정작 사용자가 알고 싶은 "모델이 지금 무엇을 보고 무엇을 못 보나"(압축으로
빠진 구간, 그 자리를 대신하는 요약, 턴마다 바뀌는 꼬리)는 둘 중 어디에도 또렷하지 않다.

원칙 하나로 정리한다: **챗 창이 그 에이전트의 모델 시점이다. 항상.** 드로어는 대화 **밖**에서 모델이 받는 것만 담는다.

## 2. 화면 규칙

### 2.1 챗 창 (main · 상주 에이전트 채널 · 인라인 카드 안 — 셋 다 같은 규칙)

| 상태 | 표시 |
|---|---|
| 컨텍스트에 있는 카드 | 지금처럼 |
| 압축으로 빠진 카드 | **흐림(opacity .38) + "컨텍스트 밖" 꼬리표**. 접지 않는다 — 무엇이 빠졌는지 보여야 요약을 믿을 수 있다 |
| 압축 요약 | 빠진 구간 **바로 뒤**에 점선 카드 `⊙ 압축 요약 · 턴 a–b · X → Y tok` + 요약 본문(+파일 목록). 모델은 흐린 카드 대신 이것을 본다 |
| 형식 넛지 | 카드 없음(라이브와 같다 — 재시도 틱만). v10.5.0 구조화 레코드는 드로어 예산 줄의 "접힌 넛지 n" 로만 센다 |
| 인라인 에이전트 카드 안 | 머리말 한 줄 `fork · 부모(main) 턴 9 시점 히스토리 12 메시지 상속 · 자기 턴 3` + **상속 한 줄**(클릭 → main 의 그 지점으로 점프, 복제하지 않음) + 자기 대화. 종료된 런은 `종료 — 끝날 때 스냅샷` |

### 2.2 드로어 (🔍) — 대화 밖에서 모델이 받는 것

모달이 아니라 **도킹**(열려 있는 동안 챗 폭이 줄고 챗은 그대로 보인다). 턴마다 자동 갱신(`● 턴 14 에 갱신`).

- 제목: 스코프 이름만 — `main` / `🦊 player1 agt-…` / `🦀 reviewer: <task>`. 종료된 인라인 스코프는 위에 출처 줄
  `main 턴 9 에서 돈 인라인 에이전트 · 종료  ↑ 카드로 · main 으로 돌아가기`.
- 예산 한 줄: 막대(시스템·함수 스키마·대화·꼬리) + 숫자 + `⊙ 압축 n회 · 턴 t`.
- 그룹 "매 턴 바뀜": 꼬리 섹션(Session State · Live Agents / Inbox / Standing Rules …) — **직전 턴과의 diff**(+/− 줄, `vs 턴 13` 태그, 변화 없으면 `변화 없음`).
- 그룹 "고정": 시스템 프롬프트(섹션별 접힘·복사), Function schemas(native_fc 의 `tools[]`, 상주·인라인은 allowed-tools 만큼), 📐 디코딩 문법.
- 없는 것: 대화 메시지 목록, 섹션 필터, 대화 요약 줄.

스코프 선택은 지금과 같다: 현재 채널(main / 상주 에이전트 칩) 또는 인라인 카드 헤더의 🔍(그 스코프로 고정, 헤더 🔍 가 `on`).

## 3. 서버 계약

### 3.1 `ctx_view` 이벤트 (신규, persistent, 스코프별)

컨텍스트 캐시가 바뀔 때마다(압축 완료·fold·resume 복원 직후·턴 종료) 그 스코프로 보낸다.

```json
{"task_id": "", "gone": {"hidx": 10} | null,
 "summary": {"text": "…", "files": ["src/app.py", …], "turns": [1, 9], "before_tokens": 18200, "after_tokens": 3600} | null,
 "compactions": 1, "folded_nudges": 1}
```

- 압축은 항상 **앞쪽 접두사**를 비운다(`ContextManager.compact_now`: anchor + evict 슬라이스 → retained). 그러므로
  "컨텍스트 밖" 은 경계 하나로 표현된다: `gone.hidx` = 캐시에 남은 첫 동적 레코드의 history 서수. 카드는 자기 레코드의
  서수(`hidx`)가 그보다 작으면 빠진 것이다.
- **턴 번호는 경계가 못 된다** (P1 실측): 턴은 런마다 1 부터 다시 시작해 두 번째 런의 final 도 turn 1 이다. 그래서 카드
  이벤트(`assistant_turn`·`observation`·`user_message`·`agent_wake`)가 `hidx` 를 싣는다 — 레코드보다 먼저 그리는
  카드(행동·최종답·사용자 에코)는 `ctx.next_ordinal`, 뒤에 그리는 카드(관찰)는 `ctx.last_ordinal`. `render_step(hidx=)`
  → 렌더러 `note_record(hidx)`(스레드 로컬) → `_emit` 이 다음 카드 이벤트 하나에 붙인다. resume 재생은 캐시 서수
  (`cache_ordinals()`)·서브 스코프는 history 줄 번호.
- `summary` 는 `ctx.summary`(+`_file_list`) 그대로. 프런트는 마지막 흐린 카드 바로 뒤에 요약 카드를 세운다(한 스코프에 하나, 갱신은 교체).
- fold 된 넛지는 카드가 없으므로 경계와 무관 — 개수만 싣는다.
- 소스: `ContextManager` 가 압축/fold/복원 후 `render.note_context_view(view)` 를 부르고, 웹 렌더러가 스코프 `task_id` 를 붙여 emit.
  CLI 렌더러는 no-op.

### 3.2 `scope_start` 확장

인라인(run/skill) 스코프에 `context_mode: "none"|"fork"|"resume"` 와 `inherited: {count, parent_turn}` 를 싣는다
(`create_subagent_ctx` 가 fork 시점의 부모 캐시 길이와 부모 턴을 안다). 프런트는 카드 머리말과 상속 한 줄을 만든다.

### 3.3 `/api/debug/prompt` 축소

반환에서 `kind=dynamic` 섹션을 뺀다(대화는 챗이 보여 준다). 추가: `budget {system, tools, convo, tail, total, window}`,
`tail_prev`(직전 턴의 꼬리 섹션 — 렌더러가 스코프별로 마지막 두 턴의 꼬리를 보관) → 프런트가 diff. `title`/`origin`
(인라인 스코프의 부모·턴·종료 여부).

### 3.4 resume / 재접속

- 재접속: `ctx_view` 는 sticky 라 스냅샷에 실린다. 요약 카드도 함께.
- resume: `_restore_cache` 뒤 같은 `ctx_view` 를 한 번 내고, `replay_from_history` 가 그린 카드에 같은 규칙이 적용된다.
  `compaction.json` 의 `dynamic_start_index` 가 경계의 진실.

## 4. 단계 (PR)

| PR | 내용 | 테스트 |
|---|---|---|
| P1 | `ctx_view` 이벤트 + 챗의 흐림/꼬리표 + 압축 요약 카드 (main·상주·인라인 공통) | 압축 후 이벤트 모양(경계·요약), 재접속 재생, resume 복원, 프런트 정적 배선 |
| P2 | 드로어: dynamic 제거, 예산 줄, 꼬리 diff, 제목·출처 줄, 필터·요약 줄 제거, 도킹 | debug_prompt 응답 모양(dynamic 없음·budget·tail_prev), diff 렌더 |
| P3 | `scope_start` 확장 + 인라인 카드 머리말·상속 점프 | create_subagent_ctx → 이벤트 필드, 카드 배선 |
| 문서 | README 🔍 단락, ARCHITECTURE(inspector.py·render/web.py), 이 문서 상태 갱신 | — |

버전: P1 이 MINOR(v10.6.0), P2·P3 는 그 뒤 MINOR 둘 또는 하나로 묶음.

## 5. 열어 둔 것

- 라우팅된 명령(`/compact`, `@agent …`)의 사용자 에코 카드는 레코드가 없는데도 `ctx.next_ordinal` 을 받는다 — 그
  서수는 다음 실제 레코드의 것이라, 그 레코드가 나중에 빠지면 이 카드도 같이 흐려진다. 명령은 애초에 컨텍스트에
  없으니 틀린 그림은 아니고, 정확히 하려면 에코 뒤에 "레코드가 안 생겼다" 를 알아야 한다(미결).

- 흐린 카드의 펼치기: 지금처럼 펼쳐진다(내용은 history 에 있다). 흐림은 "모델이 못 본다" 는 표시지 접근 제한이 아니다.
- 토큰 막대의 "대화" 값은 `ctx.get_messages()` 의 추정치(`_scaled_tokens`) — 서버 실측(`reconcile_actual_tokens`)이 있으면 그 계수를 쓴다.
