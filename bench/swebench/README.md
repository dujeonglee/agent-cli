# SWE-bench harness for agent-cli

agent-cli를 [SWE-bench](https://www.swebench.com/)에 적용하는 어댑터.
**제품 패키지 밖**(`bench/`)에 있고 별도 venv를 쓴다 — `pyproject` 의존성에
swebench를 더하지 않는다(on-prem 최소 의존 원칙). 어댑터는 설치된 `agent-cli`
명령을 subprocess로 호출하므로 제품 env와 분리된다.

```
[인퍼런스 — run_inference.py]            [eval — 공식 swebench 하니스]
repo@base_commit 체크아웃         ──→     predictions.jsonl 적용 + 테스트
agent-cli run "<issue>" (cwd=repo)         → resolved / unresolved
git diff → predictions.jsonl
+ turns.jsonl 헬스 캡처
```

## 설치 (1회)

```bash
python3 -m venv bench/.venv
bench/.venv/bin/pip install swebench datasets
```

`agent-cli`가 PATH에 설치돼 있어야 하고(`pip install -e .`), 프로바이더
(`~/.agent-cli/config.json`)가 설정돼 있어야 한다.

## 1) 인퍼런스 (패치 생성 — Docker 불필요)

```bash
# 스모크: django 5개
bench/.venv/bin/python bench/swebench/run_inference.py \
  --repo django/django --n 5 --max-turns 25 --out bench/runs/smoke

# Lite 전체 300개
bench/.venv/bin/python bench/swebench/run_inference.py \
  --dataset princeton-nlp/SWE-bench_Lite --n 300 --out bench/runs/lite

# Verified
bench/.venv/bin/python bench/swebench/run_inference.py \
  --dataset princeton-nlp/SWE-bench_Verified --n 500 --out bench/runs/verified
```

옵션: `--n`(개수) `--repo`(단일 repo 필터) `--instances id1,id2`(명시)
`--max-turns` `--timeout`(인스턴스당 wall초) `--model`(agent-cli 모델 override)
`--model-name`(예측 레코드 라벨).

산출: `<out>/predictions.jsonl` (`{instance_id, model_name_or_path,
model_patch}`) + `<out>/health.json` (인스턴스별 턴·형식실패·patch 크기·타임아웃).

repo는 `bench/.cache/repos/`에 캐시되어 재사용된다.

## 1-B) 인퍼런스 — 컨테이너 인-에이전트 (`run_inference_container.py`)

A는 호스트 bare 클론에서 "눈감고" 패치를 쓴다(테스트 실행 불가). **B는
agent-cli를 인스턴스 SWE-bench 컨테이너 *안*에서 돌려** repo+deps+테스트
환경이 준비된 상태로 작업한다 — 에이전트가 테스트를 돌리며 고칠 수 있다.

```bash
bench/.venv/bin/python bench/swebench/run_inference_container.py \
  --instances django__django-10914 --out bench/runs/smokeB
# 또는 --repo django/django --n 5
```

인스턴스마다: env/instance 이미지 빌드 → 컨테이너 기동 → `git checkout
base_commit` + `pip install -e .`(testbed env) → **별도 `agentcli` conda
env(py3.11) 생성 + agent-cli wheel 설치**(repo env가 옛 파이썬이라 분리
필수) → testbed 활성 상태에서 agent-cli를 agentcli 절대경로로 실행(그 `shell`
테스트는 testbed py로, agent-cli 자신은 py3.11로) → 프로바이더는
`host.docker.internal:8000` 로 호스트 MLX 접근 → `git diff` → predictions.

**Apple Silicon**: 인스턴스 이미지가 x86이라 **QEMU 에뮬레이션**으로 돈다
(django-10914 에이전트 226s). 대규모는 느리니 네이티브 arm64 빌드 또는 x86
리눅스 권장. wheel은 `dist/`에서 자동 빌드·컨테이너로 복사.

검증: django-10914 — 16턴·형식실패 0, 정답 패치, **eval resolved 1/1**.

## 2) eval (공식 하니스 — Docker 필요)

```bash
open -a Docker     # 데몬 시작 (macOS)

bench/.venv/bin/python -m swebench.harness.run_evaluation \
  --dataset_name princeton-nlp/SWE-bench_Lite \
  --predictions_path bench/runs/smoke/predictions.jsonl \
  --max_workers 2 \
  --run_id smoke
```

**Apple Silicon(arm64) 주의**: 공식 이미지는 x86 기반이라 일부 인스턴스만
arm64 이미지가 있다. 부재 인스턴스는 빌드 실패/스킵될 수 있다 — 스모크엔 무방.
점수가 중요하면 x86 리눅스에서 eval 권장.

## 3) 리포트

```bash
bench/.venv/bin/python bench/swebench/report.py --out bench/runs/smoke
```

resolved 점수 + **agent-cli 운영 헬스**(형식실패율·복구·parse_stage·빈패치·
타임아웃)를 한 표로. 후자가 이 하니스의 핵심 수확 — 대규모에서 agent-cli의
견고성 측정.

## 정리

`bench/.venv` `bench/.cache` `bench/runs`는 모두 gitignore 대상(로컬 산출물).
스크립트(`run_inference.py` `report.py` `README.md`)만 커밋된다.
```
