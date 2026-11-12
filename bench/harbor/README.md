# Harbor harness for agent-cli

[Harbor](https://github.com/harbor-framework/harbor)(Terminal-Bench 2 공식 하니스)에
agent-cli 를 태우는 어댑터. `bench/swebench/` 와 같은 원칙 — **제품 패키지 밖**,
harbor 는 `uv tool` 로 격리 설치, 어댑터는 harbor + stdlib 만 import 한다.

```
[호스트 harbor]                          [태스크 컨테이너]
harbor run … --agent agent_cli_harbor:AgentCli
  setup  → install():  wheel 업로드 ──→  uv tool install (py3.12 격리) + ~/.agent-cli/models.json
  run    → run():      env 주입    ──→  agent-cli run "$AGENT_CLI_TASK" -p openai -m <model> …
                                        세션 → /logs/agent/sessions (AGENT_CLI_SESSIONS_DIR)
                                        종료 코드 → /logs/agent/exit-code.txt (0·1 은 검증으로, 2+ 는 harbor 예외)
  post   ← populate_context_post_run(): turns.jsonl 합산 → 토큰/헬스, history.jsonl → ATIF trajectory.json
  verify → tests/test.sh → reward
```

## 설치 (1회)

```bash
uv tool install harbor          # Python ≥3.12 자동 확보 (호스트 python 과 무관)
python3 -m build --wheel        # dist/agent_cli-<ver>-py3-none-any.whl — 어댑터가 최신 wheel 을 집어감
open -a Docker                  # 데몬 필요 (-e docker 기본)
```

LLM 은 **컨테이너 안에서** 호출되므로 호스트 서버(omlx/vLLM 등)는
`host.docker.internal` 로 접근한다. 모델이 `~/.agent-cli/models.json` 에 등록돼
있으면 컨테이너 안 런타임 프로빙을 건너뛴다(어댑터가 파일을 주입).

## 실행

```bash
cd agent-cli
export PYTHONPATH=bench/harbor
AE="--ae AGENT_CLI_BASE_URL=http://host.docker.internal:8000/v1"

# 스모크 — 단일 태스크 (harbor 저장소 examples/tasks/hello-world 또는 아무 태스크 디렉토리)
harbor run -p <task-dir> --agent agent_cli_harbor:AgentCli -m openai/Qwen3.6-27B-MLX-8bit $AE \
  --ak prompt_template_path=bench/harbor/prompt.j2 --job-name smoke -o bench/runs/harbor

# 레지스트리 단일 태스크
harbor run -t NovitaAI/tb21-file-recovery --agent agent_cli_harbor:AgentCli -m openai/<model> $AE \
  --ak prompt_template_path=bench/harbor/prompt.j2 -o bench/runs/harbor

# Terminal-Bench 2 (89) / 샘플 10
harbor run -d terminal-bench@2.0 --agent agent_cli_harbor:AgentCli -m openai/<model> $AE \
  --ak prompt_template_path=bench/harbor/prompt.j2 --ak max_turns=60 -n 1 --job-name tb2 -o bench/runs/harbor
harbor run -d terminal-bench-sample@2.0 …

# SWE-bench Verified (x86 이미지 — Apple Silicon 에선 QEMU)
harbor run -d swebench-verified@1.0 -l 20 …
```

`-m provider/model` 은 Harbor 관례 — `openai/…` 는 모든 OpenAI 호환 서버, `anthropic/…` 도 가능.
API 키는 `--ae AGENT_CLI_API_KEY=…`. 로컬 모델은 `-n 1~2` 가 현실적(단일 서버).

### 비교 런 — Hermes (`hermes_local.py`)

같은 모델·같은 과제로 [hermes-agent](https://github.com/NousResearch/hermes-agent) 를 돌려
나란히 본다. Harbor 0.24.0 의 내장 Hermes 어댑터는 설치 끝과 버전 조회에 `hermes version`
서브커맨드를 쓰는데 hermes-agent main(2026-10)은 `hermes --version` 플래그만 받아 설치가
exit 2 로 끝난다 — `HermesLocal` 이 그 두 자리만 고친다(`--version` 은 지연 의존성 준비를
건너뛰므로 `hermes sessions && hermes --version` 순서). 0.24.0 은 로컬 0.22.0 과 격리해 `uvx`
로 돌린다(`-t` 가 `org/name` 형식이라 과제 필터는 `-i`):

```bash
cd bench/harbor
export OPENAI_API_KEY=<로컬 서버 키> OPENAI_BASE_URL=http://host.docker.internal:8000/v1
PYTHONPATH=. uvx --from harbor==0.24.0 harbor run -d NovitaAI/tb21-file-recovery \
  --agent-import-path hermes_local:HermesLocal -m openai/Qwen3.6-35B-A3B-8bit -n 1 \
  --job-name tb21-hermes -o ../runs/harbor
# 과제 하나만: -i terminal-bench/<task>
```

Hermes 는 타임아웃 런에 로그를 남기지 않는다(세션 export 가 끝에만). 2026-10-10 결과:
agent-cli(off) 4/9 · agent-cli(on) 4/9 · Hermes 4/9 — 점수는 같고 통과 과제만 다르다
(`bench/runs/harbor/tb21-hermes-35b-a3b`).

### `--ak` (어댑터 kwargs)

| kwarg | 기본 | 설명 |
|---|---|---|
| `max_turns` | 60 | `agent-cli run --max-turns` |
| `response_format` | (해석 체인) | `--response-format json_fc|xml_fc` |
| `max_depth` | (agent-cli 기본) | `--max-depth` |
| `prompt_template_path` | (없음) | Jinja 템플릿(`{{ instruction }}`) — `prompt.j2` 는 비대화형 규칙(`ask` 금지·검증 후 `complete`) 추가 |
| `wheel` | `dist/` 최신(버전 숫자 비교 — 사전순이면 9.24 가 10.27 을 이긴다) | 컨테이너에 설치할 wheel 경로 |
| `models_json` | `~/.agent-cli/models.json` | 주입할 모델 레지스트리 (`""` 면 미주입 → 런타임 프로빙) |
| `python` | 3.12 | uv 가 컨테이너에 확보할 파이썬 |
| `extras` | `image` | wheel 의 extras(쉼표 구분) — `image` 는 Pillow(이미지 축소, v10.61.1 부터 선택 의존성). `""` 면 순정 설치 |

유용한 harbor 플래그: `--agent-timeout-multiplier 2`(느린 로컬 모델), `-i 'glob'`/`-x`/`-l N`
(태스크 선택), `-k N`(시도 수), `--agent-setup-timeout-multiplier`(설치 360s 한도).

## 산출

`bench/runs/harbor/<job>/<trial>/`:
- `result.json` — reward + `agent_result{n_input_tokens, n_cache_tokens, n_output_tokens, metadata}`.
  `metadata` 는 종전 SWE-bench 하니스의 헬스 표와 같은 집계(`turns`·`failures`·`parse_stage`).
- `agent/agent-cli.txt` 전체 출력, `agent/result.txt` 최종 답변, `agent/exit-code.txt` `agent-cli run` 의 종료 코드,
  `agent/sessions/<id>/` 세션 원본
  (`history.jsonl`·`turns.jsonl`), `agent/trajectory.json` ATIF (`harbor view` 로 열람,
  `python -m harbor.utils.trajectory_validator agent/trajectory.json` 로 검증).
- `verifier/` 테스트 출력.

집계: `harbor view bench/runs/harbor/<job>` 또는 `result.json` 을 jq.

### 종료 코드 (agent-cli v10.124.0~)

`agent-cli run` 은 0 답 · 1 실패/무답(LLM 실패·max turns) · 2 설정/사용법 · 130 중단으로 끝난다(종전엔 늘 0).
harbor 0.22.0 은 에이전트 명령을 `set -o pipefail; …` 로 돌려 0 이 아니면 예외를 던지고, 그러면 **검증을 건너뛴다**.
종전 점수는 max_turns·LLM 실패 런도 검증기로 간 결과라, 어댑터(`agent_command.py`)는 **0·1 을 흡수**해 종전처럼
검증기로 보내고 **2 이상**(설정/사용법 오류, 127 명령 없음, 130, 신호)만 종전처럼 harbor 예외로 둔다.
코드 자체는 `agent/exit-code.txt` 에 남는다 — 실패 런을 가를 때 로그를 다시 해석하지 말고 이 파일을 읽는다.

알려진 한계: 잡히지 않은 예외(파이썬 traceback, exit 1)도 1 이라 이제 harbor 예외가 아니라 **검증기 판정**이 된다
(종전엔 pipefail 로 1 이 보여 harbor 예외였다). traceback 은 `agent/agent-cli.txt`, 코드는 `agent/exit-code.txt` 에 있다.

## 테스트

`python3 -m pytest bench/harbor` — ATIF 변환기(`atif.py`, stdlib 전용)·wheel 선택 단위 테스트·
실행 명령(`agent_command.py`)을 실제 `bash -c "set -o pipefail; …"` 로 돌려 가짜 `agent-cli` 의 0·1·2·130 이
0·0·2·130 으로 보이고 `exit-code.txt` 가 맞는지 보는 테스트.
제품 스위트(`tests/`)와 분리. `test_hermes_local.py` 는 harbor 가 import 될 때만 돈다 —
`uvx --from harbor==0.24.0 --with pytest python -m pytest bench/harbor/test_hermes_local.py`.

## 벤치 정책 (2026-11 확정)

- **모델·횟수**: GLM-5.3-Flash(`GLM-5.3-Flash-oQ4-mtp`, 사고 off = `--ae AGENT_CLI_THINKING=off`) 로 기준·후보를 **교차** 2회 이상(base-r1 → cand-r1 → cand-r2 → base-r2). 고정 기준 한 런에 변형들을 대지 않는다 — 같은 main 이 낮 7/9·심야 4/9 를 낸 적이 있다. 모델 서버는 한 번에 한 소비자.
- **gcode-to-text 는 합계에서 빼고 따로 보고한다.** 그림 판독 과제라 하니스 변화를 재지 못하고, 같은 휠 10회 배치가 7·5·5·0·5 (/10) 로 흔들린다(2026-11-06~07). 9과제 합계는 8과제 합계 + "gcode n/3" 로 적는다.
- **동시성**: harbor 기본 `-n 4` 는 omlx 하나를 네 trial 이 나눠 쓴다 — 첫 토큰 대기(프리필)가 과제 예산 900 s 의 55~95% 였다. 통과율이 움직이면 먼저 시간 예산 효과인지 보고, 시간 경계 과제(gcode·video·extract-moves)는 `-n 1` 로 다시 잰다.
- **극단값은 같은 휠을 다른 배치에서 재현부터.** 10회 배치의 0/10 과 7/10 이 모두 재현 런에서 5/10 으로 돌아왔다 — 한 배치의 극단은 코드에 귀속하지 않는다.
- **런 전 확인**: `--ak models_json=…` 파일이 있는지(스크래치의 설정 파일이 사라져 한 런이 전멸한 적 있다). 로그의 `First run` 줄은 설정 누락 신호다.
- **읽는 위치**: `result.json` 의 `verifier_result.rewards.reward`(통과)·`agent_info.version`(실제 돈 휠)·`agent_execution.{started_at,finished_at}`; 행동은 `agent/trajectory.json` 의 steps(`tool_calls`·`metrics.prompt_tokens`). 첫 스텝 `prompt_tokens` 가 두 휠에서 같으면 프롬프트가 같다는 실측이다.

## 주의

- **arm64**: Terminal-Bench 2 이미지의 multi-arch 여부는 태스크마다 다름. SWE-bench 는 x86.
- 매 trial 마다 컨테이너에 uv + wheel 을 설치한다(수십 초). 네트워크 차단(`allowlist`)
  태스크는 `--allow-agent-host` 로 LLM 호스트를 열어야 한다.
- 코드 인덱스 캐시(`.agent-cli/code_index.db`)는 세션과 달리 워크스페이스에 남는다
  (`code_index` 도구 사용 시).

`bench/runs` 는 gitignore 대상. 커밋되는 것은 `agent_cli_harbor.py`·`agent_command.py`·`atif.py`·`prompt.j2`·
`test_agent_command.py`·`test_atif.py`·`wheels.py`·`test_wheels.py`·`hermes_local.py`·`test_hermes_local.py`·이 README.
