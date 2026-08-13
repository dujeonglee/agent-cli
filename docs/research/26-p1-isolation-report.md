# P1 turn-local 격리 구현 및 재실험 보고서

> 상태: 구현, 결정론적 검증, 고정 source digest의 20-block 실모델 재수집,
> 논문 반영 완료.

## 1. 구현한 강제 경계

P1은 모델이 지시를 잘 따를 것이라는 기대와 파일 게시 권한을 분리한다.

1. `ContextManager.turn_scope`가 레코드에 `origin_turn`을 붙이고, turn-local
   snapshot은 다른 진행 중 turn의 레코드를 제외한다. 완료된 turn은 다음 LLM
   호출부터 다시 보인다. 출처를 나눌 수 없는 공유 compaction 요약도 다른
   turn을 제외하는 동안 보수적으로 숨긴다.
2. 요청자는 `write_paths` manifest와 같은 키의 `expected_contents` oracle을
   `/api/input`에 보낼 수 있다. manifest는 canonical path로 바꾸어 turn id에
   결합한다.
3. canonical path와 기존 파일의 inode identity를 실행 전에 예약한다. 같은
   경로나 hard-link alias를 요구하는 turn은 동시에 예약 영역에 들어가지 않는다.
4. `write_file`/`edit_file`은 임시 staging 파일에만 쓴다. 같은 turn의
   `read_file`은 staged 파일을 읽는다. 범위 밖 쓰기, shell, nested agent,
   executable hook, 미분류 workspace effect는 fail-closed한다. 일반 bridge뿐
   아니라 `_execute_tool` primitive에도 같은 검사를 두어 직접 dispatch 우회를
   막았다.
5. 모델이 완료한 뒤 text oracle(관례적인 마지막 줄바꿈 하나만 정규화) 또는
   task validator와 dispatch-time version을 검사한다. 모두 통과한 파일만 target
   디렉터리의 임시 파일에서 `os.replace`로 게시한다. oracle 부재·실패 또는
   version conflict에는 자동 게시하지 않는다.
6. `capability_granted`, `reservation_wait`, `effect_blocked`,
   `validation_passed/failed`, `commit_conflict`, `write_set_published`를 turn id와
   함께 기록한다. 검증 거부는 모델의 `complete`와 별도로 UI와 history에
   실패 이벤트로 남긴다.

이 구현이 강제하는 것은 등록된 협조적 도구와 안정적인 canonical path 가정
아래의 **승인 범위 밖 도구 기반 과제 파일 게시 방지**와 **oracle 검증 뒤 파일별 원자적 게시**다.
여러 파일 전체의 crash-atomic 전이, 외부 프로세스, 일반 응답 의미는 보장하지
않는다. 시스템 소유 history·감사 파일은 과제 write set이 아니며, 게시 뒤 일반
code-index post-hook은 manifest 밖 쓰기를 피하기 위해 생략하므로 별도 재구축 전
index가 stale일 수 있다.

## 2. 결정론적 적대 검증

`bench/multiuser/p1_adversarial.py`는 모집단 추정이 아니라 invariant 검증이다.
범위 밖 경로, `..`, shell, direct primitive dispatch, composite agent, 미분류
plugin, 검증 실패, 외부 version 변경, hard-link alias, 동일 경로 turn의 대기와
순차 validated publication을 포함한 19개 검사가 모두 예상값과 일치했다
(`bench/multiuser/out/p1-adversarial.json`).

## 3. 실모델 paired 실험

`bench/multiuser/p1_isolation_real.py`는 같은 realistic 두 과제를 새 workspace에서
동시에 실행한다. 한 run/pair가 분석 단위이며, 각 20개 block 안에서 세 arm 순서를
6개 순열로 균형화했다.

1. scoped prompt
2. scoped prompt + turn-local filtered context
3. filtered context + write capability + staged validated publication

| run 단위 결과 | Scoped | Filtered | Enforced |
|---|---:|---:|---:|
| 상대 과제 경로를 시도한 run | 0/20 (0–16.84%) | 0/20 (0–16.84%) | 0/20 (0–16.84%) |
| 상대 과제 파일이 게시된 run | 0/20 (0–16.84%) | 0/20 (0–16.84%) | 0/20 (0–16.84%) |
| 두 과제 exact oracle 통과 | 20/20 (83.16–100%) | 20/20 (83.16–100%) | 20/20 (83.16–100%) |
| 최종 저장소 정답 | 20/20 (83.16–100%) | 20/20 (83.16–100%) | 20/20 (83.16–100%) |
| 상대 완료 태그 언급 run | 1/20 (0.13–24.87%) | 0/20 (0–16.84%) | 0/20 (0–16.84%) |
| 더 긴 turn span 중앙값 | 59.5초 | 52.1초 | 59.5초 |
| run당 입력 토큰 중앙값 | 33,508 | 32,687 | 33,177 |

Enforced arm에는 실제 `write_set_published` 경로를 turn별 승인 manifest와 조인해
게시 위반을 판정한다. 상수 0으로 가정하지 않는다. 모든 원시 run은
`bench/multiuser/out/p1-isolation-realistic.jsonl`, 파생 통계·exact binomial
95% CI·exact McNemar 검정은 같은 이름의 `.json`에 보존한다. 각 run은 실행에
사용한 Python source digest도 기록하며 최종 수집은 digest 하나만 허용한다.

60개 run은 전부 첫 시도에 완료됐고 source digest는
`412db2fa3688aa413cc7e69269894d8f61a944bf140057da1bb9b738d42a65a8`
하나였다. 모든 이진 파일·정확성 대비의 exact McNemar p는 1.0이었다. 응답
cross-tag도 scoped-only discordant block 하나라 filtered/enforced 대비 p=1.0이다.
따라서 1/20 대 0/20을 모집단 차이로 해석하지 않는다.

강제 팔은 요청별 `write_set_published` 40개를 기록했고 validation 실패와 차단
효과는 없었다. 이 live workload에서 모델이 우회를 시도하지 않았다는 뜻이지,
강제 장치가 필요 없다는 뜻은 아니다. 우회와 실패 시의 invariant는 §2의 19개
결정론적 적대 검증이 담당한다. Filtered의 12.4% 짧은 span과 2.5% 적은 입력
토큰, enforced의 scoped 대비 1.0% 적은 입력 토큰은 한 endpoint의 기술 통계로만
보고한다.

## 4. 논문 반영 원칙

논문은 폐기된 기준 실험의 개선 전 수치를 다시 설명하지 않는다. 현재 시스템과
현재 실험만 기술하고, 프롬프트/컨텍스트가 응답 초점을 돕는 층과 capability가
파일 게시를 강제하는 층을 분리한다. 따라서 강한 주장은 “의미적 정확성 보장”이
아니라 다음 범위다.

> Under the stated cooperative-tool and path-stability assumptions, a turn
> cannot publish a tool-mediated task-file mutation outside its approved
> canonical write set, and
> staged files are published only after their task-supplied oracle succeeds.
