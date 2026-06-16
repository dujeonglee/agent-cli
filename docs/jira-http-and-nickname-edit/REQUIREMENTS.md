# Jira http 허용 + 닉네임 중간 변경 — Requirements

> Status: Draft
> Date: 2026-06-16
> Owner: architect (dev-pipeline team)

## 0. 문서 범위

`agent-cli web` 의 LAN 웹 UI 에서 두 개의 독립 기능을 추가한다.

1. **기능1 (Jira http)** — 사용자가 UI 에서 직접 입력한(=config 미등록) Jira base URL 에 `http://` 도 허용한다. 평문 전송 위험은 UI 경고로 알린다.
2. **기능2 (닉네임 중간 변경)** — 첫 연결 시점이 지난 뒤에도 자기 닉네임을 변경할 수 있게 한다.

두 기능은 서로 독립이며 의존 관계가 없으나, 프로젝트 규칙 #6 에 따라 **하나의 커밋**으로 묶어 처리한다.

본 문서는 **무엇을 / 왜** 만 다룬다. **어떻게**는 [DESIGN.md](DESIGN.md), **검증**은 [TEST_PLAN.md](TEST_PLAN.md) 에서 다룬다.

### 0.1 확정된 결정 (재논의 금지)

- 기능1 방식 = 「http 허용 + UI 에 평문 전송 경고 표시」. config 에 등록된 URL 의 내부 http 허용은 **현행 유지**. https / config-매칭 경로의 백엔드 동작은 **바이트 동일** 유지.
- 기능2 진입 UX = 「자기 닉네임 옆 ✎ 버튼 → 기존 `#name-bar` 컴포넌트 재노출」. 기존 name-bar UI 와 `POST /api/nickname` 을 재사용한다. 백엔드 `set_nickname` 은 이미 언제든 호출 가능(영속화 안 함, ephemeral).

---

## 1. 기능1 — 사용자 입력 Jira base URL 에 http 허용

### 1.1 현재 동작

`agent_cli/integrations/jira.py:resolve_target()` (163-195) 는 사용자 입력 `base_url` 을 다음과 같이 처리한다:

- config 의 어느 인스턴스 URL 과도 일치하면 → 그대로 신뢰(내부 http 허용). (`jira.py:183-190`)
- 일치하지 않으면(=임의 호스트) → `https://` 로 시작하지 않을 경우 `JiraError("Jira base URL must use https:// ...")` 를 raise. (`jira.py:191-194`)

즉 사용자가 사내 http Jira(예: `http://jira.lan`)를 config 없이 UI 에서 직접 입력하면 거부된다.

### 1.2 기능 요구사항 (FR-HTTP)

- **FR-HTTP-1**: config 와 일치하지 않는 사용자 입력 `base_url` 의 scheme 이 `http://` **또는** `https://` 이면 허용한다. 그 외 scheme(`ftp://`, `file://`, `javascript:`, scheme 없음 등)은 기존과 동일하게 `JiraError` 로 거부한다.
- **FR-HTTP-2**: config 에 등록된 URL 과 일치하는 경로의 동작은 변경되지 않는다(현행대로 scheme 무관 신뢰).
- **FR-HTTP-3**: `https://` URL 및 config-매칭 URL 의 백엔드 동작(반환 dict, post_comment 으로 가는 base_url, 호출 인자)은 **바이트 동일**하게 유지된다. 변경은 미등록 http URL 을 거부에서 허용으로 바꾸는 것에 한정된다.
- **FR-HTTP-4 (거부 메시지)**: 잘못된 scheme 거부 시의 `JiraError` 메시지는 이제 http 도 허용됨을 반영해 갱신한다(예: `"Jira base URL must use http:// or https:// (or configure it server-side)."`). 메시지에 `http` 와 `https` 가 모두 포함되어야 한다.
- **FR-HTTP-5 (UI 평문 경고)**: 웹 UI 의 Jira export 폼에서, 현재 URL 입력값이 `http://` 로 시작할 때 평문 전송 경고를 시각적으로 표시한다. `https://` 또는 빈 값일 때는 경고를 숨긴다.
  - 경고 트리거: URL 입력 필드 값 변경 시(폼 표시·인스턴스 선택·타이핑) 즉시 재평가.
  - 경고 문구(예): `⚠ http 는 자격증명이 평문으로 전송됩니다. 신뢰된 네트워크에서만 사용하세요.`
  - 경고는 전송을 **막지 않는다**(허용된 동작이므로). 정보성 표시일 뿐이다.

### 1.3 비기능 요구사항 (NFR-HTTP)

- **NFR-HTTP-1**: scheme 검증은 `resolve_target` 한 곳에서만 수정한다(발생 원인 위치). 새 헬퍼/추상화를 만들지 않는다.
- **NFR-HTTP-2**: 새 의존성 없음. UI 경고는 기존 `app.js` / `style.css` / `index.html` 만으로 구현한다.
- **NFR-HTTP-3**: 경고 표시는 `render` 모듈 외 직접 DOM 조작이 아니라 export IIFE 내부의 기존 Jira 폼 로직(`onJiraUrlChange` 등) 에 통합한다(프론트는 정적 자산이라 render 모듈 규칙의 대상이 아님 — 기존 export 폼 코드와 동일 위치에 둔다).

### 1.4 범위 밖

- config 스키마 변경(config 의 http 허용 정책은 이미 현행 유지).
- URL 형식 전체 검증(호스트/포트 정합성). scheme 화이트리스트만 한다.
- 경고를 모달/확인 다이얼로그로 만드는 것(인라인 텍스트 1줄로 충분).

---

## 2. 기능2 — 닉네임 중간 변경

### 2.1 현재 동작

`app.js` 의 nickname 흐름(1131-1188):

- `maybeNamePrompt(viewers)` 는 페이지 로드당 1회만 실행된다(`namePrompted` 가드, 1151). 저장된 닉네임이 있으면 조용히 적용(`postNickname`)하고 바를 띄우지 않으며, 없으면 `#name-bar` 를 노출한다.
- `applyNickname()` 은 입력값을 `POST /api/nickname` 으로 전송하고 `localStorage[agentcli_nickname]` 에 저장한 뒤 바를 숨긴다.
- 일단 한 번 설정/스킵하고 나면 **다시 닉네임을 바꿀 진입점이 없다**.

백엔드 `WebRenderer.set_nickname()` (287-297) 은 언제든 호출 가능하며, 갱신 후 roster(`viewers`)를 즉시 재브로드캐스트한다. 영속화는 하지 않는다(ephemeral, 프로세스 메모리).

### 2.2 기능 요구사항 (FR-NICK)

- **FR-NICK-1 (진입점)**: viewer roster(`#viewers`) 에서 자기 항목 옆(또는 roster 영역 내)에 ✎ 편집 버튼을 노출한다. 클릭하면 기존 `#name-bar` 를 다시 표시한다.
  - ✎ 버튼은 자기 자신(`conn_id === myConnId`)에 대해서만 노출/동작한다.
  - `myConnId` 가 아직 없으면(=identity 미수신) 버튼은 동작하지 않는다.
- **FR-NICK-2 (재노출)**: ✎ 클릭 시 `#name-bar` 를 `hidden=false` 로 전환하고, 입력 필드에 **현재 자기 닉네임**을 prefill 한 뒤 focus + select 한다. 첫 연결 prefill 과 동일한 UX 를 재사용한다.
- **FR-NICK-3 (저장)**: name-bar 에서 설정(또는 Enter)하면 기존 `applyNickname()` 경로를 그대로 사용한다 — `POST /api/nickname` 재호출 + `localStorage[agentcli_nickname]` 갱신 + 바 숨김.
- **FR-NICK-4 (roster 즉시 반영)**: 변경 후 백엔드가 브로드캐스트하는 `viewers` 이벤트로 모든 클라이언트의 roster 가 새 닉네임으로 갱신된다(기존 `set_nickname` → `_broadcast_viewers_locked` 경로 그대로).
- **FR-NICK-5 (취소)**: name-bar 의 ✕(skip) 버튼은 중간 변경 컨텍스트에서도 바를 숨기기만 한다(현재 닉네임 유지). 기존 동작과 동일.
- **FR-NICK-6 (회귀 없음)**: 첫 연결 시 닉네임 prompt 동작(`maybeNamePrompt` 의 1회성, 저장값 silent 적용)은 변경되지 않는다. ✎ 진입은 별도 경로로, `namePrompted` 가드와 독립적으로 동작한다.

### 2.3 비기능 요구사항 (NFR-NICK)

- **NFR-NICK-1**: 백엔드(`render/web.py`, `server.py`) 변경 없음 — `set_nickname` / `POST /api/nickname` 을 그대로 재사용한다. 영속화/저장 추가 없음(범위 밖).
- **NFR-NICK-2**: 기존 `#name-bar` 컴포넌트(HTML/CSS) 를 재사용한다. 별도 편집 다이얼로그를 만들지 않는다.
- **NFR-NICK-3**: ✎ 버튼은 `#viewers` 렌더(`viewers` 이벤트 핸들러, 1119-1129) 와 같은 위치에서 생성/갱신한다(UI lifecycle 한 곳).

### 2.4 범위 밖

- 닉네임 서버 영속화(세션/디스크 저장). 현행 ephemeral 유지.
- 다른 viewer 의 닉네임을 변경하는 것(자기 것만).
- 닉네임 충돌/중복 검사(현재도 없음).

---

## 3. 공통 / 비기능 요구사항

- 모든 변경은 **하나의 커밋**으로 묶인다(`CLAUDE.md` 규칙 #6).
- `pytest tests/` 전체 통과, `ruff check agent_cli/ tests/` + `ruff format --check agent_cli/ tests/` 통과.
- 회귀 금지: 기존 `tests/test_web_server.py`, `tests/test_web_renderer.py` 의 통과 케이스는 그대로 유지한다. 단, 기능1로 인해 **의미가 뒤집힌** 단일 테스트(`test_jira_export_user_supplied_http_url_is_400`)는 같은 커밋에서 새 동작에 맞게 교체한다(TEST_PLAN J-3 참조). 이는 회귀가 아니라 사양 변경에 따른 의도된 갱신이다.
- 문서: `README.md`(Jira export 의 http 허용 + 경고, 닉네임 변경 방법) / `docs/ARCHITECTURE.md`(jira resolve_target 정책, web nickname 흐름, LOC 갱신) 를 같은 커밋에 포함한다.
- 기술 부채/불필요 추상화 금지(규칙 #7).

## 4. 우선순위 / 의존성

| 항목 | 우선순위 | 비고 |
|---|---|---|
| 기능1 백엔드(resolve_target scheme 완화) | P0 | 핵심 동작 변경 + 회귀 테스트 교체 |
| 기능1 프론트(http 경고) | P1 | 사용자 안전 표시 |
| 기능2 프론트(✎ → name-bar 재노출) | P0 | 사용자 대면 진입점 |

기능1 과 기능2 는 서로 독립이라 의존 관계가 없다. 하나의 PR 로 묶어 검토한다.
