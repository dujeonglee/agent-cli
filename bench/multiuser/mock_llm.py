#!/usr/bin/env python3
"""결정적 목 LLM — OpenAI 호환 /chat/completions SSE 서버 (bench 전용).

포크(Coagora) ``backend/bench/mockLlm.mjs`` 의 본류 대응물이다. 지연 지시자
문법(``[[bench k=v ...]]``)은 포크와 동일하게 유지해 두 구현의 실험 스크립트가
같은 시나리오 언어를 쓰지만, **도구 호출의 표현이 다르다**: 포크는 OpenAI
네이티브 ``tool_calls`` 델타를 흘리는 반면, 본류 에이전트는 도구를 **content
본문의 json_fc op 배열**로 파싱하므로 여기서는 content 로 op 를 흘린다.
(``agent_cli/wire_formats/json_fc.py`` — ``[{"action": ..., 파라미터}]``.)

난수·시계 의존 분기 없음 — 같은 대화 상태 + 같은 지시자는 항상 같은 응답을
만든다(포크 mockLlm.mjs:22 와 같은 결정성 계약). 진행 상태는 서버가 아니라
**대화 자체**에서 읽는다: 마지막 ``[[bench]]`` user 메시지 이후의 관찰(user
role, "Observation" 프리픽스) 개수가 곧 완료한 도구 스텝 수다.

지시자 파라미터 (기본값은 포크와 동일):
  ttft=100   첫 토큰까지 지연 ms
  tok=5      토큰 간 간격 ms
  n=12       방출 토큰 수
  work=0     >0 이면 먼저 shell sleep 도구 스텝 1회 (ms)
  fwrite=0   >0 이면 write_file 도구 스텝을 이 횟수만큼
  fpath=     fwrite 대상 경로
  marker=    fwrite 내용에 박을 마커 문자열 (ablation 판별용)
  lines=8    fwrite 내용 줄 수 (torn-write 검출은 다중 줄이 필요)
  id=none    로그 상관 태그 (동작 무관)

사용: python bench/multiuser/mock_llm.py [port]   (기본 8099)
에이전트 연결: AGENT_CLI_BASE_URL=http://127.0.0.1:8099/v1
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DEFAULTS = {"ttft": 100, "tok": 5, "n": 12, "work": 0, "fwrite": 0, "lines": 8}
_LOG = bool(os.environ.get("MOCK_LLM_LOG"))
#: /models 가 광고하는 컨텍스트 창 — N1(동시 압축) 실험은 이것을 작게 줘서
#: 압축을 유발한다. 출력 상한은 창의 1/16 로 고정(헤드룸 0 사고 방지).
_CTX = int(os.environ.get("MOCK_LLM_CTX", "131072"))
#: 압축 요약 콜의 지연(ms) — 낙관적 압축의 "무락 구간" 길이를 실험이 제어.
_SUM_MS = int(os.environ.get("MOCK_LLM_SUM_MS", "80"))
_DIRECTIVE = re.compile(r"\[\[bench([^\]]*)\]\]")
_KV = re.compile(r"(\w+)=([^\s\]]+)")


def parse_directive(messages: list[dict]) -> dict:
    """마지막 user 메시지의 ``[[bench ...]]`` 를 파싱. 없으면 기본값."""
    text = ""
    for m in reversed(messages):
        if m.get("role") == "user":
            c = m.get("content", "")
            if isinstance(c, str) and _DIRECTIVE.search(c):
                text = c
                break
    params: dict = dict(DEFAULTS)
    params["fpath"] = ""
    params["marker"] = ""
    params["id"] = "none"
    m = _DIRECTIVE.search(text)
    if m:
        for k, v in _KV.findall(m.group(1)):
            if k in ("ttft", "tok", "n", "work", "fwrite", "lines"):
                try:
                    params[k] = int(v)
                except ValueError:
                    pass
            else:
                params[k] = v
    return params


def tool_steps_done(messages: list[dict]) -> int:
    """마지막 ``[[bench]]`` user 메시지 이후 도착한 관찰 수.

    본류 json_fc 대화에서 도구 결과는 "Observation" 으로 시작하는 user
    메시지로 돌아온다. 이 개수가 곧 이번 지시자에서 완료한 도구 스텝 수 —
    서버 상태 없이 대화만으로 진행도를 판정한다(mockLlm.mjs:73-80 동형).
    """
    last_bench = -1
    for i, m in enumerate(messages):
        if (
            m.get("role") == "user"
            and isinstance(m.get("content"), str)
            and _DIRECTIVE.search(m["content"])
        ):
            last_bench = i
    if last_bench < 0:
        return 0
    done = 0
    for m in messages[last_bench + 1 :]:
        if (
            m.get("role") == "user"
            and isinstance(m.get("content"), str)
            and m["content"].lstrip().lower().startswith("observation")
        ):
            done += 1
    return done


_SUMMARY_MARKERS = ("Transcript to summarise", "Running summary of earlier")


def is_summary_request(messages: list[dict]) -> bool:
    """본류 압축의 요약 콜인가 — 트랜스크립트 안에 [[bench]] 원문이 인용돼
    있으므로 지시자보다 **먼저** 판별해야 한다. 아니면 요약 응답이 긴 턴
    시나리오로 재생돼 압축이 L 만큼 걸린다(실측 +2.4s/턴)."""
    for m in reversed(messages):
        if m.get("role") == "user":
            c = str(m.get("content", ""))
            return any(marker in c for marker in _SUMMARY_MARKERS)
    return False


def build_content(params: dict, messages: list[dict]) -> str:
    """이번 응답의 전체 content (json_fc). 스트리밍은 이것을 쪼개 보낸다."""
    done = tool_steps_done(messages)
    steps = []
    if params["work"] > 0:
        steps.append("work")
    steps.extend(["fwrite"] * params["fwrite"])

    if done < len(steps):
        step = steps[done]
        if step == "work":
            sec = params["work"] / 1000.0
            op = {"action": "shell", "command": f"sleep {sec:g}"}
        else:
            body_lines = [
                f"{params['marker'] or params['id']} line {i + 1} of "
                f"{params['lines']} (step {done + 1}/{params['fwrite']})"
                for i in range(params["lines"])
            ]
            op = {
                "action": "write_file",
                "path": params["fpath"] or "bench-out.txt",
                "content": "\n".join(body_lines) + "\n",
            }
        return "working.\n" + json.dumps([op], ensure_ascii=False)

    return "done.\n" + json.dumps(
        [{"action": "complete", "result": f"bench done id={params['id']}"}],
        ensure_ascii=False,
    )


def pad_to_tokens(content: str, n: int) -> str:
    """content 길이가 n 청크(1자/청크 하한)에 못 미치면 **앞에** 산문 패딩.

    n 이 곧 스트리밍 길이(L = ttft + n×tok)가 되게 하는 보정 — 패딩 없이는
    긴 턴 지시(n=600)가 content 55자에 캡혀 L 을 못 채운다. 패딩을 op 배열
    **앞**에 두는 이유: json_fc 는 "산문 추론 + op 배열" 형태라 앞 산문은
    정상 문법이지만 배열 뒤 텍스트는 파서 잔여물이 된다.
    """
    if len(content) >= n:
        return content
    filler = "reasoning… " * (n // 11 + 1)
    return filler[: n - len(content)] + "\n" + content


class Handler(BaseHTTPRequestHandler):
    # HTTP/1.0 + 무프레이밍 + Connection: close = "닫힐 때까지 읽기" 스트리밍.
    # 1.1 로 두면 chunked/Content-Length 프레이밍이 없어서 클라이언트
    # (urllib3/httpx)의 버퍼링이 비결정적이 된다 — 실측: 같은 응답이 어떤
    # 호출에서는 라이브 스트림, 어떤 호출에서는 종료 시 일괄 도착.
    protocol_version = "HTTP/1.0"

    def log_message(self, *args):  # 소음 제거
        pass

    def do_GET(self):
        """``/models`` 능력 프로브 응답 — 없으면 provider 가 부팅/첫 콜에서
        실패 프로브+백오프(~1.3s 실측)를 물고 첫 턴 TTFT 를 오염시킨다."""
        if self.path.rstrip("/").endswith("/models"):
            body = json.dumps(
                {
                    "object": "list",
                    "data": [
                        {
                            "id": "bench-mock",
                            "object": "model",
                            # 본류 감지가 읽는 필드는 vLLM 스타일
                            # ``max_model_len`` (capabilities.py
                            # _detect_openai_context_window tier 1) —
                            # 다른 이름(context_window 등)은 무시되고 128K
                            # 폴백으로 떨어져 N1 의 압축이 영영 안 걸린다
                            # (실측). 출력 상한은 감지기가 창/4 로 계산한다.
                            "max_model_len": _CTX,
                        }
                    ],
                }
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_error(404)

    def do_POST(self):
        if not self.path.rstrip("/").endswith("/chat/completions"):
            self.send_error(404)
            return
        length = int(self.headers.get("Content-Length", 0))
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            self.send_error(400)
            return
        messages = body.get("messages", [])
        if is_summary_request(messages):
            params = dict(
                DEFAULTS, ttft=_SUM_MS, tok=1, n=4, fpath="", marker="", id="summary"
            )
            content = "## Summary\nbench transcript summarised."
        else:
            params = parse_directive(messages)
            content = pad_to_tokens(
                build_content(params, messages), max(1, params["n"])
            )
        if _LOG:
            tail = messages[-1] if messages else {}
            print(
                json.dumps(
                    {
                        "t": time.time(),
                        "picked_id": params["id"],
                        "n": params["n"],
                        "msgs": len(messages),
                        "tail_role": tail.get("role"),
                        "tail_head": str(tail.get("content", ""))[:80],
                    },
                    ensure_ascii=False,
                ),
                file=sys.stderr,
                flush=True,
            )

        # 비스트리밍 호출(stream 미지정/false — 압축 요약 콜이 이 모양)은
        # 일반 JSON completion 으로 응답한다. SSE 를 보내면 클라이언트가
        # content 를 못 읽어 CompactionError 가 난다(실측: N1 에서 압축
        # 12/12 실패). 지연은 총합(ttft + n×tok)을 한 번에 잔다.
        if not body.get("stream"):
            time.sleep((params["ttft"] + params["n"] * params["tok"]) / 1000.0)
            payload = json.dumps(
                {
                    "id": "mock",
                    "object": "chat.completion",
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": content},
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {
                        "prompt_tokens": sum(
                            len(str(m.get("content", ""))) // 4 for m in messages
                        ),
                        "completion_tokens": len(content) // 4,
                    },
                }
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return

        # 토큰화: content 를 **정확히 n 조각**으로 분할 (패딩이 1자/조각 하한
        # 보장). ceil-크기 슬라이싱은 len 이 n 을 살짝 넘을 때 조각 수가
        # 절반으로 줄어 L 을 못 채운다(실측: 81자/n=80 → 41조각) — 경계
        # 인덱스 방식으로 자른다. op JSON 은 임의 지점에서 잘려도 무관.
        n = max(1, params["n"])
        bounds = [len(content) * i // n for i in range(n + 1)]
        chunks = [
            content[bounds[i] : bounds[i + 1]]
            for i in range(n)
            if bounds[i + 1] > bounds[i]
        ]

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()

        def sse(payload: dict):
            self.wfile.write(b"data: " + json.dumps(payload).encode() + b"\n\n")
            self.wfile.flush()

        time.sleep(params["ttft"] / 1000.0)
        try:
            for i, chunk in enumerate(chunks):
                if i > 0:
                    time.sleep(params["tok"] / 1000.0)
                sse(
                    {
                        "id": "mock",
                        "object": "chat.completion.chunk",
                        "choices": [
                            {"index": 0, "delta": {"content": chunk}},
                        ],
                    }
                )
            sse(
                {
                    "id": "mock",
                    "object": "chat.completion.chunk",
                    "choices": [
                        {"index": 0, "delta": {}, "finish_reason": "stop"},
                    ],
                    "usage": {
                        "prompt_tokens": sum(
                            len(str(m.get("content", ""))) // 4 for m in messages
                        ),
                        "completion_tokens": len(content) // 4,
                    },
                }
            )
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass  # 클라이언트 중단(인터럽트 벤치) — 정상 경로


def main() -> None:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8099
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"mock LLM listening on http://127.0.0.1:{port}/v1", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
