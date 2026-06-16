# History Schema Enrich + read_context JSON Query — DESIGN

history.jsonl 레코드에 **검색 키를 가산**하고, read_context 를 **구조화 JSON
쿼리**로 바꿔 "정말 필요한 정보만" 효과적으로 회상한다.

## 결정 (사용자 확정)
- **단일 파일** — 별도 인덱스 없이 history.jsonl 을 그대로 enrich.
- **하위호환 무시** — 구 세션 마이그레이션 안 함(키 없으면 쿼리에서 자연 제외).
- **JSON 쿼리부터** — BM25/FTS5 는 다음 단계(미포함).

## 스키마 (가산 enrich)
round-trip 필드(`role`/`thought`/`ops`/`content`/`tool`/`success`)는 **그대로**
두고, 검색 키만 추가:

| 키 | 의미 | 출처 |
|---|---|---|
| `kind` | query·action·observation·final·raw·system | `_classify_record`(shape) |
| `tools` | 관여 툴명 리스트 | shape(observation=tool, action=ops 액션) |
| `text` | 평탄 검색면(`[author]:`·`Observation:` 벗김; action=thought+op 요약; final=result) | shape |
| `turn` | LLM 턴 인덱스 | loop `ctx.set_turn` (턴 경계) |
| `ts` | ISO 타임스탬프 | 쓰기 시각 |
| `author` | 닉네임(웹 멀티유저) | `_add_user_message` 가 레코드에 동봉 |

## 핵심 설계 포인트
- **enrich 는 파일 쓰기에만** (`manager._append_to_history` → `_enrich_record`).
  `_cache`/`get_messages`(LLM 경로)는 무변경 — round-trip/LLM 호출이 절대 안 깨짐
  (extra 키 무시). 외부 jq 와 read_context 가 파일의 enrich 키를 쓴다.
- **`_classify_record` 단일 출처** — 쓰기 enrich 와 read_context 의 **읽기 시점**이
  같은 함수로 분류. 그래서 read_context 는 어떤 레코드 shape 든 동작하고(영속
  enrich 유무 무관), prefix 관습을 재추측하지 않는다. `turn`/`author` 만 영속 키
  의존(읽기로 못 만듦).
- **`turn` 스탬프**: `run()` while 상단에서 `ctx.set_turn(self.turn + 1)` — 곧
  실행될 턴 번호를 미리 박아 injected 메시지 + 그 턴 action/observation 이 같은
  turn 을 공유. 시작 쿼리는 turn 0.

## read_context = JSON 쿼리
`mode=search` 필터(자유 조합, ≥1 필수): `keyword`(text 부분일치)·`kind`·`tool`
(tools 멤버십)·`author`·`turn`(int 또는 {from,to}) + `sessions`(current/all/<id>).
→ loc + kind/turn/tools/author + text 프리뷰, 50 cap. `mode=list`/`fetch` 무변경.

구 `scope`(reasoning/tool/observation/query)+keyword-필수 폐기. 구 tool-scope 가
단수 `action` 만 읽어 ops-모양 액션을 놓치던 버그도 `text` 기반으로 해소.

## 변경 파일
- `agent_cli/context/manager.py` — `set_turn`/`_current_turn`, `_enrich_record`(파일),
  모듈 `_classify_record`/`_op_summary`.
- `agent_cli/loop.py` — `run()` 턴 경계 `set_turn`; `_add_user_message` 가 `author`
  동봉.
- `agent_cli/tools/context.py` — `_mode_search` 필드 쿼리 재작성(`_match_record`,
  `_normalize_kinds`/`_normalize_str_set`/`_normalize_turn`), 스키마/디스패치/도크
  갱신, `_classify_record` 읽기 재사용. (구 `_match_turn`/`_format_tool`/
  `_format_obs_match`/`_normalize_scope`/`_VALID_SCOPES` 제거.)
- 테스트: `_classify_record`/enrich(manager) + read_context 필드 쿼리 전면 갱신.

## 범위 밖 (다음 단계)
BM25/FTS5 랭킹, action↔observation `ref` 링크, text 평탄화 고도화.
