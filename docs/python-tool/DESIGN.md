# `python` 도구 — 상태가 남는 파이썬 실행 (v10.65.0)

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
  `state: texts (dict, 12 keys) · parse (function)` (20개까지, 타입·크기만).
  상태줄은 관찰에만 — 꼬리에는 넣지 않는다(토큰 비용 0).
- 예외는 트레이스백(우리 프레임 제외)을 실패 관찰로; 워커와 상태는 산다.
- 타임아웃(기본 120s)이면 프로세스 그룹째 죽이고 **상태가 사라졌음**을 들고 있던
  이름과 함께 적는다. 다음 호출이 새 워커를 띄운다.
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
- 프롬프트: Task Guidelines 한 줄 — 데이터 작업은 `python` 으로, 한 번 읽고 함수만
  고쳐라, 그려서 봐라, 산출물은 파일로.
- 웹: 카드 요약은 첫 줄, 펼치면 `>>> ` 셀.

## 4. 측정

고정 worktree 둘(도구 없음 = `AGENT_CLI_PYTHON_TOOL=0` / 있음), 같은 wheel 방식.
지표: 통과율 · 호출 수 · 소요 시간 · **python 을 실제로 쓴 런 비율**. 결과는 이
문서 §5 에 적는다.

## 5. 결과

(측정 후 기록)
