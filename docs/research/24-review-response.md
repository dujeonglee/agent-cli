# CHI 리뷰어 관점의 기술 논문 리뷰 및 수정안

**검토 대상:** `09-full-paper-draft.md` (v1.1-wip, 2026-08-13)  
**검토일:** 2026-08-13  
**검토 관점:** CHI 2027 full paper reviewer  
**평가 범위:** 사용자 연구의 결과는 의도적으로 평가에서 제외한다. 즉, §6의 미완성 결과 자체를 감점 사유로 삼지 않고, §1–5와 §7–8에 있는 기술적·상호작용적 기여만 평가한다. 다만 실제 제출본에 결과 없는 TODO 섹션을 남겨도 된다는 뜻은 아니다.

---

## 0. 작업 체크포인트 (2026-08-13, P1 완료 후)

이 문서의 §1–8은 수정 전 초안을 대상으로 작성한 원 리뷰이므로 문제 설명과
제안 문구를 이력으로 보존한다. 현재 구현·실험·논문 상태는 이 절과 §11의
체크리스트가 우선한다. P0 및 P1의 상세 재현 정보는
`26-p1-isolation-report.md`와 `bench/multiuser/out/`에 있다.

### 처리 완료

- **P0 통계 단위 수정:** torn-write 실험을 arm당 30개의 독립 process/workspace
  run으로 다시 수집했다. 분석 단위를 snapshot이 아닌 run으로 바꾸고 잘못된
  Fisher 분석을 제거했으며, exact binomial CI와 paired exact McNemar 검정을
  본문에 반영했다. 1/2/5/10 ms 민감도 결과와 final-file 결과도 분리했다.
- **P0 semantic 측정 수정:** distinct-task 조건을 같은 20개 block 안에서
  재수집하고, 파일명 생성 여부 대신 과제별 exact-content oracle과 최종 저장소
  oracle을 사용했다. 개선 전 실험은 본문에서 반복 설명하지 않고 현재 실험만
  기술한다.
- **P0 보장 범위 수정:** symlink를 canonicalize하고 기존 hard-link alias를
  workspace-exclusive로 처리한다. 새 도구의 미분류 workspace effect는
  fail-closed한다. 논문의 보장 표를 가정 A1–A5에 결합한 invariant/boundary
  표로 바꿨다.
- **P1 context 대안 추가:** scoped prompt, turn-local filtered context,
  enforced publication의 세 arm을 한 source digest에서 20 block, 총 60개의
  실모델 run으로 비교했다. 각 block의 arm 순서는 여섯 순열로 균형화했다.
- **P1 파일 게시 강제:** 요청별 canonical write capability, path/inode 예약,
  turn-local staging, content/test oracle, version check, 파일별 atomic publication,
  audit event를 구현했다. 범위 밖 쓰기, shell, nested agent, executable hook,
  unclassified effect는 capability mode에서 차단한다.
- **P1 검증:** 19/19 adversarial invariant가 예상값과 일치했다. 실모델 결과는
  세 arm 모두 exact task와 final repository가 20/20이었고 cross-scope file
  publication은 0/20이었다. scoped 응답 하나의 상대 완료 태그는 의미 정확성
  보장으로 확대하지 않고 boundary example로 보고했다.
- **회귀 검증:** 전체 suite는 3,617 passed, 35 skipped, 0 failed였으며 관련
  subset은 552 passed였다. 구현·원자료·영문/한글 논문 초안은 커밋
  `69c79828`까지 함께 반영했다.
- **주장 교정:** 강한 주장을 “semantic correctness”가 아니라, 명시한
  cooperative-tool/path-stability 가정 아래의 **cross-request task-file
  publication isolation and validated publication**으로 한정했다. token 비용도
  전체 비용이 아닌 measured input-token premium으로 표현했다.

### 남은 제출 작업

1. **제출 형태 결정:** first-use study를 수행해 §6을 채우거나, 기술 논문으로
   제출한다면 RQ4·§6·초록/논의의 study TODO를 모두 제거한다. 결과 없는
   프로토콜과 TODO 표를 제출본에 남기지 않는다.
2. **필수 그림 두 개 작성:** 두 참가자, attributed streams, shared context,
   effect gate, serial/parallel timeline을 묶은 architecture figure와 실제 UI에서
   ownership·waiting·effects가 보이는 figure를 완성한다.
3. **기존 live TTFT 재현 정보 보강:** 사용 모델, serving engine/revision,
   CPU/GPU/RAM, decoding 설정, concurrency, arm 순서, 유효/실패 run, 실패 원인,
   실제 bootstrap CI, heavier-load 재실행 조건을 본문 또는 supplement에 적는다.
   P1 isolation arm의 모델·block·누락 run 정보는 이미 반영됐지만 기존 latency
   arm 정보는 아직 불완전하다.
4. **throughput/effect-share 표 자립화:** normalized throughput, effect share,
   recovery의 수식과 분모·분자, cell별 n, 요약 통계와 range/CI를 추가하고 50%
   knee가 두 turn·완전 overlap·특정 단계 구조의 결과임을 명시한다.
5. **범위와 구조 정리:** isolate-and-merge 대비 우월성으로 읽힐 문장을 줄이고
   범위를 one-live-context busy-turn contract로 한정한다. replay, compaction,
   fairness, lifecycle은 supporting invariant 또는 supplement로 이동한다.
6. **HCI/design-space 근거 정리:** 사용자 결과가 생기기 전 interface 원칙은
   design hypothesis/implication으로 유지한다. design-space에는 search date,
   포함·제외 규칙, documentation snapshot, 축 도출 절차를 추가하고
   undocumented와 absent를 구분한다.
7. **제출 마감 정리:** 미사용/역할이 불명확한 참고문헌을 정리하고 author note,
   TODO, 익명화되지 않은 artifact 링크를 최종 검사한다.

### 다음 작업 권장 순서

`제출 형태 결정 → live-method 정보 보강 → effect-share 표 수정 → 논문 구조·주장
정리 → 그림 제작 → 참고문헌·TODO·익명화 최종 점검` 순서가 가장 안전하다.
사용자 연구를 진행한다면 그림 작업과 병행할 수 있지만, §6 결과가 나온 뒤 초록,
기여, 논의, 결론을 다시 맞춰야 한다.

---

## 1. 결론부터: 현재 판정

**현재 판정: Weak Reject / Major Revision. CHI의 짧은 R&R 경계에는 걸칠 수 있으나, 그대로 제출하면 핵심 보장의 타당성과 통계 분석 단위 때문에 탈락할 가능성이 높다.**

이 판정은 “사용자 연구가 없어서”가 아니다. CHI는 인간 대상 실험이 없는 기술 기여도 받아들일 수 있고, Coagora의 문제 설정과 시스템 기여는 충분히 HCI에 관련된다. 현재 걸림돌은 다음 네 가지다.

1. torn-write 실험에서 **한 실행 안의 2ms 샘플들을 독립 표본처럼 Fisher 검정에 사용한 의사반복(pseudoreplication)** 문제가 있다.
2. “동일 자원 효과는 겹치지 않는다”는 보장이 **경로 별칭, `UNKNOWN` 효과, 비협조적 프로세스와 부분 쓰기**를 포괄하지 못한다. 일부는 논문의 선언과 구현이 정확히 일치하지 않는다.
3. 의미적 오염 실험의 `own task completed`는 실제로는 **예정된 파일명을 모두 썼는가**에 가깝다. 과제의 내용적 정답이나 저장소 정확성을 측정하지 않는다.
4. 가장 중요한 실패가 공유 세션 자체에서 필연적으로 생긴 것인지, 현재의 **프롬프트 구성 방식—동시 사용자의 요청을 서로에게 user-role instruction처럼 노출하는 방식—에서 생긴 것인지** 분리되지 않았다. turn-local context 또는 동시 요청 필터링이 빠진 핵심 대안이다.

반대로, 이전 초안에서 문제였던 과도한 분량과 11개 RQ의 분산은 크게 개선됐다. 현재 서론부터 결론까지 약 7,100단어이고, 사용자 연구 섹션을 제외하면 약 5,740단어다. 이는 CHI 2027이 권장하는 5,000–8,000단어 범위에 들어간다. 이번에는 “절반을 잘라야 한다”가 아니라, **핵심 타당성 문제를 고치고 약한 검증 묶음을 부록으로 옮겨 중심 주장을 더 선명하게 만드는 것**이 필요하다.

CHI 2027은 originality, significance, validity/research quality, presentation clarity를 중심으로 평가하고 기술 기여에는 검증 가능성·재현성·복제 가능성을 요구한다. 이 기준에서 Coagora는 중요성과 재현성은 강하지만, 현재 중앙 실험의 분석 타당성과 보장 범위가 부족하다. 관련 공식 기준은 [CHI 2027 Papers](https://chi2027.acm.org/authors/papers/)와 [Guide to a Successful Submission](https://chi2027.acm.org/guide-to-a-successful-submission/)에 근거했다.

## 2. 리뷰 점수표

| 기준 | 현재 평가 | 판단 근거 |
|---|---:|---|
| HCI 중요성 | 4/5 | 여러 개발자가 하나의 상태 있는 에이전트를 동시에 조작할 때 생기는 소유권·대기·개입 문제는 시의성과 파급력이 높다. |
| 독창성 | 3.5/5 | 락 자체는 새롭지 않지만, 추론 아래에 효과 정렬을 두고 물리적 무결성과 의미적 귀속을 분리한 상호작용 아키텍처는 새롭다. |
| 기술적 완성도 | 3/5 | 구현과 계측은 인상적이지만, 경로 별칭과 `UNKNOWN` 처리 때문에 보장 범위가 현재 문구보다 좁다. |
| 평가의 타당성 | 2.5/5 | 다양한 실험과 원시자료는 강점이나, 핵심 두 실험의 표본단위·측정 타당성에 문제가 있다. |
| 재현성·투명성 | 4/5 | 155개 수치 검증과 원시 JSONL은 매우 좋다. 다만 수치의 재현성과 통계적 해석의 정당성은 별개다. |
| 표현 명료성 | 4/5 | 이전 버전보다 훨씬 읽기 쉽고 주장이 절제돼 있다. 핵심 그림과 일부 정의는 여전히 빠져 있다. |
| CHI 적합성(사용자 연구 제외) | 3.5/5 | 기술 HCI 논문으로 성립 가능하다. 단, 사람에 대한 함의를 관찰 결과가 아니라 설계 가설로 한정해야 한다. |
| 종합 | Weak Reject | P0 문제를 해결하면 R&R 또는 borderline accept 수준까지 올라갈 가능성이 크다. |

## 3. 논문의 강점

### 3.1 실제로 중요한 상호작용 문제를 정확히 집었다

이 논문은 “여러 브라우저가 같은 채팅을 본다”를 협업이라고 부르지 않는다. 두 사람이 동시에 요청할 때 무엇이 실행되고, 누가 행동을 소유하며, 누가 멈출 수 있고, 어느 상태가 어떤 순서로 바뀌는지를 계약으로 정의한다. 이것은 단순 성능 최적화가 아니라 HCI와 CSCW의 제어·책임·상황 인식 문제다.

### 3.2 물리적 무결성과 의미적 정확성을 분리한 것이 가장 강한 통찰이다

트랜스크립트가 구조적으로 정상이고 파일 쓰기가 겹치지 않아도 모델은 다른 사용자의 요청을 수행할 수 있다. 이 구분은 shared-agent 연구가 앞으로 반복해서 사용할 만한 개념적 도구다. 특히 prompt scoping이 한 모델에서는 파일 수준 오염을 없앴지만 다른 모델에서는 잔여 실패를 남겼다는 결과는 과장되지 않은 좋은 negative result다.

### 3.3 같은 구현에서 세 계약을 비교한다

serial, reject-and-retry, parallel이 같은 전송·컨텍스트·도구 계층을 사용한다. 별도 프로토타입끼리 비교하는 것보다 내부 타당성이 높고, 기존에 출하된 serial 경로를 기준으로 쓴 점도 straw-man 우려를 줄인다.

### 3.4 검증 자료가 매우 투명하다

원시 JSONL, 파생 요약, 재계산 스크립트, claim verifier, mock/live 구분, 실패한 실행과 수정 이력이 남아 있다. `verify_paper_claims.py`의 현재 155개 항목은 모두 원자료와 일치했다. 이는 **보고된 숫자의 전사 정확성**에 강한 신뢰를 준다.

다만 이 검증기는 표본 독립성, 측정 구성타당성, 대안 설명까지 확인하지는 않는다. 이 구분을 논문도 명시하면 오히려 투명성 기여가 더 강해진다.

### 3.5 분량과 구조가 CHI 제출 형태에 가까워졌다

4개의 RQ, 압축된 구현 절, 한계가 명확한 평가, 약 7,100단어의 전체 길이는 이전 버전보다 훨씬 경쟁력 있다. §5.3a의 여러 기계적 검증을 더 줄일 여지는 있지만, 현재 문제는 더 이상 전체 분량이 아니다.

---

## 4. 반드시 해결해야 할 문제(P0)

### P0-1. torn-write 통계는 독립 표본을 사용하지 않는다

#### 문제

§5.2는 2ms마다 파일 상태를 읽어 `9/110` 대 `0/361`을 만들고 Fisher exact test의 `p = 1.6 × 10⁻⁶`을 보고한다. 그러나 원자료를 생성한 `e1_ablation.py`는 각 lock scope를 **한 번씩** 실행하고, 그 한 실행 안에서 연속 스냅샷을 채취한다.

같은 실행에서 2ms 간격으로 얻은 상태들은 독립 시행이 아니다. 하나의 긴 torn 상태가 여러 샘플로 잡힐 수 있고, 쓰기 속도나 스케줄링이 샘플 수를 바꾼다. 실제로 arm별 관측 시간과 classified sample 수도 다르다. 따라서 471개 스냅샷을 독립 Bernoulli 시행처럼 넣은 Fisher p-value는 통계적으로 정당하지 않다.

더구나 현재 실험에서 최종 파일은 세 arm 모두 `intact`였다. 이 실험이 증명하는 것은 “강제 겹침 중 외부 관찰자가 혼합 중간 상태를 볼 수 있었다”이지, “완료 후 파일의 8.2%가 손상됐다”가 아니다. 본문은 대체로 sampled states라고 적었지만, 초록의 “forced overlap produces torn writes in 8.2% of sampled states”는 독자가 occurrence rate로 오해하기 쉽다.

#### 수정 방안

최소 수정은 다음과 같다.

1. Fisher p-value를 현재 데이터에서 제거한다.
2. `9/110`은 한 번의 계측 실행에서 관찰된 **노출 사례**로만 기술한다.
3. 독립된 임시 workspace와 프로세스에서 arm당 최소 20–30회를 반복한다.
4. 1차 분석 단위를 `run`으로 둔다. 예: “한 번이라도 mixed/broken 상태가 관찰된 실행 수”, “위반 상태가 관찰된 총 시간 비율”, “첫 위반까지 시간”.
5. arm별 실행 순서를 무작위화하거나 block randomization하고, 플랫폼별로 분리 보고한다.
6. 샘플링 간격 1/2/5/10ms 민감도 분석을 보조자료에 둔다.
7. lock의 목적이 최종 상태, concurrent-reader visibility, writer non-overlap 중 무엇인지 각각 분리한다.

가장 정직한 핵심 문장은 다음 정도다.

> In one forced-overlap trace, an external sampler observed mixed-writer states without the gate and none with either locking policy. Repeated runs use the run, rather than correlated snapshots, as the unit of analysis.

### P0-2. 의미적 오염 실험도 표본단위와 arm 비교가 불완전하다

#### 문제

각 반복은 두 동시 턴이 하나의 workspace, context, endpoint 부하를 공유한다. 따라서 `40 turns`는 40개의 완전 독립 표본이 아니라 **20개의 cluster 안에 중첩된 40개 관측치**다. turn별 Fisher 검정은 이 의존성을 무시한다.

스크립트에는 이미 더 적절한 run-level 지표가 있다.

- primary model, similar task: off `19/20 runs`에서 어느 한 턴이라도 cross-task, on `0/20`
- second model, distinct task: off `11/20`, on `8/20`

이 run-level 결과가 본문의 1차 결과가 되어야 한다. turn-level count는 기술 통계로 남길 수 있다.

또한 primary model의 distinct-task off와 on은 같은 interleaved 실행에서 나온 것이 아니다. 원자료상 off는 `postfix/n3c-realistic.*`, on은 이후의 `postfix2/n3c-realistic.*`에 있다. 모델·서빙 설정이 고정됐더라도 시간대와 endpoint 상태가 arm과 완전히 교락될 수 있다. 큰 차이 자체는 흥미롭지만, 동시 수집한 대조실험처럼 서술하면 안 된다.

#### 수정 방안

1. 1차 분석 단위를 run/pair로 바꾼다.
2. 두 arm을 반복 번호별로 교차 실행하고 순서를 번갈아 배치한다.
3. `crossTask`, `bothComplete` 같은 run-level 결과와 정확한 이항 신뢰구간을 보고한다.
4. turn-level 결과가 필요하면 cluster bootstrap, GEE, 또는 run 단위 permutation을 사용한다.
5. 모델별·workload별 비교를 사전에 정한 primary contrast와 exploratory contrast로 구분한다.
6. “within-model statistically separable” 같은 문장은 재분석 전 제거한다.
7. model endpoint가 deterministic하지 않다면 seed, temperature, decoding parameter, model revision과 실행 날짜를 기록한다.

### P0-3. `own task completed`는 과제 완료를 측정하지 않는다

#### 문제

`n3c_scoping_real.py`의 `ownComplete`는 해당 턴이 예정된 파일명 집합을 모두 썼는지 확인한다. 파일 내용, 요구사항 충족, 테스트 통과, 저장소 불변식은 확인하지 않는다. 그런데 표의 열 제목은 “Own task completed”다. 이는 측정값보다 강한 표현이다.

같은 한계가 `cross-user file effects`에도 있다. 상대방의 파일명을 건드린 것은 잘 잡지만 다음 실패는 놓친다.

- 자기 파일에 상대방 과제의 내용을 씀
- 올바른 파일명을 만들었지만 내용이 틀림
- shell/package 명령으로 상대 과제에 영향을 줌
- 답변 텍스트에서는 상대 요청을 따랐지만 파일은 쓰지 않음
- 두 과제를 모두 했으나 한쪽 결과가 저장소를 깨뜨림

특히 두 번째 모델의 원시 응답에는 상대 완료 태그가 나타난 turn이 scoping off `12/40`, on `10/40`으로 기록돼 있다. 파일 효과는 `14/40 → 9/40`으로 줄었지만 텍스트 수준 혼선은 거의 줄지 않았다. 현재 본문은 side effect를 1차 판정으로 선택했다고 밝히지만, “semantic focus”와 “another participant's task”라는 넓은 해석을 하려면 이 결과도 함께 보여야 한다.

#### 수정 방안

1. 지금 지표의 이름을 `wrote all assigned target paths`로 바꾼다.
2. 각 과제에 content oracle 또는 자동 테스트를 둔다. 최소한 예상 marker, line semantics, parse/test 성공 여부를 확인한다.
3. 결과를 네 층으로 분리한다.

   - structural attribution: `reply_to`
   - effect ownership: touched paths/commands
   - task correctness: content oracle/tests
   - response focus: answer-text coding

4. answer text는 condition을 모르는 두 코더 또는 사전 정의된 tag/rubric으로 판정하고 agreement를 보고한다.
5. “semantic isolation”을 주장하려면 파일 쓰기뿐 아니라 명령과 최종 응답도 포함한다. 그렇지 않으면 일관되게 “cross-user file effects”로 한정한다.
6. scoping의 이득과 함께 omission, over-cautiousness, own-task correctness를 동일한 표에서 보고한다.

### P0-4. 논문의 effect-gate 보장이 구현의 실제 별칭·분류 모델보다 넓다

#### 문제 A: 경로 별칭

§3.3은 경로를 정규화하고 같은 경로의 효과를 정렬한다고 한다. 실제 `normalize_lock_path`는 `os.path.abspath(os.path.normpath(...))`를 사용한다. 이 방식은 `.`/`..`와 상대·절대 표기는 합치지만 다음은 합치지 못한다.

- workspace 내부 symbolic link와 그 target
- 같은 inode를 가리키는 hard link
- 실행 중 rename으로 생긴 별칭
- 일부 filesystem의 case/Unicode normalization 별칭

`_confine.resolve_within`은 symlink를 따라가지만, lock key를 만드는 `effect_lock`은 그 canonical path를 재사용하지 않는다. 따라서 두 턴이 `link.txt`와 `target.txt`를 쓰면 실제로 같은 파일이어도 서로 다른 lock key로 병렬 진입할 수 있다. 이는 현재 “same-resource operations run one after another”라는 보장의 반례다.

#### 문제 B: `UNKNOWN`의 이중 의미

논문 표는 “Unknown file effect → exclusive, safety-first fallback”이라고 쓴다. `EffectIntent.is_exclusive`와 일부 주석도 UNKNOWN을 exclusive로 설명한다. 그러나 실제 `effect_lock.hold`는 `EffectKind.UNKNOWN`이면 lock을 전혀 잡지 않고 통과시킨다.

현재 내장 UNKNOWN 도구들이 parent/composite, human-wait, workspace 외부 상태라서 의도적으로 통과한다는 구현 설명은 이해된다. 문제는 **UNKNOWN이 동시에 ‘새 도구가 분류를 빠뜨렸을 때의 기본값’**이기도 하다는 점이다. 새 plugin/tool이 workspace를 수정하면서 intent override를 잊으면 fail-closed가 아니라 fail-open이 된다. 논문의 safety-first fallback과 맞지 않는다.

#### 문제 C: partial visibility와 협조적 호출만 보호

`e1_ablation.py`는 `partial` 상태를 정상으로 제외한다. 실제 write는 truncate 후 기록하므로 lock을 켜도 gate 밖의 reader는 빈 파일이나 부분 파일을 볼 수 있다. 또한 detached background process, 외부 IDE, 다른 OS process는 in-process gate를 따르지 않는다.

따라서 현재 gate가 보장하는 것은 다음처럼 좁혀야 한다.

> All participating in-process effects that declare canonical, correctly classified resources do not overlap according to the compatibility matrix.

이는 “파일이 항상 intact하다” 또는 “workspace가 atomic하다”와 다르다.

#### 수정 방안

1. 기존 파일은 `realpath`/`Path.resolve()`로, 새 파일은 canonical parent + basename으로 key를 만든다.
2. hard-link alias가 중요한 환경이라면 inode `(st_dev, st_ino)`를 사용하거나, hard link가 감지된 경로는 workspace-exclusive로 내린다.
3. canonicalization과 실제 tool execution 사이의 TOCTOU 한계를 명시한다.
4. `UNKNOWN_WORKSPACE_EFFECT`와 `NON_WORKSPACE_OR_COMPOSITE`를 별도 kind로 나눈다. 전자는 exclusive, 후자는 leaf에서만 lock한다.
5. plugin/tool registration 시 effect intent 선언을 필수화하고, 누락은 서버 시작 실패 또는 exclusive fallback으로 처리한다.
6. symlink, hard link, empty path, plugin intent omission, rename, detached shell을 포함한 adversarial regression suite를 추가한다.
7. concurrent-reader visibility까지 보장하려면 write를 temp file + atomic replace로 바꾼다. 그렇지 않으면 partial visibility를 명시적으로 보장 밖에 둔다.
8. §3.5의 “guaranteed”를 “enforced under assumptions A1–A5”로 바꾸고 가정 표를 바로 옆에 둔다.

---

## 5. 중요한 연구·프레이밍 문제(P1)

### P1-1. prompt 완화를 강제 가능한 turn-local 파일 격리로 확장해야 한다

> **반영 완료 (2026-08-13).** `origin_turn` 기반 turn-local context view,
> canonical path/inode write-set 예약, 등록 도구 경계의 fail-closed capability,
> private staging, exact-content/task validator, version 재검사, 파일별 atomic
> replace, 귀속 가능한 감사 이벤트를 구현했다. 동일 경로 순차 게시를 포함한
> 결정론적 적대 검증은 19/19 통과했다. 고정 source digest의 실모델 20-block
> 세 팔 비교에서는 세 팔 모두 exact 과제·최종 저장소 정답 20/20과 상대 과제
> 파일 게시 0/20이었다. 응답 cross-tag는 scoped 1/20, filtered/enforced 0/20;
> 모든 paired exact p는 1.0이었다. 강제 팔은 40/40 write set을 validation 뒤
> 게시했고 실패·재시도는 없었다. 본문 §3.5, §4, §5.4, §7–8과 부록 A에 반영했다.

P0 paired 실험에서 turn scoping은 관찰된 교차 사용자 경로 효과를 20/20
run에서 0/20으로 줄였지만, scoped 응답 하나는 여전히 상대 완료 태그를
언급했다. 이 결과는 프롬프트가 강한 완화책임을 보여주지만, 모델이 지시를
무시해도 유지되는 격리 invariant는 아니다. P1의 목표는 “의미적 정확성 전체”를
보장하는 것이 아니라 다음의 더 좁고 검증 가능한 성질을 강제하는 것이다.

> A turn cannot publish a tool-mediated task-file mutation outside its approved canonical write set;
> overlapping write sets cannot commit concurrently; and a staged write set is
> published only after its task-supplied oracle succeeds.

이를 위해 context, authorization, publication을 별도 층으로 구현한다.

1. **Turn-local context view.** 각 turn은 dispatch 시점의 committed context와
   자기 요청만 user-role instruction으로 본다. 다른 in-flight 요청은 제거하거나
   명시적인 quoted/metadata activity로 낮춘다. 완료된 다른 turn의 결과는 다음
   inference step부터 “newer committed activity”로 합류시킨다.
2. **Approved write-set capability.** dispatch 전에 turn별 canonical path
   allowlist를 만든다. benchmark에서는 과제에 명시된 target path가 곧
   capability다. 일반 사용에서는 사용자가 지정한 경로 또는 모델이 제안하고
   requester가 승인한 manifest만 권한이 된다. 실행 중 범위 확장은 묵시적으로
   허용하지 않고 별도 승인·예약을 거친다.
3. **Conflict reservation.** 승인된 write set을 effect gate의 canonical key로
   예약한다. 다른 active turn과 겹치면 inference 이후까지 미루지 말고 dispatch
   또는 capability 확장 시점에 queue/reject/confirmation 정책을 적용한다. 같은
   실제 파일을 가리키는 symlink와 감지된 hard link에는 P0의 fail-closed 규칙을
   그대로 쓴다.
4. **Tool-boundary enforcement.** `write_file`, `edit_file`, delete, rename/move 등
   모든 workspace mutation은 실행 전에 `turn_id × capability` 검사를 통과해야
   한다. 범위 밖 호출은 기록하고 차단한다. 임의 경로를 바꿀 수 있는 shell과
   미분류 plugin은 allowlist를 우회할 수 있으므로, capability 모드에서는
   read-only sandbox, 명시적 승인, 또는 workspace-exclusive deny 중 하나로
   fail-closed해야 한다.
5. **Staged validation.** 파일 효과를 즉시 공유 workspace에 공개하지 않고
   turn-local staging 영역에 쓴다. 과제별 exact-content oracle 또는 명시된 test를
   staging 결과에 실행한다. oracle이 실패하거나 없을 때의 정책을 분리한다:
   benchmark에서는 실패 시 publish 금지, 일반 사용에서는 승인 요청 또는
   “unvalidated” 상태로 남기되 자동 commit하지 않는다.
6. **Validated commit.** oracle 통과 후 effect gate 안에서 reservation과 기준
   version을 다시 확인하고 write set 전체를 publish한다. 가능한 파일은 temp file
   + atomic replace를 사용한다. version conflict, capability 변경, validator 실패가
   있으면 공유 workspace는 바뀌지 않아야 한다.
7. **Auditability.** `capability_granted`, `effect_blocked`, `validation_passed/failed`,
   `commit_conflict`, `write_set_published`를 turn attribution과 함께 기록한다. 모델이
   상대 과제를 시도했는지와 시스템이 실제로 게시했는지를 구분해야 한다.

이 설계가 강제할 수 있는 것은 **교차 요청 파일 효과 격리**와 **명시된 oracle에
대한 validated publication**이다. 요구사항 자체가 잘못됐거나 oracle이 불완전한
경우, 응답 텍스트의 의미, shell/외부 프로세스가 정책 밖에서 만든 효과까지
“의미적 정확성”으로 보장할 수는 없다. 논문도 이 좁은 보장과 모델의 task
correctness를 별도 행으로 보고해야 한다.

#### P1 평가 설계

주 live 비교는 현재의 `turn-scoped prompt`와 아래 두 팔을 같은 20개 paired
block에서 교대 실행한다.

1. `turn-scoped prompt` — 현재 기준선
2. `turn-local filtered context` — prompt assembly 대안
3. `filtered context + capability + staged validated commit` — 강제 격리

서로 다른 parser/CLI 과제 외에 의도적으로 같은 target path를 요구하는 충돌
과제를 추가한다. symlink/hard-link alias, `..`, rename, shell, 미분류 plugin은
실모델 비율이 아니라 결정론적 adversarial invariant suite로 검증한다.

주 결과는 run/pair 단위로 다음을 분리한다.

- 모델이 범위 밖 효과를 **시도한** run
- 범위 밖 mutation이 공유 workspace에 **게시된** run
- overlapping write set이 동시에 commit된 run
- 두 과제의 staged oracle 통과 및 최종 repository correctness
- validator 실패 후 workspace가 불변이었던 run
- capability false rejection, 사용자 승인 횟수, 추가 latency와 token 비용
- 응답의 cross-tag는 보조 의미 지표

live paired 결과에는 exact binomial CI와 exact McNemar 검정을 사용한다.
adversarial suite는 모집단 비율이나 p-value가 아니라 “각 invariant의 예상값과
관측값이 일치했는가”로 보고한다. P1의 성공 기준은 적어도 모든 참여 도구에 대해
`committed cross-scope mutation = 0`, `concurrent conflicting commit = 0`,
`failed validation followed by publication = 0`이며, false rejection과 비용을 함께
공개하는 것이다.

이 결과가 성립하면 논문의 강한 주장은 “prompt scoping이 의미를 보장한다”가
아니라 다음처럼 쓸 수 있다.

> Prompt and context scoping reduce cross-request behavior, while a turn-local
> capability and validated-commit boundary prevents cross-scope task-file
> publication under the stated cooperative-tool and path-stability assumptions.

### P1-2. isolate-and-merge 대안과의 관계를 더 좁혀야 한다

서론은 serial과 session fork를 대비한 뒤 Coagora를 “third contract”로 제시하지만, 평가는 reject, serial, parallel만 비교한다. worktree/branch 격리는 표에만 있고 merge cost, context divergence, conflict resolution을 측정하지 않는다.

두 선택지가 있다.

- **권장:** 논문 범위를 “one live shared context 안의 busy-turn contract 비교”로 명확히 한정한다.
- 또는 isolate-and-merge arm을 추가하되, TTFT만이 아니라 merge 시간, 중복 작업, context reconciliation까지 측정한다.

현재 분량과 연구 집중도를 고려하면 첫 번째가 낫다. “forking necessarily creates costly merge work” 같은 일반 문장은 동기 설명으로만 두고 비교 우월성 주장으로 읽히지 않게 해야 한다.

### P1-3. live-model 방법 정보가 본문만으로 충분하지 않다

“primary model”, “second model”, “on-premise endpoint”, “heavier endpoint load”만으로는 복제가 어렵다. 익명 심사라고 해서 사용한 모델을 숨길 필요는 없다. 최소한 다음을 본문 또는 보조자료에 제시해야 한다.

- model family, parameter/quantization class, serving engine와 revision
- temperature, top-p, seed, max tokens, tool-calling format
- host CPU/GPU/RAM과 동시 요청 설정
- arm별 유효/실패 실행 수, 실패 원인과 어느 조건에서 빠졌는지
- live TTFT의 12개 분포, 실제 bootstrap CI 수치
- “heavier endpoint load” 재실행의 날짜·반복 수·부하 조건
- arm 실행 순서와 randomization/blocking 절차

현재 §5.1은 “non-overlapping ranges and bootstrap intervals”라고만 쓰고 interval을 보여주지 않는다. 원자료에는 serial median CI 약 `[38.02, 38.28]s`, parallel `[10.71, 10.81]s`가 있으므로 표나 그림에 직접 넣는 편이 낫다. 다만 이 CI는 한 endpoint의 12회 runtime variation에 대한 것이지 다른 모델·배포로의 일반화 구간은 아니다.

또한 deterministic HOL grid의 네 실패는 조건별 위치와 원인을 보고하고, 실패를 제외해도 slope 결론이 유지되는 sensitivity analysis를 제시해야 한다.

### P1-4. throughput/effect-share 표가 독립적으로 해석되지 않는다

§5.2의 표에서 `1.987`, `1.992`가 무엇의 비율인지 열 이름만으로 알 수 없다. `Recovery`의 분모·분자도 정의되지 않는다. effect share `s`의 공식, throughput normalization 기준, 반복 수, 분산이 없다.

다음을 추가하라.

> normalized throughput = serial wall time / concurrent wall time  
> effect share = exclusive-effect hold time / end-to-end turn time  
> recovery = (conflict-scoped − workspace) / (no-lock − workspace)

실제 정의가 다르면 그 정의를 써야 한다. 각 셀의 `n`, median/mean, range 또는 CI도 필요하다. “50% knee”는 두 turn, 완전 겹침, 특정 단계 구조에서 성립하는 이론적 결과이므로 일반 법칙처럼 표현하지 말고 모델 가정을 함께 적어야 한다.

### P1-5. RQ3이 아직 너무 많은 것을 한 바구니에 넣는다

현재 RQ3은 replay, compaction, attribution, fairness, lifecycle, staleness, semantic cross-talk을 모두 포함한다. 표로 압축해 이전보다 낫지만, 논문의 가장 중요한 semantic boundary가 인프라 회귀검사들과 같은 RQ에 묻힌다.

추천 구조는 다음과 같다.

1. RQ1: responsiveness and token cost
2. RQ2: effect ordering, integrity, and collapse boundary
3. RQ3: request ownership versus semantic focus
4. Supporting invariant validation: replay/compaction/fairness/lifecycle — RQ가 아닌 짧은 subsection 또는 supplement
5. RQ4: first use — 결과가 생겼을 때만

특히 204/204 lifecycle, test count, reconnect 세부 수치는 artifact 신뢰를 높이지만 CHI 본문의 중심 발견은 아니다.

### P1-6. 기술 HCI 논문으로 제출할 경우 사람에 대한 문장을 더 엄격히 한정해야 한다

사용자 연구 결과를 제외해도 논문은 성립 가능하다. 다만 다음은 기술 측정에서 직접 관찰되지 않았다.

- 사용자가 stale context를 이해하는가
- ownership label이 실제 책임 판단을 돕는가
- waiting indicator가 올바른 mental model을 만드는가
- 팀이 coordination norm을 형성하는가
- parallel contract를 선호하거나 신뢰하는가

따라서 §7.2의 네 원칙은 “evaluation suggests”보다 **design hypotheses/implications derived from system behavior**로 명명하는 것이 안전하다. “A viable interface must…”도 “Our results motivate testing…” 정도로 낮추면 사용자 결과 없이도 논리적으로 완결된다.

실제 제출 시에는 둘 중 하나를 택해야 한다.

- 사용자 연구를 포함한다면 §6 결과가 초록·기여·논의·결론을 실제로 바꾸게 한다.
- 기술 논문으로 제출한다면 미완성 §6, RQ4, 모든 `[TODO after study]`를 제거하고 human claim을 연구 가설로 한정한다.

결과 없는 프로토콜과 TODO 표를 제출본에 남기는 선택지는 없다.

### P1-7. design-space 기여의 도출 방법이 얇다

네 축은 유용하지만, 현재는 저자들이 어떤 절차로 축을 도출했는지 충분히 설명되지 않는다. 9개 시스템을 본 뒤 축을 귀납적으로 만들었는지, CSCW 이론에서 연역했는지, 두 방식이 결합됐는지 알려야 한다.

수정 방안:

1. search date, inclusion/exclusion rule, documentation snapshot을 보조자료에 둔다.
2. 제품 문서가 “undocumented”인 것과 기능이 “없음”을 구분한다.
3. 표를 novelty proof가 아니라 scoped analytical framing으로 위치시킨다.
4. GitHub Next Ace [4]가 참고문헌에는 있으나 본문 비교에는 없는 이유를 설명하거나 제거한다.
5. compaction 관련 [29]도 현재 인용되지 않으므로 관련 절에서 쓰거나 제거한다.
6. Codex Slack 행은 통합 문서 [15]를 직접 인용하고, 발표 글 [14]와 역할을 구분한다.

---

## 6. 세부 문제와 수정안(P2)

### 6.1 “one agent”의 조작적 정의

한 process/session/context/tool registry를 공유하지만 inference loop는 turn마다 병렬로 돈다. 일부 독자는 이를 여러 agent instance로 볼 것이다. “one agent”를 shared durable session, workspace, policy, tool runtime의 동일성으로 정의하고, concurrent inference calls는 복수임을 첫 등장 때 명시하라.

### 6.2 completion-order commit의 인과 의미

도착 순서, dispatch 순서, inference completion 순서, context commit 순서가 다르다. completion-order serialization이 API-valid transcript는 만들지만 사용자가 기대하는 conversational causality까지 보장하지 않는다. 두 턴이 여러 tool step을 거칠 때 새 commit을 어느 시점부터 보는지도 timeline figure로 설명해야 한다.

### 6.3 fairness는 정책 정의이지 보편적 공정성이 아니다

현재 fairness는 “한 user당 한 active turn + strict FIFO”다. 이는 flood resistance에는 좋지만, user identity가 안정적이고 Sybil이 없다는 가정이 필요하다. 단일 사용자의 의도적 병렬 작업도 막는다. “fair”를 넓게 쓰기보다 per-principal admission fairness로 부르고, identity binding과 reconnect semantics를 설명하라.

`4.8ms vs 151.2s`도 gate가 정의한 flood workload에서의 verification에 가깝다. 일반적인 multi-user fairness magnitude로 해석하지 않도록 workload 구성과 p95 `23.6s` 같은 tail도 같이 보여야 한다.

### 6.4 default cap을 올린 8-user 결과의 해석

8-user 실험은 admission queue를 피하려고 cap을 올렸다. 이는 “default Coagora가 8명에서 scale한다”가 아니라 “queue가 없을 때 inference contention이 39% 증가했다”는 결과다. 본문 문구를 이 조건에 맞게 좁혀라.

### 6.5 token cost의 범위

현재 1.49×는 세 질문 workload의 **input tokens**다. output tokens, wall-clock compute, GPU energy, 금전 비용, 실패·중복 효과의 repair cost는 포함하지 않는다. “cost” 대신 “input-token premium in this workload”를 일관되게 사용하고 전체 토큰 표를 보조자료에 제시하라.

### 6.6 cancel의 실제 효과

사용자가 자기 turn을 취소할 수 있다고 하지만, 이미 시작된 shell subprocess, leaf effect, model HTTP call이 즉시 중단되는지 아니면 결과만 버리는지 명확하지 않다. cancellation의 linearization point와 effect rollback 부재를 contract 표에 추가하라.

### 6.7 실패·제외 보고

HOL 240회 중 4회에 attributable first token이 없었다. 어느 contract와 L에서 발생했는지, timeout인지 parser/stream 문제인지가 중요하다. responsiveness 논문에서는 “답하지 않음”도 사용자 경험의 일부이므로 단순 제외만 해서는 안 된다.

### 6.8 참고문헌과 인용 정리

현재 working reference에는 arXiv, 제품 문서, issue, peer-reviewed paper가 섞여 있다. 제출 전 다음을 수행하라.

- 모든 저자·연도·출판 상태를 BibTeX 원문과 대조
- arXiv가 정식 출판됐다면 최종 서지로 교체
- 제품 문서의 접근일 또는 archived snapshot 기록
- 본문에 쓰지 않은 [4], [29] 처리
- 번호 수동 관리 대신 LaTeX/BibTeX에서 자동 생성

### 6.9 시각 자료가 반드시 필요하다

현재 유일한 핵심 figure가 TODO다. 최소 두 개가 필요하다.

1. serial/reject/parallel의 시간선과 snapshot/commit/effect gate를 한 장에 보여주는 architecture timeline
2. 두 사용자의 concurrent stream, requester label, touched files, cancel control, stale indicator 위치를 보여주는 실제 UI screenshot 또는 충실한 wireframe

표만으로는 turn과 effect의 인과관계가 잘 보이지 않는다. 색상 외에도 선 모양·아이콘·직접 레이블을 사용하고 alt text를 준비하라.

---

## 7. 절별 수정 제안

### Abstract

현재 초록은 기술 결과가 많고 중심 메시지는 분명하다. 다만 다음을 고치면 더 강해진다.

- deterministic slope `0.00 vs 1.03`보다 live `38.2s vs 10.8s`를 우선한다.
- `8.2%`를 occurrence rate처럼 전면에 두지 않는다. 반복 run-level 분석 전에는 “a forced-overlap trace exposed mixed-writer intermediate states”로 낮춘다.
- `technically viable`를 `demonstrates feasibility under a cooperative, single-process threat model`로 좁힌다.
- “turn-scoped prompt eliminated…”에는 model/workload와 측정 endpoint가 file effects임을 넣는다.
- 사용자 연구 결과를 제외한 제출본이라면 마지막 문장의 ownership/visible control은 measured result와 derived implication을 구분한다.

### Introduction

- 현재 문제 설정과 세 기여는 대체로 좋다.
- isolate-and-merge보다 일반적으로 우월하다는 인상을 줄이고 “one live shared context의 contract”로 범위를 고정한다.
- contribution 1의 design space는 조사 방법을 보강하지 않으면 “analytical framing”으로 낮춘다.
- contribution 3에서 guarantee라는 단어에 cooperative in-process effect assumptions를 붙인다.

### Related Work and Design Space

- DB 개념을 새 기법처럼 주장하지 않는 점은 좋다.
- 제품 표의 근거 날짜와 undocumented/absent 구분을 추가한다.
- prompt/context isolation, multi-principal authorization, collaborative editor transaction/awareness literature를 Coagora의 실제 설계 선택과 더 직접 연결한다.
- 현재 sparse coordinate를 novelty의 증명으로 사용하지 않는다.

### Contract and Implementation

- “guarantee table” 앞에 assumptions 표를 둔다.
- path alias, detached effects, UNKNOWN, crash durability, cancellation의 경계를 명시한다.
- turn-local context, capability reservation, staged validation, publish의 상태 전이를
  설명하고 각 단계의 fail-closed 조건을 명시한다.
- state transition 또는 timeline figure를 추가한다.

### Technical Evaluation

- 먼저 공통 Methods를 둔다: experimental unit, n, randomization, endpoint, missing data, statistic.
- §5.2 integrity를 독립 run 단위로 재실험한다.
- §5.4 semantic 결과는 file-path completion이 아니라 content/test correctness를 포함한다.
- P1 isolation 표에는 범위 밖 효과의 attempted/blocked/published 결과와 staged
  oracle/final repository correctness를 함께 둔다.
- §5.3a의 substrate validation은 1개 표로 유지하되 방법 세부는 supplement로 옮긴다.
- deterministic mechanism check에는 p-value를 붙이지 않고 expected invariant와 관측 일치로 보고한다.

### Discussion

- physical, structural, semantic, security의 네 층을 하나의 표로 정리하면 논문의 가장 재사용 가능한 산출물이 된다.
- 사용자 행동을 관찰하지 않은 상태에서는 design principle을 testable hypothesis로 쓴다.
- effect-level safeguard를 future work로 두지 말고 P1-1의 turn-local capability와
  staged validated commit을 구현·평가한다. 모델의 범위 밖 **시도**와 시스템이
  허용한 **게시**를 나누면 prompt 완화와 강제 격리의 차이가 명확해진다.

### Conclusion

- 현재 결론의 물리/의미 경계는 좋다.
- “viable shared-agent interface”보다 “the evaluated contract exposes…”처럼 측정 범위에 맞춘다.
- 사용자 연구가 없는 버전에서는 planned study 문장을 제거하고, 후속 연구 질문으로 끝낸다.

---

## 8. 권장 추가 실험: 최소안과 이상안

| 우선순위 | 질문 | 최소 수정 | 이상적인 수정 |
|---|---|---|---|
| P0 | lock이 mixed state를 막는가 | arm당 독립 20–30 run, run-level outcome | 플랫폼 2종, sampling sensitivity, exposure duration |
| P0 | 같은 실제 파일의 별칭도 막는가 | symlink·empty path regression | hard link·rename·TOCTOU stress arm |
| P0 | scoping이 semantic correctness를 높이는가 | 파일 내용 oracle + run-level 분석 | tests, answer coding, command effects까지 다층 평가 |
| P1 | 오염이 shared prompt assembly에서 생기는가 | turn-local filtered context 1개 arm | prompt scoping × context visibility 요인 실험 |
| P1 | live latency가 재현되는가 | n, CI, 실패 조건, model config 공개 | 다른 serving stack/model에서 독립 replication |
| P1 | effect scope 성능 경계가 명확한가 | metric 공식·n·분산 추가 | turn 수 2/4/8과 workload structure 변화 |
| P1 | 교차 요청 파일 효과를 강제 차단할 수 있는가 | canonical per-turn write capability + 범위 밖 tool call 차단 | staging + oracle/test + validated atomic commit |
| P1 | 같은 파일을 요구한 두 turn의 충돌을 안전하게 처리하는가 | write-set 사전 예약과 queue/reject | 동적 capability 확장 승인과 versioned commit |

가장 중요한 P1 추가 실험은 **turn-local filtered context arm**과
**capability + staged validated commit arm**이다. 전자는 오염의 prompt-assembly
대안 설명을 닫고, 후자는 모델 순응과 무관하게 파일 충돌을 막는 enforceable
boundary를 논문의 새 기술 기여로 만든다.

## 9. 추천하는 최종 기여 문장

현재 세 기여는 다음처럼 좁히는 것이 좋다.

1. **Interaction architecture:** one live coding-agent session에서 concurrent inference, attributed turns, ordered context commit, conflict-scoped cooperative effects를 결합한 명시적 contract.
2. **Measured systems trade-off:** serial/reject/parallel의 responsiveness, input-token premium, exclusive-effect collapse boundary를 같은 구현에서 측정한 결과.
3. **Layered isolation result:** structural ownership and cooperative effect
   ordering do not imply semantic request isolation; context scoping reduces
   cross-request behavior, while turn-local write capabilities and validated
   publication enforce a narrower cross-scope file boundary under explicit
   assumptions.

design space는 이 세 기여를 설명하는 framing으로 두는 편이 안전하다. 조사 절차를 체계화할 경우에만 독립 기여로 유지하라.

논문의 한 문장 핵심 주장은 다음이 가장 설득력 있다.

> Parallelizing inference below a shared-session interface can remove head-of-line waiting, but ordering commits and file effects does not determine which participant's request the model follows.

이 문장은 속도, 시스템 계약, 의미적 실패를 한 번에 묶고 인간 연구 결과를 전제하지 않는다.

## 10. 리뷰어가 저자에게 물을 질문

1. 왜 다른 in-flight user request를 현재 turn의 user-role context에서 제거하지 않고 prompt scoping으로만 구분했는가?
2. `own task completed`가 파일명 존재 외에 어떤 semantic oracle을 통과했는가?
3. 동일 실행 안의 2ms samples를 독립 표본으로 Fisher test에 넣는 것이 왜 정당한가?
4. symlink와 hard link가 같은 실제 파일을 가리킬 때 effect gate는 어떻게 충돌을 인식하는가?
5. `UNKNOWN`은 문서상 exclusive인데 실제 gate에서는 왜 unlocked이며, 새 plugin이 effect intent를 빠뜨리면 무엇이 막아 주는가?
6. primary distinct-task의 off/on arm이 서로 다른 수집 시점에 실행됐다는 사실이 결과 해석에 어떤 영향을 주는가?
7. 두 번째 모델에서 answer text의 cross-task tag가 `12/40 → 10/40`으로 거의 유지된 결과를 본문에서 제외한 이유는 무엇인가?
8. cancellation 시 이미 실행 중인 shell process와 workspace effect는 중단되는가, 완료되는가, 또는 결과만 숨겨지는가?
9. default cap을 올린 8-user 실험은 실제 기본 설정의 scaling에 대해 무엇을 말할 수 있는가?
10. 사람 연구를 완전히 빼도 남는 단 하나의 HCI 지식 기여는 무엇인가?

## 11. 우선순위별 수정 순서

### 제출 전에 반드시

- [x] integrity 실험을 독립 run 단위로 재실행하고 잘못된 Fisher 분석을 제거한다.
- [x] semantic 실험을 run/cluster 단위로 재분석하고 distinct-task arms를 같은 block에서 다시 수집한다.
- [x] `ownComplete` 대신 content/test oracle과 final-repository oracle을 사용한다.
- [x] symlink/hard-link alias와 `UNKNOWN` fail-closed fallback을 구현하고 claim을 한정한다.
- [x] guarantee 표를 assumption-bound invariant 표로 바꾼다.
- [ ] 기존 live latency arm의 model·hardware·decoding·missing-run 정보를 공개한다. P1 isolation arm 정보는 부분 완료다.
- [ ] 핵심 architecture/timeline figure와 UI figure를 완성한다.
- [ ] 사용자 연구 포함 여부를 결정하고, 포함하지 않는다면 §6 TODO와 RQ4를 전부 제거한다.

### 강하게 권장

- [x] turn-local filtered-context arm을 추가한다.
- [x] canonical per-turn write capability, conflict reservation, tool-boundary
  fail-closed enforcement를 구현한다.
- [x] turn-local staging에서 content/test oracle을 통과한 write set만 공유
  workspace에 publish하는 validated publication arm을 추가한다.
- [ ] replay/compaction/fairness/lifecycle 세부를 supplement로 이동한다.
- [ ] isolate-and-merge에 대한 우월성 인상을 제거하고 범위를 one-live-context contract로 한정한다.
- [ ] design-space survey procedure와 문서 snapshot을 남긴다.
- [x] token “cost”를 input-token premium으로 좁히고 output/compute 제외 범위를 쓴다.

### 있으면 논문을 크게 강화

1. capability 확장 승인과 충돌 알림이 사용자 흐름에 주는 비용을 first-use
   study에서 측정한다.
2. task oracle이 없는 일반 작업을 위한 “unvalidated staging”과 requester 승인
   흐름을 평가한다.
3. 별도 serving stack 또는 외부 모델에서 latency/semantic 결과를 복제한다.
4. physical/structural/semantic/security 네 층을 재사용 가능한 분석 프레임으로 정식화한다.

---

## 12. 최종 평가

이 초안은 P0 재실험과 P1 강제 경계까지 반영하면서 초기 Weak Reject의 핵심
타당성 문제를 대부분 해소했다. 분석 단위, 과제 oracle, 경로 별칭, effect
classification이 수정됐고, turn-local context 및 validated publication 대안도
같은 paired 설계에서 평가됐다. adversarial suite는 cross-scope publication,
동시 충돌 commit, 검증 실패 후 publication의 assumption-bound invariant를
직접 검사한다.

따라서 현재 기술적 중심은 **모델 행동을 완화하는 prompt/context layer와 파일
게시를 강제하는 capability/validation layer를 분리했다**는 데 있다. scoped arm의
응답 하나가 상대 태그를 언급한 결과 때문에도 일반 semantic correctness는 계속
보장할 수 없으며, 논문의 보장 이름은 “cross-request task-file publication
isolation and validated publication”으로 유지해야 한다.

남은 accept 리스크는 새 격리 메커니즘보다 제출 완결성에 가깝다. 사용자 연구
포함 여부, 핵심 그림, 기존 live latency 실험의 재현 정보, effect-share 표의 독립적
해석 가능성, isolate-and-merge 범위, design-space 도출 방법을 해결하면 기술 논문
기준으로 Weak Accept–Accept 후보가 된다. 이 평가는 사용자 연구 결과가 생기기
전에는 사람의 이해·신뢰·협업 성과를 주장하지 않는다는 조건을 전제로 한다.
