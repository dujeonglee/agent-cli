# Agent-CLI

![License: MIT](https://img.shields.io/badge/License-MIT-brightgreen.svg)
![Python](https://img.shields.io/badge/python-3.10+-blue.svg)

ReAct 패턴 기반 에이전트 CLI. 멀티 프로바이더(OpenAI, Anthropic) 지원, on-premise LLM 최적화.

## 설치

Python 3.10+ 필요.

```bash
# 릴리스 설치 (태그된 버전)
pip install "git+ssh://git@github.com/dujeonglee/agent-cli.git@v2.0.0"

# 웹 UI 포함
pip install "agent-cli[web] @ git+ssh://git@github.com/dujeonglee/agent-cli.git@v2.0.0"

# 개발 모드
git clone git@github.com:dujeonglee/agent-cli.git
cd agent-cli
pip install -e ".[dev]"
```

설치 후 `agent-cli` 명령어가 PATH에 등록됩니다. 처음 실행하면 설정 마법사가 자동으로 시작됩니다.
버전 확인은 `agent-cli --version`. GitHub Release에 첨부된 wheel(`pip install agent_cli-*.whl`)로도 설치할 수 있습니다.

### 업데이트

```bash
agent-cli update          # 최신 릴리스 확인 → 확인 후 설치
agent-cli update --check  # 새 버전 있는지 확인만 (설치 안 함)
```

GitHub CLI(`gh`)로 최신 릴리스 태그를 확인하고(private repo 인증은 `gh` 로그인이 처리 — 토큰 설정 불필요), 릴리스에 첨부된 wheel을 받아 `pip`로 업그레이드합니다. 개발용 editable 설치(`pip install -e .`)에서는 덮어쓰지 않고 `git pull`을 안내합니다(`--force`로 우회).

## 빠른 시작

```bash
# OpenAI 호환 (기본 프로바이더, 로컬 omlx/vLLM 등)
agent-cli run "List files in the current directory"

# 특정 모델 지정
agent-cli run "Read README.md" -m gpt-4o

# OpenAI
agent-cli run "Analyze this project" -p openai

# Anthropic
agent-cli run "Read README.md and summarize" -p anthropic

# 직접 셸 명령 (LLM 없이)
agent-cli run "/sh ls -la"

# 스킬 실행
agent-cli run "/review-code src/auth.py"
agent-cli run "/summarize README.md"

# 도움말
agent-cli --help
agent-cli run --help
agent-cli web --help
agent-cli sessions --help
```

## 설정

### 첫 실행 (Setup Wizard)

설정 파일(`config.json`)이 없으면 자동으로 설정 마법사가 시작됩니다:

```
$ agent-cli run "hello"

No configuration found. Starting setup wizard...

╭─ Agent-CLI Setup ─────────────────────────────────────╮
│                                                        │
╰─ ReAct pattern agent CLI for on-premise LLMs ─────────╯

1. Select LLM Provider
   [1] OpenAI compatible (OpenAI, vLLM, LM Studio, omlx) — default
   [2] Anthropic

2. Connection → base URL, API key, 연결 테스트
3. Model → OpenAI 호환·Anthropic 모두 `/v1/models`(provider별 인증 헤더)로 자동 탐색 및 목록 선택, 실패 시 직접 입력(OpenAI 기본 `gpt-4o`, Anthropic 기본 `claude-sonnet-4-20250514`)
4. Review → 설정 확인 후 저장 위치 선택
```

수동으로 다시 실행:
```bash
agent-cli setup
```

### config.json

설정은 JSON 파일로 저장됩니다:

```json
{
  "provider": "openai",
  "base_url": "http://127.0.0.1:8000/v1",
  "api_key": "",
  "default_model": "gpt-4o"
}
```

#### Jira export (선택)

웹 UI의 Export 기능(아래)에서 Jira 코멘트로 내보냅니다. **config 는 선택입니다** — config 없이도 웹 UI 에서 base_url·계정·토큰을 직접 입력해 게시할 수 있습니다. 자주 쓰는 사이트는 config 에 등록해두면 드롭다운으로 뜨고 URL 이 미리 채워집니다(**여러 인스턴스** 가능). config 에는 **`base_url` 만** 둡니다 — 자격증명은 서버에 저장하지 않고, 코멘트를 다는 **각 사용자가 웹 UI에서 본인 계정으로 입력**합니다(그래서 코멘트 작성자가 서버 계정이 아니라 그 사용자 본인이 됩니다):

```json
{
  "jira": {
    "instances": {
      "work": {"base_url": "https://work.atlassian.net"},
      "dc":   {"base_url": "https://jira.corp.net", "deployment": "server"}
    },
    "default": "work"
  }
}
```

- **UI 에서 URL 직접 입력(옵셔널)**: config 에 등록된 인스턴스는 드롭다운으로 고르면 URL 이 채워지고, 그 자리에서 수정하거나 새 URL 을 직접 타이핑할 수도 있습니다(localStorage 에 마지막 URL·계정 기억). config 에 없는 직접 입력 URL 은 `http://` 와 `https://` 둘 다 허용하므로 **사내 평문 HTTP Jira** 도 그대로 쓸 수 있습니다 — 다만 `http://` URL 을 입력하면 자격증명이 평문으로 전송됨을 알리는 ⚠️ **경고가 폼에 표시**됩니다(차단이 아니라 정보성; 신뢰된 네트워크에서만 사용하세요). `http`/`https` 외 scheme(또는 scheme 없는 값)은 거부됩니다.
- **Cloud / Server·DC 자동 판별**: `deployment` 을 생략하면 서버가 `{base_url}/rest/api/2/serverInfo` 를 프로브해 자동 판별하고(웹 UI가 알맞은 입력 필드를 미리 선택), `"cloud"` / `"server"` 로 명시해 프로브를 건너뛸 수도 있습니다. UI 의 토글로 사용자가 직접 바꿀 수도 있습니다.
- **자격증명 입력**: Cloud 는 `email` + `API token`(Atlassian 계정 설정에서 발급), Server/Data Center 는 `username` + `password`(또는 PAT). 입력값은 **그 브라우저의 localStorage 에만** 저장되어 다음 접속 때 자동 채워지고, 코멘트 POST 한 번에만 transient 하게 쓰입니다(서버 로그·세션에 남지 않음). ⚠️ 웹 UI 는 LAN 평문 HTTP 이므로 신뢰된 네트워크에서만 사용하세요.
- Jira Cloud 무료 티어(≤10명)로도 동작합니다.

### 설정 우선순위

높은 게 낮은 걸 덮어씁니다 (필드 단위 병합):

| 우선순위 | 위치 | 용도 |
|---------|------|------|
| 1 (최고) | CLI 파라미터 (`-p`, `-m`, `--base-url`, `--api-key`) | 임시 오버라이드 |
| 2 | `.agent-cli/config.json` (프로젝트) | 워크스페이스별 설정 |
| 3 | `~/.agent-cli/config.json` (사용자) | 전역 기본 설정 |
| 4 (최저) | 환경변수 | 시스템 레벨 |

예: `~/.agent-cli/config.json`에 `gpt-4o`가 기본이지만 `-m gpt-4o-mini`로 임시 실행:
```bash
agent-cli run "task" -m gpt-4o-mini
```

### DIRECTIVE.md — 프로젝트 지시사항

에이전트가 항상 따라야 하는 규칙을 `DIRECTIVE.md` 파일에 작성하면 매 세션의 시스템 프롬프트에 자동 주입됩니다.

| 경로 | 용도 |
|------|------|
| `.agent-cli/DIRECTIVE.md` | 프로젝트별 규칙 (코딩 컨벤션, 테스트 정책 등) |
| `~/.agent-cli/DIRECTIVE.md` | 사용자 전역 규칙 (응답 언어, 개인 선호 등) |

- 두 파일 모두 존재하면 **둘 다 로드** (프로젝트 먼저, 유저 전역 뒤에)
- 동일 내용이면 중복 제거
- 파일당 최대 4,000자, 전체 최대 8,000자 (초과 시 잘림)

예시 (`.agent-cli/DIRECTIVE.md`):
```markdown
# 코드 규칙
- 코드 수정 시 관련 유닛 테스트를 반드시 추가한다
- ruff check와 ruff format을 통과해야 한다
- Python 3.10+ 호환을 유지한다
```

### 환경변수

| 변수 | config.json 키 | 설명 |
|------|---------------|------|
| `AGENT_CLI_PROVIDER` | `provider` | LLM 프로바이더 |
| `AGENT_CLI_BASE_URL` | `base_url` | API 엔드포인트 |
| `AGENT_CLI_API_KEY` | `api_key` | API 키 |
| `AGENT_CLI_MODEL` | `default_model` | 기본 모델 |
| `ANTHROPIC_API_KEY` | — | Anthropic API 키 (기존 호환) |
| `OPENAI_API_KEY` | — | OpenAI API 키 (기존 호환) |
| `AGENT_CLI_NO_READLINE` | — | readline 비활성화 |
| `AGENT_CLI_LLM_RETRY_ATTEMPTS` | — | LLM 요청 총 시도 횟수 (기본 10 = 최초 + 재시도 9회). Timeout / ConnectionError에만 적용. 1로 설정하면 재시도 비활성. **스트리밍**: post timeout `(connect 30초, read 30초)` 로 **헤더 대기·헤더 구간 interrupt 를 30초로 바운드**(broken 서버의 ~20분 행 제거) → 헤더 수신 후 소켓을 patient 로 리셋해 body 는 느긋. body 가 **30초** 무토큰이면 UI 에 대기 알림(`응답 대기 중 — …`), **20틱(10분) 연속 침묵**이면 연결 끊고 재전송(최대 3회). 토큰 오면 카운터 리셋. **비스트리밍**: `(30, 1200)` (전체 생성 read). interrupt 는 body 구간 ~8초, 헤더 구간 ≤30초. |
| `AGENT_CLI_LLM_RETRY_DELAY` | — | 재시도 간 대기 시간(초, 기본 1.0). 지수 백오프 안 씀 (on-prem 단일 사용자 전제). |

## 모델 권장 사양

에이전트 루프는 JSON 포맷 준수 + 도구 선택 + 멀티스텝 reasoning을 동시에 요구합니다. 모델 크기에 따라 성능 차이가 큽니다:

| 모델 크기 | 멀티스텝 태스크 | 단순 질의 | 비고 |
|-----------|---------------|----------|------|
| **7B 이하** | ❌ | △ | 도구 혼동, JSON 포맷 불안정, 반복 실패 빈번 |
| **14-30B** | △ | ✅ | 간단한 도구 사용 가능, 복잡한 스킬은 불안정 |
| **32B+** | ✅ | ✅ | 안정적 — 권장 최소 사양 |
| **70B+** | ✅✅ | ✅ | delegate, 복잡한 스킬 등 고급 기능 안정 |

**최소: 30B, 권장: 32B+**

권장 사양은 OpenAI 호환 서버로 서빙하는 로컬/온프렘 모델 기준입니다. 32B+ 클래스 모델에서 멀티스텝 태스크가 안정적으로 동작합니다.

## 명령어

### `run` — 단발 실행

```bash
agent-cli run "task description" [options]
```

| 옵션 | 설명 | 기본값 |
|------|------|--------|
| `-p, --provider` | `openai` / `anthropic` | `openai` |
| `-m, --model` | 모델 ID | 프로바이더 기본값 |
| `--base-url` | API 엔드포인트 | 프로바이더 기본값 |
| `--api-key` | API 키 (환경 변수 자동 감지) | |
| `-n, --max-turns` | 최대 턴 (0=무제한) | `0` |
| `--max-depth` | 중첩 깊이 (delegate + skill 합산). 한계 도달 시 두 도구 모두 자동 비활성. | `2` |
| `--delegate-timeout` | 서브에이전트 타임아웃 (초) | `300` |
| `-v, --verbose` | 원시 LLM 응답 + thinking 블록 + 컨텍스트 덤프 표시 | |
| `--style` | 렌더러 스타일 (minimal 또는 커스텀) | `minimal` |
| `--record-turns / --no-record-turns` | 세션 디렉토리에 `turns.jsonl` 기록 (회복률 통계용 메타데이터; prompt·응답 본문 미포함) | `--record-turns` |
| `--no-compaction` | 토큰 budget 90% 초과 시 LLM 요약 압축 비활성. 평소대로 플레인 FIFO drop. `AGENT_CLI_COMPACTION=off` 환경 변수도 같은 효과 (env가 flag보다 우선). | `false` |
| `--response-format` | Wire format 플러그인 이름. 빌트인: `md_array` (**기본** — 멀티-op: `## Thought`/`## Action` + flat `{action, params}` op 배열로 한 턴에 여러 독립 도구 호출, 종료는 `complete` op. Phase-2 bakeoff 95.2%=react + 실전 150턴 형식실패 0.7%로 검증 후 기본 전환), `react` (순수 JSON `{thought, action, action_input}`). 두 포맷 compliance 는 omlx 27B/35B bakeoff에서 동등. `agent_cli/wire_formats/`에 모듈을 추가하면 자동 등록. 미등록 이름은 LLM 호출 전에 즉시 실패 | `md_array` |

`run` 실행 후 세션이 자동 저장됩니다. `web --resume <id>`로 이어서 작업할 수 있습니다:

```bash
$ agent-cli run "Analyze the project structure"
# ... 실행 결과 ...
# Session 1774752167 saved. Resume with: agent-cli web --resume 1774752167

$ agent-cli web --resume 1774752167   # 이전 작업 이어서 (브라우저 UI)
```

resume 시 `web`은 이전 대화(turn)를 UI에 그대로 재생해 어디서 끊겼는지 바로 확인할 수 있습니다 (중간 도구 호출/관찰 포함).

### `web` — LAN 웹 UI (실험)

```bash
pip install 'agent-cli[web]'   # 옵션 의존성 설치 (FastAPI, uvicorn)
agent-cli web [-p openai] [-m model] [--port N] [--token <hex>]
agent-cli web --resume <session_id>    # 이전 세션 이어서 시작
```

agent-cli 인스턴스 하나를 단일 세션으로 LAN에 노출. 자동 토큰 생성(또는 `--token` 지정). **다중 뷰어 (모두 동등)**: 모든 탭이 스트림을 보고 **모두 입력·큐 가능**(controller/observer 구분 없음). 각 탭은 접속 시 **재미있는 기본 닉네임**이 채워진 입력으로 이름을 정할 수 있고(✕로 기본값 유지, 한 번 정하면 다음 접속에 기억), 접속자 로스터에 닉네임이 표시됩니다. 닉네임은 접속 후에도 로스터 옆 **✎ 버튼**으로 언제든 다시 열어 변경할 수 있습니다(현재 닉네임이 채워진 채 재노출 → 즉시 로스터에 반영). **메시지 큐**: 에이전트가 실행 중이어도 메시지를 보내면 **큐에 쌓여 실시간 표시**되고, 매 턴 종료 시 하나씩 디큐되어 대화에 주입(steering)됩니다. 자기가 큐한 메시지는 처리 전 ✕로 취소 가능. 모든 사용자 요청은 닉네임 라벨로 task 로그에 누적됩니다. 시작 시 토큰 포함 URL이 출력되고 (선택적으로) 브라우저 자동 오픈.

| 옵션 | 설명 | 기본값 |
|---|---|---|
| `--host` | bind 주소 | `0.0.0.0` (LAN) |
| `--port` | listen 포트. 생략 시 8080 우선 시도 후 사용 중이면 OS가 빈 포트 자동 할당. 명시(`--port 9090`) 시 그 포트로만 바인딩 (충돌 시 에러). 실제 URL은 시작 시 출력됨. | 자동 (8080 → 빈 포트) |
| `--token` | 인증 토큰 | 자동 생성 (32 byte URL-safe) |
| `--no-browser` | 브라우저 자동 open 비활성 | `false` |
| `--resume <id>` | 이전 세션 이어서 실행 (`agent-cli sessions`로 ID 확인) | — |
| 기타 (`-p`, `-m`, `-n`, `--max-depth` 등) | `run`과 동일 | |

UI 기능:
- 좌측 어시스턴트 카드 (markdown 렌더링: 헤더 `#`/`##`/`###`, GFM 파이프 표, 순서/비순서 리스트, **bold**/*italic*, 인라인 코드, 펜스 코드 블록) + 우측 사용자 bubble
- 도구 호출(action) / 결과(observation) 인라인 카드, ✓/✗ 상태 표시
- 각 카드 모서리에 시각 표시 (`YYMMDD HH:MM:SS`, 마우스 hover 시 전체 날짜+밀리초). delegate/skill 내부 카드도 동일. `--resume` 로 이어서 보면 카드들은 **실제 발생 시각**(history 기록 기준)으로 표시되어 재접속 시점과 혼동되지 않음
- 실시간 스트리밍 (점선 카드로 토큰 누적 → 최종 카드로 교체)
- 컨텍스트 동기화: 오래된 turn이 LLM 컨텍스트에서 밀려나거나 compaction으로 요약되면 UI에서도 제거
- 여러 탭/PC가 접속하면 모두 동등하게 입력 가능 (접속자는 닉네임 로스터에 표시)
- ANSWERING 모드: `ask` 도구 호출 시 질문 텍스트가 입력창 위에 표시되어 스크롤 없이 답변
- **토큰 현황 표시**: 상단 info bar 옆에 `ctx 5.2K/256K (2%) · ↑5.2K ↓320 · Σ↓1.8K` 형태로 매 turn 갱신 — 현재 context 사용률(%), 이번 turn 의 in/out, 세션 누적 output. 새로고침 후에도 유지(SSE snapshot). CLI(`run`)도 동일 정보를 매 turn 한 줄로 표시(`in: … | out: … | ctx: … | Σout: …`). `usage.input_tokens`(서버 실측)를 받아 `renderer.token_usage` 로 추상화 → CLI/web 공통
- **Send → Stop → Stopping… 토글**: 사용자 메시지 전송 후 worker 가 응답을 처리하는 동안 Send 버튼이 빨간 **Stop** 버튼으로 바뀝니다. 클릭하면 즉시 **Stopping…**(비활성)으로 바뀌어 중복 클릭을 막고, 진행 중인 turn 을 안전하게 중단 (CLI 의 Ctrl+C 와 같은 `stop_event` 경로 → `POST /api/stop`). LLM 생성 도중이면 스트림을 즉시 끊고 미완성 응답을 폐기하며, 도구 실행 중이면 그 스텝을 마친 뒤 멈춥니다. turn 이 끝나면 다시 Send 로 복귀. 중단은 `[interrupt]` observation 으로 기록되어 다음 입력에서 이어집니다. 두 번째 메시지가 in-flight turn 에 끼어드는 것도 자연히 차단 (Enter 는 stop 을 트리거하지 않음 — 버튼 전용). **새로고침 / 재접속 후에도 상태 유지** (서버가 last worker state 를 SSE snapshot 에 prepend). prompt 모드(ask 답변)에선 항상 Send, confirm 모드는 별도 버튼. 일반 chat·`/skill`·`@agent`(delegate) 실행 모두 Stop 으로 중단됩니다 (delegate 는 병렬 worker 가 같은 `stop_event` 를 공유).

**⚡ Prompt Inspector:** 헤더의 ⚡ 버튼으로 우측 드로어를 열면 **현재 턴에 실제로 전송된 시스템 프롬프트**를 섹션별로 확인할 수 있습니다. 상단의 토큰 예산 스택바(섹션별 색·비율), 섹션 아코디언(이름·토큰 뱃지·본문), 검색 필터를 제공합니다. 열 때마다 최신 LLM 호출의 스냅샷을 가져오며(`GET /api/debug/prompt`, 토큰 인증), 훅이 주입한 동적 섹션도 `Hook: <이름>`으로 표시됩니다. 컨텍스트 압축(compaction)이 일어나면 그 요약과 파일 목록도 `⊙ Compaction summary / Files touched (user-injected)` 섹션으로 함께 보여, 모델이 실제로 받는 압축 컨텍스트를 검사할 수 있습니다. **정적 시스템 프롬프트 아래에는 `── 동적 컨텍스트 (대화 · 관찰) ──` 구분선과 함께 현재 컨텍스트 윈도우에 든 대화·관찰(`ctx.get_messages()` 의 system 제외분)이 메시지별 섹션으로 표시**되어, 시스템 프롬프트뿐 아니라 LLM 이 실제로 받는 전체 입력을 검사할 수 있습니다(메인 스코프 한정). **첫 메시지 전에도 채워집니다** — 시작 시 시스템 프롬프트를 미리 캡처하고(`Hook:` 동적 섹션은 첫 LLM 호출 후 채워짐), `--resume` 면 복원된 대화도 드로어를 열자마자 보입니다. **에이전트별 스코프**: delegate 서브에이전트가 돌면 드로어 상단에 `[Main] [explorer·1] [coder·2]` 칩 row가 나타나, 칩을 클릭하면 해당 서브에이전트가 실제로 받은 시스템 프롬프트로 전환되고 `Main`을 누르면 메인으로 돌아옵니다. 끝난 서브에이전트의 프롬프트도 사후 검사를 위해 남아 있으며, 칩의 ✕로 개별 제거할 수 있습니다(Main은 제거 불가).

**📤 Export:** 헤더의 📤 버튼으로 **선택 모드**에 들어가면 각 대화 카드 좌측에 체크박스가 나타납니다(기본 전부 해제). 원하는 카드를 고르거나 `All`로 전체 선택한 뒤(하단 액션바에 선택 개수 표시), 두 가지로 내보낼 수 있습니다:
> - **⬇ HTML** — 선택한 대화를 self-contained HTML 파일로 다운로드(스타일 인라인, 어디서나 열림).
> - **Jira…** — 선택한 대화를 한 개의 **Jira 코멘트**로, **본인 계정으로** 게시. 인스턴스 드롭다운(설정 시) + **base URL**(설정값 prefill, 직접 입력·수정 가능) + Cloud/Server 토글 + 본인 계정·토큰(또는 username·password) + issue key(예: `PROJ-123`) 입력 후 Send. URL·자격증명은 브라우저 localStorage 에 기억됩니다. Cloud 는 ADF, Server/DC 는 wiki 마크업으로 전송. 설정은 위 [Jira export](#jira-export-선택) 참조.

**📥 Download:** 헤더의 📥 버튼으로 우측 드로어를 열면 **워크스페이스 파일 트리**가 나타납니다(디렉토리는 ▶로 펼쳐 하위 탐색, 파일·디렉토리 옆에 크기 표시). 원하는 파일/디렉토리를 체크하거나 `All`(워크스페이스 전체)로 선택한 뒤 **⬇ Download zip** 을 누르면 선택 항목이 임시 zip으로 압축되어 다운로드됩니다(전송 후 서버의 임시 파일은 삭제). 디렉토리를 고르면 그 하위 전체가, 파일을 고르면 그 파일만 담깁니다. 워크스페이스(서버 실행 디렉토리) 밖 경로는 서버가 차단합니다.

**종료 (Ctrl+C):** 한 번의 Ctrl+C로 깨끗하게 종료됩니다. uvicorn의 lifespan shutdown 훅이 활성 SSE 연결을 정리하고, 백그라운드 worker는 `SHUTDOWN` sentinel로 깨어나 빠져나가며, 세션이 자동 저장됩니다. `agent-cli web --resume <session_id>`로 이어서 실행하면 이전 turn들이 SSE snapshot으로 재생되어 UI에 그대로 복원됩니다.

CLI parity 명령어:
- `/help` — 웹 모드 명령어 안내
- `/sh <command>` — LLM 우회 셸 실행
- `/skills` — 사용 가능한 스킬 목록
- `/<skill> <args>` — 스킬 직접 실행 (예: `/optimize ./`)
- `@agents` — 사용 가능한 에이전트 목록
- `@<agent> <task>` — 에이전트에게 작업 위임

curl로도 직접 사용 가능:
```bash
curl -N "http://localhost:8080/api/stream?token=<TOKEN>"
curl -X POST "http://localhost:8080/api/input?token=<TOKEN>" \
  -H 'Content-Type: application/json' \
  -d '{"kind":"chat","content":"hello"}'
```

### `setup` — 설정 마법사

```bash
agent-cli setup
```

프로바이더, 접속 정보, 기본 모델을 대화형으로 설정합니다. 설정이 없을 때 자동으로 실행되며, 언제든 수동으로 다시 실행할 수 있습니다.

### `sessions` — 세션 관리

`run`과 `web` 모두 세션을 `.agent-cli/sessions/{session_id}/`에 자동 저장합니다. 세션 종료 시 컨텍스트 윈도우 내용이 요약으로 저장됩니다. `--resume`으로 이전 세션을 이어서 작업할 수 있습니다.

```bash
# 현재 워크스페이스의 세션 목록
agent-cli sessions

# 특정 워크스페이스의 세션 목록
agent-cli sessions --workspace /path/to/project

# 이전 세션 이어서 작업
agent-cli web -p openai -m gpt-4o --resume <session_id>
```

각 세션은 id·시각과 함께 **마지막 사용자 요청(↳)** 과 **마지막 결과(→)** 를 한눈에 보여줍니다 (아직 끝나지 않은 run 은 `→ (in progress)`). 이 요약은 세션의 `history.jsonl` 에서 마지막 user↔complete 페어를 읽어 만듭니다 (별도의 `query` 메타 필드는 제거됨).

```text
Sessions for /path/to/project:

  1774752167 2026-06-04 10:31:02
      ↳ Analyze the project structure
      → src/, tests/, docs/ 3개 최상위 디렉토리로 구성된…
```

| 옵션 | 설명 | 기본값 |
|------|------|--------|
| `-w, --workspace` | 워크스페이스 경로 필터 | 현재 디렉토리 |

**`--resume` 없이 시작할 때:** `web` 을 `--resume` 없이 실행하면, 가장 최근 세션을 위 포맷으로 보여주고 `Resume it? [y/N]` 를 묻습니다. `y` 면 그 세션을 이어가고, 그 외(Enter 포함)는 새 세션으로 시작합니다 (안전한 기본값). 파이프/비대화 환경(stdin 이 TTY 아님)에서는 묻지 않고 항상 새 세션입니다.

LLM은 `read_context` 도구로 현재 또는 이전 세션의 이력을 **SQL 로 질의**할 수 있습니다 (history 테이블에 `SELECT` — kind/tools/files/author/turn/text 컬럼, 읽기전용).

## 스킬 (Prompt Skills)

특정 작업에 최적화된 재사용 가능한 프롬프트 템플릿. Claude Code 스킬 포맷과 호환.

### 패키지 내장 스킬 (built-in)

패키지와 함께 배포되는 메타 스킬:

| 스킬 | 설명 |
|------|------|
| `/create-skill <name>` | 새 스킬 파일을 대화형으로 생성 (SKILL.md + scripts/) |
| `/create-agent <name>` | 새 에이전트 정의 파일을 대화형으로 생성 |
| `/plan <feature>` | 기능 요청을 작업 분해 + 의존성 + 범위 추정으로 구조화하여 `plan/` 에 저장 |
| `/create-team <goal>` | 에이전트 팀 구성 — 도메인 분석, 아키텍처 설계, 에이전트/스킬/오케스트레이터 생성 |

사용자가 같은 이름의 스킬을 `.agent-cli/skills/`에 만들면 built-in을 오버라이드합니다.

### 프로젝트 스킬 예시

```bash
# 코드 리뷰
agent-cli run "/review-code src/auth.py"

# 파일 요약
agent-cli run "/summarize README.md"

# 유닛 테스트 생성
agent-cli run "/test src/utils.py"

# 코드 최적화 분석
agent-cli run "/optimize ./agent_cli"

# 대화형(web) 모드에서도 사용 가능
/review-code src/auth.py
/skills   # 사용 가능한 스킬 목록
```

### 커스텀 스킬 작성

`/create-skill my-skill` 명령으로 대화형 생성하거나, `.agent-cli/skills/my-skill.md` 파일을 직접 만들면 `/my-skill` 명령어가 자동 등록됩니다:

```markdown
---
name: my-skill
description: What this skill does
allowed-tools: [read_file, shell]
max-turns: 5
argument-hint: "<file_path>"
---

Your custom prompt template here. Use $ARGUMENTS for user input.
$0, $1 or $ARGUMENTS[0], $ARGUMENTS[1] for individual arguments (0-based).
${CLAUDE_SKILL_DIR} for the skill's directory path.
${SESSION_ID} for the current session ID.
!`command` for dynamic context injection (shell command output).
```

| Frontmatter 필드 | 설명 | 필수 |
|-----------------|------|------|
| `name` | 슬래시 명령어 이름 (미지정 시 파일명) | |
| `description` | 스킬 설명 | ✓ |
| `allowed-tools` | 허용 도구 리스트 (미지정 시 전체) | |
| `max-turns` | 최대 턴 (미지정 시 글로벌 설정 사용) | |
| `model` | 스킬 실행 시 모델 오버라이드 (미지정 시 현재 모델 사용) | |
| `context` | `fork`이면 독립 컨텍스트에서 실행 (부모 대화 히스토리 없음) | |
| `hooks` | 스킬 스코프 lifecycle hooks (PreToolUse, PostToolUse 등) | |
| `disable-model-invocation` | `true`이면 LLM 자동 호출 금지 (사용자만 `/명령`으로 호출 가능) | |
| `user-invocable` | `false`이면 `/skills` 메뉴에서 숨김 (LLM만 호출 가능) | |
| `argument-hint` | `/skills` 표시 시 인자 힌트 | |

스킬 검색 경로:
1. `.agent-cli/skills/*.md` (프로젝트 로컬 플랫, 우선)
2. `.agent-cli/skills/<name>/SKILL.md` (프로젝트 로컬 디렉토리)
3. `~/.agent-cli/skills/*.md` (사용자 전역 플랫)
4. `~/.agent-cli/skills/<name>/SKILL.md` (사용자 전역 디렉토리)

같은 검색 경로 내에서 동일 이름의 플랫 파일과 디렉토리 스킬이 모두 존재하면 에러가 발생합니다.

## Hooks

에이전트 라이프사이클 전반에 걸쳐 동작하는 확장 시스템입니다. **Python hook**과 **Shell hook** 두 가지 방식을 지원합니다.

### Python Hooks

`.agent-cli/hooks/*.py` (프로젝트) 또는 `~/.agent-cli/hooks/*.py` (유저 전역):

```python
# .agent-cli/hooks/00_memory.py
EVENTS = ["OnSessionStart", "OnTurnEnd", "OnSessionEnd"]

def on_session_start(ctx):
    """세션 시작 시 관련 메모리 로드."""
    results = ctx.search_memory("project context")
    if results:
        ctx.inject_system_section("Memory", format_memories(results))

def on_turn_end(ctx):
    """중요 결정을 메모리에 저장."""
    if ctx.messages:
        last = ctx.messages[-1].get("content", "")
        if "edit_file" in last:
            ctx.store_memory([{
                "name": f"change_turn_{ctx.turn}",
                "entityType": "file_change",
                "observations": [last[:200]],
            }])

def on_session_end(ctx):
    """세션 요약 저장."""
    ctx.store_memory([{
        "name": f"session_{ctx.session_dir.name}",
        "entityType": "session",
        "observations": [f"Completed {ctx.turn} turns"],
    }])
```

**파일 규칙:**
- 숫자 prefix 순서 실행 (`00_` → `10_` → `20_`)
- 프���젝트 hooks가 유저 hooks보다 먼저
- `EVENTS` 리스트로 구독할 이벤트 선언
- 에러 시 해당 hook 건너뜀 (에이전트 루프 중단 없음)

### Hook 이벤트 (11개)

| 이벤트 | 시점 | Python | Shell |
|--------|------|--------|-------|
| `OnSessionStart` | 세션 시작 | ✓ | |
| `PreLLMCall` | LLM 호출 직전 (매 턴) | ✓ | |
| `PostLLMCall` | LLM 응답 수신 후 | ✓ | |
| `PreToolUse` | 도구 실행 직전 | ✓ block/modify | ✓ exit 2=block |
| `PostToolUse` | 도구 실행 직후 | ✓ | ✓ |
| `OnTurnEnd` | ��� 종료 후 | ✓ | |
| `OnDelegateStart` | delegate 실행 직전 | ✓ | |
| `OnDelegateEnd` | delegate 완료 후 | ✓ | |
| `OnSkillStart` | skill 실행 직전 | ✓ | |
| `OnSkillEnd` | skill 완료 후 | ✓ | |
| `OnSessionEnd` | 세션 종료 | ✓ | |

실행 순서: **Python hooks → Shell hooks** (같은 이벤트 내)

### HookContext

Python hook 함수가 받는 컨텍스�� 객체:

```python
def pre_llm_call(ctx):
    ctx.event          # 이벤트 이름 ("PreLLMCall")
    ctx.messages       # 현재 cache messages (읽기/쓰기 — compaction/FIFO 후 상태)
    ctx.turn           # 현재 턴 번호
    ctx.session_dir    # 세션 디렉토리 Path
    ctx.tool_name      # PreToolUse/PostToolUse 시 도구 이름
    ctx.tool_input     # PreToolUse 시 도구 입력
    ctx.tool_result    # PostToolUse 시 ToolResult
    ctx.llm_response   # PostLLMCall 시 LLM 응답 텍스트

    # Context 조작
    ctx.inject_message("system", "remember this")
    ctx.inject_system_section("Memory", "facts...")
    ctx.remove_system_section("Memory")

    # 도구 제어 (PreToolUse only)
    ctx.block("reason")
    ctx.modify_input({"command": "ls"})

    # MCP 메모리 (MCP memory 서버 연결 시)
    ctx.store_memory([{"name": "...", "entityType": "...", "observations": [...]}])
    ctx.search_memory("query")
    ctx.read_memory()
```

### Shell Hooks (기존 방식)

`.agent-cli/hooks.json`:

```json
{
  "PreToolUse": [
    {
      "matcher": "shell",
      "hooks": [{"command": "./block-dangerous.sh", "timeout": 30}]
    }
  ],
  "PostToolUse": [
    {
      "matcher": "edit_file",
      "hooks": [{"command": "ruff format $(cat | jq -r '.tool_input.path')"}]
    }
  ]
}
```

- stdin으로 JSON 전달: `{"hook_event_name", "tool_name", "tool_input", "tool_result"}`
- `matcher`: 도구 이름 regex (빈 문자열 = 모든 도구)
- exit 0 = 통과, exit 2 = 차단 (PreToolUse만)
- stdout JSON의 `updatedInput`으로 도구 인자 수정 가능 (PreToolUse만)

### 스킬 내 hooks

스킬 frontmatter에서도 shell hooks를 정의할 수 있습니다 (해당 스킬 실행 중에만 활성):

```yaml
---
name: safe-deploy
description: Deploy safely
hooks:
  PreToolUse:
    - matcher: shell
      hooks:
        - command: "./validate-deploy.sh"
---
```

## 프로바이더 설정

### OpenAI 호환 (기본)

```bash
# 로컬/온프렘 서버 (omlx, vLLM, LM Studio 등)
agent-cli run "task" --base-url http://127.0.0.1:8000/v1 -m my-model

# 호스티드 OpenAI
export OPENAI_API_KEY="sk-..."
agent-cli run "task" -p openai --base-url https://api.openai.com/v1 -m gpt-4o
```

### Anthropic

```bash
export ANTHROPIC_API_KEY="sk-ant-..."
agent-cli run "task" -p anthropic
```

System prompt에 자동으로 prompt cache(`cache_control: ephemeral`)가 적용된다.
한 세션에서 두 번째 콜부터 시스템 프롬프트가 캐시 히트하여 입력 비용을 90% 절감한다.
캐시 hit/write 토큰은 turn summary에 표시된다.

## 모델 레지스트리

`models.json`에 모델별 능력치를 정의합니다:

```json
{
  "models": {
    "gpt-4o": {
      "provider": "openai",
      "context_window": 32768,
      "max_output_tokens": 4096,
      "supports_structured_output": true,
      "supports_thinking": true,
      "thinking_budget": 4096,
      "supports_strict_schema": false,
      "thinking_format": "think"
    }
  }
}
```

### 파일 위치 및 정책

| 우선순위 | 위치 | 역할 | 자동 저장 |
|---------|------|------|----------|
| 1 | `.agent-cli/models.json` | 프로젝트 로컬 오버라이드 | 안 함 (읽기만) |
| 2 | `~/.agent-cli/models.json` | 사용자 전역 설정 | 새 모델 자동 저장 |
| 3 | `agent_cli/default_models.json` | 패키지 기본값 | 안 함 (읽기만) |

- 미등록 모델은 런타임 자동 감지 → `~/.agent-cli/models.json`에 저장
  - OpenAI 호환: context window를 `/v1/models`의 `max_model_len`에서 읽고, 값이 없으면 컨텍스트 오버플로 프로브로 추정. 추가로 thinking 지원 여부를 프로브.
  - Thinking 감지: 프로브 프롬프트 → `message.thinking` 필드 또는 `<think>` 태그 확인 (하드코딩 없이 자동)
  - Structured-output 감지: `response_format={"type":"json_object"}` 프로브로 `supports_structured_output`, 이어서 strict `json_schema` 프로브로 `supports_strict_schema` 판정. 산문이 자연스러운 프롬프트로 요청해 반환값이 유효 JSON(스키마 준수)일 때만 지원으로 인정(서버가 `response_format`을 무시하는 경우 오탐 방지). 프로브 실패 시 보수적으로 미지원 처리.
  - 자동 산출 규칙: `max_output = context_window // 4`. context window가 16K(`MIN_CONTEXT_WINDOW`) 미만이면 `UnsupportedModelError`로 거부.
- 런타임 감지도 실패하면 사용자에게 대화형으로 context window, thinking 지원 여부를 질문 → `~/.agent-cli/models.json`에 저장
- 이미 등록된 모델은 덮어쓰지 않음 (사용자 설정 보호)
- 다음 실행 시 저장된 설정에서 로딩 (프로브/질문 재실행 없음)

| 필드 | 설명 |
|------|------|
| `context_window` | 컨텍스트 윈도우 크기 (토큰) |
| `max_output_tokens` | 최대 출력 토큰 |
| `supports_structured_output` | Basic JSON mode 사용 가능 (OpenAI `response_format={"type":"json_object"}` / Anthropic tool calling) |
| `supports_thinking` | Thinking/reasoning 지원 |
| `thinking_budget` | Thinking 토큰 예산 |
| `thinking_format` | Thinking 블록 태그 (`"think"`, `""`) |
| `supports_strict_schema` | (현재 미사용, dormant) strict JSON Schema 표식 — 현재 어떤 provider도 이 플래그로 동작 분기 안 함 |

**설정 우선순위**: `.agent-cli/models.json` (프로젝트) > `~/.agent-cli/models.json` (전역) > `default_models.json` (패키지) > 런타임 감지 > 보수적 기본값

## 도구

LLM이 사용할 수 있는 도구 목록:

| 도구 | 설명 |
|------|------|
| `read_file` | 파일 읽기 (hashline 태그, flat-native — 한 op=한 파일). 여러 파일은 멀티-op 으로 read_file op 을 여러 개 emit |
| `write_file` | 파일 생성/덮어쓰기. 작성 content 를 hashline 으로 반환 (read_file 없이 edit_file 직결) |
| `edit_file` | hashline 기반 정밀 편집 (퍼지 매칭 지원) |
| `shell` | 셸 명령 실행 |
| `fetch` | 웹 페이지를 가져와 마크다운으로 변환 (재귀 fetch 지원) |
| `delegate` | 서브에이전트에 작업 위임 (한 op=한 task; 여러 delegate op = 병렬, 에이전트 역할 지정 가능) |
| `read_context` | 세션 이력 SQL 질의 (history 테이블 SELECT: kind/tools/files/author/turn/text) |
| `code_index` | tree-sitter 기반 SQLite 코드 인덱스 (읽기 전용, flat-native — 한 op=한 query). 여러 query 는 멀티-op 으로 (모드 섞기 가능). lazy build + sha1 incremental + edit/write post-hook 자동 갱신. 10 mode: `list`/`fetch`/`lookup`/`kind`/`file`/`refs`/`callers`/`callees`/`slice`/`build`. Python/JS/TS/C/C++/Go/Rust/Java/Markdown |
| `complete` | 작업 완료 신호 (최종 결과 반환) |
| `ask` | 사용자에게 질문하고 대기 (대화형; 질문 하나=op 하나, 여러 질문은 ask op 여러 개로 배치 — read_file 식) |
| `run_skill` | 등록된 스킬 실행 (LLM이 자동으로 호출 가능) |
| `ready_for_review` | 완료 직전 자가 검증 — 원본 query를 observation으로 받아 누락 점검 후 `complete` 호출 |

### action_input 키 네이밍 규칙

**모든 builtin 도구가 flat-native**입니다 (consolidation Step 3 완료) — `action_input` 에 표준 키를 그대로 씁니다: 파일 도구(`read_file`/`write_file`/`edit_file`)·`code_index`는 `{path/mode, ...}`, `delegate`는 `{task, ...}`, `shell`은 `{command, ...}`, 제어 도구(`complete`/`ask`/`run_skill`/`ready_for_review`)도 표준 키. 한 op = 한 대상이고, 여러 대상(여러 파일 읽기, 여러 쿼리, **여러 병렬 서브에이전트**)은 한 턴에 **op 을 여러 개** 냅니다.

어떤 builtin 도 wire-key prefix 를 쓰지 않습니다. wire-key prefix(`{tool}_{param}`) 메커니즘 — 모델이 `action` 이름을 빠뜨려도 키 모양으로 도구를 복구(dropped-action recovery) — 은 **미래 prefixed 도구/포맷용 latent seam** 으로 코드에 남아 있고, 현재 어떤 builtin 도 활성화하지 않습니다. MCP/외부 도구는 **prefix-less**(자체 bare 스키마)라 이 메커니즘과 무관합니다.

> 아래 예시들은 각 도구의 표준 키 그대로입니다 — flat-native 라 그대로 전송하면 됩니다.

### read_file — 파일 읽기

한 번에 **파일 하나**를 읽습니다 (flat-native). 모드(부분 범위 / 검색 / stat)를 고를 수 있고, 각 줄에 `LINE#HASH:content` hashline 태그가 붙습니다. 여러 파일은 멀티-op 포맷에서 read_file op 을 여러 개 emit 해 한 턴에 읽습니다 (op 배열이 곧 배치 — 중첩 배열 없음).

```json
{"action": "read_file", "action_input": {"path": "src/main.py"}}
```

부분 범위 / 검색 / stat 모드:

```json
{"action": "read_file", "action_input": {"path": "src/main.py", "line_start": 100, "line_end": 200}}
{"action": "read_file", "action_input": {"path": "agent_cli/loop.py", "search": "_handle_ask", "context": 3}}
{"action": "read_file", "action_input": {"path": "config.py", "stat": true}}
```

- 필드 (`path` 만 필수):
  - `path`: 읽을 파일 경로.
  - `line_start` / `line_end`: 1-based, inclusive. 둘 다 생략하면 full read.
  - `search`: 정규식 패턴. 매칭 줄 + 주변 `context` 줄을 반환 (기본 5).
  - `stat`: 파일 크기/총 줄 수 + 앞 20줄 (메타데이터 조회, read 아님).

### edit_file — Hashline 편집

`read_file`에서 받은 hashline 태그를 사용하여 정밀 편집합니다. **한 op = 한 편집** (flat-native):

```
1#VR:def hello():
2#KT:    return "world"
3#ZZ:
```

각 op 의 action_input (`path` + 편집 필드):

```json
{"path": "app.py", "op": "replace", "pos": "2#KT", "lines": ["    return \"hello\""]}
{"path": "app.py", "op": "replace", "pos": "1#VR", "end": "3#ZZ", "lines": ["def greet():", "    pass"]}
{"path": "app.py", "op": "append", "pos": "1#VR", "lines": ["    # comment"]}
```

여러 편집은 멀티-op 으로 edit_file op 을 여러 개 보냅니다. 단 hashline ref 는 줄번호+해시 앵커라 앞 편집이 줄을 밀면 뒤 편집의 ref 가 어긋납니다 — **같은 파일 다중편집은 턴을 나눠** 사이에 re-read 하고, 다른 파일 편집은 같은 턴에 여러 op 로 보내도 됩니다.

해시 불일치 시 퍼지 매칭으로 자동 보정합니다 (공백/따옴표/대시 정규화).

`edit_file` 성공 시 응답에 **변경 사항 unified diff** 가 포함됩니다 (`+` 녹색 / `-` 빨강, 100줄 초과 시 truncate). `write_file` 성공 시엔 작성한 content 가 **hashline(LINE#HASH:content)** 포맷으로 반환되어, `read_file` 없이 방금 쓴 파일을 바로 `edit_file` 로 수정할 수 있습니다 (write→edit 직결 — 작은 변경 시 전체 재작성 대신 부분 edit 유도).

### complete — 작업 완료

LLM이 작업을 완료했을 때 호출하는 가상 도구입니다. `result` 필드에 최종 답변을 담습니다.

```json
{"action": "complete", "action_input": {"result": "작업이 완료되었습니다. 파일을 생성했습니다."}}
```

### read_context — 세션 이력 조회

이전 또는 현재 세션의 이력을 **SQL 로 질의**합니다. LLM이 context window 밖으로 evict/compaction된 정보가 필요할 때 자발적으로 사용합니다. history.jsonl 이 인메모리 `history` 테이블로 적재되고, LLM이 `SELECT` 를 작성합니다(읽기전용).

테이블 `history` (레코드 1개 = 1행): `session`, `loc`, `seq`, `kind`(query/action/observation/final/raw/system), `turn`, `ts`, `tools`(툴명), `files`(조작 파일 경로), `author`(닉네임), `text`(검색·내용 표면), `content`(원본 — spill 레코드는 JSON).

**과대 출력 spill 회수:** 도구 출력이 50K 토큰을 넘으면(예: 레포 전체 `find`, 전 심볼 `code_index` 덤프) 컨텍스트엔 head + 회수법을 담은 **guide 만** 들어가고, 전체는 history 에 **무손실 청크**로 보존됩니다. 필요한 청크만 `json_extract` 로 가져옵니다 — guide 가 정확한 쿼리를 알려줍니다:
```json
{"action": "read_context", "action_input": {"query": "SELECT json_extract(content,'$.output[2]') FROM history WHERE turn=14 AND json_valid(content) AND json_extract(content,'$.spill')=1"}}
```

```json
// 스키마 + 예시 + 세션 목록 보기 (query 생략)
{"action": "read_context", "action_input": {}}

// auth.py 를 건드린 관측만
{"action": "read_context", "action_input": {"query": "SELECT loc, turn, text FROM history WHERE kind='observation' AND files LIKE '%auth.py%'"}}

// 특정 사용자(웹 멀티유저)의 질문만
{"action": "read_context", "action_input": {"query": "SELECT text FROM history WHERE kind='query' AND author='두정'"}}

// 키워드 + 턴 범위 + 정렬/제한
{"action": "read_context", "action_input": {"query": "SELECT loc, text FROM history WHERE text LIKE '%인증%' AND turn>=5 ORDER BY turn LIMIT 20"}}

// 다른 세션까지 — sessions 로 적재 범위 지정(all 또는 특정 id)
{"action": "read_context", "action_input": {"query": "SELECT DISTINCT session FROM history", "sessions": "all"}}
```

**읽기전용**: `SELECT`/`WITH` 만 허용(쓰기·DDL 거부), 결과 최대 50행(필요시 `LIMIT`/조건으로 좁히기). `sessions` 미지정 시 현재 세션(delegate/skill subdir 포함).

**fetch는 search와 정반대로 cap 없음** — 모델이 의도적으로 부른 회상이라 multi-line/대용량 observation도 그대로 반환. 다중 loc는 all-or-nothing (하나라도 잘못되면 전체 실패).

### code_index — SQLite 코드 인덱스

tree-sitter로 프로젝트 전체를 파싱해 `<project_root>/.agent-cli/code_index.db`에 영구 SQLite 인덱스를 만듭니다. 첫 query 시 lazy build, 이후 query마다 sha1 비교로 변경 파일만 incremental rebuild. `edit_file` / `write_file` 성공 시 자동 post-hook이 인덱스 갱신 → 모델이 직접 `mode='build'` 호출할 필요 거의 없음.

`read_file`이 텍스트(line range)에 답한다면 `code_index`는 의미 단위(symbol)와 cross-file 관계(refs/callers/callees)에 답합니다.

#### 한 op = 한 query (flat-native)

code_index 는 읽기 전용(파일 안 씀)이고, **한 op 가 한 query**를 돌립니다 (flat). 여러 query 는 멀티-op 으로 code_index op 을 여러 개 보냅니다(읽기전용이라 순서/상태 의존 없음 — 모드 섞기 OK).

```json
// 단일 query
{"action": "code_index", "action_input": {"mode": "fetch", "path": "agent_cli/loop.py", "name": "AgentLoop._call_llm"}}
```

#### 10 mode (각 op 의 action_input)

아래는 각 op 의 action_input 형태입니다 — 한 op 가 하나의 query:

```json
// 1. 파일 outline (read_file:stat의 구조 인지 대안)
{"mode": "list", "path": "agent_cli/loop.py"}

// 2. 단일 심볼 body (hashline 포맷 → edit_file 직결)
{"mode": "fetch", "path": "agent_cli/loop.py", "name": "AgentLoop._call_llm"}
{"mode": "fetch", "path": "README.md", "name": "## Setup"}

// 3. 이름으로 심볼 찾기 (인덱스 전역)
{"mode": "lookup", "name": "AgentLoop"}
{"mode": "lookup", "name": "Setup", "symbol_kind": "section"}

// 4. 특정 kind 심볼 전부 (function/type/variable/constant/section)
{"mode": "kind", "symbol_kind": "function"}

// 5. 한 파일의 모든 심볼 (재파싱 없이 인덱스 조회)
{"mode": "file", "path": "agent_cli/loop.py"}

// 6. 참조 사이트 (call=호출, name=콜백/포인터, type=타입 위치)
{"mode": "refs", "name": "AgentLoop._call_llm", "ref_kind": "call"}

// 7. 누가 호출하나
{"mode": "callers", "name": "process"}

// 8. 무엇을 호출하나
{"mode": "callees", "name": "process"}

// 9. LLM 컨텍스트용 markdown blob (정의 + 선택적 callees/callers/types/macros)
{"mode": "slice", "name": "process", "with_callees": true, "with_types": true, "depth": 2}

// 10. 전체 rebuild 강제 (드묾 — 보통 lazy build + post-hook으로 충분)
{"mode": "build"}
```

#### 인덱스 root 결정 + 가지치기

- **Root**: cwd 또는 가장 가까운 조상 디렉토리 중 `.agent-cli/` 가 있는 곳. 없으면 cwd 사용 (`.agent-cli/` 자동 생성).
- **DB**: `<root>/.agent-cli/code_index.db`. `.gitignore` 기본 패턴이 이미 `.agent-cli/` 를 덮음.
- **SQLite 백엔드 자동 폴백 (Linux)**: stdlib `sqlite3` 가 없는 CPython 빌드 (예: `--without-sqlite` 로 빌드된 잠금 서버) 에선 `agent_cli/code_index/_sqlite.py` shim 이 `pysqlite3-binary` 휠로 자동 폴백. macOS / Windows 는 stdlib `sqlite3` 가 사실상 항상 존재해서 wheel 설치 X — pyproject 의 `sys_platform == 'linux'` marker 가 Linux 에서만 폴백 휠을 끌어들임.
- **자동 prune 디렉토리**: `.git`/`.hg`/`.svn`, `.agent-cli`/`.claude`, `.venv`/`venv`/`env`, `__pycache__`/`.pytest_cache`/`.ruff_cache`/`.mypy_cache`, `node_modules`, `build`/`dist`/`target`, `.tox`. 인덱스 폭주 방지.

#### Scope 경계 — index-scoped vs per-file

- `lookup` / `kind` / `file` / `refs` / `callers` / `callees` / `slice`: **인덱스 root 안에서만** 동작. 바깥 path 주면 명시적 에러.
- `list` / `fetch`: root 안이면 인덱스 조회, **root 바깥이면 on-demand parse fallback** (한 파일만 즉석 파싱, DB 갱신 없음). `/tmp/scratch.py` 같은 ad-hoc 파일도 동작.

#### 표기 관습

- Python·JS·TS: `Class.method`
- C·C++·Rust: `namespace::Class::method` (또는 `Type::method`)
- Markdown: `Setup` 또는 `## Setup` (양쪽 다 fetch 동작)

#### 선언 vs 정의

같은 이름이 헤더의 prototype과 .cpp의 정의에 모두 있으면 fetch는 *정의*를 반환합니다. 선언만 있으면 그 선언을 `[declaration]` 표시와 함께 반환. Rust trait body의 method signature, Java interface method, C prototype 모두 `is_definition=False` 로 별도 기록됩니다.

#### hashline 출력 (`mode='fetch'`)

fetch 결과의 body는 `read_file`과 동일한 hashline 포맷(`LINE#HASH:content`)으로 반환됩니다. 따라서 fetch 결과를 그대로 `edit_file`에 넘길 수 있고, 다시 read하지 않아도 됩니다.

#### 지원 언어 (9개)

| 언어 | 확장자 |
|---|---|
| Python | `.py`, `.pyi` |
| JavaScript | `.js`, `.jsx`, `.mjs`, `.cjs` |
| TypeScript | `.ts`, `.tsx` |
| C | `.c`, `.h` |
| C++ | `.cpp`, `.cc`, `.cxx`, `.hpp`, `.hh`, `.hxx`, `.h++` |
| Go | `.go` |
| Rust | `.rs` |
| Java | `.java` |
| Markdown | `.md`, `.markdown` (heading → `kind='section'`) |

C/C++는 dedicated grammar 각각 사용. 그 외 형식은 `read_file` 으로.

#### C/C++ 전처리 — defconfig (kernel/driver 필수)

C/C++ 코드의 `#define` / `#ifdef` 분기 처리는 **번들된 pure-Python `_unifdef.py`** 가 기본 수행합니다 — 별도 설치 불필요. 시스템에 `unifdef` 바이너리가 있으면 (`brew install unifdef` / `apt install unifdef`) 자동으로 그것을 우선 사용 (battle-tested C 구현). 명시적 강제는 `AGENT_CLI_UNIFDEF=pure|system|auto` 환경변수.

**기능상 차이 없음** — 두 백엔드는 ifdef/elif/else/endif + `defined()`/논리/비교/산술 표현식에서 byte-identical 출력 (parity 테스트로 보장).

함수 시그니처가 `#ifdef CONFIG_X` 로 분기되는 코드 (커널 드라이버 등에서 흔함):

```c
#ifdef CONFIG_SOMETHING
void foo(int x)
#else
static void foo(int x)
#endif
{ ... body ... }
```

은 tree-sitter가 ERROR로 파싱해 정의가 인덱스에서 누락됩니다. 이 경우 `<project_root>/.agent-cli/defconfig` 에 `#define`/`#undef` 줄을 적으면 `unifdef -b` 가 분기를 사전에 잘라 정의가 살아납니다:

```
# .agent-cli/defconfig
#define CONFIG_SOMETHING
#undef CONFIG_LEGACY
```

파일은 사용자가 직접 작성합니다 (LLM이 추측하면 잘못된 분기를 인덱싱할 위험). `code_index` tool은 첫 query 시 이 파일이 있으면 자동으로 unifdef 에 전달합니다 — 별도 옵션 불필요. `mode='build'` 출력에 `defconfig:` 라인으로 적용 여부가 보입니다.

C/C++ 사용자도 시스템 `unifdef` 설치 선택 사항 — 번들된 pure-Python 으로 동일 동작. Python/JS/TS/Go/Rust/Java/Markdown 만 쓰는 사용자는 전처리 단계 자체가 no-op.

### ask — 사용자에게 질문 (대화형 web 전용)

LLM이 추가 정보가 필요할 때 사용자에게 질문합니다. 배열로 여러 질문을 한 번에 할 수 있습니다.

```json
{"action": "ask", "action_input": {"questions": ["어떤 파일을 수정할까요?"]}}
{"action": "ask", "action_input": {"questions": ["파일 경로는?", "사용할 언어는?"]}}
```

### shell — 셸 명령 실행

셸 명령을 실행하고 stdout/stderr를 반환합니다. 타임아웃 기본 30초.

```json
{"action": "shell", "action_input": {"command": "find agent_cli -name '*.py' | wc -l"}}
```

shell 출력은 자르지 않고 그대로 LLM에 전달됩니다. `find /` / `grep -r` 같은 큰 명령을 호출하면 컨텍스트가 그만큼 차지되니, 필요한 부분만 받도록 좁히는 명령을 권장 (`tail -n 100`, `grep ERROR`, `head -c 4096` 등). 누적 컨텍스트가 budget의 90%를 넘으면 compaction이 발동해 오래된 절반을 LLM 요약으로 흡수하고, 그 단계에서도 안 들어가면 플레인 FIFO로 떨어뜨립니다.

**위험 명령 확인.** `rm` / `rmdir` / `mv` 가 명령에 포함되면 실행 전 사용자에게 묻습니다:

```
⚠ Dangerous command detected:
  $ rm -rf /tmp/build
Allow? (y=once, n=deny, a=always allow `rm` this session)
  [y/n/a, optional comment after]:
```

응답 첫 토큰이 결정 (`y` 이번만 / `n` 거부 / `a` 이 세션 동안 같은 키워드 자동 허용), 뒤에 **선택적 코멘트** 추가 가능:

- `y and also cleanup /tmp/cache next` — 명령 실행 + 코멘트가 출력 끝에 `[User note when approving: ...]` 로 붙어서 LLM 이 다음 액션에 반영
- `n the path is wrong, try /tmp/build instead` — 거부 + 이유가 에러 메시지에 들어가서 LLM 이 다른 경로 탐색
- `a only inside /tmp` — 세션 allowlist 추가 + 코멘트 전달
- 빈 응답 / 인식 안 되는 첫 토큰 → 거부 (전체 입력은 코멘트로 보존)

첫 토큰은 별칭도 인식합니다 — `y`(yes/ok/okay/yep/yeah/sure), `a`(always/**allow**), `n`(no/nope) — 자연스러운 긍정이 안전 기본값 거부로 오인되지 않게. (`allow` 는 옵션 라벨이 "always allow" 이므로 `a` 로 매핑.)

**누가/왜 묻는지 표시.** delegate(서브에이전트)에서 올라온 확인/질문에는 **에이전트 라벨 + reasoning(thought)**(확인은 실행하려는 **action**까지)이 함께 표시됩니다 — CLI는 프롬프트 위 `↳ from [explorer] · 💭 … · ⚡ …` 헤더, 웹은 확인 다이얼로그/답변 영역에 같은 정보. 메인 에이전트는 thought/action 이 이미 인라인이라 헤더 생략.

기본 활성. 비활성하려면 `AGENT_CLI_DANGEROUS_SHELL_CONFIRM=0`. 확인을 띄울 수 있는지는 **렌더러가 판단**합니다 — CLI는 터미널(TTY), 웹은 연결된 브라우저(SSE 다이얼로그, TTY 불필요). 어느 쪽으로도 물어볼 수 없는 무인 환경(TTY 없는 CI/배치 + 미접속)에서는 자동 거부 — 위험 명령이 silent 실행되는 일 없음. parallel delegate처럼 여러 작업이 동시에 도는 경우에도 확인은 **직렬화**되어 한 번에 하나만 떠서, 응답이 엉뚱한 작업으로 새지 않음. shlex 토큰 단위 매칭이라 `rm-helper.sh`나 `echo "rm files"` 같은 false positive는 안 잡지만 `bash -c "rm x"` 처럼 wrapper 안의 위험 명령은 놓칠 수 있음 (확인 시 모델에 알려서 풀어쓰게 유도).

### write_file — 파일 생성

새 파일을 생성하거나 기존 파일을 덮어씁니다. 작성한 content 는 hashline 포맷으로 반환되어 `read_file` 없이 바로 `edit_file` 수정이 가능합니다 — 기존 파일의 작은 변경은 전체 재작성 대신 `edit_file` 권장.

```json
{"action": "write_file", "action_input": {"path": "output.txt", "content": "hello world"}}
```

### delegate — 서브에이전트 위임

작업을 in-process 서브에이전트에 위임합니다. **한 op = 한 task** (flat-native). 컨텍스트 모드로 서브에이전트가 부모 맥락을 얼마나 알지 제어합니다:

| 모드 | 동작 |
|------|------|
| `none` (기본) | 독립 실행. task에 모든 정보 포함 필요 |
| `fork` | 부모 컨텍스트를 복사하여 실행. 맥락 인지 + 독립 |

```json
{"action": "delegate", "action_input": {"task": "Read /tmp/data.csv and count rows"}}
{"action": "delegate", "action_input": {"task": "Fix the bug we found", "context": "fork"}}
{"action": "delegate", "action_input": {"task": "Review changes", "context": "fork", "tools": ["read_file", "shell"]}}
{"action": "delegate", "action_input": {"task": "Review this code for vulnerabilities", "agent": "security-reviewer"}}
```

**병렬 실행**: 한 턴에 delegate op 을 **여러 개** 내면 독립 서브에이전트가 **동시에**(threading) 실행됩니다 — 루프가 그 op 들을 모아 병렬 디스패치합니다. 독립 작업일 때만 여러 개를 내고, task B가 task A의 결과에 의존하면 A만 먼저 낸 뒤 다음 턴에 그 결과로 B를 호출하세요.

`tools` 파라미터로 서브에이전트가 사용할 수 있는 도구를 제한할 수 있습니다.

`agent` 파라미터로 에이전트 역할을 로드할 수 있습니다. 에이전트 파일은 YAML frontmatter로 `allowed-tools`, `model` 등을 설정하고, 본문에 역할/원칙을 정의합니다. 검색 경로: 프로젝트(`.agent-cli/agents/`) → 유저 전역(`~/.agent-cli/agents/`) → 패키지 내장(`agent_cli/agents/builtin/`).

패키지 내장 에이전트:

| 에이전트 | 설명 |
|---------|------|
| `explorer` | 읽기 전용 코드베이스 탐색 (read_file, shell만 사용, 파일 수정 불가) |

**산출물 포맷**: delegate 실행 결과는 구조화된 형식으로 반환됩니다:

```
STATUS: success
RESULT:
(서브에이전트 출력)

[Subagent activity]
- iter 1: read_file auth.py
- iter 2: shell pytest
- iter 3: edit_file auth.py

[Files touched]
- Read: auth.py, config.py
- Modified: auth.py

[Duration: 12.3s] [Subagent used 3 iterations]
```

실패 시에는 `[Last actions before failure]` 섹션이 추가되어 디버깅에 필요한 마지막 액션과 에러 메시지를 확인할 수 있습니다. 결과는 delegate subdir의 `result.md`에 자동 저장됩니다.

### run_skill — 스킬 실행

등록된 스킬을 도구로 호출합니다. LLM이 스스로 판단하여 적절한 스킬을 선택합니다. 시스템 프롬프트에 사용 가능한 스킬 목록이 안내됩니다.

```json
{"action": "run_skill", "action_input": {"name": "optimize", "arguments": "./"}}
{"action": "run_skill", "action_input": {"name": "summarize", "arguments": "README.md"}}
```

스킬 내부에서는 별도 ReAct 루프가 실행되며, 결과가 artifact로 저장됩니다. `disable-model-invocation: true`인 스킬은 LLM이 호출할 수 없습니다.

결과 포맷:
```
STATUS: success
RESULT:
SKILL: summarize(./)
The agent-cli directory contains a ReAct pattern-based agent CLI...

[Internal skill calls during this execution:]
- run_skill(optimize): Task completed: Analysis done.
```

`SKILL:` 헤더로 실행된 스킬과 인자를 식별하고, `[Internal skill calls]`로 내부에서 호출된 스킬 이력을 표시합니다.

#### 스킬 스택 (재귀 방지)

스킬 내부에서 다른 스킬을 호출할 수 있지만 재귀는 차단됩니다:

- `A→B` 허용: summarize 내부에서 optimize 호출 가능
- `A→A` 차단: summarize 내부에서 summarize 호출 불가
- `A→B→A` 차단: 순환 호출 불가
- Execution Context: 시스템 프롬프트에 call stack 표시 + 재귀 호출 억제 지시

## 핵심 기능

### 도구 호출 방식

모든 프로바이더가 텍스트 파싱 방식을 사용합니다 (ReAct JSON 출력 → 파싱 → 도구 실행).

| 프로바이더 | JSON 출력 보장 | 파싱 |
|-----------|--------------|------|
| OpenAI-compat | `response_format: json_object` (basic JSON mode) | JSON 파싱 |
| Anthropic | tool calling | 3단계 폴백 파싱 |

Strict JSON Schema(OpenAI `json_schema`)는 **사용하지 않습니다**. 일부 온프렘 서버/모델 조합에서 strict 스키마가 깨지는 이슈가 있어, 확장성을 위해 basic JSON mode만 사용. ReAct 구조 강제는 시스템 프롬프트 + 3단계 파서가 담당합니다 (32B+ 권장 사양에서 실질 품질 손실 거의 없음, 7-14B는 포맷 drift 가능).

### 3단계 파싱 폴백

1. **Stage 1**: `json.loads()` (직접 파싱)
2. **Stage 2**: JSON 복구 (깨진 JSON 자동 수정)
3. **Stage 3**: Regex 추출 (최후 수단)

Thinking 모델(`<think>...</think>`)은 파싱 전 자동 분리됩니다.

### 세션 & 컨텍스트 관리 시스템

토큰 budget 기반 컨텍스트 관리. 평상시는 FIFO eviction, 90% 임계 초과 시 LLM 요약 압축(compaction)으로 전환되며, 모든 변경은 `history.jsonl`에 영속화됩니다.

#### 컨텍스트 윈도우 레이아웃

```
┌─────────────────────────────────────────────────────┐
│                  System Prompt                       │
│  ┌───────────────────────────────────────────────┐  │
│  │ Role / Task Guidelines / Format Rules         │  │  ← Primacy (고정, KV cache hit)
│  │ Available Tools (inline guides)               │  │
│  │ Available Skills / Available Agents           │  │  ← Middle (참조용)
│  │ Execution Context (call stack)                │  │  ← 스킬/delegate 실행 시만
│  │ Directives (DIRECTIVE.md)                     │  │
│  │ Environment (CWD, date, platform)             │  │  ← Recency (현재 맥락)
│  │ Context Recovery Guide                        │  │
│  │ [Hook Dynamic Sections]                       │  │  ← hook이 주입한 동적 섹션
│  └───────────────────────────────────────────────┘  │
├─────────────────────────────────────────────────────┤
│            Messages (Compaction + FIFO)              │
│  ┌───────────────────────────────────────────────┐  │
│  │ [evicted — history.jsonl에만 존재]            │  │  ← 90% 초과 시 oldest half가 요약으로 흡수
│  │ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─  │  │
│  │ [Compaction summary]    ← LLM 요약 (recursive)│  │
│  │ [Touched files] a.py, b.py, <delegate:foo>    │  │
│  │ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─  │  │
│  │ user: "hooks.py 분석해줘"                     │  │
│  │ assistant: thought → action: read_file(...)   │  │  ← 자연어 변환
│  │ user: [read_file] Observation: 1#PS:...       │  │  ← 도구 결과 전문
│  │ assistant: thought → complete(분석 완료)      │  │
│  └───────────────────────────────────────────────┘  │
├─────────────────────────────────────────────────────┤
│          Token Budget (자동 계산)                     │
│  budget = context_window - max_output - 4K reserve  │
│  예: 262K model → ~254K token budget                │
│  메시지 단위 eviction (중간 잘림 없음)               │
└─────────────────────────────────────────────────────┘
```

#### 토큰 Budget 관리

- `context_window - max_output_tokens - 4000 (system prompt 예약)` 자동 계산
- 메시지 **단위로** eviction — 메시지 중간이 잘리는 일 없음
- `--max-context-tokens`로 수동 override 가능 (0 = 자동)
- 스킬/delegate는 부모 budget 상속

#### Context Compaction (90% 임계)

캐시가 budget의 90%를 넘으면 단순 FIFO drop 대신 LLM 요약 압축이 실행됩니다.

1. **분할**: `[system anchor][dynamic]` — system prompt만 무조건 보존
2. **Evict 절반 (token-based)**: oldest 절반을 떼어냄
3. **LLM 요약**: evict 묶음을 단일 호출로 요약. 요약은 구조화 섹션(TASK / STATE / DONE / PENDING / DECISIONS / FAILURES / FACTS)으로 만들어져, 에이전트가 요약만으로 작업을 이어갈 수 있게 남은 작업·실패한 시도·정확한 식별자(경로/명령/에러)를 보존합니다. 이전 요약이 있으면 prior summary를 같은 호출에 prepend (recursive single-call — 합치는 별도 단계 없음)
4. **파일 경로 추출**: evict 안의 `read_file/write_file/edit_file/code_index` 호출과 `<delegate:agent>` placeholder를 누적 file_list에 dedup 머지
5. **재구성**: `[system][summary][file_list][retained dynamic]`
6. **영속화**: `compaction.json` (version, summary, file_list, dynamic_start_index 등) — `--resume` 시 압축 상태 그대로 복원
7. **Belt-and-braces fallback**: LLM 호출 실패 또는 재구성된 cache가 여전히 budget 초과면 플레인 FIFO drop으로 떨어뜨림 — 무한 트리거 루프 방지

`--no-compaction` 플래그 또는 `AGENT_CLI_COMPACTION=off` 환경 변수로 압축을 끄면 기존 플레인 FIFO만 동작합니다 (LLM 호출 비용·외부 의존이 곤란한 배포 환경 대비). 이 설정은 delegate·skill 서브에이전트에도 그대로 전파되어, 부모에서 압축을 끄면 중첩 실행도 모두 FIFO만 사용합니다.

압축이 일어나면 CLI는 한 줄 상태로, 웹은 **대화창 인라인 시스템 라인**(`⊙ 컨텍스트 압축됨 X→Y tok`)으로 진행을 표시합니다. 압축으로 흡수된 요약·파일 목록은 웹의 ⚡ Prompt Inspector 에서도 확인할 수 있습니다(위 참조).

#### 디렉토리 구조

```
{project}/.agent-cli/
  sessions/
    {session_id}/
      session.jsonl                     # 메타데이터 (1줄: id, workspace, updated_at, query)
      history.jsonl                     # 전체 대화 기록 (JSON Lines, append-only)
      delegate_{name}_{hash}_{ts}/      # delegate subdir
        history.jsonl                   #   delegate 내부 대화
        result.md                       #   delegate 최종 결과
      skill_{name}_{hash}_{ts}/         # skill subdir
        history.jsonl                   #   skill 내부 대화
        result.md                       #   skill 최종 결과
```

#### Context Recovery

FIFO에서 밀려난 과거 메시지가 필요할 때:
- System prompt에 `history.jsonl` 경로 안내 (Context Recovery Guide)
- LLM이 `read_file(history.jsonl)` 실행하여 과거 맥락 복구
- history.jsonl 내 artifact 경로로 delegate/skill 상세 결과 접근 가능

#### 세션 관리

```bash
agent-cli sessions                     # 세션 목록
agent-cli web --resume <session_id>   # 이전 세션 이어서 작업
```

### MCP (Model Context Protocol) 지원

외부 MCP 서버의 도구를 agent-cli에서 사용할 수 있습니다.

#### 설정

`.agent-cli/mcp.json` 또는 `~/.agent-cli/mcp.json`에 서버를 정의합니다:

```json
{
  "mcpServers": {
    "github": {
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-github"],
      "env": { "GITHUB_TOKEN": "${GITHUB_TOKEN}" }
    },
    "remote-api": {
      "url": "http://localhost:8080",
      "transport": "sse"
    }
  }
}
```

- **stdio**: `command` + `args` — 로컬 프로세스로 실행
- **SSE**: `url` + `transport: "sse"` — HTTP 원격 연결
- `${VAR}` — 환경 변수 참조
- 프로젝트 설정이 유저 설정보다 우선

#### 사용

세션 시작 시 자동 연결됩니다. MCP 도구는 `{server}.{tool}` 형식으로 LLM이 자동 사용:

```json
{"action": "github.list_issues", "action_input": {"repo": "owner/repo"}}
```

### 스트리밍 출력

모든 프로바이더(OpenAI, Anthropic)에서 LLM 응답을 실시간 스트리밍합니다:
- ASCII-art 말하는 얼굴 애니메이션 + 누적 토큰 카운터 (한 줄, 시간 기반 throttle)
- 스트리밍 완료 후 thought/action을 Markdown 렌더링
- 토큰 throughput 표시: `ttft: 200ms | in: 1024 tok (892 tok/s) | out: 156 tok (201 tok/s)`
- TTFT (Time-to-First-Token) 모든 프로바이더에서 클라이언트 측정

### 안전장치

- **반복 호출 감지**: 동일 도구를 같은 파라미터로 3회 연속 호출 시 자동 중단
- **echo-as-final**: `echo`로 답하는 소형 모델 패턴 자동 감지 → `complete` 도구 호출로 변환
- **잘린 JSON 복구**: LLM 응답이 잘릴 때 JSON repair 후 마지막 불완전한 edit 라인 제거, 적용된 edit 수 리포트
- **Execution Context**: 스킬/delegate 실행 시 call stack + `depth N/M` 을 시스템 프롬프트에 표시, 재귀 호출 억제. 한계 도달 시 그 사실도 명시.
- **agent_stack / skill_stack**: 런타임 재귀 방지 (A→B→A 차단). 블록 메시지에 3가지 recovery option 포함 (다른 접근 / complete / ask).
- **max_depth**: **skill + delegate 합산 중첩 깊이** 제한 (기본 2). 한계 도달 시 `AgentLoop.__init__` 가 `delegate` 와 `run_skill` 둘 다 tool list 에서 제거 (대칭). dispatch 단계 belt-and-suspenders check 도 동일 메시지 출력.

## 프로젝트 구조

```
agent_cli/
├── main.py              CLI 명령어 (run, chat, setup, sessions)
├── loop.py              AgentLoop 클래스 + ReAct 에이전트 루프
├── config.py            config.json 3레이어 로딩 + models.json 레지스트리
├── setup.py             SetupWizard (첫 실행 설정 마법사)
├── constants.py         공유 상수 (타임아웃, 임계값)
├── hooks/               Hook 시스템 (Python + Shell 라이프사이클 훅 11개 이벤트)
├── render/              플러그인 렌더링 시스템 (minimal — 커스텀 추가 가능)
├── input_history.py     readline 히스토리 영속화
├── providers/           LLM 프로바이더 (Anthropic, OpenAI)
├── wire_formats/        wire format 플러그인 (ReAct 외 추가 가능; 파서·복구·history 표현 self-contained)
├── tools/               도구 (read/write/edit/shell/delegate/context)
├── context/             컨텍스트 관리 (compaction + FIFO + history.jsonl + 세션 메타)
├── prompts/             조건부 시스템 프롬프트
├── skills/              프롬프트 스킬 시스템 (로더, 실행기, 모델)
├── agents/              에이전트 정의 (builtin: explorer)
└── mcp/                 MCP 통합 (config, client, adapter)
```

상세 아키텍처: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)

## 테스트

```bash
# 전체 유닛 테스트 (통합 테스트는 서버 미가용 시 자동 skip)
pytest tests/ -v
```

### omlx 통합 테스트 (실 서버 필요)

`omlx_integration` 마커가 붙은 E2E 테스트는 실제 OpenAI 호환 omlx 서버를 대상으로 run_loop·툴 사용·스킬·delegate·런타임 capability 감지를 검증합니다. 서버가 없으면 자동으로 skip되므로 `pytest tests/`는 항상 green입니다.

```bash
# 실 서버를 띄운 뒤 실행
pytest tests/ -m omlx_integration -v

# 연결/모델 override (기본: http://127.0.0.1:8000/v1, Qwen3.6-27B-MLX-8bit)
OMLX_BASE_URL=http://192.168.0.44:8000/v1 \
INTEGRATION_MODELS="Qwen3.6-27B-MLX-8bit" \
  pytest tests/ -m omlx_integration -v
```

| 환경변수 | 기본값 | 설명 |
|----------|--------|------|
| `OMLX_BASE_URL` | `http://127.0.0.1:8000/v1` | omlx OpenAI 호환 엔드포인트 |
| `OMLX_API_KEY` | (없음) | 필요 시 API 키 |
| `INTEGRATION_MODELS` | `Qwen3.6-27B-MLX-8bit` | 테스트 모델 (콤마 구분, 가용 모델만 실행) |

## 환경 변수

설정 우선순위 및 전체 환경변수 목록은 [설정 섹션](#설정)을 참조하세요.

## 라이선스

MIT License
