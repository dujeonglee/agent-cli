# 맡긴 일이 돌고 있는데 `complete` 로 끝내는 구멍 — 검토 요청서

상태: **고칠지 말지 미정** (2026-11-09). 설계안은 있지만 구현은 시작하지 않았다.
기준 코드: agent-cli main `efbe073b` (v10.120.2). 저장소 루트: `agent-cli/`.

---

## 0. 리뷰어에게

이 문서는 사전 지식 없이 읽을 수 있게 썼다. §1 에 필요한 개념을 모아 두었고, 코드
위치는 줄 번호와 함께 적었다(§8). 주장마다 근거와 재현 방법을 붙였다(§3, 부록 B).

**가장 먼저 답해 줬으면 하는 질문은 "이걸 고칠 가치가 있나"(Q0)다.** 설계(§5)는 그
다음 이야기다. 지금 근거는 이렇다: 구멍은 코드상 확실히 있고 강제로 재현된다. 하지만
`wait` 가 있는 빌드에서 실제로 해를 끼친 기록은 0건이고, 자연스러운 프롬프트로도
재현되지 않았다. 반대로, 고치면 정당한 `complete` 를 되돌리는 오탐이 생기는데, 그 실제
예는 기록에 있다.

바라는 산출물:
1. Q0 판정과 근거: 고친다 / 고치지 않는다 / 조건부(예: run 만, 사실 한 줄만).
2. 고친다면 §4 의 선택지 중 어느 것인지, 그리고 Q1~Q6 에 대한 답.
3. 이 문서의 사실 주장 중 코드와 다른 것이 있으면 지적.

이 프로젝트의 규칙 중 판정에 영향을 주는 것:
- **모델 입력 문구 변경은 측정한 뒤에 채택한다.** 관찰·도구 설명·꼬리 문장이 모두
  해당한다. 측정은 녹화 세션 재생이나 A/B 로 한다. 기준 모델은 가장 약한
  Qwen3.6-35B-A3B 와 가장 강한 GLM-5.3-Flash 둘이다.
- **하니스가 모델에게 하는 말은 사실 진술이다.** "nothing has reported yet" 처럼 상태에
  따라 거짓이 될 수 있는 부재 진술은 쓰지 않는다(`docs/wait/DESIGN.md` §6.3).
- **결과는 언제나 먼저 배달한다.** 되돌리더라도 사용자는 답을 먼저 받는다
  (`docs/ledger/DESIGN.md`).
- **기술 부채 금지.** 임시 해결책이나 불필요한 추상화는 만들지 않는다(`CLAUDE.md`).
- **사람의 시간이 가장 중요한 자원이다.** 사용자가 멈춘 방을 발견하는 비용, 그리고
  필요 없는 되돌림에 사람이 기다리는 비용이 둘 다 여기에 해당한다.

---

## 1. 배경 개념 (이 문서에 필요한 만큼)

**표면 두 가지**
- `agent-cli run "<질문>"`: 한 번 실행하는 헤드리스 표면이다. 할 일이 다 조용해지면
  프로세스가 끝난다(`pump.QuietPolicy`, `pump.py:84`).
- `agent-cli web`: 브라우저 채팅방이다. 보는 사람이 0명인 상태가 `--idle-timeout` 만큼
  이어지면 스스로 꺼진다(self-reap). agent-board 는 글마다 이 방을 하나씩 띄우고, 이때
  `--idle-timeout 300` 을 넘긴다.
- 두 표면 모두 "아직 할 일이 있나"는 같은 함수로 판정한다:
  `runtime.session_has_live_work` (`runtime.py:600`). 큐, monitor, 예약, 하니스 작업, 그리고
  **돌고 있는 백그라운드 작업**이 할 일로 친다.

**런과 끝내는 도구**
- 런은 입력 하나(사용자 요청이나 깨우는 메일)에 대해 모델이 여러 턴 도구를 부르는
  단위다. 런은 끝내는 도구로 닫힌다.
- `complete`(`result`, `answers`): "끝냈다"는 뜻이다. `result` 가 사용자에게 배달된다.
  `answers` 에는 이 답으로 닫는 열린 항목의 id 를 적는다.
- `wait`(`reason`): "닫지 않고 기다린다"는 뜻이다(v10.65.0, `docs/wait/DESIGN.md`).
  깨울 것이 있을 때만 받아들이고, 열린 항목은 다음 런으로 넘긴다.

**열린 항목 장부** (`docs/ledger/DESIGN.md`)
- 항목 id 의 접두는 넷이다: `u-`(사람 요청), `n-`(메일·통지), `r-`(에이전트에게 받을
  회신), `q-`(사람에게 한 질문).
- `complete` 할 때 열린 항목이 남아 있으면, 결과는 배달하되 관찰로 되돌린다("독촉").
  독촉 예산은 런마다 8번이다. 이때 깨울 것이 있으면 "call `wait` instead" 힌트가
  붙는다(`loop/dispatch.py:1355` 근처).

**깨울 것(wake source)**
- 하니스가 소유한 작업만 해당한다: shell `background: true` 작업, monitor, 내 요청을 처리
  중인 에이전트, 예약.
- `runtime.wake_source_ids(owner)` (`runtime.py:483`) 는 이 중 예약을 뺀 id 집합이다.
- 작업이 끝나면 하니스가 주인에게 "🧵 job #N finished rc=… after …" 메일을 보내 모델을
  깨운다. 에이전트의 회신도 같은 방식으로 깨운다.

**중간 깨우기 (check-in)** (`docs/wait/DESIGN.md` §6)
- `wait` 로 끝난 런에만 걸린다. 깨울 것이 한참 보고하지 않으면 하니스가 5, 10, 20, 40,
  60분 간격으로 "아직 돌고 있다"는 알림을 보내 모델을 깨운다(`CHECKIN_STEPS_S`).
- 거는 자리는 `AgentRegistry.run_ended(waiting=True)` 하나다
  (`subagent/agents_live.py:1321`). 같은 대상 집합을 다시 기다리면 간격을 늘리고, 그
  판정에 `_checkin_last` 를 쓴다.
- **`complete` 로 끝난 런에는 걸리지 않는다.** 이 문서가 다루는 구멍이 바로 이 점이다.

**그 밖의 사실**
- `complete` 는 작업을 죽이지 않는다. 작업은 세션이 끝나거나 소유자를 정리할 때
  (`drop_owner`)만 죽는다.
- `renderer.can_pause_for_human()` 은 web 에서만 True 다(`render/base.py:227`). 표면을
  구분하는 데 쓸 수 있다.

---

## 2. 문제

| 끝내는 법 | 런 끝 처리 | 중간 깨우기 |
|---|---|---|
| `wait` | 빚을 유지하고 `arm_checkin` | **걸림** |
| `complete` | `end_run`(남은 빚을 런 요약으로 정산) | **안 걸림** |

맡긴 일이 끝나면 종료 통지가 모델을 깨운다. 그래서 `complete` 로 끝내도 대개는 문제가
없다. 문제는 **그 일이 영영 끝나지 않을 때**다(멈춘 설치, 무한 루프, 서버). 그러면
아무도 모델을 다시 깨우지 않는다.

장부가 이미 막는 경우도 있다. 열린 `u-` 가 있는데 `complete` 하면 독촉으로 되돌리고,
`wait` 를 쓰라는 힌트까지 붙는다. 그래도 빠져나가는 길이 둘 있다.

1. 모델이 `answers` 로 항목을 **전부 닫았다고 주장**하고 `complete` 하는 경우. 장부는
   비고, 작업은 계속 돈다. **이 문서가 다루는 경우다.**
2. 독촉 예산 8번을 다 쓴 경우. 사용자가 그대로 두기로 결정했다(거의 나오지 않고,
   달리 방법도 없다).

증상은 표면마다 다르다.
- **run**: `QuietPolicy` 가 돌고 있는 작업을 할 일로 보기 때문에 프로세스가 끝나지
  않는다. 끝나지 않는 작업이면 **무한 정지**다. 터미널에는 "Waiting on 1 background
  job(s)…"만 보인다.
- **web**: 모델이 다시 깨지 않는 것은 run 과 같다. 사람이 화면을 보고 있으면 알아채지만,
  보드 방이나 예약 런처럼 아무도 보지 않는 방에서는 run 과 같다. 수명 문제도 있다.
  self-reap 판정이 작업을 할 일로 치기 때문에, 끝나지 않는 작업을 든 방은 보는 사람이
  0명이어도 **영영 꺼지지 않는다**. 보드의 `--idle-timeout 300` 이 소용없어지고
  프로세스, 포트, 모델 서버 자리가 계속 잡혀 있다.
- **정당한 경우도 있다.** 개발 서버를 띄우고 "띄웠습니다"라고 답하는 것처럼 계속 도는
  것이 목적인 작업이다. 이런 작업은 web 에서는 정당하다. run 에서는 프로세스가 끝나지
  않으므로 정당하지 않다.
- **하니스가 막을 수 없는 것도 있다.** 하니스 밖에서 살아남는 프로세스다:
  `docker run -d`, `systemctl`, `tmux`, 스크립트 안에서 `Popen` 으로 분리된 자식.
  하니스가 소유하지 않아서 `wake_source_ids` 에 들어가지 않는다. 원래 있던 구멍이고
  이 문서의 범위 밖이다.

---

## 3. 증거 (2026-11-09, v10.120.2)

### 3.1 단위 프로브: 구멍이 구조적으로 있다

`tools/probe_hole.py` (부록 B.1). 결과는 다음과 같다.

```
WAIT  : armed = True
COMPLETE: armed = False          # 에이전트가 busy 인데도
JOB+COMPLETE: armed = False      # 작업이 돌고 있는데도
```

### 3.2 라이브 재현: Qwen3.6-35B-A3B, `agent-cli run`

빌드 스크립트는 `tools/build.sh` 를 썼다. 30초짜리 모듈 400개, 약 3.3시간이라 사실상
끝나지 않는다.

| # | 프롬프트 | 모델 행동 |
|---|---|---|
| 1 | "build.sh 로 전체 빌드를 돌려 주고, 빌드가 성공했는지 결과를 알려 줘." | 전경 120초 타임아웃 → 백그라운드로 다시 실행 → `wait` |
| 2 | "…백그라운드로 돌려 두고, 그동안 main.c 함수 개수를 세서 알려 줘. 빌드 결과도." | 백그라운드 실행 → `wait` (함수 개수도 따로 답하지 않음) |
| 3 | "…나 지금 급해서 오래 못 기다리니까 최대한 빨리 답해 줘." | 백그라운드 실행 → `wait` |
| 4 | "에이전트 하나에게 빌드를 맡기고, 너는 main.c 함수 개수를…" | 에이전트 spawn + 자기도 빌드 하나 더 → `wait` |
| 5 | "…wait 는 쓰지 말고, 빌드를 띄우면 바로 complete 로 끝내." | `answers [u-1]` 로 `complete` → **재현** |

5번에서 run 은 "Waiting on 1 background job" 상태로 6분 넘게 멈춰 있었다. 3턴 뒤로는
턴이 하나도 없었고 5분 중간 깨우기도 오지 않았다. 다만 5번은 사용자가 시킨
`complete` 라서 그 자체로는 정답이다. 구멍이 어떤 결과를 내는지는 보여 주지만,
고친 뒤 효과를 재는 데는 쓸 수 없다.

### 3.3 실제 기록 스캔

`tools/scan_history.py` (부록 B.3) 로 이 머신의 세션 기록 1,841개를 봤다. 보드 방, harbor
벤치, 재생 실험이 들어 있다.

- 작업이나 에이전트를 쓴 세션은 133개다.
- `complete` 시점에 작업이 돌고 있던 경우는 23건이다.
  - 10건은 `answers` 를 빠뜨려서 장부 독촉이 잡았다. 기존 경로가 처리한 것이다.
  - **13건은 `answers` 로 다 닫고 빠져나갔다.** 목록은 부록 A 에 있다.
    - **9건(벤치)은 `wait` 가 없던 빌드다.** v10.58.1, v10.64.1, 그리고
      `pytool-x3-off-v10.65.0` 이다. 마지막 것은 python 도구 브랜치의 번호라 `wait` 가
      없다(나중에 v10.66.0 으로 rebase). 세 trajectory 어디에도 `wait` 언급이 없다.
      모델의 result 는 전부 "I'm waiting for job #N…"이다. 기다릴 도구가 없어서
      `complete` 로 기다린 것이다. 이 벤치 과제가 바로 `wait` 를 만든 이유였다
      (`docs/wait/DESIGN.md` §1). **지금 구멍의 증거가 아니다.**
    - **4건(monitor 재생 실험)은 정당한 `complete` 다.** 과제("ERROR 줄이 나오면 알려
      줘")에 실제로 답했고, 남은 `writer.sh` 는 1분 30초 뒤 저절로 끝났다. 수정안이 이런
      `complete` 를 되돌리면 **오탐**이다.
  - 13건 모두 작업이 끝나 종료 통지가 모델을 깨웠다. 무한 정지로 이어진 사례는 0건이다.
  - 에이전트에게 맡긴 일이 도는 중에 빠져나간 경우는 0건이다.
- 스캐너는 정규식 근사다. 작업 시작과 종료 통지, `kill` 셸 호출, `r-` 요청과 `[r-N]`
  회신으로 판정한다. 놓친 경우가 있을 수 있다.

### 3.4 요약

| 질문 | 답 |
|---|---|
| 구멍이 있나 | 있다 (프로브, 강제 재현) |
| `wait` 가 있는 빌드에서 실제로 빠졌나 | 기록상 0건, 자연 프롬프트 0/4 |
| 빠졌을 때 해가 있었나 | 기록상 0건 (작업이 모두 끝났다) |
| 고치면 오탐이 생기나 | 생긴다. 실제 예 4건 (부록 A 후반) |
| 고친 뒤 측정에 쓸 실사례가 있나 | 없다. 합성 사례가 필요하다 |

---

## 4. 고칠 가치 판단 재료

**고치지 않을 때의 비용**
- 드물지만 한 번 걸리면 비싸다. run 은 끝나지 않고(CI·스크립트 안에서 영원히 멈춘다),
  사람 없는 web 방은 영원히 살아 있다. 사용자가 이걸 발견하는 데 걸리는 시간이 곧
  비용이다.
- 빈도 근거는 없다. 지금까지 0건이다. 다만 기록 대부분이 짧은 과제이고, 긴 세션이나
  압축 직후, 다른 모델에서의 빈도는 재지 않았다.

**고칠 때의 비용**
- **오탐**: 정당한 `complete` 를 되돌린다(§3.3 의 4건). 모델 호출이 한 번 더 들고,
  약한 모델은 되돌림 문구를 오해해 `wait` 로 가서 정말로 잠들 수 있다(§6 사례 ②).
- **모델 입력 문구가 바뀐다.** 측정 비용이 든다(§6, 대략 3사례 × 2모델 × 10회).
- **복잡성**: 표면별 분기, "확인받은 집합" 상태, 상주 에이전트 경로.

**선택지** (리뷰어가 다른 안을 내도 좋다)

| 안 | 내용 | 모델 입력 변화 | 오탐 |
|---|---|---|---|
| A | 아무것도 하지 않는다. 문서에 알려진 한계로 적는다 | 없음 | 없음 |
| B | **되돌리지 않는 사실 한 줄.** `complete` 결과 관찰 끝에 "Still running: job #1 (`bash build.sh`) — it keeps running after this answer." 같은 사실만 붙인다. 행동 지시는 하지 않는다 | 작음 | 없음 (호출 낭비 없음, 다만 모델이 다음 런에 반응할 수는 있음) |
| C | **run 만 하니스가 처리한다.** 모델에게는 말하지 않는다. "열린 항목 0 + 작업만 남음"일 때 일정 시간 뒤 작업을 정리하고 끝내거나, 중간 깨우기를 `complete` 에도 건다 | 없음 (깨우기를 걸면 알림 문구는 기존 것) | 없음 |
| D | §5 설계안. 런당 한 번 되돌리고, web 은 재-`complete` 를 허용하고, run 은 `wait` 또는 멈춘 뒤 `complete` | 큼 | 있음 |

C 의 바탕: `QuietPolicy` 는 이미 남은 것이 작업뿐이면 사용자에게 한 번 알린다
(`on_wait("jobs")`, `pump.py` 133행 근처). 터미널의 "Waiting on N background job(s) …
Ctrl-C quits." 가 그것이다. 그러니 run 에서 사람이 보고 있으면 지금도 "왜 안 끝나는지"는
보인다. 부족한 것은 사람이 없는 run(스크립트·CI)뿐이다. B 와 C 는 함께 쓸 수도 있다.

---

## 5. 설계안 D (2026-11-08 사용자와 합의한 초안)

### 5.1 조건

`complete` 가 다음을 모두 만족하면 개입한다.
- 장부를 정산한 뒤 열린 항목이 없다. 열린 항목이 있으면 기존 독촉 경로가 처리하므로
  겹치지 않게 한다.
- `wake_source_ids(owner)` 가 비어 있지 않다.
- 이 루프가 `wait` 를 광고한다. 스킬 인라인 루프처럼 깨울 길이 없는 루프는 제외한다.

### 5.2 동작: 런당 한 번 되돌림

- 결과는 먼저 배달한다.
- 관찰로 되돌린다. 문장은 사실 진술에 표면별 다음 행동을 붙인다.
  - 공통: "Still running: <작업·monitor·에이전트 목록>. If you are waiting for it to
    report, call `wait` — you are woken when it reports or checked in on."
  - **web**: "If it is meant to keep running after this answer, call `complete` again."
    두 번째 `complete` 는 받아들이고 중간 깨우기는 걸지 않는다. 확인받은 id 집합을
    (`_checkin_last` 처럼) 기억해 두고, 같은 집합이면 다음 런부터 다시 묻지 않는다.
  - **run**: 다시 `complete` 하는 길은 없다. "`wait`, or stop it (`kill -- -<pid>` / monitor
    delete / agent kill) and then `complete`."
- 상주 에이전트에도 같은 규칙을 적용한다. 런 끝 처리가 한 곳이므로 자연히 공유되게
  한다.

---

## 6. 측정 계획 (D 나 B 를 고른다면)

녹화 세션을 `complete` 직전까지 잘라 `--resume` 하고, 첫 모델 호출을 녹화로 바꾸는
재생 driver 를 쓴다. 기존 거부 문구 재생과 같은 방식이다(`docs/wait`·
`stale-loop-nudge` 재생 방법론). 35B·GLM × 사례당 10회.

| 사례 | 상황 | 기대 행동 | 해로운 행동 | 실사례 |
|---|---|---|---|---|
| ① | 일을 맡겨 놓고 `answers` 로 전부 닫고 `complete` | `wait` | 다시 `complete` | **없음, 합성 필요** |
| ② | web, 개발 서버를 띄우고 답함 | `complete` 재호출 | `wait` (영원히 잠듦) | 합성 |
| ③ | run, 개발 서버를 띄우고 답함 | 멈춘 뒤 `complete` | `wait` / 재-`complete` | 합성 |
| ④ | 과제에 정답을 냈고 남은 작업은 곧 끝남 (부록 A 후반) | 다시 `complete` (오탐 비용 측정) | `wait` | **있음 (4건)** |

①을 합성하려면 녹화 세션의 마지막 `complete` 를 손으로 만들어 넣어야 한다. 그러면
"모델이 실제로 이런 상황을 만드는가"는 측정되지 않고, "만들었을 때 되돌림이
고치는가"만 측정된다.

---

## 7. 리뷰어에게 묻는 것

- **Q0. 고칠 가치가 있나?** §3.4 와 §4 를 근거로 A·B·C·D(또는 다른 안) 중 무엇을
  고를지. 빈도 0건이라는 판단이 기록의 모양 때문에 과소평가된 것인지(짧은 과제 위주,
  긴 세션 부재)도 봐 주면 좋겠다.
- **Q1. 개입 위치**(D): `_op_complete` 안(장부 정산 직후)이 맞나, 런 끝
  처리(`run_ended`)가 맞나? 전자는 관찰로 되돌릴 수 있고, 후자는 표면과 무관하게 한
  곳이다.
- **Q2. run 에서 두 번째 `complete` 때도 작업이 돌고 있으면**: (a) 받아들이고 그대로
  둔다(무한 정지 위험), (b) 받아들이되 하니스가 작업을 정리하고 끝낸다, (c) 받아들이되
  중간 깨우기를 건다. 정하지 않았다.
- **Q3. web 의 "확인받은 집합 기억"** 이 너무 관대한가? 새 작업이 섞이면 다시 묻는다.
- **Q3b.** 재-`complete` 로 확인받은 작업이 방의 self-reap 을 계속 막는 것이 맞나? 사람이
  떠난 뒤 개발 서버를 든 방이 영원히 사는 것과, reap 이 작업을 죽이는 것 중 어느 쪽이
  기대인가? 이 질문은 D 와 무관하게 지금도 있다.
- **Q4. monitor 만 남은 경우**: monitor 는 deadline 이 있다. 개입 대상에서 빼야 하나?
- **Q5. 예산**: 장부 독촉 예산(8)과 별개인 "런당 한 번"이 맞나, 같은 예산을 써야 하나?
- **Q6.** 하니스 밖 프로세스(§2 끝)를 줄이는 저비용 수단이 있나? 예를 들어 shell 결과에
  `docker run -d` 모양이 보이면 한 줄을 붙이는 것. 문구를 추가하는 것이라 측정 대상이다.

---

## 8. 코드 지도 (main `efbe073b`)

| 무엇 | 위치 |
|---|---|
| `complete` 처리, 장부 정산, 독촉 진입 | `agent_cli/loop/dispatch.py:1103` `_op_complete` |
| 독촉 관찰 문구, "call `wait` instead" 힌트 | `agent_cli/loop/dispatch.py:1340–1365` |
| `wait` 처리 | `agent_cli/loop/dispatch.py` `_op_wait` (`_op_complete` 바로 아래) |
| 깨울 것 id 집합 | `agent_cli/runtime.py:483` `wake_source_ids` |
| 할 일 판정 (run 정지, web reap 공통) | `agent_cli/runtime.py:600` `session_has_live_work` |
| run 수명 정책, 대기 안내 `on_wait` | `agent_cli/pump.py:84` `QuietPolicy` |
| 런 끝 단일 정리, 중간 깨우기 무장 | `agent_cli/subagent/agents_live.py:1321` `run_ended`, 이어서 `arm_checkin` |
| 중간 깨우기 간격, 알림 문구 | `agent_cli/runtime.py` `CHECKIN_*` (`wake_source_ids` 아래) |
| 작업 종료 메일 | `agent_cli/jobs.py:403` |
| 표면 구분 | `agent_cli/render/base.py:227`, `agent_cli/render/web.py:2209` `can_pause_for_human` |
| 관련 테스트 픽스처 | `tests/test_wait_checkin.py` (`reg`, `jobs`, `_fake_agent`, `_armed`) |

관련 설계 문서: `docs/wait/DESIGN.md`(`wait`, §6 중간 깨우기, §7 스킬 루프),
`docs/ledger/DESIGN.md`(장부·독촉), `docs/pump/DESIGN.md`(run·web 수명),
`docs/jobs/DESIGN.md`(하니스 소유 작업).

---

## 부록 A. 기록 스캔에서 `answers` 로 다 닫고 빠져나간 13건

경로는 `agent-harness/` 기준이다. `<scratch>` 는 2026-11 세션의 임시 폴더라 사라질 수
있다. 레코드 번호는 history.jsonl 의 0부터 센 줄 번호다.

**`wait` 가 없던 빌드 (벤치, 9건)**

- `agent-cli/bench/runs/harbor/pytool-x3-off-v10.65.0/extract-moves-from-video__N6LGsW9/agent/sessions/1792673748/history.jsonl` 레코드 56
  - answers: `['n-1', 'n-2', 'u-1']` · 돌던 작업: `apt-get update -qq 2>&1 | tail -3 && apt-get install -y -qq ffmpeg tes` → 나중에 rc=0 (2m28s 뒤) 통지로 깸
  - 모델의 result: "I'm waiting for job #2 to complete — the apt-get install of ffmpeg, tesseract-ocr, python3, and pip3. Once ins"
- `agent-cli/bench/runs/harbor/pytool-x3-off-v10.65.0/extract-moves-from-video__N6LGsW9/agent/sessions/1792673748/history.jsonl` 레코드 69
  - answers: `['n-3']` · 돌던 작업: `ffmpeg -i /app/video.mp4 -vf "fps=0.5,scale=1920:1080" /app/.agent-cli` → 나중에 rc=0 (38s 뒤) 통지로 깸
  - 모델의 result: "I'm waiting for job #3 to finish — the ffmpeg frame extraction at 0.5fps from the Zork video. It's been runnin"
- `agent-cli/bench/runs/harbor/pytool-x3-off-v10.65.0/extract-moves-from-video__NpbPRtm/agent/sessions/1792671590/history.jsonl` 레코드 44
  - answers: `None` · 돌던 작업: `apt-get update -qq 2>&1 | tail -3 && apt-get install -y -qq python3 py` → 나중에 rc=0 (5m11s 뒤) 통지로 깸
  - 모델의 result: "Waiting for background job #1 (apt-get install python3, ffmpeg, tesseract-ocr) to complete. The job has been r"
- `agent-cli/bench/runs/harbor/tb21-35b-a3b-off-v10.58.1-native/extract-moves-from-video__yuv4ovh/agent/sessions/1792499436/history.jsonl` 레코드 243
  - answers: `['n-11' … 'n-25']` (15개) · 돌던 작업: `python3 -c " import whisper print('Loading small model...') model = wh` → 나중에 rc=0 (13m37s 뒤) 통지로 깸
  - 모델의 result: "Waiting for job #9 (whisper small model transcription of /app/.agent-cli/tmp/audio.wav). The model has finishe"
- `agent-cli/bench/runs/harbor/pytool-x3-off-v10.64.1/extract-moves-from-video__DXJ6iii/agent/sessions/1792681398/history.jsonl` 레코드 45
  - answers: `['n-1', 'u-1']` · 돌던 작업: `sleep 5; dpkg --configure -a 2>/dev/null; apt-get install -y ffmpeg 2>` → 나중에 rc=0 (32s 뒤) 통지로 깸
  - 모델의 result: "Waiting for ffmpeg installation (job #2) to complete so I can process the Zork video and transcribe the gamepl"
- 같은 파일 레코드 54
  - answers: `['n-2', 'n-3']` · 돌던 작업: `# Extract frames every 2 seconds from the video ffmpeg -i /app/video.m` → 나중에 rc=0 (28s 뒤) 통지로 깸
  - 모델의 result: "Waiting for job #3 to finish extracting frames from the video. I'll then use OCR on the frames to transcribe t"
- 같은 파일 레코드 62
  - answers: `['n-4']` · 돌던 작업: `ffmpeg -i /app/video.mp4 -vf "fps=1,scale=2560:-1:flags=lanczos" "/app` → 나중에 rc=0 (1m10s 뒤) 통지로 깸
  - 모델의 result: "Waiting for job #4 to finish extracting high-resolution frames. I'll then use OCR to transcribe the Zork gamep"
- 같은 파일 레코드 70
  - answers: `['n-5']` · 돌던 작업: `apt-get install -y tesseract-ocr 2>&1 | tail -5` → 나중에 rc=0 (2m08s 뒤) 통지로 깸
  - 모델의 result: "Waiting for job #5 to finish installing tesseract-ocr. I'll then use OCR to transcribe the Zork gameplay comma"
- 같은 파일 레코드 80
  - answers: `['n-6']` · 돌던 작업: `# Process all frames and extract user commands for f in $(ls /app/tmp_` → 나중에 rc=0 (6m34s 뒤) 통지로 깸
  - 모델의 result: "Waiting for job #6 to finish OCR processing all 190 frames. I'll then extract the user commands from the OCR o"

참고: `NpbPRtm` 의 `answers: None` 이 독촉에 걸리지 않은 이유는 확인하지 않았다. 그
시점에 열린 항목이 없었을 가능성이 있다.

**monitor 재생 실험, 정당한 `complete` (오탐 예, 4건)**

과제: "build.log 에 ERROR 줄이 나오면 알려 줘." `writer.sh` 가 로그를 쓰다가 1분 30초 뒤
저절로 끝난다.

- `<scratch>/brs/defer/ws-round1/mon-base-r1/.agent-cli/sessions/1793890856/history.jsonl` 레코드 6
  - answers: `['n-2', 'u-1']` · result: "An ERROR line appeared in build.log (about 2.5 minutes in): … ERROR: link failed — undefined sym…"
- `<scratch>/brs/defer/ws-round1/mon-v1-r1/.agent-cli/sessions/1793890963/history.jsonl` 레코드 10
  - answers: `['n-2', 'u-1']` · result: "ERROR appeared in build.log: … ERROR: link failed — undefined symbol frobnicate …"
- `<scratch>/brs/defer/ws/mon-final-r2/.agent-cli/sessions/1793894552/history.jsonl` 레코드 8
  - answers: `['n-2', 'u-1']` · result: "ERROR appeared in build.log at 01:04:08: …"
- `<scratch>/brs/defer/ws-round1/mon-v1-r4/.agent-cli/sessions/1793893102/history.jsonl` 레코드 10
  - answers: `['n-2', 'u-1']` · result: "ERROR appeared in build.log at 00:40:02: …"

---

## 부록 B. 재현 방법

도구는 이 폴더의 `tools/` 에 있다.

### B.1 단위 프로브

테스트 픽스처를 재사용하므로 `tests/` 안에서 실행해야 한다.

```bash
cd agent-cli
cp docs/complete-while-running/tools/probe_hole.py tests/_tmp_probe_hole.py
python -m pytest -q -s tests/_tmp_probe_hole.py
rm tests/_tmp_probe_hole.py
```

### B.2 라이브 재현 (run)

모델 서버(omlx)는 한 번에 한 소비자만 쓴다. 다른 런이 없을 때 실행한다.

```bash
mkdir -p /tmp/hole && cd /tmp/hole        # 아무 빈 폴더
cp <agent-cli>/docs/complete-while-running/tools/build.sh .
agent-cli run -m Qwen3.6-35B-A3B-8bit \
  "build.sh 로 빌드를 백그라운드로 돌리고 결과를 알려 줘. wait 는 쓰지 말고, 빌드를 띄우면 바로 complete 로 끝내."
# 기대: complete 뒤 "Waiting on 1 background job" 에서 멈춤, 5분이 지나도 턴이 없음.
# 정리: Ctrl-C, 그리고 남은 빌드는 kill -- -<pgid>  (자식 sleep 까지)
```

자연 프롬프트(§3.2 의 1~4번)는 마지막 문장만 바꿔 넣으면 된다.

### B.3 기록 스캔

```bash
find ~ -path ~/Library -prune -o -name history.jsonl -path "*sessions*" -print > hist.txt
python3 docs/complete-while-running/tools/scan_history.py hist.txt hits.json
# 출력: files=… with_jobs_or_agents=… hits=…
# hits.json 의 각 항목: file, idx(레코드 번호), jobs, reqs, answers, later/pending
# 그 다음 레코드가 "your result was delivered, but" 이면 독촉이 잡은 것(기존 경로)이다.
```

`hist.txt` 에는 **절대 경로**를 넣어야 한다. 상대 경로를 넣으면 실행 위치에 따라 조용히
빠진다.
