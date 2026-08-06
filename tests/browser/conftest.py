"""실브라우저 테스트 픽스처 (v7.12.0 — 이번 주 스크래치 검증 패턴의 승격).

jsdom 류 유닛이 원리적으로 못 잡는 부류 — 연결 풀 고갈, secure
context, CSS 렌더링(클리핑/ellipsis), 실 SSE 타이밍, 서버↔프런트
실계약 — 를 실서버(WebRenderer+WebServer+uvicorn) + 헤드리스 크롬으로
검증한다. 실사례: confirm-starvation 사가의 프런트 버그 4건이 전부 이
층에서만 잡혔다 (docs/ARCHITECTURE.md 테스트 섹션).

실행: ``AGENT_CLI_BROWSER_TESTS=1 pytest tests/browser/`` — 기본
스위트에서는 전량 skip (ollama_integration 마커와 동일 옵트인 철학,
playwright+chromium 필요).
"""

from __future__ import annotations

import os
import socket
import threading
import time

import pytest

pytestmark = pytest.mark.browser


# 옵트인 게이팅은 루트 conftest 의 collect_ignore 가 담당 (수집 단계
# 이벤트-루프 누출 방지) — 여기선 마커만 부착.
@pytest.fixture(scope="session")
def browser():
    playwright_api = pytest.importorskip("playwright.sync_api")
    with playwright_api.sync_playwright() as p:
        b = p.chromium.launch()
        yield b
        b.close()


@pytest.fixture
def page(browser):
    ctx = browser.new_context(viewport={"width": 1100, "height": 750})
    yield ctx.new_page()
    ctx.close()


class WebStack:
    """실 uvicorn 위의 WebRenderer+WebServer — 브라우저가 붙는 대상.

    worker 는 테스트가 직접 건다 (``start_confirm_loop`` / ``start_ask``)
    — 시나리오마다 필요한 대기 상대가 다르므로 픽스처는 전송층만 소유.
    """

    def __init__(self, view_token: str | None = None):
        import uvicorn

        from agent_cli.render.web import WebRenderer
        from agent_cli.web.server import WebServer, create_app

        self.renderer = WebRenderer(workspace=os.getcwd())
        self.token = "browser-test"
        # 관전 모드는 opt-in — 안 주면 view_token=None 이라 기존 시나리오는
        # 전권 단일 토큰 그대로다.
        self.view_token = view_token
        self.server = WebServer(self.renderer, token=self.token, view_token=view_token)
        app = create_app(self.server)
        # 포트 0 = OS 임시 할당 — 병렬/반복 실행 충돌 없음.
        self._config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="error")
        self._uv = uvicorn.Server(self._config)
        self._thread = threading.Thread(target=self._uv.run, daemon=True)
        self._workers: list[threading.Thread] = []

    def emit_ready(self):
        """실 부트스트랩의 header() 동형 — ready sticky (top-bar·ws 칩)."""
        self.renderer.header("openai", "Test-Model-8bit", max_turns=0)

    def start(self) -> str:
        self._thread.start()
        deadline = time.time() + 10
        while not self._uv.started:
            if time.time() > deadline:
                raise RuntimeError("uvicorn did not start")
            time.sleep(0.02)
        sock: socket.socket = self._uv.servers[0].sockets[0]
        port = sock.getsockname()[1]
        self.url = f"http://127.0.0.1:{port}/?token={self.token}"
        self.base = f"http://127.0.0.1:{port}"
        # 관전 URL — 같은 페이지, 읽기 전용 토큰. 운영자가 실제로 나눠 주는
        # 링크가 이 형태라 테스트도 같은 경로로 연다.
        self.watch_url = (
            f"http://127.0.0.1:{port}/?token={self.view_token}"
            if self.view_token
            else ""
        )
        return self.url

    def stop(self):
        # ★worker 가 다음 confirm/ask 에서 블록된 채 남으면 모듈-전역
        # interactive_lock 을 쥐고 있어 다음 테스트가 락을 못 얻는다
        # (테스트 간 간섭). should_exit + push_abort 로 블록을 풀어
        # 워커 루프가 빠져나가게 한다.
        self._uv.should_exit = True
        for _ in range(len(self._workers) + 1):
            self.renderer.push_abort()
        for w in self._workers:
            w.join(timeout=3)
        self._thread.join(timeout=5)

    # ── worker 헬퍼 ──
    def start_confirm_loop(self, results: list, command=None, danger_spans=None):
        """접속자가 생기면 y/n/a confirm 을 반복해서 묻는 worker —
        해결된 (key, comment) 를 results 에 적재 (confirm_repro 승격).

        ``command``/``danger_spans`` 를 주면 위험 명령 강조 경로를 구동한다
        (구조화 필드로 confirm 에 전달 → 다이얼로그가 명령을 강조 렌더)."""
        from agent_cli.render.base import ConfirmOption

        options = [
            ConfirmOption(key="y", label="once", aliases=("yes",)),
            ConfirmOption(key="n", label="deny", aliases=("no",)),
            ConfirmOption(key="a", label="always", aliases=("always",)),
        ]

        def worker():
            while not self.renderer.has_live_connections():
                if self._uv.should_exit:
                    return
                time.sleep(0.05)
            while not self._uv.should_exit:
                try:
                    res = self.renderer.confirm(
                        "\n⚠ Dangerous command detected. Allow?",
                        options,
                        default_key="n",
                        command=command,
                        danger_spans=danger_spans,
                    )
                except EOFError:
                    return  # stop() 의 push_abort — 락 해제하고 종료
                results.append(res)
                time.sleep(0.1)

        t = threading.Thread(target=worker, daemon=True)
        self._workers.append(t)
        t.start()

    def start_ask(self, results: list, question: str = "which language?"):
        """ask(prompt_user) 1회 — 답을 results 에 적재."""

        def worker():
            while not self.renderer.has_live_connections():
                if self._uv.should_exit:
                    return
                time.sleep(0.05)
            try:
                results.append(
                    self.renderer.prompt_user(
                        "Your answer: ", context=f"Agent asks:\n  {question}"
                    )
                )
            except EOFError:
                return

        t = threading.Thread(target=worker, daemon=True)
        self._workers.append(t)
        t.start()


@pytest.fixture
def stack():
    s = WebStack()
    s.start()
    yield s
    s.stop()


@pytest.fixture
def spectator_stack():
    """Stack with spectating enabled — ``stack.watch_url`` is the read-only
    link an operator would hand out."""
    s = WebStack(view_token="browser-watch")
    s.start()
    yield s
    s.stop()
