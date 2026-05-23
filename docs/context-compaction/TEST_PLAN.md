# Context Compaction — Test Plan

> Status: Draft
> Date: 2026-05-22
> Owner: claude (RFC)
> Companion: [REQUIREMENTS.md](REQUIREMENTS.md), [DESIGN.md](DESIGN.md)

## 0. 우선순위 표

| ID | 영역 | 우선순위 | 자동화 |
|---|---|---|---|
| **TC-1** | Trigger — 90% threshold 도달 시 compaction 발동 | P0 | ✓ |
| **TC-2** | Trigger — threshold 미만이면 발동 X (기존 동작 보존) | P0 | ✓ |
| **TC-3** | Split — anchor (system) 보존, 첫 user query는 evict 대상 | P0 | ✓ |
| **TC-4** | Split — 토큰 기준 절반 evict, 마지막 메시지는 retained | P0 | ✓ |
| **TC-5** | Summary — compactor callback 호출, 결과가 _summary에 저장 | P0 | ✓ |
| **TC-6** | Summary — 두 번째 compaction 시 _merge_summaries 호출 (재귀) | P0 | ✓ |
| **TC-7** | Summary — char cap (8000) 초과 시 truncate | P1 | ✓ |
| **TC-8** | File list — `_PATH_TOOLS` 메시지에서 path 추출 + 누적 | P0 | ✓ |
| **TC-9** | File list — 같은 path 중복 추가 안 됨 | P0 | ✓ |
| **TC-10** | File list — shell 명령은 추출 X (FR-CC-5) | P0 | ✓ |
| **TC-11** | File list — delegate는 `<delegate:agent>` placeholder | P1 | ✓ |
| **TC-12** | get_messages — summary 있을 때 prepend, 없으면 기존 흐름 | P0 | ✓ |
| **TC-13** | get_messages — file list prepend (summary 다음) | P0 | ✓ |
| **TC-14** | Fallback — LLM 요약 실패 시 FIFO drop, 경고 로그 | P0 | ✓ |
| **TC-15** | Fallback — 자연 재시도 (다음 turn에 다시 trigger 발동) | P0 | ✓ |
| **TC-16** | Fallback — path 추출 실패는 compaction 성공 (빈 리스트) | P1 | ✓ |
| **TC-17** | Persistence — compaction.json 정확히 저장 (schema § 3) | P0 | ✓ |
| **TC-18** | Persistence — Resume 시 _summary / _file_list 복원 | P0 | ✓ |
| **TC-19** | Persistence — compaction.json 없으면 빈 상태로 초기화 | P0 | ✓ |
| **TC-20** | Persistence — version mismatch 시 무시 + 빈 상태 (forward compat) | P1 | ✓ |
| **TC-21** | Visualisation — render_status 호출 (compaction start / end) | P1 | ✓ |
| **TC-22** | AgentLoop integration — set_compactor 호출 | P0 | ✓ |
| **TC-23** | AgentLoop integration — compactor가 provider.call 통해 LLM 호출 | P1 | ✓ |
| **TC-24** | 회귀 — 기존 1475 tests 모두 통과 | P0 | ✓ |
| **TC-25** | 회귀 — `agent-cli run` 짧은 task (compaction 미발동) 동작 동일 | P0 | ✓ |
| **TC-26** | 회귀 — `agent-cli web` 짧은 chat (compaction 미발동) 동작 동일 | P0 | ✓ |
| **TC-27** | E2E — long session simulation (~50K tokens) 후 compaction 1+회 발생, LLM이 초기 작업 인식 | P1 | △ manual |
| **TC-28** | E2E — Resume after compaction → 요약/파일리스트 복원, 작업 이어짐 | P1 | △ manual |
| **TC-29** | E2E — Web 모드 compaction progress SSE 이벤트 확인 | P2 | △ manual |
| **TC-30** | 성능 — compaction 트리거 빈도 (turns.jsonl 분석) | P2 | manual |
| **TC-31** | Belt-and-braces — LLM 성공 후 cache 여전히 초과 시 추가 FIFO | P0 | ✓ |
| **TC-32** | Resume invariant — `dynamic_start_index` 기반 정확한 cache 로드 | P0 | ✓ |
| **TC-33** | Resume invariant — `dynamic_start_index` 가 history len 초과 시 fallback | P1 | ✓ |
| **TC-34** | 비활성 — `--no-compaction` CLI flag → set_compactor 미호출, FIFO만 | P0 | ✓ |
| **TC-35** | 비활성 — `AGENT_CLI_COMPACTION=off` 환경변수 동일 효과 | P0 | ✓ |
| **TC-36** | 비활성 — env 변수가 CLI flag 보다 우선 | P1 | ✓ |
| **TC-37** | TurnRecorder — `record_compaction` 호출, turns.jsonl 에 event 기록 | P0 | ✓ |
| **TC-38** | TurnRecorder — `fallback_used=True` 정확히 기록 (belt-and-braces 발동 시) | P1 | ✓ |
| **TC-39** | TurnRecorder — `failure_signal="summary_failed"` 정확히 기록 (LLM 실패 시) | P1 | ✓ |

---

## 1. Trigger 동작 (TC-1, TC-2)

### TC-1: 90% threshold 도달 시 compaction 발동

```python
def test_compaction_triggers_at_90_percent(tmp_path):
    """``add`` 호출 후 ``_cache_tokens > 0.9 * max_context_tokens``
    상태가 되면 ``_compact()`` 가 호출된다. 사용자가 ``add(message)`` 외
    수동 API 호출 없이 자동 트리거."""
    ctx = ContextManager(tmp_path, max_context_tokens=100)
    called = []
    ctx.set_compactor(lambda msgs: called.append(msgs) or "summary")
    # ~91 tokens 분량 메시지를 add → trigger 발동
    ctx.add({"role": "system", "content": "sys"})
    for i in range(20):
        ctx.add({"role": "user", "content": "x" * 20})  # 약 5 tokens 추정
    assert len(called) >= 1
```

### TC-2: threshold 미만은 발동 X

```python
def test_compaction_skipped_below_threshold(tmp_path):
    ctx = ContextManager(tmp_path, max_context_tokens=10_000)
    called = []
    ctx.set_compactor(lambda msgs: called.append(msgs) or "summary")
    ctx.add({"role": "system", "content": "sys"})
    ctx.add({"role": "user", "content": "small"})
    assert called == []
    assert ctx._summary == ""
```

---

## 2. Split / Anchor (TC-3, TC-4)

### TC-3: System anchor 보존, 첫 user query는 evict

```python
def test_split_preserves_system_only(tmp_path):
    """``_split_for_compaction`` returns:
       anchor = [system]
       evict = oldest tokens up to half of dynamic
       retained = rest
    첫 user query는 anchor 아님 (FR-CC-4 변경)."""
    ctx = ContextManager(tmp_path, max_context_tokens=100)
    ctx._cache = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "first query"},
        {"role": "assistant", "content": "a"},
        {"role": "user", "content": "second"},
        {"role": "assistant", "content": "b"},
    ]
    anchor, evict, retained = ctx._split_for_compaction()
    assert [m["role"] for m in anchor] == ["system"]
    # 첫 user query 도 evict 대상
    assert evict[0]["content"] == "first query"
    # 마지막 user query는 retained
    assert retained[-1]["content"] == "b"
```

### TC-4: 토큰 절반 evict

```python
def test_split_at_token_halfpoint(tmp_path):
    """evict의 총 토큰은 dynamic 전체의 약 50%. 절반 직전에서 stop —
    한 메시지 통째로만 evict (메시지 중간 자르기 X)."""
    ...
```

---

## 3. Summary (TC-5, TC-6, TC-7)

### TC-5: 첫 compaction은 callback 결과를 _summary로

```python
def test_first_compaction_stores_summary(tmp_path):
    ctx = ContextManager(tmp_path, max_context_tokens=50)
    ctx.set_compactor(lambda msgs: f"summary-of-{len(msgs)}")
    # ... 90% 초과되게 add ...
    assert ctx._summary.startswith("summary-of-")
```

### TC-6: 두 번째 compaction은 _merge_summaries 호출

```python
def test_second_compaction_merges_with_previous_summary(tmp_path):
    """첫 compaction 후 dynamic이 다시 90% 차면, callback 이 받는
    메시지에 'Earlier summary:' 가 포함되어야 한다 (재귀 단계)."""
    seen_calls = []

    def fake_compactor(msgs):
        seen_calls.append(msgs)
        return f"summary-{len(seen_calls)}"

    ctx = ContextManager(tmp_path, max_context_tokens=50)
    ctx.set_compactor(fake_compactor)
    # ... 첫 trigger, 두 번째 trigger 유도 ...

    # 두 번째 호출의 메시지에 "Earlier summary:" 들어 있음
    second_call = seen_calls[1]
    assert any("Earlier summary:" in m.get("content", "") for m in second_call)
    assert ctx._summary == "summary-2"
```

### TC-7: char cap

```python
def test_summary_truncated_at_char_cap(tmp_path):
    ctx = ContextManager(tmp_path, max_context_tokens=50)
    ctx.set_compactor(lambda msgs: "x" * 100_000)
    # ... trigger ...
    assert len(ctx._summary) == _SUMMARY_CHAR_CAP
```

---

## 4. File list (TC-8 ~ TC-11)

### TC-8: path 추출 + 누적

```python
def test_file_list_extracts_path_from_tool_results(tmp_path):
    """``user`` role 메시지의 ``tool`` + ``args.path`` 가 추출되고,
    여러 compaction 단계에서 누적된다."""
    ctx = ContextManager(tmp_path, max_context_tokens=50)
    ctx.set_compactor(lambda msgs: "s")
    # Inject tool-result messages with paths
    ctx._cache = [
        {"role": "system", "content": "sys"},
        {"role": "user", "tool": "write_file", "args": {"path": "a.py"}, "content": "..."},
        {"role": "user", "tool": "read_file", "args": {"path": "b.py"}, "content": "..."},
    ]
    ctx._cache_tokens = 999  # force trigger threshold
    ctx._compact()
    assert "a.py" in ctx._file_list
    assert "b.py" in ctx._file_list
```

### TC-9: dedup

```python
def test_file_list_dedups_across_compactions(tmp_path):
    """같은 path가 두 번의 compaction 단계에서 모두 등장해도 리스트엔
    한 번만."""
    ...
    # 첫 compaction 후 a.py 추가
    # 두 번째 compaction 후 다시 a.py
    assert ctx._file_list.count("a.py") == 1
```

### TC-10: shell skip

```python
def test_file_list_skips_shell_commands(tmp_path):
    """``shell`` 도구의 결과는 ``_PATH_TOOLS`` 에 없어 path 추출 안 함."""
    ctx._cache = [
        {"role": "system", "content": "sys"},
        {"role": "user", "tool": "shell", "args": {"command": "rm foo.py"}, "content": "..."},
    ]
    ctx._compact()
    assert ctx._file_list == []
```

### TC-11: delegate placeholder

```python
def test_file_list_records_delegate_as_placeholder(tmp_path):
    """``delegate`` 액션은 자체 subagent 디렉토리로 가 path 추적 안 됨.
    placeholder ``<delegate:agent_name>`` 으로 표시."""
    ctx._cache = [
        {"role": "system", "content": "sys"},
        {
            "role": "assistant",
            "action": "delegate",
            "action_input": {
                "tasks": [{"agent": "explorer", "task": "find X"}]
            },
        },
    ]
    ctx._compact()
    assert "<delegate:explorer>" in ctx._file_list
```

---

## 5. get_messages (TC-12, TC-13)

### TC-12: summary prepend

```python
def test_get_messages_prepends_summary_after_system(tmp_path):
    """``_summary != ""`` 이면 system prompt 직후 ``role=user``
    메시지로 prepend (``## Summary of earlier conversation`` 헤더)."""
    ctx._summary = "user did X then Y"
    ctx._cache = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "current question"},
    ]
    msgs = ctx.get_messages()
    assert msgs[0]["role"] == "system"
    assert "## Summary of earlier conversation" in msgs[1]["content"]
    assert "user did X then Y" in msgs[1]["content"]
    assert msgs[-1]["content"] == "current question"
```

### TC-13: file list prepend

```python
def test_get_messages_prepends_file_list_after_summary(tmp_path):
    """summary 뒤, dynamic 앞에 ``## Files touched in earlier turns``
    섹션이 들어간다."""
    ctx._summary = "..."
    ctx._file_list = ["a.py", "b.py"]
    ctx._cache = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "now"},
    ]
    msgs = ctx.get_messages()
    files_msg = next(m for m in msgs if "Files touched" in m["content"])
    assert "- a.py" in files_msg["content"]
    assert "- b.py" in files_msg["content"]
```

---

## 6. Fallback (TC-14, TC-15, TC-16)

### TC-14: LLM 요약 실패 시 FIFO drop

```python
def test_summary_failure_falls_back_to_fifo(tmp_path):
    def failing_compactor(msgs):
        raise RuntimeError("provider error")
    ctx.set_compactor(failing_compactor)
    # ... 90% 초과 ...
    # _compact() 시도 → CompactionError → _evict_fifo() 호출 → cache 축소
    assert ctx._cache_tokens <= ctx.max_context_tokens
    # 요약 없음 (실패라 _summary 미설정)
    assert ctx._summary == ""
```

### TC-15: 자연 재시도

```python
def test_failed_compaction_retries_on_next_add(tmp_path):
    """첫 trigger에서 callback 실패 → FIFO drop. 다음 add에서 다시
    90% 초과하면 callback이 또 호출된다 (별도 retry counter 없음)."""
    call_count = [0]

    def sometimes_failing(msgs):
        call_count[0] += 1
        if call_count[0] == 1:
            raise RuntimeError("transient")
        return "succeeded"

    ctx.set_compactor(sometimes_failing)
    # first add trips threshold → callback fails → fifo
    # second add trips threshold again → callback succeeds
    assert call_count[0] == 2
    assert ctx._summary == "succeeded"
```

### TC-16: path 추출 실패는 compaction 성공

path 추출 함수가 raise 해도 compaction은 진행되어야 함 — 파일 리스트만 비어 있는 채로.

---

## 7. Persistence (TC-17 ~ TC-20)

### TC-17: compaction.json 정확히 저장

```python
def test_compaction_json_schema(tmp_path):
    """첫 compaction 후 ``session_dir/compaction.json`` 이 정확한
    schema 로 저장된다."""
    ctx = ContextManager(tmp_path, max_context_tokens=50)
    ctx.set_compactor(lambda msgs: "summary text")
    # ... trigger ...
    f = tmp_path / "compaction.json"
    assert f.exists()
    data = json.loads(f.read_text())
    assert data["version"] == 1
    assert data["summary"] == "summary text"
    assert isinstance(data["file_list"], list)
    assert data["compaction_count"] == 1
    assert data["last_compacted_at"]
```

### TC-18: Resume 시 복원

```python
def test_resume_restores_summary_and_file_list(tmp_path):
    """compaction.json 이 있는 디렉토리에서 ``ContextManager(resume=
    True)`` 가 ``_summary`` + ``_file_list`` 를 복원한다."""
    (tmp_path / "compaction.json").write_text(json.dumps({
        "version": 1,
        "summary": "prev summary",
        "file_list": ["x.py"],
        "compaction_count": 2,
        "last_compacted_at": "2026-01-01T00:00:00Z",
    }))
    (tmp_path / "history.jsonl").write_text("")  # empty
    ctx = ContextManager(tmp_path, max_context_tokens=1000, resume=True)
    assert ctx._summary == "prev summary"
    assert ctx._file_list == ["x.py"]
    assert ctx._compaction_count == 2
```

### TC-19: compaction.json 없으면 빈 상태

```python
def test_no_compaction_json_starts_empty(tmp_path):
    """첫 세션이거나 compaction.json 없는 디렉토리에선 빈 상태."""
    (tmp_path / "history.jsonl").write_text("")
    ctx = ContextManager(tmp_path, max_context_tokens=1000, resume=True)
    assert ctx._summary == ""
    assert ctx._file_list == []
    assert ctx._compaction_count == 0
```

### TC-20: version mismatch forward compat

```python
def test_version_mismatch_ignored(tmp_path):
    """future version 의 compaction.json 은 무시되고 빈 상태로 초기화."""
    (tmp_path / "compaction.json").write_text(json.dumps({
        "version": 99,  # unknown future version
        "summary": "...",
    }))
    ctx = ContextManager(tmp_path, max_context_tokens=1000, resume=True)
    assert ctx._summary == ""
```

---

## 8. Visualisation (TC-21)

### TC-21: render_compaction_progress helper 호출 검증

```python
def test_compaction_emits_progress_through_render_helper(tmp_path, monkeypatch):
    """ContextManager는 ``render_status`` 같은 일반 함수가 아니라
    ``render_compaction_progress`` helper만 호출해야 한다. helper가
    UI 렌더링의 단일 진입점 — 미래 변경(progress bar, dedicated SSE
    이벤트)을 한 곳에 모으기 위한 invariant."""
    calls = []
    monkeypatch.setattr(
        "agent_cli.context.manager.render_compaction_progress",
        lambda **kw: calls.append(kw),
    )
    # ... compaction trigger ...
    phases = [c["phase"] for c in calls]
    assert "start" in phases and "done" in phases


def test_compaction_warning_uses_same_helper(tmp_path, monkeypatch):
    """Fallback 경고도 helper의 ``phase=warning`` 경로로 표시 —
    ContextManager가 직접 console.print / SSE emit 안 함."""
    calls = []
    monkeypatch.setattr(
        "agent_cli.context.manager.render_compaction_progress",
        lambda **kw: calls.append(kw),
    )
    # ... compactor가 실패하도록 trigger ...
    warning_calls = [c for c in calls if c["phase"] == "warning"]
    assert len(warning_calls) == 1
    assert warning_calls[0]["reason"]


def test_render_helper_routes_through_renderer_status(monkeypatch):
    """render_compaction_progress 자체의 단위 테스트 — 인자에 따라
    ``_renderer.status`` 가 정확한 level/text 로 호출되는지."""
    from agent_cli.render import render_compaction_progress
    captured = []
    monkeypatch.setattr(
        "agent_cli.render._renderer.status",
        lambda level, msg, turn=0: captured.append((level, msg)),
    )
    render_compaction_progress(
        phase="start", old_tokens=1000, evicted_count=5
    )
    render_compaction_progress(
        phase="done", old_tokens=1000, new_tokens=400
    )
    render_compaction_progress(phase="warning", reason="provider down")
    levels = [c[0] for c in captured]
    assert levels == ["info", "info", "warning"]
```

---

## 9. AgentLoop integration (TC-22, TC-23)

### TC-22: set_compactor 호출

```python
def test_agent_loop_registers_compactor(tmp_path, caps):
    """AgentLoop._setup() 또는 __init__ 안에서 ctx.set_compactor 가
    호출되어 callback이 등록되어야 한다."""
    ctx = ContextManager(tmp_path, max_context_tokens=10_000)
    provider = _make_provider(...)
    loop = AgentLoop(query="...", provider=provider, capabilities=caps,
                      model="m", ctx=ctx)
    assert ctx._compactor_callback is not None
```

### TC-23: provider.call 통해 LLM 호출

mock provider로 검증 — compactor callback이 호출되면 provider.call도 호출되는지.

---

## 10. 회귀 (TC-24 ~ TC-26)

### TC-24: 기존 전체 테스트

기존 1475 통과 + 신규 추가만 늘어남.

### TC-25 / TC-26: 짧은 task / short chat 동작 동일

compaction 트리거 안 되는 작은 세션은 이전 동작과 byte-level 동일해야 함 (get_messages 결과 비교).

---

## 11. E2E (TC-27 ~ TC-30)

### TC-27: long session simulation

`agent-cli run` 또는 `chat`을 직접 띄워 ~50K 토큰까지 차오르도록 long task 실행. compaction이 1회 이상 발생하고, 그 후 사용자가 "처음에 만든 X 파일" 같이 물으면 LLM이 인식.

자동화 어려움 — local model + 실제 시나리오. △ 표시.

### TC-28: Resume after compaction

세션을 ~50K 토큰까지 채우고 종료 → `--resume <id>` → 요약 + 파일 리스트 복원 + LLM이 작업 인지.

### TC-29: Web mode

`agent-cli web` 에서 long session → 프론트엔드에서 "Compacting context (...)" status 메시지 표시되는지 확인.

### TC-30: 성능

`turns.jsonl` 분석해 compaction 빈도, 각 회의 토큰 변화량, LLM 응답 시간 측정. P2 — 측정 후 threshold / cap 조정 가능.

---

## 12. 안전망

- 모든 P0 / P1 자동화 가능 — pytest로 fixture + mock callback 기반 검증
- 단위 테스트는 LLM 실제 호출 X (callback mock)
- 통합 테스트는 AgentLoop과 ContextManager 결합 검증, provider mock으로 LLM 호출 가짜화
- E2E는 manual / staging 환경에서

회귀 안전: 기존 ContextManager.add / get_messages / get_raw_messages 인터페이스 보존. 새 동작은 90% threshold 이상에서만 발동 — 짧은 세션은 영향 0.
