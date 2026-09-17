# 설정 스코프 정리 — 설정의 집은 하나

> 상태: **승인, 구현 중** (2026-09-17 사용자 결정)
> 순서: ① 스코프 정리(이 문서) → ② `agent-cli mcp` 마법사 → ③ 웹 🔌 칩 (②③은 docs/mcp-ui 시안)

## 1. 결정

| 대상 | 종전 | 이후 |
|---|---|---|
| `config.json` | 프로젝트 > 유저 > env | **env + 유저** |
| `models.json` | 프로젝트 > 유저 (자동 저장은 유저) | **유저** |
| `mcp.json` | 유저 ∪ 프로젝트 (이름별 프로젝트 승) | **프로젝트** |
| `skills/` | 프로젝트 > 유저 > 내장 | **프로젝트 > 내장** |
| `agents/` | 프로젝트 > 유저 > 내장 | **프로젝트 > 내장** |
| `hooks/`, `hooks.json` | 둘 다 실행 (프로젝트 먼저) | **프로젝트** |
| `DIRECTIVE.md` | 둘 다 연결 | **프로젝트** |
| `chat_history` | 유저 | **`sessions_dir()`** |

세 줄로 설명된다:

```
유저 (~/.agent-cli)     config.json · models.json        ← 머신 사실
프로젝트 (.agent-cli)   mcp · skills · agents · hooks · DIRECTIVE
sessions_dir()          sessions · chat_history          ← 작업 트리 밖으로 뺄 수 있는 것
```

## 2. 왜

종전 규칙 "둘 다 읽고 프로젝트 우선"은 **말하기는 쉬운데 결과가 헷갈렸다**.
어느 파일이 이겼는지 매번 따져야 했고, `Path.cwd()` 기준이라 하위 디렉토리에서
실행하면 조용히 달라졌다. 병합 입도도 제각각이었다 — config 는 필드별, mcp 는
이름별, hooks 는 누적, DIRECTIVE 는 연결.

이후엔 **각 설정의 집이 하나**라 "어느 게 이겼나"라는 질문 자체가 사라진다.
규칙 수는 늘지만 조회는 항상 명확하다.

이 저장소의 `.gitignore` 가 이미 방향을 보여준다: `.agent-cli/*` 를 무시하되
`!.agent-cli/skills/` 는 커밋한다 — 스킬을 프로젝트 자산으로 다루는 관행이 있었다.

## 3. 알고 받아들인 손실

설계 대화에서 짚었고 사용자가 수용한 것들. 나중에 "왜 이렇게 했지"가 되지
않도록 적어 둔다.

- **개인 자산 축이 없다.** skills·agents·DIRECTIVE 는 "내 작업 방식"이라
  프로젝트를 넘나드는 성격인데, 프로젝트 전용이 되면 저장소마다 복사한다.
  "항상 한국어로" 같은 취향도 새 저장소마다 다시 쓴다. → 단순함을 택함.
- **전역 훅 = 유일한 정책 수단**이 사라진다. 감사 로깅·명령 차단을 모든
  프로젝트에 강제할 방법이 없어지고, 프로젝트가 스스로 훅을 정의하므로
  정책을 빠져나갈 수 있다. → 정책 용도 안 씀.
- **프로젝트별 모델 고정**이 `cd` 만으로 안 된다. `--model`/env/direnv 필요.
- **프로젝트 밖 실행은 빈껍데기.** `~/Downloads` 에서 돌리면 스킬·MCP·
  DIRECTIVE 없이 뜬다. agent-cli 는 이제 **프로젝트 안에서 쓰는 도구**다.
- **마이그레이션 경고 없음.** 8개 경로가 조용히 무시되지만(아래 표) 단일
  사용자 판단으로 경고를 넣지 않는다. 대신 이 문서와 README 가 안내한다.

### 조용히 무시되는 기존 경로

```
~/.agent-cli/mcp.json · skills/ · agents/ · hooks/ · hooks.json · DIRECTIVE.md
~/.agent-cli/chat_history                     (→ sessions_dir()/chat_history)
.agent-cli/config.json · models.json
```

## 4. `chat_history` 를 `.agent-cli/` 가 아니라 `sessions_dir()` 에 두는 이유

프로젝트별 히스토리는 의도가 맞다(↑ 에 다른 프로젝트 명령이 안 나온다).
하지만 `.agent-cli/` 에 두면 **작업 트리 안으로 들어온다**. 채팅 히스토리는
사용자가 타이핑한 것이라 경로·토큰이 섞일 수 있고, 읽기 전용 체크아웃에선
아예 못 쓴다. `AGENT_CLI_SESSIONS_DIR` 가 정확히 그 용도로 이미 있다 —
*"작업 트리에 세션을 남기지 않을 곳(헤드리스/CI, 읽기 전용·공유 체크아웃,
벤치 컨테이너)"*. 그 장치를 따르면 컨테이너·CI 에서 자동으로 트리 밖으로
나간다.

## 5. 구현

### `paths.py`

`scoped_paths()` 는 은퇴한다 — 쌍을 돌려주는 함수인데 쌍을 원하는 곳이
없어진다. 대신:

```python
def project_dir() -> Path      # cwd/.agent-cli
def user_dir() -> Path         # ~/.agent-cli
def sessions_dir() -> Path     # 기존
```

소비 모듈은 `project_dir() / "mcp.json"` 처럼 조립한다. 모듈-레벨 상수
조립(import 시점 `Path.cwd()` 고정, 테스트 monkeypatch seam)은 유지.

### 소비처 (10)

| 파일 | 변경 |
|---|---|
| `config.py` | `_CONFIG_PATHS` → `[user_dir()/config.json]`; models 탐색 → 유저만 |
| `setup.py` | "프로젝트/유저" 저장 위치 질문 제거 → 유저 고정 (2곳) |
| `mcp/config.py` | `_MCP_CONFIG_PATHS` → `[project_dir()/mcp.json]`; reversed 제거 |
| `skills/loader.py` | `[project_dir()/skills, 내장]` |
| `subagent/profiles.py` | `[project_dir()/agents, 내장]` |
| `hooks/loader.py` | `[project_dir()/hooks]` |
| `hooks/shell.py` | `[project_dir()/hooks.json]` |
| `prompts/system_prompt.py` | `_DIRECTIVE_PATHS` → `[project_dir()]`; "전역은 편집 제외" 주석 정리 |
| `input_history.py` | `sessions_dir() / "chat_history"` |
| `paths.py` | 위 |

### 버전

**9.0.0.** 기존 설정 파일이 무시되는 파괴적 변경이라 버전이 그 사실을 말해야
한다. 8.x 마이너로 묻으면 `agent-cli update` 로 올린 뒤 원인을 못 찾는다.

### 테스트

- `test_paths.py` — `TestScopedPaths`·`TestSiteEquivalence`(종전 쌍 등가성)는
  계약이 바뀌므로 **교체**: 각 소비 모듈의 상수가 단일 스코프인지 고정.
- `test_config.py`·`test_setup.py` — 프로젝트 config 무시, setup 유저 고정.
- `test_hooks.py::TestLoadHooksMergesBothScopes` — "둘 다 발화" 계약이
  사라지므로 프로젝트 단일로 교체.
- `test_system_prompt.py` — 전역 DIRECTIVE 연결 테스트 제거.
- `test_mcp.py` — 유저 스코프 병합 테스트 제거, 프로젝트 단일 고정.
- 신규: `input_history` 가 `AGENT_CLI_SESSIONS_DIR` 를 따르는지.

### 문서

README(설정 섹션·env 표·MCP·프로젝트 구조), ARCHITECTURE(paths·10개 모듈 항목).
