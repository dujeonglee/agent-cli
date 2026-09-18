# 대화 화면 통일 — 간단 명료 · 투명성

> 상태: **승인, 구현 중** (2026-09-18 사용자와 공동 설계)
> 시안: https://claude.ai/code/artifact/3baf98e1-7360-40e9-bfff-c3c395cb40b5
> 목표 키워드 둘 — **간단 명료**, **투명성**. 대화·셸 수행·reasoning 을 투명하게
> 보여주되 최대한 간단하게. Main/Agent 창을 같은 컨셉으로.

## 1. 지금 무엇이 문제인가

**같은 대화를 세 가지로 렌더한다.**

| 뷰 | 코드 | 보여주는 것 |
|---|---|---|
| 개요 | `ov*` 함수 38개 | 질문 + 응답 + reasoning(접힘) — **도구 호출 없음** |
| 전문 (드로어) | `render*` 카드 | thought · action · observation **전부** |
| 흐름 | `team_*.js` 1073줄 | 에이전트 스윔레인 |

**투명성이 드로어 안에만 있다.** 기본 화면인 개요엔 도구 호출이 안 나오고,
슬래시 명령만 화이트리스트(`OV_SLASH_TOOLS`)로 예외 처리돼 있다. 원하는 것과
정확히 반대다.

**Main 과 Agent 는 데이터 모델부터 다르다.**

```
main   : ovEntries      ← user_message + stream_chunk + assistant_turn
agent  : ovChannels[k]  ← agent_msg (내부 작업까지 텍스트로 뭉쳐 나감)
```

그래서 **에이전트 창엔 생각도 도구도 안 보이고 최종 답만** 보인다.

## 2. 설계 원칙

**모든 줄은 같은 리듬.** `아이콘 · 종류 · 한 줄 요약 · 펼침표시` 4칸 그리드.

```
💭 생각   테스트 러너를 먼저 확인한다
⚡ shell  pytest tests/ -q                      ▸
✓ 결과    3826 passed, 27 skipped in 41.23s     ▸
```

**기본은 한 줄, 누르면 전문.** 투명성과 간결함이 충돌하는 지점의 답 —
다 보여주되 **깊이로** 보여준다. 펼칠 게 있는 줄에만 hover 시 `▸`.

**스트리밍 폐기, 생성 중 한 줄.** 사용자 결정(밀도 ②):

```
● 생성 중 · 1.2K tokens
● 생성 중 · 4.8K tokens · 💭 사고 2.1K
```

숫자가 오르면 살아 있다는 뜻이고, 사고 토큰이 따로 잡히면 러너웨이가 바로
보인다. 본문이 흐르지 않으므로 **카드가 자라며 화면이 튀지 않는다**.
v8.60.0 무진전 표시(`⏳ 응답 대기 중 2:30 / 10:00 · 시도 1/4`)와 같은 자리·
같은 문법으로 이어진다.

## 3. 세 층 — 무엇이 채널이고 무엇이 중첩인가

**갈림선은 "말을 걸 수 있는가"** 하나다.

| | skill | inline agent | 상주 agent |
|---|---|---|---|
| 호출 | `run_skill` | `agent` (1회성) | spawn → 채널 |
| 컨텍스트 | 같은 ctx | 새 ctx · 끝나면 버림 | 새 ctx · 유지 |
| 수명 | 턴 안 | 턴 안 | 세션 내내 |
| 말 걸 수 있나 | 아니오 | 아니오 | **예** |
| 표현 | **중첩 블록** | **중첩 블록** | **채널 칩** |

skill·inline agent 는 끝나면 사라지므로 칩을 줘도 누를 일이 없다. 들여쓰기 +
좌측 레일(보라=skill, amber=inline)로 그리고 머리줄로 접는다. 서버는 이미
`scope_start` 에 `kind`·`depth`·`agent` 를 실어 셋을 구분할 정보가 다 있다.

## 4. 내부 작업 vs 왕래 — 통일의 범위

**세로로 흐르는 것**(생각·도구·결과·답)은 main 이든 agent 든 구분할 이유가
없으니 **한 이벤트로 통일**한다. **가로로 건너는 것**(왕래)은 방향과 상대를
실을 자리가 필요하므로 **`agent_msg` 를 유지**한다.

| | 내부 작업 | 왕래 |
|---|---|---|
| 참여자 | 1 — 자기 자신 | 2 — 보내는 쪽 / 받는 쪽 |
| 나타나는 채널 | 1곳 | 2곳 (`→` 와 `←`) |
| 이벤트 | `assistant_turn` + `task_id` ← **통일** | `agent_msg` ← 유지 |

### 주의: 대칭인 것은 peer ↔ peer 뿐

조사로 확인한 사실(시안 초안이 이걸 대칭으로 잘못 그렸다):

```
main → agent : key=<agent> direction=in  author=main        → 에이전트 창에만
user → agent : key=<agent> direction=in  author=user:<닉>    → 에이전트 창에만
peer → peer  : key=<수신>  direction=in  + key=<발신> out    → 양쪽
agent → 회신  : key=<agent> direction=out to=<요청자>        → 에이전트 창에만
agent → 질문  : key=<agent> direction=question               → 에이전트 창에만
```

`agent_message(key=…)` 의 `key` 는 "어느 창에 넣을지"인데 **main 창이라는 게
없다**. main 쪽에서 위임은 `⚡ agent` **도구 호출**로 나타난다.

**통일의 이득**: 에이전트 내부 작업을 `assistant_turn` 으로 떼어내면 에이전트
창도 main 만큼 투명해지고, `agent_msg` 는 본래 역할(왕래)만 남아 단순해진다.
`_emit` 이 **이미 스코프 task_id 를 자동 부착**하므로 배관은 대부분 있다.

## 5. 왕래 표현과 점프

세로선 + 화살표 + 상대:

```
← 방금 커밋 리뷰해줘                    main
← README 갱신해줘                      두정 (사람)
← 전송 세대 설명도 넣어주세요            reviewer (peer)
→ 폴백에 테스트가 없습니다               main
? 제가 추가할까요, 지적만 할까요?         두정 (답변 대기)
```

`author` 네임스페이스가 이미 셋을 구분한다 — `main` / `user:<닉>` /
`agent:<key>`. `(사람)` `(peer)` 꼬리표로 종류를 밝힌다.

### 점프 — 한 방향만

| 경로 | 가능 | 키 |
|---|---|---|
| main → agent | ✅ | `⚡ agent` 도구 호출이 `task_id` 를 이미 가짐 |
| peer ↔ peer | ✅ | `(author, to, seq)` — TeamView 중복제거 키 |
| agent → main | ❌ | main 쪽 대응 줄이 도구 호출이라 매칭 키가 다름 |

억지로 양방향을 맞추려면 도구 호출에 짝 키를 심어야 하는데 **이번 범위 밖**.
돌아가기 버튼으로 충분하다.

**돌아가기는 한 단계만.** 점프는 네비게이션이 아니라 **엿보기**라 새 점프가
이전 것을 덮고 한 번 누르면 사라진다. 스택이면 누를 때마다 라벨이 바뀌어
어디로 갈지 예측이 안 되는데, 채널 칩이 항상 보이므로 그 복잡도를 살 이유가
없다(사용자 지적으로 스택→단일 변경). 상태는 값 하나: `back = {c, a} | null`.

## 6. 질문 트레이 — 살린다

에이전트 질문은 대화에도 남지만 **어느 채널을 보고 있든** 입력창 위 트레이에
뜬다. docs 를 보는 중 reviewer 가 물어도 놓치지 않는다. 이미 있는 동작
(`ovAskTray` / `waiting_ask`)이라 그대로 살린다. 답하면 그 채널 대화에
`← <닉>` 으로 남아 누가 답했는지가 기록된다. 선착순.

## 7. 버릴 것 · 살릴 것

| 대상 | 처분 | 이유 |
|---|---|---|
| 흐름 뷰 `team_*.js` 1073줄 | **버림** | 채널 칩이 대체. 에이전트 1개면 값을 못 함 |
| 개요/전문 분리 `setBaseView`·`setDrawer`·`syncTabs` | **버림** | 뷰가 둘일 이유 없음. 드로어 마진 계산도 함께 |
| 스트리밍 `stream_chunk`·`card-streaming`·`stream_reset` | **버림** | 생성 중 한 줄로 대체 |
| 개요 액션 `⧉ 복사`·`▤ 전체 대화` | **버림** | 점프할 곳이 없어짐. 복사는 hover |
| 카드 렌더러 `renderAssistantTurn`·`renderObservation` | **살림** | 투명성이 여기 있음 — **기본 뷰로 승격** |
| 접기/펼치기 `card-collapse` | **살림** | 투명성과 간결함의 충돌을 푸는 지점 |
| 채널 칩 | **확장** | main 도 채널로 |
| 질문 트레이 | **살림** | 채널 무관 표면 |
| 노브 헤더 🗜️👥⏳🧠🔌 | **살림** | 최근 정리분, 손대지 않음 |

목표: JS **4745 → 2000줄 안팎**. SSE 리스너 33개 중 스트리밍 계열 4개 제거.

## 7.5. ①단계 후 발견 — ⑤는 서버 쪽에서 이미 되어 있다

②를 준비하며 확인한 사실. **계획을 줄인다.**

`begin_agent_work` 는 상주 에이전트 작업에 `task_id = "<key>#<seq>"` 를 붙여
**`scope_start`(kind="run")** 를 방출하고, 같은 스레드의 후속 이벤트는
`_emit` 이 그 `task_id` 를 자동 부착한다. 프론트는 `ensureTaskGroup` 으로
`#messages` 에 **중첩 카드**를 이미 만든다.

즉 **에이전트의 내부 작업(생각·도구·결과)은 이미 `assistant_turn` +
`task_id` 로 흐르고 전문 드로어에 그려지고 있다.** 안 보였던 이유는 그게
드로어 안에 있고 기본 화면인 개요가 안 그렸기 때문이다 — 데이터가 없어서가
아니었다.

따라서:

- **⑤(서버 이벤트 통일)는 사실상 불필요**하다. 남는 일은 프론트가 그 카드를
  "채널"로 묶어 보여주는 것뿐이고, 그건 ②와 ⑥에 흡수된다.
- `agent_msg` 가 중복으로 나르던 **최종 답 텍스트**만 정리 대상이다
  (왕래 in/out/question 은 §4 대로 유지).
- **②의 위험이 크게 준다**: 개요를 지워도 에이전트 작업이 사라지지 않는다.
  오히려 **드러난다** — `#messages` 에 이미 다 있기 때문이다.

이 발견으로 6단계가 **5단계**가 된다(⑤ 삭제, 잔여는 ⑥에 병합).

## 8. 구현 계획

규모가 커서 **6 커밋**으로 나눈다. 각 단계는 그 자체로 CI 그린이어야 한다.

### ① 흐름 뷰 제거 (순수 삭제)
- `team_view.js`·`team_model.js` 삭제, `index.html` 참조·`#team-view` 제거
- `TeamView.ingest` 호출부 제거 (app.js `agent_msg`·`scope_*` 핸들러)
- **TC**: `tests/browser/test_team_swimlane.py`(883줄)·`tests/test_team_model.py`
  (942줄) 삭제. 흐름 탭 관련 `test_header_and_stall` 케이스 정리.
- 제품 동작: 흐름 탭이 사라지고 개요/전문은 그대로 — **회귀 표면 최소**

### ② 뷰 통합 (개요 폐기, 카드를 기본으로)
- `setBaseView`·`setDrawer`·`syncTabs`·드로어 마진·뷰 탭 3개 제거
- `#messages` 를 기본 표면으로 승격, `#overview` 및 `ov*` 38함수 제거
- `ovOnSlashOutput` 화이트리스트 제거 — 모든 observation 이 그냥 보임
- **TC**: `test_overview_activity.py`·`test_overview_slash.py` 삭제,
  `test_app_markdown.py::TestOverviewFlatRender`·`TestOverviewActivityStrip` 삭제.
  **신규**: 기본 화면에 도구 호출이 보이는지(종전 개요의 구멍) 브라우저 TC

### ③ 카드 → 한 줄 리듬 + 접기
- `renderAssistantTurn`/`renderObservation` 을 4칸 그리드 행으로 재작성
- 기본 접힘, 펼칠 게 있는 줄만 `▸`
- **TC 신규**: 행 구조(아이콘·종류·요약), 접기/펼치기, 긴 출력 ellipsis,
  펼칠 것 없는 줄엔 `▸` 없음

### ④ 스트리밍 → 생성 중 한 줄
- `stream_chunk`·`stream_end`·`stream_reset` 리스너와 `card-streaming`
  머신(`ensureStreamingCard`·`updateStreamingCard`·`finalizeStreamingAsFailed`) 제거
- `token_usage`·`thinking_tick` 으로 `● 생성 중 · N tokens` 갱신
- 서버: `stream_chunk` 방출은 CLI 가 쓰므로 **남기고** web 렌더러만 무시
  (또는 web 에서 emit 생략 — 트래픽 절감; 결정 필요)
- **TC**: 스트리밍 브라우저 TC 정리. **신규**: 토큰 수 갱신, 턴 종료 시 사라짐,
  무진전 표시와 같은 자리 사용

### ⑤ ~~Main/Agent 통일 (서버 이벤트)~~ — **불필요, §7.5 참조**
서버는 이미 에이전트 내부 작업을 `assistant_turn`+`task_id` 로 방출하고
프론트도 `#messages` 에 중첩 카드로 그린다. 남는 일(채널로 묶기)은 ②·⑥에
흡수. `agent_msg` 의 중복 최종답 정리만 ⑥에서 함께.

### ⑥ 점프 + 중첩 블록
- 왕래 줄의 상대 이름 클릭 → 채널 전환 + `scrollIntoView` + 1.6s 하이라이트
- 돌아가기 단일 단계
- `scope_start` 의 `kind`/`depth` 로 skill/inline 중첩 블록 렌더
- **TC 신규**: main→agent 점프, peer↔peer 점프, agent→main 은 비활성,
  돌아가기 1단계(두 번 점프해도 스택 안 쌓임), 중첩 접기

## 9. Dead code 정리 원칙

각 단계에서 **"이제 아무도 안 쓰는 것"을 같이 지운다** — 나중에 몰아서 하면
무엇이 죽었는지 판정이 어려워진다. 매 단계 확인:

- CSS: 삭제한 JS 가 쓰던 클래스 (`.ov-*`, `.tm-*`, `.card-streaming` …)
- `index.html`: 사라진 컨테이너·탭·버튼
- `render/web.py`: 아무도 안 듣는 이벤트 방출
- 테스트: 사라진 동작을 검사하던 TC — **반전시키지 말고 삭제**하되,
  그 동작이 "이제 없어야 한다"가 계약이면 가드 TC 로 남긴다

## 10. 회귀 방지

- 각 단계 후 `pytest tests/` + `tests/browser/` 전부 그린
- 실장: 실제 `agent-cli web` 을 띄워 브라우저로 확인 — 유닛/브라우저 TC 가
  못 잡는 층이 있다(v9.1.0~9.3.0 에서 매번 실장이 잡았다)
- 각 단계는 **그 자체로 릴리스 가능**해야 한다(중간 상태로 CI 가 깨지지 않게)

## 11. 열린 질문

- ~~**`stream_chunk` 서버 방출**~~ · ~~**토큰 수 출처**~~ — **④에서 해소**:
  `WebRenderer.stream_chunk` 가 텍스트 대신 **0.5s 스로틀 tick**(`stream_tick`,
  누적 토큰만)을 낸다. `thinking_chunk`/`thinking_tick` 과 같은 모양이라 둘을
  `_tick(kind, text)` 하나로 합쳤다. 트래픽이 토큰 수에 비례하던 것이 상수가
  되고, `token_usage`(턴 종료 후 도착)를 기다릴 필요도 없다. CLI 는
  `MinimalRenderer.stream_chunk` 라 영향 없음(마르퀴 유지).
- **접힘 기본값**: 실패한 도구 결과는 펼친 채로 둘지.
