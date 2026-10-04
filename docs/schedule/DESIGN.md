# Schedule — 세션 소유 예약 실행 설계

> 상태: **agent-cli 구현 완료 (v10.12.0)** · agent-board 는 v1.34.0 (2026-10-04 공동 설계)
> 저장소: agent-cli(주) + agent-board(삭제·표시) — 짝 릴리스
> 대체하는 문서: `agent-board/docs/schedule-design.md` (2026-08-13, 보드 내부 스케줄러)

## 1. 문제

`schedule` 도구는 보드가 띄운 세션에서만 동작한다. agent-cli 안에는 스케줄러가
없고, 도구는 요청 파일에 한 줄을 적을 뿐 시각을 재고 프롬프트를 넣는 일은 보드가
한다(env `AGENT_CLI_SCHEDULER=1` 게이트). 보드 없이 쓰는 `agent-cli web` 과
`agent-cli run` 에는 도구 자체가 없다.

의도한 계약은 반대다: **예약은 agent-cli 의 기능이고 보드는 그리기만 한다.**

## 2. 확정된 결정 (사용자)

| 결정 | 선택 |
|---|---|
| 스케줄러 위치 | **agent-cli 프로세스 안**. 보드의 스케줄러는 삭제 |
| 프로세스 수명 | 켜진 예약이 있으면 **꺼지지 않는다** — "유지" 와 같은 효과. 2026-08-13 설계가 자원 소모로 기각한 안을 뒤집는다(실측: 떠 있는 방 하나 RSS 약 146 MB, 표본 1) |
| 소유 단위 | **세션**. 예약은 만든 세션의 것이고, 프로세스는 자기 세션의 예약만 발화한다 |
| 동시 실행 | 같은 세션의 **두 번째 프로세스는 기동 거부**. 동시 실행을 전제한 예외 처리는 두지 않는다 |
| 일회성 `run` | 동일 적용 — 예약이 있으면 끝나지 않고 기다린다(`monitor` 와 같은 동작) |
| 놓친 발화 | 자동 실행 없음. `missed` 로 두고 "지금 실행 / 건너뛰기" 를 묻는다 (종전 결정 유지) |
| 예약 화면 | **방 화면(agent-cli web)** 의 ⏰ 서랍. 보드 목록은 배지만 |
| 이력 | `schedule-log.jsonl` 에 발화·놓침·건너뛰기·실패를 남긴다 (판정에는 쓰지 않음) |
| 호환 | 호환층 없음. 보드 DB 의 예약은 한 번 옮기고 테이블을 지운다 |

## 3. 이미 있는 것 (재사용)

- **`monitor` 의 뼈대** (`agent_cli/monitor/`): 프로세스 안 스레드가 깨울 때를
  판단하고, `has_active_work()` 가 유휴 종료 술어(`web_instance_is_active`)와
  `run` 의 펌프(`_run_message_pump`) 두 곳에 들어가 프로세스를 살려 둔다. 예약도
  같은 두 곳에 한 줄씩 들어간다.
- **입력 큐** (`agent_cli/input_queue.py`): web 의 `/api/input` 과 `run` 의 펌프가
  같은 `InputQueue` 를 쓴다. 발화는 여기에 `nickname` 을 단 채팅 항목을 넣는 것이
  전부다 — 일하는 중이면 턴 경계에 주입되고, 쉬고 있으면 워커가 깨어난다.
- **cron 파서** (`agent-board/agent_board/cron.py`, 205줄, 의존성 0): `parse` ·
  `next_fire` · `describe`. 그대로 옮긴다.
- **보드의 정산 규칙** (`agent-board/agent_board/scheduler.py`): sleep-until-next
  + rearm, `MAX_SLEEP_S = 300`, `MISS_THRESHOLD_S = 120`, `last_fired_at` 가드.
  규칙은 그대로, asyncio 태스크만 스레드로 바뀐다.

## 4. 데이터

전부 세션 폴더(`<workspace>/.agent-cli/sessions/<sid>/`) 안에 둔다.

### 4.1 `schedules.json` — 현재 상태 (원자적 재기록)

```json
{
  "v": 1,
  "schedules": [
    {
      "id": "a1b2c3",
      "source": "agent",
      "cron": "0 9 * * 1",
      "prompt": "주간 보고를 작성해줘",
      "label": "주간 보고",
      "nickname": "",
      "enabled": true,
      "created_at": "2026-10-04T10:12:00",
      "settled_at": "2026-10-05T09:00:02",
      "last_fired_at": "2026-10-05T09:00:02",
      "missed_at": null
    }
  ]
}
```

- `source`: `user`(⏰ 서랍) | `agent`(도구). 에이전트 등록분은 세션당 5개 캡.
- `nickname`: 비면 `⏰ Scheduler`.
- 시각은 서버 로컬, naive ISO (cron 의미가 로컬 시각이다).

### 4.2 `schedule-log.jsonl` — 이력 (append-only)

```json
{"ts": "…", "id": "a1b2c3", "label": "주간 보고", "event": "fired",   "due": "…"}
{"ts": "…", "id": "a1b2c3", "label": "주간 보고", "event": "missed",  "due": "…"}
{"ts": "…", "id": "a1b2c3", "label": "주간 보고", "event": "skipped", "due": "…"}
{"ts": "…", "id": "a1b2c3", "label": "주간 보고", "event": "failed",  "due": "…", "error": "…"}
```

사람이 "왜 월요일 보고가 안 나왔지" 를 추적하는 용도. 정상 발화는 대화 기록에도
남지만 놓침·건너뛰기·실패는 여기에만 남는다.

### 4.3 세션은 한 프로세스만 연다

종전에는 agent-cli 자체에 막는 장치가 없었다 — 같은 세션을 `--resume` 으로 두 번
띄우면 둘 다 돌았다(보드는 `web.json` 의 pid 를 보고 스스로 피했을 뿐이다). 그러면
둘 다 같은 예약을 발화한다.

**두 번째 프로세스는 기동을 거부한다** (사용자 결정). 세션 폴더의 `session.lock`
에 OS 파일 잠금(`flock`)을 걸고 프로세스가 끝날 때까지 쥔다 — 프로세스가 어떻게
죽든 커널이 풀어 주므로 낡은 잠금이 남지 않는다. 못 쥐면 "session <id> is already
open in another process (pid N)" 로 끝낸다(`web` · `run` 공통, 종료 코드 ≠ 0).

이로써 한 세션 = 한 프로세스가 불변식이 되고, 동시 실행을 전제한 처리(발화 권한
승계, 파일 변경 감지, 병합)는 **두지 않는다**.

## 5. 정산 규칙

```
루프 (데몬 스레드):
  next = min(next_fire(s, after=now) for enabled s)      # 없으면 rearm 까지 대기
  wait(rearm_event, timeout=min(next − now, 300s))       # 300s = 절전·시계 점프 안전망
  for s in enabled:
    prev = now 기준 직전 발화 시각
    if settled_at >= prev:              continue          # 이미 정리됨 (exactly-once)
    if now − prev > 120s:               missed(s, prev)   # 자동 실행 금지
    else:                               fire(s, prev)

fire(s, due):
  input_queue.enqueue(text=s.prompt, nickname=s.nickname or "⏰ Scheduler")
  last_fired_at = settled_at = now; missed_at = null; log "fired"
  실패 시: missed(s, due) 와 같되 log "failed" — 깰 때마다 재시도하지 않는다

missed(s, due):
  missed_at = settled_at = due (여러 주기 놓쳐도 덮어써서 질문 1건); log "missed"
```

- 프로세스 기동 시에도 같은 정산을 한 번 돈다 — 꺼져 있던 동안 지난 것이 여기서
  `missed` 가 된다.
- `settled_at` = "이 시각까지의 발화는 전부 정리됐다". 만들 때·다시 켤 때·발화·
  놓침 판정·건너뛰기에 갱신한다 — exactly-once 가드이자, 만들기 전과 꺼 둔 동안이
  놓친 것으로 잡히지 않는 근거다. `last_fired_at` 은 표시용이다.

## 6. agent-cli 변경

### 6.1 새 패키지 `agent_cli/schedule/`

| 파일 | 내용 |
|---|---|
| `cron.py` | 보드에서 이식 |
| `registry.py` | `ScheduleRegistry(session_dir, enqueue)` — 저장·정산 스레드·`has_active_work()` |
| `runtime.py` | 프로세스 전역 접근자 (`monitor/runtime.py` 와 같은 모양) |

### 6.2 `schedule` 도구

- **항상 등록** (`monitor` 옆). env 게이트 `AGENT_CLI_SCHEDULER` 삭제.
- 인자는 그대로: `mode` add/list/delete, `cron`, `prompt`, `label`, `nickname`, `id`.
- 요청·회신 파일 계약(`schedule-requests.jsonl`, `schedule-state.json`)과 회신
  대기 폴링 삭제 — 레지스트리를 직접 부르고 결과가 즉시 나온다.
- 도구 설명에 수명을 적는다: 예약은 세션에 남고, 프로세스가 켜져 있는 동안 발화하며,
  꺼져 있던 동안 지난 것은 자동 실행되지 않는다.
- `monitor` 설명의 "On a board session, …" 문장은 "For periodic work prefer
  `schedule` — it survives a restart, a monitor does not." 로 고친다.

### 6.3 프로세스 수명

- `web_instance_is_active(...)`: `schedules.has_active_work()` 추가.
- `_run_message_pump(...)`: 같은 한 줄. 기다리기 시작할 때 한 번 알린다 —
  "예약 N개 대기 중, 다음 발화 <시각>, Ctrl-C 로 종료".
- `has_active_work()` = 켜진 예약이 하나 이상.

### 6.4 web

| 메서드 | 경로 | 동작 |
|---|---|---|
| GET | `/api/schedules` | 목록 (+ `describe`, `next_fire`, `missed_at`, 최근 이력 N줄) |
| POST | `/api/schedules` | `{cron, prompt, label?, nickname?}` → `source=user` |
| DELETE | `/api/schedules/{id}` | 삭제 |
| POST | `/api/schedules/{id}/toggle` | 켜기·끄기 |
| POST | `/api/schedules/{id}/run-now` | 즉시 발화 (missed 해소 겸용) |
| POST | `/api/schedules/{id}/dismiss` | 놓친 발화 건너뛰기 |

뷰는 `agent_cli/schedule/view.py` 가 만든다(`human` = `cron.describe` 한글 라벨 —
화면용이다. 모델에게 가는 도구 출력은 cron 원문을 쓴다).

- 헤더에 ⏰ 버튼 + 서랍: 목록 행(👤/🤖 · label · "매주 월 09:00" · 다음 발화 ·
  토글·지금 실행·삭제), 추가 폼, 최근 이력.
- 놓친 발화가 있으면 대화 위에 카드: "⏰ 놓친 예약: <label> — 지금 실행 / 건너뛰기".
- 변경은 SSE 로 알린다(다른 탭·보드 배지 갱신).

### 6.5 `run` 에서 놓친 발화

단일 실행에는 물을 화면이 없다. 기동 때 놓친 것이 있으면 한 줄로 알리고
`missed` 로 둔다 — 답은 web 에서 하거나, 모델이 `schedule` 도구로 목록을 보고
사용자에게 전한다. 자동 실행은 어디서도 하지 않는다.

## 7. agent-board 변경

- **삭제**: `scheduler.py`, `sched_contract.py`, `cron.py`, `schedules` 테이블과
  store 메서드, 예약 API 여섯 개, ⏰ 패널 UI, spawn 시 `AGENT_CLI_SCHEDULER` env.
- **이전(1회)**: 기동 때 `schedules` 테이블이 있으면 각 post 의 세션 폴더
  `schedules.json` 으로 옮기고 테이블을 지운다. 그 방의 인스턴스가 떠 있으면 먼저
  멈춘다 — 예약 파일은 프로세스가 기동 때만 읽으므로, 떠 있는 프로세스 밑에서 쓰면
  반영되지 않고 다음 저장에 덮인다(멈춘 방은 아래 "다시 띄우기" 가 되살린다). `session_id` 가 아직 없는 post 의
  예약은 옮길 곳이 없으므로 로그에 남기고 버린다(발화한 적 없는 방이다).
- **배지**: live_events 스캐너가 세션 폴더의 `schedules.json` 을 읽어 목록 카드에
  `⏰ N` 과 놓친 발화 표시만 한다. 누르면 방을 연다.
- **다시 띄우기**: 켜진 예약이 있는 방은 ① 보드 기동 시 ② 인스턴스 사망을 감지했을
  때 `orchestrator.open()` 으로 띄운다. "유지" 체크박스와 별개의 조건이다(유지는
  뷰어 연결을 붙드는 것, 이쪽은 프로세스 스스로 안 꺼지는 것).

## 8. 릴리스

- agent-cli **v10.12.0** (MINOR — 도구 표면 변경) → agent-board **v1.34.0**.
- 순서: agent-cli 먼저. 그 사이(새 cli + 옛 보드)에는 보드 스케줄러가 계속 DB 의
  예약을 주입하고 cli 의 새 예약은 따로 돈다 — 중복은 없지만 두 곳에 예약이 있는
  상태이므로 간격을 두지 않고 이어서 낸다.
- 보드 v1.34.0 은 cli ≥ 10.12.0 과 짝이다. 보드에는 cli 버전 게이트가 없어 강제하지는
  않는다 — 옛 cli 를 띄우면 그 방에는 예약 기능이 없을 뿐이다(README 에 명시).

## 9. 구현 순서 (커밋 단위)

agent-cli:
0. 세션 잠금 — 두 번째 프로세스 기동 거부 (`web` · `run`)
1. `schedule/cron.py` 이식 + 테스트
2. `schedule/registry.py` — 저장·정산·이력, 가짜 시계 테스트
   (정시 1회 · 중복 방지 · rearm · 120s 경계 · 여러 주기 놓침 1건 · 만들기·켜기 전
   시각 · `last_fired_at` 가드 제거 시 실패해야 하는 뮤테이션 테스트)
3. 배선 — 전역 접근자, 큐 주입, 수명 술어 두 곳, `run` 대기 알림
4. 도구 재작성 — 항상 등록, 파일 계약·env 게이트 삭제, 설명 갱신
5. web API + ⏰ 서랍 + 놓친 발화 카드 (브라우저 테스트)
6. README · ARCHITECTURE · 버전

agent-board (한 커밋으로 냈다 — 삭제와 대체가 서로 물려 있다):
1. DB → 파일 이전(`session_schedules.migrate_legacy`) + 스케줄러·계약·API·패널 삭제
2. 배지(`session_schedules.summary`) + 다시 띄우기(`ScheduleReviver`, 방당 60초에 한 번)
3. 복제본은 예약 파일을 물려받지 않는다 · 문서 · 버전

## 10. 위험

- **끝나지 않는 `run`**: 스크립트·벤치에서 모델이 스스로 예약을 걸면 그 실행은
  바깥에서 죽일 때까지 멈춰 있다(`monitor` 는 기한이 있지만 예약은 없다). 감수한다.
  실제로 문제가 되면 "기다리지 않기" 옵션을 그때 더한다.
- **자원**: 예약 있는 방 수 × 약 146 MB. 방이 많아지면 다시 본다.
- **보드의 재실행**: 🔄 는 옛 프로세스가 죽기를 기다린 뒤 띄운다(`_await_dead`). 세션
  잠금이 생기면 이 대기가 빠진 경로는 기동 거부로 드러난다 — 보드 쪽에서 확인한다.
- **보드가 꺼진 동안**: 프로세스가 살아 있으면 발화한다(종전에는 보드가 꺼지면
  전부 멈췄다). 프로세스도 죽으면 다음 기동 때 `missed`.
