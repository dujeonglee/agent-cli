# Hook 시스템 재설계

> Status: Draft
> Date: 2026-04-12

## 1. 문제 정의

현재 hook 시스템의 한계:
- **shell 명령어만** — 외부 프로세스 실행. context window 조작 불가
- **도구 전후 3개 시점만** — 세션/턴/LLM 호출 등 라이프사이클 미지원
- **단방향** — hook이 context를 읽거나 수정할 수 없음
- **메모리 연동 불가** — MCP memory 등 외부 저장소와 연결 수단 없음

## 2. 설계 목표

- Hook을 **정보 흐름의 허브**로 — context에 데이터를 넣고 빼는 확장점
- **Python hook + shell hook 공존** — Python은 context 조작, shell은 외부 명령
- **메모리 통합** — MCP memory를 hook에서 자연스럽게 사용
- **사용자 작성 용이** — sync 함수, HookContext가 복잡성 감춤

## 3. 라이프사이클 이벤트

```
세션 시작
  │
  ├─ OnSessionStart             ← 메모리 로드, 초기 context 구성
  │
  ├─ 매 턴:
  │   ├─ PreLLMCall             ← context 정리/보강, 메모리 검색 → inject
  │   ├─ LLM 호출
  │   ├─ PostLLMCall            ← 응답 분석, 중요 정보 추출
  │   │
  │   ├─ PreToolUse             ← 도구 실행 차단/입력 수정 (기존 유지)
  │   ├─ 도구 실행
  │   ├─ PostToolUse            ← 성공/실패 모두. ctx.tool_result.success로 구분
  │   │
  │   └─ OnTurnEnd              ← 턴 요약 → 메모리 저장
  │
  ├─ OnDelegateStart            ← delegate context 준비
  ├─ OnDelegateEnd              ← delegate 결과 → 메모리 저장
  │
  ├─ OnSkillStart               ← skill context 준비
  ├─ OnSkillEnd                 ← skill 결과 → 메모리 저장
  │
  └─ OnSessionEnd               ← 세션 요약 → 메모리 저장, 정리
```

총 11개 이벤트 (PostToolUse가 성공/실패 통합).

## 4. HookContext — Hook이 받는 것

```python
class HookContext:
    """Hook 함수가 받는 컨텍스트 객체."""

    # 읽기 전용
    session_dir: Path                 # 세션 디렉토리
    history_path: Path                # history.jsonl 경로
    turn: int                         # 현재 턴 번호
    event: str                        # 이벤트 이름 (e.g., "PreLLMCall")

    # 읽기/쓰기 가능
    messages: list[dict]              # 현재 FIFO messages

    # 이벤트별 추가 데이터
    tool_name: str | None             # PreToolUse/PostToolUse 시
    tool_input: dict | None           # PreToolUse 시
    tool_result: ToolResult | None    # PostToolUse 시 (success/failure 모두)
    llm_response: str | None          # PostLLMCall 시
    delegate_result: ToolResult | None  # OnDelegateEnd 시
    skill_result: ToolResult | None   # OnSkillEnd 시

    # Context 조작 메서드
    def inject_message(self, role: str, content: str) -> None:
        """messages에 메시지 추가."""

    def inject_system_section(self, title: str, content: str) -> None:
        """system prompt에 동적 섹션 추가."""

    def remove_system_section(self, title: str) -> None:
        """system prompt에서 동적 섹션 제거."""

    # 메모리 메서드 (MCP memory 래핑)
    def store_memory(self, entities: list[dict]) -> None:
        """MCP memory에 entity 저장."""

    def search_memory(self, query: str) -> list[dict]:
        """MCP memory에서 검색."""

    def read_memory(self) -> dict:
        """MCP memory 전체 그래프 읽기."""

    # 제어
    def block(self, reason: str) -> None:
        """PreToolUse: 도구 실행 차단."""

    def modify_input(self, new_input: dict) -> None:
        """PreToolUse: 도구 입력 수정."""
```

## 5. Hook 파일 규약

### 5.1 위치

```
.agent-cli/hooks/          ← 프로젝트 로컬
~/.agent-cli/hooks/        ← 유저 전역
```

양쪽 모두 스캔. 프로젝트 hook이 먼저 실행.

### 5.2 파일 형식

```python
# .agent-cli/hooks/00_memory.py

EVENTS = ["OnSessionStart", "OnTurnEnd", "OnSessionEnd"]

def on_session_start(ctx: HookContext):
    """세션 시작 시 관련 메모리 로드."""
    memories = ctx.search_memory("project context")
    if memories:
        ctx.inject_system_section("Memory", format_memories(memories))

def on_turn_end(ctx: HookContext):
    """중요 결정을 메모리에 저장."""
    last = ctx.messages[-1] if ctx.messages else {}
    thought = last.get("thought", "")
    if thought and len(thought) > 50:
        ctx.store_memory([{
            "name": f"decision_turn_{ctx.turn}",
            "entityType": "decision",
            "observations": [thought],
        }])

def on_session_end(ctx: HookContext):
    """세션 요약을 메모리에 저장."""
    ctx.store_memory([{
        "name": f"session_{ctx.session_dir.name}",
        "entityType": "session",
        "observations": [f"Completed {ctx.turn} turns"],
    }])
```

### 5.3 실행 순서

1. 파일명 숫자 prefix 순서 (`00_` → `10_` → `20_`)
2. 같은 prefix 내에서는 알파벳 순
3. 프로젝트 hooks → 유저 hooks (같은 순서 내)

### 5.4 이벤트 매핑

파일 내 `EVENTS` 리스트로 구독할 이벤트 선언.
함수 이름은 이벤트의 snake_case:

| 이벤트 | 함수명 |
|--------|--------|
| OnSessionStart | `on_session_start(ctx)` |
| PreLLMCall | `pre_llm_call(ctx)` |
| PostLLMCall | `post_llm_call(ctx)` |
| PreToolUse | `pre_tool_use(ctx)` |
| PostToolUse | `post_tool_use(ctx)` — ctx.tool_result.success로 성공/실패 구분 |
| OnTurnEnd | `on_turn_end(ctx)` |
| OnDelegateStart | `on_delegate_start(ctx)` |
| OnDelegateEnd | `on_delegate_end(ctx)` |
| OnSkillStart | `on_skill_start(ctx)` |
| OnSkillEnd | `on_skill_end(ctx)` |
| OnSessionEnd | `on_session_end(ctx)` |

## 6. Shell Hook 호환

기존 hooks.json 방식 유지. Python hook과 공존:

```json
// .agent-cli/hooks.json — shell hook (기존)
{
  "PreToolUse": [
    {
      "tool_name": "shell",
      "command": "echo 'executing: $TOOL_NAME'"
    }
  ]
}
```

실행 순서: Python hooks → shell hooks (같은 이벤트 내).
shell hook은 context 조작 불가 (기존 동작 유지).
기존 PostToolUseFailure shell hook은 PostToolUse에서 실행 (호환 유지).

## 7. 메모리 통합 예시

### 7.1 장기 기억 플러그인

```python
# .agent-cli/hooks/00_memory.py
EVENTS = ["OnSessionStart", "OnTurnEnd", "OnSessionEnd"]

def on_session_start(ctx):
    """프로젝트 관련 기억을 context에 로드."""
    results = ctx.search_memory(str(ctx.session_dir))
    if results:
        summary = "\n".join(
            f"- {e['name']}: {', '.join(e.get('observations', []))}"
            for e in results
        )
        ctx.inject_system_section("Project Memory", summary)

def on_turn_end(ctx):
    """파일 수정 시 기억에 기록."""
    last = ctx.messages[-1] if ctx.messages else {}
    action = last.get("action", "")
    if action in ("write_file", "edit_file"):
        path = last.get("action_input", {}).get("path", "")
        ctx.store_memory([{
            "name": f"modified_{path}",
            "entityType": "file_change",
            "observations": [last.get("thought", "")],
        }])

def on_session_end(ctx):
    """세션 요약 저장."""
    ctx.store_memory([{
        "name": f"session_{ctx.session_dir.name}",
        "entityType": "session_summary",
        "observations": [f"{ctx.turn} turns completed"],
    }])
```

### 7.2 Context 보강 플러그인

```python
# .agent-cli/hooks/10_context_boost.py
EVENTS = ["PreLLMCall"]

def pre_llm_call(ctx):
    """LLM 호출 전 관련 메모리를 context에 주입."""
    if not ctx.messages:
        return
    # 마지막 사용자 메시지에서 키워드 추출
    last_user = None
    for msg in reversed(ctx.messages):
        if msg.get("role") == "user" and msg.get("content"):
            last_user = msg["content"]
            break
    if not last_user:
        return
    # 키워드로 메모리 검색
    results = ctx.search_memory(last_user[:100])
    if results:
        hints = "\n".join(
            f"- {e['name']}: {e.get('observations', [''])[0][:100]}"
            for e in results[:5]
        )
        ctx.inject_system_section("Relevant Memory", hints)
```

## 8. 아키텍처

### 8.1 파일 구조

```
agent_cli/
├── hooks.py                 ← 기존 shell hook (호환 유지)
├── hooks/
│   ├── __init__.py
│   ├── context.py           ← HookContext 클래스
│   ├── loader.py            ← Python hook 파일 스캔/로드
│   ├── runner.py            ← hook 실행 엔진 (Python + shell 통합)
│   └── events.py            ← 이벤트 상수 정의
```

### 8.2 실행 흐름

```
이벤트 발생 (e.g., PreLLMCall)
  │
  ├─ HookContext 생성 (현재 상태 스냅샷)
  │
  ├─ Python hooks 실행 (파일명 순서)
  │   ├─ 00_memory.py: pre_llm_call(ctx)
  │   └─ 10_context_boost.py: pre_llm_call(ctx)
  │
  ├─ Shell hooks 실행 (hooks.json)
  │   └─ command 실행, 결과 수집
  │
  └─ HookContext 변경사항 적용
      ├─ messages 변경 → FIFO cache 반영
      ├─ system sections 변경 → 다음 system prompt 빌드 시 반영
      └─ block/modify → 도구 실행 제어
```

## 9. 구현 계획

### Phase 1: 기반 ✅
- [x] HookContext 클래스 구현
- [x] events.py 이벤트 상수
- [x] loader.py — .agent-cli/hooks/*.py 스캔, EVENTS 기반 매핑
- [x] runner.py — 이벤트별 hook 실행
- [x] 유닛 테스트 (42개)

### Phase 2: loop.py 통합 ✅
- [x] 11개 이벤트 발화 지점에 runner.fire(event, ctx) 호출
- [x] 기존 shell hook과 공존 (Python hooks → shell hooks 순서)
- [x] system prompt 동적 섹션 메커니즘 (_apply_system_sections)
- [x] hooks.py → hooks/shell.py 이동, __init__.py에서 re-export
- [x] 유닛 테스트 (8개 loop integration 포함)
- [x] ARCHITECTURE.md 업데이트

### Phase 3: 메모리 연동
- [x] HookContext.store_memory / search_memory / read_memory (Phase 1에서 구현)
- [ ] MCP memory 서버 연동 테스트
- [ ] 예제 hook: 00_memory.py
- [ ] integration 테스트

### Phase 4: 정리 ✅
- [x] README 업데이트 (Python hook + Shell hook + HookContext + 11 이벤트 문서화)
- [x] ARCHITECTURE.md Section 14 추가 (Hook 시스템 전체 아키텍처)
- [x] 예제 hook 작성 및 MCP memory 연동 테스트 완료
