# 에이전트 엔진 — 엔진 하나, 수명 값 하나

모든 에이전트(`agent` `mode` "run" 의 run 에이전트와 `spawn` 의 상주 에이전트)는 레지스트리
하나(`subagent/agents_live.py::AgentRegistry`)에 오르고, 각자 자기 스레드(`agent-<key>`)에서 돈다.
run 과 spawn 의 차이는 에이전트 값 `ends_after_task` 하나다 — 참이면 과제 하나의 회신을 보내고
내려온다(채널은 회색으로 남는다), 거짓이면 다음 메시지를 기다린다.

이 문서는 지금 구조(P1)와 남은 단계(P2·P3)를 적는다.

## 1. 결정 (사용자)

- 엔진은 하나로 합친다 — 주소만 통일하는 안은 접었다.
- 결과는 요청자에게 가는 회신이다. 기다리는 회신이 남으면 `complete` 할 수 없다(P2).
- 정지는 명시적 UI 로, run·spawn 공통이다. 멈추면 빚진 상대 모두에게 알린다(P3).
- 상한은 run 도 똑같이 센다. 화면은 모두 채널이다.
- main 은 기다리는 회신이 있는 동안 제자리에서 기다리되, 사용자 입력에 깨어 그 일을 하고 회신은 계속 기다린다(P2).
- 방 ■ 는 main 만 멈춘다. 자식은 채널 ■ 또는 "모두 정지"(P3).
- P1 의 결과 어댑터는 회귀 방지로 두고, P2 에서 가드 테스트와 함께 지운다.
- 에이전트 사이의 말은 `message` / `reply` 한 쌍으로 합친다(P2).

## 2. 구조 (P1)

```
tool_bridge._invoke_agent  ─ run ─▶ run_agent.run_agents(args, Caller)
      │ OnAgentStart/End 훅이 감싼다            │
      │                                         ├─ delegation_checks.run_refusal   (빈 과제·provider·순환·깊이)
      │                                         ├─ AgentRegistry.start_run_agent  (상한·명단·agents.json·로스터·스레드)
      │                                         │     └─ 스레드: begin_prompt_scope → RunAgentLoop.run() → dead
      │                                         ├─ 회신을 막힌 채 기다림 (스레드 끝 = 회신 뒤 내려옴)
      │                                         └─ run_result.write_result_md_and_tool_result / batch_tool_result
      └─ spawn·request·… ─▶ agents_live.tool_agent → AgentRegistry.spawn (delegation_checks.depth_or_recursion_refusal)
```

| 모듈 | 하는 일 |
|------|---------|
| `subagent/run_agent.py` | 호스트 `run_agents` — 과제마다 거부 판정 → 레지스트리에 올림 → 기다림 → 어댑터. run 에이전트의 루프 종류 `RunAgentLoop`(`PrivateLoop`) |
| `subagent/run_result.py` | P1 결과 어댑터 — 회신(`RunReply`)을 `STATUS: success\nRESULT:\n…`·`STATUS: error\nERROR: Subagent did not complete\n…`·`[Task i]` 묶음·`artifact`·`result.md` 로. `subagent/report.py` 의 포맷터를 부른다. **P2 에서 지운다** |
| `subagent/delegation_checks.py` | 에이전트를 띄우기 전 거부 판정 — `run_refusal`(run), `depth_or_recursion_refusal`(run·spawn 공통). 쓰지도 띄우지도 않는다 |
| `subagent/agents_live.py` | 레지스트리 — `start_run_agent`(올리고 스레드를 띄운다)·`_run_agent_worker`(한 번 돌고 내려온다)·`_run_agent_tombstone`(resume 의 회색 개체). 런의 정책은 모른다 — 호스트가 `body` 로 준다 |

### 2.1 run 에이전트가 모델에게 보이는 것 — P1 은 바이트 불변

`RunAgentLoop` 의 정체는 상주(`ResidentLoop`)와 다르다: 주소 `run:<run_dir>`(private 메일함 — 잡·모니터·체크인
통지가 여기로), `wait` 는 제자리에서 막는다, run-only 축소 스키마, 시작 메시지는 과제 원문, ctx 집은 세션 디렉터리의
`run_<name>_<hash>_<ts>/`(결과 파일 자리와 같다).

**`RunAgentLoop` 는 P1 한정 임시 종류다** (결과 어댑터와 같은 대우). 사용자가 승인한 설계는 루프 종류 하나
(`ResidentLoop`)이고, 이 종류가 따로 있는 이유는 P1 의 모델 바이트 불변 하나뿐이다. 그래서 **종류 정책 메서드만**
든다 — 정체(주소·깊이·스택·바인딩·도구·훅·정지 이벤트), 시작(과제 원문), ctx(프로파일·바인딩 고정·ctx 생성),
스코프(채널 블록), 결과 모양(`RunReply`). 루프 본체는 엔진(`run_loop`), 스레드·상한·저장·로스터는 레지스트리의 것이다.
제거 조건은 §4.

모델 대면 명단에는 run 에이전트가 없다(`AgentRegistry._residents`) — `## Live Agents` 꼬리, `status`, 주소 해석
(`resolve_key`·`request`·`resume`), 중복 이름 검사, `wait` 의 깨울 관계, auto-spawn. 상한 계산(`alive_count`)에는 든다 —
넘으면 spawn 과 같은 문장(`agent_limit_refusal`)에 `Delegation rejected: ` 를 붙여 돌려주고, 병렬이면 그 과제의 `ERROR` 다.

등가성은 `tests/equivalence/test_run_result_parity.py` 가 고정한다 — 옛 일회성 엔진에서 만든 코퍼스(단건 성공·프로파일·
max turns·호출자 정지·fork·큰 결과 포인터·거부 여섯·병렬 둘·`@agent`)의 도구 결과·부모와 자식의 모델 요청 전부·
`result.md` 를 바이트로 비교한다.

### 2.2 정지

run 에이전트의 런은 **호출자의 stop Event** 를 따른다(방 ■·Ctrl+C 가 run 을 멈춘다). 기다리던 호출 스레드가 끊기면
(CLI Ctrl+C) 그 Event 를 세우고 그대로 던진다. 상주의 평생 stop(`AgentInstance.stop_event`, kill·shutdown)은 run 과
무관하다. 에이전트별 런 정지(■·✕)는 P3 이다.

### 2.3 저장·resume

run 에이전트는 `agents.json` 에 `ends_after_task: true`·`home: <run 디렉터리 이름>` 으로 남는다. resume 은 되살리지
않는다 — 끝나기 전에 프로세스가 죽은 것도 같다(그 회신을 받을 막힌 호출이 이미 없다). 회색 개체로 복원되어 화면
로스터와 채널 재생에만 쓰인다. 되살리기는 결과 = 회신이 되는 P2 의 일이다.

### 2.4 훅

`OnAgentStart`/`OnAgentEnd` 는 run 에만 발화한다 — `ToolBridge._invoke_agent` 가 `run_agents` 를 감싸므로 인자·시점은
`agent` 호출 하나에 한 쌍이다. 상주 spawn 에 붙이는 일은 P1 밖이다.

### 2.5 화면

- 웹: run 은 상주와 같은 채널이다 — 에이전트 스레드의 `begin_agent_work(…, ctx_dir=<run 디렉터리>, caller_task=<부른 자리>)`
  가 `scope_start` 를 낸다(`channel` = 키, `run` = 참). 프런트는 그 채널에 런 블록을 열고, 부른 자리(main 의 맨 위, 또는
  부른 에이전트의 런 블록)에 한 줄 카드 `→ agt-… <profile> · working/done · open channel` 을 놓는다. 끝난 run 채널은 회색
  칩이고 입력창은 닫혀 있다(run 은 과제 하나만 받는다). 같은 이벤트가 `scopes.jsonl` 에 실려 resume 재생도 같은 자리에
  같은 카드와 채널을 그린다(run 디렉터리의 history 를 통째로).
- CLI: 모든 에이전트(run·상주)는 자기 스레드의 평소 출력을 버리고, 런의 시작·끝과 끝난 도구 호출마다 한 줄을 오는
  순서대로 찍는다 — `  agt-3a1b2c4d │ shell  rg -n parse_tokens src/  ✓`.

## 3. 단계

| 단계 | 무엇 | 모델 바이트 |
|------|------|-------------|
| P1 (지금) | 엔진 하나 — run = 레지스트리 에이전트, 상한·저장·채널·훅·깊이 검사 공통. 화면: run 카드 → 채널 + 한 줄 카드, CLI 에이전트 줄. 결과 어댑터 | 불변 — 코퍼스 바이트 비교 |
| P2 | 결과 = 회신, `message`/`reply` 한 쌍(`agent` `request` → `message`). 어댑터 삭제 + 가드 테스트(모듈·함수 이름·`STATUS: success` 조립 문자열이 저장소에 없음). 장부의 "기다리는 회신" 항목, main 제자리 `wait`(회신·사용자 입력·체크인에 깸), 남으면 `complete` 되돌림. run 에이전트 되살리기 | 바뀜 — 35B·GLM A/B(짝 기준, 교차 순서) |
| P3 | 에이전트 런 단위 stop, 채널 머리 ■/✕, ■ 메뉴(main · 모든 에이전트 · 전부), `/stop` 슬래시. 빚진 상대 모두에게 `stopped by user` 회신 — kill 의 조용한 빚 버리기도 고친다 | 바뀜 — 회신 문구 실측 |

## 4. P2 완료 조건 — P1 임시물을 전부 지운다

사용자 결정: 지울 때 철저하게 없애 최종 디자인은 **단일 모듈**(루프 종류 하나 = `ResidentLoop`, 결과 = 회신)이다.
아래 이름은 `tests/test_agent_engine_transitional.py` 의 `TRANSITIONAL`·`TRANSITIONAL_STRINGS` 에 한 곳으로 적혀
있고, 지금은 "있다" 를 확인한다. **P2 는 그 테스트를 "저장소 어디에도 없다" 로 뒤집는 것으로 끝난다.**

| 지울 것 | 자리 | 대체 |
|---------|------|------|
| `RunAgentLoop` | `subagent/run_agent.py` | `ResidentLoop` + `ends_after_task` — 회신을 보내면 내려온다 |
| `RUN_ITEM` (런 이름 `"run"`) | `subagent/run_agent.py` | 요청 id `r-…`(장부의 기다리는 회신) |
| `_wait_for_replies` (막힌 기다림) | `subagent/run_agent.py` | 장부의 기다리는 회신 + main 제자리 `wait`(회신·사용자 입력·체크인에 깸) |
| `run_result` 모듈 — `RunReply`·`write_result_md_and_tool_result`·`batch_tool_result` | `subagent/run_result.py` | 회신(`reply`)이 곧 결과 |
| `STATUS: success\nRESULT:` / `STATUS: error\nERROR: Subagent did not complete` 조립 | `subagent/run_result.py` | 회신 본문 |
| `DelegateResult`·`_extract_activity_log`·`_extract_last_actions`·`_format_delegate_output`·`_format_parallel_results`·`_persist_run_result`·`extract_result_body` | `subagent/report.py` | 회신 모양(웹 지시문 편집기·`@agent` 는 회신 본문을 읽는다) |
| `RUN_OWNER_PREFIX` (`run:` 주소) | `constants.py` | run 에이전트도 `agent:<key>` — 받은편지함으로 통지를 받는다 |
| run 의 `wait` 제자리(`PrivateLoop` 의 WAIT_BLOCK 을 run 이 쓰는 것) | `RunAgentLoop` 와 함께 | 상주의 WAIT_END_RUN. `WAIT_BLOCK` 상수 자체는 하니스 작업·P2 의 main 제자리 대기가 쓰므로 남는다 |
| `AgentTool.SUBLOOP_PARAMETERS` (run 전용 축소 스키마) | `tools/agent_tool.py` | 모든 에이전트가 같은 도구 표면(§1의 run·spawn·status·resume·kill + `message`/`reply`) |
| `AgentRegistry.start_run_agent`·`_run_agent_worker` | `subagent/agents_live.py` | `spawn(…, ends_after_task=True)` 한 길 |
| `AgentRegistry._run_agent_tombstone` | `subagent/agents_live.py` | resume 이 못 끝낸 run 에이전트를 되살려 회신을 마친다 |
| `AgentRegistry._residents` (모델 대면 명단에서 run 빼기) | `subagent/agents_live.py` | run 에이전트도 명단·주소에 나온다(`message` 상대) |
| `build_live_agents_section` 의 `ends_after_task` 거르기, `resolve_key`·`format_status`·`kill`·`resume_teammate` 의 run 키 거절 | `prompts/system_prompt.py`·`subagent/agents_live.py` | 같음 — run 도 이름 있는 에이전트로 보이고, 정지는 P3 의 런 정지 |
| run 채널 읽기 전용(`ovActiveRun`·"This run agent takes no messages")·run 칩의 ✕/↻ 숨김 | `web/static/js/runs.js` | P2: 사람이 run 채널에 직접 쓴다(시안 C) · P3: 채널 머리 ■ 런 정지·✕ 제거. 끝난 run 의 회색 칩·`run` 표시는 최종 디자인이라 남는다 |
| `tests/equivalence/test_run_result_parity.py`·`expected/run_results.json` | 테스트 | P1 바이트 기준 — P2 는 바이트가 바뀐다(35B·GLM A/B 로 판정) |

