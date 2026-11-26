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


def wait_held(page, held: list, n: int, timeout: float = 8.0) -> bool:
    """``page.route`` 의 처리기가 붙든 요청(``held``)이 ``n`` 개가 될 때까지 기다린다 — 찼으면 참. 읽기만 한다.

    동기점은 브라우저가 실제로 낸 요청이다. route 처리기는 파이썬이 **Playwright 호출 안에 있을 때만** 돈다 —
    ``time.sleep`` 으로만 기다리면, 가로챈 요청의 알림이 그 요청을 일으킨 호출(`press`·`click`)의 응답보다 늦게
    닿았을 때 목록이 끝내 차지 않는다(느린 러너에서 난 실패). 그래서 기다리는 동안 Playwright 를 부른다.
    ``page.expect_request`` 는 요청이 나간 것만 알려 준다 — 돌아온 순간 처리기는 아직 안 돌았을 수 있다."""
    deadline = time.time() + timeout
    while len(held) < n and time.time() < deadline:
        page.wait_for_timeout(
            20
        )  # Playwright 를 돌린다 — 밀린 route 처리기가 여기서 돈다
    return len(held) == n


class WebStack:
    """실 uvicorn 위의 WebRenderer+WebServer — 브라우저가 붙는 대상.

    worker 는 테스트가 직접 건다 (``start_confirm_loop`` / ``start_confirm`` / ``ask``)
    — 시나리오마다 필요한 대기 상대가 다르므로 픽스처는 전송층만 소유.
    """

    def __init__(self):
        import pathlib
        import tempfile

        import uvicorn

        from agent_cli.context.manager import ContextManager
        from agent_cli.render.web import WebRenderer
        from agent_cli.web.server import WebServer, create_app

        self.renderer = WebRenderer(workspace=os.getcwd())
        self.token = "browser-test"
        # 실 서버는 항상 ctx 를 들고 있다. 없으면 세션-한정 노브
        # (compaction·stall 등)의 POST 가 "no active context" 로 떨어져
        # 브라우저 층에서 왕복을 검증할 수 없다 (v8.60.0).
        self._ctxdir = tempfile.mkdtemp(prefix="agentcli-browser-ctx-")
        self.ctx = ContextManager(session_dir=pathlib.Path(self._ctxdir))
        # 노브 소유자 넷을 전부 꽂는다 (v10.45.0) — 칩은 소유자가 있는 노브만
        # 그려지므로(available), 렌더러 하나가 열 노브를 다 그리는지 보려면
        # registry·auditor·binding 도 있어야 한다.
        from types import SimpleNamespace

        from agent_cli.providers.capabilities import ModelCapabilities
        from agent_cli.subagent.agents_live import AgentRegistry
        from tests.binding_support import make_binding

        caps = ModelCapabilities(32768, 4096, True, supports_grammar=True)
        self.binding = make_binding(capabilities=caps, model="Test-Model-8bit")
        self.server = WebServer(
            self.renderer, token=self.token, ctx=self.ctx, binding=self.binding
        )
        self.server.agent_registry = AgentRegistry(pathlib.Path(self._ctxdir))
        # 확인(6d)은 세션의 레지스트리에 걸고 렌더러로 그린다 — 실 부트와 같은 두 전역
        from agent_cli import render, session_slots

        self._prev_renderer = render.get_renderer()
        render.set_renderer(self.renderer)
        session_slots.set_agent_registry(self.server.agent_registry)
        self.auditor = SimpleNamespace(enabled=False, in_flight=0)
        self.server.auditor = self.auditor
        # 실 부트(main.py web)와 같은 배선 — 감사 진행 수가 바뀌면 노브 상태를 다시 낸다
        self.renderer.knobs_changed = self.server.broadcast_knobs
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
        return self.url

    def wait_connected(self, timeout: float = 10.0) -> bool:
        """브라우저의 SSE 연결이 서버에 등록될 때까지 기다린다 — 등록되면 True,
        시간이 다 되거나 ``stop()`` 이 걸리면 False. 읽기만 한다.

        등록 뒤의 publish 는 그 연결 큐로 가서 스냅샷 다음에 빠짐없이 도착한다.
        등록 전의 비영속 이벤트(tick·status)는 재생되지 않으므로, 그런 이벤트를
        내는 테스트는 먼저 이걸 기다린다 (``page.goto`` 의 load 는
        EventSource 를 기다리지 않는다)."""
        deadline = time.time() + timeout
        while not self.renderer.hub.has_live_connections():
            if self._uv.should_exit or time.time() > deadline:
                return False
            time.sleep(0.05)
        return True

    def stop(self):
        # 확인을 기다리는 워커는 그 런의 정지(`should_exit`)로 깬다 — 제품의 정지와 같은 길(6d)
        from agent_cli import render, session_slots

        self._uv.should_exit = True
        for w in self._workers:
            w.join(timeout=3)
        self._thread.join(timeout=5)
        session_slots.set_agent_registry(None)
        render.set_renderer(self._prev_renderer)

    # ── worker 헬퍼 ──
    def ask(self, title="Dangerous command detected (`rm`)", *, choices=None, **kw):
        """확인 하나를 열고 답을 기다린다 — ``(답, 남긴 말)``, 정지로 깨면 ``("stopped", "")``."""
        from agent_cli import confirm

        _cid, d = confirm.ask_person(
            title,
            "",
            choices or confirm.safety_choices("once", "always"),
            owner="main",
            stopped=lambda: self._uv.should_exit,
            **kw,
        )
        return (d.action, d.note)

    def start_confirm_loop(self, results: list, command=None, danger_spans=None):
        """접속자가 생기면 확인을 거듭 여는 worker — 답 ``(답, 남긴 말)`` 을 results 에 적재.

        ``command``/``danger_spans`` 를 주면 위험 명령 강조 경로를 구동한다
        (구조화 필드로 확인에 실림 → 트레이가 명령을 강조 렌더)."""

        def worker():
            if not self.wait_connected():
                return
            while not self._uv.should_exit:
                res = self.ask(command=command, danger_spans=danger_spans)
                if res[0] == "stopped":
                    return
                results.append(res)
                time.sleep(0.1)

        t = threading.Thread(target=worker, daemon=True)
        self._workers.append(t)
        t.start()

    def start_confirm(self, results: list, question: str = "Allow this?"):
        """확인 1회 — ``(답, 남긴 말)`` 을 results 에 적재."""

        def worker():
            if self.wait_connected():
                results.append(self.ask(question))

        t = threading.Thread(target=worker, daemon=True)
        self._workers.append(t)
        t.start()


@pytest.fixture
def stack():
    s = WebStack()
    s.start()
    yield s
    s.stop()
