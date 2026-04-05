# Context 재설계 — 남은 기술 부채

> Date: 2026-04-05
> 상태: 대부분 완료

## 완료됨 ✅

1. ~~test_loop.py old API 생성자~~ → 16개 수정 완료
2. ~~native tool calling dead code~~ → 429줄 삭제
3. ~~skip된 테스트~~ → 2172줄 삭제
4. ~~_build_artifact_tags/_summary~~ → 삭제
5. ~~_append_native_observation~~ → 삭제
6. ~~Legacy bridge~~ → 163줄 삭제

## 남은 항목

### 6. read_artifact tool 정리

현재 stub으로 동작 (파일 직접 읽기).
system_prompt.py의 _ARTIFACT_INLINE 가이드 텍스트가 old 형태 (scratchpad 참조).
read_file로 대체 가능하므로 tool 자체 제거 검토.
우선순위: 낮음. 기능에 영향 없음.

### 7. docs/ARCHITECTURE.md 업데이트

현재 ARCHITECTURE.md가 old 구조(scratchpad, compression, step counter 등)를 기술.
새 구조(FIFO, history.jsonl, delegate subdir 등)로 업데이트 필요.
