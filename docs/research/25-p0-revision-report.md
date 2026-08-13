# P0 수정 및 재실험 보고서

**대상 리뷰:** `24-review-response.md`  
**반영 논문:** `09-full-paper-draft.md`, `09-full-paper-draft.ko.md`  
**실행일:** 2026-08-13  

## 1. 무엇을 바꿨는가

### P0-1 — torn-write 의사반복 제거

기존 `9/110 대 0/361` 분석은 한 실행 안의 2 ms snapshot을 독립 시행처럼
취급했다. 이를 폐기하고 한 임시 workspace·새 프로세스의 전체 trace를 한
실험 단위로 바꿨다. 반복마다 no-gate/workspace/conflict 세 팔의 순서를
무작위화했고, 팔마다 30회씩 실행했다. 결과도 다음 세 층으로 분리했다.

1. 게이트에 참여한 writer 임계영역이 실제로 겹쳤는가
2. 게이트 밖 reader가 mixed/broken 상태를 한 번이라도 보았는가
3. 두 writer 종료 후 최종 파일이 온전한가

1/2/5/10 ms sampling 민감도도 추가했다. snapshot 개수는 노출 trace를 설명할
뿐 검정 표본으로 쓰지 않는다.

주 2 ms 결과는 다음과 같다.

| 결과(run 단위) | 게이트 없음 | workspace | conflict |
|---|---:|---:|---:|
| 참여 writer 중첩 | 30/30 | 0/30 | 0/30 |
| 외부 mixed/broken 노출 | 27/30 | 0/30 | 0/30 |
| mixed/broken 최종 파일 | 3/30 | 0/30 | 0/30 |
| 빈/부분 상태 노출 | 29/30 | 30/30 | 29/30 |

외부 mixed/broken 노출의 exact McNemar p는 no-gate 대 어느 lock에서도
`1.49 × 10⁻⁸`이다. 1/5/10 ms에서 no-gate 노출은 각각 24/30, 24/30,
13/30이고 두 lock은 모두 0/30이었다.

마지막 행은 중요한 negative result다. writer 순서화는 성립하지만 direct
overwrite를 읽는 외부 reader의 원자성은 성립하지 않는다. 논문은 이제 이 둘을
같은 “physical integrity”로 묶지 않는다.

### P0-2·3 — 의미 오염의 분석 단위와 과제 정답 판정

두 동시 턴은 workspace, context, endpoint를 공유하므로 둘을 묶은 run/pair를
실험 단위로 바꿨다. off/on 팔은 반복마다 선행 순서를 교대하며, run-level exact
binomial 95% CI와 exact McNemar 대비를 사용한다. turn count는 기술 통계다.

`ownComplete`라는 과도한 이름을 제거하고 네 층을 별도로 기록한다.

| 층 | 판정 |
|---|---|
| 구조 귀속 | `reply_to` |
| 효과 소유권 | 턴별 파일 경로와 shell 명령 |
| 과제 정답 | 지정 경로 전체 + 파일별 exact content oracle |
| 최종 저장소 | 두 턴 종료 후 모든 파일을 oracle로 다시 검사 |
| 응답 초점 | 상대의 사전 정의된 literal 완료 태그가 응답에 있는지 |

라이브 20쌍은 Qwen3.6-27B-MLX-8bit에서 서로 다른 parser/CLI 문서 과제로
실행했다. 모든 40개 run이 첫 시도에 완료됐고 누락은 없었다.

| 결과(run/pair 단위) | scoping off | scoping on | paired exact p |
|---|---:|---:|---:|
| 어느 턴이든 상대 과제 경로를 건드림 | 20/20 | 0/20 | 1.91 × 10⁻⁶ |
| 두 턴 모두 지정 target path 전체 기록 | 15/20 | 20/20 | .0625 |
| 두 과제 모두 exact-content 정답 | 15/20 | 20/20 | .0625 |
| 최종 저장소 전체 정답 | 15/20 | 20/20 | .0625 |
| 어느 응답이든 상대 완료 태그 언급 | 15/20 | 1/20 | .000122 |

교차 경로 비율의 exact 95% CI는 off 83.16–100%, on 0–16.84%다. nested
turn 기술 통계는 경로 침범 27/40 → 0/40, exact 자기 과제 정답 35/40 →
40/40이었다. 경로 coverage와 content 정답 수는 이번 표본에서 같았지만 별도
지표로 유지했다. 자기 과제를 정확히 하면서 남의 과제도 수행한 run이 있었기
때문이다.

스코핑은 이 endpoint에서 큰 완화 효과를 냈지만 격리 보장은 아니다. scoped
응답 하나가 상대 태그를 언급했고, 과거 두 번째 모델 trace에서는 run 단위 경로
침범이 11/20 → 8/20(p=.508), turn 단위 응답 태그가 12/40 → 10/40으로
남았다. 이 과거 trace는 exact-content oracle이 없어 보조 증거로만 썼다.

### P0-4 — effect gate의 실제 보장과 구현 일치

- 상대/절대 및 `.`/`..`뿐 아니라 symlink와 symlink parent를 canonical path로
  합친다.
- 기존 hard link가 감지되면 좁은 경로 키를 신뢰하지 않고 workspace-exclusive로
  내린다.
- `UNKNOWN_WORKSPACE_EFFECT`(미분류, fail-closed 배타)와
  `NON_WORKSPACE_OR_COMPOSITE`(명시적 비작업공간/복합)를 분리했다.
- 새 plugin/tool이 intent를 빠뜨리면 무잠금이 아니라 workspace-exclusive다.
- empty path, symlink, hard link, cyclic symlink, plugin omission, rename TOCTOU,
  detached work를 adversarial regression suite에 넣었다.
- 논문의 guarantee 표를 A1–A5 가정 아래 강제되는 invariant 표로 바꿨다.

보장 문장은 다음 범위로 좁혔다.

> All participating in-process effects that declare stable, correctly
> classified resources do not overlap according to the compatibility matrix;
> direct overwrite visibility to external readers is not atomic.

## 2. 재현 명령

```bash
.venv/bin/python bench/multiuser/e1_ablation.py \
  --reps 30 --k 8 --size-kib 128 --sample-ms 1 2 5 10

.venv/bin/python bench/multiuser/n3c_scoping_real.py \
  --workload realistic --arms both --reps 20 --retry 1

.venv/bin/python bench/multiuser/stats_recompute.py
.venv/bin/python bench/multiuser/verify_paper_claims.py
env -u NO_COLOR .venv/bin/pytest -q
```

## 3. 산출물

- `bench/multiuser/out/e1-ablation-p0.jsonl`: 360개 독립 run 원자료
- `bench/multiuser/out/e1-ablation-p0.json`: run-level 요약·CI·paired 대비
- `bench/multiuser/out/n3c-realistic-p0.jsonl`: live semantic 원자료
- `bench/multiuser/out/n3c-realistic-p0.json`: 다층 판정·run-level 요약
- `bench/multiuser/out/stats-recompute.json`: 논문 통계 재계산
- `bench/multiuser/verify_paper_claims.py`: 논문 수치와 원자료의 일치 검증

## 4. 검증 결과

- P0 effect/metrics 회귀검사: 76 passed
- 전체 Python suite(`NO_COLOR` 해제): 3,596 passed, 35 skipped, 0 failed
- 정적 검사: 변경 Python 파일 ruff 통과
- 원자료-논문 수치 대조: 174 checked, 0 mismatches
- `git diff --check`: whitespace 오류 없음
