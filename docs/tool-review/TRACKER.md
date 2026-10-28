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
| `ask`·`answer`·`message`·`reply`·`wait`·`complete` (묶음) | 승인, 구현 중 | B+C+D+E+id 통일 (아래 §결정) | — |
| `shell` | 구현 | 시간 초과 때 프로세스 그룹째 끝내기(버그 — apt-get 이 dpkg 잠금을 쥔 채 남음), 시간 초과 결과(끝냈다·출력 끝 8줄·`background` 교정), 설명에 결과 모양(`[stderr]`·`[exit code: N]`), 작업 종류 나열 삭제, 거부 문구 표기 | 6ec4b40c |

## TODO

- [x] `shell` 검토·구현
- [ ] 소통·종결 묶음 구현: ① id 통일(내부, 모델 문구 그대로) ② B·C·D·E(모델 문구) — 개정안 https://claude.ai/artifact/3iJsAjeva3YB4wwWNcPdYF
- [ ] 상주 에이전트 둘로 실제 모델 시나리오 확인(요청·회신·알림·사람에게 묻기·wait)
- [ ] 후보(보류): 층 규칙 `complete` → `wait` — 넷이 모두 맞을 때만: 상주 루프(complete 가 아무에게도 보고 안 함), 이 런에 갚지 않은 `r-` 남음, 깨울 원인 있음(회신 대기·작업·monitor), `complete` 가 닫을 `u-`·`n-` 없음. `result` 는 `rewritten_from` 에. 원칙을 "결과 같음"→"이 루프에서 효과 같음"으로 넓히는 일이라, ② 뒤 실제 `complete` 오용을 보고 결정
- [ ] 남은 도구 검토: `code_index`, `python`, `fetch`, `browser`, `monitor`, `schedule`, `snapshot`, `history`, `memory`, `agent`, `run_skill`
- [ ] 도구를 바꾸는 층 규칙 후보: shell `cat`·`head`·`tail`(파일 하나) → `read_file` — 원칙을 "같은 정보"로 넓히는 일이라 결정 필요 (35B 8런 29회, GLM 거의 없음)
- [ ] `plan` 스킬을 실제 모델로 한 번 돌려 확인 (write 면 edit 도 붙는지)
- [ ] 전체 벤치 측정 — 사용자 지시 시 한 번에 (9과제, GLM; 35B 는 그때 결정). 볼 지표: 층이 바꾼 수·같은 이미지 다시 보기·압축 뒤 다시 보기, read_file 모드별·셸 파일 보기 수, heredoc 실패, 같은 파일 다시 쓰기, 줄 번호 ref 수, "You sent:" 뒤 재시도 성공
- [ ] 측정 뒤 회귀면 이미지를 read_file 로 되돌리기(층은 규칙만 비우고 남김)

## 미룬 것·안 하기로 한 것

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
