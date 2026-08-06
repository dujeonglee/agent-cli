#!/usr/bin/env python3
"""N4 — 늦은 합류자 증분 재생의 정합성 (v0.8 1단계 기능 검증).

병렬 계약으로 세 사용자가 턴을 흘리는 동안, 클라이언트 하나가 SSE 를 반복해서
**끊었다 붙는다**. 재접속 때마다 마지막으로 받은 SSE ``id``(= ``Last-Event-ID``,
브라우저 ``EventSource`` 가 자동으로 하는 일)를 제시하고, 서버는 그 뒤 이벤트만
재생한다. 물어보는 것은 하나다 — **끊긴 클라이언트가 결국 본 것이, 한 번도 안
끊긴 클라이언트가 본 것과 같은가.**

두 구독을 동시에 돌려 직접 대조한다:
  control — 실험 내내 붙어 있는 기준 구독
  cutter  — ``--cuts`` 회 끊었다 ``Last-Event-ID`` 로 재접속하는 구독

지표:
  ① 열 동일성 — seq 를 가진 이벤트 열이 control 과 **완전히 일치**(누락 0,
    중복 0, 순서 역전 0). 대조는 seq 뿐 아니라 **페이로드 원문까지** 비교하므로
    턴 귀속 필드(turn_id/task_id, 마커)가 재생에서 상하지 않았음을 함께 담는다.
  ② 마커 커버리지 — 제출한 사용자 메시지 마커가 전부 양쪽에 도착했는가.
  ③ 폴백 — 서버가 살릴 수 없는 커서를 제시하면 ``replay_reset`` + 전체 스냅샷.

seq 는 **영속 이벤트에만** 붙는다. transient(stream_chunk 등)는 끊긴 동안
지나가면 사라지는 것이 계약이다 — 재생 버퍼가 재현할 수 있는 마지막 지점에
커서가 고정돼야 하기 때문이다. 그래서 ①의 대조 대상도 seq 를 가진 이벤트다.

폴백 3분기 중 여기서 강제하는 것은 **다른 epoch**(프로세스 재기동 상황)와
**발급한 적 없는 미래 seq** 두 가지다. 세 번째인 "버퍼에서 트림돼 사라진
커서"는 같은 분기로 합류하지만 5,000개 영속 이벤트(≈1,500턴)를 쌓아야 도달해
이 하네스의 규모 밖이다 — 그 분기는 유닛
(``test_web_renderer.py::TestSeqCursor::test_trimmed_cursor_falls_back_to_full_snapshot``)
이 덮는다.

사용: .venv/bin/python bench/multiuser/n4_replay.py [--users 3] [--rounds 30] [--cuts 10]
"""

from __future__ import annotations

import argparse
import itertools
import json
import re
import shutil
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

from driver import TOKEN, AgentServer, MockLlm

MARKER_RE = re.compile(r"id=([A-Za-z0-9-]+)")


class SseSubscription(threading.Thread):
    """SSE 구독 하나. ``slice_events`` 를 주면 그만큼 받을 때마다 끊고 재접속한다.

    재접속 시 마지막으로 받은 ``id`` 를 ``Last-Event-ID`` 헤더로 되돌려준다 —
    브라우저 ``EventSource`` 의 동작을 그대로 흉내 낸 것이라, 재는 대상이
    하네스 전용 경로가 아니라 실제 클라이언트가 타는 경로다.
    """

    def __init__(self, base: str, *, slice_events: int | None = None):
        super().__init__(daemon=True)
        self.url = f"{base}/api/stream?token={TOKEN}"
        # 절단 시점을 **받은 이벤트 수**로 잡는다(벽시계가 아니라). 시계로
        # 자르면 워크로드가 예상보다 빨리 끝났을 때 절단이 트래픽 뒤 정적
        # 구간에 떨어져, 재접속은 했는데 놓친 것이 없는 무의미한 절단이 된다
        # (초안에서 10회 목표에 5회만 유효했다). 이벤트 계수는 절단이 항상
        # 흐르는 도중에 일어나는 것을 보장한다.
        self.slice_events = slice_events
        self.frames: list[dict] = []  # {seq, event, data}
        self.last_id: str | None = None
        self.connects = 0
        self.resets = 0
        self._halt = threading.Event()
        self._slice_seen = 0

    def stop(self) -> None:
        self._halt.set()

    def _open(self):
        req = urllib.request.Request(self.url)
        if self.last_id is not None:
            req.add_header("Last-Event-ID", self.last_id)
        self.connects += 1
        # 소켓 타임아웃은 "끊겼나" 판정이 아니라 readline 이 영원히 안 깨는
        # 것을 막는 장치다 — 유휴에는 keep-alive 주석이 오므로 타임아웃은
        # 그냥 루프를 한 바퀴 돌려 stop 플래그를 보게 한다.
        return urllib.request.urlopen(req, timeout=1.0)

    def run(self) -> None:
        while not self._halt.is_set():
            try:
                resp = self._open()
            except (urllib.error.URLError, OSError):
                time.sleep(0.05)
                continue
            self._slice_seen = 0
            try:
                self._read(resp)
            finally:
                try:
                    resp.close()
                except OSError:
                    pass

    def _read(self, resp) -> None:
        """한 접속에서 프레임을 모은다. sse-starlette 와이어: ``id:`` → ``event:``
        → ``data:`` 순, 빈 줄이 프레임 끝(CRLF). ``slice_events`` 만큼 재생
        가능한 이벤트를 받으면 돌아가서(=끊고) 다시 붙는다."""
        cur: dict = {}
        while not self._halt.is_set():
            try:
                raw = resp.readline()
            except (TimeoutError, OSError):
                continue  # 유휴 — stop 플래그 재확인
            if not raw:
                return  # 서버가 닫음
            line = raw.decode("utf-8", "replace").rstrip("\r\n")
            if line == "":
                if "event" in cur:
                    self._commit(cur)
                    if (
                        self.slice_events is not None
                        and self._slice_seen >= self.slice_events
                    ):
                        return  # 계획된 절단 — 흐르는 도중에
                cur = {}
                continue
            if line.startswith(":"):
                continue  # keep-alive 주석
            key, _, value = line.partition(":")
            cur[key.strip()] = value.strip()

    def _commit(self, frame: dict) -> None:
        event = frame.get("event", "")
        if event == "replay_reset":
            self.resets += 1
        seq_id = frame.get("id")
        if seq_id is not None:
            self.last_id = seq_id
            self._slice_seen += 1
        self.frames.append(
            {"seq": seq_id, "event": event, "data": frame.get("data", "")}
        )

    # ── 분석 헬퍼 ──

    def seq_stream(self) -> list[tuple[int, str, str]]:
        """seq 를 가진(=재생 가능한) 이벤트만, (seq, event, data) 로."""
        out = []
        for f in self.frames:
            if f["seq"] is None:
                continue
            out.append((int(str(f["seq"]).rsplit(":", 1)[-1]), f["event"], f["data"]))
        return out


def probe_fallback(base: str, cursor: str) -> dict:
    """살릴 수 없는 커서로 한 번 붙어 보고 첫 프레임들을 본다."""
    sub = SseSubscription(base)
    sub.last_id = cursor
    sub.start()
    time.sleep(2.0)
    sub.stop()
    sub.join(timeout=5)
    events = [f["event"] for f in sub.frames]
    return {
        "cursor": cursor,
        "reset_signalled": "replay_reset" in events,
        "reset_position": events.index("replay_reset")
        if "replay_reset" in events
        else None,
        "first_events": events[:3],
        "replayed_seq_events": len(sub.seq_stream()),
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--users", type=int, default=3)
    ap.add_argument("--rounds", type=int, default=30)
    ap.add_argument("--cuts", type=int, default=10)
    ap.add_argument("--out", type=Path, default=Path(__file__).parent / "out")
    args = ap.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)

    mock = MockLlm()
    ws = Path(tempfile.mkdtemp(prefix="n4-replay-"))
    server = AgentServer(ws, mock.port, contract="parallel", max_turns=args.users)
    total = args.users * args.rounds
    markers = [f"u{u}-r{r}" for r in range(args.rounds) for u in range(args.users)]
    try:
        control = SseSubscription(server.base)
        control.start()
        time.sleep(0.5)  # 두 구독이 같은 지점에서 출발하도록
        # 턴 1개 = 영속 이벤트 2개(user_message + assistant_turn)이므로 전체
        # 재생 가능한 이벤트 수는 turns×2 다. 그것을 cuts+1 구간으로 나누면
        # 절단이 트래픽 전 구간에 고르게 떨어진다.
        slice_events = max(1, (total * 2) // (args.cuts + 1))
        cutter = SseSubscription(server.base, slice_events=slice_events)
        cutter.start()

        for r in range(args.rounds):
            for u in range(args.users):
                marker = f"u{u}-r{r}"
                msg = f"question {marker} [[bench ttft=30 tok=1 n=6 id={marker}]]"
                assert server.chat(msg, f"user{u}") == 200
            time.sleep(0.05)
        server.wait_completes(total, timeout=600)
        time.sleep(3.0)  # 마지막 이벤트가 두 구독 모두에 닿을 여유

        cutter.stop()
        control.stop()
        cutter.join(timeout=10)
        control.join(timeout=10)

        ctrl_stream = control.seq_stream()
        cut_stream = cutter.seq_stream()
        ctrl_seqs = [s for s, _, _ in ctrl_stream]
        cut_seqs = [s for s, _, _ in cut_stream]

        # ① 열 동일성 — seq·이벤트명·페이로드 원문까지 완전 일치
        identical = ctrl_stream == cut_stream
        missing = sorted(set(ctrl_seqs) - set(cut_seqs))
        extra = sorted(set(cut_seqs) - set(ctrl_seqs))
        duplicates = len(cut_seqs) - len(set(cut_seqs))
        inversions = sum(1 for a, b in itertools.pairwise(cut_seqs) if b <= a)
        payload_mismatch = sum(
            1
            for a, b in zip(ctrl_stream, cut_stream)
            if a[0] == b[0] and (a[1], a[2]) != (b[1], b[2])
        )

        # ② 마커 커버리지 — 제출한 질문이 양쪽 열에 전부 나타났는가
        def seen_markers(stream) -> set[str]:
            found: set[str] = set()
            for _, _, data in stream:
                found.update(MARKER_RE.findall(data))
            return found

        ctrl_markers, cut_markers = seen_markers(ctrl_stream), seen_markers(cut_stream)
        submitted = set(markers)

        # ③ 폴백 — 살릴 수 없는 커서 두 종류
        epoch = None
        for f in cutter.frames:
            if f["seq"] and ":" in str(f["seq"]):
                epoch = str(f["seq"]).rsplit(":", 1)[0]
                break
        fallbacks = [
            probe_fallback(server.base, "deadbeef:1"),  # 다른 epoch(재기동)
            probe_fallback(server.base, f"{epoch}:999999999"),  # 발급한 적 없는 미래
        ]

        rows = [
            {"kind": "control", "seq": s, "event": e} for s, e, _ in ctrl_stream
        ] + [{"kind": "cutter", "seq": s, "event": e} for s, e, _ in cut_stream]
        (args.out / "n4-replay.jsonl").write_text(
            "".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8"
        )

        result = {
            "users": args.users,
            "rounds": args.rounds,
            "turns": total,
            "cut_slice_events": slice_events,
            "control_connects": control.connects,
            "cutter_connects": cutter.connects,
            "cuts_observed": cutter.connects - 1,
            "stream_equality": {
                "control_events": len(ctrl_stream),
                "cutter_events": len(cut_stream),
                "identical_including_payloads": identical,
                "missing": len(missing),
                "extra": len(extra),
                "duplicates": duplicates,
                "order_inversions": inversions,
                "payload_mismatch": payload_mismatch,
            },
            "marker_coverage": {
                "submitted": len(submitted),
                "control_seen": len(ctrl_markers & submitted),
                "cutter_seen": len(cut_markers & submitted),
                "cutter_missing": sorted(submitted - cut_markers)[:10],
            },
            "unservable_cursor_fallback": fallbacks,
            "resets_during_normal_reconnects": cutter.resets,
        }
        print(json.dumps(result, indent=2, ensure_ascii=False))
        (args.out / "n4-replay.json").write_text(
            json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8"
        )
    finally:
        server.stop()
        mock.stop()
        shutil.rmtree(ws, ignore_errors=True)


if __name__ == "__main__":
    main()
