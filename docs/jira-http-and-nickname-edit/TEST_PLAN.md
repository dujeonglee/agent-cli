# Jira http 허용 + 닉네임 중간 변경 — Test Plan

> Status: Draft
> Date: 2026-06-16
> Owner: architect (dev-pipeline team)
> Companion: [REQUIREMENTS.md](REQUIREMENTS.md), [DESIGN.md](DESIGN.md)

## 0. 우선순위 / 표

| ID | 영역 | 우선순위 | 자동화 | 위치 |
|---|---|---|---|---|
| J-1 | resolve_target — 미등록 http URL 허용 | P0 | O | `tests/test_jira.py`* 또는 `test_web_server.py` |
| J-2 | resolve_target — 미등록 https URL 정상(회귀) | P0 | O | 동상 |
| J-3 | export — 미등록 http URL 200 (기존 400 테스트 교체) | P0 | O | `tests/test_web_server.py` |
| J-4 | resolve_target — config-등록 http URL 신뢰 유지(회귀) | P0 | O | `tests/test_jira.py`* |
| J-5 | resolve_target — 잘못된 scheme(ftp 등) 거부 | P0 | O | 동상 |
| J-6 | resolve_target — scheme 없는 호스트 거부 | P0 | O | 동상 |
| J-7 | 거부 메시지에 http·https 둘 다 포함 | P1 | O | 동상 |
| J-8 | https 경로 바이트 동일성(회귀) | P0 | O | 기존 `test_jira_export_*` 통과 |
| W-1 | UI — http URL 입력 시 경고 노출 | P1 | △ | 수동 체크리스트 (+옵션 node) |
| W-2 | UI — https/빈 값 시 경고 숨김 | P1 | △ | 수동 체크리스트 |
| N-1 | UI — ✎ 버튼이 roster 에 자기 있을 때 노출 | P0 | △ | 수동 체크리스트 |
| N-2 | UI — ✎ 클릭 → name-bar 현재 닉네임 prefill 재노출 | P0 | △ | 수동 체크리스트 |
| N-3 | set_nickname 재호출 → roster 즉시 반영(회귀/단위) | P0 | O | `tests/test_web_renderer.py` |
| N-4 | UI — 설정 시 localStorage 갱신 + POST 재호출 | P1 | △ | 수동 체크리스트 |
| N-5 | 첫 연결 nickname prompt 회귀 없음 | P0 | △ | 수동 + 기존 단위 |
| C-1 | 회귀 — `test_web_server.py` 전체 통과 | P0 | O | pytest |
| C-2 | 회귀 — `test_web_renderer.py` 전체 통과 | P0 | O | pytest |
| C-3 | 회귀 — `pytest tests/` 전체 통과 | P0 | O | pytest |
| C-4 | 회귀 — ruff check / format | P0 | O | ruff |

자동화 컬럼: `O` = 자동, `△` = 부분(브라우저/수동 체크리스트, node 있으면 옵션 자동), `X` = 수동 전용.

\* `tests/test_jira.py` 가 없으면 `resolve_target` 단위 테스트는 `tests/test_web_server.py` 의 Jira 클래스 내 또는 신규 `tests/test_jira.py` 에 둔다(구현자 재량 — 단위 호출이 가능한 곳).

---

## 1. 기능1 백엔드 (J-*) — TDD red 대상

`resolve_target(config, target, base_url)` 직접 호출. config 는 `list_targets` 가 읽는 `{"jira": {"instances": {...}}}` 형태.

### J-1 — 미등록 http URL 허용 (★ red 우선)
```python
def test_resolve_target_user_http_allowed():
    out = jira.resolve_target({}, None, "http://jira.lan")
    assert out["base_url"] == "http://jira.lan"
    assert out["name"] == "http://jira.lan"
    assert out["deployment"] is None
```
현재 코드는 `JiraError` 를 raise하므로 이 테스트가 먼저 실패(red) → 구현 후 green.

### J-2 — 미등록 https URL 정상 (회귀)
```python
def test_resolve_target_user_https_ok():
    out = jira.resolve_target({}, None, "https://mine.atlassian.net")
    assert out["base_url"] == "https://mine.atlassian.net"
```

### J-3 — export 엔드포인트 http URL 200 (기존 400 테스트 교체)
기존 `test_jira_export_user_supplied_http_url_is_400` (`tests/test_web_server.py:1214-1227`) 은 사양이 뒤집혔으므로 **교체**한다. 새 테스트:
```python
def test_jira_export_user_supplied_http_url_ok(self, server_and_client):
    _, _, client = server_and_client
    with (
        patch("agent_cli.config.load_config", return_value={}),
        patch("agent_cli.integrations.jira.requests.post") as post,
    ):
        post.return_value = type("R", (), {"status_code": 201, "text": "{}"})()
        r = client.post(
            "/api/export/jira?token=testtoken",
            json={
                "base_url": "http://insecure.lan",
                "issue_key": "X-1",
                "deployment": "cloud",
                "entries": [{"kind": "user", "label": "User", "body": "hi"}],
                "auth": {"user": "u", "secret": "s"},
            },
        )
    assert r.status_code == 200
    assert r.json()["url"] == "http://insecure.lan/browse/X-1"
    assert post.call_args.args[0].startswith("http://insecure.lan/rest/")
```
> 주의: detect_deployment 가 네트워크를 타지 않도록 `deployment` 를 명시(`"cloud"`)한다.

### J-4 — config-등록 http URL 신뢰 유지 (회귀)
```python
def test_resolve_target_config_http_trusted():
    cfg = {"jira": {"instances": {"int": {"base_url": "http://jira.corp"}}}}
    out = jira.resolve_target(cfg, None, "http://jira.corp")
    assert out["base_url"] == "http://jira.corp"
    assert out["name"] == "int"   # config 매칭이므로 인스턴스 이름
```

### J-5 — 잘못된 scheme 거부
```python
@pytest.mark.parametrize("bad", ["ftp://jira.lan", "file:///etc", "javascript:alert(1)"])
def test_resolve_target_rejects_non_http_scheme(bad):
    with pytest.raises(jira.JiraError):
        jira.resolve_target({}, None, bad)
```

### J-6 — scheme 없는 호스트 거부
```python
def test_resolve_target_rejects_schemeless():
    with pytest.raises(jira.JiraError):
        jira.resolve_target({}, None, "jira.lan")
```

### J-7 — 거부 메시지에 http·https 둘 다 포함
```python
def test_resolve_target_error_message_mentions_both():
    with pytest.raises(jira.JiraError) as ei:
        jira.resolve_target({}, None, "ftp://x")
    msg = str(ei.value).lower()
    assert "http://" in msg and "https://" in msg
```

### J-8 — https 경로 바이트 동일성 (회귀)
기존 `test_jira_export_user_supplied_https_url_zero_config` (`:1192-1212`) 및 config 매칭 export 테스트가 **그대로 통과**해야 한다. 별도 신규 불요 — 기존 통과 유지로 검증.

---

## 2. 기능1 프론트 경고 (W-*)

JS 정규식을 Python 으로 미러링하지 않는다(이중 진실 회피 — web-fixes-3 와 동일 원칙). 수동 체크리스트 + (OS 에 node 있으면) 옵션 자동화.

### W-1 — http URL 경고 노출 (수동)
- 절차: Jira… 폼 열기 → URL 필드에 `http://jira.lan` 입력.
- 기대: `#export-jira-http-warn` 가 보이고 평문 경고 문구 표시. Send 버튼은 여전히 활성(차단 안 함).

### W-2 — https/빈 값 경고 숨김 (수동)
- 절차: URL 을 `https://mine.atlassian.net` 으로 바꾸거나 비움.
- 기대: 경고 숨김(`hidden`).
- 추가: config 인스턴스 선택으로 URL 이 https 로 채워질 때도 경고 숨김.

---

## 3. 기능2 닉네임 (N-*)

### N-3 — set_nickname 재호출 → roster 즉시 반영 (자동, 핵심 단위)
백엔드가 중간 변경을 이미 지원함을 단위로 고정(회귀 가드). 기존 `test_web_renderer.py` 의 viewers 헬퍼(`_qget`, 27-32) 패턴 사용:
```python
def test_set_nickname_rebroadcasts_updated_roster():
    r = WebRenderer()
    c = WebConnection(id="c1")
    r.register_connection(c)
    # 첫 set
    assert r.set_nickname("c1", "Alice") is True
    # 중간 변경(두 번째 호출도 허용 + 재브로드캐스트)
    assert r.set_nickname("c1", "Bob") is True
    # 마지막 viewers 이벤트에 새 이름이 보여야 함
    last = None
    while True:
        try:
            ev, data = c.queue.get_nowait()
        except Exception:
            break
        if ev == "viewers":
            last = data
    assert last is not None
    names = [v["name"] for v in last["viewers"]]
    assert "Bob" in names and "Alice" not in names
```
> conn_id 가 register 시 자동 할당된 fun nickname 을 덮어쓰는지 확인. (정확한 큐 드레인 방식은 기존 `_qget` 헬퍼 재사용 권장.)

### N-1 — ✎ 버튼 노출 (수동)
- 절차: 페이지 접속 → roster 에 "(you)" 표시 확인.
- 기대: `#rename-btn` (✎) 가 보인다. identity 수신 전에는 hidden.

### N-2 — ✎ → name-bar 재노출 + prefill (수동)
- 절차: 닉네임을 한 번 설정/스킵한 뒤 ✎ 클릭.
- 기대: `#name-bar` 가 다시 보이고, 입력 필드에 **현재 닉네임**이 prefill + 선택(select)된 상태.

### N-4 — 설정 시 localStorage + POST 재호출 (수동)
- 절차: ✎ → 새 이름 입력 → 설정(또는 Enter).
- 기대: name-bar 숨김 / roster 가 새 이름으로 갱신 / `localStorage[agentcli_nickname]` == 새 이름 / 네트워크 탭에 `POST /api/nickname` 1건.

### N-5 — 첫 연결 prompt 회귀 없음 (수동 + 단위)
- 절차: localStorage 비운 새 탭 접속.
- 기대: 기존처럼 첫 연결 시 name-bar 가 fun default prefill 로 1회 노출. ✎ 추가가 이 흐름을 깨지 않음.
- 단위: 기존 `test_web_renderer.py` 의 register/viewers/nickname 케이스가 그대로 통과(C-2).

---

## 4. 회귀 (C-*)

### C-1 / C-2 / C-3
- 우선: `pytest tests/test_web_server.py tests/test_web_renderer.py -q`.
- 전체: `pytest tests/` 통과.
- J-3 의 기존 테스트 교체 외에는 모든 기존 케이스 유지.

### C-4
- `ruff check agent_cli/ tests/` + `ruff format --check agent_cli/ tests/` 둘 다 0 exit.

---

## 5. 검증 순서 (구현자 권장)

1. **J-1** (red) → resolve_target scheme 완화 → green.
2. **J-2 / J-4 / J-5 / J-6 / J-7** → scheme 화이트리스트 전 케이스 고정.
3. **J-3** → 기존 400 테스트 교체(http 200).
4. **N-3** → 백엔드 중간 변경 지원 단위 고정.
5. **C-1..C-4** → 전체 회귀 + lint 풀 패스.
6. 프론트(W-*, N-1/N-2/N-4/N-5) → 브라우저 수동 체크리스트 1회 + 스크린샷.

---

## 6. Done 정의

- P0 자동화 항목 전부 통과(J-1..J-8 중 자동, N-3, C-1..C-4).
- P1/수동(W-*, N-1/N-2/N-4/N-5) 은 PR 본문에 체크리스트 결과/스크린샷 첨부.
- 기존 `test_jira_export_user_supplied_http_url_is_400` 는 J-3 로 교체되어 사라짐(사양 변경, 회귀 아님).
- 한 커밋에 코드 + 테스트 + README + ARCHITECTURE 함께 포함(`CLAUDE.md` #6).
