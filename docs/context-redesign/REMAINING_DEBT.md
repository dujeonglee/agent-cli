# Context 재설계 — 남은 기술 부채

> Date: 2026-04-05
> 이전 작업: Phase 1~6 완료. 핵심 리팩토링 완성.

## 1. test_loop.py old API 생성자 (16개 실패)

테스트에서 `ContextManager(provider=..., model=..., capabilities=..., scratchpad_dir=...)` 형태로
old 생성자를 사용. 새 API `ContextManager(session_dir=...)` 로 변경 필요.

실패 테스트:
- TestGracefulInterrupt: test_interrupt_records_in_ctx, test_chat_mode_ctx_preserved_after_interrupt
- TestAskTool: test_ask_single_question 외 4개
- TestContextContinuity: test_tool_observation_in_ctx 외 2개
- TestAppendObservationHelpers: test_append_text_observation_basic
- TestReadyForReviewTextPath: 4개
- TestNoOutputTruncation: test_large_file_not_truncated

작업: 각 테스트의 ContextManager 생성부를 `ContextManager(session_dir=tmp_path)` 로 변경.
ctx.add("role", "content") 호출도 ctx.add({"role":..., "content":...}) 로 변경.

## 2. loop.py native tool calling dead code (~270줄)

`_handle_native_path()` 메서드와 관련 함수들이 아직 존재:
- `_handle_native_path()` (~270줄)
- `_append_native_observation()`
- `_format_tool_call_messages()`, `_format_anthropic_tool_messages()`, `_format_openai_tool_messages()`
- `convert_to_anthropic_tools`, `convert_to_openai_tools` import
- `_execute_iteration()`의 native tool calling 준비 코드 (supports_tool_calling 분기)
- `_call_llm()`의 force_compress 호출 2곳

설계 결정: native tool calling 미사용 (DESIGN.md 4.6).
`_execute_iteration()`에서 text path만 호출하도록 변경 후 위 함수들 전부 삭제.

## 3. skip된 87개 테스트

old behavior 테스트에 `@pytest.mark.skip` 추가됨. 두 가지 처리 방향:
- 완전 삭제: scratchpad, compression, old delegate format, old fork deep copy 테스트
- 새 API로 재작성: 여전히 유효한 동작을 검증하는 테스트

삭제 대상 클래스:
- TestScratchpadIntegration, TestArtifactLazyLoading, TestArtifactTags
- TestSkillNamePropagation, TestSkillSubdirectory, TestRunSkillIntercept
- TestRunSkillNoDuplicateArtifact, TestBuildReviewObservation
- TestPersistDelegateResult (test_delegate_output.py)
- TestForkContext, TestToolDelegate (test_tools_coverage.py)
- TestReadArtifactTool (test_tools_coverage.py)
- TestSessionScratchpadCoexistence, TestRunHeadlessTmpdir (test_session.py)

## 4. loop.py의 _build_artifact_tags, _build_artifact_summary 함수

scratchpad 연동용으로 만든 함수들이 아직 loop.py에 존재. 호출부는 제거됐지만 함수 정의가 남아있음.
삭제 대상.

## 5. _append_native_observation의 ctx 연동

`_append_native_observation()`에서 `ctx.add()` 호출이 있는데, native path가 dead code이므로
함수 자체 삭제와 함께 제거됨.

## 6. read_artifact tool 정리

현재 stub으로 동작. system_prompt.py의 _ARTIFACT_INLINE 가이드 텍스트가 아직 old 형태.
read_file로 대체 가능하므로 tool 자체 제거 검토.

## 7. docs/ARCHITECTURE.md 업데이트

현재 ARCHITECTURE.md가 old 구조(scratchpad, compression, step counter 등)를 기술.
새 구조(FIFO, history.jsonl, delegate subdir 등)로 업데이트 필요.
