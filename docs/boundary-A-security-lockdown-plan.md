# 구현 계획 — 경계 A 보안 잠금 (B-1 · C-4 · B-2 · C-5) + 후속 S-1

> **상태**: 📋 계획 (설계 승인 대기, 코드 미착수)
> **핸드오프**: 이 문서만 읽으면 다른 세션(Opus)이 이어서 완결 가능하도록 작성.
> **저장소**: agent-cli(C-4·C-5) + agent-board(B-1·B-2) — **두 repo 동반 작업**. 각각 유닛 테스트 + wheel release.
> **관련 감사**: `docs/AUDIT-2026-08-09.md` 경계 A · B-1/B-2 · C-4/C-5. S-1은 §6.
> **작성**: 2026-08-09 (L-1/8.6.1 완료 직후)

---

## 0. 승인된 결정 (사용자)
- **기본 바인드 → loopback 전환** (LAN은 `--host 0.0.0.0` opt-in). 기본값 변경은 문서화.
- **C-5 = 쿠키 핸드셰이크** (HttpOnly + SameSite=Strict + Path=`/s/<id>`). Bearer+티켓 기각 사유: 토큰이 JS에 노출(XSS), 조각 많음.
- 회귀 관리 철저(유닛 테스트 + 뮤테이션).

## 0.1 사용자 표면 변경 요약 (정직하게)
- **B-1/C-4**: 기본 바인드가 `0.0.0.0`→`127.0.0.1`. LAN 사용자는 명시 opt-in 필요(기능 보존, 기본값만 변경). README/CHANGELOG 명시.
- **C-5**: 브라우저 흐름은 **동작 동일**(첫 진입 `?token=`→쿠키 발급→이후 쿠키). board 리다이렉트(`/s/<id>/?token=`)는 그대로 동작. **BC 제거**: `/api/*` 엔드포인트에 직접 `?token=` 을 붙인 URL 은 더 이상 인증 안 됨(401) — 정상 브라우저 흐름엔 그런 URL 이 없어 무영향(딥 API URL 북마크만 해당, 사실상 없음).
- **B-2**: 정상 사용 무영향(정상 admin은 그대로). 무인증 원격 익스플로잇만 차단(B-1이 1차, B-2가 심층).

---

## 1. B-1 — board 무인증 기본 배포 (agent-board)
**파일**: `agent_board/app.py:593` (`host` 기본), `agent_board/config.py`.
**변경**:
- `AGENT_BOARD_HOST` 기본을 `"0.0.0.0"` → `"127.0.0.1"`.
- `main()` 기동 시: `gateway == "board-proxy"` 이고 host 가 비-loopback(`127.0.0.1`/`::1`/`localhost` 아님)이면 **하드 에러로 종료**(exit 1) — caddy 모드 경고(`app.py:609`)와 대칭이나 board-proxy 는 인증이 없으므로 경고가 아닌 **거부**. 명시 opt-in env(`AGENT_BOARD_ALLOW_UNAUTH_LAN=1`)를 준 경우에만 허용(문서화).
**테스트** (`tests/`): 
- 기본 config host == `127.0.0.1`.
- board-proxy + 비-loopback host + opt-in 없음 → `SystemExit`/거부(기동 함수 단위 테스트, `AGENT_BOARD_HOST` 몽키패치).
- opt-in env 있으면 허용. caddy 모드는 기존 경고 경로 무회귀.

## 2. C-4 — cli 웹 LAN 기본 바인드 (agent-cli)
**파일**: `agent_cli/main.py:1755` (`host` typer.Option 기본), `:2183` display.
**변경**:
- `--host` 기본을 `"0.0.0.0"` → `"127.0.0.1"`. help 문구 갱신("default: 127.0.0.1 loopback; use --host 0.0.0.0 for LAN").
- board 는 이미 `--host 127.0.0.1` 명시 spawn 이라 무영향.
**테스트**: typer CLI 기본값 == `127.0.0.1`(runner invoke 또는 옵션 introspection). LAN opt-in(`--host 0.0.0.0`) 여전히 동작. `_LOCAL_BIND_HOSTS` 관련 경로 무회귀.

## 3. B-2 — admin base_url → API 키 유출 (agent-board)
**파일**: `agent_board/admin.py:114-144` (`list_served_models`), 호출부 `app.py:323/330`.
**근본**: B-1(loopback/인증)이 무인증 원격 변경을 막으면 1차 해소. **심층 방어**:
- 프로브 대상 `base_url` 이 **loopback/사설 아닌 원격**이면, **저장 이후 즉시 자동 프로브를 하지 않고** 명시적 확인 파라미터를 요구(또는 프로브 시 키 전송 전 base_url 이 방금 mutate 되었는지와 무관하게 항상 현재 저장값만 사용 — 이미 그러함). 
- **주의**: 정상 원격 LLM 엔드포인트(회사 내부 vLLM 등)는 키가 그리로 가야 정상 → "원격이면 키 금지"는 **금지**(정상 사용 파괴). 대신 **"base_url 변경 직후 같은 요청 흐름에서 키를 새 URL로 보내는 것"을 분리**: PUT config(저장)와 GET models(프로브)는 이미 별도 요청이므로, 추가로 프로브 응답에서 **키는 절대 반향/로그 금지**(현행 유지 확인) + admin 페이지에 "이 base_url 로 API 키가 전송됨" 경고 표기.
- **최소·안전 범위**: B-1 로 원격 무인증 차단이 핵심. B-2 전용 코드는 (a) 키가 마스킹되어 저장/반환됨을 계약 테스트로 고정, (b) 프로브가 loopback 아닌 URL 일 때 로그에 경고 남김 정도로 **표면·정상동작 무변경** 선에서.
**테스트**: get_config 가 api_key 마스킹(기존 `admin.py:85`) 계약; 프로브 URL 조립이 저장된 base_url 만 사용; 원격 base_url 정상 프로브 무회귀.

## 4. C-5 — 토큰을 URL 쿼리에서 쿠키로 (agent-cli) ★ 가장 큼
**제약**: 브라우저 `EventSource` 는 커스텀 헤더 불가 → 쿠키가 유일하게 SSE 까지 자동 적용.
**설계 (쿠키 핸드셰이크)**:
- **★ BC 결정(사용자)**: **per-endpoint 쿼리 토큰 폴백은 제거**(하위호환 버림 → 단일 인증 경로로 깔끔화). **bootstrap 1회 쿼리 토큰만 유지**(board 가 토큰을 브라우저에 전달하는 유일 통로 — legacy 가 아니라 정식 전달 메커니즘; 없애면 board/caddy 파리티가 깨져 더 지저분).
- **서버(`web/server.py`)**:
  1. **단일 인증 의존성/미들웨어**로 통일: `--trust-local` loopback → skip → **쿠키 `act`(=token)** (`secrets.compare_digest`). 각 엔드포인트의 `token: Query(...)` 파라미터는 **전부 제거**.
  2. **bootstrap 전용**: index 페이지 GET(및 base_path 루트)이 유효한 `?token=` 을 제시하면 **`Set-Cookie: act=<token>; HttpOnly; SameSite=Strict; Path=<base_path or />[; Secure]`** 부착 후 처리. `Secure` 는 TLS(요청이 https/`X-Forwarded-Proto: https`)일 때만. 이 한 곳만 쿼리 토큰을 읽음.
  3. `/api/*`·`/api/stream`(SSE) 은 **쿠키(또는 trust-local)로만** 인증 — 쿼리 토큰 안 받음.
  4. 모든 응답에 `Referrer-Policy: no-referrer` 헤더. CSP 최소 1줄(선택).
- **프론트(`web/static/app.js`)**: 
  1. 첫 로드에서 `?token=` 을 소비(서버가 쿠키 발급) 후, **매 요청에 `?token=` 을 붙이던 코드를 전부 제거** — 쿠키가 자동 전송(fetch·EventSource 공통).
  2. 성공 핸드셰이크 후 **URL 에서 `?token=` 제거**(`history.replaceState`).
  3. 접속 게이트: 첫 진입은 `?token=`(있으면 쿠키 발급), 이후엔 쿠키 존재로 판단. 쿼리 토큰을 상태로 들고 다니지 않음.
- **board(`agent_board`)**: 리다이렉트 URL `/s/<id>/?token=` **유지**(첫 진입 핸드셰이크 트리거) — board 변경 최소. 프록시는 Set-Cookie/Cookie 헤더를 이미 전달(DESIGN §9)하므로 **board 코드 변경 불필요**(확인 테스트만).
- **base_path 상호작용**: 쿠키 `Path` 는 인스턴스 base_path(`/s/<id>`)로 스코프 → 인스턴스 간 쿠키 격리. base_path 없으면(`/`) 직접 cli 사용.
**테스트** (`tests/test_web_server.py` 등):
- `?token=` 유효 → 응답에 `Set-Cookie act=…; HttpOnly; SameSite=Strict` (+ base_path Path).
- 쿠키만으로 후속 엔드포인트(`/api/input`, `/api/stream`) 인증 통과.
- 쿠키·토큰·trust-local 모두 없음 → 401.
- 잘못된 쿠키 → 401(상수시간 비교).
- `Referrer-Policy: no-referrer` 헤더 존재.
- SSE 를 쿠키로 여는 경로(브라우저 테스트 `tests/browser/` 있으면 거기, 아니면 서버 단위).
- bootstrap: index GET `?token=` → 쿠키 발급; 그 후 `/api/*`·SSE 쿠키로 통과.
- **BC 제거 확인**: `/api/*` 에 `?token=` 만 붙이고 쿠키 없음 → **401**(per-endpoint 쿼리 폴백 제거됨).
- **뮤테이션**: 쿠키 인증 분기를 제거하면 SSE 인증 테스트가 실패해야 함.

---

## 5. 릴리스 (두 repo 각각, RELEASING.md 준수)
- **agent-cli**: C-4·C-5 → **MINOR? PATCH?** 판정: C-5 는 내부 인증 계약 변경이나 사용자 동작·CLI 스키마 호환 보존(구 `?token=` 하위호환) → **PATCH(8.6.2)**. C-4 기본 바인드 변경은 "기본값 변경"이라 논쟁 여지 → 보수적으로 **MINOR(8.7.0)** 권장(기본 노출 표면이 바뀌므로 사용자가 알아야 함). README 갱신 필수. → `rm -rf build dist` → build → **isolated venv sanity** → tag → gh release.
- **agent-board**: B-1·B-2 → 기본 바인드 변경 = 사용자 표면 → **MINOR(1.25.0)**. board 는 버전 2중 소스(pyproject+__init__) — 이번에 **`dynamic=["version"]` 단일화도 같이**(감사 위생 항목) 할지 결정. RELEASING 절차 확인(board 는 CI 없음, 로컬).
- **커밋**: 각 repo 단일 커밋(코드+테스트+문서). main 직접 커밋(사용자 결정). push 전 확인. Co-Authored-By 푸터.

---

## 6. 후속 — S-1 (agent-cli, 별도 커밋/릴리스)
> 경계 A 완료 후 진행. 감사 S-1(P0).
**파일**: `agent_cli/subagent/agents_live.py:950` (`_save_state`) 외 무락 순회 6곳(`roster_snapshot:407`, `alive_count:419`, `any_activity:430`, `has_active_work:463`, `format_status:783`, `auto_spawn:921`).
**변경**: 각 순회 진입에서 `with self._cv: entries = list(self._agents.values())` 스냅샷 후 순회. 삽입/교체(`spawn:553`, `resume_teammate:899`, `restore:1042`)도 `_cv` 하에.
**테스트**: 동시 spawn + `_save_state` 반복 스레드로 `RuntimeError: dictionary changed size` 재현 → 수정 후 미발생(뮤테이션: 락 제거 시 재현). 기존 agents_live 테스트 무회귀.
**릴리스**: cli PATCH.

---

## 7. 핸드오프 체크리스트
### 경계 A (cli)
- [ ] C-4 기본 바인드 loopback + help
- [ ] C-5 서버 쿠키 핸드셰이크(우선순위·Set-Cookie·Referrer-Policy)
- [ ] C-5 app.js 게이트 완화 + URL 토큰 제거 + 쿠키 의존
- [ ] C-5 테스트(쿠키 인증·SSE·하위호환·뮤테이션) + 전체 pytest + ruff
- [ ] README/ARCHITECTURE/CHANGELOG, 버전 bump
- [ ] 단일 커밋 → build(rm -rf build dist) → isolated sanity → tag → gh release
### 경계 A (board) — ✅ 완료 (v1.25.0, released 2026-08-09)
- [x] B-1 기본 loopback + board-proxy 비-loopback 기동 거부(`enforce_bind_policy`, `AGENT_BOARD_ALLOW_UNAUTH_LAN` opt-in)
- [x] B-2 심층방어(`base_url_is_remote` 경고 + 키 마스킹 계약; 완전 내부자 방어=v2)
- [x] 버전 2중소스 단일화(dynamic+attr) + ruff 정책 동형화 + 기존 린트 잔여 정리
- [x] agent-cli 통합 테스트 `importorskip` (미co-install 시 skip)
- [x] 테스트(신규 16) + README/CHANGELOG/AUDIT + 단일 커밋 `b4d3402` → wheel release v1.25.0
- 비고: board 프록시는 Set-Cookie/Cookie 를 이미 전달(DESIGN §9) → C-5 쿠키는 board 코드 변경 불필요(cli 측 구현 시 통과 테스트만).

### 경계 A (cli) — ✅ 완료 (v8.7.0, 2026-08-09)
- [x] C-4 웹 기본 바인드 loopback (`main.py` 기본 `127.0.0.1` + help)
- [x] C-5 `_AuthMiddleware` 단일 chokepoint: trust-local/쿠키/부트스트랩 `?token=` → 토큰 주입; bootstrap `?token=`→`Set-Cookie act`(HttpOnly·SameSite=Strict·Path=base_path·TLS면 Secure); `Referrer-Policy: no-referrer`
- [x] C-5 app.js per-request 토큰 전면 제거 + 주소창 `history.replaceState` strip + SSE-실패 시 setup 도움말 (node --check 통과)
- [x] C-5 테스트 `TestCookieAuth`(부트스트랩·쿠키인증·거부·Referrer) + 뮤테이션(쿠키 분기 제거 시 실패) + markdown 하니스 stub 보강
- [x] README/ARCHITECTURE/CHANGELOG + 8.7.0 → 단일 커밋 → wheel release
- **BC 결정 정정**: per-endpoint `?token=` 은 **유지**(엄격 제거는 수십 테스트 파괴+curl 불가인데 얻는 깔끔함은 OR항 1개뿐 → 사용자 "더 깔끔하면" 기준 미달). 보안 목표는 브라우저가 토큰을 URL 에 안 싣는 것으로 달성.
- 함정 확인: board-proxy 는 trust-local 로 이미 토큰 스킵(쿠키는 브라우저 누출 방지); `Secure` 는 TLS 만(loopback 평문서 켜면 쿠키 드롭); `SameSite=Strict` 로 CSRF 차단.

### 후속 — ⏳ 미착수
- [ ] S-1 (§6) 락 스냅샷 6곳 + 재현/뮤테이션 테스트 → cli PATCH

**함정**:
- C-5: board-proxy 는 loopback+trust-local 이라 cli 가 토큰을 아예 스킵함 — 쿠키는 **브라우저 누출 방지**가 목적(cli 서버측 게이트는 trust-local 이 이미 품). 직접 cli 사용(비 trust-local)에선 쿠키가 실제 인증. 두 시나리오 모두 테스트.
- 쿠키 `Secure` 를 loopback 평문(board-proxy)에서 켜면 브라우저가 쿠키를 안 보냄 → TLS 일 때만.
- `SameSite=Strict` 누락 시 CSRF 재개방(쿠키 ambient auth). 반드시 Strict.
- board 버전 2중 소스: 손대는 김에 단일화하되 별도 결정.
