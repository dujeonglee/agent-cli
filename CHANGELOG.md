# Changelog

이 프로젝트의 주요 변경 사항을 기록합니다. 형식은
[Keep a Changelog](https://keepachangelog.com/ko/1.1.0/)를 따르며,
버전은 [Semantic Versioning](https://semver.org/lang/ko/)을 따릅니다.

버전 증가 규칙 (이 프로젝트 기준):

- **MAJOR** — CLI 플래그/설정 스키마 호환 깨짐, 기본 wire format 전환 등 하위호환 파괴
- **MINOR** — 하위호환 기능 추가 (새 도구·CLI 옵션·wire format)
- **PATCH** — 버그 픽스·문서·내부 정리

## [Unreleased]

## [4.9.4] - 2026-06-26

### Changed

- **다운로드 "All (whole workspace)" 체크박스 → 루트 행 체크박스로 통합** — 별도
  `All` 컨트롤을 없애고 트리 루트 행(`📁 /`)에 **체크박스**를 붙였습니다. 루트 체크 =
  워크스페이스 전체 다운로드(체크하면 하위 행을 dim). 이제 "체크 = 그 하위 전체
  다운로드" 가 모든 행에서 일관되고(루트도 일반 폴더처럼), 업로드 대상 선택과 함께
  루트가 트리의 한 행으로 완전히 통일됩니다. 백엔드(`{all:true}`) 무변경.

## [4.9.3] - 2026-06-26

### Changed

- **업로드 대상 모델 통일: 루트를 트리 행으로** — 폴더 재클릭 토글 해제와 ✕ 버튼을
  둘 다 제거하고, 트리 맨 위에 **`📁 / (워크스페이스 루트)` 행**(기본 선택)을 추가.
  이제 루트도 다른 폴더와 똑같은 트리 행이라 "어느 행이든 클릭 = 그 폴더로 업로드"
  하나로 통일 — 루트로 돌아가려면 루트 행을 클릭. (재클릭 토글이 펼치기와 겹쳐
  헷갈리던 것 + 루트만 특별 케이스였던 것 동시 해소.) 최상위 항목은 루트 아래로
  들여쓰기(depth 1).

## [4.9.2] - 2026-06-25

### Fixed

- **업로드 후 파일 목록이 드로어에 계속 남던 것** — 전부 성공하면 `✓ N개 업로드
  완료` 짧은 확인만 보여주고 **2.5초 뒤 자동으로 지웁니다**. 실패 항목이 있을 때만
  목록을 남겨(어떤 게 안 올라갔는지 봐야 하니) 유지. 드로어 재오픈 시에도 클리어.

## [4.9.1] - 2026-06-25

### Fixed

- **업로드 대상 폴더를 선택 취소할 방법이 없던 것** — 트리에서 폴더를 클릭하면
  "⬆ 업로드 대상" 으로 지정되는데 루트로 되돌릴 길이 없었음. 두 가지 추가: 대상
  표시 옆 **✕ 버튼**(클릭 시 루트로 리셋), **같은 폴더 다시 클릭 시 토글 해제**.

## [4.9.0] - 2026-06-25

### Added

- **디렉토리(폴더) 업로드** — 📁 드로어에 **폴더를 드래그-드롭**하거나 `폴더` 버튼으로
  선택하면 **하위 구조를 보존**해 재귀 업로드합니다(중간 디렉토리는 서버가 자동 생성).
  - 백엔드 `POST /api/workspace/upload` 의 `name` 이 이제 대상 `path` 기준 **상대경로**
    (`mydir/sub/a.c`)를 허용 — 단일파일(`a.txt`)은 그대로. 보안: 세그먼트별
    `..`/빈/절대/백슬래시 거부 + `_safe_workspace_path` 로 resolve 후 워크스페이스
    하위 재검증(traversal 가드를 완화한 게 아니라 **경로 단위로 재구성**), 그 다음
    `parent.mkdir(parents=True)`.
  - 프런트: 드롭은 `DataTransferItem.webkitGetAsEntry()` 로 디렉토리 트리를 재귀 walk
    (entry 는 drop 이벤트 중 동기 캡처), 선택은 `webkitdirectory` 인풋의
    `webkitRelativePath` 사용. 둘 다 파일별 1요청으로 상대경로와 함께 업로드.
  - e2e 실측: `src` 폴더로 `mydir/sub/b.c` 등 중첩 생성 + `a/../../etc` traversal 거부.

## [4.8.0] - 2026-06-25

### Changed

- **워크스페이스 파일 UI 통합: 📥 download + 📎 upload → 📁 하나** — 헤더 아이콘이
  📁 하나로 합쳐지고(📤 export 는 별개 유지), 클릭하면 기존 다운로드 트리 드로어가
  열려 **다운로드와 업로드를 한 곳에서** 합니다. 다운로드는 그대로(체크박스 선택 →
  ⬇ zip, 전 브라우저·디렉토리). 업로드는 **드로어 안으로 드래그-드롭/선택** →
  업로드되며, 위치는 **트리에서 클릭한 폴더**(하이라이트 + "⬆ 업로드 대상" 표시,
  미선택 시 루트)로 갑니다. 업로드 후 트리 자동 갱신. 드래그-아웃 다운로드는
  브라우저 제약(Chromium·단일파일)으로 미채택 — zip 버튼이 보편 경로. 백엔드
  엔드포인트(tree/download/upload) 무변경, 프런트 통합(별도 upload 드로어·IIFE 제거).

## [4.7.1] - 2026-06-25

### Fixed

- **web edit_file 카드가 `(0 edits)` 로 표시되던 것** — edit_file 이 flat-native
  (한 op = `{path, op, pos, end?, lines?}`, `edits` 배열 없음)로 바뀌었는데
  프런트 액션 카드가 옛 `parsed.edits.length` 를 읽어 **항상 0** → `(0 edits)`.
  이제 flat 편집의 **op + 대상 ref** 를 표시(예 `app.py (replace 2#KT)`,
  범위는 `(delete 2#KT..5#AB)`). 레거시 `edits[]` 배치는 카운트로 폴백. CLI(minimal)
  은 영향 없음(편집 카운트 미표시). 회귀 가드: app.js 에 `editCount` 잔존 금지.

## [4.7.0] - 2026-06-24

### Added

- **워크스페이스 파일 업로드 (📎)** — web 헤더의 📎 버튼으로 드로어를 열어
  **파일을 드래그-드롭하거나 선택**하면 워크스페이스로 업로드됩니다(다운로드 📥 의
  대칭). 대상 디렉토리 입력칸(비우면 루트, `src` 등 하위), 파일별 업로드 결과 표시
  (✓ 경로·덮어쓰기 여부). `POST /api/workspace/upload?name=&path=` — body=raw 파일
  바이트(파일당 한 요청, **python-multipart 의존성 0**). WRITE 라 다운로드보다 가드
  강함: 파일명 경로성분 거부(traversal), 대상 경로 `_safe_workspace_path` 하위+기존
  디렉토리만, 파일당 `_MAX_UPLOAD_BYTES`=50MB 초과 413, 토큰 인증, 덮어쓰기 허용+보고.
  드로어 open/close·backdrop 은 다운로드 패턴 재사용, IIFE 격리.

## [4.6.2] - 2026-06-24

### Fixed

- **빌트인 에이전트/스킬이 wheel 에 번들 안 되던 것** — `[tool.setuptools.package-data]`
  가 `default_models.json` + `web/static/*` 만 선언해, **`.md` 인 빌트인 에이전트
  (reviewer/explorer)·스킬(create-agent/create-skill/create-team/plan)이 pip 설치
  wheel 에서 통째 누락**됐음. editable 설치(`pip install -e .`)는 소스 트리에서 직접
  읽어 안 드러났으나, **wheel 설치 사용자는 auto-review 의 reviewer 를 못 찾아 동작
  안 함**(+ 빌트인 스킬 전부 사라짐). package-data 에 `agents/builtin/**/*.md` +
  `skills/builtin/**/*.md`(중첩 reference 디렉토리 포함) 추가, build-system 을
  `setuptools>=62.3`(재귀 `**` glob 지원)으로 상향. wheel 검증: 빌트인 10개 .md 전부
  번들 확인. 회귀 가드(`tests/test_packaging.py`) — 디스크의 모든 빌트인 .md 가
  package-data 패턴에 커버되는지 검사(누락 시 fail).

## [4.6.1] - 2026-06-23

### Fixed

- **`web --host <ip>` 새 인스턴스가 이미 도는 서버와 같은 포트를 잡아 충돌하던 것**
  — 다른 인스턴스가 `0.0.0.0:8080`(예: `--resume` 세션)에 떠 있을 때 `--host <ip>`
  로 새 세션을 시작하면 `pick_port` 가 8080 을 **여전히 free 로 판정**(`SO_REUSEADDR`
  bind 프로브가 macOS/BSD 에서 특정-IP bind 를 wildcard 리스너와 공존 허용 →
  false-positive) → 두 서버가 같은 포트 경합. `_port_has_live_listener`(connect
  프로브)를 bind 프로브 **앞에** 추가 — 라이브 리스너가 응답하면 ephemeral 포트로
  fallback. connect 는 실제 클라이언트처럼 라이브 점유와 TIME_WAIT 잔재(재사용 가능)를
  구분하므로, 재시작 시 자기 포트 재확보(`SO_REUSEADDR`)는 그대로 유지. 실측: 0.0.0.0:8080
  점유 중 새 `--host <ip>` 세션이 이제 `<ip>:50048` 같은 ephemeral 로 정상 기동.

## [4.6.0] - 2026-06-22

### Fixed

- **auto-review 토글이 브라우저 간 동기화 안 되던 것** — 토글 *상태*(서버
  `_auto_review`)는 모든 브라우저에 공유돼 동작은 일관됐으나, *버튼 표시*는
  클라이언트 로컬이라 A 가 켜도 B 의 버튼은 `off` 로 남아 "내 화면은 off 인데
  리뷰가 돈다"는 혼란이 있었음. `set_auto_review` 가 이제 sticky 로 broadcast →
  모든 브라우저 버튼이 실시간 동기화 + 재접속/새로고침 시 snapshot 으로 복원.

### Changed (추상화)

- **Sticky-state 레지스트리 (`set_sticky` / `_sticky`)** — "단일 서버 값을 라이브
  브로드캐스트 + 새 connection snapshot 에 재생"하는 패턴이 4번 반복되던 것
  (`_latest_ready`/`_latest_worker_state`/`_latest_token_usage`/`_latest_queue`
  슬롯 + `register_connection` 의 반복 if)을 한 표면으로 통합. 멤버 5개(+auto_review).
  `position`('prepend'|'append')으로 snapshot 위치 보존(ready=prepend, 나머지
  append). 동작 동일 — snapshot 순서·latest-wins·재접속 복원 invariant 회귀
  테스트로 고정. 다음 동기화 상태 추가 = `set_sticky` 한 줄.

## [4.5.3] - 2026-06-21

### Fixed

- **auto-review 결과 카드가 resume 후 사라지던 것** — v4.5.2 의 verdict 카드는
  `renderer.observation`(SSE emit)만 호출해 **history.jsonl 에 안 남아** resume 시
  재생되지 않았음(라이브≠resume 불일치 — 코드베이스의 "live card == ctx == resume"
  invariant 위반). `record_review_observation(ctx, ...)` 추가 — loop 의 관찰 레코드
  형태(`{role:user, tool:"auto-review", success, content:"Observation: …"}`)로
  `ctx.add` 해 history 에 남기고, `_review_render` 가 emit + record 둘 다 호출.
  `replay_from_history` 가 이 레코드를 observation 카드로 재생 → **resume 후에도
  리뷰 통과/거절 결과가 보임**. ctx None(CLI/pre-session)은 no-op.

## [4.5.2] - 2026-06-21

### Fixed

- **auto-review verdict 가 메인 대화창에 안 보이던 것** — reviewer 의 판정
  (ACCEPT/REJECT)이 **delegate 그룹 카드 안에만** 있어, 특히 ACCEPT 면 메인 흐름에
  아무 흔적이 없어 "complete 후 그냥 끝난" 것처럼 보였음(실측: 5-자료구조 세션이
  1라운드 ACCEPT 했는데 메인엔 verdict 0). `run_auto_review` 에 render 콜백을 실제로
  배선해 각 라운드를 **메인 observation 카드**(`tool_name="auto-review"`, ✓/✗)로
  표시: 리뷰 시작·`✅ accepted`·`❌ changes requested + 피드백`. delegate 카드의
  상세 과정은 그대로, 결론만 메인에 추가.

## [4.5.1] - 2026-06-21

### Fixed

- **멀티-op 합본 관찰 라벨 run-length 압축** — 한 턴에 같은 도구를 여럿 쓰면 합본
  관찰의 tool 라벨이 `shell+write_file+write_file+…×12`(137자)로 길어져 터미널 줄을
  넘쳤음. 연속 동-도구를 `tool×N` 으로 압축(`shell+write_file×12`, 19자). origin
  (`_combined_tool_label`, loop)에서 고쳐 CLI·web 카드·history `tool` 필드·
  read_context 가 모두 일관되게 짧아짐. 순서 보존(비인접 반복은 별도 run).

## [4.5.0] - 2026-06-21

### Added

- **Auto-review (web 토글) — complete 후 리뷰 에이전트 자동 검증 (PR2)** — 헤더의
  `🔍 Review` 토글을 켜면, 최상위 chat 의 모델이 `complete` 한 직후 **reviewer
  빌트인 에이전트**(`agents/builtin/reviewer.md`)를 자동 delegate 해 작업이 원
  요청을 실제로 충족하는지 검증하고, **accept 할 때까지** 반복합니다(reject 면
  reviewer 피드백을 메인 에이전트에 주입 → 재작업 → 다시 complete → 재리뷰).
  - **모델 자발성에 의존 X** — 제거한 `ready_for_review`(자발 호출, 사용률 0)의
    대체. reviewer 는 독립 시각으로 파일을 실제로 읽고/빌드해 검증(요약 불신).
  - **loop 무변경** — reviewer 는 평범한 delegate, verdict 는 그 complete 결과에
    심긴 시그니처(`VERDICT: ACCEPT` / `VERDICT: REJECT\n<피드백>`)를 worker 가
    파싱(`agent_cli/review.py`). 파싱은 관대(case-insensitive·마지막 매치), 형식
    실패 시 기본 REJECT(+raw 피드백, 품질 우선).
  - **종료 = accept 또는 토글 off** (safety cap 없음 — 사용자가 토글로 제어). 매
    라운드 토글을 라이브로 재확인하므로 도중에 꺼도 즉시 멈춤. 토글 off 면 complete
    은 리뷰 없이 그대로 종료.
  - **재귀 없음**: `@agent`/`/skill` 로 시작한 작업·delegate/skill 내부 complete·
    reviewer 자신의 complete 에는 적용 안 됨(auto-review 는 worker chat 경로 1곳).
    reviewer 도 delegate 라 "리뷰어를 리뷰"하는 무한재귀가 구조적으로 차단.
  - web 전용(CLI 토글 UI 없음). `POST /api/auto_review {enabled}`.

## [4.4.0] - 2026-06-21

### Removed

- **`ready_for_review` 가상 도구 제거 (PR1)** — 모델이 complete 전에 자발적으로
  호출해 자기 작업을 검토하라는 도구였으나, **실전에서 사용률 사실상 0**(297레코드
  긴 세션에서 0회). 모델은 가치 있는 self-review 를 자발적으로 안 함(nudge·write/edit
  가이드와 같은 compliance 한계). 도구 표면(virtual.py)·loop 디스패치 분기·registry
  `_ALWAYS_INCLUDE`·3개 wire format 의 format-rules 예시·react payload-hoist·프롬프트
  문구·explorer 에이전트 참조를 모두 제거. **대체 = 후속 auto-review**(complete 후
  옵션 토글로 리뷰 에이전트를 자동 delegate, 모델 자발성에 의존 X — PR2).
  - 리뷰 컨텍스트 빌더(`_build_review_observation`/`_format_tool_calls_for_review`)는
    auto-review 가 재활용하므로 **보존**.
  - 옛 세션에 `ready_for_review` 관찰 레코드가 있어도 resume 시 일반 관찰로 렌더
    (크래시 없음). 도구 제거라 SemVer minor.

## [4.3.0] - 2026-06-21

### Changed

- **write_file 작은-overwrite 관찰: hashline echo → diff (nudge 와 통일)** —
  write_file 이 기존 파일을 덮어쓰는데 변경률 < 30%(= v4.2.0 nudge 와 **같은
  임계**)면, 관찰 echo 를 파일 전체 hashline 덤프 대신 **바뀐 줄만의 unified
  diff** 로 보여줍니다. 하나의 판정(`_small_overwrite_analysis`)이 nudge 텍스트와
  diff-vs-hashline echo 를 **함께** 몰아, 정확히 churn 케이스(작은 변경을 통째
  재작성)의 echo bloat 만 ~diff 크기로 축소. nudge 가 *"~N% 만 바뀜, edit_file
  써"* 라고 말하고 diff 가 **그 바뀐 줄을 정확히 보여줘** 교육적으로 페어링.
  - 신규 파일·진짜 전면 재작성(≥30%)은 **hashline echo 유지**(write→edit 직결
    보존; diff 가 ≈ 전체라 무용). 동일-내용 재작성(0%)은 빈 diff → 저장 헤더만.
  - **observation-side 라 mimicry 위험 0**(action 무변형), write 자체·history·
    디스크 모두 무손실. `format_diff`(edit_file 과 동일) 재사용.

## [4.2.0] - 2026-06-20

### Added

- **write_file 작은-overwrite 런타임 넛지 (B1)** — write_file 이 **기존 파일을
  덮어쓰는데 바뀐 줄이 30% 미만**(작은 변경)이면, 관찰 끝에 한 줄 넛지를 붙여
  다음엔 edit_file 을 쓰도록 유도: *"~N% of lines changed (M/total) … edit_file
  costs only the changed lines — re-writing re-sends every line into context
  each turn."* **쓰기는 정상 수행**(allow + nudge, block 아님)하고 넛지는 도구
  관찰에 실려 context 로 들어감(observation-side → mimicry 위험 0). 신규 파일·
  빈 파일·진짜 전면 재작성(≥30%)에는 넛지 없음. 판정(`_rewrite_nudge`)은 쓰기
  **직전** 옛 내용으로 difflib 변경률 계산(쓰기 후엔 옛 내용이 사라짐). 임계
  `_REWRITE_NUDGE_RATIO=0.30` (실측 churn: 작은 편집 2-3% vs 전면 100%+). 정적
  프롬프트 가이드([4.1.2])가 못 막는 실제 실수 순간에 맥락 피드백.

## [4.1.2] - 2026-06-20

### Changed (prompt)

- **edit vs write 가이드 승격 + context-패널티 프레이밍** — 기존 파일의 부분
  변경 시 write_file 통째 재작성 대신 edit_file 을 쓰라는 지침을, edit_file 인라인
  가이드의 "Constraints" 괄호 속에 묻혀있던 것에서 **독립 규칙으로 끌어올리고**,
  write_file 도구 description 도 같은 메시지로 갱신. 핵심은 **"너에게 손해" 프레이밍**:
  파일을 재작성하면 (write 본문 + hashline echo 로) 매 턴 모든 줄이 context 에
  두 번 재공급되어 추론 공간을 잠식하고, edit 은 바뀐 줄만 비용. (실측 동기:
  한 세션에서 250줄 파일을 9줄/7줄 바꾸려 통째 재작성한 케이스 — 이미 있던 지침을
  모델이 무시. 효과는 단발 task A/B 로는 미검증(천장 효과/조건 미재현) — 무해한
  개선으로 반영, 진짜 churn 차단은 후속 런타임 넛지에서.)

## [4.1.1] - 2026-06-20

### Changed (정리, 동작 동일)

- **C1**: `render/minimal._CHARS_PER_TOKEN = 4` 로컬 상수 제거 → 스트리밍 "~N
  tokens" 카운터가 `estimate_tokens`(chars/4 단일 출처)를 직접 사용. `4` 가 두
  곳에서 어긋날 footgun 제거. (render→context→manager→render 가 load-time 순환이라
  web.py 처럼 **lazy import** — top-level 불가.)
- **C2**: `_call_llm` 의 매직 비율 `* 0.8`(예방 압축 target·오버플로 복구 target
  두 곳)을 명명 상수 `_COMPACTION_TARGET_RATIO` 로. manager 의 `_COMPACTION_
  THRESHOLD_RATIO`(0.9=트리거)와 구분되는 "목표=가용분 80%" 의도 명확화.

## [4.1.0] - 2026-06-20

### Added

- **`TokenUsage.total_input_tokens` property** (B1) — `input_tokens +
  cache_creation_input_tokens + cache_read_input_tokens` = 실제 프롬프트 점유량.
  `input_tokens` 만은 Anthropic prompt cache 분을 **제외**하므로, "컨텍스트가
  얼마나 찼나"를 뜻하는 곳(budget reconcile·ctx% readout)은 이 property 를 써야
  함. omlx 등 캐시 없는 provider 는 cache=0 이라 `== input_tokens`. loop 의
  reconcile 인라인 합산을 이걸로 대체(동작 동일).

### Fixed

- **top-bar ctx% 가 Anthropic prompt-cache 적중 시 과소표시되던 것** (C3) —
  `_build_token_stats` 의 `"in"` 을 `usage.total_input_tokens`(캐시 포함 전체
  점유량)로 변경. 이전엔 bare `input_tokens`(캐시 제외)라 캐시 적중 세션에서
  ctx% 가 실제보다 낮게 표시됐음. `in_speed` 는 prefill 이 비캐시분만 처리하므로
  bare `input_tokens` 유지, `cache_read/write` 는 별도 내역. omlx(캐시 0)는 무영향.

### Changed

- **manager `_sum_message_tokens(messages)` 헬퍼** (B2) — `sum(_estimate_message_
  tokens(...))` 가 5곳에서 반복되던 것을 단일 표현으로 통합(resume 복원·compaction
  evict·force_fit). 동작 동일.

## [4.0.2] - 2026-06-20

### Removed (dead code)

- `context.overflow.check_preemptive_overflow` + `context.token_estimator.
  estimate_tokens_from_messages` (+ exports, `OVERFLOW_RESERVE_TOKENS` 상수, 테스트)
  — 프로덕션 호출 0(test-only). 호출-전 오버플로 가드는 `ContextManager.ensure_within`
  (깊은 `_estimate_message_tokens`)으로 대체됐고, 이 죽은 경로의 얕은
  `estimate_tokens_from_messages`(content 만 카운트)는 manager 의 깊은 추정과
  **불일치**했음 — 제거로 그 불일치도 소멸. `overflow.py` 는 이제 내부 의존성 0
  (순수 에러-문자열 패턴). 동작 변경 없음.

## [4.0.1] - 2026-06-19

### Removed (dead code)

- `input_history.make_prompt` (test-only, superseded by `read_rich_input`).
- `code_index.store.IndexStore.get` (no caller).
- `DelegateResult.files_read`/`files_modified` (never populated).
- `build_system_prompt[_sections]` 의 죽은 `session_id` 파라미터 (기능은 진작
  `session_dir` 로 대체됐고 본문 미사용; 호출부 전부 keyword 라 안전 제거).
- `_handle_ask` 의 중복 `get_renderer` import.

### Changed (dedup, no behavior change)

- `ModelCapabilities` → models.json entry 직렬화를 단일 `capabilities.caps_to_entry()`
  로 통합 (runtime-probe save 와 setup-wizard save 가 필드 목록을 공유 — drift 방지).
- `code_index` 의 반복 가드를 로컬 `_require()`/`_validate_kind()` 헬퍼로 통합
  (~10개 "X is required for mode" + 3개 kind 검증).

전부 내부 정리 — 동작/CLI/설정 변경 없음. 전체 테스트 통과. (Python-hook 서브시스템,
wire-format·code_index 언어별 self-contained 중복, latent seam 들은 의도적으로 보존.)

## [4.0.0] - 2026-06-19

코드 변경 없는 **버전 정정** 릴리스 — v3.14.0~v3.16.1 에 걸쳐 누적된 **하위호환
파괴를 SemVer MAJOR 로 공식화**합니다. 기능/동작은 v3.16.1 과 동일합니다.

### BREAKING

- **v3.14.0 이전 세션 중 spill 이 발생한 것은 resume 불가** — v3.14.0 에서 과대
  출력 spill 서브시스템을 제거하면서, 그 시절 세션의 history.jsonl 에 남은
  `content={"spill":true,"output":[...]}` (dict) 관찰 레코드를 더는 처리하지
  못합니다. 그런 세션을 `--resume` 하면 매 턴 `_convert_observation` 의 `join`
  에서 `TypeError: sequence item …: expected str instance, dict found` 로
  크래시합니다. **해결책**: 해당 옛 세션을 resume 하지 말고 새 세션으로 시작
  (`.agent-cli/sessions/<id>` 삭제). v3.14.0 이후 생성된 세션은 영향 없음.
  - 호환을 코드로 복구(레거시 dict 관용 가드)하는 대신 MAJOR 로 표기하기로 결정
    — on-disk/history 포맷 변경이 옛 세션 resume 를 깨면 MAJOR 라는 기준 적용.

## [3.16.1] - 2026-06-19

### Fixed

- **write/edit action_input 본문 elide revert (마커 mimicry → 파일 손상)** —
  v3.16.0 의 write_file/edit_file `render_action_input_for_context` override 를
  제거. 재공급분에서 자기 write 가 `<…elided…>` 마커로 보이자 **모델이 그 마커를
  파일 본문으로 모방(mimicry)** 해 write_file 이 100B 마커를 디스크에 저장(실측:
  avltree.h/redblacktree.h 가 마커로 손상; 모델은 `shell` heredoc 으로 우회해
  겨우 복구). 본문은 다시 재공급 시 verbatim. **교훈**: 모델 자신의 출력(action)을
  가짜로 재공급하면 모방 위험 — 관찰(도구 결과) nudge 는 안전하나 action elide 는
  아님. `render_action_input_for_context` seam·`_context_view` 는 **identity 기본
  으로 유지**(미래용 latent, 동작 무영향). action_input bloat 는 미해결로 둠.

## [3.16.0] - 2026-06-19

### Changed

- **write_file/edit_file action_input 본문 재공급 elide (always)** — v3.15.0 에서
  깐 `render_action_input_for_context` seam 을 write_file(`content`)·edit_file
  (`lines`)이 override. 어시스턴트 turn 이 매 턴 LLM 에 재공급될 때 본문을 op
  모양 유지한 채 마커(`<N lines / NB written to PATH — read_file to view>`)로
  **항상 치환** — 파일은 디스크에 있고 관찰이 이미 쓰기를 확인했으므로 큰 본문을
  매 턴 재공급하는 건 context 낭비(추론 공간 잠식). `_context_view` 가 render +
  estimate 양쪽에 적용해 재공급=예산 카운트 일관. **history.jsonl 은 무손실**
  (seam 은 복사본에만 작용 — resume·read_context·audit 충실). observation echo
  트림(`render_observation` override)은 미포함(후속).

  ⚠️ 마커 mimicry(모델이 과거 턴의 `<elided>` 를 보고 본문 대신 마커를 emit)는
  유닛으로 못 잡으므로 라이브 omlx 세션 검증이 필요. 관측 시 fallback = "최근
  turn 본문 유지, 이전만 elide".

## [3.15.0] - 2026-06-19

### Added

- **도구별 action_input 컨텍스트-뷰 seam (`Tool.render_action_input_for_context`)**
  — `render_observation`(관찰 측)의 대칭(action 측): 어시스턴트 turn 이 매 턴
  LLM 에 **재공급**될 때 그 도구의 action_input 표현(기본 **identity**). 큰 본문
  인자(write_file `content`, edit_file `lines`)를 op 모양은 유지한 채 마커로
  elide 할 seam — 파일은 디스크에 있으니 본문을 매 턴 재공급할 필요가 없음. manager
  `_context_view(message)` 헬퍼가 **render(`_to_natural_language`)+estimate
  (`_estimate_message_tokens`) 양쪽**에서 consult(재공급=카운트 일관), 항상 복사본에만
  작용해 history.jsonl·cache 는 충실. **이번 커밋은 seam 만 깔며 기본 구현이
  identity 라 동작은 기존과 바이트 동일** — write/edit override + 라이브 mimicry
  검증은 후속 튜닝.

## [3.14.0] - 2026-06-19

### Changed

- **과대 도구 출력: 청크-spill → 거절+nudge (단순화)** — 도구 관찰(observation)이
  **컨텍스트 윈도우의 1/10**(`loop._oversized_cap`)을 넘으면, 이전엔 무손실 청크로
  history 에 보관하고 read_context `json_extract` 로 회수했었음. 이제 전체 출력을
  **어디에도 넣지 않고 "좁히라"는 nudge**(`_render_oversized_nudge`)로 거절합니다 —
  호출 자체는 성공. 거대 출력은 추론 공간을 잠식해 응답 품질을 떨어뜨리므로, 모델을
  라인범위/심볼/`LIMIT`/`grep`/`tee→read_file` 같은 surgical 회수로 자연스럽게
  유도합니다(전체가 꼭 필요하면 파일로 빼서 부분 조회). spill 의 보관-회수 기계
  (`_maybe_spill`/`_spill_view`/`_chunk_text_by_tokens`/`_build_spill_guide`,
  `content={"spill":...}` 레코드, read_context json_extract 회수)를 전부 제거 →
  `ctx.add` 는 순수 저장. 사용자/어시스턴트 메시지는 캡 대상 아님(의도적 입력·
  모델 자신의 출력).

- **read_context 결과 VERBATIM 반환 (트렁케이트 버그 수정)** — `_cell` 이 모든
  셀을 200자로 절단하고 `" ".join(s.split())` 로 개행·공백을 뭉개던 동작을 제거.
  이 때문에 spill 청크를 `json_extract` 로 회수해도 컨텍스트엔 200자로 잘리고
  개행이 파괴된 조각만 들어가던 버그가 있었음. 50행 cap(`_MAX_ROWS`)도 제거 —
  결과 크기는 위의 과대 출력 캡이 관장하고, 모델은 `LIMIT`/`substr` projection 으로
  작게 유지(스캔→전체 fetch 패턴). spill 전용 `content` 컬럼도 제거.

### Added

- **도구별 추상화 표면 2개 (`Tool`)** — `Tool.render_observation(result, args)`
  (도구 결과 → 관찰 본문 렌더, 기본=성공 `output`·실패 `error`)와
  `Tool.apply_oversized_cap: bool = True`(과대 출력 캡 적용 여부, 도구별 opt-out).
  loop `_tool_observation` 이 결과→관찰 seam 에서 둘 다 consult. 향후 write/edit
  echo 트림 등 도구별 튜닝의 진입점(현재 기본 구현은 종전 동작과 동일).

## [3.13.0] - 2026-06-19

### Added

- **Anthropic 모델 capability 런타임 추론** — 지금까진 openai 만 런타임 probe 가
  있고 anthropic 은 레지스트리/보수적 기본값(4096)에 의존했음. 이제 anthropic 도
  context window·thinking·structured-output 을 런타임 probe. probe 오케스트레이션을
  **공유 `_detect_capabilities(model, transport)`** 로 추출하고 **transport 만
  provider별**(OpenAI=`/chat/completions`·Bearer, Anthropic=`/messages`·
  `x-api-key`+`anthropic-version`·`content[].text`)로 분리 — 로직 중복 없음.
  Anthropic: context는 `/models` 메타(omlx) → `/messages` overflow probe → 128K
  폴백, structured 는 프롬프트-only JSON 검사(strict 항상 False), thinking 은
  `<think>` 태그 탐지. (`_detect_openai_capabilities` 는 thin wrapper 로 유지 —
  기존 동작/테스트 parity.)

### Added

- **setup 마법사가 Anthropic 도 모델 목록을 보여줌** — 지금까진 OpenAI 호환만
  `/v1/models` 로 목록·선택을 제공하고 Anthropic 은 수동 입력이었음. 이제 둘 다
  `/v1/models` 로 자동 탐색해 번호로 선택(실패 시 수동 입력 폴백). `_list_models`
  가 provider별 인증 헤더를 보냄(OpenAI=`Authorization: Bearer`, Anthropic=
  `x-api-key`+`anthropic-version`); 응답 `data[].id` 는 동형. omlx 가 두 API 를
  같은 모델로 서빙하고 실 Anthropic 도 GET /v1/models 를 지원해 양쪽 동작.
  (`_list_openai_models`→`_list_models`, `_select_openai_model`→
  `_select_model_from_list` 로 공유화.)

### Added

- **Prompt Inspector 가 첫 메시지 전에도 채워짐** — 지금까지 첫 LLM 호출이 있어야
  스냅샷이 잡혀 인스펙터가 비어 있었음:
  - **resume 즉시 대화 표시**: `/api/debug/prompt` 가 시스템 스냅샷이 없어도(메인
    스코프) ctx 에 복원된 메시지가 있으면 동적 섹션만이라도 반환(`ok=False` 게이트
    완화). resume 후 드로어를 열면 복원된 대화·관찰이 바로 보임.
  - **시작 즉시 시스템 프롬프트 표시**: web 시작 시 `capture_startup_system_prompt`
    가 정적 시스템 프롬프트를 미리 빌드·캡처(첫 메시지 전에 인스펙터 채움). `Hook:`
    동적 섹션은 첫 `PreLLMCall` 후 채워지고, 첫 LLM 호출이 스냅샷을 덮어씀.

### Added

- **Prompt Inspector 가 동적 컨텍스트(대화·관찰)도 표시** (Phase A, 읽기 전용) —
  지금까지 정적 시스템 프롬프트만 보여주던 인스펙터가, 그 아래
  `── 동적 컨텍스트 (대화 · 관찰) ──` 구분선과 함께 **현재 컨텍스트 윈도우에 든
  메시지**(`ctx.get_messages()` 의 system 제외분 — 대화·관찰·요약·파일목록)를
  메시지별 섹션으로 보여줍니다. LLM 이 실제로 받는 전체 입력을 검사 가능
  (메인 스코프 한정; 서브에이전트 스코프는 system-only). spill 레코드면 guide 표시.
  - 구현: `WebServer` 가 live `ctx` 를 받아 `GET /api/debug/prompt`(메인 스코프)에
    `_dynamic_context_sections(ctx)`(`kind="dynamic"`)를 덧붙임. **기존 sections
    파이프라인·프론트 아코디언을 그대로 재사용 — 새 추상화 0**(새 엔드포인트/렌더러
    메서드/콜백 없음, `kind` 필드 1개 + 순수 헬퍼 1개 + 프론트 구분선만).

## [3.9.3] - 2026-06-18

### Fixed

- **토큰 추정이 멀티-op(`ops`) assistant 레코드 내용을 안 세던 버그 수리** —
  `_estimate_message_tokens` 가 content/thought/top-level action_input 만 세고
  md_array(기본 포맷)의 `ops` 안에 든 action·action_input·complete result 를
  전부 누락했음 → **모든 assistant 턴이 thought 만 카운트**(예: write_file 의 큰
  content 인자나 긴 complete 결과가 예산 추정에서 통째로 빠짐). 결과적으로
  `ensure_within` 의 예방적 압축 판단이 과소추정돼 늦게 트리거(서버 reconcile/
  force_fit 로 사후 보정은 됐으나 부정확). 이제 `ops` 를 순회해 각 op 의
  action+action_input 을 카운트. (md_array 가 기본이 될 때 `_to_summary_text`·
  `_file_extract` 는 ops 처리로 고쳤으나 `_estimate_message_tokens` 는 누락됐던 것.)

### Fixed

- **세션 요약/리스팅이 spill 레코드에서 크래시하던 버그 수리** (`AttributeError:
  'dict' object has no attribute 'startswith'`). spill 레코드의 `content` 가 dict
  인데, raw history 를 읽는 일부 소비자가 여전히 문자열로 가정했음:
  `recent_exchanges`(resume 미리보기·`sessions` 목록 — 실제 크래시 지점),
  `_session_title`(read_context 세션 목록), delegate `_extract_last_actions`(서브
  에이전트 관찰 스크랩). 셋 다 `_spill_view` 로 guide 문자열을 보도록 수리. (이전
  라운드에서 manager 소비자·web replay 는 처리했으나 이 세 경로를 놓쳤음.)

### Fixed

- **spill 의 guide/청크가 긴 줄(1MB 단일-줄 파일)에서 윈도우를 초과하던 회귀 수리** —
  v3.9.0 의 spill 은 head preview(30줄)와 청크를 **라인 기준**으로 잘라서, 줄바꿈이
  거의 없는 출력(예: `read_file` hashline 으로 받은 1MB 단일 줄 파일)에서는 (a) guide
  head 가 그 한 줄을 통째로 포함해 **guide 자체가 262K 토큰**(윈도우 초과)이 되고
  (b) 청크 하나가 1MB(262K 토큰)가 되어, `tokens_after=0` 캐시 비움이 재발했음.
  이제 **청크·guide head 모두 문자(char) 기준**: `_chunk_text_by_tokens` 는 ~50K
  토큰(±200K자) 창에서 줄경계를 선호하되 **긴 줄은 하드분할**하고, guide head 는
  `_SPILL_HEAD_TOKENS`(500)로 하드캡. 실측: 1MB 단일-줄 → spill 레코드 추정 599토큰
  (이전 262K), 청크 6개 각 ≤50K, 무손실.

## [3.9.0] - 2026-06-18

### Fixed

- **단일 도구 출력이 컨텍스트 윈도우를 넘겨 압축을 깨뜨리던 버그 수리** — `find` /
  `code_index` 같은 한 번의 거대 출력(실측 800K 토큰)이 (a) 캐시 토큰을 윈도우
  이상으로 부풀리고, (b) 압축 evict 가 dynamic 전체를 먹어 `tokens_after=0`(캐시
  비움·최신 메시지 손실), (c) 요약 호출 자체를 윈도우 초과로 밀어넣던 문제.

### Added

- **과대 도구 출력 spill (무손실 청크 + 회수)** — 관찰 출력이 50K 토큰을 넘으면
  `ctx.add` 단일 지점에서 청크 분할해 `content = {"spill": true, "output":
  [guide, chunk1, ...]}` 로 저장. **컨텍스트엔 guide(output[0])만** 들어가고(토큰
  회계·LLM·요약·분류 모두 guide 기준) 전체 청크는 history.jsonl 에 **무손실 보존**.
  회수는 read_context 의 새 `content`(JSON) 컬럼 + `json_extract`:
  `SELECT json_extract(content,'$.output[N]') FROM history WHERE turn=T`.
- 어떤 단일 메시지도 윈도우를 넘지 않으므로 압축 evict·요약 호출이 항상 윈도우 안.

### Changed

- **관찰 렌더를 단일 지점(`_append_observation`)으로 통합 — "저장한 것을 보여준다"**.
  관찰 카드는 이제 ctx 에 저장된(spill 적용) 값으로 렌더되어 라이브·재접속·resume
  이 일관(거대 출력은 guide 카드). 부수적으로 multi-op 턴의 관찰 *결과*는 turn 끝에
  합본 1카드로 표시(action 카드는 op별 라이브 유지) — resume 이 이미 보여주던 모습과
  동일. 회복(recovery) 경로는 `render_recovery` 가 이미 렌더하므로 중복 안 함.

### Changed

- **compaction 요약 프롬프트를 agentic-resume 지향으로 정교화** — 기존 4-clause
  (intent/actions/decisions/outcomes) 대신 **구조화 섹션**(TASK / STATE / DONE /
  PENDING / DECISIONS / FAILURES / FACTS, 빈 섹션 생략)을 요청. 에이전트가 요약만
  으로 작업을 이어가야 하므로 **남은 작업(PENDING)·실패한 시도(FAILURES)·verbatim
  식별자(FACTS: 경로/명령/에러 문자열)** 보존 + "transcript 에 있는 것만, 지어내지
  말 것" 규칙을 추가. 재귀 병합은 "same section headings" 로 구조 유지.
  실세션 검증(Qwen3.6-27B): 구조 준수 + 실제 `AttributeError` 실패를 verbatim
  포착, 6.4K→2.3K자 압축.

## [3.7.1] - 2026-06-17

### Fixed

- **카드 시각이 본문 글씨와 겹치던 문제 수리** — absolute 배치된 모서리 시각이
  전폭 텍스트(assistant thought/final, error, user bubble)의 첫 줄 위에
  올라타던 것을, 해당 카드 상단에 거터(padding-top)를 확보해 본문이 시각
  아래에서 시작하도록 조정. (v3.7.0 CSS 회귀.)

## [3.7.0] - 2026-06-17

### Added

- **웹 카드에 시각 표시** — 각 대화 카드(user/assistant/observation/error)
  모서리에 발생 시각을 `YYMMDD HH:MM:SS` 로 표시(마우스 hover 시 전체 날짜+밀리초
  tooltip). `_emit` 단일 fan-out 지점에서 모든 이벤트에 server-stamp `ts` 를
  부착하므로 **delegate/skill 내부 카드까지 자동 커버**.
  - **resume 시각 보존**: `--resume` 로 재생되는 카드는 `replay_from_history` 가
    history record 의 원본 `ts` 를 통과시켜 **실제 발생 시각**으로 표시(재접속
    시점이 아님). history `ts`(ISO)·live `ts`(epoch) 둘 다 프론트가 수용,
    레거시 pre-ts 기록은 wall-clock fallback.

## [3.6.0] - 2026-06-17

### Added

- **웹에서 컨텍스트 압축(compaction) 가시화** — 두 군데로 노출:
  - **대화창 인라인 시스템 라인**: 압축 진행이 `⊙ 컨텍스트 압축 중… → 압축됨
    X→Y tok`(실패 시 warning) 으로 대화 타임라인에 표시. 전용 `compaction`
    SSE 이벤트(구조화 payload)를 프론트가 렌더(`.card-sys`). 이전엔 백엔드가
    generic `status` SSE 를 쐈으나 프론트 리스너가 없어 웹에선 안 보였음.
  - **Prompt Inspector**: 압축으로 흡수된 요약·파일 목록을 `⊙ Compaction
    summary / Files touched (user-injected)` 섹션으로 표시. 이들은
    `get_messages()` 가 시스템 프롬프트 직후 `role=user` 로 주입하는 내용이라
    `self.system` 엔 없지만 컨텍스트를 점유하므로 검사 가능하게 노출.

### Changed

- `render_compaction_progress` 가 `_renderer.compaction(phase, ...)` 으로 위임 —
  base 기본 구현은 `status` 한 줄(CLI 무변경), `WebRenderer` 는 전용 SSE 이벤트로
  override. (포맷 텍스트는 base 로 이동, CLI 출력 바이트 동일.)

## [3.5.1] - 2026-06-17

### Fixed

- **`--no-compaction` 플래그가 delegate/skill 서브에이전트에 전파되도록 수리** —
  부모 `AgentLoop` 의 `compaction_enabled` 가 `tool_delegate`/`_run_single`/
  `_run_parallel`(delegate)과 `_handle_run_skill`→`execute_skill`(skill)의
  `run_loop` 호출까지 스레딩됨. 이전엔 `AGENT_CLI_COMPACTION=off`(env)는 각 loop 의
  `_compaction_enabled()` per-loop 체크로 전파되었지만 `--no-compaction`(CLI 플래그)는
  main loop 만 끄고 서브에이전트는 여전히 압축하는 비대칭이 있었음. 이제 양쪽 일관.

## [3.5.0] - 2026-06-17

### Changed

- **`ask` 도구 flat-native 화 + 비-terminal 배치** — op 안의 `questions:[...]`
  배치 배열 대신 **`{action:"ask", question:"하나"}` 단수**(flat-native 불변식의
  마지막 holdout 제거). 여러 질문은 **ask op 여러 개로 배치**(read_file 처럼).
  또한 `ask` 가 더 이상 턴을 끝내지 않고 — 사용자 응답을 observation 으로 내고
  일반 도구처럼 accumulate — 여러 ask 가 순차 프롬프트 후 합성 observation 1개로
  묶입니다. 단일 질문 동작은 무변경, legacy `questions[]` 도 계속 관용(하위 안전).

## [3.4.2] - 2026-06-17

### Fixed

- **NO_JSON 회복 — 문자열 따옴표 하나 누락(앞/뒤) 일반화 수리** — `"path": mgt.c"`
  (여는 따옴표 누락)·`"path": "mgt.c}`(닫는 따옴표 누락)처럼 따옴표 한쪽이 빠진
  경우를 파서 에러-위치 가이드로 복구(`repair_value_quotes`). bail-if-invalid
  (재파싱 valid 일 때만 채택), bare `true`/`42`·EOF-truncation 은 안 건드림,
  미닫힘 `]` 와 합성. 한 페이로드의 여러 누락도 처리.
- **`## Thought` 헤더 누락 보정 (md_array)** — 모델이 `## Thought` 헤더 없이
  reasoning 을 내고 `## Action` 을 뒤따르게 하면, 앞 prose 가 drop 되던 것을 이제
  thought 로 회수(오타 헤더도 구제). 정상/헤더없는-answer/Action-맨앞 경로 무변경.

## [3.4.1] - 2026-06-17

### Fixed

- **sqlite 없는 환경에서 read_context 가 앱 기동을 깨뜨리던 버그** — `read_context`
  가 `import sqlite3` 를 모듈 최상단에 둬, stdlib `sqlite3` 확장이 없는 Python
  (locked-down/custom 빌드)에서 `No module named '_sqlite3'` 로 **코어 도구
  로드 실패 → 앱 기동 불가**. 이제 `code_index._sqlite` shim(stdlib→`pysqlite3`
  폴백)을 lazy 로 거쳐 — code_index 가 도는 환경이면 read_context 도 동작하고,
  sqlite 가 정말 없으면 쿼리 시 친절한 에러(크래시 아님). authorizer 도
  pysqlite3 가 상수를 안 노출하면 skip(안전은 SELECT prefix + ephemeral DB).

## [3.4.0] - 2026-06-17

### Changed

- **`read_context` 를 SQL 질의로 전환** — 필터 파라미터(`mode`/`kind`/`tool`/
  `author`/`turn`) 더미 대신 **단일 `query`(SQL SELECT)** 프리미티브. history
  를 인메모리 `history` 테이블(컬럼 `session/loc/seq/kind/turn/ts/tools/files/
  author/text`)로 적재하고 LLM이 SELECT 작성. 읽기전용(SELECT/READ 외 거부),
  결과 50행 cap, `query` 생략 시 스키마+예시+세션 목록. (도구 입력 스키마 변경 —
  프롬프트-구동이라 모델이 노출된 스키마로 적응.) `mode=list/search/fetch` 제거,
  context.py 736→362 LOC.

### Added

- **`files` 검색 컬럼** — 각 history 레코드에 **툴이 조작한 파일 경로**를 enrich
  (`extract_file_paths` 재사용). "auth.py 를 건드린 레코드 전부" 같은 조회 가능
  (`... WHERE files LIKE '%auth.py%'`).

## [3.3.0] - 2026-06-17

### Added

- **`read_context` 구조화 JSON 쿼리** — 세션 이력 검색이 키워드 substring +
  필드 필터(`kind`/`tool`/`author`/`turn`)를 자유 조합하는 구조화 쿼리로
  확장. `kind`(query/action/observation/final/raw), `tool`(툴명), `author`
  (웹 멀티유저 닉네임), `turn`(int 또는 {from,to} 범위)로 "정말 필요한
  레코드만" 회상. 구 `scope`(reasoning/tool/observation/query) 폐기.
- **history.jsonl 검색 enrich** — 각 레코드에 `kind`/`turn`/`ts`/`tools`/
  `text`(+`author`) 검색 키를 가산 기록(round-trip/LLM 경로는 무변경). 외부
  `jq` 로도 구조화 조회 가능. (검색 분류 단일 출처 `_classify_record` — 구
  tool-scope 가 ops-모양 액션을 놓치던 버그도 해소.)

## [3.2.0] - 2026-06-16

### Added

- **웹 큐 메시지 단일 라우팅** — 실행 중 큐에 넣은 메시지도 run 시작 메시지와
  **동일하게 라우팅**됩니다. 이제 중간에 보낸 `/sh`·`/compact`·`@agent`·`/skill`
  이 리터럴 chat 텍스트로 새지 않고 **실제로 실행**됩니다(이전엔 run 시작 시에만
  동작). `@agent` 중간 주입은 모델의 `delegate` 와 동일한 경로로 수렴. `/sh`·
  `/help` 은 종전처럼 display-only, `/compact`·`@agent`·`/skill` 은 컨텍스트에
  반영. 평문 메시지는 기존대로 스티어링 주입.

### Changed

- (내부) 사용자 메시지 intake 통합 — run-starter/injected 의 라벨링·라우팅
  중복 제거(`_add_user_message` 단일 헬퍼, `query_label`→`query_author`). 동작
  하위호환(CLI 무변경). 설계 `docs/intake-unification/DESIGN.md`.

## [3.1.1] - 2026-06-16

### Fixed

- **웹 세션 resume 시 assistant 카드 누락** — `replay_from_history` 가 단수
  `{action}` 모양만 디코드해, 두 wire format 이 실제로 저장하는 `ops` 모양
  assistant 턴(`complete` 최종답 포함)이 전부 렌더 누락되던 버그 수정. 이제
  `ops` 분기로 op마다 thought+action(complete=final) 재방출, 레거시 단수 모양·
  raw content-only(final 카드)도 호환.

## [3.1.0] - 2026-06-16

### Added

- **Jira Server/DC 의 평문 `http://` URL 지원** — 웹 UI 에서 사용자가 직접
  입력한(=config 미등록) base_url 에 `http://` 도 허용(기존 `https` 전용 →
  `http`/`https`). 사내 평문 HTTP Jira 를 대상으로 쓸 수 있습니다. 평문 자격증명
  전송 위험은 차단이 아니라 입력 시 UI 경고로 표시하며, `http`/`https` 외 scheme
  은 거부합니다. config 에 등록된 URL 의 내부 http 허용은 종전과 동일.
- **닉네임 중간 변경** — 웹 로스터 옆 ✎ 버튼으로 접속 후에도 닉네임을 언제든
  재설정(현재 닉 prefill → 즉시 로스터 반영). 첫 연결 닉네임 바를 재사용하며
  서버는 ephemeral 유지(영속화 없음).

## [3.0.0] - 2026-06-15

### Changed

- **Jira 코멘트를 프론트엔드 사용자 본인 명의로 게시** — 코멘트 작성자가 백엔드
  config 계정이 아니라, 웹 UI 에서 자격증명을 입력한 그 사용자가 됩니다.
  - **(호환 깨짐)** config `jira.instances` 에서 `email`/`api_token` 제거 —
    이제 `base_url`(+ 선택 `deployment`)만 둡니다. 자격증명은 서버에 저장하지
    않고 사용자가 웹 UI 에서 입력(브라우저 localStorage 기억, POST 한 번에만
    transient 사용).
  - **Jira Cloud + Server/Data Center 모두 지원** — `{base_url}/rest/api/2/
    serverInfo` 프로브로 deployment 자동 판별(또는 config 명시/UI 토글). Cloud=
    `/rest/api/3`+ADF, Server/DC=`/rest/api/2`+wiki 마크업으로 코멘트 본문 전송.
  - **config 선택화(zero-config)** — config 없이도 웹 UI 에서 base_url 을 직접
    입력해 게시 가능. config 인스턴스는 드롭다운+URL prefill 로 제공. config 에
    없는 사용자 입력 URL 은 `https://` 만 허용(서버가 미검증 호스트로 자격증명을
    보내므로 TLS 강제; config 등록 URL 은 내부 http 허용).

## [2.1.0] - 2026-06-14

### Added

- **`agent-cli update`** — GitHub 최신 릴리스 확인 후 업데이트(`gh` + `pip`).
  `--check`(확인만)·`-y`(확인 생략)·`--force`(dev 설치 강행). private repo
  인증은 `gh` 로그인이 처리(토큰 불필요), 릴리스 첨부 wheel 을 설치.
- **웹 워크스페이스 다운로드(📥)** — 우측 드로어의 lazy 파일 트리에서 파일/
  디렉토리를 골라 zip 다운로드(디렉토리=재귀, All=전체). 파일·디렉토리 크기 표시.
- **웹 멀티유저** — 접속자 수·닉네임 로스터(`👁 N · …`), 접속 시 닉네임 입력
  (재미있는 기본값 20개 풀에서 배정, localStorage 기억), 사용자 메시지 큐:
  실행 중 보낸 메시지가 큐에 쌓여 실시간 표시되고 매 턴 종료 시 하나씩 대화에
  주입(steering), 자기 큐 메시지 취소 가능. 모든 사용자 요청은 `[닉네임]:`
  라벨로 LLM 에 노출 + task 로그 누적.

### Changed

- **웹 제어 모델 단순화** — controller/observer 권한 시스템(권한 요청/승인)
  제거, 모든 연결이 동등하게 입력·큐 가능. `role` 이벤트 → `identity`.

### Fixed

- `pysqlite3-binary` 의존성 marker 를 x86_64 Linux 로 한정 — **arm64 Linux 에서
  agent-cli 설치 불가** 버그 수정(arm64 wheel 부재).
- `complete` 턴 history 직렬화를 포맷 동질 모양(`ops`)으로 — 단수 `{action}`
  으로 새던 불일치 수정.
- `read_context {mode:list}` 크래시(제거된 `SessionMeta.query` 참조) 수정 —
  세션 제목을 history 첫 메시지에서 유도.
- 웹 다운로드 All 후 재오픈 시 트리 비활성 잔존 수정.

### Bench (제품 외)

- `bench/swebench/` — SWE-bench 어댑터(호스트 인퍼런스 A + 컨테이너 인-에이전트
  B, 네이티브 arm64), report. django-10914 검증, B5-django resolved 4/5.

## [2.0.0] - 2026-06-14

첫 공개 릴리스. on-premise LLM을 위한 ReAct 패턴 에이전트 CLI.

### Added

**에이전트 루프**
- ReAct(Reason–Act) 루프 — Thought → Action → Observation. 단일 실행(`agent-cli run`)과 대화형 웹 UI(`agent-cli web`) 지원.
- 멀티 프로바이더 — OpenAI 호환(OpenAI, vLLM, omlx, LM Studio 등)과 Anthropic.
- `agent-cli --version` / `-V` 플래그.

**Wire format (모델 응답 형식 추상화)**
- 플러그인형 wire format 시스템. 두 멀티-op 포맷 내장: `md_array`(기본, 마크다운 envelope + flat op JSON 배열)와 `react`(JSON 쌍둥이).
- 전 builtin 도구 flat-native — `{action, ...params}` (wire-key prefix·batch 배열 제거). prefix 머신러리는 미래 prefixed 도구용 latent seam으로 보존.
- 한 턴에 여러 독립 op 디스패치. `delegate`는 연속 op를 병렬 실행(`parallel_safe`).

**Robust recovery 하니스**
- 3단계 파싱 폴백 + 형식 실패 회복(NO_JSON / NO_ACTION / NO_THOUGHT / UNKNOWN_TOOL / SCHEMA_MISMATCH / ACTION_LOOP / DEGENERATE 등 라벨링).
- failure-grounding 재시도(모델 자기 출력 echo), 액션 루프 감지, degeneration 조기 중단.
- JSON 구문 진단 — 파싱 실패 시 line/column + 캐럿으로 *어디가* 깨졌는지 모델에 제시.
- 미닫힘 op 배열 EOF 닫기 수리(문자열-인식, bail-if-invalid).
- 세션별 관측성 로그(`turns.jsonl`).

**도구**
- `read_file`(심볼/라인 범위/배치), `write_file`, `edit_file`(hashline), `shell`, `code_index`(tree-sitter 기반 심볼 인덱싱), `delegate`(서브에이전트, 병렬), `ask`, `complete`, `run_skill`, `ready_for_review`.
- MCP 도구 어댑터.

**컨텍스트 관리**
- token-budget 기반 LLM 요약 compaction + FIFO 폴백. 세션 저장·resume.

**확장성**
- 스킬 시스템(내장: create-skill, create-agent, plan, create-team) + 사용자/프로젝트 스킬.
- 에이전트 정의(내장: explorer) + 사용자/프로젝트 에이전트.
- 플러그인 렌더러 시스템.
- 라이프사이클 훅(PreLLMCall, PostLLMCall, PreToolUse, PostToolUse).

**웹 UI** (`agent-cli web`, `pip install agent-cli[web]`)
- FastAPI + SSE 기반 LAN UI, 스트리밍·중단·resume.
- 대화 내보내기(📤) — HTML 파일 또는 Jira 코멘트(다중 인스턴스).
- Prompt Inspector(⚡) — 시스템 프롬프트 디버그 드로어.

**배포**
- 순수 파이썬 패키지(`py3-none-any` wheel), Python 3.10+.
- on-prem 친화 — 의존성 최소화, locked-down 서버용 `pysqlite3-binary` 폴백(Linux).

[Unreleased]: https://github.com/dujeonglee/agent-cli/compare/v4.9.4...HEAD
[4.9.4]: https://github.com/dujeonglee/agent-cli/compare/v4.9.3...v4.9.4
[4.9.3]: https://github.com/dujeonglee/agent-cli/compare/v4.9.2...v4.9.3
[4.9.2]: https://github.com/dujeonglee/agent-cli/compare/v4.9.1...v4.9.2
[4.9.1]: https://github.com/dujeonglee/agent-cli/compare/v4.9.0...v4.9.1
[4.9.0]: https://github.com/dujeonglee/agent-cli/compare/v4.8.0...v4.9.0
[4.8.0]: https://github.com/dujeonglee/agent-cli/compare/v4.7.1...v4.8.0
[4.7.1]: https://github.com/dujeonglee/agent-cli/compare/v4.7.0...v4.7.1
[4.7.0]: https://github.com/dujeonglee/agent-cli/compare/v4.6.2...v4.7.0
[4.6.2]: https://github.com/dujeonglee/agent-cli/compare/v4.6.1...v4.6.2
[4.6.1]: https://github.com/dujeonglee/agent-cli/compare/v4.6.0...v4.6.1
[4.6.0]: https://github.com/dujeonglee/agent-cli/compare/v4.5.3...v4.6.0
[4.5.3]: https://github.com/dujeonglee/agent-cli/compare/v4.5.2...v4.5.3
[4.5.2]: https://github.com/dujeonglee/agent-cli/compare/v4.5.1...v4.5.2
[4.5.1]: https://github.com/dujeonglee/agent-cli/compare/v4.5.0...v4.5.1
[4.5.0]: https://github.com/dujeonglee/agent-cli/compare/v4.4.0...v4.5.0
[4.4.0]: https://github.com/dujeonglee/agent-cli/compare/v4.3.0...v4.4.0
[4.3.0]: https://github.com/dujeonglee/agent-cli/compare/v4.2.0...v4.3.0
[4.2.0]: https://github.com/dujeonglee/agent-cli/compare/v4.1.2...v4.2.0
[4.1.2]: https://github.com/dujeonglee/agent-cli/compare/v4.1.1...v4.1.2
[4.1.1]: https://github.com/dujeonglee/agent-cli/compare/v4.1.0...v4.1.1
[4.1.0]: https://github.com/dujeonglee/agent-cli/compare/v4.0.2...v4.1.0
[4.0.2]: https://github.com/dujeonglee/agent-cli/compare/v4.0.1...v4.0.2
[4.0.1]: https://github.com/dujeonglee/agent-cli/compare/v4.0.0...v4.0.1
[4.0.0]: https://github.com/dujeonglee/agent-cli/compare/v3.16.1...v4.0.0
[3.16.1]: https://github.com/dujeonglee/agent-cli/compare/v3.16.0...v3.16.1
[3.16.0]: https://github.com/dujeonglee/agent-cli/compare/v3.15.0...v3.16.0
[3.15.0]: https://github.com/dujeonglee/agent-cli/compare/v3.14.0...v3.15.0
[3.14.0]: https://github.com/dujeonglee/agent-cli/compare/v3.13.0...v3.14.0
[3.13.0]: https://github.com/dujeonglee/agent-cli/compare/v3.12.0...v3.13.0
[3.12.0]: https://github.com/dujeonglee/agent-cli/compare/v3.11.0...v3.12.0
[3.11.0]: https://github.com/dujeonglee/agent-cli/compare/v3.10.0...v3.11.0
[3.10.0]: https://github.com/dujeonglee/agent-cli/compare/v3.9.3...v3.10.0
[3.9.3]: https://github.com/dujeonglee/agent-cli/compare/v3.9.2...v3.9.3
[3.9.2]: https://github.com/dujeonglee/agent-cli/compare/v3.9.1...v3.9.2
[3.9.1]: https://github.com/dujeonglee/agent-cli/compare/v3.9.0...v3.9.1
[3.9.0]: https://github.com/dujeonglee/agent-cli/compare/v3.8.0...v3.9.0
[3.8.0]: https://github.com/dujeonglee/agent-cli/compare/v3.7.1...v3.8.0
[3.7.1]: https://github.com/dujeonglee/agent-cli/compare/v3.7.0...v3.7.1
[3.7.0]: https://github.com/dujeonglee/agent-cli/compare/v3.6.0...v3.7.0
[3.6.0]: https://github.com/dujeonglee/agent-cli/compare/v3.5.1...v3.6.0
[3.5.1]: https://github.com/dujeonglee/agent-cli/compare/v3.5.0...v3.5.1
[3.5.0]: https://github.com/dujeonglee/agent-cli/compare/v3.4.2...v3.5.0
[3.4.2]: https://github.com/dujeonglee/agent-cli/compare/v3.4.1...v3.4.2
[3.4.1]: https://github.com/dujeonglee/agent-cli/compare/v3.4.0...v3.4.1
[3.4.0]: https://github.com/dujeonglee/agent-cli/compare/v3.3.0...v3.4.0
[3.3.0]: https://github.com/dujeonglee/agent-cli/compare/v3.2.0...v3.3.0
[3.2.0]: https://github.com/dujeonglee/agent-cli/compare/v3.1.1...v3.2.0
[3.1.1]: https://github.com/dujeonglee/agent-cli/compare/v3.1.0...v3.1.1
[3.1.0]: https://github.com/dujeonglee/agent-cli/compare/v3.0.0...v3.1.0
[3.0.0]: https://github.com/dujeonglee/agent-cli/compare/v2.1.0...v3.0.0
[2.1.0]: https://github.com/dujeonglee/agent-cli/compare/v2.0.0...v2.1.0
[2.0.0]: https://github.com/dujeonglee/agent-cli/releases/tag/v2.0.0
