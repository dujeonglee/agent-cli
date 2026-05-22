# Web UI 3가지 문제 — Test Plan

> Status: Draft
> Date: 2026-05-22
> Owner: architect (web-fixes-3 team)
> Companion: [REQUIREMENTS.md](REQUIREMENTS.md), [DESIGN.md](DESIGN.md)

## 0. 우선순위 / 표

| ID | 영역 | 우선순위 | 자동화 | 검증 방법 |
|---|---|---|---|---|
| M-1 | Markdown — heading | P1 | O | 신규 unit |
| M-2 | Markdown — GFM table | P1 | O | 신규 unit |
| M-3 | Markdown — bold/italic | P2 | O | 신규 unit |
| M-4 | Markdown — list | P2 | O | 신규 unit |
| M-5 | Markdown — code fence 보존 | P1 | O | 신규 unit (안쪽 토큰 변환 금지) |
| M-6 | Markdown — XSS 안전성 | P0 | O | 신규 unit (`<script>` escape 유지) |
| R-1 | Resume — invalid id | P0 | O | pytest typer CLI |
| R-2 | Resume — valid id, ctx 복원 | P0 | O | pytest |
| R-3 | Resume — renderer replay → buffer 채움 | P0 | O | pytest (renderer 단위) |
| R-4 | Resume — header workspace 반영 | P1 | O | pytest |
| R-5 | Resume — 옵션 미지정 시 회귀 없음 | P0 | O | 기존 web 테스트 통과 |
| S-1 | Shutdown — Ctrl+C 1회, traceback 없음 | P0 | △ | manual + subprocess test |
| S-2 | Shutdown — worker 깨움 | P0 | O | pytest (sentinel) |
| S-3 | Shutdown — connection 정리 | P0 | O | pytest (renderer) |
| S-4 | Shutdown — finalize_session 호출 | P0 | O | pytest (mock) |
| S-5 | Shutdown — 두 번째 Ctrl+C 즉시 종료 | P1 | △ | manual |
| C-1 | 회귀 — `tests/test_web_renderer.py` 전체 통과 | P0 | O | pytest |
| C-2 | 회귀 — `tests/test_web_server.py` 전체 통과 | P0 | O | pytest |
| C-3 | 회귀 — `pytest tests/ -m "not ollama_integration"` 전체 통과 | P0 | O | pytest |
| C-4 | 회귀 — ruff check / format 통과 | P0 | O | ruff |
| C-5 | 회귀 — `agent-cli chat --resume`도 동일 ID로 정상 동작 | P0 | O | pytest |

자동화 컬럼: `O` = 자동 가능, `△` = 부분 자동(서브프로세스로 기동/종료 확인), `X` = 수동 전용.

---

## 1. Markdown (M-*)

위치: `tests/test_app_markdown.py` (신규). JS 코드를 직접 호출할 수 없으므로 **정규식 사양을 Python 측에 미러링한 검증 함수**는 만들지 않는다(이중 진실이 됨). 대신:

- 가능하면 `node` 실행이 OS에 있는 경우만 활성화되는 옵셔널 자동화(`pytest.importorskip` 패턴) 1개 추가:
  - app.js IIFE 안의 헬퍼들을 module export 하도록 작은 패치(테스트 모드에서만 `module.exports = {...}` 추가하거나, `globalThis.__exports`에 함수 노출).
  - 결정 보류 (DESIGN 5절): node 의존 없이 가는 게 안전 → 1차에는 **수동 검증 체크리스트**로 갈음하고 다음 PR에서 도입 검토.

수동 체크리스트(브라우저에서 직접 확인, P1 항목이라 차후 자동화 가능):

### M-1 — Heading
- 입력: `### Title\n## Sub\n# Big\n#### NotAHeading`
- 기대: `### Title`이 `<h3>`, `## Sub`가 `<h2>`, `# Big`이 `<h1>`, `####` 줄은 그대로 raw text.

### M-2 — GFM table
- 입력:
  ```
  | Name | Age |
  |------|-----|
  | Bob  | 30  |
  | Eve  | 25  |
  ```
- 기대: 2 column `<table>` 2 row 본문, 헤더 `<th>` 행.

### M-3 — Bold / Italic
- 입력: `**bold** and *italic* and **both *nested***`
- 기대: `<strong>bold</strong> and <em>italic</em>`. 중첩은 1차 범위 밖이라 본문이 망가지지 않으면 통과.

### M-4 — List
- 입력:
  ```
  - one
  - two
  - three

  1. first
  2. second
  ```
- 기대: 첫 그룹은 `<ul>` 3 `<li>`, 둘째 그룹은 `<ol>` 2 `<li>`.

### M-5 — Code fence 보존
- 입력:
  ````
  ```
  ## Inside should stay
  | not | a | table |
  ```
  ````
- 기대: 안쪽 `##`와 `|`이 그대로 표시(태그 변환 금지). `<pre>` 안에 들어 있어야 한다.

### M-6 — XSS 안전성 (P0)
- 입력: `<script>alert(1)</script>` + `### Heading <img onerror=...>`
- 기대: 모든 `<` / `>`가 entity로 escape된 상태로 표시. 신규 markdown 변환이 `<script>`나 `<img>`를 부활시키지 않음.

---

## 2. Resume (R-*)

위치: `tests/test_web_server.py` / `tests/test_web_renderer.py` 보강 + `tests/test_session.py` 일부.

### R-1 — Invalid session ID
```python
def test_web_resume_invalid_session(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    runner = typer.testing.CliRunner()
    result = runner.invoke(app, ["web", "--resume", "DOES-NOT-EXIST", "--no-browser"])
    assert result.exit_code == 1
    assert "not found" in result.stdout.lower()
```
uvicorn은 기동되지 않아야 한다 — port bind 시도가 일어나면 실패.

### R-2 — Valid resume, ContextManager 복원
- 사전: 임시 디렉토리에 `.agent-cli/sessions/<id>/session.jsonl` + `history.jsonl` 작성(user → assistant complete 한 쌍).
- 실행: `web()` 함수 일부를 분리한 헬퍼(예: `_prepare_web_session(resume)`) 또는 `load_session` + `ContextManager(resume=True)` 흐름을 직접 호출.
- 확인: `ctx.get_raw_messages()` 길이 == 2.

### R-3 — Renderer replay → buffer 채움 (핵심)
```python
def test_web_renderer_replay_from_history(tmp_path):
    sess = create_session(workspace=str(tmp_path))
    save_meta(sess)
    ctx = ContextManager(get_session_dir(sess), max_context_tokens=100_000)
    ctx.add({"role": "user", "content": "hi"})
    ctx.add({"role": "assistant", "thought": "ok", "action": "complete",
             "action_input": {"result": "hello"}})

    renderer = WebRenderer(workspace=str(tmp_path))
    renderer.replay_from_history(ctx)

    snapshot = renderer.register_connection(WebConnection(id="t1"))
    # ready (latest_ready) + user_message + assistant_turn 최소 3개
    event_names = [e for e, _ in snapshot]
    assert "user_message" in event_names
    assert "assistant_turn" in event_names
```

### R-4 — Header workspace 반영
- `WebRenderer(workspace=sess.workspace)` 후 `header(provider, model, max_turns)` 호출 → 신규 SSE 연결 snapshot의 `ready` 이벤트에 `workspace == sess.workspace`.

### R-5 — `--resume` 미지정 회귀 없음
- 기존 `test_web_server.py`의 정상 기동 케이스가 그대로 통과해야 한다.

### C-5 — `chat --resume` 회귀 없음
- `tests/test_session.py`에 이미 list/load/finalize 케이스가 있다면 그대로 유지.

---

## 3. Shutdown (S-*)

### S-2 — Worker shutdown sentinel (자동)
```python
def test_web_server_shutdown_wakes_worker():
    renderer = WebRenderer()
    srv = WebServer(renderer)
    msgs = []

    def worker():
        while True:
            m = srv.pop_chat()
            if m is srv.SHUTDOWN:
                msgs.append("done")
                break
            msgs.append(m)

    t = threading.Thread(target=worker, daemon=True)
    t.start()
    srv.push_chat("hello")
    srv.shutdown()
    t.join(timeout=1.0)
    assert msgs == ["hello", "done"]
    assert not t.is_alive()
```

### S-3 — SSE connection 정리 (자동)
```python
def test_web_renderer_shutdown_closes_connections():
    r = WebRenderer()
    c1, c2 = WebConnection(id="a"), WebConnection(id="b")
    r.register_connection(c1)
    r.register_connection(c2)  # c1 takeover
    r.shutdown_all_connections()
    # c2 큐에 close sentinel 들어와야 함
    item = c2.queue.get(timeout=0.5)
    assert item == _CLOSE_SENTINEL  # private symbol — patch as needed
    assert c2.closed.is_set()
```

### S-4 — finalize_session 호출 (자동, mock)
- `agent_cli.main._shutdown_web`을 직접 호출하고 `finalize_session`이 호출됐는지 assert.

### S-1 — End-to-end Ctrl+C 무 traceback (반자동)
- `subprocess.Popen(["agent-cli", "web", "--no-browser", "--port", "<free>"])` 기동 → 1초 대기 → `proc.send_signal(SIGINT)` → `proc.communicate(timeout=5)` → stderr에 `Traceback` 문자열 미포함.
- CI에서 port 충돌이 우려되면 `--port 0`로 free port 할당 + stdout에 출력된 URL 파싱. 1차에는 manual 체크리스트에만 남기고 자동화는 follow-up.

### S-5 — 2회 Ctrl+C 즉시 종료 (수동)
- S-1 시퀀스에서 1초 추가로 두 번째 SIGINT 송신 → exit code 130 또는 0 즉시 반환, hang 없음.

---

## 4. 회귀 (C-*)

### C-1 / C-2 / C-3
- `pytest tests/test_web_renderer.py tests/test_web_server.py -q` 우선 통과.
- 전체: `pytest tests/ -m "not ollama_integration"` 통과.

### C-4
- `ruff check agent_cli/ tests/` + `ruff format --check agent_cli/ tests/` 둘 다 0 exit.

---

## 5. 검증 순서 (구현자 권장)

1. **S-2 / S-3 / S-4** — shutdown 코어. 종료 흐름이 정리되어야 다른 테스트가 free port 안정성을 갖는다.
2. **R-3 (renderer replay)** — `--resume`의 핵심 단위.
3. **R-1 / R-2** — CLI 레벨 통합.
4. **M-1 ~ M-6** — markdown(수동 체크리스트 + 가능한 부분 자동화).
5. **S-1 / S-5** — manual smoke 1회.
6. **C-1..C-5** — 회귀 / lint 풀 패스.

---

## 6. Done 정의

- 위 P0 항목 전부 통과(자동화된 것).
- P1 항목 자동화된 것 통과; 수동 항목은 PR 본문에 스크린샷/로그 첨부.
- 한 커밋에 코드 + 테스트 + README + ARCHITECTURE 함께 포함(`CLAUDE.md` #6).
