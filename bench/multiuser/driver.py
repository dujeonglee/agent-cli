"""다중 사용자 벤치 공용 드라이버 — 서버 기동·입력 주입·turns.jsonl 파싱·통계.

프로세스 관리·통계의 골격은 초기 탐색 단계에서 쓰던 하네스에서 가져왔고,
**측정 방식은 다르다**: SSE 이벤트 타임라인 대신 **서버 내부 계측(turns.jsonl,
M2)** 을 읽는다. 한 프로세스의 단조 시계(mono_ms)라 클라이언트/서버 시계 차
보정이 필요 없고, 거부 계약의 재시도 대기까지 서버 관점에서 일관되게
잡힌다(reject 이벤트가 첫 시도 시각).

의존성 없음(stdlib) — 리포 규약 "새 의존성 최소화".
"""

from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
AGENT_BIN = ROOT / ".venv" / "bin" / "agent-cli"
PYTHON_BIN = ROOT / ".venv" / "bin" / "python"
TOKEN = "benchtok"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _http(url: str, data: dict | None = None, timeout: float = 10.0):
    req = urllib.request.Request(
        url,
        data=json.dumps(data).encode() if data is not None else None,
        headers={"Content-Type": "application/json"} if data is not None else {},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.status, resp.read()


def wait_http(url: str, timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            status, _ = _http(url, timeout=2.0)
            if status == 200:
                return
        except (urllib.error.URLError, OSError):
            pass
        time.sleep(0.25)
    raise RuntimeError(f"server not ready: {url}")


class MockLlm:
    """mock_llm.py 서브프로세스. ``env`` 로 노브 전달 (MOCK_LLM_CTX 등)."""

    def __init__(self, env: dict | None = None):
        self.port = free_port()
        run_env = dict(os.environ)
        run_env.update(env or {})
        self.proc = subprocess.Popen(
            [
                str(PYTHON_BIN),
                str(ROOT / "bench" / "multiuser" / "mock_llm.py"),
                str(self.port),
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=run_env,
            start_new_session=True,
        )
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            try:
                with socket.create_connection(("127.0.0.1", self.port), 1):
                    return
            except OSError:
                time.sleep(0.1)
        raise RuntimeError("mock LLM did not start")

    def stop(self):
        try:
            os.killpg(self.proc.pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass


class AgentServer:
    """``agent-cli web`` 서브프로세스 1개 = 실험 조건 1개.

    워크스페이스는 조건마다 새 임시 디렉토리 — 세션·turns.jsonl·산출 파일이
    조건 간에 섞이지 않는다.
    """

    def __init__(
        self,
        workspace: Path,
        mock_port: int | None,
        *,
        contract: str = "serial",
        lock_scope: str | None = None,
        max_turns: int = 4,
        extra: list[str] | None = None,
        resume: str | None = None,
        real_llm: dict | None = None,
    ):
        """``mock_port`` 대신 ``real_llm={"model","base_url","api_key"}`` 를
        주면 실 LLM 을 향한다(P6). ``resume`` 은 같은 워크스페이스의 기존
        세션 id 로 재기동한다(P7 suspend→resume)."""
        self.workspace = Path(workspace)
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.port = free_port()
        if real_llm is not None:
            model = real_llm["model"]
            base_url = real_llm["base_url"]
            api_key = real_llm["api_key"]
        else:
            model = "bench-mock"
            base_url = f"http://127.0.0.1:{mock_port}/v1"
            api_key = "bench"
        args = [
            str(AGENT_BIN),
            "web",
            "--host",
            "127.0.0.1",
            "--port",
            str(self.port),
            "--token",
            TOKEN,
            "--no-browser",
            "-m",
            model,
            "--base-url",
            base_url,
            "--api-key",
            api_key,
            "--concurrency-contract",
            contract,
            "--max-concurrent-turns",
            str(max_turns),
            "--turn-metrics",
        ]
        if lock_scope is not None:
            args += ["--lock-scope", lock_scope]
        if resume is not None:
            args += ["--resume", resume]
        args += extra or []
        # env 로 provider 설정을 공급한다 — 이 프로젝트의 운용 환경은 홈
        # config.json 없이 AGENT_CLI_* env 로 돌므로, 지우기만 하면 설정
        # 부재로 셋업 위저드가 떠서 부팅이 멈춘다. 플래그(-m/--base-url)가
        # env 보다 우선하므로 값 자체는 어차피 위 provider 로 고정된다.
        env = {k: v for k, v in os.environ.items() if not k.startswith("AGENT_CLI_")}
        env.update(
            {
                "AGENT_CLI_PROVIDER": "openai",
                "AGENT_CLI_BASE_URL": base_url,
                "AGENT_CLI_API_KEY": api_key,
                "AGENT_CLI_MODEL": model,
                # HOME 격리: 벤치가 사용자 ~/.agent-cli(models.json 능력치
                # 자동 저장 등)를 읽지도 쓰지도 않게 한다 — 재현성 + 무오염.
                "HOME": str(self.workspace),
            }
        )
        self.log = open(self.workspace / "server.log", "w")  # noqa: SIM115
        self.proc = subprocess.Popen(
            args,
            cwd=self.workspace,
            stdout=self.log,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            env=env,
            start_new_session=True,
        )
        wait_http(self.base + "/api/health?token=" + TOKEN)
        sessions = sorted(
            (self.workspace / ".agent-cli" / "sessions").glob("*/"),
            key=lambda p: p.stat().st_mtime,
        )
        if not sessions:
            raise RuntimeError("no session dir after server start")
        self.session_dir = sessions[-1]

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def chat(self, content: str, conn_id: str) -> int:
        """chat 입력 1건. HTTP status 반환 (409 = 거부 계약)."""
        try:
            status, _ = _http(
                f"{self.base}/api/input?token={TOKEN}",
                {"kind": "chat", "content": content, "conn_id": conn_id},
            )
            return status
        except urllib.error.HTTPError as e:
            return e.code

    def chat_retry(
        self,
        content: str,
        conn_id: str,
        *,
        interval: float = 0.25,
        timeout: float = 120.0,
    ) -> int:
        """거부 계약용: 409 면 interval 간격으로 수용까지 재시도.

        재시도 횟수 반환. 서버 관점의 대기는 reject 이벤트로 잡히므로
        여기서는 수용만 보장한다 (거부 계약의 클라이언트 규약).
        """
        deadline = time.monotonic() + timeout
        retries = 0
        while time.monotonic() < deadline:
            if self.chat(content, conn_id) == 200:
                return retries
            retries += 1
            time.sleep(interval)
        raise RuntimeError("chat_retry timed out")

    def events(self) -> list[dict]:
        path = self.session_dir / "turns.jsonl"
        if not path.is_file():
            return []
        out = []
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                pass  # 마지막 반줄 관용 (crash-tolerant 분석 규약)
        return out

    def wait_completes(self, count: int, *, timeout: float = 300.0) -> list[dict]:
        """``phase=complete`` 이벤트가 count 개 쌓일 때까지 대기 후 전체 반환."""
        return self.wait_completes_since(0, count, timeout=timeout)

    def wait_quiescent(
        self,
        *,
        min_completes: int = 1,
        settle_s: float = 5.0,
        timeout: float = 600.0,
    ) -> list[dict]:
        """워커가 놀고 이벤트가 더 안 자랄 때까지 대기 후 전체 이벤트 반환.

        complete 계수 대기가 안 통하는 경우용 — 직렬 계약은 mid-run 주입으로
        뒤 메시지가 앞 런에 흡수돼 worker 수준 complete 가 메시지 수보다
        적을 수 있다. 판정: health busy=false AND 이벤트 수가 settle_s 동안
        불변 AND complete ≥ min_completes."""
        deadline = time.monotonic() + timeout
        stable_since = None
        last_n = -1
        while time.monotonic() < deadline:
            try:
                _, body = _http(f"{self.base}/api/health?token={TOKEN}", timeout=5.0)
                busy = json.loads(body).get("busy", True)
            except (urllib.error.URLError, OSError, json.JSONDecodeError):
                busy = True
            evs = self.events()
            n = len(evs)
            completes = sum(
                1
                for e in evs
                if e.get("event") == "turn" and e.get("phase") == "complete"
            )
            if not busy and n == last_n and completes >= min_completes:
                if stable_since is None:
                    stable_since = time.monotonic()
                elif time.monotonic() - stable_since >= settle_s:
                    return evs
            else:
                stable_since = None
            last_n = n
            time.sleep(1.0)
        raise RuntimeError("timed out waiting for quiescence")

    def wait_completes_since(
        self, offset: int, count: int, *, timeout: float = 300.0
    ) -> list[dict]:
        """이벤트 인덱스 ``offset`` 이후로 turn complete 가 ``count`` 개 쌓일
        때까지 대기 — 한 서버를 여러 rep 이 공유할 때의 경계."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            evs = self.events()
            done = sum(
                1
                for e in evs[offset:]
                if e.get("event") == "turn" and e.get("phase") == "complete"
            )
            if done >= count:
                return evs
            time.sleep(0.2)
        raise RuntimeError(f"timed out waiting for {count} completes after {offset}")

    def stop(self):
        try:
            os.killpg(self.proc.pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass
        try:
            self.proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(self.proc.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
        self.log.close()


# ── turns.jsonl 사슬 해석 ────────────────────────────


def turn_chain(events: list[dict], conn_id: str) -> dict:
    """conn_id 하나의 턴 사슬을 mono_ms 로 엮는다.

    반환: {enqueue, first_reject, dispatch, first_token, complete, rejects,
    turn_id, queue_id} (없는 항목은 None). 직렬 계약은 first_token 에 턴
    식별자가 없으므로 **비중첩 실행** 전제 하에 dispatch~complete 구간에 든
    첫 first_token 을 귀속시킨다 (직렬이므로 안전; 병렬은 turn_id 정합).
    """
    chain: dict = {
        "enqueue": None,
        "first_reject": None,
        "dispatch": None,
        "first_token": None,
        "complete": None,
        "rejects": 0,
        "turn_id": None,
        "queue_id": None,
    }
    for e in events:
        if e.get("event") == "reject" and e.get("conn_id") == conn_id:
            chain["rejects"] += 1
            if chain["first_reject"] is None:
                chain["first_reject"] = e["mono_ms"]
        if e.get("event") != "turn":
            continue
        if e.get("phase") == "enqueue" and e.get("conn_id") == conn_id:
            if chain["enqueue"] is None:
                chain["enqueue"] = e["mono_ms"]
                chain["queue_id"] = e.get("queue_id")
        elif (
            e.get("phase") == "dispatch"
            and chain["dispatch"] is None
            and chain["queue_id"] is not None
            and e.get("queue_id") == chain["queue_id"]
        ):
            chain["dispatch"] = e["mono_ms"]
            chain["turn_id"] = e.get("turn_id")  # 병렬만 존재
    # first_token / complete: 병렬은 turn_id 정합, 직렬은 dispatch 이후 첫 것.
    for e in events:
        if e.get("event") != "turn":
            continue
        tid = chain["turn_id"]
        phase = e.get("phase")
        if phase not in ("first_token", "complete"):
            continue
        matched = (tid is not None and e.get("turn_id") == tid) or (
            tid is None
            and chain[phase] is None
            and chain["dispatch"] is not None
            and e["mono_ms"] >= chain["dispatch"]
        )
        if matched:
            chain[phase] = e["mono_ms"]
    return chain


def ttft_ms(chain: dict) -> float | None:
    """사용자 관점 TTFT — 첫 시도(거부 포함)부터 첫 토큰까지."""
    if chain["first_token"] is None:
        return None
    t0_candidates = [
        t for t in (chain["first_reject"], chain["enqueue"]) if t is not None
    ]
    if not t0_candidates:
        return None
    return chain["first_token"] - min(t0_candidates)


# ── 통계 ──────────────────────────────────────────


def percentile(values: list[float], p: float) -> float:
    xs = sorted(values)
    if not xs:
        return float("nan")
    k = (len(xs) - 1) * p
    lo, hi = int(k), min(int(k) + 1, len(xs) - 1)
    return xs[lo] + (xs[hi] - xs[lo]) * (k - lo)


def median(values: list[float]) -> float:
    return percentile(values, 0.5)


def slope(points: list[tuple[float, float]]) -> float:
    """단순 선형회귀 기울기 — TTFT ~ L (P1 의 핵심 지표)."""
    n = len(points)
    if n < 2:
        return float("nan")
    mx = sum(x for x, _ in points) / n
    my = sum(y for _, y in points) / n
    denom = sum((x - mx) ** 2 for x, _ in points)
    if denom == 0:
        return float("nan")
    return sum((x - mx) * (y - my) for x, y in points) / denom
