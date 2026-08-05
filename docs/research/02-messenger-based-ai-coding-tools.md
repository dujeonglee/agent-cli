# 메신저 기반 AI 코딩 에이전트 조사 (2026년 8월 기준)

> 조사 목적: 슬랙/메신저 기반 AI 코딩 도구(Claude Tag, Codex 등)의 작동 방식, 컨텍스트 전달 및 협업 한계점 분석.
> 활용처: 논문 Related Work / Motivation 섹션.

## 0. 요약

Slack/Teams에 올라온 AI 코딩 에이전트들은 벤더가 달라도 거의 동일한 아키텍처로 수렴했습니다. **`@태그` 호출 → 스레드 히스토리를 컨텍스트로 흡수 → 클라우드 샌드박스(VM)에서 리포지토리 클론 및 작업 → PR 링크를 스레드에 회신**하는 비동기 위임(delegation) 구조입니다. 논문의 motivation으로 쓸 만한 공통 한계는 세 가지로 압축됩니다: (1) 컨텍스트 단위가 스레드에 갇혀 있고 세션 지속성이 벤더별로 제각각, (2) 작업이 비동기 일괄 처리라 실행 중 실시간 개입·협업이 구조적으로 어려움, (3) 다중 사용자가 하나의 에이전트 인스턴스를 공유하면서 권한 귀속과 승인 흐름이 모호해짐(그리고 스레드 전체가 컨텍스트로 빨려 들어가면서 간접 프롬프트 인젝션 표면이 됨).

---

## 1. Claude in Slack / Claude Tag (Anthropic)

2026년 6월 발표된 **Claude Tag**는 기존 "Claude in Slack" 앱을 대체하는 Slack 네이티브 에이전트입니다. Claude Enterprise/Team 대상 베타로 출시되었습니다.

**설치 및 권한 모델.** 설정은 4단계(Slack 페어링 → 툴 접근 권한 부여 → 월간 지출 한도 설정 → 프라이빗 채널 테스트)이며, Slack Primary Owner/Owner만 구성할 수 있습니다. 관리자가 **채널 단위로** 어떤 툴과 데이터에 접근할지 통제합니다. 지출 한도는 조직 전체 및 채널별로 설정되고 75%/95% 임계치 알림이 오며, 한도를 넘는 작업은 아예 거부됩니다.

**호출 방식.** 채널/스레드에서 `@Claude` 태그, DM, 그리고 Slack AI assistant 패널 세 경로가 있습니다. 태그되면 요청을 단계로 분해해 순차 실행한 뒤 스레드에 결과를 회신합니다.

**리포지토리 접근 및 코드 실행.** 가장 특징적인 부분은 **에이전트가 자체 아이덴티티(identity)를 갖는다**는 점입니다. Claude는 연결된 각 툴에 대해 자기 계정을 보유해 Slack에는 Claude 앱으로, GitHub에는 **자체 GitHub App으로 PR을 열고**, 데이터 웨어하우스에는 전용 서비스 계정으로 질의합니다. Datadog/Linear/GitHub를 엮어 근본 원인을 추적하고 수정 PR 초안까지 작성하는 워크플로가 공식 사례로 제시됩니다. 채널에서의 작업은 조직에 과금되고, DM에서의 작업은 개인 계정 크레딧에 과금됩니다.

**다중 사용자 동작.** Anthropic이 "멀티플레이어"라고 부르는 특성으로, **채널당 하나의 Claude 인스턴스를 팀 전체가 공유**합니다. 누구나 진행 중인 작업을 조종하거나 이전 사람이 멈춘 지점부터 이어받을 수 있고, 팀은 진행 상황을 비동기로 관찰합니다. 메모리는 조직 전체 / 워크스페이스(공개 채널) / 프라이빗 채널의 3계층으로 격리되며, 도메인 간 메모리 공유는 차단됩니다(영업용 인스턴스가 엔지니어링 데이터나 학습 내용에 접근하지 못함).

출처:
- https://www.anthropic.com/news/introducing-claude-tag
- https://support.claude.com/en/articles/15594475-what-is-claude-tag
- https://claude.com/product/tag
- https://techcrunch.com/2026/06/23/anthropics-claude-tag-is-learning-your-company-one-slack-message-at-a-time/
- https://thenewstack.io/anthropic-claude-tag-slack/

---

## 2. OpenAI Codex Slack 연동

Codex GA(general availability) 시점에 Slack 연동이 함께 공개되었습니다.

**설치 및 전제조건.** ChatGPT Plus/Pro/Business/Edu/Enterprise 플랜, 연결된 GitHub 계정, **최소 하나의 구성된 클라우드 환경(environment)**이 필요하며 워크스페이스 관리자 승인이 요구될 수 있습니다. Codex 설정에서 앱을 설치한 뒤 원하는 채널에 `@Codex`를 추가합니다.

**호출 및 컨텍스트.** 채널이나 스레드에서 `@Codex`에 프롬프트를 붙여 멘션하면 됩니다. 공식 문서는 "스레드의 이전 메시지를 참조할 수 있어 컨텍스트를 다시 진술할 필요가 없는 경우가 많다"고 명시합니다. 리포지토리를 명시적으로 지정하는 문법도 지원합니다 (`@Codex fix the above in openai/codex`).

**환경/리포 선택.** Codex가 접근 가능한 환경들을 검토해 가장 적합한 것을 자동 선택하고, 모호하면 **가장 최근 사용한 환경으로 폴백**합니다. 작업은 환경의 repo map에 정의된 **기본 브랜치 기준으로 실행**됩니다.

**실행 및 회신.** 멘션되면 눈 이모지 리액션을 달고, 클라우드 chat 링크를 답글로 남긴 뒤, 완료 시 결과를 스레드에 포스팅합니다. 즉 **Slack은 트리거이자 알림 채널이고 실제 작업 공간은 Codex cloud**라는 이원 구조입니다. 엔터프라이즈 관리자는 워크스페이스 설정으로 Slack 스레드에 답변 본문을 게시할지 여부를 제한할 수 있습니다.

**보고된 한계.** 연결 해제된 계정에는 접근할 수 없고, 적절한 환경 구성이 선행되어야 하며, "실수할 수 있으므로 출력과 diff를 반드시 검토하라"고 문서가 명시합니다. 서드파티 분석은 이것이 **코딩 작업 런처에 국한**된다는 점을 지적합니다 — 채널을 감시하거나 요청을 라우팅하거나 후속 조치를 보내지 못하며, GitHub 리포·환경·플랜 티어·관리자 승인 설치를 전제하므로 비기술 사용자는 사실상 사용할 수 없습니다.

출처:
- https://openai.com/index/codex-now-generally-available/
- https://learn.chatgpt.com/docs/third-party/slack (구 URL developers.openai.com/codex/integrations/slack 에서 308 리다이렉트)
- https://slack.com/marketplace/A09F5C369E3-openai-codex
- https://www.usecarly.com/blog/codex-slack-integration/
- https://www.neowin.net/news/openai-codex-hits-general-availability-with-new-slack-integration/

---

## 3. 기타 메신저 연동 도구

### 3.1 GitHub Copilot (Slack / Microsoft Teams)

전제조건은 유료 Copilot 플랜, Slack 워크스페이스 멤버십, GitHub App for Slack 설치입니다. 최초 사용 시 GitHub 계정 연결과 **기본 리포지토리 설정**을 요구합니다. 스레드나 DM에서 `@GitHub`를 멘션해 자연어로 요청하며(예: `@GitHub Add "Hello World" to the README in octo-org/octo-repo on the develop branch`), 봇은 요약과 생성된 PR 링크로 응답합니다. Teams에서도 동일하게 `@GitHub` 멘션으로 버그 수정, 소규모 기능, 리팩터링, 로깅, 스캐폴딩 작업을 위임할 수 있습니다. 리포지토리 미지정 시 채널 기본값(`@GitHub settings`로 설정)을 쓰거나 선택을 요구하고, 브랜치는 기본 브랜치로 폴백합니다.

**권한 모델이 다른 도구들과 구분되는 핵심 지점입니다.** Copilot은 **자체 아이덴티티가 아니라 요청자의 연결된 GitHub 계정 권한으로 행동**합니다. PR 생성에는 write 권한이, 이슈 생성에는 해당 리포의 이슈 생성 권한이 이미 있어야 합니다. 즉 Claude Tag의 "에이전트 자체 계정" 모델과 Copilot의 "사용자 위임(impersonation)" 모델은 감사·귀속(attribution) 측면에서 상반됩니다.

**문서에 명시된 컨텍스트 한계이자 보안 고려사항.** GitHub 공식 문서는 *"Copilot cloud agent가 요청의 컨텍스트로 스레드 전체를 캡처하며, 이 컨텍스트는 PR에 저장된다"*고 경고하고, 공유 범위를 제한하려면 스레드 멘션 대신 DM을 사용하라고 권고합니다. 이는 **스레드 단위 컨텍스트 흡수가 곧 정보 유출 경로**임을 벤더가 직접 인정한 드문 사례로, 논문 인용 가치가 높습니다.

출처:
- https://docs.github.com/en/copilot/how-tos/use-copilot-agents/cloud-agent/integrate-cloud-agent-with-slack
- https://docs.github.com/en/copilot/concepts/tools/about-copilot-integrations
- https://github.blog/changelog/2026-03-30-create-issues-from-slack-with-copilot/
- https://github.com/orgs/community/discussions/177494

### 3.2 Devin (Cognition)

Settings > Connections > Slack에서 워크스페이스에 앱을 설치한 뒤, **조직의 모든 사용자가 개별적으로 계정을 연결**해야 합니다. 매핑은 Slack 이메일과 Devin 계정 이메일 일치로 이루어지며, 이 연결이 없으면 사용할 수 없습니다. 채널·스레드에서 `@Devin` 태그로 호출하고 첨부파일을 포함할 수 있으며, Devin은 in-thread로 진행 상황과 질문을 회신하면서 일반 chat 인터페이스처럼 왕복 대화가 가능합니다.

**세션 지속성 면에서 가장 앞서 있습니다.** 웹앱 세션과 Slack 스레드가 **양방향 동기화**되고, **아카이브된 세션의 스레드에 `@Devin`을 멘션하면 세션이 언아카이브되어 대화를 이어갈 수 있습니다.** 세션이 장시간 상호작용에 걸쳐 상태를 유지해 몇 시간 뒤에 돌아와도 하던 일을 기억합니다. `!fast`, `!lite`, `!ultra`, `!fusion`, `!swe`, `!normal`, `!new`, `!channel` 등의 bang 명령이 메시지 어디에서나 인식되고 중첩 가능하며, `!ask`/`!deep`/`mute`는 전체 세션을 시작하지 않고 동작을 제어합니다.

권한 면에서는 멘션·메시징·파일·히스토리·채널 관리·리액션·사용자 식별에 걸쳐 14개 이상의 Slack scope를 요구하고, channels/groups/DM의 메시지와 콘텐츠를 히스토리 권한으로 읽어 컨텍스트를 구성합니다. 사이드바 assistant는 유료 Slack 플랜이 필요하고 멘션·슬래시 명령은 무료 티어에서도 동작합니다.

출처:
- https://docs.devin.ai/integrations/slack
- https://slack.com/marketplace/A06A3TU8H39-devin
- https://cognition.com/blog/how-cognition-uses-devin-to-build-devin

### 3.3 Cursor (Background Agents in Slack)

Cursor 1.1에서 도입되었습니다. 관리자가 Dashboard → Integrations에서 Slack 앱을 인가하고, 기본 리포지토리 설정·usage-based pricing 활성화·프라이버시 설정 확인을 거칩니다. `@Cursor [프롬프트]`로 호출하면 **격리된 가상 머신(isolated VM)을 띄워 원격 개발 환경에서 리포와 작업하고 GitHub에 PR을 생성**합니다. 문서는 *"Cloud Agent는 호출 시 스레드 전체를 컨텍스트로 읽어 팀의 논의에 기반해 해결책을 이해하고 구현한다"*고 명시합니다.

리포/모델/브랜치/환경을 메시지와 최근 활동에서 자동 추론하며, 우선순위는 **메시지 내 명시적 언급 → 최근 활동 → 라우팅 규칙 → 채널 기본값 → 폴백 리포** 순입니다. 명시 지정 문법은 `@Cursor [repo=torvalds/linux] fix bug` 형태입니다. `@Cursor settings`(채널 기본값), `@Cursor agent [프롬프트]`(기존 스레드에서 새 에이전트 강제), `@Cursor list my agents`(실행 중 에이전트 목록) 명령을 제공하며, 컨텍스트 메뉴로 후속 지시·삭제·request ID 확인이 가능합니다. Slack 권한은 18개를 요구합니다.

출처:
- https://cursor.com/docs/integrations/slack
- https://cursor.com/changelog/1-1
- https://slack.com/marketplace/A08SKDT6QUW-cursor
- https://forum.cursor.com/t/cursor-is-now-available-in-slack/106350

---

## 4. 공통 아키텍처 패턴

다섯 도구를 관통하는 파이프라인은 다음과 같습니다.

| 단계 | 내용 | 벤더 간 차이 |
|---|---|---|
| 1 호출 | 채널/스레드에서 `@Agent` + 자연어 프롬프트 | 모두 동일. Devin/Cursor는 bang·서브커맨드 추가 |
| 2 컨텍스트 수집 | 스레드 히스토리 전체를 자동 흡수 | 모두 동일. Claude Tag만 채널 히스토리 기반 장기 메모리 추가 |
| 3 대상 결정 | 리포·브랜치·환경을 추론 또는 기본값 폴백 | Codex=최근 환경, Copilot=채널 기본 리포, Cursor=5단계 우선순위 |
| 4 실행 | 클라우드 샌드박스/격리 VM에서 리포 클론 후 작업 | Cursor는 "isolated VM" 명시, Codex는 "cloud task", Copilot은 "cloud agent" |
| 5 회신 | 스레드에 진행 상태·결과·PR 링크 포스팅 | Codex는 리액션 → 링크 → 결과의 3단 회신 |

주목할 구조적 특징은 **Slack이 실행 환경이 아니라 트리거이자 알림 표면(notification surface)**이라는 점입니다. 실제 상태와 산출물은 외부 시스템(Codex cloud, Cursor VM, GitHub PR)에 존재하고, 스레드에는 그에 대한 링크만 남습니다. 이 이원화가 아래 5·6절 한계의 근원입니다.

또 하나의 축은 **에이전트 아이덴티티 모델의 분기**입니다. Claude Tag는 에이전트에게 독립적인 계정·GitHub App·서비스 계정을 부여하는 반면(작업이 조직에 귀속), Copilot은 요청자의 GitHub 권한을 그대로 차용합니다(작업이 개인에 귀속). Devin은 Slack 이메일-계정 매핑으로 개인에 귀속시킵니다. 이는 감사 추적, 최소 권한 원칙, 책임 소재 측면에서 서로 다른 트레이드오프를 갖습니다.

출처:
- https://slack.com/blog/developers/coding-agents-in-slack
- https://www.tembo.io/blog/background-coding-agents
- https://benanderson.work/blog/async-coding-agents/

---

## 5. 컨텍스트 전달의 한계

**5.1 스레드 = 컨텍스트 윈도우의 경계.** Claude Tag 분석에서 지적되듯 "채널 간 지속 메모리가 없으며, 각 스레드가 그 자체로 하나의 컨텍스트 윈도우"입니다. 동일 히스토리가 같은 스레드 안에 있지 않는 한 다른 채널이나 이전 대화에서 논의한 내용을 기억하지 못합니다. 이는 개발 논의가 본질적으로 여러 채널·DM·이슈 트래커에 분산된다는 현실과 충돌합니다.

**5.2 컨텍스트 윈도우 물리 한계.** 매우 긴 스레드는 모델이 한 번에 처리 가능한 범위를 초과할 수 있습니다. 일반적인 Slack 스레드에서는 드물지만, 장기 인시던트 스레드처럼 수백 메시지가 누적되는 경우 문제가 됩니다.

**5.3 세션 지속성의 벤더별 편차.** Devin은 아카이브 세션 재개와 웹앱-Slack 양방향 동기화를 지원하지만, Codex는 각 멘션이 새 cloud chat을 생성하는 구조에 가깝습니다. 즉 **"세션 지속성 부재"는 카테고리 전반의 특성이라기보다 표준화되지 않은 축**이며, 논문에서는 이 비일관성 자체를 문제로 제기하는 편이 정확합니다.

**5.4 코드베이스 컨텍스트 제약.** 실행이 기본 브랜치 기준이고(Codex), 리포·브랜치가 채널 기본값에 의존하며(Copilot·Cursor), 사전에 environment를 구성해두어야만 동작합니다(Codex). 스레드에서 언급되지 않은 코드베이스 구조·설계 의도·과거 결정은 에이전트에게 전달되지 않으며, 대화 텍스트만으로는 리포지토리의 암묵적 맥락을 복원할 수 없습니다.

**5.5 비동기 실행에 따른 실시간 협업 불가.** 백그라운드 에이전트는 샌드박스에서 리포를 클론하고 변경하고 테스트를 돌리고 브랜치를 푸시하는 전 과정을 비동기로 수행합니다. 가치 제안 자체가 "IDE를 열지 않고 채팅에서 작업을 던져두는 것"이므로, **실행 중 실시간 조종(real-time steering)은 워크플로 설계에 내재되어 있지 않습니다.** 테스트 실패나 린트 오류를 피드백 루프로 되먹여 반복(iterative)하게 만드는 개선은 있었지만, 이는 자동화된 자기 교정이지 인간의 개입 지점이 아닙니다.

출처:
- https://www.mindstudio.ai/blog/claude-tag-slack-enterprise-agent
- https://www.mindstudio.ai/blog/what-is-claude-tag-anthropic-slack-agent
- https://www.datacamp.com/blog/claude-tag
- https://developertimesai.com/p/background-agents-and-autonomy
- https://www.tembo.io/blog/background-coding-agents

---

## 6. 다중 사용자 협업 관점의 한계

**6.1 공유 인스턴스와 조종권 충돌.** Claude Tag는 채널당 하나의 Claude를 전원이 공유하며 "누구나 조종하거나 이어받을 수 있는" 것을 장점으로 내세웁니다. 그러나 이 설계는 동시에 **여러 사용자가 같은 스레드에서 상충하는 지시를 내릴 때의 조정 메커니즘이 정의되어 있지 않다**는 문제를 낳습니다. 조종권 획득(turn-taking), 지시 우선순위, 충돌 해결에 대한 명시적 프로토콜이 공개 문서에 없습니다.

**6.2 권한 귀속 모델의 불일치.** 3절에서 정리했듯 Copilot은 요청자 권한으로 행동하고, Claude Tag는 에이전트 자체 아이덴티티로 행동합니다. 공유 스레드에서 A가 시작한 작업을 B가 이어받을 때 **어느 사용자의 권한으로 실행되는지, PR의 책임 주체가 누구인지**가 모델에 따라 달라지며, Claude Tag의 조직 귀속 방식에서는 개별 기여자 추적이 흐려집니다.

**6.3 강제된 개별 온보딩.** Devin은 조직의 모든 사용자가 개별적으로 Slack-Devin 계정 연결을 완료해야 사용 가능합니다. 이는 "채널에 있는 누구나 즉시 참여"라는 메신저의 개방성과 충돌하는 마찰 지점입니다.

**6.4 스레드 컨텍스트 = 정보 유출 및 인젝션 표면.** GitHub 문서가 스레드 전체가 캡처되어 PR에 저장된다고 경고하는 것이 대표 사례입니다. 더 나아가, 다중 사용자 채널에서는 **간접 프롬프트 인젝션(indirect prompt injection)**이 실증되었습니다. PromptArmor가 보고하고 MITRE ATLAS에 케이스 스터디(AML.CS0035)로 등재된 Slack AI 취약점은, 워크스페이스 내 공격자가 공개 채널에 심어둔 지시를 통해 **자신이 속하지 않은 프라이빗 채널의 데이터를 유출**할 수 있음을 보였습니다. Slack은 이후 패치를 배포했고 현재는 컨텍스트 엔지니어링과 실시간 필터로 인젝션·탈옥 시도를 완화한다고 밝히고 있습니다. 코딩 에이전트는 리포지토리 쓰기 권한을 갖기 때문에 이 표면의 영향 반경이 더 큽니다.

**6.5 승인 흐름의 실효성 저하.** 보안 분석은 **승인 피로(approval fatigue)** — 개발자가 세션당 수십 개의 프롬프트를 읽지 않고 승인하는 "엔터, 엔터, 엔터" 반사 — 로 인해 주입된 액션이 표준 권한 흐름을 통과할 수 있다고 지적합니다. 채널 공유 에이전트에서는 승인 요청이 누구에게 가야 하는지조차 불명확해 이 문제가 증폭됩니다.

**6.6 관리 통제의 조악한 입도.** 현재 제공되는 통제 수단은 채널 단위 툴 접근 제한, 지출 한도, 답변 게시 여부 토글, 역할 기반 접근(Enterprise 한정) 수준입니다. 개별 작업 단위의 사전 승인, 변경 범위 제한, 사용자별 차등 권한 같은 세밀한 거버넌스는 부족하며, Claude Tag의 경우 축적된 메모리를 조직이 주기적으로 검토·삭제해야 한다는 운영 부담이 추가로 지적됩니다.

출처:
- https://docs.github.com/en/copilot/how-tos/use-copilot-agents/cloud-agent/integrate-cloud-agent-with-slack
- https://www.promptarmor.com/resources/data-exfiltration-from-slack-ai-via-indirect-prompt-injection
- https://www.startupdefense.io/mitre-atlas-case-studies/aml-cs0035-data-exfiltration-from-slack-ai-via-indirect-prompt-injection
- https://www.theregister.com/2024/08/21/slack_ai_prompt_injection/
- https://slack.com/blog/transformation/securing-the-agentic-enterprise
- https://www.mitiga.io/blog/007-license-to-skill-p-2-slack-compromise-through-claude-code
- https://www.truefoundry.com/blog/claude-code-prompt-injection

---

## 7. 논문 작성 시 유의사항

인용 신뢰도가 갈리므로 구분해서 쓰시길 권합니다. **1차 출처(벤더 공식 문서·발표)**는 anthropic.com, support.claude.com, learn.chatgpt.com, docs.github.com, docs.devin.ai, cursor.com/docs이며 기능 서술의 근거로 안전합니다. **2차 출처(분석 블로그)**인 MindStudio, DataCamp, usecarly 등은 한계 서술에 유용하지만 벤더 공식 입장이 아니므로 "보고된(reported)" 수준으로 표현하는 편이 안전합니다. 보안 관련은 PromptArmor 원 리포트와 MITRE ATLAS 등재(AML.CS0035)가 학술 인용에 가장 적합합니다.

특히 **5.3(세션 지속성)** 항목은 주의가 필요합니다. Devin은 아카이브 세션 재개와 양방향 동기화를 명시적으로 지원하므로 "메신저 기반 도구는 세션 지속성이 없다"는 일반화는 반례가 존재합니다. "세션 지속성이 벤더별로 표준화되지 않았고, 지속되더라도 스레드 경계를 넘지 못한다"는 형태로 서술하는 것이 방어 가능합니다. 마찬가지로 "일회성(one-shot) 실행"이라는 프레이밍도 현대 백그라운드 에이전트가 테스트 실패 피드백 루프로 반복 교정한다는 점 때문에 부정확하며, **"자동 반복은 하지만 인간의 실시간 개입 지점이 없다"**가 정확한 주장입니다.
