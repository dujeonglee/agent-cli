# 감사자(auditor) — main 의 complete 마다 결과를 감사하고 되먹인다

> 상태: **구현됨 (v10.42.0, 2026-10-16) — §6 측정 전, 기본값 off**
> 결정(사용자): ① 서수 구간만 읽는다 ② 결과는 사용자에게도 보인다 ③ 트리거는 하니스 코드가 아니라 **기존 구독(`monitor`)** 으로 ④ 전달은 **기존 에이전트 메시지 경로**로 ⑤ 측정 계획 동의.
> 1판과의 차이: 하니스가 브리프를 만들어 감사자에게 넣는 구조를 버렸다. 감사자는 보통 상주 에이전트이고, 자기가 main 의 history 에 `monitor` 를 걸어 `complete` 를 구독한다. 새 카드·새 이벤트·판정 파서가 없다.

## 0. 한 줄 요약

세션 노브로 내장 프로파일 `auditor` 를 상주로 띄운다. 감사자는 시작하자마자 main 세션의 `history.jsonl` 에 `monitor(match)` 를 건다. main 런이 `complete` 로 끝나 종결 레코드가 한 줄 붙으면 모니터가 그 줄을 감사자의 런으로 배달한다. 감사자는 `history` 도구로 **그 런의 서수 구간만** 읽고 요청별 충족/미충족/확인불가를 증거 서수와 함께 판정한다. 미충족이 있으면 `message(to="main")` 로 보낸다 — 기존 에이전트 메시지라 main 채널에 카드로 그려지고 main 이 깨어나 한 번 더 손본다. 통과면 자기 채널에 `complete` 로 남긴다. 감사가 깨운 런은 감사자가 SQL 로 알아보고 건너뛴다.

## 1. 왜

- main 은 자기 보고를 검증하지 않는다. 2026-10-16 압축 핀 실측에서 한 런이 40턴 중 17턴에 멈추고 "턴이 모자라 보고서를 못 썼다"고 했다.
- 지금 있는 완료 시점 검사는 `_require_answers`(미답 요청 id 를 채웠나) 하나다. 기계적이라 "id 는 채웠는데 내용이 답이 아니다"는 못 본다.
- 정적 필터는 주장·관찰 대조만 본다. 요청에 답이 됐는지는 LLM 이 봐야 한다 — 같은 모델이라도 종결문을 **쓰는 자리**와 **대조하는 자리**는 다른 과제다. 값어치는 §6 측정으로 확인한다.

## 2. 목표 / 비목표

목표: 사용자 런마다 complete 를 한 번 감사하고, 미충족이 있으면 main 이 한 번 더 손보게 한다. 감사는 증거 기반이다(충족을 보여주는 레코드 서수를 못 짚으면 미충족/확인불가). 사용자는 결과를 본다.

비목표: 정적 필터 단계. 감사가 main 을 블로킹하는 것. 세션 전체 history 접근. 감사의 감사. 감사자가 직접 고치는 것(쓰기 도구 없음). 감사자가 사용자에게 묻는 것(`ask` 없음). 새 UI.

## 3. 구조

```
main 세션                                       auditor (상주, 같은 세션의 teammate)
  history.jsonl  ◀── complete 레코드 한 줄 ──┐
                                             │  monitor(match, file=<main>/history.jsonl,
                                             │          pattern='"kind": "final"', once=false)
                                             └─▶ 모니터 보고 = 그 줄(종결 레코드 JSON) → 감사자 런
                                                   history: 이 final 의 seq 와 직전 final 의 seq 로
                                                            구간 [a, b] 확정, 구간의 query/action/observation 조회
                                                   read_file / code_index: 현재 워크스페이스 확인
                                                   판정:
                                                     pass    → complete (자기 채널에 남음)
                                                     findings→ message(to="main", AUDIT 본문)
  main 채널 카드 ◀── 기존 에이전트 메시지 ───────────┘
  🤝 wake 런: main 이 관찰로 보고 손본다 → complete → history 에 final 한 줄
                                             └─▶ 모니터가 또 배달 — 감사자는 그 런의 query 레코드가
                                                 깨우기(author = WAKE)인 것을 보고 "skip" 으로 complete
```

### 3.1 프로파일 `auditor` (내장, `agent_cli/agents/builtin/auditor.md`)

- `allowed-tools`: `history`, `monitor`, `read_file`, `code_index`, `memory`. `shell` 은 **넣지 않는다** — 존재·크기 확인은 `read_file`·`code_index` 로 되고, 테스트를 다시 돌리는 식으로 main 의 일을 중복하는 길을 처음부터 막는다(열린 질문 C 해소). 쓰기 도구·`agent`·`ask`·`browser`·`fetch` 없음.
- `auto-spawn` frontmatter 는 쓰지 않는다. 켜고 끄는 것은 부팅 노브 `AGENT_CLI_AUDITOR`(v1 기본 **off**; §6 뒤 결정).
- 역할 프롬프트의 뼈대:
  1. **시작 의식**: 첫 런(스폰 브리프 §3.2)에서 `monitor(add, when={"type":"match","file":"<main history>","pattern":"\"kind\": \"final\""}, once=false, deadline="24h")` 를 건다. 만료 알림이 오면 다시 건다. 모니터는 세션을 넘지 못하므로 resume 뒤 첫 런에서도 같은 일을 한다(프롬프트가 "모니터 목록이 비어 있으면 건다"로 시킨다).
  2. **구간 확정**: 보고로 받은 종결 레코드의 `seq`(= 줄 번호)를 b 로, `history` 에서 `kind='final' AND seq < b` 의 최대 seq + 1 을 a 로 잡는다(없으면 1). 그 런의 요청은 `kind='query' AND seq BETWEEN a AND b`. **a..b 밖은 읽지 않는다.**
  3. **건너뛰기**: 구간의 첫 query 레코드 author 가 깨우기 발신자(`WAKE_AUTHOR`)이거나 query 가 없으면(라우팅·예약·에이전트 보고 런) 감사하지 않고 `complete("skip: wake run")`. 감사 되먹임으로 생긴 런이 여기 들어간다 — 고리 상한.
  4. **판정**: 요청마다 `met`(충족을 보여주는 서수 + 근거 한 줄) / `unmet`(무엇이 빠졌나) / `unverifiable`(종결문의 사실 주장에 구간 안 근거 레코드가 없다 — "테스트 통과" 인데 shell 관찰이 없음). 추측 금지. 요청이 요구하지 않은 것은 미충족이 아니다. 의견("더 잘할 수 있었다")은 쓰지 않는다.
  5. **전달**: unmet/unverifiable 이 하나라도 있으면 `message(to="main", text=<AUDIT 본문>)` 한 번. 전부 met 이면 `complete(<AUDIT 본문>)` 만 — main 에게 보내지 않는다.
  6. `memory` 로 "마지막으로 감사한 final seq" 를 적어 두어 중복 감사(모니터 재등록 직후 같은 줄 재배달)를 막는다.

### 3.2 스폰과 수명 — on/off 를 반복해도 하나

- 부팅 때 노브가 켜져 있으면 `runtime` 의 restore/auto_spawn 자리에서 `spawn(profile="auditor", name="auditor")`. `spawn` 은 살아 있는 같은 `(profile, name)` 을 거부하므로(`_duplicate_label`) resume 으로 되살아난 감사자가 있으면 두 번째는 생기지 않는다. 스폰 브리프(첫 요청)에 main 세션의 history 경로와 `WAKE_AUTHOR` 문자열을 적어 준다 — 감사자가 알아야 할 유일한 하니스 사실.
- 노브가 꺼져 있는데 agents.json 에서 감사자가 되살아나면 부팅 직후 **kill** 한다(툼스톤; 로스터에 "죽음"으로 보인다). 켜진 세션에서 다시 스폰될 때 같은 이름의 툼스톤이 있으면 `_duplicate_label` 이 죽은 것의 라벨을 어떻게 다루는지(1419행)에 따라 거부될 수 있다 — 구현 때 확인하고, 거부된다면 감사자 스폰 전에 같은 이름의 툼스톤을 지운다(하니스 소유 에이전트라 지워도 된다). 세션마다 on/off 를 바꿔도 살아 있는 감사자는 늘 0 또는 1 이다.
- 세션 도중 토글은 v1 에 없다. 사용자가 `agent kill auditor` 로 죽이면 그 세션에선 다시 안 띄운다(사용자 결정 존중).

### 3.3 읽기 범위 — 서수 구간만 (결정)

- v1 은 지시로 제한한다(§3.1-2). `history` 도구에 구간 고정 인자를 더하지 않는다. §6 에서 구간 밖 조회 횟수를 센다. 실제로 새면 v2 에서 도구 쪽 클램프.
- 구간 밖이 필요해 보이는 경우(이전 런의 파일을 이번에 고쳤다)는 과거 대화가 아니라 **현재 워크스페이스**를 `read_file`·`code_index` 로 본다.
- `seq` 는 history.jsonl 의 1 기준 줄 번호(= 캐시 서수 hidx + 1). 모니터 보고의 줄 번호와 `history` 의 `seq` 가 같은 기준인지 구현 때 테스트로 못 박는다.

### 3.4 판정 본문 — 문법 강제 없음

```
AUDIT run seq 99..142: findings
[12] unmet — asked per-module function names; 7 of 14 modules read (seq 103..137), REPORT.md never written
[13] met — seq 150 write_file REPORT.md; seq 151 final line is the checksum
claims: 1 verified, 1 unverifiable ("tests pass" — no shell observation in seq 99..142)
```

- 읽는 쪽은 사람과 main 뿐이다 — 하니스가 파싱하지 않으므로 EBNF 문법을 걸 이유가 없다. 문법은 "구제 불가한 것만 막는다"는 원칙이고, 문법 변경은 토크나이저로 토큰당 비용을 재야 한다 — 산문 판정에 그 비용을 치를 가치가 없다.
- 구조가 꼭 필요해지면(예: 카드가 요청별 칩을 그린다) 길은 EBNF 가 아니라 **도구 호출**이다: `verdict {run, items:[{id, status, seq, why}]}` 같은 도구를 감사자에게만 주면 기존 인자 검증(스키마)으로 모양이 보장되고 native_fc 에선 서버가 강제한다. v1 범위 밖.

### 3.5 전달 — 기존 경로 그대로 (결정)

- findings → `message(to="main")`: 상주 → main 메일함 → `MailWaker` 가 유휴 main 을 깨움 → 🤝 wake 런에서 `_deliver_agent_mail` 이 관찰 레코드(`tool="agent"`)로 넣고 **main 채널에 에이전트 메시지 카드**로 그려진다. 사용자는 이걸 본다. main 은 보통 런처럼 고치고 complete 한다.
- pass → 감사자의 `complete`: 감사자 채널(칩)에 보인다. main 채널에는 아무것도 없다 — "통과" 를 main 채널에도 그리려면 매 complete 마다 main 을 깨워야 하므로(메시지는 곧 깨우기) 하지 않는다. 사용자는 감사자 채널이나 로스터의 활동 표시로 본다.
- `expects_reply`: `message` 는 회신을 기대하지 않는 전달이라 main 에 회신 빚이 생기지 않는다(기존 규칙). 되먹임 런이 또 미충족이면 감사자가 다시 감사하지 않으므로(§3.1-3) 거기서 끝난다 — 사용자 런 1 : 감사 1 : 되먹임 ≤ 1.

### 3.6 비용·상한

- 감사자 런 = 모니터 보고(종결 레코드 한 줄) + history 조회 2~3회 + read_file 몇 개 + 판정. `max-turns` 8.
- 추가 비용은 사용자 런당 감사자 런 하나(시스템 ~3K + 조회 결과 + 출력 ~300). 깨우기 런에도 모니터는 발화하므로 "skip" 런이 하나 더 생긴다(한두 턴). §6 에서 실측.
- 모델: v1 은 main 과 같은 바인딩(프로파일 `model` 비움).

### 3.6a 수명 — 구독이 세션을 쥐지 않고, 세션이 구독을 앞지르지 않는다 (구현 중 발견)

- 문제 둘: ① 살아 있는 모니터는 `session_has_live_work` 로 **run 을 안 끝내고 web 의 idle self-reap 을 막는다** — 감사자의 24시간 반복 구독이 있으면 `agent-cli run` 이 영영 안 끝나고 board 인스턴스가 안 꺼진다. ② 그렇다고 수명에서 빼기만 하면 main 의 complete 직후 폴링 틱(1초) **전에** run 이 끝나 구독이 헛돈다.
- 해법: `MonitorRegistry.mark_passive(owner)` — `ensure_auditor` 가 감사자 주소를 수동으로 표시하면 그 감시는 `has_active_work` 에 안 센다(발화·배달은 그대로). 그리고 `QuietPolicy.should_stop` 이 **판정 전에 `monitors.tick()` 을 한 번** 돌린다: 방금 끝난 런의 final 줄이 이미 history 에 있으므로 감사자 inbox 에 보고가 들어가고, `agent_registry.has_active_work()` 가 참이 되어 run 은 감사·되먹임 런이 끝날 때까지 산다. web 은 뷰어가 없고 감사자가 쉬면 평소처럼 거둬지고, 다음 부팅의 브리프가 구독을 다시 건다.

### 3.7 하니스 변경 — 거의 없음

| 파일 | 변경 | LOC |
|---|---|---|
| `agent_cli/agents/builtin/auditor.md` | 프로파일·역할 프롬프트(시작 의식·구간·건너뛰기·판정·전달) | +110 |
| `agent_cli/constants.py` | `AGENT_CLI_AUDITOR` 노브 | +10 |
| `agent_cli/runtime.py` | 노브 on: 스폰(브리프 = history 경로·WAKE_AUTHOR) / off: 되살아난 감사자 kill | +35 |
| `agent_cli/subagent/agents_live.py` | `instance_for(profile, name)` — 툼스톤 포함 조회. 라벨 충돌은 `resume_teammate` 로 푼다(같은 키 부활) | +12 |
| `agent_cli/monitor/registry.py` · `agent_cli/pump.py` | `passive_owners`/`mark_passive` · `should_stop` 전 `tick` (§3.6a) | +20 |
| `agent_cli/tools/monitor_tool.py` | `command`/`run` 은 `shell` 이 있는 루프만 — 감사자가 monitor 를 셸 뒷문으로 못 쓰게 (구현 중 발견: 종전엔 "monitor 있는 프로파일은 전부 shell 도 있다" 는 가정으로만 지켜졌다) | +14 |
| tests | `tests/test_auditor.py`(13): 노브·프로파일 도구 집합/규칙 문구·브리프·패턴이 final 줄에만 맞음·seq=줄 번호·스폰 단일성(on/off/resume)·부팅 알림·조립 4-튜플; monitor shell 게이트 1 | +260 |
| docs | README(노브·동작), ARCHITECTURE, 이 문서 §6 | — |

새 이벤트·카드·파서·history 레코드 종류는 없다. 1판에 있던 `audit.py`·`kind:"audit"`·`audit` 카드는 전부 사라진다.

## 4. 시퀀스 (findings)

1. 사용자 런 #7: 요청 [12] "모듈별 함수 이름을 REPORT.md 에" → main 이 7개만 읽고 complete(answers=[12]) → history 142번째 줄에 final.
2. 모니터 발화 → 감사자 런: 보고 본문 = 그 줄. `history` 로 직전 final(seq 98) → 구간 99..142. query 레코드 author 가 사람 → 감사한다.
3. 조회: read_file 7건, write_file 없음 → `[12] unmet …`. `message(to="main", …)`. 자기 런은 complete.
4. main 채널에 감사자 메시지 카드. MailWaker → 🤝 런 #8: main 이 나머지를 읽고 REPORT.md 를 쓰고 complete → final 한 줄.
5. 모니터 발화 → 감사자 런: 구간 143..170 의 query author 가 WAKE → `complete("skip: wake run #8")`.
6. 사용자 런 #9 가 오면 1 부터.

## 5. 위험

- **감사자가 SQL 지시를 안 지킴**(구간 밖, 건너뛰기 실패): 측정 지표로 둔다. 새면 v2 도구 클램프(§3.3).
- **모니터 한계**: deadline 최대 24h → 감사자가 재등록해야 한다(만료 알림이 런으로 오므로 가능). 세션을 넘지 않으므로 resume 뒤 재등록. `AGENT_CLI_SESSIONS_DIR` 로 세션 폴더가 워크스페이스 밖에 있으면 `match` 파일이 봉쇄에 걸릴 수 있다 — 구현 때 확인, 걸리면 감사자 스폰 때 해당 경로를 허용 목록에 더한다.
- **예스맨**: 증거 서수 요구로 줄인다. 그래도 서수를 지어낼 수 있다 — 측정 C 가 본다.
- **감사자의 오류**: 틀린 findings 는 main 을 한 번 더 돌린다(최대 1회). main 은 감사문을 관찰로 볼 뿐 따를 의무가 없다.
- **다중 사용자 웹**: 되먹임 런의 귀속은 기존 회신 귀속 승계를 탄다(감사자 메시지는 그 런의 요청자를 물려받지 않을 수 있다 — 구현 때 확인).

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

## 7. 열린 질문

- **A. pass 를 main 채널에도 보일지**: 지금은 감사자 채널만. 보이게 하려면 매 complete 마다 main 을 깨워야 한다(비용) — 측정 뒤 결정.
- **B. 기본값**: §6 뒤.
