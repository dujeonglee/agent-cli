# Agent-CLI 레퍼런스

처음이면 [README](../README.md) 부터 읽는다.

ReAct 패턴 기반 에이전트 CLI. 멀티 프로바이더(OpenAI, Anthropic) 지원, on-premise LLM 최적화.

## 설치

Python 3.10+ 필요. 의존성 하한(`pyproject.toml`)은 쓰는 API 가 들어온 버전이고, CI `min-deps` 잡이 Python 3.10 에 하한 버전들을 실제로 깔아 유닛 수트를 돌린다(v10.124.1). MCP SDK 는 `mcp>=1.9,<3` 이다(v10.154.0, 종전 1.6 — streamable-http 전송이 제대로 도는 첫 판; 1.6~1.8 환경은 업데이트할 때 함께 올라간다).

```bash
# 설치 (기본 브랜치의 최신 — 릴리스 태그로 고정하려면 URL 끝에 @<태그>, 목록은 GitHub Releases)
pip install "git+ssh://git@github.com/dujeonglee/agent-cli.git"

# 웹 UI 포함
pip install "agent-cli[web] @ git+ssh://git@github.com/dujeonglee/agent-cli.git"

# 개발 모드
git clone git@github.com:dujeonglee/agent-cli.git
cd agent-cli
pip install -e ".[dev]"
```

설치 후 `agent-cli` 명령어가 PATH에 등록됩니다. 터미널에서 처음 실행하면 설정 마법사가 자동으로 시작됩니다(접속할 곳과 모델을 플래그로 다 주면 묻지 않습니다 — [첫 실행](#첫-실행-setup-wizard)).
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

# 직접 셸 명령 (LLM 없이) — 종료 코드는 그 명령의 것 (시간 초과 124)
agent-cli run "/sh ls -la"

# 명령 목록 — 부트 전에 답한다 (서버·세션 불필요, v10.127.1). `/compact` 는 web 전용
agent-cli run /help

# 스킬 실행 — 내장 `/plan` (그 밖의 이름은 `.agent-cli/skills/` 에 만든 스킬, 아래 "프로젝트 스킬 예시")
agent-cli run "/plan add a login rate limit"

# 도움말
agent-cli --help
agent-cli run --help
agent-cli web --help
agent-cli sessions --help

# 턴 기록의 숫자만 — 본문·경로 없이 (v10.130.0)
agent-cli stats
```

## 설정

> **⚠ v10.144.0 — 설정 모양이 바뀌었습니다. 옛 설정은 읽지 않습니다.**
> 모델 서버 설정이 "서버 여러 개를 이름으로 저장" 하는 모양이 됐습니다(아래 [config.json](#configjson)).
> v10.143.0 까지의 `~/.agent-cli/config.json`·`models.json`(최상위에 `endpoints` 가 없는 파일)은
> **읽지 않고 "서버 없음" 으로 봅니다** — 옮겨 주는 코드는 없습니다. `agent-cli setup`(또는 보드 admin)으로
> 서버를 다시 추가하세요. 첫 서버를 저장할 때 옛 파일은 지우지 않고 **`config.json.v1`·`models.json.v1`**
> 로 이름만 바꿔 남깁니다(이미 있으면 `.v1-2`, `.v1-3` …) — 다시 입력할 때 옛 값을 거기서 보면 됩니다.
>
> **agent-board 와 함께 올려야 합니다** — 서버별 설정을 읽고 쓰는 보드는 v1.48.0 이상입니다. 옛 보드의
> admin 은 새 모양 파일을 읽지 못하고, 새 보드는 낮은 agent-cli 에 안내를 띄웁니다.

### 첫 실행 (Setup Wizard)

저장된 서버가 없으면 터미널에서 실행할 때 자동으로 설정 마법사가 시작됩니다. 마법사는 **서버 하나**를
만들거나 고칩니다:

```
$ agent-cli run "hello"

No model server is set up. Starting setup wizard...

╭─ Agent-CLI Setup ─────────────────────────────────────╮
│                                                        │
╰─ ReAct pattern agent CLI for on-premise LLMs ─────────╯

1. Server name → 영문 소문자·숫자·`-` (방이 이 이름으로 서버를 기억하므로 나중에 바꿀 수 없음).
   저장된 이름을 대면 그 서버를 고친다
2. Select LLM Provider
   [1] OpenAI compatible (OpenAI, vLLM, LM Studio, omlx) — default
   [2] Anthropic
3. Connection → base URL, API key (고치는 서버면 빈 입력 = 저장된 키 유지)
4. Default Model → OpenAI 호환·Anthropic 모두 `/v1/models`(provider별 인증 헤더)로 자동 탐색 및 목록 선택, 실패 시 직접 입력
5. Review → 확인 후 저장. 첫 서버는 기본 서버가 되고, 그 뒤 서버는 기본으로 할지 묻는다
```

수동으로 다시 실행(서버 추가·수정):
```bash
agent-cli setup
agent-cli endpoints     # 저장된 서버 목록 (키는 보이지 않음)
```

**설정 파일은 필수가 아닙니다 (v10.125.0)** — 서버가 하나도 골라지지 않은 실행에서는 접속할 곳과 모델을
플래그나 환경변수로 다 주면(`--base-url` 또는 `AGENT_CLI_BASE_URL`, 그리고 `-m` 또는 `AGENT_CLI_MODEL`)
마법사 없이 바로 진행합니다 — 설정 파일 없는 컨테이너·CI 의 길입니다(이 실행은 서버 이름이 없어 모델 표가
패키지 기본 표뿐이고, 감지한 능력을 저장하지 않습니다). 키는 요구하지 않습니다(로컬 서버는 키가 없음).
터미널이 아닌 곳(CI·컨테이너·파이프·`AGENT_CLI_NO_INPUT=1`)에서 서버도 플래그도 모자라면 마법사를 띄우지
않고 한 줄과 종료 코드 `2` 로 멈춥니다(옛 모양 파일이 있으면 그 사실을 같은 줄에 덧붙입니다):

```
No model server is set up (~/.agent-cli/config.json). Run 'agent-cli setup' in a terminal, add a server in the board admin, or pass --base-url and -m.
```

```bash
agent-cli run "hello" --base-url http://127.0.0.1:8000/v1 -m Qwen3.6-35B-A3B-8bit </dev/null
```

### config.json

모델 서버를 **이름으로 여러 개** 저장합니다 (v10.144.0). `endpoint` 는 지금 선택된 서버 — 새 방과 CLI 의
기본값입니다:

```json
{
  "endpoint": "home",
  "endpoints": {
    "home": {
      "provider": "openai",
      "base_url": "http://127.0.0.1:8000/v1",
      "api_key": "",
      "default_model": "Qwen3.6-35B-A3B-8bit"
    },
    "work": {
      "provider": "openai",
      "base_url": "https://gateway.example.com/v1",
      "api_key": "…",
      "default_model": "my-model",
      "extra_headers": [{"name": "x-service-id", "value": "…"}]
    }
  }
}
```

- **서버 이름**: 영문 소문자·숫자·`-`. 방(세션)이 이름으로 서버를 기억하므로 만든 뒤 바꾸지 않습니다
  (바꾸려면 새로 만들고 옛 것을 지웁니다). 방에는 이름만 기록되고 주소·키는 복사되지 않습니다 — 서버의
  키를 바꾸면 그 서버를 쓰는 방이 다음에 뜰 때 새 키를 씁니다.
- **서버를 고르는 순서**: `--endpoint <이름>` > `AGENT_CLI_ENDPOINT` > 되살리는 세션에 기록된 이름 > config 의
  `endpoint`. 고른 서버의 값을 읽은 뒤 `--provider`·`--base-url`·`--api-key`·`--model` 이 있으면 그 값만
  덮습니다. **없는 이름은 그 자리에서 오류(종료 코드 `2`)** 입니다 — 다른 서버로 조용히 넘어가지 않습니다:

  ```
  Unknown endpoint 'lab' (saved servers: home, work). Add it with 'agent-cli setup' or in the board admin.
  ```

- **세션의 서버**: 새 세션은 고른 서버 이름을 `session.jsonl` 메타에 남기고, `--resume` 은 그 이름으로
  뜹니다(서버 기록이 없는 옛 세션은 그때 고른 서버를 쓰고 기록합니다). 지운 서버를 기억하는 세션은 되살릴 때
  위 오류로 멈춥니다. `--resume` 에 다른 `--endpoint` 를 주면 그것이 이기고 기록을 바꾸며 한 줄로 알립니다:
  `Session server changed: home → work`. 배너에 `Server:` 줄, 웹 방 머리 줄에 서버 칩이 보입니다.
- **환경변수 `AGENT_CLI_PROVIDER`·`AGENT_CLI_BASE_URL`·`AGENT_CLI_API_KEY`** 는 저장된 서버가 **0개**인
  실행의 이름 없는 서버입니다. 서버가 골라지면 쓰이지 않습니다 — 한 서버의 키가 env 로 다른 서버에 섞이지
  않게. 덮어쓰기는 플래그로 합니다. 서버는 저장돼 있는데 기본(`endpoint`)이 없으면(손으로 고친 파일) 다른 곳으로
  가지 않고 종료 코드 `2`: `No default server is set. Saved servers: home, work. Pick one with --endpoint, or set the default with 'agent-cli setup' or in the board admin.`
- **주소는 사용자가 적은 것만** 씁니다 (v10.144.0) — 서버의 `base_url`, `--base-url`, 이름 없는 실행의
  `AGENT_CLI_BASE_URL`. 어디에도 없으면 프로바이더 공개 주소(api.openai.com 등)로 가지 않고 종료 코드 `2` 입니다.
  **종전과 달라진 동작**: v10.143.0 까지는 주소가 비면 공개 주소로 갔습니다 — 이제 `-p anthropic` 만 주고 주소를
  안 주면 오류이니 `--base-url https://api.anthropic.com/v1` 을 적거나 그 서버를 저장하세요.

**`provider` 를 안 적으면 `openai`(OpenAI 호환) 서버입니다 (v10.123.1)** — 그 서버의
`base_url`·`default_model`·`api_key` 는 `-p` 없이 돌릴 때와 `-p openai` 일 때 쓰이고,
`-p anthropic` 처럼 다른 provider 를 고르면 쓰이지 않습니다(서버 값은 그 서버의 provider 에만).
모르는 provider 이름(`-p gemini`, 설정의 오타)은 사용 가능한 이름을 보여 주고 종료 코드 `2` 로 멈춥니다.

**추가 헤더 `extra_headers` (v10.134.0)** — 사내 게이트웨이처럼 키 말고도 헤더를 요구하는 서버에
임의 헤더를 붙입니다(서버마다 자기 목록). 키(`api_key`)가 가는 **모든** 요청 — 대화 호출(서브에이전트·압축 요약 포함),
능력 감지 프로브, `/models` 조회 — 에 같이 실리고, 키와 같은 범위입니다(그 서버의 provider 로 갈 때만).

```json
"work": {
  "provider": "openai",
  "base_url": "https://gateway.example.com/v1",
  "api_key": "test-…",
  "default_model": "my-model",
  "extra_headers": [
    {"name": "x-service-id", "value": "test-service", "enabled": true},
    {"name": "x-user-id", "value": "test-user", "enabled": false}
  ]
}
```

`enabled` 가 없으면 켜짐, `false` 면 저장만 하고 보내지 않습니다. 규칙(어기면 종료 코드 `2`, 메시지에는
헤더 **이름**만 나오고 값은 나오지 않습니다): 이름은 HTTP 헤더 이름 글자(RFC 7230 token), 값은 줄바꿈·NUL
없는 문자열, 하니스가 정하는 헤더(`Authorization`·`Proxy-Authorization`·`x-api-key`·`anthropic-version`·
`Content-Type`·`Content-Length`·`Host`·`Transfer-Encoding`·`Connection`)는 쓸 수 없고, 켜진 것끼리 같은
이름(대소문자 무시)은 한 번만. 헤더는 HTTP 층에만 있어 모델이 받는 프롬프트는 바뀌지 않습니다. `agent-cli setup`
은 이 목록을 묻지 않고 고치는 서버의 기존 값을 그대로 보존해 저장합니다. 보드의 admin 화면에서 서버마다 체크박스로 켜고 끌 수 있습니다.

**읽지 못한 `config.json` 은 멈춥니다 (v10.123.4)** — JSON 오류(끝 쉼표 등)·객체가 아닌 내용이면
`Could not read config <경로>: <오류, 줄·열>` 한 줄과 종료 코드 `2` — env·기본값으로 내려가지 않습니다. `agent-cli setup` 은 깨진 파일을
고치는 길이라 멈추지 않고 읽지 못했다는 사실만 보여 준 뒤 진행합니다(저장할 때 그 파일은 `.v1` 로 남깁니다).

**모델 이름은 부팅 때 검증됩니다 (v9.5.0)**. 서버의 `/v1/models` 에 그 이름이
없으면 **그 자리에서 멈추고** 사용 가능한 목록을 보여줍니다 — 터미널에서 실행
중이면 골라서 바로 저장할 수 있고, 다음부터 묻지 않습니다.

`/v1/models` 를 읽을 수 없는 경우(오프라인, 그 엔드포인트가 없는 서버)에는
**경고만 남기고 진행**합니다 — 확인할 수 없는 것을 틀렸다고 단정하면 오프라인
작업이 통째로 막히기 때문입니다. 에이전트 역할 파일(`.agent-cli/agents/*.md`)이
`model:` 로 다른 모델을 지정한 경우에도 **spawn 시점에** 같은 검사를 합니다.

**서버에 아예 닿지 못하면(연결 거부·호스트 없음·TLS — v10.126.0)** 그 사실을 그 자리에서 말합니다.
`models.json`(또는 패키지 기본 표)에 등록된 모델이면 등록된 설정으로 계속합니다 — 서버를
나중에 켜는 상주 방·보드 되살리기가 이 길입니다:

```
⚠ Cannot connect to http://127.0.0.1:8000/v1 (connection refused) — continuing with the registered settings; calls fail until the server is up.
```

등록 안 된 모델은 서버 없이는 능력(문맥 창·사고·방언)을 알 수 없으므로 프로브를 돌리지 않고
종료 코드 `2` 로 멈춥니다:

```
✗ Cannot connect to the server at http://127.0.0.1:8000/v1 (connection refused).
  Model 'my-model' is not registered in ~/.agent-cli/models.json (server 'home'), so its capabilities cannot be known without the server.
  Fix: start the server, or check --base-url / the base_url of server 'home' in ~/.agent-cli/config.json.
```

괄호 안은 사실 하나입니다: `connection refused`·`host not found`·`TLS error: <사유>`(예: `certificate verify failed: …`)·`cannot connect`(그 밖의 연결 수립 실패).
타임아웃·프록시 오류·연결된 뒤 끊김(`connection closed by the server`)·HTTP 오류(404·401·500)·빈 목록은
서버에 닿지 못했다고 단정할 수 없어 경고 후 진행합니다.
등록 안 된 모델의 능력 탐지가 실패했을 때의 질문(문맥 창·사고·이미지·방언)은 터미널에서만 묻고,
빈 입력의 문맥 창 기본값은 묻지 않았을 때 실제로 쓰이는 값(262144)입니다. LLM 호출이 끝내
실패한 줄의 연결 오류도 한 줄 분류로 찍습니다(`LLM call failed (…): ConnectionError: connection refused`, `ReadTimeout: no response (timeout)`).

`default_model` 을 비워두면 추측하지 않고 "모델을 지정하라"고 말합니다 —
`gpt-4o` 같은 이름을 몰래 끼워 넣던 동작은 v9.5.0 에서 제거됐습니다(로컬
서버에는 있을 리 없는 이름이라, 원인을 가리기만 했습니다). 설계 근거는
`docs/model-resolution/DESIGN.md`.

### 설정의 집은 하나 (v9.0.0)

각 설정은 **정확히 한 곳**에 삽니다 — 프로젝트와 유저 파일을 병합하지 않으므로 어느 파일이 이겼는지 따질 일이 없습니다.

| 스코프 | 위치 | 담는 것 |
|--------|------|---------|
| 유저 | `~/.agent-cli/` | `config.json` · `models.json` — 이 컴퓨터의 환경 (모델 서버들·API 키·서버별 모델 capability 캐시) |
| 프로젝트 | `.agent-cli/` (cwd 기준) | `mcp.json` · `skills/` · `agents/` · `hooks/` · `hooks.json` · `DIRECTIVE.md` — 이 저장소가 필요로 하는 것 |
| 세션 | `sessions_dir()` | `sessions/` — `AGENT_CLI_SESSIONS_DIR` 로 작업 트리 밖으로 뺄 수 있는 것 |

**프로젝트는 git 루트가 아니라 cwd 기준**입니다. 하위 디렉토리에서 실행하면 프로젝트 설정이 비어 있습니다. 프로젝트 밖(`~/Downloads` 등)에서 실행하면 스킬·MCP·DIRECTIVE 없이 뜹니다 — agent-cli 는 프로젝트 안에서 쓰는 도구입니다.

> **v8.x 에서 올리는 경우**: 다음 파일은 이제 **읽지 않습니다** — `~/.agent-cli/mcp.json` · `skills/` · `agents/` · `hooks/` · `hooks.json` · `DIRECTIVE.md`(→ 프로젝트로), `.agent-cli/config.json` · `models.json`(→ 유저로), `~/.agent-cli/chat_history`(→ `sessions_dir()`). 경고는 없으니 손으로 옮기세요. 설계 근거와 알고 받아들인 손실은 `docs/config-scopes/DESIGN.md`.

### 설정 우선순위

먼저 서버를 하나 고르고(`--endpoint` > `AGENT_CLI_ENDPOINT` > 세션 기록 > config 의 `endpoint`), 그 서버의 값 위에
플래그가 필드 단위로 덮습니다:

| 우선순위 | 위치 | 용도 |
|---------|------|------|
| 1 (최고) | CLI 파라미터 (`-p`, `-m`, `--base-url`, `--api-key`) — 모델은 `AGENT_CLI_MODEL` 도 | 임시 오버라이드 |
| 2 | 고른 서버의 값 (`~/.agent-cli/config.json` 의 `endpoints.<이름>`) | 기본 설정 |
| — | 환경변수 `AGENT_CLI_PROVIDER`·`AGENT_CLI_BASE_URL`·`AGENT_CLI_API_KEY` | 저장된 서버가 없을 때만 — 이름 없는 서버 |

`--base-url` 로 주소만 덮으면 그 서버의 키·헤더가 새 주소로 갑니다(종전과 같은 동작). 프로젝트별로 다른 서버나 모델을 고정하려면 `AGENT_CLI_ENDPOINT`·`AGENT_CLI_MODEL`(direnv 등)을 쓰세요 — v9.0.0 부터 프로젝트 `config.json` 은 읽지 않습니다.

예: `~/.agent-cli/config.json`에 `gpt-4o`가 기본이지만 `-m gpt-4o-mini`로 임시 실행:
```bash
agent-cli run "task" -m gpt-4o-mini
```

### DIRECTIVE.md — 프로젝트 지시사항

에이전트가 항상 따라야 하는 규칙을 `DIRECTIVE.md` 파일에 작성하면 매 세션의 시스템 프롬프트에 자동 주입됩니다.

경로는 `.agent-cli/DIRECTIVE.md` **하나**입니다 (v9.0.0 — 유저 전역 `~/.agent-cli/DIRECTIVE.md` 는 더 이상 읽지 않습니다). 응답 언어 같은 개인 취향도 프로젝트 파일에 적습니다.

- 최대 4,000자 (초과 시 잘림)

예시 (`.agent-cli/DIRECTIVE.md`):
```markdown
# 코드 규칙
- 코드 수정 시 관련 유닛 테스트를 반드시 추가한다
- 웹 UI 행동 계약(SSE·confirm 흐름·칩 렌더링·연결 고갈)은 실브라우저 테스트로: `AGENT_CLI_BROWSER_TESTS=1 pytest tests/browser/` (playwright+chromium, 기본 스위트는 미수집)
- ruff check와 ruff format을 통과해야 한다
- Python 3.10+ 호환을 유지한다
```

**스코프 마커 (5.1.0)** — 지침을 main LLM 과 서브에이전트에 나눠 보낼 수 있습니다:

```markdown
공통 규칙 (마커 이전 = main + 모든 서브에이전트)

## @main
- 사용자 보고는 한국어로, 결론 먼저

## @agents
- 결과는 영어 bullet 로 간결하게 반환
```

- `## @main` 블록은 **main LLM 에만**, `## @agents` 블록은 **모든 서브루프**(agent run/spawn·skill)에만 주입됩니다. 마커 라인 자체는 프롬프트에서 제거됩니다.
- 블록은 다음 `## @` 마커(또는 파일 끝)까지 — 블록 안에 일반 `## 헤딩` 여러 개를 담을 수 있습니다. 마커 없는 파일은 전체가 공통입니다.
- 웹 헤더의 📝 **Directive Editor** 가 이 스코프 구조를 탭(Common/Main/Sub-agents)으로 그대로 편집하며, ✨ 버튼으로 대략적 의도를 LLM 초안으로 만들 수 있습니다.

### 환경변수

| 변수 | config.json 키 | 설명 |
|------|---------------|------|
| `AGENT_CLI_ENDPOINT` | `endpoint` | 쓸 서버 이름 (v10.144.0) — `--endpoint` 다음, 세션 기록·config 의 `endpoint` 보다 앞선다. 없는 이름은 종료 코드 `2` |
| `AGENT_CLI_PROVIDER` | `provider` | 저장된 서버가 없을 때의 프로바이더 — 비면 `openai`(OpenAI 호환). 서버가 골라지면 쓰이지 않음 (v10.144.0) |
| `AGENT_CLI_BASE_URL` | `base_url` | 저장된 서버가 없을 때의 API 엔드포인트 (설정 파일 없는 컨테이너·CI). 서버가 골라지면 쓰이지 않음 |
| `AGENT_CLI_API_KEY` | `api_key` | 저장된 서버가 없을 때의 API 키. 서버가 골라지면 쓰이지 않음 |
| `AGENT_CLI_MODEL` | — | 모델 ID — 노브 `model` 의 부팅값 (v10.43.0; `--model`/`-m`, 웹 🤖 칩 v10.45.0). 세션 도중 바꾸면 다음 LLM 호출부터 실린다. 서버의 `default_model` 위에 선다 |
| `ANTHROPIC_API_KEY` | — | Anthropic API 키 (기존 호환) |
| `OPENAI_API_KEY` | — | OpenAI API 키 (기존 호환) |
| `AGENT_CLI_NO_INPUT` | — | 비어 있지 않으면 터미널에서도 묻지 않는다 — 비대화식으로 다룸(설정 마법사·모델 고르기·탐지 실패 질문·방언 질문 모두 건너뛰고 오류로). 테스트·자동화용 |
| `AGENT_CLI_WORKSPACE_CONFINE` | — | 워크스페이스 경로 봉쇄 + 민감 경로 사전 (기본 on). `0` 으로 끄면 둘 다 없음. [아래 참고](#워크스페이스-경로-봉쇄) |
| `AGENT_CLI_WORKSPACE_ROOT` | — | 봉쇄 기준 루트 경로 override (기본: 프로세스 실행 디렉토리) |
| `AGENT_CLI_BROWSER_TOOL` | — | `0` 이면 `browser` 도구를 끈다 (기본: playwright 가 깔려 있으면 켜짐) |
| `AGENT_CLI_BROWSER_HOSTS` | — | `browser` 가 열 수 있는 호스트 (쉼표 목록, 하위 도메인 포함; 비면 전부) |
| `AGENT_CLI_BROWSER_EXECUTABLE` | — | `browser` 가 띄울 브라우저 실행 파일 (기본: 설치된 Chrome → Playwright Chromium) |
| `AGENT_CLI_BROWSER_HEADLESS` | — | `auto`(기본): 헤드리스로 시작, 사이트가 사람 확인을 요구하면 보이는 창으로 전환 · `1`: 항상 헤드리스 · `0`: 처음부터 보이는 창 |
| `AGENT_CLI_AUDITOR` | — | `1` 이면 세션 감사 켬 (v10.42.0, 기본 꺼짐; web 은 🔎 칩으로 세션 도중 켜고 끔): main 의 사용자 런이 `complete` 할 때마다 하니스가 그 런의 기록만 읽는 감사 런을 돌리고 요청별 충족 판정을 main 에게 전한다 — main 이 한 번 더 깨어나 미충족을 손본다 (아래 "세션 감사자") |
| `AGENT_CLI_DANGEROUS_SHELL_CONFIRM` | — | 위험 명령(`rm`/`rmdir`/`mv`) 확인 프롬프트 (기본 on). `0` 으로 끄면 비활성. 대상이 전부 임시 자리(`/tmp`·`$TMPDIR`·`.agent-cli/tmp`) 안이면 묻지 않음 (v10.104.0) |
| `AGENT_CLI_THINKING` | `off` | 사고 스위치: `off`/`on` (미설정=**끔**, v9.24.8). 웹 🧠 노브의 초기값이자 headless(run·harbor)의 유일한 제어 — `supports_thinking:true` 모델에서만 요청에 실린다. v10.72.0: `auto` 와 정도(`AGENT_CLI_REASONING_EFFORT`)는 제거 — 켰을 때 얼마나 사고하는지는 서버·모델 기본값이 정한다(모델마다 정도의 키·어휘가 달라 하니스가 번역하면 조용히 틀린다) |
| `AGENT_CLI_TEMPERATURE` | — | 샘플링 temperature (0~2) — 매 LLM 요청에 실린다 (v10.69.0). 미설정 = **보내지 않음**: `models.json` 엔트리의 `temperature` → 서버 기본. `--temperature` 또는 웹 🎲 칩으로 세션 중 변경 |
| `AGENT_CLI_TOP_P` | — | 샘플링 top_p (0~1) — 매 LLM 요청에 실린다 (v10.69.0). 미설정 = 보내지 않음: `models.json` 엔트리의 `top_p` → 서버 기본. `--top-p` 또는 웹 🎲 칩 |
| `AGENT_CLI_IMAGE_MAX_EDGE` | — | 보내기 전 긴 변 상한(픽셀) — 넘으면 축소 (기본 `2048`, `0`=축소 없음; v10.59.0) |
| `AGENT_CLI_GRAMMAR` | — | 디코딩 문법 제약: `off` 로 끔 (미설정/`on` = 기본 켬). web UI 📐 노브의 headless 대응. 켜기는 `models.json` 에 `supports_grammar: true` 로 **기록된** 모델에서만 효력 — 미지원(false)·미확인(키 없음) 모델엔 어떤 값으로도 켜지지 않는다 (v9.24.0) |
| `AGENT_CLI_STREAM_IDLE_TIMEOUT_S` | — | 스트림 무진전(no-token) 한도 초 — 마지막 토큰 이후 N초 무진전이면 재접속·재전송 (keep-alive 는 진전 아님). 0=끔, 기본 600. `--stall` 또는 웹 ⏳ 노브로 세션 중 변경 (v8.55.0) |
| `AGENT_CLI_STREAM_MAX_ATTEMPTS` | — | 무진전 시 **총** 전송 횟수(첫 전송 포함), 1~10, 기본 4. 최대 대기 = 한도 × 이 값. `--stall-attempts` 또는 웹 ⏳ 노브로 세션 중 변경 (v8.60.0) |
| `AGENT_CLI_COMPACTION_RATIO` | — | 컨텍스트 압축 목표 비율 (0.5~1.0, 기본 1.0 — 창에 잘릴 때만 압축, v10.11.0). 낮을수록 일찍·자주 압축. `--compaction-ratio` 또는 웹 🗜️ 노브로 세션 중 변경 (v8.61.0) |
| `AGENT_CLI_MAX_AGENTS` | — | 동시 생존 서브에이전트 상한 (0 = 무제한, 기본 10). `--max-agents` 또는 웹 👥 노브로 세션 중 변경 (v8.61.0) |
| `AGENT_CLI_AUTO_APPROVE` | — | `1` 이면 확인 프롬프트(위험 셸 명령·경로 가드)를 묻지 않고 허용 (v10.43.0, 기본 꺼짐). `--auto-approve` 또는 웹 ⚡ 노브. `run` 은 물을 사람이 없는 자리라 이 값이 곧 승인면 |
| `AGENT_CLI_SNAPSHOT` | — | `0`·`false`·`no`·`off` 이면 워크스페이스 스냅샷(📸, `snapshot` 도구)을 끈다 (기본 켬, v10.36.0) |
| `AGENT_CLI_SNAPSHOT_MIN_INTERVAL_S` | — | 스냅샷 체크포인트 최소 간격 초 (기본 `10`) |
| `AGENT_CLI_SNAPSHOT_FILE_MAX_BYTES` | — | 이보다 큰 파일은 스냅샷에서 제외 (기본 `-1` = 무제한) |
| `AGENT_CLI_SNAPSHOT_FILE_COUNT_MAX` | — | 파일 수가 이를 넘으면 시작 스냅샷만 찍고 턴 단위 체크포인트를 끈다 (기본 `-1` = 무제한) |
| `AGENT_CLI_MONITOR_REPLAY_MAX_BYTES` | — | `monitor` 의 `match` 등록 때 이미 있고 모델의 마지막 관찰 뒤 바뀐 파일을, 이 크기까지는 내용 전부를 새 줄로 본다 (기본 `65536`, `0` = 자동 발화 없음, `-1` = 무제한) |
| `AGENT_CLI_DUMP_STREAM` | — | 파일 경로를 주면 서버가 보낸 스트림 줄을 해석 전에 그대로 그 파일에 덧붙인다(호출마다 구분선, v10.1.6) — 진단용 |
| `AGENT_CLI_RECORD_EMISSIONS` | — | `1` 이면 모델이 낸 텍스트를 파싱 전에 `<세션>/emissions.jsonl` 에 한 줄씩 남긴다 (기본 꺼짐) — 파서 코퍼스·진단용 |
| `AGENT_CLI_SESSIONS_DIR` | — | 세션 루트 override (기본: 작업 디렉토리의 `.agent-cli/sessions`). 작업 트리에 세션을 남기지 않을 곳 — 헤드리스/CI 자동화, 읽기 전용·공유 체크아웃, 벤치 컨테이너. `run`·`web`·`sessions`·`--resume`·`history` 가 모두 같은 루트를 봄 (v8.50.0) |

> **LLM 요청 재시도**는 고정 상수로 동작한다(더 이상 env 로 조정 불가). 네트워크 에러(Timeout / ConnectionError)는 최대 10회, 일시적 게이트웨이 5xx(**502/503/504**)는 **독립 카운터**로 최대 3회, 재시도 간격 1초. **속도 제한(429, Anthropic 의 529)은 기다렸다 다시 보낸다 (v10.25.0)**: 또 하나의 독립 카운터로 최대 8회, 응답에 `Retry-After` 가 있으면 그만큼(상한 120초), 없으면 2·4·8·…·64초로 늘려 가며 기다린다(헤더 없이 다 쓰면 약 3분). 회사 게이트웨이(LiteLLM 등) 뒤에서 여러 사람이 한 벤더 키를 나눠 쓸 때 맞는 오류다. 기다리는 동안 상태 줄에 `LLM rate limited (HTTP 429) — retrying in 17s (2/8)` 가 뜬다. 그 밖의 4xx·bare 500 은 무재시도. 스트리밍은 헤더 대기를 30초로 바운드하고, body 가 10분 연속 침묵하면 연결을 끊고 재전송한다 — **총 4회**(첫 전송 포함)까지, 즉 최장 40분 뒤 실패. 이 두 축만은 고정이 아니라 세션 노브다: 한도는 `--stall`, 횟수는 `--stall-attempts`(웹은 ⏳ 칩 하나에 두 입력, 팝업이 곱한 최대 대기를 같이 보여준다). 대기 중에는 남은 시간과 `시도 n/4` 가 웹·CLI 양쪽에 표시되고, 반복되는 대기는 한 줄을 제자리 갱신하며 재전송·실패만 기록으로 남는다. 스트림이 **중간에 끊긴 경우**(서버가 종결 청크 없이 연결을 닫음 — 재시작·프록시 끊김·OOM)도 재전송한다: 고정 10회, 1초 간격, 무진전 예산과 **별개**(즉시 실패라 공유하면 순식간에 소진된다). 재전송 전에는 항상 부분 출력을 걷는다 — 재전송은 생성을 처음부터 다시 하므로 남겨두면 새 출력이 옛 부분 뒤에 이어붙어 보인다. **정지가 서면(■·Ctrl+C·`agent` `kill`·세션 종료) 더 보내지 않는다 (v10.177.9)**: 보내기 전마다 정지를 보고, 재시도 사이의 쉼(1초·`Retry-After`·백오프)도 0.2초마다 정지를 봐서 바로 끝난다 — 스트림 도중 정지와 같은 길로 런이 멈춘다. 이미 보낸 요청이 응답 머리를 기다리는 동안(최장 30초)은 끊지 않는다. 압축 요약 호출(두 모양 모두)은 정지를 받지 않아 그 재시도는 종전대로다. 자세한 동작은 `agent_cli/providers/http.py` 참고.
>
> **세션 노브의 수명**: 웹 헤더의 🤖 🧠 📐 👥 🗜️ ⏳ 🔎 ⚡ 는 전부 **세션 한정**(서버 메모리)이다. 칩은 손으로 그린 것이 아니라 노브 표(`agent_cli/knobs.py`, `agent-cli knobs` 로 출력)에서 나온다 (v10.45.0): 표의 선언 한 줄이 `run`/`web` 플래그·env·칩 하나를 함께 만들고, 배지 글자·설명·잠김 사유는 서버가 계산해 보낸다. 브라우저 창을 닫거나 다른 기기로 다시 붙어도 값은 유지되지만(sticky 스냅샷), **프로세스를 재시작하면 기본값으로 돌아간다** — `--resume` 으로도 복원되지 않는다(세션 메타에 담지 않는다; 특히 ⚡ 자동 승인처럼 안전 게이트를 끄는 설정이 재시작을 넘어 살아남으면 위험하다). 재시작 후에도 값을 유지하려면 위 env 또는 CLI 인자로 부팅 시점에 지정한다. 🎨 테마·닉네임만 브라우저 localStorage 라 예외적으로 계속 남는다.
>
> **컨텍스트 컴팩션**은 항상 켜져 있다(호출별 압축 목표치를 넘으면 LLM 요약; 실패/여전히 초과 시 플레인 FIFO drop 으로 폴백). 서브에이전트에도 전파된다. 아직 답하지 않은 사용자 요청(꼬리 `## Open Requests` 에 있는 것)의 본문은 요약하지 않고 **원문 그대로** 남긴다(v10.41.0) — 답하고 나면 다음 압축 때 잘린다. 채팅의 "컨텍스트 밖 대화" 묶음에서도 그 카드는 접히지 않는다.

### 워크스페이스 경로 봉쇄

에이전트의 파일 **쓰기·수정·shell** 이 워크스페이스(실행 디렉토리) *밖* 경로를 건드리면
**사용자에게 확인을 물어봅니다** (기본 on). 실수로 홈 디렉토리나 시스템 파일을 덮어쓰는
사고를 막기 위한 가드입니다.

- **대상**: `write_file`, `edit_file`, `shell`. **`read_file` 은 봉쇄하지 않습니다** —
  드라이버/커널 작업은 커널 소스·툴체인·헤더를 워크스페이스 밖에서 대량으로 읽으므로,
  읽기까지 물으면 프롬프트 폭풍이 되어 사용자가 "always" 를 남발하게 됩니다.
  (읽기는 대신 아래 **민감 경로 사전**이 봅니다.)
- **shell**: 명령에서 절대경로·`../` 탈출 토큰을 **best-effort** 로 추출해 검사합니다.
  추출은 **2단계**입니다 — ① 배칭된 명령(`&&`/`|`/`;`)을 갈라 각 세그먼트가 파일을
  바꿀 수 있는지 보고 ② 바꿀 수 있는 것에서만 경로를 뽑습니다. 그래서
  `sed -n '/^start/,/^end/p'` 의 **주소 정규식**이나 `grep '/api/'` 의 패턴은 더 이상
  경로로 오인되지 않습니다. 모르는 명령은 "바꿀 수 있음"으로 보므로 목록이
  불완전해도 구멍이 생기지 않고, `sed -i`·`find -delete`·`sort -o` 와 리다이렉션은
  읽기 판정을 이깁니다.
  **세 가지는 경로로 세지 않습니다** (v10.135.1 — 과거 거절 32건 재판정에서 남은 오탐):
  ① **명령 단어** — `/usr/bin/python t.py` 의 실행 파일(앞의 `FOO=1` 대입, `env`·`time`·`nice`·
  `command`·`exec` 뒤의 것 포함). 같은 명령의 다른 인자는 그대로 봅니다(`/usr/bin/tee /outside/f`
  는 묻습니다). ② **입력 리다이렉트 `<` 의 대상** — 읽기입니다(`sort < /usr/share/dict/words`).
  `<<`·`<<<`·`<>` 는 해당하지 않고, 같은 토큰에 붙은 `>`(`</a>/out`)는 쓰기로 봅니다.
  ③ **따옴표 안의 `>`** — `python3 -c "print('a -> b')"` 는 리다이렉트가 아니고, 리다이렉션은
  **그 세그먼트에만** 걸립니다(앞 명령의 `> out.txt` 가 뒤 `grep -v "/x/"` 의 패턴을 경로로 만들지
  않습니다). 세그먼트는 따옴표 밖의 `&&`·`||`·`|`·`;`·`&`·**줄바꿈**으로 가릅니다 — 줄바꿈으로
  이어 쓴 `ls /x` 다음 줄의 `rm -rf /outside/p` 도 따로 읽혀 묻습니다.
  민감 경로 사전은 세 자리 모두에 그대로 겁니다(`cat < ~/.ssh/id_rsa`, `/usr/bin/cat ~/.ssh/id_rsa`).
  **`>/dev/null`·`2>/dev/null`·`2>&1` 은 쓰기가 아닙니다** (v9.26.2) — 리다이렉션의 대상이
  장치 의사파일(`/dev/null`·`/dev/zero`·`/dev/std{in,out,err}`·`/dev/tty`·`/dev/(u)random`·`/dev/fd/N`)이거나
  fd 복제(`&1`)면 그 세그먼트는 읽기 전용으로 남고, 그 장치 경로 자체도 묻지 않습니다.
  반대로 `2>/tmp/err.log` 처럼 fd 번호가
  붙은 **파일** 리다이렉션은 대상이 뽑힙니다.
  블록 장치(`/dev/disk0`)는 여전히 묻습니다.
  **시스템 임시 디렉터리(`/tmp`·`/var/tmp`·`$TMPDIR`)도 묻지 않습니다** (v10.103.0) — 봉쇄는
  사고 방지와 워크스페이스 밖으로 새는 것을 막는 가드인데, 임시 디렉터리에는 사용자의 파일이
  없고 유출 경로도 아닙니다. 모델이 고르지 않아도 컴파일러·pip·pytest·`mktemp` 가 명령줄에
  `/tmp` 를 올리고, 클라이언트가 안 붙은 방이나 배치 런에서는 확인 대신 **거절**이 되어 호출이
  통째로 실패했습니다. resolve 결과로 비교하므로 `/tmp` 안의 바깥행 심링크는 여전히 묻고, 민감
  경로 사전(`/tmp/id_rsa`)도 그대로 겁니다. 모델의 임시 파일 자리는 여전히 `.agent-cli/tmp/` 로
  안내합니다(프로젝트와 함께 지워지고 방에서 보이기 때문 — 확인 때문이 아니라).
  `$(...)`·`python -c "..."`·셸 변수(`$FILE`) 안에 숨은 경로는 못 잡습니다 —
  이건 사고 방지용 speed bump 이지 샌드박스가 아닙니다(진짜 격리는 OS 샌드박스 필요).
  확인 창에는 명령이 표시되고 **워크스페이스 밖 경로가 강조**됩니다 (위험 키워드
  `rm`/`rmdir`/`mv` 를 강조하는 것과 동일 — CLI 볼드-레드, 웹 `.danger` 스팬).
- **응답**: `y`(이번만) / `n`(거부) / `a`(이 위치를 이번 세션 동안 항상 허용).
  `a` 는 해당 디렉토리 서브트리를 세션 allowlist 에 넣어 이후 같은 곳은 재프롬프트하지 않습니다.
- **비대화형**(TTY 없음·연결된 클라이언트 없음)에서 밖 경로를 건드리면 **거부**됩니다
  (멈추지 않음). 배치/CI 에서는 `AGENT_CLI_WORKSPACE_CONFINE=0` 으로 끄세요.
- **루트**: 기본은 프로세스 실행 디렉토리. `AGENT_CLI_WORKSPACE_ROOT` 로 override.
  agent-board 는 각 인스턴스를 그 게시물 워크스페이스에서 spawn 하므로 그대로 맞습니다.

### 🔔 monitor — 조건 감시

오래 도는 작업을 백그라운드로 띄워 놓고 **조건이 맞으면 보고받습니다.** (명령의 **끝**만 기다리는 거라면 v10.47.0 의 백그라운드 작업(`shell` `background: true`, 아래 🧵)이 맞습니다 — 종료 코드까지 통지됩니다.)

> **`match` 의 조용함 한도 (v10.47.0)**: 파일이 `silent`(기본 10분, `monitor add` 의 인자, `0` = 끔) 동안 안 바뀌면(등록 뒤 생기지 않은 채로도) 보고를 남기고 물러납니다 — "writer 가 죽은 로그를 2시간 기다리다 run 이 멈춤" 이 깨우기로 바뀝니다. `file + idle`(그 자체가 조건)과는 별개입니다.

```
shell("./build.sh", background=true)                       # 🧵 작업 — 끝나면 통지
monitor(add, file=".agent-cli/tmp/jobs/1.log", pattern="ERROR|FAILED")
monitor(add, file="out.log", idle="5m")
monitor(add, command="test -f out/done", every="60s")
```

- **조건 3종 — 키 쌍으로 고릅니다 (v10.67.0)**: `file + pattern`(파일의 새 줄 +
  정규식) · `file + idle`(파일이 그 시간 동안 멈추면 — **침묵은 성공이
  아닙니다**, 죽은 스크립트를 잡습니다) · `command + every`(명령을 주기적으로
  돌려 exit 0 이면 발화, stdout 이 보고 본문). 조건은 평평한 키 쌍이고 `when={"type": …}` 중첩
  객체는 받지 않습니다. 키 쌍이 섞이면(`file` + `command`) 세 모양을 보여 주며 거절합니다.
  실측(세 시나리오 × 10런): 중첩 30/30 · 평평 1판 23/30(6런 영구 대기) · 평평
  2판 27/30(실패 3건은 거절 뒤 한 턴에 교정), `docs/monitor/DESIGN.md` §4.3.
- `match` 는 **등록 시점 이후의 줄만** 봅니다(이미 쌓인 로그의 과거는
  건너뜀). 등록 때 아직 없던 파일은 생기는 순간의 내용부터 전부 새 줄로
  봅니다 (v10.31.1 — 결과를 한 번에 쓰고 끝나는 `done.txt` 류도 잡힙니다)
- **등록 때 파일이 이미 있으면 근거를 줍니다 (v10.34.0)**: 등록 응답에
  파일의 수정 시각과 그것이 모델의 **마지막 도구 결과 이후인지 이전인지**,
  패턴에 맞는 마지막 줄을 적습니다 — "지난 빌드의 ERROR" 와 "방금 끝난
  작업의 READY" 는 내용만으로 구분이 안 되므로 판정은 모델에게 맡깁니다.
  마지막 결과 이후에 수정됐고 파일이 작으면(기본 64 KB 이하) 애매할 게
  없으니 내용 전부를 새 줄로 보고 바로 알립니다. 상한은
  `AGENT_CLI_MONITOR_REPLAY_MAX_BYTES` (0 = 항상 근거만, -1 = 크기 무관).
- `run` 이 모니터 때문에 끝나지 않고 기다릴 땐 한 번 알립니다 (v10.34.0,
  `🔔 모니터 N개 대기 중 — 가장 늦은 만료 …`; 예약 대기와 같은 방식)
- **알림은 자동**입니다 — 폴링하지 마세요. 조건이 맞으면 관찰로 배달되고,
  쉬고 있었다면 그 순간 깨어납니다
- **건 쪽으로 갑니다** (v9.14.0): main 이 걸면 main 의 대화로, 상주
  에이전트가 걸면 **그 에이전트의 런**으로. 그 안에서 돌던 스킬·delegate 가
  건 것도 부모 에이전트에게 갑니다
- `deadline` 기본 2시간(60초~24시간), `once` 기본 참(한 번 보고하고 은퇴).
  **해제될 때도 알립니다** — 만료든 은퇴든 조용히 사라지지 않습니다
- **모니터는 보고만 합니다** (v10.79.0) — 반응(죽이기·다음 단계)은 모델이 보고를 읽고 `shell` 로 합니다. `run="<명령>"` 인자는 없습니다 — 보내면 다른 모르는 인자처럼 버려집니다. 같은 판에서 설명·인자·보고·오류 문구를 전부 인자 짝(`file` + `pattern`)으로 고치고 초를 "2h"·"1h49m" 로 적습니다 — `docs/monitor/DESIGN.md` §13
- `command` 는 **등록할 때** 확인받습니다. 발화할 땐 사람이 없으니까요
- `command` 는 **`shell` 도구가 있는 에이전트만** 걸 수 있습니다 (v10.42.0 — 읽기 전용 프로파일에겐 `file+pattern`/`file+idle` 만; monitor 가 셸 권한의 뒷문이 되지 않게)
- 내장 에이전트 프로파일 여섯 개도 monitor 를 씁니다 (v9.14.0). 권한이
  넓어진 것은 아닙니다 — 여섯 다 이미 `shell` 을 가지고 있고, monitor 는
  같은 셸 권한에 **보고가 반드시 따라오는** 형태입니다
- 세션이 끝나면 모니터도 끝납니다. 다음 세션엔 "복원되지 않았습니다"만
  알립니다 — 세션보다 오래 가는 주기 작업이 필요하면 `schedule` 이 맞는
  도구입니다 (아래)

**백그라운드 실행은 `shell` 의 `background: true`** 로 띄웁니다(v10.47.0, 아래 🧵). monitor 설명에는
셸 요리법(`nohup … > /tmp/x.log 2>&1 & echo $!`, `python -u`/`PYTHONUNBUFFERED=1`, `( CMD; echo
"EXIT:$?" )`)을 싣지 않습니다 — 하니스가 로그·종료 코드·통지를 맡고, `/tmp` 는 꼬리
규칙(스크래치는 `.agent-cli/tmp/`)과 충돌했습니다. 남긴 것은 성질 하나: 출력을 버퍼링하는 생산자는 끝날 때까지
파일에 아무것도 쓰지 않아 건강한 작업이 침묵으로 보이므로 언버퍼로 돌리라는 문장(실행 파일 이름 없이).
monitor 의 `file+pattern`/`file+idle` 은 그 작업의 로그 파일(`.agent-cli/tmp/jobs/…`)을 가리키면 됩니다.

### 반복 작업의 상태는 파일에 (v10.102.0 — `python` 도구 삭제)

비용 큰 단계(로딩·디코딩·데이터 준비) 뒤에 다음 단계를 여러 번 시도하는 일은, 비용 큰 단계의 결과를 `.agent-cli/tmp/` 에 다시 불러올 수 있는 파일로 저장하고 시도마다 그 파일을 불러오라고 꼬리의 Task Guidelines 가 한 줄로 안내합니다. 저장 형식(JSON·npy·CSV …)은 모델이 고르고, 시도는 `shell` 에서 평범한 스크립트로 돕니다.

v10.66.0 부터 있던 `python` 도구(호출 사이에 변수·데이터가 남는 인터프리터)는 v10.102.0 에 지웠습니다. 상태를 프로세스 안에 두는 설계가 세 가지 문제를 만들었기 때문입니다 — ① 셀 코드가 파일이 아니라 spawn·forkserver 방식 `multiprocessing`(macOS·Windows 기본, Linux 3.14+ 기본)이 셀의 함수를 찾지 못하고 `Pool` 이 멈춤, ② 한 프로세스에 상태가 쌓여 고친 모듈이 반영되지 않고(재-import 하지 않음), fork 는 부모가 먼저 쓴 Accelerate·OpenCV·잠긴 lock 때문에 자식이 멈추며, 인터프리터를 죽이면 전부 잃음, ③ POSIX 전용 구현(`os.killpg`·`start_new_session`). 같은 GLM 벤치 9과제를 교차로 3회씩 돌려 도구 있는 판 17, 이 문장 판 19 통과(과제별로는 셋이 오르고 둘이 한 번씩 내림, 벽시계 짧음)라 지웠습니다 — docs/python-tool/DESIGN.md §0.

### 🧵 background jobs — 긴 명령은 하니스가 든다 (v10.47.0)

테스트 스위트·빌드·설치처럼 1분 넘게 도는 명령은 `shell` 에 `background: true` 를 주면 **하니스가 자식으로 띄우고** 즉시 돌아옵니다. v10.91.0 부터 전경 `timeout` 의 기본값 120초가 상한이기도 해서, 그 위의 값(모델이 600 을 넣고 10분을 막혀 있던 벤치 사례)은 자동으로 백그라운드 작업이 되고 결과 첫 줄이 그 사실을 말합니다. 명령에 `nohup`·`setsid`·`disown`·끝의 `&` 가 있으면 거부하지 않고 **지우고 결과에 적습니다**("Removed `nohup`, the trailing `&` — a background job is already detached, logged and reported by the harness") — 하니스가 이미 데몬화를 맡으므로 그 조각은 작업을 1초 만에 끝난 것으로 보이게 할 뿐입니다. 명령은 bash 가 있으면 bash 로 돕니다(전경·작업 같은 실행기; Environment 절에 `- Shell: bash` 한 줄) — 벤치에서 `[[ ]]`·`<(…)` 가 dash 에서 깨져 턴을 잃었습니다. docs/jobs/DESIGN.md §6. 출력은 `.agent-cli/tmp/jobs/<n>.log` 로 가고, 끝나면 종료 코드와 로그 마지막 줄들이 소유자(main 이면 깨우기 런, 에이전트면 inbox)에게 `🧵 job #n finished rc=0 after 2m31s` 로 옵니다. 돌고 있는 동안은 매 턴 꼬리에 `job #n · pid P · 경과 · 명령 → 로그 · stop it: kill -- -P` 로 보입니다(v10.79.0 — 프로세스 묶음째; pid 는 `sh -c` 리더라 `kill P` 는 셸만 죽입니다). 모델은 로그를 폴링하지도, `pkill` 로 프로세스를 찾지도, 끝 줄을 기다리는 monitor 를 걸지도 않습니다(셸 안내 문구 A/B 실측에서 셋 다 사고를 냈습니다 — `docs/jobs/DESIGN.md` §1).

> **one-shot 서브에이전트(`agent` `mode` \"run\")도 자기 잡을 기다립니다 (v10.84.0)**: 서브에이전트는 자기 주소(`run:<run_dir>`)를 가져 잡·모니터·체크인의 통지가 자기 메일함으로 오고, `wait` 는 런을 끝내는 대신 통지가 올 때까지 그 자리에서 기다린 뒤 이어갑니다(호출자는 어차피 결과를 기다리며 블록). 서브에이전트가 잡·모니터를 남기고 끝나면 하니스가 거두고 결과 끝에 한 줄로 알립니다. 스킬 루프는 호출자 턴 안의 인라인이라 `wait` 가 거부됩니다(호출자가 기다립니다). 설계 `docs/wait/DESIGN.md` §7.

- 할 일이 없으면 `wait` 로 턴을 끝냅니다(v10.65.0) — 열린 요청은 닫히지 않고 통지 런으로 넘어갑니다.
- **중간 깨우기 (v10.79.0)**: 받아들여진 `wait` 가 5분 안에 깨지 않으면 하니스가 `⏰ Check-in after 5m — still running: …` 으로 깨웁니다 — 작업마다 경과 시간·로그 변화("+37 lines since your wait" / "nothing after the command line, unchanged since your wait")·`kill -- -<pid>`, 모니터의 남은 시간, 기다리는 에이전트의 걸음 수. 같은 대상을 다시 기다리면 10·20·40·60분으로 늘고, 집합이 바뀌면 5분부터입니다. 알림은 사실 진술이라 진짜 보고와 겹쳐도 거짓이 아니고, 장부 항목이 되지 않으며 재시작을 넘어 남지 않습니다. 벤치에서 받아들여진 `wait` 50번 중 13번이 깨지 못한 채 과제 시간으로 끝난 것이 계기이고, 목적은 헤드리스 세션이 끝없이 잠들지 않게 하는 안전성입니다 — `docs/wait/DESIGN.md` §6
- 돌고 있는 작업은 세션의 "할 일" 입니다: `run` 은 끝나길 기다리며 `🧵 백그라운드 작업 N개 대기 중` 을 알리고, `web` 은 `--idle-timeout` 으로 자기를 거두지 않습니다.
- **web 방에서 사람이 작업을 멈춥니다 (v10.121.0)**: 돌고 있는 작업마다 입력창 위에 `🧵 #N · 명령 · 경과 · ■` 칩이 섭니다(경과는 브라우저가 셉니다). ■ → 칩 안의 `Stop job #N?` → Stop 이면 프로세스 묶음에 TERM, 3초 뒤 KILL. 소유자(main 또는 그 작업을 띄운 에이전트)에게 보통 종료 통지와 같은 모양으로 `🧵 job #N stopped by user <당신의 닉네임> after 12m — \`cmd\`` 가 갑니다(신호 번호는 싣지 않습니다 — rc=-15 를 하니스가 죽인 것으로 읽고 되살렸습니다; 멈추기 전에 스스로 끝났으면 `· it had already exited, rc=0`) — 모델이 rc=-15 를 "죽었다" 로 읽고 같은 명령을 다시 띄우지 않게, 누가 멈췄는지 사실로 적습니다. 보드 카드에는 `🧵 N` 칩(status.json `jobs`). `complete` 로 끝난 런이 끝나지 않는 작업(서버 등)을 남기면 web 방은 거둬지지 않으므로 — 하니스는 유한·무한 작업을 구별하지 않습니다 — 이 칩이 그 이유를 보여 주고 사람이 끝낼 수 있게 합니다. `run` 에는 UI 가 없습니다(Ctrl-C 가 전부 정리). SIGTERM 으로 끝내도(보드가 방을 끌 때, v10.134.1) 같은 정리가 돕니다 — 작업·모니터·에이전트를 거두고 세션을 저장한 뒤 신호 15 로 끝납니다. `run` 도 웹처럼 main 이 멈추기를 기다린 뒤 정리합니다(v10.177.2).
- **전경 명령이 남긴 프로세스도 작업이 됩니다 (v10.135.0)**: 모델이 전경 `shell` 로 `nohup python3 -m http.server 8877 > /dev/null 2>&1 &` 처럼 출력을 돌린 `&` 자식을 남기면, 명령이 끝난 뒤 그 프로세스 그룹을 하니스가 작업으로 넘겨받습니다 — 결과에 `[1 process kept running after this command; now tracked as job #K — …]` 한 줄이 붙고, 꼬리·칩에 보이고, 세션이 끝나거나 그 에이전트가 끝나면 멈춥니다(바로 죽이지 않습니다 — 띄운 서버를 다음 명령에서 쓸 수 있습니다). 종료 코드는 알 수 없어 통지에 `(exit code unknown)` 으로 적습니다. `setsid` 처럼 스스로 새 세션을 만든 프로세스는 그룹 밖이라 넘겨받지 못합니다. docs/jobs/DESIGN.md §7.
- 에이전트가 죽으면 그 에이전트의 작업은 죽이고, 세션이 끝날 때 남은 작업도 죽이며 콘솔에 적습니다(우리가 띄운 것이라 고아를 남기지 않습니다). 재시작을 넘어 살지 않습니다.
- 그냥 `cmd &` 도 됩니다(추적 없음). `background: true` 에는 **평문 명령**을 줍니다(`&`·리다이렉션 없이 — 하니스가 로그와 종료 코드를 맡습니다).
- `run --result-file`: 질의 런이 백그라운드 작업을 썼으면(답을 통지 뒤로 미룬 것) 그 뒤 통지로 깨어난 런들의 답이 결과 파일에 덮어써지고, 깨우기 런이 작업을 띄우지도 돌리지도 않을 때 사슬이 닫힙니다(v10.34.1 "argv 런의 답만" 규칙의 유일한 예외). `background: true` 에 `&`·`nohup`·`setsid` 가 섞인 명령은 거부합니다 — 평문으로 다시 보내면 됩니다.

### 📸 snapshot — 망친 입력 되돌리기 (v10.36.0)

하니스가 작업 디렉터리를 **git 그림자 저장소**(`.agent-cli/snapshot.git`, bare — 작업
디렉터리엔 `.git` 이 생기지 않고, 이미 git 저장소여도 그 히스토리를 건드리지 않음)에
자동으로 커밋해 둡니다: 세션 시작 때 한 번(변경이 없어도 — 세션의 경계, 라벨에 세션 id),
그 뒤 `shell`·`edit_file`·`write_file` 호출 **직전**마다(최소 10초 간격, 변경 없으면
커밋 없음), 그리고 받아들여진 `wait` 가 기다리기 직전(`before wait (turn 3)` — 간격과 무관,
변경 없으면 커밋 없음, v10.163.0). 라벨은 `before shell (turn 7): sqlite3 main.db` 처럼 그 호출의 요약(명령·경로 앞
60자)을 담아 목록만 보고 "망치기 직전" 을 고를 수 있습니다(v10.80.0). 모델에겐 시스템
프롬프트로 "망쳤으면 `snapshot` 도구(mode restore)" 만 알립니다.

- 왜 지시가 아니라 구조인가: "손대기 전에 백업하라" 는 지시는 로컬 35B 급이 무시합니다
  (실측 0/5). 반면 "망치면 여기서 복구하라" 는 구조는 따릅니다 — 벤치 `db-wal-recovery`
  (백업 없이 DB 를 열어 WAL 이 소멸하는 과제)가 0/5 → 2/3, 세 trial 모두 안내대로 복구.
- 복구는 **네이티브 도구 `snapshot`** 입니다 — `{"mode":"restore","path":"main.db-wal"}` 한
  호출(기본은 **지금 디스크와 다른 가장 최근 판** — 망가진 상태가 다음 체크포인트에 커밋돼
  있어도 그 전 판을 고름; `at` 로 특정 스냅샷), `{"mode":"list"}` 는 스냅샷 목록(`path` 를
  주면 그 경로가 바뀐 스냅샷만). 경로는 작업 디렉터리 기준 상대 또는 그 안의 절대 경로
  (v10.80.0). 복구 결과는 원인의 자리를 함께 말합니다:
  `restored main.db-wal from 610f3ac — intact at "before shell (turn 7): sqlite3 main.db",
  changed by "before shell (turn 9): ls"` — 같은 명령을 또 돌려 또 망치는 반복을 끊는 재료.
  `monitor`·`schedule` 처럼 프로세스 안의 저장소를 직접 씁니다 — 쉘로 CLI 를 치게 하는 안은
  PATH 에 기대고 도구와 중복이라 출시 전에 뺐습니다. 사람은 `git --git-dir=.agent-cli/snapshot.git
  --work-tree=. log/checkout` 으로 같은 저장소를 봅니다. 저장소는 세션을 넘어 이어지며
  `session start (<세션 id>)` 커밋이 경계입니다(docs/snapshot/DESIGN.md §6).
- 범위: 작업 디렉터리 전체. 워크스페이스의 `.gitignore` 는 **무시**합니다(보호해야 할
  `*.db`·`data/` 가 거기 적혀 있기 일쑤). 제외: `.agent-cli`·`node_modules`·`__pycache__`·`.venv`.
  하위 git 저장소는 참조(gitlink)로만 기록되고 그 안의 커밋 안 된 변경은 담기지 않습니다 —
  안내가 "그 저장소의 `git checkout`" 으로 가릅니다.
- git 이 없으면(또는 끄면) 기능이 꺼지고 사람에게만 한 줄(`📸 스냅샷 꺼짐`) — 모델에게는
  안내도 가지 않고 **`snapshot` 도구 자체가 프롬프트에서 빠집니다**(없는 기능을 약속하지
  않음). 사용자 git 설정(이름·메일·gpgsign·hooksPath)은 보지 않습니다.
- 노브: `AGENT_CLI_SNAPSHOT=0` 끔 · `AGENT_CLI_SNAPSHOT_MIN_INTERVAL_S` · 상한 장치
  `AGENT_CLI_SNAPSHOT_FILE_MAX_BYTES`(초과 파일 제외) · `AGENT_CLI_SNAPSHOT_FILE_COUNT_MAX`
  (초과 시 시작 스냅샷만) — 둘 다 기본 **무제한**.
- 구현은 내장 훅 `agent_cli/hooks/builtin/10_snapshot.py` + `agent_cli/snapshot.py`
  (docs/snapshot/DESIGN.md). 프로젝트 훅이 덧붙일 수 있습니다.

### ⏰ schedule — 예약 실행

정해진 시각마다 요청을 자동으로 넣습니다. "매주 월요일 9시에 주간 보고 써줘"
처럼 말하면 에이전트가 `schedule` 도구로 등록하고, web 화면의 ⏰ 서랍에서 직접
추가·삭제할 수도 있습니다.

```json
{"action": "schedule", "mode": "add", "cron": "0 9 * * 1",
 "prompt": "주간 보고를 작성해줘", "label": "주간 보고"}
```

- **스케줄러는 이 프로세스 안에 있습니다** (v10.12.0) — 보드 없이 쓰는 `agent-cli web`/`run` 에도
  항상 등록됩니다
- **예약은 세션의 것입니다.** 세션 폴더의 `schedules.json` 에 저장되고,
  `--resume` 으로 이어 열면 그대로 살아납니다
- **켜진 예약이 있으면 꺼지지 않습니다.** `web` 은 접속자가 없어도
  `--idle-timeout` 으로 스스로 종료하지 않고, `run` 은 작업이 끝나도 종료하지
  않고 기다립니다("⏰ 예약 N개 대기 중 … Ctrl-C 로 종료합니다"). 끝나 버리면
  "매시간 확인하겠다" 는 약속이 조용히 사라지기 때문입니다
- **놓친 예약은 자동 실행하지 않습니다.** 프로세스가 꺼져 있었거나 컴퓨터가
  절전 중이어서 2분 넘게 늦은 발화는 `Missed` 로 남고, web 화면 위쪽에서
  "Run now / Skip" 을 묻습니다. 여러 번 놓쳐도 질문은 가장 최근 것
  하나입니다. `run` 은 기동 때 알리기만 합니다
- cron 은 5필드(`분 시 일 월 요일`), **서버 로컬 시각**입니다. `*`, 숫자,
  목록(`1,3,5`), 범위(`1-5`), 스텝(`*/15`)을 지원합니다
- 에이전트가 등록할 수 있는 예약은 세션당 5개입니다(사용자 등록분은 제한 없음)
- 발화·놓침·건너뛰기·실패는 세션 폴더의 `schedule-log.jsonl` 에 남습니다
- **한 세션은 한 프로세스만 엽니다.** 이미 열려 있는 세션을 `--resume` 하면
  `session <id> is already open in pid N` 으로 거부됩니다 — 두 프로세스가 같은
  예약을 각자 발화하는 것을 막습니다

### 민감 경로 사전

봉쇄가 **경계**를 본다면, 사전은 **무엇을 읽느냐**를 봅니다. 자격증명으로 알려진
경로는 워크스페이스 안이든 밖이든, 읽기든 쓰기든 확인을 받습니다.

- **대상**: `shell` 과 **`read_file` 둘 다**. `cat ~/.ssh/id_rsa` 는 묻는데 도구로는
  조용히 읽힌다면 그건 일관성이 아니라 우연입니다.
- **무엇이 들어 있나**: 자격증명 디렉터리(`~/.ssh` `~/.aws` `~/.gnupg`
  `~/.config/gcloud` `~/.azure` `~/.kube` 키체인/키링 `~/.password-store`
  `~/.config/op`) · 상시 읽는 디렉터리 **안의 정확한 파일**(`~/.docker/config.json`
  `~/.npmrc` `~/.cargo/credentials.toml` `~/.config/gh/hosts.yml` `~/.netrc`
  셸·DB 히스토리 `/etc/shadow` 계열) · 비밀키 파일명(`id_rsa` 계열 — `.pub` 제외,
  `privkey.pem`, `*.p12`/`*.pfx`/`*.jks`) · **agent-cli 자신의
  `~/.agent-cli/config.json`**(여기에 `api_key`·`extra_headers` 가 있습니다).
- **일부러 넣지 않은 것**: `.env`(에이전트는 보통 그 `.env` 를 가진 프로젝트를
  작업 중입니다) · `*.pem`(CA 번들이 지천) · `*.key`(macOS 에선 Keynote 문서) ·
  `~/.zshrc`(실제 유출 벡터지만 PATH 디버깅으로 매일 읽습니다) ·
  `~/.config/`·`~/.cargo/` 같은 **디렉터리**(툴체인·캐시). **오탐 하나가 게이트
  전체를 무의미하게 만들기 때문**입니다 — 정밀도가 재현율보다 중요합니다.
- **macOS**: `/etc`→`/private/etc` 심볼릭, `/System/Volumes/Data/...` firmlink,
  APFS 대소문자 무시를 모두 정규화해 비교합니다(셋 다 실제 우회 경로였습니다).

## 모델 권장 사양

에이전트 루프는 JSON 포맷 준수 + 도구 선택 + 멀티스텝 reasoning을 동시에 요구합니다. 모델 크기에 따라 성능 차이가 큽니다:

| 모델 크기 | 멀티스텝 태스크 | 단순 질의 | 비고 |
|-----------|---------------|----------|------|
| **7B 이하** | ❌ | △ | 도구 혼동, JSON 포맷 불안정, 반복 실패 빈번 |
| **14-30B** | △ | ✅ | 간단한 도구 사용 가능, 복잡한 스킬은 불안정 |
| **32B+** | ✅ | ✅ | 안정적 — 권장 최소 사양 |
| **70B+** | ✅✅ | ✅ | agent 위임, 복잡한 스킬 등 고급 기능 안정 |

> **인라인 추론 태그 자동 격리 (5.10.0)**: 일부 모델(MiMo 등)이 응답 content 안에 `<think>…</think>` 류 태그로 긴 추론을 흘립니다 — provider 응답 조립 지점에서 이를 제거해 wire 파싱·컨텍스트를 보호하고, 제거분은 `thinking` 필드로 이동해 `--verbose` 기록(`verbose.jsonl` 의 `thinking`)에 남습니다 (닫히지 않은 `<think>` 는 그 지점부터 전부 추론으로 간주). 태그 4종: think/thinking/reasoning/reflection.

**최소: 30B, 권장: 32B+**

권장 사양은 OpenAI 호환 서버로 서빙하는 로컬/온프렘 모델 기준입니다. 32B+ 클래스 모델에서 멀티스텝 태스크가 안정적으로 동작합니다.

## 명령어

### `run` — 단발 실행

```bash
agent-cli run "task description" [options]
```

| 옵션 | 설명 | 기본값 |
|------|------|--------|
| `--endpoint NAME` | 쓸 모델 서버 — `config.json` 에 저장된 이름 (v10.144.0; env `AGENT_CLI_ENDPOINT`). 세션에 기록되고 `--resume` 이 그 이름으로 뜬다. 없는 이름은 종료 코드 `2` | 세션 기록 → config `endpoint` |
| `-p, --provider` | `openai` / `anthropic` — 고른 서버의 값을 덮는다 | 서버의 값 → `openai` |
| `-m, --model` | 모델 ID | 서버의 `default_model` |
| `--base-url` | API 엔드포인트 — 고른 서버의 값을 덮는다. 주소가 어디에도 없으면(서버에 `base_url` 없음·이름 없는 실행에 `AGENT_CLI_BASE_URL` 없음·서버와 다른 `-p`) 종료 코드 `2` — 프로바이더 공개 주소로 가지 않는다 (v10.144.0) | 서버의 값 |
| `--api-key` | API 키 — 고른 서버의 값을 덮는다 | 서버의 값 → `OPENAI_API_KEY`·`ANTHROPIC_API_KEY` |
| `-n, --max-turns` | 최대 턴 (0=무제한) | `0` |
| `--max-context-tokens` | 컨텍스트 윈도우 토큰 상한 (0=모델에서 자동 결정) | `0` |
| `--max-depth` | 중첩 깊이 (agent + skill 합산). 한계 도달 시 두 도구 모두 자동 비활성. | `2` |
| `--stall` | 스트림 무진전 한도 — `600`(초)·`10m`(분)·`0`(끔) (env `AGENT_CLI_STREAM_IDLE_TIMEOUT_S`, 웹 ⏳ 칩의 첫 입력) | `10m` |
| `--stall-attempts` | 무진전 시 **총** 전송 횟수(첫 전송 포함, 1~10). 최대 대기 = `--stall` × 이 값 (env `AGENT_CLI_STREAM_MAX_ATTEMPTS`, 웹 ⏳ 칩의 둘째 입력; 한도 0 이면 잠김) | `4` |
| `--compaction-ratio` | 컨텍스트 압축 목표 비율 (0.5~1.0). 1.0 = 생성이 창에 잘릴 때만 압축. 낮을수록 일찍 압축 (env `AGENT_CLI_COMPACTION_RATIO`, 웹 🗜️ 슬라이더) | `1.0` |
| `--max-agents` | 동시 생존 서브에이전트 상한 (0 = 무제한) (env `AGENT_CLI_MAX_AGENTS`, 웹 👥 칩) | `10` |
| `--model` / `-m` | 모델 ID — 노브이기도 하다 (v10.43.0): 세션 도중 바꾸면 다음 LLM 호출부터 (이 세션 서버의 models.json 칸에 등록되고 이 세션의 방언에 묶인 모델만; env `AGENT_CLI_MODEL`, 웹 🤖 칩 — 같은 서버·같은 방언의 모델 목록에서 고른다, v10.45.0) | 서버의 `default_model` |
| `--thinking` | 사고 `off`·`on` — 다음 LLM 요청부터 (v10.43.0; env `AGENT_CLI_THINKING`, 웹 🧠 노브). 정도는 서버 기본값(v10.72.0: `auto`·`--reasoning-effort` 제거) | `off` |
| `--grammar` | 디코딩 문법 제약 `auto`(지원 기록된 모델에서 켬)·`on`·`off` (env `AGENT_CLI_GRAMMAR`, 웹 📐 노브) | `auto` |
| `--temperature` | 샘플링 temperature 0~2 또는 `auto`(기본 — 보내지 않음: `models.json` 엔트리 → 서버 기본) — 다음 LLM 요청부터 (v10.69.0; env `AGENT_CLI_TEMPERATURE`, 웹 🎲 칩의 첫 입력) | `auto` |
| `--top-p` | 샘플링 top_p 0~1 또는 `auto`(기본 — 보내지 않음) (env `AGENT_CLI_TOP_P`, 웹 🎲 칩의 둘째 입력) | `auto` |
| `--auto-approve` / `--no-auto-approve` | 확인 프롬프트를 묻지 않고 허용 (env `AGENT_CLI_AUTO_APPROVE`, 웹 ⚡ 노브) | 꺼짐 |
| `--auditor` / `--no-auditor` | 세션 감사 (env `AGENT_CLI_AUDITOR`, 웹 🔎 칩; 아래 "세션 감사자") | 꺼짐 |
| `--image PATH` | QUERY 에 이미지를 붙인다 (PNG/JPEG/GIF/WebP, 반복 가능). 워크스페이스 밖 파일은 `.agent-cli/tmp/uploads/` 로 복사. 모델이 이미지를 못 보면 즉시 오류 (v10.59.0, 아래 "이미지 입력") | |
| `--title TEXT` | **방 제목** (v10.71.0) — 배너 `Title:` 줄에 보이고 세션 메타(`session.jsonl`)에 남아 `--resume` 이 지킨다(다시 주면 교체). `web` 도 같은 옵션 — 헤더 첫 줄(자기 줄, v10.74.1)·브라우저 탭·`web.json` 에. 보드는 글의 topic 을 넘긴다 | (없음) |

> 위 노브 열 축(`--thinking`·`--grammar`·`--temperature`·`--top-p`·`--max-agents`·`--compaction-ratio`·`--stall`·`--stall-attempts`·`--auto-approve`·`--auditor`)은 `agent_cli/knobs.py` 가 **한 번 선언**하고 `run`·`web` 양쪽 플래그·env·웹 노브가 거기서 파생됩니다 (v10.43.0). 우선순위는 플래그 > env > 기본값, 전부 세션 한정입니다.
| `--result-file` | 최종 답변(원문)을 지정 경로에 기록 — 렌더러 장식 없는 기계 소비용(스크립팅). **argv 질의를 든 런의 답만** 씁니다 (v10.34.1 — 그 런이 질의의 요청 번호를 들었는가로 가립니다): 깨우기 런(에이전트 회신·모니터 보고/만료 통지)과 예약 발화 런의 답은 그 통지에 대한 반응이라 쓰지 않고, 질의 런이 실패했으면 파일을 만들지 않습니다(뒤 런의 답으로 채우지도 않음 — 실패가 성공으로 둔갑하는 것을 막기 위해). `@profile task` 는 관찰 래퍼(STATUS/RESULT)를 벗긴 원문, `/skill` 의 답도 기록(v10.34.1). 런이 성공으로 끝나는 즉시(v10.31.2) 같은 디렉터리의 temp 를 거쳐 원자적으로 쓰고, 빈 답은 쓰지 않습니다. 실패/무답 시 파일 미생성 — 종료 코드(아래 표, v10.124.0)가 **같은 판정**이라 "파일 없음 + 0" 은 빈 답, "파일 없음 + 1" 은 실패입니다. 실패한 `/skill` 의 오류문은 답이 아니라 쓰지 않습니다(v10.124.0) | (없음) |
| `-v, --verbose` | 모든 LLM 호출을 세션 폴더의 `verbose.jsonl` 에 기록 (화면 출력 없음) — 아래 **verbose 기록** 참고 | |
| `--style` | 렌더러 스타일 (minimal 또는 커스텀 — `agent_cli/render/<name>.py` 플러그인. 커스텀 렌더러의 필수 구현은 **9개**(출력 코어 7 + 입력 2, v4.50.0)로 축소 — 디버그/장식 메서드는 안전한 기본값) | `minimal` |
| `--record-turns / --no-record-turns` | 세션 디렉토리에 `turns.jsonl` 기록 — 턴별 parse 결과·실패 신호·회복 primitive 에 더해 **프로바이더 토큰 사용량**(`input_tokens`/`output_tokens`/`cache_read_input_tokens`/`cache_creation_input_tokens`, `TokenUsage` 와 동일 의미 — 세션 비용·캐시 적중률은 행 합산으로 산출, v8.49.0). v10.129.0 부터 줄마다 `harness_version`·`dialect` 와 클라이언트가 잰 `llm_ms`·`ttft_ms`·`tool_ms`(사람 승인 대기 포함)·`tools`(`[이름, 성공, ms]`)·`output_chars`·`thinking_chars`·`reasoning_tokens` — 못 잰 값은 0 이 아니라 키가 없다(필드 표: docs/ARCHITECTURE.md §7.7). prompt·응답 본문·도구 인자·경로 미포함 | `--record-turns` |

**종료 코드 (v10.124.0)** — `--result-file` 과 **같은 판정**입니다(플래그를 줬는지와 무관):

| 코드 | 뜻 |
|---|---|
| `0` | 답했다 — argv 질의의 답 사슬(질의 런 + 질의가 백그라운드 작업·`wait` 로 답을 미뤘으면 그 뒤 깨우기 런)의 **마지막 런**이 성공. 빈 답 성공도 0(파일은 안 생김). 목록 명령(`/help`·`/skills`·`@agents`·`@`) — `--resume` 없이 부르면 provider·서버·세션 없이 부트 전에 답하고 `sessions` 에 빈 세션을 남기지 않습니다(v10.127.1; `/help` 는 `--resume` 이어도 부트 전, 목록은 `--resume` 이면 살아난 상주 에이전트까지 보이도록 부트 뒤) |
| `1` | 실패/무답 — LLM 호출 실패·max turns·실패한 `@agent task`·`/skill`, 못 찾은 명령(`Unknown command`·`Agent not found`). 잡히지 않은 예외도 1(traceback) |
| `2` | 설정·사용법 오류 — 모르는 provider, 읽지 못한 `config.json`, 규칙을 어긴 `extra_headers`, 방언 없는 모델, 잘못된 옵션, 빈 질의(글도 이미지도 없다 — `query is empty`, 세션을 만들지 않는다) |
| `130` | 중단(Ctrl-C — 멈출 것이 없을 때, 또는 강제 종료) |

**터미널에서 질문에 답하기 (v10.174.0, v10.176.0)**: 누가 사람에게 물으면(main·에이전트·스킬) 터미널에 `❓ [main → user] q-2`(에이전트는 `❓ [aa (agt-…) → user] q-3`, 스킬 런은 `❓ [skill demo → user] q-4` — 대화 창이 없는 묻는 쪽은 터미널 줄) · 질문 글 · `  answer with: /answer q-3 <text> · close with: /close q-3` 세 줄이 찍힙니다. `/answer q-3 <글>` 로 답하고, `/close q-3` 으로 답 없이 닫고, `/questions` 로 열린 질문을 봅니다. 묻는 쪽은 기다리지 않습니다 — 답은 나중에 메일로 옵니다. 터미널 `run` 은 main 이 결과를 낸 뒤 한가한 동안에도 친 줄을 읽습니다 — 보통의 말은 main 의 새 요청이고 질문의 답이 되지 않습니다. 스킬이 묻고 `wait` 하는 동안에도 친 명령(`/answer`·`/close` 등)은 그 자리에서 처리되고, 보통의 말은 main 앞에 줄 섭니다. 남은 일이 열린 질문뿐이면 `run` 은 끝나지 않고 한 줄 `❓ Waiting for your answer to q-3 — type /answer <q-N> <text> or /close <q-N>, or press Ctrl-C to quit.` 을 찍고 기다립니다(같은 대기 동안 다시 찍지 않습니다). 확인(위험 셸 승인 등)도 같은 틀입니다(v10.177.0) — 아래 "위험 명령 확인".

**Ctrl+C 와 `/stop` (v10.142.0)**: Ctrl+C **한 번은 main 의 런만** 멈춥니다(웹 Stop 과 같은 길 — 다음 턴 경계·도구 실행 직전, 돌고 있는 전경 `shell` 명령은 그 자리에서 끝남, 기록은 `[interrupt]`) — 에이전트는 계속 일하고, main 이 기다리던 회신은 다음 런에 메일로 옵니다. 터미널에 `■ main stopped (press Ctrl+C again within 2s to stop all agents)` 가 찍히고, **2초 안에 한 번 더** 누르면 main 과 일하는 에이전트 전부가 멈춥니다(`■ stopping everything — main and N working agents` — 웹의 Stop everything 과 같아 그 회신들은 main 을 깨우지 않고, `run` 은 조용해지면 끝납니다; 배달되지 않은 회신은 세션이 끝나면 버려집니다 — 아래 "재시작"). 확인(`c-N`)을 기다리는 호출도 정지에 닿습니다 — 한 번은 main 의 확인을, 두 번은 에이전트의 확인까지 깨우고 그 호출의 결과는 `This run was stopped while this call was waiting for approval [c-3] — it did not run.` 입니다(v10.177.0). main 이 한가하고 에이전트만 일하면 첫 누름은 `main is idle — press Ctrl+C again within 2s to stop all agents` 만 말합니다. 멈출 것이 없으면(main 한가·일하는 에이전트 없음) 세션을 닫습니다 — SIGTERM 과 같은 길로 main 이 멈춘 뒤 정리하고 `⚡ Interrupted.` · 130 (v10.177.2), main 이 이미 멈추는 중인데 2초 밖에서 또 누르거나 2초 안 두 번인데 멈출 것이 없으면 강제 종료(130 — 정지 검사에 닿지 못하는 도구의 탈출구; 그때 돌던 전경 `shell` 명령도 프로세스 그룹째 끝냅니다 — 종전엔 자기 세션이라 고아로 남았습니다)입니다. 제자리 대기(`⏳ waiting for …`) 중에 치는 `/stop <키|이름>` 은 그 에이전트의 런을, `/stop all` 은 일하는 에이전트 전부를 멈춥니다(main 은 계속 — `/help` 에도 있습니다). 멈춘 에이전트마다 한 줄 — `■ agt-3 │ stopped by user — reply sent to main (r-21)` · `■ agt-5 │ stopped by user — replies sent to main (r-30), agt-6 (r-31); 1 queued request kept`. 무엇이 누구에게 가는지는 아래 `agent` 절의 "정지".

사슬 밖의 런(예약 발화, 사슬이 닫힌 뒤의 깨우기, 런 도중 합류한 명령)은 판정을 바꾸지 않습니다. `run "/sh <cmd>"` 는 그 명령의 종료 코드를 그대로 돌려줍니다(시간 초과 124). Harbor 벤치 어댑터는 0·1 을 흡수해 검증기로 보냅니다(`bench/harbor/README.md`).

**실패 기록 `failures.jsonl` (v10.11.0)**: 루프가 쓸 수 없었던 생성은 **본문과 함께** 세션 디렉터리(에이전트는 각자 디렉터리)의 `failures.jsonl` 에 한 줄씩 남습니다 — 형식 이탈(`NO_JSON`·`NO_ACTION`·`DEGENERATE`·`SCHEMA_MISMATCH` 등 dispatch 의 `failure_signal` 전부), 출력 절단(`OUTPUT_TRUNCATED`, `stop_detail` = model_cap/context_clamp/server_cap), 폭주(`RUNAWAY`, 감지 규칙), 삼켜진 출력. 필드: `v, ts, turn, model, dialect, failure_signal, stop_reason, stop_detail, parse_stage, primitives, usage, text, text_truncated, thinking, tool_calls, tool_calls_truncated`. **`tool_calls` (v10.38.0, 기록 `v`=2)** 는 서버가 구조로 준 호출 목록 그대로입니다 — `[{id, name, input}]`, 인자가 JSON 이 아니었으면 `input` 대신 원문 `arguments`. native 함수 호출은 호출이 `text` 밖에 있어 거부된 호출의 인자도 여기에 남습니다. 인자 안의 문자열도 본문과 같은 1,000,000자 규칙으로 잘리고 `tool_calls_truncated` 가 켜집니다. `--verbose` 의 `llm_call` 도 같은 필드를 싣습니다(두 기록은 한 헬퍼에서 필드를 받는다). `turns.jsonl` 은 본문을 싣지 않고(사생활 계약) `verbose.jsonl` 은 `--verbose` 일 때만 쓰므로, 67qcmb 의 32K 폭주처럼 "모델이 실제로 뭘 냈나" 를 사후에 볼 길이 없던 구멍을 메웁니다. 항상 켜져 있고 깨끗한 세션은 파일이 생기지 않습니다. 본문은 1,000,000자에서 머리/꼬리로 잘리고 `text_truncated` 가 켜집니다.

```bash
jq -c '{turn, failure_signal, stop_reason, stop_detail, chars: (.text|length)}' .agent-cli/sessions/<id>/failures.jsonl
# 거부된 호출과 그 인자 (native 함수 호출)
jq -c 'select(.tool_calls) | {turn, failure_signal, calls: [.tool_calls[] | {name, input}]}' .agent-cli/sessions/<id>/failures.jsonl
```
| `--dialect` | Dialect 플러그인 이름. 빌트인: `json_fc` (산문 reasoning + flat `{action, params}` op 들의 bare JSON 배열로 한 턴에 여러 독립 도구 호출, 종료는 `complete` op. md_array 의 리네임+리셰이프 후계 — 마크다운 헤더 제거, v6.0.0 bakeoff A/B 140run 에서 구형과 동등 확인. 구 `## Thought/## Action` emission 도 drift 로 관용), `xml_fc` (태그-파라미터 `<tool_call><function=X><parameter=k>v</parameter></function></tool_call>` — 파라미터 값이 raw 텍스트라 파일 본문/최종 답변에 JSON escaping 불필요. `<tool_call>` XML 프라이어 모델용. **2026-07-17 Qwen 실측**: 27B=natively 동등, 35B-A3B=구제 하니스(lenient+foreign, 무-왕복)로 완주 100%·실재시도 0.06/run — 양쪽 바인딩 가능 — `docs/dialects/PHASE2.md` §8). `hermes_json` (`<tool_call>{"name": …, "arguments": {…}}</tool_call>` — Hermes 2~4·Qwen2.5/3/Next·Granite 4.0/4.1 의 네이티브 모양, **실모델 미검증**: 파서·문법·foreign 구제 테스트만), `glm_argkey` (`<tool_call>NAME<arg_key>k</arg_key><arg_value>v</arg_value></tool_call>` — GLM-4.5~5.3, **실모델 미검증**), `native_fc` (서버 네이티브 함수 호출 — OpenAI 호환 서버가 요청의 `tools` 를 받고 `tool_calls` 를 돌려줘야 함, v10.2.0). v9.27.0 부터 포맷은 코드가 아니라 **스펙(데이터)** 이다 — `agent_cli/dialects/specs/` 에 `DialectSpec` 하나를 추가하면 렌더·파서·문법·구제가 엔진에서 나온다(`docs/dialects/PHASE5.md`). `agent_cli/dialects/`에 모듈을 추가하면 자동 등록. 미등록 이름은 LLM 호출 전에 즉시 실패. **지정하면 세션 전체(main 과 모든 서브에이전트)에 강제**. 미지정이면 각 에이전트는 자기 모델의 models.json `dialect` 바인딩을 쓰고, **바인딩 없는 모델은 에러**(기본값 없음, v10.3.0) — 터미널의 `run` 은 main 모델의 방언을 그 자리에서 묻고 저장합니다(v10.127.0). `run --help` 의 이 옵션 설명은 등록된 방언 전부를 스펙의 한 줄 설명과 함께 나열합니다 | (모델 바인딩) |
| `--resume <id>` | 이전 세션을 로드해 복원된 컨텍스트 위에 QUERY 를 이어지는 요청으로 실행. `web --resume` 과 같은 on-disk 세션이라 **run↔web 상호 이어가기** 가능 (v4.46.0) | (새 세션) |

**방언(dialect) 개명 (v10.0.0)**: 모델 응답의 tool-call 모양을 가리키던 "wire format" 을 **dialect(방언)** 으로 부릅니다 — 패키지 `agent_cli.dialects`, 플래그 `--dialect`, models.json 키 `dialect`. 방언은 이제 코드가 아니라 **데이터**입니다: `agent_cli/dialects/specs/` 의 `DialectSpec` 하나(호출 단위·이름 위치·인자 포맷·파라미터 템플릿 + 산문 조각)를 공용 엔진이 읽어 파서·문법·렌더·구제 문구를 전부 유도합니다. 옛 두 방언(`json_fc`·`xml_fc`)은 코퍼스 849건에서 옛 손 코딩 모듈과 **바이트 동일**을 확인했고, `hermes_json`(Hermes·Qwen2.5/3·Granite 4.0/4.1 프라이어)·`glm_argkey`(GLM-4.5~5.3)가 스펙만으로 추가됐습니다(실모델 미검증). **호환**: `--response-format` 은 같은 파라미터의 별칭, models.json 의 옛 키 `wire_format` 은 계속 읽고(자동 저장은 `dialect`), 옛 세션의 메타 키 `response_format` 은 resume 때 영구히 읽습니다. `import agent_cli.wire_formats` 는 재수출 shim(DeprecationWarning). 별칭·shim 은 v11 에서 빠집니다. 설계: `docs/dialects/PHASE5.md`.

**verbose 기록 (v9.24.3)**: `--verbose`(run·web)는 화면에 찍지 않고 메인 세션 폴더의 `verbose.jsonl` 에 JSON 한 줄씩 기록합니다. 메인 에이전트뿐 아니라 **하위 에이전트·스킬·상주 에이전트의 호출도 같은 파일에** `scope`(메인 세션 기준 상대 경로 — `main`, `agents/agt-1a2b`, `run_task_…`)로 구분돼 쌓입니다. **첫 줄은 JSON Schema** 라 파일이 자기 필드를 설명하고, `grep`·`cat` 대신 JSON 쿼리로 분석합니다.

| kind | 담는 것 |
|---|---|
| `llm_call` | 모델이 쓴 원문(`text`)·사고(`thinking`)·종료 사유 + 하니스의 판정(`parse_stage`·`failure_signal`·`primitives`)·파싱된 호출(`ops`)·서버가 구조로 준 호출(`tool_calls`, v10.38.0 — native 함수 호출은 호출이 여기에만 있다)·`stop_detail`·토큰·문법 사용 |
| `llm_error` | LLM 호출 자체의 실패(전송·서버 오류) |
| `context` | 호출 직전 메시지별 크기와 앞 300자 (대화 전문은 `history.jsonl`) |
| `debug` | 하니스 진단 줄 |

```bash
# 실패한 턴과 그때 모델이 실제로 쓴 것
jq -c 'select(.kind=="llm_call" and .failure_signal!=null) | {scope, turn, failure_signal, text}' verbose.jsonl
# 한 에이전트의 호출만
jq -c 'select(.scope=="agents/agt-1a2b")' verbose.jsonl
# 빈 문자열 인자를 담은 도구 호출
jq -c 'select(.kind=="llm_call") | .ops[]? | select(any(.input[]?; . == ""))' verbose.jsonl
```

`turns.jsonl`(`--record-turns`, 기본 켬)은 모델 텍스트 없이 구조만 담는 항상-켬 기록이고, `verbose.jsonl` 은 원문까지 담는 선택 기록입니다. 원문을 매 호출 남기므로 크기가 커질 수 있습니다.

`run` 실행 후 세션이 자동 저장됩니다. `run --resume <id>` 또는 `web --resume <id>`로 이어서 작업할 수 있습니다:

```bash
$ agent-cli run "Analyze the project structure"
# ... 실행 결과 ...
# Session 1774752167 saved. Resume with: agent-cli run/web --resume 1774752167

$ agent-cli run "이어서 테스트도 작성해" --resume 1774752167   # CLI 로 이어서
$ agent-cli web --resume 1774752167   # 또는 브라우저 UI 로 이어서
```

resume 시 `web`은 이전 대화(turn)를 UI에 그대로 재생해 어디서 끊겼는지 바로 확인할 수 있습니다 (중간 도구 호출/관찰 포함). **Team 뷰의 활동 막대**(스킬 밴드·일회성 run·팀원 작업 구간)도 함께 복구됩니다 — 이 이벤트는 대화 히스토리에 없어 세션 디렉토리의 `scopes.jsonl` 사이드카에서 재생됩니다. **사용자 레인과 ✓ 회신 화살표도 복구됩니다** (히스토리 user 레코드의 작성자 + complete 귀속을 재생에서 동일 규칙으로 재도출; v8.2.0 이전에 기록된 세션은 작성자 정보가 없어 화살표 없이 카드만 복원).

### `web` — 브라우저 방

```bash
pip install 'agent-cli[web]'   # 옵션 의존성 설치 (FastAPI, uvicorn)
agent-cli web [-p openai] [-m model] [--port N] [--token <hex>]
agent-cli web --resume <session_id>    # 이전 세션 이어서 시작
```

옵션 의존성이 없으면 `web` 은 **빠진 패키지를 직접 댄 설치 명령**을 보여 주고 종료 코드 `1` 로 끝납니다(v10.123.3) —
`Install with: pip install 'fastapi>=0.110' 'uvicorn[standard]>=0.27' 'sse-starlette>=2.0'`. 목록은 설치 메타데이터(pyproject 의 `web` extra)에서
읽습니다. agent-cli 는 PyPI 에 없어 `pip install agent-cli[web]` 은 패키지를 못 찾습니다. 설치 없이 소스로 돌리면 `the "web" extra` 라고만 말합니다.

agent-cli 인스턴스 하나를 단일 세션으로 브라우저 UI 에 노출. **기본 바인드는 loopback(`127.0.0.1`)** — LAN 노출은 `--host 0.0.0.0` opt-in(웹 UI 가 셸을 구동할 수 있으니 신뢰된 네트워크에서만). 자동 토큰 생성(또는 `--token` 지정) — 토큰은 시작 URL 에 한 번 실려 오고, 서버가 그걸 **HttpOnly 쿠키**로 교환한 뒤엔 브라우저 URL 에서 사라집니다(주소창·로그·Referer 누출 방지). 다른 사람에게 접속을 공유하려면 헤더의 **🔗 버튼**으로 토큰 포함 링크를 복사해 전달하세요(주소창엔 토큰이 없으므로 주소창 복사로는 공유 안 됨). **다중 뷰어 (모두 동등)**: 모든 탭이 스트림을 보고 **모두 입력·큐 가능**(controller/observer 구분 없음). 각 탭은 접속 시 **재미있는 기본 닉네임**이 채워진 입력으로 이름을 정할 수 있고(✕로 기본값 유지, 한 번 정하면 다음 접속에 기억), 접속자 로스터에 닉네임이 표시됩니다. 닉네임은 접속 후에도 로스터 옆 **✎ 버튼**으로 언제든 다시 열어 변경할 수 있습니다(현재 닉네임이 채워진 채 재노출 → 즉시 로스터에 반영). **메시지 큐**: 에이전트가 실행 중이어도 메시지를 보내면 **큐에 쌓여 실시간 표시**되고, **매 턴 경계에서 대기 중 큐 전부를 한 번에 주입(drain-all, v8.16.1)** — 여러 요청이 다음 턴 LLM 컨텍스트에 **함께** 들어가 모델이 모두 보고 이어갑니다(Claude Code 식 묶음 반영). 자기가 큐한 메시지는 처리 전 ✕로 취소 가능. 주입되는 batch 앞에는 **"앞서 답하지 않은 요청도 함께 처리하라"는 시스템 notice 가 한 번 붙어(v8.14.2)**, 최신 것만 답하고 이전 요청을 흘리지 않도록 합니다(시스템 프롬프트 지침으로도 보강). **⚡ 자동 승인(확인 없이 실행, v8.17.0 · v8.18.0 헤더 이동)**: **헤더의 `⚡ 확인 없이 실행` 토글 버튼**으로 이 세션의 **위험 명령(`rm` 등)·워크스페이스 밖 접근 확인 프롬프트를 건너뛰고 자동 허용**할 수 있습니다(무엇이 실행됐는지는 타임라인에 관찰로 남음). **세션 한정·재시작 시 자동 해제**(런타임 전용)이며, 안전 게이트를 끄는 것이므로 신뢰된 작업에서만 켜세요. 기본은 꺼짐(확인 요청). **🧠 사고/추론 노력(v8.18.0)**: ctx 팝오버의 두 셀렉트로 **사고(thinking) on/off** 와 **reasoning effort(low/medium/high)** 를 세션 단위로 조절할 수 있습니다 — **다음 LLM 요청부터 반영**(공유 컨텍스트). 사고 끄기는 토큰 절약·속도↑(OpenAI 계열은 `chat_template_kwargs.enable_thinking` 지원 시 — Qwen/MLX 확인됨; Anthropic 은 `thinking` 블록 활성/비활성으로 반영). effort 는 provider 별로 번역됩니다 — **OpenAI 계열**은 `reasoning_effort` 필드(백엔드가 존중할 때 유효), **Anthropic**은 effort 를 `thinking.budget_tokens` 로 번역(low=4096 / medium=16384 / high=32768; Anthropic 은 effort enum 이 없어 budget 이 유일 레버). 기본은 **끔**(v9.24.8)입니다. 라이브 보드에서 상주 에이전트가 사고를 닫지 않은 채 사고 구간 안에서 같은 도구 호출을 끝없이 반복해, 출력 한도까지 22분을 쓴 일이 있었습니다(패킷 캡처로 확인). 문법은 사고 구간을 제약하지 않고, 사고 예산도 이 모델에는 걸리지 않았습니다. 켜려면 칩에서 `on` 을, 서버·모델 기본값에 맡기려면 `auto` 를 고릅니다. 끔은 사고 지원 모델에 스위치(`chat_template_kwargs.enable_thinking=false`)로 **명시**해 보냅니다 — Qwen 계열 템플릿은 스위치가 없으면 사고를 켭니다. **모델이 사고를 지원하지 않으면(`supports_thinking=false`) 이 컨트롤은 잠깁니다** — provider 가 사고 파라미터를 아예 보내지 않아(엄격 백엔드 400 방지, v8.21.1) 조정 자체가 무의미하기 때문입니다. **📐 문법 제약(v9.24.0)**: 서버가 문법 제약 디코딩(`guided_grammar`, omlx/xgrammar·vLLM·llama.cpp)을 지원하면 매 요청에 dialect 의 EBNF 를 실어 **도구 호출 형식을 생성 단계에서 강제**합니다 — 호출이 나오는 한 형식 오류·모르는 도구·모르는 인자·본문 안의 추론 폭주가 아예 나올 수 없습니다(원칙은 **하니스가 구제할 수 있는 모양은 문법이 막지 않는다** — 호출 없이 끝나는 턴, 빈 배열, JSON 문자열의 raw 개행, 태그 사이 공백은 모두 허용하고 복구 계층·파서가 처리합니다. 호출을 필수로 하면 EOS 가 마스킹되어 산문 폭주·가짜 호출이 나오는 것을 실측)(사고 채널은 제약하지 않고, 산문·도구 본문 모두 길이 제한 없음 — 폭주는 `max_output_tokens` 와 복구 계층이 막습니다). 비용은 **생성 속도 저하** — 규칙을 xgrammar 의 빠른 경로에 맞춘 뒤 토큰당 마스크 비용이 두 형식 모두 0.1ms 아래라(실제 토크나이저 실측; 첫 판은 json 문자열 규칙 하나가 65ms/토큰이라 −37% 였음) 실측 −6% 수준입니다(첫 토큰 시간은 동일). 헤더 칩 📐 의 배지가 사실을 말합니다: `강제`(지원+켬) / `끔`(세션에서 끔) / `미지원`(감지 결과 서버가 강제 못 함 — 잠김) / `미확인`(`models.json` 에 지원 여부가 없음 — 잠김, 모델 감지를 다시 실행하면 채워짐). 팝오버의 셀렉트(켬/끔)는 지원으로 기록된 모델에서만 열리고, 다음 요청부터 반영되며 세션 한정입니다. 켜져 있으면 생성 중 줄과 턴 통계에 📐 가 붙어 속도가 왜 다른지 그 자리에서 읽히고, 🔍 프롬프트 인스펙터의 마지막 섹션 `Decoding grammar` 에서 전문을 볼 수 있습니다(`서버 강제 · 프롬프트 아님` 태그 — 토큰을 먹지 않습니다). 지원 여부는 `models.json` 의 `supports_grammar`(true/false) — **모델 감지(프로브) 때 한 번 판정해 저장**하고, 부트에서는 프로브하지 않습니다(모델 서버가 바쁠 때 그 요청 하나에 인스턴스 열기가 수십 초 밀리는 것을 실측). 키가 없는 기존 엔트리는 미확인으로 잠기며 감지를 다시 돌리면 채워집니다. 문법이 없는 서버에선 텍스트 프로토콜 + 복구 계층이 돕니다. **스키마 일부도 문법이 강제합니다 (v9.24.4)**: 허용값이 정해진 인자(`enum` — memory 의 `mode`·`type`, edit_file 의 `op`, code_index·agent·monitor 의 `mode` 등)는 그 값들 중 하나만, `minLength: 1` 을 선언한 인자(셸 `command`, 각 도구의 `path`, memory `summary`)는 비어 있지 않은 값만 쓸 수 있고, **필수이면서 열거값인 인자**(`mode`·`op`)는 순서와 무관하게 반드시 나와야 호출을 닫을 수 있습니다. 자유 텍스트 필수 인자(`command`·`path` 등)는 강제하지 않습니다 — 억지로 쓰게 하면 명령·경로를 지어내므로 검증기가 한 번 되돌립니다. 빈 값이 정당한 인자(`write_file` 의 `content`, `edit_file` 의 `pos`)는 비워 둘 수 있습니다. 하니스의 인자 검증도 같은 선언을 보므로 문법을 못 쓰는 서버에서도 규칙이 같습니다. **v9.24.6** 부터 소통 인자(`message` 의 `to`·`text`, `reply`·`answer` 의 `text`, `answer` 의 `id`, `ask` 의 `question`)와 `run_skill` 의 `name`, `fetch` 의 `url` 도 비워 둘 수 없습니다. 빈 소통 값은 핸들러가 어차피 "nothing sent" 로 거부하던 것입니다. **xml_fc: 사고 속에는 호출 태그를 쓸 수 없음 (v9.24.8)**: 사고를 켠 세션에서 사고 구간은 `<tool_call>` 을 담을 수 없습니다. 사고 속 호출은 실행되지 않는데, 라이브 보드에서 상주 에이전트가 사고를 닫지 않은 채 그 안에서 같은 호출을 22분 동안 반복했습니다. 막히면 모델은 사고를 닫고 본문에서 호출합니다(유도 실험 16회: 사고 속 호출 4/8 → 0/8, 전부 정상 종료, 토큰당 비용 변화 없음). `a < b`, `<div>`, `</tool_call>` 같은 글은 사고 속에서도 자유롭습니다. json_fc 는 여는 시퀀스(빈 줄 뒤 `[`)를 막으면 목록 표기까지 막히므로 적용하지 않았습니다. **종결 호출은 마지막에만 (v9.24.6)**: 한 턴에 호출을 여러 개 쓸 수 있지만 `complete`·`run_skill` 뒤에는 공백과 끝만 올 수 있습니다. 하니스는 종결 호출 뒤를 실행하지 않으므로 그 뒤의 호출은 원래 무의미합니다. 그런데 라이브 보드에서 상주 에이전트가 `complete` 뒤에 끝내지 않고 호출을 또 열어, `complete` 다섯 번과 빈 `message` 반복으로 출력 한도(32K 토큰)까지 25분을 썼습니다. 하니스가 구제할 수 없는 모양이라 문법이 막습니다. 토큰당 비용은 사실상 그대로이고(비어있지 않은 본문 구간 0.002 → 0.045ms, 생성 한 토큰의 0.1%), 문법 컴파일은 오히려 줄었습니다(xml_fc 1.4초 → 0.2초 — 비어있지 않은 본문을 인자마다 펼치지 않고 규칙 하나로 참조). 기본값은 **켬(지원으로 기록된 모델에서)** 이고 `AGENT_CLI_GRAMMAR=off` 또는 칩으로 끕니다. 산문 규칙은 여는 **시퀀스**(`\n\n[` / `\n\n<tool_call>`)만 금지하므로 생각 속 `<`·`[`(정규식 lookbehind, 시프트, 인덱싱)는 자유입니다 — 문자를 통째로 막았던 첫 판은 Harbor A/B 에서 그런 토큰이 마스킹되어 추론이 뜻을 바꾸거나 폭주하는 것이 확인돼 릴리스 전에 고쳤습니다. 모든 사용자 요청은 닉네임 라벨로 task 로그에 누적됩니다. 시작 시 토큰 포함 URL이 출력되고, **로컬 bind 일 때만** 브라우저 자동 오픈(원격 `--host <ip>` 면 생략 — 아래 표 참조).

**대화 화면 — 간단 명료 · 투명성 (v9.4.0, docs/chat-ui)**: 화면은 **하나**다 — 도구 호출·reasoning 도 같은 대화 안에 보인다. **모든 줄이 같은 리듬**을 가진다 — `아이콘 · 종류 · 한 줄 요약 · 펼침표시`:

```
💭 생각   테스트 러너를 먼저 확인한다
⚡ shell  pytest tests/ -q                      ▸
✓ shell  3753 passed, 27 skipped in 38.04s     ▸
```

**기본은 한 줄, 누르면 전문** — 다 보여주되 **깊이로** 보여준다. 펼치면 요약 줄이 본문으로 바뀌어 첫 줄이 두 번 보이지 않는다(v10.119.2). 줄·스텝 카드·런 머리 항목은 **머리든 본문이든 어디를 눌러도** 펴고 접힌다(v10.143.0) — 단 글을 고르는 중이면(그 줄 안에 선택이 있으면), 링크·버튼(복사 ⧉)을 누른 것이면, 스텝 안의 줄처럼 더 안쪽의 접히는 것을 누른 것이면 토글하지 않는다. 접힌 요약은 펼친 본문과 같은 글자다 — 관찰(고정폭 12px)·메일(본문 글꼴)이 펴도 접어도 같은 글꼴·크기·색이고, 생각 줄만 접힌 요약이 기울임으로 구분된다. 실패한 스텝도 접혀 있다(v10.19.0) — 빨간 표시와 `✗ 도구 · 원인` 한 줄이 무슨 일인지 말하고, 한 번 펼치면 관찰 본문이 바로 보인다. 최종답 카드의 생각은 반대로 **펼친 채** 흐리게 보인다. 요약은 도구마다 다르고, 출력은 **첫** 유의미한 줄을 쓴다(v10.74.1 — 테스트·린트의 결론처럼 끝에 있는 것은 펼쳐 본다). **라이브 스트리밍은 버렸다** — 대신 `● 생성 중 · 4.8K tokens · 💭 사고 2.1K` 한 줄이 살아 있음을 알린다. 본문이 흐르지 않으니 카드가 자라며 화면이 튀지 않고, 사고 토큰이 따로 잡혀 러너웨이가 바로 보인다.

**대화 채널 — main 도 채널이다**: 입력창 위 칩 바에서 상대(`main` 또는 상주 에이전트)를 고르면 **화면이 그 대화만** 보여주고 입력도 그 상대에게 간다. 에이전트 창도 main 과 **똑같이 투명하다** — 생각·도구·결과가 다 보인다. 에이전트마다 key 로 정해지는 고유 이모지(🦊🐙🦉…)가 붙고, 칩엔 상태 dot(대기=초록·작업 중=주황·답변 대기=하늘·종료=회색)과 **✕ 종료 / ↻ 되살리기**가 있다.

**런 블록 (v9.23.0)**: 에이전트 채널의 단위는 런이다 — 런이 시작될 때 블록이 열리고, 받은 항목(들)이 머리(`← 본문 · 누가`), 단계가 몸통, 결과가 꼬리(최종답 카드에 `✓ (2.1s)`; 결과 없이 끝나면 상태 한 줄). 보낸 메시지는 `→ message / reply / answer` 도구 줄에 상대와 배달 상태(✓/✗)가 붙어 한 줄로 끝난다(따로 `← 받음` 줄은 없다). 아직 시작 안 한 항목은 `⏳ 대기` 로 보이다가 런이 열리면 머리로 올라가고, 사람 창 배치는 머리가 N줄이다. `reply` 없이 끝나 하네스가 요청자에게 그 사실을 알린 경우만 `⚠ 폴백` 줄(v10.177.8 — 전엔 런 요약을 대신 보냈다). resume 뒤에도 같은 블록이 선다(런 경계는 `scopes.jsonl`, 도구 줄은 에이전트 history, 수신은 `conversation.jsonl`). **점프**는 main 의 `⚡ agent` 도구 줄의 상대 이름에서만 — 그 채널의 마지막 카드가 하이라이트된다(에이전트 쪽 상대 이름은 글자일 뿐 눌리지 않는다 — peer 만 되고 main·사람은 안 되던 반쪽 버튼을 걷어냈다). 점프는 네비게이션이 아니라 **엿보기**라 **돌아가기는 한 단계**다 — 새 점프가 이전 것을 덮고, 한 번 누르면 사라진다. skill(🪄)은 말을 걸 수 없으므로 칩이 아니라 **중첩 블록**(좌측 보라 레일)으로 그려지고 머리줄로 접힌다. run 에이전트는 채널이다(아래 `agent` `mode:"run"` 참고).

**스크롤 따라가기 (v9.26.0)**: 세 규칙이다. ① 처음 열면 맨 아래. ② 바닥에서 48px 안으로 오면 그 자리에서 바닥에 붙고(`맨 아래 고정`), 이후 새 카드·스트리밍·창 크기 변화에 바닥을 따라간다. ③ 그보다 위로 올리면 보던 줄이 움직이지 않고(`위치 고정`), 그 사이 도착한 카드 수가 대화창 아래 가운데 `↓ 새 메시지 n` 버튼에 쌓인다 — 누르면 ②.

**답변 트레이는 채널과 무관**: 상주 에이전트의 질문도, 메인 LLM 의 질문·위험 명령 확인도 입력창 위 트레이에 모인다 — 다른 대화를 보는 중에도 놓치지 않고 인라인으로 답한다(누가 먼저 답하면 모두에게서 사라진다). **입력창은 항상 채팅 전용**이라 "답변 대기라 입력이 잠기는" 혼선이 없다. 하단 대기 큐는 `⏳[닉네임]` 으로 표시되다가 **`✓주입됨`**/**`✕취소됨`** 영수증을 잠시 보여준다. **main 앞 메일(에이전트 회신·모니터 보고·백그라운드 작업 종료·감사 결과 등)도 같은 대기열에 `⏳ [job] 🧵 job #1 finished …` 처럼 선다**(v10.51.0) — main 이 바쁘면 다음 턴 경계까지 거기 서 있다가 `✓ 주입됨` 으로 빠지고, 내용은 타임라인의 관찰 줄로 들어갑니다. 메일은 취소할 수 없고, main 이 놀아 곧바로 흡수되면(1.5초 안) 대기열에 나타나지 않습니다 — 관찰 줄 하나가 전부입니다(따로 도착 힌트 줄·깨우기 카드는 없습니다). 질문(❓)은 트레이로 갑니다. CLI(`run`)는 main 이 바쁠 때만 `⏳ 대기 · [job] … — 지금 턴이 끝나면 main 에 전달` 한 줄을 찍습니다. **v10.55.0 부터 메일은 main 의 열린 요청이기도 합니다** (v10.56.0 부터는 사람 요청·메일·회신 빚·질문이 꼬리의 `## Open Items` 한 목록에 닫는 순서대로 — 행동(`reply(id)`·`answer(id)`) 먼저, `complete` 의 `answers` 마지막 — 오르고, id 는 `u-1`·`n-3`·`r-5`·`q-7a3f` 처럼 접두가 닫는 법을 말합니다. `complete` 결과는 언제나 사용자에게 가고, 열린 항목이 남으면 런이 이어지되 되돌림은 런마다 8회까지입니다. main 도 에이전트의 회신 기대 메시지를 `reply(id)` 로 갚습니다. 꼬리와 안내 문구는 닫는 법을 호출 모양(`reply(id="…")`) 대신 "도구 이름 + id" 문장(`` → `reply` with id "r-1" ``)으로 적습니다 — 방언마다 호출 모양이 달라(native_fc 는 텍스트 문법이 없다) 남의 문법을 베껴 산문으로 끝내는 길을 막기 위해서입니다. 재생 측정: 끝내야 할 때 `complete` 로 끝냄 38/50 → 47/50, 산문 종료 10 → 1.): 답·모니터·작업·감사·사망 메일은 꼬리의 `## Open Requests` 에 `[n3] (mail) "🧵 job #1 finished …"` 로 오르고, 사용자 요청과 같이 `complete` 의 `answers` 로 닫힙니다(빠뜨리면 한 번 되묻고, 끝내 빠지면 최종 답에 미답으로 표시). 질문과 회신을 기대하는 메시지는 `## Owed Replies` 의 빚입니다. **v10.141.0 (사용자 결정): 에이전트의 회신(명시 `reply`·런 끝 폴백 요약)과 통지(`expects_reply` false)는 항목을 만들지 않습니다** — 회신은 도착하면 기다리던 `r-…` 항목을 닫고 내용만 전하며, 통지는 읽으면 끝입니다(`complete` 의 `answers` 에 넣을 것이 없습니다). 메일이 깨운 런은 따로 시작 메시지 없이 그 메일로 시작합니다. **🔍 프롬프트 인스펙터도 현재 대화 상대를 따른다** — 에이전트 채널에서 누르면 그 에이전트의 시스템 프롬프트+컨텍스트를 보여준다.

**📋 열린 항목 띠 (v10.119.0)**: 입력창 위 띠는 셋이고 순서는 ❓ 트레이 → 📋 열린 항목 → ⏳ 대기열입니다. 📋 띠는 모델이 지금 갚아야 하는 것 — 꼬리의 `## Open Items` 와 같은 장부 — 을 사람에게 보입니다. 행마다 id 칩(`u-2`·`n-1`·`r-1`) · 누가 · 머리말 · 닫는 법(`→ complete` / `→ complete, answers` / `→ reply "r-1"`) · 열린 뒤 지난 턴 수가 서고, 띠 머리 오른쪽에 되묻기 예산 `budget 6/8` 이 섭니다(0 이면 주황). 행을 누르면 원문 카드로 점프합니다(엿보기라 돌아가기는 한 단계). 닫힌 항목은 `✓ closed` 글자로 1.5초 남았다 사라지고(v10.119.1 — 제목은 긋지 않습니다), 열린 것이 없으면 띠가 숨습니다. 대기열 행에도 같은 id 칩이 붙어, 디큐된 요청이 같은 id 로 📋 띠에 오르는 것을 따라갈 수 있습니다. 띠는 보고 점프만 합니다 — 대기열은 취소, 트레이는 답하기라 할 수 있는 행동이 달라 합치지 않습니다. 늦게 접속한 뷰어도 같은 띠를 받습니다. 항목이 많아도 띠는 3행까지만 보이고(v10.122.0) 나머지는 끝의 `+N more ▾` 를 눌러 펼칩니다 — 펼친 목록은 화면의 약 35% 안에서 스크롤되고 머리와 `Show less ▴` 는 늘 보입니다. 닫힘 영수증은 3행에 남는 자리가 있을 때만 보입니다. 모델 입력은 바뀌지 않습니다. CLI(`run`)는 턴 경계마다 `open: u-2 n-1 r-1` 한 줄을 흐리게 찍습니다(열린 것이 없거나 직전과 같으면 생략).

**🔁 제자리 고리 (v10.120.0)**: 모델이 새 결과 없이 같은 자리를 돌면 — 호출 글자는 조금씩 바뀌어도 같은 도구가 최근 8턴 안에 이미 낸 결과만 3턴 연달아 다시 낸다(쓰기 도구·빈 출력·`(no output)` 은 세지 않고, `wait`·`monitor`·하니스 작업이 도는 동안은 세지 않는다) — 입력창 바로 위에 `🔁 No new results for N turns — the model may be going in circles.` 한 줄과 `Stop` 버튼이 섭니다. 제자리 턴이 이어지면 N 이 늘고, 새 결과가 나오거나 런이 끝나면 띠가 내려갑니다. **같은 런에서 두 번째로 걸리면 웹 main 런은 멈추고 ❓ 트레이로 묻습니다** — `c — Continue` / `s — Stop`(기본 Continue). 확인의 코멘트 칸에 쓴 글은 Hint 로, 입력창에 친 메시지와 같은 길로 다음 턴에 들어갑니다(대기열 → `[닉네임]: …` 요청, 장부 `u-N`). Stop 은 런을 끝내고 `Stopped by the user after N turns without new results.` 를 남깁니다. ⚡ 자동 승인이 켜져 있어도 묻습니다 — 자동 승인은 위험 셸·경로 확인에만 적용됩니다. 기다리는 동안 띠의 Stop 은 그 확인에 Stop 으로 답합니다. CLI(`run`)·벤치·에이전트 런은 묻지도 멈추지도 않습니다 — CLI 는 흐린 `🔁 no new results for N turns` 한 줄, 그리고 모든 발화가 `failures.jsonl` 에 `failure_signal: "stale_loop"` 로 남습니다. 모델 입력은 Hint 를 보낸 경우의 사용자 메시지 말고는 바뀌지 않습니다. 근거·규칙·등가성은 `docs/stale-guard/DESIGN.md`.

> **v9.4.0 에서 없어진 것**: 흐름(팀 스윔레인) 뷰와 `개요 · 흐름 · 전문` 탭, 전문 드로어, 라이브 스트리밍 카드. 스윔레인이 하던 "누가 무엇을 하고 있나"는 **채널 칩의 상태 dot** 이, "어디서 왔나"는 **왕래 줄과 점프**가 대신한다 — 에이전트가 하나뿐인 흔한 경우에 스윔레인은 값을 못 했고, 같은 대화를 세 벌 그리는 비용만 남았다.

| 옵션 | 설명 | 기본값 |
|---|---|---|
| `--host` | bind 주소. 기본 loopback; LAN 노출은 `--host 0.0.0.0` (opt-in) | `127.0.0.1` |
| `--port` | listen 포트. 생략 시 `0xC0DE`(49374) 우선 시도 후 사용 중이면 OS가 빈 포트 자동 할당. 명시(`--port 9090`) 시 그 포트로만 바인딩 (충돌 시 에러). 실제 URL은 시작 시 출력됨. | 자동 (`0xC0DE`=49374 → 빈 포트) |
| `--token` | 인증 토큰 | 자동 생성 (32 byte URL-safe) |
| `--no-browser` | 브라우저 자동 open 비활성 | `false` |
| `--idle-timeout` | N초 동안 **접속자 0 + 진행 중 작업 없음**이면 스스로 종료 (0 = 끄기). 온디맨드로 인스턴스를 띄우는 오케스트레이터용 — 다음 접속에 `--resume` 으로 재기동. **워커가 작업 중(LLM 턴·도구·질문 대기)이면 안 죽음**(긴 작업 끊김 방지). | `0` (끄기) |
| `--trust-local` | **loopback(127.0.0.1/::1) 요청은 토큰 인증 생략.** 앞단에 인증을 직접 하는 신뢰된 로컬 게이트웨이/프록시를 둘 때 — 게이트웨이가 토큰을 매 요청 주입할 필요 없음. **localhost 바인드(`--host 127.0.0.1`)일 때만 안전.** 비-loopback 요청과 끈 상태에선 토큰 그대로 요구. | `false` |
| `--base-path` | **리버스 프록시가 `/<prefix>/*` 를 이 인스턴스로 라우팅(+prefix strip)할 때의 URL 경로 prefix** (예 `/s/doom`). UI 의 모든 URL 은 상대경로이고 serve 되는 index.html 에 `<base href="<prefix>/">` 가 주입돼 prefix 하위로 resolve 됩니다. 기본 `''` = 루트(`<base href="/">`, 기존과 동일). | `''` (루트) |
| `--title` | **방 제목** (v10.71.0) — 헤더 첫 줄(제목만의 줄 — 칩·노브·아이콘은 그 아래 줄, v10.74.1)과 브라우저 탭(`<제목> · agent-cli`), 배너 `Title:` 줄, `web.json` 의 `title`. 세션 메타에 남아 `--resume` 이 지킨다. agent-board 는 글의 topic 을 spawn 마다 넘기므로 보드에서 고친 제목은 다음 재시작에 들어온다. 없으면 제목 줄 없이 탭은 `agent-cli` | (없음) |
| `--endpoint NAME` | 쓸 모델 서버 이름 (v10.144.0; env `AGENT_CLI_ENDPOINT`) — `run` 과 같은 순서로 고르고 세션에 기록한다. 배너 `Server:` 줄, 머리 줄의 서버 칩, `web.json` 의 `endpoint`. 방이 뜬 뒤에는 바뀌지 않는다(다른 서버는 보드에서 복제). agent-board 는 글에 기록된 서버를 spawn 마다 넘긴다 | 세션 기록 → config `endpoint` |

> 브라우저 자동 오픈은 **로컬 bind 일 때만** 일어납니다 — 기본 `127.0.0.1`(과 `0.0.0.0`/`localhost`/`::`/`::1`)처럼 *이 머신에서 localhost 로 접속 가능한* 경우. `--host <원격IP>` 처럼 특정 IP 에 바인드하면(원격 서버) 자동 오픈을 생략하고 접속 URL 만 출력합니다(서버에서 브라우저를 띄워봐야 소용없으므로). `--no-browser` 는 로컬에서도 끄고 싶을 때.

> **상태 사이드카(`status.json`)에는 v10.46.0부터 세션 노브의 현재 값이 `knobs: {이름: 값}` 으로 실립니다** — 노브가 바뀔 때마다(칩·`/api/knobs`) 다시 씁니다. 보드가 방의 값을 거울처럼 기록해 다음 재시작에 같은 플래그로 돌려주는 소스입니다(방에서 🤖 로 바꾼 모델이 재시작 뒤에도 남는다). 소유자 없는 노브는 빠집니다. v10.119.0 부터는 열린 항목이 있을 때 `open_items: {count, ids, oldest_turns, budget_left}` 도 실립니다(`/api/health` 도 같은 요약 — 보드 카드의 📋 칩 소스, 없으면 생략). v10.120.0 부터는 제자리 고리 띠가 떠 있는 동안 `stuck: {turns, since_turn, asking}` 도 실립니다 — `asking` 은 런이 멈춰 Continue/Stop 을 기다리는 중(`awaiting_input` 도 참). `/api/health` 도 같은 요약, 없으면 생략. v10.165.0 부터는 기다림을 따로 말합니다: main 이 제자리에서 기다리는 동안 `waiting: true` 가 실립니다 — 받아들여진 `wait` 의 대기 줄과 함께 서고 내립니다(`busy` 도 참입니다 — 기다림은 작업 구간 안입니다; 기다리지 않으면 생략). `agents` 에는 `waiting`(기다리는 에이전트 수)과 `list[].waiting` 이 더해지고, `agents.working` 은 기다리는 에이전트를 세지 않습니다(작업 상태 `state` 는 그대로 `busy`). 에이전트의 기다림은 우편의 기다림 표시에서 읽습니다. `/api/health` 의 `agents` 도 같습니다.
>
> **v10.121.0부터 돌고 있는 백그라운드 작업도 실립니다** — `jobs: {count, list: [{id, pid, owner, command, started_at}]}` (additive; 작업이 없으면 생략, `started_at` 은 epoch 초 — 파일은 쓰는 순간부터 낡으므로 경과는 읽는 쪽이 계산). `/api/health` 도 같은 요약. 보드 카드의 🧵 칩, "뷰어 0 인데 안 꺼지는 방" 의 이유입니다.
>
> **v7.10.0부터 상주 에이전트 요약도 실립니다** — `{"agents": {"alive", "working", "list": [...]}}` (additive; 에이전트 없으면 필드 생략). board 가 행에 🤖 칩과 "에이전트 작업 중" 상태를 그리는 소스이고 `/api/health` 도 동일 요약을 반환합니다. `--idle-timeout` 자가 종료도 에이전트가 작업 중이거나 미처리 요청이 있으면 발동하지 않습니다 (main 유휴여도 백그라운드 작업 보존).

> **헤더 (v8.57.0):** 토큰 상세(`ctx used/window (%) · ↓출력 · 💭사고`)가 헤더에 상시 표시됩니다. v10.136.0 부터 그 줄은 `ctx 41.0K/128.0K (32%) · ↓812 · 38 tok/s · Σ↑1.4M · Σ↓9.1K` 꼴입니다 — `tok/s` 는 이 턴의 출력(디코드) 속도, `Σ↑`·`Σ↓` 는 **이 런**(웹은 메시지 하나가 런 하나)의 호출마다 보낸 입력 합·받은 출력 합입니다(`Σ↑` 는 ctx 분자와 같은 정의라 캐시 적중분을 포함 — 회사 게이트웨이처럼 호출당 입력이 비용을 정하는 곳에서 그 합이 보입니다). 칩에 마우스를 올리면 툴팁에 ttft·프리필 속도·런의 캐시 적중 합·호출 수가 더 나옵니다. 값이 0 이거나 서버가 시간을 주지 않은 조각은 빠집니다 — 사고(💭) 카운터는 생성 중 실시간 증가해 러너웨이가 바로 보입니다. 대화 쪽 **생성 중 줄**(v10.70.0)은 세 채널 — 본문 · 🧠 사고 · 🔧 도구 호출 조각(`native_fc` 가 `tool_calls` 로 보내는 인자) — 의 **합**을 앞에, 0 이 아닌 🧠·🔧 내역을 뒤에 보입니다(터미널 마르퀴 `~N tokens (🧠 N · 🔧 N)` 도 같은 규칙). 숫자는 글자수/4 추정이고 실제 토큰은 턴 끝 usage 입니다. 세션 노브(🗜️ 압축 임계 · 👥 에이전트 상한 · ⏳ Stall · 🧠 사고/노력)는 각자 현재 값을 배지로 단 칩이고, 클릭하면 그 노브만의 팝업이 열립니다. 창이 좁아지면 헤더가 자동으로 줄바꿈되어 모든 칩·아이콘이 계속 보입니다.

> **오케스트레이션(인스턴스 파일):** `web` 은 시작 시 `.agent-cli/sessions/<id>/web.json`(`{session_id, host, port, token, pid}`)을 기록하고 종료 시 제거합니다. `--idle-timeout` 의 자가종료와 짝을 이뤄 — 외부 서비스가 인스턴스를 온디맨드로 띄우고, 프로세스를 추적·종료하지 않고도, 파일 하나로 "이 세션 web 떠 있나/어디로" 를 알아 접속(또는 `--resume` 재기동)할 수 있습니다.
| `--resume <id>` | 이전 세션 이어서 실행 (`agent-cli sessions`로 ID 확인) | — |
| 기타 (`-p`, `-m`, `-n`, `--max-depth` 등) | `run`과 동일 | |

UI 기능:
- 좌측 어시스턴트 카드 (markdown 렌더링: 헤더 `#`/`##`/`###`, GFM 파이프 표, 순서/비순서 리스트, **bold**/*italic*, 인라인 코드, 펜스 코드 블록) + 우측 사용자 bubble (같은 렌더러). 강조는 CommonMark flanking 규칙을 따릅니다(v9.22.3) — 공백을 사이에 둔 별표(`0 9 * * 1-5`, `2 * 3`)는 글자 그대로 남습니다. **그림 `![alt](src)` (v10.64.0):** 작업 폴더 경로(`![chart](out/chart.png)` — 원본은 클릭)와 래스터 `data:image/png|jpeg|gif|webp;base64,…` 만 그림으로 그립니다. 외부 `http(s)` 그림은 불러오지 않고 `🖼 alt` 링크로만 둡니다 — 그림 태그는 보는 순간 요청이 나가므로, 인젝션된 페이지가 모델에게 `![](https://…?d=<비밀>)` 을 쓰게 하면 클릭 없이 새어 나가기 때문입니다. SVG `data:`·그 밖의 스킴은 글 그대로입니다.
- 도구 호출(action) / 결과(observation) 인라인 카드, ✓/✗ 상태 표시 — 관찰 줄의 표시는 그 줄의 기록(`success`) 그대로이고, 정지·재시작의 `[interrupt]` 줄은 실패가 아니라 중립 `■` 입니다(v10.177.3)
- 이미지 첨부 (v10.60.0): 입력창에 붙여넣기·끌어놓기·📎 → 칩 → 보내기; 사용자 말풍선·`view_image`·`browser screenshot`·MCP 결과에 썸네일. 에이전트 채널도 받음(v10.61.0). 상세는 아래 "이미지 입력" 절.
- 보내기 실패 표시 (v10.147.3): 입력창의 글과 첨부 칩은 서버가 받았을 때만 비워집니다. 거절(죽은 에이전트, 사라진 첨부 파일 등)·네트워크 오류·20초 안에 응답이 없을 때(`no response from the server` — 그 요청은 끊습니다)면 첨부 줄에 `Not sent — <사유>` 가 서고 글·칩이 남습니다 — 종전엔 응답을 보지 않고 비워 거절된 말이 소리 없이 사라졌습니다. 보내는 중에 다시 보내면 `Still sending the previous message` 로 알리고 받지 않습니다.
- ❓ 트레이의 답 (v10.147.4, v10.177.0): 질문의 답·닫기와 확인의 버튼이 같은 규칙입니다 — 서버가 받았을 때만 비우고, 실패하면 그 항목 맨 아래에 `⚠ Not sent — <사유>` 가 서고 글이 남습니다. 확인 항목은 버튼으로만 답합니다(말 칸의 Enter 는 보내지 않고, 기본 버튼이 없습니다). 다른 뷰어가 먼저 답했거나 닫힌 물음(409)이면 글을 버리고 입력창 아래 줄에 `Not sent — already answered or closed` 가 섭니다 — 보내는 사이 항목이 트레이에서 빠진 뒤 실패해도 같은 알림입니다. 트레이는 열린 질문 목록·명단이 올 때마다 항목 단위로 맞춰져, 쓰던 답·포커스·커서가 지워지지 않습니다(종전엔 통째로 다시 그려 지워졌습니다). SSE 가 다시 붙어 같은 물음이 재생돼도 그대로입니다 — 서버가 물음마다 새 `ask_id` 를 `input_required`·`input_resolved` 에 싣습니다(화면 전용, 모델·기록에는 없음). 끊긴 사이 물음이 풀렸으면(재접속 스냅샷에 물음이 없으면) 남은 main 물음 항목을 접습니다(v10.149.2 — 종전엔 낡은 항목이 남았습니다). 보내는 중인 항목은 그 항목만 막습니다(`Still sending the previous answer`). 에이전트를 ✕ 로 지우면 그가 건 질문이 트레이에서 바로 빠지고, 멈춘·사라진 에이전트의 질문에 간 답은 거절됩니다(409 — 같은 닫힘 알림; v10.149.1, 종전엔 200 으로 받은 척하고 답이 어디에도 가지 않았습니다).
- 글 없는 버튼의 실패 (v10.155.0): 누른 것이 안 됐으면 입력창 아래 줄에 `Not <동사> — <사유>` 가 서고 그 버튼은 다시 누를 수 있습니다. 이 버튼들은 상한 없이 기다립니다 — 막힌 연결에서도 늦게라도 닿으면 적용됩니다.
  - 대기열 ✕: "Cancelled" 는 서버가 뺐다고 답한 뒤에만 섭니다. 그 사이 이미 보내졌으면 `Not cancelled — it was already sent` 와 함께 그 말은 "✓ My request applied" 로 갑니다(종전엔 누르는 즉시 "Cancelled" 로 확정해, 처리된 말도 취소된 것처럼 보였습니다).
  - 정지·지우기(입력창 옆 Stop·▾·stuck 띠의 Stop·채널 머리 ■/✕·■ all): `Not stopped — <사유>`·`Not removed — <사유>`, 버튼이 풀려 다시 누를 수 있습니다(종전엔 "Stopping…" 이 그 런이 끝날 때까지 잠겼습니다). 돌던 턴이 없었던 정지는 실패가 아닙니다.
  - 채널 칩의 ↻(되살리기): 거절(에이전트 한도·이미 살아 있음)이나 네트워크 오류면 `Not revived — <사유>`(예: `agent limit reached (1 alive)…`). 종전엔 아무 표시 없이 끝났습니다.
  - 노브: 서버가 값을 거부하면(미등록 모델 등) 마지막 상태로 되돌리면서 그 칩의 설명 줄에 `Not changed — <사유>` 가 섭니다(설명 줄이 없는 머리 토글은 입력창 아래 줄). 종전엔 말없이 되돌아갔습니다.
- 각 카드 모서리에 시각 표시 (`YYMMDD HH:MM:SS`, 마우스 hover 시 전체 날짜+밀리초). agent/skill 내부 카드도 동일. `--resume` 로 이어서 보면 카드들은 **실제 발생 시각**(history 기록 기준)으로 표시되어 재접속 시점과 혼동되지 않음
- 실시간 스트리밍 (점선 카드로 토큰 누적 → 최종 카드로 교체)
- 서브에이전트(delegate) 작업은 접히는 그룹 카드로 격리 — 헤더 아무 곳이나 클릭해 접기/펼치기, **긴 카드를 펼쳐 스크롤해도 헤더가 상단에 고정(sticky)돼 어느 위치에서든 접을 수 있고**, 본문 여백 클릭으로도 접힘 (중첩 카드·텍스트 선택은 방해 안 함)
- 컨텍스트 동기화: 오래된 turn이 LLM 컨텍스트에서 밀려나거나 compaction으로 요약되면 UI에서도 제거
- 여러 탭/PC가 접속하면 모두 동등하게 입력 가능 (접속자는 닉네임 로스터에 표시). 같은 확인(`c-N`)/질문(`q-N`)에 두 명이 동시에 답하거나 닫으면 먼저 도착한 답만 수용 — 늦은 답은 거절(409)되고(번호의 길이라 다른 확인을 오염시킬 수 없다), 목록이 그 항목을 거둡니다
- **연결 고갈 방어는 board 열기 게이트가 담당** (운용 전제: 방은 항상 board 를 통해 열기) — 각 탭은 board 카운트용 BroadcastChannel 비콘에만 응답. 페이지 자체 입장 게이트(v7.5~7.6 Web Locks 파킹)는 제거됨: Web Locks 는 secure context 전용이라 LAN http 배포에서 동작하지 않고, URL 직접 진입 케이스는 운용 정책상 범위 밖. **주의**: 브라우저 세션 복원으로 방 탭 5개 이상이 한꺼번에 재진입하면 origin 당 6연결 한도 포화가 재현될 수 있음 — 그때는 탭을 닫으면 됨
- 재접속/새로고침 시 트랜스크립트 재생은 **최근 5,000개 이벤트 윈도우**로 제한 — 아주 긴 세션은 맨 위에 "이전 N개 생략" 노티스가 표시됩니다 (전체 기록은 세션 히스토리에 보존, `--resume` 은 무관). 라이브로 보고 있던 탭은 영향 없음
- ❓ 트레이: 열린 확인(맨 위)과 질문(main·에이전트·스킬)이 입력창 위에 서고 그 자리에서 답합니다 — 질문은 `Send`·`Close without answer`(v10.176.0), 확인은 버튼(v10.177.0). 확인을 기다리는 동안에도 입력창 옆 Stop 이 보입니다
- **부재 중 질문 대기 (v7.8.0)**: 위험 shell 승인/워크스페이스 밖 접근 승인은 **지금 보는 사람이 없어도 대기**합니다 — 즉시 "(no response)"로 포기하지 않고, 세션 상태가 `awaiting_input` 이 되어 board 목록에 "답변 필요"로 표시되며, 나중에 접속하면 대기 중이던 질문이 그대로 떠서(재접속 replay) 답할 수 있습니다. 다중 방 운용에서 다른 방을 보는 사이 도착한 질문을 놓치지 않음. CLI(터미널 없음=답 불가)는 즉시 폴백
- **칩 헤더 (v7.1.0)**: 상단 바가 의미 칩 3개로 수렴 — 모델 칩(max 28ch, hover=provider·전체 이름) · **ctx 게이지 칩**(사용률 % + 미니 게이지; 클릭하면 팝오버에 토큰 상세 `ctx 5.2K/256K (2%) · ↑5.2K ↓320 · Σ↓1.8K` + 컴팩션 임계 슬라이더 + Agents 상한이 열림) · **워크스페이스 칩**(`📋 …/마지막/2세그먼트`, hover=전체 경로, **클릭=경로 복사** — LAN http 에서도 동작, 복사 후 1초 ✓). 어떤 창 폭·모델명·경로에서도 헤더 한 줄 보장.
- **토큰 현황**: ctx 칩과 팝오버가 매 turn 갱신, 새로고침 후에도 유지(SSE snapshot). CLI(`run`)도 동일 정보를 매 turn 한 줄로 표시(`in: … | out: … | ctx: … | Σin: … | Σout: …`). `usage.input_tokens`(서버 실측)를 받아 `renderer.token_usage` 로 추상화 → CLI/web 공통
- **압축 임계 슬라이더 (5.14.0)**: 토큰 현황 옆 `압축 80%` 슬라이더로 컨텍스트 **compaction 목표 비율**을 **세션 한정**으로 조절합니다(50~95%, 5% 스텝). 가용 컨텍스트(context_window − system − output)가 이 %에 닿으면 압축이 걸립니다 — 낮출수록 일찍/자주 압축(컨텍스트 적게 유지, 오버플로 여유↑), 높일수록 늦게 압축(컨텍스트 많이 유지). web 과 loop 이 같은 `ContextManager` 를 공유하므로 슬라이더를 놓는 즉시 **다음 LLM 콜에 반영**되고, 여러 뷰어의 슬라이더는 sticky 브로드캐스트로 동기화됩니다. main 대화 전용이며, 서브에이전트는 **spawn/resume 시점의 값을 상속**합니다(이미 도는 서브에 새 값을 적용하려면 재spawn). `GET/POST /api/compaction`, 토큰 인증. (재시작하면 기본 80% 로 복귀 — 영속 아님.)
- **정지 버튼은 입력창 옆 하나 (v10.142.0)**: main 만 일하면 아래의 **Stop**(종전과 같음 — main 만). main 과 에이전트가 함께 일하면 **Stop ▾** — 본체는 main 만, ▾ 메뉴가 범위를 고릅니다: `Stop main`(main 의 런만, 에이전트는 계속) · `Stop all agents`(모든 에이전트의 런 — 각자 빚진 상대에게 회신, main 은 계속 받아 정리) · `Stop everything`(main 까지 — 이때 생긴 정지 회신은 main 도, 살아 남은 상주 에이전트도 다시 깨우지 않고 쌓였다가 다음에 일을 받는 런에 함께 배달됩니다; Stop all agents·채널 ■/✕ 의 회신은 main 을 깨웁니다). main 이 한가하고 에이전트만 일하면 입력창 옆은 **Send** 그대로이고 채널 줄 끝에 작은 **■ all**(= Stop all agents)이 섭니다 — 일하는 에이전트가 있을 때만. 에이전트 하나만 멈추려면 그 채널 머리의 **■ Stop run**(아래 `agent` 절). API: `POST /api/stop` 의 본문 `{"scope": "main"|"agents"|"all"}`(없으면 `main`), 응답 `{stopped, agents}`.
- **Send → Stop → Stopping… 토글**: 사용자 메시지 전송 후 worker 가 응답을 처리하는 동안 Send 버튼이 빨간 **Stop** 버튼으로 바뀝니다. 클릭하면 즉시 **Stopping…**(비활성)으로 바뀌어 중복 클릭을 막고, 진행 중인 turn 을 안전하게 중단 (CLI 의 Ctrl+C 와 같은 `stop_event` 경로 → `POST /api/stop`). LLM 생성 도중이면 스트림을 즉시 끊고 미완성 응답을 폐기하며, 도구 실행 중이면 그 스텝을 마친 뒤 멈춥니다 — 다만 전경 `shell` 명령은 끝나기를 기다리지 않고 정지가 서는 대로 프로세스 그룹째(명령이 띄운 것까지, 시간 초과와 같은 TERM → KILL) 끝냅니다(v10.146.2 — 종전엔 최대 120초를 끝까지 돌았습니다). 그 결과는 `This run was stopped, so the command was ended after 12s, together with everything it started.` 와 그때까지의 출력 끝줄들이고, 다음 입력 런의 모델이 history 에서 읽습니다. main·상주 에이전트(■)·run 에이전트·✕·세션 종료 모두 같은 길이고, `background` 작업은 정지와 무관하게 계속 돕니다. **멈춘 뒤엔 도구를 실행하지 않습니다 (v10.140.0)** — 응답을 다 받은 뒤에야 정지가 보이는 경우(스트리밍이 아닌 서버, 응답 직후)에도 그 응답의 도구는 0회이고 바로 중단 기록으로 끝납니다(종전엔 한 번 더 실행됐습니다; main·상주 에이전트·run 공통, 이미 쓴 `complete` 답은 그대로 받습니다). turn 이 끝나면 다시 Send 로 복귀. 중단은 `[interrupt]` observation 으로 기록되어 다음 입력에서 이어집니다. 두 번째 메시지가 in-flight turn 에 끼어드는 것도 자연히 차단 (Enter 는 stop 을 트리거하지 않음 — 버튼 전용). **새로고침 / 재접속 후에도 상태 유지** (서버가 last worker state 를 SSE snapshot 에 prepend). prompt 모드(ask 답변)에선 항상 Send, confirm 모드는 별도 버튼. 일반 chat·`/skill` 실행이 Stop 으로 중단됩니다. `@<profile>`(agent run) 명령은 기다림만 멈추고 run 에이전트는 계속 일합니다(v10.142.0 — 회신은 main 메일로; 그 에이전트를 멈추려면 채널 ■).
- 빌드 단계 없는 브라우저 네이티브 ES 모듈 프런트 (v10.116.0) — ES 모듈을 지원하는 브라우저(Chrome 61·Firefox 60·Safari 11 이후)가 필요합니다.

**🎨 테마 피커:** 헤더의 🎨 버튼으로 드롭다운을 열어 **5가지 큐레이션 테마**(각 항목에 색 스와치 + 현재 ✓)를 고릅니다 — **Amber**(기본, 따뜻한 차콜+호박색), **Slate**(중성 다크), **Midnight**(딥 블루), **Terminal**(틸/near-black), **Light**. 선택은 즉시 적용 + 브라우저에 기억되고(localStorage), 첫 페인트 전에 적용되어 깜빡임이 없습니다. 다크 테마들은 **반투명 헤어라인 테두리**로 부드러운 경계를 씁니다. 모든 색은 CSS 디자인 토큰(`:root` 베이스 + `[data-theme="…"]` 오버라이드) 한 곳에서 파생되어, 테마 추가 = 토큰 블록 하나입니다. **버튼 체계 (5.5.0)**: 변형 4종 — `btn-primary`(화면당 주행동 하나: Send/저장/다운로드) / `btn-ghost`(hairline 기본값: 취소·보조 동작) / `btn-danger`(Stop·삭제 — 평시엔 조용, hover 에서만 채움) / `btn-icon`(헤더 도구·✕ 닫기) — 이 전부이며, 전역 베이스가 모든 버튼에 투명 배경+테마 글자색을 강제해 스타일 누락 버튼이 브라우저 기본 배경으로 '유령'이 되는 사고를 차단합니다. amber/terminal 은 밝은 accent 위 대비를 위해 어두운 잉크 글자(`--on-accent`)를 씁니다. **사용자 말풍선 안의 코드도 테마를 따릅니다 (v9.24.10)**: 인라인 코드와 코드 블록 배경이 말풍선 색에서 글자와 반대 방향으로 조금 민 색이 되어, 모든 테마에서 글자 대비가 WCAG AA(4.5) 이상입니다.

**모델 시점 (v10.17.0):** 챗 화면은 위에서 아래로 **모델이 받는 순서**입니다. main · 상주 에이전트 채널 · 대화 속 agent/skill 카드가 같은 세 부분을 갖습니다.

| 위치 | 이름 | 내용 |
|---|---|---|
| 맨 위 (접힘, 고정) | Head | 시스템 프롬프트 섹션(훅 섹션 포함) · 함수 스키마(native_fc) · 디코딩 문법 · 압축 요약 |

> **모델이 보는 글의 표기 (v10.77.0):** 도구 이름·인자 이름은 백틱(`` `read_file` ``, `` `line_start` ``), 값은 큰따옴표(`"replace"`), 호출 모양(`mode=get`, `read_file(path=…)`)은 쓰지 않고 문장으로 씁니다 — 도구 설명·안내·꼬리·넛지·오류 문구 모두. 형식 오류의 되묻기도 `` You sent: `edit_file` with `op` "replace", … — missing: `path`. `` 처럼 문장입니다. 설계 `docs/prompt-notation/DESIGN.md`.

> **프롬프트의 호출 예시 (v10.75.1):** 도구 가이드·Skills·Agent Profiles 의 예시는 방언이 그린다 — 텍스트 방언(json_fc·xml_fc 등)은 자기 호출 모양 그대로(예시가 곧 형식 교육), native_fc 는 `` `read_file` {"path": "app.py"} `` 처럼 도구 이름과 인자 객체만. 훅 섹션의 `<!-- HOOK_SECTIONS -->` 표식도 이제 모델에게 나가지 않는다.

| 그 아래 (접힘) | Evicted | 압축으로 컨텍스트에서 빠진 카드를 한 묶음으로. 펼치면 흐린 카드 |
| 가운데 | 대화 | 지금 모델이 보는 카드 |
| 맨 아래 (접힘, 고정) | Tail | 매턴 꼬리(턴·요청·회신 빚·에이전트 로스터·메모리 인덱스·작업 지침)와 직전 턴과의 diff |

채널의 "Head" 는 머리말 아래 "Context" 줄 바로 밑에, "Tail" 은 입력창 바로 위에 고정되어 대화만 스크롤되고(v10.136.3), 채널을 바꾸면 둘 다 그 채널의 것으로 바뀝니다. 펼친 본문은 높이 상한 안에서 스크롤해 대화를 가리지 않습니다. "Context" 줄은 지금 채널의 모델이 보는 양(시스템·함수 스키마·대화·꼬리)입니다. 헤더 ⧉(대화 복사)는 지금처럼 Head 의 압축 요약을 맨 앞에 싣습니다. Head·Tail 막대에 마우스를 올리면 무엇이 들어 있는지 한 줄 풀이가 뜹니다. agent/skill 카드는 펼쳤을 때 머리 아래와 몸통 아래에 같은 두 줄이 보이고, 끝난 실행은 종료 시점 상태로 고정됩니다. 내용은 그 스코프의 **마지막 모델 호출** 기준이며(`GET /api/debug/prompt?task_id=`), 보이는 것만 가져오고 **모델 호출이 나가는 순간** 갱신합니다(v10.63.0 — 생성 중에도 Tail 은 지금 호출에 실린 꼬리; 사용량 줄은 그 호출이 끝난 뒤 채워집니다). 런이 끝나면 마지막 호출 뒤에 닫힌 Open Items(예: `complete` 로 답한 `[u-1]`) 줄 끝에 `✓ closed` 태그가 붙고, 막대에 `✓ N closed after` 가 섭니다(v10.119.1 — 글은 긋지 않습니다) — 꼬리 글 자체는 모델이 받은 그대로입니다. Tail 안의 섹션(Session State·Standing Rules)은 접혀 있고 머리 줄 태그(`+N`·`−M`·`vs turn K`·`no change`·`✓ closed`)로 직전 호출 대비 바뀐 양을 보이며, 펼친 섹션은 다음 호출에도 펼친 채 남습니다. 🔍 버튼과 드로어는 없습니다 — 드로어가 보여 주던 것이 모두 이 틀 안에 있습니다. `--resume` 하면 세션 폴더의 `prompt_views/` 에서 틀을 되살려 재개 전과 같은 화면이 됩니다. 머리말 아래 사용량 줄은 **서버가 센 값만** 씁니다(v10.18.0): 그 스코프의 마지막 모델 호출에서 서버가 보고한 입력 토큰을 시스템·함수 스키마·꼬리·대화로 나눠 보이고, 아직 호출이 없으면 `N/A` 로 두었다가 다음 호출 뒤에 채웁니다. 마지막 실측은 `prompt_views/` 에 함께 저장되어 재개 직후에도 재개 전과 같은 숫자입니다. 아래 v10.7.0 · 8.11.0 설명의 드로어·🔍 버튼은 더 이상 없습니다.

프롬프트 내용은 위의 틀(Head·Evicted·Tail)이 챗 안에서 보여 줍니다(따로 인스펙션 드로어는 없습니다). native_fc 의 요청 `tools[]` 함수 스키마도 Head 에 있습니다.

**📝 Directive Editor (헤더 📝):** 헤더의 📝 버튼은 **에이전트 지시문 편집 전용** 드로어입니다(프롬프트 뷰어와 분리 — 8.11.0). 프로젝트 `.agent-cli/DIRECTIVE.md` 를 편집합니다 — **에디터 구조 = 파일 구조**: `🌐 Common`(main+모든 서브에이전트) / `🎯 Main`(`## @main`) / `🤖 Sub-agents`(`## @agents`) 세 탭이 파일의 스코프 블록과 1:1 로 대응하고(분해·조립은 서버의 U-C 파서 단일 출처), 내용 있는 탭엔 ● 뱃지가 붙습니다. 각 탭은 **직접 편집**하거나, 아래 입력줄에 넣고 싶은 내용을 대략적으로 적고 **✨ 생성**을 누르면 LLM 이 그 청중용 directive 초안을 써서 탭에 반영합니다(기존 내용이 있으면 병합/개정) — 초안은 **미저장** 상태로 들어와 검토 후 저장. 생성은 **산문 직접 호출**(provider 1콜 + 구조적 새니타이저)입니다 — Qwen3.6 세대에서 CoT 누출 무발생을 재실측(16/16)한 뒤 단순화(5.7.0). 메인 대화/워커와 무접촉이라 타임라인에 흔적이 없고, **탭별로 동시에 여러 생성**을 걸 수 있습니다. 저장하면 세 탭이 하나의 파일로 조립되어 기록되고 **다음 LLM 호출부터 적용**(KV 캐시 prefix 리셋), 여러 인스턴스 간 브로드캐스트 동기화. API: `GET/POST /api/directives`(scopes 계약) · `POST /api/directives/generate`, 토큰 인증, **프로젝트 파일 전용**.

**⧉ 복사 (v10.8.0):** 카드마다 호버 시 ⧉ 가 뜹니다. 누르면 그 카드가 **마크다운**으로 클립보드에 들어갑니다 — 사용자 말풍선은 `**👤 닉네임**` + 원문, 최종 답변은 모델이 쓴 마크다운 소스 그대로, 스텝 카드는 `💭 생각` → `⚡ \`도구\`` + 입력 JSON 펜스 → `✓/✗ \`도구\``(`[interrupt]` 는 `■`) + 관찰 원문 펜스, 인라인 에이전트·스킬 카드는 제목 줄 아래 안의 카드들을 재귀로. 헤더의 ⧉ 는 **보이는 대화 전체**(현재 채널)를 같은 포맷으로 이어 붙입니다. LAN http 에서도 동작합니다(비보안 컨텍스트 폴백).

**📁 Workspace files (다운로드 + 업로드 + 삭제):** 헤더의 📁 버튼으로 우측 드로어를 열면 **워크스페이스 파일 트리**가 나타납니다(디렉토리는 ▶로 펼쳐 하위 탐색). 워크스페이스 밖을 가리키거나 깨진 심링크는 🔗 로 보이고 펼치기·선택이 안 됩니다(마우스를 올리면 대상 경로; v10.113.1). 안을 가리키는 링크는 대상 종류로 보입니다. **파일·디렉토리 옆에 크기 표시 — 디렉토리는 하위 전체를 합산한 재귀 크기**이고, 맨 위 루트 행(`📁 /`)에는 **워크스페이스 총 크기**가 표시됩니다. 한 드로어에서 다운로드·업로드·삭제를 모두 합니다.

- **다운로드**: 원하는 파일/디렉토리를 체크하거나 **트리 맨 위 루트 행(`📁 /`)의 체크박스로 워크스페이스 전체**를 선택한 뒤 **⬇ Download zip** 을 누르면 선택 항목이 임시 zip으로 압축되어 다운로드됩니다(전송 후 서버의 임시 파일은 삭제). 디렉토리를 고르면 그 하위 전체가, 파일을 고르면 그 파일만 담깁니다. 트리에서 폴더를 클릭하면 그 폴더가 업로드 대상이 됩니다(루트 행 클릭 = 루트).
- **업로드**: 드로어 안으로 **파일이나 폴더를 드래그-드롭**하거나 `파일`/`폴더` 버튼으로 선택하면 업로드됩니다(파일당 한 요청). **폴더를 드롭/선택하면 하위 구조를 그대로 보존**해 재귀로 업로드합니다(중간 디렉토리는 서버가 자동 생성). 업로드 위치는 **트리에서 클릭한 폴더**(클릭하면 하이라이트되고 "⬆ 업로드 대상" 에 표시), 클릭하지 않으면 워크스페이스 루트입니다. 결과(✓ 경로, 덮어쓴 경우 표시) 후 트리가 자동 갱신됩니다.
- **삭제 (🗑)**: 체크한 파일/디렉토리를 **🗑 Delete** 로 삭제합니다. **영구 삭제라 먼저 확인(confirm) 창**이 뜨고, 디렉토리는 하위까지 재귀 삭제됩니다. 삭제 후 트리 자동 갱신(개수·실패 보고).

보안 가드: 워크스페이스(서버 실행 디렉토리) 밖 경로는 서버가 차단하고, 업로드는 경로의 모든 세그먼트에서 `..`/절대경로 금지(traversal 차단)·resolve 후 워크스페이스 하위 재검증·대상 경로 존재 확인·토큰 인증을 적용합니다. 파일 크기 상한은 없습니다(v9.22.1 — 본문을 스트리밍으로 임시 파일에 쓰고 완료 시 제자리로 옮기므로 서버 메모리에 올리지 않고, 중단된 업로드는 워크스페이스에 남지 않습니다). **삭제는 가장 파괴적이라 가드가 가장 강합니다 — 워크스페이스 하위만, traversal 차단, 워크스페이스 루트 자체는 삭제 거부, 토큰 인증.** (드래그로 파일을 브라우저 *밖으로* 끌어내는 다운로드는 브라우저 제약상 미지원 — zip 버튼이 전 브라우저·디렉토리 경로입니다.)

**Δ Changes 패널 (v10.137.0):** 입구는 셋입니다. 기준점 이후 디스크에서 파일이 바뀌면 헤더 📁(Workspace files) 아이콘 위의 **숫자 배지**와, main 대화의 **가장 최근 `✔ complete` 카드** 안의 한 줄 `Δ N files changed · View changes` 가 생깁니다(다음 런이 끝나면 새 카드로 옮겨 가고, 한 화면에 하나뿐입니다 — 목록이 누적이라 옛 카드의 숫자는 의미가 없습니다; 상주 에이전트 채널의 카드에는 붙지 않고, 대화 복사에도 들어가지 않습니다). 바뀐 게 없으면 이 둘은 없습니다. 셋째는 **📁 Workspace files 서랍 머리 아래 늘 서 있는 한 줄**입니다(v10.144.2): 바뀐 파일이 있으면 강조색 `Δ 3 files changed · since session start · 10:02   View changes ›`, 없으면 흐린 `Δ No changes · since …   Open ›`(빈 패널이 열립니다), 스냅샷을 못 쓰는 방이면 누를 수 없는 점선 줄 `Δ Change tracking is off · <까닭>` — 까닭은 `turned off (AGENT_CLI_SNAPSHOT=0)`·`git not found`·`git is not working`·`could not create the snapshot repository`·`could not read the workspace` 중 하나이고, 마우스를 올리면 git 이 낸 원문까지 보입니다. 배지·카드의 줄·서랍의 줄을 누르면 오른쪽 패널에 **기준점 이후 디스크에서 바뀐 파일** 목록이 뜹니다(📁 아이콘 자체를 누르면 지금처럼 Workspace files) — 상태(`A` 추가·`M` 수정·`D` 삭제; 이름 바뀜은 `D`+`A`)·경로·`+N −M`(바이너리는 `binary`). 파일을 누르면 기준점 대비 unified diff 가 아래에 보이고(1,000줄에서 자르고 `… truncated, N more lines` 한 줄), 같은 행을 다시 누르면 diff 가 접힙니다(행 앞 `▸` 닫힘·`▾` 열림). 📁 Workspace files 트리에도 같은 목록이 표시됩니다 — 바뀐 파일은 이름 오른쪽에 `M`·`A`, 안에 바뀐 파일이 있는 폴더(루트 포함)는 점 `●`(삭제된 파일은 트리에 없으므로 상위 폴더 점으로만 드러납니다). 링크는 링크 자체의 변경(대상 문자열)으로 🔗 행에 글자가 붙고, 하위 git 저장소(워크스페이스 안에 자기 `.git` 을 가진 폴더)는 **그 저장소의 git 으로** 셉니다 — 기준점 이후 디스크에서 바뀐 것이면 커밋했든 안 했든 `git-proj/src/a.py` 처럼 파일별로 오르고(새 파일은 그 저장소의 `.gitignore` 를 따릅니다), 기준점 때 이미 고치던 파일은 그때 내용과 비교합니다. 목록은 `Workspace` 와 `git-proj/` 그룹으로 나뉘고, 그룹 머리에 `HEAD a41c9e2 → 7be03d1 (+2 commits)` 가 붙습니다. 저장소 폴더가 통째로 지워지면 `D git-proj/`, 기준점 뒤에 생긴 저장소는 `A lib-x/` 한 줄로 요약하고(누르면 몇 개 파일·어느 커밋이었는지), 그 뒤 고친 것은 평소처럼 나옵니다. 기록한 커밋이 사라졌거나(rebase·reset 뒤 gc) 다른 저장소로 바뀌었거나(다시 clone) `.git` 만 지워졌으면 **그 저장소만** 발견한 순간을 기준으로 다시 잡고 목록 맨 위에 ⚠ 안내 줄을 띄웁니다(`✕` 로 닫거나 Clear 하면 사라지고, 재접속에도 남습니다). 커밋이 하나도 없는 하위 저장소(`git init` 만 한 폴더)는 첫 커밋 전까지 목록에 오르지 않고 "… is a git repository with no commits yet …" 안내 줄이 뜹니다(첫 커밋 뒤에는 새 저장소처럼 `A P/`). 스냅샷이 일반 파일로 담던 폴더가 커밋 있는 저장소가 되면 그 폴더는 그 저장소 몫으로 넘어가되, 목록은 기준점 이후 실제로 바뀐 파일로 이어집니다. 스냅샷(체크포인트)이 실패하고 있으면 빨간 안내 줄 "Snapshots have been failing since HH:MM (이유) …" 과 마지막 스냅샷까지의 목록이 보입니다(성공하면 사라집니다). 셀 때마다 정하는 이 두 안내 줄에는 `✕` 가 없습니다. 하위 저장소의 `.git` 은 읽기만 합니다. 📁 트리의 하위 저장소 폴더에는 작은 `git` 배지가 붙습니다. 이 동작은 이 버전 이후 시작한 세션·Clear 한 기준점부터이고, 그 전 기준점에서는 하위 저장소가 커밋이 바뀔 때 그 폴더 한 줄로 오릅니다(점 `●`, 누르면 `Subproject commit …` 두 줄). 파일의 글자를 누르면 그 파일 diff 를 연 채로 Changes 패널이 열리고(체크·펼침·업로드 대상은 그대로), 폴더 점은 패널을 엽니다. 트리 표시는 목록에서만 나오므로 Clear 로 사라지고 Undo 로 돌아옵니다. 기준점은 처음엔 **이 세션의 시작**(머리 줄 `since session start · 20:05`)이고, **`Clear list`** 를 누르면 그 순간의 디스크가 새 기준점이 됩니다(`since 21:40 · cleared`). Clear 는 **목록만 비우고 파일은 건드리지 않습니다** — 확인 창 없이 바로 비우고, 몇 초간 뜨는 `Undo` 로 이전 기준점을 되살립니다(한 단계). 토스트 `List cleared (N files).` 의 N 은 화면 목록이 아니라 Clear 하는 순간 서버가 센 수입니다 — 패널을 연 뒤 밖에서 바뀐 파일도 듭니다. 기준점은 워크스페이스의 스냅샷 저장소(`.agent-cli/snapshot.git`)에 ref 로 남으므로 방을 다시 띄워도, `--resume` 해도 같은 목록입니다. 목록은 **디스크 기준**이라 모델·상주 에이전트뿐 아니라 사람이 IDE 에서 고친 파일도 오릅니다(배지 툴팁도 "N files changed on disk since …"). 다시 세는 때는 페이지 로드·패널 열기·📁 서랍 열기·`↻`·런 끝뿐이고, 도구 호출마다 세지 않습니다. 스냅샷이 꺼진 방(`AGENT_CLI_SNAPSHOT=0`·git 없음)에는 배지·카드의 줄이 없고 서랍의 줄이 까닭을 말합니다. Clear 는 스냅샷 저장소의 HEAD 를 옮기지 않으므로 모델의 `snapshot` 도구(`list`·`restore`)에는 아무 영향이 없습니다. 좁은 화면에서도 패널 머리의 닫기 `✕` 는 제목 줄 오른쪽 끝에 남습니다. API: `GET /api/changes`(꺼진 방은 `{enabled: false, cause, reason, detail}` · 하위 저장소 항목은 `repo`·요약 줄은 `summary`, 최상위 `repos`·`notices` — 닫을 수 없는 안내는 `dismissable: false`) · `GET /api/changes/diff?path=` · `POST /api/changes/clear`(새 목록 + `cleared` — 비운 수) · `POST /api/changes/undo` · `POST /api/changes/notices/dismiss`(본문 `{repo}`)(토큰 인증).

**종료 (Ctrl+C):** 한 번의 Ctrl+C로 깨끗하게 종료됩니다. uvicorn의 lifespan shutdown 훅이 활성 SSE 연결을 정리하고, 백그라운드 worker는 `SHUTDOWN` sentinel로 깨어나 빠져나가며, 세션이 자동 저장됩니다. `agent-cli web --resume <session_id>`로 이어서 실행하면 이전 turn들이 SSE snapshot으로 재생되어 UI에 그대로 복원됩니다.

CLI parity 명령어 (웹 `/help` 와 `run /help` 는 같은 명령 표 하나에서 나옵니다 — `agent_cli/command_table.py`, v10.127.1):
- `/help` — 명령어 안내
- `/sh <command>` — LLM 우회 셸 실행
- `/skills` — 사용 가능한 스킬 목록
- `/<skill> <args>` — 스킬 직접 실행 (예: `/plan add a login rate limit`)
  - **경로 오인 방지 (v4.51.1)**: `/Users/me/proj 분석해줘` 처럼 **경로로 시작하는 메시지는 명령이 아니라 일반 요청**으로 LLM 에 전달됩니다 — 첫 토큰에 두 번째 `/` 가 있거나, 실존 경로(`/etc`, `/tmp` 등)거나, 명령 문법(영숫자·`-`·`_`)이 아니면 스킬 디스패치를 건너뜁니다. 오타(`/no-such-skill`)는 여전히 not-found 안내.
- `/questions` — 사람에게 물은 열린 질문 목록(`q-3  aa (agt-…)  질문 글`, main 의 것은 `q-2  main  질문 글`, 스킬 런은 `q-4  skill demo  질문 글`, 없으면 `no open questions`) (v10.174.0)
- `/answer <q-N> <text>` — 그 번호의 질문에 답합니다 — 웹 트레이와 같은 길입니다. 번호가 없으면 사용법(`usage: /answer <q-N> <text>`)만 말하고 아무 질문도 닫지 않습니다(질문이 하나뿐이어도 짐작하지 않습니다). 결과는 `answered q-3` 또는 `/answer: <사유>` (v10.174.0)
- `/close <q-N>` — 그 번호의 질문을 답 없이 닫습니다 — 웹 트레이의 `Close without answer` 와 같은 길입니다. 묻는 쪽은 닫혔다는 사실을 메일로 받고 가진 것으로 계속합니다. 번호가 없으면 `usage: /close <q-N>` 만. 결과는 `closed q-3` 또는 `/close: unknown or already-closed question "q-9"` (v10.176.0)
- `@agents` — 사용 가능한 에이전트 목록
- `@<agent> <task>` — 에이전트에게 작업 위임
- `@<profile>-spawn <task>` — 상주 에이전트를 띄우고 첫 과제를 건넴 (도움말에는 v10.165.0 부터)
- `@agt-<key> <message>` — 떠 있는 에이전트에게 직접 보냄 (도움말에는 v10.165.0 부터)

curl로도 직접 사용 가능:
```bash
curl -N "http://localhost:49374/api/stream?token=<TOKEN>"
curl -X POST "http://localhost:49374/api/input?token=<TOKEN>" \
  -H 'Content-Type: application/json' \
  -d '{"kind":"chat","content":"hello"}'
```

### `setup` — 설정 마법사

```bash
agent-cli setup
```

모델 서버 **하나**를 대화형으로 만들거나 고칩니다 — 이름·프로바이더·접속 정보·기본 모델 (v10.144.0). 저장된 서버가 없을 때 자동으로 실행되며, 언제든 수동으로 다시 실행할 수 있습니다(서버를 더 추가하거나 고칠 때). 저장된 서버 표(키는 `***`)를 먼저 보여 주고, 저장된 이름을 대면 그 서버를 고칩니다 — 빈 API 키 입력은 저장된 키를 유지합니다. 첫 서버는 기본 서버가 되고, 그 뒤 서버는 기본으로 할지 묻습니다. 고치는 서버의 `extra_headers` 는 묻지 않고 그대로 보존하며 모델 목록 조회에도 싣습니다. 자세한 편집(헤더·모델 표)은 보드 admin 이나 파일 편집으로 합니다.

옛 모양(v10.143.0 까지) 파일이 있으면 읽지 않았다고 한 줄로 알리고, 저장 직전에 `config.json.v1`·`models.json.v1` 로 이름만 바꿔 남깁니다(이미 있으면 `.v1-2`, `.v1-3` … — 덮어쓰지 않습니다).

### `endpoints` — 저장된 서버 목록

```bash
agent-cli endpoints
```

```
home  openai  127.0.0.1:8000          (default)
work  openai  gateway.example.com
```

이름·프로바이더·호스트(`host[:port]` — 경로·쿼리·사용자 정보는 보이지 않음)와 기본 서버 표시. 키와 헤더 값은 보이지 않습니다. 서버가 없으면 `agent-cli setup`·보드 admin 으로 추가하라는 한 줄입니다(종료 코드 `0`).

### `sessions` — 세션 관리

`run`과 `web` 모두 세션을 `.agent-cli/sessions/{session_id}/`에 자동 저장합니다. 세션 종료 시 컨텍스트 윈도우 내용이 요약으로 저장됩니다. `--resume`으로 이전 세션을 이어서 작업할 수 있습니다. 세션 루트는 `AGENT_CLI_SESSIONS_DIR` 로 작업 트리 밖으로 옮길 수 있습니다(헤드리스/CI·벤치 — `run`·`web`·`sessions`·`--resume` 이 모두 같은 루트를 봅니다, v8.50.0). 세션 id 는 만든 시각(초)이며, 같은 초에 여러 `run` 을 띄우면 각자 다음 빈 숫자를 받아 서로 다른 폴더에 씁니다(v10.127.4).

모델이 만드는 **임시 파일**(중간 결과·보조 스크립트·다운로드·백업본)은 `.agent-cli/tmp/` 에 두도록 꼬리의 Task Guidelines 가 안내합니다(v10.27.0; v10.28.0 부터 "입력 파일을 변경·소비하는 명령 전에 원본을 복사해 두라"는 백업 규칙도 같은 줄에 있어 백업본이 가는 곳이 한 군데입니다 — 전에는 위치 없이 "안전한 곳"이라고만 해서 소스 옆에 `.bak` 이 남았습니다) — `/tmp` 에 쌓이지 않고 프로젝트와 함께 지워지며, 워크스페이스 안이라 봉쇄 확인도 묻지 않습니다. 임시 파일 과제 8종 × 3회 실측: 시스템 임시 경로 사용 15/24 → 1/24 런, 봉쇄 거절 13 → 3. 절대 경로로 알려 주는 변형은 그 경로가 셸 명령 63% 에 `cd /abs && ` 로 번져 op 가 두 배가 되어 기각했습니다(상대 경로 규칙과 한 목소리). v10.90.0 부터 이 디렉토리는 세션을 조립할 때 하니스가 만들어 둡니다.

```bash
# 현재 워크스페이스의 세션 목록
agent-cli sessions

# 특정 워크스페이스의 세션 목록
agent-cli sessions --workspace /path/to/project

# 이전 세션 이어서 작업
agent-cli web -p openai -m gpt-4o --resume <session_id>
```

각 세션은 id·시각과 함께 **마지막 사용자 요청(↳)** 과 **마지막 결과(→)** 를 한눈에 보여줍니다 (답이 없는 세션은 그 세션을 연 프로세스가 지금 살아 있으면 `→ (in progress)`, 아니면 `→ (no answer)` — v10.127.2; 판정은 세션 폴더의 `session.lock` 잠금으로 합니다). 이 요약은 세션의 `history.jsonl` 에서 마지막 user↔complete 페어를 읽어 만듭니다 (별도의 `query` 메타 필드는 제거됨).

```text
Sessions for /path/to/project:

  1774752167 2026-06-04 10:31:02
      ↳ Analyze the project structure
      → src/, tests/, docs/ 3개 최상위 디렉토리로 구성된…
```

| 옵션 | 설명 | 기본값 |
|------|------|--------|
| `-w, --workspace` | 워크스페이스 경로 필터 | 현재 디렉토리 |

**`--resume` 없이 시작할 때:** `web` 을 `--resume` 없이 실행하면, 가장 최근 세션을 위 포맷으로 보여주고 `Resume it? [y/N]` 를 묻습니다. `y` 면 그 세션을 이어가고, 그 외(Enter 포함)는 새 세션으로 시작합니다 (안전한 기본값). 파이프/비대화 환경(stdin 이 TTY 아님)에서는 묻지 않고 항상 새 세션입니다. v10.144.0 부터 이 질문은 모델 서버에 접속하기 **전에** 합니다 — 이어 가는 세션에 기록된 서버로 떠야 하므로. 새 세션은 접속이 끝난 뒤 만들어, 부트가 실패하면 빈 세션 디렉터리가 남지 않습니다.

LLM은 `history` 도구로 현재 또는 이전 세션의 이력을 **SQL 로 질의**할 수 있습니다 (history 테이블에 `SELECT` — kind/tools/files/author/turn/text 컬럼, 읽기전용).

### `knobs` — 노브 표

세션 노브(모델·사고·문법·샘플링·에이전트 상한·압축 비율·무진전 한도/시도·자동 승인·감사)는 `agent_cli/knobs.py` 의 표 하나에 선언돼 있고, `run`/`web` 의 플래그·env·웹 헤더의 칩이 전부 그 표에서 파생된다 (docs/knobs/DESIGN.md). 이 명령은 그 표를 보여준다 — `--json` 은 보드처럼 밖에서 노브를 다루는 도구를 위한 것(이름·플래그·env·종류·선택지·기본값·칩 모양 힌트, 값은 없음).

```bash
agent-cli knobs          # 표 — 노브 · 플래그 · env · 종류 · 기본값 · 라벨
agent-cli knobs --json   # 같은 것을 JSON 배열로 (agent-board 가 spawn 폼을 이걸로 만든다)
```

### `stats` — 본문 없는 집계 (v10.130.0)

턴 기록(`turns.jsonl`)을 **숫자만** 남기고 접는다 — 회사 머신처럼 기록 원문을 들고 나올 수 없는 곳에서 "LLM 시간인가 도구 시간인가", "사고가 출력의 몇 %인가", "프리픽스 캐시가 듣는가" 를 버전·모델·방언별로 보려는 것. 모델 서버·설정 파일이 필요 없다.

```bash
agent-cli stats                                   # 이 프로젝트의 세션 디렉터리
agent-cli stats ROOMS_DIR HARBOR_JOB_DIR          # 흩어진 루트 여럿을 한 번에(재귀)
agent-cli stats --since 2026-11-01 --json --out stats.json
```

- **읽는 것**: 이름이 정확히 `turns.jsonl` 인 파일만(심볼릭 링크는 따라가지 않는다). **읽지 않는 것**: `history.jsonl`·`failures.jsonl`·`session.json`·`verbose.jsonl`·prompt_views — 대화·응답 본문이 든 파일은 열지 않는다.
- **출력에 드는 것**: 하니스 버전, 모델 이름, 방언 이름, 내장 도구 이름, 고정 상태 라벨(`parse_stage`·`failure_signal`·`stop_reason`·`stop_detail`·압축 모양), 그리고 숫자. MCP·스킬 도구 이름이나 모르는 값은 `other` 로 묶이고, 서버 오류 문구 같은 자유 문장은 내지 않는다. 경로·작업 디렉터리·세션 id·제목은 없고 날짜는 일 단위(첫날·끝날)뿐. 출력 머리 한 줄이 이것을 말한다 — 들고 나가기 전에 확인한다.
- **집계**: `harness_version × model × dialect` 묶음마다 세션·턴 수(세션당 중앙·p90·최대), 토큰(입력 합·출력·캐시 read·추론, 캐시 적중률·추론 비율), 시간(`ttft`·`llm`·`tool` ms 의 중앙·p90·합, LLM:도구 비), 결과 라벨별 개수와 잘린 턴, 도구별 호출·실패·시간, 압축 횟수·크기·시간. v10.129.0 이전 줄은 새 필드가 없어 그 항목의 분모에서 빠진다(항목마다 `n`), 버전·방언이 없는 줄은 `unknown`.
- 모델 이름은 그대로 나가되, 경로 모양(`/`·`~`·`.`·`C:\` 로 시작 — vLLM 은 `--model` 경로를 서빙 이름으로 쓴다)이면 마지막 요소만, 한 줄·120자까지. `org/name` 은 그대로.
- 표의 `sessions` 는 실제로는 `turns.jsonl` 파일 수다 — 에이전트 자리(`agents/<key>/`)의 기록도 하나로 센다.
- `--json` 은 JSON 하나(`schema: 1`), `--out` 은 그 출력을 파일로. 깨진 줄은 건너뛰고 센다. 종료 코드 0(0건이어도)·2(인자 오류·없는 PATH·직접 준 PATH 가 심볼릭 링크·`--out` 쓰기 실패).

### 세션 메모리 (`memory` 도구)

LLM 이 세션 중 **중대한 실패·중요한 발견·결정·메모**를 명시적으로 기록하고 필요할 때 꺼내
쓰는 도구입니다. `history`(raw 이력 SQL 질의)와 달리 **LLM 이 큐레이션한 durable
salience** 로, **컨텍스트 압축(compaction)에도 유실되지 않습니다.**

- **왜 필요한가**: 컨텍스트가 예산의 90% 를 넘으면 오래된 대화가 요약/드롭됩니다. "왜 이
  빌드가 깨졌는지" 같은 중요한 정보가 이때 사라집니다. 메모리는 **롤링 컨텍스트 밖**
  (`<session_dir>/memory.jsonl`)에 저장돼 압축 대상이 아니고, **`--resume` 로 복원**됩니다.
- **상시 인덱스**: 기록된 메모리의 요약(id·타입·한 줄)이 **세션 상태 블록**(아래)의
  `## Session Memory` 섹션에 **항상 노출**되어, LLM 이 "무엇을 기록했는지" 잊지 않습니다.
  전체 내용은 `memory` 도구의 `get` 모드(id 지정)로 필요할 때만 꺼냅니다(토큰 절약 + recall 보장).
  (시스템 프롬프트가 아니라 꼬리에 실리므로 `memory add` 가 KV 프리픽스 캐시를 깨지 않습니다.)
- **타입 4종**: `failure` ⚠(반복 회피) · `discovery` 💡(재사용) · `decision` 🔀(근거) ·
  `note` 📝(일반).
- **모드**: `add`(type+summary, 선택 detail/tags → id) · `get`(id) · `update`(id + 바꿀 필드)
  · `delete`(id). 발견이 나중에 틀리면 `update`/`delete` 로 정정. `mode` 없이 `summary` 만 와도
  `add` 로 받습니다(v10.81.0 — 벤치에서 그 모양이 3번 거절됐음). `list` 는 v10.81.0 에 삭제
  (벤치 0회, 상시 인덱스와 중복). 항목은 기록한 **턴**을 함께 담아 인덱스 줄에 `(turn N)` 으로
  보이고, `history` 의 `turn = N` 으로 그때의 원문에 닿습니다(docs/session-memory/DESIGN.md §11).

## 스킬 (Prompt Skills)

특정 작업에 최적화된 재사용 가능한 프롬프트 템플릿. Claude Code 스킬 포맷과 호환.

### 패키지 내장 스킬 (built-in)

패키지와 함께 배포되는 메타 스킬:

| 스킬 | 설명 |
|------|------|
| `/create-skill <name>` | 새 스킬 파일을 대화형으로 생성 (SKILL.md + scripts/) |
| `/create-agent <name>` | 새 에이전트 정의 파일을 대화형으로 생성 |
| `/plan <feature>` | 기능 요청을 작업 분해 + 의존성 + 범위 추정으로 구조화하여 `plan/` 에 저장 |
| `/windows-dump <dump>` | **Windows 전용** — 커널 미니덤프·`MEMORY.DMP`·사용자 모드 `.dmp` 를 Linux/macOS 에서 분류. 동봉된 `kdump.py` 가 버그체크 파라미터를 해석·역참조하고 드라이버 목록·스택 스캔·PDB 짝 판정·심볼을 낸다. 모델이 덤프를 보면 스스로 부른다 (아래 [Windows 덤프 분류](#windows-덤프-분류-windows-dump)) |
| `/orchestrate <task>` | 자율 에이전트 팀 부트스트랩 — 계획 수립 후 워커들을 **idle 로 소환만** 하고(일을 시키면 회신이 main 을 깨움), 로스터(키)를 `orchestrator` 에이전트에게 인계 후 즉시 반환. 조율(배정→리뷰→수리)은 orchestrator 가 peer message 로 자율 진행, main 은 최종 보고만 자동 수신 |

사용자가 같은 이름의 스킬을 `.agent-cli/skills/`에 만들면 built-in을 오버라이드합니다.

### Windows 덤프 분류 (windows-dump)

Windows 드라이버 크래시를 Linux/macOS 에서 보기 위한 내장 스킬입니다 (v10.105.0). 모델이 덤프 파서를 즉석에서
짜면 레이아웃을 틀리고 틀린 줄을 모른 채 분석을 이어갑니다 — 실제로 맞는 PDB 를 "이미지와 안 맞는다" 고
우기다 실패한 사례가 있었습니다(PE 의 RSDS Age 2 와 PDB 정보 스트림 Age 5 가 달랐을 뿐 GUID 는 같고 DBI
Age 는 2). 그래서 검증된 도구 `agent_cli/skills/builtin/windows-dump/kdump.py` 를 주고(표준 라이브러리만,
3.10+, 폐쇄망 OK) 중간값을 전부 보이게 했습니다.

```
python3 .../windows-dump/kdump.py info      MEMORY.DMP   # 버그체크 코드·파라미터(종류별 해석·역참조)·OS 빌드·레지스터·예외
python3 .../windows-dump/kdump.py modules   MEMORY.DMP   # 적재 드라이버: base·size·TimeDateStamp·심볼서버 식별자·PDB
python3 .../windows-dump/kdump.py stack     MEMORY.DMP   # 스택 스캔(언와인딩 아님) + 범인 명령·경계 표시
python3 .../windows-dump/kdump.py symbolize MEMORY.DMP --symdir sym --pe mydrv.sys=… --pdb mydrv.sys=…
python3 .../windows-dump/kdump.py pdbcheck  build/mydrv.sys build/mydrv.pdb      # GUID 16바이트 비교, Age 세 값 표시
python3 .../windows-dump/kdump.py fetch-symbols MEMORY.DMP --symdir sym [--server 사내서버]
```

- **버그체크 파라미터 표**: 코드마다 네 파라미터에 종류(`ip`·`va`·`irql`·`access`·`ntstatus`·`trap`·`ctx`·`exr`·`prcb`·`ptr`·`sub`)를
  붙여 두었고(Microsoft 레퍼런스 대조, 2026-11-05), `info` 가 종류대로 따라갑니다 — 명령 주소는 `module+rva`, 데이터 주소는
  널·비정규·모듈·언로드 범위·스택·풀로 분류, 트랩 프레임·CONTEXT 포인터는 역참조해 **진짜 문제 지점의 레지스터**
  (`faulting_context`)를 냅니다. 전체 덤프의 헤더 컨텍스트는 `KeBugCheckEx` 호출 지점이라 그것만 보면 범인 프레임을
  놓칩니다(WinDbg `!analyze -v` 가 `.trap`/`.cxr` 로 하는 일). `stack` 은 그 Rsp 자리에 경계선을 긋고, 다른 스택이면
  따로 스캔합니다.
- **PDB 짝 판정**은 GUID 16바이트로만 하고 Age 는 세 값(PE·PDB info·DBI)을 다 보여 줍니다. PDB 가 없거나 GUID 가
  다르면 이름을 지어내지 않고 `module+rva` 로 멈춥니다.
- 검증 범위: kdmp-parser 공개 덤프 6종(레지스터·물리 읽기 전부 일치), dumplib 커널 미니덤프 x64·x86(x86 샘플로
  KTRAP_FRAME 의 `&HardwareEsp == Esp` 실측), rust-minidump MDMP, msdl 다운로드, Win7 `ntkrnlmp.pdb`(OMAP) 로
  `KeBugCheckEx` 복원. **x64 KTRAP_FRAME 레이아웃과 실제 사내 덤프는 WinDbg 와 아직 대조하지 않았습니다** —
  `references/verify-prompt.md` 가 그 대조 절차(모델에게 붙여 넣는 프롬프트)입니다.
- 하지 않는 것: `!analyze -v` 식 원인 추정, 언와인딩, `!irp`·`!wdfkd` 류 확장, WPP 디코딩. 그건 Windows 호스트의
  WinDbg(ssh 로 `kd -z x.dmp -c "…; q"`) 몫이고 포인터 종류 파라미터는 그 용도로 그대로 찍어 줍니다.

### 프로젝트 스킬 예시

아래 이름(`/review-code` 등)은 내장이 아니라 `.agent-cli/skills/` 에 직접 만든 스킬이라고 가정한 예입니다 — 지금 쓸 수 있는 목록은 `/skills` 로 봅니다.

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
| `allowed-tools` | 허용 도구 리스트 (미지정 시 전체). `write_file` 이 있으면 `edit_file` 도 붙는다 (v10.77.0) | |
| `max-turns` | 최대 턴 (미지정 시 글로벌 설정 사용) | |
| `model` | 스킬 실행 시 모델 오버라이드 (미지정 시 현재 모델 사용) | |
| `context` | `fork`이면 독립 컨텍스트에서 실행 (부모 대화 히스토리 없음) | |
| `hooks` | 스킬 스코프 lifecycle hooks (PreToolUse, PostToolUse 등) | |
| `disable-model-invocation` | `true`이면 LLM 자동 호출 금지 (사용자만 `/명령`으로 호출 가능) | |
| `user-invocable` | `false`이면 `/skills` 메뉴에서 숨김 (LLM만 호출 가능) | |
| `argument-hint` | `/skills` 표시 시 인자 힌트 | |

스킬 검색 경로 (v9.0.0 — 프로젝트만; `~/.agent-cli/skills/` 는 읽지 않음):
1. `.agent-cli/skills/*.md` (플랫)
2. `.agent-cli/skills/<name>/SKILL.md` (디렉토리)
3. 패키지 내장

같은 검색 경로 내에서 동일 이름의 플랫 파일과 디렉토리 스킬이 모두 존재하면 에러가 발생합니다.

## Hooks

에이전트 라이프사이클 전반에 걸쳐 동작하는 확장 시스템입니다. **Python hook**과 **Shell hook** 두 가지 방식을 지원합니다.

### Python Hooks

`.agent-cli/hooks/*.py` (v9.0.0 — 프로젝트만; `~/.agent-cli/hooks/` 는 읽지 않음). 세션당 러너 하나를 main·상주 에이전트·스킬·일회성 에이전트 루프가 모두 공유하고, 패키지 내장 훅(`agent_cli/hooks/builtin/`)이 프로젝트 훅보다 먼저 돕니다. 밑줄로 시작하는 파일은 훅으로 읽지 않습니다:

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

### Hook 이벤트 (12개)

| 이벤트 | 시점 | Python | Shell |
|--------|------|--------|-------|
| `OnSessionStart` | 세션 시작 | ✓ | |
| `PreLLMCall` | LLM 호출 직전 (매 턴) | ✓ | |
| `PostLLMCall` | LLM 응답 수신 후 | ✓ | |
| `PreToolUse` | 도구 실행 직전 | ✓ block/modify | ✓ exit 2=block |
| `PostToolUse` | 도구 실행 직후 | ✓ | ✓ |
| `OnTurnEnd` | ��� 종료 후 | ✓ | |
| `PreWait` | 받아들여진 `wait` 가 기다리기 직전 (거절된 `wait` 에서는 불리지 않음, v10.163.0) | ✓ (막을 수 없음) | |
| `OnAgentStart` | agent run 실행 직전 (`agent` 도구·`@<profile>` 명령 모두, v10.168.0) | ✓ | |
| `OnAgentEnd` | agent run 완료 후 (세션 종료로 끊긴 과제에는 불리지 않음, v10.164.0) | ✓ | |
| `OnSkillStart` | skill 실행 직전 (`run_skill` 도구·`/skill` 명령 모두, v10.168.0) | ✓ | |
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
    # 훅이 넣은 섹션은 훅이 뺄 때까지, 그 작업 구간 동안 프롬프트에 있다 (v10.170.0)
    ctx.inject_system_section("Memory", "facts...")   # 제목별 병합 — 같은 제목은 교체, 다른 제목은 유지
    ctx.remove_system_section("Memory")                # 앞선 발화(OnSessionStart·앞 턴)가 넣은 섹션도 뺀다

    # 도구 제어 (PreToolUse only)
    ctx.block("reason")
    ctx.modify_input({"command": "ls"})

    # MCP 메모리 (MCP memory 서버 연결 시)
    ctx.store_memory([{"name": "...", "entityType": "...", "observations": [...]}])
    ctx.search_memory("query")
    ctx.read_memory()
```

### Shell Hooks (기존 방식)

`.agent-cli/hooks.json` **하나**입니다 (v9.0.0 — `~/.agent-cli/hooks.json` 은 읽지 않음). 모든 프로젝트에 공통으로 거는 정책 훅이라는 개념은 없어졌습니다 — 필요하면 프로젝트마다 둡니다.

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

- run/web/스킬/서브·상주 에이전트 전 경로에 적용됩니다
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
agent-cli run "task" -p anthropic --base-url https://api.anthropic.com/v1 -m <model>
# 또는 `agent-cli setup` 으로 provider anthropic 서버를 저장하고 --endpoint 로 고른다
```

System prompt에 자동으로 prompt cache(`cache_control: ephemeral`)가 적용된다.
한 세션에서 두 번째 콜부터 시스템 프롬프트가 캐시 히트하여 입력 비용을 90% 절감한다.
캐시 hit/write 토큰은 turn summary에 표시된다.

## 모델 레지스트리

`models.json`에 **서버별로** 모델 능력치를 정의합니다 (v10.144.0 — 같은 모델 이름이라도 서버에 따라 방언이 다를 수 있어 서버 아래에 둡니다). 한 방(프로세스)은 자기 서버의 칸만 읽고 씁니다:

```json
{
  "endpoints": {
    "home": {
      "models": {
        "gpt-4o": {
          "provider": "openai",
          "context_window": 32768,
          "max_output_tokens": 4096,
          "supports_thinking": true,
          "supports_vision": true,
          "dialect": "json_fc"
        }
      }
    }
  }
}
```

`supports_vision`(v10.59.0) — 모델이 이미지를 보는가. 키가 **없으면** 미확인으로 두고, 이미지를 처음 보낼 때(또는 `run --image` 부팅 때) 두 색 이미지를 보내 답을 검사하는 프로브를 **프로세스당 한 번** 돌린다. **판정이 나면 그 값을 엔트리의 빠진 키에 적는다**(v10.68.0, `config.fill_model_entry` — 사용자가 적은 값은 건드리지 않고, 없는 키만). 답이 돌아왔거나 서버가 4xx 로 이미지를 거부한 경우가 판정이고, 연결 실패·타임아웃·5xx 는 그 프로세스에서만 `false` 다(장애 순간의 결과를 파일에 박지 않는다). 첫 실행 감지(자동 등록)는 이 값을 함께 적는다.

### 파일 위치 및 정책

| 우선순위 | 위치 | 역할 | 자동 저장 |
|---------|------|------|----------|
| 1 | `~/.agent-cli/models.json` 의 `endpoints.<이 세션의 서버>.models` | 유저 설정 (v9.0.0 — 프로젝트 `models.json` 은 읽지 않음) | 새 모델 자동 저장 (그 서버 칸에) |
| 2 | `agent_cli/default_models.json` | 패키지 기본값 — 서버와 무관한 바닥 | 안 함 (읽기만) |

- 미등록 모델은 런타임 자동 감지 → `~/.agent-cli/models.json` 의 이 세션 서버 칸에 저장. 서버가 골라지지 않은 실행(env 만)은 패키지 기본 표만 읽고 저장하지 않습니다
  - OpenAI 호환: context window를 `/v1/models`의 `max_model_len`에서 읽고, 값이 없으면 컨텍스트 오버플로 프로브로 추정. 추가로 thinking 지원 여부를 프로브.
  - Anthropic: 동일한 로직을 Anthropic API(`/messages`, `x-api-key`+`anthropic-version`)로 프로브 — context window는 `/models` 메타(omlx) → `/messages` 오버플로 프로브 → 128K 폴백, structured-output 은 프롬프트 기반 JSON 검사. (`/v1/models` 메타가 없는 실 Anthropic 모델은 레지스트리/폴백 사용.)
  - Thinking 감지: 프로브 프롬프트 → `reasoning_content` 필드 또는 `<think>` 태그 확인 (하드코딩 없이 자동). **2단계(v8.22.0)**: 기본 프로브에서 미검출이면 사고 스위치를 켜고 재프로브 → Qwen 처럼 사고가 기본 OFF 인 모델도 감지. **v8.37.0 부터 두 transport 공통**: OpenAI 호환은 `enable_thinking=true`, Anthropic 호환은 `thinking` 블록으로 재프로브(재프로브 실패는 관용 → 미지원). (이미 저장된 엔트리는 재감지 전까지 갱신 안 됨 — 과거 `false` 로 잘못 저장됐으면 재감지/수동 수정 필요.)
  - 자동 산출 규칙: `max_output = context_window // 4`. context window가 16K(`MIN_CONTEXT_WINDOW`) 미만이면 `UnsupportedModelError`로 거부.
- 런타임 감지도 실패하면 사용자에게 대화형으로 context window, thinking 지원 여부, **dialect 바인딩**(등록된 방언 이름 중 하나 — 필수, v10.3.0; 오타는 재질문)을 질문 → `~/.agent-cli/models.json`에 저장
- **방언이 안 묶인 엔트리(감지는 성공했거나 손으로 적었는데 `dialect` 가 없음)는 터미널의 `run` 이 그 자리에서 묻습니다 (v10.127.0)** — 선택지마다 한 줄 설명(방언 스펙의 `summary`; `run --help` 의 `--dialect` 설명도 같은 출처)을 보여 주고, 고른 이름을 `~/.agent-cli/models.json` 의 그 엔트리에 저장한 뒤 진행합니다(다음부터 안 묻습니다). 추천은 하지 않습니다 — 어느 방언이 맞는지는 서버·모델에 달렸습니다. 터미널이 아니면(보드·파이프·`AGENT_CLI_NO_INPUT=1`)과 `web` 은 `No dialect for model '…'` 와 종료 코드 `2`, `--dialect` 를 준 실행은 묻지 않습니다. 답을 저장할 수 없는 경우(엔트리가 사용자 파일에 없음 — 패키지 기본 표에만 있는 모델 등)는 묻지 않고 같은 오류 `2` 입니다. `"dialect": ""`·`null` 은 묶이지 않은 것으로 보고 묻습니다(값이 있는 `dialect` 는 덮어쓰지 않습니다).
- 이미 등록된 모델은 덮어쓰지 않음 (사용자 설정 보호)
- 다음 실행 시 저장된 설정에서 로딩 (프로브/질문 재실행 없음)

| 필드 | 설명 |
|------|------|
| `context_window` | 컨텍스트 윈도우 크기 (토큰) |
| `max_output_tokens` | 최대 출력 토큰 |
| `supports_thinking` | Thinking/reasoning 지원 (단독 게이트, v8.21.0 — 이 키가 true 인 모델에만 🧠 스위치가 요청에 실린다; 정도는 서버 기본값, v10.72.0) |
| `dialect` | **필수 (v10.3.0)** — 이 모델의 방언 바인딩 (`json_fc`, `xml_fc`, `hermes_json`, `glm_argkey`, `native_fc`; v10.4.0 부터 옛 키 `wire_format` 은 읽지 않음). 모델마다 학습된 tool-call 포맷 프라이어가 다를 때 사용 — 지정하면 `--dialect` 미지정 시 이 포맷으로 실행되고, **서브에이전트도 자기 모델의 바인딩을 따릅니다** (프로필 `model` 오버라이드 포함 — main 과 다른 포맷으로 도는 서브에이전트 가능). 미등록 이름은 부트/spawn 시 즉시 실패. 설정 경로: 손편집 / 터미널의 질문("Dialect (glm_argkey / hermes_json / json_fc / native_fc / xml_fc): " — 탐지 실패 뒤의 모델 등록과, 방언 없는 엔트리의 `run` 부트(v10.127.0); 기본값 없음) / agent-board ⚙ admin 모델 행 드롭다운. 자동 감지 refresh 에도 보존됩니다 |

**설정 우선순위**: `~/.agent-cli/models.json` (유저) > `default_models.json` (패키지) > 런타임 감지 > 보수적 기본값

## 도구

LLM이 사용할 수 있는 도구 목록:

**지연 도구 (v10.110.0)**: `agent`·`code_index`·`monitor`·`browser`·`schedule`·`history`·`fetch`·`snapshot`·`memory`·`message`·`reply` 는 프롬프트에 "언제 쓰는가" 한 줄과 인자 뼈대(이름·타입·값 목록·필수)만 실리고, 전문은 모델이 `tool_help` 로 받습니다(스키마 불일치로 처음 거절될 때는 거절 결과에 한 번 자동으로) — main 프롬프트 8,002 → 4,763 토큰(GLM, xml_fc). 인자 설명까지 뺀 뼈대(V2)는 표적 과제 측정에서 정답·도구 선택이 인자 전문(V1)과 같고 더 싸서 골랐다. docs/tool-help/DESIGN.md

| 도구 | 설명 |
|------|------|
| `read_file` | 텍스트 파일 읽기 (hashline 태그, flat-native — 한 op=한 파일). 여러 파일은 멀티-op 으로 read_file op 을 여러 개 emit. 이미지는 `view_image` (v10.77.0) |
| `write_file` | 파일 생성/덮어쓰기. 작성 content 를 hashline 으로 반환 (read_file 없이 edit_file 직결) |
| `edit_file` | hashline 기반 정밀 편집 (퍼지 매칭 지원) |
| `shell` | 셸 명령 실행 |
| `fetch` | URL 을 가져온다 — HTML 은 마크다운으로, JSON 은 들여쓴 텍스트로, XML·평문은 그대로. v10.92.0: 같은 도메인 링크를 따라가던 `depth` 를 지웠고(벤치·실사용 0회) 오류 결과는 사실만 말합니다 — docs/tool-review/TRACKER.md. v10.52.0 부터 JSON API(PyPI·Wikipedia REST·OSV·GitHub API 등)를 직접 읽고, charset 없는 UTF-8 페이지가 깨지지 않습니다. v10.56.2 부터 본문을 자르지 않습니다. 큰 결과는 다른 도구와 같이 파일로 저장되고 발췌가 옵니다. v10.56.3 부터 성공 코드(200·202)로 온 봇 확인 페이지와 JavaScript 로만 그려지는 빈 껍데기를 실패로 돌려줍니다(DuckDuckGo 202, Google 검색 89자). v10.118.0 부터 실패는 세 줄입니다: `Fetch failed (<cause>): <url>`, 사실 한 문장, 다시 부를 가치가 있는지 한 문장. 원인은 `dns`·`connect`·`timeout`·`tls`·`http-auth`(401·403)·`http-missing`(404·410)·`http-rate`(429, `Retry-After` 초)·`http-server`(5xx)·`http-other`·`bot-check`·`needs-js`·`login-wall`(200 으로 온 로그인 폼, 신규)·`request` 13종이고, 웹 방의 관찰 카드는 이 원인을 칩으로 보입니다. 연결 실패에는 환경에 프록시 변수가 실제로 있을 때만 그 이름(값은 적지 않음)과 `NO_PROXY=*` 사실을 덧붙입니다. v10.56.4: HTML 표를 행마다 한 줄로 읽고(코드 보기 페이지·위키 정보상자의 줄과 칸이 붙던 것), Gerrit·gitiles 의 `)]}'` 접두 JSON 과 `?format=TEXT` base64 원문을 바로 읽습니다. v10.57.2: 링크를 `[글](주소)` 로 싣습니다(같은 사이트는 `/경로`, 주소가 글을 되풀이할 뿐이면 글만; Bing·Google·DuckDuckGo 추적 주소는 실제 주소로) — 검색 결과를 따라갈 수 있습니다. 블록 경계에서 줄을 바꾸고(Scholar 제목·저자가 붙던 것), 숨김 요소(`hidden`·`aria-hidden`·`display:none`)를 건너뛰고(Scholar 가 늘 싣는 숨은 "작동되지 않습니다" 문구), 공백을 HTML 처럼 접어 탭만 있는 줄·빈 목록 줄을 없앱니다(Wikipedia 본문이 266번째 줄 → 7번째 줄). 그림은 alt 글로(Wikipedia 수식의 TeX). v10.58.0 부터 `download_to` 인자로 응답을 변환 없이(원본 HTML·JSON·파일 바이트) 그 경로에 저장하고 결과로 경로·크기·첫 몇 줄만 받습니다(`depth` 와는 함께 못 씀). 바이너리(PDF·이미지·압축 파일)는 거절하지 않고 `.agent-cli/tmp/downloads/` 에 자동 저장합니다(이름은 URL 마지막 조각, 겹치면 번호; gitiles 바이너리 blob 은 base64 를 풀어 저장). v10.58.1 부터 큰 결과의 저장 파일은 `[Fetched …]` 머리줄 없이 본문만 담고, 안내가 그 파일이 변환본(원본 응답 아님)이며 원본은 `download_to` 로 받으라고 말합니다 — 모델이 머리줄째 복사한 파일을 JSON 으로 넘긴 일이 있었습니다. 작업 구간을 멈추면(■·Ctrl+C·`agent` `kill`·세션 종료) 기다리던 요청도 그만둡니다 — 전경 `shell`·MCP 호출과 같은 0.2초 간격으로 같은 정지를 보고 `This run was stopped, so the request was given up after 0s.` 를 돌려줍니다(종전에는 요청의 시한 15초를 다 기다렸고 `Fetch failed (timeout) … One retry may succeed.` 가 기록에 남았습니다). |
| `browser` | 헤드리스 브라우저 — JavaScript 가 그리는 페이지를 읽고 클릭·입력 (v10.39.0). 스냅샷의 `[ref=eN]` 로 조작. 선택 설치 `agent-cli[browser]` — playwright 가 없으면 도구 자체가 프롬프트에서 빠짐. [아래 참고](#브라우저-도구-browser) |
| `agent` | 서브에이전트 (일회성 `run` + 상주 `spawn`/`request`/`status`/`resume`/`kill`). run: 한 op=한 task, 연속 run op = 병렬. spawn: 컨텍스트 유지 상주 — 회신은 관찰로 **자동 배달**(도착 시 main 이 깨어남 — status 폴링 불필요; working/미처리 요청이 보이는 status 응답에는 "`wait` 로 턴을 마쳐라" 힌트가 붙음 — v10.65.0). 배달된 회신 말미에는 그 에이전트의 **배달-시점 잔여 상태** 한 줄(`working · N queued` 또는 `idle — ready`)이 동봉되어 밀린 작업량을 바로 알 수 있음. `status` 결과 끝의 `(main has N unread mail queued — replies, requests or notices)` 는 main 상자에 줄 선(아직 읽지 않은) 봉투 중 사람의 말이 아닌 것 — 회신·요청·통지·작업·모니터·감사·체크인 — 의 수이고, 묻는 쪽이 에이전트여도 main 의 상자다(v10.173.0) |
| `history` | 세션 이력 SQL 질의 (history 테이블 SELECT: kind/tools/files/author/turn/text) |
| `memory` | 세션 메모리 — 중대한 실패·발견·결정·메모를 기록/조회 (compaction 무관, resume 복원, 상시 인덱스). 모드: `add`/`get`/`update`/`delete`/`list` |
| `code_index` | tree-sitter 기반 SQLite 코드 인덱스 (읽기 전용, flat-native — 한 op=한 query). 여러 query 는 멀티-op 으로 (모드 섞기 가능). lazy build + sha1 incremental + edit/write post-hook 자동 갱신. 9 mode: `list`/`fetch`/`lookup`/`kind`/`file`/`refs`/`callers`/`callees`/`slice` (`build` 는 v10.92.0 에 삭제 — 매 쿼리가 갱신하므로 할 일이 없었다). Python/JS/TS/C/C++/Go/Rust/Java/Markdown. v10.92.0: 모델이 보는 안내 블록(1,104 토큰, 호출 예시 11개)을 지우고 모드 설명을 `mode` 인자에 — 1,689 → 683 토큰 |
| `view_image` | 이미지 파일(PNG/JPEG/GIF/WebP)을 모델에게 보인다 — 결과에 이미지가 붙어 압축이 요약할 때까지 남는다. 못 본다고 알려진 모델엔 없다 (v10.77.0). Pillow 가 있으면 본문까지 풀어 보고, 헤더만 멀쩡한 손상 파일은 붙이지 않고 다시 만들라는 오류로 돌려준다 (v10.113.0) |
| `tool_help` | 한 줄로만 소개된 도구의 전문 — 그 도구가 프롬프트에 원래 실렸을 설명·인자 설명·안내를 결과로 준다. 지연 도구가 있는 루프에만 붙는다(v10.109.0 기반, v10.110.0 부터 지연 도구 11개 — docs/tool-help/DESIGN.md) |
| `wait` | **닫지 않고 기다린다** (v10.65.0) — 띄운 작업·모니터·에이전트의 보고, 또는 내가 사람에게 건 질문의 답을 기다릴 때(v10.175.0 — 수락 문장 `waiting — will wake on: your question "q-3" to the user.`, 질문만 기다리면 중간 깨우기가 없다). 런을 끝내지 않는다: 받아들여지면 그 자리에서 기다리고, 깨면 같은 런이 같은 열린 항목으로 잇는다(v10.163.0 — 종전엔 런을 끝내고 열린 항목을 다음 깨우기 런으로 넘겼다). 기다리는 중 세션이 닫히면 그 자리에서 세션 종료로 끝난다 — 모델을 다시 부르지 않는다(v10.162.0·v10.164.0). 깨울 것이 없으면: 닫지 않은 것 — 갚지 않은 회신(`r-`)이나 답하지 않은 사람의 요청(`u-`) — 이 있을 때 그 번호를 말하며 거부(`wait refused — nothing would wake you, and [u-1] is still unanswered. Answer it with `complete`, or keep working. To hear from an agent, send it a request with `message` first.` · 회신이면 `… you still owe a `reply` [r-2]. Reply first, or keep working.`), 없을 때 받아들여 작업 구간을 끝낸다 — 관찰 `nothing to wait for: … This run has ended.` 이 기록에 남고 화면에 `Run ended — nothing to wait for.` 한 줄(5d). 그래서 사람의 질의를 든 런은 답하기 전에 `wait` 로 끝나지 않는다. 꼬리의 `## Open Items` 끝에 "못 닫는다면 `wait`" 한 줄이 붙는다(v10.67.1, wait 가 있는 루프에만). 깨우는 봉투(사람의 말·메일·알림)가 **이미 와 있으면** 역시 거부하되 무엇이 왔는지와 먼저 할 일을 알린다 — "the user wrote to you while you were thinking; their message is right below. Answer now with `complete`: …" / "1 notice arrived … Deal with it first, …"(v10.66.2, 문구 v10.159.0 — 런을 끝내 봐야 바로 다시 깨울 뿐; 거부 관찰과 그것은 같은 프롬프트에 연달아 들어간다). 설계: `docs/wait/DESIGN.md` |
| `complete` | 작업 완료 신호 (최종 결과 반환). `result` 는 답으로 전달되는 유일한 글이라(사용자 또는 부른 에이전트) 답을 먼저 쓰고(사용자에게는 마크다운으로 그려진다는 것도 설명에 적는다 — v10.96.1), 한 일에 기대는 답이면 끝에 `Checked:`(무엇을 돌리거나 봤고 무엇이 나왔나)·`Not checked:`(실행 대신 읽어서 추론한 것·안 돌린 것) 두 줄을 붙이라고 설명에 적는다 — 확인할 것이 없는 답엔 두 줄 없음. 같은 틀을 꼬리 `## Open Items` 의 `complete` 줄 바로 아래에도 한 줄로 싣는다 — 35B 는 도구 설명만으로는 따르지 않았다(v10.75.0). 기다리는 용도가 아니다 — 그건 `wait` |
| `ask` | 사람에게 질문 하나를 올리고 **바로 돌아온다** (v10.176.0 — 누가 묻든 한 길: main·에이전트·스킬). 관찰은 `question "q-2" sent — "…"`, 질문은 ❓ 트레이와 터미널 질문 줄에 서고, 답·닫힘은 나중에 줄 없는 통지 봉투(`── answer [n-…] from the user re [q-2] ──` / `── closed [n-…] …`)로 온다 — 답을 기다려야 하면 `wait`. 같은 질문이 열려 있는 동안 다시 물으면 원래 질문으로 접힌다. 답이 올 수 없는 곳(터미널도 웹 뷰어도 없는 `run`)에서는 거절 — `ask refused — nobody can answer here: this session has no terminal and no viewer. Go on with what you have and say what you assumed in your result.`(v10.174.0). 질문은 웹 ❓ 트레이(`Send`·`Close without answer`)나 `/answer q-N <글>`·`/close q-N` 으로만 답하거나 닫는다 — 그 밖의 말은 질문이 열려 있어도 main 의 요청이다 |
| `run_skill` | 등록된 스킬 실행 (LLM이 자동으로 호출 가능) |

### action_input 키 네이밍 규칙

**모든 builtin 도구가 flat-native**입니다 (consolidation Step 3 완료) — `action_input` 에 표준 키를 그대로 씁니다: 파일 도구(`read_file`/`write_file`/`edit_file`)·`code_index`는 `{path/mode, ...}`, `agent`는 `{mode, task, ...}`, `shell`은 `{command, ...}`, 제어 도구(`complete`/`ask`/`run_skill`)도 표준 키. 한 op = 한 대상이고, 여러 대상(여러 파일 읽기, 여러 쿼리, **여러 병렬 서브에이전트**)은 한 턴에 **op 을 여러 개** 냅니다.

어떤 builtin 도 wire-key prefix 를 쓰지 않습니다. wire-key prefix(`{tool}_{param}`) 메커니즘 — 모델이 `action` 이름을 빠뜨려도 키 모양으로 도구를 복구(dropped-action recovery) — 은 **미래 prefixed 도구/포맷용 latent seam** 으로 코드에 남아 있고, 현재 어떤 builtin 도 활성화하지 않습니다. MCP/외부 도구는 **prefix-less**(자체 bare 스키마)라 이 메커니즘과 무관합니다.

> 아래 예시들은 각 도구의 표준 키 그대로입니다 — flat-native 라 그대로 전송하면 됩니다.

### read_file — 파일 읽기

한 번에 **텍스트 파일 하나**를 읽습니다 (flat-native). 모드(부분 범위 / 검색 / stat)를 고를 수 있고, 각 줄에 `LINE#HASH:content` hashline 태그가 붙습니다. 전체·범위 읽기 결과는 `[read] path: all N lines` / `lines a–b of N` 머리줄로 시작합니다(v10.77.0 — 범위가 끝을 넘거나 빈 파일이면 그렇다고 말함). 도구 설명은 모드를 한 문단에 한 번만 말합니다(v10.77.0 에 사용 안내 블록·호출 예시·code_index 안내 삭제). 여러 파일은 멀티-op 포맷에서 read_file op 을 여러 개 emit 해 한 턴에 읽습니다 (op 배열이 곧 배치 — 중첩 배열 없음).

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
  - `line_start` / `line_end`: 1-based, inclusive. 둘 다 생략하면 full read. `line_end` 만 주면 1 부터 그 줄까지(v10.177.7 — 전엔 `line_end` 를 무시하고 전체를 읽었다).
  - `search`: 정규식 패턴. 매칭 줄 + 주변 `context` 줄을 반환 (기본 5).
  - `stat`: 파일 크기/총 줄 수 + 앞 20줄 (메타데이터 조회, read 아님).
- **이미지 (v10.59.0)**: 파일이 PNG/JPEG/GIF/WebP 이면(확장자가 아니라 매직 바이트) 글 대신 이미지를 모델에게 보여 준다. 결과 글은 `[image: shot.png (1280×720)]` 한 줄과 형식·바이트이고, 이미지는 그 관찰에 딸려 간다. 줄 범위·검색·stat 은 이미지에 뜻이 없어 무시된다. 모델이 이미지를 못 보면 그 사실이 관찰에 적힌다("이 모델은 이미지를 못 본다 — 스크립트로 읽어라").

### edit_file — Hashline 편집

`read_file`에서 받은 hashline 태그를 사용하여 정밀 편집합니다. **한 op = 한 편집** (flat-native). 같은 파일의 여러 편집은 한 턴에 이어진 `edit_file` 호출로 보낼 수 있고(모든 ref 는 같은 읽기에서, 함께 적용되므로 서로의 줄 번호를 고쳐 쓰지 않는다) — 이 규칙은 v10.112.0 부터 도구 설명에 있습니다. 해시 불일치 오류는 "This is a guardrail, not a failure: the file changed since that read." 로 시작해 지금 줄과 새 ref 를 보여 줍니다. `lines` 는 줄마다 들여쓰기까지 담은 문자열 배열이어야 하고, 맨 문자열이면 거절합니다(v10.112.0; "delete" 는 `lines` 를 쓰지 않으니 무시). v10.77.0: `pos`/`end` 에 줄 번호만(`"20"`·`20`) 보내면 편집하지 않고, 그 줄의 지금 ref 를 오류에 보여 줍니다(`end '20' is a bare line number — … That line is now: 20#KT:… Use '20#KT'.` — 35B 최근 런 편집의 18% 가 이 모양이었습니다). 설명은 `op` 의 네 값과 인자를 이름으로 말하고, 안내는 짧아졌습니다:

**바뀐 것이 없으면 그렇게 말합니다 (v10.77.1)**: 새 `lines` 가 그 자리의 줄과 같거나, 넣을 줄이 없거나, `end` 가 `pos` 보다 앞이라 파일이 그대로면, 쓰지 않고 `No change: … — <까닭>. Nothing was written.` 을 돌려줍니다. 같은 내용의 줄은 해시 태그도 같다는 것도 함께 알립니다(ref 의 번호가 그중 하나를 고릅니다).

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

**같은 파일 다중편집**도 한 턴에 보낼 수 있습니다 — **연속된 같은-path edit_file op 들은 하나의 원본 read 기준으로 함께 적용**됩니다(모든 ref 를 mutate 전에 해석 → overlap 사전거부 → 줄번호 내림차순 bottom-up 적용 → 1회 쓰기). 그래서 앞 편집이 줄을 밀어도 뒤 편집의 ref 가 어긋나지 않습니다. ref 는 **마지막 read 기준 그대로** 쓰면 됩니다(다른 편집을 의식해 줄번호를 미리 보정하지 말 것). **all-or-nothing**: 한 편집이라도 overlap/해시불일치면 그 파일은 손대지 않고 배치 전체가 거부됩니다. 거부 관찰은 **틀린 op 를 전부** `op 3 (replace 8#ZZ): Hash mismatch …` 로 나열하고, 나머지 ref 는 그대로 유효하며 파일이 안 바뀌었음을 말합니다 — 모델은 틀린 것만 고쳐 전체를 다시 보내면 됩니다(v9.26.3). 해시 불일치 오류에는 그 줄 **±3줄의 현재 내용과 새 ref** 가 함께 나와, 모델이 파일을 다시 읽지 않고 바로 고쳐 보낼 수 있습니다(v10.50.0). 겹치는 범위는 턴을 나눠 사이에 re-read 하세요. 다른 파일 편집은 같은 턴에 여러 op 로 보내도 됩니다(서로 독립).

해시 불일치 시 퍼지 매칭으로 자동 보정합니다 (공백/따옴표/대시 정규화 — 같은 줄번호 한정).

`pos`/`end` 에 read_file 이 보여 준 **줄 전체**(`"2#KT:    return \"world\""`)를 붙여 넣어도 콜론 앞 태그로 받아들입니다(v10.31.0 — Qwen3.6-35B 가 이 모양으로 두 번 거부당한 뒤 edit_file 을 포기하고 파일을 통째로 다시 쓴 실측에서; 여러 줄을 붙여 넣은 건 어느 줄인지 모호해 그대로 거부). `lines` 에 섞여 온 `#KT:` 접두는 파일 내용이라 구제하지 않습니다.

편집이 성공하면 결과에 **unified diff** 와 함께 **`Updated region` 블록**(바뀐 영역 ±3줄의 fresh hashline 태그)이 붙습니다 — 방금 편집한 자리를 이어서 다시 편집할 때 `read_file` 재왕복 없이 이 ref 를 바로 쓸 수 있습니다(`write_file` 의 hashline echo 와 같은 목적). 줄번호는 편집 후 실제 파일 기준(절대)이고, 파일 대부분을 갈아엎은 편집은 이 블록을 상한(~100줄)까지만 내보내고 나머지는 `read_file` 로 안내합니다.

`edit_file` 성공 시 응답에 **변경 사항 unified diff** 가 포함됩니다 (`+` 녹색 / `-` 빨강, 100줄 초과 시 truncate). `write_file` 성공 시엔 작성한 content 가 **hashline(LINE#HASH:content)** 포맷으로 반환되어, `read_file` 없이 방금 쓴 파일을 바로 `edit_file` 로 수정할 수 있습니다 (write→edit 직결 — 작은 변경 시 전체 재작성 대신 부분 edit 유도). 또한 `write_file` 이 **기존 파일을 덮어쓰는데 바뀐 줄이 30% 미만**이면(작은 overwrite), 관찰에 *"~N% of lines changed … edit_file costs only the changed lines"* 넛지가 붙고 **echo 가 파일 전체 hashline 대신 `unified diff` + 바뀐 영역의 fresh hashline 태그(`Updated region` 블록)** 로 바뀝니다 — `edit_file` 성공 echo 와 **동일한 조립**(공용 `render_change_echo`)이라, churn 케이스의 컨텍스트 점유를 줄이면서도 "edit 했어야 할 줄"을 보여주고 **후속 `edit_file` 을 `read_file` 재왕복 없이** 바로 할 수 있는 ref 를 함께 줍니다(쓰기는 정상 수행, 거부 아님). 신규 파일·전면 재작성(≥30%)은 파일 전체 hashline echo 를 유지합니다(write→edit 직결용, diff 대상 없음).

### complete — 작업 완료

LLM이 작업을 완료했을 때 호출하는 가상 도구입니다. `result` 필드에 최종 답변을 담습니다.

```json
{"action": "complete", "action_input": {"result": "작업이 완료되었습니다. 파일을 생성했습니다."}}
```

**요청 회계 — `answers` (v9.16.0)**: 웹 세션에서는 런 하나가 여러 사용자 요청을 합칠 수 있습니다(런 도중 도착한 메시지는 턴 경계에서 주입). 그러면 `complete` 하나가 나가는데 **무엇에 답한 것인지** 아무도 몰랐습니다. 이제 각 요청은 id 를 갖고 프롬프트 꼬리의 `## Open Requests` 로 매 턴 보이며, `complete` 은 답한 id 를 `answers` 로 밝힙니다.

```json
{"action": "complete", "action_input": {"result": "A완료 / B완료", "answers": ["1", "2"]}}
```

`## Open Requests` 가 있는 런에서 `answers` 없이 완료하려 하면 — 요청이 하나여도 — **런당 한 번** 되돌려 다시 요구합니다(v9.22.0)(결과를 붙잡는 것이 아니라 같은 산출물의 형식만 고치게 하는 것이라, 완료가 다른 요청을 기다리지 않습니다). 되돌림 관찰이 결과 원문을 인용하므로 물린 `complete` 자체는 저장되지 않고, 성공하면 그 관찰도 컨텍스트에서 접힙니다 — 재시도는 기록하지 않습니다 (v9.21.2).

**미답이 남으면 런이 끝나지 않습니다 (v9.17.0)**. `answers` 가 일부만 가리키면 최종 답변은 **그대로 즉시** 나가고(붙잡지 않습니다 — 답은 이미 렌더되고 기록됩니다), 루프는 닫히지 않은 채 남은 요청을 가리켜 계속 일합니다. 주장된 요청은 회계에서 지워져 다음 턴 꼬리에서 사라집니다. 독촉은 **요청당 한 번**입니다 — 그래도 남으면 최종 답변 말미에 `⏳ … were not answered` 각주로 닫습니다. `answers` 를 아예 안 밝힌 경우는 무엇이 남았는지 알 수 없어 독촉하지 않고 `📋 … is undeclared` 로 적습니다.

**수신 유형별로 한 가지 규칙 (v9.22.0, v9.22.1 에서 코드도 하나로)**. 런이 `complete` 을 낼 때 하네스는 그 런이 **무엇을 받았는지**로 판정합니다 — main 이냐 상주 에이전트냐가 아니라. 루프는 원래 하나이고(`agent_cli/loop/`), v9.22.1 부터 회신 빚 장부도 하나입니다(레지스트리의 주소별 `ReplyDebt`) — main 은 배달된 `message` 를, 상주는 시작 항목을 같은 장부에 적고, 거부/독촉의 갈림은 "이 런에 사용자 요청이 있었는가" 뿐입니다. 그래서 main 의 🤝 웨이크 런(사용자 발화 없음)에 빚이 남으면 상주처럼 **거부**되고, 상주의 사람 창 런(사용자 요청 있음)에 빚이 남으면 main 처럼 **수락 후 독촉**됩니다. 질문을 건 런의 회신 억제는 **물어본 상대의 빚에만** 걸립니다(v9.22.2) — `ask(to="user")` 로 사람에게 물어도 원 요청자(main)에게 진 빚은 남아 독촉·폴백을 받습니다.

| 받은 것 | `complete` 이 오면 |
|---|---|
| 사용자 요청 (main 채팅 · 에이전트 대화창 입력) | 수락. `answers` 의 id 를 회계에서 지우고, 남은 요청은 독촉(요청당 한 번). `answers` 가 없으면 되돌림(런당 한 번). |
| 에이전트의 `message` / `ask` | 회신·답을 안 했으면 상주는 **거부**(결과 원문을 인용해 `reply(id=…)`/`answer(id=…)` 로 안내), main 은 **수락 후 독촉**. 3회까지 — 그래도 남으면 런은 끝나고 하네스가 정리합니다(질문은 "답 없이 런 종료" 로 닫히고, 회신 빚은 "답하지 않았다" 는 사실 문장으로 닫힘 — v10.177.8, 전엔 라벨 붙은 런 요약을 폴백 배달). |
| 둘 다 섞임 | 수락 → `answers` 의 요청 정산 → 남은 `message`/`ask` 독촉. |

에이전트 대화창에 사람이 직접 입력한 요청도 main 과 같은 회계를 받습니다: 각 입력이 id 를 갖고 `## Open Requests` 에 오르며, 여러 건이 한 턴에 합쳐졌을 때 `complete` 은 `answers` 로 무엇에 답했는지 밝혀야 합니다. 그 런에서 `reply` 는 여전히 거부됩니다 — 요청자가 창을 보는 사람이라 `complete` 의 결과가 그대로 창에 뜹니다.

**어떤 요청에 대한 답인지 보입니다 (v9.19.0)**. 최종 답변 아래에 `답한 요청 [2] Ann 이건 REQ-A 와 별개다` 처럼 주장된 요청이 칩으로 붙습니다(CLI 는 `↳ answers [u-2] (Ann) …` 한 줄). 합쳐진 런에서는 카드 위치가 알려주지 않습니다 — 요청 둘이 한 턴에 들어오고 최종 답변은 하나이기 때문입니다. `--resume` 후에도 같은 칩이 뜹니다.

**형식 설명 속 예시는 호출이 아닙니다 (xml_fc, v9.24.8)**: 모델이 도구 호출 형식을 설명하면서 `` `<function=NAME>` `` 처럼 태그를 인라인 코드로 적어도 호출로 읽지 않습니다 — 코드 블록(```)과 마찬가지로 인라인 코드(한 줄 안의 백틱 쌍) 속 후보도 인용으로 봅니다. 코드 밖에 진짜 호출이 없을 때만 인용 속 호출을 받아들이는 기존 관용은 그대로입니다. 한계: 코드 표시 없이 줄 첫머리에 적은 완전한 예시는 진짜 호출과 구분할 수 없습니다.

**종료는 명시적 `complete` 만 (v8.4.0)**: 모델이 도구 호출 없이 산문만 내면 — 그것이 최종답변처럼 보여도 — 완료로 수용하지 않고 재시도 넛지를 보냅니다. 넛지 문구가 "방금 산문이 최종답변이었다면 그 내용을 `complete` 의 result 로 재방출하라"고 직접 안내해 한 턴 안에 수렴합니다. (v7.14 의 산문-완료 자동 수용은 실사용에서 "Now let me write the plan document:" 같은 **전환 서술을 스킬 결과로 오인 종료**하는 반례가 발견되어 제거 — 완료 의도 추론은 본질적으로 불안정하고, 조용히 틀립니다.)

**형식 재시도는 기록하지 않습니다 (v9.23.2)**: 모델 출력에 도구 호출이 없거나 JSON 이 깨졌거나, 모르는 도구·틀린 인자를 보냈거나, 출력이 토큰 상한에서 잘렸을 때 — 그 실패한 출력은 컨텍스트에 저장하지 않고, 안내 메시지가 **앞뒤 일부만**(2000자 이하는 전문, 넘으면 머리 1400자 + 생략 표시 + 꼬리 600자) 인용합니다. 회복하면 안내도 접힙니다. 같은 도구를 같은 인자로 연달아 부를 때의 반복 경고(B1)도 같습니다(v9.23.4): 막힌 호출은 기록하지 않고, 한 턴에 여러 op 를 보낸 배치 안에서 걸리면 그 op 만 건너뛰고 나머지는 실행합니다.

**거부된 호출은 한 줄로 되비춥니다 (v10.37.0)**: 모르는 도구·틀린 인자로 거부된 호출은 저장되지 않으므로 거부 안내가 모델이 가진 유일한 사본입니다. 그런데 서버 네이티브 함수 호출에서는 인용할 산문이 없어 모델이 `Missing required field(s) for 'edit_file': path …` **만** 보고 — 자기가 뭘 보냈는지 비교할 것이 없어 — 같은 호출을 네 번 반복한 실측이 있었습니다(v10.36.0 회귀 런). 이제 원인 문장 뒤에 보낸 호출을 한 줄로 붙입니다: `You sent: edit_file(op="replace", pos="12#AB", lines=[1 item]) — missing: path. Re-emit the whole call with every required field.` 값은 짧은 것만 그대로이고(40자 이하), 긴 문자열은 `<2,341 chars>`, 배열·객체는 `[n items]`·`{n keys}` 로 줄여 본문을 되풀이하지 않습니다. 빠진 필수 필드와 스키마에 없는 키(`— unknown: cmd` — 대개 이름을 잘못 쓴 같은 인자)를 함께 적고, 배치 안의 거부 op 에도 같은 줄이 붙습니다.

**반복 경고는 직전 결과를 보여 주고, 회복되면 접힙니다 (v10.32.0)**: 같은 호출을 연달아 보내 막혔을 때 — ① 안내는 **그 호출이 직전에 돌려준 결과를 그대로 인용**합니다(2000자 상한, 넘으면 머리·꼬리). 실행 전에 거부된 호출(모르는 도구·인자 형식 오류)을 그대로 다시 보내는 건 결과를 본 적 없는 재시도라 반복 경고 대상이 아니고, 거부 안내를 다시 받습니다. 다만 같은 거부를 세 번 받고도 같은 인자를 보내면 4번째에 멈추는 상한은 그대로입니다(안내 턴은 턴 예산을 안 쓰므로 이것이 유일한 상한). ② `complete` 는 "과제 자체가 끝났을 때만, 반복을 끝내려고 부르지 말 것" 으로 안내합니다 — Harbor 실측에서 한 모델이 경고 두 턴 뒤 결함을 알면서 `complete` 한 사례가 있었습니다. 그리고 이 안내는 형식 재시도 안내처럼 **일회성**이라, 다음 턴이 통과하면(또는 더 센 2단계 안내가 붙으면) 컨텍스트에서 접힙니다. 실행된 호출과 그 결과는 그대로 남고, `history.jsonl` 에는 안내도 그대로 기록됩니다. 배치 안의 반복은 그 op 자리에 한 줄로 적히는 구조라 접지 않습니다.

**거절된 시도는 화면에도 그리지 않습니다 (v9.24.11)**: 형식 거절(`answers` 누락, 모르는 도구, 인자 형식 오류 등)된 시도의 "생각"과 행동 카드는 이제 화면에 나타나지 않습니다. 생성 중 줄의 "↻ 재시도 N"만 남습니다. 생각은 그 턴이 통과할 때까지 잠시 보류되며, 다음 출력 직전에 그려지므로 화면 순서는 그대로입니다. 도구가 **실행된 뒤** 실패한 경우(없는 파일 읽기 등)와 배치 안의 잘못된 호출(실패로 기록되고 나머지는 실행)은 실제 작업이라 보입니다.

### history — 세션 이력 조회

(v10.29.0 — 구 `read_context`. "context" 는 모델이 지금 들고 있는 것이고 이 도구는 거기서 빠진 것을 되찾으니 이름이 반대 방향이었습니다. 새 세션에서 "지난 세션의 …가 뭐였지?" 되묻기 3과제 × 3회로 `read_context`·`context_recall`·`recall`·`history` 를 비교: `history` 는 9/9 첫 op 에 바로 이 도구, 평균 2.8 op (다른 이름 4.1~4.8); `context_recall` 은 "recall" 이 `memory` 설명과 겹쳐 3/9 가 메모부터 뒤졌습니다. 별칭은 없습니다 — 프로파일의 `allowed-tools` 에 `read_context` 가 있으면 `history` 로 바꾸세요. 스키마 키도 다른 도구처럼 맨 `query`·`sessions` 입니다.)

이전 또는 현재 세션의 이력을 **SQL 로 질의**합니다. LLM이 context window 밖으로 evict/compaction된 정보가 필요할 때 자발적으로 사용합니다. history.jsonl 이 인메모리 `history` 테이블로 적재되고, LLM이 `SELECT` 를 작성합니다(읽기전용).

테이블 `history` (레코드 1개 = 1행): `session`, `loc`, `seq`, `kind`(query/action/observation/final/raw/system), `turn`, `ts`, `tools`(툴명), `files`(조작 파일 경로), `author`(닉네임), `text`(각 레코드의 전체 내용).

**결과는 잘림 없이 그대로 반환됩니다** (이전의 200자 셀 절단·공백 collapse 제거). 결과를 작게 유지하려면 직접 projection/limit 하세요 — 먼저 미리보기로 훑고(`SELECT loc, substr(text,1,200) … LIMIT 30`), 원하는 한 행만 전체(`SELECT text …`)를 가져옵니다. 과대한 결과는 통째 덤프되지 않고 **"좁히라"는 nudge 로 거절**됩니다(아래 과대 출력 캡 참조).

```json
// 스키마 + 예시 + 세션 목록 보기 (query 생략)
{"action": "history", "action_input": {}}

// auth.py 를 건드린 관측만 — 먼저 미리보기로 훑기
{"action": "history", "action_input": {"query": "SELECT loc, turn, substr(text,1,200) FROM history WHERE kind='observation' AND files LIKE '%auth.py%' LIMIT 30"}}

// 특정 사용자(웹 멀티유저)의 질문만
{"action": "history", "action_input": {"query": "SELECT text FROM history WHERE kind='query' AND author='두정'"}}

// 키워드 + 턴 범위 + 정렬/제한
{"action": "history", "action_input": {"query": "SELECT loc, text FROM history WHERE text LIKE '%인증%' AND turn>=5 ORDER BY turn LIMIT 20"}}

// 다른 세션까지 — sessions 로 적재 범위 지정(all 또는 특정 id)
{"action": "history", "action_input": {"query": "SELECT DISTINCT session FROM history", "sessions": "all"}}
```

**읽기전용**: `SELECT`/`WITH` 만 허용(쓰기·DDL 거부). 행/셀 수 캡은 없습니다 — 결과를 작게 유지하는 건 모델 몫(`LIMIT`/`substr`/조건). `sessions` 미지정 시 현재 세션(run/skill subdir 포함).

### 정보원 목록 (`## Web Sources`, v10.57.0)

`fetch` 가 있는 루프는 매 턴 꼬리에 **브라우저 없이 바로 답하는 웹 정보원** 목록을 싣습니다 — 검색 엔진(막히면 `Fetch failed (bot-check)` 로 알리고 browser 로 넘어감), Wikipedia API, PyPI·npm·deps.dev, GitHub raw·API, Stack Exchange·HN, OSV·NVD, arXiv·OpenAlex, Open-Meteo·Frankfurter, Wi-Fi 개발(hostap·kernel wireless-next·lore linux-wireless·Android googlesource·Gerrit). 기본 18항목이 전부 켜져 있습니다(약 460토큰/턴).

- **웹**: 헤더 🌐 서랍에서 그룹별 체크박스로 끄고 켜고, 사내 Gerrit 같은 주소를 추가·삭제합니다. 바꾸면 다음 턴부터 반영됩니다.
- **CLI**: `.agent-cli/web-sources.json` 을 직접 고칩니다. **바꾼 것만** 적습니다 — 파일이 없으면 기본 전부 켜짐이고, 다음 버전에서 기본 정보원이 늘거나 주소가 바뀌어도 그대로 따라갑니다.

```json
{"disabled": ["vulns.nvd"],
 "added": [{"id": "user-1", "group": "wifi", "text": "gerrit.mycorp.com/changes/?q=… — 사내 Gerrit"}]}
```

`id` 는 기본 항목(`search.engines`·`wiki.summary`·`vulns.nvd`·`wifi.hostap` …, `agent_cli/default_web_sources.json`)이거나 직접 정한 이름, `group` 은 12자·`text` 는 500자까지. 꼬리에 두는 이유는 실측입니다 — 8분야 과제 × 3회 × 4변형 A/B 에서 꼬리판 24/24·첫 호출이 목록 22/24, 같은 목록을 `fetch` 도구 설명에 둔 판은 22/24·8/24(주소의 `/v1` 을 빠뜨린 채 지어내 같은 호출을 51번 되풀이)였습니다.

### 브라우저 도구 (`browser`)

JavaScript 가 그리는 페이지, 버튼을 눌러야 나오는 내용, 폼 입력이 필요하면 `browser` 를 씁니다 (정적 HTML 은 `fetch` 로 충분). 창 없이(헤드리스) Chrome/Chromium 을 띄우고, 페이지를 **접근성 트리 텍스트**로 돌려줍니다 — 버튼·링크·입력칸·랜드마크마다 `[ref=eN]` 이 붙고, 모델은 그 번호로 조작합니다. 설계와 근거는 `docs/browser/DESIGN.md`.

```bash
pip install 'agent-cli[browser]'      # playwright (Node.js 를 따로 깔 필요 없음 — 드라이버 동봉)
# 브라우저 엔진: 설치된 Google Chrome 을 쓴다. 없으면:
playwright install chromium
```

Playwright 는 **1.49 이상**이어야 합니다(페이지를 `Locator.aria_snapshot` 으로 읽는데 1.49 부터 있음, v10.112.1). 더 낮으면 도구는 그대로 보이고, 첫 호출이 설치된 버전과 `pip install -U 'playwright>=1.49'` 를 알리며 거절합니다.

playwright 가 없거나 `AGENT_CLI_BROWSER_TOOL=0` 이면 **도구가 프롬프트에 나오지 않습니다**(`snapshot` 과 같은 규칙 — 없는 기능을 약속하지 않음). 브라우저는 첫 호출 때 뜨고(안 쓰는 세션은 비용 0), 세션이 끝나면 닫힙니다.

| mode | 인자 | 하는 일 |
|---|---|---|
| `open` | `url` | 이동 → 헤더(URL·제목) + 스냅샷(큰 페이지는 예산 안의 깊이까지, 아래) |
| `snapshot` | `ref?`, `depth?` | 페이지(또는 `ref` 랜드마크 아래)의 트리 — 긴 텍스트는 80자에서 자름 |
| `find` | `text` 또는 `regex` | 스냅샷에 보이는 모양(`link "Aurora Lamp"`)으로 맞는 노드만, 조상 경로와 함께 |
| `text` | `ref?` | 읽을 글만 (본문·기사 — 스냅샷 대신 이걸로) |
| `click` / `type` / `select` / `press` | `ref` / `ref, text, submit?` / `ref, value` / `key` | 조작. 다른 페이지로 가면 새 스냅샷이, 같은 페이지에서 트리가 바뀌면(영역이 펼쳐짐·드롭다운 값) `same page, changed in place` 와 새 스냅샷이 결과에 포함됨(둘 다 `open` 과 같은 예산). 비교 전에 화면 갱신 두 프레임을 기다리고, 그대로면 200 ms 뒤 한 번 더 읽음. 그래도 그대로면 그렇다고 한 줄로 알림 — 캔버스·그림의 변화는 `screenshot` 만 보여 줌 (v10.106.0) |
| `screenshot` | `full_page?` | 뷰포트(또는 문서 전체)를 PNG 로 `.agent-cli/tmp/screenshots/` 에 저장하고 **이미지로 보여 줌**(v10.61.0 — 모델이 이미지를 보면; 트리가 못 말하는 배치·차트·그림용) |
| `back` / `close` | — | 뒤로 / 종료 |

- **ref 는 페이지 로드 단위**로 유효합니다. 번호는 세션 내내 올라가기만 해서 옛 번호의 뜻이 바뀌지 않고, 페이지가 바뀐 뒤 옛 번호를 쓰면 "stale — take a new snapshot" 으로 거부됩니다. 같은 페이지 안에서는 DOM 이 바뀌어도 그 요소가 남아 있는 한 계속 맞습니다(ref = role + 이름 + 순번 → Playwright `get_by_role().nth()`; 공개 API 만 씀).
- **큰 페이지**: 이동·조작 뒤 자동으로 붙는 스냅샷은 **과대 출력 캡(창의 1/10)의 1/4 안에 드는 깊이까지만** 보이고, `[snapshot cut to depth D: n of N nodes …]` 한 줄이 나머지를 `find`·`text`·`snapshot(ref=)` 로 안내합니다(숨은 요소도 ref 가 있어 `find` 로 찾은 번호를 바로 누를 수 있음). 명시적 `snapshot` 은 전체이고, 그 결과가 캡을 넘으면 다른 도구처럼 [과대 출력 캡](#과대-출력-캡-oversized-observation)을 탑니다. 좁히는 길은 `snapshot(ref=<랜드마크>)`·`depth`·`find`·`text` — 안내 문구가 이를 가리킵니다.
- **로컬 파일 (v10.99.0)**: `file:///절대/경로` 나 맨 경로(`report.html`·`./out/index.html`·`~/x.html`)도 엽니다 — 만든 HTML 보고서·문서를 JavaScript 까지 돌려 보는 길. 맨 경로는 작업 폴더 기준 절대 `file://` 로 바뀌고, 없는 파일은 한 줄로 거절합니다. 읽기 규칙은 `read_file` 과 같습니다: 작업 폴더 밖도 열리고, 자격증명 경로(`~/.ssh` 등)만 사용자에게 묻습니다. 페이지 안 링크로 그런 경로에 닿으면 빈 페이지로 돌리고 거절합니다. 호스트 허용 목록은 로컬 파일에 적용되지 않습니다. 한계: 스냅샷·`text` 는 iframe 안을 읽지 않지만 `screenshot` 에는 iframe 이 그려집니다.
- **안전**: 코드 실행 모드 없음. `http(s)` 와 로컬 `file:` 만(`javascript:` 등 거부). `AGENT_CLI_BROWSER_HOSTS` 허용 목록 — 이동·클릭이 목록 밖 호스트로 끝나면 빈 페이지로 돌리고 거절. `alert`/`confirm` 은 자동으로 닫고 결과에 한 줄 남김. 다운로드 거부. 프로필은 메모리 전용(세션이 끝나면 쿠키·로그인 소멸). **결제·전송·삭제처럼 되돌릴 수 없는 동작은 하니스가 구분하지 못합니다** — 로그인된 사이트에는 쓰지 말고, 허용 목록으로 갈 수 있는 곳을 묶으세요.
- **사람 확인(차단·CAPTCHA)**: 구글의 "비정상적인 트래픽" 차단이나 DuckDuckGo 의 챌린지처럼 사이트가 사람 확인을 요구하면, 도구는 스냅샷 대신 `[verification needed] …` 를 돌려주고 **보이는 브라우저 창을 띄웁니다**(`AGENT_CLI_BROWSER_HEADLESS=auto`, 기본; 쿠키를 옮겨 같은 세션). 모델은 `ask` 로 사용자에게 묻고 멈추며, 사용자가 그 창에서 확인을 마치고 답하면 같은 창에서 이어 갑니다(세션 끝까지 보이는 채로). 챌린지 페이지에서의 클릭·입력은 거부됩니다. 창을 못 띄우는 환경(서버·CI)이나 `=1` 이면 `[blocked] …` 로 끝나고, 모델은 다른 출처나 공식 API 를 쓰거나 사용자에게 묻습니다. **우회·위장(UA 변조, 자동 풀이)은 하지 않습니다.** 검색 엔진은 자동화를 자주 막으니, 아는 사이트로 바로 가거나 `fetch` 를 쓰는 편이 낫습니다. 보이는 창을 닫아 버려도 다음 `open` 이 새 창을 띄워 이어 갑니다(쿠키·로그인은 사라지며, 관찰 머리에 한 줄 알립니다, v10.41.1).
- **동시성**: 세션당 브라우저 하나를 전용 스레드가 소유하고 모든 호출(서브에이전트 포함)을 직렬화합니다. 시간 제한은 이동 30초, 조작 10초.
- 실측·비교(Playwright MCP 대비 기본 비용·턴 수)는 릴리스 노트와 `docs/browser/DESIGN.md` §7.

### 과대 출력 캡 (oversized observation)

도구 관찰(observation)이 **컨텍스트 윈도우의 1/10** 을 넘으면 그 출력은 컨텍스트에 들어가지 않습니다(도구 호출 자체는 성공). 거대 출력은 추론 공간을 잠식해 응답 품질을 떨어뜨리기 때문입니다. 대신 **모든 도구가 정확히 같은 모양의 관찰**을 받습니다:

1. **전문은 항상 파일로** — 도구가 이미 파일에 썼으면 그 파일(`read_file` 의 원본 경로), 아니면 `<session_dir>/oversized/<tool>-<hash>.txt` 에 **lazy 저장**(캡에 걸릴 때만 — 일반 호출엔 디스크 쓰기 0). 세션 디렉터리가 없는 headless 실행은 임시 디렉터리로 폴백하므로 "넘치면 파일에 남는다"가 예외 없이 성립합니다.
2. **head/tail 발췌 동봉** — 앞뒤 일부(캡의 15% 이내로 클램프)를 그대로 보여줍니다. 빌드/테스트 로그처럼 **답이 꼬리에 있는** 경우가 많아, 상당수는 추가 왕복 없이 그 자리에서 끝납니다.
3. **회수 경로 3종** — ① read_file 에 `search`(정규식) 매칭, ② read_file 에 `line_start`·`line_end` 범위 읽기(v10.77.0 부터 넛지는 호출 모양 대신 문장으로 말하고, read_file 이 없는 루프엔 ①②를 싣지 않습니다; read_file 자신의 과대 결과는 저장본이 아니라 원본 파일을 "The file is …" 로 가리킵니다), ③ **분할 정복**: 파일을 캡에 맞는 `k` 섹션으로 쪼개 **한 턴에 `agent(mode="run")` op 를 k 개**(agent-cli 가 동시 실행) 내고 각자 요약만 반환 → 부모는 distilled 만 병합. `k` 는 16 과 **지금 더 띄울 수 있는 에이전트 수**(살아 있는 에이전트 상한 − 살아 있는 수) 중 작은 것이고, 자리가 없으면 ③ 을 싣지 않습니다(v10.177.7 — 전엔 상한 10 인데 16 개를 권해 넘는 것이 거절됐다). ③ 은 `agent` 가 실제 호출 가능할 때만 표시됩니다(depth 한계 서브에이전트에선 자동 생략).
4. **다음 번 한 줄 (`Next time —`, v10.77.0 전엔 `Root cause —`)** — 애초에 벌크를 만들지 않는 법. 도구별로 유일하게 다른 부분입니다: `shell` → 명령에 `grep`/`head`/`tail`, `read_file` → `search`/범위, `fetch` → 더 좁은 URL·얕은 `depth`, `agent` → 더 좁은 task, `code_index` → 단일 심볼 fetch·`search` 필터·`max_bytes`, `history` → `LIMIT`/projection/`substr`.

- **도구별 제어 (`Tool` 표면)**: `Tool.render_observation(result, args)` 가 결과 → 관찰 본문 렌더(기본=성공 output·실패 error); `Tool.apply_oversized_cap`(기본 `True`)으로 캡 적용 여부를 끔; **`Tool.oversized_retry_hint`**(위 4번 한 줄)와 **`Tool.oversized_source_path()`**(이미 디스크에 있는 전문의 경로)만 도구가 정의합니다. `Tool.render_oversized()` 는 **어떤 도구도 오버라이드하지 않습니다** — 정책이 한 곳에 있으므로 MCP 서버 도구를 포함해 새 도구가 아무 것도 안 해도 올바르게 동작합니다. per-call 루프 컨텍스트는 **`RunContext`**(frozen: `session_dir`·`oversized_cap`·`tools_available`) 하나로 묶여 실행(`run`/`_run`)·렌더 두 표면에 동일하게 전달됩니다.
- **경로는 작업 디렉토리 상대로 표시**: 넛지가 돌려주는 파일 경로는 모델이 다음 호출에 그대로 받아쓰는 인자가 되고, `action_input` 은 그 뒤 매 턴 재공급됩니다. 그래서 `display_path()` 가 작업 디렉토리 아래에 있는 경로를 상대경로로 줄입니다 — `/Users/…/ws/b5rztq/.agent-cli/sessions/…/oversized/shell-3f9ac2.txt` → `.agent-cli/sessions/…/oversized/shell-3f9ac2.txt`. 한 넛지 안에 경로가 3~4번 등장하므로 절약이 배가됩니다. headless 임시 디렉토리는 작업 디렉토리 밖이라 절대경로를 유지합니다.
- **캡 하한**: `max(context_window, MIN_CONTEXT_WINDOW) // 10`. capabilities 미탐지로 윈도우를 모를 때도 캡이 0(=무제한)이 되지 않습니다.
- **턴 단위 예산**: 위 캡은 op 하나에 대한 것이라, 각각 캡 바로 아래인 op 이 10개면 합계가 윈도우를 채웁니다(그리고 시스템 프롬프트는 독립 파일 읽기를 한 턴에 몰아 내라고 권장합니다). 한 턴의 관찰 누적이 **캡 × 3** 을 넘으면 그 이후 op 의 관찰이 같은 정책(파일+발췌+경로)으로 치환됩니다 — 모델이 먼저 요청한 앞쪽 op 은 원문 그대로 남고 넘치는 뒤쪽이 포인터로 강등됩니다. 이때 nudge 는 "이 결과가 컸다"가 아니라 "이 턴이 예산을 넘었다"고 정직하게 말합니다(고치는 법이 다르므로).
- `run_skill` 도 다른 도구와 같은 seam 을 지납니다(스킬은 서브루프 전체 출력을 반환하므로 단일 관찰 중 가장 커질 수 있습니다).
- 스킬 템플릿의 `` !`cmd` `` 주입은 관찰 seam 을 지나지 않으므로 자체 상한(4,000자, head+tail 보존)을 가집니다.
- 사용자/어시스턴트 메시지는 캡 대상이 아닙니다(사람의 의도적 입력·모델 자신의 출력이라 거절/절단하지 않음).

### code_index — SQLite 코드 인덱스

tree-sitter로 프로젝트 전체를 파싱해 `<project_root>/.agent-cli/code_index.db`에 영구 SQLite 인덱스를 만듭니다. 첫 query 시 lazy build, 이후 query마다 변경 파일만 incremental rebuild — v10.93.0 부터 파일을 읽기 전에 크기·mtime 으로 먼저 가르고, 바뀐 것이 없으면 아무것도 쓰지 않습니다(agent-cli 저장소 3,212 파일: 쿼리·편집 훅 4.0 s → 0.35 s; 한 파일 변경 때는 그 파일만 파싱하고 나머지 행은 옛 DB 에서 SQL 로 복사, django 2,633 파일 3.05 → 1.25 s). v10.94.0 부터 `callers`·`callees` 는 물어본 이름만 SQL 로 답합니다(이름당 평균 0.3 ms, 최악 ~90 ms). 출력은 한 글자도 같습니다(함수 이름 전부 비교). v10.94.1 부터 `slice` 도 색인 전체를 올리지 않습니다 — 기호는 그 이름만, 타입·매크로는 대상 범위 안 참조만 묻습니다(`as_sql` 의 `with_callees` 1.5 s → 수십 ms, 출력 동일). v10.95.0 부터 안쪽 함수(클로저)가 끝난 뒤 바깥 함수 본문에서 일어난 호출도 바깥 함수의 호출로 셉니다(Python 만 해당). `edit_file` / `write_file` 성공 시 자동 post-hook이 인덱스 갱신 — 그래서 수동 재구축 모드(`build`)는 v10.92.0 에 지웠습니다.

**모델이 보는 글 (v10.92.0, docs/tool-review/TRACKER.md)**: 안내 블록 없이 도구 설명(이름 규칙·확장자)과 `mode` 인자 설명(모드마다 한 문장)만 — 1,689 → 683 토큰(GLM). 빈 결과는 색인의 범위를 말합니다("No symbol named \"X\" among the 1,732 indexed symbols (412 files under /repo)", 색인에 파일이 없으면 "The index has no files: nothing under /app has a supported extension.") — 벤치에서 lookup·refs 가 전부 빈 결과였는데 색인이 비어 있다는 사실은 어디에도 없었습니다. 오류 문구는 표기 규칙대로(`` `mode` "x" is not one of "list", … ``).

`read_file`이 텍스트(line range)에 답한다면 `code_index`는 의미 단위(symbol)와 cross-file 관계(refs/callers/callees)에 답합니다.

#### 이름 해석은 색인에 (v10.96.0, 스키마 4)

색인을 만들 때 참조마다 그 이름이 무엇을 가리키는지 한 번 풀어 저장합니다(docs/code-index/DESIGN.md §21). 그래서 `callers`·`callees` 는 이름이 아니라 **정의** 단위입니다 — 두 클래스의 `get` 은 서로 다른 노드이고, 테스트의 `_write` 헬퍼가 `fsio._write` 의 호출자로 섞이지 않습니다.

- 해석 결과는 다섯 가지입니다: `resolved`(색인된 정의 하나), `local`(인자·지역 변수·클래스 속성 — 호출 관계가 아님), `candidates`(이름으로만 맞춤 — 타입을 모르는 객체의 메서드 호출 등), `external`(내장 함수·색인 밖 모듈), `unresolved`.
- **Python** 은 범위 규칙을 그대로 따릅니다: 함수 안 어디서든 대입된 이름은 그 함수 전체에서 지역, 클래스 본문은 메서드에서 보이지 않음, `global`/`nonlocal`, 모듈 본문은 위에서 아래로, `import` 는 파일까지 따라감(상대 import, `import *`, 패키지 재노출, `src/`·`tests/` 같은 소스 루트 자동 인식), `self.m()`·`cls.m()` 은 그 클래스와 부모 클래스에서, `mod.f()`·`Cls.m()` 은 체인으로.
- **C** (v10.98.0, DESIGN §22) 는 블록 → 함수 → 파일 순으로 찾습니다: 인자·지역·블록 지역은 `local`, 파일 안의 정의(`static` 이든 아니든, 매크로 포함)는 그 정의, 파일에 없는 이름(헤더 프로토타입·`extern`)은 **다른 파일이 밖으로 보이는 정의** — 헤더(`.h`)의 모든 정의(static inline·매크로·타입·열거자 — 다만 `#define u8 unsigned char` 같은 타입 별칭 매크로와, 타입 자리의 매크로는 같은 파일 것만)와 `.c` 의 비-static 함수·변수 — 에서 하나면 그것, 여럿이면 `?`, 없으면 `external`. `#include` 는 따라가지 않습니다(헤더 정의를 포함하는 누구에게나 보인다고 두는 것이 그 대가). 그래서 파일마다 있는 같은 이름의 `static` 함수가 서로의 `callers` 에 섞이지 않습니다. 태그(`struct foo`)·typedef·보통 이름은 이름공간이 따로라 `struct nic *nic` 의 둘이 섞이지 않습니다. `p->open()` 같은 함수 포인터 호출은 `?`(그 이름의 C 함수가 있을 때). `.ndo_open = e1000_open`·`{ f, g }`·`&f`·캐스트로 이니셜라이저에 넣은 함수는 참조 종류 **`registration`** — 함수 밖의 ops 표에만 등록된 함수는 `callers` 에 `registered in a table: e1000_main.c:1012 (.ndo_open)` 줄이 붙습니다. 줄 순서가 다른 `#undef`/`#define` 재정의는 그 줄 이전의 마지막, `#if`/`#else` 로 갈린 같은 이름 정의는 평가하지 못해 뒤쪽 것을 고릅니다.
- **다른 언어**(C++·Go·Rust·Java·JS·TS)는 아직 범위 규칙이 없어 함수 범위와 인자만 압니다 — 결과 머리에 "matched by name only" 가 붙습니다.
- 출력 예:

```
get — a.py:1-2
caller  (1×)  b.py:5

Box.get — a.py:6-7
Box.use  (1×)  a.py:10

? "get" on an object of unknown type — any of 2 definitions:  b.py:6
```

  맨 이름은 그 이름의 정의마다 한 덩어리(최대 5), 한정 이름(`Box.get`)은 하나만. `?` 줄은 정의 하나로 고를 수 없던 자리입니다 — 타입을 모르는 객체에서 부른 것(`on an object of unknown type`), C 에서 여러 파일이 정의한 이름(`is defined in several places (includes are not followed)`; `.c` 의 `static` 정의에는 붙지 않습니다). `callees` 의 `?` 이웃도 C 의 그런 이름엔 `defined in several places` 가 붙습니다.
- **정의 고르기** (v10.97.0): `callers`·`callees`·`refs`·`slice` 에서 `path` 는 그 파일의 정의를, 모듈로 한정한 이름(`fsio._write`·`pkg.other._write`·`query.QuerySet.get`)은 그 모듈의 정의를 고릅니다 — 감싼 함수·클래스는 건너뛰어 써도 됩니다(`fsio._write` 가 `agent_cli/fsio.py` 의 `atomic_write_text._write`). 그 파일에 없으면 어디에 있는지를 오류로 알려 줍니다.
- **`refs` 는 줄마다 무엇으로 풀렸는지** 붙입니다 (v10.97.0): `→ QuerySet.get`(같은 한정 이름이 여러 파일에 있으면 `@파일`), `→ ?`(타입 모를 객체 — 그 이름의 정의 수는 끝에 한 줄), `→ local`·`→ external`·`→ unresolved`. 정의 하나를 고르면 그 정의로 풀린 참조만, 그리고 그것일 수도 있는 `?` 자리의 수를 한 줄로. `slice` 는 따라가지 않은 `?` 자리의 수를 적습니다.
- 정확도: django 에서 1,000 자리를 jedi 와 대조 — `resolved` 454 자리 중 jedi 가 확인한 것 전부 일치, `candidates` 64 자리 전부 jedi 의 답을 포함.
- 증분: 파일마다 해석하면서 읽은 것(다른 파일·모듈 이름·함수 이름)을 색인에 적어 두고, 다음 빌드는 그 가운데 바뀐 것이 있는 파일만 다시 해석합니다 — 전체를 다시 만든 것과 같은 답.
- C 정확도·비용 (v10.98.0): Lua 5.4.7 에서 clang AST 가 가리킨 정의 파일과 대조 — 정의가 있는 자리 3,214 중 3,205 일치(나머지는 `LUA_API size_t (lua_rawlen)` 처럼 매크로가 앞에 붙은 선언을 tree-sitter 가 잘못 읽은 7, 다른 헤더의 같은 이름 매크로로 `?` 2), 매크로 사용 5,374 자리는 `clang -E -dD` 로 살아 있는 `#define` 과 대조해 4,729 일치·551 `?`(같은 이름을 두 헤더가 정의)·90 `#if` 갈림. 커널 `drivers/net/ethernet/intel`(387 파일): 전체 빌드 5.1 → 5.9 s, 색인 55 → 46 MB(같은 자리의 call+name 중복 3만·정의 이름의 자기 참조가 빠졌다), 맨 호출 `?` 30,104 → 7,972, 편집 다섯 종류 증분 0.6~0.8 s 모두 전체와 같음.
- 비용: django(2,633 파일) 전체 빌드 7.4 s·색인 99 MB, 본문만 고친 편집 1.1 s, 새 파이썬 파일 1.1 s, 많이 import 되는 모듈에 함수 추가 1.1~1.2 s(1 파일 다시 해석), 가장 많이 import 되는 이름을 다시 묶는 편집 1.7 s(325 파일), `Model` 에 메서드를 더하는 편집 1.8 s(299 파일). 스키마 4 라 기존 색인은 처음 한 번 전체를 다시 만듭니다.

#### 한 op = 한 query (flat-native)

code_index 는 읽기 전용(파일 안 씀)이고, **한 op 가 한 query**를 돌립니다 (flat). 여러 query 는 멀티-op 으로 code_index op 을 여러 개 보냅니다(읽기전용이라 순서/상태 의존 없음 — 모드 섞기 OK).

```json
// 단일 query
{"action": "code_index", "action_input": {"mode": "fetch", "path": "agent_cli/loop.py", "name": "AgentLoop._call_llm"}}
```

#### 9 mode (각 op 의 action_input)

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

C/C++ 코드의 `#define` / `#ifdef` 분기 처리는 **번들된 pure-Python `_unifdef.py`** 가 기본 수행합니다 — 별도 설치 불필요. 시스템에 `unifdef` 바이너리가 있으면 (`brew install unifdef` / `apt install unifdef`) 자동으로 그것을 우선 사용 (battle-tested C 구현).

**기호가 들어간 조건은 같은 결과** — ifdef/elif/else/endif + `defined()`/논리/비교/산술 표현식에서 두 백엔드는 byte-identical 출력 (parity 테스트로 보장). **다른 점 하나: 상수만으로 된 조건**(`#if 0`, `#if 1 … #else`). 시스템 `unifdef` 는 기본적으로 이런 줄을 그대로 두고, 내장 구현은 계산해서 해당하지 않는 쪽을 빈 줄로 지웁니다. 그래서 `#if 0` 안의 죽은 코드는 시스템 백엔드에서만 색인됩니다. 줄 번호는 양쪽 모두 보존됩니다. 이 차이도 테스트로 고정돼 있습니다. 시스템 `unifdef` 가 파싱에 실패하면(예: 짝이 맞지 않는 `#elif`) 그 파일은 자동으로 내장 구현이 처리합니다.

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

**커널 버전 분기 — 기본은 최신 (v9.24.9)**: `#if LINUX_VERSION_CODE >= KERNEL_VERSION(…)` 분기는 defconfig 에 버전이 없으면 `LINUX_VERSION_CODE` 를 **최신(0xffffff = 255.255.255)** 으로 가정해 최신 분기 하나만 색인합니다. 두 분기를 모두 남기면 분기마다 블록을 여는 함수(`#if … if (a) { #else if (b) { #endif … }`)는 중괄호가 어긋나 **함수 통째로** 색인에서 빠지기 때문입니다. 대신 옛 커널용 호환 코드(`< KERNEL_VERSION(…)` 분기)는 색인에서 빠지니, 특정 커널을 다룬다면 버전을 적으세요:

```
// .agent-cli/defconfig — 대상 커널 6.1.0
#define LINUX_VERSION_CODE 393472
```

값은 `(주<<16) + (부<<8) + 수` (셸: `echo $(( (6<<16) + (1<<8) + 0 ))`), 16진수 `0x060100`, `KERNEL_VERSION(6, 1, 0)` 모두 됩니다. 빌드한 커널 트리의 `include/generated/uapi/linux/version.h` 를 그대로 붙여 넣어도 됩니다(`KERNEL_VERSION` 매크로 정의 줄은 무시). `#undef LINUX_VERSION_CODE` 는 두 분기를 모두 남깁니다. 기본값은 조건 지시문에 `LINUX_VERSION_CODE` 가 있는 파일에만 unifdef 를 돌리므로 커널과 무관한 C 프로젝트의 색인 결과·속도는 그대로입니다(실측: 커널 `drivers/net` 5.8천 파일에서 기호·참조 동일, 시간 차 1% 이내). `mode='build'` 출력에 가정한 값이 표시됩니다.

파일은 사용자가 쓰거나, 모델이 씁니다(v10.13.0 결정 — 모델에게 "사용자에게 요청하라" 가 아니라 "써라" 라고 안내). **안내가 나오는 자리 (v10.76.1)**: 이 사용법은 도구 가이드에 없고, `lookup`/`fetch` 결과가 **C/C++ 선언뿐이고 defconfig 가 없을 때만** 그 관찰 끝에 한 줄(`[Only declarations matched. … Writing .agent-cli/defconfig …]`)로 붙습니다. `code_index` tool은 첫 query 시 이 파일이 있으면 자동으로 unifdef 에 전달합니다 — 별도 옵션 불필요. `mode='build'` 출력에 `defconfig:` 라인으로 적용 여부가 보입니다.

C/C++ 사용자도 시스템 `unifdef` 설치 선택 사항 — 번들된 pure-Python 으로 동일 동작. Python/JS/TS/Go/Rust/Java/Markdown 만 쓰는 사용자는 전처리 단계 자체가 no-op.

### ask — 사람에게 질문 (v10.176.0: 누가 묻든 한 길)

질문 하나에 호출 하나입니다. 여러 질문은 한 턴에 `ask` 를 여러 번 부릅니다.

```json
{"action": "ask", "action_input": {"question": "legacy 컬럼은 버릴까요, 남길까요?"}}
```

**`ask` 는 막지 않습니다** — main·상주·일회용 에이전트·스킬 모두, 질문을 사람의 ❓ 트레이(와 터미널 질문 줄)에 올리고 즉시 돌아옵니다(관찰 `question "q-2" sent — "…"`). 어느 뷰어가 답하든 그 답은 묻는 쪽에게 줄 없는 통지 봉투로 갑니다 — 장부 항목이 아니라 `complete` 의 `answers` 로 닫을 것이 없습니다:

```
── answer [n-3] from the user re [q-2] ──
Your question: legacy 컬럼은 버릴까요, 남길까요?
Answer: 버리세요
(Continue the work it was blocking.)
```

사람이 답 없이 닫으면(`Close without answer`·`/close q-2`) `── closed [n-4] from the user re [q-2] ──` · `Your question: …` · `(The user closed it without an answer. Go on with what you have, and say what you assumed.)` 가 갑니다. 묻는 쪽이 한가하면 그 봉투가 새 작업을 열고, `wait` 로 기다리는 중이면 그 자리에서 깨웁니다. 질문이 열려 있는 동안 묻는 쪽의 꼬리에 `## Your open questions (…)` 가 섭니다(v10.175.0). 스킬 런은 자기 주소(`skill:…`)로 묻고 `wait` 로 답을 기다릴 수 있으며, 스킬 런이 끝나면 그 런이 건 질문은 닫힙니다. **v10.77.0 부터 질문은 언제나 사람에게** 갑니다 — main 이나 다른 에이전트에게 무언가 필요하면 `message` 를 보냅니다 — 그건 요청이고 상대가 `reply` 를 빚집니다.

사람은 독촉받지 않습니다 — 답을 안 하면 질문은 트레이에 남습니다. 보드의 "답변 필요" 는 열린 질문이 있는 동안 섭니다(v10.176.0).

### shell — 셸 명령 실행

셸 명령을 실행하고 stdout/stderr를 반환합니다(stderr 는 `[stderr]` 아래, 0 이 아닌 종료 코드는 `[exit code: N]`). 타임아웃 기본 120초가 상한이기도 합니다(v10.91.0) — 더 큰 `timeout` 은 명령을 백그라운드 작업으로 띄우고 결과에 그 사실을 적습니다. `timeout` 은 1초 이상이어야 하고 0·음수는 거절됩니다(v10.177.7 — 전엔 시작하자마자 시간 초과로 끝났다). 상한에 닿으면 그 명령이 띄운 프로세스까지 끝내고(v10.77.0 — `apt-get` 같은 자식도), 그때까지의 출력 끝 8줄과 함께 "`background` true 로 다시 보내라"고 알려 줍니다. 실행기는 bash 가 있으면 bash(v10.91.0).

```json
{"action": "shell", "action_input": {"command": "find agent_cli -name '*.py' | wc -l"}}
```

shell 출력은 자르지 않고 그대로 LLM에 전달됩니다. `find /` / `grep -r` 같은 큰 명령을 호출하면 컨텍스트가 그만큼 차지되니, 필요한 부분만 받도록 좁히는 명령을 권장 (`tail -n 100`, `grep ERROR`, `head -c 4096` 등). 누적 컨텍스트가 압축 목표치(창 × 압축 비율 − 시스템 프롬프트 − 세션 상태; 꼬리의 `context:` 줄이 이 목표치 대비 퍼센트)를 **넘으면** compaction이 발동해 오래된 절반을 LLM 요약으로 흡수하고, 그 단계에서도 안 들어가면 플레인 FIFO로 떨어뜨립니다.

**위험 명령 확인 (v10.177.0).** `rm` / `rmdir` / `mv` 가 명령에 포함되면 실행 전에 사람에게 묻습니다. 확인은 번호 항목 `c-N` 이고(질문 `q-N` 과 같은 장부·같은 번호 공간), 그 도구 호출은 그 자리에서 답을 기다립니다. 터미널에는 세 줄이 찍히고 **확인을 유발한 위험 키워드만 강조**됩니다(CLI 는 볼드-레드, 웹 트레이는 `.danger` 스팬):

```
⚠ [main → user] c-3  Dangerous command detected (`rm`)
  $ rm -rf build/            ← 'rm' 강조
  approve with: /approve c-3 · always: /allow c-3 · deny with: /deny c-3 <reason>
```

강조 범위는 서버에서 계산한 문자 span 이라 CLI·웹이 같은 소스를 칠하며, 유발 토큰만(정확히 그 명령을 위험하게 만든 `rm`) 표시됩니다 — `rm-helper.sh` 같은 유사 문자열은 강조하지 않습니다. 답은 번호를 적은 명령 셋과 웹 트레이의 버튼(`once` · `deny` · `always …`)뿐입니다 — 그 밖의 입력(`y` 포함)은 언제나 main 의 새 요청입니다. 명령 뒤의 글은 **선택적 코멘트**입니다:

- `/approve c-3 also cleanup /tmp/cache next` — 이번만 실행 + 코멘트가 출력 끝에 `[User note when approving: ...]` 로 붙어서 LLM 이 다음 액션에 반영. 승인·거절 때 남긴 말은 그 호출의 결과 끝에 한 줄로 모델에게 갑니다 — 도구가 실패해도 (v10.120.2). 모든 확인이 같습니다: 셸 전경·백그라운드, `monitor` 등록, 워크스페이스 밖·민감 경로 확인(`write_file`·`edit_file`·`read_file`·`view_image`·`fetch`·`browser`). 거절이면 `[User note when denying: ...]`. 한 호출이 위험 키워드와 밖 경로로 두 번 물었으면 줄도 둘(물은 순서)
- `/deny c-3 the path is wrong, try /tmp/build instead` — 거부(`User denied command containing `rm`: …`) + 이유가 거절 오류 끝에 `[User note when denying: ...]` 로 붙어서 LLM 이 다른 경로 탐색
- `/allow c-3 only inside /tmp` — 이 세션 동안 같은 키워드(경로 확인은 그 위치)를 묻지 않음 + 코멘트 전달. 이미 열려 있는 다른 확인은 대신 닫지 않습니다 — 각자 답합니다
- 번호 없는 명령은 사용법만(`usage: /approve <c-N> [note]` 등) — 확인이 하나뿐이어도 짐작하지 않습니다. 없는·이미 닫힌 번호는 `/approve: unknown or already-answered confirmation "c-9"`. 성공하면 `approved c-3` · `allowed c-3` · `denied c-3` 한 줄
- 명령은 들어오는 문에서 바로 답이 됩니다 — 터미널에서 친 줄, 웹 입력창(말풍선과 결과 카드가 섭니다), `run "/approve c-3"` 의 argv 질의가 같은 함수로 갑니다. 웹 트레이의 길은 `POST /api/confirms/<c-N>/approve|allow|deny`(본문 `note`)
- 기다리는 호출을 깨우는 것은 셋입니다 — 답, 그 런의 정지(웹 Stop·채널 ■·Stop everything·Ctrl+C), 세션 종료(방 닫기). 정지로 깨면 명령은 돌지 않고 그 호출의 결과는 `This run was stopped while this call was waiting for approval [c-3] — it did not run.` 입니다 — 방을 닫았으면 다음에 열 때 모델이 기록에서 그것을 읽습니다

**누가 묻는지 표시.** 에이전트가 연 확인은 터미널 줄의 머리가 묻는 쪽을 말합니다(`⚠ [aa (agt-1a2b3c4d) → user] c-4`). 웹 트레이 항목의 머리는 `⚠ main — confirmation c-3` / 에이전트 이름표, 그 아래 에이전트의 reasoning(thought)·action 줄(main 은 바로 위에 이미 있어 생략)입니다. 누가 물었든 사람(터미널·어느 뷰어든)이 답합니다.

**임시 자리는 묻지 않습니다** (v10.104.0). `rm`·`rmdir`·`mv` 의 대상이 **전부** 시스템 임시 루트(`/tmp`·`/var/tmp`·`$TMPDIR`) 또는 `.agent-cli/tmp/` **아래**면 확인 없이 실행됩니다 — 임시 파일은 그야말로 임시라 지우는 데 사람을 세울 이유가 없고, 클라이언트가 안 붙은 방에서는 그 확인이 곧 거절이었습니다. 판정은 보수적이어서 애매하면 묻습니다: 루트 자체(`rm -rf /tmp`)·루트 바로 아래 글로브(`rm -rf /tmp/*`)·대상 중 하나라도 임시 밖(`rm /tmp/a src/b`, `mv src/a.c /tmp/`)·파이프나 `find -exec`·`sudo` 뒤의 `rm`·`cd /tmp && rm x` 같은 상대 경로·`/tmp` 안의 바깥행 심링크·민감 이름(`id_rsa`). `rm -rf /tmp/build/*` 처럼 글로브 앞 디렉터리가 루트 아래면 통과합니다.

기본 활성. 비활성하려면 `AGENT_CLI_DANGEROUS_SHELL_CONFIRM=0`. 확인을 띄울 수 있는지는 **렌더러가 판단**합니다 — CLI는 터미널(TTY), 웹은 연결된 브라우저(SSE 다이얼로그, TTY 불필요). 어느 쪽으로도 물어볼 수 없는 무인 환경(TTY 없는 CI/배치 + 미접속)에서는 자동 거부 — 위험 명령이 silent 실행되는 일 없음. 병렬 run 팬아웃처럼 여러 작업이 동시에 도는 경우에도 확인은 **직렬화**되어 한 번에 하나만 떠서, 응답이 엉뚱한 작업으로 새지 않음. shlex 토큰 단위 매칭이라 `rm-helper.sh`나 `echo "rm files"` 같은 false positive는 안 잡지만 `bash -c "rm x"` 처럼 wrapper 안의 위험 명령은 놓칠 수 있음 (확인 시 모델에 알려서 풀어쓰게 유도).

### write_file — 파일 생성

새 파일을 생성하거나 기존 파일을 덮어씁니다. 작성한 content 는 hashline 포맷으로 반환되어 `read_file` 없이 바로 `edit_file` 수정이 가능합니다 — 기존 파일의 작은 변경은 전체 재작성 대신 `edit_file` 권장. v10.77.0: 40줄 이하는 파일 전체, 넘으면 처음·끝 10줄만 태그를 붙여 돌려줍니다(나머지는 `read_file`). 결과 머리줄에 줄 수가 붙고, 30% 미만 덮어쓰기 넛지는 명령 대신 사실만 말합니다. `edit_file` 안내의 "NEVER a shell heredoc" 금지는 지웠습니다(native_fc 64회 heredoc 쓰기가 모두 실패 0). **write_file 이 허용된 루프에는 edit_file 도 붙습니다** — 덮어쓸 수 있으면 일부도 고칠 수 있으니(더 작은 권한), `allowed-tools` 에 write_file 만 적은 프로파일·스킬도 edit_file 을 씁니다.

```json
{"action": "write_file", "action_input": {"path": "output.txt", "content": "hello world"}}
```

### agent — 서브에이전트 (run + 상주 spawn)

서브에이전트 단일 도구입니다 (5.0.0 에서 구 `delegate`/`teammate` 통합). `mode` 로 두 수명 모델을 오갑니다:

| mode | 수명 | 용도 |
|------|------|------|
| `run` | 과제 하나 — 바로 돌아오고 결과는 회신으로, `complete` 하면 내려온다(채널은 회색으로 남음) | 독립적인 일회성 작업 |
| `spawn` / `status` / `resume` / `kill` | **상주** — 답변 후에도 컨텍스트 유지, 일은 `message` 로 보낸다 | 반복 협업, 역할별 장기 작업 |

모델이 받는 **선택 기준은 비용 우선**입니다 (v10.26.0): 위임하면 요약만 돌아오고(읽은 것은 그 창에 남고, 틀린 것도 같은 어조로 돌아온다), 여러 파일을 읽어야 결론이 나올 때 위임하고 몇 번의 호출이면 직접 하며 **의심스러우면 직접** 합니다. spawn 은 같은 영역에 여러 라운드의 질문이 이어질 때만, 영역당 하나. 넘긴 일은 다시 하지 않고 제 몫을 끝낸 뒤 `wait` — 회신이 오면 깨어납니다(기다리는 회신이 남은 `complete` 는 되돌아옵니다).

```json
{"action": "agent", "action_input": {"mode": "run", "task": "Read /tmp/data.csv and count rows"}}
{"action": "agent", "action_input": {"mode": "run", "task": "Fix the bug we found", "context": "fork"}}
{"action": "agent", "action_input": {"mode": "run", "task": "Review this code", "profile": "code-reviewer"}}
{"action": "agent", "action_input": {"mode": "spawn", "profile": "researcher", "task": "레포 구조를 파악해줘"}}
{"action": "message", "action_input": {"to": "agt-3f2a1b9c", "text": "그래서 진입점이 어디야?"}}
{"action": "agent", "action_input": {"mode": "status"}}
{"action": "agent", "action_input": {"mode": "resume", "key": "agt-3f2a1b9c", "task": "이어서 진행해"}}
{"action": "agent", "action_input": {"mode": "kill", "key": "agt-3f2a1b9c"}}
```

#### 공통 — 프로파일 (profile)

run 과 spawn 이 **같은 프로파일 파일**을 씁니다. YAML frontmatter(`description`/`allowed-tools`/`model`/`hooks`/`auto-spawn`) + 본문(역할 — 서브에이전트 시스템 프롬프트의 Role 섹션을 통째로 교체). 검색 경로: 프로젝트(`.agent-cli/agents/`) → 패키지 내장(`agent_cli/agents/builtin/`) (v9.0.0 — `~/.agent-cli/agents/` 는 읽지 않음).

- **프로파일 발견**: 사용 가능한 프로파일 목록(이름+description)이 시스템 프롬프트 `## Agent Profiles` 섹션에 광고되어 **모델이 스스로 적합한 전문가를 골라** run/spawn 합니다 (`disable-model-invocation: true` 로 숨김 가능). description 이 발견 표면이니 "무엇의 전문가인지"를 명확히.
- **내장 프로파일** — 코드 워커 5종 + 범용 `general`(오케스트레이션은 main 이 담당). 모두 **격리된 private `memory`**(세션·compaction·resume 를 넘어 자기 지식 축적, 서로 못 봄)를 가집니다:
  - `code-writer`(구현 — **파일 스코프 규율**: 담당 파일만 수정, `Files touched:` 보고, 에러/정리 경로·검증 전 정직 보고).
  - `code-reviewer`(읽기 전용 리뷰 — 정확성·보안 우선, **구체적 실패 시나리오로 검증된 결함만** severity·`file:line`·CONFIRMED/PLAUSIBLE 으로 보고; 스타일 nitpick 배제).
  - `code-analyst`(읽기 전용 분석 — "어떻게 동작하나": 콜패스·등록 간접·실행/동시성 컨텍스트·수명 추적, `file:line` 인용. 결함 판정은 안 함=reviewer 몫).
  - `unittest-writer`(테스트 — **뮤테이션으로 무는지** 증명: 코드 뒤집으면 실패해야, observable effect 검증, 의존성 페이크로 격리, 실행 검증).
  - `log-analyst`(읽기 전용 — 로그·스택트레이스·크래시에서 **근본 원인**: bottom-up 프레임·cascade 첫 도미노·트리거 조건, 증거 인용).
  - `general`(v10.21.0 — **전문가가 없는 일**: 조사·자료 수집·정리, 단순 명령 실행 후 보고, 리포트·문서 **파일 작성**. 코드 작성·리뷰·분석·테스트·로그는 해당 전문가에게 넘기라고 명시. 시킨 산출물만 새로 만들고 자기가 만들지 않은 파일은 지목됐을 때만 고침, `Files written:` 보고, 사실마다 출처. 에이전트 생성 도구는 없음. 보드에서 `@general 이 폴더 조사해서 report.md 로 정리해줘` 처럼 직접 부르는 손잡이.)
  - `orchestrator`(spawn 전용 조율자 — /orchestrate 가 소환한 워커 로스터(키)를 받아 **peer `message` 로 배정→수집→리뷰→수리 루프를 자율 주행**. 워커 회신은 orchestrator 에게만 라우팅되어 main 을 깨우지 않고, main 은 최종 보고/에스컬레이션만 받음. 스스로 spawn 불가 — 워커가 더 필요하면 main 에 message 로 요청).
- **세션 감사자 `auditor` (v10.42.0, `AGENT_CLI_AUDITOR=1` 또는 web 🔎 칩)**: 하니스가 돌리는 **하니스 작업**이지 팀원이 아닙니다 — 로스터·`message`·`agent` 요청/kill·`agents.json`·에이전트 상한 어디에도 없고, 프로파일(`agent_cli/agents/harness/auditor.md`)은 `@agents` 카탈로그 밖입니다. **main 의 사용자 런이 `complete` 로 끝날 때마다** 하니스가 그 런의 기록 구간(`seq a..b`)을 과제로 감사 런 하나를 돌리고(`<세션>/harness/auditor/<a>-<b>/`, `history` 테이블 밖), 감사자는 `history` 도구로 **그 구간만** 읽어 요청마다 `met`(근거 서수) · `unmet` · `unverifiable`(종결문이 주장하지만 관찰에 근거가 없음 — "테스트 통과" 인데 shell 기록이 없는 경우) 을 판정합니다. 보고는 감사 런의 `complete` 결과이고 하니스가 그걸 main 메일함에 넣습니다(모델이 고르는 전달이 아님; 턴 소진·오류면 그 사유가 대신 갑니다): main 채널에 🔎 카드로 보이고, main 이 **한 번 더** 깨어나 미충족을 손봅니다(그 런은 깨우기 런이라 다시 감사하지 않음 — 고리 상한; 통과면 바로 끝냅니다). 도구는 `history`·`read_file`·`code_index` 뿐(쓰기·`shell`·`ask`·`message` 없음). 진행 중 감사가 있으면 `run` 은 결과 배달까지 기다리고(🔎 대기 안내), 보드는 "결과 확인 중" 을 그립니다. **감사 과정은 main 의 것으로 보이지 않습니다 (v10.53.0)**: web 은 감사 런 하나를 접힌 `🔎 auditor · seq a-b` 카드에 담고(펼치면 감사자가 실제로 읽은 것, resume 해도 다시 채워짐), CLI 는 감사 스레드 출력을 모아 버리고 끝에 `🔎 auditor · a-b ✓ (37.2s)` 한 줄만 찍습니다 — 감사자의 호출은 main 타임라인에 섞이지 않습니다. **v10.83.0**: main 의 종결문 끝 두 줄 `Checked:` / `Not checked:` 가 감사의 **검증 목록**입니다 — Checked 항목마다 구간 안의 관찰 기록을 찍어 대조하고(없으면 `unverifiable`), Not checked 는 찾아다니지 않고 요청이 그걸 요구했는지만 판정, 두 줄이 없으면 자기 보고 부재로 적습니다(보고의 `account:` 줄). 설계·측정: `docs/auditor/DESIGN.md` §3.4a·§6.7.
- **instant-agent (`instructions`)**: 프로파일 파일 없이 **인라인 텍스트로 즉석 전문가**를 만듭니다 — `{"mode":"spawn","instructions":"너는 이 레포의 dialect 전문가다..."}` (run 도 동일 지원). `profile` 과 병용하면 파일 본문 뒤에 덧붙는 오버레이가 됩니다 (파일=일반 원칙, 인라인=세션 특정 지시). 상주 에이전트의 인라인 지시는 **세션 내내, resume/부활 후에도** 유지됩니다 (manifest 영속).
- **`/create-agent` 스킬**: 새 프로파일 md 를 대화형으로 생성 (run/spawn 겸용).

#### mode:"run" — 과제 하나를 맡기고 결과 받기

**한 op = 한 task** (flat-native). 컨텍스트 모드로 서브에이전트가 부모 맥락을 얼마나 알지 제어합니다:

| 모드 | 동작 |
|------|------|
| `none` (기본) | 독립 실행. task에 모든 정보 포함 필요 |
| `fork` | 부모 컨텍스트를 복사하여 실행. 맥락 인지 + 독립 |

- **결과는 회신입니다 (v10.141.0, docs/agent-engine/DESIGN.md)**: run op 하나가 레지스트리에 에이전트(`agt-…`, 값 `ends_after_task`)를 띄우고 과제를 요청으로 보낸 뒤 **바로 돌아옵니다** — 도구 결과는 `started agent "agt-…" on request "r-21" — its reply comes back marked [r-21] and wakes you …` 한 줄입니다. 에이전트는 자기 스레드에서 과제를 돌고 `reply`(요청 id `r-21`)로 답합니다 — 답은 `reply` 하나입니다. 잊고 끝나면 부른 쪽은 결과 대신 `agt-… finished its task without a \`reply\` to your request [r-21] ("…") and is gone.` 사실 문장(마지막 호출 줄 포함)을 받습니다(v10.177.8 — 전엔 런 요약이 라벨을 달고 대신 갔습니다). 그 에이전트는 런이 `complete` 로 끝날 때 내려옵니다(채널은 회색) — 회신을 먼저 보내도 `complete` 전까지는 살아 있고, 하위 run 을 `wait` 로 기다리는 동안도 살아 있다가 깨어 이어갑니다. 회신은 장부가 보장합니다(갚을 빚이 남으면 `complete` 가 되돌아오고, 그래도 남으면 하니스가 그 요청을 "답하지 않았다" 는 사실 문장으로 닫습니다). 상주와 같은 것: 에이전트 상한(넘으면 `agent limit reached …` 거부), `agents.json`, 깊이·순환 검사, 화면, `## Live Agents`·`status`·`message` 주소(일하는 동안 `(… · run)` 표지로 나오고 `message` 로 지시를 바꿀 수 있습니다; 끝난 run 은 꼬리에서 빠집니다). 정지는 부른 런을 따르지 않습니다(v10.142.0) — 방 ■·Ctrl+C 한 번은 main 만 멈추고, run 은 자기 채널의 ■·"모두 정지"·✕ 로 멈춥니다(아래 "정지").
  - **기다리는 회신**: 보낸 요청(run 의 과제·`message` 요청)은 회신이 올 때까지 꼬리 `## Open Items` 의 둘째 절 `2. by arrival — `wait`; each reply closes its item when it comes` 에 `[r-21] (agt-3) reply awaited: "…"` 로 남습니다. 남아 있으면 `complete` 가 되돌아옵니다(장부 예산 8 그대로). 예산이 다하면 결과 끝에 `⏳ N repl(y|ies) you asked for had not arrived when this run closed (they still come as mail):` 와 목록이 붙습니다. 에이전트도 같은 규칙입니다 — 그래서 `wait` 는 프로파일 `allowed-tools` 와 무관하게 깨울 길이 있는 모든 루프에 붙습니다.
  - **main 의 제자리 대기**: 기다리는 회신이 있는 main 이 `wait` 하면 **런을 끝내지 않고** 그 자리에서 기다립니다 — 회신 도착·체크인(5분부터 두 배씩)·**사람의 입력**이 깨웁니다. 사람 입력에 깨면 모델은 `Observation: the wait was interrupted — the user wrote to you; their message is right below — r-21 still awaited, so `wait` again when you are done.` 를 보고, 그 말대로 `message` 로 에이전트의 지시를 바꾸거나 새 일을 한 뒤 다시 기다립니다. 같은 런이라 감사(🔎)도 회신까지 한 구간으로 봅니다. 기다리는 회신 없이 작업·모니터만 기다리면 지금처럼 런을 끝냅니다.
  - **웹**: run 은 다른 에이전트와 같은 **채널**입니다(칩에 `run` 표시). 부른 자리(main, 또는 부른 에이전트의 런 블록)에 한 줄 카드 `→ Run 🦊 explorer (agt-…) · <과제> — working · open channel` 이 남고, `open channel` 이 그 채널로 엿보기 점프합니다. 일하는 run 의 칩엔 ✕ 가 있고 **채널 입력창이 열려 있습니다** — 사람이 그 에이전트에게 직접 씁니다. 끝난 run 은 칩 줄 끝 **`Done N ▾` 하나로 접힙니다**(v10.143.0) — 누르면 최근에 끝난 순 목록(이름·키·과제 첫 줄·끝난 시각, 사람이 멈춘 것은 `stopped`)이 위로 열리고(8줄 + `+N more…`), 항목을 누르면 그 채널이 열립니다. 바깥을 누르거나 Esc 면 닫힙니다. 보고 있는 채널의 끝난 run 은 접지 않고 **회색 칩**(✕/↻ 없음)으로 두고 입력창이 닫힙니다(`This run agent finished its task — pick another channel`) — 다른 채널로 가면 접힙니다. 상주 에이전트는 죽어도(↻) 칩으로 남고, 부른 자리의 한 줄 카드 `open channel` 도 그대로입니다. 새로고침·resume 도 같은 모양입니다. 📋 띠는 `📋 Waiting for 1 reply · r-21 from agt-3` 를 머리로 보이고, main 이 제자리에서 기다리는 동안 띠 끝에 `main is waiting for agt-3 — type to steer it or give new work` 가 섭니다. 에이전트 안의 run(깊이 2)도 자기 채널이고 한 줄 카드는 부른 에이전트의 런 블록에 놓입니다. resume 해도 같은 카드·채널이 그려집니다.
  - **CLI**: 모든 에이전트(run·상주)의 진행이 오는 순서대로 섞여 한 줄씩 찍힙니다 — `  agt-3a1b2c4d │ ▶ explorer · find every caller…` (시작), `  agt-3a1b2c4d │ shell  rg -n parse_tokens src/  ✓` (도구 호출마다), `  agt-3a1b2c4d │ ✓ done (2.1s)` (끝). main 이 제자리에서 기다리면 `⏳ waiting for 1 reply (r-21) — type to steer or give new work, Enter to send` 한 줄 — 그때 터미널에 친 줄이 main 을 깨우는 입력이 됩니다. main 의 `message` 는 `✉ message → agt-3 (notice) Scope change: …`, 바쁜 동안 도착한 회신은 `↩ reply r-21 ← agt-3  …` 로 찍힙니다.
    - **바쁨은 배달 순간의 사실 (v10.145.3)**: `↩ reply` 줄을 찍을지는 그 메일이 **배달되는 순간** main 이 바빴는가로 정합니다 — 배달 구간 안에서 한가함을 읽어 통지에 넘기므로, 펌프가 그 메일로 이미 런을 연 뒤에 "바쁨" 으로 잘못 읽혀 같은 메일 앞에 줄이 하나 더 찍히는 일이 없습니다. 한가함은 런이 실제로 열릴 때만 꺼집니다(이미 배달된 깨우기를 건너뛰는 바퀴는 끄지 않음).
  - **resume**: 끝나기 전에 프로세스가 죽은 run 은 되살아나 과제를 다시 받고(새 요청 id) 회신을 마칩니다. 그 run 을 부른 에이전트가 돌아오지 않았으면 회신은 main 메일로 갑니다.
- **한 턴의 run 여럿**: 각 run 이 바로 돌아오고 에이전트가 각자 돕니다 — 회신이 오는 대로 main 을 깨웁니다. 독립 작업일 때만 여러 개를 내고, task B가 task A의 결과에 의존하면 A만 먼저 낸 뒤 그 회신을 읽고 B를 부르세요.
- `tools` 파라미터로 서브에이전트가 사용할 수 있는 도구를 제한할 수 있습니다. 프로파일에 `allowed-tools` 가 있으면 그것이 **상한**입니다 (v10.14.0) — `tools` 는 그 안에서 좁히기만 하고, 겹치는 도구가 하나도 없으면 호출이 거절됩니다.
- **모든 루프가 같은 표면**: `agent` 는 main 뿐 아니라 에이전트(run·spawn)·스킬 안에서도 같은 모드(run·spawn·status·resume·kill)로 보입니다 — 에이전트가 띄운 에이전트는 한 단계 깊고, `--max-depth` 의 상한에 닿은 루프에서는 도구째 빠집니다.

#### 상주 모드 — spawn / status / resume / kill, 일은 `message` 로

spawn 하면 key 를 돌려받고, 그 key(또는 이름)로 `message` 를 보내 몇 번이고 이어서 요청할 수 있습니다(v10.141.0 — 종전 `agent` 의 `request` 모드가 `message` 로 왔고, main 도 `message` 를 갖습니다: 요청·통지). 상주 에이전트는 자기 컨텍스트(이전 문답 전부)를 유지한 채 살아 있습니다.

- **비동기 + 자동 배달**: `message` 는 즉시 반환되고(`delivered as request "r-…"`), 에이전트는 자기 스레드에서 작업합니다. 회신이 준비되면 **harness 가 다음 턴 경계에서 관찰로 자동 배달**합니다 — 모델이 status 를 폴링하거나 블록할 필요가 없습니다 (main 이 idle 이어도 자동 재기동으로 배달).
- **양방향 문답 (ask 라우팅)**: 상주 에이전트가 작업 중 막혀서 `ask` 를 부르면 질문이 **main 의 mailbox 로** 옵니다 (사용자가 아니라 main LLM 이 그 에이전트의 "사용자"). main 은 같은 `request` 로 답하고, 에이전트는 답을 받을 때까지 `waiting_ask` 상태로 대기합니다. run 서브에이전트의 `ask` 는 사용자에게 갑니다.
- **에이전트↔에이전트 메시징 (5.11.0)**: 모든 상주 에이전트는 `message` 도구(커널 기본 탑재)로 **다른 상주 에이전트나 `main` 에게 직접** 메시지를 보낼 수 있습니다 — `{"to":"<key>","text":"..."}` (또는 `"to":"main"`). **비동기**입니다: 발신자는 블록하지 않고 계속 일하며, 대상의 회신은 **새 메시지로 발신자의 inbox 에 도착**합니다(도착하면 idle 이어도 깨어나 이어서 처리 — main 의 자동 배달과 동형). 배달된 회신은 **terminal**(되받아치지 않음)이라 핑퐁이 없고, 데드락은 비동기라 구조적으로 불가합니다. 회신을 받은 에이전트는 그걸로 자기 작업을 계속하고, 마무리되어 요청자(예: main)에게 보고할 결과가 있으면 다시 `message` 로 보내며, 없으면 그냥 `complete` 합니다. spawn/kill 같은 생명주기는 여전히 main 전용 — 서브에이전트는 **메시지만**. 요청은 언제나 명시적·directed 이라 상시 관찰 채널(구독)이 필요 없습니다.
- **`complete` 은 국소, 배달은 `message`/`reply` 뿐 (v9.21.0)**: 상주 에이전트의 `complete` 출력은 **아무에게도 자동 배달되지 않습니다** — 창과 history 에만 남습니다. 요청(`expects_reply=True`)으로 시작한 런은 요청자에게 **회신 하나를 빚집니다**. `reply(text)` 가 갚습니다(돌려받을 것 없음, `to` 불필요). `message(to, text)` 는 상대에게 회신을 요구하는 새 요청입니다(v9.21.0 엔 내 요청자에게 보내면 내 빚도 갚았으나 **v10.145.1 부터 갚지 않습니다** — 되물은 `message` 가 원 요청을 조용히 닫아 정작 `reply` 가 거절됐다. 갚는 것은 `reply` 하나). 빚진 게 없는 런(받은 회신·질문·독촉으로 시작)에서 `reply` 를 부르면 거부됩니다 — 그 자리가 "확인했습니다" 류 ack 가 새던 곳입니다. 갚지 않고 `complete` 하면 **최대 3회** 독촉합니다 — 거부 관찰이 그 `complete` 의 원문을 인용하며 "이게 답이었다면 `reply` 로 보내고, 남은 일이 없으면 `complete`" 로 안내하고, 물린 `complete` 자체는 컨텍스트에 남지 않습니다(v9.21.1). 그래도 없으면 하네스가 요청자에게 답하지 않았다는 사실 문장을 보냅니다(v10.177.8 — 그 전에는 런 요약을 `(no explicit reply — …)` 라벨로 대신 보냈습니다) — 요청자가 침묵을 받지는 않습니다. 실측(3인 끝말잇기 2라운드): 한 수 = 항목 1개, 독촉은 에이전트당 첫 런 1회 뒤 0. **v9.25.0**: `reply` 는 `id`(요청 번호, 꼬리의 `## Owed Replies` 에 표시)가 필수입니다. 요청마다 빚이 따로 적히고 회신도 따로 갚습니다. `id` 가 없거나 모르는 번호면 거부하고 번호 목록을 보여 줍니다(`complete` 의 `answers`, `answer` 의 `id` 와 같은 원칙). `to` 는 선택이며 번호의 상대와 어긋나면 거부합니다.
- **빚 없는 통지 · 이름 주소 · 매 턴 로스터 (v10.1.0)**: 끝말잇기 실측(cnr60g — 단어 셋에 LLM 턴 61, 메시지 39)에서 드러난 세 가지를 고쳤습니다. ① `message` 에 `expects_reply: false` 를 주면 **통지**입니다 — 상태 공유·인수인계·작별 인사처럼 답이 필요 없는 말. 받는 쪽은 빚지지 않고 꼬리표가 "ack 하지 말고 할 일 없으면 `complete`" 라고 말합니다(main 에게 보내면 main 관찰도 `notice` 로 갈림). (v10.145.1 부터 통지도 내 빚을 갚지 않습니다.) 배달된 회신의 꼬리표도 "되받아칠 것 없음, 감사 인사 금지" 로 바꿨습니다. ② `message.to`(v10.141.0 부터 main 도)가 **키·`agent:<key>` 접두·인스턴스 이름**을 모두 받습니다(`resolve_key`; 이름은 대소문자 무시, 산 것 우선, 유일할 때만). ③ 상주 에이전트의 `## Live Agents` 명단이 **매 턴 꼬리**에 실립니다(`peer_roster` 포트). 런 도중 spawn 된 동료도 바로 보입니다. 시스템 프롬프트의 섹션은 "이 프롬프트의 마지막 `## Live Agents` 가 지금 명단이다" 는 안내만 남깁니다(main 의 v8.46.0 이동과 같은 구조; v10.1.1 — 모델은 턴 경계를 못 보므로 "턴 끝" 대신 "마지막 섹션" 으로). ④ main 의 spawn 관찰이 "peer 는 `message` 로 이름이나 키로 부른다, 명단은 그들의 꼬리에 있다" 고 알려 주고, 상주 루프에서 `agent(mode=request)` 를 부르면 거절 문구가 `message` 를 가리킵니다 — 재실행에서 main 이 플레이어들에게 `agent request` 로 서로 부르라고 가르쳐 한 턴씩 낭비된 것.
- **회신·메시지의 결과는 사실을 말합니다 (v10.145.2)**: ① 요청자가 죽었거나(✕·사망·멈추는 중) 명단에 없으면 `reply` 의 결과는 `not delivered — agent "agt-…", which sent request [r-1], was already gone. [r-1] is closed; there is nothing more to do for it.` 이고 실패로 기록됩니다 — main 이 갚든 에이전트가 갚든 같은 문장이고, 그 id 에 다시 `reply` 해도 같은 문장입니다(main 도 — v10.146.6; 종전 main 은 `no unpaid request …`·`nothing to reply to …` 였습니다. 이미 회신한 id 에 다시 `reply` 하면 누구든 `already replied to [r-1] (…) in this run …` 입니다)("이미 회신했다" 가 아닙니다 — 같은 키가 나중에 되살아날 수 있어 과거형입니다). 종전엔 에이전트가 갚을 때 "delivered to whoever was waiting on it" 이라고 했습니다. 빚은 닫힙니다(기다리는 이가 없습니다). ② 런이 `wait` 로 끝나도 그 런에서 갚은 회신은 다음 런으로 가져가지 않습니다 — 다음 런에서 같은 id 에 `reply` 하면 "already replied … in this run" 대신 `no unpaid request …` 입니다(같은 런 안에서는 그대로 "already replied … in this run"). ③ 보내지 못한 `message`(모르는 상대·죽은 상대·자기 자신·빈 글)는 실패로 기록됩니다(✗, 여러 op 묶음에서는 `FAILED`) — 관찰 글은 그대로입니다.
- **사람의 말과 에이전트 메일이 main 앞 한 줄에 섭니다 (v10.147.0)**: 종전에는 사람의 말이 따로 입력 큐에 섰고, 메일이 한가한 main 을 깨우려면 그 큐에 깨우기 문장("New agent mail has arrived. …")을 가짜 사람 말로 넣었습니다. 이제 둘이 같은 상자에 서고 main 은 그 한 자리에서 기다립니다 — 깨우기 문장이 없습니다. 달라지는 것: ① 사람의 말과 메일이 함께 줄 서 있던 런(예: `--resume` 뒤의 질의, main 의 마지막 응답 중에 둘이 함께 온 경우, 바쁜 동안 말이 쌓인 뒤 메일이 온 경우)에서 그 깨우기 문장이 웹에 **사람 말풍선**으로 한 번 뜨고 그 런의 작업 기록에도 들던 것이 없어집니다(메일은 전처럼 그 런에서 사람 말 뒤에 읽힙니다). ② 깨우기 문장이 번호를 하나씩 쓰지 않아, 메일이 연 런 뒤의 `u-`·`r-`·`n-` 번호가 종전보다 작습니다. ③ 한가한 main 이 메일로 깨는 순간에 사람의 말이 오면 사람의 말이 런을 엽니다. ④ 제자리에서 기다리는 main 은 사람의 말에 바로 깹니다(종전 0.25초 간격 확인). ⑤ 웹 대기열 이벤트의 `system` 키가 없어집니다. 모델이 읽는 글과 저장 파일의 모양은 같습니다.
- **main 의 런은 상자가 엽니다 (v10.148.0)**: 사람의 말이 연 런과 메일이 연 런이 같은 길로 시작합니다 — 펌프 고유의 요청 모양과 깨우기 문장이 없어졌습니다(모델이 읽는 것은 그대로). 함께 달라진 것 셋: ① `run` 에서 main 이 제자리에서 기다리는 중 줄 선 명령(예약이 낸 `/…` 등)도 그 자리에서 처리합니다 — 종전에는 main 이 깨어 "the user wrote to you" 관찰만 읽고 말은 없었습니다(웹은 이미 그랬습니다). ② 웹에서 제자리 대기 중 처리된 명령도 말풍선이 나가고 대기열 영수증이 "✓ applied" 입니다 — 종전에는 "Cancelled" 로 보였습니다. ③ 명령 처리 중 예외가 나도 웹의 main 은 죽지 않습니다 — "Worker error: …" 로 알리고 다음 말을 처리합니다(`run` 은 종전대로 오류로 끝납니다).
- **런을 열 것을 꺼내는 함수가 하나입니다 (v10.152.0)**: main 과 에이전트가 같은 창구 함수(`PostOffice.next_run`)로 다음 런을 열 것을 꺼내고(꺼내는 법만 다릅니다 — main 은 사람의 말 먼저, 에이전트는 도착 순), 에이전트를 끝내는 법도 main 과 같이 "정지를 세우고 그 상자를 닫는다" 하나입니다(되살리면 다시 엽니다). 동작은 종전과 같습니다. 알아 둘 사실 하나: 방을 재시작한 직후에는 main 과 되살아난 에이전트가 아직 되살아나는 중인 에이전트를 `[starting]` 으로 볼 수 있습니다 — 종전에도 그랬고, 그 에이전트의 준비가 끝나면 `idle` 로 바뀝니다.
- **런을 여는 루프가 하나입니다 (v10.151.0)**: main 과 에이전트가 같은 루프(`actor.run_actor`)를 돕니다 — 기다리고, 열고, 돌리고, 끝을 판정합니다. 다른 것은 꺼내는 법·수명·표면뿐입니다(`pump.MainActor`, `resident_loop.AgentActor`). 동작은 종전과 같습니다.
- **런 끝의 사실이 한 모양입니다 (v10.153.0)**: 런이 어떻게 끝났는지(끝남·실패·정지·`wait`·예외)를 값 하나(`actor.RunEnd`)로 나타내고, main 의 런 끝 정리는 한 자리(`MainActor.ended`)에서 합니다. 동작은 종전과 같습니다. 알아 둘 사실 하나: 런 도중에 main 에게 회신을 보낸 에이전트는 그 런을 마치기 전까지 main 의 에이전트 목록에 `[busy]` 로 보입니다 — 종전에도 그랬습니다.
- **main 도 사실대로 끝납니다 (v10.156.0)**: 런이 끝나는 규칙을 main 과 에이전트에 똑같이 적용합니다. ① main 을 ■(또는 Stop everything)로 멈추면, main 에게 요청하고 답을 기다리던 에이전트가 에이전트의 ■ 와 같은 정지 회신(`The user stopped main while it was working on your request [r-…] ("…").`)을 받습니다 — 종전에는 빈 런 요약이 갔습니다. Stop everything 의 것은 그 에이전트를 깨우지 않습니다(종전에는 깨웠습니다). ② main 의 런이 실패로 끝나면 회신 머리에 원인 한 줄(`(the run ended with an error: …)`)이 붙습니다. ③ 웹 main 의 런이 예외로 죽어도 그 자리에서 뒷정리합니다 — 종전에는 회신 빚이 남아, 다음 사람 말의 런에 그 빚이 먼저 나오고 상관없는 답이 그 요청의 회신으로 갔습니다(`run` 의 종료 코드·결과 파일은 그대로). ④ 요청한 에이전트가 사라지면(✕·사망) 그가 기다리던 빚을 닫고, 일하던 쪽은 다음 턴 경계에 `request [r-…] ("…") is closed — agt-… is gone; nothing is owed back.` 를 읽습니다 — 종전에는 받을 이 없는 빚을 든 채 일했습니다. ⑤ 부모 에이전트가 오류로 죽으면 그가 띄운 run 에이전트도 멈춥니다(`… was stopped because agt-… (the agent that started it) died, …`) — 종전에는 받을 이 없이 계속 일했습니다. ⑥ 꺼지는 웹 방은 줄 선 사람의 말로 새 런을 열지 않습니다 — 종전에는 종료 중에 그 말로 모델을 불렀습니다.
- **사라진 쪽의 뒷정리 둘 (v10.157.0)**: ① 요청한 에이전트가 사라지면(✕·사망) 그가 보내 놓고 아직 아무도 시작하지 않은 요청도 받는 쪽 상자에서 빠집니다 — 종전에는 남아 있다가, 받는 쪽이 나중에 그 요청으로 런을 열어 끝까지 일한 뒤 `reply` 에서야 `not delivered — … was already gone` 을 들었습니다. 받는 쪽은 그 요청을 본 적이 없으므로 통지는 없고, 대기열·로스터의 수만 줄어듭니다. 재시작도 그런 요청을 되살리지 않습니다(세션 종료 뒤 resume 은 그대로 되살립니다). ② 부모가 죽어 하니스가 멈춘 run 이 또 run 을 띄워 두었으면, 그 손자에게 요청한 쪽이 받는 회신은 `… was stopped because … (the agent that started it) was stopped` 입니다 — 종전에는 멈춘 부모를 `died` 라고 했습니다. 죽은 부모의 자식은 그대로 `died` 입니다.
- **세션을 닫으면 일하던 것은 그 자리에서 멈춥니다 (v10.164.0)**: 웹 방을 닫거나(Ctrl+C·SIGTERM·`--idle-timeout`) 세션이 끝날 때 일하던 작업 구간 — main·상주·일회용 에이전트 모두 — 은 사람이 멈춘 것과 같은 정지 플래그로 멈추고 **세션 종료**로 끝납니다. ① main 이 모델 호출 도중이면 그 응답은 실행되지 않고 모델을 다시 부르지 않습니다 — 종전에는 응답의 도구가 돌고, 빚진 회신이 있으면 `complete` 가 되돌아와 되묻기 예산만큼(최대 9번) 모델을 더 불렀습니다. ② main 의 전경 `shell` 은 명령을 끝냅니다(`This run was stopped, so the command was ended after …` — 종전에는 끝까지 돌고 한 번 더 불렀습니다). ③ 에이전트의 history 끝에 `⚡ User interrupted. Waiting for new instructions.` 가 남지 않습니다 — 사람은 멈추지 않았습니다. ④ 끝 정리는 하지 않습니다: 정지·폴백 회신, 감사, 체크인, 처리 수(로스터의 handled 가 끊긴 요청을 세지 않습니다). **일회용 에이전트가 세션 종료로 끊기면 `OnAgentEnd` 훅이 불리지 않습니다** — 과제가 끝난 것이 아닙니다(사용자 훅의 동작 변화). 다시 열면 history 끝은 끊기기 전의 마지막 기록 다음에 재시작 기록(`⚡ The session restarted. …`)입니다. run 의 Ctrl+C 는 그대로입니다(첫 누름은 사람의 정지). 함께 고친 것: 에이전트가 작업 구간 도중 받은 요청(`message`, 회신 기대)의 원문이 갚기 전에 컨텍스트가 압축되면 요약으로 바뀌던 것 — 이제 그 요청도 작업 구간의 첫 요청처럼 갚을 때까지 원문 그대로 남습니다(종전에는 꼬리 `## Open Items` 의 앞 120자만 남았습니다). 또 하나: 받아들여진 `wait` 가 기다리기 전에(수락 기록·`PreWait` 훅·대기 줄·체크인 걸기에서) 예외로 끝나면 그 에이전트의 기다림 표시가 남아, 그를 기다리는 쪽의 `wait` 가 `… owes you a reply but is itself waiting …` 로 거짓 거절되던 것 — 이제 받아들인 `wait` 의 표시·체크인·대기 줄은 언제나 함께 내려갑니다.
- **기다림을 기다림으로 보입니다 — 화면 (v10.165.0)**: 모델이 읽는 글은 바뀌지 않습니다(작업 상태는 그대로 `busy`). ① `status.json`(과 `/api/health`)이 기다림을 따로 말합니다 — main 이 제자리에서 기다리는 동안 `waiting: true`(받아들여진 `wait` 의 대기 줄과 함께 서고 내립니다), `agents.waiting`(기다리는 에이전트 수)·`agents.list[].waiting`, 그리고 `agents.working` 은 기다리는 에이전트를 세지 않습니다. 보드가 이 값을 읽어 "Waiting" 을 그리는 것은 보드 쪽 릴리스입니다. ② 작업 블록 머리 줄의 `↓`: 작업 구간 도중 받은 요청·회신·통지는 블록 머리의 한 줄과 몸통의 관찰 카드로 서는데, 머리 줄 끝의 `↓` 가 같은 블록 안의 그 카드로 갑니다(그 배달의 id 로 찾습니다; 줄을 누르면 본문이 펼쳐지는 것은 그대로). ③ `⚠ Fallback` 줄은 빚 하나당 한 줄입니다 — 회신 없이 끝난 작업 구간이 요청 둘을 들고 있었으면 줄도 둘이고, 꼬리에 각 줄의 실제 받는 이가 섭니다(종전에는 첫 요청의 상대로 한 줄). 사람이 에이전트 창에서 시킨 구간에 다른 이의 요청이 남았으면 폴백 줄은 그 상대에게, 사람에게 가는 결과에는 폴백 라벨이 붙지 않습니다. ④ 재시작한 상주 에이전트의 채널에 `↻ Session restarted — work in progress was cut off` 한 줄이 섭니다(되살릴 때 그 채널의 기록 끝에 적습니다 — 그 뒤에 온 요청은 이 줄 아래에 섭니다. 사이에 아무 줄도 없이 거듭 재시작하면 한 번이고, 채널에 아직 줄이 하나도 없는 에이전트에게는 서지 않습니다. 모델이 읽는 재시작 기록은 따로 history 를 기준으로 적힙니다). ⑤ `/help` 에 `@<profile>-spawn <task>`·`@agt-<key> <message>` 두 줄. ⑥ 안 쓰던 `WebServer.pending_count` 를 지웠습니다.
- **기다리는 에이전트를 모델에게 "waiting" 으로 말합니다 (v10.166.0)**: 모델이 읽는 글이 바뀝니다. 받아들여진 `wait` 로 기다리는 에이전트는 작업 구간 안에 있어 작업 상태가 `busy` 인데, 종전에는 다른 에이전트를 읽는 세 자리가 모두 그 `busy` 를 그대로 말해 "일하는 중" 으로 읽혔습니다. 이제 셋이 같은 사실(우편의 기다림 표시)을 같은 낱말로 말합니다 — ① 꼬리 `## Live Agents`: `[busy]` → `[waiting]`(`[waiting · 1 queued]`), ② 회신 없이 끝난 런의 폴백 회신 꼬리: `(agent status: waiting · N queued — …)`, ③ `wait` 중간 깨우기(체크인)의 줄 선 요청 줄: `… · has your request queued, next in line · waiting, last step 1m ago`. 작업 상태(`status.json`·화면의 `state`)는 그대로 `busy` 입니다. 체크인의 기다림 줄은 기다리는 것의 이름만 적습니다 — `… · owes you a reply · itself waiting on agt-B`, 작업이면 `… on job #1`(종전: `agent:agt-B working on your request`·`job #1 (\`pytest -x tests/\`)`). `wait` 수락 문장은 그대로입니다.
- **작업 구간이 멈추면 그 안의 `fetch` 도 멈춥니다 (v10.167.0)**: 모델이 읽는 글이 하나 바뀝니다. `fetch` 가 서버의 답을 기다리는 동안 작업 구간을 멈추면(■·Ctrl+C·`agent` `kill`·세션 종료) 그 요청을 그만둡니다 — 전경 `shell`·MCP 호출과 같은 정지를 같은 간격(0.2초)으로 봅니다. 종전에는 요청의 시한 15초를 다 기다린 뒤에야 멈췄고, 기록에는 `Fetch failed (timeout) … One retry may succeed.` 가 남았습니다. 이제 관찰은 `This run was stopped, so the request was given up after 0s.` 한 줄입니다(멈춘 작업 구간은 끝났으므로 다음 작업 구간의 모델이 기록에서 읽습니다). 웹 방을 닫을 때 정리가 끝난 뒤 15초가 지나 main 이 기록에 그 줄을 덧붙이던 것도 없어집니다. 버린 요청은 제 시한에 스스로 끝납니다.
- **안 쓰는 코드와 틀린 주석 정리 (v10.167.1)**: 동작은 같습니다. 제품·테스트 어디서도 쓰지 않던 정의와 읽는 곳이 없던 필드·인자를 지우고, 같은 것이 두 이름이던 자리를 하나로 모으고(`TOOL_SCHEMAS` → `TOOLS`, `run_started` → `cancel_checkin` 등), 없는 것을 가리키거나 코드와 반대이던 주석·독스트링을 고쳤습니다. 모델이 읽는 글과 프롬프트는 그대로입니다.
- **명령으로 띄운 `@agent`·`/skill` 도 시작·끝 훅을 부릅니다 (v10.168.0)**: 사람이 명령으로 띄운 `@<profile>`·`/<skill>` 에서도 `OnAgentStart`·`OnAgentEnd`·`OnSkillStart`·`OnSkillEnd` Python 훅이 불립니다(예약이 보낸 명령 포함). 종전에는 모델이 도구로 불렀을 때만 불렸습니다 — v10.87.0 의 노트가 `/skill` 명령도 받는다고 적었지만 실제로는 불리지 않았습니다. 그 훅의 `ctx` 는 main 의 MCP 매니저를 받습니다. 모델이 읽는 글은 그대로입니다.
- **다시 접속해도 에이전트 이름이 같습니다 (v10.168.1)**: 방을 새로 고치거나 다시 접속하면(재시작 뒤 포함) 에이전트 채널의 줄에 붙는 에이전트 이름 — 런 블록 머리 줄의 보낸 이, ⏳ 대기 줄, ⚠ 폴백 줄의 받는 이, `message` 도구 줄의 상대 — 이 지어 준 이름(`🐌 peer`) 대신 키에서 만든 별명(`🐌 Quiet Snail`)으로 바뀌어 보였고, `agent` 도구 줄에서 그 에이전트 채널로 가는 칩이 사라졌습니다. 이름은 그 줄을 그릴 때 에이전트 명단에서 푸는데, 다시 접속한 화면이 명단을 줄보다 늦게 받았기 때문입니다. 이제 명단이 먼저 옵니다(라이브는 본디 그 순서였습니다). 모델이 읽는 글과 기록 파일은 바뀌지 않습니다.
- **main 에게 묻고 바로 `wait` 해도 받아들여집니다 (v10.169.0)**: 에이전트가 main 에게 회신을 기대하는 `message` 를 보내고 곧바로 `wait` 하면, main 이 그 요청을 아직 집지 않은 동안(긴 도구 호출 안이거나 다른 것을 기다리는 중)에는 "nothing would wake you" 로 거절됐습니다 — 받는 쪽이 에이전트일 때는 받아들여지던 것과 어긋났습니다. 이제 줄 선 내 요청의 받는 쪽은 main 이든 에이전트든, 기다리는 중이든 깨울 것으로 셉니다. 모델이 읽는 글: 수락 문장이 `waiting — will wake on: main has your request queued, or in 5m if nothing has reported.` 가 되고, 체크인 글에서 main 이 아직 집지 않았으면 그 줄이 `main · has your request queued` 입니다(집은 뒤에는 전과 같이 `owes you a reply`). 체크인을 걸 때 맨 앞에서 하던 해제는 지웠습니다 — 이미 걸린 것이 있는 채로 거는 길이 없어 동작은 같습니다.
- **따라가기는 사람이 굴렸을 때만 풀립니다 (v10.169.1)**: 방의 대화가 바닥을 따라가는 중에, 화면 위쪽의 내용이 바뀌어 브라우저가 스크롤 위치를 옮기면 그것을 "사람이 위로 굴렸다" 로 읽어 따라가기가 풀리는 일이 있었습니다 — 에이전트 채널에서 도는 작업 구간이 새 요청을 받아 ⏳ 줄이 블록 머리로 옮겨질 때 아래로 새 카드가 함께 붙으면 화면이 그 자리에 멈췄습니다. 이제 휠·트랙패드·터치·굴리는 키(PageUp·PageDown·Home·End·↑·↓·Space)·Tab 초점 이동·누르고 끄는 동작(스크롤바·글자 고르기)으로 사람이 굴렸을 때만 풀리고 붙습니다. 그 밖에 바뀐 위치는 따라가는 중이면 바닥으로 돌아갑니다 — 따라가는 중의 페이지 안 찾기도 여기에 듭니다(먼저 위로 굴려 풀어 두면 그대로 머뭅니다). `scrollend` 사건이 없는 브라우저에서는 첫 굴림 뒤로 종전과 같은 판정입니다. 그리고 "↓ n new" 가 에이전트 채널에서도 셉니다: 위로 올려 읽는 동안 온 카드를, 작업 블록이나 스킬·작업 묶음 카드 안에 붙은 것까지 셉니다(종전에는 대화의 맨 바깥에 붙은 것만 세어 에이전트 채널에서는 단추가 뜨지 않았습니다). 모델이 읽는 글은 바뀌지 않습니다.
- **훅이 넣은 시스템 섹션은 훅이 뺄 때까지 프롬프트에 있습니다 (v10.170.0)**: 모델이 받는 프롬프트가 두 경우에 달라집니다. ① 웹에서 지침(DIRECTIVE.md)을 저장하면 그 작업 구간의 남은 턴에서 `OnSessionStart` 훅이 넣은 섹션이 프롬프트에서 사라졌습니다 — 내장 스냅샷 훅의 `## Workspace snapshot` 안내도 그랬습니다. 이제 남습니다. ② `ctx.remove_system_section(title)` 이 앞선 발화(`OnSessionStart`, 앞 턴의 `PreLLMCall`)가 넣어 이미 프롬프트에 든 섹션도 뺍니다. 종전에는 같은 발화 안에서 방금 넣은 섹션만 취소했습니다. 뺀 제목은 뒤의 발화가 다시 넣으면 돌아옵니다. 작업 구간마다 프롬프트를 새로 만드는 것은 그대로입니다.
- **감사와 `run` 의 결과 파일·종료 코드는 그 작업 구간이 든 사람의 요청 번호로 판정합니다 (v10.171.0)**: 무엇이 작업 구간을 열었는지는 더 보지 않습니다. 감사는 성공으로 끝났고 사람의 요청(`u-`)을 하나라도 든 작업 구간에 돌고, `run --result-file` 과 종료 코드는 argv 질의의 번호를 든 작업 구간의 답입니다. "닫은 번호" 가 아니라 "든 번호" 라서 모델이 `complete` 의 `answers` 를 빠뜨리거나 다른 항목만 적어도 판정은 그대로입니다. 달라지는 길은 셋이고 모두 드뭅니다. ① 감사 — 통지·회신에 깨어난 작업 구간이 도중에 사람의 말(친 말, 예약 발화)을 받아 성공으로 끝난 경우: 종전에는 메일이 연 구간이라 감사하지 않았고, 이제 감사합니다(감사가 켜져 있을 때). ② `run` — argv 질의가 아닌 친 줄이 작업 구간을 **연** 경우(제자리 대기 중 친 줄이 읽히기 전에 앞 구간이 멈췄을 때): 종전에는 그 구간의 답이 결과 파일을 덮어쓰고 종료 코드를 다시 정했고, 이제 둘 다 그대로입니다. ③ `run` — 질의가 백그라운드 작업을 남겨 답 사슬이 열린 동안 깨어난 작업 구간이 도중에 사람의 말을 받은 경우: 종전에는 그 답이 결과 파일을 덮어쓰고 사슬을 닫을 수 있었고, 이제 그 구간은 판정하지 않으며 사슬은 열린 채 다음 깨우기 구간의 답을 기다립니다.
- **방과 보드가 낡은 상태로 남지 않습니다 (v10.171.1)**: 에이전트 명단·입력창 위 대기열·🧵 작업 칩·노브 칩과 `status.json` 은 하니스가 지금 상태를 떠서 화면과 파일로 내는데, 두 스레드가 거의 동시에 그 일을 하면 먼저 뜬 쪽이 나중에 내 옛 값이 새 값을 덮는 순서가 있었습니다. 그러면 다음 변화가 올 때까지 — 기다리는 에이전트가 일하는 중으로, 이미 꺼낸 말이 대기 줄에, 끝난 작업이 🧵 칩에, 방금 바꾼 노브가 옛 값으로(보드가 그 값을 다음 실행에 돌려줍니다), main 이 한가해졌는데 보드에는 "Responding" 으로 — 낡은 상태가 보였습니다. 드문 순서입니다(느린 기계에서 한 번 관찰). 이제 뜨기와 내기가 한 걸음이라 마지막에 나간 것이 언제나 지금의 상태입니다. 모델이 읽는 글은 바뀌지 않습니다.
- **main 도 도착 순서로 엽니다 — 빈 글 스킬과 빈 말은 거절합니다 (v10.172.0)**: ① **여는 규칙이 하나입니다.** main 과 에이전트가 같은 규칙으로 다음 작업을 엽니다 — 줄 선 첫 깨우는 것 하나(사람의 말이든 메일이든 도착 순서), 나머지는 그 작업의 첫 턴 경계가 도착 순서로 읽습니다(첫 모델 호출 전). 전에는 main 만 사람의 말을 앞세웠고, 메일이 연 작업은 그 메일을 첫 턴 경계에서야 읽었습니다. ② **모델이 읽는 순서가 바뀝니다(글자는 같습니다).** 통지가 먼저 오고 사람이 그 뒤에 썼을 때 — 전: `[dj]: …` → `🧵 job #1 finished …`, 후: `🧵 job #1 finished …` → `[dj]: …`. 꼬리 Open Items 의 줄 순서도 도착 순서입니다. 사람의 말이 다음 작업으로 밀리지는 않습니다 — 같은 작업의 첫 모델 호출 전에 읽습니다. 명령(`/skill …`)보다 먼저 온 메일이 있으면 그 메일이 작업을 열고 명령은 그 첫 턴 경계에서 실행됩니다. 메일이 연 작업을 열리자마자 ■ 로 멈추면 그 메일은 기록에 남고 멈춥니다(다시 열리지 않습니다 — 요청이었다면 보낸 에이전트가 정지 회신을 받습니다). ③ **에이전트 창의 이어진 말을 묶지 않습니다.** 에이전트가 바쁜 동안 그 창에 쓴 말 둘은 한 메시지(`[user:a]: x` 빈 줄 `[user:b]: y`)로 묶여 다음 작업을 열었습니다. 이제 첫 말이 열고 둘째는 첫 턴 경계가 읽습니다 — 메시지 둘, 글자는 같습니다. 옛 세션의 묶음 블록은 그대로 그려집니다. ④ **채운 글이 빈 스킬은 돌지 않고 거절합니다.** 인자 없이 부른 `$ARGUMENTS` 만의 스킬은 모델을 부르지 않고 `Skill returned no result` 만 돌려줬습니다. 이제 까닭을 말합니다 — 인자가 없을 때 `Skill "<name>" cannot run: no arguments were given and its prompt is empty without them.`, 인자가 있는데도 빌 때 `Skill "<name>" cannot run: its prompt is empty.` 빈 스킬 폴더도 남지 않습니다. ⑤ **이미지만 보낸 말이 작업을 엽니다.** 웹에서 main 에게 글 없이 이미지만 보내면 아무 일도 일어나지 않았습니다(에이전트 창은 됐습니다). 이제 그 이미지가 시작 메시지입니다. ⑥ **빈 말은 들어올 때 거절합니다.** 글도 이미지도 없는 말(공백뿐인 글 포함)은 사람의 말이 아닙니다 — 웹 `/api/input` 은 `400` `content must be a non-empty string`(에이전트 창과 같은 글; 전에는 `200` 으로 받고 말없이 버리거나 공백뿐인 글을 모델에게 보냈습니다), `agent-cli run ""` 은 세션을 만들기 전에 `query is empty` 한 줄과 종료 코드 `2`(전에는 아무것도 하지 않고 `0`).
- **기다릴 것이 없는 `wait` — 닫지 않은 것이 있으면 그 번호로 거절, 없으면 작업이 끝납니다 (v10.173.0)**: ① **판정이 둘로 갈립니다.** 깨울 것(돌고 있는 작업·모니터, 회신을 빚진 에이전트 등)이 없을 때 `wait` 는 닫지 않은 것 — 갚지 않은 회신(`r-`)이나 답하지 않은 사람의 요청(`u-`, `complete` 의 `answers` 로 닫는 것) — 이 있으면 그 번호를 말하며 거절하고, 없으면 받아들여 그 작업을 끝냅니다(`complete` 로 끝난 것과 같은 끝 — 일회용 에이전트는 내려옵니다). 통지 항목(`n-`)은 세지 않습니다. 깨울 것이 있으면 닫지 않은 것이 있어도 종전처럼 그 자리에서 기다립니다. 전에는 빚이 있든 없든 같은 글로 거절했고 끝 문장이 main·상주·일회용마다 달라, 할 일이 없는 에이전트가 `wait` 거절과 `complete` 사이를 오갔고 빚이 있는 쪽은 거절이 "`complete` 로 끝내라" 고 하고 되묻기가 "`reply` 를 먼저" 라고 해 엇갈렸습니다. ② **모델이 읽는 글.** 거절: `wait refused — nothing would wake you, and you still owe a `reply` [r-2]. Reply first, or keep working.` · `wait refused — nothing would wake you, and [u-1] is still unanswered. Answer it with `complete`, or keep working. To hear from an agent, send it a request with `message` first.` · 둘 다면 `wait refused — nothing would wake you, and you still owe a `reply` [r-2] and an answer to [u-1]. Settle those first, or keep working. To hear from an agent, send it a request with `message` first.`(번호가 여럿이면 `[r-2], [r-5]`). 사람의 요청이 남은 두 글의 끝 문장은 기다리는 방법입니다 — 할 일 없이 기다리는 main 이 에이전트에게 요청을 보내 그 회신을 기다리게 합니다. 끝: 기록에 `nothing to wait for: no job or monitor of yours is running, no agent owes you a reply, and you owe none. This run has ended.` 이 남고 다음 작업의 모델이 그것을 읽습니다. `wait` 설명의 끝 문장은 `When nothing will wake you: refused while a `reply` you owe or a person's request is still open; with nothing owed, the run ends.` ③ **화면.** 그렇게 끝난 작업의 끝에 `Run ended — nothing to wait for.` 한 줄 — CLI 는 `●`, 웹은 오류 카드가 아닌 중립 줄입니다(다시 접속해도 남고, `--resume` 에는 다른 끝 사유 줄처럼 나오지 않습니다). 에이전트 창에서 사람이 연 작업이 결과 없이 끝나면 빈 발신 줄을 내지 않습니다. 사람의 질의를 든 작업은 답하기 전에 `wait` 로 끝나지 않으므로 `run` 의 결과 파일·종료 코드와 감사는 그대로입니다. ④ **`agent` `status` 끝 줄.** `(undelivered replies: N)` 이 `(main has N unread mail queued — replies, requests or notices)` 로 바뀝니다 — 세는 것은 그대로(main 상자에 줄 선, 사람의 말이 아닌 봉투 전부 — 회신만이 아니라 요청·통지·작업·모니터·감사·체크인), 묻는 쪽이 에이전트여도 main 의 상자입니다.
- **답할 길이 있는 곳에서만 묻습니다 — 터미널에서는 `/answer` 로 답합니다 (v10.174.0)**: ① 터미널도 웹 뷰어도 없는 `run` 에서는 `ask` 가 거절됩니다 — 누가 묻든 `ask refused — nobody can answer here: this session has no terminal and no viewer. Go on with what you have and say what you assumed in your result.` 종전엔 main 은 `A: (no response)` 를 답으로 받았고, 에이전트의 질문은 답할 길 없이 올라갔습니다. ② `/questions` 가 열린 질문을, `/answer q-N <글>` 이 그 질문에 답합니다 — 웹 트레이와 같은 길이고 웹 채팅 칸에서도 됩니다. 답은 번호를 적은 명령과 트레이로만 받습니다 — 그 밖의 말은 질문이 열려 있어도 main 의 요청이고, 번호 없는 `/answer` 는 사용법만 말합니다. 터미널의 질문 줄에 번호와 답하는 법이 붙습니다. ③ 터미널 `run` 도 웹처럼 사람에게 물은 질문이 열려 있으면 끝나지 않습니다 — `❓ Waiting for your answer to q-3 — type /answer <q-N> <text>, or press Ctrl-C to quit.` 한 줄을 찍고 기다립니다. 종전엔 질문을 버리고 끝났습니다. ④ 터미널 `run` 은 main 이 한가한 동안에도 친 줄을 읽습니다 — 보통의 말은 main 의 새 작업을 엽니다(종전엔 main 이 기다릴 때만 읽었습니다). ⑤ 확인 프롬프트가 떠 있는 동안 친 줄은 언제나 그 프롬프트가 받습니다 — 종전엔 프롬프트가 뜨는 사이 친 줄을 main 이 가져갈 수 있었습니다.
- **프롬프트는 뜬 뒤에 친 것만 받습니다 (v10.174.1)**: 터미널 `run` 에서 main 이 일하는 동안 미리 친 줄이 그 뒤에 뜬 확인 대화(위험 셸 승인·작업 공간 밖 접근)의 결정이나 `ask` 의 답이 되던 길을 닫았습니다 — 종전엔 미리 친 `y` 가 그대로 승인이 됐습니다. 이제 그 줄은 main 의 새 요청이 되고 `  ↪ typed before this prompt — sent to main as a new message: <그 줄>` 한 줄이 찍힙니다. 프롬프트가 뜬 뒤에 친 것(붙여 넣은 여러 줄 포함)은 종전과 같습니다. 덜 친 줄은 버려집니다. 웹과 터미널이 아닌 `run` 은 바뀌지 않습니다.
- **묻고 기다리는 에이전트 — 내가 건 열린 질문은 나를 깨웁니다 (v10.175.0)**: ① 에이전트가 사람에게 `ask` 로 묻고 `wait` 하면 그 자리에서 답을 기다리고, 답이 같은 작업을 잇습니다. 종전엔 갚을 회신이 있으면 "회신을 먼저 하라" 로 거절됐고(답을 받아야 회신할 수 있는데), 없으면 작업이 끝나 답이 새 작업을 열었습니다. 수락 문장은 `waiting — will wake on: your question "q-3" to the user.` 입니다. 그 에이전트를 기다리는 쪽(main·일회용 에이전트의 부모)의 `wait` 도 받아들여집니다. ② 질문만 기다리는 동안엔 중간 깨우기가 없습니다 — 답은 사람이 줍니다. 그 에이전트를 기다리는 쪽의 중간 깨우기 줄은 `… · owes you a reply · blocked on question "q-3" to the user, unanswered for 5m` 입니다. ③ 기다릴 것이 없는 끝 글에 `no question of yours is open` 이 듭니다. ④ 상주 에이전트 안내에서 "답은 도는 런에 닿지 않으니 기다리지 말라" 문장을 지웠습니다. ⑤ 에이전트가 사람에게 물어 둔 질문이 열려 있는 동안, 그 에이전트의 꼬리에 `## Your open questions` 가 섭니다 — 답을 기다리지 말고 할 수 있는 데까지 하고, 더 나아갈 수 없으면 `wait` 하라는 안내와 질문 목록입니다. `ask` 의 관찰과 상주 설명·안내의 "할 게 없으면 `complete`" 는 지웠습니다. 장부 띠(📋)는 바뀌지 않습니다.
- **`ask` 는 누가 묻든 한 길 — 질문을 올리고 바로 돌아오고, 사람이 답하거나 닫습니다 (v10.176.0)**: ① **main 도 에이전트와 같이 묻습니다.** main 의 `ask` 는 더 이상 막는 프롬프트(웹 트레이의 답 칸·터미널 `Your answer:`)가 아닙니다 — 질문을 ❓ 트레이와 터미널 질문 줄(`❓ [main → user] q-2`)에 올리고 관찰 `question "q-2" sent — "…"` 로 돌아옵니다(종전 `User responded:` / `Q: …` / `A: …`). 답을 기다려야 하면 `wait` 하고, 꼬리에 6a 의 `## Your open questions (do not wait for the answer: do what you can without it, and call `wait` only when you cannot go further)` 가 섭니다. `complete` 로 끝내도 질문은 열린 채 남고, 늦게 온 답이 새 작업을 엽니다. ② **답은 장부 항목이 아니라 통지 봉투입니다.** 누가 물었든 답은 `── answer [n-3] from the user re [q-2] ──` / `Your question: …` / `Answer: …` / `(Continue the work it was blocking.)` 로 옵니다 — 에이전트의 답도 종전 `u-` 항목(런 도중이면 `[user]: ` 머리)이 아니라 같은 `n-` 봉투라 `complete` 의 `answers` 로 닫을 것이 없습니다. ③ **답 없이 닫습니다.** 웹 트레이의 `Close without answer`, 터미널·웹 채팅의 `/close q-2`(번호가 없으면 `usage: /close <q-N>`). 묻는 쪽은 `── closed [n-4] from the user re [q-2] ──` / `Your question: …` / `(The user closed it without an answer. Go on with what you have, and say what you assumed.)` 를 받습니다. 터미널 질문 줄 끝은 `  answer with: /answer q-2 <text> · close with: /close q-2`, `run` 대기 알림은 `❓ Waiting for your answer to q-2 — type /answer <q-N> <text> or /close <q-N>, or press Ctrl-C to quit.` 입니다. ④ **스킬도 묻고 기다립니다.** 스킬 런은 자기 주소로 묻고(`❓ [skill demo → user] q-4`, 트레이 `❓ skill demo asked`, `/questions` 의 `q-4  skill demo  …`) `wait` 로 답을 기다립니다. 기다리는 동안 친 명령(`/answer`·`/close` 등)은 그 자리에서 처리되고 보통의 말은 main 앞에 줄 섭니다. 스킬 런이 끝나면 그 런의 열린 질문은 닫힙니다. 내장 `create-skill`·`create-agent` 의 둘째 단계는 `2. Ask each clarifying question with `ask` (one question per call), then `wait` for the answers:` 입니다. ⑤ **설명은 하나, 안내는 에이전트만.** `ask` 설명은 모든 루프에 `Ask the person one question and keep working — you are not blocked. It goes to their question tray; the answer arrives later as a new message and you continue from there. Carry on with anything that does not depend on it. Use it only when you need specific input from the person; a closing remark or a pleasantry is not a question — end with `complete` instead. To get something from an agent, send it a `message` — its `reply` comes back to you.` 입니다. `ask` 안내는 에이전트만 받습니다 — main 과 스킬 런은 받지 않습니다. 에이전트 안내의 두 줄이 `For an agent, send a `message` …`, `(a repeat is folded into the original)` 로 바뀝니다. ⑥ **같은 질문은 열려 있는 동안 접힙니다** — 같은 런이 아니어도, main 이어도. ⑦ **보드의 "답변 필요"** (status.json·`/api/health` 의 `awaiting_input`)는 확인 대화가 떠 있거나 열린 질문이 있으면 섭니다 — 누가 물었든(종전엔 main 의 막는 물음만). ⑧ **지운 것**: 터미널의 여러 줄 답 입력과 그 readline 히스토리(`chat_history`, `AGENT_CLI_NO_READLINE`), 웹 `/api/input` 의 `prompt` 종류(400), 에이전트 창 입력의 `answer_id`(트레이 답·닫기는 `POST /api/questions/<q-N>/answer|close`). 확인 대화(위험 셸 승인·작업 공간 밖 접근)는 그대로입니다.
- **우편의 닿지 않던 자리 정리 (v10.176.1)**: 동작은 같습니다. 우편함·요청 장부와 회신 배관에서 부르는 곳이 없거나 값이 한 가지뿐이던 자리를 지웠습니다 — 창구의 `complete`·`end_run` 의 세션 종료 갈래, 줄을 여는 하니스 통지(n 줄)와 닫는 법 `completed`·`died`, `reply` 판정의 "명단에 없는 주체" 갈래, 작업·모니터·감사 보고 배달의 늘 같던 인자, 그리고 늘 0 이던 hop 과 그 상한. 닿지 않던 거절 문장 둘(`unknown agent`, `reply dropped — peer hop limit (6) reached`)이 사라졌고, 모델이 읽는 다른 글과 프롬프트는 그대로입니다.
- **확인은 번호 항목 `c-N` — 답은 번호를 적은 명령·트레이로만, 기다리는 호출은 정지와 종료가 깨웁니다 (v10.177.0)**: ① 위험 셸 승인·작업 공간 밖·민감 경로 확인·고리 멈춤(Continue/Stop)이 질문과 같은 장부의 번호 항목입니다. 터미널 줄은 `⚠ [main → user] c-3  Dangerous command detected (`rm`)` · `  $ rm -rf build/` · `  approve with: /approve c-3 · always: /allow c-3 · deny with: /deny c-3 <reason>`(경로 확인은 명령 대신 `  (workspace: …):` 와 경로 목록), 웹은 ❓ 트레이 맨 위의 `⚠ main — confirmation c-3` 항목(버튼 `once (allow this command)` · `deny` · `always allow `rm` this session`, 고리 멈춤은 `Continue` · `Stop`). ② 답은 `/approve <c-N> [note]` · `/allow <c-N> [note]` · `/deny <c-N> [reason]`(`/help` 에 세 줄)과 트레이 버튼(`POST /api/confirms/<c-N>/approve|allow|deny`)뿐입니다. 터미널의 `y`·`n`·`a` 는 더 이상 답이 아니라 main 의 새 요청이고, 확인 전에 친 줄의 `↪ typed before this prompt …` 알림도 없어졌습니다. ③ 기다리는 동안 웹 Stop(■)이 보이고, ■·채널 ■·Stop everything·Ctrl+C·방 닫기가 그 호출을 깨웁니다 — 결과는 `This run was stopped while this call was waiting for approval [c-3] — it did not run.`(모델이 읽는 새 글은 이것 하나). 종전엔 웹에서 Abort 가 거절(고리 멈춤은 Continue)로 기록됐고, 닫기·채널 ■ 는 깨우지 못했으며, 닫으면 그 호출이 기록에서 사라졌습니다. 터미널에서는 main 의 확인 중 Ctrl+C 가 거절이었고, 에이전트의 확인 중 Ctrl+C 한 번이 `run` 전체를 130 으로 끝냈습니다. ④ 에이전트의 확인이 터미널을 쥐지 않습니다 — 확인이 여럿이면 함께 뜨고 각자 답합니다(`/allow` 가 열린 다른 확인을 대신 닫지 않습니다). ⑤ 확인 하나가 공용 번호 하나를 써서 그 뒤의 `u-`·`r-` 번호가 하나씩 밀립니다. ⑥ 지운 것: 웹 Abort 버튼과 `/api/abort`, `/api/input` 의 `confirm` 종류(400), 확인만의 3초 지연 경고, 트레이 말 칸의 Enter 답과 기본 버튼, 터미널의 `↳ from [aa]` 머리 블록(묻는 쪽은 줄 머리가 말합니다). 거절·남긴 말·답할 길 없음의 글과 ⚡ 자동 승인은 그대로입니다.
- **웹 방을 닫으면 정리는 main 이 멈춘 뒤입니다 (v10.177.1)**: 웹 방을 닫을 때(Ctrl+C·SIGTERM·`--idle-timeout`) 정리(에이전트·작업·MCP·세션 저장)는 main 이 멈추기를 기다린 뒤에 돕니다 — 상한 7초, SIGTERM 이면 그 정리 시한(6초) 안에서 main 을 먼저 기다리고 남은 시간이 정리 몫입니다. 7초는 전경 `shell` 의 정지(TERM, 3초 뒤 KILL, 다시 3초)와 그 결과 기록을 덮습니다. 종전엔 2초만 기다려, TERM 을 무시하는 전경 명령이면 그 호출과 결과(`This run was stopped, so the command was ended after …`)가 기록에서 사라지고 그 명령이 고아로 끝까지 돌았습니다 — 이제 기록에 남고 명령은 KILL 로 끝납니다. 응답 머리를 기다리는 모델 호출처럼 정지를 보지 않는 대기는 상한에서 끊깁니다(그 호출은 아직 기록이 없습니다). 기다리는 동안 Ctrl+C 를 한 번 더 누르면 기다림만 그만두고 정리·세션 저장은 그대로 합니다 — 종전엔 정리·저장을 건너뛰고 130 으로 끝났습니다. 모델이 읽는 글·화면의 글은 그대로입니다.
- **정지를 누르면 LLM 요청 재시도도 그만둡니다 (v10.177.9)**: 모델 서버가 응답하지 않거나(ReadTimeout) 속도 제한(429 `Retry-After`)으로 재시도하는 동안 ■ 를 누르면, 종전에는 재시도를 다 쓸 때까지 — 실측 5분·14분 — 작업 구간이 풀리지 않았습니다. 이제 정지가 서면 더 보내지 않고 기다리던 쉼도 바로 그만둡니다(0.2초 간격으로 봅니다). 스트림 도중 정지와 같은 길이라 모델이 읽는 글은 그대로입니다. 이미 보낸 요청이 응답 머리를 기다리는 동안(최장 30초)은 끊지 않고, 컨텍스트를 줄이는 압축 요약 호출의 재시도는 종전대로입니다. 정지가 서지 않으면 재시도 횟수·간격·화면 줄은 종전과 같습니다.
- **답은 `reply` 하나 — 런 요약 대체 회신을 없앴습니다 (v10.177.8)**: 요청(`r-…`)의 답은 `reply` 하나로만 갑니다. 에이전트(상주·일회용 `agent` `mode` "run" 모두)나 main 이 요청에 `reply` 없이 런을 끝내면, 요청자는 그 런의 결과 대신 하니스의 사실 문장 하나를 받습니다 — `── reply [n-…] from agt-5 re [r-32] · no reply ──` / `agt-5 ended its run without a \`reply\` to your request [r-32] ("…").`(일회용은 `… finished its task without a \`reply\` … and is gone.`) / `Last activity: …`(그 런의 마지막 호출 셋) / `Decide whether to continue another way or report back to the user.` 실패한 런은 그 위에 원인 한 줄. main 이 사용자에게 한 답이 에이전트의 요청에 대한 회신으로 가던 일도 없어졌습니다. `@<profile> 과제` 명령은 에이전트가 `reply` 하지 않으면 그 사실 문장이 답 자리에 서고 실패로 적힙니다(`--result-file` 포함). 회신 전문 파일 `agents/<key>/replies/reply-<id>.md` 와 과대 회신 포인터는 지웠습니다. 모델이 읽는 글: `complete`·`message`·`reply` 설명, Reply Discipline, 에이전트의 마지막 턴 안내, `agent` 결과의 괄호, 되돌림 머리(`Observation: your result is recorded, but …`)·예산 문장, `## Open Items` 꼬리말, 감사 메일의 끝 문장. 웹의 ⚠ 줄은 `ended without a reply — the harness told the requester so`.
- **안 쓰는 코드 정리 (v10.177.4)**: 모델이 읽는 글은 그대로입니다. 바깥에서 보일 수 있는 것은 이렇습니다. ① `agents.json` 에서 `created_at`·`instructions` 두 키가 빠집니다(저장하고 되살리기만 하던 값 — 역할은 이미 `role_prompt` 에 있습니다; 옛 파일의 두 키는 읽지 않고 지나갑니다). ② v10.140.0 한 판이 쓴 `agents.json` 의 일회용 에이전트를 되살릴 때 옛 `run_…` 디렉터리(`home`)를 읽지 않고 다른 에이전트처럼 `agents/<key>` 를 집으로 봅니다. ③ 아무도 읽지 않던 응답·저장 키: `/api/debug/prompt` 와 `prompt_views/*.json` 스냅샷의 `total_chars`·최상위 `est_tokens`(섹션마다의 `est_tokens` 는 그대로), `/api/changes` 의 `baseline.commit`. ④ SSE: 리스너가 없던 `spinner`·`stream_reset` 사건을 내지 않고, `scope_start` 의 `index`·`depth`, 스티키 `stuck` 의 `fires`, `compaction` 의 `evicted_count` 가 빠집니다. 이름표가 없는 스코프의 확인·질문 머리 이름이 `scope #1` 에서 `scope` 로 바뀝니다(제품의 스코프는 늘 이름이 있어 보이지 않습니다). ⑤ 패키지 `__init__` 이 아무도 쓰지 않던 이름을 다시 내보내지 않습니다 — `agent_cli.recovery`·`agent_cli.context`·`agent_cli.web` 등에서 패키지 경로로 가져다 쓰던 외부 코드가 있다면 정의 모듈(`agent_cli.recovery.detectors` 등)에서 가져와야 합니다. ⑥ 터미널 렌더러의 캡처가 줄을 쌓지 않습니다(에이전트·하니스 작업 스레드의 출력 숨김은 그대로 — 긴 작업에서 메모리만 줄어듭니다).
- **관찰 줄의 ✓/✗ 는 그 줄의 기록 — `[interrupt]` 는 중립 `■` (v10.177.3)**: 화면이 그리는 표시를 기록과 맞췄습니다. ① main 에 온 사람의 답·닫힘 줄(`ask` — `── answer [n-…] from the user re [q-…] ──`)이 웹 라이브·터미널에서 빨간 ✗(`✗ ask  error`)로 보였습니다 — 기록은 성공이고 resume 화면은 ✓ 였습니다. 이제 어디서나 ✓ 입니다. 다른 우편 줄(작업·모니터·체크인·감사·에이전트의 통지·요청·회신·사망·폴백)은 원래 기록과 같은 값이라 그대로입니다. ② 정지·재시작의 `[interrupt]` 줄(`⚡ User interrupted. …`·`⚡ The session restarted. …`)은 도구 호출의 실패가 아니라 사실 기록이라 ✓ 도 ✗ 도 아닌 `■` 로 그립니다 — 웹(라이브·resume·공유 화면)은 색 없는 `■`, 호출 줄 뒤에 오면 그 스텝을 실패로 칠하지 않고 `✗ … · 원인` 줄도 달지 않습니다. 복사 마크다운은 `■ \`interrupt\``, 터미널은 `  ■ interrupt`(에이전트 줄 `agt-3 │ interrupt  ■`). 빨간 ✗ 는 이제 도구 호출이 실패했을 때만입니다. 기록(history)·모델이 읽는 글·종료 코드는 그대로입니다.
- **`run` 도 닫을 때 main 이 멈춘 뒤 정리합니다 (v10.177.2)**: `run` 이 SIGTERM 을 받으면 웹 방을 닫을 때와 같은 길로 끝납니다 — main 상자를 닫고, main 이 멈추기를 기다린 뒤(상한 7초, 정리 시한 6초 안) 정리·세션 저장, 그 뒤 신호 15. 종전엔 받자마자 main 을 그 자리에서 풀어, 돌던 호출과 결과가 기록에 남지 않았습니다 — 이제 TERM 을 무시하는 전경 명령이면 `This run was stopped, so the command was ended after …` 가 남고 명령은 KILL 로 끝납니다. 응답 머리를 기다리는 모델 호출처럼 정지를 보지 않는 대기는 시한(6초)에서 끊깁니다(종전엔 곧바로 끝났습니다). 기다리는 동안 SIGTERM 이 한 번 더 오면 기다림을 끊고 정리·저장 뒤 신호 15 입니다. `⚡ Terminated — cleaning up.` 은 받자마자가 아니라 main 이 멈춘 뒤 찍힙니다. 멈출 것이 없을 때의 Ctrl+C(main 한가·일하는 에이전트 없음)도 같은 닫기로 끝납니다 — 화면의 `⚡ Interrupted.` · 130 은 그대로입니다. 모델이 읽는 글·화면의 글·종료 코드는 그대로입니다.
- **최종 답의 닉네임 귀속 배관 정리 (v10.176.2)**: 동작은 같습니다. 런이 누구(사람)에게 답하는지를 요청·회신에 실어 나르던 배관을 지웠습니다 — 웹 SSE `assistant_turn` 의 `answers` 키(닉네임 목록)와 history 관찰 레코드의 `answers` 키가 빠집니다. 그 키를 읽는 화면·보드는 없었고, 모델 입력에도 실리지 않았습니다. 최종 답 아래 "답한 요청" 칩(`complete` 의 `answers` 로 고른 요청)은 그대로입니다. 옛 세션의 그 키는 읽지 않고 지나갑니다.
- **`wait` 는 런을 끝내지 않습니다 (v10.163.0)**: 받아들여진 `wait` 는 main·상주·run 에이전트 모두 그 자리에서 기다리고, 깨면(회신·통지·작업과 모니터의 보고·사람의 말·체크인) 같은 런이 같은 열린 항목으로 잇습니다 — 종전에는 main 이 회신 없이 작업·모니터만 기다리거나 에이전트가 기다리면 런을 끝내고, 열린 항목을 다음 깨우기 런으로 넘겼습니다. 그래서 ① 런 카드가 하나입니다(종전에는 기다릴 때마다 하나 더). 받은 회신은 그 `wait` 의 결과로 옵니다(`[agent]` 머리). 요청과 회신의 짝(번호)과 `## Open Items` 의 글은 그대로입니다. ② 수락 관찰 끝의 ` Open items stay open for the next run: …` 가 없어졌습니다. ③ 턴 한도(`--max-turns`)·되묻기 예산·반복 감지기·제자리 고리는 깨어 있는 구간마다 새로 셉니다 — 꼬리의 `turn N/M` 도 깬 뒤 1 부터입니다. ④ main 의 대기 줄은 작업·모니터·예약만 기다려도 섭니다: CLI `⏳ waiting for job #1 — type to steer or give new work, Enter to send`, 웹 `main is waiting for job #1 — type to steer it or give new work`(회신과 섞이면 `1 reply (r-21), job #1, monitor m-2`·`agt-3, job #1, monitor m-2`; 회신만이면 종전 글자 그대로). ⑤ 새 훅 사건 `PreWait` — 받아들여진 `wait` 가 기다리기 직전에 불리고 막을 수 없습니다. 내장 스냅샷 훅이 거기서 `before wait (turn N)` 체크포인트를 찍어, "마지막으로 고친 뒤, 기다리는 동안 남이 바꾸기 전" 지점을 `snapshot` 으로 되돌릴 수 있습니다(종전에는 `wait` 로 끝난 런의 `session end` 체크포인트가 그 자리였습니다). 기다리는 run 에이전트는 런 안이라 `wait` 뒤에도 남고, `complete` 로 끝낼 때 내려옵니다. ⑥ 에이전트를 ✕(또는 run 에이전트를 ■)하면 그가 기다리던 회신을 먼저 닫고 그다음 그가 띄운 run 에이전트를 멈춥니다 — 사망과 같은 순서입니다. 그래서 그 run 은 `request [r-…] (…) is closed — agt-… is gone; nothing is owed back.` 통지를 받고 아무에게도 정지 회신을 보내지 않습니다(종전에는 사라지는 쪽에게 정지 회신을 보낼지가 순서에 따라 갈렸습니다).
- **웹 방을 닫을 때 기다리던 main 은 모델을 다시 부르지 않습니다 (v10.162.0)**: main 이 에이전트의 회신을 제자리에서 기다리는 중에 웹 방을 닫으면(Ctrl+C·SIGTERM — 서버가 내려가며 main 앞 대기열을 닫는다), 그 기다림이 곧바로 끝나고 런은 세션 종료로 끝납니다 — 모델 호출 없음, 정지 회신·폴백 회신 없음, `[interrupt]` 기록 없음. history 의 마지막 줄은 받아들여진 `wait` 의 관찰(`Observation: waiting — will wake on: …`)이고, 다시 열면 그 뒤에 재시작 기록이 붙습니다. 종전에는 정리가 에이전트를 멈추면 기다릴 것이 사라져 `Observation: nothing you were waiting on is running any more.` 를 붙이고 닫는 도중 모델을 한 번 더 불렀습니다(그 답은 받을 이가 없이 history 에만 남았습니다). `run` 은 정리가 런이 끝난 뒤에만 돌아 이 일이 없었습니다. 닫을 때 기다리지 않고 일하던 main(모델 호출·도구 실행 중)과 에이전트의 종료는 그대로입니다.
- **명령은 들어올 때 한 번 판정합니다 (v10.161.0)**: main 이 생각하는 동안 친 명령(`/stop agt-3`·`/help`·`@agents`·`/compact`·`/<skill>`·`@<agent> <task>` …)은 종전처럼 입력창 위 대기열에 서고, main 의 안전한 자리(한가할 때·턴 경계·제자리 대기)에서 선 순서대로 실행됩니다 — 바뀐 것은 판정의 자리입니다. 명령인지는 입력이 main 앞에 들어올 때 한 번 정하고(웹은 `/help`·`/compact`·`/sh` 를 포함, `run` 은 `/sh` 를 명령으로 보지 않음), 명령은 모델에게 가지 않으므로 `wait` 판정이 그것을 "the user wrote to you … right below" 로 세지 않습니다. 종전에는 명령만 줄에 있어도 `wait` 가 그 문장으로 거절됐고 아래에는 아무 글도 없었습니다. 지금은 명령만 있으면 `wait` 가 깨울 것대로 판정되고(기다리는 회신이 있으면 받아들여져 제자리 대기가 시작하자마자 그 명령을 실행합니다 — 대기 도중 친 명령과 같은 길), 명령과 글이 섞여 있으면 글만 셉니다(문장은 같습니다). 줄 선 명령은 그대로 일로 칩니다(`run` 은 명령을 남긴 채 끝나지 않습니다). 들어온 뒤 실행 전에 같은 이름의 경로가 생겨 명령이 받아들여지지 않으면 그 글은 사람의 말로 갑니다 — main 이 제자리에서 기다리던 중이면 그 글은 대기열 끝에 다시 서므로(새 번호), 뒤에 줄 선 말이 있으면 그 말들보다 늦게 읽힙니다.
- **재시작은 컨텍스트와 명단만 (v10.160.0)**: 세션이 끝나면 모든 것이 멈추고, `--resume` 은 main·상주의 대화 이력과 에이전트 명단(상주는 한가하게, 과제 도중 끊긴 run 에이전트는 죽은 채로)·예약·화면 재생만 올립니다. 메일함·하던 런·줄 선 요청·빚·제자리 대기·사람에게 물은 질문은 버리고 아무것도 스스로 이어 하지 않습니다 — 종전에는 끊긴 런을 안내를 붙여 다시 돌리고 미배달 메일·질문을 되살렸습니다(v10.157.0 의 "resume 은 그대로 되살립니다" 도 이제 아닙니다). main 과 되살린 상주의 대화 끝에 `[interrupt] ⚡ The session restarted. …` 한 줄이 남고, main 은 관찰 카드로도 보입니다. 종료 경고는 `… — they are dropped when the session ends`. `agents.json` 은 명단만 쓰고(버전 2 그대로) 옛 파일도 명단만 읽습니다. 새 항목 번호는 기록 글에 쓰인 번호 다음부터. 자세히는 아래 "재시작은 컨텍스트와 명단만 올립니다".
- **`wait` 가 깨우는 것·무엇이 왔나 (v10.159.0)**: ① 이미 와 있는 것을 이유로 한 `wait` 거절은 깨우는 봉투만 봅니다 — "Stop everything" 의 정지 회신처럼 깨우지 않는 봉투만 와 있으면 그것 때문에 거절하지 않고, 다음에 깰 때 함께 읽힙니다(제자리 대기도 그것에 깨지 않습니다). ② 사람의 말도 깨우는 봉투입니다 — main 도 에이전트처럼, 생각하는 동안 사람이 쓴 말이 있으면 `wait` 를 거절하고 바로 아래에 붙입니다. 거절 문장은 무엇이 왔는지와 먼저 할 일을 말합니다: `wait refused — the user wrote to you while you were thinking; their message is right below. Answer now with `complete`: your answer is delivered right away, and what is still running still reports to you.` (`complete` 는 답을 바로 전달하고, 열린 것이 남으면 런을 잇습니다 — main·에이전트 같음. 둘 다 와 있으면 `… they are right below. Answer the user now with `complete`: …`, 메일·알림만이면 `… 1 notice arrived …; it is right below. Deal with it first, then `wait` again if something is still running.`). 기다리는 **도중** 친 명령은 지금처럼 그 자리에서 처리되고 깨우지 않습니다. ③ 예약이 발화해 기다림을 깨우면 `the wait was interrupted — the schedule "<이름>" fired; its prompt is right below …` 라고 보낸 쪽을 말합니다 — 종전에는 예약도 `the user wrote to you` 였습니다. ④ 켜진 예약은 main 의 `wait` 근거일 뿐입니다 — 예약은 main 에게 발화하므로 에이전트가 예약만 기다리면 거절됩니다. ⑤ 서로 회신을 빚진 채 기다리는 고리를 main 까지 봅니다 — main 이 먼저 기다리고 있으면 뒤에 기다리려는 에이전트가 거절됩니다(종전에는 받아들여져 둘 다 잠들었습니다). 같은 때 기다리려 해도 둘 다 받아들여지지 않습니다. 거절 문장은 확인한 사실만 말합니다: `X owes you a reply but is itself waiting, and nothing that would wake it is running, so if you wait too, nobody wakes.` 뒤에 그에게 진 회신이 있으면 `You owe it a reply [r-N]: send that first.`, 없으면 `Keep working, or call `complete` if the task is done.` ⑥ 죽은 에이전트는 회신을 보낼 수 없어 기다릴 근거가 아닙니다.
- **세션에 남은 일은 판정 하나로 봅니다 (v10.158.2)**: `run` 이 끝날지와 `web` 의 `--idle-timeout` 자기 종료가 같은 판정(`runtime.session_has_live_work`)을 봅니다 — main 앞의 깨우는 봉투(사람의 말·깨우는 메일), 일하거나 할 일이 쌓인 에이전트, 모니터·백그라운드 작업·감사·켜진 예약. `web` 에만 있는 항은 접속한 뷰어·바쁜 main 워커입니다. 사람에게 물은 열린 질문은 v10.174.0 부터 두 표면 모두의 항입니다 — 터미널 `run` 도 `/answer` 로 답할 수 있게 되어 질문이 열려 있는 동안 끝나지 않습니다(종전엔 웹만 셌습니다). 달라진 것 둘: ① 막 띄운 에이전트(준비 중)도 `run` 을 붙잡습니다 — 종전에는 `web` 만 셌습니다. ② main 앞에 깨우는 메일이 와 있으면 `web` 도 자기를 거두지 않습니다 — 종전에는 `run` 만 셌습니다. 깨우지 않는 메일(Stop everything 의 정지 회신)만 남았으면 둘 다 끝납니다. 모델이 읽는 것은 바뀌지 않았습니다.
- **갚을 회신 줄은 "다 되면" (v10.158.0)**: 꼬리 `## Open Items` 의 빚 한 줄이 `[r-…] (…) reply owed: "…"  → `reply` with `id` "r-…"` 에서 `[r-…] (…) reply owed: "…"  → when it is done, `reply` with `id` "r-…"` 로 바뀌었습니다. 에이전트가 중간 보고를 `reply` 로 보내 빚을 일찍 닫고, 그래서 요청자의 `wait` 가 거절되곤 했습니다. 실측(GLM, 번갈아 돌린 20쌍): 중간 보고를 `reply` 로 보낸 회차 18/20 → 10/20, 요청자의 `wait` 거절 72 → 18. 비용은 빚 한 줄마다 매 턴 5 토큰입니다.
- **깨울 것이 없을 때의 `wait` 거절·`status` 안내·쌓임 경고 (v10.146.7)**: 깨울 것이 없으면 `wait` 는 사실 한 문장 `wait refused — nothing would wake you: no job or monitor of yours is running and no agent owes you a reply.` 뒤에 부르는 쪽에 맞는 다음 걸음을 붙입니다 — main 은 `… To hear from an agent, send it a request with `message` first.`, 상주 에이전트는 `You do not need `wait` to receive messages: end this run with `complete`, and the next message to you starts a new run with your memory intact.`, run 에이전트는 `Keep working, or call `complete` if the task is done.` (종전엔 누구에게나 "nothing you started is pending (no background job, monitor or agent of yours)" — 에이전트가 일하는 중에도 "네 에이전트가 없다" 로 읽혔고, 상주에게 다음 메시지가 새 런을 연다는 것을 말하지 않았습니다). `agent` `status` 의 `⏳ Replies arrive as agent mail …` 안내는 묻는 쪽이 실제로 기다리는 회신이 있을 때만 붙습니다(사람의 `@agents` 에는 붙지 않습니다). `message` 결과의 쌓임 경고는 내가 그 에이전트에게 보낸, 아직 시작 안 된 요청이 2개 이상일 때만 `⚠ {n} of your requests to it are still queued — …` 로 나옵니다(종전엔 상대 받은편지함의 회신·통지·남의 요청까지 "queued requests" 로 셌고 통지를 보낼 때도 붙었습니다).
- **재시작한 방에서도 "컨텍스트 밖 대화" 가 보인다 (v10.24.1)**: 압축으로 컨텍스트에서 빠진 카드는 화면에 남아 한 묶음으로 접히는데, `--resume` 으로 다시 뜬 세션은 **남은 기록만** 다시 그려 그 묶음이 통째로 없었습니다 — 처음 요청까지 화면에서 사라졌습니다(기록 1,234건 중 680건이 안 보이던 방에서 확인). 이제 재시작도 빠진 구간을 다시 그려 같은 묶음으로 접습니다. 함께, **에이전트·스킬 카드**가 자기를 부른 기록의 번호를 실어, 그 호출이 컨텍스트에서 빠지면 카드째 묶음에 들어갑니다. 이 버전 전에 기록된 세션의 에이전트 카드에는 번호가 없어 그대로 보입니다.
- **호출이 두 군데에 있으면 고르지 않고 되묻는다 (v10.24.0)**: json_fc 계열에서 op 배열이 산문으로 나뉜 두 군데 이상에 있으면(산문 속 예시 뒤의 진짜 호출, 또는 진짜 호출 뒤에 결과를 상상하고 이어 쓴 호출) 앞에서부터 고르면 산문 속 **예시를 실행**하고, 뒤에서부터 고르면 반대로 상상한 결과 위의 호출을 실행합니다. 글자만으로는 가릴 수 없으므로 어느 것도 실행하지 않고 다른 형식 거부와 **같은 규칙**(저장 안 함·직전 출력 인용·마지막 안내 하나·성공 시 접힘)으로 되묻습니다: "tool calls in more than one place … NOTHING was executed. Re-send ONLY the calls you want executed now, together at the very end". 공백으로만 이어진 배열(op 마다 배열을 다시 연 모양)은 지금처럼 합쳐 전부 실행하고, 문자열 값 안의 배열·산문의 `[1, 2]` 는 세지 않습니다. **실측**(Qwen3.8-Flash-Next, json_fc, 문법 없이, 직전 출력을 만들어 넣고 되묻기 뒤의 답 70회씩): 예시 뒤 진짜 호출 → 진짜만 다시 보냄 69/70(1 은 호출 없음); 진짜 뒤 상상한 호출 → 앞의 진짜(또는 같은 일을 하는 다른 호출) 68/70, 여전히 두 군데 1, 상상한 내용을 쓴 호출 1. 같은 입력에서 "첫 것" 은 첫 경우를 70/70 틀리고 "마지막" 은 둘째 경우를 70/70 틀립니다. 등가성 코퍼스(json_fc 619·xml_fc 230)에서 달라진 것은 합성 사례 2건뿐입니다. xml_fc 는 산문과 `<tool_call>` 블록을 번갈아 쓰는 것이 정상 모양이라 그대로 전부 실행합니다. 문법 강제가 켜진 서버는 json_fc 문법이 "산문 + 배열 하나" 만 허용해 배열-산문-배열이 생성되지 않으므로, 거기서는 산문 속 예시의 경우에만 해당합니다.
- **편집 배치는 호출마다 자기 결과, `complete` 의 결과는 하니스의 회계 (v10.23.0)**: 두 가지 모두 사용자 제안입니다. ① 같은 파일 `edit_file` 여러 개를 한 턴에 보내면 한 번에 적용하고 결과도 하나였습니다(v10.20.0 은 native 에서 나머지 호출에 "함께 처리됨" 만 채움). 하니스는 각 편집의 원본 줄 범위와 바꾼 내용을 알고 있으므로 **호출마다 자기 결과**를 줍니다 — `replace 1#WT: replaced original lines 1–1 (1 → 1 line(s))` 와 그 자리의 `-`/`+`(한쪽당 6줄까지), 마지막 호출에 파일 요약과 새 해시라인 에코. 거부될 때도 호출마다 제 사정(깨진 ref 의 이유 / "valid as sent — not applied")과 마지막에 다시 보내는 방법. 텍스트 방언에서는 `[1/3] [2/3] [3/3]` 머리로 나뉩니다. **실측**(20줄 파일의 짝수 줄 10개를 한 턴에 고치는 과제, 12런씩): 파일 정확 12/12 → 12/12, 호출 55 → 39(후보는 전부 3호출), 실패 5(ACTION_LOOP 3·NO_ACTION 2) → 0. ② native_fc 에서 `complete` 에 `answers` 가 있으면 결과 메시지가 하니스가 실제로 한 회계를 말합니다 — `completed task. Requests [17][18] removed from the user request queue. Still pending: [19].` / `… No request pending.` 정산 뒤 남은 요청 id 를 종결 레코드에 `open_requests` 로 함께 남기고(추가 필드, 옛 세션 호환) 렌더가 읽습니다. `answers` 가 없는 단일 요청 실행은 측정으로 고른 `completed task: <요청>` 그대로입니다. 이 문구는 요청 id 가 웹·상주 실행에서만 생겨 CLI 하니스로는 따로 재지 못했습니다.
- **native_fc 의 하니스 꼬리말은 별도 user 메시지로 (v10.22.0)**: 턴 끝에 붙는 "끝났으면 `complete`" 안내와 세션 상태가 native_fc 에서는 마지막 `tool` 메시지 본문 끝에 들어가 도구가 한 말처럼 보였습니다(v10.20.0 검토에서 남긴 4번). `tool` 뒤에 user 메시지로 따로 둡니다(OpenAI 규격 허용). 실측(Qwen3.8-Flash-Next, 6턴 대화 × 24, "호출 없이 산문만" 거부율): 본문 끝 18.1% → 별도 user **5.6%**, 답·파일 정확 20/24 → 23/24, 호출 290 → 264, 입력 토큰 −9%. 텍스트 방언은 바뀌지 않습니다.
- **내장 프로파일 `general` (v10.21.0)**: 내장 6종이 전부 코드 작업용이라 조사·정리·보고서 같은 일에는 고를 프로파일이 없었습니다. 파일 쓰기를 허용한 범용 워커를 더합니다 — 시킨 산출물만 만들고, 코드 작성·리뷰·분석·테스트·로그 진단은 해당 전문가에게 넘기라고 설명과 본문에 못박았습니다. **실측**(Qwen3.8-Flash-Next, 6과제 × 4런, 전후 비교): 모델이 스스로 `general` 을 고른 경우는 비코드 과제 12회 중 **0** — 이 모델은 코드 구현에서도 `code-writer` 를 안 고르고 프로파일 없이 보냅니다. 전문가 대신 `general` 을 잘못 고른 경우도 0(리뷰·로그는 그대로 `code-reviewer`·`log-analyst`). 즉 지금 효과는 사용자가 `@general …` 로 직접 부르는 손잡이이고, 프로파일 없는 에이전트의 기본 역할을 `general` 로 두는 안은 행동 변화라 별도 측정 뒤 결정합니다.
- **소통 도구 개정 — id 하나, 봉투 하나 (v10.77.0)**: ① **id 가 곧 키** — 배달마다 id 하나(`u-` 사람 요청·`r-` 회신을 기다리는 요청·`n-` 통지·회신 도착·`q-` 사람에게 건 질문)를 세션의 할당기 하나가 매깁니다. 번호 공간을 모든 접두가 함께 써(`u-1`, `r-2`, `n-3` …) 번호만으로 항목이 정해지고, 받은편지함·런 이름·회신 파일(`replies/reply-<id>.md`)·웹 런 블록이 에이전트별 순번 대신 이 id 를 씁니다. ② **`ask` 는 사람에게만, `answer` 삭제** — 에이전트에게 무언가 필요하면 `message`(요청)를 보내고 상대가 `reply` 로 갚습니다. ③ **`reply` 의 `to` 삭제** — id 가 전역에서 유일해 그것만으로 갈 곳이 정해집니다. ④ **`message` 의 결과가 id 를 말합니다** — `delivered as request r-17 — its reply will come marked [r-17]`; `agent` 의 `request`·`spawn` 결과도 같습니다. ⑤ **들어오는 봉투 한 모양** — `── <kind> [<id>] from <who>[ re [<id>]] ──` 다음 줄부터 본문, 할 일이 있을 때만 괄호 한 줄(`request`·`notice`·`reply`·`answer`·`died`). ⑥ **열고 닫는 규칙은 그대로** — 요청은 그 id 의 `reply` 로만 한 번, 사람 요청·메일은 `complete` 의 `answers` 로 명시한 것만, 예산 8, 미납 회신은 라벨 붙은 런 요약. 질문을 건 런도 같은 상대에게 진 빚을 감추지 않습니다 — 묻는 쪽은 `wait` 로 회신을 기다린 뒤 자기 빚을 `reply` 로 갚습니다. `wait` 로 끝난 런의 빚은 다음 런으로 이어집니다(런 끝 판정은 한 곳입니다), **서로를 기다리는 고리**(A→B→A 둘 다 `wait`)는 두 번째 `wait` 를 거부해 끊습니다. 예전 세션의 상주 에이전트 대기 항목은 이어지지 않습니다(agents.json version 2, 호환층 없음).
- **native_fc 되먹임 검토 — 모든 호출에 결과, 호출은 다시 파싱하지 않음, 깨진 인자·스트림 오류 (v10.20.0)**: 서버 네이티브 함수 호출(native_fc)이 컨텍스트에 남기는 모양을 실제 세션으로 다시 렌더해 검토하고 네 가지를 고쳤습니다. ① **모든 호출에 결과** — 같은 파일 편집 여러 개를 한 번에 적용한 턴과 `complete` 턴은 결과 없는 호출을 남겼습니다(실측 520 중 25; OpenAI 규격 위반, 엄격한 서버는 400). 렌더 때 채웁니다: 합쳐진 호출은 "다른 호출과 함께 처리됨", 종결 호출은 `completed task: <그 턴이 답한 요청>`. 기록은 바꾸지 않아 옛 세션도 맞습니다. 종결 문구는 6턴 대화로 실측해 골랐습니다("호출 없이 산문만" 거부율: 없음 10.0% · "Delivered to the user." 17.5% · 지난 최종답을 산문으로 71% · "completed task" 11.1% · 요청 인용 9.7%). ② **호출은 다시 파싱하지 않음** — 서버가 준 호출을 텍스트로 바꿔 다시 파싱하던 것을 호출 목록에서 바로 턴을 만들게. 산문에 `[{"action": …}]` 이 있으면 그쪽이 호출이 되고 실제 호출이 버려지던 것(재현)이 사라집니다. ③ **깨진 인자는 실행하지 않음** — 인자가 JSON 이 아니면 빈 인자로 실행돼 "필수 인자 없음" 안내가 나가고, `complete` 면 런이 답 없이 끝났습니다(재현). 이제 원문을 보존해 "인자가 JSON 이 아님(어디서)" 을 알리고 실행하지 않습니다. ④ **스트림 오류** — 서버가 스트림 안에 보낸 그 밖의 오류는 "응답이 비었다" 가 아니라 서버 문구를 그대로 인용한 안내가 됩니다. 배치 안의 거부가 제 호출 id 에 붙는지는 회귀 테스트로 확인했습니다(문제 없었음). 자세한 결정은 `docs/dialects/NATIVE.md` §5.1.
- **끊긴 출력의 안내는 마지막 하나만, 도구 호출 도중 끊김은 그렇다고 알린다 (v10.19.1)**: 두 가지를 고쳤습니다. ① 출력 한도 잘림·폭주 중단 안내가 연속 실패 동안 **쌓였습니다** — "넛지는 마지막 하나만"(v10.2.3)의 접기 호출이 형식 거부 경로(`_intervene`)에만 있어, 관찰을 직접 붙이는 이 두 경로는 건너뛰었습니다(세 번 끊기면 실패 인용 셋이 다음 호출에 실림; resume 은 하나로 재판정해 라이브와 달랐음). 접기를 모든 관찰의 단일 기록점 `_append_observation` 으로 옮겼습니다. ② omlx 는 출력이 네이티브 도구 호출 **도중에** 끝나면 `finish_reason` 없이 스트림 오류 `incomplete_tool_call` 하나만 보냅니다. 이를 버려서 루프에는 빈 응답(NO_OUTPUT)으로 보였고, 모델은 "함수 호출이 없었다"는 안내만 받아 같은 큰 호출을 되풀이했습니다. 이제 잘림과 같은 길(실행 안 함·`OUTPUT_TRUNCATED` 기록, `stop_detail=incomplete_tool_call`)로 가고 "호출이 중간에 끊겼다 — 더 작은 단위로" 안내를 받습니다. 실측(Qwen3.8-Flash-Next, native_fc, 출력 한도를 600 토큰으로 낮춘 시험 런처, 80줄 파일 과제, 5런씩·300초): 끊긴 호출이 실패로 돌아온 횟수 main 75(빈 응답 74 + 잘림 1) → 19(전부 잘림으로 식별), 한 런 안 연속 실패 최대 30 → 4, 호출 수 146 → 107. **과제 완료는 나아지지 않았습니다**(1/5 → 0/5 — 이 한도에서는 양쪽 다 대부분 중도 포기·부분 완성). 고친 것은 같은 호출의 맹목 반복이지 작은 한도에서의 완주가 아닙니다.
- **최종답의 생각은 펼치고, 실패한 스텝은 접는다 (v10.19.0)**: 두 가지 기본값을 바꿨습니다. ① 최종답 카드의 💭 생각이 첫 줄만 보이던 것을 **전문을 펼친 채** 답보다 흐리게 보입니다(눌러서 접을 수 있음). 모델이 할 말을 생각 칸에 쓰고 최종답에는 `out` 한 단어만 낸 실측이 있었고, 접힌 화면에서는 빈 답으로 보였습니다. ② 실패한 스텝 카드를 펼친 채 두던 것을 **접어 둡니다**. 펼쳐진 빨간 카드가 바로 위의 성공한 스텝(파일 생성)을 가려 사용자가 찾지 못한 실측이 있었습니다. 접힌 카드는 빨간 테두리·`✗` 배지에 더해 머리 아래 `✗ 도구 · 원인` 한 줄을 달고, 그 줄이나 머리를 누르면 관찰 본문까지 한 번에 열립니다. 스텝에 붙지 못한 단독 실패 관찰도 접힌 채 원인 줄을 요약으로 보입니다. main · 상주 에이전트 채널 · agent/skill 카드 안이 같은 규칙입니다.
- **상주 에이전트가 있는 방에서 "컨텍스트 밖 대화" 가 안 보이던 것 (v10.18.1)**: 상주 에이전트가 하나라도 있으면 main 의 "컨텍스트 밖 대화" 묶음이 통째로 사라졌고, 에이전트 채널에도 묶음이 생기지 않았습니다. 서버는 에이전트의 뷰(`ctx_view`)에 그 에이전트의 **채널 key** 를 실어 보내는데, 화면은 런 스코프(`key#seq`)만 채널로 풀 줄 알아 모르는 id 를 main 으로 돌렸습니다 — 마지막에 온 에이전트의 뷰가 main 의 뷰를 덮었습니다(압축한 적 없는 에이전트면 "빠진 것 없음" 으로). v10.6.0 부터 있던 결함이고 에이전트 없는 방에서는 드러나지 않았습니다. 이제 모르는 id 는 자기 열쇠로 두어 main 을 건드리지 않고, 에이전트 채널은 자기 묶음을 갖습니다. 실제 방(main 압축 2회 · 에이전트 셋)의 이벤트를 화면에 다시 흘려 확인: 묶음 0개 → main 125장 · 에이전트 84장 · 75장.
- **토큰 추정을 실측 비율로 — 사용량 줄은 서버가 센 값만 (v10.18.0)**: 압축 시점을 정하는 내부 토큰 계산을 **토큰/바이트 비율**로 바꿨습니다. 모델 호출마다 서버가 보고한 입력 토큰을 그 요청의 UTF-8 바이트 수로 나눠 `새 비율 = (이전 비율 + 이번 실측) / 2` 로 갱신하고(첫 실측은 그대로 채택, 0.05–2.0 으로 제한), `<세션>/token_ratio.json` 에 저장해 `--resume` 때 불러옵니다. 서브에이전트는 부모의 비율로 시작합니다. 비율이 아직 없을 때만 글자수/4 추정을 씁니다. 총량은 여전히 서버 실측이 기준이고, 비율은 호출 사이에 붙는 증가분과 재개 직후의 캐시 값을 매기는 데만 씁니다. 토큰 통계가 `split {system, tools, tail, convo}`(합 = 실측 입력)를 싣고 화면은 그것만 보이며, 실측이 없으면 `N/A` 입니다. 실측(omlx `Qwen3.8-Flash-Next-oQ4e-mtp`, 창을 14,000 토큰으로 줄여 파일 8개를 통째로 읽는 과제, 수정 전후 각 5회, 회당 420초 제한): 정답 1/5 → 3/5, 압축 43 → 39회, 형식 실패 20 → 10건, 창을 넘긴 호출은 양쪽 0. 압축 직전 실측 입력의 중앙값은 10,660 → 11,695 토큰(창에 더 가깝게 채우고 압축), 압축 순간의 내부 대화 추정치 최댓값은 37,241 → 7,648. 표본이 5회씩이라 정답 수 차이는 참고용입니다. 측정된 비율은 0.247–0.253 토큰/바이트로 회차 간 거의 같았습니다.
- **서브에이전트의 스텝이 main 의 카드로 들어가던 것 (v10.17.1)**: 라이브에서 일회성 서브에이전트를 부르면 그 에이전트의 도구 호출과 결과가 자기 카드가 아니라 **main 의 `⚡ agent` 스텝 카드 안에** 행으로 붙었습니다(배지 `⚡ agent +1`). 서브에이전트 카드에는 최종답만 남고, main 의 `agent` 관찰은 짝을 잃어 별도 카드로 떨어졌습니다. 스텝 병합의 열쇠가 채널이었고 턴 번호는 루프마다 1 부터라, main 의 턴 1 이 열려 있는 동안 온 서브에이전트의 턴 1 이 "같은 턴의 두 번째 op" 로 보인 탓입니다. 열쇠를 스코프로 바꿨습니다. resume 재생은 이벤트 순서가 달라 원래 멀쩡했고, 서버가 보낸 이벤트도 처음부터 맞았습니다. 함께: 화면이 더는 부르지 않는 서버 코드를 지웠습니다 — `GET /api/debug/prompt/scopes`, `DELETE /api/debug/prompt`, `WebRenderer.prompt_scopes`·`delete_prompt_scope`·`scope_dynamic_sections`, `web.inspector._dynamic_context_sections`, `context.render.message_label`. 끝난 스코프의 대화 크기는 대화 본문 사본 대신 종료 시점의 `ctx.get_estimated_tokens()` 숫자 하나로 남깁니다(라이브와 같은 척도).
- **모델 시점 통일 — 🔍 드로어 제거 (v10.17.0)**: 드로어가 보여 주던 것을 챗 안으로 옮겼습니다. 맨 위 "맨 처음 받는 것"(시스템 프롬프트·함수 스키마·문법·압축 요약), 맨 아래 "매 턴 끝에 붙는 것"(꼬리 diff), 그리고 컨텍스트에서 빠진 카드를 접어 둔 "컨텍스트 밖 대화" 묶음입니다. main · 상주 에이전트 채널 · agent/skill 카드가 같은 부품을 씁니다(위 **모델 시점** 참고). 압축 요약 카드는 대화 중간에서 맨 위 카드 안으로 갔습니다. 서버는 스코프별 스냅샷을 `<세션>/prompt_views/` 에 파일 하나씩 남기고 resume 때 되살립니다(main 을 되살리면 시작 시점 캡처를 건너뜁니다 — 화면은 마지막 호출 기준). 함께 고친 것 둘: ① agent/skill 카드 **안에** 놓이는 카드마다 `appendToTimeline` 이 `ReferenceError` 를 냈습니다(v10.0.0 이전부터 — `return` 한 줄이 다른 코드로 바뀌어 있었음). 그 카드들은 복사 버튼·스크롤 병합을 못 받았습니다. ② 끝난 agent/skill 스코프의 `ctx_view` 가 재접속 스냅샷에서 main 의 뷰를 덮어, 새로고침하면 main 의 "컨텍스트 밖" 표시가 사라졌습니다. 상주 에이전트 채널은 이제 런 블록 안의 카드까지 그 채널의 뷰로 판정합니다.
- **런 도중 흡수된 메시지의 ⏳ 대기 줄 정리 (v10.8.2)**: 상주 에이전트가 턴 경계에서 받은편지함을 흡수하면(v9.25.0) 새 런이 열리지 않아, 화면의 `⏳ 대기` 줄이 배달 뒤에도 영원히 남았습니다(67qcmb 실측 — 7분 생성 중 도착한 메시지가 다음 경계에 배달됐는데 카드는 대기 그대로). 이제 흡수할 때 `agent_msg direction="absorbed"`(`absorbed_into` = 도는 런의 seq)를 내고 conversation.jsonl 에도 남겨, 화면이 그 대기 줄을 **도는 런 블록의 머리**로 옮깁니다(블록이 안 보이면 줄만 ✓ 흡수). resume 재생도 같은 그림입니다.
- **열면 맨 아래로 바로 (v10.8.1)**: 방을 열 때 스냅샷이 재생되는 동안 카드가 붙을 때마다 내려가는 스크롤이 보였습니다. 이제 서버가 스냅샷 끝에 `snapshot_end` 를 보내고, 클라이언트는 그때까지 타임라인을 숨긴 채 쌓다가 한 번에 맨 아래로 점프한 뒤 보여 줍니다(옛 서버라 표시가 안 오면 2초 뒤 공개). 재접속은 전체 리로드라 같은 길입니다.
- **export 제거 → 카드별 ⧉ 복사 (v10.8.0)**: 📤 Export(선택 모드 · HTML 다운로드 · Jira 코멘트 게시)와 `integrations/`(jira.py·export.py), `/api/export/*`, `jira` 설정이 사라졌습니다. 대신 카드마다 ⧉ 로 그 카드를 마크다운으로 복사하고, 헤더의 ⧉ 로 보이는 대화 전체를 복사합니다(위 **⧉ 복사** 참고). 클립보드 헬퍼는 하나로 합쳤고(워크스페이스 id 칩·인스펙터·카드가 같은 함수), 드로어는 옛 서버가 보내는 대화 섹션(`dynamic`)을 시스템에 섞지 않고 무시합니다(업그레이드 직후 "프로세스는 옛 코드, 화면은 새 코드" 창).
- **🔍 드로어 재편 — 대화 밖에서 모델이 받는 것만 (v10.7.0, P2)**: 드로어에서 대화 목록·섹션 필터·대화 요약 줄이 빠지고(대화는 챗이 곧 모델 시점), 모달이 아니라 **도킹**됩니다(열려 있는 동안 챗 폭이 줄고 챗은 그대로 보임). 제목은 스코프 이름만(`main` / 상주 에이전트 이름 / `🦀 reviewer: …`), 인라인 스코프는 위에 출처 줄(`main 안에서 돈 인라인 에이전트 · 종료 — ↑ 카드로 · main 으로 돌아가기`). 예산 한 줄(시스템·함수 스키마·대화·꼬리 막대 + `총합 / 창 (%)` + `⊙ 압축 n회`), 그룹 "매 턴 바뀜"(매턴 꼬리 섹션 — **직전 턴과의 줄 diff**, `+n −m vs 턴 p` 또는 `변화 없음`)과 "고정"(시스템 프롬프트 · N 섹션 / Function schemas · N 함수 / 📐 디코딩 문법, 접힌 묶음). 열려 있는 동안 턴마다 자동 갱신(`● 턴 N 에 갱신`), 종료된 인라인 스코프는 `끝날 때 스냅샷`. 서버: LLM 호출 스냅샷이 그 호출의 꼬리를 싣고(`render_system_prompt_snapshot(tail=)`, `split_tail` 은 `prompts/session_state` 로 이동) 같은 스코프의 직전 꼬리를 `tail_prev`/`prev_turn` 으로 돌리며, `/api/debug/prompt` 는 `budget`·`scope {kind, label, parent, ended}` 를 더하고 `dynamic` 섹션을 내지 않습니다(스냅샷 없으면 `ok=False`).
- **챗이 곧 모델 시점 — 컨텍스트 밖 카드 흐림 + 압축 요약 카드 (v10.6.0, P1)**: 웹 챗이 그 에이전트의 컨텍스트를 그대로 보여 줍니다. 압축·FIFO 로 캐시에서 빠진 카드는 흐려지고 "컨텍스트 밖" 꼬리표가 붙으며(접지 않습니다 — 무엇이 빠졌는지 보여야 요약을 믿을 수 있습니다), 빠진 구간 바로 뒤에 점선 `⊙ 압축 요약` 카드(요약 본문·파일 목록·대체한 턴 범위·전후 토큰)가 섭니다. 서버는 캐시가 바뀔 때마다(압축·FIFO·fold·resume 복원) 스코프별 sticky `ctx_view` 이벤트로 **캐시에 남은 첫 레코드의 history 서수 `gone.hidx`** 와 요약을 보내고, 모든 카드 이벤트(`assistant_turn`·`observation`·`user_message`·`agent_wake`)가 자기 레코드의 서수 `hidx` 를 실어 카드가 그보다 앞인지 판정합니다 — 압축은 항상 앞에서부터 비우므로 경계 하나면 되고, 턴 번호는 런마다 1 부터 다시 시작해 경계가 될 수 없습니다(실측). main·상주 에이전트 채널·인라인 카드 안이 같은 규칙입니다. resume 재생 카드도 레코드의 서수와 `turn` 을 싣습니다. 설계: `docs/inspector-model-view/DESIGN.md` (P2 드로어 재편·P3 인라인 머리말은 다음).
- **형식 넛지는 구조로 저장, 문장은 읽을 때 (v10.5.0)**: "저장은 방언과 독립" 원칙의 마지막 구멍을 막았습니다. 파싱 실패 관찰(`recovery: "format"`)이 방언이 조립한 안내 문장을 `content` 로 저장해, 다른 방언으로 resume 하면 "JSON 배열로 끝내라" 같은 엉뚱한 규칙이 재생됐습니다. 이제 레코드에는 `nudge: {reason: no_json|no_action, prior: <실패 원문의 경계 발췌>, syntax_error?, thinking_only?, swallowed?}` 만 남고, 모델이 읽는 문장은 라이브·캐시 렌더·resume 모두 같은 `build_format_nudge` 가 현재 방언으로 조립합니다. 웹 재생은 라이브와 같이 넛지 카드를 그리지 않고, `history` 는 `format nudge (<reason>): <발췌>` 로 검색됩니다. 다른 형식 개입(모르는 도구·스키마 불일치·answers 요구)은 하네스 문장이라 그대로입니다.
- **옛 이름 호환층 제거 (v10.4.0)**: models.json 의 옛 키 `wire_format` 은 더 이상 바인딩으로 읽지 않습니다(그 키만 있는 엔트리는 미설정). CLI 별칭 `--response-format` 과 import shim `agent_cli.wire_formats` 도 지웠습니다 — v11 로 미뤄 뒀던 정리를 "호환 없음" 규칙에 맞춰 당겼습니다. 보드 v1.33.0 이 같은 규칙으로 바인딩 없는 모델을 표시·차단합니다.
- **방언은 모델 바인딩이 정한다 — 세션 메타·기본값·부모 상속 제거 (v10.3.0)**: 해석 체인이 `--dialect`(세션 전체 강제) > models.json 모델 `dialect` 바인딩, 둘뿐입니다. 바인딩 없는 모델은 부트(main)와 spawn(서브에이전트) 모두 `No dialect for model '…'` 로 멈추고 models.json 에 `dialect` 를 적으라고 안내합니다 — 대화형 모델 등록도 `auto` 없이 방언 하나를 반드시 고릅니다. 서브에이전트는 main 과 같은 체인을 타므로 `--dialect` 가 서브 모델의 바인딩도 덮습니다. 함께, models.json 은 수정 시각이 바뀌면 다시 읽으므로 보드에서 바인딩을 고치면 **재시작 없이 다음 spawn 부터** 반영됩니다(살아 있는 에이전트의 방언은 spawn 시점에 고정). `session.jsonl` 의 `dialect` 키는 더 이상 쓰지도 읽지도 않습니다.
- **형식 넛지는 마지막 것 하나만 (v10.2.3)**: 파싱 실패가 이어지면 넛지(`recovery: "format"` 관찰)가 실패 횟수만큼 컨텍스트에 쌓였다가 다음 파싱 성공 때 한꺼번에 접혔습니다(v4.51.0 의 fold 는 성공만 해소로 봤고, docstring 의 "항상 최신 1개" 는 구현이 없었습니다). 이제 새 형식 넛지를 붙이기 직전에 앞선 미해소 넛지를 접고, resume 의 레코드 판정도 "뒤에 더 새로운 넛지가 있음" 을 해소로 봐 같은 뷰로 수렴합니다. 모델은 언제나 마지막 실패의 넛지와 그 원문 인용만 봅니다(사용자 결정 — 쌓인 실패 인용은 모방 재료). `history.jsonl` 과 챗 카드는 전부 남습니다.
- **배치 관찰 조각은 방언과 무관하게 기록 (v10.2.2)**: 한 턴에 op 가 여럿인 관찰은 `parts: [{tool, success, content}]` 를 항상 남깁니다. v10.2.0 은 native_fc 일 때만 남겨서, json_fc 로 돌린 세션을 native_fc 로 이어 가면 assistant 의 호출은 N 개인데 `tool` 메시지는 하나뿐이었습니다(엄격한 OpenAI 호환 서버는 400). 기록이 방언에 의존하면 렌더 추상화가 소용없다는 교훈 — 쓰기 쪽을 중립으로 되돌렸습니다. 이 버전 이전의 텍스트 방언 세션은 `parts` 가 없어 다중 op 턴이 native 에서 짝이 안 맞는 채로 남습니다(호환 처리 없음).
- **인스펙터가 native_fc 의 호출·스키마를 보여줌 (v10.2.1)**: 🔍 컨텍스트 인스펙터가 메시지의 `content` 만 읽어 native_fc 에서는 `[assistant]` 카드가 빈 채로 보였습니다(사용자 제보 — 호출이 `tool_calls` 필드에 있다). 이제 assistant 카드 본문에 호출이 `⚡ read_file {"path": …}  (call_1_0)` 줄로 적히고, 관찰 카드 라벨은 `[tool call_1_0]` 로 어느 호출의 결과인지 붙습니다. 프롬프트 대신 요청 `tools[]` 로 간 함수 스키마도 새 그룹 `Function schemas` 로 시스템 프롬프트 뒤·대화 앞에 함수마다 한 섹션씩 보이며 토큰 합계에 듭니다(서버 템플릿이 프롬프트에 그려 넣는 것이라). `-v` 의 `verbose.jsonl` `context` 레코드도 같은 표시 함수를 써서 호출 턴이 0자로 기록되지 않습니다. 텍스트 방언은 무변경.
- **서버 네이티브 함수 호출 방언 `native_fc` (v10.2.0)**: 요청에 함수 스키마(`tools`)를 싣고 호출은 서버가 파싱한 `tool_calls` 로 받습니다. 서버의 tool-call 파서가 `<tool_call>` 블록을 삼키는 게이트웨이(실측: LiteLLM + vLLM 류 — `tools` 없으면 블록 폐기, 있으면 `delta.tool_calls`)에서도 돌고, 모델이 학습한 네이티브 포맷을 그대로 씁니다. 선택은 바인딩뿐입니다(`--dialect native_fc` / 세션 메타 / models.json `dialect`) — 자동 감지는 하지 않고, `OUTPUT_SWALLOWED` 경고가 선택지로 언급만 합니다. 기록은 같은 `{thought, ops}` 라 json_fc 세션을 native 로, native 세션을 json_fc 로 resume 할 수 있습니다. 내보낼 때만 assistant 는 `tool_calls`, 관찰은 op 마다 `tool` 메시지(id 는 렌더 시 `call_<레코드>_<op>` 로 합성)이며, 관찰 렌더는 방언이 소유합니다(`render_observation_from_history`; 텍스트 방언은 user 텍스트). 시스템 프롬프트의 `## Available Tools` 는 빠지고(서버 템플릿이 넣음) 그 설명과 가이드 전문이 함수 `description` 으로 갑니다. 문법·구제·등가성·원문 기록은 이 방언에 없습니다(파싱 주체가 서버 — 투명성이 그만큼 줄어듭니다; `AGENT_CLI_DUMP_STREAM` 으로만 봅니다). OpenAI 호환 프로바이더 전용 — Anthropic 바인딩이면 부트에서 거부. 설계: `docs/dialects/NATIVE.md`.
- **값 안의 포맷 토큰은 텍스트 (v10.1.3)**: 태그 방언(xml_fc·glm_argkey)에서 닫힌 파라미터 값 안에 `<function=…>`·`<parameter=…>`·`<tool_call>` 같은 포맷 토큰이 있어도(문서·동료에게 보내는 형식 예시, 백틱·펜스 안 포함) 값이 잘리거나 유령 op(`shell {}`)가 생기지 않습니다. 파서가 이름 태그 직후부터 닫힌 파라미터를 순차 소비한 뒤에만 다음 호출을 찾고, 그 값 영역 안의 래퍼·태그는 드리프트 계수에서도 뺍니다. 미닫힘(절단) 값 안의 태그는 구조 토큰입니다 — 값이 어디서 끝나는지 알 길이 없어 보수적으로 자릅니다. 값 안에 `</parameter>` 바로 뒤 구조 토큰이 오는 완전한 형식 예시(펜스 안의 전체 tool_call 블록)는 여전히 모호해 그 자리에서 끊깁니다. 로컬 Qwen3.8 + xml_fc 실제 작업 12런에서 발견(파싱 실패 0, 그러나 값에 포맷을 적는 순간 깨지는 구조적 약점).
- **빈 응답(NO_OUTPUT) 세 경로 (v10.1.4)**: 회사 환경(Qwen + xml_fc)에서 turns.jsonl 의 `failure_signal` 이 `NO_OUTPUT` 으로 반복된 사고. ① 모델이 `<think>` 를 열고 닫지 않은 채 호출을 내면 **프로바이더**가 사고를 EOF 까지 삼켜 content 가 비었다 — 방언의 정지점(v7.11.4, 첫 구조 토큰에서 멈춤)은 프로바이더가 먼저 지운 뒤라 손도 못 댔다. 이제 루프가 방언의 `thinking_stop` 을 프로바이더 호출에 넘기고(OpenAI·Anthropic 경로 모두), 호출 직전에서 멈춘다. ② 서버의 tool parser(vLLM `--tool-call-parser` 등)가 `<tool_call>` 블록을 content 에서 빼내 `tool_calls` 필드로 돌려주면 content 가 빈다 — 그 호출들을 바인딩 방언의 캐노니컬 모양으로 되살려 같은 경로로 실행합니다. 스트리밍의 `delta.tool_calls` 조각도 index 별로 모아 같은 길로 보냅니다(v10.1.5 — v10.1.4 는 비스트리밍 응답만 받았다). ③ content 는 비었는데 사고 채널에 글이 있으면 재시도 문구가 "사고 채널에 갇혔다, 사고를 닫고 그 뒤에 호출을 써라" 고 말합니다. 진단: `turns.jsonl` 에서 `NO_OUTPUT` 턴의 `output_tokens` 가 크면 ①·③, `-v` 의 `verbose.jsonl` 에 `thinking` 이 차 있으면 확정. 둘 다 비었는데 `output_tokens` 가 있으면 서버가 다른 필드로 보낸 것 — `AGENT_CLI_DUMP_STREAM=<파일>` 로 돌리면 서버가 보낸 SSE 줄이 해석 전 그대로 덧붙여진다(v10.1.6, 호출마다 `### stream <시각>` 구분선). **실측 결론(v10.1.7)**: 회사 게이트웨이는 `completion_tokens: 33` 을 세고도 content 청크를 한 줄도 안 보냈다 — 서버측 tool-call 파서가 xml_fc 의 `<tool_call>` 블록을 삼킨 것(json_fc 의 bare 배열은 안 걸린다). 하니스는 이 조합(토큰 > 0, content·사고·tool_calls 전부 빔)을 `OUTPUT_SWALLOWED` 신호로 구분하고, 사용자에게 "json_fc 로 바인딩하거나 서버 파서를 끄라" 는 경고를 한 번 띄우며, 모델에겐 bare JSON 배열 우회(레지스트리의 타 방언 구제가 받는다)를 알린다.
- **구두점뿐인 지시·메시지 거부 (v10.1.2)**: `agent` 의 `task`(spawn/request/run), `message`·`reply` 의 `text` 에 글자나 숫자가 하나도 없으면(예: `>`) 보내지 않고 "no words — write it in words" 로 되돌립니다. 실측(board nnq141, 재현 5런·267건 중 8건): xml_fc + 문법 강제에서 Qwen3.8 Flash 가 `<parameter=task>\n>\n</parameter>` 처럼 구두점 하나짜리 지시를 냈고, 받은 에이전트는 빈 지시의 뜻을 찾느라 5턴을 썼습니다. 파서는 원문 그대로였습니다(원문 기록 `AGENT_CLI_RECORD_EMISSIONS=1` 로 확인). 언어 무관(유니코드 문자 클래스).
- **배치 중간의 입력 검증 실패 (v9.21.0)**: 한 턴의 여러 op 중 하나가 스키마 검증에 걸리면 그 op 만 `[k/N] — FAILED` 로 적고 나머지는 실행하며, 진짜 중단이 남으면 안 돌린 op 를 이름으로 알립니다.
- **턴을 끝내는 호출 뒤의 op (v10.177.7)**: `complete`·`wait`·`run_skill` 뒤에 온 op 은 실행하지 않고 `Not executed — \`wait\` ends the turn, so the calls after it did not run: [2/2] read_file. …` 한 줄로 알립니다(전엔 말없이 버렸다). 디코딩 문법은 종결 호출을 끝에만 허용하므로 문법 없는 서버·`native_fc` 에서만 생깁니다.
- **다중 인스턴스**: 같은 프로파일을 여러 명 스폰할 수 있습니다 — coder 3명이 서로 다른 파일을 병렬 개발하는 식. spawn 의 `name`(예: `ui`/`api`)으로 인스턴스를 구분하며 광고·대화창·회신 헤더에 `agt-x (coder · ui)` 로 표시됩니다. 모델의 도구는 이 이름을 key 대신 받습니다 — `message` 의 `to`(v10.1.0)와 `agent` 의 `kill`·`resume`·`status` 의 `key`(v10.177.7)가 같은 규칙입니다.
- **이름과 주소**: 화면은 **이름을 앞에, key 를 흐리게 뒤에** 답니다 — `🦊 code-writer · ui  agt-c83d4f82`. 프로파일도 `name` 도 없는 즉석 에이전트는 key 해시로 정해지는 별명(`🦁 조용한 사자`)으로 폴백합니다. 별명은 **표시 전용**이고 부를 때 쓰는 주소는 key 입니다(`@agt-<key>`; 도구는 spawn 의 `name` 도 받지만 별명은 받지 않습니다) — 그래서 이름 옆에 늘 key 가 붙어 있습니다. 별명은 key 의 순수 함수라 resume·재접속에도 같은 얼굴이 유지되고, 사람 닉네임 풀이 영어(`Funky Gecko`)라 사람과 에이전트가 한눈에 갈립니다.
- **Live Agents 광고**: 현재 상주 중인 에이전트 목록(key·프로파일·인스턴스명·전문영역)이 main 의 **세션 상태 블록** `## Live Agents` 섹션에 상시 광고됩니다 — compaction 이 spawn 관찰을 지워도, auto-spawn/resume 처럼 관찰이 없어도 모델이 자기 팀을 잊지 않습니다. **v8.46.0**: 시스템 프롬프트에서 꼬리로 옮기면서 `[busy · 2 queued]` / `[idle]` 같은 **휘발 상태까지 함께 실립니다** — 꼬리라 매 턴 바뀌어도 KV 프리픽스가 깨지지 않습니다. 프롬프트 인스펙터에서 그대로 확인 가능합니다. **v10.166.0**: 받아들여진 `wait` 로 기다리는 에이전트는 `[busy]` 대신 `[waiting]`(쌓인 요청이 있으면 `[waiting · 1 queued]`)으로 보입니다 — 작업 상태는 그대로 busy 이고(작업 구간 안에서 기다립니다), 모델에게 말하는 낱말만 다릅니다. **상주 에이전트에게도(5.11.0)** 자기 자신을 제외한 동료 로스터가 같은 `## Live Agents` 섹션으로 주입되어(단, `message` 도구 안내 문구), 누가 돌고 있고 어떤 역할인지 알고 서로 부를 수 있습니다 — 이쪽은 스폰 시 한 번 조립되는 **정적** 문자열이라 세션 중 변하지 않으므로 시스템 프롬프트에 그대로 남습니다(캐시 비용 없음). **한도 표시 (v9.24.7)**: main 의 제목에 살아 있는 수와 한도가 붙습니다(`## Live Agents (2/10 alive)`, 무제한이면 `∞`). 에이전트가 하나도 없어도 `(0/10 alive)` 로 한도를 알려 주고, 한도에 닿으면 "새 spawn·resume 은 하나를 kill 할 때까지 거부된다" 는 안내가 붙습니다. 서브에이전트의 peer 로스터에는 붙지 않습니다(spawn 을 못 하고, 스폰 때 한 번 만든 정적 목록이라). **v10.85.0**: main 머리글이 "기다리는 법"(내 일을 끝내고 `wait`, 회신이 깨운다, 회신을 예측하지 않는다, 의존 요청은 읽은 뒤에; `status` 폴링·재전송 금지)을 맡습니다 — 결과는 키·요청 id·"회신이 깨운다" 만 말하고, 결과·거절 문구의 호출 모양(`{"mode":"resume",…}`)·대문자 강조는 모두 문장으로 바뀌어 표기 가드가 봅니다.
- **🤝 대화창 resume 복원 (5.13.0)**: 웹 UI 의 상주 에이전트 대화창은 그 에이전트와 주고받은 요청/회신/질문을 `agents/<key>/conversation.jsonl` 에 남깁니다. **agent-cli 를 재시작하고 세션을 resume 하거나** dead 에이전트를 `mode:"resume"` 으로 되살리면, 이 로그를 재생해 대화창이 **원래 시각 그대로** 복원됩니다 (에이전트 내부 작업 상세는 메인 채팅의 접히는 작업 카드). `kill` 하면 대화창을 즉시 비우고(정리), resume 하면 다시 채웁니다 — "kill=정리 / resume=재생" 대칭. 로그 파일은 kill 후에도 남아 언제든 부활 시 복원됩니다. 받은 요청 줄은 그 요청이 받은편지함에 드는 것과 한 구간에서 이 파일에 적힙니다 (v10.146.4) — 종전엔 넣은 뒤에 적어, 받는 쪽이 먼저 답을 적으면 답이 요청 위에 기록돼 재생 순서가 뒤집힐 수 있었습니다(에이전트끼리의 `message`·`reply`, main 에게 보낸 메시지·정지 회신, 질문도 같음). 웹 화면의 이벤트는 그 기록과 같은 구간에서 나가 화면 순서가 이 파일의 순서를 따릅니다 (v10.146.5 — 종전엔 런 블록 머리에 같은 받은 항목이 두 줄 붙거나, 에이전트를 `mode:"resume"` 로 되살리는 도중 들어온 줄이 두 번 보일 수 있었습니다). 터미널(CLI)의 출력은 막힐 수 있어 그 구간 밖에서 냅니다 — 출력이 멈춘 터미널이 다른 에이전트의 배달을 세우지 않습니다.
- **auto-spawn**: frontmatter `auto-spawn: true` 프로파일은 세션 시작 시 자동 상주합니다 (resume 재생성분과 중복 스폰 없음).
- **worker 사망 통지**: worker 가 비정상 종료(ctx 생성 실패·내부 기계 예외)하면 main 에 `DIED` 관찰로 즉시 통지됩니다 — status 를 조회할 필요 없음. `kill`/세션 종료 같은 의도된 종료는 통지하지 않습니다.
- **부활 (`mode: "resume"`)**: kill 되거나 사망한 에이전트를 **이전 컨텍스트 그대로** 되살립니다 — history 가 디스크에 보존되므로 부활한 에이전트는 죽기 전 문답을 전부 기억합니다 (`task` 로 즉시 이어서 요청 가능, 회신 파일 번호도 이어감). 웹 대화 창의 dead 칩에서 ↻ 버튼으로도 부활할 수 있습니다. 부활 후에는 세션 resume 의 자동 재생성 대상으로 복귀합니다. 죽은 에이전트의 칩/기록이 남아 있는 이유가 바로 이것 — 사후 검사 + 부활 가능성.
- **스코프**: 상주 모드는 main 세션 전용 — 서브에이전트 안에서 spawn/request 등을 부르면 run 안내와 함께 거부됩니다. 동시 생존 상한 기본 **10**, 웹 UI 헤더의 숫자 입력으로 세션 한정 조절(무제한 체크박스 = 상한 없음; 상한을 낮춰도 기존 에이전트는 안 죽고 새 spawn 만 막힘).
- **수명**: main 의 Stop/Ctrl+C 는 상주 에이전트를 죽이지 않습니다(백그라운드 계속). 종료는 `kill`(✕) 또는 세션 종료 시 일괄 정리.
- **정지 — ■ 런 정지와 ✕ 제거 (v10.142.0, docs/agent-engine/DESIGN.md §2.3)**: 웹 채널 머리(채널 칩을 고르면 칩 줄 아래)에 `■ Stop run` · `✕ Remove` 가 있습니다(끝난 run 은 둘 다 없음 — 칩에 남는 조작은 죽은 상주의 ↻ 뿐). **■** 는 그 에이전트의 지금 런만 멈춥니다 — 상주는 남아 다음 대기 요청을 집고, run 에이전트는 맡은 일 하나가 수명이라 내려옵니다(회색 칩, 채널은 읽을 수 있음). **✕** 는 에이전트를 없앱니다(모델의 `agent` `kill` 도 같은 장치). 어느 쪽이든 멈춘 뒤엔 도구가 0회이고, 그 에이전트가 **빚진 상대 모두에게 회신**이 갑니다 — 종전엔 kill 이 빚을 조용히 버렸습니다:

  | 들고 있던 것 | ■ (상주) | ✕ · run 에이전트 |
  |---|---|---|
  | 지금 하던 일의 요청자 · 그 밖의 빚(`message` 로 맡긴 것) | stopped 회신 | 같음 |
  | 아직 시작 안 한 대기 요청 | 남김(다음 차례) | removed 회신(run 에이전트의 ■ 는 stopped — v10.146.6) |
  | 자기가 맡긴 하위 일 | 그대로(하위 계속) | 하위 run 은 함께 멈춤 · 하위 상주는 남음 |

  회신은 `── reply [n-7] from agt-3 re [r-21] · stopped by user ──` 봉투에 `The user stopped agt-3 while it was working on your request [r-21] ("…").` · `Last activity: …`(그 런의 마지막 호출 셋) · `Partial notes: …`(마지막 생각 앞부분, 있으면) · `Decide whether to continue another way or report back to the user.` 입니다(시작 전 제거는 `… removed by user ──` · `The user removed agt-5 before it started your request [r-32] ("…"). Nothing was done for it.`, run 에이전트를 ■ 로 멈췄으면 같은 모양에 `stopped` — `… stopped by user ──` · `The user stopped agt-5 before it started your request [r-32] ("…"). …`, v10.146.6). 회신이라 요청자의 기다리는 회신을 닫을 뿐 장부 항목을 만들지 않고, main 이 제자리에서 기다리던 중이면 그 회신에 깨어 같은 런에서 읽습니다. 모델의 `kill` 이면 주어가 지운 쪽입니다(`main stopped agt-3 …` · `stopped by main`). 사람이 채널에 직접 쓴 요청에는 회신하지 않습니다(창이 곧 배달). 에이전트가 스스로 내려가도(과제를 끝낸 run 에이전트 · 예외로 죽은 워커, v10.146.1) 아직 시작 안 한 대기 요청마다 같은 모양의 회신이 가고 줄이 닫힙니다 — `… · ended ──` · `agt-5 finished its task and is gone before it started your request [r-32] ("…"). Nothing was done for it.`, `… · died ──` · `agt-5 died before it started your request [r-32] ("…"). Nothing was done for it.` (요청자를 깨웁니다). 죽은 워커가 이미 집어 든 요청(지금 일·런 도중 흡수한 것)에도 `… · died ──` · `agt-3 died while it was working on your request [r-21] ("…").` 회신이 가고 빚이 닫힙니다(main 의 사망 통지는 그대로). API: `POST /api/agent/<key>/stop`(■), `POST /api/agent/<key>/kill`(✕).
- **재시작은 컨텍스트와 명단만 올립니다 (v10.160.0)**: 세션이 끝나면(정상 종료든 강제 종료든) 모든 것이 멈춥니다. `--resume`(웹 방 되살리기 포함)이 다시 올리는 것은 main 과 상주 에이전트의 **대화 이력**, 에이전트 **명단**(`agents.json` — 역할 프롬프트도 저장돼 프로파일 md 파일이 지워져도 무관; 상주는 한가한 상태로, 과제 도중 끊긴 run 에이전트는 죽은 채로), 예약, 화면 재생뿐입니다. 메일함(배달되지 않은 요청·회신·통지), 하던 런, 줄 선 요청, 빚, 제자리 대기, 사람에게 물어 둔 질문은 올리지 않고, **아무것도 스스로 이어 하지 않습니다** — 다음 런은 사람의 말·질의·예약이 엽니다. 대신 main 과 되살린 상주의 대화 끝에 사실 한 줄이 남습니다: `[interrupt] ⚡ The session restarted. Anything that was in progress before it was cut off and is gone: runs, background jobs, monitors, waits, requests and replies not yet delivered, and questions to the user. Nothing is owed for them and nothing more will arrive.`(main 은 Stop 처럼 관찰 카드로도 그립니다; 사이에 런 없이 거듭 재시작해도 한 줄). 무엇을 다시 맡길지는 모델이 이력을 보고 정합니다. 새 항목 번호는 기록 글(`history.jsonl`·`conversation.jsonl`)에 쓰인 번호 다음부터 잇습니다. 명시적으로 `kill` 한 에이전트는 되살아나지 않습니다. v10.158 이하가 쓴 `agents.json` 도 읽히지만 명단만 씁니다.
- **런 도중 도착한 요청도 바로 받습니다 (v9.25.0)**: 상주 에이전트가 런을 도는 동안 받은편지함에 들어온 요청·회신·질문을 **매 턴 경계에서 그 런으로 흡수**합니다. 흡수된 요청은 관찰(`── request [번호] from main (arrived while you were working) ──`)로 들어가고 요청별 빚으로 `## Owed Replies` 에 오르며, 각각 `reply(id=번호)` 로 갚습니다. 발신자가 달라도 받습니다(main·동료·사람). 사람이 그 에이전트 창에 쓴 메시지는 main 과 같은 회계(`## Open Requests`·`answers`)를 받고 그 런은 사람 런이 되어 `complete` 이 거부가 아니라 수락 후 독촉으로 처리됩니다. `ask` 의 `to` 는 `main` 이나 에이전트 키도 받습니다. 한계: 흡수는 턴 경계에서만 일어나므로 LLM 호출 하나가 도는 동안은 받지 못합니다.
- **모델 시점**: 상주 에이전트 채널도 main 과 같은 틀(Head·Evicted·Tail)로 그 에이전트가 받는 시스템 프롬프트·꼬리를 보여 줍니다. 요청 처리 과정은 run 과 같은 접이식 카드(🤝)로 표시됩니다.
- **사용자 직접 명령 (`@` 구문)**: 채팅박스(웹)나 CLI 에서 —
  - `@agents` — 프로파일 카탈로그 + live roster 통합 목록.
  - `@<profile> <task>` — 일회성 run (명시적으로 `@<profile>-run` 도 동일).
  - `@<profile>-spawn <task>` — 상주 스폰 (+초기 task). 하이픈 포함 프로파일명도 안전 — 전체 토큰이 실존 프로파일이면 그것이 우선.
  - `@agt-<key> <메시지>` — 특정 상주 에이전트에게 **직접** 전송 (`@agt-<key>` 만 치면 상태). 사용자 발신이므로 회신은 main LLM 대화에 섞이지 않고 — 웹은 🤝 창으로, **CLI 는 콘솔 라인**(`🦊 [code-writer · ui (agt-x) → user]`)으로 도착합니다.
- **웹 대화 창 + 인간 개입 (🤝)**: 웹 헤더의 🤝 버튼으로 에이전트 대화 창을 엽니다 — roster(상태 뱃지·✕ 종료)와 선택한 에이전트의 대화 스트림(요청 in / 회신 out / 질문 ❓)이 실시간으로 흐릅니다. 창 하단 입력으로 **사람이 직접 메시지**를 보낼 수 있고(닉네임 attribution), 에이전트가 질문(ask) 대기 중이면 그 메시지가 답으로 소비됩니다 (main 과 선착순). **인간↔에이전트 문답은 창에만 표시**되고 main 컨텍스트에는 배달되지 않습니다 (오염 방지).
- **idle 자동 재기동 (웹)**: main 이 입력 대기(idle) 중에 회신/질문이 도착하면 harness 가 run 을 **자동으로 깨워** 배달합니다 — 사용자가 다음 메시지를 보낼 때까지 회신이 잠들지 않습니다. 이미 다른 run 이 배달을 끝냈으면 빈 run 을 열지 않습니다.
- **CLI `run` 커맨드에서도 동작 (큐 펌프)**: `run` 커맨드는 web 과 같은 공용 입력 큐 위에서 돕니다 — 모델이 complete 해도 **상주 에이전트가 아직 일하는 중이거나 미배달 회신이 있으면 프로세스가 끝나지 않고**, 회신이 도착하면 자동으로 이어서(🤝 wake) 처리한 뒤 조용해졌을 때(quiescence) 종료합니다. 상주 에이전트를 안 쓰면 1회 실행 후 즉시 종료. 답변 없는 질문만 남은 경우(교착)는 경고 후 종료하며 resume 시 STALE 처리됩니다.
- **run 과 web 이 같은 루프를 돕니다 (v10.33.0, `agent_cli/pump.py`)**: 큐에서 꺼내 → 깨우기 판정 → `/`·`@` 라우팅 → 런 → 결과 처리 → 정지 판정이 한 벌이고, run 은 "조용해지면 끝", web 은 "서버 종료까지" 라는 수명 정책과 표면(콘솔/브라우저)만 다릅니다. 그래서 run 에서 바뀐 것 셋: ① **런 도중 주입** — 런 중에 울린 예약 발화가 런이 끝난 뒤 새 런으로 돌지 않고 web 처럼 **다음 턴 경계에 요청으로 합류**합니다(콘솔에 `📨 [예약 이름]: …` 한 줄; 합류한 요청에 답하기 전엔 런이 닫히지 않으며 `--max-turns` 가 상한). ② `@agt-<key> <메시지>`·`@agents` 가 회신을 기다리며 폴링하다 찍고 끝나던 것이, web 처럼 회신이 **런을 깨워 모델이 관찰로 받습니다**(LLM 턴 한 번). ③ `/skill`·`@agent task` 디스패치 뒤에도 답변-대기 경고가 메인 경로와 같이 나옵니다. `--result-file` 은 argv 질의 런의 답만 받습니다(v10.34.1, 옵션 표 참조).

### run_skill — 스킬 실행

등록된 스킬을 도구로 호출합니다. LLM이 스스로 판단하여 적절한 스킬을 선택합니다. 시스템 프롬프트에 사용 가능한 스킬 목록이 안내됩니다.

```json
{"action": "run_skill", "action_input": {"name": "optimize", "arguments": "./"}}
{"action": "run_skill", "action_input": {"name": "summarize", "arguments": "README.md"}}
```

스킬 내부에서는 별도 ReAct 루프가 실행되며, 결과가 artifact로 저장됩니다. `disable-model-invocation: true`인 스킬은 LLM이 호출할 수 없습니다 — 시스템 프롬프트 `## Available Skills` 목록과 이름이 틀렸을 때의 "Available:" 안내에서도 빠지고, 모델이 부를 수 있는 스킬이 하나도 없으면 `run_skill` 도구 자체가 광고되지 않습니다(v10.100.0).

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

- **루프 종류 객체 (v10.87.0, docs/loop-kinds/DESIGN.md)**: main·상주·one-shot·스킬·하니스 작업 다섯 루프가 한 엔진(`AgentLoop`) 앞뒤의 가변부(주소·포트·ctx·스코프·결과·정리)를 `LoopKind` 다섯 구현으로 가집니다. 겉으로 보이는 변화: `/skill` 명령도 `OnSkillStart`/`OnSkillEnd` 훅을 받고, 스킬 ctx 가 사고·샘플링·압축 비율 노브를 부모에서 물려받으며, 상주 에이전트 안에서 돈 스킬이 그 에이전트의 역할과 도구 범위로 돌고, 하니스 작업 주소가 `harness:<job>:<label>` 로 라벨까지 가집니다. 모델이 보는 프롬프트는 그대로입니다. **v10.88.0**: 엔진이 종류에게 묻습니다 — `wait` 정책(런 종료·제자리 차단·거부), 턴 경계에 어디서 받을지, 프롬프트 윤곽(역할·`agent` 스키마·`ask` 차단·종결 어투), 꼬리 로스터, 렌더 여부가 종류 객체의 명시 값이고 플래그 추측은 없습니다. 보이는 변화 하나: main 아래에서 돈 스킬이 main 의 에이전트 회신을 더는 흡수하지 않습니다(스킬이 끝난 뒤 main 이 받습니다). **v10.89.0**: 상주 에이전트의 런 마감(회신 계약·폴백·`run_ended`)이 단건·사람 묶음·크래시 한 벌로, 하니스 통지의 레코드와 소유자 정리(잡·모니터·python 워커·메일함)가 main·상주·one-shot·하니스 작업에서 각각 한 함수입니다 — one-shot 이 끝날 때 남긴 python 워커도 이제 거둡니다.

### 도구 호출 방식

모든 프로바이더가 텍스트 파싱 방식을 사용합니다 (ReAct JSON 출력 → 파싱 → 도구 실행).

| 프로바이더 | JSON 출력 보장 | 파싱 |
|-----------|--------------|------|
| OpenAI-compat | `dialect: json_object` (basic JSON mode) | JSON 파싱 |
| Anthropic | tool calling | 3단계 폴백 파싱 |

Strict JSON Schema(OpenAI `json_schema`)는 **사용하지 않습니다**. 일부 온프렘 서버/모델 조합에서 strict 스키마가 깨지는 이슈가 있어, 확장성을 위해 basic JSON mode만 사용. ReAct 구조 강제는 시스템 프롬프트 + 3단계 파서가 담당합니다 (32B+ 권장 사양에서 실질 품질 손실 거의 없음, 7-14B는 포맷 drift 가능).

### 3단계 파싱 폴백

1. **Stage 1**: `json.loads()` (직접 파싱)
2. **Stage 2**: JSON 복구 (깨진 JSON 자동 수정)
3. **Stage 3**: Regex 추출 (최후 수단)

Thinking 모델(`<think>...</think>`)은 파싱 전 자동 분리됩니다.

### 경로 표기 규칙 (상대경로)

에이전트 프로세스는 **cwd 가 고정**입니다 — `os.chdir` 도, 서브프로세스에 넘기는 `cwd=` 인자도 코드베이스에 없습니다. 모든 파일 도구가 `Path(path)` 라 상대경로는 그 cwd 기준으로 풀리고, `shell` 도 cwd 를 상속합니다. 즉 `src/main.c` 와 그 절대경로는 **같은 파일**을 가리킵니다. `_confine` 은 오히려 상대경로를 워크스페이스 안으로 간주해 그냥 통과시키고, 워크스페이스 밖 절대경로에만 확인을 요구합니다.

그런데 시스템 프롬프트는 `Working directory:` 한 줄만 알려줄 뿐 **상대경로를 쓰라는 지시가 없었습니다.** 실제 세션 히스토리를 세어보니 **절대경로 23 : 상대경로 2** 로, 모델이 습관대로 절대경로를 씁니다. 그 경로는 `action_input` 에 실려 **매 턴 재공급**되고 compaction 파일 목록에도 누적되므로 비용이 반복 청구됩니다.

```
/Users/idujeong/workspace/agent-harness/data/ws/b5rztq/worm_game.html   69자
worm_game.html                                                          14자
```

그래서 Environment 섹션이 규칙을 명시합니다 — 도구 인자는 상대경로, **작업 디렉토리 밖 파일**과 **최종 답변에서 사용자에게 위치를 알려줄 때**만 절대경로. 루프 자신도 같은 규칙을 지킵니다(위 `display_path`) — 넛지가 절대경로를 뱉으면 매 over-cap 마다 반대를 가르치는 셈이 되기 때문입니다.

### 세션 상태 블록 (프롬프트 꼬리)

매 턴 바뀌는 **휘발 상태**를 시스템 프롬프트가 아니라 **마지막 메시지 뒤**에 덧붙입니다.

```
── end of message · everything below is added by the harness on every turn — the user neither writes nor sees it ──

── standing rules (always in effect) ──
## Task Guidelines
- Read a file before changing it …
- Scratch files — … backups — go in `.agent-cli/tmp/` under the working directory … copy the original there first …
…

── session state (context only — not part of the conversation) ──
turn 23/40 (17 left after this one)

## Open Requests
   [2] (Ann) "REQ-B: 이건 REQ-A 와 별개다"
   [3] (Bob) "로그도 같이 봐줘"

## Owed Replies
Settle these before you `complete` — the harness holds the run open while they are unpaid:
  - [7] reply to agent:agt-orch: "floorCeil.js 가 깨졌다 — 최우선으로 고쳐줘" → reply(id="7", text="...")

## Live Agents (2/10 alive)
- `agt-ui` (code-writer · ui) [busy · 2 queued] — Writes UI code.
- `agt-dt` (explorer · dts) [idle] — Explores device trees.

## Session Memory (2) — pull full detail with `memory` with `mode` "get" and the entry's `id`; "(turn N)" is the turn it was noted on, which `history` can look up
⚠ #1 [failure] uart0 클럭 미설정 시 부팅 로그 없음 (turn 7)
🔀 #2 [decision] dts 대신 defconfig 로 수정하기로 (turn 12)

ℹ Context compaction is coming up: older turns will be replaced by a structured summary
(task, state, decisions, failures, key facts) and work continues as usual. If a detail is
worth more than its summary — an exact identifier, a failed approach — `memory` mode `add`
keeps it verbatim, … Pace and scope stay the same.
```

**왜 꼬리인가 (KV 프리픽스)**: 프로바이더는 프롬프트의 **접두사**를 캐시하고, 이전 호출과 처음 달라지는 토큰에서 재사용이 끊깁니다.

- **시스템 프롬프트에 두면** — 그 섹션 이후 전부(= 남은 시스템 프롬프트 + **대화 전체**)가 무효화됩니다. `memory add` 한 번이 수만 토큰 재계산을 유발했습니다. 이 압박 때문에 로스터는 휘발 상태를 못 실었고, `Environment` 섹션은 날짜를 일부러 뺐습니다.
- **사용자 요청 바로 뒤에 두면** — 에이전트 루프에서 사용자 요청은 프롬프트 **앞쪽**(요청 1개 + 그 뒤로 assistant/observation 이 N턴 쌓임)이라, 매 턴 블록이 바뀌면 그 뒤 모든 턴이 무효화됩니다. 직관과 반대로 최악입니다.
- **꼬리에 두면 공짜입니다** — 턴 N 이 `… obs_{N-1} + STATE_N` 로 끝나고 턴 N+1 은 `… obs_{N-1}, assistant_N, obs_N + STATE_{N+1}`. 접두사 일치는 `obs_{N-1}` 에서 끝나는데, 이건 `assistant_N`·`obs_N` 이 새 토큰이라 **어차피 거기서 끝났을 자리**입니다. 잃는 건 이전 블록 자신의 토큰 몇십 개뿐이고, 손실이 **대화 길이와 무관하게 상수**입니다. 그리고 꼬리는 recency attention 이 가장 강한 위치라, "지금 이걸 보고 판단하라"는 내용에 맞습니다.

**경계선 (v9.25.2)**: 꼬리는 사용자 메시지 **본문에 이어** 붙으므로, 모델이 받는 메시지는 `이건 왜 필요한거야?` 바로 뒤에 `## Task Guidelines` 가 오는 모양입니다. 실제로 한 모델이 그 질문에 가이드라인을 설명했습니다 — "이건" 의 가장 가까운 후보가 가이드라인이고, 사용자가 이 블록을 쓰지도 보지도 않는다는 것을 모델은 알 수 없기 때문입니다. 그래서 꼬리의 첫 줄이 그 사실을 말합니다(`the user neither writes nor sees it`). 규칙이 아니라 사실이며, "무시해도 된다" 로 읽힐 말은 넣지 않습니다 — 규칙 헤더의 `always in effect` 는 그대로입니다.

**전달 방식**: 새 메시지를 추가하지 않고 **마지막 메시지 본문에 덧붙입니다** — 프로바이더가 `messages` 를 그대로 전송하므로(`providers/anthropic.py`) 끝에 `role=user` 를 하나 더 붙이면 연속 동일-롤 턴이 되고 처리 방식이 프로바이더마다 갈립니다. `_OBS_COMPLETE_NUDGE` 와 같은 기구로 **feed 시점에만**(복사본에) 적용되며 **`history.jsonl` 에 절대 저장되지 않습니다**(저장하면 낡은 숫자가 매 턴 재공급되고 resume 프리뷰가 오염됩니다).

**같은 말은 한 자리에 (v10.76.0)**: 프롬프트 지도 리뷰에서 같은 지시가 두세 곳에 있던 것을 정리했습니다. 도구 결과 뒤에 붙는 넛지는 이제 "(If nothing remains to do, call `complete`.)" 한 문장입니다. `wait` 안내는 꼬리 `## Open Items` 꼬리말과 Task Guidelines 의 긴 명령 불릿이 맡습니다. 넛지는 무조건 붙어서, `wait` 가 없는 서브·스킬 루프에도 없는 도구를 가리키고 있었습니다. "추론을 짧게" 는 꼬리 Task Guidelines 한 곳에만 남겼고(머리 Context Discipline 에서 삭제), "결과를 정직하게 보고하라" 는 `complete` 의 `Checked:`/`Not checked:` 틀이 맡습니다(Task Guidelines 에서 삭제).

**갚을 회신 (`## Owed Replies`, v9.24.5)**: 다른 에이전트가 `message`/`ask` 로 보낸 요청·질문 중 아직 회신·답하지 않은 것을, 그것을 갚는 도구와 함께 매 턴 보여 줍니다(main·에이전트 모두 `reply`, v10.141.0). **v9.25.0**: 빚은 요청마다 하나이고 번호가 붙습니다 — `reply(id="번호", text="…")` 로 갚습니다 — 갚는 것은 `reply` 하나다(v10.145.1; 종전엔 요청자에게 보낸 `message` 도 가장 오래된 빚을 갚았다).

**`answer` 는 에이전트 질문 전용 (v10.30.0)**: 사용자 메시지는 꼬리 `## Open Requests` 에 `[1] (닉) "하이"` 처럼 번호로 실리고, `answer` 도구 설명은 "나한테 온 질문에 답하라, id 는 질문과 함께 왔다" 였습니다. 모델(Qwen3.6-35B, 방 dj72nx)이 그 둘을 이어 `answer(id="1", text="안녕하세요!")` 를 불렀고, 인사말은 질문 포트에서 거부돼 사용자에게 가지 않았으며 다음 턴은 영어 메타 문장("Greeted the user back…")으로 끝났습니다. 두 군데를 고쳤습니다 — ① 설명이 "다른 에이전트가 `[question q-… from …]` 봉투로 보낸 질문에만; Open Requests 의 번호 요청은 사용자 메시지라 `complete` 의 `result` 로 답하고 `answers` 에 번호를 적는다" 고 못 박고, `id` 설명도 "봉투의 q-… id, Open Requests 번호가 아님". ② 그래도 요청 번호로 `answer` 를 부르면 관찰이 `'1' is user request [1], not an agent question — reply to it in complete's result and set answers: ["1"]` 로 길을 알려 줍니다. 실측(Qwen3.6-35B-A3B-8bit, 웹 세션에 "하이" 8회씩): main 은 8런 중 5런이 `answer(id="1")` 을 불러(총 10회 거부, 1런은 세 번 거부되고 답을 못 냄) 첫 턴에 사용자에게 답이 간 런이 1/8 이었고, 후보는 8/8 첫 턴 `complete` 로 한국어 인사, 오용 0.

**마지막 턴 안내 (v9.24.1)**: 턴 한도(`--max-turns`, 하위 에이전트는 부모 값 상속)가 있으면 턴 줄에 남은 턴 수가 붙고, 마지막 허용 턴에는 "이번이 마지막 턴이다, 새 작업 대신 complete 로 확인한 것·확인 못 한 것·열린 것·만들거나 바꾸거나 망가뜨린 파일을 보고하라"는 안내가 붙습니다. 한도에서 루프는 마무리 없이 멈추기 때문입니다 — Harbor 실측에서 60턴을 쓴 하위 에이전트가 마지막 턴에도 다음 실험을 계획했고, 부모는 명령 목록만 받아 "실험 중 `/app/mac.vim` 을 덮어썼다"는 사실까지 잃었습니다. 하위 에이전트에게는 "호출자는 complete 에 쓴 것만 받는다", 메인에게는 사용자를 대상으로 말합니다. 완성 안 된 결과를 과제 산출물에 쓰라는 게 아니라 **상태를 보고하라**는 안내입니다. 한도가 없으면(기본 0) 붙지 않습니다.

**꼬리에 사용량 수치 없음 (v10.9.0)**: 꼬리에는 `context: ~206,333 / 209,715 tokens (98% — compaction at 100%)` 같은 사용량 줄이 없습니다. 실측(67qcmb, 2026-10-02): 상주 에이전트가 목표치의 95~99% 에 두 시간 머물며 모든 편집을 "half-edit 위험" 으로 거부하고 TAKEOVER·"STOPPING at 99%" 로 끝냈는데, 한 턴에 몇백 토큰만 보태니 100% 를 끝내 못 넘어 압축이 한 번도 돌지 않았습니다. 그 숫자가 모델에게 준 유일한 지렛대는 "일을 줄이기" 였고, 그게 곧 해악입니다. 압축은 하니스의 몫입니다 — 턴 사이에만 돌고, 오래된 턴을 구조화 요약으로 바꾸며, 응답을 자르지 않고(출력 예산은 별개 — `compaction_ratio` 가 창의 20% 이상을 출력 몫으로 남김), 작업은 이어집니다. 시스템 프롬프트의 Context Window Discipline 도 "차면 오래된 정보가 떨어지고 추론 품질도 떨어진다" 에서 "차면 오래된 턴이 자동으로 구조화 요약으로 바뀌고 작업은 이어진다" 로 바꿨습니다. 안내 단계는 70·80·90% 셋에서 **80% 하나**로 줄였습니다(퍼센트 줄이 없으니 단계별 안내가 기댈 숫자가 없고, memory 힌트는 한 번이면 족함).

**모델에게 가는 문구는 전부 영어, `thought` 용어 정리 (v10.11.1)**: 프롬프트 전수 조사에서 모델 입력에 닿는 한글 네 문장을 찾아 영어로 바꿨습니다 — `monitor` 의 `deadline`/`every` 형식 오류 둘(`parse_duration`; CLI `--stall` 오류도 같은 문구), 프로필이 요구한 모델이 서버에 없을 때의 spawn 거절(이제 "다른 프로필로 spawn 하거나 사용자에게 알리라" 는 다음 행동 포함), 내장 create-agent 스킬의 예시 과제. 사람에게만 보이는 표면(확인 대화상자·콘솔 알림·웹 UI)은 그대로 한글입니다. 또 응답 형식은 "brief reasoning as plain prose" 인데 Context Window Discipline 이 여전히 "Every thought" / "Keep `thought` short" 라고 하던 것을 "Every line of reasoning" / "Keep your reasoning brief" 로 맞췄습니다 — 없는 필드 이름은 `## Thought` 헤더를 되살릴 빌미이고, 그 헤더 반복은 폭주로 잡힙니다. `complete` 를 숨기는 방언용이던 `exposes_complete` 플래그와 ask 안내 변형(`_ASK_INLINE_NO_COMPLETE`, "a thought-only turn")은 쓰는 방언이 없어 삭제했습니다. 회귀 테스트(`tests/test_prompt_language.py`)가 다섯 방언의 시스템 프롬프트·내장 프로필/스킬에 한글이 없고 `thought` 가 없음을 지킵니다.

**프롬프트 인벤토리를 코드에서 뽑습니다 (v10.15.0)**: 하니스가 모델에게 하는 말 전부를 한 파일로 내는 스크립트입니다. 손으로 정리한 목록은 프롬프트를 고칠 때마다 낡았습니다(세 릴리스 만에 여덟 군데가 달라졌습니다).

```bash
python -m agent_cli.prompts.inventory -o inventory.json   # -o 없으면 stdout
python -m agent_cli.prompts.inventory --html inventory.html   # 훑어보는 페이지 (v10.16.0)
```

루프가 쓰는 것과 **같은 함수**로 조립하므로 실행한 커밋의 실제 프롬프트입니다. 담기는 것: ① 방언 다섯 × 상황 셋(메인 / 서브에이전트 / 도구를 `read_file`·`shell` 로 좁힌 서브에이전트)의 시스템 프롬프트 — 섹션 이름·본문·토큰 추정치, `native_fc` 는 요청에 실리는 함수 스키마까지. ② 내장 도구별 설명·인자·안내. ③ 실행 도중 끼어드는 문구(형식 넛지, 반복 호출 개입, 압축 안내, 꼬리 블록 머리말 등) — 이것만 프롬프트 조립 함수 밖에 있어 `_runtime_texts` 의 손 목록입니다. Environment 와 프로젝트의 스킬·프로파일·DIRECTIVE.md 는 실행 위치에 따라 달라지므로 출력에 `cwd` 를 남깁니다 — 내장 기준선은 저장소 루트에서 돌리세요. 출력 파일은 커밋하지 않습니다. `--html` 은 같은 데이터를 **파일 하나짜리 페이지**로 냅니다 — 방언·상황을 고르고 섹션을 펼쳐 본문과 토큰 수를 봅니다. 데이터가 페이지 안에 들어 있어 네트워크도 빌드도 필요 없고, 디스크에서 바로 열거나 그대로 게시하면 됩니다.

**도구 제약이 방언·경로와 무관하게 같은 뜻이 되도록 (v10.14.0)**: `native_fc` 에서 스킬·프로파일의 도구 제약이 동작하는지 점검하다 나온 다섯 건입니다(제약 자체는 동작했습니다 — 요청의 함수 목록이 좁혀지고, 목록 밖 호출은 실행 없이 거절됩니다). ① `native_fc` 에서 형식 거절(모르는 도구·인자 오류) 뒤 다음 요청에 **짝이 없는 `tool` 메시지**가 실렸습니다 — 거절된 호출은 저장하지 않는데 관찰은 `tool` 로 렌더해, 앞에 호출이 없거나 이미 답한 호출 id 를 다시 썼습니다. OpenAI 메시지 규칙 위반입니다(omlx 는 받아 줍니다). 이제 관찰은 **바로 앞 레코드가 호출을 가진 assistant 일 때만** `tool` 이고, 아니면 `user` 메시지입니다. ② 거절 문구의 `Available:` 목록에 `complete` 가 빠져 있던 것을 프롬프트와 같은 함수(`effective_tool_names`)로 만듭니다. ③ MCP 도구 이름이 `{server}.{tool}` → **`mcp__{server}__{tool}`** — 점은 함수 이름 규칙에 없는 문자입니다. 프로파일·스킬의 `allowed-tools` 나 훅 matcher 에 옛 이름이 있으면 고쳐야 합니다(호환층 없음). ④ `## MCP Tools` 섹션을 없앴습니다 — MCP 도구는 이미 Available Tools 에 실리는데 섹션이 한 번 더 설명했고, 도구 목록을 좁힌 루프에도 못 쓰는 도구를 광고했습니다. ⑤ `agent` 호출의 `tools` 가 프로파일의 `allowed-tools` 를 대체하던 것을 **프로파일이 상한**이 되게 고쳤습니다.

**도구 안내를 같이 쓰는 것끼리 묶고, 같은 말을 한 번만 합니다 (v10.13.0)**: ① 시스템 프롬프트의 도구 순서를 용도별로 다시 묶었습니다 — 파일(`read_file`→`edit_file`→`write_file`→`code_index`), 실행(`shell`·`fetch`·`monitor`·`schedule`), 기억(`history`·`memory`), 사람/에이전트(`ask`/`answer`·`message`/`reply`·`agent`·`run_skill`), `complete`. 순서는 프로파일의 `allowed-tools` 순서와 무관합니다. ② "수정은 edit, 새 파일·작은 파일의 전체 재작성은 write" 규칙을 `write_file` 설명 한 곳에만 둡니다. 같은 편집이 거듭 실패하는 작은 파일은 다시 써도 된다고 명시합니다. ③ `edit_file` 안내의 "CURRENT turn 에 읽어라" 는 지킬 수 없는 지시였습니다(읽기 결과는 다음 턴에 옵니다) — "그 줄을 보여 준 가장 최근 출력의 ref 를 쓰고, 파일이 바뀌기 전의 ref 는 다시 읽어라" 로 고쳤습니다. ④ 서브에이전트의 `agent` 도구는 설명·스키마·예시·Agent Profiles 모두 `run` 만 보여 줍니다. ⑤ `code_index` 설명을 한 문장으로 줄이고 모드·범위 규칙·defconfig 는 안내에만 둡니다. defconfig 는 모델이 직접 씁니다("사용자에게 요청" 문구 삭제). ⑥ `run_skill` 이 없는 루프에는 Available Skills 섹션을 내지 않습니다. 로컬 omlx(Qwen3.8-Flash-Next, json_fc)에서 과제 8개 × 5회를 main 과 번갈아 돌린 결과: 완주 40/40 대 40/40, 모델 호출 189 → 174, 형식 실패 18 → 8, 도구 오류 21 → 12, 해시 불일치 1 → 0, 첫 호출 입력 9,550 → 9,131 토큰. 과제당 5회라 차이의 크기보다 "나빠진 과제가 없다" 는 쪽만 근거로 삼습니다.

**예약 실행이 agent-cli 의 기능이 됐습니다 (v10.12.0)**: 스케줄러를 agent-board 에서 이 프로세스 안으로 옮겼습니다(`agent_cli/schedule/`, 설계 `docs/schedule/DESIGN.md`). `schedule` 도구는 항상 등록되고, 보드와의 요청·회신 파일 계약(`schedule-requests.jsonl`/`schedule-state.json`)과 `AGENT_CLI_SCHEDULER` 게이트는 삭제됐습니다. 예약은 세션 폴더에 저장되고, 켜진 예약이 있으면 `web`·`run` 모두 꺼지지 않으며, 꺼져 있던 동안 지난 발화는 자동 실행하지 않고 묻습니다. web 헤더에 ⏰ 서랍(목록·추가·이력)과 놓친 예약 카드가 생겼습니다. 함께, 같은 세션을 두 프로세스가 여는 것을 기동 시점에 거부합니다(`session.lock`). **agent-board 는 1.34.0 이상과 짝지어 쓰세요** — 그 전 보드는 자기 스케줄러로 따로 발화합니다.

**없는 것을 가리키던 프롬프트 문구 정리 (v10.11.2)**: 프롬프트 검토에서 나온 다섯 건입니다. ① `json_fc` 형식 규칙 6 은 "태그·`##` 헤더 금지" 에 범위가 없어 HTML 파일 쓰기나 마크다운 `result` 까지 금지로 읽혔습니다 — 금지 문구는 그대로 두고, 이것이 **턴을 쓰는 방식**에 대한 규칙이며 값(파일 내용·`result`·메시지 본문) 안은 자유라는 한 문장을 덧붙였습니다(금지를 약하게 고쳐 쓴 판은 omlx 실측에서 나아진 것이 없어 채택하지 않았습니다). ② `run_skill` 설명이 내장에 없는 스킬 이름(`optimize`, `review-code` 등)을 예로 들던 것을 Available Skills 목록 참조로 바꿨습니다. ③ 내장 `create-agent` 스킬의 예시가 v10 이전의 중첩 `action_input` 모양이던 것을 평평한 모양으로 고쳤습니다. ④ Context Recovery 가 `history.jsonl` 을 `read_file` 로 통째로 읽으라던 것을 `history` 조회로 바꾸고, 그 도구가 노출된 루프에서만 냅니다. ⑤ 오류 문구의 `action_input`, 실행 컨텍스트의 `delegate` 같은 옛 용어를 `arguments`/`agent` 로 바꿨습니다. `tests/test_prompt_language.py` 가 재유입을 막습니다.

**폭주 생성 조기 종료 + 잘린 호출 기록 (v10.10.0)**: 같은 방에서 생성 셋이 셸 명령의 `node -e "` 뒤에서 탭·공백·채움 문자로 무너져 32,768 토큰 출력 상한까지 달렸습니다 — 각 27~40분, 합계 106분, 결과는 전부 폐기. 방언 수준 조기 종료(v8.41.0)는 트리거 문자(`#`/`<`)가 있는 청크에서 헤더 반복만 보므로 공백 폭주를 보지 못했습니다. 이제 스트림이 **내용 무관 감지기**(`providers/runaway.py`)를 청크마다 돌립니다 — 공백만 2,048자 이어지거나(`whitespace_run`) 마지막 4,096자에 영숫자가 2% 미만이면(`no_words`; 한글·CJK 는 낱말로 셈, `0,0,0,…` 맵 격자는 절반이 숫자라 안 걸림) 스트림을 닫고 `stop_reason="runaway"` 로 돌아옵니다. 루프는 출력 상한 절단과 같은 계약으로 처리합니다 — 실행하지 않고, 실제 내용까지만 인용해 "폭주했으니 더 작은 단위로 다시" 라고 알리며(컨텍스트 탓이 아님을 분명히), 모델이 복구하면 접힙니다. 그리고 **잘린 호출도 turns.jsonl 행**이 됩니다: `failure_signal` OUTPUT_TRUNCATED/RUNAWAY, `stop_reason` length/runaway, `stop_detail` — length 는 `model_cap`(모델 출력 상한) / `context_clamp`(하니스가 창에 맞춰 줄인 max_tokens 에 도달 — 컨텍스트가 생성을 끝내는 유일한 경로) / `server_cap`(요청보다 적게 생성하고 멈춤 — omlx 는 스스로 줄임), runaway 는 감지 규칙 이름.

**압축 안내 (v9.26.4)**: 압축은 캐시가 호출별 목표치를 넘는 순간 돕니다. 안내는 **단계를 처음 넘는 턴에 한 번**만 붙고(`COMPACTION_NOTICE_STEPS`), 압축이 돌아 사용량이 내려가면 그 단계는 다시 무장됩니다. **v10.9.1**: 안내가 `memory` 의 `add` 와 함께 **더는 맞지 않는 메모리(해결된 블로커·뒤집힌 결정)는 `delete`(id 지정)로 지우라**고도 말합니다(v10.75.1 부터 모델에게 가는 문장은 `memory(mode=…)` 같은 호출 모양 대신 "`memory` mode `add`" 처럼 도구 이름과 인자를 말한다 — native_fc 에는 텍스트 호출 문법이 없다) — 메모리 색인은 매 턴 꼬리에 실리고 압축을 그대로 통과하므로, 어느 항목이 낡았는지 아직 판단할 수 있는 압축 전이 정리할 때입니다. 문구는 결핍이 아니라 사실을 말합니다 — 오래된 턴은 사라지는 게 아니라 구조화 요약으로 **대체**되고 작업은 그대로 이어진다, `memory` 는 원문 그대로 남기고 싶을 때의 선택지. 컨텍스트 압박을 알리면 모델이 조기 `complete` 로 도망갈 수 있다는 실측(`_OBS_COMPLETE_NUDGE`)은 그대로 유효하므로 "끝내지 마라" 류의 문장도 넣지 않습니다(역설적으로 끝낼 생각을 심음). 이 블록은 요청에는 들어가지만 `system` 이 아니라, 압축 예산에서 크기를 별도로 예약합니다.

### 이미지 입력 (vision, v10.59.0)

> 설계: `docs/vision/DESIGN.md`

모델이 이미지를 보는 길은 둘입니다. **사용자가 보여 주기** — `agent-cli run "이 캡처의 오류가 뭐야?" --image shot.png`(반복 가능), 웹은 입력창에 **붙여넣기·끌어놓기·📎**(v10.60.0). **모델이 열기** — `view_image` 로 PNG/JPEG/GIF/WebP 를 엽니다(v10.77.0, fetch 가 `.agent-cli/tmp/downloads/` 에 받은 그림, 모델이 `shell` 로 그린 차트도 같은 길). 모델이 습관대로 `read_file` 로 이미지를 열면 하니스가 그 호출을 `view_image` 호출로 바꿉니다 — 기록·컨텍스트에도 바뀐 호출이 남고(원래 호출은 레코드의 `rewritten_from`), 결과에 따로 알리지 않습니다. 이미지를 못 본다고 알려진 모델(`supports_vision: false`)에는 `view_image` 가 없고, `read_file` 은 이미지를 `is not a text file` 한 줄로 돌려줍니다.

- **기록은 참조, 내용은 보관본** — history.jsonl 에는 경로·크기·sha256 만 적히고(base64 없음) 글에는 자리표시 줄 `[image: path (W×H)]` 이 남습니다. 참조를 만들 때 내용을 `.agent-cli/images/<sha256>.<ext>` 에 복사해 두고(v10.101.0, 같은 내용은 한 번), 보낼 때·웹 썸네일을 그릴 때 그것을 읽습니다 — 같은 경로에 덮어써도 지난 카드와 모델이 보는 대화는 그때의 그림입니다. 보관본이 없는 이전 기록은 파일을 해시로 확인해 읽고, 없거나 바뀌었으면 그 사유가 글로 갑니다(웹 썸네일은 깨진 그림으로 보입니다). 보관소는 저절로 지워지지 않으며 지워도 안전합니다 — 그 뒤엔 이전 기록과 같이 동작합니다.
- **두 공급자** — OpenAI 호환은 `image_url` data URL, Anthropic 은 `image` base64 블록. 서버 네이티브 함수 호출(`native_fc`)에서는 `tool` 메시지에 이미지를 넣지 않고 바로 뒤 user 메시지에 붙입니다(omlx 가 tool 안 이미지를 조용히 버리는 것을 실측).
- **능력 확인** — `models.json` 의 `supports_vision`; 없으면 두 색 이미지를 보내 답을 검사하는 프로브를 한 번 돌리고 판정을 엔트리에 적는다(v10.68.0). 못 보는 모델에는 이미지를 보내지 않고 모델·사용자 양쪽에 말합니다(`--image` 는 즉시 오류).
- **크기와 창** — 토큰은 픽셀에 비례합니다(≈ 1K px/token, 1080p ≈ 2K 토큰). 긴 변이 `AGENT_CLI_IMAGE_MAX_EDGE`(2048)를 넘으면 축소, 5MB 를 넘으면 JPEG 재인코딩. **축소는 Pillow 가 있을 때만** 합니다(v10.61.1, `pip install 'agent-cli[image]'`) — 없으면 원본을 그대로 보내고(토큰만 더 듦, 정확도는 같음) 5MB 를 넘는 것만 사유를 적고 거절합니다. 크기 읽기·능력 프로브는 Pillow 없이 됩니다. 이미지는 붙은 결과와 함께 컨텍스트에 남고 **압축이 그 결과를 요약할 때만** 빠집니다(v10.77.0 — 장수 상한은 없습니다). 압축 때 요약에는 그림에서 읽은 것을 적는 IMAGES 항목이 붙고, 요약 뒤 `## Images viewed in earlier turns` 목록이 무엇을 봤는지 남깁니다. 이미지 토큰은 관찰의 과대 출력 캡과 턴 예산에 들어갑니다.
- **웹 (v10.60.0)** — 입력창에 스크린샷을 붙여넣거나(클립보드 파일) 파일을 끌어다 놓거나 📎 로 고르면 즉시 `.agent-cli/tmp/uploads/<epoch>-<이름>` 에 올라가 입력창 위에 칩으로 섭니다(✕ 로 뺌). 보내면 그 경로가 메시지와 함께 가고(글 없이 이미지만도 됨), 에이전트가 바쁘면 다른 메시지처럼 대기열에 섰다가 턴 경계에 주입됩니다. 사용자 말풍선과 `view_image` 결과 행에 썸네일(최대 160px, 클릭하면 원본)이 붙고 `--resume` 재생에도 남습니다. 썸네일은 `GET /api/workspace/file?path=` 가 워크스페이스 안 **이미지만** 서빙합니다. 모델이 못 보는 것으로 확인되면(`supports_vision: false` 또는 첫 이미지 런의 프로브) 📎 가 숨고 붙여넣기에 한 줄로 알립니다 — 미확인이면 받되 그 런 시작에 `●` 한 줄로 알립니다. 에이전트 채널도 같은 📎 로 받습니다(아래).
- **글 없이 이미지만 (v10.147.1)** — v10.142.0~v10.147.0 에서는 웹 main 에 글 없이 이미지만 보내면 명령 판정(`/stop` 인지 보는 자리)이 빈 글에서 예외를 내 main 워커가 죽었고(그 뒤의 말은 대기열에만 섰습니다), 런 도중 턴 경계에 오면 그 런이 오류로 끝났습니다. 이제 빈 글·공백만 있는 글은 명령이 아니고, 이미지만 든 말도 런을 열거나 주입됩니다.
- **모델이 찍기 (v10.61.0)** — `browser` 의 `screenshot` 모드가 PNG 를 `.agent-cli/tmp/screenshots/` 에 두고 `read_file` 과 같은 모양(자리표시 줄 + 참조)으로 보여 줍니다. **MCP 도구가 돌려준 이미지 조각**도 `.agent-cli/tmp/mcp/` 에 파일로 두고 같은 길로 갑니다. PNG/JPEG/GIF/WebP 가 아니면 저장만 하고 사유를 적습니다.
- **에이전트 채널 (v10.61.0)** — 상주 에이전트와의 대화창에서도 같은 📎·붙여넣기·끌어놓기로 보냅니다. 그 에이전트 런의 시작 메시지에 이미지가 실리고, ⏳ 대기 줄과 런 머리에 썸네일이 붙으며 `--resume` 재생에도 남습니다(질문의 답에는 못 붙입니다).
- **에이전트 채널에 이미지만 (v10.147.2)** — v10.61.0~v10.147.1 에서는 에이전트 채널에 글 없이 이미지만 보내면 레지스트리가 "empty message" 로 거절했고(404, 입력창은 비워져 아무 일도 없는 것처럼 보였습니다), 에이전트가 일하는 도중에 보낸 말은 턴 경계에 흡수될 때 이미지가 빠졌습니다. 이제 이미지만 든 말도 일감이고, 흡수된 말도 런 시작과 같이 자리표시 줄과 이미지를 싣습니다.
- **명령에 붙인 이미지 (v10.149.3)** — 사람의 말에 딸린 이미지는 그 말이 가는 곳으로 갑니다. main 입력창에서 📎 와 함께 보낸 `@agt-<key> 글`·`@<profile>-spawn 과제`·`@<profile> 과제`·`/<skill> …` 은 그 에이전트·스킬 런의 시작 메시지에 이미지가 실립니다(에이전트 채널과 같은 모양). 종전엔 펌프가 연 차례·런 도중·제자리 대기 모두 이미지를 버렸습니다. 말이 갈 곳 없는 명령(`@agt-<key>` 상태·`@`·`@agents`·`/skills`·`/stop`·`/help`·`/compact`·`/sh`)은 그대로 돌고 `This command does not take images — the attachment was not sent` 한 줄이 섭니다.
- **main 입력창 `@agt-…` 의 작성자 (v10.149.4)** — main 입력창에서 보낸 `@agt-<key> 글`·`@<profile>-spawn 과제` 는 에이전트 채널에 쓴 것과 같은 작성자(`user:<별명>`)로 갑니다 — 에이전트가 받는 시작 글이 `[user:<별명>]: …` 입니다(종전엔 늘 `[user]:`). 별명이 없는 `run` 의 argv 질의·터미널 줄은 `user` 그대로. 회신은 종전대로 그 에이전트의 채널 창으로 갑니다. 예약 발화도 같은 규칙입니다 — 예약이 낸 `@agt-<key> 글` 은 그 예약의 별명(기본 `Scheduler-<이름>`)으로 `[user:Scheduler-<이름>]: 글` 이 되고, 회신은 같은 채널 창으로 갑니다.
- **표시** — CLI 는 `🖼 shot.png (1280×720)` 한 줄(주입된 메시지의 이미지도 같은 줄).

### 세션 & 컨텍스트 관리 시스템

토큰 budget 기반 컨텍스트 관리. 매 호출 직전 캐시가 압축 목표치를 넘으면 LLM 요약 압축(compaction)이 돌고, 요약이 실패하거나 모자라면 FIFO eviction 으로 떨어지며, 모든 변경은 `history.jsonl`에 영속화됩니다.

#### 컨텍스트 윈도우 레이아웃

```
┌─────────────────────────────────────────────────────┐
│                  System Prompt                       │
│  ┌───────────────────────────────────────────────┐  │
│  │ Role / Task Guidelines / Format Rules         │  │  ← Primacy (고정, KV cache hit)
│  │ Available Tools (inline guides)               │  │
│  │ Available Skills / Available Agents           │  │  ← Middle (참조용)
│  │ Execution Context (call stack)                │  │  ← 스킬/agent 실행 시만
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
│  │ [Touched files] a.py, b.py, <agent:foo>       │  │
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
- 스킬/agent 서브에이전트는 부모 budget 상속

#### Context Compaction (목표치 초과 시)

**창이 곧 트리거 (v10.11.0)**: 기본 `compaction_ratio` 는 **1.0** 입니다. 하니스는 요청마다 `max_tokens` 를 `창 − 프롬프트 − 512` 로 줄이므로 창이 차면 출력 몫이 줄고, 생성이 그 클램프에 닿아 `length` 로 끝나면(`stop_detail=context_clamp`) 루프가 **그 자리에서 압축**(`compact_now`, 오래된 절반을 요약)하고 모델에게 "창이 차서 잘렸고 이제 압축해 자리가 돌아왔으니 그대로 다시 내라" 고 알립니다(`CONTEXT_CLAMP_NOTICE` — 출력 상한 절단의 "더 작은 단위로" 와 구분: 줄이라는 교훈이 아님). 압축이 아무것도 못 걷어내면 평소 절단 알림입니다. 즉 압축은 추정 비율이 아니라 **실제로 창이 필요해진 순간**에 돕니다 — 67qcmb 에서 고정 80% 목표치가 필요보다 일찍 압축하고, 그 퍼센트를 본 모델이 목표치 바로 아래서 두 시간 멈췄던 데 대한 답입니다. 비용은 잘린 생성 한 번(클램프 크기 이하)뿐이고, 작은 응답은 창이 거의 찼어도 그대로 지나갑니다. 비율을 낮추면 고정 비율에서 미리 압축합니다. 예방 목표치는 비율과 무관하게 **요청의 최소 출력 몫(1,024 + 여유 512)** 을 항상 남깁니다 — 1.0 에서 캐시가 창을 다 채워 창을 넘는 요청이 나가지 않도록.

매 LLM 호출 직전, 캐시가 그 호출의 목표치 `context_window × compaction_ratio − 시스템 프롬프트 − 세션 상태 − 최소 출력 몫` 을 **넘으면** 단순 FIFO drop 대신 LLM 요약 압축이 실행됩니다(비율 1.0 에서는 거대한 관찰 등이 창을 한 번에 채운 경우의 안전망). 별도의 90% 임계는 없고, 꼬리에는 사용량 수치가 없습니다(v10.9.0).

1. **분할**: `[system anchor][dynamic]` — system prompt만 무조건 보존
2. **Evict 절반 (token-based)**: oldest 절반을 떼어냄
3. **LLM 요약**: evict 묶음을 단일 호출로 요약. 요약은 구조화 섹션(TASK / STATE / DONE / PENDING / DECISIONS / FAILURES / FACTS)으로 만들어져, 에이전트가 요약만으로 작업을 이어갈 수 있게 남은 작업·실패한 시도·정확한 식별자(경로/명령/에러)를 보존합니다. 이전 요약이 있으면 prior summary를 같은 호출에 prepend (recursive single-call — 합치는 별도 단계 없음). 요청은 **경계 모양**이 먼저입니다(v10.123.0): 본 호출과 같은 요청(system·tools·messages)을 마지막으로 떼어 내는 메시지까지 잘라 보내고 뒤에 압축 지침을 붙여, 서버의 접두 캐시가 요약 호출에도 듣습니다. 요청이 창을 넘거나 응답이 요약이 아니면(빈 본문·제목 없음·오류) 발췌 전사로 한 번 다시 요약합니다. 요약 호출은 사고를 끄고 출력은 16,384 토큰 상한(v10.122.3)
4. **파일 경로 추출**: evict 안의 `read_file/write_file/edit_file/code_index` 호출과 `<agent:target>` placeholder를 누적 file_list에 dedup 머지
5. **재구성**: `[system][summary][file_list][retained dynamic]`
6. **영속화**: `compaction.json` (version, summary, file_list, dynamic_start_index 등) — `--resume` 시 압축 상태 그대로 복원
7. **Belt-and-braces fallback**: LLM 호출 실패 또는 재구성된 cache가 여전히 budget 초과면 플레인 FIFO drop으로 떨어뜨림 — 무한 트리거 루프 방지

압축은 항상 켜져 있으며, LLM 요약이 실패하거나 재구성된 cache가 여전히 budget을 초과하면 belt-and-braces 플레인 FIFO drop으로 안전하게 폴백합니다. 이 동작은 agent·skill 서브에이전트에도 그대로 전파됩니다.

**수동 `/compact` (web)**: 요약 호출도 본 호출처럼 스트리밍으로 나갑니다(v10.122.1 — 비스트리밍 요청은 게이트웨이가 첫 바이트를 기다리다 504 로 끊었다). 결과는 셋으로 구별됩니다(v10.122.2): 줄었으면 `Compacted: A → B tokens.`, 내보낼 것이 없으면 `Nothing to compact (…)`, 요약기가 실패하면 `Compaction failed: <사유>`(실패 카드), 압축이 꺼졌으면 `Compaction is off in this session.` — 실패해도 캐시는 그대로이고 FIFO 로 버리지 않습니다.

압축이 일어나면 CLI는 한 줄 상태로, 웹은 **대화창 인라인 시스템 라인**(`⊙ 컨텍스트 압축됨 X→Y tok`)으로 진행을 표시합니다. 압축으로 흡수된 요약·파일 목록은 웹 채널 맨 위 Head 틀에서도 확인할 수 있습니다(위 참조).

#### 디렉토리 구조

```
{project}/.agent-cli/
  sessions/                             # AGENT_CLI_SESSIONS_DIR 로 통째 이전 가능
    {session_id}/
      session.jsonl                     # 메타데이터 (1줄: id, workspace, updated_at, query)
      history.jsonl                     # 전체 대화 기록 (JSON Lines, append-only)
      agents/{key}/                     # 에이전트(run·spawn) — history.jsonl·conversation.jsonl
                                        #   (v10.140.0 까지의 세션은 run 을 run_{name}_{hash}_{ts}/ 에 두었다)
      skill_{name}_{hash}_{ts}/         # skill subdir
        history.jsonl                   #   skill 내부 대화
        result.md                       #   skill 최종 결과
```

#### Context Recovery

FIFO에서 밀려난 과거 메시지가 필요할 때:
- System prompt 가 `history` 조회를 안내 (Context Recovery Guide — 그 도구가 노출된 루프에만)
- LLM이 `history` 로 필요한 행만 SQL 조회하여 과거 맥락 복구
- history.jsonl 내 artifact 경로로 run/skill 상세 결과 접근 가능

#### 세션 관리

```bash
agent-cli sessions                     # 세션 목록
agent-cli web --resume <session_id>   # 이전 세션 이어서 작업
```

### MCP (Model Context Protocol) 지원

외부 MCP 서버의 도구를 agent-cli에서 사용할 수 있습니다. MCP 클라이언트 SDK(`mcp`)는 **기본 설치에 포함**됩니다 (v8.62.0 — 그 전에는 별도 설치가 필요했고, 없으면 서버 연결이 stderr 경고 한 줄로 조용히 실패해 도구만 사라졌습니다). 필요한 판은 **`mcp>=1.9,<3`** 입니다(v10.154.0, 종전 1.6) — streamable-http 전송이 제대로 도는 첫 판이기 때문입니다(1.6·1.7 에는 이 전송이 없어 그 서버의 연결이 import 오류로 실패했고, 1.8 의 클라이언트는 요청을 하나씩만 보내 상한으로 포기한 호출 하나가 그 연결을 막았습니다). 1.6~1.8 이 깔린 환경은 `agent-cli update`(또는 다시 설치)할 때 pip 가 mcp 를 함께 올립니다.

#### 등록 — `agent-cli mcp` (v9.1.0)

손으로 JSON 을 짜는 대신 마법사를 쓰세요. **저장 전에 실제로 연결해 도구 개수를 세고**, 원격 서버는 **HTTP 전송 세대를 자동 판별**하며(문서의 URL 만 봐서는 streamable-http 인지 구 sse 인지 알기 어렵습니다 — 판별 결과는 파일에 명시 저장됩니다), `${VAR}` 는 현재 셸에 값이 있는지 확인해 보여줍니다 — 붙지 않는 설정은 파일에 남지 않고, 토큰 오타는 저장 전에 드러납니다.

```bash
agent-cli mcp                 # 등록 목록 + 실제 연결 진단 (실패 이유·설정 파일 경로 표시)
agent-cli mcp --no-test       # 연결 없이 목록만 (스크립트용; 파이프 뒤에서 프롬프트로 멈추지 않음)
agent-cli mcp add             # 대화형 등록: 이름 → 로컬/원격 → 명령·인자 또는 URL → env → 연결 테스트 → 저장
agent-cli mcp test <이름>     # 하나만 다시 연결해 보기
agent-cli mcp remove <이름>
```

`agent-cli mcp` 의 종료 코드는 실패한 서버가 하나라도 있으면 1 입니다 (`agent-cli mcp && …` 로 스크립트에서 쓸 수 있게). **`.agent-cli/mcp.json` 을 읽지 못하면(v10.123.4)** — JSON 오류·객체가 아님·`mcpServers` 가 객체가 아님 — `agent-cli mcp` 는 `Could not read <경로>: <오류>` 와 종료 코드 1, `mcp add`·`mcp remove`·`mcp test` 는 같은 문장으로 거절하고 **파일을 건드리지 않습니다**. `run`·`web` 부팅은 `[warn]` 한 줄 뒤 MCP 없이 진행합니다. 읽지 않는 옛 `~/.agent-cli/mcp.json` 이 남아 있으면 마법사가 **이 프로젝트로 가져올지 묻습니다** — v9.0.0 스코프 정리의 마이그레이션 경로입니다(원본은 지우지 않습니다).

연결 실패 이유는 mcp SDK 가 `ExceptionGroup` 으로 감싼 것을 잎까지 풀어서 보여줍니다(부팅 시 `[warn]` 줄도).

#### 웹 — 🔌 칩 (v9.2.0)

`agent-cli web` 헤더의 🔌 칩이 부팅 시 붙인 MCP 서버 상태를 보여줍니다. 배지는 **붙은/전체**(`1/2`)라 하나라도 실패하면 숫자만 보고 알고, 칩 색이 경고색으로 바뀝니다. 클릭하면 서버별 전송 방식·도구 수·이름, 실패한 서버는 **이유**가 나옵니다 — 지금까지 이 정보는 stderr 에만 있어 웹에서는 볼 방법이 없었습니다. 등록된 서버가 없으면 칩은 숨겨집니다.

**읽기 전용**입니다. 등록·수정은 `agent-cli mcp` 에서만 합니다 — MCP 등록은 `command` + `args` 로 **로컬 프로세스를 실행**하는 설정이라, 링크를 가진 사람이 서버 머신에서 임의 명령을 돌릴 수 있게 되는 웹 UI 의 신뢰 수준을 넘습니다. 그래서 `POST /api/mcp` 는 존재하지 않습니다(테스트로 고정). 부팅 시점 상태라 세션 중엔 바뀌지 않습니다 — 서버를 추가하면 재시작해야 붙습니다.

#### 설정 파일

`.agent-cli/mcp.json` 에 저장됩니다 (v9.0.0 — 프로젝트만; `~/.agent-cli/mcp.json` 은 읽지 않음. MCP 서버는 로컬 프로세스를 띄우는 프로젝트 성격의 설정입니다). 직접 편집해도 됩니다:

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
- **Streamable HTTP**: `url` + `transport: "streamable-http"` — MCP 2025-03-26 의 원격 전송(현재 권장). `streamable_http` · `http` 도 같은 뜻으로 받습니다. mcp 2.x 에서도 붙습니다(v10.154.0 — 전에는 2.x 에서 `cannot import name 'streamablehttp_client'` 로 연결되지 않았습니다).
- **SSE**: `url` + `transport: "sse"` — 구 HTTP+SSE 전송
- **로컬/원격은 `url` 키의 유무로 결정**됩니다. `url` 이 없으면 `stdio` — `transport: "sse"` 를 적어도 `url` 이 없으면 stdio 로 취급됩니다. `url` 이 있는데 `transport` 를 생략하면 **`sse`** 입니다(기존 설정 호환). 신규 서버는 대개 Streamable HTTP 이니 `agent-cli mcp add` 로 등록하거나 `transport` 를 명시하세요.

> 구 `sse` 설정으로 Streamable HTTP 서버에 붙으면 서버가 `-32600 "Not Acceptable: Client must accept both application/json and text/event-stream"` 으로 거절합니다. 이 경우 agent-cli 가 **"전송 방식이 다릅니다 — 이 서버는 Streamable HTTP 입니다"** 로 안내합니다 (v9.3.0).
- `${VAR}` — 환경 변수 참조. **`env` 블록 안에서만** 치환되며(`command`·`url` 에는 적용되지 않음), **정의되지 않은 변수는 빈 문자열**이 됩니다(에러 아님 — 토큰 오타는 인증 실패로만 드러납니다).
- 한 서버 연결이 실패해도 나머지는 연결되고 에이전트는 계속 돕니다. 실패는 stderr 경고 한 줄뿐이니, 도구가 안 보이면 stderr 를 먼저 확인하세요.
- **연결 상한 45초** (v10.152.0) — 부팅 때 서버들에 **동시에** 연결하고, 서버마다 45초(`initialize`·도구 목록까지) 안에 준비되지 않으면 그 서버만 `no response within 45s — the server started but does not answer initialize (check the package name and arguments)` 로 실패합니다(마법사의 연결 테스트와 같은 값·같은 문장). 전에는 답하지 않는 서버 하나가 `run`·`web` 부팅을 끝없이 막았고, 서버가 여럿이면 차례로 기다렸습니다. 부팅 줄·🔌 칩의 순서는 설정 순서 그대로입니다. 서버별로 바꾸는 키는 없습니다.
- **종료** (v10.152.0) — 세션이 끝나면 연결마다 세션과 전송을 닫아 stdio 서버 프로세스가 실제로 끝납니다. 전에는 해제가 실패해(오류는 삼켜졌다) 서버가 남을 수 있었고, mcp 2.x 에서는 종료가 돌아오지 않았습니다. macOS 에서 mcp 1.x 로, 스스로 끝나지 않는 서버(답이 없거나 호출을 붙든 서버)를 끝낼 때 SDK 가 stderr 에 `Process group termination failed for PID … falling back to simple terminate` 를 한 줄 낼 수 있습니다 — 다른 방법으로 넘어갔다는 안내이고 서버는 정상 종료됩니다.
- **`call_timeout_s`** (v10.149.0) — 그 서버의 도구 호출 하나를 기다리는 상한(초). 생략하면 **120**. 양의 수만 받습니다(`2.5` 같은 소수 가능) — `0`·음수·문자열(`"30"`)·`true` 면 그 서버는 `Invalid config for '<이름>': call_timeout_s must be a positive number of seconds, got …` 로 **연결되지 않습니다**(`command`·`url` 이 없을 때와 같은 길 — 부팅 줄과 🔌 칩에 보입니다). 마법사는 이 키를 묻지 않습니다 — 파일로만 정합니다. `mcp add` 로 같은 이름을 **덮어쓰면** 항목 전체를 새로 쓰므로 이 키도 사라집니다(`env` 처럼).

  상한 안에 답이 없으면 기다리던 요청을 취소하고 모델에게 `MCP mcp__<서버>__<도구> did not answer within 120s, so the call was given up. The server may still be working on it.` 를 돌려줍니다. 런을 멈추면(■·Ctrl+C·`agent` `kill`) 기다림도 그만두고 `This run was stopped, so the MCP call was given up after 3s.` 입니다. agent-cli 는 서버에 취소 통지를 보내지 않습니다 — mcp 1.x 에서는 서버가 하던 일을 계속할 수 있고 늦게 온 답은 버려집니다. mcp 2.x SDK 는 취소된 요청에 통지(`notifications/cancelled`)를 스스로 보내 서버가 그만둡니다. 어느 쪽이든 같은 연결로 다음 호출은 그대로 됩니다. main 과 에이전트가 같은 서버를 동시에 불러도 됩니다(v10.152.0 — 전에는 뒤의 호출이 `This event loop is already running` 으로 실패했습니다).

#### 사용

세션 시작 시 자동 연결됩니다 (`run`·`web` 양쪽 — web 은 v4.46.0 부터). 도구가 돌려준 **이미지 조각**은 v10.61.0 부터 `.agent-cli/tmp/mcp/` 에 파일로 두고 모델에게 이미지로 보여 줍니다("이미지 입력" 절). 서버가 도구 오류(`isError`)로 답하면 그 글은 그대로 두고 **실패**로 기록합니다(v10.177.7 — 전엔 성공으로 기록됐다). MCP 도구는 `mcp__{server}__{tool}` 이름으로 LLM이 자동 사용합니다 (v10.14.0). 함수 이름 규칙(영문자·숫자·`_`·`-`, 64자)에 맞춘 이름이라 `native_fc` 를 포함한 모든 방언에서 같습니다. 서버·도구 이름의 다른 문자(점 등)는 `_` 로 바뀌고, 64자를 넘으면 앞부분 + 전체 이름의 해시 8자로 줄입니다. 프로파일·스킬의 `allowed-tools` 나 훅 matcher 에 옛 이름을 적어 두었다면 새 이름으로 고쳐야 합니다.

```json
[{"action": "mcp__github__list_issues", "repo": "owner/repo"}]
```

#### 대안: Playwright MCP 서버

내장 [`browser` 도구](#브라우저-도구-browser)(v10.39.0) 대신 Microsoft 의 Playwright MCP 서버를 붙일 수도 있습니다 — 스크린샷·네트워크 디버깅·드래그·파일 업로드처럼 내장 도구가 일부러 빼 둔 기능이 필요할 때.

```json
{
  "mcpServers": {
    "playwright": {
      "command": "npx",
      "args": ["-y", "@playwright/mcp@0.0.83", "--headless", "--isolated"]
    }
  }
}
```

알고 쓸 것(2026-10 실측, 내장 도구를 만든 이유이기도 합니다): Node.js 와 npm 접속이 필요하고, 0.0.x 라 버전마다 도구가 바뀌며(고정 권장), 도구 25개의 설명이 매 요청에 실려 첫 요청이 약 5,000토큰 늘고(10,595 → 15,630), 도구를 고를 수 없어 `browser_run_code_unsafe`·`browser_evaluate` 같은 임의 코드 실행 도구가 항상 열려 있습니다. 기본 브라우저는 설치된 Google Chrome 이고 없으면 `--browser chromium` + `npx -y @playwright/mcp@0.0.83 install-browser chrome-for-testing`. 큰 페이지 스냅샷(위키백과 59K–89K자)은 과대 출력 캡으로 파일에 저장됩니다.

### 스트리밍 출력

모든 프로바이더(OpenAI, Anthropic)에서 LLM 응답을 실시간 스트리밍합니다:
- ASCII-art 말하는 얼굴 애니메이션 + 누적 토큰 카운터 (한 줄, 시간 기반 throttle)
- 스트리밍 완료 후 thought/action을 Markdown 렌더링
- 토큰 throughput 표시: `ttft: 200ms | in: 1024 tok (892 tok/s) | out: 156 tok (201 tok/s)`
- 누적 (v10.136.0): 턴 줄의 `Σin`·`Σout` 은 이 런의 호출마다 보낸 입력(캐시 포함, `ctx` 와 같은 정의)과 받은 출력의 합. 런이 끝나면(`Session … saved` 앞) 합계 한 줄 — `Total: in 1.4M (cached 1.1M) · out 9.1K · 23 calls · LLM 4m12s`. `cached` 는 그중 캐시 적중, `LLM` 은 호출마다 ttft+생성 시간의 합이고 0 이면 빠집니다. 범위는 main 런 하나 — 깨우기 런(에이전트 회신·작업 통지)은 자기 줄을 따로 찍고, 서브에이전트 호출과 압축 요약 호출은 들지 않습니다. 웹은 런 끝 줄 없이 상단 칩(`Σ↑`·`Σ↓`·툴팁의 호출 수)이 같은 합을 보입니다. 1,000,000 이상은 `M` 으로 줄입니다(`ctx` 창 크기 포함)
- TTFT (Time-to-First-Token) 모든 프로바이더에서 클라이언트 측정
- 프리픽스 캐시 적중 (v10.128.0): OpenAI 호환 서버가 `prompt_tokens_details.cached_tokens` 를 주면 줄 끝에 `cache hit: 98.0K` 가 붙고, `in` 의 속도(tok/s)는 캐시 아닌 토큰만으로 잰다 — 적중 턴의 프리필 속도가 부풀지 않는다. `in`·`ctx` 는 전과 같이 캐시를 포함한 프롬프트 전체다. 추론 토큰(`completion_tokens_details.reasoning_tokens`, `out` 안에 든 몫)은 `--verbose` 기록·`turns.jsonl` 에 실린다

### 안전장치

- **반복 호출 감지**: 동일 도구를 같은 파라미터로 3회 연속 호출 시 자동 중단
- **echo-as-final**: `echo`로 답하는 소형 모델 패턴 자동 감지 → `complete` 도구 호출로 변환
- **잘린 JSON 복구**: LLM 응답이 잘릴 때 JSON repair 후 마지막 불완전한 edit 라인 제거, 적용된 edit 수 리포트
- **Execution Context**: 스킬/agent 실행 시 call stack + `depth N/M` 을 시스템 프롬프트에 표시, 재귀 호출 억제. 한계 도달 시 그 사실도 명시.
- **agent_stack / skill_stack**: 런타임 재귀 방지 (A→B→A 차단). 블록 메시지에 3가지 recovery option 포함 (다른 접근 / complete / ask).
- **max_depth**: **skill + agent 합산 중첩 깊이** 제한 (기본 2). 한계 도달 시 `AgentLoop.__init__` 가 `agent` 와 `run_skill` 둘 다 tool list 에서 제거 (대칭). dispatch 단계 belt-and-suspenders check 도 동일 메시지 출력.

## 프로젝트 구조

```
agent_cli/
├── main.py              CLI 명령어 (run, web, setup, sessions, update)
├── loop.py              AgentLoop 클래스 + ReAct 에이전트 루프
├── config.py            config.json 3레이어 로딩 + models.json 레지스트리
├── setup.py             SetupWizard (첫 실행 설정 마법사)
├── constants.py         공유 상수 (타임아웃, 임계값)
├── hooks/               Hook 시스템 (Python + Shell 라이프사이클 훅 11개 이벤트)
├── render/              플러그인 렌더링 시스템 (minimal — 커스텀 추가 가능)
├── providers/           LLM 프로바이더 (Anthropic, OpenAI)
├── dialects/        dialect 플러그인 (json_fc/xml_fc 내장, 추가 가능; 파서·복구·history 표현 self-contained)
├── tools/               도구 (read/write/edit/shell/agent/context)
├── context/             컨텍스트 관리 (compaction + FIFO + history.jsonl + 세션 메타)
├── prompts/             조건부 시스템 프롬프트
├── skills/              프롬프트 스킬 시스템 (로더, 실행기, 모델)
├── agents/              에이전트 정의 (builtin: explorer)
└── mcp/                 MCP 통합 (config, client, adapter, wizard — `agent-cli mcp`)
```

상세 아키텍처: [docs/ARCHITECTURE.md](ARCHITECTURE.md)

## 테스트

```bash
# 전체 유닛 테스트 (통합 테스트는 서버 미가용 시 자동 skip)
pytest tests/ -v
```

엔진(`agent_cli.loop.run.run_loop(kind, start, ctx)`)은 루프 종류를 받는다. 종류 없이 부품(provider·capabilities·model·ports·ctx)으로 엔진을 돌리거나 세우는 테스트는 `tests/loopkit.py` 의 `run_loop(...)`·`make_loop(...)` 를 쓴다 — 정적 바인딩과 맨 종류를 거기서 만든다. 협력자(`ToolBridge`·`TurnDispatcher`·`LLMCaller`)를 루프 없이 세우는 테스트의 설정은 `make_config(...)`.

### omlx 통합 테스트 (실 서버 필요)

`omlx_integration` 마커가 붙은 E2E 테스트는 실제 OpenAI 호환 omlx 서버를 대상으로 엔진 루프·툴 사용·스킬·agent run·런타임 capability 감지를 검증합니다. 서버가 없으면 자동으로 skip되므로 `pytest tests/`는 항상 green입니다.

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
