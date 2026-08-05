# research/ — 다중 사용자 AI 코딩 에이전트 세션 연구 조사 자료

2026-08-05 수행한 논문 준비 조사·기획 결과. 주제: **여러 인간이 하나의 자율 AI 코딩 에이전트 세션을 공유하는 환경**의 학술적 신규성과 논문화·실험 전략.

## 프로젝트 관계 (필독)

- **agent-cli(본 리포지토리) = 본류(mainline).** Python 기반 ReAct 에이전트 CLI로, 다중 뷰어 공유 세션(직렬 계약)·상주 서브에이전트·컨텍스트 compaction·SWE-bench 하네스를 갖춘 기준 코드베이스다.
- **Coagora(구칭 Aidit-Code) = 본류의 포크(fork).** 다중 사용자 **병렬 턴** 기능군 — 병렬 추론+직렬 부수효과 계약, turnId 다중화, 부수효과 계층 락, per-user 공정 큐, 턴 단위 인터럽트 — 을 먼저 개발·실측 검증한 실험 분기다.
- 따라서 "Coagora에 있고 agent-cli에 없는 기능"(10 문서)은 **포크에서 검증된 뒤 아직 본류에 역병합되지 않은 백로그**이며, 병합 절차는 11 문서가 정의한다. 병합 완료 후 동시성 계약 코드의 단일 출처는 본류가 되고, Coagora는 커뮤니티/UI 계층 분기로 역할이 재정의된다.

## 문서 목록

| 문서 | 내용 |
|---|---|
| [01-repo-analysis-aidit-code.md](01-repo-analysis-aidit-code.md) | 포크(Coagora) 기술 분석 — "게시글=세션" 모델, 병렬 추론+직렬 부수효과 계약, 4계층 동시성 방어, 실측 자산 |
| [02-messenger-based-ai-coding-tools.md](02-messenger-based-ai-coding-tools.md) | 메신저 기반 도구 조사 — Claude Tag, Codex, Copilot, Devin, Cursor의 아키텍처·컨텍스트·협업 한계 (출처 URL 포함) |
| [03-repo-analysis-agent-cli.md](03-repo-analysis-agent-cli.md) | 본류(agent-cli) 기술 분석 — 공유 워커 멀티플레이어 모델, ReAct 회복 하네스, 상주 서브에이전트, 컨텍스트 압축 |
| [04-related-work-cscw-hci.md](04-related-work-cscw-hci.md) | CSCW/SE/HCI 선행 연구 — 근접 연구(Daryanto, Lehmann, Ace)와 차별화 논거, 연구 격차 8종, CSCW 이론 앵커 |
| [05-comparison-messenger-vs-shared-cli-session.md](05-comparison-messenger-vs-shared-cli-session.md) | 메신저 태그 방식 vs 공동 세션 방식 — 구조 4축 설계 공간, CSCW 이론 렌즈 UX 비교 |
| [06-novelty-research-questions-methodology.md](06-novelty-research-questions-methodology.md) | 신규성 판정(조건부 있음), RQ 7개, 차별화 요소 6종, 평가 방법론(벤치마크+사용자 연구), 다음 단계 |
| [07-new-paper-draft.md](07-new-paper-draft.md) | 신규 논문 기획 — 새 제목("Multiplayer Coding Agents"), 절별 설계, 특허 청구항↔논문 절 매핑, 기존 PAPER.html과의 차별화 |
| [08-usecase-performance-experiment-plan.md](08-usecase-performance-experiment-plan.md) | 유스케이스 6종(U1–U6) + 성능 실험 8종(P1–P8) 설계·가설 + 사용자 연구 로드맵(S1–S3) + 실행 순서 |
| [09-full-paper-draft.md](09-full-paper-draft.md) | **논문 전체 본문(영문, 투고용 초안)** — Abstract, §1–§8, 참고문헌 32건. 실측(✔)과 미실행([TODO]) 구분 표기 |
| [10-agent-cli-gap-analysis.md](10-agent-cli-gap-analysis.md) | 본류 미병합 기능 백로그 — 격차 매트릭스(A–E), 실험 P1–P8별 본류 실행 가능성 판정. §3 전략 α/β는 11로 대체됨 |
| [11-upstream-merge-plan.md](11-upstream-merge-plan.md) | **포크→본류 역병합 계획** — 계약 이식 원칙, 컴포넌트 대응표, 병합 단계 M0–M6, 단계별 해금 실험, 검증 게이트, 리스크 |

읽는 순서: 연구 결론은 **06 → 05 → 04**, 논문은 **09**(전문)·07(기획), 개발 착수는 **11 → 10**, 시스템 근거는 01(포크)·03(본류), 도구 현황은 02.

## 명명 규약

시스템명은 **Coagora**(구칭 Aidit-Code). 파일시스템 경로·리포지토리명(`D:\yoon\codes\Aidit\Aidit-Code`, `Aidit-Code/backend/...`)은 리포 개명 전까지 기존 표기를 유지한다. "Aidit" 단독 표기는 선행 모출원(특허)의 명칭이므로 변경하지 않는다.

## 주의

- 04 §9에 표기된 미확인 출처(⚠)는 인용 전 원문 검증 필요.
- 병합 작업은 본류 `CLAUDE.md` 규약(테스트+README+ARCHITECTURE.md+ruff를 한 커밋으로)을 따른다 — 11 문서 §2 참조.
