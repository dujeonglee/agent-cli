# Monitor — 조건 → 액션 감시 도구 설계

> 상태: **검토 보류** (2026-09-17 사용자와 공동 설계, 구현 전)
> 결정된 것과 열린 것을 구분해 적는다. 재개 시 §9 부터 읽으면 된다.

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
shell("nohup long.sh > /tmp/out.log 2>&1 &")   # 0.00s 반환, 자식은 계속
```

실측 확인함. 사용자 원안의 "파일 리다이렉션"이 바로 이걸 가능하게 하는 조건이다.

**② 깨우기 메커니즘이 이미 있다 — `MailWaker` + `InputQueue`.**

teammate 회신용으로 만든 것. main 이 idle 이면 합성 아이템을 입력 큐에 넣어
깨운다. `_armed` 플래그로 "여러 mail → wake 하나" 합치기까지 되어 있다.
(`agents_live.py::MailWaker`)

**③ `run` 도 web 과 같은 펌프를 쓴다 — `_run_message_pump` (main.py).**

정지 판정은 "큐 비었고 `registry.has_active_work()` False". `enqueue(conn_id,
text)` 시그니처가 run(`InputQueue.enqueue`)·web(`server.enqueue`) 동일이라
**배달 코드 한 벌이 양쪽에 붙는다.**

**④ 펌프는 조건 평가 지점이 될 수 없다.**

`run_one()` 을 **동기로** 부르므로 턴이 도는 몇 분 동안 루프가 멈춰 있다. 조건
평가는 **전용 스레드**여야 한다 — `AgentRegistry` 가 이미 같은 모양(스레드 소유 +
`has_active_work()`)이라 형제로 붙인다.

**⑤ web 은 무한 대기다.** 정지 판정이 없으므로 수명 문제는 `run` 에만 해당.

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
        then=[{"type": "notify"}],     # 기본값
        deadline="2h",                 # 필수
        once=True)                     # 기본값
monitor(mode="list")
monitor(mode="delete", id="mon-3")
```

`add`/`delete`/`list` 세 모드는 **`ScheduleTool` 과 같은 어휘**를 의도적으로 맞춘다.

**`when` 은 하나만.** AND/OR 합성을 넣지 않는다 — 규칙 엔진이 비대해지는 지점이
정확히 거기고, 조건 둘이 필요하면 monitor 둘을 걸면 된다.

**`then` 은 리스트.** "알리고 + 무언가" 가 자연스럽다.

**`stop` 은 액션이 아니다.** 초안엔 액션으로 뒀으나 오해를 부른다("무엇을 stop
하나?"). 셋을 구분해야 한다: ①모니터 자신 ②감시 대상 프로세스 ③에이전트 런.
①이 압도적으로 흔하고 그건 **액션이 아니라 수명 속성**(`once`)이다. ②는 `shell`
액션의 `kill <pid>` 로 흡수. ③은 필요 없다. 명시적 취소는 `mode="delete"`.

`once=True` 가 **기본값**이어야 한다. 반대로 하면 깜빡 잊은 모니터가 계속 깨운다 —
wake 폭주가 실수로 열리는 가장 흔한 경로다.

## 5. 확장 지점

`WireFormat`·`Tool`·`render/<name>.py` 와 같은 방식 — `type` 키로 찾는 작은
레지스트리 둘, 각각 클래스 하나. **조건 추가 = 클래스 하나, 소비 지점 0.**

```python
class Condition(ABC):
    type: str
    def check(self, st: dict) -> Match | None: ...   # st = 모니터별 상태(오프셋 등)

class Action(ABC):
    type: str
    def fire(self, match: Match, mon: Monitor) -> None: ...
```

### 조건 (v1: 5종)

| `type` | 파라미터 | 비고 |
|---|---|---|
| `match` | `file`, `pattern` | 새 줄만 검사. 바이트 오프셋 커서, 파일 축소 시 리셋 |
| `silence` | `file`, `seconds` | **침묵은 성공이 아니다** — 죽은 스크립트 탐지 |
| `exit` | `pid` | 종료 코드 포함 |
| `interval` | `seconds` | headless 용 주기 보고 |
| `command` | `command` | 탈출구. stdout 한 줄 = 매치 1건 (§3.2) |

`silence` 를 넣는 근거: 로그만 보는 감시는 스크립트가 죽어 조용해지면 영원히
기다린다. Claude Monitor 도구 설명에도 같은 제목의 섹션이 있다("이 프로세스가
지금 죽었다면 내 필터가 뭐라도 내보낼까?"). v8.55.0 의 ProgressClock 무진전
워치독과 같은 개념이라 내부 일관성도 있다.

`interval` 은 board 에서 `schedule` 과 기능이 겹친다 — **겹쳐도 허용하기로 결정**
(사용자). 대신 도구 설명에 "board 세션이면 `schedule` 이 더 적합" 한 줄을 넣어
모델이 헷갈리지 않게 한다.

### 액션 (v1: 2종)

- `notify` — 등록한 LLM 깨우기 (기본)
- `shell` — 명령 실행 (§7 보안 게이트 적용)

## 6. 핵심 흐름

```
① 등록   monitor(add, ...) → MonitorRegistry.add() → monitors.json
② 평가   폴링 스레드(1s)가 조건 check() — 턴이 도는 중에도 계속 (§2-④)
③ 발화   매치 → Action.fire()
④ 배달   notify → enqueue(None, 보고문) → 펌프가 깨어나 턴 실행
⑤ 해제   once=True 면 자동, 아니면 deadline·budget 소진까지
```

④ 는 **새 경로가 아니다** — `MailWaker` 가 이미 같은 큐에 합성 아이템을 넣는다.

스레드는 `command` 만 전용(stdout 리더), 나머지 넷은 공용 폴링 스레드 하나로
충분하다 — 전부 값싼 파일·PID 검사.

## 7. 가드 셋 — 여기가 실제 난이도

### 7.1 수명

`deadline` **필수. 없으면 등록 거부.**

`run` 펌프의 정지 판정에 모니터를 합류시키는 이상, 안 끝나는 모니터 = 안 끝나는
세션이다. 코드에 이미 흉터가 있다 — `has_active_work()` 가 `waiting_ask` 를
**의도적으로 제외**하며 남긴 주석: *"main 이 답하지 않기로 한 질문을 기다리는 건
영원히 안 끝나는 교착이라, 펌프는 경고 후 종료를 택한다."*

배선은 기존 계약을 건드리지 않고 **파라미터 추가**로만:

```python
_run_message_pump(input_queue, waker, registry, run_one, *, monitors=None, ...)
#   registry.has_active_work() or (monitors and monitors.has_active_work())
```

### 7.2 폭주

- `min_interval`(기본 30s) 안에 쌓인 매치는 **한 보고로 합쳐서** 보낸다
  (`MailWaker._armed` 합치기와 같은 패턴)
- `max_wakes`(기본 20) 소진 시 **조용히 멎지 말고 마지막으로 한 번 알리고 해제**

Claude Monitor 도구에도 "이벤트를 너무 많이 내는 모니터는 자동 정지" 규칙이 있다.

### 7.3 보안

`command` 조건과 `shell` 액션은 임의 명령을 돌린다. **발화 시점이 아니라 등록
시점에 확인받아 봉인한다** — 사람은 에이전트가 monitor 를 거는 순간엔 있지만
새벽 3시 발화 때는 없다.

`can_prompt()` 가 False 면 `shell.py` 와 **똑같이 거부**한다(헤드리스 일관성;
`shell.py` 가 이미 "interface can't prompt for confirmation right now" 로 거부).

`notify` 만 쓰는 모니터는 무확인 통과 — 새 권한이 아니다. 애초에 `shell` 도구로
`nohup script &` 를 돌릴 수 있으므로 백그라운드 셸 자체가 새 능력은 아니다.

## 8. 영속

`session_dir/monitors.json`, `agents.json` 방식 그대로. `--resume` 시 복원하되
**되살리기 전 재검증**: PID 살아 있나 / 파일 있나 / deadline 안 지났나. 죽은 건
버리고 몇 개 버렸는지 알린다.

## 9. 실행 계획

커밋 셋으로 나눈다 — 한 덩어리면 리뷰가 어렵고 §7.1 펌프 변경이 묻힌다.

1. **코어** — registry·conditions·actions + 단위 테스트 (배선 없음, 제품 동작 불변)
2. **배선** — 도구 등록 + run/web 조립 + 펌프 정지 판정 + 통합 테스트
3. **영속** — monitors.json + resume 복원

### 테스트 계획

| 층 | 내용 |
|---|---|
| 조건 | 타입별 매치/미매치, 오프셋 커서 전진, 파일 축소 리셋, silence 경계 |
| 수명 | deadline 없으면 등록 거부, 만료 자동 해제, `once` 기본값 |
| 폭주 | min_interval 합치기, max_wakes 소진 시 **마지막 알림 후** 해제 |
| 보안 | `command`/`shell` 등록 시 확인 요구, `can_prompt()` False 거부, `notify` 무확인 |
| 배달 | enqueue 호출 형태, run·web 양쪽 동일 경로 |
| 펌프 | 모니터 살아 있으면 run 이 안 끝남 / 해제되면 끝남 ← **회귀 위험 1순위** |
| 영속 | 저장·복원·죽은 모니터 폐기 |

### 파일

새로: `agent_cli/monitor/{__init__,registry,conditions,actions}.py`,
`agent_cli/tools/monitor_tool.py`

수정: `tools/registry.py`(도구 등록 — `_ALL_TOOLS` 끝에, KV 캐시 순서 보존),
`runtime.py`(`build_monitor_registry` — `build_agent_registry` 형제),
`main.py`(run·web 조립 + 펌프)

## 10. 열린 질문

- **`deadline` 기본 상한** — 사용자가 `"24h"` 를 주면 그대로 받을지, 하드 상한을
  둘지. `STREAM_IDLE_TIMEOUT_MAX_S`(3600) 같은 clamp 선례가 있다.
- **`interval` 최소값** — 너무 짧으면 턴 폭주. 60s 하한이 적당해 보이나 미정.
- **보고문 형식** — 매치 줄을 얼마나 실을지(상한), 모니터 라벨·경과 시간을 함께
  줄지. 과대 출력 캡(`apply_oversized_cap`) 과의 관계도 미정.
- **duration 파서 공유** — `main.py::_parse_stall`("600"·"10m"·"0")과 같은 문법이
  필요하다. 공용 헬퍼로 추출할지, monitor 쪽에 따로 둘지(현재 `_parse_stall` 은
  typer 예외를 던져 도구에서 그대로 쓰기 어렵다).
