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
- 인터프리터: PATH 의 `python3`, 없으면 `python` — 그것뿐(v10.78.0, §7). **agent-cli
  가 설치된 인터프리터가 아니라 모델이 shell 에서 쓰는 파이썬**과 같은 것 — 그
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
- ModuleNotFoundError 관찰(v10.73.0): `missing_module_hint` 가 워커의 인터프리터 경로(`PyWorker.interpreter`)와 `<exe> -m pip install <pkg>`(import 이름≠배포 이름은 `_PKG_FOR_MODULE`; pip 없으면 `ensurepip`)를 붙인다. 벤치 컨테이너는 PATH 에 python3 가 없어 도구가 uv 의 agent-cli venv 를 쓰는데 모델은 PATH 의 pip·uv 기본 cpython 에 깔았다(pyguide r2 financial t3~t22, 그 뒤 shell 로 회귀). 어느 인터프리터인지 모델이 알 길이 없으니 빠진 순간 알린다. 그 venv 로 떨어지는 길 자체는 v10.78.0 에서 없앴다(§7).
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


## 6. import 경로 (v10.77.1)

워커는 `python3 -u _pyworker_child.py` 로 뜬다. 스크립트로 뜨면 `sys.path[0]` 은 **스크립트의
폴더**(agent_cli/tools/)라, 작업 폴더의 모듈은 import 되지 않고 `import shell`·`import history`
같은 이름은 하니스의 도구 모듈을 집었다. 라이브 GLM(v10.77.0 S4): writer 가 방금 쓴 `fib.py` 를
`import fib` 하자 ModuleNotFoundError 와 함께 "`pip install fib`" 안내가 나갔고, 모델은 `shell` 로
돌아갔다. 대화형 python3 와 같게 `sys.path[0] = ""`(지금 폴더, 셀의 `os.chdir` 를 따라감)로 둔다.
테스트: `tests/test_python_tool.py::TestImportPath` (고치기 전 코드에서 셋 다 실패 확인).

## 7. 도구 검토 (v10.78.0, docs/tool-review/TRACKER.md)

근거: 보관된 harbor 벤치의 `python` 호출 전부(GLM 568·35B 2,469). 개정안과 Fable 리뷰 세 번을 거쳤다.

- **셀은 진짜 `__main__` 모듈에서.** dict 이름 공간이라 셀 함수의 `__module__` 은 `"__main__"`
  인데 `sys.modules["__main__"]` 은 자식 스크립트였다 — pickle(fork `Pool`)·`unittest.main()`
  ("Ran 0 tests")·`doctest`·dataclass 의 문자열 `ClassVar`·`namedtuple` pickle 이 셀의 이름을
  못 찾았다(GLM v10.77.1 r2b video: 남은 OCR 을 `Pool` 로 나누다 PicklingError → shell 로 이탈).
  `types.ModuleType("__main__")` 을 `sys.modules` 에 두고 그 `__dict__` 에서 돈다(Jupyter 와 같다).
  **한계**: spawn·forkserver(macOS 기본, Linux 3.14+ 기본)의 `Pool` 은 자식이 셀의 `__main__` 을
  다시 import 하지 못해 멈춘다(예전엔 즉시 PicklingError) — 타임아웃이 끊고 상태는 산다. start
  method 를 fork 로 강제하지 않는다(macOS 의 cv2·BLAS 스레드와 fork 의 궁합).
- **실행 전 SyntaxError 는 셀의 줄만.** `_split_last_expr` 의 `ast.parse` 에서 난 SyntaxError 는
  하니스의 `ast.py` 프레임으로 시작했다(벤치 27건 중 26건) → `format_exception_only`. 실행 중의
  SyntaxError(방금 쓴 `.py` 의 import)는 그대로 traceback 전체(`<python>` 줄이 필요하다).
- **인터프리터는 PATH 의 `python3`, 없으면 `python`.** `AGENT_CLI_PYTHON`(아무도 설정하지 않았고,
  다른 파이썬을 가리키면 shell 과 다시 갈라진다)과 agent-cli 자신의 인터프리터로 떨어지는 단계를
  지웠다. 벤치 video·financial 이미지에는 처음에 `python3` 가 없어, 모델이 `apt-get` 보다 `python`
  을 먼저 부르면 워커가 pip 없는 uv venv 로 떠 고정됐다(GLM 4·35B 2 trial) — 나중에 깔린
  `/usr/bin/python3` 의 cv2 를 못 봤고, 안내대로 venv 에 pip 을 부르다 실패해 shell 로 갔다. 이제
  둘 다 없으면 띄우지 않고 "없다 — 설치하면 다음 호출부터"(`NO_INTERPRETER`) 를 돌려준다. 워커는
  뜰 때마다 다시 고르니 그대로 된다. PATH 는 하니스 프로세스의 것이라 셸의 `export PATH=` 로는
  안 바뀐다(apt 처럼 `/usr/bin` 에 놓이는 설치는 보인다). 워커가 이미 POSIX 전용이라 Windows 의
  이름은 찾지 않는다. 특정 venv 는 활성화한 채 agent-cli 를 띄우면 PATH 로 같아진다.
- **시작 인사.** 자식이 루프 전에 `{"ready": true}` 를 보낸다. 인사 전에 죽으면 이 자식을 돌릴 수
  없는 파이썬(3.8 미만·Python 2 인 `python`·버전 없는 pyenv shim·Xcode 스텁) → `STARTUP_FAILED`
  (경로와 "3.8 이상"). 인사 뒤에 죽으면 코드가 프로세스를 끝낸 것(`os._exit`, 네이티브 segfault)
  → "exited while running this code", 들고 있던 상태가 있을 때만 "its state is gone". 예전엔 둘
  다 "exited unexpectedly — its state is gone" 이라 새 워커에서 잃지 않은 상태를 잃었다고 했다.
- 문구: `timeout` 인자의 작업 종류 나열("a model load, a big OCR batch") 삭제, `reset` 을 문장으로,
  SystemExit 는 사실만(`SystemExit(2)`·정수만 괄호, unittest 의 bool 은 정수로, 문자열은 이미 출력에).
  패키지 짝 `pdfminer`→`pdfminer.six`(테스트가 오답을 고정하고 있었다)·`whisper`→`openai-whisper`·
  `skimage`→`scikit-image`. 쓰이지 않던 `STATE_MAX` 삭제.
- 그대로: 도구 설명, 상태 줄과 재사용 힌트(94~98% 에 붙어 신호는 약하지만 빼서 나아지는지는 측정이
  정한다), 꼬리 블록.
- 측정 지표(전체 벤치 때): pickle 계열 오류, 모듈 없음 안내 뒤 다음 호출 성공률·shell `python3 -c`
  이탈, `NO_INTERPRETER` 수와 설치까지의 호출 수, 안내 뒤 pip 실패 사유(PEP 668
  `externally-managed-environment` — 모델이 스스로 `--break-system-packages` 를 붙인다, 95건),
  `timeout` 인자 사용 수.
- 테스트: `tests/test_python_tool.py` 의 `TestMainModule`·`TestSyntaxError`·`TestSystemExit`·
  `TestInterpreterChoice` 와 죽음 문구 둘 — 고치기 전 코드에서 15건 실패 확인.

## 8. 한도를 넘긴 셀은 작업이 된다 (v10.91.0)

계기: GLM 벤치 base-r2 extract-moves — 479장 OCR 셀이 120초 인터럽트로 끊기자 모델이 `timeout: 110`(기본값보다 작다)을
일곱 번 넣고 90장씩 돌리며 매번 끊겼다. 8턴 16분. 힌트("pass a larger `timeout`")는 첫 번째와 여덟 번째가 같은 문장이었고,
사용법 규칙 "긴 루프는 dict 에 누적해 타임아웃에도 보존" 이 잘게 재시도하는 쪽으로 밀었다. 사용자 결정(2026-10-31):
shell 과 같은 식으로 — 한도 뒤에는 기다리지 않고 작업이 된다.

- **`timeout` 의 기본값이 상한이다** (`DEFAULT_TIMEOUT` = 120; 더 큰 값은 상한으로 깎고 결과에 적는다). 한도 안에
  회신이 없으면 `PyWorker.run(pend=True)` 이 SIGINT 대신 `PendingCell` 을 돌려주고, `PythonTool._run` 이 그것을
  `JobRegistry.adopt` 로 작업으로 만든다. 결과는 즉시: "python: the step is still running after 120s and continues as
  job #3 (pid P) — the interpreter is busy until it ends. You will be told when it ends, with its output and the `state:`
  line; its log is .agent-cli/tmp/python/main.log. … To stop it: `kill -INT -- -P` (the state is kept)."
- **몸** (`pyworker.PendingCell`, `jobs.JobHandle` 구현): 워커의 회신 읽기(`_LineRead`)를 그대로 쥐고, 레지스트리의
  `poll` 때 회신이 와 있으면 결과를 만들고(`PyWorker._parse` — 상태 줄 갱신) 워커를 풀어 준다. `stop` 은 그룹에 **INT**
  (자식의 `KeyboardInterrupt` 가 셀만 끊고 상태는 남는다 — 모델의 `kill -INT -- -P` 도 같은 길), `force` 는 인터프리터째.
  `report_lines` 가 통지의 로그 꼬리 뒤에 `=> 값`·예외(인터럽트면 "stopped on interrupt — … state is kept")·그림 경로·
  `state:` 줄을 덧붙인다.
- **바쁨**: 작업이 도는 동안 같은 소유자의 `python` 은 받지 않는다 — "python: the interpreter is busy with job #3 (2m10s
  so far) — `wait` for its notice, or stop it with `kill -INT -- -P` (the state is kept)." 인터프리터가 하나라 직렬이다.
- **로그** (`.agent-cli/tmp/python/<owner>.log`, 소유자당 하나, 호출마다 비우고 머리줄 `$ python: <첫 줄>`): 자식은
  요청의 `capture` 경로를 fd 1·2 로 두고(종전 임시 파일 자리) 이 호출이 쓴 구간만 `out` 으로 돌려준다. 셀이 작업이 되면
  이 파일이 그 작업의 로그라 `wait` 의 중간 깨우기 진행 신호(`log_growth`)와 종료 통지의 꼬리 8줄이 여기서 나온다.
  작업 레지스트리가 없는 런은 캡처 없이 종전대로(한도에 SIGINT, 상태 보존).
- 그대로: 인터럽트 경로(레지스트리 없음·모델이 멈출 때), `timed_out`(시그널 무시 → kill → 소실 안내), 그림 자동 캡처,
  `reset`, 상태 줄.
- 테스트: `tests/test_python_tool.py` `TestPendingJob`(작업화·로그 진행·바쁨·통지 본문·INT 멈춤과 상태·상한 초과 안내·
  도중 사망·빠른 셀은 작업 아님과 로그 교체·레지스트리 없음은 인터럽트).
- 다음 벤치 지표: python 연속 인터럽트 0, `continues as job` 수와 그 뒤 `wait` 사용, busy 거부 수.
- **라이브 확인 (v10.91.1, 웹 방 g312ma)**: 160초 셀 → 120초에 job #1, 모델이 바로 `wait`, 통지에 출력 꼬리 8줄·`=> 120`·`state: total (120)`, 그 통지로 답. 끝나지 않는 셀 → job #2, 도는 동안 `len(acc)` 는 바쁨 거절(`wait`·`kill -INT -- -P` 안내), 모델이 `shell` 로 `kill -INT` → 셀만 끊기고 다음 `len(acc)` 가 `=> 279`(상태 생존), 끝 통지 rc=1 "stopped on interrupt — … state is kept". 고친 것 하나: 경과 시간을 작업으로 넘어간 때부터 셌다(160초 셀이 "after 40s", 바쁨 "10s so far") — `PendingCell.started_at` 은 셀을 보낸 시각, `JobRegistry.adopt(started_at=)` 가 그것을 작업의 시작으로 쓴다. 재확인(v10.91.2): 150초 셀 바쁨 "2m11s so far"·통지 "after 2m30s"·`=> 150`. 고친 것 하나 더: 바쁨으로 거절될 호출이 먼저 캡처 로그를 비우고 머리줄을 자기 것으로 바꿨다(돌고 있는 셀의 `tick 0~13` 소실, 머리 `$ python: 1+1`) — 바쁨 거절은 `_capture_log` 전에 한다.
