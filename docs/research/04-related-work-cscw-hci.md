# 다중 사용자–AI 협업 프로그래밍: 선행 연구 조사 (CSCW·SE·HCI)

> 조사 기준일: 2026년 8월. 활용처: 논문 Related Work 및 Novelty 평가.
> ⚠ 표시 항목은 검색 스니펫 기반이라 인용 전 원문 확인 필요.

**핵심 결론:** "여러 인간이 하나의 AI 코딩 **에이전트 세션**을 동기적으로 공유"하는 주제를 정면으로 다룬 학술 논문은 아직 없습니다. 다만 조사 중 **중요한 근접 연구 1건을 추가 발견**했으므로 novelty 주장 시 반드시 차별화해야 합니다(§6-f, Daryanto et al. 2026 — 2인간+1AI 프로그래밍 실험). 그 외 가장 가까운 것은 (a) 문서 편집 도메인의 CHI 2026 논문, (b) GitHub Next의 산업계 프로토타입 "Ace"입니다.

---

## 1. Human-AI Pair Programming (1인 개발자 + AI)

포화된 영역입니다. 관련연구에서는 "출발점"으로 짧게 처리하세요.

| 논문 | 저자/연도/학회 | 핵심 발견 |
|---|---|---|
| Is GitHub Copilot a Substitute for Human Pair-programming? | Imai, **ICSE 2022 Companion** | 참가자 21명 실험. Copilot은 추가 코드 라인 수 기준 생산성을 높이나, 이후 시행에서 삭제되는 라인이 더 많아 **코드 품질은 인간 페어 프로그래밍보다 열등**. https://dl.acm.org/doi/10.1145/3510454.3522684 |
| From Developer Pairs to AI Copilots | 2025, arXiv 2506.04785 | 인간 페어 vs. Copilot 단독. 지식 전달 에피소드 빈도는 유사하나 **개발자는 Copilot 제안을 인간 파트너 제안보다 덜 검증하고 수용**. https://arxiv.org/pdf/2506.04785 |
| Code with Me or for Me? How Increasing AI Automation Transforms Developer Workflows | Chen, Talwalkar, Brennan, Neubig, 2025, arXiv 2507.08149 | **코딩 에이전트에 대한 최초의 통제 실험**. copilot vs. agent 비교. 에이전트는 인간이 완수 못했을 작업까지 해내고 노력을 줄이나, **에이전트 행동에 대한 이해(comprehension)가 채택의 핵심 장벽**. https://arxiv.org/pdf/2507.08149 |
| SWE-chat: Coding Agent Interactions From Real Users in the Wild | Baumann, Padmakumar, Li, Yang, Yang, Koyejo, 2026, arXiv 2604.20779 | 실사용 세션 6,000건·프롬프트 6.3만·툴콜 35.5만. **패턴이 이분화** — 41%는 에이전트가 사실상 전부 작성(vibe coding), 23%는 인간이 전부. **사용자가 39% 확률로 제동을 거는데 에이전트는 거의 스스로 멈춰 확인하지 않음.** AI 작성 코드의 50% 미만만 생존. https://arxiv.org/abs/2604.20779 |
| Human-AI Experience in IDEs: A Systematic Literature Review | 2025, arXiv 2503.06195 | 서베이. 개별 논문 나열 대신 인용하기 좋음. https://arxiv.org/pdf/2503.06195 |

**프레이밍 요령:** 이 계열 전체가 암묵적으로 **"1 human : 1 agent"** 를 전제한다는 점을 명시하세요. SWE-chat의 "39% 제동" 수치는 특히 유용합니다 — 인간이 여럿일 때 *누가* 제동 권한을 갖는가라는 질문이 자연스럽게 파생됩니다.

## 2. 다중 사용자 + AI 에이전트 협업

**Collaborative Document Editing with Multiple Users and AI Agents** — Lehmann, Shauchenka, Buschek, **CHI 2026**. arXiv 2509.11826 / https://dl.acm.org/doi/10.1145/3772318.3790648 — **가장 중요한 선행 연구.**
- 문제의식: AI 글쓰기 도구가 개인용이라 공동 작업자가 AI를 쓰려면 **공유 작업공간을 떠났다가 결과를 재통합해야 하는** 워크플로 단절 발생.
- 시스템: 협업 편집기에 AI 에이전트 내장. **사용자 정의 에이전트 프로필 + 태스크**라는 두 개의 새 공유 객체 도입, AI 응답을 기존 코멘트 기능에 표시해 전원에게 가시화.
- 방법: 30명 / 14개 팀 / 1주일 실사용, 인터랙션 로그 + 인터뷰.
- 핵심 발견: **팀은 에이전트를 "팀원"으로 대하지 않고 기존 협업 패턴 안으로 흡수**. 에이전트 프로필은 *개인 작업공간*, 출력물은 *공동 자산*으로 기능. 저자권·통제·조율의 기존 규범이 그대로 적용됨.
- **차별화 축:** 문서 편집 vs. 코딩의 결정적 차이 — 실행 가능성, 부작용의 비가역성, 테스트/빌드라는 객관적 정답 신호, 리포지토리라는 공유 상태.

**Exploring Collaborative GenAI Agents in Synchronous Group Settings: Eliciting Team Perceptions and Design Considerations for the Future of Work** — Johnson, Peralta, Kaur, Huang, Zhao, Guan, Rajaram, Nebeling (미시간대·칭화대), **CSCW 2025 / PACM HCI**. DOI 10.1145/3757595, arXiv 2504.14779.
- 6개 팀 25명 전문가 speculative design 워크숍 + 후속 인터뷰. 33쪽.
- 주장: GenAI 도구가 개인용으로 설계되어 **집단 작업의 미묘함과 팀 역학을 반영하는 방법을 아직 모른다**. 동기적 그룹 환경 설계 고려사항 도출.

**Controlling AI Agent Participation in Group Conversations: A Human-Centered Approach** — **IUI 2025**. https://dl.acm.org/doi/10.1145/3708359.3712089
- Slack 기반 LLM 그룹 토론 참여자 프로토타입 **"Koala"**. 2회 사용자 연구로 아이디어 발상 역학 영향 + 인터랙티브 행동 설계 공간 탐색.
- **유용성:** "다자 대화에서 AI가 *언제* 발화할 것인가"라는 발언권(floor control) 문제를 다룬 드문 연구.

**GroupMemBench: Benchmarking LLM Agent Memory in Multi-Party Conversations** — 2026, arXiv 2605.14498. ⚠
- 문제의식이 거의 동일: 에이전트가 채널·스레드·프로젝트 공간에 배치되어 **여러 사용자가 같은 에이전트와, 그리고 서로와 상호작용하는데도 현행 에이전트 메모리 시스템은 거의 전적으로 단일 사용자 전용**이며 다자 메모리 역학은 미측정. https://arxiv.org/pdf/2605.14498

**Collaborating with AI Agents: Field Experiments on Teamwork, Productivity, and Performance** — 2025, arXiv 2503.18238.
- **Pairit** 플랫폼. 인간-인간/인간-AI 팀의 실시간 협업(동기화된 텍스트·이미지 인터페이스, 실시간 채팅·편집)을 팀·모델 수준 무작위 배정과 함께 지원하는 최초 실험 플랫폼이라 주장. **방법론적 선례**로 인용 가치. https://arxiv.org/html/2503.18238

기타: Multi-Agents are Social Groups (CSCW 2025) ⚠ — 단, "1인간 : N에이전트"로 본 연구 구도와 반대 방향.

## 3. 적용 가능한 CSCW 고전 이론

**고전 4종:**

1. **Grounding / Common Ground** — Clark & Brennan (1991), *Grounding in Communication*. 협업은 상호 이해의 점진적 축적을 요구하며 매체 제약이 grounding 비용을 결정. **다중 사용자 코딩 에이전트에서는 grounding이 삼자(A–B–Agent) 문제가 되며, A가 에이전트와 확립한 common ground를 B가 공유하지 못하는 비대칭이 핵심 현상.** → **가장 강력한 이론적 앵커.**

2. **Awareness / Workspace Awareness** — Dourish & Bellotti (CSCW 1992), *Awareness and Coordination in Shared Workspaces*; Gutwin & Greenberg의 workspace awareness 프레임워크. 에이전트의 현재 시선·편집 대상에 대한 awareness는 실시간 협업 편집기의 텔레포인터/커서 문제와 구조적으로 동형. 단, 에이전트는 훨씬 빠르고 광범위하게 상태를 바꿈.

3. **Articulation Work** — Strauss; Schmidt & Bannon (1992), *Taking CSCW Seriously: Supporting Articulation Work*, JCSCW 1(1). https://link.springer.com/chapter/10.1007/978-1-84800-068-1_3 — **AI 도입은 articulation work를 없애는 게 아니라 재분배·증폭**시킨다는 것이 §5 PR 개입 연구들이 일관되게 보여주는 바. Schmidt & Simone의 coordination mechanisms, Mark 등 *Articulation Spaces* (CSCW 2014, https://dl.acm.org/doi/10.1145/2531602.2531621) 참조.

4. **Mixed-Initiative Interaction** — Horvitz (CHI 1999), *Principles of Mixed-Initiative User Interfaces*. 12개 설계 원칙. **다자 환경에서는 "언제 개입할 것인가"에 "*누구에게* 개입할 것인가"가 추가**되며 Horvitz의 원칙은 이 확장을 다루지 않음 — 명확한 이론적 격차.

**고전을 LLM 에이전트에 적용한 최신 연구 (이론 갱신의 최신성 입증용):**
- **CollabSim: A CSCW-Grounded Methodology for Investigating Collaborative Competence of LLM Agents** — Chen, Sun, Lu, Wang, Wang, Yao, 2026, arXiv 2606.06399. 다중 에이전트 실패는 개별 추론력 부족이 아니라 **"collaborative competence"**(common ground 확립, 공유된 과제 이해 유지, 개인/집단 인센티브 균형, 어긋남 복구)의 부재에서 온다고 주장. 기존 평가가 과제 성과·개별 추론에만 집중해 이를 놓친다고 지적. https://arxiv.org/abs/2606.06399
- **From Human-Human Collaboration to Human-Agent Collaboration** — 2026, arXiv 2602.05987. 원격 협업에서 인간이 빈약한 채널로 명시적 신호에 의존해 common ground를 유지한다는 CSCW 통찰을 human-agent 설계 철학으로 전이. https://arxiv.org/html/2602.05987
- **From Task Solvers to Teammates: A Theory-Grounded Architecture for Advancing Collaboration Readiness in LLM Agents** — Microsoft Research. common ground와 workspace awareness를 외부화한 모듈형 **Collaborative Readiness Layer** 제안.
- **Through the Lens of Human-Human Collaboration: A Configurable Research Platform for Exploring Human-Agent Collaboration** — 2025, arXiv 2509.18008.

## 4. 실시간 협업 편집 + AI

§2의 Lehmann et al. (CHI 2026)이 대표작이자 사실상 유일한 본격 연구. 추가로:
- **"It Felt a Bit Eerie": Exploring Humanlike Interactions During Collaborative Writing with an Artificial Agent** — 2026, arXiv 2605.24729. ⚠
- 문헌 공통 지적: **AI 글쓰기 도구는 실시간 협업 편집이 아니라 턴 기반(turn-based) 생성 패러다임을 모방**하며 이것이 인간-인간 협업과 인간-AI 협업 사이 설계·역학 격차를 만듦.
- 역사적 대조점: Google Docs가 2010년 실시간 편집 가시화를 도입해 인간-인간 awareness 표준이 되었으나 AI는 이 패러다임에 편입되지 못함.

## 5. 자율성 / 제어권 공유 (2023–2026)

**Position: Humans are Missing from AI Coding Agent Research** — Zora Zhiruo Wang 외, 2026년 2월. https://zorazrw.github.io/files/position-haicode.pdf — **gap 주장의 가장 직접적 근거.**
- **인간과 AI 코딩 시스템 사이의 공개 대화 데이터가 현저히 부족**하며, **개발자가 어떻게 프롬프트하고, 조종하고(steer), 뒤집고(override), 최종적으로 에이전트 산출 코드를 커밋하거나 폐기하는지를 담은 공개 데이터셋이 없다**.
- **Steerability는 현행 코딩 에이전트 시스템에서 약하게만 지원되며 기존 연구는 대체로 서술적이거나 주변적**.
- 사용자는 **granularity spectrum**의 서로 다른 지점에서 작동 — 고수준 의도만 주고 폭넓게 탐색시키거나(vibe coding), 세밀히 개입해 국소 수정.
- ⚠ **PDF 자동 추출 2회 실패**(2.7MB, 텍스트 레이어 미추출). 위 내용은 검색 스니펫 기반이므로 **인용 전 수동 확인 필수.**

- **How Coding Agents Fail Their Users: A Large-Scale Analysis of Developer-Agent Misalignment in 20,574 Real-World Sessions** — 2026, arXiv 2605.29442. ⚠ 제어권 실패 유형론 근거. https://arxiv.org/pdf/2605.29442
- **Cocoa: Co-Planning and Co-Execution with AI Agents** — 2024/2025, arXiv 2412.10999. 계획·실행 양쪽의 인간-에이전트 통제권 분할 인터랙션 모델. shared control 설계 공간 논의에 인용 가치 높음. https://arxiv.org/pdf/2412.10999
- **The Conversations Beneath the Code: Triadic Data for Long-Horizon Software Engineering Agents** — 2026, arXiv 2605.02244. ⚠
- **ResearStudio: A Human-Intervenable Framework for Building Controllable Deep-Research Agents** — 2025, arXiv 2510.12194.
- **Adaptive Human-Agent Teaming: A Review from the Process Dynamics Perspective** — 2025, arXiv 2504.10918. 서베이.

## 6. 핵심 질문 — "여러 인간이 하나의 AI 코딩 에이전트 세션을 공유"

**학술 문헌에 정면으로 다룬 연구는 없습니다.** 근접도 순:

**(a) 산업계 프로토타입 — GitHub Next "Ace" (Agent Collaboration Environment).** 학술 논문 아님, 연구 프로토타입. 현재 수천 명 규모 technical preview. **랜딩 페이지 확인됨: https://ace.githubnext.com/**
- **실시간 멀티플레이어 코딩 에이전트 워크스페이스** — Slack + GitHub + Copilot 결합. 팀원 전원이 **채팅·컨텍스트·클라우드 microVM을 공유**하고 동일 에이전트에 접근.
- 구성: Slack형 채팅, 참여자 전원의 공유 터미널 접근, 팀 전체가 보는 라이브 프리뷰, (수동 개입 시) 실시간 멀티플레이어 코드 편집, Ace 세션↔GitHub PR 양방향 링크. macOS 네이티브 앱 + 터미널 인터페이스.
- **명시적 문제 정의: "2026년 초 현재 모든 코딩 에이전트는 단일 플레이어(single-player) 경험으로 설계되어 있으나 소프트웨어 구축은 단일 플레이어 게임이 아니다."** 개인 구현 속도만 높이고 팀 조율을 지원하지 못해 **낭비된 작업·조율 부채(coordination debt)·어긋난 산출물**이 발생. 개발자뿐 아니라 디자이너·PM까지 같은 대화에 포함하는 것이 목표.
- 관련 발표: Maggie Appleton, *One Developer, Two Dozen Agents, Zero Alignment*, https://githubnext.com/talks/one-developer-two-dozen-agents-zero-alignment/
- **활용:** "산업계는 이 문제를 인지하고 시스템을 만들고 있으나 경험적·이론적 연구는 부재" — 설계는 앞서가고 이해는 뒤처져 있다는 구도.

**(b) Claude Code Issue #60082** — 두 개의 서로 다른 사용자 계정이 Google Docs나 VS Code Live Share처럼 하나의 Claude Code 세션을 실시간 공유·협업하게 해달라는 기능 요청. **실사용자 수요의 1차 증거.** https://github.com/anthropics/claude-code/issues/60082

**(c) Lehmann et al., CHI 2026** (§2) — 도메인만 다른 동형 문제.

**(d) 다중 에이전트 병렬 실행 문헌** — git worktree 기반 병렬 에이전트, Claude Code Agent Teams(공유 태스크 리스트, 의존성 추적, 피어 메시징, 파일 락) 등은 **"1인간 : N에이전트"** 로 본 주제(N인간 : 1에이전트)와 직교. 관련연구에서 명시적 구분 권장.

**(e) 에이전트 PR 리뷰 연구군** — 비동기적으로는 이미 "여러 인간이 하나의 에이전트 산출물을 다룸":
- **When AI Teammates Meet Code Review: Collaboration Signals Shaping the Integration of Agent-Authored Pull Requests** — MSR 2026, arXiv 2602.19441. AIDev 데이터셋 대규모 분석. **리뷰어 참여도가 성공적 통합과 가장 강한 상관**, 큰 변경 규모나 force push 같은 **조율 교란(coordination-disrupting) 행동은 머지 가능성을 낮춤**. https://arxiv.org/html/2602.19441
- **Behind Agentic Pull Requests: An Empirical Study on Developer Interventions in AI Agent-Authored Pull Requests** — MSR 2026 Mining Challenge. 인간 개입을 노력·감독의 척도로 측정. **코딩 에이전트 협업이 개발자 작업을 구현에서 감독·지도·품질 통제로 이동**시킴.
- 기타: How Do AI Coding Agents Contribute to Software Development? (arXiv 2607.21832), Security in the Age of AI Teammates (arXiv 2601.00477), Early Adoption of Agentic Coding Tools by GitHub Projects (arXiv 2607.14037), AI IDEs or Autonomous Agents? (arXiv 2601.13597).
- **차별화 포인트:** 전부 **비동기적·산출물 사후(post-hoc)** 협업. 에이전트가 혼자 일하고 인간들이 나중에 결과물을 두고 협업. **세션 진행 중의 동기적 공유는 미다룸.**

**(f) ⚠ 중요 — 반드시 차별화 필요: Human-Human-AI Triadic Programming: Uncovering the Role of AI Agent and the Value of Human Partner in Collaborative Learning** — Daryanto, Ding, Ping, Wilhelm, Chen, Brown, Rho, 2026, arXiv 2601.12134. **원문 확인 완료.**
- **구도가 본 주제와 가장 유사한 유일한 실증 연구.** 20명 within-subjects 설계로 **Dyadic(1인간+AI) vs. Triadic(2인간+AI)** 프로그래밍을 직접 비교. 즉 **2명의 인간이 하나의 AI와 동시에 프로그래밍하는 상황을 실제로 실험한 연구가 존재합니다.**
- 핵심 발견: **삼자 협업이 이자(dyadic) 기준선 대비 협업 학습과 사회적 실재감(social presence)을 향상**. 삼자 조건에서 **AI 생성 코드 의존도가 유의하게 감소**했으며, 효과는 **"HHAI-shared" 조건**에서 가장 두드러짐 — **동료가 AI 사용을 볼 수 있다는 사실이 AI 제안을 적용 전에 이해해야 한다는 책임감을 증대**시킴. 동료 책임성(peer accountability)이 수동적 수용 대신 깊은 학습 관여를 촉진.
- **차별화 논거 (반드시 논문에 포함):** ① 맥락이 **협업 학습(CSCL)/교육**이지 전문 소프트웨어 개발이 아님. ② AI가 **제안 기반 어시스턴트**이지 파일을 쓰고 명령을 실행하는 **자율 에이전트 세션**이 아님. ③ **실험실 과제 20명 단발성**이지 지속적 실무 협업이 아님. ④ 종속변수가 **학습 성과·사회적 실재감**이지 조율 비용·제어권 중재·grounding 비대칭이 아님. ⑤ "공유 세션"의 지속성·상태(state) 문제를 다루지 않음.
- **동시에 기회:** 이 논문의 "동료 가시성이 AI 산출물 검증을 촉진한다"는 발견은 본 연구의 **가설 근거**로 쓸 수 있는 강력한 재료입니다. 자율 에이전트 환경에서도 동일한 효과가 나타나는지, 아니면 에이전트의 속도·범위 때문에 무너지는지가 좋은 연구 질문이 됩니다.

## 7. 연구 격차 (Research Gap)

앞의 5개가 논문 기여로 만들기 가장 좋습니다.

1. **단일 사용자 가정의 편재성.** Copilot 사용자 연구부터 최신 에이전트 실증 연구(SWE-chat, Code with Me or for Me)까지 전부 1:1을 전제. GroupMemBench가 지적하듯 에이전트 **메모리/컨텍스트 시스템조차 단일 사용자 전용**이라 다자 컨텍스트 역학은 측정된 적이 없음.

2. **동기적 공유 세션의 공백.** 다중 인간-에이전트 연구는 (i) 비동기·사후(PR 리뷰 연구군)이거나 (ii) 코딩이 아닌 도메인(문서 편집·그룹 토론·아이디어 발상)이거나 (iii) 교육 실험실 환경(§6-f). **동기적 + 전문 코딩 + 다중 인간 + 자율 에이전트**의 교집합은 비어 있음.

3. **다자 환경에서의 제어권 중재(control arbitration).** Horvitz의 mixed-initiative 원칙과 shared control 문헌은 "에이전트가 언제 개입하는가"를 다루지만, **여러 인간 중 누가 승인/중단/되돌리기 권한을 갖는가, 충돌하는 지시가 동시에 들어올 때 어떻게 되는가**는 이론적·시스템적으로 미해결. SWE-chat의 "사용자 39% 제동"과 결합하면 강력한 문제 제기.

4. **비대칭적 grounding.** A가 에이전트와 긴 대화로 쌓은 common ground를 B는 갖지 못한 채 같은 세션에 참여할 때의 결과. Clark & Brennan을 삼자·비대칭 구도로 확장하는 것은 **이론적 기여**가 될 수 있음.

5. **에이전트 행동에 대한 workspace awareness.** Chen et al.이 comprehension을 채택 장벽으로 지목했으나 이는 인간 1명 기준. 에이전트가 초 단위로 여러 파일을 바꿀 때 **여러 인간이 동시에 상황을 파악하는 awareness 메커니즘**은 설계된 바 없음. Gutwin & Greenberg 프레임워크를 인간 아닌 에이전트 행위자에 적용하는 것이 자연스러운 확장.

6. **책임 소재와 저자권.** Lehmann et al.은 팀이 에이전트를 팀원이 아니라 **개인 작업공간(프로필) + 공동 자산(출력)**으로 다룬다고 발견. 코드에서는 훨씬 첨예 — 실행 가능하고 부작용이 비가역적이며 리포지토리라는 공유 상태를 바꾸기 때문. **다중 인간이 공유한 에이전트가 만든 코드의 책임 귀속**은 미탐구.

7. **공개 데이터 부재.** Wang et al.이 지적한 대로 인간-에이전트 상호작용 공개 데이터가 부족하며 **다중 인간 세션 데이터는 아예 없음.** 데이터셋 자체가 기여가 될 수 있음.

8. **조율 비용의 이전(displacement), 소멸 아님.** 에이전트 PR 연구들은 작업이 구현에서 감독으로 이동한다고 보고. Schmidt & Bannon의 articulation work 렌즈로 보면 **에이전트는 조율 노동을 제거하는 게 아니라 새로운 형태의 articulation work를 생성.** 이를 공유 세션 맥락에서 실증한 연구는 없음.

## 8. Novelty 주장 권장 프레이밍

> 코딩 에이전트 연구는 압도적으로 단일 개발자를 가정해 왔다(§1). 다중 인간-AI 협업 연구는 코딩이 아닌 도메인에 머물러 있고(§2, §4), 코딩에서의 다자 협업 연구는 세션이 끝난 뒤의 PR 리뷰만을 다루며(§6-e), 2인간+AI를 다룬 유일한 실증 연구는 제안 기반 어시스턴트를 쓰는 교육 실험실 환경이다(§6-f). 산업계는 이미 멀티플레이어 에이전트 워크스페이스를 만들고 있으나(Ace), **이를 뒷받침하는 경험적 이해도 이론적 프레임워크도 존재하지 않는다.**

가장 안전한 이론적 앵커는 **grounding 비대칭 + 제어권 중재** 조합입니다. 둘 다 확립된 CSCW 이론에 뿌리를 두면서 기존 문헌이 명백히 다루지 않은 확장이기 때문입니다.

## 9. 인용 전 수동 확인 권장

1. **https://zorazrw.github.io/files/position-haicode.pdf** — gap 주장의 키스톤인데 PDF 텍스트 추출 2회 실패. 저자 전체 목록·정확한 venue·taxonomy 확인 필요.
2. **arXiv 2605.14498 (GroupMemBench)**, **2605.29442 (How Coding Agents Fail Their Users)**, **2605.02244**, **2605.24729** — 스니펫 기반이라 수치·주장 확인 필요.
3. **Ace** — 학술 인용이 불가능한 산업 프로토타입이므로, 인용 형식(웹사이트 접근일자 명기) 결정 필요. https://ace.githubnext.com/
