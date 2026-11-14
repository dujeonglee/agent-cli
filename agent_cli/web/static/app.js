// agent-cli web — vanilla JS client, 진입 모듈.
//
// 빌드 단계·프레임워크 없이 브라우저 네이티브 ES 모듈로 나뉜다(v10.116.0,
// docs/web-modules/DESIGN.md). 이 파일의 역할은 하나 — 모듈 적재 순서.
// 테스트(tests/web_static.py `module_order()`)도 이 줄들을 읽어 모듈 목록을 안다.
// import 는 반드시 `./` 상대 경로 — 이 파일의 URL 기준으로 풀려 base-path
// 접두 밑에서도 맞는다.
//
// 메인 화면(v10.117.0, DESIGN §8)은 모듈 22개다. 처음 열둘은 순환 밖의 잎으로
// 적힌 순서대로 평가된다(v10.119.0 의 ledger 가 아홉째, v10.120.0 의 stuck 이 열째, v10.121.0 의 jobs 가 열한째, v10.137.0 의 changes 가 열둘째 — 순환 앞). 그 뒤 열 개는 함수 호출로 서로 얽힌 순환 묶음이다.
// ESM 은 깊이 우선 후위로 평가해, 순환에서는 처음 import 된 멤버가 마지막에
// 평가된다. 평가 시점에 순환 안을 읽는 줄은 channels 채널 바 IIFE(옛 [31])의
// 두 호출 `ovSyncChannels()`·`frameShowChannel(ovActiveChannel)` 뿐이다. 이 둘은
// frames 의 const 와 session 을 읽으므로, 순환의 나머지가 모두 평가된 뒤에
// 돌아야 한다. 그래서 묶음의 진입점은 channels 다(DESIGN §8.4). channels 뒤의
// 아홉 줄은 이미 평가된 모듈이라 아무 일도 하지 않는다(목록으로 둔다). 기능 모듈
// 열 개는 메인 화면을 import 하지 않으므로 적힌 순서대로 그 뒤에 평가된다.
import "./js/boot.js";
import "./js/dom.js";
import "./js/state.js";
import "./js/markdown.js";
import "./js/scroll.js";
import "./js/sse.js";
import "./js/copy.js";
import "./js/attach.js";
import "./js/ledger.js";
import "./js/stuck.js";
import "./js/jobs.js";
import "./js/changes.js";
import "./js/channels.js";
import "./js/timeline.js";
import "./js/frames.js";
import "./js/progress.js";
import "./js/thumbs.js";
import "./js/cards.js";
import "./js/input.js";
import "./js/share.js";
import "./js/runs.js";
import "./js/roster.js";
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
