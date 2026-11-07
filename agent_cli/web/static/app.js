// agent-cli web — vanilla JS client, 진입 모듈.
//
// 빌드 단계·프레임워크 없이 브라우저 네이티브 ES 모듈로 나뉜다(v10.116.0,
// docs/web-modules/DESIGN.md). 이 파일의 역할은 하나 — 모듈 적재 순서.
// 순서가 곧 각 모듈의 top-level 코드(이벤트 리스너 등록·첫 fetch)가 도는
// 순서이므로 옛 단일 파일의 IIFE 순서를 그대로 지킨다. 테스트
// (tests/web_static.py `module_order()`)도 이 줄들을 읽어 순서를 안다.
// import 는 반드시 `./` 상대 경로 — 이 파일의 URL 기준으로 풀려 base-path
// 접두 밑에서도 맞는다.
import "./js/main.js";
import "./js/directives.js";
import "./js/workspace.js";
import "./js/theme.js";
import "./js/knobs_client.js";
import "./js/knob_chips.js";
import "./js/mcp.js";
import "./js/knob_popup.js";
import "./js/presence.js";
import "./js/schedule.js";
import "./js/web_sources.js";
