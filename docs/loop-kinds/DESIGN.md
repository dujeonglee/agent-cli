# Loop Kinds — 루프 다섯 종류를 객체로 (v10.87.0 ~)

검토 문서(HTML, 2판): https://claude.ai/artifact/Bv1bQUZKekuPK2qqKnvCgK — 조사(file:line 전부)·
가변부 열여덟 함수·루프별 구현 표·결정 D1~D5 가 거기 있다. 이 파일은 저장소에 남는 요지와
구현 기록이다.

## 1. 문제

엔진(`AgentLoop` 와 협력자들)은 하나인데 들어가는 길과 나오는 길이 다섯이었다 — main ·
상주(`agent:<key>`) · one-shot(`run:<dir>`) · 스킬 · 하니스 작업(`harness:<job>`). 각자가
주소·포트·ctx·스코프·결과 포장·정리를 자기 모듈에서 손으로 조립했고(중복 열 곳: 런 본문,
바인딩 고정 다섯 자리, ctx 생성 두 벌, 스코프 래퍼 여섯 가지, 상주 런 래퍼 두 벌, 메일→레코드
세 구현, 소유자 정리 두 구현, 루프 밖 명령 레코드, 깊이·순환 가드, 서명 30여 인자 세 번),
엔진은 종류를 플래그 여덟 개(`wakeable`·`is_private_owner`·`dequeue_user_message is not None`·
`agent_registry is None`·`message_handler`·`skill_name`·`depth == 0`·`questions.nonblocking`)로
추측했다. 그 사이에 어긋남 네 건이 있었다:

- A. main 아래 스킬이 main 의 레지스트리 메일을 흡수한다(`_deliver_agent_mail` 은 `agent_registry` 만 본다).
- B. `execute_skill` 의 `parent_role`·`parent_tools` 를 어느 호출자도 넘기지 않아 죽어 있었다.
- C. 하니스 주소 `harness:<job>` 에 라벨이 없어 겹치는 감사 둘이 한 주소였다.
- D. 테스트·주석 잔재(없는 함수 이름을 보는 AST 검사, 낡은 owner 주석).

## 2. 설계 — 종류가 결정하고 엔진이 돈다

`agent_cli/loop/kind.py` 의 `LoopKind` 가 가변부에 이름을 붙인다.

| 묶음 | 함수 | 뜻 |
|---|---|---|
| A 정체 (생성자) | ① `owner` ② `depth` ③ `agent_stack`·`skill_stack` ④ `binding` ⑤ `tools` ⑥ `hooks_config` ⑦ `ports` ⑧ `stop_event` | 런 동안 불변 |
| B 프롬프트 (매 턴) | ⑨ `prompt_profile` ⑩ `tail_roster` ⑪ `renders` | **PR 2** — 엔진이 종류에게 묻는다 |
| C 런 (템플릿 순서) | ⑫ `start` ⑬ `make_ctx` ⑭ `open_scope`/`close_scope` ⑮ `intake` ⑯ `wait_policy` ⑰ `finish` ⑱ `release` | ⑫⑬⑭⑰⑱ 은 PR 1, ⑮⑯ 은 PR 2 |

`run(start)` 템플릿: 스코프 열기 → `make_ctx`(실패 = `LoopKindError` → `rejected`) → 엔진
`engine(query, ctx, **loop_kwargs)` → `finally: release()` → `finish` → (예외는 `crashed`) →
스코프 닫기. 엔진 호출 모양은 가짜 러너 계약과 같고, 기본은 `agent_cli.loop.run_loop` 를
호출 시점에 읽으므로 `patch("agent_cli.loop.run_loop")` 하나가 다섯 종류에 닿는다.

다섯 구현은 각자의 영역에 있다: `runtime.MainLoop` · `subagent.resident_loop.ResidentLoop` ·
`subagent.oneshot.OneShotLoop` · `skills.executor.SkillLoop` · `harness.jobs.HarnessLoop`.
one-shot 과 하니스는 공통 기반 `PrivateLoop`(private 주소·메일함 흡수 포트·끝나면
`release_private_owner`). 포트는 종류의 `make_ports` 정적 메서드(종전 `runtime.ports_for_*`
넷)가 짓는다 — `LoopPorts` 를 만드는 자리는 그것뿐이다(테스트가 AST 로 고정).

중첩 종류(one-shot·스킬)는 **호출자**를 `Caller` 로 받는다: 루프 안이면
`Caller.from_loop(cfg, state, ctx)`(돌고 있는 루프의 `LoopConfig` 가 곧 호출자의 정체), 루프
밖(`@agent`·`/skill` 명령)이면 `Caller.main_session(...)`. 기본값이 없어 하나라도 빠지면
`TypeError` — 주소 상속이 조용히 `"main"` 으로 새던 사고(docs/wiring)의 재발 방지.

원칙: 스레드와 수명(펌프·워커·작업 스레드·병렬 one-shot)은 종류 밖 — 호스트의 것. 주소는
문자열 그대로라 레지스트리의 라우팅(`deliver`·`fire_checkin`·`arrived_for`)은 바뀌지 않는다.
플러그인 레지스트리·문자열 디스패치·동적 등록은 없다 — 다섯 클래스를 코드가 직접 부른다.

## 3. 단계

| PR | 내용 | 상태 |
|---|---|---|
| 1 · 조립을 객체로 | 다섯 구현이 ①–⑧과 ⑫⑬⑭⑰⑱을 소유. `ports_for_*`·`run_subagent_message`·`_run_single`·`_run_in_scope`·`execute_skill`·`_handle_run_skill` 의 래퍼·`main._dispatch_*` 의 스코프 래퍼·`HarnessJobs._execute/_open_scope/_close_scope` 삭제. B(D3)·C(D2)·D 수정. | **v10.87.0** |
| 2 · 엔진이 묻는다 | ⑨⑩⑪(프롬프트 윤곽·꼬리 로스터·렌더)·⑮(흡수)·⑯(wait 정책)을 엔진이 `cfg.kind` 에 묻고 플래그 여덟 자리를 지운다. A(D1) 수정. | **v10.88.0** |
| 3 · 결과·마감 하나 | `ResidentLoop.finish` 가 `_handle_request`/`_handle_human_batch` 후처리를 한 벌로; 메일→레코드 세 함수 → 하나; `release` 가 python 워커까지(상주·private 통일). | 다음 |

### 3.1 PR 1 구현 기록 (v10.87.0)

검토 문서와 다른 점 — 전부 더 작게 간 쪽이다.

- **엔진에 `kind` 필드를 넣지 않았다.** 중첩 종류가 호출자에게서 읽는 것은 전부 `LoopConfig`
  (+ `state.stop_event`·ctx)에 있어 `Caller.from_loop` 가 그것을 읽는다. `LoopConfig.kind` 는
  PR 2 에서 엔진이 묻기 시작할 때 들어온다.
- **깊이·순환 가드(중복 9)는 남겼다** — `OneShotLoop.make_ctx` 와 `_handle_run_skill` 에. 엔진
  필터가 상한에서 도구를 빼므로 보통 디스패치는 거기 오지 않지만, 직접 호출(테스트·custom
  tools)이 같은 문구를 받는 계약을 테스트가 고정하고 있다.
- **상주의 결과 후처리(⑰의 나머지)는 레지스트리에 남겼다** — `ResidentLoop.finish` 는
  `(ToolResult, secs)` 만 돌려주고 회신·빚·`run_ended` 는 `_handle_request`/`_handle_human_batch`
  가 한다(PR 3). 질의 합성·회계(`start(items)`)와 작업 카드(⑭)는 종류로 왔다.

동작이 바뀐 자리 — 모델이 보는 글은 그대로다(inventory 바이트 비교: Working directory 줄만
차이).

- **D3** 스킬이 호출자의 역할·도구 범위를 받는다: `agent_role = caller.agent_role`,
  `tools = skill.allowed_tools ∩ caller.tools`. main 아래는 종전과 같고(역할 "" → ROLE_PROMPT,
  도구 전부), 상주 안의 스킬만 그 에이전트의 역할·allowlist 로 돈다.
- **D2** 하니스 주소가 `harness:<job>:<label>` — 겹치는 감사 둘이 서로의 메일함을 거두지 않는다.
- `/skill` 명령 길도 `OnSkillStart`/`OnSkillEnd` 훅을 받는다(종전엔 도구 길만). 훅의
  `tool_input` 은 도구 길이면 모델이 보낸 사전, 명령 길이면 `{"name", "arguments"}`.
- 스킬 ctx 가 `create_subagent_ctx("none", …)` 로 만들어진다 — 사고·문법·샘플링·스트림
  타임아웃·압축 비율을 부모에서 복사하고(종전엔 빠뜨렸다), 방언은 스킬이 고정한 모델의
  바인딩을 따른다(종전엔 부모 ctx 의 방언).
- 스킬 런의 예외는 두 길 모두 `run_skill(<name>) failed: …` 결과로(종전 `/skill` 은 예외가
  펌프까지 올라갔다). `verbose` 는 두 길 모두 호출자 것(종전 도구 길은 늘 False).
- 상주의 깊이가 spawn 시점 호출자 +1 로 고정된다(종전엔 런마다 "가장 최근 spawner 의 깊이
  +1" 을 다시 읽어 흔들렸다). 바인딩 고정도 에이전트마다 한 번.
- `tool_delegate(args, caller)`·`_run_parallel(specs, caller)`·`_handle_run_skill(skill_input,
  caller)` — 인자 더미가 `Caller` 하나로.

검증: 전체 테스트 8945 통과(27 skip), `ruff`, inventory 바이트 동일, 소스 AST 검사
(`LoopPorts(` 는 `make_ports` 안에서만, `Caller` 는 기본값 없음). **라이브 5종 통과**(2026-10-31,
웹 방 `g312ma`, GLM-5.3-Flash 사고 off, 이 브랜치의 editable 설치): ① main background 잡 →
`wait` → 잡 통지가 깨움 → 최종 답 `done-7` ② one-shot 이 background 잡 → `wait` 가 제자리에서
막힘(iter 2) → `[job] source=harness_mail` 레코드 → `4242` 를 50초에 보고 ③ spawn(general·pong) →
`wait` → `reply [n-9] re [r-8]` 로 깨어나 `pong-99` 보고 ④ `/plan …` 명령 → `skill_plan_…/` 에
history·result.md, 관찰 `run_skill` ⑤ 감사 노브 on → 런 뒤 `harness/auditor/21-28/` 7턴, main 에
`[auditor] source=harness` 로 `pass` 배달(주소 라벨 `21-28`).

### 3.2 PR 2 구현 기록 (v10.88.0)

엔진이 묻는 자리(`kind.py` 기본 틀의 B·C 묶음):

| 물음 | 엔진의 자리 | 종전 추측 |
|---|---|---|
| `wait_policy()` → `end_run`·`block`·`refuse` | `dispatch._op_wait` | `cfg.wakeable` + `is_private_owner(owner)` |
| `intake()` → `{"message"}`·`{"mail"}`·`{"record"}`·`{"user_request"}` | `core._intake` (넣는 법은 엔진: `_inject_message`·`_inject_mail`·`_inject_item`) | `dequeue_user_message`·`agent_registry`·`absorb_inbox` 포트 유무로 세 메서드 |
| `prompt_profile()` → role·peer_section·agent_registry·ask_blocking·reports_to_caller·peer_channel | `prompt.SystemPromptSvc.rebuild`, `llm`(함수 스키마·종결 어투), `dispatch`(ask 비차단), `tool_bridge`(거절 문구) | `agent_registry is None`·`depth > 0`·`questions.nonblocking`·`message_handler is not None` |
| `tail_roster(tools)` | `llm._build_session_state` | 레지스트리+`agent` 도구 / `peer_roster` 포트 |
| `renders()` | `core` 렌더 게이트 넷 | `not self.skill_name` |
| `tracks_run_authors` | `core._mirror_run_authors` | `dequeue_user_message is None` |

- `LoopConfig.kind` 가 들어왔고 `wakeable`·`peer_roster`·`peer_agents_section` 필드는 나갔다.
  `run_loop(kind=None)` — 종류 없이 부르면(테스트·임베더) `AgentLoop` 가 **맨 종류**를 만든다:
  `LoopKind` 기본 구현이 포트·깊이·이름에서 답을 유도한다(종전 추측 규칙이 한 곳에). 다섯
  종류는 자기 정책을 명시해 덮어쓴다. 종류 없이 만든 `LoopConfig` 도 `__post_init__` 이
  맨 종류를 세운다(`LoopKind.make_ports` — `LoopPorts` 를 만드는 자리는 여전히 `make_ports` 뿐).
- 설계와 다른 점: 흡수를 "어디서"(종류)와 "어떻게"(엔진)로 갈랐다 — 큐 항목의 명령 라우팅·
  조종 주입, 회신의 빚·장부·레코드 변환, 받은편지함 항목의 사람 요청 회계는 엔진 상태를
  만지므로 엔진에 남긴다. 종류가 돌려주는 것은 원천의 항목이다.
- **D1 적용**: `SkillLoop.intake() == []` — main 아래 스킬이 main 레지스트리의 회신을 흡수하지
  않는다. 그 메일은 스킬이 끝난 뒤 main 의 다음 턴이 받는다.
- 가드: `tests/test_loop_kinds.py::TestEngineAsksTheKind::test_no_kind_sniffing_left_in_the_engine`
  이 `loop/*.py`(kind·ports 제외)에서 종전 추측 표현 열 개를 금지한다.
- 검증: 전체 테스트 8951 통과, inventory 바이트 동일(Environment 제외 — 글 불변), **라이브 5종
  통과**(웹 방 g312ma 를 PR 2 코드로 재시작, 같은 세션 resume — pong 복원): ① main 잡 → `wait` →
  통지로 깨어나 `done-8` ② one-shot 잡 → 제자리 `wait` → `5151` 37초 ③ spawn pong2 → `wait` →
  `reply [n-21] re [r-20]` → `pong-77` ④ `/plan` → `skill_plan_e09469_…/` 6턴 ⑤ 감사 on →
  `harness/auditor/49-56/` 7턴 → main 에 `[auditor] source=harness` pass.

## 4. 결정 (2026-10-31, 사용자 승인 "D1~D5 추천대로")

- D1 스킬의 main 메일 흡수를 끊는다 → PR 2 (`SkillLoop.intake() == []`, 완료).
- D2 하니스 주소에 라벨 → PR 1 (완료).
- D3 스킬의 역할·도구 상속 — 죽은 인자를 살린다 → PR 1 (완료).
- D4 PR 1 → 검증·릴리스 → PR 2 → PR 3.
- D5 기본 틀은 `loop/kind.py`, 구현은 각자의 영역, 상주는 새 `subagent/resident_loop.py`.
