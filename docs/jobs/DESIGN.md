# 백그라운드 작업 — 하니스가 소유하는 긴 명령 (v10.47.0)

> 상태: **구현됨 (v10.47.0, 2026-10-17)**. 셸 안내 문구 A/B 의 결론. `agent_cli/jobs.py`.

## 0. 한 줄 요약

1분 넘게 도는 명령(테스트 스위트·빌드·설치)은 `shell` 에 `background: true` 로 넘긴다. 하니스가 **자식으로** 띄워 로그로 보내고, 끝나면 종료 코드와 함께 소유자에게 통지한다. 돌고 있는 동안은 매 턴 꼬리에 PID·로그 경로로 보인다. 모델은 폴링하지 않고, 패턴으로 kill 하지 않고, 끝 줄을 기다리는 monitor 를 걸지 않는다.

## 1. 왜 — 셸 안내 문구 A/B (2026-10-17, 과제 2종 × 3회 × 변형 3)

| 변형 | 문구 | check 과제 중앙값 | fix 과제 중앙값 | 사고 |
|---|---|---|---|---|
| A (종전) | "긴 명령은 timeout 을 올려라" | 450초 · 10턴 | 358초 · 24턴 | 모델이 알아서 `&` + monitor 를 썼는데, 스크립트가 쓰지 않는 끝 줄을 기다리다 2시간 deadline 까지 멈춤 1회 |
| B | "로그로 백그라운드, tail 로 확인" | 937초 · 22턴 | 529초 · 21턴 | tail 폴링이 턴마다 LLM 호출(최대 27회); `ps \| grep pytest \| xargs kill` 이 과제 문장을 argv 에 가진 agent-cli 자신을 죽임(SIGTERM) 1회 |
| C | "로그로 백그라운드, monitor 로 끝 줄" | 196초 · 8턴 | 330초 · 18턴 | `pkill -f "[p]ytest"` 가 끝 줄을 쓸 서브셸까지 죽여 monitor 가 영영 안 울림 → run 이 deadline 까지 멈춤 1회 |

세 변형의 사고는 전부 문구가 아니라 **구조** 탓이었다. ① 모델이 띄운 `cmd &` 는 재부모화돼 하니스가 종료를 모른다(그래서 monitor 의 `exit` 조건이 애초에 불가능했다 — docs/monitor/DESIGN.md §4.2). ② 종료를 모르니 모델이 로그를 보거나 프로세스를 찾아 죽인다 — 그 패턴이 자기 자신을 잡는다. ③ 끝 줄을 기다리는 monitor 는 writer 가 죽으면 영영 기다린다.

작업을 하니스가 들면 셋이 사라진다: 하니스가 부모라 `poll()` 로 종료 코드를 알고, 끝나면 통지하므로 볼 필요가 없고, PID 가 꼬리에 있으니 패턴 kill 이 필요 없다. 문구는 C 를 "작업 소유" 모양으로 고쳐 넣었다(Task Guidelines 한 줄 + `shell` 설명·`timeout` 설명).

## 2. 표면

- **도구**: `shell(command, background: true)` → 즉시 `job #n started (pid P) — output goes to .agent-cli/tmp/jobs/n.log. You will be told when it ends …`. 위험 키워드 확인·워크스페이스 봉쇄는 포그라운드와 같은 문(`_guard`)을 지난다.
- **로그**: `.agent-cli/tmp/jobs/<n>.log` (작업 디렉터리 기준; 모델 임시 파일의 자리와 같다). 첫 줄 `$ <command>`, 끝 줄 `[job #n exited rc=R after T]`.
- **통지**: 종료 시 소유자에게 `kind="job"` 메일 — main 이면 깨우기 런, 에이전트면 inbox, private 주소(`run:<dir>` one-shot·`harness:<job>`, v10.84.0)면 그 루프가 턴 경계·`wait` 에서 읽는 메일함(docs/wait/DESIGN.md §7). 관찰 레코드 `tool="job"`. 본문: `🧵 job #n finished rc=R after T — \`cmd\`` + 로그 마지막 8줄(줄당 300자). 우리가 죽인 작업(소유자 사망·세션 종료)은 통지하지 않는다.
- **꼬리**: `## Background jobs (running — you are told when each one ends; do not poll)` 아래 `- job #n · pid P · 경과 · \`cmd\` → 로그 · stop it: \`kill -- -P\`` (v10.79.0 — 프로세스 묶음째, `Job.stop_hint`; pid 는 `sh -c` 리더라 `kill P` 는 셸만 죽이고 자식이 살아남는다. `shell` 결과·Task Guidelines·`wait` 중간 깨우기 알림도 같은 모양, docs/wait/DESIGN.md §6.5). 자기 주소(`LoopConfig.owner`)의 작업만. 끝난 작업은 통지가 갔으므로 꼬리에서 빠진다(중복 금지 — 사용자 결정).
- **수명**: `runtime.session_has_live_work(jobs=)` 의 한 항. run 은 `QuietPolicy` 가 `on_wait("jobs")` 로 한 번 알리고 기다린다(`🧵 백그라운드 작업 N개 대기 중`), web 은 `web_instance_is_active` 가 거두지 않는다.
- **정리**: 소유자(에이전트) 사망 → `JobRegistry.drop_owner` 가 그 작업을 죽인다(`AgentRegistry._drop_monitors` 와 같은 자리). 세션 종료(`teardown_session`) → `stop_all` 이 남은 작업을 전부 죽이고 콘솔에 적는다. 프로세스 그룹에 TERM, 3초 뒤 KILL. 영속 없음(PID 는 프로세스와 함께 죽는다).
- **배선**: `build_job_registry()` → 프로세스 전역(`jobs.get_job_registry`, 도구가 닿는 길) + `wire_job_delivery(agent_registry, jobs)`(`deliver` seam, 역방향 `registry.jobs`). `build_agent_registry(jobs=)` 가 monitor 와 같은 자리에서 건다.

그냥 `cmd &` 는 종전대로 된다(추적 없음) — 안내가 `background: true` 로 유도한다.

- **명령은 평문으로 — 하니스가 거부한다 (v10.47.1).** 두 번째 재측정(E-fix-1)에서도 안내를 넘어 `nohup … & echo $!` 가 들어와 작업이 1초 만에 끝나고 진짜 일이 추적 밖으로 떨어졌다. `tool_shell_background` 가 `nohup`/`setsid`/`disown`·끝의 `&` 를 보면 거부하고 평문으로 다시 보내게 한다(`_detaches_itself`). 첫 재측정(D-fix-1)에서 모델이 `background: true` 에 `( … ) > log 2>&1 &` 를 그대로 넣어 작업이 1초 만에 "끝났다"(안쪽 `&` 가 진짜 일을 떼어 냈다). `background` 설명과 Task Guidelines 가 "`&`·리다이렉션·`echo EXIT` 없이 평문" 을 못 박는다.
- **`--result-file` 의 예외.** 질의 런이 백그라운드 작업을 **썼으면**(끝났어도 — 1초짜리 작업의 통지가 런 종료와 겹친 경합, E-fix-2) 그 뒤 깨우기 런들의 답을 결과 파일에 **덮어쓴다**(`main._ConsoleSurface.run_ended`, `_continuing`·`_used_jobs`). 사슬은 깨우기 런이 작업을 새로 띄우지도(`started_since(run_start)`) 돌리고 있지도 않을 때 닫힌다 — E-fix-2 처럼 깨우기 런이 진짜 스위트를 띄우는 경우를 따라간다. 작업 없이 끝난 질의는 v10.34.1 규칙 그대로. 실측 D-fix-1: 파일엔 "still waiting on job #2" 만 남고 진짜 요약은 깨우기 런에 있었다.

- **기다리는 동안은 `wait` (v10.65.0).** 아래 v10.47.1 의 "`complete` 로 기다린다고 말하라" 는 장부 P3(v10.56.0)의 `answers` 요구와 충돌했다 — 벤치 실측에서 같은 complete 가 9번 되돌려졌다. 이제 기다림은 `wait` 도구다(`docs/wait/DESIGN.md`); 안내·꼬리·넛지가 전부 그것을 가리킨다.
- **기다리는 동안 (v10.47.1).** 보드 방 실측(pwm1n9): 작업을 띄운 뒤 할 일이 없자 모델이 "기다리겠다" 산문만 7번 내 no_action 넛지를 받고, 넛지에 떠밀려 없는 파일 읽기·code_index 조회 같은 빈 행동을 하다 `complete` 했다(결과 자체는 통지 뒤 깨우기 런에서 정답). 규칙은 "행동 하나" 인데 할 행동이 없는 상태였다. 그래서 Task Guidelines 와 꼬리의 작업 블록 머리에 "할 일이 없으면 `complete` 로 기다린다고 말하라 — 통지가 깨운다, 빈 행동으로 턴을 채우지 말라" 를 넣었다. 넛지 수로 잰다(종전 D 런 check 8·4·5, fix 9·2·0).

## 3. 테스트 (`tests/test_jobs.py`)

띄움은 즉시 반환·PID·로그 머리; 종료는 로그 끝 줄·통지 본문(rc·경과·꼬리 8줄·머리/끝 줄 제외)·성공 여부·주소; kill 은 프로세스 그룹·무통지; `drop_owner` 는 그 소유자만; `stop_all` 은 전부 + closed; 닫힌 뒤 종료는 조용; 꼬리는 자기 작업만·경과·축약; 세션 상태 블록과 루프 조립; `shell` 표면(`background` true/"true"·기본 소유자·포그라운드 불변·레지스트리 없음·가드 적용·스키마); 수명(`session_has_live_work`·`QuietPolicy` 한 번 알림·web 술어·teardown 콘솔); 에이전트 사망 정리; 메일 → 관찰; 조립·문구.

## 4. 짝: monitor `match` 의 조용함 한도 (docs/monitor/DESIGN.md §12)

작업을 하니스가 들어도 모델이 로그에 `match` 를 거는 경우는 남는다(중간 진행 줄). 파일이 `silent`(기본 10분, 도구 인자, 0 = 끔) 동안 안 바뀌면 보고를 남기고 물러난다 — A·C 의 멈춤이 깨우기로 바뀐다.

## 5. foreground 시간 초과 (v10.77.0)

foreground 명령도 자기 프로세스 그룹으로 띄운다(`start_new_session=True`). 시간이 초과되면 그룹에 TERM, `KILL_GRACE_S`
뒤 남았으면 KILL — background 작업을 끝내는 `kill` 과 같은 순서. 계기: 9과제 벤치(GLM video)에서 `subprocess.run(shell=True,
timeout=)` 이 `/bin/sh` 만 죽였고, 그 아래 `apt-get` 이 dpkg 잠금을 쥔 채 남아 다음 `apt-get` 이 "Could not get lock …"
로 실패했다. 결과 문구는 끝냈다는 사실, 그때까지의 출력 끝 `TAIL_LINES`(8)줄, "이만큼 걸리는 명령은 `background` true" 교정 —
시간 초과 8건 중 5건이 foreground `apt-get install` 이었다(설명의 작업 종류 나열 "an install" 이 있는데도). 나열은 지우고
교정은 이 자리(트리거 직후)가 맡는다.
