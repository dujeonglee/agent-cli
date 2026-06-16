# Jira http 허용 + 닉네임 중간 변경 — Design

> Status: Draft
> Date: 2026-06-16
> Owner: architect (dev-pipeline team)
> Companion: [REQUIREMENTS.md](REQUIREMENTS.md), [TEST_PLAN.md](TEST_PLAN.md)

## 0. 개요

두 기능 모두 **작은 외과적 변경**이다. 새 모듈/추상화/의존성 없이 기존 코드의 한정된 지점만 수정한다.

| 기능 | 변경 파일 | 변경 지점 | 종류 |
|---|---|---|---|
| 1. Jira http (백엔드) | `agent_cli/integrations/jira.py` | `resolve_target()` scheme 가드 1곳 | 로직 완화 |
| 1. Jira http (경고 UI) | `index.html`, `app.js`, `style.css` | export Jira 폼 | 인라인 경고 |
| 2. 닉네임 변경 | `app.js`, `style.css` | viewers 핸들러 + name-bar 재사용 | 진입점 추가 |

백엔드 `render/web.py` / `server.py` 는 **변경 없음**(기능2 는 기존 `set_nickname` / `POST /api/nickname` 재사용).

---

## 1. 기능1 — Jira http 허용 (백엔드)

### 1.1 현재 코드

`agent_cli/integrations/jira.py:resolve_target()` 의 미등록 URL 경로 (191-195):

```python
    if not user_url.lower().startswith("https://"):
        raise JiraError(
            "Jira base URL must use https:// (or configure it server-side)."
        )
    return {"name": user_url, "base_url": user_url, "deployment": None}
```

### 1.2 변경 후

scheme 화이트리스트를 `https://` 단독 → `http://` + `https://` 로 확장한다:

```python
    scheme_ok = user_url.lower().startswith(("http://", "https://"))
    if not scheme_ok:
        raise JiraError(
            "Jira base URL must use http:// or https:// "
            "(or configure it server-side)."
        )
    return {"name": user_url, "base_url": user_url, "deployment": None}
```

설계 근거:

- **발생 원인 위치 유지**: scheme 정책은 `resolve_target` 한 곳의 invariant 다. 헬퍼 추출 금지(NFR-HTTP-1).
- **바이트 동일성**: config-매칭 분기(183-190)와 https 분기는 손대지 않으므로 기존 https/config 경로의 반환값과 인자가 그대로 유지된다(FR-HTTP-3).
- **거부 대상**: `tuple` prefix 매칭이라 `ftp://`, `file://`, `javascript:`, scheme 없는 호스트는 여전히 거부된다(FR-HTTP-1).
- **docstring 갱신**: `resolve_target` docstring 의 "must be `https://`" 설명을 "http 또는 https 허용(평문 위험은 UI 가 경고)" 로 갱신한다(규칙: 발견한 문서 부정확성도 같이 수정).

`server.py:export_jira` 는 변경 불필요 — `resolve_target` 결과를 그대로 `post_comment` 에 넘기므로 http URL 이 그대로 흘러간다. http 호출은 `requests` 가 자연히 처리한다.

### 1.3 기능1 — 평문 경고 UI

#### index.html

`#export-jira-form` (38-50) 안, URL 입력 필드 아래/옆에 경고 span 1개 추가:

```html
      <span id="export-jira-http-warn" class="exp-warn" hidden>⚠ http 는 자격증명이 평문으로 전송됩니다. 신뢰된 네트워크에서만 사용하세요.</span>
```

#### app.js

export IIFE 에 element 참조 추가(`$jiraSecret` 등과 같은 블록, 1481 부근):

```js
  const $jiraHttpWarn = document.getElementById("export-jira-http-warn");
```

경고 재평가 함수(폼 로직과 같은 위치):

```js
  // Show a plaintext-credential warning when the (user-typed) URL is http://.
  // https / config URLs are TLS-protected; empty hides it.
  function updateJiraHttpWarn() {
    if (!$jiraHttpWarn) return;
    const url = $jiraUrl.value.trim().toLowerCase();
    $jiraHttpWarn.hidden = !url.startsWith("http://");
  }
```

호출 지점(URL 값이 바뀌는 모든 곳):
- `onJiraUrlChange()` 끝에 호출(타이핑/blur 시 재평가).
- `onJiraTargetChange()` 는 `onJiraUrlChange()` 를 이미 호출하므로 자동 반영.
- `showJiraForm()` 의 prefill 두 분기 끝(config / zero-config) 모두 `onJiraUrlChange()` 를 거치므로 자동 반영.
- `$jiraUrl` 에 `input` 리스너를 달아 매 키 입력마다 호출:
  ```js
  if ($jiraUrl) $jiraUrl.addEventListener("input", function () {
    onJiraUrlChange();      // 기존 자격증명 reload
    updateJiraHttpWarn();   // 평문 경고 재평가
  });
  ```
  (기존 코드에 `$jiraUrl` input 리스너가 없으면 신규 추가; `onJiraUrlChange` 가 이미 어딘가에서 호출되면 그 끝에 `updateJiraHttpWarn()` 한 줄을 추가하는 것으로 갈음.)

> 구현자 노트: `onJiraUrlChange()` 본문 끝에 `updateJiraHttpWarn();` 한 줄을 넣고, `$jiraUrl` 의 `input`/`change` 이벤트에서 `onJiraUrlChange()` 가 불리도록만 보장하면 모든 경로가 커버된다. 폼을 닫을 때(`hideJiraForm`/`exit`)는 경고도 함께 숨겨지므로(폼 자체가 hidden) 별도 처리 불필요.

#### style.css

`#export-jira-form` 블록(801 부근) 근처에 경고 스타일:

```css
.exp-warn { color: #b45309; font-size: 12px; }
#export-jira-http-warn[hidden] { display: none; }
```

(색상은 기존 팔레트의 amber/warn 톤에 맞춤. 기존 `.exp-hint` 와 동급 크기.)

### 1.4 영향 없음 확인

- config-등록 http URL: 여전히 `by_url` 매칭으로 신뢰(현행). 경고 UI 는 URL 값 기준이라 config http 를 골라도 경고가 뜨지만(그 URL 도 실제 http 평문이므로) **정확한 표시**다 — 막지 않으므로 동작 무해.
- https / Cloud / Server-DC: 백엔드·프론트 모두 무변경.

---

## 2. 기능2 — 닉네임 중간 변경 (프론트)

### 2.1 설계 개념

기존 `#name-bar` 컴포넌트와 `applyNickname()` 경로를 100% 재사용한다. 추가하는 것은 **재노출 트리거** 하나뿐이다:

```
[현재] 페이지 로드 → maybeNamePrompt (1회) → name-bar (최초만)
[추가] viewers roster 의 ✎ 버튼 클릭 → name-bar 재노출 (언제든)
```

### 2.2 viewers 핸들러 변경 (app.js 1119-1129)

현재 roster 는 텍스트로만 렌더된다. ✎ 진입점을 위해 자기 항목 옆에 버튼을 둔다. 두 가지 배치 옵션:

**옵션 A (채택) — `#viewers` 옆 단일 ✎ 버튼.**
roster 텍스트는 그대로 두고, `#viewers` 영역에 "(you)" 가 존재할 때만 보이는 ✎ 버튼 하나를 둔다. 자기 자신 편집이므로 viewer 별 버튼이 불필요하고, roster 문자열 파싱/DOM 구조 변경을 최소화한다.

**옵션 B (기각) — viewer 항목별 인라인 버튼.**
roster 를 span 텍스트에서 항목별 DOM 으로 재구성해야 하고, 자기 것 외엔 버튼이 무의미하므로 과한 변경. 기각.

#### index.html

`#viewers` (16) 뒤에 편집 버튼 추가:

```html
    <button id="rename-btn" type="button" title="닉네임 변경" hidden>✎</button>
```

#### app.js — viewers 핸들러

`viewers` 이벤트에서, 내 항목이 roster 에 있으면 ✎ 버튼을 노출한다(UI lifecycle 을 viewers 렌더 한 곳에 모음, NFR-NICK-3):

```js
  const $renameBtn = document.getElementById("rename-btn");
  es.addEventListener("viewers", function (e) {
    if (!$viewers) return;
    const d = JSON.parse(e.data);
    const labels = (d.viewers || []).map(function (v) {
      return v.id === myConnId ? v.name + " (you)" : v.name;
    });
    $viewers.textContent =
      "👁 " + d.count + (labels.length ? " · " + labels.join(", ") : "");
    $viewers.title = labels.join(", ");
    // ✎ visible once we know who we are and we're in the roster.
    const me = (d.viewers || []).find(function (v) { return v.id === myConnId; });
    if ($renameBtn) $renameBtn.hidden = !me;
    maybeNamePrompt(d.viewers || []);
  });
```

> `me` 는 아래 `openNameBar(currentName)` 의 prefill 에도 쓰이므로, 최신 `viewers` 의 내 이름을 모듈 스코프 변수(`myNickname`)에 캐시한다:
> ```js
> if (me) myNickname = me.name;
> ```

### 2.3 name-bar 재노출 로직 (app.js 1131-1188)

기존 `maybeNamePrompt` 의 prefill+show 동작을 작은 헬퍼로 추출해 ✎ 와 공유한다(첫 연결 경로의 동작은 보존):

```js
  let myNickname = "";   // latest roster name for prefill on rename

  // Show the name-bar pre-filled with `current`, focused. Shared by the
  // first-connect prompt and the ✎ rename entry point.
  function openNameBar(current) {
    if (!$nameBar) return;
    $nbInput.value = current || "";
    $nameBar.hidden = false;
    $nbInput.focus();
    $nbInput.select();
  }
```

`maybeNamePrompt` 의 표시 분기를 `openNameBar(me ? me.name : "")` 호출로 치환(동작 동일):

```js
  function maybeNamePrompt(viewers) {
    if (namePrompted || !myConnId || !$nameBar) return;
    namePrompted = true;
    const saved = (localStorage.getItem(NICK_KEY) || "").trim();
    if (saved) {
      postNickname(saved);
      return;
    }
    const me = viewers.find(function (v) { return v.id === myConnId; });
    openNameBar(me ? me.name : "");
  }
```

✎ 버튼 핸들러 — 현재 닉네임으로 name-bar 재노출(FR-NICK-2):

```js
  if ($renameBtn) {
    $renameBtn.addEventListener("click", function () {
      if (!myConnId) return;            // identity not yet known
      openNameBar(myNickname);          // prefill with current nickname
    });
  }
```

`applyNickname()` (1167-1174) 는 **변경 없음** — 설정/Enter 시 `POST /api/nickname` + `localStorage` 갱신 + 바 숨김(FR-NICK-3). 백엔드 `set_nickname` 이 roster 를 재브로드캐스트하므로 FR-NICK-4 자동 충족.

### 2.4 style.css

`#rename-btn` 을 헤더의 다른 아이콘 버튼(`#export-btn` 등)과 동급 톤으로:

```css
#rename-btn { background: none; border: none; cursor: pointer; font-size: 13px; color: #64748b; padding: 0 2px; }
#rename-btn[hidden] { display: none; }
```

### 2.5 동작 시퀀스

```
첫 연결:  identity → viewers → maybeNamePrompt (저장값 없으면 name-bar)
                              → $renameBtn.hidden=false (내가 roster 에 있으니)
중간 변경: ✎ 클릭 → openNameBar(myNickname) → 입력 → 설정/Enter
        → applyNickname → POST /api/nickname → localStorage 갱신 → 바 숨김
        → 백엔드 set_nickname → viewers 재브로드캐스트 → roster 갱신 + myNickname 갱신
```

---

## 3. 변경 파일 요약

| 파일 | 기능1 | 기능2 |
|---|---|---|
| `agent_cli/integrations/jira.py` | scheme 가드 + docstring | — |
| `agent_cli/web/static/index.html` | `#export-jira-http-warn` span | `#rename-btn` 버튼 |
| `agent_cli/web/static/app.js` | `updateJiraHttpWarn` + 호출 | `openNameBar` 추출 + `$renameBtn` 핸들러 + `myNickname` 캐시 |
| `agent_cli/web/static/style.css` | `.exp-warn` | `#rename-btn` |
| `agent_cli/web/server.py` | — | — (무변경) |
| `agent_cli/web/render/web.py` | — | — (무변경) |

테스트 + README + ARCHITECTURE 는 같은 커밋에 포함(규칙 #6).

## 4. 트레이드오프 / 결정

- **기능1 scheme 만 검증, 호스트 검증 안 함**: `http://` / `https://` prefix 만 본다. 잘못된 호스트는 `post_comment` 의 `requests` 가 연결 에러로 surface 하므로 충분(과검증 회피).
- **기능2 단일 ✎ 버튼(옵션 A)**: 자기 닉네임만 바꾸므로 viewer 별 버튼 불필요. roster DOM 구조를 건드리지 않아 회귀 표면 최소.
- **백엔드 무변경(기능2)**: `set_nickname` 이 이미 idempotent + 재브로드캐스트하므로 프론트 진입점만 추가하면 끝. 영속화는 의도적으로 범위 밖(ephemeral 유지).
- **경고는 차단하지 않음(기능1)**: http 는 허용된 동작이라 전송을 막으면 모순. 정보성 인라인 텍스트로만 표시.
