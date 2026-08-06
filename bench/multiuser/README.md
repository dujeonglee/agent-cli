# bench/multiuser — 다중 사용자 동시성 계약 벤치 하네스

"병렬 추론 + 직렬 부수효과" 계약(`--concurrency-contract` / `--lock-scope`)의
성능·무결성·공정성을 **결정적으로** 측정한다. 포크(Coagora)
`backend/bench/`(mockLlm.mjs, e2-hol.mjs, e1-ablation.mjs)의 본류 재현이며,
같은 실험을 두 독립 구현(TS 이벤트루프 서버 / Python 스레드 CLI)에서 돌려
계약의 **구현 독립성**을 검증하는 것(N2)이 목적의 하나다.

## 구성

| 파일 | 역할 |
|---|---|
| `mock_llm.py` | 결정적 목 LLM — OpenAI 호환 `/chat/completions` SSE. 지연은 프롬프트 안 `[[bench ttft= tok= n= work= fwrite= fpath= marker= lines= id=]]` 지시자로 제어(포크 mockLlm.mjs 와 동일 문법). 포크와 달리 도구 호출을 **json_fc content op** 로 흘린다(본류 wire 형식). 압축 요약 콜은 지시자보다 먼저 감지해 짧게 응답(`MOCK_LLM_SUM_MS`). `MOCK_LLM_CTX` 로 광고 컨텍스트 창 제어(N1 압축 유발) |
| `driver.py` | 공용 드라이버 — 서버/목 기동(HOME 격리·워크스페이스 격리), 입력 주입, `turns.jsonl`(M2 계측) 파싱, 턴 사슬 해석(`turn_chain`/`ttft_ms`), 통계(p50/p95/기울기) |
| `e2_hol.py` | **P1/N2**: HOL 지연 — 3계약 × L{2,6,15,30}s × reps. 핵심 지표 = B TTFT ~ L 회귀 기울기 (포크: 직렬 1.000 / 거부 1.010 / 병렬 0.000) |
| `e1_ablation.py` | **P3/N2**: 효과 락 ablation — lock{off,workspace,conflict} × 동일 파일 동시 쓰기. 위반 = 두 마커 공존(mixed). 본류의 손상 메커니즘은 truncate/write 인터리브(포크는 스트림 인터리브 — One Contract, Two Runtimes) |
| `n1_compaction.py` | **N1**: 동시 턴 하의 낙관적 압축 — 압축 무락 구간 안에서 타 턴 이벤트 지속(가용성), stale 재시도 수, 질의 유실 0 + 귀속 정합(정확성) |
| `n3_attribution.py` | **N3**: 병렬 귀속 정확도 — 마커 왕복 검사로 오귀속률 측정(가설: 0) |
| `p4_fairness.py` | **P4**: per-user 공정성 — 게이트 on/off 두 팔(`--no-per-user-gate` ablation), 단기 사용자 대기 절대값 대조 |
| `p2_grid.py` | **P2**: 붕괴 경계 — 쓰기 횟수·충돌·락 스코프 그리드(+ 셸 팔은 out/p2-shell-arms.json). 붕괴는 배타 효과의 **시간 비중** 함수 |
| `p2_scope.py` | **P2-SCOPE**: 왜 락 경계를 워크스페이스→충돌 단위로 좁혔는가(§4.4 근거) — 효과 시간 비중 {50,75,90}% × 경로{서로소, 동일} × 스코프{workspace, conflict, off(참조)}. E1 과 같은 층(LLM 우회, 도구+락 직접 구동). 쓰기 1회 실지속시간을 **실행 시점에 캘리브레이션**해 비중 축을 만든다 |
| `n4_replay.py` | **N4**: 늦은 합류자 증분 재생 정합성(v0.8 1단계 검증) — 안 끊긴 구독(control)과 `Last-Event-ID` 로 반복 재접속하는 구독(cutter)을 동시에 돌려 **seq 열 + 페이로드 원문**을 대조. 살릴 수 없는 커서의 `replay_reset` 폴백도 강제 |
| `p6_real_llm.py` | **P6**: 실 LLM(온프렘, `AGENT_CLI_*` env) — HOL 순위 보존 스팟체크 + 같은 3-메시지 워크로드의 직렬 vs 병렬 토큰 계정(`llm_call` usage 이벤트) |
| `p7_lifecycle.py` | **P7**: 장기 세션 — 3 사용자 204턴, 페이즈 사이 서버 종료→`--resume` 재개 ×3. 보존 100%·id 전역 유일·압축 유계 지속 |
| `out/` | 커밋되는 원시 데이터 + 요약 (재현 가능성 규약 — 포크와 동일) |

## 실행

```bash
# 전체 P1 그리드 (약 30분, 결과는 out/e2-hol.jsonl + out/e2-summary.json)
.venv/bin/python bench/multiuser/e2_hol.py --reps 20

# 락 ablation
.venv/bin/python bench/multiuser/e1_ablation.py --reps 3 --writes 8

# 동시 압축 / 귀속 / 공정성
.venv/bin/python bench/multiuser/n1_compaction.py
.venv/bin/python bench/multiuser/n3_attribution.py
.venv/bin/python bench/multiuser/p4_fairness.py

# 락 스코프 동기 (§4.4) / 재생 정합성 (§1단계)
.venv/bin/python bench/multiuser/p2_scope.py --reps 5 --rounds 40
.venv/bin/python bench/multiuser/n4_replay.py --users 3 --rounds 30 --cuts 10

# L 하나만 추가할 때는 --append (전체 그리드 재실행 없이 합쳐 저장,
# 요약은 언제나 합쳐진 원시 파일에서 재도출)
.venv/bin/python bench/multiuser/e2_hol.py --levels 30000 --reps 20 --append
```

의존성 없음(stdlib) — 리포의 `.venv` 와 `agent-cli` 설치만 전제한다.
각 조건은 새 임시 워크스페이스 + `HOME` 격리로 돌므로 사용자 설정
(`~/.agent-cli`)을 읽지도 쓰지도 않는다.

## 측정 방법론

측정은 클라이언트가 아니라 **서버 내부 계측**(M2, `--turn-metrics` →
`{session_dir}/turns.jsonl`)을 읽는다. 한 프로세스의 단조 시계(`mono_ms`)로
enqueue → dispatch → first_token → complete 사슬을 계산하므로 시계 차 보정이
없고, 거부 계약의 재시도 대기는 서버가 본 첫 409(`reject` 이벤트)부터
잰다. TTFT = `first_token − min(enqueue, first_reject)`.

주의: 턴당 고정 오버헤드(run_loop 조립 + 목 ttft)는 모든 계약에 동일하게
들어가므로 **기울기에는 영향이 없다**. 절대값 비교는 같은 하네스 안에서만
유효하다.

그 고정 오버헤드는 **실행 세션 사이에 이동한다**. `e2-hol.jsonl` 은
L{2,6,15}s 9셀과 L=30s 3셀이 서로 다른 세션에서 측정된 합집합이며(`--append`),
그 사이 오버헤드가 약 56 ms 줄었다. `out/e2-drift-control.json` 이 같은 세션에서
L=15000 을 다시 재 그것을 확인한 대조 측정이다. 읽는 규칙:

- **기울기**는 상수항 이동에 불변이다(회귀에서 상쇄) — 핵심 지표는 안전하다.
- **같은 L 안의 계약 간 비교**(serial÷parallel 배수 등)도 안전하다. 각 세션이
  그 레벨의 3계약을 한 번에 돌렸으므로 언제나 동일 세션 비교다.
- **레벨 간 절대값**만 세션 경계를 넘는다. 배수를 인용할 때 어느 L 의 것인지
  밝히고, 서로 다른 L 의 절대 p50 을 직접 빼지 말 것.
