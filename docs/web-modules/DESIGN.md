# 웹 프런트를 ES 모듈로 (v10.116.0 — 1단계, v10.117.0 — 2단계 §8)

## 1. 왜 모듈인가

`agent_cli/web/static/app.js` 는 한 파일 5,730줄, IIFE 열한 개였다. 메인 IIFE 하나가
4,208줄이다. 2026-08 코드 리뷰(`docs/CODE-REVIEW-2026-08-22.md` §4.6)가 2,744줄일 때
이미 [높음] 두 건을 적었다.

- **메인 IIFE 의 크기**: 평평한 가변 상태 수십 개가 한 스코프에 있다. 그 뒤로 1.5배가 됐다.
- **테스트의 소스 긁기**: 테스트가 함수를 글자 위치로 잘라낸다. 종전 `_js_fn_body` 는
  "다음 `\n  function` 까지, 없으면 4,000자" 를 함수 본문으로 쳤다. `test_app_markdown`
  은 첫 IIFE 의 여닫이를 정규식으로 찾았는데, 주석 속 `})();` 에 걸려 우연히 맞던 적이
  있다(v9.4.0 ②).

해법은 빌드 없는 브라우저 네이티브 ES 모듈이다. 온프렘 배포라 번들러·npm 을 들이지
않는다. 모듈은 정적 파일 그대로 서빙되고, `import` 는 브라우저가 푼다.

## 2. 1단계 범위 (이 PR)

- 기능 IIFE 열 개를 `static/js/` 의 모듈 하나씩으로 옮긴다. 메인 IIFE 는 통째로
  `js/main.js` 가 된다. 메인 본문은 2칸 내어쓰기 말고는 한 글자도 바꾸지 않는다.
- `static/app.js` 는 진입 모듈이 된다. 하는 일은 적재 순서 하나다.
  보드·문서·테스트가 `static/app.js` 를 가리키므로 이름을 유지한다.
- `index.html` 은 `<script type="module" src="static/app.js">` 한 줄만 바뀐다.
- 행동 불변. 리팩터링·정리는 2단계 몫이다.

| 모듈 | 옛 IIFE |
|---|---|
| `main.js` | 메인 화면(SSE·카드·입력·타임라인·프레임) |
| `directives.js` | 📝 지시 편집기 |
| `workspace.js` | 📁 워크스페이스 서랍 |
| `theme.js` | 🎨 테마 메뉴 |
| `knobs_client.js` | `const KNOBS` 노브 클라이언트 (유일한 옛 전역) |
| `knob_chips.js` | 노브 칩 렌더러 |
| `mcp.js` | 🔌 MCP 상태 칩 |
| `knob_popup.js` | 노브 칩 팝업 토글 |
| `presence.js` | 탭 존재 비콘 |
| `schedule.js` | ⏰ 예약 |
| `web_sources.js` | 🌐 정보원 서랍 |

옛 파일엔 "Auto-review toggle (header button → separate IIFE)" 라는 머리 주석이 있었다.
그 밑엔 코드가 없었다(토글은 노브 칩으로 흡수됐다). 가리킬 대상이 없는 주석이라
옮기지 않았고, `auto_review.js` 도 없다.

## 3. 모듈의 모양 — 내어쓰기와 가드 뒤집기

모듈은 IIFE 본문을 2칸 내어쓴 것이다. `(function () {`·`"use strict";`·`})();` 는
뺀다. 모듈은 언제나 strict 이고 자기 스코프를 가지므로 IIFE 가 하던 일을 이미 한다.

**가드 뒤집기 — 조기 탈출 `return`**: 기능 IIFE 여덟 개가 top-level 에서 `return` 했다.
모듈 top-level 의 `return` 은 **문법 오류**라, 하나만 있어도 모듈 그래프 전체가 평가되지
않아 화면이 통째로 안 뜬다. 그래서 이 여덟 모듈은 IIFE 를 벗기고 가드의 조건을 뒤집어
나머지 본문을 블록으로 감쌌다. 꼴은 두 가지다.

- DOM 가드 일곱: `if (!$btn || !$drawer) return;` → `if ($btn && $drawer) { … }`.
- 기능 감지 하나(`presence.js`): `if (typeof BroadcastChannel === "undefined") return;` →
  `if (typeof BroadcastChannel !== "undefined") { … }`.
- 해당: `directives`·`workspace`·`theme`·`knob_chips`·`mcp`·`presence`·`schedule`·`web_sources`.
  나머지 `main`·`knobs_client`·`knob_popup` 은 그냥 내어썼다.

가드 앞의 DOM 상수 선언은 내어써 모듈 top-level 에 둔다. 모듈마다 스코프가 따로라
`$btn`·`$drawer` 같은 같은 이름이 모듈끼리 부딪치지 않는다. 블록 안 본문은 종전 IIFE
본문과 들여쓰기(2칸)가 같다. 그래서 옛 IIFE 와의 diff 는 가드 두 줄(`return` 줄 →
`if (…) {` 줄)과 닫는 괄호(`})();` → `}`)뿐이다.

- 등가성 증명은 가드 줄을 이렇게 다룬다: 옛 본문의 `if (C) return;` 줄을 `if (¬C) {` 로
  바꾸고 끝에 `}` 를 붙인 것과 새 모듈(머리 주석·`import` 제외)이 줄마다 바이트로 같다.
  `¬C` 는 스크립트가 옛 조건에서 글자로 만든다(드 모르간, `===` → `!==`).

`knobs_client.js` 는 IIFE 를 풀고 마지막 `return { all, set };` 을
`export const KNOBS = { all, set };` 으로 바꿨다. 공개 표면은 그대로 `all`·`set` 둘이다.
`knob_chips.js` 는 머리 주석 다음 첫 줄이 `import { KNOBS } from "./knobs_client.js";` 다.

## 4. 모듈 사이의 계약 — 전역은 이것뿐

- **`KNOBS`**: `knobs_client.js` 의 유일한 export. 쓰는 곳은 `knob_chips.js` 하나다.
- **CustomEvent 셋**: 메인이 SSE 를 받아 내보낸다.
  - `agentcli:knob` — `document` 에. `knob_chips.js` 가 듣는다.
  - `agentcli:directives-changed` — `window` 에. `directives.js` 가 듣는다.
  - `agentcli:schedules-changed` — `window` 에. `schedule.js` 가 듣는다.
- **`window.AGENTCLI_CONN_ID`**: 메인이 SSE 접속 id 를 적는다.
- **테스트 훅**: `window.__cardMarkdown`·`window.__timelineMarkdown`·`window.__ctxFrames`.
  브라우저 테스트(`tests/browser/`)가 읽는다.

모듈은 이 밖의 top-level 이름을 서로 쓰지 않는다. 옛 파일에서도 IIFE 끼리 닿는 길은
`KNOBS` 하나였다. `index.html` 에 인라인 핸들러(`onclick=`)는 없다.
`<head>` 의 인라인 테마 스크립트는 자기 IIFE 안에서 끝난다.

### 적재 순서가 의미 있는 이유

모듈은 진입 파일의 `import` 순서대로 평가된다(깊이 우선, 한 번씩). 각 모듈의
top-level 코드는 이벤트 리스너를 걸고 첫 fetch 를 낸다. 같은 대상에 건 리스너는 건
순서대로 불린다. 그래서 진입 파일은 옛 IIFE 순서를 그대로 지킨다.
`knobs_client` 는 `knob_chips` 의 import 로 먼저 평가되지만, top-level 에서 하는 일이
없다(fetch 는 `all()` 첫 호출 때). 그러니 순서 효과도 없다.

## 5. 테스트 helper 계약 — `tests/web_static.py`

소스를 글자로 읽는 테스트는 파일을 직접 열지 않고 이 모듈을 쓴다.

- `STATIC`: `agent_cli.web` 패키지의 `static/` 디렉터리.
- `module_order()`: 진입 파일의 `import "./js/<name>.js";` 줄을 순서대로 읽은 이름 목록.
  테스트가 실제 적재 순서를 안다.
- `js_text(name=None)`: 이름을 주면 그 모듈 원문을 준다. 안 주면 적재 순서로 이어붙인
  전체를 준다(파일 사이에 `\n`).
- `js_fn_body(js, name)`: 모듈 스코프 함수 `function NAME(` 의 정의를 **중괄호 짝
  맞추기**로 자른다. 모듈 스코프는 열 0, 그리고 가드 블록(§3) 안인
  열 2 다. 같은 이름이 두 번 이상 나오면 AssertionError 를 낸다.
  `open`·`close`·`render`·`load`·`el`·`apply`·`call` 은 여러 모듈에 있다. 그런 이름은
  `js_fn_body(js_text("schedule"), "load")` 처럼 모듈을 지정한다.

서빙 테스트는 따로 남는다. `/static/app.js` 와 `module_order()` 의 모든 모듈이 200 +
JS MIME 이어야 하고, `js/` 의 파일은 전부 적재 순서에 있어야 한다. 서빙된 `index.html`
엔 `type="module"` 이 있어야 하고, 없는 파일은 404 여야 한다.
v10.116.0 의 `test_app_markdown` 은 `js_text("main")` 을 `(function(){ … })()` 로 감싸 node 로
실행했다. v10.117.0 부터는 node 가 모듈 파일을 그대로 import 한다(§8.6).

## 6. 확인한 의미론 (스크립트 → 모듈)

1. **실행 시점**: 모듈은 `defer` 다. DOM 파싱이 끝난 뒤 실행된다. 옛 스크립트도 `<body>`
   끝에 있어 DOM 이 다 있었다. 같다.
2. **top-level 이름**: 모듈의 top-level `var`/`function` 은 전역이 되지 않는다. acorn 으로
   각 모듈의 자유 변수를 모아 다른 모듈의 top-level 선언과 대조했다. 겹침이 없다.
   옛 전역 `KNOBS` 는 import 로 바뀌었다. `KNOBS` 를 전역으로 읽던 곳(인라인 스크립트·
   브라우저 테스트)도 없다.
3. **top-level `this`**: 모듈에선 `undefined` 다. 모든 모듈에 `this` 사용이 하나도 없다.
4. **strict 전환**: 모듈은 언제나 strict 다. 옛 IIFE 중 다섯(`workspace`·`theme`·
   `KNOBS`·`knob_popup`·`presence`)은 `"use strict"` 가 없었다. 모든 모듈이 acorn 의
   모듈 모드로 파싱된다(strict 문법 위반 없음). 미선언 이름에 대입하는 곳도, `this` 도 없다.
5. **다리**: `window.__*`·`window.AGENTCLI_CONN_ID`·`CustomEvent` 는 `window`/`document`
   를 명시해 쓰므로 모듈 스코프와 무관하게 그대로 동작한다.
6. **자격 증명**: 모듈 fetch 는 CORS 모드지만 기본 `credentials` 가 `same-origin` 이라
   같은 출처 쿠키를 보낸다. 게다가 `/static` 은 인증 밖이다(`_is_public_path`). 무관하다.
7. **base-path**: 모듈 지정자 `./js/x.js` 는 **진입 모듈 URL 기준**으로 풀린다. 진입 URL
   이 `<base href>` 로 접두 밑에 오므로 모듈도 접두 밑에 온다. `test_web_base_path` 가
   모든 import 가 `./` 상대임과 접두 밑 서빙을 단정한다.
8. **MIME**: 브라우저는 JS MIME 이 아닌 모듈 스크립트를 거부한다.
   `_NoCacheStaticFiles`(Starlette `StaticFiles` + `mimetypes`)는 `.js` 를
   `text/javascript` 나 `application/javascript` 로 준다. 둘 다 허용됨을 테스트로 고정했다.

## 7. 2단계 계획 (v10.116.0 시점 — v10.117.0 에서 실행, §8)

`main.js` 를 절 단위 모듈로 쪼갠다. 후보는 `markdown`·`cards`·`frames`·`timeline`·
`input`·`sse`·`runblocks`·`queue`·`roster`·`util`·`state` 다.

- 공유 가변 상태는 `state.js` 에 모은다. 리뷰 §4.6 의 "평평한 가변 상태 30여 개" 다.
- 순수 함수(마크다운 파이프라인·에이전트 아이콘 해시·카드 마크다운)는 export 한다.
  node 가 `import()` 로 직접 실행하면 소스 긁기(`js_fn_body`·`_js_pool`·하니스 스텁)가
  사라진다.
- 그때 `tests/web_static.py` 의 긁기 helper 는 줄어들거나 없어진다. 서빙 테스트와
  `module_order()` 는 남는다.

## 8. 메인 화면의 분할 (v10.117.0)

`js/main.js`(4,210줄, 절 36개)를 절(`// ── … ──` 머리) 단위 모듈 18개로 나눴다. `main.js` 는
없어졌다. 진입 `app.js` 가 18개를 import 하고, 기능 모듈 열 개는 그 뒤에 종전 순서 그대로다.
코드 리뷰 §4.6 "프론트 ES 모듈 분할" 의 마지막 조각이다.

### 8.1 모듈 표

절 번호는 옛 `main.js` 의 `// ──` 순서다([0] 은 파일 머리 주석). 줄 수는 머리 주석·import 포함.

| 모듈 | 옛 절 | 줄 | 내용 |
|---|---|---|---|
| `boot.js` | [1] | 30 | 주소창 `?token=` 걷기·`showSetupHelp`. 첫 import |
| `dom.js` | [2]+[5] | 33 | 공유 DOM 참조 + `el`·`elHtml` |
| `state.js` | [3] 의 상태 넷 | 33 | `export const session = { mainAsk, connId, workerBusy, stopRequested }` |
| `markdown.js` | [4] + `imageUrl` | 309 | 이스케이프·마크다운·diff 색칠·그림 주소. 순수 모듈 |
| `scroll.js` | [7] | 156 | 따라가기 |
| `sse.js` | [24] | 108 | `export const es`·연결 표시·`copyToClipboard` |
| `copy.js` | [9] | 76 | 복사, 훅 `__cardMarkdown`·`__timelineMarkdown` |
| `attach.js` | [23] | 147 | 이미지 첨부 |
| `channels.js` | [28]+[30]+[31] | 442 | 채널·트레이 상태·필터·점프. **순환 진입점** |
| `timeline.js` | [6]+[8]+[26]+[27] | 429 | 카드 시각·delegate 그룹·`scope_*` 배선·타임라인 |
| `frames.js` | [10]+[11] | 625 | 모델 시점 + 틀, 훅 `__ctxFrames` |
| `progress.js` | [12]+[18] | 139 | stall 표시·생성 중 표시 |
| `thumbs.js` | [13]+[14] | 91 | 사용자 말풍선·썸네일 |
| `cards.js` | [15]+[16]+[17] | 590 | 한 줄 리듬·발신 도구·스텝 카드 |
| `input.js` | [3] 의 함수 둘 + [19]~[22]+[32] | 311 | Send/Stop·입력 모드·POST·confirm stall·입력 바인딩·abort |
| `share.js` | [25] | 276 | 🔗 공유 링크·`fmtTok`·대화 카드 SSE 핸들러 |
| `runs.js` | [29] | 329 | 런 블록 |
| `roster.js` | [33]+[34]+[35] | 261 | identity·뷰어 로스터·별명·대기열 |

합계 4,385줄이다. 옛 4,210줄보다 는 몫은 모듈 머리 주석(51줄)·import(107줄)·setter(3줄)·
`session` 객체 틀(2줄)이고, 빠진 몫은 옛 파일 머리 주석(4줄)이다.

브리프의 처음 표에서 바꾼 곳은 셋이다. 셋 다 실제 의존을 보고 정했다.

- **`imageUrl` → `markdown.js`**: 마크다운의 그림 렌더가 `imageUrl`([14])을 부른다.
  `markdown.js` 가 `thumbs.js` 를 import 하면 DOM 을 쓰는 모듈 그래프가 딸려 와 node 가
  순수하게 import 할 수 없다. 함수와 그 주석 13줄을 옮겼다.
- **`isBusyChat`·`updateSendEnabled` → `input.js`**: `updateSendEnabled` 가
  `ovApplyChannelInput`(runs)을 부른다. `state.js` 에 두면 state 가 순환에 들어가고 진입
  순서상 먼저 나와 순환의 진입점이 된다(§8.4). 두 함수는 Send/Stop 버튼 일이라 입력 바인딩과
  abort 가시성 곁으로 갔다. `state.js` 는 import 없는 잎이다.
- **[13] → `thumbs.js`**: [13] "Card renderers" 는 머리와 `renderUserMessage` 의 문서 주석뿐이다.
  그 함수는 [14] 에 있다(v10.60.0 에 썸네일 절이 사이에 끼었다).

### 8.2 세 규칙

1. **코드 불변**: 함수 본문·식은 글자 그대로 옮겼다. 바뀐 것은 (a) `import` 줄과 선언 앞
   `export `, (b) `session.x` 표기, (c) 바깥 대입 → setter 호출, (d) 모듈 머리 주석이다.
   주석도 옮기기만 했다 — `state.js` 의 주석은 옛 이름(``myConnId`` 등)으로 남아 있어,
   머리 주석에 옛 이름 → 새 이름 표를 적었다.
2. **export 는 쓰이는 것만**: 다른 모듈이 import 하는 이름만 export 한다. 예외는 node 하니스가
   import 하는 이름이다(`tests/test_app_markdown.py` 의 `HARNESS_IMPORTS` — markdown 의
   `extractCodeFences`·`renderHeadings`, cards 의 `obsSummary`, channels 의 `ovAgentIcon`·
   `ovAgentNickname`·`OV_AGENT_ICONS`·`OV_AGENT_ANIMALS`). 가드가 둘을 합쳐 센다.
   **테스트 전용 export**(다른 모듈은 쓰지 않고 node 하니스만 import 하는 이름):
   - `markdown.js`: `extractCodeFences`·`renderHeadings`
   - `cards.js`: `obsSummary`
   - `channels.js`: `ovAgentIcon`·`ovAgentNickname`·`OV_AGENT_ICONS`·`OV_AGENT_ANIMALS`
3. **`let`/`var` 는 정의 모듈만 대입한다**: import 한 바인딩은 읽기 전용이다(대입은 런타임
   TypeError). 바깥 대입이 있던 이름은 §8.3 의 두 방법으로 옮겼다.

export 집합은 acorn 스코프 해석으로 정했다. 측정 스크립트(`main-sections.py`)는 정규식이라
문자열·주석·속성 이름까지 셌다. 그 목록의 `loading`·`pinned`·`scrollToBottom` 은 바깥에서
쓰이지 않는다. `main-writes.py` 가 [4] 에서 본 `loading` 대입은 HTML 문자열
`loading="lazy"` 다. 그래서 `setLoading` 은 만들지 않았다(쓰이지 않는 export 가 된다).

### 8.3 `session` 객체와 setter — live binding 의 대입 제한

ES 모듈의 import 는 정의 모듈 변수의 **살아 있는 별칭**이다. 읽으면 늘 지금 값이 보이지만,
대입은 정의 모듈만 할 수 있다. 옛 파일에서 정의 절 밖에서 대입되던 `let`/`var` 는 여섯이었다.

- **여러 모듈이 대입하는 넷 → `session` 객체의 필드**: `ovMainAsk`([20]·[32] 가 대입) →
  `session.mainAsk`, `myConnId`([33]) → `session.connId`, `workerBusy`([32]) →
  `session.workerBusy`, `stopRequested`([22]·[32]) → `session.stopRequested`. 객체는
  `const` 이고 필드 대입은 어느 모듈에서나 된다. 읽기·쓰기 모두 `session.x` 다.
- **한 모듈이 주인인 둘 → setter**: `ovActiveChannel`·`ovBack` 은 channels 에 있고 runs 의
  `ovSyncChannels` 가 한 번씩 대입한다(채널이 roster 에서 사라지면 main 으로, 돌아갈 곳이
  사라지면 null). 기존 `ovSetChannel` 은 필터·틀·입력·스크롤까지 움직여 같은 일이 아니다.
  그래서 channels 에 `setOvActiveChannel(v)`·`setOvBack(v)` 한 줄씩을 두고 runs 는 그것을 부른다.
  "1줄 위임 금지" 는 같은 일을 하는 함수가 이미 있는데 포장만 더하는 것을 막는 규칙이다.
  모듈 경계의 setter 는 대입 권한을 넘기는 유일한 방법이라 그 규칙의 예외다.
- 나머지(`myNickname` 은 [33]·[34] 가 같은 roster 모듈, `loading` 은 바깥 대입 없음)는 그대로다.

### 8.4 순환 — 진입점이 평가 순서를 정한다

메인 화면은 함수 호출로 얽혀 있다. acorn 으로 절 단위 호출 그래프를 그리면 절 21개(약
3,300줄)가 하나의 강연결요소다. 예: `updateSendEnabled`[3] → `ovApplyChannelInput`[29] →
… → `workerBusy`[3]. 코드를 바꾸지 않고는 어떻게 묶어도 순환 없는 그래프가 되지 않는다.
그래서 **함수 호출 순환은 허용**한다. 함수 선언은 모듈 인스턴스화 때 초기화되므로, 순환
상대의 함수를 부르는 것은 평가 순서와 무관하게 안전하다.

위험은 **평가 시점 읽기**다. 모듈 top-level 코드가 아직 평가되지 않은 모듈의 `let`/`const`
를 읽으면 TDZ ReferenceError 이고, `var` 면 조용히 `undefined` 다. acorn 으로 top-level 문이
(호출을 따라 전이적으로) 읽는 바인딩을 셌다. IIFE 와 `forEach` 콜백은 그 자리에서 도는 코드로 쳤다.

- 순환 밖 잎(boot·dom·state·markdown·scroll·sse·copy·attach)의 바인딩: `$messages`·`$input`
  같은 DOM 참조와 `es`. 잎은 자기보다 앞의 모듈만 import 하므로 app.js 에 적힌 순서대로
  평가된다. 그래서 순환의 어느 모듈보다 먼저다.
- 순환 안 모듈의 바인딩을 평가 시점에 읽는 곳은 **하나뿐**이다. channels 의 채널 바 IIFE 가
  `ovSyncChannels(); frameShowChannel(ovActiveChannel);` 을 부르고, 그 호출이 frames 의
  `frames`·`ctxViews`·`$ctxBudget`·`$ctxTail`·`frameSeen` 과 `session` 을 읽는다.

평가 시점에 다른 모듈의 바인딩을 읽는 줄 전부(새 모듈의 줄 번호, acorn — IIFE·`forEach`
콜백 본문 포함, 리스너 콜백 본문 제외):

| 모듈 | 줄 | 읽는 이름 (출처) |
|---|---|---|
| `scroll.js` | 97·118·149·154·155 | `$messages` (dom) |
| `attach.js` | 127 | `$input` (dom) |
| `attach.js` | 132·138·141 | `$inputArea` (dom) |
| `input.js` | 230·231 | `$chatStop` (dom) |
| `input.js` | 236 | `$send` (dom) |
| `input.js` | 241 | `$input` (dom) |
| `input.js` | 272 | `$abort` (dom) |
| `input.js` | 278·291·295 | `es` (sse) |
| `timeline.js` | 353·384·389 | `es` (sse) |
| `share.js` | 60·66·72·80·89·105·111·116·124·130·166·174·185·191·196·200·212·220·233·241·260·266·274 | `es` (sse) |
| `roster.js` | 13·16·24·213 | `es` (sse) |
| `channels.js` | 354 | `ovSyncChannels` (runs) — 호출 |
| `channels.js` | 355 | `frameShowChannel` (frames) — 호출 |

dom·sse 는 순환 밖 잎이다. 순환 안을 향하는 것은 channels 354·355 의 두 호출뿐이다. 이 두
호출은 옛 [31] 의 채널 바 IIFE 다. 호출을 따라가면 frames 의 const 들과 `session` 을 읽는다.

ES 모듈의 평가는 깊이 우선 후위 순서다. 순환에서는 **처음 import 된 모듈이 마지막에 평가된다**
— 그 모듈의 import 를 따라 순환의 나머지가 모두 먼저 끝난다. 그래서 app.js 는 잎 여덟 다음에
**channels 를 순환의 첫 멤버로** import 한다. channels 의 채널 바 IIFE 가 돌 때 순환의 다른
모듈은 전부 평가돼 있다. app.js 의 channels 뒤 아홉 줄은 이미 평가된 모듈이라 하는 일이 없고,
모듈 목록으로 둔다.

실제 평가 순서는 이렇다(import 줄로 모의):

```
boot dom state markdown scroll sse copy attach | progress thumbs cards runs roster share frames timeline input channels
```

진입점이 바뀌면 깨진다. frames 를 순환의 첫 import 로 두면 node 에서
`ReferenceError: Cannot access 'frames' before initialization` 이 난다(확인함).

순서가 옮겨 간 나머지 top-level 부작용은 서로 무관함을 확인했다.

- 같은 대상·같은 이벤트에 여러 모듈이 리스너를 거는 곳이 없다. SSE 이벤트 종류마다 등록은
  한 모듈 한 번이다.
- `scroll.js` 의 `MutationObserver`(#messages)는 잎이라 channels 의 IIFE 가 #messages 를
  건드리기 전에 걸린다. 옛 순서([7] → [31])와 같다.
- `new EventSource` 는 sse 가 잎이라 먼저 돈다. 이벤트는 §8.5 대로 평가가 끝난 뒤에 온다.

### 8.5 SSE 핸들러 등록 시점

`es` 는 `sse.js` 에서 만들고, 핸들러(`es.addEventListener`)는 각 소유 모듈이 `import { es }`
해서 건다 — timeline 의 `scope_*`, share 의 대화 카드 이벤트들, input 의 `input_required`·
`input_resolved`·`worker_state`, roster 의 `snapshot_end`·`identity`·`viewers`·`queue`.
top-level await 가 없는 모듈 그래프는 한 작업(task) 안에서 동기로 끝까지 평가된다. EventSource
의 이벤트는 그 뒤의 작업으로 디스패치되므로, 첫 이벤트가 오기 전에 모든 모듈의 등록이 끝나
있다. 옛 단일 파일에서 [24] 뒤에 등록하던 것과 같다.

### 8.6 node 가 `markdown.js` 를 직접 import 한다

`markdown.js` 는 import 가 없고 top-level 에서 DOM·window 를 건드리지 않는다. node 하니스는
`.mjs` 스크립트 파일에 `import { escapeAndFormat } from "file:///…/js/markdown.js"` 를 쓰고
`node <파일>` 로 실행한다. 대역(stub)이 없다 — 대역이 필요해지면 그 자체로 테스트가 깨진다.
`node -e` 인자 상한(128 KiB) 때문에 스크립트는 여전히 파일로 넘긴다.

`obsSummary`(cards)·에이전트 아이콘(channels)은 순환 안이라 메인 화면 그래프 전체가 딸려 온다.
하니스는 브라우저 전역 대역 모듈을 먼저 import 하고 메인 화면 모듈을 app.js 순서로 import 한
뒤 그 이름을 import 한다. 이 평가가 TDZ 없이 끝나는 것을
`test_main_screen_graph_evaluates` 가 단정한다(§8.4 의 진입점이 바뀌면 여기서 깨진다).

v10.116.0 까지의 긁기 — 메인 본문을 `(function(){…})()` 로 감싸기, `js_fn_body` 로 함수
하나씩 떼어 잇기, 정규식으로 `OV_AGENT_*` 풀 떼기 — 는 전부 import 로 바뀌었다. 아무도 부르지
않던 `_ov_render_html` helper(없는 함수 `ovUserHtml`·`ovRespHtml` 을 가리켰다)는 함께 지웠다.
`tests/web_static.py` 의 `js_text()`·`js_fn_body()` 는 소스 글자를 보는 다른 테스트를 위해 남는다.

### 8.7 가드와 등가성

`tests/test_web_static_modules.py`:

- (a) export 된 이름은 다른 모듈이나 node 하니스가 import 한다.
- (b) import 한 이름은 출처 모듈이 export 한다.
- (c) 순환의 모양: 순환 밖 모듈은 app.js 에서 앞의 모듈만 import 하고, 순환 묶음은 하나이며
  진입점은 channels 다. 실제 평가는 node 테스트(§8.6)가 본다.
- (d) import 한 바인딩에 대입하지 않고, 다른 모듈의 top-level `let`/`var` 를 대입하지 않는다.
- `state.js` 는 import 없는 잎이고 export 는 `session` 하나, 옛 이름 넷은 코드에 없다.

등가성 증명은 줄 multiset 비교다. 옛 `main.js` 의 모든 줄과 새 모듈 18개의 모든 줄에서 빈 줄과
`// ──` 절 머리를 걷는다. 옛 쪽은 파일 머리 주석 4줄을 걷는다. 새 쪽은 모듈 머리 주석·import
문·선언 앞 `export `·setter 정의와 그 주석·`session` 객체 틀을 걷는다. 그 뒤 `session.x` 와
setter 호출을 옛 이름의 대입으로, `session` 필드 줄을 옛 선언 줄로 역치환한다.

| | 줄 |
|---|---|
| 옛 줄 | 3,994 |
| 새 줄 | 3,994 |
| 옛에만 있는 줄 | 0 |
| 새에만 있는 줄 | 0 |

acorn 으로 따로 확인한 것: 18개 모두 모듈 모드로 파싱된다. 모듈마다 빠진 import·안 쓰는
import·export 안 된 import 가 0 이다. 옛 파일과 새 모듈 전체의 자유 변수(브라우저 전역) 집합이
같다. 같은 이름의 top-level 함수가 두 모듈에 생기지 않았다(`js_fn_body` 의 모호성 목록이
v10.116.0 과 같다).
