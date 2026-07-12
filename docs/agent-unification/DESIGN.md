# Agent 통합 — delegate + teammate → `agent` (v5.0.0)

> 상태: **설계 검토 대기** (2026-07-12 사용자와 공동 설계)
> living doc — 각 단계 완료 시 진행 로그 갱신. 선행 설계: docs/teammate/DESIGN.md

## 1. 배경과 목표

delegate(일회성 파견)와 teammate(상주 팀원)를 나눠 출하한 뒤 실사용에서
두 개념의 겹침이 혼동 비용으로 확인됐다: 디렉토리 2개(agents/·teammates/),
파라미터 2벌(agent·role), 광고 섹션 2개, "다시 시작"을 spawn 으로 오인하는
모델 행동 등. **하나의 `agent` 개념으로 통합한다.**

이 문서는 teammate 설계의 결정 3건을 실사용 근거로 재개봉한다:
- D5(delegate 공존) → 폐기: 통합. 당시 근거였던 "검증된 prior 보존"은
  unknown-tool 1턴 복구 실측으로 감당 가능 판정.
- D6(이름 teammate) → 폐기: 도구명 `agent`. 당시 충돌 사유였던 "agent
  도구의 agent 파라미터"는 파라미터명을 `profile` 로 바꿔 해소.
- D11(b)(teammates/ 전용 디렉토리) → 폐기: `.agent-cli/agents/` 단일.

## 2. 확정 결정 로그 (2026-07-12 사용자 확정)

| # | 결정 | 근거/논의 |
|---|------|-----------|
| U1 | 도구명 **`agent`** 단일, 버전 **5.0.0** | 개념 대개편의 정직한 선언. 파라미터 `profile` 로 D6 충돌 해소 |
| U2 | **`mode:"run"` 은 main·서브루프 모두 지원** | 일회성+fan-out 프리미티브. 서브루프는 mailbox/wake 가 없어 blocking run 이 유일한 fan-out 형태(구조 제약). main 에서도 "즉시 결과가 필요한 단발"에 유용 — skill 과의 구분: skill=이름 붙은 재사용 절차(파일), run=즉석 임의 태스크 병렬 |
| U3 | **delegate 즉시 하드컷** | 별칭/은닉 매핑 없음. 모델이 emit 하면 기존 unknown-tool 복구 개입("없는 도구 + 가용 목록")이 nudge — 1턴 회복 실측 근거 |
| U4 | **instant-agent**: `profile`(파일) + `instructions`(인라인) 병용 | 합성 = 파일 본문 → 인라인 순(recency 우선). manifest 에 합성본+원본 필드 저장 → resume/부활 시 동일 system prompt (P3 기계 재사용) |
| U5 | DIRECTIVE 스코프: `## @main` / `## @agents` 마커, **무마커 = common** | 현행(전 루프 동일 적용)이 common 과 동치라 기존 파일 무수정 호환 |
| U6 | **wait 모드 제거** | idle 자동 재기동(P4/P5)이 완전 대체. 조사 중 결함 확인: wait_reply 가 stop_event 미감시 → Stop 이 최대 300s 불응 + 사용자 주입 지연. 제거로 자연 해소 |
| U7 | `context: "none"|"fork"` 파라미터 승계 | 이미 존재(P1). 광고 문구 강화 + "스폰 시점 스냅샷·큰 컨텍스트 fork 비용" 주의 명시 |

## 3. 최종 표면

### 3.1 도구 스키마 (모드 × 루프 가용성)

```json
{"action":"agent","mode":"run",     "task":"...", "profile":"...", "instructions":"...", "tools":[...], "context":"none|fork"}
{"action":"agent","mode":"spawn",   "profile":"...", "instructions":"...", "name":"...", "task":"...", "tools":[...], "context":"none|fork"}
{"action":"agent","mode":"request", "key":"agt-...", "message":"..."}
{"action":"agent","mode":"status",  "key":"...(생략=전체)"}
{"action":"agent","mode":"resume",  "key":"agt-...", "task":"...(옵션)"}
{"action":"agent","mode":"kill",    "key":"agt-..."}
```

| mode | main | 서브루프 | 비고 |
|------|------|---------|------|
| run | ✓ | ✓ (depth<max) | 일회성·blocking·**한 턴 다중 run op = 병렬 fan-out** (구 delegate 의미·기계 그대로) |
| spawn/request/status/resume/kill | ✓ | ✗ | 상주 — 레지스트리(main 소유) 필요. 서브루프는 스키마에서 모드 자체가 안 보이게 **동적 enum**(아래 3.2) |
| ~~wait~~ | 제거 | 제거 | U6 |

- `run` 의 파라미터는 구 delegate `{task, context, tools, agent}` 에서
  `agent`→`profile` 개명 + `instructions` 추가.
- validate(C7): mode enum + 조건부 필수(run→task, request→key·message,
  resume/kill→key) + 루프별 가용 모드 검사(서브루프에서 spawn 등 →
  "persistent modes are main-session only" 에러).

### 3.2 서브루프 노출 방식

현재 "레지스트리 없으면 teammate 도구 전체 strip" 을 **"모드 축소"** 로
바꾼다: 도구는 항상 존재하되, 시스템 프롬프트의 도구 설명·mode enum 이
루프에 따라 달라진다.

- main: 전체 모드 + Live Agents 광고.
- 서브루프: `mode:"run"` 만 문서화된 축소 스키마 (실질 = 지금의 delegate
  자리). 중앙 validate 는 전체 enum 을 알되, 디스패치가 레지스트리 부재
  시 상주 모드를 거부.
- 구현: `TOOLS["agent"]` 는 단일 인스턴스 유지, **시스템 프롬프트 렌더만
  분기** (`build_tool_descriptions` 에 registry 유무 전달 — 스키마 사본
  2벌을 만들지 않는다. KV 캐시: main/서브는 원래 프롬프트가 다르므로
  추가 비용 없음).

### 3.3 프로파일 (단일 디렉토리)

```
.agent-cli/agents/{name}.md   (프로젝트) → ~/.agent-cli/agents/ (전역)
                              → agent_cli/agents/builtin/ (내장)
```

- 내장 통합: explorer(기존) + researcher·coder·code-reviewer(teammates/
  builtin 에서 이동). frontmatter 명세 병합: `description`(발견 표면),
  `allowed-tools`, `model`, `hooks`, `disable-model-invocation`,
  `auto-spawn`(상주 전용 — run 에는 무의미, 무시).
- **레거시 폴백 없음** (사용자 결정 — 하위호환 전면 포기):
  `teammates/` 검색 경로는 코드에서 완전 제거. 기존 사용자 파일은
  `mv .agent-cli/teammates/* .agent-cli/agents/` 로 이동 (릴리스 노트
  안내).
- **내장 프로파일**: 통합 작업에서는 기존 내장(explorer + researcher/
  coder/code-reviewer)의 **기계적 이동·포맷 정합만** 수행. 내용·구성
  개편은 **별도 패치에서 사용자와 협업 제작** (사용자 결정).
- `/create-agent` 스킬이 상주·일회성 겸용 프로파일 작성을 안내하도록
  통합 개정, `/create-teammate` 는 제거.

### 3.4 instant-agent 합성 규칙 (U4)

```
role_prompt = profile 파일 본문                (profile 지정 시)
            + "\n\n## Additional instructions\n" + instructions   (지정 시)
```

- 순서 고정: 파일(일반) → 인라인(구체) — recency 로 인라인이 우선 효과.
- frontmatter 는 파일 것만 적용. `instructions` 는 순수 역할 텍스트.
- 둘 다 생략 = 익명 generalist (현행 유지).
- manifest(§3.6): `role_prompt`(합성본, 부활/resume 의 진실) +
  `profile`/`instructions`(원본, 인스펙터 가시성) 저장. 파일이 나중에
  바뀌어도 살아있는 개체는 태어난 프로파일 유지 (P3 semantics 승계).
- `mode:"run"` 도 동일 파라미터 수용 (일회성 인라인 역할 — delegate 의
  오랜 공백 해소).

### 3.5 광고/명령 표면

- 시스템 프롬프트: `Agents`(구 Available Agents)·`Teammate Roles`·
  `Live Teammates` 3섹션 → **2섹션**으로:
  - `## Agent Profiles` — 프로파일 카탈로그 (run/spawn 공용, 예시 2개:
    run fan-out + spawn 상주).
  - `## Live Agents` — 상주 멤버십 (기존 Live Teammates 승계 — 즉시
    인스펙터 반영·멤버십 플래그 포함).
- `@` 명령: `@agents` = **프로파일 카탈로그 + live roster 통합 표시**,
  `@agt-<key> [메시지]` 유지, `@teammates` 제거(하드컷).
  **`@<profile>[-run|-spawn] <task>`** (사용자 결정, 2026-07-12 개정) —
  접미사로 모드 지정 (기본 run=일회성, `-spawn`=상주+초기 task). 공백
  구문의 "task 첫 단어가 run/spawn 이면 소실" 모호성을 원천 제거.
  파싱 규칙: **전체 토큰이 실존 프로파일이면 그것 우선, 아니면
  `-run`/`-spawn` 접미사 분리** — 하이픈 포함 프로파일명
  (`@code-reviewer-spawn` → code-reviewer+spawn)과 극단 케이스
  (`foo-run` 이라는 프로파일)까지 결정적으로 해소.
- 웹 🤝 드로어: 이름만 "Agents" 로. 기능 무변경.

### 3.6 내부 개명 맵 (5.0.0 일괄)

| 현재 | 통합 후 |
|------|---------|
| `TeammateTool` / `tool_teammate` | `AgentTool` / `tool_agent` (+run 디스패치) |
| `TeammateRegistry` / `Teammate` | `AgentRegistry` / `AgentInstance` |
| `subagent/teammate.py` | `subagent/agents_live.py` (규모상 분할 검토) |
| `subagent/roles.py` (teammates 로더) | delegate 의 `agents.py` 로더와 **병합** → `subagent/profiles.py` |
| `teammates.json` (state v1) | `agents.json` (AGENTS_STATE_VERSION=1 로 새 출발) — **v1 레거시 읽기 없음** (사용자 결정: 코드 청결 우선) |
| 세션 `teammates/<key>/` | `agents/<key>/` — 구 dir 읽기 코드 없음 |
| SSE `teammate_msg`/`teammate_roster` | `agent_msg`/`agent_roster` (프런트 동시 개정) |
| 렌더러 `begin/end_teammate_work`·`teammate_*` | `begin/end_agent_work`·`agent_*` (base no-op 포함) |
| `tool_delegate`/`_run_single`/`_run_parallel` | run 모드의 실행 경로로 흡수 (`tools/delegate/` 패키지 해체 → subagent/ 로) |
| env `AGENT_CLI_MAX_TEAMMATES` | `AGENT_CLI_MAX_AGENTS` (폴백 없음) |

- 하드컷 대상(코드 제거): `DelegateTool`·delegate 스키마·`@teammates`·
  `wait` 모드·`wait_reply`·`/create-teammate`.
- 보존 대상(이름만 변경): 병렬 배칭(`parallel_safe` — run 모드 전용,
  **mode-aware 배칭**: 한 턴의 연속 agent op 이 전부 run 일 때만 병렬,
  상주 모드가 섞이면 순차)·MailWaker·InputQueue·인스펙터 스코프·
  회신 배달 레코드(`tool:"agent"` 로 변경 — `source` 필드는 유지,
  `is_format_intervention` 비오인 계약 재고정).

### 3.7 DIRECTIVE 스코프 (U5)

```markdown
(무마커 본문)          → common: main + 모든 서브에이전트  ← 현행과 동치
## @main               → main LLM 에만
## @agents             → 서브에이전트(run·상주 공통)에만
```

- 파싱: `## @` 접두 헤딩만 스코프 마커. 마커 헤딩부터 다음 마커/EOF 까지가
  그 스코프. 그 외 모든 헤딩(웹 3축 에디터의 관리 섹션 포함)은 본문으로
  취급 — **3축 에디터와 충돌 없음** (에디터는 `@` 접두 헤딩을 만들지
  않는 규칙을 명세에 추가).
- 적용: `_load_directives(scope)` — `build_system_prompt_sections` 가
  main(registry 유무 아님 — **depth 0 여부**)/서브를 판별해 common+해당
  스코프만 조립. 프로젝트/전역 파일 모두 동일 규칙.

## 4. 하위호환 방침 (사용자 확정: 전면 포기 — 코드 청결 우선)

5.0.0 은 메이저 — **레거시 읽기/폴백/별칭을 일절 두지 않는다.** 유일하게
자연 호환되는 것은 history 레코드 렌더(이름 불문)뿐이며, 이것도 보장
테스트 대상이 아니다.

| 항목 | 5.0.0 동작 |
|---|---|
| 구 세션 resume | main 대화는 이어짐(자연 호환) — **상주 에이전트는 재생성 안 됨** (`teammates.json` 읽는 코드 부재, 조용히 "에이전트 없음") |
| 모델의 delegate/teammate emission | unknown-tool 복구가 nudge (U3) |
| 사용자 프로파일 (`.agent-cli/teammates/*.md`) | 미탐색 — 릴리스 노트로 `mv → agents/` 안내 |
| DIRECTIVE.md 기존 파일 | 동작 동일 (무마커=common 이 현행과 동치 — 호환을 위한 코드가 아니라 의미론의 자연 일치) |
| env/SSE/렌더러 API/`@teammates` | 전부 신명명, 폴백 없음 |

## 5. 단계 계획 (각 단계 = 브랜치→게이트→문서 동기→wheel 릴리스)

| 단계 | 내용 | 버전 | 게이트 |
|---|---|---|---|
| **U-A** | wait 제거 + instant-agent(`instructions`) — 비파괴 선행 | 4.63.0 | wait 흔적 0(스키마·광고·문구·stop_event 결함 소멸)·instructions 합성/영속/부활 계약. 기존 teammate 테스트(wait 제외) 무수정 통과 |
| **U-B** | **통합 본체 = 5.0.0** (breaking 전부 한 릴리스, 내부 PR 분할): ①프로파일 통합(profiles.py 병합 로더·agents/ 단일·내장 기계적 이동) → ②개명 맵 전체(§3.6) → ③AgentTool(run 흡수·mode-aware 배칭·모드 축소 노출) → ④하드컷(delegate·@teammates·teammates.json/dir·구 env) → ⑤광고 2섹션·`@<profile> [run|spawn]`·SSE/프런트 개정 | **5.0.0** | 전 회귀 + 실기동 e2e(run fan-out·spawn 상주·instant·fork·@구문) + delegate emission 복구 확인. 신 세션 resume e2e(5.0 세션의 agents.json 재생성) |
| **U-C** | DIRECTIVE 스코프 | 5.1.0 | 마커 파싱·스코프별 조립·무마커=common 동치·3축 에디터 상호작용 |
| (별도) | 내장 프로파일 내용 개편 — 사용자 협업 패치 | 5.x | — |

## 6. 리스크와 완화

1. **모델 prior (delegate/teammate emission)**: 하드컷 후 unknown-tool
   개입 발생률을 turns.jsonl 로 관찰. 시스템 프롬프트의 도구 설명에
   "(formerly delegate)" 한 줄을 넣을지는 실측 후 결정 (선제 삽입은
   이름 오염이라 보류).
2. **mode-aware 병렬 배칭**: 기존 delegate 배칭 테스트를 run 모드로
   이관 + "run·spawn 혼합 턴은 순차" 계약 신설.
3. **개명 범위**: 테스트 monkeypatch 문자열이 광범위 — C1 패키지化 때
   확립한 F821→F401 2단 검증 절차 재사용.
4. **배달 레코드 tool 명 변경**(`teammate`→`agent`):
   `is_format_intervention` 비오인 계약을 `tool:"agent"` 기준으로 재고정
   (구 레코드는 렌더 자연 호환이나 무보장 — §4 방침).

## 7. 결정 포인트 — 전원 해소 (2026-07-12 사용자 확정)

1. 폴백/레거시 읽기 **전면 없음** — 하위호환보다 코드 청결 (§4 방침으로
   승격). 구 세션의 상주 에이전트 미재생성을 명시적으로 수용.
2. `@<profile>[-run|-spawn] <task>` 구문 채택 (기본 run — 접미사 방식,
   2026-07-12 공백 구문에서 개정).
3. 세션 dir `agents/<key>/` + `agents.json` — 구 이름 읽기 코드 없음.
4. 내장 프로파일 내용 개편은 별도 협업 패치.

## 진행 로그

- 2026-07-12: 공동 설계 확정 (U1~U7 — C1 은 "run 양쪽 지원"으로,
  C2 는 하드컷으로 사용자 최종 결정). 문서 작성.
- 2026-07-12: 검토 반영 — **하위호환 전면 포기 확정**(레거시 읽기/폴백/
  별칭 0, 구 세션 상주 에이전트 미재생성 수용), `@<profile> [run|spawn]`
  구문, agents/ 디스크 명명 + 구 이름 제거, 내장 개편은 별도 협업 패치.
  단계 재편: U-A(4.63.0 비파괴) → U-B(5.0.0 breaking 일괄) →
  U-C(5.1.0 DIRECTIVE). **U-A 착수 승인.**
- 2026-07-12: **U-A 완료 (v4.63.0)** — wait 모드 제거(스키마·디스패치·
  wait_reply·유도 문구 전부, stop_event 결함 자연 해소; 질문 왕복은 턴
  경계 drain 으로 성립함을 테스트로 재고정) + instant-agent
  `instructions`(compose_role_prompt 합성, Teammate/manifest 필드,
  resume·부활 동일 정체성, 러너 agent_role 수신까지 spy 검증). 테스트
  재편 -4(wait 계약)/+7(instant·wait-거부·drain 왕복), 전체 3032.
- 2026-07-12: **U-B 완료 (5.0.0)** — breaking 일괄:
  - PR-1 프로파일 통합: `subagent/profiles.py` 병합 로더
    (`load_profile`/`available_profiles`/`_profile_loader`), 검색 경로
    `.agent-cli/agents/` 단일(teammates/ 폴백 0), 내장 기계적 이동.
  - PR-2 개명 맵(§3.6): teammate.py→`agents_live.py`,
    Teammate(Registry)→AgentInstance/AgentRegistry, SSE
    `agent_msg`/`agent_roster`, 렌더러 `begin/end_agent_work`·`agent_*`,
    env `AGENT_CLI_MAX_AGENTS`, 세션 `agents/<key>/`+`agents.json`
    (구 이름 읽기 없음).
  - PR-3 AgentTool: 도구명 `agent`, run 흡수(브리지 `_invoke_agent`
    3-way: tasks 배치/run 단건→oneshot 엔진, 상주→tool_agent),
    mode-aware 배칭(`parallel_batchable`=run 만), 모드 축소 노출
    (SUBLOOP_DESCRIPTION + 디스패치 거부 — no-registry 도구 strip 제거),
    run 은 main+서브루프 양쪽. tools/delegate/ 해체 →
    `subagent/oneshot.py`+`report.py`(엔진 함수명 보존).
  - PR-4 하드컷: DelegateTool·delegate 스키마·`@teammates`·
    `/create-teammate`·`_invoke_delegate`(죽은 경로)·dispatch delegate
    분기 제거. **run 훅은 `_invoke_agent` 로 이식**(PR-4 에서 소실됐던
    OnAgentStart/End — 테스트가 아닌 잔존물 청소 중 발견·복구).
  - PR-5 표면 개정: 광고 2섹션(`## Agent Profiles` — run/spawn 예시,
    서브루프에도 카탈로그 노출 / `## Live Agents` — registry 게이트),
    `@<profile>[-run|-spawn]` 접미사 구문(`_parse_at_profile` — 실존
    프로파일 우선)+`@agents` 카탈로그+roster 통합+`@<profile>-spawn`
    (`_try_dispatch_agent_command` 흡수), DispatchOutput
    `agent_dispatch_result`/`list_agents(catalog, live)`,
    `/create-agent` 개정(run/spawn 겸용), 프런트 라벨(🤝 Agents,
    roster 필드 `profile`, `agent-btn`), manifest/roster/reply 페이로드
    키 `role`→`profile`, spawn kwarg `profile=`.
  - 후속 사용자 결정 3건(2026-07-12): `--agent-timeout`(플래그 개명),
    훅 이벤트 `OnAgentStart`/`OnAgentEnd`, run dir `run_{name}_{hash}_{ts}/`.
  - agent-board 영향 검토: **무변경** — spawn argv(안정 플래그만)·경로
    무관 프록시·status.json/web.json/history.jsonl 계약 전부 불변.
  - 회귀 게이트: 전체 3043 passed, ruff clean. README·ARCHITECTURE
    전면 동기. (잔여: 실기동 e2e — run fan-out·spawn·instant·fork·@구문.)
- 2026-07-12: **U-C 완료 (5.1.0)** — DIRECTIVE 스코프:
  - `split_directive_scopes` (system_prompt.py): `## @main`/`## @agents`
    라인 마커 분할 — 블록=다음 `## @` 마커/EOF(일반 `##` 헤딩 무중단,
    스코프 안 다중 섹션 허용), 마커 라인 렌더 제거, **무마커=본문
    그대로 common(5.0 바이트 동일 — KV 보존)**, 반복 마커는 누적.
  - `_load_directives(audience)` + 조립 게이트 `depth==0→main /
    else→agents` (run·spawn·skill 전 서브루프 = agents; 사용자 부재로
    추천안 채택 — 판정 단순성).
  - 3축 에디터 상호작용: learned append 를 첫 스코프 마커 앞(common)에
    삽입(`_append_before_scope_markers`) — 세션 교훈 상시 공통(추천안);
    페르소나 prepend=자연 common; task 프리셋은 스코프 블록 포함 교체.
  - 내장 프로파일 협업 패치 동승: Linux 커널 4종
    `kernel-coder`(구현 — checkpatch/goto-cleanup/락 컨텍스트/BUG_ON 금지,
    Files touched 계약) / `kernel-kunit`(kunit_test_suite·ops-table
    페이크·.kunitconfig·kunit.py 실행 검증) / `kernel-analyzer`(읽기 전용
    — 콜패스·컨텍스트·수명, file:line) / `kernel-reviewer`(읽기 전용 —
    race/atomic-sleep/누수/UAF, 심각도+시나리오+ACCEPT/REJECT). 기존
    범용 4종 유지(추천안).
