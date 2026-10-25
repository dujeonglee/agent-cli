# 세션 노브 — 한 번 선언, 표면 셋이 파생 (`agent_cli/knobs.py`)

> 상태: **§7 🎲 샘플링 노브 구현됨 (v10.69.0, 2026-10-24)** · **1단계 + 1.5단계 구현됨 (v10.43.0, 2026-10-17), 2단계 구현됨 (v10.44.0), 3단계 칩 일반화 구현됨 (v10.45.0)** — 선언 + env 표면 + CLI 표면(`run`/`web` 공통 데코레이터) + 부팅 적용 + 표에서 파생한 대칭 테스트, 그리고 모델 노브와 **복사본 제거**(§2.4). 2단계(web `/api/knobs` 통일)·3단계(칩 일반화·보드 spawn 인자·문서 생성)는 §5.
> 결정(사용자): ① `run` 의 제어면은 실행 인자(시작 뒤 입력 프롬프트가 없다 — §1) ② 노브 추상화 + 표면별 적응 인터페이스 ③ 1단계부터, 2·3단계는 별도 PR ④ **모델도 노브**, 노브 값은 **캐시하지 말고 영향을 미치는 코드가 직접 읽어 적용**(모델을 바꾸면 다음 LLM 호출에서 적용) — 그러면서 같은 정보를 두 곳에 들던 복사본과 죽은 코드를 정리.

## 0. 한 줄 요약

세션 도중 바꿀 수 있고 다음 LLM 호출(또는 다음 확인·런)에 영향을 주는 값 아홉 축이 "노브" 다. `knobs.KNOBS` 가 유일한 선언이고, env 읽기(`Knob.env_value`)·CLI 플래그(`knob_options`)·web API(2단계)는 그 표를 돌 뿐이다. 우선순위는 **CLI 인자 > env > 기본값**, 전부 세션 한정이다.

## 1. 왜 — 노브의 정의가 어디에도 없었다

v10.42.0 까지 같은 노브가 세 표면에 손으로 따로 배선돼 있었다.

| 노브 | 상태 소유자 | web API | `run` 인자 | env |
|---|---|---|---|---|
| 🧠 사고 · 추론 노력(당시) | ctx | `/api/thinking` | **없음** | `AGENT_CLI_THINKING`, `…_REASONING_EFFORT` |
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

### 2.1 열 축 (v10.69.0: 🎲 둘 추가 — §7 · v10.72.0: `reasoning_effort` 제거 — §8)

| name | flag | env | kind | default | owner |
|---|---|---|---|---|---|
| `thinking` | `--thinking` | `AGENT_CLI_THINKING` | off / on | `off` (v9.24.8: 끔을 명시) | ctx |
| `grammar` | `--grammar` | `AGENT_CLI_GRAMMAR` | auto / on / off | `auto` | ctx |
| `temperature` | `--temperature` | `AGENT_CLI_TEMPERATURE` | float 0–2 / auto | `auto`(None — 보내지 않음) | ctx |
| `top_p` | `--top-p` | `AGENT_CLI_TOP_P` | float 0–1 / auto | `auto`(None — 보내지 않음) | ctx |
| `max_agents` | `--max-agents` | `AGENT_CLI_MAX_AGENTS` | int (0 = 무제한) | 10 | registry |
| `compaction_ratio` | `--compaction-ratio` | `AGENT_CLI_COMPACTION_RATIO` | float | 1.0 | ctx |
| `stall` | `--stall` | `AGENT_CLI_STREAM_IDLE_TIMEOUT_S` | 600 · 10m · 0 | 600 | ctx |
| `stall_attempts` | `--stall-attempts` | `AGENT_CLI_STREAM_MAX_ATTEMPTS` | int 1–10 | 4 | ctx |
| `auto_approve` | `--auto-approve/--no-auto-approve` | `AGENT_CLI_AUTO_APPROVE` | bool | off | renderer |
| `auditor` | `--auditor/--no-auditor` | `AGENT_CLI_AUDITOR` | bool | off | auditor |
| `model` | `--model` / `-m` | `AGENT_CLI_MODEL` | 이름 | None(config `default_model` → 프로바이더 기본) | binding |

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

### 2.4 1.5단계 — 모델 노브와 "읽는 자리에서 읽기" (결정 ④)

아홉 노브는 처음부터 소유자에만 값이 있었다(§2 — `apply_all` 은 쓰고 끝, 쓰는 쪽은 매번 소유자를 읽는다). 모델만 달랐다. 부팅 때 `_bootstrap_provider` 가 모델 → 능력(창 크기·사고·문법 지원) → 방언을 풀어 **값으로 복사**했다: `SessionBootstrap` → run/web 지역변수 여섯 개 → `LoopConfig.model/capabilities/dialect`(런마다) → `AgentRuntime`(팀원·하니스 런) → `WebServer.runtime` dict, 그리고 ctx 의 토큰 예산(창의 70%). 다섯 곳이 같은 정보를 들었고 세션 도중 바꿀 길이 없었다.

- **`ModelBinding`** (`providers/binding.py`) 하나를 세션이 든다: provider(엔드포인트, 고정)·model·capabilities(바꿀 수 있음)·dialect(고정)·예산 오버라이드. `LoopConfig` 는 `binding` 필드 하나와 **매번 바인딩을 읽는 속성**(`model`·`capabilities`·`dialect`·`provider`·`provider_name`)으로 바뀌었고, `llm.py` 는 호출마다 `cfg.model`·`cfg.capabilities` 를 통해 바인딩을 읽는다. `ContextManager.max_context_tokens` 는 속성이 되어 바인딩의 창에서 매번 계산한다(명시 `--max-context-tokens` 만 고정). 팀원·하니스·delegate·스킬 런은 `AgentRuntime.binding` / `execute_skill(binding=)` / `tool_delegate(binding=)` 로 같은 객체를 참조한다. 그래서 `binding.switch("<model>")` 뒤의 **다음 LLM 호출**부터 새 모델·능력·예산이 실린다 — 캐시도 무효화도 없다.
- **`switch` 의 조건**: models.json 에 등록된 모델(능력을 프로브 없이 안다 — 미등록 모델 프로브는 수십 초), 그 모델의 방언이 세션 방언과 같을 것(history 렌더가 방언별; `--dialect` 강제면 어떤 모델이든). 거부는 사유를 돌려주고 아무것도 바꾸지 않는다. 같은 이름은 no-op.
- **`pinned(model)`**: 프로파일·스킬이 모델을 고정한 런은 그 모델의 능력·방언을 가진 **복사본**으로 돈다(종전 `create_subagent_ctx` 의 `resolve_dialect(model)` 규칙 그대로). 세션 바인딩의 `switch` 는 그 복사본에 닿지 않는다 — 고정은 고정이다.
- **노브 `model`**: `--model/-m`(기존 정적 옵션을 표가 흡수 — `Knob.short`), env `AGENT_CLI_MODEL`(config.py 의 env 맵에서 제거 — 읽는 곳은 `knobs.py` 뿐), 기본값 None(= config `default_model` → 프로바이더 기본, `main._boot_model`). 부팅에서는 `_bootstrap_provider` 가 그 값으로 바인딩을 만들고, `apply_all` 은 같은 이름이라 no-op. web 표면은 2단계 `/api/knobs` 에서.
- **정적 부품의 조립 자리는 하나**: `AgentLoop.__init__` 만 `provider/capabilities/model/…/dialect` 부품을 받아 바인딩을 만든다(바인딩 없이 부르는 테스트·일회성용; 방언 폴백 명시 > ctx > 기본). `run_loop` 는 통과만 하고, `execute_skill`·`tool_delegate`·`_run_single`·`run_subagent_message`·`_handle_run_skill`·`try_dispatch_agent_or_skill`·`AgentRuntime`·`WebServer` 는 **바인딩만** 받는다.

**지운 것(복사본·죽은 코드)**: `SessionBootstrap` 데이터클래스; run/web 의 `llm_provider/capabilities/resolved_model/resolved_url/resolved_key/provider/dialect_plugin/max_context_tokens` 지역변수와 그것을 네 함수(`try_dispatch_agent_or_skill`·`_dispatch_agent`·`_dispatch_skill`·`_run_main`)로 나르던 여섯 인자; `LoopConfig` 의 여섯 값 필드와 `AgentLoop`/`LoopConfig` 의 `base_url`·`api_key` 속성(아무도 안 읽었다); `AgentRuntime` 의 여섯 값 필드와 `from_loop_config(cfg, provider)` 의 provider 인자; `WebServer.runtime` dict(`/api/thinking`·`/api/grammar`·`/api/debug/prompt`·✨ 생성이 각자 꺼내 쓰던 `capabilities`/`provider`); `generate_directive_section(runtime=)`; `oneshot`/`runner`/`executor`/`skill_invoke` 의 여섯 매개변수; `skill_invoke` 가 스킬마다 `create_provider` 로 새 프로바이더 객체를 만들던 것; `executor` 가 스킬 디렉토리 이름을 손으로 짓던 중복(`generate_subdir_name(SKILL_PREFIX)` 로); `create_subagent_ctx` 의 fork/resume/none 세 벌 `ContextManager(...)` 생성(하나로); `harness/jobs.RUNTIME_KEYS` 의 여섯 키; `config._ENV_MAP` 의 `AGENT_CLI_MODEL`.

**테스트**: `tests/binding_support.make_binding` 이 테스트의 정적 바인딩 도우미; `tests/test_knobs.py::TestModelKnob` — 바꾸면 `LoopConfig.model`·`capabilities`·ctx 예산이 **같은 호출 없이** 따라간다(복사본 없음), 미등록·다른 방언 거부, `pinned` 복사본은 `switch` 를 따라가지 않는다, `-m` 별칭; `test_dialect_binding` 의 서브에이전트 바인딩 테스트는 `pinned` 규칙으로 이전.

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
- 각 노브의 `set` 이 자기 소유자를 실제로 바꾸고 `get` 으로 되돌아온다.
- 우선순위 CLI > env > 기본값; 잘못된 CLI 값은 `BadParameter`, 잘못된 env 는 기본값.
- 서브에이전트 ctx 는 부모의 사고·문법 값을 **복사**로 물려받는다.

## 5. 다음 단계

### 5.1 2단계 — web 표면 통일 (구현됨, v10.44.0)

- `GET /api/knobs` 가 `KNOBS` 를 돌며 노브마다 상태 한 장(`knobs.web_state`): `{name, label, help, flag, env, kind, choices, default, available, value, locked, …info}`. `info` 는 노브가 선언한 파생 사실(범위 min/max/step, `supported`/`active`, `supports_thinking`, `in_flight`, 모델의 `dialect`/`context_window`), `locked` 는 잠김 사유(사고 미지원 모델, 문법 지원 미확인/미지원). 소유자가 없으면 `available: false` 에 값 없음.
- `POST /api/knobs/{name}` `{value}` → `Knob.parse` → `Knob.set` → sticky `knob:<name>`(이벤트 `knob`) 하나. 미지名 404, 잘못된 값·거부(모델 미등록 등)·소유자 없음은 `{ok:false, error}` 로 아무것도 바꾸지 않는다. 다음 LLM 호출(또는 다음 확인·런)부터 실린다.
- **지운 것(별칭 없이)**: 개별 엔드포인트 열네 핸들러(`/api/thinking`·`/api/grammar`·`/api/max-agents`·`/api/compaction`·`/api/stream-idle`·`/api/confirm-mode`·`/api/auditor` 의 GET/POST), 렌더러의 `broadcast_compaction_ratio`·`broadcast_stream_idle`·`broadcast_grammar`·`broadcast_auditor`·`broadcast_thinking`·`broadcast_max_agents` 와 `set_auto_approve` 오버라이드(sticky), 프런트의 노브별 fetch 열넷·SSE 리스너 일곱·`agentcli:*` 이벤트 일곱. status.json 의 `grammar`·`auditing` 은 노브가 아니라 상태라 남고, `knob:grammar`·`knob:auditor` sticky 에서 읽는다. 보드는 이 엔드포인트를 쓰지 않았다.
- 칩은 그대로(디자인 결정)되 `KNOBS` 클라이언트 하나(`all/get/set/on`)를 쓴다 — 한 칩이 두 노브를 들 때(⏳ 한도·시도, 🎲 temperature·top_p)는 각 노브를 따로 저장하고 다른 축의 마지막 값을 들고 있다. 📐 셀렉트는 노브 값 그대로 `auto`(기본, 지원되면 켬)/`off`.
- 테스트 `tests/test_knobs_web.py`: 표의 모든 노브가 GET 에 표 순서로 나오고 POST 로 바뀌며 sticky 와 재접속 snapshot 에 실린다(표에서 돈다); 404·잘못된 값·소유자 없음은 무변경; 소유자 의미(clamp·무제한 0·무진전 0=끔·두 축 독립) 유지; 잠김 사유는 서버가; status.json `grammar` 는 같은 sticky; 프런트는 `fetch("api/<old>")` 없이 `KNOBS` 만.

### 5.2 3단계 — 칩 일반화·보드·문서 (칩·문서 구현됨 v10.45.0, 보드는 짝 릴리스)

- **칩의 모양도 표에 있다.** `Knob.ui: KnobUI(icon, group, placement chip|header, control ""|range|unlimited|options, scale, unit, choice_labels)` 와 서버가 계산하는 글자 셋 `badge(session)`·`note(session)`·`on(session)`. 묶음은 `KNOB_GROUPS[name] = KnobGroup(icon, label, help, badge, note)` — ⏳(stall·stall_attempts: 배지 `10m×4`/`off`, 설명 "10분 × 4회 = 최대 40분 후 실패"), 🧠(thinking·reasoning_effort: 배지 `off`/노력/`미지원`). `web_state` 한 장에 `ui`·`badge`·`note`·`on`·`group{name,icon,label,help,members,badge,note}` 가 실리고, 🤖 는 `options`(models.json 에서 이 세션의 방언에 묶인 이름들; `--dialect` 강제면 전부). 배지·설명·잠김 사유는 **서버의 말**이다 — 프런트에 노브 이름도 배지 글자도 없다(테스트가 부재를 고정).
- **프런트는 렌더러 하나.** app.js 의 `KNOBS` 클라이언트(`all/set`)와 IIFE 하나가 GET api/knobs 의 상태로 `#knobs`(칩)·`#knobs-header`(⚡ 토글)에 칩을 만든다: 종류→컨트롤(choice·bool·options → select, range → 슬라이더, unlimited → number+체크박스, number/duration → number, text → text), `scale` 로 화면 단위(분·%) 변환, 묶음은 한 팝업의 3열 그리드, 잠김은 비활성+툴팁 사유(같은 사유는 한 번), `on` 은 `.is-on`, `available=false` 는 숨김. id 규칙 `knob-<칩>-{wrap,chip,pop,badge,note}` / `knob-<노브>`. 칩별 IIFE 일곱·마크업·id 별 CSS 삭제, CSS 는 `.knob-pop` 공통 + `.knob-pop.is-group` + `.knob-toggle` + `.knob-note`.
- **한 번의 set 이 열 장을 다시 싣는다.** 모델은 사고·문법의 잠김을, 묶음 멤버는 묶음 배지를 바꾸므로 `POST /api/knobs/{name}` 뒤 `broadcast_knobs(web_states(session))`. 조작 중(focus)인 입력은 덮지 않는다 — 옆 칸에 막 적은 값이 change 전에 되돌아가던 경합(브라우저 테스트가 잡음). 하니스 진행 수는 `renderer.knob_state(name)`(web 부트가 `server.knob_session` 으로 묶음)로 🔎 상태 한 장을 다시 계산한다 — 종전 payload 에 `in_flight` 만 덧씌우던 길 삭제.
- **지운 중복.** `SessionAuditor.state()`(미사용), `WebServer.grammar_state()`(📐 `_grammar_info` 와 같은 계산 — `/api/health.grammar` 가 노브 상태 한 장을 읽는다), `note_harness` 의 payload 덧씌움, 프런트의 `KNOBS.get/on`·칩별 IIFE·마크업·id 별 CSS.
- **`agent-cli knobs [--json]`** — `table_spec()`(이름·플래그·env·종류·선택지·기본값·ui, 값 없음)을 보드 같은 바깥에 준다. 보드(v1.36.0 예정)는 이걸로 글 설정의 노브 폼을 만들고 `Post.knobs` → spawn 플래그 / 라이브 `/api/knobs` 로 넘긴다.
- **문서가 표에서 벗어나지 못한다.** `tests/test_knobs.py::TestReadmeFollowsTheTable` — README 의 CLI 표 한 줄에 그 노브의 env 와 칩 아이콘이 함께 있고 `agent-cli knobs` 가 적혀 있다; env 표는 종전 테스트.
- **테스트.** 단위: `TestChipShapeIsDeclaredInTheTable`(아이콘/묶음 선언, 칩 이름 충돌 없음, 배지·설명·`on`·배율, 묶음 배지=두 축의 곱, 미지원 모델의 잠김, 문법 배지 셋, 모델 options 의 방언 묶음), `TestKnobsCommand`, `tests/test_knobs_web.py::TestChipShapeComesFromTheServer`(멤버 하나 set → 다른 멤버 sticky 의 묶음 배지, 모델 전환 → 사고·노력 잠김 재송출), 정적 배선(`test_header_knob_chips_wired`: 옛 id 부재·`#knobs`·공통 CSS). 실브라우저 `tests/browser/test_knob_chips.py`: 표의 열 노브가 칩 일곱+토글 하나, 배지는 서버의 말, 입력 한 번이 소유자(ctx·registry·renderer·auditor·binding)까지 닿는 왕복(셀렉트·슬라이더 %→비율·무제한 0·bool·헤더 토글·모델 전환), 잠김(사고 미지원 → 두 셀렉트 비활성·"미지원"·사유 한 번), 묶음 배지·설명·한도 0 → 시도 잠김, 팝업 하나씩, 두 뷰어 동기화, 감사 진행 → "감사 중", 거부 되돌리기. `tests/browser/conftest.py` 의 `WebStack` 이 소유자 넷을 전부 꽂는다.

### 5.3 보드는 거울이다 (agent-cli v10.46.0 + 보드 v1.36.0)

- 방이 떠 있는 동안 노브의 진실은 세션 하나(소유자 넷)이고, 뷰어 간 동기화는 sticky `knob:<name>` 한 길이다(마지막 쓴 사람이 이긴다). 보드는 떠 있는 방에 **쓰지 않는다** — 두 군데서 쓰는 상태를 만들지 않기 위해.
- agent-cli 는 노브가 바뀔 때마다 `status.json` 에 `knobs: {이름: 값}` 을 쓴다(`render/web.py::_publish_status`, 소유자 없는 노브 제외). 보드는 지금 `grammar`·`auditing` 을 읽는 길로 이 값을 읽어 카드에 보여 주고 저장값과 다르면 기록한다. HTTP 폴링 없음. 10.45.0 이하 인스턴스는 키가 없어 보드가 기록하지 않는다.
- 다음 spawn 은 저장된 `knobs` 를 `agent-cli knobs --json` 표의 `flag` 로 인자로 바꿔 넘긴다(bool 은 `--x`/`--no-x`). 그래서 방에서 🤖 로 바꾼 모델이 재시작 뒤에도 남는다 — 종전엔 보드의 옛 `model_id` 로 되돌아갔다.
- 보드의 노브 입력은 **새 글 폼(⚙ 고급 접기) 한 번**뿐이고 카드의 ⚙ 패널은 항상 읽기 전용이다. 모델 변경 API(`POST /api/posts/{id}/model`)와 §16 게이트는 지운다. 뜰 수 없는 방(모델 삭제·방언 미설정)은 📋 복제로 다른 모델을 골라 살린다 — 읽기 전용 규칙에 예외를 두지 않는다.
- 테스트 `tests/test_knobs_web.py::TestStatusFileMirrorsTheKnobs`(부트 송출로 열 노브 전부, set 마다 다시 씀, 소유자 없는 노브 제외), `tests/test_web_instance_file.py::test_knobs_are_additive`.

## 6. 위험

- typer 합성 시그니처는 `__signature__`·`__annotations__` 두 속성에 의존한다 — typer 를 올릴 때 `tests/test_knobs.py::TestCliSurface` 가 먼저 깨진다.
- 자동 승인은 UI 의 승인면이지 모델 동작이 아니다. `run` 에서는 tty 확인 프롬프트를 건너뛴다는 뜻이고 README 가 그렇게 적는다. 표의 범위는 "세션 도중 바꿀 수 있고 다음 호출·확인·런에 영향을 주는 값" 으로 좁힌다 — 예약·DIRECTIVE·닉네임은 노브가 아니다.
- 서브에이전트가 부모의 노브를 물려받게 되면서 web 에서 사고를 켜면 팀원도 켜진다 — 의도한 대칭이지만 종전과 다르다.

## 7. 🎲 샘플링 노브 — temperature · top_p (v10.69.0)

> 동기: GLM-5.3-Flash 를 같은 조건으로 두 번 돌린 Harbor 벤치(2026-10-24)에서 9과제 중 4개가 뒤집혔다. 하니스는 `temperature` 를 보내지 않아 omlx 모델 설정(T 1.0 · top_p 0.95)으로 샘플링됐고, 서버 설정을 0.2 로 바꾼 1차 재측정은 8/9 였다. 샘플링은 **측정 조건**이므로 하니스 쪽에서 명시하고 기록할 자리가 필요했다.
> 결정(사용자): ① 노브는 `temperature`·`top_p` 둘(`top_k` 는 OpenAI 규격 서버가 거부하고 로컬 서버는 모델 설정으로 가진다 — 선언 한 줄이면 더할 수 있다) ② 모델별 기본값 층은 `models.json` 엔트리의 선택 키 ③ 📐 문법과 **따로** 둔다 — 같은 디코딩 단계지만 서로 독립이고(묶음의 기준은 "의존" 이다: 🧠 노력은 사고 없이, ⏳ 시도는 한도 0 이면 뜻이 없다), 답하는 질문(모양 강제 예/아니오 vs 무작위성 숫자)과 잠김(지원 기록)이 다르며, 📐 는 생성 줄·턴 통계·인스펙터에 자기 정체가 있다.

### 7.1 세 층 — 세션 노브 > models.json 엔트리 > 서버 기본

```
CLI --temperature / env AGENT_CLI_TEMPERATURE / web 🎲 / 보드 폼→spawn 플래그
        └─▶ ① ctx.sampling {temperature?, top_p?}      세션 한정 · 서브에이전트 복사
                 auto ─▶ ② ModelCapabilities.temperature/top_p   models.json 엔트리의 선택 키
                              없음 ─▶ ③ 필드를 보내지 않음               서버 기본(omlx 모델 설정 → 전역)
요청 본문: resolve_sampling(caps, ctx.sampling) — 정해진 키만 (OpenAI·Anthropic 같은 이름, 번역 없음)
```

- **기본은 auto = 보내지 않음.** 노브를 건드리지 않고 엔트리에 키가 없으면 요청 본문이 종전과 같다 — 지금까지의 벤치 조건이 그대로 재현된다.
- **값이 없음이 뜻을 가지는 첫 숫자 노브**라 `Number(optional=True)` 가 생겼다: `auto`·빈 값·None → None, 소유자(`ctx.set_sampling`)는 None 을 키 삭제로 받고 값은 `constants.SAMPLING_RANGES` 로 clamp. 표의 기본값도 None 이라 `apply_all` 은 손대지 않는다(모델 노브와 같은 규칙).
- **엔트리 키는 사용자·보드만 적는다.** `_build_from_entry` 가 읽고(`_optional_float` — 오타·불리언은 미기록으로, 부트를 막지 않는다) `caps_to_entry` 는 값이 있을 때만 쓴다. 프로브(`_detect_capabilities`)는 채우지 않으므로 자동 감지 엔트리에 키가 생기지 않는다 — 기본값은 사용자 결정으로만 들어간다. 보드 어드민 모델 편집기(v1.39.0)가 이 두 키의 UI 다.
- **요약 호출에도 간다.** `_summarize` 가 `CallSettings(sampling=ctx.sampling)` 을 넘긴다 — "다음 LLM 요청부터" 라는 칩의 약속에 요약이 예외가 아니게.
- **Anthropic 의 범위**(temperature 0~1, 확장 사고 중 temperature 고정, 일부 모델은 temperature 와 top_p 동시 지정 거부)는 서버가 거절로 말한다. 하니스는 두 프로바이더의 합집합(0–2)만 막는다 — 어느 서버를 대하는지는 세션이 안다.

### 7.2 칩 — 상태 한 장이 출처를 말한다

- 묶음 `sampling`(🎲 Sampling) 에 두 노브. 묶음 배지는 **세션이 정한 값만** `T 0.2 · p 0.95` / `auto` — 엔트리 값만으로 가는 경우는 `auto` 로 두고 설명 줄이 말한다: `Now sent: temperature 0.2 from models.json · top_p: server default.` 세션 값은 `(this session)`.
- 상태 한 장에 `min/max/step` 과 **`effective`(지금 실제로 가는 값)·`source`(session / models.json / server)·`pick`** 이 실린다. `pick` 은 auto 체크를 풀 때 칩이 보내는 값 — 실효값이 있으면 그것, 없으면 1.0(둘 다 1.0 = 효과 없음)이라 체크를 푸는 순간 동작이 바뀌지 않는다.
- 컨트롤 변형 `optional`(app.js): 숫자 입력 + `auto` 체크박스. auto 면 입력은 비활성이고 실효값(없으면 placeholder `server default`)을 보여 준다. 체크를 켜면 `"auto"` 를 보내 키를 지운다. 프런트에 노브 이름·배지 글자는 없다(기존 규칙).

### 7.3 보드

- 세션 노브 쪽은 **보드 변경 없음**: `agent-cli knobs --json` 표에 두 행이 늘어 새 글 ⚙ Advanced 폼에 숫자 칸 둘이 생기고(빈 칸 = 기본), `status.json knobs` 에 값이 실려 카드 칩 `🎲 0.2`·읽기 전용 패널에 뜨며, 재시작 spawn 인자에 `--temperature 0.2` 가 자동으로 실린다(`spawn_args` 가 표의 `flag` 로 바꾼다; null 값은 건너뛴다).
- 모델 기본값(②층)은 어드민 모델 편집기(보드 v1.39.0): 표 열 `sampling`(`T 0.2 · p 0.95` / `—`), 편집 창 숫자 칸 둘(빈 칸 = 키 삭제 → 서버 기본), 범위 검사.

### 7.4 벤치에서

Harbor 어댑터는 `--ae AGENT_CLI_TEMPERATURE=0.2`(①층) 또는 바인딩 복사본 `models.json` 의 키(②층)로 조건을 명시한다. 둘 다 omlx 모델 설정을 이기므로 서버 쪽은 기본값으로 되돌려도 된다 — 조건이 하니스 기록(`status.json knobs`·`--ak models_json`)에 남는다.

### 7.5 테스트

`tests/test_knobs.py::TestSamplingKnobs`(optional 파싱, auto=키 삭제, clamp, 묶음 배지·설명·`effective`/`source`/`pick`, 서브에이전트 복사, CLI `--temperature 0.2 --top-p auto`), `tests/test_knobs_web.py::TestSamplingIsOptional`(POST `auto` 가 지운다, 상태의 범위·출처, 잘못된 값 무변경) + 표 파생 파라미터 두 행, `tests/test_providers.py::TestSamplingInTheBody`(`resolve_sampling` 우선순위, OpenAI·Anthropic 본문에 정해진 키만), `tests/test_capabilities.py::TestSamplingDefaultsInTheEntry`(엔트리 왕복, 오타 → 미기록, 프로브는 채우지 않음). README 의 env·CLI 표 두 행은 기존 표 파생 테스트가 강제한다.

## 8. 사고는 스위치 하나 — `reasoning_effort` 제거 (v10.72.0)

> 결정(사용자, 2026-10-25): 🧠 는 `on`/`off` 둘만, 기본 `off`. `auto`(서버 기본에 맡김)와 정도 노브 `reasoning_effort` 는 **삭제**하고 정도는 서버·모델 기본값에 맡긴다. "어설픈 auto" 는 만들지 않는다.

### 8.1 왜

- 정도의 wire 는 모델마다 다르다 — OpenAI `reasoning_effort`(none·low·medium·high·xhigh), Anthropic `output_config.effort`(low~xhigh·max; `budget_tokens` 는 현행 모델이 400), Gemini 3 `thinking_level`(low·high), GLM 템플릿 `reasoning_effort`(low·high 만, 그 외는 **Max**), Qwen3 템플릿은 `reasoning_effort` 를 읽지 않고 `enable_thinking`+`thinking_budget`(토큰 수).
- 하니스가 한 어휘로 번역하면 조용히 틀린다. 종전 정책은 사고가 켜지면 `reasoning_effort: medium` 을 **항상** 실었고, GLM 템플릿은 그것을 `Max` 로 읽었다(`/v1/tokenize` 렌더링 실측). 엄격한 API 는 400 으로 말해 주지만 템플릿 서버는 200 으로 접거나 무시하므로 응답으로는 알 수 없다.
- 모델별 어휘 표(`thinking_wire`)로 접는 안도 검토했으나, 서버가 별칭으로 서빙하면 이름 추천이 무력하고, 렌더링 프로브(`/v1/tokenize`)는 omlx·vLLM 에서만 된다. 그 복잡도 대신 "하니스는 켬/끔만 말한다" 를 택했다.

### 8.2 무엇이 바뀌었나

| 자리 | 전 | 후 |
|---|---|---|
| 노브 | `thinking` off/on/auto + `reasoning_effort` auto/low/medium/high/off (묶음 🧠) | `thinking` off/on **하나**(묶음 아님, 아이콘 🧠, `choice_labels`) |
| ctx | `thinking_override: dict` · `set_thinking_override(enable_thinking, reasoning_effort)` | `thinking: bool`(기본 False) · `set_thinking(bool)` |
| 정책 | `providers.base.ThinkingPolicy` · `resolve_thinking_policy` | `thinking_switch(caps, flag) -> bool | None`(미지원 모델 None = 아무것도 싣지 않음) |
| OpenAI | 켬이면 `reasoning_effort`(기본 medium) + 오버라이드 있을 때만 `chat_template_kwargs.enable_thinking` | 지원 모델이면 `chat_template_kwargs.enable_thinking` 을 **항상** 명시, `reasoning_effort` 없음 |
| Anthropic | `thinking: {enabled, budget_tokens: 4096/16384/32768}` + `max_tokens += budget` | 켬이면 `thinking: {type: adaptive}`(`_THINKING_ON`), 끔이면 블록 없음, `max_tokens` 불변 |
| 탐지 재프로브(Anthropic) | `budget_tokens: 1024` | 프로바이더와 같은 `_THINKING_ON` |
| `CallSettings.thinking` | `dict | None` | `bool = False` |

- 문법 제약(📐)의 `thinking_open` 은 같은 `thinking_switch` 에서 읽는다 — 스위치가 늘 명시되므로 "제약 중에만 스위치를 명시" 하던 특례가 사라졌다.
- 보드는 표를 읽어 그리므로 코드 변경 없음(`agent-cli knobs --json` 의 행이 하나 줄고 선택지가 둘). 저장된 글의 `thinking: auto` 는 다음 spawn 에서 agent-cli 가 거절해 방이 뜨지 않는다 — 글의 노브를 `on`/`off` 로 고쳐 저장하면 된다(보드 CHANGELOG 1.41.0).
- 벤치 조건: `--ae AGENT_CLI_THINKING=off` 그대로. GLM 의 `reasoning_effort=low` 는 omlx 모델 설정에 남는다(하니스는 정도를 말하지 않으므로 벤치 메모에 서버 설정을 함께 적는다).

### 8.3 테스트

`tests/test_knobs.py::TestApplyReachesTheOwners::test_thinking_is_a_plain_switch`(선택지 둘·`auto` 거절·`reasoning_effort` 부재), `TestChipShapeIsDeclaredInTheTable`(배지 off/on/unsupported, 묶음 아님, `KNOB_GROUPS` 에 thinking 없음), `tests/test_providers.py::TestOpenAIProvider::test_thinking_switch_is_always_explicit_and_effort_never_sent` · `TestThinkingSwitch`(Anthropic adaptive·예산 없음·끔은 블록 없음) · `TestThinkingSwitchGate`(정책 객체·budget 표가 되살아나면 실패), `tests/test_decoding_grammar.py::TestProviderBody`(문법 변형과 스위치가 같이 간다), `tests/test_capabilities.py`(재프로브 모양), `tests/browser/test_knob_chips.py`(🧠 셀렉트 하나·옵션 둘).
