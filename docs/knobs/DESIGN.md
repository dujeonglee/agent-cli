# 세션 노브 — 한 번 선언, 표면 셋이 파생 (`agent_cli/knobs.py`)

> 상태: **1단계 구현됨 (v10.43.0, 2026-10-17)** — 선언 + env 표면 + CLI 표면(`run`/`web` 공통 데코레이터) + 부팅 적용 + 표에서 파생한 대칭 테스트. 2단계(web `/api/knobs` 통일)·3단계(칩 일반화·보드 spawn 인자·문서 생성)는 §5.
> 결정(사용자): ① `run` 의 제어면은 실행 인자(시작 뒤 입력 프롬프트가 없다 — §1) ② 노브 추상화 + 표면별 적응 인터페이스 ③ 1단계부터, 2·3단계는 별도 PR.

## 0. 한 줄 요약

세션 도중 바꿀 수 있고 다음 LLM 호출(또는 다음 확인·런)에 영향을 주는 값 아홉 축이 "노브" 다. `knobs.KNOBS` 가 유일한 선언이고, env 읽기(`Knob.env_value`)·CLI 플래그(`knob_options`)·web API(2단계)는 그 표를 돌 뿐이다. 우선순위는 **CLI 인자 > env > 기본값**, 전부 세션 한정이다.

## 1. 왜 — 노브의 정의가 어디에도 없었다

v10.42.0 까지 같은 노브가 세 표면에 손으로 따로 배선돼 있었다.

| 노브 | 상태 소유자 | web API | `run` 인자 | env |
|---|---|---|---|---|
| 🧠 사고 · 추론 노력 | ctx | `/api/thinking` | **없음** | `AGENT_CLI_THINKING`, `…_REASONING_EFFORT` |
| 📐 디코딩 문법 | ctx | `/api/grammar` | **없음** | `AGENT_CLI_GRAMMAR` |
| 👥 에이전트 상한 | registry | `/api/max-agents` | `--max-agents` | `AGENT_CLI_MAX_AGENTS` |
| 🗜 압축 비율 | ctx | `/api/compaction` | `--compaction-ratio` | `AGENT_CLI_COMPACTION_RATIO` |
| ⏱ 스트림 무진전 한도·시도 | ctx | `/api/stream-idle` | `--stall`, `--stall-attempts` | `AGENT_CLI_STREAM_*` |
| ⚡ 자동 승인 | renderer | `/api/confirm-mode` | **없음** | **없음** |
| 🔎 세션 감사 | auditor | `/api/auditor` | **없음** | `AGENT_CLI_AUDITOR` |

- `agent-cli run` 은 시작 뒤 사용자 입력 프롬프트가 없다. 입력 큐에는 argv 의 질의 하나, 그 뒤로는 하니스의 합성 깨우기(에이전트 메일·감사 결과·모니터 보고)와 예약 발화뿐이다. stdin 을 읽는 자리는 부팅 전 "최근 세션 이어갈까요?" 와 도구 확인 프롬프트뿐이다. 그래서 run 의 제어면은 실행 인자여야 하고, 질의 본문 안의 "/노브" 는 모델 입력에 명령을 섞는 셈이라 두지 않는다.
- env 초기값을 읽는 코드가 노브마다 다른 모듈에 있었고(사고·문법·압축·스트림은 `context/manager.py` 의 `default_*()`, 상한은 `agents_live.default_max_agents`, 감사는 `constants.auditor_enabled`), `ContextManager.__init__` 이 그걸 호출했다. 그래서 **web 에서 🧠·📐 를 바꿔도 서브에이전트·팀원의 ctx 는 env 값**이었다(ctx 마다 env 를 다시 읽었다).
- 비대칭의 원인은 노브의 **정의가 어디에도 없었던 것**이다. 표면마다 자기 식으로 노브를 알고 있어 새 노브를 더할 때 네 군데를 기억해야 했다(이번 🔎 칩도 📐 칩을 베껴 만들었다).

## 2. 구조 — 적응의 축은 표면이다

사용자 제안은 "노브 추상화 클래스 + env/run/web 적응 인터페이스". 채택하되 축을 분명히 한다.

- **`Knob` 은 선언**이다: `name`(API·sticky 이름), `flag`, `env`, `label`, `help`, `kind`, `default`, `owner`(`KnobSession` 의 필드 이름), `get(session)`/`set(session, value)`. 파싱·검증·값 영역 설명은 종류별 하위 클래스(`Choice`·`Bool`·`Number`·`Duration`)가 갖고, 노브 아홉 축은 그 인스턴스다. 전체 목록 `KNOBS` 가 유일한 레지스트리다.
- **표면 어댑터는 표면당 하나**다. env 표면 = `Knob.env_value`/`resolve`(표를 돈다), CLI 표면 = `knob_options`(표에서 typer 옵션을 합성하는 데코레이터 하나, `run`·`web` 양쪽에 붙는다), web 표면 = 2단계의 `/api/knobs` 하나. 노브 × 표면으로 어댑터를 두면 9×3 = 27개 클래스가 되고, 지금의 복사·붙여넣기가 옷만 갈아입는 셈이다.
- **두 방향으로 서로 모른다.** `Knob` 는 typer·FastAPI·sticky 를 모르고, 표면은 어떤 노브가 있는지 모른다. 새 노브는 선언 한 줄이고 어느 표면에도 코드가 늘지 않는다 — 이것이 "구조로 강제" 다. 표에서 파생하는 테스트(§4)는 그 위의 보험이다.
- **상태 소유자는 흩어져 있다**(ctx·registry·renderer·auditor). `KnobSession` 이 넷을 한 묶음으로 들고, 각 노브의 `set` 은 자기 소유자 하나만 만진다. 소유자가 None 이면(headless 부트·세션 없음) `apply_all` 이 그 노브를 건너뛴다(`Knob.available`).

### 2.1 아홉 축

| name | flag | env | kind | default | owner |
|---|---|---|---|---|---|
| `thinking` | `--thinking` | `AGENT_CLI_THINKING` | off / on / auto | `off` (v9.24.8: 끔을 명시) | ctx |
| `reasoning_effort` | `--reasoning-effort` | `AGENT_CLI_REASONING_EFFORT` | auto / low / medium / high / off | `auto` | ctx |
| `grammar` | `--grammar` | `AGENT_CLI_GRAMMAR` | auto / on / off | `auto` | ctx |
| `max_agents` | `--max-agents` | `AGENT_CLI_MAX_AGENTS` | int (0 = 무제한) | 10 | registry |
| `compaction_ratio` | `--compaction-ratio` | `AGENT_CLI_COMPACTION_RATIO` | float | 1.0 | ctx |
| `stall` | `--stall` | `AGENT_CLI_STREAM_IDLE_TIMEOUT_S` | 600 · 10m · 0 | 600 | ctx |
| `stall_attempts` | `--stall-attempts` | `AGENT_CLI_STREAM_MAX_ATTEMPTS` | int 1–10 | 4 | ctx |
| `auto_approve` | `--auto-approve/--no-auto-approve` | `AGENT_CLI_AUTO_APPROVE` | bool | off | renderer |
| `auditor` | `--auditor/--no-auditor` | `AGENT_CLI_AUDITOR` | bool | off | auditor |

범위 보정(clamp)은 종전처럼 소유자의 setter 가 한다(`set_compaction_ratio`·`set_max_agents` …). 잘못된 CLI 값은 즉시 실패(`typer.BadParameter`, 세션을 세우기 전), 잘못된 env 값은 기본값으로 떨어진다(종전 env 들의 동작 그대로).

### 2.2 부팅 순서

```
ctx 조립(_build_context — 노브 인자 없음)
registry 조립(max_agents 인자 없음 — 기본값)
renderer / auditor 핸들(build_auditor — enabled=False 로 시작)
_apply_knobs(knob_args, ctx, registry, renderer, auditor)   ← apply_all(resolve_all(cli))
wire_agent_mail(restore / auto_spawn)                        ← 상한 노브가 이 게이트다
```

web 은 📐 sticky 의 부트 초기값이 적용 전에 나가므로 적용 뒤 `broadcast_grammar` 를 다시 낸다. `ContextManager.__init__` 은 더 이상 env 를 읽지 않고 상수 기본값만 두며, `create_subagent_ctx` 가 부모의 `thinking_override`(복사)·`grammar_override` 를 물려준다 — §1 의 "web 노브가 팀원에 안 닿던" 비대칭이 같이 사라졌다.

### 2.3 CLI 표면의 기술 선택

typer 는 함수 시그니처에서 옵션을 만든다. `knob_options` 는 `inspect.Signature` 를 합성해 `wrapper.__signature__` 로 둔다(typer 0.15 가 `inspect.signature` 를 존중). `from __future__ import annotations` 모듈의 문자열 주석은 `typing.get_type_hints` 로 풀어 박는다(안 풀면 "Type not yet supported: str"). bool 노브는 `--x/--no-x` 스위치 쌍, 나머지는 값을 받는 옵션이고 값 파싱은 데코레이터가 `Knob.parse` 로 한다. 정적 매개변수 + 테스트로 물러서는 대안은 쓰지 않았다 — 두 명령의 플래그 집합이 같은 표에서 나오므로 어긋날 수 없다.

## 3. 바뀐 것 (1단계)

| 파일 | 변경 |
|---|---|
| `agent_cli/knobs.py` (신규) | `Kind` 넷 · `Knob` · `KnobSession` · `KNOBS` · `resolve_all`/`apply_all` · `knob_options` |
| `agent_cli/main.py` | `run`/`web` 에 `@knob_options`; `--stall`·`--stall-attempts`·`--compaction-ratio`·`--max-agents` 정적 옵션과 `_parse_stall` 제거; `_build_context` 는 노브를 안 받음; `_apply_knobs` 가 두 부트에서 같은 순서로 |
| `agent_cli/context/manager.py` | `default_compaction_ratio`·`default_grammar_override`·`default_thinking_override`·`default_stream_idle_timeout_s`·`default_stream_max_attempts` 제거; `__init__` 은 상수 기본값 |
| `agent_cli/subagent/runner.py` | 서브에이전트 ctx 가 부모의 사고·문법 값을 물려받음 |
| `agent_cli/subagent/agents_live.py` | `default_max_agents` 제거 |
| `agent_cli/constants.py` · `harness/auditor.py` · `runtime.py` | `auditor_enabled` 제거; `build_auditor` 는 `enabled=False` 로 시작(노브가 켬); `wire_agent_mail` 은 감사자를 만들지 않고 셋만 돌려줌 |
| `agent_cli/render/base.py` · `minimal.py` · `web.py` · `web/server.py` | `auto_approve` 가 기본 렌더러의 것(`set_auto_approve`); `MinimalRenderer.confirm` 도 자동 승인; web 은 sticky 전파를 덧붙임 |
| tests | `tests/test_knobs.py`(표 파생 대칭·종류·우선순위·소유자 적용·CLI 표면·서브에이전트 상속); env 기본값 테스트들은 여기로 옮기고 종전 자리에서 삭제 |

## 4. 대칭을 깨뜨릴 수 없게 하는 테스트 (`tests/test_knobs.py`)

- `run`/`web` 의 등록된 옵션이 표의 플래그(bool 은 `--no-` 짝 포함)를 전부 노출한다.
- 소스 어디에도 표 밖의 노브 env 읽기가 없다(`environ.get("AGENT_CLI_…")` 를 `knobs.py` 밖에서 찾으면 실패).
- README 의 env 표와 CLI 표가 표의 env·플래그를 전부 싣는다.
- 표의 기본값 = 소유자가 아무 노브 없이 시작한 값(기본값이 둘이 되지 않게).
- 각 노브의 `set` 이 자기 소유자를 실제로 바꾸고 `get` 으로 되돌아온다; 사고와 추론 노력은 서로를 지우지 않는다.
- 우선순위 CLI > env > 기본값; 잘못된 CLI 값은 `BadParameter`, 잘못된 env 는 기본값.
- 서브에이전트 ctx 는 부모의 사고·문법 값을 **복사**로 물려받는다.

## 5. 다음 단계

### 5.1 2단계 — web 표면 통일

- `GET /api/knobs` 가 `KNOBS` 를 돌며 `{name, kind, choices 또는 range, value, default, locked(사유 또는 null), label, info}` 를 주고, `POST /api/knobs/{name}` 이 `Knob.parse → Knob.set` 뒤 sticky `knob:<name>` 하나를 낸다. 잠김(사고 미지원 모델, 문법 지원 미확인)은 `Knob.locked(session)` 이 사유를 돌려준다(표에 접근자 하나 추가).
- 개별 엔드포인트 일곱(`/api/thinking`, `/api/grammar`, `/api/max-agents`, `/api/compaction`, `/api/stream-idle`, `/api/confirm-mode`, `/api/auditor`)과 렌더러의 `broadcast_*` 일곱을 **별칭 없이** 지운다. 보드는 이 엔드포인트를 쓰지 않는다(status.json·health 만). status.json 의 `grammar`·`auditing` 은 노브가 아니라 상태라 남는다.
- 칩은 그대로 두되 새 API 를 부른다. 테스트는 표의 모든 이름에 GET/POST 가 되는지를 표에서 돌린다.

### 5.2 3단계 — 칩 일반화·보드·문서

- 칩 IIFE 일곱 대신 `knobs` IIFE 하나가 `/api/knobs` 메타로 칩을 그린다(bool·선택지는 select, 범위는 number, 잠김은 배지, 설명은 `info`). 새 노브는 프런트 코드 없이 칩이 생긴다. 회귀 위험이 가장 크므로 브라우저 테스트로 잰다.
- `web` 이 같은 플래그를 받으므로 보드의 글 설정에 노브를 두고 spawn 인자로 넘긴다(보드 짝 릴리스).
- README 의 env·CLI·web 표를 `KNOBS` 에서 만들어 손으로 적은 표와 대조하는 테스트 — 문서가 코드에서 벗어나지 못한다.

## 6. 위험

- typer 합성 시그니처는 `__signature__`·`__annotations__` 두 속성에 의존한다 — typer 를 올릴 때 `tests/test_knobs.py::TestCliSurface` 가 먼저 깨진다.
- 자동 승인은 UI 의 승인면이지 모델 동작이 아니다. `run` 에서는 tty 확인 프롬프트를 건너뛴다는 뜻이고 README 가 그렇게 적는다. 표의 범위는 "세션 도중 바꿀 수 있고 다음 호출·확인·런에 영향을 주는 값" 으로 좁힌다 — 예약·DIRECTIVE·닉네임은 노브가 아니다.
- 서브에이전트가 부모의 노브를 물려받게 되면서 web 에서 사고를 켜면 팀원도 켜진다 — 의도한 대칭이지만 종전과 다르다.
