# Context & Artifact 재설계

> Status: Draft
> Date: 2026-04-05

## 1. 문제 정의

현재 ContextManager, Scratchpad, ArtifactStore 세 시스템이 같은 디렉토리를 공유하고
step 카운터 하나로 세 군데를 동시에 관리한다. 이로 인해:

- delegate fork 시 _step_count=0 초기화 → 부모와 step 번호 충돌
- 병렬 delegate 시 step_0001.md 동시 생성 → 파일 덮어쓰기
- 매 턴 raw LLM 응답을 artifact로 저장 → 쓸모없는 덤프 누적
- artifact inject 비활성화 상태 → 저장만 하고 아무도 안 읽는 죽은 코드
- subdirectory 라우팅으로 우회했지만 scratchpad progress는 여전히 충돌
- scratchpad을 매 턴 context에 강제 inject → 토큰 낭비 + messages 오염

## 2. 설계 원칙

### 2.1 기록의 목적

> FIFO로 밀려난 뒤에도 필요할 때 되살릴 수 있게 하는 것.

모든 기록은 LLM을 위한 것이다. 사람이 읽어야 할 기록은 없다.

### 2.2 두 종류의 기록

| | history.jsonl | artifact |
|---|---|---|
| **무엇을** | 대화 전체 (thought, action, 결과 전문) | 의미 있는 산출물 |
| **왜** | FIFO로 밀려난 과거 맥락 복구 | 상세 결과를 독립 파일로 참조 |
| **언제 쓰나** | 매 턴 자동 append | delegate/skill 완료, 계획 수립 등 |
| **언제 읽나** | LLM이 과거 맥락 필요 시 read_file | LLM이 상세 결과 필요 시 read_file |

### 2.3 핵심 원칙

- **history.jsonl은 있는 그대로 기록.** 도구 결과 전문 포함. 요약이나 필터링 없음.
- **artifact는 선택적으로 저장.** 재현 비용이 높은 산출물만 (delegate/skill 결과, 분석, 계획).
- **context에 강제 inject하지 않음.** LLM이 필요할 때 pull.

## 3. 아키텍처

### 3.1 두 레이어

```
Messages (FIFO)     ← 최근 N개 user/assistant 메시지만 유지 (기본 N=100)
Artifact Store      ← 영속 저장소 — context 밖, LLM이 read_file로 pull
```

- **LLM 기반 압축 제거.** Messages는 단순 FIFO로 관리.
- **Scratchpad 제거.** 별도 요약 파일 없음.
- **history.jsonl에 전체 대화를 JSON Lines로 기록.** FIFO는 이 파일의 마지막 N개.
- System prompt에 Context Recovery Guide를 포함하여 LLM이 필요 시 history.jsonl을 pull.

### 3.2 세션 파일 구조

```
.agent-cli/sessions/{session_id}/
├── history.jsonl                                      ← main 대화 기록
├── main_plan_e8d4_20260405T143112890.md               ← main artifact (flat)
│
├── skill_summarize_d4e1_20260405T143200100/            ← skill subdir
│   ├── history.jsonl                                   ← skill 내부 대화
│   ├── result.md                                       ← skill 최종 결과
│   └── delegate_explorer_a1b2_20260405T143210200/      ← skill이 호출한 delegate
│       ├── history.jsonl
│       └── result.md
│
└── delegate_coder_f1a9_20260405T143230456/             ← delegate subdir
    ├── history.jsonl                                   ← delegate 내부 대화
    ├── result.md                                       ← delegate 최종 결과
    └── skill_test_c3d4_20260405T143300300/             ← delegate가 호출한 skill
        ├── history.jsonl
        └── result.md
```

규칙:
- **main**: session root에 history.jsonl + flat artifact
- **delegate/skill 모두**: subdir에 history.jsonl + result.md. 구조 동일
- **재귀적 중첩**: skill/delegate가 하위 skill/delegate를 호출하면 자기 디렉토리 안에 subdir 생성
- **parent의 history.jsonl에는 호출과 최종 결과만 기록** (한 턴). 내부 과정은 subdir의 history.jsonl
- depth/agent_stack/skill_stack이 무한 중첩 방지 → 디렉토리 깊이도 자연스럽게 제한
- fork 시 복사된 history에 parent artifact 경로가 이미 있으므로 참조 문제 없음

### 3.3 명명 규칙

```
main artifact:    main_{name}_{hash}_{timestamp_ms}.md       (파일, session root)
delegate:         delegate_{name}_{hash}_{timestamp_ms}/     (디렉토리)
                    ├── history.jsonl
                    └── result.md
skill:            skill_{name}_{hash}_{timestamp_ms}/        (디렉토리)
                    ├── history.jsonl
                    └── result.md

name:         action/agent/skill 이름 (e.g., plan, explorer, summarize)
hash:         4~6자리 랜덤 hex (충돌 방지)
timestamp_ms: ISO 형식 + ms (e.g., 20260405T143022123)
```

- main artifact만 flat 파일. delegate/skill은 동일한 디렉토리 구조
- 시간순 정렬 = 작업 순서
- hash로 동일 시점 충돌 방지
- step 카운터 불필요 → _step_count 제거
- 하위 호출은 자기 디렉토리 안에 재귀적으로 subdir 생성

## 4. Context Window 구성

### 4.1 전체 구조

```
┌─ system ──────────────────────────────────────────────────┐
│                                                           │
│  [Primacy — 강한 주의]                                     │
│   Role — 아래 중 하나:                                     │
│     main: 기본 Role                                       │
│     delegate: Agent Role                                  │
│     skill: parent의 Role 상속                              │
│   Task Guidelines                                        │
│   Format Rules (ReAct)                                    │
│                                                           │
│  [Middle — 참조 영역]                                      │
│   Available Tools — 실행 주체에 따라 다름:                   │
│     main: 전체 도구                                        │
│     delegate: agent 정의의 allowed-tools                   │
│     skill: skill allowed-tools ∩ parent allowed-tools     │
│   Available Skills — skill_stack으로 재귀 방지              │
│   Available Agents — depth < max_depth                    │
│                      + agent_stack으로 재귀 방지            │
│                                                           │
│  [Recency — 강한 주의]                                     │
│   DIRECTIVE.md                                            │
│   Environment (OS, shell, cwd)                            │
│   Context Recovery Guide                                  │
│                                                           │
└───────────────────────────────────────────────────────────┘

┌─ messages (FIFO, history.jsonl의 마지막 N개를 자연어 변환) ──┐
│                                                           │
│   assistant: {thought}. → {action}({핵심 인자})            │
│   user: [{tool}] {결과 전문}                               │
│     → {artifact/delegate 경로 포함}                        │
│   assistant: {thought}. → {action}({핵심 인자})            │
│   user: [{tool}] {결과 전문}                               │
│   ... (최근 N개 메시지)                                    │
│                                                           │
│   * history.jsonl의 artifact 경로가 자연어 변환 시 포함     │
│     LLM은 이 경로로 read_file하여 상세 내용 조회 가능       │
│                                                           │
└───────────────────────────────────────────────────────────┘
```

### 4.2 FIFO 캐시 최적화

매 턴 history.jsonl을 다시 파싱하지 않는다.
메모리에 최근 N개 메시지를 캐시로 유지:

```
[턴 시작]
  cache = 이전 턴의 cache (N개)

[LLM 호출]
  messages = cache를 자연어 변환하여 사용

[턴 종료]
  1. 새 assistant/user 메시지를 cache에 append (JSON 원본)
  2. cache 크기 > N 이면 앞에서 제거
  3. history.jsonl에 새 메시지만 append (기존 내용 재작성 없음)

[세션 재개 시에만]
  history.jsonl에서 마지막 N개를 파싱하여 cache 초기화
```

- 정상 실행: history.jsonl은 write-only (append), read 안 함
- 세션 재개: history.jsonl에서 cache 복원 (유일한 read 시점)
- history.jsonl이 커져도 성능 영향 없음

### 4.3 Context Recovery Guide (system prompt 내)

```
## Context Recovery
최근 {N}개 메시지만 대화에 포함되어 있다.
이전 대화 내용이 필요하면:
  read_file("{session_dir}/history.jsonl")
```

LLM에게 행동 지시(어디를 읽어라)이므로 system role이 적합하다.
세션 내에서 경로가 고정이므로 캐싱에도 유리하다.
artifact 경로는 history.jsonl 내 대화 흐름에 포함되어 있으므로
별도 안내 불필요. history.jsonl이 대화 기록이자 artifact 인덱스 역할.

### 4.4 Messages 관리: FIFO

- history.jsonl에 전체 대화를 append-only로 기록 (JSON Lines)
- LLM 호출 시 메모리 캐시의 최근 N개를 자연어 변환하여 messages에 포함
- 메시지 단위로 관리 (토큰 기준 아님). 메시지 중간 절단 없음
- thought에 매 턴 현재 목적과 이유를 서술하므로 자기 설명적(self-describing)
- FIFO로 밀려나도 최근 thought들이 맥락을 충분히 전달
- 기본 N=100. 모델의 context window에 따라 조절 가능

### 4.5 저장과 표현의 분리

저장(history.jsonl)과 표현(LLM messages)을 분리한다.

**저장: JSON Lines (구조화, 파싱 안전)**

```jsonl
{"role":"user","content":"인증 시템을 JWT로 리팩토링 해줘"}
{"role":"assistant","thought":"현재 인증 구조를 파악해야 한다","action":"read_file","action_input":{"path":"src/auth.py"}}
{"role":"user","tool":"read_file","args":{"path":"src/auth.py"},"content":"import hashlib\nimport uuid\n..."}
{"role":"assistant","thought":"의존성을 파악하기 위해 explorer에게 위임하겠다","action":"delegate","action_input":{"tasks":[{"task":"auth.py 의존성 조사","agent":"explorer"}]}}
{"role":"user","tool":"delegate","agent":"explorer","content":"auth.py는 3곳에서 import됨","artifact":"delegate_explorer_b7c1_20260405T143045567/"}
{"role":"assistant","thought":"모든 작업이 완료되었다","action":"complete","action_input":{"result":"JWT 리팩토링 완료..."}}
```

**표현: 자연어 변환 (LLM에 전달)**

```
assistant: 현재 인증 구조를 파악해야 한다. auth.py를 읽어 구조를 확인하겠다.
           → read_file(src/auth.py)
user: [read_file] src/auth.py
      import hashlib
      import uuid
      ... (전문)
assistant: 의존성을 파악하기 위해 explorer에게 위임하겠다.
           → delegate(explorer, "auth.py 의존성 조사")
user: [delegate] explorer 완료
      auth.py는 3곳에서 import됨
      → delegate_explorer_b7c1_20260405T143045567/
assistant: 모든 작업이 완료되었다. JWT 리팩토링 완료...
```

변환 규칙:
- assistant: `{thought}. → {action}({핵심 인자 요약})`
- assistant (complete): `{thought}. {result}` — action 래핑 없이 결과 직접 표시
- user (도구 결과): `[{tool_name}] {인자 요약}\n{결과 전문}\n→ {artifact 경로}`
- user (사용자 입력): 그대로

### 4.6 Native Tool Calling 미사용

Anthropic/OpenAI의 native tool calling (tool_use blocks, function calling)을 사용하지 않는다.
모든 provider가 동일한 메시지 포맷을 사용: 자연어 user/assistant 교대.
LLM은 ReAct JSON으로 출력하고, 코드에서 파싱하여 도구를 실행하는 text parsing 방식만 사용.

이유:
- 저장(history.jsonl)과 표현(LLM messages)의 일관성
- provider별 분기 코드 제거로 단순화
- FIFO + 자연어 변환 구조와 자연스럽게 호환

### 4.7 Thought 프롬프트 강화

Format Rules에서 thought 작성 지침을 강화한다:

```
thought에는 반드시 다음을 포함할 것:
1. 현재 무엇을 달성하려 하는지 (목적)
2. 왜 이 action을 선택했는지 (이유)
```

이를 통해:
- 매 턴의 thought가 미니 맥락 앵커 역할
- FIFO로 오래된 메시지가 밀려나도 최근 thought에 목적이 반복 서술
- history.jsonl을 나중에 읽을 때도 흐름 파악이 용이

## 5. 세션 파일 상세

### 5.1 history.jsonl (전체 대화 기록)

```jsonl
{"role":"user","content":"인증 시스템을 JWT로 리팩토링 해줘"}
{"role":"assistant","thought":"현재 인증 구조를 파악하기 위해 auth.py를 읽겠다","action":"read_file","action_input":{"path":"src/auth.py"}}
{"role":"user","tool":"read_file","args":{"path":"src/auth.py"},"content":"import hashlib\nimport uuid\nfrom datetime import datetime, timedelta\n..."}
{"role":"assistant","thought":"의존성을 파악하기 위해 explorer에게 위임하겠다","action":"delegate","action_input":{"tasks":[{"task":"auth.py 의존성 조사","agent":"explorer"}]}}
{"role":"user","tool":"delegate","agent":"explorer","content":"auth.py는 3곳에서 import됨: views.py, middleware.py, api.py","artifact":"delegate_explorer_b7c1_20260405T143045567/"}
{"role":"assistant","thought":"JWT 방식이 stateless 요구사항에 맞다. 리팩토링 계획을 세우겠다","action":"complete","action_input":{"result":"리팩토링 계획: 1. jwt.py 생성 2. auth.py 수정..."}}
```

- append-only: 매 턴 끝에 JSON 한 줄 추가
- FIFO의 원본 저장소: 메모리 캐시의 백업. 세션 재개 시 마지막 N개 복원
- Context Recovery의 대상: LLM이 과거 맥락 필요 시 이 파일을 read_file
- artifact 경로가 대화 흐름에 자연스럽게 포함되어 인덱스 역할

### 5.2 Artifact 파일

```markdown
---
tags: [delegate, coder, jwt]
summary: JWT 미들���어 구현
created_at: 2026-04-05T14:32:30Z
---

{LLM이 생성한 결과 본문}
```

저장 대상:

| 저장함 | 저장 안 함 |
|--------|-----------|
| delegate/skill 최종 결과 | 매 실행 raw LLM 응답 |
| LLM이 생성한 분석/종합 | read_file 결과 |
| 실행 계획 (plan) | shell 출력 |
| 재현 비용이 높은 산출물 | 단순 도구 호출 결과 |

## 6. Delegate / Skill 흐름

### 6.1 Context 모드

| 모드 | 전달 내용 | 용도 |
|------|----------|------|
| none | task prompt만 | 독립적 작업 |
| fork | task prompt + parent history.jsonl 복사 | 맥락 필요한 작업 |
| ~~inherit~~ | 삭제 | 문제만 발생 |

**fork의 재정의**: parent의 history.jsonl을 복사하여 delegate의 history.jsonl로 사용.
delegate는 복사된 history 위에 자기 대화를 계속 append한다.
별도 read_file 없이 시작 시점부터 parent 맥락을 가지며,
FIFO가 동일하게 적용되어 마지막 N개 메시지로 context 구성.

### 6.2 Role 및 도구 상속

**Role 상속:**
- delegate: Agent Role이 기본 Role을 대체 (택 1)
- skill: parent의 Role을 이어받음 (main이 parent면 기본 Role, delegate가 parent면 Agent Role)

**도구 상속:**
- delegate: agent 정의의 allowed-tools 사용
- skill: skill의 allowed-tools ∩ parent의 allowed-tools = 실제 사용 가능 도구
- 교집합이 빈 집합이면 스킬 실행을 거부

```
explorer (allowed: read_file, shell)
  └→ run_skill summarize (allowed: read_file, shell)
     → 교집합: {read_file, shell} → 실행 가능

explorer (allowed: read_file, shell)
  └→ run_skill optimize (allowed: read_file, shell, write_file)
     → 교집합: {read_file, shell} → 실행 가능, but write_file 못 씀

explorer (allowed: read_file, shell)
  └→ run_skill deploy (allowed: write_file, fetch)
     → 교집합: {} → 실행 거부
```

**Artifact 경로:**
- skill/delegate 모두 자기 디렉토리를 가짐 (parent 디렉토리 안에 subdir)
- 재귀적으로 중첩 가능 (skill→delegate→skill→...)

### 6.3 실행 흐름

```
[시작]
  parent history.jsonl에 기록: delegate 호출 메시지
  delegate에 전달:
    - task prompt
    - parent history.jsonl 복사 (fork 모드일 때)
    - session dir 경로 (artifact 저장용)

[delegate 실행 중]
  자체 FIFO messages
    none: 빈 history.jsonl에서 시작
    fork: parent history.jsonl 복사본 위에 계속 append
  결과는 자기 디렉토리의 result.md에 저장

[완료]
  parent에 반환:
    1. 최종 결과 텍스트 (observation으로 대화에 들어감)
    2. delegate 디렉토리 경로

  parent history.jsonl에 기록: delegate 결과 + 디렉토리 경로
```

### 6.4 병렬 delegate

각 delegate가 고유 디렉토리를 가지므로 충돌 없음.
병렬 실행 후 join으로 전부 완료 대기. 완료 후 호출 순서대로 parent history.jsonl에 append.

## 7. 실행 시나리오 시각화

### 시나리오: "인증 시스템을 JWT로 리팩토링 해줘"

#### 7.1 턴 1~5: 분석 및 계획

```
                    Context Window (main)
┌─ system ─────────────────────────────────────────────┐
│ [Primacy]                                            │
│  기본 Role / Task Guidelines / Format Rules          │
│ [Middle]                                             │
│  Tools: 전체 / Skills: 전체 / Agents: 전체             │
│ [Recency]                                            │
│  DIRECTIVE.md / Environment                          │
│  Context Recovery:                                   │
│   "이전 대화 내용이 필요하면                            │
│    read_file(.agent-cli/sessions/abc/history.jsonl)" │
└──────────────────────────────────────────────────────┘
┌─ messages (FIFO N=100, 자연어 변환) ─────────────────┐
│ user: 인증 시스템을 JWT로 리팩토링 해줘.               │
│ asst: 현재 인증 구조를 파악해야 한다.                  │
│       auth.py를 읽어 구조를 확인하겠다.                │
│       → read_file(auth.py)                           │
│ user: [read_file] auth.py                            │
│       class AuthManager:                             │
│           def login(self, ...): ...                  │
│           def logout(self, ...): ...                 │
│           def verify(self, ...): ...                 │
│       (전문 150 lines)                               │
│ asst: 의존성을 파악하기 위해 explorer에게 위임하겠다.   │
│       → delegate(explorer, "auth.py 의존성 조사")     │
│ user: [delegate] explorer 완료                       │
│       auth.py는 3곳에서 import됨:                     │
│       views.py, middleware.py, api.py                 │
│       → delegate_explorer_b7c1_.../                  │
│ asst: JWT 방식이 stateless 요구사항에 맞다.            │
│       리팩토링 계획을 세우겠다.                         │
│       → complete(계획 텍스트)                         │
│       → main_plan_e8d4_...md                         │
└──────────────────────────────────────────────────────┘
```

```
                    Session Files (디스크)
┌─ .agent-cli/sessions/abc/ ───────────────────────────┐
│                                                      │
│ history.jsonl:                                       │
│   {"role":"user","content":"인증 시스템을 JWT로..."}   │
│   {"role":"assistant","thought":"현재 인증 구조를     │
│     파악해야 한다","action":"read_file",...}           │
│   {"role":"user","tool":"read_file","content":"..."}  │
│   {"role":"assistant","thought":"의존성을 파악하기     │
│     위해 explorer에게 위임","action":"delegate",...}   │
│   {"role":"user","tool":"delegate","agent":"explorer", │
│     "artifact":"delegate_explorer_b7c1_.../","..."}   │
│   {"role":"assistant","thought":"JWT 방식이           │
│     stateless 요구사항에 맞다","action":"complete",...}│
│                                                      │
│ main_plan_e8d4_20260405T143112890.md                 │
│                                                      │
│ delegate_explorer_b7c1_20260405T143045567/            │
│   ├── history.jsonl                                  │
│   └── result.md                                      │
│                                                      │
└──────────────────────────────────────────────────────┘
```

#### 7.2 턴 6~15: 구현 (FIFO가 초기 메시지를 밀어냄)

```
┌─ system ─────────────────────────────────────────────┐
│ (동일 — 고정)                                         │
└──────────────────────────────────────────────────────┘
┌─ messages (FIFO N=100, 자연어 변환, 초기 턴 밀려남) ──┐
│                                                      │
│ (N=100이면 상당히 많은 턴이 보존됨.                    │
│  밀려나기 시작하면 history.jsonl에 전문 남아있음)       │
│                                                      │
│ asst: JWT 방식이 stateless 요구사항에 맞다.            │
│       리팩토링 계획을 세우겠다.                         │
│       → main_plan_e8d4_...md                         │
│ user: 좋아, 구현 해줘                                 │
│ asst: JWT 미들웨어 구현을 coder에게 위임하겠다.        │
│       계획은 main_plan_e8d4...md에 있다.              │
│       → delegate(coder, "JWT 미들웨어 구현")          │
│ user: [delegate] coder 완료                          │
│       middleware/jwt.py 생성, auth.py 수정.           │
│       → delegate_coder_f1a9_.../                     │
│ asst: 테스트 작성을 coder에게 위임하겠다.             │ 
│       → delegate(coder, "JWT 테스트 작성")            │
│ user: [delegate] coder 완료. 5개 테스트 통과.          │
│       → delegate_coder_c2b8_.../                     │
│ asst: 구현과 테스트가 완료되었다.                      │
│       통합 테스트를 돌릴까요?                           │
│                                                      │
└──────────────────────────────────────────────────────┘
```

```
                    Session Files (디스크)
┌─ .agent-cli/sessions/abc/ ───────────────────────────┐
│                                                      │
│ history.jsonl:                                       │
│   (턴 1부터 현재까지 전부, JSON Lines)                 │
│                                                      │
│ main_plan_e8d4_20260405T143112890.md                 │
│                                                      │
│ delegate_explorer_b7c1_20260405T143045567/            │
│   ├── history.jsonl                                  │
│   └── result.md                                      │
│ delegate_coder_f1a9_20260405T143230456/              │
│   ├── history.jsonl                                  │
│   └── result.md                                      │
│ delegate_coder_c2b8_20260405T143415789/              │
│   ├── history.jsonl                                  │
│   └── result.md                                      │
│                                                      │
└──────────────────────────────────────────────────────┘
```

#### 7.3 delegate 내부 (coder, fork 모드)

```
                    Context Window (delegate: coder)
┌─ system ─────────────────────────────────────────────┐
│ [Primacy]                                            │
│  Agent Role: "코드 구현 에이전트. 주어진 계획에 따라   │
│  코드를 작성하고 테스트한다."                           │
│  Task Guidelines / Format Rules                      │
│ [Middle]                                             │
│  Tools: coder의 allowed-tools                        │
│  Skills: skill_stack 재귀 방지 적용                    │
│  Agents: depth < max_depth + agent_stack 재귀 방지    │
│ [Recency]                                            │
│  DIRECTIVE.md / Environment                          │
│  Context Recovery:                                   │
│   "이전 대화 내용이 필요하면                            │
│    read_file({delegate_history_path})"               │
└──────────────────────────────────────────────────────┘
┌─ messages (FIFO N=100, 자연어 변환) ─────────────────┐
│                                                      │
│ (fork: parent history.jsonl 복사 → 마지막 N개로 시작) │
│                                                      │
│ asst: JWT 방식이 stateless 요구사항에 맞다.            │
│       리팩토링 계획을 세우겠다.                         │
│       → main_plan_e8d4_...md                         │
│ user: JWT 미들웨어를 구현해라.                         │
│       계획: main_plan_e8d4_...md 참조                 │
│ asst: 계획을 확인하기 위해 artifact를 읽겠다.          │
│       → read_file(main_plan_e8d4_...md)              │
│ user: [read_file] main_plan_e8d4_...md               │
│       1. jwt.py 생성 2. auth.py 수정 3. config 추가  │
│       (전문)                                          │
│ asst: 계획에 따라 jwt.py를 먼저 작성하겠다.            │
│       → write_file(middleware/jwt.py, ...)            │
│ user: [write_file] middleware/jwt.py (89 lines 작성)  │
│ asst: auth.py를 JWT 방식으로 수정하겠다.               │
│       → edit_file(src/auth.py, ...)                  │
│ user: [edit_file] src/auth.py (3 changes applied)    │
│ asst: 구현 완료. JWT 미들웨어와 auth.py 수정 완료.     │
│                                                      │
└──────────────────────────────────────────────────────┘
```

#### 7.4 LLM이 과거 맥락을 복구하는 순간

```
user: "처음에 분석했던 auth.py 의존성 결과를 다시 보여줘"

LLM의 thought:
  "초기 분석 결과가 FIFO에서 밀려났다.
   history.jsonl을 읽어서 해당 내용을 찾겠다."

→ read_file(".agent-cli/sessions/abc/history.jsonl")
→ explorer 결과와 디렉토리 경로 확인
→ read_file("delegate_explorer_b7c1_20260405T143045567/result.md")
→ 결과를 사용자에게 전달
```

## 8. 현재 대비 변경 사항

### 8.1 Context 관리

| 항목 | 현재 | 변경 후 |
|------|------|---------|
| 대화 관리 | LLM 기반 압축 (summary) | FIFO N=100 + history.jsonl append-only 전체 기록 |
| 저장 포맷 | 메모리 dict | JSON Lines (history.jsonl) |
| 표현 포맷 | raw ReAct JSON | 자연어 변환 (저장과 표현 분리) |
| FIFO 성능 | N/A | 메모리 캐시, history.jsonl은 write-only. 세션 재개 시에만 read |
| scratchpad | 매 턴 context inject | 삭제. history.jsonl이 대체 |
| context recovery | compression summary를 messages에 inject | system prompt에 Context Recovery Guide |
| thought | 간단한 추론 | 목적 + 이유 필수 서술 |

### 8.2 Artifact / 파일 구조

| 항목 | 현재 | 변경 후 |
|------|------|---------|
| step 카운터 | _step_count로 세 시스템 관리 | 삭제. timestamp+hash |
| artifact 저장 | 매 턴 자동 (raw 응답) | 의미 있는 산출물만 명시적 |
| artifact 구조 | step_NNNN.md + subdirectory | main: root flat, delegate/skill: subdir (history.jsonl + result.md), 재귀 중첩 |
| Goal 섹션 | scratchpad에 존재 | 삭제. thought가 대체 |

### 8.3 System Prompt

| 항목 | 현재 | 변경 후 |
|------|------|---------|
| Role | 항상 기본 Role | main: 기본, delegate: Agent Role 대체, skill: parent 상속 |
| Git Context | system prompt에 inject | 삭제. 필요 시 LLM이 shell로 확인 |
| Session ID | 별도 섹션 | 삭제. Context Recovery Guide 경로에 포함 |
| Context Recovery | 없음 | system prompt recency에 history.jsonl 경로 가이드 |

### 8.4 Delegate / Skill

| 항목 | 현재 | 변경 후 |
|------|------|---------|
| inherit 모드 | 공유 ContextManager | 삭제 |
| fork 모드 | 대화 히스토리(messages) 복사 | parent history.jsonl 복사 후 계속 append |
| 병렬 delegate 결과 | 완료 순서대로 | join 후 호출 순서대로 append |
| skill 도구 | skill의 allowed-tools 그대로 | skill allowed-tools ∩ parent allowed-tools |
| skill 도구 교집합 빈 경우 | 실행됨 | 실행 거부 |
| skill Role | 기본 Role 고정 | parent의 Role 상속 |
| agent 재귀 | depth 제한만 | depth 제한 + agent_stack으로 동일 agent 재호출 방지 |

## 9. 영향 받는 파일

| 파일 | 변경 내용 |
|------|----------|
| `agent_cli/context/manager.py` | 전면 재작성: FIFO + 메모리 캐시 + history.jsonl append. 압축 로직 제거, scratchpad inject 제거 |
| `agent_cli/context/scratchpad.py` | 삭제 |
| `agent_cli/prompts/compression_prompt.py` | 삭제 |
| `agent_cli/loop.py` | 자연어 변환 로직 (JSON→자연어), thought 강화, history.jsonl append, FIFO 캐시 관리 |
| `agent_cli/tools/delegate.py` | inherit 제거, fork=history.jsonl 복사, agent_stack 재귀 방지, 결과를 subdir(history.jsonl+result.md)로 저장, 병렬 결과 호출 순서 append |
| `agent_cli/prompts/system_prompt.py` | Role 상속 로직 (main/delegate/skill), Git Context 제거, Session ID 제거, Context Recovery Guide 추가, thought 지침 강화 |
| `agent_cli/skills/executor.py` | 도구 교집합 로직, parent Role 상속, 빈 교집합 시 실행 거부, artifact 경로 parent 따름 |
| `tests/` | 전면 업데이트: scratchpad 테스트 삭제, FIFO 테스트, 자연어 변환 테스트, history.jsonl 테스트, 도구 교집합 테스트, agent_stack 테스트 |
