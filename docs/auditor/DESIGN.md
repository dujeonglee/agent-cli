# 감사자(auditor) — main 의 complete 마다 결과를 감사하고 되먹인다

> 상태: **구현됨 (v10.42.0, 2026-10-16~17) — 3판: 하니스가 런마다 회신을 빚지는 요청을 보낸다. §6.3 3차 실측 뒤 머지, 기본값 off**
> 결정(사용자): ① 서수 구간만 읽는다 ② 결과는 사용자에게도 보인다 ③ 트리거는 **하니스가 main 의 런 종료마다 보내는 `request(expects_reply=True)`** (2판까지의 `monitor` 구독을 대체 — §6.2 실측 뒤 사용자 제안) ④ 전달은 **기존 회신 빚** — 감사자는 `reply` 하기 전엔 `complete` 가 거절된다 ⑤ 측정 계획 동의.
> 판의 역사: 1판(하니스 파서·카드) → 2판(감사자가 `monitor` 로 구독, findings 만 `message`; §6.1·§6.2 실측에서 전달 2/10) → 3판(이 문서).

## 0. 한 줄 요약

세션 노브로 내장 프로파일 `auditor` 를 상주로 띄운다. main 의 **사용자 런**이 `complete` 로 끝날 때마다 하니스(`SessionAuditor.run_ended`)가 그 런의 history 구간 `seq a..b` 를 적은 요청을 감사자에게 **회신을 빚지는** 형태로 보낸다. 감사자는 `history` 도구로 **그 구간만** 읽고 요청별 충족/미충족/확인불가를 증거 서수와 함께 판정해 `reply` 로 돌려준다 — 빚이 있는 동안 `complete` 는 거절되므로 보고는 모델의 선택이 아니다. 회신은 기존 메일 경로로 main 채널에 카드로 그려지고 main 을 깨운다; main 은 findings 를 손보고(통과면 바로 끝내고) complete 한다. 그 깨우기 런에는 하니스가 요청을 보내지 않는다 — 고리 상한은 코드다. 새 카드·새 이벤트·판정 파서가 없다.

## 1. 왜

- main 은 자기 보고를 검증하지 않는다. 2026-10-16 압축 핀 실측에서 한 런이 40턴 중 17턴에 멈추고 "턴이 모자라 보고서를 못 썼다"고 했다.
- 지금 있는 완료 시점 검사는 `_require_answers`(미답 요청 id 를 채웠나) 하나다. 기계적이라 "id 는 채웠는데 내용이 답이 아니다"는 못 본다.
- 정적 필터는 주장·관찰 대조만 본다. 요청에 답이 됐는지는 LLM 이 봐야 한다 — 같은 모델이라도 종결문을 **쓰는 자리**와 **대조하는 자리**는 다른 과제다. 값어치는 §6 측정으로 확인한다.

## 2. 목표 / 비목표

목표: 사용자 런마다 complete 를 한 번 감사하고, 미충족이 있으면 main 이 한 번 더 손보게 한다. 감사는 증거 기반이다(충족을 보여주는 레코드 서수를 못 짚으면 미충족/확인불가). 사용자는 결과를 본다.

비목표: 정적 필터 단계. 감사가 main 을 블로킹하는 것. 세션 전체 history 접근. 감사의 감사. 감사자가 직접 고치는 것(쓰기 도구 없음). 감사자가 사용자에게 묻는 것(`ask` 없음). 새 UI.

## 3. 구조

```
main 세션                                         auditor (상주, 같은 세션의 teammate)
  사용자 런 끝 (complete → history 에 final 한 줄)
    펌프: main_run_ended → SessionAuditor.run_ended(req, ok)
      사용자 런이고 ok 일 때만 ──────────────────▶ request(expects_reply=True):
      "Audit … seq a..b of session <sid>"                history: 구간의 query/action/observation 조회
      (a = 런 전 커서+1, b = 지금 레코드 수)             read_file / code_index: 현재 워크스페이스 확인
                                                        판정 → reply(AUDIT 본문)   ← 빚: reply 전 complete 거절
  main 메일함 ◀── 회신 (기존 경로, 카드) ────────────┘
  MailWaker → 🤝 wake 런: main 이 관찰로 보고 findings 를 손본다(pass 면 바로 complete)
    펌프: run_ended(req.wake=True) → 요청 없음        ← 고리 상한
```

### 3.1 프로파일 `auditor` (내장, `agent_cli/agents/builtin/auditor.md`)

- `allowed-tools`: `history`, `read_file`, `code_index`, `memory`. `shell` 은 **넣지 않는다** — 존재·크기 확인은 `read_file`·`code_index` 로 되고, 테스트를 다시 돌리는 식으로 main 의 일을 중복하는 길을 처음부터 막는다. `monitor` 도 없다(3판: 구독할 것이 없다). 쓰기 도구·`agent`·`ask`·`browser`·`fetch` 없음. `message`·`reply` 는 상주 기본 능력으로 강제 탑재된다.
- `auto-spawn` frontmatter 는 쓰지 않는다. 켜고 끄는 것은 부팅 노브 `AGENT_CLI_AUDITOR`(v1 기본 **off**; §6 뒤 결정).
- 역할 프롬프트의 뼈대:
  1. **요청이 런을 가리킨다**: 세션 id 와 `seq a..b` 는 요청에 있다. 감사자는 런을 찾지도, 구독하지도, 어느 런이 감사 대상인지 정하지도 않는다.
  2. **구간만**: `history(sessions:"<sid>")` 에 `loc LIKE '<sid>/history.jsonl:%'`(§3.3) 와 `seq BETWEEN a AND b`. 파일의 현재 상태는 워크스페이스에서.
  3. **판정**: 요청마다 `met`(충족을 보여주는 서수 + 근거 한 줄) / `unmet` / `unverifiable`(종결문의 사실 주장에 구간 안 근거 레코드가 없다). 추측 금지, 의견 금지. 증거 규칙 둘(§6.1 에서): 파일은 `read_file` 로 직접 판정하고, 잘린 열(`substr`)은 부재의 증거가 아니다. 두 번 직접 확인해 없으면 그게 결론이다(§6.2 B-3 의 40턴 탐색 방지).
  4. **보고**: pass 든 findings 든 `reply` 로. 그 뒤 `complete` 한 줄. 감사·인사·조언 금지.

### 3.2 스폰과 수명 — on/off 를 반복해도 하나

- 부팅 때 노브가 켜져 있으면 `runtime.ensure_auditor` 가 restore/auto_spawn 자리에서 `spawn(profile="auditor", name="auditor")`. `spawn` 은 살아 있는 같은 `(profile, name)` 을 거부하므로 resume 으로 되살아난 감사자가 있으면 두 번째는 생기지 않는다; 죽은 툼스톤이면 같은 키로 `resume_teammate`. 부팅 때 보내는 것은 없다 — 감사자는 첫 요청까지 논다.
- 노브가 꺼져 있는데 agents.json 에서 감사자가 되살아나면 부팅 직후 **kill** 한다. 세션마다 on/off 를 바꿔도 살아 있는 감사자는 늘 0 또는 1 이다.
- 돌려주는 것은 `SessionAuditor` 핸들 하나(`outcome` 부팅 알림, `active`, `run_ended`) — 펌프 `PumpDeps.auditor` 자리에 꽂힌다. 비활성 핸들은 아무것도 보내지 않으므로 run/web 조립이 한 모양이다.
- 수명은 저절로 맞는다: 회신을 빚진 감사자 런은 살아 있는 작업이라 `run` 은 기다리고, 회신이 main 을 깨우고, 그 런이 끝나면 조용해진다. 2판의 passive 모니터·판정 전 tick 은 전부 사라졌다.

### 3.3 읽기 범위 — 서수 구간만 (결정)

- 구간은 하니스가 잰다(`SessionAuditor` 의 커서 — 런 전 레코드 수 +1 .. 런 뒤 레코드 수; 모든 런이 커서를 앞당겨 실패한 런·깨우기 런의 레코드가 다음 구간에 섞이지 않는다). 읽기 제한은 지시다 — `history` 도구에 구간 고정 인자를 더하지 않는다. §6 에서 구간 밖 조회 횟수를 센다. 실제로 새면 v2 에서 도구 쪽 클램프.
- 구간 밖이 필요해 보이는 경우(이전 런의 파일을 이번에 고쳤다)는 과거 대화가 아니라 **현재 워크스페이스**를 `read_file`·`code_index` 로 본다.
- `seq` 는 history.jsonl 의 1 기준 줄 번호(= 캐시 서수 hidx + 1). **실측(A-smoke)**: `history` 의 세션 테이블은 `agents/<key>/history.jsonl` 도 싣고 `seq` 는 파일별이라, 감사자 자신의 시작 런 `final`(seq 7) 이 main 의 직전 final 로 잡혀 구간이 8..43 으로 틀렸다(모델이 1..7 을 읽어 스스로 바로잡았지만 그게 "구간 밖 조회" 1건). 모든 질의에 `loc LIKE '<sid>/history.jsonl:%'` 를 넣는다 — 요청문(`AUDIT_REQUEST`)과 프로파일이 강제.

### 3.4 판정 본문 — 문법 강제 없음

```
AUDIT run seq 99..142: findings
[12] unmet — asked per-module function names; 7 of 14 modules read (seq 103..137), REPORT.md never written
[13] met — seq 150 write_file REPORT.md; seq 151 final line is the checksum
claims: 1 verified, 1 unverifiable ("tests pass" — no shell observation in seq 99..142)
```

- 읽는 쪽은 사람과 main 뿐이다 — 하니스는 회신을 배달만 하고 파싱하지 않으므로 EBNF 문법을 걸 이유가 없다. 문법은 "구제 불가한 것만 막는다"는 원칙이고, 문법 변경은 토크나이저로 토큰당 비용을 재야 한다 — 산문 판정에 그 비용을 치를 가치가 없다.
- 구조가 꼭 필요해지면(예: 카드가 요청별 칩을 그린다) 길은 EBNF 가 아니라 **도구 호출**이다: `verdict {run, items:[{id, status, seq, why}]}` 같은 도구를 감사자에게만 주면 기존 인자 검증(스키마)으로 모양이 보장되고 native_fc 에선 서버가 강제한다. v1 범위 밖.

### 3.5 전달 — 회신 빚 (3판)

- 하니스의 요청이 `expects_reply=True` 라 감사자는 회신 빚을 진다: 꼬리의 `## Owed Replies` 가 매 턴 보이고, `complete` 는 빚이 남아 있으면 거절되며(3회 독촉), 그래도 안 하면 런 요약이 라벨 붙어 폴백 배달된다. 어느 길이든 main 은 감사 결과를 받는다 — 2판의 "모델이 `message` 를 고른다" 가 1·2차 실측에서 2/10 이었던 자리.
- 회신 → main 메일함 → `MailWaker` 가 유휴 main 을 깨움 → 🤝 런에서 관찰 레코드로 들어가고 **main 채널에 에이전트 회신 카드**로 그려진다. 사용자는 이걸 본다. pass 도 같은 길이라 §7 A 는 해소됐다 — 비용은 사용자 런마다 main 의 짧은 깨우기 런 하나(관찰 읽고 complete).
- 귀속: `request(author="main")` 은 지금 런의 사용자 목록을 스냅샷하므로 회신은 그 런의 요청자에게 귀속된다(다중 사용자 웹).
- 고리: `run_ended` 는 `req.wake` 또는 `not req.author_is_user` 면 보내지 않는다. 사용자 런 1 : 감사 1 : 되먹임 ≤ 1 — 코드가 보장하고 §6 이 로그로 확인한다.

### 3.6 비용·상한

- 감사자 런 = 요청 한 줄 + history 조회 1~3회 + read_file 몇 개 + 판정 + reply. 상주 턴 상한이 폭주를 막는다(§6.2 B-3: 40턴).
- 추가 비용은 사용자 런당 감사자 런 하나 + main 의 깨우기 런 하나(pass 면 한두 턴). §6 에서 실측.
- 모델: v1 은 main 과 같은 바인딩(프로파일 `model` 비움).

### 3.7 하니스 변경

| 파일 | 변경 | LOC |
|---|---|---|
| `agent_cli/agents/builtin/auditor.md` | 프로파일·역할 프롬프트(구간·판정·증거 규칙·reply) 3판 | +87 |
| `agent_cli/constants.py` | `AGENT_CLI_AUDITOR` 노브 · `AUDIT_REQUEST` | +22 |
| `agent_cli/runtime.py` | `SessionAuditor`(핸들·커서·`run_ended`) · `ensure_auditor`(스폰/부활/kill) | +75 |
| `agent_cli/pump.py` | `PumpDeps.auditor` · 런 종료마다 `run_ended` | +7 |
| `agent_cli/main.py` | 부팅 알림 · 두 펌프에 핸들 전달 | +10 |
| `agent_cli/subagent/agents_live.py` | `instance_for(profile, name)` — 툼스톤 포함 조회 | +12 |
| `agent_cli/tools/monitor_tool.py` | `command`/`run` 은 `shell` 이 있는 루프만 — 2판 구현 중 발견한 구멍, 감사자와 무관하게 남긴다 | +14 |
| tests | `tests/test_auditor.py`(22): 노브·프로파일 도구 집합/규칙 문구·요청문·seq=줄 번호·스폰 단일성(on/off/resume)·`run_ended` 구간/깨우기/실패/빈 런/죽은 감사자·실 레지스트리 배달·펌프 배선·조립·부팅 알림; monitor shell 게이트 1 | +330 |
| docs | README(노브·동작), ARCHITECTURE, 이 문서 §6 | — |

새 이벤트·카드·파서·history 레코드 종류는 없다. 2판의 `MonitorRegistry.passive_owners`·`QuietPolicy` 의 판정 전 `tick` 은 되돌렸다.

## 4. 시퀀스 (findings)

1. 사용자 런 #7: 요청 [12] "모듈별 함수 이름을 REPORT.md 에" → main 이 7개만 읽고 complete(answers=[12]) → history 99..142 줄.
2. 펌프: `run_ended(req 사용자, ok)` → 감사자에게 "Audit … seq 99..142" (회신 빚).
3. 감사자 런: 구간 조회, read_file 7건·write_file 없음 → `reply("AUDIT run seq 99..142: findings / [12] unmet …")` → complete 한 줄.
4. main 채널에 회신 카드. MailWaker → 🤝 런 #8: main 이 나머지를 읽고 REPORT.md 를 쓰고 complete.
5. 펌프: `run_ended(req.wake)` → 요청 없음. 조용해지면 run 종료.
6. 사용자 런 #9 가 오면 1 부터.

## 5. 위험

- **감사자가 SQL 지시를 안 지킴**(구간 밖): 측정 지표로 둔다. 새면 v2 도구 클램프(§3.3).
- **예스맨**: 증거 서수 요구로 줄인다. 그래도 서수를 지어낼 수 있다.
- **감사자의 오류**: 틀린 findings 는 main 을 한 번 더 돌린다(최대 1회). main 은 감사문을 관찰로 볼 뿐 따를 의무가 없다. §6.1 의 오탐 둘은 증거 규칙으로 줄였다.
- **폭주**: 없는 파일을 계속 찾는 식의 탐색(§6.2 B-3)은 상주 턴 상한이 끊고, 그 경우 폴백 배달(런 요약)이 main 에게 간다 — 판정문은 아니지만 "감사자가 결론을 못 냈다" 는 사실은 전달된다.

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

곁다리: 상주의 `message`/`reply` 호출은 history 에 `kind: raw` 빈 본문으로만 남고 호출 자체(수신자·본문)가 기록되지 않는다 — 별도 건.

### 6.3 3차 계획 (3판)

A·D 각 3회(B 는 이 모델·32K 창에서 main 이 2/3 턴을 소진해 감사 대상이 안 생긴다 — 과제 설계 문제라 뺀다). 보는 것: **전달률**(회신이 main 메일함에 도달 — reply 든 폴백이든; reply 로 온 비율 따로), 고리(사용자 런당 감사 1·깨우기 런 1·그 런에 요청 없음), pass 때 깨우기 런의 비용(턴 수), 판정 정확도, 구간 밖 조회 0.

## 7. 열린 질문

- **A. pass 를 main 채널에도 보일지** — 3판에서 해소: 회신이라 pass 도 main 채널 카드로 보인다. 비용은 사용자 런마다 main 의 짧은 깨우기 런 하나.
- **B. 기본값**: §6.3 뒤.
