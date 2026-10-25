# `python` 도구 — 상태가 남는 파이썬 실행 (v10.66.0)

## 1. 왜

v10.62.2 Harbor 벤치(tb21-file-recovery, Qwen3.6-35B-A3B)의 실패 4건 중 3건은
능력 부족이 아니라 **턴 낭비**였다. 궤적을 압축하면:

| 과제 | 호출 | 일어난 일 |
|---|---|---|
| financial-document-processor | 62 | OCR 3번 · `process.py` 를 8번 통째로 다시 씀 · 13 KB `cat` 2번 |
| extract-moves-from-video | 59 | `reconstruct_v1~v6.py` 가 매번 400개 프레임 텍스트를 다시 읽음 |
| large-scale-text-editing | 73 | 샘플 5줄을 20번 다시 만들고 vim 을 20번 띄움 |
| gcode-to-text | 5 | 좌표를 **글자로 읽으려** 서브에이전트 3개 → 타임아웃 |

모델이 데이터를 다루는 유일한 길이 `write_file` 스크립트 + `shell python3` 였기
때문이다: 스크립트가 끝나면 읽은 데이터와 계산 결과가 사라지므로 파싱 한 줄을
고칠 때마다 파일 전체를 다시 쓰고 전처리까지 다시 한다. 반복 차단기(B1)는
이 고리를 못 잡는다 — 호출이 매번 조금씩 다르고 결과도 매번 다르게 틀린다
(기존 런 298건 대입: 같은 호출+같은 결과 3회 규칙이 이번 실패 3건 중 1건을 55/62
번째에서야 잡는다).

## 2. 무엇

Jupyter 셀 하나처럼 코드 조각을 실행하고 **상태를 남기는** 도구.

- 소유자(`main` / `agent:<key>`)마다 자식 프로세스 하나(`tools/_pyworker_child.py`
  를 `python3` 로). 첫 호출 때 뜨고, 변수·import·함수·읽어 둔 데이터가 남는다.
- 관찰 = 출력(stdout+stderr 를 **한 흐름**으로 — 자식 프로세스 출력 포함, 실제 순서)
  + 마지막 식의 값(`=> …`) + **상태 요약 한 줄**
  `state: texts (dict, 12 keys) · parse (function) — still in memory: reuse it,
  do not reload` (20개까지, 타입·크기만). **쓸모 순**: 데이터(컬렉션·긴 문자열·
  배열) → 함수·클래스 → 객체 → 스칼라, 파일 핸들은 뺀다 — 첫 벤치에서 줄의
  40~65% 가 루프 변수·핸들이라 상한에 데이터가 밀려났다. 데이터가 하나라도 있으면
  재사용 힌트를 붙인다. 상태줄은 관찰에만 — 꼬리에는 넣지 않는다(토큰 비용 0).
- 예외는 트레이스백(우리 프레임 제외)을 실패 관찰로; 워커와 상태는 산다.
- 타임아웃(기본 120s)은 **노트북의 Ctrl-C** — 프로세스 그룹에 SIGINT, 자식은
  실행 중에만 `KeyboardInterrupt` 로 받아(쉴 땐 무시) 보통의 실패 회신으로
  돌려준다: "interrupted after 120s — state is kept" + 멈춘 줄의 트레이스백 +
  상태줄. 루프가 dict 에 모으던 결과가 남고 다음 호출이 이어 간다; 띄운 하위
  프로세스(tesseract·vim)도 같이 멈춘다. 3초 안에 안 돌아오면(시그널을 무시하는
  코드) 그때 프로세스 그룹째 죽이고 **상태가 사라졌음**을 들고 있던 이름과 함께
  적는다. 첫 판은 바로 kill 이었다 — §5 의 실측이 바꿨다.
- 열린 matplotlib figure 는 `.agent-cli/tmp/python/fig-*.png` 로 저장해 이미지로
  첨부(read_file 의 이미지 경로 재사용). 좌표·경로 데이터는 그려서 본다.
- `reset: true` 면 새 인터프리터부터.
- 인터프리터: `AGENT_CLI_PYTHON` → PATH `python3` → 이 프로세스. **agent-cli 가
  설치된 인터프리터가 아니라 모델이 shell 에서 쓰는 python3** 와 같은 것 — 그
  환경의 패키지가 거기 있다. 자식 스크립트는 그래서 stdlib 만 쓰고 3.8 문법.

## 3. 어디에

- `agent_cli/pyworker.py` — `PyWorker`(프로세스·직렬 락·타임아웃·kill)·
  `PyWorkerRegistry`(소유자별·`drop_owner`·`stop_all`)·전역 `get/set_py_registry`.
  `jobs.py` 의 형제.
- `agent_cli/tools/_pyworker_child.py` — 자식. JSON 한 줄 프로토콜. fd 1·2 를
  호출마다 임시 파일로 돌리고 프로토콜은 시작 때 복제한 원래 stdout 으로.
- `agent_cli/tools/python_tool.py` — `PythonTool`. `requires_handler="python"`:
  레지스트리(`runtime.build_py_registry`, main.py 의 두 부트스트랩)가 선 세션에서만
  광고. `AGENT_CLI_PYTHON_TOOL=0` 이면 안 선다 → 도구가 프롬프트에서 빠진다.
- 수명: `teardown_session` 이 `stop_all`, 에이전트 종료가 `drop_owner`
  (`agents_live._drop_monitors` — 전역으로 찾는다: 배달이 없어 배선할 양방향이 없다).
- 스냅샷 훅 `MUTATING_TOOLS` 에 포함 — python 도 파일을 바꾼다.
- 봉쇄: 코드 안의 절대경로·`../` 문자열 리터럴을 `_confine.guard` 에 — shell 과
  같은 best-effort(변수 조합 경로는 사각지대, 문서됨).
- 프롬프트(v10.73.0, docs/tail/DESIGN.md — 역할이 자리를 정한다): 도구 설명(머리)은
  **성질만**(상태가 남는다·결과 모양·그림은 이미지·타임아웃). 꼬리 Task Guidelines 는
  **경로 선택 한 문장** — 이득이 나는 일의 성질로(비용 큰 단계 뒤 다음 단계를 반복 시도하는 일은 `python` 으로, shell 에서 돌린 파이썬 아님; 작업 종류·실행 파일 이름은 쓰지 않는다).
  쓰는 법(한 번 읽고 함수만 고쳐라·한 호출 = 한 단계·긴 루프는 dict 에 모아라·그려서
  봐라·산출물은 파일로)은 인터프리터에 상태가 있는 동안만 뜨는 `## Python session —
  holding: …` 블록(`tail_block`, 입력은 `PyWorkerRegistry.state_of`). 지난 턴에 shell
  로 파이썬을 돌렸으면(`shell_runs_python`) 교정 한 줄 `SHELL_PYTHON_NOTICE`.
  종전(v10.66.0~10.72.0)엔 설명과 꼬리 줄에 같은 네 문장이 있었다 — §5.4.
- ModuleNotFoundError 관찰(v10.73.0): `missing_module_hint` 가 워커의 인터프리터 경로(`PyWorker.interpreter`)와 `<exe> -m pip install <pkg>`(import 이름≠배포 이름은 `_PKG_FOR_MODULE`; pip 없으면 `ensurepip`)를 붙인다. 벤치 컨테이너는 PATH 에 python3 가 없어 도구가 uv 의 agent-cli venv 를 쓰는데 모델은 PATH 의 pip·uv 기본 cpython 에 깔았다(pyguide r2 financial t3~t22, 그 뒤 shell 로 회귀). 어느 인터프리터인지 모델이 알 길이 없으니 빠진 순간 알린다.
- 웹: 카드 요약은 첫 줄, 펼치면 `>>> ` 셀.

## 4. 측정

고정 worktree 둘(도구 없음 = `AGENT_CLI_PYTHON_TOOL=0` / 있음), 같은 wheel 방식.
지표: 통과율 · 호출 수 · 소요 시간 · **python 을 실제로 쓴 런 비율**. 결과는 이
문서 §5 에 적는다.

## 5. 결과

### 5.1 첫 판 (2026-10-22, 3과제 × 3회, thinking off, Qwen3.6-35B-A3B-8bit)

| | 후보 v10.66.0 (python) | 기준 v10.64.1 |
|---|---|---|
| 통과 | 0/8 (1런은 apt 해시 불일치로 무효) | 0/9 |
| 호출 수 중앙값 | 60 | 62 |
| 평균 런 시간 | 14.2분 | 19.5분 |
| 30분 타임아웃(video) | 1/3 | 3/3 |
| 부분 통과 최고 | financial 3/7 · large-scale 4/5 | financial 6/7 ×2 · large-scale 4/5 |
| python 채택 | 8런 중 7런 (4~50회) | — |

통과율은 차이가 없다 — 세 과제는 이 모델이 원래 못 푸는 것(5/9 세트의 실패 중
골랐다)이라 0 대 0 은 예상 범위. 시간은 후보가 짧지만 4~7분에 틀린 답으로 끝낸
런이 섞여 있다. financial 은 기준이 더 가까이 갔다(6/7 두 번; 후보는 CSV 를 틀리게
쓰거나 20분 안에 못 썼다). n=3 이라 어느 쪽도 결론이 아니다.

**도구 사용 패턴** (`history.jsonl` 분석): 상태 재사용은 높다(video 49/50 호출,
large-scale 41/45) — 모델은 "변수가 남는다" 를 이해한다. 그러나 셀은 길고(중앙값
2~3 KB, 최대 9 KB), 재임포트가 많고(35/45), 같은 파일을 다시 읽고(런당 최대 61번),
`state:` 줄의 40~65% 가 루프 변수·파일 핸들이었다.

**도구가 실제로 해를 끼친 곳 — 타임아웃 kill**: 3런에서 5번, 그중 4번이 95프레임
OCR 루프(3~5분이 정상)였다. 120s 에 죽으면서 모으던 결과까지 사라졌고, 모델은
같은 코드를 다시 보내 또 죽거나(video KWuX ×2) 셸 스크립트 파일로 후퇴했다(video
NpbP). 노트북이라면 Ctrl-C 뒤에도 `results` 에 37장치가 남아 있어야 한다.

### 5.2 첫 판이 바꾼 것 (v10.66.0)

1. 타임아웃 = SIGINT, 상태 유지 (§2). 시그널을 무시하는 코드만 kill.
2. 기본 120s 는 그대로 — 끊김이 싸졌고, 모델은 긴 단계에 `timeout: 300` 을 스스로
   쓴다(whisper 로드).
3. `state:` 줄 쓸모 순 정렬·핸들 제외·재사용 힌트 (§2).
4. 가이드라인 한 줄 보탬: 한 호출 = 한 단계, 긴 루프는 dict 에 모아라.

넣지 않은 것: 타임아웃 뒤 자동 재시도, 셀 길이 제한, 재임포트 경고 — 측정 없이
쌓는 행동 교정은 노이즈다. 다음 판에서 "타임아웃 kill 0회 · OCR 재실행 0회" 를
세고, 이전 5/9 통과 세트로 **퇴행 없음** 을 본다.

### 5.3 둘째 판 — 9과제 풀런 (2026-10-23, v10.66.0, 각 1회, thinking off)

job `tb21-35b-a3b-off-v10.66.0`. 비교 대상은 직전 풀런 v10.62.2(python 도구 없음).

| | v10.62.2 | v10.66.0 |
|---|---|---|
| 통과 | 5/9 | **5/9 — 같은 세트**(db-wal·extract-elf·log-summary·multi-source·regex-log) |
| 총 시간 | 85분 | **66분** |
| financial 부분 점수 | 1/7 | **6/7** (16.3분 → 5.3분) |
| gcode 부분 점수 | 0/2 | **1/2** (16.5분 → 7.1분) |
| video · large-scale | 60턴 한도 | 60턴 한도 (같음) |
| python 타임아웃 kill | — | **0** |
| python 인터럽트 | — | 1 (아래) |
| OCR 재실행 | — | **0** |
| `complete` 되돌림 | 0 | **0** |

- 유일한 인터럽트는 video 에서 모델이 `timeout: 300` 을 주고 돌린 tesseract 루프.
  끊긴 뒤 `frames (190 items) · all_text (183 items)` 가 그대로 남았고 다음 셀이
  거기서 이어 갔다 — §5.2 의 1번이 노린 모양 그대로다.
- python 채택 9/9 런, 상태 재사용 142/155(무시 3). 재임포트(extract-elf 25/26)와
  재읽기(gcode 29회 — 자기 python 출력의 oversized 파일을 다시 읽음)는 여전하다.
  첫 판의 결론대로 행동 교정은 넣지 않는다.
- 1회 런이라 부분 점수 차이는 변동 범위 안일 수 있다. 확실한 것은 **퇴행 없음**
  과 **도구가 해를 끼친 지점 0** 이다.

### 5.4 가이드의 자리 (2026-10-25, v10.73.0 — docs/tail/DESIGN.md §4)

꼬리 Task Guidelines 의 python 줄을 "도구 설명과 같은 문장이라 중복" 으로 보고
지운 변형(PR #171, 폐기)의 A/B(35B, 9과제 × 2회): python 호출 266 → 177, 그 감소가
extract-elf(23·32 → 0·4)·financial(23·7 → 0·0)에 몰렸고 대신 shell 안 인라인
python 이 9 → 32건, .py 작성 후 실행 4건. shell 안 python 의 Traceback 은 shell
관찰이 success 라 하니스에 실패로 안 잡힌다(2/46 → 10/74). financial cand-r1 은 .py
를 다시 돌려 CSV 를 0 으로 덮었다 — REPL 셀이었으면 없었을 일.

결론: **머리의 설명은 사용 가이드를 대신하지 못한다.** 설명은 성질만, 경로 선택은
꼬리 상시 한 줄, 쓰는 법은 상태가 있는 동안의 블록, 잘못된 경로 직후엔 교정 한 줄.
이 판의 측정은 docs/tail/DESIGN.md §5 — r3 7/9(base 5·6/9), 출력 토큰 2/3, 인터프리터 안내는 gcode 에서 2회(모델은 설치 대신 우회).
