# 내장 브라우저 도구 — 설계 (v10.39.0)

작성 2026-10-15, 권고안대로 승인되어 구현(§8 의 결정은 전부 권고 쪽). 근거 실측은 README 「예: 헤드리스 브라우저 (Playwright MCP)」와 세션 기록.

## 1. 목표 / 비목표

**목표**
- JavaScript 가 그리는 페이지를 텍스트 전용 모델이 읽고 조작(클릭·입력·선택)한다.
- 창 없이(헤드리스) 돈다. 외부 종속성은 Python `playwright` 패키지 하나 + 브라우저 엔진.
- MCP 로 붙였을 때 드러난 세 문제를 처음부터 없앤다: 도구 25개 설명(첫 요청 +5,000토큰),
  임의 코드 실행 도구 상시 노출, 큰 페이지 스냅샷(59K–89K자).

**v1 비목표** — 스크린샷, 파일 업로드·다운로드, 임의 JavaScript 실행, 여러 탭, 로그인 프로필 저장,
네트워크·콘솔 디버깅, 드래그, 마우스 올리기.

## 2. 종속성과 가용성

| 항목 | 결정 |
|---|---|
| Python 패키지 | `pip install agent-cli[browser]` → `playwright`. 기본 설치에는 없다 (CLAUDE.md "새 의존성 최소화"). Node.js 를 따로 깔 필요는 없다 — 패키지 안에 드라이버가 들어 있다. |
| 브라우저 엔진 | 순서대로 시도: ① `AGENT_CLI_BROWSER_EXECUTABLE` 경로 ② 설치된 Google Chrome (`channel="chrome"`) ③ Playwright Chromium (`playwright install chromium`). 셋 다 없으면 도구 호출이 설치 안내와 함께 실패한다. |
| 가용성 게이트 | `requires_handler="browser"` — `runtime.browser_resource()` 가 `playwright` 를 import 할 수 있고 `AGENT_CLI_BROWSER_TOOL` 이 `0` 이 아닐 때만 자원을 돌려준다. 아니면 `snapshot` 도구와 같은 seam(`core.py` handlers)으로 tools_list 에서 빠져 **프롬프트에 나오지 않는다** — 없는 기능을 약속하지 않는다. |
| 실행 시점 | 브라우저는 **첫 호출 때** 띄운다. 안 쓰는 세션은 비용 0. |

## 3. 도구 모양 — `browser` 하나, `mode` 로 분기

`snapshot`·`memory` 와 같은 모양. 도구 하나라 프롬프트에 실리는 설명이 하나다.

| mode | 인자 | 하는 일 | 결과 |
|---|---|---|---|
| `open` | `url` | 이동 | 헤더(URL·제목) + **스냅샷** |
| `snapshot` | `ref?`, `depth?` | 페이지(또는 `ref` 아래)의 접근성 트리 | 트리 텍스트, 요소마다 `[ref=e12]` |
| `find` | `text` 또는 `regex` | 일치하는 노드만 | 일치 노드 + 조상 경로 (MCP `browser_find` 와 같은 모양) |
| `text` | `ref?` | 읽을 글만 (본문) | `fetch` 와 같은 마크다운풍 텍스트 |
| `click` | `ref` | 클릭 | 한 줄 결과; 페이지가 바뀌었으면(URL·제목) 새 스냅샷 |
| `type` | `ref`, `text`, `submit?` | 입력 (+Enter) | 위와 같음 |
| `select` | `ref`, `value` | 드롭다운 선택 | 한 줄 |
| `press` | `key` | 키 입력 (`Enter`, `Escape`…) | 한 줄 (+새 스냅샷 if 바뀜) |
| `back` | — | 뒤로 | 헤더 + 스냅샷 |
| `close` | — | 브라우저 종료 | 한 줄 |

`required: ["mode"]`, 모드별 필수 인자는 설명과 거부 문구(A5 `summarize_call`)가 알려 준다 — `snapshot` 도구의 `path` 와 같은 수준.

### 3.1 ref — 공개 API 만으로

Python Playwright 공개 API 에는 MCP 서버의 `[ref=eN]` 스냅샷이 없다(내부 `snapshotForAI` 는 비공개). 대신:

1. `page.locator("body").aria_snapshot()` (공개) 으로 트리를 받는다. 실측 출력:
   ```
   - main:
     - heading "Shop" [level=1]
     - textbox "Email"
     - combobox:
       - option "All" [selected]
     - button "Search"
     - button "Search"
   ```
2. 줄마다 `role`·`name` 을 읽어 **같은 (role, name) 안에서 몇 번째인지(nth)** 와 함께 ref 를 붙인다. 맵은 세션 자원에 둔다: `e12 → (role="button", name="Search", nth=1, generation=7)`.
3. 동작은 `page.get_by_role(role, name=name, exact=True).nth(nth)` 로 짚는다 — MCP 서버도 ref 를 결국 이 로케이터로 바꿔 실행했다(실측 로그 `getByRole('button', { name: 'Search' })`). 같은 이름 버튼 2개가 `nth` 로 구분되는 것을 확인했다.
4. ref 를 붙이는 role: 조작 가능한 것(button, link, textbox, searchbox, checkbox, radio, combobox, listbox, option, menuitem, tab, switch, slider, spinbutton) + 범위 지정용 랜드마크(main, navigation, form, region, article, complementary, contentinfo, banner, heading). `generic`·`text`·`cell` 등은 ref 없이 표시만.
5. **세대(generation)**: 스냅샷을 새로 만들 때마다 번호가 오르고, 옛 세대의 ref 로 조작하면 "stale — take a new snapshot" 으로 거부한다 (MCP 의 `Ref e25 not found` 와 같은 회복 경로; 실측에서 모델이 스스로 회복했다).

### 3.2 결과 크기 — 큰 페이지 문제의 답

- **스냅샷은 조작·구조 위주**: 텍스트 노드(`paragraph`, `text`, `cell`)는 한 노드 80자에서 자르고 `…(N자)` 를 붙인다. 긴 본문은 `text` 모드로 따로 읽는다 — 실측에서 Flash Next 가 `browser_evaluate` 로 본문만 뽑아 맞힌 길을 도구로 제공하는 것.
- `snapshot(ref=<랜드마크>)`·`depth` 로 범위를 좁힌다 (MCP `target`·`depth` 와 같음).
- 그래도 캡을 넘으면 공통 과대 출력 정책 그대로(파일 저장 + 검색/범위 안내). `oversized_retry_hint` = "`snapshot(ref=<landmark>)` 로 범위를 좁히거나 `find(text=…)` / `text(ref=…)` 를 쓰라".
- `open`·페이지가 바뀐 `click`·`type` 결과에 스냅샷을 **포함**한다. 실측에서 35B 는 `navigate` 뒤 거의 항상 `snapshot` 을 따로 불렀다(149회) — 왕복 하나를 줄인다. 토큰이 늘 수 있으므로 측정 항목(§7)이다.
- **자동 포함 스냅샷에는 예산이 있다** (§7 1차 측정의 결과로 추가): 과대 출력 캡(창/10)의 1/4(`AUTO_SNAPSHOT_SHARE` = 창의 2.5%). 전체 트리가 예산을 넘으면 **예산 안에 드는 가장 깊은 depth** 까지만 보이고 `[snapshot cut to depth D: n of N nodes (whole tree ~T tokens) — use find/text/snapshot(ref=)]` 한 줄이 붙는다. 숨은 노드에도 ref 는 붙어 있어 `find` 로 찾은 번호를 바로 누를 수 있다. 명시적 `snapshot` 은 예산 없이 전체(캡을 넘으면 공통 정책). 위키백과 문서(트리 ~17K 토큰): 32K 창(예산 819)은 depth 1·36줄, 262K 창(예산 6,553)은 depth 4·624줄.

## 4. 안전

| 항목 | 결정 |
|---|---|
| 코드 실행 | 없다. `evaluate`/`run_code` 에 해당하는 모드를 두지 않는다. |
| URL | `http`/`https` 만. `file://`·`javascript:` 거부. |
| 허용 호스트 | `AGENT_CLI_BROWSER_HOSTS` (쉼표 목록, 하위 도메인 포함). **비어 있으면 전부 허용**. 온프레미스에선 설정으로 잠근다. 차단 목록은 두지 않는다(허용 목록이 단순하다). |
| 대화상자 | `alert`/`confirm`/`prompt` 는 자동으로 닫고 결과에 한 줄 남긴다 (막힘 방지). |
| 다운로드 | 거부(이벤트 취소). 업로드 모드 없음. |
| 프로필 | 메모리 전용 컨텍스트, 세션이 끝나면 쿠키·로그인 소멸. 헤드리스 고정. |
| 되돌릴 수 없는 동작 | 결제·전송·삭제를 하니스가 구분할 수 없다. 문서에 "로그인된 사이트·결제 화면에 쓰지 말라" 고 명시하고, 호스트 허용 목록이 유일한 장치다. |

## 5. 동시성 · 수명 · 시간 제한

- **세션당 브라우저 하나**, 세션 자원이 소유한다. 도구 싱글턴(`TOOLS`)에는 상태를 두지 않는다 (서브에이전트 스레드와 공유되므로).
- Playwright sync API 는 만든 스레드에서만 쓸 수 있다 → 자원 안에 **전용 스레드 + 요청 큐**. 모든 호출(서브에이전트 포함)은 큐에 넣고 결과를 기다린다 = 자연히 직렬. `parallel_safe=False`.
- 종료: `ON_SESSION_END` 훅과 `atexit` 에서 닫는다. 유휴 자동 종료는 v1 에 없다.
- 시간 제한: `open`·`back` 30초, 조작 10초. 넘으면 "timed out" 한 줄 (도구 실패).

## 6. 파일과 규모

| 파일 | 역할 | 예상 LOC |
|---|---|---|
| `agent_cli/browser.py` (신규) | `BrowserSession`: 엔진 선택·실행, 전용 스레드·큐, `aria_snapshot` 파싱·ref 맵·세대, 동작, 호스트 검사. `playwright` import 는 이 안에서 lazy. | 606 |
| `agent_cli/tools/browser_tool.py` (신규) | `BrowserTool(Tool)`: 스키마, mode 분기, 관찰 렌더, `oversized_retry_hint`, `requires_handler="browser"`. | 173 |
| `agent_cli/runtime.py` | `browser_resource()` | +15 |
| `agent_cli/loop/core.py` | handlers 에 `"browser"` 한 줄 | +2 |
| `agent_cli/tools/registry.py` | 등록 (`snapshot` 뒤) | +3 |
| `pyproject.toml` | `[browser]` extra | +3 |
| `tests/test_browser_tool.py` (신규) | **가짜 페이지**로: 스키마·모드별 필수 인자 거부·ref 부여와 nth·세대 거부·호스트 허용·텍스트 자르기·관찰 렌더·가용성 게이트·스레드 직렬화·실행 폴백. playwright 없이 돈다. | 700 |
| `tests/browser/test_browser_tool_live.py` (신규) | 옵트인(`AGENT_CLI_BROWSER_TESTS=1`) 실브라우저: 로컬 HTML 3종(지연 렌더 표·버튼으로 열리는 영역·폼+드롭다운). | 104 |
| README | 브라우저 절을 내장 도구 기준으로 교체, MCP 는 "대안"으로 축소 | |
| docs/ARCHITECTURE.md | 두 모듈 줄 + LOC | |

### 6.1 구현에서 설계와 달라진 점

- **ref 번호는 세션 내내 단조 증가**(설계는 문서마다 1부터). 문서가 바뀔 때 번호를 되돌리면 옛 번호가 새 요소의 뜻으로 조용히 바뀐다(옛 `e2`=heading, 새 `e2`=button) — 번호를 이어 가면 옛 번호는 언제나 "stale" 로 잡힌다.
- **같은 문서 안에서는 조작 뒤에도 ref 를 무효화하지 않는다.** ref 가 role/name 로케이터라 DOM 이 바뀌어도 요소가 남아 있으면 계속 맞는다(MCP 의 내부 id 와 다른 점). 사라진 요소는 Playwright 타임아웃이 알린다.
- `find` 는 스냅샷에 보이는 모양(`link "Aurora Lamp"`)에 맞춘다 — 모델이 보는 것과 찾는 것이 같은 꼴이도록.
- 조작 결과가 "same page" 일 때는 스냅샷을 붙이지 않고 "take a snapshot to see what changed" 로 끝낸다(드롭다운이 열린 것 같은 작은 변화에 전체 트리를 다시 보내지 않는다).
- **자동 포함 스냅샷은 예산(캡의 1/4) 안의 깊이까지만** (§3.2). 설계는 전체를 붙였고, 1차 측정(§7)에서 큰 문서가 262K 창의 컨텍스트를 두 배로 키우고 32K 창에서는 캡→파일→검색 경로로 밀려 틀리게 했다. 예산은 루프의 `RunContext.oversized_cap` 에서 오므로 도구가 세션 메서드에 `budget=` 으로 넘긴다(세션은 창 크기를 모른다).

## 7. 릴리스 전 측정

기준선은 이미 있는 MCP 실측. 같은 과제 3종(로컬 2 + 위키백과) × `Qwen3.6-35B-A3B-8bit`·`Qwen3.8-Flash-Next-oQ4e-mtp` × thinking off/on × 5회, 창 262K 와 32K.

| 지표 | MCP 기준 | 합격선 |
|---|---|---|
| 정답률 | 35B 30/30, 32K 창 위키백과 14/16 | 기준 이상 |
| 첫 요청 토큰(기본 비용) | +5,000 | +1,000 이하 (도구 하나) |
| 턴 수 | 6–14 | 기준 이하 (open 에 스냅샷 포함 효과) |
| 캡에 걸린 횟수 (32K, 위키백과) | 런당 2–3 | 기준 이하 (텍스트 자르기 효과) |

### 7.1 결과 (2026-10-16, Flash Next, thinking off, 5회씩)

실제로 돈 조합은 Flash Next × thinking off 뿐이다(35B 는 서버에 올라 있지 않았고, 사용자가 바꿔야 한다). 수치는 중앙값, 컨텍스트는 런 안의 최대 입력 토큰.

| 창 | 과제 | 내장(최종) | MCP 0.0.83 |
|---|---|---|---|
| — | 첫 요청 토큰 | **11,186** (도구 없음 10,595 → +591) | 15,638 (+5,043) |
| 262K | 쇼핑·도서관 | 5/5, 5/5 · 컨텍스트 약 5K 작음 | 5/5, 5/5 |
| 32K | 쇼핑·도서관 | 5/5, 5/5 | 5/5, 5/5 |
| 262K | 위키백과 | **5/5** · 4턴 · 최대 22.8K · 캡 0 | 5/5 · 8턴 · 21.2K · 캡 0 |
| 32K | 위키백과 | **5/5** · 7턴 · 최대 13.4K · 캡 0 | 5/5 · 7턴 · 20.9K · 캡 1 |

네 지표 모두 합격선 안. 위키백과는 세 판을 거쳤다 — **자동 포함 스냅샷(§8-2)의 대가가 여기서 드러났다**:

| 위키백과 | 262K 정답·턴·최대 컨텍스트 | 32K 정답·턴·최대 컨텍스트·캡 |
|---|---|---|
| 1판: 자동 스냅샷 전체 | 5/5 · 4 · 40.3K | **3/5** · 6 · 17.8K · 캡 2 |
| 2판: 예산 = 캡 × 0.5 | 5/5 · 4 · 40.1K | 5/5 · 6 · 14.5K · 캡 0 |
| 3판: 예산 = 캡 × 0.25 (최종) | 5/5 · 4 · **22.8K** | 5/5 · 7 · **13.4K** · 캡 0 |

- 1판 32K 의 실패 모양: 자동 포함된 문서 트리가 캡(3,276)을 넘어 파일로 빠지고, 모델이 `read_file` 검색으로 띄엄띄엄 읽다 "1980s" 를 골랐다(35B+MCP 때와 같은 모양). 설계가 이 경우를 위해 둔 `text`·`find` 는 한 번도 안 쓰였다.
- 2판에서 32K 는 고쳐졌다(다섯 번 모두 `open → find(search) → type(submit) → text(ref=본문)`). 262K 는 그대로 40K: 메인 페이지 트리(추정 9.2K)가 예산 13.1K 안이라 통째로 실리고, 문서는 depth 7 로 잘려 추정 12.9K 인데 **실제 토큰은 약 16.7K**(chars/4 추정기가 이런 트리에서 1.3배 적게 센다) — 두 페이지가 겹쳐 40K.
- 3판(최종): 자동분은 창의 2.5%. 262K 가 MCP 수준(22.8K)으로 내려왔고 32K 도 더 작아졌다. 경로는 2판과 같다(262K 는 `open → type` 두 번이면 답이 보인다). 추정기 보정은 안 했다 — 다른 도구와 같은 추정기를 쓰는 일관성이 우선.

측정 도구는 세션 스크래치(`browser_builtin/batch.sh`·`score.py`, `small_window.py` 가 `context_window` 만 줄인 런처)에 있고 저장소에는 없다.

## 8. 결정 (2026-10-15 권고안대로 승인)

1. **도구 하나 + mode** (권고) vs 모드별 도구 여러 개.
2. `open`·페이지가 바뀐 조작 뒤 **스냅샷 자동 포함** (권고: 포함, §7 로 확인). → §7 1차 측정 뒤 **예산(캡의 1/4) 안의 깊이까지만** 포함하도록 조정(2026-10-16 승인, §3.2; 처음 0.5 로 쟀다가 §7 의 이유로 0.25).
3. 허용 호스트 **기본 전부 허용** (권고) vs 기본 localhost 만.
4. 기본 활성화: **extra 가 깔려 있으면 켜짐**, `AGENT_CLI_BROWSER_TOOL=0` 으로 끔 (권고).
5. `text` 모드 포함 (권고: 포함).
6. 랜드마크에만 ref 를 줄지, heading 에도 줄지 (권고: 둘 다 — `snapshot(ref=헤딩)` 은 뜻이 없으니 heading 은 `find` 결과의 위치 표시용).
