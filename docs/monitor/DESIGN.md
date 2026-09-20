# Monitor — 조건 → 액션 감시 도구 설계

> 상태: **설계 확정(개정 2판) · 구현 대기**
> 2026-09-17 공동 설계 · 2026-09-19 §10 해소 · **2026-09-20 외부 리뷰 반영(§11)**
> 재개 시 §9(실행 계획)부터 읽으면 된다. 열린 질문은 없다.
>
> **2판에서 바뀐 것** — 리뷰가 실측으로 뒤집은 전제 셋이 있다(§11 에 경위):
> 배달 경로(큐 → 메일박스) · 조건 5종 → **3종**(`exit` 구현 불가, `interval` 은
> 주기형 `command` 에 흡수) · 액션 레지스트리 삭제(`notify` 는 암묵, `shell` 은
> 필드 하나). 영속은 **부활 없이 기록만** 한다.
>
> 2026-09-19 재검증: §2 가 전제한 것(`_run_message_pump` · `has_active_work` ·
> `MailWaker` · `ScheduleTool` · `build_agent_registry` · `_ALL_TOOLS`)이 전부
> 그대로 있다. v9.4.0~9.10.0 의 변경은 렌더·경로 게이트 계층이라 이 설계에 영향 없음.

## 1. 문제

아주 오래 도는 스크립트를 띄워 놓고 상황을 보고받고 싶다. 사용자의 원안:

```
shell 로 실행 → 출력을 파일로 리다이렉션 → 감시 도구가 그 파일을 주기적으로
읽으며 보고
```

이걸 **특정 조건 → 어떤 액션**으로 일반화한다. 액션 중 하나가 "등록한 LLM 에게
알리기".

## 2. 조사 결과 — 이미 있는 것

구현 전에 확인한 사실들. 설계를 크게 좁혔다.

**① 백그라운드 실행은 지금도 된다. `shell` 을 고칠 필요가 없다.**

`ShellTool` 은 `subprocess.run(..., capture_output=True)` 라 동기지만, **출력을
파일로 리다이렉션하면** 자식이 파이프를 물지 않아 `sh` 가 즉시 끝난다:

```
shell("nohup long.sh > /tmp/out.log 2>&1 &")   # 0.01s 반환, 자식은 계속
shell("nohup long.sh > /tmp/out.log &")        # ✗ 120s 타임아웃까지 막힌다
```

**양쪽 스트림을 다 돌려야 한다** (2판 정정). stderr 가 파이프에 남으면
`capture_output=True` 가 그걸 계속 읽으려 해 `subprocess.run` 이 타임아웃까지
블록한다 — 실측: 전자 0.01s, 후자 TIMEOUT. 초판은 예시에만 `2>&1` 을 넣고
산문에는 "파일로 리다이렉션하면"이라고만 적어, **35B 가 `> log &` 만 쓰면
조용히 막히는** 함정을 남겨 뒀다. 그래서 실행 관용구는 도구 설명에 **박아야
하는 산출물**이다(§4.3).

생산자 쪽 버퍼링도 같은 부류다: 파이썬 스크립트가 리다이렉션된 파일에 쓰면
블록 버퍼(8KB)라 **건강한 스크립트가 침묵으로 보이고** `match` 가 몇 분 늦게
본다. 선언형 조건이 이걸 없애 주지 않는다 — `python -u` / `PYTHONUNBUFFERED=1`
/ `stdbuf -oL` 도 같은 자리에 적는다.

**② 깨우기 메커니즘이 이미 있다 — `MailWaker` + `InputQueue`.**

teammate 회신용으로 만든 것. main 이 idle 이면 합성 아이템을 입력 큐에 넣어
깨운다. `_armed` 플래그로 "여러 mail → wake 하나" 합치기까지 되어 있다.
(`agents_live.py::MailWaker`)

**단, 큐가 나르는 것은 내용이 아니라 마커다** (2판 정정 — 초판이 여기서
틀렸다). `MailWaker` 가 넣는 것은 상수 `WAKE_TEXT` 하나이고
(`agents_live.py:1604-1607`), 내용은 다른 채널로 간다:

```
큐:      WAKE_TEXT (깨우기 신호)        → 펌프가 런을 시작
메일박스: registry._pending → drain_replies() → _deliver_agent_mail()
         → 턴 경계에서 tool="agent" **관찰 레코드**로 주입 (loop/core.py)
```

**깨우는 채널과 나르는 채널이 다르다.** 이 구분을 놓치면 보고문을 큐에 태우게
되고, 그러면 보고가 *사람 메시지로 위장*된다 — §6 이 그 결과를 정리한다.

**③ `run` 도 web 과 같은 펌프를 쓴다 — `_run_message_pump` (main.py).**

정지 판정은 "큐 비었고 `registry.has_active_work()` False". `enqueue(conn_id,
text)` 시그니처가 run(`InputQueue.enqueue`)·web(`server.enqueue`) 동일이라
**배달 코드 한 벌이 양쪽에 붙는다.**

**④ 펌프는 조건 평가 지점이 될 수 없다.**

`run_one()` 을 **동기로** 부르므로 턴이 도는 몇 분 동안 루프가 멈춰 있다. 조건
평가는 **전용 스레드**여야 한다 — `AgentRegistry` 가 이미 같은 모양(스레드 소유 +
`has_active_work()`)이라 형제로 붙인다.

**⑤ ~~web 은 무한 대기다~~ — 틀렸다. web 도 자기수확한다** (2판 정정).

`--idle-timeout` 이 `web_instance_is_active` (`main.py:1566-1579`) 로 자기를
거둔다. 그 술어는 뷰어·worker·`agent_registry.any_activity()`·대기 큐를 보지만
**모니터를 모른다.** board 가 띄운 인스턴스에 모니터가 살아 있어도 뷰어가
없으면 인스턴스가 사라지고 **모니터는 조용히 죽는다.**

즉 수명 결합은 `run` 만의 문제가 아니다 — §7.1 의 한 줄이 **두 곳**에 들어간다.

## 3. 기각한 대안

### 3.1 cron (`schedule` 도구 / 시스템 crontab)

`ScheduleTool` 이 이미 있다. 단, agent-cli 에 스케줄러는 없고 **agent-board 의
스케줄러에 파일로 요청을 넘기는 얇은 클라이언트**다 (`AGENT_CLI_SCHEDULER=1`
일 때만 등록됨). board 세션이면 이것만으로 원안이 충족된다 — 코드 0 줄:

```
schedule(add, cron="*/5 * * * *", prompt="/tmp/out.log 마지막 50줄 보고 요약해줘")
```

**그래서 board 환경에선 monitor 를 안 만들어도 된다.** 못 하는 건 셋:

- **조건부 깨우기 불가** — 트리거가 *시간*이지 *내용*이 아니다. 변화가 없어도
  매 틱 한 턴을 태우고, 30초 만에 ERROR 가 나도 다음 정각까지 모른다(최소 1분).
- **상태 없음** — 매 트리거가 독립 프롬프트라 "지난번 이후 새 줄"을 모른다.
- **headless 부재** — `run`·harbor·CI 에선 도구가 등록조차 안 된다.

시스템 crontab 은 더 나쁘다. 에이전트 **프로세스 밖**에서 돌아 살아 있는 세션에
알릴 방법이 없다. 새 `run` 을 띄우면 컨텍스트가 없고, 파일에 써두고 세션이
폴링하게 하면 **세션이 로그를 직접 폴링하는 것보다 나은 게 없다**(펌프는 이미
0.5s 마다 돈다). cron 의 가치는 *아무것도 안 돌 때* 깨우는 것이고, 감시는 반대로
*이미 살아 있는 세션*이 대상이다.

**결론**: 축이 다르므로 공존. headless 가 필요하다는 게 확정이라 monitor 를 만든다.

### 3.2 Claude Code 의 Monitor 모델 (셸 파이프라인)

Claude 의 `Monitor` 도구는 **조건/액션 DSL 이 없다**. 백그라운드 스크립트의
**stdout 한 줄 = 알림 하나**이고 액션은 언제나 "알린다" 하나다. 조건 타입을
추가할 일이 없고, 프로그램으로 표현되는 건 전부 지원된다 — 확장성을 플러그인이
아니라 **조합**으로 얻는다.

우아하지만 **그대로 옮기면 안 된다**: 셸 버퍼링을 정확히 다뤄야 한다(`grep
--line-buffered`, `awk fflush()`, `head` 는 플러시 불가). 한 단이라도 버퍼링하면
매치가 갇혀 **조용히 아무 알림도 안 온다**. agent-cli 의 대상은 Qwen3.6-35B 급이고,
이 모델급은 추상 지시를 구체 행동으로 잘 못 바꾼다는 실측이 있다(harbor 프롬프트
채널 실험). `{"type":"match","pattern":"ERROR"}` 를 내는 것과 버퍼링까지 맞춘
파이프라인을 짜는 것은 난이도가 다르다.

> 같은 문제에 답이 갈리는 이유가 도구가 아니라 **작성자의 역량**이다.
> Claude 에겐 프로그램이 맞고, 35B 에겐 선언형이 맞다.

**절충**: 선언형을 기본으로 하되 **탈출구로 `command` 조건 타입 하나**를 둔다.
흔한 것은 안전하게 선언형으로, 표현 안 되는 건 셸로 내려간다.

(실증: 이 설계 대화 중 Claude 자신이 `gh run list --limit 1` 폴링 스크립트를
잘못 짜, 최근 목록에 있지도 않은 옛 run 을 집어 "success" 로 **오보**했다.
실패가 조용하고 그럴듯했다. 선언형이면 이 부류 버그가 훨씬 작다 — 감시 대상을
ID 로 고정하는 건 필드 하나지 jq 파이프라인이 아니다.)

### 3.3 블로킹 대기 도구

`monitor(file, until="DONE", timeout=600)` 이 도구 호출 안에서 막고 결과 반환.
구현은 거의 없지만 "주기적 보고"가 안 되고(끝에 한 번), 대기 중 에이전트가 아무
것도 못 하며, `agent_timeout`·무진전 워치독과 부딪힌다. 감시+깨우기의 퇴화형이라
나중에 `mode="wait"` 로 얹을 수 있지만 반대는 불가.

## 4. 도구 표면

```python
monitor(mode="add",
        when={"type": "match", "file": "/tmp/out.log", "pattern": "ERROR|FAILED"},
        run="kill 4231",       # 선택 — 보고 전에 실행 (§7.3 게이트)
        deadline="2h",         # 선택 (기본 2h, 60s~24h clamp)
        once=True)             # 기본값
monitor(mode="list")
monitor(mode="delete", id="mon-3")
```

`add`/`delete`/`list` 세 모드는 **`ScheduleTool` 과 같은 어휘**를 의도적으로 맞춘다.

**`when` 은 하나만.** AND/OR 합성을 넣지 않는다 — 규칙 엔진이 비대해지는 지점이
정확히 거기고, 조건 둘이 필요하면 monitor 둘을 걸면 된다.

**알림은 액션이 아니라 모니터의 정의다** (2판). 초판은 `then=[{"type":"notify"}]`
리스트였는데, 액션 2종에 조합은 하나(notify+shell)뿐이었다. 그리고 곱집합의
위험한 원소는 **notify 없는 shell** 이다 — 새벽 3시에 명령이 돌았는데 사람도
모델도 영영 모른다. 그래서 알림은 암묵으로 두고 `shell` 은 선택 필드
`run="<cmd>"` 로 낮춘다(보고 **전에** 실행하고 종료 코드와 출력 꼬리를 보고문에
싣는다). Action ABC 와 레지스트리 하나가 통째로 사라지고, 35B 가 틀릴 중첩
객체도 하나 준다.

**`stop` 은 액션이 아니다.** 셋을 구분해야 한다: ①모니터 자신 ②감시 대상
프로세스 ③에이전트 런. ①이 압도적으로 흔하고 그건 **액션이 아니라 수명
속성**(`once`)이다. ②는 `run` 필드의 `kill <pid>` 로 흡수. ③은 필요 없다.
명시적 취소는 `mode="delete"`.

`once=True` 가 **기본값**이어야 한다. 반대로 하면 깜빡 잊은 모니터가 계속 깨운다 —
wake 폭주가 실수로 열리는 가장 흔한 경로다. (단 은퇴도 **보고한다** — §7.1.)

### 4.1 `deadline` 은 필수가 아니라 기본값이다 (2판 정정)

초판은 필수였다(§7.1: "안 끝나는 모니터 = 안 끝나는 세션"). 그 걱정은 맞지만
수단이 틀렸다 — **24h clamp 가 이미 그 걱정을 해소한다.** §10.1 이 값이 클 때
거부 대신 clamp 를 택한 근거("모델이 얼마가 맞는지 탐색하느라 턴을 태운다")는
**누락에 더 강하게 적용된다**: 35B 는 필수 파라미터를 상시 빠뜨린다.

→ `deadline` 기본 `"2h"`, 60s~24h clamp. 빠뜨려도 등록이 성공한다.

### 4.2 조건 (v1: **3종**)

| `type` | 파라미터 | 비고 |
|---|---|---|
| `match` | `file`, `pattern` | 새 줄만. 바이트 오프셋 커서, **등록 시점에 EOF 에서 시작** |
| `silence` | `file`, `seconds` | **침묵은 성공이 아니다** — 죽은 스크립트 탐지 |
| `command` | `command`, `every` | **주기 실행**. exit 0 이면 발화, stdout 이 보고 본문 |

`silence` 근거: 로그만 보는 감시는 스크립트가 죽어 조용해지면 영원히 기다린다.
v8.55.0 ProgressClock 무진전 워치독과 같은 개념이라 내부 일관성도 있다.

**삭제된 둘** (2판):

- **`exit` — 구현 불가.** `shell` 은 `subprocess.run(cmd, shell=True, ...)` 이고
  **`start_new_session` 이 없다**(`shell.py:220-226`). `&` 로 띄운 자식은 `sh` 가
  끝나면 launchd/init 로 재부모화되므로, 부모가 아닌 우리 폴링 스레드는
  `waitpid` 를 못 한다 — ESRCH(생존 여부)만 보이고 그마저 **PID 재사용 레이스**가
  있다. "종료 코드 포함"은 명세부터 불가능했다. 도구 설명이 관용구를 대신
  나른다: `( cmd; echo "EXIT:$?" ) > log 2>&1 &` + `match "^EXIT:"`.
- **`interval` — 주기형 `command` 가 상위집합이다.** `interval` 은
  `command:"true"` 에 해당하는 sugar 인데, `command("tail -50 /tmp/out.log",
  every=300)` 은 같은 주기로 돌면서 **내용까지 보고문에 실어 온다**(`interval`
  은 "시간이 됐다"만 알려 모델이 로그를 다시 읽어야 한다 — 턴 하나가 더 든다).
  겹침을 감수하기로 했던 초판 결정(§5)은 board 의 `schedule` 과의 겹침 얘기였고,
  그건 그대로 유효하다. 여기서 빼는 이유는 겹침이 아니라 **상위집합의 존재**다.

### 4.3 `command` 는 왜 스트리밍이 아니라 주기형인가

초판은 "stdout 한 줄 = 매치 1건" 인 장수 프로세스였다. 그러면
`grep --line-buffered` / `awk fflush()` / `stdbuf` 를 정확히 써야 하고,
**한 단이라도 버퍼링하면 매치가 갇혀 조용히 아무 알림도 안 온다** — §3.2 가
"35B 는 못 한다"고 판단해 선언형을 고른 바로 그 실패 모드다. **탈출구가
선언형이 피하려던 함정을 다시 들여왔다.**

주기형은 프로세스가 **끝나므로 구조적으로 플러시된다.** 지시가 아니라 구조로
함정이 사라진다. 덤으로:

- 전용 stdout 리더 스레드가 없어진다 (폴링 루프 하나로 통일)
- 소유해야 할 장수 프로세스가 없다 (delete·deadline·세션 종료·Ctrl+C 때의
  kill, 프로세스 그룹 관리가 전부 불필요)
- resume 이 성립한다 (돌고 있는 프로세스를 되살릴 방법은 없다)

각 실행은 유계 타임아웃을 갖는다. 스트리밍이 필요하면 그건 셸이 할 일이고,
`nohup ... > log &` + `match` 가 그 길이다.

### 4.4 실행 관용구 — 도구 설명은 **산출물**이다

35B 대상에서 도구 설명은 산문이 아니라 제품이다. 다음을 **설명에 박고 테스트로
고정**한다(§9 커밋 2):

```
백그라운드 실행:  nohup CMD > /tmp/x.log 2>&1 & echo $!
                 ^^^ 2>&1 없으면 shell 도구가 120s 막힌다
버퍼링 해제:      python -u / PYTHONUNBUFFERED=1 / stdbuf -oL
종료 코드:        ( CMD; echo "EXIT:$?" ) > /tmp/x.log 2>&1 &
board 세션이면:   주기 보고는 `schedule` 이 더 적합 (세션이 죽어도 산다)
```

## 5. 확장 지점

`WireFormat`·`Tool`·`render/<name>.py` 와 같은 방식 — `type` 키로 찾는 작은
레지스트리 **하나**, 클래스 하나. **조건 추가 = 클래스 하나, 소비 지점 0.**

```python
class Condition(ABC):
    type: str
    def check(self, st: dict) -> Match | None: ...   # st = 모니터별 상태(오프셋 등)
```

액션 레지스트리는 없다 (§4 — 알림은 암묵, `run` 은 필드 하나).

## 6. 핵심 흐름

```
① 등록   monitor(add, ...) → MonitorRegistry.add() → monitors.json (기록만)
② 평가   폴링 스레드(1s)가 조건 check() — 턴이 도는 중에도 계속 (§2-④)
③ 발화   매치 → (run 이 있으면 실행) → MonitorRegistry._pending 에 보고 적재
④ 깨우기 main 이 idle 이면 MailWaker 가 큐에 WAKE_TEXT — **합치기 무료**
⑤ 배달   턴 경계에서 drain() → tool="monitor" **관찰 레코드**로 주입
⑥ 해제   once / deadline / max_wakes — **어느 경로든 마지막 보고를 남긴다**
```

### 6.1 배달은 큐가 아니라 메일박스로 (2판 — 가장 큰 변경)

초판 §6-④ 는 `notify → enqueue(보고문)` 이었고 "새 경로가 아니다"라고 했다.
**틀렸다** — §2-② 가 보인 대로 큐는 마커만 나른다. 보고문을 큐에 태우면 넷이
따라온다(전부 코드로 확인):

1. **보고가 사람 메시지로 위장된다.** `_inject_queued_messages` 가
   `renderer.push_user_message(labeled, ...)` 로 사용자 카드를 그리고
   `QUEUED_REQUEST_NOTICE`("Another **user request** arrived…")를 앞에 붙인다.
   `run` 에선 `run_loop(query=report)` → `{"role":"user"}` 레코드가 되어
   history·resume 재생·검색 표면에서 사람 턴과 구별되지 않는다. "새 렌더 표면
   없음"은 맞지만 **사용자를 사칭해서** 맞는 것이다.
2. **run 과 web 의 배달 시점이 다르다.** `_inject_queued_messages` 는
   `dequeue_user_message is None` 이면 즉시 반환하는데(`loop/core.py:658`),
   `run` 의 `run_loop(...)` 호출에는 **그 인자가 없다**(`main.py:1520-1528`).
   20턴짜리 런 도중 발화하면 **런이 끝난 뒤에야** 배달된다 — 모델이 이미 성공
   보고를 한 뒤일 수 있다. §2-③ 의 "배달 코드 한 벌"은 idle 경우에만 성립한다.
3. **보고 N개 → 런 N개.** `MailWaker._armed` 합치기는 자기 `enqueue` 엔 안 붙는다.
4. **`--result-file` 이 틀린 답을 쓴다.** `answer` 가 성공한 마지막 `run_loop`
   로 덮이고(`main.py:1529-1530`) 펌프 종료 후 기록되므로, 본 작업 뒤 모니터가
   발화하면 **모니터 보고에 대한 응답**이 결과 파일에 들어간다.

**대신 메일박스를 쓴다.** 그 채널은 이미 회신 전용이 아니다 — `kind:"died"`
통지처럼 회신이 아닌 것도 나른다(`agents_live.py:195-212`).

```python
# runtime.py — 술어 합성 (기존 배선에 or 하나)
waker = MailWaker(enqueue_wake,
                  lambda: registry.has_pending_replies() or monitors.has_pending())

# loop/core.py — 턴 경계, _deliver_agent_mail 옆에 형제로
{"role": "user", "tool": "monitor", "success": True, "content": report}
```

이 레코드는 **아무 데도 등록이 필요 없다**:

| 확인한 것 | 결과 |
|---|---|
| `records.py:82-84` | `"tool" in message` → 관찰로 취급 |
| `records.py:129-133` | `tool == ""` 일 때만 형식-개입 → `"monitor"` 안전 |
| `app.js:984` | `tool === "agent"` 만 특수 처리 → 일반 경로 |

`_deliver_agent_mail` docstring 이 *"`tool=""` 는 형식-개입 마커라 금지"* 라고
적어 둔 그 관례를 그대로 따른다. 얻는 것: 정확한 귀속 · run/web 대칭 배달 ·
`_armed` 합치기 무료 · `on_run_end` 레이스 봉합 · `wake=True` 의
"🤝 이어서 진행" 표시까지.

**부수 효과 하나 더**: 큐를 안 쓰므로 web 의 `route_one`(`main.py:2375-2400`)을
타지 않는다 — `/` 나 `@` 로 시작하는 보고가 슬래시/에이전트 명령으로 **파싱되는
사고**가 구조적으로 불가능해진다.

## 7. 가드 셋 — 여기가 실제 난이도

### 7.1 수명 — 그리고 **모든 종료는 보고한다**

배선은 기존 계약을 건드리지 않고 **파라미터 추가**로만, **두 곳에**:

```python
# ① run 펌프 (main.py:1581~)
_run_message_pump(input_queue, waker, registry, run_one, *, monitors=None, ...)
#   registry.has_active_work() or (monitors and monitors.has_active_work())

# ② web 자기수확 (main.py:1566) — §2-⑤ 정정으로 새로 추가된 곳
web_instance_is_active(renderer, server, agent_registry, monitors)
```

`run` 만 고치면 board 인스턴스가 모니터를 데리고 조용히 사라진다.

**해제 경로 셋이 전부 마지막 보고를 남긴다** (2판 — 초판은 여기가 비대칭이었다).
초판 §7.2 는 `max_wakes` 소진에 대해 "조용히 멎지 말고 마지막으로 한 번 알리고
해제"라고 하면서, §7.1 의 `deadline` 만료는 조용히 끝나게 뒀다. `run` 에선 펌프가
그냥 종료되고 세션이 아무 말 없이 끝난다. 같은 규칙을 셋 다에:

| 해제 | 마지막 보고 |
|---|---|
| `once` 발화 | "… (모니터 은퇴)" |
| `deadline` 만료 | "만료 — 매치 N건, 마지막 …" |
| `max_wakes` 소진 | "상한 도달 — 해제됨" |

### 7.2 폭주

- `min_interval`(기본 30s) 안에 쌓인 매치는 **한 보고로 합쳐서** 보낸다
- `max_wakes`(기본 20) 소진 시 마지막 보고 후 해제 (위 표)

메일박스 경로를 쓰므로 `MailWaker._armed` 합치기가 **공짜로 얹힌다** — 여러
모니터가 동시에 발화해도 wake 는 하나, 보고는 턴 경계에서 한꺼번에.

### 7.3 보안 — `shell.py` 와 **같은 정책**을, 다른 시점에

`command` 조건과 `run` 필드는 임의 명령을 돌린다. **발화 시점이 아니라 등록
시점에 확인받는다** — 사람은 에이전트가 monitor 를 거는 순간엔 있지만 새벽
3시 발화 때는 없다.

이 시점 선택에는 초판이 못 본 더 강한 근거가 있다: **폴링 스레드에서
`renderer.confirm` 을 부르면 안 된다.** `interactive_lock`(`render/base.py`)이
모든 사용자 읽기를 직렬화하는데, 턴이 도는 중에 백그라운드 스레드가 그 락을
잡으면 교착이다. 등록 시점 확인은 그 호출이 **메인 스레드에서만** 일어남을
보장한다.

**초판의 "`shell.py` 와 똑같이 거부"는 실제로 같지 않았다** (2판 정정). shell
확인은 위험 **키워드 게이트**(`rm`/`rmdir`/`mv`, `shell.py:20`)이고
`AGENT_CLI_DANGEROUS_SHELL_CONFIRM=0` 으로 우회된다(`shell.py:27-30`).
모니터가 **모든** 명령에 확인을 요구하면 harbor/CI(`can_prompt()` False, env
없음)에서 `command` 가 아예 못 쓰인다 — **그 headless 환경이 §3.1 에서
monitor 를 만드는 근거였다.** 자기모순이다.

→ 등록 시점에 **같은 함수들을 재사용**한다: `_detect_dangerous` + `_confine.guard`
+ 같은 env 우회. 정책은 동일, 시점만 다르다. `command`/`run` 없는 모니터는
무확인 통과 — 새 권한이 아니다.

## 8. 영속 — 기록하되 **부활시키지 않는다** (2판)

초판은 `--resume` 시 복원 + 재검증(PID 살아 있나 / 파일 있나 / deadline
안 지났나)이었다. 잘라낸다. 이유 넷:

1. **희소하다.** `deadline` 상한이 24h 이고 `once=True` 가 기본이라, resume
   시점에 살아 있을 모니터가 애초에 적다.
2. **커서가 낡는다.** `match` 의 바이트 오프셋은 중단 기간 동안 무의미해진다 —
   되감으면 옛 매치를 다시 보고하고, 건너뛰면 그동안의 매치를 잃는다.
   **둘 다 틀렸고 어느 쪽이 맞는지 알 방법이 없다.**
3. **권한 구멍.** `sessions_dir()` 기본이 `./.agent-cli/sessions`(`paths.py:55`)
   — **워크스페이스 안**이라 에이전트가 `write_file` 로 `monitors.json` 을 고칠
   수 있다. 되살리면 **아무도 승인하지 않은 `command`/`run` 이 실행된다.**
   파일 안의 `confirmed: true` 플래그는 아무것도 증명하지 못한다(§7.3 의 확인이
   "이 프로세스에서 사람이 답했다"를 뜻하는데, 디스크는 그걸 못 나른다).
4. 재검증 매트릭스(PID×파일×deadline×확인)가 통째로 사라진다.

**그래서**: `session_dir/monitors.json` 에 쓰기만 하고, resume 때는 한 줄 알린다.

```
이전 세션의 모니터 2건은 복원되지 않았습니다 — 필요하면 다시 등록하세요:
  [mon-1] match /tmp/build.log "ERROR|FAILED"
  [mon-3] command "gh run list --limit 1 --json conclusion" every 300s
```

다섯 줄이고, 재검증도 보안 표면도 없다. 사용자가 다시 걸면 **확인도 다시
받는다** — 그게 정직한 상태다.

## 9. 실행 계획

커밋 셋으로 나눈다 — 한 덩어리면 리뷰가 어렵고 §7.1 펌프 변경이 묻힌다.

### 0단계 — 선행 (커밋 1 앞)

`constants.parse_duration()` 추출 + `_parse_stall` 을 그 위로 올리는 것은
**monitor 와 독립**이다. 먼저 따로 내면 monitor 커밋이 그만큼 작아지고,
`--stall` 회귀가 monitor 리뷰에 섞이지 않는다.

### 커밋 1 — 코어 (배선 없음, 제품 동작 불변)

`MonitorRegistry` + 조건 3종 + 폴링 스레드 + `_pending`/`drain()`/`has_pending()`
/`has_active_work()` + 단위 테스트.

### 커밋 2 — 배선

도구 등록 + run/web 조립 + **수명 두 곳**(§7.1 펌프 + `web_instance_is_active`)
+ 턴 경계 drain(§6.1) + 통합 테스트.

레지스트리를 루프에 닿게 하는 경로가 둘이다 — `cfg.agent_registry`
(`tool_bridge.py:384`)와 모듈 전역(`set_main_registry`, `agents_live.py:117`).
§6.1 의 턴 경계 drain 때문에 **어차피 `LoopConfig` 에 필드를 하나 통과시켜야
하므로**, 도구 접근도 같은 필드를 쓴다(두 경로를 새로 만들지 않는다).

**도구 설명(§4.4)은 이 커밋의 산출물이다.** 35B 대상에서 그건 산문이 아니라
제품이라, `2>&1` 관용구·버퍼링 해제·`EXIT:$?` 관용구가 설명에 있는지를
테스트로 고정한다.

### 커밋 3 — 영속

`monitors.json` 쓰기 + resume 시 "복원 안 됨" 알림 (§8). 부활 없음.

### 테스트 계획

| 층 | 내용 |
|---|---|
| 조건 | 타입별 매치/미매치, 오프셋 커서 전진, **등록 시 EOF 에서 시작**, 파일 축소 리셋, **`st_ino` 변화(로그로테이트 rename) 감지**, `silence` 를 `max(mtime, registered_at)` 기준으로, **없는 파일 = 대기(죽음 아님)** |
| 주기 command | exit 0 발화 / 비-0 무시, stdout 이 보고 본문, 유계 타임아웃, `every` clamp |
| 수명 | deadline 기본 2h·clamp, 만료 자동 해제, `once` 기본값, **해제 3경로가 전부 마지막 보고를 남기는가** |
| 폭주 | min_interval 합치기, max_wakes 소진 시 마지막 알림 후 해제 |
| 보안 | `command`/`run` 등록 시 `_detect_dangerous`+`_confine.guard` 경유, **env 우회가 shell 과 같은가**, `notify` 만이면 무확인, **폴링 스레드가 confirm 을 부르지 않는가** |
| 배달 | `tool="monitor"` 관찰 레코드 형태, run·web **양쪽에서 턴 경계 배달**, 여러 모니터 → wake 하나 |
| 펌프 | 모니터 살아 있으면 run 이 안 끝남 / 해제되면 끝남 ← **회귀 위험 1순위** |
| web 수명 | 모니터 살아 있으면 `--idle-timeout` 이 자기수확하지 않음 ← **§2-⑤ 정정분** |
| 영속 | 저장되는가, resume 때 **되살아나지 않고** 알림만 나오는가 |
| 파서 | `parse_duration` 문법 3종 + 오류가 **표면별로** 변환되는가 (CLI=BadParameter / 도구=ToolResult) |
| clamp | deadline·every 가 상·하한으로 **잘리고 그 값이 반환되는가**(거부 아님 — §10.1) |
| 보고문 | 5줄 상한 + "… N건 더" · 줄당 500자 절단 · 머리줄에 id·타입·경과 |
| 도구 설명 | `2>&1`·버퍼링·`EXIT:$?` 관용구가 설명에 있는가 (§4.4) |

### 파일

새로: `agent_cli/monitor/{__init__,registry,conditions}.py`,
`agent_cli/tools/monitor_tool.py`
(`actions.py` 없음 — §4 에서 액션 레지스트리를 삭제했다)

수정:
- `tools/registry.py` — 도구 등록. **`_ALL_TOOLS` 끝에** 붙인다(KV 캐시 순서 보존)
- `runtime.py` — `build_monitor_registry` + `MailWaker` 술어 합성(§6.1)
- `loop/core.py` — 턴 경계 drain (`_deliver_agent_mail` 형제)
- `main.py` — run·web 조립 + 펌프 정지 판정 + `web_instance_is_active`
- `constants.py` — `parse_duration()` + clamp 상수 (§10)

### 감사(docs/audit)에서 온 제약 — 착수 전 확인

**v1 은 새 표면이 필요 없다.** 보고가 `tool="monitor"` 관찰 레코드로 들어가
기존 관찰 렌더 경로를 그대로 타므로(§6.1) 새 SSE 이벤트도 새 카드도 없다 —
`test_every_emitted_event_has_a_listener` · `REPLAY_CONTRACT` · `classify()` ·
CSS 규칙 가드가 **전부 무관**하다. 그게 메일박스 경로를 고른 부수 이득이다.

(만약 나중에 전용 카드를 주게 되면 그때 위 넷을 전부 통과해야 한다.)

## 10. ~~열린 질문~~ — 해소 (2026-09-19)

넷 다 **코드에 선례가 있었다.** 새로 정하기보다 있는 규칙을 따른다.

### 10.1 `deadline` 상한 — clamp 하되 거부하지 않는다

선례: `context/manager.py:93`

```python
return max(STREAM_IDLE_TIMEOUT_MIN_S, min(s, STREAM_IDLE_TIMEOUT_MAX_S))
```

같은 모양으로 `constants.py` 에 둘을 더한다:

```python
MONITOR_DEADLINE_MAX_S = 86400   # 24h — 그 이상은 조용히 자른다
MONITOR_DEADLINE_MIN_S = 60      # 1m
```

**거부가 아니라 clamp 인 이유**: 값이 크다고 실패시키면 모델이 "얼마가 맞는지"를
탐색하느라 턴을 태운다. clamp 는 결과를 돌려주므로 한 번에 끝난다 —
`/api/stall` 이 clamp 결과를 되반영하는 것과 같은 판단이다.

**2판**: 같은 논리를 한 걸음 더 밀어 `deadline` 을 **필수에서 기본값(2h)으로**
내렸다(§4.1). 초판은 "필수 + clamp" 였는데, *누락*이야말로 35B 가 가장 자주
하는 실수라 거부의 비용이 가장 큰 자리였다.

24h 근거: 감시 대상은 "아주 오래 도는 스크립트"(§1)다. 하루를 넘기면 그건
세션이 아니라 배치 작업이고, board 의 `schedule` 이 맞는 도구다.

### 10.2 주기 `command` 의 `every` 하한 — 60s

`MONITOR_INTERVAL_MIN_S = 60`, 같은 방식으로 clamp.
(2판: `interval` 조건이 사라지고 주기형 `command` 의 `every` 가 그 자리를
물려받았다 — §4.2. 상수와 근거는 그대로다.)

근거: 주기 실행은 **변화가 없어도 매 틱 한 턴을 태운다**(§3.1 이 cron 을
기각한 바로 그 이유). 35B 급 한 턴이 수십 초 걸리는 환경에서 60s 미만은
"감시"가 아니라 사실상 연속 실행이다. 더 촘촘한 감시가 필요하면 `match` 나
`silence` 를 쓰는 게 맞다 — **그쪽은 변화가 있을 때만 깨운다.**

### 10.3 보고문 — 자체 상한, 과대 캡에 의존하지 않는다

```
🔔 monitor mon-3 (match · /tmp/out.log)   2h 14m 경과 · 3건
  ERROR: connection refused (attempt 4)
  ERROR: giving up after 5 attempts
  … 1건 더
```

규칙 셋:

- **매치 줄 최대 5개 + "… N건 더"**. `min_interval` 합치기(§7.2)가 이미 여러
  건을 한 보고로 묶으므로 상한이 없으면 로그 한 뭉치가 그대로 들어온다.
- **줄당 500자로 자른다.** 감시 대상은 로그이고 한 줄이 길 수 있다.
- **모니터 id·조건 타입·경과 시간을 머리줄에** — 모니터가 여럿일 때 어느
  것인지 알아야 하고, 경과 시간은 `deadline` 이 얼마 안 남았는지의 단서다.

**과대 출력 캡(`apply_oversized_cap`)은 쓰지 않는다.** 그건 *도구 결과*를 위한
장치다(`context_window/10` 초과분을 파일로 빼고 발췌 + 복구 경로 제시,
`tools/base.py:507`). 모니터 보고는 도구 결과가 아니라 **합성 사용자 입력**
(`enqueue`)이라 그 경로를 안 탄다. 자체 상한이 훨씬 작으므로(5줄 × 500자 ≈ 2.5KB)
캡에 닿을 일도 없다 — **닿는다면 그건 상한이 고장난 것**이다.

### 10.4 duration 파서 — `constants.py` 로 추출

`main.py::_parse_stall` 과 **같은 문법**(`"600"` · `"10m"` · `"2h"`)이 필요한데,
그 함수는 `typer.BadParameter` 를 던져 도구에서 그대로 못 쓴다(도구는
`ToolResult(False, error=…)` 로 돌려줘야 한다).

**순수 파서를 `constants.py` 에 두고 양쪽이 쓴다:**

```python
def parse_duration(raw: str) -> int:
    """"600"·"10m"·"2h" → 초. 형식 오류면 ValueError."""
```

- `main._parse_stall` — 잡아서 `typer.BadParameter` 로 변환 (CLI 표면 유지)
- `monitor_tool` — 잡아서 `ToolResult(False, error=…)` 로 변환

`"h"` 를 추가하는 것이 유일한 문법 확장이다(`deadline="2h"` 가 §4 의 예시에
이미 쓰였다). `--stall` 쪽엔 무해하다 — 시간 단위 stall 은 의미가 없지만
받아서 나쁠 것도 없고, 문법이 갈리는 게 더 나쁘다.

**이건 감사(docs/audit)의 교훈이 그대로 적용되는 자리다**: 같은 문법을 두 번
구현하면 둘이 갈라지고, 갈라진 걸 아무도 모른다. 오늘만 이스케이퍼 3종과
`el()` 2종을 그 이유로 정리했다.

## 11. 2판 개정 경위 (2026-09-20)

초판을 확정하고 구현 직전에 **외부 리뷰**를 받았다. 리뷰가 제기한 것 중
**코드로 확인된 것만** 반영했고, 확인 과정에서 초판의 전제 셋이 뒤집혔다.

| 주장 | 확인 방법 | 결과 |
|---|---|---|
| `MailWaker` 는 내용이 아니라 마커를 나른다 | `agents_live.py:1604-1607` 읽기 | **사실** → §6.1 (배달 경로 전면 교체) |
| `run` 은 턴 중 큐 주입을 안 한다 | `loop/core.py:658` + `main.py:1520-1528` | **사실** → 배달 비대칭 |
| `--result-file` 이 덮인다 | `main.py:1529-1530` | **사실** |
| `exit` 는 종료 코드를 못 얻는다 | `shell.py:220-226` 에 `start_new_session` 없음 | **사실** → 조건 삭제 |
| web 도 자기수확한다 | `main.py:1566-1579` 가 모니터를 모름 | **사실** → §2-⑤ 정정 |
| 한쪽만 리다이렉션하면 막힌다 | **실측**: `> log &` → TIMEOUT, `> log 2>&1 &` → 0.01s | **사실** → §4.4 |
| shell 확인은 키워드 게이트 + env 우회 | `shell.py:20`, `shell.py:27-30` | **사실** → §7.3 자기모순 해소 |
| 세션 디렉터리가 워크스페이스 안 | `paths.py:55` | **사실** → §8 부활 삭제 |

**사용자 판단으로 뒤집은 것 하나**: 리뷰는 `interval` 을 "cron 의 단점을 그대로
들여왔다"며 보류하자고 했다. 초판의 "board `schedule` 과 겹쳐도 허용" 결정은
그대로 유효하다고 보고, 대신 **주기형 `command` 가 `interval` 의 상위집합**이라는
점(내용까지 실어 온다 → 턴 하나 절약)을 근거로 흡수시켰다. 겹침이 아니라
상위집합이 삭제 근거다.

### 이 리뷰에서 배운 것

초판이 틀린 세 곳은 **전부 "이미 있는 것을 재사용한다"고 적은 자리**였다 —
`MailWaker` 재사용 · `shell.py` 와 동일한 거부 · web 은 수명 문제가 없음.
**재사용 주장은 그 대상을 직접 읽어 확인하기 전까지 가설이다.** 초판은 셋 다
이름만 보고 성질을 추정했다. v9.9.x 의 `~` 구멍도 같은 모양이었다(추출기는
`~/` 를 후보로 잡는데 해소기가 확장을 안 함 — 두 함수 사이에서만 보였다).
