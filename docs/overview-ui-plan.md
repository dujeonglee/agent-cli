# 관측 UI 재설계 — 구현 계획 (승인됨)

상태: **구현 중** · 대상: agent-cli web 프런트 (team_view·app.js·index.html·css) · **presentation-only**

## 원칙
- **기존 로직·데이터·서버·SSE 스키마 무변경.** 모든 표현은 기존 이벤트 재배치:
  `agent_roster · agent_msg · scope_start/end · assistant_turn · user_message · stream_chunk · queue`.
- 확정 결정: **스킬 진행 = 상태만(숫자 step 없음)** · 큐 전달상태 = 클라 추론(queue diff + user_message (conn_id,text) 매칭) · 에이전트 done = roster idle.
- 매 단계 **회귀**: `pytest tests/`(현 3361) green + `ruff` + `node --check app.js` + 브라우저 라이브 검증.

## 최종 틀 (사인오프 아티팩트 요약)
- **4단 LOD + 레벨 컨트롤**: 개요(GLANCE·기본) / 흐름(FLOW·스윔레인) / 상세(DETAIL·선택→고정 패널) / 전문(DEEP·타임라인+🔍).
- **개요 화면**: ambient 한 줄(스킬 상태·에이전트 dot·ctx·viewers·model) + **응답 블록 시퀀스**(각 블록 = [누적 쿼리] → [응답 hero, 라이브 스트리밍]) + 다음 턴 대기 큐 + 대화창(모드 배지).
- **응답 블록**: 누적 쿼리 = 직전 final 이후 user_message + final.answers. 최신 블록 = 주인공, 이전 = dim.
- **큐 수명주기**: ⏳대기 → ✓주입됨(내 것=반영됨) → **다음 응답 블록의 요청으로 승격**(사라지지 않음). 취소 = ✕(승격 안 됨).
- **상호작용**: hover=peek / click=선택→고정 패널 / expand=전체.

## 단계 (각 단계 후 회귀 + 라이브 검증 + 보고)
1. [ ] **레벨 컨트롤 + 뷰 모드**: `개요·흐름·전문` 세그먼트. 흐름=기존 #team-view(무변경), 전문=기존 timeline drawer, 개요=신규 #overview 컨테이너. 스윔레인 바 클릭 시 전문으로. (기본은 흐름 유지 → 개요 완성 후 기본 전환)
2. [ ] **개요 화면 본체**: ambient 스트립 + 응답 블록(누적 쿼리 → hero) + **hero 라이브 스트리밍**(stream_chunk main scope) + 대화창(모드 배지). 완성 후 기본 뷰=개요.
3. [ ] **다음 턴 대기 큐 + 전달 상태**: 큐 스트립(⏳[nick], 내 것 ✕) + ✓주입/승격/✕취소, 승격 시 새 응답 블록 형성.
4. [x] **선택→고정 상세 패널(Tier 2)**: 흐름/개요에서 요소 클릭 → 고정 패널(메시지 전체·스팬·관찰). 툴팁/드로어/인스펙터 통합.
5. [x] **FLOW 밀도**: 상단 클리핑(이미 v8.3.0 PAD_T=34 로 해결 — 측정상 클리핑 0) · 밀도는 이벤트-서수축의 의도된 동작으로 현상 유지(사용자 결정). ×N 클러스터는 코어 모델 변경+gated 브라우저 테스트라 보류.

## 테스트 전략 (프런트 회귀)
- 서버 StaticUI 테스트(test_web_server): 서빙되는 index.html/app.js/css에 신규 요소·wiring·CSS 토큰 규칙 present 단언.
- test_app_markdown 노드 하네스: 순수 헬퍼는 첫 IIFE 내 유지(‘})();’ 주의). 뷰 로직은 라이브 검증.
- 매 단계 전체 스위트 green + ruff + node --check. 실제 브라우저(192.168.0.44 인스턴스)로 동작 확인.

## 범위 밖 (명시)
오케스트레이션/에이전트 로직 · SSE 스키마 · 서버 · 프롬프트/컨텍스트 엔진 — 불변. 웹 프런트 표현 한정.

## 진행 로그
- **단계 1 완료** (코드+유닛+회귀): 레벨 컨트롤 3세그(개요·흐름·전문) + `#overview` 스캐폴드 + `setViewMode()`.
  - index.html: `#vt-overview`/`#vt-flow`/`#vt-detail-toggle`(id 유지) + `#overview`(hidden).
  - app.js: `setViewMode(mode)` — 흐름=team-view, 전문=drawer(기존 동작 wrap), 개요=overview. 스윔레인/독 클릭 → 전문. 기본=흐름(개요 완성 전).
  - style.css: `.vt-tab[aria-selected]` 활성 + `#overview`.
  - 테스트: `test_web_server::TestStaticUI::test_level_control_wired` 신규. 전체 3362 passed, ruff, node --check OK.
  - ⚠️ 라이브 브라우저 검증 보류 — 검증 시점 보드 서버 다운(ERR_CONNECTION_REFUSED). 보드 복구 후 확인 예정. (미커밋 — 사용자 리뷰 전)
- **단계 2 완료** (코드+유닛+회귀+라이브): 개요 화면 본체 + hero 라이브 스트리밍.
  - app.js: 개요 렌더 모듈(ovRender/ovBlockHtml/ovAmbient) + 이벤트 훅(user_message→누적쿼리,
    stream_chunk(main)→hero 스트리밍, assistant_turn(main final)→블록 확정, agent_roster→dot,
    token_usage→ctx%, scope_start/end(skill)→ambient). setViewMode 가 dock 을 개요 모드에서 숨김.
    기본 뷰 = 개요로 전환. 쿼리 본문 "[닉]:" 중복 접두 제거.
  - style.css: `.ov-*` 컴포넌트(ambient/dot/block/hero/qb/caret/pulse) 전부 토큰 — 하드 hex 0.
    `body.mode-overview #dock{display:none}`.
  - 회귀: 전체 3362 passed, ruff, node --check OK. StaticUI 테마-토큰(no-hex) 통과.
  - **라이브 검증**: 기본=개요 · 응답 블록(누적쿼리→hero) · 실제 메인 턴 스트리밍
    (sawGen=true·caret=true·본문 208자 성장·✓완료) · dock 숨김 · ctx% ambient. (미커밋 — 리뷰 전)
- **단계 3 완료** (코드+유닛+회귀+라이브): 다음 턴 대기 큐 전달 상태.
  - app.js: 큐 핸들러에 수명주기 — pending diff 로 큐를 떠난 항목을 `qLeaving` 로 잡아
    `⏳ → ✓ 주입됨`(내 것=✓ 내 요청 반영됨) / `✕ 취소됨` 영수증(1.8s 뒤 사라짐). 주입 판정은
    `qOnUserMsg`(user_message 재방출 매칭, 1.4s 타임아웃=취소), 내가 ✕=즉시 취소(qCancelledByMe).
    승격은 주입 메시지가 user_message→ovOnUserMsg 로 다음 블록 요청이 되며 자동.
  - style.css: `.queue-item.q-injected`(ok 토큰)·`.q-cancelled`(회색·취소선)·`.queue-state`.
  - 테스트: `test_queue_delivery_state_wired` 신규. 전체 3363 passed, ruff, node --check OK.
  - **라이브 검증**: busy 중 B 큐잉(⏳) · ✓ 주입됨(내 요청 반영됨) · ✕ 취소됨 · 승격. (미커밋)
- **hero 마크다운 렌더 추가** (코드+회귀+라이브): 사용자 요청 — hero 응답을 평문 대신 마크다운으로.
  - app.js `ovBlockHtml`: `status==='done'` → `escapeAndFormat()`(타임라인 `.final` 과 동일 렌더러:
    제목·목록·표·코드블록·강조·인라인코드), `status==='gen'`(스트리밍) → 평문 + caret(불완전 마크다운 방지).
  - style.css: `.ov-tx` 마크다운 요소 스타일(`pre.code`/`code`/`h1-3`/`ul,ol`/`table`) — 토큰만, 하드 hex 0.
  - 회귀: 전체 3363 passed, ruff, node --check OK. **라이브**: hero 16.2k자 답변이 제목14·코드슬랩15·
    인라인코드38·GFM표·목록·볼드로 렌더, `<br>` 아티팩트 없음. (미커밋 — 리뷰 전)
  - ⚠️ **관찰(사전존재, 이 변경과 무관)**: 닫힌 `#timeline-drawer`(position:absolute·translateX off-canvas)의
    폭넓은 `.card pre.code` 코드가 문서 scrollWidth 를 늘려 페이지 가로 스크롤 발생. 조상에 overflow-x:hidden 없음.
    `.ov-tx` 스코프 밖 → 이번 마크다운 변경의 회귀 아님. 단계 5(클리핑)에서 처리 후보 or 1줄 즉시 수정 가능(사용자 판단).
- **단계 4 완료** (코드+유닛+회귀+라이브): 선택→고정 상세 패널(Tier 2).
  - index.html: `#detail-panel`(dp-tag/title/meta/body/timeline/inspect/close) — 드로어 옆 우측 레일.
  - app.js: `dpClassifyCard`(export classify 미러) + `dpPinCard`(흐름 카드 해석→추출) + `dpPinOverview`
    (개요 블록 hero HTML 재사용) + `dpFill`/`dpClose`. hover=툴팁 유지, **click=이 패널 고정**
    (teamHost 클릭이 드로어 즉시 열기 대신 `dpPinCard`), 버튼=Tier-3 승격([▤ 전체 타임라인]→드로어+
    `expandAncestors`→`scrollTimelineTo`, [🔍]→`__openInspector`, task_id 있을 때만). `setViewMode` 가
    맥락 종속 패널을 뷰 전환 시 닫음. Esc 닫기. 본문은 카드의 이미-렌더(escapeAndFormat) HTML 재사용
    (스코프=task-body 첫 .final/.obs-body 초점, 메시지=.bubble/.final, 관찰=.obs-body).
  - style.css: `.detail-panel`(드로어보다 좁은 슬라이드인) + `.dp-*` + `.dp-body` 마크다운 — 전부 토큰.
  - 테스트: `test_detail_panel_wired`(StaticUI, 기본 스위트) 신규. 클릭 계약이 바뀐 브라우저 e2e 5개
    (`test_click_bar_*`·`test_click_navigates_to_top`·`test_clicking_nested_bar`·`test_reply_arrow_click`)를
    새 흐름(클릭→패널, `#dp-timeline`→드로어)으로 갱신. `test_swimlane_click_expands_ancestor_chain`은
    펼침→스크롤 순서 보장이 `#dp-timeline` 핸들러로 이동한 것에 맞춰 갱신. **전체 3364 passed**, ruff, node OK.
  - **라이브**: 흐름 바 클릭→패널 고정(드로어 닫힘 유지)·[전체 타임라인]→드로어·[🔍]→인스펙터·
    개요 헤더 클릭→응답 패널(마크다운)·Esc 닫기. ⚠️ 브라우저 e2e(playwright, gated)는 이 환경에서
    미실행 — 갱신만 함. 사용자 측 `AGENT_CLI_BROWSER_TESTS=1 pytest tests/browser/test_team_swimlane.py` 권장.
- **단계 5 종료** (측정+결정): 상단 클리핑 = 이미 해결(v8.3.0 PAD_T=34, 측정상 뷰박스 위 클리핑 0). 밀도 = 이벤트-서수축의 의도된 동작으로 **현상 유지**(사용자 결정 — 적응형 ROW_H/×N 클러스터 모두 보류; 후자는 코어 모델 변경+gated 브라우저 검증 필요). 코드 변경 없음.
- **죽은 컨트롤 수리** (사용자 지적): 개요 응답 블록의 `⧉ 복사`·`▤ 전체 대화` 가 핸들러 없는 span 이었음.
  - app.js: `.ov-act` 버튼화 + 위임 핸들러 — `ovCopyBlock`(클립보드; secure-context 아닌 LAN http 대비 execCommand 폴백 + "✓ 복사됨" 플래시) · `ovOpenTimeline`(드로어 열고 블록 nav_ts 카드로 점프). `ovOnFinal` 이 `navTs=d.ts` 캡처, `ovBlockHtml` 이 블록에 `data-nav-ts` 부여(개요 상세패널 [전체 타임라인] 도 정확 점프). id 기준 전수 감사 결과 다른 죽은 버튼 없음(refs=0 은 전부 CSS 레이아웃 래퍼).
  - style.css: `.ov-act`(+hover) — 토큰.
