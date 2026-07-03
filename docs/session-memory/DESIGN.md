# Session Memory — 설계 문서

세션 내에서 LLM 이 **중대한 실패·중요한 발견·결정·메모**를 명시적으로 기록하고, 필요할 때
꺼내 쓰는 도구. compaction 에 유실되지 않는 curated 저장소.

## 1. 동기 (왜 필요한가)

두 문제를 푼다:

1. **compaction 생존** — 컨텍스트가 예산의 90% 를 넘으면 `context/manager.py` 가 오래된 절반을
   요약/드롭한다. "왜 이 빌드가 깨졌는지", "probe 가 A 경로로만 불린다는 발견" 같은 salient
   한 정보가 바로 이때 유실된다. 메모리는 **롤링 컨텍스트 밖**(세션 디렉토리 파일)에 저장돼
   compaction 대상이 아니고, 상시 인덱스로 재주입된다.
2. **recall 문제** — 순수 pull(저장만 하고 안 보여줌)은 실패한다: LLM 이 "내가 뭘 기록했는지"
   자체를 잊어 `get` 을 호출할 생각을 안 한다. 그래서 **가벼운 상시 인덱스**(id·타입·한줄요약)를
   시스템 프롬프트에 노출하고, 무거운 전체 내용은 on-demand 로 꺼낸다.

### 기존 시스템과의 관계 (중복 아님)
- `read_context`(SQL over history.jsonl) = **raw 이력** 질의. 메모리는 **LLM 이 큐레이션한
  durable salience** — 상보적.
- compaction 요약 = **자동·lossy**. 메모리는 **LLM 이 명시적으로 "이건 잃지 마" 표시** — 상보적.

## 2. 스키마 (확정)

`<session_dir>/memory.jsonl` — 한 줄에 항목 하나(JSONL, append-friendly):

```json
{
  "id": 1,
  "type": "failure",
  "summary": "KUnit 빌드가 CONFIG_X 없이 깨짐",
  "detail": "kunit.py run 시 undefined ref ... 근본원인 ... 해결은 defconfig 에 CONFIG_X=y",
  "tags": ["kunit", "build"],
  "turn": 12,
  "ts": "2026-07-04T09:15:00"
}
```

| 필드 | 타입 | 작성 | 비고 |
|---|---|---|---|
| `id` | int | 자동 | 세션 내 monotonic. 참조·갱신·삭제 키 |
| `type` | enum | LLM | `failure` ⚠ / `discovery` 💡 / `decision` 🔀 / `note` 📝 |
| `summary` | str **필수** | LLM | 상시 인덱스 한 줄 |
| `detail` | str 선택 | LLM | on-demand pull |
| `tags` | [str] 선택 | LLM | 필터 |
| `turn` | int | 자동 | 기록 시점 턴 |
| `ts` | str | 자동 | ISO 기록 시각 |

**type 의미** (LLM 이 다르게 다루도록):
- `failure` ⚠ 중대한 실패 → 반복 회피
- `discovery` 💡 중요한 발견 → 재사용
- `decision` 🔀 결정 → 왜 X 를 골랐나(근거)
- `note` 📝 일반 메모

## 3. 저장 · 수명

- 파일: `<session_dir>/memory.jsonl` (= `.agent-cli/sessions/{id}/memory.jsonl`).
  `read_context`/`history.jsonl` 과 같은 세션 디렉토리.
- `add` = append 한 줄. `update`/`delete` = 파일 rewrite(항목 적어 비용 무시).
- **resume 복원**: 파일이 세션 디렉토리에 있으니 `--resume` 시 그대로 로드된다. (별도 배선 불필요 —
  도구가 매번 파일을 읽고, 시스템 프롬프트 섹션도 파일에서 빌드.)
- `id` 는 파일 내 최대 id + 1 (append-only monotonic, 삭제해도 재사용 안 함).

## 4. 도구 표면

flat-native `memory` 도구 (`code_index` 처럼 `{mode, ...}` 단일 op). 한 op = 한 메모리 연산.

| mode | 인자 | 반환 |
|---|---|---|
| `add` | `type, summary, detail?, tags?` | `{id}` |
| `get` | `id` | 전체 항목(detail 포함) |
| `update` | `id, summary?, detail?, type?, tags?` | ok (준 필드만 교체) |
| `delete` | `id` | ok |
| `list` | `type?, tag?` | 필터된 요약 목록 |

- `add`/`update` 는 `type` 이 enum 4종 아니면 친절 에러(recover 가능한 observation).
- `get`/`update`/`delete` 는 없는 id 면 친절 에러.
- `list` 는 상시 인덱스와 중복이지만 필터(타입/태그)·대량 세션에서 유용.

## 5. 상시 인덱스 주입

`build_system_prompt_sections(..., session_dir=...)` 에 `## Session Memory` 섹션 추가
(Recency 밴드, `Directives` 근처). `<session_dir>/memory.jsonl` 을 읽어 렌더:

```
## Session Memory (3개) — 전체 내용은 memory(mode=get, id=N)
⚠ #1 [failure] KUnit 빌드가 CONFIG_X 없이 깨짐
💡 #3 [discovery] 드라이버 probe 는 A 경로로만 호출됨
🔀 #5 [decision] http 허용은 사설 호스트로 제한
```

- 메모리 0개면 섹션 생략(`_load_directives` 와 동형 — 빈 문자열 반환).
- 섹션은 **요약만** 표시(detail 제외) → compaction-immune 하면서 토큰 저렴.

### 5.1 KV 캐시 / rebuild
- 시스템 프롬프트는 setup 시 1회 빌드 + DIRECTIVE 편집 시 rebuild(`consume_directives_reload()`
  → `_rebuild_system_prompt()`, loop.py:640). 메모리도 **같은 dirty-flag 패턴** 재사용:
  `memory add/update/delete` 후 `mark_memory_dirty()` → 다음 턴 시작에서 소비해 rebuild.
- 결과: 새 메모리는 **다음 턴부터** 인덱스에 보임(방금 add 한 턴엔 이미 도구 결과로 아니까 무해).
  KV prefix 는 메모리 변경 시 리셋(Directives 와 동일 트레이드오프 — 변경 빈도 낮아 수용).

### 5.2 크기 상한
- 인덱스는 **요약만**이라 항목당 1줄. 소프트 정책: 표시를 **최근 N=30**개로 캡 + `(그 외 M개,
  memory list 로 조회)` 꼬리. 하드 리밋 없음(삭제/정정로 관리 유도). N 은 튜닝값.

## 6. Scope (v1)
- **메인 세션 + delegate 각자**: delegate 서브에이전트는 자기 session_dir(subdir)를 가지므로
  자기 memory.jsonl 을 독립으로 갖는다(격리). 메인↔서브 공유는 v1 제외.
- 향후: 서브에이전트 실패를 부모로 bubble-up 하는 옵션(별도 논의).

## 7. 코드 접점 (파일별)

| 파일 | 변경 |
|---|---|
| `agent_cli/memory.py` (신규) | 스토어: `load(session_dir)`, `add/get/update/delete/list`, `render_index()`, `mark_memory_dirty()`/`consume_memory_reload()`. 순수 로직(파일 IO + 렌더) |
| `agent_cli/tools/memory_tool.py` (신규) | `MemoryTool(Tool)` — flat `{mode,...}` 스키마, `_run(args, session_dir=)` → 스토어 dispatch |
| `agent_cli/tools/registry.py` | `_ALL_TOOLS` 에 `MemoryTool()` 등록 |
| `agent_cli/prompts/system_prompt.py` | `build_system_prompt_sections` 에 `## Session Memory` 섹션(`_load_session_memory(session_dir)`) |
| `agent_cli/loop.py` | 턴 시작 reload 체크에 `consume_memory_reload()` 추가(directives 옆) |
| `docs/ARCHITECTURE.md` / `README.md` | 도구·모듈·LOC 갱신, 도구 표 + 사용법 |
| `tests/test_memory.py` (신규) | 아래 계획 |

## 8. 테스트 계획 (TDD — red 먼저)

**스토어(`memory.py`)** — 순수, 파일 IO:
- add → id 부여(monotonic), 파일 append; 두 번째 add id=2
- get(없는 id) → None/에러; get(id) → 항목
- update(부분 필드) → 준 필드만 교체, 나머지 보존; update 없는 id → 에러
- delete(id) → 제거, 이후 id 재사용 안 함(다음 add 는 max+1)
- list(type=) / list(tag=) 필터
- load(session_dir) 라운드트립: add 후 새 load 가 복원(resume 시뮬)
- render_index: 요약만·타입 아이콘·개수; 0개면 "" ; >N 이면 캡+꼬리
- type enum 검증: 잘못된 type → 에러

**도구(`memory_tool.py`)**:
- 각 mode dispatch → 스토어 호출; 잘못된 mode/누락 인자 → 친절 에러 ToolResult
- session_dir=None(세션 없음) → 명확한 에러

**시스템 프롬프트**:
- 메모리 있으면 `## Session Memory` 섹션 포함, 없으면 생략
- 섹션은 detail 미포함(요약만)

**회귀**: 기존 시스템 프롬프트 스냅샷(메모리 0개면 바이트 동일), 도구 로스터.

## 9. 미해결 / 결정 필요
- N(인덱스 표시 캡) 초기값: **30** 제안.
- `list` 를 상시 인덱스와 별도로 둘지(중복) vs 인덱스로 충분한지 — 필터 가치로 유지 제안.
- 도구 이름 `memory` 확정? (대안 `note`) — `memory` 제안.

## 10. 버전
- 새 도구 + 새 시스템 프롬프트 섹션 = 사용자 대면 기능 추가 → **MINOR**.
- resume/스키마 무변경(옛 세션은 memory.jsonl 없음 → 섹션 생략, 완전 하위호환).
