# 웹 프런트를 ES 모듈로 (v10.116.0 — 1단계)

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
`test_app_markdown` 은 `js_text("main")` 을 `(function(){ … })()` 로 감싸 node 로 실행한다.
그래서 main.js 에 `import`/`export` 가 없음을 단정한다.

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

## 7. 2단계 계획 (다음 PR)

`main.js` 를 절 단위 모듈로 쪼갠다. 후보는 `markdown`·`cards`·`frames`·`timeline`·
`input`·`sse`·`runblocks`·`queue`·`roster`·`util`·`state` 다.

- 공유 가변 상태는 `state.js` 에 모은다. 리뷰 §4.6 의 "평평한 가변 상태 30여 개" 다.
- 순수 함수(마크다운 파이프라인·에이전트 아이콘 해시·카드 마크다운)는 export 한다.
  node 가 `import()` 로 직접 실행하면 소스 긁기(`js_fn_body`·`_js_pool`·하니스 스텁)가
  사라진다.
- 그때 `tests/web_static.py` 의 긁기 helper 는 줄어들거나 없어진다. 서빙 테스트와
  `module_order()` 는 남는다.
