# 도구 프롬프트 검토 기록 (2026-10-28~)

모델이 보는 도구 문구를 하나씩 검토한다. 순서: 실제로 모델이 보는 본문 전체를 모은다 → 벤치 기록으로 실제 사용을
센다 → 문제와 제안 → 개정안(HTML)으로 바뀔 본문 전체를 보인다 → 사용자 승인 → 구현. **측정은 사용자가
지시할 때 전체 벤치로 한 번에** (2026-10-28 결정 — 그 전에는 벤치를 돌리지 않는다).

공통 규칙: 모델이 보는 글의 표기 — 도구·인자는 백틱, 값은 큰따옴표, 호출 모양 없음
(`docs/prompt-notation/DESIGN.md`, 가드 `tests/test_prompt_notation.py`).

브랜치: `view-image` (push 안 함, 버전 10.77.0).

## 검토한 도구

| 도구 | 상태 | 바꾼 것 | 커밋 |
|---|---|---|---|
| `read_file` | 구현 | 설명 한 문단(모드 한 번), 사용 안내 블록·호출 예시·code_index Flow 삭제, 인자 문장, 결과 머리줄 `[read] path: lines a–b of N`, stat 힌트·과대 결과 넛지는 문장으로("Next time —"), read_file 이 없는 루프엔 read_file 경로 생략, 이미지 분기 삭제(텍스트가 아니면 "is not a text file") | 1fcad275, da88b501 |
| `view_image` (새로) | 구현 | read_file 의 이미지를 별도 도구로. 이미지는 압축이 내보낼 때만 빠짐(6장 상한 삭제), 도구를 바꾸는 층(read_file 이미지 → view_image, 컨텍스트에 바뀐 호출, 원래 호출은 `rewritten_from`), 압축 요약기에 그림·IMAGES 항목·`## Images viewed in earlier turns` 목록 | da88b501, 08f090e6, 79fffd74, 9ec18bb8 |
| `write_file` | 구현 | "NEVER a shell heredoc" 금지 삭제(native_fc heredoc 64회 실패 0), 설명·인자 사실 문장, 30% 미만 넛지는 사실, echo 는 40줄 이하 전체·넘으면 처음·끝 10줄, write_file 이 있으면 edit_file 도 붙음(`Tool.mounted_with`), 내장 스킬 넷 allowed-tools 에 edit_file | 0c4379ae |
| `edit_file` | 구현 | 줄 번호만 온 ref 에 그 줄의 지금 ref 를 보여 줌(35B 편집의 18%), 설명은 `op` 값과 인자를 이름으로, legacy 광고 삭제, 안내 축소 | ebccc656 |
| (전체) 표기 | 구현 | 도구 22개·안내·시스템 절·꼬리·넛지·오류 문구 표기 통일, "You sent:" 줄·반복 호출 넛지를 문장으로, monitor 오류의 모르는 이름(match/silence) 수정 | 1a75cb0a |
| (압축) | 구현 | 압축 예고 메모리 문장: 기준 "앞으로의 일이 쓰지 않을 것" + 끝난 단계의 메모, memory 없는 루프엔 문장 생략 | 08f090e6 |
| `ask`·`answer`·`message`·`reply`·`wait`·`complete` (묶음) | 구현 | ① id 할당기 하나(id 가 곧 키, 순번 삭제) ② `ask` 는 사람에게만·`answer` 삭제, `reply` 의 `to` 삭제, `message`·`agent` 결과가 id 를 말함, 봉투 한 모양, 거부 문구가 접두로 종류를 말함; wait 를 건너가는 빚·main wait 정산 비대칭 수정·순환 대기 가드 | 1a7dc454, (②) |
| `python` | 구현 (v10.78.0) | 셀은 진짜 `__main__` 모듈에서(pickle·fork Pool·unittest), 파싱 SyntaxError 에 하니스 프레임 없음, 인터프리터는 PATH `python3`→`python` 뿐(`AGENT_CLI_PYTHON`·agent-cli 자기 인터프리터 폴백 삭제, 없으면 "없다 — 설치하면 다음 호출부터"), 시작 인사로 인터프리터 불량과 코드의 프로세스 종료를 가름, `timeout`·`reset`·SystemExit 문구, 패키지 짝 3개 — 개정안 https://claude.ai/artifact/UNSJtA7HTs5SyvE72Zts4W, Fable 리뷰 3회 | (이 커밋) |
| `agent` | PR A·B·C 구현 (v10.84.0 · v10.85.0 · v10.86.0) | 검토 리포트(2026-10-30, 735 trial: run 18·spawn 12·status 15·kill 10·request 6·resume 3, 한 런이 29회) https://claude.ai/artifact/NPP3ZewFnRvyzEuGB4pPD6 — 결정: H1 (a) one-shot 의 `wait` 는 자기 주소(`run:<dir>`)의 메일함을 기다림(docs/wait/DESIGN.md §7, 설계 2판 https://claude.ai/artifact/75VC1MJfqkM2ivFQtZwBBF), 스킬 루프는 `wait` 거부, 끝날 때 잡·모니터·메일함 전부 정리; H2 죽은 에이전트 `kill` 문구; H3 병렬 패널 `markup=False`; H4 유지. **PR B**(v10.85.0): T1 결과·거절 문구의 호출 모양 7곳·대문자 11낱말 삭제 — 문구를 모듈 상수로 빼 inventory 표본으로 등록(가드가 도구 결과·꼬리까지 봄), T2 "기다리는 법" 네 문장 설명→꼬리 `## Live Agents` 머리글(이동, 삭제 아님; 결과는 사실만 — 측정은 다음 전체 벤치), T4 낱말(`instructions` 의 instant-agent·`tools` 문장·`status` 넛지 한 줄); 상주 머리글의 `message` JSON 조각도 문장으로; resume 의 과제가 요청 id 를 받음. **PR C**(v10.86.0): 내장 프로파일 description 7개를 짧은 한 문장(22–35 단어, 역할·읽기/쓰기·상주 여부; `general` 만 둘째 문장에 전문가 라우팅 — v10.21.0 결정 유지)으로 — `## Agent Profiles` 절 620→427 토큰(GLM 토크나이저; Qwen 628→436), 설명만 494→301; 본문 불변; 행동 측정 불가(쓰임 9/735)라 토큰만. 머리글의 `": "` 는 YAML 매핑이라 설명이 통째로 비는 것을 실측 — `" — "` 로. 가드 tests/test_builtin_profile_descriptions.py(첫 문장 ≤35 단어·전체 ≤60·persistent). | |
| (감사자) `Checked:`/`Not checked:` 검증 목록 | 구현 (v10.83.0) | 사용자 제안(2026-10-30): 감사자가 main 종결문의 두 줄을 검증 목록으로 — Checked 항목마다 기록 대조, Not checked 는 요청이 요구했는지만, 두 줄 없음=자기 보고 부재; `AUDIT_REQUEST` 한 문장·inventory 등록·가드가 `agents/harness/*.md` 도 읽음. 측정 docs/auditor/DESIGN.md §6.7(감사 켠 오답 대표 과제 4개). | |
| `snapshot`·`history`·`memory` (묶음) | snapshot v10.80.0 · memory·history v10.81.0 | 검토 리포트(2026-10-30, 96 잡·716 trial: snapshot 166 · memory 16 · history 5 · 압축 0): 세 도구는 쓰는 주체·축·범위가 달라 통합 안 함. **PR A snapshot 수리**(docs/snapshot/DESIGN.md §6): 절대 경로 정규화(restore 실패 59건 중 52건), `list` 의 `path`, `python` 문구, 라벨에 호출 요약, 복구 결과의 원인 줄, 세션 시작 커밋 항상(경계, 사용자 결정 "저장소는 세션을 넘긴다 — 유지"). **PR B**(v10.81.0, docs/session-memory/DESIGN.md §11): memory `mode` 없는 add 구제 · `memory list` 삭제(사용자 결정) · 항목에 turn(색인 `(turn N)`) · memory·history 설명 재작성 · 표기 가드에 대문자 강조 검사(사용자 결정) — 처음 133건을 모델 문구 전체에서 소문자로. **PR C 미채택**(2026-10-30 측정): 압축 요약 꼬리에 history 범위 한 줄(`seq` a~b) — 압축 재현 A/B(창 26,000 models.json, 10 파일 읽기 → `data/` 제거 → `--resume` 되묻기, GLM 사고 off T0.2, 스냅샷 끔) 기준·후보 6런씩: `history` 호출 6/6 vs 6/6(기준도 읽기 실패 뒤 Context Recovery 로 간다), 정답 0/6 vs 0/6 — 줄은 행동을 바꾸지 않아 넣지 않음(브랜치 `compaction-history-range` 로컬 보관). 부수 발견: 12런 전부 seq 3(42k자 관찰)을 찾고도 `substr(text,1,N)` 만 써 137번째 줄에 닿지 못함 — `instr` 앵커 substr 을 쓴 런 0. | |
| `shell` | 구현 | 시간 초과 때 프로세스 그룹째 끝내기(버그 — apt-get 이 dpkg 잠금을 쥔 채 남음), 시간 초과 결과(끝냈다·출력 끝 8줄·`background` 교정), 설명에 결과 모양(`[stderr]`·`[exit code: N]`), 작업 종류 나열 삭제, 거부 문구 표기 | 6ec4b40c |

## TODO

- [x] `shell` 검토·구현
- [x] 소통·종결 묶음 구현: ① id 통일(내부, 모델 문구 그대로) ② B·C·D·E(모델 문구) — 개정안 https://claude.ai/artifact/3iJsAjeva3YB4wwWNcPdYF
- [ ] 상주 에이전트 둘로 실제 모델 시나리오 확인(요청·회신·알림·사람에게 묻기·wait)
- [ ] 후보(보류): 층 규칙 `complete` → `wait` — 넷이 모두 맞을 때만: 상주 루프(complete 가 아무에게도 보고 안 함), 이 런에 갚지 않은 `r-` 남음, 깨울 원인 있음(회신 대기·작업·monitor), `complete` 가 닫을 `u-`·`n-` 없음. `result` 는 `rewritten_from` 에. 원칙을 "결과 같음"→"이 루프에서 효과 같음"으로 넓히는 일이라, ② 뒤 실제 `complete` 오용을 보고 결정
- [x] `python` 검토·구현 (docs/python-tool/DESIGN.md §7)
- [ ] 후보: `wait` 의 기다림 상한 — v10.77.1 r1 의 시간 초과 둘(large-scale vim 18분·video OCR)이 상한 없는 `wait` 로 끝났다
- [x] `snapshot`·`history`·`memory` 검토 — snapshot 수리 v10.80.0, memory·history v10.81.0; 압축 요약 꼬리(PR C, 측정 뒤) 남음
- [ ] `history` 로 큰 관찰의 **중간**을 뽑는 길 — PR C 측정의 부수 발견(12/12 런이 `substr(text,1,N)` 만 씀). 후보: help 예시에 `substr(text, instr(text,'…'), 200)` 한 줄, 또는 `loc` + 오프셋 인자. 측정은 같은 런처로.
- [x] 내장 에이전트·스킬 **본문**의 대문자 강조 — 사용자 결정(2026-10-30)으로 본문 43곳 소문자화 + 가드가 파일도 읽음(`test_no_caps_emphasis_in_builtin_bodies`, docs/prompt-notation/DESIGN.md 가드 절)
- [ ] 남은 도구 검토: `code_index` → `fetch` → `run_skill` → `browser`·`schedule` 순 (`agent` 는 PR A·B·C 구현 완료; 2026-10-31 순서 확정)
- [ ] 도구 설명 지연 로딩(ToolSearch 식) — 드문 도구는 이름·한 줄만 싣고 설명은 요청 시. 설계 검토부터(프롬프트 크기·벤치 영향 둘 다 잰다). 메모리 `deferred-tool-descriptions-idea`
- [ ] v10.91.2 GLM 벤치 ×2 — v10.91.0 이 8/9·8/9, 그 뒤는 버그 수정(경과·로그 재사용·바쁨 거절 순서)뿐이라 급하지 않음. 볼 지표: 전경 timeout kill·`Removed` 안내·python 작업화·dash 오류
- [ ] 도구를 바꾸는 층 규칙 후보: shell `cat`·`head`·`tail`(파일 하나) → `read_file` — 원칙을 "같은 정보"로 넓히는 일이라 결정 필요 (35B 8런 29회, GLM 거의 없음)
- [ ] `plan` 스킬을 실제 모델로 한 번 돌려 확인 (write 면 edit 도 붙는지)
- [ ] 전체 벤치 측정 — 사용자 지시 시 한 번에 (9과제, GLM; 35B 는 그때 결정). 볼 지표: 층이 바꾼 수·같은 이미지 다시 보기·압축 뒤 다시 보기, read_file 모드별·셸 파일 보기 수, heredoc 실패, 같은 파일 다시 쓰기, 줄 번호 ref 수, "You sent:" 뒤 재시도 성공
- [ ] 측정 뒤 회귀면 이미지를 read_file 로 되돌리기(층은 규칙만 비우고 남김)

## 미룬 것·안 하기로 한 것

- 소통 개정에서 개정안과 달리 한 것: 사람의 답 봉투 id 는 `n-` 이 아니라 `u-` — 그 런은 사람 런이라
  `complete` 의 `answers` 로 닫혀 왔다(종전과 같은 엄밀함). 상주 `ask` 의 질문 인자 설명은 main 과 같은
  "The question to ask the user." 하나(전용 스키마를 없앴다). 회신·통지 봉투의 꼬리 안내(`_REPLY_TAIL`·
  `_NOTICE_TAIL`, ack 억제)는 개정안대로 지웠다 — ack 핑퐁 재발은 라이브에서 본다.

- 화면의 "(from read_file)" 표시 — 하지 않음. 기록의 `rewritten_from` 에만 둔다(사용자 결정).
- 모델이 직접 이미지를 내리는 hide·20% 예산·꼬리 `## Images` — 하지 않음(대화 중간을 바꿔 KV 캐시를 깸 → 압축에 맡김).
- `plan` 장부 — 보류(브랜치 `plan-ledger`, docs/ledger/DESIGN.md §14.1).

## shell — 검토 (2026-10-28)

근거: 9과제 짝 벤치 pbase·plan, 모델마다 4런 — GLM 234회·35B 515회.

- **[버그] 시간 초과가 `/bin/sh` 만 죽인다.** `subprocess.run(shell=True, timeout=)` — 그 아래 `apt-get` 은 살아남아
  dpkg 잠금을 쥐었고, 다음 `apt-get` 이 "Could not get lock … held by process 3171 (apt-get)" 로 실패(GLM pbase video).
  → 프로세스 그룹으로 띄우고 시간 초과 때 그룹째 끝내기(`jobs.kill` 과 같은 방식), 그때까지의 출력 끝부분을 돌려주기.
- **시간 초과 문구가 다음 행동을 말하지 않는다.** "Command timed out (120s)" 뿐. 시간 초과 8건 중 5건이 foreground `apt-get install`
  (설명에 "an install … set `background`" 가 있는데도). → 끝냈다는 사실·그때까지의 출력·"이만큼 걸리는 명령은 `background` true" 를
  그 자리에서(교정은 트리거 직후 — v10.73.0 자리 원칙).
- **결과 모양을 설명이 말하지 않는다.** stdout, `[stderr]` 블록, 0 이 아닐 때 `[exit code: N]` — 모델이 `echo $?` 47회, `2>&1` 256회를
  덧붙였다. → 설명에 한 문장.
- **작업 종류 나열**("a test suite, a build, an install, a large clone") — 우리 규칙(작업 종류 대신 성질) 위반, 인자 설명에도 반복.
- **호출 모양 잔재**: background 거부 문구 "background=true takes the PLAIN command" (표기 통일에서 빠짐).
- 그대로: 작업 시작·종료 통지, `## Background jobs` 꼬리, 넛지 `SHELL_PYTHON_NOTICE`, 과대 결과 retry 힌트, 위험 명령 확인.
- 참고: 매 호출은 작업 디렉터리에서 시작(cd 는 이어지지 않음) — 모델은 `cd X && …` 로 잘 쓴다(맨 `cd` 0회), 문구 불필요.

## 소통·종결 묶음 (`ask`·`answer`·`message`·`reply`·`wait`·`complete`) — 검토 (2026-10-28)

근거: 실제 세션 기록 312개(보드 방, 읽기만) — `complete` 363, `answer` 11(**전부 실패**: 사용자 요청에 쓰거나 id 를
지어냄), `ask` 6, `wait` 4, `reply` 2(전부 실패), `message` 0. 벤치 72런 — `complete` 58, `wait` 10(GLM 8, 거부 2),
`answers` 누락 0, `Checked:` GLM 32/32·35B 9/26.

- 항목 넷(`u-`·`n-`·`r-`·`q-`)에 닫는 법 셋(`complete`·`reply`·`answer`) + "요청자에게 보낸 `message` 도 reply" 예외.
- `answer` 와 `reply` 는 모양(`id`, `text`)도 뜻도 같다 — 상대가 `ask` 로 물었나 `message` 로 물었나만 다르다.
- 상주 판 `ask` 의 `to` "main"/에이전트 key 가 `message` 와 겹친다(질문 경로 둘: `q-`/`answer` 와 `r-`/`reply`).
- `reply` 의 `to` 는 `id` 가 이미 정하는 것을 다시 묻는다.
- 봉투 넷: `[question q-… from …]:`, `── answer from … ──`, `[answer to your question: …]`, `── request [r-…] from … ──`.
- 표기 잔재: 상주 `ask` 의 `to` 설명 값이 백틱, 일반 `ask` 의 "the same way you batch `read_file`".
- 그대로(측정으로 정함): `complete` 의 Checked/Not checked 와 꼬리 `RESULT_SHAPE_LINE`(v10.75.0), wait/complete 꼬리말(v10.67.1).

제안: **A** `answer` 를 `reply` 에 합침(닫는 법 둘: `complete`·`reply`) · **B** `ask` 는 사람에게만(에이전트에겐
`message`) — 결정 필요 · **C** `reply` 의 `to` 삭제 · **D** 들어오는 봉투 한 모양 · **E** 문구 정리. 권장: A+C+D+E 먼저, B 는 실사용 뒤.

### 결정 (2026-10-28, 사용자)

- **B+C+D+E + id 통일**로 간다(A 는 B 에 포함 — 에이전트가 `q-` 를 받지 않으니 `answer` 삭제).
- **엄밀함 불변** — 열고 닫는 규칙은 느슨해지면 안 된다. 기존 장부·요청 회계 테스트를 그대로 통과시켜 증명(바뀌는 기대값은 id 문자열과 봉투 모양뿐).
- ① 끝내 답하지 않은 에이전트 질문(이제 `r-` 요청)은 미납 회신으로 닫히고, 묻는 쪽은 런 요약을 받는다 — 승인.
- ② §3.7 빚 억제(`asked_targets`)를 없앤다. 묻는 쪽은 자기 빚을 연 채 `wait` 로 멈췄다가 회신을 받고 갚는다 — 승인(규칙 하나, 장부가 늘 사실).
  `wait` 의 깨움 조건에 "보낸 `message` 요청의 회신 대기" 를 넣는다.
- 예전 세션 resume 호환층 없음.
- id: 세션 할당기 하나, 번호 공간 공유, 모든 배달이 id(키). 순번(`seq`·`queued`·`_debt_seq`·입력 큐 `_seq`) 삭제.

## `monitor`·`wait` 묶음 — 검토 (2026-10-29~30)

근거: 벤치 483 trial — 받아들여진 `wait` 50, 깨지 못하고 과제 시간 끝 13(GLM 7·35B 5·27B 1), 깨어난 기다림 중앙값 130~175s;
`monitor` `add` 는 v10.67.0 뒤 9번(다섯은 작업 종료 감시 — 종료 통지가 이미 함), `run` 1번(값 `"true"`). 설명 길이 monitor 2,545자·wait 279자.
리포트(3판, Fable 리뷰 2회): https://claude.ai/artifact/ApquJovjcvDuKgSCafKRZs

### 결정 (2026-10-30, 사용자)

- **W1** 중간 깨우기: 받아들여진 `wait` 가 5분 안에 깨지 않으면 하니스가 깨운다. 같은 대상(깨울 것 id 집합)을 다시 기다리면
  10·20·40·60분. 알림은 사실 진술("Check-in after 5m — still running: …")이라 진짜 보고와 겹쳐도 거짓이 아니다 — 경합 가드 없음.
  장부 밖(`ledger: "none"`), 영속 안 함, 타이머는 `run_ended(waiting=True)` 에서 걸고 `AgentLoop.run()` 의 `run_started` 로 푼다.
- **P** 진행 신호는 (a) 로그 변화만("+37 lines since your wait" / "unchanged since your wait"). 프로세스 신호(CPU·pid 교체)는 채택 안 함.
- 에이전트를 기다릴 때의 줄: 새 필드 없이 레지스트리가 아는 것(처리 중·대기열·스스로 잠듦·사람 질문에 막힘, wait 이후 걸음 수)으로.
- **`kill -- -<pid>`** 네 자리 통일(꼬리·`shell` 결과·시스템 프롬프트·알림) — pid 는 `sh -c` 리더라 `kill <pid>` 는 셸만 죽인다.
- **M1~M3** `monitor` 문구: 첫머리에 "작업 종료는 이미 통지된다", 대문자 강조 제거, 내부 이름(`match`·`silence`·`command`·중괄호) 전부 인자 짝으로, 초 → "2h".
- **R** `monitor` 의 `run` 삭제. 알 수 없는 인자는 조용히 버려지므로 v10.79.0 한 판은 `validate` 가 명시 거절(v10.79.1 에 뺌).
- **L 선행 PR** (v10.78.1): 메일의 장부 위치를 만드는 쪽이 `ledger` 필드로 선언 — `mail_item` 의 죽은 kind 목록 제거, `"none"` 은 영속 제외. 행동 불변.

순서: L → (W1·P(a)·W2·M1~M3·R·kill 통일) 한 PR → 측정은 지시가 있을 때.

### 구현 (v10.78.1 · v10.79.0)

- v10.78.1 (PR #188): L.
- v10.79.0: W1·P(a)·W2·M1~M3·R·`kill -- -<pid>` 통일 — docs/wait/DESIGN.md §6, docs/monitor/DESIGN.md §13. 테스트
  `tests/test_wait_checkin.py`·`tests/test_monitor_wording.py`.
- v10.79.1: `run` 명시 거절(`RUN_REMOVED`) 제거 — 사용자 결정(안 쓰는 것), 해 분석은 docs/monitor/DESIGN.md §13.
- 측정: 지시가 있을 때 전체 벤치(GLM 2회 이상)에서 — 지표는 docs/wait/DESIGN.md §6.6.

