# 감사자(auditor) — main 의 complete 마다 결과를 감사하고 되먹인다

> 상태: **구현됨 (v10.42.0, 2026-10-16~17) — 4판: 감사자는 팀원이 아니라 하니스 작업(`agent_cli/harness/`). 결과 전달은 하니스가 한다. §6.5 실측(전달 4/4·판정 3/4·되먹임이 D-3 산출물을 살림) 뒤 기본값 off, web 🔎 칩으로 세션 도중 on/off**
> 결정(사용자): ① 서수 구간만 읽는다 ② 결과는 사용자에게도 보인다 ③ 트리거는 **하니스가 main 의 런 종료마다** ④ 전달은 **감사 런의 `complete` 결과를 하니스가 main 메일함에** — 모델의 선택이 아니다 ⑤ 하니스 전용 에이전트는 **구조적으로 격리**: 레지스트리 밖, 프로파일은 `agents/harness/`, 기록은 `<세션>/harness/`, `history` 테이블 밖, 보드는 🤖 칩 대신 "결과 확인 중" ⑥ 켜고 끄기는 env 부팅값 + web 칩(기본 꺼짐).
> 판의 역사: 1판(하니스 파서·카드) → 2판(감사자가 `monitor` 로 구독, findings 만 `message`; §6.1·§6.2 실측에서 전달 2/10) → 3판(상주 팀원 + 하니스 요청·회신 빚; §6.3·§6.4 전달 5/5·4/4, 그러나 로스터·`message`·kill·상한·agents.json 에 노출) → 4판(이 문서).

## 0. 한 줄 요약

main 의 **사용자 런**이 `complete` 로 끝날 때마다 하니스(`harness.auditor.SessionAuditor.run_ended`)가 그 런의 history 구간 `seq a..b` 를 적은 과제로 **감사 런 하나**를 돌린다(`harness.jobs.HarnessJobs` — 일회성 서브에이전트, 프로파일 `agents/harness/auditor.md`). 감사자는 `history` 도구로 **그 구간만** 읽어 요청별 충족을 증거 서수와 함께 판정하고 `complete` 한다. 그 결과를 하니스가 main 메일함에 넣고(`registry.deliver("main", kind:"harness")`), `MailWaker` 가 main 을 깨운다. main 은 findings 를 새 런에서 손본다. 그 런은 감사하지 않는다(고리 ≤1, 코드). 켜고 끄기: `AGENT_CLI_AUDITOR`(부팅값, 기본 off) + web 🔎 칩(`/api/auditor`).

## 1. 왜

- main 은 자기 보고를 검증하지 않는다. 2026-10-16 압축 핀 실측에서 한 런이 40턴 중 17턴에 멈추고 "턴이 모자라 보고서를 못 썼다"고 했다.
- 지금 있는 완료 시점 검사는 `_require_answers`(미답 요청 id 를 채웠나) 하나다. 기계적이라 "id 는 채웠는데 내용이 답이 아니다"는 못 본다.
- 정적 필터는 주장·관찰 대조만 본다. 요청에 답이 됐는지는 LLM 이 봐야 한다 — 같은 모델이라도 종결문을 **쓰는 자리**와 **대조하는 자리**는 다른 과제다. 값어치는 §6 측정으로 확인한다.

## 2. 목표 / 비목표

목표: 사용자 런마다 complete 를 한 번 감사하고, 미충족이 있으면 main 이 한 번 더 손보게 한다. 감사는 증거 기반이다(충족을 보여주는 레코드 서수를 못 짚으면 미충족/확인불가). 사용자는 결과를 본다. 감사자는 **하니스의 것**이다 — 모델도 사용자도 팀원으로 다루지 못한다.

비목표: 정적 필터 단계. 감사가 main 을 블로킹하는 것. 세션 전체 history 접근. 감사의 감사. 감사자가 직접 고치는 것(쓰기 도구 없음). 감사자가 누구에게 묻는 것(`ask`·`message` 없음). 감사 결과의 파싱(하니스는 배달만).

## 3. 구조

```
main 세션                                          하니스 작업 auditor (일회성, <세션>/harness/auditor/<a>-<b>/)
  사용자 런 끝 (complete → history 에 final 한 줄)
    펌프: main_run_ended → SessionAuditor.run_ended(req, ok)
      사용자 런이고 ok 이고 enabled 일 때만 ─▶ HarnessJobs.run("auditor", "a-b", AUDIT_REQUEST):
      "Audit … seq a..b of session <sid>"            history(sessions:<sid>, loc LIKE '<sid>/history.jsonl:%', seq BETWEEN a AND b)
                                                     read_file / code_index (현재 워크스페이스)
                                                     complete(result = "AUDIT run seq a..b: pass | findings …")
    메일함 ◀── registry.deliver("main", {kind:"harness", job:"auditor", success, output}) ── 하니스가 넣는다
    MailWaker → 🤝 런: 관찰 "── auditor ──" → findings 면 손보고 complete (이 런은 감사 안 함)
```

### 3.1 하니스 작업 — 팀원이 아니다 (4판, 사용자 결정 ⑤)

3판은 감사자를 상주 팀원으로 띄웠다. 동작은 했지만(§6.3·§6.4) 레지스트리에 있는 한 **모든 조회면**에 실렸다: main 과 다른 팀원의 `## Live Agents` 로스터, `message(to:"auditor")`, `agent` 의 request/kill, `agents.json`, `max_agents` 한 자리, 보드 🤖 칩. 감사받는 쪽(main)이 감사자를 끄거나 "다시 봐서 pass 로" 라고 시킬 수 있는 구조였다. 필터로 가리는 대신 **구조**로 뺐다:

- `agent_cli/harness/jobs.py` `HarnessJobs` — 하니스가 자기 몫으로 돌리는 일회성 런의 실행기. `AgentRegistry` 에 들지 않으므로 로스터·주소 해석·manifest·상한·칩 어디에도 없다(격리 = 부재; `tests/test_harness_jobs.py` 가 고정). 팀원과 공유하는 것은 런 기계뿐: `create_subagent_ctx` + `run_subagent_message` + `ports_for_oneshot`(질문 포트·레지스트리·메시지 없음 → `ask`·`message`·`reply` 가 도구에 없다).
- 프로파일은 `agent_cli/agents/harness/<job>.md` — 사용자 카탈로그(`subagent/profiles` 의 검색 경로 `agents/builtin` + 프로젝트 `agents/`) 밖이라 `@agents`·spawn·delegate 가 이름 자체를 모른다. `disable-model-invocation` 같은 표지가 필요 없다. 프로젝트가 덮어쓰지도 못한다.
- 기록은 `<세션>/harness/<job>/<label>/` — 세션과 함께 지워지고 이전되지만 `history` 테이블 밖(§3.3).
- 결과는 `deliver(job, success, output)` → `registry.deliver("main", mail={kind:"harness", …})` — 모니터 보고가 이미 쓰는 길. main 은 `build_reply_record` 의 harness 레코드(`tool=<job>`, 빚 없음)로 본다.
- 수명: `in_flight` 가 `session_has_live_work(harness=)` 의 한 항 — run 은 결과 배달까지 기다리고(`QuietPolicy` 가 "🔎 감사가 끝나길 기다립니다" 한 번), web 의 idle self-reap 도 같은 술어. `teardown_session(harness=)` 가 세운다. 배달 **뒤에야** `in_flight` 가 줄어 결과 전에 세션이 끝나지 못한다.

### 3.2 세션 디렉토리 배치 — 한 곳의 선언

종전엔 만드는 쪽 셋(main ctx `history.jsonl`, 팀원 `agents/<key>/`, delegate/스킬 `run_*`/`skill_*`)이 각자 이름을 박았고, 읽는 쪽(`history` 도구)은 `rglob("history.jsonl")` 로 "세션 밑은 전부 대화" 라고 추정했다. 하니스 런은 모델의 대화가 아니므로 그 추정이 틀리는데, 로더에 "단 `harness` 는 빼고" 를 끼우면 규칙과 예외가 다른 모듈에 흩어진다. 그래서 `context/session.py` 가 배치를 **한 번** 선언한다: `AGENTS_DIR`·`RUN_PREFIX`·`SKILL_PREFIX`·`HARNESS_DIR`·`HISTORY_NAME`, `harness_run_dir()`, `conversation_history_files()`(모델이 한 대화의 기록 전부 = 하니스 subtree 만 제외). 만드는 쪽 넷과 읽는 쪽 하나가 이 이름만 쓴다. `tests/test_session_layout.py`: 세션 밑 모든 history 파일은 대화이거나 하니스 밑이다(조용한 누락 없음).

(처음 제안은 "대화 = 명시 목록(agents/·run_*·skill_*)" 이었는데, 이름 바뀌기 전 세션의 `delegate_*` 가 빠지고 새 종류의 런은 픽스처에 넣어야만 테스트가 잡는다 — 선언의 자리와 소유자는 같으니 "하니스 subtree 제외" 로 적었다.)

### 3.3 읽기 범위 — 서수 구간만 (결정 ①)

- 구간은 하니스가 잰다(`SessionAuditor` 의 커서 — 런 전 레코드 수 +1 .. 런 뒤 레코드 수; **모든** 런이 커서를 앞당겨 실패한 런·깨우기 런·꺼져 있던 동안의 레코드가 다음 구간에 섞이지 않는다). 읽기 제한은 지시다 — `history` 도구에 구간 고정 인자를 더하지 않는다. §6 에서 구간 밖 조회 횟수를 센다.
- 구간 밖이 필요해 보이는 경우(이전 런의 파일을 이번에 고쳤다)는 과거 대화가 아니라 **현재 워크스페이스**를 `read_file`·`code_index` 로 본다.
- `seq` 는 history.jsonl 의 1 기준 줄 번호(= 캐시 서수 hidx + 1). 테이블은 팀원·delegate 파일도 싣고 `seq` 는 파일별이라 모든 질의에 `loc LIKE '<sid>/history.jsonl:%'` 를 넣는다 — 과제문(`AUDIT_REQUEST`)과 프로파일이 강제. 감사 런 자신의 기록은 테이블에 없다(§3.2) — 포함하면 감사 과제가 사용자 요청 모양(`kind='query'`)으로 main 의 무필터 질의에 섞였다(4차 A-1 세션에서 확인). 감사 결과는 main 파일의 메일 관찰에 있으니 잃는 것이 없다.

### 3.4 판정 본문 — 문법 강제 없음

```
AUDIT run seq 99..142: findings
[12] unmet — asked per-module function names; 7 of 14 modules read (seq 103..137), REPORT.md never written
[13] met — seq 150 write_file REPORT.md; seq 151 final line is the checksum
claims: 1 verified, 1 unverifiable ("tests pass" — no shell observation in seq 99..142)
```

- 읽는 쪽은 사람과 main 뿐이다 — 하니스는 결과를 배달만 하고 파싱하지 않으므로 EBNF 문법을 걸 이유가 없다. 구조가 꼭 필요해지면 길은 문법이 아니라 **도구 호출**(`verdict {…}` 스키마). v1 범위 밖.
- 증거 규칙 둘(§6.1·§6.2 에서): 파일은 `read_file` 로 직접 판정하고, 잘린 열(`substr`)은 부재의 증거가 아니다. 두 번 직접 확인해 없으면 그게 결론이다.

### 3.5 전달 — 하니스가 한다 (4판)

- 1·2판은 모델이 `message` 를 고르게 했다(전달 2/10). 3판은 회신 빚으로 강제했다(전달 9/9 — 그러나 `complete` 거절·독촉·폴백·이중 회신이라는 기계가 따라왔고, native_fc 의 빈 턴 레코드 버그도 거기서 드러났다). 4판은 전달을 모델에서 **완전히 뺀다**: 감사 런의 `complete` 결과가 곧 보고이고 하니스가 그걸 메일함에 넣는다. 2차 실측의 "이 모델은 `complete` 가 곧 보고" 라는 습관이 그대로 전달 수단이 된다.
- 런이 턴 소진·오류·크래시로 `complete` 없이 끝나면 하니스가 "auditor did not complete: <사유>" / "auditor crashed: …" 를 대신 배달한다 — 판정문은 아니지만 "결론을 못 냈다" 는 사실이 전달된다.
- 회신 → main 메일함 → `MailWaker` 가 유휴 main 을 깨움 → 🤝 런에서 관찰 `── auditor ──` 로 들어가고 **main 채널에 카드**로 그려진다. 사용자는 이걸 본다. pass 도 같은 길(§7 A 해소) — 비용은 사용자 런마다 main 의 짧은 깨우기 런 하나.
- 고리: `run_ended` 는 `req.wake` 또는 `not req.author_is_user` 면 돌리지 않는다. 사용자 런 1 : 감사 1 : 되먹임 ≤ 1.

### 3.6 비용·상한

- 감사 런 = 과제 한 줄 + history 조회 1~3회 + read_file 몇 개 + 판정 + complete. 컨텍스트는 감사마다 새로 시작해 쌓이지 않는다(3판 상주는 감사마다 커졌다). 턴 상한은 delegate 와 같은 값(`AgentRuntime.max_turns`).
- 추가 비용은 사용자 런당 감사 런 하나 + main 의 깨우기 런 하나(pass 면 한두 턴). §6 에서 실측.
- 모델: main 과 같은 바인딩(프로파일 `model` 비움). 세션 셸 훅은 감사 런에 싣지 않는다.

### 3.7 켜고 끄기 — 부팅값 + web 칩 (결정 ⑥)

- `AGENT_CLI_AUDITOR`(기본 off)는 `SessionAuditor.enabled` 의 **초기값**이다. 실행기(`HarnessJobs`)는 세션이 있으면 늘 조립되므로 세션 도중 켜고 끌 수 있다.
- web: 🔎 칩(`#auditor-wrap`, 📐 문법 노브와 같은 틀) ↔ 노브 `auditor` 의 web 표면 `GET /api/knobs`·`POST /api/knobs/auditor` `{value}` ↔ sticky `knob:auditor`(`in_flight` 포함) 로 뷰어 동기화 (v10.44.0, docs/knobs/DESIGN.md §5.1). 끄면 다음 사용자 런부터 감사하지 않고, 진행 중 감사는 끝까지 돌아 결과가 배달된다. 세션 한정 — 재시작 시 env 값.
- 보드: 감사자는 레지스트리에 없으니 🤖 칩에 안 보인다. 대신 `status.json` 의 `auditing`(하니스 작업 `on_change` → `WebRenderer.note_harness`)으로 행 상태 "결과 확인 중"(agent-board v1.35.0; 우선순위 ❗ 응답 필요 > 응답 중 > **결과 확인 중** > 에이전트 작업 중 > 대기).
- run: 부팅 때 "🔎 세션 감사자 켜짐" 한 줄(켜져 있을 때만), 감사만 남아 기다릴 때 "🔎 감사가 끝나길 기다립니다" 한 번.

### 3.8 하니스 변경

| 파일 | 변경 | LOC |
|---|---|---|
| `agent_cli/harness/jobs.py` · `auditor.py` | 하니스 작업 실행기 · 감사자 정책(커서·`run_ended`·`enabled`·`state`) · `build_auditor` | +287 |
| `agent_cli/agents/harness/auditor.md` | 프로파일 4판 (builtin 에서 이동; 보고 = `complete` 결과; 도구 셋) | 84 |
| `agent_cli/context/session.py` | 배치 선언: 이름 상수·`harness_run_dir`·`conversation_history_files` | +43 |
| `agent_cli/tools/history.py` · `subagent/agents_live.py` · `subagent/report.py` · `loop/skill_invoke.py` | 배치 이름을 선언에서 가져온다; `build_reply_record` harness 레코드; `instance_for` 제거 | ±30 |
| `agent_cli/runtime.py` | `SessionAuditor`/`ensure_auditor` 제거 → `build_auditor`; `session_has_live_work(harness=)`; `teardown_session(harness=)` | −68 |
| `agent_cli/pump.py` · `main.py` | `QuietPolicy(harness=)` + 대기 안내; 두 펌프·idle 술어·종료에 핸들; `server.auditor`; 메일 알림 | +25 |
| `agent_cli/render/web.py` · `web/server.py` · `web/instance_file.py` · `web/static/*` | `broadcast_auditor`·`note_harness`; `/api/auditor`·`health.auditing`; `status.json auditing`; 🔎 칩 | +120 |
| `agent_cli/constants.py` | `AUDIT_REQUEST` 4판 문구 | ±8 |
| tests | `test_auditor.py`(35)·`test_harness_jobs.py`(14)·`test_session_layout.py`(5); runtime_assembly·monitor_owner_routing·packaging 수정 | +600 |
| docs | README(노브·동작), ARCHITECTURE(harness/·session.py·history.py 외), 이 문서 | — |

3판의 `instance_for` 툼스톤 조회, 회신 빚 의존, `memory` 도구, 상주 spawn/resume/kill 분기는 전부 사라졌다. 턴 레코드 수리(native_fc 가상 op 빈 기록, `tests/test_server_parsed_virtual_ops.py`)는 하니스 버그라 남는다.

### 3.9 화면 — 감사 런은 자기 카드 안에 (v10.53.0)

하니스 작업이 렌더러 스코프를 열지 않아 감사 스레드의 카드가 전부 main 의
것으로 그려졌다(web 은 `_thread_to_task.get(tid) or _MAIN_SCOPE`, CLI 는 그냥
출력) — "격리는 부재" 를 레지스트리·주소·로스터에는 세웠는데 렌더 경로 한 곳이
빠졌다. 사용자가 "main 이 감사 동안 기록을 뒤진다" 로 읽었다(런 기록 대조로
확인: main 기록은 그 시간대에 연산 0, 콘솔 줄은 감사자 기록과 1:1).

`HarnessJobs._run` 이 런 하나를 `begin_scope(kind="harness", parent="",
ctx_dir="harness/<job>/<label>")` / `end_scope` 로 감싼다(렌더러 오류는 런·배달을
막지 않는다). web: 접힌 `🔎 <job> · seq <label>` 카드(회색 레일 `scope-harness`),
resume 은 `ctx_dir` 기록으로 카드 안을 다시 채운다. CLI: 상주 에이전트처럼 그
스레드 출력을 캡처해 버리고 끝에 `🔎 <job> · <label> ✓ (Ns)` 한 줄. 모델 컨텍스트는
그대로다(감사 런은 자기 ctx, main 에는 결과 메일 한 장).

## 4. 시퀀스 (findings)

1. 사용자 런 #7: 요청 [12] "모듈별 함수 이름을 REPORT.md 에" → main 이 7개만 읽고 complete(answers=[12]) → history 99..142 줄.
2. 펌프: `run_ended(req 사용자, ok)` → `HarnessJobs.run("auditor", "99-142", "Audit … seq 99..142")` — 스레드 하나, `in_flight`=1, 보드 "결과 확인 중".
3. 감사 런(`harness/auditor/99-142/`): 구간 조회, read_file 7건·write_file 없음 → `complete("AUDIT run seq 99..142: findings / [12] unmet …")`.
4. 하니스: `deliver` → main 메일함(kind harness) → `in_flight`=0. 📨 "🔎 auditor 결과 도착". MailWaker → 🤝 런 #8: main 이 나머지를 읽고 REPORT.md 를 쓰고 complete.
5. 펌프: `run_ended(req.wake)` → 감사 없음. 조용해지면 run 종료.
6. 사용자 런 #9 가 오면 1 부터.

## 5. 위험

- **감사자가 SQL 지시를 안 지킴**(구간 밖): 측정 지표로 둔다. 새면 v2 도구 클램프(§3.3).
- **예스맨**: 증거 서수 요구로 줄인다. 그래도 서수를 지어낼 수 있다.
- **감사자의 오류**: 틀린 findings 는 main 을 한 번 더 돌린다(최대 1회). main 은 감사문을 관찰로 볼 뿐 따를 의무가 없다. §6.1 의 오탐 둘은 증거 규칙으로 줄였다.
- **폭주**: 없는 파일을 계속 찾는 식의 탐색(§6.2 B-3)은 턴 상한이 끊고, 하니스가 사유를 배달한다.
- **격리의 역방향**: 감사자가 main 이나 팀원에게 말을 걸 길은 없다(`message`·`ask` 없음). 사용자가 감사자와 대화할 길도 없다 — 의도다. 판정을 고치려면 프로파일을 고친다.

## 6. 측정 계획 (구현 뒤, 기본값 결정 전) — 동의됨

환경: Flash Next, thinking off, 창 32K 런처, `AGENT_CLI_AUDITOR=1`, 과제당 5회, 비교 기준은 감사 off.

| 과제 | 심은 결함 | 보는 것 |
|---|---|---|
| A 보고서 작성(14 모듈) | 없음 | 오탐률 |
| B 보고서 + 두 번째 요청 주입 | 하나만 답하기 쉬움 | 미충족 포착률 |
| C "테스트를 돌리고 보고" | 테스트 파일 없음 | 허위 "통과" 의 unverifiable 포착률 |
| D 긴 과제, 턴 상한 20 | 중간 포기 "턴 부족" | 포착률 + 되먹임 1회 해결률 |

지표: 포착률, 오탐률, 되먹임 1회 해결률, 고리(사용자 런당 감사 ≤1·되먹임 ≤1 — 로그로 검증), 완료당 추가 토큰·지연, **구간 밖 조회 횟수**, **깨우기 런 건너뛰기 정확도**.

통과 기준(제안): 결함 과제 포착 ≥ 4/5, 깨끗한 과제 오탐 ≤ 1/5, 되먹임 1회 해결 ≥ 3/5, 완료당 추가 토큰 ≤ main 런의 1/3, 건너뛰기 5/5. 못 미치면 기본값 off 유지, 프롬프트는 증거 규칙 쪽으로만 손본다(과제 통과용 튜닝 금지).

### 6.1 1차 실측 (2026-10-16, 프로파일 1판 — monitor 구독·message 전달, A/B/D × 3)

C 는 뺐다 — 테스트 파일이 없는 프로젝트에서 "통과했다" 는 허위 주장을 결정적으로 심을 방법이 없다(운에 기대는 과제). D 는 턴 상한 12.

| 런 | main 의 결과 (실제) | 감사자 판정 | 맞음 | message→main | 깨우기 런 |
|---|---|---|---|---|---|
| A-1 | 14 읽고 REPORT.md 작성 | pass | ○ | — | — |
| A-2 | "complete" 인데 REPORT.md 없음 | findings(unmet) → 깨우기 런에서 작성 → pass | ○ ○ | ○ | 1, 해결 |
| A-3 | 3/14 읽고 포기 complete | findings(unmet) | ○ | **×** | — |
| B-1 | 작성, 체크섬 마지막 줄 | findings: [1] met, **[14] unmet** | **오탐** | × | — |
| B-2 | REPORT.md 없이 complete | findings(unmet ×2) | ○ | **×** | — |
| B-3 | 작성, 체크섬 마지막 줄 | findings: [1] met, **[13] partial** | **오탐** | × | — |
| D-1 | 턴 상한 → final 없음 | (발화 없음) | — | — | — |
| D-2 | 9/14 읽고 complete | findings → 깨우기 런에서 완성 → pass | ○ ○ | ○ | 1, 해결 |
| D-3 | 턴 상한 → final 없음 | (발화 없음) | — | — | — |

하니스 몫은 전부 맞았다: 모니터 등록 9/9 한 번씩, final 마다 발화 8/8, `sessions:` 누락 0, main 파일 필터 누락 0(§3.3 수정 뒤), 되먹임 고리 ≤1 (A-2·D-2 둘 다 깨우기 런 하나로 끝, 그 런은 건너뛰거나 "closure only" 로 판정), 아홉 런 모두 rc=0 으로 종료(§3.6a passive).

모델 몫에서 셋이 드러났다:

1. **전달 누락 4/6** — findings 를 `complete` 에만 쓰고 main 에게 `message` 하지 않았다(A-3·B-1·B-2·B-3). 1판 §5 는 "findings → message, then complete" 를 불릿 하나로 적었다. 2판: 번호 절차로, `message` 가 먼저이고 "`complete` reports to no one" 을 명시.
2. **오탐 2/2 (B-1·B-3)** — 파일은 `…\n\nchecksum: 4471-zeta\n` 으로 끝나 요구를 충족하는데, main 이 찍은 `tail`/`od -c` 전사를 읽고 "빈 줄이 뒤따른다/앞선다" 고 판정했다. B-1 은 또 `substr(text,1,120)` 으로 잘린 배치 레코드(한 턴 7 ops)를 보고 "ledger/metrics 는 read_file 로 읽지 않았다" 고 했다 — 실제로는 읽었다. 2판 §4: "파일은 파일로 판정(read_file)", "잘린 열은 부재의 증거가 아니다".
3. **전달 뒤 구경 (D-2)** — message 뒤 8턴 동안 main 의 history 를 다시 읽으며 반응을 지켜보다 다음 발화에 흡수됐다. 2판 §5: 전달 다음 턴에 complete, main 을 다시 읽지 않는다.

D 의 턴 상한 12 는 main 이 포기 complete 조차 못 하고 죽는 값이었다(2/3 final 없음 → 감사 대상 없음). 2차는 16 으로 — 14 모듈을 다 읽을 수는 없고(최소 17턴) 포기 complete 는 할 수 있는 값. 비용: 감사 런당 액션 4~18, 감사자 history 레코드 17~103개/세션.

### 6.2 2차 실측 (2026-10-16, 프로파일 2판 — §6.1 의 증거 규칙·번호 절차, A/B/D × 3, D 턴 16)

| 런 | main 의 결과 (실제) | 감사자 판정 | 맞음 | message→main |
|---|---|---|---|---|
| A-1 | "REPORT.md 썼다" 는데 파일 없음 | findings(unmet, 주장 근거 없음) | ○ | × |
| A-2 | 14 읽고 작성 | pass | ○ | — |
| A-3 | 5/14 읽고 포기 | findings | ○ | × |
| B-1·B-2 | 턴 40 소진, final 없음 | (요청 없음) | — | — |
| B-3 | "완료·검증" 이라는데 파일 없음 | 판정 없음 — 파일을 40턴 찾다 턴 소진 | × | — |
| D-1 | 턴 소진, final 없음 | (요청 없음) | — | — |
| D-2 | 10/14 를 read_file, 보고서는 정확 | findings(내용 met, 방식 unmet) | ○ | × |
| D-3 | 8/14, 파일 없음 | findings | ○ | × |

증거 규칙은 들었다(파일을 직접 읽어 판정, 1차의 체크섬 오탐 재발 없음, 판정 5/6). 전달은 **0/4** — "FIRST message" 로 바꿔도 변화가 없었다. D-2 는 `reply` 를 불렀다가 "회신할 요청이 없다, message 를 쓰라" 는 오류를 받고도 message 로 가지 않았다. 도구가 안 보여서가 아니다: 프로파일로 상주 루프를 조립해 뽑아 보면 tools 배열에 `message`(설명 전문)·`reply` 가 있고 꼬리의 `## Live Agents` 가 `main` 을 수신자로 안내한다. 이 모델에게 `complete` 가 곧 보고라는 습관이 센 것이고, 문구로는 안 풀린다 — 그래서 3판은 전달을 모델의 선택에서 빼 **회신 빚**으로 강제한다(사용자 제안). 또 하나, 거짓 종결문 + 없는 파일 조합(B-3)에서 모델이 같은 경로를 40턴 재조회했다 — 프로파일에 "두 번 직접 확인해 없으면 그게 결론" 을 넣었다.

곁다리: 상주의 `message`/`reply` 호출은 history 에 `kind: raw` 빈 본문으로만 남고 호출 자체(수신자·본문)가 기록되지 않았다 — §6.3 에서 원인이 드러나 같은 릴리스에서 수리(아래).

### 6.3 3차 실측 (2026-10-17, 3판 — 하니스 요청·회신 빚, A×3 / D×3 턴 16)

B 는 뺐다 — 이 모델·32K 창에서 main 이 2/3 턴을 소진해 감사 대상(final)이 안 생긴다(과제 설계 문제).

| 런 | main 의 결과 (실제) | 감사자 회신 | 맞음 | 전달 | 깨우기 런 |
|---|---|---|---|---|---|
| A-1 | 14 읽고도 REPORT.md 안 씀 | findings(unmet) | ○ | reply | 3턴, REPORT.md 작성·complete |
| A-2 | 14 읽고 작성 (정확) | findings(방식 unmet) — 그 뒤 스스로 pass 로 결론, 정정 reply 는 거절됨 | **오탐** | reply | 2턴, "동의" 하고 complete |
| A-3 | 14 읽고 작성 | pass | ○ | reply | 1턴, complete |
| D-1 | 6/14, 파일 없음 | findings | ○ | reply | 16턴, REPORT.md 작성·complete |
| D-2 | 턴 소진, final 없음 | (요청 없음) | — | — | — |
| D-3 | 14/14 읽고 파일 없음 | findings | ○ | reply | REPORT.md 는 썼으나 16턴 소진, complete 못 함 |

- **전달 5/5** (전부 `reply`, 폴백 0) — 1·2차의 2/10 이 사라졌다. 회신은 main 채널에 "auditor reply" 카드로 보인다.
- **고리**: 감사자 history 에 런이 세션당 하나뿐(깨우기 런에 요청 없음), 깨우기 런 ≤1. 여섯 런 모두 rc=0.
- **범위**: `sessions:` 누락 0, `loc LIKE` 누락 0, 구간 밖 조회 0.
- **판정 4/5** 맞음. 오탐 A-2 는 감사자가 첫 reply 뒤 더 확인하고 결론을 뒤집었는데(자기 complete 는 pass) 두 번째 reply 가 "이미 회신함" 으로 거절돼 정정이 못 갔다. main 은 2턴에 끝냈으니 비용은 작다.
- **되먹임 해결**: findings 4건 중 3건이 깨우기 런에서 파일을 썼고 그중 2건은 complete 까지(D-3 은 턴 16 소진).
- **비용**: pass 깨우기 런 1턴. 감사 런은 액션 4~11(A-2 만 39).
- **`reply` 두 번 (6/6) — 하니스 버그였다.** 서버 파싱 방언(native_fc)에서 턴의 구조화 기록은 `corrected_record` 선택 인자로 흘렀는데 가상 op 경로(reply·message·answer·ask)만 그걸 빠뜨려 `{"content": ""}` 를 기록했다. 다음 호출의 prior 에 자기 호출이 없으니 모델은 "아직 회신 안 했다" 고 보고 다시 보냈다(두 번째는 "이미 회신함" 거절). 수리는 인터페이스: `_handle_text_path` 가 `ParsedTurn.record` 를 한 번 만들고 `_append_observation` 이 원문 대신 그 레코드를 필수로 받는다 — 잊을 자리가 없다. 회귀 가드는 `_dispatch_op` 소스에서 특별 취급 op 목록을 뽑아 전부 돈다(`tests/test_server_parsed_virtual_ops.py`).

기본값은 off 유지(§7 B): 값어치는 보였지만 사용자 런마다 감사 런 + 깨우기 런이 붙는 비용이고, 오탐이 main 을 한 번 더 돌린다.

### 6.4 4차 실측 (2026-10-17, 3판 + 턴 레코드 수리, A×3 / D×3 턴 16)

| 런 | main 의 결과 (실제) | 감사자 회신 | 맞음 | reply | 깨우기 런 |
|---|---|---|---|---|---|
| A-1 | 14 읽고 작성 | pass | ○ | 1 | 1턴 |
| A-2 | 파일은 있으나 셸 덤프 전사를 베낀 것, write_file 없음 | findings(방식 unmet) | ○ (첫 줄 "never written" 은 과함) | 1 | 17턴, 전부 read_file 로 다시 읽고 complete |
| A-3 | 14 읽고 작성 | pass | ○ | 1 | 1턴 |
| D-1 | 작성했으나 배치 read·잘린 읽기로 이름 외삽 | findings(편차 2) | ○ | 1 | 16턴, complete |
| D-2·D-3 | 턴 소진, final 없음 | (요청 없음) | — | — | — |

- **`reply` 1회 4/4, 거절 0, `kind: raw` 0** — 수리 전 6/6 이던 이중 회신이 사라졌고 회신 호출이 수신 id·본문과 함께 기록된다.
- 전달 4/4(reply), 고리 ≤1, `sessions:`·파일 필터 누락 0, 여섯 런 rc=0. 판정 4/4 맞음(A-2 는 첫 줄 표현이 과하나 방식 위반은 사실).
- D-1 깨우기 런에서 main 이 존재하지 않는 경로로 `cd … || true` 를 친 흔적이 있다 — 파일은 안 생겼고(봉쇄 밖 쓰기 없음) 모델의 환각 경로.

### 6.5 5차 실측 (2026-10-17, 4판 — 하니스 작업·complete 전달, A×3 / D×3 턴 16)

고정 worktree(4판 커밋)에서 `PYTHONPATH` 로 돌렸다(편집 중인 설치본으로 돌린 첫 시도는 5/6 런이 시작 직후 깨져 폐기).

| 런 | main 의 실제 결과 | 감사 판정 | 맞음 | 감사 런 도구 호출 | 깨우기 런 |
|---|---|---|---|---|---|
| A-1 | 14개 읽고 작성 | pass | ○ | 39 | 1턴 |
| A-2 | 13개 읽고 notify.py 는 code_index + `^def` 검색 읽기, 작성 | findings(notify.py 미열람) | ○(부분 — `^def` 검색 읽기는 있었다) | 16 | 6턴, notify.py 읽고 complete |
| A-3 | 14개 읽고 작성(search.py 18개 전부 있음) | pass + "search.py 6개 누락" 본문 | ✗ 오탐(파일에 있다, 머리 pass·본문 findings 불일치) | 6 | 5턴, 파일 확인 후 "판정이 틀렸다" 로 complete |
| D-1, D-2 | 턴 소진, final 없음 | 감사 없음(설계대로) | — | — | — |
| D-3 | REPORT.md 미작성, 모듈은 shell grep 으로 수집 | findings(미작성·방식 위반·부산물) | ○ | 18 | 9턴, 수집본으로 REPORT.md 작성 |

- **전달 4/4** — 구조로 보장되는 것이라 실측 지표에서 빠진다. 거절·이중 회신·폴백 없음. `agents/` 디렉토리·`agents.json` 항목 없음, 기록은 `harness/auditor/<a>-<b>/`.
- **판정 3/4.** A-3 오탐: 보고서의 `search_load_06`~`search_emit_11` 은 파일 272~277줄에 있는데 "271→272 로 건너뛴다" 고 썼다 — 범위 읽기의 잘린 구간을 부재로 읽은 것(§3 "A cut column is not evidence" 가 겨냥한 바로 그 오류). main 은 파일을 다시 읽어 판정을 기각했고 5턴을 썼다. 머리에 `pass` 를 쓰고 본문에 findings 를 적은 형식 불일치도 같은 런.
- **되먹임이 일을 고쳤다.** D-3 는 감사가 "수집본이 `.agent-cli/tmp/names.txt` 에 있다" 고 짚어 주어 main 이 9턴에 REPORT.md 를 썼다 — 턴 소진으로 못 쓴 산출물을 되먹임 한 번이 살렸다. A-2 는 빠진 파일 하나를 읽고 끝냈다.
- **비용.** 감사 런 도구 호출 6~39회(A-1 은 14 모듈을 전부 교차 검증). 컨텍스트는 감사마다 새로 시작한다. 깨우기 런은 pass 1턴, findings 5~9턴.
- **결론.** 기본값 off 유지(사용자 결정 ⑥, web 🔎 칩으로 켜 가며 쓴다). 오탐 1건은 프로파일 §3 의 기존 규칙 위반이라 문구를 더 보태지 않는다(과제 통과용 튜닝 금지) — 사례가 쌓이면 범위 읽기 뒤 전체 읽기를 요구하는 쪽으로 본다.

### 6.6 6차 실측 (2026-10-17, 4판 회귀 후보 재점검 — A×3)

§6.5 에서 보인 두 신호(판정 4/4→3/4, 감사 런 턴 평균 7.5→18·상한 도달 1)를 같은 조건으로 다시 쟀다.

| 런 | main 의 실제 결과 | 감사 판정 | 맞음 | 감사 런 턴 | 깨우기 런 |
|---|---|---|---|---|---|
| A-1 | 14개 중 13개를 `search=` 검색 읽기(잘린 결과)로, 이름은 패턴에서 추론, 작성(내용은 맞음) | findings(방식 편차·이름 미입증) | ○ | 7 | 1턴, 확인만 |
| A-2 | 턴 소진(40), final 없음 | 감사 없음 | — | — | — |
| A-3 | billing.py 는 code_index 만, shell 로 일괄 카운트, 작성(내용은 맞음) | findings(billing 미열람·일괄 shell) | ○ | 7 | 4턴, billing.py 읽고 complete |

- **비용은 재현되지 않았다.** 4판 감사 런 턴 여섯 건: 40·14·5·14·7·7 (중앙값 10.5), 3판 네 건 6·11·8·5 (중앙값 7). 5차 A-1 의 40 은 분산의 꼬리로 본다 — 다만 상한에 닿은 1건은 사실이라, 감사 런이 `did not complete` 로 끝나는 경우가 쌓이면 감사 전용 턴 상한을 둔다(§7 D).
- **판정은 4판 누적 5/6** (오탐 1 = 5차 A-3). 6차 둘 다 머리·본문이 일치하고 두 findings 모두 main 기록으로 확인했다(13/20 검색 읽기, billing.py 는 code_index 뿐).
- **되먹임 비용**은 pass·확인만 1턴, findings 4턴.
- **결론.** 회귀로 보지 않는다. 4판 그대로 릴리스하고 기본값 off 를 유지한다.

## 7. 열린 질문

- **A. pass 를 main 채널에도 보일지** — 해소: 결과 카드라 pass 도 보인다. 비용은 사용자 런마다 main 의 짧은 깨우기 런 하나.
- **B. 기본값**: off 유지. web 🔎 칩으로 세션마다 켜 보며 쓴다(사용자 결정 ⑥).
- **D. 감사 전용 턴 상한** — 감사 런이 main 의 `--max-turns` 를 그대로 쓴다. 상한 도달(5차 A-1)이 반복되면 20 안팎의 전용 상한을 둔다(과제 통과용 문구 튜닝 대신 구조).
- **C. `agent-cli run` 에서의 노브 제어** — 별도 조사(사용자 업무 2·3번: run 의 `/노브` 또는 실행 인자, web 노브와의 대칭을 구조로 강제하는 설계).
