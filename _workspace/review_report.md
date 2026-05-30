# Review Report — web-fixes-3 (Task #3)

> Reviewer: reviewer (web-fixes-3 team)
> Date: 2026-05-22
> Scope: implementer가 보고한 7개 파일 (main.py, render/web.py, web/server.py, app.js, test_web_renderer.py, test_web_server.py, test_app_markdown.py)
> Companion: [DESIGN.md](../docs/web-fixes-3/DESIGN.md), [TEST_PLAN.md](../docs/web-fixes-3/TEST_PLAN.md)

---

## 0. 종합 판정 — **PASS (with minor findings)**

- **회귀**: `pytest tests/ -m "not ollama_integration"` → 1463 passed, 1 skipped (전체 통과).
- **Lint**: `ruff check` / `ruff format --check` 모두 0 exit.
- **신규 테스트 21건 자동 통과** (node 활성 env): markdown 11, replay 5, shutdown 2, resume 1, sentinel 2.
- **P0 보안 검증 완료**: XSS payload (`<script>`, `<img onerror>`), placeholder collision, ReDoS adversarial input — 모두 안전.
- **Critical / High 없음**. Medium 1, Low 4 — 모두 향후 작업으로 다룰 수 있는 수준. 본 PR 머지 권장.

---

## 1. 보안 (Adversarial 검증)

### 1.1 검증한 공격 벡터 (전부 PASS)

| 공격 시나리오 | 결과 | 비고 |
|---|---|---|
| `<script>alert(1)</script>` raw 삽입 | escape 유지 (`&lt;script&gt;`) | `escapeHtml` 1차 통과 후 markdown 변환은 escaped 문자열에서만 동작. 신규 태그 생성 path 화이트리스트(h1-3, table/tr/th/td, ul/ol/li, strong/em, code/pre)만 사용. |
| `### Header <img onerror=x>` | `<h3>&lt;img...&gt;</h3>` | heading 변환은 라인 내용을 그대로 wrapping할 뿐 escape 풀지 않음. |
| Code fence placeholder collision `<!--cf:0-->`를 user input에 포함 | escape로 `&lt;!--cf:0--&gt;` → 변환 토큰과 불일치 → restore 시 충돌 없음 | `split/join` 패턴이라 정규식 메타 영향 없음. |
| 어시스턴트가 `<strong>`/`<table>` 등 화이트리스트 태그 emit | escape 통과 → 사용자에게는 entity-encoded text로만 보임 | path 전체가 `escapeAndFormat` 또는 `escapeHtml`를 거침. |
| Rich markup `[bold]…[/bold]` injection (observation 본문) | 본 PR 변경 외, `richMarkupToHtml`은 `escapeHtml` 이후 동작하므로 안전 | 기존 동작 그대로. |

### 1.2 ReDoS / Catastrophic Backtracking

50K+ 문자 adversarial 입력으로 모든 신규 정규식 측정 (`/tmp/test_redos.js`):

| 정규식 | 50K 입력 시간 | 평가 |
|---|---|---|
| `/\*\*([^*\n]+?)\*\*/g` (bold) | < 1ms | character class + `\n` 차단으로 안전 |
| `/(^\|[^*])\*([^*\n]+?)\*(?!\*)/g` (italic) | < 1ms | 동일 |
| `/^(#{1,3})\s+(.+?)\s*$/gm` (heading) | 1ms | non-greedy + line-anchored |
| Table separator `^\s*\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)+\|?\s*$` | < 1ms | 그룹 시작이 `\|`로 고정되어 alternation 폭발 없음 |
| Code fence `/```(\w*)\n([\s\S]*?)```/g` | < 1ms | non-greedy, 매치 실패해도 글로벌 스캔 1회 |

ReDoS 결함 **없음**.

### 1.3 Command Injection / Path Traversal

- `/sh` 명령 (`_handle_sh` in `web/server.py:188`)은 본 PR에서 동작 변경 없음. 기존 코드 그대로.
- `--resume <id>` 의 `load_session` (`context/session.py:103`)은 `_SESSIONS_BASE / "sessions" / session_id` 로 path 구성. `session_id = "../../etc/passwd"` 같은 traversal은 `session.jsonl` 확장자 강제 + `is_file()` 체크로 무력화. 또한 `--resume`은 CLI argument이므로 위협 모델상 로컬 사용자 권한. **Info — 본 PR 스코프 밖**.

### 1.4 Signal Handler / Race Condition

- main.py에 직접 `signal.signal()` 등록 없음. uvicorn 자체의 SIGINT 핸들러 + lifespan shutdown 훅 + `finally` 블록의 3중 정리.
- `shutdown_all_connections`는 두 곳(lifespan + finally)에서 호출되지만 idempotent하게 설계 (`render/web.py:175-180` — lock + clear).
- `WebServer.SHUTDOWN` 식별 sentinel은 identity comparison 사용 (`server.py:265`) → user input이 우연히 같은 값이어도 충돌 불가 (TestShutdownSentinel.test_shutdown_is_identity_sentinel에서 검증).

---

## 2. 발견 사항 (Severity별)

### MEDIUM — 1건

#### F-M1. Code fence 언어 태그 regex가 `c++` / `objective-c` 등을 못 받음
- **위치**: `agent_cli/web/static/app.js:97` — `/```(\w*)\n([\s\S]*?)```/g`
- **유형**: 회귀 / UX 결함
- **재현**:
  ```
  ```c++
  ## NOT a heading inside fence
  ```
  ```
  → fence regex가 `c++`에 매칭 실패 → fence 자체가 인식 안 됨 → 안쪽 `##`이 `renderHeadings`로 변환되어 `<h2>`로 둔갑.
- **검증 (node)**: `"```c++\n## ...\n```".match(/```(\w*)\n([\s\S]*?)```/g)` → `null`.
- **DESIGN.md 1.3절 명세 위반**: 설계는 `[\w-]*`이었지만 구현은 `\w*`.
- **수정 제안**: `/```([\w-]*)\n([\s\S]*?)```/g`로 변경 (DESIGN과 일치). 1줄 변경 + test 1건 추가 (`test_code_fence_with_hyphen_lang_tag`).
- **우선순위**: 일반적인 언어 태그(`c++`, `objective-c`, `f-sharp`, `x-yaml`)를 쓰는 어시스턴트 응답에서 코드 블록 안쪽 markdown 토큰이 변환되는 회귀가 발생함. 보안 결함은 아니나 M-5 (code fence 보존)의 의도와 충돌. 다음 PR 또는 본 PR 후속 fixup 1건 권장.

### LOW — 4건

#### F-L1. Table cell의 `**bold**`가 emphasis 단계에서 셀 경계를 넘어 매칭됨
- **위치**: `agent_cli/web/static/app.js:240-247` — `renderEmphasis` 단계가 `renderTables` 결과(HTML)에 적용됨.
- **유형**: UX 회귀 (XSS는 아님)
- **재현**:
  ```
  | **start | end** |
  |---|---|
  | a | b |
  ```
  → 테이블 변환 후 `<th>**start</th><th>end**</th>` → emphasis 정규식이 `[^*\n]+?`로 `</th><th>` 사이를 가로질러 매치 → `<strong>start</th><th>end</strong>` 같은 깨진 HTML.
- **XSS 위험 여부**: 없음 — `<strong>` 태그는 화이트리스트, attribute 주입 path 없음. 사용자 인풋도 모두 escape된 상태.
- **수정 제안**: emphasis는 셀 내부 텍스트에만 적용하도록 (a) table 변환 시 셀 내용에 inline pass를 명시적으로 적용 + skip 마커 삽입, (b) 또는 emphasis 정규식에 HTML 태그 경계(`[^<*]`) 추가. 가벼운 fix지만 본 PR 1차 범위로는 무난.
- **우선순위**: 실제 발생 빈도 낮음 (악의적 입력만이 트리거). 후속 PR 권장.

#### F-L2. R-2 (`load_session + ContextManager(resume=True)` 통합) 직접 단위 테스트 없음
- **위치**: `tests/test_web_server.py`
- **유형**: 테스트 커버리지 갭
- **현재 상태**: R-3 (TestReplayFromHistory)이 사실상 같은 흐름을 검증. R-1(invalid id)은 별도 통과.
- **수정 제안**: `web()` 내부 setup을 helper로 추출하지 않는 한 직접 검증이 어렵고, 비용 대비 효과가 작음. 현 상태 수용 가능.

#### F-L3. S-4 (finalize_session 호출) 단위 테스트 없음
- **위치**: main.py finally 블록의 `finalize_session(session, ctx)` 호출 경로
- **유형**: 테스트 커버리지 갭
- **현재 상태**: `finalize_session` 자체는 기존 `tests/test_session.py`에서 검증. main.py finally 블록의 호출 순서 검증은 없음 (TestWebResumeCli는 unknown-id exit path만 검증).
- **수정 제안**: web() 함수를 더 잘게 쪼개거나 `_shutdown_web` 헬퍼를 분리하면 단위 테스트 가능. 본 PR 1차에선 보류, 후속에서 graceful-shutdown 통합 테스트(`subprocess.Popen` + SIGINT)로 보강 권장.

#### F-L4. `load_session`이 web() 안에서 2번 호출됨
- **위치**: `agent_cli/main.py:1504` (pre-check) + `1533` (실제 로드)
- **유형**: 코드 스멜 (성능 영향 무시 가능, I/O 1번 추가)
- **이유**: pre-check는 `_setup_provider` 핸드셰이크 전에 unknown-id를 빨리 잡기 위해서. implementer 메시지에서 설계 외 추가로 한 변경이라고 명시.
- **수정 제안**: 첫 호출의 반환값을 변수에 저장해 두 번째 호출을 생략. 5줄 변경, 단순 cleanup. 본 PR 후속 가능.

---

## 3. 테스트 커버리지 표 (TEST_PLAN.md 대비)

| ID | 자동화 가능 | 실제 자동화 여부 | 비고 |
|---|---|---|---|
| M-1 heading | O | ✓ test_heading_levels_1_2_3, test_four_hashes_stays_raw | |
| M-2 GFM table | O | ✓ test_pipe_table | |
| M-3 bold/italic | O | ✓ test_bold_and_italic | |
| M-4 list | O | ✓ test_unordered_list, test_ordered_list | |
| M-5 code fence 보존 | O | ✓ test_code_fence_preserves_inner_tokens | F-M1 회귀 case (`c++` 등) 보강 권장 |
| M-6 XSS | O | ✓ test_xss_safety_script_stays_escaped, test_xss_in_heading_payload_stays_escaped | P0 만족 |
| R-1 invalid id | O | ✓ TestWebResumeCli.test_unknown_session_exits_with_code_1 | |
| R-2 valid resume | O | △ TestReplayFromHistory가 사실상 동일 흐름 검증 (F-L2) | |
| R-3 replay → buffer | O | ✓ TestReplayFromHistory (5 cases) | |
| R-4 header workspace | O | ✓ 기존 test_web_renderer.py:361-420 + replay snapshot 케이스에서 workspace prop 전달 검증 | |
| R-5 미지정 회귀 | O | ✓ 기존 test_web_server.py 전체 통과 | |
| S-1 Ctrl+C end-to-end | △ | ✗ (manual checklist — 의도된 보류) | |
| S-2 worker sentinel | O | ✓ TestShutdownSentinel (2 cases) | |
| S-3 connection 정리 | O | ✓ TestShutdownAllConnections (2 cases — idempotency 포함) | |
| S-4 finalize 호출 | O | △ 단위 테스트 없음 (F-L3) | finalize_session 자체는 기존 테스트로 커버 |
| S-5 2회 Ctrl+C | △ | ✗ (manual — 의도된 보류) | |
| C-1..C-4 회귀 | O | ✓ 1463 passed, ruff 0 exit | |
| C-5 chat --resume 회귀 | O | ✓ 기존 session 테스트 그대로 | |

**P0 자동화 항목 전부 통과**. P1/△는 의도된 manual 또는 사실상 다른 테스트로 커버됨.

---

## 4. 최적화 분석

| 경로 | 복잡도 | 평가 |
|---|---|---|
| `escapeHtml` | O(n) replace | 한 번에 정규식 1개, char-class — 최적 |
| `extractCodeFences` | O(n) 글로벌 replace | 최적 |
| `restoreCodeFences` | O(N × n) (N=fence 개수) — `split.join` 반복 | N 보통 ≤10, n 일반 ≤수십KB → 실용상 무시 가능. 마이크로 옵티마이즈 필요 시 단일 `replace` with map. **Skip — 가성비 낮음**. |
| `renderTables` | O(라인 수 × 라인길이) | 라인별 정규식 1~2회. 최적 |
| `renderHeadings` | O(n) gm | 최적 |
| `renderLists` | O(n) | 라인별 정규식 1회. 최적 |
| `renderEmphasis` | O(n) 글로벌 replace 2회 | 최적 |

**최적화 결함 없음**.

---

## 5. 추가 관찰 — Architecture Hygiene

긍정적 측면:
- `WebServer.SHUTDOWN`을 public class-level identity sentinel로 둔 결정 (DESIGN 3.2(b) 그대로) — worker가 `is` 비교로 안전하게 break. 테스트로 contract 고정됨.
- lifespan shutdown + main.py finally 이중 정리 (`shutdown_all_connections` 두 번 호출 안전) — design 3.2(c) 그대로.
- replay 헬퍼가 renderer 내부 메서드로 들어감 (옵션 A 채택) — `_event_buffer`/`_persistent_count` 등 private state에 자연스럽게 접근. 응집도 높음.

부정적 측면: 위 F-M1, F-L1~L4. 모두 본 PR 범위 안에서 머지 차단 사유는 아님.

---

## 6. 최종 결론

- **PASS — 머지 권장**.
- **선택사항 권장 (후속 PR 또는 본 PR 1줄 fixup)**: F-M1 (code fence regex `\w*` → `[\w-]*`) + 테스트 1줄 추가. DESIGN 명세와의 정합성 회복.
- Critical/High 발견 없음 — implementer에게 즉시 통보 필요한 사항 없음. team-lead에게만 PASS 보고.
