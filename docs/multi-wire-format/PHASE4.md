# 멀티 wire-format — Phase 4: md_array → json_fc (리네임 + 리셰이프) (DESIGN)

> 상태: **완료 — 게이트 통과·전환 출하 v6.0.0** (2026-07-17)
> 게이트 결과: A/B 140run — completed 100%=100%, pf 0=0, rec 0.00 vs 0.03
> (35B 산문-선답 2건, 무해). 신 ≥ 구 확인 후 md_array 제거·DEFAULT 전환.
> 포팅 중 파서 보강 2건: actionless-op 보존(위치 신호)·깨진 op JSON stage 0.
> 결정: D7=ⓑ bare 배열 (래퍼 기각) · D8=alias 없이 즉시 제거 → **MAJOR v6.0.0**
> 선행: [DESIGN.md](DESIGN.md) P1~P3, [PHASE2.md](PHASE2.md) xml_fc·bakeoff

## 1. 동기

- **이름 통일**: `md_array` 를 `xml_fc` 와 짝이 되는 `json_fc` 로 — 단,
  마크다운 envelope 인 채로는 이름이 거짓이므로 리셰이프와 한 몸.
- **셰이프 통일 (D4 동형)**: `## Thought`/`## Action` 헤더 제거 →
  "산문 thought + 구조 블록" — xml_fc 와 같은 문법 모델. 헤더-runaway
  실패 클래스(`## Thought` 반복 등)가 shape 차원에서 소멸.

## 2. Wire shape (신)

```
auth 와 session 을 같이 본다 — 서로 독립이라 한 턴에.

[{"action": "read_file", "path": "src/auth.py"},
 {"action": "read_file", "path": "src/session.py"}]
```

- **D7 = bare 배열**: `{"tool_call": [...]}` 래퍼 기각 — ① 새 중첩
  레이어는 배치-중첩이 27B 를 90% 깨뜨린 실측 전례와 충돌, ② bare 배열
  body 는 현행 md_array body 그대로라 Phase-2 95.2%·실전 0.7% 검증분과
  **JSON 수리 기계 전량을 무변경 승계**.
- 종료 = 명시적 `complete` op (산문-only 종결은 md_array 초기에 시도 후
  false-terminate 로 철회된 전례 — 유지. 관련 보류 제안은 memory
  `project_noaction_complete_pending`).

## 3. 파서 (json_fc.py — md_array 승계 + 재배선)

1. **캐노니컬 (stage 1/2)**: `_op_anchor` 가 고른 op span 앞 산문 =
   thought(sanitize), 그 뒤를 `_extract_op_json`(승계 수리 기계 그대로 —
   미닫힘 `]`·anon-unwrap·다중배열 병합·escape·quote 수리 전부).
   `any("action")` 가드로 산문 속 `[1,2,3]` 오인 차단.
   > **v7.27.1 수리**: 이 가드가 턴 파서에만 구현돼 있었고 **span 선택**에는
   > 없었다 — 앵커가 "본문 첫 `[`/`{`" 였으므로 산문이 코드를 인용하면
   > (`board[i][j]`·`- [ ]`·`{ passive: false }`·`[문서](url)`) 그 괄호를 op 으로
   > 집고 뒤의 진짜 배열을 못 봤다(stage 0, 또는 산문 조각을 op 으로 채택해
   > `complete` 유실 + thought 잘림). 이제 `_op_anchor` 가 **op 서명 계층**으로
   > 후보를 걸러(1=`action` 보유, 2=action 흘린 bare dict, 0=탈락) 최선 계층의
   > **첫** span 을 앵커로 쓴다. ★방향(뒤에서부터 읽기)이 아니라 계층이 해법 —
   > 뒤에서 읽으면 "배열 뒤 산문"·"배열 재개 병합"·"산문 사이 배열 보수적
   > 처리" 세 계약이 동시에 깨진다(실측 후 기각). 파손 JSON(자격 후보 0)은
   > 첫 `[{`/`{"action"` 앵커로 수리 경로 진입 — 산문 괄호 오염 없이.
2. **legacy 관용 (stage 2)**: 구 md_array 헤더 emission — 전환기 모델
   습관 + foreign 누출 실측 shape(PHASE2 §8, 0-op 의 17%). 성공해도
   stage 2(drift) 로 계수, prior 는 캐노니컬로 재렌더(B→C 자기 교정).
3. thought-only = 0-op 넛지 / blank = stage 0 (기존 의미 유지).
4. `is_degenerate` = legacy 헤더-반복 검출 유지 (구 프라이어 누출 대비);
   캐노니컬 shape 러너웨이는 실측 후 추가.

## 4. 호환성 — D8: alias 없음 = MAJOR (v6.0.0)

| 영속 지점 | 영향 | 마이그레이션 |
|---|---|---|
| 세션 메타 `response_format: "md_array"` | resume 시 `get("md_array")` KeyError → **부트 fail-fast** | `--response-format json_fc` 명시 resume (레코드는 포맷-불가지라 B→C 재렌더로 일관 전환) |
| models.json `"wire_format": "md_array"` | 부트/spawn fail-fast | 바인딩 수정 (board 드롭다운 / 손편집 / CLI 재등록) |
| `DEFAULT_WIRE_FORMAT` | `"json_fc"` 로 전환 | — |

사용자 결정: alias 유지보다 즉시 정리 (코드베이스 단일 이름). 에러
메시지가 등록 포맷 목록을 제시하므로 마이그레이션은 자기-안내적.

## 5. 게이트 — bakeoff A/B

기본 포맷의 프롬프트 변경이므로 실측 필수 (교훈: sanity≠실전).
md_array(구) vs json_fc(신) — 27B/35B × 7태스크 × 5회. **신 ≥ 구**
(completed·pf·rec)일 때만 md_array 제거+DEFAULT 전환. 미달 시 결정 회귀
(헤더 유지 = 리네임 보류).

## 6. 전환 시퀀스

1. json_fc 를 md_array **옆에** 등록 (A/B 측정 가능 상태) ← 완료
2. bakeoff 게이트 ← 진행 중
3. 통과 시: md_array.py·테스트 제거 + 수리-기계 테스트를 json_fc 로 포팅
   + `DEFAULT_WIRE_FORMAT="json_fc"` + 전 참조 리네임 (react/xml_fc
   주석·docs·bench 기본값·foreign-rescue 테스트 픽스처) + v6.0.0


## 7. Phase 5 — react 제거 (v7.0.0 MAJOR, 2026-07-17)

2체제(json_fc + xml_fc) 확정 — 사용자 결정. react 존치 명분 소진:
fallback 사이클 경과·bakeoff 우위 전무·json_mode 유일 소비자.
- **근거 확인**: react-classic 셰이프 누출은 전 측정(0-op 캡처 30 +
  bakeoff 전체 출력)에서 **0건** → 관용 라이더는 가설로 기각 (발생 시
  A5 SCHEMA_MISMATCH 로 자연 표면화 — 실측 2건 나오면 그때).
- **json_mode 기계 동반 제거**: openai provider 분기·`provider_call_kwargs`
  기본 `{}`·두 포맷의 무의미 override·structured/strict **프로브+
  capabilities 필드** (소비자 0, 프로브는 감지 시간 낭비였음). 구
  models.json 엔트리의 잔존 키는 로더가 무시 (무해). agent-board admin
  다이얼로그의 structured/strict 체크박스는 후속 정리 대상 (기록하는
  필드를 agent-cli 가 무시할 뿐 — 무해).
- 테스트 마이그레이션: conftest react-핀 제거, react JSON emission 전면
  재저작 (AST 기반 변환 60+ 사이트 — regex 는 중첩에서 실패해 폐기),
  react 소유 스위트(parser/repair) 삭제, NO_JSON 라벨 경계 갱신
  (json_fc 에선 순수 산문 = thought-only NO_ACTION 이므로 NO_JSON 은
  "action" 흔적 있는 깨진 JSON 으로만).
- BREAKING: `--response-format react`·react 세션 resume·react 바인딩 →
  fail-fast. 마이그레이션은 json_fc 재지정.
