# diff 파이프라인: context(LLM) ↔ UI render

> 임시 분석 문서. write_file/edit_file 의 unified diff 가 **하나의 소스**에서
> 만들어져 **두 소비자**(LLM context, 사람 UI)에게 각자 다른 형태로 전달되는
> 구조와 처리 순서를 정리한다.

---

## 1. 핵심 추상화 원칙

> **diff 데이터는 plain, 색상은 render 시점에.**

- `format_diff` 는 **plain 표준 unified diff** 만 만든다 (git diff 텍스트:
  `--- a/` · `+++ b/` · `@@ … @@` · ` ` · `-` · `+`). colour markup·gutter 없음.
- 이 plain diff 가 그대로 **LLM observation** 에 들어간다 → context 가 깨끗
  (색상 태그로 토큰 낭비/노이즈 없음).
- **색상은 두 렌더러가 각자** 입힌다 — diff 라인의 첫 글자만 보고:
  - CLI: `MinimalRenderer._colorize_diff_line` → Rich markup → ANSI
  - web: `app.js colorizeDiffBody` → `rich-*` HTML span → CSS

한 데이터 경로, 매체별 색상. "UI 렌더링은 render 모듈에 집중" 원칙의 적용.

---

## 2. 컴포넌트 & 책임

| 계층 | 위치 | 책임 |
|---|---|---|
| **생성** | `tools/_diff.py` `format_diff(old, new, path)` | plain unified diff 문자열. 100줄 cap(`MAX_DIFF_LINES`) + `DIFF_TRUNCATION_PREFIX` summary |
| **부착** | `tools/write_file.py` / `tools/edit_file.py` | `ToolResult.output = "<요약>\n\n<plain diff>"` |
| **분기점** | `loop.py` `_execute_*` (≈1075) | `observation = tool_result.output` 한 문자열을 ①render ②ctx 양쪽으로 |
| **UI 운반** | `loop.py` `render_step("observation", …)` | → `renderer.observation(content, …)` |
| **ctx 운반** | `loop.py` `_append_observation(...)` | `obs_msg = "Observation: " + observation` → `ctx.add(...)` (history.jsonl + cache) |
| **LLM 변환** | `context/manager.py` `_convert_observation` | history record → `"[tool] args\n<plain diff>"` (색상 없음 그대로) |
| **CLI 색상** | `render/minimal.py` `MinimalRenderer.observation` + `_colorize_diff_line` | 요약줄 + `--- a/` 블록을 라인 첫 글자별 Rich style |
| **web 색상** | `render/web.py` `WebRenderer.observation` → SSE → `app.js` `renderObservation` + `colorizeDiffBody` | `observation` 이벤트(plain content) → `escapeHtml` → 첫 글자별 `rich-*` span |

---

## 3. 처리 시퀀스

```mermaid
sequenceDiagram
    participant LLM
    participant Loop as AgentLoop
    participant Tool as write_file/edit_file
    participant Diff as format_diff
    participant Ctx as ContextManager
    participant CLI as MinimalRenderer
    participant Web as WebRenderer→app.js

    LLM->>Loop: action: write_file(path, content)
    Loop->>Tool: _dispatch_tool_with_hooks
    Tool->>Diff: format_diff(old, new, path)
    Diff-->>Tool: PLAIN unified diff
    Tool-->>Loop: ToolResult.output = "File saved…\n\n<plain diff>"

    Note over Loop: observation = tool_result.output (한 문자열)

    par UI 렌더 (사람)
        Loop->>CLI: render_step("observation", observation)
        CLI->>CLI: 요약줄 + _colorize_diff_line(라인별)
        CLI-->>CLI: ANSI 색상 diff (터미널)
        Loop->>Web: render_step → WebRenderer.observation
        Web->>Web: SSE "observation" (content = plain)
        Web->>Web: app.js colorizeDiffBody(escapeHtml)
        Web-->>Web: rich-* span diff (브라우저)
    and context 저장 (LLM)
        Loop->>Ctx: _append_observation("Observation: "+observation)
        Ctx->>Ctx: ctx.add({tool, content}) → history.jsonl + cache
    end

    Note over Ctx,LLM: 다음 turn
    LLM->>Ctx: get_messages()
    Ctx->>Ctx: _convert_observation → "[write_file] path\n<plain diff>"
    Ctx-->>LLM: PLAIN diff (색상 태그 없음)
```

핵심: **`observation` 문자열은 단 하나(plain)**. 분기점(`loop.py` ≈1075)에서
같은 문자열이 render_step(UI)과 `_append_observation`(ctx)로 동시에 흐른다.
색상은 그 이후 각 렌더러 안에서만 생긴다 — ctx/LLM 쪽으로는 절대 새지 않음.

---

## 4. 단계별 데이터 형태 (같은 diff)

**① `format_diff` 출력 = ToolResult.output 의 diff 부분 (plain)**
```
--- a/x.py
+++ b/x.py
@@ -1,2 +1,2 @@
 def foo():
-    return 1
+    return 2
```

**② LLM 이 보는 context (`_convert_observation`)** — 그대로 plain
```
[write_file] x.py
File saved: x.py (20 bytes)

--- a/x.py
+++ b/x.py
@@ -1,2 +1,2 @@
 def foo():
-    return 1
+    return 2
```

**③ CLI 화면** — `_colorize_diff_line` 이 라인별 Rich style → 터미널 색상
(데이터는 ②와 동일, 출력만 색칠)

**④ web 화면** — `colorizeDiffBody` 가 `escapeHtml` 후 첫 글자별 span
```html
<span class="rich-bold">--- a/x.py</span>
<span class="rich-cyan">@@ -1,2 +1,2 @@</span>
 def foo():
<span class="rich-red">-    return 1</span>
<span class="rich-green">+    return 2</span>
```

---

## 5. 색상 규칙 (CLI·web 동일 매핑)

| diff 라인 | 판별 (첫 글자) | CLI Rich | web class | 색 |
|---|---|---|---|---|
| `--- a/` · `+++ b/` | `--- ` / `+++ ` | `[bold]` | `.rich-bold` | 굵게 |
| `@@ … @@` | `@@` | `[cyan]` | `.rich-cyan` | 청록 |
| 추가 | `+` | `[green]` | `.rich-green` | 초록 |
| 삭제 | `-` | `[red]` | `.rich-red` | 빨강 |
| context / 빈 줄 / truncation | 그 외 | (없음) | (없음) | 평문 |

- CLI 는 라인 content 의 `[` 를 `\[` 로 escape (Rich 태그 오인 방지).
- web 은 `escapeHtml` 이 먼저(`<`→`&lt;` 등) → XSS 안전, 그 위에 span.
- diff 영역 감지: `--- a/` 헤더부터 diff 로 간주 (앞의 "File saved…" 는 평문).

---

## 6. 설계 근거 (왜 이렇게)

1. **context 위생**: 이전엔 `format_diff` 가 `[green]+…[/green]` markup +
   `   1    1  ` gutter 를 박아 LLM observation 에 그대로 들어갔다 → 토큰 낭비 +
   모델 노이즈. plain 으로 바꿔 근본 제거.
2. **단일 소스, 매체별 표현**: diff 계산은 한 곳(`format_diff`), 색상 표현은
   매체별 렌더러. 새 렌더러(`render/<name>.py`)도 동일 plain diff 를 받아 자기
   방식으로 색칠하면 된다.
3. **위치 정보**: 라인별 gutter 대신 `@@ -A,B +C,D @@` hunk 헤더로 — git diff
   와 동일 관례, LLM 친화적.

---

## 관련 PR
- #27 `refactor(diff): plain unified diff in context, colour at render time`
