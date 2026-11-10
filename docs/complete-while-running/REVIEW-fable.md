# 리뷰 — `complete` 로 끝낸 런에 작업이 남는 구멍 (DESIGN.md 검토)

리뷰어: Fable 5.1 · 2026-11-10 · 기준 코드 main `efbe073b` (v10.120.2)
방법: 문서 주장을 코드에서 다시 확인했다(§8 코드 지도 전부, 호출부 포함). 부록 B.1 프로브와
B.3 기록 스캔은 직접 다시 돌렸다. 라이브 런(B.2)은 돌리지 않았다 — 모델 서버를 다른 사용자가
쓸 수 있어서. 라이브로만 확인되는 주장은 그렇다고 적었다.

---

## 0. 판정 요약

**Q0: 지금은 고치지 않는다 (A). 모델 입력을 바꾸는 선택지(B·D)는 채택하지 않고, C 도
두 변형 모두 기각한다.** 대신 모델 입력과 무관한 보강 둘(§4 의 E1·E2)을 권한다.

근거 셋만 먼저:

1. **고쳐도 "끝남" 을 얻지 못한다.** D 가 모델을 `wait` 로 보내도, 영원히 끝나지 않는
   작업이면 `wait` 경로 역시 끝이 없다. 체크인은 5→10→20→40→60분으로 늘고 60분에서
   **상한 없이 반복**되며(`arm_checkin`, `CHECKIN_STEPS_S[min(step, len-1)]`), 알림 문구는
   "If it is making progress, call `wait` again" 이다(`runtime.CHECKIN_AGAIN`). 그러니 구멍이
   바꾸는 것은 run 의 종료 여부가 아니라 "한 시간마다 모델이 죽일지 판단할 기회" 다. 그
   기회의 값은 모델이 실제로 kill 을 고르는 비율인데, 그 비율은 재지 않았고 35B 가 고를
   가능성은 낮다(체크인 문구가 wait 재호출을 먼저 권한다).
2. **구멍에 빠지는 자연 사례와 정당 사례의 모양이 같다.** 모델이 `answers` 로 전부 닫고
   `complete` 하려면 "과제는 끝났다고 믿는데 작업은 돈다" 는 상태여야 하고, 그 상태의
   대표가 바로 "서버 띄우고 답함"(정당)과 "답을 냈고 남은 작업은 곧 끝남"(부록 A 후반 4건,
   정당)이다. 어떤 되돌림이든 이 둘을 같이 잡는다. 오탐이 가설이 아니라 구조다.
3. **run 은 이 모양을 이미 설계로 인정했다.** `agent-cli run` 의 결과 파일 사슬은 "질의
   런이 자기 백그라운드 작업이 돌고 있는 채로 `complete` 했으면 답을 미룬 것으로 보고 다음
   깨우기 런의 답까지 쓴다"(v10.47.0, `main.py:335–347` 주석과 `_continuing = self._used_jobs()
   or waiting`). 즉 "작업을 둔 `complete`" 는 run 에서 구멍이 아니라 **유한 작업을 위한 정상
   경로**이고, 문제는 그 부분집합인 "영원히 끝나지 않는 작업" 뿐이다. 그 부분집합을 하니스는
   구별할 수 없다.

빈도 0건이 기록 모양 탓에 과소평가됐을 가능성(Q0 둘째 질문)은 인정한다 — 기록은 벤치와 짧은
과제 위주다. 그래도 판정이 바뀌지 않는 이유는 위 1·2 가 빈도와 무관하기 때문이다: 사례가 늘어도
D 는 종료를 주지 못하고 오탐은 같이 늘어난다.

---

## 1. 사실 검증 — 문서 주장 vs 코드

| 문서 주장 | 확인 결과 |
|---|---|
| §1 `session_has_live_work` 가 run 정지·web reap 의 공통 술어, 돌고 있는 작업을 할 일로 침 | **맞다.** `runtime.py:600–630`, `jobs.has_active_work()` 가 한 항. 호출부 `pump.QuietPolicy.should_stop` (`pump.py:117`), `main.web_instance_is_active` (`main.py:1602`) |
| §1 `wake_source_ids` 는 예약을 뺀 id 집합 | **맞다.** `runtime.py:483–488`, `_SCHEDULE_ID` 제외 |
| §1 체크인은 `run_ended(waiting=True)` **하나**에서 건다 | **부분 교정.** `run_ended` (`agents_live.py:1348`) 외에 private 루프의 `_wait_in_place` 가 `reg.arm_checkin(owner)` 를 직접 부른다(`dispatch.py:1301`). 결론엔 영향 없음 — 둘 다 `wait` 경로다 |
| §1 `complete` 는 작업을 죽이지 않고, 세션 끝·`drop_owner` 만 죽인다 | **맞다.** `_op_complete` 에 작업 관련 코드 없음(`dispatch.py:1103–1167`); `teardown_session` → `jobs.stop_all()` (`runtime.py:337`); `release_owner` → `jobs.drop_owner` (`runtime.py:845`) |
| §1 `can_pause_for_human` 은 web 만 True | **맞다.** `render/base.py:227` False, `render/web.py:2209` True |
| §2 표: `wait` → arm, `complete` → `end_run`, 체크인 안 걸림 | **맞다.** `agents_live.py:1348–1356`. 펌프는 `main_run_ended(..., waiting=result.waiting)` 로 넘긴다(`pump.py:261–265`) |
| §2 run 은 작업이 돌면 끝나지 않고 "Waiting on 1 background job(s)…" 만 보임 | **맞다.** `QuietPolicy.should_stop` 이 `on_wait("jobs")` 를 **한 번** 부르고(`pump.py:133–145`), 문구는 `_jobs_wait_notice` (`main.py:1643`) |
| §2 web 은 작업이 돌면 뷰어 0 이어도 영영 꺼지지 않음 | **맞다.** `web_instance_is_active` → `session_has_live_work(jobs=)`; `IdleMonitor.tick` 은 active 면 시계를 리셋한다(`web/idle.py:55`) |
| §2 "프로세스, 포트, **모델 서버 자리**가 계속 잡혀 있다" | **근거 못 찾음.** 유휴 방이 모델 서버와 유지하는 연결·예약은 코드에 없다. 잡히는 것은 프로세스·포트·보드의 인스턴스 슬롯이다. 이 문구는 빼는 게 맞다 |
| §3.1 프로브 결과 | **재현.** `WAIT armed=True`, `COMPLETE armed=False`(wake sources `{'agent:k'}`), `JOB+COMPLETE armed=False`(`{'job:1'}`). 3 passed |
| §3.3 스캔 수치 1,841 / 133 / 23 | **대체로 재현.** 내 실행: files=1,528, with_jobs_or_agents=113, hits=19, never=0. 차이는 문서가 경고한 대로 `<scratch>` 폴더(monitor 재생 4건 포함)가 사라진 것. 벤치 9건은 파일·레코드 번호까지 동일 |
| 부록 A "NpbPRtm `answers: None` 이 독촉에 걸리지 않은 이유는 확인하지 않았다 … 열린 항목이 없었을 가능성" | **틀렸다.** 같은 세션 레코드 26·30·32·34·36·38·40·42 의 `complete` 가 모두 독촉을 받았고(레코드 27~43 이 "your result was delivered, but … `answers` is missing"), 레코드 44 는 **9번째** — `NAG_BUDGET = 8` (`ledger.py:29`) 소진으로 `ledger.spend()` 가 False 를 돌려 통과한 것이다. 이것이 §2 가 "거의 나오지 않는다" 고 한 **둘째 탈출로의 실사례**다. 따라서 13건은 "answers 로 닫음 12 + 예산 소진 1" 이고, 독촉 10건 중 8건이 이 한 세션이다 |
| 부록 B.2 "정리: Ctrl-C, 그리고 남은 빌드는 `kill -- -<pgid>`" | **보통 불필요.** run 의 Ctrl-C 는 `_finalize_run` → `teardown_session` → `stop_all` 로 가고(`main.py:1586–1598`, `runtime.py:337`), `stop_all` 은 프로세스 그룹에 TERM→KILL 을 보낸다(docs/jobs/DESIGN.md 정리 항). 하드 킬된 경우만 둘째 단계가 필요하다 |
| §4 C 바탕: run 은 사람이 보면 "왜 안 끝나는지" 보인다 | **맞다.** 위 `on_wait("jobs")` |
| §2 web: "사람이 화면을 보고 있으면 알아챈다" | **맞지만 빠진 사실이 있다.** 방 안에서는 `job #N started (pid P)` 관찰 카드와 종료 통지 카드로 보인다. 그러나 **`status.json` 에는 작업 항이 없다**(`web/instance_file.py:92–`: busy·awaiting_input·viewers·agents·grammar·auditing·knobs·open_items·stuck). web 정적 파일·서버·렌더러 어디에도 `job` 이라는 낱말이 없다. 보드는 "이 방이 왜 안 꺼지는지" 를 그릴 재료가 없다. 이것이 §4 "사용자가 발견하는 비용" 의 실체이고, 모델과 무관하다(§4 E1) |
| §1 monitor 는 deadline 이 있다 (Q4 전제) | **맞고, 더 강하다.** 상한 24h (`MONITOR_DEADLINE_MAX_S = 86400`, `constants.py:232`), 만료는 `_retire(RETIRE_DEADLINE)` 로 **통지를 남긴다**(`monitor/registry.py:337, 388`). monitor 만 남은 세션은 영원히 멈출 수 없다 |

라이브로만 확인되는 것: §3.2 의 다섯 프롬프트 행동과 "5분 체크인이 오지 않았다" 는 관찰. 코드상
일관되므로(위 표의 `run_ended` 항) 의심할 이유는 없지만 이번에 재현하지는 않았다.

---

## 2. 사실 교정 요약 (문서에 반영할 것)

1. 부록 A 주석: NpbPRtm 레코드 44 는 **독촉 예산 소진**(8번 뒤 9번째)이다. 13건 분류를 "answers 12 +
   예산 소진 1" 로 고치고, §2 의 "2. 독촉 예산 8번을 다 쓴 경우 … 거의 나오지 않고" 에 이 실사례를
   달아야 한다. 또 이 사례가 `wait` 를 만든 바로 그 벤치 실측(`tools/virtual.py` WaitTool 주석 "같은
   complete 를 9번 보내고 예산이 끝나서야")이다.
2. §2: "모델 서버 자리" 삭제(근거 없음).
3. §1: 체크인을 거는 자리는 `run_ended` 와 `_wait_in_place` 둘(둘 다 `wait` 경로).
4. §2 run 증상에 **빠진 사실**: run 의 결과 파일 사슬이 "작업을 둔 `complete`" 를 "답을 미룬 것" 으로
   이미 처리한다(`main.py:335–347`, v10.47.0). 이 사실이 있으면 §2 "run 에서는 정당하지 않다" 는 "run
   에서도 유한 작업이면 정상 경로, 무한 작업만 문제" 로 좁혀진다.
5. §2 web 증상에 빠진 사실: `status.json` 에 작업 항이 없어 보드가 살아 있는 이유를 보이지 못한다.
6. 부록 B.2 정리 안내: Ctrl-C 가 `stop_all` 로 프로세스 그룹을 죽인다.

---

## 3. Q0 근거 상세

### 3.1 어디까지가 "영원" 인가 — 수명 상한 지도

| 깨울 것 | 상한 | 상한에서 일어나는 일 |
|---|---|---|
| 백그라운드 작업 (`job:N`) | **없음** (`jobs.py` — 시간 제한 없음, docs/wait/DESIGN.md §6.1 도 명시) | — |
| monitor (`mon:…`) | 24h (`MONITOR_DEADLINE_MAX_S`) | `RETIRE_DEADLINE` 통지 → 모델을 깨운다 |
| 에이전트 (`agent:k`) | 자기 런의 `max_turns`; 그 안에서 작업을 띄우면 위 첫 줄로 환원 | 회신 폴백(`end_run` 런 요약)이 요청자를 깨운다 |
| 예약 | 집합에서 제외 | — |

그러므로 구멍이 "영원" 이 되는 경로는 **작업 하나**뿐이다(에이전트는 자기 작업을 통해 간접). Q4 의
답은 여기서 바로 나온다: monitor 는 어떤 개입에서도 빼야 한다. 그리고 D 의 대상도 사실상 "작업만
남은 `complete`" 로 좁아진다.

### 3.2 `wait` 경로의 끝이 어디인가

docs/wait/DESIGN.md §6.2 와 `agents_live.arm_checkin` 을 그대로 읽으면: 같은 집합을 계속 기다리면
60분 간격이 **무한히** 반복된다. 상한은 "간격" 에만 있고 "횟수" 에는 없다. 알림(`wait_checkin`) 은
사실 진술과 두 갈래(다시 `wait` / 멈추고 접근을 바꿈)를 준다. 영원한 작업을 든 모델이 kill 을 고르는
비율은 측정된 적이 없다. 그러니 D 의 기대 이득은 "한 시간마다 kill 을 고를 확률" 이고, 그걸 모르는
채로는 D 의 값을 매길 수 없다. 측정하려면 "영원한 작업 + 체크인" 재생을 먼저 해야 하는데, 그건
이 문서의 측정 계획(§6)에 없다.

### 3.3 왜 자연 사례가 안 나오는가

모델에게 "complete 로 기다리지 말라" 는 이미 세 곳에서 말한다: 시스템 프롬프트 Task Guidelines
(`prompts/system_prompt.py:115` "do not use `complete` to wait"), `complete` 도구 설명
(`tools/virtual.py:43–45` "Not for waiting — … call `wait`"), 장부 꼬리(`loop/ledger.py:40–44`
"never `complete` to wait"). §3.2 의 자연 프롬프트 4개가 전부 `wait` 로 간 것은 이 셋이 작동한다는
증거다. 구멍에 들어가려면 모델이 **기다리는 게 아니어야** 하고, 기다리는 게 아닌데 작업이 도는
상태는 §0-2 에서 말한 정당 사례와 같은 모양이다.

### 3.4 기록 모양에 따른 과소평가

인정한다. 다만 긴 세션·압축 직후·다른 모델에서 늘어날 사례도 같은 모양(정당 사례와 동형)일
것이라는 점에서 판정은 바뀌지 않는다. 늘어나는 것이 있다면 "압축 뒤 작업을 잊고 `complete`" 인데,
그 경우 작업은 유한할 확률이 높고(기억을 잃은 것뿐), 유한하면 종료 통지가 깨운다 — 기록의 13건이
전부 그렇게 끝났다.

---

## 4. 선택지 판정

| 안 | 판정 | 이유 |
|---|---|---|
| A 아무것도 안 함 + 문서화 | **채택** | 위 §0·§3 |
| B 되돌리지 않는 사실 한 줄 | **보류** | 성공한 `complete` 에는 모델에게 가는 관찰이 없다(런이 끝난다). 한 줄이 들어갈 자리는 사용자 답 꼬리(`_with_unanswered_notice` 자리)인데 그 답은 `_terminal_record` 로 history 에 들어가 **다음 런의 모델 입력**이 된다 → 측정 대상. 얻는 것은 적다: run 콘솔엔 같은 사실(`_jobs_wait_notice`)이 이미 있고, web 방 안에는 작업 시작 관찰 카드가 있다. 없는 것은 보드 쪽 표시이고 그건 E1 이 모델 입력 없이 준다 |
| C(b) run 에서 일정 시간 뒤 작업 정리·종료 | **기각** | 유한 작업과 무한 작업을 하니스가 구별할 수 없다. 3시간 빌드는 run 의 정상 경로다(§0-3, 결과 파일 사슬이 깨우기 런의 답을 기다린다). 정리하면 그 설계를 깬다. "N시간" 은 제품 결정이고 측정으로 정할 수 없다 |
| C(c) `complete` 에도 체크인을 건다 | **기각** | 알림 본문이 "since your wait" 를 전제한다(`JobRegistry.log_growth` 기준점이 `wait` 시점 로그 크기). `complete` 뒤 걸면 그 문구가 거짓이 되어 사실 진술 원칙을 깨고, 고치면 문구 변경 → 측정. 게다가 §3.2 대로 체크인은 영원한 작업을 끝내지 못한다 |
| D 런당 한 번 되돌림 + 표면 분기 + 확인 집합 기억 | **기각** | 오탐이 구조적(§0-2), 상태(`확인받은 집합`)와 표면 분기가 늘고, 측정에 합성 사례가 필요하며(§6 ①), 얻는 것은 "kill 을 고를 기회" 뿐 |

### 제안 E — 모델 입력 없이 하는 보강 둘

**E1. `status.json` 에 작업 항을 넣고 보드가 그린다.** `write_status_file(..., jobs=)` 에
`{"count", "list": [{id, pid, elapsed, command(축약)}]}` 를 additive 로 더하고(기존 `agents`·`open_items`
와 같은 모양), 보드 카드에 🧵 칩. 이것으로 "뷰어 0 인데 안 꺼지는 방" 의 이유가 보드에 보인다 —
§4 "사용자가 발견하는 비용" 의 핵심을 모델 문구 없이 닫는다. 보드는 그리기만 한다는 원칙
(board-is-a-mirror)에 맞다. 측정 불필요(단위 테스트만). 보드 변경을 동반하므로 짝 버전.

**E2. 알려진 한계로 문서화.** docs/jobs/DESIGN.md "수명" 항과 docs/wait/DESIGN.md §6 에 한 줄씩:
"`complete` 로 끝난 런에는 체크인을 걸지 않는다(의도). 끝나지 않는 작업을 둔 채 `complete` 하면
run 은 종료 통지까지 기다리고 web 방은 reap 되지 않는다. 하니스는 유한·무한 작업을 구별하지
않는다." 그리고 이 문서(DESIGN.md)의 §2 교정(§2 목록).

E1 은 Q3b 와도 이어진다: 보이게 만든 뒤에야 "reap 이 죽이는 게 맞나" 를 사용자가 실제 방을 보고
결정할 수 있다.

---

## 5. Q1~Q6 (D 를 안 하더라도 기록으로 남긴다)

- **Q1 개입 위치.** 관찰로 되돌리려면 `_op_complete` 안(장부 정산 직후, `dispatch.py:1155` 근처)이
  유일하다 — `run_ended` 시점엔 결과가 이미 렌더·기록돼 되돌릴 수 없다. 체크인만 걸 거면
  `run_ended` 가 맞다(펌프·상주가 한 곳). 그러나 둘 다 권하지 않는다(§4).
- **Q2 run 의 두 번째 `complete`.** (a) 받아들이고 둔다. (b) 는 v10.47.0 결과 파일 사슬과 충돌, (c) 는
  문구 변경. 지금 코드가 (a) 다.
- **Q3 확인받은 집합 기억.** 관대함이 아니라 **상태가 하나 더 생기는 것**이 문제다(`_checkin_last` 와
  별개 dict, 세션 재시작·resume 때 복원 여부, 상주 에이전트별). 안 한다.
- **Q3b reap 과 작업.** 지금 코드의 답은 이미 명확하다: reap → `teardown_session` → `stop_all` 이
  **죽인다**. 작업이 reap 을 막는 것은 유한 작업의 종료 통지를 잃지 않기 위한 v10.47.0 결정이고,
  영원한 작업은 그 결정의 비용이다. 바꾸려면 "작업은 reap 을 N 시간까지만 막는다" 같은 수명 정책이
  필요한데 이는 제품 결정이고 측정으로 정할 수 없다. 순서는 E1(보이게) → 실제 방을 보고 결정.
- **Q4 monitor.** 뺀다. 24h 상한 + 만료 통지라 영원 정지가 없다(§3.1). 에이전트도 `max_turns`.
- **Q5 예산.** 했다면 별개가 맞다(장부 예산은 "열린 항목" 회계, 이것은 "작업" 회계). D 를 안 하니
  무의미.
- **Q6 하니스 밖 프로세스.** shell 결과에 `docker run -d` 감지 한 줄은 권하지 않는다 — 문구 추가라
  측정 대상이고, 패턴 목록은 끝이 없다(`nohup`, `setsid`, `&`, `systemd-run`, `tmux new -d`, 스크립트 안
  `Popen`). 이미 있는 구조가 더 낫다: 하니스 작업은 `start_new_session=True` 로 띄워(`jobs.py:200`)
  `kill -- -<pgid>` 가 `&` 자식까지 잡는다. 안내는 "긴 것은 `background` true 로" 한 줄로 충분하고 그
  문장은 이미 있다(`system_prompt.py:115`).

---

## 6. 재현 기록

- 프로브: `tests/_tmp_probe_hole.py` 로 복사해 실행 뒤 삭제. 프로젝트 `.venv` 에 pytest 가 없어
  스크래치에만 pytest 9.2.0 을 설치해 `PYTHONPATH` 로 붙였다(`.venv`·소스 불변, `git status` 는
  `docs/complete-while-running/`·`uv.lock` untracked 만). 출력:
  ```
  WAIT  : armed = True
  COMPLETE: wake sources = {'agent:k'} / armed = False
  JOB+COMPLETE: wake sources = {'job:1'} / armed = False
  3 passed
  ```
- 스캔: `find ~ -path ~/Library -prune -o -name history.jsonl -path "*sessions*" -print` → 1,528 파일.
  `scan_history.py` → `files=1528 with_jobs_or_agents=113 hits=19`, `never=0`. 19건 = 벤치 9건(문서와
  동일) + 독촉 10건(NpbPRtm 레코드 26·30·32·34·36·38·40·42 의 8건 + N6LGsW9 레코드 52·64). 문서의
  monitor 4건은 사라진 `<scratch>` 에 있어 이번 스캔엔 없다.
- 라이브(B.2)는 돌리지 않았다.

---

# 후속 리뷰 — E1 확장안: 방 UI 에서 사람이 작업을 멈춘다 (2026-11-10)

제안(팀 리드): ① `status.json` 에 `jobs` 항(additive)·SSE 로 돌고 있는 작업을 내보내고 방 UI 에
🧵 칩 + ■ 버튼 → `POST /api/jobs/stop {id}` → `JobRegistry.kill` ② 보드는 status.json 만 그린다 ③
사람이 멈추면 소유자에게 보통 종료 보고와 같은 모양의 작업 메일(사실 한 줄, `success=False`)을
보낸다. 코드로 검증한 결과를 적는다.

## 판정: **go-with-changes**

방향은 맞다. 특히 ③ 은 필수다 — 지금 `kill()` 은 `_finish(..., notify=False)` 로 끝나므로
(`jobs.py:314`) 사람이 멈춰도 소유자는 모르고, `wait` 중인 모델은 체크인(60분 반복)만 받는다.
그런데 모델이 자기 `kill -- -<pid>` 로 죽인 작업은 `tick` 이 보고 `_finish(notify=True)` 로
"finished rc=-15" 통지를 **보낸다**(`jobs.py:363–368`). 즉 "하니스가 죽인 것은 조용히, 그 밖은
통지" 가 현재 규칙이고, 사람의 멈춤은 후자에 속해야 한다. 아래 변경 7개 중 1·2·3 은 블로커다.

## 요구 변경

1. **엔드포인트는 동기 `def` 로(또는 스레드로 넘긴다).** `kill()` 은 `_await` 가 `time.sleep(0.05)`
   루프로 최대 `KILL_GRACE_S` × 2 = 6초를 **막는다**(`jobs.py:305–324`). `/api/stop` 처럼 `async def`
   로 쓰면 uvicorn 이벤트 루프가 서고 SSE 가 6초 멈춘다.
2. **`JobRegistry` 에 변화 관찰자가 필요하다.** 작업의 시작·종료는 지금 sticky 를 거치지 않는다.
   status.json 은 `_STATUS_STICKY_KEYS`(`render/web.py:88`)의 sticky 가 바뀔 때만 다시 쓰이므로
   (`set_sticky` → `_flips_status`), 작업이 유휴 중에 끝나거나 사람이 멈춰도 파일·SSE 가 갱신되지
   않는다. `deliver` 와 같은 자리에 `on_change` 콜러블 하나를 두고(`wire_job_delivery`,
   `runtime.py:95`), `start`·`_finish` 끝에서 부르게 한다 → 렌더러가 `set_sticky("jobs", …)`,
   `"jobs"` 를 `_STATUS_STICKY_KEYS` 에 넣는다. 그러면 status.json·SSE·늦은 접속 재생이 한 경로다.
3. **`elapsed` 문자열이 아니라 `started_at`(epoch) 을 내보낸다.** status.json 은 정적 파일이라
   "12m" 은 쓰는 순간부터 낡는다. 보드·방 UI 가 시각에서 계산한다. 항목: `{id, pid, owner,
   command(축약 `_short`), started_at}` + `count`. `/api/health` 가 `open_items_summary` 를 싣듯
   `jobs` 요약도 같은 자리에 두면 보드가 둘 중 어느 쪽을 읽어도 같다.
4. **새 메서드가 아니라 `kill(job, *, by: str | None = None)`.** `by` 가 있으면 `_finish(notify=True,
   why=…)`; `drop_owner`·`stop_all` 은 `by=None` 그대로(조용히). 로그 끝 줄과 보고 머리줄이 같은
   사실을 들고, **rc 도 함께** 적는다(아래 (c) 경합 때문).
5. **누가 멈췄는지 이름을 적는다.** `/api/input` 처럼 body 에 `conn_id` 를 받아
   `hub.nickname_for(conn_id)` 로 푼다(`server.py:313`). 모르면 "a viewer". 모델에게 "user" 라는
   추상명사보다 사람 이름이 낫다 — 그 사람이 방에 있고 모델이 되물을 수 있다.
6. 모르는 id·이미 끝난 작업은 404(`_schedule_or_404` 패턴, `server.py:943`). 응답은 갱신된 작업 뷰.
   인증은 `/api/*` 공통 규칙이 이미 덮는다(`server.py:16`).
7. **문구는 측정한다**(아래 (e)). 다만 작게.

## (a) 블로커·더 단순한 모양

블로커는 위 1·2·3. 더 단순한 모양으로 "TERM 만 보내고 `tick` 의 보통 통지(finished rc=-15)에
맡긴다" 를 생각했다 — 새 문구 0 이다. **권하지 않는다.** 모델에게 rc=-15 는 "죽었다" 이고, 다음
행동은 **같은 명령을 다시 띄우는 것**이다. 사람이 멈춘 서버를 모델이 되살리면 버튼의 뜻이
뒤집힌다. "누가 멈췄다" 는 사실이 있어야 모델이 되묻거나 그대로 둔다. 그래서 ③ 이 필수고, 그
문장은 새 문장이라 측정 대상이다.

## (b) 소유자 라우팅

`registry.deliver(addr)` 가 셋을 이미 가른다(`agents_live.py:2089–2134`): `main` → `_push_reply`
→ `MailWaker`, `agent:<key>` → `request(kind="job")` 로 받은편지함(한 항목 = 한 런이라 유휴
에이전트도 깬다), private(`run:`·`harness:`) → `_push_private` → 그 루프의 `wait`/턴 경계 흡수. 관찰
레코드는 세 경로가 모두 `harness_record` 하나로 만든다(`runtime.py:770–787`, `tool="job"`). 새
배선은 없다. 주의 둘: (1) 소유자가 에이전트면 **main 은 모른다** — 그게 맞다(소유자 의미론), 방
UI 에는 에이전트 카드 쪽 관찰로 보인다. (2) 죽은 소유자의 작업은 `release_owner` 가 이미 죽였으므로
"소유자 없는 작업" 은 없다(`runtime.py:845`).

## (c) 경합

- **자연 종료와 겹침.** `_finish` 가 락 안에서 `if not job.running: return` 으로 먼저 온 쪽만 남긴다
  (`jobs.py:371–373`). `tick`(1초 폴링)이 이기면 "finished rc=N", 사람이 이기면 "stopped by …".
  둘 다 참이고 통지는 정확히 하나다. 단, TERM 이 닿기 전에 프로세스가 스스로 끝났는데 사람의
  `_finish` 가 이기는 창(≤ `POLL_SECS`)이 있다 — 그때 rc 는 0 일 수 있다. 그래서 변경 4 의 "rc 도
  함께": "stopped by alice · rc=0" 은 거짓이 아니다. `killpg` 의 `ProcessLookupError` 는 이미
  삼킨다(`jobs.py:78–82`).
- **턴 도중 vs 유휴.** 턴 도중이면 `_push_reply` 가 쌓이고 다음 턴 경계의 `intake` 가 관찰로 넣는다
  (오늘 작업 종료 통지와 같은 길). 유휴면 `MailWaker.on_mail` 이 `WAKE_TEXT` 를 큐에 넣어 펌프가
  런을 연다(`agents_live.py:3032`). `wait` 로 끝난 상태도 유휴와 같고, 런 시작이 `run_started` 로
  체크인을 푼다. 전부 기존 경로다.
- **reap 과의 경합.** 작업이 사라지는 순간 `has_active_work` 는 `_inflight` 로 배달 창을 덮고
  (`jobs.py:234–238`), 배달되면 큐 `pending_count` 가 살아 있고, 런이 열리면 `worker_is_busy` 다.
  빈틈은 없다. 그리고 깨우기 런이 끝나면 방은 정상적으로 reap 될 수 있다 — **이것이 이 확장의 진짜
  이득**이다(1차 리뷰 Q3b: 사람이 보고 결정한 뒤 방이 죽을 수 있게 된다).

## (d) run(CLI)

주지 않는다. 화면이 없고, Ctrl-C 가 `_finalize_run` → `teardown_session` → `stop_all` 로 전부
죽인다. status.json 도 run 은 쓰지 않는다. 다만 변경 4 의 `kill(by=)` 는 레지스트리 메서드라
표면 무관하게 들어가고, run 에서는 아무도 `by` 를 넣지 않는다.

## (e) 문구와 측정

면제가 아니다 — 모델이 본 적 없는 새 사실("사람이 멈췄다")이고, 그 사실에 대한 해로운 반응(같은
명령을 다시 띄움)이 구체적이라 재기 쉽다. 모양은 기존 보고의 머리줄에서 낱말 하나만 바꾼다:

```
🧵 job #3 stopped by alice after 12m · rc=-15 — `bash build.sh`
  log: .agent-cli/tmp/jobs/3.log (last 8 line(s))
  …
  [job #3 stopped by alice after 12m]
```

`success=False` 는 맞다(`harness_record` 가 `success` 를 레코드에 싣는다). "do not restart it" 같은
지시는 넣지 않는다 — 사실만.

측정: 세션 재생(`--resume`, 첫 호출을 녹화로 대체) 2사례 × 2모델(35B·GLM) × 10회 = 40런.
사례 ① `wait` 중 멈춤 메일 도착, 사례 ② `complete` 뒤 유휴 중 멈춤 메일로 깨어남. 해로운 행동 =
같은 명령(또는 동등한 명령)을 되살림. 기대 = 그대로 두고 사실을 답하거나 되묻는다. 40런 중 되살림이
35B 에서 2/20 을 넘으면 문장을 바꾸는 게 아니라 **멈춘 사람의 이름이 있으니** 그 사람에게 묻는
경로가 작동하는지 먼저 본다(agent-ask 가 아니라 보통 답으로).

**측정 결과 (2026-11-10): MEASURE.md.** base 문구는 35B 에서 서버를 10/10 되살렸다(rc=-15 를 하니스의 kill 로 읽음). `stopped by user <name>` 으로 바꾼 뒤 35B 1/20·GLM 0/20 — 채택.

## 1차 리뷰와의 관계

1차 리뷰의 E1 은 "보이게" 였고 이 확장은 "멈출 수 있게" 다. 둘을 합치면 1차 리뷰가 고치지 않기로
한 구멍(끝나지 않는 작업을 둔 `complete`)의 실제 비용 — 사람 없는 방이 영원히 산다 — 을 모델 입력
변경 없이(③ 의 한 문장만 측정) 사람의 손에 넘긴다. D 보다 싸고 오탐이 없다.
